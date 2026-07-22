"""Tests for strategic_spawning.py decision logic.

Covers:
  - Type selection: design_doc_update on architecture cycles, strategic_review on strategy cycles
  - force_strategic: no vision purpose, no active directions
  - strategic_failures >= 3 blocks force_strategic (without auto-decay)
  - operator_rejection_rate calculation in _spawn_strategic_review direction stats
  - Active-task guard: _spawn_design_doc_update and _spawn_strategic_review return False when blocked
  - _build_shipped_changes_manifest: cutoff, filtering, grouping, cap
  - Autonomy fields (lifetime_autonomous_prs_merged, lifetime_operator_actions, retirement_eligible)
    in directions_summary for the strategic agent
"""

import json
import os
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy.exc import MissingGreenlet
from unittest.mock import AsyncMock, call, patch
from sqlalchemy import or_, select, update

import app.drift_detection  # noqa: F401 — registers DirectionDriftSnapshot in SQLModel.metadata for create_all
import app.shared_state as _shared_state
from app.models import (
    Artifact,
    ArtifactType,
    LocalRunner,
    Project,
    Task,
    TaskStatus,
    MessageType,
    ProjectMessage,
    RECOMMENDED_MODE_META_KEY,
)
from app.strategic_trigger import ORIGIN_OPERATOR, ORIGIN_QUALIFICATION
from app.workbench_state import (
    SESSION_IDLE_TTL_SECONDS,
    STRATEGIC_LOCAL_CONTINUATION_KEY,
    STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS,
    STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
)
from app.autonomous_research import _spawn_research_task
from app.strategic_spawning import (
    _spawn_design_doc_update,
    _spawn_strategic_review,
    _direction_activity_score,
    _build_operator_steering_directive,
    _build_shipped_changes_manifest,
    _build_recent_interventions,
    _agent_view_problem_model_packed,
    _AGENT_VIEW_FLOOR_BYTES,
    _FLOOR_MIN_GAPS,
    _STRATEGIC_REVIEW_SCHEMA,
    _DESIGN_DOC_UPDATE_SCHEMA,
    _design_doc_index,
    _summarize_design_doc_index,
    _DESIGN_DOC_INDEX_SUMMARY_CHARS,
    _OPERATOR_CONTEXT_REFS_KEEP,
)
from app.task_completion import _phase_runner_affinity
from app.payload_schemas import pack_under_budget, validate_internal_payload
from app.capability_demand import collect_missing_capability_demand
from app.max_plan_pool import _max_plan_pools, _account_by_id
from app.task_evaluators import (
    CHALLENGE_AGREES,
    CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
    CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
    CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH,
    CHALLENGE_DISAGREES,
    CHALLENGE_INDETERMINATE,
)
from app.task_capabilities import BACKEND_MANAGED_TASK_TYPES


def _select_agent_tasks(project_id):
    """Select project Tasks while excluding hidden backend control work."""

    task_type = Task.payloads[0]["type"].as_string()
    return select(Task).where(
        Task.project_id == project_id,
        or_(
            task_type.is_(None),
            task_type.not_in(sorted(BACKEND_MANAGED_TASK_TYPES)),
        ),
    )

USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


def _assert_generated_at_iso_utc(payload: dict) -> None:
    generated_at = payload.get("generated_at")
    assert isinstance(generated_at, str) and generated_at
    parsed = datetime.fromisoformat(generated_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Account pool fixture (required by _spawn_research_task) ───────────────────


@pytest.fixture(autouse=True)
def _ensure_test_account_pool():
    """Ensure _spawn_research_task finds an available account for the test user."""
    uid = "test-user-123"
    entry = {
        "id": "test-acct-ss",
        "user_id": uid,
        "name": "test",
        "enabled": True,
        "active_tasks": 0,
        "oauth_token": "sk-ant-test",
        "total_tasks": 0,
        "token_created_at": None,
        "claude_config_json": None,
    }
    _max_plan_pools.setdefault(uid, []).append(entry)
    _account_by_id["test-acct-ss"] = entry
    yield
    _max_plan_pools.get(uid, [])[:] = [
        a for a in _max_plan_pools.get(uid, []) if a["id"] != "test-acct-ss"
    ]
    _account_by_id.pop("test-acct-ss", None)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_project(**kwargs) -> Project:
    """Build a Project with sensible test defaults."""
    defaults = {
        "name": "Test Project",
        "user_id": "test-user-123",
        "autonomous_enabled": True,
        "directions": [],
        "vision": {},
        "integration_branch": "automain",
        "promotion_target": "main",
    }
    defaults.update(kwargs)
    return Project(**defaults)


def _local_continuation_marker(**overrides) -> dict:
    marker = {
        "version": 1,
        "active": True,
        "operator_user_id": "test-user-123",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
    }
    marker.update(overrides)
    return marker


def _make_local_runner(project_id: uuid.UUID, **overrides) -> LocalRunner:
    defaults = {
        "project_id": project_id,
        "token_hash": uuid.uuid4().hex + uuid.uuid4().hex,
        "status": "active",
        "capabilities": {
            "task_types": "all",
            "has_gpu": False,
            "max_concurrent": 2,
        },
        "last_seen_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }
    defaults.update(overrides)
    return LocalRunner(**defaults)


async def _seed_legacy_cycles(session, project: Project) -> None:
    """Audit W2: seed StrategicCycle rows from legacy ``vision._strategic_cycle_history``.

    Tests historically built cycle history as a JSONB list inline in vision.
    The W2 migration moved cycles to a real table; this helper backfills the
    table from the legacy shape so existing test fixtures still produce the
    same agent-context payload without rewriting every test.
    """
    from app.models import StrategicCycle as _SC
    from app.time_utils import _utcnow as _ut
    from datetime import datetime as _dt

    raw = (project.vision or {}).get("_strategic_cycle_history") or []
    if not raw:
        return

    def _parse_ts(v):
        if not v:
            return None
        try:
            d = _dt.fromisoformat(str(v).replace("Z", "+00:00"))
            return d.replace(tzinfo=None) if d.tzinfo else d
        except Exception:
            return None

    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("cycle_id"):
            continue
        # Skip if a row already exists for this cycle id.
        existing = await session.get(_SC, entry["cycle_id"])
        if existing is not None:
            continue
        meta = {
            k: v
            for k, v in entry.items()
            if k
            not in {
                "cycle_id",
                "outcome",
                "phase1_task_id",
                "phase1_completed_at",
                "phase2_task_id",
                "phase2_completed_at",
                "phase1_started_at",
                "directions_created",
                "directions_retired",
                "tasks_spawned",
                "vision_updated",
                "cycle_reasoning",
                "summary_text",
                "started_at",
            }
        }
        session.add(
            _SC(
                id=entry["cycle_id"],
                project_id=project.id,
                outcome=entry.get("outcome") or "in_progress",
                phase1_task_id=None,  # legacy entries store str not UUID; skip FK
                phase1_started_at=_parse_ts(entry.get("started_at"))
                or _parse_ts(entry.get("phase1_started_at"))
                or _ut(),
                phase1_completed_at=_parse_ts(entry.get("phase1_completed_at")),
                phase2_started_at=_parse_ts(entry.get("phase2_started_at")),
                phase2_completed_at=_parse_ts(entry.get("phase2_completed_at")),
                directions_created=int(entry.get("directions_created") or 0),
                directions_retired=int(entry.get("directions_retired") or 0),
                tasks_spawned=int(entry.get("tasks_spawned") or 0),
                vision_updated=bool(entry.get("vision_updated", False)),
                cycle_reasoning=entry.get("cycle_reasoning"),
                summary_text=entry.get("summary_text"),
                cycle_metadata=meta,
            )
        )
    await session.commit()


def _patch_spawning_deps(mocker):
    """Patch external dependencies required by the spawning functions."""
    mocker.patch(
        "app.strategic_spawning.validate_internal_payload",
        side_effect=lambda p, _src: p,
    )
    mocker.patch(
        "app.strategic_analysis._build_outcome_evaluation",
        return_value={},
    )


@pytest.fixture
def _lts_split_active(monkeypatch):
    """Activate the LTS split for version-lifecycle tests.

    The strategic version-phase block is gated on the integration branch being
    a real trunk distinct from the promotion target. Override the per-project
    resolvers (the global runtime_config bridge was removed) so tests using
    `_make_project()` without an explicit `integration_branch` column see a
    distinct trunk/target pair.
    """
    monkeypatch.setattr(
        "app.strategic_spawning.get_project_integration_branch",
        lambda _p: "automain",
    )
    monkeypatch.setattr(
        "app.strategic_spawning.get_project_promotion_target",
        lambda _p: "main",
    )
    yield


# ── Type selection: arch_frequency / strategic_cycle_count ────────────────────


@pytest.mark.asyncio
async def test_architecture_cycle_spawns_design_doc_update(db_session):
    """strategic_cycle_count divisible by arch_frequency → design_doc_update."""
    # With arch_frequency=3, _strategic_cycle_count=2 → next=3 → architecture cycle.
    project = _make_project(
        vision={
            # No purpose → force_strategic fires due to missing purpose
            "_strategic_cycle_count": 2,
            "_research_spawn_count": 5,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old = _shared_state.runtime_config.get("architecture_review_frequency", 3)
    _shared_state.runtime_config["architecture_review_frequency"] = 3
    try:
        await _spawn_research_task(project, db_session)
    finally:
        _shared_state.runtime_config["architecture_review_frequency"] = old

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1, f"Expected one task, got {len(tasks)}"
    assert tasks[0].payloads[0]["type"] == "design_doc_update", (
        f"Expected design_doc_update on architecture cycle, got {tasks[0].payloads[0]['type']}"
    )


@pytest.mark.asyncio
async def test_strategy_only_cycle_spawns_strategic_review(db_session):
    """strategic_cycle_count NOT divisible by arch_frequency → strategic_review."""
    # With arch_frequency=3, _strategic_cycle_count=1 → next=2 → strategy-only cycle.
    project = _make_project(
        vision={
            "_strategic_cycle_count": 1,
            "_research_spawn_count": 5,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old = _shared_state.runtime_config.get("architecture_review_frequency", 3)
    _shared_state.runtime_config["architecture_review_frequency"] = 3
    try:
        await _spawn_research_task(project, db_session)
    finally:
        _shared_state.runtime_config["architecture_review_frequency"] = old

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1, f"Expected one task, got {len(tasks)}"
    assert tasks[0].payloads[0]["type"] == "strategic_review", (
        f"Expected strategic_review on strategy-only cycle, got {tasks[0].payloads[0]['type']}"
    )


@pytest.mark.asyncio
async def test_strategy_only_cycle_records_strategic_cycle_row(db_session):
    """Strategy-only cycles must create + link a StrategicCycle row.

    Regression (digest "latest strategic cycle" card kept going blank):
    strategy-only reviews historically minted no cycle_id, so ~2/3 of strategic
    cycles (architecture_review_frequency defaults to every 3rd) were never
    recorded in StrategicCycle. The card only ever reflected the rarer, fragile
    architecture cycles, and showed nothing fresh whenever one stalled. A
    strategy-only spawn must now create a ``phase2_running`` row linked to the
    review task — with no architecture phase1 — so the normal phase-2
    completion accounting finalizes it like any other cycle.
    """
    from app.models import StrategicCycle

    # arch_frequency=3, _strategic_cycle_count=1 → next=2 → strategy-only cycle.
    project = _make_project(
        vision={
            "_strategic_cycle_count": 1,
            "_research_spawn_count": 5,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old = _shared_state.runtime_config.get("architecture_review_frequency", 3)
    _shared_state.runtime_config["architecture_review_frequency"] = 3
    try:
        await _spawn_research_task(project, db_session)
    finally:
        _shared_state.runtime_config["architecture_review_frequency"] = old

    tasks = (
        (await db_session.execute(_select_agent_tasks(project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 1, f"Expected one task, got {len(tasks)}"
    review_task = tasks[0]
    assert review_task.payloads[0]["type"] == "strategic_review"

    # The review task carries a freshly-minted cycle id (task column + context).
    cycle_id = review_task.strategic_cycle_id
    assert cycle_id, "strategy-only review task has no strategic_cycle_id"
    assert (review_task.context or {}).get("strategic_cycle_id") == cycle_id

    # A StrategicCycle row exists for it, advanced to phase2_running, linked via
    # phase2_task_id, with no architecture phase1 task — and phase1_started_at
    # set so the digest's DESC ordering surfaces it as the project's latest.
    cycle = await db_session.get(StrategicCycle, cycle_id)
    assert cycle is not None, "strategy-only cycle created no StrategicCycle row"
    assert cycle.project_id == project.id
    assert cycle.outcome == "phase2_running"
    assert cycle.phase2_task_id == review_task.id
    assert cycle.phase1_task_id is None
    assert cycle.phase1_completed_at is not None
    assert cycle.phase1_started_at is not None


@pytest.mark.asyncio
async def test_arch_frequency_zero_always_spawns_strategic_review(db_session):
    """arch_frequency=0 disables architecture cycles → always strategic_review."""
    # _strategic_cycle_count=2 would be an arch cycle with freq=3, but freq=0 disables it.
    project = _make_project(
        vision={
            "_strategic_cycle_count": 2,
            "_research_spawn_count": 5,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old = _shared_state.runtime_config.get("architecture_review_frequency", 3)
    _shared_state.runtime_config["architecture_review_frequency"] = 0
    try:
        await _spawn_research_task(project, db_session)
    finally:
        _shared_state.runtime_config["architecture_review_frequency"] = old

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1, f"Expected one task, got {len(tasks)}"
    assert tasks[0].payloads[0]["type"] == "strategic_review", (
        f"Expected strategic_review when arch_frequency=0, got {tasks[0].payloads[0]['type']}"
    )


@pytest.mark.asyncio
async def test_strategic_cycle_count_increments_in_vision(db_session):
    """After a strategic cycle spawns, _strategic_cycle_count increments by 1."""
    project = _make_project(
        vision={
            "_strategic_cycle_count": 0,
            "_research_spawn_count": 5,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    # Assert on in-memory attribute — refresh would re-read from DB, overwriting
    # uncommitted vision changes (see filter-test-session-refresh-pattern).
    assert (project.vision or {}).get("_strategic_cycle_count") == 1, (
        "_strategic_cycle_count should be incremented to 1 after one strategic spawn"
    )


# ── force_strategic: no vision purpose ────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_vision_purpose_triggers_force_strategic(db_session):
    """Missing vision purpose triggers force_strategic even when spawn count is low."""
    project = _make_project(
        directions=[
            {
                "id": "d-001",
                "status": "active",
                "title": "Test direction",
                "possibly_saturated": False,
            },
        ],
        vision={
            # No "purpose" key → force_strategic fires
            "_research_spawn_count": 1,  # Not at strategic_frequency (6), but force overrides
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    task_types = [t.payloads[0]["type"] for t in tasks]
    assert any(t in ("design_doc_update", "strategic_review") for t in task_types), (
        f"Expected a strategic task when vision has no purpose, got {task_types}"
    )


@pytest.mark.asyncio
async def test_no_active_directions_triggers_force_strategic(db_session):
    """No active directions triggers force_strategic when strategic_failures < 3."""
    project = _make_project(
        directions=[
            {"id": "d-001", "status": "paused", "title": "Paused direction"},
        ],
        vision={
            "purpose": "test purpose",
            "_research_spawn_count": 1,  # Not at strategic_frequency, but force applies
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    task_types = [t.payloads[0]["type"] for t in tasks]
    assert any(t in ("design_doc_update", "strategic_review") for t in task_types), (
        f"Expected a strategic task when no active directions, got {task_types}"
    )


# ── force_strategic: strategic_failures >= 3 blocks ──────────────────────────


@pytest.mark.asyncio
async def test_strategic_failures_exactly_three_blocks_force_strategic(db_session):
    """_strategic_failures == 3 (no decay timestamp) → force_strategic is False → no task."""
    project = _make_project(
        directions=[],  # No active directions — would normally force strategic
        vision={
            "purpose": "test purpose",
            "_strategic_failures": 3,
            # No _strategic_failures_at → auto-decay cannot apply
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 0, (
        f"Expected no tasks when strategic_failures >= 3 blocks force_strategic, "
        f"got {[t.payloads[0]['type'] for t in tasks]}"
    )


@pytest.mark.asyncio
async def test_strategic_failures_below_three_allows_force_strategic(db_session):
    """_strategic_failures == 2 → force_strategic is allowed → spawns a strategic task."""
    project = _make_project(
        directions=[],  # No active directions → force_strategic condition met
        vision={
            "purpose": "test purpose",
            "_strategic_failures": 2,
            "_research_spawn_count": 1,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1, f"Expected one strategic task, got {len(tasks)}"
    assert tasks[0].payloads[0]["type"] in ("design_doc_update", "strategic_review")


# ── operator_overrides in _spawn_strategic_review ─────────────────────────────


@pytest.mark.asyncio
async def test_spawn_review_includes_operator_overrides_when_present(db_session):
    """The strategic_review payload surfaces the consolidated operator-override
    summary when a reconciliation ledger carries entries."""
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_rejected_agent_edits": [
                {
                    "at": "2026-06-03T10:00:00Z",
                    "field": "running_update",
                    "kind": "running_update_superseded",
                    "agent_value": "my rewrite",
                }
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "operator_overrides" in payload
    assert payload["operator_overrides"][0]["document"] == "running_update"


# ── extra_context born-context seeding (SPAWN-path race fix) ──


@pytest.mark.asyncio
async def test_spawn_strategic_review_seeds_extra_context_at_birth(db_session):
    """``extra_context`` is merged into the created task's ``context`` in the SAME
    INSERT that creates it, so the committed task is BORN already carrying those
    keys (it never exists in a committed state without them). This is the atomic
    seam a caller uses to make its spawned strategic_review claimable ONLY with
    the pinning flags already set — closing the window in which a warm runner
    could claim it flag-less and dispatch the one-shot."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    extra = {
        "_born_marker": True,
        "strategic_session_id": "ss-born-1234",
        STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
        STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: "operator_session",
        "preferred_runner_target_id": "runner-abc",
    }
    spawned = await _spawn_strategic_review(project, db_session, extra_context=extra)
    assert spawned is True

    task = (
        (await db_session.execute(_select_agent_tasks(project.id)))
        .scalars()
        .all()[0]
    )
    # The task's FIRST (and only) committed context carries the born flags — proof
    # the seed rode the creating INSERT, not a follow-up transaction.
    ctx = task.context or {}
    assert ctx.get("_born_marker") is True
    assert ctx.get("strategic_session_id") == "ss-born-1234"
    assert ctx.get(STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY) is True
    assert ctx.get(STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY) == "operator_session"
    assert ctx.get("preferred_runner_target_id") == "runner-abc"
    # The function's own derived context (the cycle id) is preserved alongside.
    assert ctx.get("strategic_cycle_id") == task.strategic_cycle_id
    assert task.strategic_cycle_id


@pytest.mark.asyncio
async def test_spawn_strategic_review_default_extra_context_unchanged(db_session):
    """Regression: a normal cadence spawn (``extra_context`` defaulting to None)
    is unaffected — the created task carries NO born-context flags, exactly as
    before the parameter was added."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    task = (
        (await db_session.execute(_select_agent_tasks(project.id)))
        .scalars()
        .all()[0]
    )
    ctx = task.context or {}
    # No born-context leaked into a plain cadence spawn.
    assert "_born_marker" not in ctx
    assert "strategic_session_id" not in ctx
    assert STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY not in ctx
    # The cadence cycle id is still set as before.
    assert ctx.get("strategic_cycle_id") == task.strategic_cycle_id
    assert task.strategic_cycle_id


@pytest.mark.asyncio
async def test_spawn_strategic_review_cadence_still_runs_context_build(db_session):
    """Regression guard: a plain cadence spawn STILL runs the full context build.
    The payload carries the assembled ``recent_interventions`` context (produced
    solely by the inline build) the strategist consumes."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    task = (
        (await db_session.execute(_select_agent_tasks(project.id)))
        .scalars()
        .all()[0]
    )
    payload = task.payloads[0]
    # The heavy build ran: the full-context keys are present.
    assert "recent_interventions" in payload
    assert payload["type"] == "strategic_review"


@pytest.mark.asyncio
async def test_drift_snapshot_failure_does_not_expire_caller_instances(
    db_session, monkeypatch
):
    """A failed best-effort drift snapshot must not poison the shared session.

    The prior code cleared the resulting pending-rollback with a bare
    ``session.rollback()``, which expired EVERY ORM instance in the session —
    ``project`` and the in-flight strategic ``task`` included (rollback expires
    even under ``expire_on_commit=False``). The next *synchronous* attribute
    read then attempted IO and raised ``MissingGreenlet``, failing the whole
    design_doc completion (tasks a9bf1317 / 94594516, 2026-06-11). The drift
    block now runs in a SAVEPOINT, so its failure leaves ``project`` loaded.

    Fault-injection: a proxy wraps ``project`` and raises ``MissingGreenlet``
    on any attribute access if ``session.rollback()`` (a full rollback, not a
    SAVEPOINT rollback via ``begin_nested``) has been called.  With the fix the
    drift block rolls back only its SAVEPOINT, never the outer session, so the
    proxy is never poisoned and ``proxy.vision`` returns the live in-memory
    value.  Without the fix a bare ``session.rollback()`` would poison the
    proxy, making the post-spawn ``proxy.vision`` access raise
    ``MissingGreenlet`` — exactly the production failure mode.
    """
    import app.drift_detection as _drift_mod

    class _ProjectPoisonAfterOuterRollback:
        """Delegates all attribute access to the real project until a full
        ``session.rollback()`` is observed; after that any access raises
        ``MissingGreenlet``, mimicking asyncpg post-rollback expiry."""

        def __init__(self, real_project):
            self._real = real_project
            self._rolled_back = False

        def __getattr__(self, name):
            if self._rolled_back:
                raise MissingGreenlet(
                    f"simulated post-rollback lazy-load: attribute '{name}' expired"
                )
            return getattr(self._real, name)

    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    proxy = _ProjectPoisonAfterOuterRollback(project)
    real_rollback = db_session.rollback

    async def rollback_then_poison(*args, **kwargs):
        result = await real_rollback(*args, **kwargs)
        proxy._rolled_back = True
        return result

    monkeypatch.setattr(db_session, "rollback", rollback_then_poison)

    # Force the best-effort drift snapshot to fail mid-spawn.
    monkeypatch.setattr(
        _drift_mod,
        "persist_drift_snapshot",
        AsyncMock(side_effect=RuntimeError("drift flush boom")),
    )

    spawned = await _spawn_strategic_review(proxy, db_session)

    # The spawn still lands the strategic_review task despite the drift failure.
    assert spawned is True
    # Regression guard: the caller's ``project`` is NOT expired — a synchronous
    # attribute read must return the in-memory value without IO. Under the old
    # bare-rollback this access raised MissingGreenlet.
    assert proxy.vision.get("purpose") == "test purpose"
    result = await db_session.execute(select(Task).where(Task.project_id == proxy.id))
    assert any(
        t.payloads[0].get("type") == "strategic_review" for t in result.scalars().all()
    )


@pytest.mark.asyncio
async def test_spawn_review_includes_capability_trial_verdicts(db_session):
    """The strategic_review payload carries durable capability trial verdicts."""
    project = _make_project(
        vision={"purpose": "test purpose"},
        capability_adoption_verdicts={
            "language-aware-repo-navigation": {
                "verdict": "adopt",
                "lift": 0.12,
                "n_with": 42,
                "n_without": 41,
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert payload["capability_trial_verdicts"] == {
        "language-aware-repo-navigation": {
            "verdict": "adopt",
            "lift": 0.12,
            "n_with": 42,
            "n_without": 41,
        }
    }


@pytest.mark.asyncio
async def test_spawn_review_includes_model_health_signal(db_session):
    """The strategic_review payload carries the soft model size/health signal
    computed from the DURABLE problem_model (honest pre-trim counts)."""
    # 24 active gaps = 2x the healthy threshold (12) -> 'large'.
    problem_model = {
        "gaps": [
            {"id": f"g-{i}", "name": f"gap {i}", "status": "active"} for i in range(24)
        ],
    }
    project = _make_project(
        vision={"purpose": "test purpose"},
        problem_model=problem_model,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "model_health" in payload
    mh = payload["model_health"]
    assert mh["active_gap_count"] == 24
    assert mh["model_size_status"] == "large"
    assert set(mh.keys()) == {
        "active_gap_count",
        "capability_count",
        "active_friction_count",
        "estimated_token_footprint",
        "serialized_bytes",
        "model_size_status",
    }


@pytest.mark.asyncio
async def test_spawn_review_omits_operator_overrides_when_empty(db_session):
    """The payload omits operator_overrides entirely when no ledger entries exist."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "operator_overrides" not in payload


# ── knowledge_index in strategic payloads ───────────────────────────────────


async def _seed_knowledge_doc(session, project, *, title="KDoc", direction_id=None):
    """Persist one KnowledgeDocument for the project (FK source_task_id needs a task)."""
    from app.models import KnowledgeDocument

    task = Task(
        project_id=project.id,
        title="src",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "web_research", "topic": "t", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=task.id,
        title=title,
        summary="A prior knowledge summary.",
        body="# Body\n\n" + ("x" * 50),
        topic="auth",
        direction_id=direction_id,
        source_kind="web_research",
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)
    return doc


def test_knowledge_index_registered_in_both_schemas():
    """The packer must know about knowledge_index so it never warns / drops it
    as an unregistered key."""
    assert "knowledge_index" in _STRATEGIC_REVIEW_SCHEMA
    assert "knowledge_index" in _DESIGN_DOC_UPDATE_SCHEMA


@pytest.mark.asyncio
async def test_spawn_review_includes_knowledge_index_when_docs_exist(db_session):
    """strategic_review carries the compact, body-omitted knowledge index when
    the project has knowledge documents."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)
    await _seed_knowledge_doc(db_session, project, title="Prior research")

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.payloads[0]["type"] == "strategic_review"
    )

    assert "knowledge_index" in payload
    entry = payload["knowledge_index"][0]
    assert entry["title"] == "Prior research"
    assert "body" not in entry
    assert set(entry.keys()) == {
        "id",
        "title",
        "summary",
        "topic",
        "direction_id",
        "source_kind",
        "analysis_fallback_reason",
        "created_at",
        "knowledge_acceptance_evidence",
        "compute_acceptance_grade",
        "compute_acceptance_reason",
        "unavailable_cause",
        "criterion_met",
        "criterion",
        "criterion_unmeasured_reason",
        "evidence_tier",
        "per_arm_verdicts",
        "origin_provenance",
        "run_provenance",
        "sweep_id",
        "arm_label",
        "lifecycle_status",
        "evidence_tier",
        "run_provenance",
        "criterion_unmeasured_reason",
    }


@pytest.mark.asyncio
async def test_spawn_review_omits_knowledge_index_when_none(db_session):
    """strategic_review omits knowledge_index entirely when no documents exist."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.payloads[0]["type"] == "strategic_review"
    )
    assert "knowledge_index" not in payload


# ── compute_experiment_sweeps in strategic payloads ──────────────────────────


async def _seed_sweep_arm(
    session,
    project,
    *,
    sweep_id,
    arm_label,
    title="Arm doc",
    criterion_met=True,
    direction_id=None,
    persist_doc=True,
):
    """Persist one compute_experiment KnowledgeDocument carrying sweep identity.

    criterion_met (True/False/None) is stored in forward_quality_verdict so
    that _knowledge_index_entry surfaces the true success-criterion verdict.
    """
    from app.models import KnowledgeDocument

    task = Task(
        project_id=project.id,
        title="compute src",
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "compute_experiment",
                "base_branch": "main",
                "sweep_id": sweep_id,
                "experiment": arm_label,
            }
        ],
        context=({"direction_id": direction_id} if direction_id else {}),
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    if not persist_doc:
        return None
    forward_quality_verdict = {"analysis_mode": "deterministic", "criterion_met": criterion_met}
    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=task.id,
        title=title,
        summary="Arm measured summary.",
        body="# Body\n\n" + ("x" * 50),
        topic="perf",
        source_kind="compute_experiment",
        sweep_id=sweep_id,
        arm_label=arm_label,
        forward_quality_verdict=forward_quality_verdict,
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)
    return doc


def test_compute_experiment_sweeps_registered_in_both_schemas():
    """The packer must know about compute_experiment_sweeps so it never warns /
    drops it as an unregistered key."""
    assert "compute_experiment_sweeps" in _STRATEGIC_REVIEW_SCHEMA
    assert "compute_experiment_sweeps" in _DESIGN_DOC_UPDATE_SCHEMA


@pytest.mark.asyncio
async def test_spawn_review_includes_sweep_groups_when_multi_arm_sweep_exists(
    db_session,
):
    """strategic_review carries the per-sweep comparative grouping when a sweep
    has at least two completed arms."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)
    await _seed_sweep_arm(
        db_session, project, sweep_id="sw-aaaa", arm_label="baseline"
    )
    await _seed_sweep_arm(
        db_session, project, sweep_id="sw-aaaa", arm_label="treatment"
    )

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.payloads[0]["type"] == "strategic_review"
    )
    assert "compute_experiment_sweeps" in payload
    groups = payload["compute_experiment_sweeps"]
    # build_compute_sweep_groups returns {sweep_id: [_knowledge_index_entry, ...]}
    assert "sw-aaaa" in groups
    assert {entry["arm_label"] for entry in groups["sw-aaaa"]} == {"baseline", "treatment"}


@pytest.mark.asyncio
async def test_spawn_review_omits_sweep_groups_when_none(db_session):
    """strategic_review omits compute_experiment_sweeps entirely when no
    multi-arm sweeps have completed."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)
    # A single-arm sweep is not comparative → no group emitted.
    await _seed_sweep_arm(db_session, project, sweep_id="sw-solo", arm_label="only")

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.payloads[0]["type"] == "strategic_review"
    )
    assert "compute_experiment_sweeps" not in payload


@pytest.mark.asyncio
async def test_spawn_review_omits_incomplete_planned_sweep(db_session):
    """A planned arm without a usable document keeps the sweep out of payloads."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)
    await _seed_sweep_arm(
        db_session, project, sweep_id="sw-incomplete", arm_label="baseline"
    )
    await _seed_sweep_arm(
        db_session,
        project,
        sweep_id="sw-incomplete",
        arm_label="treatment",
        persist_doc=False,
    )

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.payloads[0]["type"] == "strategic_review"
    )
    assert "compute_experiment_sweeps" not in payload


@pytest.mark.asyncio
async def test_spawn_review_sweep_entries_carry_criterion_met(db_session):
    """strategic_review sweep entries expose criterion_met from forward_quality_verdict,
    not from the acceptance grade — ensuring the strategist sees the true verdict."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)
    # Two arms: one met (True), one not met (False). Both use measured_only grade
    # (no source_citations, no llm analysis) to prove verdict is criterion_met-derived.
    await _seed_sweep_arm(
        db_session, project, sweep_id="sw-crit", arm_label="met", criterion_met=True
    )
    await _seed_sweep_arm(
        db_session, project, sweep_id="sw-crit", arm_label="not_met", criterion_met=False
    )

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.payloads[0]["type"] == "strategic_review"
    )
    assert "compute_experiment_sweeps" in payload
    groups = payload["compute_experiment_sweeps"]
    assert "sw-crit" in groups
    by_label = {e["arm_label"]: e for e in groups["sw-crit"]}
    # Met arm: criterion_met=True, grade is NOT acceptance.
    assert by_label["met"]["criterion_met"] is True
    assert by_label["met"]["compute_acceptance_grade"] != "acceptance"
    # Not-met arm: criterion_met=False.
    assert by_label["not_met"]["criterion_met"] is False


@pytest.mark.asyncio
async def test_spawn_design_doc_update_includes_knowledge_index_when_docs_exist(
    db_session,
):
    """design_doc_update carries the knowledge index when documents exist."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_knowledge_doc(db_session, project, title="Design knowledge")

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.payloads[0]["type"] == "design_doc_update"
    )
    assert "knowledge_index" in payload
    assert payload["knowledge_index"][0]["title"] == "Design knowledge"


@pytest.mark.asyncio
async def test_spawn_design_doc_update_omits_knowledge_index_when_none(db_session):
    """design_doc_update omits knowledge_index entirely when no documents exist."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.payloads[0]["type"] == "design_doc_update"
    )
    assert "knowledge_index" not in payload


# ── operator_rejection_rate in _spawn_strategic_review ────────────────────────


@pytest.mark.asyncio
async def test_operator_rejection_rate_computed_correctly(db_session):
    """operator_rejection_rate = rejected / (merged + rejected) when total_rejected > 0."""
    project = _make_project(
        directions=[
            {
                "id": "d-rej01",
                "status": "active",
                "text": "Direction with rejections",
                "priority": "medium",
                "research_history": [
                    {"prs_merged": 3, "prs_rejected": 1},
                    {"prs_merged": 1, "prs_rejected": 1},
                ],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-rej01")

    # 4 merged + 2 rejected = 6 total PRs, rate = 2/6 ≈ 0.33
    assert "operator_rejection_rate" in d_stat, (
        "Expected operator_rejection_rate in direction stat"
    )
    assert d_stat["operator_rejection_rate"] == round(2 / 6, 2)
    assert d_stat.get("prs_rejected") == 2


@pytest.mark.asyncio
async def test_operator_rejection_rate_absent_when_no_rejections(db_session):
    """operator_rejection_rate and prs_rejected are absent when total_rejected == 0."""
    project = _make_project(
        directions=[
            {
                "id": "d-norej01",
                "status": "active",
                "text": "Direction with no rejections",
                "priority": "medium",
                "research_history": [
                    {"prs_merged": 4, "prs_rejected": 0},
                ],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-norej01")

    assert "operator_rejection_rate" not in d_stat, (
        "operator_rejection_rate should not appear when total_rejected == 0"
    )
    assert "prs_rejected" not in d_stat, (
        "prs_rejected should not appear when total_rejected == 0"
    )


@pytest.mark.asyncio
async def test_success_evidence_present_in_direction_stats(db_session):
    """success_evidence/retirement_condition from context reach the direction stat entry, capped."""
    long_evidence = "E" * 700
    project = _make_project(
        directions=[
            {
                "id": "d-se01",
                "status": "active",
                "text": "Direction with stated outcome",
                "priority": "medium",
                "context": {
                    "success_evidence": long_evidence,
                    "retirement_condition": "  retire when X happens  ",
                },
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-se01")

    assert d_stat["success_evidence"] == "E" * 500, (
        "success_evidence should be capped at 500"
    )
    assert d_stat["retirement_condition"] == "retire when X happens", (
        "retirement_condition should be stripped and surfaced"
    )


@pytest.mark.asyncio
async def test_recent_challenge_summary_present_in_direction_stats(db_session):
    """Strategic direction stats include bounded recent challenge telemetry."""
    old_summary = {
        "challenged_findings": 99,
        "agreement_distribution": {
            CHALLENGE_AGREES: 99,
            CHALLENGE_DISAGREES: 99,
            CHALLENGE_INDETERMINATE: 99,
        },
        "boundary_distribution": {
            CHALLENGE_BOUNDARY_REPRODUCE_ABSENT: 99,
            CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY: 99,
            CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH: 99,
        },
        "fail_closed_unparseable": 99,
    }
    recent_summary = {
        "challenged_findings": 2,
        "agreement_distribution": {
            CHALLENGE_AGREES: 1,
            CHALLENGE_DISAGREES: 0,
            CHALLENGE_INDETERMINATE: 1,
        },
        "boundary_distribution": {
            CHALLENGE_BOUNDARY_REPRODUCE_ABSENT: 1,
            CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY: 1,
            CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH: 0,
        },
        "fail_closed_unparseable": 1,
    }
    project = _make_project(
        directions=[
            {
                "id": "d-challenge",
                "status": "active",
                "text": "Direction with challenge telemetry",
                "priority": "medium",
                "research_history": [
                    {"finding_challenge_summary": old_summary},
                    {},
                    {"finding_challenge_summary": recent_summary},
                    {},
                    {},
                    {
                        "finding_challenge_summary": {
                            "challenged_findings": 1,
                            "agreement_distribution": {
                                CHALLENGE_AGREES: 0,
                                CHALLENGE_DISAGREES: 1,
                                CHALLENGE_INDETERMINATE: 0,
                            },
                            "boundary_distribution": {
                                CHALLENGE_BOUNDARY_REPRODUCE_ABSENT: 0,
                                CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY: 0,
                                CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH: 1,
                            },
                            "fail_closed_unparseable": 0,
                        }
                    },
                ],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-challenge")
    assert d_stat["recent_findings_challenge_summary"] == {
        "challenged_findings": 3,
        "agreement_distribution": {
            CHALLENGE_AGREES: 1,
            CHALLENGE_DISAGREES: 1,
            CHALLENGE_INDETERMINATE: 1,
        },
        "boundary_distribution": {
            CHALLENGE_BOUNDARY_REPRODUCE_ABSENT: 1,
            CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY: 1,
            CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH: 1,
        },
        "fail_closed_unparseable": 1,
    }


@pytest.mark.asyncio
async def test_success_evidence_absent_when_not_in_context(db_session):
    """success_evidence/retirement_condition are omitted when not set on context."""
    project = _make_project(
        directions=[
            {
                "id": "d-se02",
                "status": "active",
                "text": "Direction without stated outcome",
                "priority": "medium",
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-se02")

    assert "success_evidence" not in d_stat
    assert "retirement_condition" not in d_stat


@pytest.mark.asyncio
async def test_retired_direction_grounding_present_in_direction_stats(db_session):
    """retired_direction_grounding from context reaches the direction stat entry,
    bounded, and survives summarize compaction (metrics + retirement_memo kept)."""
    from app.strategic_payload_compaction import _compact_direction_summary_entry

    grounding = {
        "direction_id": "d-prior99",
        "classification": "abandoned",
        "metrics": {"prs_merged": 3, "cycles": 7},
        "reason": "R" * 700,
        "gap_id": "g-some-gap",
        "retirement_memo": {
            "knowledge_doc_id": "kd-abc",
            "summary": "prior work stalled on flaky infra",
        },
    }
    project = _make_project(
        directions=[
            {
                "id": "d-rg01",
                "status": "active",
                "text": "Continuation of retired work",
                "priority": "medium",
                "context": {"retired_direction_grounding": grounding},
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
            },
            {
                "id": "d-rg02",
                "status": "active",
                "text": "Novel direction with no retired origin",
                "priority": "medium",
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
            },
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-rg01")

    surfaced = d_stat["retired_direction_grounding"]
    assert surfaced["direction_id"] == "d-prior99"
    assert surfaced["classification"] == "abandoned"
    assert surfaced["metrics"] == {"prs_merged": 3, "cycles": 7}
    assert surfaced["gap_id"] == "g-some-gap"
    assert surfaced["retirement_memo"]["knowledge_doc_id"] == "kd-abc"
    # reason bounded to the sibling-field cap (500)
    assert surfaced["reason"] == "R" * 500

    # A direction with no retired origin omits the field entirely.
    d_stat_novel = next(s for s in payload["directions_summary"] if s["id"] == "d-rg02")
    assert "retired_direction_grounding" not in d_stat_novel

    # The field must survive summarize compaction (nested metrics + memo kept).
    compacted = _compact_direction_summary_entry(d_stat)
    assert "retired_direction_grounding" in compacted, (
        "compaction must preserve retired_direction_grounding, not drop it"
    )
    compacted_grounding = compacted["retired_direction_grounding"]
    assert compacted_grounding["classification"] == "abandoned"
    assert compacted_grounding["metrics"] == {"prs_merged": 3, "cycles": 7}
    assert compacted_grounding["retirement_memo"]["knowledge_doc_id"] == "kd-abc"


@pytest.mark.asyncio
async def test_gap_id_present_in_direction_stats(db_session):
    """A direction's gap_id linkage is surfaced in the direction stat entry so
    the strategic agent can reconcile directions against problem-model gaps."""
    project = _make_project(
        directions=[
            {
                "id": "d-gl01",
                "status": "active",
                "text": "Direction grounded to a gap",
                "priority": "medium",
                "gap_id": "g-target",
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-gl01")

    assert d_stat["gap_id"] == "g-target"


@pytest.mark.asyncio
async def test_gap_id_absent_when_not_set(db_session):
    """A direction with no gap_id linkage omits the key from its stat entry."""
    project = _make_project(
        directions=[
            {
                "id": "d-gl02",
                "status": "active",
                "text": "Direction with no gap link",
                "priority": "medium",
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-gl02")

    assert "gap_id" not in d_stat


@pytest.mark.asyncio
async def test_direction_stat_surfaces_proven_and_declaration_coverage(db_session):
    """The per-direction strategic stat surfaces proven_prs_merged (the
    machine-checked subset of verified merges) and acceptance_declaration_coverage
    (declared / merged-write) alongside verified_prs_merged. Proven is STRICTER
    than verified — it must be <= verified for the same window — so a direction
    that shipped many merges but proved few is visibly distinguished. A legacy
    entry that predates the acceptance aggregates degrades proven to 0 and
    coverage to None, exactly like the verified fallback."""
    project = _make_project(
        directions=[
            {
                "id": "d-proven01",
                "status": "active",
                "text": "Direction with a proof gap",
                "priority": "medium",
                # 4 verified merges, but only 2 PROVEN; one of those proven
                # merges is downstream-artifact consumed proof. 3 of 4 merged
                # WRITE tasks declared forward acceptance evidence at plan time.
                "research_history": [
                    {
                        "prs_merged": 4,
                        "prs_rejected": 0,
                        "verified_prs_merged": 4,
                        "acceptance_proven_merged": 2,
                        "acceptance_consumed_merged": 1,
                        "acceptance_declared_merged": 3,
                        "acceptance_merged_write_total": 4,
                    }
                ],
            },
            {
                "id": "d-legacy01",
                "status": "active",
                "text": "Direction with a legacy entry",
                "priority": "medium",
                # Predates the acceptance aggregates — no acceptance_* fields.
                "research_history": [{"prs_merged": 2, "prs_rejected": 0}],
            },
            {
                "id": "d-badge01",
                "status": "active",
                "text": "Direction with badge-only merges",
                "priority": "medium",
                # 3 raw merges, only 1 verified → badge_only = max(0, 3 - 1) = 2.
                "research_history": [
                    {"prs_merged": 3, "prs_rejected": 0, "verified_prs_merged": 1}
                ],
            },
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]

    proven = next(s for s in payload["directions_summary"] if s["id"] == "d-proven01")
    assert proven["verified_prs_merged"] == 4
    assert proven["proven_prs_merged"] == 2
    assert proven["consumed_prs_merged"] == 1
    # Proven is the strict subset of verified for the same window.
    assert proven["proven_prs_merged"] <= proven["verified_prs_merged"]
    assert proven["consumed_prs_merged"] <= proven["proven_prs_merged"]
    assert proven["acceptance_declaration_coverage"] == round(3 / 4, 3)
    # All merges were verified — no badge-only gap.
    assert proven["badge_only_prs_merged"] == 0

    legacy = next(s for s in payload["directions_summary"] if s["id"] == "d-legacy01")
    # Legacy fallback: nothing retroactively claimed proven, no coverage denom.
    assert legacy["proven_prs_merged"] == 0
    assert legacy["consumed_prs_merged"] == 0
    assert legacy["acceptance_declaration_coverage"] is None

    badge = next(s for s in payload["directions_summary"] if s["id"] == "d-badge01")
    # badge_only = max(0, raw_prs_merged - verified_prs_merged) = max(0, 3 - 1) = 2.
    assert badge["badge_only_prs_merged"] == 2


@pytest.mark.asyncio
async def test_cumulative_confirmed_finding_count_and_trend_in_direction_stats(
    db_session,
):
    """confirmed_finding_count and confirmed_finding_trend from outcome_progress_history reach direction stats."""
    project = _make_project(
        directions=[
            {
                "id": "d-cf01",
                "status": "active",
                "text": "Direction with findings trend",
                "priority": "medium",
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
                "outcome_progress_history": [
                    {"progress": 1},
                    {"progress": 2},
                    {"progress": 3},
                    {"progress": 5},
                    {"progress": 8},
                ],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-cf01")

    assert "confirmed_finding_count" in d_stat
    assert d_stat["confirmed_finding_count"] == 8
    assert "confirmed_finding_trend" in d_stat
    assert d_stat["confirmed_finding_trend"] == "improving"


@pytest.mark.asyncio
async def test_outcome_scaffolding_flag_surfaces_in_direction_stats(db_session):
    """outcome_scaffolding_flagged and _evidence appear in stat_entry for flagged direction, absent for unflagged."""
    project = _make_project(
        directions=[
            {
                "id": "d-scaff-flagged",
                "status": "active",
                "text": "Flagged scaffolding direction",
                "priority": "medium",
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
                "outcome_scaffolding_flagged": True,
                "outcome_scaffolding_evidence": {"reason": "no progress", "cycles": 5},
            },
            {
                "id": "d-scaff-clean",
                "status": "active",
                "text": "Clean direction",
                "priority": "medium",
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
            },
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    by_id = {s["id"]: s for s in payload["directions_summary"]}

    flagged = by_id["d-scaff-flagged"]
    assert flagged.get("outcome_scaffolding_flagged") is True
    assert flagged.get("outcome_scaffolding_evidence") == {
        "reason": "no progress",
        "cycles": 5,
    }

    clean = by_id["d-scaff-clean"]
    assert "outcome_scaffolding_flagged" not in clean
    assert "outcome_scaffolding_evidence" not in clean


@pytest.mark.asyncio
async def test_operator_rejection_rate_uses_history_aggregate(db_session):
    """operator_rejection_rate aggregates from both research_history and history_aggregate."""
    project = _make_project(
        directions=[
            {
                "id": "d-agg01",
                "status": "active",
                "text": "Direction with aggregated history",
                "priority": "high",
                "research_history": [
                    {"prs_merged": 1, "prs_rejected": 1},  # 1 + 1
                ],
                "history_aggregate": {
                    "total_prs_merged": 4,
                    "total_prs_rejected": 2,  # 4 + 2
                    "total_cycles": 5,
                    "total_tasks_failed": 0,
                },
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-agg01")

    # history: 1 merged + 1 rejected; aggregate: 4 merged + 2 rejected
    # totals: 5 merged + 3 rejected = 8 PRs → rate = 3/8 = 0.375 → 0.38
    assert d_stat.get("prs_rejected") == 3
    assert d_stat.get("operator_rejection_rate") == round(3 / 8, 2)


@pytest.mark.asyncio
async def test_operator_rejection_rate_zero_rejected_in_history_aggregate(db_session):
    """operator_rejection_rate absent when total_rejected sums to zero across both sources."""
    project = _make_project(
        directions=[
            {
                "id": "d-agg02",
                "status": "active",
                "text": "Clean direction",
                "priority": "low",
                "research_history": [{"prs_merged": 2, "prs_rejected": 0}],
                "history_aggregate": {
                    "total_prs_merged": 3,
                    "total_prs_rejected": 0,
                    "total_cycles": 3,
                },
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-agg02")

    assert "operator_rejection_rate" not in d_stat
    assert "prs_rejected" not in d_stat


@pytest.mark.asyncio
async def test_retirement_eligibility_fields_in_direction_stats(db_session):
    """lifetime_autonomous_prs_merged, lifetime_operator_actions, and retirement_eligible are set on each direction stat."""
    project = _make_project(
        directions=[
            {
                "id": "d-ret-eligible",
                "status": "active",
                "text": "Eligible direction",
                "priority": "high",
                # 2 explicit autonomous + 2 legacy fallback = 4 from history; 7 from aggregate → total 11
                "research_history": [
                    {
                        "autonomous_prs_merged": 2,
                        "prs_merged": 2,
                        "operator_action_count": 0,
                    },
                    {
                        "prs_merged": 2
                    },  # legacy: no autonomous_prs_merged, falls back to prs_merged
                ],
                "history_aggregate": {
                    "total_autonomous_prs_merged": 7,
                    "total_operator_actions": 0,
                },
            },
            {
                "id": "d-ret-ineligible-ops",
                "status": "active",
                "text": "Ineligible: has operator actions",
                "priority": "medium",
                "research_history": [
                    {
                        "autonomous_prs_merged": 15,
                        "prs_merged": 15,
                        "operator_action_count": 1,
                    },
                ],
                "history_aggregate": {
                    "total_autonomous_prs_merged": 0,
                    "total_operator_actions": 0,
                },
            },
            {
                "id": "d-ret-ineligible-count",
                "status": "active",
                "text": "Ineligible: not enough autonomous merges",
                "priority": "low",
                "research_history": [
                    {
                        "autonomous_prs_merged": 5,
                        "prs_merged": 5,
                        "operator_action_count": 0,
                    },
                ],
                "history_aggregate": {
                    "total_autonomous_prs_merged": 4,
                    "total_operator_actions": 0,
                },
            },
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    by_id = {s["id"]: s for s in payload["directions_summary"]}

    eligible = by_id["d-ret-eligible"]
    assert eligible["lifetime_autonomous_prs_merged"] == 11  # 2 + 2 (fallback) + 7
    assert eligible["lifetime_operator_actions"] == 0
    assert eligible["retirement_eligible"] is True

    ineligible_ops = by_id["d-ret-ineligible-ops"]
    assert ineligible_ops["lifetime_operator_actions"] == 1
    assert ineligible_ops["retirement_eligible"] is False

    ineligible_count = by_id["d-ret-ineligible-count"]
    assert ineligible_count["lifetime_autonomous_prs_merged"] == 9  # 5 + 4
    assert ineligible_count["retirement_eligible"] is False


@pytest.mark.asyncio
async def test_stat_payload_autonomy_agrees_with_helper_and_excludes_degraded(
    db_session,
):
    """The strategist stat payload derives autonomy from the single source of
    truth (compute_direction_autonomy_state) — the two surfaces cannot disagree,
    and degraded/unverified autonomous merges do NOT advance retirement.

    The direction has 10 RAW autonomous merges but only 8 verified, so the raw
    milestone would (wrongly) fire while the verified milestone does not.
    """
    from app.direction_outcomes import compute_direction_autonomy_state

    direction = {
        "id": "d-verified-autonomy-payload",
        "status": "active",
        "text": "Direction with degraded autonomous merges",
        "priority": "high",
        "research_history": [
            {
                "autonomous_prs_merged": 10,
                "verified_autonomous_prs_merged": 8,
                "prs_merged": 10,
                "operator_action_count": 0,
            },
        ],
        "history_aggregate": {
            "total_autonomous_prs_merged": 0,
            "total_verified_autonomous_prs_merged": 0,
            "total_operator_actions": 0,
        },
    }
    project = _make_project(directions=[direction], vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    stat = {s["id"]: s for s in payload["directions_summary"]}[
        "d-verified-autonomy-payload"
    ]

    # The payload agrees byte-for-byte with the helper on the same input.
    expected = compute_direction_autonomy_state(direction)
    assert (
        stat["lifetime_autonomous_prs_merged"]
        == expected["lifetime_autonomous_prs_merged"]
        == 8
    )
    assert stat["lifetime_operator_actions"] == expected["lifetime_operator_actions"]
    # 8 verified (not the raw 10) → NOT retirement-eligible.
    assert stat["retirement_eligible"] == expected["retirement_eligible"] is False


@pytest.mark.asyncio
async def test_cost_fields_present_when_history_has_merge_data(db_session):
    """cost_per_merged_pr and estimated_cost_usd appear when history has tasks_created + avg_time_to_merge_hours + merges."""
    from app.routers.admin_analytics import ON_DEMAND_RATE

    project = _make_project(
        directions=[
            {
                "id": "d-cost01",
                "status": "active",
                "text": "Direction with cost data",
                "priority": "medium",
                "research_history": [
                    {
                        "prs_merged": 2,
                        "tasks_created": 4,
                        "avg_time_to_merge_hours": 1.5,
                    },
                    {
                        "prs_merged": 1,
                        "tasks_created": 2,
                        "avg_time_to_merge_hours": 2.0,
                    },
                ],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-cost01")

    expected_cost = round((4 * 1.5 + 2 * 2.0) * 3600 * ON_DEMAND_RATE, 2)
    assert d_stat["estimated_cost_usd"] == expected_cost
    assert d_stat["cost_per_merged_pr"] == round(expected_cost / 3, 4)


@pytest.mark.asyncio
async def test_cost_fields_absent_without_history_data(db_session):
    """Cost fields are omitted when history lacks tasks_created or avg_time_to_merge_hours."""
    project = _make_project(
        directions=[
            {
                "id": "d-cost02",
                "status": "active",
                "text": "Direction with no cost data",
                "priority": "low",
                "research_history": [{"prs_merged": 2}],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-cost02")

    assert "estimated_cost_usd" not in d_stat
    assert "cost_per_merged_pr" not in d_stat


@pytest.mark.asyncio
async def test_cost_fields_use_recorded_spend_over_proxy(db_session):
    """When estimated_cost_usd is present in a history entry, it is used instead of the proxy."""
    project = _make_project(
        directions=[
            {
                "id": "d-cost-recorded",
                "status": "active",
                "text": "Direction with recorded cost",
                "priority": "medium",
                "research_history": [
                    {
                        "prs_merged": 2,
                        "tasks_created": 999,
                        "avg_time_to_merge_hours": 999.0,
                        "estimated_cost_usd": 6.50,
                    },
                ],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(
        s for s in payload["directions_summary"] if s["id"] == "d-cost-recorded"
    )

    assert d_stat["estimated_cost_usd"] == 6.50
    assert d_stat["cost_per_merged_pr"] == round(6.50 / 2, 2)


@pytest.mark.asyncio
async def test_cost_per_merged_pr_folds_evicted_aggregate_cost(db_session):
    """cost_per_merged_pr folds the evicted-aggregate cost into the numerator.

    A direction older than the 10-cycle window has cost-bearing cycles that
    aged out into history_aggregate. The merge DENOMINATOR already folds in
    agg.total_prs_merged; the cost NUMERATOR must fold in
    agg.total_estimated_cost_usd too, otherwise cost-per-merge is understated.
    This is the parity contract: strategic_spawning and strategic_analysis must
    report an EQUAL cost_per_merged_pr for the same direction data.
    """
    from app.routers.admin_analytics import ON_DEMAND_RATE
    from app.strategic_analysis import _entry_cost_usd
    from app.direction_metrics import canonical_cost_per_merged_pr

    history = [
        {"prs_merged": 2, "tasks_created": 4, "avg_time_to_merge_hours": 1.5},
        {"prs_merged": 1, "tasks_created": 2, "avg_time_to_merge_hours": 2.0},
    ]
    history_aggregate = {
        "total_cycles": 8,
        "total_prs_merged": 5,
        "total_estimated_cost_usd": 12.34,
    }
    project = _make_project(
        directions=[
            {
                "id": "d-cost-evicted",
                "status": "active",
                "text": "Direction with evicted cost + merges",
                "priority": "medium",
                "research_history": history,
                "history_aggregate": history_aggregate,
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(
        s for s in payload["directions_summary"] if s["id"] == "d-cost-evicted"
    )

    # Lifetime numerator = window cost + evicted-aggregate cost.
    window_cost = sum(_entry_cost_usd(h, ON_DEMAND_RATE) for h in history)
    expected_cost = round(window_cost + 12.34, 2)
    # Lifetime denominator = window merges + evicted-aggregate merges.
    total_merged = (2 + 1) + 5
    expected_cpm = canonical_cost_per_merged_pr(expected_cost, total_merged)

    assert d_stat["estimated_cost_usd"] == expected_cost
    assert d_stat["cost_per_merged_pr"] == expected_cpm
    # Parity contract: dropping the folded aggregate cost would understate it.
    window_only_cpm = canonical_cost_per_merged_pr(
        round(window_cost, 2), total_merged
    )
    assert d_stat["cost_per_merged_pr"] > window_only_cpm


@pytest.mark.asyncio
async def test_tasks_failed_infra_includes_history_aggregate(db_session):
    """tasks_failed_infra aggregates from both research_history and history_aggregate."""
    project = _make_project(
        directions=[
            {
                "id": "d-agg03",
                "status": "active",
                "text": "Direction with archived infra failures",
                "priority": "high",
                "research_history": [
                    {"tasks_failed": 1, "tasks_failed_infra": 1},
                ],
                "history_aggregate": {
                    "total_cycles": 5,
                    "total_prs_merged": 0,
                    "total_prs_rejected": 0,
                    "total_tasks_failed": 4,
                    "total_tasks_failed_infra": 3,
                },
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-agg03")

    # history: 1 failed (1 infra); aggregate: 4 failed (3 infra)
    assert d_stat["tasks_failed"] == 5
    assert d_stat.get("tasks_failed_infra") == 4


# ── Active-task guard in _spawn_design_doc_update / _spawn_strategic_review ───


@pytest.mark.asyncio
async def test_spawn_design_doc_update_skips_when_strategic_task_active(
    db_session, mocker
):
    """_spawn_design_doc_update returns False when a strategic task is already active."""
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=True),
    )
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is False


@pytest.mark.asyncio
async def test_spawn_strategic_review_skips_when_strategic_task_active(
    db_session, mocker
):
    """_spawn_strategic_review returns False when a strategic task is already active."""
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=True),
    )
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is False


@pytest.mark.asyncio
async def test_spawn_design_doc_update_respects_initiative_budget(
    db_session, mocker
):
    """Design-doc spawns stop when the initiative budget is exhausted in enforce mode."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )

    project = _make_project(
        vision={"purpose": "test purpose"},
        build_config={"bootstrap_completed_at": "2026-01-01T00:00:00"},
    )
    budget_source = _make_project(vision={"purpose": "budget source"})
    db_session.add_all([project, budget_source])
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(budget_source)

    db_session.add(
        Task(
            project_id=budget_source.id,
            title="Research budget source",
            payloads=[{"type": "research"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
    )
    await db_session.commit()

    original_mode = _shared_state.runtime_config["autonomous_spawn_budget_mode"]
    original_initiative = _shared_state.runtime_config[
        "max_global_autonomous_initiative_tasks_per_hour"
    ]
    original_global = _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"]
    _shared_state.runtime_config["autonomous_spawn_budget_mode"] = "enforce"
    _shared_state.runtime_config["max_global_autonomous_initiative_tasks_per_hour"] = 1
    _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"] = 500
    try:
        spawned = await _spawn_design_doc_update(
            project,
            db_session,
            apply_spawn_budget_guard=True,
        )

        assert spawned is False
        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        assert result.scalars().all() == []
    finally:
        _shared_state.runtime_config["autonomous_spawn_budget_mode"] = original_mode
        _shared_state.runtime_config[
            "max_global_autonomous_initiative_tasks_per_hour"
        ] = original_initiative
        _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"] = original_global


@pytest.mark.asyncio
async def test_spawn_strategic_review_respects_global_backstop(db_session, mocker):
    """Strategic review spawns stop when the global autonomous backstop is exhausted."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )

    project = _make_project(
        directions=[
            {
                "id": "d-sr-budget",
                "status": "active",
                "text": "Budget-sensitive direction",
                "priority": "medium",
            }
        ],
        vision={"purpose": "test purpose"},
    )
    budget_source = _make_project(vision={"purpose": "budget source"})
    db_session.add_all([project, budget_source])
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(budget_source)
    await _seed_legacy_cycles(db_session, project)

    db_session.add(
        Task(
            project_id=budget_source.id,
            title="Research budget source",
            payloads=[{"type": "research"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
    )
    await db_session.commit()

    original_mode = _shared_state.runtime_config["autonomous_spawn_budget_mode"]
    original_initiative = _shared_state.runtime_config[
        "max_global_autonomous_initiative_tasks_per_hour"
    ]
    original_global = _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"]
    _shared_state.runtime_config["autonomous_spawn_budget_mode"] = "observe"
    _shared_state.runtime_config["max_global_autonomous_initiative_tasks_per_hour"] = 500
    _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"] = 1
    try:
        spawned = await _spawn_strategic_review(
            project,
            db_session,
            apply_spawn_budget_guard=True,
        )

        assert spawned is False
        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        assert result.scalars().all() == []
    finally:
        _shared_state.runtime_config["autonomous_spawn_budget_mode"] = original_mode
        _shared_state.runtime_config[
            "max_global_autonomous_initiative_tasks_per_hour"
        ] = original_initiative
        _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"] = original_global


@pytest.mark.asyncio
async def test_strategic_spawn_budget_guard_fails_closed_in_enforce_mode(
    db_session, mocker
):
    """A snapshot-computation failure must BLOCK the spawn under enforce mode.

    Fail-open on a persistent snapshot/DB error would silently disable an
    operator-enabled budget — the exact opposite of enforcement.
    """
    from app.strategic_spawning import _strategic_spawn_budget_guard

    mocker.patch(
        "app.strategic_spawning.compute_autonomous_spawn_budget_snapshot",
        new=AsyncMock(side_effect=RuntimeError("snapshot db down")),
    )

    original_mode = _shared_state.runtime_config["autonomous_spawn_budget_mode"]
    _shared_state.runtime_config["autonomous_spawn_budget_mode"] = "enforce"
    try:
        guard = await _strategic_spawn_budget_guard(db_session, "initiative")
    finally:
        _shared_state.runtime_config["autonomous_spawn_budget_mode"] = original_mode

    assert guard is not None
    assert guard["guard"] == "budget_snapshot_unavailable"
    assert guard["mode"] == "enforce"
    assert guard["category"] == "initiative"


@pytest.mark.asyncio
async def test_strategic_spawn_budget_guard_fails_open_in_observe_mode(
    db_session, mocker
):
    """In observe mode the guard is advisory, so a snapshot failure preserves
    fail-open behavior (returns None => spawn proceeds)."""
    from app.strategic_spawning import _strategic_spawn_budget_guard

    mocker.patch(
        "app.strategic_spawning.compute_autonomous_spawn_budget_snapshot",
        new=AsyncMock(side_effect=RuntimeError("snapshot db down")),
    )

    original_mode = _shared_state.runtime_config["autonomous_spawn_budget_mode"]
    _shared_state.runtime_config["autonomous_spawn_budget_mode"] = "observe"
    try:
        guard = await _strategic_spawn_budget_guard(db_session, "initiative")
    finally:
        _shared_state.runtime_config["autonomous_spawn_budget_mode"] = original_mode

    assert guard is None


@pytest.mark.asyncio
async def test_default_autonomous_spawn_ceiling_parity_with_main(db_session, mocker):
    """Default global autonomous spawn cap restores parity with main (50/hr).

    Verify that the default max_global_autonomous_tasks_per_hour is 50, not 500,
    so that default deployments respect the same ceiling as main without requiring
    enforce mode. This guards against the 10x relaxation regression (872eacdf).
    """
    from app.autonomous_spawn_budget import (
        DEFAULT_AUTONOMOUS_BACKSTOP_TASKS_PER_HOUR,
        autonomous_spawn_budget_config_from_runtime,
    )

    # Verify the constant is correct.
    assert DEFAULT_AUTONOMOUS_BACKSTOP_TASKS_PER_HOUR == 50, (
        f"DEFAULT_AUTONOMOUS_BACKSTOP_TASKS_PER_HOUR should be 50 for main parity, "
        f"got {DEFAULT_AUTONOMOUS_BACKSTOP_TASKS_PER_HOUR}"
    )

    # Verify runtime_config default matches.
    config = autonomous_spawn_budget_config_from_runtime(_shared_state.runtime_config)
    assert config.global_cap == 50, (
        f"Default global_cap should be 50 for main parity, got {config.global_cap}"
    )

    # Verify strategic review respects the default global ceiling in observe mode.
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )

    project = _make_project(
        directions=[
            {
                "id": "d-cap-test",
                "status": "active",
                "text": "Cap test direction",
                "priority": "medium",
            }
        ],
        vision={"purpose": "test purpose"},
    )
    budget_source = _make_project(vision={"purpose": "budget source"})
    db_session.add_all([project, budget_source])
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(budget_source)
    await _seed_legacy_cycles(db_session, project)

    # Fill the global cap with ORCHESTRATOR tasks.
    for i in range(50):
        db_session.add(
            Task(
                project_id=budget_source.id,
                title=f"Budget task {i}",
                payloads=[{"type": "research"}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            )
        )
    await db_session.commit()

    # Attempt spawn with global cap exhausted (using default observe mode).
    spawned = await _spawn_strategic_review(
        project,
        db_session,
        apply_spawn_budget_guard=True,
    )

    # Spawn should be blocked by the global cap in observe mode.
    assert spawned is False, "Spawn should be blocked when global cap (50) is exhausted"
    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_spawn_design_doc_update_proceeds_when_no_active_strategic(db_session):
    """_spawn_design_doc_update returns True and creates a task when no strategic task is active."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    assert tasks[0].payloads[0]["type"] == "design_doc_update"
    assert tasks[0].status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_spawn_strategic_review_proceeds_when_no_active_strategic(db_session):
    """_spawn_strategic_review returns True and creates a task when no strategic task is active."""
    project = _make_project(
        directions=[
            {
                "id": "d-sr01",
                "status": "active",
                "text": "Some direction",
                "priority": "medium",
            },
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    assert tasks[0].payloads[0]["type"] == "strategic_review"
    assert tasks[0].status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_spawn_strategic_review_aborts_when_strategic_task_appears_mid_build(
    db_session, mocker
):
    """The pre-INSERT recheck catches a peer's spawn that landed mid-build.

    ``_build_recent_interventions`` commits mid-build (#8842), releasing the
    advisory lock and the project-row FOR UPDATE taken by the top-of-function
    guard — so a peer instance can commit its own strategic spawn during the
    ~30s payload build (2026-06-11 double-spawn). The guard is therefore run
    a second time immediately before the INSERT: first call (top) sees no
    active task, second call (recheck) sees the peer's task → no row inserted.
    """
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(side_effect=[False, True]),
    )
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)
    # Snapshot the id: the guarded-bail path rolls the session back, which
    # expires every ORM instance — a post-bail project.id read would emit a
    # sync lazy-load and raise MissingGreenlet under the asyncpg harness.
    project_id = project.id

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is False
    result = await db_session.execute(select(Task).where(Task.project_id == project_id))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_spawn_strategic_review_aborts_when_peer_holds_spawn_lock_at_insert(
    db_session, mocker
):
    """The pre-INSERT advisory-lock re-acquire bails when a peer holds the lock."""
    mocker.patch(
        "app.autonomous_research._try_research_spawn_advisory_lock",
        new=AsyncMock(return_value=False),
    )
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)
    # Snapshot the id: the advisory-lock bail rolls the session back, which
    # expires every ORM instance (see the mid-build test above).
    project_id = project.id

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is False
    result = await db_session.execute(select(Task).where(Task.project_id == project_id))
    assert result.scalars().all() == []


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason=(
        "MissingGreenlet on expired ORM attribute is asyncpg-specific; aiosqlite "
        "lazy-loads succeed in the async greenlet, masking the regression. "
        "Fault injection is not practical: _finalize_strategic_review_spawn "
        "receives project_id (a plain UUID), not the project ORM object, so "
        "there is no proxy intercept point inside the bail path after rollback."
    ),
)
@pytest.mark.asyncio
async def test_spawn_strategic_review_bail_survives_rollback_expiry(db_session, mocker):
    """Bail paths do not raise MissingGreenlet when the session rollback expires ORM instances.

    SQLAlchemy expires every instance in the identity map on rollback (expire_on_commit=False
    does not apply to rollback). Accessing project.id after rollback used to trigger a
    synchronous lazy-load that raises MissingGreenlet in async context (BUG_REPORT e530efec).
    The fix captures project scalars at function entry (while the instance is live — the
    documented precondition: callers pass a fresh-read project) so nothing after the
    finalize bail's rollback touches the expirable instance.

    The advisory-lock refusal below makes ``_finalize_strategic_review_spawn`` execute a
    REAL ``session.rollback()`` mid-flow — under the Postgres harness that genuinely
    expires every ORM instance, so any post-rollback ``project.<attr>`` read in the bail
    tail would propagate MissingGreenlet and fail this test. (An entry-time
    ``db_session.expire(project)`` would instead violate the fresh-read precondition and
    break the top-of-function guard, which legitimately reads the live instance.)
    """
    mocker.patch(
        "app.autonomous_research._try_research_spawn_advisory_lock",
        new=AsyncMock(return_value=False),
    )
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    # Must return False without raising: the finalize bail rolls back (expiring
    # all instances) and everything after it must use the entry-time snapshots.
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is False


# ── Bootstrap-pending guard in _spawn_design_doc_update ───────────────────────


@pytest.mark.asyncio
async def test_spawn_design_doc_update_defers_while_bootstrap_pending(db_session):
    """_spawn_design_doc_update returns False while a repo_bootstrap task is in-flight."""
    project = _make_project(
        vision={"purpose": "test purpose"},
        build_config={},  # bootstrap_completed_at not set
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create a pending repo_bootstrap task for this project
    bootstrap_task = Task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "repo_bootstrap"}],
    )
    db_session.add(bootstrap_task)
    await db_session.commit()

    spawned = await _spawn_design_doc_update(project, db_session)

    assert spawned is False
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.payloads[0]["type"].as_string() == "design_doc_update",
        )
    )
    assert result.scalars().first() is None


@pytest.mark.asyncio
async def test_spawn_strategic_review_defers_while_bootstrap_pending(db_session):
    """_spawn_strategic_review returns False while a repo_bootstrap task is in-flight."""
    project = _make_project(
        vision={"purpose": "test purpose"},
        build_config={},  # bootstrap_completed_at not set
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create a pending repo_bootstrap task for this project
    bootstrap_task = Task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "repo_bootstrap"}],
    )
    db_session.add(bootstrap_task)
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is False
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.payloads[0]["type"].as_string() == "strategic_review",
        )
    )
    assert result.scalars().first() is None


@pytest.mark.asyncio
async def test_spawn_design_doc_update_proceeds_when_bootstrap_completed(db_session):
    """_spawn_design_doc_update proceeds normally when bootstrap_completed_at is set."""
    project = _make_project(
        vision={"purpose": "test purpose"},
        build_config={"bootstrap_completed_at": "2026-01-01T00:00:00"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    assert tasks[0].payloads[0]["type"] == "design_doc_update"
    assert tasks[0].status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_spawn_strategic_review_enable_self_improvement_decoupled_from_is_self_repo(
    db_session,
):
    """Strategic payload ships repo identity separately from improvement mode.

    is_self_repo is the actual project identity. enable_self_improvement is
    driven by build_config and may be true for an external project.
    """
    # Self-repo without explicit flag → False
    project_self_no_flag = _make_project(
        is_self_repo=True,
        build_config={"some_other_key": "value"},
        vision={"purpose": "test purpose"},
    )
    db_session.add(project_self_no_flag)
    await db_session.commit()
    await db_session.refresh(project_self_no_flag)
    await _seed_legacy_cycles(db_session, project_self_no_flag)

    spawned = await _spawn_strategic_review(project_self_no_flag, db_session)
    assert spawned is True
    result = await db_session.execute(
        _select_agent_tasks(project_self_no_flag.id)
    )
    task_self = result.scalars().one()
    assert task_self.payloads[0]["is_self_repo"] is True
    assert task_self.payloads[0]["enable_self_improvement"] is False

    # Non-self-repo with explicit flag → True
    project_flag = _make_project(
        is_self_repo=False,
        build_config={"enable_self_improvement": True},
        vision={"purpose": "test purpose"},
    )
    db_session.add(project_flag)
    await db_session.commit()
    await db_session.refresh(project_flag)
    await _seed_legacy_cycles(db_session, project_flag)

    spawned2 = await _spawn_strategic_review(project_flag, db_session)
    assert spawned2 is True
    result2 = await db_session.execute(
        _select_agent_tasks(project_flag.id)
    )
    task_flag = result2.scalars().one()
    assert task_flag.payloads[0]["is_self_repo"] is False
    assert task_flag.payloads[0]["enable_self_improvement"] is True


@pytest.mark.asyncio
async def test_spawn_strategic_review_is_self_repo_follows_deploy_identity(
    db_session, monkeypatch
):
    """The agent-facing ``is_self_repo`` payload field keys off the deploy-repo
    predicate, not the raw ``project.is_self_repo`` column.

    This is the self-improvement-framing consumer: the strategic agent gates the
    "modify your own prompts/schemas/orchestration" block on this field, so it
    must classify by deploy identity like every other migrated backend gate.
    When a deploy identity is stamped, the field follows repo identity even when
    the raw column disagrees.
    """
    monkeypatch.setenv("DEPLOY_REPO_IDENTITY", "acme/widget")

    # Repo matches the deploy identity but the raw column is False → payload True
    # (deploy identity is authoritative, overriding the mis-set column).
    project_match = _make_project(
        github_owner="acme",
        github_repo="widget",
        is_self_repo=False,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project_match)
    await db_session.commit()
    await db_session.refresh(project_match)
    await _seed_legacy_cycles(db_session, project_match)

    spawned = await _spawn_strategic_review(project_match, db_session)
    assert spawned is True
    task_match = (
        (
            await db_session.execute(
                _select_agent_tasks(project_match.id)
            )
        )
        .scalars()
        .one()
    )
    assert task_match.payloads[0]["is_self_repo"] is True

    # Repo does NOT match the deploy identity but the raw column is True → payload
    # False (a stale is_self_repo flag no longer grants self-improvement framing).
    project_mismatch = _make_project(
        github_owner="other",
        github_repo="proj",
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project_mismatch)
    await db_session.commit()
    await db_session.refresh(project_mismatch)
    await _seed_legacy_cycles(db_session, project_mismatch)

    spawned2 = await _spawn_strategic_review(project_mismatch, db_session)
    assert spawned2 is True
    task_mismatch = (
        (
            await db_session.execute(
                _select_agent_tasks(project_mismatch.id)
            )
        )
        .scalars()
        .one()
    )
    assert task_mismatch.payloads[0]["is_self_repo"] is False


# ── Additional _spawn_design_doc_update tests ─────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_design_doc_update_adds_cycle_history_entry(db_session, mocker):
    """_spawn_design_doc_update writes a new StrategicCycle row.

    Audit W2: cycle history is the StrategicCycle table now (was JSONB).
    """
    from app import strategic_cycle
    from app.models import StrategicCycle as _SC
    from sqlmodel import select as _select

    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_design_doc_update(project, db_session)

    cycles = (
        (await db_session.execute(_select(_SC).where(_SC.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(cycles) == 1
    cycle = cycles[0]
    assert cycle.outcome == strategic_cycle.OUTCOME_PHASE1_RUNNING
    assert cycle.phase1_task_id is not None
    assert cycle.id.startswith("sc-")


@pytest.mark.asyncio
async def test_spawn_design_doc_update_cycle_history_unbounded_in_table(
    db_session, mocker
):
    """Audit W2: the 10-entry cap on cycle history was a JSONB-bloat guard.
    With cycles in a real table the cap is gone; ``recent_for_agent_context``
    applies a per-call limit at read time instead."""
    from app import strategic_cycle
    from app.models import StrategicCycle as _SC
    from sqlmodel import select as _select
    from app.time_utils import _utcnow

    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # Pre-populate with 10 historical cycles
    from datetime import timedelta

    for i in range(10):
        db_session.add(
            _SC(
                id=f"sc-{i:08x}",
                project_id=project.id,
                outcome="completed",
                phase1_started_at=_utcnow() - timedelta(days=i + 1),
            )
        )
    await db_session.commit()

    await _spawn_design_doc_update(project, db_session)

    all_cycles = (
        (await db_session.execute(_select(_SC).where(_SC.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(all_cycles) == 11
    # The agent-context view caps at the helper's default (10).
    recent = await strategic_cycle.recent_for_agent_context(db_session, project.id)
    assert len(recent) == 10
    # Newest at the front; the just-spawned cycle should be first.
    assert recent[0]["outcome"] == strategic_cycle.OUTCOME_PHASE1_RUNNING


# ── Additional _spawn_strategic_review tests ──────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_strategic_review_records_phase2_task_id(db_session, mocker):
    """When called with cycle_id, _spawn_strategic_review records phase2_task_id on the cycle row.

    Audit W2: cycle history is the StrategicCycle table now.
    """
    from app import strategic_cycle
    from app.models import StrategicCycle as _SC
    from app.time_utils import _utcnow

    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    cycle_id = "sc-abc12345"
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        _SC(
            id=cycle_id,
            project_id=project.id,
            outcome=strategic_cycle.OUTCOME_PHASE1_RUNNING,
            phase1_started_at=_utcnow(),
        )
    )
    await db_session.commit()

    await _spawn_strategic_review(project, db_session, cycle_id=cycle_id)

    cycle = await strategic_cycle.get(db_session, cycle_id)
    assert cycle is not None
    assert cycle.phase2_task_id is not None
    assert cycle.phase1_completed_at is not None
    assert cycle.outcome == strategic_cycle.OUTCOME_PHASE2_RUNNING


# ── recommended_mode in direction stats ──────────────────────────────────────


@pytest.mark.asyncio
async def test_recommended_mode_audit_present_in_direction_stats(db_session):
    """recommended_mode='audit' is surfaced in direction_stats payload."""
    project = _make_project(
        directions=[
            {
                "id": "d-mode01",
                "status": "active",
                "text": "Direction in audit mode",
                "priority": "high",
                "recommended_mode": "audit",
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-mode01")

    assert d_stat.get("recommended_mode") == "audit", (
        "Expected recommended_mode='audit' in direction stats for audit-mode direction"
    )


@pytest.mark.asyncio
async def test_recommended_mode_fundamentals_present_in_direction_stats(db_session):
    """recommended_mode='fundamentals' is surfaced in direction_stats payload."""
    project = _make_project(
        directions=[
            {
                "id": "d-mode02",
                "status": "active",
                "text": "Direction in fundamentals mode",
                "priority": "medium",
                "recommended_mode": "fundamentals",
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-mode02")

    assert d_stat.get("recommended_mode") == "fundamentals", (
        "Expected recommended_mode='fundamentals' in direction stats for fundamentals-mode direction"
    )


@pytest.mark.asyncio
async def test_recommended_mode_ship_absent_from_direction_stats(db_session):
    """recommended_mode='ship' and unset are both omitted — they represent default behavior."""
    project = _make_project(
        directions=[
            {
                "id": "d-mode03",
                "status": "active",
                "text": "Direction explicitly in ship mode",
                "priority": "low",
                "recommended_mode": "ship",
            },
            {
                "id": "d-mode04",
                "status": "active",
                "text": "Direction with no mode set",
                "priority": "low",
            },
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat_ship = next(
        s for s in payload["directions_summary"] if s["id"] == "d-mode03"
    )
    d_stat_unset = next(
        s for s in payload["directions_summary"] if s["id"] == "d-mode04"
    )

    assert "recommended_mode" not in d_stat_ship, (
        "recommended_mode='ship' should be omitted (default behavior)"
    )
    assert "recommended_mode" not in d_stat_unset, (
        "Unset recommended_mode should be omitted (default behavior)"
    )


@pytest.mark.asyncio
async def test_recommended_mode_pause_present_in_direction_stats(db_session):
    """recommended_mode='pause' is surfaced so the strategic agent can flag the direction."""
    project = _make_project(
        directions=[
            {
                "id": "d-mode05",
                "status": "active",
                "text": "Direction recommended for pause",
                "priority": "low",
                "recommended_mode": "pause",
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-mode05")

    assert d_stat.get("recommended_mode") == "pause", (
        "Expected recommended_mode='pause' in direction stats for pause-mode direction"
    )


@pytest.mark.asyncio
async def test_auto_retire_pending_surfaces_in_direction_stats(db_session, mocker):
    """Paused lifetime-burn directions keep their retire-pending metadata in the strategist payload."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    project = _make_project(
        directions=[
            {
                "id": "d-retire-pending",
                "status": "paused",
                "text": "Direction pending lifetime-burn retirement",
                "priority": "medium",
                "auto_paused": True,
                "paused_reason": "Lifetime burn backstop: cumulative non-merge ratio above threshold",
                "auto_retire_pending": True,
                "auto_retire_pending_reason": "lifetime_burn",
                "auto_retire_pending_at": (now - timedelta(hours=1)).isoformat(),
                "lifetime_burn_pause_count": 2,
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]
    d_stat = next(
        s for s in payload["directions_summary"] if s["id"] == "d-retire-pending"
    )

    assert d_stat.get("auto_retire_pending") is True
    assert d_stat.get("auto_retire_pending_reason") == "lifetime_burn"
    assert d_stat.get("auto_retire_pending_at") == (now - timedelta(hours=1)).isoformat()
    assert d_stat.get("lifetime_burn_pause_count") == 2


@pytest.mark.asyncio
async def test_auto_retire_pending_surfaces_for_active_scaffolding_direction(
    db_session, mocker
):
    """Active scaffolding-flagged directions surface retire-pending metadata in the strategist payload."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    project = _make_project(
        directions=[
            {
                "id": "d-scaffolding-retire",
                "status": "active",
                "text": "Direction pending scaffolding retirement",
                "priority": "medium",
                "auto_retire_pending": True,
                "auto_retire_pending_reason": "scaffolding",
                "auto_retire_pending_at": (now - timedelta(minutes=5)).isoformat(),
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]
    d_stat = next(
        s for s in payload["directions_summary"] if s["id"] == "d-scaffolding-retire"
    )

    assert d_stat.get("auto_retire_pending") is True
    assert d_stat.get("auto_retire_pending_reason") == "scaffolding"
    assert d_stat.get("auto_retire_pending_at") == (
        now - timedelta(minutes=5)
    ).isoformat()
    assert d_stat.get("status") == "active"


@pytest.mark.asyncio
async def test_spawn_gate_metrics_present_in_direction_stats(db_session):
    """spawn_gate_metrics appears in direction stats when direction has spawn-gate activity."""
    project = _make_project(
        directions=[
            {
                "id": "d-sg01",
                "status": "active",
                "text": "Direction with spawn-gate history",
                "priority": "medium",
                "research_history": [
                    {
                        "prs_merged": 2,
                        "research_spawn_gate_challenged_count": 5,
                        "research_spawn_gate_refuted_count": 2,
                        "research_spawn_gate_survived_count": 3,
                        "research_spawn_gate_manifest_excluded_fail_closed_count": 1,
                        "research_spawn_gate_manifest_excluded_fail_closed_samples": [
                            {
                                "hash": "excluded-hash",
                                "title": "Budget-excluded finding",
                                "reason": "manifest cap",
                            }
                        ],
                        "research_spawn_gate_challenged_spawned": 3,
                        "research_spawn_gate_challenged_merged": 2,
                        "research_spawn_gate_unchallenged_spawned": 4,
                        "research_spawn_gate_unchallenged_merged": 3,
                        "research_spawn_gate_wholesale_retry_exhausted": True,
                    }
                ],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-sg01")

    assert "spawn_gate_metrics" in d_stat, (
        "spawn_gate_metrics should be present for direction with spawn-gate activity"
    )
    sgm = d_stat["spawn_gate_metrics"]
    assert sgm["challenged"] == 5
    assert sgm["refuted"] == 2
    assert sgm["survived"] == 3
    assert sgm["manifest_excluded_fail_closed_count"] == 1
    assert sgm["manifest_excluded_fail_closed_samples"] == [
        {
            "hash": "excluded-hash",
            "title": "Budget-excluded finding",
            "reason": "manifest cap",
        }
    ]
    assert sgm["exhaustion_count"] == 1
    assert sgm["challenged_merge_rate"] == round(2 / 3, 3)
    assert sgm["unchallenged_merge_rate"] == round(3 / 4, 3)
    suffix = payload.get("prompt_suffix", "")
    assert "Spawn-gate grader notes for this cycle:" in suffix
    assert "wholesale retry exhausted 1 time(s)" in suffix
    # Without cause data the fallback phrasing is used.
    assert "repeated grader non-response" in suffix


@pytest.mark.asyncio
async def test_spawn_gate_metrics_absent_when_no_activity(db_session):
    """spawn_gate_metrics is absent from direction stats when no spawn-gate counters are set."""
    project = _make_project(
        directions=[
            {
                "id": "d-sg02",
                "status": "active",
                "text": "Direction without spawn-gate history",
                "priority": "medium",
                "research_history": [{"prs_merged": 1}],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-sg02")

    assert "spawn_gate_metrics" not in d_stat, (
        "spawn_gate_metrics should be absent when no spawn-gate activity"
    )


@pytest.mark.asyncio
async def test_spawn_gate_exhaustion_note_names_cause_distribution(db_session):
    """Exhaustion hint names observed cause distribution when cause_distribution is present in spawn_gate_metrics."""
    project = _make_project(
        directions=[
            {
                "id": "d-sg03",
                "status": "active",
                "text": "Direction with cause data",
                "priority": "medium",
                "research_history": [
                    {
                        "prs_merged": 1,
                        "research_spawn_gate_challenged_count": 3,
                        "research_spawn_gate_unresolved_count": 2,
                        "research_spawn_gate_challenged_spawned": 2,
                        "research_spawn_gate_wholesale_retry_exhausted": True,
                        "research_spawn_gate_cause_counts": {
                            "AGENT_TIMEOUT": 2,
                            "RATE_LIMIT": 1,
                        },
                    }
                ],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    payload = tasks[0].payloads[0]
    suffix = payload.get("prompt_suffix", "")
    assert "wholesale retry exhausted 1 time(s)" in suffix
    assert "observed causes:" in suffix
    assert "AGENT_TIMEOUT x2" in suffix
    assert "RATE_LIMIT x1" in suffix
    # Fallback phrasing should NOT appear when cause data is present.
    assert "looks infrastructural" not in suffix


@pytest.mark.asyncio
async def test_spawn_gate_exhaustion_note_falls_back_without_cause_data(db_session):
    """Exhaustion hint falls back to generic phrasing when no cause_distribution is present."""
    project = _make_project(
        directions=[
            {
                "id": "d-sg04",
                "status": "active",
                "text": "Direction without cause data",
                "priority": "medium",
                "research_history": [
                    {
                        "prs_merged": 1,
                        "research_spawn_gate_challenged_count": 2,
                        "research_spawn_gate_challenged_spawned": 2,
                        "research_spawn_gate_wholesale_retry_exhausted": True,
                    }
                ],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    payload = tasks[0].payloads[0]
    suffix = payload.get("prompt_suffix", "")
    assert "wholesale retry exhausted 1 time(s)" in suffix
    assert "repeated grader non-response" in suffix
    assert "looks infrastructural" in suffix
    assert "observed causes:" not in suffix


@pytest.mark.asyncio
async def test_strategic_review_prompt_suffix_includes_mode_hints(db_session):
    """Strategic review prompt suffix should spell out mode-specific guidance per direction."""
    project = _make_project(
        directions=[
            {
                "id": "d-mode-audit",
                "status": "active",
                "text": "Direction in audit mode",
                "priority": "high",
                "recommended_mode": "audit",
            },
            {
                "id": "d-mode-pause",
                "status": "active",
                "text": "Direction in pause mode",
                "priority": "low",
                "recommended_mode": "pause",
            },
            {
                "id": "d-mode-operator",
                "status": "active",
                "text": "Operator-pinned fundamentals direction",
                "priority": "medium",
                "recommended_mode": "fundamentals",
                RECOMMENDED_MODE_META_KEY: {
                    "source": "operator",
                    "set_at": "2026-04-01T00:00:00",
                },
            },
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    payload = tasks[0].payloads[0]
    suffix = payload["prompt_suffix"]

    assert "Mode hints for this cycle:" in suffix
    assert "d-mode-audit" in suffix and "audit mode" in suffix
    assert "d-mode-pause" in suffix and "do not propose new feature work for" in suffix
    assert "d-mode-operator" in suffix and "fundamentals mode" in suffix


@pytest.mark.asyncio
async def test_spawn_strategic_review_no_cycle_id_does_not_modify_history(
    db_session, mocker
):
    """When called without cycle_id, _spawn_strategic_review leaves cycle history unchanged."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_cycle_history": [{"cycle_id": "existing", "outcome": "ok"}],
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_strategic_review(project, db_session, cycle_id=None)

    history = (project.vision or {}).get("_strategic_cycle_history", [])
    assert len(history) == 1
    assert history[0].get("outcome") == "ok"


@pytest.mark.asyncio
async def test_spawn_strategic_review_task_context_includes_cycle_id(
    db_session, mocker
):
    """Task created by _spawn_strategic_review has strategic_cycle_id in context."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    cycle_id = "sc-testcycle"
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_cycle_history": [{"cycle_id": cycle_id}],
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_strategic_review(project, db_session, cycle_id=cycle_id)

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert tasks[0].context.get("strategic_cycle_id") == cycle_id


@pytest.mark.asyncio
async def test_spawn_strategic_review_records_preferred_runner_without_pin(
    db_session, mocker
):
    """Preferred runner affinity is advisory context, not an authoritative pin."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    cycle_id = "sc-runner-pref"
    preferred_runner_id = str(uuid.uuid4())
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_strategic_review(
        project,
        db_session,
        cycle_id=cycle_id,
        preferred_runner_target_id=preferred_runner_id,
    )

    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().one()
    assert task.context.get("strategic_cycle_id") == cycle_id
    assert task.context.get("preferred_runner_target_id") == preferred_runner_id
    assert "runner_target_id" not in task.context
    assert not (task.vm_instance_id or "").startswith("runner:")


@pytest.mark.asyncio
async def test_design_doc_update_uses_live_local_continuation_runner_affinity(
    db_session, mocker
):
    """Continuous local Strategic mode makes Phase 1 local-required."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    project = _make_project(
        vision={
            "purpose": "test purpose",
            STRATEGIC_LOCAL_CONTINUATION_KEY: _local_continuation_marker(),
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    runner = _make_local_runner(
        project.id,
        capabilities={
            "task_types": ["design_doc_update"],
            "has_gpu": False,
            "max_concurrent": 2,
        },
    )
    db_session.add(runner)
    await db_session.commit()

    spawned = await _spawn_design_doc_update(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().one()
    assert task.payloads[0]["type"] == "design_doc_update"
    assert task.context.get(STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY) is True
    assert (
        task.context.get(STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY)
        == "workbench_continuous"
    )
    assert task.context.get("preferred_runner_target_id") == str(runner.id)
    assert STRATEGIC_LOCAL_CONTINUATION_KEY not in task.payloads[0]["vision"]
    assert "runner_target_id" not in task.context
    assert not (task.vm_instance_id or "").startswith("runner:")


@pytest.mark.asyncio
async def test_strategic_review_uses_live_local_continuation_runner_affinity(
    db_session, mocker
):
    """Strategy-only Strategic reviews also become local-required."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    project = _make_project(
        vision={
            "purpose": "test purpose",
            STRATEGIC_LOCAL_CONTINUATION_KEY: _local_continuation_marker(),
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    stale_runner = _make_local_runner(
        project.id,
        capabilities={
            "task_types": ["strategic_review"],
            "has_gpu": False,
            "max_concurrent": 2,
        },
        last_seen_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
    )
    live_runner = _make_local_runner(
        project.id,
        capabilities={
            "task_types": ["strategic_review"],
            "has_gpu": False,
            "max_concurrent": 2,
        },
    )
    db_session.add(stale_runner)
    db_session.add(live_runner)
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().one()
    assert task.payloads[0]["type"] == "strategic_review"
    assert task.context.get(STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY) is True
    assert (
        task.context.get(STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY)
        == "workbench_continuous"
    )
    assert task.context.get("preferred_runner_target_id") == str(live_runner.id)
    assert STRATEGIC_LOCAL_CONTINUATION_KEY not in task.payloads[0]["vision"]
    assert "runner_target_id" not in task.context
    assert not (task.vm_instance_id or "").startswith("runner:")


@pytest.mark.asyncio
async def test_local_continuation_affinity_omitted_when_stale_or_incapable(
    db_session, mocker
):
    """Stale continuation markers and incompatible runners do not bias dispatch."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    stale_at = (
        datetime.now(timezone.utc)
        - timedelta(seconds=STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS + 60)
    ).isoformat()
    project = _make_project(
        vision={
            "purpose": "test purpose",
            STRATEGIC_LOCAL_CONTINUATION_KEY: _local_continuation_marker(
                updated_at=stale_at,
                last_seen_at=stale_at,
            ),
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        _make_local_runner(
            project.id,
            capabilities={
                "task_types": ["compute_experiment"],
                "has_gpu": True,
                "max_concurrent": 1,
            },
        )
    )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().one()
    assert task.payloads[0]["type"] == "strategic_review"
    assert STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY not in task.context
    assert "preferred_runner_target_id" not in task.context
    assert "runner_target_id" not in task.context


@pytest.mark.asyncio
async def test_local_continuation_requires_local_even_without_live_runner(
    db_session, mocker
):
    """Fresh continuous local mode must not lose hard local intent while offline."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    project = _make_project(
        vision={
            "purpose": "test purpose",
            STRATEGIC_LOCAL_CONTINUATION_KEY: _local_continuation_marker(),
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        _make_local_runner(
            project.id,
            capabilities={
                "task_types": ["strategic_review"],
                "has_gpu": False,
                "max_concurrent": 2,
            },
            status="inactive",
            last_seen_at=datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(hours=1),
        )
    )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().one()
    assert task.payloads[0]["type"] == "strategic_review"
    assert task.context.get(STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY) is True
    assert (
        task.context.get(STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY)
        == "workbench_continuous"
    )
    assert "preferred_runner_target_id" not in task.context
    assert "runner_target_id" not in task.context
    assert not (task.vm_instance_id or "").startswith("runner:")


@pytest.mark.asyncio
async def test_local_continuation_ttl_bounded_to_lease_window(
    db_session, mocker
):
    """Continuation TTL is the short lease window, not a 12h wall clock.

    SAFETY (dead-man's switch): the local-Strategic continuation TTL must no
    longer OUTLIVE the idle workbench TTL. The retired 12h-vs-4h relationship let
    an opened-and-abandoned session pin the project locally for 12h; the TTL is
    now bounded to the short lease window so abandonment self-heals fast. A marker
    still WITHIN the (short) TTL keeps its hard-local intent — verified here — while
    a marker aged past it drops to cloud (test_local_continuation_affinity_
    omitted_when_stale_or_incapable covers the stale case).
    """
    assert STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS <= SESSION_IDLE_TTL_SECONDS
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    fresh_at = (
        datetime.now(timezone.utc) - timedelta(seconds=60)
    ).isoformat()
    project = _make_project(
        vision={
            "purpose": "test purpose",
            STRATEGIC_LOCAL_CONTINUATION_KEY: _local_continuation_marker(
                updated_at=fresh_at,
                last_seen_at=fresh_at,
            ),
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().one()
    assert task.payloads[0]["type"] == "strategic_review"
    assert task.context.get(STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY) is True
    assert (
        task.context.get(STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY)
        == "workbench_continuous"
    )
    assert "preferred_runner_target_id" not in task.context
    assert "runner_target_id" not in task.context


@pytest.mark.asyncio
async def test_local_continuation_requires_local_for_fresh_foreign_owner_marker(
    db_session, mocker
):
    """The compact project marker is intentionally project-level at spawn time."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    project = _make_project(
        vision={
            "purpose": "test purpose",
            STRATEGIC_LOCAL_CONTINUATION_KEY: _local_continuation_marker(
                operator_user_id="other-operator",
                owner_session_started_at=(
                    datetime.now(timezone.utc) - timedelta(minutes=10)
                ).isoformat(),
            ),
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().one()
    assert task.payloads[0]["type"] == "strategic_review"
    assert task.context.get(STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY) is True
    assert (
        task.context.get(STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY)
        == "workbench_continuous"
    )
    assert "preferred_runner_target_id" not in task.context


@pytest.mark.asyncio
async def test_local_continuation_affinity_reads_current_db_marker(
    db_session, mocker
):
    """A stale in-memory Project marker must not bias strategy-only dispatch."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    project = _make_project(
        vision={
            "purpose": "test purpose",
            STRATEGIC_LOCAL_CONTINUATION_KEY: _local_continuation_marker(),
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    runner = _make_local_runner(
        project.id,
        capabilities={
            "task_types": ["strategic_review"],
            "has_gpu": False,
            "max_concurrent": 2,
        },
    )
    db_session.add(runner)
    await db_session.commit()

    await db_session.execute(
        update(Project)
        .where(Project.id == project.id)
        .values(vision={"purpose": "test purpose"})
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()
    assert STRATEGIC_LOCAL_CONTINUATION_KEY in (project.vision or {})

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().one()
    assert STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY not in task.context
    assert "preferred_runner_target_id" not in task.context
    assert STRATEGIC_LOCAL_CONTINUATION_KEY not in task.payloads[0]["vision"]


def test_phase_runner_affinity_falls_back_to_preferred_runner_target_id():
    runner_id = str(uuid.uuid4())
    task = Task(
        project_id=uuid.uuid4(),
        title="Architecture review",
        description="phase one",
        payloads=[{"type": "design_doc_update"}],
        context={"preferred_runner_target_id": runner_id},
    )

    assert _phase_runner_affinity(task) == runner_id


# ── previous_decision_outcomes in _spawn_strategic_review payload ────────────


@pytest.mark.asyncio
async def test_previous_decision_outcomes_populated_from_prior_cycle(db_session):
    """Payload surfaces current performance of directions created/retired by the prior cycle."""
    project = _make_project(
        directions=[
            {
                "id": "d-new01",
                "status": "active",
                "text": "Newly created direction",
                "priority": "medium",
                "research_history": [{"prs_merged": 2, "prs_rejected": 0}],
            },
            {
                "id": "d-retired01",
                "status": "retired",
                "text": "Direction retired last cycle",
                "priority": "low",
                "research_history": [],
            },
        ],
        vision={
            "purpose": "test purpose",
            "_strategic_cycle_history": [
                {
                    "cycle_id": "sc-prev0001",
                    "outcome": "completed",
                    "phase2_completed_at": "2026-04-19T00:00:00+00:00",
                    "created_direction_ids": ["d-new01"],
                    "retired_direction_ids": ["d-retired01"],
                    "summary_text": "rev 2, 3 active, 1 created, 1 retired",
                },
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    prev = payload.get("previous_decision_outcomes")
    assert prev is not None, "Expected previous_decision_outcomes in payload"
    assert prev["cycle_id"] == "sc-prev0001"
    assert len(prev["created"]) == 1
    assert len(prev["retired"]) == 1
    new_stat = prev["created"][0]
    assert new_stat["direction_id"] == "d-new01"
    assert new_stat["role"] == "created"
    assert new_stat["status"] == "active"
    assert new_stat["prs_merged"] == 2
    assert new_stat["cycles_active"] == 1
    assert "quality_score" in new_stat
    assert new_stat["auto_paused"] is False
    retired_stat = prev["retired"][0]
    assert retired_stat["role"] == "retired"
    assert retired_stat["status"] == "retired"


@pytest.mark.asyncio
async def test_previous_decision_outcomes_absent_when_no_prior_decisions(db_session):
    """No created/retired ids in prior cycles → previous_decision_outcomes omitted."""
    project = _make_project(
        directions=[
            {"id": "d-a", "status": "active", "text": "d", "priority": "medium"}
        ],
        vision={
            "purpose": "test",
            "_strategic_cycle_history": [
                {
                    "cycle_id": "sc-old",
                    "outcome": "completed",
                },  # no created/retired ids
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    await _spawn_strategic_review(project, db_session)
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().first().payloads[0]
    assert "previous_decision_outcomes" not in payload


@pytest.mark.asyncio
async def test_previous_decision_outcomes_surfaces_created_rationales(db_session):
    """created_rationales from prior cycle history appear on created direction stats."""
    project = _make_project(
        directions=[
            {
                "id": "d-new01",
                "status": "active",
                "text": "new direction",
                "priority": "medium",
                "research_history": [{"prs_merged": 2}],
            },
        ],
        vision={
            "purpose": "test",
            "_strategic_cycle_history": [
                {
                    "cycle_id": "sc-rat0001",
                    "outcome": "completed",
                    "created_direction_ids": ["d-new01"],
                    "retired_direction_ids": [],
                    "created_rationales": {
                        "d-new01": "Observed gap in error handling coverage; new direction targets resilience."
                    },
                },
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    await _spawn_strategic_review(project, db_session)

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().first().payloads[0]
    prev = payload.get("previous_decision_outcomes")
    assert prev is not None
    new_stat = prev["created"][0]
    assert new_stat["direction_id"] == "d-new01"
    assert new_stat.get("rationale") == (
        "Observed gap in error handling coverage; new direction targets resilience."
    )


@pytest.mark.asyncio
async def test_previous_decision_outcomes_surfaces_retired_rationales(db_session):
    """retired_rationales are attached to retired direction stats (including missing ones)."""
    project = _make_project(
        directions=[],  # retired direction removed
        vision={
            "purpose": "test",
            "_strategic_cycle_history": [
                {
                    "cycle_id": "sc-rat0002",
                    "outcome": "completed",
                    "created_direction_ids": [],
                    "retired_direction_ids": ["d-retired01"],
                    "retired_rationales": {
                        "d-retired01": "Saturated; low PR merge volume for 3 cycles."
                    },
                },
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    await _spawn_strategic_review(project, db_session)

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().first().payloads[0]
    prev = payload.get("previous_decision_outcomes")
    assert prev is not None
    retired_stat = prev["retired"][0]
    assert retired_stat["direction_id"] == "d-retired01"
    assert (
        retired_stat.get("rationale") == "Saturated; low PR merge volume for 3 cycles."
    )


@pytest.mark.asyncio
async def test_previous_decision_outcomes_surfaces_cycle_reasoning(db_session):
    """cycle_reasoning from prior cycle history appears at the top level of the outcome block."""
    project = _make_project(
        directions=[
            {"id": "d-x", "status": "active", "text": "d", "priority": "medium"},
        ],
        vision={
            "purpose": "test",
            "_strategic_cycle_history": [
                {
                    "cycle_id": "sc-reason01",
                    "outcome": "completed",
                    "created_direction_ids": ["d-x"],
                    "cycle_reasoning": (
                        "Portfolio skewed toward infra; created direction to restore "
                        "feature-work balance."
                    ),
                },
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    await _spawn_strategic_review(project, db_session)

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().first().payloads[0]
    prev = payload.get("previous_decision_outcomes")
    assert prev is not None
    assert prev.get("cycle_reasoning") == (
        "Portfolio skewed toward infra; created direction to restore feature-work balance."
    )


@pytest.mark.asyncio
async def test_previous_decision_outcomes_rationale_absent_when_not_recorded(
    db_session,
):
    """When rationale fields are not present in the cycle entry, direction stats have no rationale key."""
    project = _make_project(
        directions=[
            {
                "id": "d-r",
                "status": "active",
                "text": "d",
                "priority": "medium",
                "research_history": [],
            },
        ],
        vision={
            "purpose": "test",
            "_strategic_cycle_history": [
                {
                    "cycle_id": "sc-norat",
                    "outcome": "completed",
                    "created_direction_ids": ["d-r"],
                    # no created_rationales / retired_rationales / cycle_reasoning
                },
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    await _spawn_strategic_review(project, db_session)

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().first().payloads[0]
    prev = payload.get("previous_decision_outcomes")
    assert prev is not None
    assert "rationale" not in prev["created"][0]
    assert "cycle_reasoning" not in prev


@pytest.mark.asyncio
async def test_previous_decision_outcomes_missing_retired_direction(db_session):
    """A retired direction id no longer in project.directions is reported as status=missing."""
    project = _make_project(
        directions=[],  # retired direction was removed
        vision={
            "purpose": "test",
            "_strategic_cycle_history": [
                {
                    "cycle_id": "sc-gone",
                    "outcome": "completed",
                    "created_direction_ids": [],
                    "retired_direction_ids": ["d-deleted"],
                },
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    await _spawn_strategic_review(project, db_session)
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().first().payloads[0]
    prev = payload.get("previous_decision_outcomes")
    assert prev is not None
    assert prev["retired"] == [
        {"direction_id": "d-deleted", "role": "retired", "status": "missing"}
    ]


# ── _build_shipped_changes_manifest ──────────────────────────────────────────


def _make_merged_task(
    project_id, title="Fix bug", direction_id="d-001", changed_files=None, scope="small"
):
    """Create a COMPLETED task with pr_merged=True context."""
    return Task(
        project_id=project_id,
        title=title,
        description="test",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "pr_merged": True,
            "direction_id": direction_id,
            "_pr_changed_files": changed_files or ["backend/app/foo.py"],
            "scope": scope,
        },
    )


@pytest.mark.asyncio
async def test_build_shipped_manifest_empty_when_no_merged_tasks(db_session):
    """Returns empty list when there are no completed tasks with pr_merged=True."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Add a COMPLETED task without pr_merged
    task = Task(
        project_id=project.id,
        title="No merge",
        description="test",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": False},
    )
    db_session.add(task)
    await db_session.commit()

    manifest = await _build_shipped_changes_manifest(project, db_session)
    assert manifest == []


@pytest.mark.asyncio
async def test_build_shipped_manifest_groups_by_direction(db_session):
    """Tasks are grouped by direction_id; all_changed_files is deduplicated."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        _make_merged_task(
            project.id, "Task A", direction_id="d-001", changed_files=["a.py"]
        )
    )
    db_session.add(
        _make_merged_task(
            project.id, "Task B", direction_id="d-001", changed_files=["a.py", "b.py"]
        )
    )
    db_session.add(
        _make_merged_task(
            project.id, "Task C", direction_id="d-002", changed_files=["c.py"]
        )
    )
    await db_session.commit()

    manifest = await _build_shipped_changes_manifest(project, db_session)

    assert len(manifest) == 2
    by_dir = {g["direction_id"]: g for g in manifest}
    assert "d-001" in by_dir
    assert "d-002" in by_dir
    assert len(by_dir["d-001"]["tasks"]) == 2
    assert len(by_dir["d-002"]["tasks"]) == 1
    # Deduplication: a.py appears twice in d-001 but should appear once
    assert by_dir["d-001"]["all_changed_files"].count("a.py") == 1
    assert "b.py" in by_dir["d-001"]["all_changed_files"]


@pytest.mark.asyncio
async def test_build_shipped_manifest_excludes_non_merged(db_session):
    """Tasks without pr_merged=True are excluded even if COMPLETED."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_merged_task(project.id, "Merged task", direction_id="d-001"))
    # Non-merged completed task
    non_merged = Task(
        project_id=project.id,
        title="Unmerged task",
        description="test",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": False, "direction_id": "d-001"},
    )
    db_session.add(non_merged)
    await db_session.commit()

    manifest = await _build_shipped_changes_manifest(project, db_session)

    assert len(manifest) == 1
    assert len(manifest[0]["tasks"]) == 1
    assert manifest[0]["tasks"][0]["title"] == "Merged task"


@pytest.mark.asyncio
async def test_build_shipped_manifest_unassigned_direction(db_session):
    """Tasks with no direction_id are grouped under 'unassigned' direction_id=None."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Orphan task",
        description="test",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "_pr_changed_files": ["x.py"], "scope": "medium"},
    )
    db_session.add(task)
    await db_session.commit()

    manifest = await _build_shipped_changes_manifest(project, db_session)

    assert len(manifest) == 1
    assert manifest[0]["direction_id"] is None  # grouped with None key


@pytest.mark.asyncio
async def test_build_shipped_manifest_cutoff_from_design_doc(db_session):
    """Tasks completed before design_doc._meta.last_updated are excluded."""
    project = _make_project(
        vision={"purpose": "test"},
        design_doc={
            "_meta": {
                # Future cutoff — excludes all current tasks
                "last_updated": (
                    datetime.now(timezone.utc) + timedelta(hours=1)
                ).isoformat()
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        _make_merged_task(project.id, "Old merged task", direction_id="d-001")
    )
    await db_session.commit()

    manifest = await _build_shipped_changes_manifest(project, db_session)
    # All tasks have updated_at now (before the future cutoff), so they're excluded
    assert manifest == []


@pytest.mark.asyncio
async def test_build_shipped_manifest_cutoff_from_cycle_history(db_session):
    """Falls back to _strategic_cycle_history phase1_completed_at when design_doc has no timestamp."""
    project = _make_project(
        vision={
            "purpose": "test",
            "_strategic_cycle_history": [
                {
                    "cycle_id": "sc-old",
                    # Future cutoff in cycle history
                    "phase1_completed_at": (
                        datetime.now(timezone.utc) + timedelta(hours=1)
                    ).isoformat(),
                }
            ],
        },
        design_doc={"_meta": {}},  # No last_updated
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    db_session.add(_make_merged_task(project.id, "Excluded task", direction_id="d-001"))
    await db_session.commit()

    manifest = await _build_shipped_changes_manifest(project, db_session)
    assert manifest == []


@pytest.mark.asyncio
async def test_build_shipped_manifest_excludes_rollup_children(db_session):
    """Rollup child tasks (parent_task_id set) are excluded from the manifest.

    A rollup child's PR merges to the rollup staging branch, not the integration
    branch. Only the rollup parent (root task, parent_task_id=None) should appear
    in the manifest once its own PR lands on the integration branch.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Rollup parent — its PR landed on the integration branch
    parent = Task(
        project_id=project.id,
        title="Rollup: ship feature X",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "direction_id": "d-001"},
    )
    db_session.add(parent)
    await db_session.flush()

    # Rollup child — its PR merged to the staging branch only
    child = Task(
        project_id=project.id,
        title="child: add helper",
        description="child",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        parent_task_id=parent.id,
        context={"pr_merged": True, "direction_id": "d-001"},
    )
    db_session.add(child)
    await db_session.commit()

    manifest = await _build_shipped_changes_manifest(project, db_session)

    # Only the root task should appear; the child should be excluded
    assert len(manifest) == 1
    assert manifest[0]["direction_id"] == "d-001"
    assert len(manifest[0]["tasks"]) == 1
    assert manifest[0]["tasks"][0]["title"] == "Rollup: ship feature X"


# ── predict_spawn_success: quality-score and momentum signals ─────────────────


class TestPredictSpawnSuccessQualityAndMomentum:
    """Signals 16 (quality score) and 17 (momentum factor) in predict_spawn_success."""

    @pytest.fixture(autouse=True)
    def _clear_pipeline_rates(self):
        from app.shared_state import runtime_config

        prev = runtime_config.get("pipeline_scope_merge_rates")
        runtime_config["pipeline_scope_merge_rates"] = {}
        yield
        if prev is None:
            runtime_config.pop("pipeline_scope_merge_rates", None)
        else:
            runtime_config["pipeline_scope_merge_rates"] = prev

    def test_quality_score_signal_present_with_history(self):
        from app.spawn_calibration import predict_spawn_success

        healthy = {
            "research_history": [
                {
                    "taxonomy_summary": {"code_defect": 3},
                    "tasks_created": 2,
                    "prs_merged": 2,
                },
            ],
        }
        poor = {
            "research_history": [
                {
                    "taxonomy_summary": {"infra_related": 3, "review_quality": 2},
                    "tasks_created": 2,
                    "prs_merged": 0,
                },
            ],
        }
        assert predict_spawn_success("small", healthy) > predict_spawn_success(
            "small", poor
        )

    def test_no_history_skips_quality_signal(self):
        from app.spawn_calibration import predict_spawn_success

        assert predict_spawn_success("small", {}) == 0.5

    def test_momentum_hot_streak_boosts_prediction(self):
        from app.spawn_calibration import predict_spawn_success

        hot = {
            "research_history": [
                {"tasks_created": 2, "prs_merged": 1},
                {"tasks_created": 2, "prs_merged": 1},
            ],
        }
        neutral = {
            "research_history": [
                {"tasks_created": 2, "prs_merged": 1},
            ],
        }
        assert predict_spawn_success("small", hot) > predict_spawn_success(
            "small", neutral
        )

    def test_momentum_stall_penalizes_prediction(self):
        from app.spawn_calibration import predict_spawn_success

        stalled = {
            "research_history": [
                {"tasks_created": 0, "taxonomy_summary": {"code_defect": 1}},
                {"tasks_created": 0, "taxonomy_summary": {"code_defect": 1}},
                {"tasks_created": 0, "taxonomy_summary": {"code_defect": 1}},
            ],
        }
        neutral = {
            "research_history": [
                {"tasks_created": 0, "taxonomy_summary": {"code_defect": 1}},
            ],
        }
        assert predict_spawn_success("small", stalled) < predict_spawn_success(
            "small", neutral
        )

    def test_result_within_bounds(self):
        from app.spawn_calibration import predict_spawn_success

        direction = {
            "research_history": [
                {"tasks_created": 2, "prs_merged": 1},
            ],
        }
        result = predict_spawn_success("small", direction)
        assert 0.0 <= result <= 1.0

    def test_remediation_regression_penalizes_prediction(self):
        from app.spawn_calibration import predict_spawn_success

        base_history = [{"tasks_created": 2, "prs_merged": 1}]
        clean = {
            "research_history": base_history,
            "remediation_history": [
                {"result": "merged", "fingerprint": "fp-a"},
                {"result": "merged", "fingerprint": "fp-b"},
            ],
        }
        regressed = {
            "research_history": base_history,
            "remediation_history": [
                {"result": "merged", "fingerprint": "fp-a"},
                {"result": "merged", "fingerprint": "fp-b"},
                {"result": "failed", "fingerprint": "fp-a"},
                {"result": "failed", "fingerprint": "fp-b"},
            ],
        }
        assert predict_spawn_success("small", regressed) < predict_spawn_success(
            "small", clean
        )


# ── predict_spawn_success: first-attempt rejection concentration ──────────────


class TestPredictSpawnSuccessFirstAttemptConcentration:
    """Signal 18 in predict_spawn_success."""

    @pytest.fixture(autouse=True)
    def _clear_pipeline_rates(self):
        from app.shared_state import runtime_config

        prev = runtime_config.get("pipeline_scope_merge_rates")
        runtime_config["pipeline_scope_merge_rates"] = {}
        yield
        if prev is None:
            runtime_config.pop("pipeline_scope_merge_rates", None)
        else:
            runtime_config["pipeline_scope_merge_rates"] = prev

    def _direction(self):
        return {
            "id": "dir-abc",
            "research_history": [{"tasks_created": 2, "prs_merged": 1}],
        }

    def test_strong_concentration_penalty(self):
        from app.spawn_calibration import predict_spawn_success

        direction = self._direction()
        project_bad = _make_project(
            design_doc={
                "direction_first_attempt_feedback": {
                    "dir-abc": {"correctness": 8, "style": 2, "total": 10},
                },
            }
        )
        project_clean = _make_project(design_doc={})
        assert predict_spawn_success(
            "small", direction, project=project_bad
        ) < predict_spawn_success("small", direction, project=project_clean)

    def test_moderate_concentration_penalty(self):
        from app.spawn_calibration import predict_spawn_success

        direction = self._direction()
        project_mod = _make_project(
            design_doc={
                "direction_first_attempt_feedback": {
                    "dir-abc": {"correctness": 3, "style": 2, "scope": 2, "total": 7},
                },
            }
        )
        project_strong = _make_project(
            design_doc={
                "direction_first_attempt_feedback": {
                    "dir-abc": {"correctness": 8, "style": 2, "total": 10},
                },
            }
        )
        # Strong signal (0.3) penalizes more than moderate (0.5)
        assert predict_spawn_success(
            "small", direction, project=project_strong
        ) < predict_spawn_success("small", direction, project=project_mod)

    def test_below_threshold_omitted(self):
        from app.spawn_calibration import predict_spawn_success

        direction = self._direction()
        project_low = _make_project(
            design_doc={
                "direction_first_attempt_feedback": {
                    "dir-abc": {"correctness": 2, "style": 1, "total": 3},
                },
            }
        )
        project_none = _make_project(design_doc={})
        assert predict_spawn_success(
            "small", direction, project=project_low
        ) == predict_spawn_success("small", direction, project=project_none)

    def test_spread_not_concentrated_omitted(self):
        from app.spawn_calibration import predict_spawn_success

        direction = self._direction()
        project_spread = _make_project(
            design_doc={
                "direction_first_attempt_feedback": {
                    "dir-abc": {"a": 2, "b": 2, "c": 2, "d": 2, "total": 8},
                },
            }
        )
        project_none = _make_project(design_doc={})
        assert predict_spawn_success(
            "small", direction, project=project_spread
        ) == predict_spawn_success("small", direction, project=project_none)

    def test_missing_direction_id_omitted(self):
        from app.spawn_calibration import predict_spawn_success

        direction = {"research_history": [{"tasks_created": 2, "prs_merged": 1}]}
        project_bad = _make_project(
            design_doc={
                "direction_first_attempt_feedback": {
                    "dir-abc": {"correctness": 8, "total": 8},
                },
            }
        )
        project_none = _make_project(design_doc={})
        assert predict_spawn_success(
            "small", direction, project=project_bad
        ) == predict_spawn_success("small", direction, project=project_none)


# ── anomaly_summary in _spawn_strategic_review payload ───────────────────────


@pytest.mark.asyncio
async def test_anomaly_summary_surfaces_high_occurrence_entries(db_session):
    """Entries with occurrence_count >= 3 are surfaced; low-occurrence entries are filtered."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    proj_key = str(project.id)
    _shared_state.persistent_anomalies[proj_key] = {
        "CODE_ERROR": {
            "occurrence_count": 5,
            "severity": "high",
            "first_seen": "2026-04-01T00:00:00",
            "last_seen": "2026-04-10T00:00:00",
        },
        "LOW_SIGNAL": {
            "occurrence_count": 2,
            "severity": "low",
            "first_seen": "2026-04-01T00:00:00",
            "last_seen": "2026-04-05T00:00:00",
        },
        "CrossDirectionFingerprint_abc123": {
            "occurrence_count": 4,
            "severity": "high",
            "fingerprint": "abc123",
            "affected_directions": ["d-1", "d-2", "d-3"],
            "failure_count": 12,
            "first_seen": "2026-04-01T00:00:00",
            "last_seen": "2026-04-10T00:00:00",
        },
    }
    try:
        spawned = await _spawn_strategic_review(project, db_session)
        assert spawned is True
        result = await db_session.execute(
            _select_agent_tasks(project.id)
        )
        payload = result.scalars().all()[0].payloads[0]

        assert "anomaly_summary" in payload
        summary = payload["anomaly_summary"]
        recurring_keys = [e["key"] for e in summary["recurring_anomalies"]]
        assert "CODE_ERROR" in recurring_keys
        assert "LOW_SIGNAL" not in recurring_keys

        cross = summary["cross_direction_fingerprints"]
        assert len(cross) == 1
        assert cross[0]["key"] == "CrossDirectionFingerprint_abc123"
        assert cross[0]["affected_directions"] == ["d-1", "d-2", "d-3"]
        assert cross[0]["fingerprint"] == "abc123"
        assert cross[0]["failure_count"] == 12
    finally:
        _shared_state.persistent_anomalies.pop(proj_key, None)


@pytest.mark.asyncio
async def test_anomaly_summary_absent_when_no_qualifying_entries(db_session):
    """When there are no occurrence_count>=3 entries, anomaly_summary is omitted."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    proj_key = str(project.id)
    _shared_state.persistent_anomalies[proj_key] = {
        "ONE_OFF": {
            "occurrence_count": 1,
            "severity": "medium",
            "first_seen": "2026-04-01T00:00:00",
            "last_seen": "2026-04-01T00:00:00",
        },
    }
    try:
        await _spawn_strategic_review(project, db_session)
        result = await db_session.execute(
            _select_agent_tasks(project.id)
        )
        payload = result.scalars().all()[0].payloads[0]
        assert "anomaly_summary" not in payload
    finally:
        _shared_state.persistent_anomalies.pop(proj_key, None)


@pytest.mark.asyncio
async def test_anomaly_summary_skips_non_dict_sentinel(db_session):
    """A non-dict sibling value (the 'signal_last_computed_at' ISO-string sentinel
    written by anomaly_detection._compute_research_anomalies) must not crash the
    strategic-review spawn. Regression: _build_anomaly_summary previously called
    .get() on the string and raised AttributeError, hard-killing every spawn."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    proj_key = str(project.id)
    _shared_state.persistent_anomalies[proj_key] = {
        # Bookkeeping sentinel — a string value living alongside the entry dicts.
        "signal_last_computed_at": "2026-05-23T00:00:00",
        "CODE_ERROR": {
            "occurrence_count": 5,
            "severity": "high",
            "first_seen": "2026-04-01T00:00:00",
            "last_seen": "2026-04-10T00:00:00",
        },
    }
    try:
        spawned = await _spawn_strategic_review(project, db_session)
        assert spawned is True
        result = await db_session.execute(
            _select_agent_tasks(project.id)
        )
        payload = result.scalars().all()[0].payloads[0]

        # The real entry is still surfaced; the sentinel is silently skipped.
        summary = payload["anomaly_summary"]
        recurring_keys = [e["key"] for e in summary["recurring_anomalies"]]
        assert "CODE_ERROR" in recurring_keys
        assert "signal_last_computed_at" not in recurring_keys
    finally:
        _shared_state.persistent_anomalies.pop(proj_key, None)


# ── playbook_summary in _spawn_strategic_review payload ──────────────────────


@pytest.mark.asyncio
async def test_playbook_summary_includes_relevant_entries(db_session, mocker):
    """Strategy-relevant playbook entries are surfaced; irrelevant ones filtered."""
    _patch_spawning_deps(mocker)
    design_doc = {
        "playbook/auto-importerror-foo": {
            "content": "Repeated failure pattern (3x in 7d): importerror.",
            "source": "auto-generated",
            "updated_at": "2026-04-15T00:00:00",
            "confirmed_count": 0,
        },
        "playbook/review-test-coverage": {
            "content": "Review pattern from PR #100: test coverage issues.",
            "source": "review-extracted",
            "updated_at": "2026-04-18T00:00:00",
            "hit_count": 4,
        },
        "playbook/first-attempt-d12345-error-handling": {
            "content": "First-attempt rejection pattern (direction d12345).",
            "source": "first-attempt-pattern",
            "updated_at": "2026-04-10T00:00:00",
            "rejection_count": 5,
        },
        "playbook/manual-direction-note": {
            "content": "When working on directions, prefer auto-pause over delete.",
            "source": "manual",
            "updated_at": "2026-04-12T00:00:00",
        },
        "playbook/success-create-small": {
            "content": "First-attempt success pattern for create tasks.",
            "source": "success-pattern",
            "updated_at": "2026-04-19T00:00:00",
        },
    }
    project = _make_project(vision={"purpose": "test"}, design_doc=design_doc)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "playbook_summary" in payload
    keys = [e["key"] for e in payload["playbook_summary"]["entries"]]
    assert "review-test-coverage" in keys
    assert "auto-importerror-foo" in keys
    assert "first-attempt-d12345-error-handling" in keys
    assert "manual-direction-note" in keys  # keyword "directions" + "auto-pause"
    assert "success-create-small" not in keys
    # Sorted newest-first
    assert keys[0] == "review-test-coverage"


@pytest.mark.asyncio
async def test_playbook_summary_absent_when_no_relevant_entries(db_session, mocker):
    """Without strategy-relevant playbook entries, the key is omitted."""
    _patch_spawning_deps(mocker)
    design_doc = {
        "playbook/success-create-small": {
            "content": "First-attempt success pattern for create tasks.",
            "source": "success-pattern",
            "updated_at": "2026-04-19T00:00:00",
        },
    }
    project = _make_project(vision={"purpose": "test"}, design_doc=design_doc)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    await _spawn_strategic_review(project, db_session)
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]
    assert "playbook_summary" not in payload


@pytest.mark.asyncio
async def test_playbook_summary_caps_at_ten_entries(db_session, mocker):
    """Only the 10 most recent qualifying entries are included."""
    _patch_spawning_deps(mocker)
    design_doc = {
        f"playbook/auto-pattern-{i:02d}": {
            "content": f"Repeated failure pattern {i}.",
            "source": "auto-generated",
            "updated_at": f"2026-04-{i + 1:02d}T00:00:00",
        }
        for i in range(15)
    }
    project = _make_project(vision={"purpose": "test"}, design_doc=design_doc)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    await _spawn_strategic_review(project, db_session)
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]
    entries = payload["playbook_summary"]["entries"]
    assert len(entries) == 10
    # Newest-first ordering — entry 14 has the latest date
    assert entries[0]["key"] == "auto-pattern-14"


# ── playbook_keys_index in _spawn_strategic_review payload ──────────────────


@pytest.mark.asyncio
async def test_playbook_keys_index_includes_all_entries(db_session, mocker):
    """Compact index lists ALL playbook entries (not just strategy-relevant ones)."""
    _patch_spawning_deps(mocker)
    design_doc = {
        "playbook/auto-importerror": {
            "content": "Pattern for importerror.",
            "source": "auto-generated",
            "updated_at": "2026-04-15T00:00:00",
        },
        "playbook/review-test": {
            "content": "Review pattern.",
            "source": "review-extracted",
            "updated_at": "2026-04-18T00:00:00",
        },
        "playbook/success-create": {
            "content": "Success pattern.",
            "source": "success-pattern",
            "updated_at": "2026-04-19T00:00:00",
        },
    }
    project = _make_project(vision={"purpose": "test"}, design_doc=design_doc)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "playbook_keys_index" in payload
    entries = payload["playbook_keys_index"]["entries"]
    # All 3 entries should be listed, even the success-pattern (not strategy-relevant)
    assert len(entries) == 3
    keys = {e["key"] for e in entries}
    assert keys == {"auto-importerror", "review-test", "success-create"}
    # Each entry should have key, source, and updated_at
    for entry in entries:
        assert "key" in entry
        assert "source" in entry
        assert "updated_at" in entry
        assert "content" not in entry  # compact index should NOT include content
    # Sorted newest-first
    assert entries[0]["key"] == "success-create"


@pytest.mark.asyncio
async def test_playbook_keys_index_absent_when_no_entries(db_session, mocker):
    """When no playbook entries exist, keys_index is omitted."""
    _patch_spawning_deps(mocker)
    project = _make_project(vision={"purpose": "test"}, design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    await _spawn_strategic_review(project, db_session)
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]
    assert "playbook_keys_index" not in payload


@pytest.mark.asyncio
async def test_playbook_keys_index_skips_pending_entries(db_session, mocker):
    """Pending entries (mid-flight writes) are excluded from the index."""
    _patch_spawning_deps(mocker)
    design_doc = {
        "playbook/completed": {
            "content": "Completed entry.",
            "source": "manual",
            "updated_at": "2026-04-15T00:00:00",
        },
        "playbook/pending": {
            "content": "Pending entry (in flight).",
            "source": "manual",
            "updated_at": "2026-04-16T00:00:00",
            "pending_source": "task-123",  # marks it as pending
        },
    }
    project = _make_project(vision={"purpose": "test"}, design_doc=design_doc)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    await _spawn_strategic_review(project, db_session)
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "playbook_keys_index" in payload
    entries = payload["playbook_keys_index"]["entries"]
    assert len(entries) == 1
    assert entries[0]["key"] == "completed"


# ---------------------------------------------------------------------------
# _agent_view_design_doc — strips agent-unused + internal keys
# ---------------------------------------------------------------------------


def test_stamp_direction_in_flight_counts():
    """_stamp_direction_in_flight stamps the per-direction in-flight count from
    active_by_direction onto each stat in place — staffed>0, idle=0, and a
    direction absent from the mapping defaults to 0 (present-even-when-0)."""
    from app.strategic_spawning import _stamp_direction_in_flight

    stats = [
        {"id": "d-staffed", "status": "active"},
        {"id": "d-idle", "status": "active"},
        {"id": "d-absent", "status": "active"},
    ]
    _stamp_direction_in_flight(stats, {"d-staffed": 3, "d-idle": 0})
    by_id = {s["id"]: s for s in stats}
    assert by_id["d-staffed"]["in_flight"] == 3
    assert by_id["d-idle"]["in_flight"] == 0
    # Absent from the mapping still gets an explicit 0, not a missing field.
    assert by_id["d-absent"]["in_flight"] == 0


def test_stamp_direction_in_flight_handles_none_mapping():
    """A None active_by_direction (e.g. a degraded path) stamps 0, never
    raises — graceful degrade."""
    from app.strategic_spawning import _stamp_direction_in_flight

    stats = [{"id": "d-1", "status": "active"}]
    _stamp_direction_in_flight(stats, None)
    assert stats[0]["in_flight"] == 0


def test_stamped_in_flight_matches_reconciliation_pipeline():
    """The count stamped onto direction_stats and the value
    _build_cycle_start_reconciliation reports in direction_pipeline[*].in_flight
    are derived from the SAME active_by_direction mapping, so they cannot
    disagree for a given direction."""
    from app.strategic_spawning import (
        _stamp_direction_in_flight,
        _build_cycle_start_reconciliation,
    )

    direction_stats = [
        {"id": "d-staffed", "status": "active", "text": "moving", "gap_id": "g-a"},
        {"id": "d-idle", "status": "active", "text": "quiet", "gap_id": "g-b"},
    ]
    active_by_direction = {"d-staffed": 2, "d-idle": 0}

    _stamp_direction_in_flight(direction_stats, active_by_direction)

    problem_model = {
        "gaps": [
            {"id": "g-a", "status": "active"},
            {"id": "g-b", "status": "active"},
        ]
    }
    recon = _build_cycle_start_reconciliation(
        direction_stats, problem_model, active_by_direction
    )
    pipeline_by_id = {
        entry["direction_id"]: entry for entry in recon["direction_pipeline"]
    }
    stamped_by_id = {s["id"]: s for s in direction_stats}
    for did in ("d-staffed", "d-idle"):
        assert stamped_by_id[did]["in_flight"] == pipeline_by_id[did]["in_flight"]
    assert stamped_by_id["d-staffed"]["in_flight"] == 2
    assert stamped_by_id["d-idle"]["in_flight"] == 0


def test_agent_view_design_doc_strips_playbook_archive():
    """playbook_archive is operator-only state — it must never ship in agent
    payloads. Unbounded growth of this field was the trigger for the silent
    pre-claim death investigation (task 595c235d, 351KB archive bloat)."""
    from app.strategic_spawning import _agent_view_design_doc

    doc = {
        "summary": "keep",
        "systems/sys-a": {"id": "sys-a"},
        "playbook_archive": [{"_archived_key": "playbook/foo"}] * 500,
    }
    view = _agent_view_design_doc(doc)
    assert "playbook_archive" not in view
    assert view["summary"] == "keep"
    assert view["systems/sys-a"] == {"id": "sys-a"}


def test_agent_view_design_doc_strips_internal_keys():
    """Keys prefixed with ``_`` are internal/operator state; the agent's
    design-doc handler already filters them client-side, so stripping them
    server-side is both safe and saves bytes on the wire."""
    from app.strategic_spawning import _agent_view_design_doc

    doc = {
        "summary": "keep",
        "_meta": {"last_updated": "x"},
        "_strategic_cycle_history": [1, 2, 3],
    }
    view = _agent_view_design_doc(doc)
    assert "_meta" not in view
    assert "_strategic_cycle_history" not in view
    assert view["summary"] == "keep"


def test_agent_view_design_doc_handles_empty_input():
    from app.strategic_spawning import _agent_view_design_doc

    assert _agent_view_design_doc(None) == {}
    assert _agent_view_design_doc({}) == {}


def test_agent_view_problem_model_reports_per_gap_memory_trim():
    """When a gap's append-only history/past_moves are trimmed to the recent
    window, the trim is surfaced via ``_gap_memory_trimmed`` so the strategic
    agent can tell the view was shortened and pull full detail via ./fetch.
    This pre-trim runs before the byte-budget packer, so without this marker
    nothing would signal the agent that the gap's arc is incomplete."""
    from app.strategic_spawning import (
        _agent_view_problem_model,
        _GAP_HISTORY_KEEP,
        _GAP_PAST_MOVES_KEEP,
    )

    pm = {
        "gaps": [
            {
                "id": "g-long-lived",
                "history": [{"e": i} for i in range(_GAP_HISTORY_KEEP + 4)],
                "past_moves": [{"m": i} for i in range(_GAP_PAST_MOVES_KEEP + 2)],
            },
            {
                "id": "g-fresh",
                "history": [{"e": 0}],
                "past_moves": [],
            },
        ]
    }
    view = _agent_view_problem_model(pm)

    # The trimmed gap is reported with the actual vs shown counts.
    trimmed = view["_gap_memory_trimmed"]
    assert [t["gap_id"] for t in trimmed] == ["g-long-lived"], (
        f"only the over-window gap should be reported: {trimmed}"
    )
    entry = trimmed[0]
    assert entry["fields"]["history"] == {
        "shown": _GAP_HISTORY_KEEP,
        "total": _GAP_HISTORY_KEEP + 4,
    }
    assert entry["fields"]["past_moves"] == {
        "shown": _GAP_PAST_MOVES_KEEP,
        "total": _GAP_PAST_MOVES_KEEP + 2,
    }
    # The arrays themselves are actually trimmed to the recent tail.
    g0 = view["gaps"][0]
    assert len(g0["history"]) == _GAP_HISTORY_KEEP
    assert g0["history"][-1] == {"e": _GAP_HISTORY_KEEP + 3}


def test_agent_view_problem_model_no_marker_when_nothing_trimmed():
    """A model whose gaps are all within the keep window carries no trim marker,
    so the agent treats the shipped view as complete (no spurious ./fetch)."""
    from app.strategic_spawning import _agent_view_problem_model

    pm = {
        "gaps": [
            {"id": "g-a", "history": [{"e": 0}], "past_moves": [{"m": 0}]},
        ]
    }
    view = _agent_view_problem_model(pm)
    assert "_gap_memory_trimmed" not in view
    # The durable input is never mutated.
    assert "_gap_memory_trimmed" not in pm


def test_agent_view_problem_model_drops_rejected_agent_edits():
    """The rejected-agent-edits ledger is surfaced via the consolidated
    ``operator_overrides`` payload key, so it must be stripped from the
    problem_model agent view to avoid double-shipping the raw ledger."""
    from app.strategic_spawning import _agent_view_problem_model

    pm = {
        "intent": {"purpose": "x"},
        "rejected_agent_edits": [
            {"at": "2026-06-01T00:00:00Z", "field": "context", "agent_value": "a"}
        ],
    }
    view = _agent_view_problem_model(pm)
    assert "rejected_agent_edits" not in view
    # The durable input is never mutated.
    assert "rejected_agent_edits" in pm


def test_agent_view_problem_model_cluster_reexported_from_sibling_module():
    """The agent-view problem_model builder cluster was peeled into the sibling
    module ``app.strategic_problem_view`` to keep ``strategic_spawning`` under its
    size cap. ``strategic_spawning`` must re-export every moved name so existing
    ``from app.strategic_spawning import ...`` call sites and tests keep resolving
    the SAME object — this guards the extraction contract against a future edit
    that shadows a re-export with a divergent local copy."""
    from app import strategic_problem_view, strategic_spawning

    for name in (
        "_agent_view_problem_model",
        "_agent_view_problem_model_packed",
        "_agent_view_problem_model_floor",
        "_compute_model_health",
        "_shrink_pm_to_floor",
        "_skeletonize_caps_friction",
        "_serialized_bytes",
        "_GAP_HISTORY_KEEP",
        "_GAP_PAST_MOVES_KEEP",
        "_GAP_EVIDENCE_KEEP",
        "_AGENT_VIEW_FLOOR_BYTES",
    ):
        assert hasattr(strategic_problem_view, name), (
            f"{name} must be defined in the sibling module"
        )
        assert getattr(strategic_spawning, name) is getattr(
            strategic_problem_view, name
        ), f"strategic_spawning.{name} must re-export the sibling module's object"

    # Behaviour is preserved end-to-end through the re-export: the moved trim
    # still drops archived_gaps and bounds per-gap history to the keep window.
    pm = {
        "archived_gaps": [{"id": "old"}],
        "gaps": [
            {
                "id": "g",
                "history": [
                    {"e": i}
                    for i in range(strategic_problem_view._GAP_HISTORY_KEEP + 3)
                ],
            }
        ],
    }
    view = strategic_spawning._agent_view_problem_model(pm)
    assert "archived_gaps" not in view
    assert len(view["gaps"][0]["history"]) == strategic_problem_view._GAP_HISTORY_KEEP


def test_build_operator_overrides_summary_consolidates_both_ledgers():
    """Both reconciliation ledgers (vision + problem_model) are consolidated
    into one normalized list, newest-first, with document/value mapping."""
    from app.strategic_spawning import _build_operator_overrides_summary

    project = _make_project(
        vision={
            "_rejected_agent_edits": [
                {
                    "at": "2026-06-02T10:00:00Z",
                    "field": "vision.goal",
                    "kind": "value_conflict",
                    "user_value": "ship safe",
                    "agent_value": "ship fast",
                },
                {
                    "at": "2026-06-03T10:00:00Z",
                    "field": "running_update",
                    "kind": "running_update_superseded",
                    "agent_value": "my rewrite",
                },
            ]
        },
        problem_model={
            "rejected_agent_edits": [
                {
                    "at": "2026-06-01T10:00:00Z",
                    "field": "intent.purpose",
                    "kind": "value_conflict",
                    "operator_value": "refine",
                    "agent_value": "rewrite",
                },
            ]
        },
    )
    out = _build_operator_overrides_summary(project)

    # Newest-first ordering by `at`.
    assert [e["at"] for e in out] == [
        "2026-06-03T10:00:00Z",
        "2026-06-02T10:00:00Z",
        "2026-06-01T10:00:00Z",
    ]
    by_doc = {e["document"]: e for e in out}
    # running_update entry: no operator value (operator's win is the live body).
    assert by_doc["running_update"]["operator_value"] is None
    assert by_doc["running_update"]["agent_value"] == "my rewrite"
    # vision conflict maps user_value -> operator_value.
    assert by_doc["vision"]["operator_value"] == "ship safe"
    assert by_doc["vision"]["agent_value"] == "ship fast"
    # problem_model intent conflict -> document "intent", operator_value carried.
    assert by_doc["intent"]["operator_value"] == "refine"
    assert by_doc["intent"]["agent_value"] == "rewrite"


def test_build_operator_overrides_summary_empty_when_no_ledgers():
    """Returns [] when neither ledger carries entries."""
    from app.strategic_spawning import _build_operator_overrides_summary

    assert _build_operator_overrides_summary(_make_project()) == []
    assert (
        _build_operator_overrides_summary(
            _make_project(vision={"_rejected_agent_edits": []}, problem_model={})
        )
        == []
    )


def test_build_operator_overrides_summary_caps_and_truncates():
    """The summary respects the entry cap and char-bounds each value."""
    from app.strategic_spawning import (
        _build_operator_overrides_summary,
        _OPERATOR_OVERRIDES_MAX,
        _OVERRIDE_VALUE_MAXLEN,
    )

    ledger = [
        {
            "at": f"2026-06-{i:02d}T00:00:00Z",
            "field": "vision.g",
            "kind": "value_conflict",
            "user_value": "u",
            "agent_value": "x" * (_OVERRIDE_VALUE_MAXLEN + 50),
        }
        for i in range(1, _OPERATOR_OVERRIDES_MAX + 6)
    ]
    project = _make_project(vision={"_rejected_agent_edits": ledger})
    out = _build_operator_overrides_summary(project)

    assert len(out) == _OPERATOR_OVERRIDES_MAX
    # Truncated value carries the ellipsis marker and respects the cap.
    assert out[0]["agent_value"].endswith("…")
    assert len(out[0]["agent_value"]) == _OVERRIDE_VALUE_MAXLEN + 1


@pytest.mark.asyncio
async def test_design_doc_update_payload_omits_playbook_archive(db_session):
    """Integration: _spawn_design_doc_update must not ship playbook_archive."""
    project = _make_project(
        id=uuid.uuid4(),
        name="archive-strip-test",
        github_owner="test",
        github_repo="repo",
        autonomous_enabled=True,
        vision={"purpose": "test"},
        design_doc={
            "summary": "ok",
            "playbook_archive": [
                {"_archived_key": f"playbook/e{i}"} for i in range(100)
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_design_doc_update(project, db_session)
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    payload = tasks[0].payloads[0]
    # design_doc now ships as a compact navigation index (one entry per
    # non-internal section). playbook_archive is operator-only and excluded;
    # the top-level "summary" section is represented by its key.
    dd = payload["design_doc"]
    by_key = {e["key"]: e for e in dd["sections"]}
    assert "playbook_archive" not in by_key
    assert by_key["summary"]["summary"] == "ok"


@pytest.mark.asyncio
async def test_spawn_design_doc_update_payload_uses_reconciled_design_doc(
    db_session, mocker
):
    """Phase 1 payload ships the reconciled design doc built with GitHub context."""
    from app.strategic_spawning import _agent_view_design_doc

    verification_context = {
        "token": "gh-token",
        "owner": "acme",
        "repo": "studio",
        "branch": "automain",
        "project_id": "proj-123",
    }
    reconciled_design_doc = {
        "systems/api": {
            "status": "missing",
            "presence_warning": "reconciled against integration branch",
        }
    }
    project = _make_project(
        id=uuid.uuid4(),
        vision={"purpose": "test"},
        design_doc={
            "systems/api": {
                "status": "functional",
                "modules": ["backend/app/api.py"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.strategic_spawning.validate_internal_payload",
        side_effect=lambda p, _src: p,
    )
    mocker.patch(
        "app.strategic_spawning._build_shipped_changes_manifest",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.strategic_spawning.resolve_branch_verification_context",
        new=AsyncMock(return_value=verification_context),
    )
    reconcile_mock = mocker.patch(
        "app.strategic_spawning.ground_design_doc",
        new=AsyncMock(return_value=reconciled_design_doc),
    )
    mocker.patch(
        "app.strategic_spawning._verify_capability_backing_paths",
        new=AsyncMock(return_value={}),
    )

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    payload = tasks[0].payloads[0]
    # The reconciled status flows into the compact index, not a full inline doc.
    by_key = {e["key"]: e for e in payload["design_doc"]["sections"]}
    assert by_key["systems/api"]["status"] == "missing"
    assert (
        by_key["systems/api"]["presence_warning"]
        == "reconciled against integration branch"
    )
    reconcile_mock.assert_awaited_once_with(
        _agent_view_design_doc(project.design_doc), **verification_context
    )


@pytest.mark.asyncio
async def test_spawn_design_doc_update_payload_downgrades_symbol_absent_entry(
    db_session, mocker
):
    """A FILE-PRESENT but SYMBOL-ABSENT entry is downgraded to missing in the payload.

    Exercises the real symbol-aware ``ground_design_doc`` walker (NOT mocked):
    the backing file still exists on the branch (file-level pass leaves the entry
    alone), but the specifically-cited ``path:symbol`` was deleted, so the
    symbol-level pass downgrades the entry to ``missing`` with a presence_warning
    naming the absent symbol — the post-refactor facade case file-level grounding
    cannot see.
    """
    verification_context = {
        "token": "gh-token",
        "owner": "acme",
        "repo": "studio",
        "branch": "automain",
        "project_id": "proj-789",
    }
    project = _make_project(
        id=uuid.uuid4(),
        vision={"purpose": "test"},
        design_doc={
            "systems/api": {
                "status": "functional",
                "modules": ["backend/app/api.py"],
                "description": "Serves requests via backend/app/api.py:deleted_func.",
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.strategic_spawning.validate_internal_payload",
        side_effect=lambda p, _src: p,
    )
    mocker.patch(
        "app.strategic_spawning._build_shipped_changes_manifest",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.strategic_spawning.resolve_branch_verification_context",
        new=AsyncMock(return_value=verification_context),
    )
    mocker.patch(
        "app.strategic_spawning._verify_capability_backing_paths",
        new=AsyncMock(return_value={}),
    )
    # File still present on the branch → file-level pass does NOT downgrade.
    mocker.patch(
        "app.source_ref_validation._file_exists_in_repo_branch",
        new=AsyncMock(return_value=True),
    )
    # Cited symbol is unambiguously absent from the present file → symbol-level
    # pass downgrades the entry.
    mocker.patch(
        "app.source_ref_validation._symbol_defined_at_head",
        new=AsyncMock(return_value=False),
    )

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    by_key = {e["key"]: e for e in tasks[0].payloads[0]["design_doc"]["sections"]}
    entry = by_key["systems/api"]
    assert entry["status"] == "missing"
    assert "backend/app/api.py:deleted_func" in entry["presence_warning"]
    # The "[reconciled: ...]" note appended to the section's description is
    # surfaced through the bounded index summary.
    assert "[reconciled:" in entry["summary"]


@pytest.mark.asyncio
async def test_spawn_design_doc_update_symbol_indeterminate_leaves_entry(
    db_session, mocker
):
    """An indeterminate symbol result never downgrades a file-present entry."""
    verification_context = {
        "token": "gh-token",
        "owner": "acme",
        "repo": "studio",
        "branch": "automain",
        "project_id": "proj-790",
    }
    project = _make_project(
        id=uuid.uuid4(),
        vision={"purpose": "test"},
        design_doc={
            "systems/api": {
                "status": "functional",
                "modules": ["backend/app/api.py"],
                "description": "Serves requests via backend/app/api.py:maybe_func.",
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.strategic_spawning.validate_internal_payload",
        side_effect=lambda p, _src: p,
    )
    mocker.patch(
        "app.strategic_spawning._build_shipped_changes_manifest",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.strategic_spawning.resolve_branch_verification_context",
        new=AsyncMock(return_value=verification_context),
    )
    mocker.patch(
        "app.strategic_spawning._verify_capability_backing_paths",
        new=AsyncMock(return_value={}),
    )
    mocker.patch(
        "app.source_ref_validation._file_exists_in_repo_branch",
        new=AsyncMock(return_value=True),
    )
    # Indeterminate (None) and present (True) symbol results must NOT downgrade.
    mocker.patch(
        "app.source_ref_validation._symbol_defined_at_head",
        new=AsyncMock(return_value=None),
    )

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    by_key = {e["key"]: e for e in tasks[0].payloads[0]["design_doc"]["sections"]}
    entry = by_key["systems/api"]
    assert entry["status"] == "functional"
    assert "presence_warning" not in entry


@pytest.mark.asyncio
async def test_spawn_strategic_review_payload_uses_reconciled_design_doc(
    db_session, mocker
):
    """Phase 2 payload ships the reconciled design doc built with GitHub context."""
    from app.strategic_spawning import _agent_view_design_doc

    verification_context = {
        "token": "gh-token",
        "owner": "acme",
        "repo": "studio",
        "branch": "main",
        "project_id": "proj-456",
    }
    reconciled_design_doc = {
        "capabilities/search": {
            "status": "missing",
            "presence_warning": "reconciled against integration branch",
        }
    }
    project = _make_project(
        id=uuid.uuid4(),
        integration_branch="main",
        promotion_target="main",
        vision={"purpose": "test"},
        directions=[
            {
                "id": "d-reconcile",
                "status": "active",
                "text": "Verify strategic payload grounding",
                "priority": "medium",
            }
        ],
        design_doc={
            "capabilities/search": {
                "status": "working",
                "modules": ["backend/app/search.py"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.strategic_cycle.recent_for_agent_context",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.strategic_spawning._build_recent_interventions",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.strategic_spawning.resolve_branch_verification_context",
        new=AsyncMock(return_value=verification_context),
    )
    reconcile_mock = mocker.patch(
        "app.strategic_spawning.ground_design_doc",
        new=AsyncMock(return_value=reconciled_design_doc),
    )
    mocker.patch(
        "app.strategic_spawning._verify_capability_backing_paths",
        new=AsyncMock(return_value={}),
    )
    mocker.patch(
        "app.strategic_spawning._build_system_maturity_payload",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.drift_detection.compute_direction_drift",
        new=AsyncMock(return_value={}),
    )
    mocker.patch(
        "app.drift_detection.persist_drift_snapshot",
        new=AsyncMock(return_value=None),
    )

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert len(tasks) == 1
    payload = tasks[0].payloads[0]
    # The reconciled status flows into the compact index, not a full inline doc.
    by_key = {e["key"]: e for e in payload["design_doc"]["sections"]}
    assert by_key["capabilities/search"]["status"] == "missing"
    assert (
        by_key["capabilities/search"]["presence_warning"]
        == "reconciled against integration branch"
    )
    reconcile_mock.assert_awaited_once_with(
        _agent_view_design_doc(project.design_doc), **verification_context
    )


@pytest.mark.asyncio
async def test_strategic_review_payload_includes_calibration_factor(db_session):
    """prediction_calibration_factor and note are always present in strategic payload."""
    project = _make_project(
        vision={"purpose": "test", "_strategic_cycle_history": []},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    assert "prediction_calibration_factor" in payload
    assert "prediction_calibration_note" in payload
    assert isinstance(payload["prediction_calibration_factor"], float)
    assert isinstance(payload["prediction_calibration_note"], str)


@pytest.mark.asyncio
async def test_strategic_review_calibration_factor_reflects_history(db_session):
    """Factor < 1.0 when recent cycles consistently over-predicted merges."""
    directions = [
        {
            "id": f"d{i}",
            "status": "active",
            "text": f"direction {i}",
            "priority": "medium",
            "research_history": [{"prs_merged": 1}],
        }
        for i in range(3)
    ]
    cycles = [
        {
            "cycle_id": f"sc-{i}",
            "outcome": "completed",
            "created_direction_ids": [f"d{i}"],
            "created_predictions": {f"d{i}": {"similar_past_merges": 10}},
        }
        for i in range(3)
    ]
    project = _make_project(
        directions=directions,
        vision={"purpose": "test", "_strategic_cycle_history": cycles},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    factor = payload["prediction_calibration_factor"]
    assert factor < 1.0, (
        f"Expected factor < 1.0 for over-prediction history, got {factor}"
    )
    assert "optimistic" in payload["prediction_calibration_note"]


# ── Mode-aware additions: system_maturity + drift_signals ─────────────────────


@pytest.mark.asyncio
async def test_strategic_review_includes_system_maturity_when_rows_exist(db_session):
    """system_maturity payload field is present and non-empty when SystemMaturity
    rows exist; absent when no rows."""
    from app.models import SystemMaturity
    from app.time_utils import _utcnow

    project = _make_project(
        directions=[
            {
                "id": "d-sm01",
                "status": "active",
                "text": "x",
                "priority": "medium",
                "system_id": "sys-A",
            },
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        SystemMaturity(
            project_id=project.id,
            system_id="sys-A",
            score=72,
            components={"test_coverage": 0.5},
            recommended_mode="ship",
            top_issues=[{"description": "thin tests"}],
            last_audited_at=_utcnow(),
        )
    )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    assert "system_maturity" in payload
    assert isinstance(payload["system_maturity"], list)
    assert any(sm["system_id"] == "sys-A" for sm in payload["system_maturity"])
    sm_row = next(sm for sm in payload["system_maturity"] if sm["system_id"] == "sys-A")
    assert sm_row["score"] == 72
    assert sm_row["recommended_mode"] == "ship"


@pytest.mark.asyncio
async def test_strategic_review_omits_system_maturity_when_no_rows(db_session):
    """Without SystemMaturity rows, the system_maturity field is omitted from the payload."""
    project = _make_project(
        directions=[
            {
                "id": "d-sm02",
                "status": "active",
                "text": "x",
                "priority": "medium",
                "system_id": "sys-B",
            },
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    assert "system_maturity" not in payload, (
        f"system_maturity should not appear when no rows; payload keys: {sorted(payload.keys())}"
    )


@pytest.mark.asyncio
async def test_strategic_review_includes_drift_signals_when_drift_present(db_session):
    """drift_signals payload field is present when at least one direction has
    flagged drift; the field shape includes by_system and generated_at."""
    from app.models import Task as _Task, TaskStatus as _TaskStatus

    project = _make_project(
        directions=[
            {
                "id": "d-drift01",
                "status": "active",
                "text": "x",
                "priority": "medium",
                "system_id": "sys-C",
            },
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create 5 completed claim-tasks with no PR artifacts → stalled_despite_churn
    for i in range(5):
        db_session.add(
            _Task(
                project_id=project.id,
                title=f"claim {i}",
                description="",
                status=_TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[
                    {
                        "type": "create",
                        "prompt": "x",
                        "current_direction_id": "d-drift01",
                    }
                ],
                context={"direction_id": "d-drift01"},
                plan_output={"summary": "shipped"},
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            )
        )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]
    assert "drift_signals" in payload
    drift = payload["drift_signals"]
    assert "by_system" in drift
    assert "generated_at" in drift


@pytest.mark.asyncio
async def test_drift_signals_persist_snapshot_and_include_consecutive_cycles(
    db_session,
):
    """_spawn_strategic_review persists a drift snapshot and enriches the payload
    with consecutive_flagged_cycles when prior flagged snapshots exist."""
    from datetime import timedelta
    from app.drift_detection import DirectionDriftSnapshot
    from app.time_utils import _utcnow

    project = _make_project(
        directions=[
            {
                "id": "d-trend01",
                "status": "active",
                "text": "x",
                "priority": "medium",
                "system_id": "sys-T",
            },
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed two prior flagged snapshots to simulate previous cycles.
    now = _utcnow()
    for i in range(2, 0, -1):
        db_session.add(
            DirectionDriftSnapshot(
                project_id=project.id,
                direction_id="d-trend01",
                snapshot_at=now - timedelta(hours=i * 6),
                claimed_changes_count=5,
                actually_merged_prs=0,
                claim_shipped_consistency=0.0,
                over_claim_flag=False,
                stalled_despite_churn_flag=True,
            )
        )
    await db_session.commit()

    # Create 5 completed claim-tasks with unmerged PR artifacts.
    for i in range(5):
        task = Task(
            project_id=project.id,
            title=f"trend claim {i}",
            description="",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            payloads=[
                {
                    "type": "create",
                    "prompt": "x",
                    "current_direction_id": "d-trend01",
                }
            ],
            context={"direction_id": "d-trend01"},
            plan_output={"summary": "shipped"},
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
        db_session.add(task)
        await db_session.flush()
        db_session.add(
            Artifact(
                task_id=task.id,
                artifact_type=ArtifactType.PULL_REQUEST,
                pr_url=f"https://github.com/test/repo/pull/{task.id.hex[:8]}",
            )
        )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    sr_tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert sr_tasks, "No strategic_review task spawned"
    payload = sr_tasks[0].payloads[0]
    assert "drift_signals" in payload, (
        f"drift_signals missing; keys: {sorted(payload.keys())}"
    )
    drift = payload["drift_signals"]
    assert "d-trend01" in drift.get("by_direction", {}), (
        f"d-trend01 not in by_direction: {list(drift.get('by_direction', {}).keys())}"
    )
    sig = drift["by_direction"]["d-trend01"]
    # 2 prior flagged + 1 current (written by persist_drift_snapshot) = 3 consecutive
    assert "consecutive_flagged_cycles" in sig, (
        f"consecutive_flagged_cycles missing from sig: {sig}"
    )
    assert sig["consecutive_flagged_cycles"] == 3

    # Verify snapshot was persisted to DB (one new row for the current cycle).
    from sqlmodel import select as _select

    snap_result = await db_session.execute(
        _select(DirectionDriftSnapshot).where(
            DirectionDriftSnapshot.project_id == project.id,
            DirectionDriftSnapshot.direction_id == "d-trend01",
        )
    )
    snaps = snap_result.scalars().all()
    assert len(snaps) == 3, (
        f"Expected 3 snapshots (2 seeded + 1 persisted), got {len(snaps)}"
    )


@pytest.mark.asyncio
async def test_strategic_review_payload_documents_recommended_mode(db_session):
    """The optional `recommended_mode` field is part of the StrategicReviewPayload
    schema (validated via validate_internal_payload). Absence in a typical
    payload should not cause validation failures."""
    from app.payload_schemas import StrategicReviewPayload

    fields = StrategicReviewPayload.model_fields
    assert "recommended_mode" in fields, (
        "recommended_mode should be part of StrategicReviewPayload schema"
    )
    # And: the existing strategic_review payload still validates.
    project = _make_project(
        directions=[
            {"id": "d-doc01", "status": "active", "text": "x", "priority": "medium"},
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True


# ---------------------------------------------------------------------------
# efficiency_snapshot in strategic payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategic_review_includes_efficiency_snapshot_when_data_present(
    db_session,
):
    """efficiency_snapshot is present in the payload when baseline data exists."""
    from unittest.mock import AsyncMock, patch

    project = _make_project(
        directions=[
            {"id": "d-eff01", "status": "active", "text": "x", "priority": "medium"}
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _sample_snapshot = {
        "axes": {
            "cost_per_merged_pr": {
                "current": 1.5,
                "baseline": 2.0,
                "drift_pct": -25.0,
                "history": [],
            },
            "vm_reuse_rate": {
                "current": 0.6,
                "baseline": 0.5,
                "drift_pct": 20.0,
                "history": [],
            },
            "vm_cold_start_p50_ms": {
                "current": 500.0,
                "baseline": 600.0,
                "drift_pct": -16.67,
                "history": [],
            },
            "median_api_calls_per_task": {
                "current": 40.0,
                "baseline": 50.0,
                "drift_pct": -20.0,
                "history": [],
            },
            "median_review_iterations": {
                "current": 1.5,
                "baseline": 2.0,
                "drift_pct": -25.0,
                "history": [],
            },
        },
        "baseline_captured_at": "2026-04-05T00:00:00",
        "last_snapshot_at": "2026-05-01T00:00:00",
    }

    with patch(
        "app.efficiency_snapshot.read_baseline_with_drift",
        new=AsyncMock(return_value=_sample_snapshot),
    ):
        spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]
    assert "efficiency_snapshot" in payload
    snap = payload["efficiency_snapshot"]
    assert "axes" in snap
    assert "cost_per_merged_pr" in snap["axes"]
    assert snap["axes"]["cost_per_merged_pr"]["drift_pct"] == -25.0


@pytest.mark.asyncio
async def test_strategic_review_omits_efficiency_snapshot_when_no_data(db_session):
    """efficiency_snapshot is omitted when all current axis values are None."""
    from unittest.mock import AsyncMock, patch

    project = _make_project(
        directions=[
            {"id": "d-eff02", "status": "active", "text": "x", "priority": "medium"}
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _empty_snapshot = {
        "axes": {
            "cost_per_merged_pr": {
                "current": None,
                "baseline": None,
                "drift_pct": None,
                "history": [],
            },
            "vm_reuse_rate": {
                "current": None,
                "baseline": None,
                "drift_pct": None,
                "history": [],
            },
            "vm_cold_start_p50_ms": {
                "current": None,
                "baseline": None,
                "drift_pct": None,
                "history": [],
            },
            "median_api_calls_per_task": {
                "current": None,
                "baseline": None,
                "drift_pct": None,
                "history": [],
            },
            "median_review_iterations": {
                "current": None,
                "baseline": None,
                "drift_pct": None,
                "history": [],
            },
        },
        "baseline_captured_at": None,
        "last_snapshot_at": None,
    }

    with patch(
        "app.efficiency_snapshot.read_baseline_with_drift",
        new=AsyncMock(return_value=_empty_snapshot),
    ):
        spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]
    assert "efficiency_snapshot" not in payload


# ---------------------------------------------------------------------------
# parallelism_snapshot removed from strategic payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategic_review_omits_parallelism_snapshot(db_session):
    """parallelism_snapshot is no longer built or shipped in the strategic payload.

    It was a Blueprint VM-pool infra metric that does not generalize to an
    arbitrary target repo. Seeds VM-timing rows that would previously have
    populated the snapshot and confirms neither the payload key nor the
    prompt_suffix observation note appears, and that it is gone from the schema.
    """
    project = _make_project(
        directions=[
            {"id": "d-par01", "status": "active", "text": "x", "priority": "medium"}
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    from app.time_utils import _utcnow

    now = _utcnow()
    active_task = Task(
        project_id=project.id,
        title="active",
        description="",
        payloads=[{"type": "research"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_started_at=now - timedelta(minutes=30),
        context={"direction_id": "d-par01"},
    )
    db_session.add(active_task)
    await db_session.commit()

    prev_cap = _shared_state.runtime_config.get("max_concurrent_vms")
    _shared_state.runtime_config["max_concurrent_vms"] = 15
    try:
        spawned = await _spawn_strategic_review(project, db_session)
    finally:
        if prev_cap is None:
            _shared_state.runtime_config.pop("max_concurrent_vms", None)
        else:
            _shared_state.runtime_config["max_concurrent_vms"] = prev_cap

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    strategic_tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert strategic_tasks
    payload = strategic_tasks[0].payloads[0]
    assert "parallelism_snapshot" not in payload
    suffix = payload.get("prompt_suffix") or ""
    assert "throughput depends on" not in suffix
    assert "pool capacity" not in suffix

    # Gone from the pack schema too.
    from app.strategic_spawning import _STRATEGIC_REVIEW_SCHEMA

    assert "parallelism_snapshot" not in _STRATEGIC_REVIEW_SCHEMA


# ---------------------------------------------------------------------------
# held_directions block in strategic payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategic_review_includes_held_directions_block(db_session):
    """When a direction is HELD, the strategic_review payload carries a
    read-only held_directions block: the direction text, consecutive-failure
    count, dominant error fingerprint, and a few RAW recent failure messages.
    """
    from app.shared_state import record_direction_result, DIRECTION_HOLD_THRESHOLD

    _shared_state.direction_failure_streaks.clear()
    direction_id = "d-held01"
    project = _make_project(
        directions=[
            {
                "id": direction_id,
                "status": "active",
                "text": "Decompose the giant module",
                "priority": "medium",
            }
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed three FAILED tasks on the direction so the block can surface raw
    # failure messages + a dominant fingerprint.
    for i in range(3):
        db_session.add(
            Task(
                project_id=project.id,
                title=f"failed-{i}",
                description="",
                payloads=[{"type": "create"}],
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                status=TaskStatus.FAILED,
                created_by="ORCHESTRATOR",
                latest_status_message=f"ImportError: cannot import name foo (attempt {i})",
                context={"direction_id": direction_id},
            )
        )
    await db_session.commit()

    # Drive the direction into the HELD state.
    try:
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(
                direction_id,
                success=False,
                failure_class="CODE_ERROR",
                project_id=str(project.id),
            )
        assert _shared_state.get_held_directions().get(direction_id)

        spawned = await _spawn_strategic_review(project, db_session)
    finally:
        _shared_state.direction_failure_streaks.clear()

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    strategic_tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert strategic_tasks
    payload = strategic_tasks[0].payloads[0]
    assert "held_directions" in payload
    held = payload["held_directions"]
    assert isinstance(held, list) and len(held) == 1
    entry = held[0]
    assert entry["direction_id"] == direction_id
    assert "Decompose the giant module" in entry["direction_text"]
    assert entry["consecutive_failures"] == DIRECTION_HOLD_THRESHOLD
    assert entry["last_failure_class"] == "CODE_ERROR"
    assert entry["dominant_error_fingerprint"]
    msgs = entry["recent_failure_messages"]
    assert isinstance(msgs, list) and msgs
    assert any("ImportError" in m for m in msgs)


@pytest.mark.asyncio
async def test_strategic_review_omits_held_directions_when_none_held(db_session):
    """When no direction is held, the held_directions block is absent."""
    _shared_state.direction_failure_streaks.clear()
    project = _make_project(
        directions=[
            {"id": "d-ok01", "status": "active", "text": "x", "priority": "medium"}
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    try:
        spawned = await _spawn_strategic_review(project, db_session)
    finally:
        _shared_state.direction_failure_streaks.clear()

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    strategic_tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert strategic_tasks
    payload = strategic_tasks[0].payloads[0]
    assert "held_directions" not in payload


@pytest.mark.asyncio
async def test_strategic_review_skips_query_when_held_directions_filtered(db_session):
    """When held directions exist but none belong to the current project,
    the held_directions block is empty and the recent FAILED tasks query
    is not executed (avoiding wasted DB work).
    """
    from app.shared_state import record_direction_result, DIRECTION_HOLD_THRESHOLD

    _shared_state.direction_failure_streaks.clear()
    direction_id_held = "d-held02"
    other_project_id = str(uuid.uuid4())
    project = _make_project(
        directions=[
            {"id": "d-active01", "status": "active", "text": "Active dir", "priority": "medium"}
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Record a held direction in shared state with a DIFFERENT project_id
    # (simulating a held direction from another project).
    try:
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(
                direction_id_held,
                success=False,
                failure_class="CODE_ERROR",
                project_id=other_project_id,
            )
        assert _shared_state.get_held_directions().get(direction_id_held)

        spawned = await _spawn_strategic_review(project, db_session)
        assert spawned is True

        result = await db_session.execute(_select_agent_tasks(project.id))
        strategic_tasks = [
            t
            for t in result.scalars().all()
            if t.first_payload.get("type") == "strategic_review"
        ]
        assert strategic_tasks
        payload = strategic_tasks[0].payloads[0]
        # The held_directions block should be empty or absent since the held
        # direction belongs to a different project.
        held = payload.get("held_directions", [])
        assert isinstance(held, list) and len(held) == 0
    finally:
        _shared_state.direction_failure_streaks.clear()


# ---------------------------------------------------------------------------
# _build_failure_feedback_mitigation unit tests
# ---------------------------------------------------------------------------


def test_failure_feedback_downscopes_large_to_medium():
    """3 first-attempt failures in a category downscopes large→medium."""
    from app.strategic_spawning import _build_failure_feedback_mitigation

    direction = {"id": "d-abc123"}
    design_doc = {
        "direction_first_attempt_feedback": {
            "d-abc123": {
                "code_defect": 3,
                "total": 3,
                "last_updated": "2026-04-20T00:00:00",
            },
        }
    }
    scope, note, evidence = _build_failure_feedback_mitigation(
        direction, "large", "create", design_doc
    )
    assert scope == "medium"
    assert note is not None and "code defect" in note
    assert evidence["failure_downscope"]["count"] == 3
    assert evidence["failure_downscope"]["from"] == "large"
    assert evidence["failure_downscope"]["to"] == "medium"


def test_failure_feedback_downscopes_medium_to_small():
    """3+ first-attempt failures downscopes medium→small and names the category."""
    from app.strategic_spawning import _build_failure_feedback_mitigation

    direction = {"id": "d-def456"}
    design_doc = {
        "direction_first_attempt_feedback": {
            "d-def456": {
                "testing": 5,
                "scope_related": 1,
                "total": 6,
                "last_updated": "2026-04-20T00:00:00",
            },
        }
    }
    scope, note, evidence = _build_failure_feedback_mitigation(
        direction, "medium", "create", design_doc
    )
    assert scope == "small"
    assert note is not None and "testing" in note
    assert evidence["failure_downscope"]["category"] == "testing"


def test_failure_feedback_no_downscope_below_threshold():
    """2 failures (< threshold) leaves scope and note unchanged."""
    from app.strategic_spawning import _build_failure_feedback_mitigation

    direction = {"id": "d-ghi789"}
    design_doc = {
        "direction_first_attempt_feedback": {
            "d-ghi789": {
                "code_defect": 2,
                "total": 2,
                "last_updated": "2026-04-20T00:00:00",
            },
        }
    }
    scope, note, evidence = _build_failure_feedback_mitigation(
        direction, "medium", "create", design_doc
    )
    assert scope == "medium"
    assert note is None
    assert "failure_downscope" not in evidence


def test_success_pattern_biases_medium_to_small():
    """playbook/success-create-small with 3+ successes biases medium→small."""
    from app.strategic_spawning import _build_failure_feedback_mitigation

    direction = {"id": "d-jkl012"}
    design_doc = {
        "playbook/success-create-small": {
            "source": "success-pattern",
            "success_count": 4,
        },
    }
    scope, _note, evidence = _build_failure_feedback_mitigation(
        direction, "medium", "create", design_doc
    )
    assert scope == "small"
    assert evidence["success_bias"]["success_count"] == 4
    assert evidence["success_bias"]["to"] == "small"


def test_success_pattern_insufficient_count_no_bias():
    """2 successes (< threshold) leaves scope unchanged."""
    from app.strategic_spawning import _build_failure_feedback_mitigation

    direction = {"id": "d-mno345"}
    design_doc = {
        "playbook/success-create-small": {
            "source": "success-pattern",
            "success_count": 2,
        },
    }
    scope, _note, evidence = _build_failure_feedback_mitigation(
        direction, "medium", "create", design_doc
    )
    assert scope == "medium"
    assert "success_bias" not in evidence


def test_no_data_returns_proposed_scope_unchanged():
    """Empty design_doc leaves scope and note unchanged."""
    from app.strategic_spawning import _build_failure_feedback_mitigation

    scope, note, evidence = _build_failure_feedback_mitigation(
        {"id": "d-xyz"}, "medium", "create", {}
    )
    assert scope == "medium"
    assert note is None
    assert evidence["proposed_scope"] == "medium"
    assert evidence["final_scope"] == "medium"


# ── pack_under_budget integration ─────────────────────────────────────────────


def test_pack_under_budget_keeps_version_phase_floor_under_pressure():
    """Lifecycle context must never disappear just because sibling context is
    bloated. A compact version_phase floor should preserve the current goal,
    while _packer_meta records the degradation so the agent can fetch
    full lifecycle detail if needed."""

    large_finding = {
        "severity": "high",
        "summary": "Nested audit detail that should not be required inline.",
        "details": "audit-finding-body " * 300,
        "evidence": ["evidence " * 200 for _ in range(6)],
    }
    version_phase = {
        "version_number": 21,
        "phase": "build",
        "goal": "Ship the post-promotion observability milestone.",
        "tasks_merged_this_version": 9,
        "last_qualification": {
            "status": "not_promoted",
            "confidence": 0.74,
            "blocker": "audit_findings",
            "audit_findings": [large_finding for _ in range(30)],
        },
        "recent_versions": [
            {
                "version_number": 20 - i,
                "status": "promoted",
                "tasks_merged_this_version": 4 + i,
                "last_qualification": {
                    "status": "promoted",
                    "confidence": 0.91 - (i * 0.01),
                    "blocker": None,
                    "audit_findings": [large_finding for _ in range(12)],
                },
                "acceptance_aggregate": {
                    "merged": 4 + i,
                    "proven": 3 + i,
                    "proven_rate": 0.75,
                },
            }
            for i in range(5)
        ],
        "acceptance_aggregate": {"merged": 9, "proven": 7, "proven_rate": 0.7778},
    }
    payload = {
        "type": "strategic_review",
        "research_mode": "strategic",
        "project_name": "Blueprint",
        "base_branch": "automain",
        "is_self_repo": True,
        "enable_self_improvement": True,
        "vision": {"purpose": "test lifecycle floor"},
        "direction_counts": {"active": 18, "paused": 0, "total": 18, "max_active": 20},
        "max_active_directions": 20,
        "version_phase": version_phase,
        "directions_summary": [
            {
                "id": f"d-vp-{i}",
                "status": "active",
                "priority": "medium",
                "text": "large direction " * 80,
                "research_context": "direction context " * 400,
            }
            for i in range(30)
        ],
        "conversation_thread": [
            {"role": "user", "content": "conversation bulk " * 300}
            for _ in range(20)
        ],
    }

    packed = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA, budget=6000)

    assert "version_phase" in packed, (
        f"version_phase must floor instead of dropping; meta={packed['_packer_meta']}"
    )
    vp = packed["version_phase"]
    assert vp["version_number"] == 21
    assert vp["phase"] == "build"
    assert vp["goal"] == "Ship the post-promotion observability milestone."

    meta = packed["_packer_meta"]
    dropped_keys = {entry["key"] for entry in meta["dropped"]}
    trimmed = [entry for entry in meta["trimmed"] if entry["key"] == "version_phase"]
    assert "version_phase" not in dropped_keys, meta
    assert trimmed, f"version_phase degradation must be visible in _packer_meta: {meta}"
    assert trimmed[0]["kind"] in {"summarized", "floored"}


@pytest.mark.asyncio
async def test_strategic_review_packer_meta_present_on_small_project(db_session):
    """Small project: packer ships everything cleanly, meta lists no trims."""
    project = _make_project(
        directions=[
            {
                "id": "d-small1",
                "status": "active",
                "text": "Small direction",
                "priority": "medium",
                "research_history": [{"prs_merged": 1}],
            }
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "_packer_meta" in payload, "Packer must always attach _packer_meta"
    meta = payload["_packer_meta"]
    assert meta["trimmed"] == [], (
        f"Small project should have no trims: {meta['trimmed']}"
    )
    assert meta["dropped"] == [], (
        f"Small project should have no drops: {meta['dropped']}"
    )
    assert meta["budget_used"] > 0
    assert meta["budget_used"] < meta["budget_total"]


@pytest.mark.asyncio
async def test_strategic_review_packer_keeps_payload_under_cap_on_bloated_project(
    db_session,
):
    """Bloated project (50 directions, heavy history): packer must keep payload
    under the 256 KB validate_internal_payload hard cap and surface trims/drops
    so the agent can fetch full state via the ./fetch tool."""
    import json as _json
    from app.payload_schemas import _MAX_PAYLOAD_BYTES

    # Synthesize 50 directions, each with bulky research_history
    directions = [
        {
            "id": f"d-bloat{i:02d}",
            "status": "active",
            "text": f"Direction {i} " + ("filler text " * 30),
            "priority": "medium",
            "research_history": [
                {
                    "prs_merged": 1,
                    "prs_rejected": 0,
                    "tasks_failed": 0,
                    "tasks_created": 1,
                    "key_finding_descriptions": [
                        f"finding {j} " * 20 for j in range(5)
                    ],
                    "failed_task_details": [],
                    "timestamp": "2026-04-01T00:00:00Z",
                }
                for _ in range(8)
            ],
            "research_context": "context narrative " * 200,
        }
        for i in range(50)
    ]
    project = _make_project(
        directions=directions,
        vision={"purpose": "bloated project test"},
        problem_model={
            "intent": {"purpose": "test", "current_focus": "x"},
            "gaps": [
                {
                    "id": f"g{i}",
                    "name": f"gap {i}",
                    "description": "x" * 500,
                    "history": [f"h{j}" * 10 for j in range(20)],
                    "past_moves": [f"m{j}" * 10 for j in range(20)],
                    "evidence": [f"e{j}" * 10 for j in range(20)],
                }
                for i in range(20)
            ],
            "archived_gaps": [{"id": f"ag{i}", "name": "archived"} for i in range(40)],
            "friction": [],
            "capabilities": [],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    # Hard requirement: must fit under the 256KB cap that crashed autonomy before
    serialized_size = len(_json.dumps(payload, default=str).encode())
    assert serialized_size < _MAX_PAYLOAD_BYTES, (
        f"Packer failed to keep payload under cap: {serialized_size}B >= {_MAX_PAYLOAD_BYTES}B"
    )

    meta = payload["_packer_meta"]
    # On a 50-direction bloated project, directions_summary should have been
    # trimmed (cap is 12). archived_gaps drops via _agent_view_problem_model
    # before pack, so they don't appear in meta — the trim happened upstream.
    assert meta["budget_used"] <= meta["budget_total"]
    assert len(payload["directions_summary"]) <= 12, (
        f"Expected ≤12 directions after trim, got {len(payload['directions_summary'])}"
    )
    # The trim must be visible to the agent so it knows to fetch the rest.
    trimmed_keys = {entry["key"] for entry in meta["trimmed"]}
    assert "directions_summary" in trimmed_keys, (
        f"Trimmed list must include directions_summary; meta={meta}"
    )

    # Trim-proof anchor: even though directions_summary was trimmed to <=12, the
    # authoritative direction_counts + max ceiling must carry the TRUE active
    # count (50) so the agent views never under-report or render false-empty.
    assert "direction_counts" in payload, (
        "Authoritative direction_counts must survive budget pressure"
    )
    assert payload["direction_counts"]["active"] == 50, (
        f"direction_counts.active must be the true pre-trim count, got "
        f"{payload['direction_counts']['active']}"
    )
    assert payload["direction_counts"]["total"] == 50
    assert "max_active_directions" in payload, (
        "max_active ceiling must reach the agent (no hardcoded fallback)"
    )
    assert payload["direction_counts"]["max_active"] == payload["max_active_directions"]
    # Neither anchor key was trimmed or dropped — they are critical-tier.
    dropped_keys = {entry["key"] for entry in meta["dropped"]}
    assert "direction_counts" not in trimmed_keys | dropped_keys
    assert "max_active_directions" not in trimmed_keys | dropped_keys


@pytest.mark.asyncio
async def test_direction_counts_by_system_survives_directions_summary_trim(db_session):
    """The authoritative per-system ACTIVE map (direction_counts.by_system) is
    computed from the FULL direction set before packing, so it stays correct and
    complete even when directions_summary is trimmed under byte-budget pressure.
    This is what keeps the agent's per-system coverage-gap and concentration
    signals truthful when the per-direction detail list is trimmed."""
    import json as _json
    from app.payload_schemas import _MAX_PAYLOAD_BYTES

    # 50 active directions spread across 3 systems (plus a few with no system_id
    # which must NOT contribute to the map), all bulky enough to force a trim.
    systems = ["sys-a", "sys-b", "sys-c"]
    directions = []
    for i in range(50):
        d = {
            "id": f"d-sys{i:02d}",
            "status": "active",
            "text": f"Direction {i} " + ("filler text " * 30),
            "priority": "medium",
            "research_history": [
                {
                    "prs_merged": 1,
                    "prs_rejected": 0,
                    "tasks_failed": 0,
                    "tasks_created": 1,
                    "key_finding_descriptions": [
                        f"finding {j} " * 20 for j in range(5)
                    ],
                    "failed_task_details": [],
                    "timestamp": "2026-04-01T00:00:00Z",
                }
                for _ in range(8)
            ],
            "research_context": "context narrative " * 200,
        }
        # Last 5 directions intentionally carry no system_id.
        if i < 45:
            d["system_id"] = systems[i % 3]
        directions.append(d)

    # Expected per-system active tally from the FULL set: 45 directions round
    # -robin across 3 systems → 15 each; the 5 system-less ones contribute 0.
    expected_by_system = {"sys-a": 15, "sys-b": 15, "sys-c": 15}

    project = _make_project(
        directions=directions,
        vision={"purpose": "per-system trim-proofing test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    # Payload must fit under the hard cap and the detail list must have trimmed.
    serialized_size = len(_json.dumps(payload, default=str).encode())
    assert serialized_size < _MAX_PAYLOAD_BYTES
    assert len(payload["directions_summary"]) <= 12, (
        "directions_summary must be trimmed for this test to be meaningful"
    )
    meta = payload["_packer_meta"]
    trimmed_keys = {entry["key"] for entry in meta["trimmed"]}
    dropped_keys = {entry["key"] for entry in meta["dropped"]}
    assert "directions_summary" in trimmed_keys

    # The per-system map is nested under the critical direction_counts field and
    # reflects the FULL active set (15/15/15), NOT the ≤12 trimmed shipped rows.
    by_system = payload["direction_counts"]["by_system"]
    assert by_system == expected_by_system, (
        f"by_system must be the true pre-trim per-system tally, got {by_system}"
    )
    assert sum(by_system.values()) == 45, (
        "system-less directions must not contribute to the per-system map"
    )
    # Inherits direction_counts' critical tier — never trimmed or dropped.
    assert "direction_counts" not in trimmed_keys | dropped_keys


@pytest.mark.asyncio
async def test_direction_counts_survive_problem_model_floor_pressure(db_session):
    """Authoritative direction_counts survive even when a huge floor-reserved
    problem_model drops directions_summary entirely.

    The problem_model has a packer floor reservation; under a tiny byte budget it
    consumes the budget and directions_summary is dropped wholesale. The critical
    direction_counts + max_active_directions anchor must still ship verbatim."""
    schema = _STRATEGIC_REVIEW_SCHEMA
    raw = {
        "type": "strategic_review",
        "research_mode": "strategic",
        "project_name": "P",
        "base_branch": "main",
        "is_self_repo": False,
        "enable_self_improvement": False,
        "vision": {"purpose": "x"},
        "problem_model": {"gaps": [{"id": f"g{i}", "d": "y" * 200} for i in range(40)]},
        "direction_counts": {"active": 7, "paused": 2, "total": 9, "max_active": 15},
        "max_active_directions": 15,
        "directions_summary": [
            {"id": f"d{i}", "status": "active", "text": "z" * 200} for i in range(20)
        ],
    }
    # Tiny budget: forces directions_summary to drop, problem_model to degrade.
    packed = pack_under_budget(raw, schema, budget=2000)
    assert packed["direction_counts"]["active"] == 7, (
        "direction_counts must ship verbatim under maximum budget pressure"
    )
    assert packed["is_self_repo"] is False
    assert packed["max_active_directions"] == 15
    meta = packed["_packer_meta"]
    affected = {e["key"] for e in meta["trimmed"]} | {e["key"] for e in meta["dropped"]}
    assert "direction_counts" not in affected
    assert "max_active_directions" not in affected
    # The detail list is legitimately droppable; only the counts are trim-proof.
    assert "directions_summary" not in packed or len(packed["directions_summary"]) < 20


def test_directions_summary_summarization_compacts_bulky_nested_context():
    """When directions_summary must summarize, bulky research_context and
    recent_findings_evidence should compact per entry instead of causing the
    whole direction summary to ship multi-KB rows or disappear."""

    payload = {
        "type": "strategic_review",
        "research_mode": "strategic",
        "project_name": "Blueprint",
        "base_branch": "main",
        "is_self_repo": False,
        "enable_self_improvement": False,
        "vision": {"purpose": "test direction compacting"},
        "direction_counts": {"active": 24, "paused": 0, "total": 24, "max_active": 30},
        "max_active_directions": 30,
        "directions_summary": [
            {
                "id": f"d-heavy-{i:02d}",
                "status": "active",
                "priority": "high" if i < 3 else "medium",
                "text": "direction summary text " * 10,
                "total_cycles": 5,
                "prs_merged": i,
                "verified_prs_merged": i,
                "tasks_failed": 0,
                "research_context": "research-context-body " * 300,
                "recent_findings_evidence": [
                    {
                        "finding_id": f"f-{i}-{j}",
                        "summary": "finding summary " * 80,
                        "evidence": "finding evidence body " * 180,
                        "source": {"task_id": f"task-{i}-{j}", "logs": "log " * 250},
                    }
                    for j in range(4)
                ],
            }
            for i in range(24)
        ],
    }

    packed = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA, budget=30000)

    assert "directions_summary" in packed, (
        f"directions_summary should compact, not drop; meta={packed['_packer_meta']}"
    )
    assert 0 < len(packed["directions_summary"]) <= 12
    for entry in packed["directions_summary"]:
        entry_bytes = len(json.dumps(entry, default=str).encode())
        assert entry_bytes < 2000, (
            f"direction summary entry stayed too bulky ({entry_bytes}B): {entry}"
        )
        if "research_context" in entry:
            assert len(entry["research_context"]) < 1000
        if "recent_findings_evidence" in entry:
            evidence_bytes = len(
                json.dumps(entry["recent_findings_evidence"], default=str).encode()
            )
            assert evidence_bytes < 1000

    serialized = json.dumps(packed["directions_summary"], default=str)
    assert "research-context-body " * 80 not in serialized
    assert "finding evidence body " * 80 not in serialized


@pytest.mark.asyncio
async def test_drift_signals_survives_budget_pressure(db_session):
    """drift_signals must not be dropped when low-priority fields are evicted.

    Constructs a bloated project that forces the packer to drop low-priority
    fields, then asserts drift_signals (now medium priority) is still present
    while at least one low-priority field is absent from the payload.
    """
    import json as _json
    from app.payload_schemas import _MAX_PAYLOAD_BYTES

    # 50 bulky directions to force budget pressure
    directions = [
        {
            "id": f"d-dp{i:02d}",
            "status": "active",
            "text": f"Direction {i} " + ("pressure filler " * 40),
            "priority": "medium",
            "system_id": "sys-dp",
            "research_history": [
                {
                    "prs_merged": 1,
                    "prs_rejected": 0,
                    "tasks_failed": 0,
                    "tasks_created": 1,
                    "key_finding_descriptions": [
                        f"finding {j} " * 20 for j in range(5)
                    ],
                    "failed_task_details": [],
                    "timestamp": "2026-04-01T00:00:00Z",
                }
                for _ in range(8)
            ],
            "research_context": "context narrative " * 200,
        }
        for i in range(50)
    ]
    project = _make_project(
        directions=directions,
        vision={"purpose": "budget pressure test"},
        problem_model={
            "intent": {"purpose": "test", "current_focus": "x"},
            "gaps": [
                {
                    "id": f"g{i}",
                    "name": f"gap {i}",
                    "description": "x" * 500,
                    "history": [f"h{j}" * 10 for j in range(20)],
                    "past_moves": [f"m{j}" * 10 for j in range(20)],
                    "evidence": [f"e{j}" * 10 for j in range(20)],
                }
                for i in range(20)
            ],
            "archived_gaps": [{"id": f"ag{i}", "name": "archived"} for i in range(40)],
            "friction": [],
            "capabilities": [],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    # Add stalled tasks so drift_signals is non-empty and included in the payload
    for i in range(5):
        db_session.add(
            Task(
                project_id=project.id,
                title=f"drift claim {i}",
                description="",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[
                    {"type": "create", "prompt": "x", "current_direction_id": "d-dp00"}
                ],
                context={"direction_id": "d-dp00"},
                plan_output={"summary": "shipped"},
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            )
        )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    # Payload must stay under hard cap
    serialized_size = len(_json.dumps(payload, default=str).encode())
    assert serialized_size < _MAX_PAYLOAD_BYTES

    meta = payload["_packer_meta"]
    dropped_keys = {entry["key"] for entry in meta.get("dropped", [])}

    # drift_signals (medium priority) must survive budget pressure
    assert "drift_signals" in payload, (
        f"drift_signals was dropped under budget pressure; dropped={dropped_keys}"
    )

    # Confirm budget pressure existed (something was trimmed or dropped)
    trimmed_keys = {entry["key"] for entry in meta.get("trimmed", [])}
    assert dropped_keys or trimmed_keys, (
        f"Expected budget pressure (trims or drops); meta={meta}"
    )

    # drift_signals must not be among the dropped fields
    assert "drift_signals" not in dropped_keys, (
        f"drift_signals should not be dropped; dropped={dropped_keys}"
    )


# ── version_phase: promotion_readiness + recent_versions enrichment ───────────


async def _ensure_verdict_table(session):
    """Create qualificationverdict table idempotently (mirrors test_release_qualifier pattern)."""
    from app.models import QualificationVerdict as _QV

    conn = await session.connection()
    await conn.run_sync(lambda c: _QV.__table__.create(c, checkfirst=True))
    await session.commit()


async def _ensure_releaseversion_table(session):
    """Create releaseversion table idempotently."""
    from app.models import ReleaseVersion as _RV

    conn = await session.connection()
    await conn.run_sync(lambda c: _RV.__table__.create(c, checkfirst=True))
    await session.commit()


def test_summarize_version_phase_preserves_acceptance_aggregate():
    """The budget-trim summarizer keeps the additive acceptance_aggregate
    top-level key (and the raw merged count) intact under trim pressure."""
    from app.strategic_spawning import _summarize_version_phase

    block = {
        "version_number": 4,
        "phase": "stabilize",
        "tasks_merged_this_version": 6,
        "acceptance_aggregate": {"merged": 6, "proven": 4, "proven_rate": 0.6667},
        "promotion_readiness": {
            "ready": True,
            "thresholds": {"min_verdicts": 5},
            "confidence": {"mean": 0.9, "stdev": 0.02, "min": 0.8, "max": 0.95},
        },
    }
    out = _summarize_version_phase(block)
    # The aggregate and raw merged count survive the trim verbatim.
    assert out["acceptance_aggregate"] == {
        "merged": 6,
        "proven": 4,
        "proven_rate": 0.6667,
    }
    assert out["tasks_merged_this_version"] == 6
    # The trim still drops verbose thresholds from promotion_readiness.
    assert "thresholds" not in out["promotion_readiness"]


def test_summarize_version_phase_strips_nested_audit_findings_but_keeps_facts():
    """The version_phase summarizer must keep the lifecycle facts that steer the
    strategist while stripping bulky nested audit findings from both the current
    qualification and recent-version history."""
    from app.strategic_spawning import _summarize_version_phase

    huge_finding = {
        "severity": "high",
        "summary": "large audit finding",
        "details": "finding detail " * 500,
        "evidence": ["evidence detail " * 200 for _ in range(4)],
    }
    block = {
        "version_number": 21,
        "phase": "stabilize",
        "goal": "Make promotion lifecycle context impossible to lose.",
        "tasks_merged_this_version": 11,
        "acceptance_aggregate": {"merged": 11, "proven": 9, "proven_rate": 0.8182},
        "last_qualification": {
            "status": "not_promoted",
            "confidence": 0.73,
            "blocker": "audit_findings",
            "candidate_sha": "abcdef1234567890",
            "audit_findings": [huge_finding for _ in range(12)],
            "sub_scores": {"audit": 0.42, "tests": 1.0},
        },
        "recent_versions": [
            {
                "version_number": 20 - i,
                "status": "promoted",
                "phase": "stabilize",
                "tasks_merged_this_version": 5 + i,
                "acceptance_aggregate": {
                    "merged": 5 + i,
                    "proven": 4 + i,
                    "proven_rate": 0.8,
                },
                "last_qualification": {
                    "status": "promoted" if i != 1 else "not_promoted",
                    "confidence": 0.92 - (i * 0.04),
                    "blocker": None if i != 1 else "ci_red",
                    "audit_findings": [huge_finding for _ in range(10)],
                },
            }
            for i in range(5)
        ],
    }

    out = _summarize_version_phase(block)

    assert out["version_number"] == 21
    assert out["phase"] == "stabilize"
    assert out["goal"] == "Make promotion lifecycle context impossible to lose."
    assert out["tasks_merged_this_version"] == 11
    assert out["acceptance_aggregate"] == {
        "merged": 11,
        "proven": 9,
        "proven_rate": 0.8182,
    }

    last = out["last_qualification"]
    assert last["status"] == "not_promoted"
    assert last["confidence"] == 0.73
    assert last["blocker"] == "audit_findings"
    assert "audit_findings" not in last
    assert last["sub_scores"] == {"audit": 0.42, "tests": 1.0}

    recent = out["recent_versions"]
    assert len(recent) == 3
    assert recent[0]["version_number"] == 20
    assert recent[0]["tasks_merged_this_version"] == 5
    assert recent[0]["acceptance_aggregate"] == {
        "merged": 5,
        "proven": 4,
        "proven_rate": 0.8,
    }
    recent_lq = recent[1]["last_qualification"]
    assert recent_lq["status"] == "not_promoted"
    assert recent_lq["confidence"] == 0.88
    assert recent_lq["blocker"] == "ci_red"

    serialized = json.dumps(out, default=str)
    assert "large audit finding" not in serialized
    assert "finding detail" not in serialized


@pytest.mark.asyncio
async def test_version_phase_includes_promotion_readiness(
    db_session, mocker, _lts_split_active
):
    """When is_self_repo=True and QualificationVerdict rows exist, the strategic
    payload's version_phase block carries promotion_readiness with ready,
    would_promote_rate, and recurring_blockers keys."""
    from app.models import ReleaseVersion, QualificationVerdict

    await _ensure_releaseversion_table(db_session)
    await _ensure_verdict_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Open version in stabilize phase
    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        status="open",
        opened_at=now,
    )
    db_session.add(version)
    await db_session.commit()

    await db_session.refresh(version)

    # Seed QualificationVerdict rows (promotion_enabled=False = dry-run regime)
    for i in range(3):
        db_session.add(
            QualificationVerdict(
                project_id=project.id,
                release_version_id=version.id,
                would_promote=True,
                blocker=None,
                confidence=0.85 + i * 0.01,
                promotion_enabled=False,
            )
        )
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    vp = payload.get("version_phase")
    assert vp is not None, "version_phase block should be present for is_self_repo=True"
    pr = vp.get("promotion_readiness")
    assert pr is not None, "promotion_readiness should be present when verdicts exist"
    assert "ready" in pr, "promotion_readiness must have 'ready' key"
    assert "would_promote_rate" in pr, (
        "promotion_readiness must have 'would_promote_rate' key"
    )
    assert "recurring_blockers" in pr, (
        "promotion_readiness must have 'recurring_blockers' key"
    )


@pytest.mark.asyncio
async def test_version_phase_qualification_state_reconciles_finalized_verdict(
    db_session, mocker, _lts_split_active
):
    """The strategic payload's version_phase applies the finalized-verdict
    substitution (unified with the other open-version surfaces): a stale
    ``pending`` + ``promotion_pending`` record with an already-resolved verdict
    reads the terminal status (NOT ``promotion_in_flight``) and surfaces the
    verdict disagreement explicitly."""
    from app.models import QualificationVerdict, ReleaseVersion
    from sqlalchemy.orm.attributes import flag_modified

    await _ensure_releaseversion_table(db_session)
    await _ensure_verdict_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        status="open",
        opened_at=now,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    sha = "e" * 40
    db_session.add(
        QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha=sha,
            confidence=0.9,
            terminal_status="promoted",
            would_promote=True,
            promotion_enabled=False,
            finalized_at=now,
            audit_findings=[{"severity": "LOW", "summary": "nit"}],
        )
    )
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": sha,
        "confidence": 0.9,
        "promotion_pending": {"pr_number": 7, "pr_url": "https://x/pull/7"},
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    vp = tasks[0].payloads[0].get("version_phase")
    assert vp is not None, "version_phase block should be present"

    qs = vp.get("qualification_state")
    assert qs is not None, "qualification_state must be present"
    assert qs["state"] != "promotion_in_flight", (
        f"finalized verdict must supersede the stale promotion_pending: {qs}"
    )
    assert qs["state"] == "promoted", qs
    dis = vp.get("qualification_state_disagreement")
    assert dis is not None, (
        "strategic payload must surface the finalized-verdict disagreement"
    )
    assert dis["last_qualification_status"] == "pending"
    assert dis["verdict_terminal_status"] == "promoted"


@pytest.mark.asyncio
async def test_version_phase_narrates_debt_enforce_preview(
    db_session, mocker, _lts_split_active
):
    """The strategic payload keeps the observe-mode debt-enforce preview on the
    compacted version_phase.last_qualification block."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=_utcnow_naive(),
        last_qualification={
            "status": "not_promoted",
            "candidate_sha": "d" * 40,
            "confidence": 0.72,
            "threshold": 0.8,
            "sub_scores": {"audit": 0.9, "test_erosion": 0.8, "smoketest": 0.95},
            "debt_enforce_confidence_preview": 0.61,
            "debt_enforce_weight_preview": 0.15,
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    vp = tasks[0].payloads[0].get("version_phase")
    assert vp is not None, "version_phase block should be present"
    qual = vp.get("last_qualification")
    assert qual is not None, "last_qualification should be present"
    assert qual["debt_enforce_confidence_preview"] == pytest.approx(0.61)
    assert qual["debt_enforce_weight_preview"] == pytest.approx(0.15)


@pytest.mark.asyncio
async def test_version_phase_qualification_state_reconciles_crashed_audit(
    db_session, mocker, _lts_split_active
):
    """The strategic payload's version_phase applies the audit-crash reconciliation:
    a bare ``pending`` record whose version-scoped audit child crashed
    (terminal-failed, no report) reads ``audit_crashed`` instead of telling the
    strategist to wait on a dead audit."""
    from app.models import ReleaseVersion
    from sqlalchemy.orm.attributes import flag_modified

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        status="open",
        opened_at=now,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    sha = "f" * 40
    version.last_qualification = {"status": "pending", "candidate_sha": sha}
    flag_modified(version, "last_qualification")
    db_session.add(version)
    # Version-scoped release_audit task pinned to the evaluated candidate, FAILED
    # with no report artifact => a crashed audit child.
    db_session.add(
        Task(
            project_id=project.id,
            payloads=[{"type": "release_audit"}],
            vm_instance_id="agent-vm-audit-crash-strat",
            status=TaskStatus.FAILED,
            context={"release_version_id": str(version.id), "candidate_sha": sha},
        )
    )
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    vp = tasks[0].payloads[0].get("version_phase")
    assert vp is not None, "version_phase block should be present"

    qs = vp.get("qualification_state")
    assert qs is not None and qs["state"] == "audit_crashed", qs


@pytest.mark.asyncio
async def test_version_phase_includes_recent_versions(
    db_session, mocker, _lts_split_active
):
    """When is_self_repo=True and closed ReleaseVersion rows exist, version_phase
    carries a recent_versions list."""
    from datetime import timedelta
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()

    # One closed (promoted) version
    closed = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=10),
        promoted_at=now - timedelta(days=2),
    )
    db_session.add(closed)

    # One open version
    open_v = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=now,
    )
    db_session.add(open_v)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    vp = payload.get("version_phase")
    assert vp is not None
    rv = vp.get("recent_versions")
    assert rv is not None, (
        "recent_versions should be present when closed versions exist"
    )
    assert isinstance(rv, list)
    assert len(rv) >= 1
    # Each entry should have version_number and status
    for entry in rv:
        assert "version_number" in entry
        assert "status" in entry


@pytest.mark.asyncio
async def test_version_phase_block_carries_goal_coverage_drift(
    db_session, mocker, _lts_split_active
):
    """The always-on version_phase block carries goal_coverage_drift when the
    helper returns a non-None dict, and the key survives _compact_version_phase
    at both normal and floor levels."""
    from app.models import ReleaseVersion, QualificationVerdict

    await _ensure_releaseversion_table(db_session)
    await _ensure_verdict_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="build",
        status="open",
        goal="Ship feature X and Y",
        opened_at=now,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Seed a QualificationVerdict with a merge_grade that flags a goal miss
    db_session.add(
        QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            merge_grade_status="fail",
            merge_grade_coverage=0.45,
            merge_grade_biggest_gap="feature Y not covered",
            goal_alignment="partial",
            finalized_at=now,
        )
    )
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    vp = payload.get("version_phase")
    assert vp is not None, "version_phase block should be present"
    drift = vp.get("goal_coverage_drift")
    assert drift is not None, "goal_coverage_drift should be present in version_phase"
    assert "signal" in drift, "goal_coverage_drift must have 'signal' key"
    assert drift["signal"] == "drifting", (
        f"signal should be 'drifting' given stale merge grade, got {drift['signal']!r}"
    )
    assert "biggest_gap" in drift, "goal_coverage_drift must carry biggest_gap"

    # Confirm it survives compaction at both normal and floor levels.
    from app.strategic_payload_compaction import (
        _summarize_version_phase,
        _version_phase_floor,
    )

    normal = _summarize_version_phase(vp)
    assert "goal_coverage_drift" in normal, (
        "goal_coverage_drift must survive _summarize_version_phase"
    )
    assert normal["goal_coverage_drift"].get("signal") == "drifting"

    floor = _version_phase_floor(vp)
    assert "goal_coverage_drift" in floor, (
        "goal_coverage_drift must survive _version_phase_floor"
    )


@pytest.mark.asyncio
async def test_version_phase_block_carries_criterion_coverage(
    db_session, mocker, _lts_split_active
):
    """The initial strategic payload carries criterion_coverage for the open
    version and the signal survives version_phase compaction/floor trimming."""

    from app import acceptance_criteria as ac
    from app.models import ReleaseVersion
    from app.strategic_payload_compaction import (
        _summarize_version_phase,
        _version_phase_floor,
    )

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        phase="build",
        status="open",
        opened_at=now,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    c_covered = await ac.create_criterion(
        db_session, project.id, version.id, "Ship the new auth layer"
    )
    c_partial = await ac.create_criterion(
        db_session, project.id, version.id, "Harden CI pipeline"
    )
    c_uncovered = await ac.create_criterion(
        db_session, project.id, version.id, "Document the rollout"
    )
    await ac.record_evidence(
        db_session,
        criterion_id=c_covered.id,
        project_id=project.id,
        source_kind="operator_attestation",
        source_id="attest-1",
    )
    await ac.record_evidence(
        db_session,
        criterion_id=c_partial.id,
        project_id=project.id,
        source_kind="operator",
        source_id="note-7",
    )

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    vp = payload.get("version_phase")
    assert vp is not None, "version_phase block should be present"
    coverage = vp.get("criterion_coverage")
    assert coverage is not None, "criterion_coverage should be present in version_phase"
    assert coverage["total"] == 3
    assert coverage["covered"] == 0
    assert coverage["attested"] == 1
    assert coverage["partial"] == 1
    assert coverage["uncovered"] == 1
    by_key = {entry["criterion_key"]: entry for entry in coverage["criteria"]}
    assert by_key[c_covered.criterion_key]["coverage"] == "attested"
    assert by_key[c_partial.criterion_key]["coverage"] == "partial"
    assert by_key[c_uncovered.criterion_key]["coverage"] == "uncovered"

    normal = _summarize_version_phase(vp)
    assert normal["criterion_coverage"]["total"] == 3
    assert len(normal["criterion_coverage"]["criteria"]) == 3

    floor = _version_phase_floor(vp)
    assert floor["criterion_coverage"]["total"] == 3
    floor_criteria = floor["criterion_coverage"]["criteria"]
    assert len(floor_criteria) == 3
    assert {entry["criterion_key"] for entry in floor_criteria} == {
        c_covered.criterion_key,
        c_partial.criterion_key,
        c_uncovered.criterion_key,
    }

    # stuck_count survives compaction — the uncovered criterion has no direction
    # mapping so its stuck_reason is "no_mapping" and stuck_count is 1.
    assert coverage["stuck_count"] == 1, "stuck_count must be carried in the raw payload"
    assert normal["criterion_coverage"]["stuck_count"] == 1, "stuck_count must survive _summarize_version_phase"
    assert floor["criterion_coverage"]["stuck_count"] == 1, "stuck_count must survive _version_phase_floor"
    # Per-criterion stuck_reason is in the criteria list (not the floor path).
    stuck_entry = by_key[c_uncovered.criterion_key]
    assert stuck_entry["stuck_reason"] == "no_mapping", "uncovered criterion with no direction should be no_mapping"
    # Covered and partial criteria have stuck_reason=None.
    assert by_key[c_covered.criterion_key]["stuck_reason"] is None
    assert by_key[c_partial.criterion_key]["stuck_reason"] is None


@pytest.mark.asyncio
async def test_version_phase_build_failure_surfaces_in_payload(
    db_session, mocker, _lts_split_active
):
    """When the outer version_phase block assembly fails, the payload must carry a
    compact failure-marker dict (lifecycle_signal='unavailable', signal_status=
    'build_failed') rather than silently omitting the block entirely.

    This is the regression test required by the acceptance gate:
    test_version_phase_build_failure_surfaces_in_payload must run green under a
    real merge-test verdict, and the PR must change non-test production code that
    the test exercises.
    """
    from app.strategic_payload_compaction import (
        _summarize_version_phase,
        _version_phase_floor,
    )

    # Patch current_open_version to raise so the outer try/except fires.
    mocker.patch(
        "app.release_version.current_open_version",
        side_effect=RuntimeError("simulated version_phase build failure"),
    )

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    vp = payload.get("version_phase")
    assert vp is not None, (
        "version_phase must be present even when assembly fails — "
        "the failure-marker dict should be set, not None"
    )
    assert vp.get("lifecycle_signal") == "unavailable", (
        f"lifecycle_signal must be 'unavailable' on build failure; got {vp!r}"
    )
    assert vp.get("signal_status") == "build_failed", (
        f"signal_status must be 'build_failed'; got {vp.get('signal_status')!r}"
    )
    assert isinstance(vp.get("error"), str) and len(vp["error"]) > 0, (
        "error must be a non-empty bounded string"
    )
    assert len(vp["error"]) <= 240, "error must be bounded to 240 chars"
    assert vp.get("integration_branch") == "automain", (
        "integration_branch must be preserved in the failure marker"
    )
    assert vp.get("promotion_target") == "main", (
        "promotion_target must be preserved in the failure marker"
    )

    # Failure marker must survive both compaction paths so it is not silently
    # dropped under budget pressure.
    normal = _summarize_version_phase(vp)
    assert normal.get("lifecycle_signal") == "unavailable", (
        "lifecycle_signal must survive _summarize_version_phase"
    )
    assert normal.get("signal_status") == "build_failed"
    assert normal.get("integration_branch") == "automain"
    assert normal.get("promotion_target") == "main"

    floor_out = _version_phase_floor(vp)
    assert floor_out.get("lifecycle_signal") == "unavailable", (
        "lifecycle_signal must survive _version_phase_floor"
    )
    assert floor_out.get("signal_status") == "build_failed"


@pytest.mark.asyncio
async def test_version_phase_recent_versions_use_aggregate_pending_truth(
    db_session, mocker, _lts_split_active
):
    """A backend-confirmed promotion stays unshipped in the strategist payload
    while another required artifact is still unconfirmed."""
    from app import release_artifacts as artifact_registry
    from app.models import ReleaseDeployArtifact, ReleaseVersion
    from app.release_artifacts import (
        BACKEND_REVISION_ARTIFACT_KIND,
        ReleaseArtifactProvider,
    )

    await _ensure_releaseversion_table(db_session)

    backend_provider = artifact_registry._ARTIFACT_PROVIDERS[
        BACKEND_REVISION_ARTIFACT_KIND
    ]

    async def _check_synthetic(_session, _version, _artifact, _now):
        return None

    mocker.patch.dict(
        artifact_registry._ARTIFACT_PROVIDERS,
        {
            BACKEND_REVISION_ARTIFACT_KIND: backend_provider,
            "synthetic_required": ReleaseArtifactProvider(
                artifact_kind="synthetic_required",
                check=_check_synthetic,
                required=True,
            ),
        },
        clear=True,
    )

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    closed = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=10),
        promoted_at=now - timedelta(days=2),
        last_qualification={"status": "promoted", "confidence": 0.93},
        serving_status="serving_confirmed",
        served_sha="abc123def456",
    )
    open_v = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=now,
        qualification_attempts=0,
        last_qualification=None,
    )
    db_session.add_all([closed, open_v])
    await db_session.commit()
    await db_session.refresh(closed)

    db_session.add_all(
        [
            ReleaseDeployArtifact(
                version_id=closed.id,
                artifact_kind=BACKEND_REVISION_ARTIFACT_KIND,
                serving_status="serving_confirmed",
                served_identifier="abc123def456",
                served_at=now - timedelta(days=2),
            ),
            ReleaseDeployArtifact(
                version_id=closed.id,
                artifact_kind="synthetic_required",
                serving_status="unconfirmed",
            ),
        ]
    )
    await db_session.commit()

    _patch_spawning_deps(mocker)
    assert await _spawn_strategic_review(project, db_session) is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    )

    recent = (payload.get("version_phase") or {}).get("recent_versions") or []
    assert recent, "recent_versions should include the promoted version"
    latest = recent[0]
    artifacts = {entry["artifact_kind"]: entry for entry in latest["artifacts"]}
    assert latest["serving_status"] == "unconfirmed"
    assert latest["qualification_state"]["state"] == "promoted_unconfirmed"
    assert artifacts["synthetic_required"]["serving_status"] == "unconfirmed"

    suffix = payload.get("prompt_suffix", "")
    assert "NOT yet confirmed serving" in suffix, suffix
    assert "synthetic_required" in suffix, suffix


@pytest.mark.asyncio
async def test_version_phase_uses_promotion_target_gate(db_session, mocker):
    """Version-phase LTS context is active only when the resolver sees distinct branches."""
    await _ensure_releaseversion_table(db_session)

    _patch_spawning_deps(mocker)
    current_open_version = mocker.patch(
        "app.release_version.current_open_version",
        new=AsyncMock(return_value=None),
    )
    ensure_initial_release_version = mocker.patch(
        "app.release_version.ensure_initial_release_version",
        new=AsyncMock(return_value=None),
    )

    skipped_project = _make_project(
        integration_branch="trunk",
        promotion_target="trunk",
        vision={"purpose": "test purpose"},
    )
    db_session.add(skipped_project)
    await db_session.commit()
    await db_session.refresh(skipped_project)

    # integration == promotion: the version-phase LTS context is gated off
    # (the resolver fails safe instead of raising), so the review still spawns
    # but without invoking the version lifecycle.
    assert await _spawn_strategic_review(skipped_project, db_session) is True
    assert current_open_version.call_count == 0
    assert ensure_initial_release_version.call_count == 0

    current_open_version.reset_mock()
    ensure_initial_release_version.reset_mock()

    active_project = _make_project(
        integration_branch="automain",
        promotion_target="main",
        vision={"purpose": "test purpose"},
    )
    db_session.add(active_project)
    await db_session.commit()
    await db_session.refresh(active_project)

    assert await _spawn_strategic_review(active_project, db_session) is True
    # The integration!=promotion gate performs at least one version-phase lookup;
    # compute_qualification_trigger_state may add a second call via release_readiness.
    assert current_open_version.call_count >= 1
    assert ensure_initial_release_version.call_args.kwargs["promotion_target"] == "main"


@pytest.mark.asyncio
async def test_version_phase_bootstrap_passes_custom_promotion_target(
    db_session, mocker
):
    """Bootstrap uses the project promotion target even when integration_branch is main."""
    await _ensure_releaseversion_table(db_session)

    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.release_version.current_open_version",
        new=AsyncMock(return_value=None),
    )
    ensure_initial_release_version = mocker.patch(
        "app.release_version.ensure_initial_release_version",
        new=AsyncMock(return_value=None),
    )

    project = _make_project(
        integration_branch="main",
        promotion_target="deploy",
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert await _spawn_strategic_review(project, db_session) is True
    assert ensure_initial_release_version.call_count == 1
    assert (
        ensure_initial_release_version.call_args.kwargs["promotion_target"] == "deploy"
    )


# ── stabilize-stall strategic nudge ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_stabilize_stall_hint_fires_for_old_stabilize_version(
    db_session, mocker, _lts_split_active
):
    """An open stabilize-phase version older than _STABILIZE_STALL_AGE_DAYS with
    no qualification attempt gets a STALLED nudge in the strategic prompt_suffix
    and a stabilize_stall block in version_phase."""
    from datetime import timedelta
    from app.models import ReleaseVersion
    from app.release_qualifier import _STABILIZE_STALL_AGE_DAYS

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Version stuck in stabilize well past the age threshold
    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=now - timedelta(days=_STABILIZE_STALL_AGE_DAYS + 5),
        phase_changed_at=now - timedelta(days=_STABILIZE_STALL_AGE_DAYS + 5),
        last_qualification=None,
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "STALLED" in suffix, (
        f"Expected STALLED nudge in prompt_suffix; got: {suffix}"
    )
    assert "stabilize" in suffix.lower()

    vp = payload.get("version_phase")
    assert vp is not None
    stall_block = vp.get("stabilize_stall")
    assert stall_block is not None, (
        "stabilize_stall block should appear in version_phase"
    )
    assert stall_block.get("age_days", 0) >= _STABILIZE_STALL_AGE_DAYS


@pytest.mark.asyncio
async def test_stabilize_hint_points_at_carried_audit_findings(
    db_session, mocker, _lts_split_active
):
    """A stabilize version whose last_qualification carries audit findings gets
    a prompt_suffix that names the findings and tells the agent to target them
    with an audit-mode direction — and the findings ride through in
    version_phase.last_qualification.audit_findings."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={
            "status": "not_promoted",
            "confidence": 0.85,
            "blocker": None,
            "sub_scores": {"audit": 0.74, "test_erosion": 1.0},
            "audit_findings": [
                {"severity": "high", "summary": "Token baked into VM image."},
                {"severity": "low", "summary": "Private cross-module import."},
            ],
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "audit_findings" in suffix, f"Expected findings pointer; got: {suffix}"
    assert "Token baked into VM image" in suffix
    assert "recommended_mode='audit'" in suffix
    # The project has no active audit/fundamentals-mode direction, so the hint
    # must demand the agent CREATE one rather than merely "update" (a status
    # note had been satisfying the old phrasing).
    assert "MUST create a new direction" in suffix
    assert "does NOT count" in suffix

    vp = payload.get("version_phase")
    assert vp is not None
    carried = (vp.get("last_qualification") or {}).get("audit_findings")
    assert carried == [
        {"severity": "high", "summary": "Token baked into VM image."},
        {"severity": "low", "summary": "Private cross-module import."},
    ]
    # Audit (0.74) is the lowest active sub-score here, so it IS the binding
    # constraint and the hint should frame it that way.
    assert "the audit sub-score is the binding constraint on promotion" in suffix


async def _spawn_and_get_stabilize_suffix(project, db_session):
    """Helper: spawn a strategic review and return its prompt_suffix string."""
    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    return tasks[0].payloads[0].get("prompt_suffix", "")


@pytest.mark.asyncio
async def test_stabilize_hint_names_lower_dimension_when_audit_not_binding(
    db_session, mocker, _lts_split_active
):
    """When audit flagged findings but test_erosion is the LOWER sub-score,
    the hint must NOT claim audit is the binding constraint — it should point at
    test_erosion as the real limiting dimension."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={
            "status": "not_promoted",
            "confidence": 0.6,
            "blocker": None,
            "sub_scores": {"audit": 0.80, "test_erosion": 0.42},
            "audit_findings": [
                {"severity": "high", "summary": "Token baked into VM image."},
            ],
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    suffix = await _spawn_and_get_stabilize_suffix(project, db_session)
    assert "test_erosion sub-score" in suffix
    assert "audit is not the limiting dimension" in suffix
    assert "the audit sub-score is the binding constraint on promotion" not in suffix


@pytest.mark.asyncio
async def test_version_phase_unavailable_marker_on_build_failure(
    db_session, mocker, _lts_split_active
):
    """When the version_phase block assembly encounters an exception, a minimal
    version_phase block with build_unavailable=True is included in the payload,
    so the strategist can distinguish 'build failed' from 'no version exists'."""
    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_spawning_deps(mocker)
    # Force the version_phase block build to fail by mocking
    # current_open_version to raise an exception.
    mocker.patch(
        "app.release_version.current_open_version",
        new=AsyncMock(side_effect=RuntimeError("version_phase build boom")),
    )

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    vp = payload.get("version_phase")
    assert vp is not None, "version_phase block should be present even on build failure"
    assert (
        vp.get("build_unavailable") is True
    ), "version_phase should have build_unavailable=True marker"


def _patch_candidate_currency(mocker, *, resolved, head_sha):
    """Mock the GitHub resolution path used by _resolve_candidate_currency.

    ``resolved`` is the (token, owner, repo) tuple or None returned by
    ``_resolve_github_with_session``; ``head_sha`` is the integration-branch
    HEAD ``get_branch_sha`` reports.
    """
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=resolved),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=head_sha),
    )


def _make_stabilize_version_with_candidate(candidate_sha):
    from app.models import ReleaseVersion
    from app.release_scoring import _audit_finding_key

    now = _utcnow_naive()
    # Carried findings stamped with the deterministic content key the resolution
    # ledger joins against (release_scoring._audit_finding_key) so readiness can be
    # computed exactly as it is for real carried findings.
    _high = {"severity": "high", "summary": "Token baked into VM image."}
    _high["key"] = _audit_finding_key(_high["summary"], None)
    _low = {"severity": "low", "summary": "Private cross-module import."}
    _low["key"] = _audit_finding_key(_low["summary"], None)
    return ReleaseVersion(
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={
            "status": "not_promoted",
            "confidence": 0.85,
            "blocker": None,
            "sub_scores": {"audit": 0.74, "test_erosion": 1.0},
            "candidate_sha": candidate_sha,
            "audit_findings": [_high, _low],
        },
    )


async def _ensure_audit_finding_resolution_table(session):
    """Create the AuditFindingResolution ledger table idempotently."""
    from app.models import AuditFindingResolution as _AFR

    conn = await session.connection()
    await conn.run_sync(lambda c: _AFR.__table__.create(c, checkfirst=True))
    await session.commit()


async def _resolve_finding_in_ledger(
    session, project_id, release_version_id, *, summary, severity, state="resolved"
):
    """Insert a resolution-ledger row for the carried finding keyed by summary."""
    from app.models import AuditFindingResolution
    from app.release_scoring import _audit_finding_key

    row = AuditFindingResolution(
        project_id=project_id,
        release_version_id=release_version_id,
        finding_key=_audit_finding_key(summary, None),
        severity=severity,
        state=state,
    )
    session.add(row)
    await session.commit()


@pytest.mark.asyncio
async def test_stabilize_carried_findings_marked_stale_when_candidate_behind_head(
    db_session, mocker, _lts_split_active
):
    """When the qualification's candidate commit is BEHIND the live integration
    HEAD AND the resolution ledger marks every gating finding resolved-or-stale,
    the carried findings are framed POSSIBLY-STALE, the agent is steered toward a
    fresh qualify_request (trigger MET), and they are NOT asserted as a current
    binding blocker nor escalated into a new audit direction."""
    await _ensure_releaseversion_table(db_session)
    await _ensure_audit_finding_resolution_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "a" * 40
    version = _make_stabilize_version_with_candidate(candidate)
    version.project_id = project.id
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Mark the single gating (high) carried finding resolved in the ledger so the
    # pre-registered re-qualification trigger is MET and the re-qualify steer applies.
    await _resolve_finding_in_ledger(
        db_session,
        project.id,
        version.id,
        summary="Token baked into VM image.",
        severity="high",
        state="resolved",
    )

    _patch_spawning_deps(mocker)
    _patch_candidate_currency(
        mocker, resolved=("tok", "owner", "repo"), head_sha="b" * 40
    )
    suffix = await _spawn_and_get_stabilize_suffix(project, db_session)

    assert "POSSIBLY-STALE" in suffix
    assert "qualify_request" in suffix
    assert "trigger is MET" in suffix
    assert candidate[:8] in suffix
    # Must NOT assert the stale findings are a current binding blocker, and must
    # NOT churn a fresh remediation direction at a superseded snapshot.
    assert "the audit sub-score is the binding constraint on promotion" not in suffix
    assert "MUST create a new direction" not in suffix


@pytest.mark.asyncio
async def test_stabilize_hint_requalify_only_when_readiness_signaled(
    db_session, mocker, _lts_split_active
):
    """Trigger-based stabilize steering (the pre-registered re-qualification trigger).

    On the behind-head path the hint steers toward a fresh qualify_request ONLY
    when the finding-resolution ledger marks every gating high/medium finding
    resolved-or-stale. When a gating finding remains UNRESOLVED, the hint instead
    steers toward the outstanding fix-first work (and escalates to create a
    remediation direction) rather than a blanket re-qualification.
    """
    await _ensure_releaseversion_table(db_session)
    await _ensure_audit_finding_resolution_table(db_session)
    _patch_spawning_deps(mocker)
    _patch_candidate_currency(
        mocker, resolved=("tok", "owner", "repo"), head_sha="b" * 40
    )

    # ---- Scenario A: gating finding RESOLVED in the ledger → trigger MET ----
    ready_project = _make_project(is_self_repo=True, vision={"purpose": "p"})
    db_session.add(ready_project)
    await db_session.commit()
    await db_session.refresh(ready_project)

    ready_version = _make_stabilize_version_with_candidate("a" * 40)
    ready_version.project_id = ready_project.id
    db_session.add(ready_version)
    await db_session.commit()
    await db_session.refresh(ready_version)
    await _resolve_finding_in_ledger(
        db_session,
        ready_project.id,
        ready_version.id,
        summary="Token baked into VM image.",
        severity="high",
        state="resolved",
    )

    ready_suffix = await _spawn_and_get_stabilize_suffix(ready_project, db_session)
    # Re-qualify steer engaged; fix-first / escalation NOT engaged.
    assert "trigger is MET" in ready_suffix
    assert "PREFER emitting a fresh qualify_request" in ready_suffix
    assert "UNRESOLVED" not in ready_suffix
    assert "trigger is NOT met" not in ready_suffix
    assert "MUST create a new direction" not in ready_suffix

    # ---- Scenario B: gating finding UNRESOLVED (no ledger row) → trigger NOT met --
    notready_project = _make_project(is_self_repo=True, vision={"purpose": "p"})
    db_session.add(notready_project)
    await db_session.commit()
    await db_session.refresh(notready_project)

    notready_version = _make_stabilize_version_with_candidate("c" * 40)
    notready_version.project_id = notready_project.id
    db_session.add(notready_version)
    await db_session.commit()
    await db_session.refresh(notready_version)
    # No resolution-ledger row → the high gating finding reads as unresolved.

    notready_suffix = await _spawn_and_get_stabilize_suffix(
        notready_project, db_session
    )
    # Fix-first steer engaged; the blanket re-qualify trigger is explicitly NOT met.
    assert "trigger is NOT met" in notready_suffix
    assert "UNRESOLVED" in notready_suffix
    assert "fix-first work" in notready_suffix
    assert "trigger is MET" not in notready_suffix
    # The carried finding still needs fixing, so the create-a-direction escalation
    # fires (there is no active audit/fundamentals-mode direction on this project).
    assert "MUST create a new direction" in notready_suffix


@pytest.mark.asyncio
async def test_stabilize_carried_findings_live_when_candidate_equals_head(
    db_session, mocker, _lts_split_active
):
    """When the candidate commit equals the live HEAD, the findings are presented
    live exactly as before — binding-constraint framing + create-a-direction
    escalation — with no POSSIBLY-STALE wording."""
    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "c" * 40
    version = _make_stabilize_version_with_candidate(candidate)
    version.project_id = project.id
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    _patch_candidate_currency(
        mocker, resolved=("tok", "owner", "repo"), head_sha=candidate
    )
    suffix = await _spawn_and_get_stabilize_suffix(project, db_session)

    assert "POSSIBLY-STALE" not in suffix
    assert "the audit sub-score is the binding constraint on promotion" in suffix
    assert "MUST create a new direction" in suffix
    assert "Could not confirm" not in suffix


@pytest.mark.asyncio
async def test_stabilize_carried_findings_degrades_when_sha_unresolvable(
    db_session, mocker, _lts_split_active
):
    """When the integration HEAD can't be resolved (GitHub unavailable), the loop
    degrades to today's behavior but softens the currency assertion with a
    provisional caveat — and never crashes."""
    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "d" * 40
    version = _make_stabilize_version_with_candidate(candidate)
    version.project_id = project.id
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    _patch_candidate_currency(mocker, resolved=None, head_sha="e" * 40)
    suffix = await _spawn_and_get_stabilize_suffix(project, db_session)

    assert "POSSIBLY-STALE" not in suffix
    assert "the audit sub-score is the binding constraint on promotion" in suffix
    assert "Could not confirm" in suffix


@pytest.mark.asyncio
async def test_spawn_strategic_review_payload_staleness_includes_generated_at(
    db_session, mocker, _lts_split_active
):
    """When the qualification candidate is BEHIND integration HEAD, the spawned
    strategic_review payload must contain a payload_staleness block with stale=True,
    payload_compiled_sha == candidate_sha, active_candidate_sha == candidate_sha,
    and integration_head_sha == head_sha."""
    await _ensure_releaseversion_table(db_session)
    await _ensure_audit_finding_resolution_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "a" * 40
    head = "b" * 40
    version = _make_stabilize_version_with_candidate(candidate)
    version.project_id = project.id
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    _patch_candidate_currency(mocker, resolved=("tok", "owner", "repo"), head_sha=head)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    staleness = payload.get("payload_staleness")
    assert staleness is not None, "payload_staleness must be present when currency resolved"
    assert staleness["stale"] is True, "stale must be True when candidate is behind HEAD"
    assert staleness["verdict"] == "stale", "verdict must be 'stale' when candidate is behind HEAD"
    assert "checked_at" in staleness, "checked_at timestamp must be present"
    assert staleness["payload_compiled_sha"] == candidate
    assert staleness["payload_sha"] == candidate
    assert staleness["active_candidate_sha"] == candidate
    assert staleness["integration_head_sha"] == head
    _assert_generated_at_iso_utc(staleness)
    assert staleness["stale_reason"] == "payload_sha_differs_from_integration_head"


@pytest.mark.asyncio
async def test_spawn_strategic_review_payload_staleness_current_when_candidate_matches_head(
    db_session, mocker, _lts_split_active
):
    """When the qualification candidate equals the live HEAD, payload_staleness
    must be present with stale=False."""
    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    sha = "c" * 40
    version = _make_stabilize_version_with_candidate(sha)
    version.project_id = project.id
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    _patch_candidate_currency(mocker, resolved=("tok", "owner", "repo"), head_sha=sha)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    staleness = payload.get("payload_staleness")
    assert staleness is not None, "payload_staleness must be present when currency resolved"
    assert staleness["stale"] is False, "stale must be False when candidate equals HEAD"
    assert staleness["verdict"] == "fresh", "verdict must be 'fresh' when candidate matches HEAD"
    assert "checked_at" in staleness, "checked_at timestamp must be present"
    assert staleness["payload_sha"] == sha
    assert staleness["payload_compiled_sha"] == sha
    assert staleness["active_candidate_sha"] == sha
    assert staleness["integration_head_sha"] == sha
    _assert_generated_at_iso_utc(staleness)
    assert staleness["stale_reason"] is None


@pytest.mark.asyncio
async def test_spawn_strategic_review_payload_freshness_unknown_when_currency_indeterminate(
    db_session, mocker, _lts_split_active
):
    """When _resolve_candidate_currency returns None (GitHub unreachable or no
    candidate SHA), payload_staleness must be present with verdict='unknown' and
    a checked_at timestamp so the agent cannot mistake it for a confirmed-fresh result."""
    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "d" * 40
    version = _make_stabilize_version_with_candidate(candidate)
    version.project_id = project.id
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    # resolved=None means _resolve_github_with_session returned None (no credentials)
    # so _resolve_candidate_currency returns None -> _currency stays None.
    _patch_candidate_currency(mocker, resolved=None, head_sha="e" * 40)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    staleness = payload.get("payload_staleness")
    assert staleness is not None, (
        "payload_staleness must be present even when currency is indeterminate"
    )
    assert staleness.get("verdict") == "unknown", (
        "verdict must be 'unknown' when currency could not be resolved"
    )
    assert "checked_at" in staleness, "checked_at timestamp must be present"
    assert staleness.get("stale") is None or "stale" not in staleness, (
        "stale field must not be set on an unknown-verdict block"
    )


def _patch_candidate_currency_raises(mocker, exc, *, resolved=("tok", "owner", "repo")):
    """Mock the currency GitHub path so ``get_branch_sha`` raises ``exc``.

    Credential resolution succeeds (a transient GitHub failure only happens
    AFTER credentials resolve), and the bounded-retry sleep is neutralized so
    the api_unreachable classification is exercised without wall-clock delay.
    """
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=resolved),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(side_effect=exc),
    )
    mocker.patch("app.strategic_spawning._CURRENCY_RETRY_DELAY_S", 0)


@pytest.mark.asyncio
async def test_payload_staleness_api_unreachable_verdict_distinct_from_unknown(
    db_session, mocker, _lts_split_active
):
    """A TRANSIENT GitHub failure (5xx) must surface payload_staleness.verdict=
    'api_unreachable' — DISTINCT from the 'unknown' verdict used for genuine local
    indeterminacy — after a bounded retry, and must NOT set ``stale``.

    Regression: an API outage at spawn time must not be reported as a stale/unknown
    payload, which would force the strategist to re-ground every load-bearing
    payload claim.  The endpoint must never crash on the transient failure.
    """
    from fastapi import HTTPException

    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "f" * 40
    version = _make_stabilize_version_with_candidate(candidate)
    version.project_id = project.id
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    # Credentials resolve, but the live branch-HEAD read hits a GitHub 5xx — a
    # transient reachability failure, NOT genuine local indeterminacy.
    _patch_candidate_currency_raises(
        mocker,
        HTTPException(status_code=502, detail="GitHub API error (HTTP 502)."),
    )

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    staleness = payload.get("payload_staleness")
    assert staleness is not None, (
        "payload_staleness must be present even when GitHub is unreachable"
    )
    assert staleness.get("verdict") == "api_unreachable", (
        "a transient GitHub 5xx must produce verdict='api_unreachable', "
        "distinct from the 'unknown' verdict used for genuine indeterminacy"
    )
    assert staleness.get("verdict") != "unknown", (
        "api_unreachable must NOT collapse into the genuine-indeterminacy verdict"
    )
    # An unreachable API is NOT a known-stale payload — never set stale.
    assert "stale" not in staleness or staleness.get("stale") is None, (
        "stale must not be set on an api_unreachable block"
    )
    assert "checked_at" in staleness, "checked_at timestamp must be present"
    _assert_generated_at_iso_utc(staleness)


@pytest.mark.asyncio
async def test_resolve_candidate_currency_typed_verdicts(db_session, mocker):
    """Unit coverage of the typed verdicts produced by _resolve_candidate_currency:
    fresh / stale (resolved), api_unreachable (transient GitHub/transport), and
    unknown (genuine local indeterminacy) each with the right shape."""
    from fastapi import HTTPException
    import httpx

    from app.strategic_spawning import _resolve_candidate_currency

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    candidate = "a" * 40
    mocker.patch("app.strategic_spawning._CURRENCY_RETRY_DELAY_S", 0)

    # Genuine indeterminacy: no candidate SHA on the record → unknown, no GitHub call.
    res = await _resolve_candidate_currency(db_session, project, {}, "main")
    assert res["verdict"] == "unknown"
    assert res["resolved"] is False
    assert res["reason"] == "no_candidate_sha"

    # Genuine indeterminacy: candidate present but repo credentials unavailable.
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=None),
    )
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "unknown"
    assert res["reason"] == "repo_credentials_unavailable"
    assert res["candidate_sha"] == candidate

    # Resolved fresh: candidate == HEAD.
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "fresh"
    assert res["resolved"] is True
    assert res["current"] is True
    assert res["head_sha"] == candidate

    # Resolved stale: candidate != HEAD.
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value="b" * 40))
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "stale"
    assert res["current"] is False

    # Transient GitHub 5xx → api_unreachable, distinct from unknown.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(side_effect=HTTPException(status_code=502, detail="5xx")),
    )
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "api_unreachable"
    assert res["resolved"] is False
    assert res["candidate_sha"] == candidate

    # Transport error (timeout) → api_unreachable.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(side_effect=httpx.ConnectError("boom")),
    )
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "api_unreachable"

    # Branch genuinely not found (404) is NOT transient → unknown, not api_unreachable.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="not found")),
    )
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "unknown"
    assert res["reason"] == "branch_not_found"


@pytest.mark.asyncio
async def test_resolve_candidate_currency_retries_transient_failure(db_session, mocker):
    """The bounded retry must (a) make a SECOND ``get_branch_sha`` attempt after a
    transient blip and resolve fresh/stale when that retry succeeds, and (b) make
    exactly ``_CURRENCY_RETRY_ATTEMPTS`` attempts before classifying
    api_unreachable.  These assertions on ``call_count`` FAIL if the function
    only ever makes a single attempt — the core requested retry behavior."""
    from fastapi import HTTPException

    from app.strategic_spawning import (
        _CURRENCY_RETRY_ATTEMPTS,
        _resolve_candidate_currency,
    )

    # The retry must actually exercise more than one attempt for this to be a
    # meaningful guard; the production default is 2.
    assert _CURRENCY_RETRY_ATTEMPTS >= 2

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    candidate = "a" * 40
    head = "b" * 40
    # No wall-clock delay between the bounded retries.
    mocker.patch("app.strategic_spawning._CURRENCY_RETRY_DELAY_S", 0)
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )

    # (a) First attempt raises a transient 5xx, the retry succeeds with a HEAD
    # that differs from the candidate → resolves to 'stale' (NOT api_unreachable),
    # proving the transient failure was retried rather than classified.
    transient_then_ok = AsyncMock(
        side_effect=[HTTPException(status_code=502, detail="5xx"), head]
    )
    mocker.patch("app.github.get_branch_sha", new=transient_then_ok)
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "stale", (
        "a transient first attempt followed by a successful retry must resolve, "
        "not classify api_unreachable"
    )
    assert res["resolved"] is True
    assert res["head_sha"] == head
    assert transient_then_ok.await_count == 2, (
        "the transient first attempt must be retried a second time before resolving"
    )

    # (a') Same, but the retry succeeds with HEAD == candidate → 'fresh'.
    transient_then_fresh = AsyncMock(
        side_effect=[HTTPException(status_code=503, detail="5xx"), candidate]
    )
    mocker.patch("app.github.get_branch_sha", new=transient_then_fresh)
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "fresh"
    assert res["current"] is True
    assert transient_then_fresh.await_count == 2

    # (b) Every attempt fails transiently → api_unreachable only AFTER exhausting
    # the full bounded budget of attempts.
    always_transient = AsyncMock(
        side_effect=HTTPException(status_code=502, detail="5xx")
    )
    mocker.patch("app.github.get_branch_sha", new=always_transient)
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "api_unreachable"
    assert always_transient.await_count == _CURRENCY_RETRY_ATTEMPTS, (
        "api_unreachable must only be reported after the full bounded retry budget "
        f"of {_CURRENCY_RETRY_ATTEMPTS} attempts is exhausted"
    )

    # (b') A 404 short-circuits — it is not transient, so it must NOT be retried.
    not_found = AsyncMock(
        side_effect=HTTPException(status_code=404, detail="not found")
    )
    mocker.patch("app.github.get_branch_sha", new=not_found)
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "unknown"
    assert res["reason"] == "branch_not_found"
    assert not_found.await_count == 1, (
        "a non-transient 404 must short-circuit without consuming a retry"
    )


@pytest.mark.asyncio
async def test_payload_staleness_resolves_true_verdict_without_carried_findings(
    db_session, mocker, _lts_split_active
):
    """payload_staleness must carry a real stale/fresh verdict even when the
    version has no carried audit findings (i.e. the stabilize+findings fast-path
    is skipped).  Without the fallback resolution, _currency stays None and the
    payload emits verdict='unknown' on every non-findings cycle."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "a" * 40
    head = "b" * 40
    now = _utcnow_naive()
    # Stabilize phase with a candidate SHA but NO audit_findings — the
    # stabilize+carried-findings guard is skipped, so only the fallback path runs.
    version = ReleaseVersion(
        version_number=2,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={
            "status": "not_promoted",
            "confidence": 0.90,
            "blocker": None,
            "sub_scores": {"audit": 1.0, "test_erosion": 1.0},
            "candidate_sha": candidate,
            "audit_findings": [],
        },
    )
    version.project_id = project.id
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    _patch_candidate_currency(mocker, resolved=("tok", "owner", "repo"), head_sha=head)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    staleness = payload.get("payload_staleness")
    assert staleness is not None, "payload_staleness must be present"
    assert staleness.get("verdict") == "stale", (
        "verdict must be 'stale' (not 'unknown') when candidate SHA is behind HEAD "
        "even without carried audit findings — the fallback path must resolve currency"
    )
    assert staleness.get("stale") is True
    assert staleness.get("active_candidate_sha") == candidate
    assert staleness.get("integration_head_sha") == head
    _assert_generated_at_iso_utc(staleness)


@pytest.mark.asyncio
async def test_stabilize_hint_names_hard_blocker_as_binding_constraint(
    db_session, mocker, _lts_split_active
):
    """When a hard blocker fired, the hint must name the blocker as the binding
    constraint rather than asserting audit (or any numeric sub-score) is."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={
            "status": "not_promoted",
            "confidence": 0.0,
            "blocker": "ci_not_green",
            "sub_scores": {"audit": 0.0, "test_erosion": 0.0},
            "audit_findings": [
                {"severity": "high", "summary": "Token baked into VM image."},
            ],
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    suffix = await _spawn_and_get_stabilize_suffix(project, db_session)
    assert "hard blocker (ci_not_green)" in suffix
    assert "the audit sub-score is the binding constraint on promotion" not in suffix


@pytest.mark.asyncio
async def test_stabilize_hint_excludes_disabled_smoketest_from_binding(
    db_session, mocker, _lts_split_active
):
    """A disabled smoketest dimension carries a 0.0 placeholder that must not be
    treated as the binding constraint — audit (the lowest REAL dimension) is."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={
            "status": "not_promoted",
            "confidence": 0.6,
            "blocker": None,
            "smoketest_enabled": False,
            "sub_scores": {"audit": 0.55, "test_erosion": 0.90, "smoketest": 0.0},
            "audit_findings": [
                {"severity": "high", "summary": "Token baked into VM image."},
            ],
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    suffix = await _spawn_and_get_stabilize_suffix(project, db_session)
    # audit (0.55) is the lowest REAL dimension; the smoketest 0.0 placeholder
    # must not win the binding determination.
    assert "the audit sub-score is the binding constraint on promotion" in suffix
    assert "smoketest sub-score" not in suffix


@pytest.mark.asyncio
async def test_qualification_substate_hint_promotion_in_flight(
    db_session, mocker, _lts_split_active
):
    """status='pending' WITH promotion_pending (audit passed, promotion PR open)
    must be surfaced as PROMOTION_IN_FLIGHT — not read as 'audit still running'.
    Regression for incident 2026-06-02 where the agent waited on a promotion it
    should have been watching."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={
            "status": "pending",
            "promotion_pending": {
                "pr_number": 7357,
                "pr_url": "https://github.com/x/y/pull/7357",
                "auto_merge_enabled": True,
                "enabled_at": (now - timedelta(minutes=30)).isoformat(),
            },
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    assert await _spawn_strategic_review(project, db_session) is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "PROMOTION_IN_FLIGHT" in suffix, f"Expected sub-state hint; got: {suffix}"
    assert "already cleared" in suffix
    assert "still running" in suffix  # the explicit "do NOT read as ..." clause

    vp = payload.get("version_phase") or {}
    qstate = vp.get("qualification_state", {})
    assert qstate.get("state") == "promotion_in_flight"
    # The additive freshness context flows through to the strategic payload.
    assert qstate.get("promotion_age_seconds", 0) >= 1700  # ~30 min
    assert "promotion_past_timeout" in qstate
    assert "promotion_timeout_seconds" in qstate


@pytest.mark.asyncio
async def test_qualification_substate_hint_promotion_failed(
    db_session, mocker, _lts_split_active
):
    """status='promotion_failed' (passed audit, merge failed) must be surfaced as
    PROMOTION_FAILED with the failure reason and a 'fresh qualify' directive —
    not left to read as a still-pending audit."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={
            "status": "promotion_failed",
            "error": "pr_ci_red",
            "detail": "PR #7357 CI conclusively failed",
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    assert await _spawn_strategic_review(project, db_session) is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "PROMOTION_FAILED" in suffix, f"Expected sub-state hint; got: {suffix}"
    assert "pr_ci_red" in suffix
    assert "fresh qualify" in suffix.lower()

    vp = payload.get("version_phase") or {}
    assert vp.get("qualification_state", {}).get("state") == "promotion_failed"


@pytest.mark.asyncio
async def test_qualification_substate_hint_absent_while_auditing(
    db_session, mocker, _lts_split_active
):
    """Bare status='pending' (audit genuinely scoring) must NOT emit the
    promotion sub-state hint — the agent SHOULD wait in that case."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={"status": "pending"},
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    assert await _spawn_strategic_review(project, db_session) is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    payload = tasks[0].payloads[0]
    suffix = payload.get("prompt_suffix", "")
    assert "PROMOTION_IN_FLIGHT" not in suffix
    assert "PROMOTION_FAILED" not in suffix
    vp = payload.get("version_phase") or {}
    assert vp.get("qualification_state", {}).get("state") == "qualifying"


@pytest.mark.asyncio
async def test_qualification_substate_hint_suppressed_when_verdict_supersedes_pending(
    db_session, mocker, _lts_split_active
):
    """When a finalized QualificationVerdict (terminal_status='promoted') exists
    and last_qualification still says 'pending'+'promotion_pending', the resolver
    substitutes the effective status to 'promoted'.  The prompt-suffix hint must
    use the resolver-applied qualification_state (NOT the raw last_qualification)
    so PROMOTION_IN_FLIGHT is absent — the promotion already resolved."""
    from app.models import QualificationVerdict, ReleaseVersion
    from sqlalchemy.orm.attributes import flag_modified

    await _ensure_releaseversion_table(db_session)
    await _ensure_verdict_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    sha = "f" * 40
    db_session.add(
        QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha=sha,
            confidence=0.92,
            terminal_status="promoted",
            would_promote=True,
            promotion_enabled=False,
            finalized_at=now,
        )
    )
    # Stale last_qualification: still shows pending+promotion_pending even though
    # the verdict is already finalized.
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": sha,
        "confidence": 0.92,
        "promotion_pending": {
            "pr_number": 9002,
            "pr_url": "https://github.com/x/y/pull/9002",
            "auto_merge_enabled": True,
            "enabled_at": now.isoformat(),
        },
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    assert await _spawn_strategic_review(project, db_session) is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "PROMOTION_IN_FLIGHT" not in suffix, (
        "prompt-suffix must NOT assert PROMOTION_IN_FLIGHT when the verdict "
        f"has already finalized as promoted; got suffix: {suffix!r}"
    )
    # The version_phase qualification_state must reflect the resolved outcome
    vp = payload.get("version_phase") or {}
    qs = vp.get("qualification_state", {})
    assert qs.get("state") == "promoted", (
        f"qualification_state must be resolver-applied 'promoted', got: {qs}"
    )


@pytest.mark.asyncio
async def test_stabilize_findings_hint_softens_when_remediation_direction_exists(
    db_session, mocker, _lts_split_active
):
    """When an active audit-mode direction already exists, the hint must NOT
    demand a new one — it should instead tell the agent to confirm the existing
    direction targets the findings."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
        directions=[
            {
                "id": "d-existing",
                "text": "Fix the release audit findings",
                "status": "active",
                "priority": "high",
                "recommended_mode": "audit",
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        qualification_attempts=2,
        last_qualification={
            "status": "not_promoted",
            "confidence": 0.6971,
            "blocker": None,
            "sub_scores": {"audit": 0.47, "test_erosion": 1.0},
            "audit_findings": [
                {"severity": "high", "summary": "Token TTL rollout gap."},
            ],
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    suffix = tasks[0].payloads[0].get("prompt_suffix", "")
    assert "Token TTL rollout gap" in suffix
    assert "already have an active audit/fundamentals-mode direction" in suffix
    assert "MUST create a new direction" not in suffix, (
        f"Should not demand a new direction when one exists; got: {suffix}"
    )


@pytest.mark.asyncio
async def test_post_promote_handoff_hint_fires_on_fresh_version(
    db_session, mocker, _lts_split_active
):
    """A version that just opened from a promotion (version_number > 1, never
    qualified) gets a prompt_suffix asserting the prior version promoted and
    directing the strategist to author the new version's goal via new_version_goal."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification=None,
        qualification_attempts=0,
        goal=None,
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "Version handoff" in suffix, (
        f"Expected post-promotion handoff hint; got: {suffix}"
    )
    assert "intent.current_focus" in suffix
    assert "v2" in suffix and "v1" in suffix
    # The hint must ASSERT the prior version promoted and FORBID re-promoting it,
    # then direct the strategist to author the new version goal via new_version_goal.
    assert "PROMOTED" in suffix
    assert "do NOT" in suffix
    assert "new_version_goal" in suffix
    # Setup and retirement are independent — the hint must signal they can run in parallel.
    assert "concurrent" in suffix
    assert "parallel" in suffix


@pytest.mark.asyncio
async def test_post_promote_handoff_hint_grounds_in_prior_promoted_record(
    db_session, mocker, _lts_split_active
):
    """When the prior version's promoted record is readable, the handoff hint
    states the concrete promotion — its date and final confidence — so the agent
    gets a positive 'it shipped' signal rather than only a 'stale text' warning,
    and is explicitly forbidden from re-qualifying the already-shipped version."""
    from datetime import timedelta
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    promoted_at = now - timedelta(hours=3)
    prior = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=5),
        promoted_at=promoted_at,
        last_qualification={"status": "promoted", "confidence": 0.93},
    )
    db_session.add(prior)
    fresh = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification=None,
        qualification_attempts=0,
        goal="Promote v1: re-requesting qualification now that findings are fixed",
    )
    db_session.add(fresh)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    suffix = tasks[0].payloads[0].get("prompt_suffix", "")

    assert "Version handoff" in suffix
    # Grounded in the real promoted record: concrete date + final verdict.
    assert promoted_at.date().isoformat() in suffix, (
        f"Expected the prior version's promotion date in the hint; got: {suffix}"
    )
    assert "confidence 0.93" in suffix
    # And the binding instruction: do not re-qualify the already-shipped version.
    assert "re-qualify v1" in suffix


@pytest.mark.asyncio
async def test_post_promote_handoff_respects_existing_fresh_goal(
    db_session, mocker, _lts_split_active
):
    """A fresh post-promotion version can already have its goal authored
    by the time the strategic review spawns. The handoff suffix must not carry
    stale null-goal instructions that contradict version_phase."""
    from datetime import timedelta
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    promoted_at = now - timedelta(hours=2)
    prior = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=4),
        promoted_at=promoted_at,
        last_qualification={"status": "promoted", "confidence": 0.96},
        serving_status="serving_confirmed",
        served_sha="abc123def456",
        served_at=promoted_at,
    )
    fresh_goal = "Make lifecycle context recovery reliable after promotion."
    fresh = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification=None,
        qualification_attempts=0,
        goal=fresh_goal,
    )
    db_session.add_all([prior, fresh])
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    vp = payload.get("version_phase") or {}
    assert vp.get("goal") == fresh_goal

    suffix = payload.get("prompt_suffix", "")
    assert "Version handoff" in suffix
    assert fresh_goal in suffix or vp.get("goal") == fresh_goal
    assert "goal field on v2 is null" not in suffix
    assert "new_version_goal" not in suffix


@pytest.mark.asyncio
async def test_post_promote_handoff_hint_self_clears_after_first_qualification(
    db_session, mocker, _lts_split_active
):
    """Once a version has been qualified at least once, the handoff hint must
    no longer fire — the agent should be reading real verdict data, not
    refreshing the carried narrative."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={"status": "not_promoted", "confidence": 0.85},
        qualification_attempts=1,
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "Version handoff" not in suffix, (
        f"Handoff hint must self-clear once a verdict exists; got: {suffix}"
    )


@pytest.mark.asyncio
async def test_post_promote_handoff_hint_skipped_for_bootstrap_version(
    db_session, mocker, _lts_split_active
):
    """The bootstrap version (v1) didn't open from any prior promotion, so
    the handoff hint must NOT fire even when last_qualification is null."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification=None,
        qualification_attempts=0,
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "Version handoff" not in suffix, (
        f"v1 has no prior promotion; handoff hint must not fire. Got: {suffix}"
    )


@pytest.mark.asyncio
async def test_post_promote_handoff_unknown_serving_status_is_not_live(
    db_session, mocker, _lts_split_active
):
    """If the prior promoted record cannot be read, the handoff must fail
    closed instead of implying the previous version is live."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    db_session.add(
        ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="build",
            status="open",
            opened_at=now,
            phase_changed_at=now,
            last_qualification=None,
            qualification_attempts=0,
            goal="carry-over promotion narrative",
        )
    )
    await db_session.commit()

    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.release_version.recent_closed_versions",
        side_effect=RuntimeError("lookup failed"),
    )

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    suffix = tasks[0].payloads[0].get("prompt_suffix", "")

    assert "Version handoff" in suffix
    assert "is now live" not in suffix
    assert "serving evidence is unknown or unreadable" in suffix
    assert (
        "Verify lifecycle/deploy state before treating the previous version as live"
        in suffix
    )
    assert "serving_confirmed" in suffix


@pytest.mark.asyncio
async def test_failed_deploy_directive_renders_after_next_version_qualification_started(
    db_session, mocker, _lts_split_active
):
    """Failed-deploy directive must keep rendering even after the next version
    has begun qualification — until a subsequent version is promoted and confirmed
    serving. This ensures the recovery priority remains visible to the strategist
    even when the next version is building/qualifying."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    # v1: promoted but deployment failed
    prior_version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=5),
        promoted_at=now - timedelta(hours=3),
        last_qualification={"status": "promoted", "confidence": 0.93},
        serving_status="deploy_failed",
        deploy_failure_reason="Cloud Run deployment timed out",
    )
    db_session.add(prior_version)
    # v2: open and already started qualification (condition that used to clear handoff)
    current_version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={"status": "not_promoted", "confidence": 0.75},
        qualification_attempts=1,  # Has started qualifying
    )
    db_session.add(current_version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    # The post-promotion handoff must have self-cleared (fresh version has qualified)
    assert "Version handoff" not in suffix, (
        f"Post-promotion handoff should self-clear after first qualification; got: {suffix}"
    )
    # But the failed-deploy directive must be present
    assert "deployment crisis" in suffix.lower() or "failed deploy" in suffix.lower(), (
        f"Failed-deploy directive must render even after next version has qualified; got: {suffix}"
    )
    assert "v1" in suffix, f"Directive must reference the failed v1; got: {suffix}"
    assert "deploy_failed" in suffix or "DID NOT confirm serving" in suffix, (
        f"Directive must reference deployment failure; got: {suffix}"
    )
    assert "Cloud Run deployment timed out" in suffix, (
        f"Directive must include failure reason; got: {suffix}"
    )


@pytest.mark.asyncio
async def test_failed_deploy_directive_clears_when_newer_version_serving_confirmed(
    db_session, mocker, _lts_split_active
):
    """Failed-deploy directive must clear once a subsequent version is promoted
    AND confirmed serving. This prevents the recovery priority from lingering
    after it has actually been resolved by a newer promotion."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    # v1: promoted but deployment failed
    failed_version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=10),
        promoted_at=now - timedelta(days=5),
        last_qualification={"status": "promoted", "confidence": 0.93},
        serving_status="deploy_failed",
        deploy_failure_reason="Cloud Run crashed on startup",
    )
    db_session.add(failed_version)
    # v2: promoted and serving confirmed (recovery successful)
    recovery_version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=5),
        promoted_at=now - timedelta(days=1),
        last_qualification={"status": "promoted", "confidence": 0.95},
        serving_status="serving_confirmed",
        served_sha="abc123def456",
    )
    db_session.add(recovery_version)
    # v3: open and ready for next iteration
    current_version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="build",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification=None,
        qualification_attempts=0,
    )
    db_session.add(current_version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    # The failed-deploy directive must NOT be present because v2 is the latest
    # promoted version and it is serving_confirmed (not deploy_failed)
    assert (
        "deployment crisis" not in suffix.lower() and "deploy_failed" not in suffix
    ), (
        f"Failed-deploy directive must clear when recovery is confirmed serving; got: {suffix}"
    )


@pytest.mark.asyncio
async def test_failed_deploy_directive_text_states_same_sha_recovery_clears(
    db_session, mocker, _lts_split_active
):
    """The crisis directive text must correctly state that confirming THIS deploy
    serving (a recovery of the SAME promoted SHA) clears it — not only a
    subsequent promotion. The old wording wrongly implied a same-SHA recovery
    could never clear it."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    failed_version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=5),
        promoted_at=now - timedelta(hours=3),
        last_qualification={"status": "promoted", "confidence": 0.93},
        serving_status="deploy_failed",
        deploy_failure_reason="boot crash, recovered late",
    )
    db_session.add(failed_version)
    # An open v2 — the directive only fires while a version is open.
    db_session.add(
        ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="build",
            status="open",
            opened_at=now,
            phase_changed_at=now,
            last_qualification=None,
            qualification_attempts=0,
        )
    )
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    suffix = tasks[0].payloads[0].get("prompt_suffix", "")

    assert "deployment crisis" in suffix.lower(), f"directive missing; got: {suffix}"
    # The corrected wording: same-SHA recovery clears it; no subsequent version
    # is required.
    assert "SAME promoted SHA" in suffix, (
        f"directive must state same-SHA recovery clears it; got: {suffix}"
    )
    assert "do NOT need to cut and promote a subsequent" in suffix, (
        f"directive must not imply only a subsequent version clears it; got: {suffix}"
    )
    # The misleading old phrasing must be gone.
    assert (
        "remains active until a subsequent version is promoted AND confirmed serving"
        not in suffix
    )


@pytest.mark.asyncio
async def test_failed_deploy_directive_clears_on_same_sha_recovery(
    db_session, mocker, _lts_split_active
):
    """A same-SHA recovery (the single promoted version's serving_status flips
    deploy_failed -> serving_confirmed, no subsequent version) clears the crisis
    directive — the directive is computed at view time from serving_status."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    recovered_version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=5),
        promoted_at=now - timedelta(hours=6),
        last_qualification={"status": "promoted", "confidence": 0.93},
        # Recovery flipped the SAME promoted SHA back to serving_confirmed.
        serving_status="serving_confirmed",
        served_sha="abc123def456",
    )
    db_session.add(recovered_version)
    # An open v2 so the directive WOULD fire if v1 were still deploy_failed —
    # making the "directive cleared" assertion meaningful rather than vacuous.
    db_session.add(
        ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="build",
            status="open",
            opened_at=now,
            phase_changed_at=now,
            last_qualification=None,
            qualification_attempts=0,
        )
    )
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    suffix = tasks[0].payloads[0].get("prompt_suffix", "")

    assert (
        "deployment crisis" not in suffix.lower() and "deploy_failed" not in suffix
    ), f"Crisis directive must clear on a same-SHA recovery; got: {suffix}"


@pytest.mark.asyncio
async def test_stabilize_stall_hint_suppressed_for_dry_run(
    db_session, mocker, _lts_split_active
):
    """A dry-run hold (passing candidate, promotion_enabled=False) is healthy
    and must NOT trigger the stalled nudge."""
    from datetime import timedelta
    from app.models import ReleaseVersion
    from app.release_qualifier import _STABILIZE_STALL_AGE_DAYS

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=now - timedelta(days=_STABILIZE_STALL_AGE_DAYS + 5),
        phase_changed_at=now - timedelta(days=_STABILIZE_STALL_AGE_DAYS + 5),
        last_qualification={"status": "dry_run", "candidate_sha": "abc123"},
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "STALLED" not in suffix, (
        "dry_run hold should suppress the stabilize-stall nudge"
    )
    vp = payload.get("version_phase") or {}
    assert "stabilize_stall" not in vp, (
        "stabilize_stall block must not appear for dry_run hold"
    )


# ── build-stall strategic nudge ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_stall_nudge_counts_build_phase_merges_not_version_lifetime(
    db_session, mocker, _lts_split_active
):
    """A version that cycled build→stabilize→build must NOT fire the build-stall
    nudge prematurely based on the inflated version-lifetime merge count.

    Setup: version re-entered the build phase recently (phase_changed_at = now).
    There are many pre-phase-change tasks (version-lifetime count > threshold)
    but zero post-phase-change tasks (phase-scoped count = 0).  The nudge must
    NOT fire because neither age nor phase-scoped count exceed the threshold.
    """
    from app.models import ReleaseVersion
    from app.release_qualifier import _BUILD_STALL_MERGED_TASKS

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    # Version just re-entered build phase (age = 0 days)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=5,
        phase="build",
        status="open",
        opened_at=now - timedelta(days=60),
        phase_changed_at=now,  # just re-entered build from stabilize
    )
    db_session.add(version)
    await db_session.commit()

    # Seed tasks merged BEFORE the phase transition (pre-phase-change, version-lifetime)
    pre_phase_count = _BUILD_STALL_MERGED_TASKS + 5
    for i in range(pre_phase_count):
        t = Task(
            project_id=project.id,
            title=f"Pre-phase task {i}",
            description="test",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_merged": True, "direction_id": "d-old"},
        )
        t.updated_at = now - timedelta(days=30 + i)
        db_session.add(t)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "BUILD — STALLED" not in suffix, (
        "build-stall nudge must NOT fire when phase-scoped count is 0 "
        f"(only version-lifetime count {pre_phase_count} exceeds threshold); got: {suffix}"
    )
    vp = payload.get("version_phase") or {}
    assert "build_stall" not in vp, (
        "build_stall block must not appear when phase-scoped merge count is below threshold"
    )


@pytest.mark.asyncio
async def test_build_stall_nudge_tasks_merged_field_is_phase_scoped(
    db_session, mocker, _lts_split_active
):
    """When the build-stall nudge fires, version_phase['build_stall']['tasks_merged']
    must equal only the post-phase_changed_at merge count, not the version-lifetime count.
    """
    from app.models import ReleaseVersion
    from app.release_qualifier import _BUILD_STALL_AGE_DAYS

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    # Version re-entered build phase past the age threshold
    phase_entry = now - timedelta(days=_BUILD_STALL_AGE_DAYS + 2)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=6,
        phase="build",
        status="open",
        opened_at=now - timedelta(days=60),
        phase_changed_at=phase_entry,
    )
    db_session.add(version)
    await db_session.commit()

    # Tasks merged BEFORE phase re-entry (should NOT be counted by nudge)
    for i in range(10):
        t = Task(
            project_id=project.id,
            title=f"Pre-entry task {i}",
            description="test",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_merged": True, "direction_id": "d-old"},
        )
        t.updated_at = phase_entry - timedelta(days=1 + i)
        db_session.add(t)

    # Tasks merged AFTER phase re-entry (should be counted)
    post_phase_count = 3
    for i in range(post_phase_count):
        t = Task(
            project_id=project.id,
            title=f"Post-entry task {i}",
            description="test",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_merged": True, "direction_id": "d-new"},
        )
        t.updated_at = phase_entry + timedelta(hours=1 + i)
        db_session.add(t)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    # Nudge fires because age threshold is exceeded
    suffix = payload.get("prompt_suffix", "")
    assert "BUILD — STALLED" in suffix, (
        f"Expected build-stall nudge to fire on age; got: {suffix}"
    )

    vp = payload.get("version_phase") or {}
    stall_block = vp.get("build_stall")
    assert stall_block is not None, "build_stall block must appear in version_phase"
    reported_merged = stall_block.get("tasks_merged")
    assert reported_merged == post_phase_count, (
        f"build_stall.tasks_merged should be phase-scoped count {post_phase_count}, "
        f"not version-lifetime count; got {reported_merged}"
    )


# ── outcome_evaluation read path for success_evidence/retirement_condition ────


@pytest.mark.asyncio
async def test_outcome_evaluation_includes_success_evidence_and_retirement_condition(
    db_session,
):
    """outcome_evaluation in strategic payload includes success_evidence and retirement_condition when set."""
    long_evidence = "E" * 700
    project = _make_project(
        directions=[
            {
                "id": "d-oe-ev01",
                "status": "active",
                "text": "Direction with outcome criteria",
                "priority": "high",
                "context": {
                    "success_evidence": long_evidence,
                    "retirement_condition": "  retire when fully satisfied  ",
                },
                "research_history": [
                    {
                        "prs_merged": 2,
                        "prs_rejected": 0,
                        "tasks_created": 5,
                        "tasks_failed": 0,
                    }
                ],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]

    assert "outcome_evaluation" in payload
    outcome_eval = payload.get("outcome_evaluation", {})
    directions = outcome_eval.get("directions", [])

    assert len(directions) > 0, "outcome_evaluation should have directions"
    outcome = next((o for o in directions if o["id"] == "d-oe-ev01"), None)
    assert outcome is not None, "Direction should be in outcome_evaluation"
    # success_evidence should be capped at 500 chars
    assert outcome.get("success_evidence") == "E" * 500
    # retirement_condition should be stripped and capped
    assert outcome.get("retirement_condition") == "retire when fully satisfied"


@pytest.mark.asyncio
async def test_outcome_evaluation_omits_fields_when_not_set(db_session):
    """outcome_evaluation omits success_evidence/retirement_condition when not in context."""
    project = _make_project(
        directions=[
            {
                "id": "d-oe-ev02",
                "status": "active",
                "text": "Direction without outcome criteria",
                "priority": "medium",
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    outcome_eval = payload.get("outcome_evaluation", {})
    directions = outcome_eval.get("directions", [])

    outcome = next((o for o in directions if o["id"] == "d-oe-ev02"), None)
    assert outcome is not None
    assert "success_evidence" not in outcome
    assert "retirement_condition" not in outcome


@pytest.mark.asyncio
async def test_outcome_evaluation_handles_empty_strings(db_session):
    """outcome_evaluation omits empty/whitespace-only success_evidence/retirement_condition."""
    project = _make_project(
        directions=[
            {
                "id": "d-oe-ev03",
                "status": "active",
                "text": "Direction with empty criteria",
                "priority": "low",
                "context": {
                    "success_evidence": "   ",  # whitespace only
                    "retirement_condition": "",  # empty
                },
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    outcome_eval = payload.get("outcome_evaluation", {})
    directions = outcome_eval.get("directions", [])

    outcome = next((o for o in directions if o["id"] == "d-oe-ev03"), None)
    assert outcome is not None
    assert "success_evidence" not in outcome
    assert "retirement_condition" not in outcome


# ── on_automain flag in _build_recent_interventions ──────────────────────────


@pytest.mark.asyncio
async def test_on_automain_false_for_root_task_when_unverified(db_session):
    """Root tasks (no parent) report on_automain=False when ancestry cannot be verified.

    When GitHub credentials are unavailable the ancestry check is skipped and the
    task is NOT assumed to have landed on the integration branch (fail-closed).
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Direct fix",
        description="No parent",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True},
    )
    db_session.add(task)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_true_when_rollup_merged(db_session):
    """Child task reports on_automain=True when its rollup parent's pr_merged=True."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Rollup parent",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "rollup_pr_number": 42},
    )
    db_session.add(parent)
    await db_session.flush()

    child = Task(
        project_id=project.id,
        title="Child fix",
        description="child",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        parent_task_id=parent.id,
        context={"pr_merged": True},
    )
    db_session.add(child)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    by_title = {m["pr_title"]: m for m in merges}
    # Child is on automain via the parent's pr_merged DB flag (fail-open for child
    # tasks is intentional — the flag is a DB-recorded rollup-merge event).
    assert by_title["Child fix"]["on_automain"] is True
    # Rollup parent is a root task with no credentials → ancestry unverifiable → False.
    assert by_title["Rollup parent"]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_false_when_rollup_not_merged(db_session):
    """Child task reports on_automain=False when its rollup parent is not yet merged."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Unmerged rollup",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        context={"rollup_pr_number": 99},
    )
    db_session.add(parent)
    await db_session.flush()

    child = Task(
        project_id=project.id,
        title="Staged fix",
        description="child merged into staging only",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        parent_task_id=parent.id,
        context={"pr_merged": True},
    )
    db_session.add(child)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "Staged fix"
    assert merges[0]["on_automain"] is False


# ── landed_on_integration field in _build_recent_interventions ───────────────


@pytest.mark.asyncio
async def test_merges_out_landed_on_integration_false(db_session):
    """merges[] entries expose landed_on_integration=False when the context key is
    explicitly False (merged to rollup staging branch, not integration branch)."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Rollup-only fix",
        description="merged to staging only",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "landed_on_integration": False},
    )
    db_session.add(task)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["landed_on_integration"] is False


@pytest.mark.asyncio
async def test_merges_out_landed_on_integration_true(db_session):
    """merges[] entries expose landed_on_integration=True when the context key is
    explicitly True (confirmed merge to the integration branch)."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Integration fix",
        description="landed on integration",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "landed_on_integration": True},
    )
    db_session.add(task)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["landed_on_integration"] is True


@pytest.mark.asyncio
async def test_merges_out_landed_on_integration_legacy_none(db_session):
    """merges[] entries expose landed_on_integration=None for legacy rows that
    predate the landed_on_integration context key — backward compatible."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Legacy fix",
        description="old row without landed_on_integration key",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True},
    )
    db_session.add(task)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["landed_on_integration"] is None


@pytest.mark.asyncio
async def test_recent_merged_titles_landed_on_integration(db_session):
    """per_direction_delta recent_merged_titles entries expose landed_on_integration
    alongside on_automain and merge_verdict."""
    d_id = "d-test-loi"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[{"id": d_id, "text": "test direction", "status": "active"}],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task merged to rollup staging only (landed_on_integration=False)
    task = Task(
        project_id=project.id,
        title="Staging-only fix",
        description="merged to staging branch only",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "pr_merged": True,
            "direction_id": d_id,
            "landed_on_integration": False,
        },
    )
    db_session.add(task)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    delta = {d["direction_id"]: d for d in result["per_direction_delta"]}
    assert d_id in delta
    titles = delta[d_id].get("recent_merged_titles", [])
    assert len(titles) == 1
    assert titles[0]["landed_on_integration"] is False
    assert titles[0]["title"] == "Staging-only fix"


# ── on_main / undeployed_directions + deployment_state block ──────────────────


@pytest.mark.asyncio
async def test_on_main_mirrors_on_automain_without_lts_split(db_session):
    """With no LTS split (integration branch == promotion target), on_main equals
    on_automain and undeployed_directions is empty — there is no 'landed but not
    deployed' state when merges deploy directly."""
    # No LTS split: integration branch IS the promotion target. _make_project now
    # defaults to a split (automain/main), so request matching branches explicitly.
    project = _make_project(
        vision={"purpose": "test"},
        integration_branch="main",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Rollup parent",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "rollup_pr_number": 42},
    )
    db_session.add(parent)
    await db_session.flush()
    child = Task(
        project_id=project.id,
        title="Child fix",
        description="child",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        parent_task_id=parent.id,
        context={"pr_merged": True},
    )
    db_session.add(child)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    by_title = {m["pr_title"]: m for m in result["merges"]}
    # on_main == on_automain when there is no split.
    assert (
        by_title["Child fix"]["on_main"] == by_title["Child fix"]["on_automain"] is True
    )
    # No split → nothing can be "landed but not deployed".
    assert result["undeployed_directions"] == []


@pytest.mark.asyncio
async def test_undeployed_directions_lists_integration_only_merges(db_session, mocker):
    """Under an LTS split, a merge reachable from the integration branch but NOT
    from the promotion target is surfaced in undeployed_directions, grouped by
    direction."""
    from unittest.mock import AsyncMock

    # Integration-branch ancestry True, promotion-target ancestry False.
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"merge_commit_sha": "deadbeefcafe"}),
    )
    # is_ancestor_of(token, owner, repo, sha, branch, project_id=...) — branch is a[4].
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(side_effect=lambda *a, **k: a[4] == "automain"),
    )

    project = _make_project(
        integration_branch="automain",
        promotion_target="main",
        directions=[{"id": "d-x", "status": "active", "text": "warm pool reuse"}],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Wire warm pool ready signal",
        description="root merge on automain only",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 7, "direction_id": "d-x"},
    )
    db_session.add(task)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    merge = result["merges"][0]
    assert merge["on_automain"] is True
    assert merge["on_main"] is False
    undeployed = result["undeployed_directions"]
    assert len(undeployed) == 1
    assert undeployed[0]["direction_id"] == "d-x"
    assert undeployed[0]["direction_text"] == "warm pool reuse"
    assert "Wire warm pool ready signal" in undeployed[0]["merge_titles"]


@pytest.mark.asyncio
async def test_deployment_state_block_in_payload_when_split(db_session, mocker):
    """The strategic payload carries a deployment_state block under an LTS split:
    deployed SHA/status from DeploymentState, the promoted vs open version, and
    automain_ahead_of_main derived from un-promoted merged work."""
    from datetime import timedelta
    from app.models import ReleaseVersion, DeploymentState

    await _ensure_releaseversion_table(db_session)
    _patch_spawning_deps(mocker)
    # The DeploymentState read is now gated on the deploy-level identity (not
    # is_self_repo): make this project the deploy identity so its live container
    # SHA/status is observable to the strategist.
    mocker.patch.dict("os.environ", {"DEPLOY_REPO_IDENTITY": "acme/widget"})

    project = _make_project(
        is_self_repo=True,
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    db_session.add(
        ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="promoted",
            opened_at=now - timedelta(days=10),
            promoted_at=now - timedelta(days=2),
            promoted_sha="promotedsha0001",
        )
    )
    db_session.add(
        ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="build",
            status="open",
            opened_at=now - timedelta(hours=1),
        )
    )
    # A merged task during the open (un-promoted) version → ahead of main.
    db_session.add(
        Task(
            project_id=project.id,
            title="Merged during v2",
            description="x",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_merged": True},
            updated_at=now,
        )
    )
    # Live deployed state (self-repo container tracker).
    db_session.add(
        DeploymentState(
            id=1,
            current_sha="deadbeefcafe9999",
            current_revision="rev-123",
            status="STABLE",
        )
    )
    await db_session.commit()

    assert await _spawn_strategic_review(project, db_session) is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    )

    ds = payload.get("deployment_state")
    assert ds is not None, "deployment_state block missing under an LTS split"
    assert ds["integration_branch"] == "automain"
    assert ds["promotion_target"] == "main"
    assert ds["deployed_sha"] == "deadbeefcafe"  # DeploymentState.current_sha, 12-char
    assert ds["deploy_status"] == "STABLE"
    assert ds["promoted_version"] == 1
    assert ds["open_version"] == 2
    assert ds["open_status"] == "open"
    assert ds["tasks_merged_unpromoted"] >= 1
    assert ds["automain_ahead_of_main"] is True


@pytest.mark.asyncio
async def test_deployment_state_and_failed_directive_use_aggregate_failed_truth(
    db_session, mocker, _lts_split_active
):
    """A required non-backend artifact failure must keep the strategist in
    deploy-failed mode even when the backend artifact is serving_confirmed."""
    from app import release_artifacts as artifact_registry
    from app.models import ReleaseDeployArtifact, ReleaseVersion
    from app.release_artifacts import (
        BACKEND_REVISION_ARTIFACT_KIND,
        ReleaseArtifactProvider,
    )

    await _ensure_releaseversion_table(db_session)
    _patch_spawning_deps(mocker)

    backend_provider = artifact_registry._ARTIFACT_PROVIDERS[
        BACKEND_REVISION_ARTIFACT_KIND
    ]

    async def _check_synthetic(_session, _version, _artifact, _now):
        return None

    mocker.patch.dict(
        artifact_registry._ARTIFACT_PROVIDERS,
        {
            BACKEND_REVISION_ARTIFACT_KIND: backend_provider,
            "synthetic_required": ReleaseArtifactProvider(
                artifact_kind="synthetic_required",
                check=_check_synthetic,
                required=True,
            ),
        },
        clear=True,
    )

    project = _make_project(
        is_self_repo=True,
        integration_branch="automain",
        promotion_target="main",
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    failed_version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=5),
        promoted_at=now - timedelta(hours=3),
        last_qualification={"status": "promoted", "confidence": 0.93},
        serving_status="serving_confirmed",
        served_sha="abc123def456",
    )
    current_version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={"status": "not_promoted", "confidence": 0.75},
        qualification_attempts=1,
    )
    db_session.add_all([failed_version, current_version])
    await db_session.commit()
    await db_session.refresh(failed_version)

    db_session.add_all(
        [
            ReleaseDeployArtifact(
                version_id=failed_version.id,
                artifact_kind=BACKEND_REVISION_ARTIFACT_KIND,
                serving_status="serving_confirmed",
                served_identifier="abc123def456",
                served_at=now - timedelta(hours=3),
            ),
            ReleaseDeployArtifact(
                version_id=failed_version.id,
                artifact_kind="synthetic_required",
                serving_status="deploy_failed",
                reason_code="synthetic_deadline_elapsed",
            ),
        ]
    )
    await db_session.commit()

    assert await _spawn_strategic_review(project, db_session) is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    )

    deployment_state = payload.get("deployment_state") or {}
    assert deployment_state["deployed_sha"] is None
    assert deployment_state["promoted_serving_status"] == "deploy_failed"
    assert (
        deployment_state["promoted_deploy_failure_reason"]
        == "synthetic_deadline_elapsed"
    )
    assert "synthetic_required" in (deployment_state["promoted_artifact_gap"] or "")

    suffix = payload.get("prompt_suffix", "")
    assert "deployment crisis" in suffix.lower(), suffix
    assert "synthetic_required" in suffix, suffix


@pytest.mark.asyncio
async def test_deployment_state_absent_without_lts_split(db_session, mocker):
    """A single-branch project (integration branch == promotion target) emits no
    deployment_state block — there is no integration-vs-deployed distinction."""
    await _ensure_releaseversion_table(db_session)
    _patch_spawning_deps(mocker)

    project = _make_project(
        is_self_repo=True,
        integration_branch="trunk",
        promotion_target="trunk",
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert await _spawn_strategic_review(project, db_session) is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    )
    assert "deployment_state" not in payload


async def _spawn_self_repo_with_deployment_state(
    db_session,
    mocker,
    *,
    status,
    rolled_back_from_sha=None,
    rollback_count=0,
    rollback_reason=None,
):
    """Spawn a self-repo strategic review under an LTS split with a seeded
    DeploymentState row. Returns (deployment_state block, prompt_suffix)."""
    from datetime import timedelta
    from app.models import ReleaseVersion, DeploymentState

    await _ensure_releaseversion_table(db_session)
    _patch_spawning_deps(mocker)
    # The DeploymentState read is now gated on the deploy-level identity (not
    # is_self_repo): make this project the deploy identity so its live container
    # state is observable to the strategist.
    mocker.patch.dict("os.environ", {"DEPLOY_REPO_IDENTITY": "acme/widget"})

    project = _make_project(
        is_self_repo=True,
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    db_session.add(
        ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="build",
            status="open",
            opened_at=now - timedelta(hours=1),
        )
    )
    db_session.add(
        DeploymentState(
            id=1,
            current_sha="deadbeefcafe9999",
            current_revision="rev-123",
            status=status,
            rolled_back_from_sha=rolled_back_from_sha,
            rollback_count=rollback_count,
            rollback_reason=rollback_reason,
        )
    )
    await db_session.commit()

    assert await _spawn_strategic_review(project, db_session) is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    )
    return payload.get("deployment_state"), payload.get("prompt_suffix", "")


@pytest.mark.asyncio
async def test_deployment_state_block_carries_rollback_provenance(db_session, mocker):
    """The deployment_state block surfaces the rollback provenance the operator
    UI already shows: rolled-back-from SHA (12-char), count, and reason."""
    ds, _suffix = await _spawn_self_repo_with_deployment_state(
        db_session,
        mocker,
        status="ROLLED_BACK",
        rolled_back_from_sha="badc0ffee0ddf00d",
        rollback_count=2,
        rollback_reason="health check failed after deploy",
    )
    assert ds is not None, "deployment_state block missing under an LTS split"
    assert ds["rolled_back_from_sha"] == "badc0ffee0dd"  # 12-char, like deployed_sha
    assert ds["rollback_count"] == 2
    assert ds["rollback_reason"] == "health check failed after deploy"


@pytest.mark.asyncio
async def test_stabilization_directive_fires_on_rolled_back(db_session, mocker):
    """A ROLLED_BACK deploy status injects a stabilization directive naming the
    reason + count and steering the strategist away from advancing focus."""
    _ds, suffix = await _spawn_self_repo_with_deployment_state(
        db_session,
        mocker,
        status="ROLLED_BACK",
        rolled_back_from_sha="badc0ffee0ddf00d",
        rollback_count=1,
        rollback_reason="health check failed after deploy",
    )
    assert "ROLLED_BACK" in suffix
    assert "STABILIZATION" in suffix
    assert "health check failed after deploy" in suffix
    assert "Consecutive rollbacks: 1" in suffix
    assert "badc0ffee0dd" in suffix
    assert "do NOT advance focus" in suffix


@pytest.mark.asyncio
async def test_stabilization_directive_fires_on_halted(db_session, mocker):
    """A HALTED deploy status injects the repeated-rollback-halt directive."""
    _ds, suffix = await _spawn_self_repo_with_deployment_state(
        db_session,
        mocker,
        status="HALTED",
        rolled_back_from_sha="badc0ffee0ddf00d",
        rollback_count=3,
        rollback_reason="repeated boot crash",
    )
    assert "HALTED" in suffix
    assert "STABILIZATION" in suffix
    assert "manual intervention" in suffix
    assert "Consecutive rollbacks: 3" in suffix
    assert "repeated-rollback halt" in suffix


@pytest.mark.asyncio
async def test_stabilization_directive_absent_when_stable(db_session, mocker):
    """A healthy (STABLE) deploy status emits no stabilization directive."""
    _ds, suffix = await _spawn_self_repo_with_deployment_state(
        db_session,
        mocker,
        status="STABLE",
    )
    assert "STABILIZATION" not in suffix
    assert "ROLLED_BACK" not in suffix
    assert "HALTED" not in suffix


@pytest.mark.asyncio
async def test_rolling_rates_exclude_rollup_children(db_session):
    """Rollup children (parent_task_id set) are not counted as merged in rolling_rates.

    A rollup child's pr_merged=True means its PR landed on the rollup staging branch,
    not the integration branch. Only the rollup parent (root task) should count
    toward the merged counter in rolling_rates when it lands on integration.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Rollup parent — merged to integration branch
    parent = Task(
        project_id=project.id,
        title="Rollup parent",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True},
    )
    db_session.add(parent)
    await db_session.flush()

    # Two rollup children — each merged to staging only
    for i in range(2):
        child = Task(
            project_id=project.id,
            title=f"Child fix {i}",
            description="child",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            parent_task_id=parent.id,
            context={"pr_merged": True},
        )
        db_session.add(child)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    rates = result["rolling_rates"]
    # Only the root parent counts as a merged task; the two children are staging-only
    assert rates["last_7d"]["merged"] == 1, (
        f"Expected 1 integration-landed merge (root task only), "
        f"got {rates['last_7d']['merged']}"
    )


@pytest.mark.asyncio
async def test_window_merge_rate_population_consistent_with_rollup_children(db_session):
    """Both numerator and denominator of merge_rate count only root tasks.

    When a rollup parent and its children all complete in the same window,
    the merge_rate denominator ('finished') must count only root tasks,
    consistent with the numerator ('merged'). If the denominator included
    rollup children, the rate would be artificially deflated: 1 root merged
    over 3 total (parent + 2 children) = 33% instead of 1 root over 1 root = 100%.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Rollup parent — merged to integration branch
    parent = Task(
        project_id=project.id,
        title="Rollup parent",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True},
    )
    db_session.add(parent)
    await db_session.flush()

    # Two rollup children — each merged to staging only
    for i in range(2):
        child = Task(
            project_id=project.id,
            title=f"Child fix {i}",
            description="child",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            parent_task_id=parent.id,
            context={"pr_merged": True},
        )
        db_session.add(child)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    rates = result["rolling_rates"]

    # merged should be 1 (root parent only)
    assert rates["last_7d"]["merged"] == 1, (
        f"Expected 1 merged (root task only), got {rates['last_7d']['merged']}"
    )
    # finished should also be 1 (root parent only), NOT 3 (parent + 2 children)
    assert rates["last_7d"]["finished"] == 1, (
        f"Expected 1 finished (root task only), got {rates['last_7d']['finished']}. "
        "Denominator must exclude rollup children for consistent merge_rate."
    )
    # merge_rate should be 1.0 (100%), NOT 0.333 (33%)
    assert rates["last_7d"]["merge_rate"] == 1.0, (
        f"Expected 100% merge_rate, got {rates['last_7d']['merge_rate'] * 100}%. "
        "Rate is deflated when denominator counts rollup children."
    )


@pytest.mark.asyncio
async def test_active_counts_include_old_inflight_tasks_outside_lookback(db_session):
    """Current in-flight work is counted even when the task predates the lookback window."""
    project = _make_project(
        vision={"purpose": "test"},
        directions=[{"id": "d-old", "status": "active", "text": "long-lived lane"}],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old_started = _utcnow_naive() - timedelta(days=35)
    db_session.add(
        Task(
            project_id=project.id,
            title="Long running task",
            description="still in flight",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            created_at=old_started,
            updated_at=old_started,
            context={"direction_id": "d-old", "scope": "large"},
        )
    )
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session, lookback_days=21)
    assert result["active_by_direction"] == {"d-old": 1}
    assert result["active_by_scope"]["large"] == 1


# ── git-ancestry landing verification in _build_recent_interventions ──────────


@pytest.mark.asyncio
async def test_on_automain_false_when_commit_not_ancestor(db_session, mocker):
    """pr_merged=True but commit is NOT an ancestor of automain → on_automain=False.

    The ancestry check returns False and there are no _pr_changed_files for the
    content-aware fallback, so the task is correctly reported as NOT landed.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Stranded fix",
        description="pr_merged=True but not on automain",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 201},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "merge_commit_sha": "stranded-sha",
                "head": {"sha": "head-sha"},
            }
        ),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.github.list_recent_commit_messages",
        new=AsyncMock(return_value=[]),
    )

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "Stranded fix"
    assert merges[0]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_true_when_commit_is_ancestor(db_session, mocker):
    """Commit IS a git-ancestor of automain → on_automain=True via ancestry check."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Landed fix",
        description="merge SHA is a real ancestor",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 202},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"merge_commit_sha": "landed-sha", "head": {"sha": "head-sha"}}
        ),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "Landed fix"
    assert merges[0]["on_automain"] is True


@pytest.mark.asyncio
async def test_on_automain_false_when_ancestry_api_indeterminate(db_session, mocker):
    """is_ancestor_of returning None (API error / diverged) → on_automain=False.

    Degrade-safe convention: indeterminate result must NOT be treated as landed.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Indeterminate fix",
        description="ancestry API returned None",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 203},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"merge_commit_sha": "some-sha", "head": {"sha": "head-sha"}}
        ),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=None),
    )

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "Indeterminate fix"
    assert merges[0]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_child_verified_via_parent_rollup_pr(db_session, mocker):
    """Rollup child: ancestry is verified against the PARENT's rollup PR, not the child's own.

    A rollup child's own PR merges into the rollup staging branch, so its merge
    commit is never an ancestor of the integration branch once the rollup PR
    squash-merges. The child is on automain iff the parent's rollup PR landed —
    verifying the child's own PR would (wrongly) report on_automain=False.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Rollup parent",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "rollup_pr_number": 500},
    )
    db_session.add(parent)
    await db_session.flush()

    child = Task(
        project_id=project.id,
        title="Rollup child fix",
        description="child merged into staging branch",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        parent_task_id=parent.id,
        context={"pr_merged": True, "pr_number": 999},
    )
    db_session.add(child)
    await db_session.commit()

    def _pr_side_effect(token, owner, repo, pr_num, project_id=None):
        return {
            999: {"merge_commit_sha": "child-merge-sha", "head": {"sha": "child-head"}},
            500: {
                "merge_commit_sha": "rollup-merge-sha",
                "head": {"sha": "rollup-head"},
            },
        }[pr_num]

    def _anc_side_effect(token, owner, repo, sha, ref, project_id=None):
        # The child's own merge commit lives on the staging branch (not an
        # ancestor); only the rollup PR's squash commit is on the integration
        # branch.
        return sha == "rollup-merge-sha"

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(side_effect=_pr_side_effect),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(side_effect=_anc_side_effect),
    )

    result = await _build_recent_interventions(project, db_session)
    by_title = {m["pr_title"]: m for m in result["merges"]}
    # Child is on automain because the PARENT's rollup PR #500 squash-landed —
    # not because the child's own PR #999 (staging-only) is an ancestor.
    assert by_title["Rollup child fix"]["on_automain"] is True
    assert by_title["Rollup parent"]["on_automain"] is True


@pytest.mark.asyncio
async def test_on_automain_child_false_when_parent_rollup_pr_not_ancestor(
    db_session, mocker
):
    """Rollup child: on_automain=False when the parent's rollup PR has not landed.

    The rollup PR's merge commit is not an ancestor of the integration branch
    (rollup merged into a staging branch that itself never reached automain), so
    the child is correctly reported as NOT landed.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Stranded rollup parent",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "rollup_pr_number": 501},
    )
    db_session.add(parent)
    await db_session.flush()

    child = Task(
        project_id=project.id,
        title="Stranded rollup child",
        description="child whose rollup never landed",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        parent_task_id=parent.id,
        context={"pr_merged": True, "pr_number": 998},
    )
    db_session.add(child)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "merge_commit_sha": "rollup-merge-sha",
                "head": {"sha": "rollup-head"},
            }
        ),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.github.list_recent_commit_messages",
        new=AsyncMock(return_value=[]),
    )

    result = await _build_recent_interventions(project, db_session)
    by_title = {m["pr_title"]: m for m in result["merges"]}
    assert by_title["Stranded rollup child"]["on_automain"] is False
    assert by_title["Stranded rollup parent"]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_false_for_root_task_no_pr_number(db_session, mocker):
    """Root task with no pr_number reports on_automain=False even with credentials.

    When credentials resolve but the task has no pr_number or rollup_pr_number in
    its context, no ancestry check can run for it.  The task must NOT be assumed
    landed (fail-closed), so on_automain must be False.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="No-PR root fix",
        description="merged but no pr_number",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        # pr_merged=True but no pr_number/rollup_pr_number → ancestry check skipped
        context={"pr_merged": True},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "No-PR root fix"
    # No pr_number → ancestry check skipped → unverifiable → fail-closed = False
    assert merges[0]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_false_for_root_task_ancestry_api_exception(
    db_session, mocker
):
    """Root task reports on_automain=False when the ancestry API call raises an exception.

    An exception during the ancestry check leaves the task absent from
    _automain_verified.  The fallback must NOT assume landed (fail-closed).
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="API-error root fix",
        description="ancestry API raises",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 777},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"merge_commit_sha": "abc123", "head": {"sha": "head-sha"}}
        ),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(side_effect=RuntimeError("GitHub API unreachable")),
    )

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "API-error root fix"
    # API exception → task absent from _automain_verified → fail-closed = False
    assert merges[0]["on_automain"] is False


@pytest.mark.asyncio
async def test_recent_merged_titles_carry_tristate_merge_verdict(db_session, mocker):
    """Each recent_merged_titles entry carries a tri-state merge_verdict.

    on_automain is fail-closed (collapses verified-not-landed and unverifiable
    into False); merge_verdict keeps them apart so cycle-start merge-accounting
    can flag only the genuine not-landed conflict. Two merged tasks on the same
    active direction: one ancestry-verified landed, one ancestry-verified NOT
    landed — both have pr_number, so both are verifiable.
    """
    direction_id = "dir-merge-acct"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {"id": direction_id, "text": "merge accounting dir", "status": "active"}
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    landed = Task(
        project_id=project.id,
        title="Landed on automain",
        description="ancestor of integration branch",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 301, "direction_id": direction_id},
    )
    stranded = Task(
        project_id=project.id,
        title="Badge says merged but stranded",
        description="merge commit NOT an ancestor",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 302, "direction_id": direction_id},
    )
    db_session.add_all([landed, stranded])
    await db_session.commit()

    def _pr_side_effect(token, owner, repo, pr_num, project_id=None):
        return {
            301: {"merge_commit_sha": "landed-sha", "head": {"sha": "h1"}},
            302: {"merge_commit_sha": "stranded-sha", "head": {"sha": "h2"}},
        }[pr_num]

    def _anc_side_effect(token, owner, repo, sha, ref, project_id=None):
        return sha == "landed-sha"

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(side_effect=_pr_side_effect),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(side_effect=_anc_side_effect),
    )
    mocker.patch(
        "app.github.list_recent_commit_messages",
        new=AsyncMock(return_value=[]),
    )

    result = await _build_recent_interventions(project, db_session)
    delta = next(
        d for d in result["per_direction_delta"] if d["direction_id"] == direction_id
    )
    verdicts = {t["title"]: t["merge_verdict"] for t in delta["recent_merged_titles"]}
    assert verdicts["Landed on automain"] == "verified_landed"
    assert verdicts["Badge says merged but stranded"] == "verified_not_landed"


@pytest.mark.asyncio
async def test_recent_merged_titles_pending_in_open_rollup_verdict(
    db_session, mocker
):
    """PRs merged into a still-open rollup branch get verdict pending_in_open_rollup.

    When get_pull_request returns state=open (no merge_commit_sha), the task
    is in a staging branch waiting for the rollup to squash-merge. This must
    NOT be classified as verified_not_landed (a real accounting conflict) and
    must NOT trigger the stranded-discount path.
    """
    direction_id = "dir-pending-rollup"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {"id": direction_id, "text": "pending rollup dir", "status": "active"}
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Impl merged into open rollup branch",
        description="PR is still open — rollup not yet squash-merged",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 555, "direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mark_stranded_mock = mocker.patch(
        "app.strategic_spawning.mark_stranded_non_landing"
    )
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "open"}),
    )
    mocker.patch(
        "app.github.list_recent_commit_messages",
        new=AsyncMock(return_value=[]),
    )

    result = await _build_recent_interventions(project, db_session)
    delta = next(
        d for d in result["per_direction_delta"] if d["direction_id"] == direction_id
    )
    titles = delta["recent_merged_titles"]
    assert len(titles) == 1
    entry = titles[0]
    assert entry["title"] == "Impl merged into open rollup branch"
    assert entry["merge_verdict"] == "pending_in_open_rollup"
    assert entry["on_automain"] is False

    assert delta["recent_merge_verdict_counts"]["pending_in_open_rollup"] == 1
    assert delta["recent_merge_verdict_counts"]["verified_not_landed"] == 0
    assert "recent_not_landed_titles" not in delta

    mark_stranded_mock.assert_not_called()

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("stranded_discount_applied") is None


@pytest.mark.parametrize(
    ("messages", "expected_verdict", "expected_not_landed_titles"),
    [
        (
            ["Rollup squash\n\nIncluded work:\n- Honor rollup squash bodies"],
            "verified_not_landed",
            ["Honor rollup squash bodies"],
        ),
        (
            ["Rollup squash\n\nIncluded PRs: #9635, #9639"],
            "verified_landed",
            None,
        ),
        (
            ["Rollup squash\n\nIncluded PRs: #1111"],
            "verified_not_landed",
            ["Honor rollup squash bodies"],
        ),
        (None, "unverifiable", None),
    ],
    ids=["title-only-not-rescued", "pr-evidence", "absent", "scan-failure"],
)
@pytest.mark.asyncio
async def test_rollup_squash_body_evidence_prevents_not_landed_verdict(
    db_session, mocker, messages, expected_verdict, expected_not_landed_titles
):
    """Recent rollup squash bodies can upgrade false ancestry before accounting."""
    direction_id = "dir-rollup-evidence"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {"id": direction_id, "text": "merge accounting dir", "status": "active"}
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Honor rollup squash bodies",
        description="merge commit was rewritten by a rollup squash",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 9635, "direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "merge_commit_sha": "rewritten-child-sha",
                "head": {"sha": "head-sha"},
            }
        ),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    commit_messages_mock = mocker.patch(
        "app.github.list_recent_commit_messages",
        new=AsyncMock(return_value=messages),
    )

    result = await _build_recent_interventions(project, db_session)
    delta = next(
        d for d in result["per_direction_delta"] if d["direction_id"] == direction_id
    )

    assert delta["recent_merged_titles"] == [
        {
            "title": "Honor rollup squash bodies",
            "on_automain": expected_verdict == "verified_landed",
            "merge_verdict": expected_verdict,
            "landed_on_integration": None,
        }
    ]
    if expected_not_landed_titles:
        assert delta["recent_not_landed_titles"] == expected_not_landed_titles
    else:
        assert "recent_not_landed_titles" not in delta
    assert delta["recent_merge_verdict_counts"][expected_verdict] == 1
    assert sum(delta["recent_merge_verdict_counts"].values()) == 1
    commit_messages_mock.assert_any_await(
        "tok", "owner", "repo", "automain", project_id=str(project.id)
    )
    expected_scan_count = 2 if expected_verdict == "verified_landed" else 1
    assert commit_messages_mock.await_count == expected_scan_count


@pytest.mark.parametrize(
    ("messages", "expected_verdict", "expected_not_landed_titles"),
    [
        (
            ["Rollup squash\n\nIncluded PRs: #9635, #9639"],
            "verified_landed",
            None,
        ),
        (
            ["Rollup squash\n\nIncluded work:\n- Honor rollup squash bodies"],
            "verified_not_landed",
            ["Honor rollup squash bodies"],
        ),
        (
            ["Rollup squash\n\nIncluded PRs: #1111"],
            "verified_not_landed",
            ["Honor rollup squash bodies"],
        ),
    ],
    ids=["pr-match-rescued", "title-only-not-rescued", "wrong-pr-not-rescued"],
)
@pytest.mark.asyncio
async def test_squash_rescue_requires_pr_number_evidence(
    db_session, mocker, messages, expected_verdict, expected_not_landed_titles
):
    """PR-number references are primary rescue evidence; title-only matches do not rescue.

    When a task has a PR number, only a PR number match in the commit messages
    can rescue it from a false NOT-landed verdict. Title substring matches are
    not sufficient for rescue when PR numbers are available.
    """
    direction_id = "dir-pr-primary-evidence"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {"id": direction_id, "text": "pr-number-primary test", "status": "active"}
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Honor rollup squash bodies",
        description="task with pr number for rescue evidence",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 9635, "direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "merge_commit_sha": "rewritten-child-sha",
                "head": {"sha": "head-sha"},
            }
        ),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    commit_messages_mock = mocker.patch(
        "app.github.list_recent_commit_messages",
        new=AsyncMock(return_value=messages),
    )

    result = await _build_recent_interventions(project, db_session)
    delta = next(
        d for d in result["per_direction_delta"] if d["direction_id"] == direction_id
    )

    assert delta["recent_merged_titles"] == [
        {
            "title": "Honor rollup squash bodies",
            "on_automain": expected_verdict == "verified_landed",
            "merge_verdict": expected_verdict,
            "landed_on_integration": None,
        }
    ]
    if expected_not_landed_titles:
        assert delta["recent_not_landed_titles"] == expected_not_landed_titles
    else:
        assert "recent_not_landed_titles" not in delta
    assert delta["recent_merge_verdict_counts"][expected_verdict] == 1
    assert sum(delta["recent_merge_verdict_counts"].values()) == 1
    commit_messages_mock.assert_any_await(
        "tok", "owner", "repo", "automain", project_id=str(project.id)
    )


# ── stranded-PR credit discount persisted by cycle-start reconciliation ────────


@pytest.mark.asyncio
async def test_verified_not_landed_persists_stranded_discount(db_session, mocker):
    """verified_not_landed tasks get stranded_prs_merged incremented and stranded_discount_applied stamped.

    When _build_recent_interventions classifies a task as verified_not_landed
    (ancestry check returns False), it must call mark_stranded_non_landing and
    discount_stranded_pr_from_direction_credit so the direction credit gate stops
    crediting the stranded merge.
    """
    research_task_id = str(uuid.uuid4())
    direction_id = "dir-stranded-discount"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {
                "id": direction_id,
                "text": "stranded discount test dir",
                "status": "active",
                "research_history": [
                    {
                        "task_id": research_task_id,
                        "prs_merged": 1,
                        "verified_prs_merged": 1,
                    }
                ],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Stranded non-landing task",
        description="merge commit NOT an ancestor of integration branch",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "pr_merged": True,
            "pr_number": 501,
            "direction_id": direction_id,
            "research_task_id": research_task_id,
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"merge_commit_sha": "stranded-sha", "head": {"sha": "h1"}}
        ),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.list_recent_commit_messages", new=AsyncMock(return_value=[])
    )

    await _build_recent_interventions(project, db_session)

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("stranded_discount_applied") is True, (
        "stranded_discount_applied must be stamped after cycle-start reconciliation"
    )
    assert ctx.get("landed_on_integration") is False, (
        "mark_stranded_non_landing must stamp landed_on_integration=False"
    )

    await db_session.refresh(project)
    dirs = project.directions or []
    the_dir = next(d for d in dirs if d.get("id") == direction_id)
    history = the_dir.get("research_history", [])
    entry = next(h for h in history if h.get("task_id") == research_task_id)
    assert entry.get("stranded_prs_merged") == 1, (
        "stranded_prs_merged must be incremented to 1 for the matching research_history entry"
    )


@pytest.mark.asyncio
async def test_verified_not_landed_discount_survives_projection_reload(
    db_session, mocker
):
    """The stranded-discount path works on tasks loaded through the function's own projection.

    Regression (2026-07-11 outage): tasks created in the test session stay fully
    loaded in the identity map, so the sibling test above never exercised the
    tasks_in_window load_only(..., raiseload=True) projection. When that
    projection omitted Task.project_id, discount_stranded_pr_from_direction_credit
    raised InvalidRequestError and aborted the entire per-project spawn cycle.
    Expunging the task forces _build_recent_interventions to re-load it through
    its real projection, pinning both the projection contents and the discount.
    """
    research_task_id = str(uuid.uuid4())
    direction_id = "dir-stranded-projection"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {
                "id": direction_id,
                "text": "stranded discount projection test dir",
                "status": "active",
                "research_history": [
                    {
                        "task_id": research_task_id,
                        "prs_merged": 1,
                        "verified_prs_merged": 1,
                    }
                ],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Stranded non-landing task (projection reload)",
        description="merge commit NOT an ancestor of integration branch",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "pr_merged": True,
            "pr_number": 503,
            "direction_id": direction_id,
            "research_task_id": research_task_id,
        },
    )
    db_session.add(task)
    await db_session.commit()
    task_id = task.id
    # Drop the fully-loaded instance so the tasks_in_window query re-loads it
    # under its load_only(..., raiseload=True) projection — as in production.
    db_session.expunge(task)

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"merge_commit_sha": "stranded-sha3", "head": {"sha": "h3"}}
        ),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.list_recent_commit_messages", new=AsyncMock(return_value=[])
    )

    # Must not raise, and the discount must actually be applied — a projection
    # missing project_id would silently skip it (best-effort) or, pre-fix, abort
    # the whole spawn cycle.
    await _build_recent_interventions(project, db_session)

    reloaded = await db_session.get(Task, task_id)
    ctx = reloaded.context or {}
    assert ctx.get("stranded_discount_applied") is True, (
        "stranded_discount_applied must be stamped even when the task was loaded "
        "through the tasks_in_window raiseload projection"
    )

    await db_session.refresh(project)
    the_dir = next(d for d in project.directions or [] if d.get("id") == direction_id)
    entry = next(
        h
        for h in the_dir.get("research_history", [])
        if h.get("task_id") == research_task_id
    )
    assert entry.get("stranded_prs_merged") == 1


@pytest.mark.asyncio
async def test_stranded_discount_failure_does_not_abort_interventions_build(
    db_session, mocker, real_orm_expiry
):
    """One failing stranded-PR discount must not poison the spawn cycle.

    discount_stranded_pr_from_direction_credit's best-effort except path calls
    session.rollback() on the session it is handed; a rollback expires every
    loaded instance regardless of expire_on_commit=False. Run inline on the
    shared session (pre-fix), one failed discount made the next iteration's
    ``(t.context or {})`` read — and the caller's later ``project`` reads —
    raise MissingGreenlet and abort the whole spawn cycle. Post-fix each
    queued mark-stranded + discount pair runs on its OWN fresh session, so a
    failure is fully contained: the build completes, the remaining pairs still
    strand + discount, and the shared session's instances stay live.

    Uses the ``real_orm_expiry`` fixture: the suite-wide ``InstanceState._expire``
    no-op (backend/conftest.py) makes rollback-expiry impossible by default,
    so without it this test would pass vacuously against the broken code.
    """
    from sqlalchemy import inspect as sa_inspect

    dir_a, dir_b = "dir-strand-fail-a", "dir-strand-fail-b"
    rt_a, rt_b = str(uuid.uuid4()), str(uuid.uuid4())
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {
                "id": d_id,
                "text": f"stranded failure isolation {d_id}",
                "status": "active",
                "research_history": [
                    {"task_id": rt, "prs_merged": 1, "verified_prs_merged": 1}
                ],
            }
            for d_id, rt in ((dir_a, rt_a), (dir_b, rt_b))
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    tasks = []
    for i, (d_id, rt) in enumerate(((dir_a, rt_a), (dir_b, rt_b))):
        t = Task(
            project_id=project.id,
            title=f"Stranded non-landing task {d_id}",
            description="merge commit NOT an ancestor of integration branch",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={
                "pr_merged": True,
                "pr_number": 600 + i,
                "direction_id": d_id,
                "research_task_id": rt,
            },
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.commit()
    task_id_a, task_id_b = (t.id for t in tasks)
    for t in tasks:
        db_session.expunge(t)

    # Self-check that real_orm_expiry actually restored genuine expiry
    # semantics — otherwise the whole test is vacuous (the suite-wide no-op
    # would make the pre-fix shared-session rollback harmless in tests while
    # it aborts spawn cycles in production).
    await db_session.refresh(project)  # ensure an active transaction to roll back
    await db_session.rollback()
    assert sa_inspect(project).expired, (
        "real_orm_expiry fixture did not restore InstanceState._expire — "
        "rollback-expiry coverage would be vacuous"
    )
    await db_session.refresh(project)  # re-load for the build below

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"merge_commit_sha": "stranded-sha9", "head": {"sha": "h9"}}
        ),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.list_recent_commit_messages", new=AsyncMock(return_value=[])
    )

    # Pair 1's discount dies inside its own try (directions write blows up) →
    # its best-effort handler logs + rolls back the session it was handed.
    # Pair 2's discount must still go through the REAL locked write.
    from app.task_locking import with_directions_locked as _real_wdl

    _wdl_calls = {"n": 0}

    async def _flaky_wdl(session, project_id, mutate_fn, **kwargs):
        _wdl_calls["n"] += 1
        if _wdl_calls["n"] == 1:
            raise RuntimeError("directions write exploded")
        return await _real_wdl(session, project_id, mutate_fn, **kwargs)

    mocker.patch(
        "app.direction_outcomes.with_directions_locked", side_effect=_flaky_wdl
    )

    result = await _build_recent_interventions(project, db_session)

    # The build completed and BOTH directions were fully processed.
    assert result is not None
    assert _wdl_calls["n"] >= 2, "second pair's discount never ran"
    delta_by_id = {e["direction_id"]: e for e in result["per_direction_delta"]}
    assert set(delta_by_id) >= {dir_a, dir_b}
    for d_id in (dir_a, dir_b):
        assert delta_by_id[d_id].get("recent_merged_titles"), (
            f"direction {d_id} lost its merge data — the loop must survive a "
            "failed stranded-PR discount"
        )

    # F2 guard: the caller (_spawn_strategic_review → source_ref_validation)
    # keeps reading `project` on the shared session after the build. With real
    # expiry active, a shared-session rollback would have expired it and this
    # read would raise MissingGreenlet.
    assert isinstance(project.directions, list)

    from conftest import TestingSessionLocal

    async with TestingSessionLocal() as fresh:
        # Pair 1 (dir_a): its private session rolled back — nothing persisted;
        # it re-strands next cycle and the idempotency stamp must be absent.
        reloaded_a = await fresh.get(Task, task_id_a)
        ctx_a = reloaded_a.context or {}
        assert ctx_a.get("stranded_discount_applied") is None
        assert ctx_a.get("landed_on_integration") is None
        # Pair 2 (dir_b): fully stranded + discounted in its own session
        # despite pair 1's failure.
        reloaded_b = await fresh.get(Task, task_id_b)
        ctx_b = reloaded_b.context or {}
        assert ctx_b.get("landed_on_integration") is False
        assert "pr_merged" not in ctx_b
        assert ctx_b.get("stranded_discount_applied") is True
        fresh_project = await fresh.get(Project, project.id)
        dir_b_entry = next(
            d for d in fresh_project.directions or [] if d.get("id") == dir_b
        )
        hist_b = next(
            h
            for h in dir_b_entry.get("research_history", [])
            if h.get("task_id") == rt_b
        )
        assert hist_b.get("stranded_prs_merged") == 1


@pytest.mark.asyncio
async def test_verified_not_landed_stranded_discount_idempotent(db_session, mocker):
    """A task already carrying stranded_discount_applied=True is not double-counted.

    If a task was already processed by rollup verification (stranded_discount_applied=True
    in context), the cycle-start reconciliation must NOT increment stranded_prs_merged again.
    """
    research_task_id = str(uuid.uuid4())
    direction_id = "dir-stranded-idem"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {
                "id": direction_id,
                "text": "idempotency test dir",
                "status": "active",
                "research_history": [
                    {
                        "task_id": research_task_id,
                        "prs_merged": 1,
                        "verified_prs_merged": 1,
                        "stranded_prs_merged": 1,  # already discounted once
                    }
                ],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Already stranded and discounted",
        description="discount was already applied by rollup verification",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "pr_merged": True,
            "pr_number": 502,
            "direction_id": direction_id,
            "research_task_id": research_task_id,
            "landed_on_integration": False,
            "stranded_discount_applied": True,  # already stamped
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"merge_commit_sha": "stranded-sha2", "head": {"sha": "h2"}}
        ),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.list_recent_commit_messages", new=AsyncMock(return_value=[])
    )

    await _build_recent_interventions(project, db_session)

    await db_session.refresh(project)
    dirs = project.directions or []
    the_dir = next(d for d in dirs if d.get("id") == direction_id)
    history = the_dir.get("research_history", [])
    entry = next(h for h in history if h.get("task_id") == research_task_id)
    assert entry.get("stranded_prs_merged") == 1, (
        "stranded_prs_merged must not be incremented again when stranded_discount_applied=True"
    )


@pytest.mark.asyncio
async def test_unverifiable_verdict_does_not_apply_stranded_discount(
    db_session, mocker
):
    """Tasks with unverifiable verdict must not trigger the stranded-PR credit discount.

    When ancestry cannot be determined (no GitHub credentials or API error),
    the task gets verdict 'unverifiable' and must NOT be marked stranded.
    """
    research_task_id = str(uuid.uuid4())
    direction_id = "dir-unverifiable"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {
                "id": direction_id,
                "text": "unverifiable test dir",
                "status": "active",
                "research_history": [
                    {
                        "task_id": research_task_id,
                        "prs_merged": 1,
                        "verified_prs_merged": 0,
                    }
                ],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # No pr_number → ancestry check is skipped → verdict is "unverifiable"
    task = Task(
        project_id=project.id,
        title="Unverifiable merge",
        description="no pr_number so ancestry check is skipped",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "pr_merged": True,
            "direction_id": direction_id,
            "research_task_id": research_task_id,
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Even if GitHub creds are present, no pr_number → unverifiable
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.list_recent_commit_messages", new=AsyncMock(return_value=[])
    )

    await _build_recent_interventions(project, db_session)

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("stranded_discount_applied") is None, (
        "unverifiable tasks must not have stranded_discount_applied stamped"
    )
    assert ctx.get("landed_on_integration") is None, (
        "unverifiable tasks must not have landed_on_integration stamped"
    )

    await db_session.refresh(project)
    dirs = project.directions or []
    the_dir = next(d for d in dirs if d.get("id") == direction_id)
    history = the_dir.get("research_history", [])
    entry = next(h for h in history if h.get("task_id") == research_task_id)
    assert entry.get("stranded_prs_merged") is None, (
        "stranded_prs_merged must not be written for unverifiable tasks"
    )


@pytest.mark.asyncio
async def test_rollup_squash_body_evidence_applies_to_promotion_target(
    db_session, mocker
):
    """Squash-body evidence on the promotion target marks rescued work deployed."""
    direction_id = "dir-rollup-main-evidence"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {"id": direction_id, "text": "merge accounting dir", "status": "active"}
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Promoted rollup squash body",
        description="merge commit was rewritten by a promoted rollup squash",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 9635, "direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"merge_commit_sha": "rewritten-child-sha"}),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    commit_messages_mock = mocker.patch(
        "app.github.list_recent_commit_messages",
        new=AsyncMock(
            side_effect=[
                ["Rollup squash\n\nIncluded PRs: #9635"],
                ["Promoted rollup\n\nIncluded PRs: #9635"],
            ]
        ),
    )

    result = await _build_recent_interventions(project, db_session)

    merge = result["merges"][0]
    assert merge["on_automain"] is True
    assert merge["on_main"] is True
    assert result["undeployed_directions"] == []
    assert commit_messages_mock.await_args_list == [
        call("tok", "owner", "repo", "automain", project_id=str(project.id)),
        call("tok", "owner", "repo", "main", project_id=str(project.id)),
    ]


@pytest.mark.asyncio
async def test_recent_merged_titles_cap_preserves_full_merge_accounting_counts(
    db_session, mocker
):
    """The visible merge sample stays capped, but reconciliation metadata keeps
    exact verdict counts and a conflict-title sample from the full window."""

    from app.strategic_spawning import _RECON_LIST_CAP, _RECENT_MERGED_TITLES_CAP

    direction_id = "dir-merge-cap"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {"id": direction_id, "text": "merge accounting dir", "status": "active"}
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    base_time = _utcnow_naive()
    tasks = []
    for i in range(_RECENT_MERGED_TITLES_CAP + 3):
        tasks.append(
            Task(
                project_id=project.id,
                title=f"{'landed' if i < _RECENT_MERGED_TITLES_CAP else 'stranded'} {i}",
                description="merge accounting sample",
                payloads=[{"type": "create"}],
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                context={
                    "pr_merged": True,
                    "pr_number": 400 + i,
                    "direction_id": direction_id,
                },
                created_at=base_time - timedelta(minutes=i + 1),
                updated_at=base_time - timedelta(minutes=i + 1),
            )
        )
    db_session.add_all(tasks)
    await db_session.commit()

    def _pr_side_effect(token, owner, repo, pr_num, project_id=None):
        return {
            "merge_commit_sha": (
                f"landed-sha-{pr_num}"
                if pr_num < 400 + _RECENT_MERGED_TITLES_CAP
                else f"stranded-sha-{pr_num}"
            ),
            "head": {"sha": f"h-{pr_num}"},
        }

    def _anc_side_effect(token, owner, repo, sha, ref, project_id=None):
        return sha.startswith("landed-sha-")

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(side_effect=_pr_side_effect),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(side_effect=_anc_side_effect),
    )
    mocker.patch(
        "app.github.list_recent_commit_messages",
        new=AsyncMock(return_value=[]),
    )

    result = await _build_recent_interventions(project, db_session)
    delta = next(
        d for d in result["per_direction_delta"] if d["direction_id"] == direction_id
    )
    assert len(delta["recent_merged_titles"]) == _RECENT_MERGED_TITLES_CAP
    assert {t["merge_verdict"] for t in delta["recent_merged_titles"]} == {
        "verified_landed"
    }
    assert delta["recent_merge_verdict_counts"] == {
        "verified_landed": _RECENT_MERGED_TITLES_CAP,
        "verified_not_landed": 3,
        "unverifiable": 0,
        "pending_in_open_rollup": 0,
    }
    assert delta["recent_not_landed_titles"] == [
        "stranded 10",
        "stranded 11",
        "stranded 12",
    ]
    assert len(delta["recent_not_landed_titles"]) <= _RECON_LIST_CAP


@pytest.mark.asyncio
async def test_ancestry_verification_runs_with_no_open_transaction(db_session, mocker):
    """Regression: GitHub ancestry I/O must NOT run inside an open DB transaction.

    Root cause of the strategic-cycle outage: ``_build_recent_interventions``
    made sequential, awaited GitHub calls (get_pull_request + is_ancestor_of)
    while the read transaction on ``session`` stayed open. For a large merged
    set the loop exceeded Postgres'
    ``idle_in_transaction_session_timeout`` (60s), the server killed the
    connection, and the next query raised ``InterfaceError: connection is
    closed`` — silently swallowed, so no strategic task ever spawned.

    This test asserts the fix invariant directly: at the moment EACH GitHub
    client method is invoked, ``session.in_transaction()`` is False (the read
    txn was committed/released before any network I/O). It also confirms the
    verified ancestry results still flow through correctly after the connection
    is re-acquired for the post-loop children query.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Landed fix",
        description="merge SHA is a real ancestor",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 777},
    )
    db_session.add(task)
    await db_session.commit()

    # Captured txn-state at each GitHub call site. Every entry MUST be False —
    # any True means network I/O ran inside an open transaction (the bug).
    txn_states: list[bool] = []

    async def _pr_side_effect(token, owner, repo, pr_num, project_id=None):
        txn_states.append(db_session.in_transaction())
        return {"merge_commit_sha": "landed-sha", "head": {"sha": "head-sha"}}

    async def _anc_side_effect(token, owner, repo, sha, ref, project_id=None):
        txn_states.append(db_session.in_transaction())
        return True

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(side_effect=_pr_side_effect),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(side_effect=_anc_side_effect),
    )

    result = await _build_recent_interventions(project, db_session)

    # The GitHub client was actually exercised (guards against a vacuous pass if
    # the verification path were skipped) ...
    assert txn_states, "expected at least one GitHub call to be made"
    # ... and NONE of those calls happened inside an open DB transaction.
    assert all(state is False for state in txn_states), (
        "GitHub ancestry I/O ran while a DB transaction was open — this is the "
        f"idle-in-transaction bug. txn states at call sites: {txn_states}"
    )

    # Behavior preserved: the verified result still lands on the merge entry.
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["on_automain"] is True


# ── Per-project integration_branch resolution (column-based) ─────────────────


class TestGetProjectIntegrationBranch:
    """get_project_integration_branch() — the canonical column-based resolver.

    The resolver fails SAFE and never raises, so it is safe to call from read
    endpoints, background recovery loops, and the protected_branches property.
    Self-repos map an unset integration branch to automain; non-self repos with
    an unset integration branch fall back to main. Refusing to land work on the
    promotion target is enforced at the merge/rollup boundary, not here.
    """

    def test_self_repo_without_integration_branch_resolves_to_automain(self):
        """Self-repos preserve the documented automain fallback when unset."""
        project = _make_project(
            integration_branch=None,
            promotion_target=None,
            is_self_repo=True,
            github_repo="BlueprintAIStudio",
        )
        assert _shared_state.get_project_integration_branch(project) == "automain"

    def test_non_self_repo_without_integration_branch_falls_back_to_main(self):
        """External repos with an unset integration branch fall back to main."""
        project = _make_project(integration_branch=None, is_self_repo=False)
        assert _shared_state.get_project_integration_branch(project) == "main"

    def test_column_override_used(self):
        """Project with integration_branch column set uses that value."""
        project = _make_project(integration_branch="feature-trunk")
        assert _shared_state.get_project_integration_branch(project) == "feature-trunk"

    def test_column_override_ignores_global(self):
        """Column value wins regardless of global state."""
        project = _make_project(integration_branch="my-branch")
        assert _shared_state.get_project_integration_branch(project) == "my-branch"

    def test_two_projects_resolve_independently(self):
        """Two projects with different column values each get their own value."""
        project_a = _make_project(integration_branch="trunk-a")
        project_b = _make_project(integration_branch="trunk-b")
        assert _shared_state.get_project_integration_branch(project_a) == "trunk-a"
        assert _shared_state.get_project_integration_branch(project_b) == "trunk-b"

    def test_project_with_override_and_project_without_are_independent(self):
        """A configured project is unaffected by another project's unset column."""
        project_with = _make_project(integration_branch="custom-trunk")
        project_without = _make_project(integration_branch=None, is_self_repo=False)
        assert (
            _shared_state.get_project_integration_branch(project_with) == "custom-trunk"
        )
        assert _shared_state.get_project_integration_branch(project_without) == "main"

    def test_same_branch_as_promotion_target_returns_configured_value(self):
        """The resolver returns the configured value even when it equals promotion.

        Refusing to land integration work on the promotion target is enforced at
        the merge/rollup boundary; the hot-path resolver must not raise.
        """
        project = _make_project(
            integration_branch="release", promotion_target="release"
        )
        assert _shared_state.get_project_integration_branch(project) == "release"


# ── _annotate_capability_proof_status: proof status annotation ────────────────


class TestAnnotateCapabilityProofStatus:
    """_annotate_capability_proof_status decorates the vocab list with
    ``proof_status: 'proven'`` when the project has a proof record for that
    capability and ``'unproven'`` when it does not.

    This is the production code path exercised when a task with
    ``provides_capability`` lands a verified acceptance-evidence verdict and
    ``record_capability_proof`` stamps a durable entry on
    ``project.capability_proof_records``.
    """

    def test_proven_capability_gets_proven_status(self):
        from app.strategic_spawning import _annotate_capability_proof_status

        vocab = [{"name": "language-aware-repo-map", "status": "working"}]
        proof_records = {
            "language-aware-repo-map": [
                {
                    "task_id": "task-abc",
                    "proved_at": "2026-06-09T00:00:00",
                    "proof_kind": "named_test",
                    "proof_ref": "testing/test_prompt.py::test_map",
                    "commit_sha": "abc123",
                }
            ]
        }
        out = _annotate_capability_proof_status(vocab, proof_records)
        assert out[0]["proof_status"] == "proven"
        assert out[0]["name"] == "language-aware-repo-map"

    def test_unproven_capability_gets_unproven_status(self):
        from app.strategic_spawning import _annotate_capability_proof_status

        vocab = [{"name": "language-aware-repo-map", "status": "working"}]
        out = _annotate_capability_proof_status(vocab, {})
        assert out[0]["proof_status"] == "unproven"

    def test_none_proof_records_yields_unproven(self):
        from app.strategic_spawning import _annotate_capability_proof_status

        vocab = [{"name": "cap-a", "status": "planned"}]
        out = _annotate_capability_proof_status(vocab, None)
        assert out[0]["proof_status"] == "unproven"

    def test_does_not_mutate_input(self):
        from app.strategic_spawning import _annotate_capability_proof_status

        original = [{"name": "cap-a", "status": "working"}]
        proof_records = {
            "cap-a": [
                {
                    "task_id": "t1",
                    "proved_at": "",
                    "proof_kind": "named_test",
                    "proof_ref": "",
                    "commit_sha": "",
                }
            ]
        }
        out = _annotate_capability_proof_status(original, proof_records)
        # Input dict must not have been mutated.
        assert "proof_status" not in original[0]
        assert out[0]["proof_status"] == "proven"

    def test_empty_proof_list_is_unproven(self):
        from app.strategic_spawning import _annotate_capability_proof_status

        vocab = [{"name": "cap-a", "status": "working"}]
        out = _annotate_capability_proof_status(vocab, {"cap-a": []})
        # An empty list means the key exists but has no proof entries.
        assert out[0]["proof_status"] == "unproven"

    def test_multiple_capabilities_mixed_proof(self):
        from app.strategic_spawning import _annotate_capability_proof_status

        vocab = [
            {"name": "cap-proven", "status": "working"},
            {"name": "cap-claimed", "status": "partial"},
        ]
        proof_records = {
            "cap-proven": [
                {
                    "task_id": "t1",
                    "proved_at": "",
                    "proof_kind": "named_test",
                    "proof_ref": "",
                    "commit_sha": "",
                }
            ]
        }
        out = _annotate_capability_proof_status(vocab, proof_records)
        assert out[0]["proof_status"] == "proven"
        assert out[1]["proof_status"] == "unproven"

    def test_non_list_passthrough(self):
        from app.strategic_spawning import _annotate_capability_proof_status

        assert _annotate_capability_proof_status(None, {}) is None  # type: ignore[arg-type]
        assert _annotate_capability_proof_status("invalid", {}) == "invalid"  # type: ignore[arg-type]

    def test_alias_is_shared_registry_public_fn(self):
        # The annotation body now lives in the IO-free capability_registry leaf
        # so every spawn path (strategic, design, research, plan) can call it;
        # the module-local name is a back-compat alias for the same function.
        from app.capability_registry import annotate_capability_proof_status
        from app.strategic_spawning import _annotate_capability_proof_status

        assert _annotate_capability_proof_status is annotate_capability_proof_status

    def test_capability_proof_badge_reflects_reverified_evidence_status(self):
        """evidence_refs_map overrides proof_records for the badge.

        A capability that has durable proof_records (historically proven) but
        whose evidence_refs have been re-verified and show no proven=True ref
        must NOT show 'proven' — the re-verified status takes precedence over
        the write-once record-existence check.
        """
        from app.strategic_spawning import _annotate_capability_proof_status

        vocab = [
            {"name": "ci-pipeline", "status": "working"},
            {"name": "repo-map", "status": "working"},
            {"name": "untracked", "status": "planned"},
        ]
        # proof_records says both ci-pipeline and repo-map are "proven" (old logic)
        proof_records = {
            "ci-pipeline": [{"task_id": "t1", "proof_kind": "named_test", "proof_ref": "test_ci.py::test_merge", "commit_sha": "abc"}],
            "repo-map": [{"task_id": "t2", "proof_kind": "named_test", "proof_ref": "test_map.py::test_map", "commit_sha": "def"}],
        }
        # evidence_refs_map: ci-pipeline re-verified — backing code removed, proven=False
        #                    repo-map re-verified — still present, proven=True
        evidence_refs_map = {
            "ci-pipeline": [{"kind": "named_test", "ref": "test_ci.py::test_merge", "proven": False}],
            "repo-map": [{"kind": "named_test", "ref": "test_map.py::test_map", "proven": True}],
        }
        out = _annotate_capability_proof_status(vocab, proof_records, evidence_refs_map)

        # repo-map: evidence_refs has proven=True → "proven" (consistent with old logic)
        assert out[1]["proof_status"] == "proven"
        # ci-pipeline: evidence_refs has proven=False only → classify_evidence → "partial"
        # (refs exist but none is proven=True), overriding the stale proof_record
        assert out[0]["proof_status"] == "partial"
        assert out[0]["proof_status"] != "proven"
        # untracked: no entry in evidence_refs_map → falls back to proof_records → "unproven"
        assert out[2]["proof_status"] == "unproven"


# ── _verify_capability_backing_paths: advisory capability verification ────────


class TestVerifyCapabilityBackingPaths:
    """The strategic payload annotates each capability with advisory backing-
    path verification, never mutating the durable problem_model.

    classify_paths_tristate is patched per-test so the cases are deterministic
    and don't depend on the integration-branch file tree.
    """

    @pytest.mark.asyncio
    async def test_empty_pm_returns_empty(self):
        from app.strategic_spawning import _verify_capability_backing_paths

        assert await _verify_capability_backing_paths(None) == {}
        assert await _verify_capability_backing_paths({}) == {}

    @pytest.mark.asyncio
    async def test_no_backing_paths_is_unverified(self):
        from app import strategic_spawning

        pm = {"capabilities": [{"name": "CapA", "status": "working"}]}
        # classify must never be called when there are no paths to check.
        with patch.object(
            strategic_spawning,
            "classify_paths_tristate",
            new=AsyncMock(side_effect=AssertionError("should not be called")),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(pm)
        ver = out["capabilities"][0]["verification"]
        assert ver["effective_status"] == "unverified"
        assert ver["present"] == [] and ver["missing"] == []

    @pytest.mark.asyncio
    async def test_all_present_preserves_status(self):
        from app import strategic_spawning

        pm = {
            "capabilities": [
                {
                    "name": "CapA",
                    "status": "working",
                    "backing_paths": ["a.py", "b.py"],
                }
            ]
        }
        with patch.object(
            strategic_spawning,
            "classify_paths_tristate",
            new=AsyncMock(return_value=(["a.py", "b.py"], [], [])),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(pm)
        ver = out["capabilities"][0]["verification"]
        assert ver["effective_status"] == "working"
        assert ver["present"] == ["a.py", "b.py"]
        assert ver["missing"] == []

    @pytest.mark.asyncio
    async def test_mixed_presence_is_partial(self):
        from app import strategic_spawning

        pm = {
            "capabilities": [
                {
                    "name": "CapA",
                    "status": "working",
                    "backing_paths": ["a.py", "gone.py"],
                }
            ]
        }
        with patch.object(
            strategic_spawning,
            "classify_paths_tristate",
            new=AsyncMock(return_value=(["a.py"], ["gone.py"], [])),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(pm)
        ver = out["capabilities"][0]["verification"]
        assert ver["effective_status"] == "partial"
        assert ver["present"] == ["a.py"]
        assert ver["missing"] == ["gone.py"]

    @pytest.mark.asyncio
    async def test_all_missing_is_absent(self):
        from app import strategic_spawning

        pm = {
            "capabilities": [
                {
                    "name": "CapA",
                    "status": "working",
                    "backing_paths": ["gone1.py", "gone2.py"],
                }
            ]
        }
        with patch.object(
            strategic_spawning,
            "classify_paths_tristate",
            new=AsyncMock(return_value=([], ["gone1.py", "gone2.py"], [])),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(pm)
        ver = out["capabilities"][0]["verification"]
        assert ver["effective_status"] == "absent"
        assert ver["present"] == []
        assert ver["missing"] == ["gone1.py", "gone2.py"]

    @pytest.mark.asyncio
    async def test_does_not_mutate_input(self):
        """The durable problem_model dict passed in is never mutated — only a
        copy is annotated."""
        from app import strategic_spawning

        pm = {
            "capabilities": [
                {
                    "name": "CapA",
                    "status": "working",
                    "backing_paths": ["a.py"],
                }
            ]
        }
        with patch.object(
            strategic_spawning,
            "classify_paths_tristate",
            new=AsyncMock(return_value=(["a.py"], [], [])),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(pm)
        # Input capability is untouched (no verification key added in place).
        assert "verification" not in pm["capabilities"][0]
        assert out is not pm
        assert out["capabilities"][0] is not pm["capabilities"][0]

    @pytest.mark.asyncio
    async def test_passes_repo_branch_context_to_classifier(self):
        from app import strategic_spawning

        seen_kwargs = {}
        pm = {
            "capabilities": [
                {
                    "name": "CapA",
                    "status": "working",
                    "backing_paths": ["a.py"],
                }
            ]
        }

        async def _classify(paths, **kwargs):
            seen_kwargs.update(kwargs)
            return (paths, [], [])

        with patch.object(
            strategic_spawning,
            "classify_paths_tristate",
            new=AsyncMock(side_effect=_classify),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(
                pm,
                token="ghs_test",
                owner="acme",
                repo="widget",
                branch="automain",
                project_id="proj-123",
            )
        assert out["capabilities"][0]["verification"]["effective_status"] == "working"
        assert seen_kwargs == {
            "token": "ghs_test",
            "owner": "acme",
            "repo": "widget",
            "branch": "automain",
            "project_id": "proj-123",
        }


# ── _build_cycle_start_reconciliation: conflict-focused starting state ─────────


class TestBuildCycleStartReconciliation:
    """The cycle-start reconciliation surfaces only the CONFLICTS between the
    four strategic surfaces (capabilities vs grounding, gaps vs directions,
    per-direction research pipeline). Pure function — directly unit-testable."""

    def test_returns_none_when_nothing_to_reconcile(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        # No active directions, no gaps, no capabilities → nothing to surface.
        assert _build_cycle_start_reconciliation([], {}, {}) is None
        assert _build_cycle_start_reconciliation([], None, None) is None

    def test_capability_contradiction_flags_stored_vs_grounded(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "capabilities": [
                # Contradiction: stored working, code shows absent.
                {
                    "name": "payments",
                    "status": "working",
                    "verification": {
                        "effective_status": "absent",
                        "present": [],
                        "missing": ["backend/app/payments.py"],
                    },
                },
                # Not a contradiction: stored matches grounded.
                {
                    "name": "search",
                    "status": "partial",
                    "verification": {
                        "effective_status": "partial",
                        "present": ["s.py"],
                        "missing": ["t.py"],
                    },
                },
                # Not a contradiction: unverified (no backing paths).
                {
                    "name": "memos",
                    "status": "working",
                    "verification": {
                        "effective_status": "unverified",
                        "present": [],
                        "missing": [],
                    },
                },
            ]
        }
        out = _build_cycle_start_reconciliation([], pm, {})
        # An active-direction-less project with a contradiction still returns a
        # block (the contradiction is content worth surfacing).
        assert out is not None
        contradictions = out["capability_contradictions"]
        assert len(contradictions) == 1
        assert contradictions[0]["name"] == "payments"
        assert contradictions[0]["stored_status"] == "working"
        assert contradictions[0]["effective_status"] == "absent"
        assert contradictions[0]["missing"] == ["backend/app/payments.py"]
        assert out["counts"]["capability_contradictions"] == 1

    def test_orphaned_gap_excludes_staffed_and_deferred(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "gaps": [
                # Orphan: active, no deferral, no live direction.
                {
                    "id": "g-orphan",
                    "name": "drifted",
                    "status": "active",
                    "priority": "high",
                },
                # Stale active_moves alone do NOT prove staffing.
                {
                    "id": "g-moved",
                    "name": "worked",
                    "status": "active",
                    "active_moves": ["d-9"],
                },
                # Staffed via a direction pointing back (gap_id) → not an orphan.
                {"id": "g-linked", "name": "linked", "status": "active"},
                # Intentionally deferred → counted separately, not an orphan.
                {
                    "id": "g-deferred",
                    "name": "parked",
                    "status": "active",
                    "deferral": {"rationale": "next version", "created_at": "x"},
                },
                # Closed gaps never count.
                {"id": "g-closed", "name": "done", "status": "closed"},
            ]
        }
        direction_stats = [
            {"id": "d-1", "status": "active", "gap_id": "g-linked"},
        ]
        out = _build_cycle_start_reconciliation(direction_stats, pm, {})
        orphan_ids = [g["gap_id"] for g in out["orphaned_gaps"]]
        assert orphan_ids == ["g-orphan", "g-moved"]
        assert out["orphaned_gaps"][0]["priority"] == "high"
        assert out["orphaned_gaps"][0]["status"] == "active"
        assert out["counts"]["deferred_gaps"] == 1

    def test_orphaned_gap_includes_blocked_open_gaps(self):
        # A blocked gap is still an open problem; if no active direction works
        # it (and it is not deferred) it is orphan-eligible, flagged by status.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "gaps": [
                # Blocked + unstaffed → orphan, carries status="blocked".
                {
                    "id": "g-blocked",
                    "name": "blocked orphan",
                    "status": "blocked",
                    "priority": "medium",
                },
                # Blocked but staffed by a live direction → not an orphan.
                {"id": "g-blocked-linked", "name": "worked", "status": "blocked"},
                # Blocked + deferred → counted as deferred, not orphaned.
                {
                    "id": "g-blocked-deferred",
                    "name": "parked",
                    "status": "blocked",
                    "deferral": {"rationale": "later", "created_at": "x"},
                },
            ]
        }
        direction_stats = [
            {"id": "d-1", "status": "active", "gap_id": "g-blocked-linked"},
        ]
        out = _build_cycle_start_reconciliation(direction_stats, pm, {})
        orphans = {g["gap_id"]: g["status"] for g in out["orphaned_gaps"]}
        assert orphans == {"g-blocked": "blocked"}
        assert out["counts"]["deferred_gaps"] == 1

    def test_staffed_but_deferred_gap_surfaced(self):
        """A gap that is both staffed (active direction via gap_id) and carries a
        deferral is a self-contradicting state — it must appear in
        ``staffed_but_deferred``, not be silently skipped."""
        from app.strategic_spawning import _build_cycle_start_reconciliation

        deferral = {"rationale": "pending review", "created_at": "2026-01-01"}
        pm = {
            "gaps": [
                # Staffed AND deferred → contradiction, must be surfaced.
                {
                    "id": "g-contradiction",
                    "name": "needs attention",
                    "status": "active",
                    "priority": "high",
                    "deferral": deferral,
                },
                # Staffed, no deferral → not a contradiction, not orphaned.
                {"id": "g-clean", "name": "clean", "status": "active"},
                # Deferred, not staffed → counted as deferred_gap, not contradiction.
                {
                    "id": "g-deferred-only",
                    "name": "parked",
                    "status": "active",
                    "deferral": {"rationale": "later", "created_at": "x"},
                },
            ]
        }
        direction_stats = [
            {"id": "d-1", "status": "active", "gap_id": "g-contradiction"},
            {"id": "d-2", "status": "active", "gap_id": "g-clean"},
        ]
        out = _build_cycle_start_reconciliation(direction_stats, pm, {})
        # The contradiction is surfaced.
        assert len(out["staffed_but_deferred"]) == 1
        entry = out["staffed_but_deferred"][0]
        assert entry["gap_id"] == "g-contradiction"
        assert entry["deferral"] == deferral
        assert out["counts"]["staffed_but_deferred"] == 1
        # Staffed-no-deferral and deferred-unstaffed gaps are NOT in the list.
        assert out["orphaned_gaps"] == []
        assert out["counts"]["deferred_gaps"] == 1

    def test_ungrounded_directions_by_reason(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {"gaps": [{"id": "g-live", "name": "x", "status": "active"}]}
        direction_stats = [
            {"id": "d-nogap", "status": "active", "text": "no link"},
            {
                "id": "d-dangling",
                "status": "active",
                "text": "stale",
                "gap_id": "g-gone",
            },
            {"id": "d-ok", "status": "active", "text": "fine", "gap_id": "g-live"},
            # Paused direction is ignored (only active directions reconcile).
            {"id": "d-paused", "status": "paused", "text": "n/a"},
        ]
        out = _build_cycle_start_reconciliation(direction_stats, pm, {})
        by_id = {d["direction_id"]: d["reason"] for d in out["ungrounded_directions"]}
        assert by_id == {"d-nogap": "no_gap", "d-dangling": "gap_missing"}

    def test_research_pipeline_states(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            # In flight → tasks_in_progress (regardless of findings).
            {
                "id": "d-busy",
                "status": "active",
                "text": "busy",
                "recent_findings": ["f1"],
            },
            # No tasks but has findings → research_waiting_on_action.
            {
                "id": "d-wait",
                "status": "active",
                "text": "wait",
                "recent_findings": ["f1", "f2"],
            },
            # Live-held work is distinct from waiting-on-action.
            {
                "id": "d-held",
                "status": "active",
                "text": "held",
                "recent_findings": ["f1"],
            },
            # Fresh completion before cycle start is also distinct.
            {
                "id": "d-recent",
                "status": "active",
                "text": "recent",
                "recent_findings": ["f1"],
            },
            # No tasks, no findings → idle.
            {"id": "d-idle", "status": "active", "text": "idle"},
        ]
        active_by_direction = {"d-busy": 2}
        held_by_direction = {"d-held": 1}
        per_direction_delta = [
            {
                "direction_id": "d-recent",
                "direction_text": "recent",
                "recent_merged_titles": [
                    {"title": "fresh merge", "merge_verdict": "verified_landed"},
                ],
            },
        ]
        out = _build_cycle_start_reconciliation(
            direction_stats,
            {},
            active_by_direction,
            held_by_direction,
            per_direction_delta,
        )
        states = {p["direction_id"]: p["state"] for p in out["direction_pipeline"]}
        assert states == {
            "d-busy": "tasks_in_progress",
            "d-wait": "research_waiting_on_action",
            "d-held": "held_on_dependency",
            "d-recent": "recently_converted",
            "d-idle": "idle",
        }
        busy = next(
            p for p in out["direction_pipeline"] if p["direction_id"] == "d-busy"
        )
        assert busy["in_flight"] == 2
        wait = next(
            p for p in out["direction_pipeline"] if p["direction_id"] == "d-wait"
        )
        assert wait["findings"] == 2
        held = next(
            p for p in out["direction_pipeline"] if p["direction_id"] == "d-held"
        )
        assert held["held"] == 1
        assert held["state"] == "held_on_dependency"
        recent = next(
            p for p in out["direction_pipeline"] if p["direction_id"] == "d-recent"
        )
        assert recent["recently_completed"] == 1
        assert recent["state"] == "recently_converted"
        assert out["counts"]["active_directions"] == 5
        assert out["counts"]["pipeline_waiting_on_action"] == 1
        assert out["counts"]["pipeline_tasks_in_progress"] == 1
        assert out["counts"]["pipeline_held_on_dependency"] == 1
        assert out["counts"]["pipeline_recently_converted"] == 1
        assert out["counts"]["pipeline_idle"] == 1

    def test_cycle_start_reconciliation_distinguishes_held_and_recently_converted_work(
        self,
    ):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {
                "id": "d-held",
                "status": "active",
                "text": "held lane",
                "recent_findings": ["f1"],
            },
            {
                "id": "d-recent",
                "status": "active",
                "text": "fresh lane",
                "recent_findings": ["f1", "f2"],
            },
        ]
        out = _build_cycle_start_reconciliation(
            direction_stats,
            {},
            {},
            {"d-held": 1},
            [
                {
                    "direction_id": "d-recent",
                    "direction_text": "fresh lane",
                    "recent_merged_titles": [
                        {"title": "fresh merge", "merge_verdict": "verified_landed"},
                    ],
                },
            ],
        )
        assert out is not None
        states = {p["direction_id"]: p["state"] for p in out["direction_pipeline"]}
        assert states == {
            "d-held": "held_on_dependency",
            "d-recent": "recently_converted",
        }
        assert out["counts"]["pipeline_waiting_on_action"] == 0
        assert out["counts"]["pipeline_held_on_dependency"] == 1
        assert out["counts"]["pipeline_recently_converted"] == 1

    def test_non_pr_completions_count_as_recently_converted(self):
        # Directions that produce only design/web_research/compute_experiment
        # findings never set pr_merged=True, so recent_merged_titles is empty.
        # The reconciliation should still classify them as recently_converted when
        # recent_non_pr_completed > 0.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {
                "id": "d-design",
                "status": "active",
                "text": "design direction",
                "recent_findings": ["f1"],
            },
            {
                "id": "d-wait",
                "status": "active",
                "text": "waiting direction",
                "recent_findings": ["f1"],
            },
        ]
        per_direction_delta = [
            {
                "direction_id": "d-design",
                "direction_text": "design direction",
                # No recent_merged_titles — direction only produced design docs.
                "recent_non_pr_completed": 2,
            },
        ]
        out = _build_cycle_start_reconciliation(
            direction_stats,
            {},
            {},
            {},
            per_direction_delta,
        )
        assert out is not None
        states = {p["direction_id"]: p["state"] for p in out["direction_pipeline"]}
        assert states["d-design"] == "recently_converted", (
            "non-PR completions should yield recently_converted, not research_waiting_on_action"
        )
        assert states["d-wait"] == "research_waiting_on_action"
        design_entry = next(
            p for p in out["direction_pipeline"] if p["direction_id"] == "d-design"
        )
        assert design_entry["recently_completed"] == 2
        assert out["counts"]["pipeline_recently_converted"] == 1
        assert out["counts"]["pipeline_waiting_on_action"] == 1

    def test_lists_are_capped_but_counts_carry_true_total(self):
        from app.strategic_spawning import (
            _build_cycle_start_reconciliation,
            _RECON_LIST_CAP,
        )

        gaps = [
            {"id": f"g-{i}", "name": f"orphan {i}", "status": "active"}
            for i in range(_RECON_LIST_CAP + 5)
        ]
        out = _build_cycle_start_reconciliation([], {"gaps": gaps}, {})
        assert len(out["orphaned_gaps"]) == _RECON_LIST_CAP
        # The count carries the true (uncapped) total so the renderer shows "+N more".
        assert out["counts"]["orphaned_gaps"] == _RECON_LIST_CAP + 5

    def test_pipeline_counts_survive_list_cap(self):
        from app.strategic_spawning import (
            _build_cycle_start_reconciliation,
            _RECON_LIST_CAP,
        )

        direction_stats = []
        for i in range(_RECON_LIST_CAP + 3):
            direction_stats.append(
                {
                    "id": f"d-w-{i}",
                    "status": "active",
                    "text": f"wait {i}",
                    "recent_findings": ["f1"],
                }
            )
        for i in range(2):
            direction_stats.append(
                {
                    "id": f"d-p-{i}",
                    "status": "active",
                    "text": f"progress {i}",
                    "recent_findings": [],
                }
            )
        active_by_direction = {"d-p-0": 1, "d-p-1": 2}
        out = _build_cycle_start_reconciliation(
            direction_stats, {}, active_by_direction
        )
        assert len(out["direction_pipeline"]) == _RECON_LIST_CAP
        assert out["counts"]["direction_pipeline"] == _RECON_LIST_CAP + 5
        assert out["counts"]["direction_pipeline_overflow"] == 5
        assert out["counts"]["pipeline_waiting_on_action"] == _RECON_LIST_CAP + 3
        assert out["counts"]["pipeline_tasks_in_progress"] == 2
        assert out["counts"]["pipeline_idle"] == 0

    def test_merge_accounting_flags_only_verified_not_landed(self):
        # Three merged titles within the recent window for one active direction:
        # one ancestry-verified landed, one badge-asserted-but-NOT-landed, one
        # unverifiable. Only the verified-not-landed one is a real conflict; the
        # unverifiable one must NOT manufacture a contradiction.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        # Clean gap linkage so the four pre-existing classes stay empty and the
        # only signal is merge-accounting drift.
        pm = {"gaps": [{"id": "g-1", "name": "x", "status": "active"}]}
        direction_stats = [
            {"id": "d-1", "status": "active", "text": "shipping dir", "gap_id": "g-1"},
        ]
        per_direction_delta = [
            {
                "direction_id": "d-1",
                "direction_text": "shipping dir",
                "recent_merged_titles": [
                    {"title": "landed feat", "merge_verdict": "verified_landed"},
                    {"title": "ghost feat", "merge_verdict": "verified_not_landed"},
                    {"title": "maybe feat", "merge_verdict": "unverifiable"},
                ],
            },
        ]
        out = _build_cycle_start_reconciliation(
            direction_stats, pm, {}, None, per_direction_delta
        )
        assert out is not None
        acct = out["merge_accounting"]
        assert len(acct) == 1
        entry = acct[0]
        assert entry["direction_id"] == "d-1"
        assert entry["not_landed"] == 1
        assert entry["landed"] == 1
        assert entry["unverifiable"] == 1
        assert entry["not_landed_titles"] == ["ghost feat"]
        # The four pre-existing classes are unchanged by the extension.
        assert out["capability_contradictions"] == []
        assert out["orphaned_gaps"] == []
        assert out["ungrounded_directions"] == []
        # Aggregate tallies carry every bucket so nothing is silently truncated.
        assert out["counts"]["merge_accounting"] == 1
        assert out["counts"]["merge_not_landed"] == 1
        assert out["counts"]["merge_landed"] == 1
        assert out["counts"]["merge_unverifiable"] == 1

    def test_merge_accounting_landed_and_unverifiable_only_no_conflict(self):
        # A direction whose recent merges are all verified-landed or unverifiable
        # has NO accounting conflict — it must not appear in merge_accounting,
        # though its tallies still ride in counts.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-1", "status": "active", "text": "clean dir"},
        ]
        per_direction_delta = [
            {
                "direction_id": "d-1",
                "direction_text": "clean dir",
                "recent_merged_titles": [
                    {"title": "a", "merge_verdict": "verified_landed"},
                    {"title": "b", "merge_verdict": "unverifiable"},
                    {"title": "c", "merge_verdict": "unverifiable"},
                ],
            },
        ]
        out = _build_cycle_start_reconciliation(
            direction_stats, {}, {}, None, per_direction_delta
        )
        assert out is not None
        assert out["merge_accounting"] == []
        assert out["counts"]["merge_accounting"] == 0
        assert out["counts"]["merge_not_landed"] == 0
        assert out["counts"]["merge_landed"] == 1
        assert out["counts"]["merge_unverifiable"] == 2

    def test_merge_accounting_absent_when_no_delta(self):
        # Back-compatible: omitting per_direction_delta yields an empty
        # merge_accounting list and zero tallies (no second pass invented).
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [{"id": "d-1", "status": "active", "text": "x"}]
        out = _build_cycle_start_reconciliation(direction_stats, {}, {})
        assert out is not None
        assert out["merge_accounting"] == []
        assert out["counts"]["merge_accounting"] == 0
        assert out["counts"]["merge_not_landed"] == 0

    def test_merge_accounting_caps_titles_with_true_overflow(self):
        # A direction with more not-landed titles than the list cap keeps the
        # true count and reports the overflow on the entry.
        from app.strategic_spawning import (
            _build_cycle_start_reconciliation,
            _RECON_LIST_CAP,
        )

        titles = [
            {"title": f"ghost {i}", "merge_verdict": "verified_not_landed"}
            for i in range(_RECON_LIST_CAP + 4)
        ]
        per_direction_delta = [
            {
                "direction_id": "d-1",
                "direction_text": "noisy dir",
                "recent_merged_titles": titles,
            },
        ]
        out = _build_cycle_start_reconciliation([], {}, {}, None, per_direction_delta)
        assert out is not None
        entry = out["merge_accounting"][0]
        assert entry["not_landed"] == _RECON_LIST_CAP + 4
        assert len(entry["not_landed_titles"]) == _RECON_LIST_CAP
        assert entry["not_landed_titles_overflow"] == 4
        assert out["counts"]["merge_not_landed"] == _RECON_LIST_CAP + 4

    def test_merge_accounting_uses_exact_counts_beyond_visible_merge_sample(self):
        from app.strategic_spawning import (
            _build_cycle_start_reconciliation,
            _RECENT_MERGED_TITLES_CAP,
        )

        per_direction_delta = [
            {
                "direction_id": "d-1",
                "direction_text": "noisy dir",
                "recent_merged_titles": [
                    {"title": f"landed {i}", "merge_verdict": "verified_landed"}
                    for i in range(_RECENT_MERGED_TITLES_CAP)
                ],
                "recent_merge_verdict_counts": {
                    "verified_landed": _RECENT_MERGED_TITLES_CAP,
                    "verified_not_landed": 3,
                    "unverifiable": 0,
                },
                "recent_not_landed_titles": ["ghost 10", "ghost 11", "ghost 12"],
            },
        ]
        out = _build_cycle_start_reconciliation([], {}, {}, None, per_direction_delta)
        assert out is not None
        entry = out["merge_accounting"][0]
        assert entry["landed"] == _RECENT_MERGED_TITLES_CAP
        assert entry["not_landed"] == 3
        assert entry["unverifiable"] == 0
        assert entry["not_landed_titles"] == ["ghost 10", "ghost 11", "ghost 12"]
        assert entry["not_landed_titles_overflow"] == 0
        assert out["counts"]["merge_landed"] == _RECENT_MERGED_TITLES_CAP
        assert out["counts"]["merge_not_landed"] == 3

    def test_pending_in_open_rollup_emitted_and_not_treated_as_conflict(self):
        # PRs merged into an open rollup branch yield pending_in_open_rollup verdict.
        # They must appear in recon.pending_in_open_rollup (not merge_accounting),
        # counts.merge_pending_in_open_rollup must carry the title count, and
        # counts.merge_pending_in_open_rollup_accounting must carry the direction count.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-1", "status": "active", "text": "rollup dir"},
        ]
        per_direction_delta = [
            {
                "direction_id": "d-1",
                "direction_text": "rollup dir",
                "recent_merged_titles": [
                    {"title": "feat A", "merge_verdict": "pending_in_open_rollup"},
                    {"title": "feat B", "merge_verdict": "pending_in_open_rollup"},
                    {"title": "landed C", "merge_verdict": "verified_landed"},
                ],
                "recent_merge_verdict_counts": {
                    "verified_landed": 1,
                    "verified_not_landed": 0,
                    "unverifiable": 0,
                    "pending_in_open_rollup": 2,
                },
            },
        ]
        out = _build_cycle_start_reconciliation(
            direction_stats, {}, {}, None, per_direction_delta
        )
        assert out is not None
        # Pending rollup items are NOT merge conflicts.
        assert out["merge_accounting"] == []
        assert out["counts"]["merge_accounting"] == 0
        assert out["counts"]["merge_not_landed"] == 0
        # They appear in the dedicated pending section.
        pending = out["pending_in_open_rollup"]
        assert len(pending) == 1
        entry = pending[0]
        assert entry["direction_id"] == "d-1"
        assert entry["pending_in_open_rollup"] == 2
        assert "feat A" in entry["pending_in_open_rollup_titles"]
        assert "feat B" in entry["pending_in_open_rollup_titles"]
        assert entry["pending_in_open_rollup_titles_overflow"] == 0
        # Aggregate counts match.
        assert out["counts"]["merge_pending_in_open_rollup"] == 2
        assert out["counts"]["merge_pending_in_open_rollup_accounting"] == 1
        # Landed count is unaffected.
        assert out["counts"]["merge_landed"] == 1

    def test_pending_in_open_rollup_only_cycle_renders_section(self):
        # A cycle whose ONLY merge signal is pending rollup work must not hit the
        # nothing-to-reconcile early return and must still emit the section.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        per_direction_delta = [
            {
                "direction_id": "d-2",
                "direction_text": "pending only dir",
                "recent_merged_titles": [
                    {"title": "feat X", "merge_verdict": "pending_in_open_rollup"},
                ],
                "recent_merge_verdict_counts": {
                    "verified_landed": 0,
                    "verified_not_landed": 0,
                    "unverifiable": 0,
                    "pending_in_open_rollup": 1,
                },
            },
        ]
        out = _build_cycle_start_reconciliation(
            [], {}, {}, None, per_direction_delta
        )
        assert out is not None, "pending-only cycle must not return None"
        assert len(out["pending_in_open_rollup"]) == 1
        assert out["counts"]["merge_pending_in_open_rollup"] == 1
        assert out["counts"]["merge_pending_in_open_rollup_accounting"] == 1

    def test_phantom_direction_refs_flagged_in_current_focus(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "intent": {
                "current_focus": "Advance d-4890df9e which is in flight and d-aaaabbbb also.",
            }
        }
        # Only d-deadbeef exists in direction_stats; the two cited refs are phantoms.
        direction_stats = [{"id": "d-deadbeef", "status": "active", "gap_id": None}]
        out = _build_cycle_start_reconciliation(
            direction_stats, pm, {}, running_update=""
        )
        assert out is not None
        phantoms = out["phantom_direction_refs"]
        phantom_ids = {p["direction_id"] for p in phantoms}
        assert "d-4890df9e" in phantom_ids
        assert "d-aaaabbbb" in phantom_ids
        assert "d-deadbeef" not in phantom_ids
        for p in phantoms:
            assert p["cited_in"] == "intent.current_focus"
        assert out["counts"]["phantom_direction_refs"] == 2

    def test_real_direction_ref_not_falsely_flagged(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "intent": {
                "current_focus": "Working on d-12345678 and d-abcdef01.",
            }
        }
        direction_stats = [
            {"id": "d-12345678", "status": "active", "gap_id": None},
            {"id": "d-abcdef01", "status": "retired", "gap_id": None},
        ]
        out = _build_cycle_start_reconciliation(
            direction_stats, pm, {}, running_update=""
        )
        # Both cited IDs exist — phantom_direction_refs must be empty.
        if out is not None:
            assert out["phantom_direction_refs"] == []
            assert out["counts"]["phantom_direction_refs"] == 0

    def test_stale_active_moves_conflict(self):
        # gap.active_moves references a direction that is no longer active → surfaced
        # as stale_active_moves. An active direction in active_moves is NOT stale.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "gaps": [
                # Stale: active_moves points at a retired direction not in active_dirs.
                {
                    "id": "g-stale",
                    "name": "stale gap",
                    "status": "active",
                    "active_moves": ["d-retired"],
                },
                # Clean: active_moves points at a live active direction.
                {
                    "id": "g-clean",
                    "name": "clean gap",
                    "status": "active",
                    "active_moves": ["d-live"],
                },
                # Closed gaps are never checked.
                {
                    "id": "g-closed",
                    "name": "closed gap",
                    "status": "closed",
                    "active_moves": ["d-retired"],
                },
            ]
        }
        direction_stats = [
            {"id": "d-live", "status": "active", "gap_id": "g-clean"},
            # d-retired is NOT in direction_stats at all (deleted/missing).
        ]
        out = _build_cycle_start_reconciliation(direction_stats, pm, {})
        assert out is not None
        stale = out["stale_active_moves"]
        assert len(stale) == 1
        assert stale[0]["gap_id"] == "g-stale"
        assert stale[0]["gap_name"] == "stale gap"
        assert stale[0]["stale_direction_ids"] == ["d-retired"]
        assert out["counts"]["stale_active_moves"] == 1
        # Clean gap is not surfaced.
        assert not any(s["gap_id"] == "g-clean" for s in stale)

    def test_qualification_prose_drift_flags_stale_sha_in_current_focus(self):
        # A stale candidate SHA embedded in current_focus is surfaced as
        # qualification_prose_drift when last_qual carries a different candidate.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "intent": {
                "current_focus": "Audit findings on candidate f0224ab4 are blocking.",
            }
        }
        last_qual = {"candidate_sha": "e587a4d0aabbccdd"}
        direction_stats = [{"id": "d-aaaa1111", "status": "active", "gap_id": None}]
        out = _build_cycle_start_reconciliation(
            direction_stats, pm, {}, running_update="", last_qual=last_qual
        )
        assert out is not None
        drift = out["qualification_prose_drift"]
        assert len(drift) == 1
        assert drift[0]["stale_sha"] == "f0224ab4"
        assert drift[0]["active_candidate_sha"] == "e587a4d0"
        assert drift[0]["cited_in"] == "intent.current_focus"
        assert out["counts"]["qualification_prose_drift"] == 1

    def test_qualification_prose_drift_no_flag_when_sha_matches_active(self):
        # When prose cites the CURRENT candidate SHA, no drift is flagged.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "intent": {
                "current_focus": "Candidate e587a4d0 is the active qualification target.",
            }
        }
        last_qual = {"candidate_sha": "e587a4d0aabbccdd"}
        direction_stats = [{"id": "d-aaaa1111", "status": "active", "gap_id": None}]
        out = _build_cycle_start_reconciliation(
            direction_stats, pm, {}, running_update="", last_qual=last_qual
        )
        assert out is not None
        assert out["qualification_prose_drift"] == []
        assert out["counts"]["qualification_prose_drift"] == 0

    def test_qualification_prose_drift_absent_without_last_qual(self):
        # Without last_qual, no SHA detection runs — the field is empty but present.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "intent": {
                "current_focus": "Working on f0224ab4 and other things.",
            }
        }
        direction_stats = [{"id": "d-aaaa1111", "status": "active", "gap_id": None}]
        out = _build_cycle_start_reconciliation(
            direction_stats, pm, {}, running_update="", last_qual=None
        )
        assert out is not None
        assert out["qualification_prose_drift"] == []
        assert out["counts"]["qualification_prose_drift"] == 0

    def test_qualification_surface_conflicts_stale_candidate_with_pending_status(self):
        # A stale candidate with status="pending" (active qualification in flight)
        # should NOT raise a conflict — integration-branch drift during active
        # qualification is expected, not a surface disagreement.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        version_phase_block = {
            "last_qualification": {
                "candidate_sha": "abc123def456789",
                "status": "pending",
                "confidence": 0.85,
                "threshold": 0.80,
            },
            "qualification_state": {"state": "qualifying"},
            # Production shape emitted by _resolve_candidate_currency: verdict
            # is "stale" (resolved=True, current=False) when the pinned SHA is
            # behind integration HEAD. There is no "is_stale" key.
            "last_qualification_candidate_currency": {
                "verdict": "stale",
                "resolved": True,
                "current": False,
                "candidate_sha": "abc123def456789",
                "head_sha": "9998887776665554443",
            },
        }
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=version_phase_block,
        )
        assert out is not None
        conflicts = out["qualification_surface_conflicts"]
        stale_conflicts = [
            c
            for c in conflicts
            if c.get("conflict_type") == "stale_candidate_with_promotion_failed_status"
        ]
        # pending + stale is normal during active qualification; NOT a conflict
        assert len(stale_conflicts) == 0

    def test_qualification_surface_conflicts_stale_candidate_with_promotion_failed(self):
        # A stale candidate with status="promotion_failed" IS a genuine conflict
        # — the version should have advanced its candidate after failure.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        version_phase_block = {
            "last_qualification": {
                "candidate_sha": "abc123def456789",
                "status": "promotion_failed",
                "confidence": 0.85,
                "threshold": 0.80,
            },
            "qualification_state": {"state": "promotion_failed"},
            "last_qualification_candidate_currency": {
                "verdict": "stale",
                "resolved": True,
                "current": False,
                "candidate_sha": "abc123def456789",
                "head_sha": "9998887776665554443",
            },
        }
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=version_phase_block,
        )
        assert out is not None
        conflicts = out["qualification_surface_conflicts"]
        stale_conflicts = [
            c
            for c in conflicts
            if c.get("conflict_type") == "stale_candidate_with_promotion_failed_status"
        ]
        assert len(stale_conflicts) == 1
        assert "stale" in stale_conflicts[0]["description"].lower()
        assert "promotion_failed" in stale_conflicts[0]["description"]

    def test_qualification_surface_conflicts_status_state_mismatch(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        version_phase_block = {
            "last_qualification": {
                "candidate_sha": "abc123def456789",
                "status": "pending",
                "confidence": 0.85,
                "threshold": 0.80,
            },
            # Deliberately mismatched: status="pending" but state="promoted"
            # (promoted is not a valid state for pending status)
            "qualification_state": {"state": "promoted"},
        }
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=version_phase_block,
        )
        assert out is not None
        conflicts = out["qualification_surface_conflicts"]
        status_conflicts = [
            c
            for c in conflicts
            if c.get("conflict_type") == "status_state_mismatch"
        ]
        assert len(status_conflicts) == 1
        assert "status" in status_conflicts[0]["description"].lower()
        assert "state" in status_conflicts[0]["description"].lower()

    def test_status_state_mismatch_not_flagged_for_finalized_verdict_substitution(self):
        """When qualification_state_disagreement is present the resolver already
        documented the status→state divergence (e.g. pending→promoted via a
        finalized QualificationVerdict).  _build_cycle_start_reconciliation must
        NOT flag it as a status_state_mismatch conflict — that would be a false
        positive against the resolver's own deliberate substitution.

        Acceptance evidence: named_test requirement from direction task."""
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        # Simulate what the resolver produces when a merge-test verdict with
        # terminal_status="promoted" exists while last_qualification.status is
        # still "pending" (the stale-pending window):
        #   - last_qualification.status = "pending"  (raw, unmodified)
        #   - qualification_state.state = "promoted"  (resolver-applied)
        #   - qualification_state_disagreement = {...}  (resolver note)
        version_phase_block = {
            "last_qualification": {
                "candidate_sha": "abc123def456789",
                "status": "pending",
                "promotion_pending": {
                    "pr_number": 9001,
                    "pr_url": "https://github.com/x/y/pull/9001",
                    "auto_merge_enabled": True,
                },
                "confidence": 0.92,
                "threshold": 0.80,
            },
            # Resolver substituted "pending" → "promoted" via QualificationVerdict
            "qualification_state": {"state": "promoted", "summary": "Promoted."},
            # Resolver documented the substitution here:
            "qualification_state_disagreement": {
                "last_qualification_status": "pending",
                "verdict_terminal_status": "promoted",
            },
        }
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=version_phase_block,
        )
        assert out is not None
        conflicts = out["qualification_surface_conflicts"]
        status_conflicts = [
            c
            for c in conflicts
            if c.get("conflict_type") == "status_state_mismatch"
        ]
        assert status_conflicts == [], (
            "status_state_mismatch must NOT fire when qualification_state_disagreement "
            f"is present (resolver already documented the substitution); got: {conflicts}"
        )

    def test_qualification_surface_conflicts_ignores_partial_evidence_coverage(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        version_phase_block = {
            "last_qualification": {
                "candidate_sha": "abc123def456789",
                "status": "pending",
                # Candidate-bound qualification remains confident while monotonic
                # evidence coverage is still partial.
                "confidence": 0.85,
                "threshold": 0.80,
            },
            "qualification_state": {"state": "qualifying"},
            # But only 2 of 5 criteria are covered
            "criterion_coverage": {
                "total": 5,
                "covered": 2,
                "partial": 1,
                "uncovered": 2,
            },
        }
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=version_phase_block,
        )
        assert out is not None
        conflicts = out["qualification_surface_conflicts"]
        coverage_conflicts = [
            c
            for c in conflicts
            if c.get("conflict_type") == "criterion_coverage_vs_confidence"
        ]
        assert coverage_conflicts == []
        assert out["counts"]["qualification_surface_conflicts"] == 0

    def test_qualification_surface_conflicts_none_when_surfaces_agree(self):
        # A genuinely agreeing set of surfaces should produce NO conflicts
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        version_phase_block = {
            "last_qualification": {
                "candidate_sha": "abc123def456789",
                "status": "promoted",
                "confidence": 0.95,
                "threshold": 0.80,
            },
            "qualification_state": {"state": "promoted"},
            "last_qualification_candidate_currency": {
                "verdict": "fresh",
                "resolved": True,
                "current": True,
                "candidate_sha": "abc123def456789",
                "head_sha": "abc123def456789",
            },
            # All criteria are covered
            "criterion_coverage": {
                "total": 5,
                "covered": 5,
                "partial": 0,
                "uncovered": 0,
            },
            "promotion_readiness": {
                "ready": True,
            },
        }
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=version_phase_block,
        )
        assert out is not None
        assert out["qualification_surface_conflicts"] == []
        assert out["counts"]["qualification_surface_conflicts"] == 0

    def test_qualification_surface_conflicts_promotion_readiness_unready(self):
        # promotion_readiness.ready=False while last_qualification.status=
        # "promotion_failed" should produce a promotion_readiness_unready
        # conflict when is_current_readiness=True (pre-go-live calibration mode).
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        version_phase_block = {
            "last_qualification": {
                "candidate_sha": "abc123def456789",
                "status": "promotion_failed",
                "confidence": 0.85,
                "threshold": 0.80,
            },
            # State kept consistent with status so only the promotion-readiness
            # conflict fires (promotion_failed is a valid state for this status).
            "qualification_state": {"state": "promotion_failed"},
            "last_qualification_candidate_currency": {
                "verdict": "fresh",
                "resolved": True,
                "current": True,
                "candidate_sha": "abc123def456789",
                "head_sha": "abc123def456789",
            },
            "criterion_coverage": {
                "total": 5,
                "covered": 5,
                "partial": 0,
                "uncovered": 0,
            },
            "promotion_readiness": {
                "ready": False,
                # is_current_readiness=True: pre-go-live calibration regime
                # where ready=False reflects actual track-record failure.
                "is_current_readiness": True,
            },
        }
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=version_phase_block,
        )
        assert out is not None
        conflicts = out["qualification_surface_conflicts"]
        pr_conflicts = [
            c
            for c in conflicts
            if c.get("conflict_type") == "promotion_readiness_unready"
        ]
        assert len(pr_conflicts) == 1
        assert "not ready" in pr_conflicts[0]["description"].lower()
        assert "promotion_failed" in pr_conflicts[0]["description"]

    def test_promotion_readiness_unready_suppressed_in_live_regime(self):
        # When is_current_readiness=False (live regime), promotion_readiness
        # forced ready=False by _annotate_regime is a regime relabel, not a
        # genuine track-record failure — the conflict must NOT be raised.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        version_phase_block = {
            "last_qualification": {
                "candidate_sha": "abc123def456789",
                "status": "promotion_failed",
                "confidence": 0.85,
                "threshold": 0.80,
            },
            "qualification_state": {"state": "promotion_failed"},
            "last_qualification_candidate_currency": {
                "verdict": "fresh",
                "resolved": True,
                "current": True,
                "candidate_sha": "abc123def456789",
                "head_sha": "abc123def456789",
            },
            "criterion_coverage": {
                "total": 5,
                "covered": 5,
                "partial": 0,
                "uncovered": 0,
            },
            "promotion_readiness": {
                "ready": False,
                # is_current_readiness=False: live regime, ready=False is a
                # regime relabel by _annotate_regime, not a real conflict.
                "is_current_readiness": False,
                # live_regime_calibration_artifact=True: machine-checkable signal
                # that ready=False is a regime artifact, not a real failure.
                "live_regime_calibration_artifact": True,
            },
        }
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=version_phase_block,
        )
        assert out is not None
        conflicts = out["qualification_surface_conflicts"]
        pr_conflicts = [
            c
            for c in conflicts
            if c.get("conflict_type") == "promotion_readiness_unready"
        ]
        # Live-regime forced ready=False must not surface as a conflict
        assert len(pr_conflicts) == 0
        # The promotion_readiness block carries the machine-checkable artifact flag.
        pr_block = version_phase_block["promotion_readiness"]
        assert pr_block.get("live_regime_calibration_artifact") is True

    def test_qualification_surface_conflicts_absent_without_version_phase_block(self):
        # When version_phase_block is None (no LTS split), the check simply
        # doesn't run, so the field is empty even if surfaces would disagree.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=None,
        )
        assert out is not None
        assert out["qualification_surface_conflicts"] == []
        assert out["counts"]["qualification_surface_conflicts"] == 0


# ── _build_prune_candidates: concrete cleanup list when the model is heavy ─────


class TestBuildPruneCandidates:
    """Concrete prune candidates are surfaced ONLY when the model is large or
    oversized, drawn from signals the backend already has. Pure function."""

    _LARGE = {"model_size_status": "large"}

    def test_returns_none_when_model_not_heavy(self):
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "gaps": [
                {
                    "id": "g1",
                    "name": "x",
                    "status": "active",
                    "active_moves": ["d-retired"],
                },
            ],
        }
        directions = [{"id": "d-retired", "status": "retired"}]
        for status in (
            None,
            {},
            {"model_size_status": "healthy"},
            {"model_size_status": "growing"},
        ):
            assert (
                _build_prune_candidates(
                    pm, directions, status, "2026-06-04T00:00:00+00:00"
                )
                is None
            )

    def test_returns_none_when_no_candidates_even_if_heavy(self):
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "gaps": [
                # Active gap worked by a still-active, never-merged direction.
                {
                    "id": "g1",
                    "name": "x",
                    "status": "active",
                    "active_moves": ["d-live"],
                },
            ],
            "capabilities": [
                {
                    "name": "ok",
                    "status": "working",
                    "verification": {"effective_status": "working"},
                },
            ],
        }
        directions = [{"id": "d-live", "status": "active", "research_history": []}]
        assert (
            _build_prune_candidates(
                pm, directions, self._LARGE, "2026-06-04T00:00:00+00:00"
            )
            is None
        )

    def test_surfaces_gap_linked_to_retired_direction(self):
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "gaps": [
                {
                    "id": "g-ret",
                    "name": "retired work",
                    "status": "active",
                    "active_moves": ["d-ret"],
                },
            ],
        }
        directions = [{"id": "d-ret", "status": "retired"}]
        out = _build_prune_candidates(
            pm,
            directions,
            {"model_size_status": "oversized"},
            "2026-06-04T00:00:00+00:00",
        )
        assert out is not None
        assert out["model_size_status"] == "oversized"
        assert len(out["resolved_gaps"]) == 1
        rg = out["resolved_gaps"][0]
        assert rg["gap_id"] == "g-ret"
        assert rg["reason"] == "direction_retired"
        assert rg["directions"] == ["d-ret"]
        assert out["counts"]["resolved_gaps"] == 1

    def test_surfaces_gap_linked_to_shipped_direction(self):
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "gaps": [
                {
                    "id": "g-ship",
                    "name": "shipped work",
                    "status": "active",
                    "active_moves": ["d-ship"],
                },
            ],
        }
        # Still active, but has merged PRs in its history → shipped.
        directions = [
            {
                "id": "d-ship",
                "status": "active",
                "research_history": [{"prs_merged": 2}],
            },
        ]
        out = _build_prune_candidates(
            pm, directions, self._LARGE, "2026-06-04T00:00:00+00:00"
        )
        assert len(out["resolved_gaps"]) == 1
        assert out["resolved_gaps"][0]["reason"] == "direction_shipped"

    def test_verified_merge_ships_gap_but_degraded_does_not(self):
        """Gap retirement is a ground-truth decision: only a verified merge
        retires the gap; a direction whose only merges are unverified_degraded
        (verified_prs_merged == 0) must NOT mark the gap direction_shipped."""
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "gaps": [
                {
                    "id": "g-deg",
                    "name": "degraded only",
                    "status": "active",
                    "active_moves": ["d-deg"],
                },
                {
                    "id": "g-ver",
                    "name": "verified",
                    "status": "active",
                    "active_moves": ["d-ver"],
                },
            ],
        }
        directions = [
            # Raw prs_merged>0 but the verification-aware count is 0 → not shipped.
            {
                "id": "d-deg",
                "status": "active",
                "research_history": [{"prs_merged": 3, "verified_prs_merged": 0}],
            },
            # A genuine verified merge → still ships the gap.
            {
                "id": "d-ver",
                "status": "active",
                "research_history": [{"prs_merged": 1, "verified_prs_merged": 1}],
            },
        ]
        out = _build_prune_candidates(
            pm, directions, self._LARGE, "2026-06-04T00:00:00+00:00"
        )
        resolved = {rg["gap_id"]: rg["reason"] for rg in out["resolved_gaps"]}
        assert resolved == {"g-ver": "direction_shipped"}
        assert "g-deg" not in resolved

    def test_surfaces_claimed_but_absent_capabilities(self):
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "capabilities": [
                {
                    "name": "ghost",
                    "status": "working",
                    "verification": {"effective_status": "absent"},
                },
                # partial is NOT absent → not a prune candidate here.
                {
                    "name": "partial-cap",
                    "status": "working",
                    "verification": {"effective_status": "partial"},
                },
                {
                    "name": "unverified-cap",
                    "status": "working",
                    "verification": {"effective_status": "unverified"},
                },
            ],
        }
        out = _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
        assert [c["name"] for c in out["absent_capabilities"]] == ["ghost"]
        assert out["absent_capabilities"][0]["stored_status"] == "working"
        assert out["counts"]["absent_capabilities"] == 1

    def test_surfaces_expired_hold_deferrals(self):
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "gaps": [
                # Hold elapsed.
                {
                    "id": "g-exp",
                    "name": "expired hold",
                    "status": "active",
                    "deferral": {
                        "rationale": "later",
                        "created_at": "x",
                        "hold_until": "2026-01-01T00:00:00+00:00",
                    },
                },
                # Hold still in the future → NOT a candidate.
                {
                    "id": "g-fut",
                    "name": "future hold",
                    "status": "active",
                    "deferral": {
                        "rationale": "later",
                        "created_at": "x",
                        "hold_until": "2027-01-01T00:00:00+00:00",
                    },
                },
                # Deferral with no hold_until → NOT a candidate.
                {
                    "id": "g-noh",
                    "name": "no hold",
                    "status": "active",
                    "deferral": {"rationale": "later", "created_at": "x"},
                },
            ],
        }
        out = _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
        assert [g["gap_id"] for g in out["expired_holds"]] == ["g-exp"]
        assert out["expired_holds"][0]["hold_until"] == "2026-01-01T00:00:00+00:00"

    def test_tolerates_malformed_hold_until(self):
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "gaps": [
                {
                    "id": "g-bad",
                    "name": "bad ts",
                    "status": "active",
                    "deferral": {
                        "rationale": "x",
                        "created_at": "x",
                        "hold_until": "not-a-timestamp",
                    },
                },
                {
                    "id": "g-num",
                    "name": "num ts",
                    "status": "active",
                    "deferral": {
                        "rationale": "x",
                        "created_at": "x",
                        "hold_until": 12345,
                    },
                },
            ],
        }
        # Malformed hold_until is ignored — no candidates, so None.
        assert (
            _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
            is None
        )

    def test_respects_cap_and_carries_true_totals(self):
        from app.strategic_spawning import _build_prune_candidates, _RECON_LIST_CAP

        gaps = [
            {
                "id": f"g-{i}",
                "name": f"resolved {i}",
                "status": "active",
                "active_moves": ["d-ret"],
            }
            for i in range(_RECON_LIST_CAP + 4)
        ]
        directions = [{"id": "d-ret", "status": "retired"}]
        out = _build_prune_candidates(
            {"gaps": gaps},
            directions,
            self._LARGE,
            "2026-06-04T00:00:00+00:00",
        )
        assert len(out["resolved_gaps"]) == _RECON_LIST_CAP
        assert out["counts"]["resolved_gaps"] == _RECON_LIST_CAP + 4

    def test_surfaces_ungroundable_capabilities(self):
        """Capabilities with no backing_paths, no source, no verification appear
        in ungroundable_capabilities; a capability with backing_paths is excluded."""
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "capabilities": [
                # No backing_paths, no source, no verification → ungroundable.
                {"name": "ungrounded-cap", "status": "planned"},
                # Has backing_paths → NOT ungroundable.
                {
                    "name": "grounded-cap",
                    "status": "working",
                    "backing_paths": ["backend/app/foo.py"],
                },
                # Has source → NOT ungroundable (even without backing_paths).
                {"name": "sourced-cap", "status": "working", "source": "documentation"},
                # Has verification → NOT ungroundable.
                {
                    "name": "verified-cap",
                    "status": "working",
                    "verification": {"effective_status": "working"},
                },
            ],
        }
        out = _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
        assert out is not None
        names = [c["name"] for c in out["ungroundable_capabilities"]]
        assert names == ["ungrounded-cap"]
        assert out["counts"]["ungroundable_capabilities"] == 1

    def test_prune_candidates_names_ungrounded_capabilities(self):
        """Acceptance gate: ungroundable caps surface and counts are correct."""
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "capabilities": [
                {"name": "no-ground-cap", "status": "working"},
                {"name": "another-no-ground", "status": "planned"},
                # excluded
                {
                    "name": "grounded",
                    "status": "working",
                    "backing_paths": ["path/to/file.py"],
                },
            ],
        }
        out = _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
        assert out is not None
        names = [c["name"] for c in out["ungroundable_capabilities"]]
        assert "no-ground-cap" in names
        assert "another-no-ground" in names
        assert "grounded" not in names
        assert out["counts"]["ungroundable_capabilities"] == 2

    def test_does_not_surface_cap_with_source_as_ungroundable(self):
        """A capability with a source field is NOT ungroundable even without
        backing_paths or verification."""
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "capabilities": [
                {
                    "name": "cap-with-source",
                    "status": "working",
                    "source": "design_doc",
                },
            ],
        }
        # source is present → not in ungroundable_capabilities → only candidate class
        # available is none → returns None.
        assert (
            _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
            is None
        )

    def test_domain_merge_groups_keyword_bucketing(self):
        """Capabilities sharing a keyword bucket form a merge group."""
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "capabilities": [
                {"name": "strategic-planning-cap", "status": "working"},
                {"name": "strategic-review-cap", "status": "working"},
                {"name": "strategic-spawning-cap", "status": "working"},
                # different bucket — no match
                {"name": "compute-scheduler", "status": "working"},
            ],
        }
        out = _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
        assert out is not None
        groups = {g["bucket"]: g["names"] for g in out["domain_merge_groups"]}
        assert "strategic" in groups
        assert len(groups["strategic"]) == 3
        # compute bucket: only one entry → no group
        assert "compute" not in groups

    def test_domain_merge_groups_no_groups_when_singletons(self):
        """When each keyword bucket has at most one capability, no merge groups are emitted."""
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "capabilities": [
                {"name": "strategic-review-cap", "status": "working"},
                {"name": "task-runner", "status": "working"},
                {"name": "operator-admin", "status": "working"},
            ],
        }
        out = _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
        # Each bucket has exactly one entry → no domain_merge_groups.
        # But long_names/ungroundable/heavy may fire — check domain_merge_groups specifically.
        if out is not None:
            assert out["domain_merge_groups"] == []
            assert out["counts"]["domain_merge_groups"] == 0

    def test_heavy_gaps_ranked_by_history_size(self):
        """Active gaps are ranked largest-first by serialized history weight."""
        from app.strategic_spawning import _build_prune_candidates

        def _history(n: int) -> list:
            return [
                {"event": f"entry-{i}", "ts": f"2026-0{i + 1}-01"} for i in range(n)
            ]

        pm = {
            "gaps": [
                {
                    "id": "g-small",
                    "name": "small gap",
                    "status": "active",
                    "history": _history(2),
                },
                {
                    "id": "g-large",
                    "name": "large gap",
                    "status": "active",
                    "history": _history(10),
                },
                {
                    "id": "g-medium",
                    "name": "medium gap",
                    "status": "active",
                    "history": _history(5),
                },
                # archived gap — not included in heavy_gaps
                {
                    "id": "g-arch",
                    "name": "archived gap",
                    "status": "archived",
                    "history": _history(20),
                },
            ],
        }
        out = _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
        assert out is not None
        ids = [g["gap_id"] for g in out["heavy_gaps"]]
        assert ids[0] == "g-large"
        assert ids[1] == "g-medium"
        assert ids[2] == "g-small"
        assert "g-arch" not in ids
        assert out["counts"]["heavy_gaps"] == 3

    def test_long_names_capabilities_and_gaps(self):
        """Names exceeding 80 chars appear in long_names; shorter names are excluded."""
        from app.strategic_spawning import _build_prune_candidates

        long_cap = "a" * 81
        long_gap = "b" * 90
        short_cap = "short-cap"
        short_gap = "short-gap"

        pm = {
            "capabilities": [
                {"name": long_cap, "status": "working"},
                {"name": short_cap, "status": "working"},
            ],
            "gaps": [
                {"id": "g-long", "name": long_gap, "status": "active"},
                {"id": "g-short", "name": short_gap, "status": "active"},
            ],
        }
        out = _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
        assert out is not None
        kinds_and_names = [(e["kind"], e["name"]) for e in out["long_names"]]
        long_entries = {name for _, name in kinds_and_names}
        assert long_cap[:120] in long_entries
        assert long_gap[:120] in long_entries
        assert short_cap not in long_entries
        assert short_gap not in long_entries
        assert out["counts"]["long_names"] == 2
        for e in out["long_names"]:
            assert e["length"] > 80

    def test_counts_includes_new_candidate_classes(self):
        """counts dict carries totals for all 7 candidate classes."""
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "capabilities": [
                # ungroundable
                {"name": "no-ground", "status": "planned"},
                # absent
                {
                    "name": "ghost",
                    "status": "working",
                    "verification": {"effective_status": "absent"},
                },
            ],
            "gaps": [
                # resolved
                {
                    "id": "g1",
                    "name": "g1",
                    "status": "active",
                    "active_moves": ["d-ret"],
                },
            ],
        }
        directions = [{"id": "d-ret", "status": "retired"}]
        out = _build_prune_candidates(
            pm, directions, self._LARGE, "2026-06-04T00:00:00+00:00"
        )
        assert out is not None
        for key in (
            "resolved_gaps",
            "absent_capabilities",
            "expired_holds",
            "ungroundable_capabilities",
            "domain_merge_groups",
            "heavy_gaps",
            "long_names",
        ):
            assert key in out["counts"], f"counts missing key: {key}"


# ── Onboarding goal reaches the first strategic cycle's payload ─────────────────
#
# The New Project onboarding flow seeds the user's goal into BOTH channels the
# strategic agent's bootstrap reads on cycle 1: problem_model.intent (purpose +
# current_focus) and a from_type='user' ProjectMessage that lands in the
# conversation thread. These tests lock in that the seeded intent and message
# actually reach the spawned strategic-cycle payloads, and that an empty goal
# leaves today's blank-slate behavior intact.

_ONBOARD_GOAL = "Ship a self-serve billing portal for small teams"


def _seeded_problem_model(goal: str) -> dict:
    """Build a problem_model dict shaped exactly like onboarding's seed.

    Mirrors admin_onboard._seed_strategic_intent: purpose + current_focus set
    to the goal with a user_message provenance source, validated through the
    real model so the test exercises the same payload the agent receives.
    """
    from app.problem_model import parse_problem_model, Source

    pm = parse_problem_model(None)
    pm.intent.purpose = goal
    pm.intent.current_focus = goal
    pm.intent.source = Source(type="user_message", excerpt=goal[:120])
    pm.last_updated_by = "user"
    return pm.model_dump()


@pytest.mark.asyncio
async def test_seeded_goal_reaches_strategic_review_payload(db_session):
    """Goal seeded at onboarding is visible to the first strategic_review cycle.

    Asserts both bootstrap channels carry it: problem_model.intent.purpose and
    the from_type='user' message in conversation_thread + open_messages.
    """
    project = _make_project(
        vision={},  # blank-slate vision, as a freshly-onboarded project has
        problem_model=_seeded_problem_model(_ONBOARD_GOAL),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMessage(project_id=project.id, from_type="user", content=_ONBOARD_GOAL)
    )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    # Channel 1: intent/purpose
    assert payload["problem_model"]["intent"]["purpose"] == _ONBOARD_GOAL
    # Channel 2: user-message conversation history
    thread_contents = [m["content"] for m in payload["conversation_thread"]]
    assert _ONBOARD_GOAL in thread_contents
    open_contents = [m["content"] for m in payload["open_messages"]]
    assert _ONBOARD_GOAL in open_contents


@pytest.mark.asyncio
async def test_seeded_goal_reaches_design_doc_update_payload(db_session):
    """Goal seeded at onboarding is visible to the Phase-1 design_doc_update cycle."""
    project = _make_project(
        vision={},
        problem_model=_seeded_problem_model(_ONBOARD_GOAL),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMessage(project_id=project.id, from_type="user", content=_ONBOARD_GOAL)
    )
    await db_session.commit()

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert payload["problem_model"]["intent"]["purpose"] == _ONBOARD_GOAL
    open_contents = [m["content"] for m in payload["open_messages"]]
    assert _ONBOARD_GOAL in open_contents


@pytest.mark.asyncio
async def test_operator_context_refs_promoted_to_strategic_review_payload(db_session):
    """Selected Strategic workbench refs become first-class pointer context."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    msg = ProjectMessage(
        project_id=project.id,
        from_type="user",
        message_type=MessageType.FEEDBACK,
        content="Please review the release blocker with the linked memo.",
        created_at=datetime(2026, 6, 3, 1, 0, 0),
        context={
            "strategic_session": {
                "version": 1,
                "interaction_mode": "continuous",
                "active_surface": "pipeline",
                "session_started_at": "2026-06-29T12:00:00Z",
                "captured_at": "2026-06-29T12:05:00Z",
                "context_package_id": "strategic-context:test",
                "selected_refs": [
                    {"kind": "task", "id": "task-1", "label": "Release blocker"},
                    {"kind": "task", "id": "task-1", "label": "Duplicate"},
                    {
                        "kind": "knowledge_document",
                        "id": "knowledge:doc-1",
                        "label": "Prior memo",
                    },
                ],
                "selected_ref_descriptors": [
                    {
                        "kind": "task",
                        "id": "task-1",
                        "label": "Release blocker task",
                        "summary": "Small task descriptor",
                        "status": "blocked",
                        "target": {
                            "surface": "task",
                            "task_id": "task-1",
                            "logs": "must not be copied",
                        },
                    }
                ],
                "canvas_body": "must not be copied",
            }
        },
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    entries = payload["operator_context_refs"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["source_message_id"] == str(msg.id)
    assert entry["context_package_id"] == "strategic-context:test"
    assert entry["session_started_at"] == "2026-06-29T12:00:00Z"
    assert entry["captured_at"] == "2026-06-29T12:05:00Z"

    selected_refs = entry["selected_refs"]
    assert selected_refs[:2] == [
        {
            "kind": "task",
            "id": "task-1",
            "label": "Release blocker",
            "descriptor": {
                "kind": "task",
                "id": "task-1",
                "label": "Release blocker task",
                "summary": "Small task descriptor",
                "status": "blocked",
                "target": {"surface": "task", "task_id": "task-1"},
            },
        },
        {
            "kind": "knowledge_document",
            "id": "knowledge:doc-1",
            "label": "Prior memo",
        },
    ]
    serialized = json.dumps(entries)
    assert "canvas_body" not in serialized
    assert "must not be copied" not in serialized
    assert "logs" not in serialized


@pytest.mark.asyncio
async def test_operator_context_refs_ignore_malformed_and_cap(db_session):
    """Malformed snapshots are skipped and promoted refs stay tightly bounded."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="agent",
            message_type=MessageType.NOTE,
            content="Agent-selected context should not be treated as operator context.",
            created_at=datetime(2026, 6, 3, 3, 0, 0),
            context={
                "strategic_session": {
                    "context_package_id": "strategic-context:agent",
                    "selected_refs": [{"kind": "task", "id": "agent-only"}],
                }
            },
        )
    )
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.NOTE,
            content="Malformed context is ignored.",
            created_at=datetime(2026, 6, 3, 2, 0, 0),
            context={"strategic_session": "not-a-snapshot"},
        )
    )
    refs = [
        {"kind": "task", "id": "task-with-large-label", "label": "L" * 2000},
        {"kind": "task", "id": "x" * 1000, "label": "oversized id"},
        {"kind": "", "id": "missing-kind"},
        "not-a-ref",
        *[
            {"kind": "task", "id": f"task-{i}", "label": f"Task {i}"}
            for i in range(_OPERATOR_CONTEXT_REFS_KEEP + 3)
        ],
    ]
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.FEEDBACK,
            content="Use the selected bounded context.",
            created_at=datetime(2026, 6, 3, 1, 0, 0),
            context={
                "strategic_session": {
                    "context_package_id": "strategic-context:bounded",
                    "selected_refs": refs,
                }
            },
        )
    )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]
    entries = payload["operator_context_refs"]

    assert len(entries) == 1
    selected_refs = entries[0]["selected_refs"]
    assert len(selected_refs) == _OPERATOR_CONTEXT_REFS_KEEP
    assert selected_refs[0] == {"kind": "task", "id": "task-with-large-label"}
    ref_ids = [ref["id"] for ref in selected_refs]
    assert "agent-only" not in ref_ids
    assert "x" * 1000 not in ref_ids
    assert ref_ids[-1] == f"task-{_OPERATOR_CONTEXT_REFS_KEEP - 2}"


@pytest.mark.asyncio
async def test_empty_goal_leaves_strategic_payload_blank(db_session):
    """A skipped goal preserves today's blank-slate behavior — no seeded
    purpose and no seeded user message reach the strategic payload."""
    project = _make_project(vision={}, problem_model={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    # No purpose was seeded (empty/absent problem_model intent).
    assert not (payload.get("problem_model") or {}).get("intent", {}).get("purpose")
    assert payload["conversation_thread"] == []
    assert payload["open_messages"] == []


@pytest.mark.asyncio
async def test_open_bug_report_survives_thread_cap_in_strategic_review(db_session):
    """An OPEN BUG_REPORT created before 15+ newer messages must still appear in
    the strategic_review open_messages inbox — not only in the architecture cycle.

    Regression guard for the thread-slice behavior: previously open_messages was
    derived by filtering the conversation_thread (capped at the 15 newest rows),
    so an old open BUG_REPORT aged out of the strategist's view entirely and
    could never be resolved.
    """
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Oldest row: an OPEN BUG_REPORT the strategist must keep seeing.
    bug_text = "Bug: strategist never sees this aged-out open report"
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="agent",
            message_type=MessageType.BUG_REPORT,
            content=bug_text,
            status="open",
            created_at=datetime(2026, 5, 1, 0, 0, 0),
        )
    )
    # 30 newer messages — well beyond the 15-row conversation_thread cap.
    for i in range(30):
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                from_type="user",
                message_type=MessageType.NOTE,
                content=f"newer chatter #{i}",
                status="open",
                created_at=datetime(2026, 6, 1, 0, 0, i),
            )
        )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    # The aged-out bug report is gone from the recency-capped thread...
    thread_contents = [m["content"] for m in payload["conversation_thread"]]
    assert bug_text not in thread_contents
    assert len(payload["conversation_thread"]) == 15
    # ...but the authoritative open-message inbox still surfaces it.
    open_contents = [m["content"] for m in payload["open_messages"]]
    assert bug_text in open_contents


@pytest.mark.asyncio
async def test_open_messages_not_derived_from_capped_thread(db_session):
    """The open-message inbox can exceed the 15-row conversation_thread cap.

    Proves open_messages is its own query, not a byproduct of the capped thread.
    """
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(25):
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                from_type="user",
                message_type=MessageType.NOTE,
                content=f"open message #{i}",
                status="open",
                created_at=datetime(2026, 6, 1, 0, 0, i),
            )
        )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    # All 25 open messages surface, far more than the thread cap of 15.
    assert len(payload["open_messages"]) == 25
    assert len(payload["conversation_thread"]) == 15


def test_summarize_open_messages_preserves_critical_and_oldest():
    """The budget-pressure fallback keeps BUG_REPORT/BLOCKER + oldest-open items
    and trims the rest, instead of dropping the whole inbox."""
    from app.strategic_spawning import (
        _summarize_open_messages,
        _OPEN_MESSAGES_KEEP_WHEN_TRIMMED,
        _OPEN_MESSAGE_EXCERPT_MAX_CHARS,
    )

    messages = []
    # One old critical BUG_REPORT (oldest) and one BLOCKER.
    messages.append(
        {
            "id": "bug",
            "type": "BUG_REPORT",
            "content": "x" * 5000,
            "created_at": "2026-05-01T00:00:00",
        }
    )
    messages.append(
        {
            "id": "blk",
            "type": "BLOCKER",
            "content": "blocked",
            "created_at": "2026-05-02T00:00:00",
        }
    )
    # Many newer NOTEs that should be trimmed away first.
    for i in range(50):
        messages.append(
            {
                "id": f"note{i}",
                "type": "NOTE",
                "content": f"note {i}",
                "created_at": f"2026-06-01T00:00:{i:02d}",
            }
        )

    trimmed = _summarize_open_messages(messages)

    assert len(trimmed) == _OPEN_MESSAGES_KEEP_WHEN_TRIMMED
    ids = {m["id"] for m in trimmed}
    # Critical items survive trimming.
    assert "bug" in ids
    assert "blk" in ids
    # Per-message excerpt is capped.
    bug = next(m for m in trimmed if m["id"] == "bug")
    assert len(bug["content"]) == _OPEN_MESSAGE_EXCERPT_MAX_CHARS


def test_reproducing_bug_survives_over_older_stale_one():
    """A reproducing bug report ranks higher than an older stale one.

    When the inbox must be trimmed under budget pressure, reproducing bug
    reports (citation_state != "absent") should rank higher in survival priority
    than stale ones (citation_state == "absent"). This ensures fresh, actionable
    bugs are preserved while stale ones (likely already fixed/refactored away)
    are deprioritized.
    """
    from app.strategic_spawning import _open_message_survival_key, _bound_open_messages

    messages = []
    # Old STALE bug report (citation_state == "absent")
    messages.append(
        {
            "id": "old_stale",
            "type": "BUG_REPORT",
            "content": "This bug is stale (cited code is absent)",
            "created_at": "2026-05-01T00:00:00",
            "citation_state": "absent",
        }
    )
    # Newer REPRODUCING bug report (no citation_state = not stale)
    messages.append(
        {
            "id": "new_reproducing",
            "type": "BUG_REPORT",
            "content": "This bug is fresh and reproducing",
            "created_at": "2026-06-01T00:00:00",
        }
    )

    # Verify the reproducing bug ranks higher (lower tuple value) than the stale one.
    stale_key = _open_message_survival_key(messages[0])
    reproducing_key = _open_message_survival_key(messages[1])

    # Reproducing should have tier=0, stale should have tier=1.
    assert reproducing_key[0] == 0  # tier 0: reproducing critical
    assert stale_key[0] == 1  # tier 1: stale critical
    # So reproducing_key < stale_key, meaning reproducing survives first.
    assert reproducing_key < stale_key

    # When both fit in the budget, both survive.
    trimmed = _bound_open_messages(messages, keep=2)
    ids = {m["id"] for m in trimmed}
    assert "old_stale" in ids
    assert "new_reproducing" in ids

    # When we must trim to 1 item, the reproducing bug survives.
    trimmed_strict = _bound_open_messages(messages, keep=1)
    assert len(trimmed_strict) == 1
    assert trimmed_strict[0]["id"] == "new_reproducing"


def test_open_message_citation_summary_promotes_absent_state_with_refs():
    """A fully absent citation signal is promoted with its broken/ungrounded refs."""
    from app.strategic_spawning import _open_message_citation_summary

    context = {
        "_provenance_warnings": {
            "citation_state": "absent",
            "broken_refs": ["backend/app/x.py"],
            "ungrounded_refs": ["backend/app/y.py:foo"],
        }
    }
    summary = _open_message_citation_summary(context)
    assert summary["citation_state"] == "absent"
    assert summary["broken_refs"] == ["backend/app/x.py"]
    assert summary["ungrounded_refs"] == ["backend/app/y.py:foo"]


def test_open_message_citation_summary_promotes_mixed_present_state_with_refs():
    """A partially grounded (present) report keeps its ref detail instead of being stripped."""
    from app.strategic_spawning import _open_message_citation_summary

    context = {
        "_provenance_warnings": {
            "citation_state": "present",
            "broken_refs": ["backend/app/x.py"],
            "ungrounded_refs": [],
        }
    }
    summary = _open_message_citation_summary(context)
    # Non-absent state is preserved (NOT collapsed/stripped) so the strategist
    # can see it is mixed rather than fully stale.
    assert summary["citation_state"] == "present"
    assert summary["broken_refs"] == ["backend/app/x.py"]
    assert "ungrounded_refs" not in summary


def test_open_message_citation_summary_promotes_indeterminate_state_with_refs():
    """An indeterminate report carrying ungrounded refs surfaces those refs."""
    from app.strategic_spawning import _open_message_citation_summary

    context = {
        "_provenance_warnings": {
            "citation_state": "indeterminate",
            "broken_refs": [],
            "ungrounded_refs": ["backend/app/z.py:bar"],
        }
    }
    summary = _open_message_citation_summary(context)
    assert summary["citation_state"] == "indeterminate"
    assert summary["ungrounded_refs"] == ["backend/app/z.py:bar"]
    assert "broken_refs" not in summary


def test_open_message_citation_summary_omits_non_absent_state_without_refs():
    """A non-absent state with no broken/ungrounded refs has nothing to flag."""
    from app.strategic_spawning import _open_message_citation_summary

    context = {
        "_provenance_warnings": {
            "citation_state": "present",
            "broken_refs": [],
            "ungrounded_refs": [],
        }
    }
    assert _open_message_citation_summary(context) == {}


def test_open_message_citation_summary_empty_without_warning():
    """No recorded warning yields an empty summary (no keys added)."""
    from app.strategic_spawning import _open_message_citation_summary

    assert _open_message_citation_summary({}) == {}
    assert _open_message_citation_summary(None) == {}
    assert _open_message_citation_summary({"_provenance_warnings": "nope"}) == {}


@pytest.mark.asyncio
async def test_open_message_counts_ship_when_inbox_dropped(db_session):
    """Trim-proof open-message count ships at critical tier and survives a full inbox drop.

    open_message_count is computed from the raw feed BEFORE packing and placed at
    critical tier, so even when the entire open_messages list is dropped under byte
    budget pressure the agent can distinguish a genuinely empty inbox from a dropped
    one and knows to ./fetch messages list to retrieve them.
    """
    from app.payload_schemas import pack_under_budget
    from app.strategic_spawning import (
        _summarize_open_messages,
        _floor_open_messages,
    )

    # Build a schema that gives open_messages only 1 byte of budget by inflating
    # all critical+high content to crowd it out — simulating extreme byte pressure
    # where the inbox would normally be dropped wholesale.
    # We test the pack contract directly rather than spawning a task.
    messages = [
        {
            "id": f"msg-{i}",
            "type": "NOTE",
            "content": f"operator message #{i} with some content",
            "created_at": f"2026-06-01T00:00:{i:02d}",
        }
        for i in range(40)
    ]

    # Use a tiny budget so open_messages (high priority, multi-KB) gets dropped
    # while open_message_count (critical, tiny scalar) always ships.
    tiny_budget = 512  # bytes — far too small to hold the message list

    packed = pack_under_budget(
        {
            "open_message_count": len(messages),
            "open_messages": messages,
        },
        schema={
            "open_message_count": {"priority": "critical"},
            "open_messages": {
                "priority": "high",
                "summarize": _summarize_open_messages,
                "floor": _floor_open_messages,
            },
        },
        budget=tiny_budget,
    )

    meta = packed["_packer_meta"]
    dropped_keys = {entry["key"] for entry in meta["dropped"]}
    trimmed_keys = {entry["key"] for entry in meta["trimmed"]}

    # The trim-proof count must always ship at critical tier.
    assert "open_message_count" in packed, (
        "open_message_count must ship at critical tier even under extreme budget pressure"
    )
    assert packed["open_message_count"] == 40, (
        f"open_message_count must equal the full pre-trim inbox depth; got {packed.get('open_message_count')}"
    )
    # The critical scalar must never appear in trimmed or dropped lists.
    assert "open_message_count" not in dropped_keys | trimmed_keys, (
        f"open_message_count must not be dropped or trimmed; meta={meta}"
    )
    # open_messages was dropped or trimmed (i.e. the inbox did NOT ship in full).
    assert "open_messages" in dropped_keys or "open_messages" in trimmed_keys, (
        "Under tiny budget open_messages must be dropped or trimmed to exercise the count-fallback path"
    )
    # open_messages must NOT appear in packed when dropped.
    if "open_messages" in dropped_keys:
        assert "open_messages" not in packed, (
            "Dropped open_messages must not appear in the packed payload"
        )


class TestParallelismHint:
    """_build_parallelism_hint nudges toward enough INDEPENDENT direction lanes.

    Target = min(max_active_directions, max_concurrent_vms) — derived from config,
    never hardcoded. The hint fires below that target and goes quiet at/above it.
    """

    def test_at_or_above_target_returns_empty(self):
        from app.strategic_spawning import _build_parallelism_hint

        # At the target (min(max_active, vms)): no nag.
        assert (
            _build_parallelism_hint(8, max_concurrent_vms=8, max_active_directions=10)
            == ""
        )
        # max_active_directions is the target even when the fleet could run more VMs.
        assert (
            _build_parallelism_hint(10, max_concurrent_vms=15, max_active_directions=10)
            == ""
        )
        # Above the VM-clamped target also no nag.
        assert (
            _build_parallelism_hint(10, max_concurrent_vms=8, max_active_directions=10)
            == ""
        )

    def test_below_target_nags_above_the_old_floor(self):
        from app.strategic_spawning import _build_parallelism_hint

        # Core behavior change: the old gate went silent at 3 directions. Now 4 on
        # a 15-VM fleet (target 10) still nags, and 5 of 8 (target 8) still nags.
        assert _build_parallelism_hint(
            4, max_concurrent_vms=15, max_active_directions=10
        )
        assert _build_parallelism_hint(
            5, max_concurrent_vms=8, max_active_directions=10
        )

    def test_below_target_emits_independence_framed_hint(self):
        from app.strategic_spawning import _build_parallelism_hint

        hint = _build_parallelism_hint(
            1, max_concurrent_vms=8, max_active_directions=10
        )
        assert hint  # non-empty
        assert "PARALLELISM" in hint
        assert "1 active direction" in hint
        assert "8 VMs" in hint
        # Frames it as decomposing into INDEPENDENT, non-conflicting lanes (not a quota).
        assert "INDEPENDENT" in hint
        assert "conflicts_with" in hint
        assert "load-bearing" in hint

    def test_capacity_is_the_limit_does_not_nag(self):
        from app.strategic_spawning import _build_parallelism_hint

        # Fleet can only run 2 VMs, so 2 active directions is NOT underutilized.
        assert (
            _build_parallelism_hint(2, max_concurrent_vms=2, max_active_directions=10)
            == ""
        )
        # But 1 active on a 2-VM fleet still leaves a lane idle.
        assert _build_parallelism_hint(
            1, max_concurrent_vms=2, max_active_directions=10
        )

    def test_unknown_capacity_targets_max_active_and_omits_vm_clause(self):
        from app.strategic_spawning import _build_parallelism_hint

        # No capacity signal → target falls back to max_active_directions; no VM clause.
        hint = _build_parallelism_hint(
            1, max_concurrent_vms=0, max_active_directions=10
        )
        assert hint
        assert "VMs at once" not in hint  # no concrete VM count when capacity unknown
        # At the max_active_directions target with unknown capacity: no nag.
        assert (
            _build_parallelism_hint(10, max_concurrent_vms=0, max_active_directions=10)
            == ""
        )


# ── Deploy-model framing (integration-branch landing ≠ deployment) ────────────


async def _spawn_strategic_review_payload(project, session) -> dict:
    """Run a strategy-only cycle and return the spawned strategic_review payload."""
    old = _shared_state.runtime_config.get("architecture_review_frequency", 3)
    _shared_state.runtime_config["architecture_review_frequency"] = 3
    try:
        await _spawn_research_task(project, session)
    finally:
        _shared_state.runtime_config["architecture_review_frequency"] = old
    result = await session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1, f"Expected one task, got {len(tasks)}"
    payload = tasks[0].payloads[0]
    assert payload["type"] == "strategic_review", payload["type"]
    return payload


@pytest.mark.asyncio
async def test_deploy_framing_present_when_integration_and_promotion_differ(db_session):
    """When the integration branch and promotion target differ (the LTS split),
    the strategic payload carries the deploy-model framing so the agent does not
    treat 'landed on the integration branch' as 'deployed/live'."""
    project = _make_project(
        vision={"_strategic_cycle_count": 1, "_research_spawn_count": 5},
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    payload = await _spawn_strategic_review_payload(project, db_session)
    suffix = payload.get("prompt_suffix") or ""
    assert "Deployment model" in suffix, "deploy framing missing from prompt_suffix"
    # Both sides of the split are named, using the project's real branch names.
    assert "`automain`" in suffix and "`main`" in suffix
    # The operative warnings that defuse the enable-and-measure / hard-pivot trap.
    assert "not yet PROMOTED" in suffix
    assert "nothing live to measure" in suffix


@pytest.mark.asyncio
async def test_deploy_framing_absent_when_branches_equal(db_session):
    """A single-branch project (integration branch == promotion target) deploys
    merges directly, so the deploy-model framing must NOT appear — it would be
    actively misleading there."""
    project = _make_project(
        vision={"_strategic_cycle_count": 1, "_research_spawn_count": 5},
        integration_branch="main",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    payload = await _spawn_strategic_review_payload(project, db_session)
    suffix = payload.get("prompt_suffix") or ""
    assert "Deployment model" not in suffix, (
        "deploy framing must be omitted when integration branch == promotion target"
    )


@pytest.mark.asyncio
async def test_deploy_framing_uses_project_branch_names_not_hardcoded(db_session):
    """The framing is project-generic: it interpolates the project's own
    integration/promotion branch names and never hardcodes Blueprint's
    'automain'/'main' literals."""
    project = _make_project(
        vision={"_strategic_cycle_count": 1, "_research_spawn_count": 5},
        integration_branch="staging",
        promotion_target="release",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    payload = await _spawn_strategic_review_payload(project, db_session)
    suffix = payload.get("prompt_suffix") or ""
    assert "Deployment model" in suffix
    assert "`staging`" in suffix and "`release`" in suffix
    # No Blueprint-specific branch literals leak into a non-Blueprint project.
    assert "automain" not in suffix
    assert "`main`" not in suffix


# ── Realtime strategic_cycle_started emission (see app.strategic_events) ───────


@pytest.mark.asyncio
async def test_spawn_strategic_review_emits_cycle_started(db_session):
    """_spawn_strategic_review broadcasts strategic_cycle_started (phase 2) post-commit."""
    project = _make_project(
        directions=[
            {
                "id": "d-evt01",
                "status": "active",
                "text": "A direction",
                "priority": "medium",
                "research_history": [{"prs_merged": 1}],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    with patch(
        "app.strategic_events.broadcast_to_project", new=AsyncMock()
    ) as mock_bcast:
        spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    started = [
        c.args
        for c in mock_bcast.await_args_list
        if c.args[1] == "strategic_cycle_started"
    ]
    assert len(started) == 1, "expected exactly one strategic_cycle_started broadcast"
    _proj_id, _event, data = started[0]
    assert _proj_id == str(project.id)
    assert data["phase"] == 2
    assert data.get("task_id")


@pytest.mark.asyncio
async def test_arch_cycle_emits_cycle_started_phase1(db_session):
    """A design_doc_update spawn broadcasts strategic_cycle_started with phase 1."""
    project = _make_project(
        vision={
            "_strategic_cycle_count": 2,
            "_research_spawn_count": 5,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old = _shared_state.runtime_config.get("architecture_review_frequency", 3)
    _shared_state.runtime_config["architecture_review_frequency"] = 3
    try:
        with patch(
            "app.strategic_events.broadcast_to_project", new=AsyncMock()
        ) as mock_bcast:
            await _spawn_research_task(project, db_session)
    finally:
        _shared_state.runtime_config["architecture_review_frequency"] = old

    started = [
        c.args
        for c in mock_bcast.await_args_list
        if c.args[1] == "strategic_cycle_started"
    ]
    assert len(started) == 1, "expected one strategic_cycle_started for the arch spawn"
    assert started[0][2]["phase"] == 1


@pytest.mark.asyncio
async def test_running_update_superseded_signal_present_when_recent_ledger_entry(
    db_session,
):
    """A recent running_update_superseded ledger entry surfaces a derived signal
    in the strategic payload so next cycle's thinker treats the document as
    operator-authoritative."""
    from app.running_update_merge import RUNNING_UPDATE_REJECTED_KIND
    from app.vision_merge import REJECTED_EDITS_KEY

    recent_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    project = _make_project(
        directions=[
            {
                "id": "d-rus01",
                "status": "active",
                "text": "Some direction",
                "priority": "medium",
            }
        ],
        vision={
            "purpose": "test purpose",
            REJECTED_EDITS_KEY: [
                {
                    "at": recent_at,
                    "field": "running_update",
                    "kind": RUNNING_UPDATE_REJECTED_KIND,
                    "agent_value": "superseded agent doc",
                },
                # An unrelated vision field conflict sharing the ledger — must be ignored.
                {
                    "at": recent_at,
                    "field": "purpose",
                    "kind": "field_conflict",
                    "agent_value": "x",
                },
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "running_update_superseded" in payload
    signal = payload["running_update_superseded"]
    assert signal["count"] == 1
    assert signal["most_recent_at"] == recent_at


@pytest.mark.asyncio
async def test_running_update_superseded_signal_absent_without_ledger_entry(db_session):
    """No running_update_superseded ledger entry → field omitted from the payload."""
    project = _make_project(
        directions=[
            {
                "id": "d-rus02",
                "status": "active",
                "text": "Some direction",
                "priority": "medium",
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "running_update_superseded" not in payload


@pytest.mark.asyncio
async def test_running_update_superseded_signal_excludes_stale_entries(db_session):
    """A ledger entry older than the 30-day window is not surfaced."""
    from app.running_update_merge import RUNNING_UPDATE_REJECTED_KIND
    from app.vision_merge import REJECTED_EDITS_KEY

    stale_at = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    project = _make_project(
        directions=[
            {
                "id": "d-rus03",
                "status": "active",
                "text": "Some direction",
                "priority": "medium",
            }
        ],
        vision={
            "purpose": "test purpose",
            REJECTED_EDITS_KEY: [
                {
                    "at": stale_at,
                    "field": "running_update",
                    "kind": RUNNING_UPDATE_REJECTED_KIND,
                    "agent_value": "old superseded doc",
                }
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "running_update_superseded" not in payload


# ── operator-steered cycle: steering directive in prompt_suffix ───────────────


def _operator_refresh_marker(reason: str = "operator_message") -> dict:
    """A pending operator-origin strategic-refresh marker (as request_strategic_refresh writes it)."""
    return {
        "requested_at": "2026-06-03T00:00:00",
        "reason": reason,
        "count": 1,
        "origin": ORIGIN_OPERATOR,
    }


@pytest.mark.asyncio
async def test_operator_steered_cycle_appends_steering_directive_to_prompt_suffix(
    db_session,
):
    """An operator-origin spawn surfaces a high-priority steering directive that
    flags the cycle as operator-steered AND quotes the operator's message."""
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": _operator_refresh_marker(),
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    steering_text = "Please pivot the roadmap toward billing reliability this week."
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.FEEDBACK,
            content=steering_text,
            created_at=datetime(2026, 6, 3, 1, 0, 0),
        )
    )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "OPERATOR STEERING" in suffix
    assert "operator steering" in suffix.lower()
    # The actual operator message must be quoted, not just referenced.
    assert steering_text in suffix


@pytest.mark.asyncio
async def test_qualification_origin_marker_adds_no_steering_directive(db_session):
    """A qualification-origin marker must NOT be framed as operator steering."""
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": {
                "requested_at": "2026-06-03T00:00:00",
                "reason": "qualification_verdict",
                "count": 1,
                "origin": ORIGIN_QUALIFICATION,
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.FEEDBACK,
            content="A user message that must not be promoted to a steering directive.",
            created_at=datetime(2026, 6, 3, 1, 0, 0),
        )
    )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "OPERATOR STEERING" not in payload.get("prompt_suffix", "")


@pytest.mark.asyncio
async def test_no_marker_adds_no_steering_directive(db_session):
    """A cadence cycle (no pending marker at all) carries no steering framing."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.FEEDBACK,
            content="Just a routine question, not operator steering of this cycle.",
            created_at=datetime(2026, 6, 3, 1, 0, 0),
        )
    )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "OPERATOR STEERING" not in payload.get("prompt_suffix", "")


@pytest.mark.asyncio
async def test_operator_steering_directive_quotes_most_recent_message_and_truncates(
    db_session,
):
    """With several operator messages, the newest is quoted and long content is bounded."""
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": _operator_refresh_marker(),
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    old_text = "An older operator note from earlier in the session."
    newest_text = "NEWEST: " + ("x" * 2000)  # exceeds the 600-char quote cap
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.FEEDBACK,
            content=old_text,
            created_at=datetime(2026, 6, 3, 1, 0, 0),
        )
    )
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.FEEDBACK,
            content=newest_text,
            created_at=datetime(2026, 6, 3, 2, 0, 0),
        )
    )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    suffix = payload["prompt_suffix"]
    # Newest message is present (its distinctive prefix) and is truncated.
    assert "NEWEST:" in suffix
    assert "…" in suffix
    # The full 2000-char body must not appear verbatim — it was bounded.
    assert ("x" * 2000) not in suffix


# ── architecture-cycle operator wake: directive survives Phase 1 → Phase 2 ────
#
# Regression for review iter-1 MAJOR (autonomous_research.py:1193): on an
# architecture cycle the operator-origin refresh marker is consumed when Phase 1
# (design_doc_update) is recorded, so by the time Phase 2 (strategic_review)
# chains off the design-doc completion the marker reads None and the steering
# directive would be silently dropped (~1/3 of operator wakes). The fix threads
# an ``operator_steered`` flag through the design_doc task context into Phase 2.


@pytest.mark.asyncio
async def test_design_doc_update_stamps_operator_steered_when_marker_operator_origin(
    db_session,
):
    """Phase 1 stamps ``operator_steered`` on the design_doc task context when an
    operator-origin refresh marker is armed, so the signal survives the marker's
    consumption and reaches Phase 2."""
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": _operator_refresh_marker(),
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True

    result = await db_session.execute(
        _select_agent_tasks(project.id)
    )
    task = result.scalars().all()[0]
    assert task.payloads[0]["type"] == "design_doc_update"
    assert (task.context or {}).get("operator_steered") is True


@pytest.mark.asyncio
async def test_design_doc_update_start_event_includes_cycle_and_steering(
    db_session, mocker
):
    steering_excerpt = "Focus the next strategy cycle on billing reliability."
    emit = AsyncMock()
    mocker.patch("app.strategic_events.emit_strategic_cycle_started", new=emit)
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": _operator_refresh_marker_with_excerpt(
                steering_excerpt
            ),
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert await _spawn_design_doc_update(project, db_session) is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().all()[0]
    emit.assert_awaited_once()
    assert emit.await_args.args == (project.id, task.id, 1)
    assert emit.await_args.kwargs["cycle_id"] == task.context["strategic_cycle_id"]
    assert emit.await_args.kwargs["steering_origin"] == ORIGIN_OPERATOR
    assert emit.await_args.kwargs["steering_excerpt"] == steering_excerpt


@pytest.mark.asyncio
async def test_design_doc_update_no_operator_steered_when_no_marker(db_session):
    """A cadence (non-operator) architecture cycle carries no operator_steered flag."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().all()[0]
    assert "operator_steered" not in (task.context or {})


@pytest.mark.asyncio
async def test_strategic_review_start_event_includes_cycle_and_steering(
    db_session, mocker
):
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    emit = AsyncMock()
    mocker.patch("app.strategic_events.emit_strategic_cycle_started", new=emit)
    cycle_id = "sc-phase2evt"
    steering_excerpt = "Tie the outcome to the operator steer."
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert (
        await _spawn_strategic_review(
            project,
            db_session,
            cycle_id=cycle_id,
            operator_steered=True,
            steering_excerpt=steering_excerpt,
        )
        is True
    )

    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().all()[0]
    emit.assert_awaited_once()
    assert emit.await_args.args == (project.id, task.id, 2)
    assert emit.await_args.kwargs == {
        "cycle_id": cycle_id,
        "steering_origin": ORIGIN_OPERATOR,
        "steering_excerpt": steering_excerpt,
    }


@pytest.mark.asyncio
async def test_architecture_cycle_operator_wake_reaches_phase2_after_marker_cleared(
    db_session, mocker
):
    """End-to-end: an operator wake landing on an architecture cycle still
    surfaces the steering directive in the Phase 2 strategic_review prompt, even
    though the refresh marker was consumed when Phase 1 was recorded.

    This is the path the four strategy-only tests above never exercise:
    design_doc_update → _handle_design_doc_completion → _spawn_strategic_review.
    """
    from app.task_completion import _handle_design_doc_completion

    # Phase-1 → Phase-2 completion emits cycle events; stub them so the test
    # exercises only the steering-directive threading, not the SSE layer.
    mocker.patch("app.strategic_events.emit_strategic_cycle_started", new=AsyncMock())
    mocker.patch("app.strategic_events.emit_strategic_cycle_completed", new=AsyncMock())

    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": _operator_refresh_marker(),
        },
        design_doc={"overview": {"body": "x"}, "_meta": {"revision": 1}},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    steering_text = "Pivot the roadmap toward billing reliability this week."
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.FEEDBACK,
            content=steering_text,
            created_at=datetime(2026, 6, 3, 1, 0, 0),
        )
    )
    await db_session.commit()

    # Phase 1: spawn the design_doc_update task (stamps operator_steered).
    assert await _spawn_design_doc_update(project, db_session) is True
    design_doc_task = (
        (
            await db_session.execute(
                _select_agent_tasks(project.id)
            )
        )
        .scalars()
        .all()[0]
    )
    assert (design_doc_task.context or {}).get("operator_steered") is True

    # Simulate the autonomous loop consuming the operator marker when it records
    # the Phase 1 spawn (clear_strategic_refresh in autonomous_research.py), and
    # mark Phase 1 complete so the Phase 2 active-task guard does not skip.
    from app.strategic_trigger import strategic_refresh_origin

    preferred_runner_id = str(uuid.uuid4())
    design_doc_task.context = {
        **(design_doc_task.context or {}),
        "runner_target_id": preferred_runner_id,
        STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
        STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: "workbench_continuous",
    }
    design_doc_task.vm_instance_id = f"runner:{preferred_runner_id}"
    project.vision = {
        k: v for k, v in project.vision.items() if k != "_strategic_refresh"
    }
    design_doc_task.status = TaskStatus.COMPLETED
    db_session.add(project)
    db_session.add(design_doc_task)
    await db_session.commit()
    await db_session.refresh(project)
    # Precondition: the marker is gone, so Phase 2 cannot re-derive operator
    # origin from vision — it must rely on the threaded flag.
    assert strategic_refresh_origin(project.vision) is None

    # Phase 2: chain the strategic review off the design-doc completion.
    await _handle_design_doc_completion(
        design_doc_task, "", db_session, project=project
    )

    strategic_task = (
        (
            await db_session.execute(
                _select_agent_tasks(project.id)
            )
        )
        .scalars()
        .all()
    )
    review = next(
        t for t in strategic_task if t.payloads[0]["type"] == "strategic_review"
    )
    suffix = review.payloads[0].get("prompt_suffix", "")
    assert "OPERATOR STEERING" in suffix
    assert steering_text in suffix
    assert (
        (review.context or {}).get("preferred_runner_target_id")
        == preferred_runner_id
    )
    assert (
        (review.context or {}).get(STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY)
        is True
    )
    assert (
        (review.context or {}).get(STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY)
        == "workbench_continuous"
    )
    assert "runner_target_id" not in (review.context or {})
    assert not (review.vm_instance_id or "").startswith("runner:")


# ── persisted steering excerpt survives the conversation-thread aging out ─────
#
# Root-cause regression: the operator's message is re-derived from a short,
# volatile recent-messages snapshot at spawn time. When enough messages
# accumulate before the deferred cycle reaches its review phase, that snapshot
# no longer carries the operator message and the directive was silently dropped.
# The fix persists the excerpt on the refresh marker at arm time and uses it as
# a durable fallback when the live thread yields nothing.


def _operator_refresh_marker_with_excerpt(excerpt: str) -> dict:
    marker = _operator_refresh_marker()
    marker["excerpt"] = excerpt
    return marker


def test_steering_directive_falls_back_to_excerpt_when_thread_has_no_operator_msg():
    """With no qualifying operator message in the thread, the directive is built
    from the persisted fallback excerpt rather than returning None."""
    excerpt = "Persisted steering: prioritize billing reliability."
    directive = _build_operator_steering_directive([], fallback_excerpt=excerpt)
    assert directive is not None
    assert "OPERATOR STEERING" in directive
    assert excerpt in directive


def test_steering_directive_fallback_excerpt_is_truncated():
    """A long fallback excerpt is bounded by the same per-message quote cap."""
    long_excerpt = "LEAD: " + ("y" * 2000)
    directive = _build_operator_steering_directive([], fallback_excerpt=long_excerpt)
    assert directive is not None
    assert "LEAD:" in directive
    assert "…" in directive
    assert ("y" * 2000) not in directive


def test_steering_directive_none_when_no_thread_and_no_excerpt():
    """No operator message and no fallback excerpt → no steering framing."""
    assert _build_operator_steering_directive([], fallback_excerpt=None) is None
    assert _build_operator_steering_directive([], fallback_excerpt="   ") is None


def test_steering_directive_prefers_live_thread_over_fallback_excerpt():
    """When the live thread still carries the operator message, that message is
    quoted in preference to the (possibly stale) fallback excerpt."""
    live = [
        {
            "from": "user",
            "type": MessageType.FEEDBACK.value,
            "content": "LIVE thread steering message.",
        }
    ]
    directive = _build_operator_steering_directive(
        live, fallback_excerpt="STALE fallback excerpt."
    )
    assert directive is not None
    assert "LIVE thread steering message." in directive
    assert "STALE fallback excerpt." not in directive


@pytest.mark.asyncio
async def test_strategy_only_cycle_uses_marker_excerpt_when_message_aged_out(
    db_session,
):
    """The operator message has aged out of the recent-messages snapshot, but the
    excerpt persisted on the marker still produces a steering directive."""
    excerpt = "Persisted steering: ship the billing reliability fix this week."
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": _operator_refresh_marker_with_excerpt(excerpt),
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)
    # No operator ProjectMessage seeded — it has aged out of the snapshot.

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]
    suffix = payload.get("prompt_suffix", "")
    assert "OPERATOR STEERING" in suffix
    assert excerpt in suffix


@pytest.mark.asyncio
async def test_design_doc_update_stamps_steering_excerpt_from_marker(db_session):
    """Phase 1 persists the marker excerpt on the design_doc task context so Phase
    2 can rebuild the directive after the marker is consumed."""
    excerpt = "Persisted steering: focus on billing reliability."
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": _operator_refresh_marker_with_excerpt(excerpt),
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().all()[0]
    assert (task.context or {}).get("operator_steered") is True
    assert (task.context or {}).get("steering_excerpt") == excerpt


# ── regression: operator steering survives a *fully aged-out* recent-messages ──
# snapshot. The parent bug is that the operator's text is re-derived from a
# short (15-message) volatile snapshot at the deferred cycle's review phase;
# when that window is entirely crowded out by system-posted digest events the
# operator message is gone and — pre-fix — the directive vanished silently.
# The tests below pin the durable-excerpt fallback against exactly that
# "snapshot is non-empty but carries zero qualifying operator steering" shape
# that the existing empty-thread / single-message tests above never exercise.


def _system_note_digest_thread(n: int) -> list[dict]:
    """A conversation_thread snapshot of *n* system-posted NOTE digest events.

    Mirrors the shape ``_spawn_strategic_review`` builds from ``ProjectMessage``
    rows: ``from``/``type``/``content``/``context`` keys. These are passive
    NOTEs carrying a routine digest ``event_type`` (``cycle_summary``), so
    ``_build_operator_steering_directive``'s ``classify_operator_arming`` gate
    rejects every one — the thread is non-empty yet yields zero qualifying
    operator steering messages, the exact aged-out condition the fix targets.
    ``from='user'`` deliberately passes the cheap from-filter so the test proves
    the *classification* gate (not just the author check) is what excludes them.
    """
    return [
        {
            "from": "user",
            "type": MessageType.NOTE.value,
            "content": f"[cycle digest {i}] routine system summary line.",
            "context": {"event_type": "cycle_summary"},
        }
        for i in range(n)
    ]


def test_steering_directive_fallback_when_thread_is_all_system_notes():
    """Req 1 — deferred-cycle directive fallback.

    With a *populated* 15-entry thread that contains zero qualifying operator
    steering messages (all system-posted NOTE digest events), the persisted
    fallback excerpt must still produce a non-None directive whose body quotes
    the excerpt verbatim. Pre-fix (no excerpt fallback) this returned None and
    the operator's steering was dropped."""
    excerpt = "Pivot the roadmap toward billing reliability this quarter."
    thread = _system_note_digest_thread(15)

    directive = _build_operator_steering_directive(thread, fallback_excerpt=excerpt)

    assert directive is not None
    assert "OPERATOR STEERING" in directive
    # The excerpt content appears verbatim (it is short enough to dodge the cap).
    assert excerpt in directive
    # None of the crowding system-digest lines leaked into the directive.
    assert "cycle digest" not in directive


@pytest.mark.asyncio
async def test_spawn_strategic_review_thread_aged_out_uses_steering_excerpt(
    db_session,
):
    """Req 2 — thread-aged-out end-to-end through ``_spawn_strategic_review``.

    An operator FEEDBACK message is seeded with an *old* timestamp, then 15
    newer system NOTE digest messages crowd it out of the 15-row recent-messages
    window the spawn builds its ``conversation_thread`` from. With
    ``operator_steered=True`` and a threaded ``steering_excerpt`` (the Phase 2
    architecture-cycle path, where the marker was already consumed at Phase 1),
    the directive must still reach the payload's ``prompt_suffix`` from the
    durable excerpt."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    # The real operator steering message — but stamped old so it sorts out of
    # the 15-most-recent window once the digest events pile up on top of it.
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.FEEDBACK,
            content="Original operator steering that has since aged out.",
            created_at=datetime(2026, 6, 1, 0, 0, 0),
        )
    )
    # 15 newer system-posted digest NOTEs that crowd the operator message out.
    for i in range(15):
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                from_type="system",
                message_type=MessageType.NOTE,
                content=f"[cycle digest {i}] routine system summary.",
                context={"event_type": "cycle_summary"},
                created_at=datetime(2026, 6, 3, 1, 0, i),
            )
        )
    await db_session.commit()

    steering_excerpt = "Pivot toward billing reliability"
    spawned = await _spawn_strategic_review(
        project,
        db_session,
        operator_steered=True,
        steering_excerpt=steering_excerpt,
    )
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "OPERATOR STEERING" in suffix
    assert steering_excerpt in suffix
    # Sanity: the operator message really did age out of the shipped snapshot,
    # so the directive came from the durable excerpt — not the live thread.
    thread_contents = [
        m.get("content", "") for m in payload.get("conversation_thread", [])
    ]
    assert not any("aged out" in c for c in thread_contents)


@pytest.mark.asyncio
async def test_spawn_design_doc_update_no_excerpt_when_operator_marker_lacks_excerpt(
    db_session,
):
    """Req 3 (negative edge) — Phase 1 stamps ``operator_steered`` but NOT a
    ``steering_excerpt`` when the consumed operator marker carries no ``excerpt``
    key (an old-format marker, or an empty-content steering message). The
    positive path is covered by
    ``test_design_doc_update_stamps_steering_excerpt_from_marker`` above; this
    pins that a missing excerpt does not synthesize a bogus one."""
    project = _make_project(
        vision={
            "purpose": "test purpose",
            # Operator-origin marker with NO "excerpt" field.
            "_strategic_refresh": _operator_refresh_marker(),
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().all()[0]
    ctx = task.context or {}
    assert ctx.get("operator_steered") is True
    # No excerpt on the marker → no steering_excerpt key on the task context.
    assert "steering_excerpt" not in ctx


@pytest.mark.asyncio
async def test_spawn_design_doc_update_stamps_longer_distinct_excerpt(db_session):
    """Req 3 (value-flow) — the exact marker excerpt flows verbatim onto the
    Phase 1 task context, independent of the specific text. Uses a distinct,
    longer excerpt than the sibling positive test so the assertion proves a
    value copy rather than a hardcoded default."""
    excerpt = (
        "Refocus the next cycle on the auth-token refresh race that is dropping "
        "operator sessions during deploys; deprioritize the dashboard polish."
    )
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": _operator_refresh_marker_with_excerpt(excerpt),
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().all()[0]
    assert (task.context or {}).get("steering_excerpt") == excerpt


def _recent_strategic_task(project_id, minutes_ago: int) -> Task:
    """A terminal strategic task created within the spawn-cooldown window.

    Puts ``_spawn_research_task`` on the cooldown branch so an armed operator
    marker is what drives the strategic spawn (and its consume), mirroring the
    helper in test_autonomous_research.py."""
    return Task(
        project_id=project_id,
        title="Recent strategic cycle",
        payloads=[{"type": "design_doc_update", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=_utcnow_naive() - timedelta(minutes=minutes_ago),
    )


@pytest.mark.asyncio
async def test_steering_acted_confirmation_skipped_when_marker_has_no_excerpt(
    db_session, mocker
):
    """Req 4 (negative) — ``_maybe_notify_operator_steering_acted`` is NOT called
    when the consumed marker is operator-origin but carries no ``excerpt`` (an
    old-format marker / empty-content message). With no preserved text the woken
    cycle has no directive to act on, so the confirmation must be suppressed."""
    mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    notify_mock = mocker.patch(
        "app.autonomous_research._maybe_notify_operator_steering_acted",
        new=AsyncMock(),
    )

    project = _make_project(
        vision={
            "purpose": "test",
            "_strategic_refresh": {
                "requested_at": _utcnow_naive().isoformat(),
                "reason": "operator_message",
                "count": 1,
                "origin": ORIGIN_OPERATOR,
                # no "excerpt" key
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(_recent_strategic_task(project.id, minutes_ago=5))
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    # Marker was consumed by the spawn ...
    assert (project.vision or {}).get("_strategic_refresh") is None
    # ... but with no excerpt the confirmation was suppressed.
    assert notify_mock.await_count == 0


@pytest.mark.asyncio
async def test_steering_acted_confirmation_posted_when_marker_has_excerpt(
    db_session, mocker
):
    """Req 4 (positive contrast) — ``_maybe_notify_operator_steering_acted`` IS
    called when the consumed marker is operator-origin AND carries a non-empty
    ``excerpt``: the operator's steering text was preserved, so the woken cycle
    genuinely has a directive to act on."""
    mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    notify_mock = mocker.patch(
        "app.autonomous_research._maybe_notify_operator_steering_acted",
        new=AsyncMock(),
    )

    project = _make_project(
        vision={
            "purpose": "test",
            "_strategic_refresh": {
                "requested_at": _utcnow_naive().isoformat(),
                "reason": "operator_message",
                "count": 1,
                "origin": ORIGIN_OPERATOR,
                "excerpt": "Please refocus on the billing reliability work.",
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(_recent_strategic_task(project.id, minutes_ago=5))
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    assert (project.vision or {}).get("_strategic_refresh") is None
    # Excerpt was preserved → confirmation fired exactly once.
    assert notify_mock.await_count == 1


@pytest.mark.asyncio
async def test_arm_operator_refresh_persists_excerpt_and_replaces_on_coalesce(
    db_session,
):
    """Req 5 — excerpt stored on the marker at arm time, replaced on coalesce.

    ``maybe_arm_operator_refresh`` (the wake-gate both message endpoints share)
    must persist the operator's message text on the refresh marker as the
    steering excerpt. A second steering message coalesces into the single-slot
    marker and replaces the excerpt with the newer text — the most recent
    operator message is the active steering intent."""
    from app.strategic_trigger import (
        ARMING_STEERING,
        maybe_arm_operator_refresh,
        strategic_refresh_excerpt,
    )

    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    armed = await maybe_arm_operator_refresh(
        db_session,
        project.id,
        MessageType.FEEDBACK,
        message_content="test steering content",
    )
    assert armed == ARMING_STEERING
    await db_session.commit()
    await db_session.refresh(project)

    marker = (project.vision or {}).get("_strategic_refresh")
    assert isinstance(marker, dict)
    assert marker.get("origin") == ORIGIN_OPERATOR
    # Short content (< cap) is stored verbatim.
    assert marker.get("excerpt") == "test steering content"
    assert strategic_refresh_excerpt(project.vision) == "test steering content"

    # A second steering message coalesces and replaces the excerpt.
    armed2 = await maybe_arm_operator_refresh(
        db_session,
        project.id,
        MessageType.FEEDBACK,
        message_content="newer steering content wins",
    )
    assert armed2 == ARMING_STEERING
    await db_session.commit()
    await db_session.refresh(project)

    marker2 = (project.vision or {}).get("_strategic_refresh")
    assert marker2.get("excerpt") == "newer steering content wins"
    # Coalesced into the same single slot (not stacked into a second marker).
    assert marker2.get("count") == 2


@pytest.mark.asyncio
async def test_open_bug_report_older_than_thread_cap_appears_in_open_messages(
    db_session,
):
    """Open BUG_REPORT created before 15+ newer messages still appears in payload.

    Regression test for: strategist's open-message feed survives the 15-msg
    thread cap. The conversation_thread is capped at 15 recent messages of ANY
    status, but open_messages must come from a dedicated query so open
    BUG_REPORT/BLOCKER items older than the thread cap are NOT aged out of view.
    """
    project = _make_project(
        vision={"purpose": "test project"},
        problem_model={},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create an old open BUG_REPORT (will be older than the 15-message cap).
    old_bug_content = "Ancient bug that needs fixing"
    old_bug = ProjectMessage(
        project_id=project.id,
        from_type="system",
        message_type=MessageType.BUG_REPORT,
        content=old_bug_content,
        status="open",
    )
    db_session.add(old_bug)
    await db_session.commit()

    # Create 20+ newer messages to push the old bug out of the 15-message
    # conversation_thread window.
    for i in range(20):
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                from_type="system" if i % 2 == 0 else "user",
                message_type=MessageType.NOTE if i % 3 == 0 else MessageType.FEEDBACK,
                content=f"Recent message {i}",
                status="resolved" if i % 5 == 0 else "open",
            )
        )
    await db_session.commit()

    # Spawn the strategic review; the old bug should still appear in open_messages.
    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    payload = tasks[0].payloads[0]

    # The old bug should be in open_messages despite being aged out of
    # conversation_thread (which is capped at 15 most recent rows).
    open_msg_contents = [m["content"] for m in payload["open_messages"]]
    assert old_bug_content in open_msg_contents, (
        "Old open BUG_REPORT aged out of conversation_thread must still appear "
        "in open_messages via dedicated status-filtered query"
    )

    # Verify the old bug is NOT in conversation_thread (proves it aged out).
    thread_contents = [m["content"] for m in payload["conversation_thread"]]
    assert old_bug_content not in thread_contents, (
        "Old bug aged out of 15-message thread cap as expected"
    )


def _pathological_problem_model(n_gaps: int = 400) -> dict:
    """A problem_model whose ACTIVE gap list (no count cap by design) has grown
    so large that the count-based agent-view trim alone cannot fit it under the
    payload byte budget."""
    big = "x" * 2000  # bulky free-text body per gap
    gaps = []
    for i in range(n_gaps):
        gaps.append(
            {
                "id": f"g-{i}",
                "name": f"gap {i}",
                "description": big,
                "evidence": big,
                "priority": "high" if i % 3 == 0 else "low",
                "status": "active",
                "opened": f"2026-01-{(i % 28) + 1:02d}T00:00:00Z",
                "history": [{"ts": "2026-01-01T00:00:00Z", "event": big}],
                "past_moves": [f"d-{i}-{j}" for j in range(20)],
            }
        )
    return {"gaps": gaps, "capabilities": [], "constraints": []}


def test_agent_view_problem_model_packed_shrinks_under_floor():
    """A pathologically large model is shrunk under the guaranteed-small floor
    and carries a marker of what was shortened — never returned oversized."""
    pm = _pathological_problem_model()
    packed = _agent_view_problem_model_packed(pm)

    serialized = len(json.dumps(packed, default=str).encode())
    assert serialized <= _AGENT_VIEW_FLOOR_BYTES, (
        f"floor shrink must bring the model under {_AGENT_VIEW_FLOOR_BYTES} "
        f"bytes, got {serialized}"
    )
    marker = packed.get("_agent_view_floor_applied")
    assert marker is not None, "shrink must leave a loud, visible marker"
    assert marker["floor_bytes"] == _AGENT_VIEW_FLOOR_BYTES
    # At least the highest-value gaps survive so the strategist can still reason.
    assert len(packed["gaps"]) >= _FLOOR_MIN_GAPS


def _caps_friction_heavy_problem_model(
    n_caps: int = 120, n_friction: int = 120
) -> dict:
    """A problem_model whose bulk lives in capabilities and friction (verbose
    free-text bodies), with few/no gaps — so gap-shrinking alone cannot reach
    the floor."""
    big = "y" * 800
    caps = []
    for i in range(n_caps):
        caps.append(
            {
                "name": f"cap {i}",
                "status": "working" if i % 2 == 0 else "planned",
                "notes": big,
                "source": {"kind": "pr", "ref": big},
                "backing_paths": [f"backend/app/mod_{i}_{j}.py" for j in range(8)],
            }
        )
    friction = []
    for i in range(n_friction):
        friction.append(
            {
                "id": f"f-{i}",
                "name": f"friction {i}",
                "status": "active" if i % 2 == 0 else "resolved",
                "description": big,
                "seen_in": [f"d-{i}-{j}" for j in range(8)],
                "mitigations_tried": [big],
                "source": {"kind": "agent_observation", "ref": big},
            }
        )
    return {"gaps": [], "capabilities": caps, "friction": friction}


def test_floor_shrinks_caps_and_friction_when_gaps_are_not_the_bulk():
    """A capability/friction-heavy model (few/no gaps) is shrunk under the floor
    by reducing capabilities and friction to a name+status skeleton, and is
    delivered with an honest summarized-section marker — never dropped."""
    pm = _caps_friction_heavy_problem_model()
    packed = _agent_view_problem_model_packed(pm)

    serialized = len(json.dumps(packed, default=str).encode())
    assert serialized <= _AGENT_VIEW_FLOOR_BYTES, (
        f"caps/friction-heavy model must be shrunk under {_AGENT_VIEW_FLOOR_BYTES} "
        f"bytes, got {serialized}"
    )
    marker = packed.get("_agent_view_floor_applied")
    assert marker is not None, "shrink must leave a loud, visible marker"
    assert marker["summarized_capabilities"] > 0, "capabilities must be reduced"
    assert marker["summarized_friction"] > 0, "friction must be reduced"
    # Reduced entries are name+status skeletons — detail fields are gone.
    cap = packed["capabilities"][0]
    assert "notes" not in cap and "backing_paths" not in cap and "source" not in cap
    assert {"name", "status"} <= set(cap.keys())
    fric = packed["friction"][0]
    assert "description" not in fric and "seen_in" not in fric
    assert "mitigations_tried" not in fric and "source" not in fric
    assert {"id", "name", "status"} <= set(fric.keys())


def test_floor_leaves_caps_friction_untouched_when_gaps_are_the_bulk():
    """Regression: when gap-shrinking alone reaches the floor, capabilities and
    friction are NOT touched and no summarized-section marker is recorded — the
    gap-only shrink path is unchanged."""
    # Gaps whose bulk is the (strippable) append-only history; stripping the
    # first floor level brings the model under the floor without dropping gaps.
    big = "z" * 1200
    gaps = [
        {
            "id": f"g-{i}",
            "name": f"gap {i}",
            "status": "active",
            "priority": "high",
            "opened": "2026-01-01T00:00:00Z",
            "history": [{"ts": "2026-01-01T00:00:00Z", "event": big}],
            "past_moves": [big],
        }
        for i in range(30)
    ]
    pm = {
        "gaps": gaps,
        "capabilities": [
            {
                "name": "keep me",
                "status": "working",
                "notes": "n",
                "backing_paths": ["a.py"],
            }
        ],
        "friction": [
            {"id": "f-keep", "name": "keep", "status": "active", "description": "d"}
        ],
    }
    packed = _agent_view_problem_model_packed(pm)

    marker = packed["_agent_view_floor_applied"]
    assert marker["dropped_gaps"] == 0, "field-stripping should reach the floor"

    marker = packed["_agent_view_floor_applied"]
    assert "summarized_capabilities" not in marker
    assert "summarized_friction" not in marker
    # Detail-bearing capability/friction entries survive intact.
    assert packed["capabilities"][0]["notes"] == "n"
    assert packed["friction"][0]["description"] == "d"


def test_floor_no_pressure_path_is_byte_identical():
    """A model that already fits the floor is returned byte-identical — no
    marker, no reordering, no shrink."""
    pm = {
        "gaps": [{"id": "g1", "name": "g", "status": "active", "priority": "high"}],
        "capabilities": [{"name": "c", "status": "working", "notes": "n"}],
        "friction": [{"id": "f1", "name": "f", "status": "active", "description": "d"}],
    }
    packed = _agent_view_problem_model_packed(pm)
    assert "_agent_view_floor_applied" not in packed
    assert packed["capabilities"][0]["notes"] == "n"
    assert packed["friction"][0]["description"] == "d"


def test_pathological_problem_model_degrades_loudly_when_it_cannot_fit():
    """The GENUINELY EXCEPTIONAL case: a problem_model so large it cannot fit the
    packer budget even on its own. The floor reservation now equals the WHOLE
    agent-view model, so an oversized model's reservation exceeds the budget and
    the packer drops the floor guarantee — at which point the ``summarize``
    callback (_agent_view_problem_model_packed -> _shrink_pm_to_floor) degrades
    the model LOUDLY rather than silently: it ships a bounded floored skeleton
    carrying the ``_agent_view_floor_applied`` marker, recorded in
    ``_packer_meta['trimmed']`` so the agent's problemModelRecovery resolves to
    'fetch'. This is the visibly-exceptional fallback, never the normal mode.

    Siblings are kept modest here so the degradation is clearly attributable to
    the model's own size — the normal/contended whole-model guarantee (where the
    model fits and siblings yield first) is exercised separately in
    ``test_strategic_review_ships_whole_problem_model_under_contention``."""
    pm = _pathological_problem_model()  # ~MBs of active-gap bulk, >> 120KB budget
    payload = {
        "type": "strategic_review",
        "project_name": "p",
        "base_branch": "main",
        "model": "claude-opus-4-8",
        "effort": "medium",
        "vision": {"purpose": "small"},
        "problem_model": pm,
        "model_health": {"model_size_status": "oversized", "gap_count": 400},
    }
    packed = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA)

    meta = packed["_packer_meta"]
    dropped_keys = {d["key"] for d in meta["dropped"]}

    # The model is DELIVERED (degraded), never silently dropped.
    assert "problem_model" in packed, (
        "problem_model must be delivered degraded, never silently dropped"
    )
    assert "problem_model" not in dropped_keys, (
        f"problem_model was dropped: {meta['dropped']}"
    )
    # Degraded loudly: the floored skeleton carries the marker so the agent
    # knows it must ./fetch the full model.
    assert "_agent_view_floor_applied" in packed["problem_model"], (
        "an oversized model must ship floored with a loud marker"
    )
    serialized = len(json.dumps(packed["problem_model"], default=str).encode())
    assert serialized <= _AGENT_VIEW_FLOOR_BYTES, (
        f"floored model must fit the {_AGENT_VIEW_FLOOR_BYTES}-byte floor, got {serialized}"
    )
    # Recorded in _packer_meta['trimmed'] (kind=summarized) so problemModelRecovery
    # becomes 'fetch'. This is the agent's single fetch-decision channel.
    trimmed_pm = [t for t in meta["trimmed"] if t["key"] == "problem_model"]
    assert trimmed_pm, (
        f"the degraded model must be recorded in _packer_meta['trimmed']: {meta['trimmed']}"
    )
    assert trimmed_pm[0]["kind"] == "summarized"
    # The tiny size/health signal still ships alongside it.
    assert "model_health" in packed
    assert "model_health" not in dropped_keys


def _mature_agent_view_problem_model() -> dict:
    """A realistic, mature-but-healthy agent-view problem_model that fits well
    under the 120KB packer budget while carrying full per-entry detail.

    Sized at the healthy thresholds (12 gaps / 20 capabilities / 8 friction) with
    substantial — but not pathological — free-text bodies, so the WHOLE model is
    expected to ship inline under normal cycle sizes (the floor never fires)."""
    gap_body = "g" * 1600
    gaps = [
        {
            "id": f"g-{i}",
            "name": f"gap {i}: concrete delta between intent and capability {i}",
            "description": gap_body,
            "evidence": gap_body,
            "priority": "high" if i % 2 == 0 else "medium",
            "status": "active",
            "opened": f"2026-02-{(i % 27) + 1:02d}T00:00:00Z",
            "history": [{"ts": "2026-02-01T00:00:00Z", "event": "opened"}],
            "past_moves": [f"d-{i}-{j}" for j in range(3)],
        }
        for i in range(12)
    ]
    cap_notes = "c" * 1400
    capabilities = [
        {
            "name": f"cap {i}: registry-derived subsystem {i}",
            "status": "working" if i % 2 == 0 else "partial",
            "notes": cap_notes,
            "source": {"type": "shipped_change", "pr_number": 7000 + i},
            "backing_paths": [
                f"backend/app/mod_{i}.py",
                f"backend/testing/test_mod_{i}.py",
            ],
        }
        for i in range(20)
    ]
    fric_body = "f" * 1400
    friction = [
        {
            "id": f"f-{i}",
            "name": f"friction {i}: recurring blocker observed across moves",
            "status": "active",
            "description": fric_body,
            "seen_in": [f"d-{i}", f"d-{i + 1}"],
            "mitigations_tried": [f"tried approach {k}" for k in range(3)],
        }
        for i in range(8)
    ]
    return {"gaps": gaps, "capabilities": capabilities, "friction": friction}


def test_strategic_review_ships_whole_problem_model_under_contention():
    """The NORMAL operating mode: a realistic mature problem_model ships WHOLE
    inline even when recoverable siblings genuinely contend for the budget.

    Because the floor reservation now equals the whole agent-view model, its
    bytes are reserved off the top of the budget, so the recoverable siblings
    (directions_summary, conversation_thread, self_reflection_log, …) degrade or
    drop FIRST while the model keeps its full capabilities and friction detail.
    The skeletonizing path (_skeletonize_caps_friction / _shrink_pm_to_floor)
    must NOT fire: no ``_agent_view_floor_applied`` marker, full per-entry detail
    preserved, and the model is neither in the dropped nor the trimmed list."""
    pm = _mature_agent_view_problem_model()
    model_bytes = len(json.dumps(pm, default=str).encode())
    # Sanity: the realistic model is comfortably under the 120KB budget so the
    # whole-model guarantee is the expected outcome (not the floored fallback).
    assert model_bytes < 120 * 1024

    payload = {
        "type": "strategic_review",
        "research_mode": "strategic",
        "project_name": "p",
        "base_branch": "main",
        "model": "claude-opus-4-8",
        "effort": "medium",
        "vision": {"purpose": "x" * 8000},
        "problem_model": pm,
        "model_health": {"model_size_status": "healthy", "gap_count": 12},
        # Recoverable, summarizable siblings sized to genuinely contend so the
        # reservation is what protects the model — not slack budget.
        "directions_summary": [
            {"id": f"d-{i}", "text": "z" * 1100, "priority": "high"} for i in range(50)
        ],
        "conversation_thread": [
            {"id": f"m-{i}", "role": "user", "content": "w" * 1100} for i in range(40)
        ],
        "self_reflection_log": [{"what_was_hard": "s" * 1100} for _ in range(20)],
    }
    packed = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA)

    meta = packed["_packer_meta"]
    dropped_keys = {d["key"] for d in meta["dropped"]}
    trimmed_keys = {t["key"] for t in meta["trimmed"]}

    # Genuine contention: at least one recoverable sibling degraded or dropped.
    assert (dropped_keys | trimmed_keys) - {"problem_model"}, (
        f"expected recoverable siblings to yield first; meta={meta}"
    )

    # The WHOLE model shipped inline — present, not dropped, not trimmed.
    assert "problem_model" in packed
    assert "problem_model" not in dropped_keys, f"model dropped: {meta['dropped']}"
    assert "problem_model" not in trimmed_keys, f"model trimmed: {meta['trimmed']}"

    shipped = packed["problem_model"]
    # The skeletonizing floor path did NOT fire.
    assert "_agent_view_floor_applied" not in shipped, (
        "the whole model must ship without the floored/skeletonized marker"
    )
    # Full per-entry detail survived — capabilities/friction were NOT collapsed
    # to name+status skeletons.
    assert len(shipped["capabilities"]) == 20
    assert len(shipped["friction"]) == 8
    assert len(shipped["gaps"]) == 12
    assert shipped["capabilities"][0].get("notes"), "capability detail was collapsed"
    assert shipped["capabilities"][0].get("backing_paths"), (
        "capability detail collapsed"
    )
    assert shipped["friction"][0].get("description"), "friction detail was collapsed"


def test_strategic_review_degrades_loudly_in_near_budget_band():
    """The NEAR-BUDGET band: a problem_model that fits the budget on its OWN, but
    whose bytes plus the always-critical identity/vision keys exceed it. The
    full-model floor reservation is ``<= budget`` (so the old drop condition kept
    the guarantee), yet the critical ``vision`` key sorts first and would be
    starved into a hard PayloadTooLargeError. The reworked packer accounts for
    non-floored critical keys' bytes and drops the floor guarantee, so the model
    degrades LOUDLY via summarize (marker + _packer_meta record) and the cycle
    still runs — it must NOT raise or silently drop the model."""
    pm = _mature_agent_view_problem_model()
    model_bytes = len(json.dumps(pm, default=str).encode())
    # A large but load-bearing critical vision. model alone fits the budget;
    # model + vision does not.
    vision = {"purpose": "p" * 12000}
    vision_bytes = len(json.dumps(vision, default=str).encode())
    # Budget fits the model alone (reservation <= budget) but NOT model + vision.
    budget = model_bytes + vision_bytes - 2000
    assert model_bytes <= budget < model_bytes + vision_bytes

    payload = {
        "type": "strategic_review",
        "research_mode": "strategic",
        "project_name": "p",
        "base_branch": "main",
        "model": "claude-opus-4-8",
        "effort": "medium",
        "vision": vision,
        "problem_model": pm,
        "model_health": {"model_size_status": "healthy", "gap_count": 12},
    }
    # Must NOT raise PayloadTooLargeError — the cycle still runs.
    packed = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA, budget=budget)

    # The critical vision shipped intact.
    assert packed["vision"] == vision
    meta = packed["_packer_meta"]
    dropped_keys = {d["key"] for d in meta["dropped"]}

    # The model is delivered DEGRADED (floored), never silently dropped.
    assert "problem_model" in packed
    assert "problem_model" not in dropped_keys, f"model dropped: {meta['dropped']}"
    assert "_agent_view_floor_applied" in packed["problem_model"], (
        "the degraded model must carry the loud floor-applied marker"
    )
    # Recorded in _packer_meta['trimmed'] so problemModelRecovery resolves to 'fetch'.
    trimmed_pm = [t for t in meta["trimmed"] if t["key"] == "problem_model"]
    assert trimmed_pm and trimmed_pm[0]["kind"] == "summarized", (
        f"the degraded model must be recorded as summarized: {meta['trimmed']}"
    )


# ── design_doc compact index in the strategic payload ─────────────────────────
# The strategic cycle ships the design doc as a COMPACT, BOUNDED navigation
# index (key + bounded summary + reconciled presence status/modules/warning for
# systems/capabilities) instead of full inline prose; full per-section content
# is fetched on demand via POST /api/agent/design-doc (op:get).


_BIG_DESIGN_PROSE = "LOREM_" + ("x" * 4000)


def _design_doc_with_sections(prose: str) -> dict:
    """A design doc whose section bodies carry *prose* (made large to prove the
    index does not scale with content)."""
    return {
        "systems/auth": {
            "status": "mature",
            "description": prose,
            "modules": ["backend/app/auth.py", "backend/app/routers/agent.py"],
        },
        "capabilities/login": {
            "status": "working",
            "notes": prose,
            "modules": ["backend/app/auth.py"],
        },
        "notes/roadmap": {"title": "Roadmap", "body": prose},
        # Operator-only / internal keys — must be excluded from the index.
        "_meta": {"last_updated": "2026-01-01"},
        "playbook_archive": [{"key": "x", "value": prose}],
    }


def test_design_doc_index_is_complete():
    """Every non-internal section is represented by its key; operator-only and
    underscore-prefixed keys are excluded."""
    index = _design_doc_index(_design_doc_with_sections("short prose"))
    keys = {e["key"] for e in index["sections"]}
    assert keys == {"systems/auth", "capabilities/login", "notes/roadmap"}
    assert "_meta" not in keys
    assert "playbook_archive" not in keys


def test_design_doc_index_excludes_proposed_changes_bookkeeping_key():
    """proposed_changes is the reconciliation queue, shipped as its OWN top-level
    payload field — it is not an architecture section and must not pollute the
    index with a summary-less 'Other sections' entry."""
    doc = _design_doc_with_sections("short prose")
    doc["proposed_changes"] = [{"key": "systems/auth", "op": "set"}]
    keys = {e["key"] for e in _design_doc_index(doc)["sections"]}
    assert "proposed_changes" not in keys
    assert keys == {"systems/auth", "capabilities/login", "notes/roadmap"}


def test_design_doc_index_carries_reconciled_presence():
    """systems/* and capabilities/* entries carry the reconciled presence
    status, the declared backing modules, and any presence_warning."""
    doc = {
        "systems/auth": {
            "status": "missing",
            "description": "x",
            "modules": ["backend/app/auth.py"],
            "presence_warning": (
                "claimed mature but all referenced module file(s) are absent"
            ),
        },
        "capabilities/login": {
            "status": "partial",
            "notes": "y",
            "modules": ["backend/app/auth.py"],
        },
    }
    by_key = {e["key"]: e for e in _design_doc_index(doc)["sections"]}
    assert by_key["systems/auth"]["status"] == "missing"
    assert by_key["systems/auth"]["modules"] == ["backend/app/auth.py"]
    assert by_key["systems/auth"]["presence_warning"].startswith("claimed mature")
    assert by_key["capabilities/login"]["status"] == "partial"
    assert by_key["capabilities/login"]["modules"] == ["backend/app/auth.py"]
    # notes/* are not presence-reconciled — no status field.
    assert "status" not in _design_doc_index({"notes/x": {"body": "z"}})["sections"][0]


def test_design_doc_index_is_bounded_does_not_scale_with_prose():
    """The index serialized size stays roughly stable when section prose grows
    massively — per-entry summaries are bounded, so the 4000x blow-up does not
    propagate."""
    small = _design_doc_index(_design_doc_with_sections("short"))
    large = _design_doc_index(_design_doc_with_sections(_BIG_DESIGN_PROSE))
    small_bytes = len(json.dumps(small).encode())
    large_bytes = len(json.dumps(large).encode())
    # The big-prose variant adds at most a few bounded summaries' worth of bytes.
    assert large_bytes - small_bytes < 3 * (_DESIGN_DOC_INDEX_SUMMARY_CHARS + 64)
    for entry in large["sections"]:
        if "summary" in entry:
            assert len(entry["summary"]) <= _DESIGN_DOC_INDEX_SUMMARY_CHARS


def test_design_doc_index_summary_truncated_with_ellipsis():
    doc = {"notes/x": {"summary": "A" * (_DESIGN_DOC_INDEX_SUMMARY_CHARS + 500)}}
    summary = _design_doc_index(doc)["sections"][0]["summary"]
    assert len(summary) <= _DESIGN_DOC_INDEX_SUMMARY_CHARS
    assert summary.endswith("…")


def test_design_doc_index_empty_doc():
    assert _design_doc_index(None) == {"sections": []}
    assert _design_doc_index({}) == {"sections": []}


def test_summarize_design_doc_index_drops_summaries_keeps_skeleton():
    """The budget-pressure fallback drops per-entry summaries but keeps the
    navigation skeleton (key + status + modules + presence_warning)."""
    index = _design_doc_index(
        {
            "systems/auth": {
                "status": "mature",
                "description": "prose here",
                "modules": ["backend/app/auth.py"],
            }
        }
    )
    assert "summary" in index["sections"][0]
    skeleton = _summarize_design_doc_index(index)
    entry = skeleton["sections"][0]
    assert "summary" not in entry
    assert entry["key"] == "systems/auth"
    assert entry["status"] == "mature"
    assert entry["modules"] == ["backend/app/auth.py"]


@pytest.mark.asyncio
async def test_strategic_review_ships_design_doc_index_not_full_prose(
    db_session, mocker
):
    """The strategic_review packed payload carries the compact design_doc index
    (not full inline prose) for every non-internal section, with reconciled
    presence statuses on systems/capabilities."""
    unique = "UNIQUE_PROSE_MARKER_SR_" + ("z" * 4000)
    design_doc = {
        "systems/auth": {
            "status": "mature",
            "description": unique,
            "modules": ["backend/app/auth.py"],
        },
        "capabilities/login": {
            "status": "working",
            "notes": unique,
            "modules": ["backend/app/auth.py"],
        },
        "notes/roadmap": {"title": "Roadmap", "body": unique},
    }
    # Passthrough grounding so the index is built from exactly this doc and the
    # presence statuses are deterministic (no git/GitHub dependency in tests).
    mocker.patch(
        "app.strategic_spawning.ground_design_doc",
        new=AsyncMock(side_effect=lambda doc, **kw: doc),
    )
    project = _make_project(vision={"purpose": "p"}, design_doc=design_doc)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    dd = payload["design_doc"]
    assert set(dd.keys()) == {"sections"}
    by_key = {e["key"]: e for e in dd["sections"]}
    assert set(by_key) == {"systems/auth", "capabilities/login", "notes/roadmap"}
    assert by_key["systems/auth"]["status"] == "mature"
    assert by_key["systems/auth"]["modules"] == ["backend/app/auth.py"]
    # Full prose is NOT inline anywhere in the packed design_doc field.
    assert unique not in json.dumps(dd)


@pytest.mark.asyncio
async def test_design_doc_update_ships_design_doc_index_not_full_prose(
    db_session, mocker
):
    """The design_doc_update packed payload also carries the compact index and
    not the full inline prose."""
    unique = "UNIQUE_PROSE_MARKER_DDU_" + ("z" * 4000)
    design_doc = {
        "systems/auth": {
            "status": "mature",
            "description": unique,
            "modules": ["backend/app/auth.py"],
        },
        "capabilities/login": {
            "status": "working",
            "notes": unique,
            "modules": ["backend/app/auth.py"],
        },
    }
    mocker.patch(
        "app.strategic_spawning.ground_design_doc",
        new=AsyncMock(side_effect=lambda doc, **kw: doc),
    )
    project = _make_project(vision={"purpose": "p"}, design_doc=design_doc)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]
    assert payload["type"] == "design_doc_update"

    dd = payload["design_doc"]
    assert set(dd.keys()) == {"sections"}
    by_key = {e["key"]: e for e in dd["sections"]}
    assert set(by_key) == {"systems/auth", "capabilities/login"}
    assert by_key["capabilities/login"]["status"] == "working"
    assert unique not in json.dumps(dd)


# ── Missing-foundation demand in strategic payloads ──────────────────────────


def _blocked_dependent(project_id, need, *, direction_id="d-strat-dep"):
    """A BLOCKED dependent waiting on a single capability/foundation need."""
    from app.time_utils import _utcnow as _ut

    return Task(
        project_id=project_id,
        title="create: needs a missing foundation",
        payloads=[{"type": "create", "prompt": "build feature", "base_branch": "main"}],
        status=TaskStatus.BLOCKED,
        blocked_at=_ut() - timedelta(hours=4),
        context={
            "direction_id": direction_id,
            "blocker": {
                "source": "agent",
                "waiting_on": need.get("ref"),
                "reason": "BLOCKER: waiting on foundation",
                "need": need,
                "needs": [need],
            },
        },
        created_by="ORCHESTRATOR",
    )


@pytest.mark.asyncio
async def test_strategic_review_includes_missing_capability_demand(db_session):
    """The strategic_review payload surfaces aggregated missing-foundation demand
    (matching the collector) plus an advisory hint, and validates cleanly."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    need = {"kind": "capability", "ref": "auth-subsystem", "label": "Auth subsystem"}
    db_session.add(_blocked_dependent(project.id, need))
    await db_session.commit()

    expected = await collect_missing_capability_demand(db_session, project.id)
    assert expected["total"] == 1

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = [
        t
        for t in result.scalars().all()
        if t.payloads[0].get("type") == "strategic_review"
    ][0].payloads[0]

    assert payload["missing_capability_demand"] == expected
    assert "Missing-foundation demand" in (payload.get("prompt_suffix") or "")
    validate_internal_payload(dict(payload), "test_sr_demand")


@pytest.mark.asyncio
async def test_strategic_review_omits_demand_when_none(db_session):
    """The strategic_review payload omits the demand field when no demand exists."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    assert (await collect_missing_capability_demand(db_session, project.id))[
        "total"
    ] == 0

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = [
        t
        for t in result.scalars().all()
        if t.payloads[0].get("type") == "strategic_review"
    ][0].payloads[0]

    assert "missing_capability_demand" not in payload
    validate_internal_payload(dict(payload), "test_sr_no_demand")


@pytest.mark.asyncio
async def test_design_doc_update_includes_missing_capability_demand(db_session):
    """The design_doc_update payload surfaces aggregated missing-foundation demand
    (matching the collector) plus an advisory hint, and validates cleanly."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    need = {"kind": "capability", "ref": "search-index", "label": "Search index"}
    db_session.add(_blocked_dependent(project.id, need))
    await db_session.commit()

    expected = await collect_missing_capability_demand(db_session, project.id)
    assert expected["total"] == 1

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = [
        t
        for t in result.scalars().all()
        if t.payloads[0].get("type") == "design_doc_update"
    ][0].payloads[0]

    assert payload["missing_capability_demand"] == expected
    assert "Missing-foundation demand" in (payload.get("prompt_suffix") or "")
    validate_internal_payload(dict(payload), "test_ddu_demand")


@pytest.mark.asyncio
async def test_design_doc_update_omits_demand_when_none(db_session):
    """The design_doc_update payload omits the demand field when no demand exists."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert (await collect_missing_capability_demand(db_session, project.id))[
        "total"
    ] == 0

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = [
        t
        for t in result.scalars().all()
        if t.payloads[0].get("type") == "design_doc_update"
    ][0].payloads[0]

    assert "missing_capability_demand" not in payload
    validate_internal_payload(dict(payload), "test_ddu_no_demand")


# ---------------------------------------------------------------------------
# _direction_activity_score: verified-merge selection input
# ---------------------------------------------------------------------------


def test_direction_activity_score_prefers_verified_merges():
    """A degraded merge (verified_prs_merged=0) must not boost the activity score."""
    degraded = {"verified_prs_merged": 0, "prs_merged": 5}
    verified = {"verified_prs_merged": 5, "prs_merged": 5}
    # The degraded direction shipped nothing verified → no merge boost (score 0),
    # while the verified direction earns the 3x-per-merge boost.
    assert _direction_activity_score(degraded) == 0
    assert _direction_activity_score(verified) == 15
    assert _direction_activity_score(verified) > _direction_activity_score(degraded)


def test_direction_activity_score_legacy_falls_back_to_prs_merged():
    """Stat dict predating verified_prs_merged uses raw prs_merged (back-compat)."""
    legacy = {"prs_merged": 4}
    # 4 merges * 3 = 12, no other signals.
    assert _direction_activity_score(legacy) == 12


def test_direction_activity_score_prefers_reachable_prs_merged():
    """Reachable merges (landed on integration) take precedence over verified merges."""
    # When reachable_prs_merged is present, use it instead of verified_prs_merged
    reachable_stat = {"reachable_prs_merged": 3, "verified_prs_merged": 5, "prs_merged": 5}
    # 3 reachable merges * 3 = 9, not 5 * 3 = 15
    assert _direction_activity_score(reachable_stat) == 9

    # When reachable_prs_merged is 0 (key present), use 0 — do NOT fall back to verified
    zero_reachable = {"reachable_prs_merged": 0, "verified_prs_merged": 4}
    # Key present with value 0 means reachability was recorded and nothing landed: score 0
    assert _direction_activity_score(zero_reachable) == 0

    # When both reachable and verified are present and non-zero, reachable wins
    both_present = {"reachable_prs_merged": 2, "verified_prs_merged": 5}
    # 2 reachable * 3 = 6, not 5 * 3 = 15
    assert _direction_activity_score(both_present) == 6

    # Missing reachable_prs_merged falls back through to verified_prs_merged
    missing_reachable = {"verified_prs_merged": 3}
    # 3 verified * 3 = 9
    assert _direction_activity_score(missing_reachable) == 9

    # All three missing falls back to prs_merged (legacy back-compat)
    legacy_only = {"prs_merged": 2}
    # 2 merges * 3 = 6
    assert _direction_activity_score(legacy_only) == 6


# ---------------------------------------------------------------------------
# Direction-health/prioritization consumes the proven-vs-delivered signal.
#
# These exercise the strategic-DECISION consumption layer: the same scorers that
# feed prioritization (autonomous_research._direction_score), health/quality, and
# difficulty must distinguish a direction shipping PROVEN merges from one shipping
# an equal pile of UNDECLARED merges — additively and fail-safe over legacy data.
# ---------------------------------------------------------------------------


def _accept_cycle(*, proven, declared, merged=2, **extra):
    """A research_history cycle that recorded the acceptance contract.

    ``merged`` verified write-task merges, of which ``proven`` are machine-proven
    and ``declared`` declared forward evidence (proven ⊆ declared ⊆ merged).
    """
    entry = {
        "tasks_created": merged,
        "tasks_completed": merged,
        "tasks_failed": 0,
        "tasks_failed_infra": 0,
        "prs_merged": merged,
        "verified_prs_merged": merged,
        "acceptance_merged_write_total": merged,
        "acceptance_proven_merged": proven,
        "acceptance_declared_merged": declared,
        "acceptance_declaration_coverage": round(declared / merged, 3)
        if merged
        else None,
    }
    entry.update(extra)
    return entry


def test_direction_health_reflects_declaration_coverage():
    """Proven-merge directions out-score equal-volume undeclared ones across the
    automated health/prioritization scorers; legacy data is untouched."""
    from app.direction_metrics import (
        _compute_difficulty_index,
        _compute_direction_quality_score,
        _compute_productivity_score,
        proof_weighted_merged,
    )
    from app.direction_outcomes import verified_merged_count

    # Equal MERGE VOLUME (3 cycles, 2 verified merges each); only proof differs.
    proven_dir = {
        "research_history": [_accept_cycle(proven=2, declared=2) for _ in range(3)]
    }
    undeclared_dir = {
        "research_history": [_accept_cycle(proven=0, declared=0) for _ in range(3)]
    }

    # 1. Quality/health: the undeclared pile no longer reads as fully healthy.
    q_proven = _compute_direction_quality_score(proven_dir)
    q_undeclared = _compute_direction_quality_score(undeclared_dir)
    assert q_proven > q_undeclared

    # 2. Productivity (prioritization input): proven work is more productive.
    assert _compute_productivity_score(proven_dir) > _compute_productivity_score(
        undeclared_dir
    )

    # 3. Difficulty (prioritization input): undeclared work reads as HARDER
    #    (more tasks per proven merge) so it is deprioritized, not credited.
    assert _compute_difficulty_index(undeclared_dir) > _compute_difficulty_index(
        proven_dir
    )

    # 4. Proof-weighting only ever DISCOUNTS — never inflates above verified.
    for entry in undeclared_dir["research_history"] + proven_dir["research_history"]:
        assert proof_weighted_merged(entry) <= verified_merged_count(entry)

    # 5. Fail-safe: the proof discount is GATED on acceptance data being present.
    #    Stripping the acceptance_* fields yields the legacy twin of the undeclared
    #    direction — no proof component, no discount — so it scores strictly HIGHER
    #    than the undeclared one and its merges keep full (verified) credit.
    legacy_twin = {
        "research_history": [
            {k: v for k, v in e.items() if not k.startswith("acceptance_")}
            for e in undeclared_dir["research_history"]
        ]
    }
    for entry in legacy_twin["research_history"]:
        assert proof_weighted_merged(entry) == float(verified_merged_count(entry))
    assert _compute_direction_quality_score(legacy_twin) > q_undeclared
    assert _compute_productivity_score(legacy_twin) > _compute_productivity_score(
        undeclared_dir
    )


# ── _summarize_memo_citations tests ──────────────────────────────────────────


def test_summarize_memo_citations_legacy_shape_unchanged():
    """Legacy {task_id, summary_excerpt} entries pass through unchanged."""
    from app.strategic_spawning import _summarize_memo_citations

    memos = [
        {"task_id": "t1", "summary_excerpt": "First finding."},
        {"task_id": "t2", "summary_excerpt": "Second finding."},
    ]
    result = _summarize_memo_citations(memos)
    assert len(result) == 2
    assert result[0]["task_id"] == "t1"
    assert result[0]["summary_excerpt"] == "First finding."
    assert result[1]["task_id"] == "t2"
    assert result[1]["summary_excerpt"] == "Second finding."


def test_summarize_memo_citations_strips_heavy_fields():
    """Heavy fields are stripped, compact retirement excerpts survive."""
    from app.strategic_spawning import _summarize_memo_citations

    memos = [
        {
            "task_id": "t-ret",
            "summary_excerpt": "Retirement memo.",
            "reasoning_kind": "direction_retirement",
            "direction_id": "dir-xyz",
            "tags": ["retirement"],
            "citations": ["task-cit-1", "task-cit-2"],
            "final_metrics": {"merged_prs": 10},
            "success_evidence": "All tests passed.",
            "retirement_condition": "KPIs met.",
            "rationale": "Long rationale text.",
            "reasoning": "Full multi-paragraph reasoning that is verbose and should be dropped.",
            "citation_refs": "task-cit-1, task-cit-2",
            "final_metrics_excerpt": "final_prs_merged=10, final_cycles_active=4",
            "rationale_excerpt": "Long rationale text.",
            "reasoning_excerpt": "Direction: retired because KPIs were met.",
        }
    ]
    result = _summarize_memo_citations(memos)
    assert len(result) == 1
    entry = result[0]
    assert entry["task_id"] == "t-ret"
    assert entry["reasoning_kind"] == "direction_retirement"
    assert entry["direction_id"] == "dir-xyz"
    assert entry["tags"] == ["retirement"]
    assert entry["success_evidence"] == "All tests passed."
    assert entry["retirement_condition"] == "KPIs met."
    assert entry["citation_refs"] == "task-cit-1, task-cit-2"
    assert (
        entry["final_metrics_excerpt"] == "final_prs_merged=10, final_cycles_active=4"
    )
    assert entry["rationale_excerpt"] == "Long rationale text."
    assert entry["reasoning_excerpt"] == "Direction: retired because KPIs were met."
    # Heavy/verbose fields stripped
    assert "citations" not in entry, "_summarize_memo_citations must strip citations"
    assert "final_metrics" not in entry, (
        "_summarize_memo_citations must strip final_metrics"
    )
    assert "rationale" not in entry, "_summarize_memo_citations must strip rationale"
    assert "reasoning" not in entry, (
        "_summarize_memo_citations must strip full reasoning"
    )


def test_summarize_memo_citations_strips_full_reasoning_keeps_excerpt():
    """Full reasoning is stripped in the budget-degraded path; reasoning_excerpt survives."""
    from app.strategic_spawning import _summarize_memo_citations

    long_reasoning = "Paragraph one. " * 100  # simulate a verbose reasoning blob
    memos = [
        {
            "task_id": "t-full",
            "summary_excerpt": "A retirement memo with full reasoning.",
            "reasoning": long_reasoning,
            "reasoning_excerpt": "Compact excerpt that fits in budget.",
            "reasoning_kind": "direction_retirement",
        }
    ]
    result = _summarize_memo_citations(memos)
    assert len(result) == 1
    entry = result[0]
    assert "reasoning" not in entry, (
        "full reasoning must be stripped to honour budget bounds"
    )
    assert entry["reasoning_excerpt"] == "Compact excerpt that fits in budget."
    assert entry["task_id"] == "t-full"


def test_summarize_memo_citations_caps_entry_count():
    """Entry count is capped to _MEMO_CITATIONS_KEEP (newest-first)."""
    from app.strategic_spawning import _summarize_memo_citations, _MEMO_CITATIONS_KEEP

    memos = [{"task_id": f"t{i}", "summary_excerpt": f"memo {i}"} for i in range(20)]
    result = _summarize_memo_citations(memos)
    assert len(result) == _MEMO_CITATIONS_KEEP
    assert result[0]["task_id"] == "t0"


def test_summarize_memo_citations_caps_excerpt_length():
    """summary_excerpt is capped to _MEMO_EXCERPT_MAX_CHARS."""
    from app.strategic_spawning import (
        _summarize_memo_citations,
        _MEMO_EXCERPT_MAX_CHARS,
    )

    long_text = "x" * (_MEMO_EXCERPT_MAX_CHARS + 100)
    memos = [{"task_id": "t1", "summary_excerpt": long_text}]
    result = _summarize_memo_citations(memos)
    assert len(result[0]["summary_excerpt"]) == _MEMO_EXCERPT_MAX_CHARS


def test_summarize_memo_citations_non_list_passthrough():
    """Non-list input is returned as-is (guard for legacy callers)."""
    from app.strategic_spawning import _summarize_memo_citations

    assert _summarize_memo_citations(None) is None  # type: ignore[arg-type]
    assert _summarize_memo_citations("invalid") == "invalid"  # type: ignore[arg-type]


def test_summarize_memo_citations_non_dict_entries_preserved():
    """Non-dict entries in the list are preserved unchanged."""
    from app.strategic_spawning import _summarize_memo_citations

    memos = [{"task_id": "t1", "summary_excerpt": "ok"}, "not-a-dict"]
    result = _summarize_memo_citations(memos)
    assert result[1] == "not-a-dict"


# ---------------------------------------------------------------------------
# _summarize_efficiency_snapshot — format_compliance and agent_timeout_rate
# ---------------------------------------------------------------------------


def test_summarize_efficiency_snapshot_includes_format_compliance():
    """format_compliance is forwarded with current/baseline/drift_pct only (no history)."""
    from app.strategic_spawning import _summarize_efficiency_snapshot

    snapshot = {
        "axes": {
            "cost_per_merged_pr": {
                "current": 1.5,
                "baseline": 2.0,
                "drift_pct": -25.0,
                "history": [],
            }
        },
        "baseline_captured_at": "2026-04-05T00:00:00",
        "last_snapshot_at": "2026-05-01T00:00:00",
        "format_compliance": {
            "format_compliance_rate": {
                "current": 0.92,
                "baseline": 0.88,
                "drift_pct": 4.5,
                "history": [{"captured_at": "2026-04-01", "value": 0.85}],
            },
            "self_repair_success_rate": {
                "current": 0.75,
                "baseline": 0.70,
                "drift_pct": 7.1,
                "history": [],
            },
        },
    }
    result = _summarize_efficiency_snapshot(snapshot)
    assert "format_compliance" in result
    fc = result["format_compliance"]
    assert "format_compliance_rate" in fc
    assert fc["format_compliance_rate"] == {
        "current": 0.92,
        "baseline": 0.88,
        "drift_pct": 4.5,
    }
    assert "history" not in fc["format_compliance_rate"]
    assert "self_repair_success_rate" in fc
    assert fc["self_repair_success_rate"]["current"] == 0.75


def test_summarize_efficiency_snapshot_includes_agent_timeout_rate():
    """agent_timeout_rate overall + dominant fields are forwarded (no history)."""
    from app.strategic_spawning import _summarize_efficiency_snapshot

    snapshot = {
        "axes": {
            "cost_per_merged_pr": {
                "current": 1.5,
                "baseline": 2.0,
                "drift_pct": -25.0,
                "history": [],
            }
        },
        "agent_timeout_rate": {
            "overall": {
                "current": 0.08,
                "baseline": 0.06,
                "drift_pct": 33.3,
                "history": [{"captured_at": "2026-04-01", "value": 0.07}],
            },
            "by_subtype": {"context_limit": {"current": 0.05}},
            "dominant_subtype": "context_limit",
            "dominant_share": "62%",
            "denominator": 100,
        },
    }
    result = _summarize_efficiency_snapshot(snapshot)
    assert "agent_timeout_rate" in result
    atr = result["agent_timeout_rate"]
    assert atr["overall"] == {"current": 0.08, "baseline": 0.06, "drift_pct": 33.3}
    assert "history" not in atr["overall"]
    assert atr["dominant_subtype"] == "context_limit"
    assert atr["dominant_share"] == "62%"
    assert "by_subtype" not in atr
    assert "denominator" not in atr


def test_summarize_efficiency_snapshot_forwards_axis_seed_provenance():
    """Axis provenance is preserved when it differs from baseline_captured_at."""
    from app.strategic_spawning import _summarize_efficiency_snapshot

    snapshot = {
        "baseline_captured_at": "2026-04-05T00:00:00",
        "axes": {
            "llm_cost_per_merged_pr": {
                "current": 1.2,
                "baseline": 0.9,
                "drift_pct": 33.3,
                "baseline_seeded_at": "2026-04-12T00:00:00",
                "history": [],
            }
        },
    }
    result = _summarize_efficiency_snapshot(snapshot)
    axis = result["axes"]["llm_cost_per_merged_pr"]
    assert axis["seeded_at"] == "2026-04-12T00:00:00"
    assert "baseline_seeded_at" not in axis


def test_summarize_efficiency_snapshot_omits_blocks_when_absent():
    """format_compliance and agent_timeout_rate keys are absent when raw snapshot lacks them."""
    from app.strategic_spawning import _summarize_efficiency_snapshot

    snapshot = {
        "axes": {
            "cost_per_merged_pr": {
                "current": 1.5,
                "baseline": 2.0,
                "drift_pct": -25.0,
                "history": [],
            }
        },
    }
    result = _summarize_efficiency_snapshot(snapshot)
    assert "format_compliance" not in result
    assert "agent_timeout_rate" not in result


def test_summarize_efficiency_snapshot_empty_returns_empty():
    """Empty/None snapshot returns empty dict."""
    from app.strategic_spawning import _summarize_efficiency_snapshot

    assert _summarize_efficiency_snapshot({}) == {}
    assert _summarize_efficiency_snapshot(None) == {}  # type: ignore[arg-type]


def test_summarize_efficiency_snapshot_includes_dominant_failure_class():
    """strategist efficiency-snapshot summary forwards the dominant non-MERGED
    failure-class LLM cost (highest-cost failure mode excluding merged tasks)."""
    from app.strategic_spawning import _summarize_efficiency_snapshot

    snapshot = {
        "axes": {
            "cost_per_merged_pr": {
                "current": 1.5,
                "baseline": 2.0,
                "drift_pct": -25.0,
                "history": [],
            }
        },
        "baseline_captured_at": "2026-04-05T00:00:00",
        "last_snapshot_at": "2026-05-01T00:00:00",
        "llm_cost_attribution": {
            "total_llm_usd": 100.0,
            "rework_share": 0.15,
            "by_model": [
                {"provider": "anthropic", "model": "claude-opus", "cost_usd": 50.0}
            ],
            "by_task_type": [{"task_type": "task_edit", "cost_usd": 75.0}],
            # by_failure_class sorted by cost descending: context_limit (80) > MERGED (15) > import_error (5)
            "by_failure_class": [
                {"failure_class": "context_limit", "task_count": 2, "total_cost_usd": 80.0},
                {"failure_class": "MERGED", "task_count": 3, "total_cost_usd": 15.0},
                {"failure_class": "import_error", "task_count": 1, "total_cost_usd": 5.0},
            ],
        },
    }
    result = _summarize_efficiency_snapshot(snapshot)
    assert "llm_cost_attribution" in result
    attr = result["llm_cost_attribution"]
    assert attr["dominant_failure_class"] == {
        "failure_class": "context_limit",
        "task_count": 2,
        "total_cost_usd": 80.0,
    }


def test_summarize_efficiency_snapshot_forwards_gate_health():
    """panel_review_effectiveness gate_health verdict is forwarded by _summarize_efficiency_snapshot.

    The gate-health block is the canonical way for the strategic agent to
    autonomously confirm the adversarial-second-opinion gate's fail-open rate
    after a version promotion, without an explicit manual fetch.
    """
    from app.strategic_spawning import _summarize_efficiency_snapshot

    gate_health = {
        "state": "HEALTHY",
        "fail_open_rate": 0.02,
        "total_invocations": 50,
        "min_sample": 30,
    }
    snapshot = {
        "axes": {
            "cost_per_merged_pr": {
                "current": 1.5,
                "baseline": 2.0,
                "drift_pct": -25.0,
                "history": [],
            }
        },
        "baseline_captured_at": "2026-04-05T00:00:00",
        "last_snapshot_at": "2026-05-01T00:00:00",
        "panel_review_effectiveness": {
            "gate_health": gate_health,
            "fail_open_rate": 0.02,
            "total_challenged": 49,
            "challenged_degraded": 1,
            "effective_coverage": 0.85,
            # raw history arrays stripped by summarize
            "history": [{"captured_at": "2026-04-01", "value": 0.01}],
        },
    }
    result = _summarize_efficiency_snapshot(snapshot)
    assert "panel_review_effectiveness" in result, (
        "panel_review_effectiveness missing from summarized snapshot"
    )
    peff = result["panel_review_effectiveness"]
    assert peff["gate_health"] == gate_health, (
        "gate_health verdict not forwarded faithfully"
    )
    assert peff["fail_open_rate"] == 0.02
    assert peff["total_challenged"] == 49
    assert peff["challenged_degraded"] == 1
    assert peff["effective_coverage"] == 0.85
    assert "history" not in peff, "history array must be stripped from summarized payload"


def test_summarize_efficiency_snapshot_forwards_post_deploy_gate_health():
    """panel_review_effectiveness_post_deploy is forwarded by _summarize_efficiency_snapshot.

    The deploy-scoped gate-health block lets the strategist see post-deploy
    adversarial coverage that resets every deploy, without having to fetch it
    separately.
    """
    from app.strategic_spawning import _summarize_efficiency_snapshot

    gate_health_pd = {
        "state": "HEALTHY",
        "fail_open_rate": 0.01,
        "total_invocations": 35,
        "min_sample": 30,
    }
    snapshot = {
        "axes": {
            "cost_per_merged_pr": {
                "current": 1.5,
                "baseline": 2.0,
                "drift_pct": -25.0,
                "history": [],
            }
        },
        "baseline_captured_at": "2026-04-05T00:00:00",
        "last_snapshot_at": "2026-05-01T00:00:00",
        "panel_review_effectiveness_post_deploy": {
            "gate_health": gate_health_pd,
            "fail_open_rate": 0.01,
            "total_challenged": 34,
            "challenged_degraded": 0,
            "effective_coverage": 0.90,
            # history stripped by summarize
            "history": [{"captured_at": "2026-05-01", "value": 0.01}],
        },
    }
    result = _summarize_efficiency_snapshot(snapshot)
    assert "panel_review_effectiveness_post_deploy" in result, (
        "panel_review_effectiveness_post_deploy missing from summarized snapshot"
    )
    peff_pd = result["panel_review_effectiveness_post_deploy"]
    assert peff_pd["gate_health"] == gate_health_pd, (
        "gate_health verdict not forwarded faithfully"
    )
    assert peff_pd["fail_open_rate"] == 0.01
    assert peff_pd["total_challenged"] == 34
    assert peff_pd["challenged_degraded"] == 0
    assert peff_pd["effective_coverage"] == 0.90
    assert "history" not in peff_pd, "history array must be stripped from summarized payload"


def test_summarize_efficiency_snapshot_forwards_format_miss_attribution():
    """strategist efficiency payload includes per-handler format-miss attribution.

    dominant_schema / dominant_reason / dominant_share are extracted from the
    format_miss_by_schema_and_reason cross-product and forwarded compactly so a
    format-compliance regression can be attributed to the producing handler
    without phantom low-sample noise driving the global delta.
    """
    from app.strategic_spawning import _summarize_efficiency_snapshot

    snapshot = {
        "axes": {},
        "baseline_captured_at": "2026-04-05T00:00:00",
        "last_snapshot_at": "2026-05-01T00:00:00",
        "format_compliance": {
            "format_compliance_rate": {
                "current": 0.88,
                "baseline": 0.92,
                "drift_pct": -4.3,
                "history": [],
            },
            "self_repair_success_rate": {
                "current": 0.70,
                "baseline": 0.75,
                "drift_pct": -6.7,
                "history": [],
            },
            "repair_attempts": 20,
            # strategic handler has 10 misses (8 schema_missing + 2 extra_key),
            # research handler has 3 misses (all schema_missing).
            "format_miss_by_schema_and_reason": {
                "strategic": {"schema_missing": 8, "extra_key": 2},
                "research": {"schema_missing": 3},
            },
        },
    }
    result = _summarize_efficiency_snapshot(snapshot)
    assert "format_compliance" in result
    fc = result["format_compliance"]

    # dominant_schema is "strategic" (10 misses vs 3)
    assert fc["dominant_schema"] == "strategic"
    # dominant_reason within "strategic" is "schema_missing" (8 vs 2)
    assert fc["dominant_reason"] == "schema_missing"
    # dominant_share = 10 / 13 ≈ 0.769
    assert fc["dominant_share"] == round(10 / 13, 3)

    # rates still forwarded
    assert fc["format_compliance_rate"]["current"] == 0.88
    assert fc["repair_attempts"] == 20


def test_summarize_efficiency_snapshot_format_miss_absent_no_dominant():
    """When format_miss_by_schema_and_reason is absent, no dominant_* keys are emitted."""
    from app.strategic_spawning import _summarize_efficiency_snapshot

    snapshot = {
        "axes": {},
        "format_compliance": {
            "format_compliance_rate": {
                "current": 0.90,
                "baseline": 0.90,
                "drift_pct": 0.0,
                "history": [],
            },
            "repair_attempts": 5,
            # no format_miss_by_schema_and_reason key
        },
    }
    result = _summarize_efficiency_snapshot(snapshot)
    fc = result["format_compliance"]
    assert "dominant_schema" not in fc
    assert "dominant_reason" not in fc
    assert "dominant_share" not in fc


# ── direction-linked knowledge in strategist payload ──────────────────────────


@pytest.mark.asyncio
async def test_strategist_context_surfaces_direction_linked_knowledge(db_session):
    """strategic_review outcome_evaluation surfaces direction_knowledge for
    directions that have linked knowledge documents."""
    direction_id = "d-linked-knowledge"
    project = _make_project(
        directions=[
            {
                "id": direction_id,
                "status": "active",
                "text": "Direction with linked knowledge",
                "priority": "high",
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)
    await _seed_knowledge_doc(
        db_session,
        project,
        title="Auth research findings",
        direction_id=direction_id,
    )

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.payloads[0]["type"] == "strategic_review"
    )

    outcome_eval = payload["outcome_evaluation"]
    direction_outcomes = outcome_eval["directions"]
    matched = next((d for d in direction_outcomes if d["id"] == direction_id), None)
    assert matched is not None, f"direction {direction_id!r} not in outcome_evaluation"
    assert "direction_knowledge" in matched, (
        "direction_knowledge missing from direction outcome — linked knowledge not surfaced"
    )
    assert matched["direction_knowledge"][0]["title"] == "Auth research findings"


# ── spawn_fix op: one-time direction-free implementation dispatch ──────────────
#
# spawn_fix lets the strategist dispatch a ONE-TIME implementation task (bug fix,
# CI remediation, isolated cleanup) with NO strategic direction. These tests
# exercise the production handler in routers/admin_agent_ops.py end-to-end over
# the real /api/agent/strategic endpoint, mirroring the spawn_task harness in
# test_pipeline.py but asserting the direction-free, one_time_fix provenance.


async def _make_strategic_review_task(db_session):
    """Create a project + IN_PROGRESS strategic_review task; return
    (project, review_id) for spawn_fix endpoint tests."""
    project = Project(name="P", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    review = Task(
        project_id=project.id,
        user_id="u1",
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"command": "review"}],
    )
    db_session.add(review)
    await db_session.commit()
    await db_session.refresh(review)
    return project, str(review.id)


async def _post_spawn_fix(
    db_session, task_id, *, title, prompt, spawn_type="create", expect_status=200
):
    """POST a strategic spawn_fix op against the test app over the real endpoint.

    Returns the parsed JSON body (success body on 200, FastAPI error envelope on
    an expected error status)."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test-token"},
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "spawn_fix",
                    "task_id": task_id,
                    "data": {"type": spawn_type, "title": title, "prompt": prompt},
                },
            )
        assert resp.status_code == expect_status, resp.text
        return resp.json()
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


def test_one_time_spawnable_registry_set_is_implementation_types():
    """STRATEGIST_ONE_TIME_SPAWNABLE_TYPES is exactly the PR-producing
    implementation types, and is disjoint from the investigation/knowledge types
    spawn_task accepts (no overlap between the two dispatch lanes)."""
    from app.task_capabilities import (
        STRATEGIST_ONE_TIME_SPAWNABLE_TYPES,
        STRATEGIST_SPAWNABLE_TASK_TYPES,
    )

    assert {"create", "refactor", "qa"} == STRATEGIST_ONE_TIME_SPAWNABLE_TYPES
    # The two lanes are mutually exclusive: a one-time fix is direction-free and
    # PR-producing; a spawn_task spawn is a read-only investigation.
    assert STRATEGIST_ONE_TIME_SPAWNABLE_TYPES.isdisjoint(
        STRATEGIST_SPAWNABLE_TASK_TYPES
    )


@pytest.mark.asyncio
async def test_spawn_fix_creates_task_without_direction_id(db_session):
    """spawn_fix with a valid implementation type (create) creates a QUEUED task
    carrying created_by=STRATEGIC_AGENT and the one-time-fix provenance, and with
    NO direction_id anywhere — neither in context nor in the payload — so it can
    never surface in any direction portfolio logic."""
    _project, review_id = await _make_strategic_review_task(db_session)

    body = await _post_spawn_fix(
        db_session,
        review_id,
        title="Fix red CI test on automain",
        prompt="The launch test clamps the spend cap; fix the assertion.",
        spawn_type="create",
    )
    assert body["status"] == "ok"
    assert body["op"] == "spawn_fix"
    assert body["type"] == "create"
    assert body["deduplicated"] is False
    assert body["priority"] == 100

    spawned = (
        await db_session.execute(select(Task).where(Task.id == body["task_id"]))
    ).scalar_one()
    assert spawned.status == TaskStatus.QUEUED
    assert spawned.created_by == "STRATEGIC_AGENT"
    assert spawned.first_payload.get("type") == "create"

    # Direction-free provenance: spawned_by marks the one-time-fix lane and the
    # one_time_fix flag is set; NO direction_id key exists in context.
    ctx = spawned.context or {}
    assert ctx.get("spawned_by") == "strategic_direct_fix"
    assert ctx.get("one_time_fix") is True
    assert ctx.get("parent_strategic_task_id") == review_id
    assert "direction_id" not in ctx

    # The payload itself carries no direction_id key (direction-free by construction).
    assert "direction_id" not in spawned.first_payload


@pytest.mark.asyncio
async def test_spawn_fix_rejects_direction_only_investigation_type(db_session):
    """A research task is a strategist_spawnable investigation type, NOT a
    one-time implementation type, so spawn_fix must reject it with a 400 listing
    the registry-derived allowed types."""
    from app.task_capabilities import STRATEGIST_ONE_TIME_SPAWNABLE_TYPES

    _project, review_id = await _make_strategic_review_task(db_session)

    body = await _post_spawn_fix(
        db_session,
        review_id,
        title="Investigate something",
        prompt="research the codebase",
        spawn_type="research",
        expect_status=400,
    )
    detail = body["error"]["message"]
    assert "must be one of" in detail
    assert "research" in detail  # echoes the rejected type
    assert str(sorted(STRATEGIST_ONE_TIME_SPAWNABLE_TYPES)) in detail


@pytest.mark.asyncio
async def test_spawn_fix_fix_type_without_pr_context_fails_cleanly(db_session):
    """``fix`` requires pr_number + branch a prompt-only one-time spawn cannot
    supply, so it is not in STRATEGIST_ONE_TIME_SPAWNABLE_TYPES. The handler must
    reject it at the allowlist check with a clean 400 (not an opaque 500), and
    create NO task."""
    from sqlalchemy import func as _f

    _project, review_id = await _make_strategic_review_task(db_session)

    body = await _post_spawn_fix(
        db_session,
        review_id,
        title="Fix the thing",
        prompt="patch the bug",
        spawn_type="fix",
        expect_status=400,
    )
    detail = body["error"]["message"]
    assert "must be one of" in detail

    # No task was created by the failed validation.
    count = (
        await db_session.execute(
            select(_f.count())
            .select_from(Task)
            .where(Task.created_by == "STRATEGIC_AGENT")
        )
    ).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_spawn_fix_idempotent_on_retry_duplicate(db_session):
    """A retried spawn_fix (same source review, type and title within 30 min)
    returns the existing task instead of creating a duplicate, keyed on the
    strategic_direct_fix provenance."""
    from sqlalchemy import func as _f

    _project, review_id = await _make_strategic_review_task(db_session)

    title = "Fix flaky compute spawn test"
    prompt = "Reset the spend-gate global config in the fixture."

    first = await _post_spawn_fix(
        db_session, review_id, title=title, prompt=prompt, spawn_type="create"
    )
    assert first["status"] == "ok"
    assert first["deduplicated"] is False
    first_id = first["task_id"]

    second = await _post_spawn_fix(
        db_session, review_id, title=title, prompt=prompt, spawn_type="create"
    )
    assert second["status"] == "ok"
    assert second["deduplicated"] is True
    assert second["task_id"] == first_id

    # Exactly one one-time-fix child exists for this review.
    count = (
        await db_session.execute(
            select(_f.count())
            .select_from(Task)
            .where(Task.created_by == "STRATEGIC_AGENT", Task.title == title)
        )
    ).scalar()
    assert count == 1

    # The parent's spawn counter incremented exactly once (dedup does not double-count).
    review = (
        await db_session.execute(select(Task).where(Task.id == review_id))
    ).scalar_one()
    assert (review.context or {}).get("_strategic_tasks_spawned") == 1


@pytest.mark.asyncio
async def test_spawn_fix_per_review_budget_enforced(db_session, mocker):
    """One strategic review may spawn at most the per-review cap of one-time
    fixes; the next is refused with 429 status='skipped' and an observable budget
    flag stamped on the review."""
    from sqlalchemy import func as _f

    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"strategist_spawn_per_review_cap": 2, "strategist_spawn_window_cap": 50},
    )

    _project, review_id = await _make_strategic_review_task(db_session)

    # Distinct titles (overlap below threshold) so semantic dedup never fires.
    for n in ("alpha", "beta"):
        body = await _post_spawn_fix(
            db_session, review_id, title=f"Fix {n}", prompt=f"do {n}"
        )
        assert body["status"] == "ok"
        assert body["deduplicated"] is False

    refused = await _post_spawn_fix(
        db_session,
        review_id,
        title="Fix gamma",
        prompt="do gamma",
        expect_status=429,
    )
    assert refused["status"] == "skipped"
    assert refused["op"] == "spawn_fix"
    assert refused["scope"] == "per_review"

    # Only the two budgeted tasks exist; the third was not created.
    count = (
        await db_session.execute(
            select(_f.count())
            .select_from(Task)
            .where(Task.created_by == "STRATEGIC_AGENT")
        )
    ).scalar()
    assert count == 2

    # The refusal is observable: the review carries the exhaustion flag.
    review = (
        await db_session.execute(select(Task).where(Task.id == review_id))
    ).scalar_one()
    flag = (review.context or {}).get("_strategic_spawn_budget_exhausted")
    assert flag is not None
    assert flag["scope"] == "per_review"


# ── CI compliance in strategic review payload ─────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_strategic_review_includes_ci_compliance_when_available(
    db_session, mocker
):
    """When get_or_compute_ci_compliance returns a result it is included in the
    strategic review payload under the 'ci_compliance' key."""
    from unittest.mock import AsyncMock

    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )

    fake_envelope = {
        "status": "success",
        "integration_branch": "automain",
        "head_sha": "abc123def456",
        "data": {
            "ref": "abc123def456",
            "compliant": True,
            "violations": [],
            "branch_literal_violations": [],
            "checked": 5,
            "limits_available": True,
            "tree_available": True,
        },
        "computed": False,
    }
    mocker.patch(
        "app.ci_compliance.ensure_current_ci_compliance",
        new=AsyncMock(return_value=fake_envelope),
    )

    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    assert "ci_compliance" in payload, "ci_compliance block missing from strategic payload"
    ci = payload["ci_compliance"]
    assert ci["head_sha"] == "abc123def456"
    assert ci["compliant"] is True


@pytest.mark.asyncio
async def test_spawn_strategic_review_omits_ci_compliance_when_unavailable(
    db_session, mocker
):
    """When get_or_compute_ci_compliance returns None the payload has no ci_compliance key."""
    from unittest.mock import AsyncMock

    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.ci_compliance.ensure_current_ci_compliance",
        new=AsyncMock(return_value={
            "status": "unavailable",
            "reason": "github_credentials_unavailable",
            "integration_branch": "automain",
            "head_sha": None,
            "data": None,
        }),
    )

    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    assert "ci_compliance" not in payload, (
        "ci_compliance should be absent when ensure_current_ci_compliance is unavailable"
    )


@pytest.mark.asyncio
async def test_direction_stats_reachable_prs_merged_in_stat_entry(db_session):
    """reachable_prs_merged is surfaced in directions_summary stat_entry and survives compaction.

    A direction with at least one history entry that carries reachable_prs_merged
    should expose the lifetime reachable total. A direction with only legacy history
    entries (no reachable_prs_merged field) should omit the key rather than fabricate 0.
    """
    project = _make_project(
        directions=[
            {
                "id": "d-reachable01",
                "status": "active",
                "text": "Direction with reachable credit",
                "priority": "medium",
                # 3 merged, 2 ancestry-confirmed reachable.
                "research_history": [
                    {
                        "prs_merged": 3,
                        "verified_prs_merged": 3,
                        "reachable_prs_merged": 2,
                    }
                ],
            },
            {
                "id": "d-legacy-noreachable",
                "status": "active",
                "text": "Direction with legacy entry (no reachable field)",
                "priority": "medium",
                # Pre-reachability: reachable_prs_merged key absent.
                "research_history": [{"prs_merged": 4, "verified_prs_merged": 4}],
            },
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]

    reachable_dir = next(
        s for s in payload["directions_summary"] if s["id"] == "d-reachable01"
    )
    assert reachable_dir["reachable_prs_merged"] == 2, (
        "reachable_prs_merged must appear in stat_entry and survive compaction"
    )
    assert reachable_dir["reachable_prs_merged"] <= reachable_dir["prs_merged"]

    legacy_dir = next(
        s for s in payload["directions_summary"] if s["id"] == "d-legacy-noreachable"
    )
    assert "reachable_prs_merged" not in legacy_dir, (
        "legacy directions without reachable_prs_merged must not fabricate a value"
    )


@pytest.mark.asyncio
async def test_direction_stats_known_zero_reachable_in_window(db_session):
    """A direction with in-window reachable_prs_merged=0 surfaces reachable_prs_merged: 0 (present, not omitted).

    Known-zero (field present, value 0) must be distinguishable from legacy-unknown
    (field absent).  The stat_entry must include reachable_prs_merged=0 so the
    agent renderer can emit the explicit alarm annotation rather than silently
    treating it as legacy-unknown.
    """
    project = _make_project(
        directions=[
            {
                "id": "d-zero-reachable",
                "status": "active",
                "text": "Direction where all merges were badge-only (zero reachable)",
                "priority": "high",
                # 2 merged, reachability measured but zero confirmed reachable.
                "research_history": [
                    {
                        "prs_merged": 2,
                        "verified_prs_merged": 2,
                        "reachable_prs_merged": 0,
                    }
                ],
            },
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]

    zero_dir = next(
        s for s in payload["directions_summary"] if s["id"] == "d-zero-reachable"
    )
    assert "reachable_prs_merged" in zero_dir, (
        "known-zero reachability (field present, value 0) must not be omitted from stat_entry"
    )
    assert zero_dir["reachable_prs_merged"] == 0, (
        "reachable_prs_merged must be 0, not inflated"
    )


@pytest.mark.asyncio
async def test_direction_stats_known_zero_reachable_survives_eviction(db_session):
    """A direction whose reachability-recorded cycles were all evicted still surfaces reachable_prs_merged: 0.

    When all in-window history is evicted and the history_aggregate only has
    total_reachable_prs_merged=0 but total_reachable_recorded_cycles>0, the
    stat_entry must still include reachable_prs_merged=0 (not omit the field).
    """
    from app.direction_metrics import _fold_evicted_into_aggregate

    # Build an aggregate that represents evicted cycles with known-zero reachability.
    evicted_cycles = [
        {
            "timestamp": "2025-01-01T00:00:00",
            "prs_merged": 3,
            "verified_prs_merged": 3,
            "reachable_prs_merged": 0,  # key present, value 0
        },
        {
            "timestamp": "2025-02-01T00:00:00",
            "prs_merged": 2,
            "verified_prs_merged": 2,
            "reachable_prs_merged": 0,
        },
    ]
    history_aggregate = _fold_evicted_into_aggregate(None, evicted_cycles)
    assert history_aggregate["total_reachable_recorded_cycles"] == 2
    assert history_aggregate["total_reachable_prs_merged"] == 0

    project = _make_project(
        directions=[
            {
                "id": "d-evicted-zero-reachable",
                "status": "active",
                "text": "Direction with evicted zero-reachable aggregate",
                "priority": "high",
                # No in-window history — all reachability info is in the aggregate.
                "research_history": [],
                "history_aggregate": history_aggregate,
            },
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]

    evicted_dir = next(
        s for s in payload["directions_summary"] if s["id"] == "d-evicted-zero-reachable"
    )
    assert "reachable_prs_merged" in evicted_dir, (
        "known-zero reachability must survive eviction via total_reachable_recorded_cycles counter"
    )
    assert evicted_dir["reachable_prs_merged"] == 0


# ---------------------------------------------------------------------------
# Summarize fallbacks for previously-exempt droppable payload sections.
# Each of the six sections below dropped WHOLESALE under byte-budget pressure
# before gaining a summarize_fn. These tests assert each summarizer (a) shrinks
# a representative oversized input, (b) never raises on malformed/empty input,
# and (c) is wired into the schema with a callable summarize entry.
# ---------------------------------------------------------------------------


def _ser_size(value) -> int:
    """Serialized byte size, matching the packer's accounting."""
    return len(json.dumps(value, default=str))


_DROPPABLE_SUMMARIZE_SECTIONS = (
    "operator_overrides",
    "knowledge_index",
    "previous_decision_outcomes",
    "playbook_summary",
    "past_retirement_critiques",
    "self_reflection_log",
)


def test_droppable_sections_have_summarize_fallback():
    """The six previously-exempt droppable sections now carry a summarize_fn.

    Acceptance contract: each section key in _STRATEGIC_REVIEW_SCHEMA maps to a
    schema entry whose ``summarize`` value is CALLABLE (not exempt/None), so under
    byte-budget pressure the packer degrades the section instead of dropping it
    wholesale. knowledge_index is also checked in _DESIGN_DOC_UPDATE_SCHEMA.
    """
    from app.strategic_spawning import (
        _STRATEGIC_REVIEW_SCHEMA,
        _DESIGN_DOC_UPDATE_SCHEMA,
    )

    for key in _DROPPABLE_SUMMARIZE_SECTIONS:
        entry = _STRATEGIC_REVIEW_SCHEMA.get(key)
        assert entry is not None, f"{key} missing from _STRATEGIC_REVIEW_SCHEMA"
        assert callable(entry.get("summarize")), (
            f"{key} must have a callable summarize fallback, not exempt-drop"
        )
        assert not entry.get("exempt"), (
            f"{key} must not be exempt once it has a summarize fallback"
        )

    # knowledge_index appears in the design-doc schema variant too.
    dd_entry = _DESIGN_DOC_UPDATE_SCHEMA.get("knowledge_index")
    assert dd_entry is not None
    assert callable(dd_entry.get("summarize")), (
        "knowledge_index must have a summarize fallback in _DESIGN_DOC_UPDATE_SCHEMA"
    )


def test_summarize_operator_overrides_shrinks_and_keeps_keys():
    from app.strategic_spawning import _summarize_operator_overrides

    overrides = [
        {
            "document": "vision",
            "field": f"field_{i}",
            "kind": "scalar_conflict",
            "agent_value": "A" * 240,
            "operator_value": "O" * 240,
            "at": f"2026-07-0{i}T00:00:00Z",
        }
        for i in range(1, 9)
    ]
    result = _summarize_operator_overrides(overrides)
    assert isinstance(result, list)
    assert _ser_size(result) < _ser_size(overrides)
    assert len(result) <= 4  # newest-few cap
    # Load-bearing routing keys survive; prose values are truncated hard.
    first = result[0]
    assert first["document"] == "vision"
    assert first["field"] == "field_1"
    assert first["kind"] == "scalar_conflict"
    assert first["at"] == "2026-07-01T00:00:00Z"
    assert len(first["agent_value"]) < 240


def test_summarize_operator_overrides_malformed_never_raises():
    from app.strategic_spawning import _summarize_operator_overrides

    assert _summarize_operator_overrides(None) is None
    assert _summarize_operator_overrides("str") == "str"
    assert _summarize_operator_overrides([]) == []
    # Non-dict list entries pass through untouched.
    assert _summarize_operator_overrides([123, "x"]) == [123, "x"]
    # Missing fields don't crash.
    assert _summarize_operator_overrides([{}]) == [
        {
            "document": None,
            "field": None,
            "kind": None,
            "agent_value": None,
            "operator_value": None,
            "at": None,
        }
    ]


def test_summarize_knowledge_index_shrinks_and_keeps_pointers():
    from app.strategic_spawning import _summarize_knowledge_index

    entries = [
        {
            "id": f"doc-{i}",
            "title": f"Doc {i}",
            "summary": "S" * 300,
            "topic": "research",
            "direction_id": f"d-{i}",
            "source_kind": "web_research",
            "sweep_id": None,
            "arm_label": None,
            "per_arm_verdicts": [{"arm": "a", "blob": "X" * 500}],
            "origin_provenance": {"deep": {"nested": "Y" * 500}},
            "knowledge_acceptance_evidence": "Z" * 300,
            "created_at": "2026-07-01T00:00:00Z",
        }
        for i in range(15)
    ]
    result = _summarize_knowledge_index(entries)
    assert isinstance(result, list)
    assert _ser_size(result) < _ser_size(entries)
    assert len(result) <= 8
    first = result[0]
    # Navigation pointers survive so ./fetch knowledge <id> still works.
    assert first["id"] == "doc-0"
    assert first["title"] == "Doc 0"
    assert first["direction_id"] == "d-0"
    # Heavy sub-fields dropped, summary truncated.
    assert "per_arm_verdicts" not in first
    assert "origin_provenance" not in first
    assert len(first["summary"]) < 300


def test_summarize_knowledge_index_malformed_never_raises():
    from app.strategic_spawning import _summarize_knowledge_index

    assert _summarize_knowledge_index(None) is None
    assert _summarize_knowledge_index("x") == "x"
    assert _summarize_knowledge_index([]) == []
    assert _summarize_knowledge_index([123]) == [123]
    # Entry with no summary key: no crash, id preserved.
    assert _summarize_knowledge_index([{"id": "d1"}]) == [{"id": "d1"}]


def test_summarize_previous_decision_outcomes_shrinks_keeps_ids():
    from app.strategic_spawning import _summarize_previous_decision_outcomes

    value = {
        "cycle_id": "c-1",
        "phase2_completed_at": "2026-07-01T00:00:00Z",
        "summary_text": "T" * 500,
        "cycle_reasoning": "R" * 2000,
        "created": [
            {
                "direction_id": f"d-{i}",
                "role": "created",
                "status": "active",
                "text": "X" * 200,
                "prs_merged": 3,
                "rationale": "why " * 100,
                "prediction": {"similar_past_merges": 2, "blob": "P" * 500},
            }
            for i in range(10)
        ],
        "retired": [
            {
                "direction_id": "d-ret",
                "role": "retired",
                "status": "missing",
                "final_metrics": {"merged": 5, "blob": "F" * 500},
            }
        ],
    }
    result = _summarize_previous_decision_outcomes(value)
    assert isinstance(result, dict)
    assert _ser_size(result) < _ser_size(value)
    assert result["cycle_id"] == "c-1"
    assert result["phase2_completed_at"] == "2026-07-01T00:00:00Z"
    assert "cycle_reasoning" not in result  # verbose free-text dropped
    assert len(result["created"]) <= 6
    first = result["created"][0]
    assert first["direction_id"] == "d-0"  # fetch pointer preserved
    assert "prediction" not in first  # heavy sub-body dropped
    assert "final_metrics" not in result["retired"][0]


def test_summarize_previous_decision_outcomes_malformed_never_raises():
    from app.strategic_spawning import _summarize_previous_decision_outcomes

    assert _summarize_previous_decision_outcomes(None) is None
    assert _summarize_previous_decision_outcomes("x") == "x"
    assert _summarize_previous_decision_outcomes({}) == {}
    # created not a list -> untouched key.
    out = _summarize_previous_decision_outcomes({"cycle_id": "c", "created": "bad"})
    assert out["cycle_id"] == "c"
    assert out["created"] == "bad"


def test_summarize_playbook_summary_shrinks_keeps_keys():
    from app.strategic_spawning import _summarize_playbook_summary

    value = {
        "entries": [
            {
                "key": f"pb-{i}",
                "content": "C" * 400,
                "source": "auto-generated",
                "updated_at": "2026-07-01T00:00:00Z",
                "hit_count": 3,
                "confirmed_count": 2,
            }
            for i in range(10)
        ]
    }
    result = _summarize_playbook_summary(value)
    assert isinstance(result, dict)
    assert _ser_size(result) < _ser_size(value)
    assert len(result["entries"]) <= 6
    first = result["entries"][0]
    assert first["key"] == "pb-0"  # fetch pointer preserved
    assert len(first["content"]) < 400
    assert "hit_count" not in first  # stat fields dropped


def test_summarize_playbook_summary_malformed_never_raises():
    from app.strategic_spawning import _summarize_playbook_summary

    assert _summarize_playbook_summary(None) is None
    assert _summarize_playbook_summary("x") == "x"
    assert _summarize_playbook_summary({}) == {}
    # entries not a list -> passthrough.
    assert _summarize_playbook_summary({"entries": "bad"}) == {"entries": "bad"}
    # Non-dict entry preserved.
    out = _summarize_playbook_summary({"entries": [1, {"key": "k"}]})
    assert out["entries"][0] == 1


def test_summarize_past_retirement_critiques_shrinks_keeps_ids():
    from app.strategic_spawning import _summarize_past_retirement_critiques

    value = [
        {
            "direction_id": f"d-{i}",
            "what_it_addressed": "W" * 300,
            "pattern_that_produced_it": "P" * 300,
            "lesson": "L" * 300,
        }
        for i in range(6)
    ]
    result = _summarize_past_retirement_critiques(value)
    assert isinstance(result, list)
    assert _ser_size(result) < _ser_size(value)
    assert len(result) <= 3
    first = result[0]
    assert first["direction_id"] == "d-0"
    assert len(first["lesson"]) < 300


def test_summarize_past_retirement_critiques_sentinel_and_malformed():
    from app.strategic_spawning import _summarize_past_retirement_critiques

    assert _summarize_past_retirement_critiques(None) is None
    assert _summarize_past_retirement_critiques("x") == "x"
    assert _summarize_past_retirement_critiques([]) == []
    # no_eligible_retirements sentinel is preserved.
    assert _summarize_past_retirement_critiques([{"no_eligible_retirements": True}]) == [
        {"no_eligible_retirements": True}
    ]
    assert _summarize_past_retirement_critiques([42]) == [42]


def test_summarize_self_reflection_log_shrinks_keeps_pointers():
    from app.strategic_spawning import _summarize_self_reflection_log

    value = [
        {
            "ts": f"2026-07-0{i}T00:00:00Z",
            "cycle_task_id": f"t-{i}",
            "what_was_hard": "H" * 300,
            "missing_context": "M" * 300,
            "proposed_self_improvement": "I" * 300,
        }
        for i in range(1, 6)
    ]
    result = _summarize_self_reflection_log(value)
    assert isinstance(result, list)
    assert _ser_size(result) < _ser_size(value)
    assert len(result) <= 3
    # Newest-few (tail) kept; pointers preserved.
    last = result[-1]
    assert last["cycle_task_id"] == "t-5"
    assert last["ts"] == "2026-07-05T00:00:00Z"
    assert len(last["what_was_hard"]) < 300


def test_summarize_self_reflection_log_malformed_never_raises():
    from app.strategic_spawning import _summarize_self_reflection_log

    assert _summarize_self_reflection_log(None) is None
    assert _summarize_self_reflection_log("x") == "x"
    assert _summarize_self_reflection_log([]) == []
    assert _summarize_self_reflection_log(["str", 1]) == ["str", 1]


def test_build_self_reflection_themes_finds_common_theme():
    """Themes are extracted when >5 entries share a recurring word in what_was_hard."""
    from app.strategic_spawning import _build_self_reflection_themes

    log = [
        {"what_was_hard": "Missing context on async patterns"},
        {"what_was_hard": "Needed more context for the change"},
        {"what_was_hard": "Hard to follow without broader context"},
        {"what_was_hard": "Confused by missing context in schema"},
        {"what_was_hard": "Struggled with test context setup"},
        {"what_was_hard": "Ran out of context for large refactor"},
    ]
    result = _build_self_reflection_themes(log)
    assert isinstance(result, list)
    assert len(result) >= 1
    context_themes = [t for t in result if t["theme"] == "context"]
    assert len(context_themes) == 1
    assert context_themes[0]["occurrences"] >= 5


def test_build_self_reflection_themes_empty_under_six():
    """Empty result when log has 5 or fewer entries."""
    from app.strategic_spawning import _build_self_reflection_themes

    assert _build_self_reflection_themes([]) == []
    assert _build_self_reflection_themes([{"what_was_hard": "x"}]) == []
    assert _build_self_reflection_themes([{"what_was_hard": str(i)} for i in range(5)]) == []


def test_build_self_reflection_themes_no_commonality():
    """Empty result when >5 entries have no overlapping significant words."""
    from app.strategic_spawning import _build_self_reflection_themes

    log = [
        {"what_was_hard": "Struggled with python packaging"},
        {"what_was_hard": "Hard to debug kubernetes networking"},
        {"what_was_hard": "Confused by react rendering lifecycle"},
        {"what_was_hard": "Missing documentation for build steps"},
        {"what_was_hard": "Unexpected behavior in database migrations"},
        {"what_was_hard": "Could not reproduce flaky integration test"},
    ]
    result = _build_self_reflection_themes(log)
    assert isinstance(result, list)
    assert result == []


def test_build_self_reflection_themes_malformed():
    """Malformed or None input returns empty list."""
    from app.strategic_spawning import _build_self_reflection_themes

    assert _build_self_reflection_themes(None) == []
    assert _build_self_reflection_themes("x") == []
    assert _build_self_reflection_themes(42) == []
    assert _build_self_reflection_themes([123]) == []


def test_build_self_reflection_themes_non_dict_entries_in_long_log():
    """Non-dict entries in a >5-entry log are skipped without raising AttributeError."""
    from app.strategic_spawning import _build_self_reflection_themes

    log = [
        {"what_was_hard": "Missing context for the async change"},
        {"what_was_hard": "Hard to follow without broader context"},
        "not a dict",
        {"what_was_hard": "Confused by missing context in schema"},
        42,
        {"what_was_hard": "Struggled with test context setup"},
        None,
        {"what_was_hard": "Ran out of context for large refactor"},
    ]
    result = _build_self_reflection_themes(log)
    assert isinstance(result, list)
    context_themes = [t for t in result if t["theme"] == "context"]
    assert len(context_themes) == 1
    assert context_themes[0]["occurrences"] >= 5


def test_build_self_reflection_themes_sorts_by_frequency():
    """Themes are sorted descending by occurrence count, at most 5 returned."""
    from app.strategic_spawning import _build_self_reflection_themes

    log = [
        {"what_was_hard": "Something about schema design testing"},
        {"what_was_hard": "Schema migration was painful testing"},
        {"what_was_hard": "Could not validate schema testing"},
        {"what_was_hard": "Schema changes broke everything testing"},
        {"what_was_hard": "Schema testing took too long"},
        {"what_was_hard": "Schema and testing both frustrating"},
    ]
    result = _build_self_reflection_themes(log)
    assert len(result) <= 5
    # Should have schema (6) first, then testing (6)
    assert result[0]["theme"] == "schema"
    occurrences = [t["occurrences"] for t in result]
    assert occurrences == sorted(occurrences, reverse=True)


def _oversized_section_fixtures() -> dict:
    """Realistic oversized values for the six previously-exempt sections."""
    return {
        "operator_overrides": [
            {
                "document": "vision",
                "field": f"f{i}",
                "kind": "k",
                "agent_value": "A" * 240,
                "operator_value": "O" * 240,
                "at": f"2026-07-0{(i % 9) + 1}T00:00:00Z",
            }
            for i in range(8)
        ],
        "knowledge_index": [
            {
                "id": f"doc-{i}",
                "title": f"Doc {i}",
                "summary": "S" * 300,
                "topic": "t",
                "direction_id": f"d-{i}",
                "source_kind": "web_research",
                "per_arm_verdicts": [{"blob": "X" * 400}],
                "origin_provenance": {"blob": "Y" * 400},
            }
            for i in range(15)
        ],
        "previous_decision_outcomes": {
            "cycle_id": "c-1",
            "phase2_completed_at": "2026-07-01T00:00:00Z",
            "summary_text": "T" * 500,
            "cycle_reasoning": "R" * 2000,
            "created": [
                {
                    "direction_id": f"d-{i}",
                    "role": "created",
                    "status": "active",
                    "text": "X" * 200,
                    "prediction": {"blob": "P" * 400},
                }
                for i in range(10)
            ],
            "retired": [],
        },
        "playbook_summary": {
            "entries": [
                {
                    "key": f"pb-{i}",
                    "content": "C" * 400,
                    "source": "auto-generated",
                    "hit_count": 3,
                }
                for i in range(10)
            ]
        },
        "past_retirement_critiques": [
            {
                "direction_id": f"d-{i}",
                "what_it_addressed": "W" * 300,
                "pattern_that_produced_it": "P" * 300,
                "lesson": "L" * 300,
            }
            for i in range(6)
        ],
        "self_reflection_log": [
            {
                "ts": f"2026-07-0{i}T00:00:00Z",
                "cycle_task_id": f"t-{i}",
                "what_was_hard": "H" * 300,
                "missing_context": "M" * 300,
                "proposed_self_improvement": "I" * 300,
            }
            for i in range(1, 6)
        ],
    }


_CRITICAL_IDENTITY_PAYLOAD = {
    "type": "strategic_review",
    "research_mode": False,
    "project_name": "P",
    "base_branch": "main",
    "is_self_repo": True,
    "enable_self_improvement": True,
    "model": "m",
    "effort": "medium",
    "vision": "V",
    "direction_counts": {"active": 1},
    "max_active_directions": 5,
    "open_message_count": 0,
}


@pytest.mark.parametrize("section", _DROPPABLE_SUMMARIZE_SECTIONS)
def test_droppable_section_degrades_not_drops_under_budget(section):
    """Integration: each previously-exempt section trims (not drops) under pressure.

    Packs the critical identity keys plus ONE oversized section at a budget just
    below the whole-payload size, so the section cannot ship whole and must fall
    through to its summarize_fn. Asserts the section survives (appears in output,
    trimmed) rather than landing in the wholesale-dropped list — the exact
    behavior change from swapping ``exempt: True`` to ``summarize: <fn>``.
    """
    from app.strategic_spawning import _STRATEGIC_REVIEW_SCHEMA
    from app.payload_schemas import pack_under_budget

    fixtures = _oversized_section_fixtures()
    payload = {**_CRITICAL_IDENTITY_PAYLOAD, section: fixtures[section]}
    # The packer's budget accounting sums per-VALUE sizes (not the whole-dict
    # size). Reserve exactly the critical values plus one byte under the whole
    # section value, so the section cannot ship whole and must fall through to
    # its summarize_fn (which produces a strictly smaller, budget-fitting form).
    critical_used = sum(
        _ser_size(v) for v in _CRITICAL_IDENTITY_PAYLOAD.values()
    )
    whole_section = _ser_size(fixtures[section])
    budget = critical_used + whole_section - 1
    packed = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA, budget=budget)
    meta = packed.get("_packer_meta", {})
    dropped_keys = {d["key"] for d in meta.get("dropped", [])}
    trimmed_keys = {t["key"] for t in meta.get("trimmed", [])}

    assert section in packed, (
        f"{section} degraded to nothing under budget — summarize should keep a skeleton"
    )
    assert section not in dropped_keys, (
        f"{section} was dropped wholesale; expected it to be trimmed instead"
    )
    assert section in trimmed_keys, (
        f"{section} should be recorded as trimmed in _packer_meta"
    )
    # The retained value is strictly smaller than the original.
    assert _ser_size(packed[section]) < _ser_size(fixtures[section])


# ── Reconciliation sections fixture contract ──────────────────────────────────


class TestReconciliationSectionsContract:
    """Pin the list-class keys of cycle_start_reconciliation to a fixture so
    that adding a backend key without updating the agent renderer fails CI.

    The fixture (backend/testing/__fixtures__/reconciliation_sections.json) is
    the single source of truth.  These tests verify the backend side; the agent
    side is covered in agent/src/handlers/strategic-sections.test.ts.
    """

    def _load_fixture(self) -> dict:
        from pathlib import Path

        fixture_path = (
            Path(__file__).parent / "__fixtures__" / "reconciliation_sections.json"
        )
        with open(fixture_path) as f:
            return json.load(f)

    def _minimal_recon_output(self) -> dict:
        """Return a non-None _build_cycle_start_reconciliation result."""
        from app.strategic_spawning import _build_cycle_start_reconciliation

        # An active direction with no gap_id is always ungrounded → non-empty
        # conflict list → function returns a dict (not None).
        direction_stats = [
            {"id": "d-contract-test", "status": "active", "text": "contract test"}
        ]
        out = _build_cycle_start_reconciliation(direction_stats, {}, {})
        assert out is not None, (
            "_build_cycle_start_reconciliation returned None for minimal data — "
            "the early-return guard may have changed; update this helper."
        )
        return out

    def test_fixture_schema_version_and_list_keys(self):
        fixture = self._load_fixture()
        assert fixture["schema_version"] == 1
        assert isinstance(fixture["list_keys"], list)
        assert len(fixture["list_keys"]) > 0

    def test_backend_output_contains_all_fixture_list_keys(self):
        """Every fixture key must appear as a list in the backend output.

        Fails when the fixture adds a key that _build_cycle_start_reconciliation
        does not yet emit — update the backend function or remove the key.
        """
        fixture = self._load_fixture()
        expected_keys = set(fixture["list_keys"])
        out = self._minimal_recon_output()

        actual_list_keys = {k for k, v in out.items() if isinstance(v, list)}
        missing = expected_keys - actual_list_keys
        assert not missing, (
            f"Keys in reconciliation_sections fixture but absent from "
            f"_build_cycle_start_reconciliation output: {sorted(missing)}. "
            f"Add them to the backend function or remove them from the fixture."
        )

    def test_fixture_covers_all_backend_list_keys(self):
        """Every list key the backend emits must be declared in the fixture.

        Fails when _build_cycle_start_reconciliation adds a new list key without
        updating backend/testing/__fixtures__/reconciliation_sections.json — the
        CI guard that catches the compute-but-never-render drift class.
        """
        fixture = self._load_fixture()
        expected_keys = set(fixture["list_keys"])
        out = self._minimal_recon_output()

        actual_list_keys = {k for k, v in out.items() if isinstance(v, list)}
        extra = actual_list_keys - expected_keys
        assert not extra, (
            f"_build_cycle_start_reconciliation emits list keys not declared in "
            f"the reconciliation_sections fixture: {sorted(extra)}. "
            f"Add them to backend/testing/__fixtures__/reconciliation_sections.json "
            f"AND add a renderer in agent/src/handlers/strategic-sections.ts."
        )


# ---------------------------------------------------------------------------
# _maybe_spawn_ci_compliance_cap_bump_fix — chronic vs routine classification
# ---------------------------------------------------------------------------

from app.strategic_spawning import _maybe_spawn_ci_compliance_cap_bump_fix  # noqa: E402


def _chronic_history():
    """Return a 3-bump history that trips the chronic-offender threshold."""
    from app.size_limit_policy import BumpRecord
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    return [
        BumpRecord(
            commit_sha="aaa",
            commit_date=(now - timedelta(days=150)).isoformat(),
            old_cap=3500,
            new_cap=4050,
        ),
        BumpRecord(
            commit_sha="bbb",
            commit_date=(now - timedelta(days=90)).isoformat(),
            old_cap=4050,
            new_cap=4500,
        ),
        BumpRecord(
            commit_sha="ccc",
            commit_date=(now - timedelta(days=30)).isoformat(),
            old_cap=4500,
            new_cap=4700,
        ),
    ]


def _make_gate(violations, *, head_sha="a" * 40):
    return {
        "blocked": True,
        "head_sha": head_sha,
        "violations": violations,
        "branch_literal_violations": [],
        "violation_count": len(violations),
    }


@pytest.mark.asyncio
async def test_cap_bump_task_decomposes_chronic_offender(db_session, mocker):
    """When a violation is on a chronic offender, the spawned task must direct
    decomposition and must NOT instruct a blind cap bump (--fix) for that file.

    This is the acceptance test named by the task:
    test_strategic_spawning.py::test_cap_bump_task_decomposes_chronic_offender
    """
    project = Project(
        name="chronic-test",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
        promotion_target="main",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test chronic spawn"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    gate = _make_gate(
        [{"section": "backend", "rel": "orchestrator.py", "count": 9200, "cap": 9000}]
    )

    with patch(
        "app.size_limit_policy.parse_bump_history",
        return_value=_chronic_history(),
    ):
        spawned = await _maybe_spawn_ci_compliance_cap_bump_fix(db_session, project, gate)
    await db_session.commit()

    assert spawned is not None
    prompt = spawned.first_payload["prompt"]
    title = spawned.first_payload["title"]

    # Must direct decomposition, not a cap bump.
    assert "decompos" in prompt.lower(), (
        "Expected prompt to mention decomposition for a chronic offender"
    )
    assert "orchestrator.py" in prompt, (
        "Expected prompt to name the chronic offender file"
    )
    # Must NOT tell the agent to run --fix on the chronic file.
    assert "python3 backend/testing/check_size_limits.py --fix" not in prompt, (
        "Prompt must not instruct --fix for a chronic offender"
    )
    # Title should signal decomposition, not a cap bump.
    assert "decompos" in title.lower() or "chronic" in title.lower(), (
        f"Task title should indicate decomposition, got: {title!r}"
    )
    # Context keys preserved.
    assert spawned.context["spawned_by"] == "ci_compliance_cap_bump_gate"
    assert spawned.context["head_sha"] == "a" * 40
    assert spawned.context["ci_compliance_cap_bump"] is True


@pytest.mark.asyncio
async def test_cap_bump_task_routine_violation_keeps_fix_instruction(db_session, mocker):
    """When all violations are routine (no chronic history), the spawned task
    must still instruct `check_size_limits.py --fix` as before.
    """
    project = Project(
        name="routine-test",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
        promotion_target="main",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test routine spawn"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    gate = _make_gate(
        [{"section": "backend", "rel": "new_module.py", "count": 1600, "cap": 1500}]
    )

    with patch("app.size_limit_policy.parse_bump_history", return_value=[]):
        spawned = await _maybe_spawn_ci_compliance_cap_bump_fix(db_session, project, gate)
    await db_session.commit()

    assert spawned is not None
    prompt = spawned.first_payload["prompt"]
    title = spawned.first_payload["title"]

    assert "python3 backend/testing/check_size_limits.py --fix" in prompt, (
        "Routine violation must still instruct --fix"
    )
    assert "bump" in title.lower() or "fix" in title.lower(), (
        f"Routine task title should reference bump/fix, got: {title!r}"
    )


@pytest.mark.asyncio
async def test_cap_bump_task_uncapped_violation_treated_as_routine(db_session, mocker):
    """Violations with cap=None (new files) are never chronic and must keep the
    --fix instruction regardless of git history.
    """
    project = Project(
        name="uncapped-test",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
        promotion_target="main",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test uncapped spawn"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    gate = _make_gate(
        [{"section": "backend", "rel": "brand_new.py", "count": 2000, "cap": None}]
    )

    classify_breach_mock = mocker.patch("app.size_limit_policy.classify_breach")
    spawned = await _maybe_spawn_ci_compliance_cap_bump_fix(db_session, project, gate)
    await db_session.commit()

    # classify_breach must NOT be called for uncapped violations.
    classify_breach_mock.assert_not_called()
    assert spawned is not None
    assert "python3 backend/testing/check_size_limits.py --fix" in spawned.first_payload["prompt"]


# ---------------------------------------------------------------------------
# Recent-handled conversation tail: bounds and ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_recent_handled_conversation_bounds_and_order(db_session):
    """``_fetch_recent_handled_conversation`` returns a bounded, chronological,
    excerpt-capped tail of the recently-ANSWERED operator conversation.

    Asserts the observable contract:
      * only status handled/resolved conversational rows are returned (open and
        system rows are excluded);
      * at most ``_RECENT_HANDLED_KEEP`` rows survive, keeping the NEWEST;
      * the survivors are ordered oldest-first (stable transcript read);
      * each content excerpt is capped at ``_RECENT_HANDLED_EXCERPT_MAX_CHARS``.
    """
    from app.strategic_spawning import (
        _fetch_recent_handled_conversation,
        _RECENT_HANDLED_KEEP,
        _RECENT_HANDLED_EXCERPT_MAX_CHARS,
    )

    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    base = datetime(2025, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
    # 12 handled operator messages, ascending created_at (so idx 11 is newest).
    handled_msgs = []
    for i in range(12):
        m = ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.NOTE,
            content=f"handled-{i:02d}",
            status="handled",
        )
        db_session.add(m)
        handled_msgs.append(m)
    # A long resolved message to exercise the excerpt cap (newest of all).
    long_content = "x" * (_RECENT_HANDLED_EXCERPT_MAX_CHARS + 500)
    long_msg = ProjectMessage(
        project_id=project.id,
        from_type="strategic",
        message_type=MessageType.NOTE,
        content=long_content,
        status="resolved",
    )
    db_session.add(long_msg)
    # Excluded rows: an OPEN message and a handled SYSTEM (bookkeeping) message.
    open_msg = ProjectMessage(
        project_id=project.id,
        from_type="user",
        message_type=MessageType.NOTE,
        content="still-open",
        status="open",
    )
    sys_msg = ProjectMessage(
        project_id=project.id,
        from_type="system",
        message_type=MessageType.NOTE,
        content="internal bookkeeping",
        status="handled",
    )
    db_session.add_all([open_msg, sys_msg])
    await db_session.commit()

    # Stamp deterministic ascending timestamps so ordering is unambiguous.
    for i, m in enumerate(handled_msgs):
        m.created_at = base + timedelta(minutes=i)
        db_session.add(m)
    long_msg.created_at = base + timedelta(minutes=100)  # newest overall
    db_session.add(long_msg)
    await db_session.commit()

    tail = await _fetch_recent_handled_conversation(db_session, project.id)

    # Bounded to the KEEP count.
    assert len(tail) == _RECENT_HANDLED_KEEP
    contents = [m["content"] for m in tail]
    ids = {m["id"] for m in tail}

    # Excluded rows never appear.
    assert "still-open" not in contents
    assert str(open_msg.id) not in ids
    assert "internal bookkeeping" not in contents
    assert str(sys_msg.id) not in ids

    # The newest survivors are kept: long_msg (t+100) plus the newest handled
    # operator rows; the oldest handled rows are dropped under the bound.
    assert str(long_msg.id) in ids
    assert "handled-00" not in contents  # oldest, dropped
    assert "handled-11" in contents  # newest handled operator row, kept

    # Chronological (oldest-first) order among the survivors.
    created = [m["created_at"] for m in tail]
    assert created == sorted(created)
    # long_msg is the newest → it sorts LAST in the oldest-first tail.
    assert tail[-1]["id"] == str(long_msg.id)

    # Excerpt cap enforced on the long message.
    assert len(tail[-1]["content"]) == _RECENT_HANDLED_EXCERPT_MAX_CHARS

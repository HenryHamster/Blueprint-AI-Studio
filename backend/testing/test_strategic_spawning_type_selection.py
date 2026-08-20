"""strategic_spawning: cycle type selection and force_strategic gating.

Covers design_doc_update vs strategic_review selection on architecture/strategy
cycles, ``force_strategic`` triggers and the ``strategic_failures >= 3`` block,
operator overrides, born-context ``extra_context`` seeding, knowledge/compute
indexes in the payload, and ``operator_rejection_rate``.

Shared scaffolding lives in ``_strategic_spawning_test_helpers.py``.
"""

import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy.exc import MissingGreenlet
from unittest.mock import AsyncMock
from sqlalchemy import select

import app.drift_detection  # noqa: F401 — registers DirectionDriftSnapshot in SQLModel.metadata for create_all
import app.shared_state as _shared_state
from app.models import (
    Task,
    TaskStatus,
)
from app.workbench_state import (
    STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
)
from app.autonomous_research import _spawn_research_task
from app.strategic_spawning import (
    _spawn_design_doc_update,
    _spawn_strategic_review,
    _STRATEGIC_REVIEW_SCHEMA,
    _DESIGN_DOC_UPDATE_SCHEMA,
)
from app.task_evaluators import (
    CHALLENGE_AGREES,
    CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
    CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
    CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH,
    CHALLENGE_DISAGREES,
    CHALLENGE_INDETERMINATE,
)
from _strategic_spawning_test_helpers import (  # noqa: F401 — _ensure_test_account_pool is an autouse fixture
    USE_POSTGRES,
    _ensure_test_account_pool,
    _make_project,
    _patch_spawning_deps,
    _seed_legacy_cycles,
    _select_agent_tasks,
)


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
async def test_design_doc_spawn_commits_cadence_receipt_with_task(db_session):
    """Architecture-phase cadence receipt is born in the task/cycle commit."""
    from app.research_cycle_cadence import (
        STRATEGIC_CADENCE_ACCEPTED_COUNT_CONTEXT_KEY,
        STRATEGIC_LAST_ACCEPTED_RESEARCH_COUNT_KEY,
    )

    project = _make_project(
        vision={"purpose": "test purpose", "_research_spawn_count": 6}
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(
        project,
        db_session,
        cadence_accepted_count=6,
    )

    assert spawned is True
    await db_session.refresh(project)
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    task = next(
        row
        for row in result.scalars().all()
        if row.first_payload["type"] == "design_doc_update"
    )
    assert task.context[STRATEGIC_CADENCE_ACCEPTED_COUNT_CONTEXT_KEY] == 6
    assert project.vision[STRATEGIC_LAST_ACCEPTED_RESEARCH_COUNT_KEY] == 6
    assert project.vision["_research_spawn_count"] == 6


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



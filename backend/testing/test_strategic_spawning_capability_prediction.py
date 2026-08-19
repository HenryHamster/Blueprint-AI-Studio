"""strategic_spawning: spawn-success prediction and direction-stat signals.

Covers ``predict_spawn_success`` (quality-score/momentum signals, first-attempt
rejection concentration), ``recommended_mode`` and spawn-gate metrics in the
direction stats, ``previous_decision_outcomes``, and
``_build_shipped_changes_manifest``.

Shared scaffolding lives in ``_strategic_spawning_test_helpers.py``.
"""

import uuid
from datetime import datetime, timezone, timedelta

import pytest
from unittest.mock import AsyncMock
from sqlalchemy import update

import app.drift_detection  # noqa: F401 — registers DirectionDriftSnapshot in SQLModel.metadata for create_all
import app.shared_state as _shared_state
from app.models import (
    Project,
    Task,
    TaskStatus,
    RECOMMENDED_MODE_META_KEY,
)
from app.workbench_state import (
    SESSION_IDLE_TTL_SECONDS,
    STRATEGIC_LOCAL_CONTINUATION_KEY,
    STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS,
    STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
)
from app.strategic_spawning import (
    _spawn_design_doc_update,
    _spawn_strategic_review,
    _build_shipped_changes_manifest,
)
from app.task_completion import _phase_runner_affinity
from _strategic_spawning_test_helpers import (  # noqa: F401 — _ensure_test_account_pool is an autouse fixture
    _ensure_test_account_pool,
    _local_continuation_marker,
    _make_local_runner,
    _make_project,
    _patch_spawning_deps,
    _seed_legacy_cycles,
    _select_agent_tasks,
)


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
    # The legacy vision body is no longer shipped, so the internal
    # local-continuation marker cannot leak into the agent-visible payload.
    assert "vision" not in task.payloads[0]
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
    # The legacy vision body is no longer shipped, so the internal
    # local-continuation marker cannot leak into the agent-visible payload.
    assert "vision" not in task.payloads[0]
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
    # The legacy vision body is no longer shipped, so the internal
    # local-continuation marker cannot leak into the agent-visible payload.
    assert "vision" not in task.payloads[0]


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
        "_agent_view_problem_model_lean",
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



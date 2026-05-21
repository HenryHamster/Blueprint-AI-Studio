import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

import app.shared_state as _ss
from app.direction_lifecycle import (
    _detect_partial_remediation_success,
    _process_direction_lifecycle,
)
from app.models import ArtifactType, Project, Task, TaskStatus
from app.task_completion import _handle_investigation_completion


def _history_entry(ts: datetime, failure_rate: float) -> dict:
    failed = round(10 * failure_rate)
    return {
        "timestamp": ts.isoformat(),
        "tasks_created": 10,
        "tasks_failed": failed,
        "tasks_failed_infra": 0,
        "prs_merged": 10 - failed,
    }


@pytest.mark.asyncio
async def test_remediation_spawn_sets_last_remediation_at_for_partial_detector(
    db_session, mocker
):
    """D13: in-flight remediation has a boundary timestamp before merge outcome."""
    mocker.patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock)
    direction_id = "d13-direction"
    direction = {
        "id": direction_id,
        "text": "Reduce recurring code failures",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "High code failure rate",
        "diagnosis_in_progress": True,
        "diagnosis_started_at": (datetime.utcnow() - timedelta(hours=1)).isoformat(),
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [
            _history_entry(datetime.utcnow() - timedelta(days=2), 0.9),
        ],
    }
    project = Project(
        name="D13 remediation spawn",
        user_id="u-d13",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    investigation = Task(
        project_id=project.id,
        title="Investigate direction",
        payloads=[
            {"type": "investigate", "prompt": "investigate", "base_branch": "main"}
        ],
        context={
            "direction_id": direction_id,
            "diagnosis_fingerprint": "fp-d13",
            "last_failure_class": "CODE_ERROR",
        },
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(investigation)
    await db_session.commit()
    await db_session.refresh(investigation)

    report = {
        "summary": "Missing lock causes recurring failed tasks",
        "root_cause": "A shared direction counter is updated without synchronization.",
        "affected_files": [
            {"path": "backend/app/shared_state.py", "issue": "missing lock"}
        ],
        "recommended_fix": "Guard the shared counter update with the existing lock.",
        "severity": "high",
        "confidence": "high",
    }
    await _handle_investigation_completion(
        investigation,
        SimpleNamespace(
            logs=json.dumps(report), artifact_type=ArtifactType.INVESTIGATION_REPORT
        ),
        db_session,
    )
    await db_session.commit()
    await db_session.refresh(project)

    remediation = (
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == investigation.id)
            )
        )
        .scalars()
        .one()
    )
    refreshed = next(d for d in project.directions if d["id"] == direction_id)
    assert refreshed["last_remediation_task_id"] == str(remediation.id)
    assert refreshed["last_remediation_at"]
    assert "last_remediation_result" not in refreshed

    spawned_at = datetime.fromisoformat(refreshed["last_remediation_at"])
    refreshed["research_history"].append(
        _history_entry(spawned_at + timedelta(minutes=1), 0.1)
    )
    assert _detect_partial_remediation_success(refreshed) is True


def _cost_history(*costs: float) -> list[dict]:
    """Build a minimal research_history with estimated_cost_usd entries and healthy metrics."""
    entries = []
    for i, cost in enumerate(costs):
        entries.append(
            {
                "timestamp": (
                    datetime.utcnow() - timedelta(days=len(costs) - i)
                ).isoformat(),
                "tasks_created": 2,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 2,
                "estimated_cost_usd": cost,
            }
        )
    return entries


async def _make_cost_project(db_session, direction: dict) -> Project:
    project = Project(
        name="Cost Budget Test",
        user_id="u-cost",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)
    return project


@pytest.mark.asyncio
async def test_process_lifecycle_pauses_on_cost_budget_exceeded(db_session, mocker):
    """Direction auto-pauses with cost evidence when cumulative spend exceeds cost_budget_usd."""
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    unproductive_diag = mocker.patch(
        "app.autonomous_research_diagnosis._spawn_unproductive_diagnosis",
        new_callable=AsyncMock,
    )
    direction = {
        "id": "d-budget-over",
        "text": "Over-budget direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "cost_budget_usd": 5.0,
        "research_history": _cost_history(3.0, 3.0),  # cumulative 6.0 > budget 5.0
    }
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "paused", (
        "direction should be auto-paused when cost budget exceeded"
    )
    assert d.get("auto_paused") is True
    reason = d.get("paused_reason", "")
    assert "budget" in reason.lower() or "cost" in reason.lower(), (
        f"paused_reason should mention budget/cost, got: {reason!r}"
    )
    assert d.get("cost_pause_evidence", {}).get("cumulative_estimated_cost_usd") == 6.0
    assert d.get("cost_pause_evidence", {}).get("cost_budget_usd") == 5.0
    unproductive_diag.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_lifecycle_no_pause_when_cost_within_budget(db_session, mocker):
    """Direction stays active when cumulative spend is below cost_budget_usd."""
    direction = {
        "id": "d-budget-ok",
        "text": "Within-budget direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "cost_budget_usd": 100.0,
        "research_history": _cost_history(3.0, 3.0),  # cumulative 6.0 < budget 100.0
    }
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "active", (
        "direction should stay active when spend is within budget"
    )


@pytest.mark.asyncio
async def test_process_lifecycle_no_cost_pause_without_budget_configured(
    db_session, mocker
):
    """Direction without cost_budget_usd is not paused regardless of estimated spend."""
    direction = {
        "id": "d-no-budget",
        "text": "Unbounded direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        # no cost_budget_usd set
        "research_history": _cost_history(
            500.0, 500.0
        ),  # high spend, no budget ceiling
    }
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "active", (
        "direction without cost_budget_usd should not be paused on cost"
    )


@pytest.mark.asyncio
async def test_process_lifecycle_flags_retirement_consideration_for_high_cost_per_merge(
    db_session, mocker
):
    """Expensive directions are flagged when cost/merge exceeds project median * multiplier."""
    now = datetime.utcnow()
    expensive_direction = {
        "id": "d-expensive",
        "text": "Expensive direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [
            {
                "timestamp": now.isoformat(),
                "tasks_created": 2,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 1,
                "estimated_cost_usd": 10.0,
            }
        ],
    }
    project = Project(
        name="Retirement Flag Test",
        user_id="u-retire",
        autonomous_enabled=True,
        directions=[
            {
                "id": "d-cheap-1",
                "text": "Cheap 1",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [
                    {
                        "timestamp": now.isoformat(),
                        "tasks_created": 2,
                        "tasks_failed": 0,
                        "tasks_failed_infra": 0,
                        "prs_merged": 1,
                        "estimated_cost_usd": 1.0,
                    }
                ],
            },
            {
                "id": "d-cheap-2",
                "text": "Cheap 2",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [
                    {
                        "timestamp": now.isoformat(),
                        "tasks_created": 2,
                        "tasks_failed": 0,
                        "tasks_failed_infra": 0,
                        "prs_merged": 1,
                        "estimated_cost_usd": 2.0,
                    }
                ],
            },
            expensive_direction,
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch.dict(
        "app.direction_lifecycle._runtime_config",
        {
            "direction_cost_per_merge_window": 6,
            "direction_cost_per_merge_multiplier": 2.0,
        },
        clear=False,
    )

    await _process_direction_lifecycle(
        project,
        db_session,
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=now,
    )
    await db_session.refresh(project)

    by_id = {d["id"]: d for d in project.directions}
    assert by_id["d-expensive"].get("retirement_consideration_needed") is True
    assert (
        by_id["d-expensive"]
        .get("retirement_consideration_evidence", {})
        .get("threshold_usd")
        == 4.0
    )
    assert "retirement_consideration_needed" not in by_id["d-cheap-1"]
    assert "retirement_consideration_needed" not in by_id["d-cheap-2"]


@pytest.mark.asyncio
async def test_retirement_evidence_includes_provider_model_split(db_session, mocker):
    """When a research_history entry carries cost_by_provider_model, the rolling
    aggregate is exposed on retirement_consideration_evidence and sums to the
    entry-level estimated_cost_usd within rounding."""
    now = datetime.utcnow()
    expensive_direction = {
        "id": "d-expensive-split",
        "text": "Expensive with split",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [
            {
                "timestamp": now.isoformat(),
                "tasks_created": 2,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 1,
                "estimated_cost_usd": 10.0,
                "cost_by_provider_model": {
                    "anthropic|claude-opus-4-7": 8.0,
                    "openai|gpt-5.4": 2.0,
                },
            }
        ],
    }
    project = Project(
        name="Split evidence test",
        user_id="u-split",
        autonomous_enabled=True,
        directions=[
            {
                "id": "d-split-cheap-1",
                "text": "Cheap 1",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [
                    {
                        "timestamp": now.isoformat(),
                        "tasks_created": 2,
                        "tasks_failed": 0,
                        "tasks_failed_infra": 0,
                        "prs_merged": 1,
                        "estimated_cost_usd": 1.0,
                    }
                ],
            },
            {
                "id": "d-split-cheap-2",
                "text": "Cheap 2",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [
                    {
                        "timestamp": now.isoformat(),
                        "tasks_created": 2,
                        "tasks_failed": 0,
                        "tasks_failed_infra": 0,
                        "prs_merged": 1,
                        "estimated_cost_usd": 2.0,
                    }
                ],
            },
            expensive_direction,
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch.dict(
        "app.direction_lifecycle._runtime_config",
        {
            "direction_cost_per_merge_window": 6,
            "direction_cost_per_merge_multiplier": 2.0,
        },
        clear=False,
    )

    await _process_direction_lifecycle(
        project,
        db_session,
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=now,
    )
    await db_session.refresh(project)

    by_id = {d["id"]: d for d in project.directions}
    evidence = by_id["d-expensive-split"].get("retirement_consideration_evidence") or {}
    split = evidence.get("cost_by_provider_model")
    assert isinstance(split, dict) and split, "provider/model split should be populated"
    assert "anthropic|claude-opus-4-7" in split
    assert "openai|gpt-5.4" in split
    total_split = sum(v["cost_usd"] for v in split.values())
    assert abs(total_split - 10.0) < 1e-3
    # share_pct is a 0..1 fraction
    shares = {k: v["share_pct"] for k, v in split.items()}
    assert abs(sum(shares.values()) - 1.0) < 1e-3
    assert abs(shares["anthropic|claude-opus-4-7"] - 0.8) < 1e-3


@pytest.mark.asyncio
async def test_retirement_consideration_still_flagged_without_provider_model_split(
    db_session, mocker
):
    """Legacy directions without cost_by_provider_model on history entries
    must still be flagged for retirement; the split is omitted (empty dict)
    rather than crashing or being skipped."""
    now = datetime.utcnow()
    expensive_legacy = {
        "id": "d-legacy-expensive",
        "text": "Legacy expensive",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [
            {
                "timestamp": now.isoformat(),
                "tasks_created": 2,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 1,
                "estimated_cost_usd": 10.0,
                # No cost_by_provider_model — legacy entry.
            }
        ],
    }
    project = Project(
        name="Legacy retirement test",
        user_id="u-legacy",
        autonomous_enabled=True,
        directions=[
            {
                "id": "d-legacy-cheap-1",
                "text": "Cheap legacy 1",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [
                    {
                        "timestamp": now.isoformat(),
                        "tasks_created": 2,
                        "tasks_failed": 0,
                        "tasks_failed_infra": 0,
                        "prs_merged": 1,
                        "estimated_cost_usd": 1.0,
                    }
                ],
            },
            {
                "id": "d-legacy-cheap-2",
                "text": "Cheap legacy 2",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [
                    {
                        "timestamp": now.isoformat(),
                        "tasks_created": 2,
                        "tasks_failed": 0,
                        "tasks_failed_infra": 0,
                        "prs_merged": 1,
                        "estimated_cost_usd": 2.0,
                    }
                ],
            },
            expensive_legacy,
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch.dict(
        "app.direction_lifecycle._runtime_config",
        {
            "direction_cost_per_merge_window": 6,
            "direction_cost_per_merge_multiplier": 2.0,
        },
        clear=False,
    )

    await _process_direction_lifecycle(
        project,
        db_session,
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=now,
    )
    await db_session.refresh(project)

    by_id = {d["id"]: d for d in project.directions}
    legacy_d = by_id["d-legacy-expensive"]
    assert legacy_d.get("retirement_consideration_needed") is True
    evidence = legacy_d.get("retirement_consideration_evidence") or {}
    # Threshold gating still works for legacy entries.
    assert evidence.get("threshold_usd") == 4.0
    # Split is present but empty rather than missing — matches the
    # documented "{}" fallback shape.
    assert evidence.get("cost_by_provider_model") == {}


@pytest.mark.asyncio
async def test_process_lifecycle_uses_default_cpm_config_when_runtime_values_invalid(
    db_session, mocker
):
    """Invalid runtime config values should not crash lifecycle processing."""
    direction = {
        "id": "d-invalid-cpm-config",
        "text": "Config fallback direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": _cost_history(1.0, 1.0),
    }
    project = await _make_cost_project(db_session, direction)
    mocker.patch.dict(
        "app.direction_lifecycle._runtime_config",
        {
            "direction_cost_per_merge_window": "not-an-int",
            "direction_cost_per_merge_multiplier": "not-a-float",
        },
        clear=False,
    )

    await _process_direction_lifecycle(
        project,
        db_session,
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
    )
    await db_session.refresh(project)

    assert project.directions[0]["status"] == "active"


# ── Autonomous vs operator-assisted attribution (pending sibling cluster 1/2) ──
# The sibling adds classify_task_autonomy to direction_outcomes and switches
# _cost_per_merge to use autonomous_prs_merged as its numerator.
_XFAIL_SIBLING = pytest.mark.xfail(
    strict=False,
    reason="pending sibling cluster 1/2: classify_task_autonomy not yet in direction_outcomes",
)


@_XFAIL_SIBLING
def test_classify_task_autonomy_orchestrator_created():
    """ORCHESTRATOR-created task with no operator signals is classified as autonomous."""
    from app.direction_outcomes import classify_task_autonomy  # ImportError → xfail

    task = Task(created_by="ORCHESTRATOR", latest_status_message=None)
    assert classify_task_autonomy(task) is True


@_XFAIL_SIBLING
def test_classify_task_autonomy_user_created():
    """USER-created task is always operator-assisted regardless of other signals."""
    from app.direction_outcomes import classify_task_autonomy

    task = Task(created_by="USER")
    assert classify_task_autonomy(task) is False


@_XFAIL_SIBLING
def test_classify_task_autonomy_admin_spawned():
    """ORCHESTRATOR task with ADMIN_* spawned_by context is operator-assisted."""
    from app.direction_outcomes import classify_task_autonomy

    task = Task(created_by="ORCHESTRATOR", context={"spawned_by": "ADMIN_MANUAL"})
    assert classify_task_autonomy(task) is False


@_XFAIL_SIBLING
def test_classify_task_autonomy_human_approved_merge():
    """Task whose merge was approved by an operator (status message prefix) is operator-assisted."""
    from app.direction_outcomes import classify_task_autonomy

    task = Task(
        created_by="ORCHESTRATOR",
        latest_status_message="Human-approved: merged by operator",
    )
    assert classify_task_autonomy(task) is False


@_XFAIL_SIBLING
def test_classify_task_autonomy_request_changes_spawned():
    """Fix task spawned via request-changes (human feedback in fix_prompt) is operator-assisted."""
    from app.direction_outcomes import classify_task_autonomy

    task = Task(
        created_by="ORCHESTRATOR",
        payloads=[
            {
                "type": "fix",
                "prompt": "fix issues",
                "fix_prompt": "Human reviewer requested changes:\n\nMissing tests",
                "base_branch": "main",
            }
        ],
    )
    assert classify_task_autonomy(task) is False


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=False,
    reason="pending sibling cluster 1/2: _cost_per_merge not yet using autonomous_prs_merged",
)
async def test_process_lifecycle_cpm_zero_autonomous_no_retirement_flag(
    db_session, mocker
):
    """CPM is None when autonomous_prs_merged=0 — no retirement flag even if prs_merged>0."""
    now = datetime.utcnow()

    def _entry(cost, total_merged, autonomous_merged):
        return {
            "timestamp": now.isoformat(),
            "tasks_created": 2,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": total_merged,
            "autonomous_prs_merged": autonomous_merged,
            "assisted_prs_merged": total_merged - autonomous_merged,
            "estimated_cost_usd": cost,
        }

    project = Project(
        name="CPM Zero Autonomous Test",
        user_id="u-cpm-zero",
        autonomous_enabled=True,
        directions=[
            {
                "id": "d-za-cheap-1",
                "text": "Cheap A",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [_entry(1.0, 1, 1)],
            },
            {
                "id": "d-za-cheap-2",
                "text": "Cheap B",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [_entry(2.0, 1, 1)],
            },
            {
                "id": "d-za-all-assisted",
                "text": "All Assisted",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                # prs_merged=1 but autonomous_prs_merged=0 → CPM must be None
                "research_history": [_entry(10.0, 1, 0)],
            },
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    mocker.patch.dict(
        "app.direction_lifecycle._runtime_config",
        {
            "direction_cost_per_merge_window": 6,
            "direction_cost_per_merge_multiplier": 2.0,
        },
        clear=False,
    )

    await _process_direction_lifecycle(
        project,
        db_session,
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=now,
    )
    await db_session.refresh(project)

    by_id = {d["id"]: d for d in project.directions}
    assert "retirement_consideration_needed" not in by_id["d-za-all-assisted"]


@pytest.mark.asyncio
async def test_process_lifecycle_cpm_unchanged_for_fully_autonomous_direction(
    db_session, mocker
):
    """Retirement threshold is unchanged when autonomous_prs_merged == prs_merged (migration no-op)."""
    now = datetime.utcnow()

    def _auto_entry(cost, merged):
        return {
            "timestamp": now.isoformat(),
            "tasks_created": 2,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": merged,
            "autonomous_prs_merged": merged,
            "assisted_prs_merged": 0,
            "estimated_cost_usd": cost,
        }

    project = Project(
        name="Fully Autonomous CPM Regression",
        user_id="u-cpm-full",
        autonomous_enabled=True,
        directions=[
            {
                "id": "d-fa-cheap-1",
                "text": "Cheap 1",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [_auto_entry(1.0, 1)],
            },
            {
                "id": "d-fa-cheap-2",
                "text": "Cheap 2",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [_auto_entry(2.0, 1)],
            },
            {
                "id": "d-fa-expensive",
                "text": "Expensive",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [_auto_entry(10.0, 1)],
            },
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    mocker.patch.dict(
        "app.direction_lifecycle._runtime_config",
        {
            "direction_cost_per_merge_window": 6,
            "direction_cost_per_merge_multiplier": 2.0,
        },
        clear=False,
    )

    await _process_direction_lifecycle(
        project,
        db_session,
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=now,
    )
    await db_session.refresh(project)

    by_id = {d["id"]: d for d in project.directions}
    # median([1.0, 2.0, 10.0])=2.0, threshold=4.0; 10.0>4.0 → flagged
    assert by_id["d-fa-expensive"].get("retirement_consideration_needed") is True
    assert "retirement_consideration_needed" not in by_id["d-fa-cheap-1"]
    assert "retirement_consideration_needed" not in by_id["d-fa-cheap-2"]


@pytest.mark.asyncio
async def test_process_lifecycle_lifetime_burn_pause_via_aggregate(db_session, mocker):
    """Lifetime burn backstop pauses direction + sets evidence + flags manual-intervention.

    Tests the aggregate-driven slow burn: a direction whose recent rolling window
    would NOT trip the churn guardrail, but whose lifetime aggregate (trimmed old
    cycles) carries enough accumulated non-merge volume to trip the lifetime
    backstop threshold. Asserts the direction is paused with 'Lifetime burn backstop'
    reason, the stored evidence matches the computed metrics, and the
    manual-intervention flag is set with its timestamp.
    """
    now = datetime.utcnow()

    direction = {
        "id": "d-lifetime-burn",
        "text": "Direction with aggregate slow burn",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        # Recent rolling window: 3 cycles, each with 10 completed tasks
        # and 2 merges = 8 non-merge per cycle, 20% merge rate.
        # Total window: 24 non-merge, 6 merged, 20% merge rate.
        # Rule 5 doesn't fire: although 24 >= 15, merge_rate is 20% > 10% minimum.
        "research_history": [
            {
                "timestamp": (now - timedelta(days=3)).isoformat(),
                "tasks_created": 10,
                "tasks_completed": 10,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 2,
            },
            {
                "timestamp": (now - timedelta(days=2)).isoformat(),
                "tasks_created": 10,
                "tasks_completed": 10,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 2,
            },
            {
                "timestamp": now.isoformat(),
                "tasks_created": 10,
                "tasks_completed": 10,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 2,
            },
        ],
        # Trimmed history aggregate: 136 completed, 0 failed, 10 merged
        # Non-merge in aggregate: 126. Combined with window's 24 = 150 total,
        # lifetime merge rate = 16/(150+16) ≈ 9.6%, which is below 10% threshold.
        # This should trip the lifetime backstop (Rule 6).
        "history_aggregate": {
            "total_cycles": 10,
            "total_tasks_created": 136,
            "total_tasks_completed": 136,
            "total_tasks_failed": 0,
            "total_tasks_failed_infra": 0,
            "total_prs_merged": 10,
        },
    }

    project = Project(
        name="Lifetime Burn Test",
        user_id="u-lifetime",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    await _process_direction_lifecycle(
        project,
        db_session,
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=now,
    )
    await db_session.refresh(project)

    d = project.directions[0]
    # Direction should be paused with lifetime burn reason
    assert d["status"] == "paused", (
        "direction should be auto-paused when lifetime burn threshold exceeded"
    )
    assert d.get("auto_paused") is True
    reason = d.get("paused_reason", "")
    assert reason.startswith("Lifetime burn backstop:"), (
        f"paused_reason should start with 'Lifetime burn backstop:', got: {reason!r}"
    )
    # Verify evidence is stored and matches computed metrics
    evidence = d.get("lifetime_burn_pause_evidence", {})
    assert isinstance(evidence, dict), "lifetime_burn_pause_evidence should be a dict"
    assert evidence.get("lifetime_merged") == 16, (
        f"expected lifetime_merged=16, got {evidence.get('lifetime_merged')}"
    )
    assert evidence.get("lifetime_non_merge_finished") == 150, (
        f"expected lifetime_non_merge_finished=150, got {evidence.get('lifetime_non_merge_finished')}"
    )
    assert 0.095 < evidence.get("lifetime_merge_rate", 0) < 0.098, (
        f"expected lifetime_merge_rate ~0.0964, got {evidence.get('lifetime_merge_rate')}"
    )
    # Manual-intervention flag should be set
    assert d.get("needs_manual_intervention") is True, (
        "direction should be flagged for manual intervention on lifetime burn pause"
    )
    assert d.get("needs_manual_intervention_at"), (
        "needs_manual_intervention_at timestamp should be set"
    )


@pytest.mark.asyncio
async def test_lifetime_burn_manual_intervention_survives_ttl_window(
    db_session, mocker
):
    """Lifetime-burn manual-intervention flag persists past TTL window.

    A direction paused by the lifetime-burn backstop is flagged for manual
    intervention (operator must retire or reset). Unlike remediation-stuck
    scenarios, this flag should NOT be cleared by the TTL safety net after
    72 hours — the direction remains paused and needs operator action.
    """
    now = datetime.utcnow()
    # Flag was set 75 hours ago (past the 72h TTL)
    flagged_at = now - timedelta(hours=75)

    direction = {
        "id": "d-lifetime-burn-ttl",
        "text": "Lifetime burn direction",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "Lifetime burn backstop: cumulative non-merge tasks exceeded threshold",
        "paused_at": flagged_at.isoformat(),
        "needs_manual_intervention": True,
        "needs_manual_intervention_at": flagged_at.isoformat(),
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        # No remediation_monitor_started_at — lifetime-burn is not a remediation case
        "lifetime_burn_pause_evidence": {
            "lifetime_merged": 16,
            "lifetime_non_merge_finished": 150,
            "lifetime_merge_rate": 0.0964,
        },
        "research_history": [],
        "history_aggregate": {},
    }

    project = Project(
        name="Lifetime Burn TTL Test",
        user_id="u-lb-ttl",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Process lifecycle with now = 75 hours after the flag was set
    await _process_direction_lifecycle(
        project,
        db_session,
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=now,
    )
    await db_session.refresh(project)

    d = project.directions[0]
    # The lifetime-burn manual-intervention flag should SURVIVE the TTL window
    assert d.get("needs_manual_intervention") is True, (
        "lifetime-burn manual-intervention flag should survive past 72h TTL window"
    )
    assert d.get("needs_manual_intervention_at") == flagged_at.isoformat(), (
        "needs_manual_intervention_at should remain unchanged"
    )
    assert d["status"] == "paused", "direction should remain paused after TTL expiry"


@pytest.mark.asyncio
async def test_remediation_stuck_manual_intervention_cleared_by_ttl_window(
    db_session, mocker
):
    """Remediation-stuck manual-intervention flag IS cleared by TTL window.

    A direction paused by a code-failure reason with no remediation monitor
    ever started (i.e., remediation kept failing and never merged) is a
    'stuck remediation' scenario. The manual-intervention flag should be
    cleared by the TTL safety net after 72 hours so operators are not
    perpetually alerted about a direction the system cannot fix.
    """
    now = datetime.utcnow()
    # Flag was set 75 hours ago (past the 72h TTL) but within 48h cooldown window
    # to prevent auto-recovery from interfering with the test.
    flagged_at = now - timedelta(hours=30)

    direction = {
        "id": "d-remediation-stuck-ttl",
        "text": "Stuck remediation direction",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "High code failure rate",
        "paused_at": flagged_at.isoformat(),
        "needs_manual_intervention": True,
        "needs_manual_intervention_at": (
            now - timedelta(hours=75)
        ).isoformat(),  # Set the flag 75h ago
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        # No remediation_monitor_started_at — remediation has not successfully merged
        "research_history": [
            {
                "timestamp": (now - timedelta(days=1)).isoformat(),
                "tasks_created": 10,
                "tasks_failed": 9,
                "tasks_failed_infra": 0,
                "prs_merged": 0,
            }
        ],
    }

    project = Project(
        name="Remediation Stuck TTL Test",
        user_id="u-rem-stuck-ttl",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Process lifecycle with now = 75 hours after the flag was set
    await _process_direction_lifecycle(
        project,
        db_session,
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=now,
    )
    await db_session.refresh(project)

    d = project.directions[0]
    # The remediation-stuck manual-intervention flag SHOULD be cleared by TTL
    assert d.get("needs_manual_intervention") is None, (
        "remediation-stuck manual-intervention flag should be cleared after 72h TTL"
    )
    assert d.get("needs_manual_intervention_at") is None, (
        "needs_manual_intervention_at should be cleared"
    )
    assert d["status"] == "paused", (
        "direction should remain paused (within 48h recovery window; TTL only clears the flag, not pause state)"
    )


@pytest.mark.asyncio
async def test_process_lifecycle_churn_pause_emits_blocker_notification(
    db_session, mocker
):
    """Churn-guardrail pause emits a BLOCKER direction_churn_pause message (not milestone).

    Constructs a direction whose rolling window trips the churn guardrail (high non-merge
    volume, low merge rate) but does NOT exceed the lifetime-burn threshold. Asserts:
    - direction transitions to paused with auto_paused=True and a 'Churn guardrail:' reason
    - churn_pause_evidence is stored with expected windowed counts
    - a BLOCKER ProjectMessage with event_type='direction_churn_pause' is emitted
    - needs_manual_intervention is NOT set (churn pauses auto-recover)
    """
    from app.models import ProjectMessage, MessageType

    now = datetime.utcnow()

    # 5 recent cycles, each with 10 tasks completed and 0 merges.
    # Window: 50 non-merge_finished, 0 merged, 0% merge rate.
    # This trips the churn guardrail (rule 5: non_merge_finished>=15, merge_rate<10%).
    # Lifetime totals are modest enough to NOT trip the lifetime backstop.
    direction = {
        "id": "d-churn-test",
        "text": "Direction with rolling churn pattern",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        # 5 cycles with 10 tasks each. First cycle has 1 merge so total_merged=1
        # (bypasses Rule 2 "Unproductive: 0 PRs merged"). Remaining cycles have 0
        # merges. non_merge_finished = 49, merge_rate = 1/50 = 2% < 10% → Rule 5
        # (Churn guardrail) fires.
        "research_history": [
            {
                "timestamp": (now - timedelta(days=5)).isoformat(),
                "tasks_created": 10,
                "tasks_completed": 10,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 1,  # one merge in first cycle → total_merged=1, bypasses Rule 2
            },
            *[
                {
                    "timestamp": (now - timedelta(days=i)).isoformat(),
                    "tasks_created": 10,
                    "tasks_completed": 10,
                    "tasks_failed": 0,
                    "tasks_failed_infra": 0,
                    "prs_merged": 0,
                }
                for i in range(4, 0, -1)
            ],
        ],
        # Small aggregate — not enough lifetime volume to trip backstop
        "history_aggregate": {
            "total_cycles": 2,
            "total_tasks_created": 10,
            "total_tasks_completed": 10,
            "total_tasks_failed": 0,
            "total_tasks_failed_infra": 0,
            "total_prs_merged": 2,
        },
    }

    project = Project(
        name="Churn Pause Test",
        user_id="u-churn",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    await _process_direction_lifecycle(
        project,
        db_session,
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=now,
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "paused", "direction should be auto-paused by churn guardrail"
    assert d.get("auto_paused") is True
    reason = d.get("paused_reason", "")
    assert reason.startswith("Churn guardrail:"), (
        f"paused_reason should start with 'Churn guardrail:', got: {reason!r}"
    )

    # Churn evidence must be stored
    evidence = d.get("churn_pause_evidence", {})
    assert isinstance(evidence, dict), "churn_pause_evidence should be a dict"
    assert evidence.get("non_merge_finished", 0) >= 15, (
        f"expected non_merge_finished>=15, got {evidence.get('non_merge_finished')}"
    )
    assert evidence.get("merge_rate", 1.0) < 0.10, (
        f"expected merge_rate<0.10, got {evidence.get('merge_rate')}"
    )

    # A BLOCKER ProjectMessage with event_type='direction_churn_pause' must be present
    from sqlmodel import select

    msgs = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.message_type == MessageType.BLOCKER,
                )
            )
        )
        .scalars()
        .all()
    )
    churn_msgs = [
        m for m in msgs if m.context.get("event_type") == "direction_churn_pause"
    ]
    assert len(churn_msgs) == 1, (
        f"expected exactly 1 direction_churn_pause BLOCKER message, got {len(churn_msgs)}"
    )
    msg_ctx = churn_msgs[0].context
    assert msg_ctx.get("evidence") is not None, (
        "churn evidence should be in message context"
    )

    # Churn pauses do NOT require manual intervention (they auto-recover)
    assert not d.get("needs_manual_intervention"), (
        "churn pause should NOT set needs_manual_intervention"
    )

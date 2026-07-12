from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

import app.shared_state as _ss
import uuid

from app.time_utils import _utcnow
from app.direction_lifecycle import (
    _process_direction_lifecycle,
    _process_direction_lifecycle_locked,
)
from app.models import Project, Task, TaskStatus


def _history_entry(ts: datetime, failure_rate: float) -> dict:
    failed = round(10 * failure_rate)
    return {
        "timestamp": ts.isoformat(),
        "tasks_created": 10,
        "tasks_failed": failed,
        "tasks_failed_infra": 0,
        "prs_merged": 10 - failed,
    }


def _cost_history(*costs: float) -> list[dict]:
    """Build a minimal research_history with estimated_cost_usd entries and healthy metrics."""
    entries = []
    for i, cost in enumerate(costs):
        entries.append(
            {
                "timestamp": (
                    _utcnow() - timedelta(days=len(costs) - i)
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
        busy_direction_ids=set(),
        now=_utcnow(),
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
        busy_direction_ids=set(),
        now=_utcnow(),
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
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "active", (
        "direction without cost_budget_usd should not be paused on cost"
    )


@pytest.mark.asyncio
async def test_process_lifecycle_cost_pause_fires_when_aggregate_pushes_over_budget(
    db_session, mocker
):
    """Auto-pause fires when live history alone is under budget but live + aggregate exceeds it.

    This is the regression the fold fix addresses: without folding
    history_aggregate.total_estimated_cost_usd, a direction past the 10-cycle
    eviction window would fail to pause even when its true lifetime cost exceeds
    the budget.
    """
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    direction = {
        "id": "d-agg-budget-over",
        "text": "Direction past eviction window",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "cost_budget_usd": 5.0,
        # Live history alone: 2.0 — BELOW budget 5.0
        "research_history": _cost_history(2.0),
        # Evicted aggregate: 4.0 — so lifetime = 6.0 > budget 5.0
        "history_aggregate": {"total_estimated_cost_usd": 4.0},
    }
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "paused", (
        "direction should be auto-paused when live + aggregate cost exceeds budget"
    )
    evidence = d.get("cost_pause_evidence", {})
    assert evidence.get("cumulative_estimated_cost_usd") == 6.0, (
        f"expected cumulative cost 6.0, got {evidence.get('cumulative_estimated_cost_usd')!r}"
    )
    assert evidence.get("cost_budget_usd") == 5.0


@pytest.mark.asyncio
async def test_process_lifecycle_flags_retirement_consideration_for_high_cost_per_merge(
    db_session, mocker
):
    """Expensive directions are flagged when cost/merge exceeds project median * multiplier."""
    now = _utcnow()
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
    now = _utcnow()
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
    now = _utcnow()
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
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)

    assert project.directions[0]["status"] == "active"


@pytest.mark.asyncio
async def test_process_lifecycle_cpm_unchanged_for_fully_autonomous_direction(
    db_session, mocker
):
    """Retirement threshold is unchanged when autonomous_prs_merged == prs_merged (migration no-op)."""
    now = _utcnow()

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
    now = _utcnow()

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
    now = _utcnow()
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
    now = _utcnow()
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

    now = _utcnow()

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


@pytest.mark.asyncio
async def test_process_lifecycle_already_built_routes_to_possibly_complete(
    db_session, mocker
):
    """An "already-built" direction is flagged possibly_complete, not churn-paused.

    The direction's recent cycles are dominated by correct no-op "already-present"
    completions (tasks_completed_no_pr ~= tasks_completed), with no merges and no code
    failures. A residual amount of genuine non-merge volume still trips the churn /
    zero-merge family, but the already-built signature must suppress the pause and
    surface the direction as possibly_complete for the operator instead.
    """
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()
    # 5 cycles: 20 completed each, 16 no-op (0.8 dominance), 0 merged, 0 failures.
    # Real non-merge volume = 5*(20-16) = 20 (>=15 churn threshold) so a churn/zero-merge
    # pause would fire — but no_pr_ratio = 0.8 satisfies the already-built signature.
    history = [
        {
            "timestamp": (now - timedelta(days=i)).isoformat(),
            "tasks_created": 20,
            "tasks_completed": 20,
            "tasks_completed_no_pr": 16,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": 0,
        }
        for i in range(5, 0, -1)
    ]
    direction = {
        "id": "d-already-built",
        "text": "Feature that is already implemented on the integration branch",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": history,
    }
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=now,
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "active", (
        f"already-built direction must NOT be paused, got status={d['status']!r} "
        f"reason={d.get('paused_reason')!r}"
    )
    assert not d.get("auto_paused"), "already-built direction must not be auto-paused"
    assert d.get("possibly_complete") is True, (
        "already-built direction should be flagged possibly_complete"
    )
    suggestion = d.get("completion_suggestion", "")
    assert "already built" in suggestion.lower(), (
        f"completion_suggestion should explain already-built, got: {suggestion!r}"
    )


@pytest.mark.asyncio
async def test_process_lifecycle_productive_direction_not_churn_paused(
    db_session, mocker
):
    """Fix #1 integration: a still-productive direction (last_cycle_productive=True,
    consecutive_unproductive_cycles=0) whose most-recent cycle completed genuine
    no-op work (already-built) is NOT paused by the cumulative merge-rate family
    (Rule 2 / 5 / 6).

    Signal-that-lies refinement: ``last_cycle_productive`` alone (mere spawning) no
    longer shields — the direction must show a real landing/no-op signal in recent
    history. The latest cycle here carries a genuine no-op completion, so the shield
    legitimately holds; a spawn-only-never-land direction is now correctly paused
    (covered by the unit tests in test_strategic_completion)."""
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()
    # 6 cycles, 5 tasks each, fully completed, 0 merged — Rule 2 + Rule 5 territory.
    history = [
        {
            "timestamp": (now - timedelta(days=i)).isoformat(),
            "tasks_created": 5,
            "tasks_completed": 4,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": 0,
        }
        for i in range(6, 0, -1)
    ]
    # Most-recent cycle completed a genuine no-op (deliverable already built) — a
    # real productivity signal that holds the shield under the corrected semantics.
    history[-1]["tasks_completed_no_pr"] = 1
    direction = {
        "id": "d-productive-no-merge",
        "text": "Productive operator-ask direction with no merges yet",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "last_cycle_productive": True,
        "consecutive_unproductive_cycles": 0,
        "research_history": history,
    }
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=now,
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "active", (
        f"still-productive direction must NOT be paused, got status={d['status']!r} "
        f"reason={d.get('paused_reason')!r}"
    )
    assert not d.get("auto_paused")


@pytest.mark.asyncio
async def test_process_lifecycle_productive_direction_still_pauses_on_code_failure(
    db_session, mocker
):
    """Fix #1 integration: the productivity gate does NOT shield code-failure rules.
    A direction with last_cycle_productive=True but high code-failure rate (Rule 3)
    is still auto-paused."""
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()
    # 3 cycles, 10 tasks each, 9 code failures → >70% code failure rate (Rule 3).
    history = [
        {
            "timestamp": (now - timedelta(days=i)).isoformat(),
            "tasks_created": 3,
            "tasks_completed": 0,
            "tasks_failed": 3,
            "tasks_failed_infra": 0,
            "prs_merged": 0,
        }
        for i in range(3, 0, -1)
    ]
    direction = {
        "id": "d-productive-but-failing",
        "text": "Direction whose tasks keep crashing",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "last_cycle_productive": True,
        "consecutive_unproductive_cycles": 0,
        "research_history": history,
    }
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=now,
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "paused", (
        "code-failure rules must still pause a 'productive' direction"
    )
    reason = d.get("paused_reason", "")
    assert "High code failure rate" in reason or "Recent stall" in reason, (
        f"expected a code-failure pause reason, got: {reason!r}"
    )


@pytest.mark.asyncio
async def test_grace_period_blocks_unproductive_repause_on_stale_history(
    db_session, mocker
):
    """Just-recovered direction with frozen unproductive history is NOT re-paused.

    When auto_recovered_at is set and no history entry exists after that timestamp,
    history-based rules (1, 2, 5, 6) are skipped. A direction that previously triggered
    Rule 2 (Unproductive: 0 PRs merged) must not immediately re-trigger it before any
    new research cycle runs.
    """
    recovered_at = _utcnow() - timedelta(minutes=5)
    # 5 cycles with 0 merges, all timestamped before recovery — Rule 2 would normally fire
    history = [
        {
            "timestamp": (recovered_at - timedelta(days=i)).isoformat(),
            "tasks_created": 5,
            "tasks_completed": 5,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": 0,
        }
        for i in range(5, 0, -1)
    ]
    direction = {
        "id": "d-grace-no-repause",
        "text": "Unproductive direction in grace period",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "auto_recovered_at": recovered_at.isoformat(),
        "research_history": history,
    }
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "active", (
        "grace period should block Rule 2 re-pause when no new cycle exists after recovery"
    )
    assert d.get("auto_recovered_at") is not None, (
        "auto_recovered_at should remain set while still in grace period"
    )


@pytest.mark.asyncio
async def test_grace_period_expires_after_new_cycle_appended_triggers_pause(
    db_session, mocker
):
    """Grace period ends once a new research cycle is appended after recovery.

    A direction with a new unproductive cycle (timestamp > auto_recovered_at) IS
    paused again by Rule 2, and auto_recovered_at is cleared on re-pause.
    """
    recovered_at = _utcnow() - timedelta(hours=2)
    # 4 pre-recovery cycles with 0 merges
    pre_history = [
        {
            "timestamp": (recovered_at - timedelta(days=i)).isoformat(),
            "tasks_created": 5,
            "tasks_completed": 5,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": 0,
        }
        for i in range(4, 0, -1)
    ]
    # One new cycle AFTER recovery — still 0 merges, tasks fully completed
    new_cycle = {
        "timestamp": (recovered_at + timedelta(minutes=30)).isoformat(),
        "tasks_created": 5,
        "tasks_completed": 5,
        "tasks_failed": 0,
        "tasks_failed_infra": 0,
        "prs_merged": 0,
    }
    direction = {
        "id": "d-grace-expires",
        "text": "Unproductive direction with new cycle after recovery",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "auto_recovered_at": recovered_at.isoformat(),
        "research_history": [*pre_history, new_cycle],
    }
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "paused", (
        "direction should be re-paused once a new unproductive cycle exists after recovery"
    )
    assert d.get("auto_paused") is True
    reason = d.get("paused_reason", "")
    assert "Unproductive" in reason, (
        f"paused_reason should be Rule 2 (Unproductive), got: {reason!r}"
    )
    assert d.get("auto_recovered_at") is None, (
        "auto_recovered_at should be cleared when re-paused"
    )


@pytest.mark.asyncio
async def test_grace_period_does_not_block_code_failure_pause(db_session, mocker):
    """Grace period does NOT protect against code-failure rules (3, 4).

    A direction in grace period (auto_recovered_at set, no new cycle appended)
    with high code failure rate IS still paused by Rule 3. Auto-retirement must
    not be weakened by the grace period.
    """
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )

    recovered_at = _utcnow() - timedelta(minutes=5)
    # 3 cycles with 90% code failure rate — triggers Rule 3 regardless of grace
    history = [
        {
            "timestamp": (recovered_at - timedelta(days=i)).isoformat(),
            "tasks_created": 10,
            "tasks_failed": 9,
            "tasks_failed_infra": 0,
            "prs_merged": 0,
        }
        for i in range(3, 0, -1)
    ]
    direction = {
        "id": "d-grace-code-fail",
        "text": "Code-failing direction in grace period",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "auto_recovered_at": recovered_at.isoformat(),
        "research_history": history,
    }
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "paused", (
        "grace period must NOT protect against code-failure rules (Rule 3)"
    )
    assert d.get("auto_paused") is True
    reason = d.get("paused_reason", "")
    assert "High code failure rate" in reason or "Recent stall" in reason, (
        f"paused_reason should indicate a code-failure rule, got: {reason!r}"
    )


# sha256("")[:16] — fingerprint for directions without success_evidence
_EMPTY_SE_FP = "e3b0c44298fc1c14"


def _scaffolding_direction_for_lifecycle(progress_window):
    return {
        "id": "d-scaffold-lc",
        "text": "Outcome-less scaffolding direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [
            {
                "tasks_created": 3,
                "tasks_completed": 3,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 3,
            }
            for _ in range(4)
        ],
        "history_aggregate": {
            "total_cycles": 10,
            "total_prs_merged": 8,
            "total_tasks_completed": 8,
            "total_tasks_failed": 0,
            "total_tasks_failed_infra": 0,
        },
        "outcome_progress_history": [
            {"progress": p, "success_evidence_fp": _EMPTY_SE_FP}
            for p in progress_window
        ],
    }


@pytest.mark.asyncio
async def test_lifecycle_flags_outcome_scaffolding_and_notifies_once(
    db_session, mocker
):
    """Scaffolding direction gets the advisory flag (not paused) and one notification per transition."""
    notify = mocker.patch(
        "app.notification_dispatch.post_direction_outcome_stalled_message",
        new_callable=AsyncMock,
    )
    direction = _scaffolding_direction_for_lifecycle([5, 5, 5, 5, 5, 5])
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)
    d = project.directions[0]
    assert d["status"] == "active", "advisory flag must NOT pause the direction"
    assert d.get("outcome_scaffolding_flagged") is True
    assert d.get("outcome_scaffolding_evidence", {}).get("total_cycles") == 14
    notify.assert_awaited_once()

    # Second pass while still flagged: no second notification (once per transition).
    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)
    assert project.directions[0].get("outcome_scaffolding_flagged") is True
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifecycle_clears_outcome_scaffolding_when_progress_resumes(
    db_session, mocker
):
    """Once the confirmed-finding count grows across the window, the advisory flag is cleared."""
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_stalled_message",
        new_callable=AsyncMock,
    )
    direction = _scaffolding_direction_for_lifecycle([5, 5, 5, 5, 5, 5])
    project = await _make_cost_project(db_session, direction)
    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)
    assert project.directions[0].get("outcome_scaffolding_flagged") is True

    # Progress resumes -> the confirmed-finding count grows across the window,
    # which the detector reads as advancement.
    project.directions[0]["outcome_progress_history"] = [
        {"progress": p} for p in [5, 5, 6, 7, 8, 9]
    ]
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)
    assert project.directions[0].get("outcome_scaffolding_flagged") is None


@pytest.mark.asyncio
async def test_scaffolding_memo_spawned_on_first_flag(db_session, mocker):
    """A memo task is spawned the first time a direction is flagged as outcome-less scaffolding."""
    from unittest.mock import AsyncMock

    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_stalled_message",
        new_callable=AsyncMock,
    )
    direction = _scaffolding_direction_for_lifecycle([5, 5, 5, 5, 5, 5])
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)
    assert project.directions[0].get("outcome_scaffolding_flagged") is True

    memo_tasks = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.created_by == "ORCHESTRATOR",
                )
            )
        )
        .scalars()
        .all()
    )
    scaffolding_memos = [
        t
        for t in memo_tasks
        if isinstance(t.context, dict)
        and t.context.get("memo_reasoning_kind") == "outcome_scaffolding"
    ]
    assert len(scaffolding_memos) == 1, (
        f"expected 1 outcome_scaffolding memo, got {len(scaffolding_memos)}"
    )
    memo = scaffolding_memos[0]
    assert memo.context.get("memo_source_direction_id") == direction["id"]
    assert memo.status == TaskStatus.COMPLETED
    assert (memo.payloads[0].get("topic") or "").startswith("Outcome-less scaffolding:")


@pytest.mark.asyncio
async def test_scaffolding_memo_not_respawned_on_evidence_refresh(db_session, mocker):
    """No additional memo is spawned on subsequent lifecycle passes while the flag is still set."""
    from unittest.mock import AsyncMock

    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_stalled_message",
        new_callable=AsyncMock,
    )
    direction = _scaffolding_direction_for_lifecycle([5, 5, 5, 5, 5, 5])
    project = await _make_cost_project(db_session, direction)

    # First pass: flag is set and memo is spawned
    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)
    assert project.directions[0].get("outcome_scaffolding_flagged") is True

    # Second pass: flag already set, no new memo
    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)

    memo_tasks = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.created_by == "ORCHESTRATOR",
                )
            )
        )
        .scalars()
        .all()
    )
    scaffolding_memos = [
        t
        for t in memo_tasks
        if isinstance(t.context, dict)
        and t.context.get("memo_reasoning_kind") == "outcome_scaffolding"
    ]
    assert len(scaffolding_memos) == 1, (
        f"expected exactly 1 memo (no re-spawn), got {len(scaffolding_memos)}"
    )
    assert scaffolding_memos[0].status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_outcome_scaffolding_memo_completed_at_spawn(db_session, mocker):
    """Outcome-scaffolding memo is immediately COMPLETED at spawn (no VM dispatch).

    The memo task must:
    - be in status COMPLETED (not QUEUED)
    - have exactly one MEMO Artifact row
    - have plan_output['memo']['summary'] containing substantive scaffolding context
    - carry citations with 'direction:<id>' and tags with 'outcome-scaffolding'
    """
    from app.models import Artifact, ArtifactType

    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_stalled_message",
        new_callable=AsyncMock,
    )
    direction = _scaffolding_direction_for_lifecycle([5, 5, 5, 5, 5, 5])
    did = direction["id"]
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)
    assert project.directions[0].get("outcome_scaffolding_flagged") is True

    memo_tasks = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.created_by == "ORCHESTRATOR",
                )
            )
        )
        .scalars()
        .all()
    )
    scaffolding_memos = [
        t
        for t in memo_tasks
        if isinstance(t.context, dict)
        and t.context.get("memo_reasoning_kind") == "outcome_scaffolding"
    ]
    assert len(scaffolding_memos) == 1
    memo = scaffolding_memos[0]

    # Task must be COMPLETED immediately — no VM dispatch
    assert memo.status == TaskStatus.COMPLETED, (
        f"memo task should be COMPLETED at spawn, got {memo.status}"
    )

    # Exactly one MEMO Artifact row must exist
    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == memo.id)))
        .scalars()
        .all()
    )
    memo_artifacts = [a for a in artifacts if a.artifact_type == ArtifactType.MEMO]
    assert len(memo_artifacts) == 1, (
        f"expected 1 MEMO artifact, got {len(memo_artifacts)}"
    )

    # plan_output['memo']['summary'] must contain substantive content
    plan_output = memo.plan_output or {}
    memo_body = plan_output.get("memo", {})
    summary = memo_body.get("summary", "")
    assert summary, "memo summary must be non-empty"
    assert len(summary) > 20, f"memo summary too short: {summary!r}"

    # Citations must include direction reference
    citations = memo_body.get("citations", [])
    assert any(f"direction:{did}" in c for c in citations), (
        f"citations must include 'direction:{did}', got {citations}"
    )

    # Tags must include outcome-scaffolding
    tags = memo_body.get("tags", [])
    assert "outcome-scaffolding" in tags, (
        f"tags must include 'outcome-scaffolding', got {tags}"
    )


# ── Persistence-pressure ladder tests ─────────────────────────────────────────


def _already_flagged_direction(flagged_cycles: int = 0, **extra) -> dict:
    """Direction pre-flagged as outcome-less scaffolding with an existing cycle counter.

    Uses the same scaffolding shape as _scaffolding_direction_for_lifecycle so
    _check_outcome_scaffolding continues to return truthy on each pass (all fps
    are None/identical → stalled success_evidence signal).
    """
    d = _scaffolding_direction_for_lifecycle([5, 5, 5, 5, 5, 5])
    d["outcome_scaffolding_flagged"] = True
    d["outcome_scaffolding_flagged_at"] = _utcnow().isoformat()
    d["outcome_scaffolding_evidence"] = {"total_cycles": 14}
    d["outcome_scaffolding_flagged_cycles"] = flagged_cycles
    # total_cycles = 4 (research_history) + 10 (history_aggregate) = 14.
    # Set last_cycle_count to 13 so the lifecycle pass sees one new cycle (14>13)
    # and advances the counter exactly once — simulating a real completed cycle.
    d["outcome_scaffolding_last_cycle_count"] = 13
    d.update(extra)
    return d


@pytest.mark.asyncio
async def test_scaffolding_flag_counter_increments_on_new_cycle(db_session, mocker):
    """outcome_scaffolding_flagged_cycles increments only when a new research cycle has completed."""
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_stalled_message",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_scaffolding_escalation_message",
        new_callable=AsyncMock,
    )

    # last_cycle_count=13 < total_cycles=14 → one new cycle → counter advances
    direction = _already_flagged_direction(flagged_cycles=1)
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d.get("outcome_scaffolding_flagged_cycles") == 2, (
        f"counter should be 2 after one new cycle, got {d.get('outcome_scaffolding_flagged_cycles')}"
    )
    assert d["status"] == "active", (
        "direction must remain active — only the counter increments"
    )


@pytest.mark.asyncio
async def test_scaffolding_counter_does_not_increment_without_new_cycle(
    db_session, mocker
):
    """outcome_scaffolding_flagged_cycles must NOT advance when no new research cycle completed."""
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_stalled_message",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_scaffolding_escalation_message",
        new_callable=AsyncMock,
    )

    direction = _already_flagged_direction(flagged_cycles=1)
    # Override last_cycle_count to equal current total_cycles (14) — no new cycle
    direction["outcome_scaffolding_last_cycle_count"] = 14
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d.get("outcome_scaffolding_flagged_cycles") == 1, (
        f"counter must not advance when no new cycle completed, got {d.get('outcome_scaffolding_flagged_cycles')}"
    )
    assert d["status"] == "active"


@pytest.mark.asyncio
async def test_scaffolding_legacy_missing_watermark_backfills_without_increment(
    db_session, mocker
):
    """Legacy flagged directions missing the watermark must backfill it without advancing."""
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_stalled_message",
        new_callable=AsyncMock,
    )
    escalate_notify = mocker.patch(
        "app.notification_dispatch.post_direction_outcome_scaffolding_escalation_message",
        new_callable=AsyncMock,
    )

    direction = _already_flagged_direction(flagged_cycles=2)
    direction.pop("outcome_scaffolding_last_cycle_count", None)
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d.get("outcome_scaffolding_flagged_cycles") == 2, (
        "legacy missing watermark must not miscount the current total_cycles as a new cycle"
    )
    assert d.get("outcome_scaffolding_last_cycle_count") == 14, (
        "legacy missing watermark should be backfilled to the current completed-cycle count"
    )
    assert d["status"] == "active"
    escalate_notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_scaffolding_escalation_fires_at_mid_threshold(db_session, mocker):
    """Escalation notification fires when flagged_cycles reaches escalation_cycles (default 3)."""
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_stalled_message",
        new_callable=AsyncMock,
    )
    escalate_notify = mocker.patch(
        "app.notification_dispatch.post_direction_outcome_scaffolding_escalation_message",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.notification_dispatch.post_direction_milestone_message",
        new_callable=AsyncMock,
    )

    # escalation_cycles default = 3; preset counter to 2 so next pass hits 3
    direction = _already_flagged_direction(flagged_cycles=2)
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d.get("outcome_scaffolding_flagged_cycles") == 3
    assert d["status"] == "active", "escalation must not retire the direction"
    escalate_notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_scaffolding_escalation_does_not_refire_without_new_cycle(
    db_session, mocker
):
    """Escalation notification must not repeat on later ticks without a new completed cycle."""
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_stalled_message",
        new_callable=AsyncMock,
    )
    escalate_notify = mocker.patch(
        "app.notification_dispatch.post_direction_outcome_scaffolding_escalation_message",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.notification_dispatch.post_direction_milestone_message",
        new_callable=AsyncMock,
    )

    direction = _already_flagged_direction(flagged_cycles=2)
    project = await _make_cost_project(db_session, direction)
    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)
    assert project.directions[0].get("outcome_scaffolding_flagged_cycles") == 3
    escalate_notify.assert_awaited_once()
    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d.get("outcome_scaffolding_flagged_cycles") == 3, (
        "counter must stay pinned when no new cycle completed after escalation"
    )
    escalate_notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_scaffolding_auto_retires_at_high_threshold(db_session, mocker):
    """Direction is auto-retired when flagged_cycles reaches max_flagged_cycles (default 6).

    Two-tick model: tick 1 sets auto_retire_pending=True (flag-not-execute contract);
    tick 2 designated retire pass executes the retirement and fires the milestone.
    """
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_stalled_message",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_scaffolding_escalation_message",
        new_callable=AsyncMock,
    )
    milestone_notify = mocker.patch(
        "app.notification_dispatch.post_direction_milestone_message",
        new_callable=AsyncMock,
    )

    # max_flagged_cycles default = 6; preset counter to 5 so next pass hits 6
    direction = _already_flagged_direction(flagged_cycles=5)
    did = direction["id"]
    project = await _make_cost_project(db_session, direction)

    # --- Tick 1: flag-not-execute — sets auto_retire_pending, does NOT retire yet.
    active_directions = await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d.get("auto_retire_pending") is True, (
        "tick 1 must set auto_retire_pending=True rather than retiring immediately"
    )
    assert d.get("auto_retire_pending_reason") == "scaffolding"
    assert d["status"] != "completed", (
        "direction must NOT be retired on the flag tick — retirement deferred to next tick"
    )
    milestone_notify.assert_not_awaited()
    # Flagged direction must NOT be returned in active_directions (skip-the-append contract).
    assert did not in {ad.get("id") for ad in active_directions}, (
        "flagged direction must be excluded from active_directions even before retirement"
    )

    # --- Tick 2: designated retire pass executes the retirement.
    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)

    # The direction is retired (status=completed) and immediately pruned from
    # project.directions in the same tick — verify via the gap and milestone.
    remaining_ids = {d["id"] for d in (project.directions or [])}
    assert did not in remaining_ids, (
        "retired direction must be pruned from project.directions after the designated retire pass"
    )
    milestone_notify.assert_awaited_once()
    kwargs = milestone_notify.call_args.kwargs
    assert kwargs.get("milestone") == "auto-retired-scaffolding"
    # Vision memory records the retirement.
    retired_mem = (project.vision or {}).get("_recently_retired_directions") or []
    mem_entry = next((m for m in retired_mem if m.get("id") == did), None)
    assert mem_entry is not None, "retired direction must appear in vision memory"
    assert mem_entry.get("reason") == "auto_retired_scaffolding", (
        f"vision memory reason should be 'auto_retired_scaffolding', got: {mem_entry.get('reason')!r}"
    )


@pytest.mark.asyncio
async def test_scaffolding_flag_clear_resets_counter(db_session, mocker):
    """When the scaffolding condition clears, counter is removed alongside the flag."""
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_stalled_message",
        new_callable=AsyncMock,
    )

    # Grow the confirmed-finding count so _check_outcome_scaffolding returns None
    direction = _already_flagged_direction(flagged_cycles=2)
    direction["outcome_progress_history"] = [
        {"progress": p} for p in [5, 5, 6, 7, 8, 9]
    ]
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d.get("outcome_scaffolding_flagged") is None, (
        "flag should be cleared when scaffolding condition no longer holds"
    )
    assert "outcome_scaffolding_flagged_cycles" not in d, (
        "counter should be removed when flag is cleared"
    )
    assert "outcome_scaffolding_last_cycle_count" not in d, (
        "last_cycle_count watermark should be removed when flag is cleared"
    )


@pytest.mark.asyncio
async def test_scaffolding_stale_verdict_does_not_freeze_counter(db_session, mocker):
    """A stale advancing evidence_progress verdict must NOT freeze the ladder counter.

    A stale advancing verdict (cycle_index far in the past) is old enough that
    _check_outcome_scaffolding still fires (the suppression inside the detector
    requires a RECENT verdict via _evidence_progress_suppresses_scaffolding). The
    persistence-pressure ladder must use the SAME recency-aware helper rather than
    a recency-blind status check, so a stale verdict grants no credit — the counter
    advances, the direction eventually escalates and auto-retires.
    """
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_stalled_message",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_scaffolding_escalation_message",
        new_callable=AsyncMock,
    )

    # cycle_index=1 with total_cycles=14 → 13 cycles stale → exceeds stall_run(6)
    # so _evidence_progress_suppresses_scaffolding returns False inside BOTH the
    # detector AND the ladder, meaning the counter must increment.
    direction = _already_flagged_direction(
        flagged_cycles=1,
        evidence_progress={"status": "advancing", "cycle_index": 1},
    )
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d.get("outcome_scaffolding_flagged_cycles") == 2, (
        "counter MUST increment when evidence_progress verdict is stale (recency check fails)"
    )
    assert d.get("outcome_scaffolding_flagged") is True, (
        "flag must remain set (stale advancing does not clear the scaffolding flag)"
    )


# ── Contract-reset and fingerprint-filter unit tests ──────────────────────────


def test_reset_outcome_detection_state_clears_all_keys():
    """_reset_outcome_detection_state empties history and removes all outcome-detection fields."""
    from app.direction_lifecycle import _reset_outcome_detection_state

    d = {
        "status": "active",
        "outcome_progress_history": [{"progress": 5, "success_evidence_fp": "abc"}],
        "outcome_progress_ever_met": True,
        "evidence_progress": {"status": "advancing"},
        "possibly_complete": True,
        "completion_suggestion": "done",
        "outcome_scaffolding_flagged": True,
        "outcome_scaffolding_flagged_at": "2026-01-01T00:00:00",
        "outcome_scaffolding_flagged_cycles": 3,
    }
    _reset_outcome_detection_state(d)

    assert d["outcome_progress_history"] == [], "history must be emptied (not removed)"
    for key in (
        "outcome_progress_ever_met",
        "evidence_progress",
        "possibly_complete",
        "completion_suggestion",
        "outcome_scaffolding_flagged",
        "outcome_scaffolding_flagged_at",
        "outcome_scaffolding_flagged_cycles",
    ):
        assert key not in d, f"{key} should be removed by reset"
    assert d["status"] == "active", "non-outcome keys must survive the reset"


def test_check_outcome_scaffolding_filters_stale_contract_snapshots():
    """Snapshots with an old success_evidence_fp are excluded from the stall window.

    The detector must return None when all snapshots pre-date the current contract,
    then fire once enough new-contract snapshots accumulate.
    """
    import hashlib
    from app.strategic_completion import _check_outcome_scaffolding

    old_fp = hashlib.sha256(b"old contract").hexdigest()[:16]
    new_se = "new success evidence"
    new_fp = hashlib.sha256(new_se.encode()).hexdigest()[:16]

    direction = {
        "id": "d-fp-filter",
        "text": "test",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "context": {"success_evidence": new_se},
        "research_history": [
            {
                "tasks_created": 3,
                "tasks_completed": 3,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 3,
            }
            for _ in range(14)
        ],
        "history_aggregate": {
            "total_cycles": 10,
            "total_prs_merged": 8,
            "total_tasks_completed": 8,
            "total_tasks_failed": 0,
            "total_tasks_failed_infra": 0,
        },
        # All snapshots carry the OLD fingerprint — none match new contract.
        "outcome_progress_history": [
            {"progress": 5, "success_evidence_fp": old_fp} for _ in range(6)
        ],
    }
    assert _check_outcome_scaffolding(direction) is None, (
        "old-contract snapshots must not fire the detector"
    )

    # Replace with new-contract snapshots — detector should now fire.
    direction["outcome_progress_history"] = [
        {"progress": 5, "success_evidence_fp": new_fp} for _ in range(6)
    ]
    assert _check_outcome_scaffolding(direction) is not None, (
        "new-contract snapshots must fire the detector once stall window is full"
    )


# ── Additional contract-reset unit tests ──────────────────────────────────────


def test_reset_outcome_detection_state_clears_completion_suggestion():
    """_reset_outcome_detection_state specifically removes the completion_suggestion field.

    completion_suggestion is the least-frequently-tested of the 8 outcome-detection
    keys.  Verify it is removed (not left as an empty string or None) so a stale
    advisory is never surfaced to operators after a contract rewrite.
    """
    from app.direction_lifecycle import _reset_outcome_detection_state

    d = {
        "status": "active",
        "text": "Improve auth coverage",
        "completion_suggestion": "All SAML tests green — direction may be done",
        "outcome_progress_history": [{"progress": 4, "success_evidence_fp": "fp-old"}],
        "outcome_progress_ever_met": True,
    }
    _reset_outcome_detection_state(d)

    assert "completion_suggestion" not in d, (
        "completion_suggestion must be removed, not left as empty value"
    )
    assert d["outcome_progress_history"] == [], "history must be emptied, not removed"
    assert "outcome_progress_ever_met" not in d
    assert d["status"] == "active", "non-outcome fields must survive the reset"
    assert d["text"] == "Improve auth coverage", "text must survive the reset"


# ── Already-fixed deadlock detection ─────────────────────────────────────────


def test_detect_already_fixed_deadlock_insufficient_window():
    from app.research_direction_state import _detect_already_fixed_deadlock

    history = [
        {"tasks_created": 1, "scope_empty_noop_count": 1},
        {"tasks_created": 1, "scope_empty_noop_count": 1},
    ]
    deadlocked, consec = _detect_already_fixed_deadlock(history, window=3)
    assert not deadlocked
    assert consec == 0


def test_detect_already_fixed_deadlock_tasks_created_zero_not_counted():
    from app.research_direction_state import _detect_already_fixed_deadlock

    # tasks_created=0 entries are not eligible (dedup-deadlock territory)
    history = [
        {"tasks_created": 0, "scope_empty_noop_count": 0},
        {"tasks_created": 0, "scope_empty_noop_count": 0},
        {"tasks_created": 0, "scope_empty_noop_count": 0},
    ]
    deadlocked, consec = _detect_already_fixed_deadlock(history)
    assert not deadlocked
    assert consec == 0


def test_detect_already_fixed_deadlock_partial_noop_not_enough():
    from app.research_direction_state import _detect_already_fixed_deadlock

    # scope_empty_noop_count < tasks_created — not all tasks were SCOPE_EMPTY
    history = [
        {"tasks_created": 2, "scope_empty_noop_count": 1},
        {"tasks_created": 2, "scope_empty_noop_count": 1},
        {"tasks_created": 2, "scope_empty_noop_count": 1},
    ]
    deadlocked, consec = _detect_already_fixed_deadlock(history)
    assert not deadlocked
    assert consec == 0


def test_detect_already_fixed_deadlock_fires_on_three_consecutive():
    from app.research_direction_state import _detect_already_fixed_deadlock

    history = [
        {"tasks_created": 1, "scope_empty_noop_count": 1},
        {"tasks_created": 2, "scope_empty_noop_count": 2},
        {"tasks_created": 1, "scope_empty_noop_count": 1},
    ]
    deadlocked, consec = _detect_already_fixed_deadlock(history)
    assert deadlocked
    assert consec == 3


def test_detect_already_fixed_deadlock_breaks_on_merged_pr():
    from app.research_direction_state import _detect_already_fixed_deadlock

    # Middle entry has scope_empty_noop_count < tasks_created (a PR was merged)
    history = [
        {"tasks_created": 1, "scope_empty_noop_count": 1},
        {"tasks_created": 2, "scope_empty_noop_count": 0},  # prs_merged > 0
        {"tasks_created": 1, "scope_empty_noop_count": 1},
        {"tasks_created": 1, "scope_empty_noop_count": 1},
    ]
    deadlocked, consec = _detect_already_fixed_deadlock(history)
    # Only last 2 entries are consecutive noops (middle entry breaks the run)
    assert not deadlocked
    assert consec == 0


def test_detect_already_fixed_deadlock_stops_at_legacy_entry():
    from app.research_direction_state import _detect_already_fixed_deadlock

    # Legacy entries (no scope_empty_noop_count key) act as window breaks
    history = [
        {"tasks_created": 1},  # legacy — no scope_empty_noop_count
        {"tasks_created": 1, "scope_empty_noop_count": 1},
        {"tasks_created": 1, "scope_empty_noop_count": 1},
        {"tasks_created": 1, "scope_empty_noop_count": 1},
    ]
    # The legacy entry stops the consecutive count even though 3 new-style entries exist
    # (they are not consecutive from the end due to the break at the legacy entry)
    deadlocked, consec = _detect_already_fixed_deadlock(history)
    assert deadlocked
    assert consec == 3


@pytest.mark.asyncio
async def test_already_fixed_deadlock_auto_completes_via_apply_post_spawn(db_session):
    """#10113 superseded: an already-fixed-deadlocked direction completes terminally
    via apply_post_spawn_state instead of the old alert-only no-op.

    Three prior cycles whose spawned tasks all resolved SCOPE_EMPTY (work already on
    HEAD) make the direction genuinely exhausted. When a fresh no-findings cycle is
    applied and the direction is neither code-failing nor externally frozen, it is
    routed to terminal status='completed' with an exhaustion reason and an INFO
    'Direction auto-completed' alert.
    """
    from app.models import Alert
    from app.research_direction_state import apply_post_spawn_state
    from test_autonomous_research import _active_direction, _make_research_task

    direction_id = "d-af-complete"
    # Prior cycles: tasks_created >= 1 and scope_empty_noop_count >= tasks_created
    # (already-fixed deadlock signal), zero merges.
    noop_history = [
        {
            "timestamp": (_utcnow() - timedelta(hours=h)).isoformat(),
            "task_id": f"t-af-{h}",
            "tasks_created": 2,
            "scope_empty_noop_count": 2,
            "prs_merged": 0,
            "filter_breakdown": {
                "hash_dedup": 0, "word_overlap_dedup": 0,
                "file_overlap_dedup": 0, "evidence_overlap_dedup": 0,
                "type_filtered": 0, "empty_prompt": 0,
            },
            "summary": "all tasks no-op — work already on HEAD",
        }
        for h in (15, 10, 5)
    ]
    direction = {
        **_active_direction(direction_id, "Already-fixed deadlock completion"),
        "research_history": noop_history,
    }
    project = Project(
        name="AF Deadlock Complete Test",
        user_id="u-af-complete",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    # Fresh no-findings cycle — the appended entry is excluded by the af site's
    # history[:-1] detection, which still sees the 3 SCOPE_EMPTY cycles.
    await apply_post_spawn_state(
        db_session,
        task,
        [],  # findings
        [],  # child_tasks
        [],  # deduplicated
        [],  # insights
        [],  # direction_notes
        [],  # direction_proposals
        0, 0, 0, 0, 0, 0,
        "research-branch", "main", 0, None,
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "completed", (
        f"already-fixed-deadlocked direction should be terminally completed, got {d['status']!r}"
    )
    assert d.get("auto_completed") is True, "auto_completed must be set"
    assert d.get("completed_at") is not None, "completed_at must be stamped"
    assert "exhausted" in (d.get("auto_completed_reason") or ""), (
        f"auto_completed_reason must reflect exhaustion, got {d.get('auto_completed_reason')!r}"
    )

    result = await db_session.execute(
        select(Alert).where(
            Alert.project_id == project.id,
            Alert.title == "Direction auto-completed",
            Alert.dismissed_at.is_(None),
        )
    )
    assert result.scalars().first() is not None, (
        "expected a 'Direction auto-completed' alert"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Crash-guard: _directed_research_recently_completed folded into lifecycle
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lifecycle_skips_direction_with_recently_completed_research(
    db_session, mocker
):
    """A direction with no refractory_until but a recently-COMPLETED directed
    research task is excluded from active_directions by
    _process_direction_lifecycle_locked (crash guard)."""
    from app.shared_state import runtime_config as _runtime_config

    direction_id = "d-crash-guard-01"
    _runtime_config["completed_research_cooldown_minutes"] = 120

    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )

    try:
        direction = {
            "id": direction_id,
            "text": "Crash guard direction",
            "status": "active",
            "priority": "medium",
            "paths": [],
            "added_at": "2026-01-01T00:00:00",
            # No refractory_until — Gate 1 would allow this direction
        }
        project = Project(
            name="Crash Guard Test",
            user_id="u-crash-guard",
            autonomous_enabled=True,
            directions=[direction],
            vision={"purpose": "test"},
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Insert a COMPLETED research task for this direction within the cooldown window
        completed_task = Task(
            project_id=project.id,
            title=f"Research: direction-{direction_id}",
            description="x",
            payloads=[
                {
                    "type": "research",
                    "current_direction_id": direction_id,
                    "directions": [],
                    "base_branch": "main",
                }
            ],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
        db_session.add(completed_task)
        await db_session.commit()

        active = await _process_direction_lifecycle_locked(
            project, db_session, set(), _utcnow()
        )
        assert active == [], (
            "Direction with a recently-completed research task must be excluded "
            "from active_directions by the crash guard"
        )
    finally:
        _runtime_config.pop("completed_research_cooldown_minutes", None)


# ─────────────────────────────────────────────────────────────────────────────
# Zombie-direction auto-retire ladder (4th ladder, ref knowledge bf6d8788)
# ─────────────────────────────────────────────────────────────────────────────


def _unproductive_history(now: datetime, cycles: int = 5) -> list[dict]:
    """research_history that trips Rule 2 (Unproductive: 0 PRs merged).

    Each cycle finishes real work (tasks_completed > 0) but merges nothing, and
    the last cycle has no tasks in flight, so _check_direction_health returns an
    'Unproductive:' pause reason.
    """
    return [
        {
            "timestamp": (now - timedelta(days=cycles - i)).isoformat(),
            "tasks_created": 2,
            "tasks_completed": 2,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": 0,
        }
        for i in range(cycles)
    ]


@pytest.mark.asyncio
async def test_zombie_churn_threshold_recommends_retirement_without_completion(
    db_session, mocker
):
    """Acceptance selector: a churn/unproductive direction already paused once
    (churn_family_pause_count=1) that re-trips a churn-family pause crosses the
    zombie threshold — but is NO LONGER terminally auto-retired. Instead it stays
    PAUSED and non-terminal, carries a strategist-reviewable retirement
    recommendation with bounded evidence, preserves its gap staffing, and writes
    no retired-memory. A still-productive direction under the same count is
    untouched."""
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()

    zombie = {
        "id": "d-zombie",
        "text": "Recycling zombie direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": _unproductive_history(now),
        "churn_family_pause_count": 1,  # already paused once before
        "gap_id": "g-zombie",
    }
    # A genuinely productive direction with the same churn count must NOT retire —
    # last_cycle_productive PLUS a real productivity signal (its most-recent cycle
    # completed a genuine no-op) shields it from the churn-family pause. (Under the
    # signal-that-lies fix, mere spawning no longer holds the shield.)
    healthy_history = _unproductive_history(now)
    healthy_history[-1]["tasks_completed_no_pr"] = 1
    healthy = {
        "id": "d-healthy",
        "text": "Still-productive direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": healthy_history,
        "churn_family_pause_count": 1,
        "last_cycle_productive": True,
        "consecutive_unproductive_cycles": 0,
    }

    project = Project(
        name="Zombie Retire Test",
        user_id="u-zombie",
        autonomous_enabled=True,
        directions=[zombie, healthy],
        vision={"purpose": "test"},
        problem_model={
            "gaps": [
                {
                    "id": "g-zombie",
                    "name": "Zombie gap",
                    "status": "active",
                    "active_moves": ["d-zombie"],
                    "past_moves": [],
                }
            ]
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project)

    by_id = {d["id"]: d for d in project.directions}
    z = by_id["d-zombie"]
    # NON-TERMINAL: crossing the churn threshold must NOT complete/auto_complete.
    assert z["status"] == "paused", (
        "zombie direction must stay paused (non-terminal), not be auto-retired"
    )
    assert z.get("auto_completed") is not True
    assert z.get("completed_at") is None
    assert z.get("churn_family_pause_count") == 2, (
        "churn_family_pause_count should increment on the threshold-crossing pause"
    )
    # A reviewable retirement recommendation is recorded with bounded evidence.
    assert z.get("churn_retirement_recommended") is True, (
        "crossing the threshold must flag a reviewable retirement recommendation"
    )
    assert z.get("churn_retirement_recommended_at")
    ev = z.get("churn_retirement_evidence") or {}
    assert ev.get("churn_family_pause_count") == 2
    assert ev.get("threshold") == 2
    assert isinstance(ev.get("summary"), str) and ev["summary"]
    # Flagged for an operator/strategist decision, but not terminally retired.
    assert z.get("needs_manual_intervention") is True
    # Staffing link PRESERVED on the direction: gap_id stays intact so the
    # direction re-staffs its gap on reactivation (terminal retire dropped the row).
    assert z.get("gap_id") == "g-zombie"

    # Memory state PRESERVED: no retired-memory entry written (the terminal retire
    # path recorded one; a recommendation must not).
    retired_memory = (project.vision or {}).get("_recently_retired_directions") or []
    assert not any(m.get("id") == "d-zombie" for m in retired_memory), (
        "a recommendation must not write retired-direction memory"
    )

    # NON-TERMINAL: the direction survives the next prune tick (a completed
    # direction would be removed), so it stays available to reactivate/retire.
    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now + timedelta(hours=1)
    )
    await db_session.refresh(project)
    surviving = {d["id"]: d for d in project.directions}
    assert "d-zombie" in surviving, (
        "a recommended-but-not-retired direction must survive the prune tick"
    )
    assert surviving["d-zombie"]["status"] == "paused"
    assert surviving["d-zombie"].get("churn_retirement_recommended") is True

    h = surviving["d-healthy"]
    assert h["status"] != "completed", (
        "a still-productive direction must not be flagged by the zombie ladder"
    )
    assert not h.get("auto_completed")
    assert not h.get("churn_retirement_recommended")


async def _make_gap_project(db_session, direction: dict, problem_model: dict) -> Project:
    """A _make_cost_project variant that also seeds a problem_model so the
    terminal auto-retire gap reconciliation can be exercised."""
    project = Project(
        name="Gap Reconcile Test",
        user_id="u-gap-reconcile",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
        problem_model=problem_model,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)
    return project


@pytest.mark.asyncio
async def test_code_failure_auto_retire_reconciles_gap(db_session, mocker):
    """The code-failure auto-retire ladder must prune the now-completed direction
    out of its gap's active_moves and archive it in past_moves — the same terminal
    invariant the zombie/strategic retires preserve."""
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()
    # >70% code-failure rate (Rule 3) re-trips a code-failure pause this tick;
    # code_failure_pause_count preset to 2 → increments to 3 → hits the retire
    # threshold (_MAX_AUTO_PAUSE_CYCLES = 3).
    history = [
        {
            "timestamp": (now - timedelta(days=i)).isoformat(),
            "tasks_created": 3,
            "tasks_completed": 0,
            "tasks_failed": 3,
            "tasks_failed_infra": 0,
            "prs_merged": 0,
        }
        for i in range(3, 0, -1)
    ]
    direction = {
        "id": "d-codefail",
        "text": "Direction whose tasks keep crashing",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": history,
        "code_failure_pause_count": 2,
        "gap_id": "g-codefail",
    }
    problem_model = {
        "gaps": [
            {
                "id": "g-codefail",
                "name": "Code-failure gap",
                "status": "active",
                "active_moves": ["d-codefail"],
                "past_moves": [],
            }
        ]
    }
    project = await _make_gap_project(db_session, direction, problem_model)

    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "completed", "code-failure ladder should auto-retire"
    assert d.get("auto_completed") is True
    assert "code-failure" in (d.get("auto_completed_reason") or "").lower()

    gap = project.problem_model["gaps"][0]
    assert "d-codefail" not in gap["active_moves"], (
        "retired direction must be pruned from the gap's active_moves"
    )
    assert "d-codefail" in gap["past_moves"], (
        "retired direction must be archived in the gap's past_moves"
    )
    # Last active move removed with no prior deferral → a GapDeferral is recorded.
    assert gap.get("deferral") is not None, (
        "emptying active_moves on retire should record a GapDeferral"
    )


@pytest.mark.asyncio
async def test_scaffolding_auto_retire_reconciles_gap_and_persists(db_session, mocker):
    """The outcome-scaffolding auto-retire branch must (a) prune the completed
    direction out of its gap's active_moves AND (b) persist the auto_retire_pending
    flag across a session refresh — even on a tick where the flagged-cycles counter
    does NOT advance (the flag-not-execute persistence contract).

    Two-tick model: tick 1 sets auto_retire_pending=True (persisted); tick 2
    designated retire pass reconciles the gap and removes the direction.
    """
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_stalled_message",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_scaffolding_escalation_message",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.notification_dispatch.post_direction_milestone_message",
        new_callable=AsyncMock,
    )
    # flagged_cycles already at the default retire threshold (6) and a watermark
    # far above total_cycles so the counter does NOT advance this tick. The key
    # invariant: even when the counter doesn't increment, auto_retire_pending is
    # set and persisted (directions_modified=True on the flag tick).
    direction = _already_flagged_direction(
        flagged_cycles=6,
        outcome_scaffolding_last_cycle_count=999,
    )
    did = direction["id"]
    direction["gap_id"] = "g-scaffold"
    problem_model = {
        "gaps": [
            {
                "id": "g-scaffold",
                "name": "Scaffolding gap",
                "status": "active",
                "active_moves": [did],
                "past_moves": [],
            }
        ]
    }
    project = await _make_gap_project(db_session, direction, problem_model)

    # --- Tick 1: sets auto_retire_pending=True, persisted via directions_modified.
    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=_utcnow()
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d.get("auto_retire_pending") is True, (
        "scaffolding retire must persist auto_retire_pending=True flag across refresh "
        "(even when the counter did not advance this tick)"
    )
    assert d["status"] != "completed", (
        "retirement must be deferred to the next tick via the designated retire pass"
    )

    # --- Tick 2: designated retire pass executes the retirement and reconciles the gap.
    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=_utcnow()
    )
    await db_session.refresh(project)

    # Direction retired and pruned; verify via gap and vision memory.
    remaining_ids = {d["id"] for d in (project.directions or [])}
    assert did not in remaining_ids, (
        "retired direction must be pruned from project.directions after the designated retire pass"
    )

    # (a) Gap reconciled out of active_moves and archived in past_moves.
    gap = project.problem_model["gaps"][0]
    assert did not in gap["active_moves"], (
        "retired direction must be pruned from the gap's active_moves"
    )
    assert did in gap["past_moves"], (
        "retired direction must be archived in the gap's past_moves"
    )


@pytest.mark.asyncio
async def test_churn_recommendation_preserves_gap_staffing(db_session, mocker):
    """The churn-threshold recommendation is non-terminal, so it must PRESERVE the
    direction's gap staffing — the direction stays in the gap's active_moves and
    is not archived into past_moves (unlike the terminal code-failure/lifetime-burn
    retire ladders, which still reconcile the gap)."""
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()
    zombie = {
        "id": "d-zombie-stale",
        "text": "Recycling zombie with stale gap link",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": _unproductive_history(now),
        "churn_family_pause_count": 1,
    }
    problem_model = {
        "gaps": [
            {
                "id": "g-orphan",
                "name": "Gap still staffing the zombie by id",
                "status": "active",
                "active_moves": ["d-zombie-stale"],
                "past_moves": [],
            }
        ]
    }
    project = await _make_gap_project(db_session, zombie, problem_model)

    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "paused", "churn threshold recommends, does not retire"
    assert d.get("churn_retirement_recommended") is True

    gap = project.problem_model["gaps"][0]
    assert "d-zombie-stale" in gap["active_moves"], (
        "staffing must be preserved — the direction stays in active_moves"
    )
    assert "d-zombie-stale" not in gap["past_moves"]


@pytest.mark.asyncio
async def test_zombie_not_retired_on_first_churn_pause(db_session, mocker):
    """A fresh direction's FIRST churn-family pause increments the counter to 1
    and pauses — but does NOT retire (>=2 fence)."""
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()
    direction = {
        "id": "d-first-pause",
        "text": "First churn pause",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": _unproductive_history(now),
    }
    project = Project(
        name="First Pause Test",
        user_id="u-first",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "paused", "first churn pause should pause, not retire"
    assert d.get("auto_completed") is not True
    assert d.get("churn_family_pause_count") == 1
    assert d.get("paused_reason", "").startswith("Unproductive:")


@pytest.mark.asyncio
async def test_code_failure_pause_does_not_increment_churn_count(db_session, mocker):
    """A code-failure pause (Rules 3/4) increments code_failure_pause_count but
    leaves churn_family_pause_count untouched — the two ladders are disjoint."""
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()
    # 3 recent cycles with >70% code failures → Rule 3 'High code failure rate:'.
    direction = {
        "id": "d-codefail",
        "text": "Code-failing direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "last_cycle_productive": True,
        "consecutive_unproductive_cycles": 0,
        "research_history": [
            {
                "timestamp": (now - timedelta(days=i)).isoformat(),
                "tasks_created": 3,
                "tasks_completed": 0,
                "tasks_failed": 3,
                "tasks_failed_infra": 0,
                "prs_merged": 0,
            }
            for i in range(3, 0, -1)
        ],
    }
    project = Project(
        name="Code Fail Test",
        user_id="u-codefail",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d.get("paused_reason", "").startswith("High code failure rate:")
    assert d.get("code_failure_pause_count") == 1
    assert d.get("churn_family_pause_count", 0) == 0, (
        "code-failure pause must not feed the zombie/churn ladder"
    )


@pytest.mark.asyncio
async def test_churn_pause_count_preserved_on_task_creating_cycle(db_session):
    """apply_post_spawn_state must NOT reset churn_family_pause_count when a
    direction merely creates child tasks.

    Creating tasks is not the same as shipping merged PRs — unproductive/churning
    directions routinely spawn tasks that never merge. The old reset-on-task-
    creation behavior let a churn-paused (counter=1) direction wipe its counter
    before the next lifecycle tick could advance it to the >=2 auto-retire fence,
    freezing the zombie ladder one short of retirement. The counter is now only
    incremented by direction_lifecycle.py and never reset externally, so it stays
    unchanged across a task-creating (non-merging) cycle."""
    from app.research_direction_state import apply_post_spawn_state
    from test_autonomous_research import _active_direction, _make_research_task

    direction_id = "d-reset"
    direction = {
        **_active_direction(direction_id, "Recovered direction"),
        "churn_family_pause_count": 1,
    }
    project = Project(
        name="Churn Reset Test",
        user_id="u-reset",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    child = Task(
        project_id=project.id,
        title="impl child",
        payloads=[{"type": "create", "base_branch": "main"}],
        context={"direction_id": direction_id},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)

    await apply_post_spawn_state(
        db_session,
        task,
        [],  # findings
        [child],  # child_tasks — productive cycle
        [],  # deduplicated
        [],  # insights
        [],  # direction_notes
        [],  # direction_proposals
        0, 0, 0, 0, 0, 0,
        "research-branch", "main", 0, None,
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d.get("churn_family_pause_count", 0) == 1, (
        "a task-creating (non-merging) cycle must NOT reset the zombie churn-pause "
        "counter — it stays at its pre-cycle value (1) so the retire ladder can "
        "still reach the >=2 fence"
    )


def _lifetime_burn_history(now: datetime) -> tuple[list[dict], dict]:
    """research_history + history_aggregate that trips the lifetime burn backstop
    (Rule 6) but NOT the churn guardrail (Rule 5) or unproductive (Rule 2).

    Recent window: 3 cycles, 10 tasks each, 2 merges per cycle = 20% merge rate.
    Rule 5 threshold needs <10% rate, so it doesn't fire.
    Aggregate: 136 non-merge tasks, 10 merges → combined lifetime merge rate ~9.6%
    which is below the 10% threshold → trips Rule 6 (lifetime burn backstop).
    """
    history = [
        {
            "timestamp": (now - timedelta(days=3 - i)).isoformat(),
            "tasks_created": 10,
            "tasks_completed": 10,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": 2,
        }
        for i in range(3)
    ]
    aggregate = {
        "total_cycles": 10,
        "total_tasks_created": 136,
        "total_tasks_completed": 136,
        "total_tasks_failed": 0,
        "total_tasks_failed_infra": 0,
        "total_prs_merged": 10,
    }
    return history, aggregate


@pytest.mark.asyncio
async def test_lifetime_burn_manual_hold_auto_retires_after_bound(db_session, mocker):
    """Acceptance test: a direction that trips the lifetime burn backstop for the
    second time (_MAX_LIFETIME_BURN_PAUSE_CYCLES=2) is auto-retired on that same
    lifecycle tick rather than accumulating another manual hold.

    Verifies:
    - status=completed, auto_completed=True
    - auto_completed_reason mentions 'lifetime burn'
    - needs_manual_intervention NOT set (cleared on retirement)
    - completed_at is set
    - gap is reconciled (pruned from active_moves)
    """
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()
    history, aggregate = _lifetime_burn_history(now)

    # Direction already paused once for lifetime burn (count=1, threshold=2).
    direction = {
        "id": "d-lb-retire",
        "text": "Structurally stuck lifetime burn direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": history,
        "history_aggregate": aggregate,
        "lifetime_burn_pause_count": 1,  # already paused once — next pause retires
        "gap_id": "g-lb-retire",
    }

    project = Project(
        name="Lifetime Burn Retire Test",
        user_id="u-lb-retire",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
        problem_model={
            "gaps": [
                {
                    "id": "g-lb-retire",
                    "name": "LB retire gap",
                    "status": "active",
                    "active_moves": ["d-lb-retire"],
                    "past_moves": [],
                }
            ]
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "completed", (
        "direction should be auto-retired after hitting _MAX_LIFETIME_BURN_PAUSE_CYCLES"
    )
    assert d.get("auto_completed") is True
    assert "lifetime burn" in (d.get("auto_completed_reason") or "").lower(), (
        f"auto_completed_reason should mention 'lifetime burn', got: {d.get('auto_completed_reason')!r}"
    )
    assert d.get("completed_at") is not None
    assert d.get("lifetime_burn_pause_count") == 2, (
        "lifetime_burn_pause_count should reflect the retiring pause event"
    )
    # Manual-hold flags cleared on retirement
    assert not d.get("needs_manual_intervention"), (
        "needs_manual_intervention should be cleared when direction is auto-retired"
    )

    # Gap reconciled: direction pruned from active_moves, recorded in past_moves.
    gap = project.problem_model["gaps"][0]
    assert "d-lb-retire" not in gap["active_moves"], (
        "retired direction should be removed from gap active_moves"
    )
    assert "d-lb-retire" in gap["past_moves"], (
        "retired direction should be recorded in gap past_moves"
    )


@pytest.mark.asyncio
async def test_lifetime_burn_retire_stale_gap_id(db_session, mocker):
    """Regression: lifetime-burn auto-retire must remove the direction from
    active_moves even when the direction's gap_id is stale (points to a
    different gap than where it is actually staffed) or missing entirely.

    The old code only ran reconcile when gap_id matched the staffing gap;
    the new code uses _reconcile_retired_gap which matches by direction id
    across ALL gaps — so a stale or absent gap_id is not a silencing no-op.
    """
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()
    history, aggregate = _lifetime_burn_history(now)

    # Sub-case 1: gap_id points to a *different* gap than where the direction
    # is actually staffed.
    direction_stale = {
        "id": "d-lb-stale-gap",
        "text": "Stale gap_id lifetime burn direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": history,
        "history_aggregate": aggregate,
        "lifetime_burn_pause_count": 1,
        "gap_id": "g-wrong-gap",  # stale — direction is actually in g-real-gap
    }

    project_stale = Project(
        name="LB Stale GapId Test",
        user_id="u-lb-stale",
        autonomous_enabled=True,
        directions=[direction_stale],
        vision={"purpose": "test"},
        problem_model={
            "gaps": [
                {
                    "id": "g-real-gap",
                    "name": "Real gap (direction is actually here)",
                    "status": "active",
                    "active_moves": ["d-lb-stale-gap"],
                    "past_moves": [],
                },
                {
                    "id": "g-wrong-gap",
                    "name": "Wrong gap (gap_id points here but direction isn't)",
                    "status": "active",
                    "active_moves": [],
                    "past_moves": [],
                },
            ]
        },
    )
    db_session.add(project_stale)
    await db_session.commit()
    await db_session.refresh(project_stale)
    _ss.circuit_breakers.pop(str(project_stale.id), None)

    await _process_direction_lifecycle(
        project_stale, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project_stale)

    d_stale = project_stale.directions[0]
    assert d_stale["status"] == "completed", (
        "direction should be auto-retired even with stale gap_id"
    )
    real_gap = next(
        g for g in project_stale.problem_model["gaps"] if g["id"] == "g-real-gap"
    )
    assert "d-lb-stale-gap" not in real_gap["active_moves"], (
        "retired direction must be removed from the gap it was actually staffing, "
        "regardless of stale gap_id on the direction dict"
    )
    assert "d-lb-stale-gap" in real_gap["past_moves"], (
        "retired direction should appear in past_moves of the real gap"
    )

    # Sub-case 2: direction has no gap_id at all but is staffing a gap.
    history2, aggregate2 = _lifetime_burn_history(now)
    direction_no_gap = {
        "id": "d-lb-no-gap-id",
        "text": "Missing gap_id lifetime burn direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": history2,
        "history_aggregate": aggregate2,
        "lifetime_burn_pause_count": 1,
        # No gap_id field at all
    }

    project_no_gap = Project(
        name="LB No GapId Test",
        user_id="u-lb-no-gap",
        autonomous_enabled=True,
        directions=[direction_no_gap],
        vision={"purpose": "test"},
        problem_model={
            "gaps": [
                {
                    "id": "g-no-gap-id-gap",
                    "name": "Gap staffed without gap_id on direction",
                    "status": "active",
                    "active_moves": ["d-lb-no-gap-id"],
                    "past_moves": [],
                }
            ]
        },
    )
    db_session.add(project_no_gap)
    await db_session.commit()
    await db_session.refresh(project_no_gap)
    _ss.circuit_breakers.pop(str(project_no_gap.id), None)

    await _process_direction_lifecycle(
        project_no_gap, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project_no_gap)

    d_no_gap = project_no_gap.directions[0]
    assert d_no_gap["status"] == "completed", (
        "direction should be auto-retired even without any gap_id"
    )
    gap_no_id = project_no_gap.problem_model["gaps"][0]
    assert "d-lb-no-gap-id" not in gap_no_id["active_moves"], (
        "retired direction must be removed from active_moves when gap_id is absent"
    )
    assert "d-lb-no-gap-id" in gap_no_id["past_moves"], (
        "retired direction should appear in past_moves even without gap_id"
    )


@pytest.mark.asyncio
async def test_lifetime_burn_no_retire_below_threshold(db_session, mocker):
    """First lifetime-burn pause (count absent) increments to 1 and sets
    needs_manual_intervention=True, but does NOT auto-retire the direction."""
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()
    history, aggregate = _lifetime_burn_history(now)

    direction = {
        "id": "d-lb-first",
        "text": "First lifetime burn pause direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": history,
        "history_aggregate": aggregate,
        # No lifetime_burn_pause_count yet — this is the first pause.
    }

    project = Project(
        name="Lifetime Burn First Pause Test",
        user_id="u-lb-first",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "paused", (
        "direction should be paused (not retired) on first lifetime burn pause"
    )
    assert d.get("lifetime_burn_pause_count") == 1, (
        "lifetime_burn_pause_count should be 1 after first burn pause"
    )
    assert d.get("needs_manual_intervention") is True, (
        "needs_manual_intervention should be set on the first burn pause"
    )
    assert not d.get("auto_completed"), (
        "direction should NOT be auto-retired below the threshold"
    )


@pytest.mark.asyncio
async def test_lifetime_burn_auto_retire_full_path(db_session, mocker):
    """End-to-end lifetime-burn retire driven entirely by real lifecycle ticks,
    starting from counter=0 (NO hand-set lifetime_burn_pause_count).

    This is the regression guard for the freeze-at-1 bug: lifetime-burn pauses set
    needs_manual_intervention, which blocks the time-based auto-recovery branch, so
    the direction can never re-enter the active loop where the counter used to be
    incremented — it permanently stalled at 1 and the retire fence was unreachable.

    Tick 1 (active): _check_direction_health returns a lifetime-burn pause reason →
    counter advances 0->1, needs_manual_intervention set, status->paused.
    Tick 2 (paused, past the 48h recovery window): the auto-recovery extend-pause
    branch — the ONLY place a manual-held burn pause is re-touched — advances the
    counter 1->2, hits _MAX_LIFETIME_BURN_PAUSE_CYCLES, and auto-retires the
    direction with ZERO operator action.
    """
    from sqlalchemy.orm.attributes import flag_modified

    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()
    history, aggregate = _lifetime_burn_history(now)

    direction = {
        "id": "d-lb-e2e",
        "text": "Lifetime-burn stuck direction (full path)",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": history,
        "history_aggregate": aggregate,
        "gap_id": "g-lb-e2e",
        # No lifetime_burn_pause_count — start from 0 and let the machine advance it.
    }
    project = Project(
        name="Lifetime Burn Full Path Test",
        user_id="u-lb-e2e",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
        problem_model={
            "gaps": [
                {
                    "id": "g-lb-e2e",
                    "name": "LB e2e gap",
                    "status": "active",
                    "active_moves": ["d-lb-e2e"],
                    "past_moves": [],
                }
            ]
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    # --- Tick 1: active direction trips the lifetime-burn backstop -> pause, count=1.
    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "paused", "tick 1 should pause the lifetime-burn direction"
    assert d.get("lifetime_burn_pause_count") == 1, (
        "counter should advance 0->1 on the first burn pause"
    )
    assert d.get("needs_manual_intervention") is True, (
        "first burn pause sets the manual-hold flag (which blocks auto-recovery)"
    )
    assert not d.get("auto_completed"), "must NOT retire on the first pause (1 < 2)"

    # --- Simulate 72h elapsed (past the 48h auto-recovery window).
    project.directions[0]["paused_at"] = (
        _utcnow() - timedelta(hours=72)
    ).isoformat()
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    # --- Tick 2: the manual-held burn pause hits the auto-recovery extend-pause
    #     branch, which now advances the counter 1->2 and SETS auto_retire_pending
    #     (flag-not-execute contract) rather than retiring immediately.
    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now + timedelta(hours=73)
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d.get("auto_retire_pending") is True, (
        "tick 2 must set auto_retire_pending=True rather than retiring immediately"
    )
    assert d.get("auto_retire_pending_reason") == "lifetime_burn"
    assert d.get("lifetime_burn_pause_count") == 2, (
        "counter must advance to 2 (not freeze at 1) to reach the retire fence"
    )
    assert d["status"] != "completed", (
        "direction must NOT be retired on the flag tick — retirement deferred to tick 3"
    )

    # --- Tick 3: designated retire pass executes the retirement.
    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now + timedelta(hours=74)
    )
    await db_session.refresh(project)

    # The direction is retired and immediately pruned in the same tick.
    remaining_ids = {d["id"] for d in (project.directions or [])}
    assert "d-lb-e2e" not in remaining_ids, (
        "retired direction must be pruned from project.directions after the designated retire pass"
    )
    # Gap reconciled: direction removed from active_moves, recorded in past_moves.
    gap = project.problem_model["gaps"][0]
    assert "d-lb-e2e" not in gap["active_moves"], (
        "retired direction removed from gap active_moves"
    )
    assert "d-lb-e2e" in gap["past_moves"], (
        "retired direction recorded in gap past_moves"
    )
    # Vision memory records the retirement.
    retired_mem = (project.vision or {}).get("_recently_retired_directions") or []
    mem_entry = next((m for m in retired_mem if m.get("id") == "d-lb-e2e"), None)
    assert mem_entry is not None, "retired direction must appear in vision memory"
    assert mem_entry.get("reason") == "auto_retired_lifetime_burn", (
        f"vision memory reason should be 'auto_retired_lifetime_burn', got: {mem_entry.get('reason')!r}"
    )


@pytest.mark.asyncio
async def test_churn_family_recommendation_full_path(db_session, mocker):
    """End-to-end churn/unproductive path driven by real lifecycle ticks with a
    real apply_post_spawn_state call between ticks, starting from counter=0.

    Reproduces the production freeze: a churn-paused direction (counter=1) runs a
    research cycle that spawns child tasks. apply_post_spawn_state must NOT reset
    churn_family_pause_count (creating tasks != shipping merges), so a subsequent
    recovered-then-re-paused tick can still advance the counter to the >=2
    threshold. At the threshold the direction is flagged with a reviewable
    retirement recommendation and kept PAUSED — it is no longer auto-retired.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app.research_direction_state import apply_post_spawn_state
    from test_autonomous_research import _make_research_task

    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()

    direction = {
        "id": "d-churn-e2e",
        "text": "Churn/unproductive stuck direction (full path)",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": _unproductive_history(now),
        "gap_id": "g-churn-e2e",
        # No churn_family_pause_count — start from 0 and let the machine advance it.
    }
    project = Project(
        name="Churn Full Path Test",
        user_id="u-churn-e2e",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
        problem_model={
            "gaps": [
                {
                    "id": "g-churn-e2e",
                    "name": "Churn e2e gap",
                    "status": "active",
                    "active_moves": ["d-churn-e2e"],
                    "past_moves": [],
                }
            ]
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    # --- Tick 1: active -> Unproductive pause, counter 0->1.
    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project)
    d = project.directions[0]
    assert d["status"] == "paused", "tick 1 should pause the churn direction"
    assert d.get("churn_family_pause_count") == 1, "counter advances 0->1 on first pause"
    assert (d.get("paused_reason") or "").startswith("Unproductive:")
    assert not d.get("auto_completed")

    # --- A research cycle runs between lifecycle ticks and spawns a child task.
    #     The fix under test: apply_post_spawn_state must NOT reset the churn counter
    #     just because a task was created (a created task is not a merged PR).
    task = await _make_research_task(project, project.directions[0], db_session)
    child = Task(
        project_id=project.id,
        title="impl child (no merge)",
        payloads=[{"type": "create", "base_branch": "main"}],
        context={"direction_id": "d-churn-e2e"},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)

    await apply_post_spawn_state(
        db_session, task, [], [child], [], [], [], [],
        0, 0, 0, 0, 0, 0,
        "research-branch", "main", 0, None,
    )
    await db_session.refresh(project)
    d = project.directions[0]
    assert d.get("churn_family_pause_count") == 1, (
        "apply_post_spawn_state must NOT reset the churn counter on a task-creating "
        "(non-merging) cycle — that was the freeze-before-threshold bug"
    )

    # --- Simulate 72h elapsed so the direction auto-recovers on the next tick
    #     (restore the paused state in case the research cycle touched it).
    project.directions[0]["status"] = "paused"
    project.directions[0]["auto_paused"] = True
    project.directions[0]["paused_at"] = (
        _utcnow() - timedelta(hours=72)
    ).isoformat()
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    # --- Tick 2: auto-recovery reactivates the direction (a churn-family pause sets
    #     no manual hold). The grace period blocks an immediate re-pause this tick.
    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now + timedelta(hours=73)
    )
    await db_session.refresh(project)
    d = project.directions[0]
    assert d["status"] == "active", "tick 2 should auto-recover the churn direction"
    assert d.get("churn_family_pause_count") == 1, "counter survives auto-recovery"

    # --- A post-recovery research cycle finishes unproductively (completed, zero
    #     merges). Its timestamp is after auto_recovered_at so the grace period lifts
    #     and the next tick re-evaluates the churn-family health rules.
    post_recovery_cycle = {
        "timestamp": (_utcnow() + timedelta(hours=1)).isoformat(),
        "tasks_created": 2,
        "tasks_completed": 2,
        "tasks_failed": 0,
        "tasks_failed_infra": 0,
        "prs_merged": 0,
    }
    project.directions[0]["research_history"] = (
        list(project.directions[0]["research_history"]) + [post_recovery_cycle]
    )
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    # --- Tick 3: the recovered direction re-trips the churn-family rule, counter
    #     advances 1->2, crossing the threshold. The direction is flagged with a
    #     reviewable retirement recommendation and kept paused — not auto-retired.
    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now + timedelta(hours=74)
    )
    await db_session.refresh(project)
    d = project.directions[0]
    assert d["status"] == "paused", (
        "tick 3 keeps the churn direction paused (non-terminal) at the threshold"
    )
    assert d.get("churn_family_pause_count") == 2, (
        "counter must advance to 2 (not be reset by the intervening research cycle)"
    )
    assert d.get("auto_completed") is not True
    assert d.get("churn_retirement_recommended") is True, (
        "crossing the threshold must record a reviewable retirement recommendation"
    )
    # Non-terminal: the direction keeps its gap link and survives (not pruned), so
    # it remains available to reactivate/retire. The pause itself moves it into the
    # gap's past_moves (normal pause staffing) — it is NOT terminally retired.
    assert d.get("gap_id") == "g-churn-e2e"
    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now + timedelta(hours=75)
    )
    await db_session.refresh(project)
    assert any(x["id"] == "d-churn-e2e" for x in project.directions), (
        "recommended-but-not-retired direction must survive the prune tick"
    )


@pytest.mark.asyncio
async def test_lifecycle_auto_retire_records_retired_memory_before_prune(db_session, mocker):
    """Lifecycle auto-retire must write _recently_retired_directions to project.vision
    on the same tick that marks the direction completed, before the next prune pass
    removes it from project.directions.

    Uses the code-failure retire path (code_failure_pause_count already at
    threshold-1) so a single tick exercises a real production terminal-retire code
    path without hand-setting the terminal counter. (The churn-family ladder no
    longer auto-retires — it records a reviewable recommendation — so a still
    terminal ladder is used here to exercise the retired-memory write.) A second
    lifecycle tick then prunes the completed direction, confirming the memory
    survives.
    """
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()
    # >70% code-failure rate (Rule 3) re-trips a code-failure pause this tick;
    # code_failure_pause_count preset to 2 → increments to 3 → hits the retire
    # threshold (_MAX_AUTO_PAUSE_CYCLES = 3).
    code_failure_history = [
        {
            "timestamp": (now - timedelta(days=i)).isoformat(),
            "tasks_created": 3,
            "tasks_completed": 0,
            "tasks_failed": 3,
            "tasks_failed_infra": 0,
            "prs_merged": 0,
        }
        for i in range(3, 0, -1)
    ]
    direction = {
        "id": "d-retire-mem",
        "text": "Retire memory test direction to verify vision write on auto-retire",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": code_failure_history,
        "code_failure_pause_count": 2,  # one below the terminal fence
        "gap_id": "g-retire-mem",
    }
    project = Project(
        name="Retire Memory Test",
        user_id="u-retire-mem",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
        problem_model={
            "gaps": [
                {
                    "id": "g-retire-mem",
                    "name": "Retire mem gap",
                    "status": "active",
                    "active_moves": ["d-retire-mem"],
                    "past_moves": [],
                }
            ]
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    # Single lifecycle tick: direction trips a code-failure pause with
    # code_failure_pause_count already at 2, advances to 3 (>= _MAX_AUTO_PAUSE_CYCLES),
    # triggering the terminal code-failure auto-retire.
    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "completed", "direction must be auto-retired by lifecycle tick"
    assert d.get("auto_completed") is True

    # The retired memory must be written to vision on the SAME tick as the retire.
    retired_memory = (project.vision or {}).get("_recently_retired_directions") or []
    assert any(mem.get("id") == "d-retire-mem" for mem in retired_memory), (
        f"_recently_retired_directions must contain the auto-retired direction "
        f"before the next prune tick; got: {retired_memory}"
    )
    entry = next(m for m in retired_memory if m.get("id") == "d-retire-mem")
    assert "auto_retired" in (entry.get("reason") or ""), (
        f"reason must identify auto-retire source; got: {entry.get('reason')!r}"
    )

    # Confirm memory survives the prune pass (next tick removes the completed direction).
    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now + timedelta(hours=1)
    )
    await db_session.refresh(project)
    assert not any(d.get("id") == "d-retire-mem" for d in (project.directions or [])), (
        "completed direction must be pruned on the next lifecycle tick"
    )
    survived_memory = (project.vision or {}).get("_recently_retired_directions") or []
    assert any(mem.get("id") == "d-retire-mem" for mem in survived_memory), (
        "retired memory must survive the prune tick"
    )


@pytest.mark.asyncio
async def test_malformed_paused_at_skips_only_bad_direction(db_session, mocker):
    """Malformed paused_at on one direction does not abort auto-recovery for others.

    When one paused direction carries a malformed paused_at timestamp, the lifecycle
    sweep skips that direction gracefully and continues processing the rest, allowing
    auto-recovery and other lifecycle logic to proceed. The bad direction remains
    paused (state unchanged), while a valid direction with a recoverable pause_at
    is auto-recovered normally.
    """
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()

    # Direction with malformed paused_at — should be skipped gracefully
    bad_direction = {
        "id": "d-malformed-pause",
        "text": "Direction with malformed paused_at",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "Test pause",
        "paused_at": "not-a-valid-iso-timestamp",  # malformed
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
    }

    # Direction with valid paused_at that is past recovery window — should auto-recover
    good_direction = {
        "id": "d-valid-pause",
        "text": "Direction with valid paused_at",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "Test pause",
        "paused_at": (now - timedelta(hours=72)).isoformat(),  # 72h ago, past 48h recovery window
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [
            {
                "timestamp": (now - timedelta(days=1)).isoformat(),
                "tasks_created": 2,
                "tasks_completed": 2,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 1,
            }
        ],
    }

    project = Project(
        name="Malformed Paused At Test",
        user_id="u-malformed",
        autonomous_enabled=True,
        directions=[bad_direction, good_direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    # Process lifecycle — should not crash despite malformed paused_at
    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project)

    by_id = {d["id"]: d for d in project.directions}

    # Bad direction should remain paused (skipped gracefully)
    bad = by_id["d-malformed-pause"]
    assert bad["status"] == "paused", (
        "malformed paused_at direction should remain paused (skipped, not crashed)"
    )
    assert bad.get("auto_paused") is True
    assert bad.get("paused_at") == "not-a-valid-iso-timestamp", (
        "malformed paused_at should remain unchanged"
    )

    # Good direction should auto-recover (not skipped by the bad one)
    good = by_id["d-valid-pause"]
    assert good["status"] == "active", (
        "valid paused_at direction should auto-recover despite sibling with malformed paused_at"
    )
    assert good.get("auto_paused") is None, (
        "auto_paused should be cleared on auto-recovery"
    )
    assert good.get("paused_at") is None, (
        "paused_at should be cleared on auto-recovery"
    )
    assert good.get("auto_recovered_at") is not None, (
        "auto_recovered_at should be stamped on auto-recovery"
    )


@pytest.mark.asyncio
async def test_lifecycle_sweep_syncs_direction_table(db_session, mocker):
    """After a lifecycle sweep auto-pauses a direction, the Direction table row
    must reflect the new status — the dual-write in _process_direction_lifecycle_locked
    must sync the normalised table before committing the blob.
    """
    from app.models import Direction

    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()
    direction = {
        "id": "d-sweep-sync-test",
        "text": "Direction to be auto-paused by sweep",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "cost_budget_usd": 1.0,
        # Two history entries summing to 2.0 > budget of 1.0 → triggers auto-pause
        "research_history": _cost_history(1.1, 0.9),
    }
    project = Project(
        name="Sweep Sync Test Project",
        user_id="u-sweep-sync",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    # Run the lifecycle sweep — should auto-pause the direction AND sync Direction table
    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=now,
    )
    await db_session.refresh(project)

    # Blob should show the direction as paused
    blob_dir = project.directions[0]
    assert blob_dir["status"] == "paused", (
        f"blob status should be paused after sweep, got {blob_dir['status']!r}"
    )

    # Direction table row must also reflect the paused status (the dual-write)
    result = await db_session.execute(
        select(Direction).where(
            Direction.project_id == project.id,
            Direction.direction_key == "d-sweep-sync-test",
        )
    )
    row = result.scalars().first()
    assert row is not None, (
        "Direction table row must exist after lifecycle sweep syncs the table"
    )
    assert row.status == "paused", (
        f"Direction table row status should be 'paused' after sweep, got {row.status!r}"
    )


def test_compact_direction_summary_preserves_churn_retirement_recommendation():
    """The strategic-payload compactor must keep the churn-retirement
    recommendation flag and its bounded evidence so the strategist can see it
    after per-entry compaction (the field is only useful if it survives packing).
    """
    from app.strategic_payload_compaction import _compact_direction_summary_entry

    stat = {
        "id": "d-churn",
        "priority": "medium",
        "status": "paused",
        "text": "churn direction recommended for retirement",
        "auto_paused": True,
        "paused_reason": "Unproductive: 0 PRs merged",
        "churn_retirement_recommended": True,
        "churn_retirement_evidence": {
            "churn_family_pause_count": 2,
            "threshold": 2,
            "merge_rate": 0.0,
            "non_merge_finished": 4,
            "window_cycles": 5,
            "summary": "Paused 2x for churn with no escape; retirement recommended.",
        },
    }
    out = _compact_direction_summary_entry(stat)
    assert out.get("churn_retirement_recommended") is True
    ev = out.get("churn_retirement_evidence") or {}
    assert ev.get("churn_family_pause_count") == 2
    assert ev.get("threshold") == 2
    assert ev.get("merge_rate") == 0.0
    assert isinstance(ev.get("summary"), str) and ev["summary"]

@pytest.mark.asyncio
async def test_paused_extend_burn_sets_pending_flag_not_executes(db_session, mocker):
    """Paused-extend burn path sets auto_retire_pending=True without retiring on the same tick.

    A direction with needs_manual_intervention and a 'Lifetime burn backstop:' pause
    reason that hits _MAX_LIFETIME_BURN_PAUSE_CYCLES on the extend-pause path must
    set auto_retire_pending rather than immediately executing status=completed.
    """
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()
    history, aggregate = _lifetime_burn_history(now)

    # Direction already at threshold-1: lb_count=1, paused with manual hold.
    # Simulating the state after tick 1 (first burn pause) with 72h elapsed.
    direction = {
        "id": "d-lb-pending",
        "text": "Paused-extend burn pending flag test",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "Lifetime burn backstop: cumulative non-merge ratio above threshold",
        "paused_at": (now - timedelta(hours=72)).isoformat(),
        "needs_manual_intervention": True,
        "needs_manual_intervention_at": (now - timedelta(hours=72)).isoformat(),
        "lifetime_burn_pause_count": 1,
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": history,
        "history_aggregate": aggregate,
        "gap_id": "g-lb-pending",
    }
    project = Project(
        name="LB Pending Flag Test",
        user_id="u-lb-pending",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
        problem_model={
            "gaps": [
                {
                    "id": "g-lb-pending",
                    "name": "LB pending gap",
                    "status": "active",
                    "active_moves": ["d-lb-pending"],
                    "past_moves": [],
                }
            ]
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    # Tick: extend-pause path fires, counter 1->2, sets pending flag.
    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d.get("auto_retire_pending") is True, (
        "paused-extend burn path must set auto_retire_pending=True rather than retiring immediately"
    )
    assert d.get("auto_retire_pending_reason") == "lifetime_burn"
    assert d.get("lifetime_burn_pause_count") == 2, (
        "counter must advance to 2 on the flagging tick"
    )
    assert d["status"] != "completed", (
        "direction must NOT be retired on the same tick the pending flag is set"
    )
    # Gap must still be staffed — retirement deferred to next tick.
    gap = project.problem_model["gaps"][0]
    assert "d-lb-pending" in gap["active_moves"], (
        "gap must still show direction as staffed on the flag tick"
    )


@pytest.mark.asyncio
async def test_scaffolding_sets_pending_flag_not_executes(db_session, mocker):
    """Scaffolding path sets auto_retire_pending=True without retiring on the same tick.

    A direction that hits max_flagged_cycles in the active-directions loop must set
    auto_retire_pending rather than immediately executing status=completed.
    """
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_stalled_message",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.notification_dispatch.post_direction_outcome_scaffolding_escalation_message",
        new_callable=AsyncMock,
    )
    milestone_notify = mocker.patch(
        "app.notification_dispatch.post_direction_milestone_message",
        new_callable=AsyncMock,
    )

    # flagged_cycles=5 → next pass increments to 6 (= max_flagged_cycles default).
    direction = _already_flagged_direction(flagged_cycles=5)
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=_utcnow()
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d.get("auto_retire_pending") is True, (
        "scaffolding path must set auto_retire_pending=True rather than retiring immediately"
    )
    assert d.get("auto_retire_pending_reason") == "scaffolding"
    assert d["status"] != "completed", (
        "direction must NOT be retired on the same tick the pending flag is set"
    )
    milestone_notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_designated_retire_pass_consumes_pending_flag(db_session, mocker):
    """Designated retire pass converts auto_retire_pending=True to status=completed.

    Pre-stamp both reason variants ("lifetime_burn" and "scaffolding") on separate
    directions and verify the designated pass retires each correctly on the next tick.
    """
    mocker.patch(
        "app.notification_dispatch.post_direction_milestone_message",
        new_callable=AsyncMock,
    )

    now = _utcnow()
    history, aggregate = _lifetime_burn_history(now)

    # Direction 1: pending lifetime_burn retirement.
    dir_burn = {
        "id": "d-pending-burn",
        "text": "Pending lifetime-burn retire",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "Lifetime burn backstop: prior flag",
        "paused_at": (now - timedelta(hours=1)).isoformat(),
        "needs_manual_intervention": True,
        "needs_manual_intervention_at": (now - timedelta(hours=1)).isoformat(),
        "lifetime_burn_pause_count": 2,
        "auto_retire_pending": True,
        "auto_retire_pending_reason": "lifetime_burn",
        "auto_retire_pending_at": (now - timedelta(hours=1)).isoformat(),
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": history,
        "history_aggregate": aggregate,
        "gap_id": "g-pending-burn",
    }

    # Direction 2: pending scaffolding retirement.
    direction_sc = _already_flagged_direction(flagged_cycles=6)
    direction_sc["id"] = "d-pending-sc"
    direction_sc["auto_retire_pending"] = True
    direction_sc["auto_retire_pending_reason"] = "scaffolding"
    direction_sc["auto_retire_pending_at"] = (now - timedelta(hours=1)).isoformat()

    project = Project(
        name="Designated Retire Pass Test",
        user_id="u-designated-retire",
        autonomous_enabled=True,
        directions=[dir_burn, direction_sc],
        vision={"purpose": "test"},
        problem_model={
            "gaps": [
                {
                    "id": "g-pending-burn",
                    "name": "Pending burn gap",
                    "status": "active",
                    "active_moves": ["d-pending-burn"],
                    "past_moves": [],
                }
            ]
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project)

    # Both directions are retired (status=completed) and pruned in the same tick.
    remaining_ids = {d["id"] for d in (project.directions or [])}
    assert "d-pending-burn" not in remaining_ids, (
        "designated retire pass must retire and prune the lifetime_burn-pending direction"
    )
    assert "d-pending-sc" not in remaining_ids, (
        "designated retire pass must retire and prune the scaffolding-pending direction"
    )

    # Vision memory records both retirements.
    retired_mem = (project.vision or {}).get("_recently_retired_directions") or []
    retired_ids = {m.get("id") for m in retired_mem}
    assert "d-pending-burn" in retired_ids, "lifetime-burn direction must appear in vision memory"
    assert "d-pending-sc" in retired_ids, "scaffolding direction must appear in vision memory"

    burn_mem = next((m for m in retired_mem if m.get("id") == "d-pending-burn"), None)
    assert burn_mem is not None
    assert burn_mem.get("reason") == "auto_retired_lifetime_burn", (
        f"lifetime-burn reason should be 'auto_retired_lifetime_burn', got: {burn_mem.get('reason')!r}"
    )

    sc_mem = next((m for m in retired_mem if m.get("id") == "d-pending-sc"), None)
    assert sc_mem is not None
    assert sc_mem.get("reason") == "auto_retired_scaffolding", (
        f"scaffolding reason should be 'auto_retired_scaffolding', got: {sc_mem.get('reason')!r}"
    )

    # Gap reconciled for lifetime-burn direction.
    gap = project.problem_model["gaps"][0]
    assert "d-pending-burn" not in gap["active_moves"], (
        "retired direction removed from gap active_moves by designated pass"
    )
    assert "d-pending-burn" in gap["past_moves"]


@pytest.mark.asyncio
async def test_designated_retire_coherence_guard_skips_already_terminal(
    db_session, mocker
):
    """Coherence guard prevents double-retirement when a direction is already terminal.

    If a direction reaches a terminal status via another path (e.g. operator PATCH
    sets status=completed) while auto_retire_pending=True is still set, the
    designated-retire pass must skip the retirement logic (clear the flag only)
    rather than overwriting auto_completed_reason and firing a duplicate
    Alert + milestone notification.
    """
    mock_notify = mocker.patch(
        "app.notification_dispatch.post_direction_milestone_message",
        new_callable=AsyncMock,
    )

    now = _utcnow()
    # Direction that was retired by another path (operator PATCH) while the
    # auto_retire_pending flag was still set. The coherence guard must detect
    # the terminal status and clear the flag without re-executing retirement.
    direction = {
        "id": "d-already-terminal",
        "text": "Direction already completed by another path",
        "status": "completed",
        "auto_completed": True,
        "auto_completed_reason": "operator-patch: manually completed",
        "completed_at": (now - timedelta(minutes=5)).isoformat(),
        # Flag set before the operator completed this direction; should be cleared
        # without triggering a second retirement.
        "auto_retire_pending": True,
        "auto_retire_pending_reason": "lifetime_burn",
        "auto_retire_pending_at": (now - timedelta(hours=1)).isoformat(),
        "lifetime_burn_pause_count": 2,
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
    }

    project = Project(
        name="Coherence Guard Test",
        user_id="u-coherence",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project)

    # The direction is pruned (status=completed → pruned in the sweep).
    remaining_ids = {d["id"] for d in (project.directions or [])}
    assert "d-already-terminal" not in remaining_ids, (
        "already-terminal direction must be pruned from project.directions"
    )
    # The coherence guard must have prevented a second retirement notification.
    mock_notify.assert_not_called(), (
        "post_direction_milestone_message must NOT be called for an already-terminal direction "
        "(coherence guard should skip the retirement logic and clear the flag only)"
    )


# ---------------------------------------------------------------------------
# reactivate_paused_direction: the shared paused→active seam
# ---------------------------------------------------------------------------
from app.direction_lifecycle import reactivate_paused_direction  # noqa: E402
from app.strategic_completion import _check_direction_health  # noqa: E402


def _auto_paused_direction() -> dict:
    """An auto-paused churn-family direction with residue and preserved counters."""
    return {
        "id": "d-reactivate",
        "text": "auto-paused churn direction",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "Churn guardrail: high churn, low merge rate",
        "paused_at": "2026-01-01T00:00:00",
        "needs_manual_intervention": False,
        "escalation_notification_pending": True,
        "escalation_notification_retries": 2,
        "last_researched_at": "2026-01-01T00:00:00",
        "remediation_attempts": 3,
        "churn_family_pause_count": 1,
        "lifetime_burn_pause_count": 2,
        # Simulate a direction that was flagged for auto-retirement while paused.
        # _clear_direction_reactivation_state must strip these three keys so an
        # operator reactivation is not silently overridden by the designated-retire
        # pass on the next lifecycle tick (the d-21efb120 incident class).
        "auto_retire_pending": True,
        "auto_retire_pending_reason": "lifetime_burn",
        "auto_retire_pending_at": "2026-01-01T01:00:00",
        "research_history": [],
    }


def test_reactivate_auto_paused_stamps_grace_and_clears_residue():
    d = _auto_paused_direction()
    result = reactivate_paused_direction(d, allow_user_paused=False)
    assert result is True
    assert d["status"] == "active"
    # Grace marker stamped because the prior status was 'paused'
    assert d.get("auto_recovered_at") is not None
    # Pause/intervention residue cleared
    for key in (
        "auto_paused",
        "paused_reason",
        "paused_at",
        "escalation_notification_pending",
        "escalation_notification_retries",
        "last_researched_at",
    ):
        assert key not in d, f"{key} should be cleared on reactivation"
    assert d["remediation_attempts"] == 0
    # Counters that must survive a reactivation are untouched
    assert d["churn_family_pause_count"] == 1
    assert d["lifetime_burn_pause_count"] == 2
    # Pending auto-retirement must be cancelled on reactivation: without this the
    # designated-retire pass would silently override the operator's intent on the
    # very next lifecycle tick (the d-21efb120 incident class).
    for key in ("auto_retire_pending", "auto_retire_pending_reason", "auto_retire_pending_at"):
        assert key not in d, f"{key} must be cleared on reactivation to cancel pending retire"


def test_reactivate_user_paused_blocked_when_not_allowed():
    d = {
        "id": "d-user-paused",
        "status": "paused",
        "auto_paused": False,  # user-paused, not auto-paused
        "paused_reason": "operator paused",
        "paused_at": "2026-01-01T00:00:00",
        "research_history": [],
    }
    snapshot = dict(d)
    result = reactivate_paused_direction(d, allow_user_paused=False)
    assert result is False
    # No mutation on a blocked reactivation
    assert d == snapshot
    assert d["status"] == "paused"
    assert "auto_recovered_at" not in d


def test_reactivate_user_paused_allowed_for_operator():
    d = {
        "id": "d-user-paused",
        "status": "paused",
        "auto_paused": False,
        "paused_reason": "operator paused",
        "paused_at": "2026-01-01T00:00:00",
        "research_history": [],
    }
    result = reactivate_paused_direction(d, allow_user_paused=True)
    assert result is True
    assert d["status"] == "active"
    # Prior status was 'paused' → grace stamped even for a user-paused resume
    assert d.get("auto_recovered_at") is not None
    assert "auto_paused" not in d


def test_reactivate_staged_does_not_stamp_grace():
    d = {
        "id": "d-staged",
        "status": "staged",
        "staged_for_version": "v22",
        "staged_activated_at": None,
        "research_history": [],
    }
    result = reactivate_paused_direction(d, allow_user_paused=False)
    assert result is True
    assert d["status"] == "active"
    # Ordinary staged→active activation is grace-free
    assert "auto_recovered_at" not in d


def test_reactivate_proposed_does_not_stamp_grace():
    d = {"id": "d-proposed", "status": "proposed", "research_history": []}
    result = reactivate_paused_direction(d, allow_user_paused=False)
    assert result is True
    assert d["status"] == "active"
    assert "auto_recovered_at" not in d


def test_reactivated_direction_survives_health_check_with_stale_history():
    """Grace stamped by the helper makes _check_direction_health return None even
    when the frozen research_history would otherwise trip Rule 2 (Unproductive)."""
    # 5 pre-recovery cycles with 0 merges — Rule 2 would normally fire.
    history = [
        {
            "timestamp": f"2026-01-0{i}T00:00:00",
            "tasks_created": 5,
            "tasks_completed": 5,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": 0,
        }
        for i in range(1, 6)
    ]
    d = {
        "id": "d-stale",
        "text": "stale unproductive",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "Unproductive: 0 PRs merged across 5 cycles",
        "paused_at": "2026-01-06T00:00:00",
        "research_history": history,
    }
    # Before reactivation the health check would want to pause it.
    assert _check_direction_health(d) is not None
    assert reactivate_paused_direction(d, allow_user_paused=False) is True
    # After reactivation the stamped grace suppresses the history-based re-pause
    # until a cycle newer than auto_recovered_at is appended.
    assert _check_direction_health(d) is None


@pytest.mark.asyncio
async def test_resurrected_direction_survives_next_lifecycle_tick(db_session, mocker):
    """A freshly-resurrected direction SURVIVES the next lifecycle tick.

    resurrect_direction stamps auto_recovered_at (grace) and zeroes the ladder
    counters, so even though the frozen research_history would otherwise trip the
    churn/saturation family (Rules 1, 2, 5), the direction is NOT re-paused or
    re-retired on the very next _process_direction_lifecycle pass. This is the
    guarantee that distinguishes the sanctioned resurrection path from a raw
    completed->active status flip (which re-retires immediately).
    """
    from app.direction_lifecycle import resurrect_direction

    now = _utcnow()
    # 5 unproductive cycles (0 merges), all timestamped in the past. Under normal
    # health evaluation this trips Rule 2 (Unproductive: 0 PRs merged).
    history = [
        {
            "timestamp": (now - timedelta(days=i)).isoformat(),
            "tasks_created": 5,
            "tasks_completed": 5,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": 0,
        }
        for i in range(5, 0, -1)
    ]
    # A terminally auto-retired direction with primed ladder counters AND a
    # pending auto-retire flag — the exact snapshot state a DirectionEvent row
    # may carry when the direction was snapshotted while the flag was set before
    # it was pruned.
    direction = {
        "id": "d-resurrected-survives",
        "text": "Wrongly auto-retired direction being brought back",
        "status": "completed",
        "auto_completed": True,
        "auto_completed_reason": "Auto-retired: zombie direction",
        "completed_at": now.isoformat(),
        # auto_retire_pending* carried into a snapshot while flagged; resurrection
        # must strip these so the designated-retire pass doesn't re-retire the
        # freshly-resurrected direction on the very next tick.
        "auto_retire_pending": True,
        "auto_retire_pending_reason": "lifetime_burn",
        "auto_retire_pending_at": (now - timedelta(hours=1)).isoformat(),
        "churn_family_pause_count": 2,
        "lifetime_burn_pause_count": 2,
        "code_failure_pause_count": 0,
        "auto_pause_count": 3,
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": history,
    }

    resurrect_direction(direction)

    # Primitive produced a clean, resurrectable direction.
    assert direction["status"] == "active"
    assert direction["churn_family_pause_count"] == 0
    assert direction["lifetime_burn_pause_count"] == 0
    assert direction["code_failure_pause_count"] == 0
    assert direction["auto_pause_count"] == 0
    assert "auto_completed" not in direction
    assert "completed_at" not in direction
    assert direction.get("auto_recovered_at") is not None
    assert direction.get("resurrected_at") is not None
    # Pending auto-retirement must be stripped so the designated-retire pass
    # cannot re-retire on the next tick, bypassing the grace period.
    assert "auto_retire_pending" not in direction, (
        "auto_retire_pending must be stripped by resurrect_direction"
    )
    assert "auto_retire_pending_reason" not in direction
    assert "auto_retire_pending_at" not in direction

    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=now,
    )
    await db_session.refresh(project)

    # Grace holds: the direction is still present and active — not re-paused,
    # not re-retired, not pruned.
    assert len(project.directions) == 1, (
        "resurrected direction must not be pruned on the next tick"
    )
    d = project.directions[0]
    assert d["status"] == "active", (
        f"grace window should keep the resurrected direction active, got {d['status']!r}"
    )
    assert d.get("auto_recovered_at") is not None, (
        "auto_recovered_at should remain set while still in grace period"
    )
    assert d.get("churn_family_pause_count", 0) == 0


# ---------------------------------------------------------------------------
# Churn-retirement RECOMMENDATION-and-hold lifecycle (replaces the former
# silent terminal zombie retire). See direction_lifecycle churn ladder site.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_churn_retirement_recommended_survives_ttl_and_cooldown(db_session, mocker):
    """NAMED acceptance test. A churn-retirement-recommended direction is HELD
    PAUSED indefinitely: neither the 72h needs_manual_intervention TTL fallback
    nor the 48h auto-recovery cooldown silently un-flags or reactivates it. It
    survives repeated lifecycle ticks well past BOTH windows without reactivating
    and without the recommendation clearing — only an explicit keep/retire
    decision may release it."""
    from sqlalchemy.orm.attributes import flag_modified

    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()
    direction = {
        "id": "d-hold",
        "text": "Churn direction held for a keep/retire decision",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": _unproductive_history(now),
        "churn_family_pause_count": 1,  # next churn pause hits the recommend fence
        "gap_id": "g-hold",
    }
    project = Project(
        name="Churn Hold Test",
        user_id="u-hold",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
        problem_model={
            "gaps": [
                {
                    "id": "g-hold",
                    "name": "Hold gap",
                    "status": "active",
                    "active_moves": ["d-hold"],
                    "past_moves": [],
                }
            ]
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    # Tick 1: drive the direction to the churn-retirement recommendation + hold.
    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project)
    d = project.directions[0]
    assert d["status"] == "paused", "recommendation must hold the direction paused"
    assert d.get("churn_retirement_recommended") is True
    assert d.get("needs_manual_intervention") is True
    assert d.get("auto_completed") is not True

    # Repeatedly age the hold past BOTH the 72h intervention TTL and the 48h
    # auto-recovery cooldown (both computed against real wall-clock _utcnow()),
    # then tick. Without the churn_retirement_recommended guards the TTL loop
    # would clear needs_manual_intervention and a follow-up recovery tick would
    # reactivate the direction. The guards must keep it held every time.
    for i in range(3):
        stale = (_utcnow() - timedelta(hours=200)).isoformat()
        project.directions[0]["paused_at"] = stale
        project.directions[0]["needs_manual_intervention_at"] = stale
        flag_modified(project, "directions")
        db_session.add(project)
        await db_session.commit()

        await _process_direction_lifecycle(
            project,
            db_session,
            busy_direction_ids=set(),
            now=now + timedelta(hours=200 * (i + 1)),
        )
        await db_session.refresh(project)
        d = project.directions[0]
        assert d["status"] == "paused", (
            f"tick {i + 2}: churn-recommended direction must not silently reactivate"
        )
        assert d.get("churn_retirement_recommended") is True, (
            f"tick {i + 2}: recommendation must not be cleared by time alone"
        )
        assert d.get("needs_manual_intervention") is True, (
            f"tick {i + 2}: needs_manual_intervention must survive the 72h TTL fallback"
        )
        assert d.get("churn_family_pause_count") == 2, (
            f"tick {i + 2}: ladder counter must not advance while held paused"
        )

    # Never terminally retired by a time-based pass: no auto-retired memory is
    # ever written across all the aged ticks.
    retired_memory = (project.vision or {}).get("_recently_retired_directions") or []
    assert not any(mem.get("id") == "d-hold" for mem in retired_memory), (
        "a held direction must never record retired memory via a time-based pass"
    )


@pytest.mark.asyncio
async def test_auto_recovery_recovers_normal_but_not_recommended(db_session, mocker):
    """The churn_retirement_recommended auto-recovery guard is targeted: a normal
    auto-paused direction past the cooldown still auto-recovers to active, while a
    churn-retirement-recommended direction at the same age stays paused."""
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = _utcnow()
    stale = (now - timedelta(hours=200)).isoformat()

    normal = {
        "id": "d-normal-recover",
        "text": "Ordinary auto-paused direction past cooldown",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "Unproductive: 0 PRs merged across 5 cycles",
        "paused_at": stale,
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [
            {
                "timestamp": (now - timedelta(days=1)).isoformat(),
                "tasks_created": 2,
                "tasks_completed": 2,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 1,
            }
        ],
    }
    recommended = {
        "id": "d-recommended-held",
        "text": "Churn-retirement-recommended direction past cooldown",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "Unproductive: 0 PRs merged across 5 cycles",
        "paused_at": stale,
        "churn_retirement_recommended": True,
        "churn_retirement_recommended_reason": "Retirement recommended: zombie",
        "churn_retirement_recommended_at": stale,
        "needs_manual_intervention": True,
        "needs_manual_intervention_at": stale,
        "churn_family_pause_count": 2,
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
    }
    project = Project(
        name="Recovery Guard Contrast",
        user_id="u-recover-guard",
        autonomous_enabled=True,
        directions=[normal, recommended],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    await _process_direction_lifecycle(
        project, db_session, busy_direction_ids=set(), now=now
    )
    await db_session.refresh(project)

    by_id = {d["id"]: d for d in project.directions}
    assert by_id["d-normal-recover"]["status"] == "active", (
        "a normal auto-paused direction past cooldown must still auto-recover"
    )
    held = by_id["d-recommended-held"]
    assert held["status"] == "paused", (
        "a churn-retirement-recommended direction must NOT auto-recover"
    )
    assert held.get("churn_retirement_recommended") is True
    assert held.get("needs_manual_intervention") is True


def test_reactivate_clears_churn_retirement_recommendation():
    """reactivate_paused_direction (operator/strategist keep decision) clears the
    churn-retirement recommendation cleanly so the hold is released and cannot
    silently re-apply."""
    from app.direction_lifecycle import reactivate_paused_direction

    d = {
        "id": "d-reactivate",
        "status": "paused",
        "auto_paused": True,
        "paused_at": "2026-01-01T00:00:00",
        "paused_reason": "Unproductive: 0 PRs merged across 5 cycles",
        "churn_retirement_recommended": True,
        "churn_retirement_recommended_reason": "Retirement recommended: zombie",
        "churn_retirement_recommended_at": "2026-01-01T00:00:00",
        "needs_manual_intervention": True,
        "needs_manual_intervention_at": "2026-01-01T00:00:00",
        "churn_family_pause_count": 2,
    }
    assert reactivate_paused_direction(d, allow_user_paused=False) is True
    assert d["status"] == "active"
    assert "churn_retirement_recommended" not in d
    assert "churn_retirement_recommended_reason" not in d
    assert "churn_retirement_recommended_at" not in d
    assert "needs_manual_intervention" not in d


def test_resurrect_strips_churn_retirement_recommendation():
    """resurrect_direction strips the churn-retirement recommendation from a
    snapshot AND resets the churn ladder counters, so a resurrected direction
    leaves with the recommendation gone and cannot immediately re-recommend."""
    from app.direction_lifecycle import resurrect_direction

    d = {
        "id": "d-resurrect-churn",
        "status": "paused",
        "auto_paused": True,
        "paused_at": "2026-01-01T00:00:00",
        "churn_retirement_recommended": True,
        "churn_retirement_recommended_reason": "Retirement recommended: zombie",
        "churn_retirement_recommended_at": "2026-01-01T00:00:00",
        "needs_manual_intervention": True,
        "needs_manual_intervention_at": "2026-01-01T00:00:00",
        "churn_family_pause_count": 2,
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
    }
    resurrect_direction(d)
    assert d["status"] == "active"
    assert "churn_retirement_recommended" not in d
    assert "churn_retirement_recommended_reason" not in d
    assert "churn_retirement_recommended_at" not in d
    assert d["churn_family_pause_count"] == 0, (
        "ladder reset so a resurrected direction cannot immediately re-recommend"
    )


def test_resurrection_notes_capped_and_idempotent():
    """resurrect_direction keeps only the newest resurrection notes and ignores retries."""
    from app.direction_lifecycle import resurrect_direction

    notes = [
        {"at": f"2026-01-01T00:00:{i:02d}", "actor": "operator", "action": "resurrected"}
        for i in range(50)
    ]
    d = {
        "id": "d-resurrect-notes",
        "status": "completed",
        "auto_completed": True,
        "completed_at": "2026-01-01T00:00:00",
        "resurrection_notes": notes,
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
    }

    resurrect_direction(d, now_iso="2026-02-01T00:00:00", actor="operator", rationale="retry")
    first_pass = list(d["resurrection_notes"])
    assert len(first_pass) == 50
    assert first_pass[0]["at"] == "2026-01-01T00:00:01"
    assert first_pass[-1]["at"] == "2026-02-01T00:00:00"

    resurrect_direction(d, now_iso="2026-03-01T00:00:00", actor="operator", rationale="retry")
    assert d["resurrection_notes"] == first_pass


def test_keep_decision_reactivates_and_resets_churn_ladder():
    """apply_churn_retirement_keep reactivates a held-paused direction, clears the
    recommendation + manual-hold residue, and resets the churn ladder to 0."""
    from app.direction_lifecycle import apply_churn_retirement_keep

    d = {
        "id": "d-keep",
        "status": "paused",
        "auto_paused": True,
        "paused_at": "2026-01-01T00:00:00",
        "paused_reason": "Churn guardrail: repeated no-merge",
        "churn_retirement_recommended": True,
        "churn_retirement_recommended_reason": "Retirement recommended",
        "churn_retirement_recommended_at": "2026-01-01T00:00:00",
        "needs_manual_intervention": True,
        "needs_manual_intervention_at": "2026-01-01T00:00:00",
        "churn_family_pause_count": 2,
        "churn_pause_evidence": {"windowed_non_merge": 9},
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
    }
    assert apply_churn_retirement_keep(d, allow_user_paused=True) is True
    assert d["status"] == "active"
    assert "churn_retirement_recommended" not in d
    assert "needs_manual_intervention" not in d
    assert d["churn_family_pause_count"] == 0
    assert "churn_pause_evidence" not in d
    assert d.get("auto_recovered_at")


def test_retire_decision_completes_and_clears_recommendation():
    """apply_churn_retirement_retire confirms the terminal outcome and clears the
    recommendation + manual-hold flags."""
    from app.direction_lifecycle import apply_churn_retirement_retire

    d = {
        "id": "d-retire",
        "status": "paused",
        "auto_paused": True,
        "churn_retirement_recommended": True,
        "churn_retirement_recommended_reason": "Retirement recommended",
        "churn_retirement_recommended_at": "2026-01-01T00:00:00",
        "needs_manual_intervention": True,
        "needs_manual_intervention_at": "2026-01-01T00:00:00",
        "churn_family_pause_count": 2,
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }
    apply_churn_retirement_retire(d, reason="Retired by explicit decision")
    assert d["status"] == "completed"
    assert d["auto_completed"] is True
    assert d["auto_completed_reason"] == "Retired by explicit decision"
    assert d["completed_at"]
    assert "churn_retirement_recommended" not in d
    assert "needs_manual_intervention" not in d


@pytest.mark.asyncio
async def test_paused_churn_recommended_direction_surfaces_to_operator(db_session):
    """A paused churn-retirement-recommended direction produces a needs_operator
    attention item even though the manual-intervention path only sees active ones,
    and the strategist payload surfaces the recommendation flag."""
    from app.operator_attention import build_operator_attention
    from app.strategic_spawning import _build_direction_stats

    direction = {
        "id": "d-churn-attn",
        "text": "Zombie direction pending decision",
        "status": "paused",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "auto_paused": True,
        "paused_at": "2026-06-01T00:00:00",
        "churn_family_pause_count": 2,
        "needs_manual_intervention": True,
        "needs_manual_intervention_at": "2026-06-01T00:00:00",
        "churn_retirement_recommended": True,
        "churn_retirement_recommended_reason": "Retirement recommended: zombie loop",
        "churn_retirement_recommended_at": "2026-06-01T00:00:00",
    }
    project = Project(
        name="Churn Attention Test",
        user_id="u-churn",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    attention = await build_operator_attention(db_session, project, "u-churn")
    churn_items = [
        item
        for item in attention["items"]
        if item.get("reason") == "churn_retirement_recommended"
    ]
    assert churn_items, "paused churn-recommended direction must surface to operator"
    assert churn_items[0]["severity"] == "needs_operator"
    assert churn_items[0]["ref"]["id"] == "d-churn-attn"
    assert "zombie loop" in churn_items[0]["detail"]
    assert attention["counts"]["churn_retirement_recommended"] == 1

    _included, stats = _build_direction_stats(project)
    stat = next(s for s in stats if s["id"] == "d-churn-attn")
    assert stat["churn_retirement_recommended"] is True
    assert "zombie loop" in stat["churn_retirement_recommended_reason"]


def test_retired_diagnoses_archive_dedups_by_direction_id():
    """retired_diagnoses_archive deduplicates by direction_id: appending the same
    direction twice keeps only the latest entry."""
    from app.research_direction_state import _append_archived_diagnosis

    doc = {"retired_diagnoses_archive": []}
    direction = {
        "id": "d-dedup-test",
        "research_context": "## Eviction Summary\nFirst diagnosis for d-dedup-test.",
    }

    # First append: add the direction to the archive
    _append_archived_diagnosis(doc, direction, "d-dedup-test")
    assert len(doc["retired_diagnoses_archive"]) == 1
    first_entry = doc["retired_diagnoses_archive"][0]
    assert first_entry["direction_id"] == "d-dedup-test"
    assert "First diagnosis" in first_entry["diagnosis"]

    # Second append with same direction_id: should deduplicate, not append
    direction["research_context"] = "## Eviction Summary\nUpdated diagnosis for d-dedup-test."
    _append_archived_diagnosis(doc, direction, "d-dedup-test")

    # Archive should still have only 1 entry (deduplicated), with updated content
    assert len(doc["retired_diagnoses_archive"]) == 1
    updated_entry = doc["retired_diagnoses_archive"][0]
    assert updated_entry["direction_id"] == "d-dedup-test"
    assert "Updated diagnosis" in updated_entry["diagnosis"]

    # Append a different direction: should add a new entry
    direction2 = {
        "id": "d-dedup-test-2",
        "research_context": "## Eviction Summary\nDiagnosis for a different direction.",
    }
    _append_archived_diagnosis(doc, direction2, "d-dedup-test-2")
    assert len(doc["retired_diagnoses_archive"]) == 2
    assert doc["retired_diagnoses_archive"][0]["direction_id"] == "d-dedup-test"
    assert doc["retired_diagnoses_archive"][1]["direction_id"] == "d-dedup-test-2"

    # Append to first direction again: should still keep only 1 entry for d-dedup-test,
    # now at the end (moved by dedup+append)
    direction["research_context"] = "## Eviction Summary\nThird update for d-dedup-test."
    _append_archived_diagnosis(doc, direction, "d-dedup-test")
    assert len(doc["retired_diagnoses_archive"]) == 2
    # After dedup+append, d-dedup-test is now at the end
    assert doc["retired_diagnoses_archive"][0]["direction_id"] == "d-dedup-test-2"
    assert doc["retired_diagnoses_archive"][1]["direction_id"] == "d-dedup-test"
    assert "Third update" in doc["retired_diagnoses_archive"][1]["diagnosis"]

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

import app.shared_state as _ss
from app.direction_lifecycle import (
    _process_direction_lifecycle,
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
    now = datetime.utcnow()
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
        cooldown_minutes=0.0,
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
    consecutive_unproductive_cycles=0) with many cycles and 0 merges is NOT paused by
    the cumulative merge-rate family (Rule 2 / 5 / 6)."""
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    now = datetime.utcnow()
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
        cooldown_minutes=0.0,
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
    now = datetime.utcnow()
    # 3 cycles, 10 tasks each, 9 code failures → >70% code failure rate (Rule 3).
    history = [
        {
            "timestamp": (now - timedelta(days=i)).isoformat(),
            "tasks_created": 10,
            "tasks_completed": 1,
            "tasks_failed": 9,
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
        cooldown_minutes=0.0,
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
    recovered_at = datetime.utcnow() - timedelta(minutes=5)
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
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
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
    recovered_at = datetime.utcnow() - timedelta(hours=2)
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
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
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

    recovered_at = datetime.utcnow() - timedelta(minutes=5)
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
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
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
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
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
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
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
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
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
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
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
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
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
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
    )
    await db_session.refresh(project)
    assert project.directions[0].get("outcome_scaffolding_flagged") is True

    # Second pass: flag already set, no new memo
    await _process_direction_lifecycle(
        project,
        db_session,
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
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
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
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
    d["outcome_scaffolding_flagged_at"] = datetime.utcnow().isoformat()
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
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
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
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
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
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
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
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
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
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
    )
    await db_session.refresh(project)
    assert project.directions[0].get("outcome_scaffolding_flagged_cycles") == 3
    escalate_notify.assert_awaited_once()
    await _process_direction_lifecycle(
        project,
        db_session,
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d.get("outcome_scaffolding_flagged_cycles") == 3, (
        "counter must stay pinned when no new cycle completed after escalation"
    )
    escalate_notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_scaffolding_auto_retires_at_high_threshold(db_session, mocker):
    """Direction is auto-retired when flagged_cycles reaches max_flagged_cycles (default 6)."""
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

    active_directions = await _process_direction_lifecycle(
        project,
        db_session,
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "completed", (
        "direction should be auto-retired (status=completed) after max_flagged_cycles"
    )
    assert d.get("auto_completed") is True
    reason = d.get("auto_completed_reason", "")
    assert "scaffolding" in reason.lower(), (
        f"auto_completed_reason should mention scaffolding, got: {reason!r}"
    )
    milestone_notify.assert_awaited_once()
    kwargs = milestone_notify.call_args.kwargs
    assert kwargs.get("milestone") == "auto-retired-scaffolding"
    # Retired direction must NOT be returned in active_directions — otherwise the
    # downstream greedy selection could spawn a research cycle on a terminal
    # direction in the same orchestrator tick.
    assert did not in {ad.get("id") for ad in active_directions}, (
        "auto-retired direction must be excluded from active_directions"
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
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
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
        cooldown_minutes=0.0,
        busy_direction_ids=set(),
        now=datetime.utcnow(),
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
            "timestamp": (datetime.utcnow() - timedelta(hours=h)).isoformat(),
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


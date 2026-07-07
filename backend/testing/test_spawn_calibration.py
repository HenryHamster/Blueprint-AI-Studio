import pytest

from app.models import Project, Task, TaskStatus
from app.spawn_calibration import (
    _assess_project_spawn_health,
    _calibrate_scope,
    _feedback_scope_override,
    _get_failure_coaching_suffix,
    _poor_decomp_history,
    predict_spawn_success,
)


@pytest.mark.asyncio
async def test_over_budget_active_project_tasks_degrade_spawn_health(
    db_session, mocker, monkeypatch
):
    """D14: queued/in-flight backlog blocks additional implementation dispatch."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    mocker.patch("app.compute.any_zone_available", return_value=True)
    project = Project(
        name="D14 backlog",
        user_id="u-d14",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.QUEUED,
    ]
    for idx, status in enumerate(statuses):
        db_session.add(
            Task(
                project_id=project.id,
                title=f"active task {idx}",
                payloads=[{"type": "create", "prompt": "work"}],
                status=status,
                created_by="ORCHESTRATOR",
            )
        )
    await db_session.commit()

    degraded, reason = await _assess_project_spawn_health(
        db_session,
        project.id,
        design_cap=2,
    )

    assert degraded is True
    assert reason is not None
    assert "active task backlog" in reason


# --- _get_failure_coaching_suffix ---


def _make_dfa(direction_id: str, counts: dict) -> dict:
    """Build a minimal design_doc with direction_first_attempt_feedback."""
    return {"direction_first_attempt_feedback": {direction_id: counts}}


def test_failure_coaching_no_design_doc():
    assert _get_failure_coaching_suffix("d1", {}) is None
    assert _get_failure_coaching_suffix("d1", None) is None


def test_failure_coaching_missing_direction_entry():
    doc = _make_dfa(
        "other_dir", {"code_defect": 5, "total": 5, "last_updated": "2026-01-01"}
    )
    assert _get_failure_coaching_suffix("d1", doc) is None


def test_failure_coaching_total_below_threshold():
    # Only 2 rejections — below the minimum of 3.
    doc = _make_dfa("d1", {"code_defect": 2, "total": 2, "last_updated": "2026-01-01"})
    assert _get_failure_coaching_suffix("d1", doc) is None


def test_failure_coaching_no_dominant_category():
    # 4 total across 3 categories — max share is 2/4 = 50%? No: 2+1+1=4, max=2/4=0.5 ≥ 0.4
    # Use a more evenly distributed case: 3 categories of 1 each = 3 total, max share 1/3 < 40%.
    doc = _make_dfa(
        "d1",
        {
            "code_defect": 1,
            "scope_related": 1,
            "review_quality": 1,
            "total": 3,
            "last_updated": "2026-01-01",
        },
    )
    assert _get_failure_coaching_suffix("d1", doc) is None


def test_failure_coaching_dominant_category_returned():
    # 4 code_defect out of 5 total = 80% — well above 40%.
    doc = _make_dfa(
        "d1",
        {
            "code_defect": 4,
            "scope_related": 1,
            "total": 5,
            "last_updated": "2026-01-01",
        },
    )
    result = _get_failure_coaching_suffix("d1", doc)
    assert result is not None
    assert "code_defect" in result
    assert "4/5" in result


def test_failure_coaching_exactly_at_threshold():
    # 2 out of 5 = 40% — exactly at threshold, should return coaching.
    doc = _make_dfa(
        "d1",
        {
            "scope_related": 2,
            "code_defect": 1,
            "review_quality": 1,
            "other": 1,
            "total": 5,
            "last_updated": "2026-01-01",
        },
    )
    result = _get_failure_coaching_suffix("d1", doc)
    assert result is not None
    assert "scope_related" in result


def test_failure_coaching_excludes_total_and_last_updated_from_counts():
    # If "total" and "last_updated" were not excluded they would skew the computation.
    doc = _make_dfa(
        "d1",
        {
            "code_defect": 4,
            "total": 10,  # should be ignored
            "last_updated": "2026-01-01",  # should be ignored (non-numeric anyway)
        },
    )
    result = _get_failure_coaching_suffix("d1", doc)
    # Only code_defect=4, total from cat_counts=4 >= 3, share=100% >= 40%
    assert result is not None
    assert "code_defect" in result
    assert "4/4" in result


# --- _feedback_scope_override and _calibrate_scope ---


def _make_project(design_doc: dict):
    """Minimal project-like object for scope-override tests (no DB needed)."""

    class _P:
        directions = []

    p = _P()
    p.design_doc = design_doc
    return p


def test_feedback_scope_override_downscopes_at_threshold():
    """Exactly 3 scope_related first-attempt rejections → downscope medium→small."""
    proj = _make_project(
        {
            "direction_first_attempt_feedback": {
                "dir1": {"scope_related": 3, "total": 3, "last_updated": "2026-01-01"}
            }
        }
    )
    scope, calibrated_from = _feedback_scope_override(
        "medium", 1, {"id": "dir1"}, proj, None
    )
    assert scope == "small"
    assert calibrated_from == "medium"


def test_feedback_scope_override_no_downscope_below_threshold():
    """Only 2 scope_related rejections (below 3) → no override."""
    proj = _make_project(
        {
            "direction_first_attempt_feedback": {
                "dir1": {"scope_related": 2, "total": 2, "last_updated": "2026-01-01"}
            }
        }
    )
    assert _feedback_scope_override("medium", 1, {"id": "dir1"}, proj, None) is None


def test_feedback_scope_override_no_downscope_at_smallest_scope():
    """idx=0 (already at 'small') → downscope guard is skipped regardless of counts."""
    proj = _make_project(
        {
            "direction_first_attempt_feedback": {
                "dir1": {"scope_related": 5, "total": 5, "last_updated": "2026-01-01"}
            }
        }
    )
    # idx=0 means there is no smaller scope; the guard `if idx > 0` must prevent downscoping.
    assert _feedback_scope_override("small", 0, {"id": "dir1"}, proj, None) is None


def test_feedback_scope_override_retains_scope_on_strong_success_pattern():
    """success_count >= 5 for task_type/scope → retain proposed scope."""
    proj = _make_project({"playbook/success-create-small": {"success_count": 5}})
    scope, calibrated_from = _feedback_scope_override(
        "small", 0, {"id": "d2"}, proj, "create"
    )
    assert scope == "small"
    assert calibrated_from is None


def test_feedback_scope_override_no_retain_weak_success_pattern():
    """success_count < 5 → success-pattern guard does not fire; returns None."""
    proj = _make_project({"playbook/success-create-small": {"success_count": 4}})
    assert _feedback_scope_override("small", 0, {"id": "d2"}, proj, "create") is None


def test_feedback_scope_override_no_retain_missing_task_type():
    """task_type=None → success-pattern check is skipped entirely."""
    proj = _make_project({"playbook/success-create-small": {"success_count": 10}})
    # Without task_type the playbook key cannot be formed, so no retention override.
    assert _feedback_scope_override("small", 0, {"id": "d2"}, proj, None) is None


def test_calibrate_scope_downscopes_via_failure_feedback_coldstart():
    """_calibrate_scope propagates failure-feedback downscope when history is sparse."""
    proj = _make_project(
        {
            "direction_first_attempt_feedback": {
                "dir3": {"scope_related": 4, "total": 4, "last_updated": "2026-01-01"}
            }
        }
    )
    direction = {"id": "dir3", "title": "T", "research_history": []}
    scope, calibrated_from = _calibrate_scope("medium", direction, proj, "create")
    assert scope == "small"
    assert calibrated_from == "medium"


def test_calibrate_scope_success_pattern_retained_coldstart():
    """_calibrate_scope returns the success-pattern scope intact when evidence is strong."""
    proj = _make_project({"playbook/success-create-small": {"success_count": 7}})
    direction = {"id": "dir4", "title": "T", "research_history": []}
    # With no failure feedback and strong success-pattern evidence, scope is retained.
    scope, calibrated_from = _calibrate_scope("small", direction, proj, "create")
    assert scope == "small"
    assert calibrated_from is None


def test_feedback_scope_override_retains_scope_on_direction_affinity():
    """2+ direction-local successes retain scope even when global success_count < 5."""
    proj = _make_project(
        {
            "playbook/success-create-small": {
                "success_count": 3,  # below global threshold of 5
                "direction_successes": {"dir-local": 2},
            }
        }
    )
    scope, calibrated_from = _feedback_scope_override(
        "small", 0, {"id": "dir-local"}, proj, "create"
    )
    assert scope == "small"
    assert calibrated_from is None


def test_feedback_scope_override_no_retain_on_single_direction_success():
    """Only 1 direction-local success (below threshold of 2) does not retain scope."""
    proj = _make_project(
        {
            "playbook/success-create-small": {
                "success_count": 3,
                "direction_successes": {"dir-local": 1},
            }
        }
    )
    assert (
        _feedback_scope_override("small", 0, {"id": "dir-local"}, proj, "create")
        is None
    )


def test_feedback_scope_override_ignores_malformed_direction_successes():
    """Malformed persisted direction_successes does not crash and is treated as empty."""
    proj = _make_project(
        {
            "playbook/success-create-small": {
                "success_count": 3,
                "direction_successes": "bad",
            }
        }
    )
    assert (
        _feedback_scope_override("small", 0, {"id": "dir-local"}, proj, "create")
        is None
    )


# --- aggregate scope_breakdown / decomposition_quality reader tests ---


def _make_scope_breakdown(created: int, merged: int, failed: int = 0) -> dict:
    return {
        "created": created,
        "merged": merged,
        "failed": failed,
        "failed_infra": 0,
        "failed_scope_overflow": 0,
    }


def test_calibrate_scope_uses_aggregate_scope_breakdown_when_live_entries_sparse():
    """Direction with 1 live entry but history_aggregate having >= 3 data points
    should use direction-level calibration (40% threshold), not fall back to
    project-wide aggregation.
    """
    # 1 live entry: 1 medium created, 0 merged → rate=0, below 40% threshold
    # Aggregate: 4 medium tasks (3 merged, 1 failed) → rate=75%, healthy
    # Without the fix, direction_data_points=1 < 3 → project-wide fallback with no data → cold-start
    # With the fix, aggregate adds 4 created → direction_data_points=5 >= 3 → direction-level,
    # rate=75% ≥ 40% → retain medium scope
    direction = {
        "id": "d-agg",
        "title": "Agg test",
        "research_history": [
            {
                "tasks_created": 1,
                "scope_breakdown": {"medium": _make_scope_breakdown(1, 0, 1)},
            }
        ],
        "history_aggregate": {
            "scope_breakdown": {"medium": _make_scope_breakdown(4, 3, 1)},
        },
    }
    scope, calibrated_from = _calibrate_scope("medium", direction)
    # direction-level: combined (1+4)=5 created, (0+3)=3 merged → rate=3/(3+1)=75% >= 40%
    assert scope == "medium"
    assert calibrated_from is None


def test_calibrate_scope_upgrades_to_large_when_aggregate_medium_poor_small_healthy():
    """Direction with poor medium aggregate but healthy small aggregate upgrades to large
    (for decompose routing), not downscopes to small. This matches the existing logic:
    medium fails + small healthy → large(decompose).
    """
    # Aggregate: 4 medium tasks, 0 merged → rate=0% < 40%
    # Small aggregate: 4 tasks, 3 merged → rate=75%, healthy
    # Expected: medium→large upgrade (decompose path), not medium→small downscope
    direction = {
        "id": "d-upgrade",
        "title": "Upgrade via aggregate",
        "research_history": [],
        "history_aggregate": {
            "scope_breakdown": {
                "medium": _make_scope_breakdown(4, 0, 4),
                "small": _make_scope_breakdown(4, 3, 1),
            }
        },
    }
    scope, calibrated_from = _calibrate_scope("medium", direction)
    assert scope == "large"
    assert calibrated_from == "medium"


def test_calibrate_scope_downscopes_to_small_when_aggregate_shows_both_poor():
    """Direction with poor medium AND poor small aggregate rates downscopes to small
    (no healthy scope found at small; falls through to the smallest scope with acceptable rate).
    """
    # Aggregate: medium 4 tasks, 0 merged → rate=0% < 40%
    # Small aggregate: 4 tasks, 1 merged → rate=25% < 40% → also poor
    # The downgrade loop checks small: effective=1+3=4? No: effective=merged+failed-infra_excl_overflow
    # merged=1, failed=3, failed_infra=0, failed_scope_overflow=0 → effective=1+3-0=4
    # rate=1/4=25% < 40% → but the loop checks: if smaller_rate is None or smaller_rate >= threshold
    # 25% < 40% → doesn't qualify → no downscope → returns medium (original scope, unchanged)
    # Actually: the loop returns the first smaller scope with rate >= threshold OR rate is None
    # Since small rate=25% < 40% → rate is NOT None and NOT >= threshold → skip
    # Loop exhausted → return proposed_scope, None (unchanged)
    direction = {
        "id": "d-both-poor",
        "title": "Both poor via aggregate",
        "research_history": [],
        "history_aggregate": {
            "scope_breakdown": {
                "medium": _make_scope_breakdown(4, 0, 4),
                "small": _make_scope_breakdown(4, 1, 3),
            }
        },
    }
    scope, calibrated_from = _calibrate_scope("medium", direction)
    # Both small and medium are poor; falls through to `return proposed_scope, None`
    assert scope == "medium"
    assert calibrated_from is None


def test_predict_spawn_success_incorporates_aggregate_scope_breakdown():
    """Direction with no live research_history but aggregate scope data gets Signal 1
    from the aggregate, not a fallback of 0 data points (which would be skipped).
    """
    # Aggregate: 5 medium tasks, 5 merged → rate=100%; effective=5 >= 2 → Signal 1 fires
    direction = {
        "id": "d-sig1",
        "title": "Signal 1 agg",
        "research_history": [],
        "history_aggregate": {
            "scope_breakdown": {"medium": _make_scope_breakdown(5, 5)},
        },
    }
    score = predict_spawn_success("medium", direction)
    # With aggregate data, Signal 1 fires with rate=1.0. Without pipeline_scope_merge_rates
    # in runtime_config (default None), only Signal 1 contributes → score=1.0.
    assert score > 0.5, f"Expected score > 0.5, got {score}"


def test_predict_spawn_success_poor_aggregate_scope_lowers_score():
    """Direction with only poor-rate aggregate scope data gets a low Signal 1 score."""
    direction = {
        "id": "d-sig1-poor",
        "title": "Signal 1 poor",
        "research_history": [],
        "history_aggregate": {
            "scope_breakdown": {"medium": _make_scope_breakdown(5, 0, 5)},
        },
    }
    score = predict_spawn_success("medium", direction)
    # Signal 1: rate=0.0, weight=2.0. Without other signals, weighted avg = 0.0
    # (but clamped to ≥ 0.0; the neutral fallback is 0.5 only when signals list is empty)
    assert score < 0.5, f"Expected score < 0.5, got {score}"


def test_predict_spawn_success_incorporates_aggregate_decomp_quality():
    """Large-scope spawn with no live decomp history but aggregate decomp data uses it
    for Signal 5.
    """
    # Aggregate decomp: 6 subtasks with merge rate 0.9 → weighted sum = 5.4
    direction = {
        "id": "d-sig5",
        "title": "Signal 5 agg",
        "research_history": [],
        "history_aggregate": {
            "decomposition_quality": {
                "subtask_merge_rate_weighted_sum": 5.4,
                "subtask_count_sum": 6,
                "total_decompositions": 2,
                "cross_module_count_sum": 0,
            }
        },
    }
    score = predict_spawn_success("large", direction)
    # Signal 5: rate = 5.4/6 = 0.9, weight=1.5 → score biased high
    assert score > 0.5, f"Expected score > 0.5 from healthy agg decomp, got {score}"


def test_predict_spawn_success_poor_aggregate_decomp_lowers_score():
    """Large-scope spawn with poor aggregate decomp rate gets a low Signal 5."""
    direction = {
        "id": "d-sig5-poor",
        "title": "Signal 5 poor",
        "research_history": [],
        "history_aggregate": {
            "decomposition_quality": {
                "subtask_merge_rate_weighted_sum": 0.0,
                "subtask_count_sum": 6,
                "total_decompositions": 2,
                "cross_module_count_sum": 0,
            }
        },
    }
    score = predict_spawn_success("large", direction)
    # Signal 5: rate=0.0/6=0.0, weight=1.5 → score biased low
    assert score < 0.5, f"Expected score < 0.5 from poor agg decomp, got {score}"


def test_poor_decomp_history_incorporates_aggregate_decomp():
    """_poor_decomp_history returns True when live data is empty but aggregate shows
    a bad weighted-average subtask_merge_rate (< 0.3 over >= 3 subtasks).
    """
    direction = {
        "research_history": [],
        "history_aggregate": {
            "decomposition_quality": {
                "subtask_merge_rate_weighted_sum": 0.5,  # 0.5/5 = 10% avg
                "subtask_count_sum": 5,
                "total_decompositions": 2,
                "cross_module_count_sum": 0,
            }
        },
    }
    assert _poor_decomp_history(direction) is True


def test_poor_decomp_history_healthy_aggregate_returns_false():
    """_poor_decomp_history returns False when aggregate shows a healthy merge rate."""
    direction = {
        "research_history": [],
        "history_aggregate": {
            "decomposition_quality": {
                "subtask_merge_rate_weighted_sum": 2.1,  # 2.1/3 = 70% avg
                "subtask_count_sum": 3,
                "total_decompositions": 1,
                "cross_module_count_sum": 0,
            }
        },
    }
    assert _poor_decomp_history(direction) is False


def test_poor_decomp_history_aggregate_below_min_subtasks_returns_false():
    """_poor_decomp_history returns False when total subtasks (live + agg) < 3."""
    direction = {
        "research_history": [],
        "history_aggregate": {
            "decomposition_quality": {
                "subtask_merge_rate_weighted_sum": 0.1,
                "subtask_count_sum": 2,  # below threshold of 3
                "total_decompositions": 1,
                "cross_module_count_sum": 0,
            }
        },
    }
    assert _poor_decomp_history(direction) is False


def test_poor_decomp_history_live_and_agg_combined():
    """_poor_decomp_history combines live and aggregate subtask counts correctly."""
    # Live: 2 subtasks at rate 0.8 (healthy)
    # Aggregate: 4 subtasks at weighted sum 0.2 (0.2/4 = 5% avg, very bad)
    # Combined: ws = 0.8*2 + 0.2 = 1.8, total = 6 → 1.8/6 = 30% = exactly at threshold
    # threshold check: ws/total >= 0.3 → False (returns False, not poor)
    direction = {
        "research_history": [
            {
                "decomposition_quality": [
                    {"subtask_merge_rate": 0.8, "subtask_count": 2}
                ]
            }
        ],
        "history_aggregate": {
            "decomposition_quality": {
                "subtask_merge_rate_weighted_sum": 0.2,
                "subtask_count_sum": 4,
                "total_decompositions": 1,
                "cross_module_count_sum": 0,
            }
        },
    }
    # ws = 0.8*2 + 0.2 = 1.8, total = 2 + 4 = 6, rate = 0.3 → not < 0.3 → False
    assert _poor_decomp_history(direction) is False


def test_poor_decomp_history_live_and_agg_poor_combined():
    """Combined live + agg subtask rate below 0.3 returns True."""
    # Live: 1 subtask at rate 0.2
    # Aggregate: 4 subtasks at weighted sum 0.4 (0.1 avg)
    # Combined: ws = 0.2 + 0.4 = 0.6, total = 5 → 0.6/5 = 12% < 30% → True
    direction = {
        "research_history": [
            {
                "decomposition_quality": [
                    {"subtask_merge_rate": 0.2, "subtask_count": 1}
                ]
            }
        ],
        "history_aggregate": {
            "decomposition_quality": {
                "subtask_merge_rate_weighted_sum": 0.4,
                "subtask_count_sum": 4,
                "total_decompositions": 1,
                "cross_module_count_sum": 0,
            }
        },
    }
    assert _poor_decomp_history(direction) is True

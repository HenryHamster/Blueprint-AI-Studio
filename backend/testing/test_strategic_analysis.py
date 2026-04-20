"""Tests for _build_outcome_evaluation in app.strategic_analysis."""
import pytest
from unittest.mock import patch
from app.strategic_analysis import _build_outcome_evaluation
from app.direction_metrics import _compute_difficulty_index


def _make_project(directions=None, vision=None):
    """Build a minimal project-like object."""
    class Project:
        pass
    p = Project()
    p.directions = directions or []
    p.vision = vision or {}
    return p


# ---------------------------------------------------------------------------
# 1. Empty directions
# ---------------------------------------------------------------------------

def test_empty_directions_returns_defaults():
    project = _make_project()
    result = _build_outcome_evaluation(project)

    assert result["overall"]["cycles"] == 0
    assert result["overall"]["merged"] == 0
    assert result["overall"]["failed"] == 0
    assert result["overall"]["success_rate"] == 0.0
    assert result["overall"]["trend"] == "stable"
    assert result["window"]["from"] == "all_time"
    assert result["window"]["vision_revision"] == 0
    assert "directions" not in result
    assert "systems" not in result


# ---------------------------------------------------------------------------
# 2. History entries — basic aggregation
# ---------------------------------------------------------------------------

def test_history_aggregation():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 3, "prs_rejected": 1,
                 "tasks_failed": 0, "tasks_created": 5},
                {"timestamp": "2026-01-02T00:00:00", "prs_merged": 2, "prs_rejected": 0,
                 "tasks_failed": 1, "tasks_created": 4},
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)

    assert result["overall"]["merged"] == 5
    assert result["overall"]["failed"] == 1
    assert result["overall"]["cycles"] == 2

    d = result["directions"][0]
    assert d["merged"] == 5
    assert d["failed"] == 1
    assert d["cycles"] == 2
    assert d["acceptance_rate"] == round(5 / 6, 3)  # merged=5, rejected=1, total=6


def test_history_acceptance_rate():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 2, "prs_rejected": 2},
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    d = result["directions"][0]
    assert d["acceptance_rate"] == 0.5


def test_no_prs_omits_acceptance_rate():
    directions = [
        {"id": "dir-1", "status": "active", "research_history": [
            {"timestamp": "2026-01-01T00:00:00", "prs_merged": 0, "prs_rejected": 0},
        ]}
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    assert "acceptance_rate" not in result["directions"][0]


# ---------------------------------------------------------------------------
# 3. failed_approaches in research_context
# ---------------------------------------------------------------------------

def test_failed_approaches_appear_in_output():
    research_ctx = (
        "## Established Findings\n"
        "- Finding A\n"
        "## Failed Approaches\n"
        "- Approach X\n"
        "- Approach Y\n"
    )
    directions = [
        {"id": "dir-1", "status": "active", "research_context": research_ctx, "research_history": []}
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    d = result["directions"][0]
    assert "failed_approaches" in d
    assert "Approach X" in d["failed_approaches"]
    assert "Approach Y" in d["failed_approaches"]


# ---------------------------------------------------------------------------
# 4. scope_breakdown / type_distribution
# ---------------------------------------------------------------------------

def test_type_distribution_and_product_ratio():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 1,
                    "tasks_created": 4,
                    "type_distribution": {"create": 3, "fix": 1},
                    "product_ratio": 0.8,
                },
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    d = result["directions"][0]
    assert d["type_distribution"] == {"create": 3, "fix": 1}
    assert d["product_ratio"] == 0.8


# ---------------------------------------------------------------------------
# 5. Multiple directions — aggregate metrics
# ---------------------------------------------------------------------------

def test_multiple_directions_aggregate():
    directions = [
        {
            "id": "dir-A",
            "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 3, "tasks_failed": 1},
            ],
        },
        {
            "id": "dir-B",
            "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1, "tasks_failed": 3},
            ],
        },
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)

    assert result["overall"]["merged"] == 4
    assert result["overall"]["failed"] == 4
    assert result["overall"]["success_rate"] == round(4 / 8, 2)
    assert len(result["directions"]) == 2


# ---------------------------------------------------------------------------
# 6. operator_rejection_rate — only when rejected > 0
# ---------------------------------------------------------------------------

def test_operator_rejection_rate_computed_when_rejections():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 3, "prs_rejected": 1},
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    d = result["directions"][0]
    assert "operator_rejection_rate" in d
    assert d["operator_rejection_rate"] == round(1 / (3 + 1), 2)


def test_operator_rejection_rate_absent_when_no_rejections():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 3, "prs_rejected": 0},
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    d = result["directions"][0]
    assert "operator_rejection_rate" not in d


# ---------------------------------------------------------------------------
# 7. Trend calculation
# ---------------------------------------------------------------------------

def test_trend_improving():
    # second half has much higher merge rate than first half
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 0},
                {"timestamp": "2026-01-02T00:00:00", "prs_merged": 0},
                {"timestamp": "2026-01-03T00:00:00", "prs_merged": 5},
                {"timestamp": "2026-01-04T00:00:00", "prs_merged": 5},
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    assert result["overall"]["trend"] == "improving"


def test_trend_declining():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 5},
                {"timestamp": "2026-01-02T00:00:00", "prs_merged": 5},
                {"timestamp": "2026-01-03T00:00:00", "prs_merged": 0},
                {"timestamp": "2026-01-04T00:00:00", "prs_merged": 0},
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    assert result["overall"]["trend"] == "declining"


def test_trend_stable_fewer_than_4_entries():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1},
                {"timestamp": "2026-01-02T00:00:00", "prs_merged": 100},
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    assert result["overall"]["trend"] == "stable"


# ---------------------------------------------------------------------------
# 8. Window filter respects vision last_updated
# ---------------------------------------------------------------------------

def test_window_filter_excludes_old_history():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {"timestamp": "2025-01-01T00:00:00", "prs_merged": 10},  # before window
                {"timestamp": "2026-03-01T00:00:00", "prs_merged": 2},   # in window
            ],
        }
    ]
    project = _make_project(
        directions=directions,
        vision={"last_updated": "2026-01-01T00:00:00", "revision": 3},
    )
    result = _build_outcome_evaluation(project)
    assert result["overall"]["merged"] == 2
    assert result["window"]["from"] == "2026-01-01T00:00:00"
    assert result["window"]["vision_revision"] == 3


# ---------------------------------------------------------------------------
# 9. Systems cross-reference
# ---------------------------------------------------------------------------

def test_systems_cross_reference():
    directions = [
        {"id": "dir-1", "status": "active", "system_id": "sys-A",
         "research_history": [{"timestamp": "2026-01-01T00:00:00", "prs_merged": 2, "tasks_failed": 1}]},
    ]
    vision = {"systems": [{"id": "sys-A", "name": "Auth", "status": "in_progress"}]}
    project = _make_project(directions=directions, vision=vision)
    result = _build_outcome_evaluation(project)

    assert "systems" in result
    sys_entry = result["systems"][0]
    assert sys_entry["id"] == "sys-A"
    assert sys_entry["name"] == "Auth"
    assert sys_entry["merged"] == 2
    assert sys_entry["failed"] == 1
    assert sys_entry["directions_count"] == 1


# ---------------------------------------------------------------------------
# 10. Directions with inactive statuses are skipped
# ---------------------------------------------------------------------------

def test_inactive_directions_skipped():
    directions = [
        {"id": "dir-active", "status": "active",
         "research_history": [{"timestamp": "2026-01-01T00:00:00", "prs_merged": 1}]},
        {"id": "dir-archived", "status": "archived",
         "research_history": [{"timestamp": "2026-01-01T00:00:00", "prs_merged": 99}]},
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    assert result["overall"]["merged"] == 1
    assert len(result["directions"]) == 1


# ---------------------------------------------------------------------------
# 11. confirmed / proposed findings from research_context
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 12. Failure taxonomy — tasks_failed_infra and taxonomy_summary
# ---------------------------------------------------------------------------

def test_taxonomy_summary_aggregated_across_history():
    directions = [
        {
            "id": "dir-1", "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "tasks_failed": 3,
                 "tasks_failed_infra": 2,
                 "taxonomy_summary": {"infra_related": 2, "code_defect": 1}},
                {"timestamp": "2026-01-02T00:00:00", "tasks_failed": 2,
                 "tasks_failed_infra": 0,
                 "taxonomy_summary": {"review_quality": 1, "code_defect": 1}},
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    d = result["directions"][0]
    assert d["tasks_failed_infra"] == 2
    assert d["taxonomy_summary"] == {
        "infra_related": 2, "code_defect": 2, "review_quality": 1,
    }


def test_taxonomy_summary_absent_when_all_zero():
    directions = [
        {"id": "dir-1", "status": "active", "research_history": [
            {"timestamp": "2026-01-01T00:00:00", "tasks_failed": 0,
             "tasks_failed_infra": 0,
             "taxonomy_summary": {"code_defect": 0, "infra_related": 0}},
        ]}
    ]
    project = _make_project(directions=directions)
    d = _build_outcome_evaluation(project)["directions"][0]
    assert "tasks_failed_infra" not in d
    assert "taxonomy_summary" not in d


def test_taxonomy_summary_respects_window_filter():
    directions = [
        {
            "id": "dir-1", "status": "active",
            "research_history": [
                {"timestamp": "2025-01-01T00:00:00", "tasks_failed_infra": 10,
                 "taxonomy_summary": {"infra_related": 10}},  # outside window
                {"timestamp": "2026-03-01T00:00:00", "tasks_failed_infra": 1,
                 "taxonomy_summary": {"review_quality": 2}},  # inside window
            ],
        }
    ]
    project = _make_project(
        directions=directions,
        vision={"last_updated": "2026-01-01T00:00:00", "revision": 1},
    )
    d = _build_outcome_evaluation(project)["directions"][0]
    assert d["tasks_failed_infra"] == 1
    assert d["taxonomy_summary"] == {"review_quality": 2}


# ---------------------------------------------------------------------------
# Per-direction trend and velocity
# ---------------------------------------------------------------------------

def test_per_direction_trend_improving():
    directions = [{
        "id": "dir-1", "status": "active",
        "research_history": [
            {"timestamp": "2026-01-01T00:00:00", "prs_merged": 0},
            {"timestamp": "2026-01-02T00:00:00", "prs_merged": 1},
            {"timestamp": "2026-01-03T00:00:00", "prs_merged": 4},
            {"timestamp": "2026-01-04T00:00:00", "prs_merged": 5},
        ],
    }]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["trend"] == "improving"


def test_per_direction_trend_declining():
    directions = [{
        "id": "dir-1", "status": "active",
        "research_history": [
            {"timestamp": "2026-01-01T00:00:00", "prs_merged": 5},
            {"timestamp": "2026-01-02T00:00:00", "prs_merged": 4},
            {"timestamp": "2026-01-03T00:00:00", "prs_merged": 1},
            {"timestamp": "2026-01-04T00:00:00", "prs_merged": 0},
        ],
    }]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["trend"] == "declining"


def test_per_direction_trend_absent_for_fewer_than_4_cycles():
    directions = [{
        "id": "dir-1", "status": "active",
        "research_history": [
            {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1},
            {"timestamp": "2026-01-02T00:00:00", "prs_merged": 10},
            {"timestamp": "2026-01-03T00:00:00", "prs_merged": 10},
        ],
    }]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert "trend" not in d


def test_per_direction_velocity_accelerating():
    # Lifetime avg = 8/4 = 2.0; recent 2 avg = (1+5)/2 = 3.0; velocity = 1.5
    directions = [{
        "id": "dir-1", "status": "active",
        "research_history": [
            {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1},
            {"timestamp": "2026-01-02T00:00:00", "prs_merged": 1},
            {"timestamp": "2026-01-03T00:00:00", "prs_merged": 1},
            {"timestamp": "2026-01-04T00:00:00", "prs_merged": 5},
        ],
    }]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["velocity"] == 1.5


def test_per_direction_velocity_decelerating():
    # Lifetime avg = 12/4 = 3.0; recent 2 avg = (1+1)/2 = 1.0; velocity ≈ 0.33
    directions = [{
        "id": "dir-1", "status": "active",
        "research_history": [
            {"timestamp": "2026-01-01T00:00:00", "prs_merged": 5},
            {"timestamp": "2026-01-02T00:00:00", "prs_merged": 5},
            {"timestamp": "2026-01-03T00:00:00", "prs_merged": 1},
            {"timestamp": "2026-01-04T00:00:00", "prs_merged": 1},
        ],
    }]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["velocity"] == 0.33
    assert d["velocity"] < 1.0


def test_confirmed_and_proposed_findings():
    research_ctx = (
        "## Established Findings\n"
        "- Finding plain\n"
        "- Finding confirmed (confirmed)\n"
        "- Finding proposed (proposed)\n"
    )
    directions = [
        {"id": "dir-1", "status": "active", "research_context": research_ctx, "research_history": []}
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    d = result["directions"][0]
    assert "confirmed" in d
    assert "proposed" in d
    # "Finding plain" and "Finding confirmed" should be in confirmed
    assert any("Finding plain" in c for c in d["confirmed"])
    assert any("Finding confirmed" in c for c in d["confirmed"])
    assert any("Finding proposed" in c for c in d["proposed"])


# ---------------------------------------------------------------------------
# difficulty_index
# ---------------------------------------------------------------------------

def test_difficulty_index_no_history_defaults_to_one():
    assert _compute_difficulty_index({}) == 1.0


def test_difficulty_index_no_merges_defaults_to_one():
    d = {"research_history": [{"tasks_created": 10, "prs_merged": 0}]}
    assert _compute_difficulty_index(d) == 1.0


def test_difficulty_index_all_merges():
    d = {"research_history": [{"tasks_created": 3, "prs_merged": 3}]}
    assert _compute_difficulty_index(d) == 1.0


def test_difficulty_index_mixed_uses_history_and_aggregate():
    d = {
        "research_history": [{"tasks_created": 10, "prs_merged": 2}],
        "history_aggregate": {"total_tasks_created": 40, "total_prs_merged": 3},
    }
    # (10+40)/(2+3) = 10.0
    assert _compute_difficulty_index(d) == 10.0


def test_difficulty_index_capped_at_thirty():
    d = {"research_history": [{"tasks_created": 1000, "prs_merged": 1}]}
    assert _compute_difficulty_index(d) == 30.0


def test_cost_estimate_per_direction_and_overall():
    from app.routers.admin_analytics import ON_DEMAND_RATE
    directions = [
        {
            "id": "dir-cheap", "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 4,
                 "tasks_created": 4, "avg_time_to_merge_hours": 1.0},
            ],
        },
        {
            "id": "dir-pricey", "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1,
                 "tasks_created": 5, "avg_time_to_merge_hours": 4.0},
            ],
        },
    ]
    result = _build_outcome_evaluation(_make_project(directions=directions))
    by_id = {d["id"]: d for d in result["directions"]}

    cheap_cost = round(4 * 1.0 * 3600 * ON_DEMAND_RATE, 2)
    pricey_cost = round(5 * 4.0 * 3600 * ON_DEMAND_RATE, 2)
    assert by_id["dir-cheap"]["estimated_cost_usd"] == cheap_cost
    assert by_id["dir-cheap"]["cost_per_merged_pr"] == round(cheap_cost / 4, 2)
    assert by_id["dir-pricey"]["estimated_cost_usd"] == pricey_cost
    assert by_id["dir-pricey"]["cost_per_merged_pr"] == round(pricey_cost / 1, 2)

    overall = result["overall"]
    assert overall["estimated_cost_usd"] == round(cheap_cost + pricey_cost, 2)
    ranking = overall["cost_efficiency_ranking"]
    assert [r["id"] for r in ranking] == ["dir-cheap", "dir-pricey"]


def test_cost_estimate_absent_when_no_runtime_data():
    directions = [
        {"id": "dir-1", "status": "active", "research_history": [
            {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1, "tasks_created": 2},
        ]}
    ]
    result = _build_outcome_evaluation(_make_project(directions=directions))
    assert "estimated_cost_usd" not in result["directions"][0]
    assert "estimated_cost_usd" not in result["overall"]


def test_cost_per_merged_pr_omitted_when_no_merges():
    from app.routers.admin_analytics import ON_DEMAND_RATE  # noqa: F401
    directions = [
        {"id": "dir-1", "status": "active", "research_history": [
            {"timestamp": "2026-01-01T00:00:00", "prs_merged": 0,
             "tasks_created": 3, "avg_time_to_merge_hours": 2.0},
        ]}
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert "estimated_cost_usd" in d
    assert "cost_per_merged_pr" not in d


def test_scope_performance_aggregates_across_cycles():
    directions = [{
        "id": "dir-1", "status": "active",
        "research_history": [
            {
                "timestamp": "2026-01-01T00:00:00", "prs_merged": 2,
                "scope_breakdown": {
                    "small": {"created": 5, "merged": 3, "failed": 1},
                    "medium": {"created": 2, "merged": 0, "failed": 2},
                },
                "escalation_outcomes": {
                    "small_to_medium": {"attempted": 1, "merged": 1, "failed": 0},
                },
            },
            {
                "timestamp": "2026-01-02T00:00:00", "prs_merged": 1,
                "scope_breakdown": {
                    "small": {"created": 3, "merged": 2, "failed": 0},
                },
                "escalation_outcomes": {
                    "small_to_medium": {"attempted": 2, "merged": 1, "failed": 1},
                    "medium_to_large": {"attempted": 1, "merged": 0, "failed": 1},
                },
            },
        ],
    }]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["scope_performance"] == {
        "small": {"created": 8, "merged": 5, "failed": 1},
        "medium": {"created": 2, "merged": 0, "failed": 2},
    }
    assert d["escalation_outcomes"] == {
        "small_to_medium": {"attempted": 3, "merged": 2, "failed": 1},
        "medium_to_large": {"attempted": 1, "merged": 0, "failed": 1},
    }


def test_scope_performance_omits_empty_scopes():
    directions = [{
        "id": "dir-1", "status": "active",
        "research_history": [
            {"timestamp": "2026-01-01T00:00:00", "prs_merged": 0,
             "scope_breakdown": {
                 "small": {"created": 0, "merged": 0, "failed": 0},
                 "medium": {"created": 1, "merged": 0, "failed": 1},
             }},
        ],
    }]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["scope_performance"] == {"medium": {"created": 1, "merged": 0, "failed": 1}}


def test_scope_performance_absent_when_no_data():
    directions = [{
        "id": "dir-1", "status": "active",
        "research_history": [{"timestamp": "2026-01-01T00:00:00", "prs_merged": 1}],
    }]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert "scope_performance" not in d
    assert "escalation_outcomes" not in d


def test_difficulty_index_surfaced_in_outcome_evaluation():
    directions = [{
        "id": "dir-1",
        "status": "active",
        "research_history": [{"timestamp": "2026-01-01T00:00:00",
                              "tasks_created": 20, "prs_merged": 2}],
    }]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    assert result["directions"][0]["difficulty_index"] == 10.0


# ---------------------------------------------------------------------------
# strategic_effectiveness — cycle-over-cycle decision quality
# ---------------------------------------------------------------------------

def test_strategic_effectiveness_absent_without_cycle_history():
    project = _make_project(directions=[{"id": "d1", "status": "active",
                                          "research_history": [{"prs_merged": 1}]}])
    result = _build_outcome_evaluation(project)
    assert "strategic_effectiveness" not in result


def test_strategic_effectiveness_skips_in_progress_cycles():
    vision = {"_strategic_cycle_history": [
        {"cycle_id": "c1", "outcome": "in_progress"},
    ]}
    result = _build_outcome_evaluation(_make_project(vision=vision))
    assert "strategic_effectiveness" not in result


def test_strategic_effectiveness_aggregates_completed_cycles():
    directions = [
        {"id": "dA", "status": "active",
         "research_history": [{"prs_merged": 2}]},  # created → merged
        {"id": "dB", "status": "active",
         "research_history": [{"prs_merged": 0}]},  # created → no merges
        {"id": "dR", "status": "retired",
         "research_history": []},  # retired, still retired
    ]
    vision = {
        "_strategic_failures": 2,
        "_strategic_cycle_history": [
            {
                "cycle_id": "c1", "outcome": "completed",
                "phase2_completed_at": "2026-02-01T00:00:00",
                "created_direction_ids": ["dA", "dB"],
                "retired_direction_ids": ["dR"],
                "vision_updated": True,
            },
        ],
    }
    result = _build_outcome_evaluation(_make_project(directions=directions, vision=vision))
    eff = result["strategic_effectiveness"]
    assert eff["cycles_evaluated"] == 1
    assert eff["strategic_failures"] == 2
    cycle = eff["cycles"][0]
    assert cycle["created_with_merges"] == 1
    assert cycle["retired_still_retired"] == 1
    # 2 successes / 3 decisions = 0.67
    assert cycle["decision_conversion_rate"] == 0.67
    assert cycle["vision_updated"] is True
    assert eff["avg_decision_conversion_rate"] == 0.67


def test_strategic_effectiveness_trend_improving():
    directions = [
        {"id": f"d{i}", "status": "active",
         "research_history": [{"prs_merged": 1}]} for i in range(5)
    ]
    # Early cycles: 0% conversion. Later cycles: 100%.
    vision = {"_strategic_cycle_history": [
        {"cycle_id": "c1", "outcome": "completed",
         "created_direction_ids": ["missing1"], "retired_direction_ids": []},
        {"cycle_id": "c2", "outcome": "completed",
         "created_direction_ids": ["missing2"], "retired_direction_ids": []},
        {"cycle_id": "c3", "outcome": "completed",
         "created_direction_ids": ["d0", "d1"], "retired_direction_ids": []},
        {"cycle_id": "c4", "outcome": "completed",
         "created_direction_ids": ["d2", "d3"], "retired_direction_ids": []},
    ]}
    result = _build_outcome_evaluation(_make_project(directions=directions, vision=vision))
    assert result["strategic_effectiveness"]["trend"] == "improving"


def test_decision_accuracy_absent_without_completed_cycle():
    project = _make_project(directions=[{"id": "d1", "status": "active",
                                          "research_history": [{"prs_merged": 1}]}])
    assert "decision_accuracy" not in _build_outcome_evaluation(project)


def test_decision_accuracy_scores_created_directions():
    directions = [
        {"id": "dA", "status": "active",
         "research_history": [{"prs_merged": 3}]},  # success
        {"id": "dB", "status": "active", "auto_paused": True,
         "research_history": [{"prs_merged": 2}]},  # auto_paused → not success
        {"id": "dC", "status": "active",
         "research_history": [{"prs_merged": 0}]},  # no merges → not success
    ]
    vision = {"_strategic_cycle_history": [
        {"cycle_id": "c1", "outcome": "completed",
         "phase2_completed_at": "2026-03-01T00:00:00",
         "created_direction_ids": ["dA", "dB", "dC"],
         "retired_direction_ids": []},
    ]}
    result = _build_outcome_evaluation(_make_project(directions=directions, vision=vision))
    da = result["decision_accuracy"]
    assert da["cycle_id"] == "c1"
    assert da["created_count"] == 3
    assert da["created_successful"] == 1
    assert da["decision_accuracy_rate"] == round(1 / 3, 2)
    by_id = {c["direction_id"]: c for c in da["created"]}
    assert by_id["dA"]["success"] is True
    assert by_id["dB"]["success"] is False
    assert by_id["dB"]["auto_paused"] is True
    assert by_id["dC"]["success"] is False
    assert "median_quality_score" in da


def test_decision_accuracy_retired_breakdown():
    directions = [
        {"id": "dKeep", "status": "retired", "research_history": []},
        {"id": "dBack", "status": "active",
         "research_history": [{"prs_merged": 1}]},  # was retired but is active again
    ]
    vision = {"_strategic_cycle_history": [
        {"cycle_id": "c1", "outcome": "completed",
         "created_direction_ids": [],
         "retired_direction_ids": ["dKeep", "dBack", "dGone"]},
    ]}
    result = _build_outcome_evaluation(_make_project(directions=directions, vision=vision))
    da = result["decision_accuracy"]
    by_id = {r["direction_id"]: r for r in da["retired"]}
    assert by_id["dKeep"]["stayed_retired"] is True
    assert by_id["dBack"]["stayed_retired"] is False
    assert by_id["dGone"]["stayed_retired"] is True  # missing == stayed retired
    assert da["retired_accuracy_rate"] == round(2 / 3, 2)


def test_decision_accuracy_handles_missing_created_direction():
    vision = {"_strategic_cycle_history": [
        {"cycle_id": "c1", "outcome": "completed",
         "created_direction_ids": ["dGone"],
         "retired_direction_ids": []},
    ]}
    result = _build_outcome_evaluation(_make_project(directions=[], vision=vision))
    da = result["decision_accuracy"]
    assert da["created"][0]["status"] == "missing"
    assert da["created"][0]["success"] is False
    assert da["decision_accuracy_rate"] == 0.0


def test_strategic_effectiveness_keeps_only_last_5_cycles():
    cycles = [
        {"cycle_id": f"c{i}", "outcome": "completed",
         "created_direction_ids": [], "retired_direction_ids": []}
        for i in range(8)
    ]
    vision = {"_strategic_cycle_history": cycles}
    result = _build_outcome_evaluation(_make_project(vision=vision))
    eff = result["strategic_effectiveness"]
    assert eff["cycles_evaluated"] == 5
    assert [c["cycle_id"] for c in eff["cycles"]] == ["c3", "c4", "c5", "c6", "c7"]

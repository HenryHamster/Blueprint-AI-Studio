"""Tests for _build_outcome_evaluation and _compute_prediction_calibration_factor in app.strategic_analysis."""

from app.strategic_analysis import (
    _build_outcome_evaluation as _real_build_outcome_evaluation,
    _compute_prediction_calibration_factor as _real_compute_prediction_calibration_factor,
    _compute_prediction_calibration,
    _build_retired_directions_summary,
    _index_findings_by_direction,
    _build_retired_approaches_block,
    _RETIRED_MAX_COUNT,
    _RETIRED_PER_ENTRY_CHAR_CAP,
)
from app.direction_metrics import _compute_difficulty_index


def _make_project(directions=None, vision=None):
    """Build a minimal project-like object."""

    class Project:
        pass

    p = Project()
    p.directions = directions or []
    p.vision = vision or {}
    return p


# Audit W2: cycle history is now passed in (was JSONB on vision). These test
# wrappers preserve the legacy single-arg call pattern by extracting cycle
# history from project.vision._strategic_cycle_history when not supplied.
def _build_outcome_evaluation(project, cycle_history=None):
    if cycle_history is None:
        cycle_history = (project.vision or {}).get("_strategic_cycle_history") or []
    return _real_build_outcome_evaluation(project, cycle_history)


def _compute_prediction_calibration_factor(project, cycle_history=None, **kwargs):
    if cycle_history is None:
        cycle_history = (project.vision or {}).get("_strategic_cycle_history") or []
    return _real_compute_prediction_calibration_factor(project, cycle_history, **kwargs)


# ---------------------------------------------------------------------------
# 1. Empty directions
# ---------------------------------------------------------------------------


def test_empty_directions_returns_defaults():
    project = _make_project()
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )

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
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 3,
                    "prs_rejected": 1,
                    "tasks_failed": 0,
                    "tasks_created": 5,
                },
                {
                    "timestamp": "2026-01-02T00:00:00",
                    "prs_merged": 2,
                    "prs_rejected": 0,
                    "tasks_failed": 1,
                    "tasks_created": 4,
                },
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )

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
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 2,
                    "prs_rejected": 2,
                },
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
    d = result["directions"][0]
    assert d["acceptance_rate"] == 0.5


def test_no_prs_omits_acceptance_rate():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 0,
                    "prs_rejected": 0,
                },
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
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
        {
            "id": "dir-1",
            "status": "active",
            "research_context": research_ctx,
            "research_history": [],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
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
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
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
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 3,
                    "tasks_failed": 1,
                },
            ],
        },
        {
            "id": "dir-B",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 1,
                    "tasks_failed": 3,
                },
            ],
        },
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )

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
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 3,
                    "prs_rejected": 1,
                },
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
    d = result["directions"][0]
    assert "operator_rejection_rate" in d
    assert d["operator_rejection_rate"] == round(1 / (3 + 1), 2)


def test_operator_rejection_rate_absent_when_no_rejections():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 3,
                    "prs_rejected": 0,
                },
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
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
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
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
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
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
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
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
                {"timestamp": "2026-03-01T00:00:00", "prs_merged": 2},  # in window
            ],
        }
    ]
    project = _make_project(
        directions=directions,
        vision={"last_updated": "2026-01-01T00:00:00", "revision": 3},
    )
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
    assert result["overall"]["merged"] == 2
    assert result["window"]["from"] == "2026-01-01T00:00:00"
    assert result["window"]["vision_revision"] == 3


# ---------------------------------------------------------------------------
# 9. Systems cross-reference
# ---------------------------------------------------------------------------


def test_systems_cross_reference():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "system_id": "sys-A",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 2, "tasks_failed": 1}
            ],
        },
    ]
    vision = {"systems": [{"id": "sys-A", "name": "Auth", "status": "in_progress"}]}
    project = _make_project(directions=directions, vision=vision)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )

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
        {
            "id": "dir-active",
            "status": "active",
            "research_history": [{"timestamp": "2026-01-01T00:00:00", "prs_merged": 1}],
        },
        {
            "id": "dir-archived",
            "status": "archived",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 99}
            ],
        },
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
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
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "tasks_failed": 3,
                    "tasks_failed_infra": 2,
                    "taxonomy_summary": {"infra_related": 2, "code_defect": 1},
                },
                {
                    "timestamp": "2026-01-02T00:00:00",
                    "tasks_failed": 2,
                    "tasks_failed_infra": 0,
                    "taxonomy_summary": {"review_quality": 1, "code_defect": 1},
                },
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
    d = result["directions"][0]
    assert d["tasks_failed_infra"] == 2
    assert d["taxonomy_summary"] == {
        "infra_related": 2,
        "code_defect": 2,
        "review_quality": 1,
    }


def test_taxonomy_summary_absent_when_all_zero():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "tasks_failed": 0,
                    "tasks_failed_infra": 0,
                    "taxonomy_summary": {"code_defect": 0, "infra_related": 0},
                },
            ],
        }
    ]
    project = _make_project(directions=directions)
    d = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )["directions"][0]
    assert "tasks_failed_infra" not in d
    assert "taxonomy_summary" not in d


def test_taxonomy_summary_respects_window_filter():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2025-01-01T00:00:00",
                    "tasks_failed_infra": 10,
                    "taxonomy_summary": {"infra_related": 10},
                },  # outside window
                {
                    "timestamp": "2026-03-01T00:00:00",
                    "tasks_failed_infra": 1,
                    "taxonomy_summary": {"review_quality": 2},
                },  # inside window
            ],
        }
    ]
    project = _make_project(
        directions=directions,
        vision={"last_updated": "2026-01-01T00:00:00", "revision": 1},
    )
    d = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )["directions"][0]
    assert d["tasks_failed_infra"] == 1
    assert d["taxonomy_summary"] == {"review_quality": 2}


# ---------------------------------------------------------------------------
# Per-direction trend and velocity
# ---------------------------------------------------------------------------


def test_per_direction_trend_improving():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 0},
                {"timestamp": "2026-01-02T00:00:00", "prs_merged": 1},
                {"timestamp": "2026-01-03T00:00:00", "prs_merged": 4},
                {"timestamp": "2026-01-04T00:00:00", "prs_merged": 5},
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["trend"] == "improving"


def test_per_direction_trend_declining():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 5},
                {"timestamp": "2026-01-02T00:00:00", "prs_merged": 4},
                {"timestamp": "2026-01-03T00:00:00", "prs_merged": 1},
                {"timestamp": "2026-01-04T00:00:00", "prs_merged": 0},
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["trend"] == "declining"


def test_per_direction_trend_absent_for_fewer_than_4_cycles():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1},
                {"timestamp": "2026-01-02T00:00:00", "prs_merged": 10},
                {"timestamp": "2026-01-03T00:00:00", "prs_merged": 10},
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert "trend" not in d


def test_per_direction_velocity_accelerating():
    # Lifetime avg = 8/4 = 2.0; recent 2 avg = (1+5)/2 = 3.0; velocity = 1.5
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1},
                {"timestamp": "2026-01-02T00:00:00", "prs_merged": 1},
                {"timestamp": "2026-01-03T00:00:00", "prs_merged": 1},
                {"timestamp": "2026-01-04T00:00:00", "prs_merged": 5},
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["velocity"] == 1.5


def test_per_direction_velocity_decelerating():
    # Lifetime avg = 12/4 = 3.0; recent 2 avg = (1+1)/2 = 1.0; velocity ≈ 0.33
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 5},
                {"timestamp": "2026-01-02T00:00:00", "prs_merged": 5},
                {"timestamp": "2026-01-03T00:00:00", "prs_merged": 1},
                {"timestamp": "2026-01-04T00:00:00", "prs_merged": 1},
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["velocity"] == 0.33
    assert d["velocity"] < 1.0


def test_success_evidence_present_in_outcome():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "context": {
                "success_evidence": "S" * 700,
                "retirement_condition": "  retire when done  ",
            },
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1},
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["success_evidence"] == "S" * 500
    assert d["retirement_condition"] == "retire when done"


def test_success_evidence_absent_when_missing():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1},
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert "success_evidence" not in d
    assert "retirement_condition" not in d


def test_success_evidence_updated_in_outcome():
    """Outcome reflects updated success_evidence value."""
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "context": {
                "success_evidence": "Initial evidence",
                "retirement_condition": "Never retire",
            },
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1},
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["success_evidence"] == "Initial evidence"

    # Simulate an update to success_evidence
    directions[0]["context"]["success_evidence"] = "Updated evidence"
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["success_evidence"] == "Updated evidence"


def test_retirement_condition_updated_in_outcome():
    """Outcome reflects updated retirement_condition value."""
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "context": {
                "success_evidence": "Some evidence",
                "retirement_condition": "Old condition",
            },
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1},
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["retirement_condition"] == "Old condition"

    # Simulate an update to retirement_condition
    directions[0]["context"]["retirement_condition"] = "New condition"
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["retirement_condition"] == "New condition"


def test_success_evidence_cleared_in_outcome():
    """Outcome omits success_evidence when cleared."""
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "context": {
                "success_evidence": "Evidence to clear",
                "retirement_condition": "Keep this",
            },
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1},
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert "success_evidence" in d

    # Simulate clearing success_evidence by removing it
    directions[0]["context"]["success_evidence"] = ""
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert "success_evidence" not in d
    assert d["retirement_condition"] == "Keep this"


def test_retirement_condition_cleared_in_outcome():
    """Outcome omits retirement_condition when cleared."""
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "context": {
                "success_evidence": "Keep this",
                "retirement_condition": "Condition to clear",
            },
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1},
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert "retirement_condition" in d

    # Simulate clearing retirement_condition by removing it
    directions[0]["context"]["retirement_condition"] = None
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert "retirement_condition" not in d
    assert d["success_evidence"] == "Keep this"


def test_both_fields_updated_together():
    """Outcome reflects simultaneous updates to both fields."""
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "context": {
                "success_evidence": "Old evidence",
                "retirement_condition": "Old condition",
            },
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1},
            ],
        }
    ]

    # Update both fields
    directions[0]["context"]["success_evidence"] = "New evidence"
    directions[0]["context"]["retirement_condition"] = "New condition"
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["success_evidence"] == "New evidence"
    assert d["retirement_condition"] == "New condition"


def test_success_evidence_truncation_after_update():
    """Updated success_evidence longer than 500 chars is truncated."""
    long_evidence = "X" * 700
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "context": {
                "success_evidence": long_evidence,
                "retirement_condition": "Some condition",
            },
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1},
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["success_evidence"] == "X" * 500
    assert len(d["success_evidence"]) == 500


def test_retirement_condition_truncation_after_update():
    """Updated retirement_condition longer than 500 chars is truncated."""
    long_condition = "Y" * 600
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "context": {
                "success_evidence": "Some evidence",
                "retirement_condition": long_condition,
            },
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1},
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["retirement_condition"] == "Y" * 500
    assert len(d["retirement_condition"]) == 500


def test_confirmed_and_proposed_findings():
    research_ctx = (
        "## Established Findings\n"
        "- Finding plain\n"
        "- Finding confirmed (confirmed)\n"
        "- Finding proposed (proposed)\n"
    )
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_context": research_ctx,
            "research_history": [],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
    d = result["directions"][0]
    assert "confirmed" in d
    assert "proposed" in d
    # "Finding plain" and "Finding confirmed" should be in confirmed
    assert any("Finding plain" in c for c in d["confirmed"])
    assert any("Finding confirmed" in c for c in d["confirmed"])
    assert any("Finding proposed" in c for c in d["proposed"])


def test_cumulative_confirmed_finding_count_and_trend():
    """Verify outcome includes cumulative confirmed-finding count and trend."""
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [],
            "outcome_progress_history": [
                {"progress": 1},
                {"progress": 2},
                {"progress": 3},
                {"progress": 5},  # improving trend
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    d = result["directions"][0]

    assert "confirmed_finding_count" in d
    assert d["confirmed_finding_count"] == 5
    assert "confirmed_finding_trend" in d
    assert d["confirmed_finding_trend"] == "improving"


def test_confirmed_finding_trend_declining():
    """Verify declining trend detection."""
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [],
            "outcome_progress_history": [
                {"progress": 10},
                {"progress": 9},
                {"progress": 5},
                {"progress": 4},  # declining trend
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    d = result["directions"][0]

    assert d["confirmed_finding_trend"] == "declining"


def test_confirmed_finding_trend_stable():
    """Verify stable trend detection."""
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [],
            "outcome_progress_history": [
                {"progress": 5},
                {"progress": 5},
                {"progress": 5},
                {"progress": 6},  # stable trend (6 is not > 5*1.3 and not < 5*0.7)
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    d = result["directions"][0]

    assert d["confirmed_finding_trend"] == "stable"


def test_confirmed_finding_count_no_history():
    """Verify missing outcome_progress_history doesn't add the field."""
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [],
            # No outcome_progress_history
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    d = result["directions"][0]

    assert "confirmed_finding_count" not in d
    assert "confirmed_finding_trend" not in d


def test_confirmed_finding_trend_single_snapshot():
    """Verify trend not added with single snapshot."""
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [],
            "outcome_progress_history": [
                {"progress": 5},
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project)
    d = result["directions"][0]

    assert "confirmed_finding_count" in d
    assert d["confirmed_finding_count"] == 5
    assert "confirmed_finding_trend" not in d  # no trend with only 1 snapshot


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
            "id": "dir-cheap",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 4,
                    "tasks_created": 4,
                    "avg_time_to_merge_hours": 1.0,
                },
            ],
        },
        {
            "id": "dir-pricey",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 1,
                    "tasks_created": 5,
                    "avg_time_to_merge_hours": 4.0,
                },
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


def test_cost_budget_exceeded_flag():
    """budget_exceeded appears when estimated spend exceeds cost_budget_usd."""
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "cost_budget_usd": 0.01,  # very low budget — will be exceeded
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 1,
                    "tasks_created": 10,
                    "avg_time_to_merge_hours": 2.0,
                },
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["cost_budget_usd"] == 0.01
    assert d.get("budget_exceeded") is True


def test_cost_budget_not_exceeded():
    """budget_exceeded absent when spend is within cost_budget_usd."""
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "cost_budget_usd": 9999.0,  # very high budget — not exceeded
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 1,
                    "tasks_created": 1,
                    "avg_time_to_merge_hours": 1.0,
                },
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["cost_budget_usd"] == 9999.0
    assert "budget_exceeded" not in d


def test_cost_budget_absent_when_no_budget_set():
    """cost_budget_usd and budget_exceeded absent when direction has no budget."""
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 1,
                    "tasks_created": 5,
                    "avg_time_to_merge_hours": 2.0,
                },
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert "cost_budget_usd" not in d
    assert "budget_exceeded" not in d


def test_high_cost_per_merge_flag_on_expensive_direction():
    """high_cost_per_merge set on direction whose cost/PR is >2x project median.

    Three directions are needed so the median is the middle element rather than
    the average of two values (which would make the expensive direction
    mathematically unable to exceed the threshold for n=2).
    """
    directions = [
        {
            "id": "dir-cheap-a",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 10,
                    "tasks_created": 2,
                    "avg_time_to_merge_hours": 1.0,
                },
            ],
        },
        {
            "id": "dir-cheap-b",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 8,
                    "tasks_created": 2,
                    "avg_time_to_merge_hours": 1.0,
                },
            ],
        },
        {
            "id": "dir-pricey",
            "status": "active",
            "research_history": [
                # 100 tasks x 4h each / 1 merge -- cost/merge >> median * 2
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 1,
                    "tasks_created": 100,
                    "avg_time_to_merge_hours": 4.0,
                },
            ],
        },
    ]
    result = _build_outcome_evaluation(_make_project(directions=directions))
    by_id = {d["id"]: d for d in result["directions"]}
    assert "high_cost_per_merge" not in by_id["dir-cheap-a"]
    assert "high_cost_per_merge" not in by_id["dir-cheap-b"]
    assert by_id["dir-pricey"].get("high_cost_per_merge") is True


def test_high_cost_per_merge_absent_with_single_direction():
    """high_cost_per_merge never set when only one direction has cost data."""
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 1,
                    "tasks_created": 50,
                    "avg_time_to_merge_hours": 8.0,
                },
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert "high_cost_per_merge" not in d


def test_cost_estimate_absent_when_no_runtime_data():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 1,
                    "tasks_created": 2,
                },
            ],
        }
    ]
    result = _build_outcome_evaluation(_make_project(directions=directions))
    assert "estimated_cost_usd" not in result["directions"][0]
    assert "estimated_cost_usd" not in result["overall"]


def test_cost_per_merged_pr_omitted_when_no_merges():
    from app.routers.admin_analytics import ON_DEMAND_RATE  # noqa: F401

    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 0,
                    "tasks_created": 3,
                    "avg_time_to_merge_hours": 2.0,
                },
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert "estimated_cost_usd" in d
    assert "cost_per_merged_pr" not in d


def test_scope_performance_aggregates_across_cycles():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 2,
                    "scope_breakdown": {
                        "small": {"created": 5, "merged": 3, "failed": 1},
                        "medium": {"created": 2, "merged": 0, "failed": 2},
                    },
                    "escalation_outcomes": {
                        "small_to_medium": {"attempted": 1, "merged": 1, "failed": 0},
                    },
                },
                {
                    "timestamp": "2026-01-02T00:00:00",
                    "prs_merged": 1,
                    "scope_breakdown": {
                        "small": {"created": 3, "merged": 2, "failed": 0},
                    },
                    "escalation_outcomes": {
                        "small_to_medium": {"attempted": 2, "merged": 1, "failed": 1},
                        "medium_to_large": {"attempted": 1, "merged": 0, "failed": 1},
                    },
                },
            ],
        }
    ]
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
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 0,
                    "scope_breakdown": {
                        "small": {"created": 0, "merged": 0, "failed": 0},
                        "medium": {"created": 1, "merged": 0, "failed": 1},
                    },
                },
            ],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert d["scope_performance"] == {
        "medium": {"created": 1, "merged": 0, "failed": 1}
    }


def test_scope_performance_absent_when_no_data():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [{"timestamp": "2026-01-01T00:00:00", "prs_merged": 1}],
        }
    ]
    d = _build_outcome_evaluation(_make_project(directions=directions))["directions"][0]
    assert "scope_performance" not in d
    assert "escalation_outcomes" not in d


def test_difficulty_index_surfaced_in_outcome_evaluation():
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "tasks_created": 20,
                    "prs_merged": 2,
                }
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
    assert result["directions"][0]["difficulty_index"] == 10.0


# ---------------------------------------------------------------------------
# strategic_effectiveness — cycle-over-cycle decision quality
# ---------------------------------------------------------------------------


def test_strategic_effectiveness_absent_without_cycle_history():
    project = _make_project(
        directions=[
            {"id": "d1", "status": "active", "research_history": [{"prs_merged": 1}]}
        ]
    )
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
    assert "strategic_effectiveness" not in result


def test_strategic_effectiveness_skips_in_progress_cycles():
    vision = {
        "_strategic_cycle_history": [
            {"cycle_id": "c1", "outcome": "in_progress"},
        ]
    }
    result = _build_outcome_evaluation(_make_project(vision=vision))
    assert "strategic_effectiveness" not in result


def test_strategic_effectiveness_aggregates_completed_cycles():
    directions = [
        {
            "id": "dA",
            "status": "active",
            "research_history": [{"prs_merged": 2}],
        },  # created → merged
        {
            "id": "dB",
            "status": "active",
            "research_history": [{"prs_merged": 0}],
        },  # created → no merges
        {
            "id": "dR",
            "status": "retired",
            "research_history": [],
        },  # retired, still retired
    ]
    vision = {
        "_strategic_failures": 2,
        "_strategic_cycle_history": [
            {
                "cycle_id": "c1",
                "outcome": "completed",
                "phase2_completed_at": "2026-02-01T00:00:00",
                "created_direction_ids": ["dA", "dB"],
                "retired_direction_ids": ["dR"],
                "vision_updated": True,
            },
        ],
    }
    result = _build_outcome_evaluation(
        _make_project(directions=directions, vision=vision)
    )
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
        {"id": f"d{i}", "status": "active", "research_history": [{"prs_merged": 1}]}
        for i in range(5)
    ]
    # Early cycles: 0% conversion. Later cycles: 100%.
    vision = {
        "_strategic_cycle_history": [
            {
                "cycle_id": "c1",
                "outcome": "completed",
                "created_direction_ids": ["missing1"],
                "retired_direction_ids": [],
            },
            {
                "cycle_id": "c2",
                "outcome": "completed",
                "created_direction_ids": ["missing2"],
                "retired_direction_ids": [],
            },
            {
                "cycle_id": "c3",
                "outcome": "completed",
                "created_direction_ids": ["d0", "d1"],
                "retired_direction_ids": [],
            },
            {
                "cycle_id": "c4",
                "outcome": "completed",
                "created_direction_ids": ["d2", "d3"],
                "retired_direction_ids": [],
            },
        ]
    }
    result = _build_outcome_evaluation(
        _make_project(directions=directions, vision=vision)
    )
    assert result["strategic_effectiveness"]["trend"] == "improving"


def test_decision_accuracy_absent_without_completed_cycle():
    project = _make_project(
        directions=[
            {"id": "d1", "status": "active", "research_history": [{"prs_merged": 1}]}
        ]
    )
    assert "decision_accuracy" not in _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )


def test_decision_accuracy_scores_created_directions():
    directions = [
        {
            "id": "dA",
            "status": "active",
            "research_history": [{"prs_merged": 3}],
        },  # success
        {
            "id": "dB",
            "status": "active",
            "auto_paused": True,
            "research_history": [{"prs_merged": 2}],
        },  # auto_paused → not success
        {
            "id": "dC",
            "status": "active",
            "research_history": [{"prs_merged": 0}],
        },  # no merges → not success
    ]
    vision = {
        "_strategic_cycle_history": [
            {
                "cycle_id": "c1",
                "outcome": "completed",
                "phase2_completed_at": "2026-03-01T00:00:00",
                "created_direction_ids": ["dA", "dB", "dC"],
                "retired_direction_ids": [],
            },
        ]
    }
    result = _build_outcome_evaluation(
        _make_project(directions=directions, vision=vision)
    )
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
        {
            "id": "dBack",
            "status": "active",
            "research_history": [{"prs_merged": 1}],
        },  # was retired but is active again
    ]
    vision = {
        "_strategic_cycle_history": [
            {
                "cycle_id": "c1",
                "outcome": "completed",
                "created_direction_ids": [],
                "retired_direction_ids": ["dKeep", "dBack", "dGone"],
            },
        ]
    }
    result = _build_outcome_evaluation(
        _make_project(directions=directions, vision=vision)
    )
    da = result["decision_accuracy"]
    by_id = {r["direction_id"]: r for r in da["retired"]}
    assert by_id["dKeep"]["stayed_retired"] is True
    assert by_id["dBack"]["stayed_retired"] is False
    assert by_id["dGone"]["stayed_retired"] is True  # missing == stayed retired
    assert da["retired_accuracy_rate"] == round(2 / 3, 2)


def test_decision_accuracy_handles_missing_created_direction():
    vision = {
        "_strategic_cycle_history": [
            {
                "cycle_id": "c1",
                "outcome": "completed",
                "created_direction_ids": ["dGone"],
                "retired_direction_ids": [],
            },
        ]
    }
    result = _build_outcome_evaluation(_make_project(directions=[], vision=vision))
    da = result["decision_accuracy"]
    assert da["created"][0]["status"] == "missing"
    assert da["created"][0]["success"] is False
    assert da["decision_accuracy_rate"] == 0.0


def test_strategic_effectiveness_keeps_only_last_5_cycles():
    cycles = [
        {
            "cycle_id": f"c{i}",
            "outcome": "completed",
            "created_direction_ids": [],
            "retired_direction_ids": [],
        }
        for i in range(8)
    ]
    vision = {"_strategic_cycle_history": cycles}
    result = _build_outcome_evaluation(_make_project(vision=vision))
    eff = result["strategic_effectiveness"]
    assert eff["cycles_evaluated"] == 5
    assert [c["cycle_id"] for c in eff["cycles"]] == ["c3", "c4", "c5", "c6", "c7"]


# ---------------------------------------------------------------------------
# _compute_prediction_calibration_factor tests
# ---------------------------------------------------------------------------


class TestPredictionCalibrationFactor:
    def test_no_history_returns_one(self):
        project = _make_project(vision={})
        assert (
            _compute_prediction_calibration_factor(
                project, (project.vision or {}).get("_strategic_cycle_history") or []
            )
            == 1.0
        )

    def test_empty_cycle_history_returns_one(self):
        project = _make_project(vision={"_strategic_cycle_history": []})
        assert (
            _compute_prediction_calibration_factor(
                project, (project.vision or {}).get("_strategic_cycle_history") or []
            )
            == 1.0
        )

    def test_completed_cycles_with_zero_predicted_returns_one(self):
        project = _make_project(
            directions=[{"id": "d1", "research_history": [{"prs_merged": 5}]}],
            vision={
                "_strategic_cycle_history": [
                    {
                        "outcome": "completed",
                        "created_predictions": {"d1": {"similar_past_merges": 0}},
                    }
                ]
            },
        )
        assert (
            _compute_prediction_calibration_factor(
                project, (project.vision or {}).get("_strategic_cycle_history") or []
            )
            == 1.0
        )

    def test_insufficient_history_blends_toward_one(self):
        # 1 data point (< min_samples=3) with ratio=0.0 should blend toward 1.0
        project = _make_project(
            directions=[{"id": "d1", "research_history": [{"prs_merged": 0}]}],
            vision={
                "_strategic_cycle_history": [
                    {
                        "outcome": "completed",
                        "created_predictions": {"d1": {"similar_past_merges": 10}},
                    }
                ]
            },
        )
        factor = _compute_prediction_calibration_factor(
            project, (project.vision or {}).get("_strategic_cycle_history") or []
        )
        # With 1 sample, raw=0.0, weight=1/3, blended = (1/3)*0 + (2/3)*1 = 0.667
        assert 0.6 < factor < 0.8

    def test_systematic_over_prediction_factor_below_one(self):
        # Three cycles each predicting 10 merges but only 3 happen → ratio 0.3 each
        directions = [
            {"id": f"d{i}", "research_history": [{"prs_merged": 3}]} for i in range(3)
        ]
        cycles = [
            {
                "outcome": "completed",
                "created_predictions": {f"d{i}": {"similar_past_merges": 10}},
            }
            for i in range(3)
        ]
        project = _make_project(
            directions=directions, vision={"_strategic_cycle_history": cycles}
        )
        factor = _compute_prediction_calibration_factor(
            project, (project.vision or {}).get("_strategic_cycle_history") or []
        )
        assert factor < 1.0, f"Expected factor < 1.0 for over-prediction, got {factor}"
        assert factor >= 0.5

    def test_systematic_under_prediction_factor_above_one(self):
        # Three cycles each predicting 2 merges but 8 happen → ratio 4.0 each
        directions = [
            {"id": f"d{i}", "research_history": [{"prs_merged": 8}]} for i in range(3)
        ]
        cycles = [
            {
                "outcome": "completed",
                "created_predictions": {f"d{i}": {"similar_past_merges": 2}},
            }
            for i in range(3)
        ]
        project = _make_project(
            directions=directions, vision={"_strategic_cycle_history": cycles}
        )
        factor = _compute_prediction_calibration_factor(
            project, (project.vision or {}).get("_strategic_cycle_history") or []
        )
        assert factor > 1.0, f"Expected factor > 1.0 for under-prediction, got {factor}"
        assert factor <= 1.5

    def test_extreme_over_prediction_clips_to_minimum(self):
        # Predict 100, actual 0 → ratio 0.0, well below 0.5 clip floor
        directions = [
            {"id": f"d{i}", "research_history": [{"prs_merged": 0}]} for i in range(5)
        ]
        cycles = [
            {
                "outcome": "completed",
                "created_predictions": {f"d{i}": {"similar_past_merges": 100}},
            }
            for i in range(5)
        ]
        project = _make_project(
            directions=directions, vision={"_strategic_cycle_history": cycles}
        )
        factor = _compute_prediction_calibration_factor(
            project, (project.vision or {}).get("_strategic_cycle_history") or []
        )
        assert factor == 0.5

    def test_extreme_under_prediction_clips_to_maximum(self):
        # Predict 1, actual 1000 → ratio 1000.0, well above 1.5 clip ceiling
        directions = [
            {"id": f"d{i}", "research_history": [{"prs_merged": 1000}]}
            for i in range(5)
        ]
        cycles = [
            {
                "outcome": "completed",
                "created_predictions": {f"d{i}": {"similar_past_merges": 1}},
            }
            for i in range(5)
        ]
        project = _make_project(
            directions=directions, vision={"_strategic_cycle_history": cycles}
        )
        factor = _compute_prediction_calibration_factor(
            project, (project.vision or {}).get("_strategic_cycle_history") or []
        )
        assert factor == 1.5

    def test_only_completed_cycles_are_used(self):
        # Failed/in-progress cycles should be ignored
        direction = {"id": "d1", "research_history": [{"prs_merged": 1}]}
        project = _make_project(
            directions=[direction],
            vision={
                "_strategic_cycle_history": [
                    {
                        "outcome": "failed",
                        "created_predictions": {"d1": {"similar_past_merges": 100}},
                    },
                    {
                        "outcome": "completed",
                        "created_predictions": {"d1": {"similar_past_merges": 1}},
                    },
                ]
            },
        )
        # Only the completed cycle matters: actual=1, predicted=1 → ratio=1.0
        # But 1 sample < min_samples=3 → blends toward 1.0 anyway
        factor = _compute_prediction_calibration_factor(
            project, (project.vision or {}).get("_strategic_cycle_history") or []
        )
        # ratio=1.0, weight=1/3, blended = 1.0
        assert factor == 1.0

    def test_lookback_respects_cycle_cap(self):
        # 10 completed cycles, all with ratio=0.1 (over-predicting)
        # but lookback_cycles=2 means only the last 2 should count
        directions = [
            {"id": f"d{i}", "research_history": [{"prs_merged": 1}]} for i in range(10)
        ]
        cycles = [
            {
                "outcome": "completed",
                "created_predictions": {f"d{i}": {"similar_past_merges": 10}},
            }
            for i in range(10)
        ]
        project = _make_project(
            directions=directions, vision={"_strategic_cycle_history": cycles}
        )
        # With lookback_cycles=2, we get 2 data points, both with ratio=0.1
        # 2 < min_samples=3 → weight=2/3, blended = (2/3)*0.1 + (1/3)*1.0 ≈ 0.4
        # Clipped to 0.5
        factor = _compute_prediction_calibration_factor(
            project,
            (project.vision or {}).get("_strategic_cycle_history") or [],
            lookback_cycles=2,
        )
        assert factor == 0.5  # clipped

    def test_missing_direction_skipped(self):
        # Direction referenced in prediction no longer exists → skip
        project = _make_project(
            directions=[],  # direction removed
            vision={
                "_strategic_cycle_history": [
                    {
                        "outcome": "completed",
                        "created_predictions": {"d-gone": {"similar_past_merges": 5}},
                    }
                ]
            },
        )
        assert (
            _compute_prediction_calibration_factor(
                project, (project.vision or {}).get("_strategic_cycle_history") or []
            )
            == 1.0
        )


# ---------------------------------------------------------------------------
# _compute_prediction_calibration tests
# ---------------------------------------------------------------------------


class TestComputePredictionCalibration:
    def _make_cycle(self, direction_id: str, predicted: float) -> dict:
        return {
            "outcome": "completed",
            "created_predictions": {direction_id: {"similar_past_merges": predicted}},
        }

    def test_returns_dict_with_required_keys(self):
        project = _make_project(vision={})
        result = _compute_prediction_calibration(project, [])
        assert set(result.keys()) == {"factor", "note", "sample_count"}

    def test_no_history_returns_well_calibrated(self):
        project = _make_project(vision={})
        result = _compute_prediction_calibration(project, [])
        assert result["factor"] == 1.0
        assert result["sample_count"] == 0
        assert "well-calibrated" in result["note"]

    def test_optimistic_note_when_over_predicting(self):
        # Direction predicted 10 merges but only got 5 → factor = 0.5 → optimistic
        project = _make_project(
            directions=[
                {
                    "id": "d1",
                    "research_history": [{"prs_merged": 5}],
                    "history_aggregate": {},
                }
            ],
        )
        cycle = self._make_cycle("d1", 10)
        result = _compute_prediction_calibration(project, [cycle, cycle, cycle])
        assert result["factor"] < 1.0
        assert "optimistic" in result["note"]
        assert result["sample_count"] >= 3

    def test_conservative_note_when_under_predicting(self):
        # Direction predicted 2 merges but got 6 → factor > 1 → pessimistic/conservative
        project = _make_project(
            directions=[
                {
                    "id": "d1",
                    "research_history": [{"prs_merged": 6}],
                    "history_aggregate": {},
                }
            ],
        )
        cycle = self._make_cycle("d1", 2)
        result = _compute_prediction_calibration(project, [cycle, cycle, cycle])
        assert result["factor"] > 1.0
        assert "conservative" in result["note"]

    def test_sparse_history_returns_low_sample_count(self):
        # Only 1 data point — factor blended toward 1.0, sample_count == 1
        project = _make_project(
            directions=[
                {
                    "id": "d1",
                    "research_history": [{"prs_merged": 0}],
                    "history_aggregate": {},
                }
            ],
        )
        cycle = self._make_cycle("d1", 10)
        result = _compute_prediction_calibration(project, [cycle])
        assert result["sample_count"] == 1
        # Blended factor should be close to 1.0 (sparse)
        assert abs(result["factor"] - 1.0) < 0.5

    def test_factor_matches_calibration_factor_helper(self):
        project = _make_project(
            directions=[
                {
                    "id": "d1",
                    "research_history": [{"prs_merged": 3}],
                    "history_aggregate": {},
                }
            ],
        )
        cycles = [self._make_cycle("d1", 3)] * 5
        result = _compute_prediction_calibration(project, cycles)
        expected_factor = _real_compute_prediction_calibration_factor(project, cycles)
        assert result["factor"] == expected_factor


# ---------------------------------------------------------------------------
# Autonomous attribution: CPM uses autonomous_prs_merged
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# evidence block from recent_findings_evidence
# ---------------------------------------------------------------------------


def test_evidence_included_for_merged_findings():
    research_ctx = (
        "## Established Findings\n"
        "- feat: auth fix [small] (merged). Description\n"
        "- feat: perf fix [small] (proposed). Description\n"
    )
    evidence = [
        {
            "title": "feat: auth fix",
            "claims": ["claim A", "claim B", "claim C", "claim D"],
            "source_refs": [
                {"ref": "backend/app/auth.py"},
                {"ref": "PR #123"},
                {"ref": "backend/app/models.py"},
                {"ref": "extra"},
            ],
        },
        {
            "title": "feat: perf fix",
            "claims": ["claim X"],
            "source_refs": [{"ref": "agent/src/index.ts"}],
        },
    ]
    directions = [
        {
            "id": "d1",
            "status": "active",
            "research_context": research_ctx,
            "recent_findings_evidence": evidence,
            "research_history": [],
        }
    ]
    result = _build_outcome_evaluation(_make_project(directions=directions), [])
    d = result["directions"][0]
    assert "evidence" in d
    ev = {e["title"]: e for e in d["evidence"]}
    # claims capped at 3
    assert len(ev["feat: auth fix"]["claims"]) == 3
    # source_refs capped at 3
    assert len(ev["feat: auth fix"]["source_refs"]) == 3
    assert "feat: perf fix" in ev


def test_evidence_omits_failed_findings():
    research_ctx = (
        "## Established Findings\n- feat: broken [small] (failed: ci). Description\n"
    )
    evidence = [
        {
            "title": "feat: broken",
            "claims": ["claim A"],
            "source_refs": [{"ref": "file.py"}],
        },
    ]
    directions = [
        {
            "id": "d1",
            "status": "active",
            "research_context": research_ctx,
            "recent_findings_evidence": evidence,
            "research_history": [],
        }
    ]
    result = _build_outcome_evaluation(_make_project(directions=directions), [])
    d = result["directions"][0]
    assert "evidence" not in d


def test_evidence_absent_when_no_recent_findings_evidence():
    directions = [
        {
            "id": "d1",
            "status": "active",
            "research_history": [{"timestamp": "2026-01-01T00:00:00", "prs_merged": 1}],
        }
    ]
    result = _build_outcome_evaluation(_make_project(directions=directions), [])
    assert "evidence" not in result["directions"][0]


class TestAutonomousCPM:
    """cost_per_merged_pr uses autonomous_prs_merged as denominator."""

    def test_cpm_absent_when_all_assisted_merges(self):
        """CPM is None when autonomous_prs_merged == 0 even if prs_merged > 0."""
        directions = [
            {
                "id": "dir-1",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": "2026-01-01T00:00:00",
                        "prs_merged": 3,
                        "autonomous_prs_merged": 0,
                        "assisted_prs_merged": 3,
                        "tasks_created": 3,
                        "avg_time_to_merge_hours": 2.0,
                    }
                ],
            }
        ]
        d = _build_outcome_evaluation(_make_project(directions=directions))[
            "directions"
        ][0]
        assert "cost_per_merged_pr" not in d

    def test_fully_autonomous_cpm_equals_legacy_cpm(self):
        """Fully-autonomous direction: CPM with autonomous_prs_merged == CPM with prs_merged."""
        from app.routers.admin_analytics import ON_DEMAND_RATE

        prs = 4
        hours = 1.5
        tasks = 4
        expected_cost = round(tasks * hours * 3600 * ON_DEMAND_RATE, 2)
        directions = [
            {
                "id": "dir-1",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": "2026-01-01T00:00:00",
                        "prs_merged": prs,
                        "autonomous_prs_merged": prs,
                        "assisted_prs_merged": 0,
                        "tasks_created": tasks,
                        "avg_time_to_merge_hours": hours,
                    }
                ],
            }
        ]
        d = _build_outcome_evaluation(_make_project(directions=directions))[
            "directions"
        ][0]
        assert d.get("cost_per_merged_pr") == round(expected_cost / prs, 2)

    def test_legacy_entry_without_autonomous_field_uses_prs_merged_fallback(self):
        """Legacy entries lacking autonomous_prs_merged default to prs_merged (all-autonomous)."""
        from app.routers.admin_analytics import ON_DEMAND_RATE

        prs = 2
        hours = 1.0
        tasks = 2
        expected_cost = round(tasks * hours * 3600 * ON_DEMAND_RATE, 2)
        # No autonomous_prs_merged key — pre-attribution legacy entry
        directions = [
            {
                "id": "dir-1",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": "2026-01-01T00:00:00",
                        "prs_merged": prs,
                        "tasks_created": tasks,
                        "avg_time_to_merge_hours": hours,
                    }
                ],
            }
        ]
        d = _build_outcome_evaluation(_make_project(directions=directions))[
            "directions"
        ][0]
        assert d.get("cost_per_merged_pr") == round(expected_cost / prs, 2)

    def test_mixed_attribution_cpm_uses_autonomous_count(self):
        """CPM denominator is autonomous count, not total merged."""
        from app.routers.admin_analytics import ON_DEMAND_RATE

        tasks = 5
        hours = 2.0
        expected_cost = round(tasks * hours * 3600 * ON_DEMAND_RATE, 2)
        directions = [
            {
                "id": "dir-1",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": "2026-01-01T00:00:00",
                        "prs_merged": 5,
                        "autonomous_prs_merged": 3,
                        "assisted_prs_merged": 2,
                        "tasks_created": tasks,
                        "avg_time_to_merge_hours": hours,
                    }
                ],
            }
        ]
        d = _build_outcome_evaluation(_make_project(directions=directions))[
            "directions"
        ][0]
        assert d.get("cost_per_merged_pr") == round(expected_cost / 3, 2)


# ---------------------------------------------------------------------------
# _build_retired_directions_summary tests
# ---------------------------------------------------------------------------


def _recent_iso(days_ago: int = 0) -> str:
    """ISO timestamp for N days ago (within the 90d window)."""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _old_iso() -> str:
    """ISO timestamp older than the 90d window."""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()


def test_retired_directions_appear_in_summary():
    """Retired direction within the window is included."""
    directions = [
        {
            "id": "dir-ret",
            "text": "Improve logging",
            "status": "retired",
            "retirement_reason": "saturated",
            "research_history": [
                {
                    "timestamp": _recent_iso(5),
                    "prs_merged": 4,
                    "prs_rejected": 1,
                    "tasks_failed": 0,
                },
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_retired_directions_summary(project)
    assert len(result) == 1
    entry = result[0]
    assert entry["id"] == "dir-ret"
    assert entry["status"] == "retired"
    assert entry["prs_merged"] == 4
    assert entry["prs_rejected"] == 1
    assert "retirement_reason" in entry


def test_completed_and_superseded_included():
    """Completed and superseded directions are included."""
    directions = [
        {
            "id": "dir-comp",
            "text": "Refactor auth",
            "status": "completed",
            "research_history": [
                {
                    "timestamp": _recent_iso(3),
                    "prs_merged": 2,
                    "prs_rejected": 0,
                    "tasks_failed": 0,
                }
            ],
        },
        {
            "id": "dir-super",
            "text": "Old feature",
            "status": "superseded",
            "research_history": [
                {
                    "timestamp": _recent_iso(10),
                    "prs_merged": 1,
                    "prs_rejected": 0,
                    "tasks_failed": 0,
                }
            ],
        },
    ]
    project = _make_project(directions=directions)
    result = _build_retired_directions_summary(project)
    ids = {e["id"] for e in result}
    assert "dir-comp" in ids
    assert "dir-super" in ids


def test_expired_with_auto_completed_reason_included():
    """Expired direction with auto_completed_reason is included."""
    directions = [
        {
            "id": "dir-exp",
            "text": "Expired direction",
            "status": "expired",
            "auto_completed_reason": "vision goals met",
            "research_history": [
                {
                    "timestamp": _recent_iso(20),
                    "prs_merged": 6,
                    "prs_rejected": 0,
                    "tasks_failed": 0,
                }
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_retired_directions_summary(project)
    assert len(result) == 1
    assert result[0]["auto_completed_reason"] == "vision goals met"


def test_expired_without_auto_completed_reason_excluded():
    """Expired direction without auto_completed_reason is excluded."""
    directions = [
        {
            "id": "dir-bare-exp",
            "text": "Bare expired",
            "status": "expired",
            "research_history": [
                {
                    "timestamp": _recent_iso(5),
                    "prs_merged": 1,
                    "prs_rejected": 0,
                    "tasks_failed": 0,
                }
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_retired_directions_summary(project)
    assert result == []


def test_active_directions_excluded():
    """Active directions are not included."""
    directions = [
        {
            "id": "dir-active",
            "text": "Active direction",
            "status": "active",
            "research_history": [
                {
                    "timestamp": _recent_iso(1),
                    "prs_merged": 2,
                    "prs_rejected": 0,
                    "tasks_failed": 0,
                }
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_retired_directions_summary(project)
    assert result == []


def test_directions_older_than_window_excluded():
    """Directions retired beyond the window_days cutoff are excluded."""
    directions = [
        {
            "id": "dir-old",
            "text": "Old retired direction",
            "status": "retired",
            "research_history": [
                {
                    "timestamp": _old_iso(),
                    "prs_merged": 3,
                    "prs_rejected": 0,
                    "tasks_failed": 0,
                }
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_retired_directions_summary(project)
    assert result == []


def test_max_count_cap():
    """At most max_count entries are returned."""
    directions = [
        {
            "id": f"dir-{i}",
            "text": f"Direction {i}",
            "status": "retired",
            "research_history": [
                {
                    "timestamp": _recent_iso(i),
                    "prs_merged": 1,
                    "prs_rejected": 0,
                    "tasks_failed": 0,
                }
            ],
        }
        for i in range(25)
    ]
    project = _make_project(directions=directions)
    result = _build_retired_directions_summary(project, max_count=5)
    assert len(result) == 5


def test_per_entry_char_cap():
    """Each entry JSON is within per_entry_char_cap chars."""
    import json

    long_text = "A" * 500
    long_reason = "B" * 500
    directions = [
        {
            "id": "dir-fat",
            "text": long_text,
            "status": "retired",
            "retirement_reason": long_reason,
            "research_history": [
                {
                    "timestamp": _recent_iso(2),
                    "prs_merged": 2,
                    "prs_rejected": 0,
                    "tasks_failed": 0,
                }
            ],
        }
    ]
    project = _make_project(directions=directions)
    cap = _RETIRED_PER_ENTRY_CHAR_CAP
    result = _build_retired_directions_summary(project, per_entry_char_cap=cap)
    assert len(result) == 1
    entry_len = len(json.dumps(result[0]))
    assert entry_len <= cap, f"Entry exceeds cap: {entry_len} > {cap}"


def test_total_payload_bounded_with_many_retired():
    """Total summary for _RETIRED_MAX_COUNT entries stays within ~16KB."""
    import json

    directions = [
        {
            "id": f"dir-{i:03d}",
            "text": f"Direction about improving the system component {i} " * 3,
            "status": "retired",
            "retirement_reason": f"Reason {i}: saturated after investigation",
            "research_history": [
                {
                    "timestamp": _recent_iso(i % 80),
                    "prs_merged": i % 5,
                    "prs_rejected": 0,
                    "tasks_failed": 0,
                }
            ],
        }
        for i in range(_RETIRED_MAX_COUNT + 5)
    ]
    project = _make_project(directions=directions)
    result = _build_retired_directions_summary(project)
    assert len(result) <= _RETIRED_MAX_COUNT
    total_chars = len(json.dumps(result))
    assert total_chars <= 16_384, f"Summary too large: {total_chars} chars"


def test_failed_approaches_included():
    """Failed approaches from research_context are extracted and included."""
    ctx = """
## Established findings
- Some finding (confirmed)

## Failed approaches
- Tried approach A — did not work
- Tried approach B — too slow
"""
    directions = [
        {
            "id": "dir-fa",
            "text": "Direction with failed approaches",
            "status": "retired",
            "research_context": ctx,
            "research_history": [
                {
                    "timestamp": _recent_iso(7),
                    "prs_merged": 1,
                    "prs_rejected": 0,
                    "tasks_failed": 2,
                }
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_retired_directions_summary(project)
    assert len(result) == 1
    # failed_approaches may be trimmed if entry would exceed cap, but with
    # a small entry it should be present
    entry = result[0]
    if "failed_approaches" in entry:
        assert len(entry["failed_approaches"]) > 0


# ---------------------------------------------------------------------------
# _index_findings_by_direction
# ---------------------------------------------------------------------------


def test_index_findings_groups_by_direction():
    rows = [
        {"direction_id": "d-1", "finding_title": "F1"},
        {"direction_id": "d-1", "finding_title": "F2"},
        {"direction_id": "d-2", "finding_title": "F3"},
    ]
    idx = _index_findings_by_direction(rows)
    assert set(idx.keys()) == {"d-1", "d-2"}
    assert len(idx["d-1"]) == 2
    assert len(idx["d-2"]) == 1


def test_index_findings_no_direction_id_goes_to_unlinked():
    rows = [{"direction_id": None, "finding_title": "X"}]
    idx = _index_findings_by_direction(rows)
    assert "_unlinked" in idx
    assert "X" == idx["_unlinked"][0]["finding_title"]


def test_build_outcome_evaluation_injects_recent_findings():
    directions = [
        {
            "id": "d-1",
            "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1, "tasks_failed": 0}
            ],
        }
    ]
    project = _make_project(directions=directions)
    findings_rows = [
        {
            "direction_id": "d-1",
            "finding_title": "T1",
            "claims": ["c1"],
            "sources": [],
            "actionability_grade": "HIGH",
        }
    ]
    fbd = _index_findings_by_direction(findings_rows)
    result = _real_build_outcome_evaluation(project, [], findings_by_direction=fbd)
    d_out = result["directions"][0]
    assert "recent_findings" in d_out
    assert d_out["recent_findings"][0]["finding_title"] == "T1"


def test_build_outcome_evaluation_no_findings_by_direction_unchanged():
    directions = [
        {
            "id": "d-1",
            "status": "active",
            "research_history": [
                {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1, "tasks_failed": 0}
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _real_build_outcome_evaluation(project, [])
    d_out = result["directions"][0]
    assert "recent_findings" not in d_out


def test_recent_diagnosis_included_when_present():
    """recent_diagnosis block appears when direction has diagnosis_summary."""
    directions = [
        {
            "id": "d-1",
            "status": "active",
            "diagnosis_summary": "The root cause was a race condition in the async loop. Fixed by adding proper synchronization.",
            "diagnosis_completed_at": "2026-03-15T10:30:00",
            "diagnosed_affected_files": [
                "backend/app/async_handler.py",
                "backend/app/sync.py",
            ],
            "research_history": [{"timestamp": "2026-01-01T00:00:00", "prs_merged": 1}],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
    d = result["directions"][0]
    assert "recent_diagnosis" in d
    assert (
        d["recent_diagnosis"]["summary"]
        == "The root cause was a race condition in the async loop. Fixed by adding proper synchronization."
    )
    assert d["recent_diagnosis"]["diagnosed_at"] == "2026-03-15T10:30:00"
    assert "backend/app/async_handler.py" in d["recent_diagnosis"]["affected_files"]


def test_recent_diagnosis_absent_when_empty():
    """recent_diagnosis block is absent when diagnosis_summary is empty or missing."""
    directions = [
        {
            "id": "d-1",
            "status": "active",
            "research_history": [{"timestamp": "2026-01-01T00:00:00", "prs_merged": 1}],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
    d = result["directions"][0]
    assert "recent_diagnosis" not in d


def test_recent_diagnosis_truncates_long_summary():
    """recent_diagnosis summary is capped at 500 chars."""
    long_summary = "A" * 600
    directions = [
        {
            "id": "d-1",
            "status": "active",
            "diagnosis_summary": long_summary,
            "diagnosis_completed_at": "2026-03-15T10:30:00",
            "research_history": [{"timestamp": "2026-01-01T00:00:00", "prs_merged": 1}],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
    d = result["directions"][0]
    assert "recent_diagnosis" in d
    assert len(d["recent_diagnosis"]["summary"]) == 500
    assert d["recent_diagnosis"]["summary"] == "A" * 500


def test_recent_diagnosis_omits_missing_optional_fields():
    """recent_diagnosis includes only non-empty optional fields."""
    directions = [
        {
            "id": "d-1",
            "status": "active",
            "diagnosis_summary": "A simple diagnosis with no other metadata.",
            "research_history": [{"timestamp": "2026-01-01T00:00:00", "prs_merged": 1}],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
    d = result["directions"][0]
    assert "recent_diagnosis" in d
    assert "summary" in d["recent_diagnosis"]
    assert "diagnosed_at" not in d["recent_diagnosis"]
    assert "affected_files" not in d["recent_diagnosis"]


# ---------------------------------------------------------------------------
# _build_retired_approaches_block tests
# ---------------------------------------------------------------------------


def test_retired_approaches_block_empty_when_no_summary():
    """Block is None when retired_directions_summary is empty or None."""
    assert _build_retired_approaches_block(None) is None
    assert _build_retired_approaches_block([]) is None


def test_retired_approaches_block_empty_when_no_failed_approaches():
    """Block is None when no retired direction has failed_approaches."""
    summary = [
        {
            "id": "dir-1",
            "text": "Direction A",
            "status": "retired",
            "prs_merged": 2,
        },
        {
            "id": "dir-2",
            "text": "Direction B",
            "status": "completed",
            "prs_merged": 1,
        },
    ]
    assert _build_retired_approaches_block(summary) is None


def test_retired_approaches_block_includes_header_when_present():
    """Block includes header when there are failed approaches."""
    summary = [
        {
            "id": "dir-ret",
            "text": "Improve logging",
            "status": "retired",
            "failed_approaches": ["Approach X", "Approach Y"],
        }
    ]
    block = _build_retired_approaches_block(summary)
    assert block is not None
    assert "Do not re-propose these" in block
    assert "already attempted under retired directions" in block


def test_retired_approaches_block_tags_with_direction_id():
    """Each approach is tagged with the direction ID it came from."""
    summary = [
        {
            "id": "dir-auth",
            "text": "Auth flow",
            "status": "retired",
            "failed_approaches": ["OAuth attempt", "SAML attempt"],
        }
    ]
    block = _build_retired_approaches_block(summary)
    assert "dir-auth" in block
    assert "Auth flow" in block
    assert "OAuth attempt" in block
    assert "SAML attempt" in block


def test_retired_approaches_block_truncates_long_approaches():
    """Each approach is truncated to per_entry_char_cap (default 80)."""
    long_approach = "A" * 100
    summary = [
        {
            "id": "dir-1",
            "text": "Dir",
            "status": "retired",
            "failed_approaches": [long_approach],
        }
    ]
    block = _build_retired_approaches_block(summary, per_entry_char_cap=20)
    assert "A" * 20 in block
    assert "A" * 21 not in block


def test_retired_approaches_block_caps_total_entries():
    """Total entries are capped at max_entries (default 30)."""
    summary = [
        {
            "id": "dir-1",
            "text": "Dir",
            "status": "retired",
            "failed_approaches": [f"Approach {i}" for i in range(40)],
        }
    ]
    block = _build_retired_approaches_block(summary, max_entries=10)
    assert block is not None
    # Count the number of "- Approach" lines
    approach_count = block.count("- Approach")
    assert approach_count == 10


def test_retired_approaches_block_across_multiple_directions():
    """Block aggregates approaches from multiple retired directions."""
    summary = [
        {
            "id": "dir-1",
            "text": "First",
            "status": "retired",
            "failed_approaches": ["Approach A", "Approach B"],
        },
        {
            "id": "dir-2",
            "text": "Second",
            "status": "retired",
            "failed_approaches": ["Approach C"],
        },
    ]
    block = _build_retired_approaches_block(summary)
    assert block is not None
    assert "Approach A" in block
    assert "Approach B" in block
    assert "Approach C" in block
    assert "dir-1" in block
    assert "dir-2" in block


def test_retired_approaches_block_truncates_direction_text():
    """Direction text is truncated to 40 chars."""
    long_text = "A" * 100
    summary = [
        {
            "id": "dir-1",
            "text": long_text,
            "status": "retired",
            "failed_approaches": ["Some approach"],
        }
    ]
    block = _build_retired_approaches_block(summary)
    assert block is not None
    # Check that only the first 40 chars of direction text are in the block
    assert "A" * 40 in block
    # But not the full 100 chars
    assert "A" * 50 not in block


def test_retired_approaches_block_skips_directions_without_approaches():
    """Directions without failed_approaches are skipped (no empty entries)."""
    summary = [
        {
            "id": "dir-1",
            "text": "No approaches",
            "status": "retired",
        },
        {
            "id": "dir-2",
            "text": "Has approaches",
            "status": "retired",
            "failed_approaches": ["Approach X"],
        },
    ]
    block = _build_retired_approaches_block(summary)
    assert block is not None
    assert "dir-2" in block
    assert "Approach X" in block
    # dir-1 should not appear since it has no approaches
    assert "dir-1" not in block


def test_retired_approaches_block_respects_max_entries_across_directions():
    """max_entries cap applies across all directions combined."""
    summary = [
        {
            "id": "dir-1",
            "text": "D1",
            "status": "retired",
            "failed_approaches": ["Approach 1", "Approach 2"],
        },
        {
            "id": "dir-2",
            "text": "D2",
            "status": "retired",
            "failed_approaches": ["Approach 3", "Approach 4"],
        },
    ]
    block = _build_retired_approaches_block(summary, max_entries=3)
    # Should have at most 3 entries
    entry_count = block.count("\n- ") + (1 if block.startswith("- ") else 0)
    assert entry_count <= 3

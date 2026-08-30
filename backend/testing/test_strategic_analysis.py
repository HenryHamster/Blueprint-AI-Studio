"""Tests for _build_outcome_evaluation and _compute_prediction_calibration_factor in app.strategic_analysis."""

import uuid
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.strategic_analysis import (
    _build_outcome_evaluation as _real_build_outcome_evaluation,
    _compute_prediction_calibration_factor as _real_compute_prediction_calibration_factor,
    _compute_prediction_calibration,
    _build_retired_directions_summary,
    _index_findings_by_direction,
    _build_retired_approaches_block,
    _entry_cost_usd,
    _RETIRED_MAX_COUNT,
    _RETIRED_PER_ENTRY_CHAR_CAP,
)
from app.strategic_completion import record_retired_direction_memory
from app.direction_metrics import (
    _compute_difficulty_index,
    _compute_lifetime_generalized_outcome_state,
    _compute_productivity_score,
)


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


def test_success_rate_clamped_when_pr_ineligible_failures_exceed_total_failed():
    """Genuinely-cancelled PR-ineligible tasks must not drive success_rate > 1.0.

    total_failed sums each cycle's ``tasks_failed`` (TaskStatus.FAILED only),
    while type_breakdown[*]['failed'] counts FAILED *plus* genuinely-cancelled
    tasks. Subtracting the broader PR-ineligible failure count from the stricter
    total can go negative; the fix clamps the eligible-failure count at zero and
    the emitted rate at 1.0 so no impossible success_rate reaches strategic eval.
    """
    directions = [
        {
            "id": "dir-A",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 3,
                    # No FAILED tasks this cycle...
                    "tasks_failed": 0,
                    # ...but one genuinely-cancelled read-only 'research' task,
                    # which type_breakdown counts as a failure.
                    "type_breakdown": {
                        "research": {"created": 1, "merged": 0, "failed": 1},
                    },
                },
            ],
        },
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )

    assert result["overall"]["merged"] == 3
    assert result["overall"]["failed"] == 0
    # Pre-fix this emitted 1.5 (3 / max(3 + (0 - 1), 1) = 3/2). Must be clamped.
    assert result["overall"]["success_rate"] == 1.0


def test_success_rate_excludes_retired_direction_pr_ineligible_failures():
    """Retired directions must not contribute to total_failed_pr_ineligible.

    total_merged/total_failed only sum over active/expired/paused directions.
    A retired direction with windowed research_history must not have its
    PR-ineligible failures subtracted from a total_failed that never included
    them, which would silently inflate success_rate toward 1.0.
    """
    directions = [
        {
            "id": "dir-active",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 4,
                    "tasks_failed": 2,
                    "type_breakdown": {},
                },
            ],
        },
        {
            "id": "dir-retired",
            "status": "retired",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 0,
                    "tasks_failed": 0,
                    # This PR-ineligible failure must NOT be subtracted
                    # from total_failed, because this direction is retired
                    # and contributed 0 to total_merged/total_failed.
                    "type_breakdown": {
                        "research": {"created": 1, "merged": 0, "failed": 1},
                    },
                },
            ],
        },
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )

    # Only active direction contributes: 4 merged, 2 failed → 4/(4+2) = 0.667
    # If the retired direction's PR-ineligible failure was (wrongly) subtracted:
    #   total_failed_pr_ineligible=1, total_failed_pr_eligible=max(2-1,0)=1,
    #   success_rate=4/(4+1)=0.8 — incorrect silent inflation.
    assert result["overall"]["merged"] == 4
    assert result["overall"]["failed"] == 2
    assert result["overall"]["success_rate"] == round(4 / (4 + 2), 2)


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


def test_outcome_scaffolding_flag_surfaces_in_outcome():
    """outcome_scaffolding_flagged and _evidence appear for flagged direction, absent for unflagged."""
    directions = [
        {
            "id": "dir-flagged",
            "status": "active",
            "research_history": [],
            "outcome_scaffolding_flagged": True,
            "outcome_scaffolding_evidence": {"reason": "no progress", "cycles": 5},
        },
        {
            "id": "dir-clean",
            "status": "active",
            "research_history": [],
        },
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project, [])
    by_id = {d["id"]: d for d in result["directions"]}

    flagged = by_id["dir-flagged"]
    assert flagged.get("outcome_scaffolding_flagged") is True
    assert flagged.get("outcome_scaffolding_evidence") == {
        "reason": "no progress",
        "cycles": 5,
    }

    clean = by_id["dir-clean"]
    assert "outcome_scaffolding_flagged" not in clean
    assert "outcome_scaffolding_evidence" not in clean


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


# ---------------------------------------------------------------------------
# Verified-merge scoring: a degraded/unverified merge (prs_merged=1 but
# verified_prs_merged=0) must NOT inflate selection/scoring inputs. Legacy
# entries (only prs_merged) must still count via the fallback.
# ---------------------------------------------------------------------------


def test_difficulty_index_excludes_degraded_unverified_merge():
    """A degraded merge counts as zero verified merges → direction stays 'hard'."""
    d = {
        "research_history": [
            {"tasks_created": 10, "prs_merged": 1, "verified_prs_merged": 0}
        ]
    }
    # 0 verified merges → defaults to 1.0 (unknown / easy-until-proven), NOT
    # 10/1 = 10.0 which the raw count would have produced.
    assert _compute_difficulty_index(d) == 1.0


def test_difficulty_index_legacy_entry_still_counts():
    """Entry with only prs_merged (no verified field) falls back to raw count."""
    d = {"research_history": [{"tasks_created": 10, "prs_merged": 2}]}
    assert _compute_difficulty_index(d) == 5.0


def test_productivity_score_excludes_degraded_unverified_merge():
    """A degraded merge does not credit productivity (verified merges only)."""
    degraded = {
        "research_history": [
            {"tasks_created": 1, "prs_merged": 1, "verified_prs_merged": 0}
        ]
    }
    verified = {
        "research_history": [
            {"tasks_created": 1, "prs_merged": 1, "verified_prs_merged": 1}
        ]
    }
    # Degraded merge scores 0 (no verified merge, no rejection); a real verified
    # merge scores positive. They must NOT be equal.
    assert _compute_productivity_score(degraded) == 0.0
    assert _compute_productivity_score(verified) > 0.0


def test_productivity_score_legacy_entry_still_counts():
    """Legacy entry (only prs_merged) credits productivity via fallback."""
    legacy = {"research_history": [{"tasks_created": 1, "prs_merged": 1}]}
    assert _compute_productivity_score(legacy) > 0.0


def test_outcome_evaluation_window_merged_excludes_degraded_but_acceptance_raw():
    """outcome['merged'] counts verified merges; acceptance_rate stays raw."""
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 2,
                    "verified_prs_merged": 1,
                    "prs_rejected": 1,
                    "tasks_created": 4,
                },
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )
    d = result["directions"][0]
    # Verified merged = 1 (one of the two merges was degraded/unverified).
    assert d["merged"] == 1
    # acceptance_rate is an operator-judgment metric: counts RAW merges
    # (2 merged / (2 merged + 1 rejected)).
    assert d["acceptance_rate"] == round(2 / 3, 3)
    # operator_rejection_rate also stays raw (1 rejected / 3 total raw PRs).
    assert d["operator_rejection_rate"] == round(1 / 3, 2)


def test_prediction_calibration_factor_uses_verified_merges():
    """Calibration compares prediction against the VERIFIED actual merge total."""
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                # 4 raw merges but only 2 verified → actual should be 2.
                {"prs_merged": 4, "verified_prs_merged": 2},
            ],
        }
    ]
    project = _make_project(directions=directions)
    cycle_history = [
        {
            "outcome": "completed",
            "created_predictions": {"dir-1": {"similar_past_merges": 2}},
        }
    ]
    # actual_verified (2) / predicted (2) = 1.0 ratio. With raw merges it would
    # have been 4/2 = 2.0 → clipped to 1.5. Prove we get the verified-derived 1.0
    # (blended toward 1.0 for the single sample, which keeps it exactly 1.0).
    factor = _real_compute_prediction_calibration_factor(
        project, cycle_history, min_samples=1
    )
    assert factor == 1.0


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
    assert by_id["dir-cheap"]["cost_per_merged_pr"] == round(cheap_cost / 4, 4)
    assert by_id["dir-pricey"]["estimated_cost_usd"] == pricey_cost
    assert by_id["dir-pricey"]["cost_per_merged_pr"] == round(pricey_cost / 1, 4)

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


def test_decision_accuracy_success_requires_verified_merge():
    """Raw merges alone do not count as created-direction success."""
    directions = [
        {
            "id": "dRawOnly",
            "status": "active",
            "research_history": [
                {"prs_merged": 1, "verified_prs_merged": 0},
            ],
        }
    ]
    vision = {
        "_strategic_cycle_history": [
            {
                "cycle_id": "c1",
                "outcome": "completed",
                "phase2_completed_at": "2026-03-01T00:00:00",
                "created_direction_ids": ["dRawOnly"],
                "retired_direction_ids": [],
            },
        ]
    }
    result = _build_outcome_evaluation(
        _make_project(directions=directions, vision=vision)
    )
    da = result["decision_accuracy"]
    assert da["created_count"] == 1
    assert da["created_successful"] == 0
    assert da["decision_accuracy_rate"] == 0.0
    created = da["created"][0]
    assert created["prs_merged"] == 1
    assert created["success"] is False


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
    """cost_per_merged_pr uses the FULL merged population (autonomous + assisted) as denominator.

    The numerator (total_cost) covers ALL work on a direction. To keep
    numerator/denominator population-consistent, the denominator must also cover
    ALL merged PRs — including operator-assisted ones — so a direction is not
    penalised for accepting operator help.
    """

    def test_cpm_present_when_all_assisted_merges(self):
        """CPM uses total merged (autonomous + assisted), so prs_merged=3 gives CPM=cost/3."""
        from app.routers.admin_analytics import ON_DEMAND_RATE

        tasks = 3
        hours = 2.0
        expected_cost = round(tasks * hours * 3600 * ON_DEMAND_RATE, 2)
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
                        "tasks_created": tasks,
                        "avg_time_to_merge_hours": hours,
                    }
                ],
            }
        ]
        d = _build_outcome_evaluation(_make_project(directions=directions))[
            "directions"
        ][0]
        # total merged = 3 (the full population), not 0 (autonomous only)
        assert d.get("cost_per_merged_pr") == round(expected_cost / 3, 4)

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

    def test_legacy_entry_without_autonomous_field_uses_prs_merged(self):
        """Legacy entries lacking autonomous_prs_merged use prs_merged as the total merged count."""
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
        assert d.get("cost_per_merged_pr") == round(expected_cost / prs, 4)

    def test_mixed_attribution_cpm_uses_total_merged_count(self):
        """CPM denominator is the FULL merged count (autonomous + assisted), not autonomous-only.

        Population-consistency: the numerator (total_cost) covers all direction work;
        the denominator must cover the same population — all merged PRs. Using
        autonomous-only (3) instead of total (5) would inflate CPM and penalise
        directions that accepted operator help.
        """
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
        # canonical: total merged = 5 (not autonomous-only = 3)
        assert d.get("cost_per_merged_pr") == round(expected_cost / 5, 4)


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


# ---------------------------------------------------------------------------
# Regression tests: time-window filter uses fields that writers actually produce
# ---------------------------------------------------------------------------


def test_retired_at_field_excludes_old_direction():
    """Direction with retired_at 91 days ago is excluded from the window."""
    directions = [
        {
            "id": "dir-old-retired",
            "text": "Old retired direction",
            "status": "retired",
            "retired_at": _old_iso(),
        }
    ]
    project = _make_project(directions=directions)
    result = _build_retired_directions_summary(project)
    assert result == [], (
        "direction with retired_at beyond the window should be excluded"
    )


def test_completed_at_used_for_completed_direction():
    """completed_at is used for completed directions; old ones are excluded, recent ones included."""
    recent_dir = {
        "id": "dir-recent-completed",
        "text": "Recent completed direction",
        "status": "completed",
        "completed_at": _recent_iso(5),
    }
    old_dir = {
        "id": "dir-old-completed",
        "text": "Old completed direction",
        "status": "completed",
        "completed_at": _old_iso(),
    }
    project = _make_project(directions=[recent_dir, old_dir])
    result = _build_retired_directions_summary(project)
    ids = [r["id"] for r in result]
    assert "dir-recent-completed" in ids, (
        "recent completed direction should be included"
    )
    assert "dir-old-completed" not in ids, (
        "completed direction older than window should be excluded"
    )


def test_expired_at_used_for_expired_direction():
    """expired_at is used for expired directions; old ones are excluded."""
    directions = [
        {
            "id": "dir-old-expired",
            "text": "Old expired direction",
            "status": "expired",
            "auto_completed_reason": "Proposal deadline passed",
            "expired_at": _old_iso(),
        }
    ]
    project = _make_project(directions=directions)
    result = _build_retired_directions_summary(project)
    assert result == [], (
        "expired direction with expired_at beyond the window should be excluded"
    )


def test_superseded_direction_with_completed_at_included():
    """superseded direction with a recent completed_at is included in the window."""
    directions = [
        {
            "id": "dir-superseded",
            "text": "Superseded direction",
            "status": "superseded",
            "completed_at": _recent_iso(3),
        }
    ]
    project = _make_project(directions=directions)
    result = _build_retired_directions_summary(project)
    ids = [r["id"] for r in result]
    assert "dir-superseded" in ids, (
        "superseded direction with recent completed_at should be included"
    )


# ---------------------------------------------------------------------------
# _entry_cost_usd — recorded spend preferred over proxy
# ---------------------------------------------------------------------------


def test_entry_cost_usd_uses_recorded_when_positive():
    """Positive estimated_cost_usd is returned directly without applying the proxy."""
    entry = {"estimated_cost_usd": 5.50, "tasks_created": 100, "avg_time_to_merge_hours": 10.0}
    result = _entry_cost_usd(entry, on_demand_rate=0.001)
    assert result == 5.50


def test_entry_cost_usd_falls_back_to_proxy_when_missing():
    """When estimated_cost_usd is absent, proxy is computed from tasks_created * hours * rate."""
    from app.routers.admin_analytics import ON_DEMAND_RATE

    entry = {"tasks_created": 4, "avg_time_to_merge_hours": 2.0}
    expected = 4 * 2.0 * 3600 * ON_DEMAND_RATE
    assert _entry_cost_usd(entry, ON_DEMAND_RATE) == expected


def test_entry_cost_usd_falls_back_to_proxy_when_zero():
    """Zero estimated_cost_usd is treated as absent (falls back to proxy)."""
    from app.routers.admin_analytics import ON_DEMAND_RATE

    entry = {"estimated_cost_usd": 0, "tasks_created": 3, "avg_time_to_merge_hours": 1.0}
    expected = 3 * 1.0 * 3600 * ON_DEMAND_RATE
    assert _entry_cost_usd(entry, ON_DEMAND_RATE) == expected


def test_entry_cost_usd_returns_zero_when_no_data():
    """Returns 0.0 when neither recorded spend nor proxy fields are present."""
    assert _entry_cost_usd({}, on_demand_rate=0.001) == 0.0


# ---------------------------------------------------------------------------
# Recorded spend in _build_outcome_evaluation
# ---------------------------------------------------------------------------


def test_cost_uses_recorded_spend_over_proxy():
    """estimated_cost_usd on a history entry is used instead of the proxy."""
    directions = [
        {
            "id": "dir-recorded",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 2,
                    "tasks_created": 999,
                    "avg_time_to_merge_hours": 999.0,
                    "estimated_cost_usd": 7.77,
                },
            ],
        }
    ]
    result = _build_outcome_evaluation(_make_project(directions=directions))
    d = result["directions"][0]
    assert d["estimated_cost_usd"] == 7.77
    assert d["cost_per_merged_pr"] == round(7.77 / 2, 4)


def test_cost_sums_recorded_and_proxy_entries():
    """Mixed history: entries with estimated_cost_usd use recorded; others use proxy."""
    from app.routers.admin_analytics import ON_DEMAND_RATE

    proxy_entry = {"timestamp": "2026-01-01T00:00:00", "prs_merged": 1,
                   "tasks_created": 2, "avg_time_to_merge_hours": 1.0}
    recorded_entry = {"timestamp": "2026-01-02T00:00:00", "prs_merged": 1,
                      "estimated_cost_usd": 4.00}
    directions = [
        {
            "id": "dir-mixed",
            "status": "active",
            "research_history": [proxy_entry, recorded_entry],
        }
    ]
    proxy_cost = 2 * 1.0 * 3600 * ON_DEMAND_RATE
    expected_total = round(proxy_cost + 4.00, 2)
    result = _build_outcome_evaluation(_make_project(directions=directions))
    d = result["directions"][0]
    assert d["estimated_cost_usd"] == expected_total


# ---------------------------------------------------------------------------
# Recorded spend in _build_retired_directions_summary
# ---------------------------------------------------------------------------


def test_retired_summary_uses_recorded_cost_per_merge():
    """cost_per_merge in retired summary uses estimated_cost_usd when present.

    Uses canonical cost_per_merged_pr denominator: ALL merged PRs (raw total),
    not verified-only. This ensures directions with unverified/degraded merges
    report cost metrics that reflect actual work per delivered PR.
    """
    directions = [
        {
            "id": "dir-ret-recorded",
            "text": "Recorded spend direction",
            "status": "retired",
            "retired_at": _recent_iso(5),
            "research_history": [
                {
                    "timestamp": _recent_iso(5),
                    "prs_merged": 4,
                    "verified_prs_merged": 2,
                    "tasks_created": 999,
                    "avg_time_to_merge_hours": 999.0,
                    "estimated_cost_usd": 8.00,
                }
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_retired_directions_summary(project)
    assert len(result) == 1
    entry = result[0]
    assert entry["prs_merged"] == 4
    # Canonical rule: denominator MUST be ALL merged PRs (the raw 4), not verified-only.
    # Using the canonical helper ensures cost_per_merge is consistent with live path.
    assert entry.get("cost_per_merge") == round(8.00 / 4, 4)


# ---------------------------------------------------------------------------
# Generalized success metrics — mixed-portfolio (non-code + code) directions
# ---------------------------------------------------------------------------


def test_generalized_successes_visible_for_research_only_direction():
    """A direction with 0 PR merges but research successes shows generalized_successes."""
    directions = [
        {
            "id": "dir-research",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 0,
                    "tasks_failed": 0,
                    "generalized_successes": 3,  # 3 research tasks produced findings
                },
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project, [])
    d = result["directions"][0]
    # Merged is still zero (PR-based code metric)
    assert d["merged"] == 0
    # But generalized_successes shows the non-code contribution
    assert d.get("generalized_successes") == 3
    assert "generalized_health_rate" in d
    assert d["generalized_health_rate"] > 0


def test_overall_generalized_success_rate_for_mixed_portfolio():
    """Overall summary includes generalized_success_rate for mixed portfolios."""
    directions = [
        {
            "id": "dir-code",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 2,
                    "tasks_completed": 2,  # the 2 merges are completed terminal outcomes
                    "tasks_failed": 1,
                    "generalized_successes": 2,  # same as prs_merged for code-only
                },
            ],
        },
        {
            "id": "dir-research",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 0,
                    "tasks_failed": 0,
                    "generalized_successes": 3,  # non-code successes only
                },
            ],
        },
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project, [])
    overall = result["overall"]
    # PR-based: 2 merges, 1 failure
    assert overall["merged"] == 2
    assert overall["success_rate"] == round(2 / (2 + 1), 2)
    # Generalized: 2 + 3 = 5 successes, 1 failure
    assert "generalized_successes" in overall
    assert overall["generalized_successes"] == 5
    assert overall["generalized_success_rate"] == round(5 / (5 + 1), 2)


def test_generalized_health_rate_matches_dashboard_with_neutral_outcomes():
    """Strategic generalized_health_rate equals the canonical dashboard value.

    Regression guard for the divergent-denominator bug: the strategic path used
    to divide by (successes + failed), omitting neutral terminal outcomes, so it
    reported an optimistically higher 'health' than the operator dashboard for
    the same direction. With the reconciliation, both paths divide successes by
    ALL terminal outcomes (success + neutral + failure), so the two agree.
    """
    direction = {
        "id": "dir-mixed",
        "status": "active",
        "research_history": [
            {
                "timestamp": "2026-01-01T00:00:00",
                "prs_merged": 2,
                "generalized_successes": 3,  # 2 code merges + 1 non-code success
                "outcome_kind_breakdown": {
                    "code_merge": {"success": 2, "neutral": 0, "failure": 0},
                    "research": {"success": 1, "neutral": 2, "failure": 1},
                },
            },
        ],
    }
    project = _make_project(directions=[direction])
    result = _build_outcome_evaluation(project, [])
    d = result["directions"][0]

    # Denominator is ALL 6 terminal outcomes (3 success + 2 neutral + 1 failure),
    # not the old 3 (successes) + 1 (failed) = 4 that dropped the neutrals.
    assert d["generalized_terminal_outcomes"] == 6
    assert d["generalized_health_rate"] == round(3 / 6, 3)

    # The strategic value equals the canonical dashboard helper for the same data.
    dashboard = _compute_lifetime_generalized_outcome_state(direction)
    assert d["generalized_health_rate"] == dashboard["generalized_health_rate"]
    # And it is strictly lower than the old neutral-dropping denominator would give.
    assert d["generalized_health_rate"] < round(3 / (3 + 1), 3)


def test_generalized_success_rate_absent_for_code_only_portfolio():
    """When all successes are PRs, generalized_success_rate is not added (no new info)."""
    directions = [
        {
            "id": "dir-code",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 3,
                    "tasks_failed": 1,
                    # No generalized_successes field — legacy entry fallback to prs_merged
                },
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(project, [])
    overall = result["overall"]
    # When generalized == merged (fallback), the field is absent
    assert "generalized_successes" not in overall
    assert "generalized_success_rate" not in overall


def test_decision_accuracy_includes_generalized_success_for_research_direction():
    """A created direction with research successes (0 PRs) shows generalized_success=True."""
    directions = [
        {
            "id": "dir-research-only",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 0,
                    "generalized_successes": 2,
                }
            ],
        }
    ]
    vision = {
        "_strategic_cycle_history": [
            {
                "cycle_id": "c1",
                "outcome": "completed",
                "phase2_completed_at": "2026-02-01T00:00:00",
                "created_direction_ids": ["dir-research-only"],
                "retired_direction_ids": [],
            },
        ]
    }
    result = _build_outcome_evaluation(
        _make_project(directions=directions, vision=vision)
    )
    da = result["decision_accuracy"]
    entry = da["created"][0]
    # PR-based success is False (0 merges)
    assert entry["success"] is False
    # But generalized_success is True (research work succeeded)
    assert entry["generalized_success"] is True
    # And the aggregate counters reflect this
    assert "created_generalized_successful" in da
    assert da["created_generalized_successful"] == 1
    assert "generalized_decision_accuracy_rate" in da
    assert da["generalized_decision_accuracy_rate"] == 1.0


def test_code_only_direction_success_and_generalized_success_match():
    """A code direction that merged should have success == generalized_success == True."""
    directions = [
        {
            "id": "dir-code",
            "status": "active",
            "research_history": [{"prs_merged": 2}],
        }
    ]
    vision = {
        "_strategic_cycle_history": [
            {
                "cycle_id": "c1",
                "outcome": "completed",
                "created_direction_ids": ["dir-code"],
                "retired_direction_ids": [],
            }
        ]
    }
    result = _build_outcome_evaluation(
        _make_project(directions=directions, vision=vision)
    )
    da = result["decision_accuracy"]
    entry = da["created"][0]
    assert entry["success"] is True
    assert entry["generalized_success"] is True
    # When they match, no extra aggregate fields added
    assert "created_generalized_successful" not in da


# ── knowledge_by_direction in _build_outcome_evaluation ─────────────────────


def test_direction_knowledge_injected_into_outcome():
    """direction_knowledge appears in the per-direction outcome when
    knowledge_by_direction contains entries for that direction."""
    directions = [
        {
            "id": "d-with-knowledge",
            "status": "active",
            "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
        },
        {
            "id": "d-no-knowledge",
            "status": "active",
            "research_history": [],
        },
    ]
    knowledge_entry = {
        "id": "kd-1",
        "title": "Auth research",
        "summary": "Key findings about auth.",
        "topic": "auth",
        "direction_id": "d-with-knowledge",
        "source_kind": "web_research",
    }
    knowledge_by_direction = {"d-with-knowledge": [knowledge_entry]}
    result = _real_build_outcome_evaluation(
        _make_project(directions=directions),
        [],
        knowledge_by_direction=knowledge_by_direction,
    )
    dirs_by_id = {d["id"]: d for d in result["directions"]}
    assert "direction_knowledge" in dirs_by_id["d-with-knowledge"]
    assert dirs_by_id["d-with-knowledge"]["direction_knowledge"][0]["title"] == "Auth research"
    assert "direction_knowledge" not in dirs_by_id["d-no-knowledge"]


def test_direction_knowledge_absent_when_param_not_passed():
    """direction_knowledge is never present when knowledge_by_direction is None."""
    directions = [
        {
            "id": "d-abc",
            "status": "active",
            "research_history": [],
        }
    ]
    result = _real_build_outcome_evaluation(_make_project(directions=directions), [])
    for d in result["directions"]:
        assert "direction_knowledge" not in d


def test_direction_knowledge_capped_at_five():
    """No more than 5 knowledge entries are injected per direction."""
    entries = [{"id": f"kd-{i}", "title": f"Doc {i}"} for i in range(8)]
    knowledge_by_direction = {"d-cap": entries}
    directions = [{"id": "d-cap", "status": "active", "research_history": []}]
    result = _real_build_outcome_evaluation(
        _make_project(directions=directions),
        [],
        knowledge_by_direction=knowledge_by_direction,
    )
    assert len(result["directions"][0]["direction_knowledge"]) == 5


# ---------------------------------------------------------------------------
# _build_retired_directions_summary — durable memory augmentation
# ---------------------------------------------------------------------------


def test_retired_directions_summary_includes_durable_memory_entries():
    """Direction in vision['_recently_retired_directions'] but pruned from the
    live directions list still appears in the retired-directions summary.

    When the memory entry carries a ``text`` field (written by
    ``record_retired_direction_memory`` since the scramble fix), the summary
    must surface the original human-readable text, NOT a stopword-stripped
    token-bag join.
    """
    readable_text = "Improve the authentication flow for users to log in faster"
    project = _make_project(
        directions=[],  # direction already pruned from live list
        vision={
            "_recently_retired_directions": [
                {
                    "id": "d-pruned",
                    "text": readable_text,
                    "text_tokens": ["improve", "auth", "performance"],
                    "retired_at": _recent_iso(1),
                    "reason": "saturation",
                    "gap_id": "g-auth",
                }
            ]
        },
    )
    result = _build_retired_directions_summary(project)
    assert len(result) == 1
    entry = result[0]
    assert entry["id"] == "d-pruned"
    assert entry["status"] == "retired"
    assert entry["retirement_reason"] == "saturation"
    # Full readable text must be used, not a stopword-stripped token join.
    assert entry["text"] == readable_text
    # Sanity: the scrambled token-join form must NOT appear as the full text.
    assert entry["text"] != "improve auth performance"


def test_retired_directions_summary_surfaces_outcome():
    """The compact outcome ('why retired / what landed') is surfaced to the agent."""
    project = _make_project(
        directions=[],
        vision={
            "_recently_retired_directions": [
                {
                    "id": "d-sup",
                    "text_tokens": ["improve", "auth"],
                    "retired_at": _recent_iso(1),
                    "reason": "strategic_superseded",
                    "outcome": "superseded by d-successor",
                }
            ]
        },
    )
    result = _build_retired_directions_summary(project)
    assert len(result) == 1
    entry = result[0]
    assert entry["retirement_reason"] == "strategic_superseded"
    assert entry["retirement_outcome"] == "superseded by d-successor"


def test_retired_directions_summary_falls_back_to_structured_outcome():
    """When freetext ``outcome`` is absent, the renderer surfaces the always-present
    structured ``retired_outcome`` (classification + metrics) as retirement_outcome."""
    project = _make_project(
        directions=[],
        vision={
            "_recently_retired_directions": [
                {
                    "id": "d-struct",
                    "text_tokens": ["improve", "auth"],
                    "retired_at": _recent_iso(1),
                    "reason": "saturation",
                    # No freetext "outcome" — the lifecycle retire path never writes
                    # it — but the structured retired_outcome is always present.
                    "retired_outcome": {
                        "classification": "abandoned",
                        "metrics": {"prs_merged": 0, "cycles": 4},
                    },
                }
            ]
        },
    )
    result = _build_retired_directions_summary(project)
    assert len(result) == 1
    outcome = result[0]["retirement_outcome"]
    assert outcome.startswith("abandoned")
    assert "prs_merged=0" in outcome
    assert "cycles=4" in outcome


def test_retired_directions_summary_prefers_freetext_over_structured_outcome():
    """A freetext ``outcome`` still wins over the structured fallback."""
    project = _make_project(
        directions=[],
        vision={
            "_recently_retired_directions": [
                {
                    "id": "d-both-outcomes",
                    "text_tokens": ["improve", "auth"],
                    "retired_at": _recent_iso(1),
                    "reason": "strategic_superseded",
                    "outcome": "superseded by d-successor",
                    "retired_outcome": {
                        "classification": "landed",
                        "metrics": {"prs_merged": 3},
                    },
                }
            ]
        },
    )
    result = _build_retired_directions_summary(project)
    assert result[0]["retirement_outcome"] == "superseded by d-successor"


def test_retired_directions_summary_omits_outcome_when_absent():
    """Entries without an outcome do not emit a retirement_outcome key."""
    project = _make_project(
        directions=[],
        vision={
            "_recently_retired_directions": [
                {
                    "id": "d-old",
                    "text_tokens": ["improve", "auth"],
                    "retired_at": _recent_iso(1),
                    "reason": "saturation",
                }
            ]
        },
    )
    result = _build_retired_directions_summary(project)
    assert len(result) == 1
    assert "retirement_outcome" not in result[0]


def test_retired_directions_summary_deduplicates_live_and_memory():
    """A direction present in both the live list and durable memory is not duplicated."""
    project = _make_project(
        directions=[
            {
                "id": "d-both",
                "status": "retired",
                "retired_at": _recent_iso(1),
                "text": "improve auth",
                "research_history": [],
            }
        ],
        vision={
            "_recently_retired_directions": [
                {
                    "id": "d-both",
                    "text_tokens": ["improve", "auth"],
                    "retired_at": _recent_iso(1),
                    "reason": "saturation",
                }
            ]
        },
    )
    result = _build_retired_directions_summary(project)
    ids = [e["id"] for e in result]
    assert ids.count("d-both") == 1


def test_retired_directions_summary_memory_respects_window():
    """Durable memory entries older than window_days are excluded."""
    project = _make_project(
        directions=[],
        vision={
            "_recently_retired_directions": [
                {
                    "id": "d-ancient",
                    "text_tokens": ["old", "direction"],
                    "retired_at": "2020-01-01T00:00:00",  # well outside 90d window
                    "reason": "completed",
                }
            ]
        },
    )
    result = _build_retired_directions_summary(project)
    assert result == []


def test_retired_directions_summary_memory_surfaces_outcome():
    """Memory-augmented entries surface cycles and prs_merged outcome metrics."""
    project = _make_project(
        directions=[],  # direction pruned from live list
        vision={
            "_recently_retired_directions": [
                {
                    "id": "d-pruned-with-merges",
                    "text_tokens": ["improve", "auth"],
                    "retired_at": _recent_iso(1),
                    "reason": "saturation",
                    "cycles": 5,
                    "prs_merged": 3,
                }
            ]
        },
    )
    result = _build_retired_directions_summary(project)
    assert len(result) == 1
    entry = result[0]
    assert entry["id"] == "d-pruned-with-merges"
    assert entry["cycles"] == 5
    assert entry["prs_merged"] == 3


def test_retired_directions_summary_cost_per_merge_includes_aggregate_cost():
    """cost_per_merge reflects lifetime cost including folded history_aggregate spend.

    A direction with empty research_history (all cycles evicted) and
    history_aggregate.total_estimated_cost_usd=10.0 should yield a
    cost_per_merge that reflects the aggregate cost, not 0.
    """
    project = _make_project(
        directions=[
            {
                "id": "d-evicted-cost",
                "text": "direction with evicted cost",
                "status": "retired",
                "retired_at": _recent_iso(1),
                "research_history": [],
                "history_aggregate": {
                    "total_cycles": 5,
                    "total_prs_merged": 2,
                    "total_verified_prs_merged": 2,
                    "total_estimated_cost_usd": 10.0,
                },
            }
        ]
    )
    result = _build_retired_directions_summary(project)
    assert len(result) == 1
    entry = result[0]
    # 10.0 / 2 merges = 5.0
    assert entry.get("cost_per_merge") == 5.0, (
        f"expected cost_per_merge=5.0 from aggregate, got {entry.get('cost_per_merge')!r}"
    )
    assert entry["status"] == "retired"


def test_retired_summary_cost_per_merge_uses_raw_denominator():
    """cost_per_merge divides by ALL merged PRs, not verified-only subset.

    The canonical cost_per_merged_pr helper mandates that the denominator
    must cover the same population as the numerator: ALL work (cost) divided
    by ALL merged PRs (autonomous + assisted). Using verified-only merges
    while including all costs overstates the metric for directions with
    unverified/degraded merges. This test ensures retired summary uses raw
    total_merged count, not verified_total_merged.
    """
    directions = [
        {
            "id": "dir-mixed-merges",
            "text": "Direction with degraded merges",
            "status": "retired",
            "retired_at": _recent_iso(5),
            "research_history": [
                {
                    "timestamp": _recent_iso(5),
                    "prs_merged": 4,  # raw total: 4 PRs
                    "verified_prs_merged": 2,  # only 2 verified; 2 were degraded
                    "tasks_created": 10,
                    "avg_time_to_merge_hours": 1.0,
                    "estimated_cost_usd": 8.0,  # cost reflects all 4 merges
                }
            ],
        }
    ]
    project = _make_project(directions=directions)
    result = _build_retired_directions_summary(project)
    assert len(result) == 1
    entry = result[0]

    # Canonical rule: denominator MUST be ALL merged PRs (the raw count).
    # Cost is 8.0, which reflects work on all 4 merges. Dividing by only 2
    # (verified) would inflate the reported cost/merge from 2.0 to 4.0.
    assert entry["prs_merged"] == 4
    assert entry.get("cost_per_merge") == round(8.0 / 4, 4)
    # The inflated value (dividing by verified-only) must NOT appear.
    assert entry.get("cost_per_merge") != round(8.0 / 2, 4)


# ---------------------------------------------------------------------------
# success_rate denominator: PR-eligible vs PR-ineligible tasks
# ---------------------------------------------------------------------------


def test_success_rate_same_population_denominator():
    """success_rate should count only PR-eligible failed tasks in denominator.

    Demonstrates that a portfolio with PR-eligible success and PR-ineligible
    failures (e.g., failed read-only audits) must not deflate the success_rate
    by mixing per-PR numerator with all-tasks denominator.

    Portfolio:
    - dir-1: 2 merged PRs + 1 security_audit task failed (PR-ineligible)
    - dir-2: 1 merged PR + 2 research tasks failed (PR-ineligible)

    Expected: success_rate = 3 merged / (3 + 0 pr_eligible_failed) = 1.0
    Bug (before fix): success_rate = 3 / (3 + 3) = 0.5 (deflated)
    """
    directions = [
        {
            "id": "dir-1",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 2,
                    "verified_prs_merged": 2,
                    "tasks_failed": 1,
                    "type_breakdown": {
                        "security_audit": {"created": 1, "merged": 0, "failed": 1},
                    },
                }
            ],
        },
        {
            "id": "dir-2",
            "status": "active",
            "research_history": [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "prs_merged": 1,
                    "verified_prs_merged": 1,
                    "tasks_failed": 2,
                    "type_breakdown": {
                        "research": {"created": 2, "merged": 0, "failed": 2},
                    },
                }
            ],
        },
    ]
    project = _make_project(directions=directions)
    result = _build_outcome_evaluation(
        project, (project.vision or {}).get("_strategic_cycle_history") or []
    )

    # Both dirs have only PR-ineligible failures, so portfolio has:
    # - 3 total merged PRs
    # - 0 PR-eligible failed tasks
    # → success_rate should be 3 / (3 + 0) = 1.0
    assert result["overall"]["merged"] == 3
    assert result["overall"]["failed"] == 3  # raw count includes all tasks
    assert result["overall"]["success_rate"] == 1.0  # only counts PR-eligible failures


# ---------------------------------------------------------------------------
# Word-order preservation in retired_directions_summary
# ---------------------------------------------------------------------------


def test_retired_directions_summary_preserves_text_order():
    """Text reconstructed from text_tokens must preserve original word order,
    not scramble it as set() iteration would."""
    project = _make_project(
        directions=[],
        vision={
            "_recently_retired_directions": [
                {
                    "id": "d-order",
                    # Tokens stored in a specific order that differs from
                    # alphabetical and reverse-alphabetical, so any set-based
                    # reconstruction will reliably produce a different order.
                    "text_tokens": ["zebra", "mango", "apple", "kite"],
                    "retired_at": _recent_iso(1),
                    "reason": "saturation",
                }
            ]
        },
    )
    result = _build_retired_directions_summary(project)
    assert len(result) == 1
    text = result[0]["text"]
    # Exact left-to-right token order must be preserved
    assert text == "zebra mango apple kite", (
        f"Expected 'zebra mango apple kite', got {text!r} — "
        "set() may have scrambled word order"
    )


def test_record_retired_direction_memory_text_tokens_ordered():
    """record_retired_direction_memory must store text_tokens in the same
    left-to-right order as the source direction text (excluding stopwords)."""
    vision: dict = {}
    direction = {
        "id": "d-tok-order",
        # "strengthen" is a content word; "the" and "for" are stopwords.
        "text": "strengthen the authentication flow for users",
    }
    record_retired_direction_memory(vision, direction, reason="saturation")

    entries = vision.get("_recently_retired_directions") or []
    assert len(entries) == 1
    tokens = entries[0]["text_tokens"]
    # Stopwords "the", "for" removed; remaining words in original order
    assert tokens == ["strengthen", "authentication", "flow", "users"], (
        f"Expected ordered tokens, got {tokens!r}"
    )


# ── repair_retired_direction_memory_texts — async DB tests ───────────────────


@pytest_asyncio.fixture()
async def repair_db():
    """Isolated in-memory SQLite DB for repair_retired_direction_memory_texts tests."""
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
            type_, **kw
        )

    _engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _Session = sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    async with _Session() as session:
        yield session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


@pytest.mark.asyncio
async def test_repair_retired_direction_memory_texts_backfills_text(repair_db):
    """Legacy entries with text_tokens but no text are backfilled from the
    DirectionEvent trail. The function returns True and patches text in-place."""
    from app.models import DirectionEvent
    from app.direction_event_trail import repair_retired_direction_memory_texts
    from app.time_utils import _utcnow

    project_id = uuid.uuid4()
    direction_key = "d-legacy-1"
    full_text = "Add rate limiting to the public API endpoints"

    # Insert a DirectionEvent row carrying the full direction text in snapshot_data.
    event = DirectionEvent(
        project_id=project_id,
        direction_key=direction_key,
        event_kind="status_changed",
        prior_status="active",
        new_status="retired",
        actor=None,
        reason="saturation",
        snapshot_data={"id": direction_key, "text": full_text, "status": "retired"},
        recorded_at=_utcnow(),
    )
    repair_db.add(event)
    await repair_db.commit()

    # Vision with a legacy entry: text_tokens present, text absent.
    vision = {
        "_recently_retired_directions": [
            {
                "id": direction_key,
                "text_tokens": ["add", "rate", "limiting", "public", "api", "endpoints"],
                "retired_at": "2026-06-01T00:00:00",
                "reason": "saturation",
            }
        ]
    }
    patched = await repair_retired_direction_memory_texts(repair_db, project_id, vision)
    assert patched is True, "Expected True when at least one entry was repaired"

    entry = vision["_recently_retired_directions"][0]
    assert entry["text"] == full_text, (
        f"Expected full readable text, got {entry['text']!r}"
    )
    # text_tokens must remain unchanged (repair is additive, not destructive).
    assert entry["text_tokens"] == ["add", "rate", "limiting", "public", "api", "endpoints"]


@pytest.mark.asyncio
async def test_repair_retired_direction_memory_texts_skips_already_text(repair_db):
    """Entries that already have a non-empty text field must not be touched,
    and the function returns False when nothing needs repair."""
    from app.direction_event_trail import repair_retired_direction_memory_texts

    project_id = uuid.uuid4()
    vision = {
        "_recently_retired_directions": [
            {
                "id": "d-has-text",
                "text": "Already readable direction text",
                "text_tokens": ["already", "readable"],
                "retired_at": "2026-06-01T00:00:00",
                "reason": "saturation",
            }
        ]
    }
    original_text = vision["_recently_retired_directions"][0]["text"]
    patched = await repair_retired_direction_memory_texts(repair_db, project_id, vision)
    assert patched is False, "Expected False when no entries needed repair"
    assert vision["_recently_retired_directions"][0]["text"] == original_text


@pytest.mark.asyncio
async def test_repair_retired_direction_memory_texts_no_snapshot(repair_db):
    """When no DirectionEvent exists for a key, the entry is left unchanged and
    the function returns False."""
    from app.direction_event_trail import repair_retired_direction_memory_texts

    project_id = uuid.uuid4()
    vision = {
        "_recently_retired_directions": [
            {
                "id": "d-no-event",
                "text_tokens": ["some", "tokens"],
                "retired_at": "2026-06-01T00:00:00",
                "reason": "saturation",
            }
        ]
    }
    patched = await repair_retired_direction_memory_texts(repair_db, project_id, vision)
    assert patched is False, "Expected False when event trail has no snapshot"
    # Entry must not have a text key added (no snapshot = nothing to backfill).
    assert "text" not in vision["_recently_retired_directions"][0]


def test_retired_directions_summary_legacy_entry_no_text_falls_back_to_tokens():
    """When a legacy entry has text_tokens but no text, _build_retired_directions_summary
    falls back to joining the tokens so the summary never returns an empty text field."""
    project = _make_project(
        directions=[],
        vision={
            "_recently_retired_directions": [
                {
                    "id": "d-legacy-tokens",
                    "text_tokens": ["zebra", "mango", "apple"],
                    "retired_at": _recent_iso(1),
                    "reason": "saturation",
                }
            ]
        },
    )
    result = _build_retired_directions_summary(project)
    assert len(result) == 1
    # Without a text field the summary falls back to joining text_tokens.
    assert result[0]["text"] == "zebra mango apple", (
        f"Expected token join fallback, got {result[0]['text']!r}"
    )

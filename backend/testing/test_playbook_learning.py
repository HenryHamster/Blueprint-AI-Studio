"""Playbook test suite (split).

Tests exercising the app.playbook_learning surface, split out of the former
testing/test_playbook_auto.py monolith. Behavior-preserving: test bodies
are copied verbatim; only file boundaries and shared-helper imports change.
"""


# ─────────────────────────────────────────────────────────────────────────────
# summarize_decomposition_outcomes
# ─────────────────────────────────────────────────────────────────────────────


def test_summarize_decomposition_outcomes_empty_history():
    from app.playbook_learning import summarize_decomposition_outcomes

    assert summarize_decomposition_outcomes({}) is None
    assert summarize_decomposition_outcomes({"research_history": []}) is None


def test_summarize_decomposition_outcomes_no_decomposition_quality():
    from app.playbook_learning import summarize_decomposition_outcomes

    direction = {
        "research_history": [
            {"tasks_created": 2},
            {"tasks_created": 3, "decomposition_quality": []},
            {"tasks_created": 1, "decomposition_quality": "not-a-list"},
        ]
    }
    assert summarize_decomposition_outcomes(direction) is None


def test_summarize_decomposition_outcomes_single_cycle_averages():
    from app.playbook_learning import summarize_decomposition_outcomes

    direction = {
        "research_history": [
            {
                "tasks_created": 2,
                "decomposition_quality": [
                    {
                        "subtask_merge_rate": 0.8,
                        "avg_files_per_subtask": 2.0,
                        "max_files_in_subtask": 3,
                        "dependency_depth": 1,
                        "cross_module_count": 0,
                    },
                    {
                        "subtask_merge_rate": 0.6,
                        "avg_files_per_subtask": 4.0,
                        "max_files_in_subtask": 5,
                        "dependency_depth": 2,
                        "cross_module_count": 1,
                    },
                ],
            }
        ]
    }
    out = summarize_decomposition_outcomes(direction)
    assert out is not None
    assert out["prior_decomposition_count"] == 2
    assert out["avg_subtask_merge_rate"] == 0.7
    assert out["avg_files_per_subtask"] == 3.0
    assert out["max_files_in_subtask"] == 5
    assert out["avg_dependency_depth"] == 1.5
    assert out["max_dependency_depth"] == 2
    assert out["high_cross_module_correlates_with_low_merge"] is False


def test_summarize_decomposition_outcomes_cross_module_correlation_flag():
    from app.playbook_learning import summarize_decomposition_outcomes

    direction = {
        "research_history": [
            {
                "tasks_created": 1,
                "decomposition_quality": [
                    {"subtask_merge_rate": 0.9, "cross_module_count": 0},
                    {"subtask_merge_rate": 0.85, "cross_module_count": 1},
                    {"subtask_merge_rate": 0.4, "cross_module_count": 3},
                    {"subtask_merge_rate": 0.3, "cross_module_count": 2},
                ],
            }
        ]
    }
    out = summarize_decomposition_outcomes(direction)
    assert out is not None
    assert out["high_cross_module_correlates_with_low_merge"] is True


def test_summarize_decomposition_outcomes_skips_non_numeric():
    from app.playbook_learning import summarize_decomposition_outcomes

    direction = {
        "research_history": [
            {
                "tasks_created": 1,
                "decomposition_quality": [
                    {
                        "subtask_merge_rate": "nope",
                        "avg_files_per_subtask": None,
                        "max_files_in_subtask": "x",
                        "dependency_depth": None,
                        "cross_module_count": "y",
                    },
                    {
                        "subtask_merge_rate": 0.5,
                        "avg_files_per_subtask": 2.0,
                        "max_files_in_subtask": 2,
                        "dependency_depth": 1,
                        "cross_module_count": 0,
                    },
                ],
            }
        ]
    }
    out = summarize_decomposition_outcomes(direction)
    assert out is not None
    assert out["prior_decomposition_count"] == 2
    assert out["avg_subtask_merge_rate"] == 0.5
    assert out["avg_files_per_subtask"] == 2.0
    assert out["max_files_in_subtask"] == 2


def test_summarize_decomposition_outcomes_only_last_three_productive():
    from app.playbook_learning import summarize_decomposition_outcomes

    def _cycle(rate, created=1):
        return {
            "tasks_created": created,
            "decomposition_quality": [
                {"subtask_merge_rate": rate, "cross_module_count": 0}
            ],
        }

    direction = {
        "research_history": [
            _cycle(0.1),  # productive but outside last-3 window
            _cycle(0.0, created=0),  # unproductive — skipped entirely
            _cycle(0.5),
            _cycle(0.5),
            _cycle(0.5),
        ]
    }
    out = summarize_decomposition_outcomes(direction)
    assert out is not None
    assert out["prior_decomposition_count"] == 3
    assert out["avg_subtask_merge_rate"] == 0.5


# ─────────────────────────────────────────────────────────────────────────────
# Tests for select_sr_miss_examples_for_injection
# ─────────────────────────────────────────────────────────────────────────────

_SAMPLE_TRENDS: dict = {
    "categories": {
        "correctness": {
            "weighted_miss_rate": 0.8,
            "examples": [{"severity": "MAJOR", "desc": "Global correctness issue"}],
        },
        "error-handling": {
            "weighted_miss_rate": 0.5,
            "examples": [{"severity": "MINOR", "desc": "Global error handling gap"}],
        },
    },
    "by_direction": {
        "dir-x": {
            "categories": {
                "security": {
                    "miss_rate": 0.9,
                    "examples": [
                        {"severity": "CRITICAL", "desc": "Direction X security miss"}
                    ],
                },
            }
        },
        "dir-empty": {
            "categories": {
                "correctness": {
                    "miss_rate": 0.7,
                    # no examples key → direction has no qualifying examples
                },
            }
        },
    },
}


def test_select_sr_miss_examples_global_when_no_direction_id():
    """When direction_id is None, global categories are used."""
    from app.playbook_learning import select_sr_miss_examples_for_injection

    result = select_sr_miss_examples_for_injection(_SAMPLE_TRENDS, direction_id=None)
    # Should include global categories ranked by weighted_miss_rate
    assert "correctness" in result
    assert any("Global correctness issue" in ex["desc"] for ex in result["correctness"])
    # Global ranking preserved: correctness (0.8) before error-handling (0.5)
    keys = list(result.keys())
    assert keys.index("correctness") < keys.index("error-handling")


def test_select_sr_miss_examples_prefers_direction_when_examples_present():
    """When direction_id is provided and has examples, those are preferred over global."""
    from app.playbook_learning import select_sr_miss_examples_for_injection

    result = select_sr_miss_examples_for_injection(_SAMPLE_TRENDS, direction_id="dir-x")
    # Direction-scoped category should be present
    assert "security" in result
    assert any("Direction X security miss" in ex["desc"] for ex in result["security"])
    # Global-only category must not appear (direction slice was used instead)
    assert "correctness" not in result
    assert "error-handling" not in result


def test_select_sr_miss_examples_falls_back_to_global_when_direction_has_no_examples():
    """When the direction has no categories with examples, fall back to global."""
    from app.playbook_learning import select_sr_miss_examples_for_injection

    result = select_sr_miss_examples_for_injection(
        _SAMPLE_TRENDS, direction_id="dir-empty"
    )
    # dir-empty has no examples, so global categories are used
    assert "correctness" in result
    assert "security" not in result


def test_select_sr_miss_examples_falls_back_to_global_for_unknown_direction():
    """When direction_id is not found in by_direction, fall back to global."""
    from app.playbook_learning import select_sr_miss_examples_for_injection

    result = select_sr_miss_examples_for_injection(
        _SAMPLE_TRENDS, direction_id="dir-unknown"
    )
    assert "correctness" in result


def test_select_sr_miss_examples_empty_trends_returns_empty():
    """When sr_catch_rate_trends has no categories, return empty dict."""
    from app.playbook_learning import select_sr_miss_examples_for_injection

    assert select_sr_miss_examples_for_injection({}) == {}
    assert select_sr_miss_examples_for_injection({"categories": {}}) == {}



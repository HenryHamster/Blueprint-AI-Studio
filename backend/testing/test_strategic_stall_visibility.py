"""Focused tests for advisory stall visibility in Strategic payloads."""

from types import SimpleNamespace

from app.strategic_spawning import _build_direction_stats


def test_direction_stats_surface_latest_bounded_stall_advisory() -> None:
    project = SimpleNamespace(
        directions=[
            {
                "id": "d-stalled",
                "text": "Prove the outcome",
                "priority": "high",
                "status": "active",
                "research_history": [],
                "history_aggregate": {},
                "stall_advisory": {
                    "version": 1,
                    "blocking_invariant": "No production observation",
                    "invariant_fingerprint": "a" * 64,
                    "new_evidence_since_previous_cycle": [
                        "Synthetic replay still passes",
                        "No production sample yet",
                    ],
                    "recommendation": "pivot",
                    "continuation_pivot_rationale": "Change the measurement source",
                    "repeat_without_evidence_count": 2,
                    "warning": True,
                    "recorded_at": "2026-07-28T20:00:00+00:00",
                    "cycle_index": 7,
                    "cycle_task_id": "task-7",
                    "untrusted_extra": "must not enter the agent payload",
                },
            }
        ]
    )

    _, stats = _build_direction_stats(project)

    advisory = stats[0]["stall_advisory"]
    assert advisory["blocking_invariant"] == "No production observation"
    assert advisory["new_evidence_since_previous_cycle"] == [
        "Synthetic replay still passes",
        "No production sample yet",
    ]
    assert advisory["recommendation"] == "pivot"
    assert advisory["continuation_pivot_rationale"] == "Change the measurement source"
    assert advisory["repeat_without_evidence_count"] == 2
    assert advisory["warning"] is True
    assert "untrusted_extra" not in advisory


def test_direction_stats_omit_malformed_stall_advisory() -> None:
    project = SimpleNamespace(
        directions=[
            {
                "id": "d-active",
                "text": "Keep moving",
                "priority": "medium",
                "status": "active",
                "research_history": [],
                "history_aggregate": {},
                "stall_advisory": "not-a-record",
            }
        ]
    )

    _, stats = _build_direction_stats(project)

    assert "stall_advisory" not in stats[0]

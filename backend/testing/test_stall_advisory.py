from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.models import Project
from app.direction_lifecycle import _reset_outcome_detection_state
from app.stall_advisory import (
    STALL_ADVISORY_HISTORY_MAX,
    apply_stall_advisory,
    blocking_invariant_fingerprint,
    clear_current_stall_advisory,
    normalize_stall_advisory_fields,
)
from app.strategic_completion import (
    EVIDENCE_PROGRESS_CONTEXT_KEY,
    _apply_evidence_progress_verdicts,
    _reset_scaffolding_state_on_outcome_change,
)


def _stall_fields(**overrides):
    fields = {
        "stall_advisory_version": 1,
        "blocking_invariant": "  Runner   cannot allocate GPU  ",
        "new_evidence_since_previous_cycle": [],
        "recommendation": "continue",
        "continuation_pivot_rationale": "Wait for the scheduled capacity window.",
    }
    fields.update(overrides)
    return fields


def test_stall_fields_are_optional_for_legacy_stalled_calls():
    assert normalize_stall_advisory_fields(
        {"direction_id": "d-1", "status": "stalled", "rationale": "legacy"}
    ) is None


def test_stall_fields_are_normalized_and_partial_records_rejected():
    normalized = normalize_stall_advisory_fields(_stall_fields())
    assert normalized is not None
    assert normalized["blocking_invariant"] == "Runner cannot allocate GPU"
    assert len(normalized["invariant_fingerprint"]) == 64
    assert normalized["invariant_fingerprint"] == blocking_invariant_fingerprint(
        "runner cannot allocate gpu"
    )

    with pytest.raises(ValueError, match="stall_advisory_version must be 1"):
        normalize_stall_advisory_fields({"blocking_invariant": "same"})
    with pytest.raises(ValueError, match="missing required stall advisory fields"):
        normalize_stall_advisory_fields({"stall_advisory_version": 1})
    with pytest.raises(ValueError, match="must be one of"):
        normalize_stall_advisory_fields(_stall_fields(recommendation="retire"))
    with pytest.raises(ValueError, match="must be a list"):
        normalize_stall_advisory_fields(
            _stall_fields(new_evidence_since_previous_cycle="none")
        )


def test_repeat_count_warning_and_history_are_bounded_without_enforcement():
    direction = {"id": "d-1", "status": "active"}
    normalized = normalize_stall_advisory_fields(_stall_fields())
    assert normalized is not None

    first = apply_stall_advisory(
        direction,
        normalized,
        recorded_at="2026-01-01T00:00:00Z",
        cycle_index=1,
        cycle_task_id="t-1",
    )
    assert first["repeat_without_evidence_count"] == 0
    assert first["warning"] is False

    second = apply_stall_advisory(
        direction,
        normalized,
        recorded_at="2026-01-02T00:00:00Z",
        cycle_index=2,
        cycle_task_id="t-2",
    )
    assert second["repeat_without_evidence_count"] == 1
    assert second["warning"] is True

    third = apply_stall_advisory(
        direction,
        normalized,
        recorded_at="2026-01-03T00:00:00Z",
        cycle_index=3,
        cycle_task_id="t-3",
    )
    assert third["repeat_without_evidence_count"] == 2
    assert third["warning"] is True
    assert direction["status"] == "active"
    assert "blocked" not in direction
    assert "retired_at" not in direction

    for index in range(STALL_ADVISORY_HISTORY_MAX + 5):
        changed = normalize_stall_advisory_fields(
            _stall_fields(blocking_invariant=f"invariant {index}")
        )
        assert changed is not None
        apply_stall_advisory(
            direction,
            changed,
            recorded_at=f"2026-02-{(index % 28) + 1:02d}T00:00:00Z",
            cycle_index=index + 4,
            cycle_task_id=f"t-{index + 4}",
        )
    assert len(direction["stall_advisory_history"]) == STALL_ADVISORY_HISTORY_MAX


def test_replaying_same_cycle_is_noop_without_incrementing_repeat():
    direction = {"id": "d-1", "status": "active"}
    normalized = normalize_stall_advisory_fields(_stall_fields())
    assert normalized is not None
    first = apply_stall_advisory(
        direction,
        normalized,
        recorded_at="first",
        cycle_index=1,
        cycle_task_id="t-1",
    )
    replay = apply_stall_advisory(
        direction,
        normalized,
        recorded_at="replayed",
        cycle_index=1,
        cycle_task_id="t-1",
    )
    assert first["repeat_without_evidence_count"] == 0
    assert replay["repeat_without_evidence_count"] == 0
    assert replay["warning"] is False
    assert len(direction["stall_advisory_history"]) == 1


def test_late_replay_does_not_reorder_history_or_regress_current():
    direction = {"id": "d-1", "status": "active"}
    t1 = normalize_stall_advisory_fields(_stall_fields())
    t2 = normalize_stall_advisory_fields(
        _stall_fields(blocking_invariant="Second blocker")
    )
    assert t1 is not None and t2 is not None
    first = apply_stall_advisory(
        direction, t1, recorded_at="first", cycle_index=1, cycle_task_id="t-1"
    )
    second = apply_stall_advisory(
        direction, t2, recorded_at="second", cycle_index=2, cycle_task_id="t-2"
    )
    replay = apply_stall_advisory(
        direction, t1, recorded_at="late", cycle_index=1, cycle_task_id="t-1"
    )
    assert replay is first
    assert [r["cycle_task_id"] for r in direction["stall_advisory_history"]] == [
        "t-1",
        "t-2",
    ]
    assert direction["stall_advisory"] is second


def test_new_evidence_resets_repeat_and_progress_clears_only_current():
    direction = {"id": "d-1", "status": "active"}
    no_delta = normalize_stall_advisory_fields(_stall_fields())
    assert no_delta is not None
    apply_stall_advisory(
        direction,
        no_delta,
        recorded_at="now",
        cycle_index=1,
        cycle_task_id="t-1",
    )
    with_delta = normalize_stall_advisory_fields(
        _stall_fields(new_evidence_since_previous_cycle=["Capacity request accepted"])
    )
    assert with_delta is not None
    latest = apply_stall_advisory(
        direction,
        with_delta,
        recorded_at="later",
        cycle_index=2,
        cycle_task_id="t-2",
    )
    assert latest["repeat_without_evidence_count"] == 0
    assert clear_current_stall_advisory(direction) is True
    assert "stall_advisory" not in direction
    assert len(direction["stall_advisory_history"]) == 2


def test_evidence_progress_persists_advisory_and_observe_advancing_clears_it(mocker):
    mocker.patch.dict(
        "app.strategic_completion._runtime_config",
        {"strategic_challenge_gate_mode": "observe"},
    )
    project = Project(
        name="stall-advisory",
        user_id="u1",
        directions=[
            {
                "id": "d-1",
                "status": "active",
                "research_history": [],
                "history_aggregate": {},
            }
        ],
    )
    stall = normalize_stall_advisory_fields(_stall_fields())
    assert stall is not None
    stalled_task = SimpleNamespace(
        id=uuid.uuid4(),
        context={
            EVIDENCE_PROGRESS_CONTEXT_KEY: [
                {
                    "direction_id": "d-1",
                    "status": "stalled",
                    "rationale": "same blocker",
                    "stall_advisory": stall,
                }
            ]
        },
    )
    assert _apply_evidence_progress_verdicts(stalled_task, project) == 1
    assert project.directions[0]["stall_advisory"]["recommendation"] == "continue"

    advancing_task = SimpleNamespace(
        id=uuid.uuid4(),
        context={
            EVIDENCE_PROGRESS_CONTEXT_KEY: [
                {
                    "direction_id": "d-1",
                    "status": "advancing",
                    "rationale": "new proof",
                }
            ]
        },
    )
    assert _apply_evidence_progress_verdicts(advancing_task, project) == 1
    assert "stall_advisory" not in project.directions[0]
    assert len(project.directions[0]["stall_advisory_history"]) == 1


def test_enforce_advancing_without_challenge_does_not_clear_advisory(mocker):
    mocker.patch.dict(
        "app.strategic_completion._runtime_config",
        {"strategic_challenge_gate_mode": "enforce"},
    )
    project = Project(
        name="stall-advisory-enforce",
        user_id="u1",
        directions=[
            {
                "id": "d-1",
                "status": "active",
                "research_history": [],
                "history_aggregate": {},
                "stall_advisory": {"warning": True},
                "stall_advisory_history": [{"warning": True}],
            }
        ],
    )
    task = SimpleNamespace(
        id=uuid.uuid4(),
        context={
            EVIDENCE_PROGRESS_CONTEXT_KEY: [
                {
                    "direction_id": "d-1",
                    "status": "advancing",
                    "rationale": "unchallenged",
                }
            ]
        },
    )
    assert _apply_evidence_progress_verdicts(task, project) == 1
    assert project.directions[0]["stall_advisory"] == {"warning": True}


def test_outcome_contract_change_clears_current_but_retains_history():
    direction = {
        "research_history": [],
        "history_aggregate": {},
        "stall_advisory": {"warning": True},
        "stall_advisory_history": [{"warning": True}],
    }
    assert _reset_scaffolding_state_on_outcome_change(direction) is True
    assert "stall_advisory" not in direction
    assert direction["stall_advisory_history"] == [{"warning": True}]

    direction.update(
        {
            "stall_advisory": {"warning": True},
            "stall_advisory_history": [{"warning": True}],
        }
    )
    _reset_outcome_detection_state(direction)
    assert "stall_advisory" not in direction
    assert direction["stall_advisory_history"] == [{"warning": True}]

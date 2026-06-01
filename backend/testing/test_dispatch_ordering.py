"""Unit tests for the extracted dispatch-ordering seam.

The orchestrator's Step 2.8 (health-aware reordering + confidence floor),
Step 2.9 (pipeline-slot cap) and the slot-reservation promotion were extracted
out of ``orchestrator_worker`` into:

  - ``app.orchestrator_health.reorder_eligible_by_health``
  - ``app.orchestrator_filters.cap_pipeline_slots``
  - ``app.orchestrator_filters.reserve_slots_for_types``

These tests exercise each transform directly (previously they were only
reachable by reimplementing the inline logic in the test). They assert the
list-shaping behavior: degradation-gated reorder, confidence floor, review/fix
budget cap, and reserved-type promotion under queue pressure.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.orchestrator_filters import (
    cap_pipeline_slots,
    reserve_slots_for_types,
    apply_project_fairness,
    _project_fairness_weight,
)
from app.orchestrator_health import reorder_eligible_by_health


def _task(tid, *, ttype=None, scope=None, risk=None, project_id=None):
    """Minimal candidate-task stand-in exposing the attributes the seam reads."""
    ctx = {}
    if scope is not None:
        ctx["scope"] = scope
    if risk is not None:
        ctx["risk"] = risk
    payload = {"type": ttype} if ttype is not None else {}
    return SimpleNamespace(
        id=tid,
        context=ctx,
        project_id=project_id,
        first_payload=payload,
    )


class _FakeResult:
    def __init__(self, scalar_value):
        self._scalar = scalar_value

    def scalar(self):
        return self._scalar


class _FakeSession:
    """Session whose ``execute`` returns a fixed scalar (the active count)."""

    def __init__(self, scalar_value):
        self._scalar = scalar_value

    async def execute(self, _stmt):
        return _FakeResult(self._scalar)


# ---------------------------------------------------------------------------
# reorder_eligible_by_health
# ---------------------------------------------------------------------------

_HEALTHY_ZONES = {"available": 3, "total_zones": 3}
_DEGRADED_ZONES = {"available": 1, "total_zones": 3}


@pytest.mark.asyncio
async def test_reorder_noop_when_single_task():
    tasks = [_task("a", scope="small")]
    out = await reorder_eligible_by_health(
        ["a"],
        tasks,
        recent_results=[],
        slots_available=8,
        runtime_config={"max_concurrent_vms": 8},
        zone_summary=_DEGRADED_ZONES,
        session=_FakeSession(0),
        predict_fn=lambda scope, direction, proj: 0.9,
    )
    assert out == ["a"]


@pytest.mark.asyncio
async def test_reorder_noop_when_no_degradation_signal():
    # Healthy zones, no failures, slots at full capacity -> no degradation.
    tasks = [_task("a", scope="medium"), _task("b", scope="small")]
    out = await reorder_eligible_by_health(
        ["a", "b"],
        tasks,
        recent_results=[],
        slots_available=8,
        runtime_config={"max_concurrent_vms": 8, "dispatch_confidence_floor": 0.0},
        zone_summary=_HEALTHY_ZONES,
        session=_FakeSession(0),
        predict_fn=lambda scope, direction, proj: 0.1,
    )
    assert out == ["a", "b"]


@pytest.mark.asyncio
async def test_reorder_sorts_by_predicted_success_under_degradation():
    tasks = [_task("low", scope="medium"), _task("high", scope="small")]
    # Degradation via low slots (< 50% of max). predict_fn favors the small task.
    scores = {"medium": 0.2, "small": 0.8}
    out = await reorder_eligible_by_health(
        ["low", "high"],
        tasks,
        recent_results=[],
        slots_available=1,
        runtime_config={"max_concurrent_vms": 8, "dispatch_confidence_floor": 0.0},
        zone_summary=_HEALTHY_ZONES,
        session=_FakeSession(0),
        predict_fn=lambda scope, direction, proj: scores[scope],
    )
    assert out == ["high", "low"]


@pytest.mark.asyncio
async def test_reorder_confidence_floor_drops_sub_threshold_tasks():
    tasks = [_task("keep", scope="small"), _task("drop", scope="medium")]
    scores = {"small": 0.6, "medium": 0.05}
    out = await reorder_eligible_by_health(
        ["keep", "drop"],
        tasks,
        recent_results=[],
        slots_available=1,  # degradation signal
        runtime_config={"max_concurrent_vms": 8, "dispatch_confidence_floor": 0.10},
        zone_summary=_HEALTHY_ZONES,
        session=_FakeSession(0),
        predict_fn=lambda scope, direction, proj: scores[scope],
    )
    assert out == ["keep"]


@pytest.mark.asyncio
async def test_reorder_degradation_from_failure_rate():
    # >30% failures in the recent window triggers degradation even with healthy
    # zones and full slots.
    tasks = [_task("low", scope="medium"), _task("high", scope="small")]
    scores = {"medium": 0.2, "small": 0.8}
    recent = [("x", "failure"), ("y", "failure"), ("z", "success")]
    out = await reorder_eligible_by_health(
        ["low", "high"],
        tasks,
        recent_results=recent,
        slots_available=8,
        runtime_config={"max_concurrent_vms": 8, "dispatch_confidence_floor": 0.0},
        zone_summary=_HEALTHY_ZONES,
        session=_FakeSession(0),
        predict_fn=lambda scope, direction, proj: scores[scope],
    )
    assert out == ["high", "low"]


# ---------------------------------------------------------------------------
# cap_pipeline_slots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cap_defers_review_fix_past_budget_but_passes_others():
    tasks = [
        _task("r1", ttype="review"),
        _task("f1", ttype="fix"),
        _task("c1", ttype="create"),
        _task("rs1", ttype="research"),
        _task("r2", ttype="review"),
    ]
    eligible = ["r1", "f1", "c1", "rs1", "r2"]
    # cap=2, no active pipeline tasks -> budget 2: keep first two review/fix,
    # defer the third (r2). create/research always pass.
    out = await cap_pipeline_slots(
        eligible,
        tasks,
        runtime_config={"max_concurrent_vms": 8, "max_pipeline_slots": 2},
        session=_FakeSession(0),
    )
    assert out == ["r1", "f1", "c1", "rs1"]


@pytest.mark.asyncio
async def test_cap_budget_reduced_by_active_pipeline_tasks():
    tasks = [_task("r1", ttype="review"), _task("c1", ttype="create")]
    eligible = ["r1", "c1"]
    # cap=2 but 2 already active -> budget 0: review deferred, create kept.
    out = await cap_pipeline_slots(
        eligible,
        tasks,
        runtime_config={"max_concurrent_vms": 8, "max_pipeline_slots": 2},
        session=_FakeSession(2),
    )
    assert out == ["c1"]


@pytest.mark.asyncio
async def test_cap_defaults_to_half_of_max_vms_when_unset():
    tasks = [_task(f"r{i}", ttype="review") for i in range(6)]
    eligible = [f"r{i}" for i in range(6)]
    # max_pipeline_slots unset -> default max(1, 8//2) = 4.
    out = await cap_pipeline_slots(
        eligible,
        tasks,
        runtime_config={"max_concurrent_vms": 8},
        session=_FakeSession(0),
    )
    assert out == ["r0", "r1", "r2", "r3"]


# ---------------------------------------------------------------------------
# reserve_slots_for_types
# ---------------------------------------------------------------------------


def test_reserve_noop_without_queue_pressure():
    tasks = [_task("a", ttype="review"), _task("b", ttype="create")]
    eligible = ["a", "b"]
    # slots_available >= len(eligible) -> no pressure -> unchanged.
    out = reserve_slots_for_types(
        eligible,
        tasks,
        runtime_config={"reserved_slots_for_types": {"create": 1}},
        slots_available=2,
    )
    assert out == ["a", "b"]


def test_reserve_promotes_reserved_type_under_pressure():
    tasks = [
        _task("r1", ttype="review"),
        _task("r2", ttype="review"),
        _task("c1", ttype="create"),
    ]
    eligible = ["r1", "r2", "c1"]
    # slots_available=2 -> window is [r1, r2]; create reserved 1 but absent from
    # window -> promote c1, displacing the last non-reserved (r2). The displaced
    # task is dropped from the dispatch list for this cycle (it remains QUEUED in
    # the DB and becomes eligible again next cycle) — preserved upstream behavior.
    out = reserve_slots_for_types(
        eligible,
        tasks,
        runtime_config={"reserved_slots_for_types": {"create": 1}},
        slots_available=2,
    )
    assert out == ["r1", "c1"]


def test_reserve_no_promotion_when_reserved_type_already_present():
    tasks = [
        _task("c1", ttype="create"),
        _task("r1", ttype="review"),
        _task("r2", ttype="review"),
    ]
    eligible = ["c1", "r1", "r2"]
    # create already in the [c1, r1] window -> nothing to promote.
    out = reserve_slots_for_types(
        eligible,
        tasks,
        runtime_config={"reserved_slots_for_types": {"create": 1}},
        slots_available=2,
    )
    assert out == ["c1", "r1", "r2"]


# ---------------------------------------------------------------------------
# apply_project_fairness  (Step 2.95 — round-robin across projects)
# ---------------------------------------------------------------------------


def _by_project(tasks):
    return {t.id: t.project_id for t in tasks}


def _weights(tasks):
    return {t.id: _project_fairness_weight(getattr(t, "priority", 0)) for t in tasks}


def test_fairness_noop_for_single_project():
    tasks = [_task(t, project_id="P") for t in ("a", "b", "c")]
    eligible = ["a", "b", "c"]
    out = apply_project_fairness(eligible, _by_project(tasks))
    assert out == eligible  # nothing to interleave


def test_fairness_breaks_single_project_monopoly_in_window():
    # P1 fanned out 40 tasks, P2 + P3 one each — the candidate scan orders all
    # 40 P1 tasks first, so without fairness a 4-slot window dispatches only
    # P1 and starves P2/P3 for cycles. Fairness must surface P2 + P3.
    tasks = [_task(f"p1_{i}", project_id="P1") for i in range(40)]
    tasks += [_task("p2", project_id="P2"), _task("p3", project_id="P3")]
    eligible = [f"p1_{i}" for i in range(40)] + ["p2", "p3"]
    out = apply_project_fairness(eligible, _by_project(tasks))
    # Permutation — no task lost.
    assert sorted(out) == sorted(eligible)
    # A realistic 4-slot dispatch window now serves all three projects.
    window = out[:4]
    assert "p2" in window and "p3" in window, (
        f"fairness must break P1 monopoly within the window, got {window}"
    )


def test_fairness_composes_with_slot_cap():
    # End-to-end composition mirroring the dispatch loop: fairness reorders,
    # then the slot cap (eligible_ids[:slots_available]) truncates. Two projects
    # each with 3 tasks, 4 slots → fairness guarantees the smaller project is
    # represented rather than fully crowded out.
    tasks = [_task(f"a{i}", project_id="A") for i in range(3)]
    tasks += [_task(f"b{i}", project_id="B") for i in range(3)]
    eligible = ["a0", "a1", "a2", "b0", "b1", "b2"]
    fair = apply_project_fairness(eligible, _by_project(tasks), _weights(tasks))
    slots_available = 4
    dispatched = fair[:slots_available]
    assert any(t.startswith("a") for t in dispatched)
    assert any(t.startswith("b") for t in dispatched)
    # Round-robin (weight 1 each): a0, b0, a1, b1.
    assert dispatched == ["a0", "b0", "a1", "b1"]


def test_fairness_weight_from_priority_gives_more_turns():
    # P1 tasks priority 2 (weight 3), P2 tasks priority 0 (weight 1). P1 draws
    # 3 per round but P2 is still serviced every round — no starvation.
    p1 = [_task(f"p1_{i}", project_id="P1") for i in range(4)]
    for t in p1:
        t.priority = 2
    p2 = [_task(f"p2_{i}", project_id="P2") for i in range(2)]
    for t in p2:
        t.priority = 0
    tasks = p1 + p2
    eligible = [t.id for t in tasks]
    out = apply_project_fairness(eligible, _by_project(tasks), _weights(tasks))
    assert sorted(out) == sorted(eligible)
    # Round 1: 3 P1 (weight 3) then 1 P2 (weight 1).
    assert out[:4] == ["p1_0", "p1_1", "p1_2", "p2_0"]

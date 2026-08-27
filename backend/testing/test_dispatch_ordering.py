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
    apply_completion_priority,
    cap_pipeline_slots,
    reserve_slots_for_types,
    apply_project_fairness,
    dispatch_window_size,
    filter_failing_types,
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
async def test_reorder_preserves_task_priority_under_degradation():
    release_audit = _task("release-audit", scope="medium")
    release_audit.priority = 100
    ordinary_small = _task("ordinary-small", scope="small", risk="low")
    ordinary_small.priority = 50
    tasks = [release_audit, ordinary_small]
    scores = {"medium": 0.2, "small": 0.9}

    out = await reorder_eligible_by_health(
        ["release-audit", "ordinary-small"],
        tasks,
        recent_results=[],
        slots_available=1,
        runtime_config={"max_concurrent_vms": 8, "dispatch_confidence_floor": 0.0},
        zone_summary=_HEALTHY_ZONES,
        session=_FakeSession(0),
        predict_fn=lambda scope, direction, proj: scores[scope],
    )

    assert out == ["release-audit", "ordinary-small"]


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
    assert out[:2] == ["r1", "c1"]
    assert out == ["r1", "c1", "r2"]


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


@pytest.mark.asyncio
async def test_urgent_release_audit_survives_all_dispatch_reorders():
    audit = _task("audit", ttype="release_audit", scope="medium")
    audit.priority = 100
    ordinary = _task("ordinary", ttype="create", scope="small", risk="low")
    ordinary.priority = 50
    tasks = [audit, ordinary]

    health_ordered = await reorder_eligible_by_health(
        [audit.id, ordinary.id],
        tasks,
        recent_results=[],
        slots_available=1,
        runtime_config={"max_concurrent_vms": 8, "dispatch_confidence_floor": 0.0},
        zone_summary=_HEALTHY_ZONES,
        session=_FakeSession(0),
        predict_fn=lambda scope, direction, proj: 0.2 if scope == "medium" else 0.9,
    )
    completion_ordered = apply_completion_priority(
        health_ordered,
        {audit.id: "release_audit", ordinary.id: "create"},
        {audit.id: None, ordinary.id: None},
        set(),
        set(),
    )
    reserved = reserve_slots_for_types(
        completion_ordered,
        tasks,
        {"reserved_slots_for_types": {"create": 1}},
        slots_available=1,
    )

    assert reserved == [audit.id, ordinary.id]


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


# ── Dispatch-window backfill + per-type failure-gate withholding ───────────────
# A dispatch-window position must only ever be spent on a task that can ACTUALLY
# dispatch this cycle. Regression cover for: window slots consumed by candidates
# that dispatch nothing (failure-gated types, compute holds) and never backfilled,
# starving healthy tasks ranked just below the cutoff.


class TestDispatchWindowSize:
    def test_window_is_never_narrower_than_the_slot_count(self):
        """Widening must never drop a task the old `[:slots_available]` cut kept."""
        for slots in (1, 2, 5, 10, 50, 200):
            assert dispatch_window_size(slots) >= slots

    def test_window_scans_past_the_slot_count_to_allow_backfill(self):
        """The whole point: there must be room BEHIND the slots for backfill."""
        for slots in (1, 5, 25):
            assert dispatch_window_size(slots) > slots

    def test_zero_slots_scans_nothing(self):
        assert dispatch_window_size(0) == 0
        assert dispatch_window_size(-1) == 0

    def test_overscan_is_bounded(self):
        """A queue of non-dispatchable heads must not turn one cycle into an
        unbounded scan — the window stays proportional to the slot count."""
        assert dispatch_window_size(100) == 300

    def test_healthy_task_behind_a_wall_of_held_heads_is_inside_the_window(self):
        """The reported bug, at the seam.

        5 slots; the 10 highest-ranked candidates are compute experiments held for
        an offline runner (they dispatch NOTHING). Under the old cut the window was
        exactly `eligible[:5]` — all held — so the healthy task at index 10 got ZERO
        slots every cycle for up to 6h despite free capacity. It must now be inside
        the scanned window so the dispatch loop can backfill onto it.
        """
        eligible = [f"held-{i}" for i in range(10)] + ["healthy"]
        window = eligible[: dispatch_window_size(5)]
        assert "healthy" in window
        # …and the held heads keep their rank: order is preserved, not reshuffled.
        assert window[:10] == [f"held-{i}" for i in range(10)]


class TestFilterFailingTypes:
    def test_failing_type_candidates_are_withheld_from_the_window(self):
        tasks = [
            _task("a", ttype="review"),
            _task("b", ttype="research"),
            _task("c", ttype="review"),
        ]
        kept, withheld = filter_failing_types(
            ["a", "b", "c"], tasks, {"review"}
        )
        assert kept == ["b"], "failure-gated tasks must not occupy window slots"
        assert withheld == {"review"}

    def test_relative_order_of_survivors_is_preserved(self):
        """Filtering is a subsequence op — it must not perturb the fairness /
        completion-priority / slot-reservation permutations applied upstream."""
        tasks = [_task(x, ttype=("bad" if x in ("b", "d") else "good")) for x in "abcde"]
        kept, _ = filter_failing_types(list("abcde"), tasks, {"bad"})
        assert kept == ["a", "c", "e"]

    def test_no_failing_types_is_a_noop(self):
        tasks = [_task("a", ttype="review"), _task("b", ttype="research")]
        kept, withheld = filter_failing_types(["a", "b"], tasks, set())
        assert kept == ["a", "b"]
        assert withheld == set()

    def test_withheld_types_are_reported_for_the_stall_detector(self):
        """The all-types-blocked stall alert reasons over the types that WANTED to
        dispatch. Withholding them from the window must not hide them from it, or
        the alert would never fire again."""
        tasks = [_task("a", ttype="review"), _task("b", ttype="fix")]
        kept, withheld = filter_failing_types(["a", "b"], tasks, {"review", "fix"})
        assert kept == []
        assert withheld == {"review", "fix"}, (
            "the detector must still see every type it just withheld"
        )

    def test_healthy_task_wins_the_slot_freed_by_a_gated_queue_head(self):
        """End-to-end at the seam: a single slot, a failure-gated task at the head.

        Before: window = ['gated'] → the gate skipped it → ZERO dispatches.
        After:  'gated' is withheld pre-cut, so the healthy task takes the slot.
        """
        tasks = [_task("gated", ttype="review"), _task("healthy", ttype="research")]
        kept, _ = filter_failing_types(["gated", "healthy"], tasks, {"review"})
        assert kept[:1] == ["healthy"]


# ---------------------------------------------------------------------------
# Requeued tasks with a non-generic execution site must reach a terminal state
# ---------------------------------------------------------------------------
#
# A walkthrough_e2e task is pinned at birth to a site-proven local runner by
# the qualifier.  If it is ever requeued (rate-limit / dead-auth / pull-skip),
# generic dispatch cannot re-place it (orchestrator_runner_dispatch fails the
# site closed), so it used to sit QUEUED forever with a warning every dispatch
# cycle — and the qualifier's finalize treats that non-terminal child as
# walkthrough_e2e_task_not_terminal, deadlocking the qualification.  These
# tests pin the dispatch seam that now fails such a task closed to a terminal
# state with a readable reason naming the execution site.


def test_unplaceable_execution_site_reason_names_non_generic_site():
    from app.orchestrator_filters import unplaceable_execution_site_reason

    reason = unplaceable_execution_site_reason("walkthrough_e2e")
    assert reason is not None, (
        "a walkthrough_e2e task that is QUEUED can never be placed by generic "
        "dispatch — the seam must return a terminal-failure reason"
    )
    assert "local_runner" in reason, (
        f"the reason must name the execution site so the operator knows why the "
        f"task failed; got: {reason!r}"
    )
    assert "walkthrough_e2e" in reason, (
        f"the reason must name the task type; got: {reason!r}"
    )


def test_unplaceable_execution_site_reason_is_none_for_generic_sites():
    from app.orchestrator_filters import unplaceable_execution_site_reason

    for generic_type in ("create", "refactor", "review"):
        assert unplaceable_execution_site_reason(generic_type) is None, (
            f"generic task type {generic_type!r} must stay generically "
            "dispatchable — no terminal-failure reason"
        )
    # Unknown types conservatively resolve to the default matrix too.
    assert unplaceable_execution_site_reason("not_a_real_type") is None


@pytest.mark.asyncio
async def test_requeued_non_generic_site_task_reaches_terminal_state(db_session):
    """A QUEUED walkthrough_e2e task is failed closed by the dispatch seam.

    After the seam runs the task is FAILED (not QUEUED), so it drops out of the
    QUEUED dispatch window and the per-cycle warning stops.  The message names
    the execution site so the operator and the qualifier's finalize see exactly
    why no path could place it.
    """
    from app.orchestrator_filters import fail_unplaceable_non_generic_site_task
    from testing.factories import make_project, make_task
    from app.models import TaskStatus

    project = make_project(name="stranded-walkthrough", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "walkthrough_e2e"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    failed = await fail_unplaceable_non_generic_site_task(
        db_session,
        task,
        task_type="walkthrough_e2e",
    )
    assert failed is True
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED, (
        "the requeued non-generic-site task must reach a terminal state instead "
        f"of staying QUEUED forever; status={task.status}"
    )
    assert "local_runner" in (task.latest_status_message or ""), (
        f"the terminal reason must name the execution site; got: "
        f"{task.latest_status_message!r}"
    )
    from app.failure_classification import UNPLACEABLE_EXECUTION_SITE

    assert (task.context or {}).get("failure_category") == (
        UNPLACEABLE_EXECUTION_SITE
    ), (
        "an unplaceable non-generic-site task is a PERMANENT routing-contract "
        "failure, not a transient PROVISION_FAILURE (which would be resurrected "
        "onto the same dead-end QUEUED state forever)"
    )

    # A later dispatch pass must not re-queue or re-warn: the seam keeps an
    # already-terminal task terminal (it has dropped out of the QUEUED window).
    failed_again = await fail_unplaceable_non_generic_site_task(
        db_session,
        task,
        task_type="walkthrough_e2e",
    )
    assert failed_again is True
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED, (
        "a second dispatch pass must leave the task terminal — it must not be "
        f"re-admitted to the QUEUED dispatch window; status={task.status}"
    )


@pytest.mark.asyncio
async def test_non_generic_site_seam_leaves_generic_tasks_alone(db_session):
    """The seam is a no-op for generically-placeable task types — it must never
    terminalize ordinary QUEUED work."""
    from app.orchestrator_filters import fail_unplaceable_non_generic_site_task
    from testing.factories import make_project, make_task
    from app.models import TaskStatus

    project = make_project(name="ordinary-create", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "ordinary work"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    failed = await fail_unplaceable_non_generic_site_task(
        db_session,
        task,
        task_type="create",
    )
    assert failed is False
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED, (
        "generic task types must be left for the normal dispatch matrix, not "
        f"failed by the unplaceable-site seam; status={task.status}"
    )

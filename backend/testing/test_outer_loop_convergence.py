"""Unit tests for digest_metrics.compute_outer_loop_convergence.

Covers the OUTER review<->fix PR-cycle aggregation: cycles-to-merge distribution,
churn/drift rollups, terminal outcomes, escalation-reason parsing from Alerts, the
convergence-rate formula, the created-before-window filter, and the empty window.
"""

from datetime import timedelta
from uuid import uuid4

import pytest

from app.digest_metrics import compute_outer_loop_convergence
from app.models import TaskStatus
from testing.factories import make_task, make_alert, make_project, _utcnow


def _task(project_id, *, review_loop=None, ctx_extra=None, created_at=None, status=TaskStatus.COMPLETED):
    ctx = {}
    if review_loop is not None:
        ctx["review_loop"] = review_loop
    if ctx_extra:
        ctx.update(ctx_extra)
    return make_task(
        project_id=project_id,
        status=status,
        context=ctx,
        created_at=created_at or _utcnow(),
    )


def test_empty_window_returns_zeros():
    res = compute_outer_loop_convergence(
        all_tasks=[],
        escalation_alerts=[],
        window_start=_utcnow() - timedelta(hours=24),
    )
    assert res["prs_with_review_loop"] == 0
    assert res["iteration_median"] is None
    assert res["convergence_rate"] is None
    assert res["escalation_total"] == 0
    assert res["outcomes"]["merged"] == 0


def test_merged_and_rejected_convergence_rate():
    """merged / (merged + rejected + unresolved-escalations)."""
    pid = uuid4()
    tasks = [
        _task(pid, review_loop={"iteration": 1}, ctx_extra={"pr_merged": True}),
        _task(pid, review_loop={"iteration": 2}, ctx_extra={"pr_merged": True}),
        _task(pid, review_loop={"iteration": 3}, ctx_extra={"pr_merged": True}),
        _task(pid, review_loop={"iteration": 4}, ctx_extra={"pr_rejected": True}),
    ]
    res = compute_outer_loop_convergence(
        all_tasks=tasks,
        escalation_alerts=[],
        window_start=_utcnow() - timedelta(hours=24),
    )
    assert res["prs_with_review_loop"] == 4
    assert res["outcomes"]["merged"] == 3
    assert res["outcomes"]["rejected"] == 1
    # 3 / (3 + 1 + 0) = 0.75
    assert res["convergence_rate"] == 0.75


def test_iteration_distribution_and_percentiles():
    pid = uuid4()
    tasks = [
        _task(pid, review_loop={"iteration": 1}, ctx_extra={"pr_merged": True}),
        _task(pid, review_loop={"iteration": 2}, ctx_extra={"pr_merged": True}),
        _task(pid, review_loop={"iteration": 3}, ctx_extra={"pr_merged": True}),
        _task(pid, review_loop={"iteration": 7}, ctx_extra={"pr_rejected": True}),
    ]
    res = compute_outer_loop_convergence(
        all_tasks=tasks,
        escalation_alerts=[],
        window_start=_utcnow() - timedelta(hours=24),
    )
    assert res["iteration_distribution"] == {"1": 1, "2": 1, "3": 1, "4+": 1}
    assert res["iteration_median"] == 3
    assert res["iteration_p95"] == 7


def test_churn_and_drift_rollup():
    pid = uuid4()
    tasks = [
        _task(
            pid,
            review_loop={"iteration": 2, "churn_detections": 2, "drift_signal": {"drift_in": [{"file": "a.py"}], "drift_out": []}},
            ctx_extra={"pr_merged": True},
        ),
        _task(
            pid,
            review_loop={"iteration": 3, "churn_detections": 1, "drift_signal": {"drift_in": [], "drift_out": []}},
            ctx_extra={"pr_rejected": True},
        ),
        _task(pid, review_loop={"iteration": 1}, ctx_extra={"pr_merged": True}),
    ]
    res = compute_outer_loop_convergence(
        all_tasks=tasks,
        escalation_alerts=[],
        window_start=_utcnow() - timedelta(hours=24),
    )
    assert res["churn_total"] == 3
    assert res["churn_pr_count"] == 2
    # only the first task has a non-empty drift signal
    assert res["drift_pr_count"] == 1


def test_cap_exhausted_and_repeat_fingerprint_outcomes():
    pid = uuid4()
    tasks = [
        _task(
            pid,
            review_loop={"iteration": 16},
            ctx_extra={"pr_rejected": True, "failure_detail": "review_cap_exhausted", "review_cap_iterations": 16},
            status=TaskStatus.FAILED,
        ),
        _task(
            pid,
            review_loop={"iteration": 4},
            ctx_extra={"blocked_repeat_fingerprint": "abc123"},
            status=TaskStatus.FAILED,
        ),
    ]
    res = compute_outer_loop_convergence(
        all_tasks=tasks,
        escalation_alerts=[],
        window_start=_utcnow() - timedelta(hours=24),
    )
    assert res["outcomes"]["cap_exhausted"] == 1
    assert res["outcomes"]["repeat_fingerprint_halt"] == 1
    # cap-exhausted sets pr_rejected
    assert res["outcomes"]["rejected"] == 1


def test_structured_terminal_outcome_marker_rolls_up():
    pid = uuid4()
    original = _task(
        pid,
        review_loop=None,
        ctx_extra={
            "review_loop_terminal_outcome": {
                "kind": "stuck",
                "reason": "stuck",
                "pr_number": 77,
                "iterations": 5,
            }
        },
    )
    orig_id = str(original.id)
    tasks = [
        original,
        _task(pid, review_loop={"iteration": 1, "original_task_id": orig_id}),
        _task(pid, review_loop={"iteration": 2, "original_task_id": orig_id}),
    ]
    res = compute_outer_loop_convergence(
        all_tasks=tasks,
        escalation_alerts=[],
        window_start=_utcnow() - timedelta(hours=24),
    )
    assert res["terminal_outcome_histogram"]["stuck"] == 1
    assert res["incidence_counts"]["stuck"] == 1
    assert res["incidence_counts"]["overall_escalations"] == 1
    assert res["incidence_rates"]["stuck"] == 1.0


def test_later_merge_overrides_stale_structured_escalation_marker():
    pid = uuid4()
    original = _task(
        pid,
        review_loop=None,
        ctx_extra={
            "pr_merged": True,
            "review_loop_terminal_outcome": {
                "kind": "stuck",
                "reason": "stuck",
                "pr_number": 88,
                "iterations": 4,
            },
        },
    )
    orig_id = str(original.id)
    tasks = [
        original,
        _task(pid, review_loop={"iteration": 1, "original_task_id": orig_id}),
        _task(pid, review_loop={"iteration": 2, "original_task_id": orig_id}),
    ]
    res = compute_outer_loop_convergence(
        all_tasks=tasks,
        escalation_alerts=[],
        window_start=_utcnow() - timedelta(hours=24),
    )
    assert res["outcomes"]["merged"] == 1
    assert res["convergence_rate"] == 1.0
    assert res["terminal_outcome_histogram"]["merged"] == 1
    assert res["terminal_outcome_histogram"]["stuck"] == 0
    assert res["incidence_counts"]["stuck"] == 0


def test_escalation_reason_parsing_from_alerts():
    pid = uuid4()
    t_stuck = _task(pid, review_loop={"iteration": 5})
    t_standoff = _task(pid, review_loop={"iteration": 6})
    tasks = [t_stuck, t_standoff]
    alerts = [
        make_alert(pid, task_id=t_stuck.id, title="Review-fix escalation: stuck cycle on PR #42"),
        make_alert(pid, task_id=t_standoff.id, title="Review-fix escalation: dispute standoff on PR #43"),
        make_alert(pid, task_id=t_standoff.id, title="Review-fix escalation: repeat-fingerprint halt on PR #43"),
        # Unrelated alert must be ignored.
        make_alert(pid, task_id=None, title="Circuit breaker opened: autonomous tasks paused"),
    ]
    res = compute_outer_loop_convergence(
        all_tasks=tasks,
        escalation_alerts=alerts,
        window_start=_utcnow() - timedelta(hours=24),
    )
    assert res["escalation_by_reason"]["stuck"] == 1
    assert res["escalation_by_reason"]["standoff"] == 1
    assert res["escalation_by_reason"]["repeat-fingerprint"] == 1
    # distinct escalated tasks (t_stuck + t_standoff), not alert count
    assert res["escalation_total"] == 2


def test_unresolved_escalation_lowers_convergence_rate():
    """A task that escalated but never reached merged/rejected counts against convergence."""
    pid = uuid4()
    t_merged = _task(pid, review_loop={"iteration": 1}, ctx_extra={"pr_merged": True})
    t_escalated = _task(pid, review_loop={"iteration": 8})  # no terminal flag
    alerts = [
        make_alert(pid, task_id=t_escalated.id, title="Review-fix escalation: stuck cycle on PR #99"),
    ]
    res = compute_outer_loop_convergence(
        all_tasks=[t_merged, t_escalated],
        escalation_alerts=alerts,
        window_start=_utcnow() - timedelta(hours=24),
    )
    # merged=1, rejected=0, unresolved-escalation=1 -> 1/2 = 0.5
    assert res["convergence_rate"] == 0.5


def test_tasks_created_before_window_excluded():
    pid = uuid4()
    now = _utcnow()
    window_start = now - timedelta(hours=24)
    in_window = _task(pid, review_loop={"iteration": 1}, ctx_extra={"pr_merged": True}, created_at=now - timedelta(hours=1))
    old = _task(pid, review_loop={"iteration": 2}, ctx_extra={"pr_merged": True}, created_at=now - timedelta(hours=48))
    res = compute_outer_loop_convergence(
        all_tasks=[in_window, old],
        escalation_alerts=[],
        window_start=window_start,
    )
    assert res["prs_with_review_loop"] == 1
    assert res["outcomes"]["merged"] == 1


def test_multi_iteration_single_chain_counts_once():
    """A k-cycle PR spawns k review_loop tasks sharing original_task_id — they
    must collapse to ONE chain: one cycles-to-merge observation (max iteration),
    one PR count, one terminal outcome."""
    pid = uuid4()
    orig_id = str(uuid4())
    tasks = [
        _task(pid, review_loop={"iteration": i, "original_task_id": orig_id})
        for i in range(1, 6)  # iterations 1..5, all the same PR-chain
    ]
    # Terminal merge stamped on the last iteration task in this fixture.
    tasks[-1].context = {**tasks[-1].context, "pr_merged": True}
    res = compute_outer_loop_convergence(
        all_tasks=tasks,
        escalation_alerts=[],
        window_start=_utcnow() - timedelta(hours=24),
    )
    # One PR, not five.
    assert res["prs_with_review_loop"] == 1
    # cycles-to-merge = max(iteration) = 5, contributing a single 4+ observation
    # (NOT 1,2,3,4,5 across five tasks).
    assert res["iteration_distribution"] == {"1": 0, "2": 0, "3": 0, "4+": 1}
    assert res["iteration_median"] == 5
    assert res["iteration_p95"] == 5
    assert res["outcomes"]["merged"] == 1
    assert res["convergence_rate"] == 1.0


def test_terminal_outcome_read_from_chain_original_task():
    """pr_merged is stamped on the chain-ORIGINAL task (which carries no
    review_loop, so it is absent from loop_tasks). The merge must still be
    attributed to the chain via review_loop.original_task_id."""
    pid = uuid4()
    original = _task(pid, review_loop=None, ctx_extra={"pr_merged": True})
    orig_id = str(original.id)
    iter_tasks = [
        _task(pid, review_loop={"iteration": 1, "original_task_id": orig_id}),
        _task(pid, review_loop={"iteration": 2, "original_task_id": orig_id}),
    ]
    res = compute_outer_loop_convergence(
        all_tasks=[original, *iter_tasks],
        escalation_alerts=[],
        window_start=_utcnow() - timedelta(hours=24),
    )
    # Original has no review_loop -> not a chain on its own; the two iteration
    # tasks form ONE chain that inherits the original's merge.
    assert res["prs_with_review_loop"] == 1
    assert res["outcomes"]["merged"] == 1
    assert res["convergence_rate"] == 1.0


@pytest.mark.asyncio
async def test_outer_loop_endpoint_hydrates_pre_window_original(client, db_session):
    project = make_project()
    db_session.add(project)
    await db_session.flush()

    now = _utcnow()
    original = _task(
        project.id,
        review_loop=None,
        ctx_extra={"pr_merged": True},
        created_at=now - timedelta(hours=48),
    )
    orig_id = str(original.id)
    tasks = [
        _task(
            project.id,
            review_loop={"iteration": 1, "original_task_id": orig_id},
            created_at=now - timedelta(hours=1),
        ),
        _task(
            project.id,
            review_loop={"iteration": 2, "original_task_id": orig_id},
            created_at=now - timedelta(minutes=30),
        ),
    ]
    db_session.add(original)
    db_session.add_all(tasks)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/outer-loop-convergence?hours=24&project_id={project.id}"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["outcomes"]["merged"] == 1
    assert body["terminal_outcome_histogram"]["merged"] == 1


def test_chain_churn_is_max_not_sum():
    """churn_detections is a running total carried across iterations, so a chain
    contributes max(churn) once, not the sum of every iteration's value."""
    pid = uuid4()
    orig_id = str(uuid4())
    tasks = [
        _task(pid, review_loop={"iteration": 1, "original_task_id": orig_id, "churn_detections": 1}),
        _task(pid, review_loop={"iteration": 2, "original_task_id": orig_id, "churn_detections": 3}),
    ]
    res = compute_outer_loop_convergence(
        all_tasks=tasks,
        escalation_alerts=[],
        window_start=_utcnow() - timedelta(hours=24),
    )
    assert res["prs_with_review_loop"] == 1
    assert res["churn_pr_count"] == 1
    # max(1, 3) == 3, NOT 1 + 3 == 4
    assert res["churn_total"] == 3


def test_escalation_by_reason_dedupes_repeated_same_reason_alerts():
    """Multiple Alerts of the SAME reason on one task count once in by_reason, so
    sum(by_reason) does not over-count vs. the distinct-task escalation_total."""
    pid = uuid4()
    t = _task(pid, review_loop={"iteration": 5})
    alerts = [
        make_alert(pid, task_id=t.id, title="Review-fix escalation: stuck cycle on PR #7"),
        make_alert(pid, task_id=t.id, title="Review-fix escalation: stuck cycle on PR #7"),
    ]
    res = compute_outer_loop_convergence(
        all_tasks=[t],
        escalation_alerts=alerts,
        window_start=_utcnow() - timedelta(hours=24),
    )
    assert res["escalation_by_reason"]["stuck"] == 1
    assert res["escalation_total"] == 1


def test_dominant_blocker_named_when_one_cause_dominates():
    """When one candidate cause covers >=50% of non-converged chains (and >=2,
    strictly outranking runners-up), it is named with its quantified share."""
    pid = uuid4()
    tasks = []
    # 7 churn-only non-converged chains (no terminal flag -> outcome None).
    for _ in range(7):
        tasks.append(
            _task(
                pid,
                review_loop={"iteration": 2, "original_task_id": str(uuid4()), "churn_detections": 1},
            )
        )
    # 4 plain-rejected non-converged chains with no churn/drift signal.
    for _ in range(4):
        tasks.append(
            _task(
                pid,
                review_loop={"iteration": 1, "original_task_id": str(uuid4())},
                ctx_extra={"pr_rejected": True},
            )
        )
    res = compute_outer_loop_convergence(
        all_tasks=tasks,
        escalation_alerts=[],
        window_start=_utcnow() - timedelta(hours=24),
    )
    assert res["non_converged_total"] == 11
    blocker = res["dominant_blocker"]
    assert blocker is not None
    assert blocker["cause"] == "churn"
    assert blocker["count"] == 7
    assert blocker["total"] == 11
    assert blocker["share"] == 0.636


def test_dominant_blocker_none_when_no_clear_dominant():
    """An even split between two causes has no clear dominant -> None."""
    pid = uuid4()
    tasks = [
        # 2 churn-only non-converged chains.
        _task(pid, review_loop={"iteration": 2, "original_task_id": str(uuid4()), "churn_detections": 1}),
        _task(pid, review_loop={"iteration": 2, "original_task_id": str(uuid4()), "churn_detections": 1}),
        # 2 drift-only non-converged chains.
        _task(
            pid,
            review_loop={
                "iteration": 2,
                "original_task_id": str(uuid4()),
                "drift_signal": {"drift_in": [{"file": "a.py"}], "drift_out": []},
            },
        ),
        _task(
            pid,
            review_loop={
                "iteration": 2,
                "original_task_id": str(uuid4()),
                "drift_signal": {"drift_in": [{"file": "b.py"}], "drift_out": []},
            },
        ),
    ]
    res = compute_outer_loop_convergence(
        all_tasks=tasks,
        escalation_alerts=[],
        window_start=_utcnow() - timedelta(hours=24),
    )
    assert res["non_converged_total"] == 4
    # churn=2 and drift=2 tie -> neither strictly dominates.
    assert res["dominant_blocker"] is None


def test_dominant_blocker_none_below_sample_floor():
    """A single non-converged chain is below the count>=2 floor -> None."""
    pid = uuid4()
    tasks = [
        _task(pid, review_loop={"iteration": 1, "original_task_id": str(uuid4())}, ctx_extra={"pr_merged": True}),
        _task(
            pid,
            review_loop={"iteration": 2, "original_task_id": str(uuid4()), "churn_detections": 1},
        ),
    ]
    res = compute_outer_loop_convergence(
        all_tasks=tasks,
        escalation_alerts=[],
        window_start=_utcnow() - timedelta(hours=24),
    )
    assert res["non_converged_total"] == 1
    assert res["dominant_blocker"] is None


def test_tasks_without_review_loop_ignored():
    pid = uuid4()
    tasks = [
        _task(pid, review_loop=None, ctx_extra={"pr_merged": True}),  # no review_loop
        _task(pid, review_loop={"iteration": 2}, ctx_extra={"pr_merged": True}),
    ]
    res = compute_outer_loop_convergence(
        all_tasks=tasks,
        escalation_alerts=[],
        window_start=_utcnow() - timedelta(hours=24),
    )
    assert res["prs_with_review_loop"] == 1


# ---------------------------------------------------------------------------
# dominant_blocker — edge cases that must always resolve to None
# (the gated cause-ranking happy paths live above; these guard the boundaries)
# ---------------------------------------------------------------------------


def test_dominant_blocker_none_on_empty_window():
    res = compute_outer_loop_convergence(
        all_tasks=[],
        escalation_alerts=[],
        window_start=_utcnow() - timedelta(hours=24),
    )
    assert res["dominant_blocker"] is None


def test_dominant_blocker_none_when_all_merged():
    pid = uuid4()
    tasks = [
        _task(pid, review_loop={"iteration": 1}, ctx_extra={"pr_merged": True}),
        _task(pid, review_loop={"iteration": 2}, ctx_extra={"pr_merged": True}),
    ]
    res = compute_outer_loop_convergence(
        all_tasks=tasks,
        escalation_alerts=[],
        window_start=_utcnow() - timedelta(hours=24),
    )
    assert res["dominant_blocker"] is None


def test_dominant_blocker_escalated_then_merged_not_attributed():
    """CRITICAL invariant: a chain that escalated (stuck alert) and then merged
    resolves to ``merged`` and must NEVER count toward a blocker. With only that
    one chain in the window, dominant_blocker is None and non_converged_total is 0."""
    pid = uuid4()
    orig_id = str(uuid4())
    chain = [
        _task(pid, review_loop={"iteration": 1, "original_task_id": orig_id}),
        _task(
            pid,
            review_loop={"iteration": 2, "original_task_id": orig_id},
            ctx_extra={"pr_merged": True},
        ),
    ]
    alerts = [
        make_alert(
            pid,
            task_id=chain[0].id,
            title="Review-fix escalation: stuck cycle on PR #5",
        ),
    ]
    res = compute_outer_loop_convergence(
        all_tasks=chain,
        escalation_alerts=alerts,
        window_start=_utcnow() - timedelta(hours=24),
    )
    # The chain merged despite the earlier escalation.
    assert res["outcomes"]["merged"] == 1
    assert res["dominant_blocker"] is None

"""Unit and integration tests for review_pipeline.py.

Covers:
  1. _max_review_iterations() — scope/fix_reason caps (pure unit, no DB).
  2. _spawn_pipeline_task() — global iteration cap (DB-backed).
  3. _spawn_pipeline_task() — per-chain iteration cap (DB-backed).
  4. _spawn_pipeline_task() — stuck-cycle detection (DB-backed).
  5. Edge cases: empty review_history, issues without severity,
     zero issues in current iteration.
"""

import os
import uuid
from datetime import timedelta

import pytest
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.models import Project, Task, TaskStatus
from app.review_pipeline import (
    MAX_REVIEW_FIX_ITERATIONS,
    PIPELINE_TASK_STALE_MINUTES,
    SPAWN_REASON_BLOCKED_REPEAT,
    SPAWN_REASON_KEY,
    STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_DEFAULT,
    STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_MEDIUM,
    STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_REFACTOR,
    STUCK_CYCLE_TEXT_THRESHOLD_DEFAULT,
    STUCK_CYCLE_TEXT_THRESHOLD_MEDIUM,
    STUCK_CYCLE_TEXT_THRESHOLD_REFACTOR,
    STUCK_CYCLE_THRESHOLD_DEFAULT,
    STUCK_CYCLE_THRESHOLD_MEDIUM,
    STUCK_CYCLE_THRESHOLD_REFACTOR,
    _compute_scope_budget_utilization,
    _detect_issue_churn,
    _failure_fingerprint_msg,
    _repeating_issue_descriptions,
    _format_repeating_issues_section,
    compute_drift_signal,
    _feedback_jaccard,
    _feedback_token_set,
    _is_cross_cutting_pr,
    _is_pipeline_task_stale,
    _is_test_file,
    _max_review_iterations,
    _spawn_pipeline_task,
    maybe_spawn_cross_cutting_safeguard,
    post_merge_summary_comment,
    should_auto_approve_diff,
)
from testing.factories import make_project, make_task


# ── _max_review_iterations ────────────────────────────────────────────────────


class TestMaxReviewIterations:
    """Unit tests for the scope/fix_reason iteration cap function.

    No database required — all assertions are against the return value of
    _max_review_iterations() called with Task instances built from make_task().
    """

    def _task(self, scope=None, fix_reason=None) -> Task:
        ctx: dict = {}
        if scope is not None:
            ctx["scope"] = scope
        review_loop: dict = {}
        if fix_reason is not None:
            review_loop["fix_reason"] = fix_reason
        if review_loop:
            ctx["review_loop"] = review_loop
        return make_task(context=ctx)

    # ── Scope-based caps ──────────────────────────────────────────────────────

    def test_small_scope_returns_10(self):
        assert _max_review_iterations(self._task(scope="small")) == 10

    def test_medium_scope_returns_12(self):
        assert _max_review_iterations(self._task(scope="medium")) == 12

    def test_large_scope_returns_16(self):
        assert _max_review_iterations(self._task(scope="large")) == 16

    def test_unknown_scope_returns_16(self):
        """An unrecognised scope falls through to the else branch → 16."""
        assert _max_review_iterations(self._task(scope="xlarge")) == 16

    def test_absent_scope_defaults_to_medium(self):
        """No scope key in context → ctx.get('scope', 'medium') == 'medium' → 12."""
        t = make_task(context={})
        assert _max_review_iterations(t) == 12

    def test_none_context_defaults_to_medium(self):
        """task.context = None is handled safely via 'or {}' guard → 12."""
        t = make_task(context=None)
        assert _max_review_iterations(t) == 12

    # ── fix_reason='ci_failure' overrides ────────────────────────────────────

    def test_ci_failure_returns_8(self):
        assert _max_review_iterations(self._task(fix_reason="ci_failure")) == 8

    def test_ci_failure_overrides_small_scope(self):
        """ci_failure cap (8) takes priority over the small-scope cap (10)."""
        assert (
            _max_review_iterations(self._task(scope="small", fix_reason="ci_failure"))
            == 8
        )

    def test_ci_failure_overrides_medium_scope(self):
        assert (
            _max_review_iterations(self._task(scope="medium", fix_reason="ci_failure"))
            == 8
        )

    def test_ci_failure_overrides_large_scope(self):
        assert (
            _max_review_iterations(self._task(scope="large", fix_reason="ci_failure"))
            == 8
        )

    def test_other_fix_reason_does_not_override(self):
        """fix_reason other than 'ci_failure' is ignored; scope applies normally."""
        assert (
            _max_review_iterations(self._task(scope="small", fix_reason="code_review"))
            == 10
        )

    # ── Safety ceiling ────────────────────────────────────────────────────────

    def test_result_never_exceeds_max_review_fix_iterations(self):
        """min(limit, MAX_REVIEW_FIX_ITERATIONS) always applies."""
        for scope in ("small", "medium", "large", "unknown", None):
            limit = _max_review_iterations(self._task(scope=scope))
            assert limit <= MAX_REVIEW_FIX_ITERATIONS, (
                f"scope={scope!r} produced limit={limit} > MAX={MAX_REVIEW_FIX_ITERATIONS}"
            )

    def test_constants_match_expected_values(self):
        """Tripwire: cap constants must not change without a deliberate review."""
        assert MAX_REVIEW_FIX_ITERATIONS == 16
        assert STUCK_CYCLE_THRESHOLD_DEFAULT == 0.55
        assert STUCK_CYCLE_THRESHOLD_REFACTOR == 0.65

    # ── Direction-aware reduction ─────────────────────────────────────────────

    def _direction_data(self, history: list[dict], agg: dict | None = None) -> dict:
        return {"research_history": history, "history_aggregate": agg or {}}

    def test_low_acceptance_rate_reduces_small_cap(self):
        """acceptance_rate < 0.3 reduces cap by ~30% (10 → 7)."""
        # 1 merged, 9 rejected → acceptance_rate = 0.1 < 0.3
        history = [{"prs_merged": 1, "prs_rejected": 9}]
        d = self._direction_data(history)
        assert _max_review_iterations(self._task(scope="small"), direction_data=d) == 7

    def test_low_acceptance_rate_reduces_medium_cap(self):
        """acceptance_rate < 0.3 reduces cap by ~30% (12 → 8)."""
        history = [{"prs_merged": 1, "prs_rejected": 9}]
        d = self._direction_data(history)
        assert _max_review_iterations(self._task(scope="medium"), direction_data=d) == 8

    def test_low_acceptance_rate_reduces_large_cap(self):
        """acceptance_rate < 0.3 reduces cap by ~30% (16 → 11)."""
        history = [{"prs_merged": 1, "prs_rejected": 9}]
        d = self._direction_data(history)
        assert _max_review_iterations(self._task(scope="large"), direction_data=d) == 11

    def test_high_rejection_ratio_reduces_cap(self):
        """prs_rejected / prs_merged > 2.0 in recent history reduces cap."""
        # 1 merged, 3 rejected → ratio = 3.0 > 2.0
        history = [{"prs_merged": 1, "prs_rejected": 3}]
        d = self._direction_data(history)
        assert _max_review_iterations(self._task(scope="small"), direction_data=d) == 7

    def test_high_rejection_no_merges_reduces_cap(self):
        """0 merged but ≥3 rejected also triggers the reduction."""
        history = [{"prs_merged": 0, "prs_rejected": 3}]
        d = self._direction_data(history)
        assert _max_review_iterations(self._task(scope="small"), direction_data=d) == 7

    def test_good_acceptance_rate_no_reduction(self):
        """Healthy direction (acceptance_rate ≥ 0.3, low rejection) keeps full cap."""
        history = [{"prs_merged": 5, "prs_rejected": 1}]
        d = self._direction_data(history)
        assert _max_review_iterations(self._task(scope="small"), direction_data=d) == 10

    def test_no_direction_data_uses_scope_cap(self):
        """direction_data=None falls back to scope-based caps unchanged."""
        assert (
            _max_review_iterations(self._task(scope="small"), direction_data=None) == 10
        )

    def test_ci_failure_not_affected_by_direction_data(self):
        """ci_failure cap (8) is not reduced even for a bad direction."""
        history = [{"prs_merged": 0, "prs_rejected": 9}]
        d = self._direction_data(history)
        assert (
            _max_review_iterations(
                self._task(scope="small", fix_reason="ci_failure"), direction_data=d
            )
            == 8
        )

    def test_no_history_data_no_reduction(self):
        """Empty history means acceptance_rate=None → no reduction."""
        d = self._direction_data([])
        assert _max_review_iterations(self._task(scope="small"), direction_data=d) == 10

    # ── Retry carry-forward: _prior_review_iterations_consumed ──────────────

    def test_prior_iterations_reduce_cap(self):
        """Retry clones carry forward prior iteration count; half is deducted."""
        t = make_task(
            context={"scope": "medium", "_prior_review_iterations_consumed": 8}
        )
        # medium cap 12, minus 8//2 = 4 → 8
        assert _max_review_iterations(t) == 8

    def test_prior_iterations_floor_at_three(self):
        """Heavy prior usage cannot drive the cap below 3."""
        t = make_task(
            context={"scope": "small", "_prior_review_iterations_consumed": 100}
        )
        assert _max_review_iterations(t) == 3

    def test_prior_iterations_zero_no_reduction(self):
        t = make_task(
            context={"scope": "medium", "_prior_review_iterations_consumed": 0}
        )
        assert _max_review_iterations(t) == 12


# ── DB-backed helpers ─────────────────────────────────────────────────────────


async def _make_db_project(db_session) -> Project:
    """Persist a project and return the refreshed instance."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _persist(db_session, *tasks) -> None:
    """Add and commit a batch of tasks, refreshing each."""
    for t in tasks:
        db_session.add(t)
    await db_session.commit()
    for t in tasks:
        await db_session.refresh(t)


# ── _requeue_parent_task: carry-forward review iterations ────────────────────


@pytest.mark.asyncio
async def test_requeue_carries_forward_prior_review_iterations(db_session, mocker):
    """Cloning a failed child captures its review_loop.iteration into
    _prior_review_iterations_consumed, and the clone's cap reflects the deduction."""
    from app.task_lifecycle import _requeue_parent_task

    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    parent = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        context={"plan_branch": "plan/test"},
    )
    await _persist(db_session, parent)
    child = make_task(
        project_id=project.id,
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
        context={"scope": "medium", "review_loop": {"iteration": 8}},
        payloads=[{"type": "create", "prompt": "do a thing", "base_branch": "main"}],
        latest_status_message="some failure",
    )
    await _persist(db_session, child)

    await _requeue_parent_task(parent, db_session)

    clone = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id,
                    Task.status == TaskStatus.QUEUED,
                )
            )
        )
        .scalars()
        .first()
    )
    assert clone is not None
    assert (clone.context or {}).get("_prior_review_iterations_consumed") == 8
    # medium cap 12, minus 8//2 = 4 → 8
    assert _max_review_iterations(clone) == 8


# ── should_auto_approve_diff ────────────────────────────────────────────


class TestShouldAutoApproveSmallDiff:
    def _task(self, *, type_="create", scope="small", files=None, diff=None):
        payload = {"type": type_}
        ctx: dict = {"scope": scope}
        if files is not None:
            ctx["_pr_changed_files"] = files
        if diff is not None:
            ctx["_pr_diff_summary"] = diff
        return make_task(payloads=[payload], context=ctx)

    def test_happy_path(self):
        t = self._task(
            files=["app/foo.py"],
            diff=[{"file": "app/foo.py", "added": 10, "removed": 2}],
        )
        skip, reason = should_auto_approve_diff(t, None)
        assert skip is True
        assert "auto-approve" in reason

    def test_medium_scope_within_limits_approved(self):
        t = self._task(
            scope="medium",
            files=["app/foo.py", "app/bar.py"],
            diff=[
                {"file": "app/foo.py", "added": 10, "removed": 0},
                {"file": "app/bar.py", "added": 10, "removed": 0},
            ],
        )
        skip, _ = should_auto_approve_diff(t, None)
        assert skip is True

    def test_medium_scope_over_lines_rejected(self):
        t = self._task(
            scope="medium",
            files=["app/foo.py"],
            diff=[{"file": "app/foo.py", "added": 80, "removed": 0}],
        )
        skip, reason = should_auto_approve_diff(t, None)
        assert skip is False
        assert "lines" in reason

    def test_medium_scope_over_files_rejected(self):
        t = self._task(
            scope="medium",
            files=["app/a.py", "app/b.py", "app/c.py"],
            diff=[
                {"file": "app/a.py", "added": 1, "removed": 0},
                {"file": "app/b.py", "added": 1, "removed": 0},
                {"file": "app/c.py", "added": 1, "removed": 0},
            ],
        )
        skip, _ = should_auto_approve_diff(t, None)
        assert skip is False

    def test_medium_scope_disabled_via_zero(self, mocker):
        from app.shared_state import runtime_config

        mocker.patch.dict(runtime_config, {"auto_approve_medium_max_lines": 0})
        t = self._task(
            scope="medium",
            files=["app/foo.py"],
            diff=[{"file": "app/foo.py", "added": 5, "removed": 0}],
        )
        skip, reason = should_auto_approve_diff(t, None)
        assert skip is False
        assert "disabled" in reason

    def test_large_scope_rejected(self):
        t = self._task(
            scope="large",
            files=["app/foo.py"],
            diff=[{"file": "app/foo.py", "added": 5, "removed": 0}],
        )
        skip, _ = should_auto_approve_diff(t, None)
        assert skip is False

    def test_non_eligible_type_rejected(self):
        t = self._task(
            type_="plan",
            files=["app/foo.py"],
            diff=[{"file": "app/foo.py", "added": 5, "removed": 0}],
        )
        skip, _ = should_auto_approve_diff(t, None)
        assert skip is False

    def test_fix_type_eligible(self):
        t = self._task(
            type_="fix",
            files=["app/foo.py"],
            diff=[{"file": "app/foo.py", "added": 5, "removed": 0}],
        )
        skip, _ = should_auto_approve_diff(t, None)
        assert skip is True

    def test_migrate_type_small_diff_eligible(self):
        t = self._task(
            type_="migrate",
            files=["backend/alembic/versions/0039_x.py", "backend/app/main.py"],
            diff=[
                {
                    "file": "backend/alembic/versions/0039_x.py",
                    "added": 20,
                    "removed": 0,
                },
                {"file": "backend/app/main.py", "added": 1, "removed": 1},
            ],
        )
        skip, reason = should_auto_approve_diff(t, None)
        assert skip is True
        assert "auto-approve" in reason

    def test_migrate_type_large_diff_rejected(self):
        t = self._task(
            type_="migrate",
            files=["backend/alembic/versions/0039_x.py"],
            diff=[
                {
                    "file": "backend/alembic/versions/0039_x.py",
                    "added": 200,
                    "removed": 0,
                }
            ],
        )
        skip, reason = should_auto_approve_diff(t, None)
        assert skip is False
        assert "lines" in reason

    def test_too_many_lines_rejected(self):
        t = self._task(
            files=["app/foo.py"],
            diff=[{"file": "app/foo.py", "added": 100, "removed": 0}],
        )
        skip, reason = should_auto_approve_diff(t, None)
        assert skip is False
        assert "lines" in reason

    def test_too_many_non_test_files_rejected(self):
        t = self._task(
            files=["app/a.py", "app/b.py", "app/c.py", "app/d.py", "app/e.py"],
            diff=[
                {"file": "app/a.py", "added": 1, "removed": 0},
                {"file": "app/b.py", "added": 1, "removed": 0},
                {"file": "app/c.py", "added": 1, "removed": 0},
                {"file": "app/d.py", "added": 1, "removed": 0},
                {"file": "app/e.py", "added": 1, "removed": 0},
            ],
        )
        skip, _ = should_auto_approve_diff(t, None)
        assert skip is False

    def test_test_files_dont_count_toward_file_budget(self):
        t = self._task(
            files=["app/a.py", "testing/test_a.py", "testing/test_b.py"],
            diff=[
                {"file": "app/a.py", "added": 5, "removed": 0},
                {"file": "testing/test_a.py", "added": 3, "removed": 0},
                {"file": "testing/test_b.py", "added": 3, "removed": 0},
            ],
        )
        skip, _ = should_auto_approve_diff(t, None)
        assert skip is True

    def test_small_prod_change_with_large_test_file_auto_approves(self):
        """Small prod change (20 lines) + large test file (100 lines) should
        auto-approve because only non-test lines are counted in the budget."""
        t = self._task(
            files=["app/foo.py", "backend/testing/test_foo.py"],
            diff=[
                {"file": "app/foo.py", "added": 15, "removed": 5},
                {"file": "backend/testing/test_foo.py", "added": 80, "removed": 20},
            ],
        )
        skip, reason = should_auto_approve_diff(t, None)
        assert skip is True
        assert "auto-approve" in reason
        assert "non-test lines" in reason

    def test_large_prod_change_denied_regardless_of_test_lines(self):
        """Large prod change (100 lines) should be denied even if test file
        lines are excluded from the budget."""
        t = self._task(
            files=["app/foo.py", "backend/testing/test_foo.py"],
            diff=[
                {"file": "app/foo.py", "added": 60, "removed": 40},
                {"file": "backend/testing/test_foo.py", "added": 100, "removed": 0},
            ],
        )
        skip, reason = should_auto_approve_diff(t, None)
        assert skip is False
        assert "non-test lines" in reason

    def test_test_only_change_auto_approves(self):
        """A change that only touches test files should auto-approve."""
        t = self._task(
            files=["backend/testing/test_foo.py", "testing/test_bar.py"],
            diff=[
                {"file": "backend/testing/test_foo.py", "added": 40, "removed": 0},
                {"file": "testing/test_bar.py", "added": 30, "removed": 0},
            ],
        )
        skip, reason = should_auto_approve_diff(t, None)
        assert skip is True
        assert "auto-approve" in reason
        assert "0 non-test lines" in reason

    def test_scope_overflow_warning_disables_auto_approve(self):
        t = self._task(
            scope="small",
            files=[
                "app/a.py",
                "app/b.py",
                "app/c.py",
                "app/d.py",
                "app/e.py",
                "app/f.py",
                "app/g.py",
            ],
            diff=[
                {"file": "app/a.py", "added": 1, "removed": 0},
                {"file": "app/b.py", "added": 1, "removed": 0},
            ],
        )
        skip, reason = should_auto_approve_diff(t, None)
        assert skip is False
        assert "scope overflow warning" in reason

    def test_missing_diff_summary_rejected(self):
        t = self._task(files=["app/a.py"])
        skip, reason = should_auto_approve_diff(t, None)
        assert skip is False
        assert "diff size" in reason

    def test_low_acceptance_rate_direction_rejected(self, mocker):
        mocker.patch(
            "app.research_completion._compute_acceptance_rate",
            return_value=0.2,
        )
        t = self._task(
            files=["app/a.py"],
            diff=[{"file": "app/a.py", "added": 5, "removed": 0}],
        )
        skip, reason = should_auto_approve_diff(t, {"research_history": []})
        assert skip is False
        assert "acceptance_rate" in reason

    def test_disabled_via_zero_max_lines(self, mocker):
        from app.shared_state import runtime_config

        mocker.patch.dict(runtime_config, {"auto_approve_max_lines": 0})
        t = self._task(
            files=["app/a.py"],
            diff=[{"file": "app/a.py", "added": 1, "removed": 0}],
        )
        skip, reason = should_auto_approve_diff(t, None)
        assert skip is False
        assert "disabled" in reason


# ── _spawn_pipeline_task: iteration cap ───────────────────────────────────────


@pytest.mark.asyncio
async def test_global_cap_blocks_spawn(db_session, mocker):
    """When the global completed-task count for a PR reaches the cap,
    _spawn_pipeline_task returns None without creating a new task, transitions
    the root task to FAILED with failure_detail=review_cap_exhausted, closes
    the PR, keeps requires_approval=True, and fires a failure alert."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    alert_mock = mocker.patch(
        "app.main._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    close_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    # Original 'create' task that opened the PR — the chain's root.
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "small"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Source task: scope='small' → global_cap=10.
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 101,
                "branch": "feat/x",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "review_loop": {"iteration": 0, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)

    # 10 completed review tasks for PR #101 — reaches the global cap.
    for _ in range(10):
        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[
                {
                    "type": "review",
                    "pr_number": 101,
                    "branch": "feat/x",
                    "base_branch": "main",
                }
            ],
        )
        db_session.add(t)

    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=101,
        branch="feat/x",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    assert result is None, "Global cap (10 iterations, small scope) should block spawn"

    await db_session.refresh(original_task)
    assert original_task.status == TaskStatus.FAILED, (
        f"Global cap must transition the root task to FAILED, got {original_task.status}"
    )
    assert (original_task.context or {}).get(
        "failure_detail"
    ) == "review_cap_exhausted", (
        "Global cap must tag failure_detail=review_cap_exhausted so the rollup respawn "
        "loop can skip this child and the parent can finalize."
    )
    assert (original_task.context or {}).get("pr_rejected") is True, (
        "Global cap must preempt the webhook by setting pr_rejected=True before closing the PR"
    )
    assert original_task.requires_approval is True, (
        "Global cap preserves requires_approval=True as the UI signal for manual intervention"
    )
    assert "manual review" in (original_task.latest_status_message or "").lower(), (
        f"Status message should mention manual review: {original_task.latest_status_message!r}"
    )
    close_mock.assert_awaited_once()
    assert close_mock.call_args.args[3] == 101, (
        f"close_pull_request must be called with pr_number=101, got {close_mock.call_args.args}"
    )
    alert_mock.assert_called_once()
    call_args = alert_mock.call_args[0]
    assert "cap" in call_args[2].lower() or "manual" in call_args[2].lower(), (
        f"Alert should mention cap or manual intervention, got: {call_args[2]!r}"
    )


@pytest.mark.asyncio
async def test_cap_paths_symmetric_terminal_state(db_session, mocker):
    """Both the global-cap and per-chain-cap paths must produce a symmetric
    terminal state: FAILED status with failure_detail=review_cap_exhausted,
    requires_approval=True, and a 'manual review required' status message.
    Prevents silent-block conditions where one cap path fails the task but
    the other leaves it COMPLETED (the bug that produced the PR #1947 respawn
    loop, fixed 2026-04-17)."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    async def _run_cap(pr_number: int, iteration: int, completed_count: int) -> Task:
        project = await _make_db_project(db_session)
        original_task = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
            context={"scope": "small"},
        )
        db_session.add(original_task)
        await db_session.commit()
        await db_session.refresh(original_task)

        source_task = make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            payloads=[
                {
                    "type": "review",
                    "pr_number": pr_number,
                    "branch": "feat/sym",
                    "base_branch": "main",
                    "original_task_id": str(original_task.id),
                }
            ],
            context={
                "scope": "small",
                "review_loop": {
                    "iteration": iteration,
                    "original_task_id": str(original_task.id),
                },
            },
        )
        db_session.add(source_task)
        for _ in range(completed_count):
            db_session.add(
                make_task(
                    project_id=project.id,
                    status=TaskStatus.COMPLETED,
                    payloads=[
                        {
                            "type": "review",
                            "pr_number": pr_number,
                            "branch": "feat/sym",
                            "base_branch": "main",
                        }
                    ],
                )
            )
        await db_session.commit()
        await db_session.refresh(source_task)

        result, _ = await _spawn_pipeline_task(
            source_task,
            "fix",
            db_session,
            pr_number=pr_number,
            branch="feat/sym",
            base_branch="main",
            original_task_id=str(original_task.id),
            fix_prompt="Still broken",
        )
        assert result is None
        await db_session.refresh(original_task)
        return original_task

    global_root = await _run_cap(pr_number=901, iteration=0, completed_count=10)
    per_chain_root = await _run_cap(pr_number=902, iteration=10, completed_count=0)

    for root, label in ((global_root, "global"), (per_chain_root, "per-chain")):
        assert root.status == TaskStatus.FAILED, (
            f"{label} cap must transition to FAILED, got {root.status}"
        )
        assert (root.context or {}).get("failure_detail") == "review_cap_exhausted", (
            f"{label} cap must tag failure_detail=review_cap_exhausted"
        )
        assert root.requires_approval is True, (
            f"{label} cap must preserve requires_approval"
        )
        msg = (root.latest_status_message or "").lower()
        assert "manual review" in msg, (
            f"{label} cap message missing 'manual review': {msg!r}"
        )
        assert "review-fix cycle reached limit" in msg, (
            f"{label} cap message should use symmetric 'Review-fix cycle reached limit' phrasing: {msg!r}"
        )


@pytest.mark.asyncio
async def test_infra_failed_tasks_dont_count_toward_global_cap(db_session, mocker):
    """FAILED tasks with infra failure_category (PROVISION_FAILURE, RATE_LIMIT,
    AGENT_TIMEOUT, EXTERNAL_API) never ran a real review, so they must not
    consume iteration budget."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 103,
                "branch": "feat/x",
                "base_branch": "main",
            }
        ],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)

    # 20 FAILED review tasks, all infra-class — cap is 10 for small scope,
    # but none of these should count.
    for fc in ["PROVISION_FAILURE", "RATE_LIMIT", "AGENT_TIMEOUT", "EXTERNAL_API"] * 5:
        t = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            payloads=[
                {
                    "type": "review",
                    "pr_number": 103,
                    "branch": "feat/x",
                    "base_branch": "main",
                }
            ],
            context={"failure_category": fc},
        )
        db_session.add(t)

    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=103,
        branch="feat/x",
        base_branch="main",
        fix_prompt="Fix",
    )
    assert result is not None, "Infra-class FAILED tasks must not count toward the cap"
    assert result.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_supersede_cancelled_tasks_dont_count_toward_global_cap(
    db_session, mocker
):
    """CANCELLED tasks with infra failure_category (set by supersede paths in
    review_pipeline.py:1170 and :1204) never ran a real review, so they must
    not consume iteration budget. Regression guard for incident 2026-05-07
    task 78de0fdb: ~13 of 25 tasks on PR #4091's chain were supersede-cancels
    (snapshot-stale, no-heartbeat-15min) that each burned a cap slot, hitting
    the global cap on churn alone with zero useful review-fix progress."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 105,
                "branch": "feat/x",
                "base_branch": "main",
            }
        ],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)

    # 20 CANCELLED review/fix tasks with AGENT_TIMEOUT (the supersede stamp).
    # Cap is 10 for small scope — would block spawn if counted.
    for i in range(20):
        ttype = "fix" if i % 2 else "review"
        t = make_task(
            project_id=project.id,
            status=TaskStatus.CANCELLED,
            payloads=[
                {
                    "type": ttype,
                    "pr_number": 105,
                    "branch": "feat/x",
                    "base_branch": "main",
                }
            ],
            context={"failure_category": "AGENT_TIMEOUT"},
        )
        db_session.add(t)

    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=105,
        branch="feat/x",
        base_branch="main",
        fix_prompt="Fix",
    )
    assert result is not None, "Supersede-cancelled tasks must not count toward the cap"
    assert result.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_archived_tasks_dont_count_toward_global_cap(db_session, mocker):
    """Archived tasks must not count — this is how reset-iteration-cap works."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 104,
                "branch": "feat/x",
                "base_branch": "main",
            }
        ],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)

    # 15 archived COMPLETED tasks — would exceed cap if counted.
    for _ in range(15):
        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[
                {
                    "type": "review",
                    "pr_number": 104,
                    "branch": "feat/x",
                    "base_branch": "main",
                }
            ],
            archived=True,
        )
        db_session.add(t)

    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=104,
        branch="feat/x",
        base_branch="main",
        fix_prompt="Fix",
    )
    assert result is not None, "Archived tasks must not count toward the cap"


@pytest.mark.asyncio
async def test_code_error_failed_tasks_do_count_toward_global_cap(db_session, mocker):
    """CODE_ERROR failures ARE real review iterations and must count — otherwise
    a broken PR could loop forever."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 105,
                "branch": "feat/x",
                "base_branch": "main",
            }
        ],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)

    # 10 CODE_ERROR-failed tasks — reaches small-scope cap of 10.
    for _ in range(10):
        t = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            payloads=[
                {
                    "type": "review",
                    "pr_number": 105,
                    "branch": "feat/x",
                    "base_branch": "main",
                }
            ],
            context={"failure_category": "CODE_ERROR"},
        )
        db_session.add(t)

    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=105,
        branch="feat/x",
        base_branch="main",
    )
    assert result is None, "CODE_ERROR FAILED tasks must count toward the cap"


@pytest.mark.asyncio
async def test_global_cap_under_limit_allows_spawn(db_session, mocker):
    """With fewer completed tasks than the cap, _spawn_pipeline_task creates a task."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 102,
                "branch": "feat/y",
                "base_branch": "main",
            }
        ],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)

    # Only 5 completed tasks — below the small-scope cap of 10.
    for _ in range(5):
        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[
                {
                    "type": "review",
                    "pr_number": 102,
                    "branch": "feat/y",
                    "base_branch": "main",
                }
            ],
        )
        db_session.add(t)

    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=102,
        branch="feat/y",
        base_branch="main",
        fix_prompt="Fix the lint error",
    )

    assert result is not None, "Under the cap, a new fix task should be spawned"
    assert result.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_per_chain_cap_blocks_spawn_and_marks_original(db_session, mocker):
    """When the per-chain iteration counter exceeds the cap, _spawn_pipeline_task
    returns None, transitions the original task to FAILED with
    failure_detail=review_cap_exhausted, closes the PR, and fires a failure alert."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    alert_mock = mocker.patch(
        "app.main._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    close_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    # Original 'create' task that started the pipeline.
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "small"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Source review task at iteration=10 (scope='small' → cap=10).
    # Next iteration would be 11 > 10 → per-chain cap fires.
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 103,
                "branch": "feat/z",
                "base_branch": "main",
            }
        ],
        context={
            "scope": "small",
            "review_loop": {
                "iteration": 10,
                "original_task_id": str(original_task.id),
                "pr_number": 103,
                "branch": "feat/z",
                "base_branch": "main",
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=103,
        branch="feat/z",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Still broken",
    )

    assert result is None, "Per-chain cap should block the fix spawn"

    await db_session.refresh(original_task)
    assert original_task.status == TaskStatus.FAILED, (
        f"Per-chain cap must transition the original task to FAILED, got {original_task.status}"
    )
    assert (original_task.context or {}).get(
        "failure_detail"
    ) == "review_cap_exhausted", (
        "Per-chain cap must tag failure_detail=review_cap_exhausted"
    )
    assert original_task.requires_approval is True, (
        "Per-chain cap preserves requires_approval=True"
    )
    assert "manual review" in (original_task.latest_status_message or "").lower(), (
        f"Status message should mention manual review: {original_task.latest_status_message!r}"
    )
    close_mock.assert_awaited_once()
    assert close_mock.call_args.args[3] == 103, (
        f"close_pull_request must be called with pr_number=103, got {close_mock.call_args.args}"
    )
    alert_mock.assert_called_once()
    call_args = alert_mock.call_args[0]
    assert "cap" in call_args[2].lower() or "manual" in call_args[2].lower(), (
        f"Alert should mention cap or manual review, got: {call_args[2]!r}"
    )


@pytest.mark.asyncio
async def test_cap_exhaustion_records_task_and_direction_result(db_session, mocker):
    """Review-fix cap exhaustion must surface to deploy_monitoring.task_results
    and to per-direction failure streaks. Without this, cap failures bypass
    per-type failure gates and anomaly detection — regression guard for the
    cluster 2/5 fix of the missing-record_task_result sweep."""
    from collections import deque
    import app.shared_state as ss
    from app.routers import agent_status as _agent_status_mod

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    # Reset module-level state so the assertions are deterministic.
    ss.deploy_monitoring["task_results"] = deque(maxlen=100)
    ss.direction_failure_streaks.clear()
    _agent_status_mod._recorded_task_ids.clear()

    project = await _make_db_project(db_session)
    direction_id = "dir-cap-rec"

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "small", "direction_id": direction_id},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 777,
                "branch": "feat/rec",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "direction_id": direction_id,
            "review_loop": {"iteration": 10, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=777,
        branch="feat/rec",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Still broken",
    )

    assert result is None, "Per-chain cap should block spawn"

    results = list(ss.deploy_monitoring["task_results"])
    assert len(results) == 1, (
        f"Cap exhaustion must append exactly one deploy_monitoring entry, got {results!r}"
    )
    _, outcome, fclass, ttype = results[0]
    assert outcome == "failure", f"Expected failure outcome, got {outcome!r}"
    assert fclass == "CODE_ERROR", f"Expected CODE_ERROR failure_class, got {fclass!r}"
    assert ttype == "create", f"Expected task_type=create, got {ttype!r}"

    streak = ss.direction_failure_streaks.get(direction_id)
    assert streak is not None, "Cap exhaustion must register a direction failure streak"
    assert streak["consecutive_infra_failures"] == 0
    assert streak["consecutive_failures"] == 1
    assert streak["last_failure_class"] == "CODE_ERROR"


@pytest.mark.asyncio
async def test_cap_exhaustion_records_remediation_failure_on_direction(
    db_session, mocker
):
    """Cap exhaustion on a remediation create task must stamp the linked direction
    with a failed outcome. Without this call, a premature 'merged' recorded on PR
    creation would persist even though the PR was auto-closed."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    direction_id = "dir-cap-remed"
    project = await _make_db_project(db_session)
    project.directions = [{"id": direction_id, "status": "active"}]
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Remediate", "base_branch": "main"}],
        context={
            "scope": "small",
            "direction_id": direction_id,
            "diagnosis_task_id": "diag-cap",
        },
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 888,
                "branch": "feat/rec",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "direction_id": direction_id,
            "diagnosis_task_id": "diag-cap",
            "review_loop": {"iteration": 10, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=888,
        branch="feat/rec",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Still broken",
    )
    assert result is None

    await db_session.refresh(project)
    direction = next(d for d in project.directions if d["id"] == direction_id)
    assert direction.get("remediation_attempts") == 1, (
        f"Cap exhaustion must record a remediation attempt on the direction, got {direction!r}"
    )
    assert direction.get("last_remediation_result") == "failed"


@pytest.mark.asyncio
async def test_cap_exhaustion_idempotent(db_session, mocker):
    """If rollup or another handler re-enters _spawn_pipeline_task for an
    already-cap-exhausted PR, the side effects (PR close, alert) must fire
    exactly once. Before the 2026-04-17 fix this path looped every 10min,
    re-writing the same status message and never converging."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    alert_mock = mocker.patch(
        "app.main._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    close_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    comment_mock = mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "small"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    async def _spawn_once():
        source_task = make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            payloads=[
                {
                    "type": "review",
                    "pr_number": 555,
                    "branch": "feat/idem",
                    "base_branch": "main",
                    "original_task_id": str(original_task.id),
                }
            ],
            context={
                "scope": "small",
                "review_loop": {
                    "iteration": 0,
                    "original_task_id": str(original_task.id),
                },
            },
        )
        db_session.add(source_task)
        await db_session.commit()
        return await _spawn_pipeline_task(
            source_task,
            "fix",
            db_session,
            pr_number=555,
            branch="feat/idem",
            base_branch="main",
            original_task_id=str(original_task.id),
        )

    for _ in range(10):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[
                    {
                        "type": "review",
                        "pr_number": 555,
                        "branch": "feat/idem",
                        "base_branch": "main",
                    }
                ],
            )
        )
    await db_session.commit()

    await _spawn_once()
    assert close_mock.await_count == 1
    assert alert_mock.call_count == 1
    assert comment_mock.await_count == 1

    await _spawn_once()
    assert close_mock.await_count == 1, (
        f"close_pull_request must only fire once; idempotent handler got {close_mock.await_count} calls"
    )
    assert alert_mock.call_count == 1, (
        f"Alert must only fire once; got {alert_mock.call_count}"
    )
    assert comment_mock.await_count == 1, (
        f"PR comment must only fire once; got {comment_mock.await_count}"
    )

    await _spawn_once()
    assert close_mock.await_count == 1


@pytest.mark.asyncio
async def test_per_chain_cap_small_vs_medium_scope(db_session, mocker):
    """small scope caps at 10 while medium scope allows up to 12 — verifies the
    per-chain cap is scope-aware (not a fixed constant)."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    # At iteration=11, small scope (cap=10) should block; medium scope (cap=12) allows.
    for scope, should_block in [("small", True), ("medium", False)]:
        original = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
            context={"scope": scope},
        )
        db_session.add(original)
        await db_session.commit()
        await db_session.refresh(original)

        source = make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            payloads=[
                {
                    "type": "review",
                    "pr_number": 200 + (1 if scope == "small" else 2),
                    "branch": "b",
                    "base_branch": "main",
                }
            ],
            context={
                "scope": scope,
                "review_loop": {
                    "iteration": 11,
                    "original_task_id": str(original.id),
                    "pr_number": 200 + (1 if scope == "small" else 2),
                    "branch": "b",
                    "base_branch": "main",
                },
            },
        )
        db_session.add(source)
        await db_session.commit()
        await db_session.refresh(source)

        result, _ = await _spawn_pipeline_task(
            source,
            "fix",
            db_session,
            pr_number=source.first_payload["pr_number"],
            branch="b",
            base_branch="main",
            original_task_id=str(original.id),
            fix_prompt="Fix it",
        )

        if should_block:
            assert result is None, (
                f"scope={scope!r} at iteration=11 should be blocked (cap=10)"
            )
        else:
            assert result is not None, (
                f"scope={scope!r} at iteration=11 should be allowed (cap=12)"
            )


# ── _spawn_pipeline_task: stuck-cycle detection ───────────────────────────────


def _issue(description: str, severity: str = "CRITICAL") -> dict:
    return {"description": description, "severity": severity}


@pytest.mark.asyncio
async def test_stuck_cycle_detected_at_50pct_blocks_spawn(db_session, mocker):
    """When ≥50% of CRITICAL/MAJOR issues repeat across consecutive iterations,
    _spawn_pipeline_task returns None and marks the original task requires_approval."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Source task at iteration=1 with one prior review entry that has a CRITICAL issue.
    prior_issues = [_issue("null pointer dereference", "CRITICAL")]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 301,
                "branch": "b1",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 301,
                "branch": "b1",
                "base_branch": "main",
                "review_history": [
                    {
                        "iteration": 0,
                        "feedback": "prev feedback",
                        "issues": prior_issues,
                    },
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Current iteration repeats 100% of CRITICAL issues from the prior entry.
    current_issues = [_issue("null pointer dereference", "CRITICAL")]

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=301,
        branch="b1",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Still the same issue",
        current_feedback="Still the same issue",
        current_issues=current_issues,
    )

    assert result is None, "Stuck cycle (100% repeat) should block the fix spawn"

    await db_session.refresh(original_task)
    assert original_task.requires_approval is True, (
        "Stuck-cycle detection must set requires_approval=True"
    )
    assert "stuck" in (original_task.latest_status_message or "").lower(), (
        f"Status message should mention stuck cycle: {original_task.latest_status_message!r}"
    )


@pytest.mark.asyncio
async def test_stuck_cycle_distinct_issues_do_not_match(db_session, mocker):
    """Genuinely-different issues must NOT trigger stuck-cycle detection even
    when prior and current iterations both contain CRITICAL/MAJOR feedback.
    Jaccard token similarity below the threshold keeps the pipeline moving on
    real progress between iterations."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    prior_issues = [
        _issue("Missing error handling in processUser", "CRITICAL"),
        _issue("Missing error handling in processPayment", "CRITICAL"),
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 305,
                "branch": "b5",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 305,
                "branch": "b5",
                "base_branch": "main",
                "review_history": [
                    {
                        "iteration": 0,
                        "feedback": "two handlers",
                        "issues": prior_issues,
                    },
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Current issues: distinct topics with no meaningful token overlap with prior.
    current_issues = [
        _issue("Cache invalidation race condition", "CRITICAL"),
        _issue("Database connection pool exhaustion", "CRITICAL"),
    ]

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=305,
        branch="b5",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="New handlers to fix",
        current_feedback="New handlers to fix",
        current_issues=current_issues,
    )

    assert result is not None, (
        "Distinct issues with no token overlap must not trigger stuck-cycle detection"
    )
    assert result.status == TaskStatus.QUEUED

    await db_session.refresh(original_task)
    assert original_task.requires_approval is not True, (
        "Distinct issues alone must not escalate to requires_approval"
    )


@pytest.mark.asyncio
async def test_stuck_cycle_detects_rephrased_issue(db_session, mocker):
    """Issues rephrased between iterations (same meaning, different wording)
    must trigger stuck-cycle detection. Token Jaccard similarity catches what
    exact equality would miss — e.g. 'Error handling missing in payment
    processor' vs 'Payment processor lacks error handling'."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    prior_issues = [
        _issue("Error handling code missing in payment processor", "CRITICAL")
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 306,
                "branch": "b6",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 306,
                "branch": "b6",
                "base_branch": "main",
                "review_history": [
                    {
                        "iteration": 0,
                        "feedback": "needs error handling",
                        "issues": prior_issues,
                    },
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Same underlying issue, different phrasing. Exact-match would miss this;
    # token Jaccard (5 shared / 7 union ≈ 0.71) correctly identifies the repeat.
    current_issues = [_issue("Payment processor code lacks error handling", "CRITICAL")]

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=306,
        branch="b6",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Try again",
        current_feedback="Try again",
        current_issues=current_issues,
    )

    assert result is None, (
        "Rephrased repeat issue should be detected and block fix spawn"
    )

    await db_session.refresh(original_task)
    assert original_task.requires_approval is True, (
        "Stuck-cycle on rephrased issue must escalate via requires_approval"
    )
    assert "stuck" in (original_task.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_stuck_cycle_not_triggered_below_threshold(db_session, mocker):
    """When fewer than 50% of CRITICAL/MAJOR issues repeat, no stuck cycle fires."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # 2 CRITICAL issues in the prior entry, only 1 of 3 current issues repeats (<50%).
    prior_issues = [
        _issue("Memory leak in cache cleanup", "CRITICAL"),
        _issue("Deadlock on retry path", "CRITICAL"),
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 302,
                "branch": "b2",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 302,
                "branch": "b2",
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": "feedback", "issues": prior_issues},
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # 3 current issues, only 1 repeats (33% < 50% threshold). The two new issues
    # have no token overlap with prior, keeping Jaccard similarity well below 0.5.
    current_issues = [
        _issue("Memory leak in cache cleanup", "CRITICAL"),  # repeats
        _issue("TLS handshake fails on reconnect", "CRITICAL"),
        _issue("Race condition in queue dispatch", "MAJOR"),
    ]

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=302,
        branch="b2",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Some things improved",
        current_feedback="Some things improved",
        current_issues=current_issues,
    )

    assert result is not None, (
        "33% repeat rate is below threshold — spawn should proceed"
    )
    assert result.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_refactor_stuck_threshold_is_65pct(db_session, mocker):
    """For refactor source tasks, the stuck threshold is 65%, not 50%.
    At exactly 60% repeat, a non-refactor would be stuck but a refactor is not."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "refactor", "prompt": "Restructure", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # 5 prior CRITICAL issues; current has 5 issues, 3 of which repeat (60%).
    prior_issues = [_issue(f"structural issue {i}", "CRITICAL") for i in range(5)]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        # Source type 'refactor' → 75% threshold.
        payloads=[
            {
                "type": "refactor",
                "pr_number": 303,
                "branch": "b3",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 303,
                "branch": "b3",
                "base_branch": "main",
                "review_history": [
                    {
                        "iteration": 0,
                        "feedback": "structural concerns",
                        "issues": prior_issues,
                    },
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # 3 of 5 current issues repeat → 60%, below 65% refactor threshold.
    current_issues = [
        _issue("structural issue 0", "CRITICAL"),  # repeats
        _issue("structural issue 1", "CRITICAL"),  # repeats
        _issue("structural issue 2", "CRITICAL"),  # repeats
        _issue("new issue X", "CRITICAL"),
        _issue("new issue Y", "MAJOR"),
    ]

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=303,
        branch="b3",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Partially improved",
        current_feedback="Partially improved",
        current_issues=current_issues,
    )

    assert result is not None, (
        "60% repeat rate is below the 65% refactor threshold — spawn should proceed"
    )
    assert result.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_refactor_stuck_at_80pct(db_session, mocker):
    """For refactor source tasks, 80% repeat rate exceeds the 65% threshold → stuck."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "refactor", "prompt": "Restructure", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    prior_issues = [_issue(f"arch issue {i}", "CRITICAL") for i in range(5)]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "refactor",
                "pr_number": 304,
                "branch": "b4",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 304,
                "branch": "b4",
                "base_branch": "main",
                "review_history": [
                    {
                        "iteration": 0,
                        "feedback": "arch concerns",
                        "issues": prior_issues,
                    },
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # 4 of 5 current issues repeat → 80% ≥ 65% → stuck.
    current_issues = [
        _issue("arch issue 0", "CRITICAL"),
        _issue("arch issue 1", "CRITICAL"),
        _issue("arch issue 2", "CRITICAL"),
        _issue("arch issue 3", "CRITICAL"),
        _issue("new issue Z", "CRITICAL"),
    ]

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=304,
        branch="b4",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Still mostly the same",
        current_feedback="Still mostly the same",
        current_issues=current_issues,
    )

    assert result is None, "80% repeat rate exceeds the 65% refactor threshold → stuck"

    await db_session.refresh(original_task)
    assert original_task.requires_approval is True


@pytest.mark.asyncio
async def test_refactor_stuck_detected_earlier_at_70pct(db_session, mocker):
    """Lowered refactor threshold (0.65) catches stuck cycles at 70% that the
    previous 0.75 threshold would have let continue for additional iterations."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "refactor", "prompt": "Restructure", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # 10 prior CRITICAL issues; 7 of 10 current issues repeat → 70%.
    prior_issues = [_issue(f"layer issue {i}", "CRITICAL") for i in range(10)]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "refactor",
                "pr_number": 305,
                "branch": "b5",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 305,
                "branch": "b5",
                "base_branch": "main",
                "review_history": [
                    {
                        "iteration": 0,
                        "feedback": "layer concerns",
                        "issues": prior_issues,
                    },
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    current_issues = [_issue(f"layer issue {i}", "CRITICAL") for i in range(7)] + [
        _issue("new A", "CRITICAL"),
        _issue("new B", "CRITICAL"),
        _issue("new C", "MAJOR"),
    ]

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=305,
        branch="b5",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Partial",
        current_feedback="Partial",
        current_issues=current_issues,
    )

    assert result is None, (
        "70% repeat rate meets the lowered 65% refactor threshold → stuck "
        "(previously 0.75 would have let this continue)"
    )


# ── Edge cases ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_edge_empty_review_history_no_stuck_cycle(db_session, mocker):
    """When review_history is empty (first fix attempt), stuck-cycle detection
    cannot fire because len(new_review_history) < 2."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {"type": "create", "pr_number": 401, "branch": "b10", "base_branch": "main"}
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "pr_number": 401,
                "branch": "b10",
                "base_branch": "main",
                "review_history": [],  # empty — no prior entries
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    current_issues = [_issue("some bug", "CRITICAL")]

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=401,
        branch="b10",
        base_branch="main",
        fix_prompt="Fix the bug",
        current_feedback="Fix the bug",
        current_issues=current_issues,
    )

    # Empty history → new_review_history has 1 entry → len < 2 → no stuck detection.
    assert result is not None, (
        "Empty review_history should not trigger stuck-cycle detection"
    )
    assert result.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_edge_zero_current_issues_no_stuck_cycle(db_session, mocker):
    """When the current iteration has zero CRITICAL/MAJOR issues, the stuck check
    short-circuits because curr_critical_major is falsy."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    prior_issues = [_issue("real problem", "CRITICAL")]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {"type": "create", "pr_number": 402, "branch": "b11", "base_branch": "main"}
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "pr_number": 402,
                "branch": "b11",
                "base_branch": "main",
                "review_history": [
                    {
                        "iteration": 0,
                        "feedback": "prior feedback",
                        "issues": prior_issues,
                    },
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Zero current CRITICAL/MAJOR issues → no stuck cycle possible.
    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=402,
        branch="b11",
        base_branch="main",
        fix_prompt="All fixed now",
        current_feedback="All fixed now",
        current_issues=[],  # no issues in this iteration
    )

    assert result is not None, (
        "Zero current issues must not trigger stuck-cycle detection"
    )
    assert result.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_edge_issues_without_severity_ignored_by_stuck_cycle(db_session, mocker):
    """Issues that lack a 'severity' key (or have severity != CRITICAL/MAJOR) are
    excluded from stuck-cycle matching, so the stuck check never fires."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    # Prior and current issues have only MINOR severity — not counted.
    prior_issues = [
        {"description": "formatting nit", "severity": "MINOR"},
        {"description": "missing docstring"},  # no severity key at all
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {"type": "create", "pr_number": 403, "branch": "b12", "base_branch": "main"}
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "pr_number": 403,
                "branch": "b12",
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": "minor nits", "issues": prior_issues},
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    current_issues = [
        {"description": "formatting nit", "severity": "MINOR"},  # not CRITICAL/MAJOR
        {"description": "missing docstring"},  # no severity
        {
            "description": "another style suggestion",
            "severity": "minor",
        },  # case insensitive check
    ]

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=403,
        branch="b12",
        base_branch="main",
        fix_prompt="Style fixes only",
        current_feedback="Style fixes only",
        current_issues=current_issues,
    )

    assert result is not None, (
        "Issues with only MINOR/absent severity must not trigger stuck-cycle detection"
    )
    assert result.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_edge_only_major_issues_count_for_stuck_cycle(db_session, mocker):
    """MAJOR issues (not just CRITICAL) are counted in the stuck-cycle calculation."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    prior_issues = [_issue("memory leak in handler", "MAJOR")]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 404,
                "branch": "b13",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 404,
                "branch": "b13",
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": "major bug", "issues": prior_issues},
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Same MAJOR issue repeats 100% → stuck (MAJOR is counted).
    current_issues = [_issue("memory leak in handler", "MAJOR")]

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=404,
        branch="b13",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Leak still present",
        current_feedback="Leak still present",
        current_issues=current_issues,
    )

    assert result is None, (
        "100% repeat of MAJOR issues should trigger stuck-cycle detection"
    )

    await db_session.refresh(original_task)
    assert original_task.requires_approval is True


# ── Stuck-cycle detection: feedback-text fallback ─────────────────────────────


def test_feedback_jaccard_unit():
    """_feedback_jaccard returns intersection/union over normalized tokens (≥3 chars)."""
    assert _feedback_jaccard("", "anything") == 0.0
    assert (
        _feedback_jaccard("a b c", "a b c") == 0.0
    )  # all tokens <3 chars → empty sets
    # Identical content (after lowercasing) → 1.0
    assert (
        _feedback_jaccard("Null pointer in handler", "null POINTER in HANDLER") == 1.0
    )
    # Disjoint content → 0.0
    assert _feedback_jaccard("memory leak", "race condition") == 0.0
    # Partial overlap: {"missing", "error", "handling"} ∩ {"missing", "error", "checks"}
    # → 2 / 4 = 0.5
    assert _feedback_jaccard("missing error handling", "missing error checks") == 0.5


@pytest.mark.asyncio
async def test_stuck_cycle_text_fallback_blocks_when_feedback_repeats(
    db_session, mocker
):
    """When neither prior nor current iteration carries a structured `issues` list
    but their feedback text is highly similar (Jaccard ≥ 0.7), the fallback path
    detects a stuck cycle and escalates for manual review."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Prior history entry has feedback only, no `issues` key — exactly the shape
    # produced when verdict parsing falls back to log/keyword scraping.
    prior_feedback = (
        "Tests are failing in test_pipeline_health.py "
        "because retry backoff exceeds the configured timeout window."
    )
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 601,
                "branch": "b20",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 601,
                "branch": "b20",
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": prior_feedback},  # no `issues` key
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Current iteration also has no structured issues, and its feedback is nearly
    # identical to the prior iteration's — the fix agent made no progress.
    current_feedback = (
        "Tests are failing in test_pipeline_health.py "
        "because retry backoff exceeds the configured timeout window now."
    )

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=601,
        branch="b20",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Same failure",
        current_feedback=current_feedback,
        current_issues=None,  # no structured issues this iteration either
    )

    assert result is None, (
        "Highly-similar feedback text across iterations (no structured issues on "
        "either side) should trigger the stuck-cycle fallback"
    )

    await db_session.refresh(original_task)
    assert original_task.requires_approval is True, (
        "Text-fallback stuck detection must escalate via requires_approval=True"
    )
    assert "stuck" in (original_task.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_stuck_cycle_text_fallback_allows_distinct_feedback(db_session, mocker):
    """When structured issues are absent but feedback text is genuinely different
    between iterations, the fallback must NOT trigger — the fix agent is making
    progress on different problems each time."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 602,
                "branch": "b21",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 602,
                "branch": "b21",
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": "Linting errors in admin_settings.py"},
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=602,
        branch="b21",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="New problem",
        current_feedback="Database migration produces duplicate key constraint violation",
        current_issues=None,
    )

    assert result is not None, (
        "Distinct feedback text must not trigger the fallback stuck-cycle path"
    )
    assert result.status == TaskStatus.QUEUED

    await db_session.refresh(original_task)
    assert original_task.requires_approval is not True


@pytest.mark.asyncio
async def test_stuck_cycle_text_fallback_when_only_one_side_lacks_issues(
    db_session, mocker
):
    """The fallback must also fire when only ONE side lacks structured issues
    (asymmetric case): prior has no `issues` key, current does — but neither
    direction can run the structured comparison, so feedback similarity governs."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    prior_feedback = "Refactor breaks public interface used by external consumers"
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 603,
                "branch": "b22",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 603,
                "branch": "b22",
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": prior_feedback},  # no `issues` key
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Current iteration has structured issues but with similar feedback text.
    current_feedback = "Refactor breaks public interface used by external consumers."
    current_issues = [_issue("interface stability", "CRITICAL")]

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=603,
        branch="b22",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Still breaking interface",
        current_feedback=current_feedback,
        current_issues=current_issues,
    )

    assert result is None, (
        "When prior entry lacks `issues`, structured comparison cannot run; "
        "highly-similar feedback text must trigger the fallback"
    )
    await db_session.refresh(original_task)
    assert original_task.requires_approval is True


@pytest.mark.asyncio
async def test_stuck_cycle_text_fallback_skipped_when_feedback_empty(
    db_session, mocker
):
    """When structured issues are absent AND one side has empty feedback, the
    fallback cannot compute similarity and must safely allow the spawn."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {"type": "create", "pr_number": 604, "branch": "b23", "base_branch": "main"}
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "pr_number": 604,
                "branch": "b23",
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": ""},  # empty feedback, no issues
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=604,
        branch="b23",
        base_branch="main",
        fix_prompt="Try again",
        current_feedback="Some new feedback",
        current_issues=None,
    )

    assert result is not None, (
        "Empty prior feedback must not trigger stuck-cycle (cannot compare)"
    )


def test_stuck_cycle_text_threshold_constants():
    """Tripwire: text-fallback thresholds must remain higher than the structured
    thresholds to compensate for the noisier comparison."""
    assert STUCK_CYCLE_TEXT_THRESHOLD_DEFAULT == 0.7
    assert STUCK_CYCLE_TEXT_THRESHOLD_REFACTOR == 0.75
    assert STUCK_CYCLE_TEXT_THRESHOLD_DEFAULT > STUCK_CYCLE_THRESHOLD_DEFAULT
    assert STUCK_CYCLE_TEXT_THRESHOLD_REFACTOR > STUCK_CYCLE_THRESHOLD_REFACTOR


def test_stuck_cycle_issue_similarity_threshold_constants():
    """Tripwire: rephrased-issue thresholds must stay above 0.6 so that two
    issues sharing only boilerplate tokens (e.g. processUser vs processOrder,
    Jaccard = 0.6) cannot trigger the secondary fallback."""
    assert STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_DEFAULT == 0.68
    assert STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_REFACTOR == 0.7
    assert STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_DEFAULT > 0.6
    assert STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_REFACTOR > 0.6


def test_stuck_cycle_medium_thresholds_match_default():
    """Tripwire: medium-scope thresholds match the defaults so cross-module PRs
    get the same patience as other scopes to converge on cross-file fixes."""
    assert STUCK_CYCLE_THRESHOLD_MEDIUM == 0.55
    assert STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_MEDIUM == 0.68
    assert STUCK_CYCLE_TEXT_THRESHOLD_MEDIUM == 0.7
    assert STUCK_CYCLE_THRESHOLD_MEDIUM == STUCK_CYCLE_THRESHOLD_DEFAULT
    assert (
        STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_MEDIUM
        == STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_DEFAULT
    )
    assert STUCK_CYCLE_TEXT_THRESHOLD_MEDIUM == STUCK_CYCLE_TEXT_THRESHOLD_DEFAULT


async def _run_stuck_scope_case(db_session, mocker, *, scope: str, pr_number: int):
    """Drive _spawn_pipeline_task at ~54.5% exact-issue repeat.

    Returns the result tuple plus the reloaded original task so callers can
    assert scope-conditional stuck behaviour.
    """
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # 6 of 11 current CRITICAL issues repeat → ~54.5% exact-match, stays below
    # the 0.55 default threshold (which now applies to all non-refactor scopes).
    # New issues share no tokens with prior, keeping Jaccard below both rephrased thresholds.
    prior_issues = [_issue(f"alpha issue {i}", "CRITICAL") for i in range(6)]
    branch = f"b-{scope}"
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": scope,
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": "prev", "issues": prior_issues},
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    current_issues = [
        _issue("alpha issue 0", "CRITICAL"),  # repeats
        _issue("alpha issue 1", "CRITICAL"),  # repeats
        _issue("alpha issue 2", "CRITICAL"),  # repeats
        _issue("alpha issue 3", "CRITICAL"),  # repeats
        _issue("alpha issue 4", "CRITICAL"),  # repeats
        _issue("alpha issue 5", "CRITICAL"),  # repeats
        _issue("zeta fresh concern", "CRITICAL"),
        _issue("omega distinct finding", "CRITICAL"),
        _issue("kappa separate worry", "CRITICAL"),
        _issue("theta new worry", "CRITICAL"),
        _issue("sigma other concern", "CRITICAL"),
    ]
    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=pr_number,
        branch=branch,
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Partial",
        current_feedback="Partial",
        current_issues=current_issues,
    )
    await db_session.refresh(original_task)
    return result, original_task


@pytest.mark.asyncio
async def test_stuck_cycle_medium_scope_unaffected_at_55pct(db_session, mocker):
    """Medium-scope tasks share the 0.55 default and pass through at ~54.5%."""
    result, original_task = await _run_stuck_scope_case(
        db_session, mocker, scope="medium", pr_number=701
    )
    assert result is not None, (
        "Medium scope must not trigger at ~54.5% — threshold matches the 0.55 default"
    )
    assert result.status == TaskStatus.QUEUED
    assert original_task.requires_approval is not True


@pytest.mark.asyncio
async def test_stuck_cycle_small_scope_unaffected_at_55pct(db_session, mocker):
    """Small-scope tasks keep the 0.55 default threshold and pass through at ~54.5%."""
    result, original_task = await _run_stuck_scope_case(
        db_session, mocker, scope="small", pr_number=702
    )
    assert result is not None, (
        "Small scope must not trigger at ~54.5% — default threshold remains 0.55"
    )
    assert result.status == TaskStatus.QUEUED
    assert original_task.requires_approval is not True


@pytest.mark.asyncio
async def test_stuck_cycle_detects_rephrased_issues(db_session, mocker):
    """When both iterations have structured CRITICAL/MAJOR issues whose
    descriptions are rewordings of the same concepts (0% exact match but
    high token overlap), the rephrased-issue fallback flags the cycle."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    prior_issues = [
        _issue("Missing null check in handler", "CRITICAL"),
        _issue("Race condition in scheduler", "CRITICAL"),
        _issue("Query builder direct concat", "CRITICAL"),
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 401,
                "branch": "b1",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 401,
                "branch": "b1",
                "base_branch": "main",
                "review_history": [
                    {
                        "iteration": 0,
                        "feedback": "prev feedback",
                        "issues": prior_issues,
                    },
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Same concepts, different wording — zero exact matches.
    current_issues = [
        _issue("Handler lacks null check", "CRITICAL"),
        _issue("Scheduler has race condition", "CRITICAL"),
        _issue("Direct string concat in query builder", "CRITICAL"),
    ]

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=401,
        branch="b1",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Same issues, reworded",
        current_feedback="Same issues, reworded",
        current_issues=current_issues,
    )

    assert result is None, (
        "Rephrased but conceptually identical issues should trigger the "
        "rephrased-issue fallback and block the fix spawn"
    )

    await db_session.refresh(original_task)
    assert original_task.requires_approval is True, (
        "Rephrased-issue stuck detection must set requires_approval=True"
    )
    assert "stuck" in (original_task.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_stuck_cycle_rephrased_fallback_ignores_distinct_targets(
    db_session, mocker
):
    """Issues sharing boilerplate tokens but targeting genuinely different
    symbols must NOT trip the rephrased-issue fallback. 'Missing error
    handling in processUser' vs 'Missing error handling in processOrder'
    yields Jaccard = 0.6, just below the 0.68 default threshold.
    """
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    prior_issues = [
        _issue("Missing error handling in processUser", "CRITICAL"),
        _issue("Missing error handling in processPayment", "CRITICAL"),
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 402,
                "branch": "b2",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 402,
                "branch": "b2",
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": "prev", "issues": prior_issues},
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Shared pattern, different target symbols — must not be treated as rephrased.
    current_issues = [
        _issue("Missing error handling in processOrder", "CRITICAL"),
        _issue("Missing error handling in processRefund", "CRITICAL"),
    ]

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=402,
        branch="b2",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="New handlers to fix",
        current_feedback="New handlers to fix",
        current_issues=current_issues,
    )

    assert result is not None, (
        "Distinct-target issues sharing boilerplate tokens must not trigger "
        "the rephrased-issue fallback"
    )
    assert result.status == TaskStatus.QUEUED

    await db_session.refresh(original_task)
    assert original_task.requires_approval is not True


@pytest.mark.asyncio
async def test_stuck_cycle_refactor_uses_higher_rephrased_threshold(db_session, mocker):
    """For refactor source tasks the rephrased-issue threshold is 0.8, not 0.65.
    A Jaccard similarity of ~0.67 (enough to trip default) must NOT escalate a
    refactor cycle."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "refactor", "prompt": "Restructure", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Same wording set as the default-threshold rephrased test. Jaccard ≈ 0.67,
    # which clears default (0.65) but not refactor (0.8).
    prior_issues = [
        _issue("Missing null check in handler", "CRITICAL"),
        _issue("Race condition in scheduler", "CRITICAL"),
        _issue("Query builder uses direct concat", "CRITICAL"),
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "refactor",
                "pr_number": 403,
                "branch": "b3",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 403,
                "branch": "b3",
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": "prev", "issues": prior_issues},
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    current_issues = [
        _issue("Handler lacks null check", "CRITICAL"),
        _issue("Scheduler has race condition", "CRITICAL"),
        _issue("Direct string concat in query builder", "CRITICAL"),
    ]

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=403,
        branch="b3",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Same issues, reworded",
        current_feedback="Same issues, reworded",
        current_issues=current_issues,
    )

    assert result is not None, (
        "Refactor rephrased-issue threshold (0.8) must not fire at ~0.67 similarity"
    )
    assert result.status == TaskStatus.QUEUED


# ── Strategic context propagation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_strategic_context_propagates_to_spawned_fix(db_session, mocker):
    """A spawned fix task must inherit direction_id, related_files, parent_context,
    and scope from the source task's context so downstream agents see the same
    strategic context (direction, intent, relevant files) the create task had.
    Internal underscore-prefixed flags must not leak into the spawned task."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    direction_id = str(uuid.uuid4())
    related_files = ["backend/app/foo.py", "backend/app/bar.py"]
    parent_context = "Parent research: improve foo subsystem reliability"

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 501,
                "branch": "feat/ctx",
                "base_branch": "main",
                "prompt": "Build X",
            }
        ],
        context={
            "scope": "small",
            "direction_id": direction_id,
            "related_files": related_files,
            "parent_context": parent_context,
            "architecture_context": "Some architecture note",
            "_is_rollup_review": True,  # internal flag — must NOT propagate
            "_rollup_retries": 2,  # internal flag — must NOT propagate
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=501,
        branch="feat/ctx",
        base_branch="main",
        fix_prompt="Address the review feedback",
    )

    assert created is True
    assert result is not None

    ctx = result.context or {}
    assert ctx.get("direction_id") == direction_id, (
        "direction_id must propagate to spawned task"
    )
    assert ctx.get("related_files") == related_files, (
        "related_files must propagate to spawned task"
    )
    assert ctx.get("parent_context") == parent_context, (
        "parent_context must propagate to spawned task"
    )
    assert ctx.get("scope") == "small", "scope must propagate to spawned task"
    assert ctx.get("architecture_context") == "Some architecture note", (
        "non-private strategic context should also propagate"
    )

    # Internal flags must not leak into the spawned task's context.
    assert "_is_rollup_review" not in ctx, (
        "underscore-prefixed internal flags must not propagate"
    )
    assert "_rollup_retries" not in ctx, (
        "underscore-prefixed internal flags must not propagate"
    )

    # review_loop is overridden with the pipeline's own iteration state, not carried verbatim.
    assert ctx.get("review_loop", {}).get("iteration") == 1
    assert ctx.get("review_loop", {}).get("pr_number") == 501


@pytest.mark.asyncio
async def test_ci_failure_logs_propagate_to_spawned_fix_context(db_session, mocker):
    """CI-failure fix tasks carry structured failed-log context so the agent can
    derive targeted pytest selectors instead of treating the text prompt as the
    only source of truth."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 502,
                "branch": "feat/ci",
                "base_branch": "main",
                "prompt": "Build X",
            }
        ],
        context={"scope": "medium", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    ci_logs = "FAILED backend/testing/test_models.py::test_create - AssertionError"
    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=502,
        branch="feat/ci",
        base_branch="main",
        fix_prompt="Fix CI",
        fix_reason="ci_failure",
        ci_failure_logs=ci_logs,
    )

    assert created is True
    assert result is not None
    assert (result.context or {}).get("ci_failure_logs") == ci_logs
    assert (result.context or {}).get("review_loop", {}).get(
        "fix_reason"
    ) == "ci_failure"


@pytest.mark.asyncio
async def test_investigated_files_propagate_to_spawned_fix_context(db_session, mocker):
    """Code-review fix tasks carry the reviewer's working set forward so the
    fix agent can start at the paths the reviewer already opened, rather than
    rediscovering the codebase from scratch each iteration."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 503,
                "branch": "feat/inv",
                "base_branch": "main",
                "prompt": "Build Y",
            }
        ],
        context={"scope": "medium", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    investigated = [
        "admin-ui/src/components/DirectionCard.tsx",
        "admin-ui/src/components/DirectionCard.test.tsx",
        "backend/app/direction_metrics.py",
    ]
    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=503,
        branch="feat/inv",
        base_branch="main",
        fix_prompt="Address the review feedback",
        investigated_files=investigated,
    )

    assert created is True
    assert result is not None
    assert (result.context or {}).get("investigated_files") == investigated


@pytest.mark.asyncio
async def test_investigated_files_capped_at_25(db_session, mocker):
    """Working-set propagation respects a 25-path cap so a chatty reviewer
    can't blow up downstream task contexts."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 504,
                "branch": "feat/cap",
                "base_branch": "main",
                "prompt": "Z",
            }
        ],
        context={"scope": "medium", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    big_list = [f"src/file_{i}.py" for i in range(40)]
    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=504,
        branch="feat/cap",
        base_branch="main",
        fix_prompt="x",
        investigated_files=big_list,
    )

    assert created is True
    propagated = (result.context or {}).get("investigated_files") or []
    assert len(propagated) == 25
    assert propagated == big_list[:25]


# ── Phase 2c: warm-VM continuity pin ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_warm_pin_not_set_when_flag_disabled(db_session, mocker):
    """With warm_continuity_enabled=False, no warm fields should land on the
    spawned task's context (default behavior — feature ships dark)."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch.dict(
        "app.review_pipeline._runtime_config",
        {"warm_continuity_enabled": False},
        clear=False,
    )

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="agent-vm-source-disabled",
        payloads=[
            {
                "type": "create",
                "pr_number": 8001,
                "branch": "feat/wp1",
                "base_branch": "main",
                "prompt": "X",
            }
        ],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=8001,
        branch="feat/wp1",
        base_branch="main",
    )

    assert created is True
    assert result is not None
    ctx = result.context or {}
    assert "warm_target_vm_id" not in ctx, (
        "warm pin must not be set when feature is disabled"
    )
    assert "warm_target_pr" not in ctx
    assert "warm_handoff_deadline" not in ctx
    # Spawned task should still get a fresh VM ID since no warm pin is active.
    assert result.vm_instance_id is not None


@pytest.mark.asyncio
async def test_warm_pin_set_when_flag_enabled_and_source_in_progress(
    db_session, mocker
):
    """With warm_continuity_enabled=True and source IN_PROGRESS with a vm_instance_id,
    the spawned task carries warm_target_vm_id, warm_target_pr, and a
    warm_handoff_deadline in the future, and is created with vm_instance_id=None."""
    from datetime import datetime

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch.dict(
        "app.review_pipeline._runtime_config",
        {"warm_continuity_enabled": True},
        clear=False,
    )

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="agent-vm-source-warm",
        payloads=[
            {
                "type": "create",
                "pr_number": 8002,
                "branch": "feat/wp2",
                "base_branch": "main",
                "prompt": "X",
            }
        ],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=8002,
        branch="feat/wp2",
        base_branch="main",
    )

    assert created is True
    assert result is not None
    ctx = result.context or {}
    assert ctx.get("warm_target_vm_id") == "agent-vm-source-warm"
    assert str(ctx.get("warm_target_pr")) == "8002"
    deadline_iso = ctx.get("warm_handoff_deadline")
    assert deadline_iso, "warm_handoff_deadline should be set"
    deadline = datetime.fromisoformat(str(deadline_iso))
    if deadline.tzinfo is not None:
        deadline = deadline.replace(tzinfo=None)
    assert deadline > datetime.utcnow(), "deadline should be in the future"
    # Spawned task must have vm_instance_id=NULL so the orchestrator's
    # provisioning loop knows to consult the heartbeat map.
    assert result.vm_instance_id is None


@pytest.mark.asyncio
async def test_warm_pin_not_set_when_source_completed_long_ago(db_session, mocker):
    """When source task is terminal AND its last_heartbeat is older than
    WARM_HANDOFF_WINDOW_SECS, warm pin must not propagate."""
    from datetime import datetime, timedelta
    from app.review_pipeline import WARM_HANDOFF_WINDOW_SECS

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch.dict(
        "app.review_pipeline._runtime_config",
        {"warm_continuity_enabled": True},
        clear=False,
    )

    project = await _make_db_project(db_session)
    long_ago = datetime.utcnow() - timedelta(seconds=WARM_HANDOFF_WINDOW_SECS + 60)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_instance_id="agent-vm-source-stale",
        last_heartbeat=long_ago,
        payloads=[
            {
                "type": "create",
                "pr_number": 8003,
                "branch": "feat/wp3",
                "base_branch": "main",
                "prompt": "X",
            }
        ],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=8003,
        branch="feat/wp3",
        base_branch="main",
    )

    assert created is True
    ctx = result.context or {}
    assert "warm_target_vm_id" not in ctx, (
        "warm pin must not propagate when source completed beyond WARM_HANDOFF_WINDOW_SECS"
    )
    assert result.vm_instance_id is not None


@pytest.mark.asyncio
async def test_warm_pin_suppressed_for_approve_path(db_session, mocker):
    """suppress_warm_handoff=True (passed by the APPROVE-CI-failure spawn)
    must NOT propagate warm pin even when the source/flag would otherwise allow it."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch.dict(
        "app.review_pipeline._runtime_config",
        {"warm_continuity_enabled": True},
        clear=False,
    )

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="agent-vm-source-approve",
        payloads=[
            {
                "type": "create",
                "pr_number": 8004,
                "branch": "feat/wp4",
                "base_branch": "main",
                "prompt": "X",
            }
        ],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=8004,
        branch="feat/wp4",
        base_branch="main",
        fix_prompt="CI fix",
        fix_reason="ci_failure",
        ci_failure_logs="FAILED test_x",
        suppress_warm_handoff=True,
    )

    assert created is True
    assert result is not None
    ctx = result.context or {}
    assert "warm_target_vm_id" not in ctx, (
        "suppress_warm_handoff must prevent warm-pin propagation on APPROVE path"
    )
    assert result.vm_instance_id is not None


@pytest.mark.asyncio
async def test_warm_target_pr_stored_as_int(db_session, mocker):
    """Issue 6: warm_target_pr must be stored as an int, not a string. The
    routers/agent.py:next_role query reads via .as_string() (which on Postgres
    reads ->> and always returns text). Storing an int matches production —
    SQLite test backend serializes JSONB as JSON text but the in-memory dict
    on the freshly refreshed Task instance preserves the original Python
    type, so we can assert directly here.

    This is the lighter SQLite-friendly assertion. The Postgres-tagged
    round-trip variant below additionally exercises the .as_string() query
    that next_role uses."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch.dict(
        "app.review_pipeline._runtime_config",
        {"warm_continuity_enabled": True},
        clear=False,
    )

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="agent-vm-source-int-pr",
        payloads=[
            {
                "type": "create",
                "pr_number": 8765,
                "branch": "feat/intpr",
                "base_branch": "main",
                "prompt": "X",
            }
        ],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=8765,
        branch="feat/intpr",
        base_branch="main",
    )
    assert created is True
    assert result is not None
    await db_session.refresh(result)

    ctx = result.context or {}
    assert "warm_target_pr" in ctx, (
        "warm_target_pr should be set when warm-pin propagates"
    )
    stored_pr = ctx["warm_target_pr"]
    # Accept int — the production fix is to coerce explicitly to int().
    # On SQLite, JSON deserialization should preserve numeric type. If the
    # backend ever ends up returning a string ("8765"), the next_role query
    # `.as_string() == str(req.prior_pr_number)` still succeeds on Postgres
    # (->> always produces text), but the in-memory representation diverges.
    # The test asserts the consistent int contract.
    assert isinstance(stored_pr, int), (
        f"warm_target_pr must be stored as int, got {type(stored_pr).__name__} "
        f"with value {stored_pr!r}"
    )
    assert stored_pr == 8765


@pytest.mark.skipif(
    os.getenv("USE_TEST_POSTGRES", "").lower() != "true"
    or not os.getenv("DATABASE_URL", "").startswith("postgresql"),
    reason="JSONB ->> round-trip semantics require real PostgreSQL",
)
@pytest.mark.asyncio
async def test_warm_target_pr_roundtrip_postgres(db_session, mocker):
    """Issue 6 (Postgres-tagged): write an int via _spawn_pipeline_task, then
    issue the same `Task.context['warm_target_pr'].as_string() == str(pr)`
    query that routers/agent.py:next_role uses. The value stored as an int
    must round-trip through ->> as the matching numeric string."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch.dict(
        "app.review_pipeline._runtime_config",
        {"warm_continuity_enabled": True},
        clear=False,
    )

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="agent-vm-source-pgrt",
        payloads=[
            {
                "type": "create",
                "pr_number": 4242,
                "branch": "feat/pgrt",
                "base_branch": "main",
                "prompt": "X",
            }
        ],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=4242,
        branch="feat/pgrt",
        base_branch="main",
    )
    assert created is True
    assert result is not None

    # Mirror the exact query routers/agent.py:next_role uses.
    matched = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.id == result.id,
                    Task.context["warm_target_pr"].as_string() == str(4242),
                )
            )
        )
        .scalars()
        .first()
    )
    assert matched is not None, (
        "Task.context['warm_target_pr'].as_string() == '4242' must match — "
        "this is the query next_role uses to find the pinned task"
    )
    """When the source task carries `_self_review_outcome` (set by the create
    handler's self-review), it must be carried forward to the spawned review
    task's context so the review agent can surface blind spots. This is a
    deliberate exception to the generic underscore-prefix filter."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    outcome = {
        "verdict": "PASS",
        "categories": ["incomplete_review"],
        "truncated_files_read": ["agent/src/prompt.ts"],
    }

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 777,
                "branch": "feat/sr",
                "base_branch": "main",
                "prompt": "Build X",
            }
        ],
        context={
            "scope": "small",
            "_self_review_outcome": outcome,
            "_other_internal": "filtered",
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=777,
        branch="feat/sr",
        base_branch="main",
    )

    assert created is True
    assert result is not None
    ctx = result.context or {}
    assert ctx.get("_self_review_outcome") == outcome, (
        "_self_review_outcome must be carried forward to enable blind-spot surfacing"
    )
    # Other underscore-prefixed flags must still be filtered.
    assert "_other_internal" not in ctx


@pytest.mark.asyncio
async def test_diagnosis_fingerprint_propagates_across_review_fix_iteration(
    db_session, mocker
):
    """Fingerprint-dependent outcome recording requires diagnosis_fingerprint (and
    trigger_failure_class / trigger_failure_taxonomy) to survive multi-iteration
    review-fix cycles. The explicit propagation from the original create task is
    the safety net when the source task's context has drifted and no longer holds
    these fields. Ensure they are copied onto the spawned fix task."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Remediate X", "base_branch": "main"}],
        context={
            "scope": "small",
            "diagnosis_task_id": "diag-fp",
            "diagnosis_fingerprint": "fp-abc123",
            "error_fingerprint": "err-xyz789",
            "trigger_failure_class": "code_error",
            "trigger_failure_taxonomy": "CODE_DEFECT",
            "diagnosis_root_cause": "rc",
            "diagnosis_recommended_fix": "rf",
        },
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Source task is a mid-cycle review task whose context has drifted — it no
    # longer carries diagnosis_fingerprint. The explicit copy from orig must
    # restore it on the spawned fix task.
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 444,
                "branch": "feat/fp",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "review_loop": {"iteration": 1, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=444,
        branch="feat/fp",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Address feedback",
    )
    assert created is True
    assert result is not None
    ctx = result.context or {}
    assert ctx.get("diagnosis_fingerprint") == "fp-abc123", (
        "diagnosis_fingerprint must survive review-fix iteration via explicit "
        "propagation from the original create task"
    )
    assert ctx.get("error_fingerprint") == "err-xyz789"
    assert ctx.get("trigger_failure_class") == "code_error"
    assert ctx.get("trigger_failure_taxonomy") == "CODE_DEFECT"


@pytest.mark.asyncio
async def test_self_review_outcome_absent_when_not_on_source(db_session, mocker):
    """When the source task has no `_self_review_outcome`, the spawned task's
    context must not synthesize one."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 778,
                "branch": "feat/sr2",
                "base_branch": "main",
                "prompt": "Build Y",
            }
        ],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=778,
        branch="feat/sr2",
        base_branch="main",
    )

    assert created is True
    assert result is not None
    ctx = result.context or {}
    assert "_self_review_outcome" not in ctx


# ── Scope-budget utilization on spawned fix ───────────────────────────────────


@pytest.mark.asyncio
async def test_scope_budget_utilization_attached_to_fix_from_source_files(
    db_session, mocker
):
    """When the source task carries _pr_changed_files, the spawned fix task's
    context exposes a scope_budget_utilization block with a non-test count
    compared against the scope's file budget."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 601,
                "branch": "feat/sb",
                "base_branch": "main",
            }
        ],
        context={
            "scope": "medium",
            "_pr_changed_files": [
                "backend/app/a.py",
                "backend/app/b.py",
                "backend/app/c.py",
                "backend/app/d.py",
                "backend/app/e.py",
                "backend/testing/test_a.py",
                "backend/testing/test_b.py",
            ],
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=601,
        branch="feat/sb",
        base_branch="main",
        fix_prompt="Address the review feedback",
    )
    assert created is True and result is not None

    util = (result.context or {}).get("scope_budget_utilization")
    assert util is not None
    assert util["scope"] == "medium"
    assert util["file_count"] == 7
    assert util["non_test_file_count"] == 5
    assert util["budget"] == 10  # medium production_files_max
    assert util["percentage"] == 50


@pytest.mark.asyncio
async def test_scope_budget_utilization_falls_back_to_create_task(db_session, mocker):
    """When the source task has no _pr_changed_files but the original (create)
    task does, the utilization is computed against the create task's snapshot."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "X", "base_branch": "main"}],
        context={
            "scope": "small",
            "_pr_changed_files": [
                "backend/app/a.py",
                "backend/app/b.py",
                "backend/testing/test_a.py",
            ],
        },
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 602,
                "branch": "feat/fb",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "review_loop": {"iteration": 0, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=602,
        branch="feat/fb",
        base_branch="main",
        fix_prompt="fix",
        original_task_id=str(original_task.id),
    )
    assert created is True and result is not None
    util = (result.context or {}).get("scope_budget_utilization")
    assert util is not None
    assert util["non_test_file_count"] == 2
    assert util["budget"] == 6  # small total_files_max
    assert util["scope"] == "small"


@pytest.mark.asyncio
async def test_scope_budget_utilization_absent_when_no_changed_files(
    db_session, mocker
):
    """No _pr_changed_files anywhere → no utilization block emitted (can't bluff)."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 603,
                "branch": "feat/no",
                "base_branch": "main",
            }
        ],
        context={"scope": "medium", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=603,
        branch="feat/no",
        base_branch="main",
        fix_prompt="fix",
    )
    assert created is True and result is not None
    assert "scope_budget_utilization" not in (result.context or {})


@pytest.mark.asyncio
async def test_scope_budget_utilization_attached_to_review_spawn(db_session, mocker):
    """Review spawns also carry utilization so reviewers can weigh budget pressure."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 604,
                "branch": "feat/rv",
                "base_branch": "main",
                "prompt": "X",
            }
        ],
        context={
            "scope": "small",
            "_pr_changed_files": ["a.py", "b.py"],
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=604,
        branch="feat/rv",
        base_branch="main",
    )
    assert created is True and result is not None
    util = (result.context or {}).get("scope_budget_utilization")
    assert util is not None
    assert util["non_test_file_count"] == 2
    assert util["scope"] == "small"


# ── Graduated scope-pressure iteration cap ────────────────────────────────────


@pytest.mark.asyncio
async def test_scope_pressure_graduated_cap_at_mid_utilization(db_session, mocker):
    """Medium task at 80% (8/10 prod) gets a graduated cap well below per_chain_cap
    but no no-new-files prompt (below 90%)."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 701,
                "branch": "feat/gc1",
                "base_branch": "main",
            }
        ],
        context={
            "scope": "medium",
            "_pr_changed_files": [f"backend/app/p{i}.py" for i in range(8)]
            + ["backend/testing/test_a.py"],
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=701,
        branch="feat/gc1",
        base_branch="main",
        fix_prompt="Address review feedback",
    )
    assert created is True and result is not None
    ctx = result.context or {}
    # source iteration=0 → spawned iteration=1, base_remaining=11, 80% util
    # → round(11 * 0.20) = 2 remaining, hard_cap = 1 + 2 = 3
    assert ctx.get("_scope_budget_hard_cap") == 3
    # No no-new-files prompt below 90%
    payload = result.payloads[0] if result.payloads else {}
    assert "SCOPE BUDGET EXHAUSTED" not in (payload.get("prompt") or "")


@pytest.mark.asyncio
async def test_scope_pressure_floor_of_two_at_high_utilization(db_session, mocker):
    """At 80% the graduated formula floors at 2; at 100% it still floors at 2."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 702,
                "branch": "feat/gc2",
                "base_branch": "main",
            }
        ],
        context={
            "scope": "medium",
            "_pr_changed_files": [f"backend/app/p{i}.py" for i in range(8)],  # 8/10=80%
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=702,
        branch="feat/gc2",
        base_branch="main",
        fix_prompt="fix",
    )
    ctx = result.context or {}
    # iteration=1, base_remaining=11, 80% util → round(11*0.20)=2 → floor=2 →
    # hard_cap=1+2=3
    assert ctx.get("_scope_budget_hard_cap") == 3
    payload = result.payloads[0] if result.payloads else {}
    # 80% < 90% → no hard_prefix yet
    assert "SCOPE BUDGET EXHAUSTED" not in (payload.get("prompt") or "")


@pytest.mark.asyncio
async def test_scope_pressure_no_new_files_still_at_90_percent(db_session, mocker):
    """At ≥90% the no-new-files prompt still fires alongside the graduated cap."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 703,
                "branch": "feat/gc3",
                "base_branch": "main",
            }
        ],
        context={
            "scope": "medium",
            "_pr_changed_files": [
                f"backend/app/p{i}.py" for i in range(10)
            ],  # 10/10=100%
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=703,
        branch="feat/gc3",
        base_branch="main",
        fix_prompt="Address feedback",
    )
    ctx = result.context or {}
    # iteration=1, base_remaining=11, 100% util → round(0)=0 → floor=2 →
    # hard_cap=1+2=3
    assert ctx.get("_scope_budget_hard_cap") == 3
    payload = result.payloads[0] if result.payloads else {}
    assert "SCOPE BUDGET EXHAUSTED" in (payload.get("prompt") or "")


@pytest.mark.asyncio
async def test_scope_pressure_inactive_below_warn_threshold(db_session, mocker):
    """Below 70% utilization the graduated cap does not activate at all."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 704,
                "branch": "feat/gc4",
                "base_branch": "main",
            }
        ],
        context={
            "scope": "medium",
            "_pr_changed_files": [f"backend/app/p{i}.py" for i in range(4)],  # 4/7=57%
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=704,
        branch="feat/gc4",
        base_branch="main",
        fix_prompt="fix",
    )
    ctx = result.context or {}
    assert "_scope_budget_hard_cap" not in ctx
    payload = result.payloads[0] if result.payloads else {}
    assert "SCOPE BUDGET EXHAUSTED" not in (payload.get("prompt") or "")


@pytest.mark.asyncio
async def test_prior_hard_cap_propagates_while_under_pressure(db_session, mocker):
    """A prior hard cap on the source task is carried forward to the spawn when
    current utilization is still ≥ warn threshold."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 710,
                "branch": "feat/gc10",
                "base_branch": "main",
            }
        ],
        context={
            "scope": "medium",
            # 8/10 non-test = 80% (above warn threshold)
            "_pr_changed_files": [f"backend/app/p{i}.py" for i in range(8)]
            + ["backend/testing/test_a.py"],
            "_scope_budget_hard_cap": 2,  # tighter than any freshly-derived cap
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=710,
        branch="feat/gc10",
        base_branch="main",
        fix_prompt="fix",
    )
    ctx = result.context or {}
    # Prior cap (2) is tighter than freshly-derived (3), so min wins.
    assert ctx.get("_scope_budget_hard_cap") == 2


@pytest.mark.asyncio
async def test_prior_hard_cap_relaxes_when_utilization_drops(db_session, mocker):
    """A prior hard cap is NOT propagated once the PR file footprint has dropped
    below the warn threshold — the iteration restriction relaxes back."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 711,
                "branch": "feat/gc11",
                "base_branch": "main",
            }
        ],
        context={
            "scope": "medium",
            # 4/7 non-test = 57% — below warn threshold
            "_pr_changed_files": [f"backend/app/p{i}.py" for i in range(4)],
            "_scope_budget_hard_cap": 3,  # prior cap from a tighter earlier spawn
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=711,
        branch="feat/gc11",
        base_branch="main",
        fix_prompt="fix",
    )
    ctx = result.context or {}
    # Utilization below warn threshold → prior cap must NOT propagate.
    assert "_scope_budget_hard_cap" not in ctx


# ── filter_pipeline_duplicates: pre-dispatch dedup safety net ─────────────────


@pytest.mark.asyncio
async def test_pipeline_dedup_cancels_when_sibling_already_running(db_session, mocker):
    """A review/fix candidate is cancelled if another task for the same
    (pr_number, task_type) is already IN_PROGRESS. Sibling stays untouched."""
    from datetime import datetime, timezone
    from app.orchestrator_filters import filter_pipeline_duplicates

    broadcast_mock = mocker.AsyncMock()

    project = await _make_db_project(db_session)

    # Sibling that's already running on PR #501.
    running = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 501,
                "branch": "feat/x",
                "base_branch": "main",
            }
        ],
    )
    # Candidate — a duplicate review spawned past the TOCTOU window.
    candidate = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[
            {
                "type": "review",
                "pr_number": 501,
                "branch": "feat/x",
                "base_branch": "main",
            }
        ],
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.add(running)
    db_session.add(candidate)
    await db_session.commit()
    await db_session.refresh(running)
    await db_session.refresh(candidate)

    result = await filter_pipeline_duplicates(
        [candidate.id], [candidate], db_session, broadcast_mock
    )
    await db_session.commit()
    assert result == [], "Duplicate with running sibling must be filtered out"
    await db_session.refresh(candidate)
    assert candidate.status == TaskStatus.CANCELLED
    assert "Deduplicated" in (candidate.latest_status_message or "")
    # Cancelled candidate must carry an infra failure_category so rollup parents
    # don't get upgraded to CODE_ERROR by worst_child_failure_category.
    assert (candidate.context or {}).get("failure_category") == "AGENT_TIMEOUT"
    # Sibling must not be touched.
    await db_session.refresh(running)
    assert running.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_pipeline_dedup_keeps_oldest_queued_cancels_newer(db_session, mocker):
    """When all siblings are QUEUED, the oldest survives and newer ones are
    cancelled — no running sibling yet, so the pipeline still moves forward."""
    from datetime import datetime, timedelta, timezone
    from app.orchestrator_filters import filter_pipeline_duplicates

    broadcast_mock = mocker.AsyncMock()

    project = await _make_db_project(db_session)
    base_time = datetime.now(timezone.utc).replace(tzinfo=None)

    older = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[
            {"type": "fix", "pr_number": 502, "branch": "feat/y", "base_branch": "main"}
        ],
        created_at=base_time,
    )
    newer = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[
            {"type": "fix", "pr_number": 502, "branch": "feat/y", "base_branch": "main"}
        ],
        created_at=base_time + timedelta(seconds=5),
    )
    db_session.add(older)
    db_session.add(newer)
    await db_session.commit()
    await db_session.refresh(older)
    await db_session.refresh(newer)

    # Both are eligible in this dispatch batch.
    result = await filter_pipeline_duplicates(
        [older.id, newer.id], [older, newer], db_session, broadcast_mock
    )
    await db_session.commit()
    assert older.id in result, "Oldest QUEUED sibling must survive"
    assert newer.id not in result, "Newer duplicate must be filtered"
    await db_session.refresh(newer)
    assert newer.status == TaskStatus.CANCELLED
    assert (newer.context or {}).get("failure_category") == "AGENT_TIMEOUT"


@pytest.mark.asyncio
async def test_pipeline_dedup_sole_candidate_never_cancelled(db_session, mocker):
    """Invariant: if the candidate is the only task on its (pr, type), it
    must pass through — safety net must not stall a legitimate pipeline."""
    from app.orchestrator_filters import filter_pipeline_duplicates

    broadcast_mock = mocker.AsyncMock()

    project = await _make_db_project(db_session)
    candidate = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[
            {
                "type": "review",
                "pr_number": 503,
                "branch": "feat/z",
                "base_branch": "main",
            }
        ],
    )
    db_session.add(candidate)
    await db_session.commit()
    await db_session.refresh(candidate)

    result = await filter_pipeline_duplicates(
        [candidate.id], [candidate], db_session, broadcast_mock
    )
    assert result == [candidate.id]
    await db_session.refresh(candidate)
    assert candidate.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_pipeline_dedup_non_pipeline_task_passes_through(db_session, mocker):
    """Non-review/fix tasks (create, research, refactor, etc.) pass through
    unaffected even if they happen to share a pr_number field."""
    from app.orchestrator_filters import filter_pipeline_duplicates

    broadcast_mock = mocker.AsyncMock()

    project = await _make_db_project(db_session)
    candidate = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
    )
    db_session.add(candidate)
    await db_session.commit()
    await db_session.refresh(candidate)

    result = await filter_pipeline_duplicates(
        [candidate.id], [candidate], db_session, broadcast_mock
    )
    assert result == [candidate.id]


@pytest.mark.asyncio
async def test_pipeline_dedup_different_pr_numbers_pass_through(db_session, mocker):
    """Two review tasks on DIFFERENT PR numbers are not duplicates — both pass."""
    from app.orchestrator_filters import filter_pipeline_duplicates

    broadcast_mock = mocker.AsyncMock()

    project = await _make_db_project(db_session)
    a = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[
            {"type": "review", "pr_number": 601, "branch": "a", "base_branch": "main"}
        ],
    )
    b = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[
            {"type": "review", "pr_number": 602, "branch": "b", "base_branch": "main"}
        ],
    )
    db_session.add(a)
    db_session.add(b)
    await db_session.commit()
    await db_session.refresh(a)
    await db_session.refresh(b)

    result = await filter_pipeline_duplicates(
        [a.id, b.id], [a, b], db_session, broadcast_mock
    )
    assert set(result) == {a.id, b.id}


@pytest.mark.asyncio
async def test_pipeline_dedup_different_projects_same_pr_pass_through(
    db_session, mocker
):
    """Two review tasks sharing a pr_number but in DIFFERENT projects are not
    duplicates — the grouping key includes project_id. Guards against a regression
    where removing project_id from the key would cause cross-project dedup."""
    from app.orchestrator_filters import filter_pipeline_duplicates

    broadcast_mock = mocker.AsyncMock()

    project_a = await _make_db_project(db_session)
    project_b = await _make_db_project(db_session)
    assert project_a.id != project_b.id

    a = make_task(
        project_id=project_a.id,
        status=TaskStatus.QUEUED,
        payloads=[
            {
                "type": "review",
                "pr_number": 700,
                "branch": "feat/a",
                "base_branch": "main",
            }
        ],
    )
    b = make_task(
        project_id=project_b.id,
        status=TaskStatus.QUEUED,
        payloads=[
            {
                "type": "review",
                "pr_number": 700,
                "branch": "feat/b",
                "base_branch": "main",
            }
        ],
    )
    db_session.add(a)
    db_session.add(b)
    await db_session.commit()
    await db_session.refresh(a)
    await db_session.refresh(b)

    result = await filter_pipeline_duplicates(
        [a.id, b.id], [a, b], db_session, broadcast_mock
    )
    await db_session.commit()
    assert set(result) == {a.id, b.id}, (
        "Same PR number in different projects must not dedup"
    )
    await db_session.refresh(a)
    await db_session.refresh(b)
    assert a.status == TaskStatus.QUEUED
    assert b.status == TaskStatus.QUEUED


# ── _spawn_pipeline_task: supersession paths persist AGENT_TIMEOUT ─────────────


@pytest.mark.asyncio
async def test_spawn_pipeline_supersedes_stale_task_with_agent_timeout(
    db_session, mocker
):
    """A review/fix candidate older than PIPELINE_TASK_STALE_MINUTES with no
    heartbeat is superseded: status flips to CANCELLED and failure_category
    must be AGENT_TIMEOUT so rollup parents don't treat it as a code defect."""
    from datetime import timedelta
    from app.review_pipeline import PIPELINE_TASK_STALE_MINUTES, _utcnow

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "small"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Stale in-flight review with no heartbeat for > threshold.
    stale_started = _utcnow() - timedelta(minutes=PIPELINE_TASK_STALE_MINUTES + 5)
    stale_review = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 701,
                "branch": "feat/s",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        started_at=stale_started,
        last_heartbeat=None,
    )
    db_session.add(stale_review)

    # Source task driving a fresh review spawn on the same chain.
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 701,
                "branch": "feat/s",
                "base_branch": "main",
                "prompt": "Build X",
            }
        ],
        context={
            "scope": "small",
            "review_loop": {"iteration": 0, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(stale_review)
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=701,
        branch="feat/s",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    await db_session.commit()

    assert created is True and result is not None, (
        "Fresh review must spawn after supersession"
    )
    await db_session.refresh(stale_review)
    assert stale_review.status == TaskStatus.CANCELLED
    assert "Superseded" in (stale_review.latest_status_message or "")
    assert (stale_review.context or {}).get("failure_category") == "AGENT_TIMEOUT"


@pytest.mark.asyncio
async def test_spawn_fix_cancels_in_flight_review_with_agent_timeout(
    db_session, mocker
):
    """When a fix is spawned on a chain that has an in-flight review, the review
    is cancelled (snapshot is stale). The cancelled review must carry
    failure_category=AGENT_TIMEOUT so it doesn't poison rollup classification."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "small"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Fresh in-flight review (not stale — has a recent heartbeat).
    in_flight_review = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 702,
                "branch": "feat/r",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        started_at=_utcnow_for_test(),
        last_heartbeat=_utcnow_for_test(),
    )
    db_session.add(in_flight_review)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 702,
                "branch": "feat/r",
                "base_branch": "main",
                "prompt": "Build X",
            }
        ],
        context={
            "scope": "small",
            "review_loop": {"iteration": 0, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(in_flight_review)
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=702,
        branch="feat/r",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Resolve issues",
    )
    await db_session.commit()

    assert created is True and result is not None, (
        "Fresh fix must spawn after cancelling review"
    )
    await db_session.refresh(in_flight_review)
    assert in_flight_review.status == TaskStatus.CANCELLED
    assert "Superseded by fix" in (in_flight_review.latest_status_message or "")
    assert (in_flight_review.context or {}).get("failure_category") == "AGENT_TIMEOUT"


@pytest.mark.asyncio
async def test_fix_prompt_does_not_duplicate_prior_review_feedback(db_session, mocker):
    """Prior review feedback must NOT be prepended to the fix payload's prompt.

    The fix agent already consumes structured review_history from task context
    (with severity tags and file/line locations). Backend-side enrichment of the
    prompt with raw latest_status_message text from prior review tasks would
    duplicate the same feedback in a less useful form."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    # Simulate multiple prior completed review tasks for this PR — if the old
    # enrichment path still existed, their latest_status_message text would be
    # concatenated into the fix prompt.
    for i in range(3):
        prior = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[
                {
                    "type": "review",
                    "pr_number": 999,
                    "branch": "b",
                    "base_branch": "main",
                }
            ],
            latest_status_message=f"LEGACY-PRIOR-FEEDBACK-{i}",
        )
        db_session.add(prior)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {"type": "review", "pr_number": 999, "branch": "b", "base_branch": "main"}
        ],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    fix_prompt = "Address the flagged null check."
    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=999,
        branch="b",
        base_branch="main",
        fix_prompt=fix_prompt,
    )

    assert created is True and result is not None
    spawned_prompt = result.first_payload.get("prompt", "")
    # The fix prompt must match exactly — no prepended history section, no
    # raw prior feedback text from completed review tasks.
    assert spawned_prompt == fix_prompt, (
        f"Fix payload prompt should equal fix_prompt verbatim; got: {spawned_prompt!r}"
    )
    assert "Previous review feedback" not in spawned_prompt
    for i in range(3):
        assert f"LEGACY-PRIOR-FEEDBACK-{i}" not in spawned_prompt


def _utcnow_for_test():
    """Naive UTC timestamp matching app/time_utils.py _utcnow()."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── _is_pipeline_task_stale ───────────────────────────────────────────────────


class TestIsPipelineTaskStale:
    """Unit tests for the stale-task detection helper. No DB required."""

    def _task(self, *, last_heartbeat=None, started_at=None) -> Task:
        return make_task(last_heartbeat=last_heartbeat, started_at=started_at)

    def test_recent_heartbeat_is_not_stale(self):
        now = _utcnow_for_test()
        task = self._task(last_heartbeat=now - timedelta(minutes=5))
        assert _is_pipeline_task_stale(task) is False

    def test_old_heartbeat_is_stale(self):
        now = _utcnow_for_test()
        task = self._task(last_heartbeat=now - timedelta(minutes=30))
        assert _is_pipeline_task_stale(task) is True

    def test_queued_task_no_heartbeat_no_started_is_not_stale(self):
        task = self._task()
        assert _is_pipeline_task_stale(task) is False

    def test_no_heartbeat_recent_started_is_not_stale(self):
        now = _utcnow_for_test()
        task = self._task(started_at=now - timedelta(minutes=5))
        assert _is_pipeline_task_stale(task) is False

    def test_no_heartbeat_old_started_is_stale(self):
        now = _utcnow_for_test()
        task = self._task(started_at=now - timedelta(minutes=30))
        assert _is_pipeline_task_stale(task) is True

    def test_heartbeat_takes_precedence_over_started_at(self):
        now = _utcnow_for_test()
        # Recent heartbeat but very old started_at → not stale.
        task = self._task(
            last_heartbeat=now - timedelta(minutes=1),
            started_at=now - timedelta(hours=5),
        )
        assert _is_pipeline_task_stale(task) is False

    def test_boundary_heartbeat_just_past_threshold_is_stale(self):
        now = _utcnow_for_test()
        task = self._task(
            last_heartbeat=now
            - timedelta(minutes=PIPELINE_TASK_STALE_MINUTES, seconds=1)
        )
        assert _is_pipeline_task_stale(task) is True

    def test_boundary_heartbeat_just_inside_threshold_is_not_stale(self):
        now = _utcnow_for_test()
        task = self._task(
            last_heartbeat=now - timedelta(minutes=PIPELINE_TASK_STALE_MINUTES - 1)
        )
        assert _is_pipeline_task_stale(task) is False


# ── _feedback_token_set ───────────────────────────────────────────────────────


class TestFeedbackTokenSet:
    """Unit tests for the feedback tokenizer used by stuck-cycle detection."""

    def test_normal_sentence_tokenized(self):
        assert _feedback_token_set("The quick brown fox jumps") == {
            "the",
            "quick",
            "brown",
            "fox",
            "jumps",
        }

    def test_short_tokens_filtered(self):
        # "a" and "is" are shorter than 3 chars and must be dropped.
        assert _feedback_token_set("a cat is here") == {"cat", "here"}

    def test_tokens_lowercased(self):
        assert _feedback_token_set("FOO Bar BAZ") == {"foo", "bar", "baz"}

    def test_empty_string_returns_empty_set(self):
        assert _feedback_token_set("") == set()

    def test_punctuation_split_into_tokens(self):
        # Regex matches [a-z0-9_]+ so punctuation is treated as a separator.
        assert _feedback_token_set("foo, bar. baz!") == {"foo", "bar", "baz"}

    def test_underscores_kept_inside_tokens(self):
        assert _feedback_token_set("snake_case_var other") == {
            "snake_case_var",
            "other",
        }


# ── _is_test_file ─────────────────────────────────────────────────────────────


class TestIsTestFile:
    def test_testing_directory_slash(self):
        assert _is_test_file("backend/testing/test_foo.py") is True

    def test_tests_directory_slash(self):
        assert _is_test_file("pkg/tests/helpers.py") is True

    def test_testing_prefix_no_leading_slash(self):
        assert _is_test_file("testing/factories.py") is True

    def test_tests_prefix_no_leading_slash(self):
        assert _is_test_file("tests/unit.py") is True

    def test_test_prefix_filename(self):
        assert _is_test_file("backend/app/test_helpers.py") is True

    def test_underscore_test_py_suffix(self):
        assert _is_test_file("backend/app/module_test.py") is True

    def test_dot_test_in_filename(self):
        assert _is_test_file("admin-ui/src/foo.test.ts") is True

    def test_dot_spec_in_filename(self):
        assert _is_test_file("admin-ui/src/bar.spec.js") is True

    def test_regular_source_file(self):
        assert _is_test_file("backend/app/review_pipeline.py") is False

    def test_regular_frontend_file(self):
        assert _is_test_file("admin-ui/src/components/Foo.tsx") is False

    def test_case_insensitive(self):
        assert _is_test_file("Backend/TESTING/Foo.py") is True


# ── _compute_scope_budget_utilization ─────────────────────────────────────────


class TestComputeScopeBudgetUtilization:
    def test_none_changed_files_returns_none(self):
        assert _compute_scope_budget_utilization(None, "small") is None

    def test_empty_changed_files_returns_none(self):
        assert _compute_scope_budget_utilization([], "small") is None

    def test_unknown_scope_returns_none(self):
        assert (
            _compute_scope_budget_utilization(["backend/app/foo.py"], "bogus") is None
        )

    def test_small_scope_mixed_files(self):
        # small budget = 6 (total_files_max); 3 non-test files → 50%
        result = _compute_scope_budget_utilization(
            [
                "backend/app/a.py",
                "backend/app/b.py",
                "backend/app/c.py",
                "backend/testing/test_a.py",
                "backend/testing/test_b.py",
            ],
            "small",
        )
        assert result == {
            "scope": "small",
            "file_count": 5,
            "non_test_file_count": 3,
            "budget": 6,
            "percentage": 50,
        }

    def test_medium_scope_uses_production_files_max(self):
        # medium production_files_max = 10; 10 non-test → 100%
        files = [f"backend/app/m{i}.py" for i in range(10)] + [
            "backend/testing/test_m.py"
        ]
        result = _compute_scope_budget_utilization(files, "medium")
        assert result["budget"] == 10
        assert result["non_test_file_count"] == 10
        assert result["file_count"] == 11
        assert result["percentage"] == 100

    def test_all_test_files_zero_non_test(self):
        result = _compute_scope_budget_utilization(
            [
                "backend/testing/test_a.py",
                "admin-ui/src/foo.test.ts",
                "pkg/tests/b.py",
            ],
            "small",
        )
        assert result["non_test_file_count"] == 0
        assert result["file_count"] == 3
        assert result["percentage"] == 0

    def test_large_scope_budget(self):
        # large total_files_max = 20; 10 non-test → 50%
        files = [f"backend/app/l{i}.py" for i in range(10)]
        result = _compute_scope_budget_utilization(files, "large")
        assert result["budget"] == 20
        assert result["percentage"] == 50


# ── issue-churn detection ─────────────────────────────────────────────────────


class TestDetectIssueChurn:
    def _entry(self, issues):
        return {"iteration": 0, "feedback": "f", "issues": issues}

    def test_short_history_no_churn(self):
        hist = [self._entry([_issue("a")])]
        is_churn, counts = _detect_issue_churn(hist)  # noqa: RUF059
        assert is_churn is False

    def test_non_decreasing_distinct_issues_is_churn(self):
        # Counts 2, 2 with entirely different issues each iter → churn.
        hist = [
            self._entry([_issue("b1"), _issue("b2")]),
            self._entry([_issue("c1"), _issue("c2")]),
        ]
        is_churn, counts = _detect_issue_churn(hist)
        assert is_churn is True
        assert counts == [2, 2]

    def test_strictly_decreasing_not_churn(self):
        hist = [
            self._entry([_issue("b1"), _issue("b2")]),
            self._entry([_issue("c1")]),
        ]
        is_churn, _ = _detect_issue_churn(hist)
        assert is_churn is False

    def test_zero_in_window_not_churn(self):
        hist = [
            self._entry([]),
            self._entry([_issue("c1")]),
        ]
        is_churn, _ = _detect_issue_churn(hist)
        assert is_churn is False

    def test_oscillating_counts_is_churn(self):
        # Counts 3, 5 — rebounds to higher → non-decreasing → churn.
        hist = [
            self._entry([_issue(f"b{i}") for i in range(3)]),
            self._entry([_issue(f"c{i}") for i in range(5)]),
        ]
        is_churn, counts = _detect_issue_churn(hist)
        assert is_churn is True
        assert counts == [3, 5]

    def test_dip_not_churn(self):
        # Counts 4, 2 — final below first → net progress, not churn.
        hist = [
            self._entry([_issue(f"a{i}") for i in range(4)]),
            self._entry([_issue(f"b{i}") for i in range(2)]),
        ]
        is_churn, counts = _detect_issue_churn(hist)
        assert is_churn is False
        assert counts == [4, 2]

    def test_minor_issues_ignored(self):
        hist = [
            self._entry([_issue("b", "MINOR")]),
            self._entry([_issue("c", "MINOR")]),
        ]
        is_churn, counts = _detect_issue_churn(hist)
        assert is_churn is False
        assert counts == [0, 0]


class TestRepeatingIssues:
    def _entry(self, descriptions, severity="CRITICAL"):
        return {"issues": [_issue(d, severity) for d in descriptions]}

    def test_returns_descriptions_appearing_in_multiple_iterations(self):
        hist = [
            self._entry(["null pointer in foo", "unused import"]),
            self._entry(["null pointer in foo", "new bug"]),
            self._entry(["null pointer in foo", "another"]),
        ]
        out = _repeating_issue_descriptions(hist)
        assert "null pointer in foo" in out
        assert "unused import" not in out
        assert "another" not in out

    def test_ignores_minor(self):
        hist = [
            self._entry(["x"], "MINOR"),
            self._entry(["x"], "MINOR"),
        ]
        assert _repeating_issue_descriptions(hist) == []

    def test_empty_for_no_repeats(self):
        hist = [self._entry(["a"]), self._entry(["b"]), self._entry(["c"])]
        assert _repeating_issue_descriptions(hist) == []

    def test_format_section_empty_when_no_repeats(self):
        assert _format_repeating_issues_section([self._entry(["a"])]) == ""

    def test_format_section_includes_bullets(self):
        hist = [self._entry(["issue X"]), self._entry(["issue X"])]
        section = _format_repeating_issues_section(hist)
        assert "Repeating issues" in section
        assert "- issue X" in section


@pytest.mark.asyncio
async def test_issue_churn_injects_narrow_focus_and_reduces_cap(db_session, mocker):
    """When CRITICAL+MAJOR counts don't decrease across ISSUE_CHURN_WINDOW iterations
    (but the specific issues rotate, evading stuck-cycle detection), the spawned fix
    task should get a narrow-focus directive prefix and the per-chain cap should be
    reduced immediately on the first detection to curtail unproductive churn."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Build a review_history carried into the source task with 2 prior entries,
    # each having 2 CRITICAL issues but with distinct descriptions (no token
    # overlap with each other or current → evades stuck detection).
    prior_history = [
        {
            "iteration": 0,
            "feedback": "f0",
            "issues": [_issue("alpha widget broken"), _issue("beta gauge crash")],
        },
        {
            "iteration": 1,
            "feedback": "f1",
            "issues": [_issue("gamma router panic"), _issue("delta cache miss")],
        },
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 701,
                "branch": "b1",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 2,
                "original_task_id": str(original_task.id),
                "pr_number": 701,
                "branch": "b1",
                "base_branch": "main",
                "review_history": prior_history,
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Current iteration has 2 NEW CRITICAL issues (distinct tokens) → count 2,
    # matching prior counts → non-decreasing → churn.
    current_issues = [_issue("epsilon timer off"), _issue("zeta parser err")]

    result, spawned = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=701,
        branch="b1",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Please fix.",
        current_feedback="new feedback",
        current_issues=current_issues,
    )

    assert result is not None, "Churn must not block spawn (only stuck-cycle blocks)"
    assert spawned is True

    # Narrow-focus directive prefixed to the fix prompt.
    payload = result.first_payload or {}
    assert "ISSUE CHURN DETECTED" in (payload.get("prompt") or "")
    assert "Please fix." in payload["prompt"]

    # First churn detection now reduces the cap via the 1/2 rule.
    # medium baseline=12, iteration=3 (source had 2, incremented), remaining=9 → 3+9//2=7.
    ctx_cap = (result.context or {}).get("review_loop", {}).get("max_iterations")
    assert ctx_cap == 7, f"First churn detection should reduce cap to 7, got {ctx_cap}"


@pytest.mark.asyncio
async def test_first_churn_detection_reduces_cap(db_session, mocker):
    """First churn detection injects a narrow-focus directive and reduces the
    per-chain cap via the 1/2 rule to curtail unproductive churn early."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    prior_history = [
        {
            "iteration": 0,
            "feedback": "f0",
            "issues": [_issue("alpha widget broken"), _issue("beta gauge crash")],
        },
        {
            "iteration": 1,
            "feedback": "f1",
            "issues": [_issue("gamma router panic"), _issue("delta cache miss")],
        },
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 702,
                "branch": "b1",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 2,
                "original_task_id": str(original_task.id),
                "pr_number": 702,
                "branch": "b1",
                "base_branch": "main",
                "review_history": prior_history,
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    current_issues = [_issue("epsilon timer off"), _issue("zeta parser err")]
    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=702,
        branch="b1",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Please fix.",
        current_feedback="fb",
        current_issues=current_issues,
    )

    # medium scope → baseline 12; iteration=3; remaining=9 → 3+9//2=7.
    new_loop = (result.context or {}).get("review_loop", {})
    assert new_loop.get("max_iterations") == 7
    assert new_loop.get("churn_detections") == 1


@pytest.mark.asyncio
async def test_second_churn_detection_uses_one_half_reduction(db_session, mocker):
    """Second churn detection in the same fix cycle uses a 1/2 reduction:
    reduced_cap = iteration + (remaining // 2)."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    prior_history = [
        {
            "iteration": 0,
            "feedback": "f0",
            "issues": [_issue("alpha widget broken"), _issue("beta gauge crash")],
        },
        {
            "iteration": 1,
            "feedback": "f1",
            "issues": [_issue("gamma router panic"), _issue("delta cache miss")],
        },
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 703,
                "branch": "b1",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 2,
                "original_task_id": str(original_task.id),
                "pr_number": 703,
                "branch": "b1",
                "base_branch": "main",
                "review_history": prior_history,
                "churn_detections": 1,
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    current_issues = [_issue("epsilon timer off"), _issue("zeta parser err")]
    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=703,
        branch="b1",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Please fix.",
        current_feedback="fb",
        current_issues=current_issues,
    )

    # medium scope → baseline 12; iteration = 3; remaining = 9.
    # Second detection applies 1/2 rule: 3 + 9//2 = 7 (cap already reduced on
    # detection #1, so this exercises the subsequent-detection path).
    new_loop = (result.context or {}).get("review_loop", {})
    assert new_loop.get("max_iterations") == 7
    assert new_loop.get("churn_detections") == 2


@pytest.mark.asyncio
async def test_mid_budget_injects_narrow_focus_without_cap_reduction(
    db_session, mocker
):
    """When a fix iteration has consumed >= 50% of the per-chain cap but no
    churn has been detected, a softer narrow-focus directive should be
    prepended to the fix prompt without reducing the cap."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # One prior entry with MORE issues than current → counts strictly decrease
    # ([3, 1]) → no churn detected, so the mid-budget branch takes effect.
    prior_history = [
        {
            "iteration": 0,
            "feedback": "f0",
            "issues": [_issue("a"), _issue("b"), _issue("c")],
        },
    ]
    # scope=medium → baseline cap 12; iteration 6 is exactly 50% → mid-budget.
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 801,
                "branch": "b2",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 6,
                "original_task_id": str(original_task.id),
                "pr_number": 801,
                "branch": "b2",
                "base_branch": "main",
                "review_history": prior_history,
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, spawned = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=801,
        branch="b2",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Please fix.",
        current_feedback="new feedback",
        current_issues=[_issue("single issue")],
    )

    assert result is not None and spawned is True
    payload = result.first_payload or {}
    prompt = payload.get("prompt") or ""
    assert "over half of the fix iteration budget" in prompt
    assert "ISSUE CHURN DETECTED" not in prompt
    assert "Please fix." in prompt

    # Cap is NOT reduced by the mid-budget directive.
    from app.review_pipeline import _max_review_iterations

    baseline = _max_review_iterations(source_task)
    ctx_cap = (result.context or {}).get("review_loop", {}).get("max_iterations")
    assert ctx_cap == baseline, (
        f"Mid-budget directive must not reduce cap: baseline={baseline}, got={ctx_cap}"
    )


# ── direction_title propagation ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_direction_title_propagated_to_spawned_task(db_session, mocker):
    """When the source task's direction has text, _spawn_pipeline_task must inject
    direction_title (truncated to 80 chars) into the spawned task's context so
    that agent PR-body footers can display a human-readable direction label."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    direction_id = "dir-title-test"
    direction_text = "Reduce average review iteration count per merged PR per direction"

    project = await _make_db_project(db_session)
    project.directions = [
        {"id": direction_id, "text": direction_text, "status": "active"}
    ]
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 555,
                "branch": "feat/t",
                "base_branch": "main",
            }
        ],
        context={"scope": "small", "direction_id": direction_id},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, spawned = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=555,
        branch="feat/t",
        base_branch="main",
    )

    assert spawned is True and result is not None
    assert (result.context or {}).get("direction_title") == direction_text[:80], (
        f"direction_title must be propagated from direction.text[:80]; "
        f"got {(result.context or {}).get('direction_title')!r}"
    )


@pytest.mark.asyncio
async def test_direction_title_not_set_when_no_direction_text(db_session, mocker):
    """When the direction entry has no text, direction_title must not be injected
    into the spawned task context (omit rather than show empty string)."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    direction_id = "dir-no-text"
    project = await _make_db_project(db_session)
    project.directions = [{"id": direction_id, "status": "active"}]
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 556,
                "branch": "feat/u",
                "base_branch": "main",
            }
        ],
        context={"scope": "small", "direction_id": direction_id},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, spawned = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=556,
        branch="feat/u",
        base_branch="main",
    )

    assert spawned is True and result is not None
    assert "direction_title" not in (result.context or {}), (
        "direction_title must not be set when direction has no text"
    )


@pytest.mark.asyncio
async def test_scope_overflow_warning_propagates_to_review_context_and_prompt(
    db_session, mocker
):
    """When changed files exceed scope budget, review tasks should carry a
    machine-readable overflow signal and explicit coherence-vs-sprawl guidance."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 991,
                "branch": "feat/overflow",
                "base_branch": "main",
                "prompt": "Implement feature X",
            }
        ],
        context={
            "scope": "small",
            "_pr_changed_files": [
                "backend/app/a.py",
                "backend/app/b.py",
                "backend/app/c.py",
                "backend/app/d.py",
                "backend/app/e.py",
                "backend/app/f.py",
                "backend/app/g.py",
            ],
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, spawned = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=991,
        branch="feat/overflow",
        base_branch="main",
        original_prompt="Implement feature X",
    )

    assert spawned is True and result is not None
    ctx = result.context or {}
    assert ctx.get("scope_overflow_warned") is True
    overflow = ctx.get("scope_overflow_warning") or {}
    assert overflow.get("scope") == "small"
    assert overflow.get("threshold") == 6
    assert overflow.get("non_test_file_count") == 7
    assert (ctx.get("review_loop") or {}).get("scope_overflow_warned") is True

    payload = result.first_payload or {}
    op = payload.get("original_prompt") or ""
    assert "coherent, necessary unit" in op
    assert "scope sprawl" in op

    await db_session.refresh(source_task)
    source_ctx = source_task.context or {}
    assert source_ctx.get("scope_overflow_warned") is True
    assert (source_ctx.get("scope_overflow_warning") or {}).get("threshold") == 6


@pytest.mark.asyncio
async def test_scope_overflow_pr_signal_emitted_once(db_session, mocker):
    """Overflow label/comment should be emitted once, then suppressed on later spawns."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("ghs_test", "acme", "repo"),
    )
    label_mock = mocker.patch("app.github.add_issue_label", new_callable=AsyncMock)
    comment_mock = mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    overflow_files = [
        "backend/app/a.py",
        "backend/app/b.py",
        "backend/app/c.py",
        "backend/app/d.py",
        "backend/app/e.py",
        "backend/app/f.py",
        "backend/app/g.py",
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 992,
                "branch": "feat/overflow-once",
                "base_branch": "main",
                "prompt": "Implement feature Y",
            }
        ],
        context={
            "scope": "small",
            "_pr_changed_files": overflow_files,
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    first_result, first_spawned = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=992,
        branch="feat/overflow-once",
        base_branch="main",
        original_prompt="Implement feature Y",
    )
    assert first_spawned is True and first_result is not None
    assert label_mock.await_count == 1
    assert comment_mock.await_count == 1
    first_comment = comment_mock.await_args.args[4]
    assert "Scope overflow detected" in first_comment
    assert "- Declared scope: `small`" in first_comment
    assert "- Non-test files changed: `7`" in first_comment
    assert "- Scope budget: `6`" in first_comment
    for path in overflow_files:
        assert f"- `{path}`" in first_comment

    await db_session.refresh(source_task)
    assert (source_task.context or {}).get("scope_overflow_pr_signal_emitted") is True

    first_result.status = TaskStatus.COMPLETED
    db_session.add(first_result)
    await db_session.commit()

    source_task.status = TaskStatus.IN_PROGRESS
    source_task.context = {
        **(source_task.context or {}),
        "review_loop": {"iteration": 1},
    }
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    second_result, second_spawned = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=992,
        branch="feat/overflow-once",
        base_branch="main",
        original_prompt="Implement feature Y",
    )
    assert second_spawned is True and second_result is not None
    assert label_mock.await_count == 1
    assert comment_mock.await_count == 1


# ── post_merge_summary_comment ────────────────────────────────────────────────


class TestPostMergeSummaryComment:
    """Unit tests for post_merge_summary_comment — no DB required."""

    def _make_orig(
        self,
        *,
        created_by="ORCHESTRATOR",
        task_type="create",
        scope="small",
        provider=None,
        model=None,
        direction_id=None,
        direction_title=None,
    ) -> Task:
        ctx: dict = {}
        if scope:
            ctx["scope"] = scope
        if provider:
            ctx["llm_provider"] = provider
        if model:
            ctx["llm_model"] = model
        if direction_id:
            ctx["direction_id"] = direction_id
        if direction_title:
            ctx["direction_title"] = direction_title
        return make_task(
            created_by=created_by,
            payloads=[{"type": task_type}],
            context=ctx,
        )

    def _make_ci(self, *, iteration=0) -> Task:
        return make_task(
            context={"review_loop": {"iteration": iteration, "pr_number": 42}},
        )

    @pytest.mark.asyncio
    async def test_skips_user_created_task(self, mocker):
        mock_comment = mocker.patch(
            "app.github.create_pr_comment", new_callable=AsyncMock
        )
        orig = self._make_orig(created_by="USER")
        ci = self._make_ci(iteration=1)
        await post_merge_summary_comment(orig, ci, "tok", "owner", "repo", 42)
        mock_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_basic_summary_posted(self, mocker):
        posted: list[str] = []

        async def _fake_comment(tok, owner, repo, pr, body):
            posted.append(body)

        mocker.patch("app.github.create_pr_comment", side_effect=_fake_comment)
        mocker.patch("app.task_completion._estimated_task_cost_usd", return_value=0.05)

        orig = self._make_orig(
            scope="small",
            provider="anthropic",
            model="claude-sonnet-4-5",
            direction_id="dir-abc",
            direction_title="Add webhook enrichment",
        )
        ci = self._make_ci(iteration=2)
        await post_merge_summary_comment(orig, ci, "tok", "owner", "repo", 42)

        assert len(posted) == 1
        body = posted[0]
        assert "Blueprint" in body
        assert "`create`" in body
        assert "`small` scope" in body
        assert "2 review cycles" in body
        assert "~$0.05" in body
        assert "anthropic/claude-sonnet-4-5" in body
        assert "Add webhook enrichment" in body

    @pytest.mark.asyncio
    async def test_zero_iterations_omitted(self, mocker):
        posted: list[str] = []

        async def _fake_comment(tok, owner, repo, pr, body):
            posted.append(body)

        mocker.patch("app.github.create_pr_comment", side_effect=_fake_comment)
        mocker.patch("app.task_completion._estimated_task_cost_usd", return_value=None)

        orig = self._make_orig()
        ci = self._make_ci(iteration=0)
        await post_merge_summary_comment(orig, ci, "tok", "owner", "repo", 42)

        assert len(posted) == 1
        assert "cycle" not in posted[0]

    @pytest.mark.asyncio
    async def test_comment_error_is_swallowed(self, mocker):
        mocker.patch(
            "app.github.create_pr_comment", side_effect=RuntimeError("network")
        )
        mocker.patch("app.task_completion._estimated_task_cost_usd", return_value=None)
        orig = self._make_orig()
        ci = self._make_ci(iteration=1)
        # Should not raise
        await post_merge_summary_comment(orig, ci, "tok", "owner", "repo", 42)


# ── _is_cross_cutting_pr ──────────────────────────────────────────────────────


class TestIsCrossCuttingPr:
    """Pure unit tests — no DB required."""

    def _task(
        self, scope="small", changed_files=None, overflow_warned=False, diff_lines=None
    ):
        review_loop: dict = {}
        if overflow_warned:
            review_loop["scope_overflow_warned"] = True
        ctx: dict = {"scope": scope}
        if changed_files is not None:
            ctx["_pr_changed_files"] = changed_files
        if review_loop:
            ctx["review_loop"] = review_loop
        if diff_lines is not None:
            ctx["_pr_diff_summary"] = [{"added": diff_lines, "removed": 0}]
        return make_task(context=ctx)

    def test_gate_disabled_returns_false(self):
        import app.shared_state as ss

        original = ss.runtime_config.get("cross_cutting_gate_enabled")
        try:
            ss.runtime_config["cross_cutting_gate_enabled"] = False
            t = self._task(scope="large")
            ok, reason = _is_cross_cutting_pr(t)
            assert ok is False
            assert "disabled" in reason
        finally:
            ss.runtime_config["cross_cutting_gate_enabled"] = (
                original if original is not None else True
            )

    def test_large_scope_always_triggers(self):
        t = self._task(scope="large")
        ok, reason = _is_cross_cutting_pr(t)
        assert ok is True
        assert "large" in reason

    def test_medium_scope_all_three_modules_triggers(self):
        files = ["backend/app/foo.py", "agent/src/bar.ts", "admin-ui/src/baz.tsx"]
        t = self._task(scope="medium", changed_files=files)
        ok, reason = _is_cross_cutting_pr(t)
        assert ok is True
        assert "medium" in reason

    def test_medium_scope_two_modules_no_trigger(self):
        files = ["backend/app/foo.py", "agent/src/bar.ts"]
        t = self._task(scope="medium", changed_files=files)
        ok, _ = _is_cross_cutting_pr(t)
        assert ok is False

    def test_medium_scope_no_files_no_trigger(self):
        t = self._task(scope="medium", changed_files=[])
        ok, _ = _is_cross_cutting_pr(t)
        assert ok is False

    def test_overflow_warned_large_diff_triggers(self):
        t = self._task(scope="small", overflow_warned=True, diff_lines=200)
        ok, reason = _is_cross_cutting_pr(t)
        assert ok is True
        assert "overflow" in reason

    def test_overflow_warned_small_diff_no_trigger(self):
        t = self._task(scope="small", overflow_warned=True, diff_lines=50)
        ok, _ = _is_cross_cutting_pr(t)
        assert ok is False

    def test_overflow_not_warned_even_with_large_diff_no_trigger(self):
        t = self._task(scope="small", overflow_warned=False, diff_lines=500)
        ok, _ = _is_cross_cutting_pr(t)
        assert ok is False

    def test_small_scope_no_overflow_no_trigger(self):
        t = self._task(scope="small")
        ok, _ = _is_cross_cutting_pr(t)
        assert ok is False


# ── maybe_spawn_cross_cutting_safeguard ───────────────────────────────────────


@pytest.mark.asyncio
async def test_maybe_spawn_cross_cutting_safeguard_not_cross_cutting(
    db_session, mocker
):
    """Non-cross-cutting PRs return False without spawning anything."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig = make_task(
        project_id=project.id,
        context={"scope": "small"},
        payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    result = await maybe_spawn_cross_cutting_safeguard(
        orig, 99, "feat/x", "main", db_session
    )
    assert result is False
    assert orig.requires_approval is False
    assert (orig.context or {}).get("safeguard_task_id") is None


@pytest.mark.asyncio
async def test_maybe_spawn_cross_cutting_safeguard_spawns_for_large_scope(
    db_session, mocker
):
    """Large-scope PRs trigger a safeguard task and sets requires_approval."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig = make_task(
        project_id=project.id,
        context={"scope": "large"},
        payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    result = await maybe_spawn_cross_cutting_safeguard(
        orig, 42, "feat/x", "main", db_session
    )
    assert result is True
    assert orig.requires_approval is True
    assert (orig.context or {}).get("safeguard_task_id") is not None
    assert (orig.context or {}).get("safeguard_reason") == "scope=large"

    # Safeguard task must exist in DB
    safeguard_id = uuid.UUID((orig.context or {})["safeguard_task_id"])
    safeguard = await db_session.get(Task, safeguard_id)
    assert safeguard is not None
    assert safeguard.parent_task_id == orig.id


@pytest.mark.asyncio
async def test_maybe_spawn_cross_cutting_safeguard_idempotent(db_session, mocker):
    """If safeguard_task_id already set, second call is a no-op."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    existing_id = str(uuid.uuid4())
    orig = make_task(
        project_id=project.id,
        context={"scope": "large", "safeguard_task_id": existing_id},
        payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    result = await maybe_spawn_cross_cutting_safeguard(
        orig, 42, "feat/x", "main", db_session
    )
    assert result is False
    # safeguard_task_id unchanged
    assert (orig.context or {}).get("safeguard_task_id") == existing_id


# ── _maybe_release_safeguard_gate ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_safeguard_gate_noop_for_non_safeguard(db_session):
    """Task with no safeguard_for_task_id is a no-op."""
    from app.task_completion import _maybe_release_safeguard_gate

    t = make_task(context={})
    # Should not raise and should not touch DB
    await _maybe_release_safeguard_gate(t, [], db_session)


@pytest.mark.asyncio
async def test_release_safeguard_gate_maintains_on_critical_finding(db_session):
    """Gate stays on (requires_approval=True) when a critical finding is present."""
    from app.task_completion import _maybe_release_safeguard_gate

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig = make_task(project_id=project.id, requires_approval=True)
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    safeguard = make_task(
        project_id=project.id,
        context={"safeguard_for_task_id": str(orig.id), "safeguard_pr_number": 7},
    )
    findings = [{"severity": "critical", "description": "SQL injection"}]
    await _maybe_release_safeguard_gate(safeguard, findings, db_session)

    await db_session.refresh(orig)
    assert orig.requires_approval is True


@pytest.mark.asyncio
async def test_release_safeguard_gate_releases_on_low_findings(db_session):
    """Gate is released (requires_approval=False) when findings are low severity."""
    from app.task_completion import _maybe_release_safeguard_gate

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig = make_task(project_id=project.id, requires_approval=True)
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    safeguard = make_task(
        project_id=project.id,
        context={"safeguard_for_task_id": str(orig.id), "safeguard_pr_number": 7},
    )
    findings = [{"severity": "low", "description": "Unused import"}]
    await _maybe_release_safeguard_gate(safeguard, findings, db_session)

    await db_session.refresh(orig)
    assert orig.requires_approval is False


@pytest.mark.asyncio
async def test_release_safeguard_gate_sets_ci_waiting_when_evaluating(db_session):
    """When orig is EVALUATING, gate release sets ci_waiting=True."""
    from app.task_completion import _maybe_release_safeguard_gate

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig = make_task(
        project_id=project.id,
        requires_approval=True,
        status=TaskStatus.EVALUATING,
        context={"review_loop": {"iteration": 1}},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    safeguard = make_task(
        project_id=project.id,
        context={"safeguard_for_task_id": str(orig.id), "safeguard_pr_number": 7},
    )
    await _maybe_release_safeguard_gate(safeguard, [], db_session)

    await db_session.refresh(orig)
    assert orig.requires_approval is False
    assert (orig.context or {}).get("review_loop", {}).get("ci_waiting") is True
    assert (orig.context or {}).get("review_loop", {}).get("pr_number") == 7


# ── Cross-cutting gate: integration (spawn + merge-gate) ──────────────────────


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=False,
    reason="Pending _spawn_cross_cutting_safeguard_task call in _spawn_pipeline_task sibling subtask",
)
async def test_safeguard_spawned_and_merge_gate_set_for_large_scope_pr(
    db_session, mocker
):
    """When a review is triggered for a large-scope PR, the pipeline must spawn a
    safeguard (security_audit or qa) task AND set requires_approval=True on the
    original task so it cannot be merged until the safeguard approves.

    The agent spawn is mocked via _spawn_cross_cutting_safeguard_task; the test
    verifies both that the spawn was attempted and that the merge gate is armed."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch("app.github.add_issue_label", new_callable=AsyncMock)

    safeguard_mock = mocker.patch(
        "app.review_pipeline._spawn_cross_cutting_safeguard_task",
        create=True,
        new_callable=AsyncMock,
        return_value=None,
    )

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {"type": "create", "prompt": "Ship big feature", "base_branch": "main"}
        ],
        context={
            "scope": "large",
            "_pr_changed_files": [
                "backend/app/a.py",
                "agent/src/index.ts",
                "admin-ui/src/App.tsx",
            ],
        },
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 700,
                "branch": "feat/big",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "large",
            "review_loop": {"iteration": 0, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=700,
        branch="feat/big",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    (
        safeguard_mock.assert_awaited_once(),
        ("A cross-cutting safeguard task must be spawned for large-scope PRs"),
    )
    await db_session.refresh(original_task)
    assert original_task.requires_approval is True, (
        "Original task must be merge-gated (requires_approval=True) until the safeguard approves"
    )


@pytest.mark.asyncio
async def test_safeguard_not_spawned_for_small_scope_pr(db_session, mocker):
    """Small-scope PRs must never trigger the cross-cutting safeguard gate,
    even when they happen to touch multiple top-level modules."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    safeguard_mock = mocker.patch(
        "app.review_pipeline._spawn_cross_cutting_safeguard_task",
        create=True,
        new_callable=AsyncMock,
        return_value=None,
    )

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Small fix", "base_branch": "main"}],
        context={
            "scope": "small",
            "_pr_changed_files": [
                "backend/app/foo.py",
                "agent/src/index.ts",
                "admin-ui/src/App.tsx",
            ],
        },
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 701,
                "branch": "feat/small",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "review_loop": {"iteration": 0, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=701,
        branch="feat/small",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    (
        safeguard_mock.assert_not_awaited(),
        ("Small-scope PRs must never trigger the cross-cutting safeguard gate"),
    )


# ── compute_drift_signal ──────────────────────────────────────────────────────


class TestComputeDriftSignal:
    """Unit tests for the drift detector that powers the fix-agent trajectory
    section. drift_in lists files in the current diff that have never been
    planned or part of an APPROVE iteration; drift_out lists files that were
    expected (planned or approved) but are missing from the current diff."""

    def test_empty_history_falls_back_to_planned_only(self):
        result = compute_drift_signal(
            review_history=[],
            planned_files=["a.py", "b.py"],
            current_files=["a.py", "c.py"],
        )
        # c.py is in current but not planned and never approved → drift_in.
        assert [d["file"] for d in result["drift_in"]] == ["c.py"]
        # b.py was planned but is missing from current → drift_out.
        assert result["drift_out"] == ["b.py"]

    def test_approve_iteration_extends_acceptable_set(self):
        history = [
            {
                "iteration": 1,
                "verdict": "APPROVE",
                "changed_files": ["a.py", "extra.py"],
            }
        ]
        result = compute_drift_signal(
            review_history=history,
            planned_files=["a.py"],
            current_files=["a.py", "extra.py"],
        )
        # extra.py was approved at iter 1, so even though unplanned it is not drift.
        assert result["drift_in"] == []
        assert result["drift_out"] == []

    def test_drift_in_annotated_with_first_seen_and_flag_count(self):
        history = [
            {
                "iteration": 1,
                "verdict": "REQUEST_CHANGES",
                "changed_files": ["a.py", "alien.py"],
                "issues": [
                    {
                        "severity": "major",
                        "file": "alien.py",
                        "description": "out of plan",
                    },
                ],
            },
            {
                "iteration": 2,
                "verdict": "REQUEST_CHANGES",
                "changed_files": ["a.py", "alien.py"],
                "issues": [
                    {
                        "severity": "major",
                        "file": "alien.py",
                        "description": "still here",
                    },
                ],
            },
        ]
        result = compute_drift_signal(
            review_history=history,
            planned_files=["a.py"],
            current_files=["a.py", "alien.py"],
        )
        assert len(result["drift_in"]) == 1
        entry = result["drift_in"][0]
        assert entry["file"] == "alien.py"
        # alien.py first appeared in iter 1's changed_files snapshot.
        assert entry["first_appeared_iteration"] == 1
        # Two reviewers cited alien.py in their issue list.
        assert entry["flag_count"] == 2

    def test_pr_3435_replay(self):
        """Reproduces PR #3435: iter 1 APPROVE with test_reaper.py changes,
        iter 2 rebase-fix replaced those with main.py edit, iters 3+ flagged
        main.py. With trajectory data, drift_in = main.py (post-iter-1 leftover)
        and drift_out = test_reaper.py (lost from approved iter)."""
        history = [
            {
                "iteration": 1,
                "verdict": "APPROVE",
                "changed_files": ["backend/testing/test_reaper.py"],
            },
            {
                "iteration": 3,
                "verdict": "REQUEST_CHANGES",
                "changed_files": ["backend/app/main.py"],
                "issues": [
                    {
                        "severity": "major",
                        "file": "backend/app/main.py",
                        "description": "unrelated EXPECTED_SCHEMA_REVISION edit",
                    }
                ],
            },
            {
                "iteration": 5,
                "verdict": "REQUEST_CHANGES",
                "changed_files": ["backend/app/main.py"],
                "issues": [
                    {
                        "severity": "major",
                        "file": "backend/app/main.py",
                        "description": "unrelated EXPECTED_SCHEMA_REVISION edit",
                    }
                ],
            },
        ]
        result = compute_drift_signal(
            review_history=history,
            planned_files=["backend/testing/test_reaper.py"],
            current_files=["backend/app/main.py"],
        )
        drift_in_files = [d["file"] for d in result["drift_in"]]
        assert drift_in_files == ["backend/app/main.py"]
        assert result["drift_in"][0]["flag_count"] == 2
        assert result["drift_out"] == ["backend/testing/test_reaper.py"]

    def test_handles_malformed_entries(self):
        # Non-dict entries, missing fields, non-string filenames must not crash.
        history = [
            None,  # type: ignore[list-item]
            {"iteration": 1, "verdict": "APPROVE"},  # no changed_files
            {"iteration": 2, "changed_files": [None, 123, "ok.py"]},
        ]
        result = compute_drift_signal(
            review_history=history,  # type: ignore[arg-type]
            planned_files=["a.py"],
            current_files=["ok.py", "a.py"],
        )
        # ok.py was in iter 2's changed_files (REQUEST_CHANGES, no verdict
        # field defaults to non-APPROVE) so it's drift_in.
        assert [d["file"] for d in result["drift_in"]] == ["ok.py"]
        assert result["drift_out"] == []


# ── _failure_fingerprint_msg + repeat-fingerprint halt ────────────────────────


class TestFailureFingerprint:
    """Pure-function tests for the cross-iteration failure fingerprint.

    The fingerprint must be stable across iteration-specific noise (UUIDs,
    timestamps, /tmp paths, iter/attempt counters, agent-vm IDs) so two
    iterations hitting the same underlying blocker produce the same hash.
    """

    def test_empty_input_returns_empty(self):
        assert _failure_fingerprint_msg("") == ""
        assert _failure_fingerprint_msg(None) == ""
        assert _failure_fingerprint_msg("   ") == ""

    def test_identical_input_produces_identical_fp(self):
        msg = "[wrapper] ./check failed: viewport missing libatk-1.0.so.0"
        assert _failure_fingerprint_msg(msg) == _failure_fingerprint_msg(msg)

    def test_uuid_noise_stripped(self):
        a = "Task abc12345-def6-7890-1234-fedcba987654 failed: libatk missing"
        b = "Task 11111111-2222-3333-4444-555555555555 failed: libatk missing"
        assert _failure_fingerprint_msg(a) == _failure_fingerprint_msg(b)

    def test_timestamp_noise_stripped(self):
        a = "[2026-05-06T09:31:42] ./check failed: libatk missing"
        b = "[2026-05-06T10:15:03] ./check failed: libatk missing"
        assert _failure_fingerprint_msg(a) == _failure_fingerprint_msg(b)

    def test_tmp_path_noise_stripped(self):
        a = "wrote /tmp/check_resp_abc123 — libatk missing"
        b = "wrote /tmp/check_resp_xyz789 — libatk missing"
        assert _failure_fingerprint_msg(a) == _failure_fingerprint_msg(b)

    def test_iter_counter_stripped(self):
        a = "iter=3 ./check failed: libatk missing"
        b = "iter=12 ./check failed: libatk missing"
        assert _failure_fingerprint_msg(a) == _failure_fingerprint_msg(b)

    def test_agent_vm_id_stripped(self):
        a = "VM agent-vm-abc123 failed: libatk missing"
        b = "VM agent-vm-zzzfff failed: libatk missing"
        assert _failure_fingerprint_msg(a) == _failure_fingerprint_msg(b)

    def test_distinct_failures_have_distinct_fp(self):
        a = "./check failed: libatk-1.0.so.0 missing"
        b = "./check failed: ruff returned 1474 errors"
        assert _failure_fingerprint_msg(a) != _failure_fingerprint_msg(b)

    def test_returns_12_hex_chars(self):
        fp = _failure_fingerprint_msg("any non-empty input")
        assert len(fp) == 12
        assert all(c in "0123456789abcdef" for c in fp)


@pytest.mark.asyncio
async def test_repeat_fingerprint_halts_chain_and_classifies_blocked(
    db_session, mocker
):
    """Two same-type FAILED tasks in the same chain with identical failure
    fingerprints → next spawn returns (None, False), the most-recent FAILED
    is reclassified as BLOCKED_PRECONDITION, the chain's original task is
    marked requires_approval=True, ProjectMessage posted to strategic."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    common_failure = "[wrapper] ./check failed: error while loading shared libraries: libatk-1.0.so.0"

    # Two FAILED fixes in same chain on same PR with the same fingerprint.
    earlier_fix = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 901,
                "branch": "fix/901",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "review_loop": {
                "iteration": 2,
                "original_task_id": str(original_task.id),
                "pr_number": 901,
            },
        },
        latest_status_message=f"[2026-05-06T09:11:42] iter=2 vm=agent-vm-aaa111 {common_failure}",
    )
    most_recent_fix = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 901,
                "branch": "fix/901",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "review_loop": {
                "iteration": 3,
                "original_task_id": str(original_task.id),
                "pr_number": 901,
            },
        },
        latest_status_message=f"[2026-05-06T09:42:08] iter=3 vm=agent-vm-bbb222 {common_failure}",
    )
    await _persist(db_session, earlier_fix, most_recent_fix)

    # Source is most_recent_fix being handed off (FAILED here, but the halt
    # fires regardless of source.status — see also EVALUATING test below).
    result, was_created = await _spawn_pipeline_task(
        most_recent_fix,
        "review",
        db_session,
        pr_number=901,
        branch="fix/901",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    assert result is None
    assert was_created is False

    # most_recent_fix gets reclassified.
    await db_session.refresh(most_recent_fix)
    ctx = most_recent_fix.context or {}
    assert ctx.get("failure_category") == "BLOCKED_PRECONDITION"
    assert ctx.get("blocked_repeat_fingerprint")
    assert ctx.get("blocked_repeat_prior_task_id") == str(earlier_fix.id)
    msg = (most_recent_fix.latest_status_message or "").lstrip().lower()
    assert msg.startswith("blocker:")

    # source_task gets the spawn-reason marker.
    ctx_src = most_recent_fix.context or {}
    assert ctx_src.get(SPAWN_REASON_KEY) == SPAWN_REASON_BLOCKED_REPEAT

    # Original task gets requires_approval=True (mirrors stuck-cycle).
    await db_session.refresh(original_task)
    assert original_task.requires_approval is True
    assert "repeat-fingerprint" in (original_task.latest_status_message or "").lower()

    # ProjectMessage posted to strategic so it can decide the next move.
    from app.models import MessageType, ProjectMessage

    pms = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.message_type == MessageType.BLOCKER,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(pms) == 1
    assert pms[0].from_type == "system"
    assert pms[0].context.get("trigger") == "repeat_fingerprint_halt"
    assert pms[0].context.get("pr_number") == 901
    assert pms[0].context.get("source_task_id") == str(most_recent_fix.id)
    assert pms[0].context.get("prior_task_id") == str(earlier_fix.id)


@pytest.mark.asyncio
async def test_repeat_fingerprint_halts_when_source_is_evaluating(db_session, mocker):
    """Production handoff path: agent_status.py transitions FAILED → EVALUATING
    before calling _spawn_pipeline_task. The halt must still fire because the
    signal lives in chain history (pr_pipeline_tasks), not in source.status."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    common_fp_msg = (
        "[wrapper] ./check failed: ruff returned 1474 errors in unrelated files"
    )
    f1 = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 911,
                "branch": "fix/911",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        latest_status_message=f"[2026-05-06T08:14:11] iter=1 {common_fp_msg}",
    )
    f2 = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 911,
                "branch": "fix/911",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        latest_status_message=f"[2026-05-06T09:42:08] iter=2 {common_fp_msg}",
    )
    # Source is now EVALUATING (handoff after FAILED-with-open-PR). Its
    # latest_status_message is the boilerplate handoff line, NOT the failure.
    source_evaluating = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[
            {
                "type": "fix",
                "pr_number": 911,
                "branch": "fix/911",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        latest_status_message="Reported FAILED with open PR #911; handed off to review pipeline (CODE_ERROR)",
    )
    await _persist(db_session, f1, f2, source_evaluating)

    result, _ = await _spawn_pipeline_task(
        source_evaluating,
        "review",
        db_session,
        pr_number=911,
        branch="fix/911",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    # Halt must fire even though source.status is EVALUATING.
    assert result is None

    # f2 (most recent FAILED) reclassified.
    await db_session.refresh(f2)
    assert (f2.context or {}).get("failure_category") == "BLOCKED_PRECONDITION"

    # Original task marked.
    await db_session.refresh(original_task)
    assert original_task.requires_approval is True


@pytest.mark.asyncio
async def test_distinct_fingerprints_do_not_halt_chain(db_session, mocker):
    """Two FAILED fix tasks with DIFFERENT failure fingerprints — the chain
    continues normally (no halt, no reclassification, no requires_approval)."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    earlier_fix = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 902,
                "branch": "fix/902",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        latest_status_message="[wrapper] ./check failed: TypeError in foo.ts:42",
    )
    most_recent_fix = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 902,
                "branch": "fix/902",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        latest_status_message="[wrapper] ./check failed: ReferenceError in bar.ts:17",
    )
    await _persist(db_session, earlier_fix, most_recent_fix)

    await _spawn_pipeline_task(
        most_recent_fix,
        "review",
        db_session,
        pr_number=902,
        branch="fix/902",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    await db_session.refresh(most_recent_fix)
    assert (most_recent_fix.context or {}).get(
        "failure_category"
    ) != "BLOCKED_PRECONDITION"
    assert "blocked_repeat_fingerprint" not in (most_recent_fix.context or {})

    await db_session.refresh(original_task)
    assert original_task.requires_approval is False


@pytest.mark.asyncio
async def test_completed_source_task_skips_repeat_check(db_session, mocker):
    """Repeat-fingerprint halt must NOT fire when source_task is COMPLETED.
    A successful task means the chain IS making progress, even if prior
    iterations failed identically — halting here would be a false positive."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Two prior FAILED fixes with identical fingerprints, but the source task
    # COMPLETED — the halt path must NOT fire.
    fp_msg = "./check failed: libatk missing"
    prior_a = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 903,
                "branch": "fix/903",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        latest_status_message=fp_msg,
    )
    prior_b = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 903,
                "branch": "fix/903",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        latest_status_message=fp_msg,
    )
    source_completed = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 903,
                "branch": "fix/903",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "review_loop": {
                "iteration": 4,
                "original_task_id": str(original_task.id),
                "pr_number": 903,
            },
        },
    )
    await _persist(db_session, prior_a, prior_b, source_completed)

    await _spawn_pipeline_task(
        source_completed,
        "review",
        db_session,
        pr_number=903,
        branch="fix/903",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    await db_session.refresh(source_completed)
    ctx = source_completed.context or {}
    assert ctx.get("failure_category") != "BLOCKED_PRECONDITION"
    assert ctx.get(SPAWN_REASON_KEY) != SPAWN_REASON_BLOCKED_REPEAT

    await db_session.refresh(original_task)
    assert original_task.requires_approval is False

    # Prior FAILED tasks must NOT be reclassified.
    await db_session.refresh(prior_a)
    await db_session.refresh(prior_b)
    assert (prior_a.context or {}).get("failure_category") != "BLOCKED_PRECONDITION"
    assert (prior_b.context or {}).get("failure_category") != "BLOCKED_PRECONDITION"


@pytest.mark.asyncio
async def test_repeat_fingerprint_halt_is_idempotent(db_session, mocker):
    """Calling _spawn_pipeline_task twice for the same chain after a halt has
    already fired must NOT create a second BLOCKER ProjectMessage or re-mutate
    the failed task — `blocked_repeat_fingerprint` in context is the dedup key."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    common_failure = "[wrapper] ./check failed: libatk missing"
    f1 = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 921,
                "branch": "fix/921",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        latest_status_message=common_failure,
    )
    f2 = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 921,
                "branch": "fix/921",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        latest_status_message=common_failure,
    )
    await _persist(db_session, f1, f2)

    # First call fires the halt, posts message #1.
    await _spawn_pipeline_task(
        f2,
        "review",
        db_session,
        pr_number=921,
        branch="fix/921",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    # Second call must NOT post another message.
    await _spawn_pipeline_task(
        f2,
        "review",
        db_session,
        pr_number=921,
        branch="fix/921",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    from app.models import MessageType, ProjectMessage

    pms = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.message_type == MessageType.BLOCKER,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(pms) == 1, (
        "Repeat-fingerprint halt must be idempotent — only one BLOCKER message per chain"
    )


@pytest.mark.asyncio
async def test_only_one_failure_does_not_halt(db_session, mocker):
    """Halt requires TWO same-type FAILED tasks in chain. One isn't enough."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    only_failure = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 904,
                "branch": "fix/904",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        latest_status_message="./check failed: libatk missing",
    )
    await _persist(db_session, only_failure)

    await _spawn_pipeline_task(
        only_failure,
        "review",
        db_session,
        pr_number=904,
        branch="fix/904",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    await db_session.refresh(only_failure)
    assert (only_failure.context or {}).get(
        "failure_category"
    ) != "BLOCKED_PRECONDITION"
    await db_session.refresh(original_task)
    assert original_task.requires_approval is False


# ---------------------------------------------------------------------------
# build_ci_failure_fix_prompt() — conflict-aware CI-failure fix prompt.
#
# Regression guard for PR #5587 (2026-05-23): the CI-failure fix prompt in
# task_lifecycle.py used to say "Fix only the build/lint/test errors ... don't
# change logic, just fix CI failures" with no rebase step, so a PR that was
# ALSO conflicting never got its conflict resolved — the fix chased CI while
# the conflict outlived the global iteration cap and the PR auto-closed.
# ---------------------------------------------------------------------------

from app.review_pipeline import build_ci_failure_fix_prompt


def test_ci_failure_fix_prompt_is_conflict_aware():
    prompt = build_ci_failure_fix_prompt(
        pr_number=5587,
        base_branch="automain",
        ci_errors="FAILED testing/test_foo.py::test_bar\nProcess completed with exit code 1.",
    )
    # Must instruct a rebase against base BEFORE chasing CI, and conflict resolution.
    assert "git rebase origin/automain" in prompt
    assert "conflict" in prompt.lower()
    # Must carry the PR + base + CI errors through.
    assert "#5587" in prompt
    assert "test_foo.py::test_bar" in prompt
    # Must NOT reintroduce the misdirecting language that caused #5587.
    assert "don't change logic, just fix CI failures" not in prompt
    assert "Fix only the build/lint/test errors" not in prompt


def test_ci_failure_fix_prompt_check_runs_hint_optional():
    with_owner = build_ci_failure_fix_prompt(
        pr_number=1,
        base_branch="main",
        ci_errors="boom",
        owner="acme",
        repo="widgets",
    )
    assert "acme/widgets" in with_owner
    without_owner = build_ci_failure_fix_prompt(
        pr_number=1, base_branch="main", ci_errors="boom"
    )
    assert "check-runs" not in without_owner

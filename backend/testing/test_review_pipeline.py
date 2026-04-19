"""Unit and integration tests for review_pipeline.py.

Covers:
  1. _max_review_iterations() — scope/fix_reason caps (pure unit, no DB).
  2. _spawn_pipeline_task() — global iteration cap (DB-backed).
  3. _spawn_pipeline_task() — per-chain iteration cap (DB-backed).
  4. _spawn_pipeline_task() — stuck-cycle detection (DB-backed).
  5. Edge cases: empty review_history, issues without severity,
     zero issues in current iteration.
"""
import uuid
from datetime import timedelta

import pytest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.models import Project, Task, TaskStatus
from app.review_pipeline import (
    ISSUE_CHURN_WINDOW,
    MAX_REVIEW_FIX_ITERATIONS,
    PIPELINE_TASK_STALE_MINUTES,
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
    _repeating_issue_descriptions,
    _format_repeating_issues_section,
    _feedback_jaccard,
    _feedback_token_set,
    _is_pipeline_task_stale,
    _is_test_file,
    _max_review_iterations,
    _spawn_pipeline_task,
    should_auto_approve_small_diff,
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
        assert _max_review_iterations(self._task(scope="small", fix_reason="ci_failure")) == 8

    def test_ci_failure_overrides_medium_scope(self):
        assert _max_review_iterations(self._task(scope="medium", fix_reason="ci_failure")) == 8

    def test_ci_failure_overrides_large_scope(self):
        assert _max_review_iterations(self._task(scope="large", fix_reason="ci_failure")) == 8

    def test_other_fix_reason_does_not_override(self):
        """fix_reason other than 'ci_failure' is ignored; scope applies normally."""
        assert _max_review_iterations(self._task(scope="small", fix_reason="code_review")) == 10

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
        assert _max_review_iterations(self._task(scope="small"), direction_data=None) == 10

    def test_ci_failure_not_affected_by_direction_data(self):
        """ci_failure cap (8) is not reduced even for a bad direction."""
        history = [{"prs_merged": 0, "prs_rejected": 9}]
        d = self._direction_data(history)
        assert _max_review_iterations(
            self._task(scope="small", fix_reason="ci_failure"), direction_data=d
        ) == 8

    def test_no_history_data_no_reduction(self):
        """Empty history means acceptance_rate=None → no reduction."""
        d = self._direction_data([])
        assert _max_review_iterations(self._task(scope="small"), direction_data=d) == 10

    # ── Retry carry-forward: _prior_review_iterations_consumed ──────────────

    def test_prior_iterations_reduce_cap(self):
        """Retry clones carry forward prior iteration count; half is deducted."""
        t = make_task(context={"scope": "medium", "_prior_review_iterations_consumed": 8})
        # medium cap 12, minus 8//2 = 4 → 8
        assert _max_review_iterations(t) == 8

    def test_prior_iterations_floor_at_three(self):
        """Heavy prior usage cannot drive the cap below 3."""
        t = make_task(context={"scope": "small", "_prior_review_iterations_consumed": 100})
        assert _max_review_iterations(t) == 3

    def test_prior_iterations_zero_no_reduction(self):
        t = make_task(context={"scope": "medium", "_prior_review_iterations_consumed": 0})
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

    clone = (await db_session.execute(
        select(Task).where(
            Task.parent_task_id == parent.id,
            Task.status == TaskStatus.QUEUED,
        )
    )).scalars().first()
    assert clone is not None
    assert (clone.context or {}).get("_prior_review_iterations_consumed") == 8
    # medium cap 12, minus 8//2 = 4 → 8
    assert _max_review_iterations(clone) == 8


# ── should_auto_approve_small_diff ────────────────────────────────────────────


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
        skip, reason = should_auto_approve_small_diff(t, None)
        assert skip is True
        assert "auto-approve" in reason

    def test_non_small_scope_rejected(self):
        t = self._task(
            scope="medium",
            files=["app/foo.py"],
            diff=[{"file": "app/foo.py", "added": 5, "removed": 0}],
        )
        skip, _ = should_auto_approve_small_diff(t, None)
        assert skip is False

    def test_non_eligible_type_rejected(self):
        t = self._task(
            type_="plan",
            files=["app/foo.py"],
            diff=[{"file": "app/foo.py", "added": 5, "removed": 0}],
        )
        skip, _ = should_auto_approve_small_diff(t, None)
        assert skip is False

    def test_fix_type_eligible(self):
        t = self._task(
            type_="fix",
            files=["app/foo.py"],
            diff=[{"file": "app/foo.py", "added": 5, "removed": 0}],
        )
        skip, _ = should_auto_approve_small_diff(t, None)
        assert skip is True

    def test_too_many_lines_rejected(self):
        t = self._task(
            files=["app/foo.py"],
            diff=[{"file": "app/foo.py", "added": 100, "removed": 0}],
        )
        skip, reason = should_auto_approve_small_diff(t, None)
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
        skip, _ = should_auto_approve_small_diff(t, None)
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
        skip, _ = should_auto_approve_small_diff(t, None)
        assert skip is True

    def test_missing_diff_summary_rejected(self):
        t = self._task(files=["app/a.py"])
        skip, reason = should_auto_approve_small_diff(t, None)
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
        skip, reason = should_auto_approve_small_diff(t, {"research_history": []})
        assert skip is False
        assert "acceptance_rate" in reason

    def test_disabled_via_zero_max_lines(self, mocker):
        from app.shared_state import runtime_config
        mocker.patch.dict(runtime_config, {"auto_approve_max_lines": 0})
        t = self._task(
            files=["app/a.py"],
            diff=[{"file": "app/a.py", "added": 1, "removed": 0}],
        )
        skip, reason = should_auto_approve_small_diff(t, None)
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
    mocker.patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock)
    alert_mock = mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock, return_value=("tok", "o", "r"),
    )
    close_mock = mocker.patch("app.github.close_pull_request", new_callable=AsyncMock, return_value=True)
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
        payloads=[{"type": "review", "pr_number": 101, "branch": "feat/x", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
        context={"scope": "small", "review_loop": {"iteration": 0,
                                                    "original_task_id": str(original_task.id)}},
    )
    db_session.add(source_task)

    # 10 completed review tasks for PR #101 — reaches the global cap.
    for _ in range(10):
        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review", "pr_number": 101, "branch": "feat/x", "base_branch": "main"}],
        )
        db_session.add(t)

    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=101, branch="feat/x", base_branch="main",
        original_task_id=str(original_task.id),
    )

    assert result is None, "Global cap (10 iterations, small scope) should block spawn"

    await db_session.refresh(original_task)
    assert original_task.status == TaskStatus.FAILED, (
        f"Global cap must transition the root task to FAILED, got {original_task.status}"
    )
    assert (original_task.context or {}).get("failure_detail") == "review_cap_exhausted", (
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
    mocker.patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock, return_value=("tok", "o", "r"),
    )
    mocker.patch("app.github.close_pull_request", new_callable=AsyncMock, return_value=True)
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
            payloads=[{"type": "review", "pr_number": pr_number,
                       "branch": "feat/sym", "base_branch": "main",
                       "original_task_id": str(original_task.id)}],
            context={"scope": "small",
                     "review_loop": {"iteration": iteration,
                                     "original_task_id": str(original_task.id)}},
        )
        db_session.add(source_task)
        for _ in range(completed_count):
            db_session.add(make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "review", "pr_number": pr_number,
                           "branch": "feat/sym", "base_branch": "main"}],
            ))
        await db_session.commit()
        await db_session.refresh(source_task)

        result, _ = await _spawn_pipeline_task(
            source_task, "fix", db_session,
            pr_number=pr_number, branch="feat/sym", base_branch="main",
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
        assert root.requires_approval is True, f"{label} cap must preserve requires_approval"
        msg = (root.latest_status_message or "").lower()
        assert "manual review" in msg, f"{label} cap message missing 'manual review': {msg!r}"
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
        payloads=[{"type": "review", "pr_number": 103, "branch": "feat/x", "base_branch": "main"}],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)

    # 20 FAILED review tasks, all infra-class — cap is 10 for small scope,
    # but none of these should count.
    for fc in ["PROVISION_FAILURE", "RATE_LIMIT", "AGENT_TIMEOUT", "EXTERNAL_API"] * 5:
        t = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            payloads=[{"type": "review", "pr_number": 103, "branch": "feat/x", "base_branch": "main"}],
            context={"failure_category": fc},
        )
        db_session.add(t)

    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=103, branch="feat/x", base_branch="main",
        fix_prompt="Fix",
    )
    assert result is not None, "Infra-class FAILED tasks must not count toward the cap"
    assert result.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_archived_tasks_dont_count_toward_global_cap(db_session, mocker):
    """Archived tasks must not count — this is how reset-iteration-cap works."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "review", "pr_number": 104, "branch": "feat/x", "base_branch": "main"}],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)

    # 15 archived COMPLETED tasks — would exceed cap if counted.
    for _ in range(15):
        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review", "pr_number": 104, "branch": "feat/x", "base_branch": "main"}],
            archived=True,
        )
        db_session.add(t)

    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=104, branch="feat/x", base_branch="main",
        fix_prompt="Fix",
    )
    assert result is not None, "Archived tasks must not count toward the cap"


@pytest.mark.asyncio
async def test_code_error_failed_tasks_do_count_toward_global_cap(db_session, mocker):
    """CODE_ERROR failures ARE real review iterations and must count — otherwise
    a broken PR could loop forever."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "review", "pr_number": 105, "branch": "feat/x", "base_branch": "main"}],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)

    # 10 CODE_ERROR-failed tasks — reaches small-scope cap of 10.
    for _ in range(10):
        t = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            payloads=[{"type": "review", "pr_number": 105, "branch": "feat/x", "base_branch": "main"}],
            context={"failure_category": "CODE_ERROR"},
        )
        db_session.add(t)

    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=105, branch="feat/x", base_branch="main",
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
        payloads=[{"type": "review", "pr_number": 102, "branch": "feat/y", "base_branch": "main"}],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)

    # Only 5 completed tasks — below the small-scope cap of 10.
    for _ in range(5):
        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review", "pr_number": 102, "branch": "feat/y", "base_branch": "main"}],
        )
        db_session.add(t)

    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=102, branch="feat/y", base_branch="main",
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
    mocker.patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock)
    alert_mock = mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock, return_value=("tok", "o", "r"),
    )
    close_mock = mocker.patch("app.github.close_pull_request", new_callable=AsyncMock, return_value=True)
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
        payloads=[{"type": "review", "pr_number": 103, "branch": "feat/z", "base_branch": "main"}],
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
        source_task, "fix", db_session,
        pr_number=103, branch="feat/z", base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Still broken",
    )

    assert result is None, "Per-chain cap should block the fix spawn"

    await db_session.refresh(original_task)
    assert original_task.status == TaskStatus.FAILED, (
        f"Per-chain cap must transition the original task to FAILED, got {original_task.status}"
    )
    assert (original_task.context or {}).get("failure_detail") == "review_cap_exhausted", (
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
    mocker.patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock, return_value=("tok", "o", "r"),
    )
    mocker.patch("app.github.close_pull_request", new_callable=AsyncMock, return_value=True)
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
        payloads=[{"type": "review", "pr_number": 777, "branch": "feat/rec", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
        context={"scope": "small", "direction_id": direction_id,
                 "review_loop": {"iteration": 10,
                                 "original_task_id": str(original_task.id)}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=777, branch="feat/rec", base_branch="main",
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
async def test_cap_exhaustion_records_remediation_failure_on_direction(db_session, mocker):
    """Cap exhaustion on a remediation create task must stamp the linked direction
    with a failed outcome. Without this call, a premature 'merged' recorded on PR
    creation would persist even though the PR was auto-closed."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock, return_value=("tok", "o", "r"),
    )
    mocker.patch("app.github.close_pull_request", new_callable=AsyncMock, return_value=True)
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
        payloads=[{"type": "review", "pr_number": 888, "branch": "feat/rec",
                   "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
        context={
            "scope": "small",
            "direction_id": direction_id,
            "diagnosis_task_id": "diag-cap",
            "review_loop": {"iteration": 10,
                            "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=888, branch="feat/rec", base_branch="main",
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
    mocker.patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock)
    alert_mock = mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock, return_value=("tok", "o", "r"),
    )
    close_mock = mocker.patch("app.github.close_pull_request", new_callable=AsyncMock, return_value=True)
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
            payloads=[{"type": "review", "pr_number": 555, "branch": "feat/idem", "base_branch": "main",
                       "original_task_id": str(original_task.id)}],
            context={"scope": "small",
                     "review_loop": {"iteration": 0,
                                     "original_task_id": str(original_task.id)}},
        )
        db_session.add(source_task)
        await db_session.commit()
        return await _spawn_pipeline_task(
            source_task, "fix", db_session,
            pr_number=555, branch="feat/idem", base_branch="main",
            original_task_id=str(original_task.id),
        )

    for _ in range(10):
        db_session.add(make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review", "pr_number": 555, "branch": "feat/idem", "base_branch": "main"}],
        ))
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
            payloads=[{"type": "review", "pr_number": 200 + (1 if scope == "small" else 2),
                       "branch": "b", "base_branch": "main"}],
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
            source, "fix", db_session,
            pr_number=source.first_payload["pr_number"],
            branch="b", base_branch="main",
            original_task_id=str(original.id),
            fix_prompt="Fix it",
        )

        if should_block:
            assert result is None, f"scope={scope!r} at iteration=11 should be blocked (cap=10)"
        else:
            assert result is not None, f"scope={scope!r} at iteration=11 should be allowed (cap=12)"


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
        payloads=[{"type": "create", "pr_number": 301, "branch": "b1", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 301,
                "branch": "b1",
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": "prev feedback", "issues": prior_issues},
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
        source_task, "fix", db_session,
        pr_number=301, branch="b1", base_branch="main",
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
        payloads=[{"type": "create", "pr_number": 305, "branch": "b5", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 305,
                "branch": "b5",
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": "two handlers", "issues": prior_issues},
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
        source_task, "fix", db_session,
        pr_number=305, branch="b5", base_branch="main",
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

    prior_issues = [_issue("Error handling code missing in payment processor", "CRITICAL")]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create", "pr_number": 306, "branch": "b6", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 306,
                "branch": "b6",
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": "needs error handling", "issues": prior_issues},
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
        source_task, "fix", db_session,
        pr_number=306, branch="b6", base_branch="main",
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
        payloads=[{"type": "create", "pr_number": 302, "branch": "b2", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
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
        _issue("Memory leak in cache cleanup", "CRITICAL"),   # repeats
        _issue("TLS handshake fails on reconnect", "CRITICAL"),
        _issue("Race condition in queue dispatch", "MAJOR"),
    ]

    result, _ = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=302, branch="b2", base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Some things improved",
        current_feedback="Some things improved",
        current_issues=current_issues,
    )

    assert result is not None, "33% repeat rate is below threshold — spawn should proceed"
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
    prior_issues = [
        _issue(f"structural issue {i}", "CRITICAL") for i in range(5)
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        # Source type 'refactor' → 75% threshold.
        payloads=[{"type": "refactor", "pr_number": 303, "branch": "b3", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 303,
                "branch": "b3",
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": "structural concerns", "issues": prior_issues},
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
        source_task, "fix", db_session,
        pr_number=303, branch="b3", base_branch="main",
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
        payloads=[{"type": "refactor", "pr_number": 304, "branch": "b4", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 304,
                "branch": "b4",
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": "arch concerns", "issues": prior_issues},
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
        source_task, "fix", db_session,
        pr_number=304, branch="b4", base_branch="main",
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
        payloads=[{"type": "refactor", "pr_number": 305, "branch": "b5", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 305,
                "branch": "b5",
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": "layer concerns", "issues": prior_issues},
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
        source_task, "fix", db_session,
        pr_number=305, branch="b5", base_branch="main",
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
        payloads=[{"type": "create", "pr_number": 401, "branch": "b10", "base_branch": "main"}],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "pr_number": 401,
                "branch": "b10",
                "base_branch": "main",
                "review_history": [],   # empty — no prior entries
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    current_issues = [_issue("some bug", "CRITICAL")]

    result, _ = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=401, branch="b10", base_branch="main",
        fix_prompt="Fix the bug",
        current_feedback="Fix the bug",
        current_issues=current_issues,
    )

    # Empty history → new_review_history has 1 entry → len < 2 → no stuck detection.
    assert result is not None, "Empty review_history should not trigger stuck-cycle detection"
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
        payloads=[{"type": "create", "pr_number": 402, "branch": "b11", "base_branch": "main"}],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "pr_number": 402,
                "branch": "b11",
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": "prior feedback", "issues": prior_issues},
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Zero current CRITICAL/MAJOR issues → no stuck cycle possible.
    result, _ = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=402, branch="b11", base_branch="main",
        fix_prompt="All fixed now",
        current_feedback="All fixed now",
        current_issues=[],  # no issues in this iteration
    )

    assert result is not None, "Zero current issues must not trigger stuck-cycle detection"
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
        payloads=[{"type": "create", "pr_number": 403, "branch": "b12", "base_branch": "main"}],
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
        {"description": "formatting nit", "severity": "MINOR"},      # not CRITICAL/MAJOR
        {"description": "missing docstring"},                          # no severity
        {"description": "another style suggestion", "severity": "minor"},  # case insensitive check
    ]

    result, _ = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=403, branch="b12", base_branch="main",
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
        payloads=[{"type": "create", "pr_number": 404, "branch": "b13", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
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
        source_task, "fix", db_session,
        pr_number=404, branch="b13", base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Leak still present",
        current_feedback="Leak still present",
        current_issues=current_issues,
    )

    assert result is None, "100% repeat of MAJOR issues should trigger stuck-cycle detection"

    await db_session.refresh(original_task)
    assert original_task.requires_approval is True


# ── Stuck-cycle detection: feedback-text fallback ─────────────────────────────


def test_feedback_jaccard_unit():
    """_feedback_jaccard returns intersection/union over normalized tokens (≥3 chars)."""
    assert _feedback_jaccard("", "anything") == 0.0
    assert _feedback_jaccard("a b c", "a b c") == 0.0  # all tokens <3 chars → empty sets
    # Identical content (after lowercasing) → 1.0
    assert _feedback_jaccard("Null pointer in handler", "null POINTER in HANDLER") == 1.0
    # Disjoint content → 0.0
    assert _feedback_jaccard("memory leak", "race condition") == 0.0
    # Partial overlap: {"missing", "error", "handling"} ∩ {"missing", "error", "checks"}
    # → 2 / 4 = 0.5
    assert _feedback_jaccard("missing error handling", "missing error checks") == 0.5


@pytest.mark.asyncio
async def test_stuck_cycle_text_fallback_blocks_when_feedback_repeats(db_session, mocker):
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
        payloads=[{"type": "create", "pr_number": 601, "branch": "b20", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
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
        source_task, "fix", db_session,
        pr_number=601, branch="b20", base_branch="main",
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
        payloads=[{"type": "create", "pr_number": 602, "branch": "b21", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
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
        source_task, "fix", db_session,
        pr_number=602, branch="b21", base_branch="main",
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
async def test_stuck_cycle_text_fallback_when_only_one_side_lacks_issues(db_session, mocker):
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
        payloads=[{"type": "create", "pr_number": 603, "branch": "b22", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
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
        source_task, "fix", db_session,
        pr_number=603, branch="b22", base_branch="main",
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
async def test_stuck_cycle_text_fallback_skipped_when_feedback_empty(db_session, mocker):
    """When structured issues are absent AND one side has empty feedback, the
    fallback cannot compute similarity and must safely allow the spawn."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create", "pr_number": 604, "branch": "b23", "base_branch": "main"}],
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
        source_task, "fix", db_session,
        pr_number=604, branch="b23", base_branch="main",
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


def test_stuck_cycle_medium_thresholds_lower_than_default():
    """Tripwire: medium-scope thresholds must sit below the defaults so that
    higher-complexity tasks bail out of stuck cycles earlier."""
    assert STUCK_CYCLE_THRESHOLD_MEDIUM == 0.50
    assert STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_MEDIUM == 0.68
    assert STUCK_CYCLE_TEXT_THRESHOLD_MEDIUM == 0.65
    assert STUCK_CYCLE_THRESHOLD_MEDIUM < STUCK_CYCLE_THRESHOLD_DEFAULT
    assert STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_MEDIUM == STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_DEFAULT
    assert STUCK_CYCLE_TEXT_THRESHOLD_MEDIUM < STUCK_CYCLE_TEXT_THRESHOLD_DEFAULT


async def _run_stuck_scope_case(
    db_session, mocker, *, scope: str, pr_number: int
):
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

    # 6 of 11 current CRITICAL issues repeat → ~54.5% exact-match, clears the
    # 0.50 medium threshold but stays below the 0.55 default. New issues share
    # no tokens with prior, keeping Jaccard below both rephrased thresholds.
    prior_issues = [_issue(f"alpha issue {i}", "CRITICAL") for i in range(6)]
    branch = f"b-{scope}"
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create", "pr_number": pr_number, "branch": branch,
                   "base_branch": "main", "original_task_id": str(original_task.id)}],
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
        source_task, "fix", db_session,
        pr_number=pr_number, branch=branch, base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Partial",
        current_feedback="Partial",
        current_issues=current_issues,
    )
    await db_session.refresh(original_task)
    return result, original_task


@pytest.mark.asyncio
async def test_stuck_cycle_medium_scope_triggers_at_55pct(db_session, mocker):
    """Medium-scope tasks trigger the stuck cycle at ~54.5% exact-match repeat."""
    result, original_task = await _run_stuck_scope_case(
        db_session, mocker, scope="medium", pr_number=701
    )
    assert result is None, "Medium scope must stuck-detect at the lower 0.50 threshold"
    assert original_task.requires_approval is True


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
        payloads=[{"type": "create", "pr_number": 401, "branch": "b1", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 401,
                "branch": "b1",
                "base_branch": "main",
                "review_history": [
                    {"iteration": 0, "feedback": "prev feedback", "issues": prior_issues},
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
        source_task, "fix", db_session,
        pr_number=401, branch="b1", base_branch="main",
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
async def test_stuck_cycle_rephrased_fallback_ignores_distinct_targets(db_session, mocker):
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
        payloads=[{"type": "create", "pr_number": 402, "branch": "b2", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
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
        source_task, "fix", db_session,
        pr_number=402, branch="b2", base_branch="main",
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
        payloads=[{"type": "refactor", "pr_number": 403, "branch": "b3", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
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
        source_task, "fix", db_session,
        pr_number=403, branch="b3", base_branch="main",
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
        payloads=[{"type": "create", "pr_number": 501, "branch": "feat/ctx",
                   "base_branch": "main", "prompt": "Build X"}],
        context={
            "scope": "small",
            "direction_id": direction_id,
            "related_files": related_files,
            "parent_context": parent_context,
            "architecture_context": "Some architecture note",
            "_is_rollup_review": True,   # internal flag — must NOT propagate
            "_rollup_retries": 2,          # internal flag — must NOT propagate
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=501, branch="feat/ctx", base_branch="main",
        fix_prompt="Address the review feedback",
    )

    assert created is True
    assert result is not None

    ctx = result.context or {}
    assert ctx.get("direction_id") == direction_id, "direction_id must propagate to spawned task"
    assert ctx.get("related_files") == related_files, "related_files must propagate to spawned task"
    assert ctx.get("parent_context") == parent_context, "parent_context must propagate to spawned task"
    assert ctx.get("scope") == "small", "scope must propagate to spawned task"
    assert ctx.get("architecture_context") == "Some architecture note", (
        "non-private strategic context should also propagate"
    )

    # Internal flags must not leak into the spawned task's context.
    assert "_is_rollup_review" not in ctx, "underscore-prefixed internal flags must not propagate"
    assert "_rollup_retries" not in ctx, "underscore-prefixed internal flags must not propagate"

    # review_loop is overridden with the pipeline's own iteration state, not carried verbatim.
    assert ctx.get("review_loop", {}).get("iteration") == 1
    assert ctx.get("review_loop", {}).get("pr_number") == 501


@pytest.mark.asyncio
async def test_self_review_outcome_propagates_to_spawned_review(db_session, mocker):
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
        payloads=[{"type": "create", "pr_number": 777, "branch": "feat/sr",
                   "base_branch": "main", "prompt": "Build X"}],
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
        source_task, "review", db_session,
        pr_number=777, branch="feat/sr", base_branch="main",
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
        payloads=[{"type": "review", "pr_number": 444, "branch": "feat/fp",
                   "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
        context={
            "scope": "small",
            "review_loop": {"iteration": 1,
                            "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=444, branch="feat/fp", base_branch="main",
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
        payloads=[{"type": "create", "pr_number": 778, "branch": "feat/sr2",
                   "base_branch": "main", "prompt": "Build Y"}],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task, "review", db_session,
        pr_number=778, branch="feat/sr2", base_branch="main",
    )

    assert created is True
    assert result is not None
    ctx = result.context or {}
    assert "_self_review_outcome" not in ctx


# ── Scope-budget utilization on spawned fix ───────────────────────────────────


@pytest.mark.asyncio
async def test_scope_budget_utilization_attached_to_fix_from_source_files(db_session, mocker):
    """When the source task carries _pr_changed_files, the spawned fix task's
    context exposes a scope_budget_utilization block with a non-test count
    compared against the scope's file budget."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "review", "pr_number": 601, "branch": "feat/sb",
                   "base_branch": "main"}],
        context={
            "scope": "medium",
            "_pr_changed_files": [
                "backend/app/a.py", "backend/app/b.py", "backend/app/c.py",
                "backend/app/d.py", "backend/app/e.py",
                "backend/testing/test_a.py", "backend/testing/test_b.py",
            ],
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=601, branch="feat/sb", base_branch="main",
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
            "_pr_changed_files": ["backend/app/a.py", "backend/app/b.py",
                                  "backend/testing/test_a.py"],
        },
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "review", "pr_number": 602, "branch": "feat/fb",
                   "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
        context={"scope": "small",
                 "review_loop": {"iteration": 0,
                                 "original_task_id": str(original_task.id)}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=602, branch="feat/fb", base_branch="main",
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
async def test_scope_budget_utilization_absent_when_no_changed_files(db_session, mocker):
    """No _pr_changed_files anywhere → no utilization block emitted (can't bluff)."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "review", "pr_number": 603, "branch": "feat/no",
                   "base_branch": "main"}],
        context={"scope": "medium", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=603, branch="feat/no", base_branch="main",
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
        payloads=[{"type": "create", "pr_number": 604, "branch": "feat/rv",
                   "base_branch": "main", "prompt": "X"}],
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
        source_task, "review", db_session,
        pr_number=604, branch="feat/rv", base_branch="main",
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
        payloads=[{"type": "review", "pr_number": 701, "branch": "feat/gc1",
                   "base_branch": "main"}],
        context={
            "scope": "medium",
            "_pr_changed_files": [
                f"backend/app/p{i}.py" for i in range(8)
            ] + ["backend/testing/test_a.py"],
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=701, branch="feat/gc1", base_branch="main",
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
        payloads=[{"type": "review", "pr_number": 702, "branch": "feat/gc2",
                   "base_branch": "main"}],
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
        source_task, "fix", db_session,
        pr_number=702, branch="feat/gc2", base_branch="main",
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
        payloads=[{"type": "review", "pr_number": 703, "branch": "feat/gc3",
                   "base_branch": "main"}],
        context={
            "scope": "medium",
            "_pr_changed_files": [f"backend/app/p{i}.py" for i in range(10)],  # 10/10=100%
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=703, branch="feat/gc3", base_branch="main",
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
        payloads=[{"type": "review", "pr_number": 704, "branch": "feat/gc4",
                   "base_branch": "main"}],
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
        source_task, "fix", db_session,
        pr_number=704, branch="feat/gc4", base_branch="main",
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
        payloads=[{"type": "review", "pr_number": 710, "branch": "feat/gc10",
                   "base_branch": "main"}],
        context={
            "scope": "medium",
            # 8/10 non-test = 80% (above warn threshold)
            "_pr_changed_files": [
                f"backend/app/p{i}.py" for i in range(8)
            ] + ["backend/testing/test_a.py"],
            "_scope_budget_hard_cap": 2,  # tighter than any freshly-derived cap
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=710, branch="feat/gc10", base_branch="main",
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
        payloads=[{"type": "review", "pr_number": 711, "branch": "feat/gc11",
                   "base_branch": "main"}],
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
        source_task, "fix", db_session,
        pr_number=711, branch="feat/gc11", base_branch="main",
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
        payloads=[{"type": "review", "pr_number": 501, "branch": "feat/x", "base_branch": "main"}],
    )
    # Candidate — a duplicate review spawned past the TOCTOU window.
    candidate = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "review", "pr_number": 501, "branch": "feat/x", "base_branch": "main"}],
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
        payloads=[{"type": "fix", "pr_number": 502, "branch": "feat/y", "base_branch": "main"}],
        created_at=base_time,
    )
    newer = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "fix", "pr_number": 502, "branch": "feat/y", "base_branch": "main"}],
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
        payloads=[{"type": "review", "pr_number": 503, "branch": "feat/z", "base_branch": "main"}],
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
        payloads=[{"type": "review", "pr_number": 601, "branch": "a", "base_branch": "main"}],
    )
    b = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "review", "pr_number": 602, "branch": "b", "base_branch": "main"}],
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
async def test_pipeline_dedup_different_projects_same_pr_pass_through(db_session, mocker):
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
        payloads=[{"type": "review", "pr_number": 700, "branch": "feat/a", "base_branch": "main"}],
    )
    b = make_task(
        project_id=project_b.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "review", "pr_number": 700, "branch": "feat/b", "base_branch": "main"}],
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
    assert set(result) == {a.id, b.id}, "Same PR number in different projects must not dedup"
    await db_session.refresh(a)
    await db_session.refresh(b)
    assert a.status == TaskStatus.QUEUED
    assert b.status == TaskStatus.QUEUED


# ── _spawn_pipeline_task: supersession paths persist AGENT_TIMEOUT ─────────────


@pytest.mark.asyncio
async def test_spawn_pipeline_supersedes_stale_task_with_agent_timeout(db_session, mocker):
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
        payloads=[{"type": "review", "pr_number": 701, "branch": "feat/s",
                   "base_branch": "main", "original_task_id": str(original_task.id)}],
        started_at=stale_started,
        last_heartbeat=None,
    )
    db_session.add(stale_review)

    # Source task driving a fresh review spawn on the same chain.
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create", "pr_number": 701, "branch": "feat/s",
                   "base_branch": "main", "prompt": "Build X"}],
        context={"scope": "small", "review_loop": {"iteration": 0,
                                                    "original_task_id": str(original_task.id)}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(stale_review)
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task, "review", db_session,
        pr_number=701, branch="feat/s", base_branch="main",
        original_task_id=str(original_task.id),
    )
    await db_session.commit()

    assert created is True and result is not None, "Fresh review must spawn after supersession"
    await db_session.refresh(stale_review)
    assert stale_review.status == TaskStatus.CANCELLED
    assert "Superseded" in (stale_review.latest_status_message or "")
    assert (stale_review.context or {}).get("failure_category") == "AGENT_TIMEOUT"


@pytest.mark.asyncio
async def test_spawn_fix_cancels_in_flight_review_with_agent_timeout(db_session, mocker):
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
        payloads=[{"type": "review", "pr_number": 702, "branch": "feat/r",
                   "base_branch": "main", "original_task_id": str(original_task.id)}],
        started_at=_utcnow_for_test(),
        last_heartbeat=_utcnow_for_test(),
    )
    db_session.add(in_flight_review)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create", "pr_number": 702, "branch": "feat/r",
                   "base_branch": "main", "prompt": "Build X"}],
        context={"scope": "small", "review_loop": {"iteration": 0,
                                                    "original_task_id": str(original_task.id)}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(in_flight_review)
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=702, branch="feat/r", base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Resolve issues",
    )
    await db_session.commit()

    assert created is True and result is not None, "Fresh fix must spawn after cancelling review"
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
            payloads=[{"type": "review", "pr_number": 999, "branch": "b", "base_branch": "main"}],
            latest_status_message=f"LEGACY-PRIOR-FEEDBACK-{i}",
        )
        db_session.add(prior)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "review", "pr_number": 999, "branch": "b", "base_branch": "main"}],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    fix_prompt = "Address the flagged null check."
    result, created = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=999, branch="b", base_branch="main",
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
            last_heartbeat=now - timedelta(minutes=PIPELINE_TASK_STALE_MINUTES, seconds=1)
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
            _compute_scope_budget_utilization(["backend/app/foo.py"], "bogus")
            is None
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
        hist = [self._entry([_issue("a")]), self._entry([_issue("b")])]
        is_churn, counts = _detect_issue_churn(hist)
        assert is_churn is False

    def test_non_decreasing_distinct_issues_is_churn(self):
        # Counts 2, 2, 2 with entirely different issues each iter → churn.
        hist = [
            self._entry([_issue("a1"), _issue("a2")]),
            self._entry([_issue("b1"), _issue("b2")]),
            self._entry([_issue("c1"), _issue("c2")]),
        ]
        is_churn, counts = _detect_issue_churn(hist)
        assert is_churn is True
        assert counts == [2, 2, 2]

    def test_strictly_decreasing_not_churn(self):
        hist = [
            self._entry([_issue("a1"), _issue("a2"), _issue("a3")]),
            self._entry([_issue("b1"), _issue("b2")]),
            self._entry([_issue("c1")]),
        ]
        is_churn, _ = _detect_issue_churn(hist)
        assert is_churn is False

    def test_zero_in_window_not_churn(self):
        hist = [
            self._entry([_issue("a1")]),
            self._entry([]),
            self._entry([_issue("c1")]),
        ]
        is_churn, _ = _detect_issue_churn(hist)
        assert is_churn is False

    def test_oscillating_counts_is_churn(self):
        # Counts 5, 3, 5 — dip then rebound to original → zero net progress.
        hist = [
            self._entry([_issue(f"a{i}") for i in range(5)]),
            self._entry([_issue(f"b{i}") for i in range(3)]),
            self._entry([_issue(f"c{i}") for i in range(5)]),
        ]
        is_churn, counts = _detect_issue_churn(hist)
        assert is_churn is True
        assert counts == [5, 3, 5]

    def test_dip_then_partial_recover_not_churn(self):
        # Counts 4, 2, 3 — final below first → net progress, not churn.
        hist = [
            self._entry([_issue(f"a{i}") for i in range(4)]),
            self._entry([_issue(f"b{i}") for i in range(2)]),
            self._entry([_issue(f"c{i}") for i in range(3)]),
        ]
        is_churn, counts = _detect_issue_churn(hist)
        assert is_churn is False
        assert counts == [4, 2, 3]

    def test_minor_issues_ignored(self):
        hist = [
            self._entry([_issue("a", "MINOR")]),
            self._entry([_issue("b", "MINOR")]),
            self._entry([_issue("c", "MINOR")]),
        ]
        is_churn, counts = _detect_issue_churn(hist)
        assert is_churn is False
        assert counts == [0, 0, 0]


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
    task should get a narrow-focus directive prefix. On the first detection the cap
    is not reduced so the directive has budget to take effect."""
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
        {"iteration": 0, "feedback": "f0",
         "issues": [_issue("alpha widget broken"), _issue("beta gauge crash")]},
        {"iteration": 1, "feedback": "f1",
         "issues": [_issue("gamma router panic"), _issue("delta cache miss")]},
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create", "pr_number": 701, "branch": "b1", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
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
        source_task, "fix", db_session,
        pr_number=701, branch="b1", base_branch="main",
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

    # First churn detection: cap unchanged so the narrow-focus directive has
    # iteration budget to take effect.
    ctx_cap = (result.context or {}).get("review_loop", {}).get("max_iterations")
    from app.review_pipeline import _max_review_iterations
    baseline = _max_review_iterations(source_task)
    assert ctx_cap == baseline, (
        f"First churn detection should leave cap at baseline {baseline}, got {ctx_cap}"
    )


@pytest.mark.asyncio
async def test_first_churn_detection_does_not_reduce_cap(db_session, mocker):
    """First churn detection injects a narrow-focus directive but leaves the
    per-chain cap unchanged so the directive has budget to take effect."""
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
        {"iteration": 0, "feedback": "f0",
         "issues": [_issue("alpha widget broken"), _issue("beta gauge crash")]},
        {"iteration": 1, "feedback": "f1",
         "issues": [_issue("gamma router panic"), _issue("delta cache miss")]},
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create", "pr_number": 702, "branch": "b1", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 2,
                "original_task_id": str(original_task.id),
                "pr_number": 702, "branch": "b1", "base_branch": "main",
                "review_history": prior_history,
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    current_issues = [_issue("epsilon timer off"), _issue("zeta parser err")]
    result, _ = await _spawn_pipeline_task(
        source_task, "fix", db_session,
        pr_number=702, branch="b1", base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Please fix.",
        current_feedback="fb", current_issues=current_issues,
    )

    # medium scope → baseline 12; first detection leaves cap unchanged.
    from app.review_pipeline import _max_review_iterations
    baseline = _max_review_iterations(source_task)
    new_loop = (result.context or {}).get("review_loop", {})
    assert new_loop.get("max_iterations") == baseline
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
        {"iteration": 0, "feedback": "f0",
         "issues": [_issue("alpha widget broken"), _issue("beta gauge crash")]},
        {"iteration": 1, "feedback": "f1",
         "issues": [_issue("gamma router panic"), _issue("delta cache miss")]},
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create", "pr_number": 703, "branch": "b1", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 2,
                "original_task_id": str(original_task.id),
                "pr_number": 703, "branch": "b1", "base_branch": "main",
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
        source_task, "fix", db_session,
        pr_number=703, branch="b1", base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Please fix.",
        current_feedback="fb", current_issues=current_issues,
    )

    # medium scope → baseline 12; iteration = 3; remaining = 9.
    # Second detection: 3 + 9//2 = 7 (vs 9 under 1/3 rule).
    new_loop = (result.context or {}).get("review_loop", {})
    assert new_loop.get("max_iterations") == 7
    assert new_loop.get("churn_detections") == 2


@pytest.mark.asyncio
async def test_mid_budget_injects_narrow_focus_without_cap_reduction(db_session, mocker):
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

    # Only one prior entry → below ISSUE_CHURN_WINDOW, so churn cannot trigger.
    prior_history = [
        {"iteration": 0, "feedback": "f0", "issues": [_issue("alpha broken")]},
    ]
    # scope=medium → baseline cap 12; iteration 6 is exactly 50% → mid-budget.
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create", "pr_number": 801, "branch": "b2", "base_branch": "main",
                   "original_task_id": str(original_task.id)}],
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
        source_task, "fix", db_session,
        pr_number=801, branch="b2", base_branch="main",
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

"""Unit and integration tests for review_pipeline.py.

Covers:
  1. _max_review_iterations() — scope/fix_reason caps (pure unit, no DB).
  2. _spawn_pipeline_task() — global iteration cap (DB-backed).
  3. _spawn_pipeline_task() — per-chain iteration cap (DB-backed).
  4. _spawn_pipeline_task() — stuck-cycle detection (DB-backed).
  5. Edge cases: empty review_history, issues without severity,
     zero issues in current iteration.
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.models import Alert, AlertSeverity, Project, Task, TaskStatus
from app.review_pipeline import (
    MAX_REVIEW_FIX_ITERATIONS,
    MAX_REVIEW_HISTORY_FEEDBACK_CHARS,
    PERSISTENT_CONFLICT_ESCALATION_THRESHOLD,
    PIPELINE_TASK_STALE_MINUTES,
    SPAWN_REASON_BLOCKED_REPEAT,
    SPAWN_REASON_KEY,
    SPAWN_REASON_PERSISTENT_CONFLICT,
    SPAWN_REASON_SUPERSEDE_CHURN,
    STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_DEFAULT,
    STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_MEDIUM,
    STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_REFACTOR,
    STUCK_CYCLE_TEXT_THRESHOLD_DEFAULT,
    STUCK_CYCLE_TEXT_THRESHOLD_MEDIUM,
    STUCK_CYCLE_TEXT_THRESHOLD_REFACTOR,
    STUCK_CYCLE_THRESHOLD_DEFAULT,
    STUCK_CYCLE_THRESHOLD_MEDIUM,
    STUCK_CYCLE_THRESHOLD_REFACTOR,
    EARLY_NONCONVERGENCE_ESCALATE_AT,
    SPAWN_REASON_NONCONVERGENCE,
    _cap_review_history_feedback,
    _compute_scope_budget_utilization,
    _detect_issue_churn,
    _failure_fingerprint_msg,
    _repeating_issue_descriptions,
    _format_repeating_issues_section,
    build_ci_failure_fix_prompt,
    build_merge_conflict_fix_prompt,
    build_migration_guard_fix_prompt,
    compute_drift_signal,
    detect_early_nonconvergence,
    detect_fix_all_issue_class,
    fix_all_occurrences_directive,
    _feedback_jaccard,
    _feedback_token_set,
    _genuine_blocking_issue_exists,
    _is_cross_cutting_pr,
    _is_pipeline_task_stale,
    _is_test_file,
    _is_verification_only_rejection,
    _max_review_iterations,
    _recent_consecutive_lane_count,
    _spawn_pipeline_task,
    maybe_spawn_cross_cutting_safeguard,
    post_merge_summary_comment,
    should_auto_approve_diff,
    REVIEW_FIX_GOAL_PREAMBLE,
)
from testing.factories import make_project, make_task


# ── merge-conflict fix prompt ───────────────────────────────────────────────


class TestMergeConflictFixPrompt:
    """The conflict-resolution prompt must steer the agent away from the two
    failure modes that wedged rollup PR #8920: silently dropping a side's hunk,
    and keeping one side's code against the other side's test for the same
    feature. It must also set the expectation that ./check now verifies the
    affected tests so a bad resolution can't pass ./pr open."""

    def test_combines_both_intents_and_forbids_dropping(self):
        prompt = build_merge_conflict_fix_prompt(8920, "automain")
        assert "PR #8920" in prompt
        assert "automain" in prompt
        low = prompt.lower()
        # Combine both behaviors on overlapping hunks; don't pick a side.
        assert "combine both behaviors" in low
        assert "do not pick one side" in low
        # Don't silently drop content either side added.
        assert "do not drop" in low
        # Set the gate expectation: ./check runs affected tests, ./pr open blocks.
        assert "./check" in prompt and "./pr open" in prompt

    def test_appends_merge_error_when_provided(self):
        prompt = build_merge_conflict_fix_prompt(
            7, "main", merge_error="CONFLICT (content): file.py"
        )
        assert "Original merge error: CONFLICT (content): file.py" in prompt

    def test_omits_merge_error_section_when_absent(self):
        prompt = build_merge_conflict_fix_prompt(7, "main")
        assert "Original merge error" not in prompt


class TestMigrationGuardFixPrompt:
    """The migration repair must distinguish duplicate intent from new work."""

    def test_drops_semantic_duplicate_instead_of_renumbering_it(self):
        prompt = build_migration_guard_fix_prompt(
            pr_number=15420,
            base_branch="automain",
            guard_reason="duplicate migration revision 0625",
        )

        assert "PR #15420" in prompt
        assert "Guard reason: duplicate migration revision 0625" in prompt
        low = prompt.lower()
        assert "same schema change" in low
        assert "drop this pr's semantically duplicate migration" in low
        assert "do not renumber a semantically duplicate migration" in low
        assert "do not decide from revision numbers or filenames alone" in low

    def test_renumbers_only_genuinely_new_schema_work(self):
        prompt = build_migration_guard_fix_prompt(
            pr_number=15421,
            base_branch="automain",
            guard_reason="revision id collides with base",
        )

        low = prompt.lower()
        assert "only when the pr contains genuinely new schema work" in low
        assert "renumber that pr migration" in low
        assert "rechain each remaining pr migration" in low


# ── gh-token prompt pattern ────────────────────────────────────────────────────


class TestGhTokenPromptPattern:
    """Verify that fix prompts use the orchestrator check-runs proxy instead of
    exposing a raw GitHub token to the agent model.

    The agent must never receive the raw PAT-backed GitHub token. Check-run
    inspection must go through the /api/agent/github/check-runs proxy endpoint
    using the agent's own $AGENT_API_TOKEN credential.
    """

    def test_build_ci_failure_fix_prompt_uses_proxy_endpoint(self):
        prompt = build_ci_failure_fix_prompt(
            pr_number=42,
            base_branch="main",
            ci_errors="some error",
            owner="myorg",
            repo="myrepo",
        )
        assert "gh-token" not in prompt, (
            "build_ci_failure_fix_prompt must not reference ./gh-token — "
            "raw GitHub token helpers must not be exposed to the agent model"
        )
        assert "/api/agent/github/check-runs" in prompt, (
            "build_ci_failure_fix_prompt must use the orchestrator check-runs proxy "
            "endpoint so the model never sees the raw GitHub token"
        )
        assert "$AGENT_API_TOKEN" in prompt, (
            "check-runs proxy call must authenticate via $AGENT_API_TOKEN"
        )

    def test_build_ci_failure_fix_prompt_no_check_runs_hint_when_no_owner(self):
        prompt = build_ci_failure_fix_prompt(
            pr_number=1,
            base_branch="main",
            ci_errors="err",
            owner=None,
            repo=None,
        )
        assert "check-runs" not in prompt
        assert "gh-token" not in prompt

    def test_build_ci_failure_fix_prompt_adds_viewport_budget_remediation(self):
        # When CI fails on the viewport escape-hatch budget, the prompt must
        # spell out the bump-the-JSON remediation — the agent can't infer this
        # cross-file invariant and otherwise flails (frontend non-convergence).
        ci_errors = (
            "Viewport escape-hatch debt exceeds the budget in "
            "admin-ui/src/test/viewport-escape-hatch-budget.json.\n"
            "  CatchUpPanel: 3 escape hatches (budget: 1, over by 2)"
        )
        prompt = build_ci_failure_fix_prompt(
            pr_number=7, base_branch="main", ci_errors=ci_errors
        )
        assert "viewport-escape-hatch-budget.json" in prompt
        assert "bump" in prompt.lower()
        assert "total" in prompt.lower()

    def test_build_ci_failure_fix_prompt_no_viewport_hint_for_unrelated_errors(self):
        prompt = build_ci_failure_fix_prompt(
            pr_number=8,
            base_branch="main",
            ci_errors="FAILED testing/test_foo.py::test_bar - AssertionError",
        )
        assert "escape-hatch" not in prompt.lower()
        assert "viewport-escape-hatch-budget.json" not in prompt


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


async def _make_db_project(db_session, **project_kwargs) -> Project:
    """Persist a project and return the refreshed instance."""
    project = make_project(**project_kwargs)
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

    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

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


# ── _spawn_pipeline_task: verify_tests fast-path ──────────────────────────────


@pytest.mark.asyncio
async def test_spawn_verify_tests_creates_task_and_bypasses_cap(db_session, mocker):
    """_spawn_pipeline_task('verify_tests', ...) creates a one-shot verify_tests
    task on the PR head, bypassing the review-fix iteration caps (a 99-iteration
    chain that would block a review/fix spawn must NOT block verify_tests), and
    dedups a second spawn to the same active task."""
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

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[
            {
                "type": "create",
                "pr_number": 222,
                "branch": "feat/x",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            # A 99-iteration chain would trip the per-chain review-fix cap for a
            # review/fix spawn; verify_tests deliberately ignores it.
            "review_loop": {
                "iteration": 99,
                "original_task_id": str(original_task.id),
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    task, created = await _spawn_pipeline_task(
        source_task,
        "verify_tests",
        db_session,
        pr_number=222,
        branch="feat/x",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert created is True
    assert task is not None
    assert task.first_payload.get("type") == "verify_tests"
    assert task.first_payload.get("branch") == "feat/x"
    assert task.first_payload.get("pr_number") == 222
    assert task.first_payload.get("base_branch") == "main"
    assert task.status == TaskStatus.QUEUED

    # Second spawn for the same PR dedups to the same active task.
    task2, created2 = await _spawn_pipeline_task(
        source_task,
        "verify_tests",
        db_session,
        pr_number=222,
        branch="feat/x",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert created2 is False
    assert task2 is not None
    assert task2.id == task.id


@pytest.mark.asyncio
async def test_review_spawn_defers_when_blocked_fix_active(db_session, mocker):
    """PR #8742 regression: a same-PR fix sitting BLOCKED (e.g. file_conflict
    park) must make a review spawn DEFER, not spawn a re-review of the unchanged
    head. BLOCKED holds the DB active-idempotency slot (alembic 0138), so the
    cross-type liveness loop must treat it as live and hit the defer branch.

    Pre-fix BLOCKED was absent from _PIPELINE_ACTIVE_STATUSES, so the BLOCKED
    fix was invisible → the review spawned → REQUEST_CHANGES on an unchanged head
    → repeat (11 wasted opus re-reviews in prod). MUST FAIL pre-fix.

    The BLOCKED fix is also deliberately stale (no heartbeat, old started_at):
    a BLOCKED task is a precondition park, not a dying VM, so it must NOT be
    stale-superseded — it must reach the defer branch.
    """
    from datetime import datetime

    from app.review_pipeline import SPAWN_REASON_DEFERRED, PIPELINE_TASK_STALE_MINUTES

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

    # A fix on the SAME PR/chain, parked BLOCKED with a stale (no) heartbeat.
    blocked_fix = make_task(
        project_id=project.id,
        status=TaskStatus.BLOCKED,
        last_heartbeat=None,
        started_at=datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(minutes=PIPELINE_TASK_STALE_MINUTES + 5),
        payloads=[
            {
                "type": "fix",
                "pr_number": 8742,
                "branch": "feat/x",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "review_loop": {"iteration": 2, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(blocked_fix)
    await db_session.commit()
    await db_session.refresh(blocked_fix)

    # The originating task tries to (re)spawn a review for the same PR.
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[
            {
                "type": "create",
                "pr_number": 8742,
                "branch": "feat/x",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "review_loop": {"iteration": 2, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=8742,
        branch="feat/x",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    # Cross-type defer: no review spawned, deferral marker set.
    assert result is None
    assert created is False
    assert (source_task.context or {}).get(SPAWN_REASON_KEY) == SPAWN_REASON_DEFERRED

    # The BLOCKED fix must NOT have been stale-superseded (still BLOCKED).
    await db_session.refresh(blocked_fix)
    assert blocked_fix.status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_review_spawn_defers_to_ci_waiting_fix_and_does_not_churn(
    db_session, mocker
):
    """Rollup PR #10389 regression: a same-PR fix parked in CI-wait
    (review_loop.ci_waiting) with a stale heartbeat must make a review spawn
    DEFER — not stale-supersede the fix. A CI-waiting task's silence is by
    design (the orchestrator, not the VM, polls CI from here) and a multi-shard
    CI run outlasts the 15-min stale threshold, so superseding it both cancels a
    task making authoritative progress AND counts toward the supersession-churn
    halt that terminalized research b2231dd0.

    Pre-fix: the CI-waiting fix was stale-superseded → CANCELLED, churn counter
    incremented, a fresh review spawned. MUST FAIL pre-fix.
    """
    from datetime import datetime

    from app.review_pipeline import (
        SPAWN_REASON_DEFERRED,
        PIPELINE_TASK_STALE_MINUTES,
        _review_supersede_churn_count,
    )

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

    # A fix on the SAME PR/chain, EVALUATING + ci_waiting, with a stale heartbeat
    # (a 12-shard CI run easily exceeds the 15-min stale threshold).
    ci_waiting_fix = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        last_heartbeat=datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(minutes=PIPELINE_TASK_STALE_MINUTES + 5),
        payloads=[
            {
                "type": "fix",
                "pr_number": 10389,
                "branch": "research/x",
                "base_branch": "automain",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "review_loop": {
                "iteration": 1,
                "ci_waiting": True,
                "original_task_id": str(original_task.id),
            },
        },
    )
    db_session.add(ci_waiting_fix)
    await db_session.commit()
    await db_session.refresh(ci_waiting_fix)

    # The originating task tries to (re)spawn a review for the same PR (e.g. a
    # rollup resume pass).
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[
            {
                "type": "create",
                "pr_number": 10389,
                "branch": "research/x",
                "base_branch": "automain",
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
        "review",
        db_session,
        pr_number=10389,
        branch="research/x",
        base_branch="automain",
        original_task_id=str(original_task.id),
    )

    # Cross-type defer: no review spawned, deferral marker set.
    assert result is None
    assert created is False
    assert (source_task.context or {}).get(SPAWN_REASON_KEY) == SPAWN_REASON_DEFERRED

    # The CI-waiting fix must NOT have been stale-superseded (still EVALUATING).
    await db_session.refresh(ci_waiting_fix)
    assert ci_waiting_fix.status == TaskStatus.EVALUATING

    # And the supersession-churn counter must NOT have advanced — a deferral is
    # not a supersession, so it can never feed the churn halt.
    await db_session.refresh(original_task)
    assert _review_supersede_churn_count(original_task) == 0


@pytest.mark.asyncio
async def test_review_spawn_defers_when_in_progress_fix_active(db_session, mocker):
    """Cross-type regression lock: an IN_PROGRESS same-PR fix must also defer a
    review spawn (the in-flight fix must finish first). This pins that adding
    BLOCKED to the active set did not loosen the long-standing IN_PROGRESS defer.
    """
    from datetime import datetime

    from app.review_pipeline import SPAWN_REASON_DEFERRED

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

    inprogress_fix = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=datetime.now(timezone.utc).replace(tzinfo=None),
        payloads=[
            {
                "type": "fix",
                "pr_number": 8742,
                "branch": "feat/x",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "review_loop": {"iteration": 2, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(inprogress_fix)
    await db_session.commit()
    await db_session.refresh(inprogress_fix)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[
            {
                "type": "create",
                "pr_number": 8742,
                "branch": "feat/x",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "review_loop": {"iteration": 2, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=8742,
        branch="feat/x",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    assert result is None
    assert created is False
    assert (source_task.context or {}).get(SPAWN_REASON_KEY) == SPAWN_REASON_DEFERRED
    await db_session.refresh(inprogress_fix)
    assert inprogress_fix.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_review_spawns_when_all_prior_fixes_terminal(db_session, mocker):
    """Complement: when every prior same-PR fix is TERMINAL (COMPLETED / FAILED /
    CANCELLED), none are live, so the cross-type defer does NOT fire and a fresh
    review spawns. Confirms widening the active set to include BLOCKED did not
    make terminal fixes spuriously block a review.
    """
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

    # One terminal fix in each terminal status. CANCELLED is excluded from the
    # per-PR cap query (COMPLETED/FAILED only), so iteration count stays low and
    # the cap (16) is not tripped by these.
    for st in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        db_session.add(
            make_task(
                project_id=project.id,
                status=st,
                payloads=[
                    {
                        "type": "fix",
                        "pr_number": 8742,
                        "branch": "feat/x",
                        "base_branch": "main",
                        "original_task_id": str(original_task.id),
                    }
                ],
                context={
                    "review_loop": {
                        "iteration": 1,
                        "original_task_id": str(original_task.id),
                    },
                },
            )
        )
    await db_session.commit()

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[
            {
                "type": "create",
                "pr_number": 8742,
                "branch": "feat/x",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            # iteration>1 so the first-review cross-cutting safeguard gate
            # (iteration==1) is skipped — it needs github mocks we don't set up.
            "review_loop": {"iteration": 3, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=8742,
        branch="feat/x",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    # No live cross-type task → a fresh review is created.
    assert created is True
    assert result is not None
    assert result.first_payload.get("type") == "review"
    assert result.first_payload.get("pr_number") == 8742
    assert result.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_idempotency_collision_with_raced_out_task_does_not_raise(
    db_session, mocker
):
    """Regression: when a same-key spawn collides on the ux_task_active_idempotency_key
    partial unique index but the colliding task has raced OUT of the active-status
    window (e.g. QUEUED -> BLOCKED on a file-conflict lock) so the active-only
    recovery lookup misses it, _spawn_pipeline_task must fall back to a
    status-agnostic lookup and return the existing task — NOT re-raise the
    IntegrityError (which previously surfaced to operators as a bogus 'CI check
    error' and wedged the whole review chain)."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    # Force the active-status lookup to always miss — simulates the colliding
    # task leaving the active window between the failed INSERT and the re-query.
    # This makes BOTH the pre-check (so we reach the INSERT) and the recovery
    # active-only lookup (so we reach the broadened fallback) return None.
    mocker.patch(
        "app.review_pipeline._get_active_pipeline_task_by_key",
        new_callable=AsyncMock,
        return_value=None,
    )

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
        status=TaskStatus.EVALUATING,
        payloads=[
            {
                "type": "create",
                "pr_number": 222,
                "branch": "feat/x",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "review_loop": {"iteration": 99, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # First spawn: pre-check mocked to None, so the INSERT succeeds and a real
    # task with the computed idempotency_key now exists in an active status.
    task1, created1 = await _spawn_pipeline_task(
        source_task,
        "verify_tests",
        db_session,
        pr_number=222,
        branch="feat/x",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert created1 is True
    assert task1 is not None
    assert task1.idempotency_key  # key was assigned, so a re-spawn will collide

    # Second spawn: pre-check (mocked) misses → INSERT collides on the unique
    # index → recovery active-only lookup (mocked) also misses → the broadened
    # status-agnostic re-query must still find task1. Pre-fix this re-raised.
    task2, created2 = await _spawn_pipeline_task(
        source_task,
        "verify_tests",
        db_session,
        pr_number=222,
        branch="feat/x",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert created2 is False
    assert task2 is not None
    assert task2.id == task1.id


@pytest.mark.asyncio
async def test_spawn_lock_timeout_defers_without_poisoning_session(db_session, mocker):
    """Regression for prod burst 2026-06-07 01:05: the spawn commit was
    cancelled by Postgres' lock_timeout (asyncpg LockNotAvailableError waiting
    on a peer's uncommitted same-key insert). Pre-fix the error propagated with
    the session left in pending-rollback, cascading PendingRollbackError
    through the rollup caller and the rest of post-commit. _spawn_pipeline_task
    must roll back, mark the source DEFERRED (next sweep retries), return
    (None, False), and leave the session usable."""
    from sqlalchemy.exc import IntegrityError as _IntegrityError
    from sqlalchemy.exc import OperationalError as _OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession
    import contextlib as _contextlib

    from app.review_pipeline import SPAWN_REASON_DEFERRED

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

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[
            {
                "type": "create",
                "pr_number": 333,
                "branch": "feat/y",
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

    # Snapshot scalar ids BEFORE the spawn call: the production lock-timeout
    # handler rolls back the shared session, which expires every loaded
    # instance — an expired sync read afterwards raises MissingGreenlet under
    # real ORM expiry.
    project_id = project.id
    seed_task_ids = {original_task.id, source_task.id}

    # Fail the spawn commit ONLY when the new pipeline task is pending. Inject
    # a GENUINE flush-level abort first (duplicate-PK insert) so the underlying
    # transaction is really dead — the way a lock_timeout-cancelled INSERT
    # leaves it — then raise the asyncpg lock-timeout shape SQLAlchemy wraps.
    # (Mirrors the real-poison pattern in test_reaper's per-row collision test.)
    real_commit = _AsyncSession.commit
    fired = {"n": 0}

    async def patched_commit(self):
        pending_pipeline = any(
            isinstance(o, Task)
            and str(getattr(o, "idempotency_key", "") or "").startswith(
                "review_pipeline:"
            )
            for o in self.new
        )
        if pending_pipeline and fired["n"] == 0:
            fired["n"] += 1
            dup = uuid.uuid4()
            for _ in range(2):
                self.add(
                    Task(
                        id=dup,
                        project_id=project_id,
                        payloads=[{"type": "create"}],
                        status=TaskStatus.QUEUED,
                    )
                )
            with _contextlib.suppress(_IntegrityError):
                await self.flush()  # real duplicate-PK abort
            raise _OperationalError(
                "INSERT INTO task ...",
                {},
                Exception("canceling statement due to lock timeout"),
            )
        return await real_commit(self)

    mocker.patch.object(_AsyncSession, "commit", patched_commit)

    # "fix" routes through _spawn_pipeline_task's main-body INSERT + commit —
    # the path the prod burst hit (verify_tests delegates to a separate
    # spawner and is not the regression surface here).
    spawned, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=333,
        branch="feat/y",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Address the review feedback on PR #333",
    )

    assert fired["n"] == 1, "the injected lock-timeout must have fired exactly once"
    assert spawned is None
    assert created is False
    # Deferral marker set on the (re-fetched) source so the rollup caller's
    # existing DEFERRED branch leaves the parent EVALUATING for the next sweep.
    assert (source_task.context or {}).get(SPAWN_REASON_KEY) == SPAWN_REASON_DEFERRED
    assert "lock timeout" in (source_task.latest_status_message or "").lower()

    # The session must be CLEAN: committing the deferral marker (what the
    # rollup caller's final commit does) must not raise PendingRollbackError.
    await db_session.commit()

    # Nothing was spawned and the poison rows were rolled back.
    rows = (
        (await db_session.execute(select(Task).where(Task.project_id == project_id)))
        .scalars()
        .all()
    )
    ids = {t.id for t in rows}
    assert ids == seed_task_ids, (
        f"only the seed tasks may exist after the deferred spawn, got {ids}"
    )


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
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
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
    terminal_outcome = (original_task.context or {}).get("review_loop_terminal_outcome")
    assert terminal_outcome and terminal_outcome.get("kind") == "cap_exhausted"
    assert terminal_outcome.get("reason") == "review_cap_exhausted"
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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
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
        terminal_outcome = (root.context or {}).get("review_loop_terminal_outcome")
        assert terminal_outcome and terminal_outcome.get("kind") == "cap_exhausted"
        assert terminal_outcome.get("reason") == "review_cap_exhausted"
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
async def test_cap_exhaustion_skips_approval_when_gates_disabled(db_session, mocker):
    """With require_approval_gates off, hitting the review-fix cap must still
    FAIL the child (terminal -> partial rollup) and close the PR, but must NOT
    set requires_approval. Otherwise rollup_management escalates the flag onto
    the parent and services.py's 2h approval-gate timeout FAILs the otherwise-
    healthy parent, discarding its already-merged siblings. Regression for the
    c255c4cb / rollup PR #6722 incident (2026-05-28)."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
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
    # Operator has opted out of manual approval gating.
    mocker.patch.dict(
        "app.shared_state.runtime_config", {"require_approval_gates": False}
    )

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
                "pr_number": 903,
                "branch": "feat/gates-off",
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
    # 10 COMPLETED reviews on the same PR trip the small-scope global cap (10).
    for _ in range(10):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[
                    {
                        "type": "review",
                        "pr_number": 903,
                        "branch": "feat/gates-off",
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
        pr_number=903,
        branch="feat/gates-off",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Still broken",
    )
    assert result is None, "Global cap must still block the spawn"

    await db_session.refresh(original_task)
    # Terminal outcome is preserved — the cap still fails the child and closes the PR.
    assert original_task.status == TaskStatus.FAILED, (
        f"Cap must still FAIL the child even with gates off, got {original_task.status}"
    )
    assert (original_task.context or {}).get("failure_detail") == "review_cap_exhausted"
    close_mock.assert_awaited_once()
    # The fix: NO approval gate is raised when require_approval_gates is off.
    assert not original_task.requires_approval, (
        "With require_approval_gates off, cap exhaustion must NOT set "
        "requires_approval — the capped child is FAILED (terminal) and converges "
        "via partial rollup without gating the parent."
    )


# ── FIX D: cap exhaustion after approval → CANCELLED (no CODE_ERROR) ────────


def _mock_cap_exhaustion_github(mocker):
    """Mock the GitHub side effects of _handle_cap_exhaustion."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)


@pytest.mark.asyncio
async def test_approved_cap_exhaustion_records_distinct_outcome(db_session, mocker):
    """FIX D: when the review-fix cap is reached on an already-APPROVED PR
    (orig.review_loop.review_approved=True, no outstanding CHANGES_REQUESTED),
    the orig lands CANCELLED with failure_detail=cap_exhausted_after_approve and
    records a distinct non-CODE_ERROR outcome so approved merge-mechanics churn
    is queryable without looking like a code failure."""
    from app.failure_classification import APPROVED_CAP_PR_CLOSED

    _mock_cap_exhaustion_github(mocker)
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec_task = mocker.patch("app.routers.agent_status.record_task_result")
    rec_dir = mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )

    from app.review_pipeline import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={
            "scope": "small",
            "direction_id": "dir-approved-cap",
            "review_loop": {"review_approved": True, "pr_number": 404},
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "fix",
                "pr_number": 404,
                "original_task_id": str(orig.id),
            }
        ],
        context={"review_loop": {"review_approved": True, "pr_number": 404}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task,
        str(orig.id),
        404,
        16,
        db_session,
        scope="per-chain",
    )

    await db_session.refresh(orig)
    assert orig.status == TaskStatus.CANCELLED, (
        f"Cap-after-approve must land CANCELLED, got {orig.status}"
    )
    assert (orig.context or {}).get("failure_detail") == "cap_exhausted_after_approve"
    assert (orig.context or {}).get("failure_category") == APPROVED_CAP_PR_CLOSED
    assert "merge mechanics exhausted the cap" in (orig.latest_status_message or "")
    assert (orig.context or {}).get("pr_rejected") is not True, (
        "An approved PR is not a rejection — pr_rejected must not be set"
    )
    # No failure alert for a non-genuine outcome, but durable accounting records
    # the distinct approved-cap class for digest/analytics visibility.
    alert_mock.assert_not_called()
    rec_task.assert_called_once_with(
        success=False,
        failure_class=APPROVED_CAP_PR_CLOSED,
        task_id=str(orig.id),
        task_type="create",
    )
    rec_dir.assert_called_once_with(
        "dir-approved-cap",
        success=False,
        failure_class=APPROVED_CAP_PR_CLOSED,
        project_id=str(project.id),
    )


@pytest.mark.asyncio
async def test_approved_rollup_cap_preserved_not_closed(db_session, mocker):
    """RC-1: when the review-fix cap is reached on an already-APPROVED ROLLUP
    PR that is clean and non-empty, the PR must be LEFT OPEN for hand-merge
    rather than auto-closed. APPROVED_CAP_PR_CLOSED labeling is kept (approval
    changes the label, not whether the PR is destroyed)."""
    from app.failure_classification import APPROVED_CAP_PR_CLOSED

    close_mock = _mock_preservable_pr(mocker, changed_files=4, mergeable_state="clean")
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec_task = mocker.patch("app.routers.agent_status.record_task_result")
    rec_dir = mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )

    from app.review_pipeline import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={
            "scope": "medium",
            "direction_id": "dir-approved-rollup-cap",
            "_is_rollup_review": True,
            "rollup_pr_number": 407,
            "review_loop": {"review_approved": True, "pr_number": 407},
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "fix",
                "pr_number": 407,
                "original_task_id": str(orig.id),
            }
        ],
        context={"review_loop": {"review_approved": True, "pr_number": 407}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task,
        str(orig.id),
        407,
        16,
        db_session,
        scope="per-chain",
    )

    await db_session.refresh(orig)
    # Approved + preservable rollup: PR must be LEFT OPEN.
    close_mock.assert_not_called()
    assert orig.requires_approval is True, "Preserved PR must be flagged for operator"
    assert (orig.context or {}).get(
        "failure_detail"
    ) == "cap_exhausted_after_approve", (
        "Approved labeling must be preserved even on the preserve path"
    )
    assert (orig.context or {}).get("failure_category") == APPROVED_CAP_PR_CLOSED, (
        "Must record APPROVED_CAP_PR_CLOSED, not CODE_ERROR"
    )
    assert (orig.context or {}).get("pr_preserved_for_handmerge") is True
    assert (orig.context or {}).get("pr_rejected") is not True, (
        "Approved PR must never set pr_rejected"
    )
    alert_mock.assert_awaited()
    rec_task.assert_called_once_with(
        success=False,
        failure_class=APPROVED_CAP_PR_CLOSED,
        task_id=str(orig.id),
        task_type="create",
    )
    rec_dir.assert_called_once_with(
        "dir-approved-rollup-cap",
        success=False,
        failure_class=APPROVED_CAP_PR_CLOSED,
        project_id=str(project.id),
    )


@pytest.mark.asyncio
async def test_cap_exhaustion_with_outstanding_changes_requested_stays_failed(
    db_session, mocker
):
    """GUARDRAIL: when the latest review iteration still has outstanding
    CHANGES_REQUESTED (genuine non-convergence), cap exhaustion keeps today's
    FAILED + CODE_ERROR path even though an earlier round may have approved."""
    _mock_cap_exhaustion_github(mocker)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.routers.agent_status.record_task_result")
    mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )

    from app.review_pipeline import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={
            "scope": "small",
            # A stale earlier approval flag must NOT override a later unresolved
            # rejection: the latest iteration's review_history has CHANGES_REQUESTED.
            "review_loop": {
                "review_approved": True,
                "pr_number": 405,
                "review_history": [
                    {"verdict": "APPROVE"},
                    {"verdict": "CHANGES_REQUESTED", "issues": ["x"]},
                ],
            },
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 405, "original_task_id": str(orig.id)}],
        context={
            "review_loop": {
                "pr_number": 405,
                "review_history": [
                    {"verdict": "CHANGES_REQUESTED", "issues": ["x"]},
                ],
            }
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task, str(orig.id), 405, 16, db_session, scope="per-chain"
    )

    await db_session.refresh(orig)
    assert orig.status == TaskStatus.FAILED, (
        "Outstanding CHANGES_REQUESTED is genuine non-convergence → FAILED"
    )
    assert (orig.context or {}).get("failure_detail") == "review_cap_exhausted"
    assert (orig.context or {}).get("failure_category") == "CODE_ERROR"
    assert (orig.context or {}).get("pr_rejected") is True


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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

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
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
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
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
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
async def test_review_history_feedback_is_bounded(db_session, mocker):
    """Root-cause regression for the Postgres fat-row saturation outage.

    The review->fix loop stores per-iteration reviewer feedback in
    review_loop.review_history and carries it forward into every downstream
    task.context on every spawn. On the log-scraping fallback path that feedback
    is the agent's ENTIRE run log (multi-MB). Because context is GIN-indexed,
    persisting that blob produced multi-second single-row PK updates that
    saturated Postgres.

    Thread an oversized feedback blob through the real _spawn_pipeline_task path
    — both as the current iteration's feedback AND as a fat blob already sitting
    in the carried-forward history (a pre-fix in-flight row) — and assert the
    persisted context stays small: every feedback field is truncated to the cap,
    and the whole serialized context is kilobytes, not megabytes.

    Exercises backend/app/context_bounds.bound_review_history via the
    review_pipeline storage chokepoint — a test-only diff cannot make this pass.
    """
    from app.context_bounds import (
        REVIEW_FEEDBACK_MAX_CHARS,
        _TRUNCATION_MARKER,
        context_size_bytes,
    )

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
        context={"scope": "medium"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # An oversized blob standing in for the agent's full run log (the fat-row
    # pathology). ~700 KB — far above REVIEW_FEEDBACK_MAX_CHARS (8 KB) and any
    # legitimate freeform feedback, but within the 2 MB DB CHECK constraint so the
    # source_task INSERT succeeds while still exercising the bounding logic.
    fat_log = "AGENT-RUN-LOG " * 50_000  # 14 chars × 50_000 = 700_000 chars
    assert (
        len(fat_log) > 500_000
    )  # >> REVIEW_FEEDBACK_MAX_CHARS; below 2 MB DB constraint

    # The carried-forward history already holds a fat entry (models a pre-fix
    # in-flight row): the bound must self-heal it, not just the new entry.
    prior_fat_entry = {
        "iteration": 0,
        "feedback": fat_log,
        "verdict": "REQUEST_CHANGES",
        "issues": [_issue("alpha bravo charlie", "CRITICAL")],
        "changed_files": ["a.py"],
    }
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 9501,
                "branch": "feat/fat",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                # iteration>=2 so the iteration==1 cross-cutting safeguard is
                # skipped; distinct prior/current issues so stuck-cycle does NOT
                # fire (it would block the spawn and return None).
                "iteration": 2,
                "original_task_id": str(original_task.id),
                "pr_number": 9501,
                "branch": "feat/fat",
                "base_branch": "main",
                "review_history": [prior_fat_entry],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Current iteration's feedback is ALSO the full fat log, with structured
    # issues distinct from the prior entry (so no stuck-cycle match).
    current_issues = [_issue("xray yankee zulu", "CRITICAL")]

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=9501,
        branch="feat/fat",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Address the issues",
        current_feedback=fat_log,
        current_issues=current_issues,
    )

    assert result is not None and created is True, (
        "Fix should spawn (distinct issues → no stuck-cycle block)"
    )

    history = (result.context or {})["review_loop"]["review_history"]
    # Both the carried-forward fat entry and the new fat entry are present...
    assert len(history) == 2
    # ...and EVERY feedback field is bounded — the carried-forward blob
    # self-healed, not just the new one.
    for entry in history:
        assert len(entry["feedback"]) <= REVIEW_FEEDBACK_MAX_CHARS, (
            f"feedback not bounded: {len(entry['feedback'])} chars"
        )
    # The fat entries were actually truncated (marker present), proving the bound
    # fired rather than the blob just happening to be short.
    assert history[-1]["feedback"].endswith(_TRUNCATION_MARKER)
    assert history[0]["feedback"].endswith(_TRUNCATION_MARKER)

    # Structured data every other consumer reads is preserved verbatim.
    assert history[-1]["issues"] == current_issues
    assert history[-1]["verdict"] == "REQUEST_CHANGES"
    assert history[0]["issues"] == [_issue("alpha bravo charlie", "CRITICAL")]
    assert history[0]["changed_files"] == ["a.py"]

    # The whole persisted context is now kilobytes, not the ~4 MB of raw feedback
    # that went in — the property whose violation saturated Postgres.
    persisted_bytes = context_size_bytes(result.context)
    assert persisted_bytes < 100_000, (
        f"persisted context still fat: {persisted_bytes} bytes"
    )


@pytest.mark.asyncio
async def test_review_history_entry_count_is_bounded(db_session, mocker):
    """The carried-forward review_history entry count is capped (keep-most-recent)
    so it cannot grow without bound across a long chain. Complements the
    per-entry feedback truncation in test_review_history_feedback_is_bounded."""
    from app.context_bounds import REVIEW_HISTORY_MAX_ENTRIES

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
        context={"scope": "medium"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # A pathologically long carried-forward history (well past the cap). Each
    # entry carries a DISTINCT CRITICAL description, and the new iteration's issue
    # is distinct again, so stuck-cycle detection cannot match the most-recent two
    # and block the spawn.
    over = REVIEW_HISTORY_MAX_ENTRIES + 40
    long_history = [
        {
            "iteration": i,
            "feedback": f"iteration {i} feedback",
            "verdict": "REQUEST_CHANGES",
            "issues": [_issue(f"distinct issue number {i}", "CRITICAL")],
        }
        for i in range(over)
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 9502,
                "branch": "feat/long",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 2,
                "original_task_id": str(original_task.id),
                "pr_number": 9502,
                "branch": "feat/long",
                "base_branch": "main",
                "review_history": long_history,
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=9502,
        branch="feat/long",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Address the issues",
        current_feedback="brand new distinct feedback",
        current_issues=[_issue("brand new distinct critical", "CRITICAL")],
    )

    assert result is not None and created is True
    history = (result.context or {})["review_loop"]["review_history"]
    assert len(history) == REVIEW_HISTORY_MAX_ENTRIES, (
        f"entry count not capped: {len(history)}"
    )
    # Keep-most-recent: the newest entry (the one just appended) survives, the
    # oldest (iteration 0) is evicted.
    assert history[-1]["feedback"] == "brand new distinct feedback"
    assert all(e.get("iteration") != 0 for e in history[:-1])


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
async def test_stuck_cycle_with_gates_off_blocks_spawn_without_approval_gate(
    db_session, mocker
):
    """With require_approval_gates OFF, a detected stuck cycle must still halt
    the spawn (and set the informational status message) but must NOT set
    requires_approval on the chain original — otherwise services.py's 2h
    approval-gate timeout would FAIL an EVALUATING parent the operator never
    asked to gate. The rollup respawn-cap terminalization converges instead.

    Companion to test_stuck_cycle_detected_at_50pct_blocks_spawn (gates on)."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch.dict(
        "app.shared_state.runtime_config", {"require_approval_gates": False}
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

    prior_issues = [_issue("null pointer dereference", "CRITICAL")]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 302,
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
                "pr_number": 302,
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

    current_issues = [_issue("null pointer dereference", "CRITICAL")]

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=302,
        branch="b1",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Still the same issue",
        current_feedback="Still the same issue",
        current_issues=current_issues,
    )

    assert result is None, "Stuck cycle should still block the fix spawn"

    await db_session.refresh(original_task)
    # The fix: gates off → NO approval gate on the chain original.
    assert original_task.requires_approval is not True, (
        "With require_approval_gates off, a stuck cycle must NOT set "
        "requires_approval on the chain original"
    )
    # The informational status message is still set regardless of gating.
    assert "stuck" in (original_task.latest_status_message or "").lower(), (
        f"Status message should still mention stuck cycle: "
        f"{original_task.latest_status_message!r}"
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


def test_pilot_propagate_keys_wired_into_underscore_allowlist():
    """The lane-pilot's canonical PILOT_PROPAGATE_KEYS must be the source of
    truth for the spawn-time _PROPAGATE_UNDERSCORE_KEYS allow-list, not a
    re-typed pair of literals. The spawn-time context copy drops underscore keys
    by default, so the intent-to-treat arm (_review_fix_lane_arm) and the
    chain-private lane id (_review_fix_lane_id) only survive iterations because
    they are listed here — a drift between the two sets would silently
    de-populate the trial's arm/receipt subpopulations."""
    from app.review_fix_lane_pilot import PILOT_PROPAGATE_KEYS
    from app.review_pipeline import _PROPAGATE_UNDERSCORE_KEYS

    assert PILOT_PROPAGATE_KEYS, "pilot propagate set must be non-empty"
    # Every pilot key is carried by the spawn-time allow-list (no drift).
    assert PILOT_PROPAGATE_KEYS <= _PROPAGATE_UNDERSCORE_KEYS
    # These are underscore-internal keys; without the allow-list the default
    # spawn copy would strip them, so wiring them here is load-bearing.
    assert all(k.startswith("_") for k in PILOT_PROPAGATE_KEYS)


# ── Strategic context propagation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_strategic_context_propagates_to_spawned_fix(db_session, mocker):
    """A spawned fix task must inherit direction_id, related_files, parent_context,
    and scope from the source task's context so downstream agents see the same
    strategic context (direction, intent, relevant files) the create task had.

    Per-task underscore-prefixed flags must not leak; the rollup-classifier
    underscore flags (_is_rollup_review, _rollup_retries) DO propagate so the
    cap policy keeps resolving to the elevated rollup cap on every iteration
    (see _PROPAGATE_UNDERSCORE_KEYS in review_pipeline.py)."""
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
            # Per-task transient internal flag — must NOT propagate
            "_self_review_internal_state": {"foo": "bar"},
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

    # Per-task internal flags must not leak into the spawned task's context.
    assert "_self_review_internal_state" not in ctx, (
        "underscore-prefixed internal flags (not on the propagate allowlist) "
        "must not propagate"
    )

    # review_loop is overridden with the pipeline's own iteration state, not carried verbatim.
    assert ctx.get("review_loop", {}).get("iteration") == 1
    assert ctx.get("review_loop", {}).get("pr_number") == 501


@pytest.mark.asyncio
async def test_second_opinion_state_not_propagated_to_spawned_tasks(db_session, mocker):
    """The second-opinion key family is task-local review state and must NOT
    ride the review→fix→review context copy. Regression (PR #9243): an
    inherited reconciliation record fired the single-shot re-entry override on
    the next review task, clobbering its fresh APPROVE with iteration-1's
    REQUEST_CHANGES + verbatim issues — which then tripped the stuck-cycle
    detector and falsely parked the chain."""
    from app.task_completion import REVIEW_SECOND_OPINION_TASK_LOCAL_KEYS

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    second_opinion_ctx = {
        "review_second_opinion": {
            "state": "unchallenged",
            "primary_verdict": "REQUEST_CHANGES",
            "reconciled_verdict": "REQUEST_CHANGES",
            "surviving_issues": [{"description": "iteration-1 issue"}],
            "review_task_id": str(uuid.uuid4()),
        },
        "awaiting_review_second_opinion": str(uuid.uuid4()),
        "review_second_opinion_parked": {"verdict": "REQUEST_CHANGES", "issues": []},
        "review_second_opinion_for_task_id": str(uuid.uuid4()),
        "blind_review_for_task_id": str(uuid.uuid4()),
    }
    # The literal keys above must stay in lockstep with the canonical family —
    # if a key is renamed or added, this test must be updated alongside it.
    assert set(second_opinion_ctx) == set(REVIEW_SECOND_OPINION_TASK_LOCAL_KEYS)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 502,
                "branch": "feat/so",
                "base_branch": "main",
                "prompt": "Review X",
            }
        ],
        context={"direction_id": str(uuid.uuid4()), **second_opinion_ctx},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=502,
        branch="feat/so",
        base_branch="main",
        fix_prompt="Address the review feedback",
    )

    assert created is True and result is not None
    ctx = result.context or {}
    for key in REVIEW_SECOND_OPINION_TASK_LOCAL_KEYS:
        assert key not in ctx, (
            f"task-local second-opinion key {key!r} must not propagate to "
            f"spawned pipeline tasks"
        )
    assert ctx.get("direction_id"), "strategic context must still propagate"


@pytest.mark.asyncio
async def test_rollup_classifier_flags_propagate_to_spawned_fix(db_session, mocker):
    """Rollup-chain recognition must survive spawn-time propagation so the
    elevated iteration cap stays in force across every fix/review iteration.

    EVALUATING-strand root fix: ``_is_rollup_review`` is the parent-ONLY marker
    (``rollup_state._is_rollup_parent_awaiting_merge`` short-circuits on it), so
    it is DELIBERATELY no longer propagated to child tasks — a child carrying it
    looked like a rollup parent awaiting merge and stranded in EVALUATING. The
    cap policy no longer depends on the child carrying it: ``_rollup_retries``
    still propagates, and the propagated rollup lineage (``rollup_pr_number`` +
    ``review_loop.pr_number``) is recognised by ``_is_rollup_chain_source``.

    Regression for PR #6061 (auto-closed at iter 12 with reason="global"): the
    elevated cap must NOT collapse to the medium-scope base cap.
    """
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 601,
                "branch": "feat/rollup",
                "base_branch": "main",
                "prompt": "Land rollup PR",
            }
        ],
        context={
            "scope": "medium",
            "_is_rollup_review": True,
            "_rollup_retries": 2,
            "rollup_pr_number": 601,
            "review_loop": {"iteration": 0, "pr_number": 601},
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
        branch="feat/rollup",
        base_branch="main",
        fix_prompt="Address the review feedback",
    )

    assert created is True
    assert result is not None

    ctx = result.context or {}
    assert "_is_rollup_review" not in ctx, (
        "_is_rollup_review is the parent-only marker and must NOT propagate to "
        "child tasks — propagating it strands children in EVALUATING because "
        "_is_rollup_parent_awaiting_merge short-circuits True for them"
    )
    assert ctx.get("_rollup_retries") == 2, (
        "_rollup_retries must propagate so cap policy keeps recognising this "
        "chain as a rollup retry sequence"
    )
    # The propagated rollup lineage keeps the chain recognisable for the cap.
    assert ctx.get("rollup_pr_number") == 601
    assert ctx.get("review_loop", {}).get("pr_number") == 601

    # The child, now WITHOUT _is_rollup_review, is correctly NOT a rollup parent.
    from app.rollup_state import _is_rollup_parent_awaiting_merge

    assert _is_rollup_parent_awaiting_merge(ctx) is False, (
        "A child review/fix task must not be classified as a rollup parent "
        "awaiting merge once _is_rollup_review no longer propagates"
    )
    # But the cap-policy source recogniser still sees it as a rollup chain.
    from app.review_pipeline import _is_rollup_chain_source

    assert _is_rollup_chain_source(result) is True, (
        "_is_rollup_chain_source must still recognise the chain via "
        "_rollup_retries / rollup lineage so the elevated cap holds"
    )


@pytest.mark.asyncio
async def test_rollup_chain_at_iter_13_does_not_hit_global_cap(db_session, mocker):
    """Cap-boundary regression: a rollup chain reaching 13 completed PR iterations
    on a medium-scope task must NOT hit `_handle_cap_exhaustion` because the
    rollup-chain recogniser keeps the cap at MAX_REVIEW_FIX_ITERATIONS=16, not
    the medium-scope base_cap of 12.

    Post EVALUATING-strand fix: the elevated cap no longer depends on the child
    carrying `_is_rollup_review` (the parent-only marker). The propagated rollup
    lineage (`rollup_pr_number` + `review_loop.pr_number`) keeps the chain
    recognisable via `_is_rollup_chain_source`.
    """
    broadcast_mock = mocker.patch(
        "app.review_pipeline.broadcast_to_project", new_callable=AsyncMock
    )
    cap_exhaustion_mock = mocker.patch(
        "app.review_pipeline._handle_cap_exhaustion", new_callable=AsyncMock
    )

    project = await _make_db_project(db_session)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 701,
                "branch": "feat/rollup-chain",
                "base_branch": "main",
            }
        ],
        context={
            "scope": "medium",
            "_is_rollup_review": True,
            "rollup_pr_number": 701,
            "review_loop": {"iteration": 12, "pr_number": 701},
        },
    )
    db_session.add(source_task)

    # 13 completed PR iterations — above the medium base_cap (12) but below the
    # elevated rollup cap (MAX_REVIEW_FIX_ITERATIONS=16).
    for _ in range(13):
        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[
                {
                    "type": "review",
                    "pr_number": 701,
                    "branch": "feat/rollup-chain",
                    "base_branch": "main",
                }
            ],
        )
        db_session.add(t)

    await db_session.commit()
    await db_session.refresh(source_task)

    # Sanity check: MAX_REVIEW_FIX_ITERATIONS must actually exceed our iteration
    # count, otherwise the test cannot meaningfully distinguish the two caps.
    assert MAX_REVIEW_FIX_ITERATIONS == 16
    assert MAX_REVIEW_FIX_ITERATIONS > 13, (
        "Test premise broken: 13 iterations must be below the rollup cap"
    )

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=701,
        branch="feat/rollup-chain",
        base_branch="main",
        fix_prompt="Address the review feedback",
    )

    assert cap_exhaustion_mock.call_count == 0, (
        f"Rollup chain at 13 iterations (medium scope) must NOT hit "
        f"_handle_cap_exhaustion when the rollup chain is recognised correctly "
        f"(elevated cap=16). Got {cap_exhaustion_mock.call_count} call(s)."
    )
    assert result is not None, (
        "Rollup chain at iter 13 (medium scope) must still spawn a fix task "
        "because the elevated cap (16) is not exhausted"
    )
    # The child must NOT carry the parent-only _is_rollup_review marker (strand
    # fix), but the rollup lineage that drives the elevated cap is carried.
    assert "_is_rollup_review" not in (result.context or {})
    assert (result.context or {}).get("rollup_pr_number") == 701
    from app.review_pipeline import _is_rollup_chain_source

    assert _is_rollup_chain_source(result) is True
    # broadcast_to_project is invoked for the new task; surface the mock to
    # keep linters happy and confirm the spawn path completed normally.
    assert broadcast_mock.await_count >= 1


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
    assert deadline > datetime.now(timezone.utc).replace(tzinfo=None), "deadline should be in the future"
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
    long_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=WARM_HANDOFF_WINDOW_SECS + 60)
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
async def test_scope_pressure_large_scope_at_70_percent(db_session, mocker):
    """Large-scope task at ≥70% utilization gets a graduated cap with hard_cap_remaining=3."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    # Large scope has 20 file budget; 70% = 14 files. Use 15 files to exceed threshold.
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 705,
                "branch": "feat/large-gc",
                "base_branch": "main",
            }
        ],
        context={
            "scope": "large",
            "_pr_changed_files": [f"backend/app/large_{i}.py" for i in range(15)]
            + ["backend/testing/test_large.py"],
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
        pr_number=705,
        branch="feat/large-gc",
        base_branch="main",
        fix_prompt="Address review feedback",
    )
    assert created is True and result is not None
    ctx = result.context or {}
    # source iteration=0 → spawned iteration=1, base_cap=16, base_remaining=15, 75% util
    # (15/20 non-test files = 75%) → round(15 * 0.25) = 4 remaining,
    # no floor needed since 4 >= 3 → hard_cap = 1 + 4 = 5
    assert ctx.get("_scope_budget_hard_cap") == 5
    # No no-new-files prompt below 90%
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
    # NOTE: no external commit here — the filter must durably commit the
    # CANCELLED transition itself BEFORE broadcasting. Expiring the candidate
    # forces the refresh below to re-read from the DB, proving the transition
    # is persisted (not merely set in-memory and pending rollback).
    assert result == [], "Duplicate with running sibling must be filtered out"
    db_session.expire(candidate)
    await db_session.refresh(candidate)
    assert candidate.status == TaskStatus.CANCELLED
    assert "Deduplicated" in (candidate.latest_status_message or "")
    # The CANCELLED broadcast must have fired (after the commit above).
    broadcast_mock.assert_awaited_once()
    # Cancelled candidate must carry an infra failure_category so rollup parents
    # don't get upgraded to CODE_ERROR by worst_child_failure_category.
    assert (candidate.context or {}).get("failure_category") == "ADMIN_CANCEL"
    # Sibling must not be touched.
    await db_session.refresh(running)
    assert running.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_pipeline_dedup_commits_cancelled_before_broadcast(db_session, mocker):
    """Regression: the dedup-cancel path must durably commit the loser's
    terminal CANCELLED transition BEFORE emitting the task_updated broadcast.
    Emitting first lets the UI refetch /api/admin/tasks and read a still-QUEUED
    row (stale UI), and a later rollback would discard the CANCELLED change
    after the event already announced it (lost transition)."""
    from datetime import datetime, timezone
    from app.orchestrator_filters import filter_pipeline_duplicates

    project = await _make_db_project(db_session)

    running = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "review", "pr_number": 777, "base_branch": "main"}],
    )
    candidate = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "review", "pr_number": 777, "base_branch": "main"}],
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.add(running)
    db_session.add(candidate)
    await db_session.commit()
    await db_session.refresh(candidate)

    # Record the relative order of session.commit() and the broadcast. The
    # filter must commit the CANCELLED transition first so the event is never
    # emitted for an uncommitted (rollback-able) change.
    events: list[str] = []
    real_commit = db_session.commit

    async def _record_commit(*args, **kwargs):
        result = await real_commit(*args, **kwargs)
        events.append("commit")
        return result

    async def _on_broadcast(*_args, **_kwargs):
        events.append("broadcast")

    mocker.patch.object(db_session, "commit", side_effect=_record_commit)
    broadcast_mock = mocker.AsyncMock(side_effect=_on_broadcast)

    result = await filter_pipeline_duplicates(
        [candidate.id], [candidate], db_session, broadcast_mock
    )
    assert result == []
    broadcast_mock.assert_awaited_once()
    # A commit must be recorded, and the last commit before the broadcast must
    # precede it — proving commit-before-broadcast ordering.
    assert "commit" in events
    assert events.index("broadcast") > 0
    assert events[events.index("broadcast") - 1] == "commit"


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
    assert (newer.context or {}).get("failure_category") == "ADMIN_CANCEL"


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
    must be ADMIN_CANCEL so rollup parents don't treat it as a code defect."""
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
    assert (stale_review.context or {}).get("failure_category") == "ADMIN_CANCEL"


@pytest.mark.asyncio
async def test_supersession_deletes_vm(db_session, mocker):
    """When a stale task is superseded, its VM must be deleted after the spawn
    commits."""
    from datetime import timedelta
    from app.review_pipeline import PIPELINE_TASK_STALE_MINUTES, _utcnow

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mock_ensure_vm_deleted = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock
    )

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

    # Stale in-flight review with VM and no heartbeat for > threshold.
    stale_started = _utcnow() - timedelta(minutes=PIPELINE_TASK_STALE_MINUTES + 5)
    stale_review = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 802,
                "branch": "feat/vm-test",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        started_at=stale_started,
        last_heartbeat=None,
        vm_instance_id="agent-vm-test-stale-123",
    )
    db_session.add(stale_review)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 802,
                "branch": "feat/vm-test",
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
        pr_number=802,
        branch="feat/vm-test",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    # Verify the stale task was superseded
    await db_session.refresh(stale_review)
    assert stale_review.status == TaskStatus.CANCELLED

    # Verify _ensure_vm_deleted was called for the stale task's VM
    mock_ensure_vm_deleted.assert_called()
    calls = mock_ensure_vm_deleted.call_args_list
    vm_call_found = False
    for call in calls:
        args, kwargs = call
        if args[0] == "agent-vm-test-stale-123":
            vm_call_found = True
            assert "reason" in kwargs
            assert "superseded_stale_task" in kwargs["reason"]
            break
    assert vm_call_found, "VM deletion should be called for superseded task"


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
    assert (in_flight_review.context or {}).get("failure_category") == "ADMIN_CANCEL"


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


@pytest.mark.asyncio
async def test_fix_task_carries_viewport_shot_entries(db_session, mocker):
    """Fix tasks must forward viewport_shot_entries from the original create task
    so the fix agent can visually inspect and reason about the visual changes."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    # Create an original task with viewport_screenshots in its context
    viewport_shots = [
        {"name": "mobile_before.png", "storage_uri": "gs://bucket/mobile_before.png"},
        {"name": "desktop_after.png", "storage_uri": "gs://bucket/desktop_after.png"},
    ]
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={
            "scope": "small",
            "viewport_screenshots": viewport_shots,
        },
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Spawn a review task and verify it carries viewport_shot_entries
    review_task, review_created = await _spawn_pipeline_task(
        original_task,
        "review",
        db_session,
        pr_number=999,
        branch="feat/b",
        base_branch="main",
    )

    assert review_created is True and review_task is not None
    review_shots = review_task.first_payload.get("viewport_shot_entries", [])
    assert review_shots == viewport_shots, (
        f"Review task should carry viewport_shot_entries from original task; got: {review_shots!r}"
    )

    # Now spawn a fix task from the review task
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 999,
                "branch": "feat/b",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Spawn a fix task and verify it carries viewport_shot_entries
    fix_prompt = "Address the visual alignment issue."
    fix_task, fix_created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=999,
        branch="feat/b",
        base_branch="main",
        fix_prompt=fix_prompt,
    )

    assert fix_created is True and fix_task is not None
    fix_shots = fix_task.first_payload.get("viewport_shot_entries", [])
    assert fix_shots == viewport_shots, (
        f"Fix task should carry viewport_shot_entries from original task; got: {fix_shots!r}"
    )
    assert "viewport_source_task_id" in fix_task.first_payload, (
        "Fix task should carry viewport_source_task_id to identify the source of screenshots"
    )
    assert fix_task.first_payload.get("viewport_source_task_id") == str(
        original_task.id
    ), (
        f"Fix task viewport_source_task_id should reference original task; got: {fix_task.first_payload.get('viewport_source_task_id')!r}"
    )


@pytest.mark.asyncio
async def test_supersede_churn_halts_after_limit(db_session, mocker):
    """Two consecutive supersessions with no counted run in between must stop
    respawning and escalate the chain original."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch("app.review_pipeline._REVIEW_SUPERSEDE_CHURN_LIMIT", 2)

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

    stale_started = _utcnow_for_test() - timedelta(
        minutes=PIPELINE_TASK_STALE_MINUTES + 5
    )
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
    )
    db_session.add(stale_review)

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

    first_result, first_created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=701,
        branch="feat/s",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert first_created is True and first_result is not None
    await db_session.commit()

    await db_session.refresh(stale_review)
    assert stale_review.status == TaskStatus.CANCELLED
    assert (stale_review.context or {}).get("failure_detail") == (
        "superseded_by_review_pipeline"
    )

    first_result.started_at = stale_started
    first_result.last_heartbeat = None
    db_session.add(first_result)
    await db_session.commit()
    await db_session.refresh(first_result)

    second_result, second_created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=701,
        branch="feat/s",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    await db_session.commit()

    assert second_created is False
    assert second_result is None
    await db_session.refresh(original_task)
    assert (original_task.context or {}).get("_review_supersede_churn_count") == 2
    assert (original_task.context or {}).get("review_loop_terminal_outcome", {}).get(
        "kind"
    ) == "supersession_churn_halt"
    assert original_task.requires_approval is True
    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.task_id == original_task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "supersession-churn" in alerts[0].title
    assert "PR #701" in alerts[0].title
    assert (
        (original_task.context or {})
        .get("review_loop_terminal_outcome", {})
        .get("detail")
    )


@pytest.mark.asyncio
async def test_churn_halt_deletes_inflight_task_vms(db_session, mocker):
    """When supersession churn halts the pipeline, in-flight tasks' VMs
    must be deleted after the spawn commits.

    Both the stale review and the stale fix accumulate the churn count to the
    limit.  The churn-halt then fires, and vm_lifecycle._ensure_vm_deleted
    is called for every vm_instance_id collected during the stale supersessions.
    """
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch("app.review_pipeline._REVIEW_SUPERSEDE_CHURN_LIMIT", 2)
    # Capture the mock so we can assert on calls after the churn-halt fires.
    mock_ensure_vm_deleted = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock
    )

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

    stale_started = _utcnow_for_test() - timedelta(
        minutes=PIPELINE_TASK_STALE_MINUTES + 5
    )
    stale_review = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 901,
                "branch": "feat/churn-vm",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        started_at=stale_started,
        vm_instance_id="agent-vm-stale-review-901",
    )
    db_session.add(stale_review)

    # A fix task that is also stale (old heartbeat), so it is superseded in
    # the same first-loop pass.  That drives the churn count from 1 to 2
    # (== limit), triggering the halt, and its VM must be cleaned up.
    inflight_fix = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "fix",
                "pr_number": 901,
                "branch": "feat/churn-vm",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        started_at=stale_started,
        vm_instance_id="agent-vm-inflight-fix-901",
    )
    db_session.add(inflight_fix)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 901,
                "branch": "feat/churn-vm",
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
    await db_session.refresh(inflight_fix)
    await db_session.refresh(source_task)

    # Both stale tasks are superseded in the first loop (count 0→1→2 == limit),
    # then the churn-halt fires and both VMs are deleted after commit.
    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=901,
        branch="feat/churn-vm",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    # Churn halt returns (None, False)
    assert result is None
    assert created is False

    # Both stale tasks were superseded (cancelled) by the pipeline.
    await db_session.refresh(stale_review)
    assert stale_review.status == TaskStatus.CANCELLED

    await db_session.refresh(inflight_fix)
    assert inflight_fix.status == TaskStatus.CANCELLED

    # Both VMs must have been deleted after the commit — regardless of
    # whether they were collected via stale-supersession or churn-halt
    # cancellation, _ensure_vm_deleted must fire for each.
    deleted_vm_ids = {call.args[0] for call in mock_ensure_vm_deleted.call_args_list}
    assert "agent-vm-stale-review-901" in deleted_vm_ids
    assert "agent-vm-inflight-fix-901" in deleted_vm_ids


@pytest.mark.asyncio
async def test_supersede_churn_resets_after_counted_run(db_session, mocker):
    """A cap-counted non-infra run between supersessions must reset the streak."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.review_pipeline._REVIEW_SUPERSEDE_CHURN_LIMIT", 2)

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

    stale_started = _utcnow_for_test() - timedelta(
        minutes=PIPELINE_TASK_STALE_MINUTES + 5
    )
    stale_review = make_task(
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
        started_at=stale_started,
    )
    db_session.add(stale_review)

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
    await db_session.refresh(stale_review)
    await db_session.refresh(source_task)

    first_result, first_created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=702,
        branch="feat/r",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert first_created is True and first_result is not None
    await db_session.commit()

    counted_source = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 702,
                "branch": "feat/r",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "review_loop": {"iteration": 1, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(counted_source)
    await db_session.commit()
    await db_session.refresh(counted_source)

    first_result.started_at = stale_started
    first_result.last_heartbeat = None
    db_session.add(first_result)
    await db_session.commit()
    await db_session.refresh(first_result)

    second_result, second_created = await _spawn_pipeline_task(
        counted_source,
        "review",
        db_session,
        pr_number=702,
        branch="feat/r",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    await db_session.commit()

    assert second_created is True and second_result is not None
    await db_session.refresh(original_task)
    assert (original_task.context or {}).get("_review_supersede_churn_count") == 1
    assert (original_task.context or {}).get("review_loop_terminal_outcome") is None


@pytest.mark.asyncio
async def test_supersede_churn_bounded_when_orig_task_unloadable(db_session, mocker):
    """A supersession whose original_task_id resolves to a missing/unloadable task
    must still remain bounded (does not supersede unboundedly) and fails safe.

    When the chain-root task cannot be loaded, the counter increment and halt are
    both skipped in the normal flow. This test verifies the fail-safe: the spawn
    is halted with SPAWN_REASON_SUPERSEDE_CHURN when orig_task is unloadable and
    a supersession would occur, preventing unbounded churn."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    # Create a source task with an invalid/unresolvable original_task_id (e.g., a
    # task ID that doesn't exist in the DB).
    unloadable_original_id = str(uuid.uuid4())

    stale_started = _utcnow_for_test() - timedelta(
        minutes=PIPELINE_TASK_STALE_MINUTES + 5
    )
    stale_review = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 703,
                "branch": "feat/u",
                "base_branch": "main",
                "original_task_id": unloadable_original_id,
            }
        ],
        started_at=stale_started,
    )
    db_session.add(stale_review)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 703,
                "branch": "feat/u",
                "base_branch": "main",
                "prompt": "Build X",
            }
        ],
        context={
            "scope": "small",
            "review_loop": {
                "iteration": 0,
                "original_task_id": unloadable_original_id,
            },
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
        pr_number=703,
        branch="feat/u",
        base_branch="main",
        original_task_id=unloadable_original_id,
    )
    await db_session.commit()

    # Spawn should be halted, not created. The stale task is superseded
    # (CANCELLED), but the new task is not spawned because orig_task is unloadable.
    assert result is None, "Spawn must be halted when orig_task is unloadable"
    assert created is False
    assert (source_task.context or {}).get(
        SPAWN_REASON_KEY
    ) == SPAWN_REASON_SUPERSEDE_CHURN

    # Verify the stale task was actually superseded (cancelled).
    await db_session.refresh(stale_review)
    assert stale_review.status == TaskStatus.CANCELLED
    assert (stale_review.context or {}).get("failure_detail") == (
        "superseded_by_review_pipeline"
    )


@pytest.mark.asyncio
async def test_noop_fix_churn_increments_counter(db_session, mocker):
    """A completed fix task with no_op_reason must INCREMENT the supersession-churn
    counter rather than resetting it to zero.  This ensures that a streak of
    no-op fixes (deliverable already present) accumulates toward the churn halt
    instead of masking it."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.review_pipeline._REVIEW_SUPERSEDE_CHURN_LIMIT", 3)

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        # Simulated prior stale supersession put the count at 1.
        context={"scope": "small", "_review_supersede_churn_count": 1},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Source task is a COMPLETED fix whose agent called ./done --already-done
    # (no_op_reason is the canonical marker).
    noop_fix = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 710,
                "branch": "feat/noop",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "no_op_reason": "deliverable already present on main — no new commits",
            "review_loop": {
                "iteration": 2,
                "original_task_id": str(original_task.id),
            },
        },
    )
    db_session.add(noop_fix)
    await db_session.commit()
    await db_session.refresh(noop_fix)

    result, created = await _spawn_pipeline_task(
        noop_fix,
        "review",
        db_session,
        pr_number=710,
        branch="feat/noop",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    await db_session.commit()

    # Churn limit not yet reached (1 + 1 = 2 < 3), but the no-op fix
    # short-circuit must intercept and close the chain — no review spawns.
    assert result is None
    assert created is False
    await db_session.refresh(original_task)
    # Counter must have been INCREMENTED from 1 to 2, not reset to 0.
    assert (original_task.context or {}).get("_review_supersede_churn_count") == 2
    # Terminal outcome must indicate no-op fix convergence.
    assert (
        (original_task.context or {})
        .get("review_loop_terminal_outcome", {})
        .get("kind")
    ) == "converged"
    assert (
        (original_task.context or {})
        .get("review_loop_terminal_outcome", {})
        .get("reason")
    ) == "noop_fix_already_satisfied"


@pytest.mark.asyncio
async def test_noop_fix_churn_halt_fires(db_session, mocker):
    """Consecutive no-op fixes must trip the supersession-churn halt without any
    stale-task supersession occurring — the halt must fire on no-op-fix churn alone."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch("app.review_pipeline._REVIEW_SUPERSEDE_CHURN_LIMIT", 2)

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        # Churn count at 1 — as if one prior no-op fix already ran.
        context={"scope": "small", "_review_supersede_churn_count": 1},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Second consecutive no-op fix — no stale task, just another no-op completion.
    second_noop_fix = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 711,
                "branch": "feat/noop2",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "no_op_reason": "deliverable already present on main — no new commits",
            "review_loop": {
                "iteration": 4,
                "original_task_id": str(original_task.id),
            },
        },
    )
    db_session.add(second_noop_fix)
    await db_session.commit()
    await db_session.refresh(second_noop_fix)

    result, created = await _spawn_pipeline_task(
        second_noop_fix,
        "review",
        db_session,
        pr_number=711,
        branch="feat/noop2",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    await db_session.commit()

    # Churn limit reached (1 + 1 = 2 >= 2) — halt must fire, no task spawned.
    assert result is None
    assert created is False
    await db_session.refresh(original_task)
    assert (original_task.context or {}).get("_review_supersede_churn_count") == 2
    assert (
        (original_task.context or {})
        .get("review_loop_terminal_outcome", {})
        .get("kind")
    ) == "supersession_churn_halt"
    assert original_task.requires_approval is True
    # Terminal outcome reason must indicate no-op-fix cause, not stale supersession.
    assert (
        (original_task.context or {})
        .get("review_loop_terminal_outcome", {})
        .get("reason")
    ) == "noop_fix_already_satisfied"
    # An escalation alert must be created.
    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.task_id == original_task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_real_fix_resets_churn_noop_fix_does_not(db_session, mocker):
    """A fix with no_op_reason must NOT reset the churn counter; a fix WITHOUT
    no_op_reason (real commits) must reset it.  This is the regression guard:
    legitimate iterative review→fix chains must not be broken."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.review_pipeline._REVIEW_SUPERSEDE_CHURN_LIMIT", 5)

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "small", "_review_supersede_churn_count": 3},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # --- Part 1: no-op fix must NOT reset ---
    noop_fix = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 712,
                "branch": "feat/real",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "no_op_reason": "deliverable already present on main",
            "review_loop": {
                "iteration": 2,
                "original_task_id": str(original_task.id),
            },
        },
    )
    db_session.add(noop_fix)
    await db_session.commit()
    await db_session.refresh(noop_fix)

    noop_result, noop_created = await _spawn_pipeline_task(
        noop_fix,
        "review",
        db_session,
        pr_number=712,
        branch="feat/real",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    await db_session.commit()

    # No-op fix short-circuit intercepts — no review spawns, churn is 4.
    assert noop_created is False and noop_result is None
    await db_session.refresh(original_task)
    assert (original_task.context or {}).get("_review_supersede_churn_count") == 4
    assert (
        (original_task.context or {})
        .get("review_loop_terminal_outcome", {})
        .get("kind")
    ) == "converged"
    assert (
        (original_task.context or {})
        .get("review_loop_terminal_outcome", {})
        .get("reason")
    ) == "noop_fix_already_satisfied"

    # --- Part 2: real fix (no no_op_reason) MUST reset ---
    # No spawned review to complete from Part 1 (short-circuit prevented it).
    real_fix = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 712,
                "branch": "feat/real",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            # no no_op_reason — real commits were pushed
            "review_loop": {
                "iteration": 4,
                "original_task_id": str(original_task.id),
            },
        },
    )
    db_session.add(real_fix)
    await db_session.commit()
    await db_session.refresh(real_fix)

    real_result, real_created = await _spawn_pipeline_task(
        real_fix,
        "review",
        db_session,
        pr_number=712,
        branch="feat/real",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    await db_session.commit()

    assert real_created is True and real_result is not None
    await db_session.refresh(original_task)
    # Real fix must RESET churn to 0.
    assert (original_task.context or {}).get("_review_supersede_churn_count") == 0
    # Terminal outcome from Part 1's no-op fix remains in place (historical
    # breadcrumb) — a real fix resets the churn counter, not the outcome.


@pytest.mark.asyncio
async def test_noop_fix_halt_cancels_inflight_children(db_session, mocker):
    """When the no-op-fix churn halt fires, any in-flight review/fix tasks for
    the same PR chain that are still active must be cancelled proactively."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch("app.review_pipeline._REVIEW_SUPERSEDE_CHURN_LIMIT", 2)

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "small", "_review_supersede_churn_count": 1},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # An in-flight stale review on the same PR chain — exempt from stale-check
    # because it has a recent heartbeat but will match the PR chain. We make it
    # QUEUED (fresh, no heartbeat expiry) so it reaches the halt block still active
    # and gets cancelled by the new cleanup pass.
    # NOTE: a non-stale same-type (review) task normally triggers a dedup-return
    # which prevents the halt from firing. To exercise the cancellation path here
    # we use a FIX task (cross-type) that causes a "cancel-and-continue" (not
    # dedup-return) when the spawn type is "fix"; but since we're spawning a
    # "review" the cross-type fix would defer. Instead, we use a stale review
    # (no heartbeat, old started_at) so it gets superseded in the dedup loop
    # AND we verify it's cancelled by the time the halt fires.
    stale_started = _utcnow_for_test() - timedelta(
        minutes=PIPELINE_TASK_STALE_MINUTES + 5
    )
    stale_review = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 713,
                "branch": "feat/cancel",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        started_at=stale_started,
    )
    db_session.add(stale_review)

    # No-op fix: second consecutive no-op, churn goes 1→2 = limit.
    noop_fix = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 713,
                "branch": "feat/cancel",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "no_op_reason": "deliverable already present on main",
            "review_loop": {
                "iteration": 4,
                "original_task_id": str(original_task.id),
            },
        },
    )
    db_session.add(noop_fix)
    await db_session.commit()
    await db_session.refresh(stale_review)
    await db_session.refresh(noop_fix)

    result, created = await _spawn_pipeline_task(
        noop_fix,
        "review",
        db_session,
        pr_number=713,
        branch="feat/cancel",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    await db_session.commit()

    # Halt fired (stale supersession + no-op fix both contribute; churn >= 2).
    assert result is None
    assert created is False
    await db_session.refresh(original_task)
    assert original_task.requires_approval is True

    # The stale review must be cancelled (by the dedup loop or the halt cleanup).
    await db_session.refresh(stale_review)
    assert stale_review.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_real_fix_and_review_dont_interfere_with_noop_short_circuit(
    db_session, mocker
):
    """After a real fix spawns a review and the review completes, the churn
    counter must NOT be reset by the review-type source task.  When the
    subsequent fix completes as a no-op, the short-circuit must close the
    chain as converged (not spin forever).

    Steps:
      1. Pre-set churn counter to 1 (simulating a prior stale-cycle).
      2. REAL fix (no no_op_reason, resets churn to 0) → review spawns.
      3. Review completes REQUEST_CHANGES → spawns a new fix (churn stays 0;
         review source must NOT reset/modify the counter).
      4. The fix completes as no-op → short-circuit closes chain as converged.
    """
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.review_pipeline._REVIEW_SUPERSEDE_CHURN_LIMIT", 2)

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "small", "_review_supersede_churn_count": 1},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    pr_number = 720
    branch = "feat/noop-interleave"
    base_branch = "main"
    orig_id = str(original_task.id)

    # ── Step 1: real fix (no no_op_reason) resets churn to 0 → review spawns ──
    real_fix = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": base_branch,
                "original_task_id": orig_id,
            }
        ],
        context={
            "scope": "small",
            # No no_op_reason — real commits were pushed, resets churn.
            "review_loop": {"iteration": 2, "original_task_id": orig_id},
        },
    )
    db_session.add(real_fix)
    await db_session.commit()
    await db_session.refresh(real_fix)

    review_1, review_1_created = await _spawn_pipeline_task(
        real_fix,
        "review",
        db_session,
        pr_number=pr_number,
        branch=branch,
        base_branch=base_branch,
        original_task_id=orig_id,
    )
    await db_session.commit()

    # Real fix must spawn a review (no short-circuit applies).
    assert review_1_created is True, "Real fix must spawn a review"
    assert review_1 is not None
    await db_session.refresh(original_task)
    assert (original_task.context or {}).get("_review_supersede_churn_count") == 0, (
        "Real fix must RESET churn counter to 0"
    )

    # ── Step 2: review completes REQUEST_CHANGES → spawns fix ─────────────────
    assert review_1 is not None
    review_1.status = TaskStatus.COMPLETED
    db_session.add(review_1)
    await db_session.commit()

    review_task_source = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "review",
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": base_branch,
                "original_task_id": orig_id,
            }
        ],
        context={
            "scope": "small",
            "review_loop": {"iteration": 3, "original_task_id": orig_id},
            "verdict": "REQUEST_CHANGES",
            "issues": [{"severity": "major", "description": "Needs cleanup"}],
        },
    )
    db_session.add(review_task_source)
    await db_session.commit()
    await db_session.refresh(review_task_source)

    fix_from_review, fix_from_review_created = await _spawn_pipeline_task(
        review_task_source,
        "fix",
        db_session,
        pr_number=pr_number,
        branch=branch,
        base_branch=base_branch,
        original_task_id=orig_id,
        fix_prompt="Address review feedback",
        current_feedback="Needs cleanup",
        current_issues=[{"severity": "major", "description": "Needs cleanup"}],
    )
    await db_session.commit()

    # Review-type source must NOT modify the churn counter — it stays at 0.
    await db_session.refresh(original_task)
    assert (original_task.context or {}).get("_review_supersede_churn_count") == 0, (
        "Review source must NOT modify churn counter"
    )

    # Mark the spawned fix COMPLETED so the next review-spawn dedup does not block.
    if fix_from_review is not None:
        fix_from_review.status = TaskStatus.COMPLETED
        db_session.add(fix_from_review)
        await db_session.commit()

    # ── Step 3: the fix completes as no-op → short-circuit closes chain ───────
    noop_fix = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": base_branch,
                "original_task_id": orig_id,
            }
        ],
        context={
            "scope": "small",
            "no_op_reason": "deliverable already present on main — no new commits",
            "review_loop": {"iteration": 4, "original_task_id": orig_id},
        },
    )
    db_session.add(noop_fix)
    await db_session.commit()
    await db_session.refresh(noop_fix)

    result, created = await _spawn_pipeline_task(
        noop_fix,
        "review",
        db_session,
        pr_number=pr_number,
        branch=branch,
        base_branch=base_branch,
        original_task_id=orig_id,
    )
    await db_session.commit()

    # No-op fix short-circuit must close the chain as converged (0+1=1 < 2).
    assert result is None, "No-op fix must short-circuit, no review spawned"
    assert created is False
    await db_session.refresh(original_task)
    assert (original_task.context or {}).get("_review_supersede_churn_count") == 1, (
        "No-op fix must increment churn counter to 1"
    )
    term = (original_task.context or {}).get("review_loop_terminal_outcome", {})
    assert term.get("kind") == "converged", (
        f"Expected converged terminal outcome, got {term.get('kind')}"
    )
    assert term.get("reason") == "noop_fix_already_satisfied", (
        f"Expected noop_fix_already_satisfied reason, got {term.get('reason')}"
    )


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
async def test_churn_cap_reduction_durable_across_spawns(db_session, mocker):
    """When churn is detected, the reduced cap must persist across spawns via
    _churn_cap_reduction so that subsequent spawns honor the earlier reduction
    and _max_review_iterations returns the reduced cap instead of the base cap."""
    from app.review_pipeline import _max_review_iterations

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

    # Setup: build a review_history with 2 entries, each having 2 CRITICAL issues
    # (distinct descriptions with low token overlap → evades stuck detection but is churn).
    # Use completely different issue names to avoid Jaccard similarity matching.
    prior_history = [
        {
            "iteration": 0,
            "feedback": "f0",
            "issues": [
                _issue("missing null check in function_alpha"),
                _issue("off by one in loop_beta"),
            ],
        },
        {
            "iteration": 1,
            "feedback": "f1",
            "issues": [
                _issue("array bounds in handler_gamma"),
                _issue("type mismatch in parser_delta"),
            ],
        },
    ]
    # First spawn: iteration 2, detects churn, reduces cap.
    first_source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 704,
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
                "pr_number": 704,
                "branch": "b1",
                "base_branch": "main",
                "review_history": prior_history,
            },
        },
    )
    db_session.add(first_source_task)
    await db_session.commit()
    await db_session.refresh(first_source_task)

    # First spawn: iteration 3 will detect churn (counts [2, 2, 2] → non-decreasing).
    # medium scope baseline=12, iteration=3, remaining=9 → reduced_cap = 3 + 9//2 = 7.
    first_result, _ = await _spawn_pipeline_task(
        first_source_task,
        "fix",
        db_session,
        pr_number=704,
        branch="b1",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Please fix.",
        current_feedback="f2",
        current_issues=[
            _issue("undefined reference in context_epsilon"),
            _issue("missing import in config_zeta"),
        ],
    )

    assert first_result is not None
    first_ctx = first_result.context or {}
    first_review_loop = first_ctx.get("review_loop", {})

    # Verify: first spawn's context has the reduced cap stored.
    assert first_review_loop.get("max_iterations") == 7
    assert first_ctx.get("_churn_cap_reduction") == 7, (
        "Churn cap reduction must be persisted to _churn_cap_reduction"
    )

    # Second spawn: use the first spawn's result as the source task.
    # Iteration 4: reduce issue count to [2, 1] (decreasing) → no churn detected.
    # The prior _churn_cap_reduction (7) must be respected in _max_review_iterations.
    second_source_task = first_result
    second_result, _ = await _spawn_pipeline_task(
        second_source_task,
        "fix",
        db_session,
        pr_number=704,
        branch="b1",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Further fix.",
        current_feedback="f3",
        current_issues=[_issue("race condition in thread_handler")],
    )

    assert second_result is not None
    second_ctx = second_result.context or {}

    # Verify: second spawn's _max_review_iterations honors the prior churn reduction.
    # Without the fix, _max_review_iterations would return 12 (baseline for medium scope).
    # With the fix, it must return 7 (the reduced cap from the first spawn).
    computed_cap = _max_review_iterations(second_result)
    assert computed_cap == 7, (
        f"_max_review_iterations must honor persisted churn reduction: "
        f"expected 7 (reduced cap), got {computed_cap}"
    )

    # Also verify: the persisted _churn_cap_reduction is carried forward.
    assert second_ctx.get("_churn_cap_reduction") == 7


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


_SG_DUAL_HEAD_DIFF = (
    "diff --git a/backend/alembic/versions/0002_dup.py "
    "b/backend/alembic/versions/0002_dup.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/backend/alembic/versions/0002_dup.py\n"
    '+"""dup migration"""\n'
    '+revision = "0002"\n'
    '+down_revision = "0001"\n'
)


@pytest.mark.asyncio
async def test_release_safeguard_gate_post_gate_merge_held_on_dual_head(
    db_session, mocker
):
    """A gated post-gate merge (orig already COMPLETED) HOLDS on a dual head.

    Exercises the _sg_gated=True branch of _maybe_release_safeguard_gate: the
    held PR targets the project integration branch and adds a migration whose
    revision id collides with one already on that branch, so the pre-merge
    Alembic dual-head guard must block — merge_pull_request is never called and
    the PR is left open for the guarded retry/resolver flow.
    """
    from app.task_completion import _maybe_release_safeguard_gate

    project = make_project(integration_branch="automain")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # orig already terminalized (COMPLETED) with an open held PR — the path that
    # fires the merge directly from the gate-release rather than via CI polling.
    orig = make_task(
        project_id=project.id,
        requires_approval=True,
        status=TaskStatus.COMPLETED,
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    safeguard = make_task(
        project_id=project.id,
        context={"safeguard_for_task_id": str(orig.id), "safeguard_pr_number": 42},
    )

    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    # PR is open, unmerged, and targets the integration branch -> _sg_gated=True.
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "open",
            "merged": False,
            "base": {"ref": "automain"},
        },
    )
    # Make the real dual-head guard see a same-revision collision on the target.
    mocker.patch(
        "app.github.get_pr_files",
        new_callable=AsyncMock,
        return_value=[{"filename": "backend/alembic/versions/0002_dup.py"}],
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new_callable=AsyncMock,
        return_value=_SG_DUAL_HEAD_DIFF,
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new_callable=AsyncMock,
        return_value=["0001_init.py", "0002_existing.py"],
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )

    await _maybe_release_safeguard_gate(safeguard, [], db_session)

    # Gate still released, but the held merge was NOT fired — left open for the
    # guarded retry/resolver flow.
    merge_mock.assert_not_called()
    await db_session.refresh(orig)
    assert orig.requires_approval is False
    # Idempotency flag was set with the gate-clear commit so the merge is never
    # re-attempted from here even though it was held.
    assert (orig.context or {}).get("_safeguard_merge_fired") is True


# ── Cross-cutting gate: integration (spawn + merge-gate) ──────────────────────


@pytest.mark.asyncio
async def test_safeguard_spawned_and_merge_gate_set_for_large_scope_pr(
    db_session, mocker
):
    """When a review is triggered for a large-scope PR, the pipeline must spawn a
    safeguard (security_audit or qa) task AND set requires_approval=True on the
    original task so it cannot be merged until the safeguard approves.

    The agent spawn is mocked via maybe_spawn_cross_cutting_safeguard; the test
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
        "app.review_pipeline.maybe_spawn_cross_cutting_safeguard",
        new_callable=AsyncMock,
        wraps=maybe_spawn_cross_cutting_safeguard,
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

    await db_session.refresh(original_task)
    assert original_task.requires_approval is not True, (
        "Small-scope PRs must never be merge-gated by the cross-cutting safeguard"
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

    # Persist before refreshing: the halt path leaves the reclassification and
    # the spawn-reason marker pending for the rollup caller's final commit
    # (per the _spawn_pipeline_task contract); under real ORM expiry a refresh
    # without this commit would discard the pending writes.
    await db_session.commit()

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


def test_ci_failure_fix_prompt_includes_test_diagnosis_guidance():
    """Regression guard: after PRs #5793 (shard-5 flake) + #5789 (sibling mock-
    signature drift in test_rate_limit_detection.py) both hit the 8-iter cap on
    'NEW failing test' loops, the prompt now tells the agent to diagnose the
    test (real regression / sibling-broken / flake) before editing their diff."""
    prompt = build_ci_failure_fix_prompt(
        pr_number=1,
        base_branch="main",
        ci_errors="FAILED testing/test_foo.py::test_bar",
    )
    # Must surface the three failure modes and tell the agent to look at the test.
    assert "Diagnose before fixing" in prompt
    assert "mock-signature drift" in prompt
    assert "flake" in prompt
    # Must tell the agent to verify on base HEAD before editing their diff.
    assert "Run the test on `main` HEAD" in prompt


def test_ci_failure_fix_prompt_check_runs_hint_optional():
    with_owner = build_ci_failure_fix_prompt(
        pr_number=1,
        base_branch="main",
        ci_errors="boom",
        owner="acme",
        repo="widgets",
    )
    # owner/repo gate the orchestrator check-runs hint (the proxy URL itself no
    # longer interpolates them — see PR #5840). Verify the hint is present.
    assert "/api/agent/github/check-runs" in with_owner
    without_owner = build_ci_failure_fix_prompt(
        pr_number=1, base_branch="main", ci_errors="boom"
    )
    assert "check-runs" not in without_owner


# ---------------------------------------------------------------------------
# REVIEW_FIX_GOAL_PREAMBLE — goal framing for review-fix tasks.
#
# Regression guard for PR #6061 (2026-05-26): the auto-review fix prompt used
# to say only "A code review requested changes. Address the following issues:"
# which framed the agent's job as "patch the cited line." Each fix narrowly
# edited the cited spot and silently dropped invariants the function used to
# enforce; the next reviewer surfaced the dropped invariant as a new MAJOR.
# Seven cycles, no convergence — auto-closed at the iteration cap.
# ---------------------------------------------------------------------------


def test_review_fix_goal_preamble_frames_goal_as_mergeable_pr():
    assert "make this PR mergeable" in REVIEW_FIX_GOAL_PREAMBLE
    assert "invariant" in REVIEW_FIX_GOAL_PREAMBLE


def test_review_fix_goal_preamble_warns_against_dropping_adjacent_guarantees():
    # The preamble must explicitly tell the agent that adjacent regressions
    # cost more iterations than the cited finding — otherwise narrow-patching
    # remains the implicit default.
    assert "regression in adjacent behavior" in REVIEW_FIX_GOAL_PREAMBLE


# ---------------------------------------------------------------------------
# Spawn-time warm-pin funnel — _spawn_pipeline_task records exactly one
# outcome per spawn that reaches the warm-pinning decision block.
# ---------------------------------------------------------------------------


def _clear_spawn_pin_events() -> None:
    import app.warm_backlog_reuse as wbr

    wbr._spawn_attempt_events.clear()
    wbr._spawn_pin_set_events.clear()
    wbr._spawn_pin_skipped_events.clear()


async def _make_pin_source_task(db_session, *, vm_instance_id, status, last_heartbeat):
    """Build + persist a source review task suitable for driving the warm-pin
    decision in _spawn_pipeline_task. Pin decisions look at vm_instance_id,
    status, and last_heartbeat — the rest is generic chain bookkeeping."""
    project = await _make_db_project(db_session)
    original = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "small"},
    )
    db_session.add(original)
    await db_session.commit()
    await db_session.refresh(original)

    source = make_task(
        project_id=project.id,
        status=status,
        vm_instance_id=vm_instance_id,
        last_heartbeat=last_heartbeat,
        payloads=[
            {
                "type": "review",
                "pr_number": 901,
                "branch": "feat/warm-pin",
                "base_branch": "main",
                "original_task_id": str(original.id),
            }
        ],
        context={
            "scope": "small",
            "review_loop": {
                "iteration": 0,
                "original_task_id": str(original.id),
            },
        },
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source, original


@pytest.mark.asyncio
async def test_spawn_pin_funnel_records_pin_set(db_session, mocker):
    """Eligible source (IN_PROGRESS, has vm_instance_id, flag on) → pin_set."""
    from app.time_utils import _utcnow
    import app.warm_backlog_reuse as wbr

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.review_pipeline.is_warm_continuity_suppressed", return_value=False
    )
    _clear_spawn_pin_events()
    try:
        source, original = await _make_pin_source_task(
            db_session,
            vm_instance_id="agent-vm-xyz",
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=_utcnow(),
        )

        result, _was_created = await _spawn_pipeline_task(
            source,
            "fix",
            db_session,
            pr_number=901,
            branch="feat/warm-pin",
            base_branch="main",
            original_task_id=str(original.id),
            fix_prompt="Fix the issue",
        )
        assert result is not None
        assert (result.context or {}).get("warm_target_vm_id") == "agent-vm-xyz"

        snap = wbr.get_spawn_pin_funnel_snapshot()
        assert snap["spawn_attempts"] == 1
        assert snap["pin_set"] == 1
        assert snap["skipped_total"] == 0
        assert snap["skipped_by_reason"] == {}
    finally:
        _clear_spawn_pin_events()


@pytest.mark.asyncio
async def test_spawn_pin_funnel_records_skip_disabled(db_session, mocker):
    """warm_continuity_enabled=False → skip reason 'disabled'."""
    from app.time_utils import _utcnow
    import app.warm_backlog_reuse as wbr
    import app.review_pipeline as rp

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.review_pipeline.is_warm_continuity_suppressed", return_value=False
    )
    mocker.patch.dict(
        rp._runtime_config, {"warm_continuity_enabled": False}, clear=False
    )
    _clear_spawn_pin_events()
    try:
        source, original = await _make_pin_source_task(
            db_session,
            vm_instance_id="agent-vm-xyz",
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=_utcnow(),
        )

        result, _was_created = await _spawn_pipeline_task(
            source,
            "fix",
            db_session,
            pr_number=901,
            branch="feat/warm-pin",
            base_branch="main",
            original_task_id=str(original.id),
            fix_prompt="Fix the issue",
        )
        assert result is not None
        assert "warm_target_vm_id" not in (result.context or {})

        snap = wbr.get_spawn_pin_funnel_snapshot()
        assert snap["spawn_attempts"] == 1
        assert snap["pin_set"] == 0
        assert snap["skipped_by_reason"] == {"disabled": 1}
        assert snap["skipped_total"] == 1
    finally:
        _clear_spawn_pin_events()


@pytest.mark.asyncio
async def test_spawn_pin_funnel_records_skip_breaker_tripped(db_session, mocker):
    """is_warm_continuity_suppressed()==True → skip reason 'breaker_tripped'."""
    from app.time_utils import _utcnow
    import app.warm_backlog_reuse as wbr

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.review_pipeline.is_warm_continuity_suppressed", return_value=True)
    _clear_spawn_pin_events()
    try:
        source, original = await _make_pin_source_task(
            db_session,
            vm_instance_id="agent-vm-xyz",
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=_utcnow(),
        )

        result, _was_created = await _spawn_pipeline_task(
            source,
            "fix",
            db_session,
            pr_number=901,
            branch="feat/warm-pin",
            base_branch="main",
            original_task_id=str(original.id),
            fix_prompt="Fix the issue",
        )
        assert result is not None
        assert "warm_target_vm_id" not in (result.context or {})

        snap = wbr.get_spawn_pin_funnel_snapshot()
        assert snap["spawn_attempts"] == 1
        assert snap["pin_set"] == 0
        assert snap["skipped_by_reason"] == {"breaker_tripped": 1}
        assert snap["skipped_total"] == 1
    finally:
        _clear_spawn_pin_events()


@pytest.mark.asyncio
async def test_spawn_pin_funnel_records_skip_source_cold(db_session, mocker):
    """Source has no vm_instance_id → skip reason 'source_cold'."""
    from app.time_utils import _utcnow
    import app.warm_backlog_reuse as wbr

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.review_pipeline.is_warm_continuity_suppressed", return_value=False
    )
    _clear_spawn_pin_events()
    try:
        source, original = await _make_pin_source_task(
            db_session,
            vm_instance_id=None,
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=_utcnow(),
        )

        result, _was_created = await _spawn_pipeline_task(
            source,
            "fix",
            db_session,
            pr_number=901,
            branch="feat/warm-pin",
            base_branch="main",
            original_task_id=str(original.id),
            fix_prompt="Fix the issue",
        )
        assert result is not None
        assert "warm_target_vm_id" not in (result.context or {})

        snap = wbr.get_spawn_pin_funnel_snapshot()
        assert snap["spawn_attempts"] == 1
        assert snap["pin_set"] == 0
        assert snap["skipped_by_reason"] == {"source_cold": 1}
        assert snap["skipped_total"] == 1
    finally:
        _clear_spawn_pin_events()


@pytest.mark.asyncio
async def test_spawn_pin_funnel_records_skip_stale_source(db_session, mocker):
    """COMPLETED source with stale heartbeat → skip reason 'stale_source'."""
    from app.time_utils import _utcnow
    import app.warm_backlog_reuse as wbr
    from app.review_pipeline import WARM_HANDOFF_WINDOW_SECS

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.review_pipeline.is_warm_continuity_suppressed", return_value=False
    )
    _clear_spawn_pin_events()
    try:
        # Heartbeat just past the warm-handoff window.
        stale_hb = _utcnow() - timedelta(seconds=WARM_HANDOFF_WINDOW_SECS + 10)
        source, original = await _make_pin_source_task(
            db_session,
            vm_instance_id="agent-vm-xyz",
            status=TaskStatus.COMPLETED,
            last_heartbeat=stale_hb,
        )

        result, _was_created = await _spawn_pipeline_task(
            source,
            "fix",
            db_session,
            pr_number=901,
            branch="feat/warm-pin",
            base_branch="main",
            original_task_id=str(original.id),
            fix_prompt="Fix the issue",
        )
        assert result is not None
        assert "warm_target_vm_id" not in (result.context or {})

        snap = wbr.get_spawn_pin_funnel_snapshot()
        assert snap["spawn_attempts"] == 1
        assert snap["pin_set"] == 0
        assert snap["skipped_by_reason"] == {"stale_source": 1}
        assert snap["skipped_total"] == 1
    finally:
        _clear_spawn_pin_events()


@pytest.mark.asyncio
async def test_spawn_pin_funnel_records_skip_caller_suppressed(db_session, mocker):
    """suppress_warm_handoff=True → skip reason 'caller_suppressed'."""
    from app.time_utils import _utcnow
    import app.warm_backlog_reuse as wbr

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.review_pipeline.is_warm_continuity_suppressed", return_value=False
    )
    _clear_spawn_pin_events()
    try:
        source, original = await _make_pin_source_task(
            db_session,
            vm_instance_id="agent-vm-xyz",
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=_utcnow(),
        )

        result, _was_created = await _spawn_pipeline_task(
            source,
            "fix",
            db_session,
            pr_number=901,
            branch="feat/warm-pin",
            base_branch="main",
            original_task_id=str(original.id),
            fix_prompt="Fix the issue",
            suppress_warm_handoff=True,
        )
        assert result is not None
        assert "warm_target_vm_id" not in (result.context or {})

        snap = wbr.get_spawn_pin_funnel_snapshot()
        assert snap["spawn_attempts"] == 1
        assert snap["pin_set"] == 0
        assert snap["skipped_by_reason"] == {"caller_suppressed": 1}
        assert snap["skipped_total"] == 1
    finally:
        _clear_spawn_pin_events()


# ── C4: review-completion early-exit gates on merged, not closed ─────────────


@pytest.mark.asyncio
async def test_review_early_exit_skips_only_when_merged(db_session, mocker):
    """C4 fix #1: the review-completion early-exit MUST gate on merged==True.

    Pre-fix it short-circuited on ``state == 'closed'``, so a PR caught inside
    the orchestrator's transient close+reopen mergeability-reset window was
    treated as "review skipped" — silently DROPPING the review_approved handoff
    and stranding the parent in EVALUATING until the 60-min/6h backstop.

    This test drives _process_post_commit on a review task whose PR reports
    closed-not-merged and asserts the early-exit did NOT fire (no "review
    skipped" message), proving the handler falls through to process the verdict
    so the handoff is preserved. (A truly merged PR — separate assertion below
    via the message — still early-exits.)
    """
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as _AS

    # Token + closed-not-merged PR. get_pull_request is imported from
    # app.github inside _process_post_commit, so patch it there.
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "closed", "merged": False}),
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    # Block any downstream fix-spawn / GitHub side effects so the test focuses on
    # the early-exit branch decision. _spawn_pipeline_task returning (None, ...)
    # lands the verdict path in a benign "fix not spawned" terminus.
    mocker.patch(
        "app.task_lifecycle._spawn_pipeline_task",
        new=AsyncMock(return_value=(None, False)),
    )
    mocker.patch("app.github.create_pr_comment", new=AsyncMock(return_value=None))

    project = await _make_db_project(db_session)
    review_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "review",
                "pr_number": 91,
                "branch": "blueprint/c4",
                "base_branch": "main",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 91,
                "branch": "blueprint/c4",
                "base_branch": "main",
                "iteration": 1,
            }
        },
        latest_status_message="REQUEST_CHANGES",
    )
    review_task.vm_instance_id = f"agent-vm-c4-{uuid.uuid4().hex[:8]}"
    await _persist(db_session, review_task)

    payload = AgentStatusPayload(
        task_id=review_task.id,
        status=TaskStatus.COMPLETED,
        logs="REQUEST_CHANGES",
    )

    _factory = async_sessionmaker(db_session.bind, class_=_AS, expire_on_commit=False)
    await _process_post_commit(review_task.id, payload, _session_factory=_factory)

    async with _factory() as fresh:
        refetched = await fresh.get(Task, review_task.id)
        msg = (refetched.latest_status_message or "").lower()

    assert "review skipped" not in msg, (
        "Closed-not-merged PR must NOT trigger the 'review skipped' early-exit "
        "(C4 fix #1) — that drops the review_approved handoff. Got: "
        f"{refetched.latest_status_message!r}"
    )


@pytest.mark.asyncio
async def test_review_early_exit_fires_when_merged(db_session, mocker):
    """C4 fix #1 (counterpart): a genuinely MERGED PR still early-exits with
    'review skipped' — the merged-only gate must not regress the real skip."""
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as _AS

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "closed", "merged": True}),
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))

    project = await _make_db_project(db_session)
    review_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "review",
                "pr_number": 92,
                "branch": "blueprint/c4m",
                "base_branch": "main",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 92,
                "branch": "blueprint/c4m",
                "base_branch": "main",
                "iteration": 1,
            }
        },
        latest_status_message="APPROVE",
    )
    review_task.vm_instance_id = f"agent-vm-c4m-{uuid.uuid4().hex[:8]}"
    await _persist(db_session, review_task)

    payload = AgentStatusPayload(
        task_id=review_task.id,
        status=TaskStatus.COMPLETED,
        logs="APPROVE",
    )

    _factory = async_sessionmaker(db_session.bind, class_=_AS, expire_on_commit=False)
    await _process_post_commit(review_task.id, payload, _session_factory=_factory)

    async with _factory() as fresh:
        refetched = await fresh.get(Task, review_task.id)
        msg = (refetched.latest_status_message or "").lower()

    assert "already merged" in msg and "review skipped" in msg, (
        "A merged PR must still hit the early-exit ('already merged — review "
        f"skipped'). Got: {refetched.latest_status_message!r}"
    )


# ── C4 fix B/C: merge-success caller BLOCKS on a contended parent ───────────
#
# Fix #3 converted the merge-success orig fetch from a blocking session.get to
# _lock_original_task with skip_locked. Under contention that returned None and
# the ENTIRE `if orig:` block was SKIPPED — permanently dropping ONE-SHOT
# post-merge side effects (hotfix back-merge, security rescan, remediation
# outcome, review-playbook, regression-case advancement, smoketest, and the
# rollup parent's EVALUATING→COMPLETED + pr_merged transition). The merge has
# ALREADY landed on GitHub, so a brief BLOCK is correct: the caller now uses
# skip_locked=False (bounded by lock_timeout). These tests cover the CALLER's
# deferral consequence, not just that the helper returns a row.

_C4_USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


def _mock_merge_success_deps(mocker):
    """Mock GH deps so _process_post_commit's review/APPROVE path reaches the
    merge-success block and merges. base_branch is a NON-integration branch so
    the CI gate is skipped (is_ci_gated_merge=False → ci_status=None)."""
    # All callers here drive an APPROVE verdict straight to the synchronous
    # merge-success path. An APPROVE is always high-stakes, so the default-on
    # second-opinion gate would park the review EVALUATING + spawn a panel
    # challenger before any merge bookkeeping ran — defeating these tests'
    # purpose. Disable the gate so the merge-success path under test runs
    # directly (the gate itself is covered in
    # test_task_lifecycle.py::test_second_opinion_*).
    mocker.patch.dict(
        "app.task_lifecycle._runtime_config",
        {"review_second_opinion_gate_enabled": False},
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    # PR open + not merged, with a head sha (the merge-success path reads it).
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"sha": "deadbeef"},
            }
        ),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "ok"}),
    )
    mocker.patch(
        "app.github.submit_pr_review",
        new=AsyncMock(return_value={"id": 1}),
    )
    mocker.patch("app.github.get_check_runs_cached", new=AsyncMock(return_value=[]))
    mocker.patch("app.github.get_pr_files", new=AsyncMock(return_value=[]))
    mocker.patch("app.github.create_pr_comment", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    # Branch cleanup + playbook touch GitHub/DB — no-op them (they're guarded
    # in the handler anyway; mocking keeps the test focused + fast).
    mocker.patch(
        "app.task_lifecycle._cleanup_branches", new=AsyncMock(return_value=None)
    )
    mocker.patch(
        "app.review_learning.extract_review_feedback_to_playbook",
        new=AsyncMock(return_value=None),
    )


def _mock_small_auto_merge_deps(mocker):
    mocker.patch(
        "app.task_lifecycle.should_auto_approve_diff",
        return_value=(True, "tiny diff"),
    )
    mocker.patch("app.direction_metrics._compute_acceptance_rate", return_value=1.0)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new=AsyncMock(
            return_value={
                "blocked": False,
                "findings": [],
                "summary": "ok",
                "report": "",
            }
        ),
    )
    mocker.patch("app.github.get_pr_files", new=AsyncMock(return_value=[]))
    mocker.patch(
        "app.task_lifecycle._merge_with_retry",
        new=AsyncMock(return_value={"merged": True, "message": "ok"}),
    )
    mocker.patch("app.github.create_pr_comment", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.task_lifecycle._cleanup_branches", new=AsyncMock(return_value=None)
    )


@pytest.mark.skipif(
    not _C4_USE_POSTGRES,
    reason="real FOR UPDATE row contention requires Postgres",
)
@pytest.mark.asyncio
async def test_merge_success_caller_blocks_then_runs_one_shots(db_session, mocker):
    """C4 fix B/C (the core regression): when the rollup parent (orig) row is
    BRIEFLY held FOR UPDATE by a peer during the merge-success bookkeeping, the
    caller must BLOCK (skip_locked=False, bounded lock_timeout) and then run the
    one-shot effects — NOT skip them. We observe the most load-bearing one-shot:
    the parent's EVALUATING→COMPLETED + pr_merged=True transition.

    Pre-fix (skip_locked=True), the contended fetch returned None and the entire
    `if orig:` block was skipped, leaving the parent EVALUATING with no pr_merged
    and dropping back-merge / rescan / remediation effects permanently.
    """
    import asyncio
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as _AS
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload

    _mock_merge_success_deps(mocker)

    project = await _make_db_project(db_session)

    # The rollup PARENT (orig): EVALUATING, will be flipped COMPLETED by the
    # one-shot block.
    parent = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        context={},
    )
    await _persist(db_session, parent)
    parent_id = parent.id

    # The REVIEW task: APPROVE verdict, base_branch is a non-integration branch
    # so the CI gate is skipped, original_task_id points at the parent.
    review_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "review",
                "pr_number": 4242,
                "branch": "research/c4-stg",
                "base_branch": "research/c4-base",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 4242,
                "branch": "research/c4-stg",
                "base_branch": "research/c4-base",
                "original_task_id": str(parent_id),
                "iteration": 1,
                "review_approved": True,
                "ci_passed": True,
            }
        },
        latest_status_message="APPROVE",
    )
    review_task.vm_instance_id = f"agent-vm-c4b-{uuid.uuid4().hex[:8]}"
    await _persist(db_session, review_task)

    payload = AgentStatusPayload(
        task_id=review_task.id,
        status=TaskStatus.COMPLETED,
        logs="APPROVE",
    )

    _factory = async_sessionmaker(db_session.bind, class_=_AS, expire_on_commit=False)

    # Peer session holds the parent row FOR UPDATE, then releases after a short
    # delay — long enough that the caller MUST block, short enough to fit inside
    # the bounded lock_timeout so the caller acquires and runs the one-shots.
    Session = async_sessionmaker(db_session.bind, class_=_AS, expire_on_commit=False)
    _peer_released = asyncio.Event()

    async def _hold_then_release():
        async with Session() as peer:
            # Mirror the existing peer-hold seam (test_lock_window): ORM FOR
            # UPDATE on the Task row (the Postgres table is `task`, mapped via
            # the model — not a raw `tasks` literal).
            held = (
                (
                    await peer.execute(
                        select(Task).where(Task.id == parent_id).with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            assert held is not None
            # Hold long enough that the caller MUST block on the parent row when
            # it reaches the (fast, mocked) merge-success bookkeeping — but well
            # under the caller's 8s lock_timeout so it acquires after release.
            await asyncio.sleep(2.0)
            await peer.rollback()  # releases the FOR UPDATE lock
            _peer_released.set()

    hold_task = asyncio.create_task(_hold_then_release())
    # Give the peer a beat to actually acquire the lock before the caller runs.
    await asyncio.sleep(0.2)

    await _process_post_commit(review_task.id, payload, _session_factory=_factory)

    await hold_task
    assert _peer_released.is_set()

    async with _factory() as fresh:
        refetched_parent = await fresh.get(Task, parent_id)

    assert refetched_parent.status == TaskStatus.COMPLETED, (
        "Merge-success caller must BLOCK on the briefly-contended parent and "
        "then run the one-shot transition — got status "
        f"{refetched_parent.status!r}. A skip_locked DEFER would leave it "
        "EVALUATING and drop all post-merge effects (C4 fix B/C regression)."
    )
    assert (refetched_parent.context or {}).get("pr_merged") is True, (
        "pr_merged=True is the load-bearing one-shot — it must be set, proving "
        "the `if orig:` block executed instead of being skipped."
    )


@pytest.mark.asyncio
async def test_merge_success_caller_runs_one_shots_when_uncontended(db_session, mocker):
    """C4 fix B/C (non-PG baseline): with no contention the merge-success caller
    acquires the parent and runs the one-shot transition. Mirrors the contended
    test's assertions so the happy path is covered on SQLite too (where blocking
    FOR UPDATE returns immediately and there is no real row contention)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as _AS
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload

    _mock_merge_success_deps(mocker)

    project = await _make_db_project(db_session)
    parent = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        context={},
    )
    await _persist(db_session, parent)
    parent_id = parent.id

    review_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "review",
                "pr_number": 4343,
                "branch": "research/c4-stg2",
                "base_branch": "research/c4-base2",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 4343,
                "branch": "research/c4-stg2",
                "base_branch": "research/c4-base2",
                "original_task_id": str(parent_id),
                "iteration": 1,
                "review_approved": True,
                "ci_passed": True,
            }
        },
        latest_status_message="APPROVE",
    )
    review_task.vm_instance_id = f"agent-vm-c4c-{uuid.uuid4().hex[:8]}"
    await _persist(db_session, review_task)

    payload = AgentStatusPayload(
        task_id=review_task.id,
        status=TaskStatus.COMPLETED,
        logs="APPROVE",
    )

    _factory = async_sessionmaker(db_session.bind, class_=_AS, expire_on_commit=False)
    await _process_post_commit(review_task.id, payload, _session_factory=_factory)

    async with _factory() as fresh:
        refetched_parent = await fresh.get(Task, parent_id)

    assert refetched_parent.status == TaskStatus.COMPLETED
    assert (refetched_parent.context or {}).get("pr_merged") is True


@pytest.mark.asyncio
async def test_approve_verdict_retires_improvement_lane(db_session, mocker):
    """When _process_post_commit processes an APPROVE verdict on a review task
    that carries LANE_CHAIN_ID_KEY, it must retire the chain's ImprovementLane
    so the lane reaches a terminal state immediately rather than staying
    ACTIVE/PARKED until the next TTL sweep.

    Regression: retire_chain_lane is called with task.context inside the
    `if verdict == 'APPROVE':` block; removing or bypassing that call would
    leave the lane live indefinitely — not caught by unit tests on
    retire_chain_lane alone."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as _AS
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload
    from app.improvement_lane_ops import create_lane
    from app.models import ImprovementLane, ImprovementLaneStatus
    from app.review_fix_lane_pilot import LANE_CHAIN_ID_KEY

    _mock_merge_success_deps(mocker)

    project = await _make_db_project(db_session)

    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/approve-retire",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.ACTIVE.value
    await db_session.flush()

    parent = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={},
    )
    await _persist(db_session, parent)

    review_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "review",
                "pr_number": 7777,
                "branch": "lane/approve-retire",
                "base_branch": "main",
            }
        ],
        context={
            LANE_CHAIN_ID_KEY: str(lane.id),
            "review_loop": {
                "pr_number": 7777,
                "branch": "lane/approve-retire",
                "base_branch": "main",
                "original_task_id": str(parent.id),
                "iteration": 1,
                "review_approved": True,
                "ci_passed": True,
            },
        },
        latest_status_message="APPROVE",
    )
    review_task.vm_instance_id = f"agent-vm-lane-{uuid.uuid4().hex[:8]}"
    await _persist(db_session, review_task)

    payload = AgentStatusPayload(
        task_id=review_task.id,
        status=TaskStatus.COMPLETED,
        logs="APPROVE",
    )

    _factory = async_sessionmaker(db_session.bind, class_=_AS, expire_on_commit=False)
    await _process_post_commit(review_task.id, payload, _session_factory=_factory)

    async with _factory() as fresh:
        refreshed_lane = await fresh.get(ImprovementLane, lane.id)

    assert refreshed_lane.status == ImprovementLaneStatus.RETIRED.value, (
        f"Lane must be RETIRED after APPROVE verdict, got {refreshed_lane.status}"
    )


@pytest.mark.asyncio
async def test_small_auto_merge_to_distinct_promotion_target_alerts_without_pr_merged(
    db_session, mocker
):
    import asyncio
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as _AS
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload
    from app.models import ArtifactType

    _mock_small_auto_merge_deps(mocker)
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = await _make_db_project(
        db_session,
        integration_branch="automain",
        promotion_target="main",
        directions=[{"id": "dir-1"}],
    )
    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build it", "base_branch": "main"}],
        context={"scope": "small", "direction_id": "dir-1"},
    )
    await _persist(db_session, task)

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        logs="done",
        pr_url="https://github.com/owner/repo/pull/5150",
        artifact_type=ArtifactType.PULL_REQUEST,
    )

    _factory = async_sessionmaker(db_session.bind, class_=_AS, expire_on_commit=False)
    await _process_post_commit(task.id, payload, _session_factory=_factory)
    await asyncio.sleep(0)

    async with _factory() as fresh:
        refetched_task = await fresh.get(Task, task.id)
        alerts = (
            (await fresh.execute(select(Alert).where(Alert.task_id == task.id)))
            .scalars()
            .all()
        )

    assert refetched_task.status == TaskStatus.COMPLETED
    assert (refetched_task.context or {}).get("pr_merged") is None
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert alerts[0].title == "Merged PR landed on promotion target before integration"
    approval_calls = [
        c for c in dispatch_mock.call_args_list if c.args[1] == "APPROVAL_NEEDED"
    ]
    assert len(approval_calls) == 1
    assert approval_calls[0].args[2]["pr_number"] == 5150


@pytest.mark.asyncio
async def test_small_auto_merge_on_single_branch_sets_pr_merged(db_session, mocker):
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as _AS
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload
    from app.models import ArtifactType

    _mock_small_auto_merge_deps(mocker)
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = await _make_db_project(
        db_session,
        integration_branch="main",
        promotion_target="main",
        directions=[{"id": "dir-1"}],
    )
    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build it", "base_branch": "main"}],
        context={"scope": "small", "direction_id": "dir-1"},
    )
    await _persist(db_session, task)

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        logs="done",
        pr_url="https://github.com/owner/repo/pull/5151",
        artifact_type=ArtifactType.PULL_REQUEST,
    )

    _factory = async_sessionmaker(db_session.bind, class_=_AS, expire_on_commit=False)
    await _process_post_commit(task.id, payload, _session_factory=_factory)

    async with _factory() as fresh:
        refetched_task = await fresh.get(Task, task.id)
        alerts = (
            (await fresh.execute(select(Alert).where(Alert.task_id == task.id)))
            .scalars()
            .all()
        )

    assert refetched_task.status == TaskStatus.COMPLETED
    assert (refetched_task.context or {}).get("pr_merged") is True
    assert alerts == []
    approval_calls = [
        c for c in dispatch_mock.call_args_list if c.args[1] == "APPROVAL_NEEDED"
    ]
    assert approval_calls == []


@pytest.mark.asyncio
async def test_review_merge_to_distinct_promotion_target_alerts_without_pr_merged(
    db_session, mocker
):
    import asyncio
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as _AS
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload

    _mock_merge_success_deps(mocker)
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = await _make_db_project(
        db_session,
        integration_branch="automain",
        promotion_target="main",
    )
    parent = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        context={},
    )
    await _persist(db_session, parent)

    review_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "review",
                "pr_number": 6262,
                "branch": "research/promo-stg",
                "base_branch": "main",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 6262,
                "branch": "research/promo-stg",
                "base_branch": "main",
                "original_task_id": str(parent.id),
                "iteration": 1,
                "review_approved": True,
                "ci_passed": True,
            }
        },
        latest_status_message="APPROVE",
    )
    review_task.vm_instance_id = f"agent-vm-promo-{uuid.uuid4().hex[:8]}"
    await _persist(db_session, review_task)

    payload = AgentStatusPayload(
        task_id=review_task.id,
        status=TaskStatus.COMPLETED,
        logs="APPROVE",
    )

    _factory = async_sessionmaker(db_session.bind, class_=_AS, expire_on_commit=False)
    await _process_post_commit(review_task.id, payload, _session_factory=_factory)
    await asyncio.sleep(0)

    async with _factory() as fresh:
        refetched_parent = await fresh.get(Task, parent.id)
        alerts = (
            (await fresh.execute(select(Alert).where(Alert.task_id == parent.id)))
            .scalars()
            .all()
        )

    assert refetched_parent.status == TaskStatus.COMPLETED
    assert (refetched_parent.context or {}).get("pr_merged") is None
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert alerts[0].title == "Merged PR landed on promotion target before integration"
    approval_calls = [
        c for c in dispatch_mock.call_args_list if c.args[1] == "APPROVAL_NEEDED"
    ]
    assert len(approval_calls) == 1
    assert approval_calls[0].args[2]["pr_number"] == 6262


def test_is_custom_staged_rollup_root_true_for_release_candidate_root():
    from app.branch_resolution import is_custom_staged_rollup_root

    project = make_project(
        integration_branch="automain",
        promotion_target="release/main",
        build_config={"staging_branch_prefixes": ["release/"]},
    )
    parent = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        context={"_is_rollup_review": True, "rollup_pr_number": 6264},
    )

    assert is_custom_staged_rollup_root(parent, project, "release/candidate-1") is True


def test_is_custom_staged_rollup_root_false_for_default_research_branch():
    from app.branch_resolution import is_custom_staged_rollup_root

    project = make_project(
        integration_branch="automain",
        promotion_target="release/main",
    )
    parent = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        context={"rollup_pr_number": 4343},
    )

    assert is_custom_staged_rollup_root(parent, project, "research/c4-base") is False


@pytest.mark.asyncio
async def test_review_empty_pr_close_on_intermediate_staged_rollup_stays_evaluating(
    db_session, mocker
):
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as _AS
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "open", "merged": False}),
    )
    mocker.patch("app.github.get_pr_files", new=AsyncMock(return_value=[]))
    mocker.patch(
        "app.github.close_pull_request",
        new=AsyncMock(return_value=True),
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.github.create_pr_comment", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.task_lifecycle._spawn_pipeline_task",
        new=AsyncMock(return_value=(None, False)),
    )
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = await _make_db_project(
        db_session,
        integration_branch="automain",
        promotion_target="release/main",
        build_config={"staging_branch_prefixes": ["release/"]},
    )
    parent = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        context={"rollup_pr_number": 6265},
    )
    await _persist(db_session, parent)

    review_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "review",
                "pr_number": 6265,
                "branch": "release/rollup-stg",
                "base_branch": "release/candidate-1",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 6265,
                "branch": "release/rollup-stg",
                "base_branch": "release/candidate-1",
                "original_task_id": str(parent.id),
                "iteration": 1,
            }
        },
        latest_status_message="REQUEST_CHANGES",
    )
    review_task.vm_instance_id = f"agent-vm-empty-rollup-{uuid.uuid4().hex[:8]}"
    await _persist(db_session, review_task)

    payload = AgentStatusPayload(
        task_id=review_task.id,
        status=TaskStatus.COMPLETED,
        logs="REQUEST_CHANGES",
    )

    _factory = async_sessionmaker(db_session.bind, class_=_AS, expire_on_commit=False)
    await _process_post_commit(review_task.id, payload, _session_factory=_factory)

    async with _factory() as fresh:
        refetched_parent = await fresh.get(Task, parent.id)
        alerts = (
            (await fresh.execute(select(Alert).where(Alert.task_id == parent.id)))
            .scalars()
            .all()
        )

    assert refetched_parent.status == TaskStatus.EVALUATING
    assert (refetched_parent.context or {}).get("pr_closed_empty") is True
    assert (refetched_parent.context or {}).get("pr_merged") is None
    assert "awaiting downstream landing" in (
        refetched_parent.latest_status_message or ""
    )
    assert alerts == []
    approval_calls = [
        c for c in dispatch_mock.call_args_list if c.args[1] == "APPROVAL_NEEDED"
    ]
    assert approval_calls == []


@pytest.mark.asyncio
async def test_review_merge_on_single_branch_sets_pr_merged(db_session, mocker):
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as _AS
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload

    _mock_merge_success_deps(mocker)
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = await _make_db_project(
        db_session,
        integration_branch="main",
        promotion_target="main",
    )
    parent = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        context={},
    )
    await _persist(db_session, parent)

    review_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "review",
                "pr_number": 6263,
                "branch": "research/promo-stg",
                "base_branch": "main",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 6263,
                "branch": "research/promo-stg",
                "base_branch": "main",
                "original_task_id": str(parent.id),
                "iteration": 1,
                "review_approved": True,
                "ci_passed": True,
            }
        },
        latest_status_message="APPROVE",
    )
    review_task.vm_instance_id = f"agent-vm-promo-{uuid.uuid4().hex[:8]}"
    await _persist(db_session, review_task)

    payload = AgentStatusPayload(
        task_id=review_task.id,
        status=TaskStatus.COMPLETED,
        logs="APPROVE",
    )

    _factory = async_sessionmaker(db_session.bind, class_=_AS, expire_on_commit=False)
    await _process_post_commit(review_task.id, payload, _session_factory=_factory)

    async with _factory() as fresh:
        refetched_parent = await fresh.get(Task, parent.id)
        alerts = (
            (await fresh.execute(select(Alert).where(Alert.task_id == parent.id)))
            .scalars()
            .all()
        )

    assert refetched_parent.status == TaskStatus.COMPLETED
    assert (refetched_parent.context or {}).get("pr_merged") is True
    assert alerts == []
    approval_calls = [
        c for c in dispatch_mock.call_args_list if c.args[1] == "APPROVAL_NEEDED"
    ]
    assert approval_calls == []


# ── Early-escalation operator Alerts ──────────────────────────────────────────
#
# The review-fix loop's three early-escalation outcomes (stuck cycle, standoff/
# dispute, repeat-fingerprint halt) must each surface a deduplicated operator
# Alert — the same place cap-exhaustion alerts appear — regardless of whether
# manual approval gates are enabled. See alert_dispatch._maybe_create_escalation_alert.


async def _escalation_alerts(db_session, task_id):
    """Return undismissed review-fix escalation Alert rows tied to a task."""
    rows = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.task_id == task_id,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return [r for r in rows if r.title.startswith("Review-fix escalation:")]


async def _build_stuck_scenario(db_session, *, pr_number, fix_agent_responses=None):
    """Persist an original + source task primed to trip stuck-cycle detection."""
    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    review_loop = {
        "iteration": 1,
        "original_task_id": str(original_task.id),
        "pr_number": pr_number,
        "branch": "b1",
        "base_branch": "main",
        "review_history": [
            {
                "iteration": 0,
                "feedback": "prev feedback",
                "issues": [_issue("null pointer dereference", "CRITICAL")],
            },
        ],
    }
    if fix_agent_responses is not None:
        review_loop["fix_agent_responses"] = fix_agent_responses
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": pr_number,
                "branch": "b1",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={"scope": "medium", "review_loop": review_loop},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)
    return original_task, source_task


async def _trigger_stuck(db_session, source_task, original_task, pr_number):
    return await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=pr_number,
        branch="b1",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Still the same issue",
        current_feedback="Still the same issue",
        current_issues=[_issue("null pointer dereference", "CRITICAL")],
    )


@pytest.mark.asyncio
async def test_stuck_cycle_creates_escalation_alert(db_session, mocker):
    """A stuck cycle fires a deduplicated WARNING Alert tied to the chain
    original, identifying the PR + reason + iteration count."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    original_task, source_task = await _build_stuck_scenario(db_session, pr_number=401)
    result, _ = await _trigger_stuck(db_session, source_task, original_task, 401)
    assert result is None

    alerts = await _escalation_alerts(db_session, original_task.id)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.severity == AlertSeverity.WARNING
    assert "stuck cycle" in alert.title
    assert "PR #401" in alert.title
    assert "1" in alert.message  # iteration count


@pytest.mark.asyncio
async def test_stuck_cycle_escalation_alert_fires_with_gates_off(db_session, mocker):
    """Even with require_approval_gates OFF, the durable Alert must still fire —
    the whole point is surfacing the escalation without operator babysitting."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch.dict(
        "app.shared_state.runtime_config", {"require_approval_gates": False}
    )

    original_task, source_task = await _build_stuck_scenario(db_session, pr_number=402)
    result, _ = await _trigger_stuck(db_session, source_task, original_task, 402)
    assert result is None

    await db_session.refresh(original_task)
    # Gates off → no approval flag, but the Alert still surfaces.
    assert original_task.requires_approval is not True
    alerts = await _escalation_alerts(db_session, original_task.id)
    assert len(alerts) == 1
    assert "stuck cycle" in alerts[0].title
    terminal_outcome = (original_task.context or {}).get("review_loop_terminal_outcome")
    assert terminal_outcome and terminal_outcome.get("kind") == "stuck"
    assert terminal_outcome.get("reason") == "stuck"


@pytest.mark.asyncio
async def test_stuck_cycle_escalation_alert_deduplicated_on_reentry(db_session, mocker):
    """Re-entering the same stuck escalation must NOT create a second Alert."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    original_task, source_task = await _build_stuck_scenario(db_session, pr_number=403)
    await _trigger_stuck(db_session, source_task, original_task, 403)
    await _trigger_stuck(db_session, source_task, original_task, 403)

    alerts = await _escalation_alerts(db_session, original_task.id)
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_standoff_creates_escalation_alert_with_standoff_reason(
    db_session, mocker
):
    """A standoff (fix-agent rationale rejected, issue re-raised) fires an Alert
    labelled with the 'dispute standoff' reason rather than plain 'stuck'."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    # A prior fix-agent response carrying an issue_id makes the repeated issue a
    # standoff (dispute) rather than the agent simply not trying.
    original_task, source_task = await _build_stuck_scenario(
        db_session,
        pr_number=404,
        fix_agent_responses=[{"issue_id": "abc123", "rationale": "intended"}],
    )
    result, _ = await _trigger_stuck(db_session, source_task, original_task, 404)
    assert result is None

    alerts = await _escalation_alerts(db_session, original_task.id)
    assert len(alerts) == 1
    assert "dispute standoff" in alerts[0].title
    assert "PR #404" in alerts[0].title
    terminal_outcome = (original_task.context or {}).get("review_loop_terminal_outcome")
    assert terminal_outcome and terminal_outcome.get("kind") == "standoff"
    assert terminal_outcome.get("reason") == "standoff"


@pytest.mark.asyncio
async def test_repeat_fingerprint_creates_escalation_alert(db_session, mocker):
    """A repeat-fingerprint halt fires a WARNING Alert labelled 'repeat-fingerprint
    halt', tied to the chain original, and deduplicated on re-entry."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)

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
    earlier_fix = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 905,
                "branch": "fix/905",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={"review_loop": {"original_task_id": str(original_task.id)}},
        latest_status_message=f"[2026-05-06T09:11:42] iter=2 vm=agent-vm-aaa {common_failure}",
    )
    most_recent_fix = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 905,
                "branch": "fix/905",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={"review_loop": {"original_task_id": str(original_task.id)}},
        latest_status_message=f"[2026-05-06T09:42:08] iter=3 vm=agent-vm-bbb {common_failure}",
    )
    await _persist(db_session, earlier_fix, most_recent_fix)

    result, was_created = await _spawn_pipeline_task(
        most_recent_fix,
        "review",
        db_session,
        pr_number=905,
        branch="fix/905",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert result is None and was_created is False

    alerts = await _escalation_alerts(db_session, original_task.id)
    assert len(alerts) == 1
    assert "repeat-fingerprint halt" in alerts[0].title
    assert "PR #905" in alerts[0].title
    terminal_outcome = (original_task.context or {}).get("review_loop_terminal_outcome")
    assert (
        terminal_outcome and terminal_outcome.get("kind") == "repeat_fingerprint_halt"
    )
    assert terminal_outcome.get("reason") == "repeat-fingerprint"

    # Re-entry (idempotent halt) must not create a second Alert.
    await _spawn_pipeline_task(
        most_recent_fix,
        "review",
        db_session,
        pr_number=905,
        branch="fix/905",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    alerts = await _escalation_alerts(db_session, original_task.id)
    assert len(alerts) == 1


# ── FIX #1: fix-all-occurrences class detection ──────────────────────────────


class TestDetectFixAllIssueClass:
    def test_two_issues_sharing_stem_is_a_class(self):
        # Same fix applied at several sites → shared normalized stem.
        issues = [
            _issue("Use timezone-aware datetime in services.py", "MAJOR"),
            _issue("Use timezone-aware datetime in reaper.py", "MAJOR"),
        ]
        assert detect_fix_all_issue_class(issues) is True

    def test_single_unrelated_issue_is_not_a_class(self):
        issues = [_issue("Null pointer dereference in handler", "CRITICAL")]
        assert detect_fix_all_issue_class(issues) is False

    def test_two_distinct_issues_are_not_a_class(self):
        # Different stems, no pattern cue → not a file-spanning class.
        issues = [
            _issue("Null pointer dereference in handler", "CRITICAL"),
            _issue("Race condition in the cache layer", "MAJOR"),
        ]
        assert detect_fix_all_issue_class(issues) is False

    def test_pattern_cue_triggers_on_single_issue(self):
        # Reviewer enumerates one site but flags it as one of many.
        issues = [
            _issue(
                "This is one instance of a pattern — other call sites use the "
                "deprecated API too",
                "MAJOR",
            )
        ]
        assert detect_fix_all_issue_class(issues) is True

    def test_all_instances_cue_triggers(self):
        issues = [_issue("Fix all instances of the unguarded dict access", "CRITICAL")]
        assert detect_fix_all_issue_class(issues) is True

    def test_minor_only_does_not_trigger(self):
        # Minor severity is excluded from class detection.
        issues = [
            _issue("Use timezone-aware datetime in a.py", "MINOR"),
            _issue("Use timezone-aware datetime in b.py", "MINOR"),
        ]
        assert detect_fix_all_issue_class(issues) is False

    def test_minor_with_pattern_cue_does_not_trigger(self):
        # The pattern-cue path is gated BEHIND the critical/major severity
        # filter (review_heuristics.detect_fix_all_issue_class), so a minor
        # nit that happens to mention "all instances" must NOT escalate into a
        # repo-wide grep mandate — the docstring's "isolated nit" guarantee.
        issues = [_issue("Fix all instances of this minor style nit", "MINOR")]
        assert detect_fix_all_issue_class(issues) is False

    def test_empty_issues_does_not_trigger(self):
        assert detect_fix_all_issue_class([]) is False
        assert detect_fix_all_issue_class(None) is False

    def test_directive_text_is_nonempty_and_mentions_grep(self):
        d = fix_all_occurrences_directive()
        assert d
        assert "grep" in d.lower()
        assert "in scope" in d.lower()


# ── FIX #3a: early non-convergence detection ─────────────────────────────────


class TestDetectEarlyNonconvergence:
    def _round(self, descs, verdict="REQUEST_CHANGES", severity="CRITICAL"):
        return {
            "verdict": verdict,
            "issues": [_issue(d, severity) for d in descs],
        }

    def test_plateau_low_but_nonzero_escalates(self):
        # [3, 2, 2, 1, 1] shrinks but never reaches 0 — churn MISSES this
        # (decreasing counts) but it grinds to the cap.
        hist = [
            self._round(["a1", "a2", "a3"]),
            self._round(["b1", "b2"]),
            self._round(["c1", "c2"]),
            self._round(["d1"]),
            self._round(["e1"]),
        ]
        should, reason = detect_early_nonconvergence(hist)
        assert should is True
        assert "plateau" in reason

    def test_normally_converging_loop_does_not_escalate(self):
        # Counts decline to ZERO (APPROVE) — a healthy loop must NOT escalate.
        hist = [
            self._round(["a1", "a2", "a3"]),
            self._round(["b1"]),
            self._round([], verdict="APPROVE"),
        ]
        should, _ = detect_early_nonconvergence(hist)
        assert should is False

    def test_verification_only_drift_escalates(self):
        # Same issue re-raised every recent round with NO new descriptions, but
        # an earlier round had a distinct issue so it isn't caught as a plateau
        # of identical text. The last 3 rounds raise nothing new.
        hist = [
            self._round(["leak in foo", "leak in bar"]),
            self._round(["leak in foo"]),
            self._round(["leak in foo"]),
            self._round(["leak in foo"]),
        ]
        should, _reason = detect_early_nonconvergence(hist)
        assert should is True

    def test_one_at_a_time_disclosure_escalates(self):
        # Each round flags a SMALL batch of NEW distinct sites (count > plateau
        # max of 3 so the plateau branch does not also fire), all distinct
        # across rounds → total distinct (16) >> max single-round (4): serial
        # disclosure of one issue-class.
        hist = [
            self._round(["site a1", "site a2", "site a3", "site a4"]),
            self._round(["site b1", "site b2", "site b3", "site b4"]),
            self._round(["site c1", "site c2", "site c3", "site c4"]),
            self._round(["site d1", "site d2", "site d3", "site d4"]),
        ]
        should, reason = detect_early_nonconvergence(hist)
        assert should is True
        assert "disclosure" in reason

    def test_too_few_rounds_does_not_escalate(self):
        hist = [self._round(["a1"]), self._round(["b1"])]
        should, _ = detect_early_nonconvergence(hist)
        assert should is False

    def test_high_steady_count_not_plateau(self):
        # Counts stay high (>3) — not the "low-but-nonzero" plateau shape; the
        # churn detector (non-decreasing) owns this case instead.
        hist = [
            self._round([f"a{i}" for i in range(6)]),
            self._round([f"b{i}" for i in range(6)]),
            self._round([f"c{i}" for i in range(6)]),
            self._round([f"d{i}" for i in range(6)]),
        ]
        should, _ = detect_early_nonconvergence(hist)
        # Distinct total (24) >> max round (6) → caught as one-at-a-time
        # disclosure, which is the correct escalation for 24 distinct issues.
        assert should is True


@pytest.mark.asyncio
async def test_early_nonconvergence_escalates_at_K_blocks_spawn(db_session, mocker):
    """A plateau ([... ,2,2,1,1]) trajectory at iteration K must block the fix
    spawn, flag requires_approval, and set the early-nonconvergence spawn
    reason — instead of grinding to the full cap."""
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

    # Prior rounds form a low-but-nonzero plateau. iteration=K means the current
    # (incoming) round is the K-th — escalation should fire.
    plateau_history = [
        {
            "iteration": 0,
            "verdict": "REQUEST_CHANGES",
            "issues": [_issue("a1"), _issue("a2"), _issue("a3")],
        },
        {
            "iteration": 1,
            "verdict": "REQUEST_CHANGES",
            "issues": [_issue("b1"), _issue("b2")],
        },
        {
            "iteration": 2,
            "verdict": "REQUEST_CHANGES",
            "issues": [_issue("c1"), _issue("c2")],
        },
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 901,
                "branch": "b1",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": EARLY_NONCONVERGENCE_ESCALATE_AT - 1,
                "original_task_id": str(original_task.id),
                "pr_number": 901,
                "branch": "b1",
                "base_branch": "main",
                "review_history": plateau_history,
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Current round: a single low-count issue continues the plateau (→ [..,2,2,1]
    # in the inspected window, all 0 < c <= 3).
    current_issues = [_issue("d1", "CRITICAL")]

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=901,
        branch="b1",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Address the remaining issue",
        current_feedback="One more issue",
        current_issues=current_issues,
    )

    assert result is None, "Early non-convergence at K should block the fix spawn"
    assert created is False
    await db_session.refresh(original_task)
    assert original_task.requires_approval is True
    assert "not converging" in (original_task.latest_status_message or "").lower()
    ctx_src = source_task.context or {}
    assert ctx_src.get(SPAWN_REASON_KEY) == SPAWN_REASON_NONCONVERGENCE


@pytest.mark.asyncio
async def test_converging_loop_does_not_escalate_early(db_session, mocker):
    """A healthy loop whose issue count is collapsing toward zero must NOT be
    escalated early even past iteration K — _spawn_pipeline_task should produce
    a real fix task."""
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

    # A sharply-converging trajectory: big count, then one round, ALL distinct
    # (no repeats) so neither plateau, verify-drift, nor one-at-a-time fires.
    converging_history = [
        {
            "iteration": 0,
            "verdict": "REQUEST_CHANGES",
            "issues": [_issue(f"firstpass-{i}") for i in range(6)],
        },
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 902,
                "branch": "b2",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": EARLY_NONCONVERGENCE_ESCALATE_AT - 1,
                "original_task_id": str(original_task.id),
                "pr_number": 902,
                "branch": "b2",
                "base_branch": "main",
                "review_history": converging_history,
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # One genuinely-new issue this round — not a plateau/drift/disclosure shape.
    current_issues = [_issue("brand-new-distinct-issue", "CRITICAL")]

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=902,
        branch="b2",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Address the new issue",
        current_feedback="A new issue",
        current_issues=current_issues,
    )

    assert result is not None, "Converging loop must NOT be escalated early"
    assert created is True
    ctx_src = source_task.context or {}
    assert ctx_src.get(SPAWN_REASON_KEY) != SPAWN_REASON_NONCONVERGENCE


# ── FIX #3b: preserve work on cap (non-empty, mergeable rollup) ──────────────


def _mock_preservable_pr(mocker, *, changed_files: int, mergeable_state: str):
    """Mock GitHub side effects + get_pull_request for the preserve-work path."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    close_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "open",
            "changed_files": changed_files,
            "mergeable_state": mergeable_state,
        },
    )
    return close_mock


@pytest.mark.asyncio
async def test_cap_preserves_nonempty_mergeable_rollup_pr(db_session, mocker):
    """FIX #3b: capping a NON-EMPTY, conflict-free ROLLUP PR on REQUEST_CHANGES
    leaves the PR OPEN, flags requires_approval, records NO CODE_ERROR, and does
    NOT close the PR."""
    close_mock = _mock_preservable_pr(mocker, changed_files=4, mergeable_state="clean")
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec_task = mocker.patch("app.routers.agent_status.record_task_result")
    rec_dir = mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )

    from app.review_pipeline import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={
            "scope": "medium",
            "_is_rollup_review": True,
            "rollup_pr_number": 610,
            # Genuine non-convergence: latest round has outstanding changes.
            "review_loop": {
                "pr_number": 610,
                "review_history": [
                    {"verdict": "CHANGES_REQUESTED", "issues": [_issue("x")]},
                ],
            },
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 610, "original_task_id": str(orig.id)}],
        context={"review_loop": {"pr_number": 610}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task, str(orig.id), 610, 16, db_session, scope="per-chain"
    )

    await db_session.refresh(orig)
    assert (orig.context or {}).get("failure_detail") == (
        "review_cap_preserved_for_handmerge"
    ), "Preservable rollup PR must be tagged for hand-merge"
    assert (orig.context or {}).get("pr_preserved_for_handmerge") is True
    assert orig.requires_approval is True, "Preserved PR must be flagged for operator"
    assert (orig.context or {}).get("failure_category") != "CODE_ERROR", (
        "Preserving salvageable work must NOT record CODE_ERROR"
    )
    assert (orig.context or {}).get("pr_rejected") is not True
    close_mock.assert_not_called()  # PR LEFT OPEN
    rec_task.assert_not_called()
    rec_dir.assert_not_called()
    alert_mock.assert_awaited()  # operator still alerted


@pytest.mark.asyncio
async def test_preserve_creates_handmerge_attention_message(db_session, mocker):
    """Preserving a non-approved rollup PR creates a durable ProjectMessage (NOTE)
    keyed to the task, carrying the PR number in context/content."""
    from app.models import MessageType, ProjectMessage

    _mock_preservable_pr(mocker, changed_files=4, mergeable_state="clean")
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("app.routers.agent_status.record_task_result")
    mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )

    from app.review_pipeline import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={
            "scope": "medium",
            "_is_rollup_review": True,
            "rollup_pr_number": 711,
            "review_loop": {
                "pr_number": 711,
                "review_history": [
                    {"verdict": "CHANGES_REQUESTED", "issues": [_issue("x")]},
                ],
            },
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 711, "original_task_id": str(orig.id)}],
        context={"review_loop": {"pr_number": 711}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task, str(orig.id), 711, 16, db_session, scope="per-chain"
    )

    await db_session.refresh(orig)
    assert (orig.context or {}).get("pr_preserved_for_handmerge") is True
    assert (orig.context or {}).get("handmerge_pr_number") == 711

    # A durable ProjectMessage (NOTE) must be created.
    msg = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key == f"handmerge:{orig.id}",
            )
        )
    ).scalar_one_or_none()
    assert msg is not None, "A durable handmerge attention message must be created"
    assert msg.message_type == MessageType.NOTE
    assert msg.from_type == "system"
    assert msg.from_task_id == orig.id
    assert msg.status == "open"
    assert (msg.context or {}).get("event_type") == "handmerge_pr_preserved"
    assert (msg.context or {}).get("pr_number") == 711
    assert "711" in msg.content


@pytest.mark.asyncio
async def test_approved_preserve_creates_handmerge_attention_message(db_session, mocker):
    """Preserving an APPROVED rollup PR also creates a durable ProjectMessage keyed
    to the task so the operator has an explicit durable record of the open PR."""
    from app.models import MessageType, ProjectMessage

    _mock_preservable_pr(mocker, changed_files=4, mergeable_state="clean")
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("app.routers.agent_status.record_task_result")
    mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )

    from app.review_pipeline import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build Y", "base_branch": "main"}],
        context={
            "scope": "medium",
            "_is_rollup_review": True,
            "rollup_pr_number": 712,
            "review_loop": {"review_approved": True, "pr_number": 712},
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 712, "original_task_id": str(orig.id)}],
        context={"review_loop": {"review_approved": True, "pr_number": 712}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task, str(orig.id), 712, 16, db_session, scope="per-chain"
    )

    await db_session.refresh(orig)
    assert (orig.context or {}).get("pr_preserved_for_handmerge") is True
    assert (orig.context or {}).get("handmerge_pr_number") == 712

    msg = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key == f"handmerge:{orig.id}",
            )
        )
    ).scalar_one_or_none()
    assert msg is not None, "Approved preserve path must also create handmerge message"
    assert msg.message_type == MessageType.NOTE
    assert msg.from_type == "system"
    assert msg.status == "open"
    assert (msg.context or {}).get("pr_number") == 712
    assert "712" in msg.content


@pytest.mark.asyncio
async def test_preserve_message_idempotent_on_retry(db_session):
    """Re-calling the preserve path (idempotency guard) must not create a duplicate
    ProjectMessage for the same task — the idempotency_key prevents double-insert."""
    from app.models import ProjectMessage
    from app.review_loop_convergence import _create_handmerge_attention_message

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[{"type": "create", "prompt": "Build Z", "base_branch": "main"}],
        context={
            "pr_preserved_for_handmerge": True,
            "handmerge_pr_number": 713,
        },
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    # Call message creation twice to verify idempotency.
    await _create_handmerge_attention_message(orig, 713, 16, "per-chain", db_session)
    await _create_handmerge_attention_message(orig, 713, 16, "per-chain", db_session)

    msgs = list(
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.idempotency_key == f"handmerge:{orig.id}",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs) == 1, "Idempotent creation must not insert a duplicate message"


@pytest.mark.asyncio
async def test_cap_closes_empty_rollup_pr(db_session, mocker):
    """GUARDRAIL: an EMPTY rollup PR (changed_files == 0) is not preservable —
    it keeps today's close + FAIL + CODE_ERROR path."""
    close_mock = _mock_preservable_pr(mocker, changed_files=0, mergeable_state="clean")
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.routers.agent_status.record_task_result")
    mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )

    from app.review_pipeline import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={
            "scope": "medium",
            "_is_rollup_review": True,
            "rollup_pr_number": 611,
            "review_loop": {
                "pr_number": 611,
                "review_history": [
                    {"verdict": "CHANGES_REQUESTED", "issues": [_issue("x")]},
                ],
            },
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 611, "original_task_id": str(orig.id)}],
        context={"review_loop": {"pr_number": 611}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task, str(orig.id), 611, 16, db_session, scope="per-chain"
    )

    await db_session.refresh(orig)
    assert orig.status == TaskStatus.FAILED
    assert (orig.context or {}).get("failure_detail") == "review_cap_exhausted"
    assert (orig.context or {}).get("failure_category") == "CODE_ERROR"
    assert (orig.context or {}).get("pr_rejected") is True
    close_mock.assert_awaited()  # empty PR IS closed


@pytest.mark.asyncio
async def test_cap_closes_conflicted_rollup_pr(db_session, mocker):
    """GUARDRAIL: a genuinely-conflicted rollup PR (mergeable_state == 'dirty')
    cannot be hand-merged, so it keeps the close + FAIL + CODE_ERROR path."""
    close_mock = _mock_preservable_pr(mocker, changed_files=4, mergeable_state="dirty")
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.routers.agent_status.record_task_result")
    mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )

    from app.review_pipeline import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={
            "scope": "medium",
            "_is_rollup_review": True,
            "rollup_pr_number": 612,
            "review_loop": {
                "pr_number": 612,
                "review_history": [
                    {"verdict": "CHANGES_REQUESTED", "issues": [_issue("x")]},
                ],
            },
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 612, "original_task_id": str(orig.id)}],
        context={"review_loop": {"pr_number": 612}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task, str(orig.id), 612, 16, db_session, scope="per-chain"
    )

    await db_session.refresh(orig)
    assert orig.status == TaskStatus.FAILED
    assert (orig.context or {}).get("failure_detail") == "review_cap_exhausted"
    assert (orig.context or {}).get("failure_category") == "CODE_ERROR"
    close_mock.assert_awaited()  # conflicted PR IS closed


@pytest.mark.asyncio
async def test_cap_closes_nonrollup_pr_even_if_mergeable(db_session, mocker):
    """GUARDRAIL: a non-rollup chain PR is not preserved — only rollup PRs
    aggregate already-completed sibling work worth keeping open. A plain create
    task keeps the close + FAIL + CODE_ERROR path."""
    close_mock = _mock_preservable_pr(mocker, changed_files=4, mergeable_state="clean")
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.routers.agent_status.record_task_result")
    mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )

    from app.review_pipeline import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={
            "scope": "medium",
            # No rollup markers.
            "review_loop": {
                "pr_number": 613,
                "review_history": [
                    {"verdict": "CHANGES_REQUESTED", "issues": [_issue("x")]},
                ],
            },
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 613, "original_task_id": str(orig.id)}],
        context={"review_loop": {"pr_number": 613}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task, str(orig.id), 613, 16, db_session, scope="per-chain"
    )

    await db_session.refresh(orig)
    assert orig.status == TaskStatus.FAILED
    assert (orig.context or {}).get("failure_detail") == "review_cap_exhausted"
    close_mock.assert_awaited()


# ── FIX #1: narrow-focus ↔ fix-all reconciliation ────────────────────────────


@pytest.mark.asyncio
async def test_fix_all_directive_reconciles_with_narrow_focus_prefix(
    db_session, mocker
):
    """When a fix_prompt already carries the FIX-ALL-OCCURRENCES directive AND a
    narrow-focus prefix (churn) is injected by _spawn_pipeline_task, the spawned
    prompt must include the reconciliation clause so the narrow-focus 'avoid
    modifying files not referenced by the issues' rule does not silently veto
    migrating the flagged class's sibling sites."""
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

    # Two prior rounds with non-decreasing CRITICAL counts (2 → 2), all DISTINCT
    # descriptions so the stuck-cycle detector does NOT fire but issue-churn DOES
    # (which injects the narrow-focus prefix). The descriptions differ from the
    # current round so this isn't escalated as non-convergence at iteration 1.
    churn_history = [
        {
            "iteration": 0,
            "verdict": "REQUEST_CHANGES",
            "issues": [_issue("alpha one"), _issue("alpha two")],
        },
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 920,
                "branch": "rec1",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 920,
                "branch": "rec1",
                "base_branch": "main",
                "review_history": churn_history,
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Current round: 2 distinct CRITICAL issues → counts [2, 2] non-decreasing →
    # churn → narrow-focus prefix injected. fix_prompt carries the directive.
    current_issues = [_issue("beta one"), _issue("beta two")]
    fix_prompt_with_directive = (
        fix_all_occurrences_directive() + "A code review flagged this PR."
    )

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=920,
        branch="rec1",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt=fix_prompt_with_directive,
        current_feedback="beta feedback",
        current_issues=current_issues,
    )

    assert result is not None and created is True
    prompt = (result.payloads[0] if result.payloads else {}).get("prompt") or ""
    # Narrow-focus prefix is present (churn fired)...
    assert "ISSUE CHURN DETECTED" in prompt
    # ...the fix-all directive survives...
    assert "FIX ALL OCCURRENCES" in prompt
    # ...and the reconciliation clause was appended to the narrow-focus prefix.
    assert "takes precedence" in prompt


@pytest.mark.asyncio
async def test_no_reconcile_clause_without_fix_all_directive(db_session, mocker):
    """When the fix_prompt does NOT carry the fix-all directive, the narrow-focus
    prefix must NOT gain the reconciliation clause (no spurious precedence note)."""
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

    churn_history = [
        {
            "iteration": 0,
            "verdict": "REQUEST_CHANGES",
            "issues": [_issue("alpha one"), _issue("alpha two")],
        },
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 921,
                "branch": "rec2",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 921,
                "branch": "rec2",
                "base_branch": "main",
                "review_history": churn_history,
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    current_issues = [_issue("beta one"), _issue("beta two")]

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=921,
        branch="rec2",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="A code review flagged this PR.",  # no directive
        current_feedback="beta feedback",
        current_issues=current_issues,
    )

    assert result is not None and created is True
    prompt = (result.payloads[0] if result.payloads else {}).get("prompt") or ""
    assert "ISSUE CHURN DETECTED" in prompt  # churn still fired
    assert "FIX ALL OCCURRENCES" not in prompt
    assert "takes precedence" not in prompt


# ── _pr_number_match: None-bind dialect resolution regression ────────────────


def test_pr_number_match_picks_sqlite_branch_on_none_bind_session():
    """Regression for the None-bind dialect footgun (PR #7099 class).

    ``_pr_number_match`` builds a dialect-conditional WHERE clause. A session
    whose ``.bind`` is None still resolves a real engine lazily at execute-time.
    The old idiom (``session.bind.dialect.name if session.bind else "postgresql"``)
    defaulted to the Postgres ``.as_string()`` branch on such a session, emitting
    JSON-path SQL that SQLite cannot run. Routing through ``resolve_dialect_name``
    resolves the dialect via ``get_bind()`` instead, so a None-bind SQLite-backed
    session correctly selects the ``json_extract`` branch.
    """
    from unittest.mock import MagicMock

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.ci_merge_helpers import _pr_number_match

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    try:
        # The footgun condition: .bind is None, but get_bind() resolves SQLite.
        session = MagicMock()
        session.bind = None
        session.get_bind.return_value = engine
        assert session.get_bind().dialect.name == "sqlite"

        expr = _pr_number_match(session, 7099)
        compiled = str(expr).lower()
        assert "json_extract" in compiled, (
            "None-bind SQLite session must select the json_extract branch — "
            "the old idiom defaulted to the Postgres JSON-path branch. "
            f"Got: {compiled}"
        )
        assert "as_string" not in compiled and "->" not in compiled
    finally:
        engine.sync_engine.dispose()


def test_landed_signal_predicate_picks_sqlite_branch_on_none_bind_session():
    """Regression for the None-bind dialect footgun in ``landed_signal``.

    ``landed_signal_predicate`` builds a dialect-conditional predicate (SQLite
    ``json_extract`` vs Postgres ``@>``). It now resolves the dialect through the
    centralised ``resolve_dialect_name`` resolver rather than hand-rolling the
    ``.bind`` → ``get_bind()`` → ``"postgresql"`` logic inline. This asserts a
    None-bind session whose ``get_bind()`` resolves SQLite still selects the
    ``json_extract`` branch (not the Postgres ``@>`` form).
    """
    from unittest.mock import MagicMock

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.landed_signal import landed_signal_predicate

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    try:
        # The footgun condition: .bind is None, but get_bind() resolves SQLite.
        session = MagicMock()
        session.bind = None
        session.get_bind.return_value = engine
        assert session.get_bind().dialect.name == "sqlite"

        expr = landed_signal_predicate(session)
        compiled = str(expr).lower()
        assert "json_extract" in compiled, (
            "None-bind SQLite session must select the json_extract branch — "
            "the old idiom defaulted to the Postgres @> JSONB branch. "
            f"Got: {compiled}"
        )
        assert "@>" not in compiled
    finally:
        engine.sync_engine.dispose()


@pytest.mark.asyncio
async def test_pr_number_match_matches_seeded_pr_number_end_to_end(db_session):
    """Functional companion: against the real test engine, ``_pr_number_match``
    selects exactly the task whose ``payloads[0].pr_number`` equals the target
    and excludes others — proving the chosen branch actually executes and
    matches (on SQLite standalone and on ``--postgres`` alike)."""
    from app.ci_merge_helpers import _pr_number_match

    project = await _make_db_project(db_session)
    target = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "review", "pr_number": 7099}],
    )
    other = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "review", "pr_number": 12345}],
    )
    await _persist(db_session, target, other)

    rows = (
        (
            await db_session.execute(
                select(Task).where(_pr_number_match(db_session, 7099))
            )
        )
        .scalars()
        .all()
    )
    matched_ids = {t.id for t in rows}
    assert target.id in matched_ids
    assert other.id not in matched_ids


# ── _is_verification_only_rejection ────────────────────────────────────────────


class TestIsVerificationOnlyRejection:
    """Pure-unit coverage for the verification-only REQUEST_CHANGES classifier.

    True ONLY when every blocking issue is verification-class (the reviewer
    could not run/verify) AND no blocking issue cites a concrete code defect.
    Conservative bias: anything ambiguous, empty, or defect-bearing → False.
    """

    def _issue(self, desc, severity="major"):
        return {"description": desc, "severity": severity}

    # ── positive: pure verification-class sets downgrade ──────────────────

    def test_could_not_run_check_is_verification_only(self):
        issues = [self._issue("Could not run ./check — command not found in sandbox")]
        assert _is_verification_only_rejection(issues, "") is True

    def test_http_401_probe_is_verification_only(self):
        issues = [self._issue("CI probe returned 401 Unauthorized; cannot verify")]
        assert _is_verification_only_rejection(issues, "") is True

    def test_stale_workspace_install_failure_is_verification_only(self):
        issues = [self._issue("Stale workspace: could not install deps, npm ci failed")]
        assert _is_verification_only_rejection(issues, "") is True

    def test_build_failed_pass_status_is_verification_only(self):
        issues = [self._issue("build failed locally; tsc did not pass in my env")]
        assert _is_verification_only_rejection(issues, "") is True

    def test_feedback_fallback_when_no_structured_issues(self):
        # No issues, but the prose feedback is a pure could-not-verify note.
        assert (
            _is_verification_only_rejection(
                [],
                "Unable to verify — pytest unavailable, the CI check was not verifiable",
            )
            is True
        )

    def test_multiple_verification_issues_all_pass(self):
        issues = [
            self._issue("could not run the test suite"),
            self._issue("unable to verify the build, 403 forbidden on the artifact"),
        ]
        assert _is_verification_only_rejection(issues, "") is True

    # ── negative: any concrete code defect respects the verdict ───────────

    def test_concrete_logic_bug_blocks(self):
        issues = [
            self._issue("Off-by-one error in the loop bound — reads past the array")
        ]
        assert _is_verification_only_rejection(issues, "") is False

    def test_concrete_defect_mixed_with_verification_still_blocks(self):
        # Even if MOST issues are verification-class, a single concrete defect
        # anywhere in the blocking set keeps the verdict.
        issues = [
            self._issue("could not run ./check"),
            self._issue(
                "SQL injection in the query builder — user input is interpolated"
            ),
        ]
        assert _is_verification_only_rejection(issues, "") is False

    def test_defect_and_verify_phrase_in_same_issue_blocks(self):
        # One issue mentions both a build worry AND a real null-deref defect.
        issues = [
            self._issue(
                "build failed but also there is a null pointer deref when payload is empty"
            )
        ]
        assert _is_verification_only_rejection(issues, "") is False

    def test_missing_test_blocks(self):
        issues = [self._issue("Missing test for the error-handling branch")]
        assert _is_verification_only_rejection(issues, "") is False

    def test_broken_invariant_blocks(self):
        issues = [self._issue("This change breaks the invariant that ids are unique")]
        assert _is_verification_only_rejection(issues, "") is False

    def test_race_condition_blocks(self):
        issues = [self._issue("Race condition between the claim and the commit")]
        assert _is_verification_only_rejection(issues, "") is False

    # ── negative: ambiguous / opaque issue is not provably verification-class

    def test_opaque_issue_is_not_verification_only(self):
        issues = [self._issue("Please clean this up")]
        assert _is_verification_only_rejection(issues, "") is False

    def test_one_opaque_among_verification_blocks(self):
        # All-or-nothing: a single non-verification-class issue keeps blocking.
        issues = [
            self._issue("could not run the verify gate"),
            self._issue("rename this variable for clarity"),
        ]
        assert _is_verification_only_rejection(issues, "") is False

    # ── negative: empty / whitespace blocking set ─────────────────────────

    def test_empty_issues_and_empty_feedback_is_false(self):
        assert _is_verification_only_rejection([], "") is False
        assert _is_verification_only_rejection(None, None) is False

    def test_whitespace_only_is_false(self):
        assert _is_verification_only_rejection([self._issue("   ")], "   ") is False

    def test_non_dict_issues_are_skipped(self):
        # Defensive: malformed (non-dict) issue entries are ignored; with no
        # usable structured issue, the prose feedback decides.
        assert (
            _is_verification_only_rejection(
                ["junk", None, 42], "could not verify — ./check not found"
            )
            is True
        )


# ── _genuine_blocking_issue_exists ─────────────────────────────────────────────


class TestGenuineBlockingIssueExists:
    """Pure-unit coverage for the structured-evidence blocker check that drives
    the REQUEST_CHANGES→APPROVE reconciliation.

    True ONLY when a structured issue is BOTH blocking-severity (CRITICAL/MAJOR,
    or a blank/unknown severity that cannot be proven non-blocking) AND not purely
    verification-class. Encodes the review contract: REQUEST_CHANGES requires a
    real CRITICAL/MAJOR defect; MINOR-only / empty / verification-only do not.
    """

    def _issue(self, desc, severity="major"):
        return {"description": desc, "severity": severity}

    # ── False: the cases that reconcile to APPROVE on green CI ────────────

    def test_empty_issues_is_not_a_blocker(self):
        # The exact PR #9678 shape: REQUEST_CHANGES with an empty issues array.
        assert _genuine_blocking_issue_exists([]) is False
        assert _genuine_blocking_issue_exists(None) is False

    def test_only_minor_issues_is_not_a_blocker(self):
        issues = [
            self._issue("Rename this variable for clarity", "minor"),
            self._issue("Add a docstring here", "minor"),
        ]
        assert _genuine_blocking_issue_exists(issues) is False

    def test_minor_synonyms_are_non_blocking(self):
        for sev in ("nit", "nitpick", "low", "info", "trivial", "style", "suggestion"):
            assert (
                _genuine_blocking_issue_exists([self._issue("polish", sev)]) is False
            ), sev

    def test_verification_only_major_is_not_a_blocker(self):
        # Severity is MAJOR but the content is purely could-not-verify: the
        # prompt tells reviewers to flag unverifiable steps as MAJOR, so severity
        # alone cannot distinguish a tooling gap from a real defect.
        issues = [
            self._issue("Could not run ./check — command not found in sandbox"),
            self._issue("CI probe returned 401 Unauthorized; unable to verify"),
        ]
        assert _genuine_blocking_issue_exists(issues) is False

    def test_blank_description_is_skipped(self):
        assert _genuine_blocking_issue_exists([self._issue("   ", "critical")]) is False

    def test_non_dict_entries_are_skipped(self):
        assert _genuine_blocking_issue_exists(["junk", None, 42]) is False

    # ── True: genuine blockers keep REQUEST_CHANGES ───────────────────────

    def test_critical_concrete_defect_is_a_blocker(self):
        issues = [self._issue("SQL injection: user input is interpolated", "critical")]
        assert _genuine_blocking_issue_exists(issues) is True

    def test_major_concrete_defect_is_a_blocker(self):
        issues = [self._issue("Race condition between claim and commit", "major")]
        assert _genuine_blocking_issue_exists(issues) is True

    def test_concrete_defect_among_minors_is_a_blocker(self):
        issues = [
            self._issue("rename for clarity", "minor"),
            self._issue("Off-by-one error reads past the array", "major"),
        ]
        assert _genuine_blocking_issue_exists(issues) is True

    def test_minor_severity_governs_even_for_defect_like_text(self):
        # A defect-like phrase the agent itself classified MINOR is, by the
        # contract's severity semantics, non-blocking — the agent's own
        # classification governs. The reconciliation does not second-guess
        # severity downward.
        issues = [self._issue("null pointer deref when payload is empty", "minor")]
        assert _genuine_blocking_issue_exists(issues) is False

    def test_opaque_major_issue_is_a_blocker(self):
        # Not verification-class, not a recognized defect cue, but CRITICAL/MAJOR:
        # cannot prove non-blocking → keep blocking (fail-safe).
        issues = [self._issue("Please rework this approach", "major")]
        assert _genuine_blocking_issue_exists(issues) is True

    def test_unknown_severity_with_text_is_a_blocker(self):
        issues = [self._issue("something is off here", "")]
        assert _genuine_blocking_issue_exists(issues) is True

    def test_defect_and_verify_phrase_in_same_issue_is_a_blocker(self):
        issues = [
            self._issue(
                "build failed but also a null pointer deref when payload is empty"
            )
        ]
        assert _genuine_blocking_issue_exists(issues) is True

    def test_one_opaque_among_verification_is_a_blocker(self):
        # A single non-verification, non-defect-cue MAJOR issue among
        # verification noise keeps blocking (cannot prove it non-blocking).
        issues = [
            self._issue("could not run the verify gate"),
            self._issue("this needs substantial rework before it can land"),
        ]
        assert _genuine_blocking_issue_exists(issues) is True


@pytest.mark.asyncio
async def test_already_merged_skip_completes_stranded_leaf_origin(db_session, mocker):
    """A leaf write-task reaped mid-run with an open PR is parked EVALUATING and
    handed a review to drive it to completion (reaper open-PR handoff). If the PR
    then merges out-of-band BEFORE that review runs, _spawn_pipeline_task's
    already-merged guard skips the review — and must COMPLETE the stranded
    originating task, or it sits EVALUATING forever and wedges its rollup parent.
    Regression for the merged-but-stuck strand (task 233d0287, 2026-06-05)."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_db_project(db_session)

    # Leaf 'create' task: reaped mid-run, parked EVALUATING, PR merged out-of-band.
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "plan/abc"}],
        context={"scope": "small", "pr_merged": True},
    )
    db_session.add(original_task)
    # The review task the reaper spawned to drive the reaped task to completion.
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 8064,
                "branch": "blueprint/task-x",
                "base_branch": "plan/abc",
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
    await db_session.refresh(original_task)
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=8064,
        branch="blueprint/task-x",
        base_branch="plan/abc",
        original_task_id=str(original_task.id),
    )

    assert result is None and created is False, (
        "Review spawn must be skipped for an already-merged PR"
    )
    # The already-merged guard leaves the COMPLETED transition pending for the
    # caller's commit — commit it (as the rollup caller would) before the
    # refresh, which under real ORM expiry reloads persisted state.
    await db_session.commit()
    await db_session.refresh(original_task)
    assert original_task.status == TaskStatus.COMPLETED, (
        "Stranded leaf originating task must be COMPLETED on the already-merged "
        f"skip, got {original_task.status}"
    )


@pytest.mark.asyncio
async def test_already_merged_skip_leaves_rollup_parent_evaluating(db_session, mocker):
    """The already-merged completion is gated to leaf write-tasks: a rollup-chain
    source must NOT be force-completed here — its intermediate-vs-terminal landing
    is decided by classify_rollup_merge_outcome. It stays EVALUATING."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_db_project(db_session)

    rollup_parent = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "plan", "prompt": "rollup", "base_branch": "research/abc"}],
        # _is_rollup_review marks a rollup-chain source (_is_rollup_chain_source).
        context={"scope": "small", "pr_merged": True, "_is_rollup_review": True},
    )
    db_session.add(rollup_parent)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 8088,
                "branch": "plan/abc",
                "base_branch": "research/abc",
                "original_task_id": str(rollup_parent.id),
            }
        ],
        context={
            "scope": "small",
            "review_loop": {"iteration": 0, "original_task_id": str(rollup_parent.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(rollup_parent)
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=8088,
        branch="plan/abc",
        base_branch="research/abc",
        original_task_id=str(rollup_parent.id),
    )

    assert result is None and created is False
    await db_session.refresh(rollup_parent)
    assert rollup_parent.status == TaskStatus.EVALUATING, (
        "A rollup-chain source must NOT be force-completed by the already-merged "
        f"skip; got {rollup_parent.status}"
    )


@pytest.mark.asyncio
async def test_already_merged_skip_retires_active_lane(db_session, mocker):
    """When the already-merged guard fires, _spawn_pipeline_task must retire the
    chain's ImprovementLane so it reaches a terminal state immediately rather than
    staying ACTIVE and polluting lane metrics until the next TTL sweep.

    Regression: retire_chain_lane is called with source_task.context, so omitting
    LANE_CHAIN_ID_KEY from that context, passing the wrong context, or removing
    the call entirely would all leave the lane ACTIVE after this call — none of
    which would be caught by the unit tests on retire_chain_lane alone."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_db_project(db_session)

    from app.improvement_lane_ops import create_lane
    from app.models import ImprovementLaneStatus
    from app.review_fix_lane_pilot import LANE_CHAIN_ID_KEY

    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/already-merged-retire",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.ACTIVE.value
    await db_session.flush()

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "small", "pr_merged": True},
    )
    db_session.add(original_task)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 9999,
                "branch": "lane/already-merged-retire",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "review_loop": {"iteration": 0, "original_task_id": str(original_task.id)},
            LANE_CHAIN_ID_KEY: str(lane.id),
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(original_task)
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=9999,
        branch="lane/already-merged-retire",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    assert result is None and created is False, (
        "Review spawn must be skipped for an already-merged PR"
    )
    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.RETIRED.value, (
        f"Lane must be RETIRED after already-merged skip, got {lane.status}"
    )


@pytest.mark.asyncio
async def test_pr_closed_unmerged_skips_spawn_and_retires_lane(db_session, mocker):
    """When a chain's PR was closed without merge, _spawn_pipeline_task must skip
    the spawn and retire the chain's ImprovementLane so it reaches RETIRED rather
    than staying PARKED indefinitely."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_db_project(db_session)

    from app.improvement_lane_ops import create_lane
    from app.models import ImprovementLaneStatus
    from app.review_fix_lane_pilot import LANE_CHAIN_ID_KEY

    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="T",
        goal="G",
        branch_name="lane/closed-unmerged",
        base_branch="main",
    )
    lane.status = ImprovementLaneStatus.PARKED.value
    await db_session.flush()

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "small", "pr_closed_unmerged": True},
    )
    db_session.add(original_task)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 9998,
                "branch": "lane/closed-unmerged",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "review_loop": {"iteration": 0, "original_task_id": str(original_task.id)},
            LANE_CHAIN_ID_KEY: str(lane.id),
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(original_task)
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=9998,
        branch="lane/closed-unmerged",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    assert result is None and created is False, (
        "Review spawn must be skipped for a PR closed without merge"
    )
    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.RETIRED.value, (
        f"Lane must be RETIRED after pr_closed_unmerged skip, got {lane.status}"
    )


# ── clear_review_escalation_approval_gate (stale stuck-hold release) ──────────


def _escalation_gated_task(kind="stuck", **ctx_extra):
    ctx = {
        "review_loop_terminal_outcome": {
            "kind": kind,
            "reason": kind,
            "pr_number": 9179,
            "iterations": 4,
            "recorded_at": "2026-06-09T23:29:15",
        },
        "_approval_set_at": "2026-06-09T23:29:15.375394",
        **ctx_extra,
    }
    return make_task(
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context=ctx,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
    )


def test_clear_gate_releases_stuck_hold_and_stamps_resolution():
    from app.review_loop_convergence import clear_review_escalation_approval_gate

    task = _escalation_gated_task(kind="stuck")
    assert clear_review_escalation_approval_gate(task, resolved_by="pr_9179_merged")
    assert task.requires_approval is False
    # The timer baseline must not survive — a later unrelated hold starts fresh.
    assert "_approval_set_at" not in task.context
    outcome = task.context["review_loop_terminal_outcome"]
    assert outcome["resolved"] is True
    assert outcome["resolved_by"] == "pr_9179_merged"
    assert outcome["resolved_at"]
    # The original breadcrumb survives for downstream aggregation.
    assert outcome["kind"] == "stuck"
    assert outcome["iterations"] == 4


@pytest.mark.parametrize(
    "kind", ["standoff", "repeat_fingerprint_halt", "supersession_churn_halt"]
)
def test_clear_gate_releases_other_resolvable_escalation_kinds(kind):
    from app.review_loop_convergence import clear_review_escalation_approval_gate

    task = _escalation_gated_task(kind=kind)
    assert clear_review_escalation_approval_gate(
        task, resolved_by="review_approve_pr_9179"
    )
    assert task.requires_approval is False


def test_clear_gate_never_touches_a_spend_budget_hold():
    from app.review_loop_convergence import clear_review_escalation_approval_gate
    from app.spawn_calibration import SPEND_BUDGET_APPROVAL_HOLD_REASON

    task = _escalation_gated_task(
        kind="stuck", approval_hold_reason=SPEND_BUDGET_APPROVAL_HOLD_REASON
    )
    assert not clear_review_escalation_approval_gate(task, resolved_by="pr_merged")
    assert task.requires_approval is True
    assert task.context.get("_approval_set_at")


def test_clear_gate_never_touches_holds_without_a_resolvable_breadcrumb():
    """Branch-safety / risk-approval holds carry no stuck/standoff terminal
    outcome — the release must leave them alone. cap_exhausted closed the PR
    (terminal), so it is deliberately not resolvable either."""
    from app.review_loop_convergence import clear_review_escalation_approval_gate

    no_outcome = make_task(
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={"_approval_set_at": "2026-06-09T23:29:15"},
        payloads=[{"type": "research", "prompt": "p"}],
    )
    assert not clear_review_escalation_approval_gate(no_outcome, resolved_by="x")
    assert no_outcome.requires_approval is True

    capped = _escalation_gated_task(kind="cap_exhausted")
    assert not clear_review_escalation_approval_gate(capped, resolved_by="x")
    assert capped.requires_approval is True


def test_clear_gate_noop_when_gate_not_set():
    from app.review_loop_convergence import clear_review_escalation_approval_gate

    task = _escalation_gated_task(kind="stuck")
    task.requires_approval = False
    ctx_before = dict(task.context)
    assert not clear_review_escalation_approval_gate(task, resolved_by="x")
    assert task.context == ctx_before


# ── classify_iteration_lane ───────────────────────────────────────────────────


class TestClassifyIterationLane:
    """Unit tests for classify_iteration_lane — pure function, no DB."""

    def _task(self, payload_type: str = "fix", fix_reason: str = "") -> Task:
        ctx: dict = {}
        if fix_reason:
            ctx["review_loop"] = {"fix_reason": fix_reason}
        return make_task(
            payloads=[{"type": payload_type, "pr_number": 1}],
            context=ctx,
        )

    def test_panel_review_is_arbitration(self):
        from app.review_pipeline import classify_iteration_lane, LANE_ARBITRATION

        t = self._task(payload_type="panel_review")
        assert classify_iteration_lane(t) == LANE_ARBITRATION

    def test_merge_conflict_is_conflict(self):
        from app.review_pipeline import classify_iteration_lane, LANE_CONFLICT

        t = self._task(fix_reason="merge_conflict")
        assert classify_iteration_lane(t) == LANE_CONFLICT

    def test_update_branch_failed_is_conflict(self):
        from app.review_pipeline import classify_iteration_lane, LANE_CONFLICT

        t = self._task(fix_reason="update_branch_failed")
        assert classify_iteration_lane(t) == LANE_CONFLICT

    def test_ci_failure_is_ci(self):
        from app.review_pipeline import classify_iteration_lane, LANE_CI

        t = self._task(fix_reason="ci_failure")
        assert classify_iteration_lane(t) == LANE_CI

    def test_code_review_is_review_convergence(self):
        from app.review_pipeline import classify_iteration_lane, LANE_REVIEW_CONVERGENCE

        t = self._task(fix_reason="code_review")
        assert classify_iteration_lane(t) == LANE_REVIEW_CONVERGENCE

    def test_no_fix_reason_is_review_convergence(self):
        from app.review_pipeline import classify_iteration_lane, LANE_REVIEW_CONVERGENCE

        t = self._task()
        assert classify_iteration_lane(t) == LANE_REVIEW_CONVERGENCE

    def test_review_type_is_review_convergence(self):
        from app.review_pipeline import classify_iteration_lane, LANE_REVIEW_CONVERGENCE

        t = self._task(payload_type="review")
        assert classify_iteration_lane(t) == LANE_REVIEW_CONVERGENCE

    def test_panel_review_overrides_fix_reason(self):
        """payload_type=panel_review takes priority regardless of fix_reason."""
        from app.review_pipeline import classify_iteration_lane, LANE_ARBITRATION

        ctx = {"review_loop": {"fix_reason": "ci_failure"}}
        t = make_task(
            payloads=[{"type": "panel_review", "pr_number": 1}],
            context=ctx,
        )
        assert classify_iteration_lane(t) == LANE_ARBITRATION


class TestRecentConsecutiveLaneCount:
    """Unit tests for the persistent-conflict recent segment counter."""

    def _task(self, fix_reason: str, minutes: int) -> Task:
        task = make_task(
            payloads=[{"type": "fix", "pr_number": 1}],
            context={"review_loop": {"fix_reason": fix_reason}},
            status=TaskStatus.COMPLETED,
        )
        task.created_at = datetime(2026, 6, 1, 12, minutes, tzinfo=timezone.utc)
        return task

    def test_counts_only_newest_uninterrupted_lane_segment(self):
        from app.review_pipeline import LANE_CONFLICT

        tasks = [
            self._task("merge_conflict", 1),
            self._task("code_review", 2),
            self._task("merge_conflict", 3),
        ]

        assert _recent_consecutive_lane_count(tasks, LANE_CONFLICT) == 1

    def test_skips_non_cap_counting_tasks_before_segment_break(self):
        from app.review_pipeline import LANE_CONFLICT

        skipped_review = self._task("code_review", 4)
        tasks = [
            self._task("merge_conflict", 1),
            self._task("merge_conflict", 2),
            skipped_review,
        ]

        assert (
            _recent_consecutive_lane_count(
                tasks,
                LANE_CONFLICT,
                counts_toward_cap=lambda task: task is not skipped_review,
            )
            == 2
        )


# ── per-lane sub-cap tests ─────────────────────────────────────────────────────


def _mock_lane_cap_github(mocker):
    """Set up GitHub mocks for lane-cap tests that route through _handle_cap_exhaustion."""
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )


@pytest.mark.asyncio
async def test_conflict_lane_sub_cap_blocks_spawn(db_session, mocker):
    """Conflict-lane sub-cap (default 4): once 4 merge-conflict/update-branch
    fix iterations have run, a 5th merge-conflict spawn is blocked even when the
    global cap has not been reached. The review_convergence lane is unaffected."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    _mock_lane_cap_github(mocker)

    from app.review_pipeline import SPAWN_REASON_CAP
    from app.shared_state import runtime_config

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "p", "base_branch": "main"}],
        context={"scope": "large"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # 4 completed merge-conflict fix iterations — reaches the conflict sub-cap.
    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[
                    {
                        "type": "fix",
                        "pr_number": 9100,
                        "branch": "b",
                        "base_branch": "main",
                    }
                ],
                context={
                    "review_loop": {
                        "fix_reason": "merge_conflict",
                        "original_task_id": str(original_task.id),
                    }
                },
            )
        )

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "fix",
                "pr_number": 9100,
                "branch": "b",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "large",
            "review_loop": {"iteration": 4, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Attempt a 5th merge-conflict spawn — must be blocked by sub-cap.
    # Suppress the persistent-conflict early escalation (threshold > 4) so this
    # test exercises the lane sub-cap path specifically.
    mocker.patch.dict(
        runtime_config,
        {"review_lane_cap_conflict": 4, "review_persistent_conflict_threshold": 10},
    )
    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=9100,
        branch="b",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_reason="merge_conflict",
    )

    assert result is None
    assert created is False
    assert (source_task.context or {}).get(SPAWN_REASON_KEY) == SPAWN_REASON_CAP


@pytest.mark.asyncio
async def test_conflict_lane_sub_cap_rollup_preserves_for_handmerge(db_session, mocker):
    """Conflict-lane sub-cap on a rollup PR → routes to preserve-for-handmerge
    (orig.requires_approval=True, failure_detail='review_cap_preserved_for_handmerge',
    PR left open / not closed)."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    # PR lookup returns a clean, non-empty, open rollup PR — preservable.
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"state": "open", "changed_files": 3, "mergeable_state": "clean"},
    )
    close_pr_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

    from app.review_pipeline import SPAWN_REASON_CAP
    from app.shared_state import runtime_config

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "p", "base_branch": "main"}],
        context={"scope": "large", "_is_rollup_review": True},
        requires_approval=False,
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # 4 completed merge-conflict fix iterations — reaches the conflict sub-cap.
    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[
                    {
                        "type": "fix",
                        "pr_number": 9110,
                        "branch": "b",
                        "base_branch": "main",
                    }
                ],
                context={
                    "review_loop": {
                        "fix_reason": "merge_conflict",
                        "original_task_id": str(original_task.id),
                    }
                },
            )
        )

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "fix",
                "pr_number": 9110,
                "branch": "b",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "large",
            "_is_rollup_review": True,
            "review_loop": {"iteration": 4, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Suppress persistent-conflict early escalation (threshold > 4) so this
    # test exercises the rollup preserve-for-handmerge path at the lane sub-cap.
    mocker.patch.dict(
        runtime_config,
        {"review_lane_cap_conflict": 4, "review_persistent_conflict_threshold": 10},
    )
    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=9110,
        branch="b",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_reason="merge_conflict",
    )

    assert result is None
    assert created is False
    assert (source_task.context or {}).get(SPAWN_REASON_KEY) == SPAWN_REASON_CAP

    # Preserve-for-handmerge path: orig flagged, PR NOT closed.
    await db_session.refresh(original_task)
    assert original_task.requires_approval is True
    assert (original_task.context or {}).get(
        "failure_detail"
    ) == "review_cap_preserved_for_handmerge"
    close_pr_mock.assert_not_called()


@pytest.mark.asyncio
async def test_conflict_lane_exhaustion_does_not_block_review_convergence(
    db_session, mocker
):
    """Conflict lane exhausted → review_convergence spawns are unaffected."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    _mock_lane_cap_github(mocker)

    from app.shared_state import runtime_config

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "p", "base_branch": "main"}],
        context={"scope": "large"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Conflict lane at cap.
    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[
                    {
                        "type": "fix",
                        "pr_number": 9101,
                        "branch": "b",
                        "base_branch": "main",
                    }
                ],
                context={
                    "review_loop": {
                        "fix_reason": "merge_conflict",
                        "original_task_id": str(original_task.id),
                    }
                },
            )
        )

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 9101,
                "branch": "b",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "large",
            "review_loop": {"iteration": 1, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    mocker.patch.dict(runtime_config, {"review_lane_cap_conflict": 4})
    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=9101,
        branch="b",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    # review_convergence lane → not blocked by conflict sub-cap.
    assert result is not None and created is True, (
        "review_convergence spawn must not be blocked by conflict lane exhaustion"
    )


@pytest.mark.asyncio
async def test_arbitration_lane_exhaustion_routes_as_standoff(db_session, mocker):
    """Arbitration lane cap (default 2): once 2 panel_review iterations have run,
    a 3rd panel_review spawn routes to the standoff escalation path (SPAWN_REASON_STUCK)
    rather than the global cap path."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

    from app.review_pipeline import SPAWN_REASON_STUCK
    from app.shared_state import runtime_config

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "p", "base_branch": "main"}],
        context={"scope": "large"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # 2 completed panel_review tasks — arbitration lane at cap.
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[
                    {
                        "type": "panel_review",
                        "pr_number": 9102,
                        "branch": "b",
                        "base_branch": "main",
                    }
                ],
                context={"review_loop": {"original_task_id": str(original_task.id)}},
            )
        )

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "panel_review",
                "pr_number": 9102,
                "branch": "b",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "large",
            "review_loop": {"iteration": 2, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    mocker.patch.dict(runtime_config, {"review_lane_cap_arbitration": 2})
    result, created = await _spawn_pipeline_task(
        source_task,
        "panel_review",
        db_session,
        pr_number=9102,
        branch="b",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    assert result is None
    assert created is False
    # Arbitration exhaustion routes as standoff, not cap.
    assert (source_task.context or {}).get(SPAWN_REASON_KEY) == SPAWN_REASON_STUCK
    # The original task should be marked requires_approval.
    await db_session.refresh(original_task)
    terminal_outcome = (original_task.context or {}).get(
        "review_loop_terminal_outcome"
    ) or {}
    assert terminal_outcome.get("kind") == "standoff"
    assert terminal_outcome.get("reason") == "arbitration_lane_exhausted"


@pytest.mark.asyncio
async def test_non_review_collective_cap_blocks_ci_spawn(db_session, mocker):
    """Non-review collective cap (default 8): with 4 conflict + 4 CI = 8 total
    non-review iterations, a new CI fix spawn is blocked even if the CI sub-cap
    hasn't been individually reached."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    _mock_lane_cap_github(mocker)

    from app.review_pipeline import SPAWN_REASON_CAP
    from app.shared_state import runtime_config

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "p", "base_branch": "main"}],
        context={"scope": "large"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # 4 conflict + 4 CI = 8 non-review iterations.
    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[
                    {
                        "type": "fix",
                        "pr_number": 9103,
                        "branch": "b",
                        "base_branch": "main",
                    }
                ],
                context={
                    "review_loop": {
                        "fix_reason": "merge_conflict",
                        "original_task_id": str(original_task.id),
                    }
                },
            )
        )
    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[
                    {
                        "type": "fix",
                        "pr_number": 9103,
                        "branch": "b",
                        "base_branch": "main",
                    }
                ],
                context={
                    "review_loop": {
                        "fix_reason": "ci_failure",
                        "original_task_id": str(original_task.id),
                    }
                },
            )
        )

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "fix",
                "pr_number": 9103,
                "branch": "b",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "large",
            "review_loop": {"iteration": 8, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    mocker.patch.dict(
        runtime_config, {"review_lane_cap_non_review": 8, "review_lane_cap_ci": 6}
    )
    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=9103,
        branch="b",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_reason="ci_failure",
    )

    assert result is None
    assert created is False
    assert (source_task.context or {}).get(SPAWN_REASON_KEY) == SPAWN_REASON_CAP


@pytest.mark.asyncio
async def test_global_cap_still_bounded_with_lane_churn(db_session, mocker):
    """Even if non-review lane churn doesn't hit sub-caps, the global cap still
    applies. 16 review_convergence iterations on a large-scope PR blocks spawn."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    _mock_lane_cap_github(mocker)

    from app.review_pipeline import SPAWN_REASON_CAP

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "p", "base_branch": "main"}],
        context={"scope": "large"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    for _ in range(16):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[
                    {
                        "type": "review",
                        "pr_number": 9104,
                        "branch": "b",
                        "base_branch": "main",
                    }
                ],
                context={"review_loop": {"original_task_id": str(original_task.id)}},
            )
        )

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "fix",
                "pr_number": 9104,
                "branch": "b",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "large",
            "review_loop": {"iteration": 16, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=9104,
        branch="b",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    assert result is None
    assert created is False
    assert (source_task.context or {}).get(SPAWN_REASON_KEY) == SPAWN_REASON_CAP


@pytest.mark.asyncio
async def test_terminal_outcome_detail_includes_lane_breakdown(db_session, mocker):
    """When the global cap is hit, review_loop_terminal_outcome.detail must
    include the per-lane breakdown string so fix_loop_telemetry can parse it."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    _mock_lane_cap_github(mocker)

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "p", "base_branch": "main"}],
        context={"scope": "large"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # 8 code-review fixes + 4 CI fixes + 4 conflict fixes = 16 total (global cap).
    for _ in range(8):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[
                    {
                        "type": "fix",
                        "pr_number": 9105,
                        "branch": "b",
                        "base_branch": "main",
                    }
                ],
                context={
                    "review_loop": {
                        "fix_reason": "code_review",
                        "original_task_id": str(original_task.id),
                    }
                },
            )
        )
    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[
                    {
                        "type": "fix",
                        "pr_number": 9105,
                        "branch": "b",
                        "base_branch": "main",
                    }
                ],
                context={
                    "review_loop": {
                        "fix_reason": "ci_failure",
                        "original_task_id": str(original_task.id),
                    }
                },
            )
        )
    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[
                    {
                        "type": "fix",
                        "pr_number": 9105,
                        "branch": "b",
                        "base_branch": "main",
                    }
                ],
                context={
                    "review_loop": {
                        "fix_reason": "merge_conflict",
                        "original_task_id": str(original_task.id),
                    }
                },
            )
        )

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "fix",
                "pr_number": 9105,
                "branch": "b",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "large",
            "review_loop": {"iteration": 16, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=9105,
        branch="b",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    await db_session.refresh(original_task)
    terminal = (original_task.context or {}).get("review_loop_terminal_outcome") or {}
    detail = terminal.get("detail", "")
    lane_counts_stored = terminal.get("lane_counts") or {}
    # Detail should carry the lane breakdown.
    assert "conflict=" in detail or lane_counts_stored, (
        f"Terminal outcome detail must include lane breakdown, got detail={detail!r}, "
        f"lane_counts={lane_counts_stored!r}"
    )
    assert lane_counts_stored.get("ci") == 4 or "ci=4" in detail


# ── Issue ledger: contested-region injection ──────────────────────────────────


def _contest_issue(
    description: str,
    file: str = "backend/app/task_evaluators.py",
    line: int = 42,
    severity: str = "CRITICAL",
) -> dict:
    return {
        "description": description,
        "severity": severity,
        "file": file,
        "line": line,
    }


@pytest.mark.asyncio
async def test_contested_region_injects_fix_prompt_prefix(db_session, mocker):
    """When review_history contains a contested region (demanded → applied → differently
    demanded), _spawn_pipeline_task must prepend the CONTESTED-REGIONS prefix to the
    fix task's prompt."""
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

    # iter 0: region A demanded with description D1
    # iter 1: region A absent → APPLIED
    # iter 2: region A reappears with very different description D2 → CONTESTED
    contested_history = [
        {
            "iteration": 0,
            "verdict": "REQUEST_CHANGES",
            "issues": [_contest_issue("replace old constants with new enum")],
        },
        {
            "iteration": 1,
            "verdict": "REQUEST_CHANGES",
            "issues": [_issue("unrelated issue on different file")],
        },
        {
            "iteration": 2,
            "verdict": "REQUEST_CHANGES",
            "issues": [_contest_issue("revert to original frozenset constants")],
        },
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 9400,
                "branch": "contested1",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 2,
                "original_task_id": str(original_task.id),
                "pr_number": 9400,
                "branch": "contested1",
                "base_branch": "main",
                "review_history": contested_history,
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # current_issues uses a different description to avoid triggering the
    # stuck-cycle detector (which compares last 2 history entries).
    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=9400,
        branch="contested1",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Fix these issues.",
        current_feedback="revert to original frozenset constants",
        current_issues=[_issue("add missing type annotation")],
    )

    assert result is not None and created is True
    prompt = (result.payloads[0] if result.payloads else {}).get("prompt") or ""
    assert "CONTESTED" in prompt
    # Contested prefix should be first (topmost)
    assert prompt.index("CONTESTED") < prompt.index("Fix these issues.")


@pytest.mark.asyncio
async def test_contested_region_appends_review_prompt_guidance(db_session, mocker):
    """When contested regions exist, the review task's original_prompt gets the
    CONTESTED-REGIONS appendix directing the reviewer to reconcile prior demands."""
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

    # Descriptions must have Jaccard < 0.5 to trigger CONTESTED (not RE_DEMANDED).
    # "remove deprecated call site" vs "add back original method": 0 common tokens.
    contested_history = [
        {
            "iteration": 0,
            "verdict": "REQUEST_CHANGES",
            "issues": [_contest_issue("remove deprecated call site")],
        },
        {
            "iteration": 1,
            "verdict": "REQUEST_CHANGES",
            "issues": [_issue("unrelated issue on different file")],
        },
        {
            "iteration": 2,
            "verdict": "REQUEST_CHANGES",
            "issues": [_contest_issue("add back original method implementation")],
        },
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 9401,
                "branch": "contested2",
                "base_branch": "automain",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 3,
                "original_task_id": str(original_task.id),
                "pr_number": 9401,
                "branch": "contested2",
                "base_branch": "automain",
                "review_history": contested_history,
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=9401,
        branch="contested2",
        base_branch="automain",
        original_task_id=str(original_task.id),
        original_prompt="Review this PR for correctness.",
    )

    assert result is not None and created is True
    original_prompt = (result.payloads[0] if result.payloads else {}).get(
        "original_prompt"
    ) or ""
    assert "CONTESTED" in original_prompt
    # Reconcile language and base-diff instruction must be present
    assert "reconcil" in original_prompt.lower() or "coexist" in original_prompt.lower()
    assert "git diff" in original_prompt
    assert "origin/automain" in original_prompt


@pytest.mark.asyncio
async def test_contested_ledger_serialized_into_review_loop(db_session, mocker):
    """When contested regions exist, review_loop['issue_ledger'] is written with
    the contested list so future spawns can access the full oscillation history."""
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

    contested_history = [
        {
            "iteration": 0,
            "verdict": "REQUEST_CHANGES",
            "issues": [_contest_issue("use list instead of frozenset")],
        },
        {
            "iteration": 1,
            "verdict": "REQUEST_CHANGES",
            "issues": [_issue("unrelated issue")],
        },
        {
            "iteration": 2,
            "verdict": "REQUEST_CHANGES",
            "issues": [_contest_issue("revert back to frozenset")],
        },
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 9402,
                "branch": "contested3",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 2,
                "original_task_id": str(original_task.id),
                "pr_number": 9402,
                "branch": "contested3",
                "base_branch": "main",
                "review_history": contested_history,
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=9402,
        branch="contested3",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Fix.",
        current_feedback="revert back to frozenset",
        # current_issues is a new issue (different from the last history entry)
        # to avoid triggering the stuck-cycle detector.
        current_issues=[_issue("update docstring format")],
    )

    assert result is not None and created is True
    spawned_loop = (result.context or {}).get("review_loop", {})
    ledger = spawned_loop.get("issue_ledger")
    assert ledger is not None
    assert len(ledger["contested"]) >= 1
    c = ledger["contested"][0]
    assert c["file"] == "backend/app/task_evaluators.py"
    assert (
        "frozenset" in c["demanded_description"]
        or "frozenset" in c["reappeared_description"]
    )


@pytest.mark.asyncio
async def test_no_contested_regions_no_ledger_in_review_loop(db_session, mocker):
    """When review_history has no contested regions, review_loop must NOT contain
    an 'issue_ledger' key (avoid polluting context with empty dicts)."""
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

    normal_history = [
        {
            "iteration": 0,
            "verdict": "REQUEST_CHANGES",
            "issues": [_issue("fix null check")],
        },
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 9403,
                "branch": "clean1",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 9403,
                "branch": "clean1",
                "base_branch": "main",
                "review_history": normal_history,
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=9403,
        branch="clean1",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Fix null check.",
        current_feedback="fix null check",
        # Different description from the history entry to avoid stuck-cycle detection.
        current_issues=[_issue("add type annotation")],
    )

    assert result is not None and created is True
    spawned_loop = (result.context or {}).get("review_loop", {})
    assert "issue_ledger" not in spawned_loop


# ── Reviewer-model pin ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reviewer_model_pin_stamped_on_first_review(db_session, mocker):
    """First review spawn in a chain with a type_model stamps reviewer_model
    into review_loop so subsequent reviews reuse the same model."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    from app.shared_state import runtime_config

    runtime_config["task_type_models"] = {"review": "claude-opus-pinned"}
    runtime_config["pin_reviewer_model"] = True
    try:
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
            status=TaskStatus.COMPLETED,
            payloads=[
                {
                    "type": "create",
                    "pr_number": 9410,
                    "branch": "pin1",
                    "base_branch": "main",
                    "original_task_id": str(original_task.id),
                }
            ],
            context={
                "scope": "medium",
                "review_loop": {
                    "iteration": 0,
                    "original_task_id": str(original_task.id),
                    "pr_number": 9410,
                    "branch": "pin1",
                    "base_branch": "main",
                },
            },
        )
        db_session.add(source_task)
        await db_session.commit()
        await db_session.refresh(source_task)

        result, created = await _spawn_pipeline_task(
            source_task,
            "review",
            db_session,
            pr_number=9410,
            branch="pin1",
            base_branch="main",
            original_task_id=str(original_task.id),
        )

        assert result is not None and created is True
        spawned_loop = (result.context or {}).get("review_loop", {})
        assert spawned_loop.get("reviewer_model") == "claude-opus-pinned"
        # Payload model should also be set
        model_in_payload = (result.payloads[0] if result.payloads else {}).get("model")
        assert model_in_payload == "claude-opus-pinned"
    finally:
        runtime_config.pop("task_type_models", None)
        runtime_config.pop("pin_reviewer_model", None)


@pytest.mark.asyncio
async def test_reviewer_model_pin_reused_on_subsequent_review(db_session, mocker):
    """When source_loop already has a reviewer_model, subsequent review spawns
    reuse that model even if the runtime config has a different model set."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    from app.shared_state import runtime_config

    runtime_config["task_type_models"] = {"review": "claude-new-model"}
    runtime_config["pin_reviewer_model"] = True
    try:
        project = await _make_db_project(db_session)
        original_task = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create", "prompt": "Build", "base_branch": "main"}],
        )
        db_session.add(original_task)
        await db_session.commit()
        await db_session.refresh(original_task)

        # source_loop already has reviewer_model from iter 1's review spawn
        source_task = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[
                {
                    "type": "fix",
                    "pr_number": 9411,
                    "branch": "pin2",
                    "base_branch": "main",
                    "original_task_id": str(original_task.id),
                }
            ],
            context={
                "scope": "medium",
                "review_loop": {
                    "iteration": 1,
                    "original_task_id": str(original_task.id),
                    "pr_number": 9411,
                    "branch": "pin2",
                    "base_branch": "main",
                    "reviewer_model": "claude-original-pinned",
                },
            },
        )
        db_session.add(source_task)
        await db_session.commit()
        await db_session.refresh(source_task)

        result, created = await _spawn_pipeline_task(
            source_task,
            "review",
            db_session,
            pr_number=9411,
            branch="pin2",
            base_branch="main",
            original_task_id=str(original_task.id),
            current_feedback="prev feedback",
        )

        assert result is not None and created is True
        spawned_loop = (result.context or {}).get("review_loop", {})
        # Must reuse the pinned model, NOT the new runtime config model
        assert spawned_loop.get("reviewer_model") == "claude-original-pinned"
        model_in_payload = (result.payloads[0] if result.payloads else {}).get("model")
        assert model_in_payload == "claude-original-pinned"
    finally:
        runtime_config.pop("task_type_models", None)
        runtime_config.pop("pin_reviewer_model", None)


@pytest.mark.asyncio
async def test_reviewer_model_pin_disabled_via_config(db_session, mocker):
    """When pin_reviewer_model=False, source_loop's reviewer_model is NOT used
    and review_loop does not stamp reviewer_model."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    from app.shared_state import runtime_config

    runtime_config["task_type_models"] = {"review": "claude-new-model"}
    runtime_config["pin_reviewer_model"] = False
    try:
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
            status=TaskStatus.COMPLETED,
            payloads=[
                {
                    "type": "fix",
                    "pr_number": 9412,
                    "branch": "pin3",
                    "base_branch": "main",
                    "original_task_id": str(original_task.id),
                }
            ],
            context={
                "scope": "medium",
                "review_loop": {
                    "iteration": 1,
                    "original_task_id": str(original_task.id),
                    "pr_number": 9412,
                    "branch": "pin3",
                    "base_branch": "main",
                    "reviewer_model": "claude-old-pinned",
                },
            },
        )
        db_session.add(source_task)
        await db_session.commit()
        await db_session.refresh(source_task)

        result, created = await _spawn_pipeline_task(
            source_task,
            "review",
            db_session,
            pr_number=9412,
            branch="pin3",
            base_branch="main",
            original_task_id=str(original_task.id),
            current_feedback="prev feedback",
        )

        assert result is not None and created is True
        spawned_loop = (result.context or {}).get("review_loop", {})
        # Pin disabled → no reviewer_model stamped by review spawn
        assert spawned_loop.get("reviewer_model") is None
        # Payload uses the current runtime model, not the pinned one
        model_in_payload = (result.payloads[0] if result.payloads else {}).get("model")
        assert model_in_payload == "claude-new-model"
    finally:
        runtime_config.pop("task_type_models", None)
        runtime_config.pop("pin_reviewer_model", None)


@pytest.mark.asyncio
async def test_reviewer_model_carried_forward_in_fix_spawn(db_session, mocker):
    """Fix spawns carry reviewer_model forward into review_loop so the next
    review spawn can find it."""
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

    # Review task with reviewer_model stamped
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": 9413,
                "branch": "pin4",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "pr_number": 9413,
                "branch": "pin4",
                "base_branch": "main",
                "reviewer_model": "claude-pinned-abc",
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=9413,
        branch="pin4",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Fix the issue.",
        current_feedback="fix this",
        current_issues=[_issue("fix this")],
    )

    assert result is not None and created is True
    spawned_loop = (result.context or {}).get("review_loop", {})
    # Fix spawn carries reviewer_model forward for the next review to find
    assert spawned_loop.get("reviewer_model") == "claude-pinned-abc"


# ── Stale-head cap exemption ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_head_reviews_dont_count_toward_global_cap(db_session, mocker):
    """Review tasks completed with _review_stale_head=True in their context were
    discarded because their verdict was against an older head — they did no useful
    review work and must not consume iteration budget."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "p", "base_branch": "main"}],
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
                "pr_number": 9901,
                "branch": "feat/stale",
                "base_branch": "main",
            }
        ],
        context={"scope": "small", "review_loop": {"iteration": 0}},
    )
    db_session.add(source_task)

    # 12 review tasks marked stale-head (cap is 10 for small scope — would block
    # if counted). All are COMPLETED.
    for _ in range(12):
        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[
                {
                    "type": "review",
                    "pr_number": 9901,
                    "branch": "feat/stale",
                    "base_branch": "main",
                }
            ],
            context={"_review_stale_head": True},
        )
        db_session.add(t)

    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=9901,
        branch="feat/stale",
        base_branch="main",
        fix_prompt="Fix",
    )
    assert result is not None, "Stale-head review tasks must not count toward the cap"
    assert result.status == TaskStatus.QUEUED


# ── Mid-retry defer ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mid_retry_defer_defers_review_when_fix_infra_failed_in_grace_window(
    db_session, mocker
):
    """When a same-PR fix is FAILED with an infra failure_category within the 20-min
    grace window AND has retry budget remaining (resurrection not yet spent), spawning
    a review must be deferred so the fix can be resurrected first."""
    from app.review_pipeline import SPAWN_REASON_DEFERRED

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

    # A fix that died of AGENT_TIMEOUT within the grace window, resurrection not yet spent.
    failed_fix = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        updated_at=__import__("datetime").datetime.now(timezone.utc).replace(tzinfo=None)
        - __import__("datetime").timedelta(minutes=5),
        payloads=[
            {
                "type": "fix",
                "pr_number": 9902,
                "branch": "feat/mid-retry",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "failure_category": "AGENT_TIMEOUT",
            "_resurrection_count": 0,
            "review_loop": {"original_task_id": str(original_task.id)},
        },
    )
    db_session.add(failed_fix)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[
            {
                "type": "create",
                "pr_number": 9902,
                "branch": "feat/mid-retry",
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
        "review",
        db_session,
        pr_number=9902,
        branch="feat/mid-retry",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    assert result is None, (
        "Review spawn must be deferred during fix mid-retry grace window"
    )
    assert created is False
    assert (source_task.context or {}).get(SPAWN_REASON_KEY) == SPAWN_REASON_DEFERRED


@pytest.mark.asyncio
async def test_mid_retry_defer_allows_review_when_fix_outside_grace_window(
    db_session, mocker
):
    """Outside the 20-min grace window the review spawn proceeds — no deadlock."""
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

    # Fix failed >20 min ago — outside the grace window (filtered out by SQL updated_at check).
    old_failed_fix = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        updated_at=__import__("datetime").datetime.now(timezone.utc).replace(tzinfo=None)
        - __import__("datetime").timedelta(minutes=25),
        payloads=[
            {
                "type": "fix",
                "pr_number": 9903,
                "branch": "feat/old-fail",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "failure_category": "AGENT_TIMEOUT",
            "_resurrection_count": 0,
            "review_loop": {"original_task_id": str(original_task.id)},
        },
    )
    db_session.add(old_failed_fix)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[
            {
                "type": "create",
                "pr_number": 9903,
                "branch": "feat/old-fail",
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
        "review",
        db_session,
        pr_number=9903,
        branch="feat/old-fail",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    assert result is not None, (
        "Review spawn must proceed when fix failure is outside grace window"
    )
    assert created is True


@pytest.mark.asyncio
async def test_mid_retry_defer_allows_review_when_budget_exhausted(db_session, mocker):
    """When the fix's resurrection budget is exhausted (_resurrection_count >= 1),
    the review spawn proceeds — no deadlock."""
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

    # Fix failed within grace window but resurrection budget exhausted (_resurrection_count >= 1).
    exhausted_fix = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        updated_at=__import__("datetime").datetime.now(timezone.utc).replace(tzinfo=None)
        - __import__("datetime").timedelta(minutes=5),
        payloads=[
            {
                "type": "fix",
                "pr_number": 9904,
                "branch": "feat/exhausted",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "failure_category": "AGENT_TIMEOUT",
            "_resurrection_count": 1,  # budget spent (cap is 1)
            "review_loop": {"original_task_id": str(original_task.id)},
        },
    )
    db_session.add(exhausted_fix)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[
            {
                "type": "create",
                "pr_number": 9904,
                "branch": "feat/exhausted",
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
        "review",
        db_session,
        pr_number=9904,
        branch="feat/exhausted",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    assert result is not None, (
        "Review spawn must proceed when fix resurrection budget is exhausted"
    )
    assert created is True


# ── Review-fix lane pilot (flag-gated, self-repo only) ───────────────────────
#
# These exercise the wiring added in app.review_fix_lane_pilot + the
# _spawn_pipeline_task hook. The pilot lane-izes ONLY the fix side; the review/
# audit path must stay cold and independent (gate integrity). With the flag off
# every chain runs the classic path unchanged.


def _lane_pilot_on(mocker):
    """Enable the lane pilot + substrate for the duration of a test."""
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"improvement_lanes_enabled": True, "review_fix_lane_pilot_enabled": True},
        clear=False,
    )


def _lane_shadow_on(mocker):
    """Enable checkpoint-only shadow mode for improvement lanes."""
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"improvement_lanes_enabled": True, "review_fix_lane_pilot_enabled": False},
        clear=False,
    )


@pytest.mark.asyncio
async def test_lane_pilot_fix_side_only_review_stays_cold(db_session, mocker):
    """Lane-arm chain: implement completion checkpoints + reserves the warm
    member and spawns the review COLD (no lane context, warm handoff
    suppressed, never on the lane-reserved VM); REQUEST_CHANGES spawns a fix
    that carries the lane id, is marked a lane phase, resumes warm, and records
    warm-resume + checkpoint USE receipts. The reviewer is never lane-ized."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    _lane_pilot_on(mocker)
    # SHA reachability is verified before every checkpoint; return True so the
    # happy path proceeds without a real GitHub API call.
    mocker.patch(
        "app.github_token_resolver.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github_refs.is_ancestor_of",
        new_callable=AsyncMock,
        return_value=True,
    )

    from app.models import (
        ImprovementLane,
        ImprovementLaneStatus,
        PoolVm,
        PoolVmState,
    )
    from app.review_fix_lane_pilot import (
        LANE_CHAIN_ID_KEY,
        LANE_PILOT_ASSIGNMENT_KEY,
        LANE_PILOT_CHECKPOINT_RECEIPT_KEY,
        LANE_PILOT_WARM_RESUME_RECEIPT_KEY,
    )

    project = await _make_db_project(db_session, is_self_repo=True)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "medium"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # The implement (create) phase just completed on a warm pool member.
    vm_id = "pool-vm-lane-1"
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        vm_instance_id=vm_id,
        payloads=[
            {
                "type": "create",
                "pr_number": 321,
                "branch": "feat/lane",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "head_sha": "abc1234def5678",
            # Intent-to-treat: this chain is in the lane arm.
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            "review_loop": {
                "iteration": 0,
                "original_task_id": str(original_task.id),
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)
    member = PoolVm(
        vm_instance_id=vm_id,
        project_id=project.id,
        state=PoolVmState.LEASED,
        leased_task_id=source_task.id,
    )
    db_session.add(member)
    await db_session.commit()

    # ── Implement completion → COLD review ──
    review, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=321,
        branch="feat/lane",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert created is True
    assert review is not None
    rctx = review.context or {}
    # Reviewer carries NO lane dispatch directives — cold + independent.
    assert "improvement_lane_id" not in rctx
    assert rctx.get("is_lane_phase") is None
    assert "lane_checkpoint_sha" not in rctx
    # Warm handoff suppressed: a real cold VM name was assigned and the review
    # is NOT pinned to the (now lane-reserved) implementer VM.
    assert review.vm_instance_id is not None
    assert review.vm_instance_id != vm_id
    assert rctx.get("warm_target_vm_id") is None
    # Arm + chain-lane id ride forward (inert to dispatch).
    assert rctx.get(LANE_PILOT_ASSIGNMENT_KEY) == "lane"
    assert rctx.get(LANE_CHAIN_ID_KEY)

    # ── Lane checkpointed + workspace WARM_RESERVED + member reserved ──
    lane_id = uuid.UUID(str(rctx[LANE_CHAIN_ID_KEY]))
    lane = await db_session.get(ImprovementLane, lane_id)
    assert lane is not None
    assert lane.status == ImprovementLaneStatus.PARKED.value
    assert lane.last_checkpoint_sha == "abc1234def5678"
    assert lane.workspace_state == "WARM_RESERVED"
    await db_session.refresh(member)
    assert member.state == PoolVmState.IDLE
    assert member.reserved_lane_id == lane_id

    # ── REQUEST_CHANGES → FIX resumes warm on the lane ──
    fix, fcreated = await _spawn_pipeline_task(
        review,
        "fix",
        db_session,
        pr_number=321,
        branch="feat/lane",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Address the review feedback",
        current_feedback="Tighten the null guard",
        fix_reason="code_review",
    )
    assert fcreated is True
    assert fix is not None
    fctx = fix.context or {}
    # Fix side IS lane-ized: warm-resume directives present.
    assert fctx.get("improvement_lane_id") == str(lane_id)
    assert fctx.get("is_lane_phase") is True
    assert fctx.get("lane_checkpoint_sha") == "abc1234def5678"
    # USE receipts are NOT set at spawn time — they are stamped only at the
    # warm-claim site (_finalize_warm_claim) so cold-fallback tasks carry no
    # false receipt.
    assert LANE_PILOT_WARM_RESUME_RECEIPT_KEY not in fctx
    assert LANE_PILOT_CHECKPOINT_RECEIPT_KEY not in fctx
    assert fctx.get(LANE_PILOT_ASSIGNMENT_KEY) == "lane"
    # The lane resumed ACTIVE for the fix phase.
    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.ACTIVE.value
    assert lane.current_task_id == fix.id

    # ── Warm-resumed fix completion -> COLD re-review ──
    review.status = TaskStatus.COMPLETED
    # Simulate what _finalize_warm_claim writes: warm-resume receipts are set
    # only at actual warm claim, not at spawn time.  The fix ran warm (the lane
    # reservation was intact), so both receipts are now True.
    fix.context = {
        **fctx,
        "head_sha": "def5678abc1234",
        "review_loop": {"iteration": 1, "original_task_id": str(original_task.id)},
        LANE_PILOT_WARM_RESUME_RECEIPT_KEY: True,
        LANE_PILOT_CHECKPOINT_RECEIPT_KEY: True,
    }
    fix.status = TaskStatus.EVALUATING
    db_session.add(review)
    db_session.add(fix)
    await db_session.commit()
    await db_session.refresh(fix)

    rereview, rcreated = await _spawn_pipeline_task(
        fix,
        "review",
        db_session,
        pr_number=321,
        branch="feat/lane",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert rcreated is True
    assert rereview is not None
    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.PARKED.value
    assert lane.last_checkpoint_sha == "def5678abc1234"
    assert lane.checkpoints[-1]["warm_resume"] is True


@pytest.mark.asyncio
async def test_lane_checkpoint_missing_under_budget_spawns_recovery_instead_of_review(
    db_session, mocker
):
    """Lane-arm review boundary with a missing/unverifiable checkpoint, under the
    requeue budget: the cold review is SUPPRESSED and a lane-aware recovery of the
    failed phase is requeued instead.

    The recovery task carries ONLY durable chain metadata (lane chain id, arm,
    review_loop, recovery reason) and NONE of the stale warm-resume directives
    (no improvement_lane_id, no lane_checkpoint_sha, no warm-handoff pin). The
    active lane is rebound to the recovery task, phase_requeues is incremented,
    and a visible recovery reason is recorded on the lane manifest. No review task
    is created. Exercises checkpoint_or_recover_phase ->
    _handle_lane_checkpoint_recovery -> rebind_lane_to_recovery.
    """
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    _lane_pilot_on(mocker)
    mocker.patch(
        "app.github_token_resolver.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    # SHA NOT reachable on the lane branch → checkpoint degrades to missing.
    mocker.patch(
        "app.github_refs.is_ancestor_of",
        new_callable=AsyncMock,
        return_value=False,
    )

    from app.models import ImprovementLane, ImprovementLaneStatus
    from app.review_pipeline import SPAWN_REASON_LANE_RECOVERY
    from app.review_fix_lane_pilot import (
        LANE_CHAIN_ID_KEY,
        LANE_PILOT_ASSIGNMENT_KEY,
        LANE_RECOVERY_ATTEMPT_KEY,
        LANE_RECOVERY_REASON_KEY,
    )

    project = await _make_db_project(db_session, is_self_repo=True)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "medium"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        vm_instance_id="vm-lane-recover-1",
        payloads=[
            {
                "type": "create",
                "pr_number": 510,
                "branch": "feat/lane-recover",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "head_sha": "deadbeef12345678",
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            "review_loop": {
                "iteration": 0,
                "original_task_id": str(original_task.id),
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # Implement completion → review boundary, but the checkpoint is unverifiable.
    recovery, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=510,
        branch="feat/lane-recover",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    # A recovery task was created in place of the review.
    assert created is True
    assert recovery is not None
    assert recovery.first_payload.get("type") == "fix"
    assert recovery.id != source_task.id

    rctx = recovery.context or {}
    # Durable chain metadata rides forward.
    assert rctx.get(LANE_PILOT_ASSIGNMENT_KEY) == "lane"
    lane_id_raw = rctx.get(LANE_CHAIN_ID_KEY)
    assert lane_id_raw
    assert rctx.get(LANE_RECOVERY_REASON_KEY) == "checkpoint_missing"
    assert rctx.get(LANE_RECOVERY_ATTEMPT_KEY) == 1
    assert (rctx.get("review_loop") or {}).get("original_task_id") == str(
        original_task.id
    )
    # No stale warm-resume / lane-reserved-VM directives on the recovery task.
    assert "improvement_lane_id" not in rctx
    assert "lane_checkpoint_sha" not in rctx
    assert rctx.get("is_lane_phase") is None
    assert "warm_target_vm_id" not in rctx
    # Recovery runs cold (fresh clone) — not pinned to the implementer VM.
    assert recovery.vm_instance_id is None

    # The lane is rebound to the recovery task, still ACTIVE, miss counted.
    lane_id = uuid.UUID(str(lane_id_raw))
    lane = await db_session.get(ImprovementLane, lane_id)
    assert lane is not None
    assert lane.status == ImprovementLaneStatus.ACTIVE.value
    assert lane.current_task_id == recovery.id
    assert lane.phase_requeues == 1
    # Operator-visible recovery reason recorded on the manifest.
    recovery_state = (lane.manifest or {}).get("recovery") or {}
    assert recovery_state.get("reason") == "checkpoint_missing"
    assert recovery_state.get("exhausted") is False
    assert recovery_state.get("attempt") == 1

    # No review task was created for this chain.
    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    review_tasks = [t for t in all_tasks if t.first_payload.get("type") == "review"]
    assert review_tasks == []

    # Source carries the visible recovery spawn reason.
    await db_session.refresh(source_task)
    assert (source_task.context or {}).get(
        SPAWN_REASON_KEY
    ) == SPAWN_REASON_LANE_RECOVERY


@pytest.mark.asyncio
async def test_lane_checkpoint_missing_over_budget_kills_lane_and_falls_back(
    db_session, mocker
):
    """Lane-arm review boundary with a checkpoint miss that EXHAUSTS the requeue
    budget: the lane is closed (KILLED) inline, any lane-held reservation is
    released, a visible exhaustion reason is recorded, and the pipeline falls back
    to a classic cold review (the PR is not orphaned).

    Exercises checkpoint_or_recover_phase -> close_lane_checkpoint_exhausted.
    """
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    _lane_pilot_on(mocker)
    mocker.patch(
        "app.github_token_resolver.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github_refs.is_ancestor_of",
        new_callable=AsyncMock,
        return_value=False,
    )

    from app.improvement_lane_ops import cas_transition, create_lane
    from app.models import (
        ImprovementLaneStatus,
        PoolVm,
        PoolVmState,
        _LANE_MAX_PHASE_REQUEUES,
    )
    from app.review_fix_lane_pilot import LANE_CHAIN_ID_KEY, LANE_PILOT_ASSIGNMENT_KEY

    project = await _make_db_project(db_session, is_self_repo=True)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "medium"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Pre-existing lane already AT the requeue budget — one more miss exhausts it.
    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="lane-exhausted",
        goal="exhaust requeues",
        branch_name="feat/lane-exhaust",
        base_branch="main",
        pilot_arm="lane",
    )
    await db_session.flush()
    await cas_transition(
        db_session,
        lane.id,
        ImprovementLaneStatus.PROPOSED,
        ImprovementLaneStatus.ACTIVE,
        current_task_id=None,
    )
    lane.phase_requeues = _LANE_MAX_PHASE_REQUEUES
    # A warm-pool member reserved for the lane must be released on exhaustion.
    member = PoolVm(
        vm_instance_id="pool-vm-exhaust-1",
        project_id=project.id,
        state=PoolVmState.IDLE,
        reserved_lane_id=lane.id,
    )
    db_session.add(member)
    db_session.add(lane)
    await db_session.commit()
    await db_session.refresh(lane)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        vm_instance_id="vm-lane-exhaust-src",
        payloads=[
            {
                "type": "create",
                "pr_number": 511,
                "branch": "feat/lane-exhaust",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "head_sha": "feedface87654321",
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            LANE_CHAIN_ID_KEY: str(lane.id),
            "review_loop": {
                "iteration": 0,
                "original_task_id": str(original_task.id),
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)
    lane.current_task_id = source_task.id
    db_session.add(lane)
    await db_session.commit()
    await db_session.refresh(lane)

    review, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=511,
        branch="feat/lane-exhaust",
        base_branch="main",
        original_task_id=str(original_task.id),
    )

    # Classic cold review fallback — the PR is still reviewed.
    assert created is True
    assert review is not None
    assert review.first_payload.get("type") == "review"
    rctx = review.context or {}
    assert "improvement_lane_id" not in rctx
    assert rctx.get("is_lane_phase") is None

    # Lane closed (KILLED) with a visible exhaustion reason; reservation released.
    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.KILLED.value
    assert lane.phase_requeues == _LANE_MAX_PHASE_REQUEUES + 1
    recovery_state = (lane.manifest or {}).get("recovery") or {}
    assert recovery_state.get("exhausted") is True
    await db_session.refresh(member)
    assert member.reserved_lane_id is None

    # The next fix spawn must stay classic: the exhausted lane is terminal and
    # must not be re-wired into the fix task even though the durable chain id
    # still exists on the cold review.
    review.status = TaskStatus.COMPLETED
    db_session.add(review)
    await db_session.commit()
    await db_session.refresh(review)

    fix, fix_created = await _spawn_pipeline_task(
        review,
        "fix",
        db_session,
        pr_number=511,
        branch="feat/lane-exhaust",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Resume after exhausted lane",
    )
    assert fix_created is True
    assert fix is not None
    fctx = fix.context or {}
    assert "improvement_lane_id" not in fctx
    assert fctx.get("is_lane_phase") is None
    assert "lane_checkpoint_sha" not in fctx


@pytest.mark.asyncio
async def test_lane_pilot_flag_off_classic_spawn_unchanged(db_session, mocker):
    """Flag off: no intent-to-treat assignment, no lane context, no lane row,
    no reservation — the classic spawn path is unchanged."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    # Deliberately do NOT enable the pilot.

    from app.models import ImprovementLane
    from app.review_fix_lane_pilot import LANE_CHAIN_ID_KEY, LANE_PILOT_ASSIGNMENT_KEY

    project = await _make_db_project(db_session, is_self_repo=True)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "medium"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        vm_instance_id="vm-classic-1",
        payloads=[
            {
                "type": "create",
                "pr_number": 400,
                "branch": "feat/classic",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "head_sha": "abc1234def5678",
            "review_loop": {"iteration": 0, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    review, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=400,
        branch="feat/classic",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert created is True
    rctx = review.context or {}
    # No pilot state written at all.
    assert LANE_PILOT_ASSIGNMENT_KEY not in rctx
    assert LANE_CHAIN_ID_KEY not in rctx
    assert "improvement_lane_id" not in rctx
    assert rctx.get("is_lane_phase") is None
    # No lane row created.
    lanes = (
        (
            await db_session.execute(
                select(ImprovementLane).where(ImprovementLane.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert lanes == []


@pytest.mark.asyncio
async def test_lane_shadow_mode_creates_armless_lane_without_reservation(
    db_session, mocker
):
    """Shadow mode writes real checkpoints on an armless lane without taking
    warm-pool reservations or attaching full-pilot dispatch/prompt context."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    _lane_shadow_on(mocker)
    mocker.patch(
        "app.github_token_resolver.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github_refs.is_ancestor_of",
        new_callable=AsyncMock,
        return_value=True,
    )

    from app.models import (
        ImprovementLane,
        ImprovementLaneStatus,
        PoolVm,
        PoolVmState,
    )
    from app.improvement_lane_ops import cas_transition, create_lane
    from app.review_fix_lane_pilot import (
        LANE_CHAIN_ID_KEY,
        LANE_PILOT_ASSIGNMENT_KEY,
        LANE_RESUME_CONTEXT_KEY,
    )

    project = await _make_db_project(db_session, is_self_repo=True)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "medium"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    vm_id = "pool-vm-shadow-1"
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=vm_id,
        payloads=[
            {
                "type": "create",
                "pr_number": 610,
                "branch": "feat/shadow",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "head_sha": "abc1234def5678",
            # Simulate a chain that was assigned before the operator switched
            # from full pilot to shadow; shadow spawns must scrub this key.
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            "_pr_changed_files": ["backend/app/shadow.py"],
            "review_loop": {"iteration": 0, "original_task_id": str(original_task.id)},
        },
    )
    member = PoolVm(
        vm_instance_id=vm_id,
        project_id=project.id,
        state=PoolVmState.LEASED,
        leased_task_id=source_task.id,
    )
    db_session.add_all([source_task, member])
    await db_session.commit()
    await db_session.refresh(source_task)

    existing_lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="shadow-old-lane",
        goal="stale full-pilot lane",
        branch_name="feat/shadow",
        base_branch="main",
        pilot_arm="lane",
    )
    await db_session.flush()
    await cas_transition(
        db_session,
        existing_lane.id,
        ImprovementLaneStatus.PROPOSED,
        ImprovementLaneStatus.ACTIVE,
        current_task_id=source_task.id,
    )
    existing_lane.workspace_state = "WARM_RESERVED"
    existing_lane.workspace_vm_instance_id = vm_id
    db_session.add(existing_lane)
    source_task.context = {
        **(source_task.context or {}),
        LANE_CHAIN_ID_KEY: str(existing_lane.id),
    }
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    review, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=610,
        branch="feat/shadow",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert created is True
    assert review is not None
    rctx = review.context or {}
    assert LANE_PILOT_ASSIGNMENT_KEY not in rctx
    assert "improvement_lane_id" not in rctx
    assert rctx.get("is_lane_phase") is None
    assert "lane_checkpoint_sha" not in rctx
    assert LANE_RESUME_CONTEXT_KEY not in rctx
    assert rctx.get(LANE_CHAIN_ID_KEY) != str(existing_lane.id)
    # Shadow does not force the reviewer cold: warm-continuity behaves like the
    # classic path when the source VM is live.
    assert rctx.get("warm_target_vm_id") == vm_id
    assert review.vm_instance_id is None

    lane_id = uuid.UUID(str(rctx[LANE_CHAIN_ID_KEY]))
    lane = await db_session.get(ImprovementLane, lane_id)
    assert lane is not None
    assert lane.pilot_arm is None
    assert lane.status == ImprovementLaneStatus.PARKED.value
    assert lane.last_checkpoint_sha == "abc1234def5678"
    assert lane.checkpoint_seq == 1
    assert lane.workspace_state == "COLD"
    assert lane.workspace_vm_instance_id is None
    assert lane.vm_cost_usd_total == 0
    await db_session.refresh(existing_lane)
    assert existing_lane.pilot_arm == "lane"
    assert existing_lane.workspace_state == "WARM_RESERVED"
    await db_session.refresh(member)
    assert member.reserved_lane_id is None
    assert member.state == PoolVmState.LEASED

    review.status = TaskStatus.COMPLETED
    review.vm_instance_id = "shadow-review-vm"
    db_session.add(review)
    await db_session.commit()
    await db_session.refresh(review)
    await db_session.refresh(source_task)
    assert (source_task.context or {}).get(LANE_PILOT_ASSIGNMENT_KEY) is None
    assert (source_task.context or {}).get(LANE_CHAIN_ID_KEY) == str(lane_id)

    fix, fix_created = await _spawn_pipeline_task(
        review,
        "fix",
        db_session,
        pr_number=610,
        branch="feat/shadow",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Address the review feedback",
        current_feedback="Tighten the guard",
        fix_reason="code_review",
    )
    assert fix_created is True
    assert fix is not None
    fctx = fix.context or {}
    assert LANE_PILOT_ASSIGNMENT_KEY not in fctx
    assert fctx.get(LANE_CHAIN_ID_KEY) == str(lane_id)
    assert "improvement_lane_id" not in fctx
    assert fctx.get("is_lane_phase") is None
    assert "lane_checkpoint_sha" not in fctx
    assert LANE_RESUME_CONTEXT_KEY not in fctx
    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.ACTIVE.value
    assert lane.current_task_id == fix.id
    assert lane.workspace_state == "COLD"

    fix.status = TaskStatus.EVALUATING
    fix.context = {
        **fctx,
        "head_sha": "def5678abc1234",
        "_pr_changed_files": ["backend/app/shadow.py", "backend/app/next.py"],
        "review_loop": {"iteration": 1, "original_task_id": str(original_task.id)},
    }
    db_session.add(fix)
    await db_session.commit()
    await db_session.refresh(fix)

    rereview, rereview_created = await _spawn_pipeline_task(
        fix,
        "review",
        db_session,
        pr_number=610,
        branch="feat/shadow",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert rereview_created is True
    assert rereview is not None
    rrctx = rereview.context or {}
    assert LANE_PILOT_ASSIGNMENT_KEY not in rrctx
    assert "improvement_lane_id" not in rrctx
    assert "lane_checkpoint_sha" not in rrctx

    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.PARKED.value
    assert lane.last_checkpoint_sha == "def5678abc1234"
    assert lane.checkpoint_seq == 2
    assert lane.pilot_arm is None
    assert lane.workspace_state == "COLD"
    phase_log = (lane.manifest or {}).get("phase_log") or []
    assert len(phase_log) == 2
    assert any("create PR#610" in entry for entry in phase_log)
    assert any("fix PR#610" in entry for entry in phase_log)


@pytest.mark.asyncio
async def test_lane_fix_spawn_skips_lane_when_mode_switches_to_shadow(
    db_session, mocker
):
    """A lane created while full pilot is on must not be reused if fix spawn
    happens after the chain switches to shadow mode."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    _lane_pilot_on(mocker)
    mocker.patch(
        "app.github_token_resolver.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github_refs.is_ancestor_of",
        new_callable=AsyncMock,
        return_value=True,
    )

    from app.models import (
        ImprovementLane,
        ImprovementLaneStatus,
        PoolVm,
        PoolVmState,
    )
    from app.review_fix_lane_pilot import (
        LANE_CHAIN_ID_KEY,
        LANE_PILOT_ASSIGNMENT_KEY,
    )

    project = await _make_db_project(db_session, is_self_repo=True)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "medium"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    vm_id = "pool-vm-lane-switch-1"
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        vm_instance_id=vm_id,
        payloads=[
            {
                "type": "create",
                "pr_number": 611,
                "branch": "feat/lane-switch",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "head_sha": "abc1234def5678",
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            "review_loop": {"iteration": 0, "original_task_id": str(original_task.id)},
        },
    )
    member = PoolVm(
        vm_instance_id=vm_id,
        project_id=project.id,
        state=PoolVmState.LEASED,
        leased_task_id=source_task.id,
    )
    db_session.add_all([source_task, member])
    await db_session.commit()
    await db_session.refresh(source_task)

    review, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=611,
        branch="feat/lane-switch",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert created is True
    assert review is not None
    rctx = review.context or {}
    assert rctx.get(LANE_PILOT_ASSIGNMENT_KEY) == "lane"
    lane_id = uuid.UUID(str(rctx[LANE_CHAIN_ID_KEY]))

    lane = await db_session.get(ImprovementLane, lane_id)
    assert lane is not None
    assert lane.pilot_arm == "lane"
    assert lane.status == ImprovementLaneStatus.PARKED.value
    assert lane.workspace_state == "WARM_RESERVED"

    _lane_shadow_on(mocker)

    review.status = TaskStatus.COMPLETED
    review.vm_instance_id = "lane-review-vm"
    db_session.add(review)
    await db_session.commit()
    await db_session.refresh(review)

    fix, fix_created = await _spawn_pipeline_task(
        review,
        "fix",
        db_session,
        pr_number=611,
        branch="feat/lane-switch",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Address the review feedback",
        current_feedback="Tighten the guard",
        fix_reason="code_review",
    )
    assert fix_created is True
    assert fix is not None
    fctx = fix.context or {}
    assert LANE_PILOT_ASSIGNMENT_KEY not in fctx
    assert "improvement_lane_id" not in fctx
    assert fctx.get("is_lane_phase") is None
    assert "lane_checkpoint_sha" not in fctx

    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.PARKED.value
    assert lane.workspace_state == "WARM_RESERVED"


@pytest.mark.asyncio
async def test_lane_shadow_chains_do_not_contribute_to_pilot_aggregates(
    db_session,
):
    """Armless shadow lanes and tasks without ITT assignment remain invisible
    to compute_lane_pilot_aggregates."""
    from app.improvement_lane_ops import create_lane
    from app.review_fix_lane_pilot import (
        LANE_CHAIN_ID_KEY,
        compute_lane_pilot_aggregates,
    )

    project = await _make_db_project(db_session, is_self_repo=True)
    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="shadow-lane",
        goal="shadow aggregate exclusion",
        branch_name="feat/shadow-agg",
        base_branch="main",
        pilot_arm=None,
    )
    lane.phase_requeues = 2
    shadow_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "fix", "prompt": "fix", "base_branch": "main"}],
        context={LANE_CHAIN_ID_KEY: str(lane.id), "pr_merged": True},
        estimated_cost_usd=9.0,
    )
    db_session.add(shadow_task)
    await db_session.commit()

    agg = await compute_lane_pilot_aggregates(db_session, project.id)
    assert agg["n_lane"] == 0
    assert agg["n_control"] == 0
    assert agg["lane_merge_rate"] is None
    assert agg["control_merge_rate"] is None
    assert agg["checkpoint_loss_rate"] is None
    assert agg["lane_fix_tasks_sampled"] == 0


@pytest.mark.asyncio
async def test_lane_shadow_checkpoint_miss_records_reason_without_recovery_spawn(
    db_session, mocker
):
    """Shadow checkpoint misses are visible but never redirect to recovery."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    _lane_shadow_on(mocker)
    mocker.patch(
        "app.github_token_resolver.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github_refs.is_ancestor_of",
        new_callable=AsyncMock,
        return_value=False,
    )

    from app.models import ImprovementLane, ImprovementLaneStatus
    from app.review_fix_lane_pilot import LANE_CHAIN_ID_KEY, LANE_PILOT_ASSIGNMENT_KEY
    from app.review_pipeline import SPAWN_REASON_LANE_RECOVERY

    project = await _make_db_project(db_session, is_self_repo=True)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "medium"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        vm_instance_id="vm-shadow-miss-1",
        payloads=[
            {
                "type": "create",
                "pr_number": 611,
                "branch": "feat/shadow-miss",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "head_sha": "deadbeef12345678",
            "review_loop": {"iteration": 0, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    review, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=611,
        branch="feat/shadow-miss",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert created is True
    assert review is not None
    assert review.first_payload.get("type") == "review"
    rctx = review.context or {}
    assert LANE_PILOT_ASSIGNMENT_KEY not in rctx
    assert "improvement_lane_id" not in rctx
    lane_id = uuid.UUID(str(rctx[LANE_CHAIN_ID_KEY]))

    lane = await db_session.get(ImprovementLane, lane_id)
    assert lane is not None
    assert lane.pilot_arm is None
    assert lane.status == ImprovementLaneStatus.ACTIVE.value
    assert lane.phase_requeues == 1
    assert lane.workspace_state == "COLD"
    recovery_state = (lane.manifest or {}).get("recovery") or {}
    assert recovery_state.get("reason") == "sha_not_reachable"
    assert recovery_state.get("attempt") == 1
    assert recovery_state.get("exhausted") is False

    await db_session.refresh(source_task)
    assert (source_task.context or {}).get(
        SPAWN_REASON_KEY
    ) != SPAWN_REASON_LANE_RECOVERY

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    recovery_tasks = [
        t
        for t in tasks
        if (t.context or {}).get(SPAWN_REASON_KEY) == SPAWN_REASON_LANE_RECOVERY
    ]
    assert recovery_tasks == []


@pytest.mark.asyncio
async def test_lane_shadow_checkpoint_owner_conflict_records_visible_reason(
    db_session,
):
    """A shadow CAS owner conflict records an operator-visible marker without
    consuming the checkpoint-miss requeue budget."""
    from app.improvement_lane_ops import cas_transition, create_lane
    from app.models import ImprovementLaneStatus
    from app.review_fix_lane_pilot import (
        CHECKPOINT_PHASE_OK,
        checkpoint_or_recover_phase,
    )

    project = await _make_db_project(db_session, is_self_repo=True)
    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="shadow-owner-conflict",
        goal="record visible CAS conflict",
        branch_name="feat/shadow-owner",
        base_branch="main",
        pilot_arm=None,
    )
    await db_session.flush()
    owner_task_id = uuid.uuid4()
    await cas_transition(
        db_session,
        lane.id,
        ImprovementLaneStatus.PROPOSED,
        ImprovementLaneStatus.ACTIVE,
        current_task_id=owner_task_id,
    )
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "fix", "prompt": "fix", "base_branch": "main"}],
        context={"head_sha": "abc1234def5678"},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)
    await db_session.refresh(lane)

    outcome = await checkpoint_or_recover_phase(
        db_session,
        source_task,
        lane,
        sha="abc1234def5678",
        summary="shadow owner conflict",
        reserve_workspace=False,
        enable_recovery=False,
        record_miss_marker=True,
    )

    assert outcome == CHECKPOINT_PHASE_OK
    # The recovery marker is left pending for the caller's commit (the lane
    # ops contract) — commit before refresh so the refresh reads persisted
    # state instead of discarding the pending write under real ORM expiry.
    await db_session.commit()
    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.ACTIVE.value
    assert lane.current_task_id == owner_task_id
    assert lane.phase_requeues == 0
    recovery_state = (lane.manifest or {}).get("recovery") or {}
    assert recovery_state.get("reason") == "owner_mismatch"
    assert recovery_state.get("attempt") == 0
    assert recovery_state.get("exhausted") is False


@pytest.mark.asyncio
async def test_lane_pilot_control_arm_runs_classic(db_session, mocker):
    """Control-arm chain: pilot enabled, but the chain runs the classic
    review->fix path with no lane reservation and no lane context."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    _lane_pilot_on(mocker)

    from app.models import ImprovementLane, PoolVm, PoolVmState
    from app.review_fix_lane_pilot import LANE_CHAIN_ID_KEY, LANE_PILOT_ASSIGNMENT_KEY

    project = await _make_db_project(db_session, is_self_repo=True)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "medium"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    vm_id = "pool-vm-control-1"
    member = PoolVm(
        vm_instance_id=vm_id, project_id=project.id, state=PoolVmState.LEASED
    )
    db_session.add(member)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        vm_instance_id=vm_id,
        payloads=[
            {
                "type": "create",
                "pr_number": 401,
                "branch": "feat/control",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "head_sha": "abc1234def5678",
            LANE_PILOT_ASSIGNMENT_KEY: "control",
            "review_loop": {"iteration": 0, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    review, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=401,
        branch="feat/control",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert created is True
    rctx = review.context or {}
    # Control arm carries its assignment forward (for measurement) but no lane.
    assert rctx.get(LANE_PILOT_ASSIGNMENT_KEY) == "control"
    assert LANE_CHAIN_ID_KEY not in rctx
    assert "improvement_lane_id" not in rctx
    assert rctx.get("is_lane_phase") is None
    # No lane row, and the pool member was NOT reserved.
    lanes = (
        (
            await db_session.execute(
                select(ImprovementLane).where(ImprovementLane.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert lanes == []
    await db_session.refresh(member)
    assert member.reserved_lane_id is None
    assert member.state == PoolVmState.LEASED


@pytest.mark.asyncio
async def test_lane_pilot_kill_degrades_to_classic_and_frees_reservations(
    db_session, mocker
):
    """A KILL flag-flip degrades lanes to classic spawning and force-expires
    existing reservations."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    _lane_pilot_on(mocker)

    from app.improvement_lane_ops import create_lane
    from app.models import PoolVm, PoolVmState
    from app.review_fix_lane_pilot import (
        LANE_PILOT_ASSIGNMENT_KEY,
        apply_lane_pilot_kill,
        is_review_fix_lane_pilot_enabled,
    )
    from app.shared_state import runtime_config

    project = await _make_db_project(db_session, is_self_repo=True)
    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="t",
        goal="g",
        branch_name="feat/kill",
        base_branch="main",
        pilot_arm="lane",
    )
    await db_session.flush()
    # A member reserved to the lane.
    member = PoolVm(
        vm_instance_id="pool-vm-kill-1",
        project_id=project.id,
        state=PoolVmState.IDLE,
        reserved_lane_id=lane.id,
    )
    db_session.add(member)
    await db_session.commit()

    assert is_review_fix_lane_pilot_enabled() is True

    # KILL: flip the flag off + force-expire reservations.
    result = await apply_lane_pilot_kill(db_session, project.id, reason="pilot_kill")
    await db_session.commit()
    assert result["reservations_cleared"] == 1
    assert runtime_config["review_fix_lane_pilot_enabled"] is False
    assert is_review_fix_lane_pilot_enabled() is False
    await db_session.refresh(member)
    assert member.reserved_lane_id is None
    assert member.reservation_expires_at is None

    # Now spawning on a would-be lane-arm chain degrades to classic.
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "medium"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        vm_instance_id="vm-postkill-1",
        payloads=[
            {
                "type": "create",
                "pr_number": 402,
                "branch": "feat/postkill",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "head_sha": "abc1234def5678",
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            "review_loop": {"iteration": 0, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    review, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=402,
        branch="feat/postkill",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert created is True
    rctx = review.context or {}
    # Flag off → classic: no lane dispatch context attached.
    assert "improvement_lane_id" not in rctx
    assert rctx.get("is_lane_phase") is None


@pytest.mark.asyncio
async def test_lane_pilot_assign_arm_is_deterministic_and_splits():
    """assign_lane_arm is a pure, stable function of the seed and partitions
    a population across both arms."""
    from app.review_fix_lane_pilot import assign_lane_arm

    seed = "task-abc-123"
    assert assign_lane_arm(seed) == assign_lane_arm(seed)  # stable
    assert assign_lane_arm(seed) in ("lane", "control")
    arms = {assign_lane_arm(f"task-{i}") for i in range(200)}
    assert arms == {"lane", "control"}  # both arms populated


@pytest.mark.asyncio
async def test_lane_pilot_gate_integrity_alert_raised_and_deduped(db_session):
    """A gate-integrity violation posts exactly one BLOCKER ProjectMessage even
    when raised repeatedly (deduped on the violation event)."""
    from app.models import MessageType, ProjectMessage
    from app.review_fix_lane_pilot import raise_gate_integrity_alert

    project = await _make_db_project(db_session, is_self_repo=True)
    await raise_gate_integrity_alert(db_session, project.id, "reviewer saw lane ctx")
    await db_session.commit()
    await raise_gate_integrity_alert(db_session, project.id, "reviewer saw lane ctx")
    await db_session.commit()

    msgs = (
        (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs) == 1
    assert msgs[0].message_type == MessageType.BLOCKER
    assert (msgs[0].context or {}).get("event_type") == (
        "review_fix_lane_gate_integrity_violation"
    )


@pytest.mark.asyncio
async def test_lane_pilot_aggregates_checkpoint_loss_rate(db_session):
    """compute_lane_pilot_aggregates counts arms from tasks and checkpoint
    loss from lane rows."""
    from app.improvement_lane_ops import create_lane
    from app.capability_trial_verdict import LANE_PILOT_ASSIGNMENT_KEY
    from app.models import ImprovementLane
    from app.review_fix_lane_pilot import compute_lane_pilot_aggregates

    project = await _make_db_project(db_session, is_self_repo=True)
    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="lane-task",
        goal="g",
        branch_name="b0",
        base_branch="main",
        pilot_arm="lane",
    )
    lane.phase_requeues = 2

    lane_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "fix", "prompt": "fix", "base_branch": "main"}],
        context={LANE_PILOT_ASSIGNMENT_KEY: "lane", "pr_merged": True},
    )
    control_task = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[{"type": "review", "prompt": "review", "base_branch": "main"}],
        context={LANE_PILOT_ASSIGNMENT_KEY: "control", "pr_merged": False},
    )
    db_session.add_all([lane_task, control_task])
    await db_session.commit()

    lanes = (
        (
            await db_session.execute(
                select(ImprovementLane).where(ImprovementLane.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(lanes) == 1

    agg = await compute_lane_pilot_aggregates(db_session, project.id)
    assert agg["n_lane"] == 1
    assert agg["n_control"] == 1
    # One lane row had a requeue → 1.0 checkpoint-loss rate.
    assert agg["checkpoint_loss_rate"] == 1.0


@pytest.mark.asyncio
async def test_lane_pilot_aggregates_count_original_chains_not_iterations(db_session):
    """compute_lane_pilot_aggregates counts chains (original PRs), not tasks.

    A 3-task chain (create + review + fix) sharing one original_task_id must
    contribute exactly ONE lane sample, not three.  Merge outcome is True if ANY
    task in the chain has pr_merged=True.  Cost is summed across all tasks.
    Engagement receipts (warm_resume_fraction, checkpoint_fraction) are also
    chain-level: a chain is considered engaged if any fix task in it had the
    receipt stamped.
    """
    from app.capability_trial_verdict import (
        LANE_PILOT_ASSIGNMENT_KEY,
        LANE_PILOT_CHECKPOINT_RECEIPT_KEY,
        LANE_PILOT_WARM_RESUME_RECEIPT_KEY,
    )
    from app.review_fix_lane_pilot import compute_lane_pilot_aggregates

    project = await _make_db_project(db_session, is_self_repo=True)

    # ── Chain A: lane arm, 3 tasks sharing same original_task_id ────────────
    # The root create task (no original_task_id in payload).
    root_task_id = uuid.uuid4()
    root_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "build", "base_branch": "main"}],
        context={LANE_PILOT_ASSIGNMENT_KEY: "lane"},
    )
    root_task.id = root_task_id

    # Review iteration task — not merged.
    review_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "review",
                "prompt": "review",
                "base_branch": "main",
                "original_task_id": str(root_task_id),
            }
        ],
        context={LANE_PILOT_ASSIGNMENT_KEY: "lane", "pr_merged": False},
        estimated_cost_usd=1.0,
    )

    # Fix iteration task — merged, has warm-resume + checkpoint receipts.
    fix_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "prompt": "fix",
                "base_branch": "main",
                "original_task_id": str(root_task_id),
            }
        ],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            "pr_merged": True,
            LANE_PILOT_WARM_RESUME_RECEIPT_KEY: True,
            LANE_PILOT_CHECKPOINT_RECEIPT_KEY: True,
        },
        estimated_cost_usd=2.0,
    )

    # ── Chain B: control arm, single task ───────────────────────────────────
    control_root_id = uuid.uuid4()
    control_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "fix", "prompt": "fix", "base_branch": "main"}],
        context={LANE_PILOT_ASSIGNMENT_KEY: "control", "pr_merged": False},
        estimated_cost_usd=1.5,
    )
    control_task.id = control_root_id

    db_session.add_all([root_task, review_task, fix_task, control_task])
    await db_session.commit()

    agg = await compute_lane_pilot_aggregates(db_session, project.id)

    # Chain A contributes 1 lane sample (not 3 tasks), chain B contributes 1 control.
    assert agg["n_lane"] == 1, f"expected 1 lane chain, got {agg['n_lane']}"
    assert agg["n_control"] == 1, f"expected 1 control chain, got {agg['n_control']}"

    # Merge outcome: chain A is merged (fix_task had pr_merged=True).
    assert agg["lane_merge_rate"] == 1.0, (
        f"expected lane merged, got {agg['lane_merge_rate']}"
    )
    assert agg["control_merge_rate"] == 0.0, (
        f"expected control not merged, got {agg['control_merge_rate']}"
    )

    # Cost: chain A cost = review_task(1.0) + fix_task(2.0) = 3.0 for merged chain.
    # root_task has no estimated_cost_usd set, defaults to 0.
    assert agg["lane_cost_per_merged"] == pytest.approx(3.0, abs=0.01), (
        f"expected chain-summed cost 3.0, got {agg['lane_cost_per_merged']}"
    )

    # Engagement: chain A fix task had both receipts → 1 warm chain / 1 fix chain.
    assert agg["lane_fix_tasks_sampled"] == 1, (
        f"expected 1 engagement chain (not task count), got {agg['lane_fix_tasks_sampled']}"
    )
    assert agg["warm_resume_fraction"] == 1.0, (
        f"expected warm_resume_fraction=1.0, got {agg['warm_resume_fraction']}"
    )
    assert agg["checkpoint_fraction"] == 1.0, (
        f"expected checkpoint_fraction=1.0, got {agg['checkpoint_fraction']}"
    )


@pytest.mark.asyncio
async def test_lane_pilot_aggregates_partial_warm_resume_fraction(db_session):
    """compute_lane_pilot_aggregates returns fractional engagement at partial values.

    Two lane-arm chains are created:
    - Chain A fix task has LANE_PILOT_WARM_RESUME_RECEIPT_KEY=True and
      LANE_PILOT_CHECKPOINT_RECEIPT_KEY=True.
    - Chain B fix task has neither receipt key.

    warm_resume_fraction and checkpoint_fraction must both be 0.5 (1 out of 2),
    not 0.0 or 1.0.  A separate variant also verifies that the two fractions are
    measured independently: chain A has checkpoint only, chain B has warm-resume
    only, so both fractions are 0.5 but from different chains.
    """
    from app.capability_trial_verdict import (
        LANE_PILOT_ASSIGNMENT_KEY,
        LANE_PILOT_CHECKPOINT_RECEIPT_KEY,
        LANE_PILOT_WARM_RESUME_RECEIPT_KEY,
    )
    from app.review_fix_lane_pilot import compute_lane_pilot_aggregates

    project = await _make_db_project(db_session, is_self_repo=True)

    # ── Chain A: fix task carries both warm-resume + checkpoint receipts ──────
    root_a_id = uuid.uuid4()
    root_a = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "create", "base_branch": "main"}],
        context={LANE_PILOT_ASSIGNMENT_KEY: "lane"},
    )
    root_a.id = root_a_id

    fix_a = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "prompt": "fix",
                "base_branch": "main",
                "original_task_id": str(root_a_id),
            }
        ],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            "pr_merged": True,
            LANE_PILOT_WARM_RESUME_RECEIPT_KEY: True,
            LANE_PILOT_CHECKPOINT_RECEIPT_KEY: True,
        },
        estimated_cost_usd=1.0,
    )

    # ── Chain B: fix task carries neither receipt ────────────────────────────
    root_b_id = uuid.uuid4()
    root_b = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "create", "base_branch": "main"}],
        context={LANE_PILOT_ASSIGNMENT_KEY: "lane"},
    )
    root_b.id = root_b_id

    fix_b = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "prompt": "fix",
                "base_branch": "main",
                "original_task_id": str(root_b_id),
            }
        ],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            "pr_merged": False,
        },
        estimated_cost_usd=1.0,
    )

    db_session.add_all([root_a, fix_a, root_b, fix_b])
    await db_session.commit()

    agg = await compute_lane_pilot_aggregates(db_session, project.id)

    assert agg["lane_fix_tasks_sampled"] == 2, (
        f"expected 2 engagement chains, got {agg['lane_fix_tasks_sampled']}"
    )
    assert agg["warm_resume_fraction"] == pytest.approx(0.5), (
        f"expected warm_resume_fraction=0.5 (1/2), got {agg['warm_resume_fraction']}"
    )
    assert agg["checkpoint_fraction"] == pytest.approx(0.5), (
        f"expected checkpoint_fraction=0.5 (1/2), got {agg['checkpoint_fraction']}"
    )

    # ── Second scenario: fractions diverge because receipts are on different chains
    # Create a fresh project so state is isolated.
    project2 = await _make_db_project(db_session, is_self_repo=True)

    # Chain C: checkpoint only (no warm-resume).
    root_c_id = uuid.uuid4()
    root_c = make_task(
        project_id=project2.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "create", "base_branch": "main"}],
        context={LANE_PILOT_ASSIGNMENT_KEY: "lane"},
    )
    root_c.id = root_c_id

    fix_c = make_task(
        project_id=project2.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "prompt": "fix",
                "base_branch": "main",
                "original_task_id": str(root_c_id),
            }
        ],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            "pr_merged": True,
            LANE_PILOT_CHECKPOINT_RECEIPT_KEY: True,
        },
        estimated_cost_usd=1.0,
    )

    # Chain D: warm-resume only (no checkpoint).
    root_d_id = uuid.uuid4()
    root_d = make_task(
        project_id=project2.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "create", "base_branch": "main"}],
        context={LANE_PILOT_ASSIGNMENT_KEY: "lane"},
    )
    root_d.id = root_d_id

    fix_d = make_task(
        project_id=project2.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "prompt": "fix",
                "base_branch": "main",
                "original_task_id": str(root_d_id),
            }
        ],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            "pr_merged": False,
            LANE_PILOT_WARM_RESUME_RECEIPT_KEY: True,
        },
        estimated_cost_usd=1.0,
    )

    db_session.add_all([root_c, fix_c, root_d, fix_d])
    await db_session.commit()

    agg2 = await compute_lane_pilot_aggregates(db_session, project2.id)

    assert agg2["lane_fix_tasks_sampled"] == 2, (
        f"expected 2 engagement chains in project2, got {agg2['lane_fix_tasks_sampled']}"
    )
    # Both fractions are 0.5 but from different chains — verifies they are
    # measured independently and not conflated.
    assert agg2["warm_resume_fraction"] == pytest.approx(0.5), (
        f"expected warm_resume_fraction=0.5 (chain D only), got {agg2['warm_resume_fraction']}"
    )
    assert agg2["checkpoint_fraction"] == pytest.approx(0.5), (
        f"expected checkpoint_fraction=0.5 (chain C only), got {agg2['checkpoint_fraction']}"
    )


@pytest.mark.asyncio
async def test_lane_pilot_aggregates_gate_integrity_violations(db_session):
    """compute_lane_pilot_aggregates counts gate-integrity violations correctly.

    A violation is a review/audit Task in the lane arm that still carries
    improvement_lane_id in its context (meaning the strip-transient guard was
    bypassed).  The zero-violation (clean) case must also return 0 so the
    signal is only non-zero on real breaches.
    """
    from app.capability_trial_verdict import LANE_PILOT_ASSIGNMENT_KEY
    from app.review_fix_lane_pilot import compute_lane_pilot_aggregates

    project = await _make_db_project(db_session, is_self_repo=True)
    other_project = await _make_db_project(db_session, is_self_repo=True)

    # ── Clean case: no violations before planting ────────────────────────────
    agg_clean = await compute_lane_pilot_aggregates(db_session, project.id)
    assert agg_clean["gate_integrity_violations"] == 0

    # ── Violation row: lane-arm review task that kept improvement_lane_id ────
    violation_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "review", "prompt": "review this", "base_branch": "main"}],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            "improvement_lane_id": str(uuid.uuid4()),
        },
    )
    await _persist(db_session, violation_task)

    agg_with = await compute_lane_pilot_aggregates(db_session, project.id)
    assert agg_with["gate_integrity_violations"] >= 1

    # ── Control-arm task with improvement_lane_id should NOT count ───────────
    control_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "review", "prompt": "review this", "base_branch": "main"}],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "control",
            "improvement_lane_id": str(uuid.uuid4()),
        },
    )
    await _persist(db_session, control_task)

    agg_after_control = await compute_lane_pilot_aggregates(db_session, project.id)
    assert (
        agg_after_control["gate_integrity_violations"]
        == agg_with["gate_integrity_violations"]
    )

    # ── Different project's violation should NOT count ───────────────────────
    other_violation = make_task(
        project_id=other_project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "audit", "prompt": "audit this", "base_branch": "main"}],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            "improvement_lane_id": str(uuid.uuid4()),
        },
    )
    await _persist(db_session, other_violation)

    agg_isolated = await compute_lane_pilot_aggregates(db_session, project.id)
    assert (
        agg_isolated["gate_integrity_violations"]
        == agg_with["gate_integrity_violations"]
    )

    # ── Lane-arm fix task (not review/audit) should NOT count ────────────────
    fix_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "fix", "prompt": "fix this", "base_branch": "main"}],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            "improvement_lane_id": str(uuid.uuid4()),
        },
    )
    await _persist(db_session, fix_task)

    agg_after_fix = await compute_lane_pilot_aggregates(db_session, project.id)
    assert (
        agg_after_fix["gate_integrity_violations"]
        == agg_with["gate_integrity_violations"]
    )


@pytest.mark.asyncio
async def test_lane_pilot_durable_kill_survives_enabled_flag_reload(db_session, mocker):
    """A durable KILL verdict suppresses lane mechanics even when the runtime
    config flag is still True (simulating a config reload that re-enables the flag
    after apply_lane_pilot_kill flipped it off)."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    _lane_pilot_on(mocker)

    from app.models import ImprovementLane
    from app.review_fix_lane_pilot import (
        LANE_PILOT_ASSIGNMENT_KEY,
        LANE_PILOT_CAP_NAME,
        is_review_fix_lane_pilot_active_for_project,
    )
    from app.shared_state import runtime_config

    project = await _make_db_project(db_session, is_self_repo=True)

    # Simulate a durable KILL verdict recorded on the project (e.g. by a prior
    # apply_lane_pilot_kill cycle), while the in-memory flag is still True
    # (as it would be after a runtime_config reload from persisted state).
    project.capability_adoption_verdicts = {
        LANE_PILOT_CAP_NAME: {
            "verdict": "KILL",
            "verdict_reason": "lane_merge_rate_below_floor",
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Confirm the flag is enabled in-memory but the project-aware gate returns False.
    assert runtime_config.get("review_fix_lane_pilot_enabled") is True
    assert not is_review_fix_lane_pilot_active_for_project(project)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "medium"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # A would-be lane-arm source task (has the assignment key set).
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        vm_instance_id="vm-kill-reload-1",
        payloads=[
            {
                "type": "create",
                "pr_number": 501,
                "branch": "feat/kill-reload",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "head_sha": "abc1234def5678",
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            "review_loop": {"iteration": 0, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    review, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=501,
        branch="feat/kill-reload",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert created is True
    rctx = review.context or {}
    # KILL is durable: no lane dispatch context on the spawned review.
    assert "improvement_lane_id" not in rctx
    assert rctx.get("is_lane_phase") is None
    assert "lane_checkpoint_sha" not in rctx
    # No lane rows created.
    lanes = (
        (
            await db_session.execute(
                select(ImprovementLane).where(ImprovementLane.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert lanes == []


@pytest.mark.asyncio
async def test_lane_pilot_adopt_forces_lane_arm_for_new_chains(db_session, mocker):
    """A durable ADOPT verdict routes every newly-eligible chain to the 'lane'
    arm, even for a seed that would hash to 'control' under normal 50/50 split."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    _lane_pilot_on(mocker)

    from app.review_fix_lane_pilot import (
        LANE_PILOT_ASSIGNMENT_KEY,
        LANE_PILOT_CAP_NAME,
        assign_lane_arm,
        record_lane_arm_assignment,
        is_review_fix_lane_adopted,
    )

    project = await _make_db_project(db_session, is_self_repo=True)

    # Record a durable ADOPT verdict on the project.
    project.capability_adoption_verdicts = {
        LANE_PILOT_CAP_NAME: {
            "verdict": "ADOPT",
            "verdict_reason": "lane_cost_improvement_met",
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert is_review_fix_lane_adopted(project)

    # Use a seed that normally hashes to 'control'.
    control_seed = "task-ctrl-0"
    assert assign_lane_arm(control_seed) == "control"

    # record_lane_arm_assignment must override to 'lane' under ADOPT.
    ctx: dict = {}
    arm = record_lane_arm_assignment(ctx, control_seed, project)
    assert arm == "lane"
    assert ctx[LANE_PILOT_ASSIGNMENT_KEY] == "lane"


@pytest.mark.asyncio
async def test_lane_pilot_adopt_gate_integrity_preserved(db_session, mocker):
    """Under ADOPT routing, review/audit tasks still never carry lane dispatch
    context (gate integrity invariant)."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    _lane_pilot_on(mocker)
    mocker.patch(
        "app.github_token_resolver.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github_refs.is_ancestor_of",
        new_callable=AsyncMock,
        return_value=True,
    )

    from app.review_fix_lane_pilot import (
        LANE_PILOT_ASSIGNMENT_KEY,
        LANE_PILOT_CAP_NAME,
    )

    project = await _make_db_project(db_session, is_self_repo=True)
    project.capability_adoption_verdicts = {
        LANE_PILOT_CAP_NAME: {
            "verdict": "ADOPT",
            "verdict_reason": "lane_cost_improvement_met",
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "medium"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        vm_instance_id="vm-adopt-gate-1",
        payloads=[
            {
                "type": "create",
                "pr_number": 502,
                "branch": "feat/adopt-gate",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "head_sha": "abc1234def5678",
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            "review_loop": {"iteration": 0, "original_task_id": str(original_task.id)},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    review, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=502,
        branch="feat/adopt-gate",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    assert created is True
    rctx = review.context or {}
    # Gate integrity: reviewer NEVER carries lane dispatch context, even under ADOPT.
    assert "improvement_lane_id" not in rctx
    assert rctx.get("is_lane_phase") is None
    assert "lane_checkpoint_sha" not in rctx
    # The arm assignment is still propagated forward (for measurement).
    assert rctx.get(LANE_PILOT_ASSIGNMENT_KEY) == "lane"


# ── Lane checkpoint SHA verification ──────────────────────────────────────────
#
# These directly exercise checkpoint_and_reserve_on_phase_complete to confirm
# that unverified SHAs degrade to mark_checkpoint_missing rather than being
# recorded as durable checkpoints.


class TestLaneCheckpointShaVerification:
    """SHA reachability is verified before recording a lane checkpoint.

    A False (not reachable) or None (API unavailable) result must degrade to
    mark_checkpoint_missing with a distinct reason string instead of recording
    a stale or unverified SHA.  A True result must proceed through the normal
    checkpoint_lane path.
    """

    async def _setup_lane_and_task(self, db_session):
        """Create a project, an ACTIVE lane, and a source task for checkpoint tests."""
        from app.improvement_lane_ops import cas_transition, create_lane
        from app.models import ImprovementLaneStatus

        project = await _make_db_project(db_session, is_self_repo=True)
        lane = await create_lane(
            db_session,
            project_id=project.id,
            direction_id=None,
            title="checkpoint-sha-test",
            goal="verify sha before checkpoint",
            branch_name="feat/sha-verify",
            base_branch="main",
            pilot_arm="lane",
        )
        await db_session.flush()
        await cas_transition(
            db_session,
            lane.id,
            ImprovementLaneStatus.PROPOSED,
            ImprovementLaneStatus.ACTIVE,
            current_task_id=None,
        )

        source_task = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            payloads=[{"type": "fix", "prompt": "fix", "base_branch": "main"}],
            context={"head_sha": "abc1234def5678"},
        )
        db_session.add(source_task)
        await db_session.commit()
        await db_session.refresh(source_task)
        lane.current_task_id = source_task.id
        db_session.add(lane)
        await db_session.commit()
        await db_session.refresh(lane)
        return lane, source_task

    @pytest.mark.asyncio
    async def test_lane_checkpoint_unverified_sha_degrades_to_checkpoint_missing(
        self, db_session, mocker
    ):
        """SHA not reachable on branch → mark_checkpoint_missing(reason='sha_not_reachable')
        and return False; checkpoint_lane must NOT be called."""
        mocker.patch(
            "app.github_token_resolver.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("gh-token", "owner", "repo"),
        )
        mocker.patch(
            "app.github_refs.is_ancestor_of",
            new_callable=AsyncMock,
            return_value=False,
        )
        mock_checkpoint_lane = mocker.patch(
            "app.improvement_lane_ops.checkpoint_lane", new_callable=AsyncMock
        )

        from app.review_fix_lane_pilot import checkpoint_and_reserve_on_phase_complete

        lane, source_task = await self._setup_lane_and_task(db_session)
        initial_requeues = lane.phase_requeues or 0

        result = await checkpoint_and_reserve_on_phase_complete(
            db_session,
            source_task,
            lane,
            sha="abc1234def5678",
            summary="phase complete",
        )

        assert result is False
        mock_checkpoint_lane.assert_not_called()
        # phase_requeues incremented via mark_checkpoint_missing — the bump is
        # left pending for the caller's commit (lane ops contract), so commit
        # before refresh: under real ORM expiry a refresh would otherwise
        # discard the pending write.
        await db_session.commit()
        await db_session.refresh(lane)
        assert (lane.phase_requeues or 0) == initial_requeues + 1

    @pytest.mark.asyncio
    async def test_lane_checkpoint_sha_verification_unavailable_degrades(
        self, db_session, mocker
    ):
        """API returns None (transient) → skip checkpoint without burning
        phase_requeues budget; checkpoint_lane must NOT be called."""
        mocker.patch(
            "app.github_token_resolver.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("gh-token", "owner", "repo"),
        )
        mocker.patch(
            "app.github_refs.is_ancestor_of",
            new_callable=AsyncMock,
            return_value=None,
        )
        mock_checkpoint_lane = mocker.patch(
            "app.improvement_lane_ops.checkpoint_lane", new_callable=AsyncMock
        )

        from app.review_fix_lane_pilot import checkpoint_and_reserve_on_phase_complete

        lane, source_task = await self._setup_lane_and_task(db_session)
        initial_requeues = lane.phase_requeues or 0

        result = await checkpoint_and_reserve_on_phase_complete(
            db_session,
            source_task,
            lane,
            sha="abc1234def5678",
            summary="phase complete",
        )

        assert result is False
        mock_checkpoint_lane.assert_not_called()
        await db_session.refresh(lane)
        # Transient unavailability must NOT burn a requeue slot.
        assert (lane.phase_requeues or 0) == initial_requeues

    @pytest.mark.asyncio
    async def test_lane_checkpoint_sha_token_error_degrades(self, db_session, mocker):
        """Token fetch failure (credentials missing, etc.) → return False WITHOUT
        burning phase_requeues budget; checkpoint_lane must NOT be called."""
        mocker.patch(
            "app.github_token_resolver.get_github_token_for_task",
            new_callable=AsyncMock,
            side_effect=Exception("no github credentials"),
        )
        mock_checkpoint_lane = mocker.patch(
            "app.improvement_lane_ops.checkpoint_lane", new_callable=AsyncMock
        )

        from app.review_fix_lane_pilot import checkpoint_and_reserve_on_phase_complete

        lane, source_task = await self._setup_lane_and_task(db_session)
        initial_requeues = lane.phase_requeues or 0

        result = await checkpoint_and_reserve_on_phase_complete(
            db_session,
            source_task,
            lane,
            sha="abc1234def5678",
            summary="phase complete",
        )

        assert result is False
        mock_checkpoint_lane.assert_not_called()
        await db_session.refresh(lane)
        # Transient exception must NOT increment phase_requeues.
        assert (lane.phase_requeues or 0) == initial_requeues

    @pytest.mark.asyncio
    async def test_lane_checkpoint_sha_reachable_proceeds(self, db_session, mocker):
        """SHA verified as reachable → checkpoint_lane is called and True is returned."""
        mocker.patch(
            "app.github_token_resolver.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("gh-token", "owner", "repo"),
        )
        mocker.patch(
            "app.github_refs.is_ancestor_of",
            new_callable=AsyncMock,
            return_value=True,
        )

        from app.review_fix_lane_pilot import checkpoint_and_reserve_on_phase_complete

        lane, source_task = await self._setup_lane_and_task(db_session)

        result = await checkpoint_and_reserve_on_phase_complete(
            db_session,
            source_task,
            lane,
            sha="abc1234def5678",
            summary="phase complete",
        )

        assert result is True
        # checkpoint_lane leaves its writes pending for the caller's commit
        # (lane ops contract) — commit before refresh under real ORM expiry.
        await db_session.commit()
        await db_session.refresh(lane)
        assert lane.last_checkpoint_sha == "abc1234def5678"

    @pytest.mark.asyncio
    async def test_lane_checkpoint_owner_mismatch_is_stale_noop(
        self, db_session, mocker
    ):
        """A stale task that no longer owns the ACTIVE lane must not consume the
        checkpoint-miss budget or trigger recovery, even on a short SHA."""
        mock_mark_checkpoint_missing = mocker.patch(
            "app.improvement_lane_ops.mark_checkpoint_missing",
            new_callable=AsyncMock,
        )

        from app.models import ImprovementLaneStatus
        from app.review_fix_lane_pilot import (
            CHECKPOINT_PHASE_OK,
            checkpoint_or_recover_phase,
        )

        lane, source_task = await self._setup_lane_and_task(db_session)
        lane.current_task_id = uuid.uuid4()
        await db_session.commit()
        await db_session.refresh(lane)
        initial_requeues = lane.phase_requeues or 0

        result = await checkpoint_or_recover_phase(
            db_session,
            source_task,
            lane,
            sha="abc",
            summary="phase complete",
        )

        assert result == CHECKPOINT_PHASE_OK
        await db_session.refresh(lane)
        assert lane.status == ImprovementLaneStatus.ACTIVE.value
        assert (lane.phase_requeues or 0) == initial_requeues
        mock_mark_checkpoint_missing.assert_not_called()


@pytest.mark.asyncio
async def test_lane_pilot_is_review_fix_lane_pilot_active_for_project():
    """is_review_fix_lane_pilot_active_for_project returns False when KILL is
    present, and True for an eligible project without a KILL verdict."""
    from app.review_fix_lane_pilot import (
        LANE_PILOT_CAP_NAME,
        is_review_fix_lane_pilot_active_for_project,
    )
    from unittest.mock import MagicMock, patch

    def _make_mock_project(verdicts=None, is_self_repo=True):
        p = MagicMock()
        p.capability_adoption_verdicts = verdicts or {}
        return p

    with (
        patch(
            "app.review_fix_lane_pilot.is_review_fix_lane_pilot_enabled",
            return_value=True,
        ),
        patch("app.review_fix_lane_pilot.is_pilot_eligible_project", return_value=True),
    ):
        # No verdict → active.
        assert is_review_fix_lane_pilot_active_for_project(_make_mock_project()) is True

        # KILL verdict → inactive.
        assert (
            is_review_fix_lane_pilot_active_for_project(
                _make_mock_project({LANE_PILOT_CAP_NAME: {"verdict": "KILL"}})
            )
            is False
        )

        # ADOPT verdict → active (ADOPT does not suppress the gate).
        assert (
            is_review_fix_lane_pilot_active_for_project(
                _make_mock_project({LANE_PILOT_CAP_NAME: {"verdict": "ADOPT"}})
            )
            is True
        )

    # Flag off → inactive regardless of project state.
    with (
        patch(
            "app.review_fix_lane_pilot.is_review_fix_lane_pilot_enabled",
            return_value=False,
        ),
        patch("app.review_fix_lane_pilot.is_pilot_eligible_project", return_value=True),
    ):
        assert (
            is_review_fix_lane_pilot_active_for_project(_make_mock_project()) is False
        )


@pytest.mark.asyncio
async def test_lane_pilot_manifest_patch_and_fix_resume_context(db_session, mocker):
    """End-to-end manifest leg: an implement/fix phase checkpoint derives a
    manifest patch that accumulates onto the lane; the next fix spawn's
    wire_fix_lane_context attaches a bounded lane-resume block derived from that
    manifest; the unconditional strip removes the resume key from a review/audit
    spawn (gate integrity); and classic/control contexts stay untouched.

    Exercises production code in review_fix_lane_pilot.py:
    checkpoint_and_reserve_on_phase_complete -> build_lane_manifest_patch,
    wire_fix_lane_context -> build_fix_resume_block, and
    strip_transient_lane_context over LANE_RESUME_CONTEXT_KEY.
    """
    from app.improvement_lane_ops import cas_transition, create_lane
    from app.models import ImprovementLaneStatus
    from app.review_fix_lane_pilot import (
        LANE_RESUME_CONTEXT_KEY,
        checkpoint_and_reserve_on_phase_complete,
        strip_transient_lane_context,
        wire_fix_lane_context,
    )

    # Reachable SHA so the checkpoint is recorded (not degraded to missing).
    mocker.patch(
        "app.github_token_resolver.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github_refs.is_ancestor_of",
        new_callable=AsyncMock,
        return_value=True,
    )

    project = await _make_db_project(db_session, is_self_repo=True)
    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="lane-manifest-leg",
        goal="accumulate manifest",
        branch_name="lane/manifest-leg",
        base_branch="main",
        pilot_arm="lane",
    )
    await db_session.flush()
    await cas_transition(
        db_session,
        lane.id,
        ImprovementLaneStatus.PROPOSED,
        ImprovementLaneStatus.ACTIVE,
        current_task_id=None,
    )

    # Implement/fix phase task carrying durable phase state to harvest.
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "fix", "pr_number": 4242, "base_branch": "main"}],
        context={
            "head_sha": "cafe1234beef5678",
            "_pr_changed_files": ["backend/app/foo.py", "agent/src/bar.ts"],
            "review_loop": {
                "iteration": 2,
                "review_history": [
                    {
                        "issues": [
                            {
                                "severity": "major",
                                "description": "guard null",
                                "file": "foo.py",
                            }
                        ]
                    },
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)
    lane.current_task_id = source_task.id
    db_session.add(lane)
    await db_session.commit()
    await db_session.refresh(lane)

    # (1) Manifest accumulation: the checkpoint records a derived manifest patch.
    wrote = await checkpoint_and_reserve_on_phase_complete(
        db_session,
        source_task,
        lane,
        sha="cafe1234beef5678",
        summary="addressed null-guard review",
    )
    assert wrote is True
    # checkpoint_lane leaves the PARKED transition + manifest merge pending for
    # the caller's commit (lane ops contract) — commit before refresh so the
    # refresh reads persisted state under real ORM expiry.
    await db_session.commit()
    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.PARKED.value
    manifest = lane.manifest or {}
    assert manifest.get("phase_log") == [
        "fix PR#4242 iter2: addressed null-guard review"
    ]
    assert "backend/app/foo.py" in manifest.get("critical_files", [])
    assert manifest.get("remaining") == ["[MAJOR] [foo.py] guard null"]

    # (2) Fix-only resume injection: the next fix ctx gets a bounded resume block.
    fix_ctx: dict = {}
    wire_fix_lane_context(fix_ctx, lane)
    assert fix_ctx["improvement_lane_id"] == str(lane.id)
    assert fix_ctx["is_lane_phase"] is True
    assert fix_ctx["lane_checkpoint_sha"] == "cafe1234beef5678"
    resume = fix_ctx[LANE_RESUME_CONTEXT_KEY]
    assert resume["last_checkpoint_sha"] == "cafe1234beef5678"
    assert resume["phase_log"] == ["fix PR#4242 iter2: addressed null-guard review"]
    assert "backend/app/foo.py" in resume["critical_files"]

    # (3) Review/audit stripping: the unconditional strip removes the resume key
    #     (and the other transient dispatch directives) so a reviewer never sees
    #     stale fix context — gate integrity.
    review_ctx = dict(fix_ctx)
    strip_transient_lane_context(review_ctx)
    assert LANE_RESUME_CONTEXT_KEY not in review_ctx
    assert "improvement_lane_id" not in review_ctx
    assert "is_lane_phase" not in review_ctx
    assert "lane_checkpoint_sha" not in review_ctx

    # (4) Classic/control purity: no lane → no lane keys, no resume content.
    classic_ctx: dict = {"scope": "small"}
    wire_fix_lane_context(classic_ctx, None)
    assert classic_ctx == {"scope": "small"}


# ── Persistent merge conflict escalation ────────────────────────────────────


@pytest.mark.asyncio
async def test_persistent_merge_conflict_escalates_before_cap(db_session, mocker):
    """When the conflict lane has accumulated >= PERSISTENT_CONFLICT_ESCALATION_THRESHOLD
    completions and a new conflict fix would be spawned, _spawn_pipeline_task returns
    (None, False), sets failure_detail='persistent_merge_conflict' on the orig task,
    closes the PR, and the status message explicitly names 'merge conflict'.

    This fires before LANE_CAP_CONFLICT_DEFAULT (4), so the escalation happens at
    iteration 3 (after 2 completed conflict-fix tasks), well below the global cap.
    """
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    close_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    comment_mock = mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    orig = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "small"},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    # Source task simulating a third conflict-fix spawn attempt.
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "fix",
                "pr_number": 555,
                "branch": "feat/conflict",
                "base_branch": "main",
                "original_task_id": str(orig.id),
            }
        ],
        context={
            "scope": "small",
            "review_loop": {
                "iteration": 2,
                "original_task_id": str(orig.id),
                "fix_reason": "merge_conflict",
            },
        },
    )
    db_session.add(source_task)

    # Two COMPLETED conflict-fix tasks for PR #555 — reaches the threshold (2).
    for _ in range(PERSISTENT_CONFLICT_ESCALATION_THRESHOLD):
        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[
                {
                    "type": "fix",
                    "pr_number": 555,
                    "branch": "feat/conflict",
                    "base_branch": "main",
                }
            ],
            context={
                "review_loop": {
                    "fix_reason": "merge_conflict",
                    "original_task_id": str(orig.id),
                }
            },
        )
        db_session.add(t)

    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=555,
        branch="feat/conflict",
        base_branch="main",
        fix_reason="merge_conflict",
        original_task_id=str(orig.id),
    )

    assert result is None, "Persistent conflict should block the spawn"
    assert not created

    await db_session.refresh(orig)
    assert orig.status == TaskStatus.FAILED, (
        f"orig must be FAILED on persistent conflict, got {orig.status}"
    )
    assert (orig.context or {}).get("failure_detail") == "persistent_merge_conflict", (
        f"failure_detail must be 'persistent_merge_conflict', "
        f"got {(orig.context or {}).get('failure_detail')!r}"
    )

    # Status message must name merge conflict explicitly.
    status_msg = orig.latest_status_message or ""
    assert "merge conflict" in status_msg.lower(), (
        f"Status message must mention 'merge conflict': {status_msg!r}"
    )

    terminal_outcome = (orig.context or {}).get("review_loop_terminal_outcome") or {}
    assert terminal_outcome.get("kind") == "persistent_conflict", (
        f"Terminal outcome kind must be 'persistent_conflict': {terminal_outcome!r}"
    )

    close_mock.assert_awaited_once()
    assert close_mock.call_args.args[3] == 555

    # PR comment must mention merge conflict.
    comment_mock.assert_awaited()
    comment_text = comment_mock.call_args[0][4] if comment_mock.call_args else ""
    assert "merge conflict" in comment_text.lower(), (
        f"PR comment must mention 'merge conflict': {comment_text!r}"
    )

    # spawn_reason must be recorded.
    await db_session.refresh(source_task)
    assert (source_task.context or {}).get(
        SPAWN_REASON_KEY
    ) == SPAWN_REASON_PERSISTENT_CONFLICT


@pytest.mark.asyncio
async def test_conflict_resolved_on_first_attempt_no_escalation(db_session, mocker):
    """When only one conflict-fix task exists (below threshold), the second conflict-
    fix spawn proceeds normally — no early escalation."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    project = await _make_db_project(db_session)

    orig = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "small"},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "fix",
                "pr_number": 556,
                "branch": "feat/conflict2",
                "base_branch": "main",
                "original_task_id": str(orig.id),
            }
        ],
        context={
            "scope": "small",
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(orig.id),
                "fix_reason": "merge_conflict",
            },
        },
    )
    db_session.add(source_task)

    # Only ONE prior conflict-fix (below threshold of 2) → no escalation.
    prior_conflict = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 556,
                "branch": "feat/conflict2",
                "base_branch": "main",
            }
        ],
        context={
            "review_loop": {
                "fix_reason": "merge_conflict",
                "original_task_id": str(orig.id),
            }
        },
    )
    db_session.add(prior_conflict)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, _ = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=556,
        branch="feat/conflict2",
        base_branch="main",
        fix_reason="merge_conflict",
        fix_prompt="Resolve merge conflict on feat/conflict2",
        original_task_id=str(orig.id),
    )

    # The orig must NOT have been marked with persistent_merge_conflict —
    # that's the sole assertion: the escalation did not fire.
    await db_session.refresh(orig)
    assert (orig.context or {}).get("failure_detail") != "persistent_merge_conflict", (
        "One conflict-fix attempt is below threshold — no escalation should fire"
    )
    spawn_reason = (source_task.context or {}).get(SPAWN_REASON_KEY)
    assert spawn_reason != SPAWN_REASON_PERSISTENT_CONFLICT, (
        f"spawn_reason must not be persistent_conflict with only 1 prior conflict attempt: {spawn_reason!r}"
    )


@pytest.mark.asyncio
async def test_persistent_conflict_escalation_below_global_cap(db_session, mocker):
    """The persistent-conflict escalation fires well below LANE_CAP_CONFLICT_DEFAULT (4)
    and the global cap, so iterations are not wasted on a hopeless conflict."""
    from app.review_pipeline import LANE_CAP_CONFLICT_DEFAULT, MAX_REVIEW_FIX_ITERATIONS

    assert PERSISTENT_CONFLICT_ESCALATION_THRESHOLD < LANE_CAP_CONFLICT_DEFAULT, (
        f"Persistent-conflict threshold ({PERSISTENT_CONFLICT_ESCALATION_THRESHOLD}) "
        f"must be below the lane sub-cap ({LANE_CAP_CONFLICT_DEFAULT}) — "
        f"otherwise the lane cap fires first and the explicit conflict message is lost"
    )
    assert PERSISTENT_CONFLICT_ESCALATION_THRESHOLD < MAX_REVIEW_FIX_ITERATIONS, (
        f"Persistent-conflict threshold ({PERSISTENT_CONFLICT_ESCALATION_THRESHOLD}) "
        f"must be well below the global cap ({MAX_REVIEW_FIX_ITERATIONS})"
    )


# ── review_history per-entry feedback size cap ────────────────────────────────
#
# A Postgres saturation outage was traced to task.context JSONB growing to
# multiple MB because review_loop.review_history appends one entry per fix
# iteration and each entry stores the reviewer's freeform feedback verbatim. On
# the verdict-parse fallback path that feedback is the agent's ENTIRE log, so a
# SINGLE entry can be multi-MB. task.context is GIN-indexed, so every such write
# pays a TOAST rewrite + GIN reindex → the multi-second single-row PK updates
# that saturated Postgres. A count cap on entries is necessary but NOT
# sufficient; each entry's feedback SIZE must also be bounded. The freeform
# feedback is read only by the inter-iteration similarity comparison
# (_feedback_jaccard); structured issues/changed_files/verdict are untouched.


def test_cap_review_history_feedback_helper():
    """_cap_review_history_feedback truncates oversized feedback to the cap,
    passes through short / boundary / non-string entries unchanged, preserves all
    other keys, and never mutates its input (pure)."""
    big = "z" * (MAX_REVIEW_HISTORY_FEEDBACK_CHARS + 500)
    history = [
        {
            "iteration": 0,
            "feedback": big,
            "issues": [{"description": "x", "severity": "CRITICAL"}],
            "verdict": "REQUEST_CHANGES",
            "changed_files": ["a.py"],
        },
        {"iteration": 1, "feedback": "short"},
        {"iteration": 2, "feedback": None},  # non-string feedback left alone
        {"iteration": 3},  # no feedback key at all
        {"iteration": 4, "feedback": "x" * MAX_REVIEW_HISTORY_FEEDBACK_CHARS},  # at cap
    ]
    capped = _cap_review_history_feedback(history)

    # Oversized entry truncated to EXACTLY the cap; every other key preserved.
    assert len(capped[0]["feedback"]) == MAX_REVIEW_HISTORY_FEEDBACK_CHARS
    assert capped[0]["feedback"] == big[:MAX_REVIEW_HISTORY_FEEDBACK_CHARS]
    assert capped[0]["issues"] == [{"description": "x", "severity": "CRITICAL"}]
    assert capped[0]["verdict"] == "REQUEST_CHANGES"
    assert capped[0]["changed_files"] == ["a.py"]
    assert capped[0]["iteration"] == 0

    # Short / non-string / missing / exactly-at-cap entries pass through unchanged
    # (the condition is len > cap, so a string of exactly cap length is verbatim —
    # this guards the off-by-one boundary).
    assert capped[1]["feedback"] == "short"
    assert capped[2]["feedback"] is None
    assert "feedback" not in capped[3]
    assert len(capped[4]["feedback"]) == MAX_REVIEW_HISTORY_FEEDBACK_CHARS

    # Purity: the input list and its entries are never mutated.
    assert len(history[0]["feedback"]) == MAX_REVIEW_HISTORY_FEEDBACK_CHARS + 500
    assert history[0] is not capped[0]  # oversized entry was shallow-copied
    assert history[1] is capped[1]  # within-bounds entry passed by reference

    # Custom max_chars honored; empty / None history returns a fresh list.
    custom = _cap_review_history_feedback([{"feedback": "abcdef"}], max_chars=3)
    assert custom[0]["feedback"] == "abc"
    assert _cap_review_history_feedback([]) == []
    assert _cap_review_history_feedback(None) == []


@pytest.mark.asyncio
async def test_review_history_feedback_is_size_capped(db_session, mocker):
    """Acceptance: after many fix iterations each carrying a multi-MB fallback
    feedback log, every stored review_history entry's feedback is byte-bounded
    and the serialized task.context stays small (no fat row).

    The count cap alone (N entries) cannot prevent a fat row when each entry is
    multi-MB — the per-entry size cap must. Spawn iteration is 1 (source
    iteration=0), which skips stuck-cycle and early-nonconvergence detection so
    the fix task is actually created and we can inspect what gets persisted.
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

    # 12 prior iterations (>10), each carrying an oversized feedback blob (5x the
    # cap) plus a DISTINCT critical issue (distinct issues keep churn/stuck
    # detectors quiet — not that they run at iteration 1).
    oversized = MAX_REVIEW_HISTORY_FEEDBACK_CHARS * 5
    prior_history = [
        {
            "iteration": i,
            "feedback": f"iter{i} " + ("F" * oversized),
            "issues": [_issue(f"distinct critical issue number {i}", "CRITICAL")],
            "verdict": "REQUEST_CHANGES",
        }
        for i in range(12)
    ]
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 720,
                "branch": "b-cap",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 0,
                "original_task_id": str(original_task.id),
                "pr_number": 720,
                "branch": "b-cap",
                "base_branch": "main",
                "review_history": prior_history,
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    # The verdict-parse fallback: current_feedback IS the agent's entire log (~2 MB).
    huge_log = "L" * 2_000_000

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=720,
        branch="b-cap",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Address the review feedback",
        current_feedback=huge_log,
        current_issues=[_issue("a fresh critical issue", "CRITICAL")],
    )

    assert created is True and result is not None, (
        "iteration-1 spawn skips stuck/nonconvergence detection — the fix task "
        "must be created"
    )

    from app.context_bounds import REVIEW_FEEDBACK_MAX_CHARS, _TRUNCATION_MARKER

    history = result.context["review_loop"]["review_history"]
    assert len(history) == 13, "12 carried-forward entries + 1 newly appended entry"

    # Every stored feedback (carried-forward AND the new full-log entry) is bounded.
    # The cap is REVIEW_FEEDBACK_MAX_CHARS (8192) from context_bounds, which replaced
    # the legacy MAX_REVIEW_HISTORY_FEEDBACK_CHARS (4000) as the canonical bound.
    for entry in history:
        assert len(entry["feedback"]) <= REVIEW_FEEDBACK_MAX_CHARS, (
            f"entry {entry.get('iteration')} feedback is {len(entry['feedback'])} "
            f"chars — exceeds cap {REVIEW_FEEDBACK_MAX_CHARS}"
        )
    # The new entry (the 2 MB full-log fallback) is truncated to exactly the cap with marker.
    assert len(history[-1]["feedback"]) == REVIEW_FEEDBACK_MAX_CHARS
    assert history[-1]["feedback"].endswith(_TRUNCATION_MARKER)

    # Structured fields (what every non-similarity consumer reads) survive intact.
    assert history[-1]["issues"] == [_issue("a fresh critical issue", "CRITICAL")]
    assert history[0]["issues"] == [
        _issue("distinct critical issue number 0", "CRITICAL")
    ]

    # The persisted row stays small: >2 MB of input feedback collapses to a
    # serialized context that is a tiny fraction of the multi-MB fat-row regime
    # that produced the >500ms single-row PK updates.
    serialized = json.dumps(result.context)
    assert len(serialized) < 200_000, (
        f"serialized context is {len(serialized)} bytes — must stay well under "
        "the multi-MB fat-row regime that saturated Postgres"
    )


@pytest.mark.asyncio
async def test_review_history_carried_forward_feedback_is_recapped(db_session, mocker):
    """A pre-existing oversized carried-forward entry (written before this cap was
    deployed) is re-capped when the next fix spawns, while a fresh short feedback
    is stored verbatim (no off-by-one truncation)."""
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

    pre_deploy_fat = "P" * (MAX_REVIEW_HISTORY_FEEDBACK_CHARS + 12_345)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 721,
                "branch": "b-recap",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 0,
                "original_task_id": str(original_task.id),
                "pr_number": 721,
                "branch": "b-recap",
                "base_branch": "main",
                "review_history": [
                    {
                        "iteration": 0,
                        "feedback": pre_deploy_fat,
                        "issues": [_issue("legacy issue", "MAJOR")],
                        "verdict": "REQUEST_CHANGES",
                    }
                ],
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    short_feedback = "Short reviewer note that is well under the cap."
    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=721,
        branch="b-recap",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Fix the legacy issue",
        current_feedback=short_feedback,
        current_issues=[_issue("a new issue", "CRITICAL")],
    )

    from app.context_bounds import REVIEW_FEEDBACK_MAX_CHARS, _TRUNCATION_MARKER

    assert created is True and result is not None
    history = result.context["review_loop"]["review_history"]
    assert len(history) == 2

    # The carried-forward pre-deploy fat entry is re-capped, keys preserved.
    # Cap is REVIEW_FEEDBACK_MAX_CHARS (8192) from context_bounds (canonical bound).
    assert len(history[0]["feedback"]) == REVIEW_FEEDBACK_MAX_CHARS
    assert history[0]["feedback"] == (
        pre_deploy_fat[: REVIEW_FEEDBACK_MAX_CHARS - len(_TRUNCATION_MARKER)]
        + _TRUNCATION_MARKER
    )
    assert history[0]["issues"] == [_issue("legacy issue", "MAJOR")]

    # The fresh short feedback is stored verbatim — capping must not mangle it.
    assert history[1]["feedback"] == short_feedback


@pytest.mark.asyncio
async def test_stuck_cycle_still_detected_after_feedback_cap(db_session, mocker):
    """Capping feedback must not break stuck-cycle detection. Two oversized,
    near-identical feedback logs (the verdict-parse fallback shape, no structured
    issues on either side) are both truncated to the cap BEFORE the Jaccard
    comparison runs, so the bounded prefixes still register as a stuck cycle."""
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

    # A shared body that exceeds the cap, so the first MAX_..._CHARS of both the
    # prior and current feedback are identical. Real word tokens (≥3 chars) so the
    # token-set Jaccard has signal.
    shared_body = (
        "the retry backoff in pipeline health exceeds the configured timeout "
        "window and the integration tests keep failing on every iteration "
    ) * 60
    assert len(shared_body) > MAX_REVIEW_HISTORY_FEEDBACK_CHARS
    prior_feedback = shared_body + (" previousuniquetail " * 3000)
    current_feedback = shared_body + (" currentuniquetail " * 3000)

    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 722,
                "branch": "b-stuck",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "iteration": 1,  # spawn iteration becomes 2 → stuck check runs
                "original_task_id": str(original_task.id),
                "pr_number": 722,
                "branch": "b-stuck",
                "base_branch": "main",
                "review_history": [
                    # No `issues` key → forces the feedback-text fallback path.
                    {"iteration": 0, "feedback": prior_feedback},
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
        pr_number=722,
        branch="b-stuck",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_prompt="Same failure again",
        current_feedback=current_feedback,
        current_issues=None,  # no structured issues this iteration either
    )

    assert result is None, (
        "Oversized near-identical feedback must still trip stuck-cycle detection "
        "after capping — the cap is applied before the Jaccard read"
    )
    await db_session.refresh(original_task)
    assert original_task.requires_approval is True
    assert "stuck" in (original_task.latest_status_message or "").lower(), (
        f"Status message should mention stuck cycle: {original_task.latest_status_message!r}"
    )


@pytest.mark.asyncio
async def test_spawn_review_second_opinion_forwards_viewport_shots(db_session):
    """panel_review challenger payload carries viewport_shot_entries forwarded
    from the review task when the review payload contains them."""
    from app.task_evaluators import spawn_review_second_opinion

    project = Project(name="vp-test", user_id="user-1", github_repo="o/r")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    shot_entries = [
        {"name": "before/comp/renders-375px"},
        {"name": "after/comp/renders-375px"},
    ]
    review_task = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "review",
                "pr_number": 9001,
                "branch": "feat/vp",
                "base_branch": "main",
                "original_task_id": str(uuid.uuid4()),
                "viewport_shot_entries": shot_entries,
                "viewport_source_task_id": "src-task-abc",
            }
        ],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={},
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    challenger = await spawn_review_second_opinion(
        review_task,
        db_session,
        verdict="APPROVE",
        issues=[],
        feedback="lgtm",
        pr_number=9001,
        branch="feat/vp",
        base_branch="main",
        original_task_id=str(uuid.uuid4()),
        original_prompt="do the thing",
        investigated_files=["app/x.py"],
    )

    assert challenger is not None
    cp = challenger.first_payload
    assert cp.get("type") == "panel_review"
    assert cp.get("viewport_shot_entries") == shot_entries, (
        "challenger payload must carry viewport_shot_entries forwarded from the review task"
    )
    assert cp.get("viewport_source_task_id") == "src-task-abc"


async def test_spawn_blind_multi_reviewer_creates_panel_review_task(db_session):
    """spawn_blind_multi_reviewer creates a panel_review challenger with
    is_blind: True, default reviewer count, forwarded viewport shots,
    and both REVIEW_SECOND_OPINION_SOURCE_KEY + BLIND_REVIEW_SOURCE_KEY
    in context. The source review task is parked EVALUATING."""
    from app.task_evaluators import (
        BLIND_REVIEW_SOURCE_KEY,
        REVIEW_SECOND_OPINION_SOURCE_KEY,
        spawn_blind_multi_reviewer,
    )
    from app.task_completion import REVIEW_SECOND_OPINION_AWAITING_KEY

    project = Project(name="blind-test", user_id="user-1", github_repo="o/r")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    shot_entries = [
        {"name": "before/comp/blind-375px"},
    ]
    source_task = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "review",
                "pr_number": 9002,
                "branch": "feat/blind",
                "base_branch": "main",
                "original_task_id": str(uuid.uuid4()),
                "viewport_shot_entries": shot_entries,
                "viewport_source_task_id": "src-blind-abc",
            }
        ],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    challenger = await spawn_blind_multi_reviewer(
        source_task,
        db_session,
        verdict="APPROVE",
        issues=[],
        feedback="lgtm",
        pr_number=9002,
        branch="feat/blind",
        base_branch="main",
        original_task_id=str(uuid.uuid4()),
        original_prompt="do the thing",
        investigated_files=["app/x.py"],
    )

    assert challenger is not None
    cp = challenger.first_payload
    assert cp.get("type") == "panel_review"
    assert cp.get("is_blind") is True, "blind multi-reviewer: is_blind must be True"
    assert cp.get("blind_reviewer_count") == 3, (
        "default blind_reviewer_count must be 3"
    )
    assert cp.get("viewport_shot_entries") == shot_entries, (
        "challenger payload must carry viewport_shot_entries forwarded from the source task"
    )
    assert cp.get("viewport_source_task_id") == "src-blind-abc"

    # Context must carry both source keys for routing and identification.
    ctx = challenger.context or {}
    assert REVIEW_SECOND_OPINION_SOURCE_KEY in ctx, (
        "blind challenger context must have REVIEW_SECOND_OPINION_SOURCE_KEY for reconciliation routing"
    )
    assert BLIND_REVIEW_SOURCE_KEY in ctx, (
        "blind challenger context must have BLIND_REVIEW_SOURCE_KEY for identification"
    )
    assert ctx[REVIEW_SECOND_OPINION_SOURCE_KEY] == str(source_task.id)
    assert ctx[BLIND_REVIEW_SOURCE_KEY] == str(source_task.id)

    # Source task must be parked EVALUATING with the awaiting marker.
    assert source_task.status == TaskStatus.EVALUATING, (
        "source task must be parked EVALUATING pending blind review"
    )
    assert (source_task.context or {}).get(REVIEW_SECOND_OPINION_AWAITING_KEY) is not False, (  # truthy (True or task id str)
        "source task must have REVIEW_SECOND_OPINION_AWAITING_KEY set"
    )

    # Cleanup: can't leave a parked task in the session without committing
    # the parking state. The test already committed through the spawn; just
    # verify the DB state is consistent.
    await db_session.refresh(source_task)
    assert source_task.status == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_noop_fix_skips_review_spawn_below_churn_limit(db_session, mocker):
    """A completed fix task with no_op_reason set must NOT spawn a new review
    even when the supersession-churn limit has not been reached.  The no-op fix
    short-circuit must close the chain as converged (deliverable already present
    via another path) instead of spawning a fresh review that would loop."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.review_pipeline._REVIEW_SUPERSEDE_CHURN_LIMIT", 5)

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

    # Source task: COMPLETED fix with no_op_reason (zero net diff after rebase).
    noop_fix = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "pr_number": 800,
                "branch": "feat/noop-spawn",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "small",
            "no_op_reason": "deliverable already present on main — no new commits",
            "review_loop": {
                "iteration": 2,
                "original_task_id": str(original_task.id),
            },
        },
    )
    db_session.add(noop_fix)
    await db_session.commit()
    await db_session.refresh(noop_fix)

    result, created = await _spawn_pipeline_task(
        noop_fix,
        "review",
        db_session,
        pr_number=800,
        branch="feat/noop-spawn",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    await db_session.commit()

    # No review should be spawned — short-circuit closed the chain.
    assert result is None, "Expected no review task (no-op fix short-circuit)"
    assert created is False, "Expected no new task (no-op fix short-circuit)"

    # Original task must carry the convergence terminal outcome.
    await db_session.refresh(original_task)
    term = (original_task.context or {}).get("review_loop_terminal_outcome", {})
    assert term.get("kind") == "converged", (
        f"Expected converged terminal outcome, got {term.get('kind')}"
    )
    assert term.get("reason") == "noop_fix_already_satisfied", (
        f"Expected noop_fix_already_satisfied reason, got {term.get('reason')}"
    )

    # Source task should have a descriptive status message.
    await db_session.refresh(noop_fix)
    assert "No-op fix" in (noop_fix.latest_status_message or ""), (
        "Source task status message should indicate no-op fix closure"
    )


# ── R3: stale-supersede must not blind-overwrite a live reviewer ─────────────
#
# The per-PR advisory lock serializes SPAWNERS only — it does NOT serialize
# against a reviewer agent committing its OWN terminal status. The supersede
# decision reads `active_pipeline` with a plain UNLOCKED SELECT and the cancel is
# an ORM UPDATE-by-PK with no WHERE-status guard (and version_id_col was dropped
# globally, so there is no optimistic-concurrency backstop). Two consequences,
# both covered below:
#   1. Task.last_heartbeat goes STALE on a LIVE agent whose row write was
#      409-skipped under contention (routers/agent.py:4525-4545). VmContact is
#      written off the contended row and cannot be starved — corroborate against
#      it before declaring a task dead (mirrors reaper.py:481).
#   2. A reviewer that reaches COMPLETED inside the supersede window is
#      blind-overwritten to CANCELLED, and its context blob (carrying the fresh
#      APPROVE verdict) is replaced from the stale snapshot — the verdict is
#      ERASED, not merely superseded.


async def _make_stale_review(db_session, project, original_task, *, pr_number, vm_id=None):
    stale_started = _utcnow_for_test() - timedelta(
        minutes=PIPELINE_TASK_STALE_MINUTES + 5
    )
    review = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "review",
                "pr_number": pr_number,
                "branch": "feat/r3",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        started_at=stale_started,
    )
    review.last_heartbeat = stale_started
    review.vm_instance_id = vm_id
    db_session.add(review)
    await db_session.commit()
    await db_session.refresh(review)
    return review


def _make_r3_source(project, original_task, *, pr_number):
    return make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": pr_number,
                "branch": "feat/r3",
                "base_branch": "main",
                "prompt": "Build X",
            }
        ],
        context={
            "scope": "small",
            "review_loop": {"iteration": 0, "original_task_id": str(original_task.id)},
        },
    )


@pytest.mark.asyncio
async def test_lock_starved_but_live_reviewer_is_not_superseded(db_session, mocker):
    """R3: a LIVE reviewer whose on-row heartbeat is lock-starved (stale
    last_heartbeat) but whose VmContact is FRESH must NOT be cancelled.

    This is the false-stale premise the codebase already documents: the heartbeat
    handler writes the Task row with_for_update(skip_locked=True) and is
    409-skipped under contention, so last_heartbeat goes stale on an agent that
    is very much alive. The reaper grew a VmContact override for exactly this
    (reaper.py:481); the supersede path never did — so it cancelled the live
    reviewer and deleted its VM.
    """
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    delete_vm = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock
    )

    from app.models import VmContact

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

    live_review = await _make_stale_review(
        db_session, project, original_task, pr_number=920, vm_id="vm-live-1"
    )
    # The agent IS alive: it is still calling in, and every contact lands on its
    # OWN uncontended VmContact row even when the Task-row write is skipped.
    db_session.add(
        VmContact(vm_instance_id="vm-live-1", last_contact_at=_utcnow_for_test())
    )
    source_task = _make_r3_source(project, original_task, pr_number=920)
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=920,
        branch="feat/r3",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    await db_session.commit()
    await db_session.refresh(live_review)

    assert live_review.status == TaskStatus.IN_PROGRESS, (
        "a LIVE reviewer (fresh VmContact, lock-starved heartbeat) was cancelled "
        "as a corpse"
    )
    assert (live_review.context or {}).get("failure_detail") is None
    delete_vm.assert_not_awaited()
    # Same-type dedup: the live review is trusted to finish, no duplicate spawn.
    assert created is False
    assert result is not None and result.id == live_review.id


@pytest.mark.asyncio
async def test_genuinely_dead_reviewer_is_still_superseded(db_session, mocker):
    """R3 negative control: no VmContact at all (crashed / preempted / OOM VM
    sends no requests) → the stale reviewer IS still superseded. The fix must not
    turn the supersede path off."""
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

    dead_review = await _make_stale_review(
        db_session, project, original_task, pr_number=921, vm_id="vm-dead-1"
    )
    # No VmContact row for vm-dead-1 — the VM is genuinely gone.
    source_task = _make_r3_source(project, original_task, pr_number=921)
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    _result, created = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=921,
        branch="feat/r3",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    await db_session.commit()
    await db_session.refresh(dead_review)

    assert dead_review.status == TaskStatus.CANCELLED
    assert (dead_review.context or {}).get("failure_detail") == (
        "superseded_by_review_pipeline"
    )
    assert created is True


@pytest.mark.asyncio
async def test_stale_vm_contact_does_not_rescue_a_dead_reviewer(db_session, mocker):
    """R3 negative control 2: a VmContact that is ITSELF older than the cutoff is
    not proof of life — the reviewer is still superseded."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    from app.models import VmContact

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

    dead_review = await _make_stale_review(
        db_session, project, original_task, pr_number=922, vm_id="vm-dead-2"
    )
    db_session.add(
        VmContact(
            vm_instance_id="vm-dead-2",
            last_contact_at=_utcnow_for_test()
            - timedelta(minutes=PIPELINE_TASK_STALE_MINUTES + 5),
        )
    )
    source_task = _make_r3_source(project, original_task, pr_number=922)
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=922,
        branch="feat/r3",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    await db_session.commit()
    await db_session.refresh(dead_review)

    assert dead_review.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_reviewer_that_completes_inside_supersede_window_keeps_its_verdict(
    db_session, mocker
):
    """R3: a reviewer that commits COMPLETED + APPROVE *inside* the supersede
    window must NOT be blind-overwritten to CANCELLED, and its verdict must
    survive.

    The window is simulated at its exact boundary: the candidate is selected and
    judged stale from the unlocked snapshot, and the agent's own terminal write
    lands before the destructive UPDATE. Without the FOR UPDATE re-select +
    status re-assert, the ORM UPDATE-by-PK overwrites status AND rewrites the
    whole context blob from the stale snapshot, ERASING the APPROVE verdict.
    """
    from app import review_pipeline
    from sqlalchemy.orm.attributes import flag_modified

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    delete_vm = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock
    )

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

    racing_review = await _make_stale_review(
        db_session, project, original_task, pr_number=923, vm_id="vm-race-1"
    )
    source_task = _make_r3_source(project, original_task, pr_number=923)
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    review_id = racing_review.id
    real_relock = review_pipeline._relock_pipeline_task_for_cancel

    async def _agent_commits_then_relock(session, task_id):
        # The agent's completion handler wins the race: it locks its own row and
        # commits COMPLETED + APPROVE just before we try to cancel it.
        if task_id == review_id:
            t = await session.get(Task, task_id)
            ctx = dict(t.context or {})
            ctx["blueprint_review_verdict"] = "APPROVE"
            t.context = ctx
            t.status = TaskStatus.COMPLETED
            flag_modified(t, "context")
            session.add(t)
            await session.commit()
        return await real_relock(session, task_id)

    mocker.patch.object(
        review_pipeline,
        "_relock_pipeline_task_for_cancel",
        side_effect=_agent_commits_then_relock,
    )

    await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=923,
        branch="feat/r3",
        base_branch="main",
        original_task_id=str(original_task.id),
    )
    await db_session.commit()

    db_session.expunge_all()
    landed = await db_session.get(Task, review_id)
    assert landed.status == TaskStatus.COMPLETED, (
        "a reviewer that committed COMPLETED inside the supersede window was "
        "blind-overwritten to CANCELLED by an unguarded UPDATE-by-PK"
    )
    assert (landed.context or {}).get("blueprint_review_verdict") == "APPROVE", (
        "the APPROVE verdict was ERASED — the cancel rewrote the whole context "
        "blob from the pre-race snapshot"
    )
    assert (landed.context or {}).get("failure_detail") is None
    delete_vm.assert_not_awaited()

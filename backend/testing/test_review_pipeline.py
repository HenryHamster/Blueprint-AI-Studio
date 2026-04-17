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

import pytest
from unittest.mock import AsyncMock, patch

from app.models import Project, Task, TaskStatus
from app.review_pipeline import (
    MAX_REVIEW_FIX_ITERATIONS,
    STUCK_CYCLE_THRESHOLD_DEFAULT,
    STUCK_CYCLE_THRESHOLD_REFACTOR,
    _max_review_iterations,
    _spawn_pipeline_task,
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
        assert STUCK_CYCLE_THRESHOLD_DEFAULT == 0.5
        assert STUCK_CYCLE_THRESHOLD_REFACTOR == 0.75

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


# ── _spawn_pipeline_task: iteration cap ───────────────────────────────────────


@pytest.mark.asyncio
async def test_global_cap_blocks_spawn(db_session, mocker):
    """When the global completed-task count for a PR reaches the cap,
    _spawn_pipeline_task returns None without creating a new task, marks the
    chain's root task requires_approval=True with a 'manual review required'
    status message, and fires a failure alert so the user is notified."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock)
    alert_mock = mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)

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
    assert original_task.requires_approval is True, (
        "Global cap must set requires_approval=True on the root task"
    )
    assert "manual review" in (original_task.latest_status_message or "").lower(), (
        f"Status message should mention manual review: {original_task.latest_status_message!r}"
    )
    alert_mock.assert_called_once()
    call_args = alert_mock.call_args[0]
    assert "cap" in call_args[2].lower() or "manual" in call_args[2].lower(), (
        f"Alert should mention cap or manual intervention, got: {call_args[2]!r}"
    )


@pytest.mark.asyncio
async def test_cap_paths_symmetric_terminal_state(db_session, mocker):
    """Both the global-cap and per-chain-cap paths must produce a symmetric
    terminal state: requires_approval=True on the root task with a
    'manual review required' status message. This prevents silent-block
    conditions where one cap path marks the chain but the other does not."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)

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
        assert root.requires_approval is True, f"{label} cap must set requires_approval"
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
    returns None, sets requires_approval=True on the original task, and fires
    a failure alert so the user is notified."""
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock)
    alert_mock = mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)

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
    assert original_task.requires_approval is True, (
        "Per-chain cap must set requires_approval=True on the original task"
    )
    assert "manual review" in (original_task.latest_status_message or "").lower(), (
        f"Status message should mention manual review: {original_task.latest_status_message!r}"
    )
    alert_mock.assert_called_once()
    call_args = alert_mock.call_args[0]
    assert "cap" in call_args[2].lower() or "manual" in call_args[2].lower(), (
        f"Alert should mention cap or manual review, got: {call_args[2]!r}"
    )


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
        _issue("issue A", "CRITICAL"),
        _issue("issue B", "CRITICAL"),
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

    # 3 current issues, only 1 repeats (33% < 50% threshold).
    current_issues = [
        _issue("issue A", "CRITICAL"),   # repeats
        _issue("new issue C", "CRITICAL"),
        _issue("new issue D", "MAJOR"),
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
async def test_refactor_stuck_threshold_is_75pct(db_session, mocker):
    """For refactor source tasks, the stuck threshold is 75%, not 50%.
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

    # 3 of 5 current issues repeat → 60%, below 75% refactor threshold.
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
        "60% repeat rate is below the 75% refactor threshold — spawn should proceed"
    )
    assert result.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_refactor_stuck_at_80pct(db_session, mocker):
    """For refactor source tasks, 80% repeat rate exceeds the 75% threshold → stuck."""
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

    # 4 of 5 current issues repeat → 80% ≥ 75% → stuck.
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

    assert result is None, "80% repeat rate exceeds the 75% refactor threshold → stuck"

    await db_session.refresh(original_task)
    assert original_task.requires_approval is True


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

"""Decision-parity tests for the consolidated review-skip policy.

These tests pin the review-skip *decisions* that were previously scattered
across ``review_heuristics.py`` and inline blocks in ``task_lifecycle.py`` and
prove the consolidated ``review_policy`` module produces IDENTICAL outcomes.

Each test compares a faithful reference implementation of the OLD control flow
(the wiring that lived inline in ``task_lifecycle.py``) against the new
``review_policy`` composite functions, across every documented rule vector:

  1. Initial-review skip           → should_skip_initial_review
  2. Auto-approve-small-main gate  → evaluate_auto_approve_small_main
  3. Re-review incl. scan-blocked  → should_skip_re_review

All functions under test are pure (no DB / no async), so these run in the
standalone suite without a database.
"""

import pytest

from app.direction_metrics import _compute_acceptance_rate
from app.review_policy import (
    AUTO_APPROVE_MIN_ACCEPTANCE_RATE,
    evaluate_auto_approve_small_main,
    should_auto_approve_diff,
    should_skip_initial_review,
    should_skip_re_review,
    should_skip_rereview,
)
from app.shared_state import runtime_config
from testing.factories import make_task


def _task(*, type_="create", scope="small", files=None, diff=None):
    payload = {"type": type_}
    ctx: dict = {"scope": scope}
    if files is not None:
        ctx["_pr_changed_files"] = files
    if diff is not None:
        ctx["_pr_diff_summary"] = diff
    return make_task(payloads=[payload], context=ctx)


# Direction fixtures exercised through the real _compute_acceptance_rate.
_DIR_FAVORABLE = {"history_aggregate": {"total_prs_merged": 9, "total_prs_rejected": 1}}
_DIR_POOR = {"history_aggregate": {"total_prs_merged": 1, "total_prs_rejected": 9}}
_DIR_UNKNOWN: dict = {}  # no PR history → acceptance is None


# ── Reference implementations of the OLD inline control flow ──────────────────


def _old_initial_review_skip(is_intermediate: bool, scope: str) -> tuple[bool, str]:
    """Mirrors the documented initial-review skip table verbatim."""
    if not is_intermediate:
        return (True, "non-intermediate branch: CI and promotion gate backstop review")
    if scope != "large":
        return (True, f"{scope} scope on intermediate branch: rollup PR gets full review")
    return (False, f"{scope} scope on intermediate branch: full review required")


def _old_auto_approve_small_main_skip(
    task, is_intermediate: bool, scope: str, direction_data: dict | None
) -> bool:
    """Mirrors the OLD task_lifecycle inline gate: returns whether
    ``auto_approve_small`` would have been set True."""
    if is_intermediate or scope != "small":
        return False
    if not runtime_config.get("auto_approve_small_diff_enabled", True):
        return False
    acceptance = (
        _compute_acceptance_rate(direction_data) if direction_data is not None else None
    )
    if acceptance is None or acceptance <= AUTO_APPROVE_MIN_ACCEPTANCE_RATE:
        return False
    skip, _ = should_auto_approve_diff(task, direction_data)
    return skip


def _old_rereview_outcome(
    fix_reason: str, skip_rereview_minor_only: bool, scan_blocked: bool
) -> str:
    """Mirrors the OLD three-way task_lifecycle branch:
    ``if not scan_blocked and skip: ci_wait / elif not scan_blocked: spawn / else: blocked``."""
    skip, _ = should_skip_rereview(fix_reason, skip_rereview_minor_only)
    if not scan_blocked and skip:
        return "ci_wait"
    if not scan_blocked:
        return "spawn_review"
    return "blocked"


def _new_rereview_outcome(
    fix_reason: str, skip_rereview_minor_only: bool, scan_blocked: bool
) -> str:
    d = should_skip_re_review(fix_reason, skip_rereview_minor_only, scan_blocked)
    if d.rule == "security_blocked":
        return "blocked"
    if d.skip:
        return "ci_wait"
    return "spawn_review"


# ── Parity: initial-review skip ───────────────────────────────────────────────


_INITIAL_REVIEW_VECTORS = [
    (False, "small"),
    (False, "medium"),
    (False, "large"),
    (True, "small"),
    (True, "medium"),
    (True, "large"),
    (True, "unknown"),
]


class TestInitialReviewSkipParity:
    @pytest.mark.parametrize("is_intermediate,scope", _INITIAL_REVIEW_VECTORS)
    def test_matches_reference(self, is_intermediate, scope):
        assert should_skip_initial_review(is_intermediate, scope) == _old_initial_review_skip(
            is_intermediate, scope
        )

    def test_non_intermediate_always_skips(self):
        skip, _ = should_skip_initial_review(False, "large")
        assert skip is True

    def test_intermediate_large_requires_review(self):
        skip, _ = should_skip_initial_review(True, "large")
        assert skip is False

    def test_intermediate_small_medium_skips(self):
        assert should_skip_initial_review(True, "small")[0] is True
        assert should_skip_initial_review(True, "medium")[0] is True


# ── Parity: auto-approve-small-main gate ──────────────────────────────────────


class TestAutoApproveSmallMainParity:
    def _small_task(self):
        return _task(
            scope="small",
            files=["app/foo.py"],
            diff=[{"file": "app/foo.py", "added": 10, "removed": 2}],
        )

    def _big_task(self):
        return _task(
            scope="small",
            files=["app/foo.py"],
            diff=[{"file": "app/foo.py", "added": 500, "removed": 500}],
        )

    def test_favorable_small_diff_skips(self):
        t = self._small_task()
        d = evaluate_auto_approve_small_main(t, False, "small", _DIR_FAVORABLE)
        assert d.skip is True
        assert d.rule == "auto_approve_small"
        assert d.skip == _old_auto_approve_small_main_skip(t, False, "small", _DIR_FAVORABLE)

    def test_poor_direction_rejected(self):
        t = self._small_task()
        d = evaluate_auto_approve_small_main(t, False, "small", _DIR_POOR)
        assert d.skip is False
        assert d.rule == "direction_acceptance_gate"
        assert d.skip == _old_auto_approve_small_main_skip(t, False, "small", _DIR_POOR)

    def test_unknown_direction_rejected(self):
        t = self._small_task()
        d = evaluate_auto_approve_small_main(t, False, "small", _DIR_UNKNOWN)
        assert d.skip is False
        assert d.rule == "direction_acceptance_gate"
        assert d.skip == _old_auto_approve_small_main_skip(t, False, "small", _DIR_UNKNOWN)

    def test_none_direction_rejected(self):
        t = self._small_task()
        d = evaluate_auto_approve_small_main(t, False, "small", None)
        assert d.skip is False
        assert d.skip == _old_auto_approve_small_main_skip(t, False, "small", None)

    def test_intermediate_never_auto_approves(self):
        t = self._small_task()
        d = evaluate_auto_approve_small_main(t, True, "small", _DIR_FAVORABLE)
        assert d.skip is False
        assert d.skip == _old_auto_approve_small_main_skip(t, True, "small", _DIR_FAVORABLE)

    def test_medium_scope_not_eligible_on_main(self):
        t = _task(
            scope="medium",
            files=["app/foo.py"],
            diff=[{"file": "app/foo.py", "added": 5, "removed": 0}],
        )
        d = evaluate_auto_approve_small_main(t, False, "medium", _DIR_FAVORABLE)
        assert d.skip is False
        assert d.skip == _old_auto_approve_small_main_skip(t, False, "medium", _DIR_FAVORABLE)

    def test_favorable_but_diff_too_big_rejected(self):
        t = self._big_task()
        d = evaluate_auto_approve_small_main(t, False, "small", _DIR_FAVORABLE)
        assert d.skip is False
        assert d.rule == "none"
        assert d.skip == _old_auto_approve_small_main_skip(t, False, "small", _DIR_FAVORABLE)

    def test_disabled_via_config(self, mocker):
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"auto_approve_small_diff_enabled": False},
        )
        t = self._small_task()
        d = evaluate_auto_approve_small_main(t, False, "small", _DIR_FAVORABLE)
        assert d.skip is False
        assert d.skip == _old_auto_approve_small_main_skip(t, False, "small", _DIR_FAVORABLE)


# ── Parity: re-review incl. scan-blocked override ─────────────────────────────


_FIX_REASONS = ["ci_failure", "merge_conflict", "migration_guard", "code_review"]


class TestReReviewSkipParity:
    @pytest.mark.parametrize("fix_reason", _FIX_REASONS)
    @pytest.mark.parametrize("minor_only", [True, False])
    @pytest.mark.parametrize("scan_blocked", [True, False])
    def test_matches_reference(self, fix_reason, minor_only, scan_blocked):
        assert _new_rereview_outcome(fix_reason, minor_only, scan_blocked) == _old_rereview_outcome(
            fix_reason, minor_only, scan_blocked
        )

    def test_scan_blocked_overrides_everything(self):
        # Even a skip-eligible ci_failure fix must NOT skip or spawn when blocked.
        d = should_skip_re_review("ci_failure", False, scan_blocked=True)
        assert d.rule == "security_blocked"
        assert d.skip is False

    def test_ci_failure_skips_to_ci_wait(self):
        d = should_skip_re_review("ci_failure", False, scan_blocked=False)
        assert d.skip is True
        assert d.rule == "ci_failure"

    def test_merge_conflict_skips(self):
        d = should_skip_re_review("merge_conflict", False, scan_blocked=False)
        assert d.skip is True
        assert d.rule == "merge_conflict"

    def test_migration_guard_skips(self):
        d = should_skip_re_review("migration_guard", False, scan_blocked=False)
        assert d.skip is True
        assert d.rule == "migration_guard"

    def test_minor_only_skips(self):
        d = should_skip_re_review("code_review", True, scan_blocked=False)
        assert d.skip is True
        assert d.rule == "minor_only"

    def test_plain_code_review_spawns(self):
        d = should_skip_re_review("code_review", False, scan_blocked=False)
        assert d.skip is False
        assert d.rule == "none"

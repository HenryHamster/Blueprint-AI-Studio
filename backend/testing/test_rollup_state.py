"""Unit tests for rollup_state pure dict helpers and rollup PR body builder."""

from datetime import datetime, timezone
from types import SimpleNamespace

from app.rollup_management import _build_rollup_pr_body
from app.rollup_state import (
    _ROLLUP_TRANSIENT_CTX_KEYS,
    _clear_rollup_transient_state,
    _is_rollup_parent_awaiting_merge,
)


class TestClearRollupTransientState:
    def test_none_returns_empty_dict(self):
        assert _clear_rollup_transient_state(None) == {}

    def test_empty_returns_empty_dict(self):
        assert _clear_rollup_transient_state({}) == {}

    def test_strips_all_fifteen_transient_keys(self):
        ctx = dict.fromkeys(_ROLLUP_TRANSIENT_CTX_KEYS, "v")
        assert len(ctx) == 15
        # The escalation-latch is transient (must reset on terminal so future
        # deferral episodes can re-escalate).
        assert "_merge_escalation_attempted" in _ROLLUP_TRANSIENT_CTX_KEYS
        # The duplicate-create guard claim is a per-cycle transient too.
        assert "rollup_pr_creating_at" in _ROLLUP_TRANSIENT_CTX_KEYS
        # The merge-queue in-flight marker is per-cycle transient as well.
        assert "_merge_queue_update_at" in _ROLLUP_TRANSIENT_CTX_KEYS
        # The Alembic auto-reconcile in-flight stake: same shape as
        # ``rollup_pr_creating_at`` — a per-cycle claim that a peer sweep is
        # mid-push. A stale one left behind would block the next reconcile until
        # its TTL expired, so it MUST be scrubbed on every terminal transition.
        assert "_alembic_autoreconcile_inflight_at" in _ROLLUP_TRANSIENT_CTX_KEYS
        # The scan-report exactly-once marker is scoped to ONE rollup PR; a
        # re-entry on a NEW PR must post its own report.
        assert "_scan_report_posted_pr" in _ROLLUP_TRANSIENT_CTX_KEYS
        assert _clear_rollup_transient_state(ctx) == {}

    def test_preserves_non_transient_keys(self):
        ctx = {"rollup_pr_number": 42, "direction_id": "d-1", "plan_output": "x"}
        assert _clear_rollup_transient_state(ctx) == ctx

    def test_mixed_keys_strips_only_transient(self):
        ctx = {
            "_merge_deferred_count": 3,
            "_rollup_retries": 2,
            "_behind_update_retries": 1,
            "rollup_pr_number": 99,
            "pr_merged": True,
            "staging_branch": "rollup/abc",
        }
        result = _clear_rollup_transient_state(ctx)
        assert result == {
            "rollup_pr_number": 99,
            "pr_merged": True,
            "staging_branch": "rollup/abc",
        }

    def test_returns_new_dict_not_mutating_input(self):
        ctx = {"_merge_deferred_count": 5, "keep": True}
        original = dict(ctx)
        _clear_rollup_transient_state(ctx)
        assert ctx == original


class TestIsRollupParentAwaitingMerge:
    def test_none_returns_false(self):
        assert _is_rollup_parent_awaiting_merge(None) is False

    def test_empty_returns_false(self):
        assert _is_rollup_parent_awaiting_merge({}) is False

    def test_is_rollup_review_true_returns_true(self):
        assert _is_rollup_parent_awaiting_merge({"_is_rollup_review": True}) is True

    def test_rollup_pr_set_and_not_merged_returns_true(self):
        assert _is_rollup_parent_awaiting_merge({"rollup_pr_number": 42}) is True
        assert (
            _is_rollup_parent_awaiting_merge(
                {"rollup_pr_number": 42, "pr_merged": False}
            )
            is True
        )

    def test_rollup_pr_set_and_merged_returns_false(self):
        assert (
            _is_rollup_parent_awaiting_merge(
                {"rollup_pr_number": 42, "pr_merged": True}
            )
            is False
        )

    def test_neither_flag_set_returns_false(self):
        assert _is_rollup_parent_awaiting_merge({"other_key": "value"}) is False

    def test_is_rollup_review_true_even_when_pr_merged(self):
        # _is_rollup_review short-circuits before the pr_merged check
        assert (
            _is_rollup_parent_awaiting_merge(
                {"_is_rollup_review": True, "pr_merged": True}
            )
            is True
        )

    def test_child_review_with_rollup_pr_in_review_loop_returns_false(self):
        # Regression guard for incident 2026-05-07 (task 12b4eb3b):
        # A review task spawned for the rollup PR inherits rollup_pr_number into
        # its own context AND has review_loop.pr_number == rollup_pr_number.
        # Without this exclusion the fallback predicate falsely identifies it as
        # a rollup parent and parks it in EVALUATING after the CI-fix takeover.
        ctx = {
            "rollup_pr_number": 4177,
            "review_loop": {"pr_number": 4177, "iteration": 2},
        }
        assert _is_rollup_parent_awaiting_merge(ctx) is False

    def test_child_fix_with_rollup_pr_in_review_loop_returns_false(self):
        # Same exclusion applies to fix tasks spawned for the rollup PR
        ctx = {
            "rollup_pr_number": 4177,
            "review_loop": {"pr_number": 4177, "iteration": 3, "fix_reason": "ci_failure"},
        }
        assert _is_rollup_parent_awaiting_merge(ctx) is False

    def test_review_loop_with_different_pr_does_not_mask_parent(self):
        # The child-task exclusion only fires when review_loop.pr_number matches
        # rollup_pr_number — different PRs (shouldn't happen in practice but be
        # safe) leave the rollup-parent fallback intact
        ctx = {
            "rollup_pr_number": 4177,
            "review_loop": {"pr_number": 9999},
        }
        assert _is_rollup_parent_awaiting_merge(ctx) is True


class TestBuildRollupPrBody:
    def _make_child(
        self, task_type: str, spot: bool = True, seconds: float | None = None
    ):
        from datetime import timedelta

        prov = (
            datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
            if seconds is not None
            else None
        )
        stop = (
            (
                datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
                + timedelta(seconds=seconds)
            )
            if seconds is not None
            else None
        )
        return SimpleNamespace(
            payloads=[{"type": task_type}],
            vm_spot=spot,
            vm_provisioned_at=prov,
            vm_stopped_at=stop,
        )

    def test_minimal_body_has_required_sections(self):
        body = _build_rollup_pr_body(3, "rollup/abc")
        assert "Rollup of 3 subtasks from `rollup/abc`" in body
        assert "*Automated by Blueprint Pipeline*" in body

    def test_direction_line_included_when_provided(self):
        body = _build_rollup_pr_body(
            2, "rollup/xyz", direction_id="abcd1234", direction_title="My Direction"
        )
        assert "**Direction:** My Direction `abcd1234`" in body

    def test_direction_id_truncated_to_8_chars(self):
        body = _build_rollup_pr_body(1, "rollup/x", direction_id="abcdef1234567890")
        assert "`abcdef12`" in body

    def test_task_type_breakdown_from_children(self):
        children = [
            self._make_child("create"),
            self._make_child("create"),
            self._make_child("fix"),
        ]
        body = _build_rollup_pr_body(3, "rollup/x", children=children)
        assert "**Tasks:**" in body
        assert "2 create" in body
        assert "1 fix" in body

    def test_cost_line_present_when_timing_available(self):
        children = [self._make_child("create", spot=True, seconds=3600)]
        body = _build_rollup_pr_body(1, "rollup/x", children=children)
        assert "**Estimated cost:**" in body
        assert "$" in body

    def test_no_cost_line_when_no_timing(self):
        children = [self._make_child("create", seconds=None)]
        body = _build_rollup_pr_body(1, "rollup/x", children=children)
        assert "Estimated cost" not in body

    def test_rollup_note_included(self):
        body = _build_rollup_pr_body(2, "rollup/x", "\n\n*1 subtask(s) failed.*")
        assert "*1 subtask(s) failed.*" in body

    def test_footer_always_last_line(self):
        body = _build_rollup_pr_body(
            1, "rollup/x", direction_title="D", direction_id="aaa"
        )
        assert body.strip().endswith("*Automated by Blueprint Pipeline*")

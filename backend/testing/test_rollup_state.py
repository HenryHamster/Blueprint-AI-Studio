"""Unit tests for rollup_state pure dict helpers."""
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

    def test_strips_all_ten_transient_keys(self):
        ctx = {k: "v" for k in _ROLLUP_TRANSIENT_CTX_KEYS}
        assert len(ctx) == 10
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

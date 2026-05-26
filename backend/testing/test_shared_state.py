"""Unit tests for app.shared_state helpers."""

from types import SimpleNamespace

import pytest

from app.shared_state import (
    TASK_TYPE_BRANCH_PREFIX,
    derive_task_branch,
    resolve_task_branch,
)


def _task(
    *,
    task_id="00000000-0000-0000-0000-0000000000aa",
    payloads=None,
    context=None,
):
    """Minimal duck-typed task with the attributes resolve_task_branch reads."""
    if payloads is None:
        payloads = [{"type": "create"}]
    first_payload = payloads[0] if payloads else None
    return SimpleNamespace(
        id=task_id,
        payloads=payloads,
        first_payload=first_payload,
        context=context or {},
    )


class TestDeriveTaskBranch:
    def test_unknown_type_falls_back_to_task_prefix(self):
        # The fallback prefix protects against future task types not in the map.
        b = derive_task_branch("11111111-2222-3333-4444-555555555555", "novel-type")
        assert b == "blueprint/task-11111111"

    def test_refactor_uses_refactor_prefix(self):
        b = derive_task_branch("11111111-2222-3333-4444-555555555555", "refactor")
        assert b == "blueprint/refactor-11111111"

    def test_fix_uses_task_prefix(self):
        # Fix tasks reuse the parent branch in real runs, but if synthesized
        # they go under `task-` like create/qa per the agent's branch map.
        b = derive_task_branch("11111111-2222-3333-4444-555555555555", "fix")
        assert b == "blueprint/task-11111111"

    def test_prefix_map_covers_known_types(self):
        # Guard against accidental drift from agent/handlers -- these prefixes
        # must remain in sync with the agent's per-type branch derivation.
        for t in ("create", "qa", "fix", "refactor", "docs", "deps", "migrate"):
            assert t in TASK_TYPE_BRANCH_PREFIX


class TestResolveTaskBranch:
    def test_review_loop_branch_wins_over_payload_and_context(self):
        t = _task(
            payloads=[{"type": "fix", "branch": "blueprint/payload-branch"}],
            context={
                "branch": "blueprint/ctx-branch",
                "review_loop": {"branch": "blueprint/review-loop-branch"},
            },
        )
        assert resolve_task_branch(t) == "blueprint/review-loop-branch"

    def test_payload_branch_wins_over_context_when_review_loop_missing(self):
        t = _task(
            payloads=[{"type": "fix", "branch": "blueprint/payload-branch"}],
            context={"branch": "blueprint/ctx-branch"},
        )
        assert resolve_task_branch(t) == "blueprint/payload-branch"

    def test_context_branch_used_when_review_loop_and_payload_missing(self):
        t = _task(
            payloads=[{"type": "fix"}],
            context={"branch": "blueprint/ctx-branch"},
        )
        assert resolve_task_branch(t) == "blueprint/ctx-branch"

    def test_review_loop_empty_branch_falls_through_to_payload(self):
        # An empty string or None in review_loop.branch must not block the
        # fallback chain -- those are sentinel "unset" values.
        t = _task(
            payloads=[{"type": "fix", "branch": "blueprint/payload-branch"}],
            context={"review_loop": {"branch": None}},
        )
        assert resolve_task_branch(t) == "blueprint/payload-branch"
        t = _task(
            payloads=[{"type": "fix", "branch": "blueprint/payload-branch"}],
            context={"review_loop": {"branch": ""}},
        )
        assert resolve_task_branch(t) == "blueprint/payload-branch"

    def test_no_branch_anywhere_falls_back_to_derive(self):
        # The synthesized branch must use the task's payload type, not the
        # default "task" prefix -- refactor children push to refactor-*.
        t = _task(
            task_id="aabbccdd-1111-2222-3333-444444444444",
            payloads=[{"type": "refactor"}],
            context={},
        )
        assert resolve_task_branch(t) == "blueprint/refactor-aabbccdd"

    def test_none_context_is_safe(self):
        t = _task(payloads=[{"type": "create"}], context=None)
        # No context, no payload branch -> derived.
        expected = derive_task_branch(t.id, "create")
        assert resolve_task_branch(t) == expected

    def test_fix_task_with_parent_branch_in_review_loop_returns_parent_branch(self):
        # Regression -- fix tasks reuse the parent's PR branch. The branch
        # carried in review_loop.branch is the PR's HEAD ref the parent
        # agent pushed; synthesizing blueprint/task-{fix_id} would yield a
        # name no agent has ever pushed, breaking the next review's clone.
        t = _task(
            task_id="ffffffff-0000-0000-0000-000000000000",
            payloads=[{"type": "fix"}],
            context={"review_loop": {"branch": "blueprint/task-abc12345"}},
        )
        out = resolve_task_branch(t)
        assert out == "blueprint/task-abc12345"
        # Critically: not the fix task's own synthesized branch.
        assert out != "blueprint/task-ffffffff"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

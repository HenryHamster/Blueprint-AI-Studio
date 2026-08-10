"""
Unit tests for app/task_completion.py.

Covers:
- _parse_plan_output: JSON extraction from agent logs
- _extract_json_array: JSON array extraction by required keys
- _inject_file_overlap_deps: file-overlap dependency injection and cycle fallback
- _log_file_overlaps: overlapping file detection
- _compute_first_attempt_succeeded: attempt_count threshold and status check
- _ensure_branch: retry/backoff, token refresh, exists-verification, terminal failure
- _record_remediation_outcome / _record_remediation_if_terminal: remediation
  outcome propagation
- build_blueprint_pr_* helpers
"""

import json
import os
import uuid as _uuid_mod
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastapi import HTTPException

from app.task_completion import (
    _parse_plan_output,
    _extract_json_array,
    _extract_last_json_array,
    _inject_build_on_deps,
    _inject_file_overlap_deps,
    _log_file_overlaps,
    _compute_first_attempt_succeeded,
    _validate_plan_dependencies,
    _ensure_branch,
    _handle_plan_completion,
    _handle_design_doc_completion,
    _record_remediation_outcome,
    _record_remediation_if_terminal,
    _record_project_remediation_outcome,
    build_blueprint_pr_footer,
    build_blueprint_pr_merge_summary_comment,
    patch_pr_body_with_blueprint_meta,
    BLUEPRINT_PR_META_MARKER,
    BLUEPRINT_PR_MERGE_SUMMARY_MARKER,
)
from app.models import PoolVmState, StrategicCycle, Task, TaskStatus
from testing.factories import make_project, make_task

_USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


@pytest.mark.asyncio
async def test_phase2_spawn_exception_emits_negative_receipt_for_failed_cycle(
    db_session, mocker
):
    """A spawn exception must agree with the durable failed phase-two outcome.

    The active-task skip is covered by the deferred-path regression in
    ``test_strategic_completion.py``; this test keeps the exception contract in
    the task-completion suite and verifies the durable cycle alongside the
    post-commit event envelope.
    """
    project = make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        payloads=[{"type": "design_doc_update", "prompt": "update"}],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    cycle = StrategicCycle(
        id="cycle-phase2-spawn-failure",
        project_id=project.id,
        phase1_task_id=task.id,
    )
    task.strategic_cycle_id = cycle.id
    db_session.add_all([cycle, task])
    await db_session.commit()

    mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(side_effect=RuntimeError("spawn boom")),
    )
    mocker.patch("app.alert_dispatch._create_strategic_alert", new=AsyncMock())
    emit = AsyncMock()
    mocker.patch("app.strategic_events.emit_strategic_cycle_completed", new=emit)

    await _handle_design_doc_completion(task, "", db_session, project=project)

    await db_session.refresh(cycle)
    assert cycle.outcome == "failed_phase2_spawn"
    assert cycle.failure_reason == "spawn boom"
    emit.assert_awaited_once()
    receipt = emit.await_args.kwargs
    assert receipt["phase_state"] == "failed"
    assert receipt["completion"]["status"] == "failed"
    assert receipt["completion"]["failure_reason"] == "spawn boom"
    assert "FAILED to spawn" in receipt["completion"]["status_message"]


@pytest.mark.asyncio
async def test_phase2_spawn_recovers_poisoned_session_before_failure_writes(
    db_session, mocker
):
    """A failed spawn flush must not turn an already-completed Phase 1 FAILED."""
    project = make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        payloads=[{"type": "design_doc_update", "prompt": "update"}],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = task.id

    cycle = StrategicCycle(
        id="cycle-phase2-poisoned-session",
        project_id=project.id,
        phase1_task_id=task_id,
    )
    task.strategic_cycle_id = cycle.id
    db_session.add_all([cycle, task])
    await db_session.commit()

    async def _poison_session(_project, session, **_kwargs):
        # A duplicate primary-key flush puts a real SQLAlchemy AsyncSession in
        # PendingRollbackError state, matching the production CI-compliance
        # flush failure rather than merely raising a synthetic RuntimeError.
        session.expunge(task)
        duplicate = make_task(project_id=project.id, id=task_id)
        session.add(duplicate)
        await session.flush()

    async def _assert_clean_alert(_project_id, session, _message):
        # This query would raise PendingRollbackError if the handler reused the
        # poisoned transaction for its alert/marker recovery.
        assert await session.get(Task, task_id) is not None

    mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=_poison_session,
    )
    mocker.patch(
        "app.alert_dispatch._create_strategic_alert",
        new=_assert_clean_alert,
    )
    mocker.patch(
        "app.strategic_events.emit_strategic_cycle_completed",
        new=AsyncMock(),
    )

    await _handle_design_doc_completion(task, "", db_session, project=project)

    recovered_task = await db_session.get(Task, task_id)
    await db_session.refresh(project)
    await db_session.refresh(cycle)
    assert recovered_task is not None
    assert recovered_task.status == TaskStatus.COMPLETED
    assert project.vision["_pending_strategic_review"]["design_doc_task_id"] == str(
        task_id
    )
    assert cycle.outcome == "failed_phase2_spawn"


# ── _compute_first_attempt_succeeded ────────────────────────────────────────


class TestComputeFirstAttemptSucceeded:
    def test_attempt_zero_completed(self):
        assert _compute_first_attempt_succeeded(0, TaskStatus.COMPLETED) is True

    def test_attempt_one_is_not_first(self):
        """attempt_count=1 means one retry was consumed (UI Attempt 2)."""
        assert _compute_first_attempt_succeeded(1, TaskStatus.COMPLETED) is False

    def test_banked_attempt_is_not_first_after_counter_reset(self):
        """A manual fresh retry epoch cannot turn lifetime Attempt 2 into Attempt 1."""
        assert (
            _compute_first_attempt_succeeded(
                0,
                TaskStatus.COMPLETED,
                banked_attempt_count=1,
            )
            is False
        )

    def test_attempt_two_is_not_first(self):
        assert _compute_first_attempt_succeeded(2, TaskStatus.COMPLETED) is False

    def test_high_attempt_count(self):
        assert _compute_first_attempt_succeeded(10, TaskStatus.COMPLETED) is False

    def test_failed_task_not_first_success(self):
        assert _compute_first_attempt_succeeded(0, TaskStatus.FAILED) is False

    def test_failed_task_any_attempt(self):
        assert _compute_first_attempt_succeeded(1, TaskStatus.FAILED) is False

    def test_evaluating_status_not_success(self):
        assert _compute_first_attempt_succeeded(0, TaskStatus.EVALUATING) is False

    def test_queued_status_not_success(self):
        assert _compute_first_attempt_succeeded(0, TaskStatus.QUEUED) is False

    def test_completed_strategic_review_is_unmeasured_without_an_adjudicator(self):
        assert (
            _compute_first_attempt_succeeded(
                0,
                TaskStatus.COMPLETED,
                task_type="strategic_review",
            )
            is None
        )

    def test_retried_unadjudicated_strategic_review_stays_unmeasured(self):
        assert (
            _compute_first_attempt_succeeded(
                2,
                TaskStatus.COMPLETED,
                task_type="strategic_review",
            )
            is None
        )

    def test_banked_retried_unadjudicated_strategic_review_stays_unmeasured(self):
        """A reset retry counter must not turn unknown semantics into failure."""
        assert (
            _compute_first_attempt_succeeded(
                0,
                TaskStatus.COMPLETED,
                banked_attempt_count=1,
                task_type="strategic_review",
            )
            is None
        )

    def test_non_strategic_first_attempt_behavior_is_unchanged(self):
        assert (
            _compute_first_attempt_succeeded(
                0,
                TaskStatus.COMPLETED,
                task_type="create",
            )
            is True
        )


def test_pending_rollback_handler_error_is_transient_by_type():
    from sqlalchemy.exc import PendingRollbackError

    from app.task_lifecycle_completion_dispatch import (
        _is_transient_completion_handler_error,
    )

    exc = PendingRollbackError("rollback required")
    assert _is_transient_completion_handler_error(exc) is True


def test_logic_handler_error_is_not_transient():
    from app.task_lifecycle_completion_dispatch import (
        _is_transient_completion_handler_error,
    )

    assert _is_transient_completion_handler_error(RuntimeError("logic bug")) is False


# ── _parse_plan_output ───────────────────────────────────────────────────────


class TestParsePlanOutput:
    def _make_plan(self, subtasks):
        return json.dumps({"subtasks": subtasks})

    def test_well_formed_json(self):
        plan = self._make_plan([{"title": "Task A", "prompt": "Do A"}])
        result = _parse_plan_output(plan)
        assert result is not None
        assert result["subtasks"][0]["title"] == "Task A"

    def test_multiple_subtasks(self):
        plan = self._make_plan(
            [
                {"title": "Task A", "prompt": "Do A"},
                {"title": "Task B", "prompt": "Do B", "depends_on_indices": [0]},
            ]
        )
        result = _parse_plan_output(plan)
        assert result is not None
        assert len(result["subtasks"]) == 2
        assert result["subtasks"][1]["depends_on_indices"] == [0]

    def test_embedded_in_text(self):
        """JSON embedded in surrounding log text should still be extracted."""
        text = (
            "Some preamble\n"
            + self._make_plan([{"title": "A", "prompt": "p"}])
            + "\nTrailing text"
        )
        result = _parse_plan_output(text)
        assert result is not None
        assert result["subtasks"][0]["title"] == "A"

    def test_braces_in_string_values(self):
        """Braces inside JSON string values (e.g. code snippets) must not break extraction."""
        plan = json.dumps(
            {
                "subtasks": [
                    {
                        "title": "Fix",
                        "prompt": "Run this: func() { return {a: 1}; }",
                    }
                ]
            }
        )
        result = _parse_plan_output(plan)
        assert result is not None
        assert "func()" in result["subtasks"][0]["prompt"]

    def test_escaped_characters_in_strings(self):
        """Escaped backslashes and quotes in string values should be handled correctly."""
        plan = json.dumps(
            {
                "subtasks": [
                    {
                        "title": "Escape test",
                        "prompt": 'Use \\"double quotes\\" and backslash: \\\\',
                    }
                ]
            }
        )
        result = _parse_plan_output(plan)
        assert result is not None
        assert result["subtasks"][0]["title"] == "Escape test"

    def test_empty_subtasks_list(self):
        plan = json.dumps({"subtasks": []})
        result = _parse_plan_output(plan)
        assert result is not None
        assert result["subtasks"] == []

    def test_empty_input(self):
        result = _parse_plan_output("")
        assert result is None

    def test_no_subtasks_key(self):
        result = _parse_plan_output(json.dumps({"other": "data"}))
        assert result is None

    def test_malformed_truncated_json(self):
        """Truncated JSON (no closing brace) should return None."""
        partial = '{"subtasks": [{"title": "A"'
        result = _parse_plan_output(partial)
        assert result is None

    def test_just_text_no_json(self):
        result = _parse_plan_output("The agent ran but produced no structured output.")
        assert result is None

    def test_nested_code_block_with_braces(self):
        """Code blocks containing braces nested inside subtask prompts."""
        plan_dict = {
            "subtasks": [
                {
                    "title": "Add function",
                    "prompt": "Add this:\n```python\ndef foo():\n    return {'key': {'nested': True}}\n```",
                }
            ]
        }
        text = json.dumps(plan_dict)
        result = _parse_plan_output(text)
        assert result is not None
        assert "nested" in result["subtasks"][0]["prompt"]

    def test_first_valid_match_returned(self):
        """When multiple subtasks JSON objects appear, first one is returned."""
        plan1 = json.dumps({"subtasks": [{"title": "First"}]})
        plan2 = json.dumps({"subtasks": [{"title": "Second"}]})
        result = _parse_plan_output(plan1 + "\n" + plan2)
        assert result is not None
        assert result["subtasks"][0]["title"] == "First"

    def test_input_truncated_to_500k(self):
        """Function internally caps input to 500k characters."""
        # Build a plan that starts well after 500k chars — should not be found
        padding = "x" * 500_001
        plan = padding + json.dumps({"subtasks": [{"title": "Late"}]})
        result = _parse_plan_output(plan)
        assert result is None


# ── _extract_json_array ──────────────────────────────────────────────────────


class TestExtractJsonArray:
    def test_valid_array_with_required_key(self):
        text = json.dumps([{"severity": "high", "title": "Issue 1"}])
        result = _extract_json_array(text, "severity")
        assert result is not None
        parsed = json.loads(result)
        assert parsed[0]["severity"] == "high"

    def test_multiple_required_keys(self):
        text = json.dumps([{"severity": "high", "title": "T", "description": "D"}])
        result = _extract_json_array(text, "severity", "title")
        assert result is not None

    def test_missing_required_key_returns_none(self):
        text = json.dumps([{"title": "Issue 1"}])
        result = _extract_json_array(text, "severity")
        assert result is None

    def test_all_required_keys_must_be_present(self):
        """Returns None if even one required key is absent."""
        text = json.dumps([{"severity": "high"}])
        result = _extract_json_array(text, "severity", "missing_key")
        assert result is None

    def test_empty_input(self):
        result = _extract_json_array("", "severity")
        assert result is None

    def test_no_array_in_text(self):
        result = _extract_json_array('{"severity": "high"}', "severity")
        assert result is None

    def test_embedded_in_log_text(self):
        prefix = "Agent output:\n"
        array = json.dumps([{"severity": "critical", "fix_prompt": "Fix it"}])
        suffix = "\nDone."
        result = _extract_json_array(prefix + array + suffix, "severity")
        assert result is not None
        parsed = json.loads(result)
        assert parsed[0]["severity"] == "critical"

    def test_multiple_arrays_first_matching_returned(self):
        """When multiple arrays exist, the first one containing required keys is returned."""
        first = json.dumps([{"unrelated": "data"}])
        second = json.dumps([{"severity": "low"}])
        result = _extract_json_array(first + " " + second, "severity")
        assert result is not None
        parsed = json.loads(result)
        assert parsed[0]["severity"] == "low"

    def test_array_with_braces_in_string_values(self):
        """Braces inside string values must not confuse bracket counting."""
        items = [{"severity": "high", "description": "Call foo({a: 1})"}]
        text = json.dumps(items)
        result = _extract_json_array(text, "severity")
        assert result is not None
        parsed = json.loads(result)
        assert "foo(" in parsed[0]["description"]

    def test_multiple_items_in_array(self):
        items = [
            {"severity": "critical", "title": "A"},
            {"severity": "high", "title": "B"},
            {"severity": "low", "title": "C"},
        ]
        result = _extract_json_array(json.dumps(items), "severity")
        assert result is not None
        parsed = json.loads(result)
        assert len(parsed) == 3

    def test_boundary_empty_array(self):
        """An empty array [] doesn't contain the required key — returns None."""
        result = _extract_json_array("[]", "severity")
        assert result is None


# ── _extract_last_json_array ─────────────────────────────────────────────────


class TestExtractLastJsonArray:
    def test_single_array_returns_it(self):
        text = json.dumps([{"hash": "abc", "verdict": "pass"}])
        result = _extract_last_json_array(text, "hash", "verdict")
        assert result is not None
        parsed = json.loads(result)
        assert parsed[0]["hash"] == "abc"

    def test_two_arrays_returns_last(self):
        first = json.dumps([{"hash": "template", "verdict": "pass | fail"}])
        second = json.dumps([{"hash": "real_hash", "verdict": "fail"}])
        result = _extract_last_json_array(first + "\n\n" + second, "hash", "verdict")
        assert result is not None
        parsed = json.loads(result)
        assert parsed[0]["hash"] == "real_hash"

    def test_only_first_matches_required_keys(self):
        """When only the first array has the required keys, return it (only match)."""
        first = json.dumps([{"hash": "abc", "verdict": "pass"}])
        second = json.dumps([{"unrelated": "data"}])
        result = _extract_last_json_array(first + " " + second, "hash", "verdict")
        assert result is not None
        parsed = json.loads(result)
        assert parsed[0]["hash"] == "abc"

    def test_returns_none_when_required_key_absent(self):
        text = json.dumps([{"title": "no hash key here"}])
        result = _extract_last_json_array(text, "hash", "verdict")
        assert result is None

    def test_three_arrays_returns_third(self):
        a1 = json.dumps([{"hash": "h1", "verdict": "pass"}])
        a2 = json.dumps([{"hash": "h2", "verdict": "fail"}])
        a3 = json.dumps([{"hash": "h3", "verdict": "pass"}])
        result = _extract_last_json_array(a1 + " " + a2 + " " + a3, "hash", "verdict")
        assert result is not None
        parsed = json.loads(result)
        assert parsed[0]["hash"] == "h3"

    def test_empty_input_returns_none(self):
        assert _extract_last_json_array("", "hash") is None


# ── _inject_file_overlap_deps ────────────────────────────────────────────────


class TestInjectFileOverlapDeps:
    def test_no_shared_files_no_deps_added(self):
        subtasks = [
            {"title": "A", "related_files": ["a.py"]},
            {"title": "B", "related_files": ["b.py"]},
        ]
        _inject_file_overlap_deps(subtasks)
        assert "depends_on_indices" not in subtasks[0]
        assert "depends_on_indices" not in subtasks[1]

    def test_shared_file_adds_dependency(self):
        subtasks = [
            {"title": "A", "related_files": ["shared.py"]},
            {"title": "B", "related_files": ["shared.py"]},
        ]
        _inject_file_overlap_deps(subtasks)
        assert 0 in subtasks[1].get("depends_on_indices", [])

    def test_shared_file_chain(self):
        """Three subtasks sharing the same file get chained: 0→1→2."""
        subtasks = [
            {"title": "A", "related_files": ["shared.py"]},
            {"title": "B", "related_files": ["shared.py"]},
            {"title": "C", "related_files": ["shared.py"]},
        ]
        _inject_file_overlap_deps(subtasks)
        assert 0 in subtasks[1].get("depends_on_indices", [])
        assert 1 in subtasks[2].get("depends_on_indices", [])

    def test_preserves_existing_explicit_deps(self):
        """Injected deps are merged with (not replacing) explicit deps."""
        subtasks = [
            {"title": "A", "related_files": ["shared.py"]},
            {"title": "B", "related_files": ["shared.py"], "depends_on_indices": [99]},
        ]
        _inject_file_overlap_deps(subtasks)
        deps = subtasks[1].get("depends_on_indices", [])
        assert 0 in deps
        assert 99 in deps

    def test_no_duplicate_deps_added(self):
        """If the dependency already exists it should not be added twice."""
        subtasks = [
            {"title": "A", "related_files": ["shared.py"]},
            {"title": "B", "related_files": ["shared.py"], "depends_on_indices": [0]},
        ]
        _inject_file_overlap_deps(subtasks)
        assert subtasks[1]["depends_on_indices"].count(0) == 1

    def test_multiple_shared_files(self):
        """Multiple shared files between same pair of tasks add dep only once."""
        subtasks = [
            {"title": "A", "related_files": ["x.py", "y.py"]},
            {"title": "B", "related_files": ["x.py", "y.py"]},
        ]
        _inject_file_overlap_deps(subtasks)
        # Both x.py and y.py trigger dep injection, but dep 0 should appear at most twice
        deps = subtasks[1].get("depends_on_indices", [])
        assert 0 in deps

    def test_no_related_files_no_deps(self):
        subtasks = [
            {"title": "A"},
            {"title": "B"},
        ]
        _inject_file_overlap_deps(subtasks)
        assert "depends_on_indices" not in subtasks[0]
        assert "depends_on_indices" not in subtasks[1]

    def test_single_subtask_no_change(self):
        subtasks = [{"title": "A", "related_files": ["a.py"]}]
        _inject_file_overlap_deps(subtasks)
        assert "depends_on_indices" not in subtasks[0]

    def test_cycle_fallback_reverts_injection(self):
        """When injection creates a cycle, caller should detect and revert.

        _inject_file_overlap_deps itself adds the dep; cycle detection happens
        in the caller (_handle_plan_completion). Here we verify that the
        injection would create a cycle detectable by _validate_plan_dependencies,
        and that reverting to original deps makes the plan valid again.
        """
        # A explicitly depends on B (index 1), B and A share a file
        # Injection would add B → A, creating A→B→A cycle
        subtasks = [
            {"title": "A", "related_files": ["shared.py"], "depends_on_indices": [1]},
            {"title": "B", "related_files": ["shared.py"]},
        ]
        # Save original deps
        original_deps = {
            i: list(st.get("depends_on_indices", [])) for i, st in enumerate(subtasks)
        }
        _inject_file_overlap_deps(subtasks)
        # After injection, the plan should be cyclic
        assert _validate_plan_dependencies(subtasks) is False
        # Simulate the fallback: revert to original deps
        for i, st in enumerate(subtasks):
            if original_deps.get(i):
                st["depends_on_indices"] = original_deps[i]
            else:
                st.pop("depends_on_indices", None)
        # After revert, plan should be valid again
        assert _validate_plan_dependencies(subtasks) is True


# ── _inject_build_on_deps ────────────────────────────────────────────────────


class TestInjectBuildOnDeps:
    def test_consumer_referencing_new_module_is_ordered(self):
        """B builds a feature in a different file that imports A's new module."""
        subtasks = [
            {
                "title": "feat: add auth client module",
                "prompt": "Create the auth client.",
                "related_files": ["src/auth/client.py"],
            },
            {
                "title": "feat: wire login flow",
                "prompt": "Add a login flow that imports src/auth/client.py.",
                "related_files": ["src/routes/login.py"],
            },
        ]
        added = _inject_build_on_deps(subtasks)
        assert 0 in subtasks[1].get("depends_on_indices", [])
        assert (0, 1, "src/auth/client.py") in added

    def test_no_reference_stays_parallel(self):
        """Independent subtasks in different files get no build-on edge."""
        subtasks = [
            {
                "title": "A",
                "prompt": "Create src/a.py.",
                "related_files": ["src/a.py"],
            },
            {
                "title": "B",
                "prompt": "Create src/b.py, an unrelated helper.",
                "related_files": ["src/b.py"],
            },
        ]
        added = _inject_build_on_deps(subtasks)
        assert added == []
        assert "depends_on_indices" not in subtasks[1]

    def test_same_file_left_to_overlap_pass(self):
        """When the consumer also edits the file, no build-on edge is added.

        The same-file case is already serialized by _inject_file_overlap_deps;
        build-on injection must not double-handle it.
        """
        subtasks = [
            {
                "title": "A",
                "prompt": "Create src/shared.py.",
                "related_files": ["src/shared.py"],
            },
            {
                "title": "B",
                "prompt": "Extend src/shared.py with a helper.",
                "related_files": ["src/shared.py"],
            },
        ]
        added = _inject_build_on_deps(subtasks)
        assert added == []

    def test_no_backward_edge(self):
        """A reference from an EARLIER subtask to a later one adds no edge."""
        subtasks = [
            {
                "title": "consumer first",
                "prompt": "Use src/late.py once it exists.",
                "related_files": ["src/early.py"],
            },
            {
                "title": "producer second",
                "prompt": "Create the module.",
                "related_files": ["src/late.py"],
            },
        ]
        added = _inject_build_on_deps(subtasks)
        assert added == []
        assert "depends_on_indices" not in subtasks[0]

    def test_bounded_match_avoids_substring_false_positive(self):
        """Short path a.py must not match inside data.py in the prompt."""
        subtasks = [
            {
                "title": "A",
                "prompt": "Create a.py.",
                "related_files": ["a.py"],
            },
            {
                "title": "B",
                "prompt": "Create data.py, a fresh dataset loader.",
                "related_files": ["data.py"],
            },
        ]
        added = _inject_build_on_deps(subtasks)
        assert added == []

    def test_merges_with_existing_explicit_deps(self):
        subtasks = [
            {"title": "A", "prompt": "Create src/m.py.", "related_files": ["src/m.py"]},
            {
                "title": "B",
                "prompt": "Build on src/m.py.",
                "related_files": ["src/b.py"],
                "depends_on_indices": [99],
            },
        ]
        _inject_build_on_deps(subtasks)
        deps = subtasks[1].get("depends_on_indices", [])
        assert 0 in deps
        assert 99 in deps


# ── _log_file_overlaps ───────────────────────────────────────────────────────


class TestLogFileOverlaps:
    def test_no_shared_files(self):
        subtasks = [
            {"title": "A", "related_files": ["a.py"]},
            {"title": "B", "related_files": ["b.py"]},
        ]
        overlaps = _log_file_overlaps(subtasks)
        assert overlaps == []

    def test_shared_file_detected(self):
        subtasks = [
            {"title": "A", "related_files": ["shared.py"]},
            {"title": "B", "related_files": ["shared.py"]},
        ]
        overlaps = _log_file_overlaps(subtasks)
        assert len(overlaps) == 1
        i, j, files = overlaps[0]
        assert i == 0
        assert j == 1
        assert "shared.py" in files

    def test_multiple_shared_files(self):
        subtasks = [
            {"title": "A", "related_files": ["x.py", "y.py"]},
            {"title": "B", "related_files": ["x.py", "y.py"]},
        ]
        overlaps = _log_file_overlaps(subtasks)
        assert len(overlaps) == 1
        _, _, files = overlaps[0]
        assert "x.py" in files
        assert "y.py" in files

    def test_partial_overlap(self):
        subtasks = [
            {"title": "A", "related_files": ["shared.py", "only_a.py"]},
            {"title": "B", "related_files": ["shared.py", "only_b.py"]},
        ]
        overlaps = _log_file_overlaps(subtasks)
        assert len(overlaps) == 1
        _, _, files = overlaps[0]
        assert files == {"shared.py"}

    def test_no_related_files_key(self):
        """Subtasks without related_files key are treated as empty."""
        subtasks = [{"title": "A"}, {"title": "B", "related_files": ["b.py"]}]
        overlaps = _log_file_overlaps(subtasks)
        assert overlaps == []

    def test_empty_related_files(self):
        subtasks = [
            {"title": "A", "related_files": []},
            {"title": "B", "related_files": []},
        ]
        overlaps = _log_file_overlaps(subtasks)
        assert overlaps == []

    def test_three_way_overlap(self):
        """Three subtasks all sharing the same file produce three pairs."""
        subtasks = [
            {"title": "A", "related_files": ["shared.py"]},
            {"title": "B", "related_files": ["shared.py"]},
            {"title": "C", "related_files": ["shared.py"]},
        ]
        overlaps = _log_file_overlaps(subtasks)
        # Pairs: (0,1), (0,2), (1,2)
        assert len(overlaps) == 3

    def test_only_upper_triangle_pairs(self):
        """Each pair is reported once (j > i), not duplicated."""
        subtasks = [
            {"title": "A", "related_files": ["shared.py"]},
            {"title": "B", "related_files": ["shared.py"]},
        ]
        overlaps = _log_file_overlaps(subtasks)
        for i, j, _ in overlaps:
            assert j > i

    def test_uses_title_fallback(self):
        """Returns correct index-based tuple even when title is missing."""
        subtasks = [
            {"related_files": ["shared.py"]},
            {"related_files": ["shared.py"]},
        ]
        overlaps = _log_file_overlaps(subtasks)
        assert len(overlaps) == 1
        assert overlaps[0][0] == 0
        assert overlaps[0][1] == 1


# ── _ensure_branch ───────────────────────────────────────────────────────────


@pytest.fixture
def _patch_ensure_branch_deps(mocker):
    """Patch asyncio.sleep and failure-alert plumbing for _ensure_branch tests."""
    mocker.patch("app.task_completion.asyncio.sleep", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.routers.agent_status.record_task_result")


class TestEnsureBranch:
    @pytest.mark.asyncio
    async def test_success_first_attempt(
        self, db_session, mocker, _patch_ensure_branch_deps
    ):
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, payloads=[{"type": "plan"}])
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        get_token = mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        get_sha = mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            return_value="abc123",
        )
        create = mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            return_value=None,
        )

        # R4-W1 (audit C2): _ensure_branch returns Task | None.
        # Success returns the freshly-locked Task instance.
        relocked = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        assert relocked is not None
        assert isinstance(relocked, Task)
        # Only source-branch SHA lookup + create, no verify path
        assert get_sha.await_count == 1
        assert create.await_count == 1
        # First attempt: force_refresh must be False
        assert get_token.await_args.kwargs.get("force_refresh") is False
        assert relocked.status != TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_transient_verify_error_does_not_short_circuit(
        self, db_session, mocker, _patch_ensure_branch_deps
    ):
        """A 502 during verification is transient — keep retrying, eventually succeed."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, payloads=[{"type": "plan"}])
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        # First call (source SHA) OK, second call (verify) raises 502,
        # third call (source SHA on retry) OK.
        get_sha = mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            side_effect=[
                "abc123",
                HTTPException(status_code=502, detail="bad gateway"),
                "abc123",
            ],
        )
        # Create fails once (triggering verify), succeeds second time
        mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            side_effect=[RuntimeError("boom"), None],
        )

        relocked = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        # R4-W1 (audit C2): success returns the relocked Task instance.
        assert relocked is not None
        assert get_sha.await_count == 3
        assert relocked.status != TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_force_refresh_on_retry(
        self, db_session, mocker, _patch_ensure_branch_deps
    ):
        """On retry attempts (attempt > 0), token fetch uses force_refresh=True."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, payloads=[{"type": "plan"}])
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        get_token = mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        # First attempt fails outright at create; verify also 404s so we move on.
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            side_effect=[
                "sha1",  # source SHA attempt 1
                HTTPException(status_code=404, detail="not found"),  # verify
                "sha1",  # source SHA attempt 2
            ],
        )
        mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            side_effect=[RuntimeError("down"), None],
        )

        relocked = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        # R4-W1 (audit C2): success returns the relocked Task instance.
        assert relocked is not None
        # Attempt 0: force_refresh=False; attempt 1: force_refresh=True
        refresh_flags = [
            c.kwargs.get("force_refresh") for c in get_token.await_args_list
        ]
        assert refresh_flags == [False, True]

    @pytest.mark.asyncio
    async def test_branch_already_exists_short_circuits(
        self, db_session, mocker, _patch_ensure_branch_deps
    ):
        """If create_branch fails but verify shows the branch already at the
        source tip, we reuse the existing branch."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, payloads=[{"type": "plan"}])
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        get_sha = mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            # source SHA, then verify: the branch is at the SAME tip we wanted.
            side_effect=["sha1", "sha1"],
        )
        create = mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=422, detail="reference exists"),
        )
        delete = mocker.patch(
            "app.task_completion.delete_branch", new_callable=AsyncMock
        )

        relocked = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        # R4-W1 (audit C2): success returns the relocked Task instance.
        assert relocked is not None
        assert create.await_count == 1
        assert get_sha.await_count == 2
        assert relocked.status != TaskStatus.FAILED
        # An in-sync branch is adopted as-is; nothing is destroyed.
        assert delete.await_count == 0

    # ── stale-branch adoption (create_branch reuse) ──────────────────────────
    #
    # create_branch reports reuse of a pre-existing ref (422) instead of
    # returning a success-shaped result. "The branch exists" is NOT "the branch
    # is usable": a crashed prior run leaves a ref at a completely different
    # SHA, and adopting it silently bases the whole plan/research fan-out on the
    # wrong code.

    @pytest.mark.asyncio
    async def test_reused_branch_ahead_of_source_is_adopted_not_destroyed(
        self, db_session, mocker, _patch_ensure_branch_deps
    ):
        """A staging branch that already CONTAINS the source tip is ahead of it —
        typically because children already merged their PRs into it. Adopt it;
        deleting it would destroy landed child work."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, payloads=[{"type": "plan"}])
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            return_value="source-sha",
        )
        create = mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            return_value={
                "reused": True,
                "sha_matches": False,
                "existing_sha": "branch-ahead-sha",
                "requested_sha": "source-sha",
            },
        )
        ancestry = mocker.patch(
            "app.task_completion.compare_ref_status",
            new_callable=AsyncMock,
            return_value="ahead",  # existing tip already contains source-sha
        )
        delete = mocker.patch(
            "app.task_completion.delete_branch", new_callable=AsyncMock
        )

        relocked = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        assert relocked is not None
        assert relocked.status != TaskStatus.FAILED
        assert ancestry.await_count == 1
        assert delete.await_count == 0, (
            "a branch that already contains the source tip must NEVER be deleted — "
            "children may have merged into it"
        )
        assert create.await_count == 1  # no recreate

    @pytest.mark.asyncio
    async def test_reused_branch_strictly_behind_source_is_reset(
        self, db_session, mocker, _patch_ensure_branch_deps
    ):
        """A pre-existing branch that is strictly BEHIND the source tip has NO
        commits of its own — a stale leftover from a crashed run cut off an older
        base. Resetting it to the source tip is a fast-forward that destroys
        nothing, so delete + recreate is safe here (and ONLY here).

        NOTE the narrowness: "behind" is the only status that licenses a delete.
        This test used to assert the same behaviour for a DIVERGED branch, which
        was a latent data-loss bug — see
        ``test_diverged_branch_with_merged_children_is_adopted_never_deleted``."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, payloads=[{"type": "plan"}])
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            return_value="source-sha",
        )
        create = mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            side_effect=[
                # 1st: GitHub 422 → reused at a stale, divergent tip.
                {
                    "reused": True,
                    "sha_matches": False,
                    "existing_sha": "stale-sha",
                    "requested_sha": "source-sha",
                },
                # 2nd (after the delete): a clean create at the source tip.
                {"ref": "refs/heads/plan/xyz", "object": {"sha": "source-sha"}},
            ],
        )
        mocker.patch(
            "app.task_completion.compare_ref_status",
            new_callable=AsyncMock,
            return_value="behind",  # no unique commits — FF reset loses nothing
        )
        delete = mocker.patch(
            "app.task_completion.delete_branch", new_callable=AsyncMock
        )

        relocked = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        assert relocked is not None
        assert relocked.status != TaskStatus.FAILED
        assert delete.await_count == 1
        assert delete.await_args.args[3] == "plan/xyz"
        assert create.await_count == 2, "the branch must be recreated at the source tip"
        assert create.await_args.args[4] == "source-sha"

    @pytest.mark.asyncio
    async def test_indeterminate_ancestry_adopts_rather_than_deleting(
        self, db_session, mocker, _patch_ensure_branch_deps
    ):
        """An ancestry check that FAILS (None — GitHub API error) must never be
        able to destroy a ref: adopt the existing branch instead."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, payloads=[{"type": "plan"}])
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            return_value="source-sha",
        )
        mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            return_value={
                "reused": True,
                "sha_matches": False,
                "existing_sha": "other-sha",
                "requested_sha": "source-sha",
            },
        )
        mocker.patch(
            "app.task_completion.compare_ref_status",
            new_callable=AsyncMock,
            return_value=None,  # API error — indeterminate
        )
        delete = mocker.patch(
            "app.task_completion.delete_branch", new_callable=AsyncMock
        )

        relocked = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        assert relocked is not None
        assert relocked.status != TaskStatus.FAILED
        assert delete.await_count == 0

    @pytest.mark.asyncio
    async def test_diverged_branch_with_merged_children_is_adopted_never_deleted(
        self, db_session, mocker, _patch_ensure_branch_deps
    ):
        """A DIVERGED staging branch must be ADOPTED, never deleted. THE regression
        test for a data-loss bug.

        The real-world shape this reproduces:
          * ``plan_completion`` / ``research_completion`` inherit an existing staging
            branch (guarded on ``rollup_pr_number``), i.e. the HEAD BRANCH OF AN OPEN
            ROLLUP PR, with CHILD PRs ALREADY MERGED INTO IT;
          * the source branch (``parent_base``, normally automain) keeps advancing
            underneath it — dozens of commits over the life of a rollup.

        So the staging branch has unique commits (the merged children) AND the source
        has unique commits (automain's advance) => compare status ``"diverged"``.

        The trap: ``is_ancestor_of`` collapses BOTH ``"behind"`` and ``"diverged"``
        onto ``False``. Any implementation that deletes on a falsy ancestry answer
        will DELETE THE HEAD BRANCH OF AN OPEN PR — GitHub auto-closes the PR — and
        destroy every merged child commit. Only the RAW status can tell the two apart.

        A force-pushed source branch produces the same ``"diverged"`` status, and the
        same rule applies: never destroy the side holding unique commits.
        """
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, payloads=[{"type": "plan"}])
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            return_value="automain-advanced-sha",
        )
        create = mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            return_value={
                "reused": True,
                "sha_matches": False,
                # staging branch tip: carries the merged child commits
                "existing_sha": "staging-with-merged-children-sha",
                "requested_sha": "automain-advanced-sha",
            },
        )
        compare = mocker.patch(
            "app.task_completion.compare_ref_status",
            new_callable=AsyncMock,
            return_value="diverged",
        )
        delete = mocker.patch(
            "app.task_completion.delete_branch", new_callable=AsyncMock
        )

        relocked = await _ensure_branch(
            task, "plan/staging-xyz", "automain", "PLAN", db_session
        )

        # Assert the DESTRUCTIVE call first: it is the whole point of this test, and
        # it makes the failure message diagnostic if the guard ever regresses.
        assert delete.await_count == 0, (
            "DATA LOSS: a DIVERGED staging branch was deleted. It is the head branch "
            "of an open rollup PR and holds merged child commits; deleting it closes "
            "the PR and destroys that work. 'diverged' must ADOPT — only 'behind' "
            "(no unique commits) may be reset."
        )
        assert create.await_count == 1, "no delete => no recreate"
        assert compare.await_count == 1
        assert relocked is not None
        assert relocked.status != TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_unresolvable_source_tip_retries_rather_than_adopting(
        self, db_session, mocker, _patch_ensure_branch_deps
    ):
        """A source tip we could not resolve must NOT silently adopt the existing
        branch.

        If ``get_branch_sha`` yields nothing, we have no anchor to compare the
        pre-existing branch against — so we cannot know whether it is current or a
        stale leftover. Adopting on that unverified read re-opens the stale-tip bug
        this whole path exists to close. Retry instead; exhausting retries fails the
        task loudly. The branch is never created off a null anchor either.
        """
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, payloads=[{"type": "plan"}])
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            return_value=None,  # tip could not be resolved
        )
        create = mocker.patch(
            "app.task_completion.create_branch", new_callable=AsyncMock
        )
        delete = mocker.patch(
            "app.task_completion.delete_branch", new_callable=AsyncMock
        )

        result = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        assert result is None
        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED
        assert create.await_count == 0, "never create a branch off a null anchor"
        assert delete.await_count == 0

    @pytest.mark.asyncio
    async def test_stale_branch_that_cannot_be_reset_fails_the_task(
        self, db_session, mocker, _patch_ensure_branch_deps
    ):
        """If the branch STILL points at the stale tip after delete+recreate, we
        must not adopt it — retries exhaust and the task fails loudly rather than
        fanning out children off the wrong base."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, payloads=[{"type": "plan"}])
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            return_value="source-sha",
        )
        stale_ref = {
            "reused": True,
            "sha_matches": False,
            "existing_sha": "stale-sha",
            "requested_sha": "source-sha",
        }
        mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            return_value=stale_ref,  # the delete never takes effect
        )
        mocker.patch(
            "app.task_completion.compare_ref_status",
            new_callable=AsyncMock,
            return_value="behind",
        )
        mocker.patch("app.task_completion.delete_branch", new_callable=AsyncMock)

        result = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        assert result is None
        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_failure_after_all_retries_marks_task_failed(
        self, db_session, mocker, _patch_ensure_branch_deps
    ):
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, payloads=[{"type": "plan"}])
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            side_effect=RuntimeError("persistent failure"),
        )
        mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            side_effect=RuntimeError("should not reach"),
        )
        alert = mocker.patch(
            "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
        )

        result = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        # R4-W1 (audit C2): failure returns None and marks the task FAILED.
        assert result is None
        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED
        assert "Failed to create plan branch" in (task.latest_status_message or "")
        assert alert.await_count == 1

    @pytest.mark.asyncio
    async def test_missing_github_token_marks_task_failed(
        self, db_session, mocker, _patch_ensure_branch_deps
    ):
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, payloads=[{"type": "plan"}])
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            side_effect=RuntimeError("no installation token"),
        )
        get_sha = mocker.patch(
            "app.task_completion.get_branch_sha", new_callable=AsyncMock
        )

        result = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        # R4-W1 (audit C2): failure returns None and marks the task FAILED.
        assert result is None
        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED
        # Token fetch failed before any GitHub call — verify was never reached.
        assert get_sha.await_count == 0


# ── _record_remediation_outcome ──────────────────────────────────────────────


class TestRecordRemediationOutcome:
    @pytest.mark.asyncio
    async def test_skips_non_remediation_tasks(self, db_session):
        """Tasks without diagnosis_task_id/direction_id should be ignored."""
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, context={})
        db_session.add(task)
        await db_session.commit()

        updated = await _record_remediation_outcome(
            task, merged=True, session=db_session
        )
        assert updated is False
        assert "remediation_attempts" not in project.directions[0]

    @pytest.mark.asyncio
    async def test_records_merge_outcome(self, db_session):
        """A merged remediation task stamps direction with attempts + fingerprint."""
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={
                "diagnosis_task_id": "diag-123",
                "direction_id": "dir-1",
                "diagnosis_fingerprint": "abc123def456",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        updated = await _record_remediation_outcome(
            task, merged=True, session=db_session
        )
        await db_session.commit()
        await db_session.refresh(project)

        assert updated is True
        d = project.directions[0]
        assert d["remediation_attempts"] == 1
        assert d["last_remediation_result"] == "merged"
        assert d["last_remediation_diagnosis_task_id"] == "diag-123"
        assert d["last_remediation_fingerprint"] == "abc123def456"
        assert d["last_remediation_task_id"] == str(task.id)
        assert "last_remediation_at" in d

    @pytest.mark.asyncio
    async def test_increments_attempts_on_repeat_failure(self, db_session):
        """Repeated failed remediations accumulate attempts and flip the result."""
        project = make_project(
            directions=[
                {
                    "id": "dir-1",
                    "status": "active",
                    "remediation_attempts": 2,
                    "last_remediation_result": "merged",
                }
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={"diagnosis_task_id": "diag-9", "direction_id": "dir-1"},
        )
        db_session.add(task)
        await db_session.commit()

        updated = await _record_remediation_outcome(
            task, merged=False, session=db_session
        )
        await db_session.commit()
        await db_session.refresh(project)

        assert updated is True
        d = project.directions[0]
        assert d["remediation_attempts"] == 3
        assert d["last_remediation_result"] == "failed"

    @pytest.mark.asyncio
    async def test_no_match_when_direction_missing(self, db_session):
        """Retired/unknown direction_id returns False and does not mutate the
        project's directions, but stamps an orphan outcome on the task so the
        remediation result is not silently discarded."""
        project = make_project(directions=[{"id": "dir-other"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={
                "diagnosis_task_id": "diag-1",
                "direction_id": "dir-missing",
                "diagnosis_fingerprint": "fp-x",
            },
        )
        db_session.add(task)
        await db_session.commit()

        updated = await _record_remediation_outcome(
            task, merged=True, session=db_session
        )
        assert updated is False
        assert "remediation_attempts" not in project.directions[0]
        orphan = (task.context or {}).get("orphan_remediation_outcome")
        assert orphan is not None
        assert orphan["result"] == "merged"
        assert orphan["missing_direction_id"] == "dir-missing"
        assert orphan["fingerprint"] == "fp-x"

    @pytest.mark.asyncio
    async def test_increments_when_fingerprint_matches(self, db_session):
        """Same fingerprint as prior remediation increments attempts."""
        project = make_project(
            directions=[
                {
                    "id": "dir-1",
                    "status": "active",
                    "remediation_attempts": 2,
                    "last_remediation_fingerprint": "fp-same",
                }
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={
                "diagnosis_task_id": "diag-1",
                "direction_id": "dir-1",
                "diagnosis_fingerprint": "fp-same",
            },
        )
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_outcome(task, merged=False, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        assert project.directions[0]["remediation_attempts"] == 3

    @pytest.mark.asyncio
    async def test_resets_when_fingerprint_differs(self, db_session):
        """Different fingerprint means a new root cause — counter resets to 1."""
        project = make_project(
            directions=[
                {
                    "id": "dir-1",
                    "status": "active",
                    "remediation_attempts": 2,
                    "last_remediation_fingerprint": "fp-old",
                }
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={
                "diagnosis_task_id": "diag-2",
                "direction_id": "dir-1",
                "diagnosis_fingerprint": "fp-new",
            },
        )
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_outcome(task, merged=True, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert d["remediation_attempts"] == 1
        assert d["last_remediation_fingerprint"] == "fp-new"

    @pytest.mark.asyncio
    async def test_increments_when_no_fingerprint_available(self, db_session):
        """Missing fingerprint on context falls back to conservative increment."""
        project = make_project(
            directions=[
                {
                    "id": "dir-1",
                    "status": "active",
                    "remediation_attempts": 1,
                    "last_remediation_fingerprint": "fp-prior",
                }
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={"diagnosis_task_id": "diag-3", "direction_id": "dir-1"},
        )
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_outcome(task, merged=False, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert d["remediation_attempts"] == 2
        assert d["last_remediation_fingerprint"] == "fp-prior"

    @pytest.mark.asyncio
    async def test_failed_outcome_stamps_failure_class_and_summary(self, db_session):
        """A failed remediation records the failure class and truncated error summary."""
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        long_msg = "x" * 500
        task = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            latest_status_message=long_msg,
            context={
                "diagnosis_task_id": "diag-1",
                "direction_id": "dir-1",
                "failure_category": "CODE_ERROR",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_outcome(task, merged=False, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert d["last_remediation_result"] == "failed"
        assert d["last_remediation_failure_class"] == "CODE_ERROR"
        assert d["last_remediation_error_summary"] == long_msg[:200]
        assert len(d["last_remediation_error_summary"]) == 200

    @pytest.mark.asyncio
    async def test_merge_clears_prior_failure_details(self, db_session):
        """A merged remediation must clear stale failure class/summary from prior failures."""
        project = make_project(
            directions=[
                {
                    "id": "dir-1",
                    "status": "active",
                    "remediation_attempts": 1,
                    "last_remediation_result": "failed",
                    "last_remediation_failure_class": "CODE_ERROR",
                    "last_remediation_error_summary": "previous error output",
                }
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={"diagnosis_task_id": "diag-1", "direction_id": "dir-1"},
        )
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_outcome(task, merged=True, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert d["last_remediation_result"] == "merged"
        assert "last_remediation_failure_class" not in d
        assert "last_remediation_error_summary" not in d

    @pytest.mark.asyncio
    async def test_appends_remediation_history_entry(self, db_session):
        """Each remediation outcome appends an entry to remediation_history."""
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            latest_status_message="ImportError: boom",
            context={
                "diagnosis_task_id": "diag-1",
                "direction_id": "dir-1",
                "diagnosis_fingerprint": "fp-hist",
                "failure_category": "CODE_ERROR",
            },
            payloads=[
                {
                    "type": "create",
                    "prompt": "apply guard clause",
                    "base_branch": "main",
                }
            ],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_outcome(task, merged=False, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        history = project.directions[0]["remediation_history"]
        assert isinstance(history, list) and len(history) == 1
        entry = history[0]
        assert entry["task_id"] == str(task.id)
        assert entry["result"] == "failed"
        assert entry["failure_class"] == "CODE_ERROR"
        assert entry["error_summary"] == "ImportError: boom"
        assert entry["prompt_snippet"] == "apply guard clause"
        assert entry["fingerprint"] == "fp-hist"
        assert "timestamp" in entry

    @pytest.mark.asyncio
    async def test_history_capped_at_max_entries(self, db_session):
        """Appending beyond the cap drops the oldest entry (FIFO)."""
        from app.task_completion import _REMEDIATION_HISTORY_MAX

        existing = [
            {
                "task_id": f"prev-{i}",
                "result": "failed",
                "timestamp": f"2020-01-0{i + 1}T00:00:00",
            }
            for i in range(_REMEDIATION_HISTORY_MAX)
        ]
        project = make_project(
            directions=[
                {
                    "id": "dir-1",
                    "status": "active",
                    "remediation_history": list(existing),
                }
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={"diagnosis_task_id": "diag-1", "direction_id": "dir-1"},
            payloads=[
                {"type": "create", "prompt": "newest fix", "base_branch": "main"}
            ],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_outcome(task, merged=True, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        history = project.directions[0]["remediation_history"]
        assert len(history) == _REMEDIATION_HISTORY_MAX
        # Oldest (prev-0) evicted; newest appended at the end
        assert history[0]["task_id"] == "prev-1"
        assert history[-1]["task_id"] == str(task.id)
        assert history[-1]["result"] == "merged"

    @pytest.mark.asyncio
    async def test_merged_history_entry_omits_failure_fields(self, db_session):
        """Merged entries should not carry failure_class / error_summary keys."""
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={"diagnosis_task_id": "diag-1", "direction_id": "dir-1"},
            payloads=[{"type": "create", "prompt": "the fix", "base_branch": "main"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_outcome(task, merged=True, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        entry = project.directions[0]["remediation_history"][-1]
        assert entry["result"] == "merged"
        assert "failure_class" not in entry
        assert "error_summary" not in entry

    @pytest.mark.asyncio
    async def test_history_entry_includes_ci_failure_type(self, db_session):
        """ci_failure_type from task context propagates into remediation_history."""
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={
                "diagnosis_task_id": "diag-1",
                "direction_id": "dir-1",
                "ci_failure_type": "test",
            },
            payloads=[{"type": "create", "prompt": "the fix", "base_branch": "main"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_outcome(task, merged=True, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        entry = project.directions[0]["remediation_history"][-1]
        assert entry["ci_failure_type"] == "test"

    @pytest.mark.asyncio
    async def test_dedup_skips_second_call_same_task_id(self, db_session):
        """Calling _record_remediation_outcome twice for the same task does not
        inflate remediation_attempts or duplicate remediation_history."""
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={
                "diagnosis_task_id": "diag-1",
                "direction_id": "dir-1",
                "diagnosis_fingerprint": "fp-dedup",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        # First call records normally.
        first = await _record_remediation_outcome(task, merged=True, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)
        assert first is True
        assert project.directions[0]["remediation_attempts"] == 1
        assert len(project.directions[0]["remediation_history"]) == 1

        # Second call with same task must not increment attempts or add a history entry.
        second = await _record_remediation_outcome(task, merged=True, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)
        assert second is True
        d = project.directions[0]
        assert d["remediation_attempts"] == 1, "attempt count must not increment on dedup"
        assert len(d["remediation_history"]) == 1, "history must not grow on dedup"

    @pytest.mark.asyncio
    async def test_remediation_outcome_recorded_once_per_task(self, db_session):
        """Gate test: each unique task_id is recorded exactly once regardless of
        how many times the function is called, while distinct tasks each land."""
        project = make_project(directions=[{"id": "dir-2", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task_a = make_task(
            project_id=project.id,
            context={
                "diagnosis_task_id": "diag-a",
                "direction_id": "dir-2",
                "diagnosis_fingerprint": "fp-a",
            },
        )
        task_b = make_task(
            project_id=project.id,
            context={
                "diagnosis_task_id": "diag-b",
                "direction_id": "dir-2",
                "diagnosis_fingerprint": "fp-a",
            },
        )
        db_session.add(task_a)
        db_session.add(task_b)
        await db_session.commit()
        await db_session.refresh(task_a)
        await db_session.refresh(task_b)

        # Record task_a twice — second call must be a no-op.
        await _record_remediation_outcome(task_a, merged=False, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)
        await _record_remediation_outcome(task_a, merged=False, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert d["remediation_attempts"] == 1
        assert len(d["remediation_history"]) == 1
        assert d["remediation_history"][0]["task_id"] == str(task_a.id)

        # Record task_b — it is a different task so it must land as a second entry.
        await _record_remediation_outcome(task_b, merged=True, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert d["remediation_attempts"] == 2
        assert len(d["remediation_history"]) == 2
        assert d["remediation_history"][1]["task_id"] == str(task_b.id)


class TestRecordProjectRemediationOutcome:
    """Dedup behaviour for _record_project_remediation_outcome."""

    @pytest.mark.asyncio
    async def test_dedup_skips_second_call_same_task_id(self, db_session):
        """Calling _record_project_remediation_outcome twice for the same task
        does not increment state['attempts'] or duplicate state['history']."""
        project = make_project(directions=[])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        # First call records normally.
        first = await _record_project_remediation_outcome(
            task, merged=True, session=db_session
        )
        await db_session.commit()
        await db_session.refresh(project)
        assert first is True
        state = project.remediation_tracking
        assert state["attempts"] == 1
        assert len(state["history"]) == 1

        # Second call with same task must be a no-op.
        second = await _record_project_remediation_outcome(
            task, merged=True, session=db_session
        )
        await db_session.commit()
        await db_session.refresh(project)
        assert second is True
        state = project.remediation_tracking
        assert state["attempts"] == 1, "attempt count must not increment on dedup"
        assert len(state["history"]) == 1, "history must not grow on dedup"


class TestRemediationSuccessPropagation:
    """Propagation of merged remediation context to sibling directions via fingerprint match."""

    def _paused_sibling(self, direction_id: str, fingerprint: str, **overrides):
        from app.time_utils import _utcnow as _now

        sibling = {
            "id": direction_id,
            "status": "paused",
            "auto_paused": True,
            "error_fingerprint": fingerprint,
            "paused_at": _now().isoformat(),
        }
        sibling.update(overrides)
        return sibling

    @pytest.mark.asyncio
    async def test_injects_note_into_sibling_with_matching_fingerprint(
        self, db_session
    ):
        project = make_project(
            directions=[
                {"id": "dir-1", "status": "active"},
                self._paused_sibling("dir-2", "fp-shared"),
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            payloads=[{"prompt": "Fix the missing import in shared_utils."}],
            context={
                "diagnosis_task_id": "diag-1",
                "direction_id": "dir-1",
                "diagnosis_fingerprint": "fp-shared",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_outcome(task, merged=True, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        sibling = next(d for d in project.directions if d["id"] == "dir-2")
        assert sibling["status"] == "active"
        assert not sibling.get("auto_paused")
        assert sibling["last_remediation_task_id"] == str(task.id)
        notes = sibling.get("notes") or []
        assert len(notes) == 1
        assert notes[0]["source"] == "sibling_remediation_success"
        # Target direction still stamped normally.
        target = next(d for d in project.directions if d["id"] == "dir-1")
        assert target["last_remediation_result"] == "merged"

    @pytest.mark.asyncio
    async def test_resumed_paused_sibling_gets_grace_marker(self, db_session):
        """A paused sibling resumed by remediation propagation gets the
        auto_recovered_at grace marker so its frozen research_history cannot
        immediately re-pause or zombie-retire it before a new cycle runs."""
        project = make_project(
            directions=[
                {"id": "dir-1", "status": "active"},
                self._paused_sibling("dir-2", "fp-shared"),
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            payloads=[{"prompt": "Fix the shared import error."}],
            context={
                "diagnosis_task_id": "diag-1",
                "direction_id": "dir-1",
                "diagnosis_fingerprint": "fp-shared",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_outcome(task, merged=True, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        sibling = next(d for d in project.directions if d["id"] == "dir-2")
        assert sibling["status"] == "active"
        assert sibling.get("auto_recovered_at") is not None, (
            "resuming a paused sibling must stamp the grace marker"
        )

    @pytest.mark.asyncio
    async def test_noop_when_no_siblings_share_fingerprint(self, db_session):
        project = make_project(
            directions=[
                {"id": "dir-1", "status": "active"},
                self._paused_sibling("dir-2", "fp-other"),
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={
                "diagnosis_task_id": "diag-1",
                "direction_id": "dir-1",
                "diagnosis_fingerprint": "fp-shared",
            },
        )
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_outcome(task, merged=True, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        sibling = next(d for d in project.directions if d["id"] == "dir-2")
        assert not sibling.get("notes")

    @pytest.mark.asyncio
    async def test_note_content_includes_remediation_context(self, db_session):
        project = make_project(
            directions=[
                {"id": "dir-1", "status": "active"},
                self._paused_sibling("dir-2", "fp-shared"),
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            payloads=[
                {
                    "prompt": "Pin the pydantic version in requirements.txt to fix startup."
                }
            ],
            context={
                "diagnosis_task_id": "diag-1",
                "direction_id": "dir-1",
                "diagnosis_fingerprint": "fp-shared",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_outcome(task, merged=True, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        sibling = next(d for d in project.directions if d["id"] == "dir-2")
        note = (sibling.get("notes") or [])[0]
        assert note["source"] == "sibling_remediation_success"
        assert note["added_by_task"] == str(task.id)
        text = note["text"]
        assert "dir-1" in text
        assert str(task.id) in text
        assert "pydantic" in text

    @pytest.mark.asyncio
    async def test_does_not_propagate_success_on_failed_remediation(self, db_session):
        """Failed remediation should not emit a success note (failure note is a separate source)."""
        project = make_project(
            directions=[
                {"id": "dir-1", "status": "active"},
                self._paused_sibling("dir-2", "fp-shared"),
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            latest_status_message="build failed",
            payloads=[{"prompt": "bad fix"}],
            context={
                "diagnosis_task_id": "diag-1",
                "direction_id": "dir-1",
                "diagnosis_fingerprint": "fp-shared",
                "failure_category": "CODE_ERROR",
            },
        )
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_outcome(task, merged=False, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        sibling = next(d for d in project.directions if d["id"] == "dir-2")
        notes = sibling.get("notes") or []
        assert all(n["source"] != "sibling_remediation_success" for n in notes)

    @pytest.mark.asyncio
    async def test_unpauses_sibling_and_stamps_prior_art(self, db_session):
        """Merged remediation should clear pause state and stamp prior-art fields on the sibling."""
        sibling_seed = self._paused_sibling(
            "dir-2",
            "fp-shared",
            paused_reason="recurring CODE_ERROR",
            needs_manual_intervention=True,
            escalation_notification_pending=True,
        )
        project = make_project(
            directions=[
                {"id": "dir-1", "status": "active"},
                sibling_seed,
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            payloads=[{"prompt": "Pin pydantic in requirements.txt."}],
            context={
                "diagnosis_task_id": "diag-1",
                "direction_id": "dir-1",
                "diagnosis_fingerprint": "fp-shared",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_outcome(task, merged=True, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        sibling = next(d for d in project.directions if d["id"] == "dir-2")
        assert sibling["status"] == "active"
        assert "auto_paused" not in sibling
        assert "paused_at" not in sibling
        assert "paused_reason" not in sibling
        assert "needs_manual_intervention" not in sibling
        assert "escalation_notification_pending" not in sibling
        assert sibling["last_remediation_task_id"] == str(task.id)
        assert sibling["last_remediation_diagnosis_task_id"] == "diag-1"
        assert sibling["last_remediation_fingerprint"] == "fp-shared"
        assert sibling["last_remediation_result"] == "merged"
        assert sibling["last_remediation_at"]
        assert sibling["cross_direction_remediation_source"] == "dir-1"

    @pytest.mark.asyncio
    async def test_does_not_unpause_sibling_with_different_fingerprint(
        self, db_session
    ):
        """Exact-fingerprint-match guard: siblings with a different fingerprint stay paused."""
        project = make_project(
            directions=[
                {"id": "dir-1", "status": "active"},
                self._paused_sibling("dir-2", "fp-other"),
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            payloads=[{"prompt": "Fix something else."}],
            context={
                "diagnosis_task_id": "diag-1",
                "direction_id": "dir-1",
                "diagnosis_fingerprint": "fp-shared",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_outcome(task, merged=True, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        sibling = next(d for d in project.directions if d["id"] == "dir-2")
        assert sibling["status"] == "paused"
        assert sibling["auto_paused"] is True
        assert "cross_direction_remediation_source" not in sibling
        assert "last_remediation_task_id" not in sibling


class TestRemediationFailurePropagation:
    """Propagation of failed remediation context to sibling directions via fingerprint match."""

    def _paused_sibling(self, direction_id: str, fingerprint: str, **overrides):
        from app.time_utils import _utcnow as _now

        sibling = {
            "id": direction_id,
            "status": "paused",
            "auto_paused": True,
            "error_fingerprint": fingerprint,
            "paused_at": _now().isoformat(),
        }
        sibling.update(overrides)
        return sibling

    @pytest.mark.asyncio
    async def test_injects_failure_note_into_sibling_with_matching_fingerprint(
        self, db_session
    ):
        project = make_project(
            directions=[
                {"id": "dir-1", "status": "active"},
                self._paused_sibling("dir-2", "fp-shared"),
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            latest_status_message="ImportError: missing module xyz",
            payloads=[{"prompt": "Add xyz to requirements.txt to fix startup."}],
            context={
                "diagnosis_task_id": "diag-1",
                "direction_id": "dir-1",
                "diagnosis_fingerprint": "fp-shared",
                "failure_category": "CODE_ERROR",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_outcome(task, merged=False, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        sibling = next(d for d in project.directions if d["id"] == "dir-2")
        notes = sibling.get("notes") or []
        assert len(notes) == 1
        note = notes[0]
        assert note["source"] == "sibling_remediation_failure"
        assert note["added_by_task"] == str(task.id)
        text = note["text"]
        assert "dir-1" in text
        assert "xyz" in text
        assert "requirements.txt" in text

    @pytest.mark.asyncio
    async def test_failure_noop_when_no_siblings_share_fingerprint(self, db_session):
        project = make_project(
            directions=[
                {"id": "dir-1", "status": "active"},
                self._paused_sibling("dir-2", "fp-other"),
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            latest_status_message="boom",
            context={
                "diagnosis_task_id": "diag-1",
                "direction_id": "dir-1",
                "diagnosis_fingerprint": "fp-shared",
            },
        )
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_outcome(task, merged=False, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        sibling = next(d for d in project.directions if d["id"] == "dir-2")
        assert not sibling.get("notes")

    @pytest.mark.asyncio
    async def test_failure_note_respects_max_length(self, db_session):
        from app.strategic_constants import STRATEGIC_RATIONALE_MAX

        project = make_project(
            directions=[
                {"id": "dir-1", "status": "active"},
                self._paused_sibling("dir-2", "fp-shared"),
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            latest_status_message="x" * 500,
            payloads=[{"prompt": "y" * 500}],
            context={
                "diagnosis_task_id": "diag-1",
                "direction_id": "dir-1",
                "diagnosis_fingerprint": "fp-shared",
            },
        )
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_outcome(task, merged=False, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        sibling = next(d for d in project.directions if d["id"] == "dir-2")
        note = (sibling.get("notes") or [])[0]
        assert len(note["text"]) <= STRATEGIC_RATIONALE_MAX


# ── _record_remediation_if_terminal ─────────────────────────────────────────


class TestRecordRemediationIfTerminal:
    """Wrapper that filters by terminal status + diagnosis context, then delegates."""

    def _diag_ctx(self, **overrides):
        ctx = {"diagnosis_task_id": "diag-1", "direction_id": "dir-1"}
        ctx.update(overrides)
        return ctx

    @pytest.mark.asyncio
    async def test_non_terminal_status_is_noop(self, db_session):
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, context=self._diag_ctx())
        db_session.add(task)
        await db_session.commit()

        for status in (
            TaskStatus.IN_PROGRESS,
            TaskStatus.QUEUED,
            TaskStatus.EVALUATING,
            TaskStatus.PROVISIONING,
        ):
            await _record_remediation_if_terminal(task, status, db_session)
        await db_session.refresh(project)
        assert "remediation_attempts" not in project.directions[0]

    @pytest.mark.asyncio
    async def test_skips_when_diagnosis_task_id_missing(self, db_session):
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, context={"direction_id": "dir-1"})
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_if_terminal(task, TaskStatus.COMPLETED, db_session)
        await db_session.refresh(project)
        assert "remediation_attempts" not in project.directions[0]

    @pytest.mark.asyncio
    async def test_skips_when_direction_id_missing(self, db_session):
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, context={"diagnosis_task_id": "diag-1"})
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_if_terminal(task, TaskStatus.COMPLETED, db_session)
        await db_session.refresh(project)
        assert "remediation_attempts" not in project.directions[0]

    @pytest.mark.asyncio
    async def test_auto_remediation_failure_records_project_outcome(self, db_session):
        """Failing CI auto-remediation (no direction_id) records a project-level
        outcome via _record_project_remediation_outcome — the CI follow-up
        spawn itself lives in task_lifecycle.py and is exercised separately."""
        project = make_project(directions=[])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={
                "auto_remediation": True,
                "ci_failure_branch": "main",
                "_previous_error_fingerprint": "fp-ci-1",
            },
            latest_status_message="boom",
        )
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        state = project.remediation_tracking or {}
        assert state.get("attempts") == 1
        assert state.get("last_result") == "failed"
        assert state.get("last_fingerprint") == "fp-ci-1"

    @pytest.mark.asyncio
    async def test_auto_remediation_completed_records_project_outcome(self, db_session):
        """COMPLETED auto-remediation (non-PR-pipeline type) records a merged
        outcome at the project level when no direction linkage exists."""
        project = make_project(directions=[])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={
                "auto_remediation": True,
                "ci_failure_branch": "main",
                "_previous_error_fingerprint": "fp-ci-2",
            },
        )
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_if_terminal(task, TaskStatus.COMPLETED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        state = project.remediation_tracking or {}
        assert state.get("attempts") == 1
        assert state.get("last_result") == "merged"

    @pytest.mark.asyncio
    async def test_completed_records_merged_true(self, db_session):
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, context=self._diag_ctx())
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_if_terminal(task, TaskStatus.COMPLETED, db_session)
        await db_session.commit()
        await db_session.refresh(project)
        d = project.directions[0]
        assert d["remediation_attempts"] == 1
        assert d["last_remediation_result"] == "merged"

    @pytest.mark.asyncio
    async def test_failed_records_merged_false(self, db_session):
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, context=self._diag_ctx())
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)
        d = project.directions[0]
        assert d["remediation_attempts"] == 1
        assert d["last_remediation_result"] == "failed"

    @pytest.mark.asyncio
    async def test_cancelled_records_merged_false(self, db_session):
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, context=self._diag_ctx())
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_if_terminal(task, TaskStatus.CANCELLED, db_session)
        await db_session.commit()
        await db_session.refresh(project)
        d = project.directions[0]
        assert d["remediation_attempts"] == 1
        assert d["last_remediation_result"] == "failed"

    @pytest.mark.asyncio
    async def test_swallows_inner_exception(self, db_session, mocker):
        """Errors from _record_remediation_outcome must not propagate."""
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, context=self._diag_ctx())
        db_session.add(task)
        await db_session.commit()

        boom = mocker.patch(
            "app.task_completion._record_remediation_outcome",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db blew up"),
        )
        await _record_remediation_if_terminal(task, TaskStatus.COMPLETED, db_session)
        assert boom.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "task_type", ["create", "refactor", "docs", "qa", "deps", "migrate", "fix"]
    )
    async def test_completed_pr_creating_type_does_not_record(
        self, db_session, task_type
    ):
        """Tasks that produce a PR must not record 'merged' on their own COMPLETED —
        the review pipeline drives the real outcome."""
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            payloads=[{"type": task_type, "prompt": "x"}],
            context=self._diag_ctx(),
        )
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_if_terminal(task, TaskStatus.COMPLETED, db_session)
        await db_session.refresh(project)
        assert "remediation_attempts" not in project.directions[0]

    @pytest.mark.asyncio
    async def test_completed_review_type_does_not_record(self, db_session):
        """Review COMPLETED reflects the review verdict, not a merge — outcome
        recording is deferred until the actual PR merge success path fires."""
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            payloads=[{"type": "review", "pr_number": 1, "branch": "b"}],
            context=self._diag_ctx(),
        )
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_if_terminal(task, TaskStatus.COMPLETED, db_session)
        await db_session.commit()
        await db_session.refresh(project)
        d = project.directions[0]
        assert "last_remediation_result" not in d
        assert not d.get("remediation_attempts")

    @pytest.mark.asyncio
    async def test_failed_pr_creating_type_still_records(self, db_session):
        """FAILED/CANCELLED still record immediately — no PR to wait on."""
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "x"}],
            context=self._diag_ctx(),
        )
        db_session.add(task)
        await db_session.commit()

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)
        d = project.directions[0]
        assert d["remediation_attempts"] == 1
        assert d["last_remediation_result"] == "failed"


# ── follow-up investigation on remediation failure ──────────────────────────


# ── unlinked (project-level) remediation sibling propagation ───────────────


class TestUnlinkedRemediationSiblingPropagation:
    """When a CI auto-remediation task has no direction_id but carries a
    fingerprint, its outcome should still propagate to sibling directions
    sharing that fingerprint — un-pause on merge, 'do not repeat' note on fail.
    """

    def _paused_sibling(self, direction_id: str, fingerprint: str, **overrides):
        from app.time_utils import _utcnow as _now

        sibling = {
            "id": direction_id,
            "status": "paused",
            "auto_paused": True,
            "error_fingerprint": fingerprint,
            "paused_at": _now().isoformat(),
        }
        sibling.update(overrides)
        return sibling

    @pytest.mark.asyncio
    async def test_merged_unlinked_remediation_unpauses_sibling(self, db_session):
        project = make_project(
            directions=[
                self._paused_sibling("dir-2", "fp-shared", paused_reason="CODE_ERROR"),
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            payloads=[
                {"type": "create", "prompt": "Pin pydantic", "base_branch": "main"}
            ],
            context={
                "auto_remediation": True,
                "_previous_error_fingerprint": "fp-shared",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        recorded = await _record_project_remediation_outcome(
            task, merged=True, session=db_session
        )
        await db_session.commit()
        await db_session.refresh(project)
        assert recorded

        sibling = next(d for d in project.directions if d["id"] == "dir-2")
        assert sibling["status"] == "active"
        assert "auto_paused" not in sibling
        assert sibling["last_remediation_task_id"] == str(task.id)
        assert sibling["last_remediation_result"] == "merged"
        assert sibling["last_remediation_fingerprint"] == "fp-shared"
        # No source direction exists for unlinked remediation.
        assert "cross_direction_remediation_source" not in sibling
        note = (sibling.get("notes") or [])[0]
        assert note["source"] == "sibling_remediation_success"
        assert "Unlinked CI remediation merged" in note["text"]
        # Project-level tracking also recorded.
        assert project.remediation_tracking["last_result"] == "merged"

    @pytest.mark.asyncio
    async def test_failed_unlinked_remediation_notes_sibling(self, db_session):
        project = make_project(
            directions=[
                self._paused_sibling("dir-2", "fp-shared"),
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            latest_status_message="ImportError: module xyz",
            payloads=[{"type": "create", "prompt": "Bad fix", "base_branch": "main"}],
            context={
                "auto_remediation": True,
                "error_fingerprint": "fp-shared",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        sibling = next(d for d in project.directions if d["id"] == "dir-2")
        # Sibling remains paused but gets a warning note.
        assert sibling["status"] == "paused"
        note = (sibling.get("notes") or [])[0]
        assert note["source"] == "sibling_remediation_failure"
        assert "Unlinked CI remediation FAILED" in note["text"]
        assert "Do not repeat" in note["text"]

    @pytest.mark.asyncio
    async def test_unlinked_remediation_without_fingerprint_does_not_propagate(
        self, db_session
    ):
        project = make_project(
            directions=[
                self._paused_sibling("dir-2", "fp-shared"),
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "no fp", "base_branch": "main"}],
            context={"auto_remediation": True},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_project_remediation_outcome(task, merged=True, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        sibling = next(d for d in project.directions if d["id"] == "dir-2")
        assert sibling["status"] == "paused"
        assert not sibling.get("notes")

    @pytest.mark.asyncio
    async def test_unlinked_remediation_with_mismatched_fingerprint_leaves_sibling(
        self, db_session
    ):
        project = make_project(
            directions=[
                self._paused_sibling("dir-2", "fp-other"),
            ]
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "diff fp", "base_branch": "main"}],
            context={
                "auto_remediation": True,
                "_previous_error_fingerprint": "fp-shared",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_project_remediation_outcome(task, merged=True, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        sibling = next(d for d in project.directions if d["id"] == "dir-2")
        assert sibling["status"] == "paused"
        assert "last_remediation_task_id" not in sibling


# ── _release_pool_member_if_terminal ────────────────────────────────────────


class TestReleasePoolMemberIfTerminal:
    """A terminal task served by a warm-pool member returns it to IDLE
    (COMPLETED) or retires it (FAILED/CANCELLED), instead of stranding it
    LEASED against the pool budget until the orphan-lease reaper reclaims it."""

    async def _lease_member_to_task(self, db_session, *, vm_id="vm-pool-rel"):
        """Register an IDLE member, lease it to a fresh task, return (task, member)."""
        from app.warm_pool import lease_idle_pool_vm, register_pool_vm

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, vm_instance_id=vm_id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        await register_pool_vm(
            db_session,
            vm_instance_id=vm_id,
            project_id=project.id,
            account_id="acct-1",
            provider="anthropic",
            state=PoolVmState.IDLE,
        )
        await db_session.commit()
        member = await lease_idle_pool_vm(
            db_session,
            project_id=project.id,
            account_id="acct-1",
            provider="anthropic",
            leased_task_id=task.id,
        )
        await db_session.commit()
        assert member is not None and member.state == PoolVmState.LEASED
        return task, member

    async def _with_pool_enabled(self, enabled=True):
        import app.shared_state as shared_state

        prev = shared_state.runtime_config.get("warm_pool_enabled", False)
        shared_state.runtime_config["warm_pool_enabled"] = enabled
        return shared_state, prev

    @pytest.mark.asyncio
    async def test_completed_returns_member_to_idle(self, db_session):
        from app.task_completion import _release_pool_member_if_terminal
        from app.warm_pool import get_pool_vm

        shared_state, prev = await self._with_pool_enabled()
        try:
            task, _ = await self._lease_member_to_task(db_session, vm_id="vm-done")
            outcome = await _release_pool_member_if_terminal(
                task, TaskStatus.COMPLETED, db_session
            )
            await db_session.commit()
            assert outcome == "idle"
            member = await get_pool_vm(db_session, "vm-done")
            assert member.state == PoolVmState.IDLE
            assert member.leased_task_id is None
            assert member.lease_expires_at is None
            # Idle banking preserved: a fresh idle window started on return.
            assert member.idle_since is not None
        finally:
            shared_state.runtime_config["warm_pool_enabled"] = prev

    @pytest.mark.asyncio
    async def test_failed_retires_member(self, db_session):
        from app.task_completion import _release_pool_member_if_terminal
        from app.warm_pool import get_pool_vm

        shared_state, prev = await self._with_pool_enabled()
        try:
            task, _ = await self._lease_member_to_task(db_session, vm_id="vm-fail")
            outcome = await _release_pool_member_if_terminal(
                task, TaskStatus.FAILED, db_session
            )
            await db_session.commit()
            assert outcome == "retiring"
            member = await get_pool_vm(db_session, "vm-fail")
            assert member.state == PoolVmState.RETIRING
            assert member.leased_task_id is None
        finally:
            shared_state.runtime_config["warm_pool_enabled"] = prev

    @pytest.mark.asyncio
    async def test_noop_when_pool_disabled(self, db_session):
        from app.task_completion import _release_pool_member_if_terminal
        from app.warm_pool import get_pool_vm

        shared_state, prev = await self._with_pool_enabled(enabled=True)
        try:
            task, _ = await self._lease_member_to_task(db_session, vm_id="vm-off")
            shared_state.runtime_config["warm_pool_enabled"] = False
            outcome = await _release_pool_member_if_terminal(
                task, TaskStatus.COMPLETED, db_session
            )
            await db_session.commit()
            assert outcome is None
            member = await get_pool_vm(db_session, "vm-off")
            # Left untouched — still LEASED.
            assert member.state == PoolVmState.LEASED
        finally:
            shared_state.runtime_config["warm_pool_enabled"] = prev

    @pytest.mark.asyncio
    async def test_noop_when_member_leased_to_other_task(self, db_session):
        """A vm_instance_id reused across tasks must not let one task's terminal
        state reclaim a member leased to a different task."""
        from app.task_completion import _release_pool_member_if_terminal
        from app.warm_pool import get_pool_vm

        shared_state, prev = await self._with_pool_enabled()
        try:
            task, _ = await self._lease_member_to_task(db_session, vm_id="vm-other")
            # A different task points at the same vm_instance_id.
            interloper = make_task(
                project_id=task.project_id, vm_instance_id="vm-other"
            )
            db_session.add(interloper)
            await db_session.commit()
            await db_session.refresh(interloper)
            outcome = await _release_pool_member_if_terminal(
                interloper, TaskStatus.COMPLETED, db_session
            )
            await db_session.commit()
            assert outcome is None
            member = await get_pool_vm(db_session, "vm-other")
            assert member.state == PoolVmState.LEASED
            assert member.leased_task_id == task.id
        finally:
            shared_state.runtime_config["warm_pool_enabled"] = prev

    @pytest.mark.asyncio
    async def test_non_terminal_status_is_noop(self, db_session):
        from app.task_completion import _release_pool_member_if_terminal

        shared_state, prev = await self._with_pool_enabled()
        try:
            task, _ = await self._lease_member_to_task(db_session, vm_id="vm-inprog")
            outcome = await _release_pool_member_if_terminal(
                task, TaskStatus.IN_PROGRESS, db_session
            )
            assert outcome is None
        finally:
            shared_state.runtime_config["warm_pool_enabled"] = prev

    @pytest.mark.asyncio
    async def test_wrapper_releases_member_for_non_remediation_task(self, db_session):
        """_record_remediation_if_terminal reconciles the pool member even when the
        task has no diagnosis/remediation linkage (the common pool-served case)."""
        from app.warm_pool import get_pool_vm

        shared_state, prev = await self._with_pool_enabled()
        try:
            task, _ = await self._lease_member_to_task(db_session, vm_id="vm-wrap")
            # No diagnosis_task_id / direction_id / auto_remediation — the
            # remediation body returns early, but the pool member must still
            # be reconciled.
            await _record_remediation_if_terminal(
                task, TaskStatus.COMPLETED, db_session
            )
            await db_session.commit()
            member = await get_pool_vm(db_session, "vm-wrap")
            assert member.state == PoolVmState.IDLE
        finally:
            shared_state.runtime_config["warm_pool_enabled"] = prev


# ── _handle_plan_completion — scope-aware subtask effort ──────────────────────


class TestPlanSubtaskScopeAwareEffort:
    """Plan subtasks inherit effort from parent, but with a scope-aware floor."""

    def _plan_logs(self, subtasks):
        return json.dumps({"subtasks": subtasks})

    async def _setup_plan_task(self, db_session, mocker, parent_effort="medium"):
        mocker.patch(
            "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
        )
        mocker.patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            payloads=[
                {
                    "type": "plan",
                    "prompt": "plan it",
                    "base_branch": "main",
                    "effort": parent_effort,
                }
            ],
            status=TaskStatus.IN_PROGRESS,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        # R4-W1 (audit C2): _ensure_branch now returns Task | None on
        # success/failure (was bool). Patch to return the task instance
        # to mimic the success path. The function is imported lazily
        # inside _handle_plan_completion, so patch the source module.
        mocker.patch(
            "app.task_completion._ensure_branch",
            new_callable=AsyncMock,
            return_value=task,
        )
        return task

    async def _child_tasks(self, db_session, parent_task):
        from sqlmodel import select as _select
        from app.models import Task as _Task

        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == parent_task.id)
        )
        return result.scalars().all()

    @pytest.mark.asyncio
    async def test_large_scope_subtask_raises_effort_above_parent(
        self, db_session, mocker
    ):
        """Large-scope subtask under medium-effort parent is floored up to 'high'."""
        task = await self._setup_plan_task(db_session, mocker, parent_effort="medium")
        logs = self._plan_logs(
            [
                {
                    "title": "Sweeping refactor",
                    "prompt": "Do big thing",
                    "scope": "large",
                },
            ]
        )
        await _handle_plan_completion(task, logs, db_session)
        children = await self._child_tasks(db_session, task)
        assert len(children) == 1
        assert children[0].payloads[0]["effort"] == "high"

    @pytest.mark.asyncio
    async def test_small_scope_subtask_keeps_parent_effort_when_higher(
        self, db_session, mocker
    ):
        """Small-scope subtask under medium-effort parent keeps 'medium' (parent floor wins)."""
        task = await self._setup_plan_task(db_session, mocker, parent_effort="medium")
        logs = self._plan_logs(
            [
                {"title": "Small tweak", "prompt": "Narrow fix", "scope": "small"},
            ]
        )
        await _handle_plan_completion(task, logs, db_session)
        children = await self._child_tasks(db_session, task)
        assert len(children) == 1
        assert children[0].payloads[0]["effort"] == "medium"

    @pytest.mark.asyncio
    async def test_explicit_subtask_effort_max_beats_scope_floor(
        self, db_session, mocker
    ):
        """Subtask-specified effort wins when it ranks higher than the scope floor."""
        task = await self._setup_plan_task(db_session, mocker, parent_effort="medium")
        logs = self._plan_logs(
            [
                {
                    "title": "Critical tiny change",
                    "prompt": "p",
                    "scope": "small",
                    "effort": "max",
                },
            ]
        )
        await _handle_plan_completion(task, logs, db_session)
        children = await self._child_tasks(db_session, task)
        assert len(children) == 1
        assert children[0].payloads[0]["effort"] == "max"

    @pytest.mark.asyncio
    async def test_plan_output_stored_as_dict_not_list(self, db_session, mocker):
        """plan_output for plan tasks must be a dict, not a list.

        query_findings_index (research_completion.py) skips non-list plan_output,
        so a dict here guarantees plan tasks never pollute the Findings Journal
        research feed.
        """
        task = await self._setup_plan_task(db_session, mocker)
        logs = self._plan_logs([{"title": "T1", "prompt": "p1"}])
        await _handle_plan_completion(task, logs, db_session)
        await db_session.refresh(task)
        assert isinstance(task.plan_output, dict), (
            "plan_output must be a dict so query_findings_index excludes it from "
            "the research findings feed"
        )
        assert not isinstance(task.plan_output, list)
        assert "subtasks" in task.plan_output


class TestPlanPrerequisiteResolution:
    """Plan spawn applies the same prerequisite contract as blocked resume."""

    async def _setup_plan(self, db_session, mocker):
        mocker.patch(
            "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
        )
        mocker.patch(
            "app.plan_completion.broadcast_to_project", new_callable=AsyncMock
        )
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            title="Parent plan",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "plan", "prompt": "plan", "base_branch": "main"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        mocker.patch(
            "app.task_completion._ensure_branch", new_callable=AsyncMock, return_value=task
        )
        return project, task

    async def _children(self, db_session, parent):
        from sqlmodel import select

        return (
            (
                await db_session.execute(select(Task).where(Task.parent_task_id == parent.id))
            )
            .scalars()
            .all()
        )

    @pytest.mark.asyncio
    async def test_shipped_completed_prerequisite_spawns_child_runnable(
        self, db_session, mocker
    ):
        project, plan = await self._setup_plan(db_session, mocker)
        foundation = make_task(
            project_id=project.id,
            title="Shipped foundation",
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
        )
        db_session.add(foundation)
        await db_session.commit()
        logs = json.dumps(
            {"subtasks": [{"title": "Dependent", "prompt": "work", "prerequisite": {"kind": "task", "ref": str(foundation.id)}}]}
        )

        await _handle_plan_completion(plan, logs, db_session)

        child = (await self._children(db_session, plan))[0]
        assert child.status == TaskStatus.QUEUED
        assert child.context.get("unresolved_prerequisite") is None
        assert child.context["_spawn_prereq_resolved"] == [
            {"kind": "task", "ref": str(foundation.id)}
        ]

    @pytest.mark.asyncio
    async def test_ghost_completed_prerequisite_with_unmerged_rollup_blocks_child(
        self, db_session, mocker
    ):
        """A child merged only to an unmerged completed rollup is not shipped.

        The dependent must remain BLOCKED rather than run against the integration
        base, which does not contain the staging-only foundation.
        """
        project, plan = await self._setup_plan(db_session, mocker)
        rollup = make_task(
            project_id=project.id,
            title="Completed but unmerged rollup",
            status=TaskStatus.COMPLETED,
        )
        db_session.add(rollup)
        await db_session.commit()
        foundation = make_task(
            project_id=project.id,
            parent_task_id=rollup.id,
            title="Staging-only foundation",
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
        )
        db_session.add(foundation)
        await db_session.commit()
        logs = json.dumps(
            {
                "subtasks": [
                    {
                        "title": "Dependent",
                        "prompt": "work",
                        "prerequisite": {
                            "kind": "task",
                            "ref": str(foundation.id),
                        },
                    }
                ]
            }
        )

        await _handle_plan_completion(plan, logs, db_session)

        child = (await self._children(db_session, plan))[0]
        assert child.status == TaskStatus.BLOCKED
        assert child.context.get("_spawn_prereq_resolved") is None
        assert child.context["unresolved_prerequisite"] == [
            {"kind": "task", "ref": str(foundation.id)}
        ]

    @pytest.mark.asyncio
    async def test_parent_plan_cannot_become_child_prerequisite(self, db_session, mocker):
        _, plan = await self._setup_plan(db_session, mocker)
        logs = json.dumps(
            {"subtasks": [{"title": "Dependent", "prompt": "work", "prerequisite": {"kind": "task", "ref": str(plan.id)}}]}
        )

        await _handle_plan_completion(plan, logs, db_session)

        child = (await self._children(db_session, plan))[0]
        assert child.status == TaskStatus.BLOCKED
        assert str(plan.id) not in child.depends_on

    @pytest.mark.asyncio
    async def test_blocked_child_broadcasts_committed_status(self, db_session, mocker):
        _, plan = await self._setup_plan(db_session, mocker)
        broadcast = mocker.patch(
            "app.plan_completion.broadcast_to_project", new_callable=AsyncMock
        )
        missing_foundation_id = _uuid_mod.uuid4()
        logs = json.dumps(
            {"subtasks": [{"title": "Blocked", "prompt": "work", "prerequisite": {"kind": "task", "ref": str(missing_foundation_id)}}]}
        )

        await _handle_plan_completion(plan, logs, db_session)

        child = (await self._children(db_session, plan))[0]
        assert child.status == TaskStatus.BLOCKED
        assert broadcast.await_args.args[2]["status"] == TaskStatus.BLOCKED


# ── _record_regression_signal_if_matching ──────────────────────────────────


# ── build_blueprint_pr_footer ────────────────────────────────────────────────


class TestBuildBlueprintPrFooter:
    """Pure-function tests for the PR-body metadata footer builder."""

    def _task_with_timing(self, seconds: float = 3600.0, spot: bool = False):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return make_task(
            vm_provisioned_at=now - timedelta(seconds=seconds),
            vm_stopped_at=now,
            vm_spot=spot,
            context={},
        )

    def test_cost_included_when_timing_present(self):
        task = self._task_with_timing(3600.0, spot=False)
        footer = build_blueprint_pr_footer(task)
        assert "$" in footer

    def test_cost_on_demand_rate(self):
        task = self._task_with_timing(3600.0, spot=False)
        footer = build_blueprint_pr_footer(task)
        # 3600s * (0.067/3600) = $0.067
        assert "$0.07" in footer

    def test_cost_spot_rate(self):
        task = self._task_with_timing(3600.0, spot=True)
        footer = build_blueprint_pr_footer(task)
        # 3600s * (0.020/3600) = $0.020
        assert "$0.02" in footer

    def test_cost_omitted_when_no_start_time(self):
        task = make_task(vm_provisioned_at=None, vm_stopped_at=None, context={})
        footer = build_blueprint_pr_footer(task, iteration_count=3)
        assert "$" not in footer
        assert "3 iterations" in footer

    def test_iterations_gt_one_included(self):
        task = make_task(context={})
        footer = build_blueprint_pr_footer(task, iteration_count=3)
        assert "3 iterations" in footer

    def test_iterations_one_omitted(self):
        task = make_task(context={})
        footer = build_blueprint_pr_footer(task, iteration_count=1)
        assert "iteration" not in footer

    def test_iterations_zero_omitted(self):
        task = make_task(context={})
        footer = build_blueprint_pr_footer(task, iteration_count=0)
        assert "iteration" not in footer

    def test_direction_id_included(self):
        task = make_task(context={"direction_id": "d-abc123"})
        footer = build_blueprint_pr_footer(task)
        assert "d-abc123" in footer
        assert "direction:" in footer

    def test_direction_title_and_id_both_shown(self):
        task = make_task(context={"direction_id": "d-abc123"})
        footer = build_blueprint_pr_footer(task, direction_title="PR clarity metric")
        assert "PR clarity metric" in footer
        assert "d-abc123" in footer

    def test_long_direction_title_truncated(self):
        task = make_task(context={"direction_id": "d-xyz"})
        long_title = "A" * 80
        footer = build_blueprint_pr_footer(task, direction_title=long_title)
        assert "A" * 60 in footer
        assert "A" * 61 not in footer

    def test_no_direction_omits_direction_field(self):
        task = make_task(context={})
        footer = build_blueprint_pr_footer(task, iteration_count=2)
        assert "direction:" not in footer

    def test_returns_empty_when_no_data(self):
        task = make_task(context={})
        assert build_blueprint_pr_footer(task) == ""

    def test_uses_blueprint_meta_marker(self):
        task = make_task(context={"direction_id": "d-abc"})
        footer = build_blueprint_pr_footer(task)
        assert footer.startswith(BLUEPRINT_PR_META_MARKER)

    def test_all_fields_combined(self):
        task = self._task_with_timing(1800.0, spot=False)
        task.context = {"direction_id": "d-xyz"}
        footer = build_blueprint_pr_footer(
            task, iteration_count=2, direction_title="My Direction"
        )
        assert "$" in footer
        assert "2 iterations" in footer
        assert "My Direction" in footer
        assert "d-xyz" in footer
        assert BLUEPRINT_PR_META_MARKER in footer

    def test_falls_back_to_context_review_metadata(self):
        task = make_task(
            context={
                "review_loop": {"iteration": 3},
                "direction_id": "d-ctx",
                "direction_title": "From context",
            }
        )
        footer = build_blueprint_pr_footer(task)
        assert "3 iterations" in footer
        assert "From context" in footer
        assert "d-ctx" in footer


class TestBuildBlueprintPrMergeSummaryComment:
    def test_formats_merge_summary_from_context_and_payload(self):
        task = make_task(
            payloads=[{"type": "review", "scope": "backend"}],
            context={
                "review_loop": {"iteration": 2},
                "direction_title": "Reduce flaky tests",
                "agent_handler": "review-fix",
                "llm_model": "gpt-5",
            },
        )
        with patch("app.max_plan_pool.get_task_llm_provider", return_value="openai"):
            comment = build_blueprint_pr_merge_summary_comment(task)

        assert comment.startswith(BLUEPRINT_PR_MERGE_SUMMARY_MARKER)
        assert "### Blueprint Merge Summary" in comment
        assert "Review-fix iterations: **2**" in comment
        assert "- Task: `review` (`backend`)" in comment
        assert "- Agent: `review-fix`" in comment
        assert "- Provider/Model: `openai / gpt-5`" in comment
        assert "- Direction: Reduce flaky tests" in comment

    def test_defaults_when_metadata_missing(self):
        task = make_task(payloads=[{}], context={})
        with patch("app.max_plan_pool.get_task_llm_provider", return_value=""):
            comment = build_blueprint_pr_merge_summary_comment(task)

        assert "Review-fix iterations: **n/a**" in comment
        assert "- Task: `create` (`unknown`)" in comment
        assert "- Provider/Model: `unknown`" in comment
        assert "- Direction: n/a" in comment


# ── patch_pr_body_with_blueprint_meta ────────────────────────────────────────


class TestPatchPrBodyWithBlueprintMeta:
    """Tests for the async GitHub PR body patcher."""

    @pytest.mark.asyncio
    async def test_appends_footer_when_no_existing_marker(self):
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = {"body": "Existing PR body."}
        patched_body = None

        async def mock_get(*args, **kwargs):
            return mock_response

        async def mock_patch(*args, **kwargs):
            nonlocal patched_body
            patched_body = kwargs.get("json", {}).get("body")
            return MagicMock(is_success=True)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.patch = mock_patch
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client
            await patch_pr_body_with_blueprint_meta(
                "tok", "owner", "repo", 1, "<!-- blueprint:meta -->\n*$0.05*"
            )

        assert patched_body is not None
        assert "<!-- blueprint:meta -->" in patched_body
        assert "Existing PR body." in patched_body

    @pytest.mark.asyncio
    async def test_replaces_existing_marker_block(self):
        old_body = "PR summary.\n\n<!-- blueprint:meta -->\n*old content*"
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = {"body": old_body}
        patched_body = None

        async def mock_get(*args, **kwargs):
            return mock_response

        async def mock_patch(*args, **kwargs):
            nonlocal patched_body
            patched_body = kwargs.get("json", {}).get("body")
            return MagicMock(is_success=True)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.patch = mock_patch
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client
            await patch_pr_body_with_blueprint_meta(
                "tok", "owner", "repo", 1, "<!-- blueprint:meta -->\n*new content*"
            )

        assert patched_body is not None
        assert "new content" in patched_body
        assert "old content" not in patched_body

    @pytest.mark.asyncio
    async def test_returns_early_on_get_failure(self):
        mock_response = MagicMock()
        mock_response.is_success = False
        patch_called = False

        async def mock_get(*args, **kwargs):
            return mock_response

        async def mock_patch(*args, **kwargs):
            nonlocal patch_called
            patch_called = True
            return MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.patch = mock_patch
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client
            await patch_pr_body_with_blueprint_meta(
                "tok", "owner", "repo", 1, "<!-- blueprint:meta -->\n*x*"
            )

        assert not patch_called


# ── _handle_self_audit_completion / auto-clear direction mode ─────────────────


class TestHandleSelfAuditCompletion:
    """Tests for _handle_self_audit_completion focusing on the direction-mode
    auto-clear path: a direction stuck in audit/fundamentals mode (set by
    self_audit) should be cleared once the system reports 'ship' for
    _SHIP_RECOVERY_THRESHOLD consecutive assessments."""

    def _findings(self, system_id="sys-1", mode="ship", score=90):
        return json.dumps(
            {
                "findings": [
                    {
                        "system_id": system_id,
                        "recommended_mode": mode,
                        "score": score,
                        "components": {
                            "test_coverage": 90,
                            "friction_density": 10,
                            "claim_consistency": 90,
                            "failure_mix": 10,
                        },
                        "top_issues": [],
                        "evidence": [],
                    }
                ]
            }
        )

    async def _run(self, db_session, mocker, task, logs):
        from app.task_completion import _handle_self_audit_completion

        mocker.patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock)
        await _handle_self_audit_completion(task, logs, db_session)

    @pytest.mark.asyncio
    async def test_audit_mode_clears_after_threshold_ship_assessments(
        self, db_session, mocker
    ):
        """Direction in 'audit' (auto-set) clears after 2 consecutive 'ship' assessments."""
        from app.models import (
            RECOMMENDED_MODE_META_KEY,
            RECOMMENDED_MODE_SOURCE_SELF_AUDIT,
        )

        direction = {
            "id": "dir-1",
            "system_id": "sys-1",
            "status": "active",
            "recommended_mode": "audit",
            RECOMMENDED_MODE_META_KEY: {
                "source": RECOMMENDED_MODE_SOURCE_SELF_AUDIT,
                "mode": "audit",
            },
        }
        project = make_project(directions=[direction])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # First ship assessment — count not yet at threshold, mode stays.
        task1 = make_task(project_id=project.id)
        db_session.add(task1)
        await db_session.commit()
        await db_session.refresh(task1)
        await self._run(db_session, mocker, task1, self._findings(mode="ship"))
        await db_session.refresh(project)
        assert project.directions[0]["recommended_mode"] == "audit", (
            "should not clear after 1 ship"
        )

        # Second ship assessment — threshold met, mode clears.
        task2 = make_task(project_id=project.id)
        db_session.add(task2)
        await db_session.commit()
        await db_session.refresh(task2)
        await self._run(db_session, mocker, task2, self._findings(mode="ship"))
        await db_session.refresh(project)
        d = project.directions[0]
        assert d["recommended_mode"] is None, (
            "audit mode should be auto-cleared after 2 ship assessments"
        )
        meta = d.get(RECOMMENDED_MODE_META_KEY, {})
        assert "cleared_reason" in meta, "clear metadata should record reason"

    @pytest.mark.asyncio
    async def test_strategic_set_mode_is_not_cleared(self, db_session, mocker):
        """Directions whose mode was set by the strategic agent are left untouched."""
        from app.models import (
            RECOMMENDED_MODE_META_KEY,
            RECOMMENDED_MODE_SOURCE_STRATEGIC,
            SystemMaturity,
        )

        direction = {
            "id": "dir-2",
            "system_id": "sys-2",
            "status": "active",
            "recommended_mode": "audit",
            RECOMMENDED_MODE_META_KEY: {
                "source": RECOMMENDED_MODE_SOURCE_STRATEGIC,
                "mode": "audit",
            },
        }
        project = make_project(directions=[direction])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Seed SystemMaturity with 1 prior ship so next ship hits threshold=2.
        db_session.add(
            SystemMaturity(
                project_id=project.id,
                system_id="sys-2",
                score=90,
                components={"_consecutive_ship_count": 1},
                recommended_mode="ship",
                top_issues=[],
                evidence=[],
            )
        )
        await db_session.commit()

        task = make_task(project_id=project.id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        await self._run(
            db_session, mocker, task, self._findings(system_id="sys-2", mode="ship")
        )
        await db_session.refresh(project)
        assert project.directions[0]["recommended_mode"] == "audit", (
            "strategic-set mode must not be auto-cleared"
        )

    @pytest.mark.asyncio
    async def test_fundamentals_mode_clears_after_threshold(self, db_session, mocker):
        """'fundamentals' mode (auto-set) also clears after threshold ship assessments."""
        from app.models import (
            SystemMaturity,
            RECOMMENDED_MODE_META_KEY,
            RECOMMENDED_MODE_SOURCE_SELF_AUDIT,
        )

        direction = {
            "id": "dir-3",
            "system_id": "sys-3",
            "status": "active",
            "recommended_mode": "fundamentals",
            RECOMMENDED_MODE_META_KEY: {
                "source": RECOMMENDED_MODE_SOURCE_SELF_AUDIT,
                "mode": "fundamentals",
            },
        }
        project = make_project(directions=[direction])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Seed 1 prior ship so the next ship hits threshold.
        db_session.add(
            SystemMaturity(
                project_id=project.id,
                system_id="sys-3",
                score=90,
                components={"_consecutive_ship_count": 1},
                recommended_mode="ship",
                top_issues=[],
                evidence=[],
            )
        )
        await db_session.commit()

        task = make_task(project_id=project.id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        await self._run(
            db_session, mocker, task, self._findings(system_id="sys-3", mode="ship")
        )
        await db_session.refresh(project)
        assert project.directions[0]["recommended_mode"] is None, (
            "fundamentals mode should clear after threshold ship assessments"
        )

    @pytest.mark.asyncio
    async def test_non_ship_mode_resets_consecutive_count(self, db_session, mocker):
        """A non-ship audit finding resets the consecutive ship counter."""
        from app.models import SystemMaturity

        project = make_project(directions=[])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Seed with 1 consecutive ship.
        db_session.add(
            SystemMaturity(
                project_id=project.id,
                system_id="sys-4",
                score=90,
                components={"_consecutive_ship_count": 1},
                recommended_mode="ship",
                top_issues=[],
                evidence=[],
            )
        )
        await db_session.commit()

        task = make_task(project_id=project.id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        # Incoming mode is "audit" — not ship, so counter resets.
        await self._run(
            db_session, mocker, task, self._findings(system_id="sys-4", mode="audit")
        )

        from sqlmodel import select as _sel

        sm = (
            (
                await db_session.execute(
                    _sel(SystemMaturity).where(SystemMaturity.system_id == "sys-4")
                )
            )
            .scalars()
            .first()
        )
        assert sm is not None
        assert sm.components.get("_consecutive_ship_count", -1) == 0, (
            "consecutive ship count should reset on non-ship finding"
        )


# ── confirm_applied_playbook_entries ────────────────────────────────────────


class TestConfirmAppliedPlaybookEntries:
    @pytest.mark.asyncio
    async def test_no_op_when_no_entry_ids(self, db_session):
        """Returns early without DB write when playbook_applied_entry_ids is absent."""
        from app.task_completion import confirm_applied_playbook_entries

        project = make_project(
            design_doc={"playbook/tip-a": {"content": "x", "confirmed_count": 0}}
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, context={})
        db_session.add(task)
        await db_session.commit()

        await confirm_applied_playbook_entries(task, db_session)
        await db_session.refresh(project)
        assert project.design_doc["playbook/tip-a"]["confirmed_count"] == 0

    @pytest.mark.asyncio
    async def test_increments_confirmed_count_and_stamps_timestamp(self, db_session):
        """Increments confirmed_count and sets last_confirmed_at for matching entries."""
        from app.task_completion import confirm_applied_playbook_entries

        project = make_project(
            design_doc={
                "playbook/tip-a": {"content": "a", "confirmed_count": 2},
                "playbook/tip-b": {"content": "b", "confirmed_count": 0},
            }
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={"playbook_applied_entry_ids": ["playbook/tip-a"]},
        )
        db_session.add(task)
        await db_session.commit()

        await confirm_applied_playbook_entries(task, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        assert project.design_doc["playbook/tip-a"]["confirmed_count"] == 3
        assert "last_confirmed_at" in project.design_doc["playbook/tip-a"]
        # unmentioned entry untouched
        assert project.design_doc["playbook/tip-b"]["confirmed_count"] == 0

    @pytest.mark.asyncio
    async def test_peer_completion_credits_prior_entry_during_pending_update(
        self, db_session
    ):
        from app.task_completion import confirm_applied_playbook_entries

        writer_task_id = _uuid_mod.uuid4()
        project = make_project(
            design_doc={
                "playbook/tip-a": {
                    "content": "uncommitted replacement",
                    "pending_source": str(writer_task_id),
                    "confirmed_count": 0,
                    "_pending_previous": {
                        "content": "committed guidance",
                        "confirmed_count": 2,
                    },
                }
            }
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={"playbook_applied_entry_ids": ["playbook/tip-a"]},
        )
        db_session.add(task)
        await db_session.commit()

        await confirm_applied_playbook_entries(task, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        entry = project.design_doc["playbook/tip-a"]
        assert entry["confirmed_count"] == 0
        assert entry["_pending_previous"]["confirmed_count"] == 3
        assert "last_confirmed_at" in entry["_pending_previous"]

    @pytest.mark.asyncio
    async def test_normalizes_missing_prefix(self, db_session):
        """Entry IDs without 'playbook/' prefix are auto-prefixed before lookup."""
        from app.task_completion import confirm_applied_playbook_entries

        project = make_project(
            design_doc={
                "playbook/tip-x": {"content": "x", "confirmed_count": 1},
            }
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={"playbook_applied_entry_ids": ["tip-x"]},  # no prefix
        )
        db_session.add(task)
        await db_session.commit()

        await confirm_applied_playbook_entries(task, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        assert project.design_doc["playbook/tip-x"]["confirmed_count"] == 2

    @pytest.mark.asyncio
    async def test_skips_unknown_entry_ids(self, db_session):
        """Entry IDs not present in design_doc are silently skipped."""
        from app.task_completion import confirm_applied_playbook_entries

        project = make_project(
            design_doc={
                "playbook/real": {"content": "r", "confirmed_count": 0},
            }
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={"playbook_applied_entry_ids": ["playbook/ghost"]},
        )
        db_session.add(task)
        await db_session.commit()

        await confirm_applied_playbook_entries(task, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        assert project.design_doc["playbook/real"]["confirmed_count"] == 0

    @pytest.mark.asyncio
    async def test_no_op_when_project_has_no_design_doc(self, db_session):
        """Returns early without error when project.design_doc is None."""
        from app.task_completion import confirm_applied_playbook_entries

        project = make_project(design_doc=None)
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={"playbook_applied_entry_ids": ["playbook/tip-a"]},
        )
        db_session.add(task)
        await db_session.commit()

        await confirm_applied_playbook_entries(task, db_session)
        # no exception raised, no changes


# ── cross-layer remediation prompt ──────────────────────────────────────────




@pytest.mark.asyncio
async def test_spawn_forward_quality_grader_sets_sonnet_quality_tier(db_session):
    """spawn_forward_quality_grader must set quality_tier='sonnet' in the spawned
    grader task's payload — regression guard against accidental tier promotion."""
    from app.task_evaluators import spawn_forward_quality_grader

    project = make_project(name="FwdGraderTierProj")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    source = make_task(
        project_id=project.id,
        payloads=[{"type": "research", "base_branch": "main"}],
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)

    grader = await spawn_forward_quality_grader(
        source, "some artifact text", db_session
    )
    assert grader is not None
    assert (grader.first_payload or {}).get("quality_tier") == "sonnet", (
        "Spawned forward-quality grader must carry quality_tier='sonnet'"
    )


@pytest.mark.asyncio
async def test_spawn_finding_challenge_sets_sonnet_quality_tier(db_session):
    """spawn_finding_challenge must set quality_tier='sonnet' in the spawned
    challenge task's payload — regression guard against accidental tier promotion."""
    from app.task_evaluators import spawn_finding_challenge

    project = make_project(name="FindingChallengeTierProj")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    source = make_task(
        project_id=project.id,
        payloads=[{"type": "research", "base_branch": "main"}],
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)

    finding = {"description": "Some finding", "severity": "high"}
    challenge = await spawn_finding_challenge(
        source,
        finding,
        "reproduce_absent",
        "KEEP",
        db_session,
    )
    assert challenge is not None
    assert (challenge.first_payload or {}).get("quality_tier") == "sonnet", (
        "Spawned finding challenge must carry quality_tier='sonnet'"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Security audit completion handler — idempotency under replay
# ══════════════════════════════════════════════════════════════════════════════


_SECURITY_AUDIT_FINDINGS_JSON = json.dumps(
    [
        {
            "severity": "critical",
            "title": "SQL Injection in login endpoint",
            "description": "User input is interpolated directly into SQL queries.",
            "fix_prompt": "Parameterize all SQL queries in backend/app/routers/auth.py.",
            "file_path": "backend/app/routers/auth.py",
            "line_number": 42,
        },
        {
            "severity": "high",
            "title": "Missing auth check on admin endpoint",
            "description": "The /admin/reset endpoint has no @require_role decorator.",
            "fix_prompt": "Add @require_role decorator to the reset endpoint.",
            "file_path": "backend/app/routers/admin.py",
            "line_number": 87,
        },
    ]
)


class TestHandleSecurityAuditCompletion:
    """Tests for _handle_security_audit_completion focusing on idempotency."""

    async def _run(self, db_session, mocker, task, logs=None):
        from app.task_completion import _handle_security_audit_completion

        mocker.patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("token", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_open_pull_requests",
            new_callable=AsyncMock,
            return_value=[],
        )
        mocker.patch("app.task_completion._runtime_config", {})
        if logs is None:
            logs = _SECURITY_AUDIT_FINDINGS_JSON
        await _handle_security_audit_completion(task, logs, db_session)

    @pytest.mark.asyncio
    async def test_security_audit_completion_replay_spawns_no_duplicate_children(
        self, db_session, mocker
    ):
        """Replaying _handle_security_audit_completion when children already exist
        must not spawn duplicates — the already-spawned guard must fire."""
        from sqlmodel import select as sqlmodel_select
        from app.models import Task as TaskModel

        project = make_project(name="SecurityAuditIdempotencyProj")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        audit_task = make_task(
            project_id=project.id,
            title="security_audit: main",
            status=TaskStatus.COMPLETED,
            payloads=[
                {"type": "security_audit", "prompt": "audit", "base_branch": "main"}
            ],
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        # Simulate prior attempt: pre-commit children with the security_audit_task_id
        # context key (as if the first attempt committed children but crashed before
        # flipping the parent to EVALUATING).
        prior_child = make_task(
            project_id=project.id,
            title="[CRITICAL] SQL Injection fix",
            status=TaskStatus.QUEUED,
            parent_task_id=audit_task.id,
            context={"security_audit_task_id": str(audit_task.id), "finding_index": 0},
            payloads=[
                {"type": "create", "prompt": "fix sql injection", "base_branch": "main"}
            ],
        )
        db_session.add(prior_child)
        await db_session.commit()
        await db_session.refresh(prior_child)

        # Invoke the handler a second time (replay)
        await self._run(db_session, mocker, audit_task)

        # Exactly one child should exist — the pre-existing one; no new ones spawned
        children = (
            (
                await db_session.execute(
                    sqlmodel_select(TaskModel).where(
                        TaskModel.parent_task_id == audit_task.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(children) == 1, (
            f"Expected 1 child (pre-existing), got {len(children)} — duplicate children spawned on replay"
        )

        # Parent must be EVALUATING after the replay completes remaining effects
        await db_session.refresh(audit_task)
        assert audit_task.status == TaskStatus.EVALUATING, (
            f"Expected parent EVALUATING after replay, got {audit_task.status}"
        )

    @pytest.mark.asyncio
    async def test_security_audit_children_and_evaluating_committed_atomically(
        self, db_session, mocker
    ):
        """Children and parent EVALUATING flip must land in a single commit so a crash
        between commits cannot leave COMPLETED parent with committed children."""
        from sqlmodel import select as sqlmodel_select
        from app.models import Task as TaskModel

        project = make_project(name="SecurityAuditAtomicCommitProj")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        audit_task = make_task(
            project_id=project.id,
            title="security_audit: atomic",
            status=TaskStatus.COMPLETED,
            payloads=[
                {"type": "security_audit", "prompt": "audit", "base_branch": "main"}
            ],
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        commit_count = 0
        original_commit = db_session.commit

        async def counting_commit():
            nonlocal commit_count
            commit_count += 1
            await original_commit()

        mocker.patch.object(db_session, "commit", side_effect=counting_commit)

        await self._run(db_session, mocker, audit_task)

        await db_session.refresh(audit_task)
        children = (
            (
                await db_session.execute(
                    sqlmodel_select(TaskModel).where(
                        TaskModel.parent_task_id == audit_task.id
                    )
                )
            )
            .scalars()
            .all()
        )

        # When children exist, parent must be EVALUATING — proved by one commit only
        # in the spawn+flip path (the plan_output save at top + one final commit)
        assert audit_task.status == TaskStatus.EVALUATING
        assert len(children) == 2  # 2 actionable findings (critical + high)
        # The critical constraint: at most 2 commits total (plan_output save + spawn+flip)
        # NOT 3 separate commits (children, history, parent flip separately)
        assert commit_count <= 2, (
            f"Expected ≤2 commits (plan_output + atomic spawn+flip), got {commit_count}. "
            "A COMPLETED parent with committed children is a race window for duplicate spawns."
        )

    @pytest.mark.asyncio
    async def test_security_audit_no_children_when_no_actionable_findings(
        self, db_session, mocker
    ):
        """When findings contain no critical/high items, no children are spawned and
        the parent is marked COMPLETED directly (not EVALUATING)."""
        from sqlmodel import select as sqlmodel_select
        from app.models import Task as TaskModel

        project = make_project(name="SecurityAuditNoActionableProj")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        audit_task = make_task(
            project_id=project.id,
            title="security_audit: low only",
            status=TaskStatus.COMPLETED,
            payloads=[
                {"type": "security_audit", "prompt": "audit", "base_branch": "main"}
            ],
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        low_only_logs = json.dumps(
            [
                {
                    "severity": "low",
                    "title": "Minor logging issue",
                    "description": "Log level inconsistency.",
                    "fix_prompt": "Fix logging levels.",
                    "file_path": "backend/app/main.py",
                    "line_number": 10,
                }
            ]
        )

        await self._run(db_session, mocker, audit_task, logs=low_only_logs)

        await db_session.refresh(audit_task)
        children = (
            (
                await db_session.execute(
                    sqlmodel_select(TaskModel).where(
                        TaskModel.parent_task_id == audit_task.id
                    )
                )
            )
            .scalars()
            .all()
        )

        assert len(children) == 0
        assert audit_task.status == TaskStatus.COMPLETED


_PERF_FINDINGS_JSON = json.dumps(
    [
        {
            "severity": "high",
            "area": "database",
            "title": "N+1 query in user listing",
            "description": "User list endpoint queries database in loop.",
            "fix_prompt": "Use join query to fetch all users at once.",
        },
        {
            "severity": "high",
            "area": "cache",
            "title": "Missing cache invalidation",
            "description": "Cache not invalidated after updates.",
            "fix_prompt": "Add cache clear on update endpoints.",
        },
    ]
)


class TestHandlePerfCompletion:
    """Tests for _handle_perf_completion focusing on idempotency."""

    async def _run(self, db_session, mocker, task, logs=None):
        from app.task_completion import _handle_perf_completion

        mocker.patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("token", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_open_pull_requests",
            new_callable=AsyncMock,
            return_value=[],
        )
        mocker.patch("app.task_completion._runtime_config", {})
        if logs is None:
            logs = _PERF_FINDINGS_JSON
        await _handle_perf_completion(task, logs, db_session)

    @pytest.mark.asyncio
    async def test_perf_completion_idempotent_on_post_commit_replay(
        self, db_session, mocker
    ):
        """Replaying _handle_perf_completion when a child already exists
        must not spawn a duplicate — the already-spawned guard must fire."""
        from sqlmodel import select as sqlmodel_select
        from app.models import Task as TaskModel

        project = make_project(name="PerfIdempotencyProj")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        perf_task = make_task(
            project_id=project.id,
            title="perf: main",
            status=TaskStatus.COMPLETED,
            payloads=[
                {"type": "perf", "prompt": "audit", "base_branch": "main"}
            ],
        )
        db_session.add(perf_task)
        await db_session.commit()
        await db_session.refresh(perf_task)

        # Simulate prior attempt: pre-commit child with the perf_task_id
        # context key (as if the first attempt committed a child but crashed before
        # flipping the parent to EVALUATING).
        prior_child = make_task(
            project_id=project.id,
            title="[PERF] Fix 2 high-severity performance findings",
            status=TaskStatus.QUEUED,
            parent_task_id=perf_task.id,
            context={"perf_task_id": str(perf_task.id), "finding_count": 2},
            payloads=[
                {"type": "create", "prompt": "fix perf issues", "base_branch": "main"}
            ],
        )
        db_session.add(prior_child)
        await db_session.commit()
        await db_session.refresh(prior_child)

        # Invoke the handler a second time (replay)
        await self._run(db_session, mocker, perf_task)

        # Exactly one child should exist — the pre-existing one; no new ones spawned
        children = (
            (
                await db_session.execute(
                    sqlmodel_select(TaskModel).where(
                        TaskModel.parent_task_id == perf_task.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(children) == 1, (
            f"Expected 1 child (pre-existing), got {len(children)} — duplicate children spawned on replay"
        )

        # Parent must be EVALUATING after the replay completes remaining effects
        await db_session.refresh(perf_task)
        assert perf_task.status == TaskStatus.EVALUATING, (
            f"Expected parent EVALUATING after replay, got {perf_task.status}"
        )


# ── _handle_walkthrough_e2e_completion ──────────────────────────────────────


class TestWalkthroughE2ECompletionIngestsOrRemoved:
    """Regression tests for _handle_walkthrough_e2e_completion.

    The named test required by acceptance evidence:
        test_walkthrough_e2e_completion_ingests_or_removed
    """

    async def _run(self, db_session, task, logs):
        from app.task_completion import _handle_walkthrough_e2e_completion

        await _handle_walkthrough_e2e_completion(task, logs, db_session)

    @pytest.mark.asyncio
    async def test_walkthrough_e2e_completion_ingests_or_removed(self, db_session):
        """_handle_walkthrough_e2e_completion ingests {git_sha,results} artifacts.

        With a matching candidate_sha the handler delegates to
        record_walkthrough_e2e_evidence, which writes covered-tier evidence for
        each criterion whose passed=True.  A mismatched SHA writes nothing
        (SHA-match safety enforcement).  The legacy {flows:[...]} schema is
        skipped gracefully (no exception, no evidence).
        """
        import app.acceptance_criteria as ac
        from app.models import ReleaseVersion

        project = make_project(name="WalkthroughE2ETest")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        sha = "a" * 40
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            status="open",
            last_qualification={"status": "scoring", "candidate_sha": sha},
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        # Register the three canonical evidence criteria behind the frozen
        # four-row v26 report (workday slug + meta row intentionally collapse).
        crit_a = await ac.create_criterion(
            db_session, project.id, version.id, "Runner enroll flow"
        )
        crit_a.criterion_key = "c-dfdc9d82"
        db_session.add(crit_a)
        await db_session.commit()
        crit_b = await ac.create_criterion(
            db_session, project.id, version.id, "Iframe token flow"
        )
        crit_b.criterion_key = "c-f52fd975"
        db_session.add(crit_b)
        await db_session.commit()
        crit_meta = await ac.create_criterion(
            db_session, project.id, version.id, "Complete walkthrough meta flow"
        )
        crit_meta.criterion_key = "c-5214996f"
        db_session.add(crit_meta)
        await db_session.commit()

        # Build a well-formed artifact in the correct schema.
        artifact = json.dumps(
            {
                "git_sha": sha,
                "exercised_engine_sha": sha,
                "run_timestamp": "2026-01-01T00:00:00Z",
                "results": [
                    {"criterion_key": crit_a.criterion_key, "passed": True, "notes": "ok"},
                    {"criterion_key": crit_b.criterion_key, "passed": False, "notes": "fail"},
                    {"criterion_key": "c-workday-session-lifecycle", "passed": True, "notes": "ok"},
                    {"criterion_key": crit_meta.criterion_key, "passed": False, "notes": "iframe failed"},
                ],
            }
        )

        task = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={
                "release_version_id": str(version.id),
                "candidate_sha": sha,
                "qualification_attempt_id": "attempt-1",
            },
            payloads=[{"type": "walkthrough_e2e", "candidate_sha": sha}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        # Happy-path: matching SHA → evidence written for crit_a only.
        await self._run(db_session, task, artifact)

        ev_a = await ac.get_evidence_for_criterion(db_session, crit_a.id)
        assert len(ev_a) == 1, "Expected one evidence row for the passing criterion"
        assert ev_a[0].source_kind == ac.WALKTHROUGH_E2E_EVIDENCE_KIND
        assert ev_a[0].detail["candidate_sha"] == sha
        assert ev_a[0].detail["passed"] is True
        assert ac.compute_criterion_coverage(ev_a) == "covered"

        ev_b = await ac.get_evidence_for_criterion(db_session, crit_b.id)
        assert ev_b == [], "Failed criterion must not gain evidence"
        ev_meta = await ac.get_evidence_for_criterion(db_session, crit_meta.id)
        assert ev_meta == [], "Failed meta gate must not gain alias-collapsed evidence"

        # SHA mismatch: writes nothing (SHA-match safety).
        mismatched_artifact = json.dumps(
            {
                "git_sha": "b" * 40,
                "exercised_engine_sha": "b" * 40,
                "run_timestamp": "2026-01-01T00:00:00Z",
                "results": [
                    {"criterion_key": "c-dfdc9d82", "passed": True, "notes": "ok"},
                    {"criterion_key": "c-f52fd975", "passed": True, "notes": "ok"},
                    {"criterion_key": "c-workday-session-lifecycle", "passed": True, "notes": "ok"},
                    {"criterion_key": "c-5214996f", "passed": True, "notes": "ok"},
                ],
            }
        )
        await self._run(db_session, task, mismatched_artifact)
        ev_b_after = await ac.get_evidence_for_criterion(db_session, crit_b.id)
        assert ev_b_after == [], "Mismatched SHA must not write evidence"

        # Legacy {flows:[...]} schema: skipped gracefully, no exception, no extra evidence.
        legacy_artifact = json.dumps(
            {
                "flows": [
                    {"flow": crit_a.criterion_key, "verdict": "PASS", "candidate_sha": sha},
                ]
            }
        )
        await self._run(db_session, task, legacy_artifact)
        ev_a_after = await ac.get_evidence_for_criterion(db_session, crit_a.id)
        assert len(ev_a_after) == 1, "Legacy schema must not write additional evidence"


class TestHandleWalkthroughE2eCompletionStandalone:
    """Standalone (no-DB) tests for _handle_walkthrough_e2e_completion guard paths.

    Covers edge cases (missing git_sha, missing results list, no open version)
    not exercised by the DB-backed integration test above.
    """

    def _make_task(self, project_id=None, candidate_sha=None):
        import uuid
        task = MagicMock()
        task.id = uuid.uuid4()
        task.project_id = project_id or uuid.uuid4()
        task.context = {"candidate_sha": candidate_sha} if candidate_sha else {}
        task.first_payload = (
            {"candidate_sha": candidate_sha} if candidate_sha else {}
        )
        return task

    def _make_session(self):
        session = MagicMock()
        session.rollback = AsyncMock()
        return session

    @pytest.mark.asyncio
    async def test_unparseable_artifact_is_noop(self):
        from app.task_completion import _handle_walkthrough_e2e_completion
        task = self._make_task(candidate_sha="abc123")
        session = self._make_session()
        await _handle_walkthrough_e2e_completion(task, "not json", session)
        session.rollback.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_results_list_is_noop(self):
        from app.task_completion import _handle_walkthrough_e2e_completion
        task = self._make_task()
        session = self._make_session()
        artifact = json.dumps({"git_sha": "abc123", "run_timestamp": "2026-01-01T00:00:00Z"})
        with patch("app.acceptance_criteria.record_walkthrough_e2e_evidence") as mock_rwe, \
             patch("app.release_version.current_open_version", new=AsyncMock(return_value=MagicMock())):
            await _handle_walkthrough_e2e_completion(task, artifact, session)
            mock_rwe.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_open_version_is_noop(self):
        from app.task_completion import _handle_walkthrough_e2e_completion
        task = self._make_task(candidate_sha="abc123")
        session = self._make_session()
        artifact = json.dumps({
            "git_sha": "abc123",
            "exercised_engine_sha": "abc123",
            "run_timestamp": "2026-01-01T00:00:00Z",
            "results": [
                {"criterion_key": "c-dfdc9d82", "passed": True},
                {"criterion_key": "c-f52fd975", "passed": True},
                {"criterion_key": "c-workday-session-lifecycle", "passed": True},
                {"criterion_key": "c-5214996f", "passed": True},
            ],
        })
        with patch("app.acceptance_criteria.record_walkthrough_e2e_evidence") as mock_rwe, \
             patch("app.release_version.current_open_version", new=AsyncMock(return_value=None)):
            await _handle_walkthrough_e2e_completion(task, artifact, session)
            mock_rwe.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_artifact_calls_record_walkthrough_e2e_evidence(self):
        """The handler delegates to record_walkthrough_e2e_evidence with correct kwargs."""
        from app.task_completion import _handle_walkthrough_e2e_completion
        sha = "deadbeef1234567890abcdef"
        task = self._make_task(candidate_sha=sha)
        session = self._make_session()
        session.get = AsyncMock(return_value=task)
        results = [
            {"criterion_key": "c-dfdc9d82", "passed": True, "notes": "step 1 ok"},
            {"criterion_key": "c-f52fd975", "passed": False, "notes": None},
            {"criterion_key": "c-workday-session-lifecycle", "passed": True, "notes": "ok"},
            {"criterion_key": "c-5214996f", "passed": False, "notes": "iframe failed"},
        ]
        artifact = json.dumps({
            "git_sha": sha,
            "exercised_engine_sha": sha,
            "run_timestamp": "2026-01-01T00:00:00Z",
            "results": results,
        })
        mock_version = MagicMock()
        summary = {"status": "recorded", "recorded": 1, "skipped_not_passed": 1, "unmatched_keys": []}
        with patch("app.acceptance_criteria.record_walkthrough_e2e_evidence", new=AsyncMock(return_value=summary)) as mock_rwe, \
             patch("app.release_version.current_open_version", new=AsyncMock(return_value=mock_version)):
            await _handle_walkthrough_e2e_completion(task, artifact, session)
            mock_rwe.assert_awaited_once()
            call_kwargs = mock_rwe.call_args.kwargs
            assert call_kwargs["candidate_sha"] == sha, "candidate_sha must match artifact git_sha"
            assert call_kwargs["results"] == results, "results list must be passed through"


def test_meta_criterion_requires_substantive_subcriteria():
    """c-5214996f is blocked when a required sub-criterion is failed or absent.

    Exercises _walkthrough_meta_criterion_gate directly (pure, no DB). The
    required set is supplied by the caller (resolved from the open version's
    ACTIVE rubric by _walkthrough_meta_required_subcriteria) — the gate is the
    subset check on that resolved set, not a hardcoded v26 key list.
    """
    from app.acceptance_criteria import _walkthrough_meta_criterion_gate

    v26_required = frozenset({"c-dfdc9d82", "c-f52fd975"})

    # Gate should pass only when ALL required sub-criteria are passed.
    full_pass = [
        {"criterion_key": "c-dfdc9d82", "passed": True, "notes": "ok"},
        {"criterion_key": "c-f52fd975", "passed": True, "notes": "ok"},
        {"criterion_key": "c-workday-session-lifecycle", "passed": True, "notes": "ok"},
        {"criterion_key": "c-5214996f", "passed": True, "notes": "ok"},
    ]
    assert _walkthrough_meta_criterion_gate(full_pass, v26_required) is True, \
        "gate must pass when c-dfdc9d82 and c-f52fd975 are both passed"

    # Missing c-f52fd975 entirely → gate fails.
    missing_iframe = [
        {"criterion_key": "c-dfdc9d82", "passed": True, "notes": "ok"},
        {"criterion_key": "c-5214996f", "passed": True, "notes": "ok"},
    ]
    assert _walkthrough_meta_criterion_gate(missing_iframe, v26_required) is False, \
        "gate must fail when c-f52fd975 is absent"

    # c-f52fd975 present but passed=False → gate fails.
    iframe_failed = [
        {"criterion_key": "c-dfdc9d82", "passed": True, "notes": "ok"},
        {"criterion_key": "c-f52fd975", "passed": False, "notes": "failed"},
        {"criterion_key": "c-5214996f", "passed": True, "notes": "ok"},
    ]
    assert _walkthrough_meta_criterion_gate(iframe_failed, v26_required) is False, \
        "gate must fail when c-f52fd975 has passed=False"

    # c-dfdc9d82 absent → gate fails.
    missing_runner = [
        {"criterion_key": "c-f52fd975", "passed": True, "notes": "ok"},
        {"criterion_key": "c-5214996f", "passed": True, "notes": "ok"},
    ]
    assert _walkthrough_meta_criterion_gate(missing_runner, v26_required) is False, \
        "gate must fail when c-dfdc9d82 is absent"

    # Fresh (non-v26) rubric keys resolve the same way: the meta is gated on
    # whatever walkthrough sub-criteria the OPEN rubric registers.
    fresh_required = frozenset({"c-fresh-runner", "c-fresh-iframe"})
    fresh_full_pass = [
        {"criterion_key": "c-fresh-runner", "passed": True, "notes": "ok"},
        {"criterion_key": "c-fresh-iframe", "passed": True, "notes": "ok"},
        {"criterion_key": "c-5214996f", "passed": True, "notes": "ok"},
    ]
    assert _walkthrough_meta_criterion_gate(fresh_full_pass, fresh_required) is True, \
        "gate must recognize fresh walkthrough-provable sub-criterion keys"

    # A legacy workday alias in the batch resolves to the meta itself and must
    # NOT satisfy a fresh substantive sub-criterion that was not exercised.
    assert _walkthrough_meta_criterion_gate(
        [{"criterion_key": "c-workday-session-lifecycle", "passed": True, "notes": "ok"}],
        fresh_required,
    ) is False, \
        "a batch that does not exercise a fresh required sub-criterion must fail the gate"

    # Empty batch → gate fails.
    assert _walkthrough_meta_criterion_gate([], v26_required) is False, \
        "gate must fail on empty results"

    # Fail-closed: an empty required set (a rubric whose only walkthrough
    # criterion is the meta itself) must never mint meta evidence.
    assert _walkthrough_meta_criterion_gate(full_pass, frozenset()) is False, \
        "gate must fail closed when the rubric resolves no substantive sub-criteria"

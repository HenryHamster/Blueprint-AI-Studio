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
- _synthesize_memo_from_source_finding, build_blueprint_pr_* helpers
"""

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastapi import HTTPException

from app.task_completion import (
    _parse_plan_output,
    _extract_json_array,
    _inject_file_overlap_deps,
    _log_file_overlaps,
    _compute_first_attempt_succeeded,
    _validate_plan_dependencies,
    _ensure_branch,
    _handle_plan_completion,
    _record_remediation_outcome,
    _record_remediation_if_terminal,
    _record_project_remediation_outcome,
    build_blueprint_pr_footer,
    build_blueprint_pr_merge_summary_comment,
    patch_pr_body_with_blueprint_meta,
    BLUEPRINT_PR_META_MARKER,
    BLUEPRINT_PR_MERGE_SUMMARY_MARKER,
    _synthesize_memo_from_source_finding,
)
from app.models import PoolVmState, Task, TaskStatus
from testing.factories import make_project, make_task

_USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


# ── _compute_first_attempt_succeeded ────────────────────────────────────────


class TestComputeFirstAttemptSucceeded:
    def test_attempt_zero_completed(self):
        assert _compute_first_attempt_succeeded(0, TaskStatus.COMPLETED) is True

    def test_attempt_one_completed(self):
        """attempt_count=1 (reaper retried once) still counts as first-attempt bucket."""
        assert _compute_first_attempt_succeeded(1, TaskStatus.COMPLETED) is True

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
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
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
        """If create_branch fails but verify succeeds, we reuse the existing branch."""
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
            side_effect=["sha1", "sha2"],  # source SHA, then verify returns OK
        )
        create = mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=422, detail="reference exists"),
        )

        relocked = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        # R4-W1 (audit C2): success returns the relocked Task instance.
        assert relocked is not None
        assert create.await_count == 1
        assert get_sha.await_count == 2
        assert relocked.status != TaskStatus.FAILED

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
            interloper = make_task(project_id=task.project_id, vm_instance_id="vm-other")
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
            await _record_remediation_if_terminal(task, TaskStatus.COMPLETED, db_session)
            await db_session.commit()
            member = await get_pool_vm(db_session, "vm-wrap")
            assert member.state == PoolVmState.IDLE
        finally:
            shared_state.runtime_config["warm_pool_enabled"] = prev


# ── _synthesize_memo_from_source_finding ────────────────────────────────────


class TestSynthesizeMemoFromSourceFinding:
    """Pure-function tests for the backend-side memo synthesis helper."""

    def test_regression_false_positive_full_data(self):
        sf = {
            "verdict_reason": "design_doc says this is intentional",
            "investigation_summary": "trace confirms the behavior",
            "regression_class": "sr_catch_rate:correctness",
        }
        result = _synthesize_memo_from_source_finding(
            "regression_false_positive", sf, "task-1"
        )
        assert result is not None
        assert "design_doc" in result["summary"]
        assert "trace confirms" in result["reasoning"]
        assert "task:task-1" in result["citations"]
        assert "regression-false-positive" in result["tags"]
        assert "sr_catch_rate:correctness" in result["tags"]

    def test_regression_false_positive_missing_verdict_reason_returns_none(self):
        assert (
            _synthesize_memo_from_source_finding("regression_false_positive", {}, "t")
            is None
        )

    def test_investigation_unactionable_with_root_cause(self):
        sf = {
            "root_cause": "workers acquire locks in opposite order",
            "summary": "deadlock in rollup coordinator",
            "recommended_fix": "fix lock order",
            "outcome": "structural",
        }
        result = _synthesize_memo_from_source_finding(
            "investigation_unactionable", sf, "task-2"
        )
        assert result is not None
        assert "workers acquire locks" in result["reasoning"]
        assert "fix lock order" in result["reasoning"]
        assert "investigation-unactionable" in result["tags"]
        assert "structural" in result["tags"]
        assert "task:task-2" in result["citations"]

    def test_investigation_unactionable_missing_root_cause_and_summary_returns_none(
        self,
    ):
        assert (
            _synthesize_memo_from_source_finding(
                "investigation_unactionable", {"outcome": "x"}, "t"
            )
            is None
        )

    def test_unknown_reasoning_kind_returns_none(self):
        assert (
            _synthesize_memo_from_source_finding("deferred_finding", {"body": "x"}, "t")
            is None
        )


# ── _handle_plan_completion — scope-aware subtask effort ──────────────────────


class TestPlanSubtaskScopeAwareEffort:
    """Plan subtasks inherit effort from parent, but with a scope-aware floor."""

    def _plan_logs(self, subtasks):
        return json.dumps({"subtasks": subtasks})

    async def _setup_plan_task(self, db_session, mocker, parent_effort="medium"):
        mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
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



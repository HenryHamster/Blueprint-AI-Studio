"""
Unit tests for app/task_completion.py pure functions.

Covers:
- _parse_plan_output: JSON extraction from agent logs
- _extract_json_array: JSON array extraction by required keys
- _inject_file_overlap_deps: file-overlap dependency injection and cycle fallback
- _log_file_overlaps: overlapping file detection
- _compute_first_attempt_succeeded: attempt_count threshold and status check
- _ensure_branch: retry/backoff, token refresh, exists-verification, terminal failure
"""
import json
import pytest
from unittest.mock import AsyncMock

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
    _maybe_spawn_followup_investigation,
    _handle_investigation_completion,
    _extract_partial_diagnosis,
    _handle_investigation_terminal_failure,
    _maybe_spawn_reinvestigation_after_failure,
    _is_structural_diagnosis,
    _clear_direction_diagnosis_flag,
    _calculate_remediation_scope,
)
from app.strategic_constants import STRATEGIC_RATIONALE_MAX
from app.models import TaskStatus
from testing.factories import make_project, make_task


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
        plan = self._make_plan([
            {"title": "Task A", "prompt": "Do A"},
            {"title": "Task B", "prompt": "Do B", "depends_on_indices": [0]},
        ])
        result = _parse_plan_output(plan)
        assert result is not None
        assert len(result["subtasks"]) == 2
        assert result["subtasks"][1]["depends_on_indices"] == [0]

    def test_embedded_in_text(self):
        """JSON embedded in surrounding log text should still be extracted."""
        text = "Some preamble\n" + self._make_plan([{"title": "A", "prompt": "p"}]) + "\nTrailing text"
        result = _parse_plan_output(text)
        assert result is not None
        assert result["subtasks"][0]["title"] == "A"

    def test_braces_in_string_values(self):
        """Braces inside JSON string values (e.g. code snippets) must not break extraction."""
        plan = json.dumps({
            "subtasks": [{
                "title": "Fix",
                "prompt": "Run this: func() { return {a: 1}; }",
            }]
        })
        result = _parse_plan_output(plan)
        assert result is not None
        assert "func()" in result["subtasks"][0]["prompt"]

    def test_escaped_characters_in_strings(self):
        """Escaped backslashes and quotes in string values should be handled correctly."""
        plan = json.dumps({
            "subtasks": [{
                "title": "Escape test",
                "prompt": 'Use \\"double quotes\\" and backslash: \\\\',
            }]
        })
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
            "subtasks": [{
                "title": "Add function",
                "prompt": "Add this:\n```python\ndef foo():\n    return {'key': {'nested': True}}\n```",
            }]
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
        original_deps = {i: list(st.get("depends_on_indices", [])) for i, st in enumerate(subtasks)}
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
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("app.routers.agent_status.record_task_result")


class TestEnsureBranch:
    @pytest.mark.asyncio
    async def test_success_first_attempt(self, db_session, mocker, _patch_ensure_branch_deps):
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

        ok = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        assert ok is True
        # Only source-branch SHA lookup + create, no verify path
        assert get_sha.await_count == 1
        assert create.await_count == 1
        # First attempt: force_refresh must be False
        assert get_token.await_args.kwargs.get("force_refresh") is False
        assert task.status != TaskStatus.FAILED

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

        ok = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        assert ok is True
        assert get_sha.await_count == 3
        assert task.status != TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_force_refresh_on_retry(self, db_session, mocker, _patch_ensure_branch_deps):
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

        ok = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        assert ok is True
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

        ok = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        assert ok is True
        assert create.await_count == 1
        assert get_sha.await_count == 2
        assert task.status != TaskStatus.FAILED

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
            "app.main._maybe_create_failure_alert", new_callable=AsyncMock
        )

        ok = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        assert ok is False
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

        ok = await _ensure_branch(task, "plan/xyz", "main", "PLAN", db_session)

        assert ok is False
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

        updated = await _record_remediation_outcome(task, merged=True, session=db_session)
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

        updated = await _record_remediation_outcome(task, merged=True, session=db_session)
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
        project = make_project(directions=[{
            "id": "dir-1",
            "status": "active",
            "remediation_attempts": 2,
            "last_remediation_result": "merged",
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={"diagnosis_task_id": "diag-9", "direction_id": "dir-1"},
        )
        db_session.add(task)
        await db_session.commit()

        updated = await _record_remediation_outcome(task, merged=False, session=db_session)
        await db_session.commit()
        await db_session.refresh(project)

        assert updated is True
        d = project.directions[0]
        assert d["remediation_attempts"] == 3
        assert d["last_remediation_result"] == "failed"

    @pytest.mark.asyncio
    async def test_no_match_when_direction_missing(self, db_session):
        """Unknown direction_id is a no-op (returns False, no mutation)."""
        project = make_project(directions=[{"id": "dir-other"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={"diagnosis_task_id": "diag-1", "direction_id": "dir-missing"},
        )
        db_session.add(task)
        await db_session.commit()

        updated = await _record_remediation_outcome(task, merged=True, session=db_session)
        assert updated is False
        assert "remediation_attempts" not in project.directions[0]

    @pytest.mark.asyncio
    async def test_increments_when_fingerprint_matches(self, db_session):
        """Same fingerprint as prior remediation increments attempts."""
        project = make_project(directions=[{
            "id": "dir-1",
            "status": "active",
            "remediation_attempts": 2,
            "last_remediation_fingerprint": "fp-same",
        }])
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
        project = make_project(directions=[{
            "id": "dir-1",
            "status": "active",
            "remediation_attempts": 2,
            "last_remediation_fingerprint": "fp-old",
        }])
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
        project = make_project(directions=[{
            "id": "dir-1",
            "status": "active",
            "remediation_attempts": 1,
            "last_remediation_fingerprint": "fp-prior",
        }])
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
        project = make_project(directions=[{
            "id": "dir-1",
            "status": "active",
            "remediation_attempts": 1,
            "last_remediation_result": "failed",
            "last_remediation_failure_class": "CODE_ERROR",
            "last_remediation_error_summary": "previous error output",
        }])
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
            payloads=[{"type": "create", "prompt": "apply guard clause", "base_branch": "main"}],
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
            {"task_id": f"prev-{i}", "result": "failed", "timestamp": f"2020-01-0{i+1}T00:00:00"}
            for i in range(_REMEDIATION_HISTORY_MAX)
        ]
        project = make_project(directions=[{
            "id": "dir-1", "status": "active", "remediation_history": list(existing),
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            context={"diagnosis_task_id": "diag-1", "direction_id": "dir-1"},
            payloads=[{"type": "create", "prompt": "newest fix", "base_branch": "main"}],
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
    async def test_injects_note_into_sibling_with_matching_fingerprint(self, db_session):
        project = make_project(directions=[
            {"id": "dir-1", "status": "active"},
            self._paused_sibling("dir-2", "fp-shared"),
        ])
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
        assert sibling["status"] == "paused"
        assert sibling["auto_paused"] is True
        notes = sibling.get("notes") or []
        assert len(notes) == 1
        assert notes[0]["source"] == "sibling_remediation_success"
        # Target direction still stamped normally.
        target = next(d for d in project.directions if d["id"] == "dir-1")
        assert target["last_remediation_result"] == "merged"

    @pytest.mark.asyncio
    async def test_noop_when_no_siblings_share_fingerprint(self, db_session):
        project = make_project(directions=[
            {"id": "dir-1", "status": "active"},
            self._paused_sibling("dir-2", "fp-other"),
        ])
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
        project = make_project(directions=[
            {"id": "dir-1", "status": "active"},
            self._paused_sibling("dir-2", "fp-shared"),
        ])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            payloads=[{"prompt": "Pin the pydantic version in requirements.txt to fix startup."}],
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
        project = make_project(directions=[
            {"id": "dir-1", "status": "active"},
            self._paused_sibling("dir-2", "fp-shared"),
        ])
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
    async def test_injects_failure_note_into_sibling_with_matching_fingerprint(self, db_session):
        project = make_project(directions=[
            {"id": "dir-1", "status": "active"},
            self._paused_sibling("dir-2", "fp-shared"),
        ])
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
        project = make_project(directions=[
            {"id": "dir-1", "status": "active"},
            self._paused_sibling("dir-2", "fp-other"),
        ])
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
        project = make_project(directions=[
            {"id": "dir-1", "status": "active"},
            self._paused_sibling("dir-2", "fp-shared"),
        ])
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

        for status in (TaskStatus.IN_PROGRESS, TaskStatus.QUEUED, TaskStatus.EVALUATING, TaskStatus.PROVISIONING):
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
    @pytest.mark.parametrize("task_type", ["create", "refactor", "docs", "qa", "deps", "migrate", "fix"])
    async def test_completed_pr_creating_type_does_not_record(self, db_session, task_type):
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
    async def test_completed_review_type_does_record(self, db_session):
        """Review tasks represent the PR's actual outcome — COMPLETED means merged."""
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
        assert d["remediation_attempts"] == 1
        assert d["last_remediation_result"] == "merged"

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

class TestFollowupInvestigationOnFailure:
    """_maybe_spawn_followup_investigation spawns a new investigate task when a
    diagnosis-spawned remediation fails, subject to dedup + cap guards."""

    @staticmethod
    def _make_remediation_task(project_id, *, direction_id="dir-1",
                               fingerprint="fp-A", prompt="apply the fix",
                               status=TaskStatus.FAILED,
                               latest_status_message="ImportError: boom"):
        return make_task(
            project_id=project_id,
            status=status,
            latest_status_message=latest_status_message,
            context={
                "diagnosis_task_id": "diag-1",
                "direction_id": direction_id,
                "diagnosis_fingerprint": fingerprint,
                "failure_category": "CODE_ERROR",
            },
            payloads=[{"type": "create", "prompt": prompt, "base_branch": "main"}],
        )

    @pytest.mark.asyncio
    async def test_follow_up_spawned_on_first_failure(self, db_session):
        """Failed remediation spawns an investigate task and updates direction state."""
        project = make_project(directions=[{
            "id": "dir-1", "status": "active", "text": "demo direction",
            "diagnosis_summary": "race on record_direction_result",
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_remediation_task(project.id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        fp_entry = d["followup_investigation_attempts_by_fingerprint"]["fp-A"]
        assert isinstance(fp_entry, list)
        assert len(fp_entry) == 1
        # Entry is a parseable ISO timestamp
        from datetime import datetime as _dt
        _dt.fromisoformat(fp_entry[0])
        assert d["diagnosis_in_progress"] is True
        assert "diagnosis_started_at" in d

        from sqlmodel import select as _select
        from app.models import Task as TaskModel
        spawned = (await db_session.execute(
            _select(TaskModel).where(
                TaskModel.project_id == project.id,
                TaskModel.id != task.id,
            )
        )).scalars().all()
        assert len(spawned) == 1
        spawned_task = spawned[0]
        assert spawned_task.payloads[0]["type"] == "investigate"
        assert spawned_task.context["diagnosis_trigger"] == "remediation_failure_followup"
        assert spawned_task.context["direction_id"] == "dir-1"
        assert spawned_task.context["diagnosis_fingerprint"] == "fp-A"
        assert spawned_task.context["failed_remediation_task_id"] == str(task.id)
        assert spawned_task.context["original_diagnosis_task_id"] == "diag-1"

    @pytest.mark.asyncio
    async def test_follow_up_skipped_by_24h_dedup(self, db_session):
        """An investigate task created in the last 24h blocks a new follow-up."""
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        # Pre-seed a recent investigate task for this direction
        prior = make_task(
            project_id=project.id,
            payloads=[{"type": "investigate", "prompt": "old", "base_branch": "main"}],
            context={"direction_id": "dir-1"},
        )
        db_session.add(prior)
        await db_session.commit()
        task = self._make_remediation_task(project.id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert "followup_investigation_attempts_by_fingerprint" not in d
        assert d.get("diagnosis_in_progress") is not True

    @pytest.mark.asyncio
    async def test_follow_up_skipped_when_cap_reached(self, db_session):
        """Per-fingerprint cap blocks a third follow-up for the same fingerprint."""
        from app.time_utils import _utcnow
        recent_ts = [_utcnow().isoformat(), _utcnow().isoformat()]
        project = make_project(directions=[{
            "id": "dir-1", "status": "active",
            "followup_investigation_attempts_by_fingerprint": {"fp-A": list(recent_ts)},
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_remediation_task(project.id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert d["followup_investigation_attempts_by_fingerprint"] == {"fp-A": recent_ts}
        assert d.get("diagnosis_in_progress") is not True

    @pytest.mark.asyncio
    async def test_follow_up_skipped_when_diagnosis_in_progress(self, db_session):
        """Do not spawn when an investigation is already open for the direction."""
        project = make_project(directions=[{
            "id": "dir-1", "status": "active", "diagnosis_in_progress": True,
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_remediation_task(project.id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert "followup_investigation_attempts_by_fingerprint" not in d

    @pytest.mark.asyncio
    async def test_follow_up_prompt_includes_enriched_context(self, db_session):
        """Spawned investigate prompt carries summary, failed approach, class, error."""
        project = make_project(directions=[{
            "id": "dir-1", "status": "active",
            "text": "fix rollup reliability",
            "diagnosis_summary": "queue drains lock dict without ownership check",
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_remediation_task(
            project.id,
            prompt="Wrap drain_rollups in per-project asyncio.Lock",
            latest_status_message="TypeError in drain_rollups at line 88: NoneType",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()

        from sqlmodel import select as _select
        from app.models import Task as TaskModel
        spawned = (await db_session.execute(
            _select(TaskModel).where(
                TaskModel.project_id == project.id,
                TaskModel.id != task.id,
            )
        )).scalars().first()
        assert spawned is not None
        prompt_text = spawned.payloads[0]["prompt"]
        assert "queue drains lock dict without ownership check" in prompt_text
        assert "Wrap drain_rollups in per-project asyncio.Lock" in prompt_text
        assert "CODE_ERROR" in prompt_text
        assert "TypeError in drain_rollups" in prompt_text

    @pytest.mark.asyncio
    async def test_follow_up_propagates_failure_class(self, db_session):
        """Parent's trigger_failure_class becomes last_failure_class on the spawned
        follow-up, so _spawn_investigation_remediation will set trigger_failure_class
        correctly on the next remediation."""
        project = make_project(directions=[{
            "id": "dir-1", "status": "active", "text": "demo",
        }])
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
                "diagnosis_fingerprint": "fp-A",
                "trigger_failure_class": "CODE_ERROR",
                "trigger_failure_taxonomy": {"kind": "import"},
            },
            payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()

        from sqlmodel import select as _select
        from app.models import Task as TaskModel
        spawned = (await db_session.execute(
            _select(TaskModel).where(
                TaskModel.project_id == project.id,
                TaskModel.id != task.id,
            )
        )).scalars().first()
        assert spawned is not None
        assert spawned.context["last_failure_class"] == "CODE_ERROR"
        assert spawned.context["failure_taxonomy"] == {"kind": "import"}

        # Verify the downstream hop: a remediation spawned from this follow-up
        # investigation carries trigger_failure_class set from last_failure_class.
        from app.task_completion import _spawn_investigation_remediation
        remediation = await _spawn_investigation_remediation(
            task=spawned,
            direction_id="dir-1",
            summary="s", root_cause="rc", recommended_fix="rf",
            severity="medium", confidence="high",
            affected_paths=["a.py"], remediation_attempts=0,
            session=db_session,
        )
        assert remediation is not None
        assert remediation.context["trigger_failure_class"] == "CODE_ERROR"
        assert remediation.context["trigger_failure_taxonomy"] == {"kind": "import"}
        # Fingerprint propagates from the parent investigation context so that
        # _record_remediation_outcome can set last_remediation_fingerprint and
        # sibling/follow-up logic can key off the same fingerprint.
        assert remediation.context["diagnosis_fingerprint"] == "fp-A"

    @pytest.mark.asyncio
    async def test_remediation_propagates_error_fingerprint_fallback(self, db_session):
        """When parent has only error_fingerprint (no diagnosis_fingerprint), that
        value is propagated onto the spawned remediation so follow-up logic can
        still key off a stable fingerprint."""
        project = make_project(directions=[{
            "id": "dir-1", "status": "active", "text": "demo",
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        parent = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"direction_id": "dir-1", "error_fingerprint": "ef-B"},
            payloads=[{"type": "investigate", "prompt": "x"}],
        )
        db_session.add(parent)
        await db_session.commit()
        await db_session.refresh(parent)

        from app.task_completion import _spawn_investigation_remediation
        remediation = await _spawn_investigation_remediation(
            task=parent,
            direction_id="dir-1",
            summary="s", root_cause="rc", recommended_fix="rf",
            severity="medium", confidence="high",
            affected_paths=["a.py"], remediation_attempts=0,
            session=db_session,
        )
        assert remediation is not None
        # error_fingerprint now also seeds diagnosis_fingerprint so downstream
        # dedup / sibling-propagation logic can key off a stable scalar even
        # when the investigation context only set error_fingerprint.
        assert remediation.context["error_fingerprint"] == "ef-B"
        assert remediation.context["diagnosis_fingerprint"] == "ef-B"

    @pytest.mark.asyncio
    async def test_remediation_fingerprint_from_failure_fingerprints(self, db_session):
        """When parent has only failure_fingerprints list, the first element seeds
        both error_fingerprint and diagnosis_fingerprint on the remediation."""
        project = make_project(directions=[{
            "id": "dir-1", "status": "active", "text": "demo",
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        parent = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"direction_id": "dir-1", "failure_fingerprints": ["fp-C", "fp-D"]},
            payloads=[{"type": "investigate", "prompt": "x"}],
        )
        db_session.add(parent)
        await db_session.commit()
        await db_session.refresh(parent)

        from app.task_completion import _spawn_investigation_remediation
        remediation = await _spawn_investigation_remediation(
            task=parent,
            direction_id="dir-1",
            summary="s", root_cause="rc", recommended_fix="rf",
            severity="medium", confidence="high",
            affected_paths=["a.py"], remediation_attempts=0,
            session=db_session,
        )
        assert remediation is not None
        assert remediation.context["error_fingerprint"] == "fp-C"
        assert remediation.context["diagnosis_fingerprint"] == "fp-C"

    @pytest.mark.asyncio
    async def test_follow_up_prompt_includes_earlier_attempts(self, db_session):
        """Prior remediation_history entries surface in the follow-up prompt."""
        prior_history = [
            {
                "task_id": "old-task-1",
                "result": "failed",
                "failure_class": "CODE_ERROR",
                "error_summary": "AttributeError on direction.locked",
                "prompt_snippet": "add .locked attr to direction dict",
                "timestamp": "2026-04-10T00:00:00",
            },
            {
                "task_id": "old-task-2",
                "result": "failed",
                "failure_class": "TEST_FAILURE",
                "error_summary": "test_drain_idempotent regressed",
                "prompt_snippet": "gate drain behind is_locked helper",
                "timestamp": "2026-04-12T00:00:00",
            },
        ]
        project = make_project(directions=[{
            "id": "dir-1", "status": "active",
            "text": "rollup reliability",
            "remediation_history": list(prior_history),
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_remediation_task(
            project.id,
            prompt="attempt 3: acquire lock before drain",
            latest_status_message="still racing on drain",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()

        from sqlmodel import select as _select
        from app.models import Task as TaskModel
        spawned = (await db_session.execute(
            _select(TaskModel).where(
                TaskModel.project_id == project.id,
                TaskModel.id != task.id,
            )
        )).scalars().first()
        assert spawned is not None
        prompt_text = spawned.payloads[0]["prompt"]
        assert "Earlier remediation attempts" in prompt_text
        assert "add .locked attr to direction dict" in prompt_text
        assert "gate drain behind is_locked helper" in prompt_text
        assert "TEST_FAILURE" in prompt_text
        # The current (most recent) attempt is surfaced separately and must not be
        # duplicated in the earlier-attempts block.
        assert prompt_text.count("attempt 3: acquire lock before drain") == 1

    @pytest.mark.asyncio
    async def test_merged_remediation_does_not_spawn_followup(self, db_session):
        """A completed remediation must not trigger a follow-up investigation.
        For PR-creating types, outcome recording is deferred until the review
        pipeline resolves, so last_remediation_result stays unset here."""
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_remediation_task(project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.COMPLETED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert "followup_investigation_attempts_by_fingerprint" not in d
        assert d.get("diagnosis_in_progress") is not True

        from sqlmodel import select as _select
        from app.models import Task as TaskModel
        all_tasks = (await db_session.execute(
            _select(TaskModel).where(TaskModel.project_id == project.id)
        )).scalars().all()
        assert len(all_tasks) == 1

    @pytest.mark.asyncio
    async def test_follow_up_uses_no_fingerprint_key_when_absent(self, db_session):
        """Missing fingerprint still caps follow-ups under a sentinel key."""
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        # Task without diagnosis_fingerprint in context
        task = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            latest_status_message="boom",
            context={"diagnosis_task_id": "diag-1", "direction_id": "dir-1"},
            payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        fp_entry = d["followup_investigation_attempts_by_fingerprint"]["no_fingerprint"]
        assert isinstance(fp_entry, list) and len(fp_entry) == 1

    @pytest.mark.asyncio
    async def test_follow_up_allowed_when_prior_attempts_expired(self, db_session):
        """Attempts older than the decay window do not count toward the cap."""
        from datetime import timedelta
        from app.time_utils import _utcnow
        from app.task_completion import _FOLLOWUP_INVESTIGATION_DECAY_DAYS
        stale = (_utcnow() - timedelta(days=_FOLLOWUP_INVESTIGATION_DECAY_DAYS + 1)).isoformat()
        project = make_project(directions=[{
            "id": "dir-1", "status": "active",
            "followup_investigation_attempts_by_fingerprint": {"fp-A": [stale, stale]},
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_remediation_task(project.id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        fp_entry = d["followup_investigation_attempts_by_fingerprint"]["fp-A"]
        # Expired timestamps pruned; new attempt appended — single fresh entry remains
        assert len(fp_entry) == 1
        assert fp_entry[0] != stale
        assert d["diagnosis_in_progress"] is True

    @pytest.mark.asyncio
    async def test_follow_up_skipped_when_recent_attempts_still_cap(self, db_session):
        """Recent attempts (within the decay window) continue to block the cap."""
        from datetime import timedelta
        from app.time_utils import _utcnow
        from app.task_completion import _FOLLOWUP_INVESTIGATION_DECAY_DAYS
        recent = (_utcnow() - timedelta(days=_FOLLOWUP_INVESTIGATION_DECAY_DAYS - 1)).isoformat()
        project = make_project(directions=[{
            "id": "dir-1", "status": "active",
            "followup_investigation_attempts_by_fingerprint": {"fp-A": [recent, recent]},
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_remediation_task(project.id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert d["followup_investigation_attempts_by_fingerprint"] == {"fp-A": [recent, recent]}
        assert d.get("diagnosis_in_progress") is not True

    @pytest.mark.asyncio
    async def test_follow_up_skipped_when_cap_reached_across_other_directions(self, db_session):
        """Cross-direction: if siblings already hit the project-wide cap for this
        fingerprint, a new direction does not get its own per-direction quota."""
        from app.time_utils import _utcnow
        recent = _utcnow().isoformat()
        project = make_project(directions=[
            {"id": "dir-other", "status": "active",
             "followup_investigation_attempts_by_fingerprint": {"fp-A": [recent, recent]}},
            {"id": "dir-1", "status": "active", "text": "fresh direction"},
        ])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_remediation_task(project.id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        target = next(d for d in project.directions if d["id"] == "dir-1")
        assert "followup_investigation_attempts_by_fingerprint" not in target
        assert target.get("diagnosis_in_progress") is not True

    @pytest.mark.asyncio
    async def test_follow_up_legacy_int_entry_treated_as_expired(self, db_session):
        """Pre-decay int entries (e.g. 2) no longer permanently block recovery."""
        project = make_project(directions=[{
            "id": "dir-1", "status": "active",
            "followup_investigation_attempts_by_fingerprint": {"fp-A": 2},
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_remediation_task(project.id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        fp_entry = d["followup_investigation_attempts_by_fingerprint"]["fp-A"]
        assert isinstance(fp_entry, list) and len(fp_entry) == 1
        assert d["diagnosis_in_progress"] is True

    @pytest.mark.asyncio
    async def test_cap_exhausted_all_failed_escalates(self, db_session, mocker):
        """Cap hit with all prior remediations failed flags needs_manual_intervention + posts."""
        from app.time_utils import _utcnow
        post_mock = mocker.patch(
            "app.notification_dispatch.post_remediation_escalation_message",
            new_callable=AsyncMock,
        )
        recent_ts = [_utcnow().isoformat(), _utcnow().isoformat()]
        project = make_project(directions=[{
            "id": "dir-1", "status": "active", "text": "rollup reliability",
            "followup_investigation_attempts_by_fingerprint": {"fp-A": list(recent_ts)},
            "remediation_history": [
                {"task_id": "old-1", "result": "failed", "fingerprint": "fp-A",
                 "timestamp": "2026-04-10T00:00:00"},
                {"task_id": "old-2", "result": "failed", "fingerprint": "fp-A",
                 "timestamp": "2026-04-12T00:00:00"},
            ],
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_remediation_task(project.id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert d.get("needs_manual_intervention") is True
        assert post_mock.await_count == 1
        call_kwargs = post_mock.await_args.kwargs
        assert call_kwargs["direction_text"] == "rollup reliability"
        # Cap state unchanged; no follow-up spawned
        assert d["followup_investigation_attempts_by_fingerprint"] == {"fp-A": recent_ts}
        assert d.get("diagnosis_in_progress") is not True

    @pytest.mark.asyncio
    async def test_cap_exhausted_with_prior_merged_does_not_escalate(self, db_session, mocker):
        """A prior merged remediation on this fingerprint indicates partial success — no escalation."""
        from app.time_utils import _utcnow
        post_mock = mocker.patch(
            "app.notification_dispatch.post_remediation_escalation_message",
            new_callable=AsyncMock,
        )
        recent_ts = [_utcnow().isoformat(), _utcnow().isoformat()]
        project = make_project(directions=[{
            "id": "dir-1", "status": "active",
            "followup_investigation_attempts_by_fingerprint": {"fp-A": list(recent_ts)},
            "remediation_history": [
                {"task_id": "old-1", "result": "merged", "fingerprint": "fp-A",
                 "timestamp": "2026-04-10T00:00:00"},
                {"task_id": "old-2", "result": "failed", "fingerprint": "fp-A",
                 "timestamp": "2026-04-12T00:00:00"},
            ],
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_remediation_task(project.id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert d.get("needs_manual_intervention") is not True
        assert post_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_cap_exhausted_idempotent_when_already_flagged(self, db_session, mocker):
        """When needs_manual_intervention is already set, do not re-post the escalation."""
        from app.time_utils import _utcnow
        post_mock = mocker.patch(
            "app.notification_dispatch.post_remediation_escalation_message",
            new_callable=AsyncMock,
        )
        recent_ts = [_utcnow().isoformat(), _utcnow().isoformat()]
        project = make_project(directions=[{
            "id": "dir-1", "status": "active",
            "needs_manual_intervention": True,
            "followup_investigation_attempts_by_fingerprint": {"fp-A": list(recent_ts)},
            "remediation_history": [
                {"task_id": "old-1", "result": "failed", "fingerprint": "fp-A",
                 "timestamp": "2026-04-10T00:00:00"},
            ],
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_remediation_task(project.id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        assert post_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_follow_up_skipped_by_cross_direction_fingerprint(self, db_session):
        """A recent investigate task on a sibling direction sharing the same
        fingerprint blocks a new follow-up and records a dedup note."""
        project = make_project(directions=[
            {"id": "dir-1", "status": "active"},
            {"id": "dir-2", "status": "paused"},
        ])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        # Sibling direction already has an open investigate with same fingerprint
        sibling_inv = make_task(
            project_id=project.id,
            payloads=[{"type": "investigate", "prompt": "sibling", "base_branch": "main"}],
            context={"direction_id": "dir-2", "diagnosis_fingerprint": "fp-A"},
        )
        db_session.add(sibling_inv)
        await db_session.commit()
        task = self._make_remediation_task(project.id, direction_id="dir-1",
                                           fingerprint="fp-A")
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _record_remediation_if_terminal(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = next(x for x in project.directions if x["id"] == "dir-1")
        assert "followup_investigation_attempts_by_fingerprint" not in d
        assert d.get("diagnosis_in_progress") is not True
        notes = d.get("notes") or []
        assert any(n.get("source") == "followup_dedup" for n in notes)
        assert any(
            n.get("shared_investigation_task_id") == str(sibling_inv.id)
            for n in notes
        )


# ── _handle_investigation_completion ────────────────────────────────────────

class TestIsStructuralDiagnosis:
    def test_detects_scope_too_broad(self):
        assert _is_structural_diagnosis("the direction is too broad", "rethink")

    def test_detects_misaligned(self):
        assert _is_structural_diagnosis("Fundamentally misaligned with codebase", "")

    def test_actionable_root_cause_is_not_structural(self):
        assert not _is_structural_diagnosis(
            "missing null check in parse_request", "add guard clause at line 42"
        )

    def test_case_insensitive(self):
        assert _is_structural_diagnosis("Direction Scope is unclear", "")

    def test_scope_word_in_code_context_is_not_structural(self):
        assert not _is_structural_diagnosis(
            "The validation scope too narrowly checks input types", ""
        )
        assert not _is_structural_diagnosis(
            "This import is out of scope for this module", ""
        )

    def test_narrow_the_scope_of_code_is_not_structural(self):
        assert not _is_structural_diagnosis(
            "lock contention in reaper",
            "narrow the scope of the test fixture to only include active tasks",
        )

    def test_narrow_the_scope_with_direction_context_is_structural(self):
        assert _is_structural_diagnosis(
            "the direction scope is unclear",
            "narrow the scope to a single failure class",
        )


class TestHandleInvestigationCompletion:
    def _payload(self, logs=""):
        from types import SimpleNamespace
        from app.models import ArtifactType
        return SimpleNamespace(logs=logs, artifact_type=ArtifactType.INVESTIGATION_REPORT)

    def _report(self, **overrides) -> str:
        base = {
            "summary": "Tasks fail due to missing lock in _record_direction_result",
            "root_cause": "Concurrent writers race on the direction dict, causing one writer's update to overwrite the other. The bug is in shared_state.record_direction_result which reads, mutates, and writes without a lock.",
            "affected_files": [
                {"path": "backend/app/shared_state.py", "issue": "missing lock around direction dict write"},
                {"path": "backend/app/shared_state.py", "issue": "dup path"},
                {"path": "backend/app/autonomous_research.py", "issue": "calls the unsafe function"},
            ],
            "recommended_fix": "Wrap record_direction_result body in the existing asyncio.Lock held for per-project state. Add a targeted regression test that exercises concurrent calls.",
            "severity": "high",
            "confidence": "high",
        }
        base.update(overrides)
        return json.dumps(base)

    async def _setup(self, db_session, mocker, direction_overrides=None):
        mocker.patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock)
        direction = {
            "id": "dir-invest-1",
            "text": "Improve pipeline reliability via targeted tests",
            "status": "paused",
            "diagnosis_in_progress": True,
            "diagnosis_started_at": "2026-04-17T00:00:00",
        }
        if direction_overrides:
            direction.update(direction_overrides)
        project = make_project(directions=[direction])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        return project

    @pytest.mark.asyncio
    async def test_parses_report_and_stores_on_plan_output(self, db_session, mocker):
        project = await self._setup(db_session, mocker)
        task = make_task(
            project_id=project.id,
            context={"direction_id": "dir-invest-1"},
        )
        db_session.add(task)
        await db_session.commit()

        await _handle_investigation_completion(task, self._payload(self._report()), db_session)
        await db_session.commit()
        await db_session.refresh(task)

        assert task.status == TaskStatus.COMPLETED
        assert isinstance(task.plan_output, dict)
        assert task.plan_output["root_cause"].startswith("Concurrent writers")

    @pytest.mark.asyncio
    async def test_stamps_direction_state_and_clears_flag(self, db_session, mocker):
        project = await self._setup(db_session, mocker)
        task = make_task(
            project_id=project.id,
            context={"direction_id": "dir-invest-1"},
        )
        db_session.add(task)
        await db_session.commit()

        await _handle_investigation_completion(task, self._payload(self._report()), db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert "diagnosis_completed_at" in d
        assert d["diagnosis_summary"].startswith("Tasks fail due to missing lock")
        assert "backend/app/shared_state.py" in d["diagnosed_affected_files"]
        assert d["diagnosed_affected_files"].count("backend/app/shared_state.py") == 1
        assert "diagnosis_in_progress" not in d
        assert "diagnosis_started_at" not in d

    @pytest.mark.asyncio
    async def test_appends_diagnosis_to_research_context(self, db_session, mocker):
        project = await self._setup(db_session, mocker)
        task = make_task(
            project_id=project.id,
            context={"direction_id": "dir-invest-1"},
        )
        db_session.add(task)
        await db_session.commit()

        await _handle_investigation_completion(task, self._payload(self._report()), db_session)
        await db_session.commit()
        await db_session.refresh(project)

        ctx = project.directions[0].get("research_context") or ""
        assert "Last diagnosis" in ctx
        assert "Diagnosed affected files" in ctx
        assert "backend/app/shared_state.py" in ctx

    @pytest.mark.asyncio
    async def test_spawns_remediation_on_high_confidence(self, db_session, mocker):
        project = await self._setup(db_session, mocker)
        task = make_task(
            project_id=project.id,
            context={"direction_id": "dir-invest-1"},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _handle_investigation_completion(task, self._payload(self._report()), db_session)
        await db_session.commit()
        await db_session.refresh(project)

        from sqlmodel import select as _select
        from app.models import Task as _Task
        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == task.id)
        )
        children = result.scalars().all()
        assert len(children) == 1
        child = children[0]
        assert child.first_payload["type"] == "create"
        assert child.first_payload["base_branch"] == "main"
        assert child.context["diagnosis_task_id"] == str(task.id)
        assert child.context["diagnosed_direction_id"] == "dir-invest-1"
        assert child.context["direction_id"] == "dir-invest-1"
        assert child.context["diagnosis_confidence"] == "high"
        assert "backend/app/shared_state.py" in child.context["related_files"]
        assert child.status == TaskStatus.QUEUED
        assert project.directions[0]["remediation_task_id"] == str(child.id)

    @pytest.mark.asyncio
    async def test_spawns_remediation_on_medium_confidence(self, db_session, mocker):
        project = await self._setup(db_session, mocker)
        task = make_task(
            project_id=project.id,
            context={"direction_id": "dir-invest-1"},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _handle_investigation_completion(
            task, self._payload(self._report(confidence="medium")), db_session
        )
        await db_session.commit()
        await db_session.refresh(project)

        from sqlmodel import select as _select
        from app.models import Task as _Task
        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == task.id)
        )
        children = result.scalars().all()
        assert len(children) == 1
        child = children[0]
        assert child.context["diagnosis_confidence"] == "medium"
        assert child.context["direction_id"] == "dir-invest-1"
        assert project.directions[0]["remediation_task_id"] == str(child.id)

    @pytest.mark.asyncio
    async def test_skips_spawn_for_structural_root_cause(self, db_session, mocker):
        project = await self._setup(db_session, mocker)
        task = make_task(
            project_id=project.id,
            context={"direction_id": "dir-invest-1"},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        structural_report = self._report(
            root_cause="The direction scope is too broad — it conflates two unrelated concerns.",
            recommended_fix="Split the direction into two narrower directions.",
        )
        await _handle_investigation_completion(task, self._payload(structural_report), db_session)
        await db_session.commit()
        await db_session.refresh(project)

        from sqlmodel import select as _select
        from app.models import Task as _Task
        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == task.id)
        )
        assert result.scalars().all() == []

        d = project.directions[0]
        notes = d.get("notes") or []
        assert len(notes) == 1
        assert "structurally misaligned" in notes[0]["text"]
        assert notes[0]["source"] == "investigation_diagnosis"
        assert "remediation_task_id" not in d

    @pytest.mark.asyncio
    async def test_low_confidence_spawns_followup_investigation(self, db_session, mocker):
        """Low-confidence diagnosis spawns a follow-up investigate task, not a remediation."""
        project = await self._setup(
            db_session, mocker,
            direction_overrides={"error_fingerprint": "fp-low"},
        )
        task = make_task(
            project_id=project.id,
            context={"direction_id": "dir-invest-1", "diagnosis_fingerprint": "fp-low"},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _handle_investigation_completion(
            task, self._payload(self._report(confidence="low")), db_session
        )
        await db_session.commit()
        await db_session.refresh(project)

        from sqlmodel import select as _select
        from app.models import Task as _Task
        # No remediation create-task child
        children = (await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == task.id)
        )).scalars().all()
        assert children == []
        # But a follow-up investigate task exists (sibling, not child)
        siblings = (await db_session.execute(
            _select(_Task).where(
                _Task.project_id == project.id,
                _Task.id != task.id,
            )
        )).scalars().all()
        assert len(siblings) == 1
        followup = siblings[0]
        assert followup.payloads[0]["type"] == "investigate"
        assert followup.context["diagnosis_trigger"] == "low_confidence_followup"
        assert followup.context["previous_investigation_task_id"] == str(task.id)
        assert followup.context["original_diagnosis_task_id"] == str(task.id)
        assert followup.context["diagnosis_fingerprint"] == "fp-low"
        assert "LOW-confidence" in followup.payloads[0]["prompt"]

        d = project.directions[0]
        assert "remediation_task_id" not in d
        # diagnosis flag re-set because the follow-up is in progress
        assert d.get("diagnosis_in_progress") is True
        assert "diagnosis_started_at" in d
        assert "diagnosis_completed_at" in d
        fp_entry = d["followup_investigation_attempts_by_fingerprint"]["fp-low"]
        assert isinstance(fp_entry, list) and len(fp_entry) == 1

    @pytest.mark.asyncio
    async def test_low_confidence_followup_respects_fingerprint_cap(self, db_session, mocker):
        """Cap on prior follow-up attempts blocks spawning on low confidence."""
        from app.time_utils import _utcnow
        recent = [_utcnow().isoformat(), _utcnow().isoformat()]
        project = await self._setup(
            db_session, mocker,
            direction_overrides={
                "error_fingerprint": "fp-low",
                "followup_investigation_attempts_by_fingerprint": {"fp-low": list(recent)},
            },
        )
        task = make_task(
            project_id=project.id,
            context={"direction_id": "dir-invest-1", "diagnosis_fingerprint": "fp-low"},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _handle_investigation_completion(
            task, self._payload(self._report(confidence="low")), db_session
        )
        await db_session.commit()
        await db_session.refresh(project)

        from sqlmodel import select as _select
        from app.models import Task as _Task
        siblings = (await db_session.execute(
            _select(_Task).where(
                _Task.project_id == project.id,
                _Task.id != task.id,
            )
        )).scalars().all()
        assert siblings == []
        d = project.directions[0]
        assert d["followup_investigation_attempts_by_fingerprint"] == {"fp-low": recent}
        assert "diagnosis_in_progress" not in d

    @pytest.mark.asyncio
    async def test_malformed_json_falls_back_gracefully(self, db_session, mocker):
        project = await self._setup(db_session, mocker)
        task = make_task(
            project_id=project.id,
            context={"direction_id": "dir-invest-1"},
        )
        db_session.add(task)
        await db_session.commit()

        bad_logs = "this is not json at all"
        await _handle_investigation_completion(task, self._payload(bad_logs), db_session)
        await db_session.commit()
        await db_session.refresh(task)
        await db_session.refresh(project)

        assert task.status == TaskStatus.COMPLETED
        assert task.plan_output == {"raw": bad_logs}
        d = project.directions[0]
        assert "diagnosis_in_progress" not in d
        assert "diagnosis_completed_at" in d

    @pytest.mark.asyncio
    async def test_handles_missing_direction_gracefully(self, db_session, mocker):
        """Task without a direction_id context still completes cleanly."""
        mocker.patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock)
        project = make_project(directions=[])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, context={})
        db_session.add(task)
        await db_session.commit()

        await _handle_investigation_completion(task, self._payload(self._report()), db_session)
        await db_session.commit()
        await db_session.refresh(task)

        assert task.status == TaskStatus.COMPLETED
        assert isinstance(task.plan_output, dict)

    @pytest.mark.asyncio
    async def test_empty_logs_completes_without_raising(self, db_session, mocker):
        mocker.patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock)
        project = make_project(directions=[{"id": "d-1", "diagnosis_in_progress": True}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, context={"direction_id": "d-1"})
        db_session.add(task)
        await db_session.commit()

        await _handle_investigation_completion(task, self._payload(""), db_session)
        await db_session.commit()
        await db_session.refresh(task)
        await db_session.refresh(project)

        assert task.status == TaskStatus.COMPLETED
        assert task.plan_output == {}
        assert "diagnosis_in_progress" not in project.directions[0]


# ── _clear_direction_diagnosis_flag ─────────────────────────────────────────

class TestClearDirectionDiagnosisFlag:
    def _direction(self, **overrides):
        d = {
            "id": "dir-1",
            "diagnosis_in_progress": True,
            "diagnosis_started_at": "2026-04-17T00:00:00",
            "diagnosis_task_id": "task-xyz",
        }
        d.update(overrides)
        return d

    @pytest.mark.asyncio
    async def test_clears_flags_and_stores_summary(self, db_session):
        project = make_project(directions=[self._direction()])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, context={"direction_id": "dir-1"})
        db_session.add(task)
        await db_session.commit()

        await _clear_direction_diagnosis_flag(task, db_session, "partial summary")
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert "diagnosis_in_progress" not in d
        assert "diagnosis_started_at" not in d
        assert "diagnosis_task_id" not in d
        assert "diagnosis_completed_at" in d
        assert d["diagnosis_summary"] == "partial summary"

    @pytest.mark.asyncio
    async def test_no_direction_id_in_context_is_noop(self, db_session):
        project = make_project(directions=[self._direction()])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, context={})
        db_session.add(task)
        await db_session.commit()

        await _clear_direction_diagnosis_flag(task, db_session, "summary")
        await db_session.commit()
        await db_session.refresh(project)

        assert project.directions[0]["diagnosis_in_progress"] is True
        assert "diagnosis_completed_at" not in project.directions[0]

    @pytest.mark.asyncio
    async def test_project_not_found_is_noop(self, db_session):
        import uuid
        task = make_task(project_id=uuid.uuid4(), context={"direction_id": "dir-1"})
        db_session.add(task)
        await db_session.commit()

        await _clear_direction_diagnosis_flag(task, db_session, "summary")

    @pytest.mark.asyncio
    async def test_direction_id_not_matching_is_noop(self, db_session):
        project = make_project(directions=[self._direction()])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, context={"direction_id": "dir-nope"})
        db_session.add(task)
        await db_session.commit()

        await _clear_direction_diagnosis_flag(task, db_session, "summary")
        await db_session.commit()
        await db_session.refresh(project)

        assert project.directions[0]["diagnosis_in_progress"] is True
        assert "diagnosis_completed_at" not in project.directions[0]

    @pytest.mark.asyncio
    async def test_summary_truncated_to_max(self, db_session):
        project = make_project(directions=[self._direction()])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, context={"direction_id": "dir-1"})
        db_session.add(task)
        await db_session.commit()

        long_summary = "x" * (STRATEGIC_RATIONALE_MAX + 500)
        await _clear_direction_diagnosis_flag(task, db_session, long_summary)
        await db_session.commit()
        await db_session.refresh(project)

        assert len(project.directions[0]["diagnosis_summary"]) == STRATEGIC_RATIONALE_MAX

    @pytest.mark.asyncio
    async def test_none_summary_omits_key(self, db_session):
        project = make_project(directions=[self._direction()])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(project_id=project.id, context={"direction_id": "dir-1"})
        db_session.add(task)
        await db_session.commit()

        await _clear_direction_diagnosis_flag(task, db_session, None)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert "diagnosis_summary" not in d
        assert "diagnosis_completed_at" in d
        assert "diagnosis_in_progress" not in d


# ── _calculate_remediation_scope ────────────────────────────────────────────

class TestCalculateRemediationScope:
    def test_small_when_few_paths_and_low_severity(self):
        assert _calculate_remediation_scope(["a.py", "b.py"], "low", 0) == "small"

    def test_small_when_few_paths_and_medium_severity(self):
        assert _calculate_remediation_scope(["a.py"], "medium", 0) == "small"

    def test_small_when_three_paths_high_severity(self):
        """<=4 paths route to small regardless of severity."""
        paths = ["a.py", "b.py", "c.py"]
        assert _calculate_remediation_scope(paths, "high", 0) == "small"

    def test_small_when_four_paths_medium_severity(self):
        paths = ["a.py", "b.py", "c.py", "d.py"]
        assert _calculate_remediation_scope(paths, "medium", 0) == "small"

    def test_small_when_five_paths_high_severity(self):
        """Medium tier eliminated — 5 paths routes to small (merge rate ~100%)."""
        paths = ["a.py", "b.py", "c.py", "d.py", "e.py"]
        assert _calculate_remediation_scope(paths, "high", 0) == "small"

    def test_large_when_six_paths(self):
        paths = [f"f{i}.py" for i in range(6)]
        assert _calculate_remediation_scope(paths, "medium", 0) == "large"

    def test_small_when_few_paths_critical_severity(self):
        """Critical with <=4 files still routes to small; severity no longer escalates."""
        assert _calculate_remediation_scope(["a.py"], "critical", 0) == "small"

    def test_empty_paths_low_severity_is_small(self):
        assert _calculate_remediation_scope([], "low", 0) == "small"

    def test_retry_does_not_escalate_small(self):
        """Retries keep the same scope — medium-scope remediations fail at 0%."""
        assert _calculate_remediation_scope(["a.py"], "low", 1) == "small"

    def test_retry_does_not_escalate_five_paths(self):
        paths = ["a.py", "b.py", "c.py", "d.py", "e.py"]
        assert _calculate_remediation_scope(paths, "medium", 1) == "small"

    def test_large_stays_large_after_retry(self):
        paths = [f"f{i}.py" for i in range(6)]
        assert _calculate_remediation_scope(paths, "medium", 5) == "large"

    def test_initial_attempt_does_not_bump(self):
        assert _calculate_remediation_scope(["a.py"], "low", 0) == "small"


class TestRemediationScopeInPayload:
    """Integration: _handle_investigation_completion propagates scope into payload."""

    def _payload(self, logs=""):
        from types import SimpleNamespace
        from app.models import ArtifactType
        return SimpleNamespace(logs=logs, artifact_type=ArtifactType.INVESTIGATION_REPORT)

    def _report(self, **overrides) -> str:
        base = {
            "summary": "Missing lock causing race condition",
            "root_cause": "Concurrent writers race on a shared dict without a lock.",
            "affected_files": [{"path": "backend/app/shared_state.py", "issue": "missing lock"}],
            "recommended_fix": "Wrap the mutation in the existing asyncio.Lock.",
            "severity": "medium",
            "confidence": "high",
        }
        base.update(overrides)
        return json.dumps(base)

    async def _setup(self, db_session, mocker, direction_overrides=None):
        mocker.patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock)
        direction = {
            "id": "dir-scope-1",
            "text": "Improve pipeline reliability",
            "status": "paused",
            "diagnosis_in_progress": True,
        }
        if direction_overrides:
            direction.update(direction_overrides)
        project = make_project(directions=[direction])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        return project

    async def _spawned_child(self, db_session, parent_task_id):
        from sqlmodel import select as _select
        from app.models import Task as _Task
        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == parent_task_id)
        )
        return result.scalars().first()

    @pytest.mark.asyncio
    async def test_small_scope_for_simple_diagnosis(self, db_session, mocker):
        project = await self._setup(db_session, mocker)
        task = make_task(project_id=project.id, context={"direction_id": "dir-scope-1"})
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _handle_investigation_completion(task, self._payload(self._report()), db_session)
        await db_session.commit()
        child = await self._spawned_child(db_session, task.id)
        assert child is not None
        assert child.context["scope"] == "small"

    @pytest.mark.asyncio
    async def test_large_scope_for_many_files(self, db_session, mocker):
        project = await self._setup(db_session, mocker)
        task = make_task(project_id=project.id, context={"direction_id": "dir-scope-1"})
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        affected = [{"path": f"backend/app/mod_{i}.py"} for i in range(6)]
        await _handle_investigation_completion(
            task, self._payload(self._report(affected_files=affected)), db_session,
        )
        await db_session.commit()
        child = await self._spawned_child(db_session, task.id)
        assert child is not None
        assert child.context["scope"] == "large"

    @pytest.mark.asyncio
    async def test_small_scope_for_five_file_diagnosis(self, db_session, mocker):
        """Medium tier eliminated — 5-file diagnoses route to small."""
        project = await self._setup(db_session, mocker)
        task = make_task(project_id=project.id, context={"direction_id": "dir-scope-1"})
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        affected = [{"path": f"backend/app/mod_{i}.py"} for i in range(5)]
        await _handle_investigation_completion(
            task, self._payload(self._report(affected_files=affected)), db_session,
        )
        await db_session.commit()
        child = await self._spawned_child(db_session, task.id)
        assert child is not None
        assert child.context["scope"] == "small"

    @pytest.mark.asyncio
    async def test_retries_do_not_escalate_scope(self, db_session, mocker):
        """Prior retry count should not escalate a small-scope diagnosis to medium."""
        project = await self._setup(
            db_session, mocker, direction_overrides={"remediation_attempts": 2},
        )
        task = make_task(project_id=project.id, context={"direction_id": "dir-scope-1"})
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _handle_investigation_completion(task, self._payload(self._report()), db_session)
        await db_session.commit()
        child = await self._spawned_child(db_session, task.id)
        assert child is not None
        assert child.context["scope"] == "small"


# ── _handle_investigation_terminal_failure ──────────────────────────────────

class TestHandleInvestigationTerminalFailure:
    """Clears diagnosis_in_progress on investigate-task FAILED/CANCELLED and
    optionally re-spawns investigation when the failure was infra-related."""

    @staticmethod
    def _make_investigation_task(project_id, *, direction_id="dir-1",
                                 fingerprint="fp-A", status=TaskStatus.FAILED,
                                 latest_status_message="VM failed to start: quota exceeded",
                                 extra_context=None):
        ctx = {
            "direction_id": direction_id,
            "diagnosis_trigger": "auto_pause_diagnosis",
        }
        if fingerprint:
            ctx["diagnosis_fingerprint"] = fingerprint
        if extra_context:
            ctx.update(extra_context)
        return make_task(
            project_id=project_id,
            status=status,
            latest_status_message=latest_status_message,
            context=ctx,
            payloads=[{"type": "investigate", "prompt": "diagnose", "base_branch": "main"}],
        )

    @pytest.mark.asyncio
    async def test_infra_failure_clears_flag_and_spawns_reinvestigation(self, db_session):
        """VM crash on an investigation clears diagnosis flag AND spawns a re-investigation."""
        project = make_project(directions=[{
            "id": "dir-1", "status": "paused", "text": "fix rollup reliability",
            "diagnosis_in_progress": True,
            "diagnosis_started_at": "2026-04-17T00:00:00",
            "diagnosis_task_id": "some-prior-id",
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_investigation_task(project.id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _handle_investigation_terminal_failure(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        # Re-investigation spawned — flag is re-set for the new task
        assert d.get("diagnosis_in_progress") is True
        assert "diagnosis_started_at" in d
        # Prior task id cleared (not restored by re-spawn — re-spawn stamps new started_at only)
        assert d.get("diagnosis_task_id") != "some-prior-id"
        # diagnosis_completed_at stamped for this terminal event
        assert "diagnosis_completed_at" in d
        # Re-investigation recorded against the fingerprint cap
        fp_entry = d["followup_investigation_attempts_by_fingerprint"]["fp-A"]
        assert isinstance(fp_entry, list) and len(fp_entry) == 1

        from sqlmodel import select as _select
        from app.models import Task as _Task
        siblings = (await db_session.execute(
            _select(_Task).where(
                _Task.project_id == project.id,
                _Task.id != task.id,
            )
        )).scalars().all()
        assert len(siblings) == 1
        followup = siblings[0]
        assert followup.payloads[0]["type"] == "investigate"
        assert followup.context["diagnosis_trigger"] == "investigation_failure_followup"
        assert followup.context["previous_investigation_task_id"] == str(task.id)
        assert followup.context["original_diagnosis_task_id"] == str(task.id)
        assert followup.context["direction_id"] == "dir-1"
        assert followup.context["diagnosis_fingerprint"] == "fp-A"

    @pytest.mark.asyncio
    async def test_cancelled_clears_flag_no_respawn(self, db_session):
        """CANCELLED status clears the flag but never spawns a re-investigation."""
        project = make_project(directions=[{
            "id": "dir-1", "status": "paused",
            "diagnosis_in_progress": True,
            "diagnosis_started_at": "2026-04-17T00:00:00",
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_investigation_task(
            project.id,
            status=TaskStatus.CANCELLED,
            latest_status_message="Cancelled: parent dependency failed",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _handle_investigation_terminal_failure(task, TaskStatus.CANCELLED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert "diagnosis_in_progress" not in d
        assert "diagnosis_started_at" not in d
        assert "diagnosis_completed_at" in d
        # No follow-up recorded
        assert "followup_investigation_attempts_by_fingerprint" not in d

        from sqlmodel import select as _select
        from app.models import Task as _Task
        siblings = (await db_session.execute(
            _select(_Task).where(
                _Task.project_id == project.id,
                _Task.id != task.id,
            )
        )).scalars().all()
        assert siblings == []

    @pytest.mark.asyncio
    async def test_code_error_clears_flag_no_respawn(self, db_session):
        """CODE_ERROR investigation failure clears the flag but does not re-spawn."""
        project = make_project(directions=[{
            "id": "dir-1", "status": "paused",
            "diagnosis_in_progress": True,
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        # A status message that classifies as CODE_ERROR (not infra)
        task = self._make_investigation_task(
            project.id,
            latest_status_message="Investigate agent returned malformed JSON — parse error",
            extra_context={"failure_category": "CODE_ERROR"},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _handle_investigation_terminal_failure(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert "diagnosis_in_progress" not in d
        assert "diagnosis_completed_at" in d
        assert "followup_investigation_attempts_by_fingerprint" not in d

        from sqlmodel import select as _select
        from app.models import Task as _Task
        siblings = (await db_session.execute(
            _select(_Task).where(
                _Task.project_id == project.id,
                _Task.id != task.id,
            )
        )).scalars().all()
        assert siblings == []

    @pytest.mark.asyncio
    async def test_salvages_partial_diagnosis_on_failure(self, db_session):
        """A failed investigate task with partial plan_output stamps partial_* on direction."""
        project = make_project(directions=[{
            "id": "dir-1", "status": "paused",
            "diagnosis_in_progress": True,
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_investigation_task(project.id)
        task.plan_output = {
            "summary": "token parser dies on empty input",
            "root_cause": "null deref in _parse_token",
            "affected_files": [{"path": "app/x.py", "issue": "deref"}, "app/y.py"],
        }
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _handle_investigation_terminal_failure(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert d["partial_diagnosis_summary"] == "token parser dies on empty input"
        assert "null deref" in d["partial_diagnosis_root_cause"]
        assert d["partial_diagnosis_files"] == ["app/x.py", "app/y.py"]
        assert d["partial_diagnosis_task_id"] == str(task.id)

    @pytest.mark.asyncio
    async def test_code_error_with_partial_root_cause_respawns(self, db_session):
        """CODE_ERROR crash is not normally retried, but partial root_cause unlocks re-spawn."""
        project = make_project(directions=[{
            "id": "dir-1", "status": "paused", "text": "rollup",
            "diagnosis_in_progress": True,
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_investigation_task(
            project.id,
            latest_status_message="Investigate agent returned malformed JSON — parse error",
            extra_context={"failure_category": "CODE_ERROR"},
        )
        task.plan_output = {
            "summary": "race in rollup cursor",
            "root_cause": "cursor advanced before ack",
            "affected_files": ["app/rollup_management.py"],
        }
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _handle_investigation_terminal_failure(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        from sqlmodel import select as _select
        from app.models import Task as _Task
        siblings = (await db_session.execute(
            _select(_Task).where(_Task.project_id == project.id, _Task.id != task.id)
        )).scalars().all()
        assert len(siblings) == 1
        followup = siblings[0]
        assert followup.context["diagnosis_trigger"] == "investigation_failure_followup"
        prompt = followup.payloads[0]["prompt"]
        assert "cursor advanced before ack" in prompt
        assert "rollup_management.py" in prompt

    def test_extract_partial_diagnosis_returns_none_when_empty(self):
        class _T:
            plan_output = None
        assert _extract_partial_diagnosis(_T()) is None
        _T.plan_output = {"summary": "", "root_cause": "", "affected_files": []}
        assert _extract_partial_diagnosis(_T()) is None

    @pytest.mark.asyncio
    async def test_non_investigate_task_is_noop(self, db_session):
        """A failing create/plan/etc task does not clear a direction diagnosis flag."""
        project = make_project(directions=[{
            "id": "dir-1", "status": "paused",
            "diagnosis_in_progress": True,
            "diagnosis_started_at": "2026-04-17T00:00:00",
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            context={"direction_id": "dir-1"},
            payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _handle_investigation_terminal_failure(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        # Flag untouched for non-investigate types
        assert d.get("diagnosis_in_progress") is True
        assert "diagnosis_completed_at" not in d

    @pytest.mark.asyncio
    async def test_completed_status_is_noop(self, db_session):
        """COMPLETED is handled by _handle_investigation_completion, not this path."""
        project = make_project(directions=[{
            "id": "dir-1", "status": "paused",
            "diagnosis_in_progress": True,
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_investigation_task(project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _handle_investigation_terminal_failure(task, TaskStatus.COMPLETED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        # Flag untouched — caller relies on _handle_investigation_completion
        assert d.get("diagnosis_in_progress") is True

    @pytest.mark.asyncio
    async def test_respawn_skipped_when_cap_reached(self, db_session):
        """Per-fingerprint cap blocks re-investigation but still clears the flag."""
        from app.time_utils import _utcnow
        recent = [_utcnow().isoformat(), _utcnow().isoformat()]
        project = make_project(directions=[{
            "id": "dir-1", "status": "paused",
            "diagnosis_in_progress": True,
            "followup_investigation_attempts_by_fingerprint": {"fp-A": list(recent)},
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_investigation_task(project.id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _handle_investigation_terminal_failure(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        # Flag cleared (no re-spawn re-set it)
        assert "diagnosis_in_progress" not in d
        assert "diagnosis_completed_at" in d
        # Cap unchanged
        assert d["followup_investigation_attempts_by_fingerprint"] == {"fp-A": recent}

        from sqlmodel import select as _select
        from app.models import Task as _Task
        siblings = (await db_session.execute(
            _select(_Task).where(
                _Task.project_id == project.id,
                _Task.id != task.id,
            )
        )).scalars().all()
        assert siblings == []

    @pytest.mark.asyncio
    async def test_respawn_skipped_when_recent_investigate_exists(self, db_session):
        """24h dedup: flag still cleared, but no re-investigation spawned."""
        project = make_project(directions=[{
            "id": "dir-1", "status": "paused",
            "diagnosis_in_progress": True,
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        # Another investigate task for this direction already exists in the last 24h
        sibling = make_task(
            project_id=project.id,
            payloads=[{"type": "investigate", "prompt": "p", "base_branch": "main"}],
            context={"direction_id": "dir-1"},
        )
        db_session.add(sibling)
        await db_session.commit()
        task = self._make_investigation_task(project.id)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _handle_investigation_terminal_failure(task, TaskStatus.FAILED, db_session)
        await db_session.commit()
        await db_session.refresh(project)

        d = project.directions[0]
        assert "diagnosis_in_progress" not in d
        assert "diagnosis_completed_at" in d
        assert "followup_investigation_attempts_by_fingerprint" not in d

        from sqlmodel import select as _select
        from app.models import Task as _Task
        all_investigate = (await db_session.execute(
            _select(_Task).where(
                _Task.project_id == project.id,
                _Task.id != task.id,
            )
        )).scalars().all()
        # Only the pre-existing sibling remains — no new task spawned
        assert len(all_investigate) == 1
        assert all_investigate[0].id == sibling.id

    @pytest.mark.asyncio
    async def test_missing_direction_id_is_noop(self, db_session):
        """Investigate task without direction_id in context is a safe no-op."""
        project = make_project(directions=[{"id": "dir-1", "status": "active"}])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            context={},
            payloads=[{"type": "investigate", "prompt": "x", "base_branch": "main"}],
        )
        db_session.add(task)
        await db_session.commit()

        # Must not raise
        await _handle_investigation_terminal_failure(task, TaskStatus.FAILED, db_session)

    @pytest.mark.asyncio
    async def test_swallows_inner_exception(self, db_session, mocker):
        """Errors from helpers must not propagate (mirrors _record_remediation_if_terminal)."""
        project = make_project(directions=[{
            "id": "dir-1", "status": "paused", "diagnosis_in_progress": True,
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = self._make_investigation_task(project.id)
        db_session.add(task)
        await db_session.commit()

        boom = mocker.patch(
            "app.task_completion._maybe_spawn_reinvestigation_after_failure",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db blew up"),
        )
        # Should swallow the exception and still return
        await _handle_investigation_terminal_failure(task, TaskStatus.FAILED, db_session)
        assert boom.await_count == 1

    @pytest.mark.asyncio
    async def test_reinvestigation_skipped_by_cross_direction_fingerprint(self, db_session):
        """Sibling direction with a recent investigate sharing the fingerprint
        blocks the reinvestigation spawn and records a dedup note."""
        project = make_project(directions=[
            {"id": "dir-1", "status": "paused", "text": "primary"},
            {"id": "dir-2", "status": "paused", "text": "sibling"},
        ])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        sibling_inv = make_task(
            project_id=project.id,
            payloads=[{"type": "investigate", "prompt": "sibling", "base_branch": "main"}],
            context={"direction_id": "dir-2", "diagnosis_fingerprint": "fp-A"},
        )
        db_session.add(sibling_inv)
        await db_session.commit()

        failed_inv = self._make_investigation_task(
            project.id, direction_id="dir-1", fingerprint="fp-A",
        )
        db_session.add(failed_inv)
        await db_session.commit()
        await db_session.refresh(failed_inv)

        target = next(d for d in project.directions if d["id"] == "dir-1")
        spawned = await _maybe_spawn_reinvestigation_after_failure(
            investigation_task=failed_inv,
            project=project,
            direction=target,
            session=db_session,
        )
        assert spawned is None
        assert "followup_investigation_attempts_by_fingerprint" not in target
        assert target.get("diagnosis_in_progress") is not True
        notes = target.get("notes") or []
        assert any(n.get("source") == "followup_dedup" for n in notes)
        assert any(
            n.get("shared_investigation_task_id") == str(sibling_inv.id)
            for n in notes
        )

    @pytest.mark.asyncio
    async def test_reinvestigation_cap_exhausted_escalates(self, db_session, mocker):
        """Cap hit on investigation-failure path flags needs_manual_intervention + posts."""
        from app.time_utils import _utcnow
        post_mock = mocker.patch(
            "app.notification_dispatch.post_remediation_escalation_message",
            new_callable=AsyncMock,
        )
        recent_ts = [_utcnow().isoformat(), _utcnow().isoformat()]
        project = make_project(directions=[{
            "id": "dir-1", "status": "paused", "text": "rollup reliability",
            "followup_investigation_attempts_by_fingerprint": {"fp-A": list(recent_ts)},
            "remediation_history": [
                {"task_id": "old-1", "result": "failed", "fingerprint": "fp-A",
                 "timestamp": "2026-04-10T00:00:00"},
            ],
        }])
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        failed_inv = self._make_investigation_task(
            project.id, direction_id="dir-1", fingerprint="fp-A",
        )
        db_session.add(failed_inv)
        await db_session.commit()
        await db_session.refresh(failed_inv)

        target = next(d for d in project.directions if d["id"] == "dir-1")
        spawned = await _maybe_spawn_reinvestigation_after_failure(
            investigation_task=failed_inv,
            project=project,
            direction=target,
            session=db_session,
        )
        await db_session.commit()
        await db_session.refresh(project)

        assert spawned is None
        d = project.directions[0]
        assert d.get("needs_manual_intervention") is True
        assert post_mock.await_count == 1
        assert d["followup_investigation_attempts_by_fingerprint"] == {"fp-A": recent_ts}


# ── _handle_plan_completion — scope-aware subtask effort ──────────────────────

class TestPlanSubtaskScopeAwareEffort:
    """Plan subtasks inherit effort from parent, but with a scope-aware floor."""

    def _plan_logs(self, subtasks):
        return json.dumps({"subtasks": subtasks})

    async def _setup_plan_task(self, db_session, mocker, parent_effort="medium"):
        mocker.patch("app.task_completion._ensure_branch", new_callable=AsyncMock, return_value=True)
        mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
        mocker.patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id,
            payloads=[{"type": "plan", "prompt": "plan it", "base_branch": "main", "effort": parent_effort}],
            status=TaskStatus.IN_PROGRESS,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        return task

    async def _child_tasks(self, db_session, parent_task):
        from sqlmodel import select as _select
        from app.models import Task as _Task
        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == parent_task.id)
        )
        return result.scalars().all()

    @pytest.mark.asyncio
    async def test_large_scope_subtask_raises_effort_above_parent(self, db_session, mocker):
        """Large-scope subtask under medium-effort parent is floored up to 'high'."""
        task = await self._setup_plan_task(db_session, mocker, parent_effort="medium")
        logs = self._plan_logs([
            {"title": "Sweeping refactor", "prompt": "Do big thing", "scope": "large"},
        ])
        await _handle_plan_completion(task, logs, db_session)
        children = await self._child_tasks(db_session, task)
        assert len(children) == 1
        assert children[0].payloads[0]["effort"] == "high"

    @pytest.mark.asyncio
    async def test_small_scope_subtask_keeps_parent_effort_when_higher(self, db_session, mocker):
        """Small-scope subtask under medium-effort parent keeps 'medium' (parent floor wins)."""
        task = await self._setup_plan_task(db_session, mocker, parent_effort="medium")
        logs = self._plan_logs([
            {"title": "Small tweak", "prompt": "Narrow fix", "scope": "small"},
        ])
        await _handle_plan_completion(task, logs, db_session)
        children = await self._child_tasks(db_session, task)
        assert len(children) == 1
        assert children[0].payloads[0]["effort"] == "medium"

    @pytest.mark.asyncio
    async def test_explicit_subtask_effort_max_beats_scope_floor(self, db_session, mocker):
        """Subtask-specified effort wins when it ranks higher than the scope floor."""
        task = await self._setup_plan_task(db_session, mocker, parent_effort="medium")
        logs = self._plan_logs([
            {"title": "Critical tiny change", "prompt": "p", "scope": "small", "effort": "max"},
        ])
        await _handle_plan_completion(task, logs, db_session)
        children = await self._child_tasks(db_session, task)
        assert len(children) == 1
        assert children[0].payloads[0]["effort"] == "max"



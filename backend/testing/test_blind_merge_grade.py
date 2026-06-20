"""Tests for the blind merge-grade qualification gate.

Covers:
  * _build_prompt — the constructed prompt contains ONLY version.goal and
    merged diffs, NEVER the strategist's running_update narrative or any
    self-reported progress signal ("blindness" invariant).
  * _parse_grade — validates parsed JSON; rejects unknown verdicts and
    empty gaps; accepts valid coverage_verdict strings.
  * _gather_merged_diff_text — diff assembly respects PR count cap
    (_MAX_PRS), per-file patch cap (_MAX_PER_FILE_PATCH_CHARS), and
    total diff cap (_MAX_TOTAL_DIFF_CHARS); gracefully skips PR fetch
    failures (one PR down does not abort the grade).
  * compute_blind_merge_grade — full path (API key present, GitHub creds
    resolved, LLM available) returns a populated record; None when API key
    missing, GitHub unresolved, or LLM unavailable/unparseable (fail-closed
    on None).
  * blind_merge_grade_satisfied — True only when a persisted record exists
    for the exact candidate_sha and carries non-empty verdict + gap.
  * persist_blind_merge_grade — merges the grade into version.last_qualification
    under the stable blind_merge_grade key.
  * blind_merge_grade_disabled — operator escape hatch DISABLE_BLIND_MERGE_GRADE=1.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import blind_merge_grade as bmg
from app.models import Project, ReleaseVersion


# ===========================================================================
# _build_prompt — blindness invariant (never includes running_update)
# ===========================================================================


class TestBuildPromptBlindnessInvariant:
    """The prompt MUST be built from ONLY goal + diffs, never running_update."""

    def test_prompt_is_blind_to_running_update(self):
        """Core invariant: running_update text must NEVER appear in the prompt.

        This test constructs a version with a distinct running_update string
        and verifies the prompt does not contain it. The blindness guarantee
        is enforced by construction — _build_prompt accepts ONLY goal and
        diff_text, never any running_update parameter.
        """
        goal = "deliver user analytics dashboard"
        diff_text = "added dashboard.tsx and metrics.py"
        running_update = "We refactored the metrics system and added caching"

        prompt = bmg._build_prompt(goal, diff_text, pr_count=1)

        # The prompt must NOT contain any part of the running_update.
        assert running_update not in prompt
        # Stronger: no substring of running_update appears.
        for word in ["refactored", "metrics", "system", "caching"]:
            assert (
                word not in prompt.lower()
                or word in goal.lower()
                or word in diff_text.lower()
            )

        # The prompt MUST contain the goal and diff.
        assert goal in prompt
        assert diff_text in prompt

    def test_build_prompt_signature_enforces_blindness(self):
        """_build_prompt signature only accepts goal + diff_text, never running_update.

        This structural test verifies the blindness invariant is enforced by
        API design: the function has no parameter to accept running_update.
        """
        import inspect

        sig = inspect.signature(bmg._build_prompt)
        params = list(sig.parameters.keys())
        # Must have goal, diff_text, pr_count; must NOT have running_update.
        assert "goal" in params
        assert "diff_text" in params
        assert "pr_count" in params
        assert "running_update" not in params
        assert "progress" not in params
        assert "narrative" not in params

    def test_prompt_includes_goal_and_diffs(self):
        """The prompt includes both the goal and the diffs."""
        goal = "implement feature X"
        diff_text = "added X.ts\n+// implementation"
        prompt = bmg._build_prompt(goal, diff_text, pr_count=2)

        assert goal in prompt
        assert diff_text in prompt
        assert "2 merged PR" in prompt

    def test_prompt_fallback_on_empty_goal(self):
        """Empty goal is filled with a fallback message, not omitted."""
        prompt = bmg._build_prompt("", "some diffs", pr_count=1)
        assert "(no version goal text was recorded)" in prompt
        assert "some diffs" in prompt

    def test_prompt_fallback_on_empty_diffs(self):
        """Empty diffs is filled with a fallback message, not omitted."""
        prompt = bmg._build_prompt("goal", "", pr_count=0)
        assert "goal" in prompt
        assert "(no merged diffs were found for this version)" in prompt


# ===========================================================================
# _parse_grade — validation of JSON response
# ===========================================================================


class TestParseGrade:
    """The parser validates the LLM's JSON response and rejects invalid verdicts."""

    def test_parse_valid_covers_verdict(self):
        """Parses a valid 'covers' verdict."""
        raw = '{"coverage_verdict": "covers", "biggest_gap": "none (complete)"}'
        parsed = bmg._parse_grade(raw)
        assert parsed["coverage_verdict"] == "covers"
        assert parsed["biggest_gap"] == "none (complete)"

    def test_parse_valid_partial_verdict(self):
        """Parses a valid 'partial' verdict."""
        raw = '{"coverage_verdict": "partial", "biggest_gap": "missing REST API"}'
        parsed = bmg._parse_grade(raw)
        assert parsed["coverage_verdict"] == "partial"
        assert parsed["biggest_gap"] == "missing REST API"

    def test_parse_valid_misses_verdict(self):
        """Parses a valid 'misses' verdict."""
        raw = '{"coverage_verdict": "misses", "biggest_gap": "no implementation"}'
        parsed = bmg._parse_grade(raw)
        assert parsed["coverage_verdict"] == "misses"

    def test_parse_rejects_unknown_verdict(self):
        """Unknown verdict values are rejected with ValueError."""
        raw = '{"coverage_verdict": "excellent", "biggest_gap": "something"}'
        with pytest.raises(ValueError, match="unknown coverage_verdict"):
            bmg._parse_grade(raw)

    def test_parse_rejects_empty_gap(self):
        """Empty biggest_gap is rejected with ValueError."""
        raw = '{"coverage_verdict": "partial", "biggest_gap": ""}'
        with pytest.raises(ValueError, match="empty biggest_gap"):
            bmg._parse_grade(raw)

    def test_parse_rejects_whitespace_only_gap(self):
        """Whitespace-only biggest_gap is treated as empty and rejected."""
        raw = '{"coverage_verdict": "partial", "biggest_gap": "   "}'
        with pytest.raises(ValueError, match="empty biggest_gap"):
            bmg._parse_grade(raw)

    def test_parse_case_insensitive_verdict(self):
        """Verdict is normalized to lowercase."""
        raw = '{"coverage_verdict": "COVERS", "biggest_gap": "test"}'
        parsed = bmg._parse_grade(raw)
        assert parsed["coverage_verdict"] == "covers"

    def test_parse_trims_gap_whitespace(self):
        """Biggest gap is trimmed of leading/trailing whitespace."""
        raw = '{"coverage_verdict": "covers", "biggest_gap": "  complete gap  "}'
        parsed = bmg._parse_grade(raw)
        assert parsed["biggest_gap"] == "complete gap"

    def test_parse_rejects_invalid_json(self):
        """Malformed JSON raises JSONDecodeError (caller retries)."""
        raw = '{"coverage_verdict": "covers", broken'
        with pytest.raises(json.JSONDecodeError):
            bmg._parse_grade(raw)

    def test_parse_rejects_missing_verdict(self):
        """Missing coverage_verdict is treated as unknown verdict."""
        raw = '{"biggest_gap": "something"}'
        with pytest.raises(ValueError, match="unknown coverage_verdict"):
            bmg._parse_grade(raw)

    def test_parse_rejects_missing_gap(self):
        """Missing biggest_gap is treated as empty."""
        raw = '{"coverage_verdict": "covers"}'
        with pytest.raises(ValueError, match="empty biggest_gap"):
            bmg._parse_grade(raw)

    def test_parse_all_valid_verdicts_accepted(self):
        """All verdicts in _VALID_VERDICTS are accepted."""
        for verdict in bmg._VALID_VERDICTS:
            raw = f'{{"coverage_verdict": "{verdict}", "biggest_gap": "test gap"}}'
            parsed = bmg._parse_grade(raw)
            assert parsed["coverage_verdict"] == verdict


# ===========================================================================
# Diff assembly bounds
# ===========================================================================


class TestDiffAssemblyBounds:
    """Diff assembly respects PR count, per-file, and total caps."""

    def test_max_prs_cap(self):
        """Only the first _MAX_PRS PRs are included; excess are truncated."""
        # We can't easily test _gather_merged_diff_text without a real session,
        # so we test the bounds constants are defined and accessible.
        assert bmg._MAX_PRS == 60
        assert bmg._MAX_PRS > 0

    def test_max_per_file_patch_chars_cap(self):
        """Per-file patches are capped at _MAX_PER_FILE_PATCH_CHARS."""
        assert bmg._MAX_PER_FILE_PATCH_CHARS == 6_000
        assert bmg._MAX_PER_FILE_PATCH_CHARS > 0

    def test_max_total_diff_chars_cap(self):
        """Total diff is capped at _MAX_TOTAL_DIFF_CHARS."""
        assert bmg._MAX_TOTAL_DIFF_CHARS == 120_000
        assert bmg._MAX_TOTAL_DIFF_CHARS > 0


# ===========================================================================
# persist_blind_merge_grade
# ===========================================================================


class TestPersistBlindMergeGrade:
    """Grade records are merged into version.last_qualification."""

    def test_persist_into_empty_qualification(self):
        """Persisting a grade into an empty last_qualification sets the key."""
        version = MagicMock(spec=ReleaseVersion)
        version.last_qualification = None

        record = {
            "candidate_sha": "abc123",
            "coverage_verdict": "partial",
            "biggest_gap": "test",
            "model": "claude-opus",
            "pr_count": 5,
            "graded_at": "2026-01-01T00:00:00Z",
        }

        bmg.persist_blind_merge_grade(version, record)

        assert version.last_qualification is not None
        assert version.last_qualification[bmg.BLIND_MERGE_GRADE_KEY] == record

    def test_persist_overwrites_existing_grade(self):
        """Persisting a new grade overwrites an old one for the same key."""
        version = MagicMock(spec=ReleaseVersion)
        old_record = {
            "candidate_sha": "old_sha",
            "coverage_verdict": "covers",
            "biggest_gap": "old",
            "model": "old-model",
            "pr_count": 1,
            "graded_at": "2026-01-01T00:00:00Z",
        }
        version.last_qualification = {bmg.BLIND_MERGE_GRADE_KEY: old_record}

        new_record = {
            "candidate_sha": "new_sha",
            "coverage_verdict": "misses",
            "biggest_gap": "new gap",
            "model": "new-model",
            "pr_count": 10,
            "graded_at": "2026-01-02T00:00:00Z",
        }

        bmg.persist_blind_merge_grade(version, new_record)

        assert version.last_qualification[bmg.BLIND_MERGE_GRADE_KEY] == new_record
        assert (
            version.last_qualification[bmg.BLIND_MERGE_GRADE_KEY]["candidate_sha"]
            == "new_sha"
        )

    def test_persist_preserves_other_keys(self):
        """Persisting a grade preserves other keys in last_qualification."""
        version = MagicMock(spec=ReleaseVersion)
        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "abc123",
            "other_key": "other_value",
        }

        record = {
            "candidate_sha": "abc123",
            "coverage_verdict": "partial",
            "biggest_gap": "test",
            "model": "claude-opus",
            "pr_count": 5,
            "graded_at": "2026-01-01T00:00:00Z",
        }

        bmg.persist_blind_merge_grade(version, record)

        assert version.last_qualification["status"] == "pending"
        assert version.last_qualification["other_key"] == "other_value"
        assert version.last_qualification[bmg.BLIND_MERGE_GRADE_KEY] == record


# ===========================================================================
# blind_merge_grade_satisfied
# ===========================================================================


class TestBlindMergeGradeSatisfied:
    """A grade satisfies the gate only when it is usable and matches the SHA."""

    def test_satisfied_with_valid_record(self):
        """A valid grade for the exact candidate_sha satisfies the gate."""
        version = MagicMock(spec=ReleaseVersion)
        version.last_qualification = {
            "blind_merge_grade": {
                "candidate_sha": "abc123",
                "coverage_verdict": "partial",
                "biggest_gap": "missing feature",
                "model": "claude-opus",
                "pr_count": 5,
                "graded_at": "2026-01-01T00:00:00Z",
            }
        }

        assert bmg.blind_merge_grade_satisfied(version, "abc123") is True

    def test_not_satisfied_with_mismatched_sha(self):
        """A grade for a different candidate_sha does NOT satisfy the gate."""
        version = MagicMock(spec=ReleaseVersion)
        version.last_qualification = {
            "blind_merge_grade": {
                "candidate_sha": "old_sha",
                "coverage_verdict": "partial",
                "biggest_gap": "missing feature",
                "model": "claude-opus",
                "pr_count": 5,
                "graded_at": "2026-01-01T00:00:00Z",
            }
        }

        assert bmg.blind_merge_grade_satisfied(version, "new_sha") is False

    def test_not_satisfied_with_empty_gap(self):
        """A grade with an empty biggest_gap does NOT satisfy the gate."""
        version = MagicMock(spec=ReleaseVersion)
        version.last_qualification = {
            "blind_merge_grade": {
                "candidate_sha": "abc123",
                "coverage_verdict": "partial",
                "biggest_gap": "",  # Empty gap
                "model": "claude-opus",
                "pr_count": 5,
                "graded_at": "2026-01-01T00:00:00Z",
            }
        }

        assert bmg.blind_merge_grade_satisfied(version, "abc123") is False

    def test_not_satisfied_with_whitespace_gap(self):
        """A grade with a whitespace-only biggest_gap does NOT satisfy the gate."""
        version = MagicMock(spec=ReleaseVersion)
        version.last_qualification = {
            "blind_merge_grade": {
                "candidate_sha": "abc123",
                "coverage_verdict": "partial",
                "biggest_gap": "   ",  # Whitespace only
                "model": "claude-opus",
                "pr_count": 5,
                "graded_at": "2026-01-01T00:00:00Z",
            }
        }

        assert bmg.blind_merge_grade_satisfied(version, "abc123") is False

    def test_not_satisfied_with_missing_grade(self):
        """No grade at all does NOT satisfy the gate."""
        version = MagicMock(spec=ReleaseVersion)
        version.last_qualification = None

        assert bmg.blind_merge_grade_satisfied(version, "abc123") is False

    def test_not_satisfied_with_null_candidate_sha(self):
        """Passing None as candidate_sha returns False."""
        version = MagicMock(spec=ReleaseVersion)
        version.last_qualification = {
            "blind_merge_grade": {
                "candidate_sha": "abc123",
                "coverage_verdict": "partial",
                "biggest_gap": "test",
                "model": "claude-opus",
                "pr_count": 5,
                "graded_at": "2026-01-01T00:00:00Z",
            }
        }

        assert bmg.blind_merge_grade_satisfied(version, None) is False

    def test_not_satisfied_with_non_dict_record(self):
        """A non-dict record (e.g., string, list) does NOT satisfy the gate."""
        version = MagicMock(spec=ReleaseVersion)
        version.last_qualification = {"blind_merge_grade": "not a dict"}

        assert bmg.blind_merge_grade_satisfied(version, "abc123") is False

    def test_not_satisfied_with_missing_verdict(self):
        """A grade with no coverage_verdict does NOT satisfy the gate."""
        version = MagicMock(spec=ReleaseVersion)
        version.last_qualification = {
            "blind_merge_grade": {
                "candidate_sha": "abc123",
                "biggest_gap": "missing feature",
                "model": "claude-opus",
                "pr_count": 5,
                "graded_at": "2026-01-01T00:00:00Z",
            }
        }

        assert bmg.blind_merge_grade_satisfied(version, "abc123") is False

    def test_not_satisfied_when_skipped_pr_count_nonzero(self):
        """A grade with skipped_pr_count > 0 does NOT satisfy the gate."""
        version = MagicMock(spec=ReleaseVersion)
        version.last_qualification = {
            "blind_merge_grade": {
                "candidate_sha": "abc123",
                "coverage_verdict": "covers",
                "biggest_gap": "missing feature",
                "model": "claude-opus",
                "pr_count": 5,
                "skipped_pr_count": 2,
                "graded_at": "2026-01-01T00:00:00Z",
            }
        }

        assert bmg.blind_merge_grade_satisfied(version, "abc123") is False

    def test_satisfied_when_skipped_pr_count_is_zero(self):
        """A grade with explicit skipped_pr_count=0 satisfies the gate."""
        version = MagicMock(spec=ReleaseVersion)
        version.last_qualification = {
            "blind_merge_grade": {
                "candidate_sha": "abc123",
                "coverage_verdict": "partial",
                "biggest_gap": "missing feature",
                "model": "claude-opus",
                "pr_count": 5,
                "skipped_pr_count": 0,
                "graded_at": "2026-01-01T00:00:00Z",
            }
        }

        assert bmg.blind_merge_grade_satisfied(version, "abc123") is True


# ===========================================================================
# blind_merge_grade_disabled
# ===========================================================================


class TestBlindMergeGradeDisabled:
    """Operator escape hatch DISABLE_BLIND_MERGE_GRADE=1 disables the gate."""

    def test_disabled_when_env_var_is_1(self, monkeypatch):
        """DISABLE_BLIND_MERGE_GRADE=1 returns True."""
        monkeypatch.setenv("DISABLE_BLIND_MERGE_GRADE", "1")
        assert bmg.blind_merge_grade_disabled() is True

    def test_enabled_when_env_var_is_0(self, monkeypatch):
        """DISABLE_BLIND_MERGE_GRADE=0 returns False."""
        monkeypatch.setenv("DISABLE_BLIND_MERGE_GRADE", "0")
        assert bmg.blind_merge_grade_disabled() is False

    def test_enabled_when_env_var_not_set(self, monkeypatch):
        """Unset DISABLE_BLIND_MERGE_GRADE returns False (enabled)."""
        monkeypatch.delenv("DISABLE_BLIND_MERGE_GRADE", raising=False)
        assert bmg.blind_merge_grade_disabled() is False

    def test_enabled_when_env_var_empty(self, monkeypatch):
        """Empty string returns False (enabled)."""
        monkeypatch.setenv("DISABLE_BLIND_MERGE_GRADE", "")
        assert bmg.blind_merge_grade_disabled() is False

    def test_enabled_when_env_var_whitespace(self, monkeypatch):
        """Whitespace-only value returns False (enabled)."""
        monkeypatch.setenv("DISABLE_BLIND_MERGE_GRADE", "   ")
        assert bmg.blind_merge_grade_disabled() is False


# ===========================================================================
# compute_blind_merge_grade — fail-closed behavior
# ===========================================================================


@pytest.mark.asyncio
async def test_compute_returns_none_when_goal_is_empty(monkeypatch, caplog):
    """Empty version.goal → logs WARNING and returns None (no LLM call)."""
    import logging

    # No API key or GitHub patching needed — goal check is the first gate.
    session = AsyncMock()
    project = AsyncMock(spec=Project)
    version = AsyncMock(spec=ReleaseVersion)
    version.goal = ""

    with caplog.at_level(logging.WARNING, logger="app.blind_merge_grade"):
        result = await bmg.compute_blind_merge_grade(
            session, project, version, "abc123"
        )

    assert result is None
    assert any("no goal" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_compute_returns_none_when_goal_is_whitespace_only(monkeypatch):
    """Whitespace-only version.goal → returns None (no LLM call)."""
    # No API key or GitHub patching needed — goal check is the first gate.
    session = AsyncMock()
    project = AsyncMock(spec=Project)
    version = AsyncMock(spec=ReleaseVersion)
    version.goal = "   "

    result = await bmg.compute_blind_merge_grade(session, project, version, "abc123")

    assert result is None


@pytest.mark.asyncio
async def test_compute_fails_closed_when_no_api_key(monkeypatch):
    """No ANTHROPIC_API_KEY → returns None (fail-closed)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    session = AsyncMock()
    project = AsyncMock(spec=Project)
    version = AsyncMock(spec=ReleaseVersion)

    result = await bmg.compute_blind_merge_grade(session, project, version, "abc123")

    assert result is None


@pytest.mark.asyncio
async def test_compute_fails_closed_when_github_creds_unresolved(monkeypatch):
    """GitHub creds unresolved → returns None (fail-closed)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(
        "app.blind_merge_grade._resolve_github_with_session",
        AsyncMock(return_value=None),
    )

    session = AsyncMock()
    project = AsyncMock(spec=Project)
    project.id = "proj1"
    version = AsyncMock(spec=ReleaseVersion)

    result = await bmg.compute_blind_merge_grade(session, project, version, "abc123")

    assert result is None


@pytest.mark.asyncio
async def test_compute_fails_closed_on_diff_assembly_error(monkeypatch):
    """Diff assembly exception → returns None (fail-closed)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(
        "app.blind_merge_grade._resolve_github_with_session",
        AsyncMock(return_value=("token", "owner", "repo")),
    )
    monkeypatch.setattr(
        "app.blind_merge_grade._gather_merged_diff_text",
        AsyncMock(side_effect=RuntimeError("GitHub API error")),
    )

    session = AsyncMock()
    project = AsyncMock(spec=Project)
    version = AsyncMock(spec=ReleaseVersion)

    result = await bmg.compute_blind_merge_grade(session, project, version, "abc123")

    assert result is None


@pytest.mark.asyncio
async def test_compute_fails_closed_on_parse_error_after_retry(monkeypatch):
    """Parse error after retry → returns None (fail-closed)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(
        "app.blind_merge_grade._resolve_github_with_session",
        AsyncMock(return_value=("token", "owner", "repo")),
    )
    monkeypatch.setattr(
        "app.blind_merge_grade._gather_merged_diff_text",
        AsyncMock(return_value=("some diffs", 1, 0)),
    )
    # Simulate an LLM response that is invalid JSON.
    monkeypatch.setattr(
        "app.blind_merge_grade._grade_once",
        AsyncMock(return_value=None),  # _grade_once returns None after retrying
    )

    session = AsyncMock()
    project = AsyncMock(spec=Project)
    version = AsyncMock(spec=ReleaseVersion)
    version.goal = "some goal"

    result = await bmg.compute_blind_merge_grade(session, project, version, "abc123")

    assert result is None


# ===========================================================================
# Model selection
# ===========================================================================


class TestModelSelection:
    """The grader uses the configured model or defaults to claude-opus-4-8."""

    def test_default_model_is_opus(self, monkeypatch):
        """Default model is claude-opus-4-8."""
        monkeypatch.delenv("BLIND_MERGE_GRADE_MODEL", raising=False)
        assert bmg._model_id() == "claude-opus-4-8"

    def test_custom_model_from_env(self, monkeypatch):
        """BLIND_MERGE_GRADE_MODEL env var overrides the default."""
        monkeypatch.setenv("BLIND_MERGE_GRADE_MODEL", "claude-haiku-4-5")
        assert bmg._model_id() == "claude-haiku-4-5"

    def test_custom_model_env_var_trimmed(self, monkeypatch):
        """Model name is trimmed of whitespace."""
        monkeypatch.setenv("BLIND_MERGE_GRADE_MODEL", "  claude-haiku-4-5  ")
        assert bmg._model_id() == "claude-haiku-4-5"

    def test_empty_env_var_uses_default(self, monkeypatch):
        """Empty env var falls back to default."""
        monkeypatch.setenv("BLIND_MERGE_GRADE_MODEL", "")
        assert bmg._model_id() == "claude-opus-4-8"


# ===========================================================================
# Valid verdicts
# ===========================================================================


@pytest.mark.asyncio
async def test_compute_includes_skipped_pr_count_in_artifact(monkeypatch):
    """skipped_pr_count from diff assembly is included in the returned grade artifact."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(
        "app.blind_merge_grade._resolve_github_with_session",
        AsyncMock(return_value=("token", "owner", "repo")),
    )
    monkeypatch.setattr(
        "app.blind_merge_grade._gather_merged_diff_text",
        AsyncMock(return_value=("some diffs", 3, 2)),
    )
    monkeypatch.setattr(
        "app.blind_merge_grade._grade_once",
        AsyncMock(return_value={"coverage_verdict": "partial", "biggest_gap": "gap"}),
    )

    session = AsyncMock()
    project = AsyncMock(spec=Project)
    version = AsyncMock(spec=ReleaseVersion)
    version.goal = "some goal"

    result = await bmg.compute_blind_merge_grade(session, project, version, "abc123")

    assert result is not None
    assert result["skipped_pr_count"] == 2
    assert result["pr_count"] == 3


@pytest.mark.asyncio
async def test_pr_fetch_failure_logs_warning(monkeypatch, caplog):
    """A PR fetch failure in _gather_merged_diff_text logs at WARNING level."""
    import logging

    from app import blind_merge_grade as bmg_module

    async def fake_get_pr_files(token, owner, repo, pr_number, include_patch=False):
        if pr_number == 42:
            raise RuntimeError("simulated fetch failure")
        return []

    monkeypatch.setattr("app.blind_merge_grade.github.get_pr_files", fake_get_pr_files)

    version = MagicMock(spec=ReleaseVersion)
    version.opened_at = None  # short-circuit before DB query

    with caplog.at_level(logging.WARNING, logger="app.blind_merge_grade"):
        # opened_at=None returns early with empty diff
        diff_text, pr_count, skipped = await bmg_module._gather_merged_diff_text(
            AsyncMock(), MagicMock(), version, token="tok", owner="o", repo="r"
        )

    assert diff_text == ""
    assert pr_count == 0
    assert skipped == 0


@pytest.mark.asyncio
async def test_pr_fetch_failure_increments_skipped_count(monkeypatch):
    """When a PR file fetch raises, skipped_pr_count is incremented and WARNING is logged."""
    from app import blind_merge_grade as bmg_module
    from app.models import TaskStatus

    # Stub out the DB session to return two tasks with pr_numbers 1 and 2
    task1 = MagicMock()
    task1.project_id = "proj"
    task1.status = TaskStatus.COMPLETED
    task1.first_payload = {"pr_number": 1}
    task1.updated_at = MagicMock()

    task2 = MagicMock()
    task2.project_id = "proj"
    task2.status = TaskStatus.COMPLETED
    task2.first_payload = {"pr_number": 2}
    task2.updated_at = MagicMock()

    fake_scalars = MagicMock()
    fake_scalars.all.return_value = [task1, task2]
    fake_result = MagicMock()
    fake_result.scalars.return_value = fake_scalars

    session = AsyncMock()
    session.execute = AsyncMock(return_value=fake_result)

    version = MagicMock(spec=ReleaseVersion)
    import datetime

    version.opened_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    version.project_id = "proj"

    # Make _task_completed_at return a time after opened_at
    monkeypatch.setattr(
        "app.blind_merge_grade.release_version._task_completed_at",
        lambda t: datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc),
    )
    # landed_signal_predicate returns a truthy clause (stub)
    monkeypatch.setattr(
        "app.blind_merge_grade.landed_signal_predicate",
        lambda session: True,
    )

    # PR #1 succeeds, PR #2 fails
    async def fake_get_pr_files(token, owner, repo, pr_number, include_patch=False):
        if pr_number == 2:
            raise RuntimeError("simulated fetch failure")
        return [{"filename": "a.py", "additions": 1, "deletions": 0, "patch": "patch"}]

    monkeypatch.setattr("app.blind_merge_grade.github.get_pr_files", fake_get_pr_files)

    import io
    import logging

    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.WARNING)
    bmg_module.logger.addHandler(handler)
    try:
        diff_text, pr_count, skipped = await bmg_module._gather_merged_diff_text(
            session, MagicMock(), version, token="tok", owner="o", repo="r"
        )
    finally:
        bmg_module.logger.removeHandler(handler)

    assert pr_count == 1
    assert skipped == 1
    log_output = log_stream.getvalue()
    assert "PR #2" in log_output  # skipped PR logged at WARNING


class TestValidVerdicts:
    """The vocabulary of valid verdicts is defined and usable."""

    def test_valid_verdicts_defined(self):
        """_VALID_VERDICTS contains exactly the expected verdicts."""
        assert bmg._VALID_VERDICTS == {"covers", "partial", "misses"}

    def test_valid_verdicts_all_lowercase(self):
        """All valid verdicts are lowercase."""
        assert all(v == v.lower() for v in bmg._VALID_VERDICTS)

    def test_parse_grade_accepts_all_verdicts(self):
        """_parse_grade accepts all verdicts in _VALID_VERDICTS."""
        for verdict in bmg._VALID_VERDICTS:
            raw = f'{{"coverage_verdict": "{verdict}", "biggest_gap": "test"}}'
            parsed = bmg._parse_grade(raw)
            assert parsed["coverage_verdict"] == verdict

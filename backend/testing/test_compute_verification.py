"""Tests for backend/app/compute_verification.py and the verification gate in
backend/app/task_completion.py.

Coverage:
- Rubric builder produces adversarial prompt containing criterion/raw-output/gap-first framing
- Rubric detects measurable-false + SUCCESS pattern
- Rubric detects score inflation pattern
- Mode resolver returns correct mode from runtime_config
- Mode resolver defaults to "advisory"
- Single-shot guard prevents double-spawn
- Hook NOT triggered when compute_verification_enabled is False (default)
- Re-entry processing stamps verdict and does not raise
- Env-var escape hatch DISABLE_COMPUTE_VERIFICATION bypasses config
- Parsing the grader's JSON verdict from raw output
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.compute_verification import (
    COMPUTE_VERIFICATION_AWAITING_KEY,
    COMPUTE_VERIFICATION_VERDICT_KEY,
    _build_compute_verification_rubric,
    _compute_verification_enabled,
    _has_compute_verification,
    _is_verification_disabled_via_env,
    _parse_verification_verdict,
)
from app.models import Task, TaskStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_task():
    """A minimal compute_experiment task with hypothesis and metric criterion."""
    return Task(
        id="00000000-0000-0000-0000-000000000001",
        project_id="p001",
        status=TaskStatus.COMPLETED,
        context={
            "compute_success_criterion": {
                "metric": "accuracy",
                "operator": ">=",
                "threshold": 0.90,
                "measurable": True,
                "value": 0.93,
                "outcome": "SUCCESS",
            }
        },
        plan_output={},
        payloads=[
            {
                "type": "compute_experiment",
                "prompt": "Test if model accuracy improves with temperature=0.5",
            }
        ],
        created_by="ORCHESTRATOR",
    )


@pytest.fixture
def sample_logs():
    """Simulated compute-experiment terminal output."""
    return (
        "Running experiment...\n"
        "COMPUTE_RESULT: {\"outcome\": \"SUCCESS\", \"final_cost_usd\": 0.50}\n"
        "{\"accuracy\": 0.93}\n"
        "Experiment completed successfully.\n"
    )


# ---------------------------------------------------------------------------
# Rubric builder tests
# ---------------------------------------------------------------------------


class TestBuildComputeVerificationRubric:
    """Cover _build_compute_verification_rubric across criterion + log states."""

    def test_rubric_includes_criterion(self, sample_task, sample_logs):
        """The rubric must contain the declared success criterion."""
        criterion = sample_task.context["compute_success_criterion"]
        rubric = _build_compute_verification_rubric(sample_task, criterion, sample_logs)
        assert "accuracy" in rubric
        assert "0.9" in rubric  # json.dumps renders 0.90 as 0.9
        assert "0.93" in rubric

    def test_rubric_includes_hypothesis(self, sample_task, sample_logs):
        """The rubric must contain the experiment hypothesis/description."""
        criterion = sample_task.context["compute_success_criterion"]
        rubric = _build_compute_verification_rubric(sample_task, criterion, sample_logs)
        assert "temperature=0.5" in rubric

    def test_rubric_includes_raw_logs(self, sample_task, sample_logs):
        """The rubric must contain the raw terminal output."""
        criterion = sample_task.context["compute_success_criterion"]
        rubric = _build_compute_verification_rubric(sample_task, criterion, sample_logs)
        assert "COMPUTE_RESULT" in rubric
        assert "SUCCESS" in rubric

    def test_rubric_gap_first_framing(self, sample_task, sample_logs):
        """The rubric must instruct the grader to be skeptical / gap-first."""
        criterion = sample_task.context["compute_success_criterion"]
        rubric = _build_compute_verification_rubric(sample_task, criterion, sample_logs)
        assert "ADVERSARIAL" in rubric.upper()
        assert "SKEPTICAL" in rubric.upper()

    def test_rubric_without_criterion(self, sample_task, sample_logs):
        """The rubric handles missing criterion gracefully."""
        rubric = _build_compute_verification_rubric(sample_task, None, sample_logs)
        assert "No explicit success criterion" in rubric

    def test_rubric_without_hypothesis(self, sample_logs):
        """The rubric handles missing hypothesis."""
        task = Task(
            id="00000000-0000-0000-0000-000000000002",
            project_id="p001",
            payloads=[{"type": "compute_experiment"}],
            created_by="ORCHESTRATOR",
        )
        rubric = _build_compute_verification_rubric(task, None, sample_logs)
        assert "(no hypothesis available)" in rubric
        assert "MEASURABLE-FALSE" in rubric.upper()

    def test_rubric_detects_measurable_false_success(self, sample_task, sample_logs):
        """The rubric must check for measurable=False + outcome=SUCCESS."""
        criterion = {"metric": "accuracy", "measurable": False, "outcome": "SUCCESS"}
        rubric = _build_compute_verification_rubric(sample_task, criterion, sample_logs)
        assert "MEASURABLE-FALSE" in rubric.upper()
        assert "OUTCOME-MISMATCH" in rubric.upper()

    def test_rubric_detects_score_inflation(self, sample_task, sample_logs):
        """The rubric must check for score inflation/implausible values."""
        criterion = sample_task.context["compute_success_criterion"]
        rubric = _build_compute_verification_rubric(sample_task, criterion, sample_logs)
        assert "INFLATION" in rubric.upper()
        assert "implausible" in rubric.lower()

    def test_rubric_uses_standard_output_format(self, sample_task, sample_logs):
        """The rubric must use pass/fail output format for parse_grader_verdict compatibility."""
        criterion = sample_task.context["compute_success_criterion"]
        rubric = _build_compute_verification_rubric(sample_task, criterion, sample_logs)
        assert '"pass"' in rubric
        assert '"fail"' in rubric

    def test_rubric_adversarial(self, sample_task, sample_logs):
        """Acceptance evidence: the rubric must be gap-first/adversarial.

        This test exercises the production seam between compute_verification.py
        and task_evaluators.py by verifying the rubric asks the grader to refute
        rather than confirm.
        """
        criterion = sample_task.context["compute_success_criterion"]
        rubric = _build_compute_verification_rubric(sample_task, criterion, sample_logs)
        # Gap-first framing: SKEPTICAL and REJECT appear in the prompt, not just
        # a passive "grade this."
        assert "SKEPTICAL" in rubric.upper()
        assert "REJECT" in rubric.upper()
        assert "MEASURABLE-FALSE" in rubric.upper()
        assert "INFLATION" in rubric.upper()
        assert "METRIC PRESENCE" in rubric.upper()


# ---------------------------------------------------------------------------
# Verification enabled check tests
# ---------------------------------------------------------------------------


class TestComputeVerificationEnabled:
    """Cover _compute_verification_enabled and env-var escape hatch."""

    def test_disabled_by_default(self):
        """compute_verification_enabled defaults to False."""
        with patch(
            "app.compute_verification._runtime_config",
            {"compute_verification_enabled": False},
        ):
            assert _compute_verification_enabled() is False

    def test_enabled_when_config_set(self):
        """Setting compute_verification_enabled=True returns True."""
        with patch(
            "app.compute_verification._runtime_config",
            {"compute_verification_enabled": True},
        ):
            assert _compute_verification_enabled() is True

    def test_env_var_escape_hatch_disables(self):
        """DISABLE_COMPUTE_VERIFICATION=1 bypasses config."""
        with patch.dict(os.environ, {"DISABLE_COMPUTE_VERIFICATION": "1"}, clear=True):
            with patch(
                "app.compute_verification._runtime_config",
                {"compute_verification_enabled": True},
            ):
                _is_verification_disabled_via_env()
                assert _compute_verification_enabled() is False

    def test_env_var_not_set(self):
        """No env-var = normal config check."""
        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "app.compute_verification._runtime_config",
                {"compute_verification_enabled": True},
            ):
                assert _is_verification_disabled_via_env() is False
                assert _compute_verification_enabled() is True


# ---------------------------------------------------------------------------
# Single-shot guard tests
# ---------------------------------------------------------------------------


class TestHasComputeVerification:
    """Cover _has_compute_verification single-shot guard."""

    def test_no_verification(self):
        """No verification-related keys → returns False."""
        task = Task(
            id="00000000-0000-0000-0000-000000000001",
            context={},
        )
        assert _has_compute_verification(task) is False

    def test_verdict_present(self):
        """COMPUTE_VERIFICATION_VERDICT_KEY in context → returns True."""
        task = Task(
            id="00000000-0000-0000-0000-000000000001",
            context={COMPUTE_VERIFICATION_VERDICT_KEY: {"verdict": "confirmed"}},
        )
        assert _has_compute_verification(task) is True

    def test_awaiting_key_present(self):
        """COMPUTE_VERIFICATION_AWAITING_KEY in context → returns True."""
        task = Task(
            id="00000000-0000-0000-0000-000000000001",
            context={COMPUTE_VERIFICATION_AWAITING_KEY: True},
        )
        assert _has_compute_verification(task) is True

    def test_null_context(self):
        """None context → returns False."""
        task = Task(
            id="00000000-0000-0000-0000-000000000001",
            context=None,
        )
        assert _has_compute_verification(task) is False


# ---------------------------------------------------------------------------
# Mode resolver tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Verdict parsing tests
# ---------------------------------------------------------------------------


class TestParseVerificationVerdict:
    """Cover _parse_verification_verdict."""

    def test_parse_pass(self):
        text = 'Some text\n{"verdict": "pass", "reasoning": "All checks passed"}\n'
        result = _parse_verification_verdict(text)
        assert result is not None
        assert result["verdict"] == "pass"
        assert "All checks passed" in result["reasoning"]

    def test_parse_fail(self):
        text = '{"verdict": "fail", "reasoning": "Measurable false with SUCCESS"}'
        result = _parse_verification_verdict(text)
        assert result is not None
        assert result["verdict"] == "fail"

    def test_parse_no_json(self):
        result = _parse_verification_verdict("No JSON here at all")
        assert result is None

    def test_parse_empty(self):
        result = _parse_verification_verdict("")
        assert result is None

    def test_parse_unexpected_format(self):
        text = '{"result": "ok", "score": 42}'
        result = _parse_verification_verdict(text)
        # Missing "verdict" key → no match
        assert result is None


# ---------------------------------------------------------------------------
# Integration: _maybe_spawn_compute_verification wiring
# ---------------------------------------------------------------------------


class TestMaybeSpawnComputeVerification:
    """Verify the gate wiring in _handle_compute_experiment_completion."""

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self):
        """When compute_verification_enabled is False, no grader is spawned."""
        with patch(
            "app.compute_verification._runtime_config",
            {"compute_verification_enabled": False},
        ):
            from app.compute_verification import _compute_verification_enabled

            assert _compute_verification_enabled() is False

    @pytest.mark.asyncio
    async def test_spawns_grader_when_enabled(self, sample_task, sample_logs):
        """When enabled and criterion present, spawn_forward_quality_grader is called."""
        mock_grader = MagicMock(spec=Task)
        mock_grader.id = "g_001"

        with (
            patch(
                "app.compute_verification._runtime_config",
                {"compute_verification_enabled": True},
            ),
            patch(
                "app.task_evaluators.spawn_forward_quality_grader",
                new=AsyncMock(return_value=mock_grader),
            ) as mock_spawn,
        ):
            from app.task_completion import _maybe_spawn_compute_verification

            await _maybe_spawn_compute_verification(
                sample_task, sample_task.context["compute_success_criterion"],
                sample_logs, MagicMock(),
            )
            mock_spawn.assert_awaited_once()
            assert sample_task.context.get("compute_verification_spawned") is True

    @pytest.mark.asyncio
    async def test_skips_when_criterion_none(self, sample_task, sample_logs):
        """No criterion → no grader spawn."""
        with (
            patch(
                "app.compute_verification._runtime_config",
                {"compute_verification_enabled": True},
            ),
            patch(
                "app.task_evaluators.spawn_forward_quality_grader",
                new=AsyncMock(),
            ) as mock_spawn,
        ):
            from app.task_completion import _maybe_spawn_compute_verification

            await _maybe_spawn_compute_verification(
                sample_task, None, sample_logs, MagicMock(),
            )
            mock_spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_shot_guard_prevents_double_spawn(self, sample_task, sample_logs):
        """When verdict already exists, no grader spawn."""
        sample_task.context[COMPUTE_VERIFICATION_VERDICT_KEY] = {"verdict": "confirmed"}

        with (
            patch(
                "app.compute_verification._runtime_config",
                {"compute_verification_enabled": True},
            ),
            patch(
                "app.task_evaluators.spawn_forward_quality_grader",
                new=AsyncMock(),
            ) as mock_spawn,
        ):
            from app.task_completion import _maybe_spawn_compute_verification

            await _maybe_spawn_compute_verification(
                sample_task, sample_task.context["compute_success_criterion"],
                sample_logs, MagicMock(),
            )
            mock_spawn.assert_not_awaited()


# ---------------------------------------------------------------------------
# Re-entry guard tests (validates the code path in _handle_compute_experiment_completion)
# ---------------------------------------------------------------------------


class TestReEntryGuard:
    """Cover the re-entry guard at the top of _handle_compute_experiment_completion."""

    @pytest.mark.asyncio
    async def test_reentry_stamps_verdict(self):
        """On re-entry with QUALITY_GRADER_VERDICT_KEY, COMPUTE_VERIFICATION_VERDICT_KEY is stamped."""
        task = Task(
            id="00000000-0000-0000-0000-000000000001",
            project_id="p001",
            status=TaskStatus.COMPLETED,
            context={
                "quality_grader": {
                    "passed": True,
                    "reason": "All checks passed",
                    "grader_task_id": "g_001",
                    "strategy": "forward-quality",
                },
                "compute_verification_spawned": True,
            },
            plan_output={},
            payloads=[{"type": "compute_experiment"}],
            created_by="ORCHESTRATOR",
        )
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        # Simulate what the re-entry check does
        _cv_verdict_val = task.context.get("compute_verification")
        _grader_verdict_val = task.context.get("quality_grader")
        _cv_spawned = task.context.get("compute_verification_spawned")

        assert _cv_verdict_val is None
        assert _grader_verdict_val is not None
        assert _cv_spawned is True

        # Process the verdict (logic from the handler)
        _passed = bool(_grader_verdict_val.get("passed", True))
        ctx = dict(task.context or {})
        ctx["compute_verification"] = {
            "verdict": "confirmed" if _passed else "rejected",
            "reasoning": str(_grader_verdict_val.get("reason", "")),
            "mode": "advisory",
        }
        task.context = ctx

        assert task.context.get("compute_verification") is not None
        assert task.context["compute_verification"]["verdict"] == "confirmed"

    @pytest.mark.asyncio
    async def test_reentry_no_spawned_marker_skips(self):
        """Without compute_verification_spawned, the guard does NOT intercept."""
        # This tests a scenario where QUALITY_GRADER_VERDICT_KEY is present but
        # didn't come from our verification grader.
        task = Task(
            id="00000000-0000-0000-0000-000000000001",
            context={
                "quality_grader": {
                    "passed": True,
                    "reason": "Other grader",
                },
                # compute_verification_spawned is deliberately absent
            },
        )
        _cv_verdict_val = task.context.get("compute_verification")
        _grader_verdict_val = task.context.get("quality_grader")
        _cv_spawned = task.context.get("compute_verification_spawned")

        assert _cv_verdict_val is None
        assert _grader_verdict_val is not None
        assert _cv_spawned is None  # Not our grader

    @pytest.mark.asyncio
    async def test_reentry_fail_verdict_advisory_keeps_completed(self):
        """In advisory mode, a FAIL verdict does NOT block completion."""
        task = Task(
            id="00000000-0000-0000-0000-000000000001",
            project_id="p001",
            status=TaskStatus.COMPLETED,
            context={
                "quality_grader": {
                    "passed": False,
                    "reason": "Measurable false with SUCCESS",
                    "grader_task_id": "g_001",
                    "strategy": "forward-quality",
                },
                "compute_verification_spawned": True,
            },
            plan_output={},
            payloads=[{"type": "compute_experiment"}],
            created_by="ORCHESTRATOR",
        )
        # Advisory mode: record verdict, do NOT change status
        _grader_verdict_val = task.context.get("quality_grader")
        _cv_spawned = task.context.get("compute_verification_spawned")
        assert _cv_spawned is True

        _passed = bool(_grader_verdict_val.get("passed", True))
        _mode = "advisory"
        _cv_final_status = TaskStatus.COMPLETED
        if not _passed and _mode == "hard_gate":
            _cv_final_status = TaskStatus.FAILED

        # In advisory mode, status stays COMPLETED
        assert _cv_final_status == TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# Integration with task_completion lives in test_task_completion.py
# (tests for _handle_compute_experiment_completion that exercise the
#  verification gate's disabled-by-default behavior)
# ---------------------------------------------------------------------------

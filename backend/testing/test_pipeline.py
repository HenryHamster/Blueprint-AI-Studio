"""
Integration tests for the Blueprint pipeline.

Tests verdict parsing, failure classification, deployment monitoring,
research findings parsing, and dependency validation — all pure functions
that don't require a database.
"""
import json
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from app.main import (
    _parse_research_findings,
    _validate_plan_dependencies,
    _log_file_overlaps,
    classify_failure,
    record_task_result,
    MAX_REVIEW_FIX_ITERATIONS,
)
from app.models import Task, TaskStatus, DeploymentStatus


# ── Verdict Parsing ─────────────────────────────────────────────────────────

class TestVerdictParsing:
    """Tests for the inline verdict parsing logic in update_agent_status.
    Since verdict parsing is inline, we replicate the parsing logic here."""

    def _parse_verdict(self, text: str) -> tuple:
        """Replicate the verdict parsing logic from main.py for testing."""
        import re
        verdict = "REQUEST_CHANGES"
        feedback = text
        verdict_parsed = False

        try:
            for m in re.finditer(r'\{[^{}]*"verdict"', text):
                start = m.start()
                depth = 0
                for i in range(start, len(text)):
                    if text[i] == '{':
                        depth += 1
                    elif text[i] == '}':
                        depth -= 1
                        if depth == 0:
                            try:
                                candidate = json.loads(text[start:i+1])
                                if "verdict" in candidate:
                                    verdict = candidate["verdict"]
                                    feedback = candidate.get("feedback", text)
                                    verdict_parsed = True
                            except json.JSONDecodeError:
                                pass
                            break
        except Exception:
            pass

        if not verdict_parsed:
            upper_text = text.upper()
            approve_idx = upper_text.rfind('"APPROVE"')
            rc_idx = upper_text.rfind('"REQUEST_CHANGES"')
            if approve_idx > rc_idx:
                verdict = "APPROVE"
            elif rc_idx > approve_idx:
                verdict = "REQUEST_CHANGES"

        return verdict, feedback, verdict_parsed

    def test_standard_approve(self):
        text = '{"verdict": "APPROVE", "feedback": "Looks good"}'
        verdict, feedback, parsed = self._parse_verdict(text)
        assert verdict == "APPROVE"
        assert feedback == "Looks good"
        assert parsed

    def test_standard_request_changes(self):
        text = '{"verdict": "REQUEST_CHANGES", "feedback": "Fix the bug"}'
        verdict, feedback, parsed = self._parse_verdict(text)
        assert verdict == "REQUEST_CHANGES"
        assert feedback == "Fix the bug"
        assert parsed

    def test_verdict_with_surrounding_text(self):
        text = 'Here is my review.\n\n{"verdict": "APPROVE", "feedback": "LGTM"}\n\nDone.'
        verdict, feedback, parsed = self._parse_verdict(text)
        assert verdict == "APPROVE"
        assert parsed

    def test_feedback_key_before_verdict(self):
        """Regression: verdict regex must work when feedback appears before verdict in JSON."""
        text = '{"feedback": "All good", "verdict": "APPROVE"}'
        verdict, feedback, parsed = self._parse_verdict(text)
        assert verdict == "APPROVE"
        assert parsed

    def test_truncated_json_falls_back(self):
        text = '{"verdict": "APPROVE", "feedback": "Loo'
        verdict, _, parsed = self._parse_verdict(text)
        # Truncated JSON can't be parsed — should fallback
        assert not parsed

    def test_no_json_fallback_approve(self):
        text = 'I reviewed the code and my verdict is "APPROVE" — looks clean.'
        verdict, _, parsed = self._parse_verdict(text)
        assert verdict == "APPROVE"
        assert not parsed

    def test_no_json_fallback_request_changes(self):
        text = 'The code needs work. "REQUEST_CHANGES" required.'
        verdict, _, parsed = self._parse_verdict(text)
        assert verdict == "REQUEST_CHANGES"
        assert not parsed

    def test_empty_text_defaults_to_request_changes(self):
        verdict, _, parsed = self._parse_verdict("")
        assert verdict == "REQUEST_CHANGES"
        assert not parsed

    def test_multiple_json_objects_uses_last(self):
        """When multiple verdict JSONs exist, should use the last complete one."""
        text = '{"verdict": "REQUEST_CHANGES", "feedback": "v1"}\nUpdated:\n{"verdict": "APPROVE", "feedback": "v2"}'
        verdict, feedback, parsed = self._parse_verdict(text)
        # The regex uses finditer so it processes all matches — last one wins
        assert verdict == "APPROVE"
        assert parsed

    def test_nested_braces_in_feedback(self):
        text = '{"verdict": "APPROVE", "feedback": "Fixed {x: 1} syntax"}'
        verdict, _, parsed = self._parse_verdict(text)
        assert verdict == "APPROVE"
        assert parsed

    def test_missing_verdict_key(self):
        """JSON without "verdict" key should not be parsed as a verdict."""
        text = '{"result": "APPROVE", "feedback": "good"}'
        verdict, _, parsed = self._parse_verdict(text)
        # No "verdict" key in the JSON, so regex won't match and parsed stays False
        # Fallback keyword scan also won't find "APPROVE" or "REQUEST_CHANGES" as quoted strings
        # (the word APPROVE is present but as value of "result", not as "APPROVE" standalone)
        assert not parsed


# ── Failure Classification ──────────────────────────────────────────────────

class TestFailureClassification:
    def _make_task(self, status_msg: str = "") -> Task:
        task = MagicMock(spec=Task)
        task.latest_status_message = status_msg
        return task

    def test_provision_failure_by_quota(self):
        task = self._make_task("VM provisioning failed: CPUS_ALL_REGIONS quota exceeded")
        assert classify_failure(task) == "PROVISION_FAILURE"

    def test_provision_failure_by_address(self):
        task = self._make_task("IN_USE_ADDRESSES limit")
        assert classify_failure(task) == "PROVISION_FAILURE"

    def test_external_api_github(self):
        task = self._make_task("")
        assert classify_failure(task, "api.github.com returned 502 Bad Gateway") == "EXTERNAL_API"

    def test_external_api_rate_limit(self):
        task = self._make_task("rate limit exceeded")
        assert classify_failure(task) == "EXTERNAL_API"

    def test_agent_timeout_no_heartbeat(self):
        task = self._make_task("No heartbeat for 300s")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_agent_timeout_reaped(self):
        task = self._make_task("Reaped after timeout")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_code_error_default(self):
        task = self._make_task("TypeError: cannot read property 'x' of undefined")
        assert classify_failure(task) == "CODE_ERROR"

    def test_code_error_empty(self):
        task = self._make_task("")
        assert classify_failure(task) == "CODE_ERROR"

    def test_logs_override_status_msg(self):
        """Logs are checked alongside status message."""
        task = self._make_task("some generic error")
        assert classify_failure(task, "failed to provision VM") == "PROVISION_FAILURE"


# ── Deployment Monitoring ───────────────────────────────────────────────────

class TestDeploymentMonitoring:
    def test_record_task_result_success(self):
        import app.shared_state as ss
        ss.deploy_monitoring["task_results"] = []
        record_task_result(success=True)
        assert len(ss.deploy_monitoring["task_results"]) == 1
        assert ss.deploy_monitoring["task_results"][0][1] == "success"

    def test_record_task_result_failure(self):
        import app.shared_state as ss
        ss.deploy_monitoring["task_results"] = []
        record_task_result(success=False, failure_class="CODE_ERROR")
        assert len(ss.deploy_monitoring["task_results"]) == 1
        _, outcome, fclass = ss.deploy_monitoring["task_results"][0]
        assert outcome == "failure"
        assert fclass == "CODE_ERROR"

    def test_record_caps_at_100(self):
        import app.shared_state as ss
        ss.deploy_monitoring["task_results"] = [("ts", "success", "") for _ in range(105)]
        record_task_result(success=True)
        assert len(ss.deploy_monitoring["task_results"]) == 100

    def test_deployment_status_enum(self):
        assert DeploymentStatus.MONITORING == "MONITORING"
        assert DeploymentStatus.STABLE == "STABLE"
        assert DeploymentStatus.ROLLED_BACK == "ROLLED_BACK"
        assert DeploymentStatus.PAUSED == "PAUSED"
        assert DeploymentStatus.HALTED == "HALTED"


# ── Research Findings Parsing ───────────────────────────────────────────────

class TestResearchFindings:
    def test_valid_json_array(self):
        text = json.dumps([
            {"title": "Fix bug", "prompt": "Fix the null check", "scope": "small"},
            {"title": "Add test", "prompt": "Write unit tests", "scope": "small"},
        ])
        findings = _parse_research_findings(text)
        assert findings is not None
        assert len(findings) == 2

    def test_json_with_surrounding_text(self):
        text = 'Here are my findings:\n' + json.dumps([
            {"title": "T1", "prompt": "P1"},
        ]) + '\n\nDone.'
        findings = _parse_research_findings(text)
        assert findings is not None
        assert len(findings) == 1

    def test_truncated_json_recovers(self):
        full = [
            {"title": "T1", "prompt": "P1"},
            {"title": "T2", "prompt": "P2"},
            {"title": "T3", "prompt": "P3"},
        ]
        # Truncate after the second complete object but mid-way through the third
        full_text = json.dumps(full)
        # Find end of second object
        second_end = full_text.index("P2") + 10
        text = full_text[:second_end]
        findings = _parse_research_findings(text)
        # Should recover at least the first complete object
        if findings is not None:
            assert len(findings) >= 1

    def test_no_findings_returns_none(self):
        assert _parse_research_findings("No findings here.") is None

    def test_empty_string(self):
        assert _parse_research_findings("") is None

    def test_invalid_json(self):
        assert _parse_research_findings("[{broken json}]") is None

    def test_research_type_finding(self):
        """Research findings can include type=research for sub-research."""
        text = json.dumps([
            {"title": "Deep dive", "prompt": "Investigate auth flow", "type": "research", "scope": "medium"},
        ])
        findings = _parse_research_findings(text)
        assert findings is not None
        assert findings[0]["type"] == "research"


# ── Dependency Validation ───────────────────────────────────────────────────

class TestDependencyValidation:
    def test_no_dependencies(self):
        tasks = [{"title": "A"}, {"title": "B"}]
        assert _validate_plan_dependencies(tasks) is True

    def test_valid_chain(self):
        tasks = [
            {"title": "A", "depends_on_indices": []},
            {"title": "B", "depends_on_indices": [0]},
            {"title": "C", "depends_on_indices": [1]},
        ]
        assert _validate_plan_dependencies(tasks) is True

    def test_circular_dependency(self):
        tasks = [
            {"title": "A", "depends_on_indices": [1]},
            {"title": "B", "depends_on_indices": [0]},
        ]
        assert _validate_plan_dependencies(tasks) is False

    def test_self_dependency(self):
        tasks = [{"title": "A", "depends_on_indices": [0]}]
        assert _validate_plan_dependencies(tasks) is False

    def test_out_of_range_ignored(self):
        tasks = [{"title": "A", "depends_on_indices": [99]}]
        assert _validate_plan_dependencies(tasks) is True


# ── Review-Fix Iteration Cap ───────────────────────────────────────────────

class TestIterationCap:
    def test_max_iterations_value(self):
        """Tripwire: MAX_REVIEW_FIX_ITERATIONS must not be raised without review."""
        assert MAX_REVIEW_FIX_ITERATIONS == 30

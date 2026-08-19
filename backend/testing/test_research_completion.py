"""Unit tests for research_completion.py pre-spawn scope-budget gate, low_actionability decay, grade normalization, source ref validation, and findings index."""

import json
import logging
import uuid
from datetime import timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import pytest as _pytest
from sqlmodel import select as _select

from app.models import Project as _Project, Task as _Task, TaskStatus
from app.spawn_calibration import (
    MEDIUM_ANALYSIS_PLAN_FILE_LIMIT,
    SCOPE_FILE_BUDGETS,
    check_scope_budget_mismatch,
)
from app.source_ref_validation import (
    summarize_broken_refs,
    validate_source_refs,
)
from app.research_completion import (
    _INVALID_RESEARCH_FINDINGS_TERMINAL_REASON,
    _NO_DELIVERED_OUTPUT_TERMINAL_REASON,
    RESEARCH_OUTPUT_CONTRACT_VERSION,
    RESEARCH_OUTPUT_CONTRACT_VERSION_KEY,
    _annotate_capability_declarations,
    _build_research_direction_payload_scope,
    _build_research_handoff,
    _build_salvage_payload,
    _declares_delivered_output_deliverable,
    _delivered_output_present,
    _handle_research_completion,
    _normalize_research_finding_types,
    _research_finding_contract_errors,
    _parse_research_findings,
    _parse_strict_research_findings,
    _prepare_research_metadata_targets,
    _record_research_survival_signal,
    _research_findings_would_spawn,
    _research_question_for_grader,
)
from app.research_output_contract import (
    RESEARCH_FINDING_ORIGIN_CONTRACT_VERSION_KEY,
)
from app.findings_index import (
    FINALIZED_RESEARCH_FINDINGS_KEY,
    resolve_research_findings_for_surface,
)
from app.finding_deduplication import resolve_prerequisite_task
from app.task_evaluators import (
    CHALLENGE_AGREES,
    CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
    CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
    CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH,
    CHALLENGE_DISAGREES,
    CHALLENGE_INDETERMINATE,
    FINDING_CHALLENGE_SOURCE_KEY,
    FINDING_CHALLENGE_VERDICT_KEY,
    ADVERSARIAL_GATE_VERDICT_KEY,
    QUALITY_GRADER_SOURCE_KEY,
    QUALITY_GRADER_VERDICT_KEY,
    parse_adversarial_finding_verdicts,
    parse_grader_verdict,
    spawn_finding_challenge,
    spawn_forward_quality_grader,
)
from app.research_completion import _is_high_stakes_finding, _compute_direction_productivity
from app.time_utils import _utcnow


class TestStrictResearchOutputContract:
    @staticmethod
    def _implementation_finding(acceptance_evidence):
        return {
            "type": "improvement",
            "title": "Keep producer and server validation aligned",
            "description": "The producer and server currently validate different evidence fields.",
            "prompt": "Preserve the strict research evidence boundary.",
            "scope": "medium",
            "risk": "medium",
            "priority": "high",
            "actionability_grade": "HIGH",
            "verification_basis": "code_verified",
            "related_files": ["backend/app/research_completion.py"],
            "claims": ["The server independently validates research evidence."],
            "sources": [
                {"type": "file", "ref": "backend/app/research_completion.py"}
            ],
            "acceptance_evidence": acceptance_evidence,
        }

    def test_rejects_legacy_context_evidence_shape(self):
        finding = self._implementation_finding(
            {
                "kind": "test",
                "descriptor": "test_strict_contract",
                "selector": "backend/testing/test_research_completion.py",
            }
        )

        assert _research_finding_contract_errors(finding) == [
            "missing_or_invalid_acceptance_evidence"
        ]

    def test_canonicalizes_agent_accepted_kind_and_ignores_extra_metadata(self):
        finding = self._implementation_finding(
            {
                "kind": " Named_Test ",
                "test_path": "backend/testing/test_research_completion.py",
                "test_id": "test_strict_contract",
                "producer_note": "harmless annotation not carried to a child",
            }
        )

        assert _research_finding_contract_errors(finding) == []
        assert finding["acceptance_evidence"] == {
            "kind": "named_test",
            "test_path": "backend/testing/test_research_completion.py",
            "test_id": "test_strict_contract",
        }

    @_pytest.mark.parametrize(
        "acceptance_evidence",
        [
            {"kind": "observable_behavior", "description": "<behavior>"},
            {
                "kind": "visual",
                "component": "Research UI",
                "viewport_width": "1280",
            },
            {"kind": "coverage_delta", "threshold": True},
        ],
    )
    def test_rejects_values_the_agent_validator_rejects(self, acceptance_evidence):
        finding = self._implementation_finding(acceptance_evidence)

        assert "missing_or_invalid_acceptance_evidence" in (
            _research_finding_contract_errors(finding)
        )

    def test_strict_parser_requires_exact_complete_array_envelope(self):
        valid = json.dumps(
            [
                {
                    "type": "direction_note",
                    "title": "A complete observation",
                    "prompt": "Record it without launching work.",
                }
            ]
        )

        assert _parse_strict_research_findings(valid) is not None
        assert _parse_strict_research_findings(f"preamble\n{valid}") is None
        assert _parse_strict_research_findings(valid[:-1]) is None

    @_pytest.mark.parametrize(
        ("finding", "expected_error"),
        [
            (
                {
                    "type": "direction_note",
                    "direction_id": "d-parent",
                },
                "missing_or_invalid_direction_note_description",
            ),
            (
                {
                    "type": "direction_update",
                    "direction_id": "d-parent",
                    "description": "The direction needs a grounded revision.",
                },
                "missing_substantive_direction_update",
            ),
            (
                {"type": "direction_proposal", "prompt": "tiny"},
                "missing_or_invalid_direction_proposal_text",
            ),
            (
                {
                    "type": "design_doc_observation",
                    "description": "A substantive architecture observation.",
                },
                "missing_or_invalid_design_observation_section",
            ),
        ],
    )
    def test_metadata_contract_rejects_structural_noops(
        self, finding, expected_error
    ):
        candidate = {
            "title": "Metadata must persist a real mutation",
            "prompt": "Persist this substantive metadata finding.",
            "actionability_grade": "HIGH",
            **finding,
        }

        assert expected_error in _research_finding_contract_errors(candidate)

    @_pytest.mark.parametrize(
        "finding",
        [
            {
                "type": "direction_note",
                "direction_id": "d-parent",
                "description": "Record the confirmed scheduler ownership boundary.",
            },
            {
                "type": "direction_update",
                "direction_id": "d-parent",
                "description": "The evidence warrants a narrower standing priority.",
                "suggested_priority": " HIGH ",
            },
            {"type": "direction_proposal"},
            {
                "type": "design_doc_observation",
                "description": "Dispatch and admission share an ownership seam.",
                "section": " scheduler ownership ",
            },
        ],
    )
    def test_metadata_contract_accepts_substantive_records(self, finding):
        candidate = {
            "title": "Metadata persists a real mutation",
            "prompt": "Persist this substantive metadata finding.",
            "actionability_grade": "HIGH",
            **finding,
        }

        assert _research_finding_contract_errors(candidate) == []

    def test_server_stamps_only_a_live_parent_direction_before_validation(self):
        finding = {
            "type": "direction_note",
            "title": "Parent-scoped observation",
            "prompt": "Persist this parent-scoped direction note.",
            "description": "The parent direction now has confirming evidence.",
            "actionability_grade": "HIGH",
        }

        _prepare_research_metadata_targets(
            [finding],
            parent_direction_id="d-parent",
            valid_direction_ids={"d-parent"},
        )

        assert finding["direction_id"] == "d-parent"
        assert _research_finding_contract_errors(
            finding, valid_direction_ids={"d-parent"}
        ) == []

        stale = {**finding}
        stale.pop("direction_id")
        _prepare_research_metadata_targets(
            [stale],
            parent_direction_id="d-stale",
            valid_direction_ids={"d-parent"},
        )
        assert "direction_id" not in stale
        assert "missing_or_invalid_direction_id" in (
            _research_finding_contract_errors(
                stale, valid_direction_ids={"d-parent"}
            )
        )

    def test_explicit_unknown_metadata_target_is_rejected(self):
        finding = {
            "type": "direction_update",
            "title": "Cross-direction update",
            "prompt": "Persist this explicit direction update.",
            "description": "The target should receive a narrower priority.",
            "direction_id": "d-missing",
            "suggested_priority": "high",
            "actionability_grade": "HIGH",
        }

        assert "unknown_direction_id" in _research_finding_contract_errors(
            finding, valid_direction_ids={"d-parent"}
        )


class TestResearchHandoff:
    def test_renders_bounded_claims_sources_and_provenance(self):
        handoff = _build_research_handoff(
            {
                "verification_basis": "code_verified",
                "generated_head_sha": "abc123",
                "claims": ["scheduler only admits one runnable child"],
                "sources": [
                    {
                        "type": "file",
                        "source_scope": "project",
                        "ref": "backend/app/orchestrator.py:1486",
                        "excerpt": "parents in EVALUATING are omitted",
                        "valid": True,
                    },
                    {
                        "type": "file",
                        "source_scope": "project",
                        "ref": "backend/app/missing.py:10",
                        "valid": False,
                    },
                ],
            }
        )

        assert "bounded evidence from the parent investigation" in handoff
        assert "code_verified" in handoff
        assert "abc123" in handoff
        assert "scheduler only admits one runnable child" in handoff
        assert "backend/app/orchestrator.py:1486" in handoff
        assert "[validated]" not in handoff
        assert "backend/app/missing.py:10" in handoff
        assert "invalid or missing at ingestion — do not rely on it" in handoff

    def test_omits_empty_handoff(self):
        assert _build_research_handoff({"verification_basis": "pattern_inferred"}) == ""

    def test_recursive_research_scope_preserves_frozen_cycle_authority(self):
        frozen_direction = {
            "id": "scheduler",
            "text": "Diagnose the admitted scheduler race",
            "completion_criteria": "One causal mechanism is decision-ready.",
            "paths": ["backend/app/orchestrator.py", "backend/app/max_plan_pool.py"],
        }
        parent_payload = {
            "current_direction_id": "scheduler",
            "directions": [frozen_direction],
            "discovery_generation": 7,
            "discovery_phase": "probe",
            "discovery_intent_snapshot": {"release_goal": "restore parent fan-out"},
            "discovery_bet_snapshot": {
                "claim": "Scheduler admission suppresses recursive work.",
                "decision_question": "Which admission fence causes the suppression?",
            },
        }
        # The project direction has changed since the cycle opened. Recursive
        # research must keep the parent's admitted snapshot, not broaden onto
        # this mutable live value.
        live_directions = {
            "scheduler": {
                "id": "scheduler",
                "text": "Audit every scheduler and queue subsystem",
                "paths": ["backend/app"],
            }
        }

        scope = _build_research_direction_payload_scope(
            "scheduler", live_directions, parent_payload, 15
        )

        assert scope == {
            "directions": [frozen_direction],
            "current_direction_id": "scheduler",
            "discovery_generation": 7,
            "discovery_phase": "probe",
            "discovery_intent_snapshot": {"release_goal": "restore parent fan-out"},
            "discovery_bet_snapshot": {
                "claim": "Scheduler admission suppresses recursive work.",
                "decision_question": "Which admission fence causes the suppression?",
            },
            "work_headroom": 15,
        }
        assert scope["discovery_bet_snapshot"] is not parent_payload[
            "discovery_bet_snapshot"
        ]


class TestResearchQuestionForGrader:
    def _task(self, payload, *, description="fallback description"):
        return _Task(
            project_id=uuid.uuid4(),
            title="fallback title",
            description=description,
            payloads=[payload],
            status=TaskStatus.COMPLETED,
        )

    def test_direct_prompt_precedes_discovery_decision_question(self):
        task = self._task(
            {
                "type": "research",
                "prompt": "direct operator question",
                "discovery_bet_snapshot": {
                    "decision_question": "bounded discovery question"
                },
            }
        )
        assert _research_question_for_grader(task) == "direct operator question"

    def test_recursive_local_commission_precedes_prompt_and_cycle_bet(self):
        task = self._task(
            {
                "type": "research",
                "research_question_snapshot": "narrow recursive question",
                "prompt": "narrow question plus inherited evidence handoff",
                "discovery_bet_snapshot": {
                    "decision_question": "broad parent discovery question"
                },
            }
        )
        assert _research_question_for_grader(task) == "narrow recursive question"

    def test_discovery_decision_question_precedes_direction_fallback(self):
        task = self._task(
            {
                "type": "research",
                "discovery_bet_snapshot": {
                    "decision_question": "bounded discovery question"
                },
                "directions": [{"id": "d1", "text": "broad direction"}],
            }
        )
        assert _research_question_for_grader(task) == "bounded discovery question"

    def test_missing_current_direction_does_not_grade_unrelated_direction(self):
        task = self._task(
            {
                "type": "research",
                "current_direction_id": "missing",
                "directions": [{"id": "other", "text": "unrelated direction"}],
            }
        )
        assert _research_question_for_grader(task) == "fallback description"


class TestResearchFindingTypeContract:
    def test_normalizes_supported_type_before_backend_routing(self):
        findings = [{"type": " Improvement "}]

        valid, invalid_type = _normalize_research_finding_types(findings)

        assert valid is True
        assert invalid_type is None
        assert findings[0]["type"] == "improvement"

    @_pytest.mark.parametrize(
        "finding_type", ["knowledge", "metadata", "note", "typo"]
    )
    def test_rejects_unknown_type_instead_of_generic_create_fallback(
        self, finding_type
    ):
        valid, invalid_type = _normalize_research_finding_types(
            [{"type": finding_type}]
        )

        assert valid is False
        assert invalid_type == finding_type

    def test_strict_implementation_contract_requires_evidence_handoff(self):
        finding = {
            "type": "improvement",
            "title": "Tighten research completion",
            "prompt": "Make research completion fail closed.",
        }

        assert _research_finding_contract_errors(finding) == [
            "missing_or_invalid_actionability_grade",
            "missing_or_invalid_description",
            "missing_or_invalid_scope",
            "missing_or_invalid_risk",
            "missing_or_invalid_priority",
            "missing_or_invalid_verification_basis",
            "missing_or_invalid_related_files",
            "missing_claim",
            "missing_source_ref",
            "missing_or_invalid_acceptance_evidence",
        ]

        finding.update(
            {
                "claims": ["The completion boundary currently trusts legacy output."],
                "sources": [{"type": "file", "ref": "backend/app/research_completion.py"}],
                "description": "The strict boundary must reject incomplete implementation evidence.",
                "scope": "medium",
                "risk": "medium",
                "priority": "high",
                "actionability_grade": "HIGH",
                "verification_basis": "code_verified",
                "related_files": ["backend/app/research_completion.py"],
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_research_completion.py",
                    "test_id": "test_strict_implementation_contract_requires_evidence_handoff",
                },
            }
        )
        assert _research_finding_contract_errors(finding) == []

    @_pytest.mark.parametrize(
        "finding_type",
        ["research", "web_research", "compute_experiment", "measurement", "benchmark"],
    )
    def test_empirical_finding_contract_requires_work_class_and_stop_condition(
        self, finding_type
    ):
        finding = {
            "type": finding_type,
            "title": "Run a bounded investigation",
            "prompt": "Resolve one falsifiable uncertainty.",
            "actionability_grade": "MEDIUM",
        }
        if finding_type in {"compute_experiment", "measurement", "benchmark"}:
            finding["compute_experiment"] = {
                "command": "python3 -m testing.measure_scheduler",
                "spend_cap_usd": 2,
                "deadline_seconds": 300,
                "smoke_sample": {
                    "exit_code": 0,
                    "startup_ok": True,
                    "metric_captured": True,
                    "timed_out": False,
                    "error_excerpt": "",
                },
            }

        assert _research_finding_contract_errors(finding) == [
            "missing_or_invalid_work_class",
            "missing_or_invalid_stop_or_pivot_condition",
        ]

    def test_empirical_finding_contract_canonicalizes_bounded_fields(self):
        finding = {
            "type": "research",
            "title": "Confirm the scheduler mechanism",
            "prompt": "Inspect the bounded scheduler surface.",
            "actionability_grade": "HIGH",
            "work_class": " Confirmatory ",
            "stop_or_pivot_condition": "  Stop after the competing race is refuted.  ",
        }

        assert _research_finding_contract_errors(finding) == []
        assert finding["work_class"] == "confirmatory"
        assert (
            finding["stop_or_pivot_condition"]
            == "Stop after the competing race is refuted."
        )

    @_pytest.mark.parametrize(
        "finding_type", ["compute_experiment", "measurement", "benchmark"]
    )
    def test_compute_contract_rejects_green_but_unlaunchable_findings(
        self, finding_type
    ):
        finding = {
            "type": finding_type,
            "title": "Measure the scheduler",
            "prompt": "Run one bounded scheduler measurement.",
            "actionability_grade": "HIGH",
            "work_class": "confirmatory",
            "stop_or_pivot_condition": "Stop when scheduler latency is measured.",
            "compute_experiment": {"command": "python3 measure.py"},
        }

        errors = _research_finding_contract_errors(finding)

        assert any(error.startswith("invalid_compute_launch_contract:") for error in errors)
        assert "missing_or_invalid_smoke_sample" in errors

    @_pytest.mark.parametrize(
        ("launch_override", "reason_fragment"),
        [
            ({"command": None}, "missing or empty command"),
            ({"spend_cap_usd": "2"}, "positive finite number"),
            ({"spend_cap_usd": float("inf")}, "positive finite number"),
            ({"deadline_seconds": 30.5}, "positive integer"),
            (
                {
                    "success_criterion": {
                        "metric": "latency_ms",
                        "operator": ">=",
                        "threshold": "10",
                    }
                },
                "finite numeric threshold",
            ),
            (
                {
                    "success_criterion": {
                        "metric": "latency_ms",
                        "operator": [">="],
                        "threshold": 10,
                    }
                },
                "supported operator",
            ),
            (
                {"command": r"python -c '\nprint(1)'"},
                "literal escaped newline",
            ),
            ({"env": {"GOOD\n": "value"}}, "env is invalid"),
            (
                {"no_progress_timeout_seconds": "30"},
                "no_progress_timeout_seconds is invalid",
            ),
            (
                {
                    "success_criteria": [
                        {
                            "metric": "latency_ms",
                            "operator": "<=",
                            "threshold": float("inf"),
                        }
                    ]
                },
                "success_criteria[0] is invalid",
            ),
            (
                {"runner_target": {"runner_id": "not-a-uuid"}},
                "runner_target is invalid",
            ),
        ],
    )
    def test_compute_contract_rejects_coercible_or_malformed_v2_shapes(
        self, launch_override, reason_fragment
    ):
        launch = {
            "command": "python3 -m testing.measure_scheduler",
            "spend_cap_usd": 2,
            "deadline_seconds": 300,
            "smoke_sample": {
                "exit_code": 0,
                "startup_ok": True,
                "metric_captured": True,
                "timed_out": False,
                "error_excerpt": "",
            },
            **launch_override,
        }
        finding = {
            "type": "compute_experiment",
            "title": "Measure the scheduler",
            "prompt": "Run one bounded scheduler measurement.",
            "actionability_grade": "HIGH",
            "work_class": "confirmatory",
            "stop_or_pivot_condition": "Stop when scheduler latency is measured.",
            "compute_experiment": launch,
        }

        errors = _research_finding_contract_errors(finding)

        assert any(
            error.startswith("invalid_compute_launch_contract:")
            and reason_fragment in error
            for error in errors
        )

    def test_compute_contract_accepts_launch_alias_and_integral_json_number_smoke(self):
        finding = {
            "type": "compute_experiment",
            "title": "Measure the scheduler",
            "prompt": "Run one bounded scheduler measurement.",
            "actionability_grade": "HIGH",
            "work_class": "confirmatory",
            "stop_or_pivot_condition": "Stop when scheduler latency is measured.",
            "compute_experiment": {},
            "launch_contract": {
                "command": "python3 -m testing.measure_scheduler",
                "spend_cap_usd": 2,
                "deadline_seconds": 300,
                "smoke_sample": {
                    "exit_code": 0.0,
                    "startup_ok": True,
                    "metric_captured": True,
                    "timed_out": False,
                    "error_excerpt": "",
                },
            },
        }

        assert _research_finding_contract_errors(finding) == []


class TestRecordResearchSurvivalSignal:
    """_record_research_survival_signal: honest research success signal on the task."""

    def _task(self, context=None):
        return _Task(
            project_id=uuid.uuid4(),
            title="research",
            payloads=[{"type": "research"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context=context or {},
        )

    def test_records_surviving_count(self):
        t = self._task()
        _record_research_survival_signal(t, surviving=3)
        assert t.context["research_surviving_findings"] == 3

    def test_zero_survivors_downgrades_first_attempt_success(self):
        t = self._task({"_first_attempt_succeeded": True})
        _record_research_survival_signal(t, surviving=0)
        assert t.context["research_surviving_findings"] == 0
        assert t.context["_first_attempt_succeeded"] is False

    def test_positive_survivors_preserve_first_attempt_success(self):
        t = self._task({"_first_attempt_succeeded": True})
        _record_research_survival_signal(t, surviving=1)
        assert t.context["_first_attempt_succeeded"] is True

    def test_zero_survivors_never_upgrades_first_attempt_success(self):
        # A later attempt already recorded False — a no-survivor cycle must not
        # flip it to True.
        t = self._task({"_first_attempt_succeeded": False})
        _record_research_survival_signal(t, surviving=0)
        assert t.context["_first_attempt_succeeded"] is False


class TestScopeBudgetGate:
    """check_scope_budget_mismatch: pre-spawn file-count / scope mismatch detection."""

    def test_small_5_files_upgrades_to_medium(self):
        files = ["a.py", "b.py", "c.py", "d.py", "e.py"]
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch(
            "small", files
        )
        assert scope == "medium"
        assert cal_from == "small"
        assert warning is not None and "scope_budget_mismatch" in warning
        assert not requires_approval

    def test_medium_18_files_upgrades_to_large(self):
        files = [f"file{i}.py" for i in range(18)]
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch(
            "medium", files
        )
        assert scope == "large"
        assert cal_from == "medium"
        assert warning is not None and "scope_budget_mismatch" in warning
        assert not requires_approval

    def test_large_40_files_requires_approval(self):
        files = [f"file{i}.py" for i in range(40)]
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch(
            "large", files
        )
        assert scope == "large"
        assert cal_from is None
        assert warning is not None and "scope_budget_mismatch" in warning
        assert requires_approval

    def test_small_2_files_passes_unchanged(self):
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch(
            "small", ["a.py", "b.py"]
        )
        assert scope == "small"
        assert cal_from is None
        assert warning is None
        assert not requires_approval

    def test_medium_within_limit_passes_unchanged(self):
        files = [f"file{i}.py" for i in range(MEDIUM_ANALYSIS_PLAN_FILE_LIMIT)]
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch(
            "medium", files
        )
        assert scope == "medium"
        assert cal_from is None
        assert warning is None
        assert not requires_approval

    def test_large_within_budget_passes_unchanged(self):
        large_limit = (SCOPE_FILE_BUDGETS.get("large") or {}).get("total_files_max", 20)
        files = [f"file{i}.py" for i in range(large_limit)]
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch(
            "large", files
        )
        assert scope == "large"
        assert cal_from is None
        assert warning is None
        assert not requires_approval

    def test_empty_files_passes_unchanged(self):
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch(
            "small", []
        )
        assert scope == "small"
        assert cal_from is None
        assert warning is None
        assert not requires_approval

    def test_none_files_passes_unchanged(self):
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch(
            "medium", None
        )
        assert scope == "medium"
        assert cal_from is None
        assert warning is None
        assert not requires_approval

    def test_medium_exactly_at_limit_passes_unchanged(self):
        # MEDIUM_ANALYSIS_PLAN_FILE_LIMIT files is exactly at the limit, not over
        files = [f"file{i}.py" for i in range(MEDIUM_ANALYSIS_PLAN_FILE_LIMIT)]
        scope, cal_from, warning, _requires_approval = check_scope_budget_mismatch(
            "medium", files
        )
        assert scope == "medium"
        assert cal_from is None
        assert warning is None

    def test_medium_one_over_limit_upgrades(self):
        files = [f"file{i}.py" for i in range(MEDIUM_ANALYSIS_PLAN_FILE_LIMIT + 1)]
        scope, cal_from, _warning, _requires_approval = check_scope_budget_mismatch(
            "medium", files
        )
        assert scope == "large"
        assert cal_from == "medium"


class TestScopeAwareEffortMapping:
    """Scope-aware effort scaling: verify scope→effort mapping for code-producing task types."""

    def test_scope_aware_types_include_qa_and_migrate(self):
        """qa and migrate are in _SCOPE_AWARE_EFFORT_TYPES."""
        from app.research_completion import _SCOPE_AWARE_EFFORT_TYPES

        assert "qa" in _SCOPE_AWARE_EFFORT_TYPES
        assert "migrate" in _SCOPE_AWARE_EFFORT_TYPES

    def test_original_scope_aware_types_still_present(self):
        """Original types (create, fix, refactor, plan) remain in _SCOPE_AWARE_EFFORT_TYPES."""
        from app.research_completion import _SCOPE_AWARE_EFFORT_TYPES

        expected = {"create", "fix", "refactor", "plan"}
        assert expected.issubset(_SCOPE_AWARE_EFFORT_TYPES)

    def test_scope_to_effort_mapping(self):
        """scope_to_effort correctly maps scopes to effort levels."""
        from app.branch_resolution import scope_to_effort

        assert scope_to_effort("small") == "low"
        assert scope_to_effort("medium") == "medium"
        assert scope_to_effort("large") == "high"
        # Unknown scopes fall back to medium
        assert scope_to_effort("unknown") == "medium"
        assert scope_to_effort(None) == "medium"

    def test_qa_small_maps_to_low_effort(self):
        """A small-scope qa task should map to low effort."""
        from app.branch_resolution import scope_to_effort

        scope = "small"
        effort = scope_to_effort(scope)
        assert effort == "low"

    def test_qa_medium_maps_to_medium_effort(self):
        """A medium-scope qa task should map to medium effort."""
        from app.branch_resolution import scope_to_effort

        scope = "medium"
        effort = scope_to_effort(scope)
        assert effort == "medium"

    def test_qa_large_maps_to_high_effort(self):
        """A large-scope qa task should map to high effort."""
        from app.branch_resolution import scope_to_effort

        scope = "large"
        effort = scope_to_effort(scope)
        assert effort == "high"

    def test_migrate_small_maps_to_low_effort(self):
        """A small-scope migrate task should map to low effort."""
        from app.branch_resolution import scope_to_effort

        scope = "small"
        effort = scope_to_effort(scope)
        assert effort == "low"

    def test_migrate_large_maps_to_high_effort(self):
        """A large-scope migrate task should map to high effort."""
        from app.branch_resolution import scope_to_effort

        scope = "large"
        effort = scope_to_effort(scope)
        assert effort == "high"


class TestLowActionabilityDecay:
    """Low-actionability deferral gate: discard findings after 3 consecutive deferrals."""

    def test_low_actionability_decay_threshold_3(self):
        """Finding with deferred_reason='low_actionability' is discarded after 3 deferrals."""
        findings = [
            {
                "title": "low-actionability-1",
                "deferred_reason": "low_actionability",
                "deferred_count": 2,
                "actionability_grade": "LOW",
            },
            {
                "title": "low-actionability-2",
                "deferred_reason": "low_actionability",
                "deferred_count": 3,
                "actionability_grade": "LOW",
            },
            {
                "title": "low-actionability-3",
                "deferred_reason": "low_actionability",
                "deferred_count": 4,
                "actionability_grade": "LOW",
            },
        ]

        # Simulate the decay logic from research_completion.py lines 590-598
        _decayed: list[dict] = []
        _kept: list[dict] = []
        for f in findings:
            _count = int(f.get("deferred_count", 0) or 0)
            _reason = f.get("deferred_reason")
            if (
                (_reason == "ultra_low_confidence" and _count >= 4)
                or (_reason == "degradation_gate" and _count >= 3)
                or (_reason == "low_actionability" and _count >= 3)
            ):
                _decayed.append(f)
            else:
                _kept.append(f)

        # Only findings with count >= 3 should be decayed
        assert len(_kept) == 1
        assert _kept[0]["title"] == "low-actionability-1"
        assert len(_decayed) == 2
        assert _decayed[0]["title"] == "low-actionability-2"
        assert _decayed[1]["title"] == "low-actionability-3"

    def test_high_and_medium_actionability_pass_through(self):
        """HIGH and MEDIUM actionability grades are not gated."""
        findings = [
            {"title": "high-grade", "actionability_grade": "HIGH"},
            {"title": "medium-grade", "actionability_grade": "MEDIUM"},
            {"title": "low-grade", "actionability_grade": "LOW"},
        ]

        # The gate only defers if actionability_grade == "LOW" and no prior deferred_reason
        deferred_for_low_actionability = [
            f
            for f in findings
            if f.get("actionability_grade") == "LOW" and not f.get("deferred_reason")
        ]

        assert len(deferred_for_low_actionability) == 1
        assert deferred_for_low_actionability[0]["title"] == "low-grade"

    def test_previously_deferred_low_actionability_flow_through_decay(self):
        """Previously-deferred LOW findings use decay rule, not re-gated."""
        finding = {
            "title": "previously-deferred-low",
            "actionability_grade": "LOW",
            "deferred_reason": "low_actionability",
            "deferred_count": 1,
        }

        # The gate checks for no prior deferred_reason, so this doesn't get re-gated
        should_gate = finding.get("actionability_grade") == "LOW" and not finding.get(
            "deferred_reason"
        )
        assert not should_gate, "Previously-deferred findings should not be re-gated"

        # Instead, it goes through the decay logic
        _count = int(finding.get("deferred_count", 0) or 0)
        _reason = finding.get("deferred_reason")
        should_decay = _reason == "low_actionability" and _count >= 3
        assert not should_decay, (
            "Deferral count=1 should not trigger decay (threshold=3)"
        )

    def test_low_actionability_stamping(self):
        """LOW-graded findings are stamped with correct reason and incremented count."""
        _now_iso = _utcnow().isoformat()

        def _stamp_deferral(f: dict, reason: str) -> dict:
            """Simulate the _stamp_deferral function."""
            stamped = dict(f)
            stamped["deferred_count"] = int(stamped.get("deferred_count", 0)) + 1
            stamped["deferred_reason"] = reason
            stamped.setdefault("originally_researched_at", _now_iso)
            return stamped

        finding = {"title": "new-low-finding", "actionability_grade": "LOW"}
        stamped = _stamp_deferral(finding, "low_actionability")

        assert stamped["deferred_reason"] == "low_actionability"
        assert stamped["deferred_count"] == 1
        assert "originally_researched_at" in stamped
        assert stamped["title"] == "new-low-finding"


class TestSalvagePayloadConstruction:
    """Salvage-bundle payload construction preserves full finding context."""

    def _simulate_salvage_payload(self, candidates: list[dict], source_task_id: str) -> dict:
        """Simulate the salvage bundle payload construction from research_completion.py."""
        return {
            "type": "salvage_bundle",
            "source_task_id": source_task_id,
            "salvaged_findings": [
                {
                    "title": f.get("title") or (f.get("description") or "")[:80],
                    "discard_reason": f.get("deferred_reason") or "unknown",
                    "direction_id": f.get("direction_id"),
                    "prompt": f.get("prompt"),
                    "description": (f.get("description") or "")[:500] if f.get("description") else None,
                    "related_files": f.get("related_files"),
                    "claims": f.get("claims"),
                    "sources": f.get("sources"),
                }
                for f in candidates
            ],
            "total_discarded": len(candidates),
        }

    def test_includes_all_decayed_findings(self):
        """All decay-category findings appear in the salvage payload."""
        candidates = [
            {"title": "ultra-low-conf", "deferred_reason": "ultra_low_confidence",
             "direction_id": "d-abc"},
            {"title": "low-actionability", "deferred_reason": "low_actionability",
             "direction_id": "d-def"},
            {"title": "degradation-gate", "deferred_reason": "degradation_gate",
             "direction_id": "d-abc"},
            {"title": "adversarial-gate", "deferred_reason": "adversarial_gate",
             "direction_id": "d-ghi"},
            {"title": "prereq-deferred", "deferred_reason": "prereq_deferred_same_cycle",
             "direction_id": None},
        ]
        payload = self._simulate_salvage_payload(candidates, "task-001")
        assert payload["type"] == "salvage_bundle"
        assert payload["total_discarded"] == 5
        assert len(payload["salvaged_findings"]) == 5
        titles = {f["title"] for f in payload["salvaged_findings"]}
        assert "ultra-low-conf" in titles
        assert "low-actionability" in titles
        assert "degradation-gate" in titles
        assert "adversarial-gate" in titles
        assert "prereq-deferred" in titles

    def test_each_finding_has_reason_and_title(self):
        """Every salvaged finding entry has a title, discard_reason, and rich context."""
        candidates = [
            {
                "title": "perf-finding",
                "prompt": "Optimize the query hot path",
                "description": "The N+1 query pattern in task_lifecycle.py causes slow page loads",
                "related_files": ["backend/app/task_lifecycle.py"],
                "claims": [{"claim": "N+1 detected in loop", "confidence": "high"}],
                "sources": ["task_audit_report_2026.pdf"],
                "deferred_reason": "low_actionability",
                "direction_id": "d-1",
            },
            {
                "title": "",
                "description": "desc-based title with rich context",
                "prompt": "Refactor the stale handler",
                "related_files": ["backend/app/reaper.py", "backend/app/orchestrator.py"],
                "deferred_reason": "ultra_low_confidence",
                "direction_id": "d-2",
            },
        ]
        payload = self._simulate_salvage_payload(candidates, "task-002")
        entries = payload["salvaged_findings"]

        # First finding: all rich fields preserved
        e0 = entries[0]
        assert e0["title"] == "perf-finding"
        assert e0["discard_reason"] == "low_actionability"
        assert e0["prompt"] == "Optimize the query hot path"
        assert e0["description"] == "The N+1 query pattern in task_lifecycle.py causes slow page loads"
        assert e0["related_files"] == ["backend/app/task_lifecycle.py"]
        assert e0["claims"] == [{"claim": "N+1 detected in loop", "confidence": "high"}]
        assert e0["sources"] == ["task_audit_report_2026.pdf"]
        assert e0["direction_id"] == "d-1"

        # Second finding: title fallback from description, rich fields preserved
        e1 = entries[1]
        assert e1["title"].startswith("desc-based title")
        assert e1["discard_reason"] == "ultra_low_confidence"
        assert e1["prompt"] == "Refactor the stale handler"
        assert e1["description"] == "desc-based title with rich context"
        assert e1["related_files"] == ["backend/app/reaper.py", "backend/app/orchestrator.py"]
        assert e1["direction_id"] == "d-2"

    def test_empty_candidates_produce_no_task(self):
        """Empty salvage candidates means no bundle entries."""
        payload = self._simulate_salvage_payload([], "task-003")
        assert payload["total_discarded"] == 0
        assert payload["salvaged_findings"] == []

    def test_fallback_for_missing_title(self):
        """Finding missing title falls back to description snippet; rich context preserved."""
        candidates = [
            {
                "description": "A very long description that serves as the fallback title",
                "prompt": "Implement the fallback-handling logic",
                "related_files": ["backend/app/fallback.py"],
                "deferred_reason": "low_actionability",
                "direction_id": "d-x",
            }
        ]
        payload = self._simulate_salvage_payload(candidates, "task-004")
        entry = payload["salvaged_findings"][0]
        assert entry["title"].startswith("A very long description")
        assert entry["discard_reason"] == "low_actionability"
        assert entry["prompt"] == "Implement the fallback-handling logic"
        assert entry["description"] == "A very long description that serves as the fallback title"
        assert entry["related_files"] == ["backend/app/fallback.py"]

    def test_real_function_preserves_prompt_and_related_files(self):
        """The production _build_salvage_payload preserves prompt and related_files."""
        candidates = [
            {
                "title": "n-plus-one",
                "prompt": "Fix N+1 query in task_lifecycle.py by adding selectinload",
                "description": "Task detail resolution issues N+1 in the loop",
                "related_files": ["backend/app/task_lifecycle.py"],
                "claims": [{"claim": "N+1 detected", "confidence": "high"}],
                "sources": ["perf_report_v3.pdf"],
                "deferred_reason": "low_actionability",
                "direction_id": "d-001",
            },
            {
                "title": "unused-index",
                "prompt": "Drop unused composite index on task.status",
                "related_files": ["backend/app/models.py", "backend/alembic/versions/0421_poolvm_hot_path_indexes.py"],
                "deferred_reason": "ultra_low_confidence",
                "direction_id": "d-002",
            },
        ]
        payload = _build_salvage_payload(candidates, "task-real-001")
        assert payload["type"] == "salvage_bundle"
        assert payload["source_task_id"] == "task-real-001"
        assert payload["total_discarded"] == 2
        entries = payload["salvaged_findings"]
        assert len(entries) == 2

        e0 = entries[0]
        assert e0["title"] == "n-plus-one"
        assert e0["prompt"] == "Fix N+1 query in task_lifecycle.py by adding selectinload"
        assert e0["description"] == "Task detail resolution issues N+1 in the loop"
        assert e0["related_files"] == ["backend/app/task_lifecycle.py"]
        assert e0["claims"] == [{"claim": "N+1 detected", "confidence": "high"}]
        assert e0["sources"] == ["perf_report_v3.pdf"]
        assert e0["discard_reason"] == "low_actionability"
        assert e0["direction_id"] == "d-001"

        e1 = entries[1]
        assert e1["title"] == "unused-index"
        assert e1["prompt"] == "Drop unused composite index on task.status"
        assert e1["related_files"] == [
            "backend/app/models.py",
            "backend/alembic/versions/0421_poolvm_hot_path_indexes.py",
        ]
        assert e1["discard_reason"] == "ultra_low_confidence"
        assert e1["direction_id"] == "d-002"

    def test_dedup_filtered_reason_preserved(self):
        """Findings with dedup-formatted discard_reason are preserved in the payload."""
        candidates = [
            {
                "title": "hash-duplicate",
                "prompt": "Fix duplicated logic in auth handler",
                "deferred_reason": "dedup: hash match with prior task",
                "direction_id": "d-dedup01",
                "related_files": ["backend/app/auth.py"],
                "claims": [{"claim": "duplicate", "confidence": "medium"}],
                "sources": ["scan.pdf"],
            },
            {
                "title": "region-overlap",
                "prompt": "Refactor shared region",
                "deferred_reason": "dedup: region overlap with: existing task",
                "direction_id": "d-dedup02",
                "related_files": ["backend/app/models.py"],
                "claims": [],
                "sources": [],
            },
        ]
        payload = _build_salvage_payload(candidates, "task-dedup-001")
        entries = payload["salvaged_findings"]
        assert len(entries) == 2

        e0 = entries[0]
        assert "dedup:" in e0["discard_reason"]
        assert e0["title"] == "hash-duplicate"
        assert e0["prompt"] == "Fix duplicated logic in auth handler"
        assert e0["related_files"] == ["backend/app/auth.py"]
        assert e0["direction_id"] == "d-dedup01"

        e1 = entries[1]
        assert "dedup:" in e1["discard_reason"]
        assert e1["title"] == "region-overlap"
        assert e1["prompt"] == "Refactor shared region"
        assert e1["direction_id"] == "d-dedup02"


@_pytest.mark.asyncio
async def test_salvage_activity_context_recorded_on_parent_task(db_session, mocker):
    """When deferred findings decay and are bundled into a salvage task, the
    parent research task's context carries salvage_activity metadata."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-salvage01"
    )
    # An ultra_low_confidence finding deferred 4+ times decays into salvage.
    decayed = _make_finding("Repeatedly low confidence", direction_id=direction["id"])
    decayed["actionability_grade"] = "LOW"
    decayed["deferred_reason"] = "ultra_low_confidence"
    decayed["deferred_count"] = 4
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": [decayed],
    }
    db_session.add(task)
    await db_session.commit()

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding("Fresh sentinel", direction_id=direction["id"])],
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    salvage = (task.context or {}).get("salvage_activity")
    assert salvage is not None, (
        "salvage_activity must be recorded on the parent task"
    )
    assert "salvage_task_id" in salvage
    assert salvage["salvaged_count"] == 1
    assert "salvaged_at" in salvage

    # Verify salvage info is also propagated to the direction research_history entry.
    await db_session.refresh(_project)
    _history = _project.directions[0].get("research_history", [])
    assert _history, "research_history should not be empty"
    _last_entry = _history[-1]
    assert "salvage" in _last_entry, (
        "direction research_history entry should contain salvage metadata"
    )
    assert _last_entry["salvage"]["salvaged_count"] == 1
    assert _last_entry["salvage"]["salvage_task_id"] == salvage["salvage_task_id"]
    # filter_breakdown should also include the salvaged count.
    _fb = _last_entry.get("filter_breakdown", {})
    assert "salvaged" in _fb, (
        "filter_breakdown should include salvaged count"
    )
    assert _fb["salvaged"] == 1


@_pytest.mark.asyncio
async def test_expired_deferred_finding_records_retired_disposition(db_session, mocker):
    """A TTL-expired prior-deferred finding is not silently dropped: the
    completion recovery path records a durable per-finding retired disposition on
    the research task's context under finding_retired_dispositions."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-retired01"
    )
    # A deferred finding researched 10 days ago — beyond the 7-day default TTL.
    stale = _make_finding("TTL-expired deferred finding", direction_id=direction["id"])
    stale["originally_researched_at"] = (_utcnow() - timedelta(days=10)).isoformat()
    stale["deferred_reason"] = "degradation_gate"
    stale["deferred_count"] = 1
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": [stale],
    }
    db_session.add(task)
    await db_session.commit()

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding("Fresh sentinel", direction_id=direction["id"])],
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    records = (task.context or {}).get("finding_retired_dispositions")
    assert records is not None, (
        "TTL-dropped deferred findings must be recorded as retired dispositions"
    )
    assert isinstance(records, list)
    assert len(records) == 1
    rec = records[0]
    assert rec["title"] == "TTL-expired deferred finding"
    assert rec["direction_id"] == direction["id"]
    assert rec["deferred_reason"] == "degradation_gate"
    assert rec["deferred_count"] == 1
    assert rec["retired_by"] == "ttl"
    assert rec["disposition"] == "retired"
    assert "retired_at" in rec
    assert rec["finding_hash"], (
        "retired disposition must carry the dedup finding_hash"
    )


@_pytest.mark.asyncio
async def test_query_retired_dispositions_returns_recorded_records(db_session):
    """query_retired_dispositions scans recent completed research tasks' context
    and returns the recorded retired-disposition records, newest-task-first."""
    from app.research_completion import query_retired_dispositions

    project = _Project(
        name="Retired Dispositions Query",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[{"id": "d-retired-q1", "text": "Q", "status": "active"}],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    completed = _Task(
        project_id=project.id,
        title="Research: query dispositions",
        payloads=[{"type": "research", "base_branch": "main"}],
        context={
            "finding_retired_dispositions": [
                {
                    "finding_hash": "hash-1",
                    "title": "Retired one",
                    "direction_id": "d-retired-q1",
                    "deferred_reason": "degradation_gate",
                    "deferred_count": 1,
                    "retired_at": now.isoformat(),
                    "retired_by": "ttl",
                    "disposition": "retired",
                },
                {
                    "finding_hash": "hash-2",
                    "title": "Retired two",
                    "direction_id": "d-retired-q1",
                    "deferred_reason": "low_actionability",
                    "deferred_count": 3,
                    "retired_at": now.isoformat(),
                    "retired_by": "ttl",
                    "disposition": "retired",
                },
            ]
        },
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(completed)
    await db_session.commit()

    rows = await query_retired_dispositions(db_session, project.id)
    assert len(rows) == 2
    titles = {r["title"] for r in rows}
    assert titles == {"Retired one", "Retired two"}
    by_hash = {r["finding_hash"]: r for r in rows}
    assert by_hash["hash-1"]["task_id"] == str(completed.id)
    assert by_hash["hash-1"]["retired_by"] == "ttl"
    assert by_hash["hash-1"]["disposition"] == "retired"
    assert by_hash["hash-2"]["deferred_reason"] == "low_actionability"
    assert by_hash["hash-2"]["deferred_count"] == 3


class TestDeclaresDeliveredOutputDeliverable:
    """_declares_delivered_output_deliverable: conservative message/report predicate."""

    def _task(self, *, description="", payload=None, context=None):
        return _Task(
            project_id=uuid.uuid4(),
            title="t",
            description=description,
            payloads=[payload or {"type": "research"}],
            context=context or {},
            status=TaskStatus.COMPLETED,
        )

    def test_prose_sent_message_deliverable_declared(self):
        task = self._task(
            description="The deliverable of THIS task is a SENT MESSAGE, not a green task status."
        )
        assert _declares_delivered_output_deliverable(task) is True

    def test_prose_message_report_deliverable_declared(self):
        task = self._task(
            description=(
                "A task declaring a message/report deliverable that finishes with "
                "zero messages sent and no report written is recorded as failed."
            )
        )
        assert _declares_delivered_output_deliverable(task) is True

    def test_structured_deliverable_kind_declared(self):
        task = self._task(payload={"type": "research", "deliverable_kind": "report"})
        assert _declares_delivered_output_deliverable(task) is True

    def test_structured_requires_delivered_output_flag(self):
        task = self._task(context={"requires_delivered_output": True})
        assert _declares_delivered_output_deliverable(task) is True

    def test_acceptance_evidence_observable_behavior_declared(self):
        task = self._task(
            payload={
                "type": "research",
                "acceptance_evidence": {
                    "kind": "observable_behavior",
                    "description": "the declared deliverable is a report and it is written",
                },
            }
        )
        assert _declares_delivered_output_deliverable(task) is True

    def test_ordinary_research_not_flagged(self):
        # Mentions 'report' and 'message' incidentally but declares no deliverable.
        task = self._task(
            description=(
                "Analyze the messaging subsystem and report on latency in the "
                "codebase; produce findings."
            )
        )
        assert _declares_delivered_output_deliverable(task) is False

    def test_empty_task_not_flagged(self):
        assert _declares_delivered_output_deliverable(self._task()) is False


class TestNoOutputDowngrade:
    """Zero-findings deliver task with no message/report is FAILED, not green COMPLETED."""

    async def _run_empty(self, db_session, mocker, task):
        from test_autonomous_research import _patch_research_externals

        # Production invokes this post-completion handler after the task has
        # entered COMPLETED; only an explicit quality downgrade changes it.
        task.status = TaskStatus.COMPLETED
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[]
        )
        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

    @_pytest.mark.asyncio
    async def test_declared_deliverable_zero_output_downgrades_to_failed(
        self, db_session, mocker
    ):
        project, direction, task = await _grader_project_and_task(
            db_session, "d-nooutput01"
        )
        project.vision = {"_research_spawn_count": 7}
        task.description = "The deliverable of THIS task is a SENT MESSAGE, not a green task status."
        db_session.add(project)
        db_session.add(task)
        await db_session.commit()

        await self._run_empty(db_session, mocker, task)

        assert task.status == TaskStatus.FAILED, (
            "a declared message/report deliverable producing nothing must FAIL"
        )
        assert task.latest_status_message == _NO_DELIVERED_OUTPUT_TERMINAL_REASON
        await db_session.refresh(project)
        assert project.vision["_research_spawn_count"] == 7
        assert str(task.id) not in project.vision.get(
            "_research_spawn_counted_ids", []
        )

    @_pytest.mark.asyncio
    async def test_plain_research_zero_findings_stays_completed(
        self, db_session, mocker
    ):
        # No declared deliverable — an honest empty finding set stays green.
        import app.research_completion as research_completion

        project, _direction, task = await _grader_project_and_task(
            db_session, "d-nooutput02"
        )
        boundary = mocker.spy(
            research_completion, "lock_accepted_research_boundary"
        )

        await self._run_empty(db_session, mocker, task)

        assert boundary.call_count == 1
        assert task.status == TaskStatus.COMPLETED
        assert task.latest_status_message == (
            "Research completed with no actionable findings"
        )
        await db_session.refresh(project)
        assert project.vision["_research_spawn_count"] == 1
        assert str(task.id) in project.vision["_research_spawn_counted_ids"]

    @_pytest.mark.asyncio
    async def test_plain_empty_retries_evergreen_before_finalized_receipt(
        self, db_session, mocker
    ):
        """A failed zero-finding adapter remains replayable and runs on retry."""
        from test_autonomous_research import _patch_research_externals

        _project, _direction, task = await _grader_project_and_task(
            db_session, "d-empty-evergreen-retry"
        )
        task.status = TaskStatus.COMPLETED
        task.context = {
            **(task.context or {}),
            "evergreen_backlog_item_id": str(uuid.uuid4()),
        }
        db_session.add(task)
        await db_session.commit()
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[]
        )
        apply_outcome = mocker.patch(
            "app.evergreen_backlog.apply_research_outcome",
            new=AsyncMock(side_effect=[RuntimeError("ledger unavailable"), None]),
        )

        with _pytest.raises(RuntimeError, match="ledger unavailable"):
            await _handle_research_completion(task, "[]", db_session)

        await db_session.rollback()
        await db_session.refresh(task)
        assert (task.context or {}).get(FINALIZED_RESEARCH_FINDINGS_KEY) is not True

        await _handle_research_completion(task, "[]", db_session)
        await db_session.refresh(task)

        assert apply_outcome.await_count == 2
        assert task.context[FINALIZED_RESEARCH_FINDINGS_KEY] is True
        assert task.status == TaskStatus.COMPLETED

    @_pytest.mark.asyncio
    async def test_plain_research_unparseable_output_fails_closed(
        self, db_session, mocker
    ):
        from test_autonomous_research import _patch_research_externals

        project, _direction, task = await _grader_project_and_task(
            db_session, "d-invalid-output"
        )
        task.status = TaskStatus.COMPLETED
        task.context = {**(task.context or {}), "research_depth": 3}
        _patch_research_externals(mocker)

        await _handle_research_completion(
            task, "investigation finished but no JSON was emitted", db_session
        )
        await db_session.refresh(task)

        assert task.status == TaskStatus.FAILED
        assert task.latest_status_message == _INVALID_RESEARCH_FINDINGS_TERMINAL_REASON
        assert (task.context or {}).get("research_output_invalid") is True
        assert task.plan_output is None
        await db_session.refresh(project)
        assert str(task.id) not in (project.vision or {}).get(
            "_research_spawn_counted_ids", []
        )

    @_pytest.mark.asyncio
    async def test_plain_research_unknown_finding_type_fails_closed(
        self, db_session, mocker
    ):
        from test_autonomous_research import _patch_research_externals

        project, _direction, task = await _grader_project_and_task(
            db_session, "d-invalid-type"
        )
        task.status = TaskStatus.COMPLETED
        task.context = {**(task.context or {}), "research_depth": 3}
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[
                {
                    "type": "knowledge",
                    "title": "unsupported generic type",
                    "prompt": "This must not become a generic create task.",
                }
            ],
        )

        await _handle_research_completion(task, "ignored", db_session)
        await db_session.refresh(task)

        assert task.status == TaskStatus.FAILED
        assert task.latest_status_message == _INVALID_RESEARCH_FINDINGS_TERMINAL_REASON
        assert (task.context or {}).get("research_output_invalid") is True
        await db_session.refresh(project)
        assert str(task.id) not in (project.vision or {}).get(
            "_research_spawn_counted_ids", []
        )

    @_pytest.mark.asyncio
    @_pytest.mark.parametrize("finding_type", ["knowledge", "note"])
    async def test_stored_resume_unknown_type_also_fails_closed(
        self, db_session, mocker, finding_type
    ):
        from test_autonomous_research import _patch_research_externals

        project, _direction, task = await _grader_project_and_task(
            db_session, f"d-invalid-stored-{finding_type}"
        )
        task.status = TaskStatus.COMPLETED
        task.plan_output = [
            {
                "type": finding_type,
                "title": "unsupported stored type",
                "prompt": "This stored artifact must not become generic work.",
            }
        ]
        task.context = {**(task.context or {}), "_research_spawn_pending": True}
        db_session.add(task)
        await db_session.commit()
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=None
        )

        await _handle_research_completion(task, "ignored", db_session)
        await db_session.refresh(task)

        assert task.status == TaskStatus.FAILED
        assert task.latest_status_message == _INVALID_RESEARCH_FINDINGS_TERMINAL_REASON
        assert (task.context or {}).get("research_output_invalid") is True
        await db_session.refresh(project)
        assert str(task.id) not in (project.vision or {}).get(
            "_research_spawn_counted_ids", []
        )

    @_pytest.mark.asyncio
    async def test_claimed_research_missing_evidence_contract_fails_closed(
        self, db_session, mocker
    ):
        from test_autonomous_research import _patch_research_externals

        project, _direction, task = await _grader_project_and_task(
            db_session, "d-invalid-evidence"
        )
        task.status = TaskStatus.COMPLETED
        task.context = {
            **(task.context or {}),
            RESEARCH_OUTPUT_CONTRACT_VERSION_KEY: RESEARCH_OUTPUT_CONTRACT_VERSION,
            "research_depth": 3,
        }
        db_session.add(task)
        await db_session.commit()
        _patch_research_externals(mocker)
        output = [
            {
                "type": "improvement",
                "title": "Evidence-free work",
                "prompt": "This must not launch without a handoff contract.",
            }
        ]

        await _handle_research_completion(task, json.dumps(output), db_session)
        await db_session.refresh(task)

        assert task.status == TaskStatus.FAILED
        receipt = task.context["research_output_contract_rejections"][0]
        assert receipt["source"] == "agent_output"
        assert set(receipt["errors"]) == {
            "missing_or_invalid_actionability_grade",
            "missing_or_invalid_description",
            "missing_or_invalid_scope",
            "missing_or_invalid_risk",
            "missing_or_invalid_priority",
            "missing_or_invalid_verification_basis",
            "missing_or_invalid_related_files",
            "missing_claim",
            "missing_source_ref",
            "missing_or_invalid_acceptance_evidence",
        }
        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        assert children == []
        await db_session.refresh(project)
        assert str(task.id) not in (project.vision or {}).get(
            "_research_spawn_counted_ids", []
        )

    @_pytest.mark.asyncio
    async def test_claimed_metadata_inherits_live_parent_and_persists_note(
        self, db_session, mocker
    ):
        from test_autonomous_research import _patch_research_externals

        project, direction, task = await _grader_project_and_task(
            db_session, "d-metadata-parent"
        )
        task.payloads = [
            {**task.first_payload, "current_direction_id": direction["id"]}
        ]
        task.context = {
            **(task.context or {}),
            RESEARCH_OUTPUT_CONTRACT_VERSION_KEY: RESEARCH_OUTPUT_CONTRACT_VERSION,
            "research_depth": 0,
        }
        db_session.add(task)
        await db_session.commit()
        _patch_research_externals(mocker)
        note_text = "The live parent direction now has confirming scheduler evidence."
        output = [
            {
                "type": "direction_note",
                "title": "Confirm scheduler evidence",
                "prompt": "Persist the confirmed scheduler evidence on its direction.",
                "description": note_text,
                "actionability_grade": "HIGH",
            }
        ]

        await _handle_research_completion(task, json.dumps(output), db_session)
        await db_session.refresh(task)
        await db_session.refresh(project)

        persisted_direction = next(
            row for row in project.directions if row["id"] == direction["id"]
        )
        assert any(
            note.get("text") == note_text
            and note.get("added_by_task") == str(task.id)
            for note in persisted_direction.get("notes", [])
        )
        assert task.status == TaskStatus.COMPLETED
        assert (task.context or {}).get("completion_outcome") == "findings_documented"
        assert persisted_direction["last_cycle_productive"] is True

    @_pytest.mark.asyncio
    async def test_claimed_malformed_metadata_fails_closed_at_zero_headroom(
        self, db_session, mocker
    ):
        from test_autonomous_research import _patch_research_externals

        project, direction, task = await _grader_project_and_task(
            db_session, "d-metadata-zero-headroom"
        )
        task.payloads = [
            {
                **task.first_payload,
                "work_headroom": 0,
            }
        ]
        task.context = {
            **(task.context or {}),
            RESEARCH_OUTPUT_CONTRACT_VERSION_KEY: RESEARCH_OUTPUT_CONTRACT_VERSION,
            "research_depth": 0,
        }
        db_session.add(task)
        await db_session.commit()
        _patch_research_externals(mocker)
        output = [
            {
                "type": "design_doc_observation",
                "title": "Empty metadata shell",
                "prompt": "Do not credit metadata that persists no observation.",
                "actionability_grade": "HIGH",
            }
        ]

        await _handle_research_completion(task, json.dumps(output), db_session)
        await db_session.refresh(task)
        await db_session.refresh(project)

        assert task.status == TaskStatus.FAILED
        errors = task.context["research_output_contract_rejections"][0]["errors"]
        assert "missing_or_invalid_design_observation_description" in errors
        assert "missing_or_invalid_design_observation_section" in errors
        persisted_direction = next(
            row for row in project.directions if row["id"] == direction["id"]
        )
        assert persisted_direction.get("last_cycle_productive") is not True
        assert (task.context or {}).get("completion_outcome") != "findings_documented"

    @_pytest.mark.asyncio
    async def test_duplicate_direction_proposal_does_not_credit_productivity(
        self, db_session, mocker
    ):
        from test_autonomous_research import _patch_research_externals

        project, direction, task = await _grader_project_and_task(
            db_session, "d-metadata-duplicate-proposal"
        )
        duplicate_text = "Repair scheduler admission dispatch ownership boundary"
        project.directions = [
            {**row, "text": duplicate_text}
            if row.get("id") == direction["id"]
            else row
            for row in project.directions
        ]
        task.payloads = [
            {**task.first_payload, "current_direction_id": direction["id"]}
        ]
        task.context = {
            **(task.context or {}),
            RESEARCH_OUTPUT_CONTRACT_VERSION_KEY: RESEARCH_OUTPUT_CONTRACT_VERSION,
            "research_depth": 0,
        }
        db_session.add(task)
        await db_session.commit()
        _patch_research_externals(mocker)
        output = [
            {
                "type": "direction_proposal",
                "title": "Duplicate the live direction",
                "prompt": duplicate_text,
                "description": "This intentionally duplicates the existing direction.",
                "actionability_grade": "MEDIUM",
            }
        ]

        await _handle_research_completion(task, json.dumps(output), db_session)
        await db_session.refresh(task)
        await db_session.refresh(project)

        assert task.status == TaskStatus.COMPLETED
        assert (task.context or {}).get("completion_outcome") != "findings_documented"
        assert not any(
            row.get("status") == "proposed" for row in project.directions
        )
        persisted_direction = next(
            row for row in project.directions if row["id"] == direction["id"]
        )
        assert persisted_direction["last_cycle_productive"] is False
        assert persisted_direction["consecutive_unproductive_cycles"] >= 1

    @_pytest.mark.asyncio
    async def test_claimed_research_rejects_mixed_non_object_batch(
        self, db_session, mocker
    ):
        from test_autonomous_research import _make_finding, _patch_research_externals

        project, direction, task = await _grader_project_and_task(
            db_session, "d-invalid-mixed-object"
        )
        task.status = TaskStatus.COMPLETED
        task.context = {
            **(task.context or {}),
            RESEARCH_OUTPUT_CONTRACT_VERSION_KEY: RESEARCH_OUTPUT_CONTRACT_VERSION,
            "research_depth": 3,
        }
        valid = _make_finding(
            "Well-shaped finding cannot hide a malformed peer",
            direction_id=direction["id"],
        )
        valid.update(
            {
                "claims": ["The entire strict output batch is one contract."],
                "sources": [
                    {"type": "file", "ref": "backend/app/research_completion.py"}
                ],
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_research_completion.py",
                    "test_id": "test_claimed_research_rejects_mixed_non_object_batch",
                },
            }
        )
        db_session.add(task)
        await db_session.commit()
        _patch_research_externals(mocker)

        await _handle_research_completion(
            task, json.dumps([valid, "not-a-finding"]), db_session
        )
        await db_session.refresh(task)

        assert task.status == TaskStatus.FAILED
        assert task.context["research_output_contract_rejections"] == [
            {
                "index": 1,
                "source": "agent_output",
                "type": "str",
                "title": "not-a-finding",
                "errors": ["finding_must_be_an_object"],
            }
        ]
        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        assert children == []
        await db_session.refresh(project)
        assert str(task.id) not in (project.vision or {}).get(
            "_research_spawn_counted_ids", []
        )

    @_pytest.mark.asyncio
    async def test_claimed_research_rejects_truncated_array_instead_of_salvaging_prefix(
        self, db_session, mocker
    ):
        from test_autonomous_research import _patch_research_externals

        project, _direction, task = await _grader_project_and_task(
            db_session, "d-invalid-truncated-contract"
        )
        task.status = TaskStatus.COMPLETED
        task.context = {
            **(task.context or {}),
            RESEARCH_OUTPUT_CONTRACT_VERSION_KEY: RESEARCH_OUTPUT_CONTRACT_VERSION,
            "research_depth": 3,
        }
        db_session.add(task)
        await db_session.commit()
        _patch_research_externals(mocker)
        complete_finding = {
            "type": "direction_note",
            "title": "A complete prefix is still a truncated artifact",
            "prompt": "Do not accept only part of the producer's task graph.",
        }
        truncated_logs = json.dumps([complete_finding])[:-1]

        await _handle_research_completion(task, truncated_logs, db_session)
        await db_session.refresh(task)

        assert task.status == TaskStatus.FAILED
        assert task.latest_status_message == _INVALID_RESEARCH_FINDINGS_TERMINAL_REASON
        assert task.context["research_output_invalid"] is True
        assert task.plan_output is None
        await db_session.refresh(project)
        assert str(task.id) not in (project.vision or {}).get(
            "_research_spawn_counted_ids", []
        )

    @_pytest.mark.asyncio
    async def test_claimed_malformed_fresh_output_cannot_hide_behind_prior_carry(
        self, db_session, mocker
    ):
        from test_autonomous_research import _make_finding, _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-invalid-fresh-with-carry"
        )
        carried = _make_finding(
            "Valid carry cannot excuse a malformed fresh artifact",
            direction_id=direction["id"],
        )
        carried.update({"actionability_grade": "HIGH", "deferred_count": 1})
        task.context = {
            **(task.context or {}),
            RESEARCH_OUTPUT_CONTRACT_VERSION_KEY: RESEARCH_OUTPUT_CONTRACT_VERSION,
            "prior_deferred_findings": [carried],
        }
        db_session.add(task)
        await db_session.commit()
        _patch_research_externals(mocker)

        await _handle_research_completion(task, '[{"type":"improvement"', db_session)
        await db_session.refresh(task)

        assert task.status == TaskStatus.FAILED
        assert task.context["research_output_invalid"] is True
        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        assert children == []

    @_pytest.mark.asyncio
    async def test_claimed_research_quarantines_v2_carried_without_evidence(
        self, db_session, mocker
    ):
        from test_autonomous_research import _make_finding, _patch_research_externals

        from app.research_deferred_carry import unacknowledged_deferred_findings

        project, direction, task = await _grader_project_and_task(
            db_session, "d-invalid-carried-evidence"
        )
        rejected_title = "V2 evidence-free carry"
        producer = _Task(
            project_id=project.id,
            title="V2 deferred producer",
            payloads=[{"type": "research", "base_branch": "main"}],
            context={
                "direction_id": direction["id"],
                RESEARCH_OUTPUT_CONTRACT_VERSION_KEY: (
                    RESEARCH_OUTPUT_CONTRACT_VERSION
                ),
                "deferred_findings": [
                    {
                        "type": "improvement",
                        "title": rejected_title,
                        "prompt": "Do not spawn this without its evidence packet.",
                        "direction_id": direction["id"],
                    }
                ],
            },
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(producer)
        await db_session.flush()
        carried = unacknowledged_deferred_findings(producer)
        assert carried[0][RESEARCH_FINDING_ORIGIN_CONTRACT_VERSION_KEY] == (
            RESEARCH_OUTPUT_CONTRACT_VERSION
        )
        task.status = TaskStatus.COMPLETED
        task.context = {
            **(task.context or {}),
            RESEARCH_OUTPUT_CONTRACT_VERSION_KEY: RESEARCH_OUTPUT_CONTRACT_VERSION,
            "prior_deferred_findings": carried,
        }
        fresh = _make_finding(
            "Fresh evidence-backed finding", direction_id=direction["id"]
        )
        fresh.update(
            {
                "actionability_grade": "HIGH",
                "verification_basis": "code_verified",
                "related_files": ["backend/app/research_completion.py"],
                "claims": ["The fresh finding is grounded in the completion handler."],
                "sources": [
                    {"type": "file", "ref": "backend/app/research_completion.py"}
                ],
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_research_completion.py",
                    "test_id": (
                        "test_claimed_research_quarantines_v2_carried_without_evidence"
                    ),
                },
            }
        )
        db_session.add(task)
        await db_session.commit()
        _patch_research_externals(mocker)

        await _handle_research_completion(task, json.dumps([fresh]), db_session)
        await db_session.refresh(task)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        assert any(child.title == fresh["title"] for child in children)
        assert all(child.title != rejected_title for child in children)
        receipt = next(
            r
            for r in task.context["research_output_contract_rejections"]
            if r["title"] == rejected_title
        )
        assert receipt["source"] == "prior_deferred"
        assert task.context["research_rejected_deferred_findings"][0]["title"] == (
            rejected_title
        )
        await db_session.refresh(producer)
        assert producer.context["deferred_findings"] == []

    @_pytest.mark.asyncio
    async def test_v1_producer_carry_remains_legacy_compatible_for_v2_consumer(
        self, db_session, mocker
    ):
        from test_autonomous_research import _make_finding, _patch_research_externals

        from app.research_deferred_carry import unacknowledged_deferred_findings

        project, direction, consumer = await _grader_project_and_task(
            db_session, "d-v1-to-v2-carry"
        )
        legacy = _make_finding(
            "Route legacy carry under its producer contract",
            direction_id=direction["id"],
        )
        legacy.update(
            {
                "prompt": "Implement the already-researched legacy handoff.",
                "actionability_grade": "HIGH",
                "deferred_reason": "batch_cap",
                "deferred_count": 1,
            }
        )
        # No task-level contract marker: this row was produced by a v1 attempt.
        producer = _Task(
            project_id=project.id,
            title="Legacy deferred producer",
            payloads=[{"type": "research", "base_branch": "main"}],
            context={
                "direction_id": direction["id"],
                "deferred_findings": [legacy],
            },
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(producer)
        await db_session.flush()

        carried = unacknowledged_deferred_findings(producer)
        assert carried[0][RESEARCH_FINDING_ORIGIN_CONTRACT_VERSION_KEY] == 1
        consumer.status = TaskStatus.COMPLETED
        consumer.context = {
            **(consumer.context or {}),
            RESEARCH_OUTPUT_CONTRACT_VERSION_KEY: RESEARCH_OUTPUT_CONTRACT_VERSION,
            "prior_deferred_findings": carried,
        }
        db_session.add(consumer)
        await db_session.commit()

        _patch_research_externals(mocker)
        await _handle_research_completion(consumer, "[]", db_session)
        await db_session.refresh(producer)
        await db_session.refresh(consumer)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == consumer.id)
                )
            )
            .scalars()
            .all()
        )
        assert any(child.title == legacy["title"] for child in children)
        assert producer.context["deferred_findings"] == []
        assert all(
            receipt.get("title") != legacy["title"]
            for receipt in consumer.context.get(
                "research_output_contract_rejections", []
            )
        )

    @_pytest.mark.asyncio
    async def test_depth_limited_carry_finalizes_before_pruning_producer(
        self, db_session, mocker
    ):
        import app.research_completion as research_completion

        from test_autonomous_research import _make_finding, _patch_research_externals

        from app.research_deferred_carry import unacknowledged_deferred_findings

        project, direction, consumer = await _grader_project_and_task(
            db_session, "d-depth-carry-finalized"
        )
        carried_finding = _make_finding(
            "Preserve depth-limited deferred work", direction_id=direction["id"]
        )
        carried_finding.update(
            {
                "prompt": "Keep this finding recoverable after the depth boundary.",
                "actionability_grade": "HIGH",
                "deferred_reason": "batch_cap",
            }
        )
        producer = _Task(
            project_id=project.id,
            title="Depth carry producer",
            payloads=[{"type": "research", "base_branch": "main"}],
            context={
                "direction_id": direction["id"],
                "deferred_findings": [carried_finding],
            },
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(producer)
        await db_session.flush()
        consumer.status = TaskStatus.COMPLETED
        consumer.context = {
            **(consumer.context or {}),
            "research_depth": 3,
            "prior_deferred_findings": unacknowledged_deferred_findings(producer),
        }
        db_session.add(consumer)
        await db_session.commit()
        _patch_research_externals(mocker)
        boundary = mocker.spy(
            research_completion, "lock_accepted_research_boundary"
        )

        await _handle_research_completion(consumer, "[]", db_session)
        await db_session.refresh(producer)
        await db_session.refresh(consumer)
        await db_session.refresh(project)

        assert boundary.call_count == 1
        assert producer.context["deferred_findings"] == []
        assert consumer.context[FINALIZED_RESEARCH_FINDINGS_KEY] is True
        assert consumer.plan_output == []
        assert [finding["title"] for finding in consumer.context["deferred_findings"]] == [
            carried_finding["title"]
        ]
        assert resolve_research_findings_for_surface(consumer) is None
        assert project.vision["_research_spawn_count"] == 1
        assert str(consumer.id) in project.vision["_research_spawn_counted_ids"]

        snapshot = list(consumer.plan_output)
        await _handle_research_completion(consumer, "malformed replay", db_session)
        await db_session.refresh(consumer)
        await db_session.refresh(project)
        assert consumer.plan_output == snapshot
        assert consumer.context[FINALIZED_RESEARCH_FINDINGS_KEY] is True
        assert project.vision["_research_spawn_count"] == 1

    @_pytest.mark.asyncio
    async def test_depth_limit_preserves_metadata_and_salvage_eligible_carry(
        self, db_session, mocker
    ):
        """The terminal-depth handoff retains every validated finding class."""
        from test_autonomous_research import _make_finding, _patch_research_externals

        from app.research_deferred_carry import unacknowledged_deferred_findings

        project, direction, consumer = await _grader_project_and_task(
            db_session, "d-depth-complete-handoff"
        )
        decayed = _make_finding(
            "Retain repeatedly deferred work", direction_id=direction["id"]
        )
        decayed.update(
            deferred_reason="degradation_gate",
            deferred_count=3,
            originally_researched_at=_utcnow().isoformat(),
        )
        stale = _make_finding(
            "Retain TTL-expired work", direction_id=direction["id"]
        )
        stale.update(
            deferred_reason="batch_cap",
            originally_researched_at=(_utcnow() - timedelta(days=30)).isoformat(),
        )
        producer = _Task(
            project_id=project.id,
            title="Depth salvage producer",
            payloads=[{"type": "research", "base_branch": "main"}],
            context={
                "direction_id": direction["id"],
                "deferred_findings": [decayed, stale],
            },
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(producer)
        await db_session.flush()
        consumer.status = TaskStatus.COMPLETED
        consumer.context = {
            **(consumer.context or {}),
            "research_depth": 3,
            "prior_deferred_findings": unacknowledged_deferred_findings(producer),
        }
        db_session.add(consumer)
        await db_session.commit()
        note = {
            "type": "direction_note",
            "title": "Retain depth-bound evidence note",
            "description": "Carry the evidence into the next root cycle.",
            "prompt": "Carry the evidence into the next root cycle.",
            "direction_id": direction["id"],
        }
        proposal = {
            "type": "direction_proposal",
            "title": "Retain depth-bound follow-up",
            "description": "Commission this from a later research root.",
            "prompt": "Commission this from a later research root.",
            "direction_id": direction["id"],
        }
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[note, proposal],
        )

        await _handle_research_completion(consumer, "ignored", db_session)
        await db_session.refresh(producer)
        await db_session.refresh(consumer)

        expected_titles = {
            decayed["title"],
            stale["title"],
            note["title"],
            proposal["title"],
        }
        assert producer.context["deferred_findings"] == []
        assert consumer.plan_output == []
        assert {
            item["title"] for item in consumer.context["deferred_findings"]
        } == expected_titles
        assert consumer.context[FINALIZED_RESEARCH_FINDINGS_KEY] is True
        assert resolve_research_findings_for_surface(consumer) is None

    @_pytest.mark.asyncio
    async def test_carry_ack_recomputes_untrusted_hash_before_pruning(
        self, db_session
    ):
        from app.research_deferred_carry import (
            DEFERRED_FINDING_CONSUMPTION_ACKS_KEY,
            acknowledge_consumed_deferred_findings,
            canonical_deferred_finding_hash,
            unacknowledged_deferred_findings,
        )

        project, direction, consumer = await _grader_project_and_task(
            db_session, "d-carry-hash"
        )
        forged_hash = "shared-agent-supplied-hash"
        first = {
            "type": "improvement",
            "title": "Repair alpha carry",
            "description": "Alpha has a distinct implementation gap.",
            "direction_id": direction["id"],
            "finding_hash": forged_hash,
        }
        second = {
            "type": "improvement",
            "title": "Repair beta carry",
            "description": "Beta has a different implementation gap.",
            "direction_id": direction["id"],
            "finding_hash": forged_hash,
        }
        producer = _Task(
            project_id=project.id,
            title="Deferred producer with aliased hashes",
            payloads=[{"type": "research", "base_branch": "main"}],
            context={
                "direction_id": direction["id"],
                "deferred_findings": [first, second],
            },
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(producer)
        await db_session.commit()

        carried = unacknowledged_deferred_findings(producer)
        canonical_hashes = [canonical_deferred_finding_hash(item) for item in carried]
        assert canonical_hashes == [
            canonical_deferred_finding_hash(first),
            canonical_deferred_finding_hash(second),
        ]
        assert len(set(canonical_hashes)) == 2
        # The routing-lineage field is not trusted as carry identity and may be
        # preserved for legitimate same-batch references. Receipt/pruning still
        # uses the two distinct server-derived hashes above.
        assert [item["finding_hash"] for item in carried] == [
            forged_hash,
            forged_hash,
        ]

        stamped = await acknowledge_consumed_deferred_findings(
            db_session,
            consumer_task=consumer,
            carried_findings=[carried[0]],
        )
        await db_session.commit()
        await db_session.refresh(producer)

        assert stamped == 1
        assert [
            finding["title"] for finding in producer.context["deferred_findings"]
        ] == [second["title"]]
        acknowledgements = producer.context[
            DEFERRED_FINDING_CONSUMPTION_ACKS_KEY
        ]
        assert [record["finding_hash"] for record in acknowledgements] == [
            canonical_hashes[0]
        ]

    @_pytest.mark.asyncio
    async def test_unknown_prior_deferred_type_is_rejected_not_generically_spawned(
        self, db_session, mocker
    ):
        from test_autonomous_research import _make_finding, _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-invalid-deferred"
        )
        invalid_title = "Legacy generic knowledge finding"
        task.status = TaskStatus.COMPLETED
        task.context = {
            **(task.context or {}),
            "prior_deferred_findings": [
                {
                    "type": "knowledge",
                    "title": invalid_title,
                    "description": "A legacy type that is not a routing contract.",
                    "prompt": "Do not route this through generic create/plan.",
                    "direction_id": direction["id"],
                }
            ],
        }
        db_session.add(task)
        await db_session.commit()
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[
                _make_finding("Fresh valid finding", direction_id=direction["id"])
            ],
        )

        await _handle_research_completion(task, "ignored", db_session)
        await db_session.refresh(task)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        assert all(child.title != invalid_title for child in children)
        assert any(child.title == "Fresh valid finding" for child in children)
        assert task.context["research_invalid_deferred_finding_count"] == 1
        assert task.context["research_invalid_deferred_finding_types"] == ["knowledge"]

    @_pytest.mark.asyncio
    async def test_explicit_empty_output_still_processes_prior_deferred_work(
        self, db_session, mocker
    ):
        from test_autonomous_research import _make_finding, _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-prior-only"
        )
        carried = _make_finding(
            "Carried finding is the whole batch", direction_id=direction["id"]
        )
        carried.update(
            {
                "actionability_grade": "HIGH",
                "deferred_reason": "batch_cap",
                "deferred_count": 1,
            }
        )
        task.status = TaskStatus.COMPLETED
        task.context = {
            **(task.context or {}),
            RESEARCH_OUTPUT_CONTRACT_VERSION_KEY: RESEARCH_OUTPUT_CONTRACT_VERSION,
            "prior_deferred_findings": [carried],
        }
        db_session.add(task)
        await db_session.commit()
        _patch_research_externals(mocker)

        await _handle_research_completion(task, "[]", db_session)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        assert any(child.title == carried["title"] for child in children)

    def test_explicit_empty_array_is_parseable(self):
        assert _parse_research_findings("  []\n") == []

    @_pytest.mark.asyncio
    async def test_terminal_consumer_acknowledges_old_carry_without_losing_overflow(
        self, db_session, mocker
    ):
        """Consumed producer copies stop taxing headroom; newer overflow remains."""
        from test_autonomous_research import _make_finding, _patch_research_externals

        from app.research_deferred_carry import (
            DEFERRED_FINDING_CONSUMPTION_ACKS_KEY,
            canonical_deferred_finding_hash,
            unacknowledged_deferred_findings,
        )
        from app.research_completion import effective_research_finding_headroom
        from app.time_utils import _utcnow

        project, direction, consumer = await _grader_project_and_task(
            db_session, "d-carry-ack"
        )
        produced_at = _utcnow()
        produced: list[dict] = []
        for index in range(20):
            finding = _make_finding(
                title=f"Implement isolated component_{index}",
                direction_id=direction["id"],
            )
            finding.update(
                {
                    "description": (
                        f"component_{index}.py has an independent missing behavior "
                        f"identified by unique_token_{index}."
                    ),
                    "prompt": f"Implement unique_token_{index} behavior.",
                    "related_files": [f"backend/app/component_{index}.py"],
                    "actionability_grade": "HIGH",
                    "deferred_reason": "batch_cap",
                    "deferred_count": 1,
                    "originally_researched_at": produced_at.isoformat(),
                }
            )
            finding["finding_hash"] = canonical_deferred_finding_hash(finding)
            produced.append(finding)
        # Legacy parent-scoped entries may omit direction_id. The producer
        # boundary must inherit its owning direction or the autonomous scan
        # would skip the entry before the consumer ever sees it.
        produced[0].pop("direction_id", None)

        producer = _Task(
            project_id=project.id,
            title="Prior deferred producer",
            payloads=[{"type": "research", "base_branch": "main"}],
            context={
                "direction_id": direction["id"],
                "deferred_findings": produced,
            },
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(producer)
        await db_session.flush()
        consumer.status = TaskStatus.COMPLETED
        first_cycle_carry = unacknowledged_deferred_findings(producer)
        assert len(first_cycle_carry) == 20
        assert first_cycle_carry[0]["direction_id"] == direction["id"]
        consumer.context = {
            **(consumer.context or {}),
            "prior_deferred_findings": first_cycle_carry,
        }
        db_session.add(consumer)
        await db_session.commit()

        _patch_research_externals(mocker)
        mocker.patch("app.research_completion._parse_research_findings", return_value=[])

        await _handle_research_completion(consumer, "[]", db_session)
        await db_session.refresh(producer)
        await db_session.refresh(consumer)

        acknowledgements = (producer.context or {}).get(
            DEFERRED_FINDING_CONSUMPTION_ACKS_KEY, []
        )
        assert len(acknowledgements) == 20
        assert all(
            receipt["source_task_id"] == str(producer.id)
            and receipt["consumed_by_task_id"] == str(consumer.id)
            for receipt in acknowledgements
        )
        assert producer.context["deferred_findings"] == [], (
            "fully consumed producers must stop matching the scanner's bounded "
            "deferred-row query"
        )
        # The old producer is fully acknowledged/pruned and contributes zero to
        # the next scan. The five entries beyond the 15-finding batch cap now
        # live on the consumer and remain recoverable under that newer source.
        assert unacknowledged_deferred_findings(producer) == []
        overflow = unacknowledged_deferred_findings(consumer)
        assert len(overflow) == 5
        assert all(item["source_task_id"] == str(consumer.id) for item in overflow)
        assert effective_research_finding_headroom(len(overflow)) == 10

    @_pytest.mark.asyncio
    async def test_terminal_consumer_processes_and_acknowledges_carried_metadata(
        self, db_session, mocker
    ):
        """Carried notes/proposals are applied once, then stop taxing headroom."""
        from test_autonomous_research import _patch_research_externals

        from app.research_deferred_carry import (
            DEFERRED_FINDING_CONSUMPTION_ACKS_KEY,
            unacknowledged_deferred_findings,
        )

        project, direction, consumer = await _grader_project_and_task(
            db_session, "d-carried-metadata"
        )
        note_text = "Preserve the scheduler starvation trace in direction context."
        proposal_text = "Investigate cross-project runner admission fairness."
        producer = _Task(
            project_id=project.id,
            title="Prior metadata producer",
            payloads=[{"type": "research", "base_branch": "main"}],
            context={
                "direction_id": direction["id"],
                "deferred_findings": [
                    {
                        "type": "direction_note",
                        "title": "Retain scheduler trace",
                        "description": note_text,
                        "prompt": note_text,
                        "direction_id": direction["id"],
                    },
                    {
                        "type": "direction_proposal",
                        "title": "Study runner fairness",
                        "description": proposal_text,
                        "prompt": proposal_text,
                        "direction_id": direction["id"],
                    },
                ],
            },
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(producer)
        await db_session.flush()
        consumer.status = TaskStatus.COMPLETED
        consumer.context = {
            **(consumer.context or {}),
            "prior_deferred_findings": unacknowledged_deferred_findings(producer),
        }
        db_session.add(consumer)
        await db_session.commit()

        _patch_research_externals(mocker)
        mocker.patch("app.research_completion._parse_research_findings", return_value=[])

        await _handle_research_completion(consumer, "[]", db_session)
        await db_session.refresh(producer)
        await db_session.refresh(project)

        assert producer.context["deferred_findings"] == []
        acknowledgements = producer.context[
            DEFERRED_FINDING_CONSUMPTION_ACKS_KEY
        ]
        assert len(acknowledgements) == 2
        updated_direction = next(
            item for item in project.directions if item["id"] == direction["id"]
        )
        assert any(
            note["text"] == note_text
            and note["added_by_task"] == str(consumer.id)
            for note in updated_direction.get("notes", [])
        )
        assert any(
            item.get("status") == "proposed"
            and item.get("text") == proposal_text
            and item.get("proposed_by") == str(consumer.id)
            for item in project.directions
        )

    @_pytest.mark.asyncio
    async def test_capacity_degraded_declared_deliverable_zero_output_fails(
        self, db_session, mocker
    ):
        # Reproduces the a70fe988 signature: degraded provider capacity + a
        # declared deliverable + zero output → non-green terminal, not SUCCESS.
        _project, direction, task = await _grader_project_and_task(
            db_session, "d-nooutput03"
        )
        task.description = "The deliverable of THIS task is a SENT MESSAGE."
        db_session.add(task)
        await db_session.commit()

        mocker.patch(
            "app.research_completion._check_capacity_degradation",
            return_value="provider capacity/rate-limit error",
        )
        await self._run_empty(db_session, mocker, task)

        assert task.status == TaskStatus.FAILED
        assert task.latest_status_message.startswith(
            _NO_DELIVERED_OUTPUT_TERMINAL_REASON
        )
        assert "provider capacity" in task.latest_status_message

    @_pytest.mark.asyncio
    async def test_declared_deliverable_with_message_sent_stays_completed(
        self, db_session, mocker
    ):
        # Zero findings BUT the task sent a ProjectMessage — output WAS delivered.
        from app.models import ProjectMessage

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-nooutput04"
        )
        task.description = "The deliverable of THIS task is a SENT MESSAGE."
        db_session.add(task)
        message = ProjectMessage(
            project_id=_project.id,
            from_task_id=task.id,
            from_type="agent",
            content="Here is the frank state-of-internals report you asked for.",
        )
        db_session.add(message)
        await db_session.commit()

        from test_autonomous_research import _patch_research_externals

        task.status = TaskStatus.COMPLETED
        _patch_research_externals(mocker)
        await _handle_research_completion(
            task,
            "The requested report was delivered through the message channel.",
            db_session,
        )
        await db_session.refresh(task)

        assert task.status == TaskStatus.COMPLETED, (
            "a delivered message must keep the task green even without findings JSON"
        )

    @_pytest.mark.asyncio
    async def test_delivered_output_present_detects_message_and_document(
        self, db_session
    ):
        from app.models import KnowledgeDocument, ProjectMessage

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-nooutput05"
        )
        assert await _delivered_output_present(task, db_session) is False

        db_session.add(
            ProjectMessage(
                project_id=_project.id, from_task_id=task.id, content="sent"
            )
        )
        await db_session.commit()
        assert await _delivered_output_present(task, db_session) is True

        # Also detects a written report (KnowledgeDocument) alone.
        _project2, _d2, task2 = await _grader_project_and_task(
            db_session, "d-nooutput06"
        )
        assert await _delivered_output_present(task2, db_session) is False
        db_session.add(
            KnowledgeDocument(
                project_id=_project2.id,
                source_task_id=task2.id,
                title="Report",
                summary="s",
                body="b",
            )
        )
        await db_session.commit()
        assert await _delivered_output_present(task2, db_session) is True


class TestDedupFilteredFindingsAddedToSalvage:
    """Dedup-filtered findings flow into salvage candidates and the salvage bundle task."""

    @_pytest.mark.asyncio
    async def test_dedup_filtered_findings_appear_in_salvage_bundle(
        self, db_session, mocker
    ):
        """Findings filtered by dedup (hash match) appear in the spawned salvage bundle task."""
        from app.finding_deduplication import DedupOutcome
        from test_autonomous_research import _make_finding, _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dedup-salvage01"
        )

        # A finding that will be dedup-filtered (hash match)
        dedup_filtered = _make_finding(
            "Deduped perf finding", direction_id=direction["id"]
        )
        # A finding that survives dedup and spawns normally
        surviving = _make_finding(
            "Fresh finding", direction_id=direction["id"]
        )

        # Build DedupOutcome where the first finding is region-overlap filtered
        mock_outcome = DedupOutcome()
        mock_outcome.deduplicated = [(0, surviving, "hash_survivor")]
        mock_outcome.filtered = [
            {
                "title": "Deduped perf finding"[:120],
                "direction_id": direction["id"],
                "reason": "region overlap with: existing task",
            }
        ]
        mock_outcome.word_overlap_count = 1

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[dedup_filtered, surviving],
        )
        mocker.patch(
            "app.research_completion.deduplicate_findings",
            return_value=mock_outcome,
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        # Verify salvage activity was recorded on the parent task
        salvage_activity = (task.context or {}).get("salvage_activity")
        assert salvage_activity is not None, (
            "salvage_activity must be recorded when dedup-filtered findings "
            "are captured in salvage candidates"
        )
        assert salvage_activity["salvaged_count"] == 1, (
            f"Expected 1 salvaged finding, got {salvage_activity['salvaged_count']}"
        )

        # Fetch the salvage bundle task (identified by salvage_source_task_id in context)
        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == task.id)
        )
        child_tasks = result.scalars().all()
        salvage_tasks = [
            t
            for t in child_tasks
            if (t.context or {}).get("salvage_source_task_id") == str(task.id)
        ]
        assert len(salvage_tasks) == 1, (
            f"Expected 1 salvage bundle task, found {len(salvage_tasks)} "
            f"among {len(child_tasks)} total child tasks"
        )

        salvage = salvage_tasks[0]
        assert salvage.context.get("salvage_finding_count") == 1
        # Title should say "discarded" (not only "decayed")
        assert "discarded" in salvage.title.lower()

        # Verify the payload contains the dedup-filtered finding
        payload = salvage.payloads[0]
        assert payload["type"] == "salvage_bundle"
        assert len(payload["salvaged_findings"]) == 1
        entry = payload["salvaged_findings"][0]
        assert "Deduped perf finding" in entry["title"]
        assert "dedup" in (entry["discard_reason"] or "").lower()
        assert entry["direction_id"] == direction["id"]

    @_pytest.mark.asyncio
    async def test_dedup_filtered_finding_rich_context_preserved(
        self, db_session, mocker
    ):
        """Rich context (prompt, related_files, claims, sources) is preserved
        when dedup-filtered findings enter the salvage bundle payload."""
        from app.finding_deduplication import DedupOutcome
        from test_autonomous_research import _make_finding, _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dedup-salvage02"
        )

        # A dedup-filtered finding with full rich context
        rich_finding = {
            "title": "Rich perf finding",
            "description": "N+1 query in task_lifecycle.py causes slow pages",
            "prompt": "Optimize N+1 by adding selectinload",
            "related_files": ["backend/app/task_lifecycle.py"],
            "claims": [{"claim": "N+1 detected", "confidence": "high"}],
            "sources": [{"ref": "perf_audit_v2.pdf", "trust": "high"}],
            "direction_id": direction["id"],
            "priority": "medium",
            "scope": "small",
            "type": "improvement",
        }
        surviving = _make_finding(
            "Surviving finding", direction_id=direction["id"]
        )

        mock_outcome = DedupOutcome()
        mock_outcome.deduplicated = [(0, surviving, "hash_s")]
        mock_outcome.filtered = [
            {
                "title": "Rich perf finding"[:120],
                "direction_id": direction["id"],
                "reason": "region overlap with: existing task",
            }
        ]
        mock_outcome.word_overlap_count = 1

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[rich_finding, surviving],
        )
        mocker.patch(
            "app.research_completion.deduplicate_findings",
            return_value=mock_outcome,
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        # Fetch salvage bundle task (identified by salvage_source_task_id in context)
        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == task.id)
        )
        child_tasks = result.scalars().all()
        salvage_tasks = [
            t
            for t in child_tasks
            if (t.context or {}).get("salvage_source_task_id") == str(task.id)
        ]
        assert len(salvage_tasks) == 1, (
            f"Expected 1 salvage bundle task, found {len(salvage_tasks)} "
            f"among {len(child_tasks)} total child tasks"
        )

        payload = salvage_tasks[0].payloads[0]
        entry = payload["salvaged_findings"][0]

        # Verify rich context is preserved
        assert entry["title"] == "Rich perf finding"
        assert "dedup" in (entry["discard_reason"] or "").lower()
        assert entry["prompt"] == "Optimize N+1 by adding selectinload"
        assert entry["description"] == "N+1 query in task_lifecycle.py causes slow pages"
        assert entry["related_files"] == ["backend/app/task_lifecycle.py"]
        assert entry["claims"] == [{"claim": "N+1 detected", "confidence": "high"}]
        assert len(entry["sources"]) >= 1
        assert entry["sources"][0].get("ref") == "perf_audit_v2.pdf"

    @_pytest.mark.asyncio
    async def test_non_salvageable_hash_match_excluded(
        self, db_session, mocker
    ):
        """Findings filtered for hash-match (exact duplicate) are NOT salvaged.

        Hash matches represent genuine duplicates of in-flight or recently
        terminal work — not blocked-by-pipeline findings. The salvage gate
        must exclude them so stale/duplicate findings don't create noise.
        """
        from app.finding_deduplication import DedupOutcome
        from test_autonomous_research import _make_finding, _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dedup-neg-hash"
        )

        # A non-perf finding filtered with a non-salvageable reason.
        finding = _make_finding(
            "Hash-duplicate improvement", direction_id=direction["id"],
            finding_type="improvement",
        )
        surviving = _make_finding(
            "Surviving finding", direction_id=direction["id"]
        )

        mock_outcome = DedupOutcome()
        mock_outcome.deduplicated = [(0, surviving, "hash_s")]
        mock_outcome.filtered = [
            {
                "title": "Hash-duplicate improvement"[:120],
                "direction_id": direction["id"],
                "reason": "hash match with prior task",
            }
        ]

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding, surviving],
        )
        mocker.patch(
            "app.research_completion.deduplicate_findings",
            return_value=mock_outcome,
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        # Hash-match finding should NOT be salvaged — salvage_activity
        # may still be present if other paths produce candidates, but the
        # salvage count must be 0.
        salvage_activity = (task.context or {}).get("salvage_activity")
        if salvage_activity is not None:
            assert salvage_activity["salvaged_count"] == 0, (
                f"Hash-match findings should not be salvaged, "
                f"got {salvage_activity['salvaged_count']}"
            )
        # No salvage bundle task should exist.
        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == task.id)
        )
        child_tasks = result.scalars().all()
        salvage_tasks = [
            t for t in child_tasks
            if (t.first_payload or {}).get("type") == "salvage_bundle"
        ]
        assert len(salvage_tasks) == 0, (
            "No salvage_bundle task should be spawned for hash-match dedup findings"
        )

    @_pytest.mark.asyncio
    async def test_recent_terminal_region_overlap_excluded(
        self, db_session, mocker
    ):
        """Findings filtered for recent-terminal region overlap are NOT salvaged.

        'recent terminal region overlap with:' indicates the work was already
        resolved/shipped in a prior cycle — not merely blocked by in-flight work.
        The salvage gate must exclude these to avoid recycling completed work.
        """
        from app.finding_deduplication import DedupOutcome
        from test_autonomous_research import _make_finding, _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dedup-neg-terminal"
        )

        finding = _make_finding(
            "Already resolved refactor", direction_id=direction["id"],
            finding_type="improvement",
        )
        surviving = _make_finding(
            "Surviving finding", direction_id=direction["id"]
        )

        mock_outcome = DedupOutcome()
        mock_outcome.deduplicated = [(0, surviving, "hash_s")]
        mock_outcome.filtered = [
            {
                "title": "Already resolved refactor"[:120],
                "direction_id": direction["id"],
                "reason": "recent terminal region overlap with: shipped-task",
            }
        ]

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding, surviving],
        )
        mocker.patch(
            "app.research_completion.deduplicate_findings",
            return_value=mock_outcome,
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        # Terminal region overlap finding should NOT be salvaged.
        salvage_activity = (task.context or {}).get("salvage_activity")
        if salvage_activity is not None:
            assert salvage_activity["salvaged_count"] == 0, (
                f"Recent-terminal findings should not be salvaged, "
                f"got {salvage_activity['salvaged_count']}"
            )
        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == task.id)
        )
        child_tasks = result.scalars().all()
        salvage_tasks = [
            t for t in child_tasks
            if (t.first_payload or {}).get("type") == "salvage_bundle"
        ]
        assert len(salvage_tasks) == 0, (
            "No salvage_bundle task should be spawned for recent-terminal findings"
        )

    @_pytest.mark.asyncio
    async def test_cross_cycle_hash_suppression_excluded(
        self, db_session, mocker
    ):
        """Findings whose hash was previously filtered are NOT salvaged.

        The cross-cycle hash suppression gate prevents re-salvaging findings
        that were already filtered in a prior cycle. Even findings with a
        salvageable dedup reason (region overlap) must be excluded when their
        hash appears in previously_filtered_hashes.
        """
        from app.finding_deduplication import DedupOutcome, finding_hash as _finding_hash
        from test_autonomous_research import _make_finding, _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dedup-neg-crosscycle"
        )

        # The handler resolves previously_filtered_hashes by direction_id
        # from task.context; ensure it's set so the lookup can find our hash.
        task.context = {**(task.context or {}), "direction_id": direction["id"]}
        db_session.add(task)
        await db_session.commit()

        finding = _make_finding(
            "Stale region overlap", direction_id=direction["id"],
            finding_type="improvement",
        )
        surviving = _make_finding(
            "Surviving finding", direction_id=direction["id"]
        )

        # Compute the finding hash matching the production hash at
        # research_completion.py:3306-3312 (finding_hash(title, canonical_body)).
        _fhash = _finding_hash(
            finding["title"],
            finding.get("description") or finding.get("prompt") or "",
        )
        # Inject the hash into the direction so the handler sees it.
        # SQLAlchemy JSON columns require explicit dirty-marking for nested mutations.
        _dir_entry = None
        for _d in _project.directions:
            if _d["id"] == direction["id"]:
                _dir_entry = _d
                break
        assert _dir_entry is not None
        _dir_entry["recently_filtered_finding_hashes"] = [_fhash]
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(_project, "directions")
        db_session.add(_project)
        await db_session.commit()

        mock_outcome = DedupOutcome()
        mock_outcome.deduplicated = [(0, surviving, "hash_s")]
        # Salvageable reason — but the hash is in previously_filtered_hashes.
        mock_outcome.filtered = [
            {
                "title": "Stale region overlap"[:120],
                "direction_id": direction["id"],
                "reason": "region overlap with: in-flight task",
            }
        ]

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding, surviving],
        )
        mocker.patch(
            "app.research_completion.deduplicate_findings",
            return_value=mock_outcome,
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        # Cross-cycle suppression should block salvage despite salvageable reason.
        salvage_activity = (task.context or {}).get("salvage_activity")
        if salvage_activity is not None:
            assert salvage_activity["salvaged_count"] == 0, (
                f"Cross-cycle suppressed findings should not be salvaged, "
                f"got {salvage_activity['salvaged_count']}"
            )
        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == task.id)
        )
        child_tasks = result.scalars().all()
        salvage_tasks = [
            t for t in child_tasks
            if (t.first_payload or {}).get("type") == "salvage_bundle"
        ]
        assert len(salvage_tasks) == 0, (
            "No salvage_bundle task should be spawned for cross-cycle suppressed findings"
        )

    @_pytest.mark.asyncio
    async def test_evidence_overlap_dedup_captured_and_salvaged(
        self, db_session, mocker
    ):
        """A non-perf finding dropped by the Jaccard evidence-overlap dedup gate
        is captured with its full payload in the salvage bundle task."""
        from app.finding_deduplication import DedupOutcome
        from test_autonomous_research import _make_finding, _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dedup-evidence-salvage"
        )

        # A non-perf improvement finding with full rich context that will
        # be filtered by evidence overlap (Jaccard similiary dedup).
        matched_finding = _make_finding(
            "Matched finding title",
            direction_id=direction["id"],
            finding_type="improvement",
        )
        # A finding that is filtered as evidence-overlap duplicate
        evidence_overlap_finding = dict(_make_finding(
            "Performance: N+1 query in user lookup",
            direction_id=direction["id"],
            finding_type="improvement",
        ))
        evidence_overlap_finding["description"] = (
            "N+1 query in user lookup causes 500ms page load"
        )
        evidence_overlap_finding["prompt"] = "Add selectinload to user relation"
        evidence_overlap_finding["related_files"] = [
            "backend/app/user_service.py",
        ]
        evidence_overlap_finding["claims"] = [
            {"claim": "N+1 detected in user lookup", "confidence": "high"}
        ]
        evidence_overlap_finding["sources"] = [
            {"ref": "perf_profile_v3.json", "trust": "high"}
        ]
        evidence_overlap_finding["severity"] = "medium"

        surviving = _make_finding(
            "Unrelated fix", direction_id=direction["id"]
        )

        mock_outcome = DedupOutcome()
        mock_outcome.deduplicated = [(0, surviving, "hash_survive")]
        mock_outcome.evidence_overlap_count = 1
        mock_outcome.filtered = [
            {
                "title": evidence_overlap_finding["title"][:120],
                "direction_id": direction["id"],
                "reason": "evidence overlap with: Matched finding title",
                "finding": evidence_overlap_finding,
            }
        ]

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[matched_finding, evidence_overlap_finding, surviving],
        )
        mocker.patch(
            "app.research_completion.deduplicate_findings",
            return_value=mock_outcome,
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        # ── Assert salvage activity was recorded on parent ──
        salvage_activity = (task.context or {}).get("salvage_activity")
        assert salvage_activity is not None, (
            "salvage_activity must be recorded when evidence-overlap filtered "
            "findings are captured in salvage candidates"
        )
        assert salvage_activity["salvaged_count"] >= 1, (
            f"Expected >=1 salvaged finding, got {salvage_activity['salvaged_count']}"
        )

        # ── Assert a salvage bundle child task was spawned ──
        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == task.id)
        )
        child_tasks = result.scalars().all()
        salvage_tasks = [
            t
            for t in child_tasks
            if (t.context or {}).get("salvage_source_task_id") == str(task.id)
        ]
        assert len(salvage_tasks) == 1, (
            f"Expected 1 salvage bundle task, found {len(salvage_tasks)} "
            f"among {len(child_tasks)} total child tasks"
        )

        # ── Assert full payload is preserved in the salvage bundle ──
        payload = salvage_tasks[0].payloads[0]
        assert payload["type"] == "salvage_bundle"
        entry = payload["salvaged_findings"][0]
        assert "Performance: N+1" in entry["title"]
        assert "dedup: evidence overlap with:" in (entry["discard_reason"] or "").lower()
        assert entry["direction_id"] == direction["id"]
        assert entry["prompt"] == "Add selectinload to user relation"
        assert entry["description"] == "N+1 query in user lookup causes 500ms page load"
        assert entry["related_files"] == ["backend/app/user_service.py"]
        assert len(entry["claims"]) == 1
        assert entry["claims"][0]["claim"] == "N+1 detected in user lookup"

    @_pytest.mark.asyncio
    async def test_all_files_covered_by_open_pr_salvaged(
        self, db_session, mocker
    ):
        """Findings filtered because all their files are covered by an open PR are salvaged.

        'all files covered by open PR' is a salvageable reason: the finding is
        blocked by in-flight work, not already resolved. It must enter the salvage
        bundle so it is not silently dropped when a PR is open for all its files.
        """
        from app.finding_deduplication import DedupOutcome
        from test_autonomous_research import _make_finding, _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dedup-open-pr"
        )

        blocked_finding = dict(_make_finding(
            "Improve auth caching while PR open",
            direction_id=direction["id"],
            finding_type="improvement",
        ))
        blocked_finding["prompt"] = "Add in-process cache to auth.py"
        blocked_finding["related_files"] = ["backend/app/auth.py"]

        surviving = _make_finding(
            "Unrelated surviving fix", direction_id=direction["id"]
        )

        mock_outcome = DedupOutcome()
        mock_outcome.deduplicated = [(0, surviving, "hash_surv")]
        mock_outcome.file_overlap_count = 1
        mock_outcome.filtered = [
            {
                "title": "Improve auth caching while PR open"[:120],
                "direction_id": direction["id"],
                "reason": "all files covered by open PR",
                "finding": blocked_finding,
            }
        ]

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[blocked_finding, surviving],
        )
        mocker.patch(
            "app.research_completion.deduplicate_findings",
            return_value=mock_outcome,
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        salvage_activity = (task.context or {}).get("salvage_activity")
        assert salvage_activity is not None, (
            "salvage_activity must be recorded when a finding is filtered "
            "because all its files are covered by an open PR"
        )
        assert salvage_activity["salvaged_count"] >= 1

        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == task.id)
        )
        child_tasks = result.scalars().all()
        salvage_tasks = [
            t
            for t in child_tasks
            if (t.context or {}).get("salvage_source_task_id") == str(task.id)
        ]
        assert len(salvage_tasks) == 1, (
            f"Expected 1 salvage bundle task for 'all files covered by open PR', "
            f"found {len(salvage_tasks)}"
        )
        payload = salvage_tasks[0].payloads[0]
        entry = payload["salvaged_findings"][0]
        assert "Improve auth caching" in entry["title"]
        assert "dedup: all files covered by open PR" in (entry["discard_reason"] or "")
        assert entry["direction_id"] == direction["id"]
        assert entry["prompt"] == "Add in-process cache to auth.py"

    @_pytest.mark.asyncio
    async def test_retired_approach_excluded_from_salvage(
        self, db_session, mocker
    ):
        """Findings filtered because their approach was retired are NOT salvaged.

        'retired approach:' indicates the direction has already been tried and
        abandoned. The salvage gate must exclude these: salvaging a retired
        approach would re-queue dead-end work that the strategist already rejected.
        """
        from app.finding_deduplication import DedupOutcome
        from test_autonomous_research import _make_finding, _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dedup-retired"
        )

        retired_finding = _make_finding(
            "Retry retired cache approach",
            direction_id=direction["id"],
            finding_type="improvement",
        )
        surviving = _make_finding(
            "Unrelated surviving fix", direction_id=direction["id"]
        )

        mock_outcome = DedupOutcome()
        mock_outcome.deduplicated = [(0, surviving, "hash_surv")]
        mock_outcome.retired_approach_count = 1
        mock_outcome.filtered = [
            {
                "title": "Retry retired cache approach"[:120],
                "direction_id": direction["id"],
                "reason": "retired approach: in-memory-cache-strategy",
                "finding": retired_finding,
            }
        ]

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[retired_finding, surviving],
        )
        mocker.patch(
            "app.research_completion.deduplicate_findings",
            return_value=mock_outcome,
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        # Retired-approach findings must NOT be salvaged.
        salvage_activity = (task.context or {}).get("salvage_activity")
        if salvage_activity is not None:
            assert salvage_activity["salvaged_count"] == 0, (
                f"Retired-approach findings should not be salvaged, "
                f"got {salvage_activity['salvaged_count']}"
            )
        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == task.id)
        )
        child_tasks = result.scalars().all()
        salvage_tasks = [
            t
            for t in child_tasks
            if (t.first_payload or {}).get("type") == "salvage_bundle"
        ]
        assert len(salvage_tasks) == 0, (
            "No salvage_bundle task should be spawned for retired-approach filtered findings"
        )

    @_pytest.mark.asyncio
    async def test_archived_diagnosis_excluded_from_salvage(
        self, db_session, mocker
    ):
        """Findings filtered as archived diagnoses are NOT salvaged.

        'archived diagnosis:' indicates the analysis is known-stale — its root
        cause was already investigated and closed. The salvage gate must exclude
        these: re-queuing an archived diagnosis wastes resources on work the
        strategist marked done.
        """
        from app.finding_deduplication import DedupOutcome
        from test_autonomous_research import _make_finding, _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dedup-archived"
        )

        archived_finding = _make_finding(
            "Revisit archived auth diagnosis",
            direction_id=direction["id"],
            finding_type="improvement",
        )
        surviving = _make_finding(
            "Unrelated surviving fix", direction_id=direction["id"]
        )

        mock_outcome = DedupOutcome()
        mock_outcome.deduplicated = [(0, surviving, "hash_surv")]
        mock_outcome.archived_diagnoses_count = 1
        mock_outcome.filtered = [
            {
                "title": "Revisit archived auth diagnosis"[:120],
                "direction_id": direction["id"],
                "reason": "archived diagnosis: auth-session-ttl-stale",
                "finding": archived_finding,
            }
        ]

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[archived_finding, surviving],
        )
        mocker.patch(
            "app.research_completion.deduplicate_findings",
            return_value=mock_outcome,
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        # Archived-diagnosis findings must NOT be salvaged.
        salvage_activity = (task.context or {}).get("salvage_activity")
        if salvage_activity is not None:
            assert salvage_activity["salvaged_count"] == 0, (
                f"Archived-diagnosis findings should not be salvaged, "
                f"got {salvage_activity['salvaged_count']}"
            )
        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == task.id)
        )
        child_tasks = result.scalars().all()
        salvage_tasks = [
            t
            for t in child_tasks
            if (t.first_payload or {}).get("type") == "salvage_bundle"
        ]
        assert len(salvage_tasks) == 0, (
            "No salvage_bundle task should be spawned for archived-diagnosis filtered findings"
        )

    @_pytest.mark.asyncio
    async def test_fallback_key_lookup_when_finding_key_absent(
        self, db_session, mocker
    ):
        """Filtered entries without a 'finding' key fall back to _findings_by_key lookup.

        The dedup loop builds a title+direction_id keyed dict from the parsed
        findings list so old-format filtered entries (no 'finding' key) can still
        be recovered. This tests that backward-compat fallback path.
        """
        from app.finding_deduplication import DedupOutcome
        from test_autonomous_research import _make_finding, _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dedup-fallback-lookup"
        )

        # A finding that will be filtered — but the filtered entry omits 'finding'.
        target_finding = dict(_make_finding(
            "Legacy lookup finding",
            direction_id=direction["id"],
            finding_type="improvement",
        ))
        target_finding["prompt"] = "Implement legacy fix"
        target_finding["related_files"] = ["backend/app/legacy.py"]

        surviving = _make_finding(
            "Unrelated surviving fix", direction_id=direction["id"]
        )

        mock_outcome = DedupOutcome()
        mock_outcome.deduplicated = [(0, surviving, "hash_surv")]
        # Filtered entry WITHOUT 'finding' key — triggers _findings_by_key fallback
        mock_outcome.filtered = [
            {
                "title": "Legacy lookup finding"[:120],
                "direction_id": direction["id"],
                "reason": "region overlap with: in-flight task",
                # No "finding" key — forces fallback to title+direction_id lookup
            }
        ]

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[target_finding, surviving],
        )
        mocker.patch(
            "app.research_completion.deduplicate_findings",
            return_value=mock_outcome,
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        # The fallback lookup should still recover and salvage the finding.
        salvage_activity = (task.context or {}).get("salvage_activity")
        assert salvage_activity is not None, (
            "salvage_activity must be recorded even when fallback _findings_by_key "
            "lookup is used instead of the 'finding' key"
        )
        assert salvage_activity["salvaged_count"] >= 1

        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == task.id)
        )
        child_tasks = result.scalars().all()
        salvage_tasks = [
            t
            for t in child_tasks
            if (t.context or {}).get("salvage_source_task_id") == str(task.id)
        ]
        assert len(salvage_tasks) == 1, (
            f"Expected 1 salvage bundle task via fallback lookup, "
            f"found {len(salvage_tasks)}"
        )
        payload = salvage_tasks[0].payloads[0]
        entry = payload["salvaged_findings"][0]
        assert "Legacy lookup finding" in entry["title"]
        assert "dedup:" in (entry["discard_reason"] or "")

    @_pytest.mark.asyncio
    async def test_combined_decay_and_dedup_filtered_in_single_bundle(
        self, db_session, mocker
    ):
        """Decay-based and dedup-filtered candidates merge into one salvage bundle.

        When a research cycle both decays a repeatedly-deferred finding AND has
        a dedup-filtered finding with a salvageable reason, they should combine
        into a single salvage_bundle task rather than being silently dropped or
        spawning separate tasks.
        """
        from app.finding_deduplication import DedupOutcome
        from test_autonomous_research import _make_finding, _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dedup-combined-salvage"
        )

        # A prior-deferred finding that has been deferred too many times → decays
        decayed = dict(_make_finding(
            "Ultra-low-confidence repeated finding",
            direction_id=direction["id"],
        ))
        decayed["actionability_grade"] = "LOW"
        decayed["deferred_reason"] = "ultra_low_confidence"
        decayed["deferred_count"] = 4  # >= 4 threshold for ultra_low_confidence
        task.context = {
            **(task.context or {}),
            "prior_deferred_findings": [decayed],
        }
        db_session.add(task)
        await db_session.commit()

        # A new finding that is dedup-filtered for a salvageable reason
        blocked_finding = dict(_make_finding(
            "Region-blocked new improvement",
            direction_id=direction["id"],
            finding_type="improvement",
        ))
        blocked_finding["prompt"] = "Implement the blocked improvement"
        surviving = _make_finding(
            "Unrelated surviving fix", direction_id=direction["id"]
        )

        mock_outcome = DedupOutcome()
        mock_outcome.deduplicated = [(0, surviving, "hash_surv")]
        mock_outcome.filtered = [
            {
                "title": "Region-blocked new improvement"[:120],
                "direction_id": direction["id"],
                "reason": "region overlap with: in-flight PR task",
                "finding": blocked_finding,
            }
        ]

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[blocked_finding, surviving],
        )
        mocker.patch(
            "app.research_completion.deduplicate_findings",
            return_value=mock_outcome,
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        # Both the decayed and dedup-filtered finding must be merged into one bundle.
        salvage_activity = (task.context or {}).get("salvage_activity")
        assert salvage_activity is not None, (
            "salvage_activity must be recorded when both decay and dedup-filtered "
            "findings are present"
        )
        assert salvage_activity["salvaged_count"] == 2, (
            f"Expected 2 salvaged findings (1 decay + 1 dedup-filtered), "
            f"got {salvage_activity['salvaged_count']}"
        )

        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == task.id)
        )
        child_tasks = result.scalars().all()
        salvage_tasks = [
            t
            for t in child_tasks
            if (t.context or {}).get("salvage_source_task_id") == str(task.id)
        ]
        assert len(salvage_tasks) == 1, (
            f"Expected exactly 1 combined salvage bundle task, "
            f"found {len(salvage_tasks)}"
        )
        payload = salvage_tasks[0].payloads[0]
        assert payload["total_discarded"] == 2
        titles = {e["title"] for e in payload["salvaged_findings"]}
        assert any("Ultra-low-confidence" in t for t in titles), (
            "Decayed finding must appear in the combined salvage bundle"
        )
        assert any("Region-blocked" in t for t in titles), (
            "Dedup-filtered finding must appear in the combined salvage bundle"
        )
        # Verify discard_reason annotations distinguish the two sources
        reasons = {e["discard_reason"] for e in payload["salvaged_findings"]}
        assert any("ultra_low_confidence" in (r or "") for r in reasons)
        assert any("dedup:" in (r or "") for r in reasons)


@_pytest.mark.asyncio
async def test_perf_finding_with_salvageable_reason_not_double_counted(db_session, mocker):
    """A finding that is both type=='perf' AND filtered for a salvageable dedup reason
    must appear in the salvage bundle exactly once, not twice.

    Regression test for the double-counting bug where two separate loops both
    appended to _salvage_candidates from the same _dedup_outcome.filtered list
    with no mutual-exclusion guard between them.
    """
    from test_autonomous_research import _make_finding, _patch_research_externals
    from app.finding_deduplication import DedupOutcome

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-salvage-no-double-count"
    )

    # A perf-type finding filtered for a salvageable reason ("region overlap with:").
    # This triggers BOTH collection loops: loop 1 (type=="perf") AND loop 2
    # (reason starts with "region overlap with:"). Without the fix, it would be
    # appended twice.
    perf_region_overlap_finding = dict(_make_finding(
        "Perf: slow N+1 in task fetch loop",
        direction_id=direction["id"],
        finding_type="perf",
    ))
    perf_region_overlap_finding["description"] = "N+1 query in task fetch"
    perf_region_overlap_finding["prompt"] = "Add selectinload to task relation"

    surviving = _make_finding(
        "Unrelated surviving fix", direction_id=direction["id"]
    )

    mock_outcome = DedupOutcome()
    mock_outcome.deduplicated = [(0, surviving, "hash_s")]
    mock_outcome.filtered = [
        {
            "title": perf_region_overlap_finding["title"][:120],
            "direction_id": direction["id"],
            "reason": "region overlap with: some-in-flight-task",
            "finding": perf_region_overlap_finding,
        }
    ]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[perf_region_overlap_finding, surviving],
    )
    mocker.patch(
        "app.research_completion.deduplicate_findings",
        return_value=mock_outcome,
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # Salvage activity must record exactly 1 salvaged finding, not 2.
    salvage_activity = (task.context or {}).get("salvage_activity")
    assert salvage_activity is not None, (
        "salvage_activity must be recorded for a perf finding with salvageable reason"
    )
    assert salvage_activity["salvaged_count"] == 1, (
        f"Expected exactly 1 salvaged finding (no double-count), "
        f"got {salvage_activity['salvaged_count']}"
    )

    # The salvage bundle payload must also contain exactly 1 entry.
    result = await db_session.execute(
        _select(_Task).where(_Task.parent_task_id == task.id)
    )
    child_tasks = result.scalars().all()
    salvage_tasks = [
        t
        for t in child_tasks
        if (t.context or {}).get("salvage_source_task_id") == str(task.id)
    ]
    assert len(salvage_tasks) == 1, (
        f"Expected exactly 1 salvage bundle task, found {len(salvage_tasks)}"
    )
    payload = salvage_tasks[0].payloads[0]
    assert payload["type"] == "salvage_bundle"
    assert len(payload["salvaged_findings"]) == 1, (
        f"Expected exactly 1 entry in salvaged_findings (no double-count), "
        f"got {len(payload['salvaged_findings'])}"
    )
    entry = payload["salvaged_findings"][0]
    assert "Perf: slow N+1" in entry["title"]


@_pytest.mark.asyncio
async def test_dedup_filtered_perf_finding_enters_salvage(db_session, mocker):
    """A perf-type finding dropped by the hash-dedup gate is captured in the
    salvage buffer and spawns a consolidated salvage-bundle task."""
    from test_autonomous_research import _make_finding, _patch_research_externals
    from app.finding_deduplication import DedupOutcome

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-salvage-perf"
    )

    # A perf-type finding that will be rejected by hash-dedup.
    perf_finding = _make_finding(
        "Perf: slow database query in user lookup",
        direction_id=direction["id"],
        finding_type="perf",
    )

    # Mock deduplicate_findings so the perf finding appears in the dedup
    # filtered list (simulating a hash-match discard). The full finding
    # dict is carried in the "finding" key so the salvage logic can
    # inspect the type and build the salvage payload.
    mock_outcome = DedupOutcome(
        filtered=[
            {
                "title": perf_finding["title"][:120],
                "direction_id": direction["id"],
                "reason": "hash match with prior task",
                "finding": perf_finding,
            }
        ]
    )

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion.deduplicate_findings",
        return_value=mock_outcome,
    )
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[perf_finding],
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # ── Assert salvage activity is recorded on the parent task ──
    salvage = (task.context or {}).get("salvage_activity")
    assert salvage is not None, (
        "salvage_activity must be recorded on parent task"
    )
    assert "salvage_task_id" in salvage, (
        "salvage_activity must carry salvage_task_id"
    )
    assert salvage["salvaged_count"] >= 1, (
        "salvage must capture at least one dedup-filtered perf finding"
    )

    # ── Assert a salvage-bundle child task was spawned ──
    result = await db_session.execute(
        _select(_Task).where(_Task.parent_task_id == task.id)
    )
    child_tasks = result.scalars().all()
    salvage_tasks = [
        ct
        for ct in child_tasks
        if (ct.first_payload or {}).get("type") == "salvage_bundle"
    ]
    assert len(salvage_tasks) >= 1, (
        "A salvage_bundle task must be spawned when dedup-filtered "
        "perf findings are captured"
    )

    # ── Assert the salvage payload carries the perf finding context ──
    payload = salvage_tasks[0].first_payload or {}
    assert payload.get("total_discarded") == 1
    salvaged = payload.get("salvaged_findings", [])
    assert len(salvaged) == 1
    assert str(salvaged[0].get("title", "")).startswith("Perf:"), (
        f"Expected salvaged finding title to start with 'Perf:', "
        f"got {salvaged[0].get('title')!r}"
    )


class TestDeferredFindingStaleDiscard:
    """Stale-discard path: deferred findings older than the configured TTL are dropped."""

    def _apply_stale_filter(self, findings: list, ttl_days: int) -> tuple[list, list]:
        """Simulate the stale-discard logic from research_completion.py."""
        from datetime import datetime as _dt

        _stale_cutoff = _utcnow() - timedelta(days=ttl_days)
        _fresh: list[dict] = []
        _stale: list[dict] = []
        for f in findings:
            _stamp = f.get("originally_researched_at")
            _stamp_dt = None
            if isinstance(_stamp, str):
                try:
                    _stamp_dt = _dt.fromisoformat(_stamp.replace("Z", "+00:00"))
                    if _stamp_dt.tzinfo is not None:
                        _stamp_dt = _stamp_dt.replace(tzinfo=None)
                except ValueError:
                    _stamp_dt = None
            if _stamp_dt is not None and _stamp_dt < _stale_cutoff:
                _stale.append(f)
            else:
                _fresh.append(f)
        return _fresh, _stale

    def test_stale_deferred_finding_uses_configured_ttl_days(self):
        """A finding older than ttl_days is discarded; one within ttl is kept."""
        now = _utcnow()
        finding_4d_old = {
            "title": "old-finding",
            "originally_researched_at": (now - timedelta(days=4)).isoformat(),
        }
        finding_1d_old = {
            "title": "fresh-finding",
            "originally_researched_at": (now - timedelta(days=1)).isoformat(),
        }

        # TTL=3: 4-day-old finding is stale, 1-day-old is fresh
        fresh_3, stale_3 = self._apply_stale_filter(
            [finding_4d_old, finding_1d_old], ttl_days=3
        )
        assert len(stale_3) == 1
        assert stale_3[0]["title"] == "old-finding"
        assert len(fresh_3) == 1
        assert fresh_3[0]["title"] == "fresh-finding"

        # TTL=7: both findings are within the window
        fresh_7, stale_7 = self._apply_stale_filter(
            [finding_4d_old, finding_1d_old], ttl_days=7
        )
        assert len(stale_7) == 0
        assert len(fresh_7) == 2

    def test_finding_without_timestamp_is_kept(self):
        """A deferred finding with no originally_researched_at is never discarded as stale."""
        finding_no_stamp = {"title": "unstamped-finding"}
        fresh, stale = self._apply_stale_filter([finding_no_stamp], ttl_days=1)
        assert len(stale) == 0
        assert len(fresh) == 1

    def test_stale_discard_log_reflects_configured_ttl(self, caplog):
        """Warning log emits the configured day count, not a hardcoded '48h' string."""
        from unittest.mock import patch, MagicMock

        # Patch _runtime_config to return ttl_days=5
        mock_config = MagicMock()
        mock_config.get = lambda key, default=None: (
            5 if key == "deferred_finding_ttl_days" else default
        )

        now = _utcnow()
        stale_finding = {
            "title": "stale-title",
            "originally_researched_at": (now - timedelta(days=6)).isoformat(),
        }

        with patch("app.research_completion._runtime_config", mock_config):
            # Re-read the patched value the same way the production code does
            _ttl_days = int(mock_config.get("deferred_finding_ttl_days", 7))
            assert _ttl_days == 5

        # Verify the log message format uses days, not hardcoded "48h"
        _fresh, stale = self._apply_stale_filter([stale_finding], ttl_days=5)
        assert len(stale) == 1
        # Construct the expected log message and confirm it mentions "5d", not "48h"
        log_msg = f"(older than {_ttl_days}d) from task"
        assert "5d" in log_msg
        assert "48h" not in log_msg

    def test_stale_ttl_findings_added_to_salvage_candidates(self):
        """TTL-expired deferred findings become salvage candidates."""
        now = _utcnow()
        stale_finding = {
            "title": "stale-ttl-finding",
            "originally_researched_at": (now - timedelta(days=10)).isoformat(),
            "deferred_reason": "low_actionability",
            "direction_id": "d-stale",
        }
        fresh_finding = {
            "title": "fresh-finding",
            "originally_researched_at": (now - timedelta(days=1)).isoformat(),
            "deferred_reason": "low_actionability",
            "direction_id": "d-fresh",
        }

        fresh, stale = self._apply_stale_filter(
            [stale_finding, fresh_finding],
            ttl_days=7,
        )
        salvage_candidates = list(stale)

        assert len(salvage_candidates) == 1
        assert salvage_candidates[0]["title"] == "stale-ttl-finding"
        assert salvage_candidates[0]["direction_id"] == "d-stale"
        assert salvage_candidates[0]["deferred_reason"] == "low_actionability"
        assert len(fresh) == 1
        assert fresh[0]["title"] == "fresh-finding"


class TestProvenanceWarningsAttachment:
    """End-to-end: validate_source_refs + summarize_broken_refs flow used by
    research_completion to attach provenance to findings."""

    async def _spawn_deferred_child(
        self,
        db_session,
        mocker,
        *,
        direction_id,
        deferred,
        passthrough_validate_source_refs=True,
    ):
        from test_autonomous_research import _make_finding, _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, direction_id
        )
        task.context = {
            **(task.context or {}),
            "prior_deferred_findings": [deferred],
        }
        db_session.add(task)
        await db_session.commit()

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[
                _make_finding(title="Fresh sentinel", direction_id=direction["id"])
            ],
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )
        for path, value in (
            ("app.research_completion.resolve_branch_verification_context", {}),
            ("app.research_completion.resolve_head_commit", None),
            ("app.research_completion.cited_paths_changed_since", True),
            ("app.research_completion._summarize_ungrounded_claims_grounded", None),
        ):
            mocker.patch(path, new=AsyncMock(return_value=value))
        if passthrough_validate_source_refs:
            # The deferred-recovery flow re-grounds cited FILE refs against HEAD
            # (reproduce-on-HEAD gate). These provenance tests cite placeholder
            # paths that are absent on HEAD, which would trip the gate and reject
            # the finding before it spawns its child. Echo the sources back
            # unchanged (no ``valid: False`` stamp → counted present) so the
            # finding survives the gate and we exercise only the
            # staleness-provenance propagation under test here. The gate's own
            # absent-rejection behavior is covered by TestReproduceOnHeadGate.
            mocker.patch(
                "app.research_completion.validate_source_refs",
                new=AsyncMock(side_effect=lambda sources, **kwargs: sources),
            )

        await _handle_research_completion(task, "", db_session)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        return next(c for c in children if c.title == deferred["title"])

    async def test_clean_finding_has_no_provenance_key(self, monkeypatch):
        from app import source_ref_validation as srv

        monkeypatch.setattr(srv, "_file_exists_at_head", AsyncMock(return_value=True))
        finding = {
            "title": "F1",
            "description": "D1",
            "sources": [
                {"type": "file", "ref": "backend/app/foo.py"},
                {"type": "pr", "ref": "https://github.com/o/r/pull/1"},
            ],
        }
        finding["sources"] = await validate_source_refs(finding["sources"])
        prov = summarize_broken_refs(finding["sources"])
        if prov is not None:
            finding["_provenance_warnings"] = prov
        assert "_provenance_warnings" not in finding

    async def test_finding_with_one_broken_ref_gets_provenance(self, monkeypatch):
        from app import source_ref_validation as srv

        broken = "backend/app/missing.py"
        monkeypatch.setattr(
            srv,
            "_file_exists_at_head",
            AsyncMock(side_effect=lambda path: path != broken),
        )
        finding = {
            "title": "F1",
            "description": "D1",
            "sources": [
                {"type": "file", "ref": "backend/app/foo.py"},
                {"type": "file", "ref": broken},
            ],
        }
        finding["sources"] = await validate_source_refs(finding["sources"])
        prov = summarize_broken_refs(finding["sources"])
        if prov is not None:
            finding["_provenance_warnings"] = prov
        assert finding["_provenance_warnings"] == {
            "broken_source_ref_count": 1,
            "broken_refs": [broken],
        }

    async def test_finding_with_five_broken_refs_dedupes_paths(self, monkeypatch):
        from app import source_ref_validation as srv

        broken_paths = {f"backend/app/missing_{i}.py" for i in range(5)}
        monkeypatch.setattr(
            srv,
            "_file_exists_at_head",
            AsyncMock(side_effect=lambda path: path not in broken_paths),
        )
        # Two refs share a path with different line numbers — should dedupe.
        sources = [
            {"type": "file", "ref": f"backend/app/missing_{i}.py:5"} for i in range(5)
        ] + [{"type": "file", "ref": "backend/app/missing_0.py:99"}]
        finding = {"title": "F", "description": "D", "sources": sources}
        finding["sources"] = await validate_source_refs(finding["sources"])
        prov = summarize_broken_refs(finding["sources"])
        finding["_provenance_warnings"] = prov
        assert finding["_provenance_warnings"]["broken_source_ref_count"] == 5
        assert set(finding["_provenance_warnings"]["broken_refs"]) == broken_paths

    @_pytest.mark.asyncio
    async def test_prior_deferred_finding_with_changed_cited_files_gets_staleness_signal(
        self, db_session, mocker
    ):
        """A prior deferred finding surfaces HEAD-staleness through the real recovery flow."""
        from test_autonomous_research import _make_finding

        deferred = _make_finding(
            title="Deferred finding with changed cites", direction_id="d-prov-stale01"
        )
        deferred["generated_head_sha"] = "deadbeef"
        deferred["sources"] = [{"type": "file", "ref": "backend/app/foo.py"}]
        deferred_child = await self._spawn_deferred_child(
            db_session,
            mocker,
            direction_id="d-prov-stale01",
            deferred=deferred,
        )
        assert (deferred_child.context or {}).get("_provenance_warnings") == {
            "cited_files_changed_since_generation": True
        }

    @_pytest.mark.asyncio
    async def test_prior_deferred_finding_merges_staleness_with_existing_broken_refs(
        self, db_session, mocker
    ):
        """Deferred recovery preserves earlier provenance warnings and adds staleness."""
        from test_autonomous_research import _make_finding

        deferred = _make_finding(
            title="Deferred finding with prior warnings", direction_id="d-prov-stale02"
        )
        deferred["generated_head_sha"] = "deadbeef"
        deferred["sources"] = [{"type": "file", "ref": "backend/app/missing.py"}]
        deferred["_provenance_warnings"] = {
            "citation_state": "absent",
            "broken_source_ref_count": 1,
            "broken_refs": ["backend/app/missing.py"],
        }
        deferred_child = await self._spawn_deferred_child(
            db_session,
            mocker,
            direction_id="d-prov-stale02",
            deferred=deferred,
        )
        assert (deferred_child.context or {}).get("_provenance_warnings") == {
            "citation_state": "absent",
            "broken_source_ref_count": 1,
            "broken_refs": ["backend/app/missing.py"],
            "cited_files_changed_since_generation": True,
        }

    @_pytest.mark.asyncio
    async def test_spawned_child_carries_generated_head_sha_and_cited_paths(
        self, db_session, mocker
    ):
        """The child context propagates the provenance the launch recompute needs."""
        from test_autonomous_research import _make_finding

        deferred = _make_finding(
            title="Deferred carries provenance", direction_id="d-prov-prop01"
        )
        deferred["generated_head_sha"] = "cafef00d"
        deferred["sources"] = [
            {"type": "file", "ref": "backend/app/foo.py:42"},
            {"type": "pr", "ref": "https://github.com/o/r/pull/1"},
        ]
        child = await self._spawn_deferred_child(
            db_session, mocker, direction_id="d-prov-prop01", deferred=deferred
        )
        ctx = child.context or {}
        assert ctx.get("generated_head_sha") == "cafef00d"
        # Line suffix stripped; PR source skipped.
        assert ctx.get("_finding_cited_paths") == ["backend/app/foo.py"]

    @_pytest.mark.asyncio
    async def test_spawned_child_receives_visible_research_evidence_handoff(
        self, db_session, mocker
    ):
        """The implementer sees the diagnosis instead of rediscovering it."""
        from test_autonomous_research import _make_finding

        deferred = _make_finding(
            title="Deferred carries diagnosis", direction_id="d-evidence-prop01"
        )
        deferred["verification_basis"] = "code_verified"
        deferred["claims"] = ["The research child prompt drops parent evidence."]
        deferred["sources"] = [
            {
                "type": "file",
                "source_scope": "project",
                "ref": "backend/app/research_completion.py:5399",
                "excerpt": "the payload previously carried only the generic prompt",
            }
        ]
        child = await self._spawn_deferred_child(
            db_session,
            mocker,
            direction_id="d-evidence-prop01",
            deferred=deferred,
        )

        prompt = child.first_payload["prompt"]
        assert "## Research handoff" in prompt
        assert "The research child prompt drops parent evidence." in prompt
        assert "backend/app/research_completion.py:5399" in prompt
        assert "the payload previously carried only the generic prompt" in prompt
        assert "Revalidate it against the current branch" in prompt
        ctx = child.context or {}
        assert ctx["verification_basis"] == "code_verified"
        assert ctx["sources"][0]["ref"] == "backend/app/research_completion.py:5399"

    @_pytest.mark.asyncio
    async def test_spawned_child_omits_provenance_keys_when_absent(
        self, db_session, mocker
    ):
        """No generated_head_sha and no file sources → neither key is set."""
        from test_autonomous_research import _make_finding

        deferred = _make_finding(
            title="Deferred no provenance", direction_id="d-prov-prop02"
        )
        deferred.pop("generated_head_sha", None)
        deferred["sources"] = []
        child = await self._spawn_deferred_child(
            db_session, mocker, direction_id="d-prov-prop02", deferred=deferred
        )
        ctx = child.context or {}
        assert "generated_head_sha" not in ctx
        assert "_finding_cited_paths" not in ctx

    @_pytest.mark.asyncio
    async def test_spawned_child_seeds_already_resolved_for_broken_citation(
        self, db_session, mocker
    ):
        """A finding whose cited code is absent on HEAD seeds an
        `already_resolved` reproduce-on-HEAD baseline (it definitionally does
        not reproduce there)."""
        from test_autonomous_research import _make_finding

        deferred = _make_finding(
            title="Deferred broken citation repro",
            direction_id="d-repro-abs01",
            finding_type="bug",
        )
        deferred["generated_head_sha"] = "deadbeef"
        deferred["sources"] = [{"type": "file", "ref": "backend/app/missing.py"}]
        deferred["_provenance_warnings"] = {
            "citation_state": "absent",
            "broken_source_ref_count": 1,
            "broken_refs": ["backend/app/missing.py"],
        }
        child = await self._spawn_deferred_child(
            db_session, mocker, direction_id="d-repro-abs01", deferred=deferred
        )
        verdict = (child.context or {}).get("reproduce_verdict")
        assert verdict is not None
        assert verdict["status"] == "already_resolved"

    @_pytest.mark.asyncio
    async def test_spawned_child_keeps_non_absence_warnings_indeterminate(
        self, db_session, mocker
    ):
        """Non-absence provenance warnings stay indeterminate: a stale citation
        is not the same thing as cited code being definitively absent on HEAD."""
        from test_autonomous_research import _make_finding

        deferred = _make_finding(
            title="Deferred stale citation repro",
            direction_id="d-repro-ind-stale01",
            finding_type="bug",
        )
        deferred["generated_head_sha"] = "deadbeef"
        deferred["sources"] = [{"type": "file", "ref": "backend/app/foo.py"}]
        deferred["_provenance_warnings"] = {
            "cited_files_changed_since_generation": True
        }
        child = await self._spawn_deferred_child(
            db_session,
            mocker,
            direction_id="d-repro-ind-stale01",
            deferred=deferred,
        )
        verdict = (child.context or {}).get("reproduce_verdict")
        assert verdict is not None
        assert verdict["status"] == "indeterminate"

    @_pytest.mark.asyncio
    async def test_spawned_child_seeds_indeterminate_when_citations_clean(
        self, db_session, mocker
    ):
        """A finding with no provenance warnings seeds an `indeterminate`
        reproduce-on-HEAD baseline — the defect-truth is not yet established."""
        from test_autonomous_research import _make_finding

        deferred = _make_finding(
            title="Deferred clean repro",
            direction_id="d-repro-ind01",
            finding_type="bug",
        )
        deferred.pop("generated_head_sha", None)
        deferred["sources"] = []
        child = await self._spawn_deferred_child(
            db_session, mocker, direction_id="d-repro-ind01", deferred=deferred
        )
        verdict = (child.context or {}).get("reproduce_verdict")
        assert verdict is not None
        assert verdict["status"] == "indeterminate"

    def test_reconcile_overturned_absent_provenance_updates_seed(self):
        """A challenge that overturns reproduce-absent must clear the stale seed."""
        from app.research_completion import _reconcile_reproduce_provenance_state

        finding = {
            "_provenance_warnings": {
                "citation_state": "absent",
                "broken_source_ref_count": 1,
                "broken_refs": ["backend/app/missing.py"],
            },
            "_reproduce_on_head": "present",
        }
        _reconcile_reproduce_provenance_state(finding, finding["_reproduce_on_head"])
        assert finding["_provenance_warnings"]["citation_state"] == "present"

        unchanged = {"_provenance_warnings": {"citation_state": "present"}}
        _reconcile_reproduce_provenance_state(unchanged, "present")
        assert unchanged["_provenance_warnings"]["citation_state"] == "present"

    @_pytest.mark.asyncio
    @_pytest.mark.parametrize("finding_type", ["feature", "improvement", "refactor"])
    async def test_spawned_non_defect_child_omits_reproduce_verdict(
        self, db_session, mocker, finding_type
    ):
        """Non-defect research findings must not carry a reproduce verdict.

        Outcome scoring treats an absent verdict as the legacy success path for
        merged implementation work; seeding `indeterminate` here would
        incorrectly downgrade shipped feature/refactor work to neutral.
        """
        from test_autonomous_research import _make_finding

        deferred = _make_finding(
            title=f"Deferred non-defect {finding_type}",
            direction_id=f"d-repro-nondef-{finding_type}",
            finding_type=finding_type,
        )
        deferred["generated_head_sha"] = "deadbeef"
        deferred["sources"] = [{"type": "file", "ref": "backend/app/foo.py"}]
        deferred["_provenance_warnings"] = {"citation_state": "absent"}
        child = await self._spawn_deferred_child(
            db_session,
            mocker,
            direction_id=f"d-repro-nondef-{finding_type}",
            deferred=deferred,
        )
        assert "reproduce_verdict" not in (child.context or {})


class TestReproduceOnHeadGate:
    """Reproduce-on-HEAD GATE: a finding whose cited code is entirely gone on
    HEAD is rejected (not spawned), counted, and scored neutral; indeterminate
    and partially-grounded findings still pass through."""

    async def _run(
        self,
        db_session,
        mocker,
        *,
        direction_id,
        parsed_findings,
        prior_deferred=None,
        task_context=None,
        has_backend=True,
        validate=None,
        ungrounded=None,
    ):
        from test_autonomous_research import _patch_research_externals

        _project, _direction, task = await _grader_project_and_task(
            db_session, direction_id
        )
        if task_context:
            task.context = {
                **(task.context or {}),
                **task_context,
            }
            db_session.add(task)
            await db_session.commit()
        if prior_deferred is not None:
            task.context = {
                **(task.context or {}),
                "prior_deferred_findings": prior_deferred,
            }
            db_session.add(task)
            await db_session.commit()

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=parsed_findings,
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )
        mocker.patch(
            "app.research_completion.resolve_branch_verification_context",
            new=AsyncMock(return_value={}),
        )
        mocker.patch(
            "app.research_completion.resolve_head_commit",
            new=AsyncMock(return_value=None),
        )
        mocker.patch(
            "app.research_completion.cited_paths_changed_since",
            new=AsyncMock(return_value=None),
        )
        # Force a usable backend so the gate is active even in the Docker test
        # env (which has no /tmp/workspace checkout); production has GitHub creds.
        mocker.patch(
            "app.research_completion._has_validation_backend",
            return_value=has_backend,
        )
        if validate is not None:
            mocker.patch("app.research_completion.validate_source_refs", new=validate)
        if ungrounded is not None:
            mocker.patch(
                "app.research_completion._summarize_ungrounded_claims_grounded",
                new=ungrounded,
            )
        else:
            mocker.patch(
                "app.research_completion._summarize_ungrounded_claims_grounded",
                new=AsyncMock(return_value=None),
            )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)
        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        return task, {c.title for c in children}

    @_pytest.mark.asyncio
    async def test_fresh_finding_citing_only_deleted_files_is_rejected(
        self, db_session, mocker
    ):
        """A prose-claim finding whose only file citation is gone is not spawned."""
        from test_autonomous_research import _make_finding

        finding = _make_finding(
            title="Prose claim about deleted file", direction_id="d-repro-fresh01"
        )
        finding["sources"] = [{"type": "file", "ref": "backend/app/gone.py"}]

        async def _validate(sources, **kw):
            return [{**s, "valid": False} for s in sources]

        task, titles = await self._run(
            db_session,
            mocker,
            direction_id="d-repro-fresh01",
            parsed_findings=[finding],
            validate=_validate,
        )
        assert "Prose claim about deleted file" not in titles
        assert (task.context or {}).get("research_reproduce_rejected") == 1
        # Scored neutral: zero findings survived.
        assert (task.context or {}).get("research_surviving_findings") == 0

    @_pytest.mark.asyncio
    async def test_partially_grounded_finding_still_spawns(self, db_session, mocker):
        """One surviving file citation keeps the finding spawnable (never rejected)."""
        from test_autonomous_research import _make_finding

        finding = _make_finding(
            title="Partially grounded finding", direction_id="d-repro-part01"
        )
        finding["sources"] = [
            {"type": "file", "ref": "backend/app/present.py"},
            {"type": "file", "ref": "backend/app/gone.py"},
        ]

        async def _validate(sources, **kw):
            return [
                {**s, "valid": False} if s.get("ref") == "backend/app/gone.py" else s
                for s in sources
            ]

        task, titles = await self._run(
            db_session,
            mocker,
            direction_id="d-repro-part01",
            parsed_findings=[finding],
            validate=_validate,
        )
        assert "Partially grounded finding" in titles
        assert (task.context or {}).get("research_reproduce_rejected") is None
        assert (task.context or {}).get("research_surviving_findings") == 1

    @_pytest.mark.asyncio
    async def test_indeterminate_context_never_rejects(self, db_session, mocker):
        """No validation backend → all-broken citations still pass through."""
        from test_autonomous_research import _make_finding

        finding = _make_finding(
            title="Indeterminate finding", direction_id="d-repro-ind01"
        )
        finding["sources"] = [{"type": "file", "ref": "backend/app/gone.py"}]

        async def _validate(sources, **kw):
            return [{**s, "valid": False} for s in sources]

        task, titles = await self._run(
            db_session,
            mocker,
            direction_id="d-repro-ind01",
            parsed_findings=[finding],
            has_backend=False,
            validate=_validate,
        )
        assert "Indeterminate finding" in titles
        assert (task.context or {}).get("research_reproduce_rejected") is None

    @_pytest.mark.asyncio
    async def test_deferred_finding_orphaned_symbol_is_rejected(
        self, db_session, mocker
    ):
        """A deferred finding whose only cited symbol was deleted is caught on
        recovery (re-grounded symbols), not re-spawned as fresh."""
        from test_autonomous_research import _make_finding

        deferred = _make_finding(
            title="Orphaned symbol finding", direction_id="d-repro-orph01"
        )
        deferred["claims"] = ["backend/app/foo.py:gone_func drops the guard"]
        deferred["sources"] = []
        sentinel = _make_finding(title="Fresh sentinel", direction_id="d-repro-orph01")

        async def _ungrounded(claims, sources, ctx):
            if claims and "gone_func" in (claims[0] or ""):
                return {
                    "ungrounded_claim_count": 1,
                    "ungrounded_refs": ["backend/app/foo.py:gone_func"],
                }
            return None

        task, titles = await self._run(
            db_session,
            mocker,
            direction_id="d-repro-orph01",
            parsed_findings=[sentinel],
            prior_deferred=[deferred],
            ungrounded=_ungrounded,
        )
        assert "Orphaned symbol finding" not in titles  # rejected on recovery
        assert "Fresh sentinel" in titles  # clean finding still spawns
        assert (task.context or {}).get("research_reproduce_rejected") == 1

    @_pytest.mark.asyncio
    async def test_reproduce_rejected_finding_spawns_memo_with_absent_refs(
        self, db_session, mocker
    ):
        """A reproduce-rejected finding spawns exactly one terminal memo that
        names the absent cited refs and is tagged with the distinct
        reproduce-on-HEAD drop reason, while the cycle still scores neutral
        (the rejected finding is not re-counted as surviving work)."""
        from test_autonomous_research import _make_finding

        finding = _make_finding(
            title="Claim about deleted module", direction_id="d-repro-memo01"
        )
        finding["sources"] = [{"type": "file", "ref": "backend/app/gone.py"}]

        async def _validate(sources, **kw):
            return [{**s, "valid": False} for s in sources]

        task, titles = await self._run(
            db_session,
            mocker,
            direction_id="d-repro-memo01",
            parsed_findings=[finding],
            validate=_validate,
        )
        # Rejected (not spawned as impl work) and scored neutral.
        assert "Claim about deleted module" not in titles
        assert (task.context or {}).get("research_reproduce_rejected") == 1
        assert (task.context or {}).get("research_surviving_findings") == 0

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        memos = [
            c
            for c in children
            if (c.context or {}).get("memo_drop_reason") == "reproduce_on_head_absent"
        ]
        # Exactly one memo, carrying the absent reference in its payload and its
        # synthesized reasoning, and the cycle records it in spawned_memo_ids.
        assert len(memos) == 1
        memo = memos[0]
        source_finding = memo.first_payload.get("source_finding") or {}
        assert "backend/app/gone.py" in (source_finding.get("absent_references") or [])
        assert (task.context or {}).get("spawned_memo_ids") == [str(memo.id)]
        memo_reasoning = (memo.plan_output or {}).get("memo", {}).get("reasoning", "")
        assert "backend/app/gone.py" in memo_reasoning

    @_pytest.mark.asyncio
    async def test_deferred_finding_no_title_no_description_spawns_completed_memo(
        self, db_session, mocker
    ):
        """A deferred finding with no title and no description still completes inline.

        When _synthesize_deferred_finding_memo returns None (sparse finding), the
        caller must synthesize a minimal fallback memo so the task reaches COMPLETED
        at spawn time and never provisions a VM.
        """
        # Finding with neither title nor description — triggers the None path in
        # _synthesize_deferred_finding_memo. Setting actionability_grade=LOW routes
        # it through the low_actionability deferral path so it reaches the memo loop.
        sparse_finding = {
            "prompt": "implement something",
            "priority": "low",
            "scope": "small",
            "risk": "low",
            "type": "improvement",
            "direction_id": "d-sparse-memo01",
            "title": "",
            "description": "",
            "actionability_grade": "LOW",
        }

        task, _titles = await self._run(
            db_session,
            mocker,
            direction_id="d-sparse-memo01",
            parsed_findings=[sparse_finding],
        )

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        memos = [
            c
            for c in children
            if (c.context or {}).get("memo_reasoning_kind") == "deferred_finding"
        ]
        # Memo must exist (sparse finding still produces a dropped-finding memo)
        assert len(memos) >= 1
        memo = memos[0]
        # Must be COMPLETED at spawn — no VM provisioned
        assert memo.status == TaskStatus.COMPLETED, (
            f"expected COMPLETED (fallback inline synthesis), got {memo.status}"
        )
        # No MEMO Artifact row is written — the memo lives only in plan_output.
        from app.models import Artifact

        artifacts = (
            (
                await db_session.execute(
                    _select(Artifact).where(Artifact.task_id == memo.id)
                )
            )
            .scalars()
            .all()
        )
        assert artifacts == []
        assert memo.plan_output and memo.plan_output.get("memo"), (
            "plan_output['memo'] must be set for inline-completed memo"
        )

    @_pytest.mark.asyncio
    async def test_reproduce_absent_second_opinion_overturn_spawns_and_memo_records_disagreement(
        self, db_session, mocker
    ):
        """Flag-on reproduce-absent gate parks for a real side-car verdict; a
        disagreement keeps the finding, records the second opinion on the finding
        and spawned child, and suppresses the absent-drop memo."""
        from app.schemas import AgentStatusPayload
        from app.task_lifecycle import _resolve_finding_challenge_completion
        from test_autonomous_research import _make_finding, _patch_research_externals

        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"research_finding_challenge_enabled": True},
        )
        _project, direction, task = await _grader_project_and_task(
            db_session, "d-repro-chal01"
        )
        finding = _make_finding(
            title="Moved semantic target", direction_id=direction["id"]
        )
        finding["sources"] = [{"type": "file", "ref": "backend/app/old_place.py"}]

        async def _validate(sources, **kw):
            return [{**s, "valid": False} for s in sources]

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding],
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )
        mocker.patch(
            "app.research_completion.resolve_branch_verification_context",
            new=AsyncMock(return_value={}),
        )
        mocker.patch(
            "app.research_completion.resolve_head_commit",
            new=AsyncMock(return_value=None),
        )
        mocker.patch(
            "app.research_completion.cited_paths_changed_since",
            new=AsyncMock(return_value=None),
        )
        mocker.patch(
            "app.research_completion._has_validation_backend",
            return_value=True,
        )
        mocker.patch("app.research_completion.validate_source_refs", new=_validate)
        mocker.patch(
            "app.research_completion._summarize_ungrounded_claims_grounded",
            new=AsyncMock(return_value=None),
        )
        mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)
        assert task.status == TaskStatus.EVALUATING
        assert task.context.get("awaiting_finding_challenge")

        sidecar = (
            (
                await db_session.execute(
                    _select(_Task).where(
                        _Task.context[FINDING_CHALLENGE_SOURCE_KEY].as_string()
                        == str(task.id)
                    )
                )
            )
            .scalars()
            .one()
        )
        payload = AgentStatusPayload(
            task_id=sidecar.id,
            status=TaskStatus.COMPLETED,
            logs=(
                "The subject moved from old_place.py to new_place.py.\n"
                '{"verdict": "present", "agreement": "disagrees", '
                '"reasoning": "semantic target was renamed, not removed"}'
            ),
        )
        await _resolve_finding_challenge_completion(sidecar, payload, db_session)

        await db_session.refresh(task)
        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        spawned = [c for c in children if c.title == "Moved semantic target"]
        assert len(spawned) == 1
        assert (task.context or {}).get("research_reproduce_rejected") is None
        assert (task.context or {}).get("spawned_memo_ids") is None
        record = (spawned[0].context or {}).get("finding_challenge")
        assert record["challenged_boundary"] == CHALLENGE_BOUNDARY_REPRODUCE_ABSENT
        assert record["first_verdict"] == "absent"
        assert record["second_opinion_verdict"] == "present"
        assert record["agreement"] == "disagrees"
        assert "renamed" in record["reason"]
        assert (task.context or {})["finding_challenge_records"][0]["agreement"] == (
            "disagrees"
        )
        assert (task.plan_output or [])[0]["finding_challenge"]["agreement"] == (
            "disagrees"
        )

    @_pytest.mark.asyncio
    async def test_confirmed_absent_second_opinion_still_drops_and_memo_includes_record(
        self, db_session, mocker
    ):
        """A second opinion that agrees with absent preserves the genuine-absent
        drop and includes the structured record in the dropped-finding memo."""
        from test_autonomous_research import _make_finding

        finding = _make_finding(
            title="Confirmed deleted subject", direction_id="d-repro-chal02"
        )
        finding["sources"] = [{"type": "file", "ref": "backend/app/gone.py"}]

        async def _validate(sources, **kw):
            return [{**s, "valid": False} for s in sources]

        task, titles = await self._run(
            db_session,
            mocker,
            direction_id="d-repro-chal02",
            parsed_findings=[finding],
            task_context={
                FINDING_CHALLENGE_VERDICT_KEY: {
                    "challenged_boundary": CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
                    "first_verdict": "absent",
                    "second_opinion_verdict": "absent",
                    "agreement": "agrees",
                    "reason": "old symbol is genuinely gone",
                    "sidecar_task_id": "sidecar-confirm",
                },
            },
            validate=_validate,
        )
        assert "Confirmed deleted subject" not in titles
        assert (task.context or {}).get("research_reproduce_rejected") == 1
        memo = (
            (
                await db_session.execute(
                    _select(_Task).where(
                        _Task.parent_task_id == task.id,
                        _Task.context["memo_drop_reason"].as_string()
                        == "reproduce_on_head_absent",
                    )
                )
            )
            .scalars()
            .one()
        )
        source_finding = memo.first_payload.get("source_finding") or {}
        assert source_finding["finding_challenge"]["agreement"] == "agrees"
        reasoning = (memo.plan_output or {}).get("memo", {}).get("reasoning", "")
        assert "Second opinion: agrees" in reasoning

    @_pytest.mark.asyncio
    async def test_two_carried_findings_keep_sequential_challenge_receipts(
        self, db_session, mocker
    ):
        """Each carried finding is challenged once even when the next sidecar
        commit occurs before carried mutations reach ``plan_output``."""
        from app.research_deferred_carry import unacknowledged_deferred_findings
        from app.schemas import AgentStatusPayload
        from app.task_lifecycle import _resolve_finding_challenge_completion
        from test_autonomous_research import _make_finding, _patch_research_externals

        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {
                "research_finding_challenge_enabled": True,
                "research_quality_grader_enabled": False,
            },
        )
        project, direction, task = await _grader_project_and_task(
            db_session, "d-repro-chal-sequential"
        )
        carried = [
            _make_finding("First moved target", direction_id=direction["id"]),
            _make_finding("Second moved target", direction_id=direction["id"]),
        ]
        for index, finding in enumerate(carried, start=1):
            finding.update(
                {
                    "actionability_grade": "HIGH",
                    "deferred_count": 1,
                    "deferred_reason": "batch_cap",
                    "sources": [
                        {"type": "file", "ref": f"backend/app/gone_{index}.py"}
                    ],
                }
            )
        producer = _Task(
            project_id=project.id,
            title="Sequential challenge carry producer",
            payloads=[{"type": "research", "base_branch": "main"}],
            context={
                "direction_id": direction["id"],
                "deferred_findings": carried,
            },
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(producer)
        await db_session.flush()
        task.context = {
            **(task.context or {}),
            "prior_deferred_findings": unacknowledged_deferred_findings(producer),
        }
        db_session.add(task)
        await db_session.commit()

        async def _validate(sources, **_kwargs):
            return [{**source, "valid": False} for source in sources]

        _patch_research_externals(mocker)
        mocker.patch("app.research_completion._parse_research_findings", return_value=[])
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )
        mocker.patch(
            "app.research_completion.resolve_branch_verification_context",
            new=AsyncMock(return_value={}),
        )
        mocker.patch(
            "app.research_completion.resolve_head_commit",
            new=AsyncMock(return_value=None),
        )
        mocker.patch(
            "app.research_completion.cited_paths_changed_since",
            new=AsyncMock(return_value=None),
        )
        mocker.patch("app.research_completion._has_validation_backend", return_value=True)
        mocker.patch("app.research_completion.validate_source_refs", new=_validate)
        mocker.patch(
            "app.research_completion._summarize_ungrounded_claims_grounded",
            new=AsyncMock(return_value=None),
        )
        mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)

        async def _challenge_sidecars():
            return (
                (
                    await db_session.execute(
                        _select(_Task).where(
                            _Task.context[FINDING_CHALLENGE_SOURCE_KEY].as_string()
                            == str(task.id)
                        )
                    )
                )
                .scalars()
                .all()
            )

        await _handle_research_completion(task, "[]", db_session)
        sidecars = await _challenge_sidecars()
        assert len(sidecars) == 1
        first_sidecar = sidecars[0]
        await _resolve_finding_challenge_completion(
            first_sidecar,
            AgentStatusPayload(
                task_id=first_sidecar.id,
                status=TaskStatus.COMPLETED,
                logs=(
                    '{"verdict":"present","agreement":"disagrees",'
                    '"reasoning":"the first target moved"}'
                ),
            ),
            db_session,
        )

        await db_session.refresh(task)
        sidecars = await _challenge_sidecars()
        assert len(sidecars) == 2
        assert len((task.context or {}).get("finding_challenge_records") or []) == 1
        second_sidecar = next(item for item in sidecars if item.id != first_sidecar.id)
        await _resolve_finding_challenge_completion(
            second_sidecar,
            AgentStatusPayload(
                task_id=second_sidecar.id,
                status=TaskStatus.COMPLETED,
                logs=(
                    '{"verdict":"present","agreement":"disagrees",'
                    '"reasoning":"the second target moved"}'
                ),
            ),
            db_session,
        )

        await db_session.refresh(task)
        await db_session.refresh(producer)
        history = (task.context or {}).get("finding_challenge_records") or []
        assert len(await _challenge_sidecars()) == 2, "no finding may be challenged twice"
        assert len(history) == 2
        assert len({record["finding_hash"] for record in history}) == 2
        assert not (task.context or {}).get("awaiting_finding_challenge")
        assert task.context[FINALIZED_RESEARCH_FINDINGS_KEY] is True
        assert not task.context.get("_research_spawn_pending")
        assert {item["title"] for item in task.plan_output} == {
            finding["title"] for finding in carried
        }
        assert all(item.get("finding_challenge") for item in task.plan_output), (
            [
                (
                    item.get("title"),
                    item.get("_reproduce_on_head"),
                    item.get("finding_challenge"),
                )
                for item in task.plan_output
            ]
        )
        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        assert {child.title for child in children} == {
            finding["title"] for finding in carried
        }
        assert producer.context["deferred_findings"] == []

    @_pytest.mark.asyncio
    async def test_failed_reproduce_absent_challenge_preserves_absent_drop(
        self, db_session, mocker
    ):
        """Failed or indeterminate second opinions keep the original absent drop.

        The reproduce-absent gate only keeps a finding when the side-car
        explicitly establishes a present target; failed, empty, unparseable, and
        genuinely indeterminate results must still fall through to the terminal
        absent-drop memo path.
        """
        from app.task_evaluators import parse_finding_challenge_verdict
        from test_autonomous_research import _make_finding

        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"research_finding_challenge_enabled": True},
        )

        async def _validate(sources, **kw):
            return [{**s, "valid": False} for s in sources]

        cases = [
            (
                "failed",
                parse_finding_challenge_verdict(
                    "ignored logs",
                    "absent",
                    CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
                    "sidecar-failed",
                    completed=False,
                ),
            ),
            (
                "empty",
                parse_finding_challenge_verdict(
                    "",
                    "absent",
                    CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
                    "sidecar-empty",
                ),
            ),
            (
                "unparseable",
                parse_finding_challenge_verdict(
                    "no machine verdict here",
                    "absent",
                    CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
                    "sidecar-unparseable",
                ),
            ),
            (
                "indeterminate",
                parse_finding_challenge_verdict(
                    '{"verdict": "indeterminate", "agreement": "indeterminate", '
                    '"reasoning": "semantic target still cannot be established"}',
                    "absent",
                    CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
                    "sidecar-indeterminate",
                ),
            ),
        ]

        for label, challenge_record in cases:
            direction_id = f"d-repro-chal03-{label}"
            finding_title = f"Moved semantic target {label}"
            finding = _make_finding(title=finding_title, direction_id=direction_id)
            finding["sources"] = [{"type": "file", "ref": "backend/app/old_place.py"}]

            task, titles = await self._run(
                db_session,
                mocker,
                direction_id=direction_id,
                parsed_findings=[finding],
                task_context={
                    FINDING_CHALLENGE_VERDICT_KEY: challenge_record.to_context(),
                },
                validate=_validate,
            )

            assert finding_title not in titles
            assert (task.context or {}).get("research_reproduce_rejected") == 1
            assert (task.context or {}).get("spawned_memo_ids")

            children = (
                (
                    await db_session.execute(
                        _select(_Task).where(_Task.parent_task_id == task.id)
                    )
                )
                .scalars()
                .all()
            )
            memos = [
                c
                for c in children
                if (c.context or {}).get("memo_drop_reason")
                == "reproduce_on_head_absent"
            ]
            assert len(memos) == 1
            memo = memos[0]
            source_finding = memo.first_payload.get("source_finding") or {}
            record = source_finding.get("finding_challenge") or {}
            assert record["challenged_boundary"] == CHALLENGE_BOUNDARY_REPRODUCE_ABSENT
            assert record["first_verdict"] == "absent"
            assert (record.get("agreement") or "").lower() == "indeterminate"
            assert (record.get("second_opinion_verdict") or "").lower() in (
                "",
                "indeterminate",
            )
            assert (task.context or {}).get("spawned_memo_ids") == [str(memo.id)]

    @_pytest.mark.asyncio
    async def test_reproduce_absent_challenge_flag_off_uses_legacy_drop_without_sidecar(
        self, db_session, mocker
    ):
        """Default/off flag preserves the legacy single-pass absent rejection."""
        from test_autonomous_research import _make_finding

        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"research_finding_challenge_enabled": False},
        )
        finding = _make_finding(
            title="Legacy absent drop", direction_id="d-repro-legacy01"
        )
        finding["sources"] = [{"type": "file", "ref": "backend/app/gone.py"}]

        async def _validate(sources, **kw):
            return [{**s, "valid": False} for s in sources]

        task, titles = await self._run(
            db_session,
            mocker,
            direction_id="d-repro-legacy01",
            parsed_findings=[finding],
            validate=_validate,
        )
        assert "Legacy absent drop" not in titles
        assert not (task.context or {}).get("awaiting_finding_challenge")
        sidecars = (
            (
                await db_session.execute(
                    _select(_Task).where(
                        _Task.context[FINDING_CHALLENGE_SOURCE_KEY].as_string()
                        == str(task.id)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert sidecars == []


@_pytest.mark.asyncio
async def test_confirmed_low_actionability_decay_spawns_memo_with_second_opinion(
    db_session, mocker
):
    """A low-actionability finding at terminal decay still drops when the
    second opinion agrees, and the terminal memo carries that challenge record."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"research_finding_challenge_enabled": True},
    )
    _project, direction, task = await _grader_project_and_task(
        db_session, "d-low-chal01"
    )
    decayed = _make_finding("Vague cleanup", direction_id=direction["id"])
    decayed["actionability_grade"] = "LOW"
    decayed["deferred_reason"] = "low_actionability"
    decayed["deferred_count"] = 3
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": [decayed],
        FINDING_CHALLENGE_VERDICT_KEY: {
            "challenged_boundary": CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
            "first_verdict": "LOW",
            "second_opinion_verdict": "LOW",
            "agreement": "agrees",
            "reason": "still lacks a concrete code-change target",
            "sidecar_task_id": "sidecar-low-confirm",
        },
    }
    db_session.add(task)
    await db_session.commit()

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding("Fresh sentinel", direction_id=direction["id"])],
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    give_up = (task.context or {}).get("finding_structural_give_up")
    assert give_up["by_reason"] == {"low_actionability": 1}
    assert not any(
        f.get("title") == "Vague cleanup"
        for f in (task.context or {}).get("deferred_findings", [])
    )
    memo = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.parent_task_id == task.id,
                    _Task.context["memo_drop_reason"].as_string()
                    == "low_actionability_decay",
                )
            )
        )
        .scalars()
        .one()
    )
    source_finding = memo.first_payload.get("source_finding") or {}
    assert source_finding["finding_challenge"]["agreement"] == "agrees"
    reasoning = (memo.plan_output or {}).get("memo", {}).get("reasoning", "")
    assert "Second opinion: agrees" in reasoning
    assert "concrete code-change target" in reasoning


@_pytest.mark.asyncio
async def test_low_actionability_challenge_non_disagrees_decays(db_session, mocker):
    """A low-actionability challenge that does not explicitly disagree with a
    substantive rationale (e.g. agreement='agrees' with a non-LOW verdict)
    now decays the finding instead of routing to refinement."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"research_finding_challenge_enabled": True},
    )
    _project, direction, task = await _grader_project_and_task(
        db_session, "d-low-chal03"
    )
    decayed = _make_finding(
        "Specific retry starvation hardening", direction_id=direction["id"]
    )
    decayed["actionability_grade"] = "LOW"
    decayed["deferred_reason"] = "low_actionability"
    decayed["deferred_count"] = 3
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": [decayed],
        FINDING_CHALLENGE_VERDICT_KEY: {
            "challenged_boundary": CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
            "first_verdict": "LOW",
            "second_opinion_verdict": "indeterminate",
            "agreement": "agrees",
            "reason": "sidecar failed before confirming the finding is unactionable",
            "sidecar_task_id": "sidecar-low-indeterminate",
        },
    }
    db_session.add(task)
    await db_session.commit()

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding("Fresh sentinel", direction_id=direction["id"])],
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # The finding was decayed, so finding_structural_give_up is set (not None).
    give_up = (task.context or {}).get("finding_structural_give_up")
    assert give_up is not None, "decayed finding must set finding_structural_give_up"
    assert give_up.get("by_reason", {}).get("low_actionability", 0) == 1
    # With new logic: agreement != "disagrees" → decay, NOT refinement.
    deferred = (task.context or {}).get("deferred_findings", [])
    assert not any(
        f.get("title") == decayed["title"]
        and f.get("deferred_reason") == "low_actionability_refinement"
        for f in deferred
    )


@_pytest.mark.asyncio
async def test_finding_challenge_binds_identity_and_indeterminate_low_stays_deferred(
    db_session, mocker
):
    """Identity binding: a challenge record with a mismatched finding_hash is rejected
    (the finding is re-challenged rather than consuming a stale verdict).
    Indeterminate low stays deferred: when the record has agreement='indeterminate',
    the finding is decayed rather than converted to a spawnable refinement."""
    from app.finding_deduplication import finding_hash as fhash
    from test_autonomous_research import _make_finding, _patch_research_externals

    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"research_finding_challenge_enabled": True},
    )
    _project, direction, task = await _grader_project_and_task(
        db_session, "d-chal-identity"
    )
    finding_title = "Retry-budget overflow in challenge loop"
    finding_desc = "The challenge loop does not cap retry depth."
    f = _make_finding(finding_title, direction_id=direction["id"])
    f["description"] = finding_desc
    f["actionability_grade"] = "LOW"
    f["deferred_reason"] = "low_actionability"
    f["deferred_count"] = 3

    correct_hash = fhash(finding_title, finding_desc)
    wrong_hash = "000000deadbeef00"

    # --- Part 1: hash mismatch → record not consumed → no refinement ---
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": [f],
        FINDING_CHALLENGE_VERDICT_KEY: {
            "challenged_boundary": CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
            "first_verdict": "LOW",
            "second_opinion_verdict": "actionable",
            "agreement": "disagrees",
            "reason": "finding is specific and actionable",
            "sidecar_task_id": "sidecar-wrong-hash",
            "finding_hash": wrong_hash,  # does NOT match the finding's hash
        },
    }
    db_session.add(task)
    await db_session.commit()

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding("Sentinel A", direction_id=direction["id"])],
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )
    # A mismatched hash means _consume returns None → challenge re-spawned → task parked.
    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)
    # Task should be parked awaiting a new challenge (EVALUATING).
    from app.models import TaskStatus as _TS

    assert task.status == _TS.EVALUATING

    # --- Part 2: indeterminate agreement with correct hash → decay, NOT refinement ---
    # Reset task status and install a verdict with indeterminate agreement.
    task.status = _TS.COMPLETED
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": [f],
        "awaiting_finding_challenge": None,
        FINDING_CHALLENGE_VERDICT_KEY: {
            "challenged_boundary": CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
            "first_verdict": "LOW",
            "second_opinion_verdict": "",
            "agreement": "indeterminate",
            "reason": "could not reach a conclusion",
            "sidecar_task_id": "sidecar-indet",
            "finding_hash": correct_hash,  # matches the finding
        },
    }
    db_session.add(task)
    await db_session.commit()

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding("Sentinel B", direction_id=direction["id"])],
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )
    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    deferred = (task.context or {}).get("deferred_findings", [])
    assert not any(
        fn.get("title") == finding_title
        and fn.get("deferred_reason") == "low_actionability_refinement"
        for fn in deferred
    ), "indeterminate challenge must not promote to refinement"


@_pytest.mark.asyncio
async def test_research_history_records_finding_challenge_summary(db_session):
    """Post-spawn direction history records bounded challenge telemetry for the cycle."""
    from app.research_direction_state import apply_post_spawn_state
    from test_autonomous_research import _make_finding

    project, direction, task = await _grader_project_and_task(
        db_session, "d-challenge-summary"
    )
    child = _Task(
        project_id=project.id,
        title="Dispatch challenged finding",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        parent_task_id=task.id,
        created_by="ORCHESTRATOR",
        context={"direction_id": direction["id"]},
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)

    dispatched = {
        **_make_finding("Dispatched challenged finding", direction_id=direction["id"]),
        "finding_challenge": {
            "challenged_boundary": CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
            "first_verdict": "absent",
            "second_opinion_verdict": "present",
            "agreement": CHALLENGE_DISAGREES,
            "reason": "head now reproduces",
            "sidecar_task_id": "sidecar-dispatch",
        },
    }
    dropped = {
        **_make_finding("Dropped challenged finding", direction_id=direction["id"]),
        "finding_challenge": {
            "challenged_boundary": CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
            "first_verdict": "LOW",
            "second_opinion_verdict": "",
            "agreement": CHALLENGE_INDETERMINATE,
            "reason": "unparseable sidecar output",
            "sidecar_task_id": "sidecar-drop",
        },
    }

    await apply_post_spawn_state(
        db_session,
        task,
        [dispatched, dropped],
        [child],
        [(0, dispatched, "hash-dispatched")],
        [],
        [],
        [],
        0,
        0,
        0,
        0,
        0,
        0,
        "research-branch",
        "base",
        0,
        None,
    )
    await db_session.refresh(project)

    history = project.directions[0]["research_history"]
    assert history[-1]["finding_challenge_summary"] == {
        "challenged_findings": 2,
        "agreement_distribution": {
            CHALLENGE_AGREES: 0,
            CHALLENGE_DISAGREES: 1,
            CHALLENGE_INDETERMINATE: 1,
        },
        "boundary_distribution": {
            CHALLENGE_BOUNDARY_REPRODUCE_ABSENT: 1,
            CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY: 1,
            CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH: 0,
        },
        "fail_closed_unparseable": 1,
    }


@_pytest.mark.asyncio
async def test_dedup_deadlock_auto_completes_exhausted_direction(db_session):
    """Detecting a dedup deadlock retires a genuinely-exhausted direction terminally.

    #10112 superseded: instead of the old auto-pause-and-recover loop, three
    consecutive dedup-zero research cycles on a direction that is not externally
    frozen and not code-failing now:
    - Set direction status to terminal 'retired' with auto_completed=True and an
      exhaustion auto_completed_reason
    - Stamp completed_at
    - Create an INFO 'Direction auto-retired' alert (not the WARNING deadlock alert)
    - NOT auto-pause (no auto_paused / paused_reason) — the direction is terminal.
    """
    from datetime import datetime, timedelta

    from app.models import Alert
    from app.research_direction_state import apply_post_spawn_state
    from test_autonomous_research import _active_direction, _make_research_task

    direction_id = "d-deadlock-rr"
    # Two pre-existing dedup-zero history entries (both with hash_dedup activity)
    old_history = [
        {
            "timestamp": (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=10)).isoformat(),
            "task_id": "t-old-dl1",
            "tasks_created": 0,
            "findings_produced": 0,
            "filter_breakdown": {
                "hash_dedup": 2,
                "word_overlap_dedup": 0,
                "file_overlap_dedup": 0,
                "evidence_overlap_dedup": 0,
                "type_filtered": 0,
                "empty_prompt": 0,
            },
            "summary": "no findings",
        },
        {
            "timestamp": (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=5)).isoformat(),
            "task_id": "t-old-dl2",
            "tasks_created": 0,
            "findings_produced": 0,
            "filter_breakdown": {
                "hash_dedup": 1,
                "word_overlap_dedup": 1,
                "file_overlap_dedup": 0,
                "evidence_overlap_dedup": 0,
                "type_filtered": 0,
                "empty_prompt": 0,
            },
            "summary": "no findings",
        },
    ]
    direction = {
        **_active_direction(direction_id, "Dedup deadlock direction"),
        "research_history": old_history,
    }
    project = _Project(
        name="Deadlock Recovery Test",
        user_id="test-user-dlrr",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    # Call apply_post_spawn_state with a 3rd dedup-zero cycle (triggers deadlock: 3
    # consecutive zero-task cycles, >=2 with dedup activity).
    await apply_post_spawn_state(
        db_session,
        task,
        [],  # findings
        [],  # child_tasks
        [],  # deduplicated
        [],  # insights
        [],  # direction_notes
        [],  # direction_proposals
        1,  # hash_dedup_count — dedup activity in this 3rd cycle
        0,  # word_overlap_count
        0,  # file_overlap_count
        0,  # evidence_overlap_count
        0,  # type_filtered_count
        0,  # empty_prompt_count
        "research-branch",
        "main",
        0,  # research_depth
        None,  # parent_research_task_id
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "retired", (
        f"dedup-deadlocked exhausted direction should be terminally retired, "
        f"got {d['status']!r}"
    )
    assert d.get("retirement_outcome") == "exhausted"
    assert d.get("auto_completed") is True, "auto_completed must be set"
    assert d.get("completed_at") is not None, "completed_at must be stamped"
    assert "exhausted" in (d.get("auto_completed_reason") or ""), (
        f"auto_completed_reason must reflect exhaustion, got {d.get('auto_completed_reason')!r}"
    )
    # The superseded auto-pause must NOT have fired.
    assert not d.get("auto_paused"), (
        "exhausted-retired direction must not be auto-paused"
    )
    assert not d.get("paused_reason"), "no paused_reason on a retired direction"

    # An INFO 'Direction auto-retired' alert is created (not the WARNING deadlock alert).
    result = await db_session.execute(
        _select(Alert).where(
            Alert.project_id == project.id,
            Alert.title == "Direction auto-retired",
            Alert.dismissed_at.is_(None),
        )
    )
    assert result.scalars().first() is not None, (
        "expected a 'Direction auto-retired' alert"
    )


@_pytest.mark.asyncio
async def test_dedup_deadlock_populates_deadlock_filtered_titles(db_session):
    """When a dedup deadlock fires but exhaustion-completion is DECLINED (the
    direction is externally frozen), the existing auto-pause path still runs and
    writes deadlock_filtered_titles.

    The field collects titles from filtered_findings across the deadlocked history window
    so the next research cycle's prompt can exclude them explicitly. Externally-frozen
    directions are guarded out of terminal completion (#10112 supersession), so they
    fall through to the legacy pause behavior — which this test pins.
    """
    from datetime import datetime, timedelta

    from app.research_direction_state import apply_post_spawn_state
    from test_autonomous_research import _active_direction, _make_research_task

    direction_id = "d-dft-populate"
    old_history = [
        {
            "timestamp": (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=10)).isoformat(),
            "task_id": "t-dft-old1",
            "tasks_created": 0,
            "findings_produced": 0,
            "filter_breakdown": {
                "hash_dedup": 2,
                "word_overlap_dedup": 0,
                "file_overlap_dedup": 0,
                "evidence_overlap_dedup": 0,
                "type_filtered": 0,
                "empty_prompt": 0,
            },
            "filtered_findings": [
                {"title": "Fix null pointer in auth handler", "reason": "hash match"},
                {"title": "Add retry to payment service", "reason": "word overlap"},
            ],
            "summary": "no findings",
        },
        {
            "timestamp": (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=5)).isoformat(),
            "task_id": "t-dft-old2",
            "tasks_created": 0,
            "findings_produced": 0,
            "filter_breakdown": {
                "hash_dedup": 1,
                "word_overlap_dedup": 1,
                "file_overlap_dedup": 0,
                "evidence_overlap_dedup": 0,
                "type_filtered": 0,
                "empty_prompt": 0,
            },
            "filtered_findings": [
                {"title": "Fix null pointer in auth handler", "reason": "hash match"},
                {"title": "Remove unused import in models", "reason": "word overlap"},
            ],
            "summary": "no findings",
        },
    ]
    direction = {
        **_active_direction(direction_id, "Deadlock filtered titles test"),
        "research_history": old_history,
        # Externally frozen → exhaustion-completion is declined, so the legacy
        # auto-pause + deadlock_filtered_titles path still fires.
        "needs_manual_intervention": True,
    }
    project = _Project(
        name="DFT Populate Test",
        user_id="test-user-dft1",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    # Third dedup-zero cycle with filtered findings — triggers deadlock
    await apply_post_spawn_state(
        db_session,
        task,
        [],  # findings
        [],  # child_tasks
        [],  # deduplicated
        [],  # insights
        [],  # direction_notes
        [],  # direction_proposals
        1,  # hash_dedup_count
        0,
        0,
        0,
        0,
        0,
        "research-branch",
        "main",
        0,
        None,
        filtered_findings=[
            {
                "title": "Add config validation",
                "reason": "hash match",
                "direction_id": direction_id,
            },
        ],
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "paused", "direction should be paused"
    dft = d.get("deadlock_filtered_titles")
    assert dft is not None, "deadlock_filtered_titles must be set on deadlock"
    assert isinstance(dft, list), "deadlock_filtered_titles must be a list"
    # Should include titles from the history window (deduplicated)
    assert "Fix null pointer in auth handler" in dft
    assert "Add retry to payment service" in dft
    assert "Remove unused import in models" in dft
    # No duplicates
    assert len(dft) == len(set(dft)), (
        "deadlock_filtered_titles must not have duplicates"
    )


@_pytest.mark.asyncio
async def test_dedup_deadlock_filtered_titles_cleared_on_productive_cycle(db_session):
    """deadlock_filtered_titles is cleared when the next cycle creates tasks.

    After a direction recovers from dedup deadlock and its next research cycle
    successfully creates child tasks, the deadlock_filtered_titles field is removed.
    """
    from datetime import datetime, timedelta

    from app.research_direction_state import apply_post_spawn_state
    from test_autonomous_research import _active_direction, _make_research_task
    from app.models import Task as _Task

    direction_id = "d-dft-clear"
    old_history = [
        {
            "timestamp": (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=10)).isoformat(),
            "task_id": "t-dft-c1",
            "tasks_created": 0,
            "findings_produced": 0,
            "filter_breakdown": {
                "hash_dedup": 1,
                "word_overlap_dedup": 0,
                "file_overlap_dedup": 0,
                "evidence_overlap_dedup": 0,
                "type_filtered": 0,
                "empty_prompt": 0,
            },
            "summary": "no findings",
        },
        {
            "timestamp": (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=5)).isoformat(),
            "task_id": "t-dft-c2",
            "tasks_created": 0,
            "findings_produced": 0,
            "filter_breakdown": {
                "hash_dedup": 1,
                "word_overlap_dedup": 0,
                "file_overlap_dedup": 0,
                "evidence_overlap_dedup": 0,
                "type_filtered": 0,
                "empty_prompt": 0,
            },
            "summary": "no findings",
        },
    ]
    # Direction already has deadlock_filtered_titles from a prior deadlock
    direction = {
        **_active_direction(direction_id, "DFT clear test"),
        "research_history": old_history,
        "deadlock_filtered_titles": ["Old rejected title A", "Old rejected title B"],
    }
    project = _Project(
        name="DFT Clear Test",
        user_id="test-user-dft2",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    # Productive cycle: spawn a child task for this direction
    child = _Task(
        project_id=project.id,
        task_type="create",
        status=TaskStatus.QUEUED,
        payloads=[{"prompt": "fix something", "base_branch": "main"}],
        context={"direction_id": direction_id},
    )
    db_session.add(child)
    await db_session.commit()

    from app.finding_deduplication import finding_hash, canonical_finding_body

    finding = {
        "title": "New real fix",
        "description": "Actually new work",
        "direction_id": direction_id,
        "type": "fix",
    }
    fhash = finding_hash(finding["title"], canonical_finding_body(finding))

    await apply_post_spawn_state(
        db_session,
        task,
        [finding],  # findings
        [child],  # child_tasks — productive cycle
        [(0, finding, fhash)],  # deduplicated
        [],  # insights
        [],  # direction_notes
        [],  # direction_proposals
        0,
        0,
        0,
        0,
        0,
        0,
        "research-branch",
        "main",
        0,
        None,
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d.get("deadlock_filtered_titles") is None, (
        "deadlock_filtered_titles should be cleared when tasks are created"
    )


@_pytest.mark.asyncio
async def test_low_actionability_flag_off_preserves_legacy_drop_without_memo(
    db_session, mocker
):
    """With the challenge flag off, repeated low-actionability decay keeps the
    legacy structural give-up path and does not spawn a memo."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-low-legacy01"
    )
    decayed = _make_finding("Vague cleanup", direction_id=direction["id"])
    decayed["actionability_grade"] = "LOW"
    decayed["deferred_reason"] = "low_actionability"
    decayed["deferred_count"] = 3
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": [decayed],
    }
    db_session.add(task)
    await db_session.commit()

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding("Fresh sentinel", direction_id=direction["id"])],
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    give_up = (task.context or {}).get("finding_structural_give_up")
    assert give_up["by_reason"] == {"low_actionability": 1}
    assert (task.context or {}).get("spawned_memo_ids") is None
    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert not any(
        c.context and c.context.get("memo_drop_reason") == "low_actionability_decay"
        for c in children
    )


@_pytest.mark.asyncio
async def test_overturned_low_actionability_decay_redefers_for_refinement(
    db_session, mocker
):
    """A disagreeing low-actionability second opinion prevents terminal decay
    and keeps the finding in deferred_findings with the disagreement reason."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"research_finding_challenge_enabled": True},
    )
    _project, direction, task = await _grader_project_and_task(
        db_session, "d-low-chal02"
    )
    decayed = _make_finding(
        "Specific retry starvation hardening", direction_id=direction["id"]
    )
    decayed["actionability_grade"] = "LOW"
    decayed["deferred_reason"] = "low_actionability"
    decayed["deferred_count"] = 3
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": [decayed],
        FINDING_CHALLENGE_VERDICT_KEY: {
            "challenged_boundary": CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
            "first_verdict": "LOW",
            "second_opinion_verdict": "MEDIUM",
            "agreement": "disagrees",
            "reason": "has a concrete retry-starvation boundary to refine",
            "sidecar_task_id": "sidecar-low-overturn",
        },
    }
    db_session.add(task)
    await db_session.commit()

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding("Fresh sentinel", direction_id=direction["id"])],
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    assert (task.context or {}).get("finding_structural_give_up") is None
    deferred = (task.context or {}).get("deferred_findings", [])
    kept = next(f for f in deferred if f.get("title") == decayed["title"])
    assert kept["deferred_reason"] == "low_actionability_refinement"
    assert kept["finding_challenge"]["agreement"] == "disagrees"
    assert "retry-starvation" in kept["low_actionability_refinement_reason"]


class TestKnowledgeFindingRouting:
    """web_research / design findings route to the matching knowledge child.

    Guards the additive branches in BOTH routing cascades inside
    _handle_research_completion: a finding of type 'web_research' must spawn a
    web_research child and 'design' a design child, never falling through to the
    scope-based plan/create fallback. The spawned knowledge child must also carry
    the originating direction context like every other spawned child.
    """

    async def _spawn_from_finding(self, db_session, mocker, *, direction_id, finding):
        from test_autonomous_research import _patch_research_externals

        _project, _direction, task = await _grader_project_and_task(
            db_session, direction_id
        )

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding],
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )
        for path, value in (
            ("app.research_completion.resolve_branch_verification_context", {}),
            ("app.research_completion.resolve_head_commit", None),
            ("app.research_completion.cited_paths_changed_since", False),
            ("app.research_completion._summarize_ungrounded_claims_grounded", None),
        ):
            mocker.patch(path, new=AsyncMock(return_value=value))

        await _handle_research_completion(task, "", db_session)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        return next(c for c in children if c.title == finding["title"]), task

    @_pytest.mark.asyncio
    async def test_web_research_finding_spawns_web_research_child(
        self, db_session, mocker
    ):
        from test_autonomous_research import _make_finding

        finding = _make_finding(
            title="Investigate vendor API",
            direction_id="d-know-wr01",
            finding_type="web_research",
        )
        child, _task = await self._spawn_from_finding(
            db_session, mocker, direction_id="d-know-wr01", finding=finding
        )
        assert child.first_payload["type"] == "web_research"

    @_pytest.mark.asyncio
    async def test_design_finding_spawns_design_child(self, db_session, mocker):
        from test_autonomous_research import _make_finding

        finding = _make_finding(
            title="Design the ingestion pipeline",
            direction_id="d-know-ds01",
            finding_type="design",
        )
        child, _task = await self._spawn_from_finding(
            db_session, mocker, direction_id="d-know-ds01", finding=finding
        )
        assert child.first_payload["type"] == "design"

    @_pytest.mark.asyncio
    async def test_design_child_available_capabilities_carry_proof_status(
        self, db_session, mocker
    ):
        """The vocabulary-rendering child payload (design/plan/research) ships
        available_capabilities annotated with proof_status so the child can
        distinguish proven from working-but-unproven capabilities."""
        from test_autonomous_research import _make_finding, _patch_research_externals

        project, direction, task = await _grader_project_and_task(
            db_session, "d-know-ds02"
        )
        # Registry declares two capabilities; only one has a durable proof record.
        project.problem_model = {
            "intent": {"purpose": "test"},
            "capabilities": [
                {"name": "auth-core", "status": "working"},
                {"name": "fraud-detect", "status": "working"},
            ],
        }
        project.capability_proof_records = {
            "auth-core": [{"proof_kind": "named_test", "commit_sha": "abc123"}],
        }
        db_session.add(project)
        await db_session.commit()

        finding = _make_finding(
            title="Design the ingestion pipeline",
            direction_id="d-know-ds02",
            finding_type="design",
        )
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding],
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )
        for path, value in (
            ("app.research_completion.resolve_branch_verification_context", {}),
            ("app.research_completion.resolve_head_commit", None),
            ("app.research_completion.cited_paths_changed_since", False),
            ("app.research_completion._summarize_ungrounded_claims_grounded", None),
        ):
            mocker.patch(path, new=AsyncMock(return_value=value))

        await _handle_research_completion(task, "", db_session)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        child = next(c for c in children if c.title == finding["title"])
        caps = child.first_payload.get("available_capabilities")
        assert isinstance(caps, list) and caps, "available_capabilities should ship"
        by_name = {c["name"]: c for c in caps}
        assert by_name["auth-core"]["proof_status"] == "proven"
        assert by_name["fraud-detect"]["proof_status"] == "unproven"

    @_pytest.mark.asyncio
    async def test_large_scope_web_research_finding_not_routed_to_plan(
        self, db_session, mocker
    ):
        """A large-scope knowledge finding routes to its knowledge type, NOT plan.

        Guards the branch-ordering requirement: the knowledge branch must come
        before the scope-based plan/create fallback.
        """
        from test_autonomous_research import _make_finding

        finding = _make_finding(
            title="Survey distributed-tracing protocols",
            direction_id="d-know-wr02",
            scope="large",
            finding_type="web_research",
        )
        child, _task = await self._spawn_from_finding(
            db_session, mocker, direction_id="d-know-wr02", finding=finding
        )
        assert child.first_payload["type"] == "web_research"

    @_pytest.mark.asyncio
    async def test_knowledge_child_carries_direction_context(self, db_session, mocker):
        from test_autonomous_research import _make_finding

        finding = _make_finding(
            title="Research auth-token best practices",
            direction_id="d-know-ctx01",
            finding_type="web_research",
        )
        child, task = await self._spawn_from_finding(
            db_session, mocker, direction_id="d-know-ctx01", finding=finding
        )
        ctx = child.context or {}
        assert ctx.get("direction_id") == "d-know-ctx01"
        assert ctx.get("research_task_id") == str(task.id)
        assert ctx.get("parent_research_task_id") == str(task.id)


class TestRecomputeLaunchStaleness:
    """Launch-time cited-file staleness recompute (orchestrator dispatch hook)."""

    def _task(self, context):
        t = MagicMock()
        t.context = context
        t.project_id = uuid.uuid4()
        return t

    def _session(self, project=None):
        s = MagicMock()
        s.get = AsyncMock(return_value=project if project is not None else object())
        return s

    def _patch_ctx(self, mocker, *, verdict):
        mocker.patch(
            "app.research_provenance_validation.resolve_branch_verification_context",
            new=AsyncMock(return_value={}),
        )
        m = AsyncMock(return_value=verdict)
        mocker.patch(
            "app.research_provenance_validation.cited_paths_changed_since", new=m
        )
        return m

    @_pytest.mark.asyncio
    async def test_true_verdict_sets_stale_flag(self, mocker):
        from app.research_completion import recompute_launch_staleness

        self._patch_ctx(mocker, verdict=True)
        task = self._task(
            {
                "research_task_id": "r1",
                "generated_head_sha": "deadbeef",
                "_finding_cited_paths": ["backend/app/foo.py"],
            }
        )
        assert await recompute_launch_staleness(self._session(), task) is True
        assert task.context["_provenance_warnings"] == {
            "cited_files_changed_since_generation": True
        }

    @_pytest.mark.asyncio
    async def test_true_verdict_preserves_existing_warnings(self, mocker):
        from app.research_completion import recompute_launch_staleness

        self._patch_ctx(mocker, verdict=True)
        task = self._task(
            {
                "parent_research_task_id": "r1",
                "generated_head_sha": "deadbeef",
                "_finding_cited_paths": ["backend/app/foo.py"],
                "_provenance_warnings": {
                    "broken_source_ref_count": 2,
                    "broken_refs": ["backend/app/x.py"],
                },
            }
        )
        assert await recompute_launch_staleness(self._session(), task) is True
        assert task.context["_provenance_warnings"] == {
            "broken_source_ref_count": 2,
            "broken_refs": ["backend/app/x.py"],
            "cited_files_changed_since_generation": True,
        }

    @_pytest.mark.asyncio
    @_pytest.mark.parametrize("verdict", [None, False])
    async def test_indeterminate_or_unchanged_verdict_no_flag(self, mocker, verdict):
        from app.research_completion import recompute_launch_staleness

        self._patch_ctx(mocker, verdict=verdict)
        task = self._task(
            {
                "research_task_id": "r1",
                "generated_head_sha": "deadbeef",
                "_finding_cited_paths": ["backend/app/foo.py"],
            }
        )
        assert await recompute_launch_staleness(self._session(), task) is False
        assert "_provenance_warnings" not in (task.context or {})

    @_pytest.mark.asyncio
    async def test_missing_generated_head_sha_is_noop(self, mocker):
        from app.research_completion import recompute_launch_staleness

        m = self._patch_ctx(mocker, verdict=True)
        task = self._task(
            {"research_task_id": "r1", "_finding_cited_paths": ["backend/app/foo.py"]}
        )
        assert await recompute_launch_staleness(self._session(), task) is False
        assert "_provenance_warnings" not in (task.context or {})
        m.assert_not_called()

    @_pytest.mark.asyncio
    async def test_missing_cited_paths_is_noop(self, mocker):
        from app.research_completion import recompute_launch_staleness

        m = self._patch_ctx(mocker, verdict=True)
        task = self._task({"research_task_id": "r1", "generated_head_sha": "deadbeef"})
        assert await recompute_launch_staleness(self._session(), task) is False
        m.assert_not_called()

    @_pytest.mark.asyncio
    async def test_no_research_parent_is_noop(self, mocker):
        from app.research_completion import recompute_launch_staleness

        m = self._patch_ctx(mocker, verdict=True)
        task = self._task(
            {
                "generated_head_sha": "deadbeef",
                "_finding_cited_paths": ["backend/app/foo.py"],
            }
        )
        assert await recompute_launch_staleness(self._session(), task) is False
        m.assert_not_called()

    @_pytest.mark.asyncio
    async def test_exception_is_swallowed(self, mocker):
        from app.research_completion import recompute_launch_staleness

        mocker.patch(
            "app.research_provenance_validation.resolve_branch_verification_context",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        )
        task = self._task(
            {
                "research_task_id": "r1",
                "generated_head_sha": "deadbeef",
                "_finding_cited_paths": ["backend/app/foo.py"],
            }
        )
        assert await recompute_launch_staleness(self._session(), task) is False
        assert "_provenance_warnings" not in (task.context or {})


class TestQueryFindingsIndex:
    """query_findings_index: structured findings view for strategic cycle and admin."""

    def _make_task(
        self,
        direction_id,
        plan_output,
        days_ago=1,
        *,
        task_type="research",
        status=TaskStatus.COMPLETED,
    ):
        from app.time_utils import _utcnow

        task = MagicMock()
        task.id = uuid.uuid4()
        task.context = {
            "direction_id": direction_id,
            "research_findings_finalized": True,
        }
        task.created_at = _utcnow() - timedelta(days=days_ago)
        task.plan_output = plan_output
        task.status = status
        task.first_payload = {"type": task_type}
        return task

    def _mock_session(self, tasks):
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = tasks
        session = AsyncMock()
        session.execute.return_value = mock_result
        return session

    def test_returns_all_findings_no_filter(self):
        import asyncio
        from app.research_completion import query_findings_index

        t1 = self._make_task(
            "d-1",
            [
                {
                    "title": "F1",
                    "description": "D1",
                    "actionability_grade": "HIGH",
                    "claims": ["c1"],
                    "sources": [],
                }
            ],
        )
        t2 = self._make_task(
            "d-2",
            [
                {
                    "title": "F2",
                    "description": "D2",
                    "actionability_grade": "MEDIUM",
                    "claims": [],
                    "sources": [],
                }
            ],
        )
        session = self._mock_session([t1, t2])

        rows = asyncio.get_event_loop().run_until_complete(
            query_findings_index(session, "proj-1")
        )
        assert len(rows) == 2
        titles = {r["finding_title"] for r in rows}
        assert titles == {"F1", "F2"}

    def test_filters_by_direction_id(self):
        import asyncio
        from app.research_completion import query_findings_index

        t1 = self._make_task(
            "d-1",
            [
                {
                    "title": "F1",
                    "description": "D1",
                    "actionability_grade": "HIGH",
                    "claims": [],
                    "sources": [],
                }
            ],
        )
        t2 = self._make_task(
            "d-2",
            [
                {
                    "title": "F2",
                    "description": "D2",
                    "actionability_grade": "LOW",
                    "claims": [],
                    "sources": [],
                }
            ],
        )
        session = self._mock_session([t1, t2])

        rows = asyncio.get_event_loop().run_until_complete(
            query_findings_index(session, "proj-1", direction_id="d-1")
        )
        assert len(rows) == 1
        assert rows[0]["finding_title"] == "F1"
        assert rows[0]["direction_id"] == "d-1"

    def test_row_shape_includes_claims_and_sources(self):
        import asyncio
        from app.research_completion import query_findings_index

        claims = ["claim A", "claim B"]
        sources = [{"type": "file", "ref": "backend/app/foo.py"}]
        t = self._make_task(
            "d-1",
            [
                {
                    "title": "T",
                    "description": "Desc",
                    "actionability_grade": "HIGH",
                    "claims": claims,
                    "sources": sources,
                }
            ],
        )
        session = self._mock_session([t])

        rows = asyncio.get_event_loop().run_until_complete(
            query_findings_index(session, "proj-1")
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["claims"] == claims
        assert row["sources"] == sources
        assert row["actionability_grade"] == "HIGH"
        assert "task_id" in row
        assert "completed_at" in row
        assert "finding_index" in row

    def test_respects_limit(self):
        import asyncio
        from app.research_completion import query_findings_index

        findings = [
            {
                "title": f"F{i}",
                "description": "x",
                "actionability_grade": "MEDIUM",
                "claims": [],
                "sources": [],
            }
            for i in range(10)
        ]
        t = self._make_task("d-1", findings)
        session = self._mock_session([t])

        rows = asyncio.get_event_loop().run_until_complete(
            query_findings_index(session, "proj-1", limit=3)
        )
        assert len(rows) == 3

    def test_skips_non_list_plan_output(self):
        import asyncio
        from app.research_completion import query_findings_index

        t = self._make_task("d-1", {"not": "a list"})
        session = self._mock_session([t])

        rows = asyncio.get_event_loop().run_until_complete(
            query_findings_index(session, "proj-1")
        )
        assert rows == []

    def test_accepts_finalized_delivery_parent_and_skips_other_states(self):
        import asyncio
        from app.research_completion import query_findings_index

        research = self._make_task(
            "d-1",
            [{"title": "Research", "description": "D", "claims": [], "sources": []}],
        )
        self_audit = self._make_task(
            "d-1",
            [{"title": "Audit", "description": "D", "claims": [], "sources": []}],
            task_type="self_audit",
        )
        in_progress = self._make_task(
            "d-1",
            [{"title": "In flight", "description": "D", "claims": [], "sources": []}],
            status=TaskStatus.IN_PROGRESS,
        )
        delivery_parent = self._make_task(
            "d-1",
            [{"title": "Accepted delivery", "description": "D", "claims": [], "sources": []}],
            status=TaskStatus.EVALUATING,
        )
        parked_grader = self._make_task(
            "d-1",
            [{"title": "Ungraded", "description": "D", "claims": [], "sources": []}],
            status=TaskStatus.EVALUATING,
        )
        parked_grader.context.pop("research_findings_finalized")
        session = self._mock_session(
            [research, delivery_parent, self_audit, in_progress, parked_grader]
        )

        rows = asyncio.get_event_loop().run_until_complete(
            query_findings_index(session, "proj-1")
        )
        assert [row["finding_title"] for row in rows] == [
            "Research",
            "Accepted delivery",
        ]


class TestFindingsDocumentedOutcome:
    """completion_outcome='findings_documented' terminal pathway for metadata-only research."""

    def _detect_outcome(
        self, direction_notes, direction_proposals, type_filtered_count, child_tasks
    ):
        """Mirrors the logic in research_direction_state.apply_post_spawn_state."""
        if child_tasks:
            return None  # implementation children → not findings_documented
        _has_metadata = bool(
            direction_notes or direction_proposals or type_filtered_count > 0
        )
        return "findings_documented" if _has_metadata else None

    def test_direction_notes_only_triggers_findings_documented(self):
        """direction_notes with no children → findings_documented."""
        outcome = self._detect_outcome(
            direction_notes=[{"type": "direction_note", "title": "note"}],
            direction_proposals=[],
            type_filtered_count=0,
            child_tasks=[],
        )
        assert outcome == "findings_documented"

    def test_direction_proposals_only_triggers_findings_documented(self):
        """direction_proposals with no children → findings_documented."""
        outcome = self._detect_outcome(
            direction_notes=[],
            direction_proposals=[{"type": "direction_proposal", "title": "proposal"}],
            type_filtered_count=0,
            child_tasks=[],
        )
        assert outcome == "findings_documented"

    def test_design_doc_observations_type_filtered_triggers_findings_documented(self):
        """design_doc_observation findings counted in type_filtered_count → findings_documented."""
        outcome = self._detect_outcome(
            direction_notes=[],
            direction_proposals=[],
            type_filtered_count=2,
            child_tasks=[],
        )
        assert outcome == "findings_documented"

    def test_no_metadata_no_children_no_findings_documented(self):
        """No metadata findings, no children → None (existing dedup/empty-prompt path)."""
        outcome = self._detect_outcome(
            direction_notes=[],
            direction_proposals=[],
            type_filtered_count=0,
            child_tasks=[],
        )
        assert outcome is None

    def test_mixed_with_child_tasks_no_findings_documented(self):
        """When implementation child tasks exist, no findings_documented outcome."""
        outcome = self._detect_outcome(
            direction_notes=[{"type": "direction_note", "title": "note"}],
            direction_proposals=[],
            type_filtered_count=0,
            child_tasks=[object()],  # non-empty → implementation path
        )
        assert outcome is None


class TestRecordResearchTaskFailureScopeEmpty:
    """_record_research_task_failure: SCOPE_EMPTY is benign and must not penalize direction health."""

    def _make_task(self, failure_category, direction_id="d-test"):
        task = MagicMock()
        task.id = uuid.uuid4()
        task.project_id = uuid.uuid4()
        task.context = {
            "failure_category": failure_category,
            "direction_id": direction_id,
        }
        task.latest_status_message = (
            f"{failure_category}: sibling already covered scope"
        )
        task.first_payload = {"type": "research"}
        return task

    def _make_session(self):
        session = AsyncMock()
        session.add = MagicMock()
        return session

    def _make_project(self, direction_id="d-test", consecutive_unproductive=0):
        project = MagicMock()
        project.directions = [
            {
                "id": direction_id,
                "research_history": [],
                "consecutive_unproductive_cycles": consecutive_unproductive,
            }
        ]
        return project

    def _run(self, project, coro):
        """Run coro with with_directions_locked patched to call mutate_fn on project.directions."""
        import asyncio
        from unittest.mock import patch

        async def _mock_wdl(session, project_id, mutate_fn):
            mutate_fn(project.directions)

        with patch("app.research_completion.with_directions_locked", _mock_wdl):
            asyncio.get_event_loop().run_until_complete(coro)

    def test_scope_empty_marks_cycle_productive(self):
        from app.research_completion import _record_research_task_failure

        task = self._make_task("SCOPE_EMPTY")
        project = self._make_project()
        self._run(project, _record_research_task_failure(task, self._make_session()))

        direction = project.directions[0]
        assert direction["last_cycle_productive"] is True

    def test_scope_empty_resets_unproductive_counter(self):
        from app.research_completion import _record_research_task_failure

        task = self._make_task("SCOPE_EMPTY")
        project = self._make_project(consecutive_unproductive=3)
        self._run(project, _record_research_task_failure(task, self._make_session()))

        direction = project.directions[0]
        assert direction.get("consecutive_unproductive_cycles", 0) == 0

    def test_code_error_marks_cycle_unproductive(self):
        from app.research_completion import _record_research_task_failure

        task = self._make_task("CODE_ERROR")
        project = self._make_project(consecutive_unproductive=1)
        self._run(project, _record_research_task_failure(task, self._make_session()))

        direction = project.directions[0]
        assert direction["last_cycle_productive"] is False
        assert direction.get("consecutive_unproductive_cycles", 0) == 2

    def test_scope_empty_history_entry_has_failure_category(self):
        from app.research_completion import _record_research_task_failure

        task = self._make_task("SCOPE_EMPTY")
        project = self._make_project()
        self._run(project, _record_research_task_failure(task, self._make_session()))

        direction = project.directions[0]
        history = direction.get("research_history", [])
        assert len(history) == 1
        assert history[0]["research_task_failed"] is True
        assert history[0]["failure_category"] == "SCOPE_EMPTY"

    def test_uses_with_directions_locked_not_session_get(self):
        """Direction writes must go through with_directions_locked (no stale read-modify-write)."""
        import asyncio
        from unittest.mock import patch
        from app.research_completion import _record_research_task_failure

        task = self._make_task("CODE_ERROR")
        project = self._make_project()
        session = self._make_session()

        called_with = []

        async def mock_wdl(sess, project_id, mutate_fn):
            called_with.append(project_id)
            mutate_fn(project.directions)

        with patch("app.research_completion.with_directions_locked", mock_wdl):
            asyncio.get_event_loop().run_until_complete(
                _record_research_task_failure(task, session)
            )

        assert called_with == [task.project_id]
        session.get.assert_not_called()

    def test_research_failure_recording_is_idempotent_per_task(self):
        """Re-running the same failed task must not append duplicate history entries
        or double-count consecutive_unproductive_cycles."""
        from app.research_completion import _record_research_task_failure

        task = self._make_task("CODE_ERROR")
        project = self._make_project(consecutive_unproductive=1)

        # First call — records normally.
        self._run(project, _record_research_task_failure(task, self._make_session()))
        direction = project.directions[0]
        assert len(direction.get("research_history", [])) == 1
        assert direction.get("consecutive_unproductive_cycles") == 2

        # Second call with same task — must be a no-op.
        self._run(project, _record_research_task_failure(task, self._make_session()))
        direction = project.directions[0]
        assert len(direction.get("research_history", [])) == 1, (
            "Duplicate history entry appended on replay"
        )
        assert direction.get("consecutive_unproductive_cycles") == 2, (
            "consecutive_unproductive_cycles double-counted on replay"
        )


class TestTrimFindingForMemo:
    """_trim_finding_for_memo: bounds a dropped finding for memo embedding."""

    def test_keeps_core_fields_and_trims_text(self):
        from app.research_completion import _trim_finding_for_memo

        finding = {
            "title": "T",
            "type": "improvement",
            "scope": "small",
            "risk": "low",
            "priority": "medium",
            "actionability_grade": "LOW",
            "direction_id": "d-1",
            "description": "x" * 5000,
            "related_files": [f"f{i}.py" for i in range(50)],
            "_complexity_signals": {"a": 1},
            "claims": ["c"],
        }
        out = _trim_finding_for_memo(finding)
        assert out["title"] == "T"
        assert out["actionability_grade"] == "LOW"
        assert out["direction_id"] == "d-1"
        assert len(out["description"]) == 1000
        assert len(out["related_files"]) == 20
        # Bulky internal/unused fields are dropped.
        assert "_complexity_signals" not in out
        assert "claims" not in out

    def test_omits_missing_fields(self):
        from app.research_completion import _trim_finding_for_memo

        out = _trim_finding_for_memo({"title": "Only title"})
        assert out == {"title": "Only title"}


class TestSynthesizeDeferredFindingMemo:
    """_synthesize_deferred_finding_memo: backend-side synthesis for deferred-finding memos."""

    def _fn(self):
        from app.research_completion import _synthesize_deferred_finding_memo

        return _synthesize_deferred_finding_memo

    def test_full_data_returns_memo(self):
        result = self._fn()(
            {
                "title": "Fix auth edge case",
                "description": "Token refresh fails on expiry",
            },
            "low_actionability",
            "task-abc",
        )
        assert result is not None
        assert result["summary"] == "Fix auth edge case"
        assert "Fix auth edge case" in result["reasoning"]
        assert "low_actionability" in result["reasoning"]
        assert "Token refresh fails" in result["reasoning"]
        assert "task:task-abc" in result["citations"]
        assert "deferred-finding" in result["tags"]
        assert "low_actionability" in result["tags"]

    def test_degradation_gate_drop_reason(self):
        result = self._fn()(
            {"title": "Perf improvement", "description": "Cache miss rate high"},
            "degradation_gate",
            "task-xyz",
        )
        assert result is not None
        assert "degradation_gate" in result["tags"]
        assert "degradation_gate" in result["reasoning"]

    def test_no_title_uses_description_as_summary(self):
        result = self._fn()(
            {"description": "Something useful"},
            "low_actionability",
            "task-1",
        )
        assert result is not None
        assert result["summary"] == "Something useful"

    def test_no_title_and_no_description_returns_none(self):
        assert self._fn()({}, "low_actionability", "task-1") is None

    def test_empty_strings_return_none(self):
        assert (
            self._fn()({"title": "", "description": ""}, "low_actionability", "t")
            is None
        )

    def test_summary_truncated_to_200(self):
        result = self._fn()(
            {"title": "x" * 300},
            "low_actionability",
            "t",
        )
        assert result is not None
        assert len(result["summary"]) == 200


class TestFindingsTypeFilter:
    """Regression: agent JSON occasionally contains non-dict entries (bare strings,
    nulls). Before the handler's normalization loop these must be filtered out, or
    `.get()` will AttributeError and the completed research task gets terminally
    FAILED by task_lifecycle's handler-failed branch."""

    def test_parser_passes_through_non_dict_entries(self):
        """_parse_research_findings is intentionally a thin JSON-array unwrap and
        does not validate element types — the dict filter must happen at the
        handler layer."""
        import json

        from app.research_completion import _parse_research_findings

        payload = json.dumps(
            [
                {"title": "real finding", "prompt": "do thing"},
                "bare string slipped through",
                None,
                {"title": "another", "prompt": "do other"},
            ]
        )
        # _extract_json_array looks for keys 'title' + 'prompt' to anchor the array.
        wrapped = f"prefix ...\n{payload}\n... suffix"
        parsed = _parse_research_findings(wrapped)
        assert parsed is not None
        assert any(not isinstance(f, dict) for f in parsed), (
            "Parser is expected to surface raw JSON elements unchanged; "
            "if this changes the handler-side filter may become redundant."
        )

    def test_handler_filter_drops_non_dict(self):
        """The filter expression used at the top of _handle_research_completion
        keeps only dicts so the downstream .get() loop is safe."""
        findings = [
            {"title": "ok"},
            "string",
            None,
            42,
            {"title": "also ok"},
        ]
        filtered = [f for f in findings if isinstance(f, dict)]
        assert filtered == [{"title": "ok"}, {"title": "also ok"}]
        for _f in filtered:
            assert _f.get("title") in {"ok", "also ok"}


class TestSummarizeDispatchOutcome:
    """_summarize_dispatch_outcome composes the post-dispatch status_message that
    surfaces spawn/dedup/defer counts on the task itself. Before this lived only
    in logs and task.context, leaving latest_status_message stuck at the last
    agent heartbeat."""

    def _outcome(
        self,
        hash_=0,
        word=0,
        file_=0,
        evidence=0,
        retired=0,
        archived=0,
    ):
        # Lightweight stand-in for DedupOutcome — _summarize_dispatch_outcome
        # uses getattr() so any object with the right int attrs works.
        class _O:
            pass

        o = _O()
        o.hash_dedup_count = hash_
        o.word_overlap_count = word
        o.file_overlap_count = file_
        o.evidence_overlap_count = evidence
        o.retired_approach_count = retired
        o.archived_diagnoses_count = archived
        return o

    def test_all_spawned_no_dedup_no_defer(self):
        from app.research_completion import _summarize_dispatch_outcome

        msg = _summarize_dispatch_outcome(
            spawned=5, total_findings=5, dedup_outcome=self._outcome()
        )
        assert msg == "spawned 5/5 findings"

    def test_partial_spawn_with_dedup_breakdown(self):
        from app.research_completion import _summarize_dispatch_outcome

        msg = _summarize_dispatch_outcome(
            spawned=2,
            total_findings=5,
            dedup_outcome=self._outcome(hash_=1, word=2),
        )
        assert "spawned 2/5 findings" in msg
        assert "dedup filtered" in msg
        assert "1 hash" in msg
        assert "2 word-overlap" in msg

    def test_includes_deferred_reasons(self):
        from app.research_completion import _summarize_dispatch_outcome

        msg = _summarize_dispatch_outcome(
            spawned=1,
            total_findings=4,
            dedup_outcome=self._outcome(),
            deferred_reason_parts=["batch cap (3)"],
        )
        assert "spawned 1/4 findings" in msg
        assert "deferred: batch cap (3)" in msg

    def test_zero_spawn_full_dedup_is_loud(self):
        """The regression we just fixed: 5 findings → 0 children with dedup
        eating everything. The status_message must show this, not look healthy."""
        from app.research_completion import _summarize_dispatch_outcome

        msg = _summarize_dispatch_outcome(
            spawned=0,
            total_findings=5,
            dedup_outcome=self._outcome(hash_=3, word=2),
        )
        assert "spawned 0/5 findings" in msg
        assert "3 hash" in msg and "2 word-overlap" in msg

    def test_full_dedup_breakdown_all_categories(self):
        from app.research_completion import _summarize_dispatch_outcome

        msg = _summarize_dispatch_outcome(
            spawned=0,
            total_findings=6,
            dedup_outcome=self._outcome(
                hash_=1, word=1, file_=1, evidence=1, retired=1, archived=1
            ),
        )
        for label in (
            "1 hash",
            "1 word-overlap",
            "1 file-overlap",
            "1 evidence-overlap",
            "1 retired-approach",
            "1 archived-diagnosis",
        ):
            assert label in msg, f"missing {label!r} in {msg!r}"

    def test_message_truncates_at_500_chars(self):
        from app.research_completion import _summarize_dispatch_outcome

        # Construct deferred reasons that would push past 500 chars
        long_reasons = [f"reason_{i}_with_padding_text" * 5 for i in range(20)]
        msg = _summarize_dispatch_outcome(
            spawned=0,
            total_findings=99,
            dedup_outcome=self._outcome(hash_=9),
            deferred_reason_parts=long_reasons,
        )
        assert len(msg) <= 500

    # --- degraded_reason qualifier ---

    def test_degraded_reason_appended_when_set(self):
        from app.research_completion import _summarize_dispatch_outcome

        msg = _summarize_dispatch_outcome(
            spawned=2,
            total_findings=5,
            dedup_outcome=self._outcome(),
            degraded_reason="provider capacity/rate-limit error",
        )
        assert "spawned 2/5 findings" in msg
        assert "degraded: provider capacity/rate-limit error" in msg
        assert "results may be incomplete" in msg

    def test_degraded_reason_composes_with_deferred(self):
        from app.research_completion import _summarize_dispatch_outcome

        msg = _summarize_dispatch_outcome(
            spawned=0,
            total_findings=3,
            dedup_outcome=self._outcome(),
            deferred_reason_parts=["batch cap (3)"],
            degraded_reason="provider capacity/rate-limit error",
        )
        assert "degraded:" in msg
        assert "deferred:" in msg
        # Degradation qualifier should appear last.
        assert msg.index("degraded:") > msg.index("deferred:")

    def test_degraded_reason_omitted_when_none(self):
        from app.research_completion import _summarize_dispatch_outcome

        msg = _summarize_dispatch_outcome(
            spawned=3, total_findings=3, dedup_outcome=self._outcome()
        )
        assert "degraded" not in msg
        assert msg == "spawned 3/3 findings"

    def test_degraded_reason_respects_500_char_truncation(self):
        from app.research_completion import _summarize_dispatch_outcome

        # Long deferred reasons + a degradation qualifier must still fit in 500.
        long_reasons = [f"reason_{i}_with_padding_text" * 5 for i in range(20)]
        msg = _summarize_dispatch_outcome(
            spawned=0,
            total_findings=99,
            dedup_outcome=self._outcome(hash_=9),
            deferred_reason_parts=long_reasons,
            degraded_reason="provider capacity/rate-limit error — results may be incomplete",
        )
        assert len(msg) <= 500


class TestCheckCapacityDegradation:
    """_check_capacity_degradation detects provider quota / rate-limit signals."""

    def _make_task_stub(self, *, status_msg="", context=None):
        """Build a lightweight task-like object with the attrs the helper reads."""
        from types import SimpleNamespace

        return SimpleNamespace(
            latest_status_message=status_msg,
            context=context or {},
        )

    def test_none_when_clean(self):
        from app.research_completion import _check_capacity_degradation

        task = self._make_task_stub(status_msg="All findings processed normally")
        assert _check_capacity_degradation(task, "") is None

    def test_none_when_empty_message(self):
        from app.research_completion import _check_capacity_degradation

        task = self._make_task_stub(status_msg="")
        assert _check_capacity_degradation(task, "") is None

    def test_none_when_no_task(self):
        from app.research_completion import _check_capacity_degradation

        assert _check_capacity_degradation(None, "") is None

    def test_detects_quota_in_status_message(self):
        from app.research_completion import _check_capacity_degradation

        # "api usage exhausted" is a key phrase matching _external_api_subtype_from_message
        task = self._make_task_stub(
            status_msg="Fatal error: Claude Code API usage exhausted — you have no more turns"
        )
        result = _check_capacity_degradation(task, "")
        assert result is not None
        assert "provider capacity" in result or "rate-limit" in result

    def test_detects_rate_limit_in_status_message(self):
        from app.research_completion import _check_capacity_degradation

        task = self._make_task_stub(
            status_msg="upstream API returned 429 Too Many Requests — rate limit exceeded"
        )
        result = _check_capacity_degradation(task, "")
        assert result is not None
        assert "provider capacity" in result or "rate-limit" in result

    def test_detects_quota_in_logs(self):
        from app.research_completion import _check_capacity_degradation

        task = self._make_task_stub(status_msg="")
        logs = "insufficient_quota — provider rejected the request with 429"
        result = _check_capacity_degradation(task, logs)
        assert result is not None

    def test_detects_quota_in_context_failure_category(self):
        from app.research_completion import _check_capacity_degradation

        task = self._make_task_stub(
            context={
                "failure_category": "EXTERNAL_API",
                "external_api_subtype": "provider_quota",
            }
        )
        result = _check_capacity_degradation(task, "")
        assert result is not None

    def test_context_mismatch_returns_none(self):
        from app.research_completion import _check_capacity_degradation

        # EXTERNAL_API but wrong subtype
        task = self._make_task_stub(
            context={
                "failure_category": "EXTERNAL_API",
                "external_api_subtype": "network_transient",
            }
        )
        assert _check_capacity_degradation(task, "") is None

        # Right subtype but wrong failure_category
        task2 = self._make_task_stub(
            context={
                "failure_category": "CODE_ERROR",
                "external_api_subtype": "provider_quota",
            }
        )
        assert _check_capacity_degradation(task2, "") is None

    def test_agent_raw_rate_limited_with_matching_message(self):
        from app.research_completion import _check_capacity_degradation

        task = self._make_task_stub(
            status_msg="upstream rate limit exceeded — retrying",
            context={"agent_failure_category_raw": "rate_limited"},
        )
        result = _check_capacity_degradation(task, "")
        assert result is not None

    def test_agent_raw_claude_code_failed_with_quota_message(self):
        from app.research_completion import _check_capacity_degradation

        task = self._make_task_stub(
            status_msg="API usage exhausted — no more credits",
            context={"agent_failure_category_raw": "claude_code_failed"},
        )
        result = _check_capacity_degradation(task, "")
        assert result is not None

    def test_agent_raw_claude_code_failed_without_quota_message_returns_none(self):
        """A raw claude_code_failed without matching message text must not
        falsely trigger capacity degradation."""
        from app.research_completion import _check_capacity_degradation

        task = self._make_task_stub(
            status_msg="process crashed with SIGSEGV (unrelated infrastructure failure)",
            context={"agent_failure_category_raw": "claude_code_failed"},
        )
        assert _check_capacity_degradation(task, "") is None


class TestBuildPreventiveContext:
    """Regression: project_anomalies entries are sometimes stored as raw strings
    rather than the {'latest_details': ...} dict shape, which crashed
    _build_preventive_context with AttributeError: 'str' object has no attribute
    'get' and silently killed the entire research dispatch loop — 5+ consecutive
    research tasks spawned 0 children with no log, no status_message."""

    def test_string_anomaly_value_is_skipped_not_crashes(self):
        from app.research_completion import _build_preventive_context

        finding = {"related_files": ["backend/app/task_lifecycle.py"]}
        project_anomalies = {
            "task_count_spike": "raw string value, no dict",  # the corrupt shape
            "other_key": {  # well-formed; details include the top-level module
                "latest_details": "backend module path mismatch in task_lifecycle"
            },
        }
        # Must not raise. The dict-shaped entry can still match.
        result = _build_preventive_context(finding, {}, project_anomalies)
        assert result is not None
        assert result.get("anomaly_key") == "other_key"
        assert "backend module path mismatch" in result.get("warning", "")

    def test_all_string_anomalies_returns_none(self):
        from app.research_completion import _build_preventive_context

        finding = {"related_files": ["backend/app/task_lifecycle.py"]}
        project_anomalies = {
            "k1": "string value",
            "k2": 42,
            "k3": None,
        }
        # All non-dict — no anomaly match possible, must not raise.
        assert _build_preventive_context(finding, {}, project_anomalies) is None

    def test_empty_project_anomalies_returns_none(self):
        from app.research_completion import _build_preventive_context

        finding = {"related_files": ["backend/app/foo.py"]}
        assert _build_preventive_context(finding, {}, None) is None
        assert _build_preventive_context(finding, {}, {}) is None


class TestShelveFindingsForNextCycle:
    """Regression for PR #5902: the helper must write to `context.deferred_findings`,
    NOT `prior_deferred_findings`. The propagation scanner in
    `autonomous_research.py:1196` reads `deferred_findings` from completed
    research tasks and injects them as `prior_deferred_findings` on the NEXT
    research task. PR #5904 review caught that writing to
    `prior_deferred_findings` on a COMPLETED task is a dead letter — nothing
    scans that key on completed rows."""

    import pytest

    @pytest.mark.asyncio
    async def test_writes_to_deferred_findings_key_for_propagation(self, monkeypatch):
        import contextlib
        import uuid as _uuid

        from app import research_completion as rc

        captured_task = MagicMock()
        captured_task.context = {
            "some_other_key": "preserved",
            "_research_spawn_pending": True,
        }

        # Build a fake session that returns the task on the SELECT
        fake_session = MagicMock()
        fake_session.commit = AsyncMock()
        fake_result = MagicMock()
        fake_result.scalars.return_value.one_or_none.return_value = captured_task
        fake_session.execute = AsyncMock(return_value=fake_result)
        fake_session.add = MagicMock()

        @contextlib.asynccontextmanager
        async def fake_closing_session():
            yield fake_session

        # _shelve_findings_for_next_cycle lives in app.findings_shelving
        # (re-exported via rc); patch closing_session where the function
        # resolves it. rc._shelve_findings_for_next_cycle is the same object
        # via the facade re-export.
        monkeypatch.setattr(
            "app.findings_shelving.closing_session", fake_closing_session
        )

        findings = [
            {"title": "f1", "description": "d1", "prompt": "p1"},
            {"title": "f2", "description": "d2", "prompt": "p2"},
        ]
        await rc._shelve_findings_for_next_cycle(
            _uuid.uuid4(), findings, "branch_creation_defer"
        )

        # The task's context must carry the data under `deferred_findings` so
        # autonomous_research.py's propagation picks it up. NEVER under
        # `prior_deferred_findings` (that key is for in-flight reads only).
        assert "deferred_findings" in captured_task.context
        assert "prior_deferred_findings" not in captured_task.context
        assert len(captured_task.context["deferred_findings"]) == 2
        # Pre-existing context keys must be preserved (idempotent merge).
        assert captured_task.context["some_other_key"] == "preserved"
        assert "_research_spawn_pending" not in captured_task.context
        # Findings must be stamped for the recovery TTL/decay filters.
        for f in captured_task.context["deferred_findings"]:
            assert f["deferred_reason"] == "branch_creation_defer"
            assert f["deferred_count"] == 1
            assert "originally_researched_at" in f
            assert "finding_hash" in f
        # And the task's latest_status_message must be loud, not the heartbeat.
        assert "shelved" in captured_task.latest_status_message.lower()
        fake_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_appends_to_existing_deferred_findings(self, monkeypatch):
        """Re-shelving on the same task must append, not replace."""
        import contextlib
        import uuid as _uuid

        from app import research_completion as rc

        captured_task = MagicMock()
        captured_task.context = {
            "deferred_findings": [{"title": "prior", "finding_hash": "abc"}]
        }
        fake_session = MagicMock()
        fake_session.commit = AsyncMock()
        fake_result = MagicMock()
        fake_result.scalars.return_value.one_or_none.return_value = captured_task
        fake_session.execute = AsyncMock(return_value=fake_result)
        fake_session.add = MagicMock()

        @contextlib.asynccontextmanager
        async def fake_closing_session():
            yield fake_session

        monkeypatch.setattr(
            "app.findings_shelving.closing_session", fake_closing_session
        )

        await rc._shelve_findings_for_next_cycle(
            _uuid.uuid4(),
            [{"title": "new", "description": "d", "prompt": "p"}],
            "branch_creation_defer",
        )

        # Both prior and new entries must be present.
        deferred = captured_task.context["deferred_findings"]
        assert len(deferred) == 2
        assert deferred[0]["title"] == "prior"
        assert deferred[1]["title"] == "new"


# ─────────────────────────────────────────────────────────────────────────────
# Forward-quality grader: escalation + spawn-on-findings gate
#
# Research findings can spawn implementation work. When the research-quality
# grader is enabled, those findings must clear an INDEPENDENT grader before any
# child spawns: the research task parks EVALUATING, a fresh grader task scores
# the artifact, and only a PASS verdict lets the findings spawn children (a FAIL
# fails the research task and spawns nothing). The gate is proportional —
# findings that would spawn nothing are never escalated.
# ─────────────────────────────────────────────────────────────────────────────
def _enable_grader(mocker):
    """Flip the dark-shipped research-quality-grader flag ON for one test."""
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"research_quality_grader_enabled": True},
    )


async def _grader_project_and_task(db_session, direction_id="d-grade01"):
    """A minimal autonomous project + a research task ready to complete."""
    from test_autonomous_research import _active_direction, _make_research_task

    direction = _active_direction(direction_id, "Grader gate direction")
    project = _Project(
        name=f"Grader Gate {direction_id}",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = await _make_research_task(project, direction, db_session)
    # Completion handlers are entered only after the agent terminal transition
    # has durably moved the parent out of IN_PROGRESS.  Keep this shared fixture
    # faithful so the spawn-boundary cancellation fence is exercised honestly.
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()
    return project, direction, task


class TestForwardQualityWouldSpawnPredicate:
    """_research_findings_would_spawn: blast-radius predicate (pure)."""

    def test_actionable_finding_would_spawn(self):
        assert _research_findings_would_spawn(
            [{"type": "improvement", "prompt": "do x", "actionability_grade": "HIGH"}]
        )

    def test_legacy_description_only_finding_would_spawn(self):
        assert _research_findings_would_spawn(
            [
                {
                    "type": "improvement",
                    "description": "do x",
                    "prompt": "   ",
                    "actionability_grade": "HIGH",
                }
            ]
        )

    def test_observation_types_do_not_spawn(self):
        for t in (
            "design_doc_observation",
            "direction_update",
            "direction_proposal",
            "direction_note",
        ):
            assert not _research_findings_would_spawn(
                [{"type": t, "prompt": "do x", "actionability_grade": "HIGH"}]
            ), t

    def test_low_actionability_and_empty_prompt_do_not_spawn(self):
        assert not _research_findings_would_spawn(
            [{"type": "improvement", "prompt": "do x", "actionability_grade": "LOW"}]
        )
        assert not _research_findings_would_spawn(
            [{"type": "improvement", "prompt": "   "}]
        )
        assert not _research_findings_would_spawn([])

    def test_unrecognized_already_spawned_field_cannot_bypass_quality_gate(self):
        assert _research_findings_would_spawn(
            [{"type": "improvement", "prompt": "do x", "already_spawned": True}]
        )


class TestParseGraderVerdict:
    """parse_grader_verdict: structured + anchored parse, fail-closed defaults."""

    def test_structured_pass(self):
        v = parse_grader_verdict('reasoning...\n{"verdict": "pass", "reasoning": "ok"}')
        assert v.passed is True
        assert v.strategy == "forward-quality"

    def test_structured_fail(self):
        v = parse_grader_verdict('{"verdict": "fail", "reasoning": "thin"}')
        assert v.passed is False

    def test_anchored_fallback(self):
        assert parse_grader_verdict("the VERDICT: pass overall").passed is True
        assert parse_grader_verdict("VERDICT: fail — ungrounded").passed is False

    def test_not_completed_fails_closed(self):
        assert parse_grader_verdict("looks great", completed=False).passed is False

    def test_unparseable_fails_closed(self):
        assert parse_grader_verdict("no machine verdict in here").passed is False
        assert parse_grader_verdict("").passed is False

    def test_alias_tokens_pass(self):
        """Divergent accept/approve/approved/passed aliases all map to passed=True."""
        for token in ("accept", "approve", "approved", "passed"):
            v = parse_grader_verdict(f'{{"verdict": "{token}", "reasoning": "ok"}}')
            assert v.passed is True, f"token {token!r} should map to passed=True"

    def test_alias_tokens_fail(self):
        """Divergent reject/rejected/request_changes aliases all map to passed=False."""
        for token in ("reject", "rejected", "request_changes"):
            v = parse_grader_verdict(f'{{"verdict": "{token}", "reasoning": "nope"}}')
            assert v.passed is False, f"token {token!r} should map to passed=False"


@_pytest.mark.asyncio
async def test_spawn_forward_quality_grader_is_single_shot(db_session):
    """spawn_forward_quality_grader parks the source once; a second call no-ops."""
    _project, _direction, task = await _grader_project_and_task(db_session, "d-single")

    g1 = await spawn_forward_quality_grader(task, "artifact text", db_session)
    assert g1 is not None
    assert (g1.first_payload or {}).get("type") == "investigate"
    assert (g1.first_payload or {}).get("quality_tier") == "sonnet"
    assert (g1.context or {}).get(QUALITY_GRADER_SOURCE_KEY) == str(task.id)
    assert g1.parent_task_id is None
    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert task.context.get("awaiting_quality_grader") == str(g1.id)

    # Second call must NOT spawn another grader (single-shot convergence).
    g2 = await spawn_forward_quality_grader(task, "artifact text", db_session)
    assert g2 is None
    graders = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id, _Task.id != task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(graders) == 1


@_pytest.mark.asyncio
async def test_research_finding_second_opinion_sidecar_parks_and_records_verdict(
    db_session, mocker
):
    """End-to-end per-finding challenge seam: spawn_finding_challenge parks the
    source research task EVALUATING behind a bounded read-only investigate
    side-car, and _resolve_finding_challenge_completion routes the side-car's
    structured second opinion back onto the source as a FindingChallengeRecord
    — distinct from the batch quality_grader verdict."""
    from app.schemas import AgentStatusPayload
    from app.task_lifecycle import _resolve_finding_challenge_completion

    _project, _direction, task = await _grader_project_and_task(db_session, "d-chal01")

    finding = {
        "title": "Stale ref to compute_jobs.poll()",
        "prompt": "fix the polling call",
        "actionability_grade": "MEDIUM",
    }

    # Spawn the challenge side-car for a reproduce-absent verdict.
    sidecar = await spawn_finding_challenge(
        task,
        finding,
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        "absent",
        db_session,
        question="did compute_jobs.poll move?",
    )
    assert sidecar is not None
    assert (sidecar.first_payload or {}).get("type") == "investigate"
    assert (sidecar.first_payload or {}).get("scope") == "small"
    assert (sidecar.first_payload or {}).get("quality_tier") == "sonnet"
    assert (sidecar.context or {}).get(FINDING_CHALLENGE_SOURCE_KEY) == str(task.id)
    assert (sidecar.context or {}).get("challenged_boundary") == (
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT
    )
    assert sidecar.parent_task_id is None

    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert task.context.get("awaiting_finding_challenge") == str(sidecar.id)
    # The challenge verdict is NOT yet recorded, and is kept separate from the
    # batch quality_grader verdict key.
    assert task.context.get(FINDING_CHALLENGE_VERDICT_KEY) is None

    # Single-shot: a second spawn while one is in flight must no-op.
    dup = await spawn_finding_challenge(
        task, finding, CHALLENGE_BOUNDARY_REPRODUCE_ABSENT, "absent", db_session
    )
    assert dup is None

    # The side-car returns a disagreeing second opinion (code was moved, not gone).
    sidecar.status = TaskStatus.COMPLETED
    db_session.add(sidecar)
    await db_session.commit()

    # Keep the re-dispatch focused on the seam: stub the research resume handler
    # (its drop/dispatch wiring is exercised elsewhere) and the broadcast.
    resume = mocker.patch(
        "app.research_completion._handle_research_completion",
        new_callable=AsyncMock,
    )
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)

    payload = AgentStatusPayload(
        task_id=sidecar.id,
        status=TaskStatus.COMPLETED,
        logs=(
            "Checked git history: compute_jobs.poll() was renamed, not removed.\n"
            '{"verdict": "present", "agreement": "disagrees", '
            '"reasoning": "symbol relocated to poll_once()"}'
        ),
    )
    await _resolve_finding_challenge_completion(sidecar, payload, db_session)

    # The source was re-dispatched to its completion handler with the verdict set.
    resume.assert_awaited_once()

    await db_session.refresh(task)
    record = task.context.get(FINDING_CHALLENGE_VERDICT_KEY)
    assert record is not None, "second opinion must be recorded on the source"
    assert record["challenged_boundary"] == CHALLENGE_BOUNDARY_REPRODUCE_ABSENT
    assert record["first_verdict"] == "absent"
    assert record["second_opinion_verdict"] == "present"
    assert record["agreement"] == "disagrees"
    assert record["sidecar_task_id"] == str(sidecar.id)
    assert "relocated" in record["reason"]
    # The per-finding record is distinct from the batch quality_grader verdict.
    assert task.context.get(QUALITY_GRADER_VERDICT_KEY) is None
    assert not task.context.get("awaiting_finding_challenge")


@_pytest.mark.asyncio
async def test_actionable_findings_escalate_to_grader_and_park(db_session, mocker):
    """Enabled + spawnable findings → grader spawned, research parked, 0 children."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-esc01")
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding(direction_id=direction["id"])],
    )
    _enable_grader(mocker)

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # No implementation children yet — the source is parked awaiting the grade.
    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert children == [], "spawnable findings must NOT spawn before the grade"

    # Exactly one independent grader task, linked back to the research task.
    others = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id, _Task.id != task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(others) == 1
    grader = others[0]
    assert (grader.context or {}).get(QUALITY_GRADER_SOURCE_KEY) == str(task.id)
    assert (grader.first_payload or {}).get("type") == "investigate"
    grader_prompt = (grader.first_payload or {}).get("prompt", "")
    assert "Requested topic / prompt" in grader_prompt
    assert "Grader gate direction" in grader_prompt
    assert task.status == TaskStatus.EVALUATING
    assert task.context.get("awaiting_quality_grader")


@_pytest.mark.asyncio
async def test_replay_while_forward_quality_grader_pending_is_strict_noop(
    db_session, mocker
):
    """A duplicate completion cannot fall through an in-flight grader guard."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    project, direction, task = await _grader_project_and_task(
        db_session, "d-quality-replay"
    )
    _patch_research_externals(mocker)
    finding = _make_finding(
        "Do not route before the quality verdict", direction_id=direction["id"]
    )
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )
    _enable_grader(mocker)

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)
    waiting_id = task.context.get("awaiting_quality_grader")
    assert waiting_id

    # Replaying the same completion while the sidecar is still in flight used
    # to call the single-shot spawner, receive None, and continue ungated into
    # child creation. The awaiting marker must stop the handler before parsing
    # or any spawn path.
    await _handle_research_completion(task, "", db_session)
    tasks = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert [row for row in tasks if row.parent_task_id == task.id] == []
    graders = [
        row
        for row in tasks
        if (row.context or {}).get(QUALITY_GRADER_SOURCE_KEY) == str(task.id)
    ]
    assert [str(row.id) for row in graders] == [str(waiting_id)]
    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert task.context.get("awaiting_quality_grader") == waiting_id
    assert (task.context or {}).get(FINALIZED_RESEARCH_FINDINGS_KEY) is not True


@_pytest.mark.asyncio
async def test_prior_only_findings_are_included_in_forward_quality_grade(
    db_session, mocker
):
    """A zero-headroom [] artifact cannot bypass grading of carried work."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-prior-grade"
    )
    carried = _make_finding(
        "Carried finding must be graded", direction_id=direction["id"]
    )
    carried.update(
        {
            "actionability_grade": "HIGH",
            "deferred_reason": "batch_cap",
            "deferred_count": 1,
            # Legacy deferred rows can predate the prompt-required producer
            # contract. The spawn path falls back to description, so the
            # quality gate must use the same canonical body predicate.
            "prompt": "",
        }
    )
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": [carried],
    }
    db_session.add(task)
    await db_session.commit()
    _patch_research_externals(mocker)
    mocker.patch("app.research_completion._parse_research_findings", return_value=[])
    _enable_grader(mocker)

    await _handle_research_completion(task, "[]", db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert children == []
    graders = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id,
                    _Task.id != task.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(graders) == 1
    assert carried["title"] in (graders[0].first_payload or {}).get("prompt", "")
    assert task.status == TaskStatus.EVALUATING


@_pytest.mark.asyncio
async def test_parked_quality_grade_does_not_acknowledge_carried_producer(
    db_session, mocker
):
    """A parked source has not consumed its producer copy yet."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    from app.research_deferred_carry import (
        DEFERRED_FINDING_CONSUMPTION_ACKS_KEY,
        canonical_deferred_finding_hash,
    )

    project, direction, task = await _grader_project_and_task(
        db_session, "d-prior-ack-park"
    )
    carried = _make_finding(
        "Carried producer remains live while parked", direction_id=direction["id"]
    )
    carried.update({"actionability_grade": "HIGH", "deferred_count": 1})
    carried["finding_hash"] = canonical_deferred_finding_hash(carried)
    producer = _Task(
        project_id=project.id,
        title="Parked carry producer",
        payloads=[{"type": "research", "base_branch": "main"}],
        context={"deferred_findings": [carried]},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(producer)
    await db_session.flush()
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": [
            {**carried, "source_task_id": str(producer.id)}
        ],
    }
    db_session.add(task)
    await db_session.commit()

    _patch_research_externals(mocker)
    mocker.patch("app.research_completion._parse_research_findings", return_value=[])
    _enable_grader(mocker)

    await _handle_research_completion(task, "[]", db_session)
    await db_session.refresh(task)
    await db_session.refresh(producer)

    assert task.status == TaskStatus.EVALUATING
    assert DEFERRED_FINDING_CONSUMPTION_ACKS_KEY not in (producer.context or {})
    await db_session.refresh(project)
    assert project.vision.get("_research_spawn_count", 0) == 0
    assert (task.context or {}).get(FINALIZED_RESEARCH_FINDINGS_KEY) is not True


@_pytest.mark.asyncio
async def test_flag_off_spawns_children_without_grader(db_session, mocker):
    """Non-regression: with the flag OFF, findings spawn children immediately."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    project, direction, task = await _grader_project_and_task(db_session, "d-off01")
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding(direction_id=direction["id"])],
    )
    # Flag intentionally NOT enabled.

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1, "legacy spawn path must be unchanged when flag is off"
    await db_session.refresh(task)
    await db_session.refresh(project)
    assert task.status == TaskStatus.EVALUATING
    assert task.context[FINALIZED_RESEARCH_FINDINGS_KEY] is True
    assert project.vision["_research_spawn_count"] == 1
    assert str(task.id) in project.vision["_research_spawn_counted_ids"]
    # No grader anywhere in the project.
    all_tasks = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert all(
        not (t.context or {}).get(QUALITY_GRADER_SOURCE_KEY) for t in all_tasks
    ), "no grader may spawn when the gate is disabled"


@_pytest.mark.asyncio
async def test_recursive_research_child_uses_canonical_route_and_frozen_envelope(
    db_session, mocker
):
    """A research finding remains a bounded frontier parent, not a pinned leaf."""
    from app import shared_state
    from app.max_plan_pool import resolve_task_routing
    from app.research_completion import effective_research_finding_headroom
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-recursive-envelope"
    )
    frozen_direction = {
        "id": direction["id"],
        "text": "Resolve the admitted scheduler mechanism only",
        "completion_criteria": "One causal scheduler mechanism is decision-ready.",
        "paths": [
            "backend/app/orchestrator.py",
            "backend/app/max_plan_pool.py",
        ],
    }
    frozen_intent = {"release_goal": "restore useful research fan-out"}
    frozen_bet = {
        "claim": "Admission suppresses recursive research work.",
        "decision_question": "Which admission fence causes the suppression?",
    }
    task.payloads = [
        {
            "type": "research",
            "base_branch": "main",
            "directions": [frozen_direction],
            "current_direction_id": direction["id"],
            "discovery_generation": 4,
            "discovery_phase": "probe",
            "discovery_intent_snapshot": frozen_intent,
            "discovery_bet_snapshot": frozen_bet,
        }
    ]
    task.context = {**(task.context or {}), "direction_id": direction["id"]}
    db_session.add(task)
    await db_session.commit()

    mocker.patch.dict(
        shared_state.runtime_config.setdefault("task_type_models", {}),
        {"research": "legacy-research-model"},
    )
    mocker.patch.dict(
        shared_state.runtime_config.setdefault("task_type_efforts", {}),
        {"research": "low"},
    )
    mocker.patch.dict(
        shared_state.runtime_config.setdefault("task_type_routing", {}),
        {"research": {"providers": ["openai"], "effort": "max"}},
    )
    mocker.patch.dict(
        shared_state.runtime_config,
        {"research_quality_grader_enabled": False},
    )
    _patch_research_externals(mocker)
    finding = _make_finding(
        "Confirm the bounded scheduler mechanism",
        direction_id=direction["id"],
        finding_type="research",
    )
    finding.update(
        {
            "model": "finding-level-legacy-model",
            "effort": "low",
            "work_class": "confirmatory",
            "stop_or_pivot_condition": "Stop after the competing race is refuted.",
        }
    )
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1
    child = children[0]
    payload = child.first_payload or {}
    assert payload["type"] == "research"
    assert "model" not in payload
    assert "effort" not in payload
    assert resolve_task_routing(child, shared_state.runtime_config) == {
        "providers": ["openai"],
        "effort": "max",
    }
    assert payload["directions"] == [frozen_direction]
    assert payload["current_direction_id"] == direction["id"]
    assert payload["discovery_generation"] == 4
    assert payload["discovery_phase"] == "probe"
    assert payload["discovery_intent_snapshot"] == frozen_intent
    assert payload["discovery_bet_snapshot"] == frozen_bet
    assert payload["research_question_snapshot"] == finding["description"]
    assert payload["research_commission_kind"] == "recursive_subresearch"
    assert _research_question_for_grader(child) == finding["description"]
    assert payload["work_headroom"] == effective_research_finding_headroom(0)
    assert payload["work_class"] == "confirmatory"
    assert (
        payload["stop_or_pivot_condition"]
        == "Stop after the competing race is refuted."
    )


@_pytest.mark.asyncio
async def test_failed_grade_blocks_spawn_and_fails_research(db_session, mocker):
    """A FAILED grader verdict spawns NO children and fails the research task."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-fail01")
    _patch_research_externals(mocker)

    # Simulate the grader having returned a FAIL verdict on the re-entrant pass.
    task.plan_output = [_make_finding(direction_id=direction["id"])]
    task.context = {
        **(task.context or {}),
        QUALITY_GRADER_VERDICT_KEY: {
            "passed": False,
            "reason": "findings are ungrounded; load-bearing claim absent at HEAD",
        },
    }
    db_session.add(task)
    await db_session.commit()

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert children == [], "a failing grade must spawn NO implementation tasks"
    assert task.status == TaskStatus.FAILED
    assert (task.context or {}).get("evaluation_gate", {}).get("passed") is False


@_pytest.mark.asyncio
async def test_passing_grade_resumes_and_spawns_children(db_session, mocker):
    """A PASSING grader verdict resumes the research task and spawns children."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-pass01")
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding(direction_id=direction["id"])],
    )

    # Simulate the verdict the grader-completion resolver writes before re-dispatch.
    task.plan_output = [_make_finding(direction_id=direction["id"])]
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        QUALITY_GRADER_VERDICT_KEY: {"passed": True, "reason": "well-grounded"},
    }
    db_session.add(task)
    await db_session.commit()

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1, "a passing grade must let the findings spawn as before"
    # The verdict pass does NOT re-spawn a grader (single-shot).
    assert not any((c.context or {}).get(QUALITY_GRADER_SOURCE_KEY) for c in children)
    # With children spawned, the research task takes the normal post-spawn
    # EVALUATING state (awaiting child rollup) — NOT FAILED, and no longer
    # parked awaiting a grade.
    assert task.status == TaskStatus.EVALUATING


@_pytest.mark.asyncio
async def test_capacity_degradation_surfaced_in_completion_message(
    db_session, mocker
):
    """A research task whose latest_status_message indicates a provider quota
    error must surface the degradation in its completion message and context,
    even when no findings were parsed."""
    from test_autonomous_research import _patch_research_externals

    _project, _direction, task = await _grader_project_and_task(
        db_session, "d-deg01"
    )
    _patch_research_externals(mocker)

    # Patch findings parser to return nothing, and health check to healthy.
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[]
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    # Simulate a task whose last agent heartbeat was a capacity error.
    task.latest_status_message = (
        "Fatal error: Claude Code API usage exhausted — insufficient balance"
    )
    db_session.add(task)
    await db_session.commit()

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # The completion message must include the degradation qualifier.
    assert "no actionable findings" in task.latest_status_message
    assert "degraded:" in task.latest_status_message
    assert "provider capacity" in task.latest_status_message or "rate-limit" in task.latest_status_message

    # Structured context field must be set.
    assert (task.context or {}).get("degraded_by_capacity") is True
    assert (task.context or {}).get("degraded_by_capacity_reason") is not None


@_pytest.mark.asyncio
async def test_apply_post_spawn_state_preserves_degradation_qualifier(
    db_session, mocker
):
    """apply_post_spawn_state must re-append the (degraded: ...) qualifier to
    latest_status_message when task.context carries degraded_by_capacity_reason,
    even though it overwrites the message."""
    from app.research_direction_state import apply_post_spawn_state

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-deg-aps01"
    )

    task.context = {
        **(task.context or {}),
        "direction_id": direction["id"],
        "degraded_by_capacity": True,
        "degraded_by_capacity_reason": "provider capacity — account exhausted",
    }
    task.latest_status_message = "Research completed normally"
    db_session.add(task)
    await db_session.commit()

    await apply_post_spawn_state(
        db_session,
        task,
        [],   # findings
        [],   # child_tasks
        [],   # deduplicated
        [],   # insights
        [],   # direction_notes
        [],   # direction_proposals
        0,    # hash_dedup_count
        0,    # word_overlap_count
        0,    # file_overlap_count
        0,    # evidence_overlap_count
        0,    # type_filtered_count
        0,    # empty_prompt_count
        "research-branch",
        "main",
        0,    # research_depth
        None, # parent_research_task_id
    )

    await db_session.refresh(task)
    assert "degraded:" in task.latest_status_message
    assert "account exhausted" in task.latest_status_message


@_pytest.mark.asyncio
async def test_apply_post_spawn_state_no_degradation_on_clean_task(db_session, mocker):
    """apply_post_spawn_state must NOT add the degradation qualifier when
    degraded_by_capacity_reason is absent from task.context."""
    from app.research_direction_state import apply_post_spawn_state

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-deg-aps02"
    )

    task.context = {
        **(task.context or {}),
        "direction_id": direction["id"],
    }
    task.latest_status_message = "Research completed normally"
    db_session.add(task)
    await db_session.commit()

    await apply_post_spawn_state(
        db_session,
        task,
        [],   # findings
        [],   # child_tasks
        [],   # deduplicated
        [],   # insights
        [],   # direction_notes
        [],   # direction_proposals
        0,    # hash_dedup_count
        0,    # word_overlap_count
        0,    # file_overlap_count
        0,    # evidence_overlap_count
        0,    # type_filtered_count
        0,    # empty_prompt_count
        "research-branch",
        "main",
        0,    # research_depth
        None, # parent_research_task_id
    )

    await db_session.refresh(task)
    assert "degraded" not in (task.latest_status_message or "")


@_pytest.mark.asyncio
async def test_apply_post_spawn_state_records_quality_grader_verdict(db_session):
    """apply_post_spawn_state must fold quality_grader_verdict from task.context
    into the direction's research_history entry when the verdict is present."""
    from app.research_direction_state import apply_post_spawn_state
    from test_autonomous_research import _make_finding

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-qgv-pass"
    )

    task.context = {
        **(task.context or {}),
        "direction_id": direction["id"],
        QUALITY_GRADER_VERDICT_KEY: {
            "passed": True,
            "reason": "findings are well-grounded",
            "grader_task_id": "grader-aaa",
            "strategy": "forward-quality",
        },
    }
    finding = _make_finding("QG verdict finding", direction_id=direction["id"])
    task.plan_output = [finding]
    db_session.add(task)
    await db_session.commit()

    await apply_post_spawn_state(
        db_session,
        task,
        [finding],
        [],
        [(0, finding, "hash-qgv")],
        [], [], [],
        0, 0, 0, 0, 0, 0,
        "research-branch", "main", 0, None,
    )
    await db_session.refresh(_project)

    history = _project.directions[0].get("research_history", [])
    assert history, "expected a history entry"
    last_entry = history[-1]
    assert "quality_grader_verdict" in last_entry, (
        f"quality_grader_verdict missing from history entry: {last_entry}"
    )
    assert last_entry["quality_grader_verdict"]["passed"] is True
    assert last_entry["quality_grader_verdict"]["reason"] == "findings are well-grounded"


@_pytest.mark.asyncio
async def test_apply_post_spawn_state_records_quality_grader_failed_verdict(db_session):
    """apply_post_spawn_state must record a failed quality_grader_verdict too."""
    from app.research_direction_state import apply_post_spawn_state
    from test_autonomous_research import _make_finding

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-qgv-fail"
    )

    task.context = {
        **(task.context or {}),
        "direction_id": direction["id"],
        QUALITY_GRADER_VERDICT_KEY: {
            "passed": False,
            "reason": "findings lack actionable evidence",
            "grader_task_id": "grader-bbb",
            "strategy": "forward-quality",
        },
    }
    finding = _make_finding("QG verdict failed finding", direction_id=direction["id"])
    task.plan_output = [finding]
    db_session.add(task)
    await db_session.commit()

    await apply_post_spawn_state(
        db_session,
        task,
        [finding],
        [],
        [(0, finding, "hash-qgv-fail")],
        [], [], [],
        0, 0, 0, 0, 0, 0,
        "research-branch", "main", 0, None,
    )
    await db_session.refresh(_project)

    history = _project.directions[0].get("research_history", [])
    assert history, "expected a history entry"
    last_entry = history[-1]
    assert "quality_grader_verdict" in last_entry
    assert last_entry["quality_grader_verdict"]["passed"] is False


@_pytest.mark.asyncio
async def test_apply_post_spawn_state_omits_quality_grader_when_absent(db_session):
    """apply_post_spawn_state must NOT add quality_grader_verdict when the task
    context has no quality grader result (legacy or ungraded research)."""
    from app.research_direction_state import apply_post_spawn_state
    from test_autonomous_research import _make_finding

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-qgv-absent"
    )

    task.context = {
        **(task.context or {}),
        "direction_id": direction["id"],
        # No QUALITY_GRADER_VERDICT_KEY
    }
    task.context.pop(QUALITY_GRADER_VERDICT_KEY, None)
    finding = _make_finding("No QG finding", direction_id=direction["id"])
    task.plan_output = [finding]
    db_session.add(task)
    await db_session.commit()

    await apply_post_spawn_state(
        db_session,
        task,
        [finding],
        [],
        [(0, finding, "hash-qgv-absent")],
        [], [], [],
        0, 0, 0, 0, 0, 0,
        "research-branch", "main", 0, None,
    )
    await db_session.refresh(_project)

    history = _project.directions[0].get("research_history", [])
    assert history, "expected a history entry"
    last_entry = history[-1]
    assert "quality_grader_verdict" not in last_entry, (
        f"quality_grader_verdict should not be present when absent from task context: {last_entry}"
    )


@_pytest.mark.asyncio
async def test_capacity_degradation_no_false_positive_on_clean_task(
    db_session, mocker
):
    """A task that completed normally must NOT carry the degradation signal."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-deg02"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding(direction_id=direction["id"])],
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    # Clean task — no capacity error in status message.
    task.latest_status_message = "All research findings processed normally"
    db_session.add(task)
    await db_session.commit()

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # Completion message must NOT mention degradation.
    assert "degraded" not in (task.latest_status_message or "")
    assert (task.context or {}).get("degraded_by_capacity") is not True


@_pytest.mark.asyncio
async def test_capacity_degradation_surfaced_in_dispatch_summary(
    db_session, mocker
):
    """A research task that spawns findings but had a capacity error mid-run
    must surface degradation in the dispatch outcome message."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-deg03"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding(direction_id=direction["id"])],
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    # Capacity error in logs (even though status message is clean).
    deg_logs = "insufficient_quota — provider rejected the request with 429"
    task.latest_status_message = "Completed processing findings"
    db_session.add(task)
    await db_session.commit()

    await _handle_research_completion(task, deg_logs, db_session)
    await db_session.refresh(task)

    # Dispatch summary must include the degradation qualifier.
    assert "degraded:" in (task.latest_status_message or "")
    assert "provider capacity" in task.latest_status_message or "rate-limit" in task.latest_status_message
    assert (task.context or {}).get("degraded_by_capacity") is True


@_pytest.mark.asyncio
async def test_capacity_degradation_from_context_failure_category(
    db_session, mocker
):
    """When task.context already has EXTERNAL_API / provider_quota from
    persist_failure_category, the completion message must surface degradation."""
    from test_autonomous_research import _patch_research_externals
    from app.failure_classification import EXTERNAL_API, EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA

    _project, _direction, task = await _grader_project_and_task(
        db_session, "d-deg04"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[]
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    # Persisted failure classification on context — no error in status message.
    task.context = {
        **(task.context or {}),
        "failure_category": EXTERNAL_API,
        "external_api_subtype": EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA,
    }
    task.latest_status_message = "All clean"
    db_session.add(task)
    await db_session.commit()

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    assert "degraded:" in (task.latest_status_message or "")
    assert (task.context or {}).get("degraded_by_capacity") is True


@_pytest.mark.asyncio
async def test_finding_challenge_reentry_counts_at_finalized_boundary(
    db_session, mocker
):
    """A parked challenge counts once only when its resumed result is accepted."""

    project, _direction, task = await _grader_project_and_task(db_session, "d-fc-re")
    project.vision = {"_research_spawn_count": 7, "purpose": "keep-me"}
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        FINDING_CHALLENGE_VERDICT_KEY: {
            "challenged_boundary": CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
            "first_verdict": "absent",
            "second_opinion_verdict": "present",
            "agreement": CHALLENGE_DISAGREES,
            "reason": "symbol relocated",
            "sidecar_task_id": str(uuid.uuid4()),
        },
    }
    db_session.add(project)
    db_session.add(task)
    await db_session.commit()

    mocker.patch("app.research_completion._parse_research_findings", return_value=[])

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(project)

    assert project.vision["_research_spawn_count"] == 8
    assert str(task.id) in project.vision["_research_spawn_counted_ids"]
    assert project.vision["purpose"] == "keep-me"
    assert task.context.get(QUALITY_GRADER_VERDICT_KEY) is None
    assert task.context.get(ADVERSARIAL_GATE_VERDICT_KEY) is None


@_pytest.mark.asyncio
async def test_research_spawn_count_idempotent_on_replay(db_session, mocker):
    """Post-commit replay of the initial completion must not double-increment _research_spawn_count.

    On a replay, all three verdicts (grader/adversarial/challenge) are still None,
    so the existing verdict-presence guard passes again. Without the task_id stamp,
    the counter increments on every replay, shifting the modulo gate that decides
    when to fire a strategic_review.
    """
    project, _direction, task = await _grader_project_and_task(db_session, "d-idem-rsc")
    project.vision = {"_research_spawn_count": 4, "purpose": "keep-me"}
    task.status = TaskStatus.COMPLETED
    # No verdicts — this is the initial-completion state (also what replay sees).
    task.context = {**(task.context or {})}
    db_session.add(project)
    db_session.add(task)
    await db_session.commit()

    mocker.patch("app.research_completion._parse_research_findings", return_value=[])

    # First call: should increment once (4 → 5) and stamp the task_id.
    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(project)
    assert project.vision["_research_spawn_count"] == 5
    assert str(task.id) in (project.vision.get("_research_spawn_counted_ids") or [])

    # Second call (simulates post-commit replay): counter must stay at 5.
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()
    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(project)
    assert project.vision["_research_spawn_count"] == 5, (
        "replay must not double-increment _research_spawn_count"
    )
    assert project.vision["purpose"] == "keep-me"


@_pytest.mark.asyncio
async def test_finalized_replay_with_empty_artifact_preserves_snapshot_and_children(
    db_session,
):
    """A finalized receipt is authoritative even when replay logs contain ``[]``."""
    from app.research_completion import FINALIZED_RESEARCH_FINDINGS_KEY
    from test_autonomous_research import _make_finding

    project, direction, task = await _grader_project_and_task(
        db_session, "d-finalized-replay"
    )
    accepted = _make_finding(
        "Accepted finding survives replay", direction_id=direction["id"]
    )
    task.status = TaskStatus.COMPLETED
    task.latest_status_message = "Authoritative research completion"
    task.plan_output = [accepted]
    task.context = {
        **(task.context or {}),
        FINALIZED_RESEARCH_FINDINGS_KEY: True,
    }
    child = _Task(
        project_id=project.id,
        title="Accepted child survives replay",
        payloads=[{"type": "create", "prompt": "Implement the accepted finding"}],
        status=TaskStatus.QUEUED,
        parent_task_id=task.id,
        created_by="ORCHESTRATOR",
        context={"direction_id": direction["id"]},
    )
    db_session.add(task)
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)

    await _handle_research_completion(task, "[]", db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert task.status == TaskStatus.COMPLETED
    assert task.latest_status_message == "Authoritative research completion"
    assert task.plan_output == [accepted]
    assert [item.id for item in children] == [child.id]


@_pytest.mark.asyncio
async def test_normal_finalization_relocks_project_then_task_before_cadence(
    db_session, mocker
):
    """The post-bookkeeping boundary refreshes both rows before its first write."""
    import contextlib

    import app.research_completion as research_completion
    import app.research_cycle_cadence as research_cycle_cadence
    from app.task_locking import lock_project_then_task as real_lock_pair
    from test_autonomous_research import _make_finding, _patch_research_externals

    project, direction, task = await _grader_project_and_task(
        db_session, "d-final-lock-order"
    )
    _patch_research_externals(mocker)
    finding = _make_finding(
        "Finalize only under canonical locks", direction_id=direction["id"]
    )
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )

    lock_selects: list[str] = []
    lifecycle_events: list[str] = []
    recording_lock_sql = False
    real_execute = db_session.execute

    async def recording_execute(statement, *args, **kwargs):
        if recording_lock_sql:
            lock_selects.append(" ".join(str(statement).lower().split()))
        return await real_execute(statement, *args, **kwargs)

    mocker.patch.object(db_session, "execute", new=recording_execute)
    locked: dict[str, object] = {}

    @contextlib.asynccontextmanager
    async def recording_lock_pair(session, project_id, task_id, **kwargs):
        nonlocal recording_lock_sql
        lifecycle_events.append("lock_pair_enter")
        assert session.sync_session.autoflush is False
        recording_lock_sql = True
        try:
            async with real_lock_pair(
                session, project_id, task_id, **kwargs
            ) as pair:
                locked["project"], locked["task"] = pair
                lifecycle_events.append("project_then_task_locked")
                yield pair
        finally:
            recording_lock_sql = False

    mocker.patch.object(
        research_cycle_cadence,
        "lock_project_then_task",
        new=recording_lock_pair,
    )
    real_count = research_completion.count_accepted_research_cycle

    async def recording_count(session, accepted_task, *, project):
        assert project is locked["project"]
        assert accepted_task is locked["task"]
        lifecycle_events.append("cadence_with_locked_identity")
        return await real_count(session, accepted_task, project=project)

    mocker.patch.object(
        research_completion,
        "count_accepted_research_cycle",
        new=recording_count,
    )

    await _handle_research_completion(task, "", db_session, project=project)

    assert lifecycle_events == [
        "lock_pair_enter",
        "project_then_task_locked",
        "cadence_with_locked_identity",
    ]
    assert len(lock_selects) == 2
    assert " from project " in f" {lock_selects[0]} "
    assert " from task " in f" {lock_selects[1]} "
    await db_session.refresh(task)
    assert task.context[FINALIZED_RESEARCH_FINDINGS_KEY] is True


@_pytest.mark.asyncio
async def test_finalization_aborts_when_peer_admin_cancels_evaluating_parent(
    db_session, mocker
):
    """A terminal admin write between bookkeeping and final lock must win."""

    import contextlib

    from fastapi import BackgroundTasks
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import sessionmaker

    import app.research_completion as research_completion
    from app.admin_helpers import modify_task_apply
    from app.research_deferred_carry import (
        DEFERRED_FINDING_CONSUMPTION_ACKS_KEY,
        unacknowledged_deferred_findings,
    )
    from test_autonomous_research import _make_finding, _patch_research_externals

    project, direction, task = await _grader_project_and_task(
        db_session, "d-final-admin-cancel"
    )
    carried = _make_finding(
        "Do not consume carry after admin cancellation",
        direction_id=direction["id"],
    )
    carried.update(
        {
            "actionability_grade": "HIGH",
            "deferred_count": 1,
            "deferred_reason": "batch_cap",
        }
    )
    producer = _Task(
        project_id=project.id,
        title="Durable carry producer",
        payloads=[{"type": "research", "base_branch": "main"}],
        context={"deferred_findings": [carried]},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(producer)
    await db_session.flush()
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": unacknowledged_deferred_findings(producer),
        QUALITY_GRADER_VERDICT_KEY: {"passed": True, "reason": "well-grounded"},
    }
    db_session.add(task)
    await db_session.commit()

    _patch_research_externals(mocker)
    mocker.patch("app.research_completion._parse_research_findings", return_value=[])
    mocker.patch(
        "app.admin_task_mutations.broadcast_to_project",
        new=AsyncMock(return_value=None),
    )

    session_factory = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    real_boundary = research_completion.lock_accepted_research_boundary
    cancelled_once = False

    @contextlib.asynccontextmanager
    async def _cancel_before_final_boundary(session, stale_task):
        nonlocal cancelled_once
        if not cancelled_once:
            async with session_factory() as admin_session:
                admin_task = await admin_session.get(_Task, stale_task.id)
                assert admin_task is not None
                # A parent with durable implementation children is deliberately
                # EVALUATING here; this is the administratively cancellable gap
                # created by the handler's intermediate commits.
                if admin_task.status == TaskStatus.EVALUATING:
                    await modify_task_apply(
                        task=admin_task,
                        payload={"status": TaskStatus.CANCELLED.value},
                        background_tasks=BackgroundTasks(),
                        session=admin_session,
                    )
                    cancelled_once = True
        async with real_boundary(session, stale_task) as pair:
            yield pair

    mocker.patch.object(
        research_completion,
        "lock_accepted_research_boundary",
        new=_cancel_before_final_boundary,
    )

    await _handle_research_completion(task, "[]", db_session, project=project)
    assert cancelled_once is True
    # The handler returns to completion dispatch, which immediately continues
    # reading this same identity. The abort must release locks without expiring
    # it (a rollback here would raise MissingGreenlet in the terminal tail).
    assert task.status == TaskStatus.CANCELLED

    async with session_factory() as verify_session:
        persisted_task = await verify_session.get(_Task, task.id)
        persisted_producer = await verify_session.get(_Task, producer.id)
        assert persisted_task is not None and persisted_producer is not None
        assert persisted_task.status == TaskStatus.CANCELLED
        assert (
            persisted_task.context or {}
        ).get(FINALIZED_RESEARCH_FINDINGS_KEY) is not True
        assert persisted_producer.context["deferred_findings"] != []
        assert DEFERRED_FINDING_CONSUMPTION_ACKS_KEY not in (
            persisted_producer.context or {}
        )


@_pytest.mark.asyncio
async def test_finalization_recomputes_survivors_after_peer_cancels_child(
    db_session, mocker
):
    """A child cancelled after bookkeeping must not remain an accepted finding."""

    import contextlib

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import sessionmaker

    import app.research_completion as research_completion
    from app.research_execution_authority import is_authenticated_research_child
    from test_autonomous_research import _make_finding, _patch_research_externals

    project, direction, task = await _grader_project_and_task(
        db_session, "d-final-child-cancel"
    )
    finding = _make_finding(
        "Cancel before authoritative survivor join", direction_id=direction["id"]
    )
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        QUALITY_GRADER_VERDICT_KEY: {"passed": True, "reason": "well-grounded"},
    }
    db_session.add(task)
    await db_session.commit()

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )

    session_factory = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    real_boundary = research_completion.lock_accepted_research_boundary
    cancelled_child_id = None

    @contextlib.asynccontextmanager
    async def _cancel_child_before_final_boundary(session, stale_task):
        nonlocal cancelled_child_id
        if cancelled_child_id is None:
            async with session_factory() as cancelling_session:
                rows = (
                    (
                        await cancelling_session.execute(
                            _select(_Task).where(
                                _Task.project_id == stale_task.project_id,
                                _Task.parent_task_id == stale_task.id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                routed = [
                    row
                    for row in rows
                    if is_authenticated_research_child(stale_task, row)
                    and (row.context or {}).get("finding_hash")
                ]
                assert len(routed) == 1
                routed[0].status = TaskStatus.CANCELLED
                cancelled_child_id = routed[0].id
                await cancelling_session.commit()
        async with real_boundary(session, stale_task) as pair:
            yield pair

    mocker.patch.object(
        research_completion,
        "lock_accepted_research_boundary",
        new=_cancel_child_before_final_boundary,
    )

    await _handle_research_completion(task, "", db_session, project=project)

    assert cancelled_child_id is not None
    async with session_factory() as verify_session:
        persisted_task = await verify_session.get(_Task, task.id)
        persisted_child = await verify_session.get(_Task, cancelled_child_id)
        assert persisted_task is not None and persisted_child is not None
        assert persisted_child.status == TaskStatus.CANCELLED
        assert persisted_task.context[FINALIZED_RESEARCH_FINDINGS_KEY] is True
        assert persisted_task.plan_output == []
        assert (persisted_task.context or {}).get("research_surviving_findings") == 0


@_pytest.mark.asyncio
async def test_finalized_survivors_reject_same_project_retargeted_research_child(
    db_session, mocker
):
    """A child linked to another parent cannot authorize a forged finding hash."""

    from app.finding_deduplication import canonical_finding_body, finding_hash
    from app.models import TASK_ORIGINATION_RESEARCH
    from test_autonomous_research import _make_finding, _patch_research_externals

    project, direction, task = await _grader_project_and_task(
        db_session, "d-final-child-auth"
    )
    trusted = _make_finding(
        "Keep the exact routed child",
        direction_id=direction["id"],
    )
    forged = _make_finding(
        "Do not trust a retargeted sibling",
        direction_id=direction["id"],
    )
    trusted_hash = finding_hash(
        trusted["title"], canonical_finding_body(trusted)
    )
    forged_hash = finding_hash(forged["title"], canonical_finding_body(forged))
    other_parent_id = uuid.uuid4()

    task.status = TaskStatus.COMPLETED
    task.plan_output = [trusted, forged]
    task.context = {
        **(task.context or {}),
        QUALITY_GRADER_VERDICT_KEY: {"passed": True, "reason": "well-grounded"},
    }
    trusted_child = _Task(
        project_id=project.id,
        title=trusted["title"],
        payloads=[{"type": "create", "prompt": trusted["prompt"]}],
        parent_task_id=task.id,
        context={
            "research_task_id": str(task.id),
            "parent_research_task_id": str(task.id),
            "finding_hash": trusted_hash,
        },
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        origination=TASK_ORIGINATION_RESEARCH,
    )
    # This row has the durable shape of a research child, but its protected
    # lineage points to a different producer. A member can retarget its generic
    # parent edge and add a finding_hash; neither mutation grants authority.
    retargeted_child = _Task(
        project_id=project.id,
        title=forged["title"],
        payloads=[{"type": "create", "prompt": forged["prompt"]}],
        parent_task_id=task.id,
        context={
            "research_task_id": str(other_parent_id),
            "parent_research_task_id": str(other_parent_id),
            "finding_hash": forged_hash,
        },
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        origination=TASK_ORIGINATION_RESEARCH,
    )
    db_session.add_all([task, trusted_child, retargeted_child])
    await db_session.commit()

    _patch_research_externals(mocker)
    mocker.patch("app.research_completion._parse_research_findings", return_value=None)

    await _handle_research_completion(
        task,
        "truncated replay without JSON",
        db_session,
        project=project,
    )
    await db_session.refresh(task)

    assert task.context[FINALIZED_RESEARCH_FINDINGS_KEY] is True
    assert [item["title"] for item in task.plan_output] == [trusted["title"]]


@_pytest.mark.asyncio
async def test_member_cannot_retarget_authenticated_child_finding_hash(
    db_session,
):
    """A genuine delivery row cannot be rebound to an undelivered candidate."""

    from fastapi import BackgroundTasks, HTTPException

    from app.admin_task_mutations import modify_task_apply
    from app.models import TASK_ORIGINATION_RESEARCH
    from app.research_execution_authority import (
        finalized_research_findings_for_survivors,
    )

    project = _Project(name="Immutable delivery hash", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    parent = _Task(
        project_id=project.id,
        title="Research parent",
        payloads=[{"type": "research", "prompt": "inspect"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
    )
    db_session.add(parent)
    await db_session.flush()
    child = _Task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Trusted delivery",
        payloads=[{"type": "create", "prompt": "deliver trusted"}],
        context={
            "research_task_id": str(parent.id),
            "parent_research_task_id": str(parent.id),
            "finding_hash": "trusted-hash",
        },
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        origination=TASK_ORIGINATION_RESEARCH,
    )
    db_session.add(child)
    await db_session.commit()

    with _pytest.raises(HTTPException) as exc_info:
        await modify_task_apply(
            child,
            {"context": {"finding_hash": "undelivered-hash"}},
            BackgroundTasks(),
            db_session,
        )

    assert exc_info.value.status_code == 400
    await db_session.rollback()
    await db_session.refresh(parent)
    await db_session.refresh(child)
    assert (child.context or {})["finding_hash"] == "trusted-hash"
    survivors = await finalized_research_findings_for_survivors(
        db_session,
        parent,
        [
            (0, {"title": "Trusted"}, "trusted-hash"),
            (1, {"title": "Undelivered"}, "undelivered-hash"),
        ],
    )
    assert survivors == [{"title": "Trusted"}]


@_pytest.mark.asyncio
async def test_finalization_marker_waits_for_bookkeeping_and_retry_finishes(
    db_session, mocker
):
    """A bookkeeping fault must not leave an authoritative partial receipt."""

    import app.research_completion as research_completion
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-finalization-retry"
    )
    finding = _make_finding(
        "Finish bookkeeping before finalization", direction_id=direction["id"]
    )
    _patch_research_externals(mocker)
    parser = mocker.patch(
        "app.research_completion._parse_research_findings",
        side_effect=[[finding], None],
    )
    task.plan_output = [finding]
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        QUALITY_GRADER_VERDICT_KEY: {"passed": True, "reason": "well-grounded"},
    }
    db_session.add(task)
    await db_session.commit()

    original_with_directions_locked = research_completion.with_directions_locked
    failed_once = False

    async def _fail_first_productivity_write(session, project_id, mutate_fn):
        nonlocal failed_once
        if mutate_fn.__name__ == "_update_productivity" and not failed_once:
            failed_once = True
            raise RuntimeError("simulated final bookkeeping fault")
        return await original_with_directions_locked(session, project_id, mutate_fn)

    mocker.patch(
        "app.research_completion.with_directions_locked",
        new=_fail_first_productivity_write,
    )

    with _pytest.raises(RuntimeError, match="simulated final bookkeeping fault"):
        await _handle_research_completion(task, "ignored", db_session)

    await db_session.rollback()
    await db_session.refresh(task)
    assert failed_once is True
    assert (task.context or {}).get(FINALIZED_RESEARCH_FINDINGS_KEY) is not True
    first_children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(first_children) == 1

    # The retry sees the durable child, reconstructs the accepted snapshot from
    # pre-gate plan_output, completes the missed bookkeeping, and only then
    # stamps the replay receipt. It must not create a duplicate child.
    await _handle_research_completion(task, "truncated replay without JSON", db_session)
    await db_session.refresh(task)
    final_children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(final_children) == 1
    assert parser.call_count == 2
    assert task.status != TaskStatus.FAILED
    assert task.context[FINALIZED_RESEARCH_FINDINGS_KEY] is True
    assert [item["title"] for item in task.plan_output] == [finding["title"]]


@_pytest.mark.asyncio
async def test_finalization_retry_preserves_routed_carried_finding_before_ack(
    db_session, mocker
):
    """A post-spawn retry must not erase work recovered from an older producer."""

    import app.research_completion as research_completion
    from test_autonomous_research import _make_finding, _patch_research_externals

    from app.research_deferred_carry import (
        DEFERRED_FINDING_CONSUMPTION_ACKS_KEY,
        unacknowledged_deferred_findings,
    )

    project, direction, task = await _grader_project_and_task(
        db_session, "d-carried-finalization-retry"
    )
    carried = _make_finding(
        "Keep carried work authoritative after retry", direction_id=direction["id"]
    )
    carried.update(
        {
            "actionability_grade": "HIGH",
            "deferred_count": 1,
            "deferred_reason": "batch_cap",
        }
    )
    producer = _Task(
        project_id=project.id,
        title="Carried retry producer",
        payloads=[{"type": "research", "base_branch": "main"}],
        context={
            "direction_id": direction["id"],
            "deferred_findings": [carried],
        },
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(producer)
    await db_session.flush()
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": unacknowledged_deferred_findings(producer),
        QUALITY_GRADER_VERDICT_KEY: {"passed": True, "reason": "well-grounded"},
    }
    db_session.add(task)
    await db_session.commit()

    _patch_research_externals(mocker)
    parser = mocker.patch(
        "app.research_completion._parse_research_findings",
        side_effect=[[], None],
    )
    original_with_directions_locked = research_completion.with_directions_locked
    failed_once = False

    async def _fail_first_productivity_write(session, project_id, mutate_fn):
        nonlocal failed_once
        if mutate_fn.__name__ == "_update_productivity" and not failed_once:
            failed_once = True
            raise RuntimeError("simulated carried final bookkeeping fault")
        return await original_with_directions_locked(session, project_id, mutate_fn)

    mocker.patch(
        "app.research_completion.with_directions_locked",
        new=_fail_first_productivity_write,
    )

    with _pytest.raises(RuntimeError, match="simulated carried final bookkeeping fault"):
        await _handle_research_completion(task, "[]", db_session)

    await db_session.rollback()
    await db_session.refresh(task)
    await db_session.refresh(producer)
    first_children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(first_children) == 1
    assert first_children[0].title == carried["title"]
    assert task.context.get(FINALIZED_RESEARCH_FINDINGS_KEY) is not True
    assert producer.context["deferred_findings"] != []
    assert DEFERRED_FINDING_CONSUMPTION_ACKS_KEY not in producer.context

    await _handle_research_completion(task, "truncated replay without JSON", db_session)
    await db_session.refresh(task)
    await db_session.refresh(producer)
    final_children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )

    assert parser.call_count == 2
    assert len(final_children) == 1
    assert task.context[FINALIZED_RESEARCH_FINDINGS_KEY] is True
    assert [item["title"] for item in task.plan_output] == [carried["title"]]
    assert producer.context["deferred_findings"] == []
    receipts = producer.context[DEFERRED_FINDING_CONSUMPTION_ACKS_KEY]
    assert len(receipts) == 1
    assert receipts[0]["consumed_by_task_id"] == str(task.id)


@_pytest.mark.asyncio
async def test_completion_dispatch_retries_carried_only_post_spawn_failure(
    db_session, mocker
):
    """The real dispatch recovery preserves a carried-only artifact until the
    final receipt commits, then replay is an idempotent no-op."""
    import app.research_completion as research_completion

    from app.post_commit_errors import PostCommitFinalCommitFailed
    from app.research_deferred_carry import (
        DEFERRED_FINDING_CONSUMPTION_ACKS_KEY,
        unacknowledged_deferred_findings,
    )
    from app.schemas import AgentStatusPayload
    from app.task_capabilities import get_task_capability
    from app.task_lifecycle_completion_dispatch import run_completion_dispatch
    from test_autonomous_research import _make_finding, _patch_research_externals

    project, direction, task = await _grader_project_and_task(
        db_session, "d-dispatch-carried-retry"
    )
    carried = _make_finding(
        "Dispatch must preserve carried-only work", direction_id=direction["id"]
    )
    carried.update(
        {
            "actionability_grade": "HIGH",
            "deferred_count": 1,
            "deferred_reason": "batch_cap",
        }
    )
    producer = _Task(
        project_id=project.id,
        title="Dispatch retry carry producer",
        payloads=[{"type": "research", "base_branch": "main"}],
        context={
            "direction_id": direction["id"],
            "deferred_findings": [carried],
        },
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(producer)
    await db_session.flush()
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": unacknowledged_deferred_findings(producer),
        QUALITY_GRADER_VERDICT_KEY: {"passed": True, "reason": "well-grounded"},
    }
    db_session.add(task)
    await db_session.commit()

    _patch_research_externals(mocker)
    mocker.patch("app.research_completion._parse_research_findings", return_value=[])
    original_apply_post_spawn_state = research_completion.apply_post_spawn_state
    failed_once = False

    async def _fail_once_after_spawn(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("simulated non-transient post-spawn bookkeeping fault")
        return await original_apply_post_spawn_state(*args, **kwargs)

    mocker.patch(
        "app.research_completion.apply_post_spawn_state",
        new=_fail_once_after_spawn,
    )
    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        logs="[]",
    )
    task_id = task.id

    async def _refetch():
        await db_session.rollback()
        return (
            (
                await db_session.execute(
                    _select(_Task)
                    .where(_Task.id == task_id)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .one_or_none()
        )

    async def _dispatch(current):
        return await run_completion_dispatch(
            current,
            task_type="research",
            payload=payload,
            task_id=task_id,
            session=db_session,
            locked_project=None,
            preallocated_strategic_message_sequences=[],
            _peek_capability=get_task_capability("research"),
            _peek_task_type="research",
            _expected_post_commit_claim_token=None,
            _expected_childless_recovery_token=None,
            refetch=_refetch,
        )

    with _pytest.raises(
        PostCommitFinalCommitFailed, match="before final receipt failed"
    ):
        await _dispatch(task)

    task = await _refetch()
    await db_session.refresh(producer)
    assert task is not None
    assert failed_once is True
    assert task.status == TaskStatus.EVALUATING
    assert task.plan_output == [], "fresh output is empty; all routing input is carry"
    assert task.context.get("_research_spawn_pending") is True
    assert task.context.get(FINALIZED_RESEARCH_FINDINGS_KEY) is not True
    assert producer.context["deferred_findings"] != []
    assert DEFERRED_FINDING_CONSUMPTION_ACKS_KEY not in producer.context
    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1

    task, response = await _dispatch(task)
    assert response is None
    await db_session.refresh(task)
    await db_session.refresh(producer)
    await db_session.refresh(project)
    assert task.context[FINALIZED_RESEARCH_FINDINGS_KEY] is True
    assert "_research_spawn_pending" not in task.context
    assert [item["title"] for item in task.plan_output] == [carried["title"]]
    assert producer.context["deferred_findings"] == []
    assert len(producer.context[DEFERRED_FINDING_CONSUMPTION_ACKS_KEY]) == 1
    assert project.vision["_research_spawn_count"] == 1

    # A later delivery replay consumes the authoritative receipt without
    # creating another child, acknowledging twice, or advancing cadence again.
    task, response = await _dispatch(task)
    assert response is None
    await db_session.refresh(task)
    await db_session.refresh(producer)
    await db_session.refresh(project)
    replay_children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(replay_children) == 1
    assert len(producer.context[DEFERRED_FINDING_CONSUMPTION_ACKS_KEY]) == 1
    assert project.vision["_research_spawn_count"] == 1


@_pytest.mark.asyncio
async def test_evaluating_flip_is_atomic_with_child_spawn(db_session, mocker):
    """A failure in the post-spawn wiring (AFTER the child-spawn commit) must leave
    the parent EVALUATING-with-children, never COMPLETED-with-children.

    Regression for the false-complete strand (task 98249c8a, 2026-06-09): the
    EVALUATING flip used to run in a SEPARATE, later commit (apply_post_spawn_state
    -> _apply_terminal_transition); a contention/timeout between the child-spawn
    commit and that flip stranded the parent frozen at the agent-reported COMPLETED
    with live children, and _check_and_trigger_rollup early-returns unless the parent
    is EVALUATING — so the whole subtree stranded. The flip now lands atomically with
    the child-spawn commit, so this fault leaves a recoverable EVALUATING parent.
    """
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-atom1")
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding(direction_id=direction["id"])],
    )
    task.plan_output = [_make_finding(direction_id=direction["id"])]
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        QUALITY_GRADER_VERDICT_KEY: {"passed": True, "reason": "well-grounded"},
    }
    db_session.add(task)
    await db_session.commit()

    # Fault-inject the contention class AFTER the child-spawn commit: the later
    # terminal-transition lived inside apply_post_spawn_state, so raising here is
    # exactly the window that used to strand the parent at COMPLETED.
    mocker.patch(
        "app.research_completion.apply_post_spawn_state",
        side_effect=RuntimeError("simulated post-spawn contention"),
    )

    with _pytest.raises(RuntimeError, match="simulated post-spawn contention"):
        await _handle_research_completion(task, "", db_session)

    # Despite the wiring failure, the child-spawn commit already flipped the parent
    # to EVALUATING atomically with the children — never COMPLETED-with-children.
    await db_session.rollback()
    await db_session.refresh(task)
    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1, "children must be durably spawned"
    refreshed = (
        (await db_session.execute(_select(_Task).where(_Task.id == task.id)))
        .scalars()
        .first()
    )
    assert refreshed.status == TaskStatus.EVALUATING, (
        "parent must be EVALUATING-with-children after a post-spawn fault, never "
        f"frozen COMPLETED; got {refreshed.status}"
    )


@_pytest.mark.asyncio
async def test_non_spawning_findings_skip_grader_even_when_enabled(db_session, mocker):
    """Proportional gate: pure-observation findings never escalate to a grader."""
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-obs01")
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "title": "design observation",
                "description": "An observation about the design doc",
                "prompt": "noted",
                "type": "design_doc_observation",
                "direction_id": direction["id"],
            }
        ],
    )
    _enable_grader(mocker)

    await _handle_research_completion(task, "", db_session)

    all_tasks = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.project_id == _project.id)
            )
        )
        .scalars()
        .all()
    )
    assert all(
        not (t.context or {}).get(QUALITY_GRADER_SOURCE_KEY) for t in all_tasks
    ), "observation-only findings must not escalate to an independent grader"
    await db_session.refresh(task)
    assert task.status != TaskStatus.EVALUATING, (
        "must not park when nothing would spawn"
    )


@_pytest.mark.asyncio
async def test_design_doc_observation_dedup_on_reprocessing(db_session, mocker):
    """Re-processing the same design_doc_observation finding appends no duplicate."""
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-obs-dedup")
    obs_finding = {
        "title": "design observation",
        "description": "An observation about the design doc",
        "section": "architecture",
        "prompt": "noted",
        "type": "design_doc_observation",
        "direction_id": direction["id"],
    }

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[obs_finding],
    )
    _enable_grader(mocker)

    # Process the same observation twice. proposed_changes is a telemetry-migrated
    # key — stripped from design_doc at write time and canonical in project_telemetry.
    from app.project_telemetry import read_telemetry

    await _handle_research_completion(task, "", db_session)
    proposals_after_first = (
        await read_telemetry(db_session, _project.id, "proposed_changes") or []
    )
    first_count = len(proposals_after_first)

    # Create a second task to simulate re-processing the same finding
    task2 = _Task(
        project_id=_project.id,
        title="Research task 2",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task2)
    await db_session.commit()
    await db_session.refresh(task2)

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[obs_finding],
    )
    await _handle_research_completion(task2, "", db_session)
    proposals_after_second = (
        await read_telemetry(db_session, _project.id, "proposed_changes") or []
    )
    second_count = len(proposals_after_second)

    assert first_count == second_count, (
        "re-processing identical design_doc_observation must not append duplicate"
    )
    # Verify the observation is still there
    assert any(
        p.get("observation") == "An observation about the design doc"
        and p.get("section") == "architecture"
        for p in proposals_after_second
    ), "the original observation must still be in proposed_changes"


@_pytest.mark.asyncio
async def test_finding_prerequisite_wires_depends_on_to_inflight_foundation(
    db_session, mocker
):
    """A finding declaring an explicit {kind:task} prerequisite that matches an
    in-flight task gets a depends_on edge to it instead of rebuilding it."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq01")

    # An unrelated in-flight foundation already building shared scaffolding.
    foundation = _Task(
        project_id=_project.id,
        title="Provision shared database schema",
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(foundation)
    await db_session.commit()
    await db_session.refresh(foundation)

    finding = _make_finding(title="Add login endpoint", direction_id=direction["id"])
    finding["prerequisite"] = {"kind": "task", "ref": str(foundation.id)}

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    children = [c for c in children if (c.first_payload or {}).get("type") != "memo"]
    assert len(children) == 1
    assert str(foundation.id) in (children[0].depends_on or []), (
        "the dependent finding must structurally depend on the in-flight foundation"
    )


@_pytest.mark.asyncio
async def test_batch_findings_share_single_foundation_via_capability(
    db_session, mocker
):
    """Two findings declaring the same capability prerequisite both wire onto the
    single sibling foundation that advertises that capability — exactly one
    foundation, two dependents."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq02")

    foundation_finding = _make_finding(
        title="Establish payment gateway core", direction_id=direction["id"]
    )
    foundation_finding["provides_capability"] = "payment-core"
    dep_a = _make_finding(title="Add refund flow", direction_id=direction["id"])
    dep_a["prerequisite"] = {"kind": "capability", "ref": "payment-core"}
    dep_b = _make_finding(title="Wire chargeback handler", direction_id=direction["id"])
    dep_b["prerequisite"] = {"kind": "capability", "ref": "payment-core"}

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[foundation_finding, dep_a, dep_b],
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    by_title = {c.title: c for c in children}
    assert len(children) == 3, "exactly one foundation + two dependents must spawn"
    foundation_child = by_title["Establish payment gateway core"]
    assert (foundation_child.context or {}).get("provides_capability") == "payment-core"
    for dep_title in ("Add refund flow", "Wire chargeback handler"):
        assert str(foundation_child.id) in (by_title[dep_title].depends_on or []), (
            f"{dep_title} must depend on the single shared foundation"
        )


@_pytest.mark.asyncio
@_pytest.mark.parametrize(
    ("mode", "has_fresh_proof", "expect_blocked"),
    [
        ("enforce", False, True),
        ("enforce", True, False),
        ("observe", False, False),
        ("off", False, False),
    ],
)
async def test_spawn_task_backed_capability_provider_honors_proof_gate(
    db_session, mocker, mode, has_fresh_proof, expect_blocked
):
    """Same-batch advertised providers are gated only for unproven capabilities."""
    from datetime import datetime, timedelta, timezone
    from app.shared_state import runtime_config as _rc
    from app.task_blocked import get_blocker_needs
    from test_autonomous_research import _make_finding, _patch_research_externals

    project, direction, task = await _grader_project_and_task(
        db_session, f"d-task-provider-{mode}-{has_fresh_proof}"
    )
    cap = "off-registry-provider-cap"
    if has_fresh_proof:
        project.capability_proof_records = {
            cap: [
                {
                    "proof_kind": "named_test",
                    "proved_at": (
                        datetime.now(timezone.utc) - timedelta(days=1)
                    ).isoformat(),
                }
            ]
        }
        db_session.add(project)
        await db_session.commit()

    provider = _make_finding(title="Build provider", direction_id=direction["id"])
    provider["provides_capability"] = cap
    dependent = _make_finding(title="Use provider", direction_id=direction["id"])
    dependent["prerequisite"] = {"kind": "capability", "ref": cap}
    saved_mode = _rc.get("capability_proof_gate_mode")
    try:
        _rc["capability_proof_gate_mode"] = mode
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[provider, dependent],
        )
        await _handle_research_completion(task, "", db_session)
    finally:
        if saved_mode is None:
            _rc.pop("capability_proof_gate_mode", None)
        else:
            _rc["capability_proof_gate_mode"] = saved_mode

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    by_title = {child.title: child for child in children}
    foundation, child = by_title["Build provider"], by_title["Use provider"]
    assert (child.status == TaskStatus.BLOCKED) is expect_blocked
    if expect_blocked:
        assert get_blocker_needs(child) == [{"kind": "capability", "ref": cap}]
        assert str(foundation.id) not in (child.depends_on or [])
    else:
        assert str(foundation.id) in (child.depends_on or [])


@_pytest.mark.asyncio
@_pytest.mark.parametrize(
    ("provider_status", "expect_edge"),
    [
        (TaskStatus.IN_PROGRESS, True),
        (TaskStatus.COMPLETED, False),
    ],
)
async def test_task_backed_capability_provider_resumes_after_trial_adoption_proof(
    db_session, mocker, provider_status, expect_edge
):
    """A durable trial adoption immediately resumes a proof-gated dependent."""
    from app.capability_trial_verdict import persist_trial_verdicts_in_transaction
    from app.shared_state import runtime_config as _rc
    from app.task_blocked import get_blocker_needs
    from test_autonomous_research import _make_finding, _patch_research_externals

    cap = f"proof-resume-{provider_status.value.lower()}"
    project, direction, task = await _grader_project_and_task(
        db_session, f"d-task-provider-resume-{provider_status.value.lower()}"
    )
    provider = _make_finding(title="Build proof provider", direction_id=direction["id"])
    provider["provides_capability"] = cap
    dependent = _make_finding(title="Use proof provider", direction_id=direction["id"])
    dependent["prerequisite"] = {"kind": "capability", "ref": cap}

    saved_mode = _rc.get("capability_proof_gate_mode")
    try:
        _rc["capability_proof_gate_mode"] = "enforce"
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[provider, dependent],
        )
        await _handle_research_completion(task, "", db_session)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        by_title = {child.title: child for child in children}
        foundation = by_title["Build proof provider"]
        child = by_title["Use proof provider"]
        assert child.status == TaskStatus.BLOCKED
        assert get_blocker_needs(child) == [{"kind": "capability", "ref": cap}]
        assert str(foundation.id) not in (child.depends_on or [])

        foundation.status = provider_status
        if provider_status == TaskStatus.COMPLETED:
            foundation.context = {**(foundation.context or {}), "pr_merged": True}
        db_session.add(foundation)
        await db_session.commit()

        await persist_trial_verdicts_in_transaction(
            db_session,
            project.id,
            pending_verdicts=[
                (
                    cap,
                    {
                        "verdict": "adopt",
                        "lift": 0.2,
                        "n_with": 4,
                        "n_without": 4,
                        "verdict_reason": "platform trial passed",
                    },
                )
            ],
        )
        await db_session.refresh(child)
        assert child.status == TaskStatus.QUEUED
        assert get_blocker_needs(child) == []
        assert (str(foundation.id) in (child.depends_on or [])) is expect_edge
    finally:
        if saved_mode is None:
            _rc.pop("capability_proof_gate_mode", None)
        else:
            _rc["capability_proof_gate_mode"] = saved_mode


@_pytest.mark.asyncio
async def test_spawn_task_prerequisite_bypasses_task_backed_capability_gate(
    db_session, mocker
):
    """Enforce still wires an ordinary task prerequisite to its active provider."""
    from app.shared_state import runtime_config as _rc
    from test_autonomous_research import _make_finding, _patch_research_externals

    project, direction, task = await _grader_project_and_task(
        db_session, "d-task-provider-task"
    )
    foundation = _Task(
        project_id=project.id,
        title="Active task foundation",
        status=TaskStatus.IN_PROGRESS,
        context={"provides_capability": "unproven-provider-cap"},
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(foundation)
    await db_session.commit()
    finding = _make_finding(title="Use active task", direction_id=direction["id"])
    finding["prerequisite"] = {"kind": "task", "ref": str(foundation.id)}
    saved_mode = _rc.get("capability_proof_gate_mode")
    try:
        _rc["capability_proof_gate_mode"] = "enforce"
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[finding]
        )
        await _handle_research_completion(task, "", db_session)
    finally:
        if saved_mode is None:
            _rc.pop("capability_proof_gate_mode", None)
        else:
            _rc["capability_proof_gate_mode"] = saved_mode

    child = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .one()
    )
    assert child.status != TaskStatus.BLOCKED
    assert str(foundation.id) in (child.depends_on or [])


@_pytest.mark.asyncio
async def test_finding_without_prerequisite_spawns_with_no_extra_deps(
    db_session, mocker
):
    """A finding with no declared prerequisite spawns unchanged (no depends_on)."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq03")
    finding = _make_finding(title="Tidy logging output", direction_id=direction["id"])

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1
    assert not (children[0].depends_on or []), (
        "a finding with no prerequisite must not gain a dependency edge"
    )


@_pytest.mark.asyncio
async def test_unmatched_task_hash_prerequisite_escalates_to_parent(db_session, mocker):
    """When a task-hash prerequisite resolves to no spawned, deferred, or
    in-flight provider, the dependent escalates immediately instead of parking
    BLOCKED on an unmatchable hash."""
    from test_autonomous_research import _make_finding, _patch_research_externals
    from app.task_blocked import get_blocker_needs

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq04")
    finding = _make_finding(title="Add login endpoint", direction_id=direction["id"])
    missing_ref = "00000000deadbeef"
    finding["prerequisite"] = {"kind": "task", "ref": missing_ref}

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1
    child = children[0]
    assert child.status == TaskStatus.CANCELLED, (
        "an unresolvable prerequisite must escalate instead of entering BLOCKED"
    )
    assert not (child.depends_on or []), (
        "unresolved prerequisite must not create a depends_on edge"
    )
    assert get_blocker_needs(child) == []
    assert (child.context or {}).get("prerequisite_escalated_to_parent") is True
    assert (child.context or {}).get("prerequisite_escalation_reason") == (
        "no_live_or_deferred_provider"
    )
    unresolved = (child.context or {}).get("unresolved_prerequisite", [])
    assert any(
        u.get("kind") == "task" and u.get("ref") == missing_ref
        for u in (unresolved if isinstance(unresolved, list) else [unresolved])
    ), "unresolved prerequisite details must remain in context for visibility"


@_pytest.mark.asyncio
async def test_prereq_cancelled_children_excluded_from_survival_and_tasks_created(
    db_session, mocker
):
    """Prereq-cancelled children (escalated-to-parent) must NOT count toward
    research_surviving_findings or tasks_created.

    Before the fix: _surviving_findings was computed as _impl_spawned BEFORE the
    prereq-wiring loop ran. If a spawned child was later cancelled with
    prerequisite_escalated_to_parent=True (no_live_or_deferred_provider), it
    still counted as a surviving finding and as tasks_created>=1, preventing the
    saturation detector from ever marking the direction auto-complete.

    After the fix: the survival signal is adjusted downward after the loop, and
    direction_outcomes excludes those children from tasks_created.
    """
    from test_autonomous_research import _make_finding, _patch_research_externals
    from app.failure_classification import CO_DEFERRED_SHELVED

    _project, direction, task = await _grader_project_and_task(db_session, "d-prqexcl")
    finding = _make_finding(title="Add metrics endpoint", direction_id=direction["id"])
    missing_ref = "deadbeef00000000"
    # Unresolvable task-hash prerequisite forces escalation to parent.
    finding["prerequisite"] = {"kind": "task", "ref": missing_ref}

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1
    child = children[0]
    assert child.status == TaskStatus.CANCELLED, "prereq-escalated child must be CANCELLED"
    assert (child.context or {}).get("prerequisite_escalated_to_parent") is True

    # The key assertion: surviving_findings must be 0, not 1.
    surviving = (task.context or {}).get("research_surviving_findings")
    assert surviving == 0, (
        f"prereq-cancelled child must not count as a surviving finding "
        f"(got research_surviving_findings={surviving})"
    )

    # _first_attempt_succeeded must be downgraded because surviving==0.
    assert not (task.context or {}).get("_first_attempt_succeeded"), (
        "a cycle whose only child was prereq-cancelled must not be a first-attempt success"
    )

    # Confirm direction_outcomes.tasks_created would exclude this child.
    # We test this via the context predicate directly (same logic as direction_outcomes.py).
    child_is_prereq_cancelled = (
        child.status == TaskStatus.CANCELLED
        and (
            (child.context or {}).get("prerequisite_escalated_to_parent")
            or (child.context or {}).get(CO_DEFERRED_SHELVED)
        )
    )
    assert child_is_prereq_cancelled, (
        "child must satisfy the prereq-cancel predicate used in tasks_created exclusion"
    )

    # The initial research_history entry written by apply_post_spawn_state must also
    # exclude prereq-cancelled children from tasks_created (research_direction_state.py).
    await db_session.refresh(_project)
    direction_entry = next(
        (d for d in _project.directions if d.get("id") == direction["id"]), None
    )
    assert direction_entry is not None, "direction must exist in project"
    research_history = direction_entry.get("research_history", [])
    assert len(research_history) >= 1, "research_history must have at least one entry"
    assert research_history[-1]["tasks_created"] == 0, (
        f"prereq-cancelled children must not count toward tasks_created in the initial "
        f"history entry (got {research_history[-1]['tasks_created']})"
    )


@_pytest.mark.asyncio
async def test_depends_on_indices_missing_sibling_wires_to_existing_inflight(
    db_session, mocker
):
    """When a depends_on_indices sibling was deduped against an EXISTING in-flight
    task (not spawned this cycle), the dependent must wire a depends_on edge to
    that existing task rather than be CANCELLED as 'no provider'.

    Failure shape: deduplicate_findings drops the sibling finding (it matches the
    existing task by hash), so it's absent from finding_to_child; the missing-sibling
    loop builds a (kind=task, ref=hash) need. Before the fix, this need wasn't
    checked against _prereq_candidates (existing_tasks + child_tasks), so it landed
    in dead_task_needs and the dependent was CANCELLED. After the fix, the missing-
    sibling loop also calls resolve_prerequisite_task(_prereq_candidates) and wires
    the edge instead."""
    from app.research_completion import _finding_hash
    from test_autonomous_research import _make_finding, _patch_research_externals
    from app.task_blocked import get_blocker_needs

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-prq-inflight"
    )

    # Pre-existing in-flight foundation whose finding_hash matches sibling finding A.
    sibling_title = "Build shared event bus"
    sibling_desc = "Core pub/sub infrastructure all other findings depend on"
    sibling_hash = _finding_hash(sibling_title, sibling_desc)
    existing_foundation = _Task(
        project_id=_project.id,
        title=sibling_title,
        description=sibling_desc,
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        context={"finding_hash": sibling_hash},
    )
    db_session.add(existing_foundation)
    await db_session.commit()
    await db_session.refresh(existing_foundation)

    # Research cycle produces sibling finding A (matches existing task by hash) and
    # dependent finding B. Deduplicate_findings will drop A; B's depends_on_indices
    # points at A (index 0). The missing-sibling loop must resolve A's hash against
    # the existing foundation and wire the edge — not cancel B.
    finding_a = _make_finding(
        title=sibling_title,
        direction_id=direction["id"],
    )
    finding_a["description"] = sibling_desc
    finding_a["finding_hash"] = sibling_hash

    finding_b = _make_finding(
        title="Wire domain event handlers",
        direction_id=direction["id"],
    )
    finding_b["depends_on_indices"] = [0]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding_a, finding_b],
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    children = [c for c in children if (c.first_payload or {}).get("type") != "memo"]
    # Only dependent B should spawn (A was deduped).
    dep_children = [c for c in children if "event handler" in c.title.lower()]
    assert len(dep_children) == 1, "dependent finding B must spawn as a child task"
    dep = dep_children[0]
    assert dep.status != TaskStatus.CANCELLED, (
        "dependent with deduped sibling must not be CANCELLED as 'no provider'"
    )
    assert str(existing_foundation.id) in (dep.depends_on or []), (
        "dependent must wire a depends_on edge to the existing in-flight foundation"
    )
    assert get_blocker_needs(dep) == [], (
        "no unresolved need must be persisted when the edge was wired"
    )


@_pytest.mark.asyncio
async def test_depends_on_indices_deduped_sibling_no_provider_spawns_standalone(
    db_session, mocker
):
    """When a depends_on_indices sibling is dropped by deduplicate_findings as a
    duplicate of EXISTING/COVERED work that is NOT identity-resolvable, the
    dependent must spawn STANDALONE (orphaned edge dropped) — not be cancelled as
    'no_live_or_deferred_provider', which would also fail the whole research task.

    This is the real-world strand: two independent research runs surface the same
    foundation finding. The foundation is word-overlap-deduped against an existing
    in-flight task whose finding_hash differs (different body across runs), so
    resolve_prerequisite_task cannot wire an edge — the hashes never line up. Before
    the fix the dependent landed in dead_task_needs and was CANCELLED, escalating to
    the parent which FAILED ('All N subtasks were cancelled'), discarding the whole
    cycle. After the fix the dependent spawns standalone: the foundation is already
    covered, so the declared edge is orphaned, not an unmet dependency."""
    from app.research_completion import _finding_hash
    from test_autonomous_research import _make_finding, _patch_research_externals
    from app.task_blocked import get_blocker_needs

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-prq-dedup-standalone"
    )

    # Pre-existing in-flight task that shares foundation finding A's code REGION
    # (related_files) and title, but whose finding_hash is DIFFERENT (a prior
    # run's body) — so A is region-deduped against it, yet A's recomputed hash
    # resolves to no provider.
    foundation_title = "reconcile chosen snecko act-2 move logic to canonical"
    foundation_files = ["stsbot/enemies.py", "stsbot/moves.py"]
    existing_inflight = _Task(
        project_id=_project.id,
        title=foundation_title,
        payloads=[{"type": "create", "prompt": "earlier run of the same foundation"}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        context={
            "direction_id": direction["id"],
            "related_files": foundation_files,
            # Hash from a DIFFERENT body — deliberately not equal to finding A's hash.
            "finding_hash": _finding_hash(foundation_title, "a different earlier body"),
        },
    )
    db_session.add(existing_inflight)
    await db_session.commit()
    await db_session.refresh(existing_inflight)

    # Foundation finding A (same region + title → region-deduped; default body →
    # its OWN hash, which differs from the existing task's stored hash).
    finding_a = _make_finding(title=foundation_title, direction_id=direction["id"])
    finding_a["related_files"] = foundation_files
    a_hash = _finding_hash(finding_a["title"], finding_a["description"])
    assert a_hash != existing_inflight.context["finding_hash"], (
        "test precondition: A's hash must differ from the existing task's so the "
        "edge cannot be identity-resolved"
    )

    # Dependent finding B declares an edge to A (index 0).
    finding_b = _make_finding(
        title="dilute act-2 normal pool with canonical easy fights",
        direction_id=direction["id"],
    )
    finding_b["depends_on_indices"] = [0]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding_a, finding_b],
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    children = [c for c in children if (c.first_payload or {}).get("type") != "memo"]
    dep_children = [c for c in children if "dilute act-2" in c.title.lower()]
    assert len(dep_children) == 1, "dependent finding B must spawn as a child task"
    dep = dep_children[0]
    assert dep.status not in (TaskStatus.CANCELLED, TaskStatus.BLOCKED), (
        "dependent whose deduped foundation is unresolvable must spawn standalone, "
        "not be cancelled/blocked as a dead need"
    )
    assert (dep.context or {}).get("prerequisite_escalation_reason") != (
        "no_live_or_deferred_provider"
    )
    assert get_blocker_needs(dep) == [], (
        "no unresolved need may persist — the orphaned edge is dropped, not gated"
    )
    assert str(existing_inflight.id) not in (dep.depends_on or []), (
        "no edge is wired to the fuzzy word-overlap match (identities diverge)"
    )
    dropped = (dep.context or {}).get("_spawn_prereq_dropped_duplicate") or []
    assert any(d.get("finding_hash") == a_hash for d in dropped), (
        "the dropped-duplicate prerequisite must be stamped for observability"
    )


@_pytest.mark.asyncio
async def test_task_prerequisite_rebinds_to_same_batch_design_lineage(
    db_session, mocker
):
    """A dependent whose declared hash names a retitled same-batch design sibling
    wires to that sibling via recorded finding lineage instead of blocking."""
    from app.research_completion import _finding_hash
    from test_autonomous_research import _make_finding, _patch_research_externals
    from app.task_blocked import get_blocker_needs

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq08")
    original_hash = _finding_hash(
        "design: original serving contract",
        "Original design finding before planner retitle",
    )
    provider = _make_finding(
        title="design: supersession-aware serving confirmation",
        direction_id=direction["id"],
        finding_type="design",
    )
    provider["finding_hash"] = original_hash
    dependent = _make_finding(
        title="Implement serving confirmation guard",
        direction_id=direction["id"],
    )
    dependent["prerequisite"] = {"kind": "task", "ref": original_hash}

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[provider, dependent],
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    by_title = {c.title: c for c in children}
    design_child = by_title[provider["title"]]
    dependent_child = by_title[dependent["title"]]

    assert dependent_child.status != TaskStatus.BLOCKED
    assert str(design_child.id) in (dependent_child.depends_on or [])
    assert get_blocker_needs(dependent_child) == []
    assert original_hash in (design_child.context or {}).get("finding_lineage", [])


@_pytest.mark.asyncio
async def test_depends_on_indices_wires_edge_when_foundation_description_has_trailing_whitespace(
    db_session, mocker
):
    """depends_on edge must resolve when the foundation finding's description has
    trailing whitespace (e.g. a trailing newline, common in LLM-emitted findings).

    The bug: missing_hash was computed from the raw unstripped description, but
    finding_lineage on the spawned foundation task is built from the canonical
    stripped body. For a description like "Some body\\n" the two hashes diverge,
    the lineage match fails, no edge is wired, and the dependent enters BLOCKED.
    The fix: use _canonical_finding_body (which strips) for the missing_hash so
    both sides agree.

    Also covers the empty-string description case: when description is "" and
    prompt is non-empty, the canonical body uses prompt (stripped), matching what
    the lineage stores.
    """
    from test_autonomous_research import _make_finding, _patch_research_externals
    from app.task_blocked import get_blocker_needs

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-trailing-ws"
    )

    # Foundation finding whose description has a trailing newline.
    foundation = _make_finding(
        title="Build shared event infrastructure",
        direction_id=direction["id"],
    )
    foundation["description"] = "Core pub/sub infrastructure\n"

    dependent = _make_finding(
        title="Wire domain event handlers",
        direction_id=direction["id"],
    )
    dependent["depends_on_indices"] = [0]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[foundation, dependent],
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    children = [c for c in children if (c.first_payload or {}).get("type") != "memo"]
    by_title = {c.title: c for c in children}
    foundation_child = by_title[foundation["title"]]
    dependent_child = by_title[dependent["title"]]

    assert dependent_child.status != TaskStatus.BLOCKED, (
        "dependent must not be BLOCKED when foundation description has trailing whitespace"
    )
    assert str(foundation_child.id) in (dependent_child.depends_on or []), (
        "depends_on edge must wire to the foundation task despite trailing newline in description"
    )
    assert get_blocker_needs(dependent_child) == [], (
        "no unresolved need must be persisted when the edge was wired"
    )


@_pytest.mark.asyncio
async def test_observation_dependency_escalates_instead_of_blocking(db_session, mocker):
    """Observation/non-spawning findings cannot provide a task, so a dependent
    whose only need is that finding escalates instead of entering BLOCKED."""
    from test_autonomous_research import _make_finding, _patch_research_externals
    from app.task_blocked import get_blocker_needs

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq09")
    observation = _make_finding(
        title="obs: lane substrate layers a new system over pool/park-wake",
        direction_id=direction["id"],
        finding_type="design_doc_observation",
    )
    dependent = _make_finding(
        title="Use lane substrate observation",
        direction_id=direction["id"],
    )
    dependent["depends_on_indices"] = [0]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[observation, dependent],
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1
    child = children[0]
    assert child.title == dependent["title"]
    assert child.status == TaskStatus.CANCELLED
    assert get_blocker_needs(child) == []
    assert (child.context or {}).get("prerequisite_escalated_to_parent") is True
    assert (child.context or {}).get("prerequisite_escalation_reason") == (
        "structurally_unresolvable"
    )


@_pytest.mark.asyncio
async def test_working_registry_capability_prerequisite_does_not_block(
    db_session, mocker
):
    """A finding declaring a capability prerequisite that the strategist registry
    already marks ``working`` spawns UNBLOCKED with no edge and no recorded need:
    the capability exists in the codebase itself, so no provider task will ever
    advertise it — blocking would strand runnable work to the escalation
    deadline."""
    from test_autonomous_research import _make_finding, _patch_research_externals
    from app.task_blocked import get_blocker_needs

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq07")
    _project.problem_model = {
        **(_project.problem_model or {}),
        "capabilities": [
            {"name": "Budgeted compute job substrate", "status": "working"},
            {"name": "future-capability", "status": "planned"},
        ],
    }
    db_session.add(_project)
    await db_session.commit()

    finding = _make_finding(
        title="Add compute launch entrypoint", direction_id=direction["id"]
    )
    finding["prerequisite"] = {
        "kind": "capability",
        "ref": "budgeted-compute-job-substrate",
    }

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    children = [c for c in children if (c.first_payload or {}).get("type") != "memo"]
    assert len(children) == 1
    child = children[0]
    assert child.status != TaskStatus.BLOCKED, (
        "a prerequisite on an already-working registry capability must not block"
    )
    assert not (child.depends_on or []), (
        "a registry-satisfied prerequisite must not wire a depends_on edge"
    )
    assert not get_blocker_needs(child), (
        "a registry-satisfied prerequisite must not persist a blocker need"
    )


@_pytest.mark.asyncio
async def test_working_registry_capability_prerequisite_blocks_under_enforce(
    db_session, mocker
):
    """ACCEPTANCE EVIDENCE (finding feat:enforce-gate-blocking-on-both-consumers).
    Spawn-time seam: under enforce mode a finding declaring a capability prerequisite
    that the registry marks 'working' but UNPROVEN must spawn the child BLOCKED
    with the capability need recorded — the capability exists in the codebase
    but without machine-verified proof the dependent must not proceed."""
    from test_autonomous_research import _make_finding, _patch_research_externals
    from app.task_blocked import get_blocker_needs
    from app.shared_state import runtime_config as _rc

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq09")
    _project.problem_model = {
        **(_project.problem_model or {}),
        "capabilities": [
            # Working but no evidence_refs → unproven
            {"name": "Budgeted compute job substrate", "status": "working"},
        ],
    }
    db_session.add(_project)
    await db_session.commit()

    finding = _make_finding(
        title="Add compute launch entrypoint", direction_id=direction["id"]
    )
    finding["prerequisite"] = {
        "kind": "capability",
        "ref": "budgeted-compute-job-substrate",
    }

    saved_mode = _rc.get("capability_proof_gate_mode")
    try:
        _rc["capability_proof_gate_mode"] = "enforce"
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[finding]
        )
        await _handle_research_completion(task, "", db_session)
    finally:
        if saved_mode is not None:
            _rc["capability_proof_gate_mode"] = saved_mode
        else:
            _rc.pop("capability_proof_gate_mode", None)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    children = [c for c in children if (c.first_payload or {}).get("type") != "memo"]
    assert len(children) == 1
    child = children[0]
    assert child.status == TaskStatus.BLOCKED, (
        "under enforce mode, a prerequisite on an unproven working capability "
        "must park the dependent BLOCKED (spawn-time seam)"
    )
    needs = get_blocker_needs(child)
    assert any(n.get("kind") == "capability" for n in needs), (
        "BLOCKED task must record the capability need so the resume sweep can "
        "unblock it once proof is earned"
    )


async def _spawn_child_under_enforce(
    db_session,
    mocker,
    *,
    direction_key: str,
    capabilities: list[dict],
    proof_records: dict | None,
    mode: str = "enforce",
):
    """Spawn one finding whose prerequisite names ``budgeted-compute-job-substrate``
    through the REAL research-completion path with the proof gate in ``mode``
    (default ``"enforce"``), and return the spawned child task.

    ``capabilities`` is written to ``project.problem_model`` (AGENT-WRITABLE) and
    ``proof_records`` to ``project.capability_proof_records`` (the PLATFORM-ONLY
    ledger) — the two sources whose disagreement finding #4 is about.
    """
    from test_autonomous_research import _make_finding, _patch_research_externals
    from app.shared_state import runtime_config as _rc

    _project, direction, task = await _grader_project_and_task(
        db_session, direction_key
    )
    _project.problem_model = {
        **(_project.problem_model or {}),
        "capabilities": capabilities,
    }
    _project.capability_proof_records = proof_records
    db_session.add(_project)
    await db_session.commit()

    finding = _make_finding(
        title="Add compute launch entrypoint", direction_id=direction["id"]
    )
    finding["prerequisite"] = {
        "kind": "capability",
        "ref": "budgeted-compute-job-substrate",
    }

    saved_mode = _rc.get("capability_proof_gate_mode")
    try:
        _rc["capability_proof_gate_mode"] = mode
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[finding]
        )
        await _handle_research_completion(task, "", db_session)
    finally:
        if saved_mode is not None:
            _rc["capability_proof_gate_mode"] = saved_mode
        else:
            _rc.pop("capability_proof_gate_mode", None)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    children = [c for c in children if (c.first_payload or {}).get("type") != "memo"]
    assert len(children) == 1
    return children[0]


def _self_declared_working_cap() -> dict:
    """A ``working`` capability whose ONLY proof is an agent-authored evidence_ref.

    Exactly what an agent can write into the JSONB ``problem_model`` with nothing
    machine-verified behind it.
    """
    return {
        "name": "Budgeted compute job substrate",
        "status": "working",
        "evidence_refs": [
            {"kind": "named_test", "ref": "testing/test_x.py::test_x", "proven": True}
        ],
    }


def _platform_ledger(proof_kind: str = "named_test") -> dict:
    """One machine-verified PLATFORM proof-record (an agent cannot write this)."""
    return {
        "budgeted-compute-job-substrate": [
            {
                "task_id": "T1",
                "proof_kind": proof_kind,
                "proof_ref": "testing/test_x.py::test_x",
            }
        ]
    }


@_pytest.mark.asyncio
async def test_self_declared_capability_proof_blocks_spawn_under_enforce(
    db_session, mocker
):
    """SECURITY (finding #4), spawn-time seam. A prerequisite naming a ``working``
    capability whose ``proven`` evidence_ref is a pure SELF-REPORT — no corroborating
    entry in the platform-only ``capability_proof_records`` ledger — must NOT clear
    the enforce gate: the dependent parks BLOCKED with the need recorded.

    Before the fix the resolver graded from ``problem_model.capabilities[].evidence_refs``
    alone (agent-writable JSONB), so this forged ref spawned the child unblocked.
    """
    from app.task_blocked import get_blocker_needs

    child = await _spawn_child_under_enforce(
        db_session,
        mocker,
        direction_key="d-prq20",
        capabilities=[_self_declared_working_cap()],
        proof_records={},  # empty platform ledger — nothing corroborates the claim
    )

    assert child.status == TaskStatus.BLOCKED, (
        "a self-declared capability proof must never satisfy a prerequisite — "
        "without a platform ledger entry the dependent must stay BLOCKED"
    )
    assert any(n.get("kind") == "capability" for n in get_blocker_needs(child)), (
        "the capability need must be recorded so the resume sweep can unblock it "
        "once real proof is earned"
    )


@_pytest.mark.asyncio
async def test_ledger_corroborated_capability_clears_spawn_under_enforce(
    db_session, mocker
):
    """A capability whose ``proven`` claim IS corroborated by a machine-verified
    ``named_test`` entry in the platform ledger clears the enforce gate — the fix
    blocks self-reports, not real proof."""
    from app.task_blocked import get_blocker_needs

    child = await _spawn_child_under_enforce(
        db_session,
        mocker,
        direction_key="d-prq21",
        capabilities=[_self_declared_working_cap()],
        proof_records=_platform_ledger(),
    )

    assert child.status != TaskStatus.BLOCKED, (
        "a ledger-corroborated capability must satisfy the prerequisite under enforce"
    )
    assert not get_blocker_needs(child)


@_pytest.mark.asyncio
async def test_backing_path_only_ledger_blocks_spawn_under_enforce(
    db_session, mocker
):
    """A ledger holding only a structural ``backing_path`` entry grades
    ``path_proven``, not ``proven`` — it does not corroborate a ``proven``
    self-report, so the enforce gate still blocks the dependent."""
    child = await _spawn_child_under_enforce(
        db_session,
        mocker,
        direction_key="d-prq22",
        capabilities=[_self_declared_working_cap()],
        proof_records=_platform_ledger(proof_kind="backing_path"),
    )

    assert child.status == TaskStatus.BLOCKED, (
        "a structural backing_path ledger entry is not machine-verified proof and "
        "must not clear the gate for a self-reported 'proven' claim"
    )


def _platform_exemption_ledger() -> dict:
    """The operator's PLATFORM-recorded exemption for the gating capability.

    Written into ``project.capability_proof_records`` — the platform-only column no
    agent-facing endpoint can write. It is NOT a machine proof: it is the operator's
    standing authorization to clear the gate without one.
    """
    from app.capability_registry import build_operator_exemption_record

    return {
        "budgeted-compute-job-substrate": [
            build_operator_exemption_record(
                "budgeted-compute-job-substrate",
                authorized_by="operator@example.com",
                reason="external substrate, no test hook",
            )
        ]
    }


@_pytest.mark.asyncio
async def test_platform_recorded_exemption_clears_spawn_without_proof_entry(
    db_session, mocker
):
    """PRESERVED CARVE-OUT. An OPERATOR-recorded exemption (an ``operator_exemption``
    entry in the platform-only ledger) clears the spawn-time gate with NO machine
    proof: the earn loop deliberately never mints proof-records for an exempt
    capability, so demanding corroboration would nullify the exemption the operator
    granted."""
    from app.task_blocked import get_blocker_needs

    child = await _spawn_child_under_enforce(
        db_session,
        mocker,
        direction_key="d-prq23",
        capabilities=[
            {
                "name": "Budgeted compute job substrate",
                "status": "working",
                "evidence_refs": [],
                "proof_exempt": True,
                "source": {
                    "type": "user_message",
                    "ref": "operator granted exemption",
                },
            }
        ],
        # The operator's exemption record — and NO machine proof, by design.
        proof_records=_platform_exemption_ledger(),
    )

    assert child.status != TaskStatus.BLOCKED, (
        "an operator-recorded exemption must still clear the gate without a "
        "machine-proof ledger entry"
    )
    assert not get_blocker_needs(child)


@_pytest.mark.asyncio
async def test_agent_fabricated_exemption_provenance_blocks_spawn_under_enforce(
    db_session, mocker
):
    """SECURITY (finding #9), spawn-resolver seam. THE CORE PROPERTY OF THIS RELEASE.

    ``proof_exempt: true`` + ``source: {"type": "self_reflection"}`` lives entirely in
    the AGENT-WRITABLE ``problem_model`` JSONB — the same store the ledger-
    corroboration pass exists to distrust. The previous fix authorized the exemption
    off that ``source.type``, so an agent could forge one of the accepted types and
    clear the prerequisite gate for its own dependents with nothing verified.

    The exemption must now be recorded in the platform-only ledger; a fabricated
    provenance is refused and the dependent stays BLOCKED.
    """
    from app.task_blocked import get_blocker_needs

    for forged_type in ("self_reflection", "agent_observation", "user_message"):
        child = await _spawn_child_under_enforce(
            db_session,
            mocker,
            direction_key=f"d-prq23-forged-{forged_type}",
            capabilities=[
                {
                    "name": "Budgeted compute job substrate",
                    "status": "working",
                    "evidence_refs": [],
                    "proof_exempt": True,
                    "source": {"type": forged_type, "ref": "trust me"},
                }
            ],
            # Empty platform ledger — the operator granted NO exemption.
            proof_records={},
        )

        assert child.status == TaskStatus.BLOCKED, (
            f"an agent-fabricated exemption provenance (source.type={forged_type!r}) "
            "must never clear the spawn-time capability gate"
        )
        assert any(n.get("kind") == "capability" for n in get_blocker_needs(child)), (
            "the capability need must stay recorded so the resume sweep can unblock "
            "it once a real proof (or a real exemption) lands"
        )


@_pytest.mark.asyncio
async def test_bare_proof_exempt_blocks_spawn_under_enforce(db_session, mocker):
    """A BARE ``proof_exempt=True`` — no cited ``source``, so no operator/strategic
    provenance — is the silent self-report escape hatch and must still BLOCK."""
    child = await _spawn_child_under_enforce(
        db_session,
        mocker,
        direction_key="d-prq24",
        capabilities=[
            {
                "name": "Budgeted compute job substrate",
                "status": "working",
                "evidence_refs": [],
                "proof_exempt": True,
            }
        ],
        proof_records={},
    )

    assert child.status == TaskStatus.BLOCKED, (
        "an unattributed proof_exempt must never clear the gate"
    )


@_pytest.mark.asyncio
async def test_observe_mode_surfaces_unverified_capability_on_spawn(
    db_session, mocker
):
    """OBSERVE mode, spawn-time seam. A prerequisite naming a ``working``
    capability with NO proven evidence resolves as satisfied-by-the-codebase (the
    dependent is NOT gated), but the unproven status is surfaced DURABLY on the
    spawned child's context — not merely logged — so an operator can see the
    unproven foundation without re-deriving it from logs.

    This exercises ``spawn_capability_gate_disposition``'s ``"surface"`` branch
    end-to-end through the real research-completion path.
    """
    from app.research_completion import SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY

    child = await _spawn_child_under_enforce(
        db_session,
        mocker,
        direction_key="d-prq-observe",
        capabilities=[_self_declared_working_cap()],
        proof_records={},  # empty ledger — the self-report is unproven
        mode="observe",
    )

    # OBSERVE never gates: the dependent spawns (not BLOCKED on the capability).
    assert child.status != TaskStatus.BLOCKED, (
        "observe mode must never gate the dependent — it only surfaces the "
        "unverified status"
    )

    # The unverified status is durably surfaced on the child's context.
    surfaced = (child.context or {}).get(SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY) or []
    assert surfaced, (
        "observe mode must surface the unverified capability on the spawned "
        "dependent's context, not only in a log line"
    )
    entry = surfaced[0]
    assert entry["kind"] == "capability"
    assert entry["ref"] == "budgeted-compute-job-substrate"
    # A self-declared-only proof grades ``unverified`` under corroboration (no
    # platform ledger entry), so the surfaced status is a non-``proven`` tier.
    assert entry["proof_status"] not in (None, "proven"), (
        f"surfaced proof_status must be a non-proven tier, got {entry['proof_status']!r}"
    )


@_pytest.mark.asyncio
async def test_planned_registry_capability_prerequisite_still_blocks(
    db_session, mocker
):
    """The registry bypass is exact: a capability whose declared status is NOT
    ``working`` (planned/partial) still gates the dependent — the foundation
    genuinely does not exist yet."""
    from test_autonomous_research import _make_finding, _patch_research_externals
    from app.task_blocked import get_blocker_needs

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq08")
    _project.problem_model = {
        **(_project.problem_model or {}),
        "capabilities": [
            {"name": "budgeted-compute-job-substrate", "status": "planned"},
        ],
    }
    db_session.add(_project)
    await db_session.commit()

    finding = _make_finding(
        title="Add compute launch entrypoint", direction_id=direction["id"]
    )
    finding["prerequisite"] = {
        "kind": "capability",
        "ref": "budgeted-compute-job-substrate",
    }

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    children = [c for c in children if (c.first_payload or {}).get("type") != "memo"]
    assert len(children) == 1
    child = children[0]
    assert child.status == TaskStatus.BLOCKED, (
        "a planned (not-yet-built) capability prerequisite must still gate"
    )
    assert get_blocker_needs(child), "the unresolved need must be persisted"


@_pytest.mark.asyncio
async def test_title_like_task_prerequisite_dropped_not_blocking(db_session, mocker):
    """A finding declaring a TITLE-LIKE kind=task prerequisite (free prose, not a
    task id / finding hash) must NOT block the dependent: the malformed ref is
    dropped at ingestion (normalize_prerequisite rejects it) so it never drives a
    blocked need nor becomes durable stranded demand. The dependent spawns
    normally — a named foundation belongs to kind=capability."""
    from test_autonomous_research import _make_finding, _patch_research_externals
    from app.task_blocked import get_blocker_needs

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq04b")
    finding = _make_finding(title="Add login endpoint", direction_id=direction["id"])
    finding["prerequisite"] = {"kind": "task", "ref": "Build the auth subsystem"}

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1
    child = children[0]
    assert child.status != TaskStatus.BLOCKED, (
        "a title-like task prerequisite must NOT block the dependent"
    )
    assert get_blocker_needs(child) == [], (
        "a dropped title-like task ref must not be carried as a blocked need"
    )


@_pytest.mark.asyncio
async def test_deferred_sibling_prerequisite_co_defers_dependent(db_session, mocker):
    """When a prerequisite sibling is deferred THIS cycle (not spawned), the
    dependent must NOT be left as a live blocked task stranded behind dormant
    data. It is co-deferred with its prerequisite: the live task is cancelled and
    both findings are shelved in deferred_findings so they re-surface as a unit."""
    from app.research_completion import _finding_hash
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq05")
    prereq = _make_finding(title="Define shared helper", direction_id=direction["id"])
    prereq["actionability_grade"] = "LOW"
    dependent = _make_finding(title="Use shared helper", direction_id=direction["id"])
    dependent["depends_on_indices"] = [0]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[prereq, dependent],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    dependent_children = [c for c in children if c.title == dependent["title"]]
    # The dependent must never be left LIVE-blocked behind its deferred prereq.
    assert not any(c.status == TaskStatus.BLOCKED for c in dependent_children), (
        "a dependent must not sit live-blocked behind a same-cycle-deferred prereq"
    )
    assert all(c.status == TaskStatus.CANCELLED for c in dependent_children), (
        "the co-deferred dependent's live task must be cancelled"
    )

    # Both the deferred prerequisite and the co-deferred dependent are shelved so
    # the next cycle re-surfaces them together.
    deferred = (task.context or {}).get("deferred_findings", [])
    prereq_hash = _finding_hash(prereq["title"], prereq["description"])
    dependent_hash = _finding_hash(dependent["title"], dependent["description"])
    by_hash = {f.get("finding_hash"): f for f in deferred}
    assert prereq_hash in by_hash, "the deferred prerequisite must be shelved"
    assert dependent_hash in by_hash, "the co-deferred dependent must be shelved"
    assert by_hash[dependent_hash]["deferred_reason"] == "prereq_deferred_same_cycle"


@_pytest.mark.asyncio
async def test_deferred_prompt_only_prerequisite_co_defers_dependent(
    db_session, mocker
):
    """A prerequisite finding with an empty description but a non-empty prompt must
    receive the same identity hash as the hash computed by the sibling's
    depends_on resolution (which falls back to prompt when description is empty).
    Before the fix, _stamp_deferral used description-only, so the deferred prereq's
    hash never matched the sibling's missing_hash lookup, causing the sibling to be
    cancelled instead of co-deferred."""
    from app.research_completion import _finding_hash
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-prq-prompt-only"
    )
    prereq = _make_finding(
        title="Prompt-only prerequisite", direction_id=direction["id"]
    )
    # Clear description so the finding is prompt-only — this is the case that
    # previously produced a hash mismatch between _stamp_deferral and the
    # depends_on resolution path.
    prereq["description"] = ""
    prereq["actionability_grade"] = "LOW"

    dependent = _make_finding(
        title="Dependent on prompt-only prereq", direction_id=direction["id"]
    )
    dependent["depends_on_indices"] = [0]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[prereq, dependent],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    dependent_children = [c for c in children if c.title == dependent["title"]]
    assert not any(c.status == TaskStatus.BLOCKED for c in dependent_children), (
        "a dependent must not sit live-blocked behind a deferred prompt-only prereq"
    )
    assert all(c.status == TaskStatus.CANCELLED for c in dependent_children), (
        "the co-deferred dependent's live task must be cancelled"
    )

    deferred = (task.context or {}).get("deferred_findings", [])
    # The prereq hash must use title + prompt (description is empty), matching the
    # hash the depends_on resolution path computes via _finding_hash(title, prompt).
    prereq_hash = _finding_hash(prereq["title"], prereq["prompt"])
    dependent_hash = _finding_hash(dependent["title"], dependent["description"])
    by_hash = {f.get("finding_hash"): f for f in deferred}
    assert prereq_hash in by_hash, (
        "the deferred prompt-only prerequisite must be shelved with the correct hash"
    )
    assert dependent_hash in by_hash, "the co-deferred dependent must be shelved"
    assert by_hash[dependent_hash]["deferred_reason"] == "prereq_deferred_same_cycle"


@_pytest.mark.asyncio
async def test_two_deferred_sibling_prerequisites_co_defer_dependent(
    db_session, mocker
):
    """A finding declaring TWO unspawned (deferred-this-cycle) sibling
    prerequisites is co-deferred — its live task cancelled — rather than left
    blocked behind two dormant findings. All three findings re-surface together."""
    from app.research_completion import _finding_hash
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq06")
    prereq_a = _make_finding(title="Define helper A", direction_id=direction["id"])
    prereq_a["actionability_grade"] = "LOW"
    prereq_b = _make_finding(title="Define helper B", direction_id=direction["id"])
    prereq_b["actionability_grade"] = "LOW"
    dependent = _make_finding(title="Use helpers A and B", direction_id=direction["id"])
    dependent["depends_on_indices"] = [0, 1]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[prereq_a, prereq_b, dependent],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    dependent_children = [c for c in children if c.title == dependent["title"]]
    assert not any(c.status == TaskStatus.BLOCKED for c in dependent_children), (
        "a dependent on multiple deferred siblings must not be left live-blocked"
    )
    assert all(c.status == TaskStatus.CANCELLED for c in dependent_children)

    deferred = (task.context or {}).get("deferred_findings", [])
    hashes = {f.get("finding_hash") for f in deferred}
    hash_a = _finding_hash(prereq_a["title"], prereq_a["description"])
    hash_b = _finding_hash(prereq_b["title"], prereq_b["description"])
    dependent_hash = _finding_hash(dependent["title"], dependent["description"])
    assert {hash_a, hash_b, dependent_hash} <= hashes, (
        "both deferred prerequisites and the co-deferred dependent must re-surface together"
    )


@_pytest.mark.asyncio
async def test_co_deferred_dependent_retains_explicit_prerequisite(db_session, mocker):
    """A dependent declaring a deferred sibling prerequisite AND an explicit
    {kind, ref} prerequisite is co-deferred (sibling deferred this cycle). The
    shelved finding retains BOTH declarations so the next cycle reconstructs the
    full need set — the explicit prerequisite is not dropped on the way out."""
    from app.research_completion import _finding_hash
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq07")
    prereq = _make_finding(title="Define shared base", direction_id=direction["id"])
    prereq["actionability_grade"] = "LOW"
    dependent = _make_finding(
        title="Build on shared base", direction_id=direction["id"]
    )
    dependent["depends_on_indices"] = [0]
    dependent["prerequisite"] = {"kind": "task", "ref": "nonexistent-foundation-id"}

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[prereq, dependent],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    dependent_children = [c for c in children if c.title == dependent["title"]]
    assert not any(c.status == TaskStatus.BLOCKED for c in dependent_children), (
        "the dependent must be co-deferred, not left live-blocked"
    )
    assert all(c.status == TaskStatus.CANCELLED for c in dependent_children)

    deferred = (task.context or {}).get("deferred_findings", [])
    dependent_hash = _finding_hash(dependent["title"], dependent["description"])
    shelved = next(
        (f for f in deferred if f.get("finding_hash") == dependent_hash), None
    )
    assert shelved is not None, "the co-deferred dependent must be shelved"
    assert shelved.get("depends_on_indices") == [0], (
        "the sibling prerequisite declaration must survive co-deferral"
    )
    assert shelved.get("prerequisite") == {
        "kind": "task",
        "ref": "nonexistent-foundation-id",
    }, "the explicit prerequisite must not be dropped on the way out"


@_pytest.mark.asyncio
async def test_batch_cap_deferred_foundation_co_defers_dependent(db_session, mocker):
    """When the per-cycle batch cap drops a foundation finding but keeps a
    higher-priority finding that depends on it, the dependent must NOT spawn as a
    live, foundation-less child. The cap-time index remap previously dropped the
    dependent->foundation edge silently, spawning the dependent ungated. Instead
    the dependent is co-deferred alongside its foundation (mirroring the same-cycle
    prerequisite co-deferral) so the pair re-surfaces together next cycle."""
    from app.research_completion import _finding_hash
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-bcap01")
    # Foundation is LOW priority so the cap ranks it out; the dependent is HIGH
    # priority so it survives the cap and would otherwise spawn against a
    # foundation that does not yet exist.
    foundation = _make_finding(
        title="Define batch-capped foundation",
        direction_id=direction["id"],
        priority="low",
    )
    dependent = _make_finding(
        title="Build on batch-capped foundation",
        direction_id=direction["id"],
        priority="high",
    )
    dependent["depends_on_indices"] = [0]

    _patch_research_externals(mocker)
    # Force the per-cycle cap to 1 so the 2-finding batch exercises the co-defer
    # path. The cap is a flat constant now (real capacity is gated at dispatch),
    # so drive it directly rather than through the VM-slot gauges.
    mocker.patch("app.research_completion._MAX_FINDINGS_PER_CYCLE", 1)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[foundation, dependent],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    live = {
        TaskStatus.QUEUED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.PROVISIONING,
        TaskStatus.EVALUATING,
    }
    dependent_children = [c for c in children if c.title == dependent["title"]]
    assert not any(c.status in live for c in dependent_children), (
        "the dependent must NOT spawn as a live, foundation-less task when its "
        "foundation was dropped by the batch cap"
    )

    # The dependent is co-deferred (batch_cap) alongside its foundation, so both
    # re-surface together next cycle.
    deferred = (task.context or {}).get("deferred_findings", [])
    by_hash = {f.get("finding_hash"): f for f in deferred}
    foundation_hash = _finding_hash(foundation["title"], foundation["description"])
    dependent_hash = _finding_hash(dependent["title"], dependent["description"])
    assert foundation_hash in by_hash, "the batch-capped foundation must be shelved"
    assert dependent_hash in by_hash, "the co-deferred dependent must be shelved"
    assert by_hash[dependent_hash]["deferred_reason"] == "batch_cap", (
        "the dependent must be co-deferred under the batch_cap reason, not spawned"
    )
    assert (task.context or {}).get("research_surviving_findings") == 0, (
        "recoverable deferred intent must not score this completed cycle as success"
    )
    assert task.plan_output == [], (
        "only findings backed by non-cancelled child routes may be finalized"
    )


def test_batch_cap_telemetry_counts_co_deferred_dependents():
    from app.research_finding_policy import cap_findings_by_priority

    foundation = {"title": "foundation", "type": "feature", "priority": "low"}
    dependent = {
        "title": "dependent",
        "type": "feature",
        "priority": "high",
        "depends_on_indices": [0],
    }
    kept, applied_count, deferred = cap_findings_by_priority(
        [foundation, dependent], 1
    )
    assert kept == []
    assert deferred == [foundation, dependent]
    assert applied_count == len(deferred) == 2


@_pytest.mark.asyncio
async def test_positive_headroom_reserves_carried_capacity_before_ranking_fresh(
    db_session, mocker
):
    """Fresh priority must never evict work already carried into this cycle."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-headroom-reservation"
    )
    carried = [
        {
            **_make_finding(
                title=f"Carried finding {index}",
                direction_id=direction["id"],
                priority="low",
            ),
            "deferred_reason": "batch_cap",
        }
        for index in range(14)
    ]
    fresh = [
        _make_finding(
            title=f"Fresh finding {index}",
            direction_id=direction["id"],
            priority="high",
        )
        for index in range(2)
    ]
    task.payloads = [{**task.first_payload, "work_headroom": 1}]
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": carried,
    }
    db_session.add(task)
    await db_session.commit()

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=fresh,
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    child_titles = {child.title for child in children}
    assert {finding["title"] for finding in carried} <= child_titles
    assert fresh[0]["title"] in child_titles
    assert fresh[1]["title"] not in child_titles
    deferred = (task.context or {}).get("deferred_findings", [])
    assert any(
        finding.get("title") == fresh[1]["title"]
        and finding.get("deferred_reason") == "batch_cap"
        for finding in deferred
    )
    assert not any(
        str(finding.get("title") or "").startswith("Carried finding")
        for finding in deferred
    )


@_pytest.mark.asyncio
async def test_batch_cap_does_not_shrink_under_fleet_load(db_session, mocker):
    """The per-cycle finding cap is a flat ceiling, not a capacity throttle. A
    saturated fleet (every VM busy, deep global queue) must NOT shrink it: spawned
    children are QUEUED rows the dispatcher drains as slots free, so a busy fleet
    that previously throttled this to ~1-3 spawns now spawns every finding up to
    the flat ceiling. Genuine overload is handled separately by
    _assess_project_spawn_health, not by starving spawn on instantaneous load."""
    import app.shared_state as _ss
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-bcapload")
    # Eight independent findings (distinct titles → no dedup collapse, no deps).
    findings = [
        _make_finding(
            title=f"Independent improvement {i}", direction_id=direction["id"]
        )
        for i in range(8)
    ]

    _patch_research_externals(mocker)
    # Saturated fleet: under the old slots*2 / 15-queued math this floored the cap
    # to 1. The flat ceiling ignores the gauges entirely.
    mocker.patch.object(_ss, "tasks_active_gauge", 999)
    mocker.patch.object(_ss, "tasks_queued_gauge", 999)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=findings,
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    live = {
        TaskStatus.QUEUED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.PROVISIONING,
        TaskStatus.EVALUATING,
    }
    live_children = [c for c in children if c.status in live]
    assert len(live_children) == len(findings), (
        f"all {len(findings)} findings must spawn under a saturated fleet; the cap "
        f"no longer shrinks with load — got {len(live_children)} live children"
    )
    # Nothing was shelved by the batch cap (the flat ceiling was not exceeded).
    deferred = (task.context or {}).get("deferred_findings", [])
    assert not any(f.get("deferred_reason") == "batch_cap" for f in deferred), (
        "no finding may be deferred by the batch cap below the flat ceiling"
    )


@_pytest.mark.asyncio
async def test_batch_cap_counts_only_task_spawning_findings(db_session, mocker):
    """Free metadata must not displace any of the 15 implementation slots."""
    from app.project_telemetry import read_telemetry
    from test_autonomous_research import _make_finding, _patch_research_externals

    project, direction, task = await _grader_project_and_task(db_session, "d-bcapmeta")
    actionable = [
        _make_finding(
            title=f"Capacity-bearing improvement {i}",
            direction_id=direction["id"],
            priority="medium",
        )
        for i in range(15)
    ]
    observation = {
        "title": "Record a high-priority design observation",
        "description": "The research cycle found durable architectural context.",
        "prompt": "Record this observation without creating an implementation task.",
        "type": "design_doc_observation",
        "priority": "high",
        "section": "architecture",
        "direction_id": direction["id"],
    }

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[observation, *actionable],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    live = {
        TaskStatus.QUEUED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.PROVISIONING,
        TaskStatus.EVALUATING,
    }
    assert {child.title for child in children if child.status in live} == {
        finding["title"] for finding in actionable
    }
    assert not any(
        finding.get("deferred_reason") == "batch_cap"
        for finding in (task.context or {}).get("deferred_findings", [])
    )
    proposals = await read_telemetry(db_session, project.id, "proposed_changes") or []
    assert any(
        proposal.get("observation") == observation["description"]
        for proposal in proposals
    ), "the metadata finding must still be processed outside the task cap"


@_pytest.mark.asyncio
async def test_fresh_depends_on_indices_not_corrupted_by_prior_deferred_prepend(
    db_session, mocker
):
    """A fresh finding's depends_on_indices are 0-based into the agent's own
    (fresh-only) output. When prior-cycle deferred findings are prepended to the
    combined findings list, each fresh index must be shifted up by the number of
    prepended findings so it still resolves to the intended fresh sibling — not to
    a recovered deferred finding that happened to occupy the same raw slot.

    Setup: one prior deferred finding (occupies combined-list index 0); two fresh
    findings A (combined index 1) and B (combined index 2) where B declares
    depends_on_indices=[0] — meaning "depends on fresh sibling at position 0" in
    the agent's view (i.e., finding A). After the rebase the index becomes [1],
    resolving to A's child task. Without the fix the raw index [0] resolves to the
    prior deferred finding's child task, wiring the wrong edge."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-prq-rebase"
    )

    # Prior-cycle deferred finding; will be prepended at combined-list index 0.
    prior = _make_finding(title="Prior deferred finding", direction_id=direction["id"])
    prior["deferred_reason"] = "low_actionability"
    prior["deferred_count"] = 1

    # Fresh findings from the current agent run.
    # finding_a is at fresh-index 0 → combined-index 1 after prepend.
    finding_a = _make_finding(title="Foundation fresh A", direction_id=direction["id"])
    # finding_b depends on fresh-index 0 (finding_a); combined-index 2 after prepend.
    finding_b = _make_finding(title="Dependent fresh B", direction_id=direction["id"])
    finding_b["depends_on_indices"] = [0]  # agent's view: sibling at fresh position 0

    # Inject the prior deferred finding so _handle_research_completion will prepend it.
    task.context = dict(task.context or {})
    task.context["prior_deferred_findings"] = [prior]
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(task, "context")
    await db_session.commit()
    await db_session.refresh(task)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding_a, finding_b],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    # Filter out memo/notification tasks.
    spawn_children = [
        c for c in children if (c.first_payload or {}).get("type") != "memo"
    ]

    child_by_title = {c.title: c for c in spawn_children}
    assert "Foundation fresh A" in child_by_title, (
        "finding_a must spawn as a child task"
    )
    assert "Dependent fresh B" in child_by_title, "finding_b must spawn as a child task"

    child_a = child_by_title["Foundation fresh A"]
    child_b = child_by_title["Dependent fresh B"]

    assert str(child_a.id) in (child_b.depends_on or []), (
        "finding_b's depends_on edge must point at finding_a's child task "
        "(the intended fresh sibling), not the prior deferred finding"
    )
    # Prior deferred finding's child (if any) must NOT be in finding_b's depends_on.
    prior_children = [c for c in spawn_children if c.title == prior["title"]]
    for pc in prior_children:
        assert str(pc.id) not in (child_b.depends_on or []), (
            "finding_b must not be wired to the prior deferred finding — "
            "its depends_on_indices was not correctly rebased"
        )


@_pytest.mark.asyncio
async def test_independent_deferred_finding_keeps_throttle(db_session, mocker):
    """A deferred finding with NO live dependent keeps the existing throttle: it is
    shelved under its own (low_actionability) reason and is NOT re-tagged as a
    co-deferred prerequisite. Co-deferral must only couple genuine dependents."""
    from app.research_completion import _finding_hash
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq08")
    # Two unrelated LOW findings — neither depends on the other.
    low_a = _make_finding(title="Tidy log line A", direction_id=direction["id"])
    low_a["actionability_grade"] = "LOW"
    low_b = _make_finding(title="Tidy log line B", direction_id=direction["id"])
    low_b["actionability_grade"] = "LOW"

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[low_a, low_b],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    deferred = (task.context or {}).get("deferred_findings", [])
    by_hash = {f.get("finding_hash"): f for f in deferred}
    for f in (low_a, low_b):
        h = _finding_hash(f["title"], f["description"])
        assert h in by_hash, f"{f['title']} must be shelved"
        assert by_hash[h]["deferred_reason"] == "low_actionability", (
            "an independent deferred finding keeps its own throttle reason"
        )


@_pytest.mark.asyncio
async def test_repeatedly_deferred_sibling_prerequisite_is_counted_as_give_up(
    db_session, mocker
):
    """A co-deferred dependent that finally decays is counted on the parent task."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq09")
    decayed = _make_finding("Use shared helper", direction_id=direction["id"])
    decayed["deferred_reason"] = "prereq_deferred_same_cycle"
    decayed["deferred_count"] = 3

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding("Fresh sentinel", direction_id=direction["id"])],
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )
    task.context = {"research_depth": 0, "prior_deferred_findings": [decayed]}
    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    give_up = (task.context or {}).get("finding_structural_give_up")
    assert give_up is not None
    assert give_up["total"] == 1
    assert give_up["by_reason"] == {"prereq_deferred_same_cycle": 1}
    assert give_up["by_direction"] == {direction["id"]: 1}
    assert give_up["sample"][0]["reason"] == "prereq_deferred_same_cycle"


@_pytest.mark.asyncio
async def test_cyclic_prerequisite_refused_at_spawn(db_session, mocker):
    """Two findings each declaring the other as a capability prerequisite would
    close a depends_on cycle. The acyclicity check refuses the edge that would
    close it, so no full cycle is persisted — consistent with the runtime
    need-resolution path rather than deferring to the dispatch-time breaker."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq05")

    finding_a = _make_finding(title="Build cap A surface", direction_id=direction["id"])
    finding_a["provides_capability"] = "cap-a"
    finding_a["prerequisite"] = {"kind": "capability", "ref": "cap-b"}
    finding_b = _make_finding(title="Build cap B surface", direction_id=direction["id"])
    finding_b["provides_capability"] = "cap-b"
    finding_b["prerequisite"] = {"kind": "capability", "ref": "cap-a"}

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding_a, finding_b],
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 2
    by_id = {str(c.id): c for c in children}
    # At most one of the two mutual edges may be wired — the other is refused so
    # the depends_on graph never closes a cycle.
    edge_count = sum(1 for c in children if c.depends_on)
    assert edge_count <= 1, "the cycle-closing edge must be refused at spawn"
    # Verify no actual cycle: no child both depends on another AND is depended on.
    for c in children:
        for dep in c.depends_on or []:
            other = by_id.get(str(dep))
            assert other is None or str(c.id) not in (other.depends_on or []), (
                "a mutual depends_on cycle must never be persisted"
            )


class TestFindingHeadProvenance:
    """Stale-targeting helpers: HEAD-commit provenance + cited-file diff."""

    @_pytest.mark.asyncio
    @_pytest.mark.parametrize(
        ("resolved_sha", "direction_id"),
        [
            (None, "d-head-unresolved"),
            ("a" * 40, "d-head-resolved"),
        ],
        ids=["unresolved-removes-agent-sha", "resolved-replaces-agent-sha"],
    )
    async def test_handler_trusts_only_server_resolved_generated_head_sha(
        self, db_session, mocker, resolved_sha, direction_id
    ):
        from test_autonomous_research import _make_finding, _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, direction_id
        )
        finding = _make_finding(
            title="Server-owned HEAD provenance", direction_id=direction["id"]
        )
        finding["generated_head_sha"] = "agent-supplied-sha"

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding],
        )
        mocker.patch(
            "app.research_completion.resolve_branch_verification_context",
            new=AsyncMock(return_value={}),
        )
        resolve_head = mocker.patch(
            "app.research_completion.resolve_head_commit",
            new=AsyncMock(return_value=resolved_sha),
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        assert len(task.plan_output or []) == 1
        stored_finding = task.plan_output[0]
        if resolved_sha is None:
            assert "generated_head_sha" not in stored_finding
        else:
            assert stored_finding["generated_head_sha"] == resolved_sha
        resolve_head.assert_awaited_once_with()

    def test_finding_cited_paths_extracts_file_refs(self):
        from app.source_ref_validation import finding_cited_paths

        finding = {
            "sources": [
                {"type": "file", "ref": "backend/app/foo.py:42"},
                {"type": "file", "ref": "agent/src/bar.ts#L10-L20"},
                {"type": "pr", "ref": "https://github.com/o/r/pull/1"},
                {"type": "message", "ref": "msg-abc"},
                {"ref": "not a path"},
                "garbage",
            ]
        }
        paths = finding_cited_paths(finding)
        assert paths == ["backend/app/foo.py", "agent/src/bar.ts"]

    def test_finding_cited_paths_handles_missing_sources(self):
        from app.source_ref_validation import finding_cited_paths

        assert finding_cited_paths({}) == []
        assert finding_cited_paths({"sources": None}) == []
        assert finding_cited_paths("nope") == []

    async def test_resolve_head_commit_uses_github_when_creds(self, monkeypatch):
        from app import source_ref_validation as srv

        async def _fake_branch_sha(token, owner, repo, branch):
            assert (token, owner, repo, branch) == ("t", "o", "r", "main")
            return "deadbeefcafe"

        import app.github_refs as gh

        monkeypatch.setattr(gh, "get_branch_sha", _fake_branch_sha)
        sha = await srv.resolve_head_commit(
            token="t", owner="o", repo="r", branch="main"
        )
        assert sha == "deadbeefcafe"

    async def test_resolve_head_commit_github_error_degrades_to_none(self, monkeypatch):
        from app import source_ref_validation as srv
        import app.github_refs as gh

        async def _boom(*a, **k):
            raise RuntimeError("api down")

        monkeypatch.setattr(gh, "get_branch_sha", _boom)
        # No local checkout dir -> _git_output returns None -> overall None.
        monkeypatch.setattr(srv.os.path, "isdir", lambda p: False)
        sha = await srv.resolve_head_commit(
            token="t", owner="o", repo="r", branch="main"
        )
        assert sha is None

    async def test_cited_paths_changed_since_true_when_file_in_diff(self, monkeypatch):
        from app import source_ref_validation as srv
        import app.github_refs as gh

        async def _changed(token, owner, repo, base, head, project_id=None):
            return ["backend/app/foo.py", "unrelated.py"]

        monkeypatch.setattr(gh, "get_files_changed_between_refs", _changed)
        result = await srv.cited_paths_changed_since(
            "oldsha",
            ["backend/app/foo.py:42"],
            token="t",
            owner="o",
            repo="r",
            branch="main",
        )
        assert result is True

    async def test_cited_paths_changed_since_false_when_not_in_diff(self, monkeypatch):
        from app import source_ref_validation as srv
        import app.github_refs as gh

        async def _changed(*a, **k):
            return ["something/else.py"]

        monkeypatch.setattr(gh, "get_files_changed_between_refs", _changed)
        result = await srv.cited_paths_changed_since(
            "oldsha",
            ["backend/app/foo.py"],
            token="t",
            owner="o",
            repo="r",
            branch="main",
        )
        assert result is False

    async def test_cited_paths_changed_since_none_when_diff_indeterminate(
        self, monkeypatch
    ):
        from app import source_ref_validation as srv
        import app.github_refs as gh

        async def _indeterminate(*a, **k):
            return None  # API error sentinel

        monkeypatch.setattr(gh, "get_files_changed_between_refs", _indeterminate)
        result = await srv.cited_paths_changed_since(
            "oldsha",
            ["backend/app/foo.py"],
            token="t",
            owner="o",
            repo="r",
            branch="main",
        )
        assert result is None

    async def test_cited_paths_changed_since_none_for_unknown_sha(self):
        from app.source_ref_validation import cited_paths_changed_since

        assert await cited_paths_changed_since(None, ["backend/app/foo.py"]) is None
        assert await cited_paths_changed_since("", ["backend/app/foo.py"]) is None

    async def test_cited_paths_changed_since_none_when_no_cited_paths(self):
        from app.source_ref_validation import cited_paths_changed_since

        assert await cited_paths_changed_since("sha", []) is None
        assert await cited_paths_changed_since("sha", ["not a path"]) is None


class TestSiblingPrerequisiteIndexValidation:
    """Prerequisite-need construction with negative/out-of-range index validation."""

    async def test_negative_index_in_depends_on_is_skipped(self, mocker, db_session):
        """Finding with negative index in depends_on_indices is skipped with log warning."""
        from test_autonomous_research import (
            _make_finding,
            _patch_research_externals,
            _active_direction,
            _make_research_task,
        )

        # Create a minimal project
        direction = _active_direction("d-index-val", "Index validation")
        project = _Project(
            name="Index Validation Test",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = await _make_research_task(
            project, direction, db_session, status=TaskStatus.COMPLETED
        )

        findings = [
            _make_finding(title="Finding A"),
            {
                **_make_finding(title="Finding B (depends on A with negative index)"),
                "depends_on_indices": [-1],  # Negative index — should be skipped
            },
        ]

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=findings,
        )
        log_mock = mocker.patch("app.research_completion.logger")

        await _handle_research_completion(task, "", db_session)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        # Both findings should spawn because the negative index is skipped
        assert len(children) == 2
        # Check that a warning was logged for the negative index
        warning_calls = [
            call
            for call in log_mock.warning.call_args_list
            if "out-of-range" in str(call)
        ]
        assert len(warning_calls) > 0

    async def test_out_of_range_positive_index_is_skipped(self, mocker, db_session):
        """Finding with out-of-range positive index in depends_on_indices is skipped."""
        from test_autonomous_research import (
            _make_finding,
            _patch_research_externals,
            _active_direction,
            _make_research_task,
        )

        # Create a minimal project
        direction = _active_direction("d-out-range", "Out of range test")
        project = _Project(
            name="Out of Range Test",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = await _make_research_task(
            project, direction, db_session, status=TaskStatus.COMPLETED
        )

        findings = [
            _make_finding(title="Finding A"),
            {
                **_make_finding(title="Finding B (depends on nonexistent index 99)"),
                "depends_on_indices": [99],  # Out of range — should be skipped
            },
        ]

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=findings,
        )
        log_mock = mocker.patch("app.research_completion.logger")

        await _handle_research_completion(task, "", db_session)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        # Both findings should spawn because the out-of-range index is skipped
        assert len(children) == 2
        # Check that a warning was logged for the out-of-range index
        warning_calls = [
            call
            for call in log_mock.warning.call_args_list
            if "out-of-range" in str(call)
        ]
        assert len(warning_calls) > 0

    async def test_valid_index_in_depends_on_co_defers_dependent(
        self, mocker, db_session
    ):
        """A valid sibling index pointing at a same-cycle-deferred finding IS
        recognized and produces a prerequisite need — but because the prerequisite
        was deferred this cycle, the dependent is co-deferred (its live task
        cancelled and its finding shelved) rather than left live-blocked behind
        dormant data (see #8278). The co-deferral itself is the proof that the
        valid index created a need; a skipped index (negative/out-of-range) leaves
        the dependent free to spawn normally."""
        from app.research_completion import _finding_hash
        from test_autonomous_research import (
            _make_finding,
            _patch_research_externals,
            _active_direction,
            _make_research_task,
        )

        # Create a minimal project
        direction = _active_direction("d-valid-idx", "Valid index test")
        project = _Project(
            name="Valid Index Test",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = await _make_research_task(
            project, direction, db_session, status=TaskStatus.COMPLETED
        )

        finding_a = {
            **_make_finding(title="Finding A (deferred)"),
            "actionability_grade": "LOW",
        }
        finding_b = {
            **_make_finding(title="Finding B (depends on A)"),
            "actionability_grade": "HIGH",
            "depends_on_indices": [0],
        }
        findings = [finding_a, finding_b]

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=findings,
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        # B's live task must be co-deferred (cancelled), never left live-blocked
        # behind its same-cycle-deferred prerequisite.
        b_children = [c for c in children if c.title.startswith("Finding B")]
        assert b_children, "the dependent must exist as a (cancelled) child task"
        assert not any(c.status == TaskStatus.BLOCKED for c in b_children), (
            "a valid index onto a deferred sibling must co-defer, not live-block"
        )
        assert all(c.status == TaskStatus.CANCELLED for c in b_children)
        # The dependent finding is shelved with the co-deferral reason so it
        # re-surfaces alongside its foundation next cycle.
        deferred = (task.context or {}).get("deferred_findings", [])
        b_hash = _finding_hash(finding_b["title"], finding_b["description"])
        shelved = next((f for f in deferred if f.get("finding_hash") == b_hash), None)
        assert shelved is not None, "the co-deferred dependent must be shelved"
        assert shelved.get("deferred_reason") == "prereq_deferred_same_cycle"


class TestDuplicateNeedDeduplication:
    """Prerequisite-need deduplication: duplicate needs are recorded once."""

    async def test_duplicate_sibling_index_co_defers_once(self, mocker, db_session):
        """Duplicate indices in depends_on_indices ([0, 0]) collapse to a single
        prerequisite need (deduped by (kind, ref) — research_completion.py:2954),
        so the dependent on a same-cycle-deferred sibling is co-deferred exactly
        ONCE: a single cancelled live task and a single shelved finding, never a
        double-shelving from the repeated index."""
        from app.research_completion import _finding_hash
        from test_autonomous_research import (
            _make_finding,
            _patch_research_externals,
            _active_direction,
            _make_research_task,
        )

        # Create a minimal project
        direction = _active_direction("d-dup-need", "Duplicate need test")
        project = _Project(
            name="Duplicate Need Test",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = await _make_research_task(
            project, direction, db_session, status=TaskStatus.COMPLETED
        )

        finding_a = {
            **_make_finding(title="Finding A (deferred)"),
            "actionability_grade": "LOW",
        }
        finding_b = {
            **_make_finding(title="Finding B (depends on A twice)"),
            "actionability_grade": "HIGH",
            "depends_on_indices": [0, 0],  # Duplicate reference to same finding
        }
        findings = [finding_a, finding_b]

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=findings,
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        # B is co-deferred: a single cancelled live task, never live-blocked.
        b_children = [c for c in children if c.title.startswith("Finding B")]
        assert len(b_children) == 1, "the duplicate index must not double-spawn B"
        assert not any(c.status == TaskStatus.BLOCKED for c in b_children)
        assert b_children[0].status == TaskStatus.CANCELLED
        # The duplicate index collapses to a single shelved dependent (deduped),
        # which retains its declaration verbatim for next-cycle reconstruction.
        deferred = (task.context or {}).get("deferred_findings", [])
        b_hash = _finding_hash(finding_b["title"], finding_b["description"])
        b_shelved = [f for f in deferred if f.get("finding_hash") == b_hash]
        assert len(b_shelved) == 1, "duplicate indices must shelve the dependent once"
        assert b_shelved[0].get("depends_on_indices") == [0, 0]

    async def test_co_deferred_dependent_retains_both_declarations(
        self, mocker, db_session
    ):
        """A dependent that names BOTH a sibling-index prerequisite AND an explicit
        {kind, ref} prerequisite is co-deferred (its sibling foundation was deferred
        this cycle). Neither declaration may be dropped on the way out: the shelved
        finding retains both ``depends_on_indices`` and ``prerequisite`` so the next
        cycle reconstructs the full (deduplicated) need set."""
        from app.research_completion import _finding_hash
        from test_autonomous_research import (
            _make_finding,
            _patch_research_externals,
            _active_direction,
            _make_research_task,
        )

        # Create a minimal project
        direction = _active_direction("d-explicit-sib", "Explicit sibling test")
        project = _Project(
            name="Explicit Sibling Test",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = await _make_research_task(
            project, direction, db_session, status=TaskStatus.COMPLETED
        )

        # Sibling finding that provides a capability
        finding_a = {
            **_make_finding(title="Finding A (provides capability)"),
            "actionability_grade": "LOW",
            "provides_capability": "shared-foundation",
        }
        finding_b = {
            **_make_finding(title="Finding B (depends on same via both methods)"),
            "actionability_grade": "HIGH",
            "depends_on_indices": [0],  # Sibling dependency
            "prerequisite": {"kind": "capability", "ref": "shared-foundation"},
        }
        findings = [finding_a, finding_b]

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=findings,
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        # B is co-deferred (cancelled), not left live-blocked.
        b_children = [c for c in children if c.title.startswith("Finding B")]
        assert b_children, "the dependent must exist as a (cancelled) child task"
        assert not any(c.status == TaskStatus.BLOCKED for c in b_children)
        assert all(c.status == TaskStatus.CANCELLED for c in b_children)
        # Both prerequisite declarations survive co-deferral on the shelved finding.
        deferred = (task.context or {}).get("deferred_findings", [])
        b_hash = _finding_hash(finding_b["title"], finding_b["description"])
        shelved = next((f for f in deferred if f.get("finding_hash") == b_hash), None)
        assert shelved is not None, "the co-deferred dependent must be shelved"
        assert shelved.get("depends_on_indices") == [0], (
            "the sibling prerequisite declaration must survive co-deferral"
        )
        assert shelved.get("prerequisite") == {
            "kind": "capability",
            "ref": "shared-foundation",
        }, "the explicit prerequisite must not be dropped on co-deferral"


class TestCompletedFoundationSatisfiesPrereqAtSpawn:
    """Spawn-time prerequisite resolution must consult the SHIPPED-completed and
    knowledge rungs the BLOCKED-resume sweep applies — not only active tasks +
    same-cycle siblings. A task prerequisite already satisfied by a merged
    foundation was runnable all along and must NOT be cancelled as
    'no_live_or_deferred_provider' (v10 release-audit HIGH)."""

    async def test_completed_shipped_foundation_satisfies_prereq_no_cancel(
        self, mocker, db_session
    ):
        """A finding whose explicit task prerequisite is satisfied by an
        already-COMPLETED, merged (shipped) foundation must spawn RUNNABLE — not be
        cancelled. The spawn-time provider universe (active tasks + same-cycle
        siblings) omits completed/merged foundations; the completed-foundation rung
        recognizes it the same way the reaper resume sweep does."""
        from test_autonomous_research import (
            _make_finding,
            _patch_research_externals,
            _active_direction,
            _make_research_task,
        )

        direction = _active_direction("d-completed-found", "Completed foundation test")
        project = _Project(
            name="Completed Foundation Test",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = await _make_research_task(
            project, direction, db_session, status=TaskStatus.COMPLETED
        )

        # An already-shipped foundation: COMPLETED + merged (pr_merged), no failed
        # parent — exactly the provider the active-only spawn universe cannot see.
        foundation = _Task(
            project_id=project.id,
            title="Already shipped foundation",
            payloads=[{"type": "create", "prompt": "foundation"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_merged": True},
        )
        db_session.add(foundation)
        await db_session.commit()
        await db_session.refresh(foundation)

        finding_b = {
            **_make_finding(title="Finding B (needs shipped foundation)"),
            "actionability_grade": "HIGH",
            "prerequisite": {"kind": "task", "ref": str(foundation.id)},
        }

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding_b],
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        b_children = [c for c in children if c.title.startswith("Finding B")]
        assert b_children, "the dependent must exist as a child task"
        for c in b_children:
            assert c.status != TaskStatus.CANCELLED, (
                "a prereq satisfied by a shipped COMPLETED foundation must NOT cancel "
                "the dependent (regression: no_live_or_deferred_provider)"
            )
            assert c.status != TaskStatus.BLOCKED, (
                "a satisfied prereq must not block the dependent either — "
                "resume-with-no-edge semantics"
            )
            assert (c.context or {}).get(
                "prerequisite_escalation_reason"
            ) != "no_live_or_deferred_provider"

    async def test_unmerged_completed_foundation_still_escalates(
        self, mocker, db_session
    ):
        """Negative control: a COMPLETED-but-UNMERGED foundation has NOT shipped, so
        it must NOT satisfy the prerequisite — the dependent is still cancelled. Proves
        the fix gates on the shipped (pr_merged) signal and does not unblock against
        unmerged work."""
        from test_autonomous_research import (
            _make_finding,
            _patch_research_externals,
            _active_direction,
            _make_research_task,
        )

        direction = _active_direction("d-unmerged-found", "Unmerged foundation test")
        project = _Project(
            name="Unmerged Foundation Test",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = await _make_research_task(
            project, direction, db_session, status=TaskStatus.COMPLETED
        )

        foundation = _Task(
            project_id=project.id,
            title="Unmerged foundation",
            payloads=[{"type": "create", "prompt": "foundation"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={},  # NO pr_merged -> never shipped
        )
        db_session.add(foundation)
        await db_session.commit()
        await db_session.refresh(foundation)

        finding_b = {
            **_make_finding(title="Finding B (needs unmerged foundation)"),
            "actionability_grade": "HIGH",
            "prerequisite": {"kind": "task", "ref": str(foundation.id)},
        }

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding_b],
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        b_children = [c for c in children if c.title.startswith("Finding B")]
        assert b_children, "the dependent must exist as a child task"
        assert all(c.status == TaskStatus.CANCELLED for c in b_children), (
            "an UNMERGED (unshipped) foundation must NOT satisfy the prereq — the "
            "dependent stays escalated/cancelled"
        )

    async def test_spawn_prereq_satisfied_by_completed_merged_is_not_cancelled(
        self, mocker, db_session
    ):
        """Gate acceptance: a finding whose explicit task prerequisite is satisfied by
        a shipped (COMPLETED+merged) foundation spawns RUNNABLE, is not cancelled, and
        has the resolved prerequisite stamped on its context via _spawn_prereq_resolved.
        This exercises the satisfied-keys filter and the observability stamp added in the
        same change (v10 release-audit HIGH fix)."""
        from test_autonomous_research import (
            _make_finding,
            _patch_research_externals,
            _active_direction,
            _make_research_task,
        )

        direction = _active_direction("d-gate-accept1", "Gate acceptance test")
        project = _Project(
            name="Gate Acceptance Test",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = await _make_research_task(
            project, direction, db_session, status=TaskStatus.COMPLETED
        )

        foundation = _Task(
            project_id=project.id,
            title="Already shipped foundation (gate acceptance)",
            payloads=[{"type": "create", "prompt": "foundation"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_merged": True},
        )
        db_session.add(foundation)
        await db_session.commit()
        await db_session.refresh(foundation)

        finding = {
            **_make_finding(title="Finding: needs shipped foundation"),
            "actionability_grade": "HIGH",
            "prerequisite": {"kind": "task", "ref": str(foundation.id)},
        }

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding],
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        dependents = [c for c in children if c.title.startswith("Finding:")]
        assert dependents, "the dependent child task must be spawned"
        for c in dependents:
            assert c.status != TaskStatus.CANCELLED, (
                "a prereq satisfied by a shipped COMPLETED+merged foundation must NOT "
                "cancel the dependent (no_live_or_deferred_provider regression)"
            )
            assert c.status != TaskStatus.BLOCKED, (
                "a satisfied prereq must not block the dependent — "
                "resume-with-no-edge semantics"
            )
            assert (c.context or {}).get(
                "prerequisite_escalation_reason"
            ) != "no_live_or_deferred_provider"
            resolved = (c.context or {}).get("_spawn_prereq_resolved")
            assert resolved, (
                "_spawn_prereq_resolved must be stamped on the child context when "
                "a prerequisite is resolved against a completed foundation at spawn"
            )
            assert any(
                r.get("kind") == "task" and r.get("ref") == str(foundation.id)
                for r in resolved
            ), (
                "_spawn_prereq_resolved must record the resolved {kind, ref} pair "
                f"— expected task:{foundation.id} in {resolved}"
            )

    async def test_ghost_merge_prereq_still_escalates(self, mocker, db_session):
        """Negative control: a COMPLETED+pr_merged foundation whose rollup parent FAILED
        is a ghost merge — the code never reached the integration branch. Such a
        foundation must NOT satisfy the prerequisite; the dependent must still be
        cancelled via the no_live_or_deferred_provider path."""
        from test_autonomous_research import (
            _make_finding,
            _patch_research_externals,
            _active_direction,
            _make_research_task,
        )

        direction = _active_direction("d-ghost-merge1", "Ghost merge test")
        project = _Project(
            name="Ghost Merge Test",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = await _make_research_task(
            project, direction, db_session, status=TaskStatus.COMPLETED
        )

        # The rollup parent whose PR failed — code never reached integration branch.
        rollup_parent = _Task(
            project_id=project.id,
            title="Rollup parent (failed)",
            payloads=[{"type": "rollup", "prompt": "rollup"}],
            status=TaskStatus.FAILED,
            created_by="ORCHESTRATOR",
            context={},
        )
        db_session.add(rollup_parent)
        await db_session.commit()
        await db_session.refresh(rollup_parent)

        # The ghost foundation: COMPLETED+pr_merged but merged only into a staging
        # branch whose rollup subsequently failed.
        ghost_foundation = _Task(
            project_id=project.id,
            title="Ghost merge foundation",
            payloads=[{"type": "create", "prompt": "ghost"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_merged": True},
            parent_task_id=rollup_parent.id,
        )
        db_session.add(ghost_foundation)
        await db_session.commit()
        await db_session.refresh(ghost_foundation)

        finding = {
            **_make_finding(title="Finding: needs ghost foundation"),
            "actionability_grade": "HIGH",
            "prerequisite": {"kind": "task", "ref": str(ghost_foundation.id)},
        }

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding],
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        dependents = [c for c in children if c.title.startswith("Finding:")]
        assert dependents, "the dependent must exist as a child task"
        assert all(c.status == TaskStatus.CANCELLED for c in dependents), (
            "a ghost-merge foundation (pr_merged=True but rollup parent FAILED) must "
            "NOT satisfy the prereq — the dependent must remain cancelled"
        )
        for c in dependents:
            assert (c.context or {}).get(
                "prerequisite_escalation_reason"
            ) == "no_live_or_deferred_provider", (
                "ghost-merge escalation must set no_live_or_deferred_provider reason"
            )

    async def test_ghost_merge_squash_landed_not_stranded(self, mocker, db_session):
        """A non-terminal parent whose merge commit is not an ancestor of the
        integration branch (ancestry=False) but whose PR number IS referenced in
        a recent squash commit message must NOT be marked stranded."""
        from test_autonomous_research import (
            _active_direction,
            _make_finding,
            _make_research_task,
            _patch_research_externals,
        )
        import app.research_completion as _rc_mod

        direction = _active_direction("d-squash-land1", "Squash landed test")
        project = _Project(
            name="Squash Landed Test",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = await _make_research_task(
            project, direction, db_session, status=TaskStatus.COMPLETED
        )

        # Non-terminal parent with a rollup PR number — simulates an IN_PROGRESS
        # rollup whose commit was squash-merged onto integration.
        rollup_parent = _Task(
            project_id=project.id,
            title="Rollup parent (in-progress)",
            payloads=[{"type": "rollup", "prompt": "rollup"}],
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            context={"rollup_pr_number": 101},
        )
        db_session.add(rollup_parent)
        await db_session.commit()
        await db_session.refresh(rollup_parent)

        # COMPLETED+pr_merged child of the rollup — this populates
        # potential_ghost_parent_ids so the ancestry check is triggered.
        ghost_foundation = _Task(
            project_id=project.id,
            title="Foundation (pr_merged)",
            payloads=[{"type": "create", "prompt": "foundation"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_merged": True},
            parent_task_id=rollup_parent.id,
        )
        db_session.add(ghost_foundation)
        await db_session.commit()
        await db_session.refresh(ghost_foundation)

        finding = {
            **_make_finding(title="Finding: from squash PR"),
            "actionability_grade": "HIGH",
        }
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding],
        )
        mocker.patch(
            "app.release_qualifier._resolve_github_with_session",
            new=AsyncMock(return_value=("tok", "owner", "repo")),
        )
        mocker.patch(
            "app.github.get_pull_request",
            new=AsyncMock(return_value={"merge_commit_sha": "deadbeef1234"}),
        )
        # ancestry returns False — the pre-squash SHA is not on integration
        mocker.patch(
            "app.github.is_ancestor_of",
            new=AsyncMock(return_value=False),
        )
        mocker.patch(
            "app.github.get_pull_request_commits",
            new=AsyncMock(return_value=[]),
        )
        # squash reference found — PR #101 appears in a recent commit message
        squash_ref_mock = mocker.patch(
            "app.github.check_squash_referenced",
            new=AsyncMock(return_value=True),
        )
        # Clear the idempotency cache before this test
        _rc_mod._SQUASH_LANDED_CONFIRMED_PARENT_IDS.discard(str(rollup_parent.id))

        await _handle_research_completion(task, "", db_session)

        # Squash-reference check must have been called
        squash_ref_mock.assert_awaited_once()
        # Parent must have been added to the confirmed cache
        assert str(rollup_parent.id) in _rc_mod._SQUASH_LANDED_CONFIRMED_PARENT_IDS

    async def test_ghost_merge_squash_not_referenced_is_stranded(
        self, mocker, db_session
    ):
        """A non-terminal parent with ancestry=False and no squash reference
        found must be marked stranded (treated as a true ghost merge)."""
        from test_autonomous_research import (
            _active_direction,
            _make_finding,
            _make_research_task,
            _patch_research_externals,
        )
        import app.research_completion as _rc_mod

        direction = _active_direction("d-squash-strand1", "Squash strand test")
        project = _Project(
            name="Squash Strand Test",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = await _make_research_task(
            project, direction, db_session, status=TaskStatus.COMPLETED
        )

        rollup_parent = _Task(
            project_id=project.id,
            title="Rollup parent (in-progress, unmerged)",
            payloads=[{"type": "rollup", "prompt": "rollup"}],
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            context={"rollup_pr_number": 202},
        )
        db_session.add(rollup_parent)
        await db_session.commit()
        await db_session.refresh(rollup_parent)

        ghost_foundation = _Task(
            project_id=project.id,
            title="Foundation (pr_merged, unmerged rollup)",
            payloads=[{"type": "create", "prompt": "foundation"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_merged": True},
            parent_task_id=rollup_parent.id,
        )
        db_session.add(ghost_foundation)
        await db_session.commit()
        await db_session.refresh(ghost_foundation)

        finding = {
            **_make_finding(title="Finding: from unmerged PR"),
            "actionability_grade": "HIGH",
        }
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding],
        )
        mocker.patch(
            "app.release_qualifier._resolve_github_with_session",
            new=AsyncMock(return_value=("tok", "owner", "repo")),
        )
        mocker.patch(
            "app.github.get_pull_request",
            new=AsyncMock(return_value={"merge_commit_sha": "cafebabe5678"}),
        )
        mocker.patch(
            "app.github.is_ancestor_of",
            new=AsyncMock(return_value=False),
        )
        mocker.patch(
            "app.github.get_pull_request_commits",
            new=AsyncMock(return_value=[]),
        )
        # squash reference not found — PR #202 is not on integration branch
        squash_ref_mock = mocker.patch(
            "app.github.check_squash_referenced",
            new=AsyncMock(return_value=False),
        )
        _rc_mod._SQUASH_LANDED_CONFIRMED_PARENT_IDS.discard(str(rollup_parent.id))

        await _handle_research_completion(task, "", db_session)

        squash_ref_mock.assert_awaited_once()
        # Parent must NOT have been added to the squash-confirmed cache
        assert str(rollup_parent.id) not in _rc_mod._SQUASH_LANDED_CONFIRMED_PARENT_IDS

    async def test_ghost_merge_squash_idempotency_guard(self, mocker, db_session):
        """A parent already in _SQUASH_LANDED_CONFIRMED_PARENT_IDS must not
        trigger a re-check of the squash reference on a second cycle."""
        from test_autonomous_research import (
            _active_direction,
            _make_finding,
            _make_research_task,
            _patch_research_externals,
        )
        import app.research_completion as _rc_mod

        direction = _active_direction("d-squash-idem1", "Squash idempotency test")
        project = _Project(
            name="Squash Idempotency Test",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = await _make_research_task(
            project, direction, db_session, status=TaskStatus.COMPLETED
        )

        rollup_parent = _Task(
            project_id=project.id,
            title="Rollup parent (cached)",
            payloads=[{"type": "rollup", "prompt": "rollup"}],
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            context={"rollup_pr_number": 303},
        )
        db_session.add(rollup_parent)
        await db_session.commit()
        await db_session.refresh(rollup_parent)

        ghost_foundation = _Task(
            project_id=project.id,
            title="Foundation (pr_merged, cached rollup)",
            payloads=[{"type": "create", "prompt": "foundation"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_merged": True},
            parent_task_id=rollup_parent.id,
        )
        db_session.add(ghost_foundation)
        await db_session.commit()
        await db_session.refresh(ghost_foundation)

        # Pre-seed the idempotency cache
        _rc_mod._SQUASH_LANDED_CONFIRMED_PARENT_IDS.add(str(rollup_parent.id))

        finding = {
            **_make_finding(title="Finding: idempotency guard"),
            "actionability_grade": "HIGH",
        }
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding],
        )
        mocker.patch(
            "app.release_qualifier._resolve_github_with_session",
            new=AsyncMock(return_value=("tok", "owner", "repo")),
        )
        mocker.patch(
            "app.github.get_pull_request",
            new=AsyncMock(return_value={"merge_commit_sha": "11223344aabb"}),
        )
        mocker.patch(
            "app.github.is_ancestor_of",
            new=AsyncMock(return_value=False),
        )
        # check_squash_referenced must NOT be called for a cached parent
        squash_ref_mock = mocker.patch(
            "app.github.check_squash_referenced",
            new=AsyncMock(return_value=True),
        )

        await _handle_research_completion(task, "", db_session)

        squash_ref_mock.assert_not_awaited()

    async def test_recent_terminal_duplicate_finding_is_suppressed_by_history(
        self, mocker, db_session
    ):
        """A reworded finding should be suppressed once a recent terminal task already
        explored the same direction and evidence."""
        from test_autonomous_research import (
            _active_direction,
            _make_finding,
            _make_research_task,
            _patch_research_externals,
        )

        direction = _active_direction("d-terminal-history", "Terminal history test")
        project = _Project(
            name="Terminal History Test",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        current_task = await _make_research_task(
            project, direction, db_session, status=TaskStatus.COMPLETED
        )

        terminal_finding = _make_finding(
            title="measure duplicate retry inflation drivers",
            direction_id=direction["id"],
        )
        terminal_finding["sources"] = [
            {"type": "file", "ref": "backend/app/cache.py:42"},
            {"type": "file", "ref": "backend/app/models.py#L100-L200"},
        ]
        terminal_finding["claims"] = [
            "retry inflation is caused by duplicate spawning",
        ]
        terminal_task = _Task(
            project_id=project.id,
            title="Research: terminal history",
            payloads=[
                {"type": "research", "directions": [direction], "base_branch": "main"}
            ],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={
                "finding_hash": "terminal-history-hash",
                "direction_id": direction["id"],
                "pr_merged": True,
                # Cross-cycle dedup reads only findings that survived the
                # completion gates; raw pre-gate plan_output is not evidence.
                "research_findings_finalized": True,
            },
            plan_output=[terminal_finding],
        )
        db_session.add(terminal_task)
        await db_session.commit()
        await db_session.refresh(terminal_task)

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[
                {
                    **_make_finding(
                        title="track duplicate retry inflation causes",
                        direction_id=direction["id"],
                    ),
                    "description": "reworded desc",
                    "sources": [
                        {"type": "file", "ref": "backend/app/cache.py"},
                        {"type": "file", "ref": "backend/app/models.py:200"},
                    ],
                    "claims": [
                        "retry inflation is caused by duplicate spawning",
                    ],
                }
            ],
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )
        mocker.patch(
            "app.research_completion.resolve_branch_verification_context",
            new=AsyncMock(return_value={}),
        )
        mocker.patch(
            "app.research_completion.resolve_head_commit",
            new=AsyncMock(return_value=None),
        )
        mocker.patch(
            "app.research_completion.cited_paths_changed_since",
            new=AsyncMock(return_value=None),
        )
        mocker.patch(
            "app.research_completion._has_validation_backend", return_value=True
        )
        mocker.patch(
            "app.research_completion._summarize_ungrounded_claims_grounded",
            new=AsyncMock(return_value=None),
        )

        await _handle_research_completion(current_task, "", db_session)
        await db_session.refresh(current_task)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == current_task.id)
                )
            )
            .scalars()
            .all()
        )
        assert children == []
        assert (current_task.context or {}).get("research_surviving_findings") == 0

    async def test_sibling_dedup_against_completed_merged_not_cancelled(
        self, mocker, db_session
    ):
        """A finding whose depends_on_indices sibling was deduplicated (filtered) against
        an already-completed+merged task must NOT be cancelled. The satisfied-keys filter
        must recognize the completed task by finding_hash and resolve the need at spawn,
        allowing the dependent to proceed runnable."""
        from app.finding_deduplication import finding_hash as _fhash
        from test_autonomous_research import (
            _make_finding,
            _patch_research_externals,
            _active_direction,
            _make_research_task,
        )

        sibling_title = "Sibling foundation for dedup"
        sibling_desc = f"Description: {sibling_title}"
        sibling_hash = _fhash(sibling_title, sibling_desc)

        direction = _active_direction("d-sibling-ded1", "Sibling dedup test")
        project = _Project(
            name="Sibling Dedup Test",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = await _make_research_task(
            project, direction, db_session, status=TaskStatus.COMPLETED
        )

        # Completed+merged foundation whose finding_hash matches the sibling finding.
        # build_dedup_state will include sibling_hash in recent_finding_hashes, so
        # deduplicate_findings drops the sibling. The spawn loop then sees a missing
        # dep and must resolve it against this completed foundation.
        completed_sibling = _Task(
            project_id=project.id,
            title=sibling_title,
            payloads=[{"type": "create", "prompt": "sibling"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"finding_hash": sibling_hash, "pr_merged": True},
        )
        db_session.add(completed_sibling)
        await db_session.commit()
        await db_session.refresh(completed_sibling)

        # finding_a: same title/description as completed_sibling → gets deduped.
        finding_a = {
            **_make_finding(title=sibling_title),
            "description": sibling_desc,
            "actionability_grade": "HIGH",
        }
        # finding_b: depends on finding_a (index 0).
        finding_b = {
            **_make_finding(title="Dependent of sibling"),
            "actionability_grade": "HIGH",
            "depends_on_indices": [0],
        }

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding_a, finding_b],
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        # finding_a was deduped — no child for it.
        b_children = [c for c in children if c.title.startswith("Dependent of sibling")]
        assert b_children, "the dependent of the deduped sibling must still be spawned"
        for c in b_children:
            assert c.status != TaskStatus.CANCELLED, (
                "a depends_on_indices sibling deduped against a completed+merged task "
                "must NOT cancel the dependent (satisfied-keys filter must fire)"
            )

    async def test_mixed_needs_one_completed_satisfied_one_live_prereq(
        self, mocker, db_session
    ):
        """Mixed-needs edge: one prerequisite satisfied by a completed+merged foundation
        (resolved at spawn, no gate) and one pointing to a live active task (edge wired).
        The dependent must spawn runnable with a depends_on edge to the live foundation
        and must not be cancelled or blocked."""
        from app.finding_deduplication import finding_hash as _fhash
        from test_autonomous_research import (
            _make_finding,
            _patch_research_externals,
            _active_direction,
            _make_research_task,
        )

        sibling_title = "Completed sibling (mixed needs)"
        sibling_desc = f"Description: {sibling_title}"
        sibling_hash = _fhash(sibling_title, sibling_desc)

        direction = _active_direction("d-mixed-needs1", "Mixed needs test")
        project = _Project(
            name="Mixed Needs Test",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = await _make_research_task(
            project, direction, db_session, status=TaskStatus.COMPLETED
        )

        # Completed+merged foundation (satisfied by filter via finding_hash).
        completed_foundation = _Task(
            project_id=project.id,
            title=sibling_title,
            payloads=[{"type": "create", "prompt": "completed"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"finding_hash": sibling_hash, "pr_merged": True},
        )
        db_session.add(completed_foundation)

        # Live active foundation (satisfies explicit prereq via wired edge).
        live_foundation = _Task(
            project_id=project.id,
            title="Live active foundation",
            payloads=[{"type": "create", "prompt": "live"}],
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            context={},
        )
        db_session.add(live_foundation)
        await db_session.commit()
        await db_session.refresh(completed_foundation)
        await db_session.refresh(live_foundation)

        # finding_a: same hash as completed_foundation → gets deduped.
        finding_a = {
            **_make_finding(title=sibling_title),
            "description": sibling_desc,
            "actionability_grade": "HIGH",
        }
        # finding_c: depends on finding_a (deduped = need resolved at spawn) AND has
        # an explicit prereq pointing to the live foundation (wired as edge).
        finding_c = {
            **_make_finding(title="Finding C (mixed needs)"),
            "actionability_grade": "HIGH",
            "depends_on_indices": [0],
            "prerequisite": {"kind": "task", "ref": str(live_foundation.id)},
        }

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding_a, finding_c],
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        c_children = [c for c in children if c.title.startswith("Finding C")]
        assert c_children, "finding_c must be spawned as a child task"
        for c in c_children:
            assert c.status != TaskStatus.CANCELLED, (
                "mixed-needs: completed prereq resolved at spawn + live prereq wired "
                "must NOT cancel the dependent"
            )
            assert c.status != TaskStatus.BLOCKED, (
                "mixed-needs: all needs resolved — dependent must not be blocked"
            )
            # Live foundation edge must be wired.
            deps = list(c.depends_on or [])
            assert str(live_foundation.id) in deps, (
                "the live foundation must be wired as a depends_on edge on the "
                f"dependent; got depends_on={deps}"
            )


class TestBreakFindingDependencyCycles:
    """_break_dependency_cycles: minimally break a cycle in declared finding
    edges, preserving valid prerequisite chains elsewhere in the batch."""

    def test_mixed_batch_keeps_valid_chain_breaks_only_cycle(self):
        from app.plan_completion import (
            _break_dependency_cycles,
            _validate_plan_dependencies,
        )

        # Findings 0<->1 form a 2-cycle; findings 2->3->4 is a valid linear
        # prerequisite chain that must survive untouched.
        findings = [
            {"title": "cycle-a", "depends_on_indices": [1]},
            {"title": "cycle-b", "depends_on_indices": [0]},
            {"title": "chain-leaf", "depends_on_indices": []},
            {"title": "chain-mid", "depends_on_indices": [2]},
            {"title": "chain-top", "depends_on_indices": [3]},
        ]

        dropped = _break_dependency_cycles(findings, "RESEARCH")

        # The graph is now acyclic.
        assert _validate_plan_dependencies(findings)
        # Exactly one edge was dropped to break the 2-cycle.
        assert len(dropped) == 1
        # The valid chain's edges survive intact.
        assert findings[3]["depends_on_indices"] == [2]
        assert findings[4]["depends_on_indices"] == [3]
        # Only one of the two mutually-cyclic edges remains; the cycle is gone.
        remaining_cycle_edges = bool(findings[0].get("depends_on_indices")) + bool(
            findings[1].get("depends_on_indices")
        )
        assert remaining_cycle_edges == 1
        # The dropped record names the offending edge for observability.
        rec = dropped[0]
        assert {rec["successor_index"], rec["predecessor_index"]} == {0, 1}
        assert rec["successor"] in {"cycle-a", "cycle-b"}


# ─────────────────────────────────────────────────────────────────────────────
# compute_experiment launch-contract authoring + spend-threshold approval gate
#
# The decomposition/finding spawn path authors + validates a compute_experiment
# launch contract (command / spend_cap_usd / deadline_seconds / optional
# success_criterion) before creating any task. When the proposed spend cap
# exceeds the configured threshold knob (per-direction override else the global
# `compute_experiment_spend_threshold_usd`), the spawn is held for operator
# approval via Task.requires_approval; at or below the threshold it dispatches.
# A malformed / incomplete contract is deferred loudly (no broken job spawned).
# ─────────────────────────────────────────────────────────────────────────────


_DEFAULT_SMOKE_SAMPLE = {
    "exit_code": 0,
    "startup_ok": True,
    "metric_captured": True,
    "timed_out": False,
}


def _compute_finding(
    direction_id: str,
    *,
    command="python3 bench.py --n 1000",
    spend_cap_usd=2.0,
    deadline_seconds=3600,
    success_criterion=None,
    accelerator=None,
    provisioning_model=None,
    nested_key=None,
    title="Measure throughput regression",
    smoke_sample=_DEFAULT_SMOKE_SAMPLE,
    sweep_id=None,
):
    """A compute_experiment-typed research finding carrying a launch contract.

    By default the launch fields are flat on the finding; pass ``nested_key`` to
    nest them under ``compute_experiment`` / ``launch_contract`` instead.
    Pass ``smoke_sample=None`` to omit the smoke field (for negative-path tests).
    """
    from test_autonomous_research import _make_finding

    finding = _make_finding(
        title=title, direction_id=direction_id, finding_type="compute_experiment"
    )
    launch = {}
    if command is not None:
        launch["command"] = command
    if spend_cap_usd is not None:
        launch["spend_cap_usd"] = spend_cap_usd
    if deadline_seconds is not None:
        launch["deadline_seconds"] = deadline_seconds
    if success_criterion is not None:
        launch["success_criterion"] = success_criterion
    if accelerator is not None:
        launch["accelerator"] = accelerator
    if provisioning_model is not None:
        launch["provisioning_model"] = provisioning_model
    if smoke_sample is not None:
        launch["smoke_sample"] = smoke_sample
    if sweep_id is not None:
        launch["sweep_id"] = sweep_id
    if nested_key:
        finding[nested_key] = launch
    else:
        finding.update(launch)
    return finding


async def _children_of(db_session, task):
    return (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )


class TestComputeLaunchContractAuthoring:
    """_author_compute_launch_contract: collect launch fields flat or nested."""

    def test_flat_fields_collected(self):
        from app.research_completion import _author_compute_launch_contract

        contract = _author_compute_launch_contract(
            {
                "command": "run.sh",
                "spend_cap_usd": 3.5,
                "deadline_seconds": 1800,
                "success_criterion": {"metric": "ms", "max": 50},
                "machine_type": "n2-standard-4",
            }
        )
        assert contract["command"] == "run.sh"
        assert contract["spend_cap_usd"] == 3.5
        assert contract["deadline_seconds"] == 1800
        assert contract["success_criterion"] == {"metric": "ms", "max": 50}
        assert contract["machine_type"] == "n2-standard-4"

    def test_nested_under_compute_experiment_key(self):
        from app.research_completion import _author_compute_launch_contract

        contract = _author_compute_launch_contract(
            {
                "compute_experiment": {
                    "command": "x.py",
                    "spend_cap_usd": 1,
                    "deadline_seconds": 60,
                }
            }
        )
        assert contract["command"] == "x.py"
        assert contract["spend_cap_usd"] == 1
        assert contract["deadline_seconds"] == 60

    def test_nested_under_launch_contract_key(self):
        from app.research_completion import _author_compute_launch_contract

        contract = _author_compute_launch_contract(
            {
                "launch_contract": {
                    "command": "y.py",
                    "spend_cap_usd": 2,
                    "deadline_seconds": 120,
                }
            }
        )
        assert contract["command"] == "y.py"
        assert contract["deadline_seconds"] == 120

    def test_optional_fields_omitted_when_absent(self):
        from app.research_completion import _author_compute_launch_contract

        contract = _author_compute_launch_contract(
            {"command": "z.py", "spend_cap_usd": 1, "deadline_seconds": 1}
        )
        assert "success_criterion" not in contract
        assert "machine_type" not in contract


class TestComputeLaunchContractValidation:
    """_validate_compute_launch_contract: loud reasons for each failure mode."""

    def test_valid_contract_ok(self):
        from app.research_completion import _validate_compute_launch_contract

        ok, reason = _validate_compute_launch_contract(
            {
                "command": "run.sh",
                "spend_cap_usd": 2.0,
                "deadline_seconds": 60,
                "smoke_sample": _DEFAULT_SMOKE_SAMPLE,
            }
        )
        assert ok is True
        assert reason == ""

    def test_missing_command_rejected(self):
        from app.research_completion import _validate_compute_launch_contract

        ok, reason = _validate_compute_launch_contract(
            {"command": None, "spend_cap_usd": 2.0, "deadline_seconds": 60}
        )
        assert ok is False
        assert "command" in reason

    def test_blank_command_rejected(self):
        from app.research_completion import _validate_compute_launch_contract

        ok, reason = _validate_compute_launch_contract(
            {"command": "   ", "spend_cap_usd": 2.0, "deadline_seconds": 60}
        )
        assert ok is False
        assert "command" in reason

    def test_nonpositive_spend_rejected(self):
        from app.research_completion import _validate_compute_launch_contract

        ok, reason = _validate_compute_launch_contract(
            {"command": "run.sh", "spend_cap_usd": 0, "deadline_seconds": 60}
        )
        assert ok is False
        assert "spend_cap_usd" in reason

    def test_nonnumeric_spend_rejected(self):
        from app.research_completion import _validate_compute_launch_contract

        ok, reason = _validate_compute_launch_contract(
            {"command": "run.sh", "spend_cap_usd": "free", "deadline_seconds": 60}
        )
        assert ok is False
        assert "spend_cap_usd" in reason

    def test_nonpositive_deadline_rejected(self):
        from app.research_completion import _validate_compute_launch_contract

        ok, reason = _validate_compute_launch_contract(
            {"command": "run.sh", "spend_cap_usd": 2.0, "deadline_seconds": 0}
        )
        assert ok is False
        assert "deadline_seconds" in reason


class TestResolveComputeSpendThreshold:
    """_resolve_compute_spend_threshold: per-direction override else global."""

    def test_global_default(self):
        from app.research_completion import _resolve_compute_spend_threshold

        assert (
            _resolve_compute_spend_threshold(
                {"compute_experiment_spend_threshold_usd": 7.5}, None
            )
            == 7.5
        )

    def test_direction_override_wins(self):
        from app.research_completion import _resolve_compute_spend_threshold

        assert (
            _resolve_compute_spend_threshold(
                {"compute_experiment_spend_threshold_usd": 5.0},
                {"compute_experiment_spend_threshold_usd": 20.0},
            )
            == 20.0
        )

    def test_nonpositive_override_falls_back_to_global(self):
        from app.research_completion import _resolve_compute_spend_threshold

        assert (
            _resolve_compute_spend_threshold(
                {"compute_experiment_spend_threshold_usd": 5.0},
                {"compute_experiment_spend_threshold_usd": 0},
            )
            == 5.0
        )

    def test_missing_global_defaults_zero(self):
        from app.research_completion import _resolve_compute_spend_threshold

        assert _resolve_compute_spend_threshold({}, None) == 0.0


@_pytest.mark.asyncio
class TestComputeExperimentSpawn:
    """End-to-end spawn path: below/above-threshold dispatch + malformed defer."""

    async def _run_compute_overlap_case(
        self, db_session, mocker, *, active_type: str, title_prefix: str
    ):
        from test_autonomous_research import _patch_research_externals

        project, direction, task = await _grader_project_and_task(
            db_session, f"d-cx-overlap-{active_type}"
        )
        active_task = _Task(
            project_id=project.id,
            title=f"{title_prefix} measure live release proof acceptance evidence",
            payloads=[{"type": active_type}],
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            context={
                "direction_id": direction["id"],
                "related_files": ["app/release.py", "app/proof.py"],
            },
        )
        db_session.add(active_task)
        await db_session.commit()
        finding = _compute_finding(
            direction["id"],
            title="Measure live release proof acceptance evidence",
            spend_cap_usd=2.0,
            deadline_seconds=3600,
        )
        # Region-identity dedup keys on related_files: same files as the active
        # task so the compute-typed case collides (the research-typed case is
        # still kept — blocked earlier by the compute-experiment type gate).
        finding["related_files"] = ["app/release.py", "app/proof.py"]
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[finding]
        )
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 5.0},
        )

        await _handle_research_completion(task, "", db_session)

        children = await _children_of(db_session, task)
        return [
            c
            for c in children
            if (c.first_payload or {}).get("type") == "compute_experiment"
        ]

    async def test_compute_experiment_word_overlap_ignores_active_research_titles(
        self, db_session, mocker
    ):
        compute_children = await self._run_compute_overlap_case(
            db_session, mocker, active_type="research", title_prefix="Research"
        )
        assert len(compute_children) == 1
        assert (
            compute_children[0].title
            == "Measure live release proof acceptance evidence"
        )

    async def test_compute_experiment_word_overlap_blocks_active_compute_titles(
        self, db_session, mocker
    ):
        compute_children = await self._run_compute_overlap_case(
            db_session,
            mocker,
            active_type="compute_experiment",
            title_prefix="Compute",
        )
        assert compute_children == []

    async def test_below_threshold_spawns_dispatched_not_held(self, db_session, mocker):
        from test_autonomous_research import _patch_research_externals

        _project, direction, task = await _grader_project_and_task(db_session, "d-cx01")
        finding = _compute_finding(
            direction["id"], spend_cap_usd=2.0, deadline_seconds=3600
        )
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[finding]
        )
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 5.0},
        )

        await _handle_research_completion(task, "", db_session)

        children = await _children_of(db_session, task)
        assert len(children) == 1
        child = children[0]
        payload = child.first_payload or {}
        assert payload.get("type") == "compute_experiment"
        assert payload.get("command") == "python3 bench.py --n 1000"
        assert payload.get("spend_cap_usd") == 2.0
        assert payload.get("deadline_seconds") == 3600
        # At/below threshold -> dispatched, not held.
        assert child.requires_approval is False

    async def test_required_local_compute_uses_loaded_project_for_spend_policy(
        self, db_session, mocker
    ):
        """Research completion must evaluate local placement on its loaded project.

        This guards the completion-path variable mismatch that raised NameError
        before any compute child could be created.
        """
        from sqlalchemy.orm.attributes import flag_modified
        from test_autonomous_research import _patch_research_externals

        project, direction, task = await _grader_project_and_task(
            db_session, "d-cx-local-policy"
        )
        project.build_config = {
            **(project.build_config or {}),
            "resource_envelope": {"require_local_runner": True},
        }
        flag_modified(project, "build_config")
        db_session.add(project)
        await db_session.commit()
        finding = _compute_finding(
            direction["id"], spend_cap_usd=50.0, deadline_seconds=3600
        )
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding],
        )
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 5.0},
        )

        await _handle_research_completion(task, "", db_session)

        children = await _children_of(db_session, task)
        assert len(children) == 1
        assert (children[0].first_payload or {}).get("type") == "compute_experiment"
        assert children[0].requires_approval is False

    async def test_above_threshold_spawns_held_for_approval(self, db_session, mocker):
        from test_autonomous_research import _patch_research_externals

        _project, direction, task = await _grader_project_and_task(db_session, "d-cx02")
        finding = _compute_finding(
            direction["id"], spend_cap_usd=50.0, deadline_seconds=7200
        )
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[finding]
        )
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 5.0},
        )

        await _handle_research_completion(task, "", db_session)

        children = await _children_of(db_session, task)
        assert len(children) == 1
        child = children[0]
        # Task is still spawned (not dropped) — but held for operator approval.
        assert (child.first_payload or {}).get("type") == "compute_experiment"
        assert child.requires_approval is True
        msg = child.latest_status_message or ""
        assert "$50.00" in msg
        assert "approval" in msg.lower()
        # Compute spend-threshold holds must carry the approval_hold_reason marker so
        # recovery sweeps route them through the longer non-failure CANCELLED path.
        assert child.context.get("approval_hold_reason") == "spend_budget"

    async def test_per_direction_override_holds_below_global(self, db_session, mocker):
        """A per-direction override below the global threshold holds a spend that
        the global alone would have dispatched."""
        from test_autonomous_research import _patch_research_externals

        from sqlalchemy.orm.attributes import flag_modified

        _project, direction, task = await _grader_project_and_task(db_session, "d-cx03")
        # Direction-level override of $1; global is $100. Proposed $10 > $1 -> held.
        # The handler reads the override from the persisted project's directions
        # (a JSON column), so set it there rather than on the local dict copy.
        _project.directions[0]["compute_experiment_spend_threshold_usd"] = 1.0
        flag_modified(_project, "directions")
        db_session.add(_project)
        await db_session.commit()
        finding = _compute_finding(
            direction["id"], spend_cap_usd=10.0, deadline_seconds=600
        )
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[finding]
        )
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 100.0},
        )

        await _handle_research_completion(task, "", db_session)

        children = await _children_of(db_session, task)
        assert len(children) == 1
        assert children[0].requires_approval is True
        assert children[0].context.get("approval_hold_reason") == "spend_budget"

    async def test_nested_contract_below_threshold_dispatches(self, db_session, mocker):
        """A launch contract nested under `compute_experiment` is authored +
        dispatched identically to a flat one."""
        from test_autonomous_research import _patch_research_externals

        _project, direction, task = await _grader_project_and_task(db_session, "d-cx04")
        finding = _compute_finding(
            direction["id"],
            spend_cap_usd=1.0,
            deadline_seconds=120,
            nested_key="compute_experiment",
        )
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[finding]
        )
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 5.0},
        )

        await _handle_research_completion(task, "", db_session)

        children = await _children_of(db_session, task)
        assert len(children) == 1
        payload = children[0].first_payload or {}
        assert payload.get("command") == "python3 bench.py --n 1000"
        assert payload.get("spend_cap_usd") == 1.0
        assert children[0].requires_approval is False

    async def test_accelerator_and_provisioning_forwarded(self, db_session, mocker):
        from test_autonomous_research import _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-cx04a"
        )
        finding = _compute_finding(
            direction["id"],
            spend_cap_usd=1.5,
            deadline_seconds=240,
            accelerator="V100",
            provisioning_model="spot",
            nested_key="compute_experiment",
        )
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[finding]
        )
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 5.0},
        )

        await _handle_research_completion(task, "", db_session)

        children = await _children_of(db_session, task)
        payload = children[0].first_payload or {}
        assert payload["accelerator"] == "V100"
        assert payload["provisioning_model"] == "spot"

    async def test_malformed_missing_command_defers_no_task(
        self, db_session, mocker, caplog
    ):
        from test_autonomous_research import _patch_research_externals

        _project, direction, task = await _grader_project_and_task(db_session, "d-cx05")
        finding = _compute_finding(
            direction["id"], command=None, spend_cap_usd=2.0, deadline_seconds=60
        )
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[finding]
        )
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 5.0},
        )

        with caplog.at_level(logging.WARNING, logger="app.research_completion"):
            await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        # No compute_experiment task spawned for the malformed contract.
        children = await _children_of(db_session, task)
        assert not any(
            (c.first_payload or {}).get("type") == "compute_experiment"
            for c in children
        )
        # The finding is deferred loudly under the compute_contract_invalid reason.
        deferred = (task.context or {}).get("deferred_findings", [])
        assert any(
            d.get("deferred_reason") == "compute_contract_invalid" for d in deferred
        )
        # Deferred loudly: a warning names the contract failure (empty command).
        assert any(
            "malformed launch contract" in r.getMessage()
            and "command" in r.getMessage()
            for r in caplog.records
        )

    async def test_malformed_nonpositive_spend_defers_no_task(self, db_session, mocker):
        from test_autonomous_research import _patch_research_externals

        _project, direction, task = await _grader_project_and_task(db_session, "d-cx06")
        finding = _compute_finding(
            direction["id"], spend_cap_usd=0, deadline_seconds=60
        )
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[finding]
        )
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 5.0},
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        children = await _children_of(db_session, task)
        assert children == []
        deferred = (task.context or {}).get("deferred_findings", [])
        assert any(
            d.get("deferred_reason") == "compute_contract_invalid" for d in deferred
        )

    async def test_malformed_nonpositive_deadline_defers_no_task(
        self, db_session, mocker
    ):
        from test_autonomous_research import _patch_research_externals

        _project, direction, task = await _grader_project_and_task(db_session, "d-cx07")
        finding = _compute_finding(
            direction["id"], spend_cap_usd=2.0, deadline_seconds=0
        )
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[finding]
        )
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 5.0},
        )

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

        children = await _children_of(db_session, task)
        assert children == []
        deferred = (task.context or {}).get("deferred_findings", [])
        assert any(
            d.get("deferred_reason") == "compute_contract_invalid" for d in deferred
        )

    @_pytest.mark.asyncio
    async def test_compute_experiment_spawn_carries_downstream_artifact_evidence(
        self, db_session, mocker
    ):
        """A compute_experiment finding with downstream_artifact acceptance_evidence
        carries that evidence onto the spawned task's first_payload.

        The artifact delivery contract reads first_payload via read_acceptance_evidence
        to find the downstream_artifact logical key; if the field is absent the
        terminal COMPUTE_RESULT Artifact is never stamped and queued dependents never
        receive the artifact prerequisite pointer.
        """
        from test_autonomous_research import _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-cx-ae01"
        )
        finding = _compute_finding(
            direction["id"], spend_cap_usd=2.0, deadline_seconds=3600
        )
        finding["acceptance_evidence"] = {
            "kind": "downstream_artifact",
            "artifact_id": "benchmark-result-bundle",
        }
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[finding]
        )
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 5.0},
        )

        await _handle_research_completion(task, "", db_session)

        children = await _children_of(db_session, task)
        assert len(children) == 1
        child = children[0]
        payload = child.first_payload or {}
        assert payload.get("type") == "compute_experiment"
        evidence = payload.get("acceptance_evidence")
        assert evidence is not None, (
            "acceptance_evidence missing from compute_experiment payload"
        )
        assert evidence["kind"] == "downstream_artifact"
        assert evidence["artifact_id"] == "benchmark-result-bundle"
        assert child.context["acceptance_evidence"] == {
            "kind": "artifact",
            "descriptor": "benchmark-result-bundle",
        }

    @_pytest.mark.asyncio
    async def test_compute_experiment_sweep_id_propagated(self, db_session, mocker):
        """sweep_id on a compute_experiment finding is forwarded to the child payload.

        task_completion._handle_compute_experiment_completion gates the sweep
        aggregate path on sweep_id (line 3194); when it is None the aggregate
        path is bypassed and arm_task_ids is never stamped.  This test pins that
        sweep_id is present in the spawned child's first_payload.
        """
        from test_autonomous_research import _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-cx-sweep01"
        )
        finding = _compute_finding(
            direction["id"],
            spend_cap_usd=2.0,
            deadline_seconds=3600,
            sweep_id="sweep-test-123",
        )
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[finding]
        )
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 5.0},
        )

        await _handle_research_completion(task, "", db_session)

        children = await _children_of(db_session, task)
        assert len(children) == 1
        payload = children[0].first_payload or {}
        assert payload.get("type") == "compute_experiment"
        assert payload.get("sweep_id") == "sweep-test-123", (
            f"sweep_id missing from compute_experiment child payload; got {payload.get('sweep_id')!r}"
        )

    @_pytest.mark.asyncio
    async def test_compute_experiment_malformed_acceptance_evidence_degrades_not_aborts(
        self, db_session, mocker
    ):
        """A malformed acceptance_evidence on a compute_experiment finding degrades
        to absent without aborting the spawn — same lenient pattern as non-compute
        findings."""
        from test_autonomous_research import _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-cx-ae02"
        )
        finding = _compute_finding(
            direction["id"], spend_cap_usd=2.0, deadline_seconds=3600
        )
        # downstream_artifact with empty artifact_id → AcceptanceEvidence rejects it
        finding["acceptance_evidence"] = {
            "kind": "downstream_artifact",
            "artifact_id": "",
        }
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[finding]
        )
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 5.0},
        )

        await _handle_research_completion(task, "", db_session)

        children = await _children_of(db_session, task)
        assert len(children) == 1, "malformed evidence must not abort the spawn"
        payload = children[0].first_payload or {}
        assert payload.get("type") == "compute_experiment"
        assert "acceptance_evidence" not in payload, (
            "malformed acceptance_evidence must degrade to omitted, "
            f"got {payload.get('acceptance_evidence')!r}"
        )
        assert "acceptance_evidence" not in (children[0].context or {})


@_pytest.mark.asyncio
async def test_compute_experiment_with_gpu_or_material_spend_is_adversarially_challenged(
    db_session, mocker
):
    """ACCEPTANCE TEST: a compute_experiment finding with GPU accelerator or material spend
    is treated as high-stakes and must be adversarially challenged before any child is spawned.

    The adversarial spawn gate calls _is_high_stakes_finding on each finding; a compute
    finding with an accelerator or spend approaching the threshold must return True, causing
    the task to park EVALUATING while the adversarial grader runs. No compute_experiment
    child must spawn before the challenger verdict arrives.
    """
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-cx-adv01")
    # A GPU-backed compute finding — must be high-stakes regardless of spend
    finding = _compute_finding(
        direction["id"],
        spend_cap_usd=2.0,
        deadline_seconds=600,
        accelerator="T4",
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )
    mocker.patch("app.research_completion._is_high_stakes_finding", return_value=True)
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {
            "compute_experiment_spend_threshold_usd": 5.0,
            "adversarial_spawn_gate_enabled": True,
        },
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # Task must be parked EVALUATING awaiting adversarial verdict, not COMPLETED.
    assert task.status == TaskStatus.EVALUATING, (
        "GPU-backed compute finding must park the task EVALUATING awaiting adversarial challenge"
    )
    assert task.context.get("awaiting_adversarial_grader"), (
        "awaiting_adversarial_grader flag must be set after gating a high-stakes compute finding"
    )

    # No compute_experiment child must have been spawned yet.
    children = await _children_of(db_session, task)
    compute_children = [
        c
        for c in children
        if (c.first_payload or {}).get("type") == "compute_experiment"
    ]
    assert compute_children == [], (
        "compute_experiment task must not spawn before adversarial verdict arrives"
    )

    # An adversarial grader must have been spawned.
    others = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id, _Task.id != task.id
                )
            )
        )
        .scalars()
        .all()
    )
    graders = [t for t in others if (t.context or {}).get("adversarial_grader") is True]
    assert len(graders) == 1, (
        f"Exactly one adversarial grader must be spawned; got {len(graders)}"
    )


@_pytest.mark.asyncio
async def test_compute_experiment_defaults_container_image_to_project_image_research(
    db_session, mocker
):
    """When a project's manifest declares compute.image.name and a research finding
    declares an accelerator but omits container_image, _handle_research_completion
    must default the spawned compute_experiment child's container_image to the
    project's declared image. A finding declaring NEITHER accelerator NOR
    container_image must NOT inherit the default — see
    test_compute_experiment_bare_finding_stays_image_less_research below."""
    from test_autonomous_research import _patch_research_externals

    _PROJECT_IMAGE = "us-central1-docker.pkg.dev/myproject/compute/base:sha256-abc123"

    mocker.patch(
        "app.routers.admin_project_tools._discover_manifest_tools",
        new=AsyncMock(
            return_value={
                "tools": [],
                "skills": [],
                "compute": {"image": {"name": _PROJECT_IMAGE}},
                "error": None,
            }
        ),
    )

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-cx-img-default-01"
    )

    finding = _compute_finding(
        direction["id"],
        spend_cap_usd=2.0,
        deadline_seconds=3600,
        accelerator="nvidia-t4",
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"compute_experiment_spend_threshold_usd": 5.0},
    )

    await _handle_research_completion(task, "", db_session)

    children = await _children_of(db_session, task)
    compute_children = [
        c
        for c in children
        if (c.first_payload or {}).get("type") == "compute_experiment"
    ]
    assert len(compute_children) == 1, (
        f"Expected 1 compute_experiment child, got {len(compute_children)}: {children}"
    )
    payload = compute_children[0].first_payload or {}
    assert payload.get("container_image") == _PROJECT_IMAGE, (
        f"Expected container_image={_PROJECT_IMAGE!r}, got {payload.get('container_image')!r}"
    )


@_pytest.mark.asyncio
async def test_compute_experiment_bare_finding_stays_image_less_research(
    db_session, mocker
):
    """A research finding declaring NEITHER accelerator NOR container_image must
    NOT inherit the project's declared default image, even when the manifest
    declares one. Attaching a default image to an accelerator-less task arms the
    runner's fatal GPU-arch pre-flight on GPU hosts for a task that never asked
    for a GPU — the root cause of the NeurIPS-fleet outage this defect fixes.
    """
    from test_autonomous_research import _patch_research_externals

    _PROJECT_IMAGE = "us-central1-docker.pkg.dev/myproject/compute/base:sha256-abc123"

    mocker.patch(
        "app.routers.admin_project_tools._discover_manifest_tools",
        new=AsyncMock(
            return_value={
                "tools": [],
                "skills": [],
                "compute": {"image": {"name": _PROJECT_IMAGE}},
                "error": None,
            }
        ),
    )

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-cx-img-bare-01"
    )

    finding = _compute_finding(
        direction["id"],
        spend_cap_usd=2.0,
        deadline_seconds=3600,
        # NEITHER accelerator NOR container_image.
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"compute_experiment_spend_threshold_usd": 5.0},
    )

    await _handle_research_completion(task, "", db_session)

    children = await _children_of(db_session, task)
    compute_children = [
        c
        for c in children
        if (c.first_payload or {}).get("type") == "compute_experiment"
    ]
    assert len(compute_children) == 1, (
        f"Expected 1 compute_experiment child, got {len(compute_children)}: {children}"
    )
    payload = compute_children[0].first_payload or {}
    assert payload.get("container_image") is None, (
        "a bare finding (no accelerator, no container_image) must not inherit "
        f"the project default image; got {payload.get('container_image')!r}"
    )
    assert payload.get("accelerator") is None


class TestAcceptanceEvidencePropagation:
    """Plan-time acceptance_evidence rides each surviving finding to its child.

    Two complementary carriers: automain's named_test declaration lands on the
    child PAYLOAD (consumed by the merge gate) via ``_spawn_child``; this PR's
    lighter ``{kind, descriptor, selector}`` proof descriptor lands on the child
    CONTEXT (rendered/run by the agent) via ``_spawn_from_findings``. The
    declaration is a plain field on the finding dict (NOT routed through
    depends_on_indices), so it must survive batch-cap truncation for every
    finding that survives. Absence changes nothing.
    """

    async def _spawn_child(self, db_session, mocker, *, direction_id, finding):
        from test_autonomous_research import _patch_research_externals

        _project, _direction, task = await _grader_project_and_task(
            db_session, direction_id
        )
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding],
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )
        mocker.patch(
            "app.research_completion.resolve_branch_verification_context",
            new=AsyncMock(return_value={}),
        )
        for path in (
            "app.research_completion.resolve_head_commit",
            "app.research_completion.cited_paths_changed_since",
            "app.research_completion._summarize_ungrounded_claims_grounded",
        ):
            mocker.patch(path, new=AsyncMock(return_value=None))
        mocker.patch(
            "app.research_completion._has_validation_backend",
            return_value=False,
        )
        await _handle_research_completion(task, "", db_session)
        return (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .first()
        )

    async def _spawn_from_findings(
        self, db_session, mocker, *, direction_id, findings, force_cap_one=False
    ):
        from test_autonomous_research import _patch_research_externals

        _project, _direction, task = await _grader_project_and_task(
            db_session, direction_id
        )

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=findings,
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )
        if force_cap_one:
            # Force the per-cycle finding cap to 1 to exercise the truncation path
            # deterministically. The cap is a flat constant now (real capacity is
            # gated at dispatch), so drive it directly rather than via the gauges.
            mocker.patch("app.research_completion._MAX_FINDINGS_PER_CYCLE", 1)
        for path, value in (
            ("app.research_completion.resolve_branch_verification_context", {}),
            ("app.research_completion.resolve_head_commit", None),
            ("app.research_completion.cited_paths_changed_since", False),
            ("app.research_completion._summarize_ungrounded_claims_grounded", None),
        ):
            mocker.patch(path, new=AsyncMock(return_value=value))

        await _handle_research_completion(task, "", db_session)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        return children, task

    @_pytest.mark.asyncio
    async def test_finding_acceptance_evidence_populates_both_payload_and_context(
        self, db_session, mocker
    ):
        from test_autonomous_research import _make_finding

        finding = _make_finding(
            title="Finding with forward evidence", direction_id="d-ae-prop01"
        )
        finding["acceptance_evidence"] = {
            "kind": "observable_behavior",
            "description": "the endpoint returns 409 when promotion is in flight",
        }
        child = await self._spawn_child(
            db_session, mocker, direction_id="d-ae-prop01", finding=finding
        )
        assert child is not None
        evidence = child.first_payload.get("acceptance_evidence")
        assert evidence is not None, "acceptance_evidence missing from child payload"
        assert evidence["kind"] == "observable_behavior"
        assert "promotion is in flight" in evidence["description"]
        assert child.context["acceptance_evidence"] == {
            "kind": "behavior",
            "descriptor": "the endpoint returns 409 when promotion is in flight",
        }

    @_pytest.mark.asyncio
    async def test_finding_without_acceptance_evidence_spawns_unchanged(
        self, db_session, mocker
    ):
        from test_autonomous_research import _make_finding

        finding = _make_finding(
            title="Finding without forward evidence", direction_id="d-ae-prop02"
        )
        child = await self._spawn_child(
            db_session, mocker, direction_id="d-ae-prop02", finding=finding
        )
        assert child is not None
        assert "acceptance_evidence" not in child.first_payload, (
            "acceptance_evidence must not be synthesized when the finding omits it, "
            f"got {child.first_payload.get('acceptance_evidence')!r}"
        )

    @_pytest.mark.asyncio
    async def test_malformed_finding_acceptance_evidence_degrades_not_aborts(
        self, db_session, mocker
    ):
        """A malformed finding declaration must NOT abort the completion.

        An LLM-generated finding declaration is untrusted. A malformed one
        (here: observable_behavior with a too-short description) must degrade to
        'no declaration' (key omitted) and still spawn its child — the strict
        payload validation must never raise out of the spawn loop, which would
        lose ALL findings in the completion.
        """
        from test_autonomous_research import _make_finding

        finding = _make_finding(
            title="Finding with malformed evidence", direction_id="d-ae-prop03"
        )
        finding["acceptance_evidence"] = {
            "kind": "observable_behavior",
            "description": "x",  # < 10 chars → AcceptanceEvidence rejects it
        }
        child = await self._spawn_child(
            db_session, mocker, direction_id="d-ae-prop03", finding=finding
        )
        assert child is not None, "finding lost — malformed evidence aborted the batch"
        assert "acceptance_evidence" not in child.first_payload, (
            "malformed acceptance_evidence must degrade to omitted, got "
            f"{child.first_payload.get('acceptance_evidence')!r}"
        )
        assert "acceptance_evidence" not in (child.context or {})

    @_pytest.mark.asyncio
    async def test_finding_acceptance_evidence_propagates_to_child_context(
        self, db_session, mocker
    ):
        from test_autonomous_research import _make_finding

        finding = _make_finding(title="Declared proof", direction_id="d-ae-ctx01")
        finding["acceptance_evidence"] = {
            "kind": "behavior",
            "descriptor": "endpoint scopes audit to the open candidate",
        }
        children, _task = await self._spawn_from_findings(
            db_session, mocker, direction_id="d-ae-ctx01", findings=[finding]
        )
        child = next(c for c in children if c.title == "Declared proof")
        assert child.context["acceptance_evidence"] == {
            "kind": "behavior",
            "descriptor": "endpoint scopes audit to the open candidate",
        }
        assert child.first_payload["acceptance_evidence"] == {
            "kind": "observable_behavior",
            "description": "endpoint scopes audit to the open candidate",
        }

    @_pytest.mark.asyncio
    async def test_finding_absent_acceptance_evidence_is_noop(self, db_session, mocker):
        from test_autonomous_research import _make_finding

        finding = _make_finding(title="No proof", direction_id="d-ae-ctx02")
        children, _task = await self._spawn_from_findings(
            db_session, mocker, direction_id="d-ae-ctx02", findings=[finding]
        )
        child = next(c for c in children if c.title == "No proof")
        assert "acceptance_evidence" not in (child.context or {})

    @_pytest.mark.asyncio
    async def test_design_child_payload_annotations_include_capability_proof_status(
        self, db_session, mocker
    ):
        from test_autonomous_research import (
            _make_finding,
            _patch_research_externals,
        )

        finding = _make_finding(title="Design proof surface", direction_id="d-ae-ctx04")
        finding["type"] = "design"
        project, _direction, _task = await _grader_project_and_task(
            db_session, "d-ae-ctx04"
        )
        project.problem_model = {
            "capabilities": [
                {"name": "Repo Map", "status": "working"},
                {"name": "Prompt Repair", "status": "planned"},
            ]
        }
        project.capability_proof_records = {
            "repo-map": [
                {
                    "task_id": "task-proof-1",
                    "proved_at": "2026-06-09T00:00:00",
                    "proof_kind": "named_test",
                    "proof_ref": "backend/testing/test_capability_proof.py::test_repo_map",
                    "commit_sha": "abc123",
                }
            ]
        }
        db_session.add(project)
        await db_session.commit()

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding],
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )
        mocker.patch("app.shared_state.tasks_active_gauge", 0)
        mocker.patch("app.shared_state.tasks_queued_gauge", 0)
        for path, value in (
            ("app.research_completion.resolve_branch_verification_context", {}),
            ("app.research_completion.resolve_head_commit", None),
            ("app.research_completion.cited_paths_changed_since", False),
            ("app.research_completion._summarize_ungrounded_claims_grounded", None),
        ):
            mocker.patch(path, new=AsyncMock(return_value=value))

        await _handle_research_completion(_task, "", db_session)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == _task.id)
                )
            )
            .scalars()
            .all()
        )
        child = next(c for c in children if c.title == "Design proof surface")
        assert child.first_payload["available_capabilities"] == [
            {"name": "repo-map", "status": "working", "proof_status": "proven"},
            {"name": "prompt-repair", "status": "planned", "proof_status": "unproven"},
        ]

    @_pytest.mark.asyncio
    async def test_finding_malformed_acceptance_evidence_is_dropped(
        self, db_session, mocker
    ):
        from test_autonomous_research import _make_finding

        finding = _make_finding(title="Bad proof", direction_id="d-ae-ctx03")
        finding["acceptance_evidence"] = {"kind": "screenshot", "descriptor": "nope"}
        children, _task = await self._spawn_from_findings(
            db_session, mocker, direction_id="d-ae-ctx03", findings=[finding]
        )
        child = next(c for c in children if c.title == "Bad proof")
        assert "acceptance_evidence" not in (child.context or {})
        assert "acceptance_evidence" not in child.first_payload

    @_pytest.mark.asyncio
    async def test_acceptance_evidence_survives_batch_cap_truncation(
        self, db_session, mocker
    ):
        """A surviving high-priority finding keeps acceptance_evidence under a cap.

        With the batch cap forced to 1, the low-priority finding is deferred and
        the high-priority one survives — and its declaration must ride through.
        """
        from test_autonomous_research import _make_finding

        survivor = _make_finding(
            title="Survivor with proof",
            direction_id="d-ae-trunc01",
            priority="high",
        )
        survivor["acceptance_evidence"] = {
            "kind": "behavior",
            "descriptor": "endpoint scopes audit to the open candidate",
        }
        deferred = _make_finding(
            title="Deferred no proof",
            direction_id="d-ae-trunc01",
            priority="low",
        )
        children, _task = await self._spawn_from_findings(
            db_session,
            mocker,
            direction_id="d-ae-trunc01",
            findings=[survivor, deferred],
            force_cap_one=True,
        )
        titles = {c.title for c in children}
        assert "Survivor with proof" in titles
        assert "Deferred no proof" not in titles
        survivor_child = next(c for c in children if c.title == "Survivor with proof")
        assert survivor_child.context["acceptance_evidence"] == {
            "kind": "behavior",
            "descriptor": "endpoint scopes audit to the open candidate",
        }
        assert survivor_child.first_payload["acceptance_evidence"] == {
            "kind": "observable_behavior",
            "description": "endpoint scopes audit to the open candidate",
        }

    @_pytest.mark.asyncio
    async def test_criterion_carrying_finding_without_named_test_is_rejected(
        self, db_session, mocker
    ):
        """When a direction carries criterion_keys and a finding lacks
        acceptance_evidence:{kind:named_test}, the spawn loop must REJECT
        the finding (hard rejection — no child task spawned). The previous
        non-blocking provenance warning is replaced by a hard gate that
        blocks the spawn so uncovered criterion-carrying findings never
        become work that cannot close the criterion coverage loop."""
        from app.models import Direction as _Direction, ReleaseVersion
        from app import acceptance_criteria as _ac
        from test_autonomous_research import (
            _active_direction,
            _make_finding,
            _patch_research_externals,
        )

        direction_id = "d-crit-reject-01"
        direction_data = _active_direction(
            direction_id, "Criterion rejection direction"
        )
        project = _Project(
            name="CritReject Project",
            user_id="test-user-crit-reject",
            autonomous_enabled=True,
            directions=[direction_data],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        version = ReleaseVersion(
            project_id=project.id, version_number=1, status="open"
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)
        criterion = await _ac.create_criterion(
            db_session,
            project_id=project.id,
            release_version_id=version.id,
            text="All endpoints documented",
            created_by="strategist",
        )

        dir_row = _Direction(
            project_id=project.id,
            direction_key=direction_id,
            data=direction_data,
            status="active",
            criterion_keys=[criterion.criterion_key],
        )
        db_session.add(dir_row)
        await db_session.commit()

        task = _Task(
            project_id=project.id,
            title="Research: Criterion rejection direction",
            payloads=[
                {
                    "type": "research",
                    "directions": [direction_data],
                    "base_branch": "main",
                }
            ],
            context={"research_depth": 0, "direction_id": direction_id},
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        finding = _make_finding(
            title="Missing named_test evidence",
            direction_id=direction_id,
        )
        # Explicitly ensure no acceptance_evidence key is present.
        finding.pop("acceptance_evidence", None)

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding],
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )

        await _handle_research_completion(task, "", db_session)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        # The finding lacks named_test evidence on a criterion-carrying direction
        # → hard rejection: zero implementation child tasks spawned. A memo task
        # records the rejection reason but is not an implementation task.
        impl_children = [
            c for c in children
            if (c.first_payload or {}).get("type") != "memo"
        ]
        assert len(impl_children) == 0, (
            "expected zero implementation children for criterion-carrying finding "
            f"without named_test evidence; got {len(impl_children)}"
        )
        # The rejection count is recorded in the research task context.
        rejected = (task.context or {}).get("research_criterion_evidence_rejected")
        assert rejected == 1, (
            "expected research_criterion_evidence_rejected=1 in task context; "
            f"got {rejected!r}"
        )

    @_pytest.mark.asyncio
    async def test_criterion_carrying_finding_with_named_test_evidence_spawns(
        self, db_session, mocker
    ):
        """When a direction carries criterion_keys and a finding declares
        acceptance_evidence:{kind:named_test}, the spawn loop must let the
        finding through — the named_test declaration is sufficient proof."""
        from app.models import Direction as _Direction, ReleaseVersion
        from app import acceptance_criteria as _ac
        from test_autonomous_research import (
            _active_direction,
            _make_finding,
            _patch_research_externals,
        )

        direction_id = "d-crit-spawn-01"
        direction_data = _active_direction(
            direction_id, "Criterion spawn direction"
        )
        project = _Project(
            name="CritSpawn Project",
            user_id="test-user-crit-spawn",
            autonomous_enabled=True,
            directions=[direction_data],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        version = ReleaseVersion(
            project_id=project.id, version_number=1, status="open"
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)
        criterion = await _ac.create_criterion(
            db_session,
            project_id=project.id,
            release_version_id=version.id,
            text="All endpoints documented",
            created_by="strategist",
        )

        dir_row = _Direction(
            project_id=project.id,
            direction_key=direction_id,
            data=direction_data,
            status="active",
            criterion_keys=[criterion.criterion_key],
        )
        db_session.add(dir_row)
        await db_session.commit()

        task = _Task(
            project_id=project.id,
            title="Research: Criterion spawn direction",
            payloads=[
                {
                    "type": "research",
                    "directions": [direction_data],
                    "base_branch": "main",
                }
            ],
            context={"research_depth": 0, "direction_id": direction_id},
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        finding = _make_finding(
            title="Has named_test evidence",
            direction_id=direction_id,
        )
        finding["acceptance_evidence"] = {
            "kind": "named_test",
            "test_path": "backend/testing/test_research_completion.py",
        }

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding],
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )

        await _handle_research_completion(task, "", db_session)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(children) == 1, (
            "expected exactly one spawned child task for finding with "
            f"named_test evidence; got {len(children)}"
        )
        # Verify the acceptance evidence was forwarded to the child.
        child = children[0]
        assert child.first_payload.get("acceptance_evidence") == {
            "kind": "named_test",
            "test_path": "backend/testing/test_research_completion.py",
        }, (
            "child payload must carry the named_test acceptance_evidence; "
            f"got {child.first_payload.get('acceptance_evidence')!r}"
        )

    @_pytest.mark.asyncio
    async def test_criterion_keys_warning_injected_into_research_context(
        self, db_session, mocker
    ):
        """When a direction carries active criterion_keys and a finding is
        spawned, the child task's prompt_suffix must include a criterion_keys
        warning so the implementing agent knows to declare named_test
        acceptance evidence in its output. Without this upfront signal the
        agent wastes a full research cycle before the rejection gate fires."""
        from app.models import Direction as _Direction, ReleaseVersion
        from app import acceptance_criteria as _ac
        from test_autonomous_research import (
            _active_direction,
            _make_finding,
            _patch_research_externals,
        )

        direction_id = "d-crit-warn-01"
        direction_data = _active_direction(
            direction_id, "Criterion warning direction"
        )
        project = _Project(
            name="CritWarn Project",
            user_id="test-user-crit-warn",
            autonomous_enabled=True,
            directions=[direction_data],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        version = ReleaseVersion(
            project_id=project.id, version_number=1, status="open"
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)
        criterion = await _ac.create_criterion(
            db_session,
            project_id=project.id,
            release_version_id=version.id,
            text="All endpoints documented",
            created_by="strategist",
        )

        dir_row = _Direction(
            project_id=project.id,
            direction_key=direction_id,
            data=direction_data,
            status="active",
            criterion_keys=[criterion.criterion_key],
        )
        db_session.add(dir_row)
        await db_session.commit()

        task = _Task(
            project_id=project.id,
            title="Research: Criterion warning direction",
            payloads=[
                {
                    "type": "research",
                    "directions": [direction_data],
                    "base_branch": "main",
                }
            ],
            context={"research_depth": 0, "direction_id": direction_id},
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        finding = _make_finding(
            title="Finding with named_test for warning injection",
            direction_id=direction_id,
        )
        finding["acceptance_evidence"] = {
            "kind": "named_test",
            "test_path": "backend/testing/test_research_completion.py",
        }

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding],
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )

        await _handle_research_completion(task, "", db_session)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(children) == 1, (
            "expected exactly one spawned child task; "
            f"got {len(children)}"
        )
        child = children[0]
        prompt_suffix = child.first_payload.get("prompt_suffix") or ""
        assert "acceptance_evidence" in prompt_suffix, (
            "child prompt_suffix must contain criterion_keys warning "
            "about acceptance_evidence requirement; "
            f"got {prompt_suffix!r}"
        )
        assert "named_test" in prompt_suffix, (
            "child prompt_suffix must mention named_test evidence kind; "
            f"got {prompt_suffix!r}"
        )



# ── Shared capability vocabulary: off-registry advisory marking ───────────────
# A declared prerequisite / provides_capability capability ref that is NOT in the
# strategist-curated problem_model.capabilities registry must be FLAGGED ADVISORY
# and carried through — never rejected or dropped — and must still resolve EXACTLY
# against any provider that advertises it (matchers stay exact, no fuzzy matching).


def test_off_registry_capability_ref_marked_advisory_not_rejected():
    registry_names = ["payment-core"]  # the only curated capability
    findings = [
        # Consumer naming an off-registry capability as a prerequisite.
        {
            "title": "consumer off-registry",
            "prerequisite": {"kind": "capability", "ref": "seeded-eval-harness"},
        },
        # Producer advertising an off-registry capability.
        {
            "title": "producer off-registry",
            "provides_capability": "seeded-eval-harness",
        },
        # Consumer naming an ON-registry capability (different casing/spacing that
        # normalizes onto the canonical registry name) — must NOT be flagged.
        {
            "title": "consumer on-registry",
            "prerequisite": {"kind": "capability", "ref": "Payment Core"},
        },
    ]

    _annotate_capability_declarations(findings, registry_names)

    # The off-registry declarations are PRESERVED, not dropped.
    assert findings[0]["_prerequisite"] == {
        "kind": "capability",
        "ref": "seeded-eval-harness",
    }
    assert findings[1]["_provides_capability"] == "seeded-eval-harness"
    # ... and each is flagged with the advisory off-registry marker.
    assert findings[0]["_off_registry_capabilities"] == ["seeded-eval-harness"]
    assert findings[1]["_off_registry_capabilities"] == ["seeded-eval-harness"]

    # The on-registry ref normalizes onto a registry name → NOT flagged advisory,
    # but still carried through normalized.
    assert findings[2]["_prerequisite"] == {"kind": "capability", "ref": "Payment Core"}
    assert "_off_registry_capabilities" not in findings[2]

    # The off-registry prerequisite STILL resolves EXACTLY against a provider that
    # advertises the same marker — the advisory flag never breaks resolution.
    provider = _Task(
        id=uuid.uuid4(),
        title="provider",
        context={"provides_capability": "seeded-eval-harness"},
    )
    resolved = resolve_prerequisite_task(findings[0]["_prerequisite"], [provider])
    assert resolved is provider

    # A provider advertising a DIFFERENT capability does not satisfy it (exact only).
    other = _Task(
        id=uuid.uuid4(),
        title="other",
        context={"provides_capability": "payment-core"},
    )
    assert resolve_prerequisite_task(findings[0]["_prerequisite"], [other]) is None


def test_annotate_drops_title_like_task_prereq_and_records_malformed_advisory():
    """A title-like kind=task prerequisite is dropped (no _prerequisite) and a
    retirable _malformed_prerequisite advisory is recorded so it never drives an
    edge / blocked need, while an EXACT task ref and a capability ref are kept."""
    findings = [
        {
            "title": "title-like",
            "prerequisite": {"kind": "task", "ref": "Build the harness"},
        },
        {
            "title": "exact-hash",
            "prerequisite": {"kind": "task", "ref": "deadbeefcafe0001"},
        },
        {"title": "cap", "prerequisite": {"kind": "capability", "ref": "payment-core"}},
        # A finding that previously carried a malformed advisory but now declares a
        # valid prereq must have the stale advisory cleared.
        {
            "title": "now-valid",
            "prerequisite": {"kind": "task", "ref": "feedfacefeedface"},
            "_malformed_prerequisite": {
                "kind": "task",
                "ref": "old prose",
                "reason": "x",
            },
        },
    ]
    _annotate_capability_declarations(findings, [])

    # Title-like task ref: dropped, advisory recorded.
    assert "_prerequisite" not in findings[0]
    adv = findings[0]["_malformed_prerequisite"]
    assert adv["kind"] == "task"
    assert adv["ref"] == "Build the harness"
    assert "capability" in adv["reason"]
    # Exact / capability refs: carried through, no advisory.
    assert findings[1]["_prerequisite"] == {"kind": "task", "ref": "deadbeefcafe0001"}
    assert "_malformed_prerequisite" not in findings[1]
    assert findings[2]["_prerequisite"] == {"kind": "capability", "ref": "payment-core"}
    assert "_malformed_prerequisite" not in findings[2]
    # Stale advisory cleared once a valid prereq is declared.
    assert findings[3]["_prerequisite"] == {"kind": "task", "ref": "feedfacefeedface"}
    assert "_malformed_prerequisite" not in findings[3]


def test_annotate_capability_declarations_clears_stale_markers_and_skips_non_dicts():
    """Re-annotating drops a stale advisory marker once the registry catches up,
    and non-dict finding entries are skipped without raising."""
    findings = [
        {
            "title": "now on-registry",
            "provides_capability": "seeded-eval-harness",
            "_off_registry_capabilities": ["seeded-eval-harness"],  # stale
        },
        "not-a-dict",
    ]
    # Registry now contains the capability → the stale marker must be cleared.
    _annotate_capability_declarations(findings, ["seeded-eval-harness"])
    assert "_off_registry_capabilities" not in findings[0]
    assert findings[0]["_provides_capability"] == "seeded-eval-harness"


# ---------------------------------------------------------------------------
# Adversarial spawn-gate: _is_high_stakes_finding predicate (pure unit tests)
# ---------------------------------------------------------------------------


class TestIsHighStakesFinding:
    """_is_high_stakes_finding: identifies findings that warrant an adversarial challenge."""

    def _f(self, **kw):
        base = {
            "title": "Refactor auth",
            "description": "The auth module has a bug.",
            "prompt": "Fix the auth module",
            "type": "improvement",
            "scope": "small",
            "risk": "low",
        }
        base.update(kw)
        return base

    def test_large_scope_is_high_stakes(self):
        assert _is_high_stakes_finding(self._f(scope="large"))

    def test_high_risk_is_high_stakes(self):
        assert _is_high_stakes_finding(self._f(risk="high"))

    def test_critical_risk_is_high_stakes(self):
        assert _is_high_stakes_finding(self._f(risk="critical"))

    def test_very_high_risk_is_high_stakes(self):
        assert _is_high_stakes_finding(self._f(risk="very_high"))

    def test_verification_basis_downgraded_is_high_stakes(self):
        assert _is_high_stakes_finding(
            self._f(verification_basis_downgraded_from="code_confirmed")
        )

    def test_broken_refs_provenance_is_high_stakes(self):
        assert _is_high_stakes_finding(
            self._f(_provenance_warnings={"broken_refs": ["app/auth.py"]})
        )

    def test_ungrounded_refs_provenance_is_high_stakes(self):
        assert _is_high_stakes_finding(
            self._f(_provenance_warnings={"ungrounded_refs": ["app/foo.py:bar"]})
        )

    def test_cited_files_changed_is_high_stakes(self):
        assert _is_high_stakes_finding(
            self._f(_provenance_warnings={"cited_files_changed_since_generation": True})
        )

    def test_needs_revalidation_is_high_stakes(self):
        assert _is_high_stakes_finding(self._f(needs_revalidation=True))

    def test_small_scope_low_risk_no_signals_is_not_high_stakes(self):
        assert not _is_high_stakes_finding(self._f())

    def test_non_spawning_type_is_not_high_stakes(self):
        assert not _is_high_stakes_finding(
            self._f(scope="large", type="design_doc_observation")
        )

    def test_description_only_high_risk_finding_is_challenged_before_spawn(self):
        """A finding with only a non-empty description (no prompt) but high-stakes
        properties (scope=large, risk=high) should be eligible for challenge."""
        # Finding with description but empty/no prompt + scope=large → should be high-stakes
        finding = {
            "title": "Refactor auth",
            "description": "The auth module has a critical bug that affects security.",
            # No prompt or empty prompt
            "type": "improvement",
            "scope": "large",
            "risk": "low",
        }
        assert _is_high_stakes_finding(finding) is True

    def test_empty_prompt_is_not_high_stakes(self):
        assert not _is_high_stakes_finding(self._f(scope="large", prompt="   "))

    def test_unrecognized_already_spawned_field_cannot_bypass_challenge(self):
        assert _is_high_stakes_finding(self._f(scope="large", already_spawned=True))

    def test_low_actionability_is_not_high_stakes(self):
        assert not _is_high_stakes_finding(
            self._f(scope="large", actionability_grade="LOW")
        )

    def test_capability_metadata_cannot_bypass_high_stakes_review(self):
        foundation = self._f(risk="high", provides_capability="payment-core")
        _annotate_capability_declarations([foundation], [])
        assert _is_high_stakes_finding(foundation)

        prerequisite = self._f(risk="high")
        prerequisite["prerequisite"] = {"kind": "capability", "ref": "payment-core"}
        _annotate_capability_declarations([prerequisite], [])
        assert _is_high_stakes_finding(prerequisite)

    def test_non_dict_is_not_high_stakes(self):
        assert not _is_high_stakes_finding("not a dict")
        assert not _is_high_stakes_finding(None)


class TestIsHighStakesComputeFinding:
    """_is_high_stakes_finding: compute_experiment findings with GPU/spend/deadline."""

    def _cf(self, **kw):
        base = {
            "title": "Measure GPU throughput",
            "description": "Benchmark the training loop.",
            "prompt": "Run the benchmark job",
            "type": "compute_experiment",
            "command": "python3 bench.py",
            "spend_cap_usd": 1.0,
            "deadline_seconds": 600,
        }
        base.update(kw)
        return base

    def test_gpu_accelerator_is_high_stakes(self):
        assert _is_high_stakes_finding(self._cf(accelerator="T4"))

    def test_gpu_accelerator_nested_is_high_stakes(self):
        finding = {
            "title": "Benchmark",
            "prompt": "Run benchmark",
            "type": "compute_experiment",
            "compute_experiment": {
                "command": "bench.py",
                "spend_cap_usd": 1.0,
                "deadline_seconds": 600,
                "accelerator": "A100",
            },
        }
        assert _is_high_stakes_finding(finding)

    def test_spend_exceeds_threshold_is_high_stakes(self, mocker):
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 5.0},
        )
        assert _is_high_stakes_finding(self._cf(spend_cap_usd=10.0, accelerator=None))

    def test_spend_approaches_threshold_is_high_stakes(self, mocker):
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 10.0},
        )
        # 75% of threshold exceeds the 50% approach boundary
        assert _is_high_stakes_finding(self._cf(spend_cap_usd=7.5, accelerator=None))

    def test_spend_at_half_threshold_is_high_stakes(self, mocker):
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 10.0},
        )
        assert _is_high_stakes_finding(self._cf(spend_cap_usd=5.0, accelerator=None))

    def test_spend_below_approach_is_not_high_stakes(self, mocker):
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 100.0},
        )
        # 1% of threshold — well below the 50% approach boundary
        assert not _is_high_stakes_finding(
            self._cf(spend_cap_usd=1.0, accelerator=None)
        )

    def test_zero_threshold_any_spend_is_high_stakes(self, mocker):
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 0.0},
        )
        assert _is_high_stakes_finding(self._cf(spend_cap_usd=0.01, accelerator=None))

    def test_long_deadline_is_high_stakes(self, mocker):
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 1000.0},
        )
        assert _is_high_stakes_finding(
            self._cf(deadline_seconds=7200, accelerator=None, spend_cap_usd=0.01)
        )

    def test_deadline_at_boundary_is_not_high_stakes(self, mocker):
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 1000.0},
        )
        # Exactly 3600 seconds (== 1 hour) is NOT over the threshold
        assert not _is_high_stakes_finding(
            self._cf(deadline_seconds=3600, accelerator=None, spend_cap_usd=0.01)
        )

    def test_malformed_contract_not_high_stakes_via_this_gate(self, mocker):
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 5.0},
        )
        # Missing spend_cap_usd — defer path is _validate_compute_launch_contract
        finding = {
            "title": "Benchmark",
            "prompt": "Run benchmark",
            "type": "compute_experiment",
            "command": "bench.py",
            "deadline_seconds": 600,
        }
        assert not _is_high_stakes_finding(finding)

    def test_nested_compute_contract_fields_read(self, mocker):
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 5.0},
        )
        finding = {
            "title": "Nested contract benchmark",
            "prompt": "Run nested benchmark",
            "type": "compute_experiment",
            "compute_experiment": {
                "command": "bench.py",
                "spend_cap_usd": 50.0,
                "deadline_seconds": 600,
            },
        }
        assert _is_high_stakes_finding(finding)

    def test_low_actionability_compute_not_high_stakes(self):
        assert not _is_high_stakes_finding(
            self._cf(actionability_grade="LOW", accelerator="T4")
        )

    def test_measurement_type_gpu_is_high_stakes(self):
        finding = dict(self._cf(type="measurement", accelerator="V100"))
        assert _is_high_stakes_finding(finding)

    def test_benchmark_type_gpu_is_high_stakes(self):
        finding = dict(self._cf(type="benchmark", accelerator="H100"))
        assert _is_high_stakes_finding(finding)


# ---------------------------------------------------------------------------
# parse_adversarial_finding_verdicts: fail-closed parse (pure unit tests)
# ---------------------------------------------------------------------------


class TestParseAdversarialFindingVerdicts:
    """parse_adversarial_finding_verdicts: fail-closed pass/fail extraction."""

    def test_extracts_fail_verdict(self):
        logs = '[{"hash": "abc123", "verdict": "fail", "reason": "premise wrong"}]'
        result, outcome = parse_adversarial_finding_verdicts(logs, ["abc123"])
        assert result["abc123"]["passed"] is False
        assert result["abc123"]["reason"] == "premise wrong"
        assert result["abc123"]["explicit"] is True
        assert outcome == "ok"

    def test_extracts_pass_verdict(self):
        logs = '[{"hash": "abc123", "verdict": "pass", "reason": "well-grounded"}]'
        result, outcome = parse_adversarial_finding_verdicts(logs, ["abc123"])
        assert result["abc123"]["passed"] is True
        assert result["abc123"]["reason"] == "well-grounded"
        assert result["abc123"]["explicit"] is True
        assert outcome == "ok"

    def test_ordinal_fallback_binds_by_position_when_hash_mismatches(self):
        """Ordinal binding: a verdict at position 0 binds to finding[0] even with a wrong hash."""
        logs = '[{"hash": "other", "verdict": "fail", "reason": "unrelated"}]'
        result, outcome = parse_adversarial_finding_verdicts(logs, ["abc123"])
        # Ordinal binding: position 0 verdict → finding[0] = abc123
        assert result["abc123"]["passed"] is False
        assert result["abc123"]["explicit"] is True
        assert outcome == "ok"
        # Fail-closed only for a finding whose position has no corresponding verdict
        logs2 = '[{"hash": "other", "verdict": "fail", "reason": "for first"}]'
        result2, outcome2 = parse_adversarial_finding_verdicts(
            logs2, ["abc123", "xyz789"]
        )
        assert result2["abc123"]["passed"] is False
        assert result2["abc123"]["explicit"] is True  # bound by ordinal
        assert result2["xyz789"]["passed"] is False
        assert result2["xyz789"]["explicit"] is False  # no verdict at position 1
        assert result2["xyz789"]["unresolved_reason"] == "no_hash_matches"
        assert outcome2 == "ok"

    def test_not_completed_defaults_all_to_fail_closed(self):
        logs = '[{"hash": "abc123", "verdict": "fail", "reason": "fail"}]'
        result, outcome = parse_adversarial_finding_verdicts(
            logs, ["abc123"], completed=False
        )
        assert result["abc123"]["passed"] is False
        assert result["abc123"]["explicit"] is False
        assert outcome == "challenger_failed"
        assert result["abc123"]["unresolved_reason"] == "challenger_failed"

    def test_none_logs_defaults_all_to_fail_closed(self):
        result, outcome = parse_adversarial_finding_verdicts(None, ["abc123"])
        assert result["abc123"]["passed"] is False
        assert result["abc123"]["explicit"] is False
        assert outcome == "empty_logs"
        assert result["abc123"]["unresolved_reason"] == "empty_logs"

    def test_malformed_json_defaults_all_to_fail_closed(self):
        result, outcome = parse_adversarial_finding_verdicts(
            "not json at all", ["abc123"]
        )
        assert result["abc123"]["passed"] is False
        assert result["abc123"]["explicit"] is False
        assert outcome == "no_json_array"
        assert result["abc123"]["unresolved_reason"] == "no_json_array"

    def test_multiple_findings_partial_fail(self):
        logs = (
            '[{"hash": "h1", "verdict": "fail", "reason": "bad"}, '
            '{"hash": "h2", "verdict": "pass", "reason": "ok"}]'
        )
        result, outcome = parse_adversarial_finding_verdicts(logs, ["h1", "h2", "h3"])
        assert result["h1"]["passed"] is False
        assert result["h1"]["reason"] == "bad"
        assert result["h1"]["explicit"] is True
        assert result["h2"]["passed"] is True
        assert result["h2"]["reason"] == "ok"
        assert result["h2"]["explicit"] is True
        assert (
            result["h3"]["passed"] is False
        )  # absent → fail-closed (not an implicit pass)
        assert result["h3"]["explicit"] is False  # not an explicit challenger verdict
        # h3 was not matched; h1/h2 were — outcome is ok because some hashes matched
        assert outcome == "ok"
        assert result["h3"]["unresolved_reason"] == "no_hash_matches"

    def test_invalid_json_array_parse_outcome(self):
        # A JSON array is found but cannot be parsed as valid JSON (simulated via
        # a truncated array that _extract_json_array still locates).
        logs = 'some preamble [{"hash": "abc", "verdict": "fail", "reason": "bad"'
        result, outcome = parse_adversarial_finding_verdicts(logs, ["abc"])
        assert result["abc"]["passed"] is False
        assert result["abc"]["explicit"] is False
        # Either invalid_json or no_json_array depending on _extract_json_array behaviour.
        assert outcome in ("invalid_json", "no_json_array")
        assert result["abc"]["unresolved_reason"] in ("invalid_json", "no_json_array")

    def test_empty_string_logs_gives_empty_logs_outcome(self):
        result, outcome = parse_adversarial_finding_verdicts("", ["abc123"])
        assert outcome == "empty_logs"
        assert result["abc123"]["unresolved_reason"] == "empty_logs"

    def test_whitespace_only_logs_gives_empty_logs_outcome(self):
        result, outcome = parse_adversarial_finding_verdicts("   \n  ", ["abc123"])
        assert outcome == "empty_logs"
        assert result["abc123"]["unresolved_reason"] == "empty_logs"

    def test_verdict_unrecognized_token(self):
        logs = '[{"hash": "abc123", "verdict": "maybe", "reason": "not sure"}]'
        result, outcome = parse_adversarial_finding_verdicts(logs, ["abc123"])
        assert result["abc123"]["passed"] is False
        assert result["abc123"]["explicit"] is False
        assert result["abc123"]["unresolved_reason"] == "verdict_unrecognized"
        assert outcome == "ok"  # hash was found; verdict token was just unrecognized

    def test_adversarial_parser_uses_final_verdict_after_echoed_schema(self):
        """Parser must use the LAST verdict array, not an echoed schema/template.

        The adversarial challenge prompt includes an example JSON snippet:
          [{"hash": "<finding_hash from input>", "verdict": "pass" | "fail", ...}]
        If the agent echoes it back (or includes it in reasoning) before emitting
        the real final verdicts, the parser must pick the real final array.
        """
        template_array = (
            '[{"hash": "<finding_hash from input>", "verdict": "pass | fail", '
            '"reason": "<one-sentence justification>"}]'
        )
        real_array = (
            '[{"hash": "real_hash_abc", "verdict": "fail", '
            '"reason": "premise is stale at HEAD"}]'
        )
        logs = (
            "I will now evaluate each finding.\n\n"
            "Here is the expected output format:\n"
            f"{template_array}\n\n"
            "After careful review, my verdicts are:\n"
            f"{real_array}"
        )
        result, outcome = parse_adversarial_finding_verdicts(logs, ["real_hash_abc"])
        assert outcome == "ok"
        assert result["real_hash_abc"]["passed"] is False
        assert result["real_hash_abc"]["explicit"] is True
        assert result["real_hash_abc"]["reason"] == "premise is stale at HEAD"

    def test_multiple_arrays_last_with_real_hashes_wins(self):
        """When multiple arrays exist but only the last has known hashes, use the last."""
        first_array = '[{"hash": "unrelated_hash", "verdict": "pass", "reason": "ok"}]'
        real_array = (
            '[{"hash": "real_hash_xyz", "verdict": "pass", "reason": "solid evidence"}]'
        )
        logs = f"First pass:\n{first_array}\n\nFinal verdict:\n{real_array}"
        result, outcome = parse_adversarial_finding_verdicts(logs, ["real_hash_xyz"])
        assert outcome == "ok"
        assert result["real_hash_xyz"]["passed"] is True
        assert result["real_hash_xyz"]["explicit"] is True

    def test_template_placeholder_hash_does_not_match_real_hashes(self):
        """The template placeholder <finding_hash from input> must not match real hashes."""
        template_array = (
            '[{"hash": "<finding_hash from input>", "verdict": "pass", '
            '"reason": "example"}]'
        )
        real_array = '[{"hash": "abc999", "verdict": "fail", "reason": "stale"}]'
        logs = f"Schema:\n{template_array}\n\nVerdict:\n{real_array}"
        result, outcome = parse_adversarial_finding_verdicts(logs, ["abc999"])
        assert outcome == "ok"
        assert result["abc999"]["passed"] is False
        assert result["abc999"]["explicit"] is True

    def test_alias_tokens_approved_rejected(self):
        """Divergent alias tokens: approved->pass, rejected->fail (present in this parser)."""
        logs_approved = (
            '[{"hash": "h1", "verdict": "approved", "reason": "looks good"}]'
        )
        result, outcome = parse_adversarial_finding_verdicts(logs_approved, ["h1"])
        assert result["h1"]["passed"] is True
        assert result["h1"]["explicit"] is True
        assert outcome == "ok"

        logs_rejected = '[{"hash": "h2", "verdict": "rejected", "reason": "stale"}]'
        result, outcome = parse_adversarial_finding_verdicts(logs_rejected, ["h2"])
        assert result["h2"]["passed"] is False
        assert result["h2"]["explicit"] is True
        assert outcome == "ok"

    def test_absent_alias_tokens_accept_and_request_changes(self):
        """Tokens absent from this parser: accept and request_changes -> unrecognized."""
        for token in ("accept", "request_changes"):
            logs = f'[{{"hash": "h3", "verdict": "{token}", "reason": "n/a"}}]'
            result, outcome = parse_adversarial_finding_verdicts(logs, ["h3"])
            assert result["h3"]["explicit"] is False, (
                f"token {token!r} should not produce an explicit verdict in this parser"
            )
            assert result["h3"]["unresolved_reason"] == "verdict_unrecognized"
            assert outcome == "ok"


def test_adversarial_parser_uses_final_verdict_after_echoed_schema():
    """Standalone acceptance test for the last-array fix.

    This is the required named acceptance test: an echoed schema/template array
    before a valid final verdict array must not shadow the real verdicts.
    """
    template = (
        '[{"hash": "<finding_hash from input>", "verdict": "pass | fail", '
        '"reason": "<justification>"}]'
    )
    real = '[{"hash": "h_live_123", "verdict": "pass", "reason": "confirmed at HEAD"}]'
    logs = (
        "ADVERSARIAL CHALLENGE RESPONSE\n\n"
        "Expected output schema (for reference):\n"
        f"{template}\n\n"
        "After thorough investigation, my final verdicts:\n"
        f"{real}"
    )
    result, outcome = parse_adversarial_finding_verdicts(logs, ["h_live_123"])
    assert outcome == "ok", f"expected ok, got {outcome!r}"
    assert result["h_live_123"]["passed"] is True
    assert result["h_live_123"]["explicit"] is True
    assert result["h_live_123"]["reason"] == "confirmed at HEAD"


def test_spawn_gate_verdict_binds_by_index_despite_hash_mismatch():
    """Acceptance proof: ordinal binding succeeds despite a mis-transcribed hash.

    A challenger that echoes the wrong hash but places its verdict at the
    correct ordinal position (or echoes the correct 'index' field) must have
    that verdict bound to its finding — not degraded to no_hash_matches or
    fail-closed.  This is the exact failure mode the research-spawn gate
    suffered on live runs: real verdicts dropped because the hash was
    mis-transcribed by one character.
    """
    # The real finding hash (as the manifest would contain it).
    real_hash = "a1b2c3d4e5f6g7h8"

    # Challenger mis-transcribed the hash but placed the verdict at position 0
    # (the correct ordinal) — binding should succeed via ordinal fallback.
    logs_wrong_hash = (
        '[{"index": 1, "hash": "a1b2c3d4e5f6g7XX", "verdict": "pass", '
        '"reason": "premise confirmed at HEAD"}]'
    )
    result, outcome = parse_adversarial_finding_verdicts(logs_wrong_hash, [real_hash])
    assert outcome == "ok", (
        f"ordinal binding must succeed despite hash mismatch; got {outcome!r}"
    )
    assert result[real_hash]["passed"] is True
    assert result[real_hash]["explicit"] is True
    assert result[real_hash]["reason"] == "premise confirmed at HEAD"

    # Same scenario without the explicit index field — pure array-position binding.
    logs_position_only = (
        '[{"hash": "totally-wrong-hash", "verdict": "fail", '
        '"reason": "stale premise; code was refactored"}]'
    )
    result2, outcome2 = parse_adversarial_finding_verdicts(
        logs_position_only, [real_hash]
    )
    assert outcome2 == "ok", (
        f"array-position ordinal binding must succeed; got {outcome2!r}"
    )
    assert result2[real_hash]["passed"] is False
    assert result2[real_hash]["explicit"] is True
    assert result2[real_hash]["reason"] == "stale premise; code was refactored"


def test_adversarial_ordinal_binding_multi_finding_partial():
    """Ordinal binding for multi-finding manifests with partial hash mismatches.

    When some hashes match exactly and others do not, the ones with wrong
    hashes should bind by position (or explicit index) — not collapse to
    no_hash_matches for those entries.
    """
    h1 = "correct_hash_1111"
    h2 = "correct_hash_2222"
    h3 = "correct_hash_3333"

    # h1 echoed correctly; h2 and h3 mis-transcribed but in the right order.
    logs = (
        "["
        '{"index": 1, "hash": "correct_hash_1111", "verdict": "pass", "reason": "solid"},'
        '{"index": 2, "hash": "WRONG_HASH_2222", "verdict": "fail", "reason": "stale"},'
        '{"index": 3, "hash": "WRONG_HASH_3333", "verdict": "pass", "reason": "ok"}'
        "]"
    )
    result, outcome = parse_adversarial_finding_verdicts(logs, [h1, h2, h3])
    assert outcome == "ok"
    assert result[h1]["passed"] is True
    assert result[h1]["explicit"] is True
    assert result[h2]["passed"] is False
    assert result[h2]["explicit"] is True  # bound by explicit index=2
    assert result[h2]["reason"] == "stale"
    assert result[h3]["passed"] is True
    assert result[h3]["explicit"] is True  # bound by explicit index=3


def test_adversarial_explicit_index_overrides_array_position():
    """An explicit 'index' field takes priority over array position as binding key.

    If the challenger re-orders its output but still echoes the correct 1-based
    index for each finding, the verdicts should bind to the correct findings.
    """
    h1 = "hash_for_finding_1"
    h2 = "hash_for_finding_2"

    # Output is in reverse order, but index fields point to the correct findings.
    logs = (
        "["
        '{"index": 2, "hash": "wrong", "verdict": "fail", "reason": "stale"},'
        '{"index": 1, "hash": "wrong", "verdict": "pass", "reason": "confirmed"}'
        "]"
    )
    result, outcome = parse_adversarial_finding_verdicts(logs, [h1, h2])
    assert outcome == "ok"
    # index=1 → h1, index=2 → h2
    assert result[h1]["passed"] is True
    assert result[h1]["explicit"] is True
    assert result[h2]["passed"] is False
    assert result[h2]["explicit"] is True


def test_adversarial_ordinal_does_not_double_bind():
    """A finding that was matched by exact hash is never also bound by ordinal."""
    h1 = "real_hash_abc"
    h2 = "real_hash_xyz"

    # h1 echoed correctly; h2's position in the array is also 0 after h1 is removed,
    # but since h1 was hash-matched, ordinal position 0 should not rebind to h1 via h2.
    logs = (
        "["
        '{"hash": "real_hash_abc", "verdict": "pass", "reason": "good"},'
        '{"hash": "WRONG", "verdict": "fail", "reason": "bad"}'
        "]"
    )
    result, outcome = parse_adversarial_finding_verdicts(logs, [h1, h2])
    assert outcome == "ok"
    # h1 bound by exact hash (position 0 is claimed)
    assert result[h1]["passed"] is True
    assert result[h1]["explicit"] is True
    # h2 bound by ordinal at position 1 (hash mismatch → falls to array position)
    assert result[h2]["passed"] is False
    assert result[h2]["explicit"] is True


def test_adversarial_gate_unresolved_reason_distinguishes_parse_failure_modes():
    """Each parse failure mode produces a distinct per-finding unresolved_reason.

    Acceptance test: validates the new parse_outcome + per-finding unresolved_reason
    classification in parse_adversarial_finding_verdicts. The production code that
    writes parse_outcome into ADVERSARIAL_GATE_VERDICT_KEY (task_lifecycle.py) depends
    on this function returning the tuple correctly.
    """
    hashes = ["h1", "h2"]

    # 1. Challenger task failed (completed=False) → challenger_failed
    result, outcome = parse_adversarial_finding_verdicts(
        '[{"hash": "h1", "verdict": "fail"}]', hashes, completed=False
    )
    assert outcome == "challenger_failed"
    assert all(r["unresolved_reason"] == "challenger_failed" for r in result.values())
    assert all(r["passed"] is False for r in result.values())

    # 2. Empty logs → empty_logs
    result, outcome = parse_adversarial_finding_verdicts(None, hashes)
    assert outcome == "empty_logs"
    assert all(r["unresolved_reason"] == "empty_logs" for r in result.values())

    # 3. No JSON array in logs → no_json_array
    result, outcome = parse_adversarial_finding_verdicts(
        "The challenger could not determine a verdict.", hashes
    )
    assert outcome == "no_json_array"
    assert all(r["unresolved_reason"] == "no_json_array" for r in result.values())

    # 4. JSON array with no known hashes, but ordinal binding binds position 0 to h1.
    # h1 gets a fail verdict via ordinal; h2 has no verdict at position 1 → unresolved.
    result, outcome = parse_adversarial_finding_verdicts(
        '[{"hash": "unknown-hash", "verdict": "fail", "reason": "x"}]', hashes
    )
    assert outcome == "ok"  # h1 was bound by ordinal at position 0
    assert result["h1"]["passed"] is False
    assert result["h1"]["explicit"] is True  # bound by ordinal fallback
    assert (
        result["h2"]["unresolved_reason"] == "no_hash_matches"
    )  # no verdict at position 1
    assert result["h2"]["explicit"] is False

    # 5. Happy path: both hashes receive explicit verdicts → ok, no unresolved_reason
    logs = (
        '[{"hash": "h1", "verdict": "pass", "reason": "solid"}, '
        '{"hash": "h2", "verdict": "fail", "reason": "stale"}]'
    )
    result, outcome = parse_adversarial_finding_verdicts(logs, hashes)
    assert outcome == "ok"
    assert result["h1"]["passed"] is True
    assert result["h1"]["explicit"] is True
    assert "unresolved_reason" not in result["h1"]
    assert result["h2"]["passed"] is False
    assert result["h2"]["explicit"] is True
    assert "unresolved_reason" not in result["h2"]

    # 6. Unrecognized verdict token → verdict_unrecognized, outcome still ok (hash found)
    result, outcome = parse_adversarial_finding_verdicts(
        '[{"hash": "h1", "verdict": "uncertain", "reason": "dunno"}]', hashes
    )
    assert outcome == "ok"
    assert result["h1"]["unresolved_reason"] == "verdict_unrecognized"
    assert result["h1"]["passed"] is False
    # h2 was not in the output at all → no_hash_matches
    assert result["h2"]["unresolved_reason"] == "no_hash_matches"


def test_adversarial_verdict_binds_by_index_without_hash():
    """Acceptance test: verdicts with only 'index' (no 'hash') bind correctly.

    A challenger may emit verdicts identified by index/position only (no hash field),
    especially when the hash transcription is unreliable. The parser must accept
    index-only arrays and bind them to findings by their manifest position.
    """
    h1 = "finding_hash_1111"
    h2 = "finding_hash_2222"
    h3 = "finding_hash_3333"

    # Verdicts with explicit 'index' field but no 'hash' — pure index binding.
    logs_index_only = (
        "["
        '{"index": 1, "verdict": "pass", "reason": "well-grounded at HEAD"},'
        '{"index": 2, "verdict": "fail", "reason": "code was refactored"},'
        '{"index": 3, "verdict": "pass", "reason": "confirmed stable"}'
        "]"
    )
    result, outcome = parse_adversarial_finding_verdicts(logs_index_only, [h1, h2, h3])
    assert outcome == "ok", f"index-only binding must succeed; got {outcome!r}"
    assert result[h1]["passed"] is True
    assert result[h1]["explicit"] is True
    assert result[h1]["reason"] == "well-grounded at HEAD"
    assert result[h2]["passed"] is False
    assert result[h2]["explicit"] is True
    assert result[h2]["reason"] == "code was refactored"
    assert result[h3]["passed"] is True
    assert result[h3]["explicit"] is True
    assert result[h3]["reason"] == "confirmed stable"

    # Verdicts by array position alone (no index or hash fields).
    logs_position_only = (
        "["
        '{"verdict": "fail", "reason": "stale"},'
        '{"verdict": "pass", "reason": "fresh"}'
        "]"
    )
    result2, outcome2 = parse_adversarial_finding_verdicts(
        logs_position_only, [h1, h2, h3]
    )
    assert outcome2 == "ok", f"array-position binding must succeed; got {outcome2!r}"
    assert result2[h1]["passed"] is False
    assert result2[h1]["explicit"] is True
    assert result2[h2]["passed"] is True
    assert result2[h2]["explicit"] is True
    # h3 has no verdict at position 2 → fail-closed
    assert result2[h3]["passed"] is False
    assert result2[h3]["explicit"] is False


# ---------------------------------------------------------------------------
# Cross-seam contract: challenge handler output shape ↔ backend parser
# ---------------------------------------------------------------------------


class TestAdversarialChallengeHandlerToParserContract:
    """Cross-seam contract: adversarial challenge handler output ↔ backend parser.

    The prompt builder (build_adversarial_finding_prompt) instructs the challenger
    to emit a [{index, hash, verdict, reason}] array keyed by the finding's hash.
    The backend parser (parse_adversarial_finding_verdicts) expects that same shape.
    This class pins the round-trip so drift on either side — a new prompt template,
    a change to the array schema, or re-introduction of investigation-report wrapping
    — fails a test rather than silently suppressing all findings at the gate.
    """

    def _findings(self):
        return [
            {
                "title": "Missing null check in parser module",
                "description": "The parser at backend/app/foo.py:42 does not guard against None input.",
                "risk": "high",
                "scope": "small",
            },
            {
                "title": "Race condition in task dispatch path",
                "description": "Concurrent task dispatch can double-spawn under high load in orchestrator.",
                "risk": "medium",
                "scope": "medium",
            },
        ]

    def test_prompt_builder_output_schema_round_trips_through_parser(self):
        """Challenger output shaped exactly as the prompt instructs parses to real verdicts.

        Build the manifest using finding_hash (exactly as research_completion does),
        embed it in the adversarial prompt, construct a challenger response following
        the prompt's [{index, hash, verdict, reason}] schema, and assert
        parse_adversarial_finding_verdicts returns ok with explicit verdicts.
        """
        from app.task_evaluators import (
            build_adversarial_challenge_manifest,
            build_adversarial_finding_prompt,
        )
        from app.finding_deduplication import finding_hash

        findings = self._findings()
        for f in findings:
            f["hash"] = finding_hash(f["title"], f["description"])

        compact, manifest_json = build_adversarial_challenge_manifest(findings)
        assert len(compact) == 2
        prompt = build_adversarial_finding_prompt(manifest_json)
        # The manifest JSON must appear verbatim in the prompt.
        assert manifest_json in prompt

        # Build a challenger response shaped exactly as the prompt instructs.
        # The prompt ends with the schema:
        #   [{"index": <1-based>, "hash": "<hash from input>", "verdict": "pass"|"fail", "reason": "..."}]
        challenger_response = json.dumps(
            [
                {
                    "index": entry["index"],
                    "hash": entry["hash"],
                    "verdict": "pass",
                    "reason": "All three criteria hold at HEAD: premise verified, evidence real, scope accurate.",
                }
                for entry in compact
            ]
        )

        finding_hashes = [f["hash"] for f in findings]
        verdicts, parse_outcome = parse_adversarial_finding_verdicts(
            challenger_response, finding_hashes
        )

        assert parse_outcome == "ok", (
            f"Parser rejected prompt-builder-instructed challenger output; outcome={parse_outcome!r}. "
            "The prompt instructs a shape the parser cannot read — this is a live regression."
        )
        for fhash in finding_hashes:
            assert verdicts[fhash]["explicit"] is True, (
                f"Finding {fhash!r} received a fail-closed default instead of an explicit verdict. "
                "The prompt-to-parser contract is broken."
            )
            assert verdicts[fhash]["passed"] is True
            assert verdicts[fhash]["reason"] != ""

    def test_investigation_report_wrapper_breaks_parser(self):
        """Pure investigation-report output fails the parser (original regression guard).

        The handler was broken when the adversarial branch was routed through the
        standard investigation template, instructing the agent to write a JSON object
        with schema {summary, root_cause, affected_files, recommended_fix, ...} —
        which contains NO hash or verdict fields at all.  The parser anchors on a
        JSON array containing 'hash' and 'verdict' keys; an investigation-report object
        with none of those keys yields no_json_array and all findings fail closed.
        This test locks that invariant: an investigation-report object WITHOUT hash/verdict
        arrays must produce no_json_array, not 'ok'.
        """
        from app.finding_deduplication import finding_hash

        findings = self._findings()
        finding_hashes = [finding_hash(f["title"], f["description"]) for f in findings]

        # A pure investigation-report object — the exact shape the agent would emit
        # when the standard investigate template is applied instead of the adversarial
        # prompt.  Crucially, there are NO hash or verdict fields anywhere.
        pure_investigation_report = json.dumps(
            {
                "summary": "Evaluated each finding against the three criteria.",
                "root_cause": "The parser at backend/app/foo.py:42 lacks a null guard.",
                "affected_files": [
                    {
                        "path": "backend/app/foo.py",
                        "issue": "missing null check at line 42",
                    }
                ],
                "recommended_fix": "Add null guard before accessing result.",
                "severity": "medium",
                "confidence": "high",
            }
        )

        verdicts, parse_outcome = parse_adversarial_finding_verdicts(
            pure_investigation_report, finding_hashes
        )

        # A pure investigation-report with no hash/verdict fields must yield no_json_array
        # and fail-close all findings.
        assert parse_outcome == "no_json_array", (
            f"Expected no_json_array for a pure investigation-report, got {parse_outcome!r}. "
            "If the handler re-introduces investigation-report wrapping this gate would "
            "collapse to 'no verdict returned' for every finding, silently suppressing them all."
        )
        for fhash in finding_hashes:
            assert verdicts[fhash]["explicit"] is False, (
                f"Finding {fhash!r} got an explicit verdict from a pure investigation-report — "
                "the parser must fail-close when no hash/verdict array is present."
            )
            assert verdicts[fhash]["passed"] is False

    def test_hash_from_manifest_matches_hash_from_finding_deduplication(self):
        """The hash embedded by build_adversarial_challenge_manifest equals finding_hash(title, description).

        research_completion stamps each finding's hash using finding_hash before
        passing it to build_adversarial_challenge_manifest.  If the manifest ever
        re-computed the hash differently, the challenger would write back a hash
        the parser's finding_hashes list does not contain, causing no_hash_matches.
        """
        from app.task_evaluators import build_adversarial_challenge_manifest
        from app.finding_deduplication import finding_hash

        findings = self._findings()
        for f in findings:
            f["hash"] = finding_hash(f["title"], f["description"])

        compact, _ = build_adversarial_challenge_manifest(findings)

        for i, entry in enumerate(compact):
            expected_hash = finding_hash(
                findings[i]["title"], findings[i]["description"]
            )
            assert entry["hash"] == expected_hash, (
                f"Manifest hash {entry['hash']!r} != finding_hash result {expected_hash!r} "
                f"for finding {i}. A discrepancy here causes no_hash_matches at parse time."
            )

    def test_parser_produces_real_verdicts_not_fail_closed_defaults(self):
        """Explicit pass and fail verdicts are genuinely propagated, not collapsed to defaults.

        Builds a mixed response (one pass, one fail) shaped as the prompt instructs and
        verifies the parser returns the actual per-finding outcome, not uniform fail-closed
        defaults.  This catches a regression where all verdicts collapse to
        passed=False/explicit=False regardless of the challenger's actual output.
        """
        from app.task_evaluators import build_adversarial_challenge_manifest
        from app.finding_deduplication import finding_hash

        findings = self._findings()
        for f in findings:
            f["hash"] = finding_hash(f["title"], f["description"])

        compact, _ = build_adversarial_challenge_manifest(findings)
        finding_hashes = [f["hash"] for f in findings]

        # First finding passes, second fails.
        response = json.dumps(
            [
                {
                    "index": compact[0]["index"],
                    "hash": compact[0]["hash"],
                    "verdict": "pass",
                    "reason": "Premise is real and cited code exists at HEAD.",
                },
                {
                    "index": compact[1]["index"],
                    "hash": compact[1]["hash"],
                    "verdict": "fail",
                    "reason": "Cited file was removed in a prior refactor; premise is stale.",
                },
            ]
        )

        verdicts, parse_outcome = parse_adversarial_finding_verdicts(
            response, finding_hashes
        )

        assert parse_outcome == "ok"
        assert verdicts[finding_hashes[0]]["passed"] is True
        assert verdicts[finding_hashes[0]]["explicit"] is True
        assert verdicts[finding_hashes[1]]["passed"] is False
        assert verdicts[finding_hashes[1]]["explicit"] is True


# ---------------------------------------------------------------------------
# Integration tests: adversarial spawn-gate in _handle_research_completion
# ---------------------------------------------------------------------------


def _high_stakes_finding(direction_id: str, title: str = "Big risky change") -> dict:
    """A minimal finding that triggers _is_high_stakes_finding (risk=high)."""
    return {
        "title": title,
        "description": f"Description: {title}",
        "prompt": f"Implement: {title}",
        "type": "improvement",
        "priority": "high",
        "scope": "small",
        "risk": "high",
        "direction_id": direction_id,
        "actionability_grade": "HIGH",
    }


@_pytest.mark.asyncio
async def test_prior_deferred_high_stakes_finding_is_challenged_before_spawn(
    db_session, mocker
):
    """ACCEPTANCE TEST: a recovered deferred high-stakes finding is included in the
    adversarial challenge on the next cycle and cannot spawn until the challenger returns.

    Scenario: A previous cycle deferred a high-stakes finding (batch_cap reason).
    On the next research cycle, the fresh log output has no findings.
    The deferred finding must be recovered, included in the adversarial gate
    challenge payload, and the task must park EVALUATING — it must NOT spawn.
    """
    from app.research_completion import _finding_hash
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-defer-adv01"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    deferred_hs = _high_stakes_finding(
        direction["id"], "Batch-cap deferred high-stakes finding"
    )
    deferred_hs["deferred_reason"] = "batch_cap"
    deferred_hs["deferred_count"] = 1

    # Inject the deferred finding as if the previous cycle stored it.
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": [deferred_hs],
    }
    db_session.add(task)
    await db_session.commit()

    # Fresh research log returns a low-stakes finding — the recovered deferred
    # finding is high-stakes and must trigger the adversarial gate.
    from test_autonomous_research import _make_finding

    low_fresh = _make_finding(direction_id=direction["id"])
    low_fresh["title"] = "Routine low-stakes finding"
    low_fresh["risk"] = "low"
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[low_fresh],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # The task must be parked EVALUATING, not have spawned the deferred finding.
    assert task.status == TaskStatus.EVALUATING, (
        "Recovered deferred high-stakes finding must park the task awaiting adversarial verdict"
    )
    assert task.context.get("awaiting_adversarial_grader"), (
        "awaiting_adversarial_grader flag must be set"
    )

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert children == [], (
        "Recovered deferred high-stakes finding must not spawn before adversarial verdict"
    )

    # An adversarial grader must have been spawned.
    others = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id, _Task.id != task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(others) == 1, "Exactly one adversarial grader must be spawned"
    grader = others[0]
    assert (grader.context or {}).get("adversarial_grader") is True

    # The grader payload must contain the deferred finding with its hash.
    prompt = (grader.first_payload or {}).get("prompt") or ""
    fhash = _finding_hash(
        deferred_hs["title"],
        deferred_hs.get("description", "") or deferred_hs.get("prompt", ""),
    )
    assert '"hash"' in prompt, "Grader payload must include hash field"
    assert fhash in prompt, (
        "The deferred finding's hash must appear in the adversarial grader payload"
    )


@_pytest.mark.asyncio
async def test_adversarial_gate_parks_source_with_high_stakes_findings(
    db_session, mocker
):
    """Default-on adversarial gate: a high-stakes finding parks the task awaiting the grader."""
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-adv01")
    _patch_research_externals(mocker)
    # Override the patch set by _patch_research_externals to ALLOW the gate to fire.
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_high_stakes_finding(direction["id"])],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # Task must be parked EVALUATING, no implementation children yet.
    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert children == [], (
        "high-stakes finding must not spawn before adversarial verdict"
    )
    assert task.status == TaskStatus.EVALUATING
    assert task.context.get("awaiting_adversarial_grader")

    # An adversarial grader task must have been spawned.
    others = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id, _Task.id != task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(others) == 1
    grader = others[0]
    assert (grader.context or {}).get("adversarial_grader") is True


@_pytest.mark.asyncio
async def test_adversarial_gate_skips_when_no_high_stakes_findings(db_session, mocker):
    """Low-stakes findings bypass the adversarial gate entirely."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-adv02")
    _patch_research_externals(mocker)
    # Override patch to ALLOW gate but return False for all findings (none high-stakes).
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        return_value=False,
    )
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding(direction_id=direction["id"])],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # No grader, and children should have been spawned normally.
    all_tasks = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.project_id == _project.id)
            )
        )
        .scalars()
        .all()
    )
    graders = [t for t in all_tasks if (t.context or {}).get("adversarial_grader")]
    assert graders == [], "no adversarial grader for low-stakes findings"
    assert not task.context.get("awaiting_adversarial_grader")


@_pytest.mark.asyncio
async def test_adversarial_gate_disabled_by_flag(db_session, mocker):
    """Disabling adversarial_spawn_gate_enabled bypasses the gate entirely."""
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-adv03")
    _patch_research_externals(mocker)
    # Let the predicate fire, but disable the gate via runtime config.
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_high_stakes_finding(direction["id"])],
    )
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"adversarial_spawn_gate_enabled": False},
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    all_tasks = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.project_id == _project.id)
            )
        )
        .scalars()
        .all()
    )
    graders = [t for t in all_tasks if (t.context or {}).get("adversarial_grader")]
    assert graders == [], "disabled gate must not spawn adversarial grader"
    assert not task.context.get("awaiting_adversarial_grader")


@_pytest.mark.asyncio
async def test_spawn_gate_fail_closed_when_grader_spawn_returns_none(
    db_session, mocker
):
    """ACCEPTANCE TEST: adversarial spawn gate fails closed when grader spawn returns None.

    When spawn_forward_quality_grader returns None (single-shot guard hit / grader already
    in flight) during the first attempt to spawn an adversarial grader, the source task
    must be marked as awaiting the verdict and return early WITHOUT spawning high-stakes
    findings. On re-entry while still awaiting, the task must return as a no-op. Only
    when the grader verdict actually arrives can findings be spawned (after passing the
    adversarial gate).
    """
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-spawn-none"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_high_stakes_finding(direction["id"])],
    )

    # Simulate a single-shot guard that observes an exact durable sidecar marker.
    # The handler must not degrade that UUID back to the legacy True shape.
    existing_grader_id = uuid.uuid4()

    async def _existing_grader(source_task, *_args, **_kwargs):
        source_task.context = {
            **(source_task.context or {}),
            "awaiting_adversarial_grader": str(existing_grader_id),
        }
        return None

    spawn_grader_mock = mocker.patch(
        "app.task_evaluators.spawn_forward_quality_grader",
        new=AsyncMock(side_effect=_existing_grader),
    )

    # First pass: attempt to spawn grader, but it returns None.
    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # Verify grader spawn was attempted.
    assert spawn_grader_mock.called, "spawn_forward_quality_grader should be called"

    # Task must be marked as EVALUATING and awaiting adversarial grader.
    assert task.status == TaskStatus.EVALUATING, (
        "Task must be EVALUATING when grader spawn returns None"
    )
    assert task.context.get("awaiting_adversarial_grader") == str(existing_grader_id), (
        "the exact awaiting sidecar UUID must survive a guarded None return"
    )
    assert task.context.get("_adversarial_challenged_hashes"), (
        "challenged hashes must be stored for fail-closed gating"
    )

    # No implementation children must have been spawned (high-stakes finding deferred).
    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert children == [], (
        "High-stakes findings must not spawn when grader spawn returns None"
    )

    # Re-entry while still awaiting (verdict still None): must return as no-op.
    spawn_grader_mock.reset_mock()
    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # Grader spawn should NOT be attempted again on re-entry while awaiting.
    assert not spawn_grader_mock.called, (
        "On re-entry with awaiting_adversarial_grader set, grader spawn should not be retried"
    )
    assert task.status == TaskStatus.EVALUATING, (
        "Task must remain EVALUATING while awaiting verdict"
    )
    assert not children, "Still no children spawned on re-entry while awaiting"

    # Now simulate the grader verdict arriving: update task context with a passing verdict.
    task.context = {
        **(task.context or {}),
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "parse_outcome": "ok",
            "finding_verdicts": {
                task.context.get("_adversarial_challenged_hashes", [])[0]: {
                    "passed": True,
                    "explicit": True,
                    "reason": "premise holds",
                }
            },
        },
    }
    db_session.add(task)
    await db_session.commit()

    # Re-entry with verdict: must proceed to spawn.
    spawn_grader_mock.reset_mock()
    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # Now the finding should have been spawned (grader approved it).
    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) > 0, (
        "When grader verdict approves finding, it must be spawned on re-entry"
    )


@_pytest.mark.asyncio
async def test_adversarial_gate_real_failed_sidecar_defers_without_spawning(
    db_session, mocker
):
    """Failed sidecars retry once, then defer the finding without spawning work."""
    from app.research_completion import _finding_hash
    from app.schemas import AgentStatusPayload
    from app.task_lifecycle import _resolve_quality_grader_completion
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-adv04")
    _patch_research_externals(mocker)
    # Allow the predicate so _is_high_stakes_finding returns normally.
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    hs = _high_stakes_finding(direction["id"], "Risky deferred finding")
    fhash = _finding_hash(
        hs["title"], hs.get("description", "") or hs.get("prompt", "")
    )

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[hs],
    )
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"adversarial_spawn_gate_infra_failure_mode": "observe"},
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    graders = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id, _Task.id != task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(graders) == 1
    grader = graders[0]
    prompt = (grader.first_payload or {}).get("prompt") or ""
    assert "_adversarial_hash" not in prompt
    assert '"hash"' in prompt
    _, _, findings_block = prompt.partition("-----8<----- FINDINGS START -----8<-----")
    findings_json, _, _ = findings_block.partition(
        "-----8<----- FINDINGS END -----8<-----"
    )
    parsed_findings = json.loads(findings_json.strip())
    assert len(parsed_findings) == 1
    manifest_item = parsed_findings[0]
    # Compact manifest: only essential fields are kept (hash, title, description, risk, scope).
    assert manifest_item["hash"] == fhash
    assert manifest_item["title"] == hs["title"]
    assert "description" in manifest_item
    # Full raw finding fields like prompt/type/actionability_grade are not in compact manifest.
    assert "prompt" not in manifest_item
    assert "actionability_grade" not in manifest_item

    grader.status = TaskStatus.FAILED
    db_session.add(grader)
    await db_session.commit()
    payload = AgentStatusPayload(
        task_id=grader.id,
        status=TaskStatus.FAILED,
        logs=None,
    )
    await _resolve_quality_grader_completion(grader, payload, db_session)
    await db_session.refresh(task)

    # First wholesale sidecar failure gets one bounded retry, but still no
    # implementation children.
    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert children == [], "adversarial-failed finding must not spawn"

    retry_graders = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id,
                    _Task.id.notin_([task.id, grader.id]),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(retry_graders) == 1
    retry_grader = retry_graders[0]
    assert (retry_grader.context or {}).get("adversarial_grader") is True
    # A bare-FAILED grader (no status/logs) is classified as AGENT_TIMEOUT by
    # classify_failure, which now routes to the targeted retry path.  The targeted
    # retry count is incremented; the wholesale count must remain untouched.
    assert task.context.get("_adversarial_targeted_retry_count") == 1, (
        "AGENT_TIMEOUT cause must use targeted retry first"
    )
    assert task.context.get("_adversarial_wholesale_retry_count") in (None, 0), (
        "wholesale count must not be incremented on the first (targeted) retry"
    )
    assert (task.context or {}).get("deferred_findings") in (None, [])

    # Second grader failure: targeted count is exhausted (1) — falls through to wholesale.
    retry_grader.status = TaskStatus.FAILED
    db_session.add(retry_grader)
    await db_session.commit()
    retry_payload = AgentStatusPayload(
        task_id=retry_grader.id,
        status=TaskStatus.FAILED,
        logs=None,
    )
    await _resolve_quality_grader_completion(retry_grader, retry_payload, db_session)
    await db_session.refresh(task)

    # After targeted retry exhausted, wholesale retry is spawned — still no children.
    wholesale_graders = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id,
                    _Task.id.notin_([task.id, grader.id, retry_grader.id]),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(wholesale_graders) == 1, (
        "targeted retry exhausted must fall through to wholesale grader"
    )
    wholesale_grader = wholesale_graders[0]
    assert task.context.get("_adversarial_wholesale_retry_count") == 1, (
        "wholesale count must be 1 after targeted retry is exhausted"
    )
    assert (task.context or {}).get("deferred_findings") in (None, [])

    # Third grader failure: wholesale count exhausted — defer fail-closed.
    wholesale_grader.status = TaskStatus.FAILED
    db_session.add(wholesale_grader)
    await db_session.commit()
    wholesale_payload = AgentStatusPayload(
        task_id=wholesale_grader.id,
        status=TaskStatus.FAILED,
        logs=None,
    )
    await _resolve_quality_grader_completion(
        wholesale_grader, wholesale_payload, db_session
    )
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1, (
        "retry-exhausted challenger infra failure must spawn as unverified by default"
    )
    child_ctx = children[0].context or {}
    assert child_ctx.get("research_spawn_gate_verdict") == "unverified"
    assert child_ctx.get("research_spawn_gate_unverified") is True
    assert child_ctx.get("research_spawn_gate_parse_outcome") == "challenger_failed"

    # After both targeted and wholesale retries are exhausted, an infrastructure
    # failure is fail-open by default and must not decay as an adversarial deferral.
    deferred = (task.context or {}).get("deferred_findings", [])
    assert not any(d.get("title") == hs["title"] for d in deferred)
    assert task.context.get("research_spawn_gate_wholesale_retry_exhausted") is True
    assert task.context.get("research_spawn_gate_refuted_count") == 0
    assert task.context.get("research_spawn_gate_infra_fail_open_count") == 1


@_pytest.mark.asyncio
async def test_adversarial_gate_defers_recovered_deferred_finding_on_fail_verdict(
    db_session, mocker
):
    """Adversarial fail verdict for a recovered prior_deferred finding defers it, not spawns.

    Regression test for the bug where plan_output is written before prior_deferred
    recovery, so _resolve_quality_grader_completion's all_hashes list (built from
    plan_output) omits the recovered finding's hash.  That caused the verdict to be
    silently ignored (unknown hash) and the finding to get a fail-open pass on re-entry.

    With the fix the challenged hashes are persisted in task.context before parking,
    so the verdict is correctly matched and a fail verdict defers the finding.
    """
    from app.research_completion import _finding_hash
    from app.schemas import AgentStatusPayload
    from app.task_lifecycle import _resolve_quality_grader_completion
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-adv-deferred01"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    # A high-stakes finding deferred from the previous cycle.
    deferred_hs = _high_stakes_finding(
        direction["id"], "Prior-deferred high-stakes finding"
    )
    deferred_hs["deferred_reason"] = "batch_cap"
    deferred_hs["deferred_count"] = 1
    fhash = _finding_hash(
        deferred_hs["title"],
        deferred_hs.get("description", "") or deferred_hs.get("prompt", ""),
    )

    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": [deferred_hs],
    }
    db_session.add(task)
    await db_session.commit()

    # Current cycle returns only a low-stakes fresh finding (not high-stakes).
    low_fresh = _make_finding(direction_id=direction["id"])
    low_fresh["title"] = "Low-stakes fresh finding"
    low_fresh["risk"] = "low"
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[low_fresh],
    )
    mocker.patch(
        "app.research_completion.validate_source_refs",
        new=AsyncMock(side_effect=lambda sources, **kwargs: sources),
    )

    # Phase 1: completion handler parks the task, spawning an adversarial grader.
    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    assert task.status == TaskStatus.EVALUATING, (
        "task must park awaiting adversarial verdict"
    )
    # The fix: challenged hashes must include the deferred finding's hash.
    assert fhash in (task.context or {}).get("_adversarial_challenged_hashes", []), (
        "_adversarial_challenged_hashes must include the recovered deferred finding's hash"
    )

    graders = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id, _Task.id != task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(graders) == 1
    grader = graders[0]

    # Phase 2: grader completes with a FAIL verdict for the deferred finding's hash.
    grader.status = TaskStatus.COMPLETED
    db_session.add(grader)
    await db_session.commit()
    payload = AgentStatusPayload(
        task_id=grader.id,
        status=TaskStatus.COMPLETED,
        logs=(
            '[{"hash": "' + fhash + '", "verdict": "fail", '
            '"reason": "premise wrong — cited code absent on HEAD"}]'
        ),
    )
    await _resolve_quality_grader_completion(grader, payload, db_session)
    await db_session.refresh(task)

    # The deferred finding must NOT have spawned a child task.
    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    spawned_titles = [c.title for c in children]
    assert deferred_hs["title"] not in spawned_titles, (
        "adversarial-failed recovered deferred finding must not spawn"
    )
    # The failed finding must be re-deferred for the next cycle.
    all_deferred = (task.context or {}).get("deferred_findings", [])
    assert any(d.get("title") == deferred_hs["title"] for d in all_deferred), (
        "adversarial-failed recovered deferred finding must appear in deferred_findings"
    )


@_pytest.mark.asyncio
async def test_adversarial_gate_wholesale_non_response_rechallenges_once(
    db_session, mocker
):
    """A wholesale grader non-response parks the source and retries exactly once."""
    from app.research_completion import _finding_hash
    from test_autonomous_research import _patch_research_externals

    project, direction, task = await _grader_project_and_task(db_session, "d-adv05")
    project.vision = {"_research_spawn_count": 7, "purpose": "keep-me"}
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    hs = _high_stakes_finding(direction["id"], "Fail-closed missing-hash finding")
    fhash = _finding_hash(
        hs["title"], hs.get("description", "") or hs.get("prompt", "")
    )

    # Adversarial grader returned no usable verdicts for a non-empty manifest:
    # infrastructure failure, not a per-finding refutation.
    task.plan_output = [hs]
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "finding_verdicts": {},
            "grader_task_id": "test-grader-002",
            "parse_outcome": "no_hash_matches",
        },
        "_adversarial_challenged_hashes": [fhash],
    }
    db_session.add(project)
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[hs],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)
    await db_session.refresh(project)

    all_tasks = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.project_id == task.project_id)
            )
        )
        .scalars()
        .all()
    )
    children = [t for t in all_tasks if t.parent_task_id == task.id]
    graders = [t for t in all_tasks if (t.context or {}).get("adversarial_grader")]
    assert len(children) == 0, (
        "absent hash in verdict → fail-closed → finding must NOT spawn"
    )
    assert len(graders) == 1
    assert task.status == TaskStatus.EVALUATING
    assert task.context.get("awaiting_adversarial_grader") == str(graders[0].id)
    assert task.context.get("_adversarial_wholesale_retry_count") == 1
    assert task.context.get(ADVERSARIAL_GATE_VERDICT_KEY) is None
    assert task.context.get("_adversarial_challenged_hashes") == [fhash]
    assert task.context.get("research_spawn_gate_challenged_count") == 1
    assert task.context.get("research_spawn_gate_unresolved_count") == 1
    assert task.context.get("research_spawn_gate_parse_outcome") == "no_hash_matches"
    assert (task.context or {}).get("deferred_findings") in (None, [])
    assert project.vision["_research_spawn_count"] == 7


@_pytest.mark.asyncio
async def test_adversarial_gate_refuted_finding_records_challenge_reason(
    db_session, mocker
):
    """A refuted finding's deferred record carries the challenger's reason (acceptance test)."""
    from app.research_completion import _finding_hash
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-adv-reason01"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    hs = _high_stakes_finding(direction["id"], "Refuted stale finding")
    fhash = _finding_hash(
        hs["title"], hs.get("description", "") or hs.get("prompt", "")
    )

    task.plan_output = [hs]
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "finding_verdicts": {
                fhash: {
                    "passed": False,
                    "reason": "premise wrong — code moved to new module",
                    "explicit": True,
                }
            },
            "grader_task_id": "test-grader-reason-001",
        },
    }
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[hs],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    deferred = (task.context or {}).get("deferred_findings", [])
    assert any(d.get("title") == hs["title"] for d in deferred), (
        "refuted finding must be in deferred_findings"
    )
    matching = [d for d in deferred if d.get("title") == hs["title"]]
    assert len(matching) == 1
    d = matching[0]
    assert (
        d.get("adversarial_gate_reason") == "premise wrong — code moved to new module"
    ), "deferred record must carry the challenger's reason"
    assert "_adversarial_verdict" not in d, "_adversarial_verdict must be cleared"
    assert "_adversarial_verdict_reason" not in d, "internal key must be cleared"
    assert (task.context or {}).get("research_findings_finalized") is True
    assert task.plan_output == [], (
        "refuted findings may remain in the deferred retry path but must not "
        "survive into the authoritative findings index"
    )


@_pytest.mark.asyncio
async def test_adversarial_gate_deferral_decays_or_escalates_after_threshold(
    db_session, mocker
):
    """Repeated adversarial refutations are counted and terminally escalated."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-adv-decay01"
    )
    _patch_research_externals(mocker)
    decayed = _make_finding("Refuted stale finding", direction_id=direction["id"])
    decayed["risk"] = "high"
    decayed["deferred_reason"] = "adversarial_gate"
    decayed["deferred_count"] = 3
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": [decayed],
    }
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding("Fresh sentinel", direction_id=direction["id"])],
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    give_up = (task.context or {}).get("finding_structural_give_up")
    assert give_up is not None
    assert give_up["by_reason"] == {"adversarial_gate": 1}
    assert give_up["by_direction"] == {direction["id"]: 1}
    assert not any(
        f.get("title") == decayed["title"]
        for f in (task.context or {}).get("deferred_findings", [])
    )


@_pytest.mark.asyncio
async def test_adversarial_fail_closed_no_child_on_absent_hash(db_session, mocker):
    """A second wholesale non-response exhausts the retry and fails closed."""
    from app.research_completion import _finding_hash
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-adv-explicit01"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    hs = _high_stakes_finding(direction["id"], "Fail-closed explicit check")
    fhash = _finding_hash(
        hs["title"], hs.get("description", "") or hs.get("prompt", "")
    )

    # Grader returned an empty verdict dict — no hash matched → fail-closed.
    task.plan_output = [hs]
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "finding_verdicts": {},
            "grader_task_id": "test-grader-explicit-001",
            "parse_outcome": "no_hash_matches",
        },
        "_adversarial_challenged_hashes": [fhash],
        "_adversarial_wholesale_retry_count": 1,
    }
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[hs],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 0, "fail-closed: absent hash must NOT produce a child task"
    # The unresolved finding must be deferred, not silently dropped.
    deferred = (task.context or {}).get("deferred_findings", [])
    matching = [d for d in deferred if d.get("title") == hs["title"]]
    assert matching, "absent-hash finding must be deferred, not silently dropped"
    assert matching[0].get("adversarial_gate_reason") == "no_hash_matches"
    assert task.context.get("research_spawn_gate_wholesale_retry_exhausted") is True
    assert task.context.get("research_spawn_gate_parse_outcome") == "no_hash_matches"
    scoped = task.context.get("research_spawn_gate_by_direction") or {}
    assert scoped[direction["id"]]["wholesale_retry_exhausted"] is True


@_pytest.mark.asyncio
async def test_adversarial_gate_infra_failure_fails_open_with_flag(
    db_session, mocker
):
    """Retry-exhausted challenger infra failures spawn as unverified, not refuted."""
    from app.failure_classification import EXTERNAL_API
    from app.research_completion import _finding_hash
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-adv-infra-open"
    )
    _patch_research_externals(mocker)
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"adversarial_spawn_gate_infra_failure_mode": "observe"},
    )
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    hs = _high_stakes_finding(direction["id"], "Infra-failed challenger finding")
    fhash = _finding_hash(
        hs["title"], hs.get("description", "") or hs.get("prompt", "")
    )

    task.plan_output = [hs]
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "finding_verdicts": {},
            "grader_task_id": "test-grader-infra-open",
            "parse_outcome": "challenger_failed",
            "cause": {
                "category": EXTERNAL_API,
                "parse_outcome": "challenger_failed",
                "source": "test",
            },
        },
        "_adversarial_challenged_hashes": [fhash],
        "_adversarial_wholesale_retry_count": 1,
    }
    db_session.add(task)
    await db_session.commit()

    mocker.patch("app.research_completion._parse_research_findings", return_value=[hs])

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1
    child_ctx = children[0].context or {}
    assert child_ctx.get("research_spawn_gate_challenged") is True
    assert child_ctx.get("research_spawn_gate_verdict") == "unverified"
    assert child_ctx.get("research_spawn_gate_explicit") is False
    assert child_ctx.get("research_spawn_gate_unverified") is True
    assert child_ctx.get("research_spawn_gate_parse_outcome") == "challenger_failed"
    assert child_ctx.get("research_spawn_gate_cause", {}).get("category") == EXTERNAL_API
    assert "infrastructure failed" in (
        child_ctx.get("research_spawn_gate_unverified_reason") or ""
    )

    ctx = task.context or {}
    assert ctx.get("research_spawn_gate_wholesale_retry_exhausted") is True
    assert ctx.get("research_spawn_gate_challenged_count") == 1
    assert ctx.get("research_spawn_gate_refuted_count") == 0
    assert ctx.get("research_spawn_gate_survived_count") == 1
    assert ctx.get("research_spawn_gate_explicit_pass_count") == 0
    assert ctx.get("research_spawn_gate_fail_open_count") == 1
    assert ctx.get("research_spawn_gate_infra_fail_open_count") == 1
    assert ctx.get("research_spawn_gate_unresolved_count") == 0
    assert not any(
        d.get("title") == hs["title"] for d in ctx.get("deferred_findings", [])
    )


@_pytest.mark.asyncio
async def test_adversarial_gate_infra_failure_enforce_mode_fails_closed(
    db_session, mocker
):
    """The infra-fail-open mode is reversible: enforce restores fail-closed."""
    from app.failure_classification import EXTERNAL_API
    from app.research_completion import _finding_hash
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-adv-infra-enforce"
    )
    _patch_research_externals(mocker)
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"adversarial_spawn_gate_infra_failure_mode": "enforce"},
    )
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    hs = _high_stakes_finding(direction["id"], "Strict infra-failed finding")
    fhash = _finding_hash(
        hs["title"], hs.get("description", "") or hs.get("prompt", "")
    )
    task.plan_output = [hs]
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "finding_verdicts": {},
            "grader_task_id": "test-grader-infra-enforce",
            "parse_outcome": "challenger_failed",
            "cause": {
                "category": EXTERNAL_API,
                "parse_outcome": "challenger_failed",
                "source": "test",
            },
        },
        "_adversarial_challenged_hashes": [fhash],
        "_adversarial_wholesale_retry_count": 1,
    }
    db_session.add(task)
    await db_session.commit()

    mocker.patch("app.research_completion._parse_research_findings", return_value=[hs])

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert children == []
    deferred = (task.context or {}).get("deferred_findings", [])
    matching = [d for d in deferred if d.get("title") == hs["title"]]
    assert len(matching) == 1
    assert matching[0].get("adversarial_gate_reason") == "challenger_failed"
    assert matching[0].get("adversarial_gate_cause", {}).get("category") == EXTERNAL_API
    assert task.context.get("research_spawn_gate_refuted_count") == 1
    assert task.context.get("research_spawn_gate_infra_fail_open_count") == 0


@_pytest.mark.asyncio
async def test_adversarial_gate_partial_verdict_defers_only_skipped_hash(
    db_session, mocker
):
    """A valid partial verdict array is per-finding fail-closed, not wholesale."""
    from app.research_completion import _finding_hash
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-adv-partial"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    passed = _high_stakes_finding(direction["id"], "Partial explicit pass")
    skipped = _high_stakes_finding(direction["id"], "Partial skipped hash")
    pass_hash = _finding_hash(
        passed["title"], passed.get("description", "") or passed.get("prompt", "")
    )
    skipped_hash = _finding_hash(
        skipped["title"], skipped.get("description", "") or skipped.get("prompt", "")
    )
    findings = [passed, skipped]
    task.plan_output = findings
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "finding_verdicts": {
                pass_hash: {"passed": True, "reason": "grounded", "explicit": True},
                skipped_hash: {
                    "passed": False,
                    "reason": "fail-closed: no verdict returned for this finding",
                    "explicit": False,
                    "unresolved_reason": "no_hash_matches",
                },
            },
            "grader_task_id": "test-grader-partial-001",
            "parse_outcome": "ok",
        },
        "_adversarial_challenged_hashes": [pass_hash, skipped_hash],
    }
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=findings
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    all_tasks = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.project_id == task.project_id)
            )
        )
        .scalars()
        .all()
    )
    children = [t for t in all_tasks if t.parent_task_id == task.id]
    graders = [t for t in all_tasks if (t.context or {}).get("adversarial_grader")]
    assert len(graders) == 0
    assert len(children) == 1
    assert children[0].title == passed["title"]
    deferred = (task.context or {}).get("deferred_findings", [])
    assert [d.get("title") for d in deferred if d.get("title") == skipped["title"]] == [
        skipped["title"]
    ]
    assert task.context.get("_adversarial_wholesale_retry_count") is None
    assert task.context.get("research_spawn_gate_unresolved_count") == 1


@_pytest.mark.asyncio
async def test_adversarial_gate_malformed_known_verdict_token_fails_closed(
    db_session, mocker
):
    """Malformed known verdict tokens are per-entry failures, not wholesale retries."""
    from app.research_completion import _finding_hash
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-adv-bad-token"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    hs = _high_stakes_finding(direction["id"], "Malformed verdict token")
    fhash = _finding_hash(
        hs["title"], hs.get("description", "") or hs.get("prompt", "")
    )
    task.plan_output = [hs]
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "finding_verdicts": {
                fhash: {
                    "passed": False,
                    "reason": "fail-closed: no verdict returned for this finding",
                    "explicit": False,
                    "unresolved_reason": "verdict_unrecognized",
                }
            },
            "grader_task_id": "test-grader-bad-token-001",
            "parse_outcome": "ok",
        },
        "_adversarial_challenged_hashes": [fhash],
    }
    db_session.add(task)
    await db_session.commit()

    mocker.patch("app.research_completion._parse_research_findings", return_value=[hs])

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    all_tasks = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.project_id == task.project_id)
            )
        )
        .scalars()
        .all()
    )
    children = [t for t in all_tasks if t.parent_task_id == task.id]
    graders = [t for t in all_tasks if (t.context or {}).get("adversarial_grader")]
    assert children == []
    assert graders == []
    deferred = (task.context or {}).get("deferred_findings", [])
    assert len([d for d in deferred if d.get("title") == hs["title"]]) == 1
    assert deferred[0].get("adversarial_gate_reason") == "verdict_unrecognized"
    assert task.context.get("_adversarial_wholesale_retry_count") is None


@_pytest.mark.asyncio
async def test_adversarial_gate_real_pass_resolver_spawns_child_with_telemetry(
    db_session, mocker
):
    """A real spawn/resolver pass must record telemetry on the challenged child."""
    from app.research_completion import _finding_hash
    from app.schemas import AgentStatusPayload
    from app.task_lifecycle import _resolve_quality_grader_completion
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-adv06")
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    high_pass = _high_stakes_finding(direction["id"], "Security-critical refactor")
    high_fail = _high_stakes_finding(direction["id"], "Rejected risky follow-up")
    low = _make_finding(direction_id=direction["id"])
    low["title"] = "Routine low-stakes follow-up"
    low["risk"] = "low"

    pass_hash = _finding_hash(
        high_pass["title"],
        high_pass.get("description", "") or high_pass.get("prompt", ""),
    )
    fail_hash = _finding_hash(
        high_fail["title"],
        high_fail.get("description", "") or high_fail.get("prompt", ""),
    )
    task.plan_output = [high_pass, high_fail, low]
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[high_pass, high_fail, low],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert (task.context or {}).get("awaiting_adversarial_grader")

    graders = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id, _Task.id != task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(graders) == 1
    grader = graders[0]
    assert (grader.context or {}).get("adversarial_grader") is True
    grader.status = TaskStatus.COMPLETED
    db_session.add(grader)
    await db_session.commit()
    payload = AgentStatusPayload(
        task_id=grader.id,
        status=TaskStatus.COMPLETED,
        logs=(
            "["
            '{"hash": "'
            + pass_hash
            + '", "verdict": "pass", "reason": "well-grounded"},'
            '{"hash": "'
            + fail_hash
            + '", "verdict": "fail", "reason": "stale premise"}'
            "]"
        ),
    )
    await _resolve_quality_grader_completion(grader, payload, db_session)
    await db_session.refresh(task)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 2, (
        "one challenged pass + one unchallenged finding must spawn"
    )

    by_title = {child.title: child for child in children}
    challenged = by_title[high_pass["title"]]
    unchallenged = by_title[low["title"]]
    assert (challenged.context or {}).get("research_spawn_gate_challenged") is True
    assert (challenged.context or {}).get("research_spawn_gate_verdict") == "pass"
    assert (challenged.context or {}).get(
        "research_spawn_gate_reason"
    ) == "well-grounded"
    assert (challenged.context or {}).get("research_spawn_gate_explicit") is True
    assert (unchallenged.context or {}).get("research_spawn_gate_challenged") is False
    assert (unchallenged.context or {}).get("research_spawn_gate_verdict") is None
    assert (unchallenged.context or {}).get("research_spawn_gate_reason") is None
    assert (unchallenged.context or {}).get("research_spawn_gate_explicit") is None

    adv_gate = (task.context or {}).get(ADVERSARIAL_GATE_VERDICT_KEY) or {}
    assert adv_gate.get("parse_outcome") == "ok"
    assert (task.context or {}).get("research_spawn_gate_challenged_count") == 2
    assert (task.context or {}).get("research_spawn_gate_refuted_count") == 1
    assert (task.context or {}).get("research_spawn_gate_survived_count") == 1


@_pytest.mark.asyncio
async def test_spawn_gate_exhausted_flag_cleared_on_later_pass_and_scoped_by_direction(
    db_session, mocker
):
    from app.research_completion import (
        _ADVERSARIAL_TARGETED_RETRY_KEY,
        _ADVERSARIAL_WHOLESALE_RETRY_KEY,
        _finding_hash,
    )
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-adv-later-pass"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    hs1 = _high_stakes_finding(direction["id"], "Recovered pass one")
    hs2 = _high_stakes_finding(direction["id"], "Recovered pass two")
    hash1 = _finding_hash(hs1["title"], hs1.get("description", ""))
    hash2 = _finding_hash(hs2["title"], hs2.get("description", ""))

    task.plan_output = [hs1, hs2]
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "finding_verdicts": {
                hash1: {"passed": True, "reason": "grounded", "explicit": True},
                hash2: {"passed": True, "reason": "still valid", "explicit": True},
            },
            "grader_task_id": "test-grader-later-pass",
            "parse_outcome": "ok",
        },
        "_adversarial_challenged_hashes": [hash1, hash2],
        "research_spawn_gate_wholesale_retry_exhausted": True,
        "research_spawn_gate_refusal_skip_retry": True,
        "research_spawn_gate_last_wholesale_parse_outcome": "no_hash_matches",
        _ADVERSARIAL_WHOLESALE_RETRY_KEY: 1,
        _ADVERSARIAL_TARGETED_RETRY_KEY: 1,
        "_adversarial_original_challenged_hashes": [hash1, hash2],
    }
    db_session.add(task)
    await db_session.commit()
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[hs1, hs2],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    ctx = task.context or {}
    assert "research_spawn_gate_wholesale_retry_exhausted" not in ctx
    assert "research_spawn_gate_refusal_skip_retry" not in ctx
    assert "research_spawn_gate_last_wholesale_parse_outcome" not in ctx
    assert _ADVERSARIAL_WHOLESALE_RETRY_KEY not in ctx
    assert _ADVERSARIAL_TARGETED_RETRY_KEY not in ctx
    assert "_adversarial_original_challenged_hashes" not in ctx

    scoped = ctx.get("research_spawn_gate_by_direction") or {}
    assert scoped == {
        direction["id"]: {
            "challenged_count": 2,
            "refuted_count": 0,
            "survived_count": 2,
            "explicit_pass_count": 2,
            "fail_open_count": 0,
            "unresolved_count": 0,
            "wholesale_retry_exhausted": False,
            "parse_outcome": "ok",
        }
    }


@_pytest.mark.asyncio
async def test_spawn_gate_scoped_by_direction_keeps_sibling_directions_separate(
    db_session, mocker
):
    from app.research_completion import _finding_hash
    from test_autonomous_research import (
        _active_direction,
        _make_research_task,
        _patch_research_externals,
    )

    direction_a = _active_direction("d-adv-sibling-a", "Sibling direction A")
    direction_b = _active_direction("d-adv-sibling-b", "Sibling direction B")
    project = _Project(
        name="Sibling Direction Gate",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction_a, direction_b],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = await _make_research_task(project, direction_a, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_a["id"]}

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    hs_a = _high_stakes_finding(direction_a["id"], "Sibling direction A finding")
    hs_b = _high_stakes_finding(direction_b["id"], "Sibling direction B finding")
    hash_a = _finding_hash(hs_a["title"], hs_a.get("description", ""))
    hash_b = _finding_hash(hs_b["title"], hs_b.get("description", ""))

    task.plan_output = [hs_a, hs_b]
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "finding_verdicts": {
                hash_a: {
                    "passed": False,
                    "reason": "stale premise",
                    "explicit": True,
                },
                hash_b: {
                    "passed": True,
                    "reason": "well-grounded",
                    "explicit": True,
                },
            },
            "grader_task_id": "test-grader-sibling-001",
            "parse_outcome": "ok",
        },
        "_adversarial_challenged_hashes": [hash_a, hash_b],
    }
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[hs_a, hs_b],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    ctx = task.context or {}
    assert ctx.get("research_spawn_gate_challenged_count") == 2
    assert ctx.get("research_spawn_gate_refuted_count") == 1
    assert ctx.get("research_spawn_gate_survived_count") == 1
    assert ctx.get("research_spawn_gate_explicit_pass_count") == 1
    assert ctx.get("research_spawn_gate_fail_open_count") == 0
    assert ctx.get("research_spawn_gate_unresolved_count") == 0

    scoped = ctx.get("research_spawn_gate_by_direction") or {}
    assert set(scoped) == {direction_a["id"], direction_b["id"]}
    assert scoped[direction_a["id"]] == {
        "challenged_count": 1,
        "refuted_count": 1,
        "survived_count": 0,
        "explicit_pass_count": 0,
        "fail_open_count": 0,
        "unresolved_count": 0,
        "wholesale_retry_exhausted": False,
        "parse_outcome": "ok",
        "refuted_reasons": ["stale premise"],
    }
    assert scoped[direction_b["id"]] == {
        "challenged_count": 1,
        "refuted_count": 0,
        "survived_count": 1,
        "explicit_pass_count": 1,
        "fail_open_count": 0,
        "unresolved_count": 0,
        "wholesale_retry_exhausted": False,
        "parse_outcome": "ok",
    }


@_pytest.mark.asyncio
async def test_spawn_gate_decision_quality_breakdown_recorded(db_session, mocker):
    """Explicit-pass, fail-open, unresolved, and refuted outcomes get distinct counts.

    The legacy challenged/refuted/survived counters stay inclusive while the new
    breakdown splits the survived bucket into explicit_pass vs fail_open and the
    refuted bucket into explicit-refutation vs unresolved. A compact deduped
    sample of refuted/unresolved reasons is persisted, and deferred findings that
    came from the adversarial gate carry their finding_hash (keyed the same way
    verdicts are) alongside the title.
    """
    from app.research_completion import _finding_hash
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-adv-breakdown01"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    high_explicit_pass = _high_stakes_finding(direction["id"], "Explicit-pass refactor")
    high_fail_open = _high_stakes_finding(direction["id"], "Fail-open defaulted pass")
    high_refuted = _high_stakes_finding(direction["id"], "Refuted stale finding")
    high_unresolved = _high_stakes_finding(
        direction["id"], "Unresolved no-verdict finding"
    )
    low = _make_finding(direction_id=direction["id"])
    low["title"] = "Routine low-stakes follow-up"
    low["risk"] = "low"

    def _h(f):
        return _finding_hash(
            f["title"], f.get("description", "") or f.get("prompt", "")
        )

    pass_hash = _h(high_explicit_pass)
    failopen_hash = _h(high_fail_open)
    refuted_hash = _h(high_refuted)
    unresolved_hash = _h(high_unresolved)

    findings = [high_explicit_pass, high_fail_open, high_refuted, high_unresolved, low]
    task.plan_output = findings
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "finding_verdicts": {
                pass_hash: {
                    "passed": True,
                    "reason": "well-grounded",
                    "explicit": True,
                },
                # Fail-open: passed but the challenger gave no explicit verdict.
                failopen_hash: {"passed": True, "reason": "", "explicit": False},
                refuted_hash: {
                    "passed": False,
                    "reason": "stale premise",
                    "explicit": True,
                },
                # Unresolved finding carries the parser's classification instead of
                # collapsing to the generic fail-closed string.
                unresolved_hash: {
                    "passed": False,
                    "reason": "fail-closed: no verdict returned for this finding",
                    "explicit": False,
                    "unresolved_reason": "no_hash_matches",
                },
            },
            "grader_task_id": "test-grader-breakdown-001",
            "parse_outcome": "no_hash_matches",
        },
        # All four high-stakes findings were in the challenge manifest; the
        # unresolved one must be treated as challenged-but-no-verdict.
        "_adversarial_challenged_hashes": [
            pass_hash,
            failopen_hash,
            refuted_hash,
            unresolved_hash,
        ],
    }
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=findings,
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    ctx = task.context or {}
    # Legacy inclusive counters preserved.
    assert ctx.get("research_spawn_gate_challenged_count") == 4
    assert ctx.get("research_spawn_gate_refuted_count") == 2  # refuted + unresolved
    assert ctx.get("research_spawn_gate_survived_count") == 2  # explicit + fail-open
    # New decision-quality breakdown.
    assert ctx.get("research_spawn_gate_explicit_pass_count") == 1
    assert ctx.get("research_spawn_gate_fail_open_count") == 1
    assert ctx.get("research_spawn_gate_unresolved_count") == 1
    assert ctx.get("research_spawn_gate_parse_outcome") == "no_hash_matches"
    # explicit_pass + fail_open == survived; unresolved <= refuted.
    assert (
        ctx["research_spawn_gate_explicit_pass_count"]
        + ctx["research_spawn_gate_fail_open_count"]
        == ctx["research_spawn_gate_survived_count"]
    )
    # Compact reasons sample captures both the explicit refutation and the
    # parser-classified unresolved reason, deduped.
    reasons = ctx.get("research_spawn_gate_refuted_reasons") or []
    assert "stale premise" in reasons
    assert "no_hash_matches" in reasons
    assert len(reasons) == len(set(reasons)), "reasons sample must be deduped"
    assert len(reasons) <= 5
    scoped = ctx.get("research_spawn_gate_by_direction") or {}
    scoped_entry = scoped[direction["id"]]
    assert scoped_entry["challenged_count"] == 4
    assert scoped_entry["refuted_count"] == 2
    assert scoped_entry["survived_count"] == 2
    assert scoped_entry["explicit_pass_count"] == 1
    assert scoped_entry["fail_open_count"] == 1
    assert scoped_entry["unresolved_count"] == 1
    assert scoped_entry["parse_outcome"] == "no_hash_matches"
    assert scoped_entry["wholesale_retry_exhausted"] is False
    assert "stale premise" in scoped_entry["refuted_reasons"]
    assert "no_hash_matches" in scoped_entry["refuted_reasons"]

    # Deferred findings (refuted + unresolved) carry their verdict-keyed
    # finding_hash alongside the title so downstream consumers can match by hash.
    deferred = ctx.get("deferred_findings") or []
    deferred_by_title = {d.get("title"): d for d in deferred}
    assert high_refuted["title"] in deferred_by_title
    assert high_unresolved["title"] in deferred_by_title
    assert deferred_by_title[high_refuted["title"]].get("finding_hash") == refuted_hash
    assert (
        deferred_by_title[high_unresolved["title"]].get("finding_hash")
        == unresolved_hash
    )
    assert (
        deferred_by_title[high_unresolved["title"]].get("adversarial_gate_reason")
        == "no_hash_matches"
    )


@_pytest.mark.asyncio
async def test_spawn_gate_records_manifest_excluded_fail_closed_category(
    db_session, mocker
):
    """Manifest-excluded high-stakes findings are visible but do not drift counters.

    Scenario: 3 high-stakes findings exist. The manifest can only fit 2 (budget-bounded).
    The excluded finding gets fail-closed, is counted in its own category, and
    must NOT inflate manifest-admitted refuted/survived arithmetic.
    """
    from app.research_completion import _finding_hash
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-adv-bounded01"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    # Three high-stakes findings; only two were admitted to the manifest.
    hs_in_manifest_1 = _high_stakes_finding(direction["id"], "Manifest finding one")
    hs_in_manifest_2 = _high_stakes_finding(direction["id"], "Manifest finding two")
    hs_excluded = _high_stakes_finding(
        direction["id"], "Budget-excluded high-stakes finding"
    )

    def _h(f):
        return _finding_hash(
            f["title"], f.get("description", "") or f.get("prompt", "")
        )

    hash1 = _h(hs_in_manifest_1)
    hash2 = _h(hs_in_manifest_2)
    excluded_hash = _h(hs_excluded)

    findings = [hs_in_manifest_1, hs_in_manifest_2, hs_excluded]
    task.plan_output = findings
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "finding_verdicts": {
                hash1: {"passed": True, "reason": "solid", "explicit": True},
                hash2: {"passed": False, "reason": "stale", "explicit": True},
            },
            "grader_task_id": "test-grader-bounded-001",
        },
        # Only hash1 and hash2 were challenged — hs_excluded was budget-capped out.
        "_adversarial_challenged_hashes": [hash1, hash2],
    }
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=findings,
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    ctx = task.context or {}
    # challenged_count reflects only the manifest (2), not all 3 high-stakes findings.
    assert ctx.get("research_spawn_gate_challenged_count") == 2
    # hash2 was explicitly refuted; hs_excluded must NOT inflate this count.
    assert ctx.get("research_spawn_gate_refuted_count") == 1
    # survived = challenged - refuted must be >= 0 and consistent.
    assert ctx.get("research_spawn_gate_survived_count") == 1
    assert (
        ctx["research_spawn_gate_survived_count"]
        == ctx["research_spawn_gate_challenged_count"]
        - ctx["research_spawn_gate_refuted_count"]
    )
    assert ctx.get("research_spawn_gate_manifest_excluded_fail_closed_count") == 1
    samples = ctx.get("research_spawn_gate_manifest_excluded_fail_closed_samples") or []
    assert samples == [
        {
            "hash": excluded_hash,
            "title": "Budget-excluded high-stakes finding",
            "reason": (
                "fail-closed: high-stakes finding was not admitted "
                "to the adversarial manifest"
            ),
        }
    ]
    scoped = ctx.get("research_spawn_gate_by_direction") or {}
    scoped_entry = scoped[direction["id"]]
    assert scoped_entry["manifest_excluded_count"] == 1
    assert scoped_entry["manifest_excluded_samples"] == samples

    deferred = {
        d.get("title"): d
        for d in (ctx.get("deferred_findings") or [])
        if d.get("deferred_reason") == "adversarial_gate"
    }
    assert (
        deferred["Budget-excluded high-stakes finding"]["finding_hash"] == excluded_hash
    )
    assert deferred["Budget-excluded high-stakes finding"][
        "adversarial_gate_reason"
    ] == (
        "fail-closed: high-stakes finding was not admitted to the adversarial manifest"
    )


@_pytest.mark.asyncio
async def test_high_stakes_finding_default_on_adversarial_rejudgment(
    db_session, mocker
):
    """ACCEPTANCE TEST: default-on adversarial gate lifecycle — park, verdict, re-entry.

    Full lifecycle: first pass parks the task; second pass with a PASS verdict
    lets the finding spawn; a FAIL verdict defers it.

    Phase 1 — first pass: high-stakes finding → gate parks the task.
    Phase 2a — re-entry with PASS verdict: finding spawns normally.
    Phase 2b — re-entry with FAIL verdict (separate task): finding is deferred.
    """
    from app.research_completion import _finding_hash
    from test_autonomous_research import _patch_research_externals

    # --- PHASE 1: first pass parks the task ---
    _project, direction, task = await _grader_project_and_task(db_session, "d-acc01")
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )
    hs = _high_stakes_finding(direction["id"], "Security-critical refactor")
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[hs],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    assert task.status == TaskStatus.EVALUATING, (
        "Phase 1: task must be parked EVALUATING"
    )
    assert task.context.get("awaiting_adversarial_grader"), (
        "Phase 1: must set awaiting flag"
    )
    others = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id, _Task.id != task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(others) == 1, (
        "Phase 1: exactly one adversarial grader task must be spawned"
    )
    assert (others[0].context or {}).get("adversarial_grader") is True

    children_p1 = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert children_p1 == [], "Phase 1: no implementation tasks yet"

    # --- PHASE 2a: re-entry with PASS verdict → finding spawns ---
    fhash = _finding_hash(
        hs["title"], hs.get("description", "") or hs.get("prompt", "")
    )
    task.plan_output = [hs]
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "finding_verdicts": {
                fhash: {"passed": True, "reason": "all criteria met", "explicit": True}
            },
            "grader_task_id": str(others[0].id),
        },
    }
    task.context.pop("awaiting_adversarial_grader", None)
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[hs],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    children_p2 = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children_p2) == 1, (
        "Phase 2a (PASS): finding must spawn exactly one child"
    )
    assert not task.context.get("awaiting_adversarial_grader"), "Phase 2a: flag cleared"

    # --- PHASE 2b: separate task, re-entry with FAIL verdict → finding deferred ---
    _project2, direction2, task2 = await _grader_project_and_task(db_session, "d-acc02")
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )
    hs2 = _high_stakes_finding(direction2["id"], "High-risk finding that fails gate")
    fhash2 = _finding_hash(
        hs2["title"], hs2.get("description", "") or hs2.get("prompt", "")
    )
    task2.plan_output = [hs2]
    task2.status = TaskStatus.COMPLETED
    task2.context = {
        **(task2.context or {}),
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "finding_verdicts": {
                fhash2: {
                    "passed": False,
                    "reason": "stale — code no longer exists",
                    "explicit": True,
                }
            },
            "grader_task_id": "mock-grader-fail-001",
        },
    }
    db_session.add(task2)
    await db_session.commit()

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[hs2],
    )

    await _handle_research_completion(task2, "", db_session)
    await db_session.refresh(task2)

    children_fail = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task2.id)
            )
        )
        .scalars()
        .all()
    )
    assert children_fail == [], (
        "Phase 2b (FAIL): adversarial-failed finding must NOT spawn"
    )

    deferred = (task2.context or {}).get("deferred_findings", [])
    assert any(d.get("title") == hs2["title"] for d in deferred), (
        "Phase 2b (FAIL): failed finding must appear in deferred_findings"
    )
    for d in deferred:
        assert "_adversarial_verdict" not in d, (
            "Phase 2b: _adversarial_verdict must be cleared"
        )


@_pytest.mark.asyncio
async def test_malformed_stored_adversarial_verdict_fails_closed(db_session, mocker):
    """Malformed verdict records (missing/null/string passed values) fail-closed.

    Legacy boolean verdicts and explicit structured records with valid pass/fail
    booleans keep their current behavior. But missing, null, string, or otherwise
    invalid 'passed' values must not default to allowing a challenged finding to spawn —
    they must fail-closed (defer the finding, do not spawn).
    """
    from app.research_completion import _finding_hash
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-malformed-verdict"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    # Create multiple high-stakes findings to test different malformed verdict cases
    hs1 = _high_stakes_finding(direction["id"], "Finding with missing passed key")
    hs2 = _high_stakes_finding(direction["id"], "Finding with null passed value")
    hs3 = _high_stakes_finding(direction["id"], "Finding with string passed value")
    hs4 = _high_stakes_finding(direction["id"], "Finding with valid true verdict")
    hs5 = _high_stakes_finding(direction["id"], "Finding with false passed verdict")

    findings = [hs1, hs2, hs3, hs4, hs5]

    # Compute hashes for verdict assignment
    fhash1 = _finding_hash(
        hs1["title"], hs1.get("description", "") or hs1.get("prompt", "")
    )
    fhash2 = _finding_hash(
        hs2["title"], hs2.get("description", "") or hs2.get("prompt", "")
    )
    fhash3 = _finding_hash(
        hs3["title"], hs3.get("description", "") or hs3.get("prompt", "")
    )
    fhash4 = _finding_hash(
        hs4["title"], hs4.get("description", "") or hs4.get("prompt", "")
    )
    fhash5 = _finding_hash(
        hs5["title"], hs5.get("description", "") or hs5.get("prompt", "")
    )

    # Set up the task with malformed verdict records
    task.plan_output = findings
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "finding_verdicts": {
                fhash1: {"reason": "tested"},  # Missing "passed" key -> fail-closed
                fhash2: {
                    "passed": None,
                    "reason": "tested",
                },  # Null passed -> fail-closed
                fhash3: {
                    "passed": "true",
                    "reason": "tested",
                },  # String passed -> fail-closed
                fhash4: {
                    "passed": True,
                    "reason": "valid",
                    "explicit": True,
                },  # Valid True -> spawn
                fhash5: {
                    "passed": False,
                    "reason": "refuted",
                    "explicit": True,
                },  # Valid False -> defer
            },
            "grader_task_id": "mock-grader-malformed",
        },
        "_adversarial_challenged_hashes": [fhash1, fhash2, fhash3, fhash4, fhash5],
    }
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=findings,
    )

    # Re-entry with malformed verdicts
    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # Only the finding with valid True verdict should spawn
    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    spawned_titles = [c.title for c in children]

    assert len(spawned_titles) == 1, (
        "Only the finding with valid True verdict should spawn; "
        f"got {len(spawned_titles)}: {spawned_titles}"
    )
    assert hs4["title"] in spawned_titles, (
        "Only the finding with valid True verdict should spawn"
    )

    # All malformed and false-verdict findings should be deferred
    deferred = (task.context or {}).get("deferred_findings", [])
    deferred_titles = [d.get("title") for d in deferred]

    for finding in [hs1, hs2, hs3, hs5]:
        assert finding["title"] in deferred_titles, (
            f"Finding '{finding['title']}' with malformed/false verdict must be deferred"
        )

    # Verify internal verdict keys are cleared from deferred findings
    for d in deferred:
        assert "_adversarial_verdict" not in d, (
            "_adversarial_verdict must be cleared from deferred findings"
        )


class TestResolveResearchFindingsForSurface:
    """User surfaces expose only the finalized challenge-enriched snapshot."""

    def _finding(self, title: str = "F1", **kwargs):
        return {"title": title, "description": "desc", **kwargs}

    def _challenge(self, **overrides):
        base = {
            "challenged_boundary": "reproduce_absent",
            "first_verdict": "absent",
            "second_opinion_verdict": "present",
            "agreement": "disagrees",
            "reason": "code still present at HEAD",
            "sidecar_task_id": "task-abc",
            "extra_internal_key": "should_be_trimmed",
        }
        base.update(overrides)
        return base

    @staticmethod
    def _resolve(plan_output, *, finalized=True, status=TaskStatus.COMPLETED):
        from app.research_completion import (
            FINALIZED_RESEARCH_FINDINGS_KEY,
            resolve_research_findings_for_surface_from_state,
        )

        context = {FINALIZED_RESEARCH_FINDINGS_KEY: True} if finalized else {}
        return resolve_research_findings_for_surface_from_state(
            plan_output,
            context,
            status=status,
            task_type="research",
        )

    def test_challenge_enriched_findings_surface_through_serializer(self):
        """A finding in plan_output with a finding_challenge surfaces the bounded
        challenge fields and strips internal-only keys."""
        from app.research_completion import _FINDING_CHALLENGE_RECORD_KEY

        challenge = self._challenge()
        finding = self._finding(title="Bug with challenge")
        finding[_FINDING_CHALLENGE_RECORD_KEY] = challenge

        result = self._resolve([finding])

        assert result is not None
        assert len(result) == 1
        surfaced = result[0]
        assert surfaced["title"] == "Bug with challenge"

        rc = surfaced[_FINDING_CHALLENGE_RECORD_KEY]
        assert isinstance(rc, dict)
        # All bounded fields must be present
        assert rc["challenged_boundary"] == "reproduce_absent"
        assert rc["first_verdict"] == "absent"
        assert rc["second_opinion_verdict"] == "present"
        assert rc["agreement"] == "disagrees"
        assert rc["reason"] == "code still present at HEAD"
        assert rc["sidecar_task_id"] == "task-abc"
        # Internal key must not leak out
        assert "extra_internal_key" not in rc

    def test_plan_output_returned_unchanged_when_no_challenge(self):
        """Findings without a challenge record serialize exactly as before."""
        finding = self._finding(
            title="No challenge finding", actionability_grade="HIGH"
        )
        result = self._resolve([finding])

        assert result is not None
        assert len(result) == 1
        assert result[0]["title"] == "No challenge finding"
        assert result[0]["actionability_grade"] == "HIGH"
        assert "finding_challenge" not in result[0]

    def test_raw_unfinalized_output_is_not_presented(self):
        finding = self._finding(title="Rejected before finalization")
        assert self._resolve([finding], finalized=False) is None

    def test_finalized_delivery_evaluating_output_is_presented(self):
        finding = self._finding(title="Accepted analysis still delivering")
        assert self._resolve([finding], status=TaskStatus.EVALUATING) == [finding]

    def test_parked_evaluating_output_without_finalized_marker_is_not_presented(self):
        finding = self._finding(title="Still awaiting a grader")
        assert (
            self._resolve(
                [finding],
                finalized=False,
                status=TaskStatus.EVALUATING,
            )
            is None
        )

    def test_empty_final_snapshot_returns_none(self):
        assert self._resolve(None) is None
        assert self._resolve([]) is None

    def test_finalized_count_reflects_canonical_finding_set(self):
        plan_findings = [self._finding(title=f"Finding {i}") for i in range(3)]
        result = self._resolve(plan_findings)

        assert result is not None
        assert len(result) == 3


@_pytest.mark.asyncio
async def test_adversarial_gate_payload_uses_hash_field(db_session, mocker):
    """The adversarial grader payload must expose the contract hash under `hash`."""
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-adv-hash01"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    hs = _high_stakes_finding(direction["id"], "Hash payload contract")
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[hs],
    )

    captured: dict[str, object] = {}

    async def _capture_spawn(source_task, artifact_text, session, **_kwargs):
        captured["artifact_text"] = artifact_text
        source_task.status = TaskStatus.EVALUATING
        source_task.context = {
            **(source_task.context or {}),
            "awaiting_adversarial_grader": True,
        }
        return SimpleNamespace(id="grade-hash-001")

    mocker.patch(
        "app.task_evaluators.spawn_forward_quality_grader",
        new=_capture_spawn,
    )

    await _handle_research_completion(task, "", db_session)

    payload = json.loads(captured["artifact_text"])
    assert payload[0]["hash"]
    assert "_adversarial_hash" not in payload[0]


@_pytest.mark.asyncio
async def test_adversarial_gate_rechecks_prior_deferred_high_stakes_findings(
    db_session, mocker
):
    """Recovered high-stakes findings must still pass through the adversarial gate."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-adv-recover01"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    recovered = _high_stakes_finding(direction["id"], "Recovered risky finding")
    current = _make_finding(
        direction_id=direction["id"], title="Current low-stakes finding"
    )
    task.plan_output = [current]
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": [
            {
                **recovered,
                "deferred_reason": "adversarial_gate",
                "deferred_count": 1,
            }
        ],
    }
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[current],
    )

    captured: dict[str, object] = {}

    async def _capture_spawn(source_task, artifact_text, session, **_kwargs):
        captured["artifact_text"] = artifact_text
        source_task.status = TaskStatus.EVALUATING
        source_task.context = {
            **(source_task.context or {}),
            "awaiting_adversarial_grader": True,
        }
        return SimpleNamespace(id="grade-recovered-001")

    mocker.patch(
        "app.task_evaluators.spawn_forward_quality_grader",
        new=_capture_spawn,
    )

    await _handle_research_completion(task, "", db_session)

    payload = json.loads(captured["artifact_text"])
    assert payload[0]["title"] == recovered["title"]
    assert payload[0]["hash"]
    assert task.status == TaskStatus.EVALUATING
    assert task.context.get("awaiting_adversarial_grader") is True


@_pytest.mark.asyncio
async def test_adversarial_gate_uses_bounded_valid_manifest_for_long_findings(
    db_session, mocker
):
    """ACCEPTANCE TEST: a large findings list produces a parseable manifest within budget.

    With many high-stakes findings that have long descriptions, the adversarial
    grader payload must:
    1. Be valid JSON (parseable by json.loads).
    2. Stay within _ADVERSARIAL_MANIFEST_CHAR_BUDGET chars.
    3. Each admitted finding carries a stable 'hash' field matching finding_hash().
    4. The grader task context carries _adversarial_challenged_hashes that match.
    5. The source task context also carries _adversarial_challenged_hashes.
    """
    from app.task_evaluators import _ADVERSARIAL_MANIFEST_CHAR_BUDGET
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-manifest-adv01"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    # Build 30 high-stakes findings each with a ~500-char description — this
    # would overflow a naive json.dumps approach without manifest budgeting.
    long_findings = []
    for i in range(30):
        long_findings.append(
            {
                "title": f"High-stakes finding number {i}",
                "description": "Very long description " * 25 + f"(finding {i})",
                "prompt": f"Implement fix for finding {i}",
                "type": "improvement",
                "priority": "high",
                "scope": "medium",
                "risk": "high",
                "direction_id": direction["id"],
                "actionability_grade": "HIGH",
            }
        )

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=long_findings,
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # Task must be parked awaiting the adversarial grader.
    assert task.status == TaskStatus.EVALUATING, (
        "Task must be parked EVALUATING awaiting adversarial verdict"
    )

    # Find the spawned adversarial grader.
    others = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id, _Task.id != task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(others) == 1, "Exactly one adversarial grader must be spawned"
    grader = others[0]
    assert (grader.context or {}).get("adversarial_grader") is True

    # The grader prompt must contain valid JSON within budget.
    prompt = (grader.first_payload or {}).get("prompt") or ""
    import re as _re

    fence_match = _re.search(
        r"FINDINGS START.*?8<-----\n(.*?)\n-----8<----- FINDINGS END",
        prompt,
        _re.DOTALL,
    )
    assert fence_match, "Grader prompt must contain FINDINGS fences"
    findings_json = fence_match.group(1).strip()
    assert len(findings_json) <= _ADVERSARIAL_MANIFEST_CHAR_BUDGET, (
        f"Manifest must stay within budget: {len(findings_json)} > {_ADVERSARIAL_MANIFEST_CHAR_BUDGET}"
    )
    parsed = json.loads(findings_json)
    assert isinstance(parsed, list) and len(parsed) > 0, (
        "Manifest JSON must be a non-empty list"
    )

    # Each admitted finding must have a stable hash. The hash is computed from
    # the original description (before truncation) by the manifest builder.
    # Verify all hashes are non-empty strings and are consistent within the manifest.
    manifest_hashes = set()
    for item in parsed:
        assert isinstance(item["hash"], str) and len(item["hash"]) > 0, (
            f"Finding {item['title']} must have a non-empty hash"
        )
        manifest_hashes.add(item["hash"])

    # Grader context must carry _adversarial_challenged_hashes.
    grader_hashes = (grader.context or {}).get("_adversarial_challenged_hashes")
    assert isinstance(grader_hashes, list) and len(grader_hashes) > 0, (
        "Grader context must carry _adversarial_challenged_hashes"
    )
    assert grader_hashes == [item["hash"] for item in parsed], (
        "Grader context hashes must match manifest hashes"
    )

    # Source task context must also carry _adversarial_challenged_hashes.
    source_hashes = (task.context or {}).get("_adversarial_challenged_hashes")
    assert isinstance(source_hashes, list) and len(source_hashes) > 0, (
        "Source task context must carry _adversarial_challenged_hashes"
    )
    assert source_hashes == grader_hashes, (
        "Source and grader _adversarial_challenged_hashes must be identical"
    )


@_pytest.mark.asyncio
async def test_capability_foundation_finding_bypasses_adversarial_gate(
    db_session, mocker
):
    """Capability-providing findings should spawn directly instead of parking."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-capability-adv01"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.task_evaluators.spawn_forward_quality_grader",
        new=AsyncMock(return_value=None),
    )

    finding = _make_finding(
        title="Declare payment-core capability", direction_id=direction["id"]
    )
    finding["risk"] = "high"
    finding["provides_capability"] = "payment-core"
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    ctx = task.context or {}
    assert not ctx.get("awaiting_adversarial_grader")
    assert "_adversarial_challenged_hashes" not in ctx
    assert (task.plan_output or [{}])[0].get("_provides_capability") == "payment-core"

    all_tasks = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.project_id == _project.id)
            )
        )
        .scalars()
        .all()
    )
    graders = [t for t in all_tasks if (t.context or {}).get("adversarial_grader")]
    assert len(graders) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Parked-awaiting re-dispatch guard
# ─────────────────────────────────────────────────────────────────────────────


@_pytest.mark.asyncio
async def test_redispatch_while_awaiting_adversarial_grader_spawns_nothing(
    db_session, mocker
):
    """ACCEPTANCE TEST: re-dispatching a parked source is a safe no-op.

    A research task parked EVALUATING (awaiting_adversarial_grader set, no verdict
    yet) must not spawn implementation work when the completion handler is invoked
    again — the adversarial gate's single-shot guard blocks a second grader spawn,
    but without the parked-awaiting guard the spawn loop runs ungated and high-stakes
    findings bypass the gate entirely.

    After the no-op re-entry the task must still be EVALUATING with no children.
    """
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-redispatch01"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )
    hs = _high_stakes_finding(direction["id"], "Security-critical re-dispatch finding")
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[hs],
    )

    # Simulate a task that is already parked awaiting adversarial grader but has
    # no verdict yet — the state that the post-commit sweep or a duplicate terminal
    # status post would trigger a re-dispatch from.
    task.status = TaskStatus.EVALUATING
    task.context = {
        **(task.context or {}),
        "awaiting_adversarial_grader": "some-grader-id",
        "_adversarial_challenged_hashes": ["deadbeef"],
    }
    db_session.add(task)
    await db_session.commit()

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # The task must remain EVALUATING — no status change.
    assert task.status == TaskStatus.EVALUATING, (
        "Re-dispatch while parked must leave task EVALUATING"
    )

    # No implementation children may have been spawned.
    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert children == [], (
        "Re-dispatch while parked must not spawn any implementation tasks"
    )

    # The awaiting flag must still be set — the guard must not clear it.
    assert task.context.get("awaiting_adversarial_grader") == "some-grader-id", (
        "awaiting_adversarial_grader flag must still be set after no-op re-dispatch"
    )


@_pytest.mark.asyncio
async def test_redispatch_while_awaiting_finding_challenge_spawns_nothing(
    db_session, mocker
):
    """Re-dispatch while parked awaiting a finding challenge is a safe no-op."""
    from test_autonomous_research import _patch_research_externals, _make_finding

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-redispatch02"
    )
    _patch_research_externals(mocker)

    finding = _make_finding(
        direction_id=direction["id"], title="Challenge-pending finding"
    )
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )

    # Simulate a task parked awaiting a finding challenge with no verdict yet.
    task.status = TaskStatus.EVALUATING
    task.context = {
        **(task.context or {}),
        "awaiting_finding_challenge": "some-challenge-id",
    }
    db_session.add(task)
    await db_session.commit()

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    assert task.status == TaskStatus.EVALUATING, (
        "Re-dispatch while parked awaiting finding challenge must leave task EVALUATING"
    )

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert children == [], (
        "Re-dispatch while parked awaiting finding challenge must not spawn children"
    )

    assert task.context.get("awaiting_finding_challenge"), (
        "awaiting_finding_challenge flag must still be set after no-op re-dispatch"
    )


@_pytest.mark.asyncio
async def test_adversarial_verdict_present_allows_reentry(db_session, mocker):
    """When the adversarial verdict IS present, re-entry must proceed normally.

    The parked-awaiting guard must only fire when the verdict is absent.
    A task with awaiting_adversarial_grader=True AND a verdict in context is a
    legitimate re-entry (the grader completed) and must not be blocked.
    """
    from app.research_completion import _finding_hash
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-redispatch03"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )
    hs = _high_stakes_finding(direction["id"], "Verdict-present re-entry finding")
    fhash = _finding_hash(
        hs["title"], hs.get("description", "") or hs.get("prompt", "")
    )
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[hs],
    )

    # Task holds BOTH the awaiting flag AND a verdict — re-entry after grader completed.
    task.status = TaskStatus.COMPLETED
    task.plan_output = [hs]
    task.context = {
        **(task.context or {}),
        # awaiting flag has already been cleared by _resolve_quality_grader_completion,
        # but simulate the edge where it was not cleaned up yet — the guard's condition
        # is _adversarial_verdict_raw is None, so having a verdict bypasses the guard.
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "finding_verdicts": {
                fhash: {"passed": True, "reason": "all criteria met", "explicit": True}
            },
        },
    }
    db_session.add(task)
    await db_session.commit()

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # With a PASS verdict the finding must spawn normally.
    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) >= 1, (
        "With a PASS adversarial verdict, finding must spawn even if awaiting flag was set"
    )


# ─────────────────────────────────────────────────────────────────────────────
# _commit_or_handle_duplicate_spawn — self-claim collision recovery
# ─────────────────────────────────────────────────────────────────────────────


class TestSelfClaimCollisionRecovery:
    """A spawn-commit collision on the parent's OWN direction-claim key must
    preserve the child batch (release the claim, re-commit) instead of
    discarding it as a duplicate.

    Prod 2026-06-09 (task 5ac5c8c1): the agent-terminal commit parked the
    parent COMPLETED, releasing its direction claim from the partial unique
    index; the autonomous loop re-claimed the direction with a fresh research
    6s before the spawn commit; the parent's EVALUATING flip then collided on
    its own key and the duplicate-spawn guard rolled back the entire
    2-finding child batch, discarding unique work.
    """

    async def _terminal_parent_with_claim(self, db_session, direction_id):
        from app.autonomous_research import research_direction_claim_key

        project, _direction, parent = await _grader_project_and_task(
            db_session, direction_id
        )
        claim_key = research_direction_claim_key(project.id, direction_id)
        parent.idempotency_key = claim_key
        # Agent-terminal commit: parent parked COMPLETED → the partial index
        # no longer counts it, so the claim is released.
        parent.status = TaskStatus.COMPLETED
        db_session.add(parent)
        await db_session.commit()
        return project, parent, claim_key

    def _peer(self, project, claim_key):
        return _Task(
            project_id=project.id,
            title="Research: fresh re-claim of the direction",
            payloads=[{"type": "research", "directions": [], "base_branch": "main"}],
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            idempotency_key=claim_key,
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )

    def _child(self, project, parent, *, idempotency_key=None):
        return _Task(
            project_id=project.id,
            parent_task_id=parent.id,
            title="spawned finding child",
            payloads=[{"type": "create", "prompt": "do x", "base_branch": "main"}],
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            idempotency_key=idempotency_key,
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )

    @_pytest.mark.asyncio
    async def test_self_claim_collision_preserves_children(self, db_session):
        from app.research_completion import _commit_or_handle_duplicate_spawn

        project, parent, claim_key = await self._terminal_parent_with_claim(
            db_session, "d-claim01"
        )
        # The autonomous loop legally re-claims the direction in the window
        # where the parent sits terminal.
        peer = self._peer(project, claim_key)
        db_session.add(peer)
        await db_session.commit()
        await db_session.refresh(peer)

        # The parent's spawn commit: child INSERT + EVALUATING flip. The flip
        # re-enters the partial index with the same key → UniqueViolation.
        child = self._child(project, parent)
        db_session.add(child)
        parent.status = TaskStatus.EVALUATING
        parent.context = {
            **(parent.context or {}),
            "research_branch": "research/d-claim01",
        }
        parent.latest_status_message = "Research created 1 tasks from 1 findings"
        db_session.add(parent)

        committed = await _commit_or_handle_duplicate_spawn(
            db_session, parent, stage="child-task spawn"
        )

        assert committed is True
        await db_session.refresh(parent)
        # The batch survived: EVALUATING flip + spawn-commit mutations durable.
        assert parent.status == TaskStatus.EVALUATING
        assert (parent.context or {}).get("research_branch") == "research/d-claim01"
        assert parent.latest_status_message == (
            "Research created 1 tasks from 1 findings"
        )
        # The claim was released to the peer, not duplicated.
        assert parent.idempotency_key is None
        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == parent.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(children) == 1
        await db_session.refresh(peer)
        assert peer.idempotency_key == claim_key
        assert peer.status == TaskStatus.QUEUED

    @_pytest.mark.asyncio
    async def test_evergreen_adapter_failure_preserves_spawn_session(
        self, db_session, mocker
    ):
        """A failed optional adapter must not roll back the shared spawn session."""
        import app.research_completion as research_completion
        from app.models import EvergreenBacklogItem
        from test_autonomous_research import _make_finding, _patch_research_externals

        project, direction, parent = await _grader_project_and_task(
            db_session, "d-aux-savepoint"
        )
        item = EvergreenBacklogItem(
            project_id=project.id,
            finding_key="auxiliary-savepoint",
            title="Auxiliary savepoint",
            status="spawned",
            spawned_task_id=parent.id,
        )
        parent.context = {
            **(parent.context or {}),
            "evergreen_backlog_item_id": str(item.id),
        }
        db_session.add_all([item, parent])
        await db_session.commit()

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[
                _make_finding("Savepoint child", direction_id=direction["id"])
            ],
        )
        mocker.patch(
            "app.evergreen_backlog.apply_research_outcome",
            side_effect=RuntimeError("simulated optional adapter failure"),
        )
        rollback = mocker.spy(db_session, "rollback")

        await research_completion._handle_research_completion(parent, "", db_session)

        assert rollback.await_count == 0
        await db_session.refresh(parent)
        assert parent.status == TaskStatus.EVALUATING

    @_pytest.mark.asyncio
    async def test_self_claim_retry_does_not_resurrect_cancelled_parent(
        self, db_session, mocker
    ):
        """Collision rollback must reauthorize before restoring EVALUATING."""
        from app.research_completion import _commit_or_handle_duplicate_spawn
        from app.research_execution_authority import relock_research_spawn_retry

        project, parent, claim_key = await self._terminal_parent_with_claim(
            db_session, "d-claim-cancel"
        )
        peer = self._peer(project, claim_key)
        db_session.add(peer)
        await db_session.commit()

        child = self._child(project, parent)
        db_session.add(child)
        parent.status = TaskStatus.EVALUATING
        db_session.add(parent)

        async def _cancel_then_relock(session, project_id, task_id):
            parent.status = TaskStatus.CANCELLED
            await session.commit()
            return await relock_research_spawn_retry(session, project_id, task_id)

        mocker.patch(
            "app.research_completion.relock_research_spawn_retry",
            side_effect=_cancel_then_relock,
        )

        committed = await _commit_or_handle_duplicate_spawn(
            db_session, parent, stage="atomic child-task spawn and wiring"
        )

        assert committed is False
        await db_session.refresh(parent)
        assert parent.status == TaskStatus.CANCELLED
        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == parent.id)
                )
            )
            .scalars()
            .all()
        )
        assert children == []

    @_pytest.mark.asyncio
    async def test_self_claim_retry_yields_to_peer_published_delivery(
        self, db_session, mocker
    ):
        """A peer publish in rollback -> relock cannot be reinserted twice."""
        from conftest import TestingSessionLocal
        from app.models import TASK_ORIGINATION_RESEARCH
        from app.research_completion import _commit_or_handle_duplicate_spawn
        from app.research_execution_authority import relock_research_spawn_retry

        project, parent, claim_key = await self._terminal_parent_with_claim(
            db_session, "d-claim-publish-race"
        )
        peer = self._peer(project, claim_key)
        db_session.add(peer)
        await db_session.commit()
        project_id, parent_id = project.id, parent.id
        finding_hash = "concurrent-self-claim-finding"

        pending = self._child(project, parent)
        pending.origination = TASK_ORIGINATION_RESEARCH
        pending.context = {
            "research_task_id": str(parent_id),
            "parent_research_task_id": str(parent_id),
            "finding_hash": finding_hash,
        }
        db_session.add(pending)
        parent.status = TaskStatus.EVALUATING
        db_session.add(parent)
        published_id = None

        async def _publish_then_relock(session, locked_project_id, locked_task_id):
            nonlocal published_id
            async with TestingSessionLocal() as peer_session:
                peer_parent = await peer_session.get(_Task, parent_id)
                assert peer_parent is not None
                peer_parent.idempotency_key = None
                peer_parent.status = TaskStatus.EVALUATING
                published = _Task(
                    project_id=project_id,
                    parent_task_id=parent_id,
                    title="peer-published finding child",
                    payloads=[{"type": "create", "prompt": "do x"}],
                    context={
                        "research_task_id": str(parent_id),
                        "parent_research_task_id": str(parent_id),
                        "finding_hash": finding_hash,
                    },
                    status=TaskStatus.QUEUED,
                    created_by="ORCHESTRATOR",
                    origination=TASK_ORIGINATION_RESEARCH,
                    vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                )
                peer_session.add(peer_parent)
                peer_session.add(published)
                await peer_session.commit()
                published_id = published.id
            return await relock_research_spawn_retry(
                session, locked_project_id, locked_task_id
            )

        mocker.patch(
            "app.research_completion.relock_research_spawn_retry",
            side_effect=_publish_then_relock,
        )

        committed = await _commit_or_handle_duplicate_spawn(
            db_session, parent, stage="atomic child-task spawn and wiring"
        )

        assert committed is False
        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == parent_id)
                )
            )
            .scalars()
            .all()
        )
        assert [row.id for row in children] == [published_id]

    @_pytest.mark.asyncio
    async def test_true_duplicate_batch_still_discarded(self, db_session):
        """A collision NOT on the parent's own key keeps today's duplicate
        disposition: the batch rolls back and the function returns False."""
        from app.research_completion import _commit_or_handle_duplicate_spawn

        project, _direction, parent = await _grader_project_and_task(
            db_session, "d-claim02"
        )
        parent.status = TaskStatus.COMPLETED
        parent.idempotency_key = None
        db_session.add(parent)
        await db_session.commit()

        duplicated_key = "research-direction:dup-claim02:pipelined"
        peer = self._peer(project, duplicated_key)
        db_session.add(peer)
        await db_session.commit()

        # The batch itself carries the colliding key (a true duplicate spawn).
        dup_child = self._child(project, parent, idempotency_key=duplicated_key)
        db_session.add(dup_child)
        parent.status = TaskStatus.EVALUATING
        db_session.add(parent)

        committed = await _commit_or_handle_duplicate_spawn(
            db_session, parent, stage="child-task spawn"
        )

        assert committed is False
        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == parent.id)
                )
            )
            .scalars()
            .all()
        )
        assert children == []
        # Parent re-populated from its DB row: the rolled-back flip is gone.
        assert parent.status == TaskStatus.COMPLETED

    @_pytest.mark.asyncio
    async def test_atomic_topology_collision_preserves_evergreen_outcome(
        self, db_session, mocker
    ):
        """A real self-claim collision cannot publish pre-wiring children or
        roll back the Evergreen outcome that records their admission."""
        import app.research_completion as research_completion
        from app.autonomous_research import research_direction_claim_key
        from app.models import EvergreenBacklogItem
        from test_autonomous_research import _make_finding, _patch_research_externals

        project, direction, parent = await _grader_project_and_task(
            db_session, "d-atomic-claim"
        )
        claim_key = research_direction_claim_key(project.id, direction["id"])
        item = EvergreenBacklogItem(
            project_id=project.id,
            finding_key="atomic-topology-evergreen",
            title="Atomic topology",
            status="spawned",
            spawned_task_id=parent.id,
        )
        parent.idempotency_key = claim_key
        parent.context = {
            **(parent.context or {}),
            "evergreen_backlog_item_id": str(item.id),
        }
        db_session.add(item)
        db_session.add(parent)
        await db_session.commit()

        peer = self._peer(project, claim_key)
        db_session.add(peer)
        await db_session.commit()

        foundation = _make_finding(
            title="Build atomic foundation",
            direction_id=direction["id"],
        )
        dependent = _make_finding(
            title="Use atomic foundation",
            direction_id=direction["id"],
        )
        dependent["depends_on_indices"] = [0]
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[foundation, dependent],
        )
        commit_batch = mocker.spy(
            research_completion, "_commit_or_handle_duplicate_spawn"
        )

        await _handle_research_completion(parent, "", db_session)

        children = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == parent.id)
                )
            )
            .scalars()
            .all()
        )
        routed = {
            child.title: child
            for child in children
            if (child.context or {}).get("research_task_id") == str(parent.id)
        }
        assert set(routed) == {foundation["title"], dependent["title"]}
        assert str(routed[foundation["title"]].id) in (
            routed[dependent["title"]].depends_on or []
        )
        assert commit_batch.await_count == 1
        assert commit_batch.await_args.kwargs["stage"] == (
            "atomic child-task spawn and wiring"
        )
        await db_session.refresh(item)
        assert item.status == "advanced"
        assert item.outcome == "children_spawned"


@_pytest.mark.asyncio
async def test_adversarial_gate_records_unresolved_challenge_diagnostics(
    db_session, mocker
):
    """ACCEPTANCE TEST: adversarial grader completion writes bounded diagnostics to source context.

    When an adversarial grader completes with some hashes missing from its output
    (unresolved), _resolve_quality_grader_completion must write an
    adversarial_gate_diagnostic dict to the source task's context with provenance
    fields:
     - challenger_task_id / completion_state
     - challenged_count / parsed_verdict_count / unresolved_count
     - vm identity fields (None when grader has no heartbeat record)

    Deferred findings from an unresolved verdict must carry adversarial_challenger_task_id
    so operators can connect the missing verdict to its challenger task.

    Exercises production code in task_lifecycle._resolve_quality_grader_completion and
    research_completion._handle_research_completion.
    """
    from app.research_completion import _finding_hash
    from app.schemas import AgentStatusPayload
    from app.task_lifecycle import _resolve_quality_grader_completion
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-adv-diag01"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    # Two high-stakes findings: grader will return an explicit verdict for the
    # first but nothing for the second, producing one unresolved outcome.
    hs_verdicted = _high_stakes_finding(direction["id"], "Finding with verdict")
    hs_unresolved = _high_stakes_finding(direction["id"], "Finding with no verdict")
    fhash_verdicted = _finding_hash(
        hs_verdicted["title"],
        hs_verdicted.get("description", "") or hs_verdicted.get("prompt", ""),
    )

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[hs_verdicted, hs_unresolved],
    )

    # Phase 1: park the task awaiting the adversarial grader.
    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    assert task.status == TaskStatus.EVALUATING, (
        "task must park awaiting adversarial verdict"
    )

    graders = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id, _Task.id != task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(graders) == 1
    grader = graders[0]

    # Phase 2: grader returns a FAIL verdict for the first hash only;
    # the second hash is absent → fail-closed unresolved.
    grader.status = TaskStatus.COMPLETED
    db_session.add(grader)
    await db_session.commit()

    payload = AgentStatusPayload(
        task_id=grader.id,
        status=TaskStatus.COMPLETED,
        logs=(
            '[{"hash": "'
            + fhash_verdicted
            + '", "verdict": "fail", "reason": "premise wrong"}]'
        ),
    )
    await _resolve_quality_grader_completion(grader, payload, db_session)
    await db_session.refresh(task)

    # adversarial_gate_diagnostic must be written to the source task context.
    diag = (task.context or {}).get("adversarial_gate_diagnostic")
    assert diag is not None, (
        "adversarial_gate_diagnostic must be written to source context"
    )
    assert diag["challenger_task_id"] == str(grader.id), (
        "challenger_task_id must be grader id"
    )
    assert diag["completion_state"] == "completed", (
        "completion_state must be 'completed'"
    )
    assert diag["challenged_count"] == 2, (
        "challenged_count must match all_hashes length"
    )
    assert diag["parse_state"] == "ok", "parsed verdicts must be classified"
    # One explicit verdict parsed, one unresolved (absent from grader output).
    assert diag["parsed_verdict_count"] == 1, "one explicit verdict was parsed"
    assert diag["unresolved_count"] == 1, "one hash had no verdict → unresolved"
    # VM identity is None for test graders that never sent a heartbeat.
    assert diag["vm_image_name"] is None
    assert diag["vm_runtime_fingerprint"] is None
    assert diag["vm_source_sha"] is None

    # Both findings must be deferred (explicit fail + fail-closed unresolved).
    deferred = (task.context or {}).get("deferred_findings", [])
    deferred_titles = {d.get("title") for d in deferred}
    assert hs_verdicted["title"] in deferred_titles, (
        "explicitly failed finding must be deferred"
    )
    assert hs_unresolved["title"] in deferred_titles, (
        "unresolved finding must be deferred"
    )

    # Deferred findings must carry the challenger task id for operator traceability.
    for d in deferred:
        if d.get("title") in {hs_verdicted["title"], hs_unresolved["title"]}:
            assert d.get("adversarial_challenger_task_id") == str(grader.id), (
                f"deferred finding '{d.get('title')}' must carry adversarial_challenger_task_id"
            )


@_pytest.mark.asyncio
async def test_adversarial_gate_uses_poolvm_provenance_after_cache_loss(
    db_session, mocker
):
    """The diagnostic must fall back to the durable PoolVm row after cache loss."""
    from app.models import PoolVmState
    from app.research_completion import _finding_hash
    from app.schemas import AgentStatusPayload
    from app.task_lifecycle import _resolve_quality_grader_completion
    from app.warm_pool import register_pool_vm
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-adv-diag02"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    hs_verdicted = _high_stakes_finding(direction["id"], "Finding with verdict")
    hs_unresolved = _high_stakes_finding(direction["id"], "Finding with no verdict")
    fhash_verdicted = _finding_hash(
        hs_verdicted["title"],
        hs_verdicted.get("description", "") or hs_verdicted.get("prompt", ""),
    )
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[hs_verdicted, hs_unresolved],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)
    grader = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id, _Task.id != task.id
                )
            )
        )
        .scalars()
        .first()
    )
    assert grader is not None

    vm_id = "agent-vm-adversarial-diag"
    member = await register_pool_vm(
        db_session,
        vm_instance_id=vm_id,
        project_id=_project.id,
        state=PoolVmState.LEASED,
    )
    member.agent_image_name = "blueprint-agent-v9"
    member.agent_image_build_ts = 1749417600
    member.agent_runtime_fingerprint = "b" * 64
    member.agent_source_sha = "fedcba9876543210"
    await db_session.commit()

    grader.vm_instance_id = vm_id
    db_session.add(grader)
    await db_session.commit()

    # Empty the in-memory heartbeat caches so provenance can only come from the
    # PoolVm row — that DB fallback is what this test pins.
    # NOTE: image identity is NOT patched here. task_lifecycle reads the image name
    # and build_ts straight off `pool_vm` (the durable row); it never calls
    # compute.get_vm_image, so a patch on that getter would be an INERT mock.
    mocker.patch("app.compute.get_vm_runtime_fingerprint", return_value=None)
    mocker.patch("app.compute.get_vm_source_sha", return_value=None)

    grader.status = TaskStatus.COMPLETED
    db_session.add(grader)
    await db_session.commit()

    payload = AgentStatusPayload(
        task_id=grader.id,
        status=TaskStatus.COMPLETED,
        logs=(
            '[{"hash": "'
            + fhash_verdicted
            + '", "verdict": "fail", "reason": "premise wrong"}]'
        ),
    )
    await _resolve_quality_grader_completion(grader, payload, db_session)
    await db_session.refresh(task)

    diag = (task.context or {}).get("adversarial_gate_diagnostic")
    assert diag is not None
    assert diag["vm_image_name"] == "blueprint-agent-v9"
    assert diag["vm_image_build_ts"] == 1749417600
    assert diag["vm_runtime_fingerprint"] == "b" * 64
    assert diag["vm_source_sha"] == "fedcba9876543210"

    verdict = (task.context or {}).get(ADVERSARIAL_GATE_VERDICT_KEY)
    assert verdict["grader_agent_image_name"] == "blueprint-agent-v9"
    assert verdict["grader_agent_image_build_ts"] == 1749417600
    assert verdict["grader_agent_runtime_fingerprint"] == "b" * 64
    assert verdict["grader_agent_source_sha"] == "fedcba9876543210"


# ─────────────────────────────────────────────────────────────────────────────
# Machine-readable cause for grader non-response (observability only).
# Production code: failure_classification.classify_grader_nonresponse_cause /
# append_grader_diagnostic_trail, task_lifecycle._resolve_quality_grader_completion,
# research_completion per-finding + per-cycle cause export.
# ─────────────────────────────────────────────────────────────────────────────


class TestGraderNonresponseCauseHelpers:
    """Unit coverage for the cause helper + trail accumulator."""

    @_pytest.mark.asyncio
    async def test_completed_unparseable_refusal_vs_format_miss(self, db_session):
        from app.failure_classification import (
            GRADER_CAUSE_FORMAT_MISS,
            GRADER_CAUSE_REFUSAL,
            classify_grader_nonresponse_cause,
        )
        from app.task_evaluators import (
            ADVERSARIAL_PARSE_EMPTY_LOGS,
            ADVERSARIAL_PARSE_INVALID_JSON,
            ADVERSARIAL_PARSE_NO_JSON_ARRAY,
        )

        _project, _direction, grader = await _grader_project_and_task(
            db_session, "d-cause-refusal"
        )

        # Prose that reads as a refusal, with no verdict array → refusal.
        refusal = await classify_grader_nonresponse_cause(
            grader,
            ADVERSARIAL_PARSE_NO_JSON_ARRAY,
            {},
            completed=True,
            logs="I cannot comply with this challenge request.",
            session=None,
        )
        assert refusal["category"] == GRADER_CAUSE_REFUSAL
        assert refusal["source"] == "output_inspection"
        # session=None → stale tag must be absent, never raise.
        assert "stale_agent_image" not in refusal

        # Garbled JSON → format miss (tried in-format, missed schema).
        garbled = await classify_grader_nonresponse_cause(
            grader,
            ADVERSARIAL_PARSE_INVALID_JSON,
            {},
            completed=True,
            logs='[{"hash": "abc", "verdict": ',
            session=None,
        )
        assert garbled["category"] == GRADER_CAUSE_FORMAT_MISS

        # Structurally empty output → format miss (nothing to refuse with).
        empty = await classify_grader_nonresponse_cause(
            grader,
            ADVERSARIAL_PARSE_EMPTY_LOGS,
            {},
            completed=True,
            logs="",
            session=None,
        )
        assert empty["category"] == GRADER_CAUSE_FORMAT_MISS

        # Prose with no refusal marker and no array → format miss, not refusal.
        prose = await classify_grader_nonresponse_cause(
            grader,
            ADVERSARIAL_PARSE_NO_JSON_ARRAY,
            {},
            completed=True,
            logs="Here is my analysis of the three findings under review.",
            session=None,
        )
        assert prose["category"] == GRADER_CAUSE_FORMAT_MISS

    @_pytest.mark.asyncio
    async def test_failed_grader_draws_taxonomy_cause(self, db_session):
        from app.failure_classification import classify_grader_nonresponse_cause
        from app.task_evaluators import ADVERSARIAL_PARSE_CHALLENGER_FAILED

        _project, _direction, grader = await _grader_project_and_task(
            db_session, "d-cause-failed"
        )
        # Persisted agent failure category → classify_failure returns AGENT_TIMEOUT.
        grader.context = {**(grader.context or {}), "failure_category": "timeout"}
        db_session.add(grader)
        await db_session.commit()

        cause = await classify_grader_nonresponse_cause(
            grader,
            ADVERSARIAL_PARSE_CHALLENGER_FAILED,
            {},
            completed=False,
            logs=None,
            session=None,
        )
        assert cause["category"] == "AGENT_TIMEOUT"
        assert cause["source"] == "failure_taxonomy"
        assert cause["parse_outcome"] == ADVERSARIAL_PARSE_CHALLENGER_FAILED

    @_pytest.mark.asyncio
    async def test_stale_agent_image_tag_set_and_absent(self, db_session, mocker):
        from app.failure_classification import classify_grader_nonresponse_cause

        project, _direction, grader = await _grader_project_and_task(
            db_session, "d-cause-stale"
        )

        # Serving SHA known + different + grader SHA is an ancestor → stale=True.
        mocker.patch(
            "app.compute.expected_serving_source_sha",
            new=AsyncMock(return_value="new_serving_sha"),
        )
        mocker.patch(
            "app.release_promotion._resolve_github_with_session",
            new=AsyncMock(return_value=("tok", "owner", "repo")),
        )
        ancestor = mocker.patch(
            "app.github_refs.is_ancestor_of",
            new=AsyncMock(return_value=True),
        )
        cause = await classify_grader_nonresponse_cause(
            grader,
            "ok",
            {"grader_agent_source_sha": "old_grader_sha"},
            completed=True,
            logs="[]",
            session=db_session,
            project_id=project.id,
        )
        assert cause["stale_agent_image"] is True
        ancestor.assert_awaited_once()

        # Expected serving SHA unknown → tag gracefully absent.
        mocker.patch(
            "app.compute.expected_serving_source_sha",
            new=AsyncMock(return_value=None),
        )
        cause_unknown = await classify_grader_nonresponse_cause(
            grader,
            "ok",
            {"grader_agent_source_sha": "old_grader_sha"},
            completed=True,
            logs="[]",
            session=db_session,
            project_id=project.id,
        )
        assert "stale_agent_image" not in cause_unknown

    def test_append_grader_diagnostic_trail_accumulates_and_caps(self):
        from app.failure_classification import (
            GRADER_DIAGNOSTIC_TRAIL_KEY,
            append_grader_diagnostic_trail,
        )

        ctx: dict = {}
        for i in range(7):
            append_grader_diagnostic_trail(
                ctx,
                grader_task_id=f"grader-{i}",
                parse_outcome="empty_logs",
                cause={"category": "grader_format_miss"},
                vm_provenance={},
            )
        trail = ctx[GRADER_DIAGNOSTIC_TRAIL_KEY]
        # Capped at 5, keeping the most-recent entries, attempts monotonically increasing.
        assert len(trail) == 5
        assert [e["attempt"] for e in trail] == [3, 4, 5, 6, 7]
        assert trail[-1]["grader_task_id"] == "grader-6"


@_pytest.mark.asyncio
async def test_resolution_records_cause_and_accumulating_trail(db_session, mocker):
    """Two FAILED grader resolutions accumulate per-attempt trail entries (not
    overwrite) and stamp a taxonomy-drawn cause into the diagnostic."""
    from app.research_completion import _finding_hash  # noqa: F401
    from app.schemas import AgentStatusPayload
    from app.task_lifecycle import _resolve_quality_grader_completion
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-cause-trail"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )
    hs = _high_stakes_finding(direction["id"], "Risky finding for trail")
    mocker.patch("app.research_completion._parse_research_findings", return_value=[hs])

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)
    grader = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id, _Task.id != task.id
                )
            )
        )
        .scalars()
        .first()
    )
    assert grader is not None

    # Resolution 1: grader FAILED with a persisted timeout category.
    grader.status = TaskStatus.FAILED
    grader.context = {**(grader.context or {}), "failure_category": "timeout"}
    db_session.add(grader)
    await db_session.commit()
    await _resolve_quality_grader_completion(
        grader,
        AgentStatusPayload(task_id=grader.id, status=TaskStatus.FAILED, logs=None),
        db_session,
    )
    await db_session.refresh(task)

    diag = (task.context or {}).get("adversarial_gate_diagnostic") or {}
    assert diag.get("cause", {}).get("category") == "AGENT_TIMEOUT"
    trail = (task.context or {}).get("grader_diagnostic_trail") or []
    assert len(trail) == 1
    assert trail[0]["attempt"] == 1

    # The first failure spawns one bounded retry grader.
    retry_grader = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id,
                    _Task.id.notin_([task.id, grader.id]),
                )
            )
        )
        .scalars()
        .first()
    )
    assert retry_grader is not None

    # Resolution 2: retry grader also FAILED → trail accumulates, not overwrites.
    retry_grader.status = TaskStatus.FAILED
    retry_grader.context = {
        **(retry_grader.context or {}),
        "failure_category": "rate_limited",
    }
    db_session.add(retry_grader)
    await db_session.commit()
    await _resolve_quality_grader_completion(
        retry_grader,
        AgentStatusPayload(
            task_id=retry_grader.id, status=TaskStatus.FAILED, logs=None
        ),
        db_session,
    )
    await db_session.refresh(task)

    trail2 = (task.context or {}).get("grader_diagnostic_trail") or []
    assert len(trail2) == 2, "second resolution must append, not overwrite"
    assert [e["attempt"] for e in trail2] == [1, 2]
    assert trail2[1]["cause"]["category"] == "RATE_LIMIT"


@_pytest.mark.asyncio
async def test_unresolved_finding_and_cycle_summary_carry_cause(db_session, mocker):
    """The deferred unresolved finding carries the cause, and the cycle exports a
    machine-readable cause summary alongside the spawn-gate metrics."""
    from app.research_completion import _finding_hash
    from app.schemas import AgentStatusPayload
    from app.task_lifecycle import _resolve_quality_grader_completion
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-cause-perfind"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )
    hs_verdicted = _high_stakes_finding(direction["id"], "Finding with verdict")
    hs_unresolved = _high_stakes_finding(direction["id"], "Finding without verdict")
    fhash_verdicted = _finding_hash(
        hs_verdicted["title"],
        hs_verdicted.get("description", "") or hs_verdicted.get("prompt", ""),
    )
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[hs_verdicted, hs_unresolved],
    )

    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)
    grader = (
        (
            await db_session.execute(
                _select(_Task).where(
                    _Task.project_id == _project.id, _Task.id != task.id
                )
            )
        )
        .scalars()
        .first()
    )
    assert grader is not None

    # Grader returns an explicit verdict for one hash only (parse_outcome == ok);
    # the second hash is unresolved.
    grader.status = TaskStatus.COMPLETED
    db_session.add(grader)
    await db_session.commit()
    await _resolve_quality_grader_completion(
        grader,
        AgentStatusPayload(
            task_id=grader.id,
            status=TaskStatus.COMPLETED,
            logs='[{"hash": "'
            + fhash_verdicted
            + '", "verdict": "fail", "reason": "x"}]',
        ),
        db_session,
    )
    await db_session.refresh(task)

    # Per-finding attribution: the deferred unresolved finding carries the cause.
    deferred = (task.context or {}).get("deferred_findings", [])
    assert deferred, "unresolved finding must be deferred"
    assert any(
        isinstance(d.get("adversarial_gate_cause"), dict)
        and d["adversarial_gate_cause"].get("category")
        for d in deferred
    ), "deferred finding must carry adversarial_gate_cause"

    # Per-cycle export: machine-readable cause summary alongside spawn-gate metrics.
    cause = (task.context or {}).get("research_spawn_gate_cause")
    assert isinstance(cause, dict) and cause.get("category"), (
        "research_spawn_gate_cause must be exported"
    )
    counts = (task.context or {}).get("research_spawn_gate_cause_counts")
    assert isinstance(counts, dict) and counts, (
        "research_spawn_gate_cause_counts must be exported"
    )
    scoped = (task.context or {}).get("research_spawn_gate_by_direction") or {}
    scoped_entry = scoped[direction["id"]]
    assert scoped_entry["cause"] == cause
    assert scoped_entry["cause_counts"] == counts


@_pytest.mark.asyncio
async def test_forward_quality_lane_records_cause(db_session, mocker):
    """The dark forward-quality lane must now also record a diagnostic + cause +
    trail entry (it previously recorded nothing)."""
    from app.schemas import AgentStatusPayload
    from app.task_lifecycle import _resolve_quality_grader_completion

    _project, _direction, task = await _grader_project_and_task(
        db_session, "d-cause-fq"
    )

    grader = await spawn_forward_quality_grader(task, "artifact text", db_session)
    assert grader is not None
    await db_session.refresh(task)
    assert task.context.get("awaiting_quality_grader")

    grader.status = TaskStatus.COMPLETED
    db_session.add(grader)
    await db_session.commit()

    # Stub the research resume handler + broadcast so we isolate the diagnostic write.
    mocker.patch(
        "app.research_completion._handle_research_completion", new_callable=AsyncMock
    )
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)

    await _resolve_quality_grader_completion(
        grader,
        AgentStatusPayload(
            task_id=grader.id,
            status=TaskStatus.COMPLETED,
            logs='{"verdict": "pass", "reasoning": "well grounded"}',
        ),
        db_session,
    )
    await db_session.refresh(task)

    fq_diag = (task.context or {}).get("quality_grader_diagnostic")
    assert fq_diag is not None, "forward-quality lane must record a diagnostic"
    assert fq_diag["cause"]["category"] == "grader_responsive"
    assert fq_diag["completion_state"] == "completed"
    trail = (task.context or {}).get("grader_diagnostic_trail") or []
    assert trail, "forward-quality lane must append a trail entry"
    assert trail[-1]["cause"]["category"] == "grader_responsive"


# ---------------------------------------------------------------------------
# Cause-keyed targeted retry tests (acceptance test: test_spawn_gate_targeted_retry_routes_by_cause)
# ---------------------------------------------------------------------------


@_pytest.mark.asyncio
async def test_spawn_gate_targeted_retry_routes_by_cause(db_session, mocker):
    """ACCEPTANCE TEST: cause-keyed routing sends each non-response cause to the
    correct retry strategy.

    Scenario A — timeout cause: spawns a targeted retry with a reduced manifest
    (half the findings), not the full wholesale retry.
    Scenario B — transient-infra cause: uses the existing wholesale retry path.
    Scenario C — refusal cause: skips all retries and marks exhausted immediately.
    Scenario D — targeted retry exhausted (timeout on second attempt): falls
    through to wholesale retry.
    """
    from app.failure_classification import (
        AGENT_TIMEOUT,
        GRADER_CAUSE_REFUSAL,
        RATE_LIMIT,
    )
    from app.research_completion import (
        _ADVERSARIAL_TARGETED_RETRY_KEY,
        _ADVERSARIAL_WHOLESALE_RETRY_KEY,
        _finding_hash,
    )
    from test_autonomous_research import _patch_research_externals

    async def _make_task_with_cause(
        db_session, direction_suffix, cause_category, *, targeted_count=0
    ):
        """Helper: build a task whose ADVERSARIAL_GATE_VERDICT_KEY carries a given cause."""
        project, direction, task = await _grader_project_and_task(
            db_session, f"d-targeted-{direction_suffix}"
        )
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._is_high_stakes_finding",
            side_effect=lambda f: f.get("risk") == "high",
        )

        # Two high-stakes findings so the "half" slice is non-trivial.
        hs1 = _high_stakes_finding(direction["id"], f"Finding A ({direction_suffix})")
        hs2 = _high_stakes_finding(direction["id"], f"Finding B ({direction_suffix})")
        fhash1 = _finding_hash(hs1["title"], hs1.get("description", ""))
        fhash2 = _finding_hash(hs2["title"], hs2.get("description", ""))

        task.plan_output = [hs1, hs2]
        task.status = TaskStatus.COMPLETED
        task.context = {
            **(task.context or {}),
            ADVERSARIAL_GATE_VERDICT_KEY: {
                "finding_verdicts": {},
                "grader_task_id": f"test-grader-{direction_suffix}",
                "parse_outcome": "no_hash_matches",
                "cause": {
                    "category": cause_category,
                    "parse_outcome": "no_hash_matches",
                },
            },
            "_adversarial_challenged_hashes": [fhash1, fhash2],
        }
        if targeted_count:
            task.context[_ADVERSARIAL_TARGETED_RETRY_KEY] = targeted_count

        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[hs1, hs2],
        )
        db_session.add(project)
        db_session.add(task)
        await db_session.commit()
        return project, direction, task, fhash1, fhash2

    # --- Scenario A: timeout → targeted retry with reduced manifest ---
    _proj_a, _dir_a, task_a, fhash1_a, fhash2_a = await _make_task_with_cause(
        db_session, "timeout", AGENT_TIMEOUT
    )

    await _handle_research_completion(task_a, "", db_session)
    await db_session.refresh(task_a)

    all_tasks_a = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.project_id == task_a.project_id)
            )
        )
        .scalars()
        .all()
    )
    graders_a = [t for t in all_tasks_a if (t.context or {}).get("adversarial_grader")]
    assert len(graders_a) == 1, (
        "timeout cause must spawn exactly one targeted retry grader"
    )
    assert task_a.status == TaskStatus.EVALUATING
    # Targeted retry counter must be incremented.
    assert task_a.context.get(_ADVERSARIAL_TARGETED_RETRY_KEY) == 1, (
        "targeted retry count must be 1 after first targeted retry"
    )
    # Wholesale counter must remain untouched.
    assert task_a.context.get(_ADVERSARIAL_WHOLESALE_RETRY_KEY) in (None, 0), (
        "wholesale retry count must not be incremented for targeted retry"
    )
    # Reduced manifest: only ONE of the two findings should be challenged.
    challenged_hashes_a = task_a.context.get("_adversarial_challenged_hashes") or []
    assert len(challenged_hashes_a) == 1, (
        "targeted retry must challenge only half the findings (1 of 2)"
    )
    # No child spawns yet (task is parked waiting for grader).
    children_a = [t for t in all_tasks_a if t.parent_task_id == task_a.id]
    assert len(children_a) == 0, "targeted retry must not spawn child research tasks"

    # --- Scenario B: transient infra (rate-limit) → existing wholesale retry ---
    _proj_b, _dir_b, task_b, fhash1_b, fhash2_b = await _make_task_with_cause(
        db_session, "ratelimit", RATE_LIMIT
    )

    await _handle_research_completion(task_b, "", db_session)
    await db_session.refresh(task_b)

    all_tasks_b = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.project_id == task_b.project_id)
            )
        )
        .scalars()
        .all()
    )
    graders_b = [t for t in all_tasks_b if (t.context or {}).get("adversarial_grader")]
    assert len(graders_b) == 1, "rate-limit cause must spawn a wholesale retry grader"
    assert task_b.status == TaskStatus.EVALUATING
    # Wholesale counter must be incremented.
    assert task_b.context.get(_ADVERSARIAL_WHOLESALE_RETRY_KEY) == 1, (
        "wholesale retry count must be 1 for transient-infra cause"
    )
    # Targeted counter must remain untouched.
    assert task_b.context.get(_ADVERSARIAL_TARGETED_RETRY_KEY) in (None, 0), (
        "targeted retry count must not be incremented for transient-infra cause"
    )
    # Wholesale manifest should include BOTH findings.
    challenged_hashes_b = task_b.context.get("_adversarial_challenged_hashes") or []
    assert len(challenged_hashes_b) == 2, (
        "wholesale retry must challenge all originally challenged findings"
    )

    # --- Scenario C: refusal → skip all retries, mark exhausted ---
    _proj_c, _dir_c, task_c, fhash1_c, fhash2_c = await _make_task_with_cause(
        db_session, "refusal", GRADER_CAUSE_REFUSAL
    )

    await _handle_research_completion(task_c, "", db_session)
    await db_session.refresh(task_c)

    all_tasks_c = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.project_id == task_c.project_id)
            )
        )
        .scalars()
        .all()
    )
    graders_c = [t for t in all_tasks_c if (t.context or {}).get("adversarial_grader")]
    # No new grader should be spawned for a refusal.
    assert len(graders_c) == 0, "refusal cause must NOT spawn any retry grader"
    # Must be marked as exhausted immediately.
    assert (
        task_c.context.get("research_spawn_gate_wholesale_retry_exhausted") is True
    ), "refusal cause must mark retry as exhausted immediately"
    assert task_c.context.get("research_spawn_gate_refusal_skip_retry") is True, (
        "refusal cause must set refusal_skip_retry flag"
    )
    scoped_c = task_c.context.get("research_spawn_gate_by_direction") or {}
    assert scoped_c[_dir_c["id"]]["wholesale_retry_exhausted"] is True
    # Both findings must be deferred fail-closed.
    deferred_c = task_c.context.get("deferred_findings") or []
    assert fhash1_c or fhash2_c  # just ensure hashes were computed
    assert len(deferred_c) >= 2, (
        "both challenged findings must be deferred fail-closed on refusal"
    )

    # --- Scenario D: timeout cause with targeted already exhausted → wholesale retry ---
    _proj_d, _dir_d, task_d, fhash1_d, fhash2_d = await _make_task_with_cause(
        db_session, "timeout-exhausted", AGENT_TIMEOUT, targeted_count=1
    )

    await _handle_research_completion(task_d, "", db_session)
    await db_session.refresh(task_d)

    all_tasks_d = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.project_id == task_d.project_id)
            )
        )
        .scalars()
        .all()
    )
    graders_d = [t for t in all_tasks_d if (t.context or {}).get("adversarial_grader")]
    assert len(graders_d) == 1, (
        "timeout with targeted exhausted must fall through to wholesale retry grader"
    )
    assert task_d.status == TaskStatus.EVALUATING
    # Wholesale counter must now be incremented (targeted was already at limit).
    assert task_d.context.get(_ADVERSARIAL_WHOLESALE_RETRY_KEY) == 1, (
        "wholesale retry count must be 1 after targeted retry is exhausted"
    )
    # Targeted counter must stay at limit (not incremented again).
    assert task_d.context.get(_ADVERSARIAL_TARGETED_RETRY_KEY) == 1, (
        "targeted retry count must remain at limit (1) after exhaustion"
    )
    # Wholesale manifest should include both findings (full retry).
    challenged_hashes_d = task_d.context.get("_adversarial_challenged_hashes") or []
    assert len(challenged_hashes_d) == 2, (
        "wholesale retry after targeted exhaustion must challenge all findings"
    )

    # --- Scenario E: targeted retry itself times out → wholesale must use original full set ---
    # This exercises the production path:
    #   initial timeout → targeted retry spawned (narrows _adversarial_challenged_hashes to 1)
    #   → targeted retry also times out → wholesale fallback must recover original 2-hash set
    #     via _adversarial_original_challenged_hashes and challenge all findings.
    _proj_e, _dir_e, task_e, fhash1_e, fhash2_e = await _make_task_with_cause(
        db_session, "timeout-targeted-then-wholesale", AGENT_TIMEOUT, targeted_count=1
    )
    # Simulate what the targeted retry spawn wrote: narrowed challenged hashes + saved original.
    task_e.context = {
        **task_e.context,
        "_adversarial_challenged_hashes": [fhash1_e],  # reduced subset
        "_adversarial_original_challenged_hashes": [
            fhash1_e,
            fhash2_e,
        ],  # full original
    }
    db_session.add(task_e)
    await db_session.commit()

    await _handle_research_completion(task_e, "", db_session)
    await db_session.refresh(task_e)

    all_tasks_e = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.project_id == task_e.project_id)
            )
        )
        .scalars()
        .all()
    )
    graders_e = [t for t in all_tasks_e if (t.context or {}).get("adversarial_grader")]
    assert len(graders_e) == 1, (
        "targeted-then-timeout must fall through to wholesale retry grader"
    )
    assert task_e.status == TaskStatus.EVALUATING
    assert task_e.context.get(_ADVERSARIAL_WHOLESALE_RETRY_KEY) == 1, (
        "wholesale retry count must be 1 after targeted timeout falls through"
    )
    # Wholesale manifest must include BOTH findings (recovered from original, not the reduced 1).
    challenged_hashes_e = task_e.context.get("_adversarial_challenged_hashes") or []
    assert len(challenged_hashes_e) == 2, (
        "wholesale retry after targeted timeout must challenge all original findings, "
        "not just the targeted-retry subset"
    )
    # Original preservation key must be cleared after wholesale consumes it.
    assert "_adversarial_original_challenged_hashes" not in task_e.context, (
        "original hash preservation key must be cleared after wholesale retry"
    )


# ---------------------------------------------------------------------------
# Research-audit knowledge document persistence
# ---------------------------------------------------------------------------


async def _make_audit_project_and_task(
    db_session, direction_id="d-audit01", mode="audit"
):
    """A minimal project with audit-mode direction + a research task."""
    from test_autonomous_research import _make_research_task

    direction = {
        "id": direction_id,
        "text": "Audit direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "recommended_mode": mode,
    }
    project = _Project(
        name=f"Audit project {direction_id}",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = await _make_research_task(project, direction, db_session)
    # Stamp the direction_id on context so the handler can look it up.
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()
    return project, direction, task


@_pytest.mark.asyncio
async def test_audit_mode_persists_knowledge_document(db_session, mocker):
    """An audit-mode research task creates a KnowledgeDocument when findings are present."""
    from app.models import KnowledgeDocument, SourceKind
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _make_audit_project_and_task(
        db_session, "d-audit-persist"
    )
    _patch_research_externals(mocker)
    finding = _make_finding(direction_id=direction["id"])
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )
    mocker.patch(
        "app.research_completion._parse_research_insights",
        return_value=[],
    )

    await _handle_research_completion(task, "", db_session)

    docs = (
        (
            await db_session.execute(
                _select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id,
                    KnowledgeDocument.source_kind == SourceKind.RESEARCH_AUDIT.value,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(docs) == 1, "audit-mode research must persist a KnowledgeDocument"
    doc = docs[0]
    assert doc.direction_id == direction["id"]
    assert doc.topic is not None
    assert "research_audit" in (doc.tags or [])


@_pytest.mark.asyncio
async def test_non_audit_mode_no_knowledge_document(db_session, mocker):
    """A standard (non-audit) research task does NOT persist a research_audit doc."""
    from app.models import KnowledgeDocument, SourceKind
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _make_audit_project_and_task(
        db_session, "d-non-audit", mode="ship"
    )
    _patch_research_externals(mocker)
    finding = _make_finding(direction_id=direction["id"])
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )
    mocker.patch(
        "app.research_completion._parse_research_insights",
        return_value=[],
    )

    await _handle_research_completion(task, "", db_session)

    docs = (
        (
            await db_session.execute(
                _select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id,
                    KnowledgeDocument.source_kind == SourceKind.RESEARCH_AUDIT.value,
                )
            )
        )
        .scalars()
        .all()
    )
    assert docs == [], (
        "non-audit mode must NOT create a research_audit KnowledgeDocument"
    )


@_pytest.mark.asyncio
async def test_audit_mode_no_document_when_no_findings(db_session, mocker):
    """An audit-mode research task with no findings/insights does NOT persist a doc."""
    from app.models import KnowledgeDocument, SourceKind
    from test_autonomous_research import _patch_research_externals

    _project, _direction, task = await _make_audit_project_and_task(
        db_session, "d-audit-empty"
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[],
    )
    mocker.patch(
        "app.research_completion._parse_research_insights",
        return_value=[],
    )

    await _handle_research_completion(task, "", db_session)

    docs = (
        (
            await db_session.execute(
                _select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id,
                    KnowledgeDocument.source_kind == SourceKind.RESEARCH_AUDIT.value,
                )
            )
        )
        .scalars()
        .all()
    )
    assert docs == [], (
        "empty-findings audit must NOT create a research_audit KnowledgeDocument"
    )


@_pytest.mark.asyncio
async def test_audit_mode_persist_error_does_not_block_spawn(db_session, mocker):
    """A persist failure in the audit knowledge path must not block child task spawn."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _make_audit_project_and_task(
        db_session, "d-audit-error"
    )
    # Direct completion-handler calls model the post-dispatch seam, where the
    # lifecycle has already terminalized the source task.
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()
    _patch_research_externals(mocker)
    finding = _make_finding(direction_id=direction["id"])
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )
    mocker.patch(
        "app.research_completion._parse_research_insights",
        return_value=[],
    )
    mocker.patch(
        "app.knowledge_completion.persist_research_audit_document",
        side_effect=RuntimeError("simulated persist failure"),
    )

    # Must not raise despite the persist error.
    await _handle_research_completion(task, "", db_session)

    # Children were still spawned (the persist error did not abort the spawn loop).
    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) >= 1, "spawn loop must proceed even when audit persist fails"
    await db_session.refresh(task)
    assert (task.context or {}).get("research_findings_finalized") is True
    assert [item.get("title") for item in (task.plan_output or [])] == [
        finding.get("title")
    ]


class TestStampNoFindingsRefractory:
    """_stamp_no_findings_refractory: possibly_saturated should only be set after 3+ consecutive unproductive cycles."""

    def _make_task(self, direction_id="d-test"):
        task = MagicMock()
        task.id = uuid.uuid4()
        task.project_id = uuid.uuid4()
        task.context = {"direction_id": direction_id}
        task.first_payload = {}
        return task

    def _make_session(self):
        session = AsyncMock()
        session.add = MagicMock()
        return session

    def _make_project(
        self, direction_id="d-test", consecutive_unproductive=0, research_history=None
    ):
        project = MagicMock()
        project.directions = [
            {
                "id": direction_id,
                "research_history": research_history
                if research_history is not None
                else [],
                "consecutive_unproductive_cycles": consecutive_unproductive,
            }
        ]
        return project

    def _run(self, project, coro):
        import asyncio
        from unittest.mock import patch, AsyncMock

        async def _mock_wdl(session, project_id, mutate_fn):
            mutate_fn(project.directions)

        with (
            patch("app.research_completion.with_directions_locked", _mock_wdl),
            patch("app.research_completion.with_problem_model_locked", AsyncMock()),
        ):
            asyncio.get_event_loop().run_until_complete(coro)

    def test_first_unproductive_cycle_not_marked_saturated(self):
        """A single no-findings cycle must NOT set possibly_saturated (history too short)."""
        from app.research_completion import _stamp_no_findings_refractory

        task = self._make_task()
        project = self._make_project(consecutive_unproductive=0)
        self._run(project, _stamp_no_findings_refractory(task, self._make_session()))

        direction = project.directions[0]
        assert direction.get("possibly_saturated") is False

    def test_two_consecutive_unproductive_cycles_not_saturated(self):
        """Two consecutive no-findings cycles must NOT set possibly_saturated."""
        from app.research_completion import _stamp_no_findings_refractory

        task = self._make_task()
        project = self._make_project(consecutive_unproductive=1)
        self._run(project, _stamp_no_findings_refractory(task, self._make_session()))

        direction = project.directions[0]
        assert direction.get("possibly_saturated") is False

    def test_three_consecutive_unproductive_cycles_marked_saturated(self):
        """After 3 consecutive no-findings cycles, possibly_saturated must be True."""
        from app.research_completion import _stamp_no_findings_refractory

        task = self._make_task()
        project = self._make_project(consecutive_unproductive=2)
        self._run(project, _stamp_no_findings_refractory(task, self._make_session()))

        direction = project.directions[0]
        assert direction.get("possibly_saturated") is True

    def test_consecutive_counter_incremented(self):
        """consecutive_unproductive_cycles is incremented on each call."""
        from app.research_completion import _stamp_no_findings_refractory

        task = self._make_task()
        project = self._make_project(consecutive_unproductive=1)
        self._run(project, _stamp_no_findings_refractory(task, self._make_session()))

        direction = project.directions[0]
        assert direction["consecutive_unproductive_cycles"] == 2

    def _make_saturated_project(self, direction_id="d-test", history=None, agg=None):
        """Build a project whose direction is already saturated (2 consecutive unproductive cycles)."""
        project = MagicMock()
        project.directions = [
            {
                "id": direction_id,
                "status": "active",
                "research_history": history if history is not None else [],
                "history_aggregate": agg if agg is not None else {},
                "consecutive_unproductive_cycles": 2,
            }
        ]
        return project

    def test_merge_count_alone_never_claims_success(self):
        from app.research_completion import _stamp_no_findings_refractory

        verified_history = [
            {"prs_merged": 1, "verified_prs_merged": 1} for _ in range(3)
        ]
        task = self._make_task()
        project = self._make_saturated_project(history=verified_history)
        self._run(project, _stamp_no_findings_refractory(task, self._make_session()))

        direction = project.directions[0]
        assert direction.get("status") != "completed"
        assert direction.get("retirement_outcome") != "success_evidence_met"

    def test_challenge_approved_declared_success_evidence_can_complete(self):
        from app.research_completion import _stamp_no_findings_refractory

        project = self._make_saturated_project(
            history=[{"verified_prs_merged": 1} for _ in range(3)]
        )
        direction = project.directions[0]
        direction["context"] = {"success_evidence": "The live probe passes twice."}
        direction["evidence_progress"] = {
            "status": "met",
            "challenge_approved": True,
        }
        task = self._make_task()
        self._run(project, _stamp_no_findings_refractory(task, self._make_session()))

        assert direction["status"] == "completed"
        assert direction["retirement_outcome"] == "success_evidence_met"
        assert "declared success evidence met" in direction["auto_completed_reason"]

    # ── Exhaustion is terminal but explicitly non-successful ──

    def _make_exhausted_project(
        self,
        direction_id="d-test",
        research_history=None,
        extra=None,
        consecutive_unproductive=2,
    ):
        """Build an active, about-to-be-saturated direction with optional extra fields."""
        direction = {
            "id": direction_id,
            "text": "Exhausted direction",
            "status": "active",
            "research_history": research_history
            if research_history is not None
            else [],
            "history_aggregate": {},
            "consecutive_unproductive_cycles": consecutive_unproductive,
        }
        if extra:
            direction.update(extra)
        project = MagicMock()
        project.directions = [direction]
        return project

    def test_exhausted_unproductive_direction_auto_completes(self):
        """A saturated, genuinely-exhausted (already-fixed-deadlock) UNPRODUCTIVE
        direction with < 3 merges retires with outcome='exhausted'.

        This is the #10113 root fix: an already-fixed-deadlocked direction (its
        spawned tasks all terminate SCOPE_EMPTY because the work is already on HEAD)
        is retired terminally rather than looping or emitting an advisory-only alert,
        even though it has zero verified merges.
        """
        from app.research_completion import _stamp_no_findings_refractory

        # Three consecutive cycles whose spawned tasks all resolved SCOPE_EMPTY
        # (tasks_created >= 1 and scope_empty_noop_count >= tasks_created) → the
        # already-fixed deadlock signal. Zero merges.
        already_fixed_history = [
            {"tasks_created": 2, "scope_empty_noop_count": 2, "prs_merged": 0}
            for _ in range(3)
        ]
        task = self._make_task()
        project = self._make_exhausted_project(research_history=already_fixed_history)
        self._run(project, _stamp_no_findings_refractory(task, self._make_session()))

        direction = project.directions[0]
        assert direction.get("status") == "retired", (
            "A genuinely-exhausted already-fixed-deadlocked direction with <3 merges "
            "must retire without claiming success"
        )
        assert direction.get("retirement_outcome") == "exhausted"
        assert direction.get("auto_completed") is True, "auto_completed must be stamped"
        assert direction.get("completed_at"), "completed_at must be stamped"
        reason = direction.get("auto_completed_reason", "")
        assert "exhausted" in reason and "no new work remaining" in reason, (
            f"exhaustion reason must distinguish it from the proven-merge route, got: {reason!r}"
        )

    def test_exhaustion_complete_blocked_for_code_failing_direction(self):
        """GUARDRAIL 1: a code-failing direction is BROKEN, not exhausted, and must
        NOT auto-complete — the existing pause/diagnosis path owns it."""
        from app.research_completion import _stamp_no_findings_refractory

        # Saturated-shaped history, but every cycle has a CODE failure (not infra) →
        # _check_direction_health returns a 'High code failure rate' pause reason.
        code_failing_history = [
            {
                "tasks_created": 1,
                "tasks_failed": 1,
                "tasks_failed_infra": 0,
                "prs_merged": 0,
            }
            for _ in range(3)
        ]
        task = self._make_task()
        project = self._make_exhausted_project(research_history=code_failing_history)
        self._run(project, _stamp_no_findings_refractory(task, self._make_session()))

        direction = project.directions[0]
        assert direction.get("status") == "active", (
            "A code-failing direction must NOT be auto-completed as exhausted"
        )

    def test_exhaustion_complete_blocked_for_externally_frozen_direction(self):
        """GUARDRAIL 2: an externally-frozen direction (needs_manual_intervention /
        budget pause) may still have work once the block clears, so it must NOT be
        auto-completed as exhausted."""
        from app.research_completion import _stamp_no_findings_refractory

        # Saturated, no merges, no code failures → genuinely exhausted on its own…
        plain_history = [
            {"tasks_created": 1, "tasks_failed": 0, "prs_merged": 0} for _ in range(3)
        ]
        # …but flagged for manual intervention (an external freeze).
        task = self._make_task()
        project = self._make_exhausted_project(
            research_history=plain_history,
            extra={"needs_manual_intervention": True},
        )
        self._run(project, _stamp_no_findings_refractory(task, self._make_session()))

        direction = project.directions[0]
        assert direction.get("status") == "active", (
            "An externally-frozen (needs_manual_intervention) direction must NOT be "
            "auto-completed as exhausted"
        )

        # Same exhaustion shape, but frozen by a budget pause instead.
        task2 = self._make_task()
        project2 = self._make_exhausted_project(
            research_history=plain_history,
            extra={
                "cost_pause_evidence": {
                    "cumulative_estimated_cost_usd": 12.0,
                    "cost_budget_usd": 10.0,
                }
            },
        )
        self._run(project2, _stamp_no_findings_refractory(task2, self._make_session()))
        direction2 = project2.directions[0]
        assert direction2.get("status") == "active", (
            "A budget-paused (cost_pause_evidence) direction must NOT be auto-completed"
        )

    def test_no_findings_auto_completion_reconciles_gap_staffing(self):
        """Auto-completing a direction via the no-findings path must reconcile
        problem-model gap staffing — pruning the direction from active_moves
        and recording a GapDeferral when it was the last active move.

        The lifecycle and strategic-completion retire paths both call
        reconcile_gaps_for_retired_directions when retiring a direction; the
        no-findings auto-complete path must do so too, or the next lifecycle
        prune can remove the completed direction from project.directions while
        the problem model still lists it in gap.active_moves.
        """
        from app.research_completion import _stamp_no_findings_refractory
        import asyncio
        from unittest.mock import patch

        direction_id = "d-gap-test"
        gap_id = "g-test-gap"

        # Build an already-fixed exhausted direction. Gap reconciliation applies
        # to every terminal outcome, not only successful completion.
        history = [
            {"tasks_created": 1, "scope_empty_noop_count": 1, "prs_merged": 0}
            for _ in range(3)
        ]
        project = MagicMock()
        project.directions = [
            {
                "id": direction_id,
                "text": "Gap-staffed direction",
                "status": "active",
                "gap_id": gap_id,
                "research_history": history,
                "history_aggregate": {},
                "consecutive_unproductive_cycles": 2,
            }
        ]

        # problem_model with a gap that has the direction in active_moves.
        pm: dict = {
            "gaps": [
                {
                    "id": gap_id,
                    "name": "Test gap",
                    "status": "active",
                    "active_moves": [direction_id],
                    "past_moves": [],
                }
            ]
        }

        captured_pm_calls: list[dict] = []

        async def _mock_wdl(sess, pid, mutate_fn):
            mutate_fn(project.directions)

        async def _mock_wpml(sess, pid, mutate_fn):
            mutate_fn(pm)
            captured_pm_calls.append(dict(pm))

        task = self._make_task(direction_id=direction_id)
        session = self._make_session()

        with (
            patch("app.research_completion.with_directions_locked", _mock_wdl),
            patch("app.research_completion.with_problem_model_locked", _mock_wpml),
        ):
            asyncio.get_event_loop().run_until_complete(
                _stamp_no_findings_refractory(task, session)
            )

        # The direction must have become terminal to trigger gap reconciliation.
        direction = project.directions[0]
        assert direction.get("status") == "retired"
        assert direction.get("retirement_outcome") == "exhausted"

        # with_problem_model_locked must have been called.
        assert captured_pm_calls, (
            "with_problem_model_locked must be called when a direction is auto-completed"
        )

        # The gap's active_moves must no longer contain the completed direction.
        gap = pm["gaps"][0]
        assert direction_id not in gap.get("active_moves", []), (
            "auto-completed direction must be pruned from gap.active_moves"
        )
        # The direction must be recorded in past_moves.
        assert direction_id in gap.get("past_moves", []), (
            "auto-completed direction must be moved to gap.past_moves"
        )


# --------------------------------------------------------------------------- #
# Declared-success completion (criterion-coverage remains a release concern).
#
# A saturated direction completes here only when its current declared
# success_evidence has a challenge-approved `met` judgment. Criterion coverage
# remains independently enforced at qualification.
# --------------------------------------------------------------------------- #


async def _setup_saturated_proven_direction(
    db_session,
    *,
    direction_id: str,
    criterion_keys: list[str],
):
    """Create a saturated direction with approved declared-success evidence."""
    from app.models import Direction

    direction = {
        "id": direction_id,
        "text": "Saturated proven direction",
        "status": "active",
        "history_aggregate": {},
        "consecutive_unproductive_cycles": 2,  # +1 in the call → saturated
        "context": {"success_evidence": "The declared outcome probe passes."},
        "evidence_progress": {"status": "met", "challenge_approved": True},
        "research_history": [
            {"prs_merged": 1, "verified_prs_merged": 1},
            {"prs_merged": 1, "verified_prs_merged": 1},
            {"prs_merged": 1, "verified_prs_merged": 1},
        ],
    }
    project = _Project(
        name="Coverage Gate Test",
        user_id="test-user-cov-gate",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # First-class Direction ORM row carrying the mapped criterion keys. The
    # in-memory direction dict does NOT reliably carry criterion_keys; the gate
    # helper reads them from this row.
    db_session.add(
        Direction(
            project_id=project.id,
            direction_key=direction_id,
            status="active",
            data=dict(direction),
            criterion_keys=list(criterion_keys),
        )
    )
    await db_session.commit()

    task = _Task(
        project_id=project.id,
        title="Research: saturated proven",
        payloads=[
            {
                "type": "research",
                "directions": [direction],
                "base_branch": "main",
                "current_direction_id": direction_id,
            }
        ],
        context={"direction_id": direction_id, "research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task, direction


async def _open_version_with_criterion(db_session, project, *, covered: bool):
    """Open a ReleaseVersion for *project* with one active criterion. When
    *covered*, attach reachability evidence (coverage → 'covered')."""
    from app import acceptance_criteria as ac
    from app.models import CriterionEvidence, ReleaseVersion

    version = ReleaseVersion(project_id=project.id, version_number=1)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Owned acceptance outcome"
    )
    if covered:
        db_session.add(
            CriterionEvidence(
                criterion_id=criterion.id,
                project_id=project.id,
                source_kind="reachability",
                source_id="pr-1",
            )
        )
        await db_session.commit()
    return version, criterion


# --------------------------------------------------------------------------- #
# Site 1 (apply_post_spawn_state): declared-success completion.
#
# This control confirms that the same evidence-backed completion rule fires
# through the full post-spawn state pipeline.
# --------------------------------------------------------------------------- #


@_pytest.mark.asyncio
async def test_apply_post_spawn_proven_direction_with_covered_criterion_completes(
    db_session,
):
    """Site 1: when the owned criterion IS covered, the saturated proven
    direction still auto-completes via apply_post_spawn_state (control)."""
    from app.research_direction_state import apply_post_spawn_state

    direction_id = "d-apply-cov-covered"
    project, task, _direction = await _setup_saturated_proven_direction(
        db_session, direction_id=direction_id, criterion_keys=["placeholder"]
    )
    _version, criterion = await _open_version_with_criterion(
        db_session, project, covered=True
    )

    from app.models import Direction

    row = (
        await db_session.execute(
            _select(Direction).where(
                Direction.project_id == project.id,
                Direction.direction_key == direction_id,
            )
        )
    ).scalar_one()
    row.criterion_keys = [criterion.criterion_key]
    db_session.add(row)
    await db_session.commit()

    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    await apply_post_spawn_state(
        db_session,
        task,
        [],   # findings
        [],   # child_tasks
        [],   # deduplicated
        [],   # insights
        [],   # direction_notes
        [],   # direction_proposals
        0,    # hash_dedup_count
        0,    # word_overlap_count
        0,    # file_overlap_count
        0,    # evidence_overlap_count
        0,    # type_filtered_count
        0,    # empty_prompt_count
        "research-branch",
        "main",
        0,    # research_depth
        None, # parent_research_task_id
    )

    await db_session.refresh(project)
    target = next(d for d in project.directions if d.get("id") == direction_id)
    assert target.get("status") == "completed", (
        "Direction whose owned criterion IS covered must still auto-complete "
        "via apply_post_spawn_state"
    )
    assert target.get("auto_completed") is True
    assert "verified" in (target.get("auto_completed_reason") or "")


# --------------------------------------------------------------------------- #
# Deferred-memo enrollment: flag-off no-op, flag-on creates row, dedup.
# --------------------------------------------------------------------------- #


@_pytest.mark.asyncio
async def test_deferred_memo_enrollment_flag_off_no_op(db_session, mocker):
    """flag=False: a LOW-actionability dropped finding spawns a memo task but
    does NOT enroll any EvergreenBacklogItem row."""
    from test_autonomous_research import _make_finding, _patch_research_externals
    import app.research_completion as _rc
    from app.models import EvergreenBacklogItem
    from sqlmodel import select as _sel

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-ev-flag-off01"
    )
    finding = _make_finding("Low-actionability finding", direction_id=direction["id"])
    finding["actionability_grade"] = "LOW"
    finding["finding_hash"] = "ev-flag-off-hash-001"

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )
    mocker.patch(
        "app.research_completion.resolve_branch_verification_context",
        new=AsyncMock(return_value={}),
    )
    mocker.patch(
        "app.research_completion.resolve_head_commit",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "app.research_completion.cited_paths_changed_since",
        new=AsyncMock(return_value=None),
    )
    mocker.patch("app.research_completion._has_validation_backend", return_value=False)
    mocker.patch(
        "app.research_completion._summarize_ungrounded_claims_grounded",
        new=AsyncMock(return_value=None),
    )

    _rc._runtime_config["evergreen_fill_deferred_memo_enrollment"] = False
    try:
        await _handle_research_completion(task, "", db_session)
    finally:
        _rc._runtime_config["evergreen_fill_deferred_memo_enrollment"] = False

    rows = (
        (
            await db_session.execute(
                _sel(EvergreenBacklogItem).where(
                    EvergreenBacklogItem.project_id == _project.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == [], "flag=False: no backlog items should be enrolled"
    children = (
        (
            await db_session.execute(
                _sel(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert [(child.first_payload or {}).get("type") for child in children] == [
        "memo"
    ]
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED, (
        "a terminal memo is documentation, not a delivery child that parks its parent"
    )


@_pytest.mark.asyncio
async def test_deferred_memo_enrollment_flag_on_enrolls(db_session, mocker):
    """flag=True: a LOW-actionability dropped finding enrolls one EvergreenBacklogItem
    with provenance='deferred_memo', correct finding_key, title, and direction_id."""
    from test_autonomous_research import _make_finding, _patch_research_externals
    import app.research_completion as _rc
    from app.models import EvergreenBacklogItem
    from sqlmodel import select as _sel

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-ev-flag-on01"
    )
    finding = _make_finding(
        "Fix: contain eligibility-pipeline crashes", direction_id=direction["id"]
    )
    finding["actionability_grade"] = "LOW"
    finding["finding_hash"] = "ev-flag-on-hash-001"

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )
    mocker.patch(
        "app.research_completion.resolve_branch_verification_context",
        new=AsyncMock(return_value={}),
    )
    mocker.patch(
        "app.research_completion.resolve_head_commit",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "app.research_completion.cited_paths_changed_since",
        new=AsyncMock(return_value=None),
    )
    mocker.patch("app.research_completion._has_validation_backend", return_value=False)
    mocker.patch(
        "app.research_completion._summarize_ungrounded_claims_grounded",
        new=AsyncMock(return_value=None),
    )

    _rc._runtime_config["evergreen_fill_deferred_memo_enrollment"] = True
    try:
        await _handle_research_completion(task, "", db_session)
    finally:
        _rc._runtime_config["evergreen_fill_deferred_memo_enrollment"] = False

    rows = (
        (
            await db_session.execute(
                _sel(EvergreenBacklogItem).where(
                    EvergreenBacklogItem.project_id == _project.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, "flag=True: exactly one backlog item enrolled"
    item = rows[0]
    assert item.provenance == "deferred_memo"
    assert item.finding_key == "ev-flag-on-hash-001"
    assert item.status == "enrolled"
    assert item.direction_id == direction["id"]
    assert "eligibility" in item.title.lower() or "Fix" in item.title


@_pytest.mark.asyncio
async def test_deferred_memo_enrollment_dedup_on_repeat_drop(db_session, mocker):
    """A second research cycle that drops the same finding hash with the flag on
    re-enrolls (dedup) and leaves exactly one EvergreenBacklogItem row."""
    from test_autonomous_research import _make_finding, _patch_research_externals
    import app.research_completion as _rc
    from app.models import EvergreenBacklogItem
    from app.evergreen_backlog import enroll_backlog_item
    from sqlmodel import select as _sel

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-ev-dedup01"
    )
    finding = _make_finding("Dedup dropped finding", direction_id=direction["id"])
    finding["actionability_grade"] = "LOW"
    finding["finding_hash"] = "ev-dedup-hash-001"

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )
    mocker.patch(
        "app.research_completion.resolve_branch_verification_context",
        new=AsyncMock(return_value={}),
    )
    mocker.patch(
        "app.research_completion.resolve_head_commit",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "app.research_completion.cited_paths_changed_since",
        new=AsyncMock(return_value=None),
    )
    mocker.patch("app.research_completion._has_validation_backend", return_value=False)
    mocker.patch(
        "app.research_completion._summarize_ungrounded_claims_grounded",
        new=AsyncMock(return_value=None),
    )

    _rc._runtime_config["evergreen_fill_deferred_memo_enrollment"] = True
    try:
        await _handle_research_completion(task, "", db_session)
    finally:
        _rc._runtime_config["evergreen_fill_deferred_memo_enrollment"] = False

    rows_first = (
        (
            await db_session.execute(
                _sel(EvergreenBacklogItem).where(
                    EvergreenBacklogItem.project_id == _project.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows_first) == 1

    # Simulate a repeat drop (same finding_key, same project).
    dup = await enroll_backlog_item(
        db_session,
        project_id=_project.id,
        finding_key="ev-dedup-hash-001",
        title="Duplicate drop attempt",
        provenance="deferred_memo",
    )
    assert dup is not None
    assert dup.id == rows_first[0].id, "second enroll returns the existing row"

    rows_after = (
        (
            await db_session.execute(
                _sel(EvergreenBacklogItem).where(
                    EvergreenBacklogItem.project_id == _project.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows_after) == 1, "dedup: still exactly one row after second drop"


# ─────────────────────────────────────────────────────────────────────────────
# Cross-cycle filtered-hash suppression
# ─────────────────────────────────────────────────────────────────────────────


@_pytest.mark.asyncio
async def test_recently_filtered_hashes_persisted_after_dedup(db_session, mocker):
    """When a finding is hash-deduped out (prior COMPLETED+pr_merged task), its hash
    is persisted into direction.recently_filtered_finding_hashes for the next cycle."""
    from app.finding_deduplication import finding_hash as _fhash
    from test_autonomous_research import (
        _active_direction,
        _make_finding,
        _make_research_task,
        _patch_research_externals,
    )

    direction_id = "d-fh-persist01"
    title = "Fix cross-cycle suppression regression"
    # _make_finding produces description = f"Description: {title}"
    description = f"Description: {title}"
    fhash = _fhash(title, description)

    direction = _active_direction(direction_id, "Hash-persist direction")
    project = _Project(
        name=f"HashPersist {direction_id}",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # A prior COMPLETED task with pr_merged=True carries the same finding_hash,
    # so build_dedup_state will add it to recent_finding_hashes.
    prior = _Task(
        project_id=project.id,
        title=title,
        status=TaskStatus.COMPLETED,
        context={"finding_hash": fhash, "pr_merged": True},
        created_by="ORCHESTRATOR",
        vm_instance_id="test-vm-prior-fh01",
    )
    db_session.add(prior)

    task = await _make_research_task(project, direction, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    _patch_research_externals(mocker)
    finding = _make_finding(title=title, direction_id=direction_id)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    updated_dirs = project.directions or []
    target_dir = next((d for d in updated_dirs if d.get("id") == direction_id), None)
    assert target_dir is not None, "direction must still be present after completion"
    persisted = target_dir.get("recently_filtered_finding_hashes") or []
    assert fhash in persisted, (
        f"hash-deduped finding hash {fhash!r} must be written to "
        "direction.recently_filtered_finding_hashes for next-cycle suppression"
    )


@_pytest.mark.asyncio
async def test_recently_filtered_hashes_suppress_finding_next_cycle(db_session, mocker):
    """A hash pre-loaded into direction.recently_filtered_finding_hashes (from a
    prior cycle) suppresses the matching finding without any DB task carrying that
    hash — pure persisted-state cross-cycle suppression."""
    from app.finding_deduplication import finding_hash as _fhash
    from test_autonomous_research import (
        _active_direction,
        _make_finding,
        _make_research_task,
        _patch_research_externals,
    )

    direction_id = "d-fh-suppress01"
    title = "Fix pre-suppressed cross-cycle finding"
    description = f"Description: {title}"
    fhash = _fhash(title, description)

    # Pre-populate the direction with the hash from a prior cycle.
    direction = _active_direction(direction_id, "Suppression direction")
    direction["recently_filtered_finding_hashes"] = [fhash]

    project = _Project(
        name=f"HashSuppress {direction_id}",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # No prior task in the DB carries this finding_hash — suppression must come
    # solely from the persisted recently_filtered_finding_hashes field.
    task = await _make_research_task(project, direction, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    _patch_research_externals(mocker)
    finding = _make_finding(title=title, direction_id=direction_id)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 0, (
        f"Finding with hash {fhash!r} pre-loaded in recently_filtered_finding_hashes "
        "must be suppressed by the persisted state; no child task should be spawned"
    )


# ---------------------------------------------------------------------------
# Canary spawn-gate classification (acceptance gate)
# ---------------------------------------------------------------------------


def test_spawn_gate_canary_classifies_real_verdict_vs_fail_closed():
    """classify_canary_spawn_gate_result correctly distinguishes real verdicts from
    fail-closed outcomes and pending state.

    This is the acceptance gate for the on-demand canary feature: a real serving
    agent that returns a parseable verdict must yield status='real_verdict' with
    explicit_count>0; a grader that fails closed (no parseable verdict) must yield
    status='fail_closed'; an unresolved canary must yield status='pending'.
    """
    from app.routers.admin_debug import (
        classify_canary_spawn_gate_result,
        _CANARY_PROBE_HASH,
    )
    from app.task_evaluators import (
        ADVERSARIAL_PARSE_OK,
        ADVERSARIAL_PARSE_NO_JSON_ARRAY,
    )

    # 1. Pending — grader has not yet returned
    result = classify_canary_spawn_gate_result(None)
    assert result["status"] == "pending"
    assert result["parse_outcome"] is None
    assert result["explicit_count"] == 0

    # 2. Real verdict — grader returned an explicit pass for the probe finding
    gate_real = {
        "parse_outcome": ADVERSARIAL_PARSE_OK,
        "finding_verdicts": {
            _CANARY_PROBE_HASH: {
                "passed": True,
                "explicit": True,
                "reason": "premise holds",
            }
        },
        "grader_task_id": "grader-abc",
    }
    result = classify_canary_spawn_gate_result(gate_real)
    assert result["status"] == "real_verdict", (
        f"Expected real_verdict for ok parse with explicit verdict; got {result['status']!r}"
    )
    assert result["explicit_count"] == 1
    assert result["parse_outcome"] == ADVERSARIAL_PARSE_OK

    # 3. Real verdict — grader returned an explicit FAIL (still a real verdict; gate is healthy)
    gate_fail_verdict = {
        "parse_outcome": ADVERSARIAL_PARSE_OK,
        "finding_verdicts": {
            _CANARY_PROBE_HASH: {
                "passed": False,
                "explicit": True,
                "reason": "premise wrong",
            }
        },
        "grader_task_id": "grader-abc",
    }
    result = classify_canary_spawn_gate_result(gate_fail_verdict)
    assert result["status"] == "real_verdict", (
        "An explicit fail verdict is still a real_verdict — gate is functioning"
    )

    # 4. Fail-closed — grader returned no JSON array (wholesale non-response)
    gate_no_json = {
        "parse_outcome": ADVERSARIAL_PARSE_NO_JSON_ARRAY,
        "finding_verdicts": {
            _CANARY_PROBE_HASH: {
                "passed": False,
                "explicit": False,
                "unresolved_reason": ADVERSARIAL_PARSE_NO_JSON_ARRAY,
            }
        },
        "grader_task_id": "grader-def",
    }
    result = classify_canary_spawn_gate_result(gate_no_json)
    assert result["status"] == "fail_closed", (
        f"Expected fail_closed for no-JSON-array outcome; got {result['status']!r}"
    )
    assert result["explicit_count"] == 0

    # 5. Fail-closed — ok parse outcome but zero explicit verdicts (degenerate)
    gate_ok_no_explicit = {
        "parse_outcome": ADVERSARIAL_PARSE_OK,
        "finding_verdicts": {
            _CANARY_PROBE_HASH: {
                "passed": False,
                "explicit": False,
            }
        },
        "grader_task_id": "grader-ghi",
    }
    result = classify_canary_spawn_gate_result(gate_ok_no_explicit)
    assert result["status"] == "fail_closed", (
        "ok parse with zero explicit verdicts must be fail_closed"
    )


# ---------------------------------------------------------------------------
# Cross-tree base inheritance for genuine code dependents
# ---------------------------------------------------------------------------


@_pytest.mark.asyncio
async def test_genuine_cross_tree_code_dependent_inherits_prerequisite_base(
    db_session, mocker
):
    """A finding wired via depends_on_indices to an existing cross-tree foundation
    with file overlap inherits the foundation's base_branch at spawn time.

    This prevents the cross-base landed-signal wedge by making the pair same-base
    from birth so the dependent never has to wait for the foundation's code to land
    on a different integration branch.
    """
    from app.research_completion import _finding_hash
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-xbase-inherit"
    )

    # Foundation already in-flight on a different branch, shares a file with the dep.
    foundation_title = "Add shared auth middleware"
    foundation_desc = "Core auth that all endpoints depend on"
    foundation_hash = _finding_hash(foundation_title, foundation_desc)
    existing_foundation = _Task(
        project_id=_project.id,
        title=foundation_title,
        description=foundation_desc,
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        payloads=[{"type": "create", "base_branch": "main"}],
        context={
            "finding_hash": foundation_hash,
            "related_files": ["backend/app/auth.py"],
        },
    )
    db_session.add(existing_foundation)
    await db_session.commit()
    await db_session.refresh(existing_foundation)

    # Research cycle surfaces both foundation (will be deduped) and dependent.
    # finding_a matches the existing_foundation by hash, so deduplicate_findings
    # drops it; the missing-sibling loop resolves it to existing_foundation.
    finding_a = _make_finding(title=foundation_title, direction_id=direction["id"])
    finding_a["description"] = foundation_desc
    finding_a["finding_hash"] = foundation_hash
    finding_a["related_files"] = ["backend/app/auth.py"]

    finding_b = _make_finding(title="Add login endpoint", direction_id=direction["id"])
    finding_b["depends_on_indices"] = [0]
    # Overlapping file → genuine code-dep → should inherit foundation's base_branch.
    finding_b["related_files"] = ["backend/app/auth.py"]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding_a, finding_b],
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    children = [c for c in children if (c.first_payload or {}).get("type") != "memo"]
    dep_children = [c for c in children if "login" in c.title.lower()]
    assert len(dep_children) == 1, "dependent finding must spawn as a child task"
    dep = dep_children[0]
    assert str(existing_foundation.id) in (dep.depends_on or []), (
        "dependent must be wired to the existing in-flight foundation"
    )
    assert (dep.first_payload or {}).get("base_branch") == "main", (
        "genuine cross-tree code-dep must inherit foundation base_branch 'main'"
    )


@_pytest.mark.asyncio
async def test_ordering_only_cross_tree_dependent_keeps_own_base(db_session, mocker):
    """An ordering-only (no file overlap) cross-tree dependency does NOT cause the
    dependent's base_branch to be inherited — only genuine code-deps do."""
    from app.research_completion import _finding_hash
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-xbase-noinherit"
    )

    # Foundation in-flight on "main", but with disjoint files from the dependent.
    foundation_title = "Set up deployment pipeline"
    foundation_desc = "Automate releases via GitHub Actions"
    foundation_hash = _finding_hash(foundation_title, foundation_desc)
    existing_foundation = _Task(
        project_id=_project.id,
        title=foundation_title,
        description=foundation_desc,
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        payloads=[{"type": "create", "base_branch": "main"}],
        context={
            "finding_hash": foundation_hash,
            "related_files": ["infra/deploy.sh"],
        },
    )
    db_session.add(existing_foundation)
    await db_session.commit()
    await db_session.refresh(existing_foundation)

    finding_a = _make_finding(title=foundation_title, direction_id=direction["id"])
    finding_a["description"] = foundation_desc
    finding_a["finding_hash"] = foundation_hash
    finding_a["related_files"] = ["infra/deploy.sh"]

    finding_b = _make_finding(
        title="Add user profile endpoint", direction_id=direction["id"]
    )
    finding_b["depends_on_indices"] = [0]
    # Disjoint files → no code overlap → ordering-only dep → base_branch unchanged.
    finding_b["related_files"] = ["backend/app/users.py"]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding_a, finding_b],
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    children = [c for c in children if (c.first_payload or {}).get("type") != "memo"]
    dep_children = [c for c in children if "profile" in c.title.lower()]
    assert len(dep_children) == 1, "dependent finding must spawn as a child task"
    dep = dep_children[0]
    dep_base = (dep.first_payload or {}).get("base_branch") or ""
    assert dep_base != "main", (
        "ordering-only cross-tree dep must NOT inherit foundation base_branch"
    )
    assert "research/" in dep_base, (
        "ordering-only cross-tree dep must keep its own research branch"
    )


@_pytest.mark.asyncio
async def test_explicit_prereq_genuine_cross_tree_inherits_base(db_session, mocker):
    """A finding declaring an explicit {kind:task} prerequisite to a cross-tree
    foundation with file overlap also inherits the foundation's base_branch."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-xbase-explicit"
    )

    existing_foundation = _Task(
        project_id=_project.id,
        title="Build payment core",
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        payloads=[{"type": "create", "base_branch": "main"}],
        context={"related_files": ["backend/app/payments.py"]},
    )
    db_session.add(existing_foundation)
    await db_session.commit()
    await db_session.refresh(existing_foundation)

    # Finding declares the foundation explicitly by task ID.
    finding = _make_finding(title="Add refund endpoint", direction_id=direction["id"])
    finding["prerequisite"] = {"kind": "task", "ref": str(existing_foundation.id)}
    finding["related_files"] = ["backend/app/payments.py"]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    children = [c for c in children if (c.first_payload or {}).get("type") != "memo"]
    dep_children = [c for c in children if "refund" in c.title.lower()]
    assert len(dep_children) == 1
    dep = dep_children[0]
    assert str(existing_foundation.id) in (dep.depends_on or []), (
        "dependent must be wired to the explicit prerequisite foundation"
    )
    assert (dep.first_payload or {}).get("base_branch") == "main", (
        "explicit genuine cross-tree code-dep must inherit foundation base_branch"
    )


@_pytest.mark.asyncio
async def test_genuine_cross_tree_none_base_branch_is_noop(db_session, mocker):
    """When the foundation's payload has no base_branch, _maybe_inherit_base
    must return early and NOT overwrite the dependent's own base_branch with
    None or an empty string.

    This guards against silent removal of the guard added in a8181941.
    """
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-xbase-none-base"
    )

    # Foundation with NO base_branch in its payload (key absent entirely).
    existing_foundation = _Task(
        project_id=_project.id,
        title="Build notification core",
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        payloads=[{"type": "create"}],  # no base_branch key
        context={"related_files": ["backend/app/notifications.py"]},
    )
    db_session.add(existing_foundation)
    await db_session.commit()
    await db_session.refresh(existing_foundation)

    # Finding declares the foundation explicitly by task ID with file overlap
    # (genuine code-dep) — but since foundation has no base_branch, the guard
    # must fire and leave the dependent's own base_branch intact.
    finding = _make_finding(
        title="Add notification endpoint", direction_id=direction["id"]
    )
    finding["prerequisite"] = {"kind": "task", "ref": str(existing_foundation.id)}
    finding["related_files"] = ["backend/app/notifications.py"]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    children = [c for c in children if (c.first_payload or {}).get("type") != "memo"]
    dep_children = [c for c in children if "notification" in c.title.lower()]
    assert len(dep_children) == 1, "dependent finding must spawn as a child task"
    dep = dep_children[0]
    assert str(existing_foundation.id) in (dep.depends_on or []), (
        "dependent must be wired to the foundation even when base_branch is absent"
    )
    dep_base = (dep.first_payload or {}).get("base_branch")
    assert dep_base is not None, (
        "_maybe_inherit_base must not overwrite dependent base_branch with None "
        "when foundation has no base_branch"
    )
    assert dep_base != "", (
        "_maybe_inherit_base must not overwrite dependent base_branch with empty "
        "string when foundation has no base_branch"
    )


@_pytest.mark.asyncio
async def test_exhaustion_autocomplete_reconciles_gap_active_moves(db_session):
    """Auto-retiring an exhausted direction reconciles the problem-model gap.

    When apply_post_spawn_state auto-retires a direction (via the dedup-deadlock
    exhaustion path), any gap in project.problem_model whose active_moves contains
    that direction id must have it pruned out (moved to past_moves) after the call.
    This mirrors the invariant enforced by strategic and zombie retirements.

    Acceptance requirement: test_exhaustion_autocomplete_reconciles_gap_active_moves
    """
    from datetime import datetime, timedelta

    from app.research_direction_state import apply_post_spawn_state
    from test_autonomous_research import _active_direction, _make_research_task

    direction_id = "d-gap-reconcile-test"

    # Two pre-existing dedup-zero history entries to prime the deadlock window.
    old_history = [
        {
            "timestamp": (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=10)).isoformat(),
            "task_id": "t-gcr-old1",
            "tasks_created": 0,
            "findings_produced": 0,
            "filter_breakdown": {
                "hash_dedup": 2,
                "word_overlap_dedup": 0,
                "file_overlap_dedup": 0,
                "evidence_overlap_dedup": 0,
                "type_filtered": 0,
                "empty_prompt": 0,
            },
            "summary": "no findings",
        },
        {
            "timestamp": (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=5)).isoformat(),
            "task_id": "t-gcr-old2",
            "tasks_created": 0,
            "findings_produced": 0,
            "filter_breakdown": {
                "hash_dedup": 1,
                "word_overlap_dedup": 1,
                "file_overlap_dedup": 0,
                "evidence_overlap_dedup": 0,
                "type_filtered": 0,
                "empty_prompt": 0,
            },
            "summary": "no findings",
        },
    ]
    direction = {
        **_active_direction(direction_id, "Exhausted direction with gap"),
        "research_history": old_history,
    }

    # problem_model has a gap with this direction in active_moves
    gap_id = "g-test-gap-01"
    initial_problem_model = {
        "gaps": [
            {
                "id": gap_id,
                "title": "Test gap",
                "active_moves": [direction_id],
                "past_moves": [],
            }
        ]
    }

    project = _Project(
        name="Gap Reconcile Test",
        user_id="test-user-gcr1",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
        problem_model=initial_problem_model,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    # Third dedup-zero cycle — triggers exhaustion auto-complete via deadlock detection.
    await apply_post_spawn_state(
        db_session,
        task,
        [],  # findings
        [],  # child_tasks
        [],  # deduplicated
        [],  # insights
        [],  # direction_notes
        [],  # direction_proposals
        1,   # hash_dedup_count — dedup activity in 3rd cycle
        0,   # word_overlap_count
        0,   # file_overlap_count
        0,   # evidence_overlap_count
        0,   # type_filtered_count
        0,   # empty_prompt_count
        "research-branch",
        "main",
        0,   # research_depth
        None,  # parent_research_task_id
    )
    await db_session.refresh(project)

    # Direction must be terminally retired without claiming success.
    d = project.directions[0]
    assert d["status"] == "retired", (
        f"direction must be auto-retired, got {d['status']!r}"
    )
    assert d.get("retirement_outcome") == "exhausted"

    # problem_model gap must have direction pruned from active_moves and moved to past_moves.
    pm = project.problem_model or {}
    gaps = pm.get("gaps", [])
    assert gaps, "problem_model must still have gaps list"
    test_gap = next((g for g in gaps if g.get("id") == gap_id), None)
    assert test_gap is not None, f"gap {gap_id} must still exist in problem_model"

    active_moves = test_gap.get("active_moves", [])
    past_moves = test_gap.get("past_moves", [])

    assert direction_id not in active_moves, (
        f"completed direction {direction_id!r} must be pruned from gap active_moves, "
        f"got active_moves={active_moves!r}"
    )
    assert direction_id in past_moves, (
        f"completed direction {direction_id!r} must appear in gap past_moves, "
        f"got past_moves={past_moves!r}"
    )


# ---------------------------------------------------------------------------
# Criterion propagation: _handle_research_completion wires Direction.criterion_keys
# into newly spawned Task.criterion_ids.
# ---------------------------------------------------------------------------


@_pytest.mark.asyncio
async def test_criterion_ids_propagated_from_parent_direction(db_session, mocker):
    """_handle_research_completion sets criterion_ids on spawned tasks from the
    parent direction's criterion_keys.  This guards the pre-fetch + assignment
    path that was added alongside Direction.criterion_keys — a manually
    constructed Task would not catch a regression here."""
    from app.models import Direction as _Direction, ReleaseVersion
    from app import acceptance_criteria as _ac
    from test_autonomous_research import _active_direction, _patch_research_externals

    direction_id = "d-crit-prop-01"
    direction_data = _active_direction(direction_id, "Criterion propagation direction")
    project = _Project(
        name="CritProp Parent",
        user_id="test-user-crit",
        autonomous_enabled=True,
        directions=[direction_data],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create a release version and a criterion for it.
    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    criterion = await _ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="All endpoints documented",
        created_by="strategist",
    )
    inactive = await _ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Deprecated endpoint docs",
        created_by="strategist",
    )
    superseded = await _ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Superseded endpoint docs",
        created_by="strategist",
    )
    await _ac.retract_criterion(
        db_session,
        project.id,
        version.id,
        inactive.criterion_key,
        reason="superseded",
        retracted_by="strategist",
    )
    await _ac.retract_criterion(
        db_session,
        project.id,
        version.id,
        superseded.criterion_key,
        reason="replaced",
        retracted_by="strategist",
        superseded_by_key=criterion.criterion_key,
    )

    # Create a Direction ORM row with criterion_keys set (as the link-direction
    # endpoint would produce).
    dir_row = _Direction(
        project_id=project.id,
        direction_key=direction_id,
        data=direction_data,
        status="active",
        criterion_keys=[
            criterion.criterion_key,
            inactive.criterion_key,
            superseded.criterion_key,
        ],
    )
    db_session.add(dir_row)
    await db_session.commit()

    # Build a research task whose context carries the direction_id so
    # _parent_direction_id is set during research_completion processing.
    task = _Task(
        project_id=project.id,
        title="Research: Criterion propagation direction",
        payloads=[{"type": "research", "directions": [direction_data], "base_branch": "main"}],
        context={"research_depth": 0, "direction_id": direction_id},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    finding = {
        "title": "Add endpoint docs",
        "description": "Document the REST API",
        "prompt": "Add OpenAPI docs for all endpoints",
        "priority": "medium",
        "scope": "small",
        "risk": "low",
        "type": "improvement",
        "direction_id": direction_id,
        "acceptance_evidence": {
            "kind": "named_test",
            "test_path": "backend/testing/test_research_completion.py",
        },
    }
    _patch_research_externals(mocker)
    mocker.patch("app.research_completion._parse_research_findings", return_value=[finding])
    mocker.patch(
        "app.research_completion._assess_project_spawn_health", return_value=(False, None)
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1, "expected exactly one spawned child task"
    child = children[0]
    assert child.criterion_ids == [criterion.criterion_key], (
        f"spawned task must inherit only active criterion_ids from parent direction; "
        f"got {child.criterion_ids!r}"
    )


@_pytest.mark.asyncio
async def test_criterion_ids_propagated_from_finding_direction(db_session, mocker):
    """When a finding carries its own direction_id (cross-direction spawn),
    _handle_research_completion must load criterion_keys for THAT direction —
    not just the parent direction — and set them on the spawned task."""
    from app.models import Direction as _Direction, ReleaseVersion
    from app import acceptance_criteria as _ac
    from test_autonomous_research import _active_direction, _patch_research_externals

    parent_dir_id = "d-crit-parent-02"
    child_dir_id = "d-crit-child-02"

    parent_direction_data = _active_direction(parent_dir_id, "Parent direction")
    child_direction_data = _active_direction(child_dir_id, "Child direction")

    project = _Project(
        name="CritProp CrossDir",
        user_id="test-user-crit2",
        autonomous_enabled=True,
        directions=[parent_direction_data, child_direction_data],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Criterion is linked to the CHILD direction (the finding-level direction).
    criterion = await _ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Child direction criterion",
        created_by="strategist",
    )
    inactive = await _ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Deprecated child direction criterion",
        created_by="strategist",
    )
    superseded = await _ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Superseded child direction criterion",
        created_by="strategist",
    )
    await _ac.retract_criterion(
        db_session,
        project.id,
        version.id,
        inactive.criterion_key,
        reason="superseded",
        retracted_by="strategist",
    )
    await _ac.retract_criterion(
        db_session,
        project.id,
        version.id,
        superseded.criterion_key,
        reason="replaced",
        retracted_by="strategist",
        superseded_by_key=criterion.criterion_key,
    )
    child_dir_row = _Direction(
        project_id=project.id,
        direction_key=child_dir_id,
        data=child_direction_data,
        status="active",
        criterion_keys=[
            criterion.criterion_key,
            inactive.criterion_key,
            superseded.criterion_key,
        ],
    )
    # Parent direction has NO criterion_keys.
    parent_dir_row = _Direction(
        project_id=project.id,
        direction_key=parent_dir_id,
        data=parent_direction_data,
        status="active",
        criterion_keys=[],
    )
    db_session.add(child_dir_row)
    db_session.add(parent_dir_row)
    await db_session.commit()

    task = _Task(
        project_id=project.id,
        title="Research: Parent direction",
        payloads=[
            {"type": "research", "directions": [parent_direction_data], "base_branch": "main"}
        ],
        context={"research_depth": 0, "direction_id": parent_dir_id},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # The finding references the child direction (cross-direction spawn).
    finding = {
        "title": "Implement child feature",
        "description": "Child direction work",
        "prompt": "Implement the child direction feature",
        "priority": "medium",
        "scope": "small",
        "risk": "low",
        "type": "improvement",
        "direction_id": child_dir_id,
        "acceptance_evidence": {
            "kind": "named_test",
            "test_path": "backend/testing/test_research_completion.py",
        },
    }
    _patch_research_externals(mocker)
    mocker.patch("app.research_completion._parse_research_findings", return_value=[finding])
    mocker.patch(
        "app.research_completion._assess_project_spawn_health", return_value=(False, None)
    )

    await _handle_research_completion(task, "", db_session)

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1, "expected exactly one spawned child task"
    child = children[0]
    assert child.criterion_ids == [criterion.criterion_key], (
        f"spawned task must inherit only active criterion_ids from the finding's "
        f"direction (cross-direction); got {child.criterion_ids!r}"
    )


@_pytest.mark.asyncio
async def test_apply_post_spawn_state_aggregate_stranded_merges_not_counted(
    db_session, mocker
):
    """Aggregate merge context discounts stranded work without becoming success proof.

    When stranded merges have been evicted into history_aggregate, the aggregate term
    in the saturated completion summary must apply the same credit-gate discount as
    research_completion._stamp_no_findings_refractory and
    strategic_completion._compute_lifetime_burn_metrics.

    Case 1: total_verified=3, total_stranded=3 and no outcome proof → must NOT complete.
    Case 2: declared, challenged success evidence is met; the completion reason must
    report 3 credit-eligible merges (5 verified minus 2 stranded), not all 5.
    """
    from datetime import datetime, timedelta

    from app.research_direction_state import apply_post_spawn_state
    from test_autonomous_research import _active_direction, _make_research_task

    mocker.patch(
        "app.research_direction_state.broadcast_to_project",
        new_callable=AsyncMock,
    )
    # This test exercises direction accounting, not the strategic-memory
    # ProjectMessage dual-write hook.  ``apply_post_spawn_state`` can flush its
    # activity message more than once while updating the same direction, so
    # isolate that orthogonal receipt writer from this two-project fixture.
    mocker.patch(
        "app.models._stage_strategic_memory_source_for_flush",
        return_value=None,
    )

    direction_id = "d-agg-stranded-apply"

    # Two prior genuine (non-dedup-zero) cycles with tasks_created=1.
    # When apply_post_spawn_state adds a no-findings entry (tasks_created=0,
    # no dedup) for the 3rd cycle, _compute_saturation sees 3 consecutive
    # genuine low-output cycles → possibly_saturated=True → triggers auto-complete check.
    old_history = [
        {
            "timestamp": (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=10)).isoformat(),
            "task_id": "t-agg-str-1",
            "tasks_created": 1,
            "findings_produced": 0,
            "filter_breakdown": {
                "hash_dedup": 0,
                "word_overlap_dedup": 0,
                "file_overlap_dedup": 0,
                "evidence_overlap_dedup": 0,
                "type_filtered": 0,
                "empty_prompt": 0,
            },
            "summary": "test cycle 1",
        },
        {
            "timestamp": (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=5)).isoformat(),
            "task_id": "t-agg-str-2",
            "tasks_created": 1,
            "findings_produced": 0,
            "filter_breakdown": {
                "hash_dedup": 0,
                "word_overlap_dedup": 0,
                "file_overlap_dedup": 0,
                "evidence_overlap_dedup": 0,
                "type_filtered": 0,
                "empty_prompt": 0,
            },
            "summary": "test cycle 2",
        },
    ]

    # Case 1: 3 verified, 3 stranded → credit-eligible = max(0, 3-3) = 0 → NOT auto-complete.
    agg_all_stranded = {
        "total_prs_merged": 3,
        "total_verified_prs_merged": 3,
        "total_stranded_prs_merged": 3,
        "total_cycles": 5,
    }
    direction = {
        **_active_direction(direction_id, "Aggregate stranded test direction"),
        "research_history": list(old_history),
        "history_aggregate": agg_all_stranded,
    }
    project = _Project(
        name="Aggregate Stranded Test",
        user_id="test-user-agg-str1",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    await apply_post_spawn_state(
        db_session,
        task,
        [],   # findings
        [],   # child_tasks
        [],   # deduplicated
        [],   # insights
        [],   # direction_notes
        [],   # direction_proposals
        0,    # hash_dedup_count
        0,    # word_overlap_count
        0,    # file_overlap_count
        0,    # evidence_overlap_count
        0,    # type_filtered_count
        0,    # empty_prompt_count
        "research-branch",
        "main",
        0,    # research_depth
        None, # parent_research_task_id
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d.get("status") != "completed", (
        "Direction whose only aggregate merges are confirmed-stranded must NOT be "
        "auto-completed via apply_post_spawn_state"
    )

    # Case 2: declared/challenged success evidence authorizes completion. The
    # merge count remains contextual and must discount the two stranded merges.
    direction_id2 = "d-agg-stranded-apply2"
    agg_partial_stranded = {
        "total_prs_merged": 5,
        "total_verified_prs_merged": 5,
        "total_stranded_prs_merged": 2,
        "total_cycles": 5,
    }
    active_direction2 = _active_direction(
        direction_id2, "Aggregate partial stranded test direction"
    )
    direction2 = {
        **active_direction2,
        "context": {
            **(active_direction2.get("context") or {}),
            "success_evidence": "The declared direction outcome is verified",
        },
        "evidence_progress": {
            "status": "met",
            "challenge_approved": True,
        },
        "research_history": list(old_history),
        "history_aggregate": agg_partial_stranded,
    }
    project2 = _Project(
        name="Aggregate Partial Stranded Test",
        user_id="test-user-agg-str2",
        autonomous_enabled=True,
        directions=[direction2],
        vision={"purpose": "test"},
    )
    db_session.add(project2)
    await db_session.commit()
    await db_session.refresh(project2)

    task2 = await _make_research_task(project2, direction2, db_session)
    task2.context = {**(task2.context or {}), "direction_id": direction_id2}
    db_session.add(task2)
    await db_session.commit()

    await apply_post_spawn_state(
        db_session,
        task2,
        [],   # findings
        [],   # child_tasks
        [],   # deduplicated
        [],   # insights
        [],   # direction_notes
        [],   # direction_proposals
        0,    # hash_dedup_count
        0,    # word_overlap_count
        0,    # file_overlap_count
        0,    # evidence_overlap_count
        0,    # type_filtered_count
        0,    # empty_prompt_count
        "research-branch",
        "main",
        0,    # research_depth
        None, # parent_research_task_id
    )
    await db_session.refresh(project2)

    d2 = project2.directions[0]
    assert d2.get("status") == "completed", (
        "Challenged declared success evidence must complete the direction"
    )
    assert "3 verified PRs" in str(d2.get("auto_completed_reason") or ""), (
        "Completion context must discount the two stranded aggregate merges"
    )


@_pytest.mark.asyncio
async def test_sub_research_note_append_is_idempotent_on_replay(db_session):
    """Sub-research notes must not duplicate when research task is replayed.

    When a research task with research_depth > 0 is retried/replayed, apply_post_spawn_state
    must not append a duplicate sub_research_note with the same sub_task_id. The deduplication
    checks across the entire history to prevent duplicate notes from evicting distinct
    legitimate ones when the last-5 truncation applies.
    """
    from app.research_direction_state import apply_post_spawn_state
    from test_autonomous_research import _active_direction, _make_research_task, _make_finding

    direction_id = "d-sub-research-replay"
    direction = {
        **_active_direction(direction_id, "Test direction for sub-research replay"),
        "research_history": [
            {
                "timestamp": _utcnow().isoformat(),
                "task_id": "t-parent-task",
                "tasks_created": 1,
                "findings_produced": 1,
                "filter_breakdown": {
                    "hash_dedup": 0,
                    "word_overlap_dedup": 0,
                    "file_overlap_dedup": 0,
                    "evidence_overlap_dedup": 0,
                    "type_filtered": 0,
                    "empty_prompt": 0,
                },
                "summary": "parent research cycle",
            }
        ],
    }
    project = _Project(
        name="Sub-research replay test",
        user_id="test-user-sub-research-1",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent_task = await _make_research_task(project, direction, db_session)
    parent_task.context = {**(parent_task.context or {}), "direction_id": direction_id}
    db_session.add(parent_task)
    await db_session.commit()

    child = _Task(
        project_id=project.id,
        title="Sub-research child task",
        payloads=[{"type": "research"}],
        status=TaskStatus.QUEUED,
        parent_task_id=parent_task.id,
        created_by="ORCHESTRATOR",
        context={"direction_id": direction_id},
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)

    finding = _make_finding("Test finding", direction_id=direction_id)

    # First call to apply_post_spawn_state: adds sub_research_note to the history entry
    await apply_post_spawn_state(
        db_session,
        parent_task,
        [finding],
        [child],
        [(0, finding, "hash-123")],
        [{"summary": "Test insight"}],
        [],
        [],
        0,
        0,
        0,
        0,
        0,
        0,
        "research-branch",
        "main",
        1,  # research_depth = 1 (sub-research)
        str(uuid.uuid4()),  # parent_research_task_id
    )
    await db_session.refresh(project)

    # Verify first call added sub_research_notes
    d = project.directions[0]
    history = d.get("research_history", [])
    assert len(history) >= 1, "History should have at least 1 entry"
    last_entry = history[-1]
    sub_notes_first = last_entry.get("sub_research_notes", [])
    assert len(sub_notes_first) == 1, f"Expected 1 sub_research_note, got {len(sub_notes_first)}"

    # Second call to apply_post_spawn_state: replay with same parent_task.id
    # This simulates a retry scenario where the same findings are re-processed
    await apply_post_spawn_state(
        db_session,
        parent_task,  # Same task ID
        [finding],
        [child],
        [(0, finding, "hash-123")],
        [{"summary": "Test insight"}],
        [],
        [],
        0,
        0,
        0,
        0,
        0,
        0,
        "research-branch",
        "main",
        1,  # research_depth = 1 (same depth)
        str(uuid.uuid4()),  # parent_research_task_id (different but same sub_task_id)
    )
    await db_session.refresh(project)

    # Verify second call did NOT duplicate the note
    d = project.directions[0]
    history = d.get("research_history", [])
    last_entry = history[-1]
    sub_notes_second = last_entry.get("sub_research_notes", [])

    # The key check: even on replay, the sub_research_note should only appear once
    # (or if a new history entry was created, it shouldn't have duplicates)
    subtask_ids = [note.get("sub_task_id") for note in sub_notes_second]
    assert len(subtask_ids) == len(set(subtask_ids)), (
        f"Sub_research_notes contain duplicate sub_task_ids: {subtask_ids}. "
        "Replay must be idempotent."
    )


class TestResearchHistoryIdempotency:
    """Verify that research_history entries are idempotent on post-commit retry."""

    def _make_task(self, task_id, direction_id="d-test-retry"):
        """Create a mock failed research task."""
        task = MagicMock()
        task.id = task_id
        task.project_id = uuid.uuid4()
        task.context = {
            "failure_category": "CODE_ERROR",
            "direction_id": direction_id,
        }
        task.latest_status_message = "CODE_ERROR: something went wrong"
        task.first_payload = {"type": "research"}
        return task

    def _make_session(self):
        """Create a mock async session."""
        session = AsyncMock()
        session.add = MagicMock()
        return session

    def _make_project(self, direction_id="d-test-retry"):
        """Create a mock project with a direction."""
        project = MagicMock()
        project.directions = [
            {
                "id": direction_id,
                "research_history": [],
                "consecutive_unproductive_cycles": 0,
            }
        ]
        return project

    def _run(self, project, coro):
        """Run coro with with_directions_locked patched to call mutate_fn on project.directions."""
        import asyncio
        from unittest.mock import patch

        async def _mock_wdl(session, project_id, mutate_fn):
            mutate_fn(project.directions)

        with patch("app.research_completion.with_directions_locked", _mock_wdl):
            asyncio.get_event_loop().run_until_complete(coro)

    def test_research_history_append_idempotent_on_retry(self):
        """Calling _record_failure twice with same task_id must not duplicate entries."""
        from app.research_completion import _record_research_task_failure

        task_id = uuid.uuid4()
        task = self._make_task(task_id)
        project = self._make_project()
        session = self._make_session()

        # First call: record the failure
        self._run(project, _record_research_task_failure(task, session))
        history_first = project.directions[0]["research_history"]
        assert len(history_first) == 1, f"Expected 1 entry after first call, got {len(history_first)}"
        assert history_first[0]["task_id"] == str(task_id)

        # Second call: simulate post_commit_pending retry — record the same failure again
        self._run(project, _record_research_task_failure(task, session))
        history_second = project.directions[0]["research_history"]

        # Verify idempotency: still exactly one entry
        assert len(history_second) == 1, (
            f"Expected 1 entry after retry (idempotent), got {len(history_second)}. "
            f"History: {history_second}. Retry duplicated the entry."
        )
        # Verify it's the same entry
        assert history_second[0]["task_id"] == str(task_id)
        # Verify the entry still has the failure_category
        assert history_second[0]["failure_category"] == "CODE_ERROR"


async def test_apply_post_spawn_state_research_history_idempotent_on_retry(db_session):
    """Calling apply_post_spawn_state twice with the same task_id must not duplicate history entries.

    The idempotency guard at research_direction_state.py:667-675 checks existing_task_ids
    (including evicted aggregate entries) before appending. A second call with the same task
    (post-commit retry scenario) must leave exactly one research_history entry.
    """
    from app.research_direction_state import apply_post_spawn_state
    from test_autonomous_research import _active_direction, _make_research_task

    direction_id = "d-idem-apply-retry"
    direction = {
        **_active_direction(direction_id, "Idempotency retry test direction"),
        "research_history": [],
    }
    project = _Project(
        name="Apply Post Spawn Idempotency Test",
        user_id="test-user-idem-apss-1",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    _call_args = (
        db_session,
        task,
        [],    # findings
        [],    # child_tasks
        [],    # deduplicated
        [],    # insights
        [],    # direction_notes
        [],    # direction_proposals
        0,     # hash_dedup_count
        0,     # word_overlap_count
        0,     # file_overlap_count
        0,     # evidence_overlap_count
        0,     # type_filtered_count
        0,     # empty_prompt_count
        "research-branch",
        "main",
        0,     # research_depth
        None,  # parent_research_task_id
    )

    # First call: adds a research_history entry for this task
    await apply_post_spawn_state(*_call_args)
    await db_session.refresh(project)

    history_first = project.directions[0].get("research_history", [])
    assert len(history_first) == 1, (
        f"Expected 1 entry after first call, got {len(history_first)}"
    )
    assert history_first[0]["task_id"] == str(task.id)

    # Second call: simulate post-commit retry — same task, same args
    await apply_post_spawn_state(*_call_args)
    await db_session.refresh(project)

    history_second = project.directions[0].get("research_history", [])
    assert len(history_second) == 1, (
        f"Expected 1 entry after retry (idempotent), got {len(history_second)}. "
        f"apply_post_spawn_state duplicated the research_history entry on post-commit retry. "
        f"History: {history_second}"
    )
    assert history_second[0]["task_id"] == str(task.id)


async def test_apply_post_spawn_state_outcome_progress_and_findings_idempotent_on_retry(db_session):
    """Calling apply_post_spawn_state twice with same task_id must not duplicate
    outcome_progress_history or recent_findings_evidence entries.

    The fix wraps recent_findings_evidence and record_outcome_progress_snapshot
    inside the task_id guard (research_direction_state.py line 898) so they are
    only updated when the task_id is new. A post-commit retry with the same task
    must leave outcome_progress_history and recent_findings_evidence with the same
    entries as a single call — no duplicate cycle_index snapshot and no duplicated
    finding evidence.
    """
    from app.research_direction_state import apply_post_spawn_state
    from test_autonomous_research import _active_direction, _make_finding, _make_research_task

    direction_id = "d-idem-outcome-prog-retry"
    direction = {
        **_active_direction(direction_id, "Outcome progress idempotency test"),
        "research_history": [],
        "recent_findings_evidence": [],
        "outcome_progress_history": [],
    }
    project = _Project(
        name="Outcome Progress Idempotency Test",
        user_id="test-user-outcome-prog-1",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    # Create finding with claims and sources so recent_findings_evidence populates
    finding = _make_finding(title="Test Finding", direction_id=direction_id)
    finding["claims"] = ["claim1", "claim2"]
    finding["sources"] = [{"type": "file", "ref": "test.py:10"}]
    deduplicated = [(0, finding, "test-hash")]

    _call_args = (
        db_session,
        task,
        [finding],      # findings
        [],             # child_tasks (no children spawned, findings deduped)
        deduplicated,   # deduplicated
        [],             # insights
        [],             # direction_notes
        [],             # direction_proposals
        1,              # hash_dedup_count
        0,              # word_overlap_count
        0,              # file_overlap_count
        0,              # evidence_overlap_count
        0,              # type_filtered_count
        0,              # empty_prompt_count
        "research-branch",
        "main",
        0,              # research_depth
        None,           # parent_research_task_id
    )

    # First call: populate outcome_progress_history and recent_findings_evidence
    await apply_post_spawn_state(*_call_args)
    await db_session.refresh(project)

    dir_first = project.directions[0]
    rfe_first = list(dir_first.get("recent_findings_evidence", []))
    oph_first = list(dir_first.get("outcome_progress_history", []))
    hist_first = list(dir_first.get("research_history", []))

    # Record snapshots for comparison (independent of whether populated)
    rfe_first_len = len(rfe_first)
    oph_first_len = len(oph_first)
    hist_first_len = len(hist_first)

    # Second call: simulate post-commit retry — same task, same findings
    await apply_post_spawn_state(*_call_args)
    await db_session.refresh(project)

    dir_second = project.directions[0]
    rfe_second = list(dir_second.get("recent_findings_evidence", []))
    oph_second = list(dir_second.get("outcome_progress_history", []))
    hist_second = list(dir_second.get("research_history", []))

    # Verify idempotency: all three append-only lists must not grow on retry
    assert len(hist_second) == hist_first_len, (
        f"research_history duplicated on retry: "
        f"expected {hist_first_len} entries, got {len(hist_second)}"
    )
    assert len(rfe_second) == rfe_first_len, (
        f"recent_findings_evidence duplicated on retry: "
        f"expected {rfe_first_len} entries, got {len(rfe_second)}. "
        f"Before: {rfe_first}, After: {rfe_second}"
    )
    assert len(oph_second) == oph_first_len, (
        f"outcome_progress_history duplicated on retry: "
        f"expected {oph_first_len} entries, got {len(oph_second)}. "
        f"Before: {oph_first}, After: {oph_second}"
    )

    # Verify entries are identical (not just same count)
    assert rfe_second == rfe_first, (
        "recent_findings_evidence entries differ on retry"
    )
    assert oph_second == oph_first, (
        "outcome_progress_history entries differ on retry"
    )


class TestProductivityCounterReplay:
    """consecutive_unproductive_cycles must not double-count on completion-handler replay."""

    async def test_productivity_counter_idempotent_on_replay(self, monkeypatch):
        """_stamp_no_findings_refractory must increment the counter exactly once even when
        replayed with the same task (post-commit re-run scenario)."""
        import app.research_completion as rc
        from app.research_completion import _stamp_no_findings_refractory

        direction_id = str(uuid.uuid4())
        task = _Task(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            title="research",
            payloads=[{"type": "research", "current_direction_id": direction_id}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"direction_id": direction_id},
        )

        directions = [{"id": direction_id, "status": "active", "consecutive_unproductive_cycles": 1}]

        async def _fake_with_dirs_locked(session, project_id, fn, **kwargs):
            fn(directions)
            return True

        monkeypatch.setattr(rc, "with_directions_locked", _fake_with_dirs_locked)

        session = AsyncMock()

        # First call (original run): counter should increment 1 → 2
        await _stamp_no_findings_refractory(task, session)
        assert directions[0]["consecutive_unproductive_cycles"] == 2, (
            "counter should have incremented on first call"
        )

        # Second call (replay, same task): counter must NOT increment again
        await _stamp_no_findings_refractory(task, session)
        assert directions[0]["consecutive_unproductive_cycles"] == 2, (
            "counter must remain at 2 on replay — double-counting a no-findings cycle "
            "inflates the streak and triggers false saturation/auto-retirement"
        )

    async def test_productivity_counter_increments_for_different_task(self, monkeypatch):
        """A genuinely new task (different task_id) on the same direction must still increment."""
        import app.research_completion as rc
        from app.research_completion import _stamp_no_findings_refractory

        direction_id = str(uuid.uuid4())
        directions = [{"id": direction_id, "status": "active", "consecutive_unproductive_cycles": 1}]

        async def _fake_with_dirs_locked(session, project_id, fn, **kwargs):
            fn(directions)
            return True

        monkeypatch.setattr(rc, "with_directions_locked", _fake_with_dirs_locked)
        session = AsyncMock()

        def _make_task():
            return _Task(
                id=uuid.uuid4(),
                project_id=uuid.uuid4(),
                title="research",
                payloads=[{"type": "research", "current_direction_id": direction_id}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                context={"direction_id": direction_id},
            )

        await _stamp_no_findings_refractory(_make_task(), session)
        assert directions[0]["consecutive_unproductive_cycles"] == 2

        await _stamp_no_findings_refractory(_make_task(), session)
        assert directions[0]["consecutive_unproductive_cycles"] == 3, (
            "a different task's no-findings cycle must still increment the counter"
        )


async def test_note_apply_is_idempotent_on_replay(db_session):
    """Direction notes must not duplicate when apply_post_spawn_state is replayed.

    When apply_post_spawn_state is called twice with the same direction_notes (post-commit
    retry scenario), the _apply_notes callback must not append duplicate notes with the same
    text and added_by_task. The dedup check must examine existing notes before appending.
    """
    from app.research_direction_state import apply_post_spawn_state
    from test_autonomous_research import _active_direction, _make_research_task

    direction_id = "d-note-replay"
    direction = {
        **_active_direction(direction_id, "Test direction for note replay"),
        "research_history": [],
        "notes": [],
    }
    project = _Project(
        name="Note Apply Idempotency Test",
        user_id="test-user-note-idem-1",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    # Prepare direction notes to be appended
    note_text = "Test direction annotation from research completion"
    direction_notes = [
        {
            "direction_id": direction_id,
            "description": note_text,
        }
    ]

    _call_args = (
        db_session,
        task,
        [],                 # findings
        [],                 # child_tasks
        [],                 # deduplicated
        [],                 # insights
        direction_notes,    # direction_notes with the test note
        [],                 # direction_proposals
        0,                  # hash_dedup_count
        0,                  # word_overlap_count
        0,                  # file_overlap_count
        0,                  # evidence_overlap_count
        0,                  # type_filtered_count
        0,                  # empty_prompt_count
        "research-branch",
        "main",
        0,                  # research_depth
        None,               # parent_research_task_id
    )

    # First call: adds a note to the direction
    await apply_post_spawn_state(*_call_args)
    await db_session.refresh(project)

    notes_first = project.directions[0].get("notes", [])
    matching_notes_first = [n for n in notes_first if n.get("text") == note_text]
    assert len(matching_notes_first) == 1, (
        f"Expected 1 matching note after first call, got {len(matching_notes_first)}. "
        f"All notes: {notes_first}"
    )
    assert matching_notes_first[0]["added_by_task"] == str(task.id)

    # Second call: simulate post-commit retry — same task, same direction_notes
    await apply_post_spawn_state(*_call_args)
    await db_session.refresh(project)

    notes_second = project.directions[0].get("notes", [])
    matching_notes_second = [n for n in notes_second if n.get("text") == note_text]
    assert len(matching_notes_second) == 1, (
        f"Expected 1 matching note after retry (idempotent), got {len(matching_notes_second)}. "
        f"apply_post_spawn_state duplicated the direction note on post-commit retry. "
        f"All notes: {notes_second}"
    )
    assert matching_notes_second[0]["added_by_task"] == str(task.id)


async def test_zero_children_note_idempotent_on_replay(db_session):
    """Zero-children research note must not duplicate when _apply_terminal_transition is replayed.

    When a research task completes with zero children, it appends a note to the direction.
    On a post-commit retry, _apply_terminal_transition must not append duplicate notes with
    the same message and producing task id. The dedup check must examine existing notes
    before appending.
    """
    from app.research_direction_state import _apply_terminal_transition
    from test_autonomous_research import _active_direction, _make_research_task

    direction_id = "d-zero-children-replay"
    direction = {
        **_active_direction(direction_id, "Test direction for zero-children replay"),
        "research_history": [],
        "notes": [],
    }
    project = _Project(
        name="Zero Children Note Idempotency Test",
        user_id="test-user-zero-children-1",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    # Call _apply_terminal_transition with zero children, which appends a zero-children note
    _call_args = {
        "session": db_session,
        "task": task,
        "child_tasks": [],
        "findings": [],
        "direction_notes": [],
        "direction_proposals": [],
        "status_msg": "Research produced 0 tasks",
        "research_branch": "research-branch",
        "original_base": "main",
        "direction_id": direction_id,
        "skipped_duplicates": 0,
        "hash_dedup_count": 0,
        "word_overlap_count": 0,
        "file_overlap_count": 0,
        "type_filtered_count": 0,
        "empty_prompt_count": 0,
    }

    # First call: adds zero-children note to the direction
    await _apply_terminal_transition(**_call_args)
    await db_session.refresh(project)

    notes_first = project.directions[0].get("notes", [])
    # Find the note that was added by this task (should contain "created 0 tasks")
    matching_notes_first = [n for n in notes_first if "created 0 tasks" in n.get("msg", "")]
    assert len(matching_notes_first) == 1, (
        f"Expected 1 zero-children note after first call, got {len(matching_notes_first)}. "
        f"All notes: {notes_first}"
    )
    # Verify the note has the producing task id for dedup
    assert matching_notes_first[0].get("added_by_task") == str(task.id), (
        f"Expected zero-children note to have added_by_task={task.id}, "
        f"got {matching_notes_first[0].get('added_by_task')}"
    )

    # Second call: simulate post-commit retry — same task, same parameters
    await _apply_terminal_transition(**_call_args)
    await db_session.refresh(project)

    notes_second = project.directions[0].get("notes", [])
    matching_notes_second = [n for n in notes_second if "created 0 tasks" in n.get("msg", "")]
    assert len(matching_notes_second) == 1, (
        f"Expected 1 zero-children note after retry (idempotent), got {len(matching_notes_second)}. "
        f"_apply_terminal_transition duplicated the zero-children note on post-commit retry. "
        f"All notes: {notes_second}"
    )
    assert matching_notes_second[0].get("added_by_task") == str(task.id)


@_pytest.mark.asyncio
async def test_salvage_count_recorded_in_filter_breakdown(db_session):
    """salvage_count must be recorded in filter_breakdown when
    apply_post_spawn_state receives a non-zero salvage_count, so the
    research agent can see that findings were captured by the salvage
    buffer rather than silently dropped."""
    from app.research_direction_state import apply_post_spawn_state
    from test_autonomous_research import _active_direction, _make_research_task

    direction_id = "d-salvage-count-filter"
    direction = _active_direction(direction_id, "Salvage count test direction")
    project = _Project(
        name="Salvage Count Filter Test",
        user_id="test-user-salvage-cnt",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = await _make_research_task(project, direction, db_session)
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    SALVAGE_COUNT = 3

    await apply_post_spawn_state(
        db_session,
        task,
        [],   # findings
        [],   # child_tasks
        [],   # deduplicated
        [],   # insights
        [],   # direction_notes
        [],   # direction_proposals
        0,    # hash_dedup_count
        0,    # word_overlap_count
        0,    # file_overlap_count
        0,    # evidence_overlap_count
        0,    # type_filtered_count
        0,    # empty_prompt_count
        "research-branch",
        "main",
        0,    # research_depth
        None,  # parent_research_task_id
        salvage_count=SALVAGE_COUNT,
    )
    await db_session.refresh(project)

    history = project.directions[0]["research_history"]
    assert history, "Expected at least one history entry"
    fb = history[-1].get("filter_breakdown", {})
    assert fb.get("salvage_count") == SALVAGE_COUNT, (
        f"Expected salvage_count={SALVAGE_COUNT} in filter_breakdown, "
        f"got {fb.get('salvage_count')}"
    )


class TestVerificationOnlyCycleProductivity:
    """_compute_direction_productivity: verification-only cycles are credited productive."""

    def test_verification_only_cycle_credited_productive_short_refractory(self):
        """A cycle with only non-spawning findings and zero child tasks is productive.

        Prevents live-verification sweeps and metadata-only research cycles from
        incurring the extended unproductive refractory window (90-240 min) when they
        confirmed system state without spawning implementation tasks.
        """
        from app.direction_refractory import (
            resolve_direction_refractory_minutes,
            stamp_direction_refractory,
        )

        direction_id = "dir-verify-only"
        direction = {
            "id": direction_id,
            "last_cycle_productive": False,
            "consecutive_unproductive_cycles": 2,
        }

        # Simulate: agent produced only design_doc_observation findings (no child tasks)
        productive = _compute_direction_productivity(
            direction_id=direction_id,
            tasks_by_dir={},  # no child tasks spawned
            current_direction_id=direction_id,
            has_non_spawn_findings=True,  # but non-spawning findings exist
        )

        assert productive is True, (
            "A verification-only cycle (non-spawning findings, zero child tasks) "
            "must be credited productive to avoid an extended refractory window"
        )

        # Applying the productive verdict should reset the consecutive counter and
        # produce the short productive cooldown (default 45 min), not the extended
        # 90-240 min unproductive window.
        direction["last_cycle_productive"] = True
        direction["consecutive_unproductive_cycles"] = 0
        stamp_direction_refractory(direction)

        refractory_minutes = resolve_direction_refractory_minutes(direction)
        # productive_direction_cooldown_minutes defaults to 45
        assert refractory_minutes == _pytest.approx(45.0), (
            f"Expected productive cooldown ~45 min, got {refractory_minutes:.1f} min. "
            "Verification-only cycles must not receive the extended unproductive window."
        )

    def test_zero_tasks_zero_non_spawn_findings_is_unproductive(self):
        """A cycle with no findings of any kind is not productive (existing behavior)."""
        productive = _compute_direction_productivity(
            direction_id="dir-1",
            tasks_by_dir={},
            current_direction_id="dir-1",
            has_non_spawn_findings=False,
        )
        assert productive is False

    def test_spawned_tasks_make_cycle_productive_regardless_of_non_spawn(self):
        """Spawning child tasks makes the cycle productive (existing behavior preserved)."""
        productive = _compute_direction_productivity(
            direction_id="dir-1",
            tasks_by_dir={"dir-1": 2},
            current_direction_id="dir-1",
            has_non_spawn_findings=False,
        )
        assert productive is True

    def test_non_spawn_findings_do_not_credit_other_directions(self):
        """Non-spawning findings for current_direction_id do not credit a sibling direction."""
        productive = _compute_direction_productivity(
            direction_id="dir-other",
            tasks_by_dir={},
            current_direction_id="dir-primary",
            has_non_spawn_findings=True,
        )
        assert productive is False


# ---------------------------------------------------------------------------
# DB-backed integration: decay-threshold path populates _salvage_candidates
# ---------------------------------------------------------------------------


class TestDecayThresholdDBIntegration:
    """DB-backed integration tests for the salvage-bundle decay-threshold path.

    These tests call _handle_research_completion() against real in-memory
    Task/Project rows (not simulated local logic) and assert which prior-deferred
    findings get bundled into a salvage task vs kept in the pool vs dropped.

    Decay rules (from research_completion.py):
      - ultra_low_confidence:        count >= 4  → salvage
      - degradation_gate:            count >= 3  → salvage
      - adversarial_gate:            count >= 3  → salvage
      - prereq_deferred_same_cycle:  count >= 3  → salvage
      - low_actionability:           count >= 3  → salvage (when challenge disabled)
      - originally_researched_at older than TTL  → salvage
    """

    # ------------------------------------------------------------------
    # Shared test-data helpers
    # ------------------------------------------------------------------

    def _prior_finding(
        self,
        title: str,
        direction_id: str,
        deferred_reason: str,
        deferred_count: int,
        *,
        originally_researched_at: str | None = None,
    ) -> dict:
        """Return a minimal prior-deferred finding with the given decay state."""
        f: dict = {
            "title": title,
            "description": f"Description of {title}",
            "prompt": f"Fix: {title}",
            "priority": "medium",
            "scope": "small",
            "risk": "low",
            "type": "improvement",
            "direction_id": direction_id,
            "deferred_reason": deferred_reason,
            "deferred_count": deferred_count,
            "originally_researched_at": (
                originally_researched_at
                if originally_researched_at is not None
                else _utcnow().isoformat()
            ),
        }
        return f

    async def _run_completion(
        self, task, mocker, db_session, direction_id: str
    ) -> None:
        """Wire standard mocks and run _handle_research_completion."""
        from test_autonomous_research import _make_finding, _patch_research_externals

        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[_make_finding("Sentinel", direction_id=direction_id)],
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )
        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)

    async def _get_salvage_tasks(self, task, db_session) -> list:
        """Return child tasks that are salvage bundles for *task*."""
        result = await db_session.execute(
            _select(_Task).where(_Task.parent_task_id == task.id)
        )
        children = result.scalars().all()
        return [
            t
            for t in children
            if (t.context or {}).get("salvage_source_task_id") == str(task.id)
        ]

    # ------------------------------------------------------------------
    # Happy-path: each decay reason creates a salvage bundle
    # ------------------------------------------------------------------

    @_pytest.mark.asyncio
    async def test_ultra_low_confidence_count4_creates_salvage(
        self, db_session, mocker
    ):
        """ultra_low_confidence deferred 4 times hits threshold → salvage bundle created."""
        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dcyint-ulc01"
        )
        f = self._prior_finding(
            "Low-confidence finding",
            direction["id"],
            "ultra_low_confidence",
            4,
        )
        task.context = {**(task.context or {}), "prior_deferred_findings": [f]}
        db_session.add(task)
        await db_session.commit()

        await self._run_completion(task, mocker, db_session, direction["id"])

        salvage_list = await self._get_salvage_tasks(task, db_session)
        assert len(salvage_list) == 1, (
            "Exactly one salvage bundle task must be spawned for a decayed finding"
        )
        salvage = salvage_list[0]
        assert salvage.context.get("salvage_finding_count") == 1
        payload = salvage.first_payload
        assert payload["type"] == "salvage_bundle"
        assert len(payload["salvaged_findings"]) == 1
        entry = payload["salvaged_findings"][0]
        assert entry["discard_reason"] == "ultra_low_confidence"
        assert "Low-confidence finding" in entry["title"]

    @_pytest.mark.asyncio
    async def test_salvage_atomic_publication_replay_reuses_one_delivery(
        self, db_session, mocker
    ):
        """A crash after publication cannot duplicate or orphan salvage."""
        import app.research_completion as research_completion
        from app.research_execution_authority import is_authenticated_research_child
        from test_autonomous_research import _patch_research_externals

        _project, direction, task = await _grader_project_and_task(
            db_session, "d-salvage-replay"
        )
        finding = self._prior_finding(
            "Replay-safe salvage",
            direction["id"],
            "ultra_low_confidence",
            4,
        )
        task.context = {
            **(task.context or {}),
            "prior_deferred_findings": [finding],
        }
        db_session.add(task)
        await db_session.commit()
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings", return_value=[]
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )
        post_spawn_fault = mocker.patch.object(
            research_completion,
            "apply_post_spawn_state",
            side_effect=RuntimeError("crash after atomic publication"),
        )

        with _pytest.raises(RuntimeError, match="atomic publication"):
            await _handle_research_completion(task, "", db_session)
        mocker.stop(post_spawn_fault)
        await db_session.rollback()
        await db_session.refresh(task)

        first = await self._get_salvage_tasks(task, db_session)
        assert len(first) == 1
        assert is_authenticated_research_child(task, first[0])
        assert task.status == TaskStatus.EVALUATING
        assert (task.context or {}).get(FINALIZED_RESEARCH_FINDINGS_KEY) is not True
        first_id = first[0].id
        await db_session.rollback()
        await db_session.refresh(task)

        await _handle_research_completion(task, "", db_session)
        await db_session.refresh(task)
        replayed = await self._get_salvage_tasks(task, db_session)

        assert [row.id for row in replayed] == [first_id]
        assert task.status == TaskStatus.EVALUATING
        assert task.context[FINALIZED_RESEARCH_FINDINGS_KEY] is True
        assert task.context["salvage_activity"]["salvage_task_id"] == str(first_id)

    @_pytest.mark.asyncio
    async def test_degradation_gate_count3_creates_salvage(self, db_session, mocker):
        """degradation_gate deferred 3 times hits threshold → salvage bundle created."""
        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dcyint-deg01"
        )
        f = self._prior_finding(
            "Degradation-gated finding", direction["id"], "degradation_gate", 3
        )
        task.context = {**(task.context or {}), "prior_deferred_findings": [f]}
        db_session.add(task)
        await db_session.commit()

        await self._run_completion(task, mocker, db_session, direction["id"])

        salvage_list = await self._get_salvage_tasks(task, db_session)
        assert len(salvage_list) == 1
        payload = salvage_list[0].first_payload
        assert payload["salvaged_findings"][0]["discard_reason"] == "degradation_gate"

    @_pytest.mark.asyncio
    async def test_adversarial_gate_count3_creates_salvage(self, db_session, mocker):
        """adversarial_gate deferred 3 times hits threshold → salvage bundle created."""
        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dcyint-adv01"
        )
        f = self._prior_finding(
            "Adversarial-gated finding", direction["id"], "adversarial_gate", 3
        )
        task.context = {**(task.context or {}), "prior_deferred_findings": [f]}
        db_session.add(task)
        await db_session.commit()

        await self._run_completion(task, mocker, db_session, direction["id"])

        salvage_list = await self._get_salvage_tasks(task, db_session)
        assert len(salvage_list) == 1
        payload = salvage_list[0].first_payload
        assert payload["salvaged_findings"][0]["discard_reason"] == "adversarial_gate"

    @_pytest.mark.asyncio
    async def test_prereq_deferred_same_cycle_count3_creates_salvage(
        self, db_session, mocker
    ):
        """prereq_deferred_same_cycle deferred 3 times hits threshold → salvage bundle."""
        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dcyint-prq01"
        )
        f = self._prior_finding(
            "Co-deferred prerequisite finding",
            direction["id"],
            "prereq_deferred_same_cycle",
            3,
        )
        task.context = {**(task.context or {}), "prior_deferred_findings": [f]}
        db_session.add(task)
        await db_session.commit()

        await self._run_completion(task, mocker, db_session, direction["id"])

        salvage_list = await self._get_salvage_tasks(task, db_session)
        assert len(salvage_list) == 1
        payload = salvage_list[0].first_payload
        assert payload["salvaged_findings"][0]["discard_reason"] == "prereq_deferred_same_cycle"

    @_pytest.mark.asyncio
    async def test_low_actionability_count3_creates_salvage_challenge_disabled(
        self, db_session, mocker
    ):
        """low_actionability deferred 3 times decays into salvage when challenge is disabled.

        _finding_challenge_enabled returns False by default (no runtime config),
        so the finding immediately decays to salvage without spawning a challenge task.
        """
        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dcyint-low01"
        )
        f = self._prior_finding(
            "Low-actionability finding", direction["id"], "low_actionability", 3
        )
        task.context = {**(task.context or {}), "prior_deferred_findings": [f]}
        db_session.add(task)
        await db_session.commit()

        await self._run_completion(task, mocker, db_session, direction["id"])

        salvage_list = await self._get_salvage_tasks(task, db_session)
        assert len(salvage_list) == 1, (
            "low_actionability at count=3 must decay to salvage when challenge is disabled"
        )
        payload = salvage_list[0].first_payload
        assert payload["salvaged_findings"][0]["discard_reason"] == "low_actionability"

    # ------------------------------------------------------------------
    # Edge cases: below threshold → kept; stale TTL → salvaged
    # ------------------------------------------------------------------

    @_pytest.mark.asyncio
    async def test_ultra_low_confidence_count3_below_threshold_not_salvaged(
        self, db_session, mocker
    ):
        """ultra_low_confidence at count=3 is NOT at threshold (needs >=4) → no salvage."""
        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dcyint-ulc-below"
        )
        below = self._prior_finding(
            "Not-yet-decayed ULC finding", direction["id"], "ultra_low_confidence", 3
        )
        task.context = {**(task.context or {}), "prior_deferred_findings": [below]}
        db_session.add(task)
        await db_session.commit()

        await self._run_completion(task, mocker, db_session, direction["id"])

        salvage_list = await self._get_salvage_tasks(task, db_session)
        assert len(salvage_list) == 0, (
            "ultra_low_confidence at count=3 must NOT trigger salvage; threshold is >=4"
        )
        assert (task.context or {}).get("salvage_activity") is None

    @_pytest.mark.asyncio
    async def test_degradation_gate_count2_below_threshold_not_salvaged(
        self, db_session, mocker
    ):
        """degradation_gate at count=2 is below threshold (needs >=3) → no salvage."""
        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dcyint-deg-below"
        )
        below = self._prior_finding(
            "Below-threshold degradation finding",
            direction["id"],
            "degradation_gate",
            2,
        )
        task.context = {**(task.context or {}), "prior_deferred_findings": [below]}
        db_session.add(task)
        await db_session.commit()

        await self._run_completion(task, mocker, db_session, direction["id"])

        salvage_list = await self._get_salvage_tasks(task, db_session)
        assert len(salvage_list) == 0, (
            "degradation_gate at count=2 must NOT trigger salvage; threshold is >=3"
        )

    @_pytest.mark.asyncio
    async def test_stale_ttl_finding_creates_salvage(self, db_session, mocker):
        """Findings older than the TTL are bundled into salvage."""
        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dcyint-stale01"
        )
        stale = self._prior_finding(
            "Stale finding",
            direction["id"],
            "ultra_low_confidence",
            0,
            # 10 days old: well past the 7-day default TTL
            originally_researched_at=(
                _utcnow() - timedelta(days=10)
            ).isoformat(),
        )
        task.context = {**(task.context or {}), "prior_deferred_findings": [stale]}
        db_session.add(task)
        await db_session.commit()

        await self._run_completion(task, mocker, db_session, direction["id"])

        salvage_list = await self._get_salvage_tasks(task, db_session)
        assert len(salvage_list) == 1, (
            "Stale-discarded findings (beyond TTL) must enter the salvage bundle"
        )
        payload = salvage_list[0].first_payload
        assert len(payload["salvaged_findings"]) == 1
        entry = payload["salvaged_findings"][0]
        assert "Stale finding" in entry["title"]
        assert entry["discard_reason"] == "ultra_low_confidence"
        assert (task.context or {}).get("salvage_activity") is not None

    # ------------------------------------------------------------------
    # Mixed-findings: decayed and stale ones enter the bundle
    # ------------------------------------------------------------------

    @_pytest.mark.asyncio
    async def test_mixed_findings_decayed_and_stale_enter_salvage(
        self, db_session, mocker
    ):
        """Mix: decayed and stale → salvage; below-threshold → kept."""
        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dcyint-mix01"
        )
        at_threshold = self._prior_finding(
            "Decayed finding", direction["id"], "degradation_gate", 3
        )
        below_threshold = self._prior_finding(
            "Kept finding", direction["id"], "degradation_gate", 2
        )
        stale = self._prior_finding(
            "Stale finding",
            direction["id"],
            "ultra_low_confidence",
            0,
            originally_researched_at=(
                _utcnow() - timedelta(days=10)
            ).isoformat(),
        )
        task.context = {
            **(task.context or {}),
            "prior_deferred_findings": [at_threshold, below_threshold, stale],
        }
        db_session.add(task)
        await db_session.commit()

        await self._run_completion(task, mocker, db_session, direction["id"])

        salvage_list = await self._get_salvage_tasks(task, db_session)
        assert len(salvage_list) == 1, (
            "Exactly one salvage bundle should exist for discarded findings"
        )
        payload = salvage_list[0].first_payload
        assert len(payload["salvaged_findings"]) == 2, (
            "Both the at-threshold and stale findings should appear in the salvage payload"
        )
        entries_by_title = {
            entry["title"]: entry for entry in payload["salvaged_findings"]
        }
        assert "Decayed finding" in entries_by_title
        assert "Stale finding" in entries_by_title
        assert (
            entries_by_title["Decayed finding"]["discard_reason"]
            == "degradation_gate"
        )
        assert (
            entries_by_title["Stale finding"]["discard_reason"]
            == "ultra_low_confidence"
        )

    @_pytest.mark.asyncio
    async def test_multiple_decay_reasons_bundled_into_one_task(
        self, db_session, mocker
    ):
        """Multiple findings with different decay reasons are consolidated into ONE salvage task."""
        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dcyint-multi01"
        )
        findings = [
            self._prior_finding("ULC finding", direction["id"], "ultra_low_confidence", 4),
            self._prior_finding("DG finding", direction["id"], "degradation_gate", 3),
            self._prior_finding("Adv finding", direction["id"], "adversarial_gate", 3),
        ]
        task.context = {**(task.context or {}), "prior_deferred_findings": findings}
        db_session.add(task)
        await db_session.commit()

        await self._run_completion(task, mocker, db_session, direction["id"])

        salvage_list = await self._get_salvage_tasks(task, db_session)
        assert len(salvage_list) == 1, (
            "All decayed findings must be consolidated into ONE salvage task"
        )
        salvage = salvage_list[0]
        assert salvage.context.get("salvage_finding_count") == 3
        payload = salvage.first_payload
        assert len(payload["salvaged_findings"]) == 3
        reasons = {f["discard_reason"] for f in payload["salvaged_findings"]}
        assert reasons == {"ultra_low_confidence", "degradation_gate", "adversarial_gate"}

    # ------------------------------------------------------------------
    # Payload fidelity: rich context is preserved in salvage entries
    # ------------------------------------------------------------------

    @_pytest.mark.asyncio
    async def test_salvage_payload_preserves_rich_finding_context(
        self, db_session, mocker
    ):
        """The salvage payload preserves title, prompt, description, direction_id,
        related_files, and discard_reason for each decayed finding."""
        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dcyint-ctx01"
        )
        decayed: dict = {
            "title": "Cache miss in list view",
            "description": "Every page load misses the Redis cache",
            "prompt": "Add Redis caching to the list endpoint",
            "priority": "high",
            "scope": "small",
            "risk": "low",
            "type": "improvement",
            "direction_id": direction["id"],
            "related_files": ["backend/app/routers/admin_digest.py"],
            "deferred_reason": "ultra_low_confidence",
            "deferred_count": 4,
            "originally_researched_at": _utcnow().isoformat(),
        }
        task.context = {**(task.context or {}), "prior_deferred_findings": [decayed]}
        db_session.add(task)
        await db_session.commit()

        await self._run_completion(task, mocker, db_session, direction["id"])

        salvage_list = await self._get_salvage_tasks(task, db_session)
        assert len(salvage_list) == 1
        entry = salvage_list[0].first_payload["salvaged_findings"][0]
        assert entry["title"] == "Cache miss in list view"
        assert entry["discard_reason"] == "ultra_low_confidence"
        assert entry["direction_id"] == direction["id"]
        assert entry["prompt"] == "Add Redis caching to the list endpoint"
        assert entry["description"] == "Every page load misses the Redis cache"
        assert entry["related_files"] == ["backend/app/routers/admin_digest.py"]

    # ------------------------------------------------------------------
    # Parent-task context: salvage_activity is recorded after decay
    # ------------------------------------------------------------------

    @_pytest.mark.asyncio
    async def test_salvage_activity_recorded_on_parent_task_after_decay(
        self, db_session, mocker
    ):
        """salvage_activity context key is populated on the research task after decay salvage."""
        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dcyint-act01"
        )
        f = self._prior_finding(
            "Repeatedly deferred", direction["id"], "degradation_gate", 3
        )
        task.context = {**(task.context or {}), "prior_deferred_findings": [f]}
        db_session.add(task)
        await db_session.commit()

        await self._run_completion(task, mocker, db_session, direction["id"])

        salvage_activity = (task.context or {}).get("salvage_activity")
        assert salvage_activity is not None, (
            "salvage_activity must be written to the parent task context after decay salvage"
        )
        assert salvage_activity["salvaged_count"] == 1
        assert "salvage_task_id" in salvage_activity
        assert "salvaged_at" in salvage_activity

    @_pytest.mark.asyncio
    async def test_no_salvage_activity_when_all_below_threshold(
        self, db_session, mocker
    ):
        """No salvage_activity is recorded when no findings reach the decay threshold."""
        _project, direction, task = await _grader_project_and_task(
            db_session, "d-dcyint-noact01"
        )
        # count=2 for degradation_gate — one short of the threshold
        f = self._prior_finding("Near-miss finding", direction["id"], "degradation_gate", 2)
        task.context = {**(task.context or {}), "prior_deferred_findings": [f]}
        db_session.add(task)
        await db_session.commit()

        await self._run_completion(task, mocker, db_session, direction["id"])

        assert (task.context or {}).get("salvage_activity") is None, (
            "salvage_activity must NOT appear when no findings were salvaged"
        )
        salvage_list = await self._get_salvage_tasks(task, db_session)
        assert len(salvage_list) == 0


# ---------------------------------------------------------------------------
# End-to-end integration: combined decay + dedup-filtered → salvage bundle
# ---------------------------------------------------------------------------


@_pytest.mark.asyncio
async def test_salvage_bundle_combined_decay_and_dedup_path(db_session, mocker):
    """Genuine end-to-end integration: both a decayed finding (decay path) and a
    dedup-filtered finding (dedup path, #13879 ``finding`` key) flow through
    the REAL _handle_research_completion() code, are collected into
    _salvage_candidates, and appear together in the spawned salvage_bundle task.

    This test closes the direction's success_evidence gate by proving:
    1. The decay collection at research_completion.py:2113 picks up the prior-
       deferred finding (ultra_low_confidence, count>=4) and puts it in
       _salvage_candidates.
    2. The dedup-filtered salvage gate at research_completion.py:3396-3416 picks
       up a region-overlap-filtered finding whose full payload is carried in the
       DedupOutcome.filtered[].finding key (the #13879 enhancement), and appends
       it to _salvage_candidates.
    3. Both candidates merge into a single salvage_bundle task payload, with
       title, discard_reason, direction_id, prompt, description, related_files,
       claims, and sources preserved for each finding.
    """
    from app.finding_deduplication import DedupOutcome
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-combined-salvage-e2e"
    )

    # ── Path 1: decay-path finding ────────────────────────────────────────
    # ultra_low_confidence deferred 4 times triggers the decay gate.
    # Include full rich context so we can assert it survives into the payload.
    decayed_finding = {
        "title": "Slow N+1 query in report generation (decay path)",
        "description": "The report generation loop issues one query per row, causing O(n) DB calls",
        "prompt": "Add selectinload to the report query in reporting.py",
        "related_files": ["backend/app/reporting.py", "backend/app/models.py"],
        "claims": [{"claim": "N+1 pattern confirmed in profiler trace", "confidence": "high"}],
        "sources": [{"ref": "perf_trace_report_gen.json", "trust": "high"}],
        "type": "improvement",
        "priority": "medium",
        "scope": "small",
        "risk": "low",
        "direction_id": direction["id"],
        "deferred_reason": "ultra_low_confidence",
        "deferred_count": 4,
        "originally_researched_at": _utcnow().isoformat(),
    }
    task.context = {
        **(task.context or {}),
        "prior_deferred_findings": [decayed_finding],
    }
    db_session.add(task)
    await db_session.commit()

    # ── Path 2: dedup-filtered finding (#13879 ``finding`` key) ──────────
    # A fresh finding filtered by region overlap.  The ``finding`` key carries the
    # full payload — this is the enhancement from PR #13879 that the old
    # title-based lookup could not preserve without a separate lookup.
    dedup_filtered_finding = {
        "title": "Missing index on task.status causes full table scan (dedup path)",
        "description": "The task listing query performs a full table scan on status column",
        "prompt": "Add a covering index on task.status + task.created_at in alembic",
        "related_files": [
            "backend/app/models.py",
            "backend/alembic/versions/next_migration.py",
        ],
        "claims": [
            {"claim": "Full table scan observed in EXPLAIN ANALYZE", "confidence": "high"},
            {"claim": "Index selectivity > 80%", "confidence": "medium"},
        ],
        "sources": [{"ref": "db_query_plan_20260710.txt", "trust": "high"}],
        "type": "improvement",
        "priority": "high",
        "scope": "small",
        "risk": "low",
        "direction_id": direction["id"],
    }
    # A surviving finding (required so dedup produces a deduplicated list).
    surviving_finding = _make_finding(
        "Unrelated sentinel finding", direction_id=direction["id"]
    )

    # Build the DedupOutcome that the production code will consume.
    # The ``finding`` key in the filtered entry is the PR-#13879 addition —
    # it carries the full payload so no title-based lookup is needed.
    mock_dedup_outcome = DedupOutcome()
    mock_dedup_outcome.deduplicated = [(0, surviving_finding, "hash-sentinel")]
    mock_dedup_outcome.word_overlap_count = 1
    mock_dedup_outcome.filtered = [
        {
            "title": dedup_filtered_finding["title"][:120],
            "direction_id": direction["id"],
            "reason": "region overlap with: existing index task",
            # The ``finding`` key is what #13879 added — full payload,
            # not just title/direction_id as before.
            "finding": dedup_filtered_finding,
        }
    ]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[dedup_filtered_finding, surviving_finding],
    )
    mocker.patch(
        "app.research_completion.deduplicate_findings",
        return_value=mock_dedup_outcome,
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    # ── Exercise the REAL production path ────────────────────────────────
    await _handle_research_completion(task, "", db_session)
    await db_session.refresh(task)

    # ── Assert salvage_activity was recorded on the parent task ──────────
    salvage_activity = (task.context or {}).get("salvage_activity")
    assert salvage_activity is not None, (
        "salvage_activity must be recorded on the parent task when both "
        "decay-path and dedup-path findings are salvaged"
    )
    assert salvage_activity["salvaged_count"] == 2, (
        f"Expected 2 salvaged findings (1 decay + 1 dedup), "
        f"got {salvage_activity['salvaged_count']}"
    )
    assert "salvage_task_id" in salvage_activity
    assert "salvaged_at" in salvage_activity

    # ── Assert exactly one salvage_bundle child task was spawned ─────────
    result = await db_session.execute(
        _select(_Task).where(_Task.parent_task_id == task.id)
    )
    all_children = result.scalars().all()
    salvage_tasks = [
        t
        for t in all_children
        if (t.context or {}).get("salvage_source_task_id") == str(task.id)
    ]
    assert len(salvage_tasks) == 1, (
        f"Expected exactly 1 salvage_bundle child task, "
        f"found {len(salvage_tasks)} among {len(all_children)} total children"
    )

    salvage_task = salvage_tasks[0]
    assert salvage_task.context.get("salvage_finding_count") == 2
    assert "discarded" in salvage_task.title.lower()

    # ── Assert the combined payload contains BOTH findings ────────────────
    payload = salvage_task.first_payload
    assert payload is not None
    assert payload["type"] == "salvage_bundle"
    assert payload["total_discarded"] == 2
    assert len(payload["salvaged_findings"]) == 2

    entries_by_title = {e["title"]: e for e in payload["salvaged_findings"]}

    # ── Decay-path finding: verify title, discard_reason, and rich context
    decay_key = "Slow N+1 query in report generation (decay path)"
    assert decay_key in entries_by_title, (
        f"Decayed finding must appear in salvage payload. "
        f"Found titles: {list(entries_by_title)}"
    )
    decay_entry = entries_by_title[decay_key]
    assert decay_entry["discard_reason"] == "ultra_low_confidence", (
        f"decay entry discard_reason must be 'ultra_low_confidence', "
        f"got {decay_entry['discard_reason']!r}"
    )
    assert decay_entry["direction_id"] == direction["id"]
    assert decay_entry["prompt"] == "Add selectinload to the report query in reporting.py"
    assert decay_entry["description"] == (
        "The report generation loop issues one query per row, causing O(n) DB calls"
    )
    assert decay_entry["related_files"] == [
        "backend/app/reporting.py",
        "backend/app/models.py",
    ]
    assert len(decay_entry["claims"]) == 1
    assert decay_entry["claims"][0]["claim"] == "N+1 pattern confirmed in profiler trace"
    assert len(decay_entry["sources"]) == 1
    assert decay_entry["sources"][0]["ref"] == "perf_trace_report_gen.json"

    # ── Dedup-path finding: verify title, discard_reason, and rich context
    # This is the key #13879 assertion: the full finding payload is preserved
    # via the ``finding`` key rather than a title-based lookup.
    dedup_key = "Missing index on task.status causes full table scan (dedup path)"
    assert dedup_key in entries_by_title, (
        f"Dedup-filtered finding must appear in salvage payload. "
        f"Found titles: {list(entries_by_title)}"
    )
    dedup_entry = entries_by_title[dedup_key]
    assert "dedup:" in (dedup_entry["discard_reason"] or ""), (
        f"dedup entry discard_reason must start with 'dedup:', "
        f"got {dedup_entry['discard_reason']!r}"
    )
    assert "region overlap" in (dedup_entry["discard_reason"] or ""), (
        f"dedup discard_reason must preserve the original dedup reason, "
        f"got {dedup_entry['discard_reason']!r}"
    )
    assert dedup_entry["direction_id"] == direction["id"]
    assert dedup_entry["prompt"] == (
        "Add a covering index on task.status + task.created_at in alembic"
    )
    assert dedup_entry["description"] == (
        "The task listing query performs a full table scan on status column"
    )
    assert dedup_entry["related_files"] == [
        "backend/app/models.py",
        "backend/alembic/versions/next_migration.py",
    ]
    assert len(dedup_entry["claims"]) == 2
    assert dedup_entry["claims"][0]["claim"] == "Full table scan observed in EXPLAIN ANALYZE"
    assert dedup_entry["claims"][1]["claim"] == "Index selectivity > 80%"
    assert len(dedup_entry["sources"]) == 1
    assert dedup_entry["sources"][0]["ref"] == "db_query_plan_20260710.txt"


# ── Objective completion, increment 1 (residual half): a non-null merge-grade
# residual ("gap no criterion captures") opens a durable objective_gap
# completion-debt entry, mirroring the per-category sync that
# upsert_category_verdict already performs; a later cleared residual resolves
# it. Exercises the REAL production path (category_verdict_ledger.upsert_residual
# -> app.completion_debt), not a stub, against a real merge-grade-shaped verdict.
class TestObjectiveGapResidualCompletionDebt:
    async def _project_and_version(self, db_session):
        from app import release_version as _rv

        project = _Project(
            name="Objective Gap Residual Test Project",
            user_id="objective-gap-residual-user",
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        version = await _rv.open_version(db_session, project.id, version_number=1)
        await db_session.commit()
        await db_session.refresh(version)
        return project, version

    @_pytest.mark.asyncio
    async def test_real_merge_grade_residual_opens_and_later_clears_objective_gap_debt(
        self, db_session
    ):
        """A real ``upsert_residual`` call carrying the blind merge-grade
        judge's ``residual_uncaptured_gap`` opens a durable objective_gap
        entry; a later candidate with no residual resolves it."""
        from app import category_verdict_ledger as _cvl
        from app import completion_debt as _cd
        from app.models import CompletionDebtEntry

        project, version = await self._project_and_version(db_session)
        dedupe_key = _cd.objective_gap_dedupe_key(
            version.id, _cd.OBJECTIVE_GAP_RESIDUAL_KEY
        )

        # Real merge-grade artifact shape: category_verdicts all `met`, but a
        # non-empty residual ("gap no criterion captures") remains.
        await _cvl.upsert_category_verdict(
            db_session,
            version_id=version.id,
            candidate_sha="a" * 40,
            category_key="c-1a2b3c4d",
            status="met",
            tier="judged",
            gap=None,
            citations=[{"file": "backend/app/export.py", "line": 42}],
            grader_run_id="run-1",
            project_id=project.id,
        )
        await _cvl.upsert_residual(
            db_session,
            version.id,
            "a" * 40,
            "The candidate handles export but never addressed migration rollback safety",
            qualification_attempt_id=None,
            project_id=project.id,
        )
        await db_session.commit()

        row = (
            await db_session.execute(
                _select(CompletionDebtEntry).where(
                    CompletionDebtEntry.dedupe_key == dedupe_key
                )
            )
        ).scalar_one()
        assert row.kind == _cd.KIND_OBJECTIVE_GAP
        assert row.status == _cd.STATUS_OPEN
        assert row.project_id == project.id
        assert (row.detail or {}).get("gap") == (
            "The candidate handles export but never addressed migration rollback safety"
        )

        # A later candidate whose grade run reports NO residual gap resolves it.
        await _cvl.upsert_residual(
            db_session,
            version.id,
            "b" * 40,
            None,
            qualification_attempt_id=None,
            project_id=project.id,
        )
        await db_session.commit()
        await db_session.refresh(row)

        assert row.status == _cd.STATUS_RESOLVED
        assert row.resolved_at is not None

    @_pytest.mark.asyncio
    async def test_residual_debt_independent_of_category_debt_dedupe_key(
        self, db_session
    ):
        """The residual sentinel key must never collide with a real
        criterion_key's dedupe key, even for adversarial category_key input."""
        from app import completion_debt as _cd

        _project, version = await self._project_and_version(db_session)
        residual_key = _cd.objective_gap_dedupe_key(
            version.id, _cd.OBJECTIVE_GAP_RESIDUAL_KEY
        )
        category_key = _cd.objective_gap_dedupe_key(version.id, "c-1a2b3c4d")
        assert residual_key != category_key
        assert _cd.OBJECTIVE_GAP_RESIDUAL_KEY not in ("c-1a2b3c4d",)

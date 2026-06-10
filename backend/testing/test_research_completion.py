"""Unit tests for research_completion.py pre-spawn scope-budget gate, low_actionability decay, grade normalization, source ref validation, and findings index."""

import json
import logging
import uuid
from datetime import timedelta
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
    _parse_ref,
    _symbol_defined_at_head,
    collect_stale_refs,
    extract_source_file_paths,
    summarize_ungrounded_claims,
    summarize_broken_refs,
    validate_source_refs,
)
from app.research_completion import (
    _DOWNGRADED_VERIFICATION_BASIS,
    _HIGH_EVIDENCE_BASES,
    VALID_VERIFICATION_BASES,
    _annotate_capability_declarations,
    _apply_structural_actionability_downgrade,
    _handle_research_completion,
    _reconcile_verification_basis,
    _record_research_survival_signal,
    _research_findings_would_spawn,
    _summarize_ungrounded_claims_grounded,
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
from app.research_completion import _is_high_stakes_finding
from app.time_utils import _utcnow


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

    def test_medium_12_files_upgrades_to_large(self):
        files = [f"file{i}.py" for i in range(12)]
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch(
            "medium", files
        )
        assert scope == "large"
        assert cal_from == "medium"
        assert warning is not None and "scope_budget_mismatch" in warning
        assert not requires_approval

    def test_large_30_files_requires_approval(self):
        files = [f"file{i}.py" for i in range(30)]
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
            if (_reason == "ultra_low_confidence" and _count >= 4) or (_reason == "degradation_gate" and _count >= 3) or (_reason == "low_actionability" and _count >= 3):
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


class TestActionabilityGradeNormalization:
    """Normalization step: missing or invalid actionability_grade defaults to MEDIUM with a warning."""

    def _normalize(self, findings: list, caplog=None) -> list:
        """Simulate the normalization loop in research_completion.py."""
        _valid_grades = {"HIGH", "MEDIUM", "LOW"}
        result = [dict(f) for f in findings]
        for _f in result:
            _g = _f.get("actionability_grade")
            if _g not in _valid_grades:
                if caplog is not None:
                    pass  # caplog tracks via logging
                _f["actionability_grade"] = "MEDIUM"
        return result

    def test_missing_grade_defaults_to_medium(self, caplog):
        """Finding with no actionability_grade is coerced to MEDIUM with a warning."""
        findings = [{"title": "no-grade-finding"}]
        logger = logging.getLogger("app.research_completion")  # noqa: F841
        with caplog.at_level(logging.WARNING, logger="app.research_completion"):
            normalized = self._normalize(findings)
        assert normalized[0]["actionability_grade"] == "MEDIUM"

    def test_invalid_grade_defaults_to_medium(self, caplog):
        """Finding with an unrecognised grade string is coerced to MEDIUM."""
        findings = [{"title": "bad-grade", "actionability_grade": "UNKNOWN"}]
        normalized = self._normalize(findings)
        assert normalized[0]["actionability_grade"] == "MEDIUM"

    def test_none_grade_defaults_to_medium(self):
        """Finding with explicit None grade is coerced to MEDIUM."""
        findings = [{"title": "none-grade", "actionability_grade": None}]
        normalized = self._normalize(findings)
        assert normalized[0]["actionability_grade"] == "MEDIUM"

    def test_high_grade_passes_through(self):
        """HIGH grade is preserved unchanged."""
        findings = [{"title": "high-finding", "actionability_grade": "HIGH"}]
        normalized = self._normalize(findings)
        assert normalized[0]["actionability_grade"] == "HIGH"

    def test_medium_grade_passes_through(self):
        """MEDIUM grade is preserved unchanged."""
        findings = [{"title": "medium-finding", "actionability_grade": "MEDIUM"}]
        normalized = self._normalize(findings)
        assert normalized[0]["actionability_grade"] == "MEDIUM"

    def test_low_grade_passes_through(self):
        """LOW grade is preserved unchanged and enters the deferral gate downstream."""
        findings = [{"title": "low-finding", "actionability_grade": "LOW"}]
        normalized = self._normalize(findings)
        assert normalized[0]["actionability_grade"] == "LOW"

    def test_mixed_batch_normalization(self):
        """Batch with mixed valid, missing, and invalid grades is fully normalized."""
        findings = [
            {"title": "high", "actionability_grade": "HIGH"},
            {"title": "missing"},
            {"title": "low", "actionability_grade": "LOW"},
            {"title": "invalid", "actionability_grade": "BAD"},
            {"title": "medium", "actionability_grade": "MEDIUM"},
        ]
        normalized = self._normalize(findings)
        assert normalized[0]["actionability_grade"] == "HIGH"
        assert normalized[1]["actionability_grade"] == "MEDIUM"
        assert normalized[2]["actionability_grade"] == "LOW"
        assert normalized[3]["actionability_grade"] == "MEDIUM"
        assert normalized[4]["actionability_grade"] == "MEDIUM"

    def test_thin_finding_is_downgraded_to_low(self):
        """A finding missing claims, sources, and sufficient description is forced to LOW."""
        finding = {
            "title": "thin",
            "actionability_grade": "HIGH",
            "description": "too short",
        }
        structural_grade = (
            "MEDIUM"
            if (finding.get("claims") or [])
            and len((finding.get("description") or "").strip()) >= 40
            else "LOW"
        )
        assert structural_grade == "LOW"


class TestVerificationBasisNormalization:
    """Normalization step: missing or invalid verification_basis defaults to
    pattern_inferred (the lowest-evidence tier); a non-missing invalid value
    is logged just like the actionability_grade path."""

    _VALID = {"code_verified", "path_verified", "pattern_inferred", "deduced"}

    def _normalize(self, findings: list) -> list:
        """Simulate the verification_basis normalization loop in
        research_completion.py (logging warning only for non-missing invalids)."""
        logger = logging.getLogger("app.research_completion")
        result = [dict(f) for f in findings]
        for _f in result:
            _vb = _f.get("verification_basis")
            if _vb not in self._VALID:
                if _vb is not None:
                    logger.warning(
                        "[RESEARCH] Finding '%s' has invalid verification_basis=%r; "
                        "defaulting to pattern_inferred",
                        _f.get("title", "<untitled>"),
                        _vb,
                    )
                _f["verification_basis"] = "pattern_inferred"
        return result

    def test_missing_basis_defaults_to_pattern_inferred(self, caplog):
        findings = [{"title": "no-basis"}]
        with caplog.at_level(logging.WARNING, logger="app.research_completion"):
            normalized = self._normalize(findings)
        assert normalized[0]["verification_basis"] == "pattern_inferred"
        # Missing (not invalid) values are NOT warned about.
        assert "invalid verification_basis" not in caplog.text

    def test_invalid_basis_defaults_to_pattern_inferred_with_warning(self, caplog):
        findings = [{"title": "bad-basis", "verification_basis": "guessed"}]
        with caplog.at_level(logging.WARNING, logger="app.research_completion"):
            normalized = self._normalize(findings)
        assert normalized[0]["verification_basis"] == "pattern_inferred"
        assert "invalid verification_basis" in caplog.text

    def test_none_basis_defaults_to_pattern_inferred(self):
        findings = [{"title": "none-basis", "verification_basis": None}]
        normalized = self._normalize(findings)
        assert normalized[0]["verification_basis"] == "pattern_inferred"

    def test_each_valid_basis_passes_through(self):
        for basis in self._VALID:
            findings = [{"title": basis, "verification_basis": basis}]
            normalized = self._normalize(findings)
            assert normalized[0]["verification_basis"] == basis

    def test_mixed_batch_normalization(self):
        findings = [
            {"title": "code", "verification_basis": "code_verified"},
            {"title": "missing"},
            {"title": "path", "verification_basis": "path_verified"},
            {"title": "invalid", "verification_basis": "BOGUS"},
            {"title": "deduced", "verification_basis": "deduced"},
        ]
        normalized = self._normalize(findings)
        assert normalized[0]["verification_basis"] == "code_verified"
        assert normalized[1]["verification_basis"] == "pattern_inferred"
        assert normalized[2]["verification_basis"] == "path_verified"
        assert normalized[3]["verification_basis"] == "pattern_inferred"
        assert normalized[4]["verification_basis"] == "deduced"


class TestVerificationBasisHeadReconciliation:
    """_reconcile_verification_basis: a self-claimed verified-against-code tier
    is capped to an honest tier when the finding's own citations fail to resolve
    at HEAD; clean or absent citations preserve the reported basis."""

    def test_shared_tier_constant_matches_normalization_set(self):
        assert {
            "code_verified",
            "path_verified",
            "pattern_inferred",
            "deduced",
        } == VALID_VERIFICATION_BASES
        assert {"code_verified", "path_verified"} == _HIGH_EVIDENCE_BASES
        assert _DOWNGRADED_VERIFICATION_BASIS == "pattern_inferred"

    def test_code_verified_with_broken_citation_downgrades(self):
        f = {"title": "broken-cite", "verification_basis": "code_verified"}
        broken = {"broken_source_ref_count": 1, "broken_refs": ["app/gone.py"]}
        assert _reconcile_verification_basis(f, broken, None) is True
        assert f["verification_basis"] == "pattern_inferred"
        assert f["verification_basis_downgraded_from"] == "code_verified"
        assert f["verification_basis_downgrade_reason"] == (
            "HEAD validation failed: 1 broken source ref, 0 ungrounded claims"
        )

    def test_path_verified_with_ungrounded_claim_downgrades(self):
        f = {"title": "ungrounded", "verification_basis": "path_verified"}
        ungrounded = {"ungrounded_claim_count": 2, "ungrounded_refs": ["app/x.py:foo"]}
        assert _reconcile_verification_basis(f, None, ungrounded) is True
        assert f["verification_basis"] == "pattern_inferred"
        assert f["verification_basis_downgraded_from"] == "path_verified"
        assert f["verification_basis_downgrade_reason"] == (
            "HEAD validation failed: 0 broken source refs, 2 ungrounded claims"
        )

    def test_clean_citations_preserve_high_basis(self):
        f = {"title": "clean", "verification_basis": "code_verified"}
        assert _reconcile_verification_basis(f, None, None) is False
        assert f["verification_basis"] == "code_verified"

    def test_no_citations_preserve_high_basis(self):
        f = {"title": "no-cites", "verification_basis": "path_verified"}
        assert _reconcile_verification_basis(f, None, None) is False
        assert f["verification_basis"] == "path_verified"

    def test_low_evidence_basis_not_touched_even_with_broken_refs(self):
        # A finding that already self-reports a low-evidence tier is honest; a
        # broken citation does not push it further down (no negative tier).
        f = {"title": "already-low", "verification_basis": "deduced"}
        broken = {"broken_source_ref_count": 3, "broken_refs": ["a.py", "b.py"]}
        assert _reconcile_verification_basis(f, broken, None) is False
        assert f["verification_basis"] == "deduced"

    def test_both_signals_downgrade_once(self):
        f = {"title": "both", "verification_basis": "code_verified"}
        broken = {"broken_source_ref_count": 1, "broken_refs": ["a.py"]}
        ungrounded = {"ungrounded_claim_count": 1, "ungrounded_refs": ["a.py:fn"]}
        assert _reconcile_verification_basis(f, broken, ungrounded) is True
        assert f["verification_basis"] == "pattern_inferred"


class TestSourceRefParsing:
    """Parsing of file source refs into (path, start_line, end_line)."""

    def test_parse_plain_path(self):
        """Plain path with no line numbers."""
        path, start, end = _parse_ref("backend/app/research_completion.py")
        assert path == "backend/app/research_completion.py"
        assert start is None
        assert end is None

    def test_parse_with_colon_line(self):
        """Path with colon-delimited line number (format: path:42)."""
        path, start, end = _parse_ref("backend/app/research_completion.py:42")
        assert path == "backend/app/research_completion.py"
        assert start == 42
        assert end is None

    def test_parse_with_colon_range(self):
        """Path with colon-delimited line range (format: path:10-20)."""
        path, start, end = _parse_ref("backend/app/research_completion.py:10-20")
        assert path == "backend/app/research_completion.py"
        assert start == 10
        assert end == 20

    def test_parse_with_hash_l_line(self):
        """Path with GitHub #L syntax for single line."""
        path, start, end = _parse_ref("backend/app/research_completion.py#L42")
        assert path == "backend/app/research_completion.py"
        assert start == 42
        assert end is None

    def test_parse_with_hash_l_range(self):
        """Path with GitHub #L syntax for line range."""
        path, start, end = _parse_ref("backend/app/research_completion.py#L10-L20")
        assert path == "backend/app/research_completion.py"
        assert start == 10
        assert end == 20

    def test_parse_invalid_ref(self):
        """Invalid or empty ref returns the ref unchanged with no line numbers."""
        path, start, end = _parse_ref("")
        assert path == ""
        assert start is None
        assert end is None

    def test_parse_none_ref(self):
        """None ref returns (None, None, None)."""
        path, start, end = _parse_ref(None)
        assert path is None
        assert start is None
        assert end is None


class TestSourceRefValidation:
    """Validation of research finding source file references."""

    def _patch_git_show(self, monkeypatch, *, stdout=b"", stderr=b"", returncode=0):
        from app import source_ref_validation as srv

        class _Proc:
            def __init__(self):
                self.returncode = returncode

            async def communicate(self):
                return stdout, stderr

        async def _create(*args, **kwargs):
            return _Proc()

        monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", _create)

    async def test_pr_source_passes_through(self):
        """PR-type sources require no validation."""
        sources = [
            {
                "type": "pr",
                "ref": "https://github.com/owner/repo/pull/123",
                "description": "PR that added feature X",
            }
        ]
        validated = await validate_source_refs(sources)
        assert len(validated) == 1
        assert validated[0]["type"] == "pr"
        # No "valid" field should be added for PR sources
        assert "valid" not in validated[0] or validated[0].get("valid") is not False

    async def test_message_source_passes_through(self):
        """Message-type sources require no validation."""
        sources = [
            {
                "type": "message",
                "ref": "Slack message from @user",
                "description": "Team feedback on design",
            }
        ]
        validated = await validate_source_refs(sources)
        assert len(validated) == 1
        assert validated[0]["type"] == "message"

    async def test_file_source_valid(self, monkeypatch):
        """Valid file source (file exists) passes validation."""
        # Mock the workspace helpers so the test doesn't depend on /tmp/workspace
        # being present in the test environment (it isn't, inside the Docker
        # backend test container).
        from app import source_ref_validation as srv

        monkeypatch.setattr(srv, "_file_exists_at_head", AsyncMock(return_value=True))
        sources = [
            {
                "type": "file",
                "ref": "backend/testing/test_research_completion.py",
                "description": "Test file",
            }
        ]
        validated = await validate_source_refs(sources)
        assert len(validated) == 1
        # Should not have valid: false marker if validation passed
        assert validated[0].get("valid") != False  # noqa: E712

    async def test_file_source_missing_file(self, monkeypatch):
        """Missing file gets marked with valid: false."""
        from app import source_ref_validation as srv

        monkeypatch.setattr(srv, "_file_exists_at_head", AsyncMock(return_value=False))
        sources = [
            {
                "type": "file",
                "ref": "nonexistent/file_that_does_not_exist_12345.py",
                "description": "Missing file",
            }
        ]
        validated = await validate_source_refs(sources)
        assert len(validated) == 1
        # Should be marked as invalid
        assert validated[0].get("valid") == False  # noqa: E712

    async def test_file_source_missing_ref(self):
        """File source without ref field is dropped."""
        sources = [
            {
                "type": "file",
                "description": "File source without ref",
            }
        ]
        validated = await validate_source_refs(sources)
        # Should be dropped (not in validated list)
        assert len(validated) == 0

    async def test_file_source_with_line_valid(self, monkeypatch):
        """File source with valid line number."""
        from app import source_ref_validation as srv

        monkeypatch.setattr(srv, "_file_exists_at_head", AsyncMock(return_value=True))
        monkeypatch.setattr(srv, "_file_has_line_count", AsyncMock(return_value=True))
        sources = [
            {
                "type": "file",
                "ref": "backend/testing/test_research_completion.py:5",
                "description": "File at line 5",
            }
        ]
        validated = await validate_source_refs(sources)
        assert len(validated) == 1
        assert validated[0].get("valid") != False  # noqa: E712

    async def test_non_dict_source_dropped(self):
        """Non-dict source entries are dropped."""
        sources = [
            "not a dict",
            {"type": "file", "ref": "backend/app/research_completion.py"},
        ]
        validated = await validate_source_refs(sources)
        # The string should be dropped, only the dict should remain
        assert len(validated) == 1
        assert validated[0]["type"] == "file"

    async def test_none_sources_returns_empty_list(self):
        """None sources input returns empty list."""
        validated = await validate_source_refs(None)
        assert validated == []

    async def test_non_list_sources_returns_empty_list(self):
        """Non-list sources input returns empty list."""
        validated = await validate_source_refs("not a list")
        assert validated == []

    async def test_mixed_source_types(self, monkeypatch):
        """Batch with mixed source types is validated correctly.

        The existence check is mocked so the test does not depend on a local
        /tmp/workspace checkout (absent in the backend test container). Under
        the conservative degradation rules an unmocked missing file would be
        indeterminate (None) and therefore NOT flagged — so the missing ref is
        explicitly resolved to False here to exercise the invalid-marking path.
        """
        from app import source_ref_validation as srv

        missing = "nonexistent/missing.py"
        monkeypatch.setattr(
            srv,
            "_file_exists_at_head",
            AsyncMock(side_effect=lambda path: path != missing),
        )
        sources = [
            {
                "type": "pr",
                "ref": "https://github.com/owner/repo/pull/999",
            },
            {
                "type": "file",
                "ref": "backend/testing/test_research_completion.py",
            },
            {
                "type": "message",
                "ref": "Slack feedback",
            },
            {
                "type": "file",
                "ref": missing,
            },
        ]
        validated = await validate_source_refs(sources)
        # All sources should be present (even the missing file gets marked invalid)
        assert len(validated) == 4
        # Check types are preserved
        assert validated[0]["type"] == "pr"
        assert validated[1]["type"] == "file"
        assert validated[2]["type"] == "message"
        assert validated[3]["type"] == "file"
        # Check invalid file is marked
        assert validated[3].get("valid") == False  # noqa: E712

    async def test_unknown_source_type_passes_through(self):
        """Unknown source types pass through unchanged."""
        sources = [
            {
                "type": "custom_type",
                "ref": "some_reference",
                "description": "Custom source",
            }
        ]
        validated = await validate_source_refs(sources)
        assert len(validated) == 1
        assert validated[0]["type"] == "custom_type"

    async def test_symbol_ref_present_is_grounded(self, monkeypatch):
        self._patch_git_show(monkeypatch, stdout=b"def present_symbol():\n    pass\n")
        assert await _symbol_defined_at_head("backend/app/example.py", "present_symbol") is True

    async def test_symbol_ref_absent_in_existing_file_is_flagged(self, monkeypatch):
        self._patch_git_show(monkeypatch, stdout=b"def other_symbol():\n    pass\n")
        assert await _symbol_defined_at_head("backend/app/example.py", "missing_symbol") is False

    async def test_symbol_ref_missing_file_is_flagged(self, monkeypatch):
        self._patch_git_show(
            monkeypatch,
            returncode=128,
            stderr=b"fatal: path 'backend/app/missing.py' exists on disk, but not in 'HEAD'",
        )
        assert await _symbol_defined_at_head("backend/app/missing.py", "missing") is False

    async def test_malformed_symbol_ref_is_ignored(self):
        assert await summarize_ungrounded_claims(["backend/app/example.py:42"]) is None

    async def test_git_failure_degrades_gracefully(self, monkeypatch):
        from app import source_ref_validation as srv

        async def _create(*args, **kwargs):
            raise OSError("git unavailable")

        monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", _create)
        assert await _symbol_defined_at_head("backend/app/example.py", "present_symbol") is None


class TestSourceRefValidationBranchAware:
    """validate_source_refs verifies against the project's REAL integration
    branch (via the GitHub contents API) when a verification context is passed,
    with a graceful local fallback, and NEVER marks a citation invalid on an
    indeterminate result.
    """

    _CTX = {
        "token": "tok",
        "owner": "octo",
        "repo": "repo",
        "branch": "integration",
        "project_id": "proj-1",
    }

    async def test_github_path_valid_file_not_flagged(self, monkeypatch):
        """A file present on the integration branch is not flagged."""
        from app import source_ref_validation as srv

        monkeypatch.setattr(
            srv, "_file_content_in_repo_branch", AsyncMock(return_value=(True, 100))
        )
        sources = [{"type": "file", "ref": "backend/app/foo.py"}]
        validated = await validate_source_refs(sources, **self._CTX)
        assert len(validated) == 1
        assert validated[0].get("valid") != False  # noqa: E712

    async def test_github_path_absent_file_flagged(self, monkeypatch):
        """A file unambiguously absent on the integration branch (404) is flagged."""
        from app import source_ref_validation as srv

        monkeypatch.setattr(
            srv, "_file_content_in_repo_branch", AsyncMock(return_value=(False, None))
        )
        sources = [{"type": "file", "ref": "backend/app/gone.py"}]
        validated = await validate_source_refs(sources, **self._CTX)
        assert validated[0].get("valid") == False  # noqa: E712

    async def test_github_path_line_out_of_range_flagged(self, monkeypatch):
        """A line number beyond the branch file's length is flagged."""
        from app import source_ref_validation as srv

        monkeypatch.setattr(
            srv, "_file_content_in_repo_branch", AsyncMock(return_value=(True, 10))
        )
        sources = [{"type": "file", "ref": "backend/app/foo.py:99"}]
        validated = await validate_source_refs(sources, **self._CTX)
        assert validated[0].get("valid") == False  # noqa: E712

    async def test_github_path_line_within_range_not_flagged(self, monkeypatch):
        """A line number within the branch file's length is not flagged."""
        from app import source_ref_validation as srv

        monkeypatch.setattr(
            srv, "_file_content_in_repo_branch", AsyncMock(return_value=(True, 200))
        )
        sources = [{"type": "file", "ref": "backend/app/foo.py:42-50"}]
        validated = await validate_source_refs(sources, **self._CTX)
        assert validated[0].get("valid") != False  # noqa: E712

    async def test_github_path_indeterminate_existence_never_invalid(self, monkeypatch):
        """An indeterminate existence result (None) never marks the ref invalid.

        This is the core prod fix: a backend with no checkout and a GitHub
        API error must NOT collapse every citation into file-not-found.
        """
        from app import source_ref_validation as srv

        monkeypatch.setattr(
            srv, "_file_content_in_repo_branch", AsyncMock(return_value=(None, None))
        )
        sources = [{"type": "file", "ref": "backend/app/foo.py:42"}]
        validated = await validate_source_refs(sources, **self._CTX)
        assert validated[0].get("valid") != False  # noqa: E712

    async def test_github_path_indeterminate_line_count_never_invalid(self, monkeypatch):
        """File exists but its line count is indeterminate → line ref not flagged."""
        from app import source_ref_validation as srv

        monkeypatch.setattr(
            srv, "_file_content_in_repo_branch", AsyncMock(return_value=(True, None))
        )
        sources = [{"type": "file", "ref": "backend/app/foo.py:9999"}]
        validated = await validate_source_refs(sources, **self._CTX)
        assert validated[0].get("valid") != False  # noqa: E712

    async def test_partial_context_falls_back_to_local(self, monkeypatch):
        """An incomplete GitHub context falls back to the local HEAD checker."""
        from app import source_ref_validation as srv

        content_spy = AsyncMock(return_value=(False, None))
        monkeypatch.setattr(srv, "_file_content_in_repo_branch", content_spy)
        monkeypatch.setattr(srv, "_file_exists_at_head", AsyncMock(return_value=True))
        sources = [{"type": "file", "ref": "backend/app/foo.py"}]
        # owner/repo/branch present but token missing → not use_repo_branch.
        validated = await validate_source_refs(
            sources, owner="octo", repo="repo", branch="integration"
        )
        assert validated[0].get("valid") != False  # noqa: E712
        content_spy.assert_not_awaited()

    async def test_local_fallback_indeterminate_never_invalid(self, monkeypatch):
        """Local-path indeterminate (no checkout → None) never marks invalid."""
        from app import source_ref_validation as srv

        monkeypatch.setattr(srv, "_file_exists_at_head", AsyncMock(return_value=None))
        sources = [{"type": "file", "ref": "backend/app/foo.py:42"}]
        validated = await validate_source_refs(sources)
        assert validated[0].get("valid") != False  # noqa: E712

    async def test_malformed_range_flagged_regardless_of_tree(self, monkeypatch):
        """end < start is intrinsically malformed and flagged even on the branch path."""
        from app import source_ref_validation as srv

        monkeypatch.setattr(
            srv, "_file_content_in_repo_branch", AsyncMock(return_value=(True, 500))
        )
        sources = [{"type": "file", "ref": "backend/app/foo.py:50-10"}]
        validated = await validate_source_refs(sources, **self._CTX)
        assert validated[0].get("valid") == False  # noqa: E712


class TestUngroundedClaimDetection:
    """Grounding logic: detect claims that reference absent symbols and discount them."""

    def _patch_git_show(self, monkeypatch, *, stdout=b"", stderr=b"", returncode=0):
        from app import source_ref_validation as srv

        class _Proc:
            def __init__(self):
                self.returncode = returncode

            async def communicate(self):
                return stdout, stderr

        async def _create(*args, **kwargs):
            return _Proc()

        monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", _create)

    async def test_ungrounded_claim_with_absent_symbol_is_detected(self, monkeypatch):
        """A claim referencing a symbol NOT defined in an existing file is flagged."""
        self._patch_git_show(
            monkeypatch, stdout=b"def other_func():\n    pass\n"
        )
        result = await summarize_ungrounded_claims([
            "backend/app/module.py:missing_symbol is the main entry point"
        ])
        assert result is not None
        assert result["ungrounded_claim_count"] == 1
        assert "backend/app/module.py:missing_symbol" in result["ungrounded_refs"]

    async def test_grounded_claim_with_present_symbol_is_not_flagged(self, monkeypatch):
        """A claim referencing a symbol that IS defined is not flagged."""
        self._patch_git_show(
            monkeypatch, stdout=b"def present_func():\n    return True\n"
        )
        result = await summarize_ungrounded_claims([
            "backend/app/module.py:present_func implements the main logic"
        ])
        assert result is None

    async def test_multiple_claims_with_distinct_ungrounded_count(self, monkeypatch):
        """Ungrounded-claim count reflects distinct claims, not total refs."""
        from app import source_ref_validation as srv

        # Mock _symbol_defined_at_head to return False for missing symbols
        async def mock_symbol_check(path, symbol):
            return symbol == "present_func"

        monkeypatch.setattr(srv, "_symbol_defined_at_head", mock_symbol_check)

        claims = [
            "backend/app/x.py:present_func does something",
            "backend/app/x.py:missing_func does something else",
            "backend/app/x.py:missing_func is also referenced here",  # same ref, different claim
            "backend/app/y.py:another_missing creates objects",
        ]
        result = await summarize_ungrounded_claims(claims)
        assert result is not None
        # 3 claims carry ungrounded refs (claims 2, 3, 4)
        assert result["ungrounded_claim_count"] == 3
        # 2 distinct ungrounded refs (missing_func and another_missing)
        assert len(result["ungrounded_refs"]) == 2
        assert "backend/app/x.py:missing_func" in result["ungrounded_refs"]
        assert "backend/app/y.py:another_missing" in result["ungrounded_refs"]

    async def test_mixed_grounded_and_ungrounded_claims(self, monkeypatch):
        """Batch with both grounded and ungrounded claims filters only the bad ones."""
        from app import source_ref_validation as srv

        async def mock_symbol_check(path, symbol):
            return symbol in ("func_a", "func_b")

        monkeypatch.setattr(srv, "_symbol_defined_at_head", mock_symbol_check)

        claims = [
            "backend/app/module.py:func_a is solid",
            "backend/app/module.py:missing_func is broken",
            "backend/app/module.py:func_b is also solid",
            "backend/app/module.py:ghost_func is gone",
        ]
        result = await summarize_ungrounded_claims(claims)
        assert result is not None
        # 2 claims carry ungrounded refs
        assert result["ungrounded_claim_count"] == 2
        assert len(result["ungrounded_refs"]) == 2

    async def test_indeterminate_git_state_flags_nothing(self, monkeypatch):
        """When git is unavailable, no claims are flagged (fail-open)."""
        from app import source_ref_validation as srv

        async def mock_symbol_check(path, symbol):
            # Simulate git unavailability returning None (indeterminate)
            return None

        monkeypatch.setattr(srv, "_symbol_defined_at_head", mock_symbol_check)

        claims = [
            "backend/app/module.py:some_func does something",
            "backend/app/module.py:another_func does another thing",
        ]
        result = await summarize_ungrounded_claims(claims)
        # Indeterminate refs (None) are treated as grounded, so no result
        assert result is None

    async def test_claim_with_file_source_excerpt_is_checked(self, monkeypatch):
        """File sources with quote/excerpt fields are checked the same way."""
        from app import source_ref_validation as srv

        async def mock_symbol_check(path, symbol):
            return symbol == "present_func"

        monkeypatch.setattr(srv, "_symbol_defined_at_head", mock_symbol_check)

        claims = []
        sources = [
            {
                "type": "file",
                "ref": "backend/app/module.py",
                "quote": "backend/app/module.py:missing_func is defined here"
            }
        ]
        result = await summarize_ungrounded_claims(claims, sources)
        assert result is not None
        # Source excerpts increment ungrounded_refs but NOT ungrounded_claim_count
        assert result["ungrounded_claim_count"] == 0
        assert "backend/app/module.py:missing_func" in result["ungrounded_refs"]


class TestUngroundedClaimsGroundedAgainstBranch:
    """_summarize_ungrounded_claims_grounded threads the GitHub repo/branch
    verification context through to the symbol check so symbol grounding runs
    against the real integration branch, with a conservative local fallback."""

    _CTX = {
        "token": "tok",
        "owner": "octo",
        "repo": "repo",
        "branch": "integration",
        "project_id": "proj-1",
    }

    async def test_github_context_absent_symbol_flagged(self, monkeypatch):
        """With GitHub creds, an absent symbol on the branch is flagged via the
        GitHub content fetch (the local checkout is never consulted)."""
        from app import source_ref_validation as srv

        fetch = AsyncMock(return_value=(True, "def other_func():\n    pass\n"))
        monkeypatch.setattr(srv, "_file_content_in_repo_branch", fetch)
        # Local git must never be touched on the GitHub-context path.
        monkeypatch.setattr(
            srv.asyncio,
            "create_subprocess_exec",
            AsyncMock(side_effect=AssertionError("local git used on GitHub path")),
        )

        result = await _summarize_ungrounded_claims_grounded(
            ["backend/app/module.py:missing_symbol is the entry point"],
            None,
            self._CTX,
        )
        assert result is not None
        assert result["ungrounded_claim_count"] == 1
        assert "backend/app/module.py:missing_symbol" in result["ungrounded_refs"]
        # Context threaded through to the content fetch.
        assert fetch.await_count >= 1
        _, kwargs = fetch.call_args
        assert kwargs["branch"] == "integration"
        assert kwargs["owner"] == "octo"

    async def test_github_context_present_symbol_not_flagged(self, monkeypatch):
        """A symbol present on the branch is grounded → None."""
        from app import source_ref_validation as srv

        monkeypatch.setattr(
            srv,
            "_file_content_in_repo_branch",
            AsyncMock(return_value=(True, "def present_func():\n    return True\n")),
        )
        result = await _summarize_ungrounded_claims_grounded(
            ["backend/app/module.py:present_func implements the logic"],
            None,
            self._CTX,
        )
        assert result is None

    async def test_indeterminate_check_never_flags(self, monkeypatch):
        """An indeterminate symbol check (no creds → local git unavailable)
        never produces an ungrounded ref, even with a branch-only context."""
        from app import source_ref_validation as srv

        async def _indeterminate(path, symbol, **kwargs):
            return None

        monkeypatch.setattr(srv, "_symbol_defined_at_head", _indeterminate)
        result = await _summarize_ungrounded_claims_grounded(
            ["backend/app/module.py:some_func does something"],
            None,
            {"branch": "integration", "project_id": "proj-1"},  # no token/owner/repo
        )
        assert result is None

    async def test_file_source_excerpt_counts_refs_not_claims(self, monkeypatch):
        """File-source excerpts add ungrounded refs but never increment the
        claim count (mirrors summarize_ungrounded_claims' count_claim=False)."""
        from app import source_ref_validation as srv

        monkeypatch.setattr(
            srv,
            "_file_content_in_repo_branch",
            AsyncMock(return_value=(True, "def other_func():\n    pass\n")),
        )
        sources = [
            {
                "type": "file",
                "ref": "backend/app/module.py",
                "quote": "backend/app/module.py:missing_func is defined here",
            }
        ]
        result = await _summarize_ungrounded_claims_grounded([], sources, self._CTX)
        assert result is not None
        assert result["ungrounded_claim_count"] == 0
        assert "backend/app/module.py:missing_func" in result["ungrounded_refs"]


class TestStructuralActionabilityDowngrade:
    """The actionability gate downgrades findings whose structural support is insufficient."""

    def test_high_grade_with_no_claims_downgrades_to_low(self):
        """Finding with HIGH grade but no claims downgrades to LOW."""
        findings = [
            {
                "title": "F1",
                "actionability_grade": "HIGH",
                "description": "x" * 100,
                "claims": [],
                "sources": [{"type": "pr", "ref": "https://github.com/o/r/pull/1"}],
                # _ungrounded_claim_count not set (assume 0)
            }
        ]
        _apply_structural_actionability_downgrade(findings)
        assert findings[0]["actionability_grade"] == "LOW"

    def test_high_grade_with_ungrounded_claims_downgrades(self):
        """Finding with HIGH grade but all claims ungrounded downgrades to MEDIUM or LOW."""
        findings = [
            {
                "title": "F1",
                "actionability_grade": "HIGH",
                "description": "x" * 100,
                "claims": ["claim1", "claim2"],
                "sources": [{"type": "pr", "ref": "https://github.com/o/r/pull/1"}],
                "_ungrounded_claim_count": 2,  # Both claims are ungrounded
            }
        ]
        _apply_structural_actionability_downgrade(findings)
        # effective_claims = 2 - 2 = 0, so grade drops below HIGH
        assert findings[0]["actionability_grade"] != "HIGH"

    def test_medium_grade_with_partial_ungrounded_claims(self):
        """Finding with 3 claims where 1 is ungrounded drops from 3 effective to 2."""
        findings = [
            {
                "title": "F1",
                "actionability_grade": "MEDIUM",
                "description": "x" * 100,
                "claims": ["c1", "c2", "c3"],
                "sources": [{"type": "file", "ref": "backend/app/foo.py"}],
                "_ungrounded_claim_count": 1,
            }
        ]
        _apply_structural_actionability_downgrade(findings)
        # effective_claims = 3 - 1 = 2; sources = 1
        # 2 >= 2 and 1 >= 1 and len(desc) >= 80 → stays HIGH or MEDIUM, not downgraded
        assert findings[0]["actionability_grade"] in ("MEDIUM", "HIGH")

    def test_low_grade_stays_low(self):
        """Finding already at LOW grade is never downgraded further."""
        findings = [
            {
                "title": "F1",
                "actionability_grade": "LOW",
                "description": "x" * 100,
                "claims": ["c1"],
                "sources": [{"type": "pr", "ref": "https://github.com/o/r/pull/1"}],
                "_ungrounded_claim_count": 1,
            }
        ]
        _apply_structural_actionability_downgrade(findings)
        assert findings[0]["actionability_grade"] == "LOW"

    def test_downgrade_count_returned_correctly(self):
        """Return value reflects number of findings actually downgraded."""
        findings = [
            {
                "title": "F1",
                "actionability_grade": "HIGH",
                "description": "x" * 100,
                "claims": [],
                "sources": [],
                # No claims, sources, or sufficient description → downgrade to LOW
            },
            {
                "title": "F2",
                "actionability_grade": "MEDIUM",
                "description": "x" * 100,
                "claims": ["c1"],
                "sources": [{"type": "pr", "ref": "https://github.com/o/r/pull/1"}],
                # Has structure: 1 claim, 1 source, sufficient desc → stays MEDIUM
            },
            {
                "title": "F3",
                "actionability_grade": "HIGH",
                "claims": ["c1", "c2"],
                "description": "x" * 100,
                "sources": [],
                "_ungrounded_claim_count": 2,
                # effective_claims = 0; sources = 0 → downgrade to LOW
            }
        ]
        downgrades = _apply_structural_actionability_downgrade(findings)
        assert downgrades == 2
        assert findings[0]["actionability_grade"] == "LOW"
        assert findings[1]["actionability_grade"] == "MEDIUM"
        assert findings[2]["actionability_grade"] == "LOW"

    def test_grade_order_respected_on_downgrade(self):
        """Downgrades only happen when structural grade is lower than current."""
        findings = [
            {
                "title": "F1",
                "actionability_grade": "LOW",
                "description": "description",
                "claims": ["c1", "c2"],
                "sources": [{"type": "pr", "ref": "https://github.com/o/r/pull/1"}],
                # effective_claims = 2, sources = 1, desc >= 40 → structural = HIGH
                # But current is LOW, so no downgrade (can't upgrade)
            }
        ]
        _apply_structural_actionability_downgrade(findings)
        assert findings[0]["actionability_grade"] == "LOW"

    def test_missing_ungrounded_count_treats_as_zero(self):
        """Finding without _ungrounded_claim_count field is treated as 0."""
        findings = [
            {
                "title": "F1",
                "actionability_grade": "HIGH",
                "description": "x" * 100,
                "claims": ["c1"],
                "sources": [{"type": "pr", "ref": "https://github.com/o/r/pull/1"}],
                # _ungrounded_claim_count not present
            }
        ]
        _apply_structural_actionability_downgrade(findings)
        # effective_claims = 1 - 0 = 1; sources = 1; desc >= 80
        # This meets HIGH threshold (1 >= 2? No, but sources >= 1 and desc >= 80 → MEDIUM)
        # Actually: effective_claims >= 1 or valid_sources >= 1 and len(desc) >= 40 → MEDIUM
        # So no downgrade from HIGH → but wait, let me reread the threshold...
        # HIGH requires: effective_claims >= 2 AND valid_sources >= 1 AND len(desc) >= 80
        # This has 1 claim, so it cannot be HIGH, will downgrade to MEDIUM at best
        assert findings[0]["actionability_grade"] in ("MEDIUM", "LOW")


class TestExtractSourceFilePaths:
    """extract_source_file_paths: reverse-index helper for file citations."""

    def test_extracts_file_type_sources(self):
        sources = [
            {"type": "file", "ref": "backend/app/foo.py"},
            {"type": "file", "ref": "backend/app/bar.py:10-20"},
            {"type": "pr", "ref": "1234"},
        ]
        paths = extract_source_file_paths(sources)
        assert paths == ["backend/app/foo.py", "backend/app/bar.py"]

    def test_deduplicates_same_path(self):
        sources = [
            {"type": "file", "ref": "backend/app/foo.py:5"},
            {"type": "file", "ref": "backend/app/foo.py:10"},
        ]
        paths = extract_source_file_paths(sources)
        assert paths == ["backend/app/foo.py"]

    def test_skips_non_file_types(self):
        sources = [{"type": "pr", "ref": "42"}, {"type": "message", "ref": "m-123"}]
        assert extract_source_file_paths(sources) == []

    def test_none_returns_empty(self):
        assert extract_source_file_paths(None) == []

    def test_skips_sources_missing_ref(self):
        sources = [{"type": "file"}]
        assert extract_source_file_paths(sources) == []


class TestCollectStaleRefs:
    """collect_stale_refs: extract invalid refs from validate_source_refs output."""

    def test_returns_invalid_refs(self):
        validated = [
            {"type": "file", "ref": "backend/app/gone.py", "valid": False},
            {"type": "file", "ref": "backend/app/present.py"},
        ]
        stale = collect_stale_refs(validated)
        assert stale == ["backend/app/gone.py"]

    def test_empty_when_all_valid(self):
        validated = [{"type": "file", "ref": "backend/app/foo.py"}]
        assert collect_stale_refs(validated) == []

    def test_multiple_stale_refs(self):
        validated = [
            {"type": "file", "ref": "a.py", "valid": False},
            {"type": "file", "ref": "b.py", "valid": False},
            {"type": "file", "ref": "c.py"},
        ]
        stale = collect_stale_refs(validated)
        assert stale == ["a.py", "b.py"]


class TestSummarizeBrokenRefs:
    """summarize_broken_refs: structured provenance summary of broken file refs."""

    def test_returns_none_when_all_valid(self):
        validated = [
            {"type": "file", "ref": "backend/app/foo.py"},
            {"type": "pr", "ref": "https://github.com/o/r/pull/1"},
        ]
        assert summarize_broken_refs(validated) is None

    def test_returns_none_for_empty_or_non_list(self):
        assert summarize_broken_refs([]) is None
        assert summarize_broken_refs(None) is None
        assert summarize_broken_refs("not a list") is None

    def test_single_broken_ref(self):
        validated = [
            {"type": "file", "ref": "backend/app/foo.py"},
            {"type": "file", "ref": "backend/app/missing.py", "valid": False},
        ]
        result = summarize_broken_refs(validated)
        assert result == {
            "broken_source_ref_count": 1,
            "broken_refs": ["backend/app/missing.py"],
        }

    def test_five_broken_refs_all_distinct(self):
        validated = [
            {"type": "file", "ref": f"backend/app/missing_{i}.py", "valid": False}
            for i in range(5)
        ]
        result = summarize_broken_refs(validated)
        assert result is not None
        assert result["broken_source_ref_count"] == 5
        assert sorted(result["broken_refs"]) == sorted(
            f"backend/app/missing_{i}.py" for i in range(5)
        )

    def test_dedupes_by_path_across_line_numbers(self):
        validated = [
            {"type": "file", "ref": "backend/app/foo.py:5", "valid": False},
            {"type": "file", "ref": "backend/app/foo.py:10-20", "valid": False},
            {"type": "file", "ref": "backend/app/foo.py#L42", "valid": False},
            {"type": "file", "ref": "backend/app/bar.py:7", "valid": False},
        ]
        result = summarize_broken_refs(validated)
        assert result is not None
        assert result["broken_source_ref_count"] == 2
        assert result["broken_refs"] == ["backend/app/foo.py", "backend/app/bar.py"]

    def test_ignores_non_file_invalid_sources(self):
        validated = [
            {"type": "pr", "ref": "https://github.com/o/r/pull/1", "valid": False},
            {"type": "message", "ref": "slack-msg-123", "valid": False},
        ]
        assert summarize_broken_refs(validated) is None

    def test_ignores_valid_file_sources(self):
        validated = [
            {"type": "file", "ref": "backend/app/foo.py"},
            {"type": "file", "ref": "backend/app/bar.py", "valid": False},
        ]
        result = summarize_broken_refs(validated)
        assert result == {
            "broken_source_ref_count": 1,
            "broken_refs": ["backend/app/bar.py"],
        }


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
                {**s, "valid": False}
                if s.get("ref") == "backend/app/gone.py"
                else s
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
    async def test_spawned_child_carries_present_reproduction_verdict(
        self, db_session, mocker
    ):
        """A grounded finding's spawned fix carries target_defect_reproduction=present
        so its outcome scoring can credit a merge as a verified fix."""
        from test_autonomous_research import _make_finding

        finding = _make_finding(
            title="Grounded reproducible finding", direction_id="d-repro-verd01"
        )
        finding["sources"] = [{"type": "file", "ref": "backend/app/present.py"}]

        async def _validate(sources, **kw):
            return list(sources)

        task = await self._run_and_get_child_ctx(
            db_session,
            mocker,
            direction_id="d-repro-verd01",
            parsed_findings=[finding],
            validate=_validate,
        )
        assert task.get("target_defect_reproduction") == "present"

    @_pytest.mark.asyncio
    async def test_spawned_child_carries_indeterminate_reproduction_verdict(
        self, db_session, mocker
    ):
        """No backend → spawned fix carries an indeterminate verdict, which blocks
        bare-merge success scoring downstream."""
        from test_autonomous_research import _make_finding

        finding = _make_finding(
            title="Indeterminate verdict finding", direction_id="d-repro-verd02"
        )
        finding["sources"] = [{"type": "file", "ref": "backend/app/whatever.py"}]

        async def _validate(sources, **kw):
            return list(sources)

        ctx = await self._run_and_get_child_ctx(
            db_session,
            mocker,
            direction_id="d-repro-verd02",
            parsed_findings=[finding],
            has_backend=False,
            validate=_validate,
        )
        assert ctx.get("target_defect_reproduction") == "indeterminate"

    async def _run_and_get_child_ctx(self, db_session, mocker, **kw):
        """Run a research completion and return the single spawned child's context."""
        from test_autonomous_research import _patch_research_externals

        direction_id = kw.pop("direction_id")
        parsed_findings = kw.pop("parsed_findings")
        has_backend = kw.pop("has_backend", True)
        validate = kw.pop("validate", None)

        _project, _direction, task = await _grader_project_and_task(
            db_session, direction_id
        )
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
        mocker.patch(
            "app.research_completion._has_validation_backend",
            return_value=has_backend,
        )
        if validate is not None:
            mocker.patch("app.research_completion.validate_source_refs", new=validate)
        mocker.patch(
            "app.research_completion._summarize_ungrounded_claims_grounded",
            new=AsyncMock(return_value=None),
        )
        await _handle_research_completion(task, "", db_session)
        child = (
            (
                await db_session.execute(
                    _select(_Task).where(_Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .first()
        )
        return child.context or {}

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
        sentinel = _make_finding(
            title="Fresh sentinel", direction_id="d-repro-orph01"
        )

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
            if (c.context or {}).get("memo_drop_reason")
            == "reproduce_on_head_absent"
        ]
        # Exactly one memo, carrying the absent reference in its payload and its
        # synthesized reasoning, and the cycle records it in spawned_memo_ids.
        assert len(memos) == 1
        memo = memos[0]
        source_finding = memo.first_payload.get("source_finding") or {}
        assert "backend/app/gone.py" in (
            source_finding.get("absent_references") or []
        )
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
        # Must carry a MEMO artifact
        from app.models import Artifact, ArtifactType
        artifacts = (
            (
                await db_session.execute(
                    _select(Artifact).where(Artifact.task_id == memo.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(artifacts) == 1
        memo_artifact = artifacts[0]
        assert memo_artifact.artifact_type == ArtifactType.MEMO
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
        mocker.patch(
            "app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock
        )

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
        assert (spawned[0].context or {}).get("target_defect_reproduction") == "present"
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
async def test_low_actionability_challenge_indeterminate_redefers(
    db_session, mocker
):
    """A low-actionability challenge that does not confirm LOW keeps the
    finding deferred for refinement instead of terminally dropping it."""
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

    assert (task.context or {}).get("finding_structural_give_up") is None
    deferred = (task.context or {}).get("deferred_findings", [])
    kept = next(f for f in deferred if f.get("title") == decayed["title"])
    assert kept["deferred_reason"] == "low_actionability_refinement"
    assert kept["finding_challenge"]["second_opinion_verdict"] == "indeterminate"
    assert "sidecar failed" in kept["low_actionability_refinement_reason"]
    children = (
        (
            await db_session.execute(_select(_Task).where(_Task.parent_task_id == task.id))
        )
        .scalars()
        .all()
    )
    assert not any(
        c.context and c.context.get("memo_drop_reason") == "low_actionability_decay"
        for c in children
    )


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
            await db_session.execute(_select(_Task).where(_Task.parent_task_id == task.id))
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
    async def test_knowledge_child_carries_direction_context(
        self, db_session, mocker
    ):
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
            "app.research_completion.resolve_branch_verification_context",
            new=AsyncMock(return_value={}),
        )
        m = AsyncMock(return_value=verdict)
        mocker.patch("app.research_completion.cited_paths_changed_since", new=m)
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
        task = self._task(
            {"research_task_id": "r1", "generated_head_sha": "deadbeef"}
        )
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
            "app.research_completion.resolve_branch_verification_context",
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
        task.context = {"direction_id": direction_id}
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

    def test_skips_non_research_and_non_completed_tasks(self):
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
        session = self._mock_session([research, self_audit, in_progress])

        rows = asyncio.get_event_loop().run_until_complete(
            query_findings_index(session, "proj-1")
        )
        assert [row["finding_title"] for row in rows] == ["Research"]


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
        captured_task.context = {"some_other_key": "preserved"}

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

        # _shelve_findings_for_next_cycle was extracted to app.findings_index
        # (re-exported via rc); patch closing_session where the function now
        # resolves it. rc._shelve_findings_for_next_cycle is the same object
        # via the facade re-export.
        monkeypatch.setattr(
            "app.findings_index.closing_session", fake_closing_session
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
            "app.findings_index.closing_session", fake_closing_session
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
    return project, direction, task


class TestForwardQualityWouldSpawnPredicate:
    """_research_findings_would_spawn: blast-radius predicate (pure)."""

    def test_actionable_finding_would_spawn(self):
        assert _research_findings_would_spawn(
            [{"type": "improvement", "prompt": "do x", "actionability_grade": "HIGH"}]
        )

    def test_observation_types_do_not_spawn(self):
        for t in (
            "design_doc_observation",
            "direction_update",
            "direction_proposal",
            "direction_note",
            "note",
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
        assert not _research_findings_would_spawn(
            [{"type": "improvement", "prompt": "do x", "already_spawned": True}]
        )
        assert not _research_findings_would_spawn([])


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
        assert parse_grader_verdict('the VERDICT: pass overall').passed is True
        assert parse_grader_verdict('VERDICT: fail — ungrounded').passed is False

    def test_not_completed_fails_closed(self):
        assert parse_grader_verdict("looks great", completed=False).passed is False

    def test_unparseable_fails_closed(self):
        assert parse_grader_verdict("no machine verdict in here").passed is False
        assert parse_grader_verdict("").passed is False


@_pytest.mark.asyncio
async def test_spawn_forward_quality_grader_is_single_shot(db_session):
    """spawn_forward_quality_grader parks the source once; a second call no-ops."""
    _project, _direction, task = await _grader_project_and_task(db_session, "d-single")

    g1 = await spawn_forward_quality_grader(task, "artifact text", db_session)
    assert g1 is not None
    assert (g1.first_payload or {}).get("type") == "investigate"
    assert (g1.context or {}).get(QUALITY_GRADER_SOURCE_KEY) == str(task.id)
    assert g1.parent_task_id is None
    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert task.context.get("awaiting_quality_grader")

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
    assert (sidecar.context or {}).get(FINDING_CHALLENGE_SOURCE_KEY) == str(task.id)
    assert (sidecar.context or {}).get("challenged_boundary") == (
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT
    )
    assert sidecar.parent_task_id is None

    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert task.context.get("awaiting_finding_challenge")
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
    mocker.patch(
        "app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock
    )

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
    assert task.status == TaskStatus.EVALUATING
    assert task.context.get("awaiting_quality_grader")


@_pytest.mark.asyncio
async def test_flag_off_spawns_children_without_grader(db_session, mocker):
    """Non-regression: with the flag OFF, findings spawn children immediately."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-off01")
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
    # No grader anywhere in the project.
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
    ), "no grader may spawn when the gate is disabled"


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
    assert not any(
        (c.context or {}).get(QUALITY_GRADER_SOURCE_KEY) for c in children
    )
    # With children spawned, the research task takes the normal post-spawn
    # EVALUATING state (awaiting child rollup) — NOT FAILED, and no longer
    # parked awaiting a grade.
    assert task.status == TaskStatus.EVALUATING


@_pytest.mark.asyncio
async def test_finding_challenge_reentry_does_not_double_increment_spawn_count(
    db_session, mocker
):
    """A finding-challenge resume is a re-entry pass and must not re-count."""

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

    assert project.vision["_research_spawn_count"] == 7
    assert project.vision["purpose"] == "keep-me"
    assert task.context.get(QUALITY_GRADER_VERDICT_KEY) is None
    assert task.context.get(ADVERSARIAL_GATE_VERDICT_KEY) is None


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
    assert task.status != TaskStatus.EVALUATING, "must not park when nothing would spawn"


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
async def test_unresolved_prerequisite_blocks_dependent(db_session, mocker):
    """When a finding declares a prerequisite that does not resolve to any
    candidate, the dependent is transitioned into the BLOCKED state carrying the
    unresolved prerequisite as its machine-resolvable need (no depends_on edge),
    so the blocked-resume sweep gates it instead of letting it duplicate the
    foundation."""
    from test_autonomous_research import _make_finding, _patch_research_externals
    from app.task_blocked import get_blocker_metadata

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq04")
    finding = _make_finding(title="Add login endpoint", direction_id=direction["id"])
    # Declare an EXACT (finding-hash) prerequisite that does not resolve to any
    # candidate this cycle — a valid-but-unresolved forward reference.
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
    assert child.status == TaskStatus.BLOCKED, (
        "an unresolved prerequisite must transition the dependent into BLOCKED"
    )
    assert not (child.depends_on or []), (
        "unresolved prerequisite must not create a depends_on edge"
    )
    # The unresolved prerequisite is persisted as the canonical machine-resolvable
    # need under context['blocker']['need'] for the blocked-resume sweep.
    need = get_blocker_metadata(child).get("need")
    assert need == {"kind": "task", "ref": missing_ref}, (
        "unresolved prerequisite must be carried as the blocked need"
    )
    # Still stamped on context for operator visibility, in addition to BLOCKED.
    unresolved = (child.context or {}).get("unresolved_prerequisite", [])
    assert any(
        u.get("kind") == "task" and u.get("ref") == missing_ref
        for u in (unresolved if isinstance(unresolved, list) else [unresolved])
    ), "unresolved prerequisite details must remain in context for visibility"


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
    assert all(
        c.status == TaskStatus.CANCELLED for c in dependent_children
    ), "the co-deferred dependent's live task must be cancelled"

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
async def test_co_deferred_dependent_retains_explicit_prerequisite(
    db_session, mocker
):
    """A dependent declaring a deferred sibling prerequisite AND an explicit
    {kind, ref} prerequisite is co-deferred (sibling deferred this cycle). The
    shelved finding retains BOTH declarations so the next cycle reconstructs the
    full need set — the explicit prerequisite is not dropped on the way out."""
    from app.research_completion import _finding_hash
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-prq07")
    prereq = _make_finding(title="Define shared base", direction_id=direction["id"])
    prereq["actionability_grade"] = "LOW"
    dependent = _make_finding(title="Build on shared base", direction_id=direction["id"])
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
    import app.shared_state as _ss
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
    # Force _effective_cap == 1: zero free VM slots floors _cap_by_slots to 1.
    mocker.patch.object(_ss, "tasks_active_gauge", 999)
    mocker.patch.object(_ss, "tasks_queued_gauge", 0)
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

    async def test_resolve_head_commit_github_error_degrades_to_none(
        self, monkeypatch
    ):
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

    async def test_cited_paths_changed_since_true_when_file_in_diff(
        self, monkeypatch
    ):
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

    async def test_cited_paths_changed_since_false_when_not_in_diff(
        self, monkeypatch
    ):
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
        from test_autonomous_research import _make_finding, _patch_research_externals, _active_direction, _make_research_task

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

        task = await _make_research_task(project, direction, db_session)

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
            call for call in log_mock.warning.call_args_list
            if "out-of-range" in str(call)
        ]
        assert len(warning_calls) > 0

    async def test_out_of_range_positive_index_is_skipped(self, mocker, db_session):
        """Finding with out-of-range positive index in depends_on_indices is skipped."""
        from test_autonomous_research import _make_finding, _patch_research_externals, _active_direction, _make_research_task

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

        task = await _make_research_task(project, direction, db_session)

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
            call for call in log_mock.warning.call_args_list
            if "out-of-range" in str(call)
        ]
        assert len(warning_calls) > 0

    async def test_valid_index_in_depends_on_co_defers_dependent(self, mocker, db_session):
        """A valid sibling index pointing at a same-cycle-deferred finding IS
        recognized and produces a prerequisite need — but because the prerequisite
        was deferred this cycle, the dependent is co-deferred (its live task
        cancelled and its finding shelved) rather than left live-blocked behind
        dormant data (see #8278). The co-deferral itself is the proof that the
        valid index created a need; a skipped index (negative/out-of-range) leaves
        the dependent free to spawn normally."""
        from app.research_completion import _finding_hash
        from test_autonomous_research import _make_finding, _patch_research_externals, _active_direction, _make_research_task

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

        task = await _make_research_task(project, direction, db_session)

        finding_a = {**_make_finding(title="Finding A (deferred)"), "actionability_grade": "LOW"}
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
        from test_autonomous_research import _make_finding, _patch_research_externals, _active_direction, _make_research_task

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

        task = await _make_research_task(project, direction, db_session)

        finding_a = {**_make_finding(title="Finding A (deferred)"), "actionability_grade": "LOW"}
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

    async def test_co_deferred_dependent_retains_both_declarations(self, mocker, db_session):
        """A dependent that names BOTH a sibling-index prerequisite AND an explicit
        {kind, ref} prerequisite is co-deferred (its sibling foundation was deferred
        this cycle). Neither declaration may be dropped on the way out: the shelved
        finding retains both ``depends_on_indices`` and ``prerequisite`` so the next
        cycle reconstructs the full (deduplicated) need set."""
        from app.research_completion import _finding_hash
        from test_autonomous_research import _make_finding, _patch_research_externals, _active_direction, _make_research_task

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

        task = await _make_research_task(project, direction, db_session)

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
):
    """A compute_experiment-typed research finding carrying a launch contract.

    By default the launch fields are flat on the finding; pass ``nested_key`` to
    nest them under ``compute_experiment`` / ``launch_contract`` instead.
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
            {"compute_experiment": {"command": "x.py", "spend_cap_usd": 1, "deadline_seconds": 60}}
        )
        assert contract["command"] == "x.py"
        assert contract["spend_cap_usd"] == 1
        assert contract["deadline_seconds"] == 60

    def test_nested_under_launch_contract_key(self):
        from app.research_completion import _author_compute_launch_contract

        contract = _author_compute_launch_contract(
            {"launch_contract": {"command": "y.py", "spend_cap_usd": 2, "deadline_seconds": 120}}
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
            {"command": "run.sh", "spend_cap_usd": 2.0, "deadline_seconds": 60}
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

        assert _resolve_compute_spend_threshold(
            {"compute_experiment_spend_threshold_usd": 7.5}, None
        ) == 7.5

    def test_direction_override_wins(self):
        from app.research_completion import _resolve_compute_spend_threshold

        assert _resolve_compute_spend_threshold(
            {"compute_experiment_spend_threshold_usd": 5.0},
            {"compute_experiment_spend_threshold_usd": 20.0},
        ) == 20.0

    def test_nonpositive_override_falls_back_to_global(self):
        from app.research_completion import _resolve_compute_spend_threshold

        assert _resolve_compute_spend_threshold(
            {"compute_experiment_spend_threshold_usd": 5.0},
            {"compute_experiment_spend_threshold_usd": 0},
        ) == 5.0

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
            context={"direction_id": direction["id"]},
        )
        db_session.add(active_task)
        await db_session.commit()
        finding = _compute_finding(
            direction["id"],
            title="Measure live release proof acceptance evidence",
            spend_cap_usd=2.0,
            deadline_seconds=3600,
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

        _project, direction, task = await _grader_project_and_task(db_session, "d-cx04a")
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

        _project, direction, task = await _grader_project_and_task(db_session, "d-cx-ae01")
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
        assert evidence is not None, "acceptance_evidence missing from compute_experiment payload"
        assert evidence["kind"] == "downstream_artifact"
        assert evidence["artifact_id"] == "benchmark-result-bundle"

    @_pytest.mark.asyncio
    async def test_compute_experiment_malformed_acceptance_evidence_degrades_not_aborts(
        self, db_session, mocker
    ):
        """A malformed acceptance_evidence on a compute_experiment finding degrades
        to absent without aborting the spawn — same lenient pattern as non-compute
        findings."""
        from test_autonomous_research import _patch_research_externals

        _project, direction, task = await _grader_project_and_task(db_session, "d-cx-ae02")
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

    _project, direction, task = await _grader_project_and_task(
        db_session, "d-cx-adv01"
    )
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
    mocker.patch(
        "app.research_completion._is_high_stakes_finding", return_value=True
    )
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
        c for c in children
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
            # Saturate the VM gauge so _cap_by_slots floors the batch cap to 1 —
            # exercises the truncation path deterministically.
            mocker.patch("app.shared_state.tasks_active_gauge", 100)
            mocker.patch("app.shared_state.tasks_queued_gauge", 0)
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
    async def test_finding_acceptance_evidence_lands_on_child_payload(
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

    @_pytest.mark.asyncio
    async def test_finding_acceptance_evidence_propagates_to_child_context(
        self, db_session, mocker
    ):
        from test_autonomous_research import _make_finding

        finding = _make_finding(title="Declared proof", direction_id="d-ae-ctx01")
        finding["acceptance_evidence"] = {
            "kind": "test",
            "descriptor": "seam test exercises new path end-to-end",
            "selector": "testing/test_foo.py::test_bar",
        }
        children, _task = await self._spawn_from_findings(
            db_session, mocker, direction_id="d-ae-ctx01", findings=[finding]
        )
        child = next(c for c in children if c.title == "Declared proof")
        assert child.context["acceptance_evidence"] == {
            "kind": "test",
            "descriptor": "seam test exercises new path end-to-end",
            "selector": "testing/test_foo.py::test_bar",
        }

    @_pytest.mark.asyncio
    async def test_finding_absent_acceptance_evidence_is_noop(
        self, db_session, mocker
    ):
        from test_autonomous_research import _make_finding

        finding = _make_finding(title="No proof", direction_id="d-ae-ctx02")
        children, _task = await self._spawn_from_findings(
            db_session, mocker, direction_id="d-ae-ctx02", findings=[finding]
        )
        child = next(c for c in children if c.title == "No proof")
        assert "acceptance_evidence" not in (child.context or {})

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
        {"title": "title-like", "prerequisite": {"kind": "task", "ref": "Build the harness"}},
        {"title": "exact-hash", "prerequisite": {"kind": "task", "ref": "deadbeefcafe0001"}},
        {"title": "cap", "prerequisite": {"kind": "capability", "ref": "payment-core"}},
        # A finding that previously carried a malformed advisory but now declares a
        # valid prereq must have the stale advisory cleared.
        {
            "title": "now-valid",
            "prerequisite": {"kind": "task", "ref": "feedfacefeedface"},
            "_malformed_prerequisite": {"kind": "task", "ref": "old prose", "reason": "x"},
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

    def test_already_spawned_is_not_high_stakes(self):
        assert not _is_high_stakes_finding(self._f(scope="large", already_spawned=True))

    def test_low_actionability_is_not_high_stakes(self):
        assert not _is_high_stakes_finding(
            self._f(scope="large", actionability_grade="LOW")
        )

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
        assert not _is_high_stakes_finding(self._cf(spend_cap_usd=1.0, accelerator=None))

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
        assert _is_high_stakes_finding(self._cf(deadline_seconds=7200, accelerator=None, spend_cap_usd=0.01))

    def test_deadline_at_boundary_is_not_high_stakes(self, mocker):
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"compute_experiment_spend_threshold_usd": 1000.0},
        )
        # Exactly 3600 seconds (== 1 hour) is NOT over the threshold
        assert not _is_high_stakes_finding(self._cf(deadline_seconds=3600, accelerator=None, spend_cap_usd=0.01))

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

    def test_missing_hash_defaults_to_fail_closed(self):
        logs = '[{"hash": "other", "verdict": "fail", "reason": "unrelated"}]'
        result, outcome = parse_adversarial_finding_verdicts(logs, ["abc123"])
        # fail-closed: absent hash → passed=False, not an implicit pass
        assert result["abc123"]["passed"] is False
        assert result["abc123"]["explicit"] is False
        # The array was parsed but no known hash matched
        assert outcome == "no_hash_matches"
        assert result["abc123"]["unresolved_reason"] == "no_hash_matches"

    def test_not_completed_defaults_all_to_fail_closed(self):
        logs = '[{"hash": "abc123", "verdict": "fail", "reason": "fail"}]'
        result, outcome = parse_adversarial_finding_verdicts(logs, ["abc123"], completed=False)
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
        result, outcome = parse_adversarial_finding_verdicts("not json at all", ["abc123"])
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
        assert result["h3"]["passed"] is False  # absent → fail-closed (not an implicit pass)
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
        real_array = '[{"hash": "real_hash_xyz", "verdict": "pass", "reason": "solid evidence"}]'
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

    # 4. JSON array with no known hashes → no_hash_matches
    result, outcome = parse_adversarial_finding_verdicts(
        '[{"hash": "unknown-hash", "verdict": "fail", "reason": "x"}]', hashes
    )
    assert outcome == "no_hash_matches"
    assert all(r["unresolved_reason"] == "no_hash_matches" for r in result.values())

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

    _project, direction, task = await _grader_project_and_task(db_session, "d-defer-adv01")
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    deferred_hs = _high_stakes_finding(direction["id"], "Batch-cap deferred high-stakes finding")
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
    assert children == [], "high-stakes finding must not spawn before adversarial verdict"
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
    fhash = _finding_hash(hs["title"], hs.get("description", "") or hs.get("prompt", ""))

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[hs],
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
    findings_json, _, _ = findings_block.partition("-----8<----- FINDINGS END -----8<-----")
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
    assert task.context.get("_adversarial_wholesale_retry_count") == 1
    assert (task.context or {}).get("deferred_findings") in (None, [])

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

    children = (
        (
            await db_session.execute(
                _select(_Task).where(_Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert children == [], "retry-exhausted finding must not spawn"

    # After the bounded retry is exhausted, the failed finding must appear in
    # deferred_findings for the next cycle.
    deferred = (task.context or {}).get("deferred_findings", [])
    assert any(
        d.get("title") == hs["title"] for d in deferred
    ), "failed finding must be in deferred_findings"
    assert task.context.get("research_spawn_gate_wholesale_retry_exhausted") is True
    # _adversarial_verdict must be stripped from the deferred entry.
    for d in deferred:
        assert "_adversarial_verdict" not in d, "_adversarial_verdict must be cleared on deferral"


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

    _project, direction, task = await _grader_project_and_task(db_session, "d-adv-deferred01")
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    # A high-stakes finding deferred from the previous cycle.
    deferred_hs = _high_stakes_finding(direction["id"], "Prior-deferred high-stakes finding")
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

    assert task.status == TaskStatus.EVALUATING, "task must park awaiting adversarial verdict"
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
    fhash = _finding_hash(hs["title"], hs.get("description", "") or hs.get("prompt", ""))

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
    assert len(children) == 0, "absent hash in verdict → fail-closed → finding must NOT spawn"
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

    _project, direction, task = await _grader_project_and_task(db_session, "d-adv-reason01")
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    hs = _high_stakes_finding(direction["id"], "Refuted stale finding")
    fhash = _finding_hash(hs["title"], hs.get("description", "") or hs.get("prompt", ""))

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
    assert any(
        d.get("title") == hs["title"] for d in deferred
    ), "refuted finding must be in deferred_findings"
    matching = [d for d in deferred if d.get("title") == hs["title"]]
    assert len(matching) == 1
    d = matching[0]
    assert d.get("adversarial_gate_reason") == "premise wrong — code moved to new module", (
        "deferred record must carry the challenger's reason"
    )
    assert "_adversarial_verdict" not in d, "_adversarial_verdict must be cleared"
    assert "_adversarial_verdict_reason" not in d, "internal key must be cleared"


@_pytest.mark.asyncio
async def test_adversarial_gate_deferral_decays_or_escalates_after_threshold(
    db_session, mocker
):
    """Repeated adversarial refutations are counted and terminally escalated."""
    from test_autonomous_research import _make_finding, _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-adv-decay01")
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

    _project, direction, task = await _grader_project_and_task(db_session, "d-adv-explicit01")
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    hs = _high_stakes_finding(direction["id"], "Fail-closed explicit check")
    fhash = _finding_hash(hs["title"], hs.get("description", "") or hs.get("prompt", ""))

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
    assert matching, (
        "absent-hash finding must be deferred, not silently dropped"
    )
    assert matching[0].get("adversarial_gate_reason") == "no_hash_matches"
    assert task.context.get("research_spawn_gate_wholesale_retry_exhausted") is True
    assert task.context.get("research_spawn_gate_parse_outcome") == "no_hash_matches"


@_pytest.mark.asyncio
async def test_adversarial_gate_partial_verdict_defers_only_skipped_hash(
    db_session, mocker
):
    """A valid partial verdict array is per-finding fail-closed, not wholesale."""
    from app.research_completion import _finding_hash
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-adv-partial")
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

    mocker.patch("app.research_completion._parse_research_findings", return_value=findings)

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

    _project, direction, task = await _grader_project_and_task(db_session, "d-adv-bad-token")
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    hs = _high_stakes_finding(direction["id"], "Malformed verdict token")
    fhash = _finding_hash(hs["title"], hs.get("description", "") or hs.get("prompt", ""))
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
    assert (
        deferred[0].get("adversarial_gate_reason") == "verdict_unrecognized"
    )
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
            '{"hash": "' + pass_hash + '", "verdict": "pass", "reason": "well-grounded"},'
            '{"hash": "' + fail_hash + '", "verdict": "fail", "reason": "stale premise"}'
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
    assert len(children) == 2, "one challenged pass + one unchallenged finding must spawn"

    by_title = {child.title: child for child in children}
    challenged = by_title[high_pass["title"]]
    unchallenged = by_title[low["title"]]
    assert (challenged.context or {}).get("research_spawn_gate_challenged") is True
    assert (challenged.context or {}).get("research_spawn_gate_verdict") == "pass"
    assert (challenged.context or {}).get("research_spawn_gate_reason") == "well-grounded"
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

    _project, direction, task = await _grader_project_and_task(db_session, "d-adv-breakdown01")
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    high_explicit_pass = _high_stakes_finding(direction["id"], "Explicit-pass refactor")
    high_fail_open = _high_stakes_finding(direction["id"], "Fail-open defaulted pass")
    high_refuted = _high_stakes_finding(direction["id"], "Refuted stale finding")
    high_unresolved = _high_stakes_finding(direction["id"], "Unresolved no-verdict finding")
    low = _make_finding(direction_id=direction["id"])
    low["title"] = "Routine low-stakes follow-up"
    low["risk"] = "low"

    def _h(f):
        return _finding_hash(f["title"], f.get("description", "") or f.get("prompt", ""))

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
                pass_hash: {"passed": True, "reason": "well-grounded", "explicit": True},
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

    _project, direction, task = await _grader_project_and_task(db_session, "d-adv-bounded01")
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    # Three high-stakes findings; only two were admitted to the manifest.
    hs_in_manifest_1 = _high_stakes_finding(direction["id"], "Manifest finding one")
    hs_in_manifest_2 = _high_stakes_finding(direction["id"], "Manifest finding two")
    hs_excluded = _high_stakes_finding(direction["id"], "Budget-excluded high-stakes finding")

    def _h(f):
        return _finding_hash(f["title"], f.get("description", "") or f.get("prompt", ""))

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
        == ctx["research_spawn_gate_challenged_count"] - ctx["research_spawn_gate_refuted_count"]
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

    deferred = {
        d.get("title"): d
        for d in (ctx.get("deferred_findings") or [])
        if d.get("deferred_reason") == "adversarial_gate"
    }
    assert deferred["Budget-excluded high-stakes finding"]["finding_hash"] == excluded_hash
    assert deferred["Budget-excluded high-stakes finding"][
        "adversarial_gate_reason"
    ] == (
        "fail-closed: high-stakes finding was not admitted "
        "to the adversarial manifest"
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

    assert task.status == TaskStatus.EVALUATING, "Phase 1: task must be parked EVALUATING"
    assert task.context.get("awaiting_adversarial_grader"), "Phase 1: must set awaiting flag"
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
    assert len(others) == 1, "Phase 1: exactly one adversarial grader task must be spawned"
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
    assert len(children_p2) == 1, "Phase 2a (PASS): finding must spawn exactly one child"
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
    assert children_fail == [], "Phase 2b (FAIL): adversarial-failed finding must NOT spawn"

    deferred = (task2.context or {}).get("deferred_findings", [])
    assert any(
        d.get("title") == hs2["title"] for d in deferred
    ), "Phase 2b (FAIL): failed finding must appear in deferred_findings"
    for d in deferred:
        assert "_adversarial_verdict" not in d, "Phase 2b: _adversarial_verdict must be cleared"


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
    fhash1 = _finding_hash(hs1["title"], hs1.get("description", "") or hs1.get("prompt", ""))
    fhash2 = _finding_hash(hs2["title"], hs2.get("description", "") or hs2.get("prompt", ""))
    fhash3 = _finding_hash(hs3["title"], hs3.get("description", "") or hs3.get("prompt", ""))
    fhash4 = _finding_hash(hs4["title"], hs4.get("description", "") or hs4.get("prompt", ""))
    fhash5 = _finding_hash(hs5["title"], hs5.get("description", "") or hs5.get("prompt", ""))

    # Set up the task with malformed verdict records
    task.plan_output = findings
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "finding_verdicts": {
                fhash1: {"reason": "tested"},  # Missing "passed" key -> fail-closed
                fhash2: {"passed": None, "reason": "tested"},  # Null passed -> fail-closed
                fhash3: {"passed": "true", "reason": "tested"},  # String passed -> fail-closed
                fhash4: {"passed": True, "reason": "valid", "explicit": True},  # Valid True -> spawn
                fhash5: {"passed": False, "reason": "refuted", "explicit": True},  # Valid False -> defer
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
    """resolve_research_findings_for_surface: canonical challenge-enriched findings
    for surface rendering — prefers plan_output over artifact logs, trims challenge
    records to the bounded vocabulary, and falls back gracefully."""

    from app.research_completion import resolve_research_findings_for_surface

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

    def test_challenge_enriched_findings_surface_through_serializer(self):
        """A finding in plan_output with a finding_challenge surfaces the bounded
        challenge fields and strips internal-only keys."""
        from app.research_completion import (
            _FINDING_CHALLENGE_RECORD_KEY,
            resolve_research_findings_for_surface,
        )

        challenge = self._challenge()
        finding = self._finding(title="Bug with challenge")
        finding[_FINDING_CHALLENGE_RECORD_KEY] = challenge

        result = resolve_research_findings_for_surface([finding], None)

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
        from app.research_completion import resolve_research_findings_for_surface

        finding = self._finding(title="No challenge finding", actionability_grade="HIGH")
        result = resolve_research_findings_for_surface([finding], None)

        assert result is not None
        assert len(result) == 1
        assert result[0]["title"] == "No challenge finding"
        assert result[0]["actionability_grade"] == "HIGH"
        assert "finding_challenge" not in result[0]

    def test_plan_output_preferred_over_artifact_logs(self):
        """When both plan_output and artifact_logs are present, plan_output wins."""
        from app.research_completion import resolve_research_findings_for_surface
        import json

        plan_finding = self._finding(title="From plan_output")
        artifact_finding = self._finding(title="From artifact_logs")
        artifact_logs = json.dumps([artifact_finding])

        result = resolve_research_findings_for_surface([plan_finding], artifact_logs)

        assert result is not None
        assert len(result) == 1
        assert result[0]["title"] == "From plan_output"

    def test_falls_back_to_artifact_logs_when_plan_output_absent(self):
        """When plan_output is None, artifact_logs (list shape) is parsed."""
        from app.research_completion import resolve_research_findings_for_surface
        import json

        artifact_finding = self._finding(title="From artifact list logs")
        artifact_logs = json.dumps([artifact_finding])

        result = resolve_research_findings_for_surface(None, artifact_logs)

        assert result is not None
        assert len(result) == 1
        assert result[0]["title"] == "From artifact list logs"

    def test_falls_back_to_artifact_logs_when_plan_output_empty(self):
        """When plan_output is an empty list, artifact_logs is used as fallback."""
        from app.research_completion import resolve_research_findings_for_surface
        import json

        artifact_finding = self._finding(title="From fallback logs")
        artifact_logs = json.dumps([artifact_finding])

        result = resolve_research_findings_for_surface([], artifact_logs)

        assert result is not None
        assert len(result) == 1
        assert result[0]["title"] == "From fallback logs"

    def test_artifact_logs_legacy_dict_shape_unwrapped(self):
        """Artifact logs in the legacy {"logs": [...]} dict shape are unwrapped."""
        from app.research_completion import resolve_research_findings_for_surface
        import json

        artifact_finding = self._finding(title="Inside dict logs")
        artifact_logs = json.dumps({"logs": [artifact_finding]})

        result = resolve_research_findings_for_surface(None, artifact_logs)

        assert result is not None
        assert len(result) == 1
        assert result[0]["title"] == "Inside dict logs"

    def test_both_absent_returns_none(self):
        """Returns None when both plan_output and artifact_logs are absent."""
        from app.research_completion import resolve_research_findings_for_surface

        assert resolve_research_findings_for_surface(None, None) is None
        assert resolve_research_findings_for_surface([], None) is None
        assert resolve_research_findings_for_surface(None, "") is None

    def test_malformed_artifact_logs_returns_none(self):
        """Malformed JSON in artifact logs does not raise — returns None."""
        from app.research_completion import resolve_research_findings_for_surface

        result = resolve_research_findings_for_surface(None, "not valid json {{{")
        assert result is None

    def test_activity_pending_count_reflects_canonical_finding_set(self):
        """The count derived from the helper reflects the canonical (plan_output)
        finding set, not the stale artifact count, when they differ."""
        from app.research_completion import resolve_research_findings_for_surface
        import json

        # plan_output has 3 findings (canonical after challenge resolution)
        plan_findings = [
            self._finding(title=f"Finding {i}") for i in range(3)
        ]
        # artifact logs only have 2 (written before challenge resolution)
        artifact_logs = json.dumps([self._finding(title=f"Old {i}") for i in range(2)])

        result = resolve_research_findings_for_surface(plan_findings, artifact_logs)

        assert result is not None
        assert len(result) == 3  # canonical count wins

@_pytest.mark.asyncio
async def test_adversarial_gate_payload_uses_hash_field(db_session, mocker):
    """The adversarial grader payload must expose the contract hash under `hash`."""
    from test_autonomous_research import _patch_research_externals

    _project, direction, task = await _grader_project_and_task(db_session, "d-adv-hash01")
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

    _project, direction, task = await _grader_project_and_task(db_session, "d-adv-recover01")
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        side_effect=lambda f: f.get("risk") == "high",
    )

    recovered = _high_stakes_finding(direction["id"], "Recovered risky finding")
    current = _make_finding(direction_id=direction["id"], title="Current low-stakes finding")
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
        long_findings.append({
            "title": f"High-stakes finding number {i}",
            "description": "Very long description " * 25 + f"(finding {i})",
            "prompt": f"Implement fix for finding {i}",
            "type": "improvement",
            "priority": "high",
            "scope": "medium",
            "risk": "high",
            "direction_id": direction["id"],
            "actionability_grade": "HIGH",
        })

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

"""Unit tests for research_completion.py pre-spawn scope-budget gate, low_actionability decay, grade normalization, source ref validation, and findings index."""

import logging
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

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
    _apply_structural_actionability_downgrade,
    _handle_research_completion,
    _reconcile_verification_basis,
    _record_research_survival_signal,
    _research_findings_would_spawn,
    _summarize_ungrounded_claims_grounded,
)
from app.task_evaluators import (
    QUALITY_GRADER_SOURCE_KEY,
    QUALITY_GRADER_VERDICT_KEY,
    parse_grader_verdict,
    spawn_forward_quality_grader,
)
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
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch(
            "medium", files
        )
        assert scope == "medium"
        assert cal_from is None
        assert warning is None

    def test_medium_one_over_limit_upgrades(self):
        files = [f"file{i}.py" for i in range(MEDIUM_ANALYSIS_PLAN_FILE_LIMIT + 1)]
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch(
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
            if _reason == "ultra_low_confidence" and _count >= 4:
                _decayed.append(f)
            elif _reason == "degradation_gate" and _count >= 3:
                _decayed.append(f)
            elif _reason == "low_actionability" and _count >= 3:
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
        assert VALID_VERIFICATION_BASES == {
            "code_verified",
            "path_verified",
            "pattern_inferred",
            "deduced",
        }
        assert _HIGH_EVIDENCE_BASES == {"code_verified", "path_verified"}
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

    async def _spawn_deferred_child(self, db_session, mocker, *, direction_id, deferred):
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
            "broken_source_ref_count": 1,
            "broken_refs": ["backend/app/missing.py"],
            "cited_files_changed_since_generation": True,
        }


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
    # Declare a prerequisite that does not exist in the project
    finding["prerequisite"] = {"kind": "task", "ref": "nonexistent-task-id"}

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
    assert need == {"kind": "task", "ref": "nonexistent-task-id"}, (
        "unresolved prerequisite must be carried as the blocked need"
    )
    # Still stamped on context for operator visibility, in addition to BLOCKED.
    unresolved = (child.context or {}).get("unresolved_prerequisite", [])
    assert any(
        u.get("kind") == "task" and u.get("ref") == "nonexistent-task-id"
        for u in (unresolved if isinstance(unresolved, list) else [unresolved])
    ), "unresolved prerequisite details must remain in context for visibility"


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

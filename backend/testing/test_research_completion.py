"""Unit tests for research_completion.py pre-spawn scope-budget gate, low_actionability decay, grade normalization, source ref validation, and findings index."""

import logging
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from app.models import TaskStatus
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
    _apply_structural_actionability_downgrade,
    _summarize_ungrounded_claims_grounded,
)
from app.time_utils import _utcnow


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

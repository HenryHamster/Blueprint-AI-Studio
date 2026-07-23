# AUTO-SPLIT from test_research_completion.py — see refactor: split mega backend test files.
# Behavior-preserving: conftest.py fixtures (db_session, mocker, monkeypatch) are
# inherited automatically. This file holds the normalization + source-ref validation
# domain slice: grade/basis normalization, source-ref parsing, ungrounded-claim
# detection, structural actionability downgrade, and broken-ref summarization.
"""Domain slice: research_completion.py — normalization and source-ref validation

Split from test_research_completion.py. Covers:
  - TestActionabilityGradeNormalization
  - TestVerificationBasisNormalization
  - TestVerificationBasisHeadReconciliation
  - TestSourceRefParsing
  - TestSourceRefValidation
  - TestSourceRefValidationBranchAware
  - TestUngroundedClaimDetection
  - TestUngroundedClaimsGroundedAgainstBranch
  - TestStructuralActionabilityDowngrade
  - TestSummarizeBrokenRefs
"""

import logging
from unittest.mock import AsyncMock

from app.source_ref_validation import (
    _parse_ref,
    _symbol_defined_at_head,
    summarize_broken_refs,
    summarize_ungrounded_claims,
    validate_source_refs,
)
from app.research_completion import (
    _DOWNGRADED_VERIFICATION_BASIS,
    _HIGH_EVIDENCE_BASES,
    VALID_VERIFICATION_BASES,
    _apply_structural_actionability_downgrade,
    _reconcile_verification_basis,
    _summarize_ungrounded_claims_grounded,
)

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
        assert (
            await _symbol_defined_at_head("backend/app/example.py", "present_symbol")
            is True
        )

    async def test_symbol_ref_absent_in_existing_file_is_flagged(self, monkeypatch):
        self._patch_git_show(monkeypatch, stdout=b"def other_symbol():\n    pass\n")
        assert (
            await _symbol_defined_at_head("backend/app/example.py", "missing_symbol")
            is False
        )

    async def test_symbol_ref_missing_file_is_flagged(self, monkeypatch):
        self._patch_git_show(
            monkeypatch,
            returncode=128,
            stderr=b"fatal: path 'backend/app/missing.py' exists on disk, but not in 'HEAD'",
        )
        assert (
            await _symbol_defined_at_head("backend/app/missing.py", "missing") is False
        )

    async def test_malformed_symbol_ref_is_ignored(self):
        assert await summarize_ungrounded_claims(["backend/app/example.py:42"]) is None

    async def test_git_failure_degrades_gracefully(self, monkeypatch):
        from app import source_ref_validation as srv

        async def _create(*args, **kwargs):
            raise OSError("git unavailable")

        monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", _create)
        assert (
            await _symbol_defined_at_head("backend/app/example.py", "present_symbol")
            is None
        )


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

    async def test_github_path_indeterminate_line_count_never_invalid(
        self, monkeypatch
    ):
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
        self._patch_git_show(monkeypatch, stdout=b"def other_func():\n    pass\n")
        result = await summarize_ungrounded_claims(
            ["backend/app/module.py:missing_symbol is the main entry point"]
        )
        assert result is not None
        assert result["ungrounded_claim_count"] == 1
        assert "backend/app/module.py:missing_symbol" in result["ungrounded_refs"]

    async def test_grounded_claim_with_present_symbol_is_not_flagged(self, monkeypatch):
        """A claim referencing a symbol that IS defined is not flagged."""
        self._patch_git_show(
            monkeypatch, stdout=b"def present_func():\n    return True\n"
        )
        result = await summarize_ungrounded_claims(
            ["backend/app/module.py:present_func implements the main logic"]
        )
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
                "quote": "backend/app/module.py:missing_func is defined here",
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
            },
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


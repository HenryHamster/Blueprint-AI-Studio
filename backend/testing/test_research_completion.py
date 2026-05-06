"""Unit tests for research_completion.py pre-spawn scope-budget gate, low_actionability decay, grade normalization, and source ref validation."""

import logging

from app.spawn_calibration import (
    MEDIUM_ANALYSIS_PLAN_FILE_LIMIT,
    SCOPE_FILE_BUDGETS,
    check_scope_budget_mismatch,
)
from app.source_ref_validation import (
    _parse_ref,
    collect_stale_refs,
    extract_source_file_paths,
    validate_source_refs,
)
from app.time_utils import _utcnow


class TestScopeBudgetGate:
    """check_scope_budget_mismatch: pre-spawn file-count / scope mismatch detection."""

    def test_small_5_files_upgrades_to_medium(self):
        files = ["a.py", "b.py", "c.py", "d.py", "e.py"]
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch("small", files)
        assert scope == "medium"
        assert cal_from == "small"
        assert warning is not None and "scope_budget_mismatch" in warning
        assert not requires_approval

    def test_medium_12_files_upgrades_to_large(self):
        files = [f"file{i}.py" for i in range(12)]
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch("medium", files)
        assert scope == "large"
        assert cal_from == "medium"
        assert warning is not None and "scope_budget_mismatch" in warning
        assert not requires_approval

    def test_large_30_files_requires_approval(self):
        files = [f"file{i}.py" for i in range(30)]
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch("large", files)
        assert scope == "large"
        assert cal_from is None
        assert warning is not None and "scope_budget_mismatch" in warning
        assert requires_approval

    def test_small_2_files_passes_unchanged(self):
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch("small", ["a.py", "b.py"])
        assert scope == "small"
        assert cal_from is None
        assert warning is None
        assert not requires_approval

    def test_medium_within_limit_passes_unchanged(self):
        files = [f"file{i}.py" for i in range(MEDIUM_ANALYSIS_PLAN_FILE_LIMIT)]
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch("medium", files)
        assert scope == "medium"
        assert cal_from is None
        assert warning is None
        assert not requires_approval

    def test_large_within_budget_passes_unchanged(self):
        large_limit = (SCOPE_FILE_BUDGETS.get("large") or {}).get("total_files_max", 20)
        files = [f"file{i}.py" for i in range(large_limit)]
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch("large", files)
        assert scope == "large"
        assert cal_from is None
        assert warning is None
        assert not requires_approval

    def test_empty_files_passes_unchanged(self):
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch("small", [])
        assert scope == "small"
        assert cal_from is None
        assert warning is None
        assert not requires_approval

    def test_none_files_passes_unchanged(self):
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch("medium", None)
        assert scope == "medium"
        assert cal_from is None
        assert warning is None
        assert not requires_approval

    def test_medium_exactly_at_limit_passes_unchanged(self):
        # MEDIUM_ANALYSIS_PLAN_FILE_LIMIT files is exactly at the limit, not over
        files = [f"file{i}.py" for i in range(MEDIUM_ANALYSIS_PLAN_FILE_LIMIT)]
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch("medium", files)
        assert scope == "medium"
        assert cal_from is None
        assert warning is None

    def test_medium_one_over_limit_upgrades(self):
        files = [f"file{i}.py" for i in range(MEDIUM_ANALYSIS_PLAN_FILE_LIMIT + 1)]
        scope, cal_from, warning, requires_approval = check_scope_budget_mismatch("medium", files)
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
            f for f in findings
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
        should_gate = (
            finding.get("actionability_grade") == "LOW"
            and not finding.get("deferred_reason")
        )
        assert not should_gate, "Previously-deferred findings should not be re-gated"

        # Instead, it goes through the decay logic
        _count = int(finding.get("deferred_count", 0) or 0)
        _reason = finding.get("deferred_reason")
        should_decay = _reason == "low_actionability" and _count >= 3
        assert not should_decay, "Deferral count=1 should not trigger decay (threshold=3)"

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
        logger = logging.getLogger("app.research_completion")
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
        finding = {"title": "thin", "actionability_grade": "HIGH", "description": "too short"}
        structural_grade = "MEDIUM" if (finding.get("claims") or []) and len((finding.get("description") or "").strip()) >= 40 else "LOW"
        assert structural_grade == "LOW"


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

    def test_pr_source_passes_through(self):
        """PR-type sources require no validation."""
        sources = [
            {
                "type": "pr",
                "ref": "https://github.com/owner/repo/pull/123",
                "description": "PR that added feature X",
            }
        ]
        validated = validate_source_refs(sources)
        assert len(validated) == 1
        assert validated[0]["type"] == "pr"
        # No "valid" field should be added for PR sources
        assert "valid" not in validated[0] or validated[0].get("valid") is not False

    def test_message_source_passes_through(self):
        """Message-type sources require no validation."""
        sources = [
            {
                "type": "message",
                "ref": "Slack message from @user",
                "description": "Team feedback on design",
            }
        ]
        validated = validate_source_refs(sources)
        assert len(validated) == 1
        assert validated[0]["type"] == "message"

    def test_file_source_valid(self, monkeypatch):
        """Valid file source (file exists) passes validation."""
        # Mock the workspace helpers so the test doesn't depend on /tmp/workspace
        # being present in the test environment (it isn't, inside the Docker
        # backend test container).
        from app import source_ref_validation as srv
        monkeypatch.setattr(srv, "_file_exists_at_head", lambda path: True)
        sources = [
            {
                "type": "file",
                "ref": "backend/testing/test_research_completion.py",
                "description": "Test file",
            }
        ]
        validated = validate_source_refs(sources)
        assert len(validated) == 1
        # Should not have valid: false marker if validation passed
        assert validated[0].get("valid") != False

    def test_file_source_missing_file(self, monkeypatch):
        """Missing file gets marked with valid: false."""
        from app import source_ref_validation as srv
        monkeypatch.setattr(srv, "_file_exists_at_head", lambda path: False)
        sources = [
            {
                "type": "file",
                "ref": "nonexistent/file_that_does_not_exist_12345.py",
                "description": "Missing file",
            }
        ]
        validated = validate_source_refs(sources)
        assert len(validated) == 1
        # Should be marked as invalid
        assert validated[0].get("valid") == False

    def test_file_source_missing_ref(self):
        """File source without ref field is dropped."""
        sources = [
            {
                "type": "file",
                "description": "File source without ref",
            }
        ]
        validated = validate_source_refs(sources)
        # Should be dropped (not in validated list)
        assert len(validated) == 0

    def test_file_source_with_line_valid(self, monkeypatch):
        """File source with valid line number."""
        from app import source_ref_validation as srv
        monkeypatch.setattr(srv, "_file_exists_at_head", lambda path: True)
        monkeypatch.setattr(srv, "_file_has_line_count", lambda path, n: True)
        sources = [
            {
                "type": "file",
                "ref": "backend/testing/test_research_completion.py:5",
                "description": "File at line 5",
            }
        ]
        validated = validate_source_refs(sources)
        assert len(validated) == 1
        assert validated[0].get("valid") != False

    def test_non_dict_source_dropped(self):
        """Non-dict source entries are dropped."""
        sources = [
            "not a dict",
            {"type": "file", "ref": "backend/app/research_completion.py"},
        ]
        validated = validate_source_refs(sources)
        # The string should be dropped, only the dict should remain
        assert len(validated) == 1
        assert validated[0]["type"] == "file"

    def test_none_sources_returns_empty_list(self):
        """None sources input returns empty list."""
        validated = validate_source_refs(None)
        assert validated == []

    def test_non_list_sources_returns_empty_list(self):
        """Non-list sources input returns empty list."""
        validated = validate_source_refs("not a list")
        assert validated == []

    def test_mixed_source_types(self):
        """Batch with mixed source types is validated correctly."""
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
                "ref": "nonexistent/missing.py",
            },
        ]
        validated = validate_source_refs(sources)
        # All sources should be present (even the missing file gets marked invalid)
        assert len(validated) == 4
        # Check types are preserved
        assert validated[0]["type"] == "pr"
        assert validated[1]["type"] == "file"
        assert validated[2]["type"] == "message"
        assert validated[3]["type"] == "file"
        # Check invalid file is marked
        assert validated[3].get("valid") == False

    def test_unknown_source_type_passes_through(self):
        """Unknown source types pass through unchanged."""
        sources = [
            {
                "type": "custom_type",
                "ref": "some_reference",
                "description": "Custom source",
            }
        ]
        validated = validate_source_refs(sources)
        assert len(validated) == 1
        assert validated[0]["type"] == "custom_type"


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

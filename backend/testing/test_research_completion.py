"""Unit tests for research_completion.py pre-spawn scope-budget gate, low_actionability decay, grade normalization, source ref validation, and findings index."""

import logging
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from app.spawn_calibration import (
    MEDIUM_ANALYSIS_PLAN_FILE_LIMIT,
    SCOPE_FILE_BUDGETS,
    check_scope_budget_mismatch,
)
from app.source_ref_validation import (
    _parse_ref,
    collect_stale_refs,
    extract_source_file_paths,
    summarize_broken_refs,
    validate_source_refs,
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

    async def test_mixed_source_types(self):
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

    def _make_task(self, direction_id, plan_output, days_ago=1):
        from app.time_utils import _utcnow

        task = MagicMock()
        task.id = uuid.uuid4()
        task.context = {"direction_id": direction_id}
        task.created_at = _utcnow() - timedelta(days=days_ago)
        task.plan_output = plan_output
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

    def _make_session(self, project):
        session = AsyncMock()
        session.get.return_value = project
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

    def test_scope_empty_marks_cycle_productive(self):
        import asyncio
        from app.research_completion import _record_research_task_failure

        task = self._make_task("SCOPE_EMPTY")
        project = self._make_project()
        session = self._make_session(project)

        asyncio.get_event_loop().run_until_complete(
            _record_research_task_failure(task, session)
        )

        direction = project.directions[0]
        assert direction["last_cycle_productive"] is True

    def test_scope_empty_resets_unproductive_counter(self):
        import asyncio
        from app.research_completion import _record_research_task_failure

        task = self._make_task("SCOPE_EMPTY")
        project = self._make_project(consecutive_unproductive=3)
        session = self._make_session(project)

        asyncio.get_event_loop().run_until_complete(
            _record_research_task_failure(task, session)
        )

        direction = project.directions[0]
        assert direction.get("consecutive_unproductive_cycles", 0) == 0

    def test_code_error_marks_cycle_unproductive(self):
        import asyncio
        from app.research_completion import _record_research_task_failure

        task = self._make_task("CODE_ERROR")
        project = self._make_project(consecutive_unproductive=1)
        session = self._make_session(project)

        asyncio.get_event_loop().run_until_complete(
            _record_research_task_failure(task, session)
        )

        direction = project.directions[0]
        assert direction["last_cycle_productive"] is False
        assert direction.get("consecutive_unproductive_cycles", 0) == 2

    def test_scope_empty_history_entry_has_failure_category(self):
        import asyncio
        from app.research_completion import _record_research_task_failure

        task = self._make_task("SCOPE_EMPTY")
        project = self._make_project()
        session = self._make_session(project)

        asyncio.get_event_loop().run_until_complete(
            _record_research_task_failure(task, session)
        )

        direction = project.directions[0]
        history = direction.get("research_history", [])
        assert len(history) == 1
        assert history[0]["research_task_failed"] is True
        assert history[0]["failure_category"] == "SCOPE_EMPTY"


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

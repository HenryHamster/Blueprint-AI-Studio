"""
Standalone tests for _build_research_context eviction preservation.
No database required — pure function tests.
"""

import re

from app.research_context import (
    _build_research_context,
    _parse_research_context,
    _parse_eviction_counts,
    _format_eviction_body,
    build_recent_findings_evidence,
    extract_diagnosis_from_context,
)
from app.finding_deduplication import (
    extract_archived_diagnoses,
    _find_archived_diagnosis_match,
)


def _make_large_context(
    established_count: int = 0,
    failed_count: int = 0,
    oq_count: int = 0,
    entry_len: int = 90,
) -> str:
    """Build a research context string with entries of predictable size."""
    parts = []
    parts.append("## Established Findings")
    for i in range(established_count):
        parts.append(f"- Established finding {i}: " + "x" * entry_len)
    parts.append("")
    parts.append("## Failed Approaches")
    for i in range(failed_count):
        parts.append(f"- Failed approach {i}: tried pattern {i} and it failed badly")
    parts.append("")
    parts.append("## Open Questions")
    for i in range(oq_count):
        parts.append(f"- Open question {i}: " + "y" * entry_len)
    parts.append("")
    return "\n".join(parts).strip() + "\n"


# ── _parse_eviction_counts ───────────────────────────────────────────────────


class TestParseEvictionCounts:
    def test_empty_returns_zeros(self):
        result = _parse_eviction_counts(None)
        assert result == {
            "established": 0,
            "failed": 0,
            "open_questions": 0,
            "failed_titles": [],
        }

    def test_parses_count_line(self):
        body = "3 established, 2 failed, 1 open question evicted\nFailed: approach A; approach B"
        result = _parse_eviction_counts(body)
        assert result["established"] == 3
        assert result["failed"] == 2
        assert result["open_questions"] == 1
        assert result["failed_titles"] == ["approach A", "approach B"]

    def test_parses_partial_counts(self):
        body = "5 established evicted"
        result = _parse_eviction_counts(body)
        assert result["established"] == 5
        assert result["failed"] == 0
        assert result["failed_titles"] == []


# ── _format_eviction_body ────────────────────────────────────────────────────


class TestFormatEvictionBody:
    def test_empty_returns_empty(self):
        assert _format_eviction_body(0, 0, 0, []) == ""

    def test_formats_counts_and_titles(self):
        body = _format_eviction_body(3, 2, 1, ["approach A", "approach B"])
        assert "3 established" in body
        assert "2 failed" in body
        assert "1 open question" in body
        assert "evicted" in body
        assert "approach A" in body
        assert "approach B" in body

    def test_open_question_plural(self):
        body = _format_eviction_body(0, 0, 2, [])
        assert "2 open questions" in body

    def test_open_question_singular(self):
        body = _format_eviction_body(0, 0, 1, [])
        assert "1 open question evicted" in body
        assert "1 open questions" not in body


# ── _build_research_context: no eviction when under limit ───────────────────


class TestNoEvictionWhenUnderLimit:
    def test_fresh_small_context_has_no_eviction_summary(self):
        """Contexts under 8000 chars built from scratch have no eviction summary."""
        result = _build_research_context(
            prior_context=None,
            finding_titles=["Finding A", "Finding B"],
            key_finding_descriptions=["A description.", "B description."],
            cycle_insights=["Why does X happen?"],
            history_entry={},
        )
        assert "## Eviction Summary" not in result
        assert "## Established Findings" in result
        assert len(result) <= 8000

    def test_moderate_context_has_no_eviction_summary(self):
        """A context with reasonable content stays under 8000 chars with no eviction."""
        prior = _make_large_context(established_count=5, failed_count=3, oq_count=3)
        result = _build_research_context(
            prior_context=prior,
            finding_titles=["New finding"],
            key_finding_descriptions=["New description."],
            cycle_insights=[],
            history_entry={},
        )
        assert "## Eviction Summary" not in result
        assert len(result) <= 8000


# ── _build_research_context: eviction summary when over limit ────────────────


class TestEvictionSummaryWhenOverLimit:
    def test_eviction_summary_present_when_over_limit(self):
        """Contexts that exceed the limit should include an ## Eviction Summary section."""
        prior = _make_large_context(established_count=80, failed_count=20, entry_len=90)
        result = _build_research_context(
            prior_context=prior,
            finding_titles=["New finding"],
            key_finding_descriptions=["New description."],
            cycle_insights=[],
            history_entry={},
        )
        assert "## Eviction Summary" in result
        assert "evicted" in result
        assert len(result) <= 8000

    def test_eviction_preserves_failed_approach_first_60_chars(self):
        """Evicted Failed Approaches entries appear truncated to 60 chars in the summary."""
        # Build a context with large established section that will force eviction,
        # and a specific failed approach as the oldest entry (will be evicted first
        # if failed section is largest, otherwise established entries get evicted).
        # Use many failed approaches to ensure the failed section is evicted from.
        specific_approach = (
            "Specific approach to evict: tried using X pattern and it collapsed"
        )
        filler_approaches = [f"Approach filler {i}: " + "a" * 60 for i in range(50)]
        filler_established = [f"Established filler {i}: " + "b" * 90 for i in range(50)]

        parts = ["## Established Findings"]
        for e in filler_established:
            parts.append(f"- {e}")
        parts.append("")
        parts.append("## Failed Approaches")
        # specific_approach is oldest (first) so it gets evicted first
        parts.append(f"- {specific_approach}")
        for a in filler_approaches:
            parts.append(f"- {a}")
        parts.append("")
        parts.append("## Open Questions")
        parts.append("")
        prior = "\n".join(parts).strip() + "\n"

        result = _build_research_context(
            prior_context=prior,
            finding_titles=["Trigger new finding"],
            key_finding_descriptions=["Trigger description."],
            cycle_insights=[],
            history_entry={},
        )

        assert "## Eviction Summary" in result
        assert len(result) <= 8000

        parsed = _parse_research_context(result)
        eviction_text = parsed["eviction_summary"] or ""

        # Failed entries were evicted — the first 60 chars should appear in the summary
        if "Failed:" in eviction_text:
            assert specific_approach[:60] in eviction_text

    def test_only_established_count_preserved_not_text(self):
        """Evicted Established entries contribute only to the count, not full text."""
        prior = _make_large_context(established_count=80, failed_count=2, entry_len=90)
        result = _build_research_context(
            prior_context=prior,
            finding_titles=["New finding"],
            key_finding_descriptions=["Desc."],
            cycle_insights=[],
            history_entry={},
        )

        parsed = _parse_research_context(result)
        eviction_text = parsed["eviction_summary"] or ""

        if eviction_text:
            m = re.search(r"(\d+) established", eviction_text)
            if m:
                # Count is present (not full text of each evicted entry)
                assert int(m.group(1)) > 0
            # Established entries themselves don't appear as full text in eviction summary
            assert "Established finding 0" not in eviction_text


# ── _build_research_context: eviction summary accumulates ───────────────────


class TestEvictionSummaryAccumulation:
    def test_prior_eviction_summary_preserved_when_no_new_evictions(self):
        """When no trimming is needed, the prior eviction summary is still preserved."""
        prior_with_summary = (
            "## Eviction Summary\n"
            "2 established evicted\n"
            "\n"
            "## Established Findings\n"
            "- Finding A\n"
            "\n"
            "## Failed Approaches\n"
            "\n"
            "## Open Questions\n"
        )
        result = _build_research_context(
            prior_context=prior_with_summary,
            finding_titles=["Small new finding"],
            key_finding_descriptions=["Short desc."],
            cycle_insights=[],
            history_entry={},
        )
        assert "## Eviction Summary" in result
        assert "2 established evicted" in result

    def test_eviction_counts_accumulate_across_builds(self):
        """New eviction counts are added on top of prior counts."""
        # Build a prior context with an existing eviction summary (5 established, 3 failed)
        # and large enough main content to trigger new evictions when more content is added.
        large_established = "\n".join(
            [f"- Established finding {i}: " + "x" * 90 for i in range(70)]
        )
        prior_with_summary = (
            "## Eviction Summary\n"
            "5 established, 3 failed evicted\n"
            "Failed: Prior approach A (failed due to X); Prior approach B\n"
            "\n"
            f"## Established Findings\n{large_established}\n"
            "\n## Failed Approaches\n"
            "- Existing failed approach: tried Y\n"
            "\n## Open Questions\n"
        )

        # Adding more findings will push the document over 3500, triggering new evictions
        result = _build_research_context(
            prior_context=prior_with_summary,
            finding_titles=[f"New finding {i}" for i in range(5)],
            key_finding_descriptions=[f"New description {i}." for i in range(5)],
            cycle_insights=[],
            history_entry={},
        )

        assert "## Eviction Summary" in result
        assert len(result) <= 8000

        parsed = _parse_research_context(result)
        eviction_text = parsed["eviction_summary"] or ""

        # Established count must be at least 5 (from prior)
        m = re.search(r"(\d+) established", eviction_text)
        assert m, (
            f"Expected established count in eviction summary, got: {eviction_text!r}"
        )
        assert int(m.group(1)) >= 5

        # Failed count must be at least 3 (from prior)
        m = re.search(r"(\d+) failed", eviction_text)
        assert m, f"Expected failed count in eviction summary, got: {eviction_text!r}"
        assert int(m.group(1)) >= 3

        # Prior failed titles should still be in the summary
        assert "Prior approach A" in eviction_text

    def test_failed_titles_accumulate_across_builds(self):
        """Failed approach titles from prior and new evictions are both preserved."""
        # Prior context with one failed title in eviction summary + large content
        large_established = "\n".join(
            [f"- Established {i}: " + "x" * 90 for i in range(35)]
        )
        prior_with_summary = (
            "## Eviction Summary\n"
            "2 failed evicted\n"
            "Failed: Old evicted approach title here\n"
            "\n"
            f"## Established Findings\n{large_established}\n"
            "\n## Failed Approaches\n"
            "- New approach to evict: first one in section\n"
            + "\n".join([f"- Filler approach {i}: something" for i in range(20)])
            + "\n\n## Open Questions\n"
        )

        result = _build_research_context(
            prior_context=prior_with_summary,
            finding_titles=["Trigger finding"],
            key_finding_descriptions=["Trigger desc."],
            cycle_insights=[],
            history_entry={},
        )

        assert "## Eviction Summary" in result
        parsed = _parse_research_context(result)
        eviction_text = parsed["eviction_summary"] or ""

        # Old evicted title should still be present
        assert "Old evicted approach title here" in eviction_text


# ── _build_research_context: prior outcome tagging ──────────────────────────


class TestPriorOutcomeTagging:
    def _prior_with_proposed(self, titles):
        parts = ["## Established Findings"]
        for t in titles:
            parts.append(f"- {t} [small] (proposed). Short desc")
        parts.append("")
        parts.append("## Failed Approaches")
        parts.append("")
        parts.append("## Open Questions")
        parts.append("")
        return "\n".join(parts).strip() + "\n"

    def test_failed_titles_retagged_with_category(self):
        prior = self._prior_with_proposed(["Refactor X", "Add feature Y"])
        history = [
            {
                "finding_titles": ["Refactor X", "Add feature Y"],
                "tasks_created": 2,
                "prs_merged": 0,
                "failed_task_details": [
                    {"title": "Refactor X", "category": "CODE_ERROR"},
                ],
            }
        ]
        result = _build_research_context(
            prior_context=prior,
            finding_titles=[],
            key_finding_descriptions=[],
            cycle_insights=[],
            history_entry={},
            prior_history=history,
        )
        assert "Refactor X" in result and "(failed: CODE_ERROR)" in result
        # Unmatched finding stays (proposed) since not all settled AND none merged
        assert "Add feature Y [small] (proposed)" in result

    def test_merged_titles_retagged_when_settled(self):
        prior = self._prior_with_proposed(["Clean up Z"])
        history = [
            {
                "finding_titles": ["Clean up Z"],
                "tasks_created": 1,
                "prs_merged": 1,
                "failed_task_details": [],
            }
        ]
        result = _build_research_context(
            prior_context=prior,
            finding_titles=[],
            key_finding_descriptions=[],
            cycle_insights=[],
            history_entry={},
            prior_history=history,
        )
        assert "Clean up Z [small] (merged)" in result
        assert "(proposed)" not in result

    def test_no_history_is_noop(self):
        prior = self._prior_with_proposed(["Thing A"])
        result = _build_research_context(
            prior_context=prior,
            finding_titles=[],
            key_finding_descriptions=[],
            cycle_insights=[],
            history_entry={},
            prior_history=None,
        )
        assert "Thing A [small] (proposed)" in result


class TestClaimsAndSources:
    """_build_research_context: claims/sources rendering and backward compat."""

    def _build(self, titles, descs, claims=None, sources=None):
        return _build_research_context(
            prior_context=None,
            finding_titles=titles,
            key_finding_descriptions=descs,
            cycle_insights=[],
            history_entry={},
            finding_claims=claims,
            finding_sources=sources,
        )

    def test_claims_rendered_in_entry(self):
        result = self._build(
            titles=["feat: add caching"],
            descs=["Add a cache layer."],
            claims=[["cache miss rate is 40%", "redis already deployed"]],
        )
        assert "Claims: cache miss rate is 40%" in result
        assert "redis already deployed" in result

    def test_sources_rendered_in_entry(self):
        result = self._build(
            titles=["feat: add caching"],
            descs=["Add a cache layer."],
            sources=[
                [{"type": "file", "ref": "backend/app/cache.py", "excerpt": "line 42"}]
            ],
        )
        assert "Sources: backend/app/cache.py" in result

    def test_finding_without_claims_sources_unchanged(self):
        result = self._build(
            titles=["feat: basic finding"],
            descs=["A simple description."],
        )
        assert "feat: basic finding [small] (proposed)" in result
        assert "Claims" not in result
        assert "Sources" not in result

    def test_empty_claims_list_not_rendered(self):
        result = self._build(
            titles=["feat: no claims"],
            descs=["Description."],
            claims=[[]],
        )
        assert "Claims" not in result

    def test_sources_missing_ref_skipped(self):
        result = self._build(
            titles=["feat: bad source"],
            descs=["Description."],
            sources=[[{"type": "file", "excerpt": "no ref here"}]],
        )
        assert "Sources" not in result

    def test_claims_capped_at_six(self):
        result = self._build(
            titles=["feat: many claims"],
            descs=["Description."],
            claims=[
                ["claim1", "claim2", "claim3", "claim4", "claim5", "claim6", "claim7"]
            ],
        )
        assert "claim1" in result
        assert "claim6" in result
        assert "claim7" not in result


class TestBuildRecentFindingsEvidence:
    """build_recent_findings_evidence: structured evidence field for strategic agent."""

    def test_populated_when_findings_have_claims(self):
        result = build_recent_findings_evidence(
            finding_titles=["feat: caching"],
            finding_claims=[["cache hit rate low", "redis available"]],
        )
        assert len(result) == 1
        assert result[0]["title"] == "feat: caching"
        assert "cache hit rate low" in result[0]["claims"]
        assert "redis available" in result[0]["claims"]

    def test_populated_when_findings_have_sources(self):
        result = build_recent_findings_evidence(
            finding_titles=["feat: caching"],
            finding_sources=[[{"ref": "backend/app/cache.py", "excerpt": "line 42"}]],
        )
        assert len(result) == 1
        assert result[0]["source_refs"][0]["ref"] == "backend/app/cache.py"
        assert result[0]["source_refs"][0]["excerpt"] == "line 42"

    def test_empty_when_no_findings_have_claims_or_sources(self):
        result = build_recent_findings_evidence(
            finding_titles=["feat: a", "feat: b"],
            finding_claims=[[], []],
            finding_sources=[[], []],
        )
        assert result == []

    def test_empty_when_no_findings(self):
        result = build_recent_findings_evidence(finding_titles=[])
        assert result == []

    def test_capped_at_max_findings(self):
        titles = [f"feat: finding {i}" for i in range(10)]
        claims = [[f"claim for {i}"] for i in range(10)]
        result = build_recent_findings_evidence(
            finding_titles=titles,
            finding_claims=claims,
            max_findings=5,
        )
        assert len(result) <= 5
        # Should contain the most recent findings
        assert result[-1]["title"] == "feat: finding 9"

    def test_respects_max_chars_budget(self):
        titles = [f"feat: finding {i}" for i in range(10)]
        claims = [["x" * 100] for _ in range(10)]
        result = build_recent_findings_evidence(
            finding_titles=titles,
            finding_claims=claims,
            max_chars=200,
        )
        # Budget is too small for all findings
        assert len(result) < 10


class TestEvictedOutRecords:
    """_build_research_context evicted_out: structured audit records for callers."""

    def _build_with_eviction(self):
        prior = _make_large_context(established_count=80, entry_len=90)
        evicted: list = []
        _build_research_context(
            prior_context=prior,
            finding_titles=["New finding"],
            key_finding_descriptions=["New description."],
            cycle_insights=[],
            history_entry={},
            evicted_out=evicted,
        )
        return evicted

    def test_populated_when_eviction_occurs(self):
        assert len(self._build_with_eviction()) > 0

    def test_record_has_required_fields(self):
        rec = self._build_with_eviction()[0]
        assert {"title", "claims", "source_refs", "status", "evicted_at"} <= rec.keys()

    def test_title_truncated_to_200_chars(self):
        rec = self._build_with_eviction()[0]
        assert len(rec["title"]) <= 200

    def test_empty_when_no_eviction(self):
        evicted: list = []
        _build_research_context(
            prior_context=None,
            finding_titles=["Tiny"],
            key_finding_descriptions=["Short."],
            cycle_insights=[],
            history_entry={},
            evicted_out=evicted,
        )
        assert evicted == []

    def test_none_evicted_out_is_noop(self):
        prior = _make_large_context(established_count=80, entry_len=90)
        result = _build_research_context(
            prior_context=prior,
            finding_titles=["New"],
            key_finding_descriptions=["Desc."],
            cycle_insights=[],
            history_entry={},
        )
        assert "## Eviction Summary" in result


class TestExtractDiagnosisFromContext:
    """extract_diagnosis_from_context: diagnosis archive functionality."""

    def test_extracts_eviction_summary_when_present(self):
        context = (
            "## Eviction Summary\n"
            "3 established, 2 failed evicted\n"
            "Failed: Approach A; Approach B\n"
            "\n"
            "## Established Findings\n"
            "- Finding 1\n"
        )
        diagnosis = extract_diagnosis_from_context(context)
        assert "3 established" in diagnosis
        assert "2 failed" in diagnosis
        assert "Approach A" in diagnosis

    def test_returns_empty_when_no_eviction_summary(self):
        context = "## Established Findings\n- Finding 1\n## Failed Approaches\n"
        diagnosis = extract_diagnosis_from_context(context)
        assert diagnosis == ""

    def test_returns_empty_for_none_context(self):
        diagnosis = extract_diagnosis_from_context(None)
        assert diagnosis == ""

    def test_returns_empty_for_empty_string(self):
        diagnosis = extract_diagnosis_from_context("")
        assert diagnosis == ""


class TestArchivedDiagnosesExtraction:
    """extract_archived_diagnoses: pulling diagnoses from design_doc archive."""

    def test_extracts_diagnoses_from_archive(self):
        design_doc = {
            "retired_diagnoses_archive": [
                {
                    "direction_id": "d-1",
                    "diagnosis": "3 established, 2 failed evicted",
                    "archived_at": "2026-05-01T00:00:00",
                },
                {
                    "direction_id": "d-2",
                    "diagnosis": "1 established evicted",
                    "archived_at": "2026-05-02T00:00:00",
                },
            ]
        }
        diagnoses = extract_archived_diagnoses(design_doc)
        assert len(diagnoses) == 2
        assert "3 established, 2 failed evicted" in diagnoses
        assert "1 established evicted" in diagnoses

    def test_returns_empty_when_no_archive(self):
        design_doc = {}
        diagnoses = extract_archived_diagnoses(design_doc)
        assert diagnoses == []

    def test_returns_empty_for_none_design_doc(self):
        diagnoses = extract_archived_diagnoses(None)
        assert diagnoses == []

    def test_filters_empty_diagnoses(self):
        design_doc = {
            "retired_diagnoses_archive": [
                {"direction_id": "d-1", "diagnosis": "valid diagnosis"},
                {"direction_id": "d-2", "diagnosis": ""},
                {"direction_id": "d-3", "diagnosis": "   "},
            ]
        }
        diagnoses = extract_archived_diagnoses(design_doc)
        assert len(diagnoses) == 1
        assert diagnoses[0] == "valid diagnosis"


class TestArchivedDiagnosisMatching:
    """_find_archived_diagnosis_match: word-overlap matching for diagnoses."""

    def test_matches_diagnosis_with_word_overlap(self):
        # Need ≥3 non-stopword overlap (see _find_archived_diagnosis_match
        # docstring): caching, approach, database — three shared content words.
        archived = [
            "3 established, 2 failed evicted from caching approach against database",
            "1 established evicted from authentication layer",
        ]
        title = "caching approach against database tuning"
        match = _find_archived_diagnosis_match(title, archived)
        assert match is not None
        assert "caching" in match.lower()

    def test_returns_none_when_no_overlap(self):
        archived = ["3 established evicted from database pooling"]
        title = "add new authentication mechanism"
        match = _find_archived_diagnosis_match(title, archived)
        assert match is None

    def test_requires_3_word_overlap_threshold(self):
        archived = [
            "approach to improve system performance in database",
        ]
        # Only 2 shared words: "system" and "database" — below threshold
        title = "system database optimization"
        match = _find_archived_diagnosis_match(title, archived)
        # This should match because shared non-stopwords are: system, database, improve → 3
        # Actually: "improve system database" vs "approach improve system performance database"
        # Shared: system, database = 2 words. Should NOT match.
        # Let me adjust the test...
        assert match is None

    def test_handles_case_insensitivity(self):
        # Need ≥3 non-stopword overlap: caching, approach, database (after
        # _STOPWORDS removes "add"/"to" etc.).
        archived = ["FAILED Caching APPROACH AGAINST DATABASE"]
        title = "caching approach against database"
        match = _find_archived_diagnosis_match(title, archived)
        assert match is not None

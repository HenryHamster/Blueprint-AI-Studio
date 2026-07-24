"""Tests for the anti-hedge detector in review_heuristics.py.

Covers ``detect_unjustified_hedge`` — the programmatic backstop that confirms
the reviewer's anti-hedge checklist rule fired on newly-introduced default-off /
observe-mode / fail-open behavior with no written justification.

The review-skip policy predicates (initial-review skip, re-review skip,
first-attempt minor-only) now live in ``app.review_policy`` and are covered by
``test_review_policy_parity.py`` and ``test_review_pipeline.py``.
"""

from app.review_heuristics import detect_unjustified_hedge


# ── detect_unjustified_hedge ───────────────────────────────────────────────────


class TestDetectUnjustifiedHedge:
    """Verify that the anti-hedge rule fires on unjustified hedges and stays
    silent on clean or justified findings.

    Acceptance contract: a test diff introducing a default-off flag with no
    justification is flagged (blocks auto-approval); the same hedge accompanied
    by an explicit written justification is allowed through.
    """

    def _major(self, desc: str) -> dict:
        return {"severity": "major", "description": desc}

    def _critical(self, desc: str) -> dict:
        return {"severity": "critical", "description": desc}

    def _minor(self, desc: str) -> dict:
        return {"severity": "minor", "description": desc}

    # --- Positive cases: unjustified hedge → (True, non-empty reason) -----------

    def test_major_default_off_no_justification_triggers(self):
        issues = [self._major("New flag observe_mode defaults to False with no justification")]
        flagged, reason = detect_unjustified_hedge(issues)
        assert flagged is True, "unjustified default-off MAJOR issue must trigger"
        assert reason, "reason must be non-empty when triggered"

    def test_critical_fail_open_triggers(self):
        issues = [self._critical("fail_open=True on new auth check without justification")]
        flagged, _ = detect_unjustified_hedge(issues)
        assert flagged is True

    def test_major_disabled_by_default_triggers(self):
        issues = [self._major("Feature X is disabled by default with no written justification")]
        flagged, _ = detect_unjustified_hedge(issues)
        assert flagged is True

    def test_major_observe_mode_triggers(self):
        issues = [self._major("Gate introduced in observe_mode; should default to enforce")]
        flagged, _ = detect_unjustified_hedge(issues)
        assert flagged is True

    def test_major_without_justification_phrase_triggers(self):
        issues = [self._major("New flag defaults to off without an explicit justification")]
        flagged, _ = detect_unjustified_hedge(issues)
        assert flagged is True

    # --- Negative cases: no hedge or justified hedge → (False, "") --------------

    def test_empty_issues_returns_clean(self):
        # Reviewer approved (no issues) — allowed through
        flagged, reason = detect_unjustified_hedge([])
        assert flagged is False
        assert reason == ""

    def test_unrelated_major_issue_not_flagged(self):
        issues = [self._major("Missing null check in process_task before accessing .id")]
        flagged, _ = detect_unjustified_hedge(issues)
        assert flagged is False

    def test_minor_only_hedge_not_flagged(self):
        # Minor-severity findings do not block auto-approval
        issues = [self._minor("Flag defaults to off with no justification")]
        flagged, _ = detect_unjustified_hedge(issues)
        assert flagged is False

    def test_justified_hedge_allowed_through(self):
        # Reviewer found a hedge but confirmed it is justified by a staged rollout
        issues = [self._major(
            "Flag observe_mode defaults to off, justified by the staged rollout plan "
            "documented in the PR description"
        )]
        flagged, _ = detect_unjustified_hedge(issues)
        assert flagged is False, "hedge with explicit justification must not be flagged"

    def test_justified_by_design_allowed_through(self):
        issues = [self._major("fail_open defaults to True by design for this backward-compat path")]
        flagged, _ = detect_unjustified_hedge(issues)
        assert flagged is False

    def test_returns_tuple_of_bool_and_str(self):
        result = detect_unjustified_hedge([self._major("defaults to off with no justification")])
        assert isinstance(result, tuple) and len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)

    def test_non_dict_entries_skipped_gracefully(self):
        issues = ["not a dict", None, self._major("defaults to off with no justification")]
        flagged, _ = detect_unjustified_hedge(issues)
        assert flagged is True  # valid entry still triggers

    def test_missing_severity_not_flagged(self):
        # Issue with no severity key — not MAJOR/CRITICAL, so not flagged
        issue = {"description": "observe_mode defaults to False with no justification"}
        flagged, _ = detect_unjustified_hedge([issue])
        assert flagged is False

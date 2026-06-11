"""Unit tests for app.issue_ledger.

Covers:
  1. _region_anchor — symbol precedence, line-window derivation, empty fallback.
  2. _region_key — returns None when no file.
  3. build_issue_ledger — state machine:
       DEMANDED → APPLIED → CONTESTED (incident-replay shape)
       DEMANDED → APPLIED → RE_DEMANDED (high-overlap, NOT CONTESTED)
       APPROVE transitions all DEMANDED → APPLIED
       Non-review-iteration entries (no issues, fix tasks) are skipped
       Line-drift: two issues at nearby lines → same region
       Symbol anchors: symbol takes precedence over line window
       Multiple iterations with no contestation → empty ledger
       No file → region is invisible to ledger
  4. format_contested_regions_for_fix / _for_review — non-empty when contested,
       empty when no contested regions.
"""

import pytest

from app.issue_ledger import (
    ISSUE_LEDGER_MAX_CONTESTED,
    _jaccard,
    _region_anchor,
    _region_key,
    build_issue_ledger,
    format_contested_regions_for_fix,
    format_contested_regions_for_review,
)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _rc_entry(iteration: int, issues: list) -> dict:
    """Build a minimal REQUEST_CHANGES review_history entry."""
    return {"iteration": iteration, "verdict": "REQUEST_CHANGES", "issues": issues}


def _approve_entry(iteration: int) -> dict:
    return {"iteration": iteration, "verdict": "APPROVE", "issues": []}


def _issue(
    description: str,
    *,
    severity: str = "CRITICAL",
    file: str = "backend/app/foo.py",
    line: int | None = 10,
    symbol: str | None = None,
) -> dict:
    out: dict = {"description": description, "severity": severity, "file": file}
    if line is not None:
        out["line"] = line
    if symbol is not None:
        out["symbol"] = symbol
    return out


# ── _region_anchor ─────────────────────────────────────────────────────────────


class TestRegionAnchor:
    def test_symbol_takes_precedence_over_line(self):
        issue = {"symbol": "process_user", "line": 42}
        assert _region_anchor(issue) == "sym:process_user"

    def test_symbol_empty_falls_back_to_line(self):
        issue = {"symbol": "", "line": 42}
        # 42 // 40 == 1
        assert _region_anchor(issue) == "win:1"

    def test_symbol_whitespace_falls_back_to_line(self):
        issue = {"symbol": "   ", "line": 0}
        assert _region_anchor(issue) == "win:0"

    def test_line_zero_gives_block_zero(self):
        assert _region_anchor({"line": 0}) == "win:0"

    def test_line_39_gives_block_zero(self):
        assert _region_anchor({"line": 39}) == "win:0"

    def test_line_40_gives_block_one(self):
        assert _region_anchor({"line": 40}) == "win:1"

    def test_line_79_gives_block_one(self):
        assert _region_anchor({"line": 79}) == "win:1"

    def test_line_80_gives_block_two(self):
        assert _region_anchor({"line": 80}) == "win:2"

    def test_no_symbol_no_line_gives_empty(self):
        assert _region_anchor({}) == ""

    def test_invalid_line_gives_empty(self):
        assert _region_anchor({"line": "not-a-number"}) == ""

    def test_none_line_gives_empty(self):
        assert _region_anchor({"line": None}) == ""


# ── _region_key ────────────────────────────────────────────────────────────────


class TestRegionKey:
    def test_returns_none_when_no_file(self):
        assert _region_key({"description": "x"}) is None

    def test_returns_none_when_file_empty(self):
        assert _region_key({"file": "", "line": 5}) is None

    def test_returns_none_when_file_whitespace(self):
        assert _region_key({"file": "  ", "line": 5}) is None

    def test_returns_tuple_with_file_and_anchor(self):
        key = _region_key({"file": "app/foo.py", "line": 10})
        assert key == ("app/foo.py", "win:0")

    def test_symbol_anchor_in_key(self):
        key = _region_key({"file": "app/foo.py", "symbol": "my_func"})
        assert key == ("app/foo.py", "sym:my_func")

    def test_line_drift_same_block(self):
        # Lines 5 and 35 are both in block 0 → same region key
        key_a = _region_key({"file": "f.py", "line": 5})
        key_b = _region_key({"file": "f.py", "line": 35})
        assert key_a == key_b

    def test_line_drift_different_block(self):
        # Lines 5 and 45 are in blocks 0 and 1 → different region keys
        key_a = _region_key({"file": "f.py", "line": 5})
        key_b = _region_key({"file": "f.py", "line": 45})
        assert key_a != key_b

    def test_different_files_different_keys(self):
        key_a = _region_key({"file": "a.py", "line": 10})
        key_b = _region_key({"file": "b.py", "line": 10})
        assert key_a != key_b


# ── Jaccard helper ─────────────────────────────────────────────────────────────


class TestJaccard:
    def test_identical_strings_return_one(self):
        assert _jaccard("replace old constants", "replace old constants") == pytest.approx(
            1.0
        )

    def test_disjoint_strings_return_zero(self):
        assert _jaccard("alpha beta gamma", "delta epsilon zeta") == pytest.approx(0.0)

    def test_empty_string_returns_zero(self):
        assert _jaccard("", "something") == pytest.approx(0.0)

    def test_short_tokens_ignored(self):
        # Tokens < 3 chars are filtered out
        assert _jaccard("a b c", "a b c") == pytest.approx(0.0)

    def test_partial_overlap(self):
        # "replace constants" ↔ "add constants back" → shared: "constants"; total unique: replace, constants, back → 1/3
        j = _jaccard("replace constants", "add constants back")
        assert 0.0 < j < 1.0


# ── build_issue_ledger ─────────────────────────────────────────────────────────


class TestBuildIssueLedger:
    def test_empty_history_returns_empty_ledger(self):
        ledger = build_issue_ledger([])
        assert ledger["contested"] == []
        assert ledger["total_regions"] == 0

    def test_none_history_returns_empty_ledger(self):
        ledger = build_issue_ledger(None)
        assert ledger["contested"] == []

    def test_single_demand_no_contestation(self):
        history = [_rc_entry(0, [_issue("replace frozenset with list")])]
        ledger = build_issue_ledger(history)
        assert ledger["contested"] == []
        assert ledger["total_regions"] == 1

    def test_demand_then_apply_no_contestation(self):
        """Region demanded in iter 0, absent in iter 1 → APPLIED, no contest."""
        history = [
            _rc_entry(0, [_issue("replace frozenset with list")]),
            _rc_entry(1, [_issue("unrelated issue", file="other.py")]),
        ]
        ledger = build_issue_ledger(history)
        assert ledger["contested"] == []

    def test_incident_replay_contested(self):
        """Incident shape: iter 4 demanded 'replace old constants' → applied;
        iter 9 demands 'add them back' → CONTESTED (low Jaccard)."""
        history = [
            _rc_entry(
                4,
                [_issue("replace old constants", file="backend/app/task_evaluators.py", line=42)],
            ),
            # iter 5-8: the region is absent → APPLIED
            _rc_entry(5, [_issue("unrelated issue A", file="other.py")]),
            _rc_entry(6, [_issue("unrelated issue B", file="other.py")]),
            # iter 9: different demand on same region
            _rc_entry(
                9,
                [_issue("add them back", file="backend/app/task_evaluators.py", line=42)],
            ),
        ]
        ledger = build_issue_ledger(history)
        assert len(ledger["contested"]) == 1
        c = ledger["contested"][0]
        assert c["file"] == "backend/app/task_evaluators.py"
        assert "replace old constants" in c["demanded_description"]
        assert "add them back" in c["reappeared_description"]
        assert c["demanded_iteration"] == 4
        assert c["reappeared_iteration"] == 9

    def test_high_overlap_is_re_demanded_not_contested(self):
        """Reappeared demand with high Jaccard to original → RE_DEMANDED, not CONTESTED."""
        # "replace old constants list" and "replace old constants set" share ~4/5 tokens
        history = [
            _rc_entry(0, [_issue("replace old constants list", line=10)]),
            _rc_entry(1, [_issue("unrelated", file="other.py")]),
            _rc_entry(2, [_issue("replace old constants set", line=10)]),
        ]
        ledger = build_issue_ledger(history)
        # High overlap → RE_DEMANDED, not CONTESTED
        assert ledger["contested"] == []

    def test_approve_transitions_demanded_to_applied(self):
        """APPROVE marks all DEMANDED → APPLIED; reappearance after APPROVE is detected."""
        history = [
            _rc_entry(0, [_issue("remove unused import", line=5)]),
            _approve_entry(1),
            # reappears with different demand on same region
            _rc_entry(2, [_issue("add missing import here", line=5)]),
        ]
        ledger = build_issue_ledger(history)
        assert len(ledger["contested"]) == 1
        c = ledger["contested"][0]
        assert "remove unused import" in c["demanded_description"]
        assert "add missing import here" in c["reappeared_description"]

    def test_non_review_entries_are_skipped(self):
        """Entries without verdict or with non-RC/APPROVE verdicts are ignored."""
        history = [
            {"iteration": 0, "feedback": "fix this"},  # no verdict
            {"iteration": 0, "verdict": "APPROVE", "issues": []},
        ]
        ledger = build_issue_ledger(history)
        assert ledger["contested"] == []

    def test_minor_issues_are_not_tracked(self):
        """MINOR severity issues are invisible to the ledger."""
        history = [
            _rc_entry(0, [_issue("fix indentation", severity="MINOR", line=5)]),
            # all MINOR — no CRITICAL/MAJOR in iter 1 either
            _rc_entry(1, [_issue("unrelated spacing", severity="MINOR", file="other.py")]),
            _rc_entry(2, [_issue("reorder imports", severity="MINOR", line=5)]),
        ]
        ledger = build_issue_ledger(history)
        assert ledger["contested"] == []
        assert ledger["total_regions"] == 0

    def test_issues_without_file_are_not_tracked(self):
        """Issues with no file are not trackable as regions."""
        history = [
            {
                "iteration": 0,
                "verdict": "REQUEST_CHANGES",
                "issues": [{"description": "fix missing test", "severity": "CRITICAL"}],
            },
            _rc_entry(1, []),
            {
                "iteration": 2,
                "verdict": "REQUEST_CHANGES",
                "issues": [{"description": "add the test back", "severity": "CRITICAL"}],
            },
        ]
        ledger = build_issue_ledger(history)
        assert ledger["contested"] == []
        assert ledger["total_regions"] == 0

    def test_line_drift_within_window_same_region(self):
        """Lines 5 and 30 both in block 0 → same region → APPLIED → detected."""
        history = [
            _rc_entry(0, [_issue("remove unused import", line=5)]),
            _rc_entry(1, [_issue("unrelated", file="other.py")]),
            _rc_entry(2, [_issue("reorder differently", line=30)]),  # same block
        ]
        ledger = build_issue_ledger(history)
        assert len(ledger["contested"]) == 1

    def test_line_drift_across_window_different_region(self):
        """Lines 5 and 50 are in different blocks → different regions → no contest."""
        history = [
            _rc_entry(0, [_issue("remove unused import", line=5)]),
            _rc_entry(1, [_issue("unrelated", file="other.py")]),
            _rc_entry(2, [_issue("add import here", line=50)]),  # block 1
        ]
        ledger = build_issue_ledger(history)
        assert ledger["contested"] == []

    def test_symbol_anchor_takes_precedence_over_line_window(self):
        """Same symbol on different lines → same region; different symbol → different region."""
        history = [
            _rc_entry(
                0, [_issue("remove frozenset", line=10, symbol="MY_CONSTANTS")]
            ),
            _rc_entry(1, [_issue("unrelated", file="other.py")]),
            # Same file, same symbol, different line → same region
            _rc_entry(
                2, [_issue("add frozenset back", line=999, symbol="MY_CONSTANTS")]
            ),
        ]
        ledger = build_issue_ledger(history)
        assert len(ledger["contested"]) == 1

    def test_contested_capped_at_max(self):
        """Never more than ISSUE_LEDGER_MAX_CONTESTED contested regions."""
        issues_iter0 = [
            _issue(f"demand {i}", file=f"file_{i}.py", line=10)
            for i in range(ISSUE_LEDGER_MAX_CONTESTED + 5)
        ]
        issues_iter2 = [
            _issue(f"reverse {i}", file=f"file_{i}.py", line=10)
            for i in range(ISSUE_LEDGER_MAX_CONTESTED + 5)
        ]
        history = [
            _rc_entry(0, issues_iter0),
            _rc_entry(1, []),  # all APPLIED
            _rc_entry(2, issues_iter2),
        ]
        ledger = build_issue_ledger(history)
        assert len(ledger["contested"]) == ISSUE_LEDGER_MAX_CONTESTED

    def test_multiple_demands_only_contested_on_prior_applied(self):
        """A region that was DEMANDED and remains DEMANDED across two entries
        is NOT contested — it was never APPLIED."""
        history = [
            _rc_entry(0, [_issue("remove deprecated call", line=10)]),
            _rc_entry(1, [_issue("add new call instead", line=10)]),  # still demanded, never applied
        ]
        ledger = build_issue_ledger(history)
        assert ledger["contested"] == []

    def test_total_regions_counts_unique_keys(self):
        """total_regions counts unique (file, anchor) pairs across the whole history."""
        history = [
            _rc_entry(
                0,
                [
                    _issue("issue A", file="a.py", line=5),
                    _issue("issue B", file="b.py", line=5),
                ],
            ),
            _rc_entry(
                1,
                [
                    _issue("issue C", file="a.py", line=5),  # same region as A
                    _issue("issue D", file="c.py", line=5),
                ],
            ),
        ]
        ledger = build_issue_ledger(history)
        # a.py:win:0, b.py:win:0, c.py:win:0 → 3 unique regions
        assert ledger["total_regions"] == 3

    def test_contested_structure_fields(self):
        """Contested dicts have all expected fields."""
        history = [
            _rc_entry(3, [_issue("old way", line=20)]),
            _rc_entry(4, [_issue("unrelated", file="other.py")]),
            _rc_entry(7, [_issue("new way completely different", line=20)]),
        ]
        ledger = build_issue_ledger(history)
        assert len(ledger["contested"]) == 1
        c = ledger["contested"][0]
        assert "file" in c
        assert "anchor" in c
        assert "demanded_iteration" in c
        assert "demanded_description" in c
        assert "reappeared_iteration" in c
        assert "reappeared_description" in c
        assert c["demanded_iteration"] == 3
        assert c["reappeared_iteration"] == 7

    def test_malformed_entries_skipped(self):
        """Non-dict entries and missing keys don't crash the ledger builder."""
        history = [
            None,
            "not a dict",
            _rc_entry(0, [_issue("valid issue")]),
            {},  # missing verdict
            _rc_entry(1, [None, "invalid", _issue("another valid issue", file="b.py")]),
        ]
        ledger = build_issue_ledger(history)
        # Should not raise; contested may be empty depending on overlap
        assert isinstance(ledger["contested"], list)


# ── Prompt formatting ──────────────────────────────────────────────────────────


class TestFormatContestedRegions:
    def _make_contested(self, n: int = 1) -> list:
        return [
            {
                "file": f"app/foo_{i}.py",
                "anchor": f"sym:func_{i}",
                "demanded_iteration": i,
                "demanded_description": f"remove thing {i}",
                "reappeared_iteration": i + 5,
                "reappeared_description": f"add thing {i} back",
            }
            for i in range(n)
        ]

    def test_fix_prefix_empty_when_no_contested(self):
        assert format_contested_regions_for_fix([], "main") == ""

    def test_review_appendix_empty_when_no_contested(self):
        assert format_contested_regions_for_review([], "main") == ""

    def test_fix_prefix_contains_contested_marker(self):
        result = format_contested_regions_for_fix(self._make_contested(), "main")
        assert "CONTESTED" in result

    def test_fix_prefix_contains_file_and_descriptions(self):
        contested = self._make_contested(1)
        result = format_contested_regions_for_fix(contested, "main")
        assert "app/foo_0.py" in result
        assert "remove thing 0" in result
        assert "add thing 0 back" in result

    def test_fix_prefix_contains_base_branch_in_git_command(self):
        result = format_contested_regions_for_fix(self._make_contested(), "automain")
        assert "origin/automain" in result

    def test_review_appendix_contains_contested_marker(self):
        result = format_contested_regions_for_review(self._make_contested(), "main")
        assert "CONTESTED" in result

    def test_review_appendix_mentions_reconcile(self):
        result = format_contested_regions_for_review(self._make_contested(), "main")
        assert "reconcile" in result.lower() or "reconcil" in result.lower()

    def test_review_appendix_contains_base_hint(self):
        result = format_contested_regions_for_review(self._make_contested(), "main")
        assert "git diff" in result
        assert "origin/main" in result

    def test_fix_prefix_contains_iteration_numbers(self):
        contested = [
            {
                "file": "app/foo.py",
                "anchor": "sym:bar",
                "demanded_iteration": 4,
                "demanded_description": "replace old constants",
                "reappeared_iteration": 9,
                "reappeared_description": "add them back",
            }
        ]
        result = format_contested_regions_for_fix(contested, "main")
        assert "iter 4" in result
        assert "iter 9" in result
        assert "replace old constants" in result
        assert "add them back" in result

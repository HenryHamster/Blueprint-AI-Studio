"""Decision-parity tests for the consolidated review-skip policy.

These tests pin the review-skip *decisions* that were previously scattered
across ``review_heuristics.py`` and inline blocks in ``task_lifecycle.py`` and
prove the consolidated ``review_policy`` module produces IDENTICAL outcomes.

Each test compares a faithful reference implementation of the OLD control flow
(the wiring that lived inline in ``task_lifecycle.py``) against the new
``review_policy`` composite functions, across every documented rule vector:

  1. Initial-review skip           → should_skip_initial_review
  2. Auto-approve-small-main gate  → evaluate_auto_approve_small_main
  3. Re-review incl. scan-blocked  → should_skip_re_review

All functions under test are pure (no DB / no async), so these run in the
standalone suite without a database.
"""

import pytest

from app.direction_metrics import _compute_acceptance_rate
from app.review_heuristics import detect_hedges_in_diff
from app.review_policy import (
    AUTO_APPROVE_MIN_ACCEPTANCE_RATE,
    evaluate_auto_approve_small_main,
    should_auto_approve_diff,
    should_skip_initial_review,
    should_skip_re_review,
    should_skip_rereview,
)
from app.shared_state import runtime_config
from testing.factories import make_task


def _task(*, type_="create", scope="small", files=None, diff=None):
    payload = {"type": type_}
    ctx: dict = {"scope": scope}
    if files is not None:
        ctx["_pr_changed_files"] = files
    if diff is not None:
        ctx["_pr_diff_summary"] = diff
    return make_task(payloads=[payload], context=ctx)


# Direction fixtures exercised through the real _compute_acceptance_rate.
_DIR_FAVORABLE = {"history_aggregate": {"total_prs_merged": 9, "total_prs_rejected": 1}}
_DIR_POOR = {"history_aggregate": {"total_prs_merged": 1, "total_prs_rejected": 9}}
_DIR_UNKNOWN: dict = {}  # no PR history → acceptance is None


# ── Reference implementations of the OLD inline control flow ──────────────────


def _old_initial_review_skip(is_intermediate: bool, scope: str) -> tuple[bool, str]:
    """Mirrors the documented initial-review skip table verbatim."""
    if not is_intermediate:
        return (True, "non-intermediate branch: CI and promotion gate backstop review")
    if scope != "large":
        return (True, f"{scope} scope on intermediate branch: rollup PR gets full review")
    return (False, f"{scope} scope on intermediate branch: full review required")


def _old_auto_approve_small_main_skip(
    task, is_intermediate: bool, scope: str, direction_data: dict | None
) -> bool:
    """Mirrors the OLD task_lifecycle inline gate: returns whether
    ``auto_approve_small`` would have been set True."""
    if is_intermediate or scope != "small":
        return False
    if not runtime_config.get("auto_approve_small_diff_enabled", True):
        return False
    acceptance = (
        _compute_acceptance_rate(direction_data) if direction_data is not None else None
    )
    if acceptance is None or acceptance <= AUTO_APPROVE_MIN_ACCEPTANCE_RATE:
        return False
    skip, _ = should_auto_approve_diff(task, direction_data)
    return skip


def _old_rereview_outcome(
    fix_reason: str, skip_rereview_minor_only: bool, scan_blocked: bool
) -> str:
    """Mirrors the OLD three-way task_lifecycle branch:
    ``if not scan_blocked and skip: ci_wait / elif not scan_blocked: spawn / else: blocked``."""
    skip, _ = should_skip_rereview(fix_reason, skip_rereview_minor_only)
    if not scan_blocked and skip:
        return "ci_wait"
    if not scan_blocked:
        return "spawn_review"
    return "blocked"


def _new_rereview_outcome(
    fix_reason: str, skip_rereview_minor_only: bool, scan_blocked: bool
) -> str:
    d = should_skip_re_review(fix_reason, skip_rereview_minor_only, scan_blocked)
    if d.rule == "security_blocked":
        return "blocked"
    if d.skip:
        return "ci_wait"
    return "spawn_review"


# ── Parity: initial-review skip ───────────────────────────────────────────────


_INITIAL_REVIEW_VECTORS = [
    (False, "small"),
    (False, "medium"),
    (False, "large"),
    (True, "small"),
    (True, "medium"),
    (True, "large"),
    (True, "unknown"),
]


class TestInitialReviewSkipParity:
    @pytest.mark.parametrize("is_intermediate,scope", _INITIAL_REVIEW_VECTORS)
    def test_matches_reference(self, is_intermediate, scope):
        assert should_skip_initial_review(is_intermediate, scope) == _old_initial_review_skip(
            is_intermediate, scope
        )

    def test_non_intermediate_always_skips(self):
        skip, _ = should_skip_initial_review(False, "large")
        assert skip is True

    def test_intermediate_large_requires_review(self):
        skip, _ = should_skip_initial_review(True, "large")
        assert skip is False

    def test_intermediate_small_medium_skips(self):
        assert should_skip_initial_review(True, "small")[0] is True
        assert should_skip_initial_review(True, "medium")[0] is True


# ── Parity: auto-approve-small-main gate ──────────────────────────────────────


class TestAutoApproveSmallMainParity:
    def _small_task(self):
        return _task(
            scope="small",
            files=["app/foo.py"],
            diff=[{"file": "app/foo.py", "added": 10, "removed": 2}],
        )

    def _big_task(self):
        return _task(
            scope="small",
            files=["app/foo.py"],
            diff=[{"file": "app/foo.py", "added": 500, "removed": 500}],
        )

    def test_favorable_small_diff_skips(self):
        t = self._small_task()
        d = evaluate_auto_approve_small_main(t, False, "small", _DIR_FAVORABLE)
        assert d.skip is True
        assert d.rule == "auto_approve_small"
        assert d.skip == _old_auto_approve_small_main_skip(t, False, "small", _DIR_FAVORABLE)

    def test_poor_direction_rejected(self):
        t = self._small_task()
        d = evaluate_auto_approve_small_main(t, False, "small", _DIR_POOR)
        assert d.skip is False
        assert d.rule == "direction_acceptance_gate"
        assert d.skip == _old_auto_approve_small_main_skip(t, False, "small", _DIR_POOR)

    def test_unknown_direction_rejected(self):
        t = self._small_task()
        d = evaluate_auto_approve_small_main(t, False, "small", _DIR_UNKNOWN)
        assert d.skip is False
        assert d.rule == "direction_acceptance_gate"
        assert d.skip == _old_auto_approve_small_main_skip(t, False, "small", _DIR_UNKNOWN)

    def test_none_direction_rejected(self):
        t = self._small_task()
        d = evaluate_auto_approve_small_main(t, False, "small", None)
        assert d.skip is False
        assert d.skip == _old_auto_approve_small_main_skip(t, False, "small", None)

    def test_intermediate_never_auto_approves(self):
        t = self._small_task()
        d = evaluate_auto_approve_small_main(t, True, "small", _DIR_FAVORABLE)
        assert d.skip is False
        assert d.skip == _old_auto_approve_small_main_skip(t, True, "small", _DIR_FAVORABLE)

    def test_medium_scope_not_eligible_on_main(self):
        t = _task(
            scope="medium",
            files=["app/foo.py"],
            diff=[{"file": "app/foo.py", "added": 5, "removed": 0}],
        )
        d = evaluate_auto_approve_small_main(t, False, "medium", _DIR_FAVORABLE)
        assert d.skip is False
        assert d.skip == _old_auto_approve_small_main_skip(t, False, "medium", _DIR_FAVORABLE)

    def test_favorable_but_diff_too_big_rejected(self):
        t = self._big_task()
        d = evaluate_auto_approve_small_main(t, False, "small", _DIR_FAVORABLE)
        assert d.skip is False
        assert d.rule == "none"
        assert d.skip == _old_auto_approve_small_main_skip(t, False, "small", _DIR_FAVORABLE)

    def test_disabled_via_config(self, mocker):
        mocker.patch.dict(
            "app.shared_state.runtime_config",
            {"auto_approve_small_diff_enabled": False},
        )
        t = self._small_task()
        d = evaluate_auto_approve_small_main(t, False, "small", _DIR_FAVORABLE)
        assert d.skip is False
        assert d.skip == _old_auto_approve_small_main_skip(t, False, "small", _DIR_FAVORABLE)


# ── Parity: re-review incl. scan-blocked override ─────────────────────────────


_FIX_REASONS = ["ci_failure", "merge_conflict", "migration_guard", "code_review"]


class TestReReviewSkipParity:
    @pytest.mark.parametrize("fix_reason", _FIX_REASONS)
    @pytest.mark.parametrize("minor_only", [True, False])
    @pytest.mark.parametrize("scan_blocked", [True, False])
    def test_matches_reference(self, fix_reason, minor_only, scan_blocked):
        assert _new_rereview_outcome(fix_reason, minor_only, scan_blocked) == _old_rereview_outcome(
            fix_reason, minor_only, scan_blocked
        )

    def test_scan_blocked_overrides_everything(self):
        # Even a skip-eligible ci_failure fix must NOT skip or spawn when blocked.
        d = should_skip_re_review("ci_failure", False, scan_blocked=True)
        assert d.rule == "security_blocked"
        assert d.skip is False

    def test_ci_failure_skips_to_ci_wait(self):
        d = should_skip_re_review("ci_failure", False, scan_blocked=False)
        assert d.skip is True
        assert d.rule == "ci_failure"

    def test_merge_conflict_skips(self):
        d = should_skip_re_review("merge_conflict", False, scan_blocked=False)
        assert d.skip is True
        assert d.rule == "merge_conflict"

    def test_migration_guard_skips(self):
        d = should_skip_re_review("migration_guard", False, scan_blocked=False)
        assert d.skip is True
        assert d.rule == "migration_guard"

    def test_minor_only_skips(self):
        d = should_skip_re_review("code_review", True, scan_blocked=False)
        assert d.skip is True
        assert d.rule == "minor_only"

    def test_plain_code_review_spawns(self):
        d = should_skip_re_review("code_review", False, scan_blocked=False)
        assert d.skip is False
        assert d.rule == "none"


# ── Mechanical hedge detection ────────────────────────────────────────────────


class TestHedgeDetection:
    """Behavioral tests for detect_hedges_in_diff in review_heuristics.

    Each test crafts a ``diff_files`` structure mimicking the return value of
    ``get_pr_files(include_patch=True)``, runs the detector, and asserts on
    the returned findings. The detector is pure / synchronous.
    """

    @staticmethod
    def _diff_file(filename: str, patch: str) -> dict:
        return {"filename": filename, "status": "modified", "patch": patch}

    def test_detects_observe_default(self):
        """A new gate defaulting to ``observe`` produces a finding."""
        diff = [
            self._diff_file(
                "backend/app/some_module.py",
                "@@ -0,0 +1,5 @@\n"
                "+# New feature gate\n"
                "+from app.config import FeatureGate\n"
                "+\n"
                "+_MY_GATE = \"observe\"\n",
            ),
        ]
        findings = detect_hedges_in_diff(diff)
        assert len(findings) >= 1, "observe default should be caught"
        assert any("observe" in f.get("description", "") for f in findings)

    def test_detects_off_default(self):
        """A new config entry defaulting to ``off`` produces a finding."""
        diff = [
            self._diff_file(
                "backend/app/config.py",
                "@@ -10,0 +11,3 @@\n"
                "+    # Pilot mode\n"
                "+    mode = \"off\"\n",
            ),
        ]
        findings = detect_hedges_in_diff(diff)
        assert len(findings) >= 1, "off default should be caught"
        assert any("off" in f.get("description", "") for f in findings)

    def test_detects_disabled_default(self):
        """A new lane defaulting to ``disabled`` produces a finding."""
        diff = [
            self._diff_file(
                "backend/app/lane.py",
                "@@ -5,0 +6 @@\n"
                "+_pilot_lane = \"disabled\"\n",
            ),
        ]
        findings = detect_hedges_in_diff(diff)
        assert len(findings) >= 1, "disabled default should be caught"
        assert any("disabled" in f.get("description", "") for f in findings)

    def test_detects_fail_open_default(self):
        """A mode defaulting to ``fail-open`` produces a finding."""
        diff = [
            self._diff_file(
                "backend/app/mode.py",
                "@@ -0,0 +1,3 @@\n"
                "+# Fail-open mode\n"
                "+default = \"fail-open\"\n",
            ),
        ]
        findings = detect_hedges_in_diff(diff)
        assert len(findings) >= 1, "fail-open default should be caught"
        assert any("fail" in f.get("description", "").lower() for f in findings)

    def test_clean_diff_no_findings(self):
        """A diff with no hedge patterns returns an empty list."""
        diff = [
            self._diff_file(
                "backend/app/normal.py",
                "@@ -1,0 +2,5 @@\n"
                "+def greet(name: str) -> str:\n"
                "+    return f\"Hello, {name}!\"\n"
                "+\n"
                "+MAX_RETRIES = 3\n",
            ),
            self._diff_file(
                "backend/app/other.py",
                "@@ -5,0 +6 @@\n"
                "+enabled = True\n",
            ),
        ]
        findings = detect_hedges_in_diff(diff)
        assert findings == [], f"expected no findings, got {findings}"

    def test_detected_issue_is_major_severity(self):
        """Finding severity is MAJOR (so it survives reconciliation)."""
        diff = [
            self._diff_file(
                "backend/app/foo.py",
                "@@ -0,0 +1 @@\n"
                "+mode = \"observe\"\n",
            ),
        ]
        findings = detect_hedges_in_diff(diff)
        assert len(findings) >= 1
        assert all(f.get("severity") == "MAJOR" for f in findings)

    def test_detected_issue_message_matches_anti_hedge_focus(self):
        """Description contains ``unjustified hedge`` matching the prose in
        ANTI_HEDGE_FOCUS and REVIEW_ANTI_HEDGE_CRITERION."""
        diff = [
            self._diff_file(
                "backend/app/bar.py",
                "@@ -0,0 +1 @@\n"
                "+flag_default = \"off\"\n",
            ),
        ]
        findings = detect_hedges_in_diff(diff)
        assert len(findings) >= 1
        desc = findings[0].get("description", "")
        assert "unjustified hedge" in desc.lower(), (
            f"description should contain 'unjustified hedge', got: {desc}"
        )

    def test_justified_default_off_not_flagged(self):
        """A diff that references a default-off flag in a comment or test does
        NOT produce a false positive — only added lines with the hedge pattern
        trigger the detector."""
        diff = [
            self._diff_file(
                "backend/app/legacy.py",
                "@@ -10,0 +11,2 @@\n"
                "+    # This uses the existing observe-mode flag for monitoring only\n"
                "+    _check_existing_flag(flag_name)\n",
            ),
        ]
        findings = detect_hedges_in_diff(diff)
        assert findings == [], (
            f"comment-only reference should not trigger, got: {findings}"
        )

    def test_removed_lines_skipped(self):
        """Removed lines (diff prefix ``-``) are not scanned."""
        diff = [
            self._diff_file(
                "backend/app/cleanup.py",
                "@@ -5,0 +6,2 @@\n"
                "-_old_mode = \"observe\"\n"
                "+_new_mode = \"committed\"\n",
            ),
        ]
        findings = detect_hedges_in_diff(diff)
        # The removed line has _old_mode = "observe" but it starts with '-'
        # so it should be skipped. The added line has "committed" which is
        # not a hedge value.
        assert findings == [], (
            f"removed lines and non-hedge additions should not trigger, "
            f"got: {findings}"
        )

    def test_multiple_diff_files(self):
        """Multiple files with mixed content — only the file with the hedge
        produces a finding, and the other file is clean."""
        diff = [
            self._diff_file(
                "backend/app/good.py",
                "@@ -1,0 +2 @@\n"
                "+THRESHOLD = 100\n",
            ),
            self._diff_file(
                "backend/app/hedge.py",
                "@@ -10,0 +11 @@\n"
                "+_feature_gate = \"off\"\n",
            ),
            self._diff_file(
                "backend/app/unchanged.py",
                "this file has no patch\n",
            ),
        ]
        findings = detect_hedges_in_diff(diff)
        hedge_files = {f.get("file") for f in findings}
        assert "backend/app/hedge.py" in hedge_files, (
            f"should flag hedge.py, got files: {hedge_files}"
        )
        assert "backend/app/good.py" not in hedge_files

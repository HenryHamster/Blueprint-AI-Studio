"""Calibration tests for the reproducible audit sub-score.

These pin the behaviour of ``compute_reproducible_audit_score`` — the scoring
path that killed the 0.33<->0.92 gating-audit coin-flip by scoring over
challenger-CONFIRMED findings ONLY, anchored by the LLM's low-variance holistic
self-score:

    audit_sub = min(model_reported_score,
                    1 - Sum _AUDIT_DEDUCTIONS[severity] over confirmed findings)

They also assert the end-to-end calibration through ``compute_composite`` at the
REAL gating threshold (0.90) with the smoketest dimension disabled (the shipped
default), so the promote/not-promote boundary is exercised, not just the
sub-score arithmetic.

Pure / in-memory — runs under ``bash scripts/test-backend.sh --standalone
--no-postgres``.  No DB, no session, no I/O.
"""

from __future__ import annotations

import pytest

from app.ci_resolution import _CI_GREEN
from app.release_scoring import (
    _AUDIT_DEDUCTIONS,
    _confirmed_findings_from_binding_verdicts,
    compute_composite,
    compute_reproducible_audit_score,
)

# The SHIPPED gating rubric: audit 0.40 / test_erosion 0.30 / smoketest 0.30,
# threshold 0.90.  These calibration tests deliberately use the REAL threshold
# (not a relaxed test threshold) because the whole point is whether a clean
# candidate clears 0.90 and a confirmed-critical does not.
_POLICY_090 = {
    "weights": {"audit": 0.40, "test_erosion": 0.30, "smoketest": 0.30},
    "threshold": 0.90,
}

_CAND = "cand0" * 8  # 40-char candidate sha stand-in


def _challenge(*verdicts, sha: str = _CAND) -> dict:
    """Build a binding RELEASE_AUDIT_CHALLENGE envelope from verdict dicts."""
    return {"challenged_candidate_sha": sha, "verdicts": list(verdicts)}


def _verdict(verdict: str, severity: str, *, summary: str = "", reason: str = "") -> dict:
    return {
        "verdict": verdict,
        "severity": severity,
        "summary": summary or f"a {severity} finding",
        "file": "app/x.py",
        "reason": reason or ("investigated against full repo — " + "y" * 20),
    }


def _composite(audit_score, *, test_erosion=1.0) -> dict:
    """Run compute_composite the way finalize does with smoketest disabled."""
    return compute_composite(
        ci_green=True,
        audit_score=audit_score,
        test_erosion=test_erosion,
        smoketest_score=None,
        policy=_POLICY_090,
        smoketest_enabled=False,
        ci_state=_CI_GREEN,
        ci_required=True,
    )


# ---------------------------------------------------------------------------
# Guard: the deduction table this scoring path relies on is what we expect.
# ---------------------------------------------------------------------------
def test_deduction_table_anchors():
    assert _AUDIT_DEDUCTIONS["critical"] == 0.30
    assert _AUDIT_DEDUCTIONS["high"] == 0.15


# ---------------------------------------------------------------------------
# 1. Clean candidate — no confirmed findings, model_reported 0.85 -> ~0.85,
#    and through the composite at 0.90 threshold -> promote.
# ---------------------------------------------------------------------------
def test_clean_candidate_scores_model_reported_and_promotes():
    report = {"audit_score": 0.85, "findings": []}
    challenge = _challenge()  # challenge ran, confirmed nothing

    audit = compute_reproducible_audit_score(report, challenge, candidate_sha=_CAND)
    assert audit == pytest.approx(0.85)

    result = _composite(audit)
    assert result["blocker"] is None
    # Renormalized weights with smoketest off: 0.40/0.70 and 0.30/0.70.
    assert result["sub_scores"]["audit"] == pytest.approx(0.85)
    assert result["confidence"] == pytest.approx(0.9143, abs=1e-4)
    assert result["confidence"] >= 0.90
    assert result["promote"] is True


def test_renormalized_weights_breakeven_documented():
    """The clean-candidate margin above 0.90 is thin: with test_erosion=1.0 the
    audit sub-score must clear ~0.825 to promote.  Pin the breakeven so a future
    weight/threshold change surfaces here."""
    # a = 0.825 is the exact breakeven: 0.4/0.7*a + 0.3/0.7*1 == 0.90.
    just_below = _composite(0.82)
    just_above = _composite(0.83)
    assert just_below["promote"] is False
    assert just_above["promote"] is True


# ---------------------------------------------------------------------------
# 2. Confirmed CRITICAL — deduction 0.30, model_reported 0.85 -> 0.70, and
#    through the composite -> NOT promote (a genuinely-bad candidate blocks).
# ---------------------------------------------------------------------------
def test_confirmed_critical_blocks():
    report = {"audit_score": 0.85, "findings": [{"severity": "critical"}]}
    challenge = _challenge(_verdict("confirmed", "critical"))

    audit = compute_reproducible_audit_score(report, challenge, candidate_sha=_CAND)
    assert audit == pytest.approx(0.70)  # min(0.85, 1 - 0.30)

    result = _composite(audit)
    assert result["blocker"] is None  # numeric gate, not a hard blocker here
    assert result["confidence"] == pytest.approx(0.8286, abs=1e-4)
    assert result["confidence"] < 0.90
    assert result["promote"] is False


def test_confirmed_critical_blocks_even_with_perfect_self_score():
    """min() clamps to the confirmed-deduction ceiling regardless of an
    over-optimistic self-score, so a confirmed critical can never promote."""
    report = {"audit_score": 1.0, "findings": [{"severity": "critical"}]}
    challenge = _challenge(_verdict("confirmed", "critical"))
    audit = compute_reproducible_audit_score(report, challenge, candidate_sha=_CAND)
    assert audit == pytest.approx(0.70)
    assert _composite(audit)["promote"] is False


# ---------------------------------------------------------------------------
# 3. Reproducibility: UNCERTAIN (unconfirmed / unbound) findings do NOT erode.
#    A candidate whose challenge left several findings uncertain scores the
#    SAME as the clean candidate — the variance source is removed.
# ---------------------------------------------------------------------------
def test_uncertain_findings_do_not_erode():
    clean_report = {"audit_score": 0.85, "findings": []}
    clean = compute_reproducible_audit_score(
        clean_report, _challenge(), candidate_sha=_CAND
    )

    noisy_report = {
        "audit_score": 0.85,
        "findings": [
            {"severity": "critical"},
            {"severity": "high"},
            {"severity": "medium"},
        ],
    }
    noisy_challenge = _challenge(
        _verdict("uncertain", "critical"),
        _verdict("uncertain", "high"),
        _verdict("uncertain", "medium"),
    )
    noisy = compute_reproducible_audit_score(
        noisy_report, noisy_challenge, candidate_sha=_CAND
    )

    assert noisy == clean == pytest.approx(0.85)
    assert _composite(noisy)["promote"] is True


def test_refuted_findings_do_not_erode():
    """Refuted-false-positive verdicts also contribute nothing (only 'confirmed'
    deducts)."""
    report = {"audit_score": 0.85, "findings": [{"severity": "critical"}]}
    challenge = _challenge(_verdict("refuted_false_positive", "critical"))
    audit = compute_reproducible_audit_score(report, challenge, candidate_sha=_CAND)
    assert audit == pytest.approx(0.85)


def test_only_confirmed_findings_deduct_mixed_batch():
    """A mixed batch deducts ONLY over the confirmed subset."""
    report = {"audit_score": 0.95, "findings": []}
    challenge = _challenge(
        _verdict("confirmed", "high"),  # -0.15
        _verdict("uncertain", "critical"),  # ignored
        _verdict("refuted_false_positive", "high"),  # ignored
        _verdict("confirmed", "medium"),  # -0.07
    )
    audit = compute_reproducible_audit_score(report, challenge, candidate_sha=_CAND)
    # 1 - (0.15 + 0.07) = 0.78; min(0.95, 0.78) = 0.78
    assert audit == pytest.approx(0.78)


# ---------------------------------------------------------------------------
# 4. Challenge absent / unavailable -> fall back to model_reported_score ALONE.
# ---------------------------------------------------------------------------
def test_challenge_absent_falls_back_to_model_reported():
    report = {
        "audit_score": 0.85,
        # Findings present but NO challenge to confirm them: must NOT deduct
        # (the old noisy findings-derived path is never taken).
        "findings": [{"severity": "critical", "verification_note": "x" * 30}],
    }
    audit = compute_reproducible_audit_score(report, None, candidate_sha=_CAND)
    assert audit == pytest.approx(0.85)


def test_challenge_sha_mismatch_falls_back_to_model_reported():
    report = {"audit_score": 0.85, "findings": [{"severity": "critical"}]}
    # Challenge ran against a DIFFERENT candidate -> unusable -> model alone.
    challenge = _challenge(_verdict("confirmed", "critical"), sha="other" * 8)
    audit = compute_reproducible_audit_score(report, challenge, candidate_sha=_CAND)
    assert audit == pytest.approx(0.85)


def test_challenge_unparseable_verdicts_falls_back_to_model_reported():
    report = {"audit_score": 0.85, "findings": []}
    challenge = {"challenged_candidate_sha": _CAND, "verdicts": "not-a-list"}
    audit = compute_reproducible_audit_score(report, challenge, candidate_sha=_CAND)
    assert audit == pytest.approx(0.85)


def test_no_candidate_sha_skips_the_mismatch_guard():
    """When candidate_sha is not supplied the SHA guard is a no-op; the confirmed
    findings are still scored."""
    report = {"audit_score": 0.85, "findings": [{"severity": "critical"}]}
    challenge = _challenge(_verdict("confirmed", "critical"), sha="whatever" * 5)
    audit = compute_reproducible_audit_score(report, challenge)  # no candidate_sha
    assert audit == pytest.approx(0.70)


# ---------------------------------------------------------------------------
# 5. Both absent -> None (unusable audit signal -> compute_composite blocks
#    audit_missing).
# ---------------------------------------------------------------------------
def test_both_absent_unscoreable_report_returns_none():
    # A parseable-but-UNSCOREABLE report (findings present but none carry a usable
    # severity) with no self-score AND no challenge is a genuine no-signal:
    # _score_audit returns None → compute_composite blocks audit_missing.
    report = {"findings": [{"summary": "flagged something", "note": "no severity"}]}
    audit = compute_reproducible_audit_score(report, None, candidate_sha=_CAND)
    assert audit is None
    result = _composite(audit)
    assert result["blocker"] == "audit_missing"
    assert result["promote"] is False


def test_both_absent_clean_report_scores_one():
    # A CLEAN report (empty findings) with no self-score and no challenge is a
    # legitimately clean audit → 1.0, NOT audit_missing/0.0. Regression guard for
    # the fallback that must defer to _score_audit's clean-vs-unscoreable split.
    audit = compute_reproducible_audit_score(
        {"findings": []}, None, candidate_sha=_CAND
    )
    assert audit == pytest.approx(1.0)
    assert _composite(audit)["blocker"] is None


def test_non_dict_report_returns_none():
    """An absent (non-dict) audit report is an unusable signal regardless of the
    challenge — preserves the _score_audit(None) -> None gate behaviour."""
    assert compute_reproducible_audit_score(None, None) is None
    assert (
        compute_reproducible_audit_score(
            None, _challenge(_verdict("confirmed", "critical")), candidate_sha=_CAND
        )
        is None
    )


def test_model_absent_but_confirmed_present_uses_confirmed_deduction():
    """model_reported absent + a usable challenge -> 1 - Sum confirmed_deductions."""
    report = {"findings": []}  # no audit_score
    challenge = _challenge(_verdict("confirmed", "high"))
    audit = compute_reproducible_audit_score(report, challenge, candidate_sha=_CAND)
    assert audit == pytest.approx(0.85)  # 1 - 0.15


# ---------------------------------------------------------------------------
# Helper-level coverage for _confirmed_findings_from_binding_verdicts.
# ---------------------------------------------------------------------------
class TestConfirmedFindingsExtraction:
    def test_none_for_non_dict(self):
        assert _confirmed_findings_from_binding_verdicts(None) is None
        assert _confirmed_findings_from_binding_verdicts([]) is None

    def test_none_for_missing_verdicts_list(self):
        assert (
            _confirmed_findings_from_binding_verdicts({"challenged_candidate_sha": _CAND})
            is None
        )

    def test_empty_list_when_nothing_confirmed(self):
        got = _confirmed_findings_from_binding_verdicts(
            _challenge(_verdict("uncertain", "high"))
        )
        assert got == []

    def test_returns_confirmed_with_severity(self):
        got = _confirmed_findings_from_binding_verdicts(
            _challenge(
                _verdict("confirmed", "Critical"),  # case-normalized
                _verdict("uncertain", "high"),
                _verdict("confirmed", "medium"),
            )
        )
        assert [f["severity"] for f in got] == ["critical", "medium"]

    def test_sha_mismatch_returns_none(self):
        assert (
            _confirmed_findings_from_binding_verdicts(
                _challenge(_verdict("confirmed", "high"), sha="a" * 40),
                candidate_sha=_CAND,
            )
            is None
        )

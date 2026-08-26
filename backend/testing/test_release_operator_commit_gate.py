"""Acceptance evidence for the candidate operator-authorship gate."""

from __future__ import annotations

from app import release_scoring as rs
from app.release_commit_authorship import (
    AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
    default_authorship_evidence_contract,
)


_POLICY = {
    "weights": {"audit": 4 / 7, "test_erosion": 3 / 7},
    "threshold": 0.75,
}
_BASE_SHA = "a" * 40
_CANDIDATE_SHA = "b" * 40


def _receipt(count: int) -> dict:
    return {
        "source": "github_compare",
        "schema_version": AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
        "state": "resolved",
        "count": count,
        "offenders": [
            {"sha": chr(ord("c") + index) * 12, "author": f"operator-{index}"}
            for index in range(count)
        ],
        "range": {"base_sha": _BASE_SHA, "head_sha": _CANDIDATE_SHA},
        "measured_at": "2026-08-11T00:00:00+00:00",
        "retry": {"attempt": 1, "retryable": False, "next_retry_at": None},
    }


def _legacy_v1_receipt(count: int) -> dict:
    """A real legacy blob retained for fail-closed reconciliation coverage."""
    receipt = _receipt(count)
    receipt["schema_version"] = 1
    return receipt


def _measurement(receipt: dict) -> dict:
    return {
        key: receipt[key]
        for key in ("state", "count", "offenders", "range")
        if key in receipt
    }


def _candidate_record(receipt: dict | None):
    return {
        "base_sha": _BASE_SHA,
        "candidate_sha": _CANDIDATE_SHA,
    }


def _score(gate: dict) -> dict:
    return rs.compute_composite(
        ci_green=True,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=1.0,
        policy=_POLICY,
        candidate_operator_authorship_gate=gate,
    )


_ZERO_BUDGET_CATEGORY = {
    "category_key": "candidate-authorship",
    "evidence_contract": {
        "type": "operator_authored_commits",
        "predicate": {"max_count": 0},
    },
}


def test_explicit_authorship_budget_is_surfaced_to_the_operator():
    """An explicitly declared budget is enforced by the candidate gate."""
    category = {
        "category_key": "c-operator-authorship",
        "evidence_contract": default_authorship_evidence_contract(),
    }
    gate = rs.evaluate_candidate_operator_authorship_gate(
        _candidate_record(_receipt(1)),
        _measurement(_receipt(1)),
        [category],
    )

    assert category["evidence_contract"]["predicate"]["max_count"] == 0
    assert gate["status"] == "blocked"
    assert gate["max_count"] == 0
    assert _score(gate)["blocker"] == "candidate_operator_authorship_nonzero"


def test_candidate_zero_operator_commits_passes():
    """With a declared zero-budget contract, zero operator commits passes."""
    gate = rs.evaluate_candidate_operator_authorship_gate(
        _candidate_record(_receipt(0)),
        _measurement(_receipt(0)),
        [_ZERO_BUDGET_CATEGORY],
    )

    result = _score(gate)

    assert gate["status"] == "passed"
    assert gate["count"] == 0
    assert result["promote"] is True
    assert result["blocker"] is None
    assert result["sub_scores"]["candidate_operator_authorship"]["count"] == 0


def test_candidate_nonzero_operator_commits_blocks_distinctly():
    """With a declared zero-budget contract, a nonzero count hard-blocks."""
    gate = rs.evaluate_candidate_operator_authorship_gate(
        _candidate_record(_receipt(1)),
        _measurement(_receipt(1)),
        [_ZERO_BUDGET_CATEGORY],
    )

    result = _score(gate)

    assert result["promote"] is False
    assert result["blocker"] == "candidate_operator_authorship_nonzero"
    assert result["failing_gates"] == ["candidate_operator_authorship_nonzero"]
    assert result["blocker"] != "criterion_coverage_incomplete"


def test_candidate_no_contract_declared_is_unconfigured_and_blocks():
    """No active category declares an authorship evidence contract at all.

    The gate must not manufacture a zero budget, but it also must not treat
    the absence of policy as earned evidence. It reports a distinct
    non-passing ``"unconfigured"`` verdict, even when the measured count is
    nonzero.
    """
    gate = rs.evaluate_candidate_operator_authorship_gate(
        _candidate_record(_receipt(3)), _measurement(_receipt(3))
    )

    result = _score(gate)

    assert gate["status"] == "unconfigured"
    assert gate["passed"] is False
    assert gate["count"] == 3
    assert gate["max_count"] is None
    assert gate["blocker"] == "candidate_operator_authorship_evidence_missing"
    assert gate["reason"] == "no_operator_authorship_contract_declared"
    assert result["promote"] is False
    assert result["blocker"] == "candidate_operator_authorship_evidence_missing"
    assert result["sub_scores"]["candidate_operator_authorship"]["status"] == (
        "unconfigured"
    )


def test_candidate_absent_or_unresolvable_evidence_fails_closed():
    absent = rs.evaluate_candidate_operator_authorship_gate(
        _candidate_record(None), None
    )
    invalid = rs.evaluate_candidate_operator_authorship_gate(
        _candidate_record(_receipt(0)), {"state": "unresolved", "reason": "timeout"}
    )

    absent_result = _score(absent)
    invalid_result = _score(invalid)

    assert absent_result["promote"] is False
    assert absent_result["blocker"] == "candidate_operator_authorship_evidence_missing"
    assert invalid_result["promote"] is False
    assert invalid_result["blocker"] == "candidate_operator_authorship_evidence_missing"


def test_legacy_v1_receipt_is_not_a_candidate_measurement():
    legacy = {"state": "resolved", "count": "0", "offenders": []}
    gate = rs.evaluate_candidate_operator_authorship_gate(
        _candidate_record(legacy), legacy
    )

    assert gate["status"] == "unresolved"
    assert gate["reason"] == "malformed_authorship_measurement"
    assert _score(gate)["blocker"] == "candidate_operator_authorship_evidence_missing"

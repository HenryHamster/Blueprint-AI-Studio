"""Acceptance evidence for the prior-release operator-authorship gate."""

from __future__ import annotations

from app import release_scoring as rs


_POLICY = {
    "weights": {"audit": 4 / 7, "test_erosion": 3 / 7},
    "threshold": 0.75,
}
_BASE_SHA = "a" * 40
_SERVED_SHA = "b" * 40


def _receipt(count: int) -> dict:
    return {
        "source": "github_compare",
        "schema_version": 1,
        "state": "resolved",
        "count": count,
        "offenders": [
            {"sha": chr(ord("c") + index) * 12, "author": f"operator-{index}"}
            for index in range(count)
        ],
        "range": {"base_sha": _BASE_SHA, "head_sha": _SERVED_SHA},
        "measured_at": "2026-08-11T00:00:00+00:00",
        "retry": {"attempt": 1, "retryable": False, "next_retry_at": None},
    }


def _prior_record(receipt: dict | None, *, serving_status: str = "serving_confirmed"):
    return {
        "status": "promoted",
        "serving_status": serving_status,
        "base_sha": _BASE_SHA,
        "served_sha": _SERVED_SHA,
        "last_qualification": (
            {"operator_authored_commits": receipt} if receipt is not None else {}
        ),
    }


def _score(gate: dict) -> dict:
    return rs.compute_composite(
        ci_green=True,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=1.0,
        policy=_POLICY,
        prior_operator_authorship_gate=gate,
    )


def test_prior_promoted_zero_operator_commits_passes():
    gate = rs.evaluate_prior_operator_authorship_gate(
        _prior_record(_receipt(0))
    )

    result = _score(gate)

    assert gate["status"] == "passed"
    assert gate["count"] == 0
    assert result["promote"] is True
    assert result["blocker"] is None
    assert result["sub_scores"]["prior_operator_authorship"]["count"] == 0


def test_prior_promoted_nonzero_operator_commits_blocks_distinctly():
    gate = rs.evaluate_prior_operator_authorship_gate(
        _prior_record(_receipt(1))
    )

    result = _score(gate)

    assert result["promote"] is False
    assert result["blocker"] == "prior_operator_authorship_nonzero"
    assert result["failing_gates"] == ["prior_operator_authorship_nonzero"]
    assert result["blocker"] != "criterion_coverage_incomplete"


def test_prior_promoted_absent_or_unresolvable_evidence_fails_closed():
    absent = rs.evaluate_prior_operator_authorship_gate(_prior_record(None))
    invalid = rs.evaluate_prior_operator_authorship_gate(
        _prior_record(_receipt(0), serving_status="confirmation_timed_out")
    )

    absent_result = _score(absent)
    invalid_result = _score(invalid)

    assert absent_result["promote"] is False
    assert absent_result["blocker"] == "prior_operator_authorship_evidence_missing"
    assert invalid_result["promote"] is False
    assert invalid_result["blocker"] == "prior_operator_authorship_evidence_missing"


def test_no_prior_promoted_version_is_explicit_bootstrap_pass():
    gate = rs.evaluate_prior_operator_authorship_gate(None)

    result = _score(gate)

    assert gate == {
        "status": "bootstrap",
        "passed": True,
        "count": None,
        "reason": "no_prior_promoted_version",
    }
    assert result["promote"] is True
    assert result["blocker"] is None

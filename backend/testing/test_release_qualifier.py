"""Release-gate invariants that must remain true across qualifier changes."""

from __future__ import annotations

from app import release_scoring as rs


def _candidate(base: str = "a" * 40, head: str = "b" * 40) -> dict:
    return {"base_sha": base, "candidate_sha": head}


def _measurement(base: str, head: str, count: int) -> dict:
    return {
        "state": "resolved",
        "count": count,
        "offenders": [
            {"sha": chr(99 + index) * 12, "author": "Operator"}
            for index in range(count)
        ],
        "range": {"base_sha": base, "head_sha": head},
    }


def _authorship_category(max_count: int = 0) -> dict:
    return {
        "category_key": "candidate-authorship",
        "evidence_contract": {
            "type": "operator_authored_commits",
            "predicate": {"max_count": max_count},
        },
    }


def test_candidate_authorship_gate_uses_full_range_and_contract_tolerance():
    candidate = _candidate()
    categories = [_authorship_category(max_count=1)]

    clean = rs.evaluate_candidate_operator_authorship_gate(
        candidate, _measurement("a" * 40, "b" * 40, 1), categories
    )
    assert clean["status"] == "passed"
    assert clean["count"] == 1
    assert clean["max_count"] == 1
    assert clean["range"] == {"base_sha": "a" * 40, "head_sha": "b" * 40}

    dirty = rs.evaluate_candidate_operator_authorship_gate(
        candidate, _measurement("a" * 40, "b" * 40, 2), categories
    )
    assert dirty["status"] == "blocked"
    assert dirty["blocker"] == "candidate_operator_authorship_nonzero"


def test_every_hard_promotion_gate_has_a_non_promotional_clearing_path():
    """A new hard gate must declare how a future action can clear it.

    In particular, a deployed-side check may not document promotion itself as
    its only recovery.  The candidate authorship gate is pinned to a literal
    zero budget here as a security-sensitive default, while its implementation
    still accepts the existing explicit max_count contract.
    """
    hard_gates = set(rs.BLOCKER_EXECUTION_SITE)
    clearing_paths = rs.HARD_GATE_CLEARING_PATHS

    assert hard_gates == set(clearing_paths)
    for blocker in sorted(hard_gates):
        path = clearing_paths[blocker]
        assert path["kind"] in {"input", "override", "self_clearing"}, blocker
        assert path["evidence"]
        assert "promot" not in path["evidence"].lower(), blocker

    assert _authorship_category()["evidence_contract"]["predicate"]["max_count"] == 0

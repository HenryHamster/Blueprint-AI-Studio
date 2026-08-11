"""Acceptance evidence for candidate-bound release bootstrap rehearsal grading."""

from app.release_prepromotion_rehearsal import (
    apply_rehearsal_evidence,
    persisted_rehearsal_for_candidate,
)


_CANDIDATE = "a" * 40
_CATEGORY = {
    "category_key": "c-bootstrap",
    "text": (
        "The release bootstrap is rehearsed against the candidate before promotion "
        "and proves the privilege boundary and migration preconditions."
    ),
}
_VERDICT = {
    "category_key": "c-bootstrap",
    "status": "partial",
    "tier": "judged",
    "gap": "no host rehearsal receipt",
    "citations": [],
}


def _receipt(revision: str = _CANDIDATE) -> dict:
    return {
        "kind": "blueprint.local_app_release_rehearsal.v1",
        "candidate": "registry.example/app@sha256:" + "b" * 64,
        "revision": revision,
        "checked": [
            "live systemd privilege boundary",
            "worker secret and static migration inputs",
            "candidate migration reachability from live schema",
        ],
        "deferred": ["migration apply and post-migration schema verification"],
        "units_stopped": False,
    }


def test_valid_candidate_receipt_grades_rehearsal_category_met():
    result = apply_rehearsal_evidence(
        [_VERDICT], [_CATEGORY], _receipt(), _CANDIDATE
    )

    assert result is not None
    assert result[0]["status"] == "met"
    assert result[0]["tier"] == "machine"
    assert result[0]["citations"] == [
        {"kind": "test", "ref": "release_prepromotion_rehearsal"}
    ]


def test_missing_receipt_fails_closed_as_not_met():
    result = apply_rehearsal_evidence([_VERDICT], [_CATEGORY], None, _CANDIDATE)

    assert result == [_VERDICT]
    assert result[0]["status"] == "partial"


def test_receipt_for_different_candidate_does_not_satisfy_candidate():
    other_sha = "c" * 40
    receipt = _receipt(other_sha)

    assert persisted_rehearsal_for_candidate(receipt, _CANDIDATE) is None
    result = apply_rehearsal_evidence(
        [_VERDICT], [_CATEGORY], receipt, _CANDIDATE
    )
    assert result == [_VERDICT]
    assert result[0]["status"] != "met"

"""Acceptance evidence for candidate-bound release bootstrap rehearsal grading."""

import pytest
from unittest.mock import AsyncMock, patch

from app.release_prepromotion_rehearsal import (
    apply_rehearsal_evidence,
    dispatch_prepromotion_rehearsal,
    prepromotion_rehearsal_ref,
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

    assert result is not None
    assert result[0]["status"] == "not_met"
    assert result[0]["gap"] == "candidate-bound pre-promotion rehearsal receipt is absent"


@pytest.mark.asyncio
async def test_rehearsal_dispatch_pins_release_branch_and_disables_side_effects():
    dispatch = AsyncMock(return_value=True)
    with patch(
        "app.release_prepromotion_rehearsal.github.dispatch_workflow_run",
        new=dispatch,
    ):
        assert await dispatch_prepromotion_rehearsal(
            "token", "owner", "repo", 17, project_id="project"
        )

    dispatch.assert_awaited_once()
    assert dispatch.await_args.args[:5] == (
        "token",
        "owner",
        "repo",
        "deploy.yml",
        "release-v17",
    )
    assert dispatch.await_args.kwargs == {
        "project_id": "project",
        "inputs": {
            "deploy_backend": "true",
            "deploy_frontend": "false",
            "deploy_agent": "false",
            "run_migrations": "false",
            "recovery_data_reconciled": "false",
        },
    }


def test_rehearsal_ref_rejects_non_positive_or_bool_version_numbers():
    with pytest.raises(ValueError, match="positive"):
        prepromotion_rehearsal_ref(0)
    with pytest.raises(ValueError, match="integer"):
        prepromotion_rehearsal_ref(True)


def test_receipt_for_different_candidate_does_not_satisfy_candidate():
    other_sha = "c" * 40
    receipt = _receipt(other_sha)

    assert persisted_rehearsal_for_candidate(receipt, _CANDIDATE) is None
    result = apply_rehearsal_evidence(
        [_VERDICT], [_CATEGORY], receipt, _CANDIDATE
    )
    assert result is not None
    assert result[0]["status"] == "not_met"
    assert result[0]["gap"] == "candidate-bound pre-promotion rehearsal receipt is absent"

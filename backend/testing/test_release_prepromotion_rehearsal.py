"""Acceptance evidence for candidate-bound release bootstrap rehearsal grading."""

import pytest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from _release_qualifier_testhelpers import _make_project_and_version
from app import release_qualifier as rq
from app.release_prepromotion_rehearsal import PREPROMOTION_REHEARSAL_INPUTS

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


@pytest.mark.asyncio
async def test_qualification_path_dispatches_rehearsal_without_admin(
    db_session, mocker
):
    """The automated qualification launch owns the rehearsal dispatch.

    The mocked workflow completion writes the same candidate-bound receipt that
    the real host callback persists; no admin router is called or required.
    """
    project, version = await _make_project_and_version(
        db_session, phase="stabilize"
    )
    candidate = "c0de1234" * 5

    async def _dispatch_and_complete(*args, **kwargs):
        version.prepromotion_rehearsal_receipt = _receipt(candidate)
        return True

    dispatch = mocker.patch(
        "app.release_prepromotion_rehearsal.github.dispatch_workflow_run",
        new=AsyncMock(side_effect=_dispatch_and_complete),
    )
    mocker.patch("app.github.get_repo_installation_token", new=AsyncMock(return_value="gh-token"))
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    mocker.patch(
        "app.release_qualifier._resolve_candidate_ci",
        new=AsyncMock(return_value={"ci_green": True, "state": "green"}),
    )
    mocker.patch(
        "app.release_qualifier.check_qualification_migration_identity",
        new=AsyncMock(return_value=("ok", None)),
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled", return_value=True
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert result["status"] == "spawned"
    assert version.last_qualification["candidate_sha"] == candidate
    assert version.last_qualification["prepromotion_rehearsal_dispatch"] == {
        "status": "dispatched",
        "candidate_sha": candidate,
    }
    receipt = persisted_rehearsal_for_candidate(
        version.prepromotion_rehearsal_receipt, candidate
    )
    assert receipt is not None
    assert receipt["revision"] == candidate
    dispatch.assert_awaited_once()
    assert dispatch.await_args.args[:5] == (
        "gh-token",
        "acme",
        "widget",
        "deploy.yml",
        "release-v1",
    )
    assert dispatch.await_args.kwargs["inputs"] == PREPROMOTION_REHEARSAL_INPUTS
    assert dispatch.await_args.kwargs["inputs"]["deploy_frontend"] == "false"
    assert dispatch.await_args.kwargs["inputs"]["deploy_agent"] == "false"


@pytest.mark.asyncio
async def test_qualification_rehearsal_dispatch_failure_is_a_gate_error(
    db_session, mocker
):
    project, version = await _make_project_and_version(
        db_session, phase="stabilize"
    )
    candidate = "deadbeef" * 5
    mocker.patch(
        "app.release_prepromotion_rehearsal.github.dispatch_workflow_run",
        new=AsyncMock(return_value=False),
    )
    mocker.patch("app.github.get_repo_installation_token", new=AsyncMock(return_value="gh-token"))
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    mocker.patch(
        "app.release_qualifier._resolve_candidate_ci",
        new=AsyncMock(return_value={"ci_green": True, "state": "green"}),
    )
    mocker.patch(
        "app.release_qualifier.check_qualification_migration_identity",
        new=AsyncMock(return_value=("ok", None)),
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled", return_value=True
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert result["status"] == "prepromotion_rehearsal_dispatch_failed"
    assert result["blocker"] == "prepromotion_rehearsal_dispatch_failed"
    assert version.last_qualification["error"] == (
        "prepromotion_rehearsal_dispatch_failed"
    )
    assert version.last_qualification["prepromotion_rehearsal_dispatch"]["status"] == (
        "dispatch_failed"
    )
    assert version.prepromotion_rehearsal_receipt is None


@pytest.mark.asyncio
async def test_same_candidate_rehearsal_marker_suppresses_duplicate_dispatch(mocker):
    candidate = "f00dbabe" * 5
    version = SimpleNamespace(
        id=uuid.uuid4(),
        version_number=3,
        prepromotion_rehearsal_receipt=None,
        last_qualification={
            "prepromotion_rehearsal_dispatch": {
                "status": "dispatched",
                "candidate_sha": candidate,
            }
        },
    )
    project = SimpleNamespace(id=uuid.uuid4())
    dispatch = mocker.patch(
        "app.release_prepromotion_rehearsal.github.dispatch_workflow_run",
        new=AsyncMock(return_value=True),
    )

    ready, state = await rq._ensure_prepromotion_rehearsal_dispatch(
        version,
        project,
        token="token",
        owner="owner",
        repo="repo",
        candidate_sha=candidate,
    )

    assert ready is True
    assert state == version.last_qualification["prepromotion_rehearsal_dispatch"]
    dispatch.assert_not_awaited()


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

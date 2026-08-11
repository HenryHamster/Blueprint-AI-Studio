"""Acceptance evidence for candidate-bound release bootstrap rehearsal grading."""

import pytest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from _release_qualifier_testhelpers import _make_project_and_version
from app import release_qualifier as rq
from app.release_prepromotion_rehearsal import PREPROMOTION_REHEARSAL_INPUTS

from app.release_prepromotion_rehearsal import (
    apply_rehearsal_evidence,
    dispatch_prepromotion_rehearsal,
    ensure_prepromotion_rehearsal_ref,
    prepromotion_rehearsal_ref,
    persisted_rehearsal_for_candidate,
)


_CANDIDATE = "a" * 40
_CATEGORY = {
    "category_key": "c-cafe3815",
    "text": (
        "The release bootstrap is rehearsed against the candidate before promotion "
        "and proves the privilege boundary and migration preconditions."
    ),
}
_VERDICT = {
    "category_key": "c-cafe3815",
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


def test_reworded_rehearsal_criterion_still_binds_by_stable_key():
    reworded = {
        **_CATEGORY,
        "text": "The candidate release has passed its host safety contract.",
    }

    result = apply_rehearsal_evidence(
        [_VERDICT], [reworded], _receipt(), _CANDIDATE
    )

    assert result is not None
    assert result[0]["category_key"] == "c-cafe3815"
    assert result[0]["status"] == "met"


def test_receipt_omitting_a_pre_stop_precondition_fails_closed():
    receipt = _receipt()
    receipt["checked"] = receipt["checked"][1:]

    assert persisted_rehearsal_for_candidate(receipt, _CANDIDATE) is None
    result = apply_rehearsal_evidence(
        [_VERDICT], [_CATEGORY], receipt, _CANDIDATE
    )

    assert result is not None
    assert result[0]["status"] == "not_met"


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
    ), patch(
        "app.release_prepromotion_rehearsal.github.create_branch",
        new=AsyncMock(return_value={"object": {"sha": _CANDIDATE}}),
    ):
        assert await dispatch_prepromotion_rehearsal(
            "token", "owner", "repo", 17, _CANDIDATE, project_id="project"
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
async def test_rehearsal_dispatch_sees_candidate_ref_at_exact_candidate_sha(mocker):
    refs: dict[str, str] = {}
    version = SimpleNamespace(
        version_number=17,
        prepromotion_rehearsal_receipt=None,
        last_qualification={},
    )
    project = SimpleNamespace(id="project")

    async def _create_branch(_token, _owner, _repo, branch_name, sha):
        refs[branch_name] = sha
        return {
            "ref": f"refs/heads/{branch_name}",
            "object": {"sha": sha},
        }

    async def _dispatch(_token, _owner, _repo, _workflow, ref, **_kwargs):
        assert refs.get(ref) == _CANDIDATE
        return True

    create_branch = mocker.patch(
        "app.release_prepromotion_rehearsal.github.create_branch",
        new=AsyncMock(side_effect=_create_branch),
    )
    dispatch = mocker.patch(
        "app.release_prepromotion_rehearsal.github.dispatch_workflow_run",
        new=AsyncMock(side_effect=_dispatch),
    )
    mocker.patch("app.release_qualifier.flag_modified")

    ready, state = await rq._ensure_prepromotion_rehearsal_dispatch(
        version,
        project,
        token="token",
        owner="owner",
        repo="repo",
        candidate_sha=_CANDIDATE,
    )
    assert ready is True
    assert state == {"status": "dispatched", "candidate_sha": _CANDIDATE}
    assert refs["release-v17"] == _CANDIDATE
    create_branch.assert_awaited_once_with(
        "token", "owner", "repo", "release-v17", _CANDIDATE
    )
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_rehearsal_ref_recuts_stale_existing_ref(mocker):
    stale_sha = "b" * 40
    create_branch = mocker.patch(
        "app.release_prepromotion_rehearsal.github.create_branch",
        new=AsyncMock(
            side_effect=[
                {"object": {"sha": stale_sha}, "reused": True},
                {"object": {"sha": _CANDIDATE}, "reused": False},
            ]
        ),
    )
    delete_branch = mocker.patch(
        "app.release_prepromotion_rehearsal.github.delete_branch",
        new=AsyncMock(return_value=True),
    )

    assert (
        await ensure_prepromotion_rehearsal_ref(
            "token", "owner", "repo", 17, _CANDIDATE
        )
        == "release-v17"
    )
    assert create_branch.await_args_list == [
        call("token", "owner", "repo", "release-v17", _CANDIDATE),
        call("token", "owner", "repo", "release-v17", _CANDIDATE),
    ]
    delete_branch.assert_awaited_once_with(
        "token", "owner", "repo", "release-v17"
    )


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
    mocker.patch(
        "app.release_prepromotion_rehearsal.github.create_branch",
        new=AsyncMock(
            return_value={
                "ref": "refs/heads/release-v1",
                "object": {"sha": candidate},
            }
        ),
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
    mocker.patch(
        "app.release_prepromotion_rehearsal.github.create_branch",
        new=AsyncMock(
            return_value={
                "ref": "refs/heads/release-v1",
                "object": {"sha": candidate},
            }
        ),
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
@pytest.mark.parametrize(
    "terminal_status",
    ["migration_identity_collision", "spawn_failed"],
)
async def test_post_dispatch_error_marker_suppresses_retry(
    mocker, terminal_status
):
    candidate = "f00dbabe" * 5
    version = SimpleNamespace(
        id=uuid.uuid4(),
        version_number=3,
        prepromotion_rehearsal_receipt=None,
        last_qualification={
            "status": terminal_status,
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

"""Exact qualified-tree inheritance for the integration drift sentinel."""

from _release_qualifier_testhelpers import *  # noqa: F403


_CANDIDATE = "1" * 40
_PROMOTED = "2" * 40
_HEAD = "3" * 40
_TREE = "a" * 40


async def _promoted_predecessor(db_session):
    project, predecessor = await _make_project_and_version(
        db_session, phase="stabilize"
    )
    predecessor.status = "promoted"
    predecessor.candidate_sha = _CANDIDATE
    predecessor.promoted_sha = _PROMOTED
    db_session.add(predecessor)
    await db_session.commit()
    successor = await rv.open_version(
        db_session,
        project.id,
        version_number=2,
        base_sha=_HEAD,
        phase="build",
    )
    await db_session.commit()
    await db_session.refresh(predecessor)
    await db_session.refresh(successor)
    return project, predecessor, successor


def _proof_mocks(
    mocker, *, identities=None, promotion_ci=True, initial_state="no_signal"
):
    gate = mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            side_effect=[
                {"gate_state": initial_state, "head_sha": _HEAD},
                {"gate_state": "dispatched", "head_sha": _HEAD},
            ]
        ),
    )
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("token", "acme", "widget")),
    )
    promotion_receipt = mocker.patch(
        "app.release_qualifier._resolve_exact_promotion_receipt",
        new=AsyncMock(
            return_value={
                "pr_number": 18072,
                "pr_url": "https://github.test/pull/18072",
                "merge_commit_sha": _PROMOTED,
                "empty_diff": False,
            }
        ),
    )
    ci = (
        {
            "run_id": 33121581984,
            "status": "completed",
            "conclusion": "success",
            "html_url": "https://github.test/runs/33121581984",
            "head_sha": _CANDIDATE,
            "event": "pull_request",
            "head_branch": "release-v1",
        }
        if promotion_ci
        else None
    )
    ci_mock = mocker.patch(
        "app.github.get_workflow_run_for_sha",
        new=AsyncMock(return_value=ci),
    )
    identities = identities or {
        _HEAD: {"sha": _HEAD, "tree_sha": _TREE, "parents": [_CANDIDATE, _PROMOTED]},
        _CANDIDATE: {"sha": _CANDIDATE, "tree_sha": _TREE, "parents": []},
        _PROMOTED: {"sha": _PROMOTED, "tree_sha": _TREE, "parents": []},
    }
    identity_mock = mocker.patch(
        "app.github.get_git_commit_identity",
        new=AsyncMock(side_effect=lambda _t, _o, _r, sha, **_kw: identities.get(sha)),
    )
    return gate, promotion_receipt, ci_mock, identity_mock


async def _run(db_session, version_id):
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel

    return await _check_full_suite_drift_sentinel(
        version_id,
        session_factory=_shared_session_factory(db_session),
    )


@pytest.mark.parametrize("initial_state", ["no_signal", "cancelled"])
@pytest.mark.asyncio
async def test_exact_qualified_tree_inheritance_skips_dispatch_and_retires_incidents(
    db_session, mocker, initial_state
):
    from app.models import AlertSeverity
    from app.project_telemetry import read_telemetry
    from app.release_drift_sentinel_alerts import (
        STATE_RED,
        raise_drift_sentinel_alert,
    )
    from app.release_qualifier_sweep import _DRIFT_SENTINEL_INHERITED_TREE_KEY

    project, predecessor, version = await _promoted_predecessor(db_session)
    await raise_drift_sentinel_alert(
        db_session,
        project_id=project.id,
        state=STATE_RED,
        severity=AlertSeverity.ERROR,
        title="prior red",
        message="prior red",
    )
    await db_session.commit()
    gate, promotion_receipt, ci_mock, identity_mock = _proof_mocks(
        mocker, initial_state=initial_state
    )

    assert await _run(db_session, version.id) is False
    assert gate.await_count == 1
    assert promotion_receipt.await_count == 1
    assert ci_mock.await_count == 1
    assert identity_mock.await_count == 3
    receipt = await read_telemetry(
        db_session, project.id, _DRIFT_SENTINEL_INHERITED_TREE_KEY
    )
    assert receipt is not None
    assert {
        key: receipt[key]
        for key in (
            "kind",
            "open_version_id",
            "promoted_version_id",
            "github_owner",
            "github_repo",
            "head_sha",
            "head_parents",
            "candidate_sha",
            "promoted_sha",
            "tree_sha",
            "promotion_pr_number",
            "promotion_ci_run_id",
        )
    } == {
        "kind": "drift_sentinel_inherited_tree",
        "open_version_id": str(version.id),
        "promoted_version_id": str(predecessor.id),
        "github_owner": "acme",
        "github_repo": "widget",
        "head_sha": _HEAD,
        "head_parents": [_CANDIDATE, _PROMOTED],
        "candidate_sha": _CANDIDATE,
        "promoted_sha": _PROMOTED,
        "tree_sha": _TREE,
        "promotion_pr_number": 18072,
        "promotion_ci_run_id": 33121581984,
    }
    open_alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.event_type == "release.ci_unresolved",
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert open_alerts == []


@pytest.mark.asyncio
async def test_inherited_receipt_is_not_reused_after_repository_repoint(
    db_session, mocker
):
    project, _predecessor, version = await _promoted_predecessor(db_session)
    gate, promotion_receipt, _ci, identity_mock = _proof_mocks(mocker)
    assert await _run(db_session, version.id) is False
    assert promotion_receipt.await_count == 1

    project.github_repo = "fork"
    db_session.add(project)
    await db_session.commit()
    gate.reset_mock()
    gate.side_effect = [
        {"gate_state": "no_signal", "head_sha": _HEAD},
        {"gate_state": "dispatched", "head_sha": _HEAD},
    ]
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("token", "acme", "fork")),
    )
    identity_mock.reset_mock()
    identity_mock.side_effect = None
    identity_mock.return_value = None

    assert await _run(db_session, version.id) is True
    assert gate.await_count == 2
    assert promotion_receipt.await_count == 1
    assert identity_mock.await_count == 1


@pytest.mark.parametrize("field", ["promotion_pr_number", "promotion_ci_run_id"])
@pytest.mark.asyncio
async def test_boolean_external_receipt_ids_fail_closed_to_dispatch(
    db_session, mocker, field
):
    _project, _predecessor, version = await _promoted_predecessor(db_session)
    gate, promotion_receipt, ci_mock, _identity = _proof_mocks(mocker)
    if field == "promotion_pr_number":
        promotion_receipt.return_value = {
            **promotion_receipt.return_value,
            "pr_number": True,
        }
    else:
        ci_mock.return_value = {**ci_mock.return_value, "run_id": True}

    assert await _run(db_session, version.id) is True
    assert gate.await_count == 2


@pytest.mark.parametrize("field", ["promotion_pr_number", "promotion_ci_run_id"])
@pytest.mark.asyncio
async def test_boolean_ids_in_durable_receipt_do_not_replay(
    db_session, mocker, field
):
    from app.project_telemetry import read_telemetry, upsert_telemetry
    from app.release_qualifier_sweep import _DRIFT_SENTINEL_INHERITED_TREE_KEY

    project, _predecessor, version = await _promoted_predecessor(db_session)
    gate, promotion_receipt, _ci, identity_mock = _proof_mocks(mocker)
    assert await _run(db_session, version.id) is False
    receipt = await read_telemetry(
        db_session, project.id, _DRIFT_SENTINEL_INHERITED_TREE_KEY
    )
    receipt[field] = True
    await upsert_telemetry(
        db_session,
        project.id,
        _DRIFT_SENTINEL_INHERITED_TREE_KEY,
        receipt,
    )
    await db_session.commit()
    gate.reset_mock()
    gate.side_effect = [
        {"gate_state": "no_signal", "head_sha": _HEAD},
        {"gate_state": "dispatched", "head_sha": _HEAD},
    ]
    identity_mock.reset_mock()
    identity_mock.side_effect = None
    identity_mock.return_value = None

    assert await _run(db_session, version.id) is True
    assert gate.await_count == 2
    assert promotion_receipt.await_count == 1


@pytest.mark.asyncio
async def test_changed_tree_fails_closed_to_full_ci_dispatch(db_session, mocker):
    _project, _predecessor, version = await _promoted_predecessor(db_session)
    identities = {
        _HEAD: {"sha": _HEAD, "tree_sha": "b" * 40, "parents": [_CANDIDATE, _PROMOTED]},
        _CANDIDATE: {"sha": _CANDIDATE, "tree_sha": _TREE, "parents": []},
        _PROMOTED: {"sha": _PROMOTED, "tree_sha": _TREE, "parents": []},
    }
    gate, promotion_receipt, ci_mock, identity_mock = _proof_mocks(
        mocker, identities=identities
    )

    assert await _run(db_session, version.id) is True
    assert gate.await_count == 2
    assert gate.await_args.kwargs["dispatch"] is True
    assert identity_mock.await_count == 3
    promotion_receipt.assert_not_awaited()
    ci_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_backmerge_lineage_fails_closed_to_full_ci_dispatch(
    db_session, mocker
):
    _project, _predecessor, version = await _promoted_predecessor(db_session)
    identities = {
        _HEAD: {"sha": _HEAD, "tree_sha": _TREE, "parents": [_PROMOTED, _CANDIDATE]},
        _CANDIDATE: {"sha": _CANDIDATE, "tree_sha": _TREE, "parents": []},
        _PROMOTED: {"sha": _PROMOTED, "tree_sha": _TREE, "parents": []},
    }
    gate, promotion_receipt, ci_mock, identity_mock = _proof_mocks(
        mocker, identities=identities
    )

    assert await _run(db_session, version.id) is True
    assert gate.await_count == 2
    assert identity_mock.await_count == 1
    promotion_receipt.assert_not_awaited()
    ci_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_canonical_promotion_ci_receipt_fails_closed_to_dispatch(
    db_session, mocker
):
    _project, _predecessor, version = await _promoted_predecessor(db_session)
    gate, _pr, _ci, identity_mock = _proof_mocks(mocker, promotion_ci=False)

    assert await _run(db_session, version.id) is True
    assert gate.await_count == 2
    assert identity_mock.await_count == 3


@pytest.mark.asyncio
async def test_promotion_api_ambiguity_fails_closed_to_full_ci_dispatch(
    db_session, mocker
):
    _project, _predecessor, version = await _promoted_predecessor(db_session)
    gate, promotion_receipt, _ci, identity_mock = _proof_mocks(mocker)
    promotion_receipt.side_effect = RuntimeError("ambiguous promotion API response")

    assert await _run(db_session, version.id) is True
    assert gate.await_count == 2
    assert identity_mock.await_count == 3


@pytest.mark.parametrize(
    "api_body",
    [
        pytest.param(
            {"sha": _CANDIDATE, "tree": {"sha": _TREE}, "parents": []},
            id="different-sha",
        ),
        pytest.param(
            {"sha": int(_HEAD), "tree": {"sha": _TREE}, "parents": []},
            id="numeric-sha",
        ),
        pytest.param(
            {"sha": _HEAD, "tree": {"sha": int("4" * 40)}, "parents": []},
            id="numeric-tree",
        ),
        pytest.param(
            {
                "sha": _HEAD,
                "tree": {"sha": _TREE},
                "parents": [{"sha": int(_CANDIDATE)}],
            },
            id="numeric-parent",
        ),
    ],
)
@pytest.mark.asyncio
async def test_commit_identity_api_rejects_ambiguous_scalars(mocker, api_body):
    """A nominal 200 response with wrong-typed identity is not evidence."""
    from app.github import get_git_commit_identity

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = api_body
    mocker.patch(
        "app.github_refs._github_request",
        new=AsyncMock(return_value=response),
    )

    assert (
        await get_git_commit_identity(
            "token",
            "acme",
            "widget",
            _HEAD,
            project_id="project-id",
        )
        is None
    )

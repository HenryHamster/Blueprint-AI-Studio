"""Focused regressions for bounded, unlocked qualification observations."""

from _release_qualifier_testhelpers import *  # noqa: F403

from app.release_qualification_observation_protocol import (
    ALREADY_ON_MAIN,
    CI_RECOVERY,
    HOTFIX_SCOPE,
    FinalizeObservationRequired,
)


def _patch_base_observers(mocker):
    ci = mocker.patch.object(
        rq,
        "_resolve_ci_at_finalize",
        new=AsyncMock(return_value=(True, {"state": rq._CI_GREEN})),
    )
    identity = mocker.patch.object(
        rq,
        "_resolve_migration_identity_at_finalize",
        new=AsyncMock(return_value=("pass", {"source": "test"})),
    )
    apply = mocker.patch.object(
        rq,
        "_resolve_migration_apply_at_finalize",
        new=AsyncMock(return_value=("pass", {"source": "test"})),
    )
    authorship = mocker.patch.object(
        rq,
        "_measure_candidate_operator_authorship",
        new=AsyncMock(
            return_value={
                "state": "resolved",
                "count": 0,
                "offenders": [],
                "range": {"base_sha": "a" * 40, "head_sha": "b" * 40},
            }
        ),
    )
    return ci, identity, apply, authorship


@pytest.mark.asyncio
async def test_late_observations_reuse_one_base_packet(db_session, mocker):
    project, version = await _make_project_and_version(db_session)
    await _seed_pending_qualification(db_session, project, version)
    base_observers = _patch_base_observers(mocker)
    already = mocker.patch.object(
        rq,
        "_candidate_already_on_main_at_finalize",
        new=AsyncMock(return_value=False),
    )
    recovery = mocker.patch.object(
        rq,
        "_observe_ci_unresolved_promotion_pending",
        new=AsyncMock(return_value={"promotion_pending": False}),
    )
    hotfix = mocker.patch.object(
        rq,
        "check_hotfix_scope_preview",
        new=AsyncMock(side_effect=RuntimeError("preview unavailable")),
    )
    packets = []
    requested = [ALREADY_ON_MAIN, CI_RECOVERY, HOTFIX_SCOPE]

    async def _finalize(_session, _version, *, _pre_gates=None):
        packets.append(dict(_pre_gates))
        if len(packets) <= len(requested):
            raise FinalizeObservationRequired(requested[len(packets) - 1])
        return {"status": "completed"}

    mocker.patch.object(rq, "_finalize_qualification", side_effect=_finalize)

    result = await rq._finalize_in_dedicated_session(
        version.id,
        session_factory=_shared_session_factory(db_session),
    )

    assert result == {"status": "completed"}
    assert len(packets) == 4
    assert ALREADY_ON_MAIN not in packets[0]
    assert packets[1][ALREADY_ON_MAIN] is False
    assert packets[2][CI_RECOVERY] == {"promotion_pending": False}
    assert packets[3][HOTFIX_SCOPE] is None
    for packet in packets:
        assert packet["_observation_protocol"] is True
    for observer in base_observers:
        observer.assert_awaited_once()
    already.assert_awaited_once()
    recovery.assert_awaited_once()
    hotfix.assert_awaited_once()


@pytest.mark.asyncio
async def test_fourth_late_observation_exhausts_without_replay(db_session, mocker):
    project, version = await _make_project_and_version(db_session)
    await _seed_pending_qualification(db_session, project, version)
    base_observers = _patch_base_observers(mocker)
    already = mocker.patch.object(
        rq,
        "_candidate_already_on_main_at_finalize",
        new=AsyncMock(return_value=False),
    )
    recovery = mocker.patch.object(
        rq,
        "_observe_ci_unresolved_promotion_pending",
        new=AsyncMock(return_value={"promotion_pending": False}),
    )
    hotfix = mocker.patch.object(
        rq,
        "check_hotfix_scope_preview",
        new=AsyncMock(return_value={"eligible": False}),
    )
    requested = [ALREADY_ON_MAIN, CI_RECOVERY, HOTFIX_SCOPE, ALREADY_ON_MAIN]
    calls = 0

    async def _finalize(_session, _version, *, _pre_gates=None):
        nonlocal calls
        calls += 1
        raise FinalizeObservationRequired(requested[calls - 1])

    mocker.patch.object(rq, "_finalize_qualification", side_effect=_finalize)

    result = await rq._finalize_in_dedicated_session(
        version.id,
        session_factory=_shared_session_factory(db_session),
    )

    assert result == {"status": "pending", "reason": "observation_retry_exhausted"}
    assert calls == 4
    for observer in base_observers:
        observer.assert_awaited_once()
    already.assert_awaited_once()
    recovery.assert_awaited_once()
    hotfix.assert_awaited_once()
    await db_session.refresh(version)
    assert (version.last_qualification or {})["status"] == "pending"


@pytest.mark.asyncio
async def test_failed_commit_suppresses_external_notification(db_session, mocker):
    project, version = await _make_project_and_version(db_session)
    await _seed_pending_qualification(db_session, project, version)
    packet = {
        "_observation_protocol": True,
        "version_authority_fingerprint": (
            rq.qualification_finalize_authority_fingerprint(version)
        ),
        "project_observation_fingerprint": (
            rq.qualification_project_observation_fingerprint(project)
        ),
    }
    intent = {
        "project_id": project.id,
        "event_type": "RELEASE_INFRA_FAILURE",
        "payload": {"version_id": str(version.id)},
    }
    mocker.patch.object(
        rq,
        "_finalize_qualification",
        new=AsyncMock(
            return_value={
                "status": "infra_failure",
                "_postcommit_external_notification": intent,
            }
        ),
    )
    dispatch = mocker.patch.object(
        rq, "dispatch_external_notification", new=AsyncMock()
    )
    mocker.patch.object(
        db_session,
        "commit",
        new=AsyncMock(side_effect=RuntimeError("commit failed")),
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        await rq._finalize_locked_version(
            db_session,
            version.id,
            timeout_now=None,
            _observation_packet=packet,
        )

    dispatch.assert_not_called()

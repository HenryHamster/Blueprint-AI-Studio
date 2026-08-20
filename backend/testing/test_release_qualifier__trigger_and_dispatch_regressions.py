"""Focused qualification-trigger and terminal dispatch suppression regressions."""

from _release_qualifier_testhelpers import *  # noqa: F403


@pytest.mark.asyncio
async def test_terminal_dispatch_rejection_is_once_per_head(db_session, mocker):
    """Persist a terminal 403 only after lookup; suppress only its exact HEAD."""
    from app.project_telemetry import read_telemetry
    from app.release_qualifier_sweep import (
        _DRIFT_SENTINEL_TERMINAL_DISPATCH_KEY,
        _check_full_suite_drift_sentinel,
    )

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    old_head, new_head = "71bfe706590e", "82c0f8176a1f"
    old_key = (
        f"{project.github_owner or ''}/{project.github_repo or ''}"
        f":ci.yml:automain:{old_head}"
    )
    gate_mock = mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            side_effect=[
                {"gate_state": "no_signal", "head_sha": old_head, "reason": "none"},
                {
                    "gate_state": "dispatch_rejected",
                    "head_sha": old_head,
                    "reason": "GitHub denied this dispatch authority",
                    "dispatch_outcome": "rejected",
                    "dispatch_retryable": False,
                    "dispatch_suppression_key": old_key,
                    "dispatch_ref": "automain",
                    "dispatch_workflow_file": "ci.yml",
                },
                {"gate_state": "no_signal", "head_sha": old_head, "reason": "none"},
                {"gate_state": "no_signal", "head_sha": new_head, "reason": "none"},
                {"gate_state": "dispatched", "head_sha": new_head, "reason": "sent"},
            ]
        ),
    )
    factory = _shared_session_factory(db_session)

    assert await _check_full_suite_drift_sentinel(
        version.id, session_factory=factory
    ) is True
    marker = await read_telemetry(
        db_session, project.id, _DRIFT_SENTINEL_TERMINAL_DISPATCH_KEY
    )
    assert marker["suppression_key"] == old_key
    assert await _check_full_suite_drift_sentinel(
        version.id, session_factory=factory
    ) is True
    assert gate_mock.await_count == 3
    assert gate_mock.await_args.kwargs["dispatch"] is False
    assert await _check_full_suite_drift_sentinel(
        version.id, session_factory=factory
    ) is True
    assert gate_mock.await_count == 5
    assert gate_mock.await_args.kwargs["dispatch"] is True


@pytest.mark.asyncio
async def test_terminal_dispatch_retries_once_after_receipt_ttl(db_session, mocker):
    """A repaired same-HEAD authority gets one retry after bounded suppression."""
    from app.project_telemetry import read_telemetry, upsert_telemetry
    from app.release_qualifier_sweep import (
        _DRIFT_SENTINEL_TERMINAL_DISPATCH_KEY,
        _check_full_suite_drift_sentinel,
    )

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    head = "71bfe706590e"
    key = f"{project.github_owner}/{project.github_repo}:ci.yml:automain:{head}"
    rejected = {
        "gate_state": "dispatch_rejected",
        "head_sha": head,
        "reason": "GitHub denied this dispatch authority",
        "dispatch_outcome": "rejected",
        "dispatch_retryable": False,
        "dispatch_suppression_key": key,
        "dispatch_ref": "automain",
        "dispatch_workflow_file": "ci.yml",
    }
    no_signal = {"gate_state": "no_signal", "head_sha": head, "reason": "none"}
    gate_mock = mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            side_effect=[no_signal, rejected, no_signal, rejected, no_signal]
        ),
    )
    factory = _shared_session_factory(db_session)

    assert await _check_full_suite_drift_sentinel(
        version.id, session_factory=factory
    ) is True
    marker = await read_telemetry(
        db_session, project.id, _DRIFT_SENTINEL_TERMINAL_DISPATCH_KEY
    )
    assert marker["retry_authority"] == "bounded_terminal_receipt_ttl"
    marker["retry_after"] = "2000-01-01T00:00:00"
    await upsert_telemetry(
        db_session, project.id, _DRIFT_SENTINEL_TERMINAL_DISPATCH_KEY, marker
    )
    await db_session.commit()

    assert await _check_full_suite_drift_sentinel(
        version.id, session_factory=factory
    ) is True
    assert await _check_full_suite_drift_sentinel(
        version.id, session_factory=factory
    ) is True
    assert gate_mock.await_count == 5
    assert gate_mock.await_args.kwargs["dispatch"] is False


@pytest.mark.asyncio
async def test_cancelled_terminal_dispatch_rejection_is_once_per_head(
    db_session, mocker
):
    """A cancelled-run redispatch shares the exact-head terminal receipt."""
    from app.project_telemetry import read_telemetry
    from app.release_qualifier_sweep import (
        _DRIFT_SENTINEL_TERMINAL_DISPATCH_KEY,
        _check_full_suite_drift_sentinel,
    )

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    head = "71bfe706590e"
    key = f"{project.github_owner}/{project.github_repo}:ci.yml:automain:{head}"
    cancelled = {
        "gate_state": "cancelled",
        "head_sha": head,
        "reason": "prior full-suite run was cancelled",
    }
    rejected = {
        "gate_state": "dispatch_rejected",
        "head_sha": head,
        "reason": "GitHub denied this dispatch authority",
        "dispatch_outcome": "rejected",
        "dispatch_retryable": False,
        "dispatch_suppression_key": key,
        "dispatch_ref": "automain",
        "dispatch_workflow_file": "ci.yml",
    }
    gate_mock = mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(side_effect=[cancelled, rejected, cancelled]),
    )
    factory = _shared_session_factory(db_session)

    assert await _check_full_suite_drift_sentinel(
        version.id, session_factory=factory
    ) is True
    marker = await read_telemetry(
        db_session, project.id, _DRIFT_SENTINEL_TERMINAL_DISPATCH_KEY
    )
    assert marker["suppression_key"] == key

    assert await _check_full_suite_drift_sentinel(
        version.id, session_factory=factory
    ) is True
    assert gate_mock.await_count == 3
    redispatch_calls = [
        call
        for call in gate_mock.await_args_list
        if call.kwargs.get("dispatch_on_cancelled") is True
    ]
    assert len(redispatch_calls) == 1


async def _age_automain_head_marker(db_session, project_id, head_sha, *, seconds):
    """Seed the head-stability marker as if ``head_sha`` settled ``seconds`` ago."""
    from datetime import timedelta

    from app.project_telemetry import upsert_telemetry
    from app.release_qualifier_sweep import _AUTOMAIN_HEAD_FIRST_SEEN_KEY
    from app.time_utils import _utcnow

    await upsert_telemetry(
        db_session,
        project_id,
        _AUTOMAIN_HEAD_FIRST_SEEN_KEY,
        {
            "head_sha": head_sha,
            "first_seen_at": (_utcnow() - timedelta(seconds=seconds)).isoformat(),
        },
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_advanced_not_promoted_authorship_candidate_redrives(db_session, mocker):
    """An advanced candidate invalidates the old SHA's authorship blocker."""
    from app import shared_state
    from app.release_qualifier_sweep import _fire_auto_triggered_qualification

    shared_state.runtime_config["auto_qualification_triggers"] = "on"
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "prior_operator_authorship_nonzero",
        "candidate_sha": "old-authorship-candidate",
        "audit_findings": [
            {"key": "OLD_AUTHORSHIP", "severity": "high", "summary": "old SHA"}
        ],
    }
    db_session.add(version)
    await db_session.commit()
    # The new head has been quiet past the debounce window, so the redrive
    # semantics under test (stale-blocker invalidation) are what decides.
    await _age_automain_head_marker(
        db_session, project.id, "new-automain-candidate", seconds=31 * 60
    )
    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            return_value={"gate_state": "green", "head_sha": "new-automain-candidate"}
        ),
    )
    run_mock = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )

    assert await _fire_auto_triggered_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    ) is True
    run_mock.assert_awaited_once()
    assert run_mock.await_args.kwargs.get("auto_trigger") is True


@pytest.mark.asyncio
async def test_same_not_promoted_sha_never_churns(db_session, mocker):
    """Readiness changes cannot repeatedly re-grade an unchanged candidate."""
    from app import shared_state
    from app.release_qualifier_sweep import _fire_auto_triggered_qualification

    shared_state.runtime_config["auto_qualification_triggers"] = "on"
    same_sha = "unchanged-automain-candidate"
    _, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "prior_operator_authorship_nonzero",
        "candidate_sha": same_sha,
    }
    db_session.add(version)
    await db_session.commit()
    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(return_value={"gate_state": "green", "head_sha": same_sha}),
    )
    run_mock = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )

    assert await _fire_auto_triggered_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    ) is False
    run_mock.assert_not_awaited()


def _not_promoted_version_kwargs(candidate_sha: str) -> dict:
    return {
        "status": "not_promoted",
        "blocker": "below_threshold",
        "candidate_sha": candidate_sha,
    }


@pytest.mark.asyncio
async def test_fresh_automain_head_holds_quiet_window(db_session, mocker):
    """A just-advanced head must NOT immediately re-fire qualification.

    Per-delta audit-spam regression: every merge to automain used to re-fire
    qualification (and an opus audit pair) instantly.  The first sweep that
    observes a new head records it and holds; only a settled head fires.
    """
    from app import shared_state
    from app.project_telemetry import read_telemetry
    from app.release_qualifier_sweep import (
        _AUTOMAIN_HEAD_FIRST_SEEN_KEY,
        _fire_auto_triggered_qualification,
    )

    shared_state.runtime_config["auto_qualification_triggers"] = "on"
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = _not_promoted_version_kwargs("old-candidate")
    db_session.add(version)
    await db_session.commit()
    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            return_value={"gate_state": "green", "head_sha": "fresh-new-head"}
        ),
    )
    run_mock = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )

    assert await _fire_auto_triggered_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    ) is False
    run_mock.assert_not_awaited()
    marker = await read_telemetry(
        db_session, project.id, _AUTOMAIN_HEAD_FIRST_SEEN_KEY
    )
    assert marker["head_sha"] == "fresh-new-head"
    assert marker["first_seen_at"]


@pytest.mark.asyncio
async def test_head_move_restarts_quiet_window(db_session, mocker):
    """Another merge during the window restarts it for the newest head."""
    from app import shared_state
    from app.project_telemetry import read_telemetry
    from app.release_qualifier_sweep import (
        _AUTOMAIN_HEAD_FIRST_SEEN_KEY,
        _fire_auto_triggered_qualification,
    )

    shared_state.runtime_config["auto_qualification_triggers"] = "on"
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = _not_promoted_version_kwargs("old-candidate")
    db_session.add(version)
    await db_session.commit()
    # An OLDER head settled long ago — but automain has since moved again.
    await _age_automain_head_marker(
        db_session, project.id, "previous-head", seconds=31 * 60
    )
    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            return_value={"gate_state": "green", "head_sha": "even-newer-head"}
        ),
    )
    run_mock = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )

    assert await _fire_auto_triggered_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    ) is False
    run_mock.assert_not_awaited()
    marker = await read_telemetry(
        db_session, project.id, _AUTOMAIN_HEAD_FIRST_SEEN_KEY
    )
    assert marker["head_sha"] == "even-newer-head"


@pytest.mark.asyncio
async def test_live_audit_pair_defers_auto_fire(db_session, mocker):
    """A running release_audit/challenge lane blocks stacking a second pair."""
    from app import shared_state
    from app.release_qualifier_sweep import _fire_auto_triggered_qualification

    shared_state.runtime_config["auto_qualification_triggers"] = "on"
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = _not_promoted_version_kwargs("old-candidate")
    db_session.add(version)
    db_session.add(
        Task(
            project_id=project.id,
            title="live audit",
            payloads=[{"type": "release_audit", "base_branch": "main"}],
            vm_instance_id="vm-live-audit",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    await db_session.commit()
    # Head fully settled — without the live-lane skip this WOULD fire.
    await _age_automain_head_marker(
        db_session, project.id, "settled-head", seconds=31 * 60
    )
    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            return_value={"gate_state": "green", "head_sha": "settled-head"}
        ),
    )
    run_mock = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )

    assert await _fire_auto_triggered_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    ) is False
    run_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_head_marker_failure_degrades_to_legacy_fire(db_session, mocker):
    """Telemetry unavailability must never suppress qualification (fail-open
    to the legacy fire-immediately behavior, not fail-closed on the debounce)."""
    from app import shared_state
    from app.release_qualifier_sweep import _fire_auto_triggered_qualification

    shared_state.runtime_config["auto_qualification_triggers"] = "on"
    _, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = _not_promoted_version_kwargs("old-candidate")
    db_session.add(version)
    await db_session.commit()
    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            return_value={"gate_state": "green", "head_sha": "fresh-new-head"}
        ),
    )
    mocker.patch(
        "app.project_telemetry.read_telemetry",
        new=AsyncMock(side_effect=RuntimeError("telemetry down")),
    )
    run_mock = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )

    assert await _fire_auto_triggered_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    ) is True
    run_mock.assert_awaited_once()

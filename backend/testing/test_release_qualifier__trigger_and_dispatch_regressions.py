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
    from app.release_qualifier_debounce import _AUTOMAIN_HEAD_FIRST_SEEN_KEY
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
    from app.release_qualifier_debounce import _AUTOMAIN_HEAD_FIRST_SEEN_KEY
    from app.release_qualifier_sweep import _fire_auto_triggered_qualification

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
    from app.release_qualifier_debounce import _AUTOMAIN_HEAD_FIRST_SEEN_KEY
    from app.release_qualifier_sweep import _fire_auto_triggered_qualification

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


def _rejected_gate(owner, repo, head, outcome="rejected"):
    """A terminal dispatch rejection verdict for ``head``."""
    detail = (
        "workflow is unavailable to this GitHub authority"
        if outcome == "rejected_permanent_404"
        else "GitHub denied this dispatch authority"
    )
    return {
        "gate_state": "dispatch_rejected",
        "head_sha": head,
        "reason": f"ci.yml dispatch was terminally rejected: {detail}",
        "dispatch_outcome": outcome,
        "dispatch_retryable": False,
        "dispatch_suppression_key": (
            f"{owner}/{repo}:ci.yml:automain:{head}"
        ),
        "dispatch_ref": "automain",
        "dispatch_workflow_file": "ci.yml",
    }


async def _dispatch_rejected_alerts(db_session, project_id):
    """Every open dispatch-rejected drift-sentinel alert, as plain rows.

    Column-level (not entity-level) so nothing here depends on the identity
    map's post-commit expiry state — an expired ORM instance would refresh
    lazily and raise ``MissingGreenlet`` under the async session.
    """
    return (
        await db_session.execute(
            select(
                Alert.dedupe_key,
                Alert.title,
                Alert.message,
                Alert.occurrence_count,
                Alert.last_seen_at,
            ).where(
                Alert.project_id == project_id,
                Alert.dedupe_key.like("drift_sentinel:dispatch_rejected:%"),
                Alert.dismissed_at.is_(None),
            )
        )
    ).all()


@pytest.mark.asyncio
async def test_dispatch_rejection_raises_one_standing_alert_not_one_per_head(
    db_session, mocker
):
    """Three automain merges under one standing 403 == ONE operator incident.

    The per-HEAD ``dispatch_suppression_key`` bounds API churn; it is not a
    notification key.  When the alert borrowed that granularity, a repository
    permission the operator must fix exactly once (``actions: write``) minted a
    fresh WARNING on every single automain merge — an unbounded per-delta alert
    stream for an unchanging cause.
    """
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    project_id, version_id = project.id, version.id
    owner, repo = project.github_owner, project.github_repo
    heads = ["71bfe706590e", "82c0f8176a1f", "93d1a9284b2c"]
    side_effect = []
    for head in heads:
        side_effect.append(
            {"gate_state": "no_signal", "head_sha": head, "reason": "none"}
        )
        side_effect.append(_rejected_gate(owner, repo, head))
    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(side_effect=side_effect),
    )
    factory = _shared_session_factory(db_session)

    for _ in heads:
        assert (
            await _check_full_suite_drift_sentinel(version_id, session_factory=factory)
            is True
        )

    alerts = await _dispatch_rejected_alerts(db_session, project_id)
    assert len(alerts) == 1, (
        "Three automain HEADs under one standing dispatch rejection must produce "
        f"ONE operator incident, got {len(alerts)}: "
        f"{[a.dedupe_key for a in alerts]}"
    )
    key = alerts[0].dedupe_key
    for head in heads:
        assert head not in key, (
            f"dedupe_key {key!r} still carries HEAD {head!r} — the alert is keyed "
            "per-delta again and will spam once per automain merge"
        )
    assert key == f"drift_sentinel:dispatch_rejected:{project_id}:rejected"


@pytest.mark.asyncio
async def test_standing_rejection_alert_tracks_newest_head_and_counts_recurrence(
    db_session, mocker
):
    """The one incident must stay CURRENT, not freeze on the first HEAD.

    Collapsing the key without refreshing would trade spam for a stale alert
    naming a HEAD from days ago.  ``occurrence_count`` is what replaces the
    deleted alert stream as the "how long has this been broken" signal.
    """
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    project_id, version_id = project.id, version.id
    owner, repo = project.github_owner, project.github_repo
    first, newest = "71bfe706590e", "82c0f8176a1f"
    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            side_effect=[
                {"gate_state": "no_signal", "head_sha": first, "reason": "none"},
                _rejected_gate(owner, repo, first),
                {"gate_state": "no_signal", "head_sha": newest, "reason": "none"},
                _rejected_gate(owner, repo, newest),
            ]
        ),
    )
    factory = _shared_session_factory(db_session)

    for _ in range(2):
        assert (
            await _check_full_suite_drift_sentinel(version_id, session_factory=factory)
            is True
        )

    alerts = await _dispatch_rejected_alerts(db_session, project_id)
    assert len(alerts) == 1
    alert = alerts[0]
    assert newest[:12] in alert.message, (
        "The standing incident must name the CURRENT HEAD; it froze on the "
        f"first one: {alert.message!r}"
    )
    assert newest[:8] in alert.title
    assert first[:12] not in alert.message
    assert int(alert.occurrence_count or 1) == 2, (
        "Each recurrence must bump occurrence_count — that is the only "
        "remaining signal for how long the condition has persisted"
    )
    assert alert.last_seen_at is not None


@pytest.mark.asyncio
async def test_a_different_rejection_diagnosis_opens_its_own_incident(
    db_session, mocker
):
    """403 and 404 need different operator fixes, so they stay separate.

    Collapsing per-project must not collapse across DIAGNOSES: "GitHub denied
    this authority" (grant ``actions: write``) and "workflow unavailable to this
    authority" (the workflow file / App installation scope) are different
    actions, and a flip between them is new information.
    """
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    project_id, version_id = project.id, version.id
    owner, repo = project.github_owner, project.github_repo
    denied_head, missing_head = "71bfe706590e", "82c0f8176a1f"
    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            side_effect=[
                {"gate_state": "no_signal", "head_sha": denied_head, "reason": "none"},
                _rejected_gate(owner, repo, denied_head, outcome="rejected"),
                {"gate_state": "no_signal", "head_sha": missing_head, "reason": "none"},
                _rejected_gate(
                    owner, repo, missing_head, outcome="rejected_permanent_404"
                ),
            ]
        ),
    )
    factory = _shared_session_factory(db_session)

    for _ in range(2):
        assert (
            await _check_full_suite_drift_sentinel(version_id, session_factory=factory)
            is True
        )

    alerts = await _dispatch_rejected_alerts(db_session, project_id)
    keys = sorted(a.dedupe_key for a in alerts)
    assert keys == [
        f"drift_sentinel:dispatch_rejected:{project_id}:rejected",
        f"drift_sentinel:dispatch_rejected:{project_id}:rejected_permanent_404",
    ], f"expected one incident per diagnosis, got {keys}"


# ===========================================================================
# Drift-sentinel alert LIFECYCLE (release.ci_unresolved)
#
# Measured on the live control plane 2026-08-24: 126 open
# ``release.ci_unresolved`` rows spanning twelve days and 122 distinct
# integration HEADs, and NOT ONE described a condition that was still true.
# Every row carried the model's ``core`` domain default and a NULL source, the
# ``red`` / ``cancelled`` keys were still per-HEAD, and the producer owned no
# resolver at all despite the event declaring
# ``AlertRetirementMode.SOURCE_CONDITION``.  The three tests below pin each of
# those defects.
# ===========================================================================


async def _sentinel_alert_rows(db_session, project_id, *, include_dismissed=False):
    """Open drift-sentinel ``release.ci_unresolved`` rows, column-level.

    Column-level (not entity-level) for the same reason as
    ``_dispatch_rejected_alerts``: an expired ORM instance would refresh lazily
    and raise ``MissingGreenlet`` under the async session.
    """
    where = [
        Alert.project_id == project_id,
        Alert.event_type == "release.ci_unresolved",
    ]
    if not include_dismissed:
        where.append(Alert.dismissed_at.is_(None))
    rows = (
        await db_session.execute(
            select(
                Alert.dedupe_key,
                Alert.title,
                Alert.message,
                Alert.domain,
                Alert.source,
                Alert.occurrence_count,
                Alert.dismissed_at,
            ).where(*where)
        )
    ).all()
    return [r for r in rows if str(r.dedupe_key or "").startswith("drift_sentinel:")]


async def _seed_legacy_sentinel_alert(db_session, project_id, dedupe_key):
    """A row exactly as the pre-fix producer wrote it: core domain, NULL source."""
    from app.models import AlertSeverity as _Sev

    db_session.add(
        Alert(
            project_id=project_id,
            severity=_Sev.WARNING,
            title="legacy sentinel incident",
            message="written by the pre-fix per-HEAD producer",
            event_type="release.ci_unresolved",
            dedupe_key=dedupe_key,
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_sentinel_incident_carries_its_policy_domain_and_source(
    db_session, mocker
):
    """The producer must stamp what ``alert_policy`` already declares.

    ``ALERT_EVENT_POLICIES["release.ci_unresolved"]`` declares the
    ``release_lifecycle`` domain, but the sentinel wrote raw ``Alert(...)`` rows
    and every one landed as ``core`` with a NULL source.  All 126 live rows were
    therefore invisible to the ``?domain=`` / ``?source=`` filters on
    ``/api/admin/alerts`` and mis-attributed in every domain rollup — the feed
    could not even be triaged by owner.
    """
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    project_id, version_id = project.id, version.id
    head = "71bfe706590e"
    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            side_effect=[
                {"gate_state": "no_signal", "head_sha": head, "reason": "none"},
                _rejected_gate(project.github_owner, project.github_repo, head),
            ]
        ),
    )

    assert (
        await _check_full_suite_drift_sentinel(
            version_id, session_factory=_shared_session_factory(db_session)
        )
        is True
    )

    rows = await _sentinel_alert_rows(db_session, project_id)
    assert len(rows) == 1
    assert rows[0].domain == "release_lifecycle", (
        "the sentinel bypassed alert_policy and stamped the model's default "
        f"domain {rows[0].domain!r}; the row is unfilterable and mis-attributed"
    )
    assert rows[0].source == "drift_sentinel", (
        f"source must name the producer for triage, got {rows[0].source!r}"
    )


@pytest.mark.asyncio
async def test_red_sentinel_is_one_standing_incident_not_one_per_head(
    db_session, mocker
):
    """Red on the integration branch is ONE condition, not one per merge.

    #17604 collapsed the ``dispatch_rejected`` key to per-project and left
    ``red`` and ``cancelled`` keyed per HEAD, so they kept minting: the live feed
    held 12 red rows across 12 distinct shas for a branch that is red or not red
    exactly once at a time.  Raising the standing incident must also SUPERSEDE
    any per-HEAD row a previous serving image left behind, which is what makes
    the existing backlog reap itself condition-derivedly.
    """
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    project_id, version_id = project.id, version.id
    heads = ["13122be6aa01", "8a9bd18f0b22", "3ef760b2cc33"]
    # A row minted by the pre-fix per-HEAD producer, still open.
    await _seed_legacy_sentinel_alert(
        db_session, project_id, f"drift_sentinel:red:{project_id}:20086f09dd44"
    )
    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            side_effect=[
                {
                    "gate_state": "red",
                    "head_sha": head,
                    "html_url": f"https://github.test/runs/{i}",
                }
                for i, head in enumerate(heads)
            ]
        ),
    )
    mocker.patch(
        "app.release_reconciliation._harvest_promotion_ci_failures",
        new=AsyncMock(return_value="spawned"),
    )
    factory = _shared_session_factory(db_session)

    for _ in heads:
        assert (
            await _check_full_suite_drift_sentinel(version_id, session_factory=factory)
            is True
        )

    rows = await _sentinel_alert_rows(db_session, project_id)
    assert len(rows) == 1, (
        "Three red integration HEADs plus one legacy per-HEAD row must leave ONE "
        f"standing incident, got {len(rows)}: {[r.dedupe_key for r in rows]}"
    )
    row = rows[0]
    assert row.dedupe_key == f"drift_sentinel:red:{project_id}", (
        f"dedupe_key {row.dedupe_key!r} is still per-delta and will mint one "
        "operator incident per automain merge"
    )
    assert heads[-1][:12] in row.message, (
        f"the standing incident must name the CURRENT HEAD: {row.message!r}"
    )
    assert int(row.occurrence_count or 1) == 3, (
        "each recurrence must bump occurrence_count — it is the only remaining "
        "signal for how long the branch has been red"
    )


@pytest.mark.asyncio
async def test_green_full_suite_gate_retires_every_open_sentinel_incident(
    db_session, mocker
):
    """``SOURCE_CONDITION`` means this producer OWNS retirement.

    ``release.ci_unresolved`` declares ``AlertRetirementMode.SOURCE_CONDITION``
    — "the producer owns a condition-specific resolver" — and the producer had
    none.  A sentinel sweep that read green simply returned, so incidents from
    releases that had since promoted AND deployed stayed open forever; the live
    backlog's red rows all pointed at shas that were provably ancestors of
    ``main``.  Green is the one state that falsifies every sentinel claim at
    once, so it must retire all of them.
    """
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    project_id, version_id = project.id, version.id
    for key in (
        f"drift_sentinel:red:{project_id}:13122be6aa01",
        f"drift_sentinel:red:{project_id}",
        f"drift_sentinel:cancelled:{project_id}:8a9bd18f0b22",
        f"drift_sentinel:unavailable:{project_id}",
        f"drift_sentinel:dispatch_rejected:{project_id}:rejected",
    ):
        await _seed_legacy_sentinel_alert(db_session, project_id, key)
    assert len(await _sentinel_alert_rows(db_session, project_id)) == 5
    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(return_value={"gate_state": "green", "head_sha": "aa11bb22cc33"}),
    )

    assert (
        await _check_full_suite_drift_sentinel(
            version_id, session_factory=_shared_session_factory(db_session)
        )
        is False
    )

    open_rows = await _sentinel_alert_rows(db_session, project_id)
    assert open_rows == [], (
        "a green full-suite gate falsifies every sentinel claim, but these "
        f"incidents are still open: {[r.dedupe_key for r in open_rows]}"
    )
    assert (
        len(await _sentinel_alert_rows(db_session, project_id, include_dismissed=True))
        == 5
    ), "retirement must DISMISS the rows, never delete the operator's history"


@pytest.mark.asyncio
async def test_authoritative_red_retires_only_the_could_not_evaluate_incidents(
    db_session, mocker
):
    """A real conclusion ends "unverified" — it does not end "red".

    ``unavailable`` / ``dispatch_rejected`` / ``cancelled`` all make the same
    claim: full-suite CI drift could not be EVALUATED.  An authoritative red is
    direct positive evidence that the claim is now false, so those retire; the
    red incident itself must stay open, because red is still true.  Retiring
    fails OPEN — a still-broken dispatch authority re-raises its standing
    incident the very next time the gate has to dispatch.
    """
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    project_id, version_id = project.id, version.id
    for key in (
        f"drift_sentinel:unavailable:{project_id}",
        f"drift_sentinel:dispatch_rejected:{project_id}:rejected",
        f"drift_sentinel:cancelled:{project_id}:8a9bd18f0b22",
    ):
        await _seed_legacy_sentinel_alert(db_session, project_id, key)
    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            return_value={
                "gate_state": "red",
                "head_sha": "3ef760b2cc33",
                "html_url": "https://github.test/runs/9",
            }
        ),
    )
    mocker.patch(
        "app.release_reconciliation._harvest_promotion_ci_failures",
        new=AsyncMock(return_value="spawned"),
    )

    assert (
        await _check_full_suite_drift_sentinel(
            version_id, session_factory=_shared_session_factory(db_session)
        )
        is True
    )

    keys = sorted(
        r.dedupe_key for r in await _sentinel_alert_rows(db_session, project_id)
    )
    assert keys == [f"drift_sentinel:red:{project_id}"], (
        "an authoritative ci.yml conclusion ends every 'could not evaluate' "
        f"incident and only those; open incidents are {keys}"
    )

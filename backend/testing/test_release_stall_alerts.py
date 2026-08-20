"""Tests for release_stall_alerts — managed promotion and deploy-failed checks.

Covers:
  * _check_version_managed_check_wait_stall — threshold gate, first-fire WARNING,
    escalation to ERROR, and re-alert suppression within the shared interval.
  * _alert_promotion_deploy_failed — first-fire creates ERROR alert, reason codes, idempotency.
  * _resolve_promotion_deploy_failed_alert — no-op when none open, dismisses open alert, idempotent.
  * _check_deployed_gate_latch — detects candidate-carried deployed repairs.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app import release_qualifier as rq
from app import release_version as rv
from app.message_audience import (
    OPERATOR_ACTION_NEEDED,
    STRATEGIST_INTERNAL,
    route_event,
)
from app.models import (
    Alert,
    AlertSeverity,
    Project,
    ProjectMessage,
    QualificationVerdict,
)
from app.release_gate_latch import (
    _check_deployed_gate_latch,
    detect_self_modifying_gate,
    gate_implementing_paths,
)
from app.release_stall_alerts import (
    _MANAGED_CHECK_WAIT_REALERT_INTERVAL_SECONDS,
    _MANAGED_CHECK_WAIT_STALL_CRITICAL_SECONDS,
    _MANAGED_CHECK_WAIT_STALL_SECONDS,
    _alert_promotion_deploy_failed,
    _check_version_calcification,
    _check_version_managed_check_wait_stall,
    _check_version_stabilize_stall,
    _deploy_failed_alert_title,
    _resolve_promotion_deploy_failed_alert,
)
from app.time_utils import _utcnow
from sqlalchemy import select


def test_retired_pending_activation_reason_is_absent_from_alert_module():
    """The alert module cannot retain the removed activation-only branch."""
    source = Path(__file__).parents[1].joinpath(
        "app", "release_stall_alerts.py"
    ).read_text()

    assert "PENDING_OPERATOR_ACTIVATION_REASON" not in source
    assert "Approve activation" not in source


def test_gate_implementing_paths_scans_extracted_release_blocker_modules():
    """A blocker emitted by an extracted lifecycle module remains discoverable."""
    assert gate_implementing_paths("sweep_finalize_crash_loop") == [
        "backend/app/release_qualifier_sweep.py"
    ]
    assert gate_implementing_paths("audit_challenge_independent_blocker") == [
        "backend/app/release_qualifier_audit.py"
    ]
    assert gate_implementing_paths("qualification_superseded") == [
        "backend/app/release_qualifier.py",
        "backend/app/release_qualifier_sweep.py",
        "backend/app/release_reconciliation.py",
    ]


def test_gate_implementing_paths_unions_supplemental_predicate_modules():
    """The literal scan is a proxy — the authorship/audit gates' actual decision
    is made in a module that never spells out the blocker literal, so the
    supplemental map must be unioned in or a genuine repair there never
    latches (dir: fix(release): latch alert never fires; misnames the repair).
    """
    assert gate_implementing_paths("candidate_operator_authorship_nonzero") == [
        "backend/app/release_commit_authorship.py",
        "backend/app/release_scoring.py",
    ]
    assert gate_implementing_paths(
        "candidate_operator_authorship_evidence_missing"
    ) == [
        "backend/app/release_commit_authorship.py",
        "backend/app/release_scoring.py",
    ]
    assert gate_implementing_paths("criterion_coverage_incomplete") == [
        "backend/app/acceptance_criteria.py",
        "backend/app/category_verdict_ledger.py",
        "backend/app/release_scoring.py",
    ]
    # audit_missing is decided candidate-side (audit_score is None, from the
    # audit child's logs) — it gets NO supplemental modules, so the latch is
    # not armed by an unrelated acceptance_criteria/category_verdict_ledger
    # touch.
    assert gate_implementing_paths("audit_missing") == [
        "backend/app/release_scoring.py",
    ]


def test_supplemental_gate_implementing_paths_are_deployed_side_only():
    """The latch only makes a claim about gates whose validator runs in the
    DEPLOYED backend. A supplemental entry for a candidate-side blocker would
    arm the latch branch in _check_version_calcification on an unrelated file
    touch, emit a wrong operator instruction, and suppress the ordinary
    calcification alert that should have fired instead.
    """
    from app.release_gate_latch import _SUPPLEMENTAL_GATE_IMPLEMENTING_PATHS
    from app.release_scoring import BLOCKER_EXECUTION_SITE, EXECUTION_SITE_DEPLOYED

    for blocker in _SUPPLEMENTAL_GATE_IMPLEMENTING_PATHS:
        assert BLOCKER_EXECUTION_SITE.get(blocker) == EXECUTION_SITE_DEPLOYED, (
            f"{blocker} is classified "
            f"{BLOCKER_EXECUTION_SITE.get(blocker)!r}, not deployed-side"
        )


def test_supplemental_gate_implementing_paths_are_hotfix_eligible():
    """Every module the supplemental map can name must be hotfix-eligible, or
    the latch alert could direct an operator at a module the hotfix lane
    would refuse to fast-track.
    """
    from app import hotfix_policy
    from app.release_gate_latch import _SUPPLEMENTAL_GATE_IMPLEMENTING_PATHS

    for blocker, paths in _SUPPLEMENTAL_GATE_IMPLEMENTING_PATHS.items():
        for path in paths:
            assert path in hotfix_policy.HOTFIX_ELIGIBLE_MODULES, (blocker, path)


def test_detect_self_modifying_gate_latches_on_predicate_module_repair():
    """A candidate that only touches release_commit_authorship.py — not
    release_scoring.py — must still be recognized as implicated for the
    authorship gate, since that module carries the actual measurement
    predicate the gate blocks on.
    """
    deployed_surface = {
        "backend/app/release_commit_authorship.py": "deployed-blob-sha",
        "backend/app/release_scoring.py": "unchanged-blob-sha",
    }
    candidate_surface = {
        "backend/app/release_commit_authorship.py": "repaired-blob-sha",
        "backend/app/release_scoring.py": "unchanged-blob-sha",
    }
    detected = detect_self_modifying_gate(
        "candidate_operator_authorship_nonzero", deployed_surface, candidate_surface
    )
    assert detected is not None
    assert detected["implicated_paths"] == [
        "backend/app/release_commit_authorship.py"
    ]


async def _make_project_and_version(db_session):
    project = Project(
        name="Stall-Test",
        user_id="stall-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    rv.set_version_phase(version, "stabilize", reasoning="test setup")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return project, version


def _latched_qual(*, blocker: str, candidate_surface: dict[str, str]) -> dict:
    return {
        "status": "not_promoted",
        "blocker": blocker,
        "candidate_sha": "b" * 40,
        "audited_surface_paths": candidate_surface,
    }


@pytest.mark.asyncio
async def test_deployed_gate_latch_alert_names_gate_paths_and_range(db_session):
    """A candidate-carried deployed gate repair raises an operator ERROR."""
    _project, version = await _make_project_and_version(db_session)
    version.base_sha = "a" * 40
    version.last_qualification = _latched_qual(
        blocker="candidate_operator_authorship_nonzero",
        candidate_surface={"backend/app/release_scoring.py": "b" * 40},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await _check_deployed_gate_latch(
        db_session,
        version,
        deployed_surface={"backend/app/release_scoring.py": "a" * 40},
        candidate_surface=version.last_qualification["audited_surface_paths"],
    )
    await db_session.commit()

    assert fired is True
    alert = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .one()
    )
    assert alert.severity == AlertSeverity.ERROR
    assert "candidate_operator_authorship_nonzero" in alert.title
    assert "backend/app/release_scoring.py" in alert.message
    assert f"{'a' * 40}...{'b' * 40}" in alert.message

    message = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == version.project_id
                )
            )
        )
        .scalars()
        .one()
    )
    assert message.context["audience"] == OPERATOR_ACTION_NEEDED
    assert message.context["compare_range"] == f"{'a' * 40}...{'b' * 40}"


@pytest.mark.asyncio
async def test_deployed_gate_latch_ignores_ordinary_blocked_candidate(db_session):
    """An unrelated candidate change does not raise a deployed-gate latch."""
    _project, version = await _make_project_and_version(db_session)
    version.base_sha = "a" * 40
    version.last_qualification = _latched_qual(
        blocker="candidate_operator_authorship_nonzero",
        candidate_surface={
            "backend/app/release_scoring.py": "a" * 40,
            "backend/app/unrelated_feature.py": "b" * 40,
        },
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    assert (
        await _check_deployed_gate_latch(
            db_session,
            version,
            deployed_surface={"backend/app/release_scoring.py": "a" * 40},
            candidate_surface=version.last_qualification["audited_surface_paths"],
        )
        is False
    )
    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert alerts == []


def test_release_blocked_is_operator_reachable():
    assert route_event("release_blocked") == OPERATOR_ACTION_NEEDED
    assert route_event("release_blocked") != STRATEGIST_INTERNAL


@pytest.mark.asyncio
async def test_external_delivery_blocker_alert_names_promotion_switch(db_session):
    """An external qualification blocker names the setting that clears it."""
    _project, version = await _make_project_and_version(db_session)
    version.phase_changed_at = _utcnow() - timedelta(days=15)
    version.last_qualification = {
        "status": "external_blocker",
        "blocker": "promotion_disabled",
        "blocker_class": "external_delivery",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    assert await _check_version_stabilize_stall(db_session, version) is True
    await db_session.commit()

    alert = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .one()
    )
    assert "project promotion is disabled" in alert.message
    assert "Enable project promotion before requesting qualification" in alert.message

    message = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == version.project_id
                )
            )
        )
        .scalars()
        .one()
    )
    assert message.context["blocker"] == "promotion_disabled"
    assert message.context["blocker_class"] == "external_delivery"
    assert message.context["remedy"] == "enable_project_promotion"
    assert "Enable project promotion before requesting qualification" in message.content

    # The existing phase-change anchor deduplicates repeated sweep passes.
    assert await _check_version_stabilize_stall(db_session, version) is False
    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_deployed_gate_latch_handles_more_than_github_compare_file_cap(
    db_session,
):
    """The alert keeps an implicated path after 300 tree entries."""
    base = [
        {"path": f"backend/app/file_{index}.py", "sha": f"{index:040x}"}
        for index in range(350)
    ]
    candidate = [dict(entry) for entry in base]
    candidate.append(
        {"path": "backend/app/release_scoring.py", "sha": "f" * 40}
    )
    _project, version = await _make_project_and_version(db_session)
    version.base_sha = "a" * 40
    version.last_qualification = _latched_qual(
        blocker="candidate_operator_authorship_nonzero", candidate_surface={}
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await _check_deployed_gate_latch(
        db_session,
        version,
        base_tree_entries=base,
        candidate_tree_entries=candidate,
    )

    assert fired is True
    alert = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .one()
    )
    assert "backend/app/release_scoring.py" in alert.message


# ---------------------------------------------------------------------------
# Durable-verdict sourcing — the latch must survive the auto-retrigger stub
# ---------------------------------------------------------------------------

_AUTHORSHIP_GATE = "candidate_operator_authorship_nonzero"
_GATE_MODULE = "backend/app/release_commit_authorship.py"


def _retrigger_pending_stub() -> dict:
    """Exactly what the auto-retrigger stamps seconds after finalize.

    No blocker, no candidate_sha, no audited_surface_paths — every terminal fact
    the old detector read is gone.
    """
    return {"status": "pending", "confidence": 0.0}


async def _seed_latched_verdict(
    db_session, version, *, terminal_status: str, blocker: str | None
):
    verdict = QualificationVerdict(
        project_id=version.project_id,
        release_version_id=version.id,
        candidate_sha="b" * 40,
        terminal_status=terminal_status,
        blocker=blocker,
        finalized_at=_utcnow().replace(tzinfo=None),
    )
    db_session.add(verdict)
    version.base_sha = "a" * 40
    version.qualification_attempts = 3
    version.last_qualification = _retrigger_pending_stub()
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return verdict


def _patch_surfaces(mocker, requested_refs: list[str]):
    """Stub only the two GitHub tree fetches; all detector logic stays real."""
    mocker.patch(
        "app.release_stall_alerts._deployed_audited_surface",
        return_value=("a" * 40, {_GATE_MODULE: "a" * 40}),
    )

    async def _fake_at_ref(_session, _version, ref):
        requested_refs.append(ref)
        return {_GATE_MODULE: "c" * 40}

    mocker.patch(
        "app.release_stall_alerts._audited_surface_at_ref", side_effect=_fake_at_ref
    )


@pytest.mark.asyncio
async def test_deployed_gate_latch_fires_from_the_durable_verdict_after_last_qualification_is_overwritten(
    db_session, mocker
):
    """The latch fires off the append-only verdict, not the mutable blob.

    Reproduces the v34 production failure: finalize wrote a not_promoted verdict,
    the auto-retrigger overwrote ``last_qualification`` with a pending stub 28
    seconds later, and the reaper sweep (60s cycle) never saw the terminal
    record. Sourcing the terminal facts from ``QualificationVerdict`` and
    re-deriving the candidate surface at the verdict's ``candidate_sha`` makes
    the alert fire anyway.
    """
    _project, version = await _make_project_and_version(db_session)
    await _seed_latched_verdict(
        db_session, version, terminal_status="not_promoted", blocker=_AUTHORSHIP_GATE
    )
    # The blob genuinely carries nothing the old detector needed.
    assert version.last_qualification.get("blocker") is None
    assert "audited_surface_paths" not in version.last_qualification

    requested_refs: list[str] = []
    _patch_surfaces(mocker, requested_refs)

    fired = await _check_version_calcification(db_session, version)
    await db_session.commit()

    assert fired is True
    # The candidate surface was re-derived at the VERDICT's candidate_sha.
    assert requested_refs == ["b" * 40]

    alert = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .one()
    )
    assert alert.event_type == "release_blocked"
    assert alert.severity == AlertSeverity.ERROR
    assert alert.title == f"Release v{version.version_number} gate latched: {_AUTHORSHIP_GATE}"
    assert _GATE_MODULE in alert.message
    assert f"{'a' * 40}...{'b' * 40}" in alert.message
    assert "release-machinery hotfix lane" in alert.message
    assert version.calcification_alerted_blocker == _AUTHORSHIP_GATE

    message = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == version.project_id
                )
            )
        )
        .scalars()
        .one()
    )
    assert message.context["calcification"] == "deployed_gate_latch"
    assert message.context["implicated_paths"] == [_GATE_MODULE]
    assert message.context["compare_range"] == f"{'a' * 40}...{'b' * 40}"


@pytest.mark.asyncio
async def test_no_alert_when_newest_verdict_is_not_terminal(db_session, mocker):
    """An in-flight verdict row is not evidence — no alert, no GitHub lookup."""
    _project, version = await _make_project_and_version(db_session)
    await _seed_latched_verdict(
        db_session, version, terminal_status="", blocker=_AUTHORSHIP_GATE
    )

    requested_refs: list[str] = []
    _patch_surfaces(mocker, requested_refs)

    assert await _check_version_calcification(db_session, version) is False
    assert requested_refs == []
    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert alerts == []
    assert version.calcification_alerted_blocker is None


@pytest.mark.asyncio
async def test_durable_dry_run_verdict_keeps_the_dry_run_carve_out(db_session, mocker):
    """A dry_run terminal verdict stays silent even though it IS terminal."""
    _project, version = await _make_project_and_version(db_session)
    await _seed_latched_verdict(
        db_session, version, terminal_status="dry_run", blocker=_AUTHORSHIP_GATE
    )

    requested_refs: list[str] = []
    _patch_surfaces(mocker, requested_refs)

    assert await _check_version_calcification(db_session, version) is False
    assert requested_refs == []


@pytest.mark.asyncio
async def test_legacy_version_without_verdict_row_still_uses_last_qualification(
    db_session, mocker
):
    """No verdict row at all (legacy version) falls back to the mutable blob."""
    _project, version = await _make_project_and_version(db_session)
    version.base_sha = "a" * 40
    version.qualification_attempts = 3
    version.last_qualification = _latched_qual(
        blocker=_AUTHORSHIP_GATE, candidate_surface={_GATE_MODULE: "c" * 40}
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    requested_refs: list[str] = []
    _patch_surfaces(mocker, requested_refs)

    assert await _check_version_calcification(db_session, version) is True
    # The blob already carried the surface, so no re-derivation was needed.
    assert requested_refs == []
    alert = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .one()
    )
    assert f"{'a' * 40}...{'b' * 40}" in alert.message


def _mcw_qual(
    *, enabled_seconds_ago: float, last_alerted_seconds_ago: float | None = None
):
    """Build a managed_check_wait pending qualification dict."""
    enabled_at = (_utcnow() - timedelta(seconds=enabled_seconds_ago)).isoformat()
    pending: dict = {
        "pr_number": 99,
        "pr_url": "https://github.com/acme/widget/pull/99",
        "managed_check_wait": True,
        "enabled_at": enabled_at,
        "ci_state": "pending",
    }
    if last_alerted_seconds_ago is not None:
        pending["mcw_last_alerted_at"] = (
            _utcnow() - timedelta(seconds=last_alerted_seconds_ago)
        ).isoformat()
    return {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "confidence": 0.95,
        "promotion_pending": pending,
    }


# ---------------------------------------------------------------------------
# _check_version_managed_check_wait_stall
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcw_stall_does_not_fire_before_threshold(db_session):
    """No alert fires before the managed-wait threshold."""
    _project, version = await _make_project_and_version(db_session)
    version.last_qualification = _mcw_qual(
        enabled_seconds_ago=_MANAGED_CHECK_WAIT_STALL_SECONDS - 60
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await _check_version_managed_check_wait_stall(db_session, version)
    assert fired is False

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_mcw_stall_fires_warning_at_threshold(db_session):
    """At the threshold a WARNING alert fires and stamps the dedup anchor."""
    _project, version = await _make_project_and_version(db_session)
    version.last_qualification = _mcw_qual(
        enabled_seconds_ago=_MANAGED_CHECK_WAIT_STALL_SECONDS + 60
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await _check_version_managed_check_wait_stall(db_session, version)
    await db_session.commit()
    assert fired is True

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert "promotion check wait" in alerts[0].title.lower()

    await db_session.refresh(version)
    pending = version.last_qualification["promotion_pending"]
    assert pending.get("mcw_last_alerted_at") is not None
    assert pending.get("mcw_alert_count") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_enabled_at", [None, "bad-clock"])
async def test_mcw_stall_repairs_missing_or_invalid_clock(
    db_session, raw_enabled_at
):
    _project, version = await _make_project_and_version(db_session)
    started_at = (
        _utcnow() - timedelta(seconds=_MANAGED_CHECK_WAIT_STALL_SECONDS + 60)
    ).isoformat()
    pending = {
        "pr_number": 42,
        "pr_url": "https://github.com/acme/widget/pull/42",
    }
    if raw_enabled_at is not None:
        pending["enabled_at"] = raw_enabled_at
    version.last_qualification = {
        "status": "pending",
        "started_at": started_at,
        "candidate_sha": "abcd" * 10,
        "promotion_pending": pending,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await _check_version_managed_check_wait_stall(db_session, version)
    await db_session.commit()

    assert fired is True
    await db_session.refresh(version)
    canonical = version.last_qualification["promotion_pending"]
    assert canonical["enabled_at"] == started_at
    assert canonical["managed_check_wait"] is True


@pytest.mark.asyncio
async def test_mcw_stall_escalates_to_error_after_critical_threshold(db_session):
    """At the critical threshold the alert severity escalates to ERROR."""
    _project, version = await _make_project_and_version(db_session)
    version.last_qualification = _mcw_qual(
        enabled_seconds_ago=_MANAGED_CHECK_WAIT_STALL_CRITICAL_SECONDS + 60
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await _check_version_managed_check_wait_stall(db_session, version)
    await db_session.commit()
    assert fired is True

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR


@pytest.mark.asyncio
async def test_mcw_stall_suppressed_within_realert_interval(db_session):
    """A second call within the re-alert window returns False."""
    _project, version = await _make_project_and_version(db_session)
    version.last_qualification = _mcw_qual(
        enabled_seconds_ago=_MANAGED_CHECK_WAIT_STALL_SECONDS + 120,
        last_alerted_seconds_ago=_MANAGED_CHECK_WAIT_REALERT_INTERVAL_SECONDS - 60,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await _check_version_managed_check_wait_stall(db_session, version)
    assert fired is False

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_mcw_stall_refires_after_realert_interval(db_session):
    """A call after the re-alert window fires and increments alert_count."""
    _project, version = await _make_project_and_version(db_session)
    version.last_qualification = _mcw_qual(
        enabled_seconds_ago=_MANAGED_CHECK_WAIT_STALL_SECONDS + 120,
        last_alerted_seconds_ago=_MANAGED_CHECK_WAIT_REALERT_INTERVAL_SECONDS + 60,
    )
    pending = dict(version.last_qualification["promotion_pending"])
    pending["mcw_alert_count"] = 1
    qual = dict(version.last_qualification)
    qual["promotion_pending"] = pending
    version.last_qualification = qual
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await _check_version_managed_check_wait_stall(db_session, version)
    await db_session.commit()
    assert fired is True

    await db_session.refresh(version)
    assert version.last_qualification["promotion_pending"]["mcw_alert_count"] == 2


# ---------------------------------------------------------------------------
# _alert_promotion_deploy_failed / _resolve_promotion_deploy_failed_alert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_failed_alert_first_fire(db_session, mocker):
    """First call creates an ERROR alert and stamps deploy_confirm_alerted_at."""
    mocker.patch(
        "app.release_stall_alerts._release_qualifier_module",
        return_value=rq,
    )
    # Silence the _notify_operator call so the test stays self-contained.
    mocker.patch(
        "app.release_promotion._notify_operator",
        return_value=None,
        create=True,
    )
    _project, version = await _make_project_and_version(db_session)
    version.serving_status = "deploy_failed"
    version.deploy_failure_reason = "rolled_back"
    version.promoted_sha = "abc123" * 3
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await _alert_promotion_deploy_failed(db_session, version)
    await db_session.commit()
    assert fired is True

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert _deploy_failed_alert_title(version.version_number) == alerts[0].title

    await db_session.refresh(version)
    assert version.deploy_confirm_alerted_at is not None


@pytest.mark.asyncio
async def test_deploy_failed_operator_message_uses_shared_outcome_sentence(
    db_session, mocker
):
    """The alert and operator callers compose the full outcome sentence once."""
    mocker.patch(
        "app.release_stall_alerts._release_qualifier_module",
        return_value=rq,
    )
    notify = mocker.patch(
        "app.release_promotion._notify_operator",
        return_value=None,
        create=True,
    )
    _project, version = await _make_project_and_version(db_session)
    version.serving_status = "deploy_failed"
    version.deploy_failure_reason = "rolled_back"
    version.promoted_sha = "abc123" * 3
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    await _alert_promotion_deploy_failed(db_session, version)
    await db_session.commit()

    alert = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .one()
    )
    operator_message = notify.await_args.args[3]
    for message in (alert.message, operator_message):
        assert "This The" not in message
        assert "The promotion did NOT ship" in message


@pytest.mark.asyncio
async def test_deploy_failed_alert_reason_codes(db_session, mocker):
    """Known reason codes appear verbatim in the alert message."""
    mocker.patch(
        "app.release_stall_alerts._release_qualifier_module",
        return_value=rq,
    )
    mocker.patch(
        "app.release_promotion._notify_operator",
        return_value=None,
        create=True,
    )
    _project, version = await _make_project_and_version(db_session)
    version.serving_status = "deploy_failed"
    version.deploy_failure_reason = "sha_mismatch"
    version.promoted_sha = "deadbeef1234"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    await _alert_promotion_deploy_failed(db_session, version)
    await db_session.commit()

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert any("sha_mismatch" in a.message for a in alerts)


@pytest.mark.asyncio
async def test_deploy_failed_alert_idempotent(db_session, mocker):
    """Calling _alert_promotion_deploy_failed twice does not create a second alert."""
    mocker.patch(
        "app.release_stall_alerts._release_qualifier_module",
        return_value=rq,
    )
    mocker.patch(
        "app.release_promotion._notify_operator",
        return_value=None,
        create=True,
    )
    _project, version = await _make_project_and_version(db_session)
    version.serving_status = "deploy_failed"
    version.deploy_failure_reason = "halted"
    version.promoted_sha = "cafebabe1234"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await _alert_promotion_deploy_failed(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    second = await _alert_promotion_deploy_failed(db_session, version)
    await db_session.commit()

    assert first is True
    assert second is False

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_resolve_deploy_failed_alert_noop_when_none(db_session):
    """_resolve_promotion_deploy_failed_alert returns 0 when no open alert exists."""
    _project, version = await _make_project_and_version(db_session)

    count = await _resolve_promotion_deploy_failed_alert(db_session, version)
    await db_session.commit()
    assert count == 0


@pytest.mark.asyncio
async def test_resolve_deploy_failed_alert_dismisses_open(db_session, mocker):
    """_resolve_promotion_deploy_failed_alert dismisses an open deploy-failed alert."""
    mocker.patch(
        "app.release_stall_alerts._release_qualifier_module",
        return_value=rq,
    )
    mocker.patch(
        "app.release_promotion._notify_operator",
        return_value=None,
        create=True,
    )
    _project, version = await _make_project_and_version(db_session)
    version.serving_status = "deploy_failed"
    version.deploy_failure_reason = "no_new_revision"
    version.promoted_sha = "001122334455"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    await _alert_promotion_deploy_failed(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    count = await _resolve_promotion_deploy_failed_alert(db_session, version)
    await db_session.commit()
    assert count == 1

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == version.project_id,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_resolve_deploy_failed_alert_idempotent(db_session, mocker):
    """Calling _resolve_promotion_deploy_failed_alert twice returns 0 on the second call."""
    mocker.patch(
        "app.release_stall_alerts._release_qualifier_module",
        return_value=rq,
    )
    mocker.patch(
        "app.release_promotion._notify_operator",
        return_value=None,
        create=True,
    )
    _project, version = await _make_project_and_version(db_session)
    version.serving_status = "deploy_failed"
    version.deploy_failure_reason = "rolled_back"
    version.promoted_sha = "aabbccddeeff"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    await _alert_promotion_deploy_failed(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await _resolve_promotion_deploy_failed_alert(db_session, version)
    await db_session.commit()
    second = await _resolve_promotion_deploy_failed_alert(db_session, version)
    await db_session.commit()

    assert first == 1
    assert second == 0

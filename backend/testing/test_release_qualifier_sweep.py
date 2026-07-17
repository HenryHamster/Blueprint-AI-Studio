"""Tests for the event-driven auto-qualification trigger sweep.

``release_qualifier_sweep._run_auto_qualification_trigger_sweep`` /
``_run_auto_qualification_trigger_for_version`` run on EVERY reaper sweep cycle
in production (the ``auto_qualification_triggers`` knob defaults to "observe")
yet shipped with zero test references — and that is precisely where the CI-red
suppression was quietly dead:

    from app.ci_resolution import _resolve_candidate_ci   # WRONG MODULE

``_resolve_candidate_ci`` is defined in ``release_qualifier``, not in
``ci_resolution``.  The ImportError was caught by the broad ``except Exception``
around the call, logged as a benign "CI state fetch failed … treating as
unknown" warning, and left ``automain_ci_state`` at ``None`` on every single
sweep.  ``None`` is not ``"red"``, so the resolver's CI-red suppression could
never fire: an advanced candidate sitting on RED CI was always treated as
armed.  In "on" mode that means burning a ~15-minute qualification (audit +
merge-grade children) against a known-broken tree, every cycle.

These tests exercise the wiring end-to-end so the suppression is a live path:
the red candidate is held, the green candidate fires, and a genuine GitHub I/O
failure still degrades to "unknown" (the intentional fail-open) rather than
being confused with the wiring bug.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock

import pytest

from app import ci_resolution
from app import release_qualifier as rq
from app import release_qualifier_sweep as rqs
from app import release_version as rv
from app.models import Alert, Project

_OLD_SHA = "aaaa1111" * 5
_NEW_SHA = "bbbb2222" * 5


def _shared_session_factory(db_session):
    """Session factory handing back the test's shared session (no-op __aexit__).

    The "on" mode fire path opens its own session via the injected factory; under
    the test harness the single StaticPool connection means that session must BE
    the test session, and the test must keep owning its lifecycle.
    """

    @contextlib.asynccontextmanager
    async def _factory():
        yield db_session

    return _factory


async def _make_project_and_version(db_session, *, phase: str = "stabilize"):
    project = Project(
        name="RQS-Test",
        user_id="rqs-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    if phase != "build":
        rv.set_version_phase(version, phase, reasoning="test setup")
        db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return project, version


async def _seed_qual(db_session, version, qual: dict):
    version.last_qualification = qual
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return version


def _patch_sweep_io(
    mocker,
    *,
    automain_sha: str = _NEW_SHA,
    ci_state: str | None = "green",
    ci_raises: bool = False,
    fully_met: bool = False,
    requalification_ready: bool = True,
):
    """Patch the sweep's GitHub + ledger + readiness I/O; return the mocks.

    ``_resolve_candidate_ci`` is patched on ``app.release_qualifier`` — the module
    that DEFINES it.  That is load-bearing for these tests: if the sweep ever
    reaches for the symbol on ``app.ci_resolution`` again, this patch does not
    apply, the lookup fails, and the CI-red assertions below go red.
    """
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "acme", "widget")),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=automain_sha),
    )
    if ci_raises:
        resolve_ci = mocker.patch(
            "app.release_qualifier._resolve_candidate_ci",
            new=AsyncMock(side_effect=RuntimeError("github 502")),
        )
    else:
        resolve_ci = mocker.patch(
            "app.release_qualifier._resolve_candidate_ci",
            new=AsyncMock(
                return_value={"state": ci_state, "ci_green": ci_state == "green"}
            ),
        )
    mocker.patch(
        "app.category_verdict_ledger.is_candidate_fully_met",
        new=AsyncMock(return_value=fully_met),
    )
    mocker.patch(
        "app.release_readiness.compute_requalification_readiness",
        new=AsyncMock(return_value={"requalification_ready": requalification_ready}),
    )
    run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )
    return resolve_ci, run_qual


def _patch_mode(mocker, mode: str):
    mocker.patch(
        "app.runtime_config_state.get_auto_qualification_triggers_mode",
        return_value=mode,
    )


# ===========================================================================
# Wiring pin — the resolver lives in release_qualifier, not ci_resolution
# ===========================================================================


def test_resolve_candidate_ci_is_defined_in_release_qualifier_only():
    """The qualifier-scoped STRICT CI resolver lives in ``release_qualifier``.

    ``ci_resolution`` holds the SHARED merge-path helpers and deliberately does
    NOT define ``_resolve_candidate_ci`` (it fails open on an empty check-run
    list, which is exactly what the qualifier must not do).  Importing the symbol
    from there — as the sweep did — can only ever raise ImportError.
    """
    assert hasattr(rq, "_resolve_candidate_ci")
    assert not hasattr(ci_resolution, "_resolve_candidate_ci")


def test_sweep_does_not_import_resolve_candidate_ci_from_ci_resolution():
    """Source-level regression pin for the wrong-module import."""
    from pathlib import Path

    src = Path(rqs.__file__).read_text()
    assert "from app.ci_resolution import _resolve_candidate_ci" not in src


# ===========================================================================
# CI-red suppression — the path the broken import kept dead
# ===========================================================================


@pytest.mark.asyncio
async def test_ci_red_suppresses_advanced_dry_run_candidate(db_session, mocker):
    """THE regression test: an advanced candidate whose CI is RED is suppressed.

    A dry_run version whose candidate_sha has been superseded by a new automain
    HEAD is normally armed (T3 post_dry_run_advance).  When the new HEAD's CI is
    definitively red, the resolver must NOT fire — qualifying a known-broken tree
    burns an audit + merge-grade for a foregone verdict.

    Before the import fix this suppression was unreachable: the ImportError left
    ``automain_ci_state`` None, and None != "red", so run_qualification fired.
    """
    _patch_mode(mocker, "on")
    _, version = await _make_project_and_version(db_session)
    await _seed_qual(
        db_session, version, {"status": "dry_run", "candidate_sha": _OLD_SHA}
    )
    resolve_ci, run_qual = _patch_sweep_io(mocker, ci_state="red")

    await rqs._run_auto_qualification_trigger_sweep(
        [version], db_session, session_factory=_shared_session_factory(db_session)
    )

    # The strict resolver was actually reached, and asked about the NEW head.
    resolve_ci.assert_awaited_once_with("tok", "acme", "widget", _NEW_SHA)
    # ... and the red verdict suppressed the fire.
    run_qual.assert_not_called()


@pytest.mark.asyncio
async def test_ci_green_fires_advanced_dry_run_candidate(db_session, mocker):
    """Control: the suppression is scoped to RED — a green advanced candidate
    still fires, so the fix does not simply wedge the trigger shut."""
    _patch_mode(mocker, "on")
    _, version = await _make_project_and_version(db_session)
    await _seed_qual(
        db_session, version, {"status": "dry_run", "candidate_sha": _OLD_SHA}
    )
    resolve_ci, run_qual = _patch_sweep_io(mocker, ci_state="green")

    await rqs._run_auto_qualification_trigger_sweep(
        [version], db_session, session_factory=_shared_session_factory(db_session)
    )

    resolve_ci.assert_awaited_once()
    run_qual.assert_awaited_once()


@pytest.mark.asyncio
async def test_ci_red_suppresses_advanced_not_promoted_candidate(db_session, mocker):
    """The OTHER advancement leg (T1 not_promoted / criterion_coverage_incomplete)
    reads the same CI state — it too was suppression-blind while the import was
    broken."""
    _patch_mode(mocker, "on")
    _, version = await _make_project_and_version(db_session)
    await _seed_qual(
        db_session,
        version,
        {
            "status": "not_promoted",
            "candidate_sha": _OLD_SHA,
            "blocker": "criterion_coverage_incomplete",
        },
    )
    # Not fully met: the ledger cannot short-circuit to coverage_complete, so the
    # decision genuinely rests on the candidate-advanced + CI legs.
    resolve_ci, run_qual = _patch_sweep_io(mocker, ci_state="red", fully_met=False)

    await rqs._run_auto_qualification_trigger_sweep(
        [version], db_session, session_factory=_shared_session_factory(db_session)
    )

    resolve_ci.assert_awaited_once_with("tok", "acme", "widget", _NEW_SHA)
    run_qual.assert_not_called()


@pytest.mark.asyncio
async def test_ci_green_fires_advanced_not_promoted_candidate(db_session, mocker):
    """Control for the T1 leg: green CI on the new head re-drives the not-met
    grade (the sha-keyed verdict can only change on a NEW candidate)."""
    _patch_mode(mocker, "on")
    _, version = await _make_project_and_version(db_session)
    await _seed_qual(
        db_session,
        version,
        {
            "status": "not_promoted",
            "candidate_sha": _OLD_SHA,
            "blocker": "criterion_coverage_incomplete",
        },
    )
    _, run_qual = _patch_sweep_io(mocker, ci_state="green", fully_met=False)

    await rqs._run_auto_qualification_trigger_sweep(
        [version], db_session, session_factory=_shared_session_factory(db_session)
    )

    run_qual.assert_awaited_once()


@pytest.mark.asyncio
async def test_ci_io_failure_degrades_to_unknown_and_still_fires(db_session, mocker):
    """A GENUINE GitHub I/O failure still fails open to "unknown" state.

    This is the behaviour the swallowed ImportError was masquerading as, and it
    is intentional: a flaky GitHub call must not hold a candidate hostage.  Only
    a DEFINITIVE red suppresses.  Keeping this distinct is the whole point of
    binding the resolver outside the try/except.
    """
    _patch_mode(mocker, "on")
    _, version = await _make_project_and_version(db_session)
    await _seed_qual(
        db_session, version, {"status": "dry_run", "candidate_sha": _OLD_SHA}
    )
    resolve_ci, run_qual = _patch_sweep_io(mocker, ci_raises=True)

    await rqs._run_auto_qualification_trigger_sweep(
        [version], db_session, session_factory=_shared_session_factory(db_session)
    )

    resolve_ci.assert_awaited_once()
    run_qual.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_ci_call_when_candidate_sha_has_not_advanced(db_session, mocker):
    """The CI probe is the sweep's only GitHub I/O — it must stay scoped to the
    advanced case (an unchanged HEAD carries no new information)."""
    _patch_mode(mocker, "on")
    _, version = await _make_project_and_version(db_session)
    await _seed_qual(
        db_session, version, {"status": "dry_run", "candidate_sha": _OLD_SHA}
    )
    resolve_ci, run_qual = _patch_sweep_io(mocker, automain_sha=_OLD_SHA)

    await rqs._run_auto_qualification_trigger_sweep(
        [version], db_session, session_factory=_shared_session_factory(db_session)
    )

    resolve_ci.assert_not_called()
    # Nothing advanced and dry_run is terminal-ish → no fire.
    run_qual.assert_not_called()


# ===========================================================================
# Mode knob — off / observe / on
# ===========================================================================


@pytest.mark.asyncio
async def test_off_mode_does_no_work(db_session, mocker):
    _patch_mode(mocker, "off")
    _, version = await _make_project_and_version(db_session)
    await _seed_qual(
        db_session, version, {"status": "dry_run", "candidate_sha": _OLD_SHA}
    )
    resolve_ci, run_qual = _patch_sweep_io(mocker, ci_state="green")
    resolver = mocker.patch("app.qualification_triggers.resolve_qualification_trigger")

    await rqs._run_auto_qualification_trigger_sweep(
        [version], db_session, session_factory=_shared_session_factory(db_session)
    )

    resolver.assert_not_called()
    resolve_ci.assert_not_called()
    run_qual.assert_not_called()


@pytest.mark.asyncio
async def test_observe_mode_decides_but_never_spawns(db_session, mocker):
    """The production default: the resolver runs (and the CI probe with it) but
    no qualification is spawned."""
    _patch_mode(mocker, "observe")
    _, version = await _make_project_and_version(db_session)
    await _seed_qual(
        db_session, version, {"status": "dry_run", "candidate_sha": _OLD_SHA}
    )
    resolve_ci, run_qual = _patch_sweep_io(mocker, ci_state="green")

    await rqs._run_auto_qualification_trigger_sweep(
        [version], db_session, session_factory=_shared_session_factory(db_session)
    )

    # The decision path (including the CI probe) is fully exercised …
    resolve_ci.assert_awaited_once()
    # … but observe mode spawns nothing.
    run_qual.assert_not_called()


# ===========================================================================
# Sweep-level fan-out
# ===========================================================================


@pytest.mark.asyncio
async def test_wrong_phase_version_never_fires(db_session, mocker):
    """T0 hard hold: a build-phase version is not a qualification candidate."""
    _patch_mode(mocker, "on")
    _, version = await _make_project_and_version(db_session, phase="build")
    await _seed_qual(
        db_session, version, {"status": "dry_run", "candidate_sha": _OLD_SHA}
    )
    _, run_qual = _patch_sweep_io(mocker, ci_state="green")

    await rqs._run_auto_qualification_trigger_sweep(
        [version], db_session, session_factory=_shared_session_factory(db_session)
    )

    run_qual.assert_not_called()


@pytest.mark.asyncio
async def test_sweep_isolates_a_failing_version(db_session, mocker):
    """One version blowing up must not abort the sweep for the others."""
    _patch_mode(mocker, "on")
    _, version_a = await _make_project_and_version(db_session)
    _, version_b = await _make_project_and_version(db_session)

    seen: list = []

    async def _per_version(version, session, **kwargs):
        if version.id == version_a.id:
            raise RuntimeError("boom")
        seen.append(version.id)

    mocker.patch.object(
        rqs, "_run_auto_qualification_trigger_for_version", new=_per_version
    )

    await rqs._run_auto_qualification_trigger_sweep(
        [version_a, version_b],
        db_session,
        session_factory=_shared_session_factory(db_session),
    )

    assert seen == [version_b.id]


# ===========================================================================
# Infra-failure redrive cap — operator alert
# ===========================================================================


@pytest.mark.asyncio
async def test_infra_failure_redrive_cap_raises_operator_alert_once(db_session, mocker):
    """When the infra-failure redrive cap is exhausted, an operator Alert is raised
    (once).  A subsequent re-drive attempt on the same version does NOT duplicate
    the alert — the ``infra_failure_loop_escalated`` guard flag prevents that.

    Acceptance evidence for task fix(qualify): alert operator when infra-failure
    redrive cap exhausts.
    """
    from app.release_qualifier_redrive import (
        _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
        _redrive_infra_failure_qualification,
    )

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "ca11face" * 5,
        "error": "migration_apply_check_unavailable",
        "consecutive_infra_errors": _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # First call: cap is hit -> should return False AND raise an Alert.
    result_a = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result_a is False, (
        "redrive must be blocked when cap is reached"
    )

    # An operator Alert should have been raised.
    from sqlalchemy import select as _select

    alerts = (
        (await db_session.execute(
            _select(Alert).where(Alert.project_id == project.id)
        ))
        .scalars()
        .all()
    )
    assert len(alerts) == 1, "exactly one Alert must be created on first cap hit"
    assert "infra-failure" in alerts[0].title.lower() or "infra" in alerts[0].title.lower()
    assert str(version.version_number) in alerts[0].title

    # The escalation guard flag must be set.
    await db_session.refresh(version)
    assert version.last_qualification.get("infra_failure_loop_escalated") is True

    # Second call: same cap level, guard flag already set -> no duplicate alert.
    result_b = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result_b is False, (
        "redrive must still be blocked on second attempt"
    )

    alerts_after = (
        (await db_session.execute(
            _select(Alert).where(Alert.project_id == project.id)
        ))
        .scalars()
        .all()
    )
    assert len(alerts_after) == 1, (
        "alert must NOT be duplicated on subsequent cap hits"
    )


@pytest.mark.asyncio
async def test_infra_failure_cap_alert_persists_failing_dimension_and_signature(
    db_session, mocker
):
    """The infra-failure cap Alert must carry the failing dimension and the
    infra-failure signature in a PERSISTED field so an operator can see which
    gate looped and why.

    The Alert model has no JSON ``extra`` column; a prior ``extra={...}`` kwarg
    was silently dropped on persist.  This test re-reads the Alert row FROM THE
    DATABASE (a fresh query, not the in-memory instance) and asserts the
    structured evidence survives readback — i.e. it lives in ``message``, not in
    a dropped attribute.
    """
    from app.release_qualifier_redrive import (
        _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
        _redrive_infra_failure_qualification,
    )

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "ca11face" * 5,
        "error": "boot_prereq_evidence_unavailable",
        "failing_dimension": "env_preflight",
        "infra_failure_signature": "env_preflight:boot_prereq_evidence_unavailable",
        "consecutive_infra_errors": _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is False, "redrive must be blocked when cap is reached"

    from sqlalchemy import select as _select

    alert = (
        (await db_session.execute(
            _select(Alert).where(Alert.project_id == project.id)
        ))
        .scalars()
        .one()
    )
    # Force a genuine DB reload of the mapped columns — proving the evidence was
    # persisted, not merely held on the in-memory instance (a dropped ``extra``
    # kwarg would not survive this round-trip).
    await db_session.refresh(alert)

    # The structured evidence must have survived persistence in ``message``.
    assert "env_preflight" in alert.message, (
        "failing dimension must survive readback in the persisted alert message"
    )
    assert "env_preflight:boot_prereq_evidence_unavailable" in alert.message, (
        "infra-failure signature must survive readback in the persisted alert message"
    )

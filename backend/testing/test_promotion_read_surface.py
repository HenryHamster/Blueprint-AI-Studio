"""The release-promotion READ surface must not report things that are not true.

Two defects, both of the same shape: a gate that reports a state without a
truthful reason, so a wedge stays invisible until a human digs.

DEFECT 1 — ``_record_promotion_failure`` never pinned ``candidate_sha`` itself.

Its success twin ``_record_promotion_success`` takes ``candidate_sha`` and
writes it into the record explicitly; the failure twin took the key only from
whatever the caller happened to leave in ``base_record``.  Every current caller
does supply it, so this was LATENT — but the degenerate case is strictly worse
than an ordinary failure, which is why it is closed rather than documented: an
empty ``candidate_sha`` on a ``promotion_failed`` record sends the sweep
re-driver down its ``candidate_identity_unknown`` branch, and unlike the sibling
``candidate_unchanged`` branch that one has no Alert.  The version holds
silently, forever, with the read surface reporting nothing wrong.

DEFECT 1b — a version that promoted SUCCESSFULLY kept the previous attempt's
``error``.  ``base_record`` on the reconcile path is ``dict(last_qualification)``
of a ``promotion_failed`` record, so ``status`` was overwritten and ``error`` /
``detail`` / ``conflict`` rode through: v40-v44 all promoted and all still read
``error: "required_check_failed"``.  ``error`` was therefore unreadable as a
state signal.

DEFECT 2 — the trigger read surface named an executor that did not own the
work.  ``next_expected_trigger`` was hardcoded ``"sweep_retry"`` for every
armed decision, so the panel reported "armed, nothing blocking, the sweep will
retry" for a version whose trigger the sweep DISCARDS as re-driver-owned.  The
observed wedge read ``{"fire": true, "trigger": "blockers_cleared", "reason":
"armed"}`` while the sweep dropped it every cycle and the named owner did not
exist in the deployed build.  The discard was log-only, so nothing an operator
could read back said a decision had been made and thrown away.
"""

import ast
import inspect
from unittest.mock import AsyncMock

import pytest

from app.qualification_triggers import (
    REDRIVE_TRIGGER_OWNERS,
    SWEEP_OWNED_TRIGGERS,
    explain_qualification_trigger,
    qualification_trigger_owner,
    resolve_qualification_trigger,
)

from _release_qualifier_testhelpers import (
    _make_project_and_version,
    _shared_session_factory,
)

_CANDIDATE = "c0ffee" * 6 + "abcd"
_OTHER_CANDIDATE = "beadfa" * 6 + "1234"


# ===========================================================================
# DEFECT 1 — _record_promotion_failure pins candidate identity itself
# ===========================================================================


def _patch_failure_recorder_io(mocker, version):
    """Stub the two collaborators that need a live promote() transaction."""
    mocker.patch(
        "app.release_promotion._relock_version_for_promotion_write",
        new=AsyncMock(return_value=version),
    )
    mocker.patch(
        "app.release_promotion._record_qualification_verdict",
        new=AsyncMock(return_value=None),
    )


@pytest.mark.asyncio
async def test_failure_record_pins_candidate_sha_the_caller_omitted(
    db_session, mocker
):
    """The pin lives in the recorder, not in the caller's base_record.

    THE DEFECT: every one of the callers passes ``candidate_sha`` through
    ``base_record`` today, so nothing was observably broken — but the recorder
    trusted all of them, present and future, to keep doing so.  Its success
    twin does not; it pins the value itself.
    """
    from app.release_promotion import _record_promotion_failure

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "promotion_pending",
        "candidate_sha": _CANDIDATE,
    }
    db_session.add(version)
    await db_session.commit()
    _patch_failure_recorder_io(mocker, version)

    await _record_promotion_failure(
        db_session,
        version,
        {"confidence": 0.0},  # NO candidate_sha — the whole point
        "required_check_failed",
        lock_released=False,
    )

    assert version.last_qualification["candidate_sha"] == _CANDIDATE
    assert "candidate_sha_unresolved" not in version.last_qualification


@pytest.mark.asyncio
async def test_failure_record_prefers_the_explicit_candidate_argument(
    db_session, mocker
):
    """Explicit argument beats base_record beats the row's prior record."""
    from app.release_promotion import _record_promotion_failure

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"status": "pending", "candidate_sha": "0" * 40}
    db_session.add(version)
    await db_session.commit()
    _patch_failure_recorder_io(mocker, version)

    await _record_promotion_failure(
        db_session,
        version,
        {"candidate_sha": _OTHER_CANDIDATE},
        "merge_error",
        candidate_sha=_CANDIDATE,
        lock_released=False,
    )

    assert version.last_qualification["candidate_sha"] == _CANDIDATE


@pytest.mark.asyncio
async def test_failure_record_states_the_gap_rather_than_inventing_a_sha(
    db_session, mocker
):
    """When nothing resolves, SAY SO — do not fabricate, do not leave a blank.

    A blank is what routed the version into the uncapped, unalerted re-driver
    branch with no stated reason.  A wrong sha would be the same class of defect
    this whole file exists to close, so the recorder records the gap instead.
    """
    from app.release_promotion import _record_promotion_failure

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"status": "pending"}
    db_session.add(version)
    await db_session.commit()
    _patch_failure_recorder_io(mocker, version)

    await _record_promotion_failure(
        db_session,
        version,
        {"confidence": 0.0},
        "release_branch_failed",
        lock_released=False,
    )

    qual = version.last_qualification
    assert qual["candidate_sha_unresolved"] is True
    assert not qual.get("candidate_sha"), "no fabricated candidate identity"


@pytest.mark.asyncio
async def test_a_pinned_candidate_keeps_the_redriver_off_the_unalerted_branch(
    db_session, mocker
):
    """WHY the pin matters, proved against the branch it steers away from.

    ``candidate_identity_unknown`` has no attempt cap and no Alert — the hold is
    silent and unbounded.  A record written through the recorder must never land
    there while the version knows its own candidate.
    """
    from app.release_promotion import _record_promotion_failure
    from app.release_qualifier_redrive import (
        _PROMOTION_REDRIVE_DECISION_KEY,
        _redrive_promotion_failed_qualification,
    )

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "promotion_pending",
        "candidate_sha": _CANDIDATE,
    }
    db_session.add(version)
    await db_session.commit()
    _patch_failure_recorder_io(mocker, version)

    await _record_promotion_failure(
        db_session,
        version,
        {"confidence": 0.0},  # caller forgot the sha
        "required_check_failed",
        lock_released=False,
    )
    await db_session.commit()

    _patch_redrive_reads(mocker)
    fired = await _redrive_promotion_failed_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert fired is False  # same candidate, no green evidence -> a BOUNDED hold
    decision = version.last_qualification[_PROMOTION_REDRIVE_DECISION_KEY]
    assert decision["reason"] == "candidate_unchanged", decision
    assert decision["reason"] != "candidate_identity_unknown"


def _patch_redrive_reads(mocker, *, head_sha=_CANDIDATE):
    """Patch the live reads the promotion_failed re-driver makes."""
    mocker.patch(
        "app.release_qualifier_sweep._current_integration_audited_surface",
        new=AsyncMock(return_value={"backend/app/release_promotion.py": "s" * 40}),
    )
    mocker.patch(
        "app.release_readiness.auto_mark_stale_audit_findings",
        new=AsyncMock(return_value=0),
    )
    mocker.patch(
        "app.release_readiness.compute_requalification_readiness",
        new=AsyncMock(
            return_value={
                "requalification_ready": True,
                "reason": "all gating findings resolved",
                "gating_total": 1,
                "gating_resolved": 1,
            }
        ),
    )
    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(return_value={"gate_state": "no_signal", "head_sha": head_sha}),
    )


@pytest.mark.asyncio
async def test_an_unpinned_candidate_lands_on_the_unalerted_branch(
    db_session, mocker
):
    """The harm, pinned: a blank candidate really does reach the silent hold.

    This is the counterpart to the test above.  It keeps the fix honest — if
    ``candidate_identity_unknown`` ever stops being reachable, or gains a cap
    and an Alert of its own, this test says so instead of the fix quietly
    protecting against nothing.
    """
    from app.release_qualifier_redrive import (
        _PROMOTION_REDRIVE_DECISION_KEY,
        _redrive_promotion_failed_qualification,
    )
    from sqlalchemy.orm.attributes import flag_modified

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "promotion_failed",
        "error": "required_check_failed",
        "promotion_failure_count": 1,
        # candidate_sha DELIBERATELY absent — the degenerate record.
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()

    _patch_redrive_reads(mocker)
    fired = await _redrive_promotion_failed_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert fired is False
    decision = version.last_qualification[_PROMOTION_REDRIVE_DECISION_KEY]
    assert decision["reason"] == "candidate_identity_unknown", decision


# ===========================================================================
# DEFECT 1b — a promoted record must not carry the previous failure's error
# ===========================================================================


@pytest.mark.asyncio
async def test_promoted_record_clears_the_previous_attempts_failure_state(
    db_session, mocker
):
    """v40-v44 all promoted and all still read error='required_check_failed'.

    The reconcile path passes ``dict(last_qualification)`` of the
    ``promotion_failed`` record straight in as ``base_record``, so ``status``
    was overwritten while ``error`` rode through untouched.  An operator reading
    ``error`` back could not tell a currently-failing version from one that
    failed once and shipped weeks ago.
    """
    from app.release_promotion import _record_promotion_success

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    mocker.patch(
        "app.release_promotion._relock_version_for_promotion_write",
        new=AsyncMock(return_value=version),
    )
    mocker.patch(
        "app.release_promotion._record_qualification_verdict",
        new=AsyncMock(return_value=None),
    )

    # Exactly the shape release_reconciliation._reconcile_if_promotion_failed_landed
    # builds: the whole promotion_failed record, verbatim.
    base_record = {
        "status": "promotion_failed",
        "error": "required_check_failed",
        "detail": "backend-tests (shard 3) failed",
        "conflict": False,
        "candidate_sha": _CANDIDATE,
        "promotion_failure_count": 2,
        "confidence": 0.91,
    }

    await _record_promotion_success(
        db_session,
        version,
        _CANDIDATE,
        base_record,
        "https://github.com/acme/widget/pull/1",
        promoted_sha="ffff" * 10,
    )

    qual = version.last_qualification
    assert qual["status"] == "promoted"
    assert "error" not in qual, (
        "a promoted version must not report a promotion error — that is what "
        "made `error` unreadable as a state signal"
    )
    assert "detail" not in qual
    # History is PRESERVED, not destroyed: the failure is still readable, just
    # not under a key that reads as current state.
    assert qual["prior_promotion_failure"]["error"] == "required_check_failed"
    assert (
        qual["prior_promotion_failure"]["detail"] == "backend-tests (shard 3) failed"
    )
    assert qual["promotion_failure_count"] == 2, "the circuit-breaker count survives"


@pytest.mark.asyncio
async def test_a_clean_promotion_records_no_prior_failure_key(db_session, mocker):
    """No fabricated key on the ordinary path that never failed."""
    from app.release_promotion import _record_promotion_success

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    mocker.patch(
        "app.release_promotion._relock_version_for_promotion_write",
        new=AsyncMock(return_value=version),
    )
    mocker.patch(
        "app.release_promotion._record_qualification_verdict",
        new=AsyncMock(return_value=None),
    )

    await _record_promotion_success(
        db_session,
        version,
        _CANDIDATE,
        {"candidate_sha": _CANDIDATE, "confidence": 0.95},
        "https://github.com/acme/widget/pull/2",
        promoted_sha="eeee" * 10,
    )

    assert "prior_promotion_failure" not in version.last_qualification
    assert "error" not in version.last_qualification


# ===========================================================================
# DEFECT 2 (1) — next_expected_trigger names the trigger that is ARMED
# ===========================================================================


def _explain(**overrides):
    # dict(...) keyword form, matching test_qualification_triggers.py's own
    # _explain helper -- the kwargs are passed straight through.
    kwargs = dict(  # noqa: C408
        phase="stabilize",
        status=None,
        error_code=None,
        qualification_attempts=0,
        error_redrive_count=0,
        is_candidate_fully_met=True,
        requalification_ready=None,
        candidate_sha=None,
        automain_sha=None,
        automain_ci_state=None,
        blocker=None,
        in_flight=False,
        promotion_check_wait=False,
        last_attempt=None,
        knob="on",
        evaluated_at="2026-08-25T00:00:00+00:00",
        automain_head_quiet_seconds=0.0,
    )
    kwargs.update(overrides)
    return explain_qualification_trigger(**kwargs)


@pytest.mark.parametrize(
    ("overrides", "expected_trigger", "expected_owner"),
    [
        # A sweep-owned trigger: the sweep really does execute this inline.
        (
            {"qualification_attempts": 0},
            "coverage_complete",
            "sweep",
        ),
        # THE WEDGE.  promotion_failed + remediation resolved arms
        # ``blockers_cleared``, which the sweep DISCARDS.  Reporting
        # "sweep_retry" here was the lie.
        (
            {"status": "promotion_failed", "requalification_ready": True},
            "blockers_cleared",
            "_redrive_promotion_failed_qualification",
        ),
        (
            {"status": "deferred_unsynced"},
            "deferred_unsynced_redrive",
            "_redrive_deferred_unsynced_qualification",
        ),
        (
            {"status": "rubric_changed", "qualification_attempts": 1},
            "rubric_redrive",
            "_redrive_rubric_changed_qualification",
        ),
        (
            {"status": "error", "error_code": "sha_error", "qualification_attempts": 1},
            "error_redrive",
            "_redrive_error_qualification",
        ),
        (
            {"status": "infra_failure", "qualification_attempts": 1},
            "infra_failure_redrive",
            "_redrive_infra_failure_qualification",
        ),
        (
            {
                "status": "dry_run",
                "candidate_sha": "aaaa1111",
                "automain_sha": "bbbb2222",
            },
            "post_dry_run_advance",
            "sweep",
        ),
    ],
)
def test_armed_read_surface_names_the_real_trigger_and_its_owner(
    overrides, expected_trigger, expected_owner
):
    out = _explain(**overrides)

    assert out["auto_trigger"] == "armed", out["decision"]
    assert out["decision"]["trigger"] == expected_trigger
    # Literal pins, deliberately not ``out["decision"]["trigger"]``: an
    # assertion written against the very value under test certifies itself.
    assert out["next_expected_trigger"] == expected_trigger
    assert out["trigger_owner"] == expected_owner
    assert out["next_expected_trigger"] != "sweep_retry", (
        "the hardcoded default is what told an operator the sweep would retry a "
        "decision the sweep throws away"
    )


def test_a_genuine_sweep_retry_still_reports_sweep_retry():
    """The quiet-window hold really IS re-tried by the next sweep pass.

    Guards against over-correcting: only the ``armed`` row was wrong.
    """
    out = _explain(
        status="not_promoted",
        is_candidate_fully_met=False,
        blocker="below_threshold",
        candidate_sha="aaaa1111",
        automain_sha="bbbb2222",
        automain_ci_state="green",
        automain_head_quiet_seconds=0.0,
    )

    assert out["auto_trigger"] == "waiting"
    assert out["next_expected_trigger"] == "sweep_retry"
    assert out["trigger_owner"] is None, "nothing is armed, so nobody owns it"


def test_a_held_decision_has_no_trigger_owner():
    out = _explain(phase="build")

    assert out["auto_trigger"] == "waiting"
    assert out["trigger_owner"] is None


# ===========================================================================
# DEFECT 2 (2) — routing is DATA, every armed trigger has exactly one owner
# ===========================================================================


def _resolver_emitted_triggers() -> set[str]:
    """Every non-"none" trigger literal ``resolve_qualification_trigger`` emits.

    Read out of the RESOLVER'S OWN SOURCE by AST rather than from a list this
    module maintains: a hand-kept list is exactly the thing that goes stale when
    somebody adds a ninth trigger, which is the failure mode under test.
    """
    src = inspect.getsource(resolve_qualification_trigger)
    tree = ast.parse(inspect.cleandoc(src))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "trigger":
            continue
        if (
            isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node.value.value != "none"
        ):
            found.add(node.value.value)
    return found


def test_every_trigger_the_resolver_emits_has_exactly_one_owner():
    """The guard the wedge needed and did not have.

    ``blockers_cleared`` was armed by the resolver, discarded by the sweep as
    "owned by its dedicated re-driver", and had no re-driver.  Nothing failed;
    the version simply never progressed.  A trigger in NEITHER ownership table
    — or in BOTH — is a permanent silent latch, so it fails here instead.
    """
    emitted = _resolver_emitted_triggers()
    assert emitted, "AST scan found no trigger literals — the scan itself broke"

    unowned = sorted(
        t
        for t in emitted
        if t not in SWEEP_OWNED_TRIGGERS and t not in REDRIVE_TRIGGER_OWNERS
    )
    assert not unowned, (
        f"resolver triggers with NO executor: {unowned} — the sweep will discard "
        "them every cycle and no re-driver will ever run them"
    )
    doubly_owned = sorted(SWEEP_OWNED_TRIGGERS & set(REDRIVE_TRIGGER_OWNERS))
    assert not doubly_owned, f"two executors claim: {doubly_owned}"

    # Literal pin of the eight triggers as of this change, so ADDING one is a
    # visible diff here rather than a silent extension of the tables.
    assert emitted == {
        "coverage_complete",
        "candidate_advanced",
        "post_dry_run_advance",
        "rubric_redrive",
        "error_redrive",
        "infra_failure_redrive",
        "blockers_cleared",
        "deferred_unsynced_redrive",
    }


def test_every_named_redriver_owner_actually_exists():
    """Names, resolved.  A trigger routed to a missing owner is the latch.

    The deployed build during the wedge named an owner it did not define.  A
    name in ``REDRIVE_TRIGGER_OWNERS`` is a promise; this is the check.
    """
    from app import release_qualifier_redrive

    missing = sorted(
        name
        for name in REDRIVE_TRIGGER_OWNERS.values()
        if not callable(getattr(release_qualifier_redrive, name, None))
    )
    assert not missing, f"routing names non-existent re-drivers: {missing}"


def test_qualification_trigger_owner_distinguishes_sweep_owned_from_unowned():
    """None is ambiguous by design — the caller must check the sweep set first."""
    assert qualification_trigger_owner("blockers_cleared") == (
        "_redrive_promotion_failed_qualification"
    )
    assert qualification_trigger_owner("coverage_complete") is None
    assert "coverage_complete" in SWEEP_OWNED_TRIGGERS
    assert qualification_trigger_owner("no_such_trigger") is None
    assert "no_such_trigger" not in SWEEP_OWNED_TRIGGERS
    assert qualification_trigger_owner(None) is None


def test_the_sweep_executor_gates_on_the_shared_ownership_table():
    """No second, private copy of the allowlist inside the executor.

    Two tables cannot each hold the routing: the read surface would name one
    owner while the executor consulted another.
    """
    from app import release_qualifier_sweep

    src = inspect.getsource(release_qualifier_sweep._fire_auto_triggered_qualification)
    assert "SWEEP_OWNED_TRIGGERS" in src
    assert '"post_dry_run_advance"' not in src, (
        "the executor is re-declaring the trigger set instead of importing it"
    )


# ===========================================================================
# DEFECT 2 (2) — the discard leaves a breadcrumb, and fails loudly if orphaned
# ===========================================================================


@pytest.mark.asyncio
async def test_a_routed_discard_is_written_where_an_operator_will_see_it(
    db_session,
):
    """The discard used to be LOG-ONLY.  Logs rotate; the wedge outlived them."""
    from app.release_qualifier_redrive import (
        _TRIGGER_ROUTING_DECISION_KEY,
        _record_discarded_trigger_routing,
    )

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "promotion_failed",
        "candidate_sha": _CANDIDATE,
    }
    db_session.add(version)
    await db_session.commit()

    wrote = await _record_discarded_trigger_routing(
        version.id,
        "blockers_cleared",
        session_factory=_shared_session_factory(db_session),
    )

    assert wrote is True
    crumb = version.last_qualification[_TRIGGER_ROUTING_DECISION_KEY]
    assert crumb["decision"] == "routed"
    assert crumb["reason"] == "owned_by_redriver"
    assert crumb["trigger"] == "blockers_cleared"
    assert crumb["owner"] == "_redrive_promotion_failed_qualification"
    assert crumb["at"]


@pytest.mark.asyncio
async def test_a_steady_state_discard_does_not_rewrite_the_row_every_sweep(
    db_session,
):
    """The sweep runs ~every 60s; an unchanged hold must not churn the row."""
    from app.release_qualifier_redrive import _record_discarded_trigger_routing

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"status": "promotion_failed"}
    db_session.add(version)
    await db_session.commit()

    factory = _shared_session_factory(db_session)
    first = await _record_discarded_trigger_routing(
        version.id, "blockers_cleared", session_factory=factory
    )
    second = await _record_discarded_trigger_routing(
        version.id, "blockers_cleared", session_factory=factory
    )

    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_an_unowned_trigger_is_recorded_orphaned_and_alerts(
    db_session, mocker
):
    """FAIL LOUDLY.  A trigger routed to a non-existent owner is permanent.

    Simulated the only way it can happen for real: the routing table names a
    re-driver this build does not define.
    """
    from sqlalchemy import select

    from app import release_qualifier_redrive as rqr
    from app.models import Alert, AlertSeverity

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"status": "promotion_failed"}
    db_session.add(version)
    await db_session.commit()

    mocker.patch.dict(
        "app.qualification_triggers.REDRIVE_TRIGGER_OWNERS",
        {"blockers_cleared": "_redrive_that_this_build_does_not_define"},
    )

    wrote = await rqr._record_discarded_trigger_routing(
        version.id,
        "blockers_cleared",
        session_factory=_shared_session_factory(db_session),
    )

    assert wrote is True
    crumb = version.last_qualification[rqr._TRIGGER_ROUTING_DECISION_KEY]
    assert crumb["decision"] == "orphaned"
    assert crumb["reason"] == "no_executor_owns_this_trigger"
    assert crumb["trigger"] == "blockers_cleared"

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.event_type == "release.qualification_trigger_unowned"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1, "an unowned trigger must surface, not be dropped"
    assert alerts[0].severity == AlertSeverity.ERROR
    assert "blockers_cleared" in alerts[0].message


@pytest.mark.asyncio
async def test_a_trigger_with_no_table_entry_at_all_is_also_orphaned(db_session):
    """Neither table lists it: still loud, not silently dropped."""
    from app import release_qualifier_redrive as rqr

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"status": "promotion_failed"}
    db_session.add(version)
    await db_session.commit()

    wrote = await rqr._record_discarded_trigger_routing(
        version.id,
        "some_future_trigger_nobody_wired",
        session_factory=_shared_session_factory(db_session),
    )

    assert wrote is True
    crumb = version.last_qualification[rqr._TRIGGER_ROUTING_DECISION_KEY]
    assert crumb["decision"] == "orphaned"
    assert crumb.get("owner") is None


@pytest.mark.asyncio
async def test_the_orphan_alert_is_raised_once_not_once_per_sweep_cycle(
    db_session, mocker
):
    """Alert lifecycle: a durable condition earns ONE durable Alert."""
    from sqlalchemy import select

    from app import release_qualifier_redrive as rqr
    from app.models import Alert

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"status": "promotion_failed"}
    db_session.add(version)
    await db_session.commit()

    mocker.patch.dict(
        "app.qualification_triggers.REDRIVE_TRIGGER_OWNERS",
        {"blockers_cleared": "_redrive_that_this_build_does_not_define"},
    )
    factory = _shared_session_factory(db_session)
    for _ in range(3):
        await rqr._record_discarded_trigger_routing(
            version.id, "blockers_cleared", session_factory=factory
        )

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.event_type == "release.qualification_trigger_unowned"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_the_sweep_executor_stamps_the_breadcrumb_on_a_real_discard(
    db_session, mocker
):
    """End-to-end through the executor, not just the helper it calls.

    A promotion_failed version with remediation resolved arms
    ``blockers_cleared``; the sweep discards it as re-driver-owned.  That exact
    path is the one that used to leave nothing behind.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app.release_qualifier_redrive import _TRIGGER_ROUTING_DECISION_KEY
    from app.release_qualifier_sweep import _fire_auto_triggered_qualification

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "promotion_failed",
        "error": "required_check_failed",
        "candidate_sha": _CANDIDATE,
        "promotion_failure_count": 1,
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()

    mocker.patch(
        "app.runtime_config_state.get_auto_qualification_triggers_mode",
        return_value="on",
    )
    mocker.patch(
        "app.category_verdict_ledger.is_candidate_fully_met",
        new=AsyncMock(return_value=False),
    )
    _patch_redrive_reads(mocker)
    run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )

    fired = await _fire_auto_triggered_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert fired is False
    run_qual.assert_not_awaited()
    crumb = version.last_qualification[_TRIGGER_ROUTING_DECISION_KEY]
    assert crumb["trigger"] == "blockers_cleared"
    assert crumb["decision"] == "routed"
    assert crumb["owner"] == "_redrive_promotion_failed_qualification"

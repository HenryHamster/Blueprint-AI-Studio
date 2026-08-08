# AUTO-SPLIT from test_release_qualifier.py — see refactor: split mega backend test files.
# Behavior-preserving: shared preamble lives in _release_qualifier_testhelpers.py; this file
# holds one domain slice of the original test suite.
"""Domain slice: ci_unresolved blocker — calcification + readiness guards

Split from test_release_qualifier.py."""
from _release_qualifier_testhelpers import *  # noqa: F403


@pytest.fixture(autouse=True)
def _authorized_qualification_has_a_delivery_target(monkeypatch):
    monkeypatch.setattr(rq, "resolve_project_promotion_enabled", lambda _id: True)


async def _freeze_current_qualification_contract(db_session, version):
    """Bring the legacy child fixture up to the current attempt contract.

    The shared child seeder predates the goal/rubric contract fingerprint.  The
    tests below exercise final verdict persistence, so their pending attempt
    must describe the same contract as the live version instead of being
    rejected as superseded before verdict construction.
    """
    from sqlalchemy.orm.attributes import flag_modified

    qualification = dict(version.last_qualification or {})
    version_goal = (version.goal or "").strip()
    qualification["version_goal"] = version_goal
    qualification["contract_fingerprint"] = rq._qualification_contract_fingerprint(
        version_goal,
        str(qualification.get("rubric_fingerprint") or ""),
        promotion_policy=qualification.get("promotion_policy_snapshot"),
        capability_proof_mode=str(
            qualification.get("capability_proof_mode") or "off"
        ),
        env_preflight_enabled=bool(
            qualification.get("env_preflight_enabled", False)
        ),
        requested_probes=qualification.get("requested_probes") or [],
    )
    version.last_qualification = qualification
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)


# ===========================================================================
# ci_unresolved blocker — calcification + readiness guards
# ===========================================================================


@pytest.mark.asyncio
async def test_calcification_skips_ci_unresolved_blocker(db_session):
    """ci_unresolved must NOT trigger the quality calcification alert — it is an
    infrastructure / resolution concern, not a quality regression."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_unresolved",
    }
    db_session.add(version)
    await db_session.commit()

    fired = await rq._check_version_calcification(db_session, version)
    await db_session.commit()

    assert fired is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_readiness_excludes_ci_unresolved_from_recurring_blockers(db_session):
    """A window full of ci_unresolved verdicts must NOT produce a recurring-blocker
    block in the promotion-readiness summary."""
    project, _version = await _make_project_and_version(db_session)

    # Seed enough ci_unresolved verdicts to exceed the recurring-blocker threshold
    # if they were counted.
    for _ in range(rq._READINESS_RECURRING_BLOCKER_MIN + 1):
        db_session.add(
            QualificationVerdict(
                project_id=project.id,
                release_version_id=_version.id,
                candidate_sha="abc1234",
                confidence=0.0,
                sub_scores={},
                blocker="ci_unresolved",
                would_promote=False,
                promotion_enabled=False,
                terminal_status="not_promoted",
                threshold=0.9,
            )
        )
    await db_session.commit()

    result = await rq.compute_promotion_readiness(db_session, project.id)

    # ci_unresolved must not appear in blocker_counts or recurring_blockers.
    assert "ci_unresolved" not in result["blocker_counts"]
    assert "ci_unresolved" not in result["recurring_blockers"]


@pytest.mark.asyncio
async def test_build_stall_alert_fires_for_old_build_version(db_session):
    """An open build-phase version older than _BUILD_STALL_AGE_DAYS posts one
    Alert and one release_build_stalled message."""
    from datetime import timedelta

    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="build")
    # Build phase started well beyond the healthy window; phase_changed_at is
    # None for a freshly-opened build version, so the age derives from opened_at.
    version.opened_at = rq._utcnow() - timedelta(days=rq._BUILD_STALL_AGE_DAYS + 6)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_build_stall(db_session, version)
    await db_session.commit()

    assert alerted is True
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "stabilize" in alerts[0].message

    msgs = await _messages_for(db_session, project.id, "release_build_stalled")
    assert len(msgs) == 1


@pytest.mark.asyncio
async def test_build_stall_alert_fires_for_high_merged_count(db_session, mocker):
    """An open build-phase version that has accumulated merged tasks past the
    threshold fires even when the phase age is young."""
    from app.models import Alert as _Alert

    # Keep age well below threshold so only the merged-count trigger can fire,
    # and lower the merged-count threshold so the test stays fast.
    mocker.patch.object(rq, "_BUILD_STALL_AGE_DAYS", 365)
    mocker.patch.object(rq, "_BUILD_STALL_MERGED_TASKS", 2)

    project, version = await _make_project_and_version(db_session, phase="build")
    now = rq._utcnow()
    await _make_merged_task(db_session, project.id, updated_at=now)
    await _make_merged_task(db_session, project.id, updated_at=now)
    await db_session.refresh(version)

    alerted = await rq._check_version_build_stall(db_session, version)
    await db_session.commit()

    assert alerted is True
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "build phase" in alerts[0].title


@pytest.mark.asyncio
async def test_build_stall_alert_not_fired_for_young_build_version(db_session):
    """A freshly-opened build version with no merged tasks does not alert —
    a normally-progressing build must not be nagged."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="build")
    # opened_at defaults to now; no merged tasks.
    alerted = await rq._check_version_build_stall(db_session, version)
    assert alerted is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_build_stall_alert_not_fired_for_stabilize_version(db_session):
    """A stabilize-phase version is never a build stall — wrong phase, no-op,
    even when old."""
    from datetime import timedelta

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.opened_at = rq._utcnow() - timedelta(days=rq._BUILD_STALL_AGE_DAYS + 30)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_build_stall(db_session, version)
    assert alerted is False


@pytest.mark.asyncio
async def test_build_stall_alert_deduplicated(db_session):
    """A second call for the same build-phase stall is a no-op — the alert is
    posted only once per distinct build phase."""
    from datetime import timedelta

    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="build")
    version.opened_at = rq._utcnow() - timedelta(days=rq._BUILD_STALL_AGE_DAYS + 6)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await rq._check_version_build_stall(db_session, version)
    await db_session.commit()
    second = await rq._check_version_build_stall(db_session, version)
    await db_session.commit()

    assert first is True
    assert second is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_build_stall_alert_refires_after_build_phase_restarts(db_session):
    """When the version re-enters build later (phase_changed_at re-stamped to a
    new reference), the dedup resets and a fresh stall can alert again."""
    from datetime import timedelta

    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="build")
    version.opened_at = rq._utcnow() - timedelta(days=rq._BUILD_STALL_AGE_DAYS + 16)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await rq._check_version_build_stall(db_session, version)
    await db_session.commit()
    assert first is True

    # Simulate a build → stabilize → build round-trip: phase_changed_at is
    # re-stamped to a newer (but still stalled) reference time.
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._BUILD_STALL_AGE_DAYS + 1
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    second = await rq._check_version_build_stall(db_session, version)
    await db_session.commit()
    assert second is True

    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 2


@pytest.mark.asyncio
async def test_sweep_triggers_build_stall_check(db_session):
    """The sweep runs the build-stall check — an open build version old enough
    to be stalled gets an alert."""
    from datetime import timedelta

    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="build")
    project_id = project.id
    version.opened_at = rq._utcnow() - timedelta(days=rq._BUILD_STALL_AGE_DAYS + 6)
    db_session.add(version)
    await db_session.commit()

    await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    # The sweep may also emit independent lifecycle observations (for example,
    # promotion readiness).  Keep this assertion scoped to the build-stall
    # signal rather than coupling it to the total alert count.
    build_stall_alerts = [
        alert for alert in alerts if "build phase" in alert.title
    ]
    assert len(build_stall_alerts) == 1


@pytest.mark.asyncio
async def test_stabilize_stall_alert_fires_for_old_stabilize_version(db_session):
    """An open stabilize-phase version older than _STABILIZE_STALL_AGE_DAYS posts
    one Alert and one release_stabilize_stalled message."""
    from datetime import timedelta

    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # Phase started well beyond the healthy window.
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS + 6
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_stabilize_stall(db_session, version)
    await db_session.commit()

    assert alerted is True
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "stabilize phase" in alerts[0].title
    assert "qualify" in alerts[0].message

    msgs = await _messages_for(db_session, project.id, "release_stabilize_stalled")
    assert len(msgs) == 1


@pytest.mark.asyncio
async def test_stabilize_stall_alert_fires_for_high_merged_count(db_session, mocker):
    """An open stabilize-phase version that has accumulated merged tasks past the
    threshold fires even when the phase age is young."""
    from app.models import Alert as _Alert

    mocker.patch.object(rq, "_STABILIZE_STALL_AGE_DAYS", 365)
    mocker.patch.object(rq, "_STABILIZE_STALL_MERGED_TASKS", 2)

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    now = rq._utcnow()
    await _make_merged_task(db_session, project.id, updated_at=now)
    await _make_merged_task(db_session, project.id, updated_at=now)
    await db_session.refresh(version)

    alerted = await rq._check_version_stabilize_stall(db_session, version)
    await db_session.commit()

    assert alerted is True
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "stabilize phase" in alerts[0].title


@pytest.mark.asyncio
async def test_stabilize_stall_alert_not_fired_for_young_stabilize_version(db_session):
    """A freshly-entered stabilize version with no merged tasks does not alert —
    a normally-progressing stabilize must not be nagged."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    alerted = await rq._check_version_stabilize_stall(db_session, version)
    assert alerted is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_stabilize_stall_alert_not_fired_for_build_version(db_session):
    """A build-phase version is never a stabilize stall — wrong phase, no-op."""
    from datetime import timedelta

    _project, version = await _make_project_and_version(db_session, phase="build")
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS + 30
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_stabilize_stall(db_session, version)
    assert alerted is False


@pytest.mark.asyncio
async def test_stabilize_stall_not_fired_for_dry_run_hold(db_session):
    """A version that passed qualification but is held by promotion_enabled=False
    (dry_run) must NOT trigger a stabilize-stall alert — it is healthy."""
    from datetime import timedelta

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS + 10
    )
    version.last_qualification = {"status": "dry_run", "candidate_sha": "abc123"}
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_stabilize_stall(db_session, version)
    assert alerted is False


@pytest.mark.asyncio
async def test_stabilize_stall_not_fired_when_calcification_covers_it(db_session):
    """A version with a hard-blocker not_promoted status AND enough qualification
    attempts is already covered by the calcification alert — stabilize-stall must
    not double-alert."""
    from datetime import timedelta

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS + 10
    )
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
    }
    # At or above the calcification threshold the calcification alert covers it,
    # so the stabilize-stall alert must stay silent.
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_stabilize_stall(db_session, version)
    assert alerted is False


@pytest.mark.asyncio
async def test_stabilize_stall_fires_for_low_attempt_hard_blocker_strand(db_session):
    """A version stuck not_promoted with a hard blocker but below the calcification
    threshold is invisible to the calcification alert — the stabilize-stall alert
    must fire as the backstop once the version is old enough."""
    from datetime import timedelta

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS + 5
    )
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
    }
    # Below the calcification threshold — calcification alert won't fire.
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS - 1
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_stabilize_stall(db_session, version)
    assert alerted is True


@pytest.mark.asyncio
async def test_stabilize_stall_not_fired_for_low_attempt_hard_blocker_young_version(
    db_session,
):
    """A version stuck not_promoted with a hard blocker below the calcification
    threshold but NOT yet past the age gate must NOT fire the stabilize-stall
    alert — it is too young."""
    from datetime import timedelta

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS - 2
    )
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
    }
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS - 1
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_stabilize_stall(db_session, version)
    assert alerted is False


@pytest.mark.asyncio
async def test_stabilize_stall_alert_deduplicated(db_session):
    """A second call for the same stabilize-phase stall is a no-op — the alert
    is posted only once per distinct stabilize phase."""
    from datetime import timedelta

    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS + 6
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await rq._check_version_stabilize_stall(db_session, version)
    await db_session.commit()
    second = await rq._check_version_stabilize_stall(db_session, version)
    await db_session.commit()

    assert first is True
    assert second is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_stabilize_stall_alert_refires_after_stabilize_phase_restarts(db_session):
    """When the version re-enters stabilize (phase_changed_at re-stamped), the
    dedup resets and a fresh stall can alert again."""
    from datetime import timedelta

    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS + 16
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await rq._check_version_stabilize_stall(db_session, version)
    await db_session.commit()
    assert first is True

    # Simulate a stabilize → qualify → stabilize round-trip: phase_changed_at
    # is re-stamped to a newer (but still stalled) reference time.
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS + 1
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    second = await rq._check_version_stabilize_stall(db_session, version)
    await db_session.commit()
    assert second is True

    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 2


@pytest.mark.asyncio
async def test_sweep_triggers_stabilize_stall_check(db_session):
    """The sweep runs the stabilize-stall check — an open stabilize version old
    enough to be stalled gets an alert."""
    from datetime import timedelta

    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS + 6
    )
    db_session.add(version)
    await db_session.commit()

    await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "stabilize phase" in alerts[0].title


class TestFormatQualificationForApi:
    """format_qualification_for_api returns a normalized, operator-facing dict."""

    def test_promoted_status_has_correct_outcome(self):
        qual = {"status": "promoted", "confidence": 0.95, "threshold": 0.90}
        out = rq.format_qualification_for_api(qual)
        assert out["status"] == "promoted"
        assert out["outcome"] == "Promoted"

    def test_dry_run_outcome_label(self):
        qual = {"status": "dry_run", "confidence": 0.92}
        out = rq.format_qualification_for_api(qual)
        assert out["outcome"] == "Dry-run (would promote)"

    def test_not_promoted_outcome_label(self):
        qual = {"status": "not_promoted", "confidence": 0.75}
        out = rq.format_qualification_for_api(qual)
        assert out["outcome"] == "Below threshold"

    def test_pending_outcome_label(self):
        out = rq.format_qualification_for_api({"status": "pending"})
        assert out["outcome"] == "In progress"

    def test_error_outcome_label(self):
        out = rq.format_qualification_for_api({"status": "error"})
        assert out["outcome"] == "Error"

    def test_unknown_status_falls_back_to_status_string(self):
        out = rq.format_qualification_for_api({"status": "unexpected_value"})
        assert out["outcome"] == "unexpected_value"

    def test_empty_dict_returns_none_fields(self):
        out = rq.format_qualification_for_api({})
        assert out["status"] is None
        assert out["outcome"] is None
        assert out["confidence"] is None
        assert out["threshold"] is None
        assert out["sub_scores"] is None
        assert out["blocker"] is None
        assert out["started_at"] is None
        assert out["finalized_at"] is None
        assert out["migration_safety"] is None
        assert out["audit_findings"] is None
        assert out["audit_leg_proof"] is None
        assert out["promotion_conflict"] is None

    def test_all_fields_passed_through(self):
        qual = {
            "status": "not_promoted",
            "confidence": 0.82,
            "threshold": 0.90,
            "sub_scores": {"audit": 0.85, "test_erosion": 0.80, "smoketest": 0.78},
            "blocker": "ci_not_green",
            "started_at": "2026-05-21T10:00:00",
            "finalized_at": "2026-05-21T10:30:00",
            "migration_safety": "block",
            "migration_safety_detail": "table rename pending",
            "migration_apply_safety": "warn",
            "migration_apply_safety_detail": "apply path has drift",
            "audit_findings": [{"severity": "high", "summary": "stale report"}],
            "excluded_audit_findings": [{"severity": "low", "summary": "ignored"}],
            "test_erosion_detail": {"deleted_test_files": 1},
            "smoketest_detail": {"passed": False},
            "audit_reconciliation": {
                "excluded_findings_count": 1,
                "audit_leg_proof": {
                    "state": "proven",
                    "required": True,
                    "candidate_sha": "feed" * 10,
                    "audit_task_id": "audit-1",
                    "audit_task_candidate_sha": "feed" * 10,
                    "challenge_task_id": "challenge-1",
                    "challenged_candidate_sha": "feed" * 10,
                    "binding_expected_count": 1,
                    "binding_record_count": 1,
                    "binding_complete": True,
                    "reconciliation_status": "available",
                    "reconciliation_reason": None,
                },
            },
            "promotion_pr_url": "https://example.com/pr/123",
            "promotion_conflict": True,
            "promotion_error_detail": "merge conflict",
            "goal_alignment": {
                "state": "aligned",
                "assessed_grade": 0.85,
                "biggest_gap": None,
                "reasoning_summary": "Goal delivered.",
            },
        }
        out = rq.format_qualification_for_api(qual)
        assert out["confidence"] == 0.82
        assert out["threshold"] == 0.90
        assert out["sub_scores"] == qual["sub_scores"]
        assert out["blocker"] == "ci_not_green"
        assert out["started_at"] == "2026-05-21T10:00:00"
        assert out["finalized_at"] == "2026-05-21T10:30:00"
        assert out["migration_safety"] == "block"
        assert out["migration_safety_detail"] == "table rename pending"
        assert out["migration_apply_safety"] == "warn"
        assert out["migration_apply_safety_detail"] == "apply path has drift"
        assert out["audit_findings"] == qual["audit_findings"]
        assert out["excluded_audit_findings"] == qual["excluded_audit_findings"]
        assert out["test_erosion_detail"] == {"deleted_test_files": 1}
        assert out["smoketest_detail"] == {"passed": False}
        assert out["audit_reconciliation"] == {"excluded_findings_count": 1}
        assert out["audit_leg_proof"] == qual["audit_reconciliation"]["audit_leg_proof"]
        assert out["promotion_pr_url"] == "https://example.com/pr/123"
        assert out["promotion_conflict"] is True
        assert out["promotion_error_detail"] == "merge conflict"
        assert out["goal_alignment"] == qual["goal_alignment"]

    def test_smoketest_enabled_false_is_exposed(self):
        """format_qualification_for_api surfaces smoketest_enabled=False so consumers
        know the smoketest dimension was disabled and the 0.0 sub-score is a
        placeholder, not an active release-hardening lane."""
        qual = {"status": "not_promoted", "smoketest_enabled": False}
        out = rq.format_qualification_for_api(qual)
        assert out["smoketest_enabled"] is False

    def test_smoketest_enabled_true_is_exposed(self):
        qual = {"status": "not_promoted", "smoketest_enabled": True}
        out = rq.format_qualification_for_api(qual)
        assert out["smoketest_enabled"] is True

    def test_smoketest_enabled_absent_returns_none(self):
        """Legacy qualification records without smoketest_enabled return None
        so callers can distinguish 'disabled' from 'unknown'."""
        out = rq.format_qualification_for_api({"status": "pending"})
        assert out["smoketest_enabled"] is None


@pytest.mark.asyncio
async def test_finalize_dry_run_records_verdict_row(db_session, mocker):
    """A dry-run finalize appends ONE immutable verdict-history row capturing
    the would-promote outcome, confidence, sub-scores and the promotion_enabled
    state at the time — additive history alongside last_qualification."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    await _ensure_verdict_table(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )
    await _freeze_current_qualification_contract(db_session, version)
    mocker.patch.object(
        rq, "resolve_project_promotion_enabled", return_value=False
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run"

    rows = await _verdict_rows(db_session, project.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.terminal_status == "dry_run"
    assert row.would_promote is True
    assert row.promotion_enabled is False
    assert row.release_version_id == version.id
    assert row.confidence == pytest.approx(1.0)
    assert row.blocker is None
    assert row.sub_scores is not None and "audit" in row.sub_scores


@pytest.mark.asyncio
async def test_finalize_not_promoted_records_blocker_verdict_row(db_session):
    """A blocked (not-promoted) finalize records a would_promote=False verdict
    row carrying the hard-blocker code."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": ["testing/test_deleted.py"],
                "assertion_delta": 0,
                "test_count_delta": -3,
            },
            "deleted_test_file_classifications": [
                {
                    "path": "testing/test_deleted.py",
                    "classification": "genuine_erosion",
                    "rationale": "Subject module present and unchanged; deletion is genuine erosion.",
                }
            ],
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )
    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"

    rows = await _verdict_rows(db_session, project.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.terminal_status == "not_promoted"
    assert row.would_promote is False
    assert row.blocker == "test_file_deleted"


@pytest.mark.asyncio
async def test_verdict_recording_failure_does_not_propagate(db_session, mocker):
    """Recording is best-effort: a failure constructing/staging the verdict row
    must NEVER break or roll back the qualification — the dry-run still finalizes
    and last_qualification is written, just with no history row."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    await _ensure_verdict_table(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )
    await _freeze_current_qualification_contract(db_session, version)
    mocker.patch.object(
        rq, "resolve_project_promotion_enabled", return_value=False
    )

    # Make the verdict-row construction blow up — recording must swallow it.
    mocker.patch(
        "app.release_promotion.QualificationVerdict",
        side_effect=RuntimeError("boom — cannot build verdict row"),
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # The qualification itself is unaffected: it still reports dry_run and
    # last_qualification persists.
    assert result["status"] == "dry_run"
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"
    assert version.status == "open"

    # ...but no history row was written (recording failed and was swallowed).
    rows = await _verdict_rows(db_session, project.id)
    assert rows == []


@pytest.mark.asyncio
async def test_finalize_records_ci_resolution_provenance_in_verdict(db_session, mocker):
    """CI resolution provenance (state, resolved_sha, detail) is persisted in
    the QualificationVerdict sub_scores as ci_resolution for both green and
    non-green paths."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    await _ensure_verdict_table(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )
    await _freeze_current_qualification_contract(db_session, version)
    mocker.patch.object(
        rq, "resolve_project_promotion_enabled", return_value=False
    )

    # Mock CI resolution to return explicit provenance: green via PR head.
    async def mock_resolve_ci(*args, **kwargs):
        return (
            True,
            {
                "state": rq._CI_GREEN,
                "resolved_sha": "0123456789abcdef0123456789abcdef01234567",
                "detail": "PR head check-runs passed",
            },
        )

    mocker.patch.object(rq, "_resolve_ci_at_finalize", side_effect=mock_resolve_ci)

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run"

    rows = await _verdict_rows(db_session, project.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.sub_scores is not None
    assert "ci_resolution" in row.sub_scores
    ci_res = row.sub_scores["ci_resolution"]
    assert ci_res["state"] == rq._CI_GREEN
    assert ci_res["resolved_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert ci_res["detail"] == "PR head check-runs passed"


@pytest.mark.asyncio
async def test_finalize_records_ci_resolution_on_not_promoted(db_session, mocker):
    """CI resolution provenance is also recorded when a verdict is not promoted,
    including degenerate states like pending."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.5,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )
    # Mock CI resolution to return pending state (no checks found).
    async def mock_resolve_ci(*args, **kwargs):
        return (
            False,
            {
                "state": rq._CI_PENDING,
                "resolved_sha": None,
                "detail": "no check-runs on candidate and no PR head with CI",
            },
        )

    mocker.patch.object(rq, "_resolve_ci_at_finalize", side_effect=mock_resolve_ci)

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"

    rows = await _verdict_rows(db_session, project.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.sub_scores is not None
    assert "ci_resolution" in row.sub_scores
    ci_res = row.sub_scores["ci_resolution"]
    assert ci_res["state"] == rq._CI_PENDING
    assert ci_res["resolved_sha"] is None
    assert ci_res["detail"] == "no check-runs on candidate and no PR head with CI"


@pytest.mark.asyncio
async def test_readiness_empty_history_not_ready(db_session):
    """No verdict history → not ready, with an explanatory reason."""
    project, _version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["ready"] is False
    assert out["verdicts_evaluated"] == 0
    assert "nothing to evaluate" in out["reason"]
    assert out["confidence"]["mean"] is None


@pytest.mark.asyncio
async def test_readiness_insufficient_dry_runs_not_ready(db_session):
    """Fewer than the minimum evaluated verdicts → not ready even when every
    one would have promoted."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # Two clean would-promote verdicts — below _READINESS_MIN_VERDICTS (5).
    for i in range(2):
        await _seed_verdict(
            db_session, project.id, version.id, seq=i, would_promote=True
        )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["ready"] is False
    assert out["verdicts_evaluated"] == 2
    assert out["consecutive_clean_passes"] == 2
    assert "dry-run verdict" in out["reason"]


@pytest.mark.asyncio
async def test_readiness_healthy_streak_ready(db_session):
    """A long consecutive-clean streak of high-confidence would-promote verdicts
    above all thresholds → ready."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # Six clean, high-confidence, low-variance would-promote verdicts.
    confidences = [0.95, 0.96, 0.94, 0.97, 0.95, 0.96]
    for i, conf in enumerate(confidences):
        await _seed_verdict(
            db_session,
            project.id,
            version.id,
            seq=i,
            would_promote=True,
            confidence=conf,
        )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["ready"] is True, out["reason"]
    assert out["verdicts_evaluated"] == 6
    assert out["would_promote_count"] == 6
    assert out["would_promote_rate"] == pytest.approx(1.0)
    assert out["consecutive_clean_passes"] == 6
    assert out["recurring_blockers"] == {}
    assert out["confidence"]["stable"] is True


@pytest.mark.asyncio
async def test_readiness_is_framed_as_self_approval_not_trustworthy(db_session):
    """The ready signal must NOT claim the qualifier is 'trustworthy' — there is
    no recorded post-promotion ground truth, so it is a self-approval rate only.
    The verdict reason and the structured caveat fields must say so."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    for i in range(6):
        await _seed_verdict(
            db_session,
            project.id,
            version.id,
            seq=i,
            would_promote=True,
            confidence=0.95,
        )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["ready"] is True, out["reason"]
    # No unvalidated "trustworthy" claim; framed honestly as self-approval.
    assert "trustworthy" not in out["reason"].lower()
    assert "self-approval" in out["reason"].lower()
    # Structured caveat fields are present so every consumer states it honestly.
    assert out["ground_truth_validated"] is False
    assert "self-approval only" in out["caveat"]


@pytest.mark.asyncio
async def test_readiness_empty_carries_ground_truth_caveat(db_session):
    """Even the no-verdicts (not-ready) summary carries the ground-truth caveat
    fields so the UI/strategic payloads never imply a validated signal."""
    project, _version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["verdicts_evaluated"] == 0
    assert out["ground_truth_validated"] is False
    assert "self-approval only" in out["caveat"]


@pytest.mark.asyncio
async def test_readiness_declared_unsatisfied_omits_retired_acceptance_keys(
    db_session,
):
    """Readiness no longer emits advisory acceptance aggregate/caveat keys."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    await _seed_clean_streak(db_session, project, version)
    await _seed_landed_acceptance_task(db_session, project.id, satisfied=False)

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["ready"] is True, out["reason"]
    _assert_retired_acceptance_keys_absent(out)
    assert "not fully proven" not in out["reason"]


@pytest.mark.asyncio
async def test_readiness_undeclared_only_is_unaffected(db_session):
    """Undeclared merged work no longer adds retired acceptance keys."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    await _seed_clean_streak(db_session, project, version)
    await _seed_landed_acceptance_task(db_session, project.id, satisfied=None)
    await _seed_landed_acceptance_task(db_session, project.id, satisfied=None)

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["ready"] is True, out["reason"]
    _assert_retired_acceptance_keys_absent(out)
    assert "not fully proven" not in out["reason"]


@pytest.mark.asyncio
async def test_readiness_proven_only_has_no_caveat(db_session):
    """Proven-only merged work no longer adds retired acceptance keys."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    await _seed_clean_streak(db_session, project, version)
    await _seed_landed_acceptance_task(db_session, project.id, satisfied=True)

    out = await rq.compute_promotion_readiness(db_session, project.id)

    _assert_retired_acceptance_keys_absent(out)
    assert "not fully proven" not in out["reason"]


@pytest.mark.asyncio
async def test_readiness_satisfied_unproven_omits_retired_acceptance_keys(
    db_session,
):
    """Soft satisfied evidence no longer emits acceptance caveats."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    await _seed_clean_streak(db_session, project, version)
    await _seed_landed_acceptance_task(
        db_session,
        project.id,
        satisfied=True,
        proven=False,
        kind="observable_behavior",
    )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["ready"] is True, out["reason"]
    _assert_retired_acceptance_keys_absent(out)
    assert "satisfied-but-not-machine-proven" not in out["reason"]


@pytest.mark.asyncio
async def test_readiness_empty_history_omits_retired_acceptance_keys(db_session):
    """The no-verdict path omits retired acceptance aggregate/caveat keys."""
    project, _version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["verdicts_evaluated"] == 0
    _assert_retired_acceptance_keys_absent(out)


@pytest.mark.asyncio
async def test_readiness_does_not_query_acceptance_aggregate(
    db_session, mocker
):
    """Readiness no longer depends on the retired acceptance aggregate query."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    await _seed_clean_streak(db_session, project, version)
    await _seed_landed_acceptance_task(db_session, project.id, satisfied=False)

    mocker.patch(
        "app.release_version.compute_version_acceptance_aggregate",
        side_effect=RuntimeError("boom"),
    )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["ready"] is True, out["reason"]
    _assert_retired_acceptance_keys_absent(out)


@pytest.mark.asyncio
async def test_validated_precision_none_when_no_resolved_outcomes(db_session):
    """Only dry-run verdicts (no resolved promoted outcomes) → validated_precision
    null, sample 0, ground_truth_validated False; self-approval framing intact."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    for i in range(6):
        await _seed_verdict(
            db_session, project.id, version.id, seq=i, would_promote=True
        )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["validated_precision"] is None
    assert out["resolved_outcome_sample"] == 0
    assert out["serving_confirmed_count"] == 0
    assert out["deploy_failed_count"] == 0
    assert out["ground_truth_validated"] is False
    assert "self-approval only" in out["caveat"]
    # Self-approval rate is still reported independently.
    assert out["would_promote_rate"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_validated_precision_from_resolved_outcomes(db_session):
    """A mixed serving_confirmed/deploy_failed sample at/above the minimum computes
    the correct precision and flips ground_truth_validated True; unconfirmed/NULL
    rows are ignored. The self-approval rate is still reported independently."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # Self-approval window (dry-run regime) — separate signal.
    for i in range(6):
        await _seed_verdict(
            db_session, project.id, version.id, seq=i, would_promote=True
        )
    # Resolved promoted outcomes: 4 serving_confirmed, 1 deploy_failed → 0.80.
    outcomes = (
        ["serving_confirmed"] * 4 + ["deploy_failed"] + ["unconfirmed", None]  # ignored
    )
    for j, outcome in enumerate(outcomes):
        await _seed_promoted_verdict(
            db_session,
            project.id,
            version.id,
            seq=100 + j,
            post_promotion_outcome=outcome,
        )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["serving_confirmed_count"] == 4
    assert out["deploy_failed_count"] == 1
    assert out["resolved_outcome_sample"] == 5
    assert out["validated_precision"] == pytest.approx(0.8)
    assert out["ground_truth_validated"] is True
    assert "validated precision" in out["caveat"]
    # The self-approval rate is augmented, never replaced.
    assert out["would_promote_rate"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_validated_precision_below_min_keeps_unvalidated(db_session):
    """A resolved sample below the minimum reports the precision but keeps
    ground_truth_validated False and the self-approval-only caveat."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # Only 2 resolved outcomes — below _READINESS_MIN_RESOLVED_OUTCOMES (5).
    await _seed_promoted_verdict(
        db_session,
        project.id,
        version.id,
        seq=100,
        post_promotion_outcome="serving_confirmed",
    )
    await _seed_promoted_verdict(
        db_session,
        project.id,
        version.id,
        seq=101,
        post_promotion_outcome="deploy_failed",
    )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["resolved_outcome_sample"] == 2
    assert out["validated_precision"] == pytest.approx(0.5)
    assert out["ground_truth_validated"] is False
    assert "self-approval only" in out["caveat"]


@pytest.mark.asyncio
async def test_validated_precision_self_corrects_on_deploy_failed_recovery(db_session):
    """(c) When a deploy_failed verdict is later recovered (its
    post_promotion_outcome flips deploy_failed -> serving_confirmed because the
    promoted SHA finally served), the readiness validated_precision sample
    self-corrects on the next compute — the false-negative deploy_failed no longer
    depresses precision. release_readiness needs no logic change: it derives the
    precision directly from QualificationVerdict.post_promotion_outcome."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # 4 serving_confirmed + 1 deploy_failed → precision 0.80 over 5 resolved.
    confirmed_rows = [
        await _seed_promoted_verdict(
            db_session,
            project.id,
            version.id,
            seq=100 + i,
            post_promotion_outcome="serving_confirmed",
        )
        for i in range(4)
    ]
    failed_row = await _seed_promoted_verdict(
        db_session,
        project.id,
        version.id,
        seq=200,
        post_promotion_outcome="deploy_failed",
    )

    before = await rq.compute_promotion_readiness(db_session, project.id)
    assert before["serving_confirmed_count"] == 4
    assert before["deploy_failed_count"] == 1
    assert before["validated_precision"] == pytest.approx(0.8)

    # Recovery flips the verdict outcome exactly as
    # _recover_serving_confirmed does (deploy_failed -> serving_confirmed).
    failed_row.post_promotion_outcome = "serving_confirmed"
    db_session.add(failed_row)
    await db_session.commit()

    after = await rq.compute_promotion_readiness(db_session, project.id)
    assert after["serving_confirmed_count"] == 5
    assert after["deploy_failed_count"] == 0
    assert after["validated_precision"] == pytest.approx(1.0)
    # The false-negative no longer depresses the precision sample.
    assert after["validated_precision"] > before["validated_precision"]
    assert len(confirmed_rows) == 4


@pytest.mark.asyncio
async def test_readiness_recurring_blocker_not_ready(db_session):
    """A recurring hard blocker in the history blocks readiness even when the
    most-recent streak, evaluated count and would-promote rate all clear their
    thresholds."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # Two older not-promoted verdicts on the SAME blocker (recurring), then a
    # run of eight recent clean would-promote passes.  evaluated=10,
    # streak=8, rate=0.80 (boundary, passes) — only the recurring blocker fails.
    await _seed_verdict(
        db_session,
        project.id,
        version.id,
        seq=0,
        would_promote=False,
        blocker="audit_missing",
        confidence=0.4,
    )
    await _seed_verdict(
        db_session,
        project.id,
        version.id,
        seq=1,
        would_promote=False,
        blocker="audit_missing",
        confidence=0.4,
    )
    for i in range(8):
        await _seed_verdict(
            db_session,
            project.id,
            version.id,
            seq=2 + i,
            would_promote=True,
            confidence=0.95,
        )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["ready"] is False
    assert out["verdicts_evaluated"] == 10
    assert out["consecutive_clean_passes"] == 8
    assert out["would_promote_rate"] == pytest.approx(0.8)
    assert out["recurring_blockers"] == {"audit_missing": 2}
    assert "recurring hard blocker" in out["reason"]


@pytest.mark.asyncio
async def test_readiness_excludes_promotion_enabled_verdicts(db_session):
    """Verdicts recorded after promotion was enabled are post-decision and must
    NOT count toward the dry-run readiness evidence."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # Five clean dry-run-regime verdicts (would qualify as ready)...
    for i in range(5):
        await _seed_verdict(
            db_session, project.id, version.id, seq=i, would_promote=True
        )
    # ...plus a promotion-enabled verdict that must be ignored.
    await _seed_verdict(
        db_session,
        project.id,
        version.id,
        seq=99,
        would_promote=True,
        promotion_enabled=True,
    )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["verdicts_evaluated"] == 5
    assert out["ready"] is True, out["reason"]
    # Pre-go-live (promotion disabled): this IS current readiness.
    assert out["regime"] == "pre_go_live_calibration"
    assert out["is_current_readiness"] is True


@pytest.mark.asyncio
async def test_readiness_post_go_live_is_historical_not_current(db_session):
    """Once promotion is effectively enabled, the dry-run window stops receiving
    verdicts and is frozen pre-go-live history. The summary must relabel itself as
    a historical calibration window (regime='live', is_current_readiness=False)
    and NOT present a stale 'ready' streak as current readiness — otherwise the
    panel freezes on aging dry-runs while claiming to be current."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # A healthy dry-run streak that WOULD read as ready pre-go-live...
    for i in range(6):
        await _seed_verdict(
            db_session,
            project.id,
            version.id,
            seq=i,
            would_promote=True,
            confidence=0.95,
        )

    # ...but promotion is now live for this project.
    out = await rq.compute_promotion_readiness(
        db_session, project.id, promotion_enabled=True
    )

    assert out["regime"] == "live"
    assert out["is_current_readiness"] is False
    assert out["promotion_enabled"] is True
    # The stale dry-run streak must NOT surface as a live 'ready' signal.
    assert out["ready"] is False
    assert "pre-go-live" in out["reason"]
    # The underlying dry-run evidence is still carried (now labeled historical).
    assert out["verdicts_evaluated"] == 6


@pytest.mark.asyncio
async def test_readiness_post_go_live_empty_window_is_historical(db_session):
    """The 'collapsed to no verdicts' post-go-live state is also relabeled as
    historical rather than presenting a bare not-ready current-readiness number."""
    project, _version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)

    out = await rq.compute_promotion_readiness(
        db_session, project.id, promotion_enabled=True
    )

    assert out["verdicts_evaluated"] == 0
    assert out["regime"] == "live"
    assert out["is_current_readiness"] is False
    assert out["ready"] is False


@pytest.mark.asyncio
async def test_readiness_resolves_promotion_enabled_from_runtime_config(db_session):
    """When promotion_enabled is not passed, the regime is resolved from the
    effective per-project promotion gate (so the panel endpoint, which omits the
    arg, gets the live relabeling automatically once promotion is enabled)."""
    from app import shared_state

    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    for i in range(6):
        await _seed_verdict(
            db_session,
            project.id,
            version.id,
            seq=i,
            would_promote=True,
            confidence=0.95,
        )

    shared_state.runtime_config["promotion_enabled"] = True
    try:
        out = await rq.compute_promotion_readiness(db_session, project.id)
    finally:
        shared_state.runtime_config["promotion_enabled"] = False

    assert out["regime"] == "live"
    assert out["is_current_readiness"] is False
    assert out["ready"] is False


@pytest.mark.asyncio
async def test_readiness_surfaces_infra_failure_count_and_reason(db_session):
    """When the only recent qualification is an infra_failure, the readiness
    summary surfaces infra_failure_count >= 1, consecutive_infra_errors >= 1,
    and a reason that distinguishes 'broken pipeline' from 'never ran'.

    This is the anti-silent-collapse guard: operators must be able to tell the
    difference between 'qualifier has not run' and 'qualifier is thrashing on
    infrastructure'."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)

    # Stamp the version's last_qualification as an infra_failure with a streak.
    from sqlalchemy.orm.attributes import flag_modified

    version.last_qualification = {
        "status": "infra_failure",
        "consecutive_infra_errors": 3,
        "reason": "audit_report_missing",
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()

    out = await rq.compute_promotion_readiness(db_session, project.id)

    # No QualificationVerdict rows were written — infra failures don't write them.
    assert out["verdicts_evaluated"] == 0
    assert out["infra_failure_count"] == 1
    assert out["consecutive_infra_errors"] == 3
    assert "infrastructure failures" in out["reason"]
    assert out["ready"] is False


@pytest.mark.asyncio
async def test_readiness_consecutive_infra_errors_from_pending_prior_streak(db_session):
    """When the latest last_qualification is 'pending' (a new run just started
    after a streak of infra failures), consecutive_infra_errors is sourced from
    prior_infra_streak on the pending record rather than from the previous infra
    failure record."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)

    from sqlalchemy.orm.attributes import flag_modified

    version.last_qualification = {
        "status": "pending",
        "prior_infra_streak": 2,
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["consecutive_infra_errors"] == 2
    # infra_failure_count counts only infra_failure status rows, not pending.
    assert out["infra_failure_count"] == 0


# ===========================================================================
# _check_version_promotion_readiness — once-per-transition notification
# ===========================================================================


@pytest.mark.asyncio
async def test_check_promotion_readiness_skips_non_open_version(db_session):
    """A promoted (or otherwise closed) version is skipped immediately — no
    message posted, no state change."""
    _project, version = await _make_project_and_version(db_session)
    version.status = "promoted"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq._check_version_promotion_readiness(db_session, version)

    assert result is False
    assert version.promotion_ready_notified is False


@pytest.mark.asyncio
async def test_check_promotion_readiness_skips_when_promotion_already_enabled(
    db_session,
):
    """When promotion_enabled is True, the operator has already acted — no
    notification is needed and the function returns False."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    _project, version = await _make_project_and_version(db_session)

    result = await rq._check_version_promotion_readiness(db_session, version)

    assert result is False
    assert version.promotion_ready_notified is False


@pytest.mark.asyncio
async def test_check_promotion_readiness_fires_on_first_ready_transition(db_session):
    """On the first not-ready→ready transition the function posts a
    promotion_readiness_reached message, sets promotion_ready_notified=True,
    and returns True."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    for i in range(5):
        await _seed_verdict(
            db_session, project.id, version.id, seq=i, would_promote=True
        )
    assert version.promotion_ready_notified is False

    result = await rq._check_version_promotion_readiness(db_session, version)
    await db_session.commit()

    assert result is True
    await db_session.refresh(version)
    assert version.promotion_ready_notified is True

    msgs = await _messages_for(
        db_session, project.id, event_type="promotion_readiness_reached"
    )
    assert len(msgs) == 1
    assert (msgs[0].context or {}).get("version_number") == version.version_number


@pytest.mark.asyncio
async def test_check_promotion_readiness_deduplicates_within_ready_window(db_session):
    """Once promotion_ready_notified is True, a second call while readiness still
    holds returns False without posting a duplicate message."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    for i in range(5):
        await _seed_verdict(
            db_session, project.id, version.id, seq=i, would_promote=True
        )

    version.promotion_ready_notified = True
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq._check_version_promotion_readiness(db_session, version)

    assert result is False
    msgs = await _messages_for(
        db_session, project.id, event_type="promotion_readiness_reached"
    )
    assert len(msgs) == 0


@pytest.mark.asyncio
async def test_check_promotion_readiness_resets_dedup_flag_on_lapse(db_session):
    """When readiness lapses (no verdicts → not-ready) and promotion_ready_notified
    was True, the flag is reset to False so the next ready transition can notify."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    _project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # No verdicts → not ready.
    version.promotion_ready_notified = True
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # The reset branch returns True so the sweep — which commits only on a True
    # return — persists the reset. Mirror that caller contract here: commit only
    # because the function signalled it changed state.
    result = await rq._check_version_promotion_readiness(db_session, version)

    assert result is True
    await db_session.commit()
    await db_session.refresh(version)
    assert version.promotion_ready_notified is False


@pytest.mark.asyncio
async def test_check_promotion_readiness_messaging_failure_is_nonfatal(
    db_session, mocker
):
    """A failure in post_version_promotion_readiness_message is swallowed — the
    function returns False without raising and leaves promotion_ready_notified
    unset so a later cycle can retry."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    for i in range(5):
        await _seed_verdict(
            db_session, project.id, version.id, seq=i, would_promote=True
        )

    mocker.patch(
        "app.release_qualifier.post_version_promotion_readiness_message",
        new=AsyncMock(side_effect=RuntimeError("messaging down")),
    )

    result = await rq._check_version_promotion_readiness(db_session, version)

    assert result is False
    assert version.promotion_ready_notified is False


# ===========================================================================
# Connected promotion lifecycle integration tests
# ===========================================================================
# The unit tests above verify individual helpers in isolation.  These two
# integration tests drive the FULL connected path — _finalize_qualification
# → promote → v(N+1) opens — so that regressions in the wiring (not just the
# helpers) are caught together.


@pytest.mark.asyncio
async def test_finalize_omits_version_acceptance_aggregate(
    db_session, mocker
):
    """Finalize no longer writes retired acceptance_aggregate payloads."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )
    await _freeze_current_qualification_contract(db_session, version)
    mocker.patch.object(
        rq, "resolve_project_promotion_enabled", return_value=False
    )
    await _seed_landed_acceptance_task(db_session, project.id, satisfied=False)

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run"
    assert "acceptance_aggregate" not in result
    await db_session.refresh(version)
    assert "acceptance_aggregate" not in version.last_qualification

    rows = (
        (
            await db_session.execute(
                select(QualificationVerdict).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert "acceptance_aggregate" not in (rows[0].sub_scores or {})


@pytest.mark.asyncio
async def test_connected_success_promotion_lifecycle_opens_next_version_null_goal(
    db_session, mocker
):
    """Success branch: _finalize_qualification with a confident composite
    promotes, records v1 as promoted, opens v2 with base_sha = the real merge
    commit AND goal=None (no longer auto-copied from current_focus).

    The strategist will author the goal in its first cycle after version-open
    via the new_version_goal output field.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)

    focus = "Stabilise the qualifier loop and ship the promotion gate."
    project.problem_model = {"intent": {"current_focus": focus}}
    db_session.add(project)
    await db_session.commit()

    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 8,
                "test_count_delta": 3,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "smoke-a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    branch_tip_sha = "merged01" * 5
    receipt_sha = "receipt0" * 5
    mocker.patch(
        "app.github.get_branch_sha", new=_branch_sha_mock(main=branch_tip_sha)
    )
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 301, "html_url": "http://pr/301"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )
    # Promotion probes staged-direction activation in its own production
    # session. SQLite's StaticPool has only one connection, so route that
    # nested best-effort session through the test-owned session (the same
    # fixture contract used by the other connected promotion tests).
    mocker.patch(
        "app.database.AsyncSessionLocal",
        new=_shared_session_factory(db_session),
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # The connected finalize → promote chain produced a promoted verdict.
    assert result["status"] == "promoted"
    assert result["promoted_sha"] == receipt_sha
    assert result["next_version_number"] == 2

    # v1 is closed as promoted.
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == receipt_sha

    # v2 was opened with the real merge base_sha; goal starts as None.
    next_version = await rv.current_open_version(db_session, project.id)
    assert next_version is not None
    assert next_version.version_number == 2
    assert next_version.phase == "build"
    assert next_version.base_sha == receipt_sha
    # goal is None — strategist authors it in the first cycle, not at version-open.
    assert next_version.goal is None


@pytest.mark.asyncio
async def test_connected_conflict_promotion_lifecycle(db_session, mocker):
    """Conflict branch: _finalize_qualification with a confident composite but a
    conflicting GitHub merge records promotion_failed with conflict=True, keeps
    v1 open in stabilize, emits the release_promotion_failed operator message,
    and triggers the post-conflict main->automain reconciliation via
    _maybe_reconcile_after_promotion_conflict.

    This is the operator's ONLY proactive signal that promotion is stuck on a
    conflict — and the reconciliation is the self-healing trigger.  Neither was
    covered by any previous test in the connected form.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)

    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 4,
                "test_count_delta": 1,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "smoke-a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 302, "html_url": "http://pr/302"}),
    )
    # GitHub returns a merge conflict — the promotion cannot land.
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={
                "merged": False,
                "conflict": True,
                "message": "merge conflict",
            }
        ),
    )
    recon_mock = mocker.patch(
        "app.hotfix_backmerge.reconcile_main_into_automain",
        new=AsyncMock(return_value={"status": "merged"}),
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # The finalize chain saw a conflict → promotion_failed with conflict flag.
    assert result["status"] == "promotion_failed"
    assert result.get("conflict") is True

    # v1 stays open — a failed promotion never closes the version.
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"

    # No v2 was opened — promotion did not succeed.
    next_version = await rv.current_open_version(db_session, project.id)
    assert next_version is None or next_version.version_number == 1

    # The operator received a release_promotion_failed message.
    msgs = await _messages_for(db_session, project.id, "release_promotion_failed")
    assert len(msgs) == 1
    assert msgs[0].context["error"] == "merge_blocked"

    # Verify the post-conflict reconciliation wiring: _maybe_reconcile_after_
    # promotion_conflict fires when result has status=promotion_failed + conflict=True.
    # In production this is triggered by _finalize_in_dedicated_session after
    # its session commits; here we call it directly to assert the wiring is in
    # place and that the reconciler is invoked exactly once for a conflict result.
    await rq._maybe_reconcile_after_promotion_conflict(
        version.id,
        result,
        session_factory=_shared_session_factory(db_session),
    )
    recon_mock.assert_awaited_once()


# ===========================================================================
# CI-unresolved stall alerting — _check_version_ci_unresolved_stall
# ===========================================================================


@pytest.mark.asyncio
async def test_ci_unresolved_stall_alert_fires_after_threshold(db_session):
    """_check_version_ci_unresolved_stall posts an Alert and a
    release_ci_unresolved_stall message after _MIN_CALCIFICATION_ATTEMPTS
    ci_unresolved verdicts."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_unresolved",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_ci_unresolved_stall(db_session, version)
    await db_session.commit()

    assert alerted is True
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "CI state unresolvable" in alerts[0].title
    assert "CI infrastructure" in alerts[0].message

    msgs = await _messages_for(db_session, project.id, "release_ci_unresolved_stall")
    assert len(msgs) == 1
    assert version.calcification_alerted_blocker == "ci_unresolved"


@pytest.mark.asyncio
async def test_ci_unresolved_stall_dedup_suppresses_repeat(db_session):
    """A second sweep call with the same ci_unresolved stall is a no-op —
    the alert fires only once per distinct stall."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_unresolved",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await rq._check_version_ci_unresolved_stall(db_session, version)
    await db_session.commit()
    second = await rq._check_version_ci_unresolved_stall(db_session, version)
    await db_session.commit()

    assert first is True
    assert second is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_ci_unresolved_stall_does_not_fire_below_threshold(db_session):
    """Fewer than _MIN_CALCIFICATION_ATTEMPTS ci_unresolved verdicts do not
    trigger the stall alert — the unresolved state may be transient."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS - 1
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_unresolved",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_ci_unresolved_stall(db_session, version)

    assert alerted is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_ci_unresolved_stall_distinct_from_calcification_alert(db_session):
    """ci_not_green triggers calcification but NOT ci_unresolved stall, and
    ci_unresolved triggers ci_unresolved stall but NOT calcification."""
    # ci_not_green: calcification fires, ci_unresolved stall does not.
    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    calc_fired = await rq._check_version_calcification(db_session, version)
    ci_stall_fired = await rq._check_version_ci_unresolved_stall(db_session, version)
    await db_session.commit()

    assert calc_fired is True
    assert ci_stall_fired is False

    # ci_unresolved: ci_unresolved stall fires, calcification does not.
    _project2, version2 = await _make_project_and_version(db_session, phase="stabilize")
    version2.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version2.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_unresolved",
    }
    db_session.add(version2)
    await db_session.commit()
    await db_session.refresh(version2)

    calc_fired2 = await rq._check_version_calcification(db_session, version2)
    ci_stall_fired2 = await rq._check_version_ci_unresolved_stall(db_session, version2)
    await db_session.commit()

    assert calc_fired2 is False
    assert ci_stall_fired2 is True


# ===========================================================================
# managed promotion-pending: timeout bypass
# ===========================================================================


@pytest.mark.asyncio
async def test_sweep_skips_qualification_timeout_for_managed_promotion(db_session):
    """Audit H2 exemption: the qualification-timeout backstop must NOT
    misclassify a version that is already awaiting managed promotion checks.
    Qualification completed and passed; the pending clock runs on the
    promotion PR's CI, not on qualifier children."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    # Stamp a very old started_at that would normally trigger the timeout,
    # but include a promotion_pending sub-state indicating qualification passed.
    old_started = (
        _utcnow() - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 3600)
    ).isoformat()
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
        started_at=old_started,
    )
    # Inject the promotion_pending sub-state directly (simulating what promote()
    # writes after opening the promotion PR).
    version.last_qualification = {
        **version.last_qualification,
        "promotion_pending": {
            "pr_number": 42,
            "pr_url": "http://github.com/pr/42",
            "managed_check_wait": True,
            "enabled_at": _utcnow().isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    # Sweep did NOT force-finalize this version (count=0) — it is not a
    # hung-qualifier-child timeout; it is awaiting the promotion PR.
    assert count == 0
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert "blocker" not in version.last_qualification


@pytest.mark.asyncio
async def test_promotion_failed_candidate_on_main_reconciled(db_session, mocker):
    """A failed promotion with an exact merged-PR receipt reachable from main is
    reconciled to ``promoted`` and opens the next version (count=1)."""
    from app.models import ReleaseVersion as _RV

    project, version = await _make_project_and_version(db_session)

    candidate_sha = "reconcile" * 4 + "1234"  # 40 chars
    main_head_sha = "mainhead" * 5  # 40 chars
    receipt_sha = "receipt2" * 5  # distinct 40-character exact merge receipt
    version.last_qualification = {
        "status": "promotion_failed",
        "candidate_sha": candidate_sha,
        "error": "merge_blocked",
        "conflict": True,
        "confidence": 0.95,
        "sub_scores": {},
        "pr_url": "https://github.com/acme/widget/pull/42",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Candidate IS on main now (operator merged it out-of-band).
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 42,
                "html_url": "https://github.com/acme/widget/pull/42",
                "state": "closed",
                "merged": True,
                "head": {"ref": "release-v1", "sha": candidate_sha},
                "base": {"ref": "main", "sha": "base123"},
                "merge_commit_sha": receipt_sha,
            }
        ),
    )
    # PR has no migration files — post-merge check returns "ok" immediately.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=main_head_sha),
    )
    # Keep the follow-on serving barrier pending: this test owns promotion
    # reconciliation only, and must not make a live check-runs request or count
    # a second, independent serving-confirmation transition in the same sweep.
    mocker.patch(
        "app.github_checks.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "in_progress", "conclusion": None}
        ),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.database.AsyncSessionLocal",
        new=_shared_session_factory(db_session),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # Sweep counted this as a finalization.
    assert count == 1

    # Version is now promoted.
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"
    assert version.last_qualification["already_landed"] is True
    assert version.promoted_sha == receipt_sha

    # Next version was opened.
    next_versions = (
        (
            await db_session.execute(
                select(_RV).where(
                    _RV.project_id == project.id,
                    _RV.version_number == 2,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(next_versions) == 1
    assert next_versions[0].status == "open"
    assert next_versions[0].base_sha == receipt_sha


@pytest.mark.asyncio
async def test_promotion_failed_candidate_not_on_main_skipped(db_session, mocker):
    """An open version with last_qualification.status='promotion_failed' whose
    candidate_sha is NOT on main is left untouched by the sweep (count=0)."""
    _project, version = await _make_project_and_version(db_session)

    candidate_sha = "notlanded" * 4 + "abcd"  # 40 chars
    version.last_qualification = {
        "status": "promotion_failed",
        "candidate_sha": candidate_sha,
        "error": "merge_blocked",
        "conflict": True,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # Candidate is NOT on main yet.
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=False),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0

    # Version untouched — still open with promotion_failed record.
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "promotion_failed"


@pytest.mark.asyncio
async def test_promotion_failed_reconcile_idempotent(db_session, mocker):
    """Calling sweep twice on a version that was reconciled on the first call
    does NOT double-promote or open a duplicate next version (count=0 on the
    second call)."""
    from app.models import ReleaseVersion as _RV

    project, version = await _make_project_and_version(db_session)

    candidate_sha = "idemptest" * 4 + "12ab"  # 40 chars
    # Exact target equality is the no-PR promotion receipt.  A later moving
    # target tip that merely contains the candidate is intentionally not enough
    # to attribute a promotion.
    main_head_sha = candidate_sha
    version.last_qualification = {
        "status": "promotion_failed",
        "candidate_sha": candidate_sha,
        "error": "merge_blocked",
        "pr_url": "https://github.com/acme/widget/pull/42",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "closed", "merged": False}),
    )
    # PR has no migration files — post-merge check returns "ok" immediately.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=main_head_sha),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value=None))

    first_count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()
    assert first_count == 1

    # Second sweep — version is now promoted (not open), reconcile is a no-op.
    second_count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()
    assert second_count == 0

    # Exactly one next version was opened.
    next_versions = (
        (
            await db_session.execute(
                select(_RV).where(
                    _RV.project_id == project.id,
                    _RV.version_number == 2,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(next_versions) == 1


@pytest.mark.asyncio
async def test_promotion_pending_timeout_force_fails_stranded_version(
    db_session, mocker
):
    """A version stranded in promotion_pending past the wall-clock cap is
    force-failed as 'promotion_pending_timeout' after GitHub authoritatively
    reports the recorded PR closed and unmerged. The terminal PR receipt lets
    cleanup debt clear, returning the version to stabilize so the normal
    backstop re-arms.
    """
    from datetime import timedelta

    from app.release_reconciliation import (
        _reconcile_if_promotion_pending_timed_out,
        _promotion_pending_timeout_seconds,
    )
    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)
    project_id = str(project.id)

    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    # enabled_at is older than the cap — the promotion has stranded.
    stranded_enabled = (
        _utcnow() - timedelta(seconds=_promotion_pending_timeout_seconds() + 3600)
    ).isoformat()
    candidate_sha = "strandpp" * 5
    version.last_qualification = {
        **version.last_qualification,
        "candidate_sha": candidate_sha,
        "promotion_pending": {
            "pr_number": 99,
            "pr_url": "https://github.com/acme/widget/pull/99",
            "managed_check_wait": True,
            "enabled_at": stranded_enabled,
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.release_reconciliation._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "acme", "widget")),
    )
    get_pr_mock = mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 99,
                "state": "closed",
                "merged": False,
                "head": {"ref": "release-v1", "sha": candidate_sha},
                "base": {"ref": "main", "sha": "base0001"},
            }
        ),
    )
    delete_branch_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )

    timed_out = await _reconcile_if_promotion_pending_timed_out(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert timed_out is True

    await db_session.refresh(version)
    # Stays open (promotion failure never touches main) and returns to stabilize.
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "promotion_pending_timeout"
    # The sub-state is CLEARED so the qualification-timeout backstop re-arms.
    assert "promotion_pending" not in version.last_qualification
    get_pr_mock.assert_awaited_once_with(
        "tok", "acme", "widget", 99, project_id=project_id
    )
    delete_branch_mock.assert_awaited_once_with(
        "tok", "acme", "widget", "release-v1"
    )


@pytest.mark.asyncio
async def test_promotion_pending_timeout_does_not_fire_before_cap(db_session):
    """A promotion_pending sub-state younger than the cap is left untouched —
    a slow-but-progressing promotion PR must not be force-failed."""
    from app.release_reconciliation import _reconcile_if_promotion_pending_timed_out
    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    version.last_qualification = {
        **version.last_qualification,
        "candidate_sha": "freshpp0" * 5,  # 40 chars
        "promotion_pending": {
            "pr_number": 100,
            "pr_url": "https://github.com/acme/widget/pull/100",
            "managed_check_wait": True,
            "enabled_at": _utcnow().isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    timed_out = await _reconcile_if_promotion_pending_timed_out(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert timed_out is False

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    assert "promotion_pending" in version.last_qualification


@pytest.mark.asyncio
async def test_promotion_pending_timeout_wired_into_sweep(db_session, mocker):
    """The wall-clock deadline is reachable through the public sweep entry point
    (sweep_stuck_qualifications), proving the wiring — not just the helper."""
    from datetime import timedelta

    from app import shared_state
    from app.release_reconciliation import _promotion_pending_timeout_seconds
    from app.time_utils import _utcnow

    # Disable promotion so the readiness check in the same sweep is a no-op.
    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)

    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    stranded_enabled = (
        _utcnow() - timedelta(seconds=_promotion_pending_timeout_seconds() + 3600)
    ).isoformat()
    version.last_qualification = {
        **version.last_qualification,
        "candidate_sha": "sweepstr" * 5,  # 40 chars
        "promotion_pending": {
            "pr_number": 101,
            "pr_url": "https://github.com/acme/widget/pull/101",
            "managed_check_wait": True,
            "enabled_at": stranded_enabled,
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # The managed waiter runs before the timeout backstop.  Model the exact PR
    # still open with checks pending so it remains non-terminal there; the
    # overdue timeout path then closes that same PR and records the wall-clock
    # failure this wiring test is named for.
    candidate_sha = "sweepstr" * 5
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 101,
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": candidate_sha},
                "base": {"ref": "main", "sha": "main-base-101"},
            }
        ),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "in_progress", "conclusion": None}),
    )
    mocker.patch("app.github.close_pull_request", new=AsyncMock(return_value=True))
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 1

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "promotion_pending_timeout"
    assert "promotion_pending" not in version.last_qualification


@pytest.mark.asyncio
async def test_promotion_pending_timeout_respects_runtime_config_override(db_session):
    """The cap is configurable via runtime_config; a positive override changes
    when the deadline fires (and a non-positive override is ignored)."""
    from app import shared_state
    from app.release_reconciliation import (
        _PROMOTION_PENDING_TIMEOUT_SECONDS_DEFAULT,
        _promotion_pending_timeout_seconds,
    )

    # Positive override is honored.
    shared_state.runtime_config["promotion_pending_timeout_seconds"] = 3600
    try:
        assert _promotion_pending_timeout_seconds() == 3600
        # Non-positive / bogus overrides fall back to the default (never disable
        # the backstop).
        shared_state.runtime_config["promotion_pending_timeout_seconds"] = 0
        assert (
            _promotion_pending_timeout_seconds()
            == _PROMOTION_PENDING_TIMEOUT_SECONDS_DEFAULT
        )
        shared_state.runtime_config["promotion_pending_timeout_seconds"] = "garbage"
        assert (
            _promotion_pending_timeout_seconds()
            == _PROMOTION_PENDING_TIMEOUT_SECONDS_DEFAULT
        )
    finally:
        shared_state.runtime_config.pop("promotion_pending_timeout_seconds", None)


@pytest.mark.asyncio
async def test_get_latest_release_audit_report_none_when_no_audit_task(db_session):
    """No release_audit task → None (a fresh project has no audit history)."""
    project, _version = await _make_project_and_version(db_session)

    result = await rq.get_latest_release_audit_report(
        db_session, project.id, candidate_sha="feed" * 10
    )

    assert result is None


@pytest.mark.asyncio
async def test_get_latest_release_audit_report_returns_parsed_report(db_session):
    """A release_audit task with a RELEASE_AUDIT_REPORT artifact → parsed
    report plus task metadata (id, status, candidate_sha, created_at)."""
    project, version = await _make_project_and_version(db_session)

    report_logs = {
        "audit_score": 0.74,
        "test_erosion": {
            "deleted_test_files": [],
            "assertion_delta": 12,
            "test_count_delta": 3,
        },
        "findings": [
            {"severity": "high", "description": "Diff is unusually large."},
        ],
    }
    audit_task = Task(
        project_id=project.id,
        title="release audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        context={
            "release_version_id": str(version.id),
            "candidate_sha": "feed" * 10,
        },
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)
    db_session.add(
        Artifact(
            task_id=audit_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps(report_logs),
        )
    )
    await db_session.commit()

    result = await rq.get_latest_release_audit_report(
        db_session, project.id, candidate_sha="feed" * 10
    )

    assert result is not None
    assert result["audit_task_id"] == str(audit_task.id)
    assert result["task_status"] == "COMPLETED"
    assert result["candidate_sha"] == "feed" * 10
    assert result["created_at"] is not None
    assert result["report"] == report_logs


@pytest.mark.asyncio
async def test_get_latest_release_audit_report_report_none_when_no_artifact(db_session):
    """A release_audit task that crashed without posting a report → the task
    metadata is returned but ``report`` is None (not an error)."""
    project, _version = await _make_project_and_version(db_session)

    audit_task = Task(
        project_id=project.id,
        title="crashed audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        status=TaskStatus.FAILED,
        context={"candidate_sha": "feed" * 10},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)

    result = await rq.get_latest_release_audit_report(
        db_session, project.id, candidate_sha="feed" * 10
    )

    assert result is not None
    assert result["audit_task_id"] == str(audit_task.id)
    assert result["task_status"] == "FAILED"
    assert result["report"] is None


@pytest.mark.asyncio
async def test_get_latest_release_audit_report_scopes_to_candidate_sha(db_session):
    """With several release_audit tasks, the latest matching candidate SHA wins."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    project, _version = await _make_project_and_version(db_session)
    base = _utcnow()
    candidate_sha = "feed" * 10

    old_task = Task(
        project_id=project.id,
        title="old audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_at=base - timedelta(hours=2),
        context={"candidate_sha": candidate_sha},
    )
    new_task = Task(
        project_id=project.id,
        title="new audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_at=base,
        context={"candidate_sha": "beef" * 10},
    )
    # A non-audit task that must be ignored even though it is the newest.
    other_task = Task(
        project_id=project.id,
        title="some smoketest",
        payloads=[{"type": "smoketest", "base_branch": "automain"}],
        status=TaskStatus.COMPLETED,
        created_at=base + timedelta(hours=1),
    )
    db_session.add_all([old_task, new_task, other_task])
    await db_session.commit()
    await db_session.refresh(old_task)
    db_session.add(
        Artifact(
            task_id=old_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps({"audit_score": 0.91}),
        )
    )
    await db_session.commit()

    result = await rq.get_latest_release_audit_report(
        db_session, project.id, candidate_sha=candidate_sha
    )

    assert result is not None
    assert result["audit_task_id"] == str(old_task.id)
    assert result["report"] == {"audit_score": 0.91}


# ---------------------------------------------------------------------------
# resolve_lifecycle_audit_evidence / _select_lifecycle_audit — version-scoped
# audit evidence
#
# The lifecycle view must only show audit evidence belonging to the open
# version's evaluated candidate.  These cover the pure selector's classification
# (current / stale / none) plus the version-scoped DB resolver.
# ---------------------------------------------------------------------------


def test_select_lifecycle_audit_matching_candidate_is_current():
    """The newest row whose candidate_sha equals the evaluated SHA → current."""
    rows = [
        {"candidate_sha": "newc" * 10},
        {"candidate_sha": "0ld0" * 10},
    ]
    sel = rq._select_lifecycle_audit(rows, "newc" * 10)
    assert sel["state"] == "current"
    assert sel["current"] is rows[0]
    assert sel["stale"] is None


def test_select_lifecycle_audit_only_prior_candidate_is_stale():
    """Audits exist but none match the evaluated SHA → newest is stale."""
    rows = [
        {"candidate_sha": "0ld0" * 10},
        {"candidate_sha": "evnoldr" * 5 + "00000"},
    ]
    sel = rq._select_lifecycle_audit(rows, "newc" * 10)
    assert sel["state"] == "stale"
    assert sel["current"] is None
    assert sel["stale"] is rows[0]


def test_select_lifecycle_audit_empty_is_none():
    """No audit rows → none, regardless of evaluated SHA."""
    sel = rq._select_lifecycle_audit([], "newc" * 10)
    assert sel == {"state": "none", "current": None, "stale": None}


def test_select_lifecycle_audit_no_evaluated_sha_is_stale():
    """Rows present but no evaluated SHA to match → newest treated as stale."""
    rows = [{"candidate_sha": "0ld0" * 10}]
    sel = rq._select_lifecycle_audit(rows, None)
    assert sel["state"] == "stale"
    assert sel["stale"] is rows[0]


@pytest.mark.asyncio
async def test_resolve_lifecycle_audit_evidence_matching_candidate(db_session):
    """An audit pinned to the open version's evaluated candidate → current."""
    project, version = await _make_project_and_version(db_session)
    candidate = "feed" * 10
    version.last_qualification = {"status": "pending", "candidate_sha": candidate}
    db_session.add(version)
    await db_session.commit()

    report = {"audit_score": 0.8, "findings": []}
    task = await _add_release_audit_task(
        db_session,
        project,
        version_id=version.id,
        candidate_sha=candidate,
        report=report,
    )

    result = await rq.resolve_lifecycle_audit_evidence(db_session, version)

    assert result["state"] == "current"
    assert result["stale_audit"] is None
    assert result["latest_audit"] is not None
    assert result["latest_audit"]["audit_task_id"] == str(task.id)
    assert result["latest_audit"]["candidate_sha"] == candidate
    assert result["latest_audit"]["report"] == report


@pytest.mark.asyncio
async def test_resolve_lifecycle_audit_evidence_stale_prior_candidate(db_session):
    """Only an audit against an OLDER candidate exists → stale provenance, no
    current report (the prior findings must not be presented as current)."""
    project, version = await _make_project_and_version(db_session)
    version.last_qualification = {"status": "pending", "candidate_sha": "newc" * 10}
    db_session.add(version)
    await db_session.commit()

    report = {"audit_score": 0.5, "findings": [{"severity": "high"}]}
    task = await _add_release_audit_task(
        db_session,
        project,
        version_id=version.id,
        candidate_sha="0ld0" * 10,
        report=report,
    )

    result = await rq.resolve_lifecycle_audit_evidence(db_session, version)

    assert result["state"] == "stale"
    assert result["latest_audit"] is None
    assert result["stale_audit"] is not None
    assert result["stale_audit"]["audit_task_id"] == str(task.id)
    assert result["stale_audit"]["candidate_sha"] == "0ld0" * 10
    assert result["stale_audit"]["report"] == report


@pytest.mark.asyncio
async def test_resolve_lifecycle_audit_evidence_excludes_other_version(db_session):
    """An audit belonging to a DIFFERENT release version is never attached —
    a fresh version with no audit of its own returns the explicit none state."""
    project, version = await _make_project_and_version(db_session)
    version.last_qualification = {"status": "pending", "candidate_sha": "feed" * 10}
    db_session.add(version)
    await db_session.commit()

    other_version_id = uuid.uuid4()
    await _add_release_audit_task(
        db_session,
        project,
        version_id=other_version_id,
        candidate_sha="feed" * 10,
        report={"audit_score": 0.9},
    )

    result = await rq.resolve_lifecycle_audit_evidence(db_session, version)

    assert result["state"] == "none"
    assert result["latest_audit"] is None
    assert result["stale_audit"] is None


@pytest.mark.asyncio
async def test_resolve_lifecycle_audit_evidence_none_when_no_audit(db_session):
    """No release_audit task for the version → explicit none state."""
    _project, version = await _make_project_and_version(db_session)
    version.last_qualification = {"status": "pending", "candidate_sha": "feed" * 10}
    db_session.add(version)
    await db_session.commit()

    result = await rq.resolve_lifecycle_audit_evidence(db_session, version)

    assert result == {"state": "none", "latest_audit": None, "stale_audit": None}


@pytest.mark.asyncio
async def test_resolve_lifecycle_audit_evidence_current_report_none_no_artifact(
    db_session,
):
    """A matching audit that crashed without a report → current with a null
    report body but provenance present (state stays current, not none)."""
    project, version = await _make_project_and_version(db_session)
    candidate = "feed" * 10
    version.last_qualification = {"status": "pending", "candidate_sha": candidate}
    db_session.add(version)
    await db_session.commit()

    task = await _add_release_audit_task(
        db_session,
        project,
        version_id=version.id,
        candidate_sha=candidate,
        status=TaskStatus.FAILED,
    )

    result = await rq.resolve_lifecycle_audit_evidence(db_session, version)

    assert result["state"] == "current"
    assert result["latest_audit"] is not None
    assert result["latest_audit"]["audit_task_id"] == str(task.id)
    assert result["latest_audit"]["task_status"] == "FAILED"
    assert result["latest_audit"]["report"] is None


@pytest.mark.asyncio
async def test_get_latest_release_audit_report_none_when_candidate_missing(db_session):
    """No open-version candidate SHA means there is no comparable audit scope."""
    project, _version = await _make_project_and_version(db_session)

    audit_task = Task(
        project_id=project.id,
        title="candidate-scoped audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        context={"candidate_sha": "feed" * 10},
    )
    db_session.add(audit_task)
    await db_session.commit()

    result = await rq.get_latest_release_audit_report(db_session, project.id)

    assert result is None


@pytest.mark.asyncio
async def test_run_qualification_uses_project_integration_branch(db_session, mocker):
    """run_qualification must snapshot the candidate SHA from the project's
    configured integration_branch, not the global runtime_config default.

    A project with integration_branch='trunk' should call github.get_branch_sha
    with 'trunk', never with 'main' (the global default).
    """
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False

    project, version = await _make_project_and_version_custom_branches(
        db_session,
        integration_branch="trunk",
        promotion_target="release",
        phase="stabilize",
    )

    candidate = "trunk1ca" * 5  # 40-char placeholder SHA

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )

    branch_sha_calls: list[str] = []

    async def _branch_sha(token, owner, repo, branch, **kwargs):
        branch_sha_calls.append(branch)
        return candidate

    mocker.patch("app.github.get_branch_sha", new=AsyncMock(side_effect=_branch_sha))
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "success"}),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["candidate_sha"] == candidate

    # The integration_branch snapshot call must use 'trunk', not 'main'.
    assert "trunk" in branch_sha_calls, (
        f"Expected get_branch_sha call for 'trunk' but got calls: {branch_sha_calls}"
    )
    assert "main" not in branch_sha_calls, (
        "Got unexpected call for 'main' — should use per-project integration_branch"
    )


@pytest.mark.asyncio
async def test_legacy_smoketest_flags_cannot_resurrect_qualifier_vm_smoke(db_session, mocker):
    """Legacy global and project flags cannot resurrect qualifier VM smoke.

    The ordinary qualification still resolves the project's integration branch
    and spawns its active evidence children, but it never creates a smoketest
    child or records one as required.
    """
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = True

    project, version = await _make_project_and_version_custom_branches(
        db_session,
        integration_branch="trunk",
        promotion_target="release",
        phase="stabilize",
    )
    project.build_config = {
        **(project.build_config or {}),
        "smoketest_enabled": True,
    }
    db_session.add(project)
    await db_session.commit()

    candidate = "smktrunk" * 5  # 40-char SHA

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "success"}),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert result["status"] == "spawned"
    assert result["smoketest_enabled"] is False
    assert result["smoketest_task_id"] is None
    assert version.last_qualification["smoketest_enabled"] is False
    assert version.last_qualification["smoketest_task_id"] is None

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert all((task.first_payload or {}).get("type") != "smoketest" for task in tasks)


@pytest.mark.asyncio
async def test_candidate_already_on_main_uses_project_promotion_target(
    db_session, mocker
):
    """_candidate_already_on_main must check ancestry against the project's
    configured promotion_target, not the global runtime_config default.

    A project with promotion_target='release' should call github.is_ancestor_of
    with 'release' as the descendant branch, not 'main'.
    """
    project, version = await _make_project_and_version_custom_branches(
        db_session,
        integration_branch="trunk",
        promotion_target="release",
        phase="build",
    )

    candidate = "chkancst" * 5  # 40-char SHA

    ancestor_calls: list[str] = []

    async def _is_ancestor(token, owner, repo, sha, branch, **kwargs):
        ancestor_calls.append(branch)
        return True

    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(side_effect=_is_ancestor))

    result = await rq._candidate_already_on_main(
        "gh-token",
        "acme",
        "custom",
        candidate,
        version,
        project=project,
    )

    assert result is True
    assert ancestor_calls == ["release"], (
        f"Expected is_ancestor_of called with 'release' but got: {ancestor_calls}"
    )


@pytest.mark.asyncio
async def test_reconcile_promotion_failed_uses_project_promotion_target(
    db_session, mocker
):
    """When a promotion_failed version is reconciled (candidate landed on main
    out-of-band), promoted_sha is resolved from the project's promotion_target
    branch, not the global runtime_config default.

    A project with promotion_target='release' should call get_branch_sha with
    'release' to resolve the promoted merge commit SHA.
    """
    _project, version = await _make_project_and_version_custom_branches(
        db_session,
        integration_branch="trunk",
        promotion_target="release",
        phase="build",
    )
    mocker.patch(
        "app.database.AsyncSessionLocal",
        new=_shared_session_factory(db_session),
    )

    candidate_sha = "rcnclfld" * 5  # 40 chars
    # The operator fast-forwarded the exact candidate onto the configured
    # promotion target.  Exact equality is the no-PR promotion receipt.
    release_head_sha = candidate_sha

    version.last_qualification = {
        "status": "promotion_failed",
        "candidate_sha": candidate_sha,
        "pr_url": "https://github.com/acme/custom/pull/42",
        "error": "merge_blocked",
        "confidence": 0.9,
        "sub_scores": {},
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # Candidate is now on the promotion_target branch (operator merged it).
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "closed", "merged": False}),
    )
    # PR has no migration files — post-merge check returns "ok" immediately.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )

    branch_sha_calls: list[str] = []

    async def _branch_sha(token, owner, repo, branch, **kwargs):
        branch_sha_calls.append(branch)
        return release_head_sha

    mocker.patch("app.github.get_branch_sha", new=AsyncMock(side_effect=_branch_sha))
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value=None))
    # Keep the independent post-promotion serving barrier pending so this sweep
    # count isolates the promotion-failed reconciliation transition.
    mocker.patch(
        "app.github_checks.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "in_progress", "conclusion": None}),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 1

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == release_head_sha

    # promoted_sha must be resolved from the per-project promotion_target.
    assert "release" in branch_sha_calls, (
        f"Expected get_branch_sha call for 'release' but got: {branch_sha_calls}"
    )
    assert "main" not in branch_sha_calls, (
        "Got unexpected 'main' branch call — should use per-project promotion_target"
    )


# ===========================================================================
# Non-self qualify -> promote -> reconcile tail — the promotion MERGE step and
# the pending-auto-merge reconciliation path over a custom branch pair.
# ===========================================================================


@pytest.mark.asyncio
async def test_finalize_promote_merges_over_custom_branch_pair(db_session, mocker):
    """End-to-end qualify -> promote -> merge for a non-self project with a
    custom integration/promotion branch pair.

    The other custom-branch tests cover the candidate snapshot and the ancestry
    check, but the actual promotion MERGE step (open + merge the
    release-branch -> promotion-target PR) was only ever exercised with the
    hardcoded automain/main fixture.  This drives a confident candidate through
    ``_finalize_qualification`` -> ``promote`` and asserts the PR is opened from
    the release branch into the project's configured ``promotion_target``
    (resolved via the per-project branch resolver, NOT a hardcoded 'main'),
    merged with ``merge_method='merge'``, and the version is promoted.
    """
    from app import shared_state
    from app.branch_resolution import get_project_promotion_target

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version_custom_branches(
        db_session,
        integration_branch="develop",
        promotion_target="staging",
        phase="stabilize",
    )
    mocker.patch(
        "app.database.AsyncSessionLocal",
        new=_shared_session_factory(db_session),
    )
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 10,
                "test_count_delta": 4,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    target_tip_sha = "stgmerge" * 5  # unrelated moving promotion-target HEAD
    receipt_sha = "receipt0" * 5

    async def _branch_sha(token, owner, repo, branch, **kwargs):
        return target_tip_sha

    mocker.patch("app.github.get_branch_sha", new=AsyncMock(side_effect=_branch_sha))
    # is_ancestor_of=False on the idempotency pre-check: the candidate is NOT yet
    # on the promotion target, so promote() proceeds to open + merge a real PR.
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 42, "html_url": "http://pr/42"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "promoted"
    # The PR head is the release branch; its base is the per-project promotion
    # target — resolved through the branch resolver, never a hardcoded 'main'.
    assert create_mock.call_args.kwargs["head"] == "release-v1"
    assert (
        create_mock.call_args.kwargs["base"]
        == get_project_promotion_target(project)
        == "staging"
    )
    merge_mock.assert_awaited_once()
    assert merge_mock.call_args.kwargs["merge_method"] == "merge"

    await db_session.refresh(version)
    assert version.status == "promoted"
    # Promotion identity is the exact PR receipt, not whichever later commit
    # happens to be the target branch tip when reconciliation runs.
    assert version.promoted_sha == receipt_sha


@pytest.mark.asyncio
async def test_promote_inert_when_integration_equals_promotion_target(
    db_session, mocker
):
    """Single-branch external repo (integration_branch == promotion_target).

    This is the common default when onboarding an external repo.  promote() has
    an explicit single-branch guard (mirroring the back-merge guard in
    hotfix_backmerge.reconcile_main_into_automain): when the integration branch
    IS the promotion target it records an inert, already-landed success and
    returns BEFORE any GitHub I/O — without opening a release branch, opening a
    PR, merging, or even running the idempotency ancestry check.  Documents that
    the promote tail is genuinely inert for this configuration via that explicit
    guard, not merely by accident of the ancestry short-circuit.
    """
    _project, version = await _make_project_and_version_custom_branches(
        db_session,
        integration_branch="main",
        promotion_target="main",
        phase="stabilize",
    )
    candidate = "snglbrnc" * 5  # already on the shared single branch

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # integration == promotion: the explicit single-branch guard fires first, so
    # the ancestry check must never be reached.  Track is_ancestor_of calls to
    # prove the guard short-circuits ahead of any GitHub I/O.
    ancestor_calls: list[str] = []

    async def _is_ancestor(token, owner, repo, sha, branch, **kwargs):
        ancestor_calls.append(branch)
        return True

    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(side_effect=_is_ancestor))
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.create_tag", new=AsyncMock(return_value={"ref": "refs/tags/v1"})
    )
    branch_mock = mocker.patch("app.github.create_branch", new=_create_branch_mock())
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 1, "html_url": "http://pr/1"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.95},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()

    # Inert promote tail: recorded as an already-landed success with NO branch,
    # PR, or merge against the shared single branch.
    assert result["status"] == "promoted"
    branch_mock.assert_not_called()
    create_mock.assert_not_called()
    merge_mock.assert_not_called()
    # The explicit single-branch guard returns before any GitHub I/O, so the
    # idempotency ancestry check is never reached.
    assert ancestor_calls == []

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"
    assert version.last_qualification.get("already_landed") is True
    # The guard marks the LTS model inactive for this single-branch repo.
    assert version.last_qualification.get("lts_inactive") is True


def test_reaper_sweep_seam_reexported_from_release_qualifier():
    """The reaper-sweep seam lives in app.release_qualifier_sweep but its public
    symbols must stay importable/patchable as ``app.release_qualifier.<name>``.

    Guards the split-and-re-export contract: the reaper imports
    ``sweep_stuck_qualifications`` via release_qualifier, and tests patch the
    constants / helpers as ``rq.*``. A future ``ruff --fix`` dropping a re-export
    pin would silently break those callers — this test catches it.
    """
    from app import release_qualifier_sweep as rqs

    for name in (
        "sweep_stuck_qualifications",
        "_force_finalize_timed_out_qualification",
        "_force_finalize_sweep_crash_loop",
        "_parse_started_at",
        "_MAX_QUALIFICATION_DURATION_SECONDS",
        "_MAX_SWEEP_CRASH_STREAK",
    ):
        assert hasattr(rq, name), f"release_qualifier must re-export {name}"
        assert getattr(rq, name) is getattr(rqs, name), (
            f"{name} re-export must be identical to the canonical impl"
        )


# ===========================================================================
# Post-promotion serving confirmation — promotion-time stamping.
# ===========================================================================


@pytest.mark.asyncio
async def test_record_promotion_success_stamps_unconfirmed_for_self_repo(db_session):
    """A self-repo promotion is PROVISIONAL: serving_status='unconfirmed' and a
    bounded deploy_confirm_deadline are stamped at promotion time."""
    from app.release_promotion import (
        _DEPLOY_CONFIRM_WINDOW_SECONDS,
        _record_promotion_success,
    )
    from app.time_utils import _utcnow

    project = Project(
        name="Self-Repo",
        user_id="sr-user",
        github_owner="acme",
        github_repo="self",
        integration_branch="automain",
        promotion_target="main",
        is_self_repo=True,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    before = _utcnow()
    result = await _record_promotion_success(
        db_session,
        version,
        candidate_sha="c" * 40,
        base_record={"confidence": 0.9, "status": "promoted"},
        pr_url="http://pr/1",
        promoted_sha="p" * 40,
        lock_released=True,
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.serving_status == "unconfirmed"
    assert version.served_sha is None
    assert version.deploy_confirm_deadline is not None
    elapsed = (version.deploy_confirm_deadline - before).total_seconds()
    # Deadline lands roughly one window from now (allow generous slack).
    assert 0 < elapsed <= _DEPLOY_CONFIRM_WINDOW_SECONDS + 120


@pytest.mark.asyncio
async def test_record_promotion_success_leaves_serving_null_for_non_self_repo(
    db_session,
):
    """A credential-less project (no GitHub repo) leaves serving_status NULL (NA)
    — NO provider is observable for it (not even the merge baseline), so the
    promotion is never armed and the sweep never strands it waiting on a signal
    it cannot read. Arming is now driven by per-provider observability, not the
    is_self_repo flag."""
    from app.release_promotion import _record_promotion_success

    project = Project(
        name="No-Repo",
        user_id="or-user",
        github_owner=None,
        github_repo=None,
        integration_branch="automain",
        promotion_target="main",
        is_self_repo=False,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    result = await _record_promotion_success(
        db_session,
        version,
        candidate_sha="c" * 40,
        base_record={"confidence": 0.9, "status": "promoted"},
        pr_url="http://pr/1",
        promoted_sha="p" * 40,
        lock_released=True,
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.serving_status is None
    assert version.deploy_confirm_deadline is None


@pytest.mark.asyncio
async def test_sweep_skips_non_self_repo_promotions_without_artifact_rows(
    db_session, mocker
):
    """Non-self-repo promotions stay out of the serving-confirmation sweep when
    they have NULL serving scalars and no artifact registry participation."""
    from app.time_utils import _utcnow

    project = Project(
        name="Other-Repo",
        user_id="or-user-2",
        github_owner="acme",
        github_repo="other-2",
        integration_branch="automain",
        promotion_target="main",
        is_self_repo=False,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    version.status = "promoted"
    version.promoted_at = _utcnow()
    version.promoted_sha = "p" * 40
    version.last_qualification = {"status": "promoted", "promoted_sha": "p" * 40}
    db_session.add(version)
    await db_session.commit()

    confirm = mocker.patch(
        "app.release_qualifier_sweep._confirm_serving_or_fail",
        new=AsyncMock(return_value=False),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    assert count == 0
    confirm.assert_not_called()
    await db_session.refresh(version)
    assert version.serving_status is None
    assert version.deploy_confirm_deadline is None


@pytest.mark.asyncio
async def test_blocked_attempt_persists_evidence_on_verdict_row(db_session):
    """A not_promoted (blocked) attempt mirrors migration-safety + findings onto
    the immutable verdict row, and that evidence survives a later
    re-qualification that overwrites last_qualification."""
    from app import release_promotion as rp

    _project, version = await _make_project_and_version(db_session)

    base_record = {
        "candidate_sha": "abc123",
        "confidence": 0.42,
        "threshold": 0.75,
        "blocker": "migration_unsafe",
        "status": "not_promoted",
        "migration_safety": "block",
        "migration_safety_detail": "candidate renumber-skip leaves table uncreated",
        "migration_apply_safety": "block",
        "migration_apply_safety_detail": "chain does not apply on prod schema",
        "audit_findings": [{"id": "f1", "severity": "high"}],
        "excluded_audit_findings": [{"id": "f2", "reason": "stale"}],
        "test_erosion_detail": {"deleted_test_files": ["t.py"]},
        "smoketest_detail": {"passed": False},
        "audit_reconciliation": {"reconciled": False},
        "completion_debt": {"total_open": 5, "items": []},
        "debt_enforce_confidence_preview": 0.31,
        "debt_enforce_weight_preview": 0.15,
    }
    version.last_qualification = dict(base_record)
    db_session.add(version)

    await rp._record_qualification_verdict(
        db_session,
        version,
        base_record,
        terminal_status="not_promoted",
        would_promote=False,
        promotion_enabled=False,
    )
    await db_session.commit()

    rows = await _load_only_verdict(db_session, version)
    assert len(rows) == 1
    row = rows[0]
    assert row.terminal_status == "not_promoted"
    assert row.migration_safety == "block"
    assert row.migration_safety_detail == (
        "candidate renumber-skip leaves table uncreated"
    )
    assert row.migration_apply_safety == "block"
    assert row.migration_apply_safety_detail == "chain does not apply on prod schema"
    assert row.audit_findings == [{"id": "f1", "severity": "high"}]
    assert row.excluded_audit_findings == [{"id": "f2", "reason": "stale"}]
    assert row.test_erosion_detail == {"deleted_test_files": ["t.py"]}
    assert row.smoketest_detail == {"passed": False}
    assert row.audit_reconciliation == {"reconciled": False}
    assert row.sub_scores["completion_debt"] == {"total_open": 5, "items": []}
    assert row.sub_scores["debt_enforce_confidence_preview"] == 0.31
    assert row.sub_scores["debt_enforce_weight_preview"] == 0.15
    # Promotion-attempt columns stay NULL on a non-promotion attempt.
    assert row.promotion_pr_url is None
    assert row.promotion_conflict is None
    assert row.promotion_error_detail is None

    # A subsequent re-qualification overwrites last_qualification with a NEW
    # (e.g. now-passing) evidence set — the first verdict row must keep its own.
    next_record = {
        "candidate_sha": "def456",
        "confidence": 0.91,
        "threshold": 0.75,
        "status": "not_promoted",
        "migration_safety": "pass",
        "migration_safety_detail": "clean",
    }
    version.last_qualification = dict(next_record)
    db_session.add(version)
    await rp._record_qualification_verdict(
        db_session,
        version,
        next_record,
        terminal_status="not_promoted",
        would_promote=True,
        promotion_enabled=False,
    )
    await db_session.commit()

    rows = await _load_only_verdict(db_session, version)
    assert len(rows) == 2
    first, second = rows
    # The original blocked attempt's evidence is untouched by the overwrite.
    assert first.migration_safety == "block"
    assert first.migration_safety_detail == (
        "candidate renumber-skip leaves table uncreated"
    )
    assert first.audit_findings == [{"id": "f1", "severity": "high"}]
    # The new attempt carries its own (different) evidence.
    assert second.migration_safety == "pass"
    assert second.migration_safety_detail == "clean"

    from app import release_readiness as rr

    api_view = rr.format_verdict_for_api(first)
    assert api_view["completion_debt"] == {"total_open": 5, "items": []}
    assert api_view["debt_enforce_confidence_preview"] == 0.31
    assert api_view["debt_enforce_weight_preview"] == 0.15


@pytest.mark.asyncio
async def test_promotion_failed_attempt_persists_promotion_context(db_session):
    """A promotion_failed attempt mirrors the merge PR url / conflict / error
    context onto the immutable verdict row alongside migration-safety."""
    from app import release_promotion as rp

    _project, version = await _make_project_and_version(db_session)

    base_record = {
        "candidate_sha": "abc123",
        "confidence": 0.88,
        "threshold": 0.75,
        "status": "promotion_failed",
        "migration_safety": "pass",
        "migration_safety_detail": "clean",
        "audit_findings": [],
        # Promotion-failure context folded in by _record_promotion_failure.
        "pr_url": "https://github.com/acme/widget/pull/42",
        "conflict": True,
        "error": "merge_conflict",
        "detail": "automain → main had conflicts",
    }
    version.last_qualification = dict(base_record)
    db_session.add(version)

    await rp._record_qualification_verdict(
        db_session,
        version,
        base_record,
        terminal_status="promotion_failed",
        would_promote=True,
        promotion_enabled=True,
    )
    await db_session.commit()

    rows = await _load_only_verdict(db_session, version)
    assert len(rows) == 1
    row = rows[0]
    assert row.terminal_status == "promotion_failed"
    assert row.migration_safety == "pass"
    assert row.promotion_pr_url == "https://github.com/acme/widget/pull/42"
    assert row.promotion_conflict is True
    assert row.promotion_error_detail == "merge_conflict: automain → main had conflicts"


def test_format_verdict_for_api_surfaces_durable_evidence():
    """format_verdict_for_api exposes the per-attempt evidence columns so
    operators read the attempt's own migration-safety/findings, not just the
    latest last_qualification overwrite."""
    import uuid

    verdict = QualificationVerdict(
        project_id=uuid.uuid4(),
        release_version_id=uuid.uuid4(),
        candidate_sha="abc123",
        confidence=0.1,
        blocker="migration_unsafe",
        audit_missing_reason=None,
        would_promote=False,
        promotion_enabled=False,
        terminal_status="not_promoted",
        threshold=0.9,
        migration_safety="block",
        migration_safety_detail="renumber-skip leaves table uncreated",
        migration_apply_safety="block",
        migration_apply_safety_detail="diverged from prod schema",
        audit_findings=[{"severity": "high", "summary": "x"}],
        excluded_audit_findings=[{"severity": "low", "summary": "y"}],
        test_erosion_detail={"removed": 2},
        smoketest_detail={"passed": False},
        audit_reconciliation={
            "reconciled": True,
            "audit_leg_proof": {
                "state": "proven",
                "required": True,
                "candidate_sha": "abc123",
                "audit_task_id": "audit-1",
                "audit_task_candidate_sha": "abc123",
                "challenge_task_id": "challenge-1",
                "challenged_candidate_sha": "abc123",
                "binding_expected_count": 1,
                "binding_record_count": 1,
                "binding_complete": True,
                "reconciliation_status": "available",
                "reconciliation_reason": None,
            },
        },
        promotion_pr_url="https://example.com/pr/7",
        promotion_conflict=True,
        promotion_error_detail="merge conflict",
        goal_alignment={
            "state": "aligned",
            "assessed_grade": 0.9,
            "biggest_gap": None,
            "reasoning_summary": "Goal delivered.",
        },
    )

    out = rq.format_verdict_for_api(verdict)

    # Existing keys remain.
    assert out["candidate_sha"] == "abc123"
    assert out["blocker"] == "migration_unsafe"
    assert out["audit_missing_reason"] is None
    # New durable evidence keys.
    assert out["migration_safety"] == "block"
    assert out["migration_safety_detail"] == "renumber-skip leaves table uncreated"
    assert out["migration_apply_safety"] == "block"
    assert out["migration_apply_safety_detail"] == "diverged from prod schema"
    assert out["audit_findings"] == [{"severity": "high", "summary": "x"}]
    assert out["excluded_audit_findings"] == [{"severity": "low", "summary": "y"}]
    assert out["test_erosion_detail"] == {"removed": 2}
    assert out["smoketest_detail"] == {"passed": False}
    assert out["audit_reconciliation"] == {"reconciled": True}
    assert out["audit_leg_proof"]["state"] == "proven"
    assert out["promotion_pr_url"] == "https://example.com/pr/7"
    assert out["promotion_conflict"] is True
    assert out["promotion_error_detail"] == "merge conflict"
    # Goal-alignment cert surfaces from migration-0328 column.
    assert out["goal_alignment"] == {
        "state": "aligned",
        "assessed_grade": 0.9,
        "biggest_gap": None,
        "reasoning_summary": "Goal delivered.",
    }
    # Legacy rows must return None for optional certs, not raise.
    verdict_legacy = QualificationVerdict(
        project_id=uuid.uuid4(),
        release_version_id=uuid.uuid4(),
        candidate_sha="legacy",
        confidence=0.9,
        blocker=None,
        audit_missing_reason=None,
        would_promote=True,
        promotion_enabled=True,
        terminal_status="promoted",
        threshold=0.8,
    )
    out_legacy = rq.format_verdict_for_api(verdict_legacy)
    assert out_legacy["goal_alignment"] is None


@pytest.mark.asyncio
async def test_qualification_verdict_persists_blind_merge_grade(db_session):
    """The blind merge-grade certificate is mirrored from base_record onto the
    immutable QualificationVerdict row at finalize, surfaced intact by
    format_verdict_for_api, and absent (None, not error) on legacy rows."""
    from app import release_promotion as rp

    # Stable key under which QualificationVerdict stores inline grade records.
    BLIND_MERGE_GRADE_KEY = "blind_merge_grade"

    _grade = {
        "candidate_sha": "abc123",
        "coverage_verdict": "partial",
        "biggest_gap": "no end-to-end test for the promote path",
        "model": "claude-opus-4-8",
        "pr_count": 3,
        "graded_at": "2026-06-20T00:00:00+00:00",
    }

    # --- Formatter surfaces the grade intact from the durable column. -------
    verdict = QualificationVerdict(
        project_id=uuid.uuid4(),
        release_version_id=uuid.uuid4(),
        candidate_sha="abc123",
        confidence=0.42,
        blocker=None,
        audit_missing_reason=None,
        would_promote=False,
        promotion_enabled=False,
        terminal_status="not_promoted",
        threshold=0.75,
        blind_merge_grade=_grade,
    )
    out = rq.format_verdict_for_api(verdict)
    assert out["blind_merge_grade"] == _grade

    # Legacy rows (no grade) must return None, never raise.
    verdict_legacy = QualificationVerdict(
        project_id=uuid.uuid4(),
        release_version_id=uuid.uuid4(),
        candidate_sha="legacy",
        confidence=0.9,
        blocker=None,
        audit_missing_reason=None,
        would_promote=True,
        promotion_enabled=True,
        terminal_status="promoted",
        threshold=0.8,
    )
    out_legacy = rq.format_verdict_for_api(verdict_legacy)
    assert out_legacy["blind_merge_grade"] is None

    # --- Finalize path persists the grade onto the immutable row. -----------
    # BLIND_MERGE_GRADE_KEY is the stable key release_qualifier writes into
    # base_record at finalize; _record_qualification_verdict must mirror it.
    assert BLIND_MERGE_GRADE_KEY == "blind_merge_grade"

    _project, version = await _make_project_and_version(db_session)
    base_record = {
        "candidate_sha": "abc123",
        "confidence": 0.42,
        "threshold": 0.75,
        "status": "not_promoted",
        BLIND_MERGE_GRADE_KEY: _grade,
    }
    version.last_qualification = dict(base_record)
    db_session.add(version)

    await rp._record_qualification_verdict(
        db_session,
        version,
        base_record,
        terminal_status="not_promoted",
        would_promote=False,
        promotion_enabled=False,
    )
    await db_session.commit()

    rows = await _load_only_verdict(db_session, version)
    assert len(rows) == 1
    row = rows[0]
    assert row.blind_merge_grade == _grade
    # And the formatter round-trips the persisted row.
    assert rq.format_verdict_for_api(row)["blind_merge_grade"] == _grade


@pytest.mark.asyncio
async def test_goal_alignment_propagates_to_verdict(db_session):
    """goal_alignment from the audit report propagates through finalize into
    both last_qualification (base_record) and the persisted QualificationVerdict
    row; an audit report without goal_alignment yields None without error."""
    from app import release_promotion as rp

    _goal_alignment = {
        "state": "aligned",
        "assessed_grade": 0.9,
        "biggest_gap": None,
        "reasoning_summary": "All milestones addressed.",
    }

    # --- With goal_alignment present in audit report ---------------------------
    _project, version = await _make_project_and_version(db_session)
    base_record = {
        "candidate_sha": "sha-goal",
        "confidence": 0.80,
        "threshold": 0.75,
        "status": "not_promoted",
        "goal_alignment": _goal_alignment,
    }
    version.last_qualification = dict(base_record)
    db_session.add(version)

    await rp._record_qualification_verdict(
        db_session,
        version,
        base_record,
        terminal_status="not_promoted",
        would_promote=False,
        promotion_enabled=False,
    )
    await db_session.commit()

    rows = await _load_only_verdict(db_session, version)
    assert len(rows) == 1
    assert rows[0].goal_alignment == _goal_alignment
    assert rq.format_verdict_for_api(rows[0])["goal_alignment"] == _goal_alignment

    # --- Without goal_alignment (no version goal or older report) -------------
    _project2, version2 = await _make_project_and_version(db_session)
    base_record_no_goal = {
        "candidate_sha": "sha-no-goal",
        "confidence": 0.80,
        "threshold": 0.75,
        "status": "not_promoted",
    }
    version2.last_qualification = dict(base_record_no_goal)
    db_session.add(version2)

    await rp._record_qualification_verdict(
        db_session,
        version2,
        base_record_no_goal,
        terminal_status="not_promoted",
        would_promote=False,
        promotion_enabled=False,
    )
    await db_session.commit()

    rows2 = await _load_only_verdict(db_session, version2)
    assert len(rows2) == 1
    assert rows2[0].goal_alignment is None
    assert rq.format_verdict_for_api(rows2[0])["goal_alignment"] is None


def test_format_verdict_for_api_surfaces_acceptance_window_signal_without_changing_composite():
    """format_verdict_for_api surfaces acceptance-window aggregate as top-level keys.

    The three new keys (acceptance_window_total, acceptance_window_verified,
    acceptance_window_fraction) are read from sub_scores and promoted to the
    top-level response dict so the proven-vs-delivered distinction is visible to
    operators without parsing sub_scores.  The existing composite keys (confidence,
    sub_scores, blocker, threshold) must remain unchanged — this is purely additive.

    Two sub-cases:
    - A verdict whose sub_scores already carries the acceptance aggregate (new rows).
    - A legacy verdict where sub_scores is None (acceptance keys return None without error).
    """
    import uuid

    # --- Sub-case 1: verdict with acceptance aggregate in sub_scores --
    verdict_with_agg = QualificationVerdict(
        project_id=uuid.uuid4(),
        release_version_id=uuid.uuid4(),
        candidate_sha="deadbeef",
        confidence=0.82,
        blocker=None,
        audit_missing_reason=None,
        would_promote=True,
        promotion_enabled=False,
        terminal_status="dry_run",
        threshold=0.75,
        sub_scores={
            "audit": 0.9,
            "test_erosion": 1.0,
            "smoketest": 0.8,
            "acceptance_window_total": 5,
            "acceptance_window_verified": 4,
            "acceptance_window_fraction": 0.8,
        },
    )

    out = rq.format_verdict_for_api(verdict_with_agg)

    # New acceptance-window top-level keys are present and correct.
    assert out["acceptance_window_total"] == 5
    assert out["acceptance_window_verified"] == 4
    assert out["acceptance_window_fraction"] == pytest.approx(0.8)

    # Existing composite keys are unchanged.
    assert out["confidence"] == pytest.approx(0.82)
    assert out["blocker"] is None
    assert out["threshold"] == pytest.approx(0.75)
    # sub_scores is returned intact (not mutated by the extraction).
    assert out["sub_scores"]["audit"] == pytest.approx(0.9)
    assert out["sub_scores"]["acceptance_window_total"] == 5

    # --- Sub-case 2: legacy verdict where sub_scores is None --
    verdict_legacy = QualificationVerdict(
        project_id=uuid.uuid4(),
        release_version_id=uuid.uuid4(),
        candidate_sha="legacy0",
        confidence=0.6,
        blocker="audit_missing",
        audit_missing_reason="no audit task",
        would_promote=False,
        promotion_enabled=False,
        terminal_status="not_promoted",
        threshold=0.75,
        sub_scores=None,
    )

    out_legacy = rq.format_verdict_for_api(verdict_legacy)

    # Legacy rows return None for the acceptance keys without raising.
    assert out_legacy["acceptance_window_total"] is None
    assert out_legacy["acceptance_window_verified"] is None
    assert out_legacy["acceptance_window_fraction"] is None

    # Core existing fields still present and correct.
    assert out_legacy["candidate_sha"] == "legacy0"
    assert out_legacy["confidence"] == pytest.approx(0.6)
    assert out_legacy["sub_scores"] is None


def test_format_verdict_for_api_omits_acceptance_aggregate():
    """format_verdict_for_api no longer promotes retired acceptance aggregates."""
    import uuid

    verdict = QualificationVerdict(
        project_id=uuid.uuid4(),
        release_version_id=uuid.uuid4(),
        candidate_sha="agg123",
        confidence=0.95,
        would_promote=True,
        promotion_enabled=False,
        terminal_status="dry_run",
        threshold=0.9,
        sub_scores={
            "audit": 0.95,
            "acceptance_aggregate": {
                "total": 4,
                "proven": 3,
                "satisfied_unproven": 1,
                "declared_unsatisfied": 0,
                "undeclared": 0,
                "merged_unverified_degraded": 0,
                "malformed": 0,
                "satisfied_rate": 1.0,
                "proven_rate": 0.75,
            },
        },
    )
    out = rq.format_verdict_for_api(verdict)
    assert "acceptance_aggregate" not in out
    # Raw evidence fields stay intact.
    assert out["candidate_sha"] == "agg123"
    assert out["would_promote"] is True
    assert "acceptance_aggregate" not in (out["sub_scores"] or {})


def test_format_verdict_for_api_acceptance_aggregate_absent_on_legacy():
    """Legacy rows also omit the retired acceptance_aggregate response key."""
    import uuid

    verdict = QualificationVerdict(
        project_id=uuid.uuid4(),
        release_version_id=uuid.uuid4(),
        candidate_sha="legacy1",
        confidence=0.1,
        would_promote=False,
        promotion_enabled=False,
        terminal_status="not_promoted",
    )
    out = rq.format_verdict_for_api(verdict)
    assert "acceptance_aggregate" not in out


def test_format_verdict_for_api_smoketest_enabled_false():
    """format_verdict_for_api surfaces smoketest_enabled=False from sub_scores
    so consumers know the smoketest dimension was disabled and the 0.0 placeholder
    should not be rendered as an active release-hardening lane."""
    import uuid

    verdict = QualificationVerdict(
        project_id=uuid.uuid4(),
        release_version_id=uuid.uuid4(),
        candidate_sha="abc123",
        confidence=0.88,
        sub_scores={"audit": 0.90, "test_erosion": 0.85, "smoketest": 0.0, "smoketest_enabled": False},
        would_promote=False,
        promotion_enabled=False,
        terminal_status="not_promoted",
        threshold=0.9,
    )
    out = rq.format_verdict_for_api(verdict)
    assert out["smoketest_enabled"] is False


def test_format_verdict_for_api_smoketest_enabled_true():
    import uuid

    verdict = QualificationVerdict(
        project_id=uuid.uuid4(),
        release_version_id=uuid.uuid4(),
        candidate_sha="abc123",
        confidence=0.91,
        sub_scores={"audit": 0.90, "test_erosion": 0.85, "smoketest": 0.80, "smoketest_enabled": True},
        would_promote=True,
        promotion_enabled=False,
        terminal_status="dry_run",
        threshold=0.9,
    )
    out = rq.format_verdict_for_api(verdict)
    assert out["smoketest_enabled"] is True


def test_format_verdict_for_api_smoketest_enabled_none_on_legacy():
    """Legacy verdict rows without smoketest_enabled in sub_scores return None
    so callers can distinguish 'disabled' from 'unknown'."""
    import uuid

    verdict = QualificationVerdict(
        project_id=uuid.uuid4(),
        release_version_id=uuid.uuid4(),
        candidate_sha="legacy2",
        confidence=0.85,
        sub_scores={"audit": 0.90, "test_erosion": 0.85, "smoketest": 0.75},
        would_promote=False,
        promotion_enabled=False,
        terminal_status="not_promoted",
    )
    out = rq.format_verdict_for_api(verdict)
    assert "smoketest_enabled" in out
    assert out["smoketest_enabled"] is None


def test_get_latest_qualification_audit_findings_prefers_verdict_row():
    """The findings helper prefers the durable verdict row's audit_findings and
    falls back to last_qualification only when no verdict is supplied."""
    import uuid

    version = rv.ReleaseVersion(
        project_id=uuid.uuid4(),
        version_number=1,
        phase="stabilize",
        status="open",
        last_qualification={
            "audit_findings": [{"severity": "low", "summary": "stale"}]
        },
    )
    verdict = QualificationVerdict(
        project_id=version.project_id,
        release_version_id=uuid.uuid4(),
        candidate_sha="abc123",
        confidence=0.1,
        would_promote=False,
        promotion_enabled=False,
        terminal_status="not_promoted",
        audit_findings=[{"severity": "high", "summary": "durable"}],
    )

    # Verdict row takes precedence.
    from_verdict = rq.get_latest_qualification_audit_findings(version, verdict)
    assert from_verdict == [{"severity": "high", "summary": "durable"}]

    # Fallback to last_qualification when no verdict carries findings.
    fallback = rq.get_latest_qualification_audit_findings(version)
    assert fallback == [{"severity": "low", "summary": "stale"}]


# ===========================================================================
# _compute_acceptance_window_aggregate — acceptance window counting
# ===========================================================================


@pytest.mark.asyncio
async def test_acceptance_window_aggregate_all_verified(db_session):
    """All landed tasks in the window have proven acceptance — total == verified."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project, version = await _make_project_and_version(db_session)
    for _ in range(2):
        task = Task(
            project_id=project.id,
            title="merged-task",
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            context={
                "pr_merged": True,
                CODE_ACCEPTANCE_VERDICT_KEY: {
                    "satisfied": True,
                    "proven": True,
                    "kind": "named_test",
                    "test_name": "test_feature",
                },
            },
        )
        db_session.add(task)
    await db_session.commit()

    agg = await rq._compute_acceptance_window_aggregate(db_session, version)
    assert agg["acceptance_window_total"] == 2
    assert agg["acceptance_window_verified"] == 2
    assert agg["acceptance_window_fraction"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_acceptance_window_aggregate_unsatisfied_excluded(db_session):
    """A task whose code_acceptance_verdict blocks success is excluded from the
    verified count but still counted in the total."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project, version = await _make_project_and_version(db_session)
    task_ok = Task(
        project_id=project.id,
        title="ok",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "test_name": "test_ok",
            },
        },
    )
    task_blocked = Task(
        project_id=project.id,
        title="blocked",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": False,
                "proven": False,
                "kind": "named_test",
                "reason": "declared test not in changed files",
            },
        },
    )
    db_session.add(task_ok)
    db_session.add(task_blocked)
    await db_session.commit()

    agg = await rq._compute_acceptance_window_aggregate(db_session, version)
    assert agg["acceptance_window_total"] == 2
    assert agg["acceptance_window_verified"] == 1
    assert agg["acceptance_window_fraction"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_acceptance_window_aggregate_degraded_excluded(db_session):
    """A task with unverified_degraded merge status is excluded from the verified
    count but still counted in the total."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project, version = await _make_project_and_version(db_session)
    task_ok = Task(
        project_id=project.id,
        title="ok",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "test_name": "test_ok",
            },
        },
    )
    task_degraded = Task(
        project_id=project.id,
        title="degraded",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "review_loop": {"unverified_merge_reason": "verify_setup_failed"},
        },
    )
    db_session.add(task_ok)
    db_session.add(task_degraded)
    await db_session.commit()

    agg = await rq._compute_acceptance_window_aggregate(db_session, version)
    assert agg["acceptance_window_total"] == 2
    assert agg["acceptance_window_verified"] == 1
    assert agg["acceptance_window_fraction"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_acceptance_window_aggregate_legacy_no_verdict_counted(db_session):
    """A legacy task with no code_acceptance_verdict key is NOT counted as verified.

    Undeclared (legacy) merges land in the ``undeclared`` bucket, which is
    excluded from acceptance_window_verified — the metric only counts
    explicitly declared and proven/satisfied acceptance evidence.
    """
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project, version = await _make_project_and_version(db_session)
    # No CODE_ACCEPTANCE_VERDICT_KEY in context — legacy / undeclared task.
    task_legacy = Task(
        project_id=project.id,
        title="legacy",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )
    assert CODE_ACCEPTANCE_VERDICT_KEY not in (task_legacy.context or {})
    db_session.add(task_legacy)
    await db_session.commit()

    agg = await rq._compute_acceptance_window_aggregate(db_session, version)
    assert agg["acceptance_window_total"] == 1
    assert agg["acceptance_window_verified"] == 0
    assert agg["acceptance_window_fraction"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_acceptance_window_fraction_counts_inferred_test_cochange(db_session):
    """Undeclared merges that co-changed a test + production file under genuine CI
    are counted as verified, so the window fraction is non-zero.

    This is the retirement-metric proof for the inferred_test_cochange bucket: a
    merged WRITE task with NO code_acceptance_verdict, but whose
    ``_pr_changed_files`` records both a test file and a non-test production file
    and whose merge was CI-verified (no fall-open reason), lands in the
    ``inferred_test_cochange`` bucket and is now inside the window's verified
    numerator. A sibling merge that shipped the SAME verified CI but only a
    production file (no test) stays ``undeclared`` and is excluded — proving the
    numerator discriminates genuine test co-change from bare undeclared merges,
    rather than blanket-counting every legacy merge.
    """
    from app.acceptance_gate import (
        CODE_ACCEPTANCE_VERDICT_KEY,
        classify_merged_acceptance_context,
    )

    project, version = await _make_project_and_version(db_session)

    inferred_ctx = {
        "pr_merged": True,
        "landed_on_integration": True,
        "_pr_changed_files": [
            "backend/app/release_qualifier.py",
            "backend/testing/test_release_qualifier.py",
        ],
    }
    # No verdict key: the merge is genuinely undeclared, upgraded only by inference.
    assert CODE_ACCEPTANCE_VERDICT_KEY not in inferred_ctx
    assert classify_merged_acceptance_context(inferred_ctx) == "inferred_test_cochange"

    inferred_task = Task(
        project_id=project.id,
        title="inferred-cochange",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context=inferred_ctx,
    )
    # Same verified CI, production-only diff → stays undeclared (excluded).
    bare_ctx = {
        "pr_merged": True,
        "landed_on_integration": True,
        "_pr_changed_files": ["backend/app/release_qualifier.py"],
    }
    assert classify_merged_acceptance_context(bare_ctx) == "undeclared"
    bare_task = Task(
        project_id=project.id,
        title="bare-undeclared",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context=bare_ctx,
    )
    db_session.add(inferred_task)
    db_session.add(bare_task)
    await db_session.commit()

    agg = await rq._compute_acceptance_window_aggregate(db_session, version)
    # Two write-task merges landed; only the genuine test-cochange one is verified.
    assert agg["acceptance_window_total"] == 2
    assert agg["acceptance_window_verified"] == 1
    # The retirement metric is now non-zero (was a false 0/N before the bucket).
    assert agg["acceptance_window_fraction"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_acceptance_window_fraction_nonzero_with_inferred_cochange(db_session):
    """Named retirement proof: the inferred_test_cochange bucket lifts the window
    fraction strictly off zero, and the additive breakdown is exact and total.

    Builds a synthetic write-task merge window mixing (1) a genuine CI-verified
    test+production co-change (the only verified merge), and the three distinct
    undeclared reasons: (2) a benign-unverified fall-open whose diff DID co-change
    a test + production file — proving CI-verification gates inference so it stays
    undeclared as ``not_ci_verified``; (3) a CI-verified merge with no
    ``_pr_changed_files`` context (``no_changed_files_context``); and (4) a
    CI-verified production-only diff (``no_test_prod_cochange``). Asserts:
      (a) fraction is strictly non-zero (the inferred_test_cochange contribution),
      (b) per-bucket counts sum to the write-task population and the undeclared
          reason split sums to the undeclared bucket exactly, and
      (c) all pre-existing acceptance_window_* invariants still hold.
    """
    from app.acceptance_gate import (
        ACCEPTANCE_AGGREGATE_BUCKETS,
        CODE_ACCEPTANCE_VERDICT_KEY,
        _UNDECLARED_REASON_KEYS,
        acceptance_window_sub_scores_violations,
        classify_merged_acceptance_context,
        classify_undeclared_reason,
    )

    project, version = await _make_project_and_version(db_session)

    # (1) Verified CI + test + production diff → inferred_test_cochange (verified).
    inferred_ctx = {
        "pr_merged": True,
        "landed_on_integration": True,
        "_pr_changed_files": [
            "backend/app/release_qualifier.py",
            "backend/testing/test_release_qualifier.py",
        ],
    }
    assert CODE_ACCEPTANCE_VERDICT_KEY not in inferred_ctx
    assert classify_merged_acceptance_context(inferred_ctx) == "inferred_test_cochange"

    # (2) Benign-unverified fall-open, even WITH a test+prod diff → not_ci_verified.
    not_verified_ctx = {
        "pr_merged": True,
        "landed_on_integration": True,
        "review_loop": {"unverified_merge_reason": "no_detectable_test_command"},
        "_pr_changed_files": [
            "backend/app/release_qualifier.py",
            "backend/testing/test_release_qualifier.py",
        ],
    }
    assert classify_merged_acceptance_context(not_verified_ctx) == "undeclared"
    assert classify_undeclared_reason(not_verified_ctx) == "not_ci_verified"

    # (3) Verified CI but no changed-files context recorded → no_changed_files_context.
    no_files_ctx = {"pr_merged": True, "landed_on_integration": True}
    assert classify_merged_acceptance_context(no_files_ctx) == "undeclared"
    assert classify_undeclared_reason(no_files_ctx) == "no_changed_files_context"

    # (4) Verified CI, production-only diff → no_test_prod_cochange.
    prod_only_ctx = {
        "pr_merged": True,
        "landed_on_integration": True,
        "_pr_changed_files": ["backend/app/release_qualifier.py"],
    }
    assert classify_merged_acceptance_context(prod_only_ctx) == "undeclared"
    assert classify_undeclared_reason(prod_only_ctx) == "no_test_prod_cochange"

    for title, ctx in (
        ("inferred", inferred_ctx),
        ("not-verified", not_verified_ctx),
        ("no-files", no_files_ctx),
        ("prod-only", prod_only_ctx),
    ):
        db_session.add(
            Task(
                project_id=project.id,
                title=title,
                payloads=[{"type": "create"}],
                status=TaskStatus.COMPLETED,
                context=ctx,
            )
        )
    await db_session.commit()

    agg = await rq._compute_acceptance_window_aggregate(db_session, version)

    # (a) Strictly non-zero: exactly one of four write merges is verified (0.25).
    assert agg["acceptance_window_total"] == 4
    assert agg["acceptance_window_verified"] == 1
    assert agg["acceptance_window_fraction"] == pytest.approx(0.25)
    assert agg["acceptance_window_fraction"] > 0

    # (b) Per-bucket counts are exact and partition the write-task population.
    buckets = agg["acceptance_window_buckets"]
    assert set(buckets.keys()) == set(ACCEPTANCE_AGGREGATE_BUCKETS)
    assert buckets["inferred_test_cochange"] == 1
    assert buckets["undeclared"] == 3
    assert sum(buckets.values()) == agg["acceptance_window_total"]
    # verified numerator is exactly the three verified buckets.
    assert (
        buckets["proven"]
        + buckets["satisfied_unproven"]
        + buckets["inferred_test_cochange"]
    ) == agg["acceptance_window_verified"]

    # The undeclared reason split is exact and sums to the undeclared bucket.
    reasons = agg["acceptance_window_undeclared_reasons"]
    assert set(reasons.keys()) == set(_UNDECLARED_REASON_KEYS)
    assert reasons["not_ci_verified"] == 1
    assert reasons["no_changed_files_context"] == 1
    assert reasons["no_test_prod_cochange"] == 1
    assert sum(reasons.values()) == buckets["undeclared"]

    # (c) The additive nested keys do not break the sub-scores invariant guard.
    assert acceptance_window_sub_scores_violations(agg) == []


@pytest.mark.asyncio
async def test_acceptance_window_aggregate_declared_unsatisfied_reasons(db_session):
    """The declared_unsatisfied bucket is sub-classified by the PERSISTED verdict
    reason (previously discarded at aggregation), partitioning it exactly:
    sum(unsatisfied_reasons) == buckets["declared_unsatisfied"], every canonical
    cause is reachable, and verdicts stamped without a reason land in ``unknown``
    so the split still sums (fail-safe on legacy/manual rows)."""
    from app.acceptance_gate import (
        CODE_ACCEPTANCE_VERDICT_KEY,
        _UNSATISFIED_REASON_KEYS,
        acceptance_window_sub_scores_violations,
        classify_merged_acceptance_context,
    )

    project, version = await _make_project_and_version(db_session)

    reasoned = [
        (
            "named_test",
            "declared named_test 'backend/testing/test_widget.py' is not among "
            "the PR's changed files — the claimed test was not added or modified here",
        ),
        (
            "named_test",
            "declared named_test 'backend/testing/test_widget.py' did not run "
            "green under a real merge-test verdict (the merge fell open without "
            "verification)",
        ),
        (
            "downstream_artifact",
            "declared downstream_artifact acceptance evidence but the task "
            "shipped no changed files to deliver it",
        ),
        (
            "visual",
            "declared visual acceptance evidence but no matching viewport "
            "screenshot was uploaded for component 'X' at 375px",
        ),
        (
            "coverage_delta",
            "coverage_delta acceptance evidence: achieved 55.0% coverage < 80% "
            "threshold",
        ),
        (
            "observable_behavior",
            "declared observable_behavior acceptance evidence but the task "
            "shipped no changed files to exercise it",
        ),
        ("future_kind", "unrecognized acceptance kind 'future_kind'"),
    ]
    for i, (kind, reason) in enumerate(reasoned):
        ctx = {
            "pr_merged": True,
            "landed_on_integration": True,
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": False,
                "proven": False,
                "kind": kind,
                "reason": reason,
            },
        }
        assert classify_merged_acceptance_context(ctx) == "declared_unsatisfied"
        db_session.add(
            Task(
                project_id=project.id,
                title=f"unsat-{i}",
                payloads=[{"type": "create"}],
                status=TaskStatus.COMPLETED,
                context=ctx,
            )
        )
    # One legacy declared-unsatisfied verdict stamped without a reason.
    db_session.add(
        Task(
            project_id=project.id,
            title="unsat-no-reason",
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            context={
                "pr_merged": True,
                "landed_on_integration": True,
                CODE_ACCEPTANCE_VERDICT_KEY: {
                    "satisfied": False,
                    "proven": False,
                    "kind": "named_test",
                },
            },
        )
    )
    await db_session.commit()

    agg = await rq._compute_acceptance_window_aggregate(db_session, version)

    buckets = agg["acceptance_window_buckets"]
    assert buckets["declared_unsatisfied"] == len(reasoned) + 1  # 7 reasoned + 1 unknown
    reasons = agg["acceptance_window_declared_unsatisfied_reasons"]
    assert set(reasons.keys()) == set(_UNSATISFIED_REASON_KEYS)
    assert sum(reasons.values()) == buckets["declared_unsatisfied"]

    expected_counts = {
        "named_test_missing_from_diff": 1,
        "named_test_not_run_green": 1,
        "artifact_empty_diff": 1,
        "visual_no_screenshot": 1,
        "coverage_below_threshold": 1,
        "observable_empty_diff": 1,
        "unrecognized_kind": 1,
        "unknown": 1,
    }
    for key, count in reasons.items():
        assert count == expected_counts.get(key, 0), key

    # The additive breakdown does not break the sub-scores invariant guard.
    assert acceptance_window_sub_scores_violations(agg) == []


@pytest.mark.asyncio
async def test_acceptance_window_aggregate_fraction_none_when_empty(db_session):
    """When no tasks land in the version window, fraction is None (not 0 or NaN)."""
    _project, version = await _make_project_and_version(db_session)
    agg = await rq._compute_acceptance_window_aggregate(db_session, version)
    assert agg["acceptance_window_total"] == 0
    assert agg["acceptance_window_verified"] == 0
    assert agg["acceptance_window_fraction"] is None


@pytest.mark.asyncio
async def test_acceptance_window_rederives_stale_rule_version_verdicts(db_session):
    """A verdict frozen under a RETIRED acceptance rule is re-derived in the
    qualifier tally, so an evidence-rule improvement landing mid-version re-scores
    already-merged work (design doc c50453ef, rung 3).

    Two write-task merges in the same window:

      * A task whose payload DECLARES named_test evidence and whose recorded
        verdict is stamped under ``rule_version=0`` (retired) with
        ``satisfied=False``.  The current rule judges the SAME persisted merge
        record proven — the declared test is in the diff, the merge carried a
        real merge-test verdict naming it, and the diff exercises a semantically
        grounded production seam — so the aggregate must count it in the proven
        bucket, flipping the recorded unsatisfied verdict.
      * A control task whose verdict is stamped under the CURRENT rule with
        ``satisfied=False`` and whose declaration genuinely cannot be proven
        (the declared test is absent from the diff).  A current-rule stamp is
        trusted verbatim, so it stays excluded.
    """
    from app.acceptance_gate import (
        ACCEPTANCE_RULE_VERSION,
        CODE_ACCEPTANCE_VERDICT_KEY,
        acceptance_input_fingerprint,
        effective_acceptance_verdict,
    )

    project, version = await _make_project_and_version(db_session)

    stale_task = Task(
        project_id=project.id,
        title="stale-rule-merge",
        payloads=[
            {
                "type": "create",
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
            }
        ],
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": True,
            "_pr_changed_files": [
                "backend/app/widget.py",
                "backend/testing/test_widget.py",
            ],
            "review_loop": {
                "verify_tests_acceptance_block": {
                    "present": True,
                    "ran": True,
                    "status": "pass",
                    "passed": True,
                    "selector": "backend/testing/test_widget.py",
                },
            },
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": False,
                "proven": False,
                "kind": "named_test",
                "reason": (
                    "declared test not among the PR's changed files "
                    "(retired rule)"
                ),
                "rule_version": ACCEPTANCE_RULE_VERSION - 1,
            },
        },
    )
    # The frozen stamp is stale; the CURRENT rule re-derives the SAME persisted
    # record as proven (test present + verified merge + grounded seam).
    effective_stale = effective_acceptance_verdict(stale_task)
    assert effective_stale is not stale_task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert effective_stale["satisfied"] is True
    assert effective_stale["proven"] is True
    assert effective_stale["rule_version"] == ACCEPTANCE_RULE_VERSION

    control_task = Task(
        project_id=project.id,
        title="current-rule-unsatisfied",
        payloads=[
            {
                "type": "create",
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_other.py",
                },
            }
        ],
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": True,
            "_pr_changed_files": ["backend/app/widget.py"],
            "review_loop": {},
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": False,
                "proven": False,
                "kind": "named_test",
                "reason": "declared named_test ... is not among the PR's changed files",
                "rule_version": ACCEPTANCE_RULE_VERSION,
            },
        },
    )
    control_task.context[CODE_ACCEPTANCE_VERDICT_KEY][
        "input_fingerprint"
    ] = acceptance_input_fingerprint(
        control_task.first_payload,
        control_task.context,
        control_task.project_id,
    )
    # A current-rule stamp is trusted verbatim — never re-derived.
    assert (
        effective_acceptance_verdict(control_task)
        is control_task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    )

    db_session.add(stale_task)
    db_session.add(control_task)
    await db_session.commit()

    agg = await rq._compute_acceptance_window_aggregate(db_session, version)
    assert agg["acceptance_window_total"] == 2
    assert agg["acceptance_window_verified"] == 1
    assert agg["acceptance_window_fraction"] == pytest.approx(0.5)
    # The stale-rule merge re-derived into proven; the control stayed excluded.
    assert agg["acceptance_window_buckets"]["proven"] == 1
    assert agg["acceptance_window_buckets"]["declared_unsatisfied"] == 1

    # Durability: the re-derived verdict must not stay a per-drive throwaway —
    # the aggregate writes it back onto the session-bound task so every OTHER
    # reader (task_outcomes, direction_outcomes, acceptance_criteria,
    # metrics_integrity_sweep) sees the corrected row instead of the stale one.
    stale_ctx = stale_task.context or {}
    assert stale_ctx[CODE_ACCEPTANCE_VERDICT_KEY]["rule_version"] == (
        ACCEPTANCE_RULE_VERSION
    )
    assert stale_ctx[CODE_ACCEPTANCE_VERDICT_KEY]["satisfied"] is True
    assert stale_ctx[CODE_ACCEPTANCE_VERDICT_KEY]["proven"] is True
    # A current-rule stamp is untouched — the frozen dict stays verbatim.
    assert (
        effective_acceptance_verdict(control_task)
        is control_task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    )

    # Re-derivation audit (design doc c50453ef §5): the drive reports how much
    # already-merged work was re-scored, in which direction, and under which
    # retired rule version the stale population was stamped — so score movement
    # is inspectable instead of a silently-shifting aggregate. The stale task was
    # re-derived (upgrade: unsatisfied → proven); the control was stamped under
    # the current rule and never counted.
    assert agg["acceptance_window_rederived_count"] == 1
    assert agg["acceptance_window_rederived_upgrades"] == 1
    assert agg["acceptance_window_rederived_downgrades"] == 0
    assert agg["acceptance_window_rule_version"] == ACCEPTANCE_RULE_VERSION
    assert agg["acceptance_window_stale_version_counts"] == {
        ACCEPTANCE_RULE_VERSION - 1: 1
    }
    assert agg["acceptance_window_rederived_task_ids"] == [str(stale_task.id)]

    # The durable re-derived stamp is auditable too: it is marked as a
    # re-derivation and nests the ORIGINAL land-time (unsatisfied) verdict — the
    # retired rule's judgment is never destroyed by the correction.
    stale_verdict = stale_ctx[CODE_ACCEPTANCE_VERDICT_KEY]
    assert stale_verdict["stamped_at_land_time"] is False
    assert isinstance(stale_verdict["rederived_at"], str)
    assert stale_verdict["rederived_by"] == f"qualifier:v{ACCEPTANCE_RULE_VERSION}"
    assert stale_verdict["land_time_verdict"]["satisfied"] is False
    assert (
        stale_verdict["land_time_verdict"]["rule_version"]
        == ACCEPTANCE_RULE_VERSION - 1
    )


@pytest.mark.asyncio
async def test_acceptance_window_aggregate_ignores_post_terminal_updates(
    db_session,
):
    """Post-terminal writes must not drag an older merged task into a newer window."""
    from datetime import timedelta
    from unittest.mock import patch

    project, version = await _make_project_and_version(db_session)

    window_opened_at = rq._utcnow()
    old_completed_at = window_opened_at - timedelta(days=1)
    fresh_completed_at = window_opened_at + timedelta(hours=1)
    post_terminal_message_at = window_opened_at + timedelta(days=2)

    old_task = Task(
        project_id=project.id,
        title="old-merge",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        context={},
        created_at=old_completed_at,
        updated_at=old_completed_at,
    )
    db_session.add(old_task)
    await db_session.commit()

    with patch("app.models._utcnow", return_value=old_completed_at):
        old_task.status = TaskStatus.COMPLETED
        old_task.context = {"pr_merged": True}
        await db_session.commit()

    version.opened_at = window_opened_at
    await db_session.commit()

    with patch("app.models._utcnow", return_value=post_terminal_message_at):
        old_task.latest_status_message = (
            "Deduped after merge; leaving the task terminal"
        )
        await db_session.commit()

    fresh_task = Task(
        project_id=project.id,
        title="fresh-merge",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        context={},
        created_at=fresh_completed_at,
        updated_at=fresh_completed_at,
    )
    db_session.add(fresh_task)
    await db_session.commit()

    with patch("app.models._utcnow", return_value=fresh_completed_at):
        fresh_task.status = TaskStatus.COMPLETED
        fresh_task.context = {
            "pr_merged": True,
            "code_acceptance_verdict": {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "test_name": "test_fresh",
            },
        }
        await db_session.commit()

    agg = await rq._compute_acceptance_window_aggregate(db_session, version)
    assert agg["acceptance_window_total"] == 1
    assert agg["acceptance_window_verified"] == 1
    assert agg["acceptance_window_fraction"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_acceptance_window_aggregate_excludes_malformed_verdict(
    db_session,
):
    """A malformed recorded verdict is advisory only and does not count as verified."""
    project, version = await _make_project_and_version(db_session)

    malformed_task = Task(
        project_id=project.id,
        title="malformed-acceptance",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": True,
            "code_acceptance_verdict": {"kind": "named_test"},
        },
    )
    db_session.add(malformed_task)
    await db_session.commit()

    agg = await rq._compute_acceptance_window_aggregate(db_session, version)
    assert agg["acceptance_window_total"] == 1
    assert agg["acceptance_window_verified"] == 0
    assert agg["acceptance_window_fraction"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_acceptance_window_metric_distinguishes_verified_merge_predicate_from_acceptance_proof(
    db_session,
):
    """acceptance_window_verified counts only explicit proven/satisfied_unproven merges.

    This test exercises the corrected predicate: a task with a proven
    code_acceptance_verdict is counted as verified, while an identical task
    with no verdict (undeclared) is counted in the total but NOT in the
    verified count — demonstrating that the metric name honestly reflects
    declared-and-verified acceptance proof, not a broad "was merged" signal.
    """
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project, version = await _make_project_and_version(db_session)

    # Task with explicit proven acceptance — must count as verified.
    task_proven = Task(
        project_id=project.id,
        title="proven-merge",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "test_name": "test_feature_works",
            },
        },
    )
    # Task with satisfied-but-not-machine-proven verdict — also counts as verified.
    task_satisfied_unproven = Task(
        project_id=project.id,
        title="satisfied-unproven-merge",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": False,
                "kind": "named_test",
                "test_name": "test_other_feature",
            },
        },
    )
    # Task with no verdict at all (undeclared / legacy) — must NOT count as verified.
    task_undeclared = Task(
        project_id=project.id,
        title="undeclared-merge",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )
    assert CODE_ACCEPTANCE_VERDICT_KEY not in (task_undeclared.context or {})

    db_session.add(task_proven)
    db_session.add(task_satisfied_unproven)
    db_session.add(task_undeclared)
    await db_session.commit()

    agg = await rq._compute_acceptance_window_aggregate(db_session, version)
    # All three tasks are in the window.
    assert agg["acceptance_window_total"] == 3
    # Only the two explicitly declared tasks (proven + satisfied_unproven) count.
    assert agg["acceptance_window_verified"] == 2
    # Undeclared task is in total but not in verified — fraction is 2/3.
    assert agg["acceptance_window_fraction"] == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_acceptance_window_denominator_write_task_population(db_session):
    """Denominator counts only write-task merges; read-only types are excluded.

    Fixture: 2 write-type tasks (type='create') — one with proven acceptance,
    one without — plus 2 non-write tasks (type='research' and type='review')
    that are also merged. The denominator must equal 2 (write tasks only),
    fraction = 1/2, and the observability field must equal 4 (all merges).
    When there are zero write-task merges, fraction must be None.
    """
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project, version = await _make_project_and_version(db_session)

    # Write task with proven acceptance — counts in total AND verified.
    write_proven = Task(
        project_id=project.id,
        title="write-proven",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "test_name": "test_write_feature",
            },
        },
    )
    # Write task without acceptance — counts in total but NOT in verified.
    write_undeclared = Task(
        project_id=project.id,
        title="write-undeclared",
        payloads=[{"type": "fix"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )
    # Non-write task (research) — must NOT count in write-task total or verified.
    read_research = Task(
        project_id=project.id,
        title="research-task",
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )
    # Non-write task (review) — must NOT count in write-task total or verified.
    read_review = Task(
        project_id=project.id,
        title="review-task",
        payloads=[{"type": "review"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )
    db_session.add(write_proven)
    db_session.add(write_undeclared)
    db_session.add(read_research)
    db_session.add(read_review)
    await db_session.commit()

    agg = await rq._compute_acceptance_window_aggregate(db_session, version)

    # Denominator: write tasks only (create + fix = 2).
    assert agg["acceptance_window_total"] == 2, (
        f"denominator should count only write-task merges; got {agg['acceptance_window_total']}"
    )
    assert agg["acceptance_window_write_total"] == 2
    # Numerator: only the proven write task.
    assert agg["acceptance_window_verified"] == 1
    # Fraction: 1 verified / 2 write-task merges.
    assert agg["acceptance_window_fraction"] == pytest.approx(0.5)
    # Observability field includes ALL landed merges (2 write + 2 read = 4).
    assert agg["acceptance_window_total_all_merges"] == 4, (
        f"all-merges field should include non-write tasks; got {agg['acceptance_window_total_all_merges']}"
    )


@pytest.mark.asyncio
async def test_acceptance_window_denominator_none_when_no_write_tasks(db_session):
    """fraction must be None when zero write-task merges land in the window.

    Non-write merges inflate acceptance_window_total_all_merges but must NOT
    produce a 0.0 fraction — None signals 'no coverage judgement possible'.
    """
    project, version = await _make_project_and_version(db_session)

    for task_type in ("research", "review", "audit"):
        db_session.add(
            Task(
                project_id=project.id,
                title=f"{task_type}-task",
                payloads=[{"type": task_type}],
                status=TaskStatus.COMPLETED,
                context={"pr_merged": True},
            )
        )
    await db_session.commit()

    agg = await rq._compute_acceptance_window_aggregate(db_session, version)

    assert agg["acceptance_window_total"] == 0
    assert agg["acceptance_window_write_total"] == 0
    assert agg["acceptance_window_verified"] == 0
    assert agg["acceptance_window_fraction"] is None, (
        "fraction must be None when no write-task merges exist, not 0.0"
    )
    assert agg["acceptance_window_total_all_merges"] == 3


@pytest.mark.asyncio
async def test_finalize_records_acceptance_window_counts_in_verdict_sub_scores(
    db_session, mocker
):
    """_finalize_qualification persists acceptance_window_* keys in sub_scores.

    Sets up two merged tasks in the version window — one with a proven
    acceptance verdict (counted as verified), one with an unsatisfied
    acceptance verdict (excluded from the verified count) — then finalizes
    and asserts the three acceptance_window_* keys appear alongside the
    existing composite keys in version.last_qualification["sub_scores"].
    This exercises real non-test production code in _finalize_qualification
    and _compute_acceptance_window_aggregate.
    """
    from app import shared_state
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    # Seed the qualifier children (audit + smoketest).
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )
    await _freeze_current_qualification_contract(db_session, version)
    mocker.patch.object(
        rq, "resolve_project_promotion_enabled", return_value=False
    )

    # Task 1: satisfied-and-proven verdict — counted as verified.
    task_verified = Task(
        project_id=project.id,
        title="verified-merge",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "test_name": "test_verified",
            },
        },
    )
    # Task 2: unsatisfied acceptance verdict — counted in total but NOT in verified.
    task_blocked = Task(
        project_id=project.id,
        title="blocked-merge",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": False,
                "proven": False,
                "kind": "named_test",
                "reason": "declared test not in changed files",
            },
        },
    )
    db_session.add(task_verified)
    db_session.add(task_blocked)
    await db_session.commit()

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    assert result["status"] == "dry_run"

    sub_scores = version.last_qualification["sub_scores"]
    # Existing composite keys still present — additive, no breakage.
    assert "audit" in sub_scores
    assert "test_erosion" in sub_scores
    # Qualifier VM smoketest scoring was retired; ordinary post-promotion
    # application smoke remains covered by its separate handler contract.
    assert "smoketest" not in sub_scores
    # Acceptance window aggregate keys persisted alongside them.
    assert sub_scores["acceptance_window_total"] == 2
    assert sub_scores["acceptance_window_verified"] == 1
    assert sub_scores["acceptance_window_fraction"] == pytest.approx(0.5)


# ===========================================================================
# _guard_ci_unresolved_promotion_pending — incident 2026-06-09 monotonic
# ledger guard.
#
# When _finalize_qualification would write not_promoted for a ci_unresolved
# blocker (transient — CI was pending, not red), this guard checks GitHub
# state first. If the candidate is already on main the PR merged while the
# promotion_pending commit was rolling back — record success instead. If an
# open release PR still exists, restore promotion_pending so the reaper
# confirms the merge. Only if neither condition holds does the caller proceed
# with not_promoted.
# ===========================================================================


@pytest.mark.asyncio
async def test_guard_ci_unresolved_no_candidate_sha_returns_none(db_session, mocker):
    """Guard is a no-op (returns None) when candidate_sha is absent — no
    GitHub I/O should occur."""
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )

    project, version = await _make_project_and_version(db_session)

    result = await rq._guard_ci_unresolved_promotion_pending(
        db_session, version, None, {}, "tok", "acme", "widget", project
    )

    assert result is None
    is_ancestor.assert_not_called()


@pytest.mark.asyncio
async def test_guard_ci_unresolved_single_branch_is_inert_before_ancestry(
    db_session, mocker
):
    """Single-branch repos must not let same-branch ancestry look promoted.

    When integration_branch == promotion_target, the candidate is already
    reachable from that branch by construction. The ci_unresolved guard must
    return None before any GitHub ancestry/open-PR checks so the caller can
    preserve the normal not_promoted path instead of recording promotion
    success for a release PR that never existed.
    """
    project, version = await _make_project_and_version_custom_branches(
        db_session,
        integration_branch="main",
        promotion_target="main",
        phase="stabilize",
    )
    candidate_sha = "singlebr" * 5
    base_record = {"candidate_sha": candidate_sha, "confidence": 0.70}

    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )
    open_prs = mocker.patch(
        "app.github.get_open_pull_requests",
        new=AsyncMock(return_value=[]),
    )

    result = await rq._guard_ci_unresolved_promotion_pending(
        db_session,
        version,
        candidate_sha,
        base_record,
        "tok",
        "acme",
        "custom",
        project,
    )

    assert result is None
    is_ancestor.assert_not_called()
    open_prs.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification is None


@pytest.mark.asyncio
async def test_guard_ci_unresolved_candidate_already_on_main_records_promoted(
    db_session, mocker
):
    """Guard: when is_ancestor_of confirms the candidate is already reachable
    from the promotion target, _record_promotion_success is called instead of
    letting the caller write not_promoted. Regression: incident 2026-06-09."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    candidate_sha = "abcd" * 10
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.get_branch_sha",
        # Exact target equality is the no-PR promotion receipt.  Mere ancestry
        # under a later moving main tip is intentionally no longer enough to
        # attribute and tag this candidate as the promoted release.
        new=AsyncMock(return_value=candidate_sha),
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    project, version = await _make_project_and_version(db_session)
    # Promotion success opens several best-effort follow-on sessions.  Keep
    # those nested probes on SQLite's single test-owned connection, matching
    # the fixture contract used by the connected promotion tests above.
    mocker.patch(
        "app.database.AsyncSessionLocal",
        new=_shared_session_factory(db_session),
    )
    mocker.patch(
        "app.github.get_pull_requests_for_head_branch",
        new=AsyncMock(return_value=[]),
    )
    base_record = {
        "candidate_sha": candidate_sha,
        "confidence": 0.91,
        "pr_url": "https://github.com/acme/widget/pull/5",
    }

    result = await rq._guard_ci_unresolved_promotion_pending(
        db_session,
        version,
        candidate_sha,
        base_record,
        "tok",
        "acme",
        "widget",
        project,
    )
    await db_session.commit()

    assert result is not None
    # Guard intercepted — this would have been a spurious not_promoted write.
    assert result.get("status") == "promoted"
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"


@pytest.mark.asyncio
async def test_guard_ci_unresolved_open_pr_restores_promotion_pending(
    db_session, mocker
):
    """Guard: when the candidate is NOT yet on main but an open release PR
    exists, restores canonical managed promotion_pending so the reconciler can
    revalidate and retry it. The caller must NOT write not_promoted."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_pull_requests_for_head_branch",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="different-target-tip"),
    )
    mocker.patch(
        "app.github.get_open_pull_requests",
        new=AsyncMock(
            return_value=[
                {
                    "number": 42,
                    "html_url": "https://github.com/acme/widget/pull/42",
                    "state": "open",
                    "head": {"ref": "release-v1", "sha": "beef" * 10},
                    "base": {"ref": "main"},
                }
            ]
        ),
    )

    project, version = await _make_project_and_version(db_session)
    candidate_sha = "beef" * 10
    base_record = {"candidate_sha": candidate_sha, "confidence": 0.90}

    result = await rq._guard_ci_unresolved_promotion_pending(
        db_session,
        version,
        candidate_sha,
        base_record,
        "tok",
        "acme",
        "widget",
        project,
    )
    await db_session.commit()

    assert result is not None
    assert result.get("status") == "pending_managed_check_wait"
    assert result.get("pr_number") == 42
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    pend = version.last_qualification.get("promotion_pending", {})
    assert pend.get("pr_number") == 42
    assert pend.get("managed_check_wait") is True
    assert pend.get("restored_by_ci_unresolved_guard") is True


@pytest.mark.asyncio
async def test_guard_ci_unresolved_no_open_pr_returns_none(db_session, mocker):
    """Guard: candidate is not on main AND no open release PR exists — guard
    returns None and the caller proceeds with the normal not_promoted write."""
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_open_pull_requests",
        new=AsyncMock(return_value=[]),
    )

    project, version = await _make_project_and_version(db_session)
    candidate_sha = "cafe" * 10
    base_record = {"candidate_sha": candidate_sha, "confidence": 0.70}

    result = await rq._guard_ci_unresolved_promotion_pending(
        db_session,
        version,
        candidate_sha,
        base_record,
        "tok",
        "acme",
        "widget",
        project,
    )

    assert result is None


@pytest.mark.asyncio
async def test_requalification_ready_only_when_all_high_medium_resolved(db_session):
    """Readiness flips True only once EVERY carried high+medium finding is resolved.

    This is the acceptance-gate named test: the readiness signal must remain
    False while any gating finding is unresolved and become True exactly when
    all high+medium carried findings have a ledger row in resolved/stale.
    """
    from app import release_readiness as rr

    project, version = await _make_project_and_version(db_session)
    high = _carried_finding("high", "SQL injection in admin search", "app/admin.py")
    medium = _carried_finding("medium", "Missing index on task lookup", "app/db.py")
    low = _carried_finding("low", "Typo in log message", "app/log.py")
    await _set_last_qualification_findings(db_session, version, [high, medium, low])

    # Nothing resolved yet → not ready; both gating findings unresolved.
    out = await rr.compute_requalification_readiness(db_session, project.id)
    assert out["requalification_ready"] is False
    assert out["gating_total"] == 2  # low ignored
    assert out["gating_resolved"] == 0
    states = {f["key"]: f["state"] for f in out["findings"]}
    assert states[high["key"]] == "unresolved"
    assert states[medium["key"]] == "unresolved"

    # Resolve the HIGH only → still not ready (medium outstanding).
    await rr.apply_finding_resolution(
        db_session,
        project.id,
        version.id,
        high["key"],
        "resolved",
        severity="high",
        resolving_ref="PR #9999",
    )
    await db_session.commit()
    out = await rr.compute_requalification_readiness(db_session, project.id)
    assert out["requalification_ready"] is False
    assert out["gating_resolved"] == 1

    # Mark the MEDIUM as code-verified stale → every gating finding resolved → ready.
    await rr.apply_finding_resolution(
        db_session,
        project.id,
        version.id,
        medium["key"],
        "stale",
        severity="medium",
        note="no longer reachable on candidate",
        verified_against_sha="abc1234",
    )
    await db_session.commit()
    out = await rr.compute_requalification_readiness(db_session, project.id)
    assert out["requalification_ready"] is True
    assert out["gating_total"] == 2
    assert out["gating_resolved"] == 2
    states = {f["key"]: f["state"] for f in out["findings"]}
    assert states[high["key"]] == "resolved"
    assert states[medium["key"]] == "stale"


@pytest.mark.asyncio
async def test_requalification_deferred_while_promotion_ci_remediation_in_flight(
    db_session,
):
    """An in-flight promotion-CI remediation fix defers re-qualification.

    The promotion-gate harvest spawns an auto-remediation task tagged for the
    version when a promotion PR's CI fails. While that fix is active, readiness
    must be False so the next promotion attempt waits for it to land on the
    integration branch; once the task is terminal the gate clears.
    """
    from app import release_readiness as rr
    from app.models import Task, TaskStatus

    project, version = await _make_project_and_version(db_session)
    # No carried findings → would normally be ready to re-qualify.
    await _set_last_qualification_findings(db_session, version, [])
    out = await rr.compute_requalification_readiness(db_session, project.id)
    assert out["requalification_ready"] is True

    task = Task(
        project_id=project.id,
        title="Auto-fix: CI failure on automain",
        payloads=[{"type": "create", "base_branch": "automain"}],
        context={
            "promotion_ci_remediation": True,
            "promotion_ci_remediation_version_id": str(version.id),
        },
        status=TaskStatus.QUEUED,
        created_by="SYSTEM_AUTO_REMEDIATION",
        auto_remediation=True,
    )
    db_session.add(task)
    await db_session.commit()

    out = await rr.compute_requalification_readiness(db_session, project.id)
    assert out["requalification_ready"] is False
    assert "remediation in flight" in out["reason"]

    # Fix lands (task reaches a terminal state) → gate clears.
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()
    out = await rr.compute_requalification_readiness(db_session, project.id)
    assert out["requalification_ready"] is True

    # A remediation tagged for a DIFFERENT version must not gate this one.
    other = Task(
        project_id=project.id,
        title="Auto-fix: CI failure on automain",
        payloads=[{"type": "create", "base_branch": "automain"}],
        context={
            "promotion_ci_remediation": True,
            "promotion_ci_remediation_version_id": str(uuid.uuid4()),
        },
        status=TaskStatus.IN_PROGRESS,
        created_by="SYSTEM_AUTO_REMEDIATION",
        auto_remediation=True,
    )
    db_session.add(other)
    await db_session.commit()
    out = await rr.compute_requalification_readiness(db_session, project.id)
    assert out["requalification_ready"] is True


@pytest.mark.asyncio
async def test_requalification_false_when_high_medium_unresolved(db_session):
    """A single unresolved high/medium finding holds readiness False."""
    from app import release_readiness as rr

    project, version = await _make_project_and_version(db_session)
    f = _carried_finding("high", "Auth bypass", "app/auth.py")
    await _set_last_qualification_findings(db_session, version, [f])

    out = await rr.compute_requalification_readiness(db_session, project.id)
    assert out["requalification_ready"] is False
    assert out["findings"][0]["state"] == "unresolved"


@pytest.mark.asyncio
async def test_requalification_low_severity_findings_ignored(db_session):
    """Low/info findings never gate readiness — they are advisory only."""
    from app import release_readiness as rr

    project, version = await _make_project_and_version(db_session)
    findings = [
        _carried_finding("low", "Minor style nit", "a.py"),
        _carried_finding("info", "FYI: stale comment", "b.py"),
    ]
    await _set_last_qualification_findings(db_session, version, findings)

    out = await rr.compute_requalification_readiness(db_session, project.id)
    # Zero gating findings → ready by convention.
    assert out["requalification_ready"] is True
    assert out["gating_total"] == 0
    assert out["findings"] == []


@pytest.mark.asyncio
async def test_resolution_survives_last_qualification_overwrite(db_session):
    """A ledger row persists across a simulated last_qualification re-finalize.

    The whole point of the separate table: a subsequent qualification finalize
    overwrites last_qualification wholesale, but the resolution ledger keyed by
    finding_key still resolves the carried finding.
    """
    from app import release_readiness as rr

    project, version = await _make_project_and_version(db_session)
    f = _carried_finding("high", "Race in token refresh", "app/token.py")
    await _set_last_qualification_findings(db_session, version, [f])

    await rr.apply_finding_resolution(
        db_session,
        project.id,
        version.id,
        f["key"],
        "resolved",
        severity="high",
    )
    await db_session.commit()

    # Simulate a re-qualification finalize that OVERWRITES last_qualification —
    # the same finding is re-carried (same content key), nothing in the blob
    # remembers the prior resolution.
    await _set_last_qualification_findings(db_session, version, [dict(f)])

    out = await rr.compute_requalification_readiness(db_session, project.id)
    assert out["requalification_ready"] is True
    assert out["findings"][0]["state"] == "resolved"


@pytest.mark.asyncio
async def test_apply_finding_resolution_upserts(db_session):
    """A second write for the same (version, key) UPDATES, not duplicates."""
    from sqlalchemy import func, select

    from app import release_readiness as rr
    from app.models import AuditFindingResolution

    project, version = await _make_project_and_version(db_session)
    key = "deadbeefcafef00d"

    await rr.apply_finding_resolution(
        db_session,
        project.id,
        version.id,
        key,
        "resolved",
        severity="high",
        note="first",
        resolving_ref="PR #1",
    )
    await db_session.commit()

    await rr.apply_finding_resolution(
        db_session,
        project.id,
        version.id,
        key,
        "stale",
        severity="high",
        note="second",
        verified_against_sha="ff00ff00",
    )
    await db_session.commit()

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(AuditFindingResolution)
            .where(
                AuditFindingResolution.release_version_id == version.id,
                AuditFindingResolution.finding_key == key,
            )
        )
    ).scalar_one()
    assert count == 1  # upserted, not duplicated

    row = (
        await db_session.execute(
            select(AuditFindingResolution).where(
                AuditFindingResolution.release_version_id == version.id,
                AuditFindingResolution.finding_key == key,
            )
        )
    ).scalar_one()
    assert row.state == "stale"
    assert row.note == "second"
    assert row.verified_against_sha == "ff00ff00"
    # Latest-write-wins cleared the prior resolving_ref.
    assert row.resolving_ref is None


@pytest.mark.asyncio
async def test_apply_finding_resolution_rejects_bad_state(db_session):
    """An invalid resolution state is rejected before any write."""
    from app import release_readiness as rr

    project, version = await _make_project_and_version(db_session)
    with pytest.raises(ValueError):
        await rr.apply_finding_resolution(
            db_session,
            project.id,
            version.id,
            "k",
            "bogus",
        )


@pytest.mark.asyncio
async def test_requalification_truncated_all_resolved_is_ready(db_session):
    """A truncated list with every VISIBLE gating finding resolved is ready.

    The carried list was clipped at the cap, but blocking forever on a
    truncated-but-fully-resolved list permanently wedges the auto-redrive —
    a fresh qualification run re-scores the full finding set anyway.  The
    reason carries the re-score caveat, and ``audit_findings_truncated``
    stays surfaced.  Truncated with UNRESOLVED visible gating findings
    remains not-ready (unit-covered by test_truncated_partial_resolved_not_ready
    in test_release_readiness.py).
    """
    from app import release_readiness as rr

    project, version = await _make_project_and_version(db_session)
    f = _carried_finding("high", "Visible high finding", "app/x.py")
    await _set_last_qualification_findings(db_session, version, [f], truncated=True)
    await rr.apply_finding_resolution(
        db_session,
        project.id,
        version.id,
        f["key"],
        "resolved",
        severity="high",
    )
    await db_session.commit()

    out = await rr.compute_requalification_readiness(db_session, project.id)
    assert out["audit_findings_truncated"] is True
    assert out["requalification_ready"] is True
    assert "truncated" in out["reason"].lower()
    assert "re-score" in out["reason"].lower()


@pytest.mark.asyncio
async def test_requalification_prefers_verdict_row_findings(db_session):
    """Carried findings resolve from the latest QualificationVerdict row.

    Mirrors the agent fetch: the immutable verdict's audit_findings column is
    preferred over the mutable last_qualification blob.
    """
    from app import release_readiness as rr

    project, version = await _make_project_and_version(db_session)
    vf = _carried_finding("medium", "Verdict-carried finding", "app/v.py")
    verdict = QualificationVerdict(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="c0ffee" * 6 + "0000",
        confidence=0.5,
        terminal_status="dry_run",
        audit_findings=[vf],
    )
    db_session.add(verdict)
    # last_qualification carries a DIFFERENT finding — must be ignored in favor
    # of the verdict row.
    await _set_last_qualification_findings(
        db_session, version, [_carried_finding("high", "stale blob finding", "z.py")]
    )

    out = await rr.compute_requalification_readiness(db_session, project.id)
    keys = {f["key"] for f in out["findings"]}
    assert vf["key"] in keys
    assert out["gating_total"] == 1


@pytest.mark.asyncio
async def test_requalification_no_open_version(db_session):
    """No open version → not ready, with an explanatory reason."""
    from app import release_readiness as rr
    from app.models import Project

    project = Project(name="no-ver", user_id="u", github_owner="o", github_repo="r")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    out = await rr.compute_requalification_readiness(db_session, project.id)
    assert out["requalification_ready"] is False
    assert out["open_version_id"] is None


# ---------------------------------------------------------------------------
# _recover_confidence_from_ci_unresolved — unit tests
# ---------------------------------------------------------------------------


class TestRecoverConfidenceFromCiUnresolved:
    """Unit tests for release_promotion._recover_confidence_from_ci_unresolved."""

    def _fn(self, base_record, policy=None):
        from app.release_promotion import _recover_confidence_from_ci_unresolved

        return _recover_confidence_from_ci_unresolved(base_record, policy=policy)

    def test_non_ci_unresolved_blocker_returns_unchanged(self):
        """Record with a blocker other than ci_unresolved is returned unchanged."""
        record = {"blocker": "ci_not_green", "confidence": 0.0}
        out = self._fn(record)
        assert out is record

    def test_no_blocker_returns_unchanged(self):
        """Record with no blocker field is returned unchanged."""
        record = {"confidence": 0.95}
        out = self._fn(record)
        assert out is record

    def test_ci_unresolved_with_valid_sub_scores_recovers_confidence(self):
        """Core recovery path: ci_unresolved + valid sub_scores → real confidence via compute_composite."""
        record = {
            "blocker": "ci_unresolved",
            "confidence": 0.0,
            "sub_scores": {"audit": 1.0, "test_erosion": 1.0, "smoketest": 1.0},
            "smoketest_enabled": True,
        }
        out = self._fn(record)
        # Recovered confidence should be >0 (all sub_scores perfect)
        assert out["confidence"] > 0.0
        assert out.get("confidence_recovered_from_ci_unresolved") is True
        # blocker should now be absent or None (no remaining hard blocker)
        assert out.get("blocker") is None or out.get("blocker") == ""
        # Original fields preserved
        assert out["sub_scores"] == record["sub_scores"]

    def test_ci_unresolved_recovery_blocker_clears_when_ci_green(self):
        """After recovery, ci_unresolved blocker is gone (re-ran compute_composite with ci_green=True)."""
        record = {
            "blocker": "ci_unresolved",
            "confidence": 0.0,
            "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.95},
            "smoketest_enabled": True,
        }
        out = self._fn(record)
        assert out.get("blocker") != "ci_unresolved"
        assert out.get("confidence_recovered_from_ci_unresolved") is True

    def test_ci_unresolved_no_sub_scores_marks_recovery_failed(self):
        """ci_unresolved with no sub_scores key and confidence=0.0 → confidence_recovery_failed."""
        record = {"blocker": "ci_unresolved", "confidence": 0.0}
        out = self._fn(record)
        assert out.get("confidence_recovery_failed") is True
        assert "sub_scores absent" in (
            out.get("confidence_recovery_failure_reason") or ""
        )

    def test_ci_unresolved_sub_scores_not_dict_marks_recovery_failed(self):
        """ci_unresolved with sub_scores that is not a dict → confidence_recovery_failed."""
        record = {
            "blocker": "ci_unresolved",
            "confidence": 0.0,
            "sub_scores": [1.0, 0.9],
        }
        out = self._fn(record)
        assert out.get("confidence_recovery_failed") is True
        assert "not a dict" in (out.get("confidence_recovery_failure_reason") or "")

    def test_ci_unresolved_malformed_audit_score_marks_recovery_failed(self):
        """ci_unresolved with non-numeric audit sub_score → confidence_recovery_failed."""
        record = {
            "blocker": "ci_unresolved",
            "confidence": 0.0,
            "sub_scores": {"audit": "bad", "test_erosion": 0.9, "smoketest": 0.9},
        }
        out = self._fn(record)
        assert out.get("confidence_recovery_failed") is True
        assert "audit" in (out.get("confidence_recovery_failure_reason") or "")

    def test_ci_unresolved_malformed_test_erosion_marks_recovery_failed(self):
        """ci_unresolved with non-numeric test_erosion sub_score → confidence_recovery_failed."""
        record = {
            "blocker": "ci_unresolved",
            "confidence": 0.0,
            "sub_scores": {"audit": 0.9, "test_erosion": None, "smoketest": 0.9},
        }
        out = self._fn(record)
        assert out.get("confidence_recovery_failed") is True
        assert "test_erosion" in (out.get("confidence_recovery_failure_reason") or "")

    def test_ci_unresolved_non_zero_confidence_no_sub_scores_returns_unchanged(self):
        """ci_unresolved but confidence>0.0 with missing sub_scores returns unchanged (not recovery_failed)."""
        record = {"blocker": "ci_unresolved", "confidence": 0.88}
        out = self._fn(record)
        assert out is record
        assert "confidence_recovery_failed" not in out

    def test_ci_unresolved_smoketest_disabled_recovers_without_smoketest(self):
        """Recovery works when smoketest_enabled=False (drops smoketest dimension from scoring)."""
        record = {
            "blocker": "ci_unresolved",
            "confidence": 0.0,
            "sub_scores": {"audit": 1.0, "test_erosion": 1.0},
            "smoketest_enabled": False,
        }
        out = self._fn(record)
        assert out.get("confidence_recovered_from_ci_unresolved") is True
        assert out["confidence"] > 0.0

    def test_ci_unresolved_preserves_criterion_coverage_blocker(self):
        """Recovery re-runs the goal-verification gate on the persisted per-category
        verdicts: an active category still ``not_met`` keeps the
        criterion_coverage_incomplete blocker after CI clears."""
        record = {
            "blocker": "ci_unresolved",
            "confidence": 0.0,
            "sub_scores": {"audit": 1.0, "test_erosion": 1.0, "smoketest": 1.0},
            "smoketest_enabled": True,
            # The finalize persists the sha-keyed per-category verdicts on the
            # base_record; the recompute re-runs the SAME gate over them.
            "category_verdicts": [
                {"category_key": "c-1", "status": "met", "tier": "judged",
                 "citations": [{"kind": "diff", "ref": "a.py:1"}]},
                {"category_key": "c-2", "status": "not_met", "tier": "judged",
                 "gap": "not delivered", "citations": []},
            ],
        }
        out = self._fn(record)
        assert out["blocker"] == "criterion_coverage_incomplete"
        assert out["confidence"] == 0.0

    def test_ci_unresolved_preserves_migration_blocker(self):
        """Recovery keeps deterministic migration blockers masked by CI."""
        record = {
            "blocker": "ci_unresolved",
            "confidence": 0.0,
            "sub_scores": {"audit": 1.0, "test_erosion": 1.0, "smoketest": 1.0},
            "smoketest_enabled": True,
            "migration_safety": "block",
        }
        out = self._fn(record)
        assert out["blocker"] == "migration_identity_collision"
        assert out["confidence"] == 0.0

    def test_ci_unresolved_preserves_armed_merge_grade_misses(self):
        """Recovery keeps an armed failing merge-grade blocked even with empty criterion_coverage."""
        record = {
            "blocker": "ci_unresolved",
            "confidence": 0.0,
            "sub_scores": {"audit": 1.0, "test_erosion": 1.0, "smoketest": 1.0},
            "smoketest_enabled": True,
            "criterion_coverage": {
                "total": 0,
                "covered": 0,
                "partial": 0,
                "uncovered": 0,
            },
            "merge_grade_status": "fail",
            "merge_grade": {"blocked": True},
        }
        out = self._fn(record, policy={**_POLICY, "merge_grade_block_threshold": 0.1})
        assert out["blocker"] == "merge_grade_misses"
        assert out["confidence"] == 0.0

    def test_ci_unresolved_default_off_merge_grade_misses_recovers(self):
        """Recovery does not hard-block a failing merge-grade while the gate is off."""
        record = {
            "blocker": "ci_unresolved",
            "confidence": 0.0,
            "sub_scores": {"audit": 1.0, "test_erosion": 1.0, "smoketest": 1.0},
            "smoketest_enabled": True,
            "merge_grade_status": "fail",
            "merge_grade": {"blocked": True},
        }
        out = self._fn(record, policy=_POLICY)
        assert out["blocker"] is None
        assert out["confidence"] > 0.0

    def test_enforce_mode_ci_unresolved_recovery_includes_debt_blend(self):
        """NAMED ACCEPTANCE TEST — enforce-mode ci_unresolved recovery records a debt-blended confidence.

        When ci_unresolved forces confidence to 0.0, the recovery helper
        recomputes the 'real' qualified confidence. In enforce mode (completion_debt
        weight > 0), the debt blend must be applied, so the recovered confidence
        equals a normal finalize of the same sub_scores.
        """
        # Establish enforce-mode policy with debt weight.
        enforce_policy = {
            "weights": {
                "audit": 0.40,
                "test_erosion": 0.30,
                "smoketest": 0.30,
                "completion_debt": 0.15,
            },
            "threshold": 0.90,
        }

        # Sub-scores for a normally-qualifying candidate.
        sub_scores = {
            "audit": 0.85,
            "test_erosion": 0.90,
            "smoketest": 0.95,
            "debt_pressure": 0.50,  # moderate open debt
        }

        # Normal finalize with ci_green=True (control case).
        normal_result = rq.compute_composite(
            ci_green=True,
            audit_score=sub_scores["audit"],
            test_erosion=sub_scores["test_erosion"],
            smoketest_score=sub_scores["smoketest"],
            policy=enforce_policy,
            debt_pressure_score=sub_scores["debt_pressure"],
        )

        # ci_unresolved blocker — confidence forced to 0.0 (no debt blend).
        blocked_record = {
            "blocker": "ci_unresolved",
            "confidence": 0.0,
            "sub_scores": sub_scores,
            "smoketest_enabled": True,
        }

        # Recovery applies the debt blend.
        recovered = self._fn(blocked_record, policy=enforce_policy)

        # Recovered confidence must equal the normally-finalized confidence
        # (both include the debt blend).
        assert recovered["confidence"] == pytest.approx(normal_result["confidence"])
        assert recovered.get("confidence_recovered_from_ci_unresolved") is True
        assert recovered.get("blocker") is None


@pytest.mark.asyncio
async def test_readiness_excludes_rubric_holds_from_failed_attempts(db_session):
    """A rubric_changed hold measured nothing under the final rubric — legacy
    QualificationVerdict rows for it must never be counted as failed attempts in
    the promotion-readiness aggregation (mirrors infra_failure's exclusion).

    Without the exclusion these three rows would break the clean streak, drag
    the would-promote rate to 5/8, and surface "rubric_changed" as a recurring
    blocker — i.e. the gate would read a rubric hold as a quality failure."""
    project, _version = await _make_project_and_version(db_session)

    # Five clean self-approval verdicts: with rubric holds excluded this clears
    # the min-verdicts threshold and every readiness gate.
    for _ in range(5):
        db_session.add(
            QualificationVerdict(
                project_id=project.id,
                release_version_id=_version.id,
                candidate_sha="abc1234",
                confidence=0.95,
                sub_scores={},
                blocker=None,
                would_promote=True,
                promotion_enabled=False,
                terminal_status="not_promoted",
                threshold=0.9,
            )
        )
    # Three rubric_changed holds, inserted LAST so they are the most-recent rows
    # (the query reads most-recent-first — exactly where they would break the
    # clean streak if they were counted).
    for _ in range(3):
        db_session.add(
            QualificationVerdict(
                project_id=project.id,
                release_version_id=_version.id,
                candidate_sha="abc1234",
                confidence=0.0,
                sub_scores={},
                blocker="rubric_changed",
                would_promote=False,
                promotion_enabled=False,
                terminal_status="rubric_changed",
                threshold=0.9,
            )
        )
    await db_session.commit()

    result = await rq.compute_promotion_readiness(
        db_session, project.id, promotion_enabled=False
    )

    assert result["verdicts_evaluated"] == 5, (
        "rubric_changed holds must not be evaluated as failed attempts"
    )
    assert result["would_promote_count"] == 5
    assert result["would_promote_rate"] == 1.0
    assert result["consecutive_clean_passes"] == 5, (
        "rubric_changed holds must not break the consecutive-clean streak"
    )
    assert "rubric_changed" not in result["blocker_counts"]
    assert result["ready"] is True


@pytest.mark.asyncio
async def test_acceptance_window_corroboration_upgrades_soft_to_proven(db_session):
    """The acceptance-window aggregate corroborates soft named_test verdicts
    recorded on a non-CI-monitor landing path against the stored merge commit,
    upgrading them to machine-proven — the fix for "the acceptance window mints
    ~0 proven coverage".

    A landed write task whose declared test is NOT in its PR diff was capped at
    soft/unproven credit because every non-CI-monitor landing path recorded the
    verdict without a corroboration marker. The window pass re-runs the backend's
    own tree check at the stored merge commit; a confirmed presence stamps the
    marker and re-records the verdict, moving the task into the proven bucket.
    """
    from unittest.mock import AsyncMock, patch

    from app.acceptance_gate import (
        ACCEPTANCE_RULE_VERSION,
        ACCEPTANCE_TEST_CORROBORATED_KEY,
        CODE_ACCEPTANCE_VERDICT_KEY,
    )
    from app.landed_signal import LANDED_MERGE_SHA_KEY, LANDED_SIGNAL_KEY

    project, version = await _make_project_and_version(db_session)
    task = Task(
        project_id=project.id,
        title="soft-named-test",
        payloads=[
            {
                "type": "create",
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_seam.py",
                    "test_id": "test_x",
                },
            }
        ],
        status=TaskStatus.COMPLETED,
        context={
            LANDED_SIGNAL_KEY: True,
            "_pr_changed_files": ["app/seam.py"],  # declared test NOT in diff
            "review_loop": {
                "acceptance_test_targeted_green": True,
                "acceptance_test_targeted_descriptor": (
                    "backend/testing/test_seam.py"
                ),
            },
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": False,
                "kind": "named_test",
                "reason": "credited soft without machine proof",
                "rule_version": ACCEPTANCE_RULE_VERSION,
            },
            LANDED_MERGE_SHA_KEY: "a" * 40,
        },
    )
    db_session.add(task)
    await db_session.commit()

    with patch(
        "app.acceptance_gate.corroborate_acceptance_test_at_merge",
        new=AsyncMock(return_value=True),
    ):
        agg = await rq._compute_acceptance_window_aggregate(db_session, version)

    assert agg["acceptance_window_total"] == 1
    assert agg["acceptance_window_verified"] == 1
    assert agg["acceptance_window_fraction"] == pytest.approx(1.0)
    assert agg["acceptance_window_buckets"]["proven"] == 1
    # The aggregate mutated the session-bound task in place; the enclosing
    # qualifier flow's commit persists the upgrade. Commit here to mirror that
    # contract, then verify the marker and re-recorded verdict are durable.
    await db_session.commit()
    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get(ACCEPTANCE_TEST_CORROBORATED_KEY) is True
    verdict = ctx.get(CODE_ACCEPTANCE_VERDICT_KEY)
    assert isinstance(verdict, dict)
    assert verdict.get("proven") is True


@pytest.mark.asyncio
async def test_acceptance_window_corroboration_cap_bounds_attempts_not_successes(
    db_session,
):
    """The per-drive corroboration cap must bound GitHub ATTEMPTS, not successes.

    Previously the counter advanced only on a confirmed presence, so a window
    where every candidate corroborated indeterminate issued one round-trip per
    task with no bound at all. And because a non-True result stamps no marker,
    the same tasks were re-read in full on every later drive, forever.
    """
    from unittest.mock import AsyncMock, patch

    from app.acceptance_gate import (
        ACCEPTANCE_RULE_VERSION,
        ACCEPTANCE_TEST_CORROBORATED_KEY,
        CODE_ACCEPTANCE_VERDICT_KEY,
        WINDOW_CORROBORATION_ATTEMPTED_SHA_KEY,
    )
    from app.landed_signal import LANDED_MERGE_SHA_KEY, LANDED_SIGNAL_KEY

    project, version = await _make_project_and_version(db_session)
    candidates = 55  # > the 50-attempt cap
    for i in range(candidates):
        db_session.add(
            Task(
                project_id=project.id,
                title=f"soft-named-test-{i}",
                payloads=[
                    {
                        "type": "create",
                        "acceptance_evidence": {
                            "kind": "named_test",
                            "test_path": "backend/testing/test_seam.py",
                            "test_id": "test_x",
                        },
                    }
                ],
                status=TaskStatus.COMPLETED,
                context={
                    LANDED_SIGNAL_KEY: True,
                    "_pr_changed_files": ["app/seam.py"],
                    "review_loop": {
                        "acceptance_test_targeted_green": True,
                        "acceptance_test_targeted_descriptor": (
                            "backend/testing/test_seam.py"
                        ),
                    },
                    CODE_ACCEPTANCE_VERDICT_KEY: {
                        "satisfied": True,
                        "proven": False,
                        "kind": "named_test",
                        "reason": "credited soft without machine proof",
                        "rule_version": ACCEPTANCE_RULE_VERSION,
                    },
                    LANDED_MERGE_SHA_KEY: f"{i:040d}",
                },
            )
        )
    await db_session.commit()

    # Every corroboration is indeterminate (e.g. tree unavailable / no creds).
    indeterminate = AsyncMock(return_value=None)
    with patch(
        "app.acceptance_gate.corroborate_acceptance_test_at_merge", new=indeterminate
    ):
        agg = await rq._compute_acceptance_window_aggregate(db_session, version)

    assert agg["acceptance_window_total"] == candidates
    assert indeterminate.await_count == 50, (
        "the cap must bound ATTEMPTS: every candidate returned an indeterminate "
        f"result, so the loop must stop at 50 round-trips, got "
        f"{indeterminate.await_count}"
    )
    await db_session.commit()

    # Second drive: the 50 attempted tasks carry a durable per-sha attempt
    # marker, so only the 5 never-attempted tasks are re-read. Without the
    # marker every one of the 55 would be re-attempted forever.
    second = AsyncMock(return_value=None)
    with patch("app.acceptance_gate.corroborate_acceptance_test_at_merge", new=second):
        await rq._compute_acceptance_window_aggregate(db_session, version)
    assert second.await_count == candidates - 50, (
        "already-attempted tasks must not be re-read at the same merge sha; "
        f"got {second.await_count} re-attempts"
    )
    await db_session.commit()

    # An indeterminate result never stamps the corroboration marker (which would
    # be a fabricated verdict input) — only the attempt record.
    rows = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    attempted = [
        t for t in rows if WINDOW_CORROBORATION_ATTEMPTED_SHA_KEY in (t.context or {})
    ]
    assert len(attempted) == candidates
    for t in attempted:
        ctx = t.context or {}
        assert ctx[WINDOW_CORROBORATION_ATTEMPTED_SHA_KEY] == ctx[LANDED_MERGE_SHA_KEY]
        assert ACCEPTANCE_TEST_CORROBORATED_KEY not in ctx
        assert ctx[CODE_ACCEPTANCE_VERDICT_KEY]["proven"] is False


@pytest.mark.asyncio
async def test_acceptance_window_corroboration_retries_on_a_new_merge_sha(db_session):
    """The attempt marker is keyed by merge sha, so a task that later records a
    NEW merge commit is eligible for a fresh corroboration attempt."""
    from unittest.mock import AsyncMock, patch

    from app.acceptance_gate import (
        ACCEPTANCE_RULE_VERSION,
        ACCEPTANCE_TEST_CORROBORATED_KEY,
        CODE_ACCEPTANCE_VERDICT_KEY,
        WINDOW_CORROBORATION_ATTEMPTED_SHA_KEY,
    )
    from app.landed_signal import LANDED_MERGE_SHA_KEY, LANDED_SIGNAL_KEY

    project, version = await _make_project_and_version(db_session)
    task = Task(
        project_id=project.id,
        title="soft-named-test",
        payloads=[
            {
                "type": "create",
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_seam.py",
                    "test_id": "test_x",
                },
            }
        ],
        status=TaskStatus.COMPLETED,
        context={
            LANDED_SIGNAL_KEY: True,
            "_pr_changed_files": ["app/seam.py"],
            "review_loop": {
                "acceptance_test_targeted_green": True,
                "acceptance_test_targeted_descriptor": (
                    "backend/testing/test_seam.py"
                ),
            },
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": False,
                "kind": "named_test",
                "reason": "credited soft without machine proof",
                "rule_version": ACCEPTANCE_RULE_VERSION,
            },
            LANDED_MERGE_SHA_KEY: "a" * 40,
        },
    )
    db_session.add(task)
    await db_session.commit()

    with patch(
        "app.acceptance_gate.corroborate_acceptance_test_at_merge",
        new=AsyncMock(return_value=None),
    ):
        await rq._compute_acceptance_window_aggregate(db_session, version)
    await db_session.commit()
    await db_session.refresh(task)
    assert (task.context or {}).get(WINDOW_CORROBORATION_ATTEMPTED_SHA_KEY) == "a" * 40

    # The task re-lands on a different merge commit — corroboration is now a
    # genuinely different read, so it must run again (and can upgrade).
    task.context = {**(task.context or {}), LANDED_MERGE_SHA_KEY: "b" * 40}
    db_session.add(task)
    await db_session.commit()

    retried = AsyncMock(return_value=True)
    with patch("app.acceptance_gate.corroborate_acceptance_test_at_merge", new=retried):
        agg = await rq._compute_acceptance_window_aggregate(db_session, version)
    assert retried.await_count == 1
    assert agg["acceptance_window_buckets"]["proven"] == 1
    await db_session.commit()
    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get(ACCEPTANCE_TEST_CORROBORATED_KEY) is True
    assert ctx[CODE_ACCEPTANCE_VERDICT_KEY]["proven"] is True
    assert ctx[WINDOW_CORROBORATION_ATTEMPTED_SHA_KEY] == "b" * 40

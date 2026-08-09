"""Walkthrough-evidence acceptance tests split from test_acceptance_criteria.py.

This companion keeps the walkthrough alert and Flow 5 routing seam together while
leaving the parent module below its governed size cap. The shared project factory
remains defined in the parent so database setup behavior has one source of truth.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import acceptance_criteria as ac
from app.models import Alert, ReleaseVersion, VersionAcceptanceCriterion
from testing.test_acceptance_criteria import _make_project_with_member

# ---------------------------------------------------------------------------
# Alert on unmatched walkthrough criterion keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_walkthrough_e2e_evidence_alerts_on_unmatched_key(db_session):
    """A passing walkthrough result under a key that no active criterion has
    creates exactly one operator-visible Alert row while still returning the key
    in summary['unmatched_keys']."""
    project = await _make_project_with_member(db_session, name="Walk-Unmatched-Alert")
    sha = "a" * 40
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"status": "scoring", "candidate_sha": sha},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    real_crit = await ac.create_criterion(db_session, project.id, version.id, "Real criterion")
    phantom_key = "c-deadbeef"  # never registered as a criterion

    summary = await ac.record_walkthrough_e2e_evidence(
        db_session,
        version,
        candidate_sha=sha,
        results=[
            {"criterion_key": real_crit.criterion_key, "passed": True, "notes": "green"},
            {"criterion_key": phantom_key, "passed": True, "notes": "phantom"},
        ],
        qualification_attempt_id="attempt-alert-1",
    )

    assert summary["status"] == "recorded"
    assert summary["recorded"] == 1
    assert summary["unmatched_keys"] == [phantom_key]

    # An operator-visible Alert row must have been created for the dropped key.
    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert len(alerts) == 1, f"Expected 1 alert, got {len(alerts)}: {alerts}"
    alert = alerts[0]
    assert alert.event_type == "release.walkthrough_key_unmatched"
    assert phantom_key in alert.message
    assert alert.dedupe_key is not None


@pytest.mark.asyncio
async def test_record_operator_walkthrough_evidence_alerts_on_unmatched_key(db_session):
    """record_operator_walkthrough_evidence creates an Alert when a passing result's
    key resolves to no active criterion, while still returning the key in unmatched_keys."""
    project = await _make_project_with_member(db_session, name="OpWalk-Unmatched-Alert")
    sha = "b" * 40
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    real_crit = await ac.create_criterion(db_session, project.id, version.id, "Op real criterion")
    phantom_key = "c-workday-session-lifecycle"  # the canonical phantom key from the bug

    summary = await ac.record_operator_walkthrough_evidence(
        db_session,
        version,
        candidate_sha=sha,
        results=[
            {"criterion_key": real_crit.criterion_key, "passed": True, "notes": "ok"},
            {"criterion_key": phantom_key, "passed": True, "notes": "phantom"},
        ],
    )

    assert summary["status"] == "recorded"
    assert summary["recorded"] == 1
    assert summary["unmatched_keys"] == [phantom_key]

    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert len(alerts) == 1, f"Expected 1 alert, got {len(alerts)}: {alerts}"
    alert = alerts[0]
    assert alert.event_type == "release.walkthrough_key_unmatched"
    assert phantom_key in alert.message


@pytest.mark.asyncio
async def test_record_walkthrough_e2e_evidence_no_alert_when_all_keys_match(db_session):
    """When every passing key resolves to an active criterion, no Alert is created."""
    project = await _make_project_with_member(db_session, name="Walk-AllMatch-NoAlert")
    sha = "c" * 40
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"status": "scoring", "candidate_sha": sha},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    crit = await ac.create_criterion(db_session, project.id, version.id, "All-match criterion")

    summary = await ac.record_walkthrough_e2e_evidence(
        db_session,
        version,
        candidate_sha=sha,
        results=[
            {"criterion_key": crit.criterion_key, "passed": True},
        ],
        qualification_attempt_id="attempt-no-alert",
    )

    assert summary["status"] == "recorded"
    assert summary["recorded"] == 1
    assert summary["unmatched_keys"] == []

    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert alerts == [], f"Expected no alerts, got {alerts}"


@pytest.mark.asyncio
async def test_record_walkthrough_e2e_evidence_unmatched_alert_is_deduped(db_session):
    """Calling the function twice with the same unmatched key coalesces into one Alert row."""
    project = await _make_project_with_member(db_session, name="Walk-Dedup-Alert")
    sha = "d" * 40
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"status": "scoring", "candidate_sha": sha},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    phantom_key = "c-phantom-dedup"
    results = [{"criterion_key": phantom_key, "passed": True, "notes": "ghost"}]

    await ac.record_walkthrough_e2e_evidence(
        db_session, version, candidate_sha=sha, results=results, qualification_attempt_id="attempt-dedup-1"
    )
    await ac.record_walkthrough_e2e_evidence(
        db_session, version, candidate_sha=sha, results=results, qualification_attempt_id="attempt-dedup-2"
    )

    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert len(alerts) == 1, f"Expected 1 deduped alert, got {len(alerts)}"
    assert alerts[0].occurrence_count == 2


@pytest.mark.asyncio
async def test_record_walkthrough_e2e_evidence_fail_open_on_alert_error(db_session):
    """If alert creation fails, the function still returns the normal summary and does not raise."""
    from unittest.mock import patch, AsyncMock

    project = await _make_project_with_member(db_session, name="Walk-FailOpen")
    sha = "e" * 40
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"status": "scoring", "candidate_sha": sha},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    phantom_key = "c-fail-open-test"
    results = [{"criterion_key": phantom_key, "passed": True}]

    with patch(
        "app.alert_policy.create_project_alert_if_allowed",
        new=AsyncMock(side_effect=RuntimeError("simulated alert failure")),
    ):
        summary = await ac.record_walkthrough_e2e_evidence(
            db_session, version, candidate_sha=sha, results=results
        )

    assert summary["status"] == "recorded"
    assert summary["unmatched_keys"] == [phantom_key]
    assert summary["recorded"] == 0


# ---------------------------------------------------------------------------
# FLOW 5 Workday session lifecycle — phantom key routing and re-walk evidence
# ---------------------------------------------------------------------------
#
# These tests pin the two defects identified in the FLOW 5 walkthrough fix:
# (a) c-workday-session-lifecycle is not a registered criterion — posting it
#     via record_walkthrough_e2e_evidence MUST produce unmatched_keys, not
#     silently dropping evidence without an observable signal.
# (b) When FLOW 5's result is correctly routed to a registered criterion (the
#     re-walk evidence lane, analogous to the c-5214996f criterion), covered-tier
#     evidence is written and the criterion's coverage flips to "covered".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_walkthrough_e2e_evidence_canonical_phantom_workday_key_is_unmatched(
    db_session,
):
    """c-workday-session-lifecycle is a phantom key in record_walkthrough_e2e_evidence.

    Posting a PASS result keyed on c-workday-session-lifecycle must land in
    unmatched_keys (no evidence written, alert fired) — exactly the behaviour
    produced by the FLOW 5 defect before the routing fix.  Pinned here so a
    future regression is immediately visible.
    """
    project = await _make_project_with_member(db_session, name="Walk-Flow5-Phantom")
    sha = "aa" * 20  # 40 hex chars
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"status": "scoring", "candidate_sha": sha},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # No criterion with this key is ever registered — it is the phantom key from
    # the FLOW 5 bug.
    phantom_key = "c-workday-session-lifecycle"

    summary = await ac.record_walkthrough_e2e_evidence(
        db_session,
        version,
        candidate_sha=sha,
        results=[
            {
                "criterion_key": phantom_key,
                "passed": True,
                "notes": ["Workday session started and stopped cleanly"],
            },
        ],
        qualification_attempt_id="attempt-flow5-phantom",
    )

    # Status is still "recorded" (no SHA rejection), but nothing was written.
    assert summary["status"] == "recorded"
    assert summary["recorded"] == 0
    assert summary["skipped_not_passed"] == 0
    assert summary["unmatched_keys"] == [phantom_key], (
        f"Expected [{phantom_key!r}] in unmatched_keys; got {summary['unmatched_keys']}"
    )

    # An operator-visible alert must exist — the drop must NOT be silent.
    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert len(alerts) == 1, f"Expected 1 unmatched-key alert, got {len(alerts)}: {alerts}"
    assert alerts[0].event_type == "release.walkthrough_key_unmatched"
    assert phantom_key in alerts[0].message


@pytest.mark.asyncio
async def test_record_walkthrough_e2e_evidence_flow5_rewalk_criterion_writes_evidence(
    db_session,
):
    """When FLOW 5's result is routed to a registered re-walk criterion, coverage flips.

    This is the "natural home" for FLOW 5 evidence after the routing fix: fold
    the workday-session result into the same criterion that collects re-walk
    evidence (c-5214996f pattern).  Evidence must be written and the criterion
    must show 'covered' tier.
    """
    project = await _make_project_with_member(db_session, name="Walk-Flow5-Rewalk")
    sha = "bb" * 20
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"status": "scoring", "candidate_sha": sha},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Register a real criterion (simulating the c-5214996f re-walk criterion that
    # should receive FLOW 5's evidence after the routing fix).
    rewalk_crit = await ac.create_criterion(
        db_session,
        project.id,
        version.id,
        "Operator click-to-talk Workday session lifecycle (re-walk)",
        created_by="strategist",
    )

    summary = await ac.record_walkthrough_e2e_evidence(
        db_session,
        version,
        candidate_sha=sha,
        results=[
            {
                "criterion_key": rewalk_crit.criterion_key,
                "passed": True,
                "notes": [
                    "Workday session started via UI Start button",
                    "StrategicSessionControl Stop control rendered",
                    "Session stopped cleanly via strategic-session/stop",
                ],
            },
        ],
        qualification_attempt_id="attempt-flow5-rewalk",
    )

    assert summary["status"] == "recorded"
    assert summary["recorded"] == 1
    assert summary["skipped_not_passed"] == 0
    assert summary["unmatched_keys"] == []

    # Coverage must have flipped to covered.
    evidence = await ac.get_evidence_for_criterion(db_session, rewalk_crit.id)
    assert len(evidence) == 1
    ev = evidence[0]
    assert ev.source_kind == ac.WALKTHROUGH_E2E_EVIDENCE_KIND
    assert ev.detail["candidate_sha"] == sha
    assert ev.detail["passed"] is True
    assert "evaluated_at" in ev.detail
    assert ac.compute_criterion_coverage(evidence) == "covered"

    # No alert should be created when all keys are matched.
    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert alerts == [], f"Expected no alerts when key matches; got {alerts}"


@pytest.mark.asyncio
async def test_record_walkthrough_e2e_evidence_phantom_and_rewalk_key_mixed(
    db_session,
):
    """Phantom key is dropped; real re-walk criterion still gets evidence.

    This is the transition scenario: the walkthrough emits BOTH the phantom key
    (c-workday-session-lifecycle) and the real registered re-walk criterion key.
    The real key must receive covered-tier evidence; the phantom must land in
    unmatched_keys with an alert.  This mirrors the post-fix FLOW 5 where the
    old key is still present in the output but the new key is also emitted.
    """
    project = await _make_project_with_member(db_session, name="Walk-Flow5-Mixed")
    sha = "cc" * 20
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"status": "scoring", "candidate_sha": sha},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    rewalk_crit = await ac.create_criterion(
        db_session,
        project.id,
        version.id,
        "Workday session re-walk criterion",
        created_by="strategist",
    )
    phantom_key = "c-workday-session-lifecycle"

    summary = await ac.record_walkthrough_e2e_evidence(
        db_session,
        version,
        candidate_sha=sha,
        results=[
            {"criterion_key": rewalk_crit.criterion_key, "passed": True, "notes": "green"},
            {"criterion_key": phantom_key, "passed": True, "notes": "phantom"},
        ],
        qualification_attempt_id="attempt-flow5-mixed",
    )

    # One real, one phantom.
    assert summary["status"] == "recorded"
    assert summary["recorded"] == 1
    assert summary["unmatched_keys"] == [phantom_key]

    # Real criterion: covered.
    evidence = await ac.get_evidence_for_criterion(db_session, rewalk_crit.id)
    assert len(evidence) == 1
    assert ac.compute_criterion_coverage(evidence) == "covered"

    # Alert fired for the dropped phantom key.
    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert len(alerts) == 1
    assert phantom_key in alerts[0].message


@pytest.mark.asyncio
async def test_record_walkthrough_e2e_evidence_workday_slug_routes_to_live_criterion(
    db_session,
):
    """Legacy slug 'c-workday-session-lifecycle' routes to the registered c-5214996f criterion.

    When a VersionAcceptanceCriterion exists with the canonical key 'c-5214996f',
    a walkthrough result posted under the legacy slug must write covered-tier
    evidence against that criterion. Because c-5214996f is the meta criterion, the
    batch must also carry its required sub-criteria (passing) so the meta gate is
    satisfied — resolving the alias BEFORE the meta-gate check is the behaviour
    under test (a partial run posting only the alias would be gated out; see the
    dedicated gate tests). Expect unmatched_keys==[] and covered meta evidence.
    """
    project = await _make_project_with_member(db_session, name="Walk-Flow5-Alias-E2E")
    sha = "dd" * 20
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"status": "scoring", "candidate_sha": sha},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Register the meta criterion under the canonical alias-target key, plus its
    # required sub-criteria so the meta gate can legitimately pass.
    canonical_key = "c-5214996f"
    for ck, txt in (
        (canonical_key, "Workday click-to-talk session lifecycle (re-walk)"),
        ("c-dfdc9d82", "Workday session sub-criterion A"),
        ("c-f52fd975", "Workday session sub-criterion B"),
    ):
        db_session.add(VersionAcceptanceCriterion(
            project_id=project.id,
            release_version_id=version.id,
            criterion_key=ck,
            text=txt,
            status="active",
        ))
    await db_session.commit()
    crit = (await db_session.execute(
        select(VersionAcceptanceCriterion).where(
            VersionAcceptanceCriterion.release_version_id == version.id,
            VersionAcceptanceCriterion.criterion_key == canonical_key,
        )
    )).scalars().one()

    legacy_slug = "c-workday-session-lifecycle"
    summary = await ac.record_walkthrough_e2e_evidence(
        db_session,
        version,
        candidate_sha=sha,
        results=[
            {
                "criterion_key": legacy_slug,
                "passed": True,
                "notes": ["Workday toggle → Start → liveness reflects session"],
            },
            {"criterion_key": "c-dfdc9d82", "passed": True, "notes": ["sub A"]},
            {"criterion_key": "c-f52fd975", "passed": True, "notes": ["sub B"]},
        ],
        qualification_attempt_id="attempt-alias-e2e",
    )

    assert summary["status"] == "recorded"
    assert summary["recorded"] == 3, f"Expected 3 recorded; got {summary}"
    assert summary["unmatched_keys"] == [], f"Expected no unmatched; got {summary['unmatched_keys']}"

    evidence = await ac.get_evidence_for_criterion(db_session, crit.id)
    assert len(evidence) == 1
    assert ac.compute_criterion_coverage(evidence) == "covered"

    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert alerts == [], f"Expected no alerts when alias resolves; got {alerts}"


@pytest.mark.asyncio
async def test_record_operator_walkthrough_evidence_workday_slug_routes_to_live_criterion(
    db_session,
):
    """Operator lane: legacy slug 'c-workday-session-lifecycle' routes to c-5214996f criterion.

    Mirrors test_record_walkthrough_e2e_evidence_workday_slug_routes_to_live_criterion
    for the record_operator_walkthrough_evidence path (the one the walkthrough script
    actually POSTs to via the operator-walkthrough-evidence endpoint). Because
    c-5214996f is the meta criterion, the alias resolves BEFORE the meta gate, so the
    batch must also carry the passing sub-criteria for covered meta evidence.
    """
    project = await _make_project_with_member(db_session, name="Walk-Flow5-Alias-Op")
    sha = "ee" * 20
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    canonical_key = "c-5214996f"
    for ck, txt in (
        (canonical_key, "Workday click-to-talk session lifecycle (re-walk)"),
        ("c-dfdc9d82", "Workday session sub-criterion A"),
        ("c-f52fd975", "Workday session sub-criterion B"),
    ):
        db_session.add(VersionAcceptanceCriterion(
            project_id=project.id,
            release_version_id=version.id,
            criterion_key=ck,
            text=txt,
            status="active",
        ))
    await db_session.commit()
    crit = (await db_session.execute(
        select(VersionAcceptanceCriterion).where(
            VersionAcceptanceCriterion.release_version_id == version.id,
            VersionAcceptanceCriterion.criterion_key == canonical_key,
        )
    )).scalars().one()

    legacy_slug = "c-workday-session-lifecycle"
    summary = await ac.record_operator_walkthrough_evidence(
        db_session,
        version,
        candidate_sha=sha,
        results=[
            {
                "criterion_key": legacy_slug,
                "passed": True,
                "notes": ["Workday session started and liveness confirmed"],
            },
            {"criterion_key": "c-dfdc9d82", "passed": True, "notes": ["sub A"]},
            {"criterion_key": "c-f52fd975", "passed": True, "notes": ["sub B"]},
        ],
    )

    assert summary["status"] == "recorded"
    assert summary["recorded"] == 3, f"Expected 3 recorded; got {summary}"
    assert summary["unmatched_keys"] == [], f"Expected no unmatched; got {summary['unmatched_keys']}"

    evidence = await ac.get_evidence_for_criterion(db_session, crit.id)
    assert len(evidence) == 1
    assert ac.compute_criterion_coverage(evidence) == "covered"

    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert alerts == [], f"Expected no alerts when alias resolves; got {alerts}"


@pytest.mark.asyncio
async def test_record_walkthrough_e2e_evidence_workday_alias_gated_when_subcriteria_absent(
    db_session,
):
    """The legacy alias resolves to the meta criterion BEFORE the meta gate runs.

    A partial run that posts ONLY 'c-workday-session-lifecycle' (which resolves to
    c-5214996f) without its required sub-criteria (c-dfdc9d82, c-f52fd975) must be
    gated out: no covered meta evidence is minted from a partial re-walk. Guards the
    alias-bypass fix at acceptance_criteria.py — the comparison resolves the key.
    """
    project = await _make_project_with_member(db_session, name="Walk-Flow5-Alias-Gate-E2E")
    sha = "cd" * 20
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"status": "scoring", "candidate_sha": sha},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    crit = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-5214996f",
        text="Workday click-to-talk session lifecycle (re-walk)",
        status="active",
    )
    db_session.add(crit)
    await db_session.commit()
    await db_session.refresh(crit)

    summary = await ac.record_walkthrough_e2e_evidence(
        db_session,
        version,
        candidate_sha=sha,
        results=[
            {"criterion_key": "c-workday-session-lifecycle", "passed": True, "notes": ["partial"]},
        ],
        qualification_attempt_id="attempt-alias-gate-e2e",
    )

    assert summary["recorded"] == 0, f"Alias must not bypass meta gate; got {summary}"
    assert summary["skipped_not_passed"] == 1
    assert await ac.get_evidence_for_criterion(db_session, crit.id) == []


@pytest.mark.asyncio
async def test_record_operator_walkthrough_evidence_workday_alias_gated_when_subcriteria_absent(
    db_session,
):
    """Operator lane: the alias resolves to c-5214996f before the meta gate.

    Mirrors the e2e gate test for record_operator_walkthrough_evidence — a partial run
    posting only the alias must not mint covered meta evidence.
    """
    project = await _make_project_with_member(db_session, name="Walk-Flow5-Alias-Gate-Op")
    sha = "ce" * 20
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    crit = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-5214996f",
        text="Workday click-to-talk session lifecycle (re-walk)",
        status="active",
    )
    db_session.add(crit)
    await db_session.commit()
    await db_session.refresh(crit)

    summary = await ac.record_operator_walkthrough_evidence(
        db_session,
        version,
        candidate_sha=sha,
        results=[
            {"criterion_key": "c-workday-session-lifecycle", "passed": True, "notes": ["partial"]},
        ],
    )

    assert summary["recorded"] == 0, f"Alias must not bypass meta gate; got {summary}"
    assert summary["skipped_not_passed"] == 1
    assert await ac.get_evidence_for_criterion(db_session, crit.id) == []


@pytest.mark.asyncio
async def test_record_operator_walkthrough_evidence_workday_slug_unmatched_when_no_canonical_criterion(
    db_session,
):
    """Fail-closed: when c-5214996f is not registered, the slug still lands in unmatched_keys."""
    project = await _make_project_with_member(db_session, name="Walk-Flow5-FailClosed")
    sha = "ff" * 20
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    legacy_slug = "c-workday-session-lifecycle"
    summary = await ac.record_operator_walkthrough_evidence(
        db_session,
        version,
        candidate_sha=sha,
        results=[
            {"criterion_key": legacy_slug, "passed": True, "notes": "test"},
        ],
    )

    assert summary["recorded"] == 0
    assert summary["unmatched_keys"] == [legacy_slug]

    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].event_type == "release.walkthrough_key_unmatched"
    assert legacy_slug in alerts[0].message

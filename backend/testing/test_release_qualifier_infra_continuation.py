"""Retained immutable-attempt infra-continuation regression tests."""

from __future__ import annotations

import contextlib

import pytest

from app import release_version as rv
from app.models import Alert, Project


def _shared_session_factory(db_session):
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


# ===========================================================================
# Cross-SHA infra-failure streak accumulation — cap fires on moving automain
# ===========================================================================


@pytest.mark.asyncio
async def test_infra_failure_cap_fires_when_candidate_sha_advances(db_session, mocker):
    """The crash-loop escalation Alert must fire even when the candidate SHA
    advances on every cycle (moving automain), which was the production blind spot.

    Phase-1b made the redrive prefer the per-dimension QualificationDimensionResult
    streak over the attempt-level ``consecutive_infra_errors`` counter.  But the
    dimension-result row is keyed by ``candidate_sha``, so a new SHA created a
    fresh row with streak=1, and the cap (3) was never reached.

    The fix: ``_record_infra_dimension_unavailable`` now carries the streak from
    the most recent ``unavailable`` row at ANY prior SHA when no row exists for the
    current SHA, so the per-dimension streak accumulates across advancing SHAs.

    Acceptance evidence: task fix(qualify): crash-loop escalation cap never fires
    on moving automain — test_infra_failure_cap_fires_when_candidate_sha_advances.
    """
    import uuid as _uuid

    from app.models import QualificationDimensionResult
    from app.release_qualifier import _record_infra_dimension_unavailable
    from app.release_qualifier_redrive import (
        _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
        _redrive_infra_failure_qualification,
    )
    from sqlalchemy import select as _select

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    # SHA-1: simulate (_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS - 1) prior infra
    # failures on dimension "migration_apply" at an OLD candidate SHA.
    sha_old = "aabbccdd" * 5
    prior_streak = _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS - 1
    dim_row_old = QualificationDimensionResult(
        id=_uuid.uuid4(),
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha=sha_old,
        dimension="migration_apply",
        status="unavailable",
        detail={
            "infra_error": "migration_apply_check_unavailable",
            "infra_failure_signature": "migration_apply:timeout",
            "consecutive_infra_errors": prior_streak,
        },
    )
    db_session.add(dim_row_old)
    await db_session.commit()

    # SHA-2: one more infra failure on a NEW candidate SHA.  The fix should
    # carry the prior streak forward, so the returned streak == cap.
    sha_new = "11223344" * 5
    new_streak = await _record_infra_dimension_unavailable(
        db_session,
        version,
        sha_new,
        "migration_apply",
        infra_error="migration_apply_check_unavailable",
        signature="migration_apply:timeout",
    )
    assert new_streak == _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS, (
        f"streak must carry forward from old SHA: expected "
        f"{_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS}, got {new_streak}"
    )

    # Now seed last_qualification so the redrive sees status=infra_failure and
    # the per-dimension ledger row reflects the accumulated streak.
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": sha_new,
        "error": "migration_apply_check_unavailable",
        "failing_dimension": "migration_apply",
        "infra_failure_signature": "migration_apply:timeout",
        "consecutive_infra_errors": prior_streak,  # attempt-level still 1 behind
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # The redrive must hit the cap (reading the dim-ledger row with streak==cap)
    # and raise an operator Alert, even though the attempt-level counter is below.
    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is False, "redrive must be blocked when dimension streak reaches cap"

    alerts = (
        (
            await db_session.execute(
                _select(Alert).where(Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1, (
        "exactly one crash-loop Alert must fire when cap is reached via dim streak"
    )
    assert "migration_apply" in alerts[0].message, (
        "alert message must identify the looping dimension"
    )


@pytest.mark.asyncio
async def test_infra_failure_dim_streak_carries_across_sha_advance(db_session):
    """``_record_infra_dimension_unavailable`` carries the streak from the most
    recent unavailable row at a DIFFERENT candidate SHA when no row exists for
    the current SHA — verifying the cross-SHA carry-forward directly.
    """
    import uuid as _uuid

    from app.models import QualificationDimensionResult
    from app.release_qualifier import _record_infra_dimension_unavailable

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    sha_a = "aaaabbbb" * 5
    sha_b = "ccccdddd" * 5

    # Plant two rows at sha_a with streak=2, and one row at sha_b from a DIFFERENT
    # dimension (must not bleed into the carry-forward).
    db_session.add(
        QualificationDimensionResult(
            id=_uuid.uuid4(),
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha=sha_a,
            dimension="audit",
            status="unavailable",
            detail={"consecutive_infra_errors": 2},
        )
    )
    db_session.add(
        QualificationDimensionResult(
            id=_uuid.uuid4(),
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha=sha_b,
            dimension="ci",  # different dimension — must not bleed
            status="unavailable",
            detail={"consecutive_infra_errors": 99},
        )
    )
    await db_session.commit()

    # Record a new infra failure for dimension "audit" at sha_b (no existing row).
    returned_streak = await _record_infra_dimension_unavailable(
        db_session,
        version,
        sha_b,
        "audit",
        infra_error="audit_spawn_failed",
        signature="audit:timeout",
    )

    # Must carry forward streak=2 from sha_a, so new streak = 3.
    assert returned_streak == 3, (
        f"cross-SHA carry-forward failed: expected 3, got {returned_streak}"
    )

    # The "ci" dimension's 99-streak must NOT have polluted "audit".
    from sqlalchemy import select as _select

    new_row = (
        (
            await db_session.execute(
                _select(QualificationDimensionResult).where(
                    QualificationDimensionResult.release_version_id == version.id,
                    QualificationDimensionResult.candidate_sha == sha_b,
                    QualificationDimensionResult.dimension == "audit",
                )
            )
        )
        .scalars()
        .one()
    )
    assert new_row.detail["consecutive_infra_errors"] == 3, (
        "persisted streak must be 3 (carry-forward from sha_a), not 99 from ci"
    )

"""Coverage for the ``ReleaseVersion.candidate_sha`` write path.

``candidate_sha`` is a dedicated column and, since the authorship gate stopped
falling back to the mutable ``last_qualification["candidate_sha"]`` JSONB, it is
the SINGLE authoritative input to a hard promotion blocker
(``candidate_operator_authorship_evidence_missing``).  ``run_qualification`` is
the only writer, so the stamp itself needs direct coverage:

  * a NEW attempt stamps the freshly-resolved integration-branch HEAD;
  * a CONTINUED attempt whose candidate ADVANCED re-stamps to the new head —
    this is the semantics that matters, because everything else in the run (the
    diff range, the dimension rows, ``last_qualification["candidate_sha"]``)
    uses the live value re-resolved on every call.  A frozen pin would measure
    base..STALE_head in the authorship gate while the promotion candidate is
    base..NEW_head;
  * the skip guards (wrong_phase / already_pending) do NOT stamp — those paths
    evaluate no candidate, and run_qualification's contract says a fired guard
    has no side effects;
  * a pre-migration row with candidate_sha NULL yields ``(base, None)``, and a
    requalification refills the column.  (That NULL range driving the hard
    ``candidate_operator_authorship_evidence_missing`` blocker is asserted in
    test_release_commit_authorship.py, which does not stub the measurement
    helper the way this suite's autouse fixture does.)
"""
from _release_qualifier_testhelpers import *  # noqa: F403

from app.release_commit_authorship import candidate_authorship_range


_AUTOMAIN_OLD = "a1d0" * 10
_AUTOMAIN_NEW = "b2e9" * 10


def _patch_spawn_preconditions(mocker, sha):
    """Let run_qualification reach the stamp: pin automain HEAD and clear the
    absorbed-target / promotion-enabled gates that sit before the spawn."""
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=sha))
    mocker.patch(
        "app.release_qualifier._is_target_absorbed",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=True,
    )


@pytest.mark.asyncio
async def test_new_attempt_stamps_freshly_resolved_candidate(db_session, mocker):
    """A new attempt writes the integration-branch HEAD it just resolved."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    assert version.candidate_sha is None, "precondition: column starts empty"

    _patch_spawn_preconditions(mocker, _AUTOMAIN_OLD)

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert result["status"] == "spawned", result
    assert result["candidate_sha"] == _AUTOMAIN_OLD
    assert version.candidate_sha == _AUTOMAIN_OLD
    # The hard gate's range must agree with the candidate the run evaluated.
    assert candidate_authorship_range(version) == (version.base_sha, _AUTOMAIN_OLD)


@pytest.mark.asyncio
async def test_continued_attempt_restamps_when_candidate_advanced(
    db_session, mocker
):
    """REGRESSION: a CONTINUED attempt whose candidate advanced must re-stamp.

    ``candidate_sha`` is re-resolved live from integration-branch HEAD on every
    call, including a continued attempt (release_qualifier_redrive passes
    ``qualification_attempt_id`` explicitly, and re-drive exists precisely to do
    real work "when the inputs changed").  If the column were pinned only on a
    new attempt, candidate_authorship_range() — the sole input to the hard
    authorship gate — would return the STALE head while the diff range and the
    dimension rows scored the NEW one.
    """
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    _patch_spawn_preconditions(mocker, _AUTOMAIN_OLD)
    first = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    await db_session.refresh(version)
    assert first["status"] == "spawned", first
    attempt_id = first["qualification_attempt_id"]
    assert version.candidate_sha == _AUTOMAIN_OLD

    # The prior attempt's children are terminal / the record is cleared so the
    # in-flight guard does not fire, then automain advances.
    version.last_qualification = None
    db_session.add(version)
    await db_session.commit()
    _patch_spawn_preconditions(mocker, _AUTOMAIN_NEW)

    second = await rq.run_qualification(
        db_session,
        project.id,
        version,
        explicit_request=True,
        qualification_attempt_id=attempt_id,
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert second["status"] == "spawned", second
    assert second["candidate_sha"] == _AUTOMAIN_NEW
    assert version.candidate_sha == _AUTOMAIN_NEW, (
        "continued attempt kept a STALE candidate_sha; the authorship gate would "
        "measure a different range than the run's own diff/dimension rows"
    )
    assert version.last_qualification["candidate_sha"] == _AUTOMAIN_NEW
    # The one invariant that matters: no gate disagrees with another.
    assert candidate_authorship_range(version)[1] == second["candidate_sha"]


@pytest.mark.asyncio
async def test_wrong_phase_guard_does_not_stamp(db_session, mocker):
    """A fired skip guard evaluates no candidate, so it must not re-point the
    column (run_qualification's contract: a guard fires with no side effects)."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    _patch_spawn_preconditions(mocker, _AUTOMAIN_OLD)
    await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    await db_session.refresh(version)
    assert version.candidate_sha == _AUTOMAIN_OLD

    # Leave stabilize; automain advances underneath.
    rv.set_version_phase(version, "build", reasoning="test: phase flip")
    version.last_qualification = None
    db_session.add(version)
    await db_session.commit()
    _patch_spawn_preconditions(mocker, _AUTOMAIN_NEW)

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert result["status"] == "wrong_phase", result
    assert version.candidate_sha == _AUTOMAIN_OLD, (
        "a skipped run re-pointed candidate_sha; the guards must have no side effects"
    )


@pytest.mark.asyncio
async def test_already_pending_guard_does_not_stamp(db_session, mocker):
    """The in-flight guard must likewise leave the column untouched."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    _patch_spawn_preconditions(mocker, _AUTOMAIN_OLD)
    await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    await db_session.refresh(version)
    assert version.candidate_sha == _AUTOMAIN_OLD

    # Children from the first spawn are still live => in-flight.
    mocker.patch(
        "app.release_qualifier._qualification_in_flight",
        new=AsyncMock(return_value=True),
    )
    _patch_spawn_preconditions(mocker, _AUTOMAIN_NEW)

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert result["status"] == "already_pending", result
    assert version.candidate_sha == _AUTOMAIN_OLD


@pytest.mark.asyncio
async def test_requalification_refills_a_pre_migration_null_candidate_sha(
    db_session, mocker
):
    """A row written before migration 0761 has candidate_sha NULL.

    candidate_authorship_range() then returns ``(base, None)`` — which drives the
    hard ``candidate_operator_authorship_evidence_missing`` blocker for the first
    finalize after deploy (asserted against the unstubbed helpers in
    test_release_commit_authorship.py) rather than a silently-substituted range.
    The block is self-clearing: a requalification refills the column.

    The planted ``last_qualification["candidate_sha"]`` must NOT rescue the null
    column — that fallback is precisely what this PR removed.
    """
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.base_sha = "base" * 10
    version.last_qualification = {"candidate_sha": "c" * 40}
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    assert version.candidate_sha is None
    assert candidate_authorship_range(version) == ("base" * 10, None), (
        "a stale last_qualification candidate leaked back into the gate's range"
    )

    version.last_qualification = None
    db_session.add(version)
    await db_session.commit()
    _patch_spawn_preconditions(mocker, _AUTOMAIN_NEW)

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert result["status"] == "spawned", result
    assert candidate_authorship_range(version) == ("base" * 10, _AUTOMAIN_NEW)

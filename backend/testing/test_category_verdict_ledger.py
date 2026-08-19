"""Tests for the RE-GRADEABLE category/residual verdict ledger.

Exercises :mod:`app.category_verdict_ledger`:

  * upsert_category_verdict OVERWRITES the prior row on a re-grade of the same
    ``(version_id, candidate_sha, category_key)`` (no accumulation);
  * re-grading one category leaves another category's verdict untouched
    (within-candidate isolation);
  * is_candidate_fully_met is met IFF every ACTIVE acceptance criterion has a
    ``met`` verdict for the candidate AND the residual gap is clean; a missing
    verdict is fail-closed (NOT met); a regressed verdict flips it back to NOT
    met; a retracted criterion is not required; an EMPTY active set (never
    authored, or the last criterion retracted) is also NOT met — never
    vacuously fully met;
  * upsert_residual / get_residual round-trip and clear;
  * a DIRECTION-scoped grade records verdicts keyed on ``direction_id``,
    isolates cleanly from the version scope, opens a direction-keyed
    objective_gap completion-debt row on a not_met/partial verdict and resolves
    it on ``met``, and direction-scoped ``is_candidate_fully_met`` is
    fail-closed and honors an explicit rubric.
"""

from __future__ import annotations

import uuid

import pytest
from sqlmodel import select

from app import category_verdict_ledger as cvl
from app import completion_debt
from app import release_version as rv
from app.models import CompletionDebtEntry, Project, VersionAcceptanceCriterion


async def _setup(db_session):
    """Create a project + one open release version with an empty rubric.

    ``open_version`` seeds the default operator-authorship criterion on every
    new version. These tests exercise the ledger's pure met/residual logic
    against rubrics they author themselves, so retract the seeded criterion
    (retracted criteria are excluded from the active set — proven separately
    by test_retracted_criterion_not_required) to start from a clean slate.
    """
    project = Project(name="cvl-test", user_id="cvl-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    seeded = (
        await db_session.execute(
            select(VersionAcceptanceCriterion).where(
                VersionAcceptanceCriterion.release_version_id == version.id
            )
        )
    ).scalars().all()
    for criterion in seeded:
        criterion.status = "retracted"
        db_session.add(criterion)
    await db_session.commit()
    return project, version


async def _add_criterion(db_session, project, version, key, *, status="active"):
    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=key,
        text=f"criterion {key}",
        status=status,
        created_by="test",
    )
    db_session.add(criterion)
    await db_session.commit()
    return criterion


@pytest.mark.asyncio
async def test_upsert_overwrites_on_regrade(db_session):
    """A re-grade of the same key rewrites the row in place — one row, new values."""
    _project, version = await _setup(db_session)
    sha = "a" * 40

    first = await cvl.upsert_category_verdict(
        db_session,
        version_id=version.id,
        candidate_sha=sha,
        category_key="c-1",
        status="not_met",
        tier="machine",
        gap="missing thing",
        citations=["src/foo.py"],
        grader_run_id="run-1",
    )
    await db_session.commit()
    first_id = first.id

    second = await cvl.upsert_category_verdict(
        db_session,
        version_id=version.id,
        candidate_sha=sha,
        category_key="c-1",
        status="met",
        tier="judged",
        gap=None,
        citations={"pr": 42},
        grader_run_id="run-2",
    )
    await db_session.commit()

    # Same row overwritten, not a second appended row.
    assert second.id == first_id
    verdicts = await cvl.get_category_verdicts(db_session, version.id, sha)
    assert len(verdicts) == 1
    row = verdicts[0]
    assert row.id == first_id
    assert row.status == "met"
    assert row.tier == "judged"
    assert row.gap is None
    assert row.citations == {"pr": 42}
    assert row.grader_run_id == "run-2"


@pytest.mark.asyncio
async def test_within_candidate_isolation(db_session):
    """Re-grading one category must not touch another category's verdict."""
    _project, version = await _setup(db_session)
    sha = "b" * 40

    await cvl.upsert_category_verdict(
        db_session,
        version_id=version.id,
        candidate_sha=sha,
        category_key="c-1",
        status="met",
        tier="machine",
        gap=None,
        citations=None,
        grader_run_id="r1",
    )
    c2 = await cvl.upsert_category_verdict(
        db_session,
        version_id=version.id,
        candidate_sha=sha,
        category_key="c-2",
        status="not_met",
        tier="machine",
        gap="c-2 gap",
        citations=None,
        grader_run_id="r1",
    )
    await db_session.commit()
    c2_id = c2.id

    # Re-grade ONLY c-1.
    await cvl.upsert_category_verdict(
        db_session,
        version_id=version.id,
        candidate_sha=sha,
        category_key="c-1",
        status="partial",
        tier="judged",
        gap="c-1 partial",
        citations=None,
        grader_run_id="r2",
    )
    await db_session.commit()

    by_key = {
        v.category_key: v
        for v in await cvl.get_category_verdicts(db_session, version.id, sha)
    }
    assert len(by_key) == 2
    # c-1 changed.
    assert by_key["c-1"].status == "partial"
    # c-2 is completely untouched.
    assert by_key["c-2"].id == c2_id
    assert by_key["c-2"].status == "not_met"
    assert by_key["c-2"].gap == "c-2 gap"


@pytest.mark.asyncio
async def test_is_candidate_fully_met(db_session):
    """Met IFF every active criterion is met AND residual is clean; fail-closed."""
    project, version = await _setup(db_session)
    sha = "c" * 40
    await _add_criterion(db_session, project, version, "c-1")
    await _add_criterion(db_session, project, version, "c-2")

    # No verdicts at all — active criteria unmet — fail-closed.
    assert await cvl.is_candidate_fully_met(db_session, version.id, sha) is False

    # Only c-1 met; c-2 still has no verdict — still NOT met.
    await cvl.upsert_category_verdict(
        db_session,
        version_id=version.id,
        candidate_sha=sha,
        category_key="c-1",
        status="met",
        tier="machine",
        gap=None,
        citations=None,
        grader_run_id="r1",
    )
    await db_session.commit()
    assert await cvl.is_candidate_fully_met(db_session, version.id, sha) is False

    # Both met, no residual — MET.
    await cvl.upsert_category_verdict(
        db_session,
        version_id=version.id,
        candidate_sha=sha,
        category_key="c-2",
        status="met",
        tier="machine",
        gap=None,
        citations=None,
        grader_run_id="r1",
    )
    await db_session.commit()
    assert await cvl.is_candidate_fully_met(db_session, version.id, sha) is True

    # A residual uncaptured gap blocks even with all categories met.
    await cvl.upsert_residual(db_session, version.id, sha, "an uncaptured aspect")
    await db_session.commit()
    assert await cvl.is_candidate_fully_met(db_session, version.id, sha) is False

    # Clearing the residual restores MET.
    await cvl.upsert_residual(db_session, version.id, sha, None)
    await db_session.commit()
    assert await cvl.is_candidate_fully_met(db_session, version.id, sha) is True

    # Regressing c-2 back to not_met flips the candidate to NOT met (the whole
    # point of a re-gradeable, non-monotonic store).
    await cvl.upsert_category_verdict(
        db_session,
        version_id=version.id,
        candidate_sha=sha,
        category_key="c-2",
        status="not_met",
        tier="machine",
        gap="regressed",
        citations=None,
        grader_run_id="r2",
    )
    await db_session.commit()
    assert await cvl.is_candidate_fully_met(db_session, version.id, sha) is False


@pytest.mark.asyncio
async def test_zero_active_criteria_is_not_vacuously_fully_met(db_session):
    """An empty acceptance rubric is NOT vacuously fully met.

    With zero ACTIVE criteria and a clean residual the "every active is met"
    clause is vacuously satisfied — but a version with no authored acceptance
    contract (never authored, or the last criterion retracted/deleted) must not
    count as fully met: the sweep/trigger fire the coverage_complete redrive
    straight off this verdict, so certifying it would be a promotion path
    carrying zero objective acceptance evidence.  Fail closed.
    """
    project, version = await _setup(db_session)
    sha = "g" * 40

    # Never authored: no criteria at all, no residual — NOT fully met.
    assert await cvl.is_candidate_fully_met(db_session, version.id, sha) is False

    # A residual gap with no criteria is likewise not met (residual leg).
    await cvl.upsert_residual(db_session, version.id, sha, "uncaptured aspect")
    await db_session.commit()
    assert await cvl.is_candidate_fully_met(db_session, version.id, sha) is False

    # Sanity: a real (non-empty) active rubric with every criterion met IS met.
    crit = await _add_criterion(db_session, project, version, "c-1", status="active")
    await cvl.upsert_category_verdict(
        db_session,
        version_id=version.id,
        candidate_sha=sha,
        category_key="c-1",
        status="met",
        tier="machine",
        gap=None,
        citations=None,
        grader_run_id="r1",
    )
    await cvl.upsert_residual(db_session, version.id, sha, None)
    await db_session.commit()
    assert await cvl.is_candidate_fully_met(db_session, version.id, sha) is True

    # Retract the last criterion → zero ACTIVE criteria again — NOT fully met
    # even though the (now-retracted) criterion carries a met verdict.  This is
    # the deletion path: wiping the acceptance rubric cannot silently re-open
    # promotion.
    crit.status = "retracted"
    await db_session.commit()
    assert await cvl.is_candidate_fully_met(db_session, version.id, sha) is False


@pytest.mark.asyncio
async def test_retracted_criterion_not_required(db_session):
    """A non-active (retracted) criterion is excluded from the met check."""
    project, version = await _setup(db_session)
    sha = "d" * 40
    await _add_criterion(db_session, project, version, "c-1", status="active")
    await _add_criterion(db_session, project, version, "c-2", status="retracted")

    # Only the active criterion c-1 needs a met verdict; c-2 is retracted.
    await cvl.upsert_category_verdict(
        db_session,
        version_id=version.id,
        candidate_sha=sha,
        category_key="c-1",
        status="met",
        tier="machine",
        gap=None,
        citations=None,
        grader_run_id="r1",
    )
    await db_session.commit()
    assert await cvl.is_candidate_fully_met(db_session, version.id, sha) is True


@pytest.mark.asyncio
async def test_residual_roundtrip_and_isolation_by_candidate(db_session):
    """Residual round-trips per candidate; absent → None; upsert overwrites."""
    _project, version = await _setup(db_session)
    sha_a = "e" * 40
    sha_b = "f" * 40

    # Absent residual reads as None.
    assert await cvl.get_residual(db_session, version.id, sha_a) is None

    await cvl.upsert_residual(db_session, version.id, sha_a, "gap A")
    await db_session.commit()
    assert await cvl.get_residual(db_session, version.id, sha_a) == "gap A"
    # A different candidate is unaffected.
    assert await cvl.get_residual(db_session, version.id, sha_b) is None

    # Upsert overwrites the same candidate's residual.
    await cvl.upsert_residual(db_session, version.id, sha_a, "gap A revised")
    await db_session.commit()
    assert await cvl.get_residual(db_session, version.id, sha_a) == "gap A revised"


@pytest.mark.asyncio
async def test_verdicts_are_isolated_by_candidate_sha(db_session):
    """Verdicts for one candidate SHA are not returned for another."""
    _project, version = await _setup(db_session)
    sha_a = uuid.uuid4().hex[:40]
    sha_b = uuid.uuid4().hex[:40]

    await cvl.upsert_category_verdict(
        db_session,
        version_id=version.id,
        candidate_sha=sha_a,
        category_key="c-1",
        status="met",
        tier="machine",
        gap=None,
        citations=None,
        grader_run_id="r1",
    )
    await db_session.commit()

    assert len(await cvl.get_category_verdicts(db_session, version.id, sha_a)) == 1
    assert await cvl.get_category_verdicts(db_session, version.id, sha_b) == []


@pytest.mark.asyncio
async def test_direction_scoped_objective_grade_records_scoped_verdicts_and_debt(
    db_session,
):
    """A direction-scoped objective grade is fully isolated from the version scope.

    The blind judge can grade a DIRECTION objective (a campaign with no release
    version) and the verdict persists keyed on ``direction_id``:

      * upsert records a verdict with ``version_id IS NULL`` and the direction id;
      * a re-grade of the same ``(direction_id, candidate_sha, category_key)``
        OVERWRITES in place (one row, latest write wins);
      * the same ``candidate_sha`` + ``category_key`` graded under BOTH scopes
        yields two fully-isolated rows (version-scoped vs direction-scoped);
      * a ``not_met``/``partial`` direction verdict opens an ``objective_gap``
        completion-debt row keyed on the DIRECTION scope
        (``objective_gap:direction:<direction_id>:<category_key>``) carrying the
        direction id in ``detail``; a later ``met`` re-grade resolves it;
      * direction-scoped ``is_candidate_fully_met`` is fail-closed (an empty
        rubric is never fully met) and honors the explicit
        ``active_category_keys`` rubric;
      * direction-scoped residual round-trips without colliding with the version
        residual for the same candidate.
    """
    _project, version = await _setup(db_session)
    sha = uuid.uuid4().hex[:40]
    direction_id = "d-1a2b3c4d5e6f"
    key = completion_debt.objective_gap_direction_dedupe_key(direction_id, "c-1")

    # 1. A direction-scoped grade records a verdict keyed on direction_id with
    #    NO version scope.
    first = await cvl.upsert_category_verdict(
        db_session,
        direction_id=direction_id,
        candidate_sha=sha,
        category_key="c-1",
        status="not_met",
        tier="machine",
        gap="missing the acceptance behavior",
        citations=None,
        grader_run_id="dir-run-1",
    )
    await db_session.commit()
    assert first.version_id is None
    assert first.direction_id == direction_id
    direction_verdicts = await cvl.get_category_verdicts(
        db_session, None, sha, direction_id=direction_id
    )
    assert len(direction_verdicts) == 1
    assert direction_verdicts[0].id == first.id
    assert direction_verdicts[0].status == "not_met"

    # 2. The unmet direction verdict opens the direction-keyed objective_gap
    #    debt row — isolated from the version key namespace — with the direction
    #    id in its detail and version_id explicitly None.
    entry = (
        await db_session.execute(
            select(CompletionDebtEntry).where(CompletionDebtEntry.dedupe_key == key)
        )
    ).scalar_one()
    assert entry.kind == completion_debt.KIND_OBJECTIVE_GAP
    assert entry.status == completion_debt.STATUS_OPEN
    assert entry.detail["direction_id"] == direction_id
    assert entry.detail["version_id"] is None
    assert entry.detail["category_key"] == "c-1"

    # 3. A re-grade of the same direction + category OVERWRITES in place.
    second = await cvl.upsert_category_verdict(
        db_session,
        direction_id=direction_id,
        candidate_sha=sha,
        category_key="c-1",
        status="met",
        tier="judged",
        gap=None,
        citations={"pr": 7},
        grader_run_id="dir-run-2",
    )
    await db_session.commit()
    assert second.id == first.id
    assert len(
        await cvl.get_category_verdicts(db_session, None, sha, direction_id=direction_id)
    ) == 1

    # 4. The met re-grade resolves the direction-keyed objective_gap debt.
    entry = (
        await db_session.execute(
            select(CompletionDebtEntry).where(CompletionDebtEntry.dedupe_key == key)
        )
    ).scalar_one()
    assert entry.status == completion_debt.STATUS_RESOLVED

    # 5. The version scope is fully isolated: the same category_key + sha graded
    #    under the version scope is a SEPARATE row, invisible to the direction
    #    scope and vice versa.
    version_row = await cvl.upsert_category_verdict(
        db_session,
        version_id=version.id,
        candidate_sha=sha,
        category_key="c-1",
        status="not_met",
        tier="machine",
        gap="version-gap",
        citations=None,
        grader_run_id="ver-run-1",
    )
    await db_session.commit()
    assert version_row.id != first.id
    assert version_row.direction_id is None
    version_verdicts = await cvl.get_category_verdicts(db_session, version.id, sha)
    assert len(version_verdicts) == 1
    assert version_verdicts[0].id == version_row.id
    direction_verdicts = await cvl.get_category_verdicts(
        db_session, None, sha, direction_id=direction_id
    )
    assert len(direction_verdicts) == 1
    assert direction_verdicts[0].id == first.id

    # 6. Direction-scoped is_candidate_fully_met honors the explicit rubric and
    #    is fail-closed on an empty/absent one.
    assert (
        await cvl.is_candidate_fully_met(
            db_session,
            None,
            sha,
            direction_id=direction_id,
            active_category_keys={"c-1"},
        )
        is True
    )
    # A rubric key with no met verdict in this scope is NOT fully met.
    assert (
        await cvl.is_candidate_fully_met(
            db_session,
            None,
            sha,
            direction_id=direction_id,
            active_category_keys={"c-1", "c-2"},
        )
        is False
    )
    # An EMPTY rubric is never vacuously fully met (fail-closed), exactly like
    # the version path's zero-active-criteria rule.
    assert (
        await cvl.is_candidate_fully_met(
            db_session,
            None,
            sha,
            direction_id=direction_id,
            active_category_keys=set(),
        )
        is False
    )

    # 7. Direction-scoped residual round-trips without colliding with the version
    #    residual for the same candidate.
    await cvl.upsert_residual(
        db_session, None, sha, "direction residual", direction_id=direction_id
    )
    await db_session.commit()
    assert (
        await cvl.get_residual(db_session, None, sha, direction_id=direction_id)
        == "direction residual"
    )
    assert await cvl.get_residual(db_session, version.id, sha) is None
    await cvl.upsert_residual(
        db_session, None, sha, None, direction_id=direction_id
    )
    await db_session.commit()
    assert (
        await cvl.get_residual(db_session, None, sha, direction_id=direction_id)
        is None
    )


@pytest.mark.asyncio
async def test_scope_validation_requires_exactly_one_scope(db_session):
    """Ledger accessors reject a missing OR a double-specified scope.

    Every entry is scoped to exactly one of ``version_id`` or ``direction_id``;
    neither-supplied (the legacy call could never apply to a direction, but an
    accidentally-dropped kwarg must not fall through to a version-less row) and
    both-supplied are programmer errors, raised before any DB work.
    """
    sha = uuid.uuid4().hex[:40]

    with pytest.raises(ValueError):
        await cvl.upsert_category_verdict(
            db_session,
            candidate_sha=sha,
            category_key="c-1",
            status="met",
            tier="machine",
            gap=None,
            citations=None,
            grader_run_id="r1",
        )
    with pytest.raises(ValueError):
        await cvl.upsert_category_verdict(
            db_session,
            version_id=uuid.uuid4(),
            direction_id="d-1a2b3c4d5e6f",
            candidate_sha=sha,
            category_key="c-1",
            status="met",
            tier="machine",
            gap=None,
            citations=None,
            grader_run_id="r1",
        )
    with pytest.raises(ValueError):
        await cvl.get_category_verdicts(db_session, None, sha)
    with pytest.raises(ValueError):
        await cvl.is_candidate_fully_met(
            db_session, None, sha, active_category_keys={"c-1"}
        )
    with pytest.raises(ValueError):
        await cvl.upsert_residual(db_session, None, sha, "gap")
    with pytest.raises(ValueError):
        await cvl.get_residual(db_session, None, sha)

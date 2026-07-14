"""Tests for the RE-GRADEABLE category/residual verdict ledger.

Exercises :mod:`app.category_verdict_ledger`:

  * upsert_category_verdict OVERWRITES the prior row on a re-grade of the same
    ``(version_id, candidate_sha, category_key)`` (no accumulation);
  * re-grading one category leaves another category's verdict untouched
    (within-candidate isolation);
  * is_candidate_fully_met is met IFF every ACTIVE acceptance criterion has a
    ``met`` verdict for the candidate AND the residual gap is clean; a missing
    verdict is fail-closed (NOT met); a regressed verdict flips it back to NOT
    met; a retracted criterion is not required;
  * upsert_residual / get_residual round-trip and clear.
"""

from __future__ import annotations

import uuid

import pytest

from app import category_verdict_ledger as cvl
from app import release_version as rv
from app.models import Project, VersionAcceptanceCriterion


async def _setup(db_session):
    """Create a project + one open release version."""
    project = Project(name="cvl-test", user_id="cvl-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)
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

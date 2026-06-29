"""Tests for the acceptance-criteria accessor module.

Exercises :mod:`app.acceptance_criteria`:

  * create_criterion persists a row with the correct key format and fields;
  * get_criteria_for_version returns criteria ordered by created_at;
  * record_evidence persists an evidence row round-trippable by get_evidence_for_criterion;
  * compute_criterion_coverage returns 'covered', 'partial', or 'uncovered';
  * get_version_coverage_summary returns correct totals and per-criterion detail;
  * GET /version-lifecycle includes criterion_coverage on current_version and history entries.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app import acceptance_criteria as ac
from app.models import CriterionEvidence, Project, ProjectMember, ReleaseVersion

_TEST_USER_ID = "test-user-123"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _make_project(db_session, name: str = "AC-Test") -> Project:
    project = Project(name=name, user_id="ac-test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _make_version(db_session, project: Project) -> ReleaseVersion:
    version = ReleaseVersion(project_id=project.id, version_number=1)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return version


# ---------------------------------------------------------------------------
# generate_criterion_key
# ---------------------------------------------------------------------------


def test_generate_criterion_key_format():
    key = ac.generate_criterion_key()
    assert key.startswith("c-")
    hex_part = key[2:]
    assert len(hex_part) == 8
    assert all(c in "0123456789abcdef" for c in hex_part)


def test_generate_criterion_key_uniqueness():
    assert ac.generate_criterion_key() != ac.generate_criterion_key()


# ---------------------------------------------------------------------------
# create_criterion / get_criteria_for_version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_fetch_criterion(db_session):
    """create_criterion persists a row fetchable via get_criteria_for_version."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="All unit tests pass",
        created_by="strategist",
    )

    assert criterion.id is not None
    assert criterion.text == "All unit tests pass"
    assert criterion.created_by == "strategist"
    assert criterion.release_version_id == version.id
    assert criterion.project_id == project.id
    # key format
    assert criterion.criterion_key.startswith("c-")
    assert len(criterion.criterion_key) == 10  # 'c-' + 8 hex chars

    fetched = await ac.get_criteria_for_version(db_session, version.id)
    assert len(fetched) == 1
    assert fetched[0].id == criterion.id


@pytest.mark.asyncio
async def test_get_criteria_for_version_ordered(db_session):
    """get_criteria_for_version returns rows ordered by created_at ascending."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    c1 = await ac.create_criterion(db_session, project.id, version.id, "First")
    c2 = await ac.create_criterion(db_session, project.id, version.id, "Second")

    fetched = await ac.get_criteria_for_version(db_session, version.id)
    assert len(fetched) == 2
    assert fetched[0].id == c1.id
    assert fetched[1].id == c2.id


# ---------------------------------------------------------------------------
# record_evidence / get_evidence_for_criterion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_and_fetch_evidence(db_session):
    """record_evidence persists a row round-trippable via get_evidence_for_criterion."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "CI green")

    evidence = await ac.record_evidence(
        db_session,
        criterion_id=criterion.id,
        project_id=project.id,
        source_kind="task_merge",
        source_id="task-uuid-abc123",
        detail={"pr": 42},
    )

    assert evidence.id is not None
    assert evidence.source_kind == "task_merge"
    assert evidence.source_id == "task-uuid-abc123"
    assert evidence.detail == {"pr": 42}
    assert evidence.criterion_id == criterion.id

    fetched = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(fetched) == 1
    assert fetched[0].id == evidence.id


# ---------------------------------------------------------------------------
# compute_criterion_coverage (pure function)
# ---------------------------------------------------------------------------


def _make_evidence(source_kind: str) -> CriterionEvidence:
    return CriterionEvidence(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        criterion_id=uuid.uuid4(),
        source_kind=source_kind,
    )


def test_coverage_uncovered():
    assert ac.compute_criterion_coverage([]) == "uncovered"


def test_coverage_partial():
    """Evidence with source_kind='operator' (not in covered set) → 'partial'."""
    ev = _make_evidence("operator")
    assert ac.compute_criterion_coverage([ev]) == "partial"


def test_coverage_covered_task_merge():
    ev = _make_evidence("task_merge")
    assert ac.compute_criterion_coverage([ev]) == "covered"


def test_coverage_covered_reachability():
    ev = _make_evidence("reachability")
    assert ac.compute_criterion_coverage([ev]) == "covered"


def test_coverage_covered_mixed():
    """A mix including a covered kind → 'covered'."""
    evs = [_make_evidence("operator"), _make_evidence("task_merge")]
    assert ac.compute_criterion_coverage(evs) == "covered"


# ---------------------------------------------------------------------------
# get_version_coverage_summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coverage_summary(db_session):
    """Two criteria: one covered (task_merge evidence), one uncovered → correct counts."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    c1 = await ac.create_criterion(db_session, project.id, version.id, "Tests pass")
    c2 = await ac.create_criterion(db_session, project.id, version.id, "Docs updated")

    # Record evidence only for c1
    await ac.record_evidence(
        db_session,
        criterion_id=c1.id,
        project_id=project.id,
        source_kind="task_merge",
    )

    summary = await ac.get_version_coverage_summary(db_session, version.id)

    assert summary["total"] == 2
    assert summary["covered"] == 1
    assert summary["partial"] == 0
    assert summary["uncovered"] == 1

    by_key = {c["criterion_key"]: c for c in summary["criteria"]}
    assert by_key[c1.criterion_key]["coverage"] == "covered"
    assert by_key[c1.criterion_key]["evidence_count"] == 1
    assert by_key[c2.criterion_key]["coverage"] == "uncovered"
    assert by_key[c2.criterion_key]["evidence_count"] == 0


@pytest.mark.asyncio
async def test_coverage_summary_empty(db_session):
    """A version with no criteria returns a zero-count summary."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    summary = await ac.get_version_coverage_summary(db_session, version.id)

    assert summary["total"] == 0
    assert summary["covered"] == 0
    assert summary["partial"] == 0
    assert summary["uncovered"] == 0
    assert summary["criteria"] == []


# ---------------------------------------------------------------------------
# GET /version-lifecycle includes criterion_coverage
# ---------------------------------------------------------------------------


async def _make_project_with_member(db_session, name: str = "AC-Endpoint-Test") -> Project:
    """Create a project owned by the test user with a ProjectMember row.

    Required for endpoints that call _get_user_project_with_role.
    """
    project = Project(name=name, user_id=_TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    member = ProjectMember(project_id=project.id, user_id=_TEST_USER_ID, role="owner")
    db_session.add(member)
    await db_session.commit()
    return project


_LIFECYCLE_PATCHES = (
    patch(
        "app.release_qualifier.compute_promotion_readiness",
        new=AsyncMock(return_value={"ready": False, "reason": "test"}),
    ),
    patch(
        "app.release_qualifier.resolve_lifecycle_audit_evidence",
        new=AsyncMock(
            return_value={"state": "none", "latest_audit": None, "stale_audit": None}
        ),
    ),
    patch("app.release_version.count_merged_since", new=AsyncMock(return_value=0)),
)


class TestVersionLifecycleCriterionCoverage:
    """GET /version-lifecycle includes criterion_coverage on current_version and history."""

    @pytest.mark.asyncio
    async def test_current_version_with_criteria_returns_coverage(
        self, client: AsyncClient, db_session
    ):
        """An open version with criteria defined returns a non-null criterion_coverage
        dict with the correct shape."""
        project = await _make_project_with_member(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="build",
            status="open",
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        c1 = await ac.create_criterion(
            db_session, project.id, version.id, "All tests pass"
        )
        await ac.record_evidence(
            db_session,
            criterion_id=c1.id,
            project_id=project.id,
            source_kind="task_merge",
        )
        c2 = await ac.create_criterion(
            db_session, project.id, version.id, "Docs updated"
        )

        with _LIFECYCLE_PATCHES[0], _LIFECYCLE_PATCHES[1], _LIFECYCLE_PATCHES[2]:
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        coverage = resp.json()["current_version"]["criterion_coverage"]
        assert coverage is not None
        assert coverage["total"] == 2
        assert coverage["covered"] == 1
        assert coverage["uncovered"] == 1
        assert coverage["partial"] == 0
        assert len(coverage["criteria"]) == 2
        by_key = {c["criterion_key"]: c for c in coverage["criteria"]}
        assert by_key[c1.criterion_key]["coverage"] == "covered"
        assert by_key[c1.criterion_key]["evidence_count"] == 1
        assert by_key[c2.criterion_key]["coverage"] == "uncovered"
        assert by_key[c2.criterion_key]["evidence_count"] == 0

    @pytest.mark.asyncio
    async def test_current_version_no_criteria_returns_zero_totals(
        self, client: AsyncClient, db_session
    ):
        """An open version with no criteria returns criterion_coverage with total=0
        and an empty criteria list (not null)."""
        project = await _make_project_with_member(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="build",
            status="open",
        )
        db_session.add(version)
        await db_session.commit()

        with _LIFECYCLE_PATCHES[0], _LIFECYCLE_PATCHES[1], _LIFECYCLE_PATCHES[2]:
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        coverage = resp.json()["current_version"]["criterion_coverage"]
        assert coverage is not None
        assert coverage["total"] == 0
        assert coverage["criteria"] == []

    @pytest.mark.asyncio
    async def test_history_entries_carry_criterion_coverage(
        self, client: AsyncClient, db_session
    ):
        """recent_versions entries carry a criterion_coverage dict (possibly with
        total=0) when the history entry has a session."""
        project = await _make_project_with_member(db_session)
        open_version = ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="build",
            status="open",
        )
        closed_version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="promoted",
        )
        db_session.add_all([open_version, closed_version])
        await db_session.commit()
        await db_session.refresh(closed_version)

        await ac.create_criterion(
            db_session, project.id, closed_version.id, "Shipped criterion"
        )

        with _LIFECYCLE_PATCHES[0], _LIFECYCLE_PATCHES[1], _LIFECYCLE_PATCHES[2]:
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        recent = resp.json()["recent_versions"]
        assert len(recent) == 1
        coverage = recent[0]["criterion_coverage"]
        assert coverage is not None
        assert coverage["total"] == 1
        assert coverage["uncovered"] == 1
        assert len(coverage["criteria"]) == 1
        assert coverage["criteria"][0]["text"] == "Shipped criterion"

"""Tests for the acceptance-criteria accessor module.

Exercises :mod:`app.acceptance_criteria`:

  * create_criterion persists a row with the correct key format and fields;
  * get_criteria_for_version returns criteria ordered by created_at;
  * record_evidence persists an evidence row round-trippable by get_evidence_for_criterion;
  * compute_criterion_coverage returns 'covered', 'partial', or 'uncovered';
  * get_version_coverage_summary returns correct totals and per-criterion detail;
  * get_criterion_by_key returns the matching criterion or None;
  * _apply_new_acceptance_criteria creates criteria from the strategist path;
  * criterion_ids are propagated from direction to spawned task;
  * test_criterion_evidence_recorded_on_task_merge verifies merge evidence flow;
  * GET /version-lifecycle includes criterion_coverage on current_version and history entries;
  * operator_attestation source_kind satisfies coverage and the attest endpoint records it.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app import acceptance_criteria as ac
from app.models import CriterionEvidence, Direction, Project, ProjectMember, ReleaseVersion, Task, TaskStatus

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
# count_criteria_for_version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_criteria_for_version_empty(db_session):
    """count_criteria_for_version returns 0 when no criteria exist."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    count = await ac.count_criteria_for_version(db_session, version.id)
    assert count == 0


@pytest.mark.asyncio
async def test_count_criteria_for_version_with_entries(db_session):
    """count_criteria_for_version returns the correct count after inserts."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    await ac.create_criterion(db_session, project.id, version.id, "Criterion A")
    await ac.create_criterion(db_session, project.id, version.id, "Criterion B")
    count = await ac.count_criteria_for_version(db_session, version.id)
    assert count == 2


@pytest.mark.asyncio
async def test_count_criteria_for_version_isolated_to_version(db_session):
    """count_criteria_for_version only counts criteria for the specified version."""
    project = await _make_project(db_session)
    version_a = await _make_version(db_session, project)
    version_b = ReleaseVersion(project_id=project.id, version_number=2)
    db_session.add(version_b)
    await db_session.commit()
    await db_session.refresh(version_b)

    await ac.create_criterion(db_session, project.id, version_a.id, "Only in A")
    await ac.create_criterion(db_session, project.id, version_b.id, "In B 1")
    await ac.create_criterion(db_session, project.id, version_b.id, "In B 2")

    assert await ac.count_criteria_for_version(db_session, version_a.id) == 1
    assert await ac.count_criteria_for_version(db_session, version_b.id) == 2


# ---------------------------------------------------------------------------
# _apply_new_acceptance_criteria cap enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_new_acceptance_criteria_enforces_cap(db_session):
    """_apply_new_acceptance_criteria stops staging once MAX_CRITERIA_PER_VERSION is reached
    and does not raise."""
    from app.strategic_completion import _apply_new_acceptance_criteria
    from app import release_version as rv

    project = Project(
        name="AC-Cap-Enforce",
        user_id="strat-cap-user",
        vision={"purpose": "test", "revision": 1},
        problem_model={"intent": {"current_focus": "focus"}, "revision": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    # Pre-fill to (MAX - 2) so only 2 slots remain
    headroom = 2
    for i in range(ac.MAX_CRITERIA_PER_VERSION - headroom):
        await ac.create_criterion(db_session, project.id, version.id, f"Pre-existing {i}")

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "r"}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Submit 5 new criteria; only 2 should be staged (fills exactly to cap)
    criteria_texts = [f"New criterion {i}" for i in range(5)]
    await _apply_new_acceptance_criteria(criteria_texts, task, project, db_session, path="test")

    fetched = await ac.get_criteria_for_version(db_session, version.id)
    assert len(fetched) == ac.MAX_CRITERIA_PER_VERSION
    new_texts = {c.text for c in fetched if c.text.startswith("New criterion")}
    assert len(new_texts) == headroom


@pytest.mark.asyncio
async def test_apply_new_acceptance_criteria_already_at_cap_is_noop(db_session):
    """_apply_new_acceptance_criteria is a no-op (no raises) when already at cap."""
    from app.strategic_completion import _apply_new_acceptance_criteria
    from app import release_version as rv

    project = Project(
        name="AC-AtCap",
        user_id="strat-atcap-user",
        vision={"purpose": "test", "revision": 1},
        problem_model={"intent": {"current_focus": "focus"}, "revision": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    # Fill to exactly MAX
    for i in range(ac.MAX_CRITERIA_PER_VERSION):
        await ac.create_criterion(db_session, project.id, version.id, f"Full {i}")

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "r"}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Should not raise and should not add any criteria
    await _apply_new_acceptance_criteria(["Should not appear"], task, project, db_session, path="test")

    count = await ac.count_criteria_for_version(db_session, version.id)
    assert count == ac.MAX_CRITERIA_PER_VERSION


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


@pytest.mark.asyncio
async def test_record_evidence_duplicate_is_noop(db_session):
    """Duplicate non-null evidence keys are collapsed by the DB constraint."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "CI green")

    await ac.record_evidence(
        db_session,
        criterion_id=criterion.id,
        project_id=project.id,
        source_kind="task_merge",
        source_id="task-uuid-abc123",
        detail={"pr": 42},
    )

    await ac.record_evidence(
        db_session,
        criterion_id=criterion.id,
        project_id=project.id,
        source_kind="task_merge",
        source_id="task-uuid-abc123",
        detail={"pr": 43},
    )

    assert await ac.count_criteria_for_version(db_session, version.id) == 1

    fetched = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(fetched) == 1
    assert fetched[0].source_kind == "task_merge"
    assert fetched[0].source_id == "task-uuid-abc123"
    assert fetched[0].detail == {"pr": 42}


# ---------------------------------------------------------------------------
# compute_criterion_coverage (pure function)
# ---------------------------------------------------------------------------


def _make_evidence(source_kind: str, detail: dict | None = None) -> CriterionEvidence:
    return CriterionEvidence(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        criterion_id=uuid.uuid4(),
        source_kind=source_kind,
        detail=detail,
    )


def test_coverage_uncovered():
    assert ac.compute_criterion_coverage([]) == "uncovered"


def test_coverage_partial():
    """Evidence with source_kind='operator' (not in covered set) → 'partial'."""
    ev = _make_evidence("operator")
    assert ac.compute_criterion_coverage([ev]) == "partial"


def test_coverage_covered_mixed():
    """A mix including a proven task_merge → 'covered'."""
    evs = [_make_evidence("operator"), _make_evidence("task_merge", detail={"proven": True})]
    assert ac.compute_criterion_coverage(evs) == "covered"


def test_unproven_or_unreachable_merge_does_not_mark_criterion_covered():
    """A task_merge evidence without proven=True must NOT mark a criterion covered.

    A bare merge badge (no detail), detail with proven=False, or detail with proven
    absent must all yield 'partial', not 'covered'.  Only a merge that carries
    detail={"proven": True} (acceptance-proof-backed) or a 'reachability' record
    (automain ancestry confirmed) satisfies the covered gate.
    """
    # Bare task_merge badge with no detail at all → partial
    assert ac.compute_criterion_coverage([_make_evidence("task_merge")]) == "partial"

    # task_merge with explicit proven=False → partial
    assert (
        ac.compute_criterion_coverage(
            [_make_evidence("task_merge", detail={"proven": False})]
        )
        == "partial"
    )

    # task_merge with proven absent from detail → partial
    assert (
        ac.compute_criterion_coverage(
            [_make_evidence("task_merge", detail={"source": "ci_completion"})]
        )
        == "partial"
    )

    # task_merge with proven=True (real merge-test verdict) → covered
    assert (
        ac.compute_criterion_coverage(
            [_make_evidence("task_merge", detail={"proven": True})]
        )
        == "covered"
    )

    # reachability always counts as covered regardless of detail
    assert ac.compute_criterion_coverage([_make_evidence("reachability")]) == "covered"


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

    # Record proven evidence only for c1
    await ac.record_evidence(
        db_session,
        criterion_id=c1.id,
        project_id=project.id,
        source_kind="task_merge",
        detail={"proven": True},
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
    assert summary["stuck_count"] == 0
    assert summary["criteria"] == []


# ---------------------------------------------------------------------------
# stuck-criterion diagnosis (_classify_stuck_reasons / get_version_coverage_summary)
# ---------------------------------------------------------------------------


def _make_direction(project_id, key_suffix, criterion_keys, status):
    return Direction(
        project_id=project_id,
        direction_key=f"d-{key_suffix}",
        data={"id": f"d-{key_suffix}", "text": "dir", "status": status},
        status=status,
        text="dir",
        criterion_keys=list(criterion_keys),
    )


@pytest.mark.asyncio
async def test_stuck_reason_no_mapping(db_session):
    """Uncovered criterion with no Direction referencing its key → 'no_mapping'."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    c1 = await ac.create_criterion(db_session, project.id, version.id, "Unmapped")

    summary = await ac.get_version_coverage_summary(db_session, version.id)

    by_key = {c["criterion_key"]: c for c in summary["criteria"]}
    assert by_key[c1.criterion_key]["coverage"] == "uncovered"
    assert by_key[c1.criterion_key]["stuck_reason"] == "no_mapping"
    assert summary["stuck_count"] == 1


@pytest.mark.asyncio
async def test_stuck_reason_direction_retired(db_session):
    """Uncovered criterion whose only mapping is retired → 'direction_retired'."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    c1 = await ac.create_criterion(db_session, project.id, version.id, "Retired-mapped")

    for i, status in enumerate(("expired", "retired", "superseded", "completed")):
        db_session.add(
            _make_direction(project.id, f"ret{i}", [c1.criterion_key], status)
        )
    await db_session.commit()

    summary = await ac.get_version_coverage_summary(db_session, version.id)

    by_key = {c["criterion_key"]: c for c in summary["criteria"]}
    assert by_key[c1.criterion_key]["stuck_reason"] == "direction_retired"
    assert summary["stuck_count"] == 1


@pytest.mark.asyncio
async def test_stuck_reason_none_when_active_direction(db_session):
    """Uncovered criterion with an active mapping → stuck_reason None, not counted."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    c1 = await ac.create_criterion(db_session, project.id, version.id, "Active-mapped")

    db_session.add(_make_direction(project.id, "act1", [c1.criterion_key], "active"))
    await db_session.commit()

    summary = await ac.get_version_coverage_summary(db_session, version.id)

    by_key = {c["criterion_key"]: c for c in summary["criteria"]}
    assert by_key[c1.criterion_key]["coverage"] == "uncovered"
    assert by_key[c1.criterion_key]["stuck_reason"] is None
    assert summary["stuck_count"] == 0


@pytest.mark.asyncio
async def test_stuck_reason_mixed_directions_active_wins(db_session):
    """One retired + one active mapping → not stuck (active wins)."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    c1 = await ac.create_criterion(db_session, project.id, version.id, "Mixed-mapped")

    db_session.add(_make_direction(project.id, "mret", [c1.criterion_key], "retired"))
    db_session.add(_make_direction(project.id, "mact", [c1.criterion_key], "active"))
    await db_session.commit()

    summary = await ac.get_version_coverage_summary(db_session, version.id)

    by_key = {c["criterion_key"]: c for c in summary["criteria"]}
    assert by_key[c1.criterion_key]["stuck_reason"] is None
    assert summary["stuck_count"] == 0


@pytest.mark.asyncio
async def test_covered_criterion_has_no_stuck_reason(db_session):
    """A covered criterion carries stuck_reason None regardless of direction status."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    c1 = await ac.create_criterion(db_session, project.id, version.id, "Covered")
    await ac.record_evidence(
        db_session,
        criterion_id=c1.id,
        project_id=project.id,
        source_kind="task_merge",
        detail={"proven": True},
    )
    # Even with a retired mapping, a covered criterion is never stuck.
    db_session.add(_make_direction(project.id, "cret", [c1.criterion_key], "retired"))
    await db_session.commit()

    summary = await ac.get_version_coverage_summary(db_session, version.id)

    by_key = {c["criterion_key"]: c for c in summary["criteria"]}
    assert by_key[c1.criterion_key]["coverage"] == "covered"
    assert by_key[c1.criterion_key]["stuck_reason"] is None
    assert summary["stuck_count"] == 0


@pytest.mark.asyncio
async def test_stuck_count_aggregates_across_criteria(db_session):
    """stuck_count counts only the stuck uncovered criteria across a mix."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    c_no_map = await ac.create_criterion(db_session, project.id, version.id, "NoMap")
    c_retired = await ac.create_criterion(db_session, project.id, version.id, "Retired")
    c_active = await ac.create_criterion(db_session, project.id, version.id, "Active")
    c_covered = await ac.create_criterion(db_session, project.id, version.id, "Covered")

    db_session.add(
        _make_direction(project.id, "agret", [c_retired.criterion_key], "superseded")
    )
    db_session.add(
        _make_direction(project.id, "agact", [c_active.criterion_key], "active")
    )
    await ac.record_evidence(
        db_session,
        criterion_id=c_covered.id,
        project_id=project.id,
        source_kind="reachability",
    )
    await db_session.commit()

    summary = await ac.get_version_coverage_summary(db_session, version.id)

    by_key = {c["criterion_key"]: c for c in summary["criteria"]}
    assert by_key[c_no_map.criterion_key]["stuck_reason"] == "no_mapping"
    assert by_key[c_retired.criterion_key]["stuck_reason"] == "direction_retired"
    assert by_key[c_active.criterion_key]["stuck_reason"] is None
    assert by_key[c_covered.criterion_key]["stuck_reason"] is None
    # no_map + retired are stuck; active and covered are not.
    assert summary["stuck_count"] == 2


# ---------------------------------------------------------------------------
# get_criterion_by_key, criterion_ids propagation, and merge evidence recording
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_criterion_by_key_found(db_session):
    """get_criterion_by_key returns the criterion when the key exists."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "Found it")

    result = await ac.get_criterion_by_key(db_session, version.id, criterion.criterion_key)

    assert result is not None
    assert result.id == criterion.id
    assert result.text == "Found it"


@pytest.mark.asyncio
async def test_get_criterion_by_key_missing(db_session):
    """get_criterion_by_key returns None when the key does not exist."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    result = await ac.get_criterion_by_key(db_session, version.id, "c-notexist")

    assert result is None


# ---------------------------------------------------------------------------
# _apply_new_acceptance_criteria (strategist authoring path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategist_criterion_authoring(db_session):
    """_apply_new_acceptance_criteria creates criteria for the open version."""
    from app.strategic_completion import _apply_new_acceptance_criteria
    from app import release_version as rv

    project = Project(
        name="AC-Strategist",
        user_id="strat-user",
        vision={"purpose": "test", "revision": 1},
        problem_model={"intent": {"current_focus": "focus"}, "revision": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "r"}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    criteria_texts = ["All unit tests pass", "No P0 bugs open", "Deploy to staging succeeds"]
    await _apply_new_acceptance_criteria(criteria_texts, task, project, db_session, path="test")

    fetched = await ac.get_criteria_for_version(db_session, version.id)
    assert len(fetched) == 3
    fetched_texts = {c.text for c in fetched}
    assert "All unit tests pass" in fetched_texts
    assert "No P0 bugs open" in fetched_texts
    assert "Deploy to staging succeeds" in fetched_texts
    for c in fetched:
        assert c.created_by == "strategist"


@pytest.mark.asyncio
async def test_strategist_criterion_authoring_skips_duplicates(db_session):
    """_apply_new_acceptance_criteria skips criteria with text already present."""
    from app.strategic_completion import _apply_new_acceptance_criteria
    from app import release_version as rv

    project = Project(
        name="AC-DedupTest",
        user_id="strat-user2",
        vision={"purpose": "test", "revision": 1},
        problem_model={"intent": {"current_focus": "focus"}, "revision": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    # Pre-create one criterion
    await ac.create_criterion(db_session, project.id, version.id, "Existing criterion")

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "r"}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Submit the existing text plus a new one
    await _apply_new_acceptance_criteria(
        ["Existing criterion", "Brand new criterion"],
        task,
        project,
        db_session,
        path="test",
    )

    fetched = await ac.get_criteria_for_version(db_session, version.id)
    assert len(fetched) == 2  # only one new criterion created
    fetched_texts = {c.text for c in fetched}
    assert "Existing criterion" in fetched_texts
    assert "Brand new criterion" in fetched_texts


@pytest.mark.asyncio
async def test_strategist_criterion_authoring_no_op_when_no_open_version(db_session):
    """_apply_new_acceptance_criteria is a no-op when no open version exists."""
    from app.strategic_completion import _apply_new_acceptance_criteria

    project = Project(
        name="AC-NoVersion",
        user_id="strat-user3",
        vision={"purpose": "test", "revision": 1},
        problem_model={"intent": {"current_focus": "focus"}, "revision": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "r"}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Should not raise even with no open version
    await _apply_new_acceptance_criteria(
        ["Test criterion"], task, project, db_session, path="test"
    )


# ---------------------------------------------------------------------------
# criterion_ids propagation from direction to spawned task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_criterion_ids_propagated_from_direction(db_session):
    """get_direction_row returns the ORM row with criterion_keys accessible."""
    from app.directions_repo import get_direction_row

    project = await _make_project(db_session, "CriterionPropagation")
    version = await _make_version(db_session, project)

    c1 = await ac.create_criterion(db_session, project.id, version.id, "Criterion A")
    c2 = await ac.create_criterion(db_session, project.id, version.id, "Criterion B")

    direction = Direction(
        project_id=project.id,
        direction_key="d-testdir1",
        data={"id": "d-testdir1", "text": "Test direction", "status": "active"},
        status="active",
        text="Test direction",
        criterion_keys=[c1.criterion_key, c2.criterion_key],
    )
    db_session.add(direction)
    await db_session.commit()
    await db_session.refresh(direction)

    row = await get_direction_row(db_session, project.id, "d-testdir1")

    assert row is not None
    assert set(row.criterion_keys) == {c1.criterion_key, c2.criterion_key}


@pytest.mark.asyncio
async def test_spawn_task_copies_criterion_ids_from_direction(db_session):
    """criterion_ids on the spawned Task match the direction's criterion_keys.

    Exercises the propagation logic in admin_agent_ops.spawn_task: when a direction
    row has criterion_keys, those keys are copied verbatim into the new Task's
    criterion_ids field.
    """
    from app.directions_repo import get_direction_row

    project = await _make_project(db_session, "SpawnPropagation")
    version = await _make_version(db_session, project)

    c1 = await ac.create_criterion(db_session, project.id, version.id, "Feature complete")
    c2 = await ac.create_criterion(db_session, project.id, version.id, "Rollback tested")

    direction = Direction(
        project_id=project.id,
        direction_key="d-spawn-prop",
        data={"id": "d-spawn-prop", "text": "Spawn propagation test", "status": "active"},
        status="active",
        text="Spawn propagation test",
        criterion_keys=[c1.criterion_key, c2.criterion_key],
    )
    db_session.add(direction)
    await db_session.commit()
    await db_session.refresh(direction)

    # Replicate exactly what admin_agent_ops.spawn_task does:
    # look up the direction row and copy criterion_keys to the new task.
    dir_row = await get_direction_row(db_session, project.id, "d-spawn-prop")
    task_criterion_ids: list[str] = []
    if dir_row is not None and dir_row.criterion_keys:
        task_criterion_ids = list(dir_row.criterion_keys)

    spawned = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "implement feature"}],
        context={"direction_id": "d-spawn-prop"},
        criterion_ids=task_criterion_ids,
    )
    db_session.add(spawned)
    await db_session.commit()
    await db_session.refresh(spawned)

    assert set(spawned.criterion_ids) == {c1.criterion_key, c2.criterion_key}


@pytest.mark.asyncio
async def test_requeued_task_preserves_criterion_ids(db_session, mocker):
    """_requeue_parent_task copies criterion_ids onto the cloned retry task."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "Retry")

    parent = Task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 0},
        payloads=[{"type": "research", "prompt": "parent"}],
    )
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
        criterion_ids=[criterion.criterion_key],
        latest_status_message="boom",
        payloads=[{"type": "create", "prompt": "child"}],
        context={"last_error": "boom"},
    )
    db_session.add(parent)
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(parent)
    await db_session.refresh(child)

    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.classify_failure", return_value="CODE_ERROR")
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)

    from app.task_lifecycle_requeue import _requeue_parent_task

    await _requeue_parent_task(parent, db_session)

    cloned = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id,
                    Task.status == TaskStatus.QUEUED,
                )
            )
        )
        .scalars()
        .one()
    )

    assert cloned.criterion_ids == [criterion.criterion_key]
    assert child.criterion_ids == [criterion.criterion_key]


@pytest.mark.asyncio
async def test_get_direction_row_returns_none_for_missing(db_session):
    """get_direction_row returns None when the direction does not exist."""
    from app.directions_repo import get_direction_row

    project = await _make_project(db_session, "MissingDir")

    row = await get_direction_row(db_session, project.id, "d-doesnotexist")
    assert row is None


# ---------------------------------------------------------------------------
# test_criterion_evidence_recorded_on_task_merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_criterion_evidence_recorded_on_task_merge(db_session):
    """CriterionEvidence rows are created for each criterion_id after a task merge.

    Exercises the merge-evidence path: given a Task with criterion_ids pointing
    to real VersionAcceptanceCriterion rows, calling get_criterion_by_key and
    record_evidence (as ci_completion does after apply_merged_landed_signal)
    produces CriterionEvidence rows with source_kind='task_merge'.
    """
    project = await _make_project(db_session, "MergeEvidence")
    version = await _make_version(db_session, project)

    c1 = await ac.create_criterion(db_session, project.id, version.id, "Tests green")
    c2 = await ac.create_criterion(db_session, project.id, version.id, "Docs updated")

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "implement X"}],
        context={"direction_id": "d-abc"},
        criterion_ids=[c1.criterion_key, c2.criterion_key],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Simulate the ci_completion evidence-recording loop.
    ci_pr_number = 42
    for ckey in task.criterion_ids:
        crit = await ac.get_criterion_by_key(db_session, version.id, ckey)
        assert crit is not None, f"criterion_key {ckey} must resolve"
        await ac.record_evidence(
            db_session,
            criterion_id=crit.id,
            project_id=task.project_id,
            source_kind="task_merge",
            source_id=str(task.id),
            detail={"pr": ci_pr_number},
        )

    # Both criteria must now have task_merge evidence.
    ev1 = await ac.get_evidence_for_criterion(db_session, c1.id)
    ev2 = await ac.get_evidence_for_criterion(db_session, c2.id)

    assert len(ev1) == 1, "c1 must have exactly one evidence record"
    assert ev1[0].source_kind == "task_merge"
    assert ev1[0].source_id == str(task.id)
    assert ev1[0].detail == {"pr": ci_pr_number}

    assert len(ev2) == 1, "c2 must have exactly one evidence record"
    assert ev2[0].source_kind == "task_merge"

    # The ci_completion path records a bare badge (detail={"pr": N}) without
    # proven=True, so coverage is 'partial' not 'covered'.  Only
    # record_task_merge_evidence (the acceptance-gate path) records proven=True.
    assert ac.compute_criterion_coverage(ev1) == "partial"
    assert ac.compute_criterion_coverage(ev2) == "partial"

    # Summary must reflect evidence is present but not proven.
    summary = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary["total"] == 2
    assert summary["covered"] == 0
    assert summary["partial"] == 2
    assert summary["uncovered"] == 0


@pytest.mark.asyncio
async def test_autonomous_direction_task_inherits_criterion_ids_and_records_evidence_on_merge(
    db_session,
):
    """Direction.criterion_keys propagate to task.criterion_ids; a proven merge records evidence.

    Exercises the autonomous loop chain:
      1. A Direction row has criterion_keys set (as would be done via
         link-direction endpoint or strategic agent).
      2. A Task carries criterion_ids inherited from that direction (as
         research_completion propagates them at spawn time).
      3. record_task_merge_evidence records CriterionEvidence when the task's
         acceptance verdict is proven=True (the merge-test verdict path).
    """
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    # 1. Author a criterion for the version
    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Autonomous test suite passes",
        created_by="strategist",
    )

    # 2. Create a Direction row with criterion_keys set (simulates what
    #    link-direction endpoint or the strategic agent would do)
    direction_row = Direction(
        project_id=project.id,
        direction_key="d-autotest1",
        data={"id": "d-autotest1", "text": "Improve test coverage", "status": "active"},
        status="active",
        criterion_keys=[criterion.criterion_key],
    )
    db_session.add(direction_row)
    await db_session.commit()

    # 3. Create a Task that inherited criterion_ids from its direction
    #    (mirrors what research_completion._handle_research_completion does at spawn)
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    task = Task(
        project_id=project.id,
        title="Improve test coverage",
        payloads=[{"type": "create", "prompt": "add tests"}],
        context={
            "direction_id": "d-autotest1",
            # Simulate a proven merge-test verdict stamped by the acceptance gate
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "test ran green under verified merge",
            },
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    # 4. Call record_task_merge_evidence — the production function invoked from
    #    refresh_direction_outcomes_after_merge after a task's PR merges
    recorded = await ac.record_task_merge_evidence(db_session, task, project.id)

    assert recorded == 1, "Expected one criterion to be recorded"

    # 5. Verify the evidence is correct and satisfies the 'covered' predicate
    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence) == 2
    assert {e.source_kind for e in evidence} == {"task_merge", "reachability"}
    assert {e.source_id for e in evidence} == {str(task.id)}
    assert all(e.detail == {"proven": True} for e in evidence)

    # The criterion should now be 'covered' (proven task_merge evidence)
    coverage = ac.compute_criterion_coverage(evidence)
    assert coverage == "covered"


@pytest.mark.asyncio
async def test_refresh_after_merge_records_evidence_only_when_proven(db_session):
    """refresh_direction_outcomes_after_merge skips unproven merge verdicts."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.direction_outcomes import refresh_direction_outcomes_after_merge

    project = await _make_project(db_session, "MergeRefreshUnproven")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Merge verdict is proven"
    )

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "implement X"}],
        context={CODE_ACCEPTANCE_VERDICT_KEY: {"proven": False}},
        criterion_ids=[criterion.criterion_key],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await refresh_direction_outcomes_after_merge(task, db_session)

    assert await ac.get_evidence_for_criterion(db_session, criterion.id) == []


@pytest.mark.asyncio
async def test_refresh_after_merge_scopes_to_open_version_and_is_idempotent(
    db_session,
):
    """Merge refresh ignores closed-version keys, and repeated refreshes do not duplicate evidence."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.direction_outcomes import refresh_direction_outcomes_after_merge

    project = await _make_project(db_session, "MergeRefreshScoped")
    closed_version = await _make_version(db_session, project)
    closed_version.status = "promoted"
    db_session.add(closed_version)
    await db_session.commit()

    closed_criterion = await ac.create_criterion(
        db_session, project.id, closed_version.id, "Closed version criterion"
    )

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "implement Y"}],
        context={CODE_ACCEPTANCE_VERDICT_KEY: {"proven": True}},
        criterion_ids=[closed_criterion.criterion_key, "c-missing"],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await refresh_direction_outcomes_after_merge(task, db_session)
    assert await ac.get_evidence_for_criterion(db_session, closed_criterion.id) == []

    reopened_version = ReleaseVersion(project_id=project.id, version_number=2)
    db_session.add(reopened_version)
    await db_session.commit()
    await db_session.refresh(reopened_version)

    open_criterion_a = await ac.create_criterion(
        db_session, project.id, reopened_version.id, "Open criterion A"
    )
    open_criterion_b = await ac.create_criterion(
        db_session, project.id, reopened_version.id, "Open criterion B"
    )
    open_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "implement Y"}],
        context={CODE_ACCEPTANCE_VERDICT_KEY: {"proven": True}},
        criterion_ids=[open_criterion_a.criterion_key, open_criterion_b.criterion_key],
    )
    db_session.add(open_task)
    await db_session.commit()
    await db_session.refresh(open_task)

    first = await ac.record_task_merge_evidence(db_session, open_task, project.id)
    ev_a = await ac.get_evidence_for_criterion(db_session, open_criterion_a.id)
    ev_b = await ac.get_evidence_for_criterion(db_session, open_criterion_b.id)
    assert len(ev_a) == 2
    assert len(ev_b) == 2
    assert {e.source_kind for e in ev_a} == {"task_merge", "reachability"}
    assert {e.source_id for e in ev_a} == {str(open_task.id)}

    second = await ac.record_task_merge_evidence(db_session, open_task, project.id)
    ev_a_again = await ac.get_evidence_for_criterion(db_session, open_criterion_a.id)
    ev_b_again = await ac.get_evidence_for_criterion(db_session, open_criterion_b.id)
    assert len(ev_a_again) == 2
    assert len(ev_b_again) == 2
    assert first == 2
    assert second == 0

    summary = await ac.get_version_coverage_summary(db_session, reopened_version.id)
    assert summary["total"] == 2
    assert summary["covered"] == 2
    assert summary["partial"] == 0
    assert summary["uncovered"] == 0
    by_key = {c["criterion_key"]: c for c in summary["criteria"]}
    assert by_key[open_criterion_a.criterion_key]["evidence_count"] == 2
    assert by_key[open_criterion_b.criterion_key]["evidence_count"] == 2


@pytest.mark.asyncio
async def test_coverage_summary_partial_only(db_session):
    """A version with only non-covered evidence totals as partial."""
    project = await _make_project(db_session, "PartialOnly")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "Partial")

    await ac.record_evidence(
        db_session,
        criterion_id=criterion.id,
        project_id=project.id,
        source_kind="task_merge",
        source_id="task-1",
        detail={"proven": False},
    )

    summary = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary["total"] == 1
    assert summary["covered"] == 0
    assert summary["partial"] == 1
    assert summary["uncovered"] == 0
    assert summary["criteria"][0]["evidence_count"] == 1


# ---------------------------------------------------------------------------
# spawn_task endpoint-level criterion_ids propagation test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_task_endpoint_propagates_criterion_ids(
    client: AsyncClient, db_session
):
    """POST /api/agent/strategic spawn_task propagates direction.criterion_keys to Task.criterion_ids.

    This is an endpoint-level regression test: it calls the real strategic
    handler via HTTP so a code change that stops passing criterion_ids from
    direction → spawned task will fail here, unlike the unit test that
    replicates the internal logic by hand.
    """
    from app.auth import create_agent_token

    # Create project (no member row needed — agent token auth bypasses member check)
    project = Project(name="SpawnEndpointCriterion", user_id="test-user-se")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create an open release version and two criteria
    version = await _make_version(db_session, project)
    c1 = await ac.create_criterion(db_session, project.id, version.id, "CI passes")
    c2 = await ac.create_criterion(db_session, project.id, version.id, "Docs updated")

    # Create a Direction row with criterion_keys AND mirror it in project.directions
    # (spawn_task validates direction_id against project.directions JSONB blob)
    direction_key = "d-spawn-endpoint-crit"
    direction = Direction(
        project_id=project.id,
        direction_key=direction_key,
        data={
            "id": direction_key,
            "text": "Improve stability",
            "status": "active",
        },
        status="active",
        text="Improve stability",
        criterion_keys=[c1.criterion_key, c2.criterion_key],
    )
    db_session.add(direction)
    # Mirror in project.directions so spawn_task's direction_id validation passes
    project.directions = [{"id": direction_key, "text": "Improve stability", "status": "active"}]
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create strategic review task
    strategic_task = Task(
        project_id=project.id,
        title="Strategic review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-spawn-endpoint-crit",
    )
    db_session.add(strategic_task)
    await db_session.commit()
    await db_session.refresh(strategic_task)

    token = create_agent_token(
        str(strategic_task.id),
        "agent-vm-spawn-endpoint-crit",
        task_type="strategic_review",
    )

    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(strategic_task.id),
            "op": "spawn_task",
            "data": {
                "type": "research",
                "prompt": "investigate stability regression",
                "title": "Stability research",
                "direction_id": direction_key,
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["op"] == "spawn_task"

    child_id = uuid.UUID(body["task_id"])
    child = await db_session.get(Task, child_id)
    assert child is not None, "spawned task must be persisted"
    assert set(child.criterion_ids) == {
        c1.criterion_key,
        c2.criterion_key,
    }, f"criterion_ids must match direction.criterion_keys; got {child.criterion_ids}"


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
        # Use source_kind="reachability" which always satisfies _evidence_proves_covered.
        # A bare task_merge without proven=True only gives 'partial', not 'covered'.
        await ac.record_evidence(
            db_session,
            criterion_id=c1.id,
            project_id=project.id,
            source_kind="reachability",
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


# ---------------------------------------------------------------------------
# operator_attestation coverage and attest endpoint
# ---------------------------------------------------------------------------


def test_operator_attestation_covers_criterion():
    """operator_attestation source_kind must satisfy _evidence_proves_covered unconditionally.

    This is the named acceptance test for the operator-attestation waiver feature.
    It verifies that an operator_attestation evidence record flips coverage to
    'covered' regardless of what detail it carries, mirroring the reachability
    behaviour.  A criterion with only operator_attestation evidence must NOT
    remain 'partial' or 'uncovered'.
    """
    ev_no_detail = _make_evidence("operator_attestation")
    assert ac._evidence_proves_covered(ev_no_detail) is True

    ev_with_detail = _make_evidence("operator_attestation", detail={"reason": "accepted"})
    assert ac._evidence_proves_covered(ev_with_detail) is True

    assert ac.compute_criterion_coverage([ev_no_detail]) == "covered"
    assert ac.compute_criterion_coverage([ev_with_detail]) == "covered"

    # A mix of non-covering evidence plus operator_attestation must still be covered.
    ev_partial = _make_evidence("task_merge")  # no proven=True → not covering on its own
    assert ac.compute_criterion_coverage([ev_partial, ev_with_detail]) == "covered"


@pytest.mark.asyncio
async def test_attest_endpoint_happy_path(client: AsyncClient, db_session):
    """POST .../attest records operator_attestation evidence and coverage flips to 'covered'."""
    project = await _make_project_with_member(db_session, name="Attest-Happy")
    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Stuck criterion"
    )

    resp = await client.post(
        f"/api/admin/projects/{project.id}/acceptance-criteria/{criterion.criterion_key}/attest",
        json={"reason": "Direction retired; operator confirms criterion is satisfied"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["criterion_key"] == criterion.criterion_key
    assert data["coverage"] == "covered"

    # Verify evidence persisted with the correct source_kind and detail.
    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence) == 1
    assert evidence[0].source_kind == "operator_attestation"
    assert evidence[0].detail == {"reason": "Direction retired; operator confirms criterion is satisfied"}
    assert evidence[0].source_id == _TEST_USER_ID

    # GET coverage summary must now show 'covered'.
    summary = await ac.get_version_coverage_summary(db_session, version.id)
    by_key = {c["criterion_key"]: c for c in summary["criteria"]}
    assert by_key[criterion.criterion_key]["coverage"] == "covered"
    assert summary["covered"] == 1
    assert summary["uncovered"] == 0


@pytest.mark.asyncio
async def test_attest_endpoint_get_criteria_shows_covered(client: AsyncClient, db_session):
    """GET /acceptance-criteria returns coverage='covered' after an attestation."""
    project = await _make_project_with_member(db_session, name="Attest-GetCoverage")
    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    criterion = await ac.create_criterion(db_session, project.id, version.id, "Stuck")

    await client.post(
        f"/api/admin/projects/{project.id}/acceptance-criteria/{criterion.criterion_key}/attest",
        json={"reason": "accepted by operator"},
    )

    resp = await client.get(f"/api/admin/projects/{project.id}/acceptance-criteria")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["covered"] == 1
    assert data["uncovered"] == 0
    by_key = {c["criterion_key"]: c for c in data["criteria"]}
    assert by_key[criterion.criterion_key]["coverage"] == "covered"


@pytest.mark.asyncio
async def test_attest_endpoint_empty_reason_rejected(client: AsyncClient, db_session):
    """POST .../attest with an empty reason returns 400."""
    project = await _make_project_with_member(db_session, name="Attest-EmptyReason")
    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    criterion = await ac.create_criterion(db_session, project.id, version.id, "X")

    resp = await client.post(
        f"/api/admin/projects/{project.id}/acceptance-criteria/{criterion.criterion_key}/attest",
        json={"reason": ""},
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_attest_authorizes_before_body_validation(client: AsyncClient, db_session):
    """Authorization is enforced before body validation.

    A caller without project access calling attest with an empty reason
    should get a 403/404 authorization rejection, not a 400 body error.
    This verifies the check order: authorize first, then validate body.
    """
    # The test user (who created the project) has access, so we need a different
    # authentication context to simulate an unauthorized caller. Create a project
    # that the test user is NOT a member of.

    # Create a project that the test user is NOT a member of
    other_project = Project(name="Attest-OtherProject", user_id="other-owner")
    db_session.add(other_project)
    await db_session.commit()
    await db_session.refresh(other_project)

    other_version = ReleaseVersion(project_id=other_project.id, version_number=1, status="open")
    db_session.add(other_version)
    await db_session.commit()
    await db_session.refresh(other_version)

    other_criterion = await ac.create_criterion(
        db_session, other_project.id, other_version.id, "Y"
    )

    # Call attest on the project where the test user has no access,
    # with an invalid empty reason. Authorization should fail (404) before
    # body validation would fail (400).
    resp = await client.post(
        f"/api/admin/projects/{other_project.id}/acceptance-criteria/{other_criterion.criterion_key}/attest",
        json={"reason": ""},
    )
    # Should get 404 (no project access) not 400 (body error)
    assert resp.status_code == 404, f"Expected 404 for no access, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_attest_endpoint_unknown_criterion_key(client: AsyncClient, db_session):
    """POST .../attest with an unknown criterion_key returns 404."""
    project = await _make_project_with_member(db_session, name="Attest-UnknownKey")
    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()

    resp = await client.post(
        f"/api/admin/projects/{project.id}/acceptance-criteria/c-deadbeef/attest",
        json={"reason": "valid reason"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_attest_endpoint_no_open_version(client: AsyncClient, db_session):
    """POST .../attest returns 409 when the project has no open release version."""
    project = await _make_project_with_member(db_session, name="Attest-NoVersion")

    resp = await client.post(
        f"/api/admin/projects/{project.id}/acceptance-criteria/c-anykey/attest",
        json={"reason": "valid reason"},
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_attest_duplicate_attestation_is_idempotent(client: AsyncClient, db_session):
    """Repeat attestation by the same operator on the same criterion returns 200, not 500.

    The uq_criterionevidence_dedup index (migration 0365) covers operator attestations
    because source_id=user_id is non-null.  A second POST .../attest by the same user
    violates the unique constraint; the endpoint must catch IntegrityError, rollback, and
    return the same idempotent {"status":"ok","criterion_key":...,"coverage":"covered"}
    response instead of surfacing a 500.
    """
    project = await _make_project_with_member(db_session, name="Attest-Dup")
    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Stuck duplicate criterion"
    )

    url = f"/api/admin/projects/{project.id}/acceptance-criteria/{criterion.criterion_key}/attest"

    # First call: creates the evidence row.
    resp1 = await client.post(url, json={"reason": "accepted; direction retired"})
    assert resp1.status_code == 200, resp1.text
    data1 = resp1.json()
    assert data1["status"] == "ok"
    assert data1["criterion_key"] == criterion.criterion_key
    assert data1["coverage"] == "covered"

    # Second call by the same operator: violates uq_criterionevidence_dedup.
    # Must return 200 with the same shape — NOT 500 — because duplicates are a no-op.
    resp2 = await client.post(url, json={"reason": "accepted again"})
    assert resp2.status_code == 200, (
        f"Duplicate attestation must return 200 (idempotent), got {resp2.status_code}: {resp2.text}"
    )
    data2 = resp2.json()
    assert data2["status"] == "ok"
    assert data2["criterion_key"] == criterion.criterion_key
    assert data2["coverage"] == "covered"

    # Verify exactly one evidence row was persisted (the second was a no-op).
    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence) == 1, (
        f"Duplicate attestation must not create a second evidence row; found {len(evidence)}"
    )
    assert evidence[0].source_kind == "operator_attestation"
    assert evidence[0].source_id == _TEST_USER_ID


@pytest.mark.asyncio
async def test_full_pipeline_end_to_end_via_refresh_after_merge(db_session):
    """Full acceptance-criteria pipeline exercised through the real post-merge entry point.

    Covers all four pipeline stages:
      1. Criterion authoring via ac.create_criterion
      2. Direction-to-task propagation (Direction.criterion_keys → Task.criterion_ids)
      3. Merge evidence recording via refresh_direction_outcomes_after_merge (real caller)
      4. Coverage computation via ac.get_version_coverage_summary
    """
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.direction_outcomes import refresh_direction_outcomes_after_merge

    # Stage 1: Criterion authoring
    project = await _make_project(db_session, "E2EPipeline")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Test criterion",
    )

    # Stage 2: Direction-to-task propagation
    direction_row = Direction(
        project_id=project.id,
        direction_key="d-e2e-pipeline",
        data={"id": "d-e2e-pipeline", "text": "E2E pipeline direction", "status": "active"},
        status="active",
        criterion_keys=[criterion.criterion_key],
    )
    db_session.add(direction_row)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="E2E pipeline task",
        payloads=[{"type": "create", "prompt": "implement e2e"}],
        context={
            "direction_id": "d-e2e-pipeline",
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "test ran green under verified merge",
            },
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Stage 3: Merge evidence recording via the REAL post-merge entry point
    await refresh_direction_outcomes_after_merge(task, db_session)

    # Verify evidence rows: task_merge + reachability
    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence) == 2
    assert {e.source_kind for e in evidence} == {"task_merge", "reachability"}

    # Stage 4: Coverage computation
    summary = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary["total"] == 1
    assert summary["covered"] == 1
    assert summary["criteria"][0]["coverage"] == "covered"

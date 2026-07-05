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
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app import acceptance_criteria as ac
from app.operator_attention import build_operator_attention
from app.models import (
    CriterionEvidence,
    Direction,
    Project,
    ProjectMember,
    ReleaseVersion,
    VersionAcceptanceCriterion,
    Task,
    TaskStatus,
)

_TEST_USER_ID = "test-user-123"


# ---------------------------------------------------------------------------
# retired-direction status-set parity
# ---------------------------------------------------------------------------


def test_retired_direction_statuses_parity():
    """All three copies of RETIRED_DIRECTION_STATUSES must remain in sync.

    RETIRED_DIRECTION_STATUSES exists in three places to avoid import cycles:
      - research_direction_state.RETIRED_DIRECTION_STATUSES (canonical)
      - acceptance_criteria._RETIRED_DIRECTION_STATUSES (mirror)
      - reaper_evaluating_sweeps._RETIRED_DIRECTION_STATUSES (mirror)

    Adding a status to the canonical set and forgetting to update the mirrors
    silently breaks stuck-criterion classification (in _classify_stuck_reasons)
    and the reaper's retired-direction sweep guard. This test prevents divergence.
    """
    from app import research_direction_state as rds
    from app import reaper_evaluating_sweeps as res

    canonical = rds.RETIRED_DIRECTION_STATUSES
    acceptance_mirror = ac._RETIRED_DIRECTION_STATUSES
    reaper_mirror = res._RETIRED_DIRECTION_STATUSES

    assert canonical == acceptance_mirror, (
        f"acceptance_criteria._RETIRED_DIRECTION_STATUSES diverged from canonical: "
        f"canonical={sorted(canonical)}, acceptance={sorted(acceptance_mirror)}"
    )
    assert canonical == reaper_mirror, (
        f"reaper_evaluating_sweeps._RETIRED_DIRECTION_STATUSES diverged from canonical: "
        f"canonical={sorted(canonical)}, reaper={sorted(reaper_mirror)}"
    )


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
# count_active_criteria_for_version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_active_criteria_for_version_excludes_retracted(db_session):
    """count_active_criteria_for_version excludes retracted/superseded rows."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    c1 = await ac.create_criterion(db_session, project.id, version.id, "Active A")
    c2 = await ac.create_criterion(db_session, project.id, version.id, "Active B")
    c3 = await ac.create_criterion(db_session, project.id, version.id, "Will retract")

    # Retract c3 — it should no longer count toward the active cap.
    await ac.retract_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=c3.criterion_key,
        reason="stale",
        retracted_by="test",
    )

    active_count = await ac.count_active_criteria_for_version(db_session, version.id)
    total_count = await ac.count_criteria_for_version(db_session, version.id)

    # Active count excludes the retracted row; total count includes all 3.
    assert active_count == 2
    assert total_count == 3
    # Verify the two remaining active keys are the expected ones.
    _ = c1, c2  # referenced to avoid lint noise


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


@pytest.mark.asyncio
async def test_stuck_criterion_attention_item(db_session):
    """Open-version stuck criteria surface as operator-attention items."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    c_no_map = await ac.create_criterion(
        db_session, project.id, version.id, "Unmapped criterion"
    )
    c_retired = await ac.create_criterion(
        db_session, project.id, version.id, "Retired-mapped criterion"
    )
    c_active = await ac.create_criterion(
        db_session, project.id, version.id, "Active-mapped criterion"
    )
    db_session.add(
        _make_direction(project.id, "ret", [c_retired.criterion_key], "superseded")
    )
    db_session.add(_make_direction(project.id, "act", [c_active.criterion_key], "active"))
    await db_session.commit()

    attention = await build_operator_attention(db_session, project, _TEST_USER_ID)

    assert attention["level"] == "needs_operator"
    assert attention["counts"]["stuck_acceptance_criteria"] == 2
    assert attention["counts"]["needs_operator"] == 2
    items = {item["id"]: item for item in attention["items"]}
    no_map_id = f"version:{version.id}:acceptance_criterion:{c_no_map.criterion_key}"
    retired_id = f"version:{version.id}:acceptance_criterion:{c_retired.criterion_key}"
    active_id = f"version:{version.id}:acceptance_criterion:{c_active.criterion_key}"
    assert no_map_id in items
    assert retired_id in items
    assert active_id not in items
    no_map_item = items[no_map_id]
    retired_item = items[retired_id]
    assert no_map_item["reason"] == "stuck_acceptance_criterion"
    assert no_map_item["stuck_reason"] == "no_mapping"
    assert no_map_item["label"] == "Acceptance criterion is unmapped"
    assert no_map_item["release_version_id"] == str(version.id)
    assert no_map_item["release_version_number"] == 1
    assert no_map_item["target"] == {
        "surface": "project",
        "project_id": str(project.id),
        "section": "manage",
    }
    assert retired_item["stuck_reason"] == "direction_retired"
    assert (
        retired_item["label"]
        == "Acceptance criterion points to a retired direction"
    )


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
            "review_loop": {"pr_number": 77},
            # Simulate a proven merge-test verdict stamped by the acceptance gate
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "test ran green under verified merge",
                "proof_ref": "testing/test_autotest1.py::test_coverage_passes",
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

    assert recorded == 2, "Expected 2 evidence rows (task_merge + reachability) for one criterion"

    # 5. Verify the evidence is correct and satisfies the 'covered' predicate
    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence) == 2
    assert {e.source_kind for e in evidence} == {"task_merge", "reachability"}
    assert {e.source_id for e in evidence} == {str(task.id)}

    # task_merge detail must carry provenance fields (pr_number, direction_key, proof_ref)
    task_merge_ev = next(e for e in evidence if e.source_kind == "task_merge")
    assert task_merge_ev.detail.get("proven") is True
    assert task_merge_ev.detail.get("pr_number") == 77
    assert task_merge_ev.detail.get("direction_key") == "d-autotest1"
    assert task_merge_ev.detail.get("proof_ref") == "testing/test_autotest1.py::test_coverage_passes"

    # reachability row only needs proven=True (no extra provenance fields)
    reachability_ev = next(e for e in evidence if e.source_kind == "reachability")
    assert reachability_ev.detail == {"proven": True}

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
async def test_get_version_coverage_summary_evidence_provenance(db_session):
    """get_version_coverage_summary includes an evidence list on each criterion item.

    Each entry must carry source_kind, source_id, detail, and recorded_at so an
    operator can audit provenance without SQL.  Covered criteria must expose the
    task_merge detail dict including the pr_number, direction_key, and proof_ref
    fields written by record_task_merge_evidence.
    """
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project = await _make_project(db_session, "CoverageSummaryEvidence")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Evidence provenance test criterion"
    )

    direction_row = Direction(
        project_id=project.id,
        direction_key="d-prov-test",
        data={"id": "d-prov-test", "text": "Provenance direction", "status": "active"},
        status="active",
        criterion_keys=[criterion.criterion_key],
    )
    db_session.add(direction_row)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="Provenance task",
        payloads=[{"type": "create", "prompt": "implement provenance"}],
        context={
            "direction_id": "d-prov-test",
            "review_loop": {"pr_number": 99},
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "proof_ref": "testing/test_provenance.py::test_it",
            },
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    await ac.record_task_merge_evidence(db_session, task, project.id)

    summary = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary["covered"] == 1

    criteria = summary["criteria"]
    assert len(criteria) == 1
    item = criteria[0]
    assert item["criterion_key"] == criterion.criterion_key
    assert item["coverage"] == "covered"

    evidence_list = item["evidence"]
    assert len(evidence_list) == 2  # task_merge + reachability

    task_merge_entry = next(e for e in evidence_list if e["source_kind"] == "task_merge")
    assert task_merge_entry["source_id"] == str(task.id)
    assert task_merge_entry["detail"].get("proven") is True
    assert task_merge_entry["detail"].get("pr_number") == 99
    assert task_merge_entry["detail"].get("direction_key") == "d-prov-test"
    assert task_merge_entry["detail"].get("proof_ref") == "testing/test_provenance.py::test_it"
    assert task_merge_entry["recorded_at"] is not None  # ISO timestamp string

    reachability_entry = next(e for e in evidence_list if e["source_kind"] == "reachability")
    assert reachability_entry["detail"] == {"proven": True}


@pytest.mark.asyncio
async def test_get_version_coverage_summary_caps_evidence_preview(db_session):
    """Coverage summaries keep full counts but bound the serialized evidence list."""
    project = await _make_project(db_session, "CoverageSummaryEvidenceCap")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Evidence preview is bounded"
    )

    base_time = criterion.created_at
    total_evidence = ac.MAX_EVIDENCE_PER_CRITERION_IN_SUMMARY + 3
    for i in range(total_evidence):
        db_session.add(
            CriterionEvidence(
                criterion_id=criterion.id,
                project_id=project.id,
                source_kind="operator",
                source_id=f"note-{i:02d}",
                detail={"index": i},
                recorded_at=base_time + timedelta(seconds=i),
            )
        )
    db_session.add(
        CriterionEvidence(
            criterion_id=criterion.id,
            project_id=project.id,
            source_kind="reachability",
            source_id="final-proof",
            detail={"proven": True},
            recorded_at=base_time + timedelta(seconds=total_evidence),
        )
    )
    await db_session.commit()

    summary = await ac.get_version_coverage_summary(db_session, version.id)
    item = summary["criteria"][0]

    assert item["coverage"] == "covered"
    assert item["evidence_count"] == total_evidence + 1
    assert item["evidence_truncated"] is True
    assert len(item["evidence"]) == ac.MAX_EVIDENCE_PER_CRITERION_IN_SUMMARY
    assert item["evidence"][-1]["source_id"] == "final-proof"
    assert item["evidence"][0]["source_id"] == (
        f"note-{total_evidence + 1 - ac.MAX_EVIDENCE_PER_CRITERION_IN_SUMMARY:02d}"
    )


@pytest.mark.asyncio
async def test_list_criteria_evidence_field_present(db_session):
    """Criteria items from get_version_coverage_summary always include an evidence list.

    Even uncovered criteria with no evidence must carry an empty list so callers
    can iterate without None-guards.
    """
    project = await _make_project(db_session, "EvidenceFieldAlways")
    version = await _make_version(db_session, project)
    await ac.create_criterion(
        db_session, project.id, version.id, "Criterion with no evidence yet"
    )

    summary = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary["total"] == 1

    item = summary["criteria"][0]
    assert "evidence" in item
    assert isinstance(item["evidence"], list)
    assert len(item["evidence"]) == 0
    assert item["coverage"] == "uncovered"


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
    assert first == 4  # 2 criteria × 2 rows each (task_merge + reachability)
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
async def test_task_merge_evidence_covers_late_mapped_direction_criteria(db_session):
    """record_task_merge_evidence picks up criteria mapped to the direction after task spawn.

    Simulates the race: a task is spawned with criterion_ids=[], then a criterion
    is mapped to the direction before the task's PR merges.  The merge-time call to
    record_task_merge_evidence must find the criterion via Direction.criterion_keys
    and record proven evidence for it.
    """
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project = await _make_project(db_session, "LateDirectionMapping")
    version = await _make_version(db_session, project)

    # Criterion authored after task was spawned (task carries no criterion_ids yet)
    late_criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Late-mapped criterion",
        created_by="strategist",
    )

    # Direction with the late criterion already linked (simulates post-spawn mapping)
    direction_row = Direction(
        project_id=project.id,
        direction_key="d-latemap1",
        data={"id": "d-latemap1", "text": "Late mapping direction", "status": "active"},
        status="active",
        criterion_keys=[late_criterion.criterion_key],
    )
    db_session.add(direction_row)
    await db_session.commit()

    # Task spawned before the criterion was mapped — criterion_ids is empty
    task = Task(
        project_id=project.id,
        title="Late mapping task",
        payloads=[{"type": "create", "prompt": "implement late mapped"}],
        context={
            "direction_id": "d-latemap1",
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "test ran green under verified merge",
            },
        },
        criterion_ids=[],  # empty at spawn time — the whole point of this test
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    recorded = await ac.record_task_merge_evidence(db_session, task, project.id)

    assert recorded == 2, (
        "Expected 2 evidence rows (task_merge + reachability) for one criterion recorded "
        f"via late direction mapping; got {recorded}"
    )

    evidence = await ac.get_evidence_for_criterion(db_session, late_criterion.id)
    assert len(evidence) == 2
    assert {e.source_kind for e in evidence} == {"task_merge", "reachability"}
    assert all(e.detail.get("proven") is True for e in evidence)
    assert {e.source_id for e in evidence} == {str(task.id)}
    # task_merge row must carry direction_key provenance from the late-mapping path
    task_merge_ev = next(e for e in evidence if e.source_kind == "task_merge")
    assert task_merge_ev.detail.get("direction_key") == "d-latemap1"

    coverage = ac.compute_criterion_coverage(evidence)
    assert coverage == "covered"


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


@pytest.mark.asyncio
async def test_coverage_summary_all_retracted_keeps_audit_rows(db_session):
    """All-retracted criteria no longer count as active but remain auditable."""
    project = await _make_project(db_session, "AllRetractedCoverage")
    version = await _make_version(db_session, project)
    c1 = await ac.create_criterion(db_session, project.id, version.id, "Old goal A")
    c2 = await ac.create_criterion(db_session, project.id, version.id, "Old goal B")

    await ac.record_evidence(
        db_session,
        criterion_id=c1.id,
        project_id=project.id,
        source_kind="task_merge",
        source_id="task-1",
        detail={"proven": False},
    )
    for criterion in (c1, c2):
        row = await ac.retract_criterion(
            db_session,
            project.id,
            version.id,
            criterion.criterion_key,
            reason="Criterion retired by strategist",
            retracted_by="strategist",
        )
        assert row is not None
        assert row.status == "retracted"

    summary = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary["total"] == 0
    assert summary["covered"] == 0
    assert summary["partial"] == 0
    assert summary["uncovered"] == 0
    assert summary["retracted"] == 2
    assert summary["superseded"] == 0
    assert summary["inactive"] == 2
    assert summary["stuck_count"] == 0
    by_key = {c["criterion_key"]: c for c in summary["criteria"]}
    assert set(by_key) == {c1.criterion_key, c2.criterion_key}
    assert by_key[c1.criterion_key]["status"] == "retracted"
    assert by_key[c1.criterion_key]["coverage"] == "partial"
    assert by_key[c2.criterion_key]["status"] == "retracted"
    assert by_key[c2.criterion_key]["coverage"] == "uncovered"


@pytest.mark.asyncio
async def test_retracted_criterion_clears_coverage_and_gate(db_session):
    """Retracted criteria remain auditable but no longer block qualification."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.release_scoring import compute_composite

    project = await _make_project(db_session, "RetractedGate")
    version = await _make_version(db_session, project)

    covered = await ac.create_criterion(
        db_session,
        project.id,
        version.id,
        "Covered by a proven merge-test verdict",
    )
    stale = await ac.create_criterion(
        db_session,
        project.id,
        version.id,
        "Stale criterion with no feasible evidence",
    )

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "prove covered criterion"}],
        context={
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "targeted acceptance test passed under merge test",
            },
        },
        criterion_ids=[covered.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    assert await ac.record_task_merge_evidence(db_session, task, project.id) == 2

    before = await ac.get_version_coverage_summary(db_session, version.id)
    assert before["total"] == 2
    assert before["covered"] == 1
    assert before["uncovered"] == 1
    assert before["retracted"] == 0
    before_gate = compute_composite(
        ci_green=True,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=1.0,
        policy={"threshold": 0.8},
        criterion_coverage=before,
    )
    assert before_gate["blocker"] == "criterion_coverage_incomplete"

    row = await ac.retract_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=stale.criterion_key,
        reason="Criterion cannot be covered by the current strategy",
        retracted_by="strategist",
    )
    assert row is not None
    assert row.status == "retracted"

    after = await ac.get_version_coverage_summary(db_session, version.id)
    assert after["total"] == 1
    assert after["covered"] == 1
    assert after["uncovered"] == 0
    assert after["retracted"] == 1
    assert after["superseded"] == 0
    assert after["inactive"] == 1
    by_key = {c["criterion_key"]: c for c in after["criteria"]}
    assert by_key[covered.criterion_key]["status"] == "active"
    assert by_key[stale.criterion_key]["status"] == "retracted"
    assert by_key[stale.criterion_key]["coverage"] == "uncovered"
    assert by_key[stale.criterion_key]["stuck_reason"] is None

    after_gate = compute_composite(
        ci_green=True,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=1.0,
        policy={"threshold": 0.8},
        criterion_coverage=after,
    )
    assert after_gate["blocker"] is None
    assert after_gate["promote"] is True


@pytest.mark.asyncio
async def test_retract_criterion_is_idempotent(db_session):
    """Calling retract_criterion repeatedly returns the existing inactive row."""
    project = await _make_project(db_session, "RetractIdempotent")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session,
        project.id,
        version.id,
        "Criterion to retract once",
    )

    first = await ac.retract_criterion(
        db_session,
        project.id,
        version.id,
        criterion.criterion_key,
        reason="First retraction reason",
        retracted_by="strategist",
    )
    assert first is not None
    assert first.status == "retracted"
    assert first.retraction_reason == "First retraction reason"
    assert first.retracted_at is not None

    second = await ac.retract_criterion(
        db_session,
        project.id,
        version.id,
        criterion.criterion_key,
        reason="Second call should not overwrite audit fields",
        retracted_by="strategist",
        superseded_by_key="c-replacement",
    )
    assert second is not None
    assert second.id == first.id
    assert second.status == "retracted"
    assert second.retraction_reason == "First retraction reason"
    assert second.superseded_by_key is None


@pytest.mark.asyncio
async def test_set_acceptance_criteria_op_retracts_criterion(
    client: AsyncClient, db_session
):
    """The strategic HTTP op accepts a retract-only payload end-to-end."""
    from app.auth import create_agent_token

    project = await _make_project(db_session, "RetractStrategicOp")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session,
        project.id,
        version.id,
        "Criterion retired by strategist",
    )
    # Create the real successor criterion that superseded_by_key will reference.
    successor = await ac.create_criterion(
        db_session,
        project.id,
        version.id,
        "Replacement criterion",
    )

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-retract-op",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id),
        "agent-vm-retract-op",
        task_type="strategic_review",
    )
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "retract": [
                    {
                        "criterion_key": criterion.criterion_key,
                        "reason": "Superseded by direct release evidence",
                        "superseded_by_key": successor.criterion_key,
                    }
                ]
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["criteria"] == []
    assert body["retracted"] == [
        {
            "criterion_key": criterion.criterion_key,
            "status": "superseded",
            "found": True,
        }
    ]

    await db_session.refresh(criterion)
    assert criterion.status == "superseded"
    assert criterion.retracted_by == "strategist"
    assert criterion.superseded_by_key == successor.criterion_key

    summary = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary["total"] == 1  # successor is still active
    assert summary["uncovered"] == 1
    assert summary["retracted"] == 0
    assert summary["superseded"] == 1
    assert summary["inactive"] == 1


@pytest.mark.asyncio
async def test_loop_cannot_retract_operator_authored_criterion(
    client: AsyncClient, db_session
):
    """The autonomous loop must NOT retract an operator-authored criterion.

    Exercises the loop-authorship guard (acceptance_criteria.retract_criterion
    with ``enforce_loop_authored`` + the up-front router validation): a
    strategist/loop-driven ``set_acceptance_criteria`` retract targeting a
    criterion with ``created_by="operator"`` is refused with HTTP 422 and the
    criterion stays active. A batch that includes an operator-authored key is
    rejected wholesale — no criterion in the batch is retracted.
    """
    from app.auth import create_agent_token
    from app.models import MessageType, ProjectMessage

    project = await _make_project(db_session, "OperatorAuthoredGuard")
    version = await _make_version(db_session, project)
    operator_criterion = await ac.create_criterion(
        db_session,
        project.id,
        version.id,
        "Operator-authored gate that must survive the loop",
        created_by="operator",
    )
    # A loop-authored criterion in the SAME batch must not be retracted either,
    # proving the whole request is rejected before any retraction commits.
    loop_criterion = await ac.create_criterion(
        db_session,
        project.id,
        version.id,
        "Loop-authored criterion in the same batch",
        created_by="strategist",
    )

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-operator-guard",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id),
        "agent-vm-operator-guard",
        task_type="strategic_review",
    )
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "retract": [
                    {
                        "criterion_key": loop_criterion.criterion_key,
                        "reason": "Loop cleanup",
                    },
                    {
                        "criterion_key": operator_criterion.criterion_key,
                        "reason": "Loop tried to retract an operator gate",
                    },
                ]
            },
        },
    )
    assert resp.status_code == 422, (
        f"Expected 422 refusing operator-authored retraction, got "
        f"{resp.status_code}: {resp.text}"
    )
    message = resp.json()["error"]["message"]
    assert operator_criterion.criterion_key in message
    assert "operator" in message.lower()

    # Both criteria remain active — the whole batch was rejected atomically.
    await db_session.refresh(operator_criterion)
    await db_session.refresh(loop_criterion)
    assert operator_criterion.status == "active"
    assert loop_criterion.status == "active"

    summary = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary["retracted"] == 0
    assert summary["superseded"] == 0
    assert summary["total"] == 2

    # No operator-attention notification is emitted for a refused retraction.
    notes = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.message_type == MessageType.NOTE,
                )
            )
        )
        .scalars()
        .all()
    )
    assert not any(
        (m.context or {}).get("event_type")
        in ("criterion_retracted", "criterion_superseded")
        for m in notes
    )


@pytest.mark.asyncio
async def test_loop_can_retract_strategist_authored_criterion(
    client: AsyncClient, db_session
):
    """The safety valve stays intact: a loop-authored criterion still retracts.

    Guards against the loop-authorship check over-reaching and breaking the
    core valve. A ``created_by="strategist"`` (and a legacy-null) criterion must
    retract cleanly and free coverage headroom.
    """
    from app.auth import create_agent_token

    project = await _make_project(db_session, "StrategistAuthoredValve")
    version = await _make_version(db_session, project)
    loop_criterion = await ac.create_criterion(
        db_session,
        project.id,
        version.id,
        "Loop-authored uncoverable criterion",
        created_by="strategist",
    )
    legacy_criterion = await ac.create_criterion(
        db_session,
        project.id,
        version.id,
        "Legacy criterion with null provenance",
        created_by=None,
    )

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-valve-intact",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id),
        "agent-vm-valve-intact",
        task_type="strategic_review",
    )
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "retract": [
                    {
                        "criterion_key": loop_criterion.criterion_key,
                        "reason": "Uncoverable — clearing via the valve",
                    },
                    {
                        "criterion_key": legacy_criterion.criterion_key,
                        "reason": "Legacy rows remain retractable",
                    },
                ]
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    retracted_status = {
        r["criterion_key"]: r["status"] for r in body["retracted"]
    }
    assert retracted_status[loop_criterion.criterion_key] == "retracted"
    assert retracted_status[legacy_criterion.criterion_key] == "retracted"

    await db_session.refresh(loop_criterion)
    await db_session.refresh(legacy_criterion)
    assert loop_criterion.status == "retracted"
    assert legacy_criterion.status == "retracted"


@pytest.mark.asyncio
async def test_loop_retraction_emits_operator_attention_signal(
    client: AsyncClient, db_session
):
    """A successful loop retraction emits a high-signal ``criterion_retracted`` NOTE."""
    from app.auth import create_agent_token
    from app.models import MessageType, ProjectMessage

    project = await _make_project(db_session, "RetractSignal")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session,
        project.id,
        version.id,
        "Loop-authored criterion to retract with signal",
        created_by="strategist",
    )

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-retract-signal",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "agent-vm-retract-signal", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "retract": [
                    {
                        "criterion_key": criterion.criterion_key,
                        "reason": "No feasible evidence path",
                    }
                ]
            },
        },
    )
    assert resp.status_code == 200, resp.text

    notes = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.message_type == MessageType.NOTE,
                )
            )
        )
        .scalars()
        .all()
    )
    signal = [
        m
        for m in notes
        if (m.context or {}).get("event_type") == "criterion_retracted"
    ]
    assert len(signal) == 1, f"expected one criterion_retracted NOTE, got {len(signal)}"
    msg = signal[0]
    assert msg.from_type == "system"
    ctx = msg.context or {}
    assert ctx["criterion_key"] == criterion.criterion_key
    assert ctx["reason"] == "No feasible evidence path"
    assert ctx["retracted_by"] == "strategist"
    assert ctx["release_version_id"] == str(version.id)
    assert "superseded_by_key" not in ctx


@pytest.mark.asyncio
async def test_loop_supersession_emits_operator_attention_signal(
    client: AsyncClient, db_session
):
    """A successful loop supersession emits a high-signal ``criterion_superseded`` NOTE."""
    from app.auth import create_agent_token
    from app.models import MessageType, ProjectMessage

    project = await _make_project(db_session, "SupersedeSignal")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session,
        project.id,
        version.id,
        "Loop-authored criterion to supersede with signal",
        created_by="strategist",
    )
    successor = await ac.create_criterion(
        db_session,
        project.id,
        version.id,
        "Replacement criterion",
        created_by="strategist",
    )

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-supersede-signal",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "agent-vm-supersede-signal", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "retract": [
                    {
                        "criterion_key": criterion.criterion_key,
                        "reason": "Replaced by sharper criterion",
                        "superseded_by_key": successor.criterion_key,
                    }
                ]
            },
        },
    )
    assert resp.status_code == 200, resp.text

    notes = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.message_type == MessageType.NOTE,
                )
            )
        )
        .scalars()
        .all()
    )
    signal = [
        m
        for m in notes
        if (m.context or {}).get("event_type") == "criterion_superseded"
    ]
    assert len(signal) == 1, (
        f"expected one criterion_superseded NOTE, got {len(signal)}"
    )
    ctx = signal[0].context or {}
    assert ctx["criterion_key"] == criterion.criterion_key
    assert ctx["superseded_by_key"] == successor.criterion_key
    assert ctx["retracted_by"] == "strategist"
    # A retracted (non-superseded) NOTE must NOT be emitted for this case.
    assert not any(
        (m.context or {}).get("event_type") == "criterion_retracted" for m in notes
    )


@pytest.mark.asyncio
async def test_retract_criterion_helper_enforces_loop_authored_guard(db_session):
    """Unit-level: ``retract_criterion`` refuses operator-authored rows when
    ``enforce_loop_authored`` is set, and leaves them untouched.

    The operator-facing path (flag left False) is unaffected — it can still
    retract an operator-authored criterion.
    """
    project = await _make_project(db_session, "HelperGuard")
    version = await _make_version(db_session, project)
    operator_criterion = await ac.create_criterion(
        db_session,
        project.id,
        version.id,
        "Operator gate",
        created_by="operator",
    )

    with pytest.raises(ac.OperatorAuthoredRetractionError) as excinfo:
        await ac.retract_criterion(
            db_session,
            project_id=project.id,
            release_version_id=version.id,
            criterion_key=operator_criterion.criterion_key,
            reason="loop attempt",
            retracted_by="strategist",
            enforce_loop_authored=True,
        )
    assert excinfo.value.criterion_key == operator_criterion.criterion_key
    await db_session.refresh(operator_criterion)
    assert operator_criterion.status == "active"

    # Operator-facing path (flag omitted) can still retract the same criterion.
    row = await ac.retract_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=operator_criterion.criterion_key,
        reason="operator retracts their own criterion",
        retracted_by="operator",
    )
    assert row is not None
    assert row.status == "retracted"


def test_is_operator_authored_criterion_provenance():
    """Provenance classifier: operator/human authored vs loop/legacy."""
    assert ac.is_operator_authored_criterion("operator") is True
    assert ac.is_operator_authored_criterion("OPERATOR") is True
    assert ac.is_operator_authored_criterion("user") is True
    assert ac.is_operator_authored_criterion("human") is True
    # Loop and legacy provenance stay retractable.
    assert ac.is_operator_authored_criterion("strategist") is False
    assert ac.is_operator_authored_criterion("STRATEGIC_AGENT") is False
    assert ac.is_operator_authored_criterion(None) is False
    assert ac.is_operator_authored_criterion("") is False


@pytest.mark.asyncio
async def test_set_acceptance_criteria_supersession_rejects_self_reference(
    client: AsyncClient, db_session
):
    """set_acceptance_criteria rejects self-supersession (superseded_by_key == criterion_key)."""
    from app.auth import create_agent_token

    project = await _make_project(db_session, "SelfSupersede")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "Criterion A")

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-self-super",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "agent-vm-self-super", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "retract": [
                    {
                        "criterion_key": criterion.criterion_key,
                        "reason": "Self-replace",
                        "superseded_by_key": criterion.criterion_key,
                    }
                ]
            },
        },
    )
    assert resp.status_code == 400, f"Expected 400 for self-supersession, got {resp.status_code}: {resp.text}"
    assert "cannot supersede itself" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_set_acceptance_criteria_supersession_rejects_nonexistent_target(
    client: AsyncClient, db_session
):
    """set_acceptance_criteria rejects a superseded_by_key that does not exist on the open version."""
    from app.auth import create_agent_token

    project = await _make_project(db_session, "NonexistentSuper")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "Criterion B")

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-nonexist-super",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "agent-vm-nonexist-super", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "retract": [
                    {
                        "criterion_key": criterion.criterion_key,
                        "reason": "Replacing with something",
                        "superseded_by_key": "c-does-not-exist",
                    }
                ]
            },
        },
    )
    assert resp.status_code == 400, f"Expected 400 for nonexistent target, got {resp.status_code}: {resp.text}"
    assert "does not refer to an active criterion" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_set_acceptance_criteria_supersession_rejects_inactive_target(
    client: AsyncClient, db_session
):
    """set_acceptance_criteria rejects a superseded_by_key that is already inactive."""
    from app.auth import create_agent_token

    project = await _make_project(db_session, "InactiveTargetSuper")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "Source")
    already_retracted = await ac.create_criterion(
        db_session, project.id, version.id, "Already retracted"
    )
    await ac.retract_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=already_retracted.criterion_key,
        reason="Pre-retracted",
        retracted_by="test",
    )

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-inactive-target",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "agent-vm-inactive-target", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "retract": [
                    {
                        "criterion_key": criterion.criterion_key,
                        "reason": "Pointing at retracted target",
                        "superseded_by_key": already_retracted.criterion_key,
                    }
                ]
            },
        },
    )
    assert resp.status_code == 400, f"Expected 400 for inactive target, got {resp.status_code}: {resp.text}"
    assert "does not refer to an active criterion" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_apply_new_acceptance_criteria_active_only_headroom(db_session):
    """_apply_new_acceptance_criteria headroom is based on ACTIVE criteria only.

    Retracted rows must not consume cap slots so a strategist can replace
    retracted criteria without hitting the cap.
    """
    from app.strategic_completion import _apply_new_acceptance_criteria
    from app import release_version as rv

    project = Project(
        name="AC-ActiveHeadroom",
        user_id="strat-headroom-user",
        vision={"purpose": "test", "revision": 1},
        problem_model={"intent": {"current_focus": "focus"}, "revision": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    # Fill to (MAX - 1) active + 2 retracted = total rows > MAX - 1.
    for i in range(ac.MAX_CRITERIA_PER_VERSION - 1):
        await ac.create_criterion(db_session, project.id, version.id, f"Active {i}")

    retracted_one = await ac.create_criterion(
        db_session, project.id, version.id, "Retracted A"
    )
    retracted_two = await ac.create_criterion(
        db_session, project.id, version.id, "Retracted B"
    )
    await ac.retract_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=retracted_one.criterion_key,
        reason="stale",
        retracted_by="test",
    )
    await ac.retract_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=retracted_two.criterion_key,
        reason="stale",
        retracted_by="test",
    )

    # Total rows = MAX + 1, active rows = MAX - 1 → 1 slot remains.
    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "r"}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Submit 3 new texts; only 1 should be staged (fills the one remaining slot).
    await _apply_new_acceptance_criteria(
        ["New A", "New B", "New C"], task, project, db_session, path="test"
    )

    active_criteria = [
        c for c in await ac.get_criteria_for_version(db_session, version.id)
        if c.status == "active"
    ]
    new_active = [c for c in active_criteria if c.text.startswith("New")]
    assert len(new_active) == 1, (
        f"expected exactly 1 new active criterion (retracted rows must not consume cap), "
        f"got {len(new_active)}"
    )
    assert len(active_criteria) == ac.MAX_CRITERIA_PER_VERSION


@pytest.mark.asyncio
async def test_safety_valve_retract_then_author_same_call(
    client: AsyncClient, db_session
):
    """A version at MAX active criteria can retract K and author K replacements in one
    set_acceptance_criteria call; skipped_over_cap must be 0.
    """
    from app.auth import create_agent_token

    project = await _make_project(db_session, "SafetyValveProject")
    version = await _make_version(db_session, project)

    # Fill the version to exactly MAX active criteria.
    stale_criteria = []
    for i in range(ac.MAX_CRITERIA_PER_VERSION):
        c = await ac.create_criterion(
            db_session, project.id, version.id, f"Stale criterion {i}"
        )
        stale_criteria.append(c)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-safety-valve",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id),
        "agent-vm-safety-valve",
        task_type="strategic_review",
    )

    # Retract 3 stale criteria and author 3 replacements in one request.
    retract_keys = [c.criterion_key for c in stale_criteria[:3]]
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "retract": [
                    {"criterion_key": k, "reason": "superseded"} for k in retract_keys
                ],
                "criteria": [
                    {"text": f"Replacement {i}"} for i in range(3)
                ],
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["skipped_over_cap"] == 0, (
        f"expected 0 skipped (freed headroom should cover replacements), "
        f"got skipped_over_cap={body['skipped_over_cap']}"
    )
    new_criteria = [c for c in body["criteria"] if c["is_new"]]
    assert len(new_criteria) == 3

    # Active count should remain MAX (3 retracted, 3 added).
    active_count = await ac.count_active_criteria_for_version(db_session, version.id)
    assert active_count == ac.MAX_CRITERIA_PER_VERSION


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

    # Create an open release version and active/retracted/superseded criteria
    version = await _make_version(db_session, project)
    c1 = await ac.create_criterion(db_session, project.id, version.id, "CI passes")
    c2 = await ac.create_criterion(db_session, project.id, version.id, "Docs updated")
    c3 = await ac.create_criterion(
        db_session, project.id, version.id, "Deprecated criterion"
    )
    await ac.retract_criterion(
        db_session,
        project.id,
        version.id,
        c2.criterion_key,
        reason="superseded by CI gate",
        retracted_by="strategist",
    )
    await ac.retract_criterion(
        db_session,
        project.id,
        version.id,
        c3.criterion_key,
        reason="replaced by CI gate",
        retracted_by="strategist",
        superseded_by_key=c1.criterion_key,
    )

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
        criterion_keys=[c1.criterion_key, c2.criterion_key, c3.criterion_key],
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
    assert child.criterion_ids == [c1.criterion_key], (
        f"criterion_ids must exclude inactive keys; got {child.criterion_ids}"
    )


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
async def test_inactive_criteria_do_not_propagate_to_new_tasks(db_session):
    """Retracted and superseded criteria must be excluded from spawned-task criterion_ids.

    Exercises filter_active_criterion_keys, which is called by both the
    research-completion and admin-agent-ops spawn paths.  A direction whose
    criterion_keys list includes retracted or superseded keys must yield only
    the active key(s) when resolved for a new task.
    """
    project = await _make_project(db_session, "InactivePropagation")
    version = await _make_version(db_session, project)

    c_active = await ac.create_criterion(db_session, project.id, version.id, "Active criterion")
    c_retracted = await ac.create_criterion(db_session, project.id, version.id, "Retracted criterion")
    c_superseded = await ac.create_criterion(db_session, project.id, version.id, "Superseded criterion")

    await ac.retract_criterion(
        db_session, project.id, version.id,
        c_retracted.criterion_key, reason="no longer relevant", retracted_by="strategist",
    )
    await ac.retract_criterion(
        db_session, project.id, version.id,
        c_superseded.criterion_key, reason="replaced",
        retracted_by="strategist", superseded_by_key=c_active.criterion_key,
    )

    all_keys = [c_active.criterion_key, c_retracted.criterion_key, c_superseded.criterion_key]
    active_keys = await ac.filter_active_criterion_keys(
        db_session, project.id, all_keys, release_version_id=version.id
    )

    assert set(active_keys) == {c_active.criterion_key}, (
        f"filter_active_criterion_keys must exclude retracted/superseded keys; got {active_keys}"
    )
    assert c_retracted.criterion_key not in active_keys
    assert c_superseded.criterion_key not in active_keys

    # Verify compute_criterion_coverage still counts inactive criteria for audit
    summary = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary["total"] == 1  # only the active criterion counts
    assert summary["retracted"] == 1
    assert summary["superseded"] == 1


@pytest.mark.asyncio
async def test_filter_active_criterion_keys_scopes_to_version(db_session):
    """A closed-version key must not pass through when filtering for the open version."""
    project = await _make_project(db_session, "VersionScopedFiltering")
    open_version = await _make_version(db_session, project)
    closed_version = ReleaseVersion(
        project_id=project.id, version_number=2, status="closed"
    )
    db_session.add(closed_version)
    await db_session.commit()
    await db_session.refresh(closed_version)

    closed_key = "c-closed-scope"
    open_key = "c-open-scope"
    db_session.add(
        VersionAcceptanceCriterion(
            project_id=project.id,
            release_version_id=closed_version.id,
            criterion_key=closed_key,
            text="Closed version criterion",
        )
    )
    db_session.add(
        VersionAcceptanceCriterion(
            project_id=project.id,
            release_version_id=open_version.id,
            criterion_key=open_key,
            text="Open version criterion",
        )
    )
    await db_session.commit()

    filtered = await ac.filter_active_criterion_keys(
        db_session,
        project.id,
        [closed_key, open_key],
        release_version_id=open_version.id,
    )

    assert filtered == [open_key], (
        f"version-scoped filtering must exclude closed-version keys; got {filtered}"
    )


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


# ---------------------------------------------------------------------------
# test_requeue_clone_drops_inactive_criterion_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requeue_clone_drops_inactive_criterion_ids(db_session, mocker):
    """_requeue_parent_task drops retracted/superseded criterion_ids from the clone.

    When a failed child carries criterion_ids that include inactive (retracted
    or superseded) criteria, the requeued clone should carry only the active
    subset so a retracted criterion stops propagating as live work.
    """
    project = await _make_project(db_session, "RequeueDropInactive")
    version = await _make_version(db_session, project)

    active_c = await ac.create_criterion(db_session, project.id, version.id, "Active crit")
    retracted_c = await ac.create_criterion(db_session, project.id, version.id, "Retracted crit")
    await ac.retract_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=retracted_c.criterion_key,
        reason="No longer needed",
        retracted_by="test",
    )

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
        criterion_ids=[active_c.criterion_key, retracted_c.criterion_key],
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

    assert cloned.criterion_ids == [active_c.criterion_key], (
        "Only the active criterion should be propagated; retracted one must be dropped"
    )


# ---------------------------------------------------------------------------
# test_record_task_merge_evidence_skips_inactive_criteria
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_task_merge_evidence_skips_inactive_criteria(db_session):
    """record_task_merge_evidence does not record evidence for inactive criteria.

    When a task's criterion_ids include retracted or superseded criteria, the
    merge-evidence path must skip them so inactive criteria do not accrue new
    evidence and cannot be revived by a late merge.
    """
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project = await _make_project(db_session, "MergeEvidenceActiveOnly")
    version = await _make_version(db_session, project)

    active_c = await ac.create_criterion(db_session, project.id, version.id, "Active")
    retracted_c = await ac.create_criterion(db_session, project.id, version.id, "Retracted")
    await ac.retract_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=retracted_c.criterion_key,
        reason="Stale criterion",
        retracted_by="test",
    )

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "ship it"}],
        context={
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "test ran green",
            }
        },
        criterion_ids=[active_c.criterion_key, retracted_c.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    count = await ac.record_task_merge_evidence(db_session, task, project.id)
    assert count == 2, "Only the active criterion should receive evidence (2 rows: task_merge + reachability)"

    active_evidence = await ac.get_evidence_for_criterion(db_session, active_c.id)
    assert len(active_evidence) == 2  # task_merge + reachability

    retracted_evidence = await ac.get_evidence_for_criterion(db_session, retracted_c.id)
    assert retracted_evidence == [], "Retracted criterion must not receive new evidence"


# ---------------------------------------------------------------------------
# test_criterion_link_invalid_rejected_at_write
# ---------------------------------------------------------------------------


def test_criterion_link_invalid_rejected_at_write():
    """Write-time guard rejects dangling and inactive criterion keys at link time.

    Exercises the pure classifier and check helper in direction_referential_guard,
    which back every write surface that updates Direction.criterion_keys.
    """
    from app.direction_referential_guard import (
        check_criterion_key_write,
        classify_criterion_key,
    )

    active = frozenset(["c-alpha", "c-beta"])
    inactive = frozenset(["c-retracted", "c-superseded"])

    # Valid key — no rejection.
    cls, active_list = check_criterion_key_write("c-alpha", active, inactive)
    assert cls is None
    assert active_list == []

    # Dangling key (not in version at all) — rejected, active keys surfaced.
    cls, active_list = check_criterion_key_write("c-unknown", active, inactive)
    assert cls == "dangling"
    assert set(active_list) == {"c-alpha", "c-beta"}

    # Inactive key (retracted/superseded in open version) — rejected, active keys surfaced.
    cls, active_list = check_criterion_key_write("c-retracted", active, inactive)
    assert cls == "inactive"
    assert set(active_list) == {"c-alpha", "c-beta"}

    # Empty key treated as no-op (caller skips empty writes).
    cls, active_list = check_criterion_key_write("", active, inactive)
    assert cls is None
    assert active_list == []

    # classify_criterion_key returns correct classification for each case.
    assert classify_criterion_key("c-alpha", active, inactive) == "valid"
    assert classify_criterion_key("c-retracted", active, inactive) == "inactive"
    assert classify_criterion_key("c-superseded", active, inactive) == "inactive"
    assert classify_criterion_key("c-missing", active, inactive) == "dangling"
    assert classify_criterion_key("", active, inactive) == "dangling"


# ---------------------------------------------------------------------------
# Supersession chain staleness and retraction atomicity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retract_successor_clears_superseded_by_key_on_predecessor(db_session):
    """Retracting criterion B clears superseded_by_key on predecessor A.

    When A.superseded_by_key == B.criterion_key and B is later retracted,
    A's superseded_by_key must be set to None so the chain does not dangle.
    """
    project = await _make_project(db_session, "ChainClear")
    version = await _make_version(db_session, project)
    criterion_a = await ac.create_criterion(
        db_session, project.id, version.id, "Predecessor criterion"
    )
    criterion_b = await ac.create_criterion(
        db_session, project.id, version.id, "Successor criterion"
    )

    # Mark A as superseded by B.
    await ac.retract_criterion(
        db_session,
        project.id,
        version.id,
        criterion_a.criterion_key,
        reason="Superseded",
        superseded_by_key=criterion_b.criterion_key,
    )
    await db_session.refresh(criterion_a)
    assert criterion_a.superseded_by_key == criterion_b.criterion_key

    # Now retract B — A's superseded_by_key must be cleared.
    await ac.retract_criterion(
        db_session,
        project.id,
        version.id,
        criterion_b.criterion_key,
        reason="B is no longer valid",
    )
    await db_session.refresh(criterion_a)
    assert criterion_a.superseded_by_key is None


@pytest.mark.asyncio
async def test_retract_predecessor_does_not_affect_unrelated_superseded_by_keys(db_session):
    """Retracting criterion X does not clear superseded_by_key on an unrelated row.

    Only rows whose superseded_by_key matches the retracted criterion_key are
    affected; other superseded_by_key values must be left unchanged.
    """
    project = await _make_project(db_session, "ChainIsolation")
    version = await _make_version(db_session, project)
    criterion_a = await ac.create_criterion(
        db_session, project.id, version.id, "Criterion A"
    )
    criterion_b = await ac.create_criterion(
        db_session, project.id, version.id, "Criterion B (successor of A)"
    )
    criterion_x = await ac.create_criterion(
        db_session, project.id, version.id, "Unrelated criterion X"
    )

    # A superseded by B.
    await ac.retract_criterion(
        db_session,
        project.id,
        version.id,
        criterion_a.criterion_key,
        reason="Superseded by B",
        superseded_by_key=criterion_b.criterion_key,
    )
    await db_session.refresh(criterion_a)
    assert criterion_a.superseded_by_key == criterion_b.criterion_key

    # Retract unrelated X — must not touch A.superseded_by_key.
    await ac.retract_criterion(
        db_session,
        project.id,
        version.id,
        criterion_x.criterion_key,
        reason="X retired",
    )
    await db_session.refresh(criterion_a)
    assert criterion_a.superseded_by_key == criterion_b.criterion_key


@pytest.mark.asyncio
async def test_attest_retracted_criterion_returns_409(client: AsyncClient, db_session):
    """POST .../attest on a retracted criterion returns 409."""
    project = await _make_project_with_member(db_session, name="Attest-Retracted")
    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Criterion to retract"
    )
    await ac.retract_criterion(
        db_session,
        project.id,
        version.id,
        criterion.criterion_key,
        reason="No longer needed",
        retracted_by="operator",
    )

    resp = await client.post(
        f"/api/admin/projects/{project.id}/acceptance-criteria/{criterion.criterion_key}/attest",
        json={"reason": "I still want to attest this"},
    )
    assert resp.status_code == 409, resp.text
    assert "retracted" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_attest_superseded_criterion_returns_409(client: AsyncClient, db_session):
    """POST .../attest on a superseded criterion returns 409."""
    project = await _make_project_with_member(db_session, name="Attest-Superseded")
    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    predecessor = await ac.create_criterion(
        db_session, project.id, version.id, "Old criterion"
    )
    successor = await ac.create_criterion(
        db_session, project.id, version.id, "New replacement criterion"
    )
    await ac.retract_criterion(
        db_session,
        project.id,
        version.id,
        predecessor.criterion_key,
        reason="Replaced",
        superseded_by_key=successor.criterion_key,
    )

    resp = await client.post(
        f"/api/admin/projects/{project.id}/acceptance-criteria/{predecessor.criterion_key}/attest",
        json={"reason": "I want to attest the old one"},
    )
    assert resp.status_code == 409, resp.text
    assert "superseded" in resp.json()["error"]["message"]


# ---------------------------------------------------------------------------
# retire_direction criterion-link guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retire_direction_clears_criterion_keys(
    client: AsyncClient, db_session, mocker
):
    """retire_direction must clear criterion_keys from the Direction ORM row.

    When a direction is retired the autonomous loop must not propagate its
    criterion links to newly spawned tasks.  The criterion-link guard in the
    retire_direction path sets Direction.criterion_keys to [] and surfaces a
    warning in the response so the strategist knows stale links were cleared.
    """
    from app.auth import create_agent_token
    from app import strategic_cycle

    mocker.patch(
        "app.routers.admin_agent_ops.emit_strategic_updated", new=AsyncMock()
    )

    dir_id = "d-retire-crit-guard"
    project = Project(
        name="RetireCritGuard",
        user_id="test-user-crit-guard",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": dir_id,
                "text": "direction with criterion links",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Criterion linked to direction"
    )

    # Wire the Direction ORM row (dual-write peer) with criterion_keys.
    dir_row = Direction(
        project_id=project.id,
        direction_key=dir_id,
        data={"id": dir_id, "text": "direction with criterion links", "status": "active"},
        criterion_keys=[criterion.criterion_key],
    )
    db_session.add(dir_row)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-retire-crit-guard",
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "vm-retire-crit-guard", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "retire_direction",
            "data": {
                "direction_id": dir_id,
                "rationale": "work is done",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    # Guard must surface a warning that criterion links were cleared.
    warnings = body.get("warnings", [])
    assert any("criterion" in w.lower() and dir_id in w for w in warnings), (
        f"retire_direction must warn about cleared criterion_keys; got warnings={warnings}"
    )

    # The Direction ORM row must have criterion_keys cleared.
    await db_session.refresh(dir_row)
    assert dir_row.criterion_keys == [] or dir_row.criterion_keys is None, (
        f"Direction ORM row criterion_keys must be cleared after retire; "
        f"got {dir_row.criterion_keys}"
    )


# ---------------------------------------------------------------------------
# backfill_direction_merge_evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_direction_merge_evidence_covers_late_mapping(db_session):
    """Criterion mapped AFTER a proven merged task already exists flips to covered.

    This is the primary acceptance requirement: a criterion-direction mapping
    created late must not leave the criterion stranded.  The backfill helper
    must replay the merge evidence so the criterion reaches 'covered'.
    """
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    project = await _make_project(db_session, "BackfillLateMappingCoverage")
    version = await _make_version(db_session, project)

    # Create a direction row WITHOUT criterion_keys yet.
    direction_key = "d-backfill-late"
    dir_row = Direction(
        project_id=project.id,
        direction_key=direction_key,
        data={"id": direction_key, "text": "Backfill test direction", "status": "active"},
        status="active",
        criterion_keys=[],
    )
    db_session.add(dir_row)

    # Create a proven merged task for the direction (simulates already-landed PR).
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "implement it"}],
        context={
            "direction_id": direction_key,
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "green",
                "proof_ref": "testing/test_backfill.py::test_x",
            },
        },
        status=TaskStatus.COMPLETED,
        completed_at=version.opened_at + timedelta(seconds=1),
    )
    db_session.add(task)
    await db_session.commit()

    # NOW map the criterion to the direction (this is the late mapping scenario).
    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Late-mapped criterion that should be covered by backfill",
        created_by="operator",
    )
    dir_row.criterion_keys = [criterion.criterion_key]
    db_session.add(dir_row)
    await db_session.commit()

    # Before backfill: the criterion has no evidence.
    evidence_before = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert evidence_before == [], "No evidence before backfill"

    # Run backfill.
    recorded = await ac.backfill_direction_merge_evidence(db_session, project.id, direction_key)

    assert recorded == 2, f"Expected 2 evidence rows (task_merge + reachability) for 1 criterion; got {recorded}"
    evidence_after = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence_after) == 2
    assert {e.source_kind for e in evidence_after} == {"task_merge", "reachability"}

    coverage = ac.compute_criterion_coverage(evidence_after)
    assert coverage == "covered", f"Expected 'covered', got '{coverage}'"


@pytest.mark.asyncio
async def test_backfill_direction_merge_evidence_idempotent(db_session):
    """Calling backfill twice on the same proven merged task records 0 new rows the second time."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    project = await _make_project(db_session, "BackfillIdempotent")
    version = await _make_version(db_session, project)
    direction_key = "d-backfill-idempotent"

    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Idempotent criterion"
    )
    dir_row = Direction(
        project_id=project.id,
        direction_key=direction_key,
        data={"id": direction_key, "text": "Idempotent direction", "status": "active"},
        status="active",
        criterion_keys=[criterion.criterion_key],
    )
    db_session.add(dir_row)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "implement it"}],
        context={
            "direction_id": direction_key,
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            CODE_ACCEPTANCE_VERDICT_KEY: {"satisfied": True, "proven": True, "kind": "named_test"},
        },
        status=TaskStatus.COMPLETED,
        completed_at=version.opened_at + timedelta(seconds=1),
    )
    db_session.add(task)
    await db_session.commit()

    first = await ac.backfill_direction_merge_evidence(db_session, project.id, direction_key)
    second = await ac.backfill_direction_merge_evidence(db_session, project.id, direction_key)

    assert first == 2, f"First run should record 2 rows (task_merge + reachability); got {first}"
    assert second == 0, f"Second run must be idempotent (0 new rows); got {second}"


@pytest.mark.asyncio
async def test_backfill_direction_merge_evidence_skips_unproven(db_session):
    """A merged task WITHOUT a proven acceptance verdict yields no evidence."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    project = await _make_project(db_session, "BackfillSkipsUnproven")
    version = await _make_version(db_session, project)
    direction_key = "d-backfill-unproven"

    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Unproven criterion"
    )
    dir_row = Direction(
        project_id=project.id,
        direction_key=direction_key,
        data={"id": direction_key, "text": "Unproven direction", "status": "active"},
        status="active",
        criterion_keys=[criterion.criterion_key],
    )
    db_session.add(dir_row)
    # Task merged but verdict is proven=False.
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "implement it"}],
        context={
            "direction_id": direction_key,
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            CODE_ACCEPTANCE_VERDICT_KEY: {"satisfied": False, "proven": False},
        },
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    recorded = await ac.backfill_direction_merge_evidence(db_session, project.id, direction_key)

    assert recorded == 0, f"Unproven task must not produce evidence; got {recorded}"
    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert evidence == [], "No evidence for unproven task"


@pytest.mark.asyncio
async def test_backfill_direction_merge_evidence_scopes_to_direction(db_session):
    """Tasks from a different direction are not included in the backfill."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    project = await _make_project(db_session, "BackfillScoped")
    version = await _make_version(db_session, project)
    target_key = "d-backfill-scoped-target"
    other_key = "d-backfill-scoped-other"

    criterion_target = await ac.create_criterion(
        db_session, project.id, version.id, "Target direction criterion"
    )
    criterion_other = await ac.create_criterion(
        db_session, project.id, version.id, "Other direction criterion"
    )

    for dk, criterion in [(target_key, criterion_target), (other_key, criterion_other)]:
        dir_row = Direction(
            project_id=project.id,
            direction_key=dk,
            data={"id": dk, "text": dk, "status": "active"},
            status="active",
            criterion_keys=[criterion.criterion_key],
        )
        db_session.add(dir_row)

    # Task belongs to the OTHER direction.
    other_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "other"}],
        context={
            "direction_id": other_key,
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            CODE_ACCEPTANCE_VERDICT_KEY: {"satisfied": True, "proven": True, "kind": "named_test"},
        },
        status=TaskStatus.COMPLETED,
    )
    db_session.add(other_task)
    await db_session.commit()

    # Backfill only the target direction.
    recorded = await ac.backfill_direction_merge_evidence(db_session, project.id, target_key)

    assert recorded == 0, f"Other direction's task must not evidence target; got {recorded}"
    ev_target = await ac.get_evidence_for_criterion(db_session, criterion_target.id)
    assert ev_target == [], "Target criterion must have no evidence from other direction's task"

    ev_other = await ac.get_evidence_for_criterion(db_session, criterion_other.id)
    assert ev_other == [], "Other criterion was not the backfill target"


@pytest.mark.asyncio
async def test_backfill_direction_merge_evidence_skips_retracted_criterion(db_session):
    """Retracted criteria are not covered by backfill."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    project = await _make_project(db_session, "BackfillSkipsRetracted")
    version = await _make_version(db_session, project)
    direction_key = "d-backfill-retracted"

    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Retracted criterion"
    )
    # Retract the criterion before backfill.
    await ac.retract_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=criterion.criterion_key,
        reason="superseded",
    )

    dir_row = Direction(
        project_id=project.id,
        direction_key=direction_key,
        data={"id": direction_key, "text": "Retracted test", "status": "active"},
        status="active",
        criterion_keys=[criterion.criterion_key],
    )
    db_session.add(dir_row)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "implement it"}],
        context={
            "direction_id": direction_key,
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            CODE_ACCEPTANCE_VERDICT_KEY: {"satisfied": True, "proven": True, "kind": "named_test"},
        },
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    recorded = await ac.backfill_direction_merge_evidence(db_session, project.id, direction_key)

    assert recorded == 0, f"Retracted criterion must not receive backfill evidence; got {recorded}"


@pytest.mark.asyncio
async def test_link_direction_endpoint_triggers_backfill(client, db_session):
    """POST link-direction with an already-merged proven task causes evidence to appear.

    Acceptance requirement: the link-direction endpoint must run the backfill
    so criteria stranded before this feature shipped can be cleared without
    re-editing them.
    """
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    project = await _make_project(db_session, "LinkDirectionBackfill")
    member = ProjectMember(
        project_id=project.id, user_id=_TEST_USER_ID, role="owner"
    )
    db_session.add(member)
    version = await _make_version(db_session, project)

    direction_key = "d-link-backfill"
    # Create direction row without criterion_keys.
    dir_row = Direction(
        project_id=project.id,
        direction_key=direction_key,
        data={"id": direction_key, "text": "Link backfill direction", "status": "active"},
        status="active",
        criterion_keys=[],
    )
    db_session.add(dir_row)

    # Proven merged task already exists for this direction.
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "implement it"}],
        context={
            "direction_id": direction_key,
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "green CI",
            },
        },
        status=TaskStatus.COMPLETED,
        completed_at=version.opened_at + timedelta(seconds=1),
    )
    db_session.add(task)
    await db_session.commit()

    # Create the criterion AFTER the task already merged.
    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Late criterion — backfill coverage test",
        created_by="operator",
    )
    await db_session.commit()

    # POST link-direction — this is the surface that must trigger backfill.
    resp = await client.post(
        f"/api/admin/projects/{project.id}/acceptance-criteria/link-direction",
        json={"direction_key": direction_key, "criterion_keys": [criterion.criterion_key]},
        headers={"x-test-user-id": _TEST_USER_ID},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["direction_key"] == direction_key

    # The backfill should have run, recording evidence for the criterion.
    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence) >= 1, (
        "link-direction must trigger backfill; criterion should have evidence after the call"
    )
    assert any(e.source_kind == "task_merge" for e in evidence), (
        "Expected task_merge evidence from backfill"
    )
    coverage = ac.compute_criterion_coverage(evidence)
    assert coverage == "covered", f"Expected 'covered' after backfill, got '{coverage}'"

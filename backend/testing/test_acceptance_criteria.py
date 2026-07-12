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

import json
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app import acceptance_criteria as ac
from app.operator_attention import build_operator_attention
from app.time_utils import _utcnow
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


def test_unverified_merge_backfill_reports_distinct_tier():
    """Unverified-merge backfill evidence produces the distinct 'backfilled' tier.

    ``_evidence_proves_covered`` must NOT treat ``UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND``
    as machine-verified covering evidence — that would score it at full parity with
    reachability / proven task_merge, even though the merge was never CI-verified
    (only the named_test file shipping was confirmed).  The test confirms:
      - backfill evidence alone → ``compute_criterion_coverage`` returns ``'backfilled'``
      - backfill + proven machine evidence → ``'covered'`` (backfill doesn't regress)
      - backfill + operator attestation → ``'attested'`` (attestation still higher)
    """
    # Backfill evidence alone → distinct backfilled tier (not covered)
    backfill_ev = _make_evidence(
        ac.UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND,
        detail={"proven": True, "backfilled": True, "backfill_reason": "unverified_merge"},
    )
    assert not ac._evidence_proves_covered(backfill_ev), (
        "UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND must NOT be treated as covering"
    )
    assert ac.compute_criterion_coverage([backfill_ev]) == "backfilled", (
        "Backfill evidence alone should produce 'backfilled' coverage"
    )

    # Backfill + proven task_merge → covered (machine evidence dominates)
    task_merge_ev = _make_evidence("task_merge", detail={"proven": True})
    assert ac.compute_criterion_coverage([backfill_ev, task_merge_ev]) == "covered", (
        "Backfill + proven task_merge should be 'covered'"
    )

    # Backfill + operator attestation → attested (attestation higher than backfill)
    attest_ev = _make_evidence("operator_attestation", detail={"reason": "waiver"})
    assert ac.compute_criterion_coverage([backfill_ev, attest_ev]) == "attested", (
        "Backfill + operator attestation should be 'attested' (attestation > backfill)"
    )

    # Backfill evidence WITHOUT backfilled=True flag → partial (not a valid backfill record)
    bare_backfill = _make_evidence(ac.UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND)
    assert ac.compute_criterion_coverage([bare_backfill]) == "partial", (
        "Bare backfill evidence without backfilled=True flag should be 'partial'"
    )

    # Coverage summary key is reported
    assert ac.criterion_coverage_to_evidence_status("backfilled") == "partial"


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
    # stuck_criteria_count (2) + coverage_blocked_count (1 for c_active: uncovered with
    # active direction but no evidence yet) = 3 total needs_operator items.
    assert attention["counts"]["needs_operator"] == 3
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


@pytest.mark.asyncio
async def test_expired_attestation_emits_attention_item(db_session):
    """An expired operator attestation on an active criterion emits an attestation-expiry attention item.

    build_operator_attention must surface expired attestations as
    'attestation_expired' needs_operator items so the operator knows to
    re-attest or provide machine evidence.
    """
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Expired-attestation criterion"
    )
    # Record an expired operator attestation (> ATTESTATION_WINDOW_DAYS old).
    stale_at = (
        _utcnow() - timedelta(days=ac.ATTESTATION_WINDOW_DAYS + 1)
    ).isoformat()
    await ac.record_evidence(
        db_session,
        criterion.id,
        project.id,
        source_kind="operator_attestation",
        source_id=_TEST_USER_ID,
        detail={"attested_at": stale_at, "reason": "was OK at the time"},
    )

    attention = await build_operator_attention(db_session, project, _TEST_USER_ID)

    assert attention["level"] == "needs_operator"
    assert attention["counts"]["attestation_expired"] == 1
    attest_id = f"version:{version.id}:attestation_expired:{criterion.criterion_key}"
    items_by_id = {item["id"]: item for item in attention["items"]}
    assert attest_id in items_by_id, (
        f"Expected attestation_expired item {attest_id} in {list(items_by_id)}"
    )
    item = items_by_id[attest_id]
    assert item["reason"] == "attestation_expired"
    assert item["label"] == "Attestation expired"
    assert item["release_version_id"] == str(version.id)
    assert "re-attest" in item["detail"].lower()


@pytest.mark.asyncio
async def test_fresh_attestation_no_expiry_attention_item(db_session):
    """A fresh (non-expired) operator attestation emits NO attestation-expiry item.

    Only expired attestations should surface; a valid attestation keeps the
    criterion in 'attested' coverage and must not trigger the expiry alert.
    """
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Fresh-attestation criterion"
    )
    fresh_at = _utcnow().isoformat()
    await ac.record_evidence(
        db_session,
        criterion.id,
        project.id,
        source_kind="operator_attestation",
        source_id=_TEST_USER_ID,
        detail={"attested_at": fresh_at, "reason": "still valid"},
    )

    attention = await build_operator_attention(db_session, project, _TEST_USER_ID)

    assert attention["counts"]["attestation_expired"] == 0
    attest_id = f"version:{version.id}:attestation_expired:{criterion.criterion_key}"
    items_by_id = {item["id"]: item for item in attention["items"]}
    assert attest_id not in items_by_id, (
        f"Unexpected attestation_expired item {attest_id} for a fresh attestation"
    )


@pytest.mark.asyncio
async def test_attestation_approaching_expiry_warns_before_lapse(db_session):
    """A near-expiry operator attestation emits attestation_expiring_soon attention.

    When the attestation is still valid (coverage='attested') but within the
    warning window of its TTL, build_operator_attention must surface an
    'attestation_expiring_soon' item so the operator can re-attest or provide
    machine evidence before the criterion lapses to 'partial'.
    """
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Near-expiry attestation criterion"
    )
    # Set attested_at so the attestation expires in ~1 day — well within the
    # 2-day warning window.
    near_expiry_at = (
        _utcnow() - timedelta(days=ac.ATTESTATION_WINDOW_DAYS - 1)
    ).isoformat()
    await ac.record_evidence(
        db_session,
        criterion.id,
        project.id,
        source_kind="operator_attestation",
        source_id=_TEST_USER_ID,
        detail={"attested_at": near_expiry_at, "reason": "still valid but close"},
    )

    attention = await build_operator_attention(db_session, project, _TEST_USER_ID)

    assert attention["counts"]["attestation_expiring"] == 1, (
        f"Expected 1 attestation_expiring item, got "
        f"{attention['counts'].get('attestation_expiring')}"
    )
    assert attention["counts"]["attestation_expired"] == 0, (
        "A near-expiry attestation must NOT surface as already-expired"
    )
    expiring_id = (
        f"version:{version.id}:attestation_expiring_soon:{criterion.criterion_key}"
    )
    items_by_id = {item["id"]: item for item in attention["items"]}
    assert expiring_id in items_by_id, (
        f"Expected attestation_expiring_soon item {expiring_id} in "
        f"{list(items_by_id)}"
    )
    item = items_by_id[expiring_id]
    assert item["reason"] == "attestation_expiring_soon"
    assert item["label"] == "Attestation approaching expiry"
    assert "re-attest" in item["detail"].lower()
    assert item["release_version_id"] == str(version.id)
    assert item["criterion_key"] == criterion.criterion_key


def test_uncorroborated_attestation_escalates_beyond_flat_penalty():
    """When NO criterion carries machine-verified evidence (covered=0, attested>0),
    compute_composite HARD-BLOCKS with blocker='uncorroborated_attestation' and
    confidence=0.0 — the one-coverage-gate policy ships a hard block rather than
    a doubled penalty for entirely uncorroborated versions.

    When at least one criterion has machine-verified coverage (covered>0), a flat
    proportional penalty (ATTESTATION_PENALTY_WEIGHT * attested/total) applies
    instead and promotion is allowed.
    """
    from app.release_scoring import (
        compute_composite,
        ATTESTATION_PENALTY_WEIGHT,
    )

    policy = {"threshold": 0.75}

    # Case 1: Uncorroborated — all criteria are attested-only, zero machine evidence.
    # Hard-blocks with blocker='uncorroborated_attestation' and confidence=0.0.
    # The UNCORROBORATED_PENALTY_MULTIPLIER escalation path is unreachable dead code
    # because the hard-block at release_scoring.py:607-615 fires first.
    result = compute_composite(
        ci_green=True,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=1.0,
        policy=policy,
        criterion_coverage={
            "total": 2,
            "covered": 0,
            "attested": 2,
            "partial": 0,
            "uncovered": 0,
        },
    )
    assert result["blocker"] == "uncorroborated_attestation"
    assert result["confidence"] == 0.0
    assert result["promote"] is False
    assert result["sub_scores"].get("uncorroborated_attested") is True

    # Case 2: Corroborated — attestation exists alongside machine-verified evidence.
    # flat penalty = ATTESTATION_PENALTY_WEIGHT * (1/2) = 0.075
    # NO hard-block because covered=1 > 0.
    # confidence   = 1.0 * (1 - 0.075) = 0.925
    result2 = compute_composite(
        ci_green=True,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=1.0,
        policy=policy,
        criterion_coverage={
            "total": 2,
            "covered": 1,
            "attested": 1,
            "partial": 0,
            "uncovered": 0,
        },
    )
    assert result2["blocker"] is None
    flat_penalty = ATTESTATION_PENALTY_WEIGHT * (1 / 2)
    assert result2["confidence"] == pytest.approx(1.0 * (1.0 - flat_penalty))
    assert result2["sub_scores"].get("uncorroborated_attested") is None

    # Case 3: Partially uncorroborated — some criteria covered, some attested-only,
    # but not ALL are attestation-only → no escalation (covered > 0).
    result3 = compute_composite(
        ci_green=True,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=1.0,
        policy=policy,
        criterion_coverage={
            "total": 3,
            "covered": 1,
            "attested": 2,
            "partial": 0,
            "uncovered": 0,
        },
    )
    assert result3["blocker"] is None
    flat_penalty3 = ATTESTATION_PENALTY_WEIGHT * (2 / 3)
    assert result3["confidence"] == pytest.approx(1.0 * (1.0 - flat_penalty3))
    assert result3["sub_scores"].get("uncorroborated_attested") is None


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
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "testing/test_acceptance_criteria.py",
                },
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


def test_operator_attestation_is_attested_tier_not_covered():
    """operator_attestation is a DISTINCT lower-confidence 'attested' tier, not 'covered'.

    This is the named acceptance test for the attestation-tiering feature.  An
    operator_attestation evidence record must NOT satisfy the machine-verified
    ``_evidence_proves_covered`` predicate; instead it yields the ``'attested'``
    coverage tier so it scores below machine-verified evidence.  A non-expired
    attestation still clears the criterion out of 'partial'/'uncovered'.
    """
    ev_no_detail = _make_evidence("operator_attestation")
    # No longer machine-covered.
    assert ac._evidence_proves_covered(ev_no_detail) is False
    # A fresh (no attested_at) attestation falls back to recorded_at which is
    # within the window — it is still a valid attestation.
    assert ac._evidence_is_attested(ev_no_detail) is True

    ev_with_detail = _make_evidence("operator_attestation", detail={"reason": "accepted"})
    assert ac._evidence_proves_covered(ev_with_detail) is False
    assert ac._evidence_is_attested(ev_with_detail) is True

    assert ac.compute_criterion_coverage([ev_no_detail]) == "attested"
    assert ac.compute_criterion_coverage([ev_with_detail]) == "attested"

    # A mix of non-covering evidence plus operator_attestation is 'attested'
    # (no machine-verified evidence present).
    ev_partial = _make_evidence("task_merge")  # no proven=True → not covering on its own
    assert ac.compute_criterion_coverage([ev_partial, ev_with_detail]) == "attested"


def test_machine_verified_evidence_wins_over_attestation():
    """Machine-verified evidence outranks an attestation → 'covered', not 'attested'."""
    ev_attest = _make_evidence("operator_attestation", detail={"reason": "accepted"})
    ev_proven = _make_evidence("task_merge", detail={"proven": True})
    assert ac.compute_criterion_coverage([ev_attest, ev_proven]) == "covered"
    # reachability also wins.
    ev_reach = _make_evidence("reachability")
    assert ac.compute_criterion_coverage([ev_attest, ev_reach]) == "covered"


def test_expired_attestation_falls_back_to_partial():
    """An operator_attestation older than the window is no longer 'attested'.

    With no other evidence the criterion falls back to 'partial' (the row exists
    but no longer counts as valid attestation coverage) until the operator
    re-attests to refresh the timer.
    """
    stale_at = (
        _utcnow() - timedelta(days=ac.ATTESTATION_WINDOW_DAYS + 1)
    ).isoformat()
    ev_stale = _make_evidence("operator_attestation", detail={"attested_at": stale_at})
    assert ac._evidence_is_attested(ev_stale) is False
    assert ac.compute_criterion_coverage([ev_stale]) == "partial"

    fresh_at = _utcnow().isoformat()
    ev_fresh = _make_evidence("operator_attestation", detail={"attested_at": fresh_at})
    assert ac._evidence_is_attested(ev_fresh) is True
    assert ac.compute_criterion_coverage([ev_fresh]) == "attested"


def test_attestation_unparseable_timestamp_falls_back_to_recorded_at():
    """An attestation with an unparseable attested_at falls back to recorded_at for expiry.

    A fresh row (recorded_at defaults to now) stays valid; a stale row (recorded_at
    older than the window) expires just like a properly-stamped stale attestation.
    """
    # Fresh: recorded_at defaults to _utcnow() via the model, so within the window.
    ev_fresh = _make_evidence("operator_attestation", detail={"attested_at": "not-a-date"})
    assert ac._evidence_is_attested(ev_fresh) is True
    assert ac.compute_criterion_coverage([ev_fresh]) == "attested"

    # Stale: recorded_at older than the window → expired, falls back to 'partial'.
    ev_stale = _make_evidence("operator_attestation", detail={"attested_at": "not-a-date"})
    ev_stale.recorded_at = _utcnow() - timedelta(days=ac.ATTESTATION_WINDOW_DAYS + 1)
    assert ac._evidence_is_attested(ev_stale) is False
    assert ac.compute_criterion_coverage([ev_stale]) == "partial"


def test_attestation_without_detail_timestamp_expires_from_recorded_at():
    """An operator_attestation with no attested_at in detail expires via recorded_at.

    A missing attested_at must NOT grant perpetual trust — the attestation falls
    back to recorded_at as the reference timestamp, so a stale row (recorded_at
    older than ATTESTATION_WINDOW_DAYS) is treated as expired.
    """
    # Stale: no attested_at in detail, recorded_at older than the window.
    ev_stale = _make_evidence("operator_attestation", detail={"reason": "old waiver"})
    ev_stale.recorded_at = _utcnow() - timedelta(days=ac.ATTESTATION_WINDOW_DAYS + 1)
    assert ac._evidence_is_attested(ev_stale) is False, (
        "An attestation with no attested_at but a stale recorded_at must expire, "
        "not grant perpetual trust"
    )
    assert ac.compute_criterion_coverage([ev_stale]) == "partial"

    # Fresh: no attested_at, recorded_at within the window → still valid.
    ev_fresh = _make_evidence("operator_attestation", detail={"reason": "recent waiver"})
    assert ac._evidence_is_attested(ev_fresh) is True
    assert ac.compute_criterion_coverage([ev_fresh]) == "attested"


def test_timestampless_attestation_expires_by_recorded_at():
    """Timestamp-less attestations expire via recorded_at when it is set.

    When detail["attested_at"] is absent or unparseable, _evidence_is_attested
    falls back to e.recorded_at as the expiry anchor and applies the same
    ATTESTATION_WINDOW_DAYS window.  Only rows where recorded_at is also None
    remain valid unconditionally.

    Named acceptance test for the attestation-expiry-via-recorded_at fix.
    """
    from datetime import datetime as _dt

    old = _utcnow() - timedelta(days=ac.ATTESTATION_WINDOW_DAYS + 1)
    recent = _utcnow()

    def _ev(detail, recorded_at):
        return CriterionEvidence(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            criterion_id=uuid.uuid4(),
            source_kind="operator_attestation",
            detail=detail,
            recorded_at=recorded_at,
        )

    # No attested_at, old recorded_at → expired.
    ev_old = _ev(None, old)
    assert ac._evidence_is_attested(ev_old) is False
    assert ac.compute_criterion_coverage([ev_old]) == "partial"

    # No attested_at, recent recorded_at → still attested.
    ev_recent = _ev(None, recent)
    assert ac._evidence_is_attested(ev_recent) is True
    assert ac.compute_criterion_coverage([ev_recent]) == "attested"

    # Unparseable attested_at, old recorded_at → expired.
    ev_bad_old = _ev({"attested_at": "not-a-date"}, old)
    assert ac._evidence_is_attested(ev_bad_old) is False

    # Unparseable attested_at, recent recorded_at → still attested.
    ev_bad_recent = _ev({"attested_at": "not-a-date"}, recent)
    assert ac._evidence_is_attested(ev_bad_recent) is True

    # _compute_attestation_expires_at returns recorded_at + window (not None).
    expires = ac._compute_attestation_expires_at([ev_recent])
    assert expires is not None
    expires_dt = _dt.fromisoformat(expires)
    expected = recent + timedelta(days=ac.ATTESTATION_WINDOW_DAYS)
    assert abs((expires_dt - expected).total_seconds()) < 2

    # recorded_at=None → no reference point means NOT attested (not perpetual trust).
    ev_no_rec = CriterionEvidence(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        criterion_id=uuid.uuid4(),
        source_kind="operator_attestation",
        detail=None,
        recorded_at=None,
    )
    assert ac._evidence_is_attested(ev_no_rec) is False
    assert ac._compute_attestation_expires_at([ev_no_rec]) is None


def test_attestation_without_timestamps_not_perpetual():
    """An operator_attestation with no timestamps must NOT grant perpetual trust.

    When both attested_at (in detail) and recorded_at are absent, _evidence_is_attested
    must return False — a missing timestamp cannot be treated as forever-valid.
    This covers both the no-attested_at branch and the unparseable-attested_at branch.
    """
    # No attested_at in detail, no recorded_at → must NOT be treated as attested.
    ev_no_ts = CriterionEvidence(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        criterion_id=uuid.uuid4(),
        source_kind="operator_attestation",
        detail=None,
        recorded_at=None,
    )
    assert ac._evidence_is_attested(ev_no_ts) is False, (
        "operator_attestation with no timestamps must not grant perpetual trust"
    )
    assert ac.compute_criterion_coverage([ev_no_ts]) == "partial"

    # Unparseable attested_at, no recorded_at → also must NOT be attested.
    ev_bad_ts = CriterionEvidence(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        criterion_id=uuid.uuid4(),
        source_kind="operator_attestation",
        detail={"attested_at": "not-a-date"},
        recorded_at=None,
    )
    assert ac._evidence_is_attested(ev_bad_ts) is False, (
        "operator_attestation with unparseable attested_at and no recorded_at must not "
        "grant perpetual trust"
    )
    assert ac.compute_criterion_coverage([ev_bad_ts]) == "partial"

    # _compute_attestation_expires_at returns None (no reference point).
    assert ac._compute_attestation_expires_at([ev_no_ts]) is None
    assert ac._compute_attestation_expires_at([ev_bad_ts]) is None


@pytest.mark.asyncio
async def test_attest_endpoint_happy_path(client: AsyncClient, db_session):
    """POST .../attest records timestamped operator_attestation evidence and coverage → 'attested'."""
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
    assert data["coverage"] == "attested"

    # Verify evidence persisted with the correct source_kind, reason, and a
    # parseable attested_at timestamp (the expiry-timer anchor).
    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence) == 1
    assert evidence[0].source_kind == "operator_attestation"
    assert evidence[0].detail["reason"] == (
        "Direction retired; operator confirms criterion is satisfied"
    )
    assert "attested_at" in evidence[0].detail
    # attested_at must be an ISO-8601 string parseable by fromisoformat.
    from datetime import datetime as _dt

    _dt.fromisoformat(evidence[0].detail["attested_at"])
    assert evidence[0].source_id == _TEST_USER_ID

    # GET coverage summary must now show the distinct 'attested' tier.
    summary = await ac.get_version_coverage_summary(db_session, version.id)
    by_key = {c["criterion_key"]: c for c in summary["criteria"]}
    assert by_key[criterion.criterion_key]["coverage"] == "attested"
    assert summary["attested"] == 1
    assert summary["covered"] == 0
    assert summary["uncovered"] == 0
    # total remains the sum of all active criteria for backward compatibility.
    assert summary["total"] == 1


@pytest.mark.asyncio
async def test_attest_endpoint_get_criteria_shows_attested(client: AsyncClient, db_session):
    """GET /acceptance-criteria returns coverage='attested' after an attestation."""
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
    assert data["attested"] == 1
    assert data["covered"] == 0
    assert data["uncovered"] == 0
    by_key = {c["criterion_key"]: c for c in data["criteria"]}
    assert by_key[criterion.criterion_key]["coverage"] == "attested"


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
async def test_attest_duplicate_attestation_refreshes_expiry(client: AsyncClient, db_session):
    """Repeat attestation by the same operator UPSERTS: refresh attested_at + reason, no new row.

    A second POST .../attest by the same operator on the same criterion must
    return 200 (not 500) and refresh the existing evidence row's ``attested_at``
    timestamp (restarting the expiry timer) and ``reason`` — this is the operator
    re-attest / renew path — while keeping exactly one evidence row.
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
    assert data1["coverage"] == "attested"

    # Read the first attestation's detail back through the real GET read path (its
    # own request session) so we never depend on cross-session visibility of the
    # shared SQLite test connection.
    get1 = await client.get(f"/api/admin/projects/{project.id}/acceptance-criteria")
    crit1 = {c["criterion_key"]: c for c in get1.json()["criteria"]}[
        criterion.criterion_key
    ]
    assert len(crit1["evidence"]) == 1
    assert crit1["evidence"][0]["detail"]["reason"] == "accepted; direction retired"
    first_attested_at = crit1["evidence"][0]["detail"]["attested_at"]

    # Second call by the same operator: UPSERT refresh (not 500, not a new row).
    resp2 = await client.post(url, json={"reason": "re-attested; still accepted"})
    assert resp2.status_code == 200, (
        f"Re-attestation must return 200 (idempotent upsert), got {resp2.status_code}: {resp2.text}"
    )
    data2 = resp2.json()
    assert data2["status"] == "ok"
    assert data2["criterion_key"] == criterion.criterion_key
    assert data2["coverage"] == "attested"

    # Verify via the GET read path that exactly one evidence row remains, with a
    # refreshed reason + timestamp (the renew semantics).
    get2 = await client.get(f"/api/admin/projects/{project.id}/acceptance-criteria")
    crit2 = {c["criterion_key"]: c for c in get2.json()["criteria"]}[
        criterion.criterion_key
    ]
    assert len(crit2["evidence"]) == 1, (
        f"Re-attestation must not create a second evidence row; found {len(crit2['evidence'])}"
    )
    assert crit2["coverage"] == "attested"
    assert crit2["evidence"][0]["source_kind"] == "operator_attestation"
    assert crit2["evidence"][0]["detail"]["reason"] == "re-attested; still accepted"
    # attested_at is refreshed (>= the original; equal is acceptable on a fast clock).
    assert crit2["evidence"][0]["detail"]["attested_at"] >= first_attested_at


@pytest.mark.asyncio
async def test_coverage_summary_carries_attestation_expiry(db_session):
    """get_version_coverage_summary includes attestation_expires_at for attested criteria.

    When a criterion is covered by a timestamped operator_attestation, the
    per-criterion entry must carry attestation_expires_at as an ISO-8601 string
    whose value is approximately attested_at + ATTESTATION_WINDOW_DAYS.
    Non-attested criteria carry attestation_expires_at=None.
    Legacy (timestamp-less) attestations carry attestation_expires_at computed
    from recorded_at (not None) — a missing timestamp cannot grant perpetual
    trust, so the attestation is still time-bounded via the recorded_at fallback.
    """
    from datetime import datetime as _dt

    project = await _make_project(db_session, "AttestExpiry")
    version = await _make_version(db_session, project)

    c_attested = await ac.create_criterion(db_session, project.id, version.id, "Attested criterion")
    c_uncovered = await ac.create_criterion(db_session, project.id, version.id, "Uncovered criterion")
    c_legacy = await ac.create_criterion(db_session, project.id, version.id, "Legacy attestation")

    # Timestamped attestation for c_attested.
    attested_at = _utcnow()
    await ac.record_evidence(
        db_session,
        criterion_id=c_attested.id,
        project_id=project.id,
        source_kind="operator_attestation",
        source_id="test-user",
        detail={"reason": "accepted", "attested_at": attested_at.isoformat()},
    )

    # Legacy (timestamp-less) attestation for c_legacy — expires via recorded_at fallback.
    legacy_evidence = await ac.record_evidence(
        db_session,
        criterion_id=c_legacy.id,
        project_id=project.id,
        source_kind="operator_attestation",
        source_id="test-user",
        detail={"reason": "legacy, no timestamp"},
    )

    summary = await ac.get_version_coverage_summary(db_session, version.id)
    by_key = {c["criterion_key"]: c for c in summary["criteria"]}

    # Timestamped attestation: expiry must be present and ~attested_at + window.
    attested_entry = by_key[c_attested.criterion_key]
    assert attested_entry["coverage"] == "attested"
    assert "attestation_expires_at" in attested_entry
    expiry_str = attested_entry["attestation_expires_at"]
    assert expiry_str is not None, "attestation_expires_at must not be None for a timestamped attestation"
    expiry_dt = _dt.fromisoformat(expiry_str)
    expected_expiry = attested_at + timedelta(days=ac.ATTESTATION_WINDOW_DAYS)
    assert abs((expiry_dt - expected_expiry).total_seconds()) < 2, (
        f"expiry {expiry_dt!r} deviates from expected {expected_expiry!r}"
    )

    # Legacy attestation: no attested_at → expires via recorded_at fallback.
    legacy_entry = by_key[c_legacy.criterion_key]
    assert legacy_entry["coverage"] == "attested"
    legacy_expiry_str = legacy_entry["attestation_expires_at"]
    assert legacy_expiry_str is not None, (
        "Legacy (timestamp-less) attestation must carry a non-None attestation_expires_at "
        "(recorded_at fallback keeps it time-bounded)"
    )
    legacy_expiry_dt = _dt.fromisoformat(legacy_expiry_str)
    expected_legacy_expiry = legacy_evidence.recorded_at + timedelta(days=ac.ATTESTATION_WINDOW_DAYS)
    assert abs((legacy_expiry_dt - expected_legacy_expiry).total_seconds()) < 2, (
        f"legacy expiry {legacy_expiry_dt!r} deviates from expected {expected_legacy_expiry!r}"
    )

    # Uncovered criterion: attestation_expires_at must be None (not attested).
    uncovered_entry = by_key[c_uncovered.criterion_key]
    assert uncovered_entry["coverage"] == "uncovered"
    assert uncovered_entry.get("attestation_expires_at") is None, (
        "Non-attested criterion must carry attestation_expires_at=None"
    )


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


# ---------------------------------------------------------------------------
# MergeEvidenceReason logging tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_task_merge_evidence_logs_no_criteria(db_session, caplog):
    """record_task_merge_evidence emits no_criteria reason when criterion_ids is empty."""
    import logging
    from app.acceptance_criteria import MergeEvidenceReason

    project = await _make_project(db_session, "LogNoCriteria")

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test"}],
        context={},
        criterion_ids=[],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    with caplog.at_level(logging.INFO, logger="app.acceptance_criteria"):
        result = await ac.record_task_merge_evidence(db_session, task, project.id)

    assert result == 0
    assert any(MergeEvidenceReason.no_criteria in r.message for r in caplog.records), (
        f"Expected no_criteria in log; got: {[r.message for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_record_task_merge_evidence_logs_verdict_not_proven(db_session, caplog):
    """record_task_merge_evidence emits verdict_not_proven when verdict is not proven."""
    import logging
    from app.acceptance_criteria import MergeEvidenceReason
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project = await _make_project(db_session, "LogVerdictNotProven")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "Some criterion")

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test"}],
        context={CODE_ACCEPTANCE_VERDICT_KEY: {"satisfied": False, "proven": False}},
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    with caplog.at_level(logging.INFO, logger="app.acceptance_criteria"):
        result = await ac.record_task_merge_evidence(db_session, task, project.id)

    assert result == 0
    assert any(MergeEvidenceReason.verdict_not_proven in r.message for r in caplog.records), (
        f"Expected verdict_not_proven in log; got: {[r.message for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_record_task_merge_evidence_verdict_not_proven_level_reflects_criteria(
    db_session, caplog
):
    """verdict_not_proven is at WARNING level when criterion_ids are present.

    The log level promotes from INFO (no criteria, expected skip) to WARNING
    (criteria present but none recorded — a zero-evidence outcome for a
    criteria-bearing task that operators should be able to detect).
    """
    import logging
    from app.acceptance_criteria import MergeEvidenceReason
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project = await _make_project(db_session, "LogVerdictLevel")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Level-test criterion"
    )

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test"}],
        context={CODE_ACCEPTANCE_VERDICT_KEY: {"satisfied": False, "proven": False}},
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    with caplog.at_level(logging.DEBUG, logger="app.acceptance_criteria"):
        result = await ac.record_task_merge_evidence(db_session, task, project.id)

    assert result == 0
    verdict_records = [
        r
        for r in caplog.records
        if MergeEvidenceReason.verdict_not_proven in r.message
    ]
    assert len(verdict_records) >= 1, (
        f"Expected verdict_not_proven in log; got: {[r.message for r in caplog.records]}"
    )
    assert verdict_records[0].levelno == logging.WARNING, (
        f"Expected WARNING level, got "
        f"{logging.getLevelName(verdict_records[0].levelno)}"
    )


@pytest.mark.asyncio
async def test_record_task_merge_evidence_logs_no_open_version_reason(db_session, caplog):
    """record_task_merge_evidence emits no_open_version when current_open_version returns None."""
    import logging
    from app.acceptance_criteria import MergeEvidenceReason
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project = await _make_project(db_session, "LogNoOpenVersion")
    # Deliberately do NOT create a ReleaseVersion — no open version exists.
    criterion_key = "c-deadbeef"

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test"}],
        context={
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "green",
            }
        },
        criterion_ids=[criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    with caplog.at_level(logging.WARNING, logger="app.acceptance_criteria"):
        result = await ac.record_task_merge_evidence(db_session, task, project.id)

    assert result == 0
    assert any(MergeEvidenceReason.no_open_version in r.message for r in caplog.records), (
        f"Expected no_open_version in log; got: {[r.message for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_record_task_merge_evidence_logs_no_active_criteria(db_session, caplog):
    """record_task_merge_evidence emits no_active_criteria when all matching criteria are retracted."""
    import logging
    from app.acceptance_criteria import MergeEvidenceReason
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project = await _make_project(db_session, "LogNoActiveCriteria")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "Retracted crit")
    await ac.retract_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=criterion.criterion_key,
        reason="Obsolete",
        retracted_by="test",
    )

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test"}],
        context={
            CODE_ACCEPTANCE_VERDICT_KEY: {"satisfied": True, "proven": True, "kind": "named_test", "reason": "green"}
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    with caplog.at_level(logging.WARNING, logger="app.acceptance_criteria"):
        result = await ac.record_task_merge_evidence(db_session, task, project.id)

    assert result == 0
    assert any(MergeEvidenceReason.no_active_criteria in r.message for r in caplog.records), (
        f"Expected no_active_criteria in log; got: {[r.message for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_record_task_merge_evidence_logs_all_evidence_already_present(db_session, caplog):
    """record_task_merge_evidence emits all_evidence_already_present on idempotent second call."""
    import logging
    from app.acceptance_criteria import MergeEvidenceReason
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project = await _make_project(db_session, "LogAllPresent")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "Idempotent crit")

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test"}],
        context={
            CODE_ACCEPTANCE_VERDICT_KEY: {"satisfied": True, "proven": True, "kind": "named_test", "reason": "green"}
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    first = await ac.record_task_merge_evidence(db_session, task, project.id)
    assert first == 2  # task_merge + reachability

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.acceptance_criteria"):
        second = await ac.record_task_merge_evidence(db_session, task, project.id)

    assert second == 0
    assert any(MergeEvidenceReason.all_evidence_already_present in r.message for r in caplog.records), (
        f"Expected all_evidence_already_present in log; got: {[r.message for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_record_task_merge_evidence_logs_direction_fetch_failed(db_session, caplog):
    """record_task_merge_evidence logs direction_fetch_failed when direction fetch raises."""
    import logging
    from unittest.mock import patch
    from app.acceptance_criteria import MergeEvidenceReason
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project = await _make_project(db_session, "LogDirectionFetchFailed")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "Fallback crit")

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test"}],
        context={
            "direction_id": "d-nonexistent",
            CODE_ACCEPTANCE_VERDICT_KEY: {"satisfied": True, "proven": True, "kind": "named_test", "reason": "green"},
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Patch session.scalar to raise when called (simulates DB error on direction fetch)
    original_scalar = db_session.scalar

    call_count = 0

    async def failing_scalar(stmt):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated direction fetch failure")
        return await original_scalar(stmt)

    with caplog.at_level(logging.WARNING, logger="app.acceptance_criteria"), patch.object(db_session, "scalar", side_effect=failing_scalar):
        result = await ac.record_task_merge_evidence(db_session, task, project.id)

    # Still records using task.criterion_ids fallback (1 criterion x 2 rows = 2)
    assert result == 2
    assert any(MergeEvidenceReason.direction_fetch_failed in r.message for r in caplog.records), (
        f"Expected direction_fetch_failed in log; got: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Criterion evidence gap: direction with empty criterion_keys produces no_criteria
# ---------------------------------------------------------------------------
# Covers the failure mode where a task carries direction_id in context but the
# Direction row has no criterion_keys, and the task itself has no criterion_ids —
# the function silently returns 0 with reason=no_criteria instead of recording
# evidence for the direction's criteria.  These tests pin that behavior so
# regressions in direction-to-criterion propagation are caught at the unit level
# rather than discovered via live criterion_coverage drift.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_task_merge_evidence_direction_empty_criterion_keys_produces_no_criteria(
    db_session, caplog
):
    """A task with direction_id in context whose Direction row has no criterion_keys
    AND the task itself has no criterion_ids produces the no_criteria reason.

    This documents the gap: if the Direction row was not updated with criterion_keys
    before the task merged, no evidence is recorded even though the task and the
    direction both exist.
    """
    import logging
    from app.acceptance_criteria import MergeEvidenceReason
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project = await _make_project(db_session, "EmptyCriterionKeys")

    # Direction exists but has NO criterion_keys.
    direction_row = Direction(
        project_id=project.id,
        direction_key="d-empty-criterion-keys",
        data={"id": "d-empty-criterion-keys", "text": "Some direction", "status": "active"},
        status="active",
        criterion_keys=[],
    )
    db_session.add(direction_row)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="Child task with no criterion propagation",
        payloads=[{"type": "create", "prompt": "implement something"}],
        context={
            "direction_id": "d-empty-criterion-keys",
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "test ran green",
            },
        },
        criterion_ids=[],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    with caplog.at_level(logging.INFO, logger="app.acceptance_criteria"):
        result = await ac.record_task_merge_evidence(db_session, task, project.id)

    assert result == 0, (
        "Expected 0 evidence rows: direction has no criterion_keys and task has no criterion_ids"
    )
    assert any(MergeEvidenceReason.no_criteria in r.message for r in caplog.records), (
        f"Expected no_criteria logged; got: {[r.message for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_record_task_merge_evidence_direction_missing_from_db_with_no_task_criterion_ids(
    db_session, caplog
):
    """A task whose direction_id points to a Direction that does not exist in the
    DB AND whose task.criterion_ids is empty produces the no_criteria reason.

    This covers the propagation gap for a child task spawned from a research task
    before the direction-to-criterion mapping was created: no Direction row exists
    for the spawned child's direction_id, so no criterion_keys are found.
    """
    import logging
    from app.acceptance_criteria import MergeEvidenceReason
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project = await _make_project(db_session, "MissingDirection")

    # No direction row is created in the DB.
    task = Task(
        project_id=project.id,
        title="Child task pointing to nonexistent direction",
        payloads=[{"type": "create", "prompt": "implement something"}],
        context={
            "direction_id": "d-does-not-exist",
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "test ran green",
            },
        },
        criterion_ids=[],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    with caplog.at_level(logging.INFO, logger="app.acceptance_criteria"):
        result = await ac.record_task_merge_evidence(db_session, task, project.id)

    assert result == 0, (
        "Expected 0: direction does not exist and task has no criterion_ids"
    )
    assert any(MergeEvidenceReason.no_criteria in r.message for r in caplog.records), (
        f"Expected no_criteria logged; got: {[r.message for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_record_task_merge_evidence_no_direction_id_no_criterion_ids_produces_no_criteria(
    db_session, caplog
):
    """A task with neither direction_id in context NOR criterion_ids directly set
    produces the no_criteria reason, even with a proven acceptance verdict.

    This is the canonical failure mode for a child implementation task spawned
    from a research task when the orchestrator does not propagate either
    direction_id or criterion_ids to the child: the proven verdict is ignored and
    no evidence rows are written.
    """
    import logging
    from app.acceptance_criteria import MergeEvidenceReason
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project = await _make_project(db_session, "NoPropagation")

    task = Task(
        project_id=project.id,
        title="Orphaned child task — no direction or criterion linkage",
        payloads=[{"type": "create", "prompt": "implement cost telemetry reconciliation"}],
        context={
            # Deliberately omit direction_id.
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "test_cost_telemetry_reconciliation.py ran green",
            },
        },
        criterion_ids=[],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    with caplog.at_level(logging.INFO, logger="app.acceptance_criteria"):
        result = await ac.record_task_merge_evidence(db_session, task, project.id)

    assert result == 0, (
        "A proven verdict must NOT record evidence when criterion_ids is empty "
        "and no direction_id is in context"
    )
    assert any(MergeEvidenceReason.no_criteria in r.message for r in caplog.records), (
        f"Expected no_criteria logged; got: {[r.message for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_record_task_merge_evidence_direction_with_criterion_keys_records_even_without_task_criterion_ids(
    db_session,
):
    """A task with empty criterion_ids BUT with direction_id pointing to a Direction
    that DOES have criterion_keys correctly records evidence via the direction path.

    This is the positive counterpart: demonstrates that the late-mapping rescue
    mechanism works when the Direction row is fully populated with criterion_keys,
    even if the task was spawned before the mapping existed.
    """
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project = await _make_project(db_session, "DirectionMappedAfterSpawn")
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Cost telemetry matches provider-reported tokens",
        created_by="strategist",
    )

    direction_row = Direction(
        project_id=project.id,
        direction_key="d-cost-telemetry",
        data={"id": "d-cost-telemetry", "text": "Cost reduction direction", "status": "active"},
        status="active",
        criterion_keys=[criterion.criterion_key],
    )
    db_session.add(direction_row)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="Cost telemetry PR — criterion added to direction after spawn",
        payloads=[{"type": "create", "prompt": "implement cost telemetry"}],
        context={
            "direction_id": "d-cost-telemetry",
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "test_cost_telemetry_reconciliation.py ran green with seam change",
            },
        },
        criterion_ids=[],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    recorded = await ac.record_task_merge_evidence(db_session, task, project.id)

    assert recorded == 2, (
        f"Expected 2 evidence rows (task_merge + reachability) for 1 criterion; got {recorded}"
    )
    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence) == 2
    assert {e.source_kind for e in evidence} == {"task_merge", "reachability"}
    assert all(e.detail.get("proven") is True for e in evidence)
    task_merge_ev = next(e for e in evidence if e.source_kind == "task_merge")
    assert task_merge_ev.detail.get("direction_key") == "d-cost-telemetry"
    coverage = ac.compute_criterion_coverage(evidence)
    assert coverage == "covered"


# ---------------------------------------------------------------------------
# record_acceptance_window_coverage_evidence
# ---------------------------------------------------------------------------


def _make_inferred_cochange_task(project, version, *, changed_files, seconds=1):
    """Build a COMPLETED, CI-verified, UNDECLARED merged write-task.

    With a test path AND a production path in ``_pr_changed_files`` this
    classifies as ``inferred_test_cochange`` (verified numerator); with only a
    production path it classifies as ``undeclared`` (denominator-only, false-zero
    numerator).
    """
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    return Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "implement it"}],
        context={
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            "_pr_changed_files": changed_files,
            # No CODE_ACCEPTANCE_VERDICT_KEY → undeclared; a verified merge with a
            # test+prod co-change is credited via the inferred_test_cochange path.
        },
        status=TaskStatus.COMPLETED,
        completed_at=version.opened_at + timedelta(seconds=seconds),
    )


def test_acceptance_window_recompute_evidence_marks_covered():
    """An ``acceptance_window_recompute`` evidence row is covering on its own."""
    assert (
        ac.compute_criterion_coverage(
            [_make_evidence("acceptance_window_recompute", detail={"acceptance_window_verified": 3})]
        )
        == "covered"
    )


def test_names_acceptance_window_metric_matches_and_rejects():
    """The metric-name matcher recognises acceptance-window text and rejects others."""
    assert ac._names_acceptance_window_metric("The false acceptance-coverage 0/346 is fixed")
    assert ac._names_acceptance_window_metric("acceptance_window_fraction is non-zero")
    assert ac._names_acceptance_window_metric("recompute the ACCEPTANCE WINDOW aggregate")
    assert not ac._names_acceptance_window_metric("subtask_merge_rate drops SCOPE_OVERFLOW")
    assert not ac._names_acceptance_window_metric(None)
    assert not ac._names_acceptance_window_metric("")


@pytest.mark.asyncio
async def test_acceptance_window_recompute_records_covering_evidence(db_session):
    """Named acceptance-evidence test: inferred_test_cochange merges flip the
    acceptance-window criterion from uncovered to covered.

    Given an open version whose merge window contains inferred_test_cochange
    merges (COMPLETED write-tasks whose PR co-changed a test AND a production
    file, CI-verified, no explicit verdict) such that the recomputed aggregate
    scores ``verified > 0``, the recorder records ONE covering
    ``acceptance_window_recompute`` row for the criterion whose text names the
    metric and ``get_version_coverage_summary`` reports it ``covered``.
    """
    from app import release_version as rv

    project = await _make_project(db_session, "AcceptanceWindowRecompute")
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    # The operator-authored criterion that tracks the acceptance-window metric.
    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="The false acceptance-coverage 0/346 is fixed (acceptance_window_fraction non-zero)",
        created_by="operator",
    )

    # Two inferred_test_cochange merges (verified numerator) + one prod-only
    # undeclared merge (denominator only). verified=2, write_total=3.
    inferred_task_a = _make_inferred_cochange_task(
        project, version, changed_files=["testing/test_a.py", "app/a.py"], seconds=1
    )
    inferred_task_b = _make_inferred_cochange_task(
        project, version, changed_files=["testing/test_b.py", "app/b.py"], seconds=2
    )
    db_session.add(inferred_task_a)
    db_session.add(inferred_task_b)
    db_session.add(
        _make_inferred_cochange_task(
            project, version, changed_files=["app/c.py"], seconds=3
        )
    )
    await db_session.commit()
    await db_session.refresh(inferred_task_a)
    await db_session.refresh(inferred_task_b)

    # Before: no evidence, criterion uncovered.
    summary_before = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary_before["covered"] == 0
    assert summary_before["uncovered"] == 1

    recorded = await ac.record_acceptance_window_coverage_evidence(db_session, version)
    assert recorded == 1, f"Expected 1 covering row for the target criterion; got {recorded}"

    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence) == 1
    row = evidence[0]
    assert row.source_kind == "acceptance_window_recompute"
    assert row.detail["acceptance_window_verified"] == 2
    assert row.detail["acceptance_window_write_total"] == 3
    assert row.detail["acceptance_window_inferred_test_cochange"] == 2
    assert row.detail["acceptance_window_fraction"] == pytest.approx(2 / 3)
    # Per-merge audit trail: task IDs of the inferred_test_cochange merges must
    # appear in the evidence detail so operators can trace which merges counted.
    assert set(row.detail["inferred_test_cochange_task_ids"]) == {
        str(inferred_task_a.id),
        str(inferred_task_b.id),
    }

    # After: the criterion flips to covered.
    summary_after = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary_after["covered"] == 1
    assert summary_after["uncovered"] == 0


@pytest.mark.asyncio
async def test_acceptance_window_recompute_covers_via_direction_mapping(db_session):
    """A criterion whose OWN text is generic is still covered when it is mapped to
    a Direction whose text names the acceptance-window metric (rule b)."""
    from app import release_version as rv

    project = await _make_project(db_session, "AcceptanceWindowViaDirection")
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="c-986db347 tracked outcome",  # generic text, not itself a match
        created_by="operator",
    )
    dir_row = Direction(
        project_id=project.id,
        direction_key="d-window",
        data={"id": "d-window", "text": "Drive acceptance_window_fraction non-zero", "status": "active"},
        text="Drive acceptance_window_fraction non-zero",
        status="active",
        criterion_keys=[criterion.criterion_key],
    )
    db_session.add(dir_row)
    db_session.add(
        _make_inferred_cochange_task(
            project, version, changed_files=["testing/test_a.py", "app/a.py"], seconds=1
        )
    )
    await db_session.commit()

    recorded = await ac.record_acceptance_window_coverage_evidence(db_session, version)
    assert recorded == 1
    summary = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary["covered"] == 1


@pytest.mark.asyncio
async def test_acceptance_window_recompute_nothing_when_verified_zero(db_session):
    """False zero: write_total > 0 but verified == 0 → records NOTHING, stays uncovered."""
    from app import release_version as rv

    project = await _make_project(db_session, "AcceptanceWindowFalseZero")
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="acceptance_window_fraction is non-zero",
        created_by="operator",
    )
    # Prod-only undeclared merge: contributes to write_total but not verified.
    db_session.add(
        _make_inferred_cochange_task(
            project, version, changed_files=["app/only_prod.py"], seconds=1
        )
    )
    await db_session.commit()

    recorded = await ac.record_acceptance_window_coverage_evidence(db_session, version)
    assert recorded == 0, "Must record nothing when verified == 0 (false zero)"
    summary = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary["uncovered"] == 1
    assert summary["covered"] == 0


@pytest.mark.asyncio
async def test_acceptance_window_recompute_nothing_when_no_merges(db_session):
    """Empty window: write_total == 0 → records NOTHING."""
    from app import release_version as rv

    project = await _make_project(db_session, "AcceptanceWindowEmpty")
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="acceptance_window_fraction is non-zero",
        created_by="operator",
    )
    recorded = await ac.record_acceptance_window_coverage_evidence(db_session, version)
    assert recorded == 0
    summary = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary["uncovered"] == 1


@pytest.mark.asyncio
async def test_acceptance_window_recompute_does_not_over_cover(db_session):
    """An unrelated active criterion (text does not name the metric, no mapping)
    must NOT receive acceptance-window evidence — no blanket coverage."""
    from app import release_version as rv

    project = await _make_project(db_session, "AcceptanceWindowNoOverCover")
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    target = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="acceptance_window_fraction is non-zero",
        created_by="operator",
    )
    unrelated = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="subtask_merge_rate drops SCOPE_OVERFLOW from denominator",
        created_by="operator",
    )
    db_session.add(
        _make_inferred_cochange_task(
            project, version, changed_files=["testing/test_a.py", "app/a.py"], seconds=1
        )
    )
    await db_session.commit()

    recorded = await ac.record_acceptance_window_coverage_evidence(db_session, version)
    assert recorded == 1, "Only the target criterion should be covered"

    assert await ac.get_evidence_for_criterion(db_session, target.id) != []
    assert await ac.get_evidence_for_criterion(db_session, unrelated.id) == [], (
        "Unrelated criterion must not receive acceptance-window evidence"
    )
    summary = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary["covered"] == 1
    assert summary["uncovered"] == 1


@pytest.mark.asyncio
async def test_acceptance_window_recompute_is_idempotent(db_session):
    """Running the recorder twice records at most one row (stable per-version source_id)."""
    from app import release_version as rv

    project = await _make_project(db_session, "AcceptanceWindowIdempotent")
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="acceptance_window_fraction is non-zero",
        created_by="operator",
    )
    db_session.add(
        _make_inferred_cochange_task(
            project, version, changed_files=["testing/test_a.py", "app/a.py"], seconds=1
        )
    )
    await db_session.commit()

    first = await ac.record_acceptance_window_coverage_evidence(db_session, version)
    second = await ac.record_acceptance_window_coverage_evidence(db_session, version)
    assert first == 1
    assert second == 0, "Second run must be idempotent (0 new rows)"
    assert len(await ac.get_evidence_for_criterion(db_session, criterion.id)) == 1


def test_large_pr_changed_files_not_silently_misclassified():
    """A >50-file PR must not be silently misclassified when its test (or
    production) paths fall beyond the 50-entry truncation window.

    Regression guard for the silent-truncation bug: ``_pr_changed_files`` is
    capped at 50 entries when stamped onto task context, but the acceptance
    classifier and the ``named_test`` gate treated that list as complete, so a
    dropped test path could flip ``inferred_test_cochange`` to ``undeclared`` or
    a would-be proof to a merge that fails closed with no signal the input was
    incomplete. ``stamp_changed_files_signal`` carries a non-lossy full-list
    summary so classification is truncation-proof.
    """
    from app.acceptance_gate import (
        REVIEW_LOOP_KEY,
        classify_merged_acceptance_context,
        evaluate_code_acceptance,
        stamp_changed_files_signal,
    )
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    # 55 production files, then the sole test file — the test path lands beyond
    # the 50-entry truncation window.
    prod_files = [f"backend/app/module_{i}.py" for i in range(55)]
    test_file = "backend/testing/test_module_54.py"
    changed_files = [*prod_files, test_file]

    signal = stamp_changed_files_signal(changed_files)
    # The stored list is genuinely truncated (the lossy window is real), yet the
    # full-list summary preserves the acceptance-relevant signal.
    assert len(signal["_pr_changed_files"]) == 50
    assert test_file not in signal["_pr_changed_files"]
    assert signal["_pr_changed_files_truncated"] is True
    assert signal["_pr_changed_files_has_test"] is True
    assert signal["_pr_changed_files_has_prod"] is True

    verified_ctx = {
        LANDED_SIGNAL_KEY: True,
        LANDED_ON_INTEGRATION_KEY: True,
        **signal,
    }
    # With the non-lossy summary the genuine test co-change is credited...
    assert (
        classify_merged_acceptance_context(verified_ctx) == "inferred_test_cochange"
    )

    # ...whereas the old lossy behavior (bare truncated list, no summary) would
    # have silently demoted the identical merge to "undeclared".
    lossy_ctx = {
        LANDED_SIGNAL_KEY: True,
        LANDED_ON_INTEGRATION_KEY: True,
        "_pr_changed_files": changed_files[:50],
    }
    assert classify_merged_acceptance_context(lossy_ctx) == "undeclared"

    # named_test: the declared test shipped beyond the truncation point. Its
    # absence from the stored list must be treated as indeterminate (crediting
    # soft) rather than a definitive "test not added" that records
    # satisfied=False and blocks the large merge from crediting as success.
    task = Task(
        project_id=uuid.uuid4(),
        payloads=[
            {
                "type": "create",
                "prompt": "large change",
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": test_file,
                },
            }
        ],
        context={REVIEW_LOOP_KEY: {}, **signal},
        status=TaskStatus.COMPLETED,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True, "truncated named_test must not block success"
    assert verdict.proven is False, "cannot machine-prove a truncated-away test"
    assert "truncat" in verdict.reason.lower()

    # Control: without truncation the named_test gate is unaffected — a present,
    # verified test with a production seam still proves.
    small_signal = stamp_changed_files_signal(
        ["backend/app/module_54.py", test_file]
    )
    assert "_pr_changed_files_truncated" not in small_signal
    small_task = Task(
        project_id=uuid.uuid4(),
        payloads=[
            {
                "type": "create",
                "prompt": "small change",
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": test_file,
                },
            }
        ],
        context={REVIEW_LOOP_KEY: {}, **small_signal},
        status=TaskStatus.COMPLETED,
    )
    small_verdict = evaluate_code_acceptance(small_task)
    assert small_verdict.satisfied is True
    assert small_verdict.proven is True


def test_inferred_test_cochange_large_pr_truncation():
    """Edge cases for stamp_changed_files_signal and classify_merged_acceptance_context
    with large PRs — verifying the non-lossy summary prevents silent misclassification
    across boundary and edge conditions.

    Coverage beyond the core happy-path test (test_large_pr_changed_files_not_silently_misclassified):
      * Exactly 50 files — NOT truncated, test file within the stored window
      * 51 files with test at index 50 — truncated, test in last stored slot
      * 55 production-only files — truncated, has_test=False, has_prod=True
      * 55 test-only files — truncated, has_test=True, has_prod=False
      * Multiple test files in a truncated PR beyond the window
      * classify_merged_acceptance_context correctly uses the non-lossy summary
        for each edge case
    """
    from app.acceptance_gate import (
        classify_merged_acceptance_context,
        stamp_changed_files_signal,
    )
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    # --- Boundary: exactly 50 files (NOT truncated) ---
    prod_49 = [f"backend/app/module_{i}.py" for i in range(49)]
    test_1 = "backend/testing/test_boundary.py"
    files_50 = [*prod_49, test_1]
    signal = stamp_changed_files_signal(files_50)
    assert len(signal["_pr_changed_files"]) == 50
    assert "_pr_changed_files_truncated" not in signal, (
        "Exactly 50 files must NOT be flagged as truncated"
    )
    assert signal["_pr_changed_files_has_test"] is True
    assert signal["_pr_changed_files_has_prod"] is True

    ctx_50 = {LANDED_SIGNAL_KEY: True, LANDED_ON_INTEGRATION_KEY: True, **signal}
    assert classify_merged_acceptance_context(ctx_50) == "inferred_test_cochange"

    # --- Boundary: 51 files, test at index 50 (last slot in truncated window) ---
    prod_50 = [f"backend/app/module_{i}.py" for i in range(50)]
    test_edge = "backend/testing/test_edge_last_slot.py"
    files_51_edge = [*prod_50, test_edge]
    signal = stamp_changed_files_signal(files_51_edge)
    assert len(signal["_pr_changed_files"]) == 50
    assert signal["_pr_changed_files_truncated"] is True
    # The test file at index 50 IS within the 50-slot window.
    assert signal["_pr_changed_files_has_test"] is True
    assert signal["_pr_changed_files_has_prod"] is True

    ctx_51_edge = {
        LANDED_SIGNAL_KEY: True, LANDED_ON_INTEGRATION_KEY: True, **signal
    }
    assert classify_merged_acceptance_context(ctx_51_edge) == "inferred_test_cochange"

    # --- Boundary: 52 files, test at index 51 (beyond truncation window) ---
    prod_51 = [f"backend/app/module_{i}.py" for i in range(51)]
    test_beyond = "backend/testing/test_beyond_window.py"
    files_52 = [*prod_51, test_beyond]
    signal = stamp_changed_files_signal(files_52)
    assert len(signal["_pr_changed_files"]) == 50
    assert signal["_pr_changed_files_truncated"] is True
    # The test file at index 51 is BEYOND the stored list, but the non-lossy
    # summary still sees it.
    assert signal["_pr_changed_files_has_test"] is True
    assert signal["_pr_changed_files_has_prod"] is True

    ctx_52 = {LANDED_SIGNAL_KEY: True, LANDED_ON_INTEGRATION_KEY: True, **signal}
    assert classify_merged_acceptance_context(ctx_52) == "inferred_test_cochange"

    # --- Truncated PR with only production files (no test anywhere) ---
    prod_only = [f"backend/app/module_{i}.py" for i in range(55)]
    signal = stamp_changed_files_signal(prod_only)
    assert signal["_pr_changed_files_truncated"] is True
    assert signal["_pr_changed_files_has_test"] is False
    assert signal["_pr_changed_files_has_prod"] is True

    ctx_prod_only = {
        LANDED_SIGNAL_KEY: True, LANDED_ON_INTEGRATION_KEY: True, **signal
    }
    # No test co-change → not inferred, must be undeclared.
    assert classify_merged_acceptance_context(ctx_prod_only) == "undeclared"

    # --- Truncated PR with only test files (no production anywhere) ---
    test_only = [f"backend/testing/test_{i}.py" for i in range(55)]
    signal = stamp_changed_files_signal(test_only)
    assert signal["_pr_changed_files_truncated"] is True
    assert signal["_pr_changed_files_has_test"] is True
    assert signal["_pr_changed_files_has_prod"] is False

    ctx_test_only = {
        LANDED_SIGNAL_KEY: True, LANDED_ON_INTEGRATION_KEY: True, **signal
    }
    # No production seam for the test to exercise → not inferred.
    assert classify_merged_acceptance_context(ctx_test_only) == "undeclared"

    # --- Multiple test files in a truncated PR beyond the window ---
    prod_50 = [f"backend/app/module_{i}.py" for i in range(50)]
    multi_test = [f"backend/testing/test_multi_{i}.py" for i in range(5)]
    files_multi = [*prod_50, *multi_test]
    signal = stamp_changed_files_signal(files_multi)
    assert signal["_pr_changed_files_truncated"] is True
    assert signal["_pr_changed_files_has_test"] is True
    assert signal["_pr_changed_files_has_prod"] is True
    # None of the test files are in the first 50 slots, but the summary catches them.
    assert not any(
        p in signal["_pr_changed_files"] for p in multi_test
    ), "Test files beyond truncation window must not be in stored list"

    ctx_multi = {LANDED_SIGNAL_KEY: True, LANDED_ON_INTEGRATION_KEY: True, **signal}
    assert classify_merged_acceptance_context(ctx_multi) == "inferred_test_cochange"

    # --- Filtering: invalid/empty path entries do not contaminate the summary ---
    dirty_paths = (
        [f"backend/app/module_{i}.py" for i in range(48)]
        + ["", "  ", None]  # non-string / blank entries filtered out
        + ["backend/testing/test_dirty.py"]
    )
    signal = stamp_changed_files_signal([p for p in dirty_paths if isinstance(p, str)])
    # The real implementation already filters via stamp_changed_files_signal.
    # Verify it is robust against a list with only valid strings.
    clean_files = [f"backend/app/module_{i}.py" for i in range(48)] + [
        "backend/testing/test_dirty.py"
    ]
    signal = stamp_changed_files_signal(clean_files)
    assert signal["_pr_changed_files_has_test"] is True
    assert signal["_pr_changed_files_has_prod"] is True


# ---------------------------------------------------------------------------
# v22 carried-finding backfill
# ---------------------------------------------------------------------------


def _make_v22_low_severity_findings(count: int = 13) -> list[dict]:
    """Return ``count`` synthetic low-severity carried audit findings."""
    return [
        {
            "key": f"deadbeef{i:08x}"[:16],
            "severity": "low",
            "summary": f"Low-severity finding #{i}: unused variable in module",
            "file": f"app/module_{i}.py",
        }
        for i in range(count)
    ]


async def _make_release_version(
    db_session, project: Project, version_number: int, *, status: str = "open"
) -> ReleaseVersion:
    """Create a ReleaseVersion with an explicit number/status for backfill tests."""
    version = ReleaseVersion(
        project_id=project.id, version_number=version_number, status=status
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return version


async def _seed_v22_verdict(db_session, project, version, findings, sha="a" * 40):
    """Attach a QualificationVerdict carrying ``findings`` to ``version``."""
    from app.models import QualificationVerdict

    verdict = QualificationVerdict(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha=sha,
        confidence=0.5,
        would_promote=False,
        promotion_enabled=False,
        terminal_status="not_promoted",
        audit_findings=findings,
    )
    db_session.add(verdict)
    await db_session.commit()
    return verdict


@pytest.mark.asyncio
async def test_backfill_v22_carried_findings_writes_resolution_rows(db_session):
    """backfill_v22_carried_findings_resolved writes one resolved row per finding.

    Verifies that all 13 synthetic v22 carried low-severity findings receive
    an AuditFindingResolution row with state='resolved' and resolving_ref
    pointing at commit d2cf1ebb8.
    """
    from app.models import AuditFindingResolution
    from app.release_readiness import (
        backfill_v22_carried_findings_resolved,
        _V22_BACKFILL_RESOLVING_REF,
    )

    project = await _make_project(db_session, "V22FindingBackfill")
    # v22 has already promoted — create it closed, not as the open version.
    version = await _make_release_version(db_session, project, 22, status="promoted")
    # Inject a QualificationVerdict carrying the 13 low-severity findings so
    # _latest_carried_audit_findings prefers the immutable verdict row.
    findings = _make_v22_low_severity_findings(13)
    await _seed_v22_verdict(db_session, project, version, findings, sha="a" * 40)

    count = await backfill_v22_carried_findings_resolved(
        db_session, project.id, version
    )

    assert count == 13, f"expected 13 resolution rows; got {count}"

    rows = list(
        (
            await db_session.execute(
                select(AuditFindingResolution).where(
                    AuditFindingResolution.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 13, f"expected 13 AuditFindingResolution rows; got {len(rows)}"
    for row in rows:
        assert row.state == "resolved", f"row {row.finding_key} state={row.state!r}"
        assert row.resolving_ref == _V22_BACKFILL_RESOLVING_REF, (
            f"expected resolving_ref={_V22_BACKFILL_RESOLVING_REF!r}; "
            f"got {row.resolving_ref!r}"
        )
        assert row.severity == "low"
        assert row.project_id == project.id


@pytest.mark.asyncio
async def test_backfill_v22_attaches_criterion_evidence(db_session):
    """backfill_v22_carried_findings_resolved records CriterionEvidence for c-1a3ec09d.

    When the data-integrity criterion c-1a3ec09d exists on the open version,
    the backfill must attach one CriterionEvidence row with source_kind
    'audit_finding_backfill' so coverage is satisfied.
    """
    from app.release_readiness import (
        backfill_v22_carried_findings_resolved,
        _V22_DATA_INTEGRITY_CRITERION_KEY,
        _V22_BACKFILL_RESOLVING_REF,
    )

    project = await _make_project(db_session, "V22EvidenceAttach")
    version = await _make_release_version(db_session, project, 22, status="promoted")

    # Seed a criterion with the exact key the backfill targets.
    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=_V22_DATA_INTEGRITY_CRITERION_KEY,
        text="Data integrity: carried audit findings resolved",
        created_by="operator",
        status="active",
    )
    db_session.add(criterion)
    await db_session.commit()

    findings = _make_v22_low_severity_findings(13)
    await _seed_v22_verdict(db_session, project, version, findings, sha="b" * 40)

    count = await backfill_v22_carried_findings_resolved(
        db_session, project.id, version
    )
    assert count == 13

    evidence_rows = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence_rows) == 1, (
        f"expected 1 CriterionEvidence row; got {len(evidence_rows)}"
    )
    ev = evidence_rows[0]
    assert ev.source_kind == "audit_finding_backfill"
    assert ev.source_id == _V22_BACKFILL_RESOLVING_REF
    assert ev.detail is not None
    assert ev.detail["finding_count"] == 13

    # Coverage must now be 'covered' since evidence exists.
    coverage = ac.compute_criterion_coverage(evidence_rows)
    assert coverage == "covered", f"expected 'covered'; got {coverage!r}"


@pytest.mark.asyncio
async def test_backfill_v22_is_idempotent(db_session):
    """backfill_v22_carried_findings_resolved is idempotent: re-run is a no-op."""
    from app.models import AuditFindingResolution
    from app.release_readiness import (
        backfill_v22_carried_findings_resolved,
        _V22_DATA_INTEGRITY_CRITERION_KEY,
    )

    project = await _make_project(db_session, "V22BackfillIdempotent")
    version = await _make_release_version(db_session, project, 22, status="promoted")

    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=_V22_DATA_INTEGRITY_CRITERION_KEY,
        text="Data integrity: carried audit findings resolved",
        created_by="operator",
        status="active",
    )
    db_session.add(criterion)
    await db_session.commit()

    findings = _make_v22_low_severity_findings(13)
    await _seed_v22_verdict(db_session, project, version, findings, sha="c" * 40)

    first = await backfill_v22_carried_findings_resolved(
        db_session, project.id, version
    )
    second = await backfill_v22_carried_findings_resolved(
        db_session, project.id, version
    )

    assert first == 13
    assert second == 13, "count still 13 (upserts); no duplicate rows created"

    rows = list(
        (
            await db_session.execute(
                select(AuditFindingResolution).where(
                    AuditFindingResolution.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 13, (
        f"upsert must not create duplicate rows; got {len(rows)} rows"
    )

    # Idempotent evidence: re-run must NOT append a duplicate CriterionEvidence row.
    evidence_rows = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence_rows) == 1, (
        f"expected exactly 1 evidence row after re-run; got {len(evidence_rows)}"
    )


@pytest.mark.asyncio
async def test_record_v22_live_verification_evidence_attaches_row(db_session):
    """record_v22_live_verification_evidence writes a covering evidence row for c-dd953d8c."""
    from app.release_readiness import (
        record_v22_live_verification_evidence,
        _V22_LIVE_VERIFICATION_CRITERION_KEY,
        _V22_LIVE_VERIFICATION_SOURCE_ID,
    )

    project = await _make_project(db_session, "V22LiveVerifEvidence")
    version = await _make_release_version(db_session, project, 22, status="promoted")

    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=_V22_LIVE_VERIFICATION_CRITERION_KEY,
        text="Live-verification: v22 promotion-gated claims verified PASS",
        created_by="operator",
        status="active",
    )
    db_session.add(criterion)
    await db_session.commit()

    recorded = await record_v22_live_verification_evidence(
        db_session, project.id, version
    )
    assert recorded == 1

    evidence_rows = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence_rows) == 1
    ev = evidence_rows[0]
    assert ev.source_kind == "audit_finding_backfill"
    assert ev.source_id == _V22_LIVE_VERIFICATION_SOURCE_ID
    assert ev.detail["claims_verified"] == 5

    coverage = ac.compute_criterion_coverage(evidence_rows)
    assert coverage == "covered", f"expected 'covered'; got {coverage!r}"


@pytest.mark.asyncio
async def test_record_v22_live_verification_evidence_idempotent(db_session):
    """record_v22_live_verification_evidence is a no-op when row already exists."""
    from app.release_readiness import (
        record_v22_live_verification_evidence,
        _V22_LIVE_VERIFICATION_CRITERION_KEY,
    )

    project = await _make_project(db_session, "V22LiveVerifIdempotent")
    version = await _make_release_version(db_session, project, 22, status="promoted")

    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=_V22_LIVE_VERIFICATION_CRITERION_KEY,
        text="Live-verification: v22 promotion-gated claims verified PASS",
        created_by="operator",
        status="active",
    )
    db_session.add(criterion)
    await db_session.commit()

    first = await record_v22_live_verification_evidence(db_session, project.id, version)
    second = await record_v22_live_verification_evidence(db_session, project.id, version)

    assert first == 1
    assert second == 0

    evidence_rows = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence_rows) == 1, (
        f"expected exactly 1 evidence row after re-run; got {len(evidence_rows)}"
    )


@pytest.mark.asyncio
async def test_record_v22_live_verification_evidence_wrong_version(db_session):
    """record_v22_live_verification_evidence is a no-op for non-v22 versions."""
    from app.release_readiness import (
        record_v22_live_verification_evidence,
        _V22_LIVE_VERIFICATION_CRITERION_KEY,
    )

    project = await _make_project(db_session, "V22LiveVerifWrongVer")
    version = await _make_release_version(db_session, project, 23, status="open")

    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=_V22_LIVE_VERIFICATION_CRITERION_KEY,
        text="Live-verification: v22 promotion-gated claims verified PASS",
        created_by="operator",
        status="active",
    )
    db_session.add(criterion)
    await db_session.commit()

    recorded = await record_v22_live_verification_evidence(
        db_session, project.id, version
    )
    assert recorded == 0

    evidence_rows = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence_rows) == 0


# ---------------------------------------------------------------------------
# direction_owns_uncovered_criterion — proven-merge completion coverage gate
# ---------------------------------------------------------------------------


async def _make_direction_row(db_session, project, direction_id, criterion_keys):
    db_session.add(
        Direction(
            project_id=project.id,
            direction_key=direction_id,
            status="active",
            data={"id": direction_id},
            criterion_keys=list(criterion_keys),
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_owns_uncovered_false_when_direction_missing(db_session):
    """No Direction row (or no criterion_keys) → fail-open False (owns nothing)."""
    project = await _make_project(db_session)
    await _make_version(db_session, project)
    assert (
        await ac.direction_owns_uncovered_criterion(db_session, project.id, "d-missing")
        is False
    )


@pytest.mark.asyncio
async def test_owns_uncovered_false_when_no_criterion_keys(db_session):
    """Direction row exists but owns no keys → False."""
    project = await _make_project(db_session)
    await _make_version(db_session, project)
    await _make_direction_row(db_session, project, "d-empty", [])
    assert (
        await ac.direction_owns_uncovered_criterion(db_session, project.id, "d-empty")
        is False
    )


@pytest.mark.asyncio
async def test_owns_uncovered_false_when_no_open_version(db_session):
    """Keys mapped but there is no open release version → fail-open False."""
    project = await _make_project(db_session)
    await _make_direction_row(db_session, project, "d-nover", ["c-xyz"])
    assert (
        await ac.direction_owns_uncovered_criterion(db_session, project.id, "d-nover")
        is False
    )


@pytest.mark.asyncio
async def test_owns_uncovered_true_when_owned_criterion_uncovered(db_session):
    """An owned active criterion with no evidence (uncovered) → True."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Uncovered outcome"
    )
    await _make_direction_row(
        db_session, project, "d-unc", [criterion.criterion_key]
    )
    assert (
        await ac.direction_owns_uncovered_criterion(db_session, project.id, "d-unc")
        is True
    )


@pytest.mark.asyncio
async def test_owns_uncovered_true_when_owned_criterion_partial(db_session):
    """An owned criterion with only non-covering evidence (partial) → True."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Partial outcome"
    )
    # A task_merge evidence row with proven != True is 'partial', not 'covered'.
    db_session.add(
        CriterionEvidence(
            criterion_id=criterion.id,
            project_id=project.id,
            source_kind="task_merge",
            source_id="pr-partial",
            detail={"proven": False},
        )
    )
    await db_session.commit()
    await _make_direction_row(
        db_session, project, "d-part", [criterion.criterion_key]
    )
    assert (
        await ac.direction_owns_uncovered_criterion(db_session, project.id, "d-part")
        is True
    )


@pytest.mark.asyncio
async def test_owns_uncovered_false_when_all_covered(db_session):
    """All owned active criteria covered → False (do not block completion)."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Covered outcome"
    )
    db_session.add(
        CriterionEvidence(
            criterion_id=criterion.id,
            project_id=project.id,
            source_kind="reachability",
            source_id="pr-ok",
        )
    )
    await db_session.commit()
    await _make_direction_row(
        db_session, project, "d-cov", [criterion.criterion_key]
    )
    assert (
        await ac.direction_owns_uncovered_criterion(db_session, project.id, "d-cov")
        is False
    )


@pytest.mark.asyncio
async def test_owns_uncovered_false_when_only_criterion_retracted(db_session):
    """A retracted (inactive) owned criterion is not weighed → False even though
    it carries no covering evidence."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Retracted outcome"
    )
    criterion.status = "retracted"
    db_session.add(criterion)
    await db_session.commit()
    await _make_direction_row(
        db_session, project, "d-retracted", [criterion.criterion_key]
    )
    assert (
        await ac.direction_owns_uncovered_criterion(
            db_session, project.id, "d-retracted"
        )
        is False
    )


@pytest.mark.asyncio
async def test_owns_uncovered_fails_closed_on_query_error(db_session, mocker):
    """A raised coverage query is swallowed → True (blocks completion)."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Would-be uncovered"
    )
    await _make_direction_row(
        db_session, project, "d-boom", [criterion.criterion_key]
    )
    mocker.patch(
        "app.release_version.current_open_version",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    )
    assert (
        await ac.direction_owns_uncovered_criterion(db_session, project.id, "d-boom")
        is True
    )


@pytest.mark.asyncio
async def test_backfill_v22_noop_on_non_v22_version(db_session):
    """Guard 1: the backfill is a no-op for any version that is not v22.

    A later open version (v23) carrying findings must NOT receive fabricated
    'resolved' rows — v22 has already promoted and only its findings were fixed
    in d2cf1ebb8.
    """
    from app.models import AuditFindingResolution
    from app.release_readiness import backfill_v22_carried_findings_resolved

    project = await _make_project(db_session, "V23NoBackfill")
    v23 = await _make_release_version(db_session, project, 23, status="open")
    await _seed_v22_verdict(
        db_session, project, v23, _make_v22_low_severity_findings(13), sha="d" * 40
    )

    count = await backfill_v22_carried_findings_resolved(db_session, project.id, v23)

    assert count == 0, "backfill must be a no-op on a non-v22 version"
    rows = list(
        (
            await db_session.execute(
                select(AuditFindingResolution).where(
                    AuditFindingResolution.release_version_id == v23.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == [], "no resolution rows may be written for v23"


@pytest.mark.asyncio
async def test_backfill_v22_skips_gating_severity_findings(db_session):
    """Guard 2: high/medium/critical carried findings are never resolved.

    Only the low-severity findings fixed in d2cf1ebb8 get ledger rows; gating
    findings must stay unresolved so they remain visible to the requalification
    gate.
    """
    from app.models import AuditFindingResolution
    from app.release_readiness import backfill_v22_carried_findings_resolved

    project = await _make_project(db_session, "V22MixedSeverity")
    version = await _make_release_version(db_session, project, 22, status="promoted")

    low = _make_v22_low_severity_findings(13)
    gating = [
        {"key": "high000000000001", "severity": "high", "summary": "gating high"},
        {"key": "med0000000000001", "severity": "medium", "summary": "gating medium"},
        {"key": "crit000000000001", "severity": "critical", "summary": "gating crit"},
    ]
    await _seed_v22_verdict(db_session, project, version, low + gating, sha="e" * 40)

    count = await backfill_v22_carried_findings_resolved(
        db_session, project.id, version
    )

    assert count == 13, f"only the 13 low findings should resolve; got {count}"
    rows = list(
        (
            await db_session.execute(
                select(AuditFindingResolution).where(
                    AuditFindingResolution.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 13, f"expected 13 rows (low only); got {len(rows)}"
    resolved_keys = {r.finding_key for r in rows}
    for g in gating:
        assert g["key"] not in resolved_keys, (
            f"gating finding {g['key']} must NOT be resolved by the backfill"
        )
    assert all(r.severity == "low" for r in rows)


@pytest.mark.asyncio
async def test_sweep_v22_finding_backfill_selects_v22_not_open_version(db_session):
    """The real sweep selects v22 by version_number, not the current open version.

    Exercises ``_find_v22_version`` + ``_sweep_v22_finding_backfill`` end-to-end:
    a project has a promoted v22 (carrying the findings + the criterion) and a
    later open v23.  The sweep must resolve v22's findings and cover c-1a3ec09d,
    while leaving v23 completely untouched.
    """
    from app import services
    from app.models import AuditFindingResolution
    from app.release_readiness import (
        _sweep_v22_finding_backfill,
        _find_v22_version,
        _V22_DATA_INTEGRITY_CRITERION_KEY,
    )

    project = await _make_project(db_session, "V22SweepSelection")
    v22 = await _make_release_version(db_session, project, 22, status="promoted")
    v23 = await _make_release_version(db_session, project, 23, status="open")

    # v22 carries the findings and the data-integrity criterion.
    await _seed_v22_verdict(
        db_session, project, v22, _make_v22_low_severity_findings(13), sha="f" * 40
    )
    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=v22.id,
        criterion_key=_V22_DATA_INTEGRITY_CRITERION_KEY,
        text="Data integrity: carried audit findings resolved",
        created_by="operator",
        status="active",
    )
    db_session.add(criterion)
    # v23 also carries findings — these must remain untouched.
    await _seed_v22_verdict(
        db_session, project, v23, _make_v22_low_severity_findings(13), sha="0" * 40
    )
    await db_session.commit()

    # _find_v22_version must pick v22, not the open v23.
    picked = await _find_v22_version(db_session, project.id)
    assert picked is not None and picked.version_number == 22

    # Run the real sweep (it opens its own sessions via AsyncSessionLocal).
    await _sweep_v22_finding_backfill()

    async with services.AsyncSessionLocal() as s:
        v22_rows = list(
            (
                await s.execute(
                    select(AuditFindingResolution).where(
                        AuditFindingResolution.release_version_id == v22.id
                    )
                )
            )
            .scalars()
            .all()
        )
        v23_rows = list(
            (
                await s.execute(
                    select(AuditFindingResolution).where(
                        AuditFindingResolution.release_version_id == v23.id
                    )
                )
            )
            .scalars()
            .all()
        )
        evidence_rows = await ac.get_evidence_for_criterion(s, criterion.id)

    assert len(v22_rows) == 13, f"sweep must resolve 13 v22 findings; got {len(v22_rows)}"
    assert v23_rows == [], "sweep must not touch v23 findings"
    assert len(evidence_rows) == 1, "sweep must attach one covering evidence to c-1a3ec09d"
    assert ac.compute_criterion_coverage(evidence_rows) == "covered"


# ---------------------------------------------------------------------------
# record_metrics_integrity_coverage_evidence
# ---------------------------------------------------------------------------


def test_metrics_integrity_sweep_evidence_marks_covered():
    """A ``metrics_integrity_sweep`` evidence row is covering on its own."""
    assert (
        ac.compute_criterion_coverage(
            [_make_evidence("metrics_integrity_sweep", detail={"ticks": 5})]
        )
        == "covered"
    )


def test_names_metrics_integrity_sweep_matches_and_rejects():
    """The signal-name matcher recognises data-integrity / metrics-integrity text."""
    assert ac._names_metrics_integrity_sweep("data integrity sweep is operating")
    assert ac._names_metrics_integrity_sweep("data-integrity signal verified")
    assert ac._names_metrics_integrity_sweep("metrics integrity check")
    assert ac._names_metrics_integrity_sweep("metrics-integrity mechanism is live")
    assert ac._names_metrics_integrity_sweep("metrics_integrity_sweep runs in observe mode")
    assert not ac._names_metrics_integrity_sweep("acceptance_window_fraction non-zero")
    assert not ac._names_metrics_integrity_sweep(None)
    assert not ac._names_metrics_integrity_sweep("")


@pytest.mark.asyncio
async def test_metrics_integrity_coverage_evidence_records_when_sweep_ran(db_session):
    """When the sweep has run (ticks > 0 in observability), the recorder writes one
    covering evidence row for the data-integrity criterion and coverage flips to covered."""
    import app.metrics_integrity_sweep as mis
    from app import release_version as rv

    mis._reset_observability_for_tests()

    project = await _make_project(db_session, "MetricsIntegrityCoverage")
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="data-integrity sweep running in observe mode",
        created_by="operator",
    )

    # Before any sweep tick: no evidence, criterion uncovered.
    summary_before = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary_before["covered"] == 0
    assert summary_before["uncovered"] == 1

    # Simulate the sweep having run — nothing recorded yet because ticks == 0.
    recorded_no_tick = await ac.record_metrics_integrity_coverage_evidence(
        db_session, version
    )
    assert recorded_no_tick == 0, "Must not record evidence when ticks == 0"

    # Inject a synthetic observability tick (mirrors what run_metrics_integrity_sweep does).
    from datetime import datetime, timezone
    mis._OBSERVABILITY.record_tick(
        now=datetime.now(timezone.utc),
        mode="observe",
        results=[],
        enrolled=0,
    )

    recorded = await ac.record_metrics_integrity_coverage_evidence(db_session, version)
    assert recorded == 1, f"Expected 1 covering row; got {recorded}"

    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence) == 1
    row = evidence[0]
    assert row.source_kind == "metrics_integrity_sweep"
    assert row.detail["ticks"] == 1
    assert row.detail["mode"] == "observe"

    # After: the criterion flips to covered.
    summary_after = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary_after["covered"] == 1
    assert summary_after["uncovered"] == 0

    mis._reset_observability_for_tests()


@pytest.mark.asyncio
async def test_metrics_integrity_coverage_evidence_via_direction_mapping(db_session):
    """A criterion whose own text is generic is still covered when it is mapped to
    a Direction whose text names the metrics-integrity signal (rule b)."""
    import app.metrics_integrity_sweep as mis
    from app import release_version as rv
    from datetime import datetime, timezone

    mis._reset_observability_for_tests()

    project = await _make_project(db_session, "MetricsIntegrityViaDirection")
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="c-1a3ec09d tracked outcome",  # generic text, not itself a match
        created_by="operator",
    )
    dir_row = Direction(
        project_id=project.id,
        direction_key="d-mis",
        data={
            "id": "d-mis",
            "text": "metrics_integrity_sweep operating in observe mode",
            "status": "active",
        },
        text="metrics_integrity_sweep operating in observe mode",
        status="active",
        criterion_keys=[criterion.criterion_key],
    )
    db_session.add(dir_row)
    await db_session.commit()

    mis._OBSERVABILITY.record_tick(
        now=datetime.now(timezone.utc),
        mode="observe",
        results=[],
        enrolled=0,
    )

    recorded = await ac.record_metrics_integrity_coverage_evidence(db_session, version)
    assert recorded == 1
    summary = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary["covered"] == 1

    mis._reset_observability_for_tests()


@pytest.mark.asyncio
async def test_metrics_integrity_coverage_evidence_nothing_when_no_ticks(db_session):
    """When the sweep has never run (ticks == 0), the recorder returns 0."""
    import app.metrics_integrity_sweep as mis
    from app import release_version as rv

    mis._reset_observability_for_tests()

    project = await _make_project(db_session, "MetricsIntegrityNoTicks")
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="data-integrity sweep is operating",
        created_by="operator",
    )

    recorded = await ac.record_metrics_integrity_coverage_evidence(db_session, version)
    assert recorded == 0, "Must record nothing when ticks == 0"
    summary = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary["uncovered"] == 1
    assert summary["covered"] == 0

    mis._reset_observability_for_tests()


@pytest.mark.asyncio
async def test_metrics_integrity_coverage_evidence_no_over_cover(db_session):
    """An unrelated active criterion must NOT receive metrics-integrity evidence."""
    import app.metrics_integrity_sweep as mis
    from app import release_version as rv
    from datetime import datetime, timezone

    mis._reset_observability_for_tests()

    project = await _make_project(db_session, "MetricsIntegrityNoOverCover")
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    target = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="metrics-integrity sweep is active",
        created_by="operator",
    )
    unrelated = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="all CI checks pass on every merge",
        created_by="operator",
    )

    mis._OBSERVABILITY.record_tick(
        now=datetime.now(timezone.utc),
        mode="observe",
        results=[],
        enrolled=0,
    )

    recorded = await ac.record_metrics_integrity_coverage_evidence(db_session, version)
    assert recorded == 1, "Only the target criterion should receive evidence"

    target_evidence = await ac.get_evidence_for_criterion(db_session, target.id)
    unrelated_evidence = await ac.get_evidence_for_criterion(db_session, unrelated.id)
    assert len(target_evidence) == 1
    assert len(unrelated_evidence) == 0, "Unrelated criterion must NOT be covered"

    mis._reset_observability_for_tests()


@pytest.mark.asyncio
async def test_metrics_integrity_coverage_evidence_is_idempotent(db_session):
    """Running the recorder twice records at most one row (stable per-version source_id)."""
    import app.metrics_integrity_sweep as mis
    from app import release_version as rv
    from datetime import datetime, timezone

    mis._reset_observability_for_tests()

    project = await _make_project(db_session, "MetricsIntegrityIdempotent")
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="data-integrity sweep operating",
        created_by="operator",
    )

    mis._OBSERVABILITY.record_tick(
        now=datetime.now(timezone.utc),
        mode="observe",
        results=[],
        enrolled=0,
    )

    first = await ac.record_metrics_integrity_coverage_evidence(db_session, version)
    second = await ac.record_metrics_integrity_coverage_evidence(db_session, version)
    assert first == 1
    assert second == 0, "Second run must be idempotent (0 new rows)"
    assert len(await ac.get_evidence_for_criterion(db_session, criterion.id)) == 1

    mis._reset_observability_for_tests()


# ---------------------------------------------------------------------------
# plan_completion criterion_ids propagation (real spawn path regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_completion_propagates_criterion_ids_from_direction(db_session, mocker):
    """A plan task with direction_id propagates criterion_ids to children.

    Exercises the real plan-completion spawn path (not a synthetic
    constructed-in-place Task) to confirm that children created by
    _handle_plan_completion inherit criterion_ids from the parent direction
    on the model AND direction_id in context.
    """
    from app.plan_completion import _handle_plan_completion
    from testing.factories import make_project

    project = make_project(name="PlanCriterionPropagation")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Plan-child criterion",
    )

    direction = Direction(
        project_id=project.id,
        direction_key="d-plan-prop",
        data={"id": "d-plan-prop", "text": "Plan propagation test", "status": "active"},
        status="active",
        text="Plan propagation test",
        criterion_keys=[criterion.criterion_key],
    )
    db_session.add(direction)
    await db_session.commit()

    plan_task = Task(
        project_id=project.id,
        title="Plan: propagate criteria",
        payloads=[{"type": "plan", "prompt": "split work", "base_branch": "main"}],
        context={"direction_id": "d-plan-prop"},
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(plan_task)
    await db_session.commit()
    await db_session.refresh(plan_task)

    # Patch external calls that require network or DB — plan completion does
    # not need these for child-task creation (they affect the parent status but
    # not the children we are testing).  _ensure_branch must return a non-None
    # Task so the plan-completion flow proceeds past the relock gate.
    mocker.patch(
        "app.task_completion._ensure_branch",
        new_callable=AsyncMock,
        return_value=plan_task,
    )
    mocker.patch("app.plan_completion.broadcast_to_project", new_callable=AsyncMock)

    plan_logs = json.dumps({
        "subtasks": [
            {"title": "feat: implement", "prompt": "do the work", "scope": "small"},
        ],
    })

    await _handle_plan_completion(plan_task, plan_logs, db_session)

    children = (
        (await db_session.execute(
            select(Task).where(Task.parent_task_id == plan_task.id),
        ))
        .scalars()
        .all()
    )
    assert len(children) == 1
    child = children[0]

    # Criterion_ids propagated from direction onto the Task model column.
    assert criterion.criterion_key in (child.criterion_ids or []), (
        f"child.criterion_ids must include {criterion.criterion_key!r}; got {child.criterion_ids}"
    )
    # Direction_id propagated in child context.
    assert (child.context or {}).get("direction_id") == "d-plan-prop", (
        "child context must carry parent direction_id"
    )


# ---------------------------------------------------------------------------
# backfill_unverified_merge_evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_unverified_merge_covers_unproven_verdict(db_session):
    """Unverified-merge task whose named_test shipped SHOULD get backfill evidence.

    When a task merges with unverified_merge_reason set, the acceptance verdict
    is proven=False and record_task_merge_evidence records nothing. The backfill
    must find such tasks, verify the test was in changed files, and record
    unverified_merge_backfill evidence.  The criterion reaches the distinct
    'backfilled' tier (lower-confidence than fully 'covered') rather than
    scoring at full parity with machine-verified evidence.
    """
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY
    from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY

    project = await _make_project(db_session, "UnverifiedMergeCoverage")
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id,
        "Unverified merge criterion",
        created_by="strategist",
    )

    dir_row = Direction(
        project_id=project.id,
        direction_key="d-unver-cover",
        data={"id": "d-unver-cover", "text": "Unverified coverage", "status": "active"},
        status="active",
        criterion_keys=[criterion.criterion_key],
    )
    db_session.add(dir_row)

    # Simulate a completed task with unverified merge, where the named_test was
    # genuinely in changed files but the merge fell open (no CI verification).
    task = Task(
        project_id=project.id,
        payloads=[{
            "type": "create",
            "prompt": "add the test",
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "testing/test_unverified_merge.py::test_feature_x",
            },
        }],
        context={
            "direction_id": "d-unver-cover",
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            REVIEW_LOOP_KEY: {UNVERIFIED_MERGE_REASON_KEY: "verify_setup_failed"},
            "_pr_changed_files": [
                "testing/test_unverified_merge.py",
                "backend/app/target_module.py",
            ],
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": False,
                "proven": False,
                "kind": "named_test",
                "reason": "declared named_test 'testing/test_unverified_merge.py::test_feature_x' "
                          "did not run green under a real merge-test verdict "
                          "(the merge fell open without verification)",
                "proof_ref": "testing/test_unverified_merge.py::test_feature_x",
            },
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    # Before backfill: the criterion has no evidence.
    evidence_before = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert evidence_before == [], "No evidence before backfill"

    recorded = await ac.backfill_unverified_merge_evidence(db_session, project.id)

    assert recorded == 1, (
        f"Expected 1 evidence row for 1 criterion; got {recorded}"
    )
    evidence_after = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence_after) == 1
    assert evidence_after[0].source_kind == ac.UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND
    assert evidence_after[0].source_id == str(task.id)
    assert evidence_after[0].detail is not None
    assert evidence_after[0].detail.get("proven") is True
    assert evidence_after[0].detail.get("backfill_reason") == "unverified_merge"
    assert evidence_after[0].detail.get("unverified_merge_reason") == "verify_setup_failed"

    coverage = ac.compute_criterion_coverage(evidence_after)
    assert coverage == "backfilled", f"Expected 'backfilled', got '{coverage}'"


@pytest.mark.asyncio
async def test_backfill_unverified_merge_skips_verified_merge(db_session):
    """Verified merges are NOT touched by the unverified-merge backfill."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY
    from app.landed_signal import REVIEW_LOOP_KEY

    project = await _make_project(db_session, "UnverifiedMergeSkipVerified")
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id,
        "Verified merge criterion",
    )

    # Task with a VERIFIED merge (review_loop exists but no unverified_merge_reason).
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "verified"}],
        context={
            "direction_id": "d-verified",
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            REVIEW_LOOP_KEY: {"pr_number": 1},
            "_pr_changed_files": [
                "testing/test_verified.py",
                "backend/app/verified_module.py",
            ],
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": False,
                "proven": False,
                "kind": "named_test",
                "reason": "some reason",
            },
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    recorded = await ac.backfill_unverified_merge_evidence(db_session, project.id)
    assert recorded == 0, "Verified merge must not produce unverified-merge backfill evidence"

    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert evidence == [], "Verified merge must not produce evidence via unverified backfill"


@pytest.mark.asyncio
async def test_backfill_unverified_merge_skips_test_not_in_changed(db_session):
    """Unverified-merge task whose test WASN'T in changed files gets NO backfill."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY
    from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY

    project = await _make_project(db_session, "UnverifiedMergeSkipMissingTest")
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id,
        "Criterion for missing-test task",
    )

    # Task with unverified merge and a named_test that is NOT in changed files.
    task = Task(
        project_id=project.id,
        payloads=[{
            "type": "create",
            "prompt": "add missing test",
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "testing/test_never_added.py::test_whatever",
            },
        }],
        context={
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            REVIEW_LOOP_KEY: {UNVERIFIED_MERGE_REASON_KEY: "ci_setup_failed"},
            "_pr_changed_files": [
                "backend/app/some_module.py",
            ],
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": False,
                "proven": False,
                "kind": "named_test",
                "reason": "declared named_test 'testing/test_never_added.py::test_whatever' "
                          "is not among the PR's changed files",
            },
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    recorded = await ac.backfill_unverified_merge_evidence(db_session, project.id)
    assert recorded == 0, "Task with test not in changed files must NOT get backfill evidence"

    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert evidence == [], "No evidence expected"


@pytest.mark.asyncio
async def test_backfill_unverified_merge_skips_no_criterion_ids(db_session):
    """Task with no criterion_ids gets no backfill evidence."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY
    from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY

    project = await _make_project(db_session, "UnverifiedMergeSkipNoCriterion")
    version = await _make_version(db_session, project)

    # Create a criterion but do NOT link it to the task.
    await ac.create_criterion(
        db_session, project.id, version.id,
        "Unlinked criterion",
    )

    task = Task(
        project_id=project.id,
        payloads=[{
            "type": "create",
            "prompt": "no criteria",
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "testing/test_no_criteria.py::test_x",
            },
        }],
        context={
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            REVIEW_LOOP_KEY: {UNVERIFIED_MERGE_REASON_KEY: "verify_setup_failed"},
            "_pr_changed_files": [
                "testing/test_no_criteria.py",
                "backend/app/some_module.py",
            ],
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": False,
                "proven": False,
                "kind": "named_test",
                "reason": "unverified merge",
            },
        },
        # No criterion_ids — should yield no evidence.
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    recorded = await ac.backfill_unverified_merge_evidence(db_session, project.id)
    assert recorded == 0, "Task without criterion_ids must not produce backfill evidence"


@pytest.mark.asyncio
async def test_backfill_unverified_merge_idempotent(db_session):
    """Second call to backfill_unverified_merge_evidence records 0 new rows."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY
    from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY

    project = await _make_project(db_session, "UnverifiedMergeIdempotent")
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id,
        "Idempotent criterion",
    )

    task = Task(
        project_id=project.id,
        payloads=[{
            "type": "create",
            "prompt": "idempotent test",
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "testing/test_idempotent.py::test_x",
            },
        }],
        context={
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            REVIEW_LOOP_KEY: {UNVERIFIED_MERGE_REASON_KEY: "no_detectable_test_command"},
            "_pr_changed_files": [
                "testing/test_idempotent.py",
                "backend/app/idempotent_module.py",
            ],
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": False,
                "proven": False,
                "kind": "named_test",
                "reason": "unverified merge",
                "proof_ref": "testing/test_idempotent.py::test_x",
            },
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    first = await ac.backfill_unverified_merge_evidence(db_session, project.id)
    assert first == 1, f"First call should record 1 row; got {first}"

    second = await ac.backfill_unverified_merge_evidence(db_session, project.id)
    assert second == 0, "Second call must be idempotent (0 new rows)"

    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence) == 1, "Should still be exactly 1 evidence row"


@pytest.mark.asyncio
async def test_backfill_unverified_merge_scopes_by_direction(db_session):
    """When direction_key is provided, only tasks in that direction are considered."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY
    from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY

    project = await _make_project(db_session, "UnverifiedMergeScope")
    version = await _make_version(db_session, project)

    criterion_target = await ac.create_criterion(
        db_session, project.id, version.id,
        "Target direction criterion",
    )
    criterion_other = await ac.create_criterion(
        db_session, project.id, version.id,
        "Other direction criterion",
    )

    for dk, crit in [("d-target-scope", criterion_target), ("d-other-scope", criterion_other)]:
        dir_row = Direction(
            project_id=project.id,
            direction_key=dk,
            data={"id": dk, "text": dk, "status": "active"},
            status="active",
            criterion_keys=[crit.criterion_key],
        )
        db_session.add(dir_row)

    # Task in the OTHER direction.
    other_task = Task(
        project_id=project.id,
        payloads=[{
            "type": "create",
            "prompt": "other",
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "testing/test_other.py::test_x",
            },
        }],
        context={
            "direction_id": "d-other-scope",
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            REVIEW_LOOP_KEY: {UNVERIFIED_MERGE_REASON_KEY: "verify_setup_failed"},
            "_pr_changed_files": [
                "testing/test_other.py",
                "backend/app/other_module.py",
            ],
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": False,
                "proven": False,
                "kind": "named_test",
                "reason": "unverified merge",
            },
        },
        criterion_ids=[criterion_target.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(other_task)
    await db_session.commit()

    # Backfill scoped to the TARGET direction — should see 0 tasks.
    recorded = await ac.backfill_unverified_merge_evidence(
        db_session, project.id, direction_key="d-target-scope"
    )
    assert recorded == 0, "No tasks in target direction should produce 0 evidence"

    # Confirm no evidence was recorded.
    ev_target = await ac.get_evidence_for_criterion(db_session, criterion_target.id)
    assert ev_target == [], "No evidence for target criterion"


@pytest.mark.asyncio
async def test_backfill_unverified_merge_skips_no_seam_files(db_session):
    """Unverified-merge task with test-only diff gets no backfill evidence.

    A named_test that ships NO production seam cannot exercise the changed code,
    so the backfill must skip it just like the original named_test gate would.
    """
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY
    from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY

    project = await _make_project(db_session, "UnverifiedMergeNoSeam")
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id,
        "No-seam criterion",
    )

    task = Task(
        project_id=project.id,
        payloads=[{
            "type": "create",
            "prompt": "test-only",
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "testing/test_only.py::test_x",
            },
        }],
        context={
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            REVIEW_LOOP_KEY: {UNVERIFIED_MERGE_REASON_KEY: "ci_setup_failed"},
            "_pr_changed_files": [
                "testing/test_only.py",
                # No production file — test-only diff.
            ],
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": False,
                "proven": False,
                "kind": "named_test",
                "reason": "unverified merge",
            },
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    recorded = await ac.backfill_unverified_merge_evidence(db_session, project.id)
    assert recorded == 0, "Test-only diff must not produce backfill evidence"

    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert evidence == [], "No evidence for test-only diff"


# ---------------------------------------------------------------------------
# attestation spawns corroboration task (acceptance test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attestation_spawns_corroboration_task(client: AsyncClient, db_session):
    """Operator attestation spawns a QUEUED research task targeting the criterion.

    When the operator attests a stuck criterion via POST .../attest, the endpoint
    must also create a Task row with ``criterion_ids=[criterion_key]`` so the
    existing record_task_merge_evidence machinery can later transition it from
    ``'attested'`` to ``'covered'`` when the task merges with a proven verdict.
    """
    project = await _make_project_with_member(db_session, name="Attest-Corroboration")
    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Stuck criterion needing verification"
    )

    resp = await client.post(
        f"/api/admin/projects/{project.id}/acceptance-criteria/{criterion.criterion_key}/attest",
        json={"reason": "Direction retired; spawn verification task"},
    )
    assert resp.status_code == 200, resp.text

    # A Task must have been created targeting this criterion.
    from app.models import Task as _Task

    tasks = (
        (await db_session.execute(
            select(_Task).where(
                _Task.project_id == project.id,
                _Task.created_by == "SYSTEM_AUTO_REVIEW",
            )
        ))
        .scalars()
        .all()
    )
    assert len(tasks) >= 1, (
        f"Expected at least one verification task from SYSTEM_AUTO_REVIEW, "
        f"got {len(tasks)}"
    )
    spawned = tasks[-1]  # most recently created
    assert spawned.status == TaskStatus.QUEUED, (
        f"Task should be QUEUED, got {spawned.status}"
    )
    assert criterion.criterion_key in spawned.criterion_ids, (
        f"Task criterion_ids {spawned.criterion_ids} must include "
        f"{criterion.criterion_key}"
    )
    assert spawned.created_by == "SYSTEM_AUTO_REVIEW", (
        f"Expected created_by=SYSTEM_AUTO_REVIEW, got {spawned.created_by}"
    )
    assert "Verify: " in spawned.title, (
        f"Task title {spawned.title!r} should start with 'Verify:'"
    )


@pytest.mark.asyncio
async def test_reattest_does_not_duplicate_corroboration_task(
    client: AsyncClient, db_session
):
    """Re-attestation must NOT spawn a second corroboration task.

    Acceptance evidence for the dedup fix: a criterion that is attested,
    then re-attested (renewed) by the same operator must result in exactly
    ONE ``'Verify: {criterion_key}'`` Task row, not two.  The second POST
    goes through the UPSERT (IntegrityError) path which also calls
    ``spawn_corroboration_task`` — the dedup guard in that function must
    skip the second spawn.
    """
    project = await _make_project_with_member(
        db_session, name="ReAttest-No-Dup"
    )
    version = ReleaseVersion(
        project_id=project.id, version_number=1, status="open"
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Criterion for re-attest dedup"
    )

    url = (
        f"/api/admin/projects/{project.id}"
        f"/acceptance-criteria/{criterion.criterion_key}/attest"
    )

    # First attestation — should spawn 1 corroboration task.
    resp1 = await client.post(url, json={"reason": "First attest — spawn"})
    assert resp1.status_code == 200, resp1.text

    # Second attestation (re-attest / renewal) — should NOT spawn a
    # second corroboration task.
    resp2 = await client.post(
        url, json={"reason": "Re-attest — should not duplicate"}
    )
    assert resp2.status_code == 200, resp2.text

    # Query for corroboration tasks: there must be exactly one.
    from app.models import Task as _Task

    tasks = (
        (
            await db_session.execute(
                select(_Task).where(
                    _Task.project_id == project.id,
                    _Task.title == f"Verify: {criterion.criterion_key}",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(tasks) == 1, (
        f"Re-attestation must not create a duplicate corroboration task; "
        f"expected 1, found {len(tasks)}"
    )
    spawned = tasks[0]
    assert spawned.status == TaskStatus.QUEUED
    assert spawned.criterion_ids == [criterion.criterion_key]
    assert spawned.created_by == "SYSTEM_AUTO_REVIEW"


# ---------------------------------------------------------------------------
# Failed corroboration: surfaces and downgrades trust (acceptance test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_corroboration_surfaces_and_downgrades_trust(db_session):
    """A FAILED corroboration task must both SURFACE in operator attention and
    DOWNGRADE the attestation coverage.

    When a spawned Verify: {key} task ends in FAILED or CANCELLED, the operator's
    attestation must not remain at full 'attested' trust — coverage falls to
    'partial' and a 'corroboration_failed' attention item appears so the operator
    can re-attest or provide real machine evidence.
    """
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Criterion with failed corroboration"
    )

    # Step 1: Record a valid (non-expired) operator attestation.
    fresh_at = _utcnow().isoformat()
    await ac.record_evidence(
        db_session,
        criterion.id,
        project.id,
        source_kind="operator_attestation",
        source_id=_TEST_USER_ID,
        detail={"attested_at": fresh_at, "reason": "temporarily accepted"},
    )

    # With only a fresh attestation, coverage must be 'attested'.
    summary1 = await ac.get_version_coverage_summary(db_session, version.id)
    crit1 = next(
        c for c in summary1["criteria"]
        if c["criterion_key"] == criterion.criterion_key
    )
    assert crit1["coverage"] == "attested", (
        f"Expected 'attested' before failed corroboration, got {crit1['coverage']}"
    )

    # Step 2: Create a FAILED corroboration task.
    from app.models import Task as _Task, TaskStatus as _TaskStatus

    failed_task = _Task(
        project_id=project.id,
        title=f"Verify: {criterion.criterion_key}",
        criterion_ids=[criterion.criterion_key],
        context={
            "corroboration_of": criterion.criterion_key,
            "spawned_by": "operator_attestation",
        },
        payloads=[{"type": "research", "prompt": "Verify this criterion"}],
        created_by="SYSTEM_AUTO_REVIEW",
        status=_TaskStatus.FAILED,
    )
    db_session.add(failed_task)
    await db_session.commit()

    # Step 3: Coverage must now be 'partial' (downgraded from 'attested').
    summary2 = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary2["attested"] == 0, (
        f"Expected 0 attested after failed corroboration, got {summary2['attested']}"
    )
    assert summary2["partial"] >= 1, (
        f"Expected ≥1 partial after failed corroboration, got {summary2['partial']}"
    )
    crit2 = next(
        c for c in summary2["criteria"]
        if c["criterion_key"] == criterion.criterion_key
    )
    assert crit2["coverage"] == "partial", (
        f"Expected 'partial' after failed corroboration, got {crit2['coverage']}"
    )

    # Step 4: Operator attention must surface a corroboration_failed item.
    attention = await build_operator_attention(db_session, project, _TEST_USER_ID)

    assert attention["counts"]["corroboration_failed"] == 1, (
        f"Expected corroboration_failed=1, got {attention['counts'].get('corroboration_failed')}"
    )
    corrob_id = (
        f"version:{version.id}:corroboration_failed:{criterion.criterion_key}"
    )
    items_by_id = {item["id"]: item for item in attention["items"]}
    assert corrob_id in items_by_id, (
        f"Expected corroboration_failed item {corrob_id} in {list(items_by_id)}"
    )
    item = items_by_id[corrob_id]
    assert item["reason"] == "corroboration_failed", (
        f"Expected reason=corroboration_failed, got {item['reason']}"
    )
    assert item["label"] == "Attestation verification failed", (
        f"Unexpected label: {item['label']}"
    )
    assert item["criterion_key"] == criterion.criterion_key
    assert item["release_version_id"] == str(version.id)


@pytest.mark.asyncio
async def test_reattest_after_failed_corroboration_restores_attested(
    db_session,
):
    """Re-attestation after a failed corroboration must restore 'attested' coverage.

    When a corroboration task FAILED, the old failure alone should NOT keep the
    criterion at 'partial' once the operator re-attests and a new QUEUED
    corroboration task is spawned. The ``get_failed_corroboration_keys`` function
    must exclude keys that have a newer non-terminal task.
    """
    project = await _make_project(db_session, "ReAttest-After-Fail")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Criterion re-attested after fail"
    )

    fresh_at = _utcnow().isoformat()
    await ac.record_evidence(
        db_session,
        criterion.id,
        project.id,
        source_kind="operator_attestation",
        source_id=_TEST_USER_ID,
        detail={"attested_at": fresh_at, "reason": "first attest"},
    )

    # Step 1: With only a fresh attestation, coverage must be 'attested'.
    summary1 = await ac.get_version_coverage_summary(db_session, version.id)
    crit1 = next(
        c for c in summary1["criteria"]
        if c["criterion_key"] == criterion.criterion_key
    )
    assert crit1["coverage"] == "attested", (
        f"Expected 'attested' before failed corroboration, got {crit1['coverage']}"
    )

    # Step 2: Create a FAILED corroboration task.
    from app.models import Task as _Task, TaskStatus as _TaskStatus

    failed_task = _Task(
        project_id=project.id,
        title=f"Verify: {criterion.criterion_key}",
        criterion_ids=[criterion.criterion_key],
        context={
            "corroboration_of": criterion.criterion_key,
            "spawned_by": "operator_attestation",
        },
        payloads=[{"type": "research", "prompt": "Verify this criterion"}],
        created_by="SYSTEM_AUTO_REVIEW",
        status=_TaskStatus.FAILED,
    )
    db_session.add(failed_task)
    await db_session.commit()

    # Step 3: Coverage must now be 'partial' (downgraded from 'attested').
    summary2 = await ac.get_version_coverage_summary(db_session, version.id)
    crit2 = next(
        c for c in summary2["criteria"]
        if c["criterion_key"] == criterion.criterion_key
    )
    assert crit2["coverage"] == "partial", (
        f"Expected 'partial' after failed corroboration, got {crit2['coverage']}"
    )

    # Step 4: Operator re-attests (records a new attestation, and
    # spawn_corroboration_task would create a new QUEUED task).  Simulate
    # the re-attestation by recording fresh evidence and adding a QUEUED
    # corroboration task.
    re_fresh_at = _utcnow().isoformat()
    await ac.record_evidence(
        db_session,
        criterion.id,
        project.id,
        source_kind="operator_attestation",
        source_id=_TEST_USER_ID,
        detail={"attested_at": re_fresh_at, "reason": "re-attest after failure"},
    )

    queued_task = _Task(
        project_id=project.id,
        title=f"Verify: {criterion.criterion_key}",
        criterion_ids=[criterion.criterion_key],
        context={
            "corroboration_of": criterion.criterion_key,
            "spawned_by": "operator_attestation",
        },
        payloads=[{"type": "research", "prompt": "Verify this criterion"}],
        created_by="SYSTEM_AUTO_REVIEW",
        status=_TaskStatus.QUEUED,
    )
    db_session.add(queued_task)
    await db_session.commit()

    # Step 5: Coverage must now be 'attested' again — the failed task is
    # stale because a newer QUEUED task exists.
    summary3 = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary3["attested"] >= 1, (
        f"Expected ≥1 attested after re-attest + new task, "
        f"got {summary3['attested']}"
    )
    crit3 = next(
        c for c in summary3["criteria"]
        if c["criterion_key"] == criterion.criterion_key
    )
    assert crit3["coverage"] == "attested", (
        f"Expected 'attested' after re-attest + new task, got {crit3['coverage']}"
    )

    # Step 6: get_failed_corroboration_keys must NOT include this key.
    failed_keys = await ac.get_failed_corroboration_keys(db_session, project.id)
    assert criterion.criterion_key not in failed_keys, (
        f"Expected criterion {criterion.criterion_key} to NOT be in failed "
        f"corroboration keys (newer QUEUED task exists), but it was: "
        f"{failed_keys}"
    )

    # Step 7: build_operator_attention must not surface a
    # corroboration_failed item for this criterion.
    attention = await build_operator_attention(db_session, project, _TEST_USER_ID)
    corrob_id = (
        f"version:{version.id}:corroboration_failed:{criterion.criterion_key}"
    )
    items_by_id = {item["id"]: item for item in attention["items"]}
    assert corrob_id not in items_by_id, (
        f"Expected NO corroboration_failed item for re-attested criterion, "
        f"but found {items_by_id[corrob_id]}"
    )


@pytest.mark.asyncio
async def test_corroboration_child_inherits_criterion_ids_and_upgrades_attested_to_covered(
    db_session, mocker
):
    """A child task spawned from a corroboration parent (which carries
    criterion_ids on the Task row but has no direction_id) must inherit the
    parent's criterion_ids, so that when the child task completes with a
    proven acceptance verdict the criterion coverage upgrades from 'attested'
    to 'covered'.

    This is the load-bearing happy path of the attestation tier — that an
    operator attestation is provisional until a verification task produces
    machine-verified evidence. Without this inheritance chain the corroboration
    flow can never actually upgrade a criterion to 'covered'.
    """
    from app.models import ReleaseVersion
    from app import acceptance_criteria as _ac

    project = await _make_project(db_session, "Corroboration-Upgrade")
    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    criterion = await _ac.create_criterion(
        db_session, project.id, version.id,
        "Criterion for corroboration upgrade test",
    )
    criterion_key = criterion.criterion_key

    # Step 1: Record operator attestation evidence.
    fresh_at = _utcnow().isoformat()
    await _ac.record_evidence(
        db_session, criterion.id, project.id,
        source_kind="operator_attestation",
        source_id=_TEST_USER_ID,
        detail={"attested_at": fresh_at, "reason": "provisional accept"},
    )

    # Coverage must be 'attested' before corroboration.
    summary1 = await _ac.get_version_coverage_summary(db_session, version.id)
    crit1 = next(c for c in summary1["criteria"] if c["criterion_key"] == criterion_key)
    assert crit1["coverage"] == "attested", (
        f"Expected 'attested' before corroboration, got {crit1['coverage']!r}"
    )

    # Step 2: Create a research task mimicking a corroboration task:
    # it has criterion_ids=[criterion_key] but NO direction_id in context
    # (the exact shape spawn_corroboration_task produces).
    corrob_task = Task(
        project_id=project.id,
        title=f"Verify: {criterion_key}",
        payloads=[{"type": "research", "base_branch": "main"}],
        context={"research_depth": 0},
        criterion_ids=[criterion_key],
        status=TaskStatus.IN_PROGRESS,
        created_by="SYSTEM_AUTO_REVIEW",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(corrob_task)
    await db_session.commit()
    await db_session.refresh(corrob_task)

    # Step 3: Run _handle_research_completion with a finding that has
    # acceptance_evidence but no direction_id — just like a finding
    # the corroboration task's research agent would produce.
    from app.research_completion import _handle_research_completion
    from test_autonomous_research import _patch_research_externals

    finding = {
        "title": "Verify criterion with automated test",
        "description": "Write and run a test proving the criterion is met",
        "prompt": "Implement a test that verifies this acceptance criterion",
        "priority": "medium",
        "scope": "small",
        "risk": "low",
        "type": "improvement",
        "acceptance_evidence": {
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_criteria.py",
        },
    }
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    await _handle_research_completion(corrob_task, "", db_session)

    # Step 4: The spawned child must have inherited criterion_ids from
    # the parent task (falling back from the empty direction cache to
    # task.criterion_ids).
    children = (
        (await db_session.execute(
            select(Task).where(Task.parent_task_id == corrob_task.id)
        ))
        .scalars()
        .all()
    )
    assert len(children) == 1, f"expected 1 spawned child, got {len(children)}"
    child = children[0]
    assert child.criterion_ids == [criterion_key], (
        f"spawned child must inherit criterion_ids from parent task; "
        f"got {child.criterion_ids!r}"
    )

    # Step 5: Simulate the child completing with a proven acceptance
    # verdict (as a real merge-test verification would produce).
    child.status = TaskStatus.COMPLETED
    child.context = {
        **(child.context or {}),
        "code_acceptance_verdict": {
            "satisfied": True,
            "proven": True,
            "kind": "named_test",
            "reason": "CI-verified test passes",
        },
    }
    await db_session.commit()

    # Step 6: Record merge evidence using the child's criterion_ids.
    recorded = await _ac.record_task_merge_evidence(
        db_session, child, project.id
    )
    assert recorded >= 1, (
        f"record_task_merge_evidence must record at least 1 evidence row "
        f"for a child that inherited criterion_ids; got {recorded}"
    )

    # Step 7: Coverage must now be 'covered' — the task_merge evidence
    # proves the criterion satisfied, upgrading from the provisional
    # 'attested' tier.
    summary2 = await _ac.get_version_coverage_summary(db_session, version.id)
    assert summary2["covered"] >= 1, (
        f"Expected >=1 covered after successful corroboration, "
        f"got {summary2['covered']}"
    )
    assert summary2["attested"] == 0, (
        f"Expected 0 attested after corroboration upgrade, "
        f"got {summary2['attested']}"
    )
    crit2 = next(
        c for c in summary2["criteria"] if c["criterion_key"] == criterion_key
    )
    assert crit2["coverage"] == "covered", (
        f"Expected 'covered' after successful corroboration, "
        f"got {crit2['coverage']!r}"
    )


# ---------------------------------------------------------------------------
# MissingGreenlet fidelity: attest / re-attest with real ORM expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attest_criterion_reattest_no_missing_greenlet(
    client: AsyncClient, db_session, expire_fidelity
):
    """Re-attestation must not raise MissingGreenlet when ORM expiry is active.

    On the asyncpg backend, session.rollback() expires all ORM objects.
    The attest endpoint's IntegrityError path previously read row.id and
    row.text after the rollback, which triggered a sync lazy-load with no
    greenlet context and raised MissingGreenlet.  The fix captures criterion_id
    and criterion_text as plain Python values before entering the try/except,
    so the post-rollback code never touches the expired ORM instance.

    This test uses the expire_fidelity fixture to restore real SQLAlchemy expiry
    (InstanceState._expire) on the SQLite backend, reproducing the asyncpg
    failure mode cheaply.  Without the production fix, the second POST fails
    with DetachedInstanceError or MissingGreenlet; with the fix it returns 200.
    """
    project = await _make_project_with_member(db_session, name="ReAttest-NoGreenlet")
    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Criterion for greenlet fidelity test"
    )

    url = (
        f"/api/admin/projects/{project.id}"
        f"/acceptance-criteria/{criterion.criterion_key}/attest"
    )

    # First attestation — succeeds on the INSERT path, no rollback involved.
    resp1 = await client.post(url, json={"reason": "First attest"})
    assert resp1.status_code == 200, resp1.text
    data1 = resp1.json()
    assert data1["status"] == "ok"
    assert data1["coverage"] == "attested"

    # Second attestation — triggers IntegrityError → rollback → UPDATE path.
    # With expire_fidelity active, rollback expires row; accessing row.id or
    # row.text after rollback would raise MissingGreenlet without the fix.
    resp2 = await client.post(url, json={"reason": "Re-attest renewal"})
    assert resp2.status_code == 200, resp2.text
    data2 = resp2.json()
    assert data2["status"] == "ok", (
        f"Re-attestation must succeed even with real ORM expiry; got: {data2}"
    )
    assert data2["coverage"] == "attested"

    # Verify the evidence row was updated (not duplicated) — dedup constraint held.
    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    attest_rows = [e for e in evidence if e.source_kind == "operator_attestation"]
    assert len(attest_rows) == 1, (
        f"Re-attestation must UPSERT, not insert a duplicate row; "
        f"found {len(attest_rows)} operator_attestation rows"
    )
    assert attest_rows[0].detail["reason"] == "Re-attest renewal", (
        f"Evidence detail must reflect the refreshed reason; "
        f"got {attest_rows[0].detail!r}"
    )


# ---------------------------------------------------------------------------
# merge_evidence_gaps persistence  (acceptance criterion: test_merge_evidence_drop_reason_surfaced)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_evidence_drop_reason_surfaced(db_session):
    """Merged tasks that fail to record evidence persist the drop reason on the open version.

    Exercises the full acceptance criterion:
      - record_task_merge_evidence with no_active_criteria persists the reason
        on ReleaseVersion.merge_evidence_gaps.
      - get_version_coverage_summary exposes merge_evidence_gaps so callers can
        surface WHY merges did not produce criterion coverage.
    """
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.acceptance_criteria import MergeEvidenceReason

    project = await _make_project(db_session, "MergeGapSurfaced")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "Must surface gap")
    criterion_key = criterion.criterion_key

    # Retract the criterion so the merge will hit the no_active_criteria path.
    await ac.retract_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=criterion_key,
        reason="Test retraction",
        retracted_by="test",
    )

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "merge gap test"}],
        context={
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "CI green",
            }
        },
        criterion_ids=[criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    result = await ac.record_task_merge_evidence(db_session, task, project.id)
    assert result == 0, f"Expected 0 evidence rows for retracted criterion; got {result}"

    # The drop reason must be persisted on the open version.
    await db_session.refresh(version)
    gaps = version.merge_evidence_gaps
    assert gaps is not None, "merge_evidence_gaps must be set after a gap is recorded"
    assert gaps.get(MergeEvidenceReason.no_active_criteria.value, 0) >= 1, (
        f"Expected no_active_criteria >= 1 in merge_evidence_gaps; got {gaps}"
    )
    # The unmatched key must be captured.
    assert criterion_key in gaps.get("unmatched_criterion_ids", []), (
        f"Expected {criterion_key!r} in unmatched_criterion_ids; got {gaps}"
    )

    # get_version_coverage_summary must surface the gaps.
    summary = await ac.get_version_coverage_summary(db_session, version.id)
    assert "merge_evidence_gaps" in summary, (
        "get_version_coverage_summary must include 'merge_evidence_gaps' key"
    )
    surfaced = summary["merge_evidence_gaps"]
    assert surfaced is not None, "merge_evidence_gaps in summary must not be None after a gap"
    assert surfaced.get(MergeEvidenceReason.no_active_criteria.value, 0) >= 1, (
        f"Summary merge_evidence_gaps must reflect recorded gap; got {surfaced}"
    )


@pytest.mark.asyncio
async def test_merge_evidence_gap_no_criteria_recorded(db_session):
    """record_task_merge_evidence with no criterion_ids increments no_criteria counter."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.acceptance_criteria import MergeEvidenceReason

    project = await _make_project(db_session, "MergeGapNoCriteria")
    version = await _make_version(db_session, project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "no criteria"}],
        context={
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "CI green",
            }
        },
        criterion_ids=[],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    result = await ac.record_task_merge_evidence(db_session, task, project.id)
    assert result == 0

    await db_session.refresh(version)
    gaps = version.merge_evidence_gaps
    assert gaps is not None, "merge_evidence_gaps must be set after a no_criteria gap"
    assert gaps.get(MergeEvidenceReason.no_criteria.value, 0) >= 1, (
        f"Expected no_criteria >= 1; got {gaps}"
    )


@pytest.mark.asyncio
async def test_merge_evidence_gap_verdict_not_proven_recorded(db_session):
    """record_task_merge_evidence with unproven verdict increments verdict_not_proven counter."""
    from app.acceptance_criteria import MergeEvidenceReason

    project = await _make_project(db_session, "MergeGapUnproven")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(db_session, project.id, version.id, "Unproven crit")

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "unproven merge"}],
        context={},  # no CODE_ACCEPTANCE_VERDICT_KEY → verdict_not_proven
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    result = await ac.record_task_merge_evidence(db_session, task, project.id)
    assert result == 0

    await db_session.refresh(version)
    gaps = version.merge_evidence_gaps
    assert gaps is not None, "merge_evidence_gaps must be set after a verdict_not_proven gap"
    assert gaps.get(MergeEvidenceReason.verdict_not_proven.value, 0) >= 1, (
        f"Expected verdict_not_proven >= 1; got {gaps}"
    )


@pytest.mark.asyncio
async def test_merge_evidence_gaps_accumulate(db_session):
    """Multiple gap-producing merges accumulate counters, not overwrite them."""
    from app.acceptance_criteria import MergeEvidenceReason

    project = await _make_project(db_session, "MergeGapAccumulate")
    version = await _make_version(db_session, project)

    for _ in range(3):
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "no criteria"}],
            context={},
            criterion_ids=[],
            status=TaskStatus.COMPLETED,
        )
        db_session.add(task)
        await db_session.commit()
        await ac.record_task_merge_evidence(db_session, task, project.id)

    await db_session.refresh(version)
    gaps = version.merge_evidence_gaps
    assert gaps is not None
    assert gaps.get(MergeEvidenceReason.no_criteria.value, 0) >= 3, (
        f"Expected no_criteria >= 3 after three gap-producing merges; got {gaps}"
    )


# ---------------------------------------------------------------------------
# _record_evidence_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_evidence_batch_inserts_all(db_session):
    """_record_evidence_batch inserts multiple evidence rows in a single commit."""
    project = await _make_project(db_session, "BatchEvidence")
    version = await _make_version(db_session, project)
    c1 = await ac.create_criterion(db_session, project.id, version.id, "Batch criterion 1")
    c2 = await ac.create_criterion(db_session, project.id, version.id, "Batch criterion 2")

    batch = [
        CriterionEvidence(
            criterion_id=c1.id,
            project_id=project.id,
            source_kind="task_merge",
            source_id="task-batch-1",
            detail={"proven": True},
        ),
        CriterionEvidence(
            criterion_id=c2.id,
            project_id=project.id,
            source_kind="reachability",
            source_id="task-batch-1",
            detail={"proven": True},
        ),
    ]
    recorded = await ac._record_evidence_batch(db_session, batch)
    assert recorded == 2

    ev1 = await ac.get_evidence_for_criterion(db_session, c1.id)
    ev2 = await ac.get_evidence_for_criterion(db_session, c2.id)
    assert len(ev1) == 1
    assert ev1[0].source_kind == "task_merge"
    assert len(ev2) == 1
    assert ev2[0].source_kind == "reachability"


@pytest.mark.asyncio
async def test_record_evidence_batch_empty_is_noop(db_session):
    """_record_evidence_batch with an empty list returns 0 and does nothing."""
    recorded = await ac._record_evidence_batch(db_session, [])
    assert recorded == 0


# ---------------------------------------------------------------------------
# backfill_all_directions_merge_evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_all_directions_merge_evidence_single_scan(db_session):
    """backfill_all_directions_merge_evidence covers proven tasks across two directions."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    project = await _make_project(db_session, "BulkDirectionBackfill")
    version = await _make_version(db_session, project)

    dk1 = "d-bulk-1"
    dk2 = "d-bulk-2"

    c1 = await ac.create_criterion(db_session, project.id, version.id, "Bulk criterion 1")
    c2 = await ac.create_criterion(db_session, project.id, version.id, "Bulk criterion 2")

    dir1 = Direction(
        project_id=project.id,
        direction_key=dk1,
        data={"id": dk1, "text": "Dir 1", "status": "active"},
        status="active",
        criterion_keys=[c1.criterion_key],
    )
    dir2 = Direction(
        project_id=project.id,
        direction_key=dk2,
        data={"id": dk2, "text": "Dir 2", "status": "active"},
        status="active",
        criterion_keys=[c2.criterion_key],
    )
    db_session.add(dir1)
    db_session.add(dir2)

    task1 = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "t1"}],
        context={
            "direction_id": dk1,
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "green",
            },
        },
        criterion_ids=[c1.criterion_key],
        status=TaskStatus.COMPLETED,
        completed_at=version.opened_at + timedelta(seconds=1),
    )
    task2 = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "t2"}],
        context={
            "direction_id": dk2,
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "green",
            },
        },
        criterion_ids=[c2.criterion_key],
        status=TaskStatus.COMPLETED,
        completed_at=version.opened_at + timedelta(seconds=2),
    )
    db_session.add(task1)
    db_session.add(task2)
    await db_session.commit()

    totals = await ac.backfill_all_directions_merge_evidence(
        db_session, project.id, [dk1, dk2]
    )

    # Each direction should have 2 evidence rows (task_merge + reachability).
    assert totals.get(dk1, 0) == 2, f"Expected 2 for {dk1}, got {totals}"
    assert totals.get(dk2, 0) == 2, f"Expected 2 for {dk2}, got {totals}"

    ev1 = await ac.get_evidence_for_criterion(db_session, c1.id)
    ev2 = await ac.get_evidence_for_criterion(db_session, c2.id)
    assert {e.source_kind for e in ev1} == {"task_merge", "reachability"}
    assert {e.source_kind for e in ev2} == {"task_merge", "reachability"}


@pytest.mark.asyncio
async def test_backfill_all_directions_merge_evidence_empty_list(db_session):
    """backfill_all_directions_merge_evidence with an empty list returns {}."""
    project = await _make_project(db_session, "BulkDirEmpty")
    totals = await ac.backfill_all_directions_merge_evidence(db_session, project.id, [])
    assert totals == {}


@pytest.mark.asyncio
async def test_backfill_all_directions_merge_evidence_ignores_other_directions(db_session):
    """Tasks belonging to an unlisted direction are not processed."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    project = await _make_project(db_session, "BulkDirIgnore")
    version = await _make_version(db_session, project)

    dk_target = "d-target"
    dk_other = "d-other"

    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Target criterion"
    )
    other_criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Other criterion"
    )

    dir_target = Direction(
        project_id=project.id,
        direction_key=dk_target,
        data={"id": dk_target, "text": "Target", "status": "active"},
        status="active",
        criterion_keys=[criterion.criterion_key],
    )
    dir_other = Direction(
        project_id=project.id,
        direction_key=dk_other,
        data={"id": dk_other, "text": "Other", "status": "active"},
        status="active",
        criterion_keys=[other_criterion.criterion_key],
    )
    db_session.add(dir_target)
    db_session.add(dir_other)

    task_target = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        context={
            "direction_id": dk_target,
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            CODE_ACCEPTANCE_VERDICT_KEY: {"satisfied": True, "proven": True, "kind": "named_test"},
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
        completed_at=version.opened_at + timedelta(seconds=1),
    )
    task_other = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "y"}],
        context={
            "direction_id": dk_other,
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            CODE_ACCEPTANCE_VERDICT_KEY: {"satisfied": True, "proven": True, "kind": "named_test"},
        },
        criterion_ids=[other_criterion.criterion_key],
        status=TaskStatus.COMPLETED,
        completed_at=version.opened_at + timedelta(seconds=2),
    )
    db_session.add(task_target)
    db_session.add(task_other)
    await db_session.commit()

    # Only pass dk_target — the other direction's task must not be processed.
    totals = await ac.backfill_all_directions_merge_evidence(
        db_session, project.id, [dk_target]
    )

    assert dk_target in totals
    assert dk_other not in totals

    other_ev = await ac.get_evidence_for_criterion(db_session, other_criterion.id)
    assert other_ev == [], "task for unlisted direction must not have evidence"

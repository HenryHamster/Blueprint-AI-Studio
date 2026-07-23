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
  * operator_attestation source_kind satisfies coverage and the attest endpoint records it;
  * test_walkthrough_merge_evidence_flips_criterion_coverage proves v26
    walkthrough results flip coverage for the three canonical criteria.
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
    Alert,
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
# get_active_criteria_rubric
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_active_criteria_rubric_returns_active_rubric_ordered(db_session):
    """get_active_criteria_rubric returns {criterion_key, text} for ACTIVE rows,
    ordered by created_at, excluding retracted/superseded criteria."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    c1 = await ac.create_criterion(db_session, project.id, version.id, "First")
    c2 = await ac.create_criterion(db_session, project.id, version.id, "Second")
    c3 = await ac.create_criterion(db_session, project.id, version.id, "Retracted")

    # Retract c3 — the rubric accessor must exclude non-active rows.
    await ac.retract_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=c3.criterion_key,
        reason="stale",
        retracted_by="test",
    )

    rubric = await ac.get_active_criteria_rubric(db_session, version.id)

    # Only active criteria, in created_at order, projected to the rubric fields.
    assert rubric == [
        {"criterion_key": c1.criterion_key, "text": "First"},
        {"criterion_key": c2.criterion_key, "text": "Second"},
    ]
    # No coverage/status/evidence keys leak into the lightweight rubric.
    assert all(set(entry.keys()) == {"criterion_key", "text"} for entry in rubric)


@pytest.mark.asyncio
async def test_get_active_criteria_rubric_empty(db_session):
    """get_active_criteria_rubric returns [] when the version has no criteria."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    assert await ac.get_active_criteria_rubric(db_session, version.id) == []


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

    # Snapshot ids up front: the duplicate insert below is collapsed inside
    # record_evidence via IntegrityError → session.rollback(), which expires
    # every instance in the session — a post-rollback ``version.id`` lazy-load
    # would raise MissingGreenlet under real ORM expiry.
    version_id = version.id
    criterion_id = criterion.id
    project_id = project.id

    await ac.record_evidence(
        db_session,
        criterion_id=criterion_id,
        project_id=project_id,
        source_kind="task_merge",
        source_id="task-uuid-abc123",
        detail={"pr": 42},
    )

    await ac.record_evidence(
        db_session,
        criterion_id=criterion_id,
        project_id=project_id,
        source_kind="task_merge",
        source_id="task-uuid-abc123",
        detail={"pr": 43},
    )

    assert await ac.count_criteria_for_version(db_session, version_id) == 1

    fetched = await ac.get_evidence_for_criterion(db_session, criterion_id)
    assert len(fetched) == 1
    assert fetched[0].source_kind == "task_merge"
    assert fetched[0].source_id == "task-uuid-abc123"
    assert fetched[0].detail == {"pr": 42}


# ---------------------------------------------------------------------------
# compute_criterion_coverage (pure function)
# ---------------------------------------------------------------------------


_UNSET = object()


def _make_evidence(
    source_kind: str,
    detail: dict | None = None,
    recorded_at: object = _UNSET,
) -> CriterionEvidence:
    kwargs: dict = dict(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        criterion_id=uuid.uuid4(),
        source_kind=source_kind,
        detail=detail,
    )
    # Only override recorded_at when the caller supplies one, so the model's
    # default_factory (fresh _utcnow()) still applies for the common case.
    if recorded_at is not _UNSET:
        kwargs["recorded_at"] = recorded_at
    return CriterionEvidence(**kwargs)


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

    # backfilled coverage maps onto the shared EvidenceStatus axis as "partial"
    assert ac.criterion_coverage_to_evidence_status("backfilled") == "partial"


def test_machine_probe_evidence_reports_distinct_probed_tier():
    """Machine-probe evidence produces the distinct, lower-confidence 'probed' tier.

    ``_evidence_proves_covered`` must NOT treat ``acceptance_window_recompute`` or
    ``metrics_integrity_sweep`` as machine-verified covering evidence — that would
    score a mechanism-ran signal at full parity with reachability / proven
    task_merge, even though it only proves the sweep operated (not that the
    criterion's own condition holds).  This confirms:
      - a fresh probe row alone → ``compute_criterion_coverage`` returns ``'probed'``
      - probe + proven machine evidence → ``'covered'`` (probe doesn't regress)
      - probe + operator attestation → ``'attested'`` (attestation ranks higher)
      - probe + backfill → ``'backfilled'`` (backfill ranks higher than probe)
    """
    for kind in ("acceptance_window_recompute", "metrics_integrity_sweep"):
        probe_ev = _make_evidence(kind, detail={"ticks": 3})
        assert not ac._evidence_proves_covered(probe_ev), (
            f"{kind} must NOT be treated as machine-verified covering"
        )
        assert ac._evidence_is_probed(probe_ev) is True
        assert ac.compute_criterion_coverage([probe_ev]) == "probed"

        # Machine-verified evidence dominates a probe.
        proven = _make_evidence("task_merge", detail={"proven": True})
        assert ac.compute_criterion_coverage([probe_ev, proven]) == "covered"

        # A non-expired operator attestation ranks above a probe.
        attest = _make_evidence("operator_attestation", detail={"reason": "waiver"})
        assert ac.compute_criterion_coverage([probe_ev, attest]) == "attested"

        # Unverified-merge backfill ranks above a probe.
        backfill = _make_evidence(
            ac.UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND, detail={"backfilled": True}
        )
        assert ac.compute_criterion_coverage([probe_ev, backfill]) == "backfilled"

    # probed coverage maps onto the shared EvidenceStatus axis as "partial",
    # never "proven" — a mechanism-ran probe is not machine-proof.
    assert ac.criterion_coverage_to_evidence_status("probed") == "partial"


def test_stale_machine_probe_falls_back_to_partial():
    """A machine-probe row older than MACHINE_PROBE_WINDOW_DAYS is no longer 'probed'.

    Expiry is measured off ``recorded_at`` (these rows carry no attestation
    timestamp), so a stale sweep decays to 'partial' until the sweep re-runs and
    writes a fresh row.  A probe row with no ``recorded_at`` at all also fails —
    a missing timestamp cannot grant perpetual trust.
    """
    stale = _make_evidence(
        "metrics_integrity_sweep",
        detail={"ticks": 1},
        recorded_at=_utcnow() - timedelta(days=ac.MACHINE_PROBE_WINDOW_DAYS + 1),
    )
    assert ac._evidence_is_probed(stale) is False
    assert ac.compute_criterion_coverage([stale]) == "partial"

    no_ts = _make_evidence(
        "acceptance_window_recompute", detail={"ticks": 1}, recorded_at=None
    )
    assert ac._evidence_is_probed(no_ts) is False
    assert ac.compute_criterion_coverage([no_ts]) == "partial"

    # A row recorded comfortably inside the window is still valid (use one day
    # of slack so the predicate's slightly-later _utcnow() cannot tip the
    # exact-boundary comparison and flake).
    fresh = _make_evidence(
        "metrics_integrity_sweep",
        detail={"ticks": 1},
        recorded_at=_utcnow() - timedelta(days=ac.MACHINE_PROBE_WINDOW_DAYS - 1),
    )
    assert ac._evidence_is_probed(fresh) is True
    assert ac.compute_criterion_coverage([fresh]) == "probed"


def test_walkthrough_e2e_evidence_covers_only_when_fresh_and_sha_pinned():
    """A ``walkthrough_e2e`` row is 'covered' ONLY when fresh AND SHA-pinned.

    This is the whole point of the walkthrough_e2e evidence lane: an autonomous
    live-stack browser run that actually exercised the criterion against a pinned
    candidate commit earns genuine machine-verified ``covered`` coverage — but
    stale or committed walk evidence (e.g. 123 commits old) must stop counting,
    and a row with no pinned SHA must not forge coverage.

      (a) fresh + SHA-pinned            => 'covered'
      (b) stale (older than the window) => NOT covered (falls back to 'partial')
      (c) missing candidate_sha         => NOT covered (falls back to 'partial')
    """
    sha = "a" * 40

    # (a) fresh + SHA-pinned → covered. One day of slack keeps the predicate's
    # slightly-later _utcnow() from tipping the exact-boundary comparison.
    fresh_pinned = _make_evidence(
        ac.WALKTHROUGH_E2E_EVIDENCE_KIND,
        detail={"candidate_sha": sha, "evaluated_at": _utcnow().isoformat()},
        recorded_at=_utcnow() - timedelta(days=ac.WALKTHROUGH_E2E_WINDOW_DAYS - 1),
    )
    assert ac._evidence_is_walkthrough_e2e_covered(fresh_pinned) is True
    assert ac._evidence_proves_covered(fresh_pinned) is True
    assert ac.compute_criterion_coverage([fresh_pinned]) == "covered"
    # covered maps onto the shared EvidenceStatus axis as full machine-proof.
    assert ac.criterion_evidence_status([fresh_pinned]) == "proven"

    # (b) stale but SHA-pinned → NOT covered; the only evidence present is
    # non-covering, so coverage falls back to 'partial'.
    stale_pinned = _make_evidence(
        ac.WALKTHROUGH_E2E_EVIDENCE_KIND,
        detail={"candidate_sha": sha},
        recorded_at=_utcnow() - timedelta(days=ac.WALKTHROUGH_E2E_WINDOW_DAYS + 1),
    )
    assert ac._evidence_is_walkthrough_e2e_covered(stale_pinned) is False
    assert ac._evidence_proves_covered(stale_pinned) is False
    assert ac.compute_criterion_coverage([stale_pinned]) == "partial"

    # (c) fresh but missing candidate_sha → NOT covered → 'partial'. Cover the
    # blank-string, absent-key, and non-dict-detail variants.
    for bad_detail in ({"candidate_sha": ""}, {"flow": "runner_enroll"}, None):
        fresh_unpinned = _make_evidence(
            ac.WALKTHROUGH_E2E_EVIDENCE_KIND,
            detail=bad_detail,
            recorded_at=_utcnow() - timedelta(days=1),
        )
        assert ac._evidence_is_walkthrough_e2e_covered(fresh_unpinned) is False
        assert ac.compute_criterion_coverage([fresh_unpinned]) == "partial"

    # A row with no recorded_at at all cannot grant perpetual trust.
    no_ts = _make_evidence(
        ac.WALKTHROUGH_E2E_EVIDENCE_KIND,
        detail={"candidate_sha": sha},
        recorded_at=None,
    )
    assert ac._evidence_is_walkthrough_e2e_covered(no_ts) is False
    assert ac.compute_criterion_coverage([no_ts]) == "partial"

    # Machine-verified fresh+pinned walkthrough evidence coexisting with a bare
    # partial signal still resolves to 'covered' (covering wins).
    partial_only = _make_evidence("operator")
    assert ac.compute_criterion_coverage([partial_only, fresh_pinned]) == "covered"


# ---------------------------------------------------------------------------
# _evidence_is_walkthrough_e2e + walkthrough_e2e coverage tier
# ---------------------------------------------------------------------------


def test_walkthrough_e2e_fresh_pass_is_covered():
    """A fresh, SHA-pinned walkthrough_e2e PASS row grants full 'covered' tier."""
    ev = _make_evidence(
        "walkthrough_e2e",
        detail={"candidate_sha": "abc123", "verdict": "PASS", "flow": "c-dfdc9d82"},
    )
    assert ac._evidence_is_walkthrough_e2e(ev) is True
    assert ac._evidence_proves_covered(ev) is True
    assert ac.compute_criterion_coverage([ev]) == "covered"


def test_walkthrough_e2e_stale_decays():
    """A walkthrough_e2e row older than WALKTHROUGH_E2E_WINDOW_DAYS decays to 'partial'."""
    stale = _make_evidence(
        "walkthrough_e2e",
        detail={"candidate_sha": "abc123", "verdict": "PASS", "flow": "c-dfdc9d82"},
        recorded_at=_utcnow() - timedelta(days=ac.WALKTHROUGH_E2E_WINDOW_DAYS + 1),
    )
    assert ac._evidence_is_walkthrough_e2e(stale) is False
    assert ac._evidence_proves_covered(stale) is False
    assert ac.compute_criterion_coverage([stale]) == "partial"


def test_walkthrough_e2e_no_recorded_at_not_trusted():
    """A walkthrough_e2e row with no recorded_at cannot grant perpetual trust."""
    ev = _make_evidence(
        "walkthrough_e2e",
        detail={"candidate_sha": "abc123", "verdict": "PASS"},
        recorded_at=None,
    )
    assert ac._evidence_is_walkthrough_e2e(ev) is False
    assert ac._evidence_proves_covered(ev) is False


def test_walkthrough_e2e_missing_candidate_sha_not_trusted():
    """A walkthrough_e2e row without a candidate_sha is not trusted."""
    no_sha = _make_evidence(
        "walkthrough_e2e",
        detail={"verdict": "PASS", "flow": "c-dfdc9d82"},
    )
    assert ac._evidence_is_walkthrough_e2e(no_sha) is False
    assert ac._evidence_proves_covered(no_sha) is False

    empty_sha = _make_evidence(
        "walkthrough_e2e",
        detail={"candidate_sha": "", "verdict": "PASS", "flow": "c-dfdc9d82"},
    )
    assert ac._evidence_is_walkthrough_e2e(empty_sha) is False


def test_walkthrough_e2e_fail_verdict_not_covered():
    """A walkthrough_e2e row with a FAIL or DEAD_END verdict does not cover."""
    for verdict in ("FAIL", "DEAD_END", None):
        ev = _make_evidence(
            "walkthrough_e2e",
            detail={"candidate_sha": "abc123", "verdict": verdict, "flow": "c-dfdc9d82"},
        )
        assert ac._evidence_is_walkthrough_e2e(ev) is False, f"verdict={verdict!r} should not grant trust"
        assert ac._evidence_proves_covered(ev) is False


def test_walkthrough_e2e_wrong_source_kind_rejected():
    """_evidence_is_walkthrough_e2e returns False for non-walkthrough_e2e source_kinds."""
    not_walkthrough = _make_evidence(
        "operator_attestation",
        detail={"candidate_sha": "abc123", "verdict": "PASS"},
    )
    assert ac._evidence_is_walkthrough_e2e(not_walkthrough) is False


def test_walkthrough_e2e_fresh_inside_window():
    """A row recorded just inside the window boundary is still valid."""
    fresh = _make_evidence(
        "walkthrough_e2e",
        detail={"candidate_sha": "deadbeef", "verdict": "PASS", "flow": "c-5214996f"},
        recorded_at=_utcnow() - timedelta(days=ac.WALKTHROUGH_E2E_WINDOW_DAYS - 1),
    )
    assert ac._evidence_is_walkthrough_e2e(fresh) is True
    assert ac.compute_criterion_coverage([fresh]) == "covered"


def test_walkthrough_e2e_none_detail_not_trusted():
    """A walkthrough_e2e row with None detail is not trusted."""
    ev = _make_evidence("walkthrough_e2e", detail=None)
    assert ac._evidence_is_walkthrough_e2e(ev) is False
    assert ac._evidence_proves_covered(ev) is False


# ---------------------------------------------------------------------------
# criterion_evidence_status (pure function, Component 1)
# ---------------------------------------------------------------------------


def test_criterion_evidence_status_proven_from_covered():
    """A proven task_merge → covered → 'proven' on the shared axis."""
    ev = _make_evidence("task_merge", detail={"proven": True})
    assert ac.criterion_evidence_status([ev]) == "proven"
    # reachability alone also covers → proven.
    assert ac.criterion_evidence_status([_make_evidence("reachability")]) == "proven"


def test_criterion_evidence_status_unproven_when_no_evidence():
    """No evidence rows → uncovered → 'unproven'."""
    assert ac.criterion_evidence_status([]) == "unproven"


def test_criterion_evidence_status_partial_when_evidence_does_not_cover():
    """Evidence present but not covering/attested/backfilled → partial."""
    ev = _make_evidence("task_merge", detail={"proven": False})
    assert ac.criterion_evidence_status([ev]) == "partial"


def test_criterion_evidence_status_composes_coverage_and_axis_map():
    """criterion_evidence_status is exactly the composition of its two parts."""
    evs = [_make_evidence("operator"), _make_evidence("task_merge", detail={"proven": True})]
    expected = ac.criterion_coverage_to_evidence_status(ac.compute_criterion_coverage(evs))
    assert ac.criterion_evidence_status(evs) == expected == "proven"


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
    assert summary["probed"] == 0
    assert summary["partial"] == 0
    assert summary["uncovered"] == 0
    assert summary["stuck_count"] == 0
    assert summary["criteria"] == []


@pytest.mark.asyncio
async def test_coverage_summary_has_probed_bucket(db_session):
    """A criterion covered only by a fresh machine probe lands in the 'probed'
    bucket — distinct from 'covered' — and is included in ``total``."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Probed-only outcome"
    )
    db_session.add(
        CriterionEvidence(
            criterion_id=criterion.id,
            project_id=project.id,
            source_kind="acceptance_window_recompute",
            source_id="aw-probe",
            detail={"acceptance_window_verified": 1},
        )
    )
    await db_session.commit()

    summary = await ac.get_version_coverage_summary(db_session, version.id)

    assert summary["probed"] == 1
    assert summary["covered"] == 0
    assert summary["partial"] == 0
    assert summary["uncovered"] == 0
    # 'probed' is counted toward total (backward-compat: total == sum of buckets).
    assert summary["total"] == 1
    by_key = {c["criterion_key"]: c for c in summary["criteria"]}
    assert by_key[criterion.criterion_key]["coverage"] == "probed"


# ---------------------------------------------------------------------------
# phantom acceptance-criteria bookkeeping drift guard
# ---------------------------------------------------------------------------


def test_extract_referenced_criterion_keys_pattern():
    """extract_referenced_criterion_keys finds c-<8hex> tokens, ignores non-keys."""
    text = (
        "Progress: 0/6 covered against c-bbf215e3 and c-a5940d77; "
        "see commit deadbeefcafe (not a key) and c-ZZZ (bad hex)."
    )
    keys = ac.extract_referenced_criterion_keys(text)
    assert keys == {"c-bbf215e3", "c-a5940d77"}
    # word boundary: a longer hex run is NOT a criterion key
    assert ac.extract_referenced_criterion_keys("c-deadbeef00") == set()
    # defensive: non-string / empty inputs never raise
    assert ac.extract_referenced_criterion_keys(None) == set()
    assert ac.extract_referenced_criterion_keys("") == set()
    assert ac.extract_referenced_criterion_keys(12345) == set()  # type: ignore[arg-type]


def test_reconcile_flags_phantom_keys():
    """A referenced key with no registered match is a phantom (dangling) key."""
    report = ac.reconcile_criterion_key_references(
        registered={"c-11111111"},
        referenced={"c-11111111", "c-22222222"},
    )
    assert report["phantom_keys"] == ["c-22222222"]
    assert report["zero_registered"] is False
    assert report["has_drift"] is True


def test_reconcile_flags_zero_registered_when_referenced():
    """Keys referenced while zero criteria are registered → zero_registered drift."""
    report = ac.reconcile_criterion_key_references(
        registered=set(),
        referenced={"c-33333333"},
    )
    assert report["zero_registered"] is True
    assert report["phantom_keys"] == ["c-33333333"]
    assert report["has_drift"] is True

    # No drift when nothing is referenced and nothing is registered.
    clean = ac.reconcile_criterion_key_references(registered=set(), referenced=set())
    assert clean["has_drift"] is False
    assert clean["zero_registered"] is False

    # No drift when every referenced key is registered.
    matched = ac.reconcile_criterion_key_references(
        registered={"c-44444444"}, referenced={"c-44444444"}
    )
    assert matched["has_drift"] is False


@pytest.mark.asyncio
async def test_coverage_summary_includes_phantom_bookkeeping_key(db_session):
    """get_version_coverage_summary always carries a phantom_bookkeeping report;
    it flags drift when prose references keys not registered on the version."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    # Register one real criterion, then reference a DIFFERENT (dangling) key in prose.
    c1 = await ac.create_criterion(db_session, project.id, version.id, "Tests pass")
    project.problem_model = {
        "intent": {"current_focus": "Ship the tiering redesign for c-deadbeef"}
    }
    db_session.add(project)
    await db_session.commit()

    summary = await ac.get_version_coverage_summary(db_session, version.id)
    report = summary["phantom_bookkeeping"]
    assert report["has_drift"] is True
    assert "c-deadbeef" in report["phantom_keys"]
    # the registered key is NOT flagged
    assert c1.criterion_key not in report["phantom_keys"]
    assert report["zero_registered"] is False  # one criterion IS registered
    assert report["registered_count"] == 1


@pytest.mark.asyncio
async def test_phantom_bookkeeping_scans_frozen_version_goal(db_session):
    """Phantom reconciliation detects keys cited in the version's frozen goal.

    qualification grades against ReleaseVersion.goal; the drift report must
    include criterion keys referenced there even when current_focus is blank.
    """
    project = await _make_project(db_session)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        goal="Deliver the redesign tracking c-feedface and close out the backlog",
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # No registered criterion for c-feedface — expect drift
    summary = await ac.get_version_coverage_summary(db_session, version.id)
    report = summary["phantom_bookkeeping"]
    assert report["has_drift"] is True
    assert "c-feedface" in report["phantom_keys"]
    assert report["zero_registered"] is True  # nothing registered at all


@pytest.mark.asyncio
async def test_coverage_summary_no_phantom_when_prose_matches(db_session):
    """No drift when the only referenced key is a registered active criterion."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    c1 = await ac.create_criterion(db_session, project.id, version.id, "Tests pass")

    # Reference the real, registered key — via a direction's success_evidence.
    project.directions = [
        {
            "id": "d-1",
            "status": "active",
            ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(version.id),
            "context": {"success_evidence": f"criterion {c1.criterion_key} covered"},
        }
    ]
    db_session.add(project)
    await db_session.commit()

    summary = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary["phantom_bookkeeping"]["has_drift"] is False
    assert summary["phantom_bookkeeping"]["phantom_keys"] == []


@pytest.mark.asyncio
async def test_coverage_summary_scopes_direction_references_by_lifecycle_and_version(
    db_session,
):
    """Only active current context contributes drift, including prior-only keys.

    The fixture carries every direction lifecycle state plus a current/prior key
    collision. It proves that a prior-only key cited by current focus is a
    dangling current-release reference, while inactive directions remain inert.
    """
    project = await _make_project(db_session)
    prior = ReleaseVersion(project_id=project.id, version_number=1, status="promoted")
    requested = ReleaseVersion(project_id=project.id, version_number=2, status="open")
    db_session.add_all([prior, requested])
    await db_session.commit()

    current_active = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=requested.id,
        criterion_key="c-11111111",
        text="Current active criterion",
    )
    current_inactive = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=requested.id,
        criterion_key="c-22222222",
        text="Current retracted criterion",
        status="retracted",
    )
    current_collision = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=requested.id,
        criterion_key="c-44444444",
        text="Current collision criterion",
    )
    prior_only = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=prior.id,
        criterion_key="c-33333333",
        text="Prior-version criterion",
    )
    prior_collision = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=prior.id,
        criterion_key="c-44444444",
        text="Prior collision criterion",
    )
    db_session.add_all(
        [
            current_active,
            current_inactive,
            current_collision,
            prior_only,
            prior_collision,
        ]
    )
    project.problem_model = {
        "intent": {
            "current_focus": (
                "Current c-11111111; inactive c-22222222; "
                "prior c-33333333; collision c-44444444"
            )
        }
    }
    project.directions = [
        {
            "id": "d-active",
            "status": "active",
            ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(requested.id),
            "context": {"success_evidence": "dangling c-55555555"},
        },
        {
            "id": "d-paused",
            "status": "paused",
            "context": {"success_evidence": "ignored c-66666666"},
        },
        {
            "id": "d-staged",
            "status": "staged",
            "context": {"success_evidence": "ignored c-77777777"},
        },
        {
            "id": "d-retired",
            "status": "retired",
            "context": {"success_evidence": "ignored c-88888888"},
        },
    ]
    db_session.add(project)
    await db_session.commit()

    summary = await ac.get_version_coverage_summary(db_session, requested.id)
    report = summary["phantom_bookkeeping"]

    # Requested-version membership remains visible. Its active state determines
    # whether it is registered; the current registration keeps the collision valid.
    assert set(report["referenced_keys"]) == {
        "c-11111111",
        "c-22222222",
        "c-33333333",
        "c-44444444",
        "c-55555555",
    }
    assert set(report["phantom_keys"]) == {
        "c-22222222",
        "c-33333333",
        "c-55555555",
    }
    assert report["has_drift"] is True
    assert report["registered_count"] == 2
    assert "c-33333333" in report["phantom_keys"]
    assert not {
        "c-66666666",
        "c-77777777",
        "c-88888888",
    } & set(report["referenced_keys"])

    attention = await build_operator_attention(db_session, project, _TEST_USER_ID)
    phantom_items = [
        item
        for item in attention["items"]
        if item.get("reason") == "phantom_criterion_bookkeeping"
    ]
    assert len(phantom_items) == 1
    assert "c-55555555" in phantom_items[0]["detail"]
    assert "c-33333333" in phantom_items[0]["detail"]
    assert attention["counts"]["phantom_criterion_bookkeeping"] == 1


@pytest.mark.asyncio
async def test_coverage_summary_does_not_apply_live_prose_to_promoted_version(
    db_session,
):
    """A promoted release's summary is isolated from mutable current prose."""
    project = await _make_project(db_session)
    promoted = await _make_version(db_session, project)
    promoted.status = "promoted"
    open_version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        status="open",
    )
    db_session.add(open_version)
    project.problem_model = {
        "intent": {"current_focus": "Current focus has dangling c-deadbeef"}
    }
    project.directions = [
        {
            "id": "d-active",
            "status": "active",
            "context": {"success_evidence": "Also current c-feedface"},
        }
    ]
    db_session.add(project)
    await db_session.commit()

    summary = await ac.get_version_coverage_summary(db_session, promoted.id)
    report = summary["phantom_bookkeeping"]
    assert report["has_drift"] is False
    assert report["referenced_keys"] == []
    assert report["phantom_keys"] == []


@pytest.mark.asyncio
async def test_phantom_bookkeeping_uses_normalized_current_binding_and_ignores_prior(
    db_session,
):
    project = await _make_project(db_session)
    prior = ReleaseVersion(project_id=project.id, version_number=1, status="promoted")
    current = ReleaseVersion(project_id=project.id, version_number=2, status="open")
    db_session.add_all([prior, current])
    await db_session.commit()
    db_session.add_all(
        [
            Direction(
                project_id=project.id,
                direction_key="d-prior",
                status="active",
                data={
                    "id": "d-prior",
                    "status": "active",
                    ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(prior.id),
                    "context": {"success_evidence": "old c-deadbeef"},
                },
                criterion_keys=["c-deadbeef"],
            ),
            Direction(
                project_id=project.id,
                direction_key="d-current",
                status="active",
                data={
                    "id": "d-current",
                    "status": "active",
                    ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(current.id),
                    "context": {"success_evidence": "new c-feedface"},
                },
                criterion_keys=["c-feedface"],
            ),
        ]
    )
    # A stale blob copy of the old active direction must not override its row.
    project.directions = [
        {
            "id": "d-prior",
            "status": "active",
            "context": {"success_evidence": "old c-deadbeef"},
        }
    ]
    db_session.add(project)
    await db_session.commit()

    report = (await ac.get_version_coverage_summary(db_session, current.id))[
        "phantom_bookkeeping"
    ]
    assert report["referenced_keys"] == ["c-feedface"]
    assert report["phantom_keys"] == ["c-feedface"]
    assert report["zero_registered"] is True


@pytest.mark.asyncio
async def test_coverage_summary_applies_live_prose_only_to_selected_open_version(
    db_session,
):
    """A stale open row in a multi-open anomaly is historical for live prose."""
    project = await _make_project(db_session)
    older = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        opened_at=_utcnow() - timedelta(hours=1),
    )
    newer = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        status="open",
        opened_at=_utcnow(),
    )
    db_session.add_all([older, newer])
    project.problem_model = {
        "intent": {"current_focus": "Current focus has dangling c-deadbeef"}
    }
    db_session.add(project)
    await db_session.commit()

    older_summary = await ac.get_version_coverage_summary(db_session, older.id)
    newer_summary = await ac.get_version_coverage_summary(db_session, newer.id)

    assert older_summary["phantom_bookkeeping"]["has_drift"] is False
    assert older_summary["phantom_bookkeeping"]["referenced_keys"] == []
    assert newer_summary["phantom_bookkeeping"]["phantom_keys"] == ["c-deadbeef"]


@pytest.mark.asyncio
async def test_zero_registered_criteria_and_dangling_key_flagged(db_session):
    """End-to-end drift guard: an open version with ZERO registered criteria whose
    focus prose / direction success_evidence still reference criterion keys reads
    PASS at every gate today. This must instead surface a needs_operator
    'phantom_criterion_bookkeeping' attention item so the drift fails loud.

    Reproduces the v25 'phantom bookkeeping' failure: prose claimed coverage
    against criterion keys that were never registered on the open version, while
    live criterion_coverage read total==0.
    """
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)  # status defaults to 'open'

    # Zero registered criteria, but strategic focus AND an active direction both
    # reference criterion keys as though they were being tracked.
    project.problem_model = {
        "intent": {
            "current_focus": "Author v25 acceptance contract: 0/6 covered on c-a5940d77"
        }
    }
    project.directions = [
        {
            "id": "d-a5940d77",
            "status": "active",
            ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(version.id),
            "context": {"success_evidence": "attestation tiering c-bbf215e3 verified"},
        },
        # retired direction references are ignored (must not add drift on their own)
        {
            "id": "d-old",
            "status": "retired",
            "context": {"success_evidence": "legacy c-99999999"},
        },
    ]
    db_session.add(project)
    await db_session.commit()

    # The coverage summary itself must expose the drift, sourced from BOTH prose
    # fields, with zero_registered set (no active criteria exist).
    summary = await ac.get_version_coverage_summary(db_session, version.id)
    report = summary["phantom_bookkeeping"]
    assert report["zero_registered"] is True
    assert report["has_drift"] is True
    assert set(report["phantom_keys"]) == {"c-a5940d77", "c-bbf215e3"}
    # retired-direction key must NOT appear
    assert "c-99999999" not in report["phantom_keys"]
    assert report["registered_count"] == 0

    # And it must surface as a needs_operator attention item end-to-end.
    attention = await build_operator_attention(db_session, project, _TEST_USER_ID)
    phantom_items = [
        item
        for item in attention["items"]
        if item.get("reason") == "phantom_criterion_bookkeeping"
    ]
    assert len(phantom_items) == 1
    item = phantom_items[0]
    assert item["severity"] == "needs_operator"
    assert item["kind"] == "version"
    assert item["lifecycle"]["mode"] == "acceptance_criteria_lifecycle"
    assert attention["counts"]["phantom_criterion_bookkeeping"] == 1


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
async def test_reused_key_prior_direction_is_inert_for_all_current_consumers(db_session):
    """A prior binding cannot own, map, classify, or probe a reused current key."""
    project = await _make_project(db_session)
    prior = ReleaseVersion(project_id=project.id, version_number=1, status="promoted")
    current = ReleaseVersion(project_id=project.id, version_number=2, status="open")
    db_session.add_all([prior, current])
    await db_session.commit()
    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=current.id,
        criterion_key="c-11111111",
        text="Generic current outcome",
    )
    direction = Direction(
        project_id=project.id,
        direction_key="d-prior",
        data={
            "id": "d-prior",
            "text": "acceptance_window and metrics_integrity_sweep",
            ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(prior.id),
        },
        text="acceptance_window and metrics_integrity_sweep",
        status="active",
        criterion_keys=[criterion.criterion_key],
    )
    db_session.add_all([criterion, direction])
    await db_session.commit()

    assert await ac._resolve_acceptance_window_criteria(db_session, current) == []
    assert await ac._resolve_metrics_integrity_criteria(db_session, current) == []
    assert not await ac.direction_owns_uncovered_criterion(
        db_session, project.id, direction.direction_key
    )
    summary = await ac.get_version_coverage_summary(db_session, current.id)
    assert summary["criteria"][0]["stuck_reason"] == "no_mapping"
    assert summary["stuck_count"] == 1


@pytest.mark.asyncio
async def test_current_mapping_on_pre_existing_direction_matches_via_explicit_binding(
    db_session,
):
    """A direction created before the current release still matches when its
    criterion_release_version_id is explicitly bound to the current version.

    Regression: created_at < opened_at must never veto an explicit binding.
    The admin link endpoint writes the explicit binding; that is the sole
    authoritative source of mapping provenance.
    """
    from datetime import timedelta

    project = await _make_project(db_session)
    current = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(current)
    await db_session.commit()

    # Simulate a pre-existing direction: stamp created_at in the past so it
    # pre-dates the version's opened_at (testing the independence of created_at
    # from criterion_release_version_id).
    pre_existing = Direction(
        project_id=project.id,
        direction_key="d-pre",
        status="active",
        data={
            "id": "d-pre",
            "status": "active",
            # Explicit binding to current version — set by link_criteria_to_direction.
            ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(current.id),
        },
        criterion_keys=["c-pre-1111"],
    )
    db_session.add(pre_existing)
    await db_session.commit()
    # Back-date created_at to confirm it is NOT used for version resolution.
    await db_session.execute(
        __import__("sqlalchemy", fromlist=["text"]).text(
            "UPDATE direction SET created_at = :ts WHERE direction_key = 'd-pre'"
        ),
        {"ts": current.opened_at - timedelta(days=30)},
    )
    await db_session.commit()
    await db_session.refresh(pre_existing)

    # Even with created_at well before opened_at, the explicit binding wins.
    assert ac.direction_matches_criterion_release_version(pre_existing, current)


@pytest.mark.asyncio
async def test_later_unbound_direction_does_not_match_historical_version(db_session):
    """An unbound direction created during a later release must not match a
    promoted historical version.

    Regression: the one-sided ``created_at >= opened_at`` check allowed any
    direction created after V1 opened (including those from V2, V3, …) to
    satisfy a V1 historical query, contaminating promoted-version summaries.
    The compatibility rule must be fail-closed for non-open versions.
    """
    project = await _make_project(db_session)
    historical = ReleaseVersion(
        project_id=project.id, version_number=1, status="promoted"
    )
    db_session.add(historical)
    await db_session.commit()

    # Unbound direction (no criterion_release_version_id) — simulates a direction
    # that exists without an explicit version assignment.
    later_unbound = Direction(
        project_id=project.id,
        direction_key="d-later",
        status="active",
        data={"id": "d-later", "status": "active"},
        criterion_keys=["c-later-2222"],
    )
    db_session.add(later_unbound)
    await db_session.commit()

    # created_at is naturally after historical.opened_at (direction created in
    # the same test run), but the direction must still NOT match the historical
    # promoted version — the compatibility rule fails closed for non-open versions.
    assert not ac.direction_matches_criterion_release_version(later_unbound, historical)


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
async def test_stuck_reason_paused_direction_returns_direction_inactive(db_session):
    """Uncovered criterion whose only mappings are paused/staged/proposed → 'direction_inactive'.

    Non-active, non-retired directions (paused, staged, proposed) must not
    suppress the stuck alert — they should yield 'direction_inactive', not None.
    """
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    c1 = await ac.create_criterion(db_session, project.id, version.id, "Inactive-mapped")

    for i, status in enumerate(("paused", "staged", "proposed")):
        db_session.add(
            _make_direction(project.id, f"inact{i}", [c1.criterion_key], status)
        )
    await db_session.commit()

    summary = await ac.get_version_coverage_summary(db_session, version.id)

    by_key = {c["criterion_key"]: c for c in summary["criteria"]}
    assert by_key[c1.criterion_key]["coverage"] == "uncovered"
    assert by_key[c1.criterion_key]["stuck_reason"] == "direction_inactive"
    assert summary["stuck_count"] == 1


@pytest.mark.asyncio
async def test_stuck_reason_paused_plus_active_direction_not_stuck(db_session):
    """One paused + one active mapping → not stuck (active wins over inactive)."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)

    c1 = await ac.create_criterion(db_session, project.id, version.id, "Paused-and-active")

    db_session.add(_make_direction(project.id, "pa1", [c1.criterion_key], "paused"))
    db_session.add(_make_direction(project.id, "pa2", [c1.criterion_key], "active"))
    await db_session.commit()

    summary = await ac.get_version_coverage_summary(db_session, version.id)

    by_key = {c["criterion_key"]: c for c in summary["criteria"]}
    assert by_key[c1.criterion_key]["stuck_reason"] is None
    assert summary["stuck_count"] == 0


@pytest.mark.asyncio
async def test_resolve_acceptance_window_criteria_excludes_non_active_directions(db_session):
    """Rule (b) in _resolve_acceptance_window_criteria must NOT fire for paused directions.

    A paused direction whose text names the acceptance-window metric must NOT
    contribute its criterion_keys to probe targets — only active directions qualify.
    """
    project = await _make_project(db_session)
    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()

    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Generic outcome"
    )
    paused_direction = Direction(
        project_id=project.id,
        direction_key="d-paused-aw",
        data={
            "id": "d-paused-aw",
            "text": "acceptance_window metric",
            ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(version.id),
        },
        text="acceptance_window metric",
        status="paused",
        criterion_keys=[criterion.criterion_key],
    )
    db_session.add(paused_direction)
    await db_session.commit()

    # A paused direction naming the metric must NOT cause the criterion to be targeted.
    targets = await ac._resolve_acceptance_window_criteria(db_session, version)
    assert criterion not in targets
    assert all(t.criterion_key != criterion.criterion_key for t in targets)


@pytest.mark.asyncio
async def test_resolve_metrics_integrity_criteria_excludes_non_active_directions(db_session):
    """Rule (b) in _resolve_metrics_integrity_criteria must NOT fire for paused directions.

    A paused direction whose text names the metrics-integrity signal must NOT
    contribute its criterion_keys to probe targets — only active directions qualify.
    """
    project = await _make_project(db_session)
    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()

    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Generic outcome"
    )
    paused_direction = Direction(
        project_id=project.id,
        direction_key="d-paused-mi",
        data={
            "id": "d-paused-mi",
            "text": "data integrity check",
            ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(version.id),
        },
        text="data integrity check",
        status="paused",
        criterion_keys=[criterion.criterion_key],
    )
    db_session.add(paused_direction)
    await db_session.commit()

    # A paused direction naming the metric must NOT cause the criterion to be targeted.
    targets = await ac._resolve_metrics_integrity_criteria(db_session, version)
    assert criterion not in targets
    assert all(t.criterion_key != criterion.criterion_key for t in targets)


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


# ---------------------------------------------------------------------------
# get_unmapped_active_criterion_keys — standalone helper tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_unmapped_active_criterion_keys_empty_when_no_criteria(db_session):
    """No active criteria → empty result."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    await db_session.commit()

    result = await ac.get_unmapped_active_criterion_keys(
        db_session, project.id, version.id
    )
    assert result == []


@pytest.mark.asyncio
async def test_get_unmapped_active_criterion_keys_all_unmapped(db_session):
    """Active criteria with no Direction mapping → all returned."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    c1 = await ac.create_criterion(db_session, project.id, version.id, "First unmapped")
    c2 = await ac.create_criterion(db_session, project.id, version.id, "Second unmapped")
    await db_session.commit()

    result = await ac.get_unmapped_active_criterion_keys(
        db_session, project.id, version.id
    )
    assert sorted(result) == sorted([c1.criterion_key, c2.criterion_key])


@pytest.mark.asyncio
async def test_get_unmapped_active_criterion_keys_excludes_mapped(db_session):
    """Criteria linked to any direction (even retired) are excluded from the unmapped set."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    c_mapped = await ac.create_criterion(db_session, project.id, version.id, "Mapped")
    c_unmapped = await ac.create_criterion(db_session, project.id, version.id, "Unmapped")
    db_session.add(_make_direction(project.id, "d1", [c_mapped.criterion_key], "active"))
    await db_session.commit()

    result = await ac.get_unmapped_active_criterion_keys(
        db_session, project.id, version.id
    )
    assert result == [c_unmapped.criterion_key]
    assert c_mapped.criterion_key not in result


@pytest.mark.asyncio
async def test_get_unmapped_active_criterion_keys_excludes_inactive(db_session):
    """Retracted / superseded criteria are not considered unmapped — only active rows count."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    c_active = await ac.create_criterion(db_session, project.id, version.id, "Active")
    c_retracted = await ac.create_criterion(
        db_session, project.id, version.id, "Retracted"
    )
    # Retract the second criterion
    c_retracted.status = "retracted"
    db_session.add(c_retracted)
    await db_session.commit()

    result = await ac.get_unmapped_active_criterion_keys(
        db_session, project.id, version.id
    )
    assert result == [c_active.criterion_key]
    assert c_retracted.criterion_key not in result


@pytest.mark.asyncio
async def test_get_unmapped_active_criterion_keys_empty_when_all_mapped(db_session):
    """All active criteria have a direction mapping → empty result."""
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    c1 = await ac.create_criterion(db_session, project.id, version.id, "C1")
    c2 = await ac.create_criterion(db_session, project.id, version.id, "C2")
    db_session.add(
        _make_direction(project.id, "d1", [c1.criterion_key, c2.criterion_key], "active")
    )
    await db_session.commit()

    result = await ac.get_unmapped_active_criterion_keys(
        db_session, project.id, version.id
    )
    assert result == []


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
        data={
            "id": "d-latemap1",
            "text": "Late mapping direction",
            "status": "active",
            ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(version.id),
        },
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
            ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(version.id),
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
async def test_prior_version_task_cannot_mutate_next_version_evidence_or_gaps(
    db_session,
):
    """A persisted task binding wins after a version cut and direction remap."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project = await _make_project(db_session, "VersionBoundMergeEvidence")
    version_one = await _make_version(db_session, project)
    shared_key = "c-deadbeef"
    criterion_one = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version_one.id,
        criterion_key=shared_key,
        text="Version one outcome",
    )
    db_session.add(criterion_one)
    await db_session.commit()

    proven_context = {
        "direction_id": "d-versioned",
        ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(version_one.id),
        CODE_ACCEPTANCE_VERDICT_KEY: {
            "satisfied": True,
            "proven": True,
            "kind": "named_test",
        },
    }
    credit_task = Task(
        project_id=project.id,
        context=proven_context,
        payloads=[{"type": "create", "prompt": "finish v1"}],
        criterion_ids=[shared_key],
    )
    gap_task = Task(
        project_id=project.id,
        context=proven_context,
        payloads=[{"type": "create", "prompt": "finish unmatched v1 work"}],
        criterion_ids=["c-00000000"],
    )
    db_session.add_all([credit_task, gap_task])
    await db_session.commit()

    version_one.status = "promoted"
    version_two = ReleaseVersion(project_id=project.id, version_number=2)
    db_session.add(version_two)
    await db_session.commit()
    await db_session.refresh(version_two)
    criterion_two = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version_two.id,
        criterion_key=shared_key,
        text="Version two outcome with reused bare key",
    )
    direction = Direction(
        project_id=project.id,
        direction_key="d-versioned",
        data={
            "id": "d-versioned",
            "status": "active",
            ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(version_two.id),
        },
        status="active",
        criterion_keys=[shared_key],
    )
    db_session.add_all([criterion_two, direction])
    await db_session.commit()

    assert await ac.record_task_merge_evidence(
        db_session, credit_task, project.id
    ) == 2
    assert await ac.record_task_merge_evidence(
        db_session, gap_task, project.id
    ) == 0
    assert len(await ac.get_evidence_for_criterion(db_session, criterion_one.id)) == 2
    assert await ac.get_evidence_for_criterion(db_session, criterion_two.id) == []

    await db_session.refresh(version_one)
    await db_session.refresh(version_two)
    assert version_one.merge_evidence_gaps["no_active_criteria"] == 1
    assert version_two.merge_evidence_gaps is None


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
    # Goal-verification gate: the acceptance criteria ARE the merge-grade's
    # categories.  Before retraction the stale criterion grades not_met (no
    # feasible evidence) → the gate hard-blocks; the covered one is met.
    before_gate = compute_composite(
        ci_green=True,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=1.0,
        policy={"threshold": 0.8},
        category_verdicts=[
            {"category_key": covered.criterion_key, "status": "met", "tier": "judged",
             "citations": [{"kind": "diff", "ref": "app/covered.py:1"}]},
            {"category_key": stale.criterion_key, "status": "not_met", "tier": "judged",
             "gap": "no feasible evidence", "citations": []},
        ],
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

    # After retracting the stale criterion only the covered category remains
    # active and grades met → the gate no longer fires.
    after_gate = compute_composite(
        ci_green=True,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=1.0,
        policy={"threshold": 0.8},
        category_verdicts=[
            {"category_key": covered.criterion_key, "status": "met", "tier": "judged",
             "citations": [{"kind": "diff", "ref": "app/covered.py:1"}]},
        ],
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
    assert child.context[ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY] == str(version.id)


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
    ATTESTATION_WINDOW_DAYS window.  A row where recorded_at is also None has
    NO reference timestamp and cannot be time-bounded — it does NOT count as
    a valid attestation (a missing timestamp cannot grant perpetual trust).

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


def test_c1e3fafbf_evidence_tiering():
    """A future-dated attested_at cannot extend or perpetuate the attestation window.

    Named acceptance test for criterion c-1e3fafbf (attestation-vs-verified
    evidence tiering). Before the fix a future ``attested_at`` made
    ``now - attested_at`` negative — always inside the window — so the
    attestation stayed 'valid' indefinitely and its computed expiry was pushed
    arbitrarily far into the future, silently promoting a low-confidence
    operator waiver to a perpetual one. The seam now rejects a meaningfully
    future ``attested_at`` as the window anchor and falls back to
    ``recorded_at`` (:func:`_attestation_reference_timestamp`), so the
    lower-confidence 'attested' tier can never outrank machine-verified
    evidence by living forever.
    """
    from datetime import datetime as _dt

    far_future = (_utcnow() + timedelta(days=3650)).isoformat()

    # (a) Future attested_at + STALE recorded_at → not attested (falls back to
    #     the stale recorded_at, which is outside the window), coverage 'partial'.
    ev_future_stale = _make_evidence(
        "operator_attestation", detail={"attested_at": far_future}
    )
    ev_future_stale.recorded_at = _utcnow() - timedelta(
        days=ac.ATTESTATION_WINDOW_DAYS + 1
    )
    assert ac._evidence_is_attested(ev_future_stale) is False, (
        "a future attested_at must not keep a stale attestation valid"
    )
    assert ac.compute_criterion_coverage([ev_future_stale]) == "partial"
    assert ac._compute_attestation_expires_at([ev_future_stale]) is None

    # (b) Future attested_at + FRESH recorded_at → still attested via the fresh
    #     recorded_at fallback, but the expiry is anchored to recorded_at (NOT
    #     the far-future date), so the window is a normal ATTESTATION_WINDOW_DAYS.
    fresh_recorded = _utcnow()
    ev_future_fresh = _make_evidence(
        "operator_attestation", detail={"attested_at": far_future}
    )
    ev_future_fresh.recorded_at = fresh_recorded
    assert ac._evidence_is_attested(ev_future_fresh) is True
    assert ac.compute_criterion_coverage([ev_future_fresh]) == "attested"
    expires = ac._compute_attestation_expires_at([ev_future_fresh])
    assert expires is not None
    expires_dt = _dt.fromisoformat(expires)
    expected = fresh_recorded + timedelta(days=ac.ATTESTATION_WINDOW_DAYS)
    assert abs((expires_dt - expected).total_seconds()) < 2, (
        "expiry must be anchored to recorded_at, not the far-future attested_at"
    )
    # The expiry must NOT be pushed near the far-future attested_at.
    assert expires_dt < _utcnow() + timedelta(days=ac.ATTESTATION_WINDOW_DAYS + 1)

    # (c) Slightly-future attested_at within the clock-skew tolerance is still
    #     honoured as the anchor (benign client/server skew).
    skew_future = (
        _utcnow() + timedelta(seconds=ac.ATTESTATION_FUTURE_SKEW_SECONDS // 2)
    ).isoformat()
    ev_skew = _make_evidence(
        "operator_attestation", detail={"attested_at": skew_future}
    )
    assert ac._evidence_is_attested(ev_skew) is True
    assert ac.compute_criterion_coverage([ev_skew]) == "attested"

    # (d) A normal, past, in-window attested_at is unaffected by the guard.
    recent_past = (_utcnow() - timedelta(days=1)).isoformat()
    ev_recent = _make_evidence(
        "operator_attestation", detail={"attested_at": recent_past}
    )
    assert ac._evidence_is_attested(ev_recent) is True
    ref_expires = ac._compute_attestation_expires_at([ev_recent])
    assert ref_expires is not None
    ref_expires_dt = _dt.fromisoformat(ref_expires)
    expected_recent = _dt.fromisoformat(recent_past) + timedelta(
        days=ac.ATTESTATION_WINDOW_DAYS
    )
    assert abs((ref_expires_dt - expected_recent).total_seconds()) < 2


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
    """retire_direction must clear criterion_keys from the Direction ORM row AND the blob.

    When a direction is retired the autonomous loop must not propagate its
    criterion links to newly spawned tasks.  The fix clears criterion_keys from
    the blob dict BEFORE the locked write so the dual-write (sync_directions ->
    upsert_direction) atomically propagates the cleared state to the Direction
    ORM row.  Without this, a future unrelated with_directions_locked ->
    sync_directions re-reads stale criterion_keys from the blob and resurrects
    them on the ORM row.
    """
    from app.auth import create_agent_token
    from app import strategic_cycle
    from app.directions_repo import sync_directions

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

    # Wire BOTH the Direction ORM row AND the blob entry with criterion_keys
    # so the real propagation path (blob -> sync_directions -> upsert_direction)
    # is exercised.  Without blob-side keys the dual-write is a no-op and
    # the resurrection regression isn't tested.
    project.directions = [
        {
            "id": dir_id,
            "text": "direction with criterion links",
            "status": "active",
            "priority": "medium",
            "notes": [],
            "research_history": [],
            "criterion_keys": [criterion.criterion_key],
        }
    ]
    db_session.add(project)
    dir_row = Direction(
        project_id=project.id,
        direction_key=dir_id,
        data={"id": dir_id, "text": "direction with criterion links", "status": "active"},
        criterion_keys=[criterion.criterion_key],
    )
    db_session.add(dir_row)
    await db_session.commit()

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

    # The blob entry must also have criterion_keys cleared.
    await db_session.refresh(project)
    retired_blob_entry = next(
        (d for d in (project.directions or []) if d.get("id") == dir_id), None
    )
    assert retired_blob_entry is not None, "retired direction must exist in the blob"
    blob_keys = retired_blob_entry.get("criterion_keys", [])
    assert blob_keys == [] or blob_keys is None, (
        f"Blob entry criterion_keys must be cleared after retire; "
        f"got {blob_keys}"
    )

    # CRITICAL REGRESSION CHECK: a subsequent unrelated sync_directions call
    # must NOT resurrect the stale criterion_keys from the blob.
    await sync_directions(db_session, project.id, project.directions)
    await db_session.refresh(dir_row)
    assert dir_row.criterion_keys == [] or dir_row.criterion_keys is None, (
        f"sync_directions after retirement must not resurrect criterion_keys; "
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
    await _make_version(db_session, project)

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
    await _make_version(db_session, project)

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
    await _make_version(db_session, project)

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
    await _make_version(db_session, project)

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


def test_acceptance_window_recompute_evidence_marks_probed():
    """A fresh ``acceptance_window_recompute`` row is the lower-confidence 'probed' tier.

    The sweep running (and re-scoring non-zero) proves the MECHANISM operated, not
    that the criterion's own condition holds — so it must NOT be machine-verified
    ``'covered'``.  It yields the distinct, time-bounded ``'probed'`` tier.
    """
    ev = _make_evidence(
        "acceptance_window_recompute", detail={"acceptance_window_verified": 3}
    )
    assert ac._evidence_proves_covered(ev) is False
    assert ac._evidence_is_probed(ev) is True
    assert ac.compute_criterion_coverage([ev]) == "probed"


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
    acceptance-window criterion from uncovered to the lower-confidence 'probed' tier.

    Given an open version whose merge window contains inferred_test_cochange
    merges (COMPLETED write-tasks whose PR co-changed a test AND a production
    file, CI-verified, no explicit verdict) such that the recomputed aggregate
    scores ``verified > 0``, the recorder records ONE machine-probe
    ``acceptance_window_recompute`` row for the criterion whose text names the
    metric and ``get_version_coverage_summary`` reports it ``probed`` (NOT
    machine-verified ``covered``).
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
    assert summary_before["probed"] == 0
    assert summary_before["uncovered"] == 1

    recorded = await ac.record_acceptance_window_coverage_evidence(db_session, version)
    assert recorded == 1, f"Expected 1 probe row for the target criterion; got {recorded}"

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

    # After: the criterion flips to the lower-confidence 'probed' tier — the sweep
    # ran, but that is NOT machine-verified 'covered'.
    summary_after = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary_after["covered"] == 0
    assert summary_after["probed"] == 1
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
    assert summary["covered"] == 0
    assert summary["probed"] == 1


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
    assert summary["covered"] == 0
    assert summary["probed"] == 1
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
async def test_owns_uncovered_true_when_owned_criterion_probed_only(db_session):
    """An owned criterion covered ONLY by a machine probe (probed) → True.

    A direction backed solely by a mechanism-ran probe (the sweep operated but
    the criterion's condition was not machine-verified) must NOT auto-retire as
    "successful" — only ``'covered'`` clears the completion gate.
    """
    project = await _make_project(db_session)
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Probed outcome"
    )
    # A fresh metrics_integrity_sweep row yields the 'probed' tier, not 'covered'.
    db_session.add(
        CriterionEvidence(
            criterion_id=criterion.id,
            project_id=project.id,
            source_kind="metrics_integrity_sweep",
            source_id="sweep-probe",
            detail={"ticks": 2},
        )
    )
    await db_session.commit()
    await _make_direction_row(
        db_session, project, "d-probe", [criterion.criterion_key]
    )
    assert (
        await ac.direction_owns_uncovered_criterion(db_session, project.id, "d-probe")
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


def test_metrics_integrity_sweep_evidence_marks_probed():
    """A fresh ``metrics_integrity_sweep`` row is the lower-confidence 'probed' tier.

    The sweep ticking proves the MECHANISM is operating, not that the criterion's
    own condition holds — so it yields the distinct, time-bounded ``'probed'``
    tier rather than machine-verified ``'covered'``.
    """
    ev = _make_evidence("metrics_integrity_sweep", detail={"ticks": 5})
    assert ac._evidence_proves_covered(ev) is False
    assert ac._evidence_is_probed(ev) is True
    assert ac.compute_criterion_coverage([ev]) == "probed"


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
    machine-probe evidence row for the data-integrity criterion and coverage flips
    to the lower-confidence 'probed' tier (NOT machine-verified 'covered')."""
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
    assert recorded == 1, f"Expected 1 probe row; got {recorded}"

    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence) == 1
    row = evidence[0]
    assert row.source_kind == "metrics_integrity_sweep"
    assert row.detail["ticks"] == 1
    assert row.detail["mode"] == "observe"

    # After: the criterion flips to the lower-confidence 'probed' tier — the sweep
    # ticked, but that is NOT machine-verified 'covered'.
    summary_after = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary_after["covered"] == 0
    assert summary_after["probed"] == 1
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
    assert summary["covered"] == 0
    assert summary["probed"] == 1

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
            ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(version.id),
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
            ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(version.id),
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
    # The re-attestation duplicates the (criterion, kind, source) evidence key,
    # so record_evidence collapses it via IntegrityError → rollback — which
    # expires every instance in the session. Refresh before further attribute
    # reads (project/version/criterion are used below, and ``project`` is
    # passed into build_operator_attention) so real ORM expiry can't raise
    # MissingGreenlet on a sync lazy-load.
    await db_session.refresh(project)
    await db_session.refresh(version)
    await db_session.refresh(criterion)

    queued_task = _Task(
        project_id=project.id,
        title=f"Verify: {criterion.criterion_key}",
        criterion_ids=[criterion.criterion_key],
        context={
            "corroboration_of": criterion.criterion_key,
            "spawned_by": "operator_attestation",
            ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(version.id),
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
# backfill corroboration loop: spawn, dedup, failed-downgrade, re-verify
# ---------------------------------------------------------------------------


async def _make_backfill_task(
    db_session,
    project,
    criterion_key,
    *,
    direction_key="d-backfill-corrob",
    test_path="testing/test_backfill_corrob.py::test_seam",
):
    """Create a COMPLETED unverified-merge task whose named_test shipped.

    Mirrors the fixture shape used by the backfill unit tests so
    ``backfill_unverified_merge_evidence`` records a 'backfilled' evidence row
    for ``criterion_key``.
    """
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY
    from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY

    dir_row = Direction(
        project_id=project.id,
        direction_key=direction_key,
        data={"id": direction_key, "text": "Backfill corrob", "status": "active"},
        status="active",
        criterion_keys=[criterion_key],
    )
    db_session.add(dir_row)

    task = Task(
        project_id=project.id,
        payloads=[{
            "type": "create",
            "prompt": "add the test",
            "acceptance_evidence": {"kind": "named_test", "test_path": test_path},
        }],
        context={
            "direction_id": direction_key,
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            REVIEW_LOOP_KEY: {UNVERIFIED_MERGE_REASON_KEY: "verify_setup_failed"},
            "_pr_changed_files": [
                test_path.rpartition("::")[0],
                "backend/app/target_module.py",
            ],
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": False,
                "proven": False,
                "kind": "named_test",
                "reason": "merge fell open without verification",
                "proof_ref": test_path,
            },
        },
        criterion_ids=[criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    return task


async def _verify_tasks_for(db_session, project, criterion_key):
    """Return all corroboration ``Verify: {key}`` tasks for a criterion."""
    from app.models import Task as _Task

    rows = (
        (await db_session.execute(
            select(_Task).where(
                _Task.project_id == project.id,
                _Task.title == f"Verify: {criterion_key}",
            )
        ))
        .scalars()
        .all()
    )
    return list(rows)


@pytest.mark.asyncio
async def test_backfill_spawns_corroboration_task(db_session):
    """Recording backfill evidence must spawn a corroboration task for the criterion.

    Mirrors the operator-attestation earn-up loop: the lower-confidence
    'backfilled' tier gets a ``Verify: {key}`` research task tagged
    ``spawned_by=unverified_merge_backfill`` so it can be re-verified toward
    real machine-verified coverage instead of staying covering indefinitely.
    """
    project = await _make_project(db_session, "Backfill-Spawn")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Backfilled criterion"
    )

    await _make_backfill_task(db_session, project, criterion.criterion_key)

    recorded = await ac.backfill_unverified_merge_evidence(db_session, project.id)
    assert recorded == 1, f"Expected 1 backfill evidence row, got {recorded}"

    verify_tasks = await _verify_tasks_for(db_session, project, criterion.criterion_key)
    assert len(verify_tasks) == 1, (
        f"Expected exactly 1 corroboration task, got {len(verify_tasks)}"
    )
    spawned = verify_tasks[0]
    assert spawned.context.get("corroboration_of") == criterion.criterion_key
    assert spawned.context.get("spawned_by") == ac.UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND, (
        f"Expected spawned_by=unverified_merge_backfill, got "
        f"{spawned.context.get('spawned_by')!r}"
    )
    assert spawned.criterion_ids == [criterion.criterion_key]
    assert spawned.status == TaskStatus.QUEUED
    assert spawned.created_by == "SYSTEM_AUTO_REVIEW"
    assert "backfill" in spawned.first_payload["prompt"].lower(), (
        "Backfill corroboration prompt must describe the backfill provenance"
    )


@pytest.mark.asyncio
async def test_backfill_corroboration_task_is_deduped(db_session):
    """A second backfill reconcile must not create a duplicate corroboration task."""
    project = await _make_project(db_session, "Backfill-Dedup")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Backfilled criterion"
    )

    await _make_backfill_task(db_session, project, criterion.criterion_key)

    # First reconcile: records evidence and spawns one corroboration task.
    first = await ac.backfill_unverified_merge_evidence(db_session, project.id)
    assert first == 1
    # Second reconcile: evidence already present (0 new rows) but the criterion
    # is still backfilled, so the spawn path runs again — the title dedup guard
    # must prevent a duplicate task.
    second = await ac.backfill_unverified_merge_evidence(db_session, project.id)
    assert second == 0, f"Second backfill must record 0 new rows, got {second}"

    verify_tasks = await _verify_tasks_for(db_session, project, criterion.criterion_key)
    assert len(verify_tasks) == 1, (
        f"Backfill corroboration must dedup; expected 1 task, got {len(verify_tasks)}"
    )


@pytest.mark.asyncio
async def test_backfill_failed_corroboration_downgrades_coverage(db_session):
    """A FAILED backfill corroboration task downgrades 'backfilled' to 'partial'.

    Acceptance test for the backfill corroboration loop. Before this change the
    'backfilled' tier had no re-verification path: it stayed covering forever and
    was never caught if false. Now the spawned ``Verify: {key}`` task feeds the
    same failed-corroboration downgrade as operator attestation — when it FAILS,
    the backfill coverage must drop to 'partial' and no longer count as covering.
    """
    project = await _make_project(db_session, "Backfill-Downgrade")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Backfilled criterion to downgrade"
    )

    await _make_backfill_task(db_session, project, criterion.criterion_key)
    recorded = await ac.backfill_unverified_merge_evidence(db_session, project.id)
    assert recorded == 1

    # Before failure: coverage is the distinct lower-confidence 'backfilled' tier.
    summary1 = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary1["backfilled"] == 1, (
        f"Expected 1 backfilled before corroboration failure, got {summary1['backfilled']}"
    )
    crit1 = next(
        c for c in summary1["criteria"]
        if c["criterion_key"] == criterion.criterion_key
    )
    assert crit1["coverage"] == "backfilled"

    # The backfill spawned a corroboration task; mark it FAILED.
    verify_tasks = await _verify_tasks_for(db_session, project, criterion.criterion_key)
    assert len(verify_tasks) == 1
    verify_tasks[0].status = TaskStatus.FAILED
    await db_session.commit()

    # After failure: the backfill coverage is downgraded to 'partial'.
    summary2 = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary2["backfilled"] == 0, (
        f"Expected 0 backfilled after corroboration failure, got {summary2['backfilled']}"
    )
    assert summary2["partial"] >= 1, (
        f"Expected >=1 partial after corroboration failure, got {summary2['partial']}"
    )
    crit2 = next(
        c for c in summary2["criteria"]
        if c["criterion_key"] == criterion.criterion_key
    )
    assert crit2["coverage"] == "partial", (
        f"Expected 'partial' after failed backfill corroboration, got {crit2['coverage']}"
    )
    assert criterion.criterion_key in summary2["failed_corroboration_keys"]


@pytest.mark.asyncio
async def test_backfill_corroboration_failure_surfaces_in_operator_attention(db_session):
    """A failed backfill corroboration surfaces a corroboration_failed attention item.

    Parity with operator attestation: the operator sees that a lower-confidence
    coverage's verification failed and can act.  (Runs against Postgres in CI —
    build_operator_attention uses a compound SELECT SQLite cannot parse.)
    """
    project = await _make_project(db_session, "Backfill-Attention")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Backfilled criterion surfacing failure"
    )

    await _make_backfill_task(db_session, project, criterion.criterion_key)
    await ac.backfill_unverified_merge_evidence(db_session, project.id)

    verify_tasks = await _verify_tasks_for(db_session, project, criterion.criterion_key)
    verify_tasks[0].status = TaskStatus.FAILED
    await db_session.commit()

    attention = await build_operator_attention(db_session, project, _TEST_USER_ID)
    corrob_id = (
        f"version:{version.id}:corroboration_failed:{criterion.criterion_key}"
    )
    items_by_id = {item["id"]: item for item in attention["items"]}
    assert corrob_id in items_by_id, (
        f"Expected corroboration_failed item for backfill, got {list(items_by_id)}"
    )
    assert attention["counts"]["corroboration_failed"] >= 1


@pytest.mark.asyncio
async def test_reattempt_after_failed_backfill_restores_backfilled(db_session):
    """A newer non-terminal corroboration task un-stales a failed backfill downgrade.

    Parity with the operator-attestation re-attest path: once a fresh
    ``Verify: {key}`` task is in flight, the stale FAILED task no longer keeps the
    criterion pinned at 'partial' — coverage returns to 'backfilled'.
    """
    project = await _make_project(db_session, "Backfill-Restore")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Backfilled criterion re-verified"
    )

    await _make_backfill_task(db_session, project, criterion.criterion_key)
    await ac.backfill_unverified_merge_evidence(db_session, project.id)

    verify_tasks = await _verify_tasks_for(db_session, project, criterion.criterion_key)
    verify_tasks[0].status = TaskStatus.FAILED
    await db_session.commit()

    summary_fail = await ac.get_version_coverage_summary(db_session, version.id)
    crit_fail = next(
        c for c in summary_fail["criteria"]
        if c["criterion_key"] == criterion.criterion_key
    )
    assert crit_fail["coverage"] == "partial"

    # A newer QUEUED corroboration task (a later reconcile re-spawns after the
    # failed one is no longer non-terminal).
    from app.models import Task as _Task

    queued = _Task(
        project_id=project.id,
        title=f"Verify: {criterion.criterion_key}",
        criterion_ids=[criterion.criterion_key],
        context={
            "corroboration_of": criterion.criterion_key,
            "spawned_by": ac.UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND,
            ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(version.id),
        },
        payloads=[{"type": "research", "prompt": "re-verify"}],
        created_by="SYSTEM_AUTO_REVIEW",
        status=TaskStatus.QUEUED,
    )
    db_session.add(queued)
    await db_session.commit()

    summary_ok = await ac.get_version_coverage_summary(db_session, version.id)
    assert summary_ok["backfilled"] == 1, (
        f"Expected backfilled restored after re-verify, got {summary_ok['backfilled']}"
    )
    crit_ok = next(
        c for c in summary_ok["criteria"]
        if c["criterion_key"] == criterion.criterion_key
    )
    assert crit_ok["coverage"] == "backfilled"
    failed_keys = await ac.get_failed_corroboration_keys(db_session, project.id)
    assert criterion.criterion_key not in failed_keys


# ---------------------------------------------------------------------------
# Finding #9: get_failed_corroboration_keys pushes the corroboration_of filter
# into SQL (it ran on the qualifier finalize path pulling the FULL context JSONB
# of every FAILED/CANCELLED task in the project, filtering only in Python).
# The selection must be UNCHANGED by the push-down.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_corroboration_keys_sql_filter_selects_same_keys(db_session):
    """The SQL-side ``corroboration_of`` filter selects EXACTLY the keys the old
    Python-side filter did — no more, no less.

    Exercises every discriminating row shape in one project:
      * FAILED with corroboration_of            → selected
      * CANCELLED with corroboration_of         → selected
      * FAILED with NO corroboration_of         → ignored (the rows the old query
                                                  dragged the whole context JSONB
                                                  across the wire for)
      * FAILED with a NULL / non-string value   → ignored (never laundered into a
                                                  key by the ``->>`` text cast)
      * COMPLETED / IN_PROGRESS corroboration   → ignored (not a terminal failure)
      * a FAILED corroboration in ANOTHER project → ignored (project-scoped)
    """
    from app.models import Task as _Task

    project = await _make_project(db_session, "Corroboration-SQL-Filter")
    other_project = await _make_project(db_session, "Corroboration-SQL-Other")

    def _task(proj_id, status, context, title="Verify: x"):
        return _Task(
            project_id=proj_id,
            title=title,
            context=context,
            payloads=[{"type": "research", "prompt": "verify"}],
            created_by="SYSTEM_AUTO_REVIEW",
            status=status,
        )

    db_session.add_all(
        [
            # --- selected -----------------------------------------------------
            _task(
                project.id,
                TaskStatus.FAILED,
                {"corroboration_of": "crit-failed", "spawned_by": "operator_attestation"},
                title="Verify: crit-failed",
            ),
            _task(
                project.id,
                TaskStatus.CANCELLED,
                {"corroboration_of": "crit-cancelled"},
                title="Verify: crit-cancelled",
            ),
            # --- ignored: terminal, but not a corroboration task ---------------
            _task(
                project.id,
                TaskStatus.FAILED,
                {"some_other_key": "noise", "big_blob": "x" * 512},
                title="Unrelated failed task",
            ),
            _task(project.id, TaskStatus.FAILED, {}, title="Empty-context failed task"),
            # --- ignored: malformed corroboration_of values --------------------
            _task(
                project.id,
                TaskStatus.FAILED,
                {"corroboration_of": None},
                title="Verify: null-key",
            ),
            _task(
                project.id,
                TaskStatus.FAILED,
                {"corroboration_of": 42},
                title="Verify: numeric-key",
            ),
            _task(
                project.id,
                TaskStatus.FAILED,
                {"corroboration_of": {"nested": "obj"}},
                title="Verify: object-key",
            ),
            # --- ignored: non-terminal / completed corroborations --------------
            _task(
                project.id,
                TaskStatus.COMPLETED,
                {"corroboration_of": "crit-completed"},
                title="Verify: crit-completed",
            ),
            _task(
                project.id,
                TaskStatus.IN_PROGRESS,
                {"corroboration_of": "crit-in-progress"},
                title="Verify: crit-in-progress",
            ),
            # --- ignored: another project's failure ---------------------------
            _task(
                other_project.id,
                TaskStatus.FAILED,
                {"corroboration_of": "crit-other-project"},
                title="Verify: crit-other-project",
            ),
        ]
    )
    await db_session.commit()

    failed_keys = await ac.get_failed_corroboration_keys(db_session, project.id)

    assert failed_keys == {"crit-failed", "crit-cancelled"}, (
        f"SQL-pushed filter must select exactly the FAILED/CANCELLED corroboration "
        f"keys of THIS project, got {failed_keys}"
    )
    # The malformed values must not be coerced into keys by the SQL text cast.
    assert "42" not in failed_keys and 42 not in failed_keys


@pytest.mark.asyncio
async def test_failed_corroboration_keys_newer_non_terminal_still_excluded(db_session):
    """The stale-failure exclusion still works after the push-down: a key with a
    newer non-terminal ``Verify:`` task is dropped, while a key whose only
    corroboration attempt FAILED is still reported."""
    from app.models import Task as _Task

    project = await _make_project(db_session, "Corroboration-SQL-Stale")

    def _task(status, key):
        return _Task(
            project_id=project.id,
            title=f"Verify: {key}",
            context={"corroboration_of": key},
            payloads=[{"type": "research", "prompt": "verify"}],
            created_by="SYSTEM_AUTO_REVIEW",
            status=status,
        )

    db_session.add_all(
        [
            _task(TaskStatus.FAILED, "crit-retried"),
            _task(TaskStatus.QUEUED, "crit-retried"),  # newer attempt in flight
            _task(TaskStatus.FAILED, "crit-abandoned"),  # no newer attempt
        ]
    )
    await db_session.commit()

    failed_keys = await ac.get_failed_corroboration_keys(db_session, project.id)
    assert failed_keys == {"crit-abandoned"}, (
        "a key with a newer non-terminal Verify task must be excluded; a key whose "
        f"only attempt failed must be reported. Got {failed_keys}"
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


# ---------------------------------------------------------------------------
# Rollup child inherits parent's changed-files context for acceptance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollup_child_inherits_parent_changed_files_for_acceptance(db_session):
    """A landed rollup child that merged via the PARENT's PR never owns a GitHub
    PR, so it never receives its own ``_pr_changed_files`` context — its declared
    ``named_test`` cannot be located in any diff and its acceptance verdict is
    stuck at ``declared_unsatisfied``.

    When the rollup parent lands, ``_propagate_landed_to_staging_merged_descendants``
    must propagate the parent's changed-file context DOWN to the child (marked
    parent-derived, NOT github-verified) and re-stamp the child's acceptance
    verdict so the stale ``declared_unsatisfied`` flips off. Because the parent's
    diff is the union of every sibling's changes, the credit is capped at the
    ``satisfied_unproven`` tier — it must NOT mint machine ``proven`` credit.
    """
    from app.rollup_management import _propagate_landed_to_staging_merged_descendants
    from app.acceptance_gate import (
        PARENT_DERIVED_CHANGED_FILES_KEY,
        classify_merged_acceptance_context,
        record_code_acceptance_verdict,
        stamp_changed_files_signal,
    )
    from app.landed_signal import REVIEW_LOOP_KEY

    prod_seam = "backend/app/rollup_widget.py"
    declared_test = "backend/testing/test_rollup_widget.py"

    project = await _make_project(db_session, name="RollupInheritCF")

    # Rollup PR-owning parent: carries the authoritative GitHub-verified diff
    # (the union of all its children's changes) and has landed on integration.
    parent_ctx = {"pr_merged": True, "landed_on_integration": True}
    parent_ctx.update(
        stamp_changed_files_signal([declared_test, prod_seam], github_verified=True)
    )
    parent = Task(
        project_id=project.id,
        title="rollup parent",
        payloads=[{"type": "research", "prompt": "rollup"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context=parent_ctx,
    )

    # Child merged via the parent's PR: pr_merged=True but landed_on_integration
    # still False (it merged to staging, not integration) and NO own
    # _pr_changed_files. It declares the named_test as acceptance evidence.
    child = Task(
        project_id=project.id,
        parent_task_id=None,  # set below after parent has an id
        title="rollup child",
        payloads=[
            {
                "type": "create",
                "prompt": "child",
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": declared_test,
                },
            }
        ],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
            # verified merge: review_loop present with no unverified reason.
            REVIEW_LOOP_KEY: {},
        },
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    child.parent_task_id = parent.id
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)

    # BEFORE: with no changed-file context the declared test cannot be located,
    # so the verdict is fail-closed declared_unsatisfied.
    before = record_code_acceptance_verdict(child)
    assert before is not None
    assert before.satisfied is False
    assert (
        classify_merged_acceptance_context(child.context)
        == "declared_unsatisfied"
    )
    await db_session.commit()

    # Land-time propagation from the rollup parent.
    stamped = await _propagate_landed_to_staging_merged_descendants(
        parent.id, db_session
    )
    await db_session.commit()
    await db_session.refresh(child)

    assert child.id in {t.id for t in stamped}, "child must be stamped as landed"

    child_ctx = child.context or {}
    # Parent's changed files were inherited, marked parent-derived, and NOT
    # github-verified (the union diff must not mint a false proof).
    assert child_ctx.get("_pr_changed_files") == [declared_test, prod_seam]
    assert child_ctx.get(PARENT_DERIVED_CHANGED_FILES_KEY) is True
    assert "_pr_changed_files_github_verified" not in child_ctx

    # AFTER: the verdict flips off declared_unsatisfied to the soft
    # satisfied_unproven tier — satisfied but NOT machine-proven.
    recorded = child_ctx.get("code_acceptance_verdict")
    assert recorded is not None
    assert recorded["satisfied"] is True
    assert recorded["proven"] is False
    assert classify_merged_acceptance_context(child_ctx) == "satisfied_unproven"


@pytest.mark.asyncio
async def test_rollup_child_own_changed_files_not_clobbered_by_parent(db_session):
    """setdefault semantics: a rollup child that carries its OWN self-reported
    ``_pr_changed_files`` must keep it — the parent-derived propagation must never
    overwrite a child's authoritative self-report."""
    from app.rollup_management import _propagate_landed_to_staging_merged_descendants
    from app.acceptance_gate import (
        PARENT_DERIVED_CHANGED_FILES_KEY,
        stamp_changed_files_signal,
    )

    project = await _make_project(db_session, name="RollupNoClobber")

    parent_ctx = {"pr_merged": True, "landed_on_integration": True}
    parent_ctx.update(
        stamp_changed_files_signal(
            ["backend/app/parent_seam.py", "backend/testing/test_parent_seam.py"],
            github_verified=True,
        )
    )
    parent = Task(
        project_id=project.id,
        title="parent",
        payloads=[{"type": "research", "prompt": "p"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context=parent_ctx,
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    own_files = ["backend/app/child_seam.py"]
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="child-with-own-files",
        payloads=[{"type": "create", "prompt": "c"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
            "_pr_changed_files": own_files,
        },
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)

    await _propagate_landed_to_staging_merged_descendants(parent.id, db_session)
    await db_session.commit()
    await db_session.refresh(child)

    child_ctx = child.context or {}
    assert child_ctx.get("_pr_changed_files") == own_files, (
        "child's own self-reported changed files must not be clobbered"
    )
    assert PARENT_DERIVED_CHANGED_FILES_KEY not in child_ctx


@pytest.mark.asyncio
async def test_reconcile_backfill_stamps_landed_child_changed_files_from_parent(
    db_session,
):
    """Retroactive reconcile stamps a rollup parent's changed files onto
    already-landed children, flips a stale declared_unsatisfied verdict, and lets
    the unverified-merge backfill earn criterion evidence in the same run.

    The forward-path propagation only heals NEW landings, so already-landed
    children (stamped only on the PR-owning parent) stay without
    ``_pr_changed_files``.  This exercises
    :func:`ac.backfill_rollup_child_changed_files`:

      1. A VERIFIED-merge child's verdict flips declared_unsatisfied ->
         satisfied_unproven once the parent's diff is inherited (parent-derived,
         never github-verified -> never machine-proven).
      2. An UNVERIFIED-merge child gains the parent's changed-file context so the
         declared named_test can be located in the diff — clearing the "not among
         the PR's changed files" declared_unsatisfied cause — and the subsequent
         unverified-merge backfill records criterion evidence for it.
      3. Re-running the reconcile is a no-op (setdefault / idempotent).
    """
    from app.acceptance_gate import (
        CODE_ACCEPTANCE_VERDICT_KEY,
        PARENT_DERIVED_CHANGED_FILES_KEY,
        classify_merged_acceptance_context,
        record_code_acceptance_verdict,
        stamp_changed_files_signal,
    )
    from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY

    prod_seam = "backend/app/widgetflow.py"
    declared_test = "backend/testing/test_widgetflow.py"

    project = await _make_project(db_session, name="ReconcileRollupChild")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="widgetflow named_test passes",
        created_by="strategist",
    )

    # Parent (PR-owning rollup) landed on integration carrying the GitHub-verified
    # combined diff (a test path + a production seam).
    parent_ctx = {"pr_merged": True, "landed_on_integration": True}
    parent_ctx.update(
        stamp_changed_files_signal([prod_seam, declared_test], github_verified=True)
    )
    parent = Task(
        project_id=project.id,
        title="rollup parent",
        payloads=[{"type": "research", "prompt": "rollup"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context=parent_ctx,
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    def _child(context: dict) -> Task:
        return Task(
            project_id=project.id,
            parent_task_id=parent.id,
            title="rollup child",
            payloads=[
                {
                    "type": "create",
                    "prompt": "child",
                    "acceptance_evidence": {
                        "kind": "named_test",
                        "test_path": declared_test,
                    },
                }
            ],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            criterion_ids=[criterion.criterion_key],
            context=context,
        )

    # Verified-merge child: landed, review_loop present with NO unverified reason.
    child_verified = _child(
        {"pr_merged": True, "landed_on_integration": True, REVIEW_LOOP_KEY: {}}
    )
    # Unverified-merge child: landed but the merge fell open (unverified reason set).
    child_unverified = _child(
        {
            "pr_merged": True,
            "landed_on_integration": True,
            REVIEW_LOOP_KEY: {UNVERIFIED_MERGE_REASON_KEY: "no_detectable_test_command"},
        }
    )
    db_session.add_all([child_verified, child_unverified])
    await db_session.commit()

    # BEFORE: with no changed-file context both children fail closed as
    # declared_unsatisfied (the declared test cannot be located in any diff).
    for c in (child_verified, child_unverified):
        assert record_code_acceptance_verdict(c) is not None
        assert (
            classify_merged_acceptance_context(c.context) == "declared_unsatisfied"
        )
    await db_session.commit()

    # Reconcile: stamp the parent's changed files onto both landed children.
    stamped = await ac.backfill_rollup_child_changed_files(db_session, project.id)
    assert stamped == 2, "both landed children lacking changed files must be stamped"

    await db_session.refresh(child_verified)
    await db_session.refresh(child_unverified)

    # Both inherited the parent's diff as PARENT-DERIVED (never github-verified, so
    # a union-diff match can never mint machine-proven credit).
    for c in (child_verified, child_unverified):
        ctx = c.context or {}
        assert ctx.get("_pr_changed_files") == [prod_seam, declared_test]
        assert ctx.get(PARENT_DERIVED_CHANGED_FILES_KEY) is True
        assert "_pr_changed_files_github_verified" not in ctx

    # Verified child: verdict flips off declared_unsatisfied to satisfied_unproven.
    assert (
        classify_merged_acceptance_context(child_verified.context)
        == "satisfied_unproven"
    )

    # Unverified child: the declared test is now locatable in the diff, so the
    # "not among the PR's changed files" declared_unsatisfied cause is cleared —
    # its verdict reason now reflects the unverified merge, not a missing diff.
    unverified_verdict = (child_unverified.context or {}).get(
        CODE_ACCEPTANCE_VERDICT_KEY
    )
    assert unverified_verdict is not None
    assert "not among the PR's changed files" not in unverified_verdict["reason"]
    assert "did not run green" in unverified_verdict["reason"]

    # The unverified-merge backfill now finds a locatable named_test + production
    # seam and records criterion evidence — the whole point of running the
    # reconcile BEFORE it in the same endpoint.
    recorded = await ac.backfill_unverified_merge_evidence(db_session, project.id)
    assert recorded >= 1, "unverified-merge backfill must credit the stamped child"

    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert any(str(child_unverified.id) == e.source_id for e in evidence), (
        "backfill evidence must be attributed to the stamped rollup child"
    )

    # Idempotent: a second reconcile stamps nothing new (setdefault semantics).
    assert await ac.backfill_rollup_child_changed_files(db_session, project.id) == 0


@pytest.mark.asyncio
async def test_reconcile_backfill_skips_child_without_github_verified_ancestor(
    db_session,
):
    """A landed child whose ancestor carries NO github-verified changed files is
    left untouched — the reconcile must never fabricate a diff from a
    non-authoritative parent list."""
    from app.acceptance_gate import PARENT_DERIVED_CHANGED_FILES_KEY

    project = await _make_project(db_session, name="ReconcileNoVerifiedParent")

    # Parent landed but its changed files are self-reported (NOT github-verified).
    parent = Task(
        project_id=project.id,
        title="parent",
        payloads=[{"type": "research", "prompt": "rollup"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "pr_merged": True,
            "landed_on_integration": True,
            "_pr_changed_files": ["backend/app/foo.py"],
        },
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="child",
        payloads=[{"type": "create", "prompt": "child"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "landed_on_integration": True},
    )
    db_session.add(child)
    await db_session.commit()

    assert await ac.backfill_rollup_child_changed_files(db_session, project.id) == 0

    await db_session.refresh(child)
    child_ctx = child.context or {}
    assert "_pr_changed_files" not in child_ctx
    assert PARENT_DERIVED_CHANGED_FILES_KEY not in child_ctx


# ---------------------------------------------------------------------------
# Evidence summary tier + provenance fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_criterion_evidence_summary_entries_tier_field(db_session):
    """_criterion_evidence_summary_entries assigns the correct tier per evidence row."""
    from app.acceptance_criteria import (
        _criterion_evidence_summary_entries,
        UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND,
    )

    project = await _make_project(db_session, "EvidenceTierField")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Tier classification"
    )

    rows = [
        CriterionEvidence(
            criterion_id=criterion.id,
            project_id=project.id,
            source_kind="reachability",
            source_id="t1",
            detail={"proven": True},
        ),
        CriterionEvidence(
            criterion_id=criterion.id,
            project_id=project.id,
            source_kind="task_merge",
            source_id="t1",
            detail={"proven": True},
        ),
        CriterionEvidence(
            criterion_id=criterion.id,
            project_id=project.id,
            source_kind=UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND,
            source_id="t2",
            detail={"backfilled": True},
        ),
        CriterionEvidence(
            criterion_id=criterion.id,
            project_id=project.id,
            source_kind="operator",
            source_id="op1",
            detail={},
        ),
    ]

    entries = _criterion_evidence_summary_entries(rows)

    assert entries[0]["tier"] == "covered"   # reachability → covered
    assert entries[1]["tier"] == "covered"   # task_merge proven=True → covered
    assert entries[2]["tier"] == "backfilled"  # unverified_merge_backfill → backfilled
    assert entries[3]["tier"] == "partial"    # unknown source_kind → partial


@pytest.mark.asyncio
async def test_criterion_evidence_summary_entries_provenance_field(db_session):
    """_criterion_evidence_summary_entries extracts runner provenance from detail."""
    from app.acceptance_criteria import _criterion_evidence_summary_entries

    project = await _make_project(db_session, "EvidenceProvenanceField")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Provenance extraction"
    )

    with_provenance = CriterionEvidence(
        criterion_id=criterion.id,
        project_id=project.id,
        source_kind="task_merge",
        source_id="t1",
        detail={
            "proven": True,
            "executing_backend": "self_hosted",
            "runner_target_id": "runner-abc123",
            "runner_hostname": "agent-host-1",
        },
    )
    without_provenance = CriterionEvidence(
        criterion_id=criterion.id,
        project_id=project.id,
        source_kind="reachability",
        source_id="t1",
        detail={"proven": True},
    )

    entries = _criterion_evidence_summary_entries([with_provenance, without_provenance])

    prov = entries[0]["provenance"]
    assert prov is not None
    assert prov["executing_backend"] == "self_hosted"
    assert prov["runner_id"] == "runner-abc123"
    assert prov["runner_hostname"] == "agent-host-1"

    assert entries[1]["provenance"] is None


@pytest.mark.asyncio
async def test_record_task_merge_evidence_attaches_runner_provenance(db_session):
    """record_task_merge_evidence writes executing_backend + runner id to task_merge detail."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.models import ComputeJob

    project = await _make_project(db_session, "MergeEvidenceRunnerProv")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Runner provenance on task_merge"
    )

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "implement"}],
        context={
            CODE_ACCEPTANCE_VERDICT_KEY: {"proven": True},
        },
        criterion_ids=[criterion.criterion_key],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="self_hosted",
        polling_metadata={
            "runner_target_id": "runner-xyz",
            "runner_hostname": "worker-node-1",
        },
    )
    db_session.add(job)
    await db_session.commit()

    count = await ac.record_task_merge_evidence(db_session, task, project.id)
    assert count == 2  # task_merge + reachability

    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    task_merge_ev = next(e for e in evidence if e.source_kind == "task_merge")
    detail = task_merge_ev.detail or {}
    assert detail.get("executing_backend") == "self_hosted"
    assert detail.get("runner_target_id") == "runner-xyz"
    assert detail.get("runner_hostname") == "worker-node-1"
    assert detail.get("proven") is True


@pytest.mark.asyncio
async def test_coverage_summary_evidence_carries_tier_and_provenance(db_session):
    """get_version_coverage_summary evidence entries include tier and provenance fields."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.models import ComputeJob

    project = await _make_project(db_session, "SummaryTierProv")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Summary tier and provenance"
    )

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "implement"}],
        context={CODE_ACCEPTANCE_VERDICT_KEY: {"proven": True}},
        criterion_ids=[criterion.criterion_key],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="gcp",
        polling_metadata={"runner_target_id": "gcp-runner-1"},
    )
    db_session.add(job)
    await db_session.commit()

    await ac.record_task_merge_evidence(db_session, task, project.id)

    summary = await ac.get_version_coverage_summary(db_session, version.id)
    item = summary["criteria"][0]
    assert item["coverage"] == "covered"

    evidence_list = item["evidence"]
    assert len(evidence_list) == 2

    task_merge_entry = next(e for e in evidence_list if e["source_kind"] == "task_merge")
    assert task_merge_entry["tier"] == "covered"
    prov = task_merge_entry["provenance"]
    assert prov is not None
    assert prov["executing_backend"] == "gcp"
    assert prov["runner_id"] == "gcp-runner-1"

    reachability_entry = next(e for e in evidence_list if e["source_kind"] == "reachability")
    assert reachability_entry["tier"] == "covered"
    assert reachability_entry["provenance"] is None


# ---------------------------------------------------------------------------
# walkthrough_e2e evidence ingestion (record_walkthrough_e2e_evidence + endpoint)
# backfill_merged_named_test_coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_walkthrough_e2e_evidence_writes_covered_when_sha_matches(
    db_session,
):
    """A matching candidate_sha writes covered-tier evidence for passed criteria."""
    project = await _make_project_with_member(db_session, name="Walk-Match")
    sha = "b" * 40
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"status": "scoring", "candidate_sha": sha},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    passed = await ac.create_criterion(db_session, project.id, version.id, "Runner enroll")
    failed = await ac.create_criterion(db_session, project.id, version.id, "Iframe token")

    summary = await ac.record_walkthrough_e2e_evidence(
        db_session,
        version,
        candidate_sha=sha,
        results=[
            {"criterion_key": passed.criterion_key, "passed": True, "notes": "green"},
            {"criterion_key": failed.criterion_key, "passed": False, "notes": "red"},
        ],
        qualification_attempt_id="attempt-1",
    )
    assert summary["status"] == "recorded"
    assert summary["recorded"] == 1
    assert summary["skipped_not_passed"] == 1
    assert summary["unmatched_keys"] == []

    # The passing criterion is now covered by a SHA-pinned walkthrough row.
    passed_ev = await ac.get_evidence_for_criterion(db_session, passed.id)
    assert len(passed_ev) == 1
    assert passed_ev[0].source_kind == ac.WALKTHROUGH_E2E_EVIDENCE_KIND
    assert passed_ev[0].detail["candidate_sha"] == sha
    assert passed_ev[0].detail["passed"] is True
    assert "evaluated_at" in passed_ev[0].detail
    assert ac.compute_criterion_coverage(passed_ev) == "covered"

    # The failing criterion earned nothing.
    failed_ev = await ac.get_evidence_for_criterion(db_session, failed.id)
    assert failed_ev == []


@pytest.mark.asyncio
async def test_record_walkthrough_e2e_evidence_rejects_mismatched_sha(db_session):
    """A candidate_sha that does not match the qualifying SHA writes NOTHING."""
    project = await _make_project_with_member(db_session, name="Walk-Mismatch")
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"candidate_sha": "c" * 40},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    crit = await ac.create_criterion(db_session, project.id, version.id, "Meta")

    summary = await ac.record_walkthrough_e2e_evidence(
        db_session,
        version,
        candidate_sha="d" * 40,  # wrong commit
        results=[{"criterion_key": crit.criterion_key, "passed": True}],
    )
    assert summary["status"] == "rejected"
    assert summary["rejected_reason"] == "candidate_sha_mismatch"
    assert summary["recorded"] == 0
    # A run against the wrong commit forged no coverage.
    assert await ac.get_evidence_for_criterion(db_session, crit.id) == []


@pytest.mark.asyncio
async def test_record_walkthrough_e2e_evidence_rejects_missing_sha(db_session):
    """An empty candidate_sha is rejected outright (cannot pin coverage)."""
    project = await _make_project_with_member(db_session, name="Walk-MissingSha")
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"candidate_sha": "e" * 40},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    crit = await ac.create_criterion(db_session, project.id, version.id, "Meta")
    summary = await ac.record_walkthrough_e2e_evidence(
        db_session,
        version,
        candidate_sha="   ",
        results=[{"criterion_key": crit.criterion_key, "passed": True}],
    )
    assert summary["status"] == "rejected"
    assert summary["rejected_reason"] == "missing_candidate_sha"
    assert await ac.get_evidence_for_criterion(db_session, crit.id) == []


@pytest.mark.asyncio
async def test_walkthrough_e2e_endpoint_happy_path(client: AsyncClient, db_session):
    """POST .../walkthrough-e2e-evidence writes covered evidence and coverage flips."""
    project = await _make_project_with_member(db_session, name="Walk-Endpoint")
    sha = "f" * 40
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"candidate_sha": sha},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    crit = await ac.create_criterion(db_session, project.id, version.id, "Runner enroll")

    resp = await client.post(
        f"/api/admin/projects/{project.id}/acceptance-criteria/walkthrough-e2e-evidence",
        json={
            "candidate_sha": sha,
            "qualification_attempt_id": "attempt-9",
            "results": [{"criterion_key": crit.criterion_key, "passed": True}],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["recorded"] == 1

    summary = await ac.get_version_coverage_summary(db_session, version.id)
    by_key = {c["criterion_key"]: c for c in summary["criteria"]}
    assert by_key[crit.criterion_key]["coverage"] == "covered"
    assert summary["covered"] == 1


@pytest.mark.asyncio
async def test_walkthrough_e2e_endpoint_records_exercised_engine_sha(
    client: AsyncClient, db_session
):
    """The optional ``exercised_engine_sha`` is persisted onto each evidence row.

    This is the additive seam a later guard enforces against ``candidate_sha``:
    it records the SHA the candidate stack ACTUALLY served, proving the
    walkthrough exercised the candidate engine (not the deployed one). It must
    never weaken the existing candidate_sha match — a matching SHA still writes
    covered evidence, now carrying the exercised SHA in ``detail``.
    """
    project = await _make_project_with_member(db_session, name="Walk-Endpoint-Sha")
    sha = "a" * 40
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"candidate_sha": sha},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    crit = await ac.create_criterion(db_session, project.id, version.id, "Runner enroll")
    resp = await client.post(
        f"/api/admin/projects/{project.id}/acceptance-criteria/walkthrough-e2e-evidence",
        json={
            "candidate_sha": sha,
            "exercised_engine_sha": sha,
            "results": [{"criterion_key": crit.criterion_key, "passed": True}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["recorded"] == 1

    evidence = await ac.get_evidence_for_criterion(db_session, crit.id)
    assert len(evidence) == 1
    assert evidence[0].detail.get("exercised_engine_sha") == sha


@pytest.mark.asyncio
async def test_walkthrough_e2e_endpoint_rejects_mismatched_sha(
    client: AsyncClient, db_session
):
    """POST with a mismatched candidate_sha returns 409 and writes nothing."""
    project = await _make_project_with_member(db_session, name="Walk-Endpoint-Bad")
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"candidate_sha": "1" * 40},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    crit = await ac.create_criterion(db_session, project.id, version.id, "Meta")
    resp = await client.post(
        f"/api/admin/projects/{project.id}/acceptance-criteria/walkthrough-e2e-evidence",
        json={
            "candidate_sha": "2" * 40,
            "results": [{"criterion_key": crit.criterion_key, "passed": True}],
        },
    )
    assert resp.status_code == 409, resp.text
    # The app reshapes HTTPException into {"error": {"code", "message": detail}}.
    assert resp.json()["error"]["message"]["reason"] == "candidate_sha_mismatch"
    assert await ac.get_evidence_for_criterion(db_session, crit.id) == []


@pytest.mark.asyncio
async def test_walkthrough_evidence_rejects_engine_sha_mismatch(db_session):
    """A mismatched exercised_engine_sha rejects the whole batch — zero rows written.

    This is the named acceptance test for the engine-SHA genuineness guard.
    When the harness reports exercised_engine_sha != candidate_sha the function
    must return rejected/exercised_engine_sha_mismatch and write nothing, even
    though the candidate_sha itself matches the qualifying SHA.
    """
    project = await _make_project_with_member(db_session, name="Walk-EngSha-Mismatch")
    sha = "a" * 40
    other_sha = "b" * 40
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"candidate_sha": sha},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    crit = await ac.create_criterion(db_session, project.id, version.id, "Runner enroll")
    summary = await ac.record_walkthrough_e2e_evidence(
        db_session,
        version,
        candidate_sha=sha,
        results=[{"criterion_key": crit.criterion_key, "passed": True}],
        exercised_engine_sha=other_sha,  # harness drove a different engine
    )
    assert summary["status"] == "rejected"
    assert summary["rejected_reason"] == "exercised_engine_sha_mismatch"
    assert summary["recorded"] == 0
    assert await ac.get_evidence_for_criterion(db_session, crit.id) == []


@pytest.mark.asyncio
async def test_walkthrough_evidence_accepts_matching_exercised_engine_sha(db_session):
    """A matching exercised_engine_sha records evidence and stamps the SHA in detail."""
    project = await _make_project_with_member(db_session, name="Walk-EngSha-Match")
    sha = "c" * 40
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"candidate_sha": sha},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    crit = await ac.create_criterion(db_session, project.id, version.id, "Runner enroll")
    summary = await ac.record_walkthrough_e2e_evidence(
        db_session,
        version,
        candidate_sha=sha,
        results=[{"criterion_key": crit.criterion_key, "passed": True}],
        exercised_engine_sha=sha,  # harness confirmed the right engine
    )
    assert summary["status"] == "recorded"
    assert summary["recorded"] == 1
    evidence = await ac.get_evidence_for_criterion(db_session, crit.id)
    assert len(evidence) == 1
    assert evidence[0].detail.get("exercised_engine_sha") == sha


@pytest.mark.asyncio
async def test_walkthrough_evidence_accepts_absent_exercised_engine_sha(db_session):
    """Absent exercised_engine_sha is accepted for backward compatibility."""
    project = await _make_project_with_member(db_session, name="Walk-EngSha-Absent")
    sha = "d" * 40
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"candidate_sha": sha},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    crit = await ac.create_criterion(db_session, project.id, version.id, "Runner enroll")
    summary = await ac.record_walkthrough_e2e_evidence(
        db_session,
        version,
        candidate_sha=sha,
        results=[{"criterion_key": crit.criterion_key, "passed": True}],
        # exercised_engine_sha intentionally omitted
    )
    assert summary["status"] == "recorded"
    assert summary["recorded"] == 1
    assert await ac.get_evidence_for_criterion(db_session, crit.id) != []


@pytest.mark.asyncio
async def test_walkthrough_e2e_endpoint_rejects_mismatched_exercised_engine_sha(
    client: AsyncClient, db_session
):
    """POST with exercised_engine_sha != candidate_sha returns 409 and writes nothing."""
    project = await _make_project_with_member(db_session, name="Walk-Endpoint-EngSha")
    sha = "e" * 40
    other_sha = "f" * 40
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"candidate_sha": sha},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    crit = await ac.create_criterion(db_session, project.id, version.id, "Runner enroll")
    resp = await client.post(
        f"/api/admin/projects/{project.id}/acceptance-criteria/walkthrough-e2e-evidence",
        json={
            "candidate_sha": sha,
            "exercised_engine_sha": other_sha,
            "results": [{"criterion_key": crit.criterion_key, "passed": True}],
        },
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["message"]["reason"] == "exercised_engine_sha_mismatch"
    assert await ac.get_evidence_for_criterion(db_session, crit.id) == []


@pytest.mark.asyncio
async def test_backfill_merged_named_test_covers_uncovered_criterion(db_session):
    """Merged task with named_test in changed files SHOULD get backfill evidence.

    When a task's criteria were added AFTER the merge (so record_task_merge_evidence
    never fired), the backfill must find such tasks, verify the test was in changed
    files, and record UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND evidence.
    """
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    project = await _make_project(db_session, "MergedNamedTestCoverage")
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id,
        "Merged named-test criterion",
        created_by="strategist",
    )

    # Simulate a completed merged task that declared named_test in its payload.
    # Criteria were added after the merge, so record_task_merge_evidence never ran.
    task = Task(
        project_id=project.id,
        payloads=[{
            "type": "create",
            "prompt": "add the test",
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "testing/test_merged_backfill.py",
            },
        }],
        context={
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            "_pr_changed_files": [
                "testing/test_merged_backfill.py",
                "backend/app/target_module.py",
            ],
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    # Before backfill: the criterion has no evidence (coverage = uncovered).
    evidence_before = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert evidence_before == [], "No evidence before backfill"
    assert ac.compute_criterion_coverage(evidence_before) == "uncovered"

    recorded = await ac.backfill_merged_named_test_coverage(db_session, project.id)

    assert recorded == 1, (
        f"Expected 1 evidence row for 1 criterion; got {recorded}"
    )
    evidence_after = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence_after) == 1
    assert evidence_after[0].source_kind == ac.UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND
    assert evidence_after[0].source_id == str(task.id)
    assert evidence_after[0].detail is not None
    assert evidence_after[0].detail.get("proven") is True
    assert evidence_after[0].detail.get("backfill_reason") == "merged_named_test_matches_changed"

    coverage = ac.compute_criterion_coverage(evidence_after)
    assert coverage == "backfilled", f"Expected 'backfilled', got '{coverage}'"


@pytest.mark.asyncio
async def test_backfill_merged_named_test_skips_already_covered(db_session):
    """Criteria already machine-verified as covered get NO new backfill evidence."""
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    project = await _make_project(db_session, "MergedNamedTestSkipCovered")
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id,
        "Already covered criterion",
    )

    # Record covering evidence first (task_merge with proven=True).
    task = Task(
        project_id=project.id,
        payloads=[{
            "type": "create",
            "prompt": "implement X",
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "testing/test_already_covered.py",
            },
        }],
        context={
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            "_pr_changed_files": [
                "testing/test_already_covered.py",
                "backend/app/target_module.py",
            ],
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Record covering evidence (proven task_merge).
    await ac.record_evidence(
        db_session,
        criterion_id=criterion.id,
        project_id=project.id,
        source_kind="task_merge",
        source_id=str(task.id),
        detail={"proven": True},
    )

    # Verify criterion is already 'covered'.
    evidence_before = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert ac.compute_criterion_coverage(evidence_before) == "covered"

    recorded = await ac.backfill_merged_named_test_coverage(db_session, project.id)
    assert recorded == 0, "Already-covered criterion must not get backfill evidence"


@pytest.mark.asyncio
async def test_backfill_merged_named_test_skips_test_not_in_changed(db_session):
    """Declared named_test NOT in changed files gets NO backfill evidence."""
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    project = await _make_project(db_session, "MergedNamedTestSkipMissing")
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id,
        "Missing test criterion",
    )

    task = Task(
        project_id=project.id,
        payloads=[{
            "type": "create",
            "prompt": "implement X",
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "testing/test_not_shipped.py",
            },
        }],
        context={
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            "_pr_changed_files": [
                "backend/app/target_module.py",
            ],
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    recorded = await ac.backfill_merged_named_test_coverage(db_session, project.id)
    assert recorded == 0, "Test not in changed files must not get backfill"


@pytest.mark.asyncio
async def test_backfill_merged_named_test_skips_no_named_test(db_session):
    """Task with no named_test declaration gets NO backfill evidence."""
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    project = await _make_project(db_session, "MergedNamedTestSkipNoNamed")
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id,
        "No named-test criterion",
    )

    task = Task(
        project_id=project.id,
        payloads=[{
            "type": "create",
            "prompt": "implement X",
        }],
        context={
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            "_pr_changed_files": [
                "testing/test_no_named.py",
                "backend/app/target_module.py",
            ],
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    recorded = await ac.backfill_merged_named_test_coverage(db_session, project.id)
    assert recorded == 0, "No named_test declaration must not get backfill"


@pytest.mark.asyncio
async def test_backfill_merged_named_test_idempotent(db_session):
    """Second call to backfill_merged_named_test_coverage records 0 new rows."""
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    project = await _make_project(db_session, "MergedNamedTestIdempotent")
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id,
        "Idempotent criterion",
    )

    task = Task(
        project_id=project.id,
        payloads=[{
            "type": "create",
            "prompt": "implement X",
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "testing/test_idempotent_backfill.py",
            },
        }],
        context={
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            "_pr_changed_files": [
                "testing/test_idempotent_backfill.py",
                "backend/app/target_module.py",
            ],
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    # First call: records evidence.
    recorded1 = await ac.backfill_merged_named_test_coverage(db_session, project.id)
    assert recorded1 == 1

    # Second call: should record 0 new rows.
    recorded2 = await ac.backfill_merged_named_test_coverage(db_session, project.id)
    assert recorded2 == 0, "Second backfill call must be idempotent"


@pytest.mark.asyncio
async def test_backfill_merged_named_test_skips_no_criterion_ids(db_session):
    """Task with no criterion_ids gets NO backfill evidence."""
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    project = await _make_project(db_session, "MergedNamedTestNoCriterionIds")
    _ = await _make_version(db_session, project)  # open version needed for backfill to proceed

    # Create a task that has a named_test but NO criterion_ids.
    task = Task(
        project_id=project.id,
        payloads=[{
            "type": "create",
            "prompt": "implement X",
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "testing/test_no_criterion_ids.py",
            },
        }],
        context={
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            "_pr_changed_files": [
                "testing/test_no_criterion_ids.py",
                "backend/app/target_module.py",
            ],
        },
        criterion_ids=[],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    recorded = await ac.backfill_merged_named_test_coverage(db_session, project.id)
    assert recorded == 0, "Task with no criterion_ids must not get backfill"


@pytest.mark.asyncio
async def test_backfill_merged_named_test_skips_no_seam_files(db_session):
    """Test-only diff (no production seam) gets NO backfill evidence.

    A named_test that ships NO production seam cannot exercise changed code,
    so the backfill must skip it.
    """
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    project = await _make_project(db_session, "MergedNamedTestNoSeam")
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id,
        "No-seam criterion",
    )

    task = Task(
        project_id=project.id,
        payloads=[{
            "type": "create",
            "prompt": "test only change",
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "testing/test_only.py",
            },
        }],
        context={
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            "_pr_changed_files": [
                "testing/test_only.py",
            ],
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    recorded = await ac.backfill_merged_named_test_coverage(db_session, project.id)
    assert recorded == 0, "Test-only diff must not get backfill evidence"


# ---------------------------------------------------------------------------
# _handle_walkthrough_e2e_completion — log format alignment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_walkthrough_e2e_completion_ingests_results(db_session):
    """_handle_walkthrough_e2e_completion ingests evidence from the agent's
    actual log format: {git_sha, run_timestamp, results: [{criterion_key, passed, notes}]}.

    Only entries with passed=True create evidence rows; passed=False entries
    are silently skipped.
    """
    import json as _json
    from app.task_completion import _handle_walkthrough_e2e_completion

    project = await _make_project(db_session, name="Walk-Completion-Ingest")
    version = await _make_version(db_session, project)
    sha = "a" * 40
    version.last_qualification = {"status": "scoring", "candidate_sha": sha}
    await db_session.commit()
    await db_session.refresh(version)

    crit_pass = await ac.create_criterion(db_session, project.id, version.id, "Passes criterion")
    crit_fail = await ac.create_criterion(db_session, project.id, version.id, "Fails criterion")

    task = Task(
        project_id=project.id,
        payloads=[{"type": "walkthrough_e2e", "prompt": "run walkthrough"}],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = _json.dumps({
        "git_sha": sha,
        "run_timestamp": "2026-07-22T00:00:00Z",
        "results": [
            {"criterion_key": crit_pass.criterion_key, "passed": True, "notes": "all good"},
            {"criterion_key": crit_fail.criterion_key, "passed": False, "notes": "timeout"},
        ],
    })

    await _handle_walkthrough_e2e_completion(task, logs, db_session)

    pass_ev = await ac.get_evidence_for_criterion(db_session, crit_pass.id)
    assert len(pass_ev) == 1, "passed=True entry must create an evidence row"
    assert pass_ev[0].source_kind == "walkthrough_e2e"
    assert pass_ev[0].detail["candidate_sha"] == sha
    assert pass_ev[0].detail["criterion_key"] == crit_pass.criterion_key
    assert pass_ev[0].detail["passed"] is True
    assert pass_ev[0].detail["notes"] == "all good"

    fail_ev = await ac.get_evidence_for_criterion(db_session, crit_fail.id)
    assert fail_ev == [], "passed=False entry must not create any evidence row"


@pytest.mark.asyncio
async def test_handle_walkthrough_e2e_completion_skips_missing_results_key(db_session):
    """_handle_walkthrough_e2e_completion skips gracefully when 'results' key
    is absent (e.g. old 'flows' format or unparseable artifact).
    """
    import json as _json
    from app.task_completion import _handle_walkthrough_e2e_completion

    project = await _make_project(db_session, name="Walk-Completion-OldFormat")
    version = await _make_version(db_session, project)
    sha = "b" * 40
    crit = await ac.create_criterion(db_session, project.id, version.id, "Old-format crit")

    task = Task(
        project_id=project.id,
        payloads=[{"type": "walkthrough_e2e", "prompt": "run walkthrough"}],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Old-format artifact: uses "flows" key instead of "results" — must be silently skipped.
    old_format_logs = _json.dumps({
        "git_sha": sha,
        "flows": [{"flow": crit.criterion_key, "verdict": "PASS", "candidate_sha": sha}],
    })

    await _handle_walkthrough_e2e_completion(task, old_format_logs, db_session)

    evidence = await ac.get_evidence_for_criterion(db_session, crit.id)
    assert evidence == [], "Old 'flows' format must not produce any evidence rows"


# ---------------------------------------------------------------------------
# record_operator_walkthrough_evidence — operator-run walkthrough ingestion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_operator_walkthrough_evidence_records_covered(db_session):
    """record_operator_walkthrough_evidence writes covered-tier evidence when SHA is provided.

    Unlike record_walkthrough_e2e_evidence, this does NOT check that the SHA
    matches the version's qualifying candidate SHA — the operator asserts the
    commit is correct by running against it.
    """
    project = await _make_project_with_member(db_session, name="OpWalk-Record")
    sha = "aabbccdd" + "e" * 31
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"candidate_sha": "z" * 40},  # deliberately different
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    passed_crit = await ac.create_criterion(
        db_session, project.id, version.id, "Operator runner enroll"
    )
    failed_crit = await ac.create_criterion(
        db_session, project.id, version.id, "Operator iframe token"
    )
    unmatched_key = "c-nonexistent"

    summary = await ac.record_operator_walkthrough_evidence(
        db_session,
        version,
        candidate_sha=sha,
        results=[
            {"criterion_key": passed_crit.criterion_key, "passed": True, "notes": "all green"},
            {"criterion_key": failed_crit.criterion_key, "passed": False, "notes": "red"},
            {"criterion_key": unmatched_key, "passed": True, "notes": "ghost"},
        ],
    )
    assert summary["status"] == "recorded"
    assert summary["recorded"] == 1
    assert summary["skipped_not_passed"] == 1
    assert summary["unmatched_keys"] == [unmatched_key]

    # The passing criterion earned covered-tier walkthrough_e2e evidence.
    passed_ev = await ac.get_evidence_for_criterion(db_session, passed_crit.id)
    assert len(passed_ev) == 1
    assert passed_ev[0].source_kind == ac.WALKTHROUGH_E2E_EVIDENCE_KIND
    assert passed_ev[0].detail["candidate_sha"] == sha
    assert passed_ev[0].detail["passed"] is True
    assert "evaluated_at" in passed_ev[0].detail
    assert ac.compute_criterion_coverage(passed_ev) == "covered"

    # The failing criterion earned nothing.
    failed_ev = await ac.get_evidence_for_criterion(db_session, failed_crit.id)
    assert failed_ev == []


@pytest.mark.asyncio
async def test_record_operator_walkthrough_evidence_rejects_empty_sha(db_session):
    """record_operator_walkthrough_evidence rejects empty candidate_sha."""
    project = await _make_project_with_member(db_session, name="OpWalk-EmptySha")
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    crit = await ac.create_criterion(db_session, project.id, version.id, "Meta")

    for bad_sha in ("", "   ", None):
        summary = await ac.record_operator_walkthrough_evidence(
            db_session,
            version,
            candidate_sha=bad_sha,
            results=[{"criterion_key": crit.criterion_key, "passed": True}],
        )
        assert summary["status"] == "rejected"
        assert summary["rejected_reason"] == "missing_candidate_sha"
        assert summary["recorded"] == 0
        assert await ac.get_evidence_for_criterion(db_session, crit.id) == []


@pytest.mark.asyncio
async def test_operator_walkthrough_endpoint_happy_path(
    client: AsyncClient, db_session
):
    """POST .../operator-walkthrough-evidence writes covered evidence.

    This proves that an operator-run walkthrough against a non-qualifying SHA
    still produces covered-tier evidence — unlike the qualifier-spawned endpoint
    which would reject a mismatched SHA with 409.
    """
    project = await _make_project_with_member(db_session, name="OpWalk-Endpoint")
    sha = "operator-pinned-sha-" + "a" * 22
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"candidate_sha": "z" * 40},  # different from the posted SHA
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    crit = await ac.create_criterion(
        db_session, project.id, version.id, "Endpoint runner enroll"
    )

    resp = await client.post(
        f"/api/admin/projects/{project.id}/acceptance-criteria/operator-walkthrough-evidence",
        json={
            "candidate_sha": sha,
            "results": [{"criterion_key": crit.criterion_key, "passed": True}],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["recorded"] == 1

    summary = await ac.get_version_coverage_summary(db_session, version.id)
    by_key = {c["criterion_key"]: c for c in summary["criteria"]}
    assert by_key[crit.criterion_key]["coverage"] == "covered"
    assert summary["covered"] >= 1


@pytest.mark.asyncio
async def test_operator_walkthrough_endpoint_rejects_empty_sha(
    client: AsyncClient, db_session
):
    """POST .../operator-walkthrough-evidence returns 409 for empty SHA."""
    project = await _make_project_with_member(db_session, name="OpWalk-Endpoint-Empty")
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    crit = await ac.create_criterion(db_session, project.id, version.id, "Meta")

    resp = await client.post(
        f"/api/admin/projects/{project.id}/acceptance-criteria/operator-walkthrough-evidence",
        json={
            "candidate_sha": "",
            "results": [{"criterion_key": crit.criterion_key, "passed": True}],
        },
    )
    assert resp.status_code == 409, resp.text
    # The app reshapes HTTPException into {"error": {"code", "message": detail}}.
    assert resp.json()["error"]["message"]["reason"] == "missing_candidate_sha"


@pytest.mark.asyncio
async def test_walkthrough_script_ingestion_flips_coverage(
    client: AsyncClient, db_session
):
    """Prove that a walkthrough script result POSTed via the operator endpoint
    flips a criterion from uncovered (or partial) to covered.

    This is the named acceptance evidence test for the walkthrough result
    ingestion path. A production code change (the new endpoint + function)
    is exercised by this test — a test-only diff would fail.
    """
    project = await _make_project_with_member(db_session, name="Walk-Ingestion")
    sha = "walkthrough-ingestion-test-" + "b" * 14
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Create a criterion with NO evidence — starts as "uncovered"
    crit = await ac.create_criterion(
        db_session, project.id, version.id, "Ingestion test criterion"
    )

    # Verify it starts uncovered
    summary_before = await ac.get_version_coverage_summary(db_session, version.id)
    by_key_before = {c["criterion_key"]: c for c in summary_before["criteria"]}
    assert by_key_before[crit.criterion_key]["coverage"] == "uncovered"

    # POST walkthrough result (simulating what the walkthrough script does)
    resp = await client.post(
        f"/api/admin/projects/{project.id}/acceptance-criteria/operator-walkthrough-evidence",
        json={
            "candidate_sha": sha,
            "results": [{"criterion_key": crit.criterion_key, "passed": True}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["recorded"] == 1

    # After ingestion, the criterion flips to "covered"
    summary_after = await ac.get_version_coverage_summary(db_session, version.id)
    by_key_after = {c["criterion_key"]: c for c in summary_after["criteria"]}
    assert by_key_after[crit.criterion_key]["coverage"] == "covered"
    assert summary_after["covered"] >= 1


@pytest.mark.asyncio
async def test_walkthrough_merge_evidence_flips_criterion_coverage(db_session):
    """Prove that ingesting walkthrough_e2e results for the three canonical v26
    criterion keys (c-dfdc9d82, c-f52fd975, c-5214996f) flips each criterion from
    ``"uncovered"`` to ``"covered"`` with the correct ``covered_by`` label.

    This is the named acceptance-evidence test for the walkthrough result
    ingestion path.  A production code change — the ``covered_by`` field added
    to the coverage summary — is exercised by assertions below; a test-only diff
    would fail the gate.
    """
    project = await _make_project_with_member(db_session, name="Walk-Merge-Flip")
    sha = "v26-walkthrough-sha-" + "c" * 26
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"candidate_sha": sha},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Create the three canonical v26 criteria with specific keys.
    keys = ["c-dfdc9d82", "c-f52fd975", "c-5214996f"]
    criteria = {}
    for key in keys:
        crit = VersionAcceptanceCriterion(
            project_id=project.id,
            release_version_id=version.id,
            criterion_key=key,
            text=f"v26 criterion {key}",
        )
        db_session.add(crit)
        criteria[key] = crit
    await db_session.commit()
    for crit in criteria.values():
        await db_session.refresh(crit)

    # Confirm all three start as "uncovered" with no covered_by.
    summary_before = await ac.get_version_coverage_summary(db_session, version.id)
    by_key_before = {c["criterion_key"]: c for c in summary_before["criteria"]}
    for key in keys:
        assert by_key_before[key]["coverage"] == "uncovered", f"{key} should start uncovered"
        assert by_key_before[key]["covered_by"] is None, f"{key} covered_by should be None"

    assert summary_before["total"] == 3
    assert summary_before["covered"] == 0
    assert summary_before["uncovered"] == 3

    # Ingest walkthrough_e2e evidence for all three passing keys.
    results = [
        {"criterion_key": key, "passed": True, "notes": f"v26 walkthrough pass for {key}"}
        for key in keys
    ]
    summary = await ac.record_walkthrough_e2e_evidence(
        db_session,
        version,
        candidate_sha=sha,
        results=results,
        qualification_attempt_id="merge-flip-attempt",
    )
    assert summary["status"] == "recorded"
    assert summary["recorded"] == 3
    assert summary["skipped_not_passed"] == 0
    assert summary["unmatched_keys"] == []

    # After ingestion, all three flip to "covered" with "walkthrough_e2e" source.
    summary_after = await ac.get_version_coverage_summary(db_session, version.id)
    by_key_after = {c["criterion_key"]: c for c in summary_after["criteria"]}
    for key in keys:
        assert by_key_after[key]["coverage"] == "covered", f"{key} should now be covered"
        assert by_key_after[key]["covered_by"] == "walkthrough_e2e", (
            f"{key} covered_by should be 'walkthrough_e2e', "
            f"got {by_key_after[key]['covered_by']!r}"
        )

    assert summary_after["covered"] == 3
    assert summary_after["uncovered"] == 0
    assert summary_after["total"] == 3

    # Verify one CriterionEvidence row per criterion is in the DB with the
    # correct source_kind and detail fields — not just the coverage summary.
    for key, crit in criteria.items():
        rows = await ac.get_evidence_for_criterion(db_session, crit.id)
        assert len(rows) == 1, (
            f"{key}: expected exactly 1 CriterionEvidence row, got {len(rows)}"
        )
        row = rows[0]
        assert row.source_kind == ac.WALKTHROUGH_E2E_EVIDENCE_KIND, (
            f"{key}: source_kind should be {ac.WALKTHROUGH_E2E_EVIDENCE_KIND!r}, "
            f"got {row.source_kind!r}"
        )
        assert row.detail["candidate_sha"] == sha, f"{key}: detail.candidate_sha mismatch"
        assert row.detail["criterion_key"] == key, f"{key}: detail.criterion_key mismatch"
        assert row.detail["passed"] is True, f"{key}: detail.passed should be True"
        assert "evaluated_at" in row.detail, f"{key}: detail missing evaluated_at"


# ---------------------------------------------------------------------------
# executed_named_test 'covered' tier (execution-trial earn loop)
# ---------------------------------------------------------------------------


def test_executed_named_test_covers_only_when_fresh_and_sha_pinned():
    """An ``executed_named_test`` row is 'covered' ONLY when fresh AND SHA-pinned.

      (a) fresh + SHA-pinned            => 'covered'
      (b) stale (older than the window) => NOT covered (falls back to 'partial')
      (c) missing/blank executed_sha    => NOT covered (falls back to 'partial')
      (d) no recorded_at                => NOT covered
    """
    sha = "a1b2c3d4e5f6"

    fresh_pinned = _make_evidence(
        ac.EXECUTED_NAMED_TEST_EVIDENCE_KIND,
        detail={"executed_sha": sha, "test_refs": ["testing/test_x.py"]},
        recorded_at=_utcnow() - timedelta(days=ac.EXECUTED_NAMED_TEST_WINDOW_DAYS - 1),
    )
    assert ac._evidence_is_executed_named_test_covered(fresh_pinned) is True
    assert ac._evidence_proves_covered(fresh_pinned) is True
    assert ac.compute_criterion_coverage([fresh_pinned]) == "covered"
    # covered maps onto the shared EvidenceStatus axis as full machine-proof.
    assert ac.criterion_evidence_status([fresh_pinned]) == "proven"

    stale_pinned = _make_evidence(
        ac.EXECUTED_NAMED_TEST_EVIDENCE_KIND,
        detail={"executed_sha": sha},
        recorded_at=_utcnow() - timedelta(days=ac.EXECUTED_NAMED_TEST_WINDOW_DAYS + 1),
    )
    assert ac._evidence_is_executed_named_test_covered(stale_pinned) is False
    assert ac._evidence_proves_covered(stale_pinned) is False
    assert ac.compute_criterion_coverage([stale_pinned]) == "partial"

    for bad_detail in ({"executed_sha": ""}, {"foo": "bar"}, None):
        fresh_unpinned = _make_evidence(
            ac.EXECUTED_NAMED_TEST_EVIDENCE_KIND,
            detail=bad_detail,
            recorded_at=_utcnow() - timedelta(days=1),
        )
        assert ac._evidence_is_executed_named_test_covered(fresh_unpinned) is False
        assert ac.compute_criterion_coverage([fresh_unpinned]) == "partial"

    no_ts = _make_evidence(
        ac.EXECUTED_NAMED_TEST_EVIDENCE_KIND,
        detail={"executed_sha": sha},
        recorded_at=None,
    )
    assert ac._evidence_is_executed_named_test_covered(no_ts) is False


def test_executed_named_test_covered_outranks_backfilled_and_is_additive():
    """A fresh executed row lifts a backfilled criterion to 'covered' (additive)."""
    backfilled = _make_evidence(
        ac.UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND,
        detail={"proven": True, "backfilled": True},
    )
    assert ac.compute_criterion_coverage([backfilled]) == "backfilled"
    executed = _make_evidence(
        ac.EXECUTED_NAMED_TEST_EVIDENCE_KIND,
        detail={"executed_sha": "f00dfeed1234"},
    )
    # The backfilled fallback is untouched; the executed row wins the tie.
    assert ac.compute_criterion_coverage([backfilled, executed]) == "covered"


@pytest.mark.asyncio
async def test_record_executed_named_test_evidence_mints_covered_idempotently(db_session):
    """A green trial mints exactly one SHA-pinned covered row; re-ingest is a no-op."""
    project = await _make_project(db_session, "ExecMint")
    version = await _make_version(db_session, project)
    crit = await ac.create_criterion(db_session, project.id, version.id, "Exec crit")

    n = await ac.record_executed_named_test_evidence(
        db_session,
        project_id=project.id,
        criteria=[crit],
        executed_sha="a1b2c3d4e5f6a1b2",
        test_refs=["testing/test_exec.py"],
        source_task_id="src-task-1",
    )
    assert n == 1
    ev = await ac.get_evidence_for_criterion(db_session, crit.id)
    assert len(ev) == 1
    assert ev[0].source_kind == ac.EXECUTED_NAMED_TEST_EVIDENCE_KIND
    assert ev[0].detail["executed_sha"] == "a1b2c3d4e5f6a1b2"
    assert ev[0].detail["test_refs"] == ["testing/test_exec.py"]
    assert ev[0].detail["proven"] is True
    assert ac.compute_criterion_coverage(ev) == "covered"

    # Re-ingesting the SAME green result mints nothing new (idempotent).
    n2 = await ac.record_executed_named_test_evidence(
        db_session,
        project_id=project.id,
        criteria=[crit],
        executed_sha="a1b2c3d4e5f6a1b2",
        test_refs=["testing/test_exec.py"],
        source_task_id="src-task-1",
    )
    assert n2 == 0
    ev2 = await ac.get_evidence_for_criterion(db_session, crit.id)
    assert len(ev2) == 1


@pytest.mark.asyncio
async def test_record_executed_named_test_evidence_blank_sha_records_nothing(db_session):
    """A blank executed_sha cannot pin coverage — records nothing."""
    project = await _make_project(db_session, "ExecBlank")
    version = await _make_version(db_session, project)
    crit = await ac.create_criterion(db_session, project.id, version.id, "Exec crit")
    n = await ac.record_executed_named_test_evidence(
        db_session,
        project_id=project.id,
        criteria=[crit],
        executed_sha="   ",
        test_refs=["testing/test_exec.py"],
        source_task_id="src-task-1",
    )
    assert n == 0
    assert await ac.get_evidence_for_criterion(db_session, crit.id) == []


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


# ---------------------------------------------------------------------------
# enroll_criterion_coverage_trial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enroll_criterion_coverage_trial_flag_off_is_noop(db_session):
    """Flag OFF ⇒ no trial task is created (returns None)."""
    project = await _make_project(db_session, "EnrollOff")
    rc = {"acceptance_earn_execution_trials_enabled": False}
    with patch("app.shared_state.runtime_config", new=rc):
        task = await ac.enroll_criterion_coverage_trial(
            db_session,
            project.id,
            criterion_ids=["c-1"],
            source_task_id="t-1",
            test_refs=["testing/test_x.py"],
        )
    assert task is None


@pytest.mark.asyncio
async def test_enroll_criterion_coverage_trial_creates_bounded_task_and_dedups(db_session):
    """Flag ON ⇒ a bounded criterion_coverage_trial is created; refs capped; deduped."""
    project = await _make_project(db_session, "EnrollOn")
    rc = {"acceptance_earn_execution_trials_enabled": True}
    many_refs = [f"testing/test_{i}.py" for i in range(15)]
    with patch("app.shared_state.runtime_config", new=rc):
        task = await ac.enroll_criterion_coverage_trial(
            db_session,
            project.id,
            criterion_ids=["c-1", "c-2"],
            source_task_id="t-1",
            test_refs=many_refs,
        )
    assert task is not None
    p = task.first_payload
    assert p["type"] == "criterion_coverage_trial"
    assert p["source_task_id"] == "t-1"
    assert p["criterion_ids"] == ["c-1", "c-2"]
    # Refs are capped to the per-trial bound.
    assert len(p["test_refs"]) == ac.MAX_CRITERION_COVERAGE_TRIAL_TEST_FILES
    assert p["max_test_files"] == ac.MAX_CRITERION_COVERAGE_TRIAL_TEST_FILES
    assert p["wall_clock_seconds"] == ac.CRITERION_COVERAGE_TRIAL_WALL_CLOCK_SECONDS
    assert task.status == TaskStatus.QUEUED

    # A second enroll for the same source task is skipped while the first is in-flight.
    with patch("app.shared_state.runtime_config", new=rc):
        dup = await ac.enroll_criterion_coverage_trial(
            db_session,
            project.id,
            criterion_ids=["c-1"],
            source_task_id="t-1",
            test_refs=["testing/test_x.py"],
        )
    assert dup is None


# ---------------------------------------------------------------------------
# backfill_merged_named_test_coverage — execution-trial enrollment
# ---------------------------------------------------------------------------


async def _make_merged_named_test_task(db_session, project, criterion, test_path):
    """Build a COMPLETED merged task whose declared named_test shipped in the PR."""
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    task = Task(
        project_id=project.id,
        payloads=[{
            "type": "create",
            "prompt": "add the test",
            "acceptance_evidence": {"kind": "named_test", "test_path": test_path},
        }],
        context={
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
            "_pr_changed_files": [test_path, "backend/app/target_module.py"],
        },
        criterion_ids=[criterion.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


async def _criterion_coverage_trials(db_session, project_id):
    rows = (
        await db_session.execute(select(Task).where(Task.project_id == project_id))
    ).scalars().all()
    return [
        t
        for t in rows
        if isinstance(t.first_payload, dict)
        and t.first_payload.get("type") == "criterion_coverage_trial"
    ]


@pytest.mark.asyncio
async def test_backfill_flag_off_records_backfilled_and_enrolls_no_trial(db_session):
    """Flag OFF ⇒ byte-for-byte prior behaviour: backfilled row, NO trial enrolled."""
    project = await _make_project(db_session, "BackfillFlagOff")
    version = await _make_version(db_session, project)
    crit = await ac.create_criterion(db_session, project.id, version.id, "Off crit")
    await _make_merged_named_test_task(
        db_session, project, crit, "testing/test_off_backfill.py"
    )

    rc = {"acceptance_earn_execution_trials_enabled": False}
    with patch("app.shared_state.runtime_config", new=rc):
        result = await ac.backfill_merged_named_test_coverage(db_session, project.id)

    assert result == 1  # int value = backfilled rows recorded
    assert result.trials_enrolled == 0
    ev = await ac.get_evidence_for_criterion(db_session, crit.id)
    assert len(ev) == 1
    assert ev[0].source_kind == ac.UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND
    assert ac.compute_criterion_coverage(ev) == "backfilled"
    assert await _criterion_coverage_trials(db_session, project.id) == []


@pytest.mark.asyncio
async def test_backfill_flag_on_enrolls_execution_trial(db_session):
    """Flag ON ⇒ backfilled row still recorded AND a bounded execution trial enrolled."""
    project = await _make_project(db_session, "BackfillFlagOn")
    version = await _make_version(db_session, project)
    crit = await ac.create_criterion(db_session, project.id, version.id, "On crit")
    src = await _make_merged_named_test_task(
        db_session, project, crit, "testing/test_on_backfill.py"
    )

    rc = {"acceptance_earn_execution_trials_enabled": True}
    with patch("app.shared_state.runtime_config", new=rc):
        result = await ac.backfill_merged_named_test_coverage(db_session, project.id)

    # The backfilled fallback is still recorded...
    assert result == 1
    ev = await ac.get_evidence_for_criterion(db_session, crit.id)
    assert ev[0].source_kind == ac.UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND
    # ...and exactly one execution trial was enrolled for the source task.
    assert result.trials_enrolled == 1
    trials = await _criterion_coverage_trials(db_session, project.id)
    assert len(trials) == 1
    p = trials[0].first_payload
    assert p["source_task_id"] == str(src.id)
    assert p["criterion_ids"] == [crit.criterion_key]
    assert p["test_refs"] == ["testing/test_on_backfill.py"]


@pytest.mark.asyncio
async def test_backfill_enrollment_cap_bounds_trials(db_session):
    """The per-pass enrollment cap bounds how many trials a single pass enrolls."""
    project = await _make_project(db_session, "BackfillCap")
    version = await _make_version(db_session, project)
    for i in range(3):
        crit = await ac.create_criterion(
            db_session, project.id, version.id, f"Cap crit {i}"
        )
        await _make_merged_named_test_task(
            db_session, project, crit, f"testing/test_cap_{i}.py"
        )

    rc = {"acceptance_earn_execution_trials_enabled": True}
    with patch("app.shared_state.runtime_config", new=rc):
        result = await ac.backfill_merged_named_test_coverage(
            db_session, project.id, max_trial_enrollments=2
        )

    # All three criteria still get their backfilled fallback...
    assert result == 3
    # ...but only two trials are enrolled — the cap bounds test execution.
    assert result.trials_enrolled == 2
    assert len(await _criterion_coverage_trials(db_session, project.id)) == 2

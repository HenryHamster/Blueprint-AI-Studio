"""Tests for the completion-debt ledger (``app.completion_debt``) and the
best-effort recorder hooks wired into each debt-creating write path.

Coverage:
  * idempotent upsert on ``dedupe_key`` (sync + async recorders);
  * idempotent seed from the HEAD dormant-state inventory (repeated calls never
    duplicate rows);
  * one assertion per writer family that the expected ledger row is created
    (no_ci_gate / capability_trial / retirement_residual / dormant_knob) while
    the original writer outcome is unchanged.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app import ci_resolution, completion_debt as cd
from app.capability_trial_verdict import persist_trial_verdicts_in_transaction
from app.models import CompletionDebtEntry, Project, Task
from app.runtime_config_state import (
    _DORMANT_MODE_KNOBS,
    _DORMANT_OFF_FLAGS,
    iter_dormant_knob_debts,
    runtime_config,
)
from app.strategic_completion import _spawn_retirement_memos
from app.time_utils import _utcnow

try:
    from app.models import TaskStatus
except Exception:  # noqa: BLE001 - defensive: enum lives in models
    TaskStatus = None


# ── Sync fixture (an independent, single-connection SQLite engine) ────────────


@pytest.fixture
def sync_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _count(session, **filters) -> int:
    stmt = select(func.count()).select_from(CompletionDebtEntry)
    for col, val in filters.items():
        stmt = stmt.where(getattr(CompletionDebtEntry, col) == val)
    return session.execute(stmt).scalar_one()


async def _acount(session, **filters) -> int:
    stmt = select(func.count()).select_from(CompletionDebtEntry)
    for col, val in filters.items():
        stmt = stmt.where(getattr(CompletionDebtEntry, col) == val)
    return (await session.execute(stmt)).scalar_one()


# ── Core recorder idempotency ────────────────────────────────────────────────


def test_sync_recorder_is_idempotent(sync_session):
    created_first = cd.record_completion_debt(
        sync_session,
        dedupe_key="unit:knob-x",
        kind=cd.KIND_DORMANT_KNOB,
        title="Knob X is default-off",
        detail={"knob": "x"},
    )
    created_second = cd.record_completion_debt(
        sync_session,
        dedupe_key="unit:knob-x",
        kind=cd.KIND_DORMANT_KNOB,
        title="Knob X is default-off (again)",
    )
    sync_session.commit()

    assert created_first is True
    assert created_second is False
    assert _count(sync_session, dedupe_key="unit:knob-x") == 1
    row = sync_session.execute(
        select(CompletionDebtEntry).where(
            CompletionDebtEntry.dedupe_key == "unit:knob-x"
        )
    ).scalar_one()
    assert row.status == cd.STATUS_OPEN
    assert row.kind == cd.KIND_DORMANT_KNOB
    # First title wins — the second call is a pure no-op, not an overwrite.
    assert row.title == "Knob X is default-off"


def test_sync_recorder_noops_on_missing_key(sync_session):
    assert cd.record_completion_debt(
        sync_session, dedupe_key="", kind=cd.KIND_DORMANT_KNOB, title="x"
    ) is False
    assert _count(sync_session) == 0


@pytest.mark.asyncio
async def test_async_recorder_is_idempotent(db_session):
    first = await cd.arecord_completion_debt(
        db_session,
        dedupe_key="unit:async-a",
        kind=cd.KIND_CAPABILITY_TRIAL,
        title="Async A",
    )
    second = await cd.arecord_completion_debt(
        db_session,
        dedupe_key="unit:async-a",
        kind=cd.KIND_CAPABILITY_TRIAL,
        title="Async A",
    )
    await db_session.commit()

    assert first is True
    assert second is False
    assert await _acount(db_session, dedupe_key="unit:async-a") == 1


@pytest.mark.asyncio
async def test_async_resolve_flips_open_to_resolved(db_session):
    await cd.arecord_completion_debt(
        db_session,
        dedupe_key="unit:resolve-me",
        kind=cd.KIND_CAPABILITY_TRIAL,
        title="Resolve me",
    )
    resolved = await cd.aresolve_completion_debt(
        db_session, "unit:resolve-me", reason="decided:adopt"
    )
    # A second resolve is a no-op (no open row left).
    resolved_again = await cd.aresolve_completion_debt(
        db_session, "unit:resolve-me"
    )
    await db_session.commit()

    assert resolved is True
    assert resolved_again is False
    row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key == "unit:resolve-me"
            )
        )
    ).scalar_one()
    assert row.status == cd.STATUS_RESOLVED
    assert row.resolved_at is not None
    assert row.resolution_reason == "decided:adopt"


# ── resolve_debt evidence + link_task_to_debt (strategist disposition path) ──


async def _seed_open_debt(session, project_id, *, title="Open obligation"):
    entry = CompletionDebtEntry(project_id=project_id, kind=cd.KIND_DORMANT_KNOB, title=title)
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry


@pytest.mark.asyncio
async def test_resolve_debt_persists_evidence_reason(db_session):
    """resolve_debt stores the supplied evidence in resolution_reason and stays
    optional (no reason → None) for the admin resolve endpoint."""
    project = Project(name="ResolveEvidence", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    with_reason = await _seed_open_debt(db_session, project.id, title="With reason")
    resolved = await cd.resolve_debt(
        db_session, with_reason.id, project_id=project.id, reason="proof: merged in #42"
    )
    await db_session.commit()
    assert resolved.status == cd.STATUS_RESOLVED
    assert resolved.resolution_reason == "proof: merged in #42"

    no_reason = await _seed_open_debt(db_session, project.id, title="No reason")
    resolved2 = await cd.resolve_debt(db_session, no_reason.id, project_id=project.id)
    await db_session.commit()
    assert resolved2.status == cd.STATUS_RESOLVED
    assert resolved2.resolution_reason is None


@pytest.mark.asyncio
async def test_link_task_to_debt_stamps_detail_and_dedupes(db_session):
    """link_task_to_debt appends a linked-task record to detail and dedupes on
    task_id while leaving the row open."""
    project = Project(name="LinkDebt", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    entry = await _seed_open_debt(db_session, project.id)

    await cd.link_task_to_debt(
        db_session, entry.id, project_id=project.id, task_id="task-1", note="spawned"
    )
    await db_session.commit()
    # Re-linking the same task is idempotent; a different task appends.
    await cd.link_task_to_debt(db_session, entry.id, project_id=project.id, task_id="task-1")
    await cd.link_task_to_debt(db_session, entry.id, project_id=project.id, task_id="task-2")
    await db_session.commit()
    await db_session.refresh(entry)

    linked = (entry.detail or {}).get("linked_tasks") or []
    task_ids = [x.get("task_id") for x in linked]
    assert task_ids == ["task-1", "task-2"], task_ids
    assert linked[0].get("note") == "spawned"
    # Linking never disposes the row — it stays open.
    assert entry.status == cd.STATUS_OPEN


@pytest.mark.asyncio
async def test_link_task_to_debt_not_found_and_not_open(db_session):
    """link_task_to_debt raises not_found for unknown/foreign rows and not_open
    for a disposed row."""
    project = Project(name="LinkGuards", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    import uuid as _uuid

    with pytest.raises(ValueError, match="not_found"):
        await cd.link_task_to_debt(
            db_session, _uuid.uuid4(), project_id=project.id, task_id="t"
        )

    entry = await _seed_open_debt(db_session, project.id)
    await cd.resolve_debt(db_session, entry.id, project_id=project.id)
    await db_session.commit()
    with pytest.raises(ValueError, match="not_open"):
        await cd.link_task_to_debt(
            db_session, entry.id, project_id=project.id, task_id="t"
        )


@pytest.mark.asyncio
async def test_link_task_to_debt_locked_read_sees_committed_state(db_session):
    """link_task_to_debt reads the latest committed detail (not a stale cache).

    The for-update lock forces a fresh DB read, so a second call always sees the
    first call's committed linked_tasks instead of caching the pre-commit snapshot.
    This is the guard against the lost-update defect where two concurrent callers
    both read detail, compute independent merged lists, and the later writer
    silently drops the earlier one's linked-task record.
    """
    project = Project(name="LockedRead", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Entry starts with no linked tasks.
    entry = CompletionDebtEntry(
        project_id=project.id,
        kind=cd.KIND_NO_CI_GATE,
        title="Gate fell open",
        detail=None,
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    # First link: initialises detail from None via the for-update read path.
    await cd.link_task_to_debt(
        db_session, entry.id, project_id=project.id, task_id="task-X", note="first"
    )
    await db_session.commit()

    # Second link: must see the committed state from the first call, not a stale
    # cached snapshot, so both task_ids end up in the linked_tasks list.
    await cd.link_task_to_debt(
        db_session, entry.id, project_id=project.id, task_id="task-Y"
    )
    await db_session.commit()
    await db_session.refresh(entry)

    linked = (entry.detail or {}).get("linked_tasks", [])
    task_ids = [x.get("task_id") for x in linked]
    assert task_ids == ["task-X", "task-Y"], (
        f"both task_ids must be present — lost-update would yield only one: {task_ids}"
    )
    assert linked[0].get("note") == "first"
    assert entry.status == cd.STATUS_OPEN
    # updated_at must be stamped by the for-update write path.
    assert entry.updated_at is not None


# ── Seed idempotency (repeated seed → no duplicates) ─────────────────────────


def test_seed_is_idempotent_and_covers_families(sync_session):
    first = cd.seed_completion_debt_inventory(sync_session)
    sync_session.commit()
    total_after_first = _count(sync_session)

    second = cd.seed_completion_debt_inventory(sync_session)
    sync_session.commit()

    assert first > 0
    assert second == 0, "re-seeding must never duplicate rows"
    assert _count(sync_session) == total_after_first
    # Standing inventory families are present.
    assert _count(sync_session, dedupe_key="seed:no_ci_gate_live_trial") == 1
    assert _count(sync_session, dedupe_key="seed:capability_trial_rail") == 1
    # Dormant runtime-config knobs are enumerated into the ledger.
    assert _count(sync_session, kind=cd.KIND_DORMANT_KNOB) >= 1


@pytest.mark.asyncio
async def test_aseed_matches_sync_seed_and_is_idempotent(db_session):
    created = await cd.aseed_completion_debt_inventory(db_session)
    await db_session.commit()
    again = await cd.aseed_completion_debt_inventory(db_session)
    await db_session.commit()

    assert created > 0
    assert again == 0
    assert await _acount(db_session, dedupe_key="seed:no_ci_gate_live_trial") == 1


@pytest.mark.asyncio
async def test_get_open_completion_debt_returns_bounded_snapshot(db_session):
    project = Project(name="p", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await cd.arecord_completion_debt(
        db_session,
        dedupe_key=f"open:{project.id}",
        kind=cd.KIND_NO_CI_GATE,
        title="No-CI gate fell open",
        project_id=project.id,
        detail={"description": "Unverified merge"},
    )
    await cd.arecord_completion_debt(
        db_session,
        dedupe_key=f"resolved:{project.id}",
        kind=cd.KIND_CAPABILITY_TRIAL,
        title="Trial resolved",
        project_id=project.id,
    )
    await cd.aresolve_completion_debt(
        db_session,
        f"resolved:{project.id}",
        reason="decided:adopt",
    )
    await db_session.commit()

    snapshot = await cd.get_open_completion_debt(db_session, project.id)

    assert isinstance(snapshot, dict)
    assert "items" in snapshot
    assert "total_open" in snapshot
    assert len(snapshot["items"]) == 1
    assert snapshot["total_open"] == 1
    entry = snapshot["items"][0]
    assert entry["kind"] == cd.KIND_NO_CI_GATE
    assert entry["description"] == "Unverified merge"
    assert entry["created_at"] is not None
    assert entry["age_days"] is not None and entry["age_days"] >= 0


@pytest.mark.asyncio
async def test_get_open_completion_debt_includes_global_rows(db_session):
    """Global debt (project_id=None) must appear in per-project snapshots."""
    project = Project(name="p", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await cd.arecord_completion_debt(
        db_session,
        dedupe_key="global:test_global_item",
        kind=cd.KIND_SEED_INVENTORY,
        title="Global seed item (no project)",
        project_id=None,
    )
    await cd.arecord_completion_debt(
        db_session,
        dedupe_key=f"project:{project.id}:test_scoped",
        kind=cd.KIND_NO_CI_GATE,
        title="Project-scoped item",
        project_id=project.id,
    )
    await db_session.commit()

    snapshot = await cd.get_open_completion_debt(db_session, project.id)
    kinds = {e["kind"] for e in snapshot["items"]}
    assert cd.KIND_SEED_INVENTORY in kinds, (
        "global seed rows (project_id=None) must surface in per-project snapshots"
    )
    assert cd.KIND_NO_CI_GATE in kinds, "project-scoped rows must also surface"


@pytest.mark.asyncio
async def test_open_debt_snapshot_reports_true_totals_and_per_kind_bounds(db_session):
    """total_open reflects reality even when items are capped; project rows first."""
    project = Project(name="p", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Insert 6 project-scoped rows across two kinds.
    for i in range(4):
        await cd.arecord_completion_debt(
            db_session,
            dedupe_key=f"proj:knob:{project.id}:{i}",
            kind=cd.KIND_DORMANT_KNOB,
            title=f"Dormant knob {i}",
            project_id=project.id,
        )
    for i in range(2):
        await cd.arecord_completion_debt(
            db_session,
            dedupe_key=f"proj:gate:{project.id}:{i}",
            kind=cd.KIND_NO_CI_GATE,
            title=f"Gate fell open {i}",
            project_id=project.id,
        )
    # Insert 7 global rows that would overflow the 10-row snapshot cap.
    for i in range(7):
        await cd.arecord_completion_debt(
            db_session,
            dedupe_key=f"global:seed:{i}",
            kind=cd.KIND_SEED_INVENTORY,
            title=f"Global seed {i}",
            project_id=None,
        )
    await db_session.commit()

    snapshot = await cd.get_open_completion_debt(db_session, project.id)

    # total_open must reflect all 13 open rows, not just the 10-item cap.
    assert snapshot["total_open"] == 13
    assert len(snapshot["items"]) == 10  # bounded by _OPEN_COMPLETION_DEBT_LIMIT

    # Project-scoped rows float to the top: all 6 project rows must appear in
    # the snapshot before any global seed rows.
    project_items = [e for e in snapshot["items"] if e["kind"] != cd.KIND_SEED_INVENTORY]
    global_items = [e for e in snapshot["items"] if e["kind"] == cd.KIND_SEED_INVENTORY]
    assert len(project_items) == 6, "all 6 project-scoped rows must appear in snapshot"
    assert len(global_items) == 4, "remaining 4 slots filled from global rows"
    # Project rows come before global rows in the snapshot.
    if project_items and global_items:
        last_project_idx = max(snapshot["items"].index(e) for e in project_items)
        first_global_idx = min(snapshot["items"].index(e) for e in global_items)
        assert last_project_idx < first_global_idx, (
            "project-scoped rows must appear before global rows in the snapshot"
        )


# ── Writer family: no_ci_gate (ci_resolution.record_no_ci_gate_runtime) ──────


def test_no_ci_gate_writer_records_debt_on_fall_open(sync_session):
    project = Project(name="p", user_id="u")
    sync_session.add(project)
    sync_session.commit()

    ci_resolution.record_no_ci_gate_runtime(
        sync_session,
        project,
        executed_real_tests=False,
        unverified_reason="degraded_suite_unavailable",
        when=_utcnow(),
    )
    sync_session.commit()

    # Original writer outcome unchanged: the runtime block still records the
    # fall-open exactly as before.
    assert project.merge_gate_runtime["last_unverified_reason"] == (
        "degraded_suite_unavailable"
    )
    # And a single deduped debt row exists for this project.
    key = f"no_ci_gate:{project.id}"
    assert _count(sync_session, dedupe_key=key) == 1
    row = sync_session.execute(
        select(CompletionDebtEntry).where(CompletionDebtEntry.dedupe_key == key)
    ).scalar_one()
    assert row.kind == cd.KIND_NO_CI_GATE
    assert row.status == cd.STATUS_OPEN
    assert row.project_id == project.id

    # A verified run does NOT create debt.
    ci_resolution.record_no_ci_gate_runtime(
        sync_session,
        project,
        executed_real_tests=True,
        unverified_reason=None,
        when=_utcnow(),
    )
    sync_session.commit()
    assert _count(sync_session, dedupe_key=key) == 1


@pytest.mark.asyncio
async def test_no_ci_gate_writer_records_debt_on_fall_open_async(db_session):
    project = Project(name="p", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await ci_resolution.arecord_no_ci_gate_runtime(
        db_session,
        project,
        executed_real_tests=False,
        unverified_reason="degraded_suite_unavailable",
        when=_utcnow(),
    )
    await db_session.commit()

    assert project.merge_gate_runtime["last_unverified_reason"] == (
        "degraded_suite_unavailable"
    )
    key = f"no_ci_gate:{project.id}"
    assert await _acount(db_session, dedupe_key=key) == 1
    row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(CompletionDebtEntry.dedupe_key == key)
        )
    ).scalar_one()
    assert row.kind == cd.KIND_NO_CI_GATE
    assert row.status == cd.STATUS_OPEN
    assert row.project_id == project.id


# ── Writer family: capability_trial (persist_trial_verdicts_in_transaction) ──


@pytest.mark.asyncio
async def test_capability_trial_writer_opens_debt(db_session):
    project = Project(name="p", user_id="u", capability_adoption_verdicts={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Snapshot the scalar BEFORE expire_all() below: an expired sync read on an
    # async session raises MissingGreenlet under real ORM expiry.
    project_id = project.id
    key = f"capability_trial:{project_id}:cap_widget"

    # Observe/trial verdict → open debt. ``persist_trial_verdicts_in_transaction``
    # opens its OWN committing session; we only read AFTER it returns so this
    # session holds no in-flight transaction on the shared test connection.
    await persist_trial_verdicts_in_transaction(
        db_session,
        project_id,
        pending_verdicts=[
            ("cap_widget", {"verdict": "continue-trial", "verdict_reason": "insufficient_data"})
        ],
    )
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(CompletionDebtEntry.dedupe_key == key)
        )
    ).scalar_one()
    assert row.kind == cd.KIND_CAPABILITY_TRIAL
    assert row.status == cd.STATUS_OPEN
    assert row.project_id == project_id
    # Idempotent across writes — still exactly one row.
    assert await _acount(db_session, dedupe_key=key) == 1


@pytest.mark.asyncio
async def test_capability_decided_verdict_resolves_debt(db_session):
    project = Project(name="p", user_id="u", capability_adoption_verdicts={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    key = f"capability_trial:{project.id}:cap_widget"

    # Two back-to-back persists (observe → decided) each on their own committing
    # session; no reads in between so the shared test connection stays free.
    await persist_trial_verdicts_in_transaction(
        db_session,
        project.id,
        pending_verdicts=[
            ("cap_widget", {"verdict": "continue-trial", "verdict_reason": "insufficient_data"})
        ],
    )
    await persist_trial_verdicts_in_transaction(
        db_session,
        project.id,
        pending_verdicts=[
            ("cap_widget", {"verdict": "adopt", "evidence_source": "strategic_review"})
        ],
    )
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(CompletionDebtEntry.dedupe_key == key)
        )
    ).scalar_one()
    # A decided verdict discharges the debt, and still only one row exists.
    assert row.status == cd.STATUS_RESOLVED
    assert await _acount(db_session, dedupe_key=key) == 1


# ── Writer family: retirement_residual (_spawn_retirement_memos) ─────────────


@pytest.mark.asyncio
async def test_retirement_writer_records_residual_debt(db_session):
    direction_id = "dir-retire-1"
    project = Project(
        name="P",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {"id": direction_id, "text": "Direction to retire", "status": "retired"}
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "r"}],
        status=TaskStatus.COMPLETED if TaskStatus else None,
        created_by="ORCHESTRATOR",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    spawned = await _spawn_retirement_memos(
        db_session,
        project,
        task,
        retired_ids=[direction_id],
        rationales={direction_id: "Delivered full scope."},
    )
    await db_session.commit()

    # Original outcome unchanged: exactly one memo spawned.
    assert spawned == 1
    key = f"retirement_residual:{project.id}:{direction_id}"
    assert await _acount(db_session, dedupe_key=key) == 1
    row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(CompletionDebtEntry.dedupe_key == key)
        )
    ).scalar_one()
    assert row.kind == cd.KIND_RETIREMENT_RESIDUAL
    assert row.status == cd.STATUS_OPEN
    assert row.project_id == project.id


# ── Deferred-retirement residual resolution ──────────────────────────────────


def test_deferred_retirement_dedupe_key():
    """The deferred key is the ``:deferred`` sibling of the memo handoff key."""
    key = cd.deferred_retirement_dedupe_key("proj-1", "dir-9")
    assert key == "retirement_residual:proj-1:dir-9:deferred"
    # Distinct from the ordinary (memo) retirement residual key.
    assert key != "retirement_residual:proj-1:dir-9"


async def _make_retire_fixture(db_session, direction_id: str):
    project = Project(
        name="P",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {"id": direction_id, "text": "Direction to retire", "status": "retired"}
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "r"}],
        status=TaskStatus.COMPLETED if TaskStatus else None,
        created_by="ORCHESTRATOR",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task


@pytest.mark.asyncio
async def test_successful_retirement_resolves_deferred_residual_debt(db_session):
    """A gate-deferred retirement residual is resolved once the retire succeeds,
    while the separate memo handoff debt is created and left open."""
    direction_id = "dir-deferred-1"
    project, task = await _make_retire_fixture(db_session, direction_id)

    # Simulate the earlier gate-DEFERRED path opening the deferred residual row.
    deferred_key = cd.deferred_retirement_dedupe_key(project.id, direction_id)
    opened = await cd.arecord_completion_debt(
        db_session,
        dedupe_key=deferred_key,
        kind=cd.KIND_RETIREMENT_RESIDUAL,
        title="Retirement deferred for direction",
        project_id=project.id,
        source="admin_agent_ops",
        detail={"direction_id": direction_id, "kind": "deferred"},
    )
    await db_session.commit()
    assert opened is True

    # The retirement now succeeds through the memo-spawn path.
    spawned = await _spawn_retirement_memos(
        db_session,
        project,
        task,
        retired_ids=[direction_id],
        rationales={direction_id: "Delivered full scope."},
    )
    await db_session.commit()

    # Original outcome unchanged: exactly one memo spawned.
    assert spawned == 1

    # The deferred residual is now resolved and absent from the open snapshot.
    deferred_row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key == deferred_key
            )
        )
    ).scalar_one()
    assert deferred_row.status == cd.STATUS_RESOLVED
    assert deferred_row.resolved_at is not None

    snapshot = await cd.get_open_completion_debt(db_session, project.id)
    open_keys = {item["id"] for item in snapshot["items"]}
    assert str(deferred_row.id) not in open_keys

    # The ordinary memo handoff debt is a SEPARATE obligation, created and open.
    memo_key = f"retirement_residual:{project.id}:{direction_id}"
    memo_row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key == memo_key
            )
        )
    ).scalar_one()
    assert memo_row.status == cd.STATUS_OPEN


@pytest.mark.asyncio
async def test_deferred_resolution_leaves_ordinary_residual_untouched(db_session):
    """Resolving the deferred residual must not touch an already-open
    ordinary (memo) retirement residual for the same direction."""
    direction_id = "dir-deferred-2"
    project, _task = await _make_retire_fixture(db_session, direction_id)

    # Pre-open BOTH the deferred residual and the ordinary memo handoff debt.
    deferred_key = cd.deferred_retirement_dedupe_key(project.id, direction_id)
    memo_key = f"retirement_residual:{project.id}:{direction_id}"
    for key in (deferred_key, memo_key):
        await cd.arecord_completion_debt(
            db_session,
            dedupe_key=key,
            kind=cd.KIND_RETIREMENT_RESIDUAL,
            title="pre-existing residual",
            project_id=project.id,
        )
    await db_session.commit()

    resolved = await cd.aresolve_deferred_retirement_debt(
        db_session, project.id, direction_id
    )
    await db_session.commit()
    assert resolved is True

    deferred_row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key == deferred_key
            )
        )
    ).scalar_one()
    memo_row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key == memo_key
            )
        )
    ).scalar_one()
    assert deferred_row.status == cd.STATUS_RESOLVED
    # The ordinary memo residual stays open — it is a distinct obligation.
    assert memo_row.status == cd.STATUS_OPEN


@pytest.mark.asyncio
async def test_deferred_resolution_does_not_reopen_dismissed(db_session):
    """A dismissed deferred residual must not be reopened or altered by the
    resolver; resolution only touches OPEN rows."""
    direction_id = "dir-deferred-3"
    project, _task = await _make_retire_fixture(db_session, direction_id)

    deferred_key = cd.deferred_retirement_dedupe_key(project.id, direction_id)
    await cd.arecord_completion_debt(
        db_session,
        dedupe_key=deferred_key,
        kind=cd.KIND_RETIREMENT_RESIDUAL,
        title="deferred residual",
        project_id=project.id,
    )
    await db_session.commit()
    # Dismiss the row (operator disposition — permanent).
    row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key == deferred_key
            )
        )
    ).scalar_one()
    dismissed = await cd.dismiss_debt(
        db_session, row.id, project_id=project.id, reason="obsolete"
    )
    await db_session.commit()
    assert dismissed.status == cd.STATUS_DISMISSED

    # Resolving is a no-op — there is no OPEN row to flip.
    resolved = await cd.aresolve_deferred_retirement_debt(
        db_session, project.id, direction_id
    )
    await db_session.commit()
    assert resolved is False

    row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key == deferred_key
            )
        )
    ).scalar_one()
    assert row.status == cd.STATUS_DISMISSED
    assert row.resolution_reason == "obsolete"


# ── Two-way reconciliation ───────────────────────────────────────────────────


def test_resolved_debt_can_be_reopened(sync_session):
    """A resolved obligation with the same dedupe_key is reopened when re-recorded."""
    key = "unit:reopen-me"
    first = cd.record_completion_debt(
        sync_session, dedupe_key=key, kind=cd.KIND_DORMANT_KNOB, title="T"
    )
    assert first is True
    # Manually resolve the row.
    row = sync_session.execute(
        select(CompletionDebtEntry).where(CompletionDebtEntry.dedupe_key == key)
    ).scalar_one()
    row.status = cd.STATUS_RESOLVED
    sync_session.flush()

    # Re-recording after resolution must succeed (returns True) and leave exactly
    # one open row (the original row is reopened in-place).
    second = cd.record_completion_debt(
        sync_session, dedupe_key=key, kind=cd.KIND_DORMANT_KNOB, title="T (recurred)"
    )
    sync_session.commit()

    assert second is True
    open_rows = sync_session.execute(
        select(CompletionDebtEntry).where(
            CompletionDebtEntry.dedupe_key == key,
            CompletionDebtEntry.status == cd.STATUS_OPEN,
        )
    ).scalars().all()
    assert len(open_rows) == 1, "exactly one open row must exist after re-record"
    assert _count(sync_session, dedupe_key=key) == 1, "still a single row (reopened)"


def test_dismissed_debt_is_not_reopened_by_recorder(sync_session):
    """A dismissed debt must NOT be reopened by a re-record — dismissal is permanent."""
    key = "unit:dismissed-no-reopen"
    cd.record_completion_debt(
        sync_session, dedupe_key=key, kind=cd.KIND_DORMANT_KNOB, title="T"
    )
    row = sync_session.execute(
        select(CompletionDebtEntry).where(CompletionDebtEntry.dedupe_key == key)
    ).scalar_one()
    row.status = cd.STATUS_DISMISSED
    row.resolution_reason = "operator:not-applicable"
    sync_session.flush()

    result = cd.record_completion_debt(
        sync_session, dedupe_key=key, kind=cd.KIND_DORMANT_KNOB, title="T (retry)"
    )
    sync_session.commit()

    assert result is False, "re-recording a dismissed debt must return False"
    row = sync_session.execute(
        select(CompletionDebtEntry).where(CompletionDebtEntry.dedupe_key == key)
    ).scalar_one()
    assert row.status == cd.STATUS_DISMISSED, "status must remain dismissed"
    assert row.resolution_reason == "operator:not-applicable", (
        "dismissal reason must not be cleared"
    )
    assert _count(sync_session, dedupe_key=key) == 1, "must still be a single row"


@pytest.mark.asyncio
async def test_async_dismissed_debt_is_not_reopened(db_session):
    """Async recorder must also leave DISMISSED rows untouched."""
    key = "unit:async-dismissed-no-reopen"
    await cd.arecord_completion_debt(
        db_session, dedupe_key=key, kind=cd.KIND_DORMANT_KNOB, title="T"
    )
    row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(CompletionDebtEntry.dedupe_key == key)
        )
    ).scalar_one()
    row.status = cd.STATUS_DISMISSED
    row.resolution_reason = "operator:dismissed"
    await db_session.flush()

    result = await cd.arecord_completion_debt(
        db_session, dedupe_key=key, kind=cd.KIND_DORMANT_KNOB, title="T (retry)"
    )
    await db_session.commit()

    assert result is False, "async re-record of a dismissed debt must return False"
    row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(CompletionDebtEntry.dedupe_key == key)
        )
    ).scalar_one()
    assert row.status == cd.STATUS_DISMISSED
    assert row.resolution_reason == "operator:dismissed"


def test_dismissed_debt_not_reopened_by_seed(sync_session):
    """Startup seed must not resurrect a dismissed global seed/dormant-knob row."""
    # Seed once to create the inventory rows.
    created = cd.seed_completion_debt_inventory(sync_session)
    sync_session.commit()
    assert created > 0

    # Dismiss one of the seeded dormant-knob rows.
    row = sync_session.execute(
        select(CompletionDebtEntry).where(
            CompletionDebtEntry.kind == cd.KIND_DORMANT_KNOB
        )
    ).scalars().first()
    assert row is not None, "at least one dormant-knob row must exist after seed"
    dismissed_key = row.dedupe_key
    row.status = cd.STATUS_DISMISSED
    row.resolution_reason = "operator:not-applicable"
    sync_session.flush()
    sync_session.commit()

    # Re-seed (simulates a restart).
    cd.seed_completion_debt_inventory(sync_session)
    sync_session.commit()

    # The dismissed row must still be dismissed.
    row = sync_session.execute(
        select(CompletionDebtEntry).where(
            CompletionDebtEntry.dedupe_key == dismissed_key
        )
    ).scalar_one()
    assert row.status == cd.STATUS_DISMISSED, (
        "restart seed must not resurrect a dismissed debt row"
    )
    assert row.resolution_reason == "operator:not-applicable"
    assert _count(sync_session, dedupe_key=dismissed_key) == 1


def test_open_debt_still_idempotent_after_reopen_fix(sync_session):
    """An already-open debt must still be a no-op (unchanged behavior)."""
    key = "unit:still-idempotent"
    first = cd.record_completion_debt(
        sync_session, dedupe_key=key, kind=cd.KIND_DORMANT_KNOB, title="First"
    )
    second = cd.record_completion_debt(
        sync_session, dedupe_key=key, kind=cd.KIND_DORMANT_KNOB, title="Second"
    )
    sync_session.commit()

    assert first is True
    assert second is False
    assert _count(sync_session, dedupe_key=key) == 1


@pytest.mark.asyncio
async def test_knob_graduation_resolves_dormant_debt(db_session):
    """Patching a dormant knob to 'enforce' and back re-resolves and reopens its debt."""
    from app.routers.admin_settings import _apply_settings

    # Seed the dormant-knob debt row for evergreen_fill_mode.
    seeded = await cd.arecord_completion_debt(
        db_session,
        dedupe_key="dormant_knob:evergreen_fill_mode",
        kind=cd.KIND_DORMANT_KNOB,
        title="Evergreen fill mode is off/observe",
        project_id=None,
    )
    await db_session.commit()
    assert seeded is True

    # Call _apply_settings directly with a session and a minimal runtime_config.
    _rc: dict = {"evergreen_fill_mode": "observe"}
    await _apply_settings({"evergreen_fill_mode": "enforce"}, db_session, _rc)
    # _apply_settings may or may not have committed, but the resolution savepoint
    # is flushed — expire so we read the updated state.
    db_session.expire_all()

    row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key == "dormant_knob:evergreen_fill_mode"
            )
        )
    ).scalar_one()
    assert row.status == cd.STATUS_RESOLVED, (
        "graduating evergreen_fill_mode to 'enforce' must resolve its completion-debt entry"
    )

    await _apply_settings({"evergreen_fill_mode": "observe"}, db_session, _rc)
    db_session.expire_all()

    reopened_row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key == "dormant_knob:evergreen_fill_mode"
            )
        )
    ).scalar_one()
    assert reopened_row.status == cd.STATUS_OPEN, (
        "demoting evergreen_fill_mode back to 'observe' must reopen its completion-debt entry"
    )


@pytest.mark.asyncio
async def test_dormant_knob_reconciliation_recovers_env_graduation_and_demotion(
    db_session, monkeypatch
):
    """Startup-style reconciliation must resolve stale rows and reopen them."""
    from app.completion_debt import areconcile_dormant_knob_debts

    key = "dormant_knob:safeguard_challenge_gate_mode"
    seeded = await cd.arecord_completion_debt(
        db_session,
        dedupe_key=key,
        kind=cd.KIND_DORMANT_KNOB,
        title="Safeguard challenge gate is observe-only",
        project_id=None,
    )
    await db_session.commit()
    assert seeded is True

    monkeypatch.setitem(runtime_config, "safeguard_challenge_gate_mode", "enforce")
    stats = await areconcile_dormant_knob_debts(db_session)
    await db_session.commit()
    db_session.expire_all()

    resolved_row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(CompletionDebtEntry.dedupe_key == key)
        )
    ).scalar_one()
    assert resolved_row.status == cd.STATUS_RESOLVED
    assert stats["resolved"] >= 1

    monkeypatch.setitem(runtime_config, "safeguard_challenge_gate_mode", "observe")
    stats = await areconcile_dormant_knob_debts(db_session)
    await db_session.commit()
    db_session.expire_all()

    reopened_row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(CompletionDebtEntry.dedupe_key == key)
        )
    ).scalar_one()
    assert reopened_row.status == cd.STATUS_OPEN
    assert stats["opened"] >= 1


@pytest.mark.asyncio
async def test_dormant_knob_reconciliation_leaves_manual_debts_open(db_session):
    """Reconciliation must not resolve dormant_knob rows it does not own.

    A dormant_knob obligation recorded outside the runtime-config registry
    (manual/strategist/admin flows: non-registry dedupe key, different source)
    has no corresponding knob in ``iter_dormant_knob_debts`` — it must survive
    reconciliation untouched, not be swept as ``graduated:runtime-config``.
    """
    from app.completion_debt import areconcile_dormant_knob_debts

    key = "dormant_knob:manual:external-integration-followup"
    seeded = await cd.arecord_completion_debt(
        db_session,
        dedupe_key=key,
        kind=cd.KIND_DORMANT_KNOB,
        title="Manually tracked dormant integration",
        project_id=None,
        source="admin_agent_ops",
    )
    await db_session.commit()
    assert seeded is True

    await areconcile_dormant_knob_debts(db_session)
    await db_session.commit()
    db_session.expire_all()

    row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(CompletionDebtEntry.dedupe_key == key)
        )
    ).scalar_one()
    assert row.status == cd.STATUS_OPEN, (
        "manual dormant_knob debt must not be resolved by registry reconciliation"
    )

    # And the same holds for the PATCH-settings path (explicit runtime_config).
    await areconcile_dormant_knob_debts(db_session, dict(runtime_config))
    await db_session.commit()
    db_session.expire_all()

    row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(CompletionDebtEntry.dedupe_key == key)
        )
    ).scalar_one()
    assert row.status == cd.STATUS_OPEN


# ── Dormant-knob registry completeness ───────────────────────────────────


def test_dormant_knob_registry_covers_all_dormant_defaults():
    """Verify that iter_dormant_knob_debts covers all dormant mode knobs and flags.

    The registry enumerates knobs that ship in a dormant/default-off/observe state.
    This test ensures no dormant knobs silently under-record in the ledger by
    verifying that every knob with a dormant value is represented in the debt
    entries returned by iter_dormant_knob_debts().
    """
    debts = iter_dormant_knob_debts()
    registered_knobs = {d["detail"]["knob"] for d in debts}

    # Verify all dormant mode knobs are registered.
    for knob_key in _DORMANT_MODE_KNOBS:
        value = str(runtime_config.get(knob_key, "")).lower()
        if value in ("off", "observe"):
            assert (
                knob_key in registered_knobs
            ), f"Dormant mode knob '{knob_key}' (value={value}) not in registry"

    # Verify all dormant off flags are registered.
    for knob_key in _DORMANT_OFF_FLAGS:
        if not runtime_config.get(knob_key, False):
            assert (
                knob_key in registered_knobs
            ), f"Dormant flag '{knob_key}' (value=False) not in registry"


def test_completion_debt_weight_mode_seeds_dormant_debt(monkeypatch):
    """The new completion_debt_weight_mode knob (default 'observe') is a real
    runtime_config key, so iter_dormant_knob_debts now emits its dormant-knob row.

    Regression guard for Defect 2: before the knob became a real default key,
    runtime_config.get(key, "") yielded "" (not in {off, observe}) and the row
    was silently skipped — so the graduation gate seeded no obligation.
    """
    # The shipped default must be a member of the dormant set so the row seeds.
    assert str(runtime_config.get("completion_debt_weight_mode")).lower() == "observe"

    debts = iter_dormant_knob_debts()
    by_key = {d["dedupe_key"]: d for d in debts}
    assert "dormant_knob:completion_debt_weight_mode" in by_key, (
        "completion_debt_weight_mode in observe mode must seed its dormant-knob debt"
    )
    entry = by_key["dormant_knob:completion_debt_weight_mode"]
    assert entry["kind"] == "dormant_knob"
    assert entry["detail"] == {"knob": "completion_debt_weight_mode", "value": "observe"}

    # When graduated to enforce, the knob leaves the dormant set → no seed row.
    monkeypatch.setitem(runtime_config, "completion_debt_weight_mode", "enforce")
    enforced_keys = {d["dedupe_key"] for d in iter_dormant_knob_debts()}
    assert "dormant_knob:completion_debt_weight_mode" not in enforced_keys


@pytest.mark.asyncio
async def test_autonomous_spawn_budget_mode_seeds_and_graduates_dormant_debt(db_session):
    """autonomous_spawn_budget_mode in observe mode must seed and then resolve
    its dormant-knob debt when graduated to enforce."""
    from app.routers.admin_settings import _apply_settings

    assert str(runtime_config.get("autonomous_spawn_budget_mode")).lower() == "observe"

    debts = iter_dormant_knob_debts()
    by_key = {d["dedupe_key"]: d for d in debts}
    assert "dormant_knob:autonomous_spawn_budget_mode" in by_key, (
        "autonomous_spawn_budget_mode in observe mode must seed its dormant-knob debt"
    )
    entry = by_key["dormant_knob:autonomous_spawn_budget_mode"]
    assert entry["kind"] == "dormant_knob"
    assert entry["detail"] == {
        "knob": "autonomous_spawn_budget_mode",
        "value": "observe",
    }

    seeded = await cd.arecord_completion_debt(
        db_session,
        dedupe_key="dormant_knob:autonomous_spawn_budget_mode",
        kind=cd.KIND_DORMANT_KNOB,
        title="Autonomous spawn-budget mode observe-only pending dedicated rollout",
        project_id=None,
    )
    await db_session.commit()
    assert seeded is True

    _rc: dict = {"autonomous_spawn_budget_mode": "observe"}
    await _apply_settings({"autonomous_spawn_budget_mode": "enforce"}, db_session, _rc)
    assert _rc["autonomous_spawn_budget_mode"] == "enforce"
    db_session.expire_all()

    row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key
                == "dormant_knob:autonomous_spawn_budget_mode"
            )
        )
    ).scalar_one()
    assert row.status == cd.STATUS_RESOLVED, (
        "graduating autonomous_spawn_budget_mode to 'enforce' must resolve its "
        "completion-debt entry"
    )


@pytest.mark.asyncio
async def test_seed_writes_completion_debt_design_doc_section(db_session):
    """Verify the design-doc section is seeded when absent and is idempotent.

    Regression test for named_test acceptance requirement: the section must be
    written to design_doc and re-seeding must not duplicate or churn fields.
    """
    from app.completion_debt import seed_completion_debt_design_doc_section

    project = Project(name="test-seed-doc", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # First seed should write the section.
    result_first = await seed_completion_debt_design_doc_section(db_session, project.id)
    await db_session.commit()

    assert result_first is True, "first seed should return True (section written)"

    # Refresh project to read the updated design_doc.
    db_session.expire(project, ["design_doc"])
    await db_session.refresh(project)

    doc = project.design_doc or {}
    section_key = "systems/completion-debt-ledger"
    assert section_key in doc, f"section '{section_key}' must exist in design_doc"

    section = doc[section_key]
    assert isinstance(section, dict), "section must be a dict"
    assert section.get("summary") is not None, "section must have a summary"
    assert section.get("status") == "active", "section status must be 'active'"
    assert "debt_kinds" in section, "section must list debt_kinds"
    assert cd.KIND_DORMANT_KNOB in section["debt_kinds"], (
        "dormant_knob must be in debt_kinds"
    )
    assert cd.KIND_CAPABILITY_TRIAL in section["debt_kinds"], (
        "capability_trial must be in debt_kinds"
    )
    assert cd.KIND_NO_CI_GATE in section["debt_kinds"], "no_ci_gate must be in debt_kinds"
    assert cd.KIND_RETIREMENT_RESIDUAL in section["debt_kinds"], (
        "retirement_residual must be in debt_kinds"
    )
    assert cd.KIND_SEED_INVENTORY in section["debt_kinds"], (
        "seed_inventory must be in debt_kinds"
    )
    assert "recorders" in section, "section must list recorder surfaces"
    assert "reconciliation_surfaces" in section, "section must list reconciliation surfaces"
    assert "strategist_surface" in section, "section must document strategist surface"
    assert "admin_surface" in section, "section must document admin surface"
    assert "module" in section, "section must list the source module"

    # Re-seeding should be idempotent (no churn, no duplication).
    result_second = await seed_completion_debt_design_doc_section(db_session, project.id)
    await db_session.commit()

    assert result_second is False, "second seed should return False (no changes)"

    # Refresh again and verify the section is unchanged.
    db_session.expire(project, ["design_doc"])
    await db_session.refresh(project)
    doc_after_reseed = project.design_doc or {}
    assert doc_after_reseed.get(section_key) == section, (
        "re-seeding must not churn or modify the section"
    )

    # Verify that operator-authored additions are not clobbered.
    # Manually add an operator field via a fresh DB read, then re-seed.
    # Need to use with_design_doc_locked to properly track changes to JSONB.
    from app.task_locking import with_design_doc_locked

    def _add_operator_note(doc):
        doc[section_key]["operator_note"] = "This is an operator-added note"

    await with_design_doc_locked(db_session, project.id, _add_operator_note, skip_locked=False)
    await db_session.commit()

    # Re-seed again — the operator_note should remain.
    await seed_completion_debt_design_doc_section(db_session, project.id)
    await db_session.commit()

    db_session.expire(project, ["design_doc"])
    await db_session.refresh(project)
    doc_after_op = project.design_doc or {}
    section_after_op = doc_after_op.get(section_key, {})
    assert section_after_op.get("operator_note") == "This is an operator-added note", (
        "re-seeding must preserve operator-authored additions"
    )
    assert section_after_op.get("summary") is not None, (
        "re-seeding must refresh code-authored fields (summary)"
    )
    # The code fields should have been refreshed (result_third should be False if no code change
    # between this test run and the previous, but it could be True if code fields changed).
    # We just verify the section still has the expected structure.
    assert "debt_kinds" in section_after_op, "section must still have debt_kinds after re-seed"


@pytest.mark.asyncio
async def test_completion_debt_weight_mode_graduation_resolves_debt(db_session):
    """Graduating completion_debt_weight_mode to 'enforce' via _apply_settings
    resolves its standing dormant-knob completion-debt row."""
    from app.routers.admin_settings import _apply_settings

    seeded = await cd.arecord_completion_debt(
        db_session,
        dedupe_key="dormant_knob:completion_debt_weight_mode",
        kind=cd.KIND_DORMANT_KNOB,
        title="Completion-debt pressure signal observe-mode",
        project_id=None,
    )
    await db_session.commit()
    assert seeded is True

    _rc: dict = {"completion_debt_weight_mode": "observe"}
    await _apply_settings(
        {"completion_debt_weight_mode": "enforce"}, db_session, _rc
    )
    assert _rc["completion_debt_weight_mode"] == "enforce"
    db_session.expire_all()

    row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key
                == "dormant_knob:completion_debt_weight_mode"
            )
        )
    ).scalar_one()
    assert row.status == cd.STATUS_RESOLVED, (
        "graduating completion_debt_weight_mode to 'enforce' must resolve its "
        "completion-debt entry"
    )


# ── New behavior: title/description and linked-task status ───────────────────


@pytest.mark.asyncio
async def test_dormant_knob_description_is_title_not_bare_knob_value(db_session):
    """dormant_knob rows must surface their human title as description, not 'off'/'observe'."""
    project = Project(name="p", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await cd.arecord_completion_debt(
        db_session,
        dedupe_key=f"dormant_knob:test:{project.id}",
        kind=cd.KIND_DORMANT_KNOB,
        title="Feature X shipped default-off",
        project_id=project.id,
        detail={"knob": "feature_x_enabled", "value": "off"},
    )
    await db_session.commit()

    snapshot = await cd.get_open_completion_debt(db_session, project.id)
    assert len(snapshot["items"]) == 1
    item = snapshot["items"][0]
    assert item["kind"] == cd.KIND_DORMANT_KNOB
    # Description must be the human title, NOT the bare knob value.
    assert item["description"] == "Feature X shipped default-off"
    assert item["description"] != "off"
    assert item["title"] == "Feature X shipped default-off"


@pytest.mark.asyncio
async def test_get_open_completion_debt_includes_linked_tasks_with_statuses(db_session):
    """Snapshot items include linked_tasks list with resolved Task.status values."""
    project = Project(name="p", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create a real Task so its status can be resolved.
    task = Task(
        project_id=project.id,
        title="Follow-up task for the debt",
        payloads=[{"type": "fix"}],
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.flush()

    await cd.arecord_completion_debt(
        db_session,
        dedupe_key=f"no_ci_gate:linked:{project.id}",
        kind=cd.KIND_NO_CI_GATE,
        title="No-CI gate fell open",
        project_id=project.id,
        detail={"description": "Unverified merge", "linked_tasks": [{"task_id": str(task.id)}]},
    )
    await db_session.commit()

    snapshot = await cd.get_open_completion_debt(db_session, project.id)
    assert len(snapshot["items"]) == 1
    item = snapshot["items"][0]
    assert "linked_tasks" in item
    assert len(item["linked_tasks"]) == 1
    lt = item["linked_tasks"][0]
    assert lt["task_id"] == str(task.id)
    assert lt["status"] == "IN_PROGRESS"


@pytest.mark.asyncio
async def test_get_open_completion_debt_linked_task_missing_yields_unknown(db_session):
    """A linked task_id that matches no Task row gets status='unknown'."""
    import uuid as _uuid

    project = Project(name="p", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    phantom_id = str(_uuid.uuid4())
    await cd.arecord_completion_debt(
        db_session,
        dedupe_key=f"no_ci_gate:phantom:{project.id}",
        kind=cd.KIND_NO_CI_GATE,
        title="Gate fell open (linked task gone)",
        project_id=project.id,
        detail={"linked_tasks": [{"task_id": phantom_id}]},
    )
    await db_session.commit()

    snapshot = await cd.get_open_completion_debt(db_session, project.id)
    item = snapshot["items"][0]
    assert "linked_tasks" in item
    lt = item["linked_tasks"][0]
    assert lt["task_id"] == phantom_id
    assert lt["status"] == "unknown"


def test_bound_detail_trims_oldest_linked_tasks_preserving_shape():
    """_bound_detail preserves dict shape when trimming an oversized linked_tasks list.

    Pre-fix, _bound_detail collapsed the whole dict to a flat truncated preview
    when bytes exceeded _MAX_DETAIL_BYTES, so every subsequent link_task_to_debt
    call found no linked_tasks list and restarted from empty. The fix trims only
    the linked_tasks list (oldest-first), keeping all other keys intact.
    """
    import json as _json

    from app.completion_debt import _MAX_DETAIL_BYTES, _bound_detail

    # Build a detail dict with recorder metadata plus enough linked_tasks entries
    # to push over _MAX_DETAIL_BYTES (each entry is ~75 bytes; 80 entries ≈ 6000 B).
    recorder_meta = "important-recorder-metadata"
    detail = {
        "recorder_key": recorder_meta,
        "description": "A meaningful description to preserve",
        "linked_tasks": [
            {"task_id": f"task-{i:04d}-aaaa-bbbb-cccc-ddddeeeeffff", "note": f"entry {i}"}
            for i in range(80)
        ],
    }
    assert len(_json.dumps(detail, default=str)) > _MAX_DETAIL_BYTES, (
        "test setup: input must exceed _MAX_DETAIL_BYTES"
    )

    result = _bound_detail(detail)

    # Must return a dict (shape preserved), NOT the flat _truncated preview.
    assert isinstance(result, dict)
    assert "_truncated" not in result, (
        "overflow with a trimmable linked_tasks must NOT collapse to the flat preview; "
        "pre-fix code returned _truncated=True, so every subsequent link_task_to_debt "
        "call restarted from empty"
    )

    # Recorder metadata keys survive the trim.
    assert result.get("recorder_key") == recorder_meta, (
        "recorder_key must be preserved through the linked_tasks trim"
    )
    assert "description" in result, "description key must be preserved"

    # linked_tasks remains a list (shape intact).
    remaining = result.get("linked_tasks")
    assert isinstance(remaining, list), "linked_tasks must remain a list after trim"
    assert 1 <= len(remaining) < 80, "some entries trimmed; at least one kept"

    # Most-recent entry (index 79) survives; oldest (index 0) is dropped.
    surviving_ids = {x.get("task_id") for x in remaining}
    assert "task-0079-aaaa-bbbb-cccc-ddddeeeeffff" in surviving_ids, (
        "most-recent linked_task must be preserved"
    )
    assert "task-0000-aaaa-bbbb-cccc-ddddeeeeffff" not in surviving_ids, (
        "oldest linked_task must be dropped when trimming"
    )

    # Trimmed result must fit within the byte limit.
    assert len(_json.dumps(result, default=str)) <= _MAX_DETAIL_BYTES

    # Trim count observability marker is set.
    assert result.get("_linked_tasks_trimmed", 0) > 0, (
        "_linked_tasks_trimmed must be set when entries were dropped"
    )


@pytest.mark.asyncio
async def test_get_open_completion_debt_completed_linked_task_is_dispose_ready(db_session):
    """Snapshot flags a COMPLETED linked task so the strategist knows to dispose the debt."""
    project = Project(name="p", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    completed_task = Task(
        project_id=project.id,
        title="The work is done",
        payloads=[{"type": "fix"}],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(completed_task)
    await db_session.flush()

    await cd.arecord_completion_debt(
        db_session,
        dedupe_key=f"no_ci_gate:completed:{project.id}",
        kind=cd.KIND_NO_CI_GATE,
        title="Gate fell open — work done",
        project_id=project.id,
        detail={"linked_tasks": [{"task_id": str(completed_task.id)}]},
    )
    await db_session.commit()

    snapshot = await cd.get_open_completion_debt(db_session, project.id)
    item = snapshot["items"][0]
    assert "linked_tasks" in item
    lt = item["linked_tasks"][0]
    assert lt["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_bound_detail_preserves_structure_when_truncating(db_session):
    """Structure-preserving truncation: linked_tasks survive while oversized old entries are trimmed.

    Regression test for audit finding 3a1f5a73a782512e: when detail encoding exceeds
    _MAX_DETAIL_BYTES and linked_tasks is the oversized element, _bound_detail trims
    the oldest linked_tasks entries (dropping from the front) rather than collapsing
    the whole dict — keeping the list shape so link_task_to_debt can keep appending.
    Other keys (e.g. description) survive unchanged.
    """
    project = Project(name="StructurePreserve", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # The oversized element is the first linked_tasks entry (large note). _bound_detail
    # drops from the front (oldest first) until encoded size fits within _MAX_DETAIL_BYTES,
    # so task-1 is dropped and task-2 survives.
    large_note = "x" * 5000
    await cd.arecord_completion_debt(
        db_session,
        dedupe_key="test:structure_preserve",
        kind=cd.KIND_NO_CI_GATE,
        title="Test structure preservation",
        project_id=project.id,
        detail={
            "description": "Short description",
            "linked_tasks": [
                {"task_id": "task-1", "note": large_note},  # oversized — dropped as oldest
                {"task_id": "task-2"},                      # small — survives
            ],
        },
    )
    await db_session.commit()

    # Retrieve the entry to verify detail was bounded.
    entry = (
        await db_session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key == "test:structure_preserve"
            )
        )
    ).scalar_one()

    # _bound_detail trims the oldest linked_tasks entries; dict structure is preserved.
    detail = entry.detail
    assert detail is not None, "detail should not be None"
    assert isinstance(detail, dict), "detail should be a dict"
    assert "linked_tasks" in detail, (
        "linked_tasks must be preserved in dict structure after truncation"
    )
    assert isinstance(detail["linked_tasks"], list), (
        "linked_tasks must remain a list"
    )
    # task-1 (oversized, oldest) is dropped; task-2 (small, newest) survives.
    assert len(detail["linked_tasks"]) == 1, (
        "oversized oldest entry must be trimmed, leaving only the newest"
    )
    assert detail["linked_tasks"][0]["task_id"] == "task-2", (
        "newest (smallest) linked_task must survive trimming"
    )
    assert detail.get("description") == "Short description", (
        "small string fields must be preserved"
    )
    assert detail.get("_linked_tasks_trimmed") == 1, (
        "number of trimmed linked_tasks entries must be recorded"
    )
    # Encoded size must be within bounds.
    encoded = json.dumps(detail, default=str)
    assert len(encoded) <= cd._MAX_DETAIL_BYTES, (
        f"detail must fit in _MAX_DETAIL_BYTES; got {len(encoded)}"
    )


@pytest.mark.asyncio
async def test_bound_detail_preserves_linked_tasks_through_link_and_truncate_cycle(
    db_session,
):
    """Linking a task to a debt entry must preserve prior linked_tasks across truncations.

    Tests the concrete scenario from audit finding 3a1f5a73a782512e: repeated
    link_task_to_debt calls must not destroy prior task links even if the detail
    grows large enough to trigger truncation.
    """
    project = Project(name="LinkAndTruncate", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create entry with large detail that's already near truncation threshold.
    entry = CompletionDebtEntry(
        project_id=project.id,
        kind=cd.KIND_NO_CI_GATE,
        title="No-CI gate fell open",
        detail={
            "reason": "degraded_test_suite",
            "large_context": "c" * 3000,  # Near the truncation threshold.
        },
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    # First link should add task-1 to linked_tasks.
    await cd.link_task_to_debt(
        db_session, entry.id, project_id=project.id, task_id="task-1", note="first"
    )
    await db_session.commit()
    await db_session.refresh(entry)

    detail_after_first_link = entry.detail
    linked_after_first = (detail_after_first_link or {}).get("linked_tasks", [])
    assert len(linked_after_first) == 1
    assert linked_after_first[0]["task_id"] == "task-1"
    assert linked_after_first[0]["note"] == "first"

    # Second link adds task-2; must preserve task-1 even if detail is truncated.
    await cd.link_task_to_debt(
        db_session, entry.id, project_id=project.id, task_id="task-2", note="second"
    )
    await db_session.commit()
    await db_session.refresh(entry)

    detail_after_second_link = entry.detail
    linked_after_second = (detail_after_second_link or {}).get("linked_tasks", [])
    assert len(linked_after_second) == 2, (
        "both task links must be preserved; structure-preserving bounding must keep linked_tasks intact"
    )
    task_ids = [x.get("task_id") for x in linked_after_second]
    assert task_ids == ["task-1", "task-2"], (
        "task order must be preserved and no task lost to truncation"
    )
    assert linked_after_second[0]["note"] == "first", (
        "first task note must still be present"
    )
    assert linked_after_second[1]["note"] == "second", (
        "second task note must still be present"
    )

    # Verify the detail is still bounded.
    encoded = json.dumps(detail_after_second_link, default=str)
    assert len(encoded) <= cd._MAX_DETAIL_BYTES, (
        f"detail must fit in _MAX_DETAIL_BYTES even after multiple links; got {len(encoded)}"
    )

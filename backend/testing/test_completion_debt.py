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

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app import ci_resolution, completion_debt as cd
from app.capability_trial_verdict import persist_trial_verdicts_in_transaction
from app.models import CompletionDebtEntry, Project, Task
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

    assert len(snapshot) == 1
    entry = snapshot[0]
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
    kinds = {e["kind"] for e in snapshot}
    assert cd.KIND_SEED_INVENTORY in kinds, (
        "global seed rows (project_id=None) must surface in per-project snapshots"
    )
    assert cd.KIND_NO_CI_GATE in kinds, "project-scoped rows must also surface"


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

    key = f"capability_trial:{project.id}:cap_widget"

    # Observe/trial verdict → open debt. ``persist_trial_verdicts_in_transaction``
    # opens its OWN committing session; we only read AFTER it returns so this
    # session holds no in-flight transaction on the shared test connection.
    await persist_trial_verdicts_in_transaction(
        db_session,
        project.id,
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
    assert row.project_id == project.id
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

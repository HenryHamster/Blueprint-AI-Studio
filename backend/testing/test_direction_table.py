"""Tests for the first-class normalised ``direction`` table (p2-direction-table).

Covers:
  * ``directions_repo`` accessor: upsert (insert + update), get, list, expire.
  * Dual-write: ``with_directions_locked`` keeps the ``direction`` table in sync
    with the authoritative ``Project.directions`` blob (mutation, add, removal).
  * ``_expire_stale_proposals`` mirrors expiry into the table.
  * Postgres-only: the 0131 migration backfills the table from existing blobs
    and the round-trip preserves the full direction dict.
"""

import os
import uuid

import pytest
from sqlalchemy import select, text

from app.models import Direction, Project
from app import directions_repo
from app.task_locking import with_directions_locked
from testing.factories import make_project

USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true"


async def _table_rows(db_session, project_id) -> dict:
    rows = (
        (
            await db_session.execute(
                select(Direction).where(Direction.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    return {r.direction_key: r for r in rows}


# ── accessor ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_inserts_then_updates(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()

    await directions_repo.upsert_direction(
        db_session,
        project.id,
        {"id": "d-1", "text": "improve tests", "status": "active", "priority": "high"},
    )
    await db_session.commit()

    rows = await _table_rows(db_session, project.id)
    assert set(rows) == {"d-1"}
    assert rows["d-1"].status == "active"
    assert rows["d-1"].priority == "high"
    assert rows["d-1"].text == "improve tests"
    assert rows["d-1"].data["text"] == "improve tests"

    # Update the same key — single row, no duplicate.
    await directions_repo.upsert_direction(
        db_session,
        project.id,
        {"id": "d-1", "text": "improve tests", "status": "completed", "priority": "low"},
    )
    await db_session.commit()
    rows = await _table_rows(db_session, project.id)
    assert set(rows) == {"d-1"}
    assert rows["d-1"].status == "completed"
    assert rows["d-1"].priority == "low"


@pytest.mark.asyncio
async def test_upsert_without_id_is_noop(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await directions_repo.upsert_direction(db_session, project.id, {"text": "no id"})
    await db_session.commit()
    rows = await _table_rows(db_session, project.id)
    assert rows == {}


@pytest.mark.asyncio
async def test_get_and_list(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    for i in range(3):
        await directions_repo.upsert_direction(
            db_session, project.id, {"id": f"d-{i}", "text": f"t{i}", "status": "active"}
        )
    await db_session.commit()

    one = await directions_repo.get_direction(db_session, project.id, "d-1")
    assert one is not None and one["text"] == "t1"
    assert await directions_repo.get_direction(db_session, project.id, "nope") is None

    listed = await directions_repo.list_directions(db_session, project.id)
    assert {d["id"] for d in listed} == {"d-0", "d-1", "d-2"}


@pytest.mark.asyncio
async def test_get_direction_rows_batches(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    for i in range(3):
        await directions_repo.upsert_direction(
            db_session, project.id, {"id": f"d-{i}", "text": f"t{i}", "status": "active"}
        )
    await db_session.commit()

    # Batched fetch returns a dict keyed by direction_key, resolving only the
    # keys that exist (missing "nope" is silently absent, no error).
    rows = await directions_repo.get_direction_rows(
        db_session, project.id, ["d-0", "d-2", "nope"]
    )
    assert set(rows.keys()) == {"d-0", "d-2"}
    assert rows["d-0"].direction_key == "d-0"
    assert isinstance(rows["d-2"], Direction)

    # Empty / all-falsy input short-circuits to an empty dict.
    assert await directions_repo.get_direction_rows(db_session, project.id, []) == {}
    assert await directions_repo.get_direction_rows(
        db_session, project.id, [None, ""]
    ) == {}


@pytest.mark.asyncio
async def test_expire_proposals_accessor(db_session):
    from datetime import timedelta
    from app.time_utils import _utcnow

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    old_iso = (_utcnow() - timedelta(days=40)).isoformat()
    recent_iso = (_utcnow() - timedelta(days=2)).isoformat()
    cutoff = (_utcnow() - timedelta(days=30)).isoformat()
    await directions_repo.upsert_direction(
        db_session,
        project.id,
        {"id": "old", "status": "proposed", "proposed_at": old_iso},
    )
    await directions_repo.upsert_direction(
        db_session,
        project.id,
        {"id": "new", "status": "proposed", "proposed_at": recent_iso},
    )
    await db_session.commit()

    expired = await directions_repo.expire_proposals(db_session, project.id, cutoff)
    await db_session.commit()
    assert expired == 1
    rows = await _table_rows(db_session, project.id)
    assert rows["old"].status == "expired"
    assert rows["old"].data["status"] == "expired"
    assert "expired_at" in rows["old"].data
    assert rows["new"].status == "proposed"


# ── dual-write via with_directions_locked ────────────────────────────────────


@pytest.mark.asyncio
async def test_dual_write_syncs_on_append(db_session):
    project = make_project(directions=[{"id": "d-1", "text": "a", "status": "active"}])
    db_session.add(project)
    await db_session.commit()

    def _append(dirs):
        dirs.append({"id": "d-2", "text": "b", "status": "proposed"})

    await with_directions_locked(db_session, project.id, _append)
    await db_session.commit()

    rows = await _table_rows(db_session, project.id)
    assert set(rows) == {"d-1", "d-2"}
    assert rows["d-2"].status == "proposed"


@pytest.mark.asyncio
async def test_dual_write_syncs_on_mutation(db_session):
    project = make_project(directions=[{"id": "d-1", "text": "a", "status": "active"}])
    db_session.add(project)
    await db_session.commit()

    def _mutate(dirs):
        dirs[0]["status"] = "completed"
        dirs[0]["research_history"] = [{"task_id": "t1", "tasks_created": 2}]

    await with_directions_locked(db_session, project.id, _mutate)
    await db_session.commit()

    rows = await _table_rows(db_session, project.id)
    assert rows["d-1"].status == "completed"
    assert rows["d-1"].data["research_history"] == [
        {"task_id": "t1", "tasks_created": 2}
    ]


@pytest.mark.asyncio
async def test_dual_write_removes_row_on_whole_array_replace(db_session):
    project = make_project(
        directions=[
            {"id": "d-1", "text": "a", "status": "active"},
            {"id": "d-2", "text": "b", "status": "active"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    # Prime the table to match the blob.
    await directions_repo.sync_directions(db_session, project.id, project.directions)
    await db_session.commit()
    assert set(await _table_rows(db_session, project.id)) == {"d-1", "d-2"}

    # Replace the whole array, dropping d-2.
    await with_directions_locked(
        db_session,
        project.id,
        lambda _: [{"id": "d-1", "text": "a", "status": "active"}],
    )
    await db_session.commit()
    assert set(await _table_rows(db_session, project.id)) == {"d-1"}


@pytest.mark.asyncio
async def test_expire_stale_proposals_mirrors_into_table(db_session):
    from datetime import timedelta
    from app.time_utils import _utcnow
    from app.anomaly_detection import _expire_stale_proposals

    old_iso = (_utcnow() - timedelta(days=40)).isoformat()
    project = make_project(
        autonomous_enabled=True,
        directions=[{"id": "d-old", "status": "proposed", "proposed_at": old_iso}],
    )
    db_session.add(project)
    await db_session.commit()
    # Prime the table.
    await directions_repo.sync_directions(db_session, project.id, project.directions)
    await db_session.commit()

    count = await _expire_stale_proposals(db_session)
    assert count == 1
    rows = await _table_rows(db_session, project.id)
    assert rows["d-old"].status == "expired"


# ── Postgres-only: migration backfill round-trip ─────────────────────────────


@pytest.mark.skipif(
    not USE_POSTGRES, reason="backfill round-trip uses Postgres JSONB functions"
)
@pytest.mark.asyncio
async def test_migration_backfill_roundtrip(db_session):
    """Insert a project with a directions blob, run the 0131 backfill SQL, and
    verify each element round-trips into a normalised row (full dict in data,
    promoted columns extracted, no-id element skipped, duplicate id de-duped)."""
    pid = uuid.uuid4()
    project = Project(
        id=pid,
        name="backfill-rt",
        user_id="u",
        directions=[
            {"id": "d-a", "text": "alpha", "status": "active", "priority": "high"},
            {"id": "d-b", "text": "beta", "status": "proposed"},
            {"id": "d-a", "text": "dup", "status": "completed"},  # duplicate id
            {"text": "no id here", "status": "active"},  # no id -> skipped
        ],
    )
    db_session.add(project)
    await db_session.commit()

    # Mirror the migration's backfill SELECT/INSERT (the table already exists
    # in the CI schema; this exercises the exact backfill statement).
    await db_session.execute(
        text(
            """
            INSERT INTO direction (
                id, project_id, direction_key, status, priority, text, data,
                created_at, updated_at
            )
            SELECT DISTINCT ON (p.id, elem->>'id')
                gen_random_uuid(),
                p.id,
                LEFT(elem->>'id', 128),
                COALESCE(NULLIF(elem->>'status', ''), 'active'),
                NULLIF(elem->>'priority', ''),
                NULLIF(elem->>'text', ''),
                elem,
                NOW(),
                NOW()
            FROM project p
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(p.directions) = 'array' THEN p.directions
                    ELSE '[]'::jsonb
                END
            ) AS elem
            WHERE p.id = :pid AND elem->>'id' IS NOT NULL AND elem->>'id' <> ''
            ORDER BY p.id, elem->>'id'
            """
        ),
        {"pid": pid},
    )
    await db_session.commit()

    rows = await _table_rows(db_session, pid)
    # d-a (first/only via DISTINCT ON) + d-b ; the no-id element is skipped.
    assert set(rows) == {"d-a", "d-b"}
    assert rows["d-a"].priority == "high"
    assert rows["d-a"].text == "alpha"
    assert rows["d-a"].data["text"] in ("alpha", "dup")  # one wins, full dict kept
    assert rows["d-b"].status == "proposed"

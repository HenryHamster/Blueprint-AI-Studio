"""Tests for auto-supersession of stale operator messages on version close.

Covers the required behaviors:
  * a message from a promoted (>=2-versions-back) version gets auto-resolved;
  * a message from the CURRENT open version does not;
  * an unresolved BUG_REPORT-class message does not get auto-resolved even
    across a version boundary;
plus the observe/off mode gating and the age / version-distance safety margins.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from app import message_supersession as ms
from app.message_supersession import (
    SUPERSEDED_CONTEXT_KEY,
    supersede_stale_messages_on_version_close,
)
from app.models import MessageType, Project, ProjectMessage, ReleaseVersion
from app.shared_state import runtime_config as _rc
from app.time_utils import _utcnow

# A single naive-UTC "now" anchor (tests must derive timestamps from _utcnow(),
# never a bare datetime() constructor — see the DTZ lint policy). All offsets
# below are relative to this anchor and it is passed explicitly as ``now`` to the
# unit under test, so the age/version arithmetic is fully deterministic.
NOW = _utcnow()


@pytest_asyncio.fixture()
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    async with Session() as s:
        yield s
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _clean_config():
    """Restore the supersession knobs to their shipped defaults around each test."""
    saved = {k: v for k, v in _rc.items() if k.startswith("message_supersession_")}
    _rc["message_supersession_mode"] = "enforce"
    _rc["message_supersession_min_age_days"] = 14
    _rc["message_supersession_min_versions_back"] = 2
    _rc["message_supersession_max_resolve"] = 500
    yield
    for k in [k for k in _rc if k.startswith("message_supersession_")]:
        _rc.pop(k, None)
    _rc.update(saved)


async def _project(db) -> Project:
    p = Project(name="ms-unit", user_id="u1")
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _version(db, project, number: int, opened_at: datetime) -> ReleaseVersion:
    v = ReleaseVersion(
        project_id=project.id,
        version_number=number,
        status="open",
        opened_at=opened_at,
    )
    db.add(v)
    await db.commit()
    await db.refresh(v)
    return v


async def _message(
    db,
    project,
    *,
    created_at: datetime,
    msg_type=MessageType.NOTE,
    from_type: str = "strategic",
    status: str = "open",
) -> ProjectMessage:
    m = ProjectMessage(
        project_id=project.id,
        message_type=msg_type,
        from_type=from_type,
        content="status update",
        status=status,
        created_at=created_at,
    )
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


# ── Timeline used by the scenario tests ─────────────────────────────────────
# v1 opened 2026-01-01, v2 opened 2026-02-01. When v2 promotes, the successor v3
# opens (current open). With min_versions_back=2 the boundary is v2's opened_at
# (2026-02-01): messages created before it belong to v1 (>=2 versions back).
V1_OPENED = NOW - timedelta(days=180)
V2_OPENED = NOW - timedelta(days=150)
MSG_IN_V1 = NOW - timedelta(days=165)  # in v1's window, older than 14d → eligible
MSG_IN_V2 = NOW - timedelta(days=140)  # in v2's window → 1 version back → ineligible


async def _close_v2(db, project, *, mode_now=NOW):
    """Run supersession as if v2 just closed (v3 opened)."""
    return await supersede_stale_messages_on_version_close(
        db,
        project_id=project.id,
        closed_version_number=2,
        closed_version_opened_at=V2_OPENED,
        terminal_status="promoted",
        now=mode_now,
    )


@pytest.mark.asyncio
async def test_message_from_promoted_version_is_auto_resolved(db):
    project = await _project(db)
    await _version(db, project, 1, V1_OPENED)
    await _version(db, project, 2, V2_OPENED)
    msg = await _message(db, project, created_at=MSG_IN_V1)

    summary = await _close_v2(db, project)
    await db.commit()

    assert summary["mode"] == "enforce"
    assert summary["acted"] is True
    assert summary["superseded"] == 1

    await db.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolved_at == NOW
    assert "Auto-superseded" in (msg.resolution or "")
    assert "v2" in (msg.resolution or "")
    # Audit provenance stamped into context (no schema change).
    audit = (msg.context or {}).get(SUPERSEDED_CONTEXT_KEY)
    assert audit is not None
    assert audit["version_number"] == 2
    assert audit["terminal_status"] == "promoted"
    # ONE aggregate messages_superseded broadcast is queued (not one per message).
    assert len(summary["broadcasts"]) == 1
    pid, payload = summary["broadcasts"][0]
    assert pid == str(project.id)
    assert payload["count"] == 1
    assert str(msg.id) in payload["message_ids"]
    assert payload["version_number"] == 2
    assert payload["terminal_status"] == "promoted"


@pytest.mark.asyncio
async def test_message_from_current_open_version_is_not_resolved(db):
    project = await _project(db)
    await _version(db, project, 1, V1_OPENED)
    await _version(db, project, 2, V2_OPENED)
    # Authored in the current open version (just before now).
    current = await _message(db, project, created_at=NOW - timedelta(hours=1))
    # And one from the immediately-prior version (v2) — only 1 version back.
    prior = await _message(db, project, created_at=MSG_IN_V2)

    summary = await _close_v2(db, project)
    await db.commit()

    assert summary["superseded"] == 0
    await db.refresh(current)
    await db.refresh(prior)
    assert current.status == "open"
    assert prior.status == "open"


@pytest.mark.asyncio
async def test_unresolved_bug_report_not_resolved_across_version_boundary(db):
    project = await _project(db)
    await _version(db, project, 1, V1_OPENED)
    await _version(db, project, 2, V2_OPENED)
    # A bug report from v1 — old enough and far-enough back on every other axis,
    # but a still-open bug must NOT be resolved by a version promotion.
    bug = await _message(
        db, project, created_at=MSG_IN_V1, msg_type=MessageType.BUG_REPORT
    )
    blocker = await _message(
        db, project, created_at=MSG_IN_V1, msg_type=MessageType.BLOCKER
    )
    note = await _message(db, project, created_at=MSG_IN_V1)  # eligible control

    summary = await _close_v2(db, project)
    await db.commit()

    assert summary["skipped_bug_class"] == 2
    assert summary["superseded"] == 1  # only the NOTE
    await db.refresh(bug)
    await db.refresh(blocker)
    await db.refresh(note)
    assert bug.status == "open"
    assert blocker.status == "open"
    assert note.status == "resolved"


@pytest.mark.asyncio
async def test_observe_mode_measures_but_does_not_mutate(db):
    _rc["message_supersession_mode"] = "observe"
    project = await _project(db)
    await _version(db, project, 1, V1_OPENED)
    await _version(db, project, 2, V2_OPENED)
    msg = await _message(db, project, created_at=MSG_IN_V1)

    summary = await _close_v2(db, project)
    await db.commit()

    assert summary["mode"] == "observe"
    assert summary["acted"] is False
    assert summary["superseded"] == 1  # would-be count
    assert summary["broadcasts"] == []
    await db.refresh(msg)
    assert msg.status == "open"  # unchanged


@pytest.mark.asyncio
async def test_off_mode_is_a_noop(db):
    _rc["message_supersession_mode"] = "off"
    project = await _project(db)
    await _version(db, project, 1, V1_OPENED)
    await _version(db, project, 2, V2_OPENED)
    msg = await _message(db, project, created_at=MSG_IN_V1)

    summary = await _close_v2(db, project)
    await db.commit()

    assert summary["mode"] == "off"
    assert summary["candidates"] == 0
    assert summary["superseded"] == 0
    await db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_age_gate_blocks_recent_message_even_from_old_version(db):
    # min_versions_back=1 makes the version boundary ~now, so ONLY the age gate
    # protects a recent message from the just-closed version.
    _rc["message_supersession_min_versions_back"] = 1
    project = await _project(db)
    await _version(db, project, 1, V1_OPENED)
    await _version(db, project, 2, V2_OPENED)
    # Created 5 days ago — inside the 14-day age margin → must stay open.
    recent = await _message(db, project, created_at=NOW - timedelta(days=5))
    # Created well before the age cutoff → eligible.
    old = await _message(db, project, created_at=NOW - timedelta(days=40))

    summary = await _close_v2(db, project)
    await db.commit()

    assert summary["superseded"] == 1
    await db.refresh(recent)
    await db.refresh(old)
    assert recent.status == "open"
    assert old.status == "resolved"


@pytest.mark.asyncio
async def test_already_resolved_and_linked_messages_are_not_touched(db):
    project = await _project(db)
    await _version(db, project, 1, V1_OPENED)
    await _version(db, project, 2, V2_OPENED)
    resolved = await _message(
        db, project, created_at=MSG_IN_V1, status="resolved"
    )
    linked = await _message(db, project, created_at=MSG_IN_V1, status="linked")

    summary = await _close_v2(db, project)
    await db.commit()

    assert summary["superseded"] == 0
    await db.refresh(resolved)
    await db.refresh(linked)
    assert resolved.status == "resolved"
    assert linked.status == "linked"


@pytest.mark.asyncio
async def test_config_resolver_clamps_bad_values_to_defaults():
    _rc["message_supersession_mode"] = "nonsense"
    _rc["message_supersession_min_age_days"] = -5
    _rc["message_supersession_min_versions_back"] = 0
    _rc["message_supersession_max_resolve"] = 0
    cfg = ms._resolve_config()
    assert cfg["mode"] == ms.DEFAULT_MODE
    assert cfg["min_age_days"] == ms.DEFAULT_MIN_AGE_DAYS
    assert cfg["min_versions_back"] == ms.DEFAULT_MIN_VERSIONS_BACK
    assert cfg["max_resolve"] == ms.DEFAULT_MAX_RESOLVE


@pytest.mark.asyncio
async def test_larger_min_versions_back_uses_older_boundary_version(db):
    # With min_versions_back=3, closing v3 supersedes only messages from v1
    # (>=3 back from the open v4), NOT v2. Boundary = v2.opened_at, looked up.
    _rc["message_supersession_min_versions_back"] = 3
    project = await _project(db)
    await _version(db, project, 1, V1_OPENED)
    await _version(db, project, 2, V2_OPENED)
    v3_opened = NOW - timedelta(days=120)
    await _version(db, project, 3, v3_opened)
    from_v1 = await _message(db, project, created_at=MSG_IN_V1)  # < v2.opened
    from_v2 = await _message(db, project, created_at=MSG_IN_V2)  # >= v2.opened

    summary = await supersede_stale_messages_on_version_close(
        db,
        project_id=project.id,
        closed_version_number=3,
        closed_version_opened_at=v3_opened,
        terminal_status="promoted",
        now=NOW,
    )
    await db.commit()

    assert summary["superseded"] == 1
    await db.refresh(from_v1)
    await db.refresh(from_v2)
    assert from_v1.status == "resolved"
    assert from_v2.status == "open"


@pytest.mark.asyncio
async def test_supersession_scoped_to_project(db):
    project = await _project(db)
    other = Project(name="ms-other", user_id="u2")
    db.add(other)
    await db.commit()
    await db.refresh(other)
    await _version(db, project, 1, V1_OPENED)
    await _version(db, project, 2, V2_OPENED)
    mine = await _message(db, project, created_at=MSG_IN_V1)
    theirs = await _message(db, other, created_at=MSG_IN_V1)

    await _close_v2(db, project)
    await db.commit()

    await db.refresh(mine)
    await db.refresh(theirs)
    assert mine.status == "resolved"
    assert theirs.status == "open"  # another project's backlog is untouched


@pytest.mark.asyncio
async def test_open_rows_remaining_after_enforce(db):
    """Sanity: the eligible open row is the only one flipped; querying open rows
    afterward returns just the survivors."""
    project = await _project(db)
    await _version(db, project, 1, V1_OPENED)
    await _version(db, project, 2, V2_OPENED)
    await _message(db, project, created_at=MSG_IN_V1)  # resolved
    survivor = await _message(db, project, created_at=MSG_IN_V2)  # 1 back → open

    await _close_v2(db, project)
    await db.commit()

    open_rows = (
        (
            await db.execute(
                select(ProjectMessage).where(ProjectMessage.status == "open")
            )
        )
        .scalars()
        .all()
    )
    assert [r.id for r in open_rows] == [survivor.id]


@pytest.mark.asyncio
async def test_enforce_mode_emits_one_broadcast_for_many_messages(db):
    """Batch supersession must emit ONE aggregate broadcast, not one per message.

    This is the core regression guard for the broadcast-storm fix: up to
    message_supersession_max_resolve (default 500) messages may be eligible in a
    single version-close, but the caller must fire at most one live-update event.
    """
    _rc["message_supersession_max_resolve"] = 100
    project = await _project(db)
    await _version(db, project, 1, V1_OPENED)
    await _version(db, project, 2, V2_OPENED)

    # Create 10 eligible messages (all in v1's window, older than 14d).
    n = 10
    msgs = [
        await _message(db, project, created_at=MSG_IN_V1 - timedelta(seconds=i))
        for i in range(n)
    ]

    summary = await _close_v2(db, project)
    await db.commit()

    assert summary["superseded"] == n
    # Exactly ONE broadcast entry regardless of how many messages were resolved.
    assert len(summary["broadcasts"]) == 1, (
        f"Expected 1 aggregate broadcast but got {len(summary['broadcasts'])} "
        f"(one per message would be a broadcast storm)."
    )
    pid, payload = summary["broadcasts"][0]
    assert pid == str(project.id)
    assert payload["count"] == n
    assert payload["version_number"] == 2
    assert len(payload["message_ids"]) == n
    # All resolved message IDs are included in the aggregate payload.
    resolved_ids = {str(m.id) for m in msgs}
    assert set(payload["message_ids"]) == resolved_ids

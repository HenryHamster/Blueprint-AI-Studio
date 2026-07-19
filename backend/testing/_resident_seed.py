"""Shared test seeding helper for the Strategic Resident control plane.

The Resident schema (migration 0611 + 0623/0627) protects its authority
invariants with a ring of PostgreSQL triggers and non-deferrable, *circular*
foreign keys:

* ``strategicresidentcheckpoint.project_id`` -> ``strategicresident(project_id)``
* ``strategicresident.current_checkpoint_id`` -> ``strategicresidentcheckpoint(id)``

plus ``BEFORE INSERT/UPDATE`` fences (``fence_strategicresident_checkpoint_rotation``,
``fence_strategicresidentcheckpoint_insert``, ``enforce_strategicresident_authority_monotonic``,
``enforce_strategicresident_project_links`` ...). Those fences only authorise a
checkpoint rotation / ownership flip / receipt insert that is driven by a real,
matching ``IN_PROGRESS`` ``strategic_resident_turn`` task. That is exactly what
the *product* requires — but it makes it impossible to *seed* a fully-formed
"authoritative Resident + current checkpoint" starting state directly: the
circular FK + the ``BEFORE INSERT`` project-links trigger reject either insert
order, and pointing an already-``strategic_resident``-owned row at its checkpoint
(or flipping ownership) trips the rotation / monotonic fences.

These tests were originally authored against a trigger-less schema (an in-memory
SQLite ``metadata.create_all`` fixture, or a Resident's local run) where FK and
trigger enforcement simply does not exist, so their setup helpers seed state the
real migrated schema rejects. The CI Postgres shard builds the schema via the
Alembic migrations, so the fences are live and the *fixture seeding* — not the
assertion under test — fails.

``seed_without_authority_enforcement`` bridges that gap for **pure setup seeding
only**. On PostgreSQL the test role is a superuser, so
``SET session_replication_role = 'replica'`` disables FK checks *and every
user-defined trigger* for the connection; ``'origin'`` restores them. On SQLite
(the in-memory fixtures) this is a no-op.

This never weakens what a test verifies: it only relaxes enforcement while
inserting the *starting rows*, then restores it before the tested action.

Connection scoping
------------------
``session_replication_role`` is a **connection-level** GUC. The Postgres test
engine uses ``NullPool``, so a ``commit()`` closes the underlying connection and
the next statement runs on a *fresh* one that defaults back to ``'origin'``.
This helper therefore requires the wrapped seed to perform **at most one
commit** (or only ``flush()`` calls): the ``SET 'replica'`` is applied on the
connection the seed's inserts + single commit run on, and ``SET 'origin'`` is
re-applied on exit. A seed that needs several commits must be restructured to a
single commit (see ``_authority_rows`` in test_strategic_operation_authority.py,
which builds the checkpoint + task-payload update + resident in one commit).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession


@asynccontextmanager
async def seed_without_authority_enforcement(
    session: AsyncSession,
) -> AsyncIterator[None]:
    """Disable Postgres FK + user triggers for the duration of setup seeding.

    Dialect-aware: on any non-PostgreSQL bind (the SQLite in-memory fixtures)
    this is a no-op, so it is safe to wrap seeding that runs against either
    backend. The decision is made from the *session's* dialect, never a global
    env flag — several suites use a SQLite ``authority_db`` fixture even when the
    surrounding CI shard is Postgres.
    """
    bind = session.get_bind()
    if getattr(bind.dialect, "name", "") != "postgresql":
        # SQLite / trigger-less fixture — nothing to relax.
        yield
        return

    connection = await session.connection()
    await connection.exec_driver_sql("SET session_replication_role = 'replica'")
    try:
        yield
    finally:
        # Restore enforcement so the test's asserted action runs with all fences
        # ACTIVE. For a flush-only seed this is the same open-transaction
        # connection; for a single-commit seed the commit already released it and
        # this re-applies 'origin' on a fresh connection (which defaults to
        # 'origin' anyway) — explicit and pool-behaviour independent.
        connection = await session.connection()
        await connection.exec_driver_sql("SET session_replication_role = 'origin'")

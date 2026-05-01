"""Backfill heartbeat_count + align SystemMaturity server_defaults.

Round 4 audit fixes (R4-W3):

H9 — Migration 0066 added ``task.heartbeat_count`` with ``server_default=0``
but didn't backfill from R2's storage location ``task.context['_heartbeat_count']``.
In-flight tasks (those whose heartbeat counter pre-dates 0066's deploy) lose
their counter on upgrade, biasing the every-5th-heartbeat audit-trail-persist
gate for the lifetime of those rows. This migration backfills the counter
from JSONB context for any task still sitting at the default of 0.

M4 — ``SystemMaturity.last_audited_at`` is declared in models.py with
``server_default=func.now()`` (round 3 audit H9 routes the column through
``NaiveUTCDateTime`` for tz-stripping symmetry), but migration 0052 created
the column without a server_default. Raw-SQL INSERTs (admin tooling, future
backfills) would silently NULL-violate. This migration aligns the DDL with
the model declaration. ``created_at`` and ``updated_at`` already had
``server_default=func.now()`` in 0052, so only ``last_audited_at`` needs the
fix.

Revision ID: 0068
Revises: 0067
Create Date: 2026-04-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # H9: backfill heartbeat_count from R2's JSONB storage. Postgres-only —
    # SQLite test path uses SQLModel.metadata.create_all and never has
    # legacy in-flight rows to migrate. The ``= 0`` filter ensures we only
    # touch rows still at the column-default; rows already incremented by
    # the new ORM-mutation path are left untouched.
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            UPDATE task
            SET heartbeat_count = COALESCE((context->>'_heartbeat_count')::int, 0)
            WHERE jsonb_typeof(context) = 'object'
              AND context ? '_heartbeat_count'
              AND heartbeat_count = 0
            """
        )

    # M4: align systemmaturity.last_audited_at server_default with the model
    # declaration (``func.now()``). Postgres-only — SQLite reflects models.py
    # directly through SQLModel.metadata.create_all so the test path already
    # has the right default. ``created_at`` and ``updated_at`` already have
    # server_default=now() from 0052; only ``last_audited_at`` is missing it.
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "systemmaturity",
            "last_audited_at",
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
            server_default=sa.text("now()"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    # H9: no down-migration. The backfill is forward-only — restoring the
    # counter to 0 on rows that have since accumulated heartbeats would
    # break the audit-trail gate worse than leaving the higher value in
    # place. Downgrades to pre-0068 work fine without reverting this data.

    # M4: drop the server_default we added.
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "systemmaturity",
            "last_audited_at",
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
            server_default=None,
        )

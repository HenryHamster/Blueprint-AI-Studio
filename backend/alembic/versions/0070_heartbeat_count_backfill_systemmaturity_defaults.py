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


revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # H9: backfill heartbeat_count from R2's JSONB storage. Postgres-only —
    # SQLite test path uses SQLModel.metadata.create_all and never has
    # legacy in-flight rows to migrate. The ``= 0`` filter ensures we only
    # touch rows still at the column-default; rows already incremented by
    # the new ORM-mutation path are left untouched.
    #
    # R5 audit MEDIUM-B: the cast ``(context->>'_heartbeat_count')::int``
    # aborts the whole migration mid-table if any row holds a non-numeric
    # value at that subkey (e.g. ``"abc"``, an array, or ``null``). Guard
    # the cast with ``jsonb_typeof(...) = 'number'`` so non-numeric
    # garbage is silently skipped (those rows stay at heartbeat_count=0,
    # which matches the prior R2 behavior on first heartbeat).
    # In-place fix safe per ``reference_migration_patterns.md``: 0068 was
    # added in commit 16afe63e (today) and has never been applied to a
    # production DB — only ever exercised in the Docker-based test path.
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            UPDATE task
            SET heartbeat_count = COALESCE((context->>'_heartbeat_count')::int, 0)
            WHERE jsonb_typeof(context) = 'object'
              AND context ? '_heartbeat_count'
              AND jsonb_typeof(context->'_heartbeat_count') = 'number'
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
    #
    # R5-W5 MED-3 caveat: on a Postgres DB whose tables were initially
    # bootstrapped via ``SQLModel.metadata.create_all`` (e.g. test fixtures
    # that bypass alembic), ``last_audited_at`` already has the
    # ``func.now()`` server_default declared on the model. The upgrade()
    # alter_column above is a no-op against such a DB, but this downgrade()
    # would still drop the model-declared default, leaving the bootstrapped
    # DB without a default that the model expects. This is acceptable
    # because:
    #   1. test fixtures don't run migration downgrades — they tear down
    #      with metadata.drop_all,
    #   2. real Postgres deployments always go through alembic upgrade,
    #      so the default on disk is the one this migration set,
    #   3. detecting the bootstrap case at runtime would require inspecting
    #      pg_attrdef and rebuilding the original default, far more
    #      complexity than the edge case warrants.
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "systemmaturity",
            "last_audited_at",
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
            server_default=None,
        )

"""Align ProjectComputeImageBuild datetime columns to TIMESTAMPTZ.

PR #15618 merged with an unfixed review MAJOR: migration 0633 (add-table
for ``projectcomputeimagebuild``) declared four datetime columns as bare
``sa.DateTime()``, while the model uses ``NaiveUTCDateTime`` whose impl is
``sa.DateTime(timezone=True)`` → ``TIMESTAMPTZ``.

This migration is the Postgres-only ALTER COLUMN … TYPE fix. The table
is config-gated OFF and empty in production, so risk is near zero.

Conversion semantics: ``ALTER COLUMN … TYPE TIMESTAMPTZ USING <col> AT
TIME ZONE 'UTC'`` reads the existing naive timestamp as if it were
already in UTC (which it is — ``_utcnow`` produces naive UTC and the
asyncpg session is pinned to ``timezone='UTC'``). No data migration
beyond the type cast is required.

SQLite: no-op (as in migration 0063 — tests reflect ``models.py`` via
``SQLModel.metadata.create_all``, not by running this migration).

Revision ID: 0639
Revises: 0638
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0639"
down_revision = "0638"
branch_labels = None
depends_on = None

_TABLE = "projectcomputeimagebuild"

# (column, existing_nullable, had_server_default_before)
_COLUMNS = [
    ("started_at",   True,  False),
    ("deadline_at",  True,  False),
    ("created_at",   False, True),
    ("updated_at",   False, True),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for column, existing_nullable, had_default in _COLUMNS:
        kwargs = {
            "type_": sa.DateTime(timezone=True),
            "existing_type": sa.DateTime(),
            "existing_nullable": existing_nullable,
            "postgresql_using": f"{column} AT TIME ZONE 'UTC'",
        }
        if had_default:
            # Preserve the server_default the column was created with.
            kwargs["server_default"] = sa.text("now()")
        op.alter_column(_TABLE, column, **kwargs)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for column, existing_nullable, had_default in _COLUMNS:
        kwargs = {
            "type_": sa.DateTime(),
            "existing_type": sa.DateTime(timezone=True),
            "existing_nullable": existing_nullable,
            "postgresql_using": f"{column} AT TIME ZONE 'UTC'",
        }
        if had_default:
            # Preserve the pre-existing server_default (created_at/updated_at
            # were created with server_default=now() in migration 0633).
            kwargs["server_default"] = sa.text("now()")
        else:
            # Drop any default we might have added (started_at / deadline_at
            # had no server_default before this migration).
            kwargs["server_default"] = None
        op.alter_column(_TABLE, column, **kwargs)

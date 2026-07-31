"""Make Strategic Memory timestamps timezone-aware.

Revision ID: 0718
Revises: 0717
Create Date: 2026-07-30

The Strategic Memory models use ``NaiveUTCDateTime`` (a TIMESTAMPTZ-backed
type).  The initial 0715--0717 tables used plain ``sa.DateTime()``, so this
corrective migration interprets their existing naive values as UTC before
changing the PostgreSQL column type.  SQLite has no distinct timestamp-with-
timezone storage type, but batch alteration keeps its migration harness in
lockstep with the ORM declaration.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0718"
down_revision = "0717"
branch_labels = None
depends_on = None


# Keep this as a literal list: schema invariants can inspect the corrective
# migration and prove that every affected table/column reaches TIMESTAMPTZ.
_COLUMNS = [
    ("strategicmemoryprojectstate", "created_at", True),
    ("strategicmemoryprojectstate", "updated_at", True),
    ("strategiccontextenvelopesnapshot", "created_at", True),
    ("strategiccontextenvelopesnapshot", "expires_at", False),
    ("strategicmemorysource", "occurred_at", False),
    ("strategicmemorysource", "recorded_at", True),
    ("strategicmemorysource", "tombstoned_at", False),
    ("strategicmemorybackfillcursor", "created_at", True),
    ("strategicmemorybackfillcursor", "updated_at", True),
    ("strategicmemoryconsolidationjob", "lease_expires_at", False),
    ("strategicmemoryconsolidationjob", "created_at", True),
    ("strategicmemoryconsolidationjob", "updated_at", True),
    ("strategicmemoryconsolidationjob", "completed_at", False),
    ("strategicmemorysourcedisposition", "processed_at", False),
    ("strategicmemoryitem", "created_at", True),
    ("strategicmemoryitem", "updated_at", True),
    ("strategicmemoryitem", "forgotten_at", False),
    ("strategicmemoryitemversion", "asserted_effective_from", False),
    ("strategicmemoryitemversion", "asserted_effective_until", False),
    ("strategicmemoryitemversion", "recorded_at", True),
    ("strategicmemoryitemversion", "created_at", True),
    ("strategicmemorytransition", "effective_from", False),
    ("strategicmemorytransition", "effective_until", False),
    ("strategicmemorytransition", "created_at", True),
    ("strategicmemorycorrectionbarrier", "created_at", True),
    ("strategicmemorycorrectionbarrierevent", "created_at", True),
    ("strategicmemoryedge", "created_at", True),
    ("strategicmemoryprojection", "rebuilt_at", True),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table, column, has_current_timestamp_default in _COLUMNS:
            if has_current_timestamp_default:
                op.alter_column(
                    table,
                    column,
                    existing_type=sa.DateTime(),
                    server_default=None,
                )
            op.alter_column(
                table,
                column,
                existing_type=sa.DateTime(),
                type_=sa.DateTime(timezone=True),
                postgresql_using=f"{column} AT TIME ZONE 'UTC'",
            )
            if has_current_timestamp_default:
                op.alter_column(
                    table,
                    column,
                    existing_type=sa.DateTime(timezone=True),
                    server_default=sa.text("CURRENT_TIMESTAMP"),
                )
        return

    if bind.dialect.name == "sqlite":
        # SQLite has no distinct timezone-aware datetime storage type.  A batch
        # rebuild would therefore change no on-disk semantics while silently
        # dropping 0716's append-only triggers on the rebuilt ledger tables.
        return

    raise RuntimeError(
        "0717 Strategic Memory timestamp migration supports PostgreSQL and SQLite only"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table, column, has_current_timestamp_default in _COLUMNS:
            if has_current_timestamp_default:
                op.alter_column(
                    table,
                    column,
                    existing_type=sa.DateTime(timezone=True),
                    server_default=None,
                )
            op.alter_column(
                table,
                column,
                existing_type=sa.DateTime(timezone=True),
                type_=sa.DateTime(),
                postgresql_using=f"{column} AT TIME ZONE 'UTC'",
            )
            if has_current_timestamp_default:
                op.alter_column(
                    table,
                    column,
                    existing_type=sa.DateTime(),
                    server_default=sa.text("CURRENT_TIMESTAMP"),
                )
        return

    if bind.dialect.name == "sqlite":
        # See upgrade(): there is no SQLite storage change to reverse.
        return

    raise RuntimeError(
        "0717 Strategic Memory timestamp migration supports PostgreSQL and SQLite only"
    )

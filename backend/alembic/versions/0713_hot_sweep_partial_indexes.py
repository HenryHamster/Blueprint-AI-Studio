"""Add partial indexes for high-cadence bounded sweeps.

Revision ID: 0713
Revises: 0712
Create Date: 2026-07-30

Migration-safety note: this index-only revision adds no ``nullable=False``
column, so a ``server_default`` is not applicable.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0713"
down_revision = "0712"
branch_labels = None
depends_on = None

_INDEXES = (
    (
        "ix_task_queued_created_at",
        "task",
        ["created_at"],
        "status = 'QUEUED'",
    ),
    (
        "ix_task_completed_created_at",
        "task",
        ["created_at"],
        "status = 'COMPLETED'",
    ),
    (
        "ix_artifact_pull_request_created_at",
        "artifact",
        ["created_at"],
        "artifact_type = 'PULL_REQUEST'",
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for name, table, columns, predicate in _INDEXES:
            # A cancelled CREATE INDEX CONCURRENTLY can leave an INVALID
            # same-name index behind. IF NOT EXISTS would silently accept that
            # husk and let Alembic mark the migration complete. Inspect catalog
            # validity first, remove only an invalid retry artifact, and then
            # create without IF NOT EXISTS. A valid index means a prior attempt
            # finished the expensive DDL before failing later in this revision.
            is_valid = bind.execute(
                sa.text(
                    "SELECT indisvalid FROM pg_index "
                    "WHERE indexrelid = to_regclass(:index_name)"
                ),
                {"index_name": name},
            ).scalar_one_or_none()
            with op.get_context().autocommit_block():
                if is_valid is False:
                    op.execute(f"DROP INDEX CONCURRENTLY {name}")
                if is_valid is not True:
                    column_sql = ", ".join(columns)
                    op.execute(
                        f"CREATE INDEX CONCURRENTLY {name} "
                        f"ON {table} ({column_sql}) WHERE {predicate}"
                    )
        return

    for name, table, columns, predicate in _INDEXES:
        op.create_index(
            name,
            table,
            columns,
            sqlite_where=sa.text(predicate),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for name, _table, _columns, _predicate in reversed(_INDEXES):
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        return

    for name, table, _columns, _predicate in reversed(_INDEXES):
        op.drop_index(name, table_name=table)

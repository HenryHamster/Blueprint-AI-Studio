"""Add partial expression index on payloads[0]['type'] for active tasks.

Round 4 audit fix H5: orchestrator pipeline budget query SeqScans every 5s
on Task.payloads[0]['type'] filter. Add an expression index to support hot-
path filtering by task type for PROVISIONING/IN_PROGRESS rows.

Revision ID: 0067
Revises: 0066
Create Date: 2026-04-29 00:00:00.000000

"""
from alembic import op


revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite fallback — column-only index keeps metadata invariants happy
        # in tests; expression-on-JSON path filtering is Postgres-only.
        #
        # R5-W5 MED-4 caveat: this is a B-tree on a JSON-storing TEXT/BLOB
        # column that no test query references — wasted index space on
        # SQLite-based test runs. Kept anyway because:
        #   1. SQLite test DBs are in-memory and short-lived (no real cost)
        #   2. removing it would require either skipping the migration entry
        #      or adding a MIGRATION_ONLY_INDEXES opt-out to the
        #      test_model_indexes_covered_by_migrations invariant — both add
        #      machinery for a cosmetic gain
        #   3. real Postgres deploys hit the CONCURRENTLY branch below and
        #      get the proper expression-with-WHERE partial index
        op.create_index("ix_task_payloads_type_active", "task", ["payloads"])
        return
    # CONCURRENTLY cannot run inside a transaction block. Commit the
    # alembic migration's open transaction first.
    op.execute("COMMIT")
    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_payloads_type_active
        ON task ((payloads -> 0 ->> 'type'))
        WHERE status IN ('PROVISIONING', 'IN_PROGRESS')
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        op.drop_index("ix_task_payloads_type_active", table_name="task")
        return
    op.execute("COMMIT")
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_payloads_type_active")

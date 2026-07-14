"""Add previous_error_fingerprint to ix_task_auto_remediation_active.

The partial index on (project_id, created_at) WHERE auto_remediation = true
was missing previous_error_fingerprint, which is used as an equality filter
in the two auto_remediation.py hot-path queries:

1. Per-fingerprint cap check (auto_remediation.py:433-443):
   SELECT count(*) FROM task
   WHERE project_id = ? AND created_at >= ? AND status IN (?)
     AND auto_remediation = true AND previous_error_fingerprint = ?

2. Prior-completion lookup (auto_remediation.py:897-904):
   SELECT * FROM task
   WHERE project_id = ? AND status = 'COMPLETED' AND created_at >= ?
     AND auto_remediation = true AND previous_error_fingerprint = ?
   ORDER BY created_at DESC

Moving previous_error_fingerprint before created_at in the composite makes
the equality filter drive the index seek before the range scan, avoiding a
full scan of all auto_remediation rows for each fingerprint value.

Revision ID: 0488
Revises: 0487
Create Date: 2026-07-11 03:35:00.000000

"""

from alembic import op

revision = "0488"
down_revision = "0487"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_task_auto_remediation_active"
        )
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_task_auto_remediation_active "
            "ON task (project_id, previous_error_fingerprint, created_at) "
            "WHERE auto_remediation = true"
        )
    else:
        op.drop_index("ix_task_auto_remediation_active", table_name="task")
        op.create_index("ix_task_auto_remediation_active", "task",
            ["project_id", "previous_error_fingerprint", "created_at"],
            sqlite_where="auto_remediation = 1", if_not_exists=True)


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_task_auto_remediation_active"
        )
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_task_auto_remediation_active "
            "ON task (project_id, created_at) "
            "WHERE auto_remediation = true"
        )
    else:
        op.drop_index("ix_task_auto_remediation_active", table_name="task")
        op.create_index("ix_task_auto_remediation_active", "task",
            ["project_id", "created_at"],
            sqlite_where="auto_remediation = 1", if_not_exists=True)

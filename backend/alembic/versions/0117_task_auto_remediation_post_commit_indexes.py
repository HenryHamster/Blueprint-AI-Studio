"""Add partial indexes on task.auto_remediation and task.post_commit_pending.

ix_task_auto_remediation_active — serves auto_remediation.py queries that
  filter Task.auto_remediation.is_(True): (project_id, created_at) WHERE
  auto_remediation = true.

ix_task_post_commit_pending — serves services.py queries (lines 2233, 2252)
  that filter Task.post_commit_pending.is_(True): (project_id, status) WHERE
  post_commit_pending = true.

Revision ID: 0117
Revises: 0116
Create Date: 2026-05-23 00:00:00.000000

"""

from alembic import op

revision = "0117"
down_revision = "0116"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_auto_remediation_active "
            "ON task (project_id, created_at) WHERE auto_remediation = true"
        )
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_post_commit_pending "
            "ON task (project_id, status) WHERE post_commit_pending = true"
        )
    else:
        op.create_index(
            "ix_task_auto_remediation_active",
            "task",
            ["project_id", "created_at"],
            postgresql_where="auto_remediation = true",
        )
        op.create_index(
            "ix_task_post_commit_pending",
            "task",
            ["project_id", "status"],
            postgresql_where="post_commit_pending = true",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_auto_remediation_active")
        op.execute("COMMIT")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_post_commit_pending")
    else:
        op.drop_index("ix_task_auto_remediation_active", table_name="task")
        op.drop_index("ix_task_post_commit_pending", table_name="task")

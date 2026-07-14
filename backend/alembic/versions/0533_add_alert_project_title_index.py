"""Add composite index on alert(project_id, title) for hot dedupe filters.

Revision ID: 0533
Revises: 0532
Create Date: 2026-07-14

Alert.title is filtered in dedupe/existence WHERE clauses across many alert
call sites (drift_detection, pipeline_alert_scan, alert_dispatch,
hotfix_backmerge, branch_resolution, pipeline_health, ...). No existing index
covers title, so each check seq-scans after the project_id filter. Add a
composite (project_id, title) index.

Created CONCURRENTLY on Postgres so production rollouts don't block writes on
the ``alert`` table. CONCURRENTLY cannot run inside a transaction, so we issue
``COMMIT`` first to release Alembic's wrapping transaction, then issue the DDL
on the now-autocommit connection.

"""

from alembic import op

revision = "0533"
down_revision = "0532"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_alert_project_title "
            "ON alert (project_id, title)"
        )
    else:
        op.create_index(
            "ix_alert_project_title", "alert", ["project_id", "title"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_alert_project_title")
    else:
        op.drop_index("ix_alert_project_title", table_name="alert")

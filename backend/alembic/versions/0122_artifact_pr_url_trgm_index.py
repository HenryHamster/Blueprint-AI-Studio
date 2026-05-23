"""Add pg_trgm GIN index on artifact.pr_url for ILIKE query performance.

Revision ID: 0122
Revises: 0121
Create Date: 2026-05-23 00:00:00.000000

"""

from alembic import op

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_artifact_pr_url_trgm "
            "ON artifact USING gin (pr_url gin_trgm_ops)"
        )
    else:
        op.create_index(
            "ix_artifact_pr_url_trgm",
            "artifact",
            ["pr_url"],
            postgresql_using="gin",
            postgresql_ops={"pr_url": "gin_trgm_ops"},
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_artifact_pr_url_trgm")
    else:
        op.drop_index("ix_artifact_pr_url_trgm", table_name="artifact")

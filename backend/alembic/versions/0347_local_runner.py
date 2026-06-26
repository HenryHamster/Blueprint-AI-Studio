"""Add localrunner table for pull-based fleet runners.

Creates the ``localrunner`` table backing the pull-based local fleet runner:
operator hardware enrolls via POST /api/admin/projects/{id}/runners/register
(returning a one-time secret token; only its SHA256 hash is stored), polls
POST /api/runner/next-task for orchestrator-pinned work, and heartbeats via
POST /api/runner/heartbeat. The orchestrator pins QUEUED tasks to an active
registered runner before the warm-pool/GCP dispatch paths — no LOCAL_MODE flag.

project_id FKs to project with ON DELETE CASCADE so a runner cannot outlive its
owning project. token_hash is indexed for the auth lookup; (project_id, status)
is indexed for the orchestrator's per-cycle active-runner cache scan.

Revision ID: 0347
Revises: 0334
"""

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from sqlalchemy.dialects import postgresql


# Reserved revision id (linearized against production at merge time); chained
# from the current alembic head (0334) so the chain stays linear locally and
# in CI.
revision = "0347"
down_revision = "0334"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "localrunner",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("project_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=True),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_localrunner_project_id"),
        "localrunner",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_localrunner_token_hash"),
        "localrunner",
        ["token_hash"],
        unique=False,
    )
    op.create_index(
        "ix_localrunner_project_status",
        "localrunner",
        ["project_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_localrunner_project_status",
        table_name="localrunner",
    )
    op.drop_index(
        op.f("ix_localrunner_token_hash"),
        table_name="localrunner",
    )
    op.drop_index(
        op.f("ix_localrunner_project_id"),
        table_name="localrunner",
    )
    op.drop_table("localrunner")

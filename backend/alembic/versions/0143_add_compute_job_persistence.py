"""add_compute_job_persistence

Revision ID: 0143
Revises: 0142
Create Date: 2026-06-04 04:43:25.257184

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
import sqlmodel


# revision identifiers, used by Alembic.
revision = "0143"
down_revision = "0142"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Autogenerate also emitted legacy metadata drift (unrelated drops/type
    # churn for older tables). This migration intentionally keeps only the
    # additive ComputeJob table diff to avoid data loss outside this feature.
    op.create_table(
        "computejob",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("project_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("task_id", sqlmodel.sql.sqltypes.GUID(), nullable=True),
        sa.Column("backend", sa.String(length=64), nullable=False),
        sa.Column("external_compute_id", sa.String(length=255), nullable=True),
        sa.Column("machine_type", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(), server_default="QUEUED", nullable=False),
        sa.Column("terminal_reason", sa.Text(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("spend_cap_usd", sa.Float(), nullable=True),
        sa.Column("wall_clock_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "poll_attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("accrued_cost_usd", sa.Float(), nullable=True),
        sa.Column("final_cost_usd", sa.Float(), nullable=True),
        sa.Column("result_reference", sa.String(length=1024), nullable=True),
        sa.Column("artifact_id", sqlmodel.sql.sqltypes.GUID(), nullable=True),
        sa.Column(
            "polling_metadata",
            JSONB(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        # task_id / artifact_id detach (SET NULL) on parent delete: a compute
        # job is durable history that must outlive its originating task and any
        # task-owned artifact (artifacts CASCADE-delete with their task). This
        # keeps bulk task cleanup (e.g. reset_project_admin) from FK-violating.
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["artifact.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_computejob_artifact_id"),
        "computejob",
        ["artifact_id"],
        unique=False,
    )
    op.create_index(
        "ix_computejob_backend_external",
        "computejob",
        ["backend", "external_compute_id"],
        unique=False,
    )
    op.create_index(
        "ix_computejob_next_poll",
        "computejob",
        ["next_poll_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_computejob_project_id"),
        "computejob",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_computejob_project_status",
        "computejob",
        ["project_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_computejob_status_deadline",
        "computejob",
        ["status", "wall_clock_deadline"],
        unique=False,
    )
    op.create_index(
        op.f("ix_computejob_task_id"),
        "computejob",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        "ix_computejob_task_status",
        "computejob",
        ["task_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_computejob_task_status", table_name="computejob")
    op.drop_index(op.f("ix_computejob_task_id"), table_name="computejob")
    op.drop_index("ix_computejob_status_deadline", table_name="computejob")
    op.drop_index("ix_computejob_project_status", table_name="computejob")
    op.drop_index(op.f("ix_computejob_project_id"), table_name="computejob")
    op.drop_index("ix_computejob_next_poll", table_name="computejob")
    op.drop_index("ix_computejob_backend_external", table_name="computejob")
    op.drop_index(op.f("ix_computejob_artifact_id"), table_name="computejob")
    op.drop_table("computejob")

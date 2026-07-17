"""Add ProjectComputeImageBuild async tracking table.

Cloud Run's 60-minute request cap forbids inline synchronous ``docker build``.
This table drives the provision -> poll -> terminal -> reap lifecycle for
off-orchestrator image builds dispatched to an isolated builder VM, mirroring
the ``ComputeJob`` table's column/typing conventions.

Additive: brand-new table, no existing data to migrate, no existing model or
API shape changes.

Revision ID: 0628
Revises: 0627
Create Date: 2026-07-17 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "0631"
down_revision = "0630"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projectcomputeimagebuild",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("dep_hash", sa.String(255), nullable=False),
        sa.Column("registry_repo", sa.String(512), nullable=False),
        sa.Column("image_name", sa.String(255), nullable=False),
        sa.Column("builder_vm_id", sa.String(255), nullable=True),
        sa.Column("zone", sa.String(128), nullable=True),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_projectcomputeimagebuild_project_id_status",
        "projectcomputeimagebuild",
        ["project_id", "status"],
    )
    op.create_index(
        "ix_projectcomputeimagebuild_project_dep_hash",
        "projectcomputeimagebuild",
        ["project_id", "dep_hash"],
    )
    op.create_index(
        "ix_projectcomputeimagebuild_status",
        "projectcomputeimagebuild",
        ["status"],
    )
    op.create_index(
        "ix_projectcomputeimagebuild_deadline_at",
        "projectcomputeimagebuild",
        ["deadline_at"],
    )
    op.create_index(
        "ix_projectcomputeimagebuild_created_at",
        "projectcomputeimagebuild",
        ["created_at"],
    )
    op.create_index(
        "ix_projectcomputeimagebuild_project_id",
        "projectcomputeimagebuild",
        ["project_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_projectcomputeimagebuild_project_id",
        table_name="projectcomputeimagebuild",
    )
    op.drop_index(
        "ix_projectcomputeimagebuild_created_at",
        table_name="projectcomputeimagebuild",
    )
    op.drop_index(
        "ix_projectcomputeimagebuild_deadline_at",
        table_name="projectcomputeimagebuild",
    )
    op.drop_index(
        "ix_projectcomputeimagebuild_status",
        table_name="projectcomputeimagebuild",
    )
    op.drop_index(
        "ix_projectcomputeimagebuild_project_dep_hash",
        table_name="projectcomputeimagebuild",
    )
    op.drop_index(
        "ix_projectcomputeimagebuild_project_id_status",
        table_name="projectcomputeimagebuild",
    )
    op.drop_table("projectcomputeimagebuild")

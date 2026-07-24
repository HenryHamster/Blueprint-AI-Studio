"""Drop the unused projectcomputeimagebuild table.

The ProjectComputeImageBuild durable-build lane was dead: no production code
ever inserted a row, build_metrics/avg_build_seconds were always empty, and
the restart-recovery hooks were never passed at the one production call site.
Compute-image builds are dep_hash-idempotent — an existing image tag no-ops
the build — so an orchestrator restart self-heals without this scaffolding.

Revision ID: 0668
Revises: 0659
Create Date: 2026-07-22 00:00:00.000000

"""

from alembic import op

revision = "0668"
down_revision = "0659"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("projectcomputeimagebuild")


def downgrade() -> None:
    # The table was never written to in production, so downgrade recreates it
    # from the original 0633 definition (without the later additive columns like
    # cloud_build_id, image_published_at, and the TIMESTAMPTZ alignment). This is
    # a lossy downgrade by design — the table was dead code.
    import sqlalchemy as sa

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
        sa.Column("image_published_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
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
    op.create_foreign_key(
        None,
        "projectcomputeimagebuild",
        "project",
        ["project_id"],
        ["id"],
    )

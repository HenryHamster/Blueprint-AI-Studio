"""Add project_compute_image table (dep_hash -> digest persistence).

The compute-image baking pipeline (``compute_backends.build_project_compute_image``
-> ``compute_image_build.build_and_push``) content-hashes a project's declared
dependency files and bakes (or reuses) a digest-pinned compute image, returning a
``BuildResult`` (``pinned_ref``/``tag``/``dep_hash``/``built``). Nothing durably
recorded that result, so the built ``(dependency-hash -> digest)`` mapping was
thrown away. This table persists one row per ``(project_id, dep_hash)`` so a later
trigger can record and re-read what was built without re-running the pipeline.

Additive: brand-new table, no existing data to migrate, no existing model or API
shape changes. A UNIQUE constraint on ``(project_id, dep_hash)`` makes repeated
builds for the same dependency hash UPSERT the single row rather than accumulate.

NOTE: hand-written to match ``models.ProjectComputeImage`` — ``alembic`` is not
importable in the authoring environment, so ``--autogenerate`` could not be run.
Column types/nullability mirror the model exactly.

Revision ID: 0521
Revises: 0525
Create Date: 2026-07-12 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "0521"
down_revision = "0525"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projectcomputeimage",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("image_name", sa.String(), nullable=False),
        sa.Column("registry_repo", sa.String(), nullable=False),
        sa.Column("dep_hash", sa.String(), nullable=False),
        sa.Column("tag", sa.String(), nullable=False),
        sa.Column("pinned_ref", sa.String(), nullable=False),
        sa.Column("digest", sa.String(), nullable=True),
        sa.Column(
            "built", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "dep_hash", name="uq_projectcomputeimage_project_dep_hash"
        ),
    )
    op.create_index(
        "ix_projectcomputeimage_project_id",
        "projectcomputeimage",
        ["project_id"],
    )
    op.create_index(
        "ix_projectcomputeimage_dep_hash",
        "projectcomputeimage",
        ["dep_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_projectcomputeimage_dep_hash", table_name="projectcomputeimage")
    op.drop_index(
        "ix_projectcomputeimage_project_id", table_name="projectcomputeimage"
    )
    op.drop_table("projectcomputeimage")

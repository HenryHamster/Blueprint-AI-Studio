"""Add computejobfile table for individually-addressable viewable compute job files.

Each row records one viewable file (image or video) extracted from a compute
job's bundle.tar.gz, so it can be signed and rendered inline via the strategic
canvas without downloading the full tarball.

Revision ID: 0503
Revises: 0502
Create Date: 2026-07-12

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0503"
down_revision = "0502"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "computejobfile",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "compute_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("computejob.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("safe_relpath", sa.String(length=512), nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("storage_uri", sa.String(length=1024), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_uri"),
    )
    op.create_index(
        "ix_computejobfile_job_media",
        "computejobfile",
        ["compute_job_id", "media_type"],
    )
    op.create_index(
        "ix_computejobfile_storage_uri",
        "computejobfile",
        ["storage_uri"],
    )
    op.create_index(
        op.f("ix_computejobfile_compute_job_id"),
        "computejobfile",
        ["compute_job_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_computejobfile_compute_job_id"), table_name="computejobfile")
    op.drop_index("ix_computejobfile_storage_uri", table_name="computejobfile")
    op.drop_index("ix_computejobfile_job_media", table_name="computejobfile")
    op.drop_table("computejobfile")

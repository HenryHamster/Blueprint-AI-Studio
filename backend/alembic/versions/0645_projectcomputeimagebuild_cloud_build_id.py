"""Add cloud_build_id and cloud_build_staged_object to project_compute_image_build.

Revision ID: 0645
Revises: 0644
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0645"
down_revision = "0644"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "projectcomputeimagebuild",
        sa.Column("cloud_build_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "projectcomputeimagebuild",
        sa.Column("cloud_build_staged_object", sa.String(512), nullable=True),
    )


def downgrade():
    op.drop_column("projectcomputeimagebuild", "cloud_build_staged_object")
    op.drop_column("projectcomputeimagebuild", "cloud_build_id")

"""Add image_published_at to projectcomputeimagebuild.

Stamps the moment the built image was successfully pushed to the container
registry. Enables build-duration telemetry (build_completed - image_published_at)
and lets the reaper distinguish images that were published from builds that
completed with a non-push error.

Column is nullable: NULL means the build has not yet published (or failed
before publishing). Written by the builder VM on successful image push.

Type is sa.DateTime(timezone=True) (TIMESTAMPTZ) to match the NaiveUTCDateTime
model column, consistent with the timezone convention enforced by
test_migration_column_types_match_models.

Revision ID: 0640
Revises: 0639
Create Date: 2026-07-20
"""

from alembic import op
import sqlalchemy as sa

revision = "0641"
down_revision = "0640"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projectcomputeimagebuild",
        sa.Column("image_published_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projectcomputeimagebuild", "image_published_at")

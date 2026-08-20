"""Add the candidate_sha column to ReleaseVersion.

The candidate-authorship hard gate previously resolved the candidate commit
for a qualification attempt via ``version.last_qualification["candidate_sha"]``
(mutable JSONB written by many unrelated code paths). ``candidate_sha`` is a
dedicated, nullable column stamped by ``run_qualification`` with the candidate
that run actually evaluates, so the authorship gate's range can no longer be
silently substituted by an unrelated ``last_qualification`` write.
Nullable and purely additive — pre-existing rows backfill NULL.

Per CLAUDE.md this chains from the latest head (0760) and
``EXPECTED_SCHEMA_REVISION`` is bumped to 0761 to match.

Revision ID: 0761
Revises: 0760
Create Date: 2026-08-19

"""

from alembic import op
import sqlalchemy as sa


revision = "0761"
down_revision = "0760"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "releaseversion",
        sa.Column(
            "candidate_sha",
            sa.String(length=40),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("releaseversion", "candidate_sha")

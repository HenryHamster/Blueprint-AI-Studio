"""Add post-promotion serving-confirmation schema.

Today ``ReleaseVersion.status`` flips to "promoted" the instant its candidate
merges, with no record of whether the deploy then stabilized in production.
This migration adds the columns needed to track that:

``releaseversion``:
- ``serving_status``           — NULL (not-yet-promoted / NA) | "unconfirmed"
                                  | "serving_confirmed" | "deploy_failed"
- ``served_sha``               — production serving SHA captured on confirmation
- ``served_at``                — when serving was confirmed
- ``deploy_confirm_deadline``  — bounded confirmation-window deadline (stamped at promotion)
- ``deploy_confirm_alerted_at``— dedup anchor for the deploy-failed operator alert
- ``deploy_failure_reason``    — short code for why confirmation failed

``qualificationverdict`` (immutable verdict history):
- ``post_promotion_outcome``     — "serving_confirmed" | "deploy_failed" | "unconfirmed"
- ``post_promotion_outcome_at``  — when that outcome was recorded

All columns are nullable with no backfill, so the migration is purely additive
and safe against the live schema.  Chains from head 0151;
``EXPECTED_SCHEMA_REVISION`` is bumped to 0152 to match.  Reversible.

Revision ID: 0152
Revises: 0151
Create Date: 2026-06-05 16:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0152"
down_revision = "0151"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "releaseversion",
        sa.Column("serving_status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "releaseversion",
        sa.Column("served_sha", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "releaseversion",
        sa.Column("served_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "releaseversion",
        sa.Column("deploy_confirm_deadline", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "releaseversion",
        sa.Column("deploy_confirm_alerted_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "releaseversion",
        sa.Column("deploy_failure_reason", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column("post_promotion_outcome", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column("post_promotion_outcome_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("qualificationverdict", "post_promotion_outcome_at")
    op.drop_column("qualificationverdict", "post_promotion_outcome")
    op.drop_column("releaseversion", "deploy_failure_reason")
    op.drop_column("releaseversion", "deploy_confirm_alerted_at")
    op.drop_column("releaseversion", "deploy_confirm_deadline")
    op.drop_column("releaseversion", "served_at")
    op.drop_column("releaseversion", "served_sha")
    op.drop_column("releaseversion", "serving_status")

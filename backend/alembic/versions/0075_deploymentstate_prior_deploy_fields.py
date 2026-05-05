"""Add prior_deploy_sha and prior_deployed_at to DeploymentState.

These fields track the previous deployment so investigation payloads can
include a RecentChangesContext with the commit range between deploys
(prior_deploy_sha..current_sha) without requiring a live git checkout on
the investigation VM.

Revision ID: 0075
Revises: 0074
Create Date: 2026-05-05 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deploymentstate",
        sa.Column("prior_deploy_sha", sa.String(), nullable=True),
    )
    op.add_column(
        "deploymentstate",
        sa.Column(
            "prior_deployed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("deploymentstate", "prior_deployed_at")
    op.drop_column("deploymentstate", "prior_deploy_sha")

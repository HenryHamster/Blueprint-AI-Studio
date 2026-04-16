"""add_deploymentstate_jsonb_columns_without_migration

Adds three JSONB columns to deploymentstate that were added to the
SQLModel but never had corresponding migrations, causing Cloud Run
startup failures with UndefinedColumnError:

  - persistent_anomaly_state (used by anomaly_detection)
  - direction_streak_state
  - pipeline_events_state

All three are nullable with no default — the consumers treat None as
"no state yet" and initialize lazily on first write.

Revision ID: 0034
Revises: 0033
Create Date: 2026-04-16 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '0034'
down_revision = '0033'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'deploymentstate',
        sa.Column(
            'persistent_anomaly_state',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        'deploymentstate',
        sa.Column(
            'direction_streak_state',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        'deploymentstate',
        sa.Column(
            'pipeline_events_state',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('deploymentstate', 'pipeline_events_state')
    op.drop_column('deploymentstate', 'direction_streak_state')
    op.drop_column('deploymentstate', 'persistent_anomaly_state')

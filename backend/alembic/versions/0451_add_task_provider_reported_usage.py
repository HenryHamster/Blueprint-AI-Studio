"""add task.provider_reported_usage JSONB column

Provider-reported token usage from the agent's independent accumulator
(Component B of cost-telemetry reconciliation). Persisted from heartbeat
payload to enable cross-checking agent api_call_summary against raw
provider response.usage values.

Revision ID: 0451
Revises: 0450
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0451"
down_revision = "0450"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task",
        sa.Column("provider_reported_usage", JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task", "provider_reported_usage")

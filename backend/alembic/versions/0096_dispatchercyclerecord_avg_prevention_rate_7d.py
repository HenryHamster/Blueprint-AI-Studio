"""Add avg_prevention_rate_7d to DispatcherCycleRecord.

Persists the unweighted 7-day average defense prevention rate at the time
of each dispatcher cycle. NULL for historical rows and cycles where no
eligible defenses existed. Enables trend analysis of whether cross-layer
defenses are holding over time.

Revision ID: 0096
Revises: 0095
Create Date: 2026-05-09 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dispatchercyclerecord",
        sa.Column(
            "avg_prevention_rate_7d",
            sa.Float(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("dispatchercyclerecord", "avg_prevention_rate_7d")

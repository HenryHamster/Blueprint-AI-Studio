"""localrunner trust telemetry columns

Adds two first-class trust-telemetry columns to ``localrunner`` so the operator
console can distinguish a runner that is merely *connected* from one that is
*connected but failing* (the false-green a heartbeat alone cannot detect):

  * ``last_successful_job_at`` — last time the runner reported a job succeeding.
  * ``consecutive_job_failures`` — job failures since the last success (default 0).

The other two heartbeat trust fields (``disk_free_bytes``,
``gpu_utilization_pct``) are display-only telemetry and live in the existing
``capabilities`` JSONB alongside the other heartbeat-reported hardware fields
(``has_gpu``/``gpu_count``/``gpu_names``), so no column is added for them.

Revision ID: 0366
Revises: 0365
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0366"
down_revision = "0365"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "localrunner",
        sa.Column("last_successful_job_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "localrunner",
        sa.Column(
            "consecutive_job_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("localrunner", "consecutive_job_failures")
    op.drop_column("localrunner", "last_successful_job_at")

"""Drop five confirmed-dead ORM columns.

These columns were confirmed dead (written but never read, or never accessed):
  - task.infra_wait_until        (0 reads, 0 writes)
  - task.spawn_failed            (0 reads; sole write site removed)
  - task.last_remediation_at     (0 reads, 0 writes)
  - project.regression_signal_state (0 reads, 0 writes)
  - deploymentstate.prior_deployed_at (0 reads; sole write site removed)

Dropping them removes schema noise and shrinks the data model.

Revision ID: 0300
Revises: 0289
Create Date: 2026-06-16 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0300"
down_revision = "0289"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # task table: three dead columns
    op.drop_column("task", "infra_wait_until")
    op.drop_column("task", "spawn_failed")
    op.drop_column("task", "last_remediation_at")

    # project table: one dead column
    op.drop_column("project", "regression_signal_state")

    # deploymentstate table: one dead column
    op.drop_column("deploymentstate", "prior_deployed_at")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.add_column(
        "deploymentstate",
        sa.Column("prior_deployed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "project",
        sa.Column(
            "regression_signal_state",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "task",
        sa.Column("last_remediation_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "task",
        sa.Column(
            "spawn_failed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "task",
        sa.Column("infra_wait_until", sa.DateTime(), nullable=True),
    )

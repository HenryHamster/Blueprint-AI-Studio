"""Add the goal-authoring compliance ledger to ReleaseVersion.

``ReleaseVersion.goal`` opens NULL by design — ``open_version`` leaves it unset
and the strategic agent is asked to author it via the ``new_version_goal``
output field.  That ask had no owner: when the agent omitted the field,
``_apply_new_version_goal`` returned on its first guard and nothing anywhere
recorded that a required output had been requested and had not arrived, so the
omission was indistinguishable from "no goal was ever asked for".

``goal_omission_cycles`` counts consecutive COMPLETED strategic cycles that
were asked for the goal and did not deliver it; ``goal_omission_last_cycle`` is
the dedup anchor (strategic cycle id, or task id for legacy cycles) that last
incremented it, so a single cycle reaching the apply path more than once cannot
double-count.

Purely additive.  ``goal_omission_cycles`` is NOT NULL with a ``server_default``
of 0 so pre-existing rows backfill safely; ``goal_omission_last_cycle`` is
nullable.

Per CLAUDE.md this chains from the latest head (0763) and
``EXPECTED_SCHEMA_REVISION`` is bumped to 0764 to match.

Revision ID: 0764
Revises: 0763
Create Date: 2026-08-26

"""

from alembic import op
import sqlalchemy as sa


revision = "0764"
down_revision = "0763"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "releaseversion",
        sa.Column(
            "goal_omission_cycles",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "releaseversion",
        sa.Column(
            "goal_omission_last_cycle",
            sa.String(length=64),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("releaseversion", "goal_omission_last_cycle")
    op.drop_column("releaseversion", "goal_omission_cycles")

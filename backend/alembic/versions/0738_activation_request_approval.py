"""Add approved-activation control columns to ReleaseVersion.

The in-product promotion-activation approval (admin approve-activation lever)
stamps two durable columns on the promoted ``ReleaseVersion`` row:

- ``activation_requested_at`` — when an operator approved activation of this
  promoted version for the trusted control-plane host to execute; NULL while
  awaiting approval.
- ``activation_requested_by`` — the operator identity that approved it,
  mirroring ``goal_editor_info`` provenance conventions.

Together they carry the "approved-but-not-yet-executed" state on the version
whose immutable ``promoted_sha`` the approval binds to, so the receipt path can
reject any reported revision that differs or a receipt once a newer promotion
supersedes the approved version.  Both columns are nullable and purely additive
— pre-existing promoted rows backfill NULL and the legacy CLI activation path
(no approval) is unchanged.  Immutable audit provenance for an approval is
additionally recorded in ``last_qualification["activation_approval"]``, so this
schema change is limited to the two live-workflow columns.

Per CLAUDE.md this chains from the latest head (0737) and
``EXPECTED_SCHEMA_REVISION`` is bumped to 0738 to match.

Revision ID: 0738
Revises: 0737
Create Date: 2026-08-08

"""

from alembic import op
import sqlalchemy as sa


revision = "0738"
down_revision = "0737"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "releaseversion",
        sa.Column(
            "activation_requested_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "releaseversion",
        sa.Column(
            "activation_requested_by",
            sa.String(length=255),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("releaseversion", "activation_requested_by")
    op.drop_column("releaseversion", "activation_requested_at")

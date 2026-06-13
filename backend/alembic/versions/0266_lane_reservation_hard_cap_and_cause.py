"""Lane reservation hard ceiling + expiry-cause telemetry.

Adds the durable anchor and counters that let a lane-scoped warm-pool
reservation survive a long spanning review phase while staying pool-capacity
safe:

* ``poolvm.reservation_hard_expires_at`` — the HARD absolute ceiling on a
  reservation's total lifetime, stamped ONCE at reservation creation. The
  liveness-aware maintainer sweep re-stamps the SOFT ``reservation_expires_at``
  forward while the owning lane is still awaiting its fix, but can never extend
  past this anchor, so a stuck/zombie lane cannot pin a warm VM indefinitely.
* ``improvementlane.reservation_{claimed_by_fix,expired_before_fix,force_expired}_count``
  — expiry-cause telemetry so a low ``warm_resume_fraction`` is diagnosable on
  the lanes analytics surface.

Revision ID: 0266
Revises: 0260
"""

from alembic import op
import sqlalchemy as sa


# Reserved revision id for this task (linearized against production at merge
# time). Chained from the actual current head 0260 to keep the chain a single
# linear sequence: the reservation note named down_revision 0235, but 0236-0260
# merged after the reservation was issued, so chaining from 0235 would branch
# the chain (mirrors the same deviation documented in 0260, which was reserved
# against 0235 but chained from the real head 0238).
revision = "0266"
down_revision = "0260"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Naive UTC datetime, nullable — mirrors the existing reservation_expires_at
    # column (0233). No backfill: existing reservations have no hard anchor and
    # the sweep treats a NULL anchor as "no extension" (legacy clear-on-soft-
    # expiry behavior), so they drain safely without a data migration.
    op.add_column(
        "poolvm",
        sa.Column(
            "reservation_hard_expires_at",
            sa.DateTime(),
            nullable=True,
        ),
    )
    # Expiry-cause counters. NOT NULL with a server_default of 0 so existing
    # lane rows backfill to 0 without a separate data migration.
    op.add_column(
        "improvementlane",
        sa.Column(
            "reservation_claimed_by_fix_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "improvementlane",
        sa.Column(
            "reservation_expired_before_fix_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "improvementlane",
        sa.Column(
            "reservation_force_expired_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("improvementlane", "reservation_force_expired_count")
    op.drop_column("improvementlane", "reservation_expired_before_fix_count")
    op.drop_column("improvementlane", "reservation_claimed_by_fix_count")
    op.drop_column("poolvm", "reservation_hard_expires_at")

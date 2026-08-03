"""Remove descendant discovery admission accounting.

Revision ID: 0722
Revises: 0721
Create Date: 2026-08-02

Discovery cycles remain as provenance and review cohorts. Ordinary descendants
are governed by the scheduler, retry/review policy, and spend controls rather
than a second task-count execution gate.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0722"
down_revision = "0721"
branch_labels = None
depends_on = None


_CREDIT_COLUMNS = (
    "task_credit_limit",
    "attempt_credit_limit",
    "concurrency_limit",
    "reserved_task_credits",
    "consumed_task_credits",
    "reserved_attempt_credits",
    "consumed_attempt_credits",
)


def upgrade() -> None:
    op.drop_table("discoveryadmission")
    for constraint_name in (
        "chk_discoverycycle_task_credit_limit",
        "chk_discoverycycle_attempt_credit_limit",
        "chk_discoverycycle_concurrency_limit",
        "chk_discoverycycle_credit_counts",
    ):
        op.drop_constraint(constraint_name, "discoverycycle", type_="check")
    for column_name in _CREDIT_COLUMNS:
        op.drop_column("discoverycycle", column_name)

    # Review verdicts are now controller actions. Translate historical rows
    # before installing the narrower constraint so upgrades cannot strand a
    # database containing completed legacy cycles.
    op.drop_constraint(
        "chk_discoverycycle_verdict",
        "discoverycycle",
        type_="check",
    )
    # Legacy park/stop verdicts were recommendations only: the old controller
    # did not mutate direction authority. Preserve the recommendation for
    # context but require an explicit new-controller resolution instead of
    # silently reinterpreting advisory history as a binding disposition.
    op.execute(
        """
        UPDATE discoverycycle
        SET review_result = COALESCE(review_result, '{}'::jsonb) || jsonb_build_object(
                'status', 'LEGACY_DISPOSITION_REQUIRES_RESOLUTION',
                'legacy_advisory_verdict', verdict
            ),
            verdict = 'ask_operator'
        WHERE verdict IN ('park', 'stop')
        """
    )
    # An old ``inconclusive`` result made no binding decision. Requeue its
    # settled evidence for the new one-pass controller instead of fabricating
    # an ``advance`` or requiring a manual task spawn after deployment.
    op.execute(
        """
        UPDATE discoverycycle
        SET verdict = NULL,
            state = 'review_pending',
            review_task_id = NULL,
            review_result = jsonb_build_object(
                'status', 'LEGACY_INCONCLUSIVE_REQUEUED'
            )
        WHERE verdict = 'inconclusive'
          AND state IN ('reviewed','review_blocked','review_pending','reviewing')
          AND NOT EXISTS (
              SELECT 1
              FROM discoverycycle AS newer
              WHERE newer.project_id = discoverycycle.project_id
                AND newer.direction_key = discoverycycle.direction_key
                AND newer.generation > discoverycycle.generation
          )
        """
    )
    op.execute(
        """
        UPDATE discoverycycle
        SET verdict = CASE
            WHEN verdict IN ('renew','pivot','expand','branch','fallback_pending')
                THEN 'advance'
            -- A historical inconclusive predecessor with a later cycle already
            -- advanced in practice. Keep it settled; reopening it would collide
            -- with the unique live successor for the same direction.
            WHEN verdict = 'inconclusive' AND EXISTS (
                SELECT 1
                FROM discoverycycle AS newer
                WHERE newer.project_id = discoverycycle.project_id
                  AND newer.direction_key = discoverycycle.direction_key
                  AND newer.generation > discoverycycle.generation
            ) THEN 'advance'
            WHEN verdict = 'inconclusive' THEN 'ask_operator'
            ELSE verdict
        END
        WHERE verdict IS NOT NULL
        """
    )
    op.create_check_constraint(
        "chk_discoverycycle_verdict",
        "discoverycycle",
        "verdict IS NULL OR verdict IN "
        "('advance','park','stop','ask_operator')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "chk_discoverycycle_verdict",
        "discoverycycle",
        type_="check",
    )
    op.execute(
        """
        UPDATE discoverycycle
        SET verdict = 'renew'
        WHERE verdict = 'advance'
        """
    )
    op.create_check_constraint(
        "chk_discoverycycle_verdict",
        "discoverycycle",
        "verdict IS NULL OR verdict IN "
        "('renew','pivot','expand','branch','fallback_pending','park','stop',"
        "'ask_operator','inconclusive')",
    )

    op.add_column(
        "discoverycycle",
        sa.Column("task_credit_limit", sa.Integer(), nullable=True),
    )
    op.add_column(
        "discoverycycle",
        sa.Column("attempt_credit_limit", sa.Integer(), nullable=True),
    )
    op.add_column(
        "discoverycycle",
        sa.Column("concurrency_limit", sa.Integer(), nullable=True),
    )
    for column_name in (
        "reserved_task_credits",
        "consumed_task_credits",
        "reserved_attempt_credits",
        "consumed_attempt_credits",
    ):
        op.add_column(
            "discoverycycle",
            sa.Column(
                column_name,
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
    op.create_check_constraint(
        "chk_discoverycycle_task_credit_limit",
        "discoverycycle",
        "task_credit_limit IS NULL OR task_credit_limit > 0",
    )
    op.create_check_constraint(
        "chk_discoverycycle_attempt_credit_limit",
        "discoverycycle",
        "attempt_credit_limit IS NULL OR attempt_credit_limit > 0",
    )
    op.create_check_constraint(
        "chk_discoverycycle_concurrency_limit",
        "discoverycycle",
        "concurrency_limit IS NULL OR concurrency_limit > 0",
    )
    op.create_check_constraint(
        "chk_discoverycycle_credit_counts",
        "discoverycycle",
        "reserved_task_credits >= 0 AND consumed_task_credits >= 0 "
        "AND reserved_attempt_credits >= 0 AND consumed_attempt_credits >= 0",
    )

    op.create_table(
        "discoveryadmission",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cycle_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("admission_key", sa.String(length=512), nullable=False),
        sa.Column("admission_kind", sa.String(length=24), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "spawn_request_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "units",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "state",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'reserved'"),
        ),
        sa.Column(
            "admission_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "admission_kind IN ('logical_task','attempt')",
            name="chk_discoveryadmission_kind",
        ),
        sa.CheckConstraint(
            "state IN ('reserved','consumed','released')",
            name="chk_discoveryadmission_state",
        ),
        sa.CheckConstraint("units > 0", name="chk_discoveryadmission_units"),
        sa.CheckConstraint(
            "length(admission_key) > 0",
            name="chk_discoveryadmission_key_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["cycle_id"],
            ["discoverycycle.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["spawn_request_id"],
            ["strategicspawnrequest.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cycle_id",
            "admission_key",
            name="uq_discoveryadmission_cycle_key",
        ),
    )
    op.create_index(
        "ix_discoveryadmission_cycle_state_created",
        "discoveryadmission",
        ["cycle_id", "state", "created_at"],
    )
    op.create_index(
        "ix_discoveryadmission_task_id",
        "discoveryadmission",
        ["task_id"],
    )
    op.create_index(
        "ix_discoveryadmission_spawn_request_id",
        "discoveryadmission",
        ["spawn_request_id"],
    )

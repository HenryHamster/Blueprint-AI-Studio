"""Persist durable release qualification intents.

Revision ID: 0768
Revises: 0767
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0768"
down_revision = "0767"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "releasequalificationintent",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("release_version_id", sa.Uuid(), nullable=False),
        sa.Column("source_task_id", sa.Uuid(), nullable=False),
        sa.Column(
            "authority_kind",
            sa.String(length=32),
            server_default="resident_explicit",
            nullable=False,
        ),
        sa.Column("authority_key", sa.String(length=200), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("specification_hash", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "requested_probes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "expected_phase_changed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("claim_id", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','attempting','applied','superseded','failed')",
            name="ck_releasequalificationintent_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_releasequalificationintent_attempt_count",
        ),
        sa.CheckConstraint(
            "length(specification_hash) = 64",
            name="ck_releasequalificationintent_specification_hash",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["release_version_id"],
            ["releaseversion.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_releasequalificationintent_idempotency_key",
        ),
    )
    op.create_index(
        "ix_releasequalificationintent_due",
        "releasequalificationintent",
        ["status", "next_attempt_at", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_releasequalificationintent_project_status",
        "releasequalificationintent",
        ["project_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_releasequalificationintent_project_status",
        table_name="releasequalificationintent",
    )
    op.drop_index(
        "ix_releasequalificationintent_due",
        table_name="releasequalificationintent",
    )
    op.drop_table("releasequalificationintent")

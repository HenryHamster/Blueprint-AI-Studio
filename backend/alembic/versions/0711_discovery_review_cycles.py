"""Add durable discovery review cycles and evidence lineage.

Revision ID: 0711
Revises: 0710
Create Date: 2026-07-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0711"
down_revision = "0710"
branch_labels = None
depends_on = None


_LIVE_DISCOVERY_STATE_SQL = (
    "state IN ('open','admission_closed','review_pending','reviewing',"
    "'review_blocked')"
)


def upgrade() -> None:
    op.create_table(
        "discoverycycle",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("direction_key", sa.String(length=128), nullable=False),
        sa.Column(
            "generation", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("outcome_kind", sa.String(length=24), nullable=False),
        sa.Column(
            "phase",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'probe'"),
        ),
        sa.Column(
            "state",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column(
            "intent_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "bet_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("mechanism_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("candidate_sha", sa.String(length=64), nullable=True),
        sa.Column(
            "source_research_task_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("review_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_credit_limit", sa.Integer(), nullable=True),
        sa.Column("attempt_credit_limit", sa.Integer(), nullable=True),
        sa.Column("concurrency_limit", sa.Integer(), nullable=True),
        sa.Column(
            "reserved_task_credits",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "consumed_task_credits",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "reserved_attempt_credits",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "consumed_attempt_credits",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verdict", sa.String(length=32), nullable=True),
        sa.Column(
            "review_result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "revision", sa.Integer(), nullable=False, server_default=sa.text("0")
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
            "outcome_kind IN ('empirical','experiential','mixed')",
            name="chk_discoverycycle_outcome_kind",
        ),
        sa.CheckConstraint(
            "phase IN ('probe','demonstrate','productize','release')",
            name="chk_discoverycycle_phase",
        ),
        sa.CheckConstraint(
            "state IN ('open','admission_closed','review_pending','reviewing',"
            "'review_blocked','reviewed','superseded','cancelled')",
            name="chk_discoverycycle_state",
        ),
        sa.CheckConstraint(
            "verdict IS NULL OR verdict IN "
            "('renew','pivot','expand','branch','fallback_pending','park','stop',"
            "'ask_operator','inconclusive')",
            name="chk_discoverycycle_verdict",
        ),
        sa.CheckConstraint(
            "generation >= 1", name="chk_discoverycycle_generation"
        ),
        sa.CheckConstraint("revision >= 0", name="chk_discoverycycle_revision"),
        sa.CheckConstraint(
            "task_credit_limit IS NULL OR task_credit_limit > 0",
            name="chk_discoverycycle_task_credit_limit",
        ),
        sa.CheckConstraint(
            "attempt_credit_limit IS NULL OR attempt_credit_limit > 0",
            name="chk_discoverycycle_attempt_credit_limit",
        ),
        sa.CheckConstraint(
            "concurrency_limit IS NULL OR concurrency_limit > 0",
            name="chk_discoverycycle_concurrency_limit",
        ),
        sa.CheckConstraint(
            "reserved_task_credits >= 0 AND consumed_task_credits >= 0 "
            "AND reserved_attempt_credits >= 0 AND consumed_attempt_credits >= 0",
            name="chk_discoverycycle_credit_counts",
        ),
        sa.CheckConstraint(
            "mechanism_fingerprint IS NULL OR length(mechanism_fingerprint) = 64",
            name="chk_discoverycycle_mechanism_fingerprint",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_research_task_id"], ["task.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["review_task_id"], ["task.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "direction_key",
            "generation",
            name="uq_discoverycycle_project_direction_generation",
        ),
    )
    op.create_index(
        "ux_discoverycycle_live_direction",
        "discoverycycle",
        ["project_id", "direction_key"],
        unique=True,
        postgresql_where=sa.text(_LIVE_DISCOVERY_STATE_SQL),
    )
    op.create_index(
        "ix_discoverycycle_state_deadline",
        "discoverycycle",
        ["state", "deadline_at"],
    )
    op.create_index(
        "ix_discoverycycle_source_research_task_id",
        "discoverycycle",
        ["source_research_task_id"],
    )
    op.create_index(
        "ix_discoverycycle_review_task_id",
        "discoverycycle",
        ["review_task_id"],
    )

    op.create_table(
        "discoveryevidenceitem",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cycle_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("semantic_role", sa.String(length=32), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=255), nullable=True),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("head_sha", sa.String(length=64), nullable=True),
        sa.Column("mechanism_fingerprint", sa.String(length=64), nullable=True),
        sa.Column(
            "evidence_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("dedupe_key", sa.String(length=512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "length(checksum) = 64",
            name="chk_discoveryevidenceitem_checksum",
        ),
        sa.CheckConstraint(
            "mechanism_fingerprint IS NULL OR length(mechanism_fingerprint) = 64",
            name="chk_discoveryevidenceitem_mechanism_fingerprint",
        ),
        sa.ForeignKeyConstraint(
            ["cycle_id"], ["discoverycycle.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cycle_id",
            "dedupe_key",
            name="uq_discoveryevidenceitem_cycle_dedupe",
        ),
    )
    op.create_index(
        "ix_discoveryevidenceitem_cycle_role_created",
        "discoveryevidenceitem",
        ["cycle_id", "semantic_role", "created_at"],
    )
    op.create_index(
        "ix_discoveryevidenceitem_source",
        "discoveryevidenceitem",
        ["source_kind", "source_id"],
    )

    op.add_column(
        "task",
        sa.Column(
            "discovery_cycle_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_task_discovery_cycle_id",
        "task",
        "discoverycycle",
        ["discovery_cycle_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "strategicspawnrequest",
        sa.Column(
            "discovery_cycle_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_strategicspawnrequest_discovery_cycle_id",
        "strategicspawnrequest",
        "discoverycycle",
        ["discovery_cycle_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_task_discovery_cycle_id",
        "task",
        ["discovery_cycle_id"],
    )
    op.create_index(
        "ix_strategicspawnrequest_discovery_cycle_id",
        "strategicspawnrequest",
        ["discovery_cycle_id"],
    )


def downgrade() -> None:
    for table_name in ("strategicspawnrequest", "task"):
        op.drop_index(
            f"ix_{table_name}_discovery_cycle_id",
            table_name=table_name,
        )
        op.drop_constraint(
            f"fk_{table_name}_discovery_cycle_id",
            table_name,
            type_="foreignkey",
        )
        op.drop_column(table_name, "discovery_cycle_id")

    op.drop_index(
        "ix_discoveryevidenceitem_source",
        table_name="discoveryevidenceitem",
    )
    op.drop_index(
        "ix_discoveryevidenceitem_cycle_role_created",
        table_name="discoveryevidenceitem",
    )
    op.drop_table("discoveryevidenceitem")

    op.drop_index(
        "ix_discoverycycle_review_task_id",
        table_name="discoverycycle",
    )
    op.drop_index(
        "ix_discoverycycle_source_research_task_id",
        table_name="discoverycycle",
    )
    op.drop_index(
        "ix_discoverycycle_state_deadline",
        table_name="discoverycycle",
    )
    op.drop_index(
        "ux_discoverycycle_live_direction",
        table_name="discoverycycle",
    )
    op.drop_table("discoverycycle")

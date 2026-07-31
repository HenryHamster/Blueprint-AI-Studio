"""Persist fenced, canonical Strategic context envelopes.

Revision ID: 0715
Revises: 0714
Create Date: 2026-07-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0715"
down_revision = "0714"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategicmemoryprojectstate",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column(
            "context_authority_generation",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("privacy_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "context_authority_generation >= 0",
            name="ck_smem_state_ctx_gen",
        ),
        sa.CheckConstraint(
            "privacy_generation >= 0",
            name="ck_smem_state_priv_gen",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id"),
    )
    op.create_table(
        "strategiccontextenvelopesnapshot",
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("worker_generation", sa.Integer(), nullable=False),
        sa.Column("worker_incarnation", sa.Uuid(), nullable=False),
        sa.Column("input_revision", sa.BigInteger(), nullable=False),
        sa.Column("transcript_sequence", sa.BigInteger(), nullable=False),
        sa.Column("ledger_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("problem_model_revision", sa.String(length=128), nullable=False, server_default="0"),
        sa.Column("decision_authority_revision", sa.String(length=128), nullable=False, server_default="0"),
        sa.Column("context_authority_generation", sa.Integer(), nullable=False),
        sa.Column("privacy_generation", sa.Integer(), nullable=False),
        sa.Column("canonical_envelope", sa.LargeBinary(), nullable=False),
        sa.Column("canonical_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "retrieval_trace",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("worker_generation >= 0", name="ck_sctx_worker_gen"),
        sa.CheckConstraint("input_revision >= 0", name="ck_sctx_input_rev"),
        sa.CheckConstraint("transcript_sequence >= 0", name="ck_sctx_transcript_seq"),
        sa.CheckConstraint("ledger_revision >= 0", name="ck_sctx_ledger_rev"),
        sa.CheckConstraint("context_authority_generation >= 0", name="ck_sctx_context_gen"),
        sa.CheckConstraint("privacy_generation >= 0", name="ck_sctx_privacy_gen"),
        sa.CheckConstraint("length(canonical_sha256) = 64", name="ck_sctx_sha256_len"),
        sa.CheckConstraint("expires_at > created_at", name="ck_sctx_expiry"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("snapshot_id"),
        sa.UniqueConstraint(
            "project_id", "session_id", "claim_id", "worker_generation",
            "worker_incarnation", "input_revision", "transcript_sequence",
            "ledger_revision", "problem_model_revision", "decision_authority_revision",
            "context_authority_generation", "privacy_generation",
            name="uq_sctx_input_fence",
        ),
    )
    op.create_index("ix_sctx_project_expires", "strategiccontextenvelopesnapshot", ["project_id", "expires_at"])
    op.create_index("ix_sctx_project_session_seq", "strategiccontextenvelopesnapshot", ["project_id", "session_id", "transcript_sequence"])
    op.create_index("ix_sctx_project_claim", "strategiccontextenvelopesnapshot", ["project_id", "claim_id"])


def downgrade() -> None:
    op.drop_index("ix_sctx_project_claim", table_name="strategiccontextenvelopesnapshot")
    op.drop_index("ix_sctx_project_session_seq", table_name="strategiccontextenvelopesnapshot")
    op.drop_index("ix_sctx_project_expires", table_name="strategiccontextenvelopesnapshot")
    op.drop_table("strategiccontextenvelopesnapshot")
    op.drop_table("strategicmemoryprojectstate")

"""Add the minimal append-only Strategic Memory semantic ledger.

Revision ID: 0717
Revises: 0716
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0717"
down_revision = "0716"
branch_labels = None
depends_on = None


_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
_TEXT_ARRAY = postgresql.ARRAY(sa.Text()).with_variant(sa.JSON(), "sqlite")
_APPEND_ONLY_TABLES = (
    "strategicmemoryitemversion",
    "strategicmemoryitemsource",
    "strategicmemorytransition",
    "strategicmemorycorrectionbarrier",
    "strategicmemorycorrectionbarrierevent",
    "strategicmemoryedge",
    "strategicmemoryedgesource",
)


def _create_append_only_guards() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION strategic_memory_ledger_append_only() RETURNS trigger AS $$
            BEGIN
                -- Cascades from Project deletion are the sole physical-delete
                -- exception. Individual ledger rows remain append-only.
                IF EXISTS (SELECT 1 FROM project WHERE id = OLD.project_id) THEN
                    RAISE EXCEPTION 'Strategic Memory ledger rows are append-only';
                END IF;
                RETURN OLD;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        for table in _APPEND_ONLY_TABLES:
            op.execute(
                f"CREATE TRIGGER trg_{table}_append_only "
                f"BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION strategic_memory_ledger_append_only()"
            )
    elif bind.dialect.name == "sqlite":
        for table in _APPEND_ONLY_TABLES:
            op.execute(
                f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'Strategic Memory ledger rows are append-only'); END"
            )
            op.execute(
                f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
                "WHEN EXISTS (SELECT 1 FROM project WHERE id = OLD.project_id) "
                "BEGIN SELECT RAISE(ABORT, 'Strategic Memory ledger rows are append-only'); END"
            )


def _drop_append_only_guards() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in _APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
        op.execute("DROP FUNCTION IF EXISTS strategic_memory_ledger_append_only()")
    elif bind.dialect.name == "sqlite":
        for table in _APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")


def upgrade() -> None:
    with op.batch_alter_table("strategicmemoryprojectstate") as batch_op:
        batch_op.add_column(
            sa.Column("ledger_revision", sa.BigInteger(), nullable=False, server_default="0")
        )
        batch_op.create_check_constraint(
            "ck_smem_state_ledger_revision", "ledger_revision >= 0"
        )

    with op.batch_alter_table("strategicmemorysource") as batch_op:
        batch_op.create_unique_constraint("uq_smem_source_project_id", ["project_id", "id"])

    with op.batch_alter_table("strategicmemoryconsolidationjob") as batch_op:
        batch_op.add_column(
            sa.Column("accepted_proposal_idempotency_key", sa.String(length=255), nullable=True)
        )
        batch_op.add_column(sa.Column("accepted_proposal_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("proposal_schema_version", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("resulting_ledger_revision", sa.BigInteger(), nullable=True))
        batch_op.create_check_constraint(
            "ck_smem_job_proposal_hash_len",
            "accepted_proposal_hash IS NULL OR length(accepted_proposal_hash) = 64",
        )
        batch_op.create_check_constraint(
            "ck_smem_job_result_ledger",
            "resulting_ledger_revision IS NULL OR resulting_ledger_revision >= 0",
        )
        batch_op.create_unique_constraint("uq_smem_job_project_id", ["project_id", "id"])
    op.create_index(
        "uq_smem_job_project_accepted_proposal",
        "strategicmemoryconsolidationjob",
        ["project_id", "accepted_proposal_idempotency_key"],
        unique=True,
        postgresql_where=sa.text("accepted_proposal_idempotency_key IS NOT NULL"),
        sqlite_where=sa.text("accepted_proposal_idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "strategicmemoryitem",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False, server_default="project"),
        sa.Column("scope_key", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("logical_key", sa.String(length=512), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("forgotten_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("scope_type IN ('project','research_topic')", name="ck_smem_item_scope_type"),
        sa.CheckConstraint(
            "(scope_type = 'project' AND scope_key = '') OR "
            "(scope_type = 'research_topic' AND length(scope_key) > 0)",
            name="ck_smem_item_scope_key",
        ),
        sa.CheckConstraint(
            "kind IN ('goal','preference','constraint','definition','notation',"
            "'assumption','claim','decision','result','counterexample',"
            "'open_question','procedure','lesson')",
            name="ck_smem_item_kind",
        ),
        sa.CheckConstraint("current_version >= 0", name="ck_smem_item_current_version"),
        sa.CheckConstraint(
            "current_status IN ('active','challenged','superseded','retracted','forgotten')",
            name="ck_smem_item_current_status",
        ),
        sa.CheckConstraint(
            "(current_status = 'forgotten') = (forgotten_at IS NOT NULL)",
            name="ck_smem_item_forgotten_time",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "id", name="uq_smem_item_project_id"),
        sa.UniqueConstraint(
            "project_id", "scope_type", "scope_key", "logical_key",
            name="uq_smem_item_project_scope_logical",
        ),
    )
    op.create_index(
        "ix_smem_item_project_kind_status", "strategicmemoryitem", ["project_id", "kind", "current_status"]
    )
    op.create_index("ix_smem_item_project_updated", "strategicmemoryitem", ["project_id", "updated_at"])

    op.create_table(
        "strategicmemoryitemversion",
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_text", sa.Text(), nullable=False),
        sa.Column("structured_value", _JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("epistemic_status", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("asserted_effective_from", sa.DateTime(), nullable=True),
        sa.Column("asserted_effective_until", sa.DateTime(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("recorded_source_sequence", sa.BigInteger(), nullable=False),
        sa.Column("exact_terms", _TEXT_ARRAY, nullable=False),
        sa.Column("formula_fingerprints", _TEXT_ARRAY, nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_job_id", sa.Uuid(), nullable=True),
        sa.Column("extractor_version", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["project_id", "item_id"], ["strategicmemoryitem.project_id", "strategicmemoryitem.id"]
            , ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["project_id", "created_by_job_id"],
            ["strategicmemoryconsolidationjob.project_id", "strategicmemoryconsolidationjob.id"],
        ),
        sa.PrimaryKeyConstraint("item_id", "version"),
        sa.UniqueConstraint("project_id", "item_id", "version", name="uq_smem_item_version_project_item"),
        sa.CheckConstraint("version >= 1", name="ck_smem_item_version_positive"),
        sa.CheckConstraint(
            "epistemic_status IN ('operator_explicit','verified_evidence',"
            "'agent_conclusion','agent_hypothesis')",
            name="ck_smem_item_version_epistemic",
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_smem_item_version_confidence"),
        sa.CheckConstraint(
            "asserted_effective_until IS NULL OR asserted_effective_from IS NULL "
            "OR asserted_effective_until > asserted_effective_from",
            name="ck_smem_item_version_effective_interval",
        ),
        sa.CheckConstraint("recorded_source_sequence >= 1", name="ck_smem_item_version_source_seq"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_smem_item_version_hash_len"),
    )
    op.create_index("ix_smem_item_version_item_recorded", "strategicmemoryitemversion", ["item_id", "recorded_at"])
    op.create_index(
        "ix_smem_item_version_project_source", "strategicmemoryitemversion", ["project_id", "recorded_source_sequence"]
    )
    op.create_index(
        "ix_smem_item_version_effective", "strategicmemoryitemversion",
        ["project_id", "asserted_effective_from", "asserted_effective_until"],
    )

    op.create_table(
        "strategicmemoryitemsource",
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("item_version", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("source_role", sa.String(length=16), nullable=False),
        sa.Column("span_index", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("span_kind", sa.String(length=32), nullable=False),
        sa.Column("span_start", sa.BigInteger(), nullable=True),
        sa.Column("span_end", sa.BigInteger(), nullable=True),
        sa.Column("span_hash", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "item_id", "item_version"],
            [
                "strategicmemoryitemversion.project_id",
                "strategicmemoryitemversion.item_id",
                "strategicmemoryitemversion.version",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["project_id", "source_id"], ["strategicmemorysource.project_id", "strategicmemorysource.id"]
        ),
        sa.PrimaryKeyConstraint("item_id", "item_version", "source_id", "source_role", "span_index"),
        sa.CheckConstraint(
            "source_role IN ('assertion','evidence','correction','verification')",
            name="ck_smem_item_source_role",
        ),
        sa.CheckConstraint(
            "span_kind IN ('text','inline_math','display_math','code','identifier',"
            "'artifact_region')",
            name="ck_smem_item_source_span_kind",
        ),
        sa.CheckConstraint("span_index >= 0", name="ck_smem_item_source_span_index"),
        sa.CheckConstraint(
            "(span_start IS NULL AND span_end IS NULL) OR "
            "(span_start >= 0 AND span_end > span_start)",
            name="ck_smem_item_source_span_range",
        ),
        sa.CheckConstraint("length(span_hash) = 64", name="ck_smem_item_source_hash_len"),
    )
    op.create_index("ix_smem_item_source_project_source", "strategicmemoryitemsource", ["project_id", "source_id"])

    op.create_table(
        "strategicmemorytransition",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("item_version", sa.Integer(), nullable=False),
        sa.Column("transition", sa.String(length=16), nullable=False),
        sa.Column("target_item_id", sa.Uuid(), nullable=True),
        sa.Column("target_version", sa.Integer(), nullable=True),
        sa.Column("effective_from", sa.DateTime(), nullable=True),
        sa.Column("effective_until", sa.DateTime(), nullable=True),
        sa.Column("recorded_source_sequence", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["project_id", "item_id", "item_version"],
            [
                "strategicmemoryitemversion.project_id",
                "strategicmemoryitemversion.item_id",
                "strategicmemoryitemversion.version",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "target_item_id", "target_version"],
            [
                "strategicmemoryitemversion.project_id",
                "strategicmemoryitemversion.item_id",
                "strategicmemoryitemversion.version",
            ],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["project_id", "source_id"], ["strategicmemorysource.project_id", "strategicmemorysource.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "transition IN ('activate','challenge','supersede','retract','forget')",
            name="ck_smem_transition_kind",
        ),
        sa.CheckConstraint(
            "(target_item_id IS NULL AND target_version IS NULL) OR "
            "(target_item_id IS NOT NULL AND target_version IS NOT NULL)",
            name="ck_smem_transition_target_pair",
        ),
        sa.CheckConstraint(
            "(transition = 'supersede' AND target_item_id IS NOT NULL) OR "
            "(transition <> 'supersede' AND target_item_id IS NULL)",
            name="ck_smem_transition_target_kind",
        ),
        sa.CheckConstraint("recorded_source_sequence >= 1", name="ck_smem_transition_source_seq"),
        sa.CheckConstraint(
            "effective_until IS NULL OR effective_from IS NULL OR effective_until > effective_from",
            name="ck_smem_transition_effective_interval",
        ),
    )
    op.create_index(
        "ix_smem_transition_project_item_sequence", "strategicmemorytransition",
        ["project_id", "item_id", "recorded_source_sequence"],
    )
    op.create_index("ix_smem_transition_project_source", "strategicmemorytransition", ["project_id", "source_id"])

    op.create_table(
        "strategicmemorycorrectionbarrier",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("correction_source_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_item_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_version", sa.Integer(), nullable=False),
        sa.Column("match_kind", sa.String(length=16), nullable=False),
        sa.Column("match_score", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("context_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["project_id", "correction_source_id"],
            ["strategicmemorysource.project_id", "strategicmemorysource.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["project_id", "candidate_item_id", "candidate_version"],
            [
                "strategicmemoryitemversion.project_id",
                "strategicmemoryitemversion.item_id",
                "strategicmemoryitemversion.version",
            ],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "id", name="uq_smem_barrier_project_id"),
        sa.UniqueConstraint(
            "project_id", "correction_source_id", "candidate_item_id", "candidate_version",
            name="uq_smem_barrier_candidate",
        ),
        sa.CheckConstraint("match_kind IN ('exact','reply_link','classifier')", name="ck_smem_barrier_match_kind"),
        sa.CheckConstraint(
            "match_score IS NULL OR (match_score >= 0 AND match_score <= 1)",
            name="ck_smem_barrier_match_score",
        ),
        sa.CheckConstraint("context_generation >= 0", name="ck_smem_barrier_authority_gen"),
    )
    op.create_index(
        "ix_smem_barrier_project_candidate", "strategicmemorycorrectionbarrier",
        ["project_id", "candidate_item_id", "candidate_version"],
    )

    op.create_table(
        "strategicmemorycorrectionbarrierevent",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("barrier_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("actor_kind", sa.String(length=64), nullable=True),
        sa.Column("actor_id", sa.String(length=255), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("recorded_source_sequence", sa.BigInteger(), nullable=False),
        sa.Column("context_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["project_id", "barrier_id"],
            ["strategicmemorycorrectionbarrier.project_id", "strategicmemorycorrectionbarrier.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["project_id", "source_id"], ["strategicmemorysource.project_id", "strategicmemorysource.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "event_type IN ('opened','target_confirmed','resolved','dismissed')",
            name="ck_smem_barrier_event_type",
        ),
        sa.CheckConstraint("recorded_source_sequence >= 1", name="ck_smem_barrier_event_source_seq"),
        sa.CheckConstraint("context_generation >= 0", name="ck_smem_barrier_event_authority_gen"),
    )
    op.create_index(
        "ix_smem_barrier_event_project_barrier", "strategicmemorycorrectionbarrierevent",
        ["project_id", "barrier_id", "created_at"],
    )

    op.create_table(
        "strategicmemoryedge",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("from_node_type", sa.String(length=32), nullable=False, server_default="item_version"),
        sa.Column("from_node_id", sa.Uuid(), nullable=False),
        sa.Column("from_version", sa.Integer(), nullable=False),
        sa.Column("relation", sa.String(length=32), nullable=False),
        sa.Column("to_node_type", sa.String(length=32), nullable=False, server_default="item_version"),
        sa.Column("to_node_id", sa.Uuid(), nullable=False),
        sa.Column("to_version", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["project_id", "from_node_id", "from_version"],
            [
                "strategicmemoryitemversion.project_id",
                "strategicmemoryitemversion.item_id",
                "strategicmemoryitemversion.version",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "to_node_id", "to_version"],
            [
                "strategicmemoryitemversion.project_id",
                "strategicmemoryitemversion.item_id",
                "strategicmemoryitemversion.version",
            ],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "id", name="uq_smem_edge_project_id"),
        sa.UniqueConstraint(
            "project_id", "from_node_id", "from_version", "relation", "to_node_id", "to_version", "version",
            name="uq_smem_edge_versioned_relation",
        ),
        sa.CheckConstraint("from_node_type = 'item_version'", name="ck_smem_edge_from_type"),
        sa.CheckConstraint("to_node_type = 'item_version'", name="ck_smem_edge_to_type"),
        sa.CheckConstraint(
            "relation IN ('supports','contradicts','derived_from','depends_on')",
            name="ck_smem_edge_relation",
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_smem_edge_confidence"),
        sa.CheckConstraint("version >= 1", name="ck_smem_edge_version"),
        sa.CheckConstraint(
            "from_node_id <> to_node_id OR from_version <> to_version", name="ck_smem_edge_no_self"
        ),
    )
    op.create_index("ix_smem_edge_project_from", "strategicmemoryedge", ["project_id", "from_node_id", "from_version"])
    op.create_index("ix_smem_edge_project_to", "strategicmemoryedge", ["project_id", "to_node_id", "to_version"])

    op.create_table(
        "strategicmemoryedgesource",
        sa.Column("edge_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "edge_id"], ["strategicmemoryedge.project_id", "strategicmemoryedge.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["project_id", "source_id"], ["strategicmemorysource.project_id", "strategicmemorysource.id"]
        ),
        sa.PrimaryKeyConstraint("edge_id", "source_id"),
    )
    op.create_index("ix_smem_edge_source_project_source", "strategicmemoryedgesource", ["project_id", "source_id"])

    op.create_table(
        "strategicmemoryprojection",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_revision", sa.BigInteger(), nullable=False),
        sa.Column("through_source_sequence", sa.BigInteger(), nullable=False),
        sa.Column("structured_state", _JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("canonical_text", sa.Text(), nullable=False),
        sa.Column("canonical_sha256", sa.String(length=64), nullable=False),
        sa.Column("rebuilt_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id"),
        sa.CheckConstraint("ledger_revision >= 0", name="ck_smem_projection_ledger_revision"),
        sa.CheckConstraint("through_source_sequence >= 0", name="ck_smem_projection_through_source"),
        sa.CheckConstraint("length(canonical_sha256) = 64", name="ck_smem_projection_sha_len"),
    )

    _create_append_only_guards()


def downgrade() -> None:
    _drop_append_only_guards()
    op.drop_table("strategicmemoryprojection")
    op.drop_index("ix_smem_edge_source_project_source", table_name="strategicmemoryedgesource")
    op.drop_table("strategicmemoryedgesource")
    op.drop_index("ix_smem_edge_project_to", table_name="strategicmemoryedge")
    op.drop_index("ix_smem_edge_project_from", table_name="strategicmemoryedge")
    op.drop_table("strategicmemoryedge")
    op.drop_index("ix_smem_barrier_event_project_barrier", table_name="strategicmemorycorrectionbarrierevent")
    op.drop_table("strategicmemorycorrectionbarrierevent")
    op.drop_index("ix_smem_barrier_project_candidate", table_name="strategicmemorycorrectionbarrier")
    op.drop_table("strategicmemorycorrectionbarrier")
    op.drop_index("ix_smem_transition_project_source", table_name="strategicmemorytransition")
    op.drop_index("ix_smem_transition_project_item_sequence", table_name="strategicmemorytransition")
    op.drop_table("strategicmemorytransition")
    op.drop_index("ix_smem_item_source_project_source", table_name="strategicmemoryitemsource")
    op.drop_table("strategicmemoryitemsource")
    op.drop_index("ix_smem_item_version_effective", table_name="strategicmemoryitemversion")
    op.drop_index("ix_smem_item_version_project_source", table_name="strategicmemoryitemversion")
    op.drop_index("ix_smem_item_version_item_recorded", table_name="strategicmemoryitemversion")
    op.drop_table("strategicmemoryitemversion")
    op.drop_index("ix_smem_item_project_updated", table_name="strategicmemoryitem")
    op.drop_index("ix_smem_item_project_kind_status", table_name="strategicmemoryitem")
    op.drop_table("strategicmemoryitem")
    op.drop_index("uq_smem_job_project_accepted_proposal", table_name="strategicmemoryconsolidationjob")
    with op.batch_alter_table("strategicmemoryconsolidationjob") as batch_op:
        batch_op.drop_constraint("uq_smem_job_project_id", type_="unique")
        batch_op.drop_constraint("ck_smem_job_result_ledger", type_="check")
        batch_op.drop_constraint("ck_smem_job_proposal_hash_len", type_="check")
        batch_op.drop_column("resulting_ledger_revision")
        batch_op.drop_column("proposal_schema_version")
        batch_op.drop_column("accepted_proposal_hash")
        batch_op.drop_column("accepted_proposal_idempotency_key")
    with op.batch_alter_table("strategicmemorysource") as batch_op:
        batch_op.drop_constraint("uq_smem_source_project_id", type_="unique")
    with op.batch_alter_table("strategicmemoryprojectstate") as batch_op:
        batch_op.drop_constraint("ck_smem_state_ledger_revision", type_="check")
        batch_op.drop_column("ledger_revision")

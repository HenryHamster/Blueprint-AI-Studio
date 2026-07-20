"""Allow context-bound Resident inspection receipts through the DB fence.

Revision 0611 required mutation-style expected revisions on every operation
receipt. Read-only inspection receipts deliberately carry a frozen turn
context_hash instead, so Postgres rejected their inserts even though the
active Resident, turn, authority generation, and checkpoint lineage matched.

Keep the complete mutation fence unchanged. The narrow inspection branch is
limited to the server-declared capability/operation pairs, low risk, and an
exact match against the context hash persisted on the active frozen turn.

Revision ID: 0640
Revises: 0639
Create Date: 2026-07-19
"""

from alembic import op

revision = "0640"
down_revision = "0639"
branch_labels = None
depends_on = None


_INSPECTION_PAIR_SQL = """
              (NEW.capability, NEW.operation_kind) IN (
                ('task.inspect', 'inspect_task'),
                ('task.logs.inspect', 'inspect_task_log'),
                ('task.failure.inspect', 'inspect_failure'),
                ('task.provider.inspect', 'inspect_provider'),
                ('task.lineage.inspect', 'inspect_task_lineage'),
                ('runner.inspect', 'inspect_runner'),
                ('runner.list', 'inspect_runner_capacity'),
                ('pipeline.inspect', 'inspect_pipeline'),
                ('qualification.inspect', 'inspect_qualification'),
                ('pr.inspect', 'inspect_pr'),
                ('pr.diff.inspect', 'inspect_pr_diff'),
                ('pr.checks.inspect', 'inspect_pr_checks'),
                ('ci.run.inspect', 'inspect_ci'),
                ('deployment.inspect', 'inspect_deployment')
              )
              AND NEW.risk_level = 'low'
              AND NEW.status IN ('applying', 'applied')
              AND NEW.unknown_effects = '[]'::jsonb
              AND NEW.operation_id =
                    'inspect:' || NEW.turn_task_id::text || ':' || NEW.request_hash
              AND NEW.expected_revisions ? 'context_hash'
              AND NEW.expected_revisions->>'context_hash'
                    = turn_task.context->>'_resident_turn_context_hash'
"""

_MUTATION_REVISION_SQL = """
              NEW.expected_revisions ? 'authority_generation'
              AND (NEW.expected_revisions->>'authority_generation')::bigint
                    = NEW.authority_generation
              AND NEW.expected_revisions ? 'active_turn_task_version'
              AND (NEW.expected_revisions->>'active_turn_task_version')::bigint
                    = turn_task.version
              AND NEW.expected_revisions ? 'current_checkpoint_id'
              AND NEW.expected_revisions->>'current_checkpoint_id'
                    IS NOT DISTINCT FROM resident.current_checkpoint_id::text
              AND NEW.expected_revisions ? 'resident_checkpoint_version'
              AND (NEW.expected_revisions->>'resident_checkpoint_version')::bigint
                    IS NOT DISTINCT FROM current_checkpoint.version
              AND NEW.expected_revisions ? 'resident_checkpoint_hash'
              AND NEW.expected_revisions->>'resident_checkpoint_hash'
                    IS NOT DISTINCT FROM current_checkpoint.content_hash
"""


def _fence_function_sql(*, allow_inspections: bool) -> str:
    receipt_predicate_sql = _MUTATION_REVISION_SQL
    if allow_inspections:
        receipt_predicate_sql = f"(({_INSPECTION_PAIR_SQL}) OR ({_MUTATION_REVISION_SQL}))"
    return f"""
        CREATE OR REPLACE FUNCTION fence_strategicoperationreceipt_insert()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          PERFORM 1
            FROM strategicresident resident
            JOIN strategicresidentcheckpoint current_checkpoint
              ON current_checkpoint.id = resident.current_checkpoint_id
             AND current_checkpoint.project_id = resident.project_id
            JOIN task turn_task ON turn_task.id = NEW.turn_task_id
            WHERE resident.project_id = NEW.project_id
              AND resident.engine_owner = 'strategic_resident'
              AND resident.authority_generation = NEW.authority_generation
              AND resident.active_turn_task_id = NEW.turn_task_id
              AND turn_task.project_id = NEW.project_id
              AND turn_task.payloads->0->>'type' = 'strategic_resident_turn'
              AND turn_task.payloads->0->>'resident_id' = NEW.project_id::text
              AND (turn_task.payloads->0->>'authority_generation')::bigint
                    = NEW.authority_generation
              AND turn_task.payloads->0->>'engine_owner'
                    = 'strategic_resident'
              AND (turn_task.payloads->0->>'checkpoint_version')::bigint
                    = current_checkpoint.version
              AND turn_task.payloads->0->>'checkpoint_hash'
                    = current_checkpoint.content_hash
              AND turn_task.status = 'IN_PROGRESS'
              AND coalesce(
                (turn_task.context->>'_resident_mutation_authorized')::boolean,
                false
              )
              AND coalesce(
                (turn_task.context->>'_resident_input_frozen')::boolean,
                false
              )
              AND turn_task.context->>'_resident_project_id'
                    = NEW.project_id::text
              AND (turn_task.context->>'_resident_authority_generation')::bigint
                    = NEW.authority_generation
              AND turn_task.context->>'_resident_engine_owner_observed'
                    = 'strategic_resident'
              AND ({receipt_predicate_sql})
            FOR SHARE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'Resident mutation authority is stale or invalid'
              USING ERRCODE = '42501';
          END IF;
          RETURN NEW;
        END $$;
        """


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_fence_function_sql(allow_inspections=True))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_fence_function_sql(allow_inspections=False))

"""Gate the 0608 compat-trigger projection rewrite off UPDATEs of already-sequenced rows.

The 0608 ``projectmessage_0567_compat_insert`` trigger re-derives
``surface`` / ``handling_state`` / ``actor_user_id`` whenever ``should_project``
is true — INCLUDING on an UPDATE of an already-sequenced row (e.g. the agent
reply path setting ``handled_by_task_id`` on an answered question). Because the
row was already promoted to ``surface='conversation'`` at insert time,
``should_project`` is true again on that UPDATE, so the block clobbers an
application-set ``handling_state='responded'`` back to ``'pending'`` — answered
questions stay "pending" on Postgres. (SQLite's compat trigger is gated
``WHEN NEW.sequence IS NULL``, so it never fired for the sequenced row, which is
why the behaviour only manifested on the Postgres shard.)

This migration ``CREATE OR REPLACE``s the function so the projection block runs
only on INSERT and on the un-sequenced allocation UPDATE, never re-deriving an
already-sequenced row on a later UPDATE. The sequence-allocation block was
already guarded for that case (``NEW.sequence IS NULL OR TG_OP = 'INSERT'``), so
only the ``should_project`` projection guard changes. The trigger definition
(watched columns) is unchanged; replacing the function is sufficient.

Revision ID: 0638
Revises: 0637
Create Date: 2026-07-19
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0638"
down_revision = "0637"
branch_labels = None
depends_on = None

_SEQUENCE_TABLE = "projectmessagesequence"

# Copied verbatim from 0608 (the sole definer of this function) so the CREATE OR
# REPLACE reproduces the current body faithfully; the only functional change is
# the projection guard passed into _compat_trigger_function_sql().
_HIGH_SIGNAL_EVENT_TYPES = frozenset(
    {
        "auto_fix_escalated",
        "residual_fix_escalated",
        "residual_fix_audit_deferred",
        "remediation_escalated",
        "direction_burn_pause",
        "direction_outcome_scaffolding_escalation",
        "direction_churn_pause",
        "failure_alert",
        "rollback_recommended",
        "fix_loop_divergence_regressed",
        "release_calcifying",
        "release_build_stalled",
        "release_stabilize_stalled",
        "release_ci_unresolved_stall",
        "direction_outcome_stalled",
        "direction_staged_activated",
        "promotion_readiness_reached",
        "release_auto_merge_stalled",
        "strategic_steering_deferred",
        "strategic_steering_acted",
        "capability_trial_stalled",
        "capability_verdict_decided",
        "capability_trial_ambiguous_lift_escalation",
        "question_answer",
        "capability_proof_gate_graduated_observe",
        "capability_proof_gate_graduated_enforce",
        "criterion_retracted",
        "criterion_superseded",
    }
)

_UUID_PATTERN = (
    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    "[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)

# The projection guard applied at upgrade: the original 0608 guard was bare
# ``should_project``; we AND it with "not an update of an already-sequenced row".
_GATED_PROJECTION_GUARD = (
    "should_project AND NOT (TG_OP = 'UPDATE' AND OLD.sequence IS NOT NULL)"
)
_ORIGINAL_PROJECTION_GUARD = "should_project"


def _compat_trigger_function_sql(*, projection_guard: str) -> str:
    high_signal_sql = ", ".join(
        "'" + value.replace("'", "''") + "'"
        for value in sorted(_HIGH_SIGNAL_EVENT_TYPES)
    )
    uuid_pattern = _UUID_PATTERN
    return f"""
        CREATE OR REPLACE FUNCTION projectmessage_0567_compat_insert()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          candidate_reply uuid;
          linked_project uuid;
          should_project boolean;
        BEGIN
          IF NEW.reply_to_message_id IS NULL
             AND coalesce(NEW.context->>'reply_to', '') ~ '{uuid_pattern}' THEN
            candidate_reply := (NEW.context->>'reply_to')::uuid;
            NEW.reply_to_message_id := candidate_reply;
          END IF;

          IF NEW.reply_to_message_id IS NOT NULL THEN
            linked_project := NULL;
            SELECT target.project_id INTO linked_project
            FROM projectmessage target
            WHERE target.id = NEW.reply_to_message_id
              AND target.id <> NEW.id
            FOR SHARE;
            IF linked_project IS DISTINCT FROM NEW.project_id THEN
              IF NEW.sequence IS NOT NULL THEN
                RAISE EXCEPTION 'reply message must belong to the same project'
                  USING ERRCODE = '23503';
              END IF;
              NEW.reply_to_message_id := NULL;
            END IF;
          END IF;
          IF NEW.handled_by_task_id IS NOT NULL THEN
            linked_project := NULL;
            SELECT owner_task.project_id INTO linked_project
            FROM task owner_task
            WHERE owner_task.id = NEW.handled_by_task_id
            FOR SHARE;
            IF linked_project IS DISTINCT FROM NEW.project_id THEN
              IF NEW.sequence IS NOT NULL THEN
                RAISE EXCEPTION 'handling task must belong to the same project'
                  USING ERRCODE = '23503';
              END IF;
              NEW.handled_by_task_id := NULL;
            END IF;
          END IF;
          IF TG_OP = 'UPDATE'
             AND NEW.project_id IS DISTINCT FROM OLD.project_id
             AND EXISTS (
               SELECT 1 FROM projectmessage reply
               WHERE reply.reply_to_message_id = NEW.id
                 AND reply.project_id <> NEW.project_id
             ) THEN
            RAISE EXCEPTION 'moving message would strand cross-project replies'
              USING ERRCODE = '23503';
          END IF;

          should_project := NEW.surface = 'conversation'
             OR (
               NEW.sequence IS NULL AND (
                 NEW.from_type IN ('user', 'strategic')
                 OR coalesce(NEW.context->>'event_type', '') IN ({high_signal_sql})
                 OR NEW.reply_to_message_id IS NOT NULL
               )
             );
          IF should_project AND (
            NEW.sequence IS NULL OR (
              TG_OP = 'INSERT' AND NOT EXISTS (
                SELECT 1 FROM {_SEQUENCE_TABLE} allocator
                WHERE allocator.project_id = NEW.project_id
                  AND allocator.last_sequence = NEW.sequence
              )
            )
          ) THEN
            INSERT INTO {_SEQUENCE_TABLE} (project_id, last_sequence)
            VALUES (NEW.project_id, 1)
            ON CONFLICT (project_id) DO UPDATE
            SET last_sequence = {_SEQUENCE_TABLE}.last_sequence + 1
            RETURNING last_sequence INTO NEW.sequence;
          END IF;
          IF {projection_guard} THEN
            NEW.surface := 'conversation';
            NEW.handling_state := CASE
              WHEN NEW.from_type = 'strategic' THEN 'responded' ELSE 'pending'
            END;
            IF NEW.actor_user_id IS NULL THEN
              NEW.actor_user_id := left(nullif(btrim(coalesce(
                NEW.context->>'operator_user_id',
                NEW.context->'check_in'->>'operator_user_id'
              )), ''), 255);
            END IF;
          END IF;
          RETURN NEW;
        END $$
        """


def upgrade() -> None:
    op.execute(
        _compat_trigger_function_sql(projection_guard=_GATED_PROJECTION_GUARD)
    )


def downgrade() -> None:
    op.execute(
        _compat_trigger_function_sql(projection_guard=_ORIGINAL_PROJECTION_GUARD)
    )

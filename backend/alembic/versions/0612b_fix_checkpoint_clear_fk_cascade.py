"""Fix checkpoint-clear trigger to allow FK SET NULL cascade

The fence_strategicresident_checkpoint_rotation() PostgreSQL function (and the
SQLite trg_strategicresident_checkpoint_clear trigger) blocks clearing
current_checkpoint_id while the project still exists.  That guard is correct
for application code, but it also fires when the FK
fk_strategicresident_current_checkpoint (ondelete="SET NULL") cascades during
a DELETE on strategicresidentcheckpoint — e.g. during test teardown, when
rows are deleted in FK-dependency order (checkpoint before resident).

Within the same DELETE statement the deleted checkpoint row is no longer
visible to subsequent queries, so the fix is to check whether the row
referenced by OLD.current_checkpoint_id still exists; if it does not, the
update was driven by the FK cascade and clearing is safe.

Revision ID: 0612b
Revises: 0612
Create Date: 2026-07-17
"""

from alembic import op

revision = "0612b"
down_revision = "0612"
branch_labels = None
depends_on = None

_RESIDENT_TABLE = "strategicresident"
_CHECKPOINT_TABLE = "strategicresidentcheckpoint"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Replace the function; the existing trigger already points to it.
        op.execute(
            """
            CREATE OR REPLACE FUNCTION fence_strategicresident_checkpoint_rotation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE
              old_version bigint;
              old_hash text;
              new_version bigint;
              new_source_task uuid;
              new_generation bigint;
            BEGIN
              IF NEW.current_checkpoint_id IS NOT DISTINCT FROM OLD.current_checkpoint_id
                 AND NEW.previous_checkpoint_id IS NOT DISTINCT FROM OLD.previous_checkpoint_id
              THEN
                RETURN NEW;
              END IF;
              IF NEW.current_checkpoint_id IS NULL THEN
                IF NOT EXISTS (
                  SELECT 1 FROM project WHERE id = OLD.project_id
                ) THEN
                  RETURN NEW;
                END IF;
                -- fk_strategicresident_current_checkpoint (ondelete="SET NULL")
                -- fires when the referenced checkpoint row is deleted.  The
                -- DELETE already ran so the row is no longer visible within
                -- the same statement — allow the FK-driven NULL.
                IF OLD.current_checkpoint_id IS NOT NULL
                   AND NOT EXISTS (
                     SELECT 1 FROM strategicresidentcheckpoint
                     WHERE id = OLD.current_checkpoint_id
                   ) THEN
                  RETURN NEW;
                END IF;
                RAISE EXCEPTION 'Resident current checkpoint cannot be cleared'
                  USING ERRCODE = '23514';
              END IF;
              IF NEW.authority_generation <> OLD.authority_generation THEN
                RAISE EXCEPTION 'checkpoint rotation cannot change authority generation'
                  USING ERRCODE = '23514';
              END IF;
              SELECT version, source_turn_task_id, authority_generation
                INTO new_version, new_source_task, new_generation
              FROM strategicresidentcheckpoint
              WHERE id = NEW.current_checkpoint_id
                AND project_id = NEW.project_id
              FOR SHARE;
              IF OLD.engine_owner = 'legacy'
                 AND OLD.current_checkpoint_id IS NULL
                 AND NEW.previous_checkpoint_id IS NULL
                 AND new_version = 1
                 AND new_generation = OLD.authority_generation THEN
                RETURN NEW;
              END IF;
              SELECT version, content_hash INTO old_version, old_hash
              FROM strategicresidentcheckpoint
              WHERE id = OLD.current_checkpoint_id
                AND project_id = OLD.project_id
              FOR SHARE;
              PERFORM 1 FROM task turn_task
              WHERE turn_task.id = OLD.active_turn_task_id
                AND turn_task.project_id = OLD.project_id
                AND turn_task.payloads->0->>'type' = 'strategic_resident_turn'
                AND turn_task.payloads->0->>'resident_id' = OLD.project_id::text
                AND (turn_task.payloads->0->>'authority_generation')::bigint
                      = OLD.authority_generation
                AND turn_task.payloads->0->>'engine_owner'
                      = 'strategic_resident'
                AND (turn_task.payloads->0->>'checkpoint_version')::bigint
                      = old_version
                AND turn_task.payloads->0->>'checkpoint_hash' = old_hash
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
                      = OLD.project_id::text
                AND (turn_task.context->>'_resident_authority_generation')::bigint
                      = OLD.authority_generation
                AND turn_task.context->>'_resident_engine_owner_observed'
                      = 'strategic_resident'
              FOR SHARE;
              IF OLD.engine_owner <> 'strategic_resident'
                 OR NOT FOUND
                 OR NEW.previous_checkpoint_id IS DISTINCT FROM OLD.current_checkpoint_id
                 OR new_source_task IS DISTINCT FROM OLD.active_turn_task_id
                 OR new_generation <> OLD.authority_generation
                 OR new_version <> old_version + 1 THEN
                RAISE EXCEPTION 'Resident checkpoint rotation is not authorized'
                  USING ERRCODE = '42501';
              END IF;
              RETURN NEW;
            END $$;
            """
        )
    else:
        # SQLite: drop and recreate the checkpoint-clear trigger with the same
        # fix (AND EXISTS on the checkpoint row so FK cascade is allowed).
        op.execute(
            "DROP TRIGGER IF EXISTS trg_strategicresident_checkpoint_clear"
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_strategicresident_checkpoint_clear
            BEFORE UPDATE OF current_checkpoint_id ON {_RESIDENT_TABLE}
            FOR EACH ROW WHEN
              OLD.current_checkpoint_id IS NOT NULL
              AND NEW.current_checkpoint_id IS NULL
              AND EXISTS (SELECT 1 FROM project WHERE id = OLD.project_id)
              AND EXISTS (
                SELECT 1 FROM {_CHECKPOINT_TABLE}
                WHERE id = OLD.current_checkpoint_id
              )
            BEGIN
              SELECT RAISE(ABORT, 'Resident current checkpoint cannot be cleared');
            END
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Restore the pre-0612 function body (without the FK cascade escape).
        op.execute(
            """
            CREATE OR REPLACE FUNCTION fence_strategicresident_checkpoint_rotation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE
              old_version bigint;
              old_hash text;
              new_version bigint;
              new_source_task uuid;
              new_generation bigint;
            BEGIN
              IF NEW.current_checkpoint_id IS NOT DISTINCT FROM OLD.current_checkpoint_id
                 AND NEW.previous_checkpoint_id IS NOT DISTINCT FROM OLD.previous_checkpoint_id
              THEN
                RETURN NEW;
              END IF;
              IF NEW.current_checkpoint_id IS NULL THEN
                IF NOT EXISTS (
                  SELECT 1 FROM project WHERE id = OLD.project_id
                ) THEN
                  RETURN NEW;
                END IF;
                RAISE EXCEPTION 'Resident current checkpoint cannot be cleared'
                  USING ERRCODE = '23514';
              END IF;
              IF NEW.authority_generation <> OLD.authority_generation THEN
                RAISE EXCEPTION 'checkpoint rotation cannot change authority generation'
                  USING ERRCODE = '23514';
              END IF;
              SELECT version, source_turn_task_id, authority_generation
                INTO new_version, new_source_task, new_generation
              FROM strategicresidentcheckpoint
              WHERE id = NEW.current_checkpoint_id
                AND project_id = NEW.project_id
              FOR SHARE;
              IF OLD.engine_owner = 'legacy'
                 AND OLD.current_checkpoint_id IS NULL
                 AND NEW.previous_checkpoint_id IS NULL
                 AND new_version = 1
                 AND new_generation = OLD.authority_generation THEN
                RETURN NEW;
              END IF;
              SELECT version, content_hash INTO old_version, old_hash
              FROM strategicresidentcheckpoint
              WHERE id = OLD.current_checkpoint_id
                AND project_id = OLD.project_id
              FOR SHARE;
              PERFORM 1 FROM task turn_task
              WHERE turn_task.id = OLD.active_turn_task_id
                AND turn_task.project_id = OLD.project_id
                AND turn_task.payloads->0->>'type' = 'strategic_resident_turn'
                AND turn_task.payloads->0->>'resident_id' = OLD.project_id::text
                AND (turn_task.payloads->0->>'authority_generation')::bigint
                      = OLD.authority_generation
                AND turn_task.payloads->0->>'engine_owner'
                      = 'strategic_resident'
                AND (turn_task.payloads->0->>'checkpoint_version')::bigint
                      = old_version
                AND turn_task.payloads->0->>'checkpoint_hash' = old_hash
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
                      = OLD.project_id::text
                AND (turn_task.context->>'_resident_authority_generation')::bigint
                      = OLD.authority_generation
                AND turn_task.context->>'_resident_engine_owner_observed'
                      = 'strategic_resident'
              FOR SHARE;
              IF OLD.engine_owner <> 'strategic_resident'
                 OR NOT FOUND
                 OR NEW.previous_checkpoint_id IS DISTINCT FROM OLD.current_checkpoint_id
                 OR new_source_task IS DISTINCT FROM OLD.active_turn_task_id
                 OR new_generation <> OLD.authority_generation
                 OR new_version <> old_version + 1 THEN
                RAISE EXCEPTION 'Resident checkpoint rotation is not authorized'
                  USING ERRCODE = '42501';
              END IF;
              RETURN NEW;
            END $$;
            """
        )
    else:
        op.execute(
            "DROP TRIGGER IF EXISTS trg_strategicresident_checkpoint_clear"
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_strategicresident_checkpoint_clear
            BEFORE UPDATE OF current_checkpoint_id ON {_RESIDENT_TABLE}
            FOR EACH ROW WHEN
              OLD.current_checkpoint_id IS NOT NULL
              AND NEW.current_checkpoint_id IS NULL
              AND EXISTS (SELECT 1 FROM project WHERE id = OLD.project_id)
            BEGIN
              SELECT RAISE(ABORT, 'Resident current checkpoint cannot be cleared');
            END
            """
        )

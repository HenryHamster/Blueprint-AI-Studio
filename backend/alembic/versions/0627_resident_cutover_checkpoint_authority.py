"""Authorize one-transaction legacy Resident cutover checkpoints.

Revision ID: 0627
Revises: 0626
Create Date: 2026-07-17

Revision 0611 was already deployed before the cutover exception was designed.
This forward-only repair preserves its ordinary authority fences and revision
0623's FK-cascade checkpoint-clear behavior while admitting only the exact,
transaction-local marker staged by the locked ownership transition.
"""

from __future__ import annotations

from alembic import op

# Keep the lowercase Alembic declarations literal: the standalone migration
# chain validator parses them without importing migration modules.
revision = "0627"
down_revision = "0626"
branch_labels = None
depends_on = None

_RESIDENT_TABLE = "strategicresident"
_CHECKPOINT_TABLE = "strategicresidentcheckpoint"
_AUTH_KEY = "_resident_cutover_checkpoint_authorization"


def _install_postgresql_validators() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION is_authorized_resident_cutover_checkpoint(
          candidate_project_id uuid,
          candidate_id uuid,
          candidate_version bigint,
          candidate_hash text,
          candidate_checkpoint jsonb,
          candidate_generation bigint,
          candidate_source_task_id uuid
        ) RETURNS boolean LANGUAGE plpgsql VOLATILE AS $$
        BEGIN
          IF candidate_checkpoint->'migration'->'cutover_boundary'
                ->>'previous_owner' IS DISTINCT FROM 'legacy' THEN
            RETURN false;
          END IF;
          PERFORM 1
            FROM {_RESIDENT_TABLE} resident
            JOIN {_CHECKPOINT_TABLE} prior_checkpoint
              ON prior_checkpoint.id = resident.current_checkpoint_id
             AND prior_checkpoint.project_id = resident.project_id
            WHERE resident.project_id = candidate_project_id
              AND resident.engine_owner = 'legacy'
              AND jsonb_typeof(resident.rollout_state->'{_AUTH_KEY}') = 'object'
              AND resident.authority_generation
                    = (resident.rollout_state->'{_AUTH_KEY}'
                       ->>'previous_authority_generation')::bigint
              AND resident.current_checkpoint_id::text
                    = resident.rollout_state->'{_AUTH_KEY}'
                      ->>'previous_checkpoint_id'
              AND resident.rollout_state->'{_AUTH_KEY}'->>'project_id'
                    = candidate_project_id::text
              AND resident.rollout_state->'{_AUTH_KEY}'->>'previous_owner'
                    = 'legacy'
              AND (resident.rollout_state->'{_AUTH_KEY}'
                    ->>'next_authority_generation')::bigint
                    = candidate_generation
              AND resident.rollout_state->'{_AUTH_KEY}'->>'checkpoint_id'
                    = candidate_id::text
              AND (resident.rollout_state->'{_AUTH_KEY}'
                    ->>'checkpoint_version')::bigint = candidate_version
              AND resident.rollout_state->'{_AUTH_KEY}'->>'checkpoint_hash'
                    = candidate_hash
              AND resident.rollout_state->'{_AUTH_KEY}'->>'source_turn_task_id'
                    = candidate_source_task_id::text
              AND (resident.rollout_state->'{_AUTH_KEY}'
                    ->>'previous_checkpoint_version')::bigint
                    = prior_checkpoint.version
              AND resident.rollout_state->'{_AUTH_KEY}'
                    ->>'previous_checkpoint_hash' = prior_checkpoint.content_hash
              AND candidate_version = prior_checkpoint.version + 1
              AND candidate_checkpoint->'migration'->'cutover_boundary'
                    ->>'transition_id'
                    = resident.rollout_state->'{_AUTH_KEY}'->>'transition_id'
              AND candidate_checkpoint->'migration'->'cutover_boundary'
                    ->>'previous_owner' = 'legacy'
              AND (candidate_checkpoint->'migration'->'cutover_boundary'
                    ->>'previous_authority_generation')::bigint
                    = resident.authority_generation
              AND candidate_checkpoint->'migration'->'cutover_boundary'
                    ->>'previous_checkpoint_id' = prior_checkpoint.id::text
              AND (candidate_checkpoint->'migration'->'cutover_boundary'
                    ->>'previous_checkpoint_version')::bigint
                    = prior_checkpoint.version
              AND candidate_checkpoint->'migration'->'cutover_boundary'
                    ->>'previous_checkpoint_hash' = prior_checkpoint.content_hash
              AND (candidate_checkpoint->'migration'->'cutover_boundary'
                    ->>'authority_generation')::bigint = candidate_generation
              AND candidate_checkpoint->'migration'->'cutover_boundary'
                    ->>'source_turn_task_id' = candidate_source_task_id::text
            FOR SHARE OF resident, prior_checkpoint;
          RETURN FOUND;
        END
        $$
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION is_authorized_resident_cutover_rotation(
          old_project_id uuid,
          old_owner text,
          old_generation bigint,
          old_current_checkpoint_id uuid,
          old_rollout_state jsonb,
          new_project_id uuid,
          new_owner text,
          new_generation bigint,
          new_current_checkpoint_id uuid,
          new_previous_checkpoint_id uuid,
          new_active_turn_task_id uuid,
          new_interactive_successor_task_id uuid,
          new_background_successor_task_id uuid,
          new_rollout_state jsonb
        ) RETURNS boolean LANGUAGE plpgsql VOLATILE AS $$
        BEGIN
          IF old_project_id IS DISTINCT FROM new_project_id
             OR old_owner IS DISTINCT FROM 'legacy'
             OR new_owner IS DISTINCT FROM 'strategic_resident'
             OR new_generation IS DISTINCT FROM old_generation + 1
             OR jsonb_typeof(old_rollout_state->'{_AUTH_KEY}')
                  IS DISTINCT FROM 'object'
             OR new_rollout_state ? '{_AUTH_KEY}'
             OR old_rollout_state->'{_AUTH_KEY}'->>'project_id'
                  IS DISTINCT FROM old_project_id::text
             OR old_rollout_state->'{_AUTH_KEY}'->>'previous_owner'
                  IS DISTINCT FROM 'legacy'
             OR (old_rollout_state->'{_AUTH_KEY}'
                  ->>'previous_authority_generation')::bigint
                  IS DISTINCT FROM old_generation
             OR (old_rollout_state->'{_AUTH_KEY}'
                  ->>'next_authority_generation')::bigint
                  IS DISTINCT FROM new_generation
             OR old_rollout_state->'{_AUTH_KEY}'->>'previous_checkpoint_id'
                  IS DISTINCT FROM old_current_checkpoint_id::text
             OR new_previous_checkpoint_id
                  IS DISTINCT FROM old_current_checkpoint_id
             OR old_rollout_state->'{_AUTH_KEY}'->>'checkpoint_id'
                  IS DISTINCT FROM new_current_checkpoint_id::text
             OR new_active_turn_task_id IS NOT NULL
             OR new_interactive_successor_task_id IS NOT NULL
             OR new_background_successor_task_id IS NOT NULL THEN
            RETURN false;
          END IF;
          PERFORM 1
              FROM {_CHECKPOINT_TABLE} prior_checkpoint
              JOIN {_CHECKPOINT_TABLE} cutover_checkpoint
                ON cutover_checkpoint.id = new_current_checkpoint_id
               AND cutover_checkpoint.project_id = new_project_id
              WHERE prior_checkpoint.id = old_current_checkpoint_id
                AND prior_checkpoint.project_id = old_project_id
                AND (old_rollout_state->'{_AUTH_KEY}'
                      ->>'previous_checkpoint_version')::bigint
                      = prior_checkpoint.version
                AND old_rollout_state->'{_AUTH_KEY}'
                      ->>'previous_checkpoint_hash' = prior_checkpoint.content_hash
                AND (old_rollout_state->'{_AUTH_KEY}'
                      ->>'checkpoint_version')::bigint
                      = cutover_checkpoint.version
                AND cutover_checkpoint.version = prior_checkpoint.version + 1
                AND cutover_checkpoint.authority_generation = new_generation
                AND cutover_checkpoint.content_hash
                      = old_rollout_state->'{_AUTH_KEY}'->>'checkpoint_hash'
                AND cutover_checkpoint.source_turn_task_id::text
                      = old_rollout_state->'{_AUTH_KEY}'->>'source_turn_task_id'
                AND cutover_checkpoint.checkpoint->'migration'
                      ->'cutover_boundary'->>'transition_id'
                      = old_rollout_state->'{_AUTH_KEY}'->>'transition_id'
                AND cutover_checkpoint.checkpoint->'migration'
                      ->'cutover_boundary'->>'previous_owner' = 'legacy'
                AND (cutover_checkpoint.checkpoint->'migration'
                      ->'cutover_boundary'
                      ->>'previous_authority_generation')::bigint = old_generation
                AND cutover_checkpoint.checkpoint->'migration'
                      ->'cutover_boundary'->>'previous_checkpoint_id'
                      = prior_checkpoint.id::text
                AND (cutover_checkpoint.checkpoint->'migration'
                      ->'cutover_boundary'
                      ->>'previous_checkpoint_version')::bigint
                      = prior_checkpoint.version
                AND cutover_checkpoint.checkpoint->'migration'
                      ->'cutover_boundary'->>'previous_checkpoint_hash'
                      = prior_checkpoint.content_hash
                AND (cutover_checkpoint.checkpoint->'migration'
                      ->'cutover_boundary'->>'authority_generation')::bigint
                      = new_generation
                AND cutover_checkpoint.checkpoint->'migration'
                      ->'cutover_boundary'->>'source_turn_task_id'
                      = cutover_checkpoint.source_turn_task_id::text
              FOR SHARE OF prior_checkpoint, cutover_checkpoint;
          RETURN FOUND;
        END
        $$
        """
    )


def _install_postgresql_triggers(*, allow_cutover: bool) -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_strategicresidentcheckpoint_authority "
        f"ON {_CHECKPOINT_TABLE}"
    )
    authority_when = (
        """
        WHEN (NOT is_authorized_resident_cutover_checkpoint(
          NEW.project_id,
          NEW.id,
          NEW.version,
          NEW.content_hash,
          NEW.checkpoint,
          NEW.authority_generation,
          NEW.source_turn_task_id
        ))
        """
        if allow_cutover
        else ""
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicresidentcheckpoint_authority
        BEFORE INSERT ON {_CHECKPOINT_TABLE}
        FOR EACH ROW
        {authority_when}
        EXECUTE FUNCTION fence_strategicresidentcheckpoint_insert()
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_strategicresident_checkpoint_rotation "
        f"ON {_RESIDENT_TABLE}"
    )
    rotation_when = (
        """
        WHEN (NOT is_authorized_resident_cutover_rotation(
          OLD.project_id,
          OLD.engine_owner,
          OLD.authority_generation,
          OLD.current_checkpoint_id,
          OLD.rollout_state,
          NEW.project_id,
          NEW.engine_owner,
          NEW.authority_generation,
          NEW.current_checkpoint_id,
          NEW.previous_checkpoint_id,
          NEW.active_turn_task_id,
          NEW.interactive_successor_task_id,
          NEW.background_successor_task_id,
          NEW.rollout_state
        ))
        """
        if allow_cutover
        else ""
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicresident_checkpoint_rotation
        BEFORE UPDATE OF current_checkpoint_id, previous_checkpoint_id
        ON {_RESIDENT_TABLE}
        FOR EACH ROW
        {rotation_when}
        EXECUTE FUNCTION fence_strategicresident_checkpoint_rotation()
        """
    )


def _sqlite_authority_cutover_guard() -> str:
    return f"""
      AND NOT EXISTS (
        SELECT 1
        FROM {_RESIDENT_TABLE} resident
        JOIN {_CHECKPOINT_TABLE} prior_checkpoint
          ON prior_checkpoint.id = resident.current_checkpoint_id
         AND prior_checkpoint.project_id = resident.project_id
        WHERE resident.project_id = NEW.project_id
          AND resident.engine_owner = 'legacy'
          AND json_type(resident.rollout_state, '$.{_AUTH_KEY}') = 'object'
          AND CAST(json_extract(resident.rollout_state,
                '$.{_AUTH_KEY}.previous_authority_generation') AS INTEGER)
                = resident.authority_generation
          AND CAST(json_extract(resident.rollout_state,
                '$.{_AUTH_KEY}.next_authority_generation') AS INTEGER)
                = NEW.authority_generation
          AND replace(json_extract(resident.rollout_state,
                '$.{_AUTH_KEY}.project_id'), '-', '')
                = replace(CAST(NEW.project_id AS TEXT), '-', '')
          AND json_extract(resident.rollout_state,
                '$.{_AUTH_KEY}.previous_owner') = 'legacy'
          AND replace(json_extract(resident.rollout_state,
                '$.{_AUTH_KEY}.previous_checkpoint_id'), '-', '')
                = replace(CAST(prior_checkpoint.id AS TEXT), '-', '')
          AND CAST(json_extract(resident.rollout_state,
                '$.{_AUTH_KEY}.previous_checkpoint_version') AS INTEGER)
                = prior_checkpoint.version
          AND json_extract(resident.rollout_state,
                '$.{_AUTH_KEY}.previous_checkpoint_hash')
                = prior_checkpoint.content_hash
          AND replace(json_extract(resident.rollout_state,
                '$.{_AUTH_KEY}.checkpoint_id'), '-', '')
                = replace(CAST(NEW.id AS TEXT), '-', '')
          AND CAST(json_extract(resident.rollout_state,
                '$.{_AUTH_KEY}.checkpoint_version') AS INTEGER) = NEW.version
          AND json_extract(resident.rollout_state,
                '$.{_AUTH_KEY}.checkpoint_hash') = NEW.content_hash
          AND replace(json_extract(resident.rollout_state,
                '$.{_AUTH_KEY}.source_turn_task_id'), '-', '')
                = replace(CAST(NEW.source_turn_task_id AS TEXT), '-', '')
          AND NEW.version = prior_checkpoint.version + 1
          AND json_extract(NEW.checkpoint,
                '$.migration.cutover_boundary.transition_id')
                = json_extract(resident.rollout_state,
                  '$.{_AUTH_KEY}.transition_id')
          AND json_extract(NEW.checkpoint,
                '$.migration.cutover_boundary.previous_owner') = 'legacy'
          AND CAST(json_extract(NEW.checkpoint,
                '$.migration.cutover_boundary.previous_authority_generation')
                AS INTEGER) = resident.authority_generation
          AND replace(json_extract(NEW.checkpoint,
                '$.migration.cutover_boundary.previous_checkpoint_id'), '-', '')
                = replace(CAST(prior_checkpoint.id AS TEXT), '-', '')
          AND CAST(json_extract(NEW.checkpoint,
                '$.migration.cutover_boundary.previous_checkpoint_version')
                AS INTEGER) = prior_checkpoint.version
          AND json_extract(NEW.checkpoint,
                '$.migration.cutover_boundary.previous_checkpoint_hash')
                = prior_checkpoint.content_hash
          AND CAST(json_extract(NEW.checkpoint,
                '$.migration.cutover_boundary.authority_generation') AS INTEGER)
                = NEW.authority_generation
          AND replace(json_extract(NEW.checkpoint,
                '$.migration.cutover_boundary.source_turn_task_id'), '-', '')
                = replace(CAST(NEW.source_turn_task_id AS TEXT), '-', '')
      )
    """


def _sqlite_rotation_cutover_guard() -> str:
    return f"""
      AND NOT (
        OLD.engine_owner = 'legacy'
        AND NEW.engine_owner = 'strategic_resident'
        AND NEW.authority_generation = OLD.authority_generation + 1
        AND json_type(OLD.rollout_state, '$.{_AUTH_KEY}') = 'object'
        AND json_type(NEW.rollout_state, '$.{_AUTH_KEY}') IS NULL
        AND replace(json_extract(OLD.rollout_state,
              '$.{_AUTH_KEY}.project_id'), '-', '')
              = replace(CAST(OLD.project_id AS TEXT), '-', '')
        AND json_extract(OLD.rollout_state,
              '$.{_AUTH_KEY}.previous_owner') = 'legacy'
        AND CAST(json_extract(OLD.rollout_state,
              '$.{_AUTH_KEY}.previous_authority_generation') AS INTEGER)
              = OLD.authority_generation
        AND CAST(json_extract(OLD.rollout_state,
              '$.{_AUTH_KEY}.next_authority_generation') AS INTEGER)
              = NEW.authority_generation
        AND replace(json_extract(OLD.rollout_state,
              '$.{_AUTH_KEY}.previous_checkpoint_id'), '-', '')
              = replace(CAST(OLD.current_checkpoint_id AS TEXT), '-', '')
        AND NEW.previous_checkpoint_id = OLD.current_checkpoint_id
        AND replace(json_extract(OLD.rollout_state,
              '$.{_AUTH_KEY}.checkpoint_id'), '-', '')
              = replace(CAST(NEW.current_checkpoint_id AS TEXT), '-', '')
        AND NEW.active_turn_task_id IS NULL
        AND NEW.interactive_successor_task_id IS NULL
        AND NEW.background_successor_task_id IS NULL
        AND EXISTS (
          SELECT 1
          FROM {_CHECKPOINT_TABLE} prior_checkpoint
          JOIN {_CHECKPOINT_TABLE} cutover_checkpoint
            ON cutover_checkpoint.id = NEW.current_checkpoint_id
           AND cutover_checkpoint.project_id = NEW.project_id
          WHERE prior_checkpoint.id = OLD.current_checkpoint_id
            AND prior_checkpoint.project_id = OLD.project_id
            AND CAST(json_extract(OLD.rollout_state,
                  '$.{_AUTH_KEY}.previous_checkpoint_version') AS INTEGER)
                  = prior_checkpoint.version
            AND json_extract(OLD.rollout_state,
                  '$.{_AUTH_KEY}.previous_checkpoint_hash')
                  = prior_checkpoint.content_hash
            AND CAST(json_extract(OLD.rollout_state,
                  '$.{_AUTH_KEY}.checkpoint_version') AS INTEGER)
                  = cutover_checkpoint.version
            AND cutover_checkpoint.version = prior_checkpoint.version + 1
            AND cutover_checkpoint.authority_generation
                  = NEW.authority_generation
            AND cutover_checkpoint.content_hash = json_extract(
                  OLD.rollout_state, '$.{_AUTH_KEY}.checkpoint_hash')
            AND replace(CAST(cutover_checkpoint.source_turn_task_id AS TEXT),
                  '-', '') = replace(json_extract(OLD.rollout_state,
                    '$.{_AUTH_KEY}.source_turn_task_id'), '-', '')
            AND json_extract(cutover_checkpoint.checkpoint,
                  '$.migration.cutover_boundary.transition_id')
                  = json_extract(OLD.rollout_state,
                    '$.{_AUTH_KEY}.transition_id')
            AND json_extract(cutover_checkpoint.checkpoint,
                  '$.migration.cutover_boundary.previous_owner') = 'legacy'
            AND CAST(json_extract(cutover_checkpoint.checkpoint,
                  '$.migration.cutover_boundary.previous_authority_generation')
                  AS INTEGER) = OLD.authority_generation
            AND replace(json_extract(cutover_checkpoint.checkpoint,
                  '$.migration.cutover_boundary.previous_checkpoint_id'), '-', '')
                  = replace(CAST(prior_checkpoint.id AS TEXT), '-', '')
            AND CAST(json_extract(cutover_checkpoint.checkpoint,
                  '$.migration.cutover_boundary.previous_checkpoint_version')
                  AS INTEGER) = prior_checkpoint.version
            AND json_extract(cutover_checkpoint.checkpoint,
                  '$.migration.cutover_boundary.previous_checkpoint_hash')
                  = prior_checkpoint.content_hash
            AND CAST(json_extract(cutover_checkpoint.checkpoint,
                  '$.migration.cutover_boundary.authority_generation') AS INTEGER)
                  = NEW.authority_generation
            AND replace(json_extract(cutover_checkpoint.checkpoint,
                  '$.migration.cutover_boundary.source_turn_task_id'), '-', '')
                  = replace(CAST(cutover_checkpoint.source_turn_task_id AS TEXT),
                    '-', '')
        )
      )
    """


def _install_sqlite_authority(*, allow_cutover: bool) -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_strategicresidentcheckpoint_authority")
    cutover_guard = _sqlite_authority_cutover_guard() if allow_cutover else ""
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicresidentcheckpoint_authority
        BEFORE INSERT ON {_CHECKPOINT_TABLE}
        FOR EACH ROW WHEN NEW.version > 1
        {cutover_guard}
        AND NOT EXISTS (
          SELECT 1
          FROM {_RESIDENT_TABLE} resident
          JOIN task turn_task ON turn_task.id = NEW.source_turn_task_id
          JOIN {_CHECKPOINT_TABLE} current_checkpoint
            ON current_checkpoint.id = resident.current_checkpoint_id
           AND current_checkpoint.project_id = resident.project_id
          WHERE resident.project_id = NEW.project_id
            AND resident.engine_owner = 'strategic_resident'
            AND resident.authority_generation = NEW.authority_generation
            AND resident.active_turn_task_id = NEW.source_turn_task_id
            AND NEW.version = current_checkpoint.version + 1
            AND turn_task.project_id = NEW.project_id
            AND json_extract(turn_task.payloads, '$[0].type')
                  = 'strategic_resident_turn'
            AND replace(json_extract(turn_task.payloads, '$[0].resident_id'),
                  '-', '') = replace(CAST(NEW.project_id AS TEXT), '-', '')
            AND CAST(json_extract(turn_task.payloads,
                  '$[0].authority_generation') AS INTEGER)
                  = NEW.authority_generation
            AND json_extract(turn_task.payloads, '$[0].engine_owner')
                  = 'strategic_resident'
            AND CAST(json_extract(turn_task.payloads,
                  '$[0].checkpoint_version') AS INTEGER)
                  = current_checkpoint.version
            AND json_extract(turn_task.payloads, '$[0].checkpoint_hash')
                  = current_checkpoint.content_hash
            AND turn_task.status = 'IN_PROGRESS'
            AND json_extract(turn_task.context,
                  '$._resident_mutation_authorized') = 1
            AND json_extract(turn_task.context, '$._resident_input_frozen') = 1
            AND replace(json_extract(turn_task.context,
                  '$._resident_project_id'), '-', '')
                  = replace(CAST(NEW.project_id AS TEXT), '-', '')
            AND CAST(json_extract(turn_task.context,
                  '$._resident_authority_generation') AS INTEGER)
                  = NEW.authority_generation
            AND json_extract(turn_task.context,
                  '$._resident_engine_owner_observed') = 'strategic_resident'
        )
        BEGIN
          SELECT RAISE(ABORT, 'Resident checkpoint authority is stale or invalid');
        END
        """
    )


def _install_sqlite_rotation(*, allow_cutover: bool) -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_strategicresident_checkpoint_rotation")
    generation_guard = (
        "" if allow_cutover
        else "AND NEW.authority_generation = OLD.authority_generation"
    )
    cutover_guard = _sqlite_rotation_cutover_guard() if allow_cutover else ""
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicresident_checkpoint_rotation
        BEFORE UPDATE OF current_checkpoint_id, previous_checkpoint_id
        ON {_RESIDENT_TABLE}
        FOR EACH ROW WHEN
          (NEW.current_checkpoint_id IS NOT OLD.current_checkpoint_id
           OR NEW.previous_checkpoint_id IS NOT OLD.previous_checkpoint_id)
          AND NEW.current_checkpoint_id IS NOT NULL
          {generation_guard}
          AND NOT (
            OLD.engine_owner = 'legacy'
            AND OLD.current_checkpoint_id IS NULL
            AND NEW.previous_checkpoint_id IS NULL
            AND EXISTS (
              SELECT 1 FROM {_CHECKPOINT_TABLE} initial_checkpoint
              WHERE initial_checkpoint.id = NEW.current_checkpoint_id
                AND initial_checkpoint.project_id = NEW.project_id
                AND initial_checkpoint.version = 1
                AND initial_checkpoint.authority_generation
                      = OLD.authority_generation
            )
          )
          {cutover_guard}
          AND NOT (
            NEW.authority_generation = OLD.authority_generation
            AND EXISTS (
              SELECT 1
              FROM task turn_task
              JOIN {_CHECKPOINT_TABLE} old_checkpoint
                ON old_checkpoint.id = OLD.current_checkpoint_id
               AND old_checkpoint.project_id = OLD.project_id
              JOIN {_CHECKPOINT_TABLE} new_checkpoint
                ON new_checkpoint.id = NEW.current_checkpoint_id
               AND new_checkpoint.project_id = NEW.project_id
              WHERE OLD.engine_owner = 'strategic_resident'
                AND turn_task.id = OLD.active_turn_task_id
                AND turn_task.project_id = OLD.project_id
                AND json_extract(turn_task.payloads, '$[0].type')
                      = 'strategic_resident_turn'
                AND replace(json_extract(turn_task.payloads,
                      '$[0].resident_id'), '-', '')
                      = replace(CAST(OLD.project_id AS TEXT), '-', '')
                AND CAST(json_extract(turn_task.payloads,
                      '$[0].authority_generation') AS INTEGER)
                      = OLD.authority_generation
                AND json_extract(turn_task.payloads, '$[0].engine_owner')
                      = 'strategic_resident'
                AND CAST(json_extract(turn_task.payloads,
                      '$[0].checkpoint_version') AS INTEGER)
                      = old_checkpoint.version
                AND json_extract(turn_task.payloads, '$[0].checkpoint_hash')
                      = old_checkpoint.content_hash
                AND turn_task.status = 'IN_PROGRESS'
                AND json_extract(turn_task.context,
                      '$._resident_mutation_authorized') = 1
                AND json_extract(turn_task.context,
                      '$._resident_input_frozen') = 1
                AND replace(json_extract(turn_task.context,
                      '$._resident_project_id'), '-', '')
                      = replace(CAST(OLD.project_id AS TEXT), '-', '')
                AND CAST(json_extract(turn_task.context,
                      '$._resident_authority_generation') AS INTEGER)
                      = OLD.authority_generation
                AND json_extract(turn_task.context,
                      '$._resident_engine_owner_observed')
                      = 'strategic_resident'
                AND NEW.previous_checkpoint_id = OLD.current_checkpoint_id
                AND new_checkpoint.version = old_checkpoint.version + 1
                AND new_checkpoint.source_turn_task_id
                      = OLD.active_turn_task_id
                AND new_checkpoint.authority_generation
                      = OLD.authority_generation
            )
          )
        BEGIN
          SELECT RAISE(ABORT, 'Resident checkpoint rotation is not authorized');
        END
        """
    )


def _install_sqlite_checkpoint_clear() -> None:
    # Preserve revision 0623's FK SET NULL escape on both migration directions.
    op.execute("DROP TRIGGER IF EXISTS trg_strategicresident_checkpoint_clear")
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


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _install_postgresql_validators()
        _install_postgresql_triggers(allow_cutover=True)
        return
    _install_sqlite_authority(allow_cutover=True)
    _install_sqlite_rotation(allow_cutover=True)
    _install_sqlite_checkpoint_clear()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _install_postgresql_triggers(allow_cutover=False)
        op.execute(
            "DROP FUNCTION IF EXISTS is_authorized_resident_cutover_rotation("
            "uuid,text,bigint,uuid,jsonb,uuid,text,bigint,uuid,uuid,uuid,uuid,uuid,jsonb)"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS is_authorized_resident_cutover_checkpoint("
            "uuid,uuid,bigint,text,jsonb,bigint,uuid)"
        )
        return
    _install_sqlite_authority(allow_cutover=False)
    _install_sqlite_rotation(allow_cutover=False)
    _install_sqlite_checkpoint_clear()

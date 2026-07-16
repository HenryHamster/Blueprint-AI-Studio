"""Add the durable Resident message foundation without a long table lock.

PostgreSQL rollout is deliberately phased:

* metadata-only nullable/defaulted columns are committed immediately;
* deterministic projection values are computed set-wise in a temporary table;
* rows are rewritten in bounded autocommit batches;
* indexes are built concurrently; and
* foreign keys/checks are installed NOT VALID, then validated separately.

``sequence`` remains nullable while the legacy-writer compatibility soak runs.

Revision ID: 0608
Revises: 0607
Create Date: 2026-07-15
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0608"
down_revision = "0607"
branch_labels = None
depends_on = None

_CONVERSATION = "conversation"
_ACTIVITY = "activity"
_PENDING = "pending"
_RESPONDED = "responded"
_BACKFILL_BATCH_SIZE = 2_000
_COMPAT_SEQUENCE_FLOOR = 1_000_000_000_000
_SEQUENCE_TABLE = "projectmessagesequence"
_ATTACHMENT_UPLOAD_TABLE = "projectmessageattachmentupload"

# Explicit operator-facing legacy NOTE events. Message type alone is not enough:
# pipeline BUG_REPORT/BLOCKER rows remain activity unless their event contract
# says the operator must see them.
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


def _context_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _bounded_actor(context: Mapping[str, Any]) -> str | None:
    """Return only explicit legacy actor evidence; never guess project owner."""

    candidates = [context.get("operator_user_id")]
    check_in = context.get("check_in")
    if isinstance(check_in, dict):
        candidates.append(check_in.get("operator_user_id"))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()[:255]
    return None


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _build_backfill_values(
    rows: Iterable[Mapping[str, Any]],
    *,
    task_project_by_id: Mapping[uuid.UUID, uuid.UUID] | None = None,
) -> list[dict[str, Any]]:
    """Reference implementation used by dialect/focused tests.

    Production PostgreSQL does not call this helper; it performs the equivalent
    projection set-wise and rewrites rows in bounded batches.
    """

    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda row: (
            str(row.get("project_id") or ""),
            str(row.get("created_at") or ""),
            str(row.get("id") or ""),
        ),
    )
    project_by_id = {
        _uuid_or_none(row.get("id")): _uuid_or_none(row.get("project_id"))
        for row in ordered
    }
    project_by_id.pop(None, None)

    next_sequence: dict[uuid.UUID | None, int] = {}
    values: list[dict[str, Any]] = []
    for row in ordered:
        project_id = _uuid_or_none(row.get("project_id"))
        sequence = next_sequence.get(project_id, 0) + 1
        next_sequence[project_id] = sequence
        context = _context_dict(row.get("context"))
        message_id = _uuid_or_none(row.get("id"))
        reply_id = _uuid_or_none(context.get("reply_to"))
        if (
            reply_id is None
            or reply_id == message_id
            or project_by_id.get(reply_id) != project_id
        ):
            reply_id = None

        from_type = str(row.get("from_type") or "")
        event_type = str(context.get("event_type") or "")
        surface = (
            _CONVERSATION
            if from_type in {"user", "strategic"}
            or event_type in _HIGH_SIGNAL_EVENT_TYPES
            or reply_id is not None
            else _ACTIVITY
        )
        # Operational linked/resolved status is intentionally not Resident
        # handling evidence. Only an actual strategic reply is responded.
        handling_state = _RESPONDED if from_type == "strategic" else _PENDING
        values.append(
            {
                "row_id": message_id,
                "sequence": sequence,
                "actor_user_id": _bounded_actor(context),
                "surface": surface,
                "handling_state": handling_state,
                "handled_by_task_id": None,
                "reply_to_message_id": reply_id,
                "updated_at": row.get("resolved_at") or row.get("created_at"),
                "from_type": from_type,
                "from_task_id": _uuid_or_none(row.get("from_task_id")),
                "created_at": row.get("created_at"),
            }
        )

    by_id = {value["row_id"]: value for value in values}
    for reply in sorted(
        values,
        key=lambda value: (str(value.get("created_at") or ""), str(value["row_id"])),
    ):
        target = by_id.get(reply["reply_to_message_id"])
        if target is None or reply["from_type"] != "strategic":
            continue
        target["handling_state"] = _RESPONDED
        task_id = reply["from_task_id"]
        task_is_valid = (
            task_id is not None
            and (
                task_project_by_id is None
                or task_project_by_id.get(task_id)
                == project_by_id.get(reply["row_id"])
            )
        )
        if target["handled_by_task_id"] is None and task_is_valid:
            target["handled_by_task_id"] = reply["from_task_id"]

    for value in values:
        value.pop("from_type")
        value.pop("from_task_id")
        value.pop("created_at")
    return values


def _build_restartable_sequence_values(
    rows: Iterable[Mapping[str, Any]],
) -> dict[uuid.UUID, int]:
    """Reference the PostgreSQL restart algorithm for focused tests.

    A multi-commit retry must never renumber the already-committed low-range
    prefix. Remaining NULL rows append after that prefix; compatibility rows in
    the reserved range do not affect historical numbering.
    """

    materialized = [dict(row) for row in rows]
    low_prefix: dict[uuid.UUID | None, int] = {}
    for row in materialized:
        sequence = row.get("sequence")
        if (
            isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and 0 < sequence < _COMPAT_SEQUENCE_FLOOR
        ):
            project_id = _uuid_or_none(row.get("project_id"))
            low_prefix[project_id] = max(low_prefix.get(project_id, 0), sequence)

    remaining = sorted(
        (row for row in materialized if row.get("sequence") is None),
        key=lambda row: (
            str(row.get("project_id") or ""),
            str(row.get("created_at") or ""),
            str(row.get("id") or ""),
        ),
    )
    next_sequence = dict(low_prefix)
    assigned: dict[uuid.UUID, int] = {}
    for row in remaining:
        project_id = _uuid_or_none(row.get("project_id"))
        sequence = next_sequence.get(project_id, 0) + 1
        if sequence >= _COMPAT_SEQUENCE_FLOOR:
            raise ValueError("historical sequence range exhausted")
        next_sequence[project_id] = sequence
        row_id = _uuid_or_none(row.get("id"))
        if row_id is not None:
            assigned[row_id] = sequence
    return assigned


def _add_foundation_tables(*, postgresql: bool) -> None:
    """Create the allocator and upload-receipt tables restartably."""

    if postgresql:
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_SEQUENCE_TABLE} (
              project_id uuid PRIMARY KEY
                REFERENCES project(id) ON DELETE CASCADE,
              last_sequence bigint NOT NULL DEFAULT 0,
              CONSTRAINT chk_projectmessagesequence_nonnegative
                CHECK (last_sequence >= 0)
            )
            """
        )
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_ATTACHMENT_UPLOAD_TABLE} (
              attachment_id uuid PRIMARY KEY,
              project_id uuid NOT NULL
                REFERENCES project(id) ON DELETE CASCADE,
              actor_user_id varchar(255) NOT NULL,
              storage_uri text NOT NULL,
              filename varchar(512) NOT NULL,
              content_type varchar(128),
              expected_size_bytes bigint NOT NULL,
              expires_at timestamptz NOT NULL,
              consumed_at timestamptz,
              message_id uuid
                REFERENCES projectmessage(id) ON DELETE SET NULL,
              created_at timestamptz NOT NULL DEFAULT now(),
              CONSTRAINT chk_projectmessageattachmentupload_size_nonnegative
                CHECK (expected_size_bytes >= 0),
              CONSTRAINT chk_projectmessageattachmentupload_expiry_after_create
                CHECK (expires_at > created_at)
            )
            """
        )
        for statement in (
            f"CREATE INDEX IF NOT EXISTS ix_projectmessageattachmentupload_project_expires "
            f"ON {_ATTACHMENT_UPLOAD_TABLE} (project_id, expires_at)",
            f"CREATE INDEX IF NOT EXISTS ix_projectmessageattachmentupload_project_actor_created "
            f"ON {_ATTACHMENT_UPLOAD_TABLE} (project_id, actor_user_id, created_at)",
            f"CREATE INDEX IF NOT EXISTS ix_projectmessageattachmentupload_message_id "
            f"ON {_ATTACHMENT_UPLOAD_TABLE} (message_id)",
            f"CREATE INDEX IF NOT EXISTS ix_projectmessageattachmentupload_consumed_expires "
            f"ON {_ATTACHMENT_UPLOAD_TABLE} (consumed_at, expires_at)",
        ):
            op.execute(statement)
        return

    op.create_table(
        _SEQUENCE_TABLE,
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "last_sequence >= 0",
            name="chk_projectmessagesequence_nonnegative",
        ),
    )
    op.create_table(
        _ATTACHMENT_UPLOAD_TABLE,
        sa.Column("attachment_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor_user_id", sa.String(255), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("content_type", sa.String(128)),
        sa.Column("expected_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "message_id",
            sa.Uuid(),
            sa.ForeignKey("projectmessage.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint(
            "expected_size_bytes >= 0",
            name="chk_projectmessageattachmentupload_size_nonnegative",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="chk_projectmessageattachmentupload_expiry_after_create",
        ),
    )
    op.create_index(
        "ix_projectmessageattachmentupload_project_expires",
        _ATTACHMENT_UPLOAD_TABLE,
        ["project_id", "expires_at"],
    )
    op.create_index(
        "ix_projectmessageattachmentupload_project_actor_created",
        _ATTACHMENT_UPLOAD_TABLE,
        ["project_id", "actor_user_id", "created_at"],
    )
    op.create_index(
        "ix_projectmessageattachmentupload_message_id",
        _ATTACHMENT_UPLOAD_TABLE,
        ["message_id"],
    )
    op.create_index(
        "ix_projectmessageattachmentupload_consumed_expires",
        _ATTACHMENT_UPLOAD_TABLE,
        ["consumed_at", "expires_at"],
    )


def _seed_sequence_counters(*, postgresql: bool) -> None:
    """Reserve the high live range for existing projects before backfill."""

    if postgresql:
        op.execute(
            f"""
            INSERT INTO {_SEQUENCE_TABLE} (project_id, last_sequence)
            SELECT project.id, {_COMPAT_SEQUENCE_FLOOR - 1}::bigint
            FROM project
            ON CONFLICT (project_id) DO UPDATE
            SET last_sequence = greatest(
              {_SEQUENCE_TABLE}.last_sequence,
              EXCLUDED.last_sequence
            )
            """
        )
        return
    op.execute(
        f"""
        INSERT INTO {_SEQUENCE_TABLE} (project_id, last_sequence)
        SELECT project.id, {_COMPAT_SEQUENCE_FLOOR - 1}
        FROM project
        WHERE 1 = 1
        ON CONFLICT(project_id) DO UPDATE SET
          last_sequence = max(
            {_SEQUENCE_TABLE}.last_sequence,
            excluded.last_sequence
          )
        """
    )


def _add_columns(*, postgresql: bool) -> None:
    if postgresql:
        # IF NOT EXISTS makes the intentionally multi-commit migration restartable.
        op.execute(
            """
            ALTER TABLE projectmessage
              ADD COLUMN IF NOT EXISTS actor_user_id varchar(255),
              ADD COLUMN IF NOT EXISTS sequence bigint,
              ADD COLUMN IF NOT EXISTS surface varchar(32) NOT NULL DEFAULT 'activity',
              ADD COLUMN IF NOT EXISTS handling_state varchar(32) NOT NULL DEFAULT 'pending',
              ADD COLUMN IF NOT EXISTS handled_by_task_id uuid,
              ADD COLUMN IF NOT EXISTS reply_to_message_id uuid,
              ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()
            """
        )
        return
    op.add_column("projectmessage", sa.Column("actor_user_id", sa.String(255)))
    op.add_column("projectmessage", sa.Column("sequence", sa.BigInteger()))
    op.add_column(
        "projectmessage",
        sa.Column("surface", sa.String(32), nullable=False, server_default="activity"),
    )
    op.add_column(
        "projectmessage",
        sa.Column("handling_state", sa.String(32), nullable=False, server_default="pending"),
    )
    op.add_column("projectmessage", sa.Column("handled_by_task_id", sa.Uuid()))
    op.add_column("projectmessage", sa.Column("reply_to_message_id", sa.Uuid()))
    op.add_column(
        "projectmessage",
        # SQLite cannot ALTER-add a non-constant CURRENT_TIMESTAMP default.
        # Backfill first, then make this non-null/defaulted in the batch rebuild.
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def _install_postgresql_compatibility_trigger() -> None:
    """Fence mixed-version inserts/updates before releasing the DDL lock.

    The reserved range cannot collide with deterministic historical row numbers.
    It remains installed through the rollback soak; fully migrated writers that
    atomically advanced the counter and supply that exact value pass through.
    """

    high_signal_sql = ", ".join(
        "'" + value.replace("'", "''") + "'" for value in sorted(_HIGH_SIGNAL_EVENT_TYPES)
    )
    uuid_pattern = (
        "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
        "[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
    )
    op.execute(
        f"""
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
          IF should_project THEN
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
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_projectmessage_0567_compat_insert ON projectmessage"
    )
    op.execute(
        """
        CREATE TRIGGER trg_projectmessage_0567_compat_insert
        BEFORE INSERT OR UPDATE OF project_id, reply_to_message_id,
          handled_by_task_id, sequence, surface, from_type, context
        ON projectmessage
        FOR EACH ROW EXECUTE FUNCTION projectmessage_0567_compat_insert()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_task_projectmessage_links()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.project_id IS DISTINCT FROM OLD.project_id AND EXISTS (
            SELECT 1 FROM projectmessage message
            WHERE message.handled_by_task_id = NEW.id
              AND message.project_id <> NEW.project_id
          ) THEN
            RAISE EXCEPTION 'moving task would strand cross-project message handlers'
              USING ERRCODE = '23503';
          END IF;
          RETURN NEW;
        END $$
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_task_projectmessage_links ON task")
    op.execute(
        """
        CREATE TRIGGER trg_task_projectmessage_links
        BEFORE UPDATE OF project_id ON task
        FOR EACH ROW EXECUTE FUNCTION enforce_task_projectmessage_links()
        """
    )


def _postgresql_backfill() -> None:
    high_signal_sql = ", ".join(
        "'" + value.replace("'", "''") + "'" for value in sorted(_HIGH_SIGNAL_EVENT_TYPES)
    )
    uuid_pattern = (
        "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
        "[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
    )
    op.execute("DROP TABLE IF EXISTS projectmessage_0567_backfill")
    op.execute(
        f"""
        CREATE TEMP TABLE projectmessage_0567_backfill
        ON COMMIT PRESERVE ROWS AS
        WITH low_prefix AS (
          SELECT
            project_id,
            coalesce(max(sequence) FILTER (
              WHERE sequence > 0 AND sequence < {_COMPAT_SEQUENCE_FLOOR}
            ), 0)::bigint AS last_sequence
          FROM projectmessage
          GROUP BY project_id
        ), source AS (
          SELECT
            m.*,
            CASE
              WHEN coalesce(m.context->>'reply_to', '') ~ '{uuid_pattern}'
              THEN (m.context->>'reply_to')::uuid
            END AS candidate_reply_id
          FROM projectmessage AS m
          WHERE m.sequence IS NULL
        ), projected AS (
          SELECT
            s.id AS row_id,
            s.project_id,
            coalesce(prefix.last_sequence, 0) + row_number() OVER (
              PARTITION BY s.project_id ORDER BY s.created_at, s.id
            )::bigint AS sequence,
            left(nullif(btrim(coalesce(
              s.context->>'operator_user_id',
              s.context->'check_in'->>'operator_user_id'
            )), ''), 255) AS actor_user_id,
            CASE
              WHEN s.from_type IN ('user', 'strategic')
                OR coalesce(s.context->>'event_type', '') IN ({high_signal_sql})
                OR target.id IS NOT NULL
              THEN 'conversation' ELSE 'activity'
            END::varchar(32) AS surface,
            CASE WHEN s.from_type = 'strategic'
              THEN 'responded' ELSE 'pending'
            END::varchar(32) AS handling_state,
            NULL::uuid AS handled_by_task_id,
            target.id AS reply_to_message_id,
            coalesce(s.resolved_at, s.created_at, now()) AS updated_at,
            s.created_at,
            s.from_type,
            s.from_task_id
          FROM source AS s
          LEFT JOIN low_prefix AS prefix
            ON prefix.project_id = s.project_id
          LEFT JOIN projectmessage AS target
            ON target.id = s.candidate_reply_id
           AND target.project_id = s.project_id
           AND target.id <> s.id
        )
        SELECT * FROM projected
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX projectmessage_0567_backfill_row_id_idx "
        "ON projectmessage_0567_backfill (row_id)"
    )
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM projectmessage_0567_backfill "
            "WHERE sequence >= :compat_floor)"
        ),
        {"compat_floor": _COMPAT_SEQUENCE_FLOOR},
    ):
        raise RuntimeError("historical ProjectMessage sequence range exhausted")
    op.execute(
        """
        WITH earliest_reply AS (
          SELECT DISTINCT ON (reply_to_message_id)
            reply_to_message_id AS target_id,
            from_task_id
          FROM projectmessage_0567_backfill
          WHERE reply_to_message_id IS NOT NULL
            AND from_type = 'strategic'
          ORDER BY reply_to_message_id, created_at, row_id
        )
        UPDATE projectmessage_0567_backfill AS target
        SET handling_state = 'responded',
            handled_by_task_id = reply.from_task_id
        FROM earliest_reply AS reply
        WHERE target.row_id = reply.target_id
        """
    )
    while True:
        op.get_bind().execute(
            sa.text(
                """
                WITH batch AS (
                  DELETE FROM projectmessage_0567_backfill
                  WHERE row_id IN (
                    SELECT row_id
                    FROM projectmessage_0567_backfill
                    ORDER BY project_id, sequence, row_id
                    LIMIT :batch_size
                  )
                  RETURNING *
                )
                UPDATE projectmessage AS m
                SET sequence = batch.sequence,
                    actor_user_id = batch.actor_user_id,
                    surface = batch.surface,
                    handling_state = batch.handling_state,
                    handled_by_task_id = batch.handled_by_task_id,
                    reply_to_message_id = batch.reply_to_message_id,
                    updated_at = batch.updated_at
                FROM batch
                WHERE m.id = batch.row_id
                """
            ),
            {"batch_size": _BACKFILL_BATCH_SIZE},
        )
        has_remaining = op.get_bind().scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM projectmessage_0567_backfill LIMIT 1)"
            )
        )
        if not has_remaining:
            break
    op.execute("DROP TABLE projectmessage_0567_backfill")


def _portable_backfill() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, project_id, created_at, resolved_at, from_type, "
            "message_type, status, context, from_task_id FROM projectmessage"
        )
    ).mappings()
    task_project_by_id = {
        task_id: project_id
        for raw_task_id, raw_project_id in bind.execute(
            sa.text("SELECT id, project_id FROM task")
        )
        if (task_id := _uuid_or_none(raw_task_id)) is not None
        and (project_id := _uuid_or_none(raw_project_id)) is not None
    }
    updates = _build_backfill_values(
        rows,
        task_project_by_id=task_project_by_id,
    )
    if not updates:
        return
    table = sa.table(
        "projectmessage",
        sa.column("id", sa.Uuid()),
        sa.column("sequence", sa.BigInteger()),
        sa.column("actor_user_id", sa.String(255)),
        sa.column("surface", sa.String(32)),
        sa.column("handling_state", sa.String(32)),
        sa.column("handled_by_task_id", sa.Uuid()),
        sa.column("reply_to_message_id", sa.Uuid()),
        # Raw SQLite SELECTs return DATETIME values as strings; bind them
        # without a DateTime processor and let the batch rebuild preserve the
        # valid ISO value in the final DATETIME column.
        sa.column(
            "updated_at",
            sa.String() if bind.dialect.name == "sqlite" else sa.DateTime(timezone=True),
        ),
    )
    for start in range(0, len(updates), _BACKFILL_BATCH_SIZE):
        bind.execute(
            table.update()
            .where(table.c.id == sa.bindparam("row_id"))
            .values(
                sequence=sa.bindparam("sequence"),
                actor_user_id=sa.bindparam("actor_user_id"),
                surface=sa.bindparam("surface"),
                handling_state=sa.bindparam("handling_state"),
                handled_by_task_id=sa.bindparam("handled_by_task_id"),
                reply_to_message_id=sa.bindparam("reply_to_message_id"),
                updated_at=sa.bindparam("updated_at"),
            ),
            updates[start : start + _BACKFILL_BATCH_SIZE],
        )


def _reconcile_postgresql_reply_handling() -> None:
    """Rebuild handling from all durable replies, including prior-run batches."""

    op.execute(
        """
        WITH earliest_reply AS (
          SELECT DISTINCT ON (reply.reply_to_message_id)
            reply.reply_to_message_id AS target_id,
            reply.project_id,
            valid_task.id AS handled_by_task_id
          FROM projectmessage AS reply
          LEFT JOIN task AS valid_task
            ON valid_task.id = reply.from_task_id
           AND valid_task.project_id = reply.project_id
          WHERE reply.reply_to_message_id IS NOT NULL
            AND reply.from_type = 'strategic'
          ORDER BY reply.reply_to_message_id, reply.created_at, reply.id
        )
        UPDATE projectmessage AS target
        SET handling_state = 'responded',
            handled_by_task_id = coalesce(
              target.handled_by_task_id,
              reply.handled_by_task_id
            )
        FROM earliest_reply AS reply
        WHERE target.id = reply.target_id
          AND target.project_id = reply.project_id
        """
    )


_POSTGRES_INDEX_SQL = (
    (
        "ux_projectmessage_project_sequence",
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_projectmessage_project_sequence "
        "ON projectmessage (project_id, sequence) WHERE sequence IS NOT NULL",
    ),
    (
        "ix_projectmessage_project_surface_sequence",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_projectmessage_project_surface_sequence "
        "ON projectmessage (project_id, surface, sequence)",
    ),
    (
        "ix_projectmessage_project_handling_sequence",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_projectmessage_project_handling_sequence "
        "ON projectmessage (project_id, handling_state, sequence)",
    ),
    (
        "ix_projectmessage_actor_user_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_projectmessage_actor_user_id "
        "ON projectmessage (actor_user_id)",
    ),
    (
        "ix_projectmessage_handled_by_task_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_projectmessage_handled_by_task_id "
        "ON projectmessage (handled_by_task_id)",
    ),
    (
        "ix_projectmessage_reply_to_message_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_projectmessage_reply_to_message_id "
        "ON projectmessage (reply_to_message_id)",
    ),
)


def _create_or_repair_postgresql_index(index_name: str, create_sql: str) -> None:
    """Replace a failed-CIC remnant instead of trusting IF NOT EXISTS."""

    invalid = op.get_bind().scalar(
        sa.text(
            """
            SELECT NOT index_state.indisvalid
            FROM pg_index AS index_state
            WHERE index_state.indexrelid = to_regclass(:index_name)
            """
        ),
        {"index_name": index_name},
    )
    if invalid:
        # Names come exclusively from the constant tuple above.
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")
    op.execute(create_sql)


def _postgresql_constraints() -> None:
    # Short catalog changes only. NOT VALID avoids a blocking full-table scan;
    # each validation runs separately under autocommit below.
    # Reconcile any mixed-version write that committed before the compatibility
    # trigger was deployed; from this point the constraint trigger rejects drift.
    op.execute(
        """
        UPDATE projectmessage message
        SET reply_to_message_id = NULL
        WHERE reply_to_message_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM projectmessage target
            WHERE target.id = message.reply_to_message_id
              AND target.project_id = message.project_id
          )
        """
    )
    op.execute(
        """
        UPDATE projectmessage message
        SET handled_by_task_id = NULL
        WHERE handled_by_task_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM task owner_task
            WHERE owner_task.id = message.handled_by_task_id
              AND owner_task.project_id = message.project_id
          )
        """
    )
    _reconcile_postgresql_reply_handling()
    op.execute(
        """
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'projectmessage_handled_by_task_id_fkey') THEN
            ALTER TABLE projectmessage ADD CONSTRAINT projectmessage_handled_by_task_id_fkey
              FOREIGN KEY (handled_by_task_id) REFERENCES task(id) ON DELETE SET NULL NOT VALID;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'projectmessage_reply_to_message_id_fkey') THEN
            ALTER TABLE projectmessage ADD CONSTRAINT projectmessage_reply_to_message_id_fkey
              FOREIGN KEY (reply_to_message_id) REFERENCES projectmessage(id) ON DELETE SET NULL NOT VALID;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_projectmessage_sequence_positive') THEN
            ALTER TABLE projectmessage ADD CONSTRAINT chk_projectmessage_sequence_positive
              CHECK (sequence IS NULL OR sequence > 0) NOT VALID;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_projectmessage_surface') THEN
            ALTER TABLE projectmessage ADD CONSTRAINT chk_projectmessage_surface
              CHECK (surface IN ('conversation','activity')) NOT VALID;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_projectmessage_handling_state') THEN
            ALTER TABLE projectmessage ADD CONSTRAINT chk_projectmessage_handling_state
              CHECK (handling_state IN ('pending','claimed','responded','superseded')) NOT VALID;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_projectmessage_reply_not_self') THEN
            ALTER TABLE projectmessage ADD CONSTRAINT chk_projectmessage_reply_not_self
              CHECK (reply_to_message_id IS NULL OR reply_to_message_id <> id) NOT VALID;
          END IF;
        END $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_projectmessage_project_links()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          linked_project uuid;
        BEGIN
          IF NEW.reply_to_message_id IS NOT NULL THEN
            linked_project := NULL;
            SELECT target.project_id INTO linked_project
            FROM projectmessage target
            WHERE target.id = NEW.reply_to_message_id
            FOR SHARE;
            IF linked_project IS DISTINCT FROM NEW.project_id THEN
              RAISE EXCEPTION 'reply message must belong to the same project'
                USING ERRCODE = '23503';
            END IF;
          END IF;
          IF NEW.handled_by_task_id IS NOT NULL THEN
            linked_project := NULL;
            SELECT owner_task.project_id INTO linked_project
            FROM task owner_task
            WHERE owner_task.id = NEW.handled_by_task_id
            FOR SHARE;
            IF linked_project IS DISTINCT FROM NEW.project_id THEN
              RAISE EXCEPTION 'handling task must belong to the same project'
                USING ERRCODE = '23503';
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
          RETURN NEW;
        END $$
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_projectmessage_project_links ON projectmessage")
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_projectmessage_project_links
        AFTER INSERT OR UPDATE OF project_id, reply_to_message_id, handled_by_task_id
        ON projectmessage DEFERRABLE INITIALLY IMMEDIATE
        FOR EACH ROW EXECUTE FUNCTION enforce_projectmessage_project_links()
        """
    )
    # Replace the early BEFORE trigger with the durable deferrable constraint
    # trigger once reconciliation has completed.
    op.execute("DROP TRIGGER IF EXISTS trg_task_projectmessage_links ON task")
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_task_projectmessage_links
        AFTER UPDATE OF project_id ON task DEFERRABLE INITIALLY IMMEDIATE
        FOR EACH ROW EXECUTE FUNCTION enforce_task_projectmessage_links()
        """
    )


def _portable_constraints_and_indexes() -> None:
    high_signal_sql = ", ".join(
        "'" + value.replace("'", "''") + "'"
        for value in sorted(_HIGH_SIGNAL_EVENT_TYPES)
    )
    # SQLite Alembic needs a batch rebuild for ALTER TABLE constraints.
    with op.batch_alter_table("projectmessage") as batch:
        batch.alter_column(
            "updated_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        )
        batch.create_foreign_key(
            "projectmessage_handled_by_task_id_fkey", "task",
            ["handled_by_task_id"], ["id"], ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "projectmessage_reply_to_message_id_fkey", "projectmessage",
            ["reply_to_message_id"], ["id"], ondelete="SET NULL",
        )
        batch.create_check_constraint(
            "chk_projectmessage_sequence_positive", "sequence IS NULL OR sequence > 0"
        )
        batch.create_check_constraint(
            "chk_projectmessage_surface", "surface IN ('conversation','activity')"
        )
        batch.create_check_constraint(
            "chk_projectmessage_handling_state",
            "handling_state IN ('pending','claimed','responded','superseded')",
        )
        batch.create_check_constraint(
            "chk_projectmessage_reply_not_self",
            "reply_to_message_id IS NULL OR reply_to_message_id <> id",
        )
    op.create_index(
        "ux_projectmessage_project_sequence", "projectmessage",
        ["project_id", "sequence"], unique=True,
        sqlite_where=sa.text("sequence IS NOT NULL"),
    )
    op.create_index(
        "ix_projectmessage_project_surface_sequence", "projectmessage",
        ["project_id", "surface", "sequence"],
    )
    op.create_index(
        "ix_projectmessage_project_handling_sequence", "projectmessage",
        ["project_id", "handling_state", "sequence"],
    )
    op.create_index("ix_projectmessage_actor_user_id", "projectmessage", ["actor_user_id"])
    op.create_index("ix_projectmessage_handled_by_task_id", "projectmessage", ["handled_by_task_id"])
    op.create_index("ix_projectmessage_reply_to_message_id", "projectmessage", ["reply_to_message_id"])
    if op.get_bind().dialect.name == "sqlite":
        for action in ("INSERT", "UPDATE"):
            suffix = action.lower()
            op.execute(
                f"""
                CREATE TRIGGER trg_projectmessage_project_links_{suffix}
                BEFORE {action} ON projectmessage
                FOR EACH ROW
                WHEN (
                  NEW.reply_to_message_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM projectmessage target
                    WHERE target.id = NEW.reply_to_message_id
                      AND target.project_id = NEW.project_id
                  )
                ) OR (
                  NEW.handled_by_task_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM task owner_task
                    WHERE owner_task.id = NEW.handled_by_task_id
                      AND owner_task.project_id = NEW.project_id
                  )
                )
                BEGIN
                  SELECT RAISE(ABORT, 'projectmessage link crosses project boundary');
                END
                """
            )
        op.execute(
            """
            CREATE TRIGGER trg_projectmessage_inbound_links_update
            BEFORE UPDATE OF project_id ON projectmessage
            FOR EACH ROW
            WHEN EXISTS (
              SELECT 1 FROM projectmessage reply
              WHERE reply.reply_to_message_id = NEW.id
                AND reply.project_id <> NEW.project_id
            )
            BEGIN
              SELECT RAISE(ABORT, 'message move strands cross-project replies');
            END
            """
        )
        for action in ("INSERT", "UPDATE"):
            suffix = action.lower()
            update_columns = (
                " OF project_id, reply_to_message_id, handled_by_task_id, "
                "sequence, surface, from_type, context"
                if action == "UPDATE"
                else ""
            )
            op.execute(
                f"""
                CREATE TRIGGER trg_projectmessage_0567_compat_{suffix}
                AFTER {action}{update_columns} ON projectmessage
                FOR EACH ROW
                WHEN NEW.sequence IS NULL AND (
                  NEW.surface = 'conversation'
                  OR NEW.from_type IN ('user', 'strategic')
                  OR CASE WHEN json_valid(NEW.context)
                    THEN coalesce(json_extract(NEW.context, '$.event_type'), '')
                    ELSE '' END IN ({high_signal_sql})
                  OR NEW.reply_to_message_id IS NOT NULL
                  OR CASE WHEN json_valid(NEW.context)
                    THEN json_extract(NEW.context, '$.reply_to')
                    ELSE NULL END IS NOT NULL
                )
                BEGIN
                  INSERT INTO {_SEQUENCE_TABLE} (project_id, last_sequence)
                  VALUES (NEW.project_id, 1)
                  ON CONFLICT(project_id) DO UPDATE SET
                    last_sequence = last_sequence + 1;
                  UPDATE projectmessage
                  SET sequence = (
                        SELECT last_sequence FROM {_SEQUENCE_TABLE}
                        WHERE project_id = NEW.project_id
                      ),
                      surface = 'conversation',
                      handling_state = CASE
                        WHEN NEW.from_type = 'strategic' THEN 'responded'
                        ELSE 'pending'
                      END,
                      actor_user_id = coalesce(
                        NEW.actor_user_id,
                        CASE WHEN json_valid(NEW.context) THEN substr(nullif(trim(coalesce(
                          json_extract(NEW.context, '$.operator_user_id'),
                          json_extract(NEW.context, '$.check_in.operator_user_id')
                        )), ''), 1, 255) END
                      ),
                      reply_to_message_id = coalesce(
                        NEW.reply_to_message_id,
                        (
                          SELECT target.id FROM projectmessage AS target
                          WHERE target.id = lower(replace(
                            CASE WHEN json_valid(NEW.context)
                              THEN coalesce(json_extract(NEW.context, '$.reply_to'), '')
                              ELSE '' END,
                            '-', ''
                          ))
                            AND target.project_id = NEW.project_id
                            AND target.id <> NEW.id
                        )
                      )
                  WHERE id = NEW.id;
                END
                """
            )
        op.execute(
            """
            CREATE TRIGGER trg_task_projectmessage_links_update
            BEFORE UPDATE OF project_id ON task
            FOR EACH ROW
            WHEN EXISTS (
              SELECT 1 FROM projectmessage message
              WHERE message.handled_by_task_id = NEW.id
                AND message.project_id <> NEW.project_id
            )
            BEGIN
              SELECT RAISE(ABORT, 'task move strands cross-project message handler');
            END
            """
        )


def upgrade() -> None:
    bind = op.get_bind()
    is_postgresql = bind.dialect.name == "postgresql"
    _add_columns(postgresql=is_postgresql)
    _add_foundation_tables(postgresql=is_postgresql)
    if not is_postgresql:
        _portable_backfill()
        _seed_sequence_counters(postgresql=False)
        _portable_constraints_and_indexes()
        return

    _seed_sequence_counters(postgresql=True)
    # Installed in the same short DDL transaction as the columns so an old
    # authoritative writer can never slip a conversation row into the NULL
    # sequence gap while the historical backfill runs.
    _install_postgresql_compatibility_trigger()

    # Entering autocommit commits the short metadata-only ALTER transaction and
    # releases ACCESS EXCLUSIVE before any history scan/rewrite begins.
    with op.get_context().autocommit_block():
        _postgresql_backfill()
        for index_name, statement in _POSTGRES_INDEX_SQL:
            _create_or_repair_postgresql_index(index_name, statement)
        _postgresql_constraints()
        for constraint in (
            "projectmessage_handled_by_task_id_fkey",
            "projectmessage_reply_to_message_id_fkey",
            "chk_projectmessage_sequence_positive",
            "chk_projectmessage_surface",
            "chk_projectmessage_handling_state",
            "chk_projectmessage_reply_not_self",
        ):
            op.execute(f"ALTER TABLE projectmessage VALIDATE CONSTRAINT {constraint}")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for name in (
                "ix_projectmessage_reply_to_message_id",
                "ix_projectmessage_handled_by_task_id",
                "ix_projectmessage_actor_user_id",
                "ix_projectmessage_project_handling_sequence",
                "ix_projectmessage_project_surface_sequence",
                "ux_projectmessage_project_sequence",
            ):
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        op.execute("DROP TRIGGER IF EXISTS trg_projectmessage_project_links ON projectmessage")
        op.execute("DROP FUNCTION IF EXISTS enforce_projectmessage_project_links()")
        op.execute("DROP TRIGGER IF EXISTS trg_task_projectmessage_links ON task")
        op.execute("DROP FUNCTION IF EXISTS enforce_task_projectmessage_links()")
        op.execute("DROP TRIGGER IF EXISTS trg_projectmessage_0567_compat_insert ON projectmessage")
        op.execute("DROP FUNCTION IF EXISTS projectmessage_0567_compat_insert()")
        op.execute(f"DROP TABLE IF EXISTS {_ATTACHMENT_UPLOAD_TABLE}")
        op.execute(f"DROP TABLE IF EXISTS {_SEQUENCE_TABLE}")
        for name, kind in (
            ("chk_projectmessage_reply_not_self", "check"),
            ("chk_projectmessage_handling_state", "check"),
            ("chk_projectmessage_surface", "check"),
            ("chk_projectmessage_sequence_positive", "check"),
            ("projectmessage_reply_to_message_id_fkey", "foreignkey"),
            ("projectmessage_handled_by_task_id_fkey", "foreignkey"),
        ):
            op.drop_constraint(name, "projectmessage", type_=kind)
        for column in (
            "updated_at", "reply_to_message_id", "handled_by_task_id",
            "handling_state", "surface", "sequence", "actor_user_id",
        ):
            op.drop_column("projectmessage", column)
        return

    for name in (
        "ix_projectmessage_reply_to_message_id",
        "ix_projectmessage_handled_by_task_id",
        "ix_projectmessage_actor_user_id",
        "ix_projectmessage_project_handling_sequence",
        "ix_projectmessage_project_surface_sequence",
        "ux_projectmessage_project_sequence",
    ):
        op.drop_index(name, table_name="projectmessage")
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_projectmessage_0567_compat_update")
        op.execute("DROP TRIGGER IF EXISTS trg_projectmessage_0567_compat_insert")
        op.execute("DROP TRIGGER IF EXISTS trg_task_projectmessage_links_update")
        op.execute("DROP TRIGGER IF EXISTS trg_projectmessage_inbound_links_update")
        op.execute("DROP TRIGGER IF EXISTS trg_projectmessage_project_links_insert")
        op.execute("DROP TRIGGER IF EXISTS trg_projectmessage_project_links_update")
    op.drop_table(_ATTACHMENT_UPLOAD_TABLE)
    op.drop_table(_SEQUENCE_TABLE)
    with op.batch_alter_table("projectmessage") as batch:
        batch.drop_constraint("chk_projectmessage_reply_not_self", type_="check")
        batch.drop_constraint("chk_projectmessage_handling_state", type_="check")
        batch.drop_constraint("chk_projectmessage_surface", type_="check")
        batch.drop_constraint("chk_projectmessage_sequence_positive", type_="check")
        batch.drop_constraint("projectmessage_reply_to_message_id_fkey", type_="foreignkey")
        batch.drop_constraint("projectmessage_handled_by_task_id_fkey", type_="foreignkey")
        for column in (
            "updated_at", "reply_to_message_id", "handled_by_task_id",
            "handling_state", "surface", "sequence", "actor_user_id",
        ):
            batch.drop_column(column)

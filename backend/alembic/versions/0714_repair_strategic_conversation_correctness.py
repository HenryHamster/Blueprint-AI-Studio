"""Repair trusted operator purpose and provable Strategic reply statuses.

Revision ID: 0714
Revises: 0713
Create Date: 2026-07-29

Historical repair is deliberately conservative: a purpose must cite an
existing same-project user message whose normalized content exactly equals the
purpose. PostgreSQL compatibility triggers preserve the same invariant while
old application revisions coexist with the new one.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0714"
down_revision = "0713"
branch_labels = None
depends_on = None

_LOG = logging.getLogger("alembic.runtime.migration")
_PLACEHOLDER_PURPOSE_RE = re.compile(r"^<[^>]+>$")
_COVERED_MESSAGE_IDS_KEY = "_resident_reply_covered_message_ids"
_BATCH_SIZE = 500
_CONTENT_INDEX = "ix_projectmessage_content_trgm"
_PG_UUID_RE = (
    "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    "[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _json_object(value: object) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
        if isinstance(decoded, Mapping):
            return dict(decoded)
    return None


def _normalize_evidence_text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _is_uuid_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return str(parsed) == value.lower()


def _is_placeholder(value: object) -> bool:
    normalized = value.strip() if isinstance(value, str) else ""
    return (
        not normalized
        or normalized == "..."
        or bool(_PLACEHOLDER_PURPOSE_RE.fullmatch(normalized))
    )


def _purpose_reference(
    problem_model: object,
) -> tuple[str, str] | None:
    model = _json_object(problem_model)
    intent = _json_object(model.get("intent")) if model else None
    if intent is None or not _is_placeholder(intent.get("user_purpose")):
        return None
    purpose = intent.get("purpose")
    if _is_placeholder(purpose):
        return None
    source = _json_object(intent.get("source"))
    if source is None or source.get("type") != "user_message":
        return None
    try:
        message_id = str(uuid.UUID(str(source.get("message_id"))))
    except (TypeError, ValueError, AttributeError):
        return None
    return message_id, str(purpose).strip()


def _is_purpose_candidate(problem_model: object) -> bool:
    model = _json_object(problem_model)
    intent = _json_object(model.get("intent")) if model else None
    if intent is None or not _is_placeholder(intent.get("user_purpose")):
        return False
    if _is_placeholder(intent.get("purpose")):
        return False
    source = _json_object(intent.get("source"))
    return source is not None and source.get("type") == "user_message"


def _trusted_user_purpose(
    problem_model: object,
    *,
    project_id: object,
    user_messages: Mapping[tuple[str, str], str],
) -> str | None:
    reference = _purpose_reference(problem_model)
    if reference is None:
        return None
    message_id, purpose = reference
    evidence = user_messages.get((str(project_id), message_id))
    if not isinstance(evidence, str):
        return None
    if _normalize_evidence_text(purpose) != _normalize_evidence_text(evidence):
        return None
    return purpose


def _reply_reference_ids(
    reply_to_message_id: object, context: object
) -> set[uuid.UUID]:
    references: set[uuid.UUID] = set()
    if isinstance(reply_to_message_id, uuid.UUID):
        references.add(reply_to_message_id)
    elif _is_uuid_text(reply_to_message_id):
        references.add(uuid.UUID(str(reply_to_message_id)))
    payload = _json_object(context) or {}
    covered = payload.get(_COVERED_MESSAGE_IDS_KEY)
    if isinstance(covered, list):
        references.update(
            uuid.UUID(value) for value in covered if _is_uuid_text(value)
        )
    return references


def _project_table() -> sa.Table:
    return sa.Table(
        "project",
        sa.MetaData(),
        sa.Column("id", sa.Uuid()),
        sa.Column("problem_model", sa.JSON()),
    )


def _message_table() -> sa.Table:
    return sa.Table(
        "projectmessage",
        sa.MetaData(),
        sa.Column("id", sa.Uuid()),
        sa.Column("project_id", sa.Uuid()),
        sa.Column("surface", sa.String()),
        sa.Column("from_type", sa.String()),
        sa.Column("handling_state", sa.String()),
        sa.Column("status", sa.String()),
        sa.Column("reply_to_message_id", sa.Uuid()),
        sa.Column("content", sa.Text()),
        sa.Column("context", sa.JSON()),
        sa.Column("resolved_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )


_PG_PURPOSE_CANDIDATE = """
  (
    coalesce(btrim(p.problem_model #>> '{intent,user_purpose}'), '') IN ('', '...')
    OR btrim(p.problem_model #>> '{intent,user_purpose}') ~ '^<[^>]+>$'
  )
  AND coalesce(btrim(p.problem_model #>> '{intent,purpose}'), '') <> ''
  AND btrim(p.problem_model #>> '{intent,purpose}') <> '...'
  AND btrim(p.problem_model #>> '{intent,purpose}') !~ '^<[^>]+>$'
  AND p.problem_model #>> '{intent,source,type}' = 'user_message'
"""

_PG_PURPOSE_MATCH = _PG_PURPOSE_CANDIDATE + """
  AND p.problem_model #>> '{intent,source,message_id}' = m.id::text
  AND m.project_id = p.id
  AND m.from_type = 'user'
  AND regexp_replace(btrim(p.problem_model #>> '{intent,purpose}'), '\\s+', ' ', 'g')
      = regexp_replace(btrim(m.content), '\\s+', ' ', 'g')
"""

_PG_REPLY_PROOF = f"""
  pm.surface = 'conversation'
  AND pm.from_type = 'strategic'
  AND pm.handling_state = 'responded'
  AND pm.status = 'open'
  AND (
    EXISTS (
      SELECT 1 FROM projectmessage AS reply_input
      WHERE reply_input.id = pm.reply_to_message_id
        AND reply_input.project_id = pm.project_id
        AND reply_input.surface = 'conversation'
        AND reply_input.from_type IN ('user', 'operator')
    )
    OR CASE
      WHEN jsonb_typeof(
        pm.context -> '_resident_reply_covered_message_ids'
      ) = 'array'
      THEN EXISTS (
        SELECT 1 FROM jsonb_array_elements_text(
          pm.context -> '_resident_reply_covered_message_ids'
        ) AS covered(value)
        JOIN projectmessage AS covered_input
          ON covered_input.id::text = covered.value
        WHERE covered.value ~ '{_PG_UUID_RE}'
          AND covered_input.project_id = pm.project_id
          AND covered_input.surface = 'conversation'
          AND covered_input.from_type IN ('user', 'operator')
      )
      ELSE false
    END
  )
"""


def _postgres_repair(connection: sa.Connection) -> tuple[int, int, int, int]:
    purpose_repairs = int(
        connection.execute(
            sa.text(
                "SELECT count(*) FROM project p JOIN projectmessage m ON "
                "(p.problem_model #>> '{intent,source,message_id}') = m.id::text "
                f"WHERE {_PG_PURPOSE_MATCH}"
            )
        ).scalar_one()
    )
    purpose_candidates = int(
        connection.execute(
            sa.text(
                "SELECT count(*) FROM project p WHERE "
                f"{_PG_PURPOSE_CANDIDATE}"
            )
        ).scalar_one()
    )
    reply_repairs = int(
        connection.execute(
            sa.text(f"SELECT count(*) FROM projectmessage pm WHERE {_PG_REPLY_PROOF}")
        ).scalar_one()
    )
    reply_candidates = int(
        connection.execute(
            sa.text(
                "SELECT count(*) FROM projectmessage pm WHERE "
                "pm.surface='conversation' AND pm.from_type='strategic' "
                "AND pm.handling_state='responded' AND pm.status='open'"
            )
        ).scalar_one()
    )
    connection.execute(
        sa.text(
            "UPDATE project p SET problem_model = jsonb_set("
            "p.problem_model, '{intent,user_purpose}', "
            "to_jsonb(btrim(p.problem_model #>> '{intent,purpose}')), true) "
            "FROM projectmessage m WHERE "
            f"{_PG_PURPOSE_MATCH}"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE projectmessage pm SET status='resolved', "
            "resolved_at=coalesce(resolved_at, now()), updated_at=now() WHERE "
            f"{_PG_REPLY_PROOF}"
        )
    )
    return (
        purpose_repairs,
        purpose_candidates - purpose_repairs,
        reply_repairs,
        reply_candidates - reply_repairs,
    )


def _portable_repair(connection: sa.Connection) -> tuple[int, int, int, int]:
    project = _project_table()
    message = _message_table()
    purpose_repairs = ambiguous_purposes = 0
    result = connection.execute(
        sa.select(project.c.id, project.c.problem_model).where(
            project.c.problem_model.is_not(None)
        )
    )
    while batch := result.fetchmany(_BATCH_SIZE):
        references: list[tuple[object, dict[str, Any], str, str]] = []
        message_ids: list[uuid.UUID] = []
        for row in batch:
            model = _json_object(row.problem_model)
            if not _is_purpose_candidate(model):
                continue
            reference = _purpose_reference(model)
            if model is None or reference is None:
                ambiguous_purposes += 1
                continue
            message_id, purpose = reference
            references.append((row.id, model, message_id, purpose))
            message_ids.append(uuid.UUID(message_id))
        evidence = {
            (str(row.project_id), str(row.id)): row.content
            for row in connection.execute(
                sa.select(
                    message.c.id, message.c.project_id, message.c.content
                ).where(
                    message.c.id.in_(message_ids),
                    message.c.from_type == "user",
                )
            )
        }
        for project_id, model, _message_id, _purpose in references:
            repaired = _trusted_user_purpose(
                model, project_id=project_id, user_messages=evidence
            )
            if repaired is None:
                ambiguous_purposes += 1
                continue
            intent = _json_object(model.get("intent"))
            assert intent is not None
            intent["user_purpose"] = repaired
            model["intent"] = intent
            connection.execute(
                project.update()
                .where(project.c.id == project_id)
                .values(problem_model=model)
            )
            purpose_repairs += 1

    reply_repairs = ambiguous_replies = 0
    candidates = connection.execute(
        sa.select(
            message.c.id,
            message.c.project_id,
            message.c.reply_to_message_id,
            message.c.context,
        ).where(
            message.c.surface == "conversation",
            message.c.from_type == "strategic",
            message.c.handling_state == "responded",
            message.c.status == "open",
        )
    )
    while batch := candidates.fetchmany(_BATCH_SIZE):
        references_by_reply = {
            row.id: _reply_reference_ids(
                row.reply_to_message_id, row.context
            )
            for row in batch
        }
        reference_ids = {
            reference_id
            for references in references_by_reply.values()
            for reference_id in references
        }
        verified_inputs = {
            (str(row.project_id), row.id)
            for row in connection.execute(
                sa.select(message.c.id, message.c.project_id).where(
                    message.c.id.in_(reference_ids),
                    message.c.surface == "conversation",
                    message.c.from_type.in_(("user", "operator")),
                )
            )
        }
        repair_ids = [
            row.id
            for row in batch
            if any(
                (str(row.project_id), reference_id) in verified_inputs
                for reference_id in references_by_reply[row.id]
            )
        ]
        ambiguous_replies += len(batch) - len(repair_ids)
        if repair_ids:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            connection.execute(
                message.update()
                .where(message.c.id.in_(repair_ids), message.c.status == "open")
                .values(status="resolved", resolved_at=now, updated_at=now)
            )
            reply_repairs += len(repair_ids)
    return purpose_repairs, ambiguous_purposes, reply_repairs, ambiguous_replies


def _install_postgres_compatibility_triggers() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION blueprint_0711_repair_reply_status()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.surface='conversation' AND NEW.from_type='strategic'
             AND NEW.handling_state='responded' AND NEW.status='open'
             AND (
               EXISTS (
                 SELECT 1 FROM projectmessage AS reply_input
                 WHERE reply_input.id = NEW.reply_to_message_id
                   AND reply_input.project_id = NEW.project_id
                   AND reply_input.surface = 'conversation'
                   AND reply_input.from_type IN ('user', 'operator')
               )
               OR CASE
                 WHEN jsonb_typeof(
                   NEW.context -> '_resident_reply_covered_message_ids'
                 ) = 'array'
                 THEN EXISTS (
                   SELECT 1 FROM jsonb_array_elements_text(
                     NEW.context -> '_resident_reply_covered_message_ids'
                   ) AS covered(value)
                   JOIN projectmessage AS covered_input
                     ON covered_input.id::text = covered.value
                   WHERE covered.value ~ '{_PG_UUID_RE}'
                     AND covered_input.project_id = NEW.project_id
                     AND covered_input.surface = 'conversation'
                     AND covered_input.from_type IN ('user', 'operator')
                 )
                 ELSE false
               END
             )
          THEN
            NEW.status := 'resolved';
            NEW.resolved_at := coalesce(NEW.resolved_at, now());
            NEW.updated_at := now();
          END IF;
          RETURN NEW;
        END $$;
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_0711_repair_reply_status "
        "ON projectmessage"
    )
    op.execute(
        """
        CREATE TRIGGER trg_0711_repair_reply_status
        BEFORE INSERT OR UPDATE OF surface, from_type, handling_state, status,
          reply_to_message_id, context
        ON projectmessage FOR EACH ROW
        EXECUTE FUNCTION blueprint_0711_repair_reply_status()
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_0711_fill_user_purpose_from_message "
        "ON projectmessage"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION blueprint_0711_fill_user_purpose_from_message()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.from_type = 'user' THEN
            UPDATE project p SET problem_model = jsonb_set(
              p.problem_model, '{intent,user_purpose}',
              to_jsonb(btrim(p.problem_model #>> '{intent,purpose}')), true
            )
            WHERE p.id = NEW.project_id
              AND (
                coalesce(
                  btrim(p.problem_model #>> '{intent,user_purpose}'), ''
                ) IN ('', '...')
                OR btrim(
                  p.problem_model #>> '{intent,user_purpose}'
                ) ~ '^<[^>]+>$'
              )
              AND p.problem_model #>> '{intent,source,type}' = 'user_message'
              AND p.problem_model #>> '{intent,source,message_id}' = NEW.id::text
              AND btrim(p.problem_model #>> '{intent,purpose}') <> ''
              AND btrim(p.problem_model #>> '{intent,purpose}') <> '...'
              AND btrim(p.problem_model #>> '{intent,purpose}') !~ '^<[^>]+>$'
              AND regexp_replace(
                btrim(p.problem_model #>> '{intent,purpose}'), '\\s+', ' ', 'g'
              ) = regexp_replace(btrim(NEW.content), '\\s+', ' ', 'g');
          END IF;
          RETURN NEW;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_0711_fill_user_purpose_from_message
        AFTER INSERT OR UPDATE OF project_id, from_type, content
        ON projectmessage FOR EACH ROW
        EXECUTE FUNCTION blueprint_0711_fill_user_purpose_from_message()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION blueprint_0711_fill_user_purpose_from_project()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          evidence projectmessage%ROWTYPE;
        BEGIN
          IF (
               coalesce(
                 btrim(NEW.problem_model #>> '{intent,user_purpose}'), ''
               ) IN ('', '...')
               OR btrim(
                 NEW.problem_model #>> '{intent,user_purpose}'
               ) ~ '^<[^>]+>$'
             )
             AND NEW.problem_model #>> '{intent,source,type}' = 'user_message'
          THEN
            SELECT * INTO evidence
            FROM projectmessage
            WHERE id::text = NEW.problem_model #>> '{intent,source,message_id}'
              AND project_id = NEW.id
              AND from_type = 'user';
            IF FOUND
               AND btrim(NEW.problem_model #>> '{intent,purpose}') <> ''
               AND btrim(NEW.problem_model #>> '{intent,purpose}') <> '...'
               AND btrim(
                 NEW.problem_model #>> '{intent,purpose}'
               ) !~ '^<[^>]+>$'
               AND regexp_replace(
                 btrim(NEW.problem_model #>> '{intent,purpose}'),
                 '\\s+', ' ', 'g'
               ) = regexp_replace(btrim(evidence.content), '\\s+', ' ', 'g')
            THEN
              NEW.problem_model := jsonb_set(
                NEW.problem_model, '{intent,user_purpose}',
                to_jsonb(btrim(
                  NEW.problem_model #>> '{intent,purpose}'
                )), true
              );
            END IF;
          END IF;
          RETURN NEW;
        END $$;
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_0711_fill_user_purpose_from_project "
        "ON project"
    )
    op.execute(
        """
        CREATE TRIGGER trg_0711_fill_user_purpose_from_project
        BEFORE INSERT OR UPDATE OF problem_model
        ON project FOR EACH ROW
        EXECUTE FUNCTION blueprint_0711_fill_user_purpose_from_project()
        """
    )


def upgrade() -> None:
    connection = op.get_bind()
    is_postgres = connection.dialect.name == "postgresql"
    counts = (
        _postgres_repair(connection)
        if is_postgres
        else _portable_repair(connection)
    )
    _LOG.info(
        "0711 correctness report: purpose_repairs=%d "
        "ambiguous_purposes_preserved=%d reply_repairs=%d "
        "ambiguous_replies_preserved=%d",
        *counts,
    )
    if is_postgres:
        _install_postgres_compatibility_triggers()
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        # projectmessage is hot and potentially large. Match the repository's
        # 0529 pattern: leave Alembic's transaction before the index build so
        # PostgreSQL can create it without a table-blocking transactional DDL
        # lock.
        op.execute("COMMIT")
        invalid_index = bool(
            connection.execute(
                sa.text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_index i "
                    "JOIN pg_class c ON c.oid=i.indexrelid "
                    "WHERE c.relname=:index_name "
                    "AND c.relnamespace=to_regnamespace(current_schema()) "
                    "AND NOT i.indisvalid)"
                ),
                {"index_name": _CONTENT_INDEX},
            ).scalar_one()
        )
        # The catalog probe opens a transaction. End it before either
        # concurrent DDL statement.
        op.execute("COMMIT")
        if invalid_index:
            op.execute(
                f"DROP INDEX CONCURRENTLY IF EXISTS {_CONTENT_INDEX}"
            )
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_CONTENT_INDEX} "
            "ON projectmessage USING gin (content gin_trgm_ops)"
        )
    else:
        op.create_index(
            _CONTENT_INDEX,
            "projectmessage",
            ["content"],
            postgresql_using="gin",
            postgresql_ops={"content": "gin_trgm_ops"},
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_0711_fill_user_purpose_from_project "
            "ON project"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS "
            "blueprint_0711_fill_user_purpose_from_project()"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_0711_fill_user_purpose_from_message "
            "ON projectmessage"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS "
            "blueprint_0711_fill_user_purpose_from_message()"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_0711_repair_reply_status "
            "ON projectmessage"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS blueprint_0711_repair_reply_status()"
        )
        op.execute("COMMIT")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_CONTENT_INDEX}")
    else:
        op.drop_index(_CONTENT_INDEX, table_name="projectmessage")

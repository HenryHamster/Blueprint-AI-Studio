"""Add the durable per-project Strategic Resident control plane.

The new engine remains shadow-only after this migration: every project is
backfilled with ``engine_owner='legacy'`` and authority generation 1.  A later
atomic ownership flip grants the new engine write authority.

Revision ID: 0611
Revises: 0610
Create Date: 2026-07-16
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite


revision = "0611"
down_revision = "0610"
branch_labels = None
depends_on = None

_RESIDENT_TABLE = "strategicresident"
_CHECKPOINT_TABLE = "strategicresidentcheckpoint"
_RECEIPT_TABLE = "strategicoperationreceipt"
_CHECKPOINT_NAMESPACE = uuid.UUID("33d6659c-727d-4dc8-b04c-fd30cc753ad1")
_CHECKPOINT_MAX_BYTES = 128_000
_MAX_DEPTH = 10
_MAX_STRING_CHARS = 4_000
_MAX_DICT_ITEMS = 200
_MAX_LIST_ITEMS = 100
_MAX_CHECKPOINT_NODES = 5_000
_LEGACY_MAX_DEPTH = 6
_LEGACY_SUMMARY_MAX_BYTES = 10_000
_LEGACY_DECISIONS_MAX_BYTES = 32_000
_LEGACY_CURSOR_MAX_CHARS = 2_000
_LEGACY_MAX_DICT_ITEMS = 50
_LEGACY_MAX_LIST_ITEMS = 50
_LEGACY_MAX_NODES = 1_000
_BACKFILL_BATCH_SIZE = 25
_DIRECTION_FIELDS = (
    "id",
    "text",
    "status",
    "priority",
    "rationale",
    "recommended_mode",
    "gap_id",
    "success_evidence",
    "retirement_condition",
    "staged_for_version",
    "superseded_by",
)


def _json_type(*, postgresql_dialect: bool) -> sa.types.TypeEngine:
    return postgresql.JSONB() if postgresql_dialect else sa.JSON()


def _json_default(value: str, *, postgresql_dialect: bool) -> sa.TextClause:
    suffix = "::jsonb" if postgresql_dialect else ""
    return sa.text(f"'{value}'{suffix}")


def _safe_json(value: Any) -> Any:
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


def _decode_json_value(value: Any) -> Any:
    """Normalize JSON values returned as text by SQLite/raw SQL drivers."""

    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _bounded_public_value(
    value: Any,
    *,
    depth: int = 0,
    node_budget: list[int] | None = None,
) -> Any:
    """Deterministically bound strategic JSON and discard operational keys."""

    if node_budget is None:
        node_budget = [_MAX_CHECKPOINT_NODES]
    if node_budget[0] <= 0:
        return None
    node_budget[0] -= 1
    if depth >= _MAX_DEPTH:
        return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key in sorted(value, key=lambda item: str(item))[:_MAX_DICT_ITEMS]:
            key = str(raw_key)
            if key.startswith("_"):
                continue
            result[key] = _bounded_public_value(
                value[raw_key],
                depth=depth + 1,
                node_budget=node_budget,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _bounded_public_value(
                item,
                depth=depth + 1,
                node_budget=node_budget,
            )
            for item in list(value)[:_MAX_LIST_ITEMS]
        ]
    if isinstance(value, str):
        return value[:_MAX_STRING_CHARS]
    return _safe_json(value)


def _direction_projection(
    raw: Any, *, node_budget: list[int] | None = None
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return projected
    for direction in raw:
        if not isinstance(direction, Mapping):
            continue
        status = str(direction.get("status") or "active")
        if status not in {"active", "paused", "proposed", "staged"}:
            continue
        item = {
            field: _bounded_public_value(direction[field], node_budget=node_budget)
            for field in _DIRECTION_FIELDS
            if field in direction
        }
        context = direction.get("context")
        if isinstance(context, Mapping):
            bounded_context = {
                key: _bounded_public_value(context[key], node_budget=node_budget)
                for key in (
                    "success_evidence",
                    "retirement_condition",
                    "recommended_mode",
                    "gap_id",
                )
                if key in context
            }
            if bounded_context:
                item["context"] = bounded_context
        projected.append(item)
        if len(projected) >= _MAX_LIST_ITEMS:
            break
    return projected


def _memory_projection(
    raw: Any, *, node_budget: list[int] | None = None
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for entry in raw[-100:]:
        if not isinstance(entry, Mapping):
            continue
        note = entry.get("note")
        if not isinstance(note, str) or not note.strip():
            continue
        item = {"note": note.strip()[:_MAX_STRING_CHARS]}
        if node_budget is not None:
            if node_budget[0] <= 0:
                break
            node_budget[0] -= 1
        if entry.get("set_at") is not None:
            item["set_at"] = str(entry["set_at"])[:128]
        result.append(item)
    return result


def _problem_model_summary(
    raw: Any, *, node_budget: list[int] | None = None
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    summary: dict[str, Any] = {"truncated": True}
    for key in (
        "revision",
        "purpose",
        "target_user",
        "problem",
        "problems",
        "goals",
        "constraints",
    ):
        if key in raw and not key.startswith("_"):
            summary[key] = _bounded_public_value(raw[key], node_budget=node_budget)
    return summary


def _vision_summary(
    raw: Any, *, node_budget: list[int] | None = None
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    return {
        key: _bounded_public_value(raw[key], node_budget=node_budget)
        for key in ("revision", "purpose", "systems", "priorities", "context")
        if key in raw
    }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _bounded_legacy_value(
    value: Any,
    *,
    depth: int = 0,
    node_budget: list[int] | None = None,
) -> Any:
    """Bound selected legacy strategy state without retaining session history."""

    if node_budget is None:
        node_budget = [_LEGACY_MAX_NODES]
    if node_budget[0] <= 0 or depth >= _LEGACY_MAX_DEPTH:
        return None
    node_budget[0] -= 1
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key in sorted(value, key=lambda item: str(item))[
            :_LEGACY_MAX_DICT_ITEMS
        ]:
            key = str(raw_key)
            if key.startswith("_"):
                continue
            result[key] = _bounded_legacy_value(
                value[raw_key],
                depth=depth + 1,
                node_budget=node_budget,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _bounded_legacy_value(
                item,
                depth=depth + 1,
                node_budget=node_budget,
            )
            for item in list(value)[:_LEGACY_MAX_LIST_ITEMS]
        ]
    if isinstance(value, str):
        return value[:_LEGACY_DECISIONS_MAX_BYTES]
    return _safe_json(value)


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _legacy_strategic_projection(
    vision: Any,
) -> tuple[dict[str, Any], str | None]:
    """Project only durable strategic legacy fields from the session blob."""

    if not isinstance(vision, Mapping):
        return {}, None
    blob = vision.get("_resident_strategic_session")
    if not isinstance(blob, Mapping):
        return {}, None

    strategy: dict[str, Any] = {}
    summary = blob.get("_session_summary")
    if isinstance(summary, str) and summary:
        strategy["legacy_session_summary"] = _truncate_utf8(
            summary,
            _LEGACY_SUMMARY_MAX_BYTES,
        )

    decisions = blob.get("_decisions_reg")
    if isinstance(decisions, str):
        if len(decisions.encode("utf-8")) > _LEGACY_DECISIONS_MAX_BYTES:
            decisions = None
            strategy["legacy_decisions_register_malformed"] = True
        else:
            try:
                decisions = json.loads(decisions)
            except (TypeError, ValueError):
                decisions = None
                strategy["legacy_decisions_register_malformed"] = True
    if isinstance(decisions, Mapping):
        decisions = dict(decisions)
        for key in ("decisions", "entries"):
            if isinstance(decisions.get(key), list):
                decisions[key] = decisions[key][:_LEGACY_MAX_LIST_ITEMS]
        projected = _bounded_legacy_value(decisions)
        if len(_canonical_json(projected)) <= _LEGACY_DECISIONS_MAX_BYTES:
            strategy["legacy_decisions_register"] = projected
        else:
            strategy["legacy_decisions_register_malformed"] = True
    elif isinstance(decisions, list):
        projected = _bounded_legacy_value(decisions[:_LEGACY_MAX_LIST_ITEMS])
        if len(_canonical_json(projected)) <= _LEGACY_DECISIONS_MAX_BYTES:
            strategy["legacy_decisions_register"] = projected
        else:
            strategy["legacy_decisions_register_malformed"] = True

    cursor = blob.get("_message_cursor")
    legacy_cursor = (
        str(cursor)[:_LEGACY_CURSOR_MAX_CHARS] if cursor is not None else None
    )
    return strategy, legacy_cursor


def _build_initial_checkpoint(project: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    node_budget = [_MAX_CHECKPOINT_NODES]
    vision = project.get("vision") or {}
    legacy_strategy, legacy_message_cursor = _legacy_strategic_projection(vision)
    content: dict[str, Any] = {
        "schema_version": 1,
        "charter": _bounded_public_value(
            project.get("problem_model") or {}, node_budget=node_budget
        ),
        "working_strategy": {
            "vision": _bounded_public_value(vision, node_budget=node_budget),
            "running_update": str(project.get("running_update") or "")[
                :_MAX_STRING_CHARS
            ],
            "directions": _direction_projection(
                project.get("directions"), node_budget=node_budget
            ),
            "strategic_memory": _memory_projection(
                project.get("strategic_memory_notes"),
                node_budget=node_budget,
            ),
            **legacy_strategy,
        },
        "cursors": {
            "handled_message_sequence": int(
                project.get("handled_message_sequence") or 0
            ),
            "project_change": {},
            **(
                {"legacy_message_cursor": legacy_message_cursor}
                if legacy_message_cursor is not None
                else {}
            ),
        },
        "migration": {
            "source": "legacy_projection",
            "truncated": False,
        },
    }
    encoded = _canonical_json(content)
    if len(encoded) > _CHECKPOINT_MAX_BYTES:
        summary_budget = [_MAX_CHECKPOINT_NODES]
        content["charter"] = _problem_model_summary(
            project.get("problem_model"), node_budget=summary_budget
        )
        strategy = content["working_strategy"]
        strategy["vision"] = _vision_summary(
            project.get("vision"), node_budget=summary_budget
        )
        strategy["directions"] = strategy["directions"][:100]
        strategy["strategic_memory"] = strategy["strategic_memory"][-50:]
        content["migration"]["truncated"] = True
        encoded = _canonical_json(content)
    if len(encoded) > _CHECKPOINT_MAX_BYTES:
        strategy = content["working_strategy"]
        strategy["directions"] = strategy["directions"][:25]
        strategy["strategic_memory"] = strategy["strategic_memory"][-20:]
        encoded = _canonical_json(content)
    if len(encoded) > _CHECKPOINT_MAX_BYTES:
        # Final fail-safe for unusually wide public arrays/maps: retain a small,
        # deterministic strategic core instead of blocking the deployment.
        content["charter"] = {
            key: _bounded_public_value(value)
            for key, value in list((content.get("charter") or {}).items())[:5]
        }
        strategy = content["working_strategy"]
        strategy["vision"] = {
            key: _bounded_public_value(value)
            for key, value in list((strategy.get("vision") or {}).items())[:5]
        }
        strategy["directions"] = [
            {
                key: (
                    value[:1_000]
                    if isinstance(value, str)
                    else _bounded_public_value(value)
                )
                for key, value in list(direction.items())[:8]
            }
            for direction in strategy["directions"][:10]
        ]
        strategy["strategic_memory"] = [
            {
                "note": str(entry.get("note") or "")[:1_000],
                **(
                    {"set_at": str(entry.get("set_at"))[:128]}
                    if entry.get("set_at")
                    else {}
                ),
            }
            for entry in strategy["strategic_memory"][-10:]
        ]
        encoded = _canonical_json(content)
    if len(encoded) > _CHECKPOINT_MAX_BYTES:
        raise ValueError("minimal strategic checkpoint exceeds maximum size")
    return content, hashlib.sha256(encoded).hexdigest()


def _legacy_rollout_state(vision: Any) -> dict[str, Any]:
    if not isinstance(vision, Mapping):
        return {"shadow": True, "source": "legacy"}
    blob = vision.get("_resident_strategic_session")
    result: dict[str, Any] = {"shadow": True, "source": "legacy"}
    if not isinstance(blob, Mapping):
        return result
    for source, target in (
        ("session_id", "legacy_session_id"),
        ("task_id", "legacy_task_id"),
        ("runner_id", "legacy_runner_id"),
    ):
        value = blob.get(source)
        if value is not None:
            result[target] = str(value)[:128]
    return result


def _background_policy_for(
    *,
    project_paused: Any,
    has_continuous: Any,
    has_chat: Any,
) -> str:
    if bool(project_paused):
        return "paused"
    if bool(has_continuous):
        return "active"
    if bool(has_chat):
        return "paused"
    return "active"


def _load_interaction_mode_summary(
    *, postgresql_dialect: bool
) -> dict[uuid.UUID, tuple[bool, bool]]:
    if postgresql_dialect:
        statement = sa.text(
            """
            SELECT project_id,
                   bool_or(
                     prefs->'_strategic_workbench'->>'interaction_mode'
                       = 'continuous'
                   ) AS has_continuous,
                   bool_or(
                     prefs->'_strategic_workbench'->>'interaction_mode'
                       = 'chat'
                   ) AS has_chat
            FROM messagesnoiseprefs
            GROUP BY project_id
            """
        )
    else:
        statement = sa.text(
            """
            SELECT project_id,
                   max(CASE WHEN json_valid(prefs)
                     AND json_extract(
                       prefs, '$._strategic_workbench.interaction_mode'
                     ) = 'continuous' THEN 1 ELSE 0 END) AS has_continuous,
                   max(CASE WHEN json_valid(prefs)
                     AND json_extract(
                       prefs, '$._strategic_workbench.interaction_mode'
                     ) = 'chat' THEN 1 ELSE 0 END) AS has_chat
            FROM messagesnoiseprefs
            GROUP BY project_id
            """
        )
    summary: dict[uuid.UUID, tuple[bool, bool]] = {}
    for row in op.get_bind().execute(statement).mappings():
        project_id = row["project_id"]
        if not isinstance(project_id, uuid.UUID):
            project_id = uuid.UUID(str(project_id))
        summary[project_id] = (
            bool(row.get("has_continuous")),
            bool(row.get("has_chat")),
        )
    return summary


def _load_handled_message_cursors() -> dict[uuid.UUID, int]:
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                """
            WITH cursor_summary AS (
              SELECT project_id,
                     min(CASE
                       WHEN surface = 'conversation'
                        AND handling_state IN ('pending','claimed')
                       THEN sequence
                     END) AS min_pending,
                     max(CASE
                       WHEN surface = 'conversation' THEN sequence
                     END) AS max_conversation
              FROM projectmessage
              WHERE sequence IS NOT NULL
              GROUP BY project_id
            )
            SELECT project_id,
                   CASE
                     WHEN min_pending IS NOT NULL THEN
                       CASE WHEN min_pending > 1 THEN min_pending - 1 ELSE 0 END
                     ELSE coalesce(max_conversation, 0)
                   END AS handled_cursor
            FROM cursor_summary
            """
            )
        )
        .mappings()
    )
    result: dict[uuid.UUID, int] = {}
    for row in rows:
        project_id = row["project_id"]
        if not isinstance(project_id, uuid.UUID):
            project_id = uuid.UUID(str(project_id))
        result[project_id] = max(0, int(row.get("handled_cursor") or 0))
    return result


def _checkpoint_id(project_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_CHECKPOINT_NAMESPACE, f"{project_id}:1")


def _create_tables(*, postgresql_dialect: bool) -> None:
    json_type = _json_type(postgresql_dialect=postgresql_dialect)
    empty_object = _json_default("{}", postgresql_dialect=postgresql_dialect)
    empty_list = _json_default("[]", postgresql_dialect=postgresql_dialect)
    current_checkpoint_args: list[Any] = [sa.Uuid()]
    previous_checkpoint_args: list[Any] = [sa.Uuid()]
    if not postgresql_dialect:
        # SQLite permits a forward reference to a table created later. Defining
        # these inline avoids batch-rebuilding the parent after checkpoint rows
        # exist (which would activate the checkpoint -> resident CASCADE).
        current_checkpoint_args.append(
            sa.ForeignKey(
                f"{_CHECKPOINT_TABLE}.id",
                name="fk_strategicresident_current_checkpoint",
                ondelete="SET NULL",
            )
        )
        previous_checkpoint_args.append(
            sa.ForeignKey(
                f"{_CHECKPOINT_TABLE}.id",
                name="fk_strategicresident_previous_checkpoint",
                ondelete="SET NULL",
            )
        )

    op.create_table(
        "strategicresident",
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "engine_owner",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'legacy'"),
        ),
        sa.Column(
            "engine_version",
            sa.String(64),
            nullable=False,
            server_default=sa.text("'legacy-v1'"),
        ),
        sa.Column(
            "background_policy",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "authority_generation",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "active_turn_task_id",
            sa.Uuid(),
            sa.ForeignKey("task.id", ondelete="SET NULL"),
        ),
        sa.Column("active_turn_lane", sa.String(16)),
        sa.Column(
            "interactive_successor_task_id",
            sa.Uuid(),
            sa.ForeignKey("task.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "background_successor_task_id",
            sa.Uuid(),
            sa.ForeignKey("task.id", ondelete="SET NULL"),
        ),
        # Checkpoint FKs are added after the checkpoint table exists.
        sa.Column("current_checkpoint_id", *current_checkpoint_args),
        sa.Column("previous_checkpoint_id", *previous_checkpoint_args),
        sa.Column(
            "handled_message_sequence",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "handled_project_change_cursor",
            json_type,
            nullable=False,
            server_default=empty_object,
        ),
        sa.Column(
            "runner_policy", json_type, nullable=False, server_default=empty_object
        ),
        sa.Column(
            "provider_policy", json_type, nullable=False, server_default=empty_object
        ),
        sa.Column(
            "failover_policy", json_type, nullable=False, server_default=empty_object
        ),
        sa.Column(
            "health_status",
            sa.String(24),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
        sa.Column(
            "health_state", json_type, nullable=False, server_default=empty_object
        ),
        sa.Column(
            "recovery_state", json_type, nullable=False, server_default=empty_object
        ),
        sa.Column(
            "scheduling_state",
            json_type,
            nullable=False,
            server_default=empty_object,
        ),
        sa.Column(
            "rollout_state", json_type, nullable=False, server_default=empty_object
        ),
        sa.Column("next_wakeup_at", sa.DateTime(timezone=True)),
        sa.Column("last_wakeup_at", sa.DateTime(timezone=True)),
        sa.Column("last_healthy_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint(
            "engine_owner IN ('legacy','strategic_resident')",
            name="chk_strategicresident_engine_owner",
        ),
        sa.CheckConstraint(
            "background_policy IN ('active','paused')",
            name="chk_strategicresident_background_policy",
        ),
        sa.CheckConstraint(
            "authority_generation >= 1",
            name="chk_strategicresident_authority_generation",
        ),
        sa.CheckConstraint(
            "active_turn_lane IS NULL OR active_turn_lane IN ('interactive','background')",
            name="chk_strategicresident_active_lane",
        ),
        sa.CheckConstraint(
            "(active_turn_task_id IS NULL AND active_turn_lane IS NULL) OR "
            "(active_turn_task_id IS NOT NULL AND active_turn_lane IS NOT NULL)",
            name="chk_strategicresident_active_pair",
        ),
        sa.CheckConstraint(
            "active_turn_task_id IS NULL OR interactive_successor_task_id IS NULL "
            "OR active_turn_task_id <> interactive_successor_task_id",
            name="chk_strategicresident_active_interactive_distinct",
        ),
        sa.CheckConstraint(
            "active_turn_task_id IS NULL OR background_successor_task_id IS NULL "
            "OR active_turn_task_id <> background_successor_task_id",
            name="chk_strategicresident_active_background_distinct",
        ),
        sa.CheckConstraint(
            "interactive_successor_task_id IS NULL OR "
            "background_successor_task_id IS NULL OR "
            "interactive_successor_task_id <> background_successor_task_id",
            name="chk_strategicresident_successors_distinct",
        ),
        sa.CheckConstraint(
            "current_checkpoint_id IS NULL OR previous_checkpoint_id IS NULL OR "
            "current_checkpoint_id <> previous_checkpoint_id",
            name="chk_strategicresident_checkpoints_distinct",
        ),
        sa.CheckConstraint(
            "handled_message_sequence >= 0",
            name="chk_strategicresident_message_cursor",
        ),
        sa.CheckConstraint(
            "health_status IN ('unknown','healthy','degraded','recovering','blocked')",
            name="chk_strategicresident_health_status",
        ),
    )

    op.create_table(
        "strategicresidentcheckpoint",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey(f"{_RESIDENT_TABLE}.project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column(
            "schema_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("checkpoint", json_type, nullable=False, server_default=empty_object),
        sa.Column(
            "authority_generation",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "source_turn_task_id",
            sa.Uuid(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.UniqueConstraint(
            "project_id",
            "version",
            name="uq_strategicresidentcheckpoint_project_version",
        ),
        sa.CheckConstraint(
            "version > 0", name="chk_strategicresidentcheckpoint_version"
        ),
        sa.CheckConstraint(
            "schema_version > 0",
            name="chk_strategicresidentcheckpoint_schema_version",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="chk_strategicresidentcheckpoint_hash",
        ),
        sa.CheckConstraint(
            "authority_generation >= 1",
            name="chk_strategicresidentcheckpoint_authority_generation",
        ),
        sa.CheckConstraint(
            "(version = 1 AND source_turn_task_id IS NULL) OR "
            "(version > 1 AND source_turn_task_id IS NOT NULL)",
            name="chk_strategicresidentcheckpoint_source_version",
        ),
    )

    op.create_table(
        "strategicoperationreceipt",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey(f"{_RESIDENT_TABLE}.project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Durable UUID identity without a Task FK: receipts outlive Task cleanup.
        sa.Column("turn_task_id", sa.Uuid(), nullable=False),
        sa.Column("authority_generation", sa.BigInteger(), nullable=False),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("operation_kind", sa.String(64), nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column(
            "risk_level",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'low'"),
        ),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "expected_revisions",
            json_type,
            nullable=False,
            server_default=empty_object,
        ),
        sa.Column("observed_revisions_before", json_type),
        sa.Column("observed_revisions_after", json_type),
        sa.Column(
            "status",
            sa.String(24),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("receipt", json_type, nullable=False, server_default=empty_object),
        sa.Column(
            "unknown_effects", json_type, nullable=False, server_default=empty_list
        ),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_detail", sa.String(1000)),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "project_id",
            "operation_id",
            name="uq_strategicoperationreceipt_project_operation",
        ),
        sa.CheckConstraint(
            "authority_generation >= 1",
            name="chk_strategicoperationreceipt_authority_generation",
        ),
        sa.CheckConstraint(
            "risk_level IN ('low','medium','high','critical')",
            name="chk_strategicoperationreceipt_risk_level",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="chk_strategicoperationreceipt_request_hash",
        ),
        sa.CheckConstraint(
            "status IN ('pending','applying','applied','reconciling',"
            "'reconciled','failed','unknown_effect')",
            name="chk_strategicoperationreceipt_status",
        ),
    )


def _create_indexes(*, postgresql_dialect: bool) -> None:
    del postgresql_dialect
    op.create_index(
        "ix_strategicresident_scheduler",
        "strategicresident",
        ["next_wakeup_at", "project_id"],
        postgresql_where=sa.text(
            "engine_owner = 'strategic_resident' AND background_policy = 'active' "
            "AND next_wakeup_at IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "engine_owner = 'strategic_resident' AND background_policy = 'active' "
            "AND next_wakeup_at IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_strategicresident_owner_health",
        "strategicresident",
        ["engine_owner", "health_status"],
    )
    op.create_index(
        "ix_strategicresident_active_turn_task",
        "strategicresident",
        ["active_turn_task_id"],
    )
    op.create_index(
        "ix_strategicresident_interactive_successor",
        "strategicresident",
        ["interactive_successor_task_id"],
    )
    op.create_index(
        "ix_strategicresident_background_successor",
        "strategicresident",
        ["background_successor_task_id"],
    )
    op.create_index(
        "ix_strategicresidentcheckpoint_project_created",
        "strategicresidentcheckpoint",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_strategicoperationreceipt_project_status_updated",
        "strategicoperationreceipt",
        ["project_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_strategicoperationreceipt_task_started",
        "strategicoperationreceipt",
        ["turn_task_id", "started_at"],
    )


def _resident_table(json_type: sa.types.TypeEngine) -> sa.TableClause:
    return sa.table(
        _RESIDENT_TABLE,
        sa.column("project_id", sa.Uuid()),
        sa.column("engine_owner", sa.String()),
        sa.column("engine_version", sa.String()),
        sa.column("background_policy", sa.String()),
        sa.column("authority_generation", sa.BigInteger()),
        sa.column("handled_message_sequence", sa.BigInteger()),
        sa.column("handled_project_change_cursor", json_type),
        sa.column("runner_policy", json_type),
        sa.column("provider_policy", json_type),
        sa.column("failover_policy", json_type),
        sa.column("health_status", sa.String()),
        sa.column("health_state", json_type),
        sa.column("recovery_state", json_type),
        sa.column("scheduling_state", json_type),
        sa.column("rollout_state", json_type),
        sa.column("current_checkpoint_id", sa.Uuid()),
    )


def _checkpoint_table(json_type: sa.types.TypeEngine) -> sa.TableClause:
    return sa.table(
        _CHECKPOINT_TABLE,
        sa.column("id", sa.Uuid()),
        sa.column("project_id", sa.Uuid()),
        sa.column("version", sa.BigInteger()),
        sa.column("schema_version", sa.Integer()),
        sa.column("content_hash", sa.String()),
        sa.column("checkpoint", json_type),
        sa.column("authority_generation", sa.BigInteger()),
    )


def _backfill(*, postgresql_dialect: bool) -> None:
    bind = op.get_bind()
    json_type = _json_type(postgresql_dialect=postgresql_dialect)
    resident = _resident_table(json_type)
    checkpoint = _checkpoint_table(json_type)
    mode_summary = _load_interaction_mode_summary(postgresql_dialect=postgresql_dialect)
    message_cursors = _load_handled_message_cursors()
    last_project_id: Any = None
    while True:
        where = "" if last_project_id is None else "WHERE id > :last_project_id"
        project_rows = list(
            bind.execute(
                sa.text(
                    f"""
                    SELECT id, vision, problem_model, directions,
                           strategic_memory_notes, running_update, paused
                    FROM project
                    {where}
                    ORDER BY id
                    LIMIT :batch_size
                    """
                ),
                {
                    "last_project_id": last_project_id,
                    "batch_size": _BACKFILL_BATCH_SIZE,
                },
            ).mappings()
        )
        if not project_rows:
            break

        resident_values: list[dict[str, Any]] = []
        checkpoint_values: list[dict[str, Any]] = []
        project_ids: list[uuid.UUID] = []
        for raw_row in project_rows:
            row = dict(raw_row)
            for field in (
                "vision",
                "problem_model",
                "directions",
                "strategic_memory_notes",
            ):
                row[field] = _decode_json_value(row.get(field))
            raw_project_id = row["id"]
            project_id = (
                raw_project_id
                if isinstance(raw_project_id, uuid.UUID)
                else uuid.UUID(str(raw_project_id))
            )
            project_ids.append(project_id)
            handled_message_sequence = message_cursors.get(project_id, 0)
            row["handled_message_sequence"] = handled_message_sequence
            has_continuous, has_chat = mode_summary.get(project_id, (False, False))
            resident_values.append(
                {
                    "project_id": project_id,
                    "engine_owner": "legacy",
                    "engine_version": "legacy-v1",
                    "background_policy": _background_policy_for(
                        project_paused=row.get("paused"),
                        has_continuous=has_continuous,
                        has_chat=has_chat,
                    ),
                    "authority_generation": 1,
                    "handled_message_sequence": handled_message_sequence,
                    "handled_project_change_cursor": {},
                    "runner_policy": {},
                    "provider_policy": {},
                    "failover_policy": {},
                    "health_status": "unknown",
                    "health_state": {},
                    "recovery_state": {},
                    "scheduling_state": {},
                    "rollout_state": _legacy_rollout_state(row.get("vision")),
                }
            )
            content, content_hash = _build_initial_checkpoint(row)
            checkpoint_values.append(
                {
                    "id": _checkpoint_id(project_id),
                    "project_id": project_id,
                    "version": 1,
                    "schema_version": 1,
                    "content_hash": content_hash,
                    "checkpoint": content,
                    "authority_generation": 1,
                }
            )

        resident_insert = (
            postgresql.insert(resident)
            if postgresql_dialect
            else sqlite.insert(resident)
        ).values(resident_values)
        bind.execute(
            resident_insert.on_conflict_do_nothing(index_elements=["project_id"])
        )
        checkpoint_insert = (
            postgresql.insert(checkpoint)
            if postgresql_dialect
            else sqlite.insert(checkpoint)
        ).values(checkpoint_values)
        bind.execute(
            checkpoint_insert.on_conflict_do_nothing(
                index_elements=["project_id", "version"]
            )
        )
        actual_checkpoint_id = (
            sa.select(checkpoint.c.id)
            .where(
                checkpoint.c.project_id == resident.c.project_id,
                checkpoint.c.version == 1,
            )
            .correlate(resident)
            .scalar_subquery()
        )
        bind.execute(
            sa.update(resident)
            .where(
                resident.c.project_id.in_(project_ids),
                resident.c.current_checkpoint_id.is_(None),
            )
            .values(current_checkpoint_id=actual_checkpoint_id)
        )
        last_project_id = project_rows[-1]["id"]


def _add_checkpoint_pointer_fks(*, postgresql_dialect: bool) -> None:
    if postgresql_dialect:
        op.create_foreign_key(
            "fk_strategicresident_current_checkpoint",
            _RESIDENT_TABLE,
            _CHECKPOINT_TABLE,
            ["current_checkpoint_id"],
            ["id"],
            ondelete="SET NULL",
            use_alter=True,
        )
        op.create_foreign_key(
            "fk_strategicresident_previous_checkpoint",
            _RESIDENT_TABLE,
            _CHECKPOINT_TABLE,
            ["previous_checkpoint_id"],
            ["id"],
            ondelete="SET NULL",
            use_alter=True,
        )
        return
    # SQLite pointer FKs are declared inline in _create_tables; no parent-table
    # rebuild is safe after checkpoint rows have been backfilled.


def _install_postgresql_triggers() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_strategicresident_authority_monotonic()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.authority_generation < OLD.authority_generation THEN
            RAISE EXCEPTION 'Resident authority generation cannot decrease'
              USING ERRCODE = '23514';
          END IF;
          IF NEW.engine_owner IS DISTINCT FROM OLD.engine_owner
             AND NEW.authority_generation <= OLD.authority_generation THEN
            RAISE EXCEPTION 'Resident ownership flip must advance authority generation'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicresident_authority_monotonic
        BEFORE UPDATE OF engine_owner, authority_generation
        ON {_RESIDENT_TABLE}
        FOR EACH ROW EXECUTE FUNCTION enforce_strategicresident_authority_monotonic()
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION enforce_strategicresident_project_links()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE linked_project uuid;
        BEGIN
          IF NEW.active_turn_task_id IS NOT NULL THEN
            SELECT project_id INTO linked_project FROM task
            WHERE id = NEW.active_turn_task_id FOR SHARE;
            IF linked_project IS DISTINCT FROM NEW.project_id THEN
              RAISE EXCEPTION 'active Resident task must belong to project'
                USING ERRCODE = '23503';
            END IF;
          END IF;
          IF NEW.interactive_successor_task_id IS NOT NULL THEN
            linked_project := NULL;
            SELECT project_id INTO linked_project FROM task
            WHERE id = NEW.interactive_successor_task_id FOR SHARE;
            IF linked_project IS DISTINCT FROM NEW.project_id THEN
              RAISE EXCEPTION 'interactive successor must belong to project'
                USING ERRCODE = '23503';
            END IF;
          END IF;
          IF NEW.background_successor_task_id IS NOT NULL THEN
            linked_project := NULL;
            SELECT project_id INTO linked_project FROM task
            WHERE id = NEW.background_successor_task_id FOR SHARE;
            IF linked_project IS DISTINCT FROM NEW.project_id THEN
              RAISE EXCEPTION 'background successor must belong to project'
                USING ERRCODE = '23503';
            END IF;
          END IF;
          IF NEW.current_checkpoint_id IS NOT NULL THEN
            linked_project := NULL;
            SELECT project_id INTO linked_project FROM {_CHECKPOINT_TABLE}
            WHERE id = NEW.current_checkpoint_id FOR SHARE;
            IF linked_project IS DISTINCT FROM NEW.project_id THEN
              RAISE EXCEPTION 'current checkpoint must belong to project'
                USING ERRCODE = '23503';
            END IF;
          END IF;
          IF NEW.previous_checkpoint_id IS NOT NULL THEN
            linked_project := NULL;
            SELECT project_id INTO linked_project FROM {_CHECKPOINT_TABLE}
            WHERE id = NEW.previous_checkpoint_id FOR SHARE;
            IF linked_project IS DISTINCT FROM NEW.project_id THEN
              RAISE EXCEPTION 'previous checkpoint must belong to project'
                USING ERRCODE = '23503';
            END IF;
          END IF;
          RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicresident_project_links
        BEFORE INSERT OR UPDATE OF project_id, active_turn_task_id,
          interactive_successor_task_id, background_successor_task_id,
          current_checkpoint_id, previous_checkpoint_id
        ON {_RESIDENT_TABLE}
        FOR EACH ROW EXECUTE FUNCTION enforce_strategicresident_project_links()
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION fence_strategicoperationreceipt_insert()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          PERFORM 1
            FROM {_RESIDENT_TABLE} resident
            JOIN {_CHECKPOINT_TABLE} current_checkpoint
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
              AND NEW.expected_revisions ? 'authority_generation'
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
            FOR SHARE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'Resident mutation authority is stale or invalid'
              USING ERRCODE = '42501';
          END IF;
          RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicoperationreceipt_authority
        BEFORE INSERT ON {_RECEIPT_TABLE}
        FOR EACH ROW EXECUTE FUNCTION fence_strategicoperationreceipt_insert()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_strategicoperationreceipt_immutability()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.status IN ('applied','reconciled','failed') THEN
            RAISE EXCEPTION 'terminal Resident operation receipt is immutable'
              USING ERRCODE = '23514';
          END IF;
          IF NEW.project_id IS DISTINCT FROM OLD.project_id
             OR NEW.turn_task_id IS DISTINCT FROM OLD.turn_task_id
             OR NEW.authority_generation IS DISTINCT FROM OLD.authority_generation
             OR NEW.operation_id IS DISTINCT FROM OLD.operation_id
             OR NEW.operation_kind IS DISTINCT FROM OLD.operation_kind
             OR NEW.capability IS DISTINCT FROM OLD.capability
             OR NEW.risk_level IS DISTINCT FROM OLD.risk_level
             OR NEW.request_hash IS DISTINCT FROM OLD.request_hash
             OR NEW.expected_revisions IS DISTINCT FROM OLD.expected_revisions
             OR NEW.observed_revisions_before IS DISTINCT FROM OLD.observed_revisions_before
             OR NEW.started_at IS DISTINCT FROM OLD.started_at THEN
            RAISE EXCEPTION 'Resident operation receipt identity is immutable'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicoperationreceipt_immutability
        BEFORE UPDATE ON {_RECEIPT_TABLE}
        FOR EACH ROW EXECUTE FUNCTION enforce_strategicoperationreceipt_immutability()
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION fence_strategicresidentcheckpoint_insert()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          current_checkpoint uuid;
          current_version bigint;
          current_hash text;
        BEGIN
          IF NEW.version = 1 THEN
            RETURN NEW;
          END IF;
          SELECT resident.current_checkpoint_id
            INTO current_checkpoint
            FROM {_RESIDENT_TABLE} resident
            WHERE resident.project_id = NEW.project_id
              AND resident.engine_owner = 'strategic_resident'
              AND resident.authority_generation = NEW.authority_generation
              AND resident.active_turn_task_id = NEW.source_turn_task_id
            FOR SHARE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'Resident checkpoint authority is stale or invalid'
              USING ERRCODE = '42501';
          END IF;
          SELECT checkpoint.version, checkpoint.content_hash
            INTO current_version, current_hash
            FROM {_CHECKPOINT_TABLE} checkpoint
            WHERE checkpoint.id = current_checkpoint
              AND checkpoint.project_id = NEW.project_id
            FOR SHARE;
          IF NOT FOUND OR NEW.version <> current_version + 1 THEN
            RAISE EXCEPTION 'Resident checkpoint lineage is not contiguous'
              USING ERRCODE = '23514';
          END IF;
          PERFORM 1
            FROM task turn_task
            WHERE turn_task.id = NEW.source_turn_task_id
              AND turn_task.project_id = NEW.project_id
              AND turn_task.payloads->0->>'type' = 'strategic_resident_turn'
              AND turn_task.payloads->0->>'resident_id' = NEW.project_id::text
              AND (turn_task.payloads->0->>'authority_generation')::bigint
                    = NEW.authority_generation
              AND turn_task.payloads->0->>'engine_owner'
                    = 'strategic_resident'
              AND (turn_task.payloads->0->>'checkpoint_version')::bigint
                    = current_version
              AND turn_task.payloads->0->>'checkpoint_hash' = current_hash
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
            FOR SHARE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'Resident checkpoint source task is invalid'
              USING ERRCODE = '42501';
          END IF;
          RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicresidentcheckpoint_authority
        BEFORE INSERT ON {_CHECKPOINT_TABLE}
        FOR EACH ROW EXECUTE FUNCTION fence_strategicresidentcheckpoint_insert()
        """
    )
    op.execute(
        f"""
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
          FROM {_CHECKPOINT_TABLE}
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
          FROM {_CHECKPOINT_TABLE}
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
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicresident_checkpoint_rotation
        BEFORE UPDATE OF current_checkpoint_id, previous_checkpoint_id
        ON {_RESIDENT_TABLE}
        FOR EACH ROW EXECUTE FUNCTION fence_strategicresident_checkpoint_rotation()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_strategicresidentcheckpoint_immutability()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'Strategic Resident checkpoints are immutable'
            USING ERRCODE = '23514';
        END $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicresidentcheckpoint_immutability
        BEFORE UPDATE ON {_CHECKPOINT_TABLE}
        FOR EACH ROW EXECUTE FUNCTION enforce_strategicresidentcheckpoint_immutability()
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION protect_strategicresident_task_projects()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM {_RESIDENT_TABLE} resident
            WHERE (
              resident.active_turn_task_id = NEW.id OR
              resident.interactive_successor_task_id = NEW.id OR
              resident.background_successor_task_id = NEW.id
            ) AND resident.project_id <> NEW.project_id
          ) THEN
            RAISE EXCEPTION 'task move would strand Resident task pointer'
              USING ERRCODE = '23503';
          END IF;
          RETURN NEW;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_task_strategicresident_project_links
        BEFORE UPDATE OF project_id ON task
        FOR EACH ROW EXECUTE FUNCTION protect_strategicresident_task_projects()
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION clear_strategicresident_active_turn_on_task_delete()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          UPDATE {_RESIDENT_TABLE}
          SET active_turn_task_id = NULL,
              active_turn_lane = NULL,
              updated_at = now()
          WHERE active_turn_task_id = OLD.id;
          RETURN OLD;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_task_strategicresident_active_delete
        BEFORE DELETE ON task
        FOR EACH ROW EXECUTE FUNCTION clear_strategicresident_active_turn_on_task_delete()
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION protect_strategicresident_checkpoint_projects()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM {_RESIDENT_TABLE} resident
            WHERE (
              resident.current_checkpoint_id = NEW.id OR
              resident.previous_checkpoint_id = NEW.id
            ) AND resident.project_id <> NEW.project_id
          ) THEN
            RAISE EXCEPTION 'checkpoint move would strand Resident pointer'
              USING ERRCODE = '23503';
          END IF;
          RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_checkpoint_strategicresident_project_links
        BEFORE UPDATE OF project_id ON {_CHECKPOINT_TABLE}
        FOR EACH ROW EXECUTE FUNCTION protect_strategicresident_checkpoint_projects()
        """
    )


def _install_sqlite_triggers() -> None:
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicresident_authority_monotonic
        BEFORE UPDATE OF engine_owner, authority_generation
        ON {_RESIDENT_TABLE}
        FOR EACH ROW WHEN
          NEW.authority_generation < OLD.authority_generation OR
          (
            NEW.engine_owner <> OLD.engine_owner AND
            NEW.authority_generation <= OLD.authority_generation
          )
        BEGIN
          SELECT RAISE(ABORT, 'Resident authority generation is not monotonic');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicresident_project_links_insert
        BEFORE INSERT ON {_RESIDENT_TABLE}
        FOR EACH ROW WHEN
          (NEW.active_turn_task_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM task WHERE id = NEW.active_turn_task_id
              AND project_id = NEW.project_id
          )) OR
          (NEW.interactive_successor_task_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM task WHERE id = NEW.interactive_successor_task_id
              AND project_id = NEW.project_id
          )) OR
          (NEW.background_successor_task_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM task WHERE id = NEW.background_successor_task_id
              AND project_id = NEW.project_id
          )) OR
          (NEW.current_checkpoint_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM {_CHECKPOINT_TABLE}
            WHERE id = NEW.current_checkpoint_id AND project_id = NEW.project_id
          )) OR
          (NEW.previous_checkpoint_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM {_CHECKPOINT_TABLE}
            WHERE id = NEW.previous_checkpoint_id AND project_id = NEW.project_id
          ))
        BEGIN
          SELECT RAISE(ABORT, 'strategicresident link crosses project boundary');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicresident_project_links_update
        BEFORE UPDATE OF project_id, active_turn_task_id,
          interactive_successor_task_id, background_successor_task_id,
          current_checkpoint_id, previous_checkpoint_id
        ON {_RESIDENT_TABLE}
        FOR EACH ROW WHEN
          (NEW.active_turn_task_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM task WHERE id = NEW.active_turn_task_id
              AND project_id = NEW.project_id
          )) OR
          (NEW.interactive_successor_task_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM task WHERE id = NEW.interactive_successor_task_id
              AND project_id = NEW.project_id
          )) OR
          (NEW.background_successor_task_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM task WHERE id = NEW.background_successor_task_id
              AND project_id = NEW.project_id
          )) OR
          (NEW.current_checkpoint_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM {_CHECKPOINT_TABLE}
            WHERE id = NEW.current_checkpoint_id AND project_id = NEW.project_id
          )) OR
          (NEW.previous_checkpoint_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM {_CHECKPOINT_TABLE}
            WHERE id = NEW.previous_checkpoint_id AND project_id = NEW.project_id
          ))
        BEGIN
          SELECT RAISE(ABORT, 'strategicresident link crosses project boundary');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicoperationreceipt_authority
        BEFORE INSERT ON {_RECEIPT_TABLE}
        FOR EACH ROW WHEN NOT EXISTS (
          SELECT 1
          FROM {_RESIDENT_TABLE} resident
          JOIN task turn_task ON turn_task.id = NEW.turn_task_id
          JOIN {_CHECKPOINT_TABLE} current_checkpoint
            ON current_checkpoint.id = resident.current_checkpoint_id
           AND current_checkpoint.project_id = resident.project_id
          WHERE resident.project_id = NEW.project_id
            AND resident.engine_owner = 'strategic_resident'
            AND resident.authority_generation = NEW.authority_generation
            AND resident.active_turn_task_id = NEW.turn_task_id
            AND turn_task.project_id = NEW.project_id
            AND json_extract(turn_task.payloads, '$[0].type')
              = 'strategic_resident_turn'
            AND replace(
              json_extract(turn_task.payloads, '$[0].resident_id'), '-', ''
            ) = replace(CAST(NEW.project_id AS TEXT), '-', '')
            AND CAST(json_extract(
              turn_task.payloads, '$[0].authority_generation'
            ) AS INTEGER) = NEW.authority_generation
            AND json_extract(turn_task.payloads, '$[0].engine_owner')
              = 'strategic_resident'
            AND CAST(json_extract(
              turn_task.payloads, '$[0].checkpoint_version'
            ) AS INTEGER) = current_checkpoint.version
            AND json_extract(turn_task.payloads, '$[0].checkpoint_hash')
              = current_checkpoint.content_hash
            AND turn_task.status = 'IN_PROGRESS'
            AND json_extract(
              turn_task.context, '$._resident_mutation_authorized'
            ) = 1
            AND json_extract(
              turn_task.context, '$._resident_input_frozen'
            ) = 1
            AND replace(
              json_extract(turn_task.context, '$._resident_project_id'), '-', ''
            ) = replace(CAST(NEW.project_id AS TEXT), '-', '')
            AND CAST(json_extract(
              turn_task.context, '$._resident_authority_generation'
            ) AS INTEGER) = NEW.authority_generation
            AND json_extract(
              turn_task.context, '$._resident_engine_owner_observed'
            ) = 'strategic_resident'
            AND json_type(
              NEW.expected_revisions, '$.authority_generation'
            ) IS NOT NULL
            AND CAST(json_extract(
              NEW.expected_revisions, '$.authority_generation'
            ) AS INTEGER) = NEW.authority_generation
            AND json_type(
              NEW.expected_revisions, '$.active_turn_task_version'
            ) IS NOT NULL
            AND CAST(json_extract(
              NEW.expected_revisions, '$.active_turn_task_version'
            ) AS INTEGER) = turn_task.version
            AND json_type(
              NEW.expected_revisions, '$.current_checkpoint_id'
            ) IS NOT NULL
            AND replace(
              json_extract(
                NEW.expected_revisions, '$.current_checkpoint_id'
              ),
              '-',
              ''
            ) IS replace(CAST(resident.current_checkpoint_id AS TEXT), '-', '')
            AND json_type(
              NEW.expected_revisions, '$.resident_checkpoint_version'
            ) IS NOT NULL
            AND CAST(json_extract(
              NEW.expected_revisions, '$.resident_checkpoint_version'
            ) AS INTEGER) IS current_checkpoint.version
            AND json_type(
              NEW.expected_revisions, '$.resident_checkpoint_hash'
            ) IS NOT NULL
            AND json_extract(
              NEW.expected_revisions, '$.resident_checkpoint_hash'
            ) IS current_checkpoint.content_hash
        )
        BEGIN
          SELECT RAISE(ABORT, 'Resident mutation authority is stale or invalid');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicoperationreceipt_immutability
        BEFORE UPDATE ON {_RECEIPT_TABLE}
        FOR EACH ROW WHEN
          OLD.status IN ('applied','reconciled','failed') OR
          NEW.project_id IS NOT OLD.project_id OR
          NEW.turn_task_id IS NOT OLD.turn_task_id OR
          NEW.authority_generation IS NOT OLD.authority_generation OR
          NEW.operation_id IS NOT OLD.operation_id OR
          NEW.operation_kind IS NOT OLD.operation_kind OR
          NEW.capability IS NOT OLD.capability OR
          NEW.risk_level IS NOT OLD.risk_level OR
          NEW.request_hash IS NOT OLD.request_hash OR
          NEW.expected_revisions IS NOT OLD.expected_revisions OR
          NEW.observed_revisions_before IS NOT OLD.observed_revisions_before OR
          NEW.started_at IS NOT OLD.started_at
        BEGIN
          SELECT RAISE(ABORT, 'Resident operation receipt is immutable');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicresidentcheckpoint_authority
        BEFORE INSERT ON {_CHECKPOINT_TABLE}
        FOR EACH ROW WHEN NEW.version > 1 AND NOT EXISTS (
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
            AND replace(
              json_extract(turn_task.payloads, '$[0].resident_id'), '-', ''
            ) = replace(CAST(NEW.project_id AS TEXT), '-', '')
            AND CAST(json_extract(
              turn_task.payloads, '$[0].authority_generation'
            ) AS INTEGER) = NEW.authority_generation
            AND json_extract(turn_task.payloads, '$[0].engine_owner')
              = 'strategic_resident'
            AND CAST(json_extract(
              turn_task.payloads, '$[0].checkpoint_version'
            ) AS INTEGER) = current_checkpoint.version
            AND json_extract(turn_task.payloads, '$[0].checkpoint_hash')
              = current_checkpoint.content_hash
            AND turn_task.status = 'IN_PROGRESS'
            AND json_extract(
              turn_task.context, '$._resident_mutation_authorized'
            ) = 1
            AND json_extract(
              turn_task.context, '$._resident_input_frozen'
            ) = 1
            AND replace(
              json_extract(turn_task.context, '$._resident_project_id'), '-', ''
            ) = replace(CAST(NEW.project_id AS TEXT), '-', '')
            AND CAST(json_extract(
              turn_task.context, '$._resident_authority_generation'
            ) AS INTEGER) = NEW.authority_generation
            AND json_extract(
              turn_task.context, '$._resident_engine_owner_observed'
            ) = 'strategic_resident'
        )
        BEGIN
          SELECT RAISE(ABORT, 'Resident checkpoint authority is stale or invalid');
        END
        """
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
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicresident_checkpoint_rotation
        BEFORE UPDATE OF current_checkpoint_id, previous_checkpoint_id
        ON {_RESIDENT_TABLE}
        FOR EACH ROW WHEN
          (
            NEW.current_checkpoint_id IS NOT OLD.current_checkpoint_id OR
            NEW.previous_checkpoint_id IS NOT OLD.previous_checkpoint_id
          )
          AND NEW.current_checkpoint_id IS NOT NULL
          AND NEW.authority_generation = OLD.authority_generation
          AND NOT (
            OLD.engine_owner = 'legacy' AND
            OLD.current_checkpoint_id IS NULL AND
            NEW.previous_checkpoint_id IS NULL AND
            EXISTS (
              SELECT 1 FROM {_CHECKPOINT_TABLE} initial_checkpoint
              WHERE initial_checkpoint.id = NEW.current_checkpoint_id
                AND initial_checkpoint.project_id = NEW.project_id
                AND initial_checkpoint.version = 1
                AND initial_checkpoint.authority_generation
                  = OLD.authority_generation
            )
          )
          AND NOT EXISTS (
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
              AND replace(
                json_extract(turn_task.payloads, '$[0].resident_id'), '-', ''
              ) = replace(CAST(OLD.project_id AS TEXT), '-', '')
              AND CAST(json_extract(
                turn_task.payloads, '$[0].authority_generation'
              ) AS INTEGER) = OLD.authority_generation
              AND json_extract(turn_task.payloads, '$[0].engine_owner')
                = 'strategic_resident'
              AND CAST(json_extract(
                turn_task.payloads, '$[0].checkpoint_version'
              ) AS INTEGER) = old_checkpoint.version
              AND json_extract(turn_task.payloads, '$[0].checkpoint_hash')
                = old_checkpoint.content_hash
              AND turn_task.status = 'IN_PROGRESS'
              AND json_extract(
                turn_task.context, '$._resident_mutation_authorized'
              ) = 1
              AND json_extract(
                turn_task.context, '$._resident_input_frozen'
              ) = 1
              AND replace(
                json_extract(turn_task.context, '$._resident_project_id'), '-', ''
              ) = replace(CAST(OLD.project_id AS TEXT), '-', '')
              AND CAST(json_extract(
                turn_task.context, '$._resident_authority_generation'
              ) AS INTEGER) = OLD.authority_generation
              AND json_extract(
                turn_task.context, '$._resident_engine_owner_observed'
              ) = 'strategic_resident'
              AND NEW.previous_checkpoint_id = OLD.current_checkpoint_id
              AND new_checkpoint.version = old_checkpoint.version + 1
              AND new_checkpoint.source_turn_task_id = OLD.active_turn_task_id
              AND new_checkpoint.authority_generation = OLD.authority_generation
          )
        BEGIN
          SELECT RAISE(ABORT, 'Resident checkpoint rotation is not authorized');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_strategicresidentcheckpoint_immutability
        BEFORE UPDATE ON {_CHECKPOINT_TABLE}
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, 'Strategic Resident checkpoints are immutable');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_task_strategicresident_project_links
        BEFORE UPDATE OF project_id ON task
        FOR EACH ROW WHEN EXISTS (
          SELECT 1 FROM {_RESIDENT_TABLE} resident
          WHERE (
            resident.active_turn_task_id = NEW.id OR
            resident.interactive_successor_task_id = NEW.id OR
            resident.background_successor_task_id = NEW.id
          ) AND resident.project_id <> NEW.project_id
        )
        BEGIN
          SELECT RAISE(ABORT, 'task move strands Resident pointer');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_task_strategicresident_active_delete
        BEFORE DELETE ON task
        FOR EACH ROW
        BEGIN
          UPDATE {_RESIDENT_TABLE}
          SET active_turn_task_id = NULL,
              active_turn_lane = NULL,
              updated_at = CURRENT_TIMESTAMP
          WHERE active_turn_task_id = OLD.id;
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_checkpoint_strategicresident_project_links
        BEFORE UPDATE OF project_id ON {_CHECKPOINT_TABLE}
        FOR EACH ROW WHEN EXISTS (
          SELECT 1 FROM {_RESIDENT_TABLE} resident
          WHERE (
            resident.current_checkpoint_id = NEW.id OR
            resident.previous_checkpoint_id = NEW.id
          ) AND resident.project_id <> NEW.project_id
        )
        BEGIN
          SELECT RAISE(ABORT, 'checkpoint move strands Resident pointer');
        END
        """
    )


def upgrade() -> None:
    postgresql_dialect = op.get_bind().dialect.name == "postgresql"
    _create_tables(postgresql_dialect=postgresql_dialect)
    _create_indexes(postgresql_dialect=postgresql_dialect)
    _backfill(postgresql_dialect=postgresql_dialect)
    _add_checkpoint_pointer_fks(postgresql_dialect=postgresql_dialect)
    if postgresql_dialect:
        _install_postgresql_triggers()
    else:
        _install_sqlite_triggers()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for trigger, table in (
            ("trg_strategicresidentcheckpoint_immutability", _CHECKPOINT_TABLE),
            ("trg_strategicresidentcheckpoint_authority", _CHECKPOINT_TABLE),
            ("trg_checkpoint_strategicresident_project_links", _CHECKPOINT_TABLE),
            ("trg_task_strategicresident_active_delete", "task"),
            ("trg_task_strategicresident_project_links", "task"),
            ("trg_strategicoperationreceipt_authority", _RECEIPT_TABLE),
            ("trg_strategicoperationreceipt_immutability", _RECEIPT_TABLE),
            ("trg_strategicresident_project_links", _RESIDENT_TABLE),
            ("trg_strategicresident_authority_monotonic", _RESIDENT_TABLE),
            ("trg_strategicresident_checkpoint_rotation", _RESIDENT_TABLE),
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        for function in (
            "enforce_strategicresidentcheckpoint_immutability",
            "fence_strategicresidentcheckpoint_insert",
            "protect_strategicresident_checkpoint_projects",
            "clear_strategicresident_active_turn_on_task_delete",
            "protect_strategicresident_task_projects",
            "fence_strategicoperationreceipt_insert",
            "enforce_strategicoperationreceipt_immutability",
            "enforce_strategicresident_project_links",
            "enforce_strategicresident_authority_monotonic",
            "fence_strategicresident_checkpoint_rotation",
        ):
            op.execute(f"DROP FUNCTION IF EXISTS {function}()")
        op.drop_constraint(
            "fk_strategicresident_previous_checkpoint",
            _RESIDENT_TABLE,
            type_="foreignkey",
        )
        op.drop_constraint(
            "fk_strategicresident_current_checkpoint",
            _RESIDENT_TABLE,
            type_="foreignkey",
        )
    else:
        for trigger in (
            "trg_strategicresidentcheckpoint_immutability",
            "trg_strategicresidentcheckpoint_authority",
            "trg_checkpoint_strategicresident_project_links",
            "trg_task_strategicresident_active_delete",
            "trg_task_strategicresident_project_links",
            "trg_strategicoperationreceipt_authority",
            "trg_strategicoperationreceipt_immutability",
            "trg_strategicresident_project_links_update",
            "trg_strategicresident_project_links_insert",
            "trg_strategicresident_authority_monotonic",
            "trg_strategicresident_checkpoint_rotation",
            "trg_strategicresident_checkpoint_clear",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")

    op.drop_table(_RECEIPT_TABLE)
    op.drop_table(_CHECKPOINT_TABLE)
    op.drop_table(_RESIDENT_TABLE)

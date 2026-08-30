"""Add bounded Workday recovery lookup indexes.

Revision ID: 0767
Revises: 0766
Create Date: 2026-08-29

The continuation poll performs these project-scoped lookups whenever its
private queue is empty.  Build both indexes concurrently so the additive
repair does not block the live conversation table.
"""

from __future__ import annotations

from typing import Any, Mapping

from alembic import op
import sqlalchemy as sa


revision = "0767"
down_revision = "0766"
branch_labels = None
depends_on = None

_RECOVERY_INDEX = "ix_projectmessage_workday_recovery_candidates"
_ATOMIC_REPLY_INDEX = "ix_projectmessage_workday_atomic_reply_id"
_REQUESTED_MARKER = "_workday_consultation_requested"
_ATOMIC_REPLY_MARKER = "_workday_consultation_atomic_reply_id"

_CREATE_SQL = {
    _RECOVERY_INDEX: (
        f"CREATE INDEX CONCURRENTLY {_RECOVERY_INDEX} "
        "ON public.projectmessage (project_id, sequence DESC, id) "
        "WHERE sequence IS NOT NULL "
        "AND from_type = 'strategic' "
        "AND surface = 'conversation' "
        f"AND context @> '{{\"{_REQUESTED_MARKER}\": true}}'::jsonb"
    ),
    _ATOMIC_REPLY_INDEX: (
        f"CREATE INDEX CONCURRENTLY {_ATOMIC_REPLY_INDEX} "
        "ON public.projectmessage "
        f"(project_id, ((context ->> '{_ATOMIC_REPLY_MARKER}'))) "
        f"WHERE context ? '{_ATOMIC_REPLY_MARKER}'"
    ),
}

_INDEX_STATE_SQL = sa.text(
    """
    SELECT
      index_ns.nspname AS index_schema,
      table_ns.nspname AS table_schema,
      table_rel.relname AS table_name,
      access_method.amname AS access_method,
      index_state.indisunique AS is_unique,
      index_state.indisprimary AS is_primary,
      index_state.indisvalid AS is_valid,
      index_state.indisready AS is_ready,
      index_state.indislive AS is_live,
      index_state.indnkeyatts AS key_attribute_count,
      index_state.indnatts AS total_attribute_count,
      pg_get_indexdef(index_rel.oid, 0, false) AS definition,
      pg_get_expr(index_state.indpred, index_state.indrelid, false) AS predicate
    FROM pg_class AS index_rel
    JOIN pg_namespace AS index_ns ON index_ns.oid = index_rel.relnamespace
    JOIN pg_index AS index_state ON index_state.indexrelid = index_rel.oid
    JOIN pg_class AS table_rel ON table_rel.oid = index_state.indrelid
    JOIN pg_namespace AS table_ns ON table_ns.oid = table_rel.relnamespace
    JOIN pg_am AS access_method ON access_method.oid = index_rel.relam
    WHERE index_ns.nspname = 'public'
      AND index_rel.relname = :index_name
    """
)


def _index_state(bind: Any, name: str) -> Mapping[str, Any] | None:
    return (
        bind.execute(_INDEX_STATE_SQL, {"index_name": name})
        .mappings()
        .one_or_none()
    )


def _normalized_sql(value: object) -> str:
    return " ".join(str(value or "").split())


def _definition_is_expected(name: str, state: Mapping[str, Any]) -> bool:
    definition = _normalized_sql(state.get("definition"))
    predicate = _normalized_sql(state.get("predicate"))
    common = bool(
        state.get("index_schema") == "public"
        and state.get("table_schema") == "public"
        and state.get("table_name") == "projectmessage"
        and state.get("access_method") == "btree"
        and state.get("is_unique") is False
        and state.get("is_primary") is False
        and definition.startswith(f"CREATE INDEX {name} ON public.projectmessage ")
        and " OR " not in predicate
    )
    if not common:
        return False
    if name == _RECOVERY_INDEX:
        requested = f'context @> \'{{"{_REQUESTED_MARKER}": true}}\'::jsonb'
        return bool(
            state.get("key_attribute_count") == 3
            and state.get("total_attribute_count") == 3
            and "USING btree (project_id, sequence DESC, id)" in definition
            and "sequence IS NOT NULL" in predicate
            and "from_type" in predicate
            and "= 'strategic'::text" in predicate
            and "surface" in predicate
            and "= 'conversation'::text" in predicate
            and requested in predicate
            and predicate.count(" AND ") == 3
        )
    return bool(
        state.get("key_attribute_count") == 2
        and state.get("total_attribute_count") == 2
        and "USING btree (project_id, ((context ->> " in definition
        and f"'{_ATOMIC_REPLY_MARKER}'::text" in definition
        and f"context ? '{_ATOMIC_REPLY_MARKER}'::text" in predicate
        and " AND " not in predicate
    )


def _assert_expected_state(
    name: str,
    state: Mapping[str, Any] | None,
    *,
    healthy: bool,
) -> None:
    if state is None or not _definition_is_expected(name, state):
        raise RuntimeError(
            f"0767 refuses unexpected {name} definition: {dict(state or {})}"
        )
    if healthy:
        valid_state = bool(
            state.get("is_valid") is True
            and state.get("is_ready") is True
            and state.get("is_live") is True
        )
    else:
        valid_state = bool(
            state.get("is_valid") is False and state.get("is_live") is True
        )
    if not valid_state:
        raise RuntimeError(f"0767 {name} index state is not repairable")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.create_index(
            _RECOVERY_INDEX,
            "projectmessage",
            ["project_id", "sequence", "id"],
            sqlite_where=sa.text(
                "sequence IS NOT NULL AND from_type = 'strategic' "
                "AND surface = 'conversation' "
                f"AND json_extract(context, '$.{_REQUESTED_MARKER}') = 1"
            ),
        )
        op.create_index(
            _ATOMIC_REPLY_INDEX,
            "projectmessage",
            ["project_id", "context"],
            sqlite_where=sa.text(
                f"json_type(context, '$.{_ATOMIC_REPLY_MARKER}') IS NOT NULL"
            ),
        )
        return
    if bind.dialect.name != "postgresql":
        raise RuntimeError("0767 supports PostgreSQL and SQLite only")

    for name, create_sql in _CREATE_SQL.items():
        state = _index_state(bind, name)
        if state is not None and state.get("is_valid") is True:
            _assert_expected_state(name, state, healthy=True)
            continue
        if state is not None:
            _assert_expected_state(name, state, healthy=False)
        with op.get_context().autocommit_block():
            if state is not None:
                _assert_expected_state(
                    name,
                    _index_state(op.get_bind(), name),
                    healthy=False,
                )
                op.execute(f"DROP INDEX CONCURRENTLY public.{name}")
            op.execute(create_sql)
        _assert_expected_state(name, _index_state(op.get_bind(), name), healthy=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.drop_index(_ATOMIC_REPLY_INDEX, table_name="projectmessage")
        op.drop_index(_RECOVERY_INDEX, table_name="projectmessage")
        return
    if bind.dialect.name != "postgresql":
        raise RuntimeError("0767 supports PostgreSQL and SQLite only")
    names = (_ATOMIC_REPLY_INDEX, _RECOVERY_INDEX)
    for name in names:
        state = _index_state(bind, name)
        if state is not None:
            _assert_expected_state(
                name,
                state,
                healthy=state.get("is_valid") is True,
            )
    with op.get_context().autocommit_block():
        for name in names:
            state = _index_state(op.get_bind(), name)
            if state is None:
                continue
            _assert_expected_state(
                name,
                state,
                healthy=state.get("is_valid") is True,
            )
            op.execute(f"DROP INDEX CONCURRENTLY public.{name}")

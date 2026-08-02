"""Repair the invalid direction staged-status partial index.

Revision ID: 0720
Revises: 0719
Create Date: 2026-08-01

Migration 0378 used ``CREATE INDEX CONCURRENTLY IF NOT EXISTS``.  An
interrupted concurrent build can leave an INVALID same-name index; a retry then
accepts that husk and advances Alembic even though PostgreSQL will not use or
dump the index.  Production diagnostics confirmed precisely that state for
``ix_direction_status_staged``.

This index-only repair adds no ``nullable=False`` column, so a
``server_default`` is not applicable.
"""

from __future__ import annotations

from typing import Any, Mapping

from alembic import op
import sqlalchemy as sa


revision = "0720"
down_revision = "0719"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_direction_status_staged"
_EXPECTED_DEFINITION = (
    "CREATE INDEX ix_direction_status_staged ON public.direction "
    "USING btree (status) WHERE ((status)::text = 'staged'::text)"
)
_EXPECTED_PREDICATE = "((status)::text = 'staged'::text)"

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
      key_attribute.attname AS key_column,
      pg_get_indexdef(index_rel.oid, 0, false) AS definition,
      pg_get_expr(index_state.indpred, index_state.indrelid, false) AS predicate
    FROM pg_class AS index_rel
    JOIN pg_namespace AS index_ns ON index_ns.oid = index_rel.relnamespace
    JOIN pg_index AS index_state ON index_state.indexrelid = index_rel.oid
    JOIN pg_class AS table_rel ON table_rel.oid = index_state.indrelid
    JOIN pg_namespace AS table_ns ON table_ns.oid = table_rel.relnamespace
    JOIN pg_am AS access_method ON access_method.oid = index_rel.relam
    LEFT JOIN pg_attribute AS key_attribute
      ON key_attribute.attrelid = table_rel.oid
     AND key_attribute.attnum = index_state.indkey[0]
    WHERE index_ns.nspname = 'public'
      AND index_rel.relname = :index_name
    """
)


def _index_state(bind: Any) -> Mapping[str, Any] | None:
    return (
        bind.execute(_INDEX_STATE_SQL, {"index_name": _INDEX_NAME})
        .mappings()
        .one_or_none()
    )


def _assert_expected_healthy(state: Mapping[str, Any] | None) -> None:
    expected = {
        "index_schema": "public",
        "table_schema": "public",
        "table_name": "direction",
        "access_method": "btree",
        "is_unique": False,
        "is_primary": False,
        "is_valid": True,
        "is_ready": True,
        "is_live": True,
        "key_attribute_count": 1,
        "total_attribute_count": 1,
        "key_column": "status",
        "definition": _EXPECTED_DEFINITION,
        "predicate": _EXPECTED_PREDICATE,
    }
    if state is None or any(
        state.get(key) != value for key, value in expected.items()
    ):
        raise RuntimeError("0720 direction staged-status index verification failed")


def _assert_expected_repairable(state: Mapping[str, Any] | None) -> None:
    expected = {
        "index_schema": "public",
        "table_schema": "public",
        "table_name": "direction",
        "access_method": "btree",
        "is_unique": False,
        "is_primary": False,
        "is_valid": False,
        "is_live": True,
        "key_attribute_count": 1,
        "total_attribute_count": 1,
        "key_column": "status",
        "definition": _EXPECTED_DEFINITION,
        "predicate": _EXPECTED_PREDICATE,
    }
    if state is None or any(
        state.get(key) != value for key, value in expected.items()
    ):
        raise RuntimeError("0720 refuses to replace an unexpected invalid index")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # Migration 0378 already creates the ordinary SQLite index.  SQLite has
        # no concurrent-build invalid-husk state to repair.
        return
    if bind.dialect.name != "postgresql":
        raise RuntimeError("0720 supports PostgreSQL and SQLite only")

    state = _index_state(bind)
    if state is not None and state.get("is_valid") is True:
        _assert_expected_healthy(state)
        return
    if state is not None:
        _assert_expected_repairable(state)

    with op.get_context().autocommit_block():
        if state is not None:
            # Re-read immediately before destructive DDL so a disappeared or
            # replaced same-name index fails closed instead of being dropped.
            _assert_expected_repairable(_index_state(op.get_bind()))
            op.execute(f"DROP INDEX CONCURRENTLY public.{_INDEX_NAME}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY {_INDEX_NAME} "
            "ON public.direction (status) WHERE status = 'staged'"
        )

    _assert_expected_healthy(_index_state(op.get_bind()))


def downgrade() -> None:
    raise NotImplementedError(
        "0720 cannot safely restore an invalid or missing production index"
    )

"""Tests for reflect_schema_drift — the deployability-smoketest core that catches
the live-DB-vs-code drift class CI can't (prod incident 2026-06-04).

Isolated: builds throwaway in-memory SQLite schemas with a synthetic MetaData, so
the reflect+compare logic is exercised without the shared harness DB or the full
SQLModel.metadata.
"""

import pytest
import sqlalchemy as sa

from app.deploy_smoketest import reflect_schema_drift


def _meta_with_task() -> sa.MetaData:
    meta = sa.MetaData()
    sa.Table(
        "task",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("status", sa.String),
        sa.Column("blocker_kind", sa.String),
    )
    sa.Table(
        "project",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("merge_gate_runtime", sa.JSON),
    )
    return meta


def test_no_drift_when_db_matches_models():
    meta = _meta_with_task()
    engine = sa.create_engine("sqlite://")
    meta.create_all(engine)  # DB exactly matches the models
    with engine.connect() as conn:
        assert reflect_schema_drift(conn, metadata=meta) == []


def test_missing_column_is_flagged():
    """The exact incident shape: a column the code expects, absent from the DB."""
    meta = _meta_with_task()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        # 'task' exists but WITHOUT blocker_kind (the renumber-skip prod state).
        conn.execute(sa.text("CREATE TABLE task (id INTEGER PRIMARY KEY, status VARCHAR)"))
        conn.execute(
            sa.text("CREATE TABLE project (id INTEGER PRIMARY KEY, merge_gate_runtime JSON)")
        )
    with engine.connect() as conn:
        drift = reflect_schema_drift(conn, metadata=meta)
    assert drift == ["column 'task.blocker_kind' is missing from the database"]


def test_missing_table_is_flagged():
    meta = _meta_with_task()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE task (id INTEGER PRIMARY KEY, status VARCHAR, "
                "blocker_kind VARCHAR)"
            )
        )
        # 'project' table omitted entirely.
    with engine.connect() as conn:
        drift = reflect_schema_drift(conn, metadata=meta)
    assert drift == ["table 'project' is missing from the database"]


def test_extra_db_columns_are_ignored():
    """An additive migration ahead of the code (extra column/table) must NOT be
    flagged — old code that never references it still runs fine."""
    meta = _meta_with_task()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE task (id INTEGER PRIMARY KEY, status VARCHAR, "
                "blocker_kind VARCHAR, future_col VARCHAR)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE project (id INTEGER PRIMARY KEY, "
                "merge_gate_runtime JSON, another_future JSON)"
            )
        )
        conn.execute(sa.text("CREATE TABLE future_table (id INTEGER PRIMARY KEY)"))
    with engine.connect() as conn:
        assert reflect_schema_drift(conn, metadata=meta) == []


def test_tables_filter_restricts_scope():
    meta = _meta_with_task()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE task (id INTEGER PRIMARY KEY, status VARCHAR)"))
        # project omitted, but we only check 'task'.
    with engine.connect() as conn:
        drift = reflect_schema_drift(conn, metadata=meta, tables=["task"])
    assert drift == ["column 'task.blocker_kind' is missing from the database"]


@pytest.mark.asyncio
async def test_no_schema_drift_against_migrated_head(db_session):
    """The real models must match the migrated schema (zero drift). This is the
    safety invariant that lets the boot-time `_verify_schema_drift` be a FATAL
    check: it proves there are no benign model-vs-schema mismatches that would
    false-positive and wedge every deploy, AND it guards future model changes
    from silently introducing drift. Meaningful on Postgres (schema built by
    alembic, so it can diverge from the models); the SQLite fixture builds from
    `metadata.create_all`, so it's trivially clean there."""
    connection = await db_session.connection()
    drift = await connection.run_sync(reflect_schema_drift)
    assert drift == [], "models drifted from the migrated schema:\n  " + "\n  ".join(
        drift
    )

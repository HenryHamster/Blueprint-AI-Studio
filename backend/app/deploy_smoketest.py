"""Deployability smoketest primitives — does the code actually run against this
database?

The redesigned smoketest (the old one was disabled because it just re-ran CI)
verifies a candidate is *deployable*, the one class CI structurally cannot catch:
CI builds a fresh DB from the candidate's own migrations, so it can never see a
drift between what the code expects and what the LIVE database actually has —
exactly the back-merge renumber that took prod down on 2026-06-04 (the live DB's
``alembic_version`` was ahead of its real schema, so a column the code queried
did not exist).

This module is the reusable core. :func:`reflect_schema_drift` is pure reflection
+ comparison (no I/O beyond the passed connection), so it is testable in
isolation and reusable by both the pre-promotion scratch-DB gate (run the
candidate's migrations against a prod-schema-seeded DB, then assert no drift) and
a future boot-time / periodic assertion. The scratch-DB orchestration that seeds
from prod and runs the candidate's migrations layers on top of this.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlmodel import SQLModel


def reflect_schema_drift(
    connection: sa.engine.Connection,
    metadata: sa.MetaData | None = None,
    tables: list[str] | None = None,
) -> list[str]:
    """Tables/columns the models (the code) require that the database lacks.

    Reflects the live schema from *connection* and compares it to *metadata*
    (the application's ``SQLModel.metadata`` by default). Returns sorted,
    human-readable strings naming each missing table or column; an empty list
    means the database satisfies everything the code will query.

    Directional ON PURPOSE: it flags only objects the code expects but the DB is
    MISSING — the class that boot-crashes the app (``UndefinedColumnError`` /
    ``relation does not exist``). EXTRA database objects are ignored: an additive
    migration that ran ahead of the code, or a column a newer migration added,
    never breaks code that doesn't reference it. So this is the right asymmetry
    for "is the deployed code safe against this schema?".

    *tables* optionally restricts the check to a subset (by table name) — used by
    focused tests; production callers pass ``None`` to check the whole model set.
    Dialect-agnostic (uses the SQLAlchemy inspector), so it runs the same against
    Postgres (prod / scratch) and SQLite (tests).
    """
    meta = metadata if metadata is not None else SQLModel.metadata
    inspector = sa.inspect(connection)
    db_tables = set(inspector.get_table_names())
    names = tables if tables is not None else list(meta.tables)

    drift: list[str] = []
    for table_name in names:
        table = meta.tables.get(table_name)
        if table is None:
            # Name not in the model metadata — nothing the code requires.
            continue
        if table_name not in db_tables:
            drift.append(f"table '{table_name}' is missing from the database")
            continue
        db_columns = {c["name"] for c in inspector.get_columns(table_name)}
        drift.extend(
            f"column '{table_name}.{column.name}' is missing from the database"
            for column in table.columns
            if column.name not in db_columns
        )
    return sorted(drift)

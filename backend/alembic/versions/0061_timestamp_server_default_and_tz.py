"""Standardize created_at / updated_at to TIMESTAMPTZ with server_default now().

Audit 2026-04-29 (DB comprehensive remediation, Phase D / D2 — findings
H-S4 + H-S5): historical Blueprint datetime columns were declared as
SQLAlchemy ``DateTime()`` (Postgres ``TIMESTAMP WITHOUT TIME ZONE``)
with Python-side ``default_factory=_utcnow`` and no ``server_default``.
That has two known failure modes:

- **H-S4**: any raw-SQL INSERT path (admin tooling, future migrations,
  ``COPY FROM ...``) silently writes ``NULL`` because there's no
  server-side default. The Python-only default fires only when a row is
  constructed via the ORM. ``project_message`` / ``allow_listed_user``
  test paths have hit this; production pgaudit-driven backfills would
  trip it harder.
- **H-S5**: naive ``TIMESTAMP`` columns mix unsafely with any code that
  hands the driver a timezone-aware ``datetime`` (``datetime.now(timezone.utc)``).
  asyncpg encodes those into ``TIMESTAMPTZ`` literals which then crash at
  bind-time on ``TIMESTAMP``-without-tz columns. The opposite mix —
  naive datetime into ``TIMESTAMPTZ`` — is safe with ``timezone='UTC'``
  set on the asyncpg session (see ``app.database._make_engine``), so
  migrating columns to ``TIMESTAMPTZ`` is the strictly safer direction.

The umbrella migration plan lives in memory ``project_timestamptz_migration.md``
and is intentionally batched table-by-table to keep blast radius small.
This wave covers the five most-touched ``created_at`` / ``updated_at``
columns:

- ``task.created_at`` (no prior server_default; updated_at already has
  one from migration 0033, this revision just adds timezone awareness).
- ``task.updated_at`` (timezone awareness only — server_default from 0033).
- ``project.created_at``.
- ``alloweduser.created_at``.
- ``projectmessage.created_at``.
- ``alert.created_at``.

``Project`` does not declare ``updated_at`` so it is not in scope here;
adding new columns is out of scope for this type-conversion migration.
Future waves will pick up the remaining timestamp columns
(``maxplanaccount.created_at/updated_at``, ``residualremediationcase.*``,
``strategiccycle.*``, ``deploymentstate.deployed_at``, etc.).

Conversion semantics: ``ALTER COLUMN ... TYPE TIMESTAMPTZ USING <col> AT
TIME ZONE 'UTC'`` reads the existing naive timestamp as if it were
already in UTC (which it is — ``_utcnow`` produces naive UTC and the
asyncpg session is pinned to ``timezone='UTC'``). No data migration
beyond the type cast is required.

SQLite fallback: SQLite has no native ``TIMESTAMPTZ`` and the test path
relies on ``SQLModel.metadata.create_all`` reflecting the model
declarations rather than running this migration. Mirroring the 0056
pattern, this migration is a no-op on non-Postgres dialects.

Revision ID: 0061
Revises: 0060
Create Date: 2026-04-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0061'
down_revision = '0060'
branch_labels = None
depends_on = None


# (table, column, had_server_default_before, existing_nullable)
# had_server_default_before — only ``task.updated_at`` was declared with
# ``server_default=now()`` (migration 0033). Every other entry in this
# wave is gaining a server_default for the first time. The downgrade
# path uses this flag to decide whether to drop or preserve the default
# when reverting the column to naive ``TIMESTAMP``.
_COLUMNS = [
    ("task",           "created_at", False, False),
    ("task",           "updated_at", True,  False),
    ("project",        "created_at", False, False),
    ("alloweduser",    "created_at", False, False),
    ("projectmessage", "created_at", False, False),
    ("alert",          "created_at", False, False),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        # SQLite (test path) reflects models.py via SQLModel.metadata.create_all;
        # ALTER COLUMN TYPE is not generally supported on SQLite anyway.
        return

    for table, column, _had_default, existing_nullable in _COLUMNS:
        # Postgres needs an explicit USING clause to convert naive
        # TIMESTAMP → TIMESTAMPTZ. Treating the existing naive value as
        # already-UTC matches the runtime contract (_utcnow + asyncpg
        # session timezone='UTC').
        op.alter_column(
            table,
            column,
            type_=sa.DateTime(timezone=True),
            existing_type=sa.DateTime(),
            server_default=sa.text("now()"),
            existing_nullable=existing_nullable,
            postgresql_using=f"{column} AT TIME ZONE 'UTC'",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    for table, column, had_default, existing_nullable in _COLUMNS:
        # Reverse: TIMESTAMPTZ → naive TIMESTAMP. Convert from UTC back
        # to a naive value so existing rows remain readable by code
        # paths that haven't been redeployed yet.
        kwargs = {
            "type_": sa.DateTime(),
            "existing_type": sa.DateTime(timezone=True),
            "existing_nullable": existing_nullable,
            "postgresql_using": f"{column} AT TIME ZONE 'UTC'",
        }
        if had_default:
            # Preserve the pre-existing server_default (task.updated_at
            # from migration 0033) so this downgrade leaves the column
            # in its pre-0061 shape, not a different shape that strips
            # the older default.
            kwargs["server_default"] = sa.text("now()")
        else:
            # Drop the server_default we added in upgrade().
            kwargs["server_default"] = None
        op.alter_column(table, column, **kwargs)

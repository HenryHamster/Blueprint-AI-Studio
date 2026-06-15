"""Add CHECK constraint capping Task.context serialized size.

Defense-in-depth backstop for task.context JSONB fat-row growth.

task.context has no server-side size guard despite being GIN-indexed (making
fat-row writes doubly expensive: TOAST rewrite plus GIN reindex) and being
written from many scattered code paths with no single chokepoint. A Postgres
CHECK constraint at 2 MB provides defense in depth: it sits well above the
largest legitimate context a healthy task produces (typically a few hundred KB
of tool traces, attempt history, and banked metrics), while a multi-MB runaway
(the historical failure mode that saturated Postgres) is rejected at
INSERT/UPDATE time with ``CheckViolation`` rather than silently propagating.

The constraint is added as NOT VALID so it governs only new writes without
scanning existing rows — this avoids a migration failure when pre-existing rows
already exceed the threshold (as they may in a post-outage recovery state).

The constraint uses ``octet_length(context::text)`` which measures the byte
length of the JSONB value's text serialization, matching the Python-side
measurement (``len(json.dumps(...).encode())``) within a few percent.

Mirrors the payloads CHECK constraint added in migration 0065.

Revision ID: 0277
Revises: 0275
Create Date: 2026-06-15 00:00:00.000000
"""

from alembic import op

revision = "0277"
down_revision = "0275"
branch_labels = None
depends_on = None

# 2 MB — generous above any legitimate context, blocks multi-MB runaways.
_CONTEXT_SIZE_LIMIT = 2097152


def upgrade() -> None:
    # Postgres-only — the constraint expression uses octet_length(jsonb::text)
    # which is Postgres-specific. SQLite test paths skip the constraint.
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # NOT VALID: enforces the limit on new writes only; does not scan existing
    # rows. Avoids migration failure when pre-existing rows already exceed the
    # threshold (e.g. post-outage recovery state).
    op.execute(
        f"ALTER TABLE task ADD CONSTRAINT chk_task_context_size "
        f"CHECK (octet_length(context::text) < {_CONTEXT_SIZE_LIMIT}) NOT VALID"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint("chk_task_context_size", "task", type_="check")

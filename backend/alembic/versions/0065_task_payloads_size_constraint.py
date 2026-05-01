"""Add CHECK constraint capping Task.payloads serialized size.

Audit 2026-04-29 (DB comprehensive remediation, Phase D4 / finding M-S5):
``Task.payloads`` is unbounded JSONB. The Python layer caps strategic-spawn
payloads at 120 KB via ``pack_under_budget`` (see memory
``reference_strategic_payload_bloat.md``: a single 256 KB+ payload silently
killed all strategic autonomy until 7adceb15 introduced the cap), but
nothing at the database level prevented a future code path from inserting
or updating a Task with a runaway payload. A CHECK constraint at 200 KB
provides defense in depth: it sits well above the 120 KB Python cap so
legitimate near-cap payloads do not bounce, while a 256 KB+ runaway
(the historical failure mode) is rejected at INSERT/UPDATE time with
``CheckViolation`` rather than silently propagating.

The constraint uses ``octet_length(payloads::text)`` which measures the
byte length of the JSONB value's text serialization. This matches what
``pack_under_budget`` measures Python-side (``len(json.dumps(...).encode())``)
modulo whitespace — Postgres' JSONB::text emits a canonical compact form,
so 200 KB DB-side ≈ 200 KB Python-side within a few percent.

Revision ID: 0063
Revises: 0062
Create Date: 2026-04-29 00:00:00.000000

"""
from alembic import op


revision = '0063'
down_revision = '0062'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres-only — the constraint expression uses octet_length(jsonb::text)
    # which is Postgres-specific. SQLite test paths skip the constraint
    # (the Python layer's pack_under_budget cap already runs there).
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.create_check_constraint(
        "chk_task_payloads_size",
        "task",
        "octet_length(payloads::text) < 200000",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint("chk_task_payloads_size", "task", type_="check")

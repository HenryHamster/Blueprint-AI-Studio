"""Bound TEXT columns used as PKs or B-tree-indexed identifiers.

Audit 2026-04-29 (DB comprehensive remediation, Phase D / D3 — findings
M-S6 + M-S7): three String columns were declared without ``max_length``
and either drive a B-tree index or serve as a primary key:

- ``residualremediationcase.fingerprint`` — indexed via ``index=True``
  (alembic 0051) and used in the ``ux_residualremediationcase_project_fingerprint``
  unique constraint. Postgres B-tree refuses entries whose key exceeds
  ~2 KB ("index row size N exceeds maximum 2712 for index ..."), and
  unbounded TEXT lets a pathological fingerprint trip that error at
  insertion time. Tightened to VARCHAR(512), which is comfortably under
  the limit while leaving headroom over the 64-character SHA-256 hex
  digests fingerprints actually use today.
- ``maxplanaccount.name`` — short human label set by admins via the
  pool-accounts UI. Tightened to VARCHAR(255).
- ``orchestratorerrorack.key`` — primary key, format
  ``"<exception_class>|<source>"`` or the literal ``"*"`` global ack.
  Tightened to VARCHAR(255).

Pre-flight assumption: existing values fit comfortably under the new
caps. ``fingerprint`` is a SHA-256 hex digest in current usage (64
chars); ``name`` is admin-typed short text; ``key`` is
``ExceptionClass|<file_path:line>``-shaped, deepest case ~120 chars.
No truncation step is included — if a prod row violates the new bound
the ALTER will fail loudly, which is the correct behaviour: that means
something is generating overly-long identifiers and we want to know.

Revision ID: 0062
Revises: 0061
Create Date: 2026-04-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0062'
down_revision = '0061'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "residualremediationcase",
        "fingerprint",
        type_=sa.String(512),
        existing_type=sa.String(),
        existing_nullable=False,
    )
    op.alter_column(
        "maxplanaccount",
        "name",
        type_=sa.String(255),
        existing_type=sa.String(),
        existing_nullable=False,
    )
    op.alter_column(
        "orchestratorerrorack",
        "key",
        type_=sa.String(255),
        existing_type=sa.String(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "orchestratorerrorack",
        "key",
        type_=sa.String(),
        existing_type=sa.String(255),
        existing_nullable=False,
    )
    op.alter_column(
        "maxplanaccount",
        "name",
        type_=sa.String(),
        existing_type=sa.String(255),
        existing_nullable=False,
    )
    op.alter_column(
        "residualremediationcase",
        "fingerprint",
        type_=sa.String(),
        existing_type=sa.String(512),
        existing_nullable=False,
    )

"""Advance the acceptance-healing ledger migration to the reserved revision.

The ledger schema was already introduced by the immutable 0744 migration.
This follow-up is intentionally empty: it preserves the applied 0744 identity
while allowing the serving schema expectation to use the reserved 0750
revision without attempting to create the table twice.

Revision ID: 0750
Revises: 0744
Create Date: 2026-08-09
"""

from __future__ import annotations


revision = "0750"
down_revision = "0744"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

"""Reconcile the missing ``computejob`` table onto the live LTS database.

The ``main -> automain`` back-merge renumber collision (see 0144's docstring)
left the live database's ``alembic_version`` pointer already past revision id
``0143`` by the time ``0143`` became ``add_compute_job_persistence``: the
original ``0143`` this database consumed was the vm_contact follow-up (now
renumbered ``0144``). Alembic therefore considered ``add_compute_job_persistence``
applied and skipped it — the v5 deploy's migration job upgraded cleanly to head
while the ``computejob`` table was never created. The startup schema-drift guard
(``app/main.py::_verify_schema_drift``) then correctly refused to boot the v5
code (deploy 2026-06-05, revision 00375-gpt), pinning prod on the previous
revision exactly as designed.

This migration re-applies 0143 IDEMPOTENTLY by importing and replaying its
``upgrade()`` verbatim when — and only when — the ``computejob`` table is
absent: on the live DB it creates the missing table + indexes byte-identically
to what fresh databases got from 0143; on databases where 0143 ran normally it
is a no-op. Replaying by import (rather than mirroring the DDL) keeps 0143 the
single canonical provider for the model-coverage invariants and makes
divergence between the two definitions impossible. Forward-only and additive.

Revision ID: 0151
Revises: 0150
Create Date: 2026-06-05 15:30:00.000000

"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0151"
down_revision = "0150"
branch_labels = None
depends_on = None


def _load_0143():
    """Load the 0143 migration module by file path (digit-prefixed migration
    modules are not importable by name)."""
    path = Path(__file__).with_name("0143_add_compute_job_persistence.py")
    spec = importlib.util.spec_from_file_location("_blueprint_migration_0143", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "computejob" in insp.get_table_names():
        # Healthy / fresh DB: 0143 already created the table — nothing to do.
        return
    # Renumber-skip DB: replay 0143's upgrade verbatim. The global ``op`` proxy
    # inside 0143 binds to THIS migration's active context, so the DDL runs in
    # the normal upgrade transaction.
    _load_0143().upgrade()


def downgrade() -> None:
    # Forward-only reconciliation: it only ensures a table exists that migration
    # 0143 already owns. Dropping it here would wrongly remove 0143's table when
    # downgrading to 0150 (where it must still be present), so there is no
    # correct rollback for this revision.
    raise NotImplementedError(
        "0151 is a forward-only reconciliation; teardown of computejob belongs "
        "to 0143's downgrade on databases where 0143 actually ran."
    )

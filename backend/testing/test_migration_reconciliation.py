"""Tests for the deploy-time Alembic 0143 revision-identity reconciliation.

Exercises ``reconcile_pre_renumber_divergence`` against simulated database
states via a sync SQLite connection (no DB fixture / Docker DB required):

  (1) a pre-renumber ``main`` DB  -> rewinds the version pointer to the parent;
  (2) a fresh/empty DB            -> strict no-op;
  (3) an already-correct DB       -> strict no-op;
  (4) any DB where ``computejob`` already exists -> strict no-op;
  (5) idempotency across repeated runs.
"""

import sqlalchemy as sa

from app.migration_reconciliation import (
    AUTHORIZED_0143_DIVERGENCE,
    reconcile_pre_renumber_divergence,
)


def _connection_at(
    version: str | None,
    *,
    with_alembic_version: bool = True,
    with_computejob: bool = False,
) -> sa.Connection:
    """Build a fresh in-memory SQLite connection seeded to a given state.

    The single connection is the whole DB lifetime (``sqlite://`` is per-conn),
    so DDL/inserts here are what the reconciliation observes.
    """
    engine = sa.create_engine("sqlite://")
    conn = engine.connect()
    if with_alembic_version:
        conn.execute(
            sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        if version is not None:
            conn.execute(
                sa.text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
                {"v": version},
            )
    if with_computejob:
        conn.execute(sa.text("CREATE TABLE computejob (id VARCHAR)"))
    return conn


def _current_version(conn: sa.Connection) -> str | None:
    return conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()


def test_rewinds_pre_renumber_main_db():
    # Stamped at the divergent 0143 (deployed vm_contact identity), computejob absent.
    conn = _connection_at(AUTHORIZED_0143_DIVERGENCE.revision)
    outcome = reconcile_pre_renumber_divergence(conn)
    assert outcome.rewound is True
    assert _current_version(conn) == AUTHORIZED_0143_DIVERGENCE.parent_revision


def test_noop_on_fresh_db_without_alembic_version():
    conn = _connection_at(None, with_alembic_version=False)
    outcome = reconcile_pre_renumber_divergence(conn)
    assert outcome.rewound is False
    assert not sa.inspect(conn).has_table("alembic_version")


def test_noop_when_alembic_version_empty():
    conn = _connection_at(None)  # table exists, no stamped row
    outcome = reconcile_pre_renumber_divergence(conn)
    assert outcome.rewound is False
    assert _current_version(conn) is None


def test_noop_on_already_correct_head_db():
    conn = _connection_at("0150")
    outcome = reconcile_pre_renumber_divergence(conn)
    assert outcome.rewound is False
    assert _current_version(conn) == "0150"


def test_noop_when_computejob_already_present():
    # Stamped at 0143 but the table exists (already-reconciled / integration DB).
    conn = _connection_at(
        AUTHORIZED_0143_DIVERGENCE.revision, with_computejob=True
    )
    outcome = reconcile_pre_renumber_divergence(conn)
    assert outcome.rewound is False
    assert _current_version(conn) == AUTHORIZED_0143_DIVERGENCE.revision


def test_idempotent_across_repeated_runs():
    conn = _connection_at(AUTHORIZED_0143_DIVERGENCE.revision)
    first = reconcile_pre_renumber_divergence(conn)
    assert first.rewound is True
    # A second run now sees the parent revision -> strict no-op, no further rewind.
    second = reconcile_pre_renumber_divergence(conn)
    assert second.rewound is False
    assert _current_version(conn) == AUTHORIZED_0143_DIVERGENCE.parent_revision


def test_on_disk_revision_matches_authorized_integration_identity():
    # Guards the signature's on-disk check: the repo's 0143 file IS the
    # compute-job migration named by the registry.
    from app.migration_reconciliation import _VERSIONS_DIR

    expected = (
        _VERSIONS_DIR
        / f"{AUTHORIZED_0143_DIVERGENCE.revision}"
        f"_{AUTHORIZED_0143_DIVERGENCE.integration_identity}.py"
    )
    assert expected.is_file()

"""Tests for the pre-promotion scratch-DB migration-apply gate.

The gate reproduces what Alembic would actually do on the LIVE database and
catches the deploy-fatal renumber-skip class (prod incident 2026-06-04) BEFORE
promotion — the class CI structurally cannot see because CI builds a fresh DB
from the candidate's own migrations.

The end-to-end cases (block / clean / seed) require a real migration-applying
Postgres path — the renumber-skip is only meaningful when Alembic genuinely
skips an already-stamped revision against a seeded scratch DB. They are SKIPPED
on the local SQLite harness, mirroring
``test_deploy_smoketest.test_no_schema_drift_against_migrated_head``. The
fail-safe / determinism cases that need no scratch DB run everywhere.
"""

import os

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.migration_apply_gate import (
    ephemeral_postgres_database,
    run_migration_apply_gate,
)

# Same Postgres-vs-SQLite selection conftest uses. The scratch-DB cases need a
# server we can CREATE DATABASE on (CI's `test` user is a superuser).
USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")

requires_postgres = pytest.mark.skipif(
    not USE_POSTGRES,
    reason="scratch-DB migration-apply gate requires a real Postgres server "
    "(USE_TEST_POSTGRES=true); the renumber-skip is not reproducible on SQLite",
)


def _base_url():
    return make_url(os.environ["DATABASE_URL"])


# --- Synthetic candidate migration chains ----------------------------------
# A minimal three-revision chain. ``a02`` creates the ``computejob`` table — the
# stand-in for the table the real renumber-skip dropped. ``a01`` seeds a
# pre-existing table; ``a03`` is an unrelated later migration.

_MIG_A01 = """
from alembic import op
import sqlalchemy as sa

revision = "a01"
down_revision = None


def upgrade():
    op.create_table("widget", sa.Column("id", sa.Integer, primary_key=True))


def downgrade():
    op.drop_table("widget")
"""

_MIG_A02_COMPUTEJOB = """
from alembic import op
import sqlalchemy as sa

revision = "a02"
down_revision = "a01"


def upgrade():
    op.create_table("computejob", sa.Column("id", sa.Integer, primary_key=True))


def downgrade():
    op.drop_table("computejob")
"""

_MIG_A03 = """
from alembic import op
import sqlalchemy as sa

revision = "a03"
down_revision = "a02"


def upgrade():
    op.create_table("audit_entry", sa.Column("id", sa.Integer, primary_key=True))


def downgrade():
    op.drop_table("audit_entry")
"""

_CANDIDATE_SOURCES = {
    "a01_widget.py": _MIG_A01,
    "a02_computejob.py": _MIG_A02_COMPUTEJOB,
    "a03_audit.py": _MIG_A03,
}


def _required_metadata():
    """What the candidate's code requires: every table the chain should create."""
    meta = sa.MetaData()
    for name in ("widget", "computejob", "audit_entry"):
        sa.Table(name, meta, sa.Column("id", sa.Integer, primary_key=True))
    return meta


async def _seed_prod_sim(prod_engine, tables: dict[str, bool], revision: str | None):
    """Stand up a prod-sim DB: create each table in *tables* whose value is True,
    and stamp ``alembic_version`` to *revision* (omit the row when None)."""

    def _seed(sync_conn):
        for name, present in tables.items():
            if present:
                sync_conn.execute(
                    sa.text(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)")
                )
        sync_conn.execute(
            sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        if revision is not None:
            sync_conn.execute(
                sa.text("INSERT INTO alembic_version (version_num) VALUES (:r)"),
                {"r": revision},
            )

    async with prod_engine.begin() as conn:
        await conn.run_sync(_seed)


# --- End-to-end (Postgres) -------------------------------------------------


@requires_postgres
@pytest.mark.asyncio
async def test_renumber_skip_is_blocked_naming_missing_table():
    """REGRESSION (v5 computejob renumber-skip): prod is stamped at ``a02`` but
    the ``a02`` body was skipped, so ``computejob`` is ABSENT. Applying the
    candidate chain re-runs only ``a03`` (Alembic sees ``a02`` already applied
    and skips it), so ``computejob`` is STILL never created — the gate must
    ``block`` and name the missing ``computejob`` table.

    This is the exact mechanic of the 2026-06-04 incident: the live DB stamped
    ahead of its real schema, a required table silently never created."""
    async with ephemeral_postgres_database(_base_url()) as (prod_engine, _name):
        # Renumber-skip live state: widget present, computejob ABSENT, stamped a02.
        await _seed_prod_sim(
            prod_engine, {"widget": True, "computejob": False}, revision="a02"
        )
        verdict, detail = await run_migration_apply_gate(
            prod_engine=prod_engine,
            candidate_sources=_CANDIDATE_SOURCES,
            required_metadata=_required_metadata(),
        )

    assert verdict == "block", detail
    assert "computejob" in detail
    assert "a02" in detail


@requires_postgres
@pytest.mark.asyncio
async def test_clean_candidate_is_ok():
    """A healthy live DB stamped at ``a01`` (computejob legitimately not yet
    created) — applying the candidate chain runs ``a02`` (creates computejob)
    and ``a03``, so every required table is present and the gate returns ``ok``."""
    async with ephemeral_postgres_database(_base_url()) as (prod_engine, _name):
        await _seed_prod_sim(
            prod_engine, {"widget": True, "computejob": False}, revision="a01"
        )
        verdict, detail = await run_migration_apply_gate(
            prod_engine=prod_engine,
            candidate_sources=_CANDIDATE_SOURCES,
            required_metadata=_required_metadata(),
        )

    assert verdict == "ok", detail
    assert detail is None


@requires_postgres
@pytest.mark.asyncio
async def test_indeterminate_prod_state_defers():
    """Production has no recorded Alembic revision (empty ``alembic_version``) —
    the gate cannot reproduce the deploy, so it HOLDS (defer), never ``ok``."""
    async with ephemeral_postgres_database(_base_url()) as (prod_engine, _name):
        await _seed_prod_sim(prod_engine, {"widget": True}, revision=None)
        verdict, detail = await run_migration_apply_gate(
            prod_engine=prod_engine,
            candidate_sources=_CANDIDATE_SOURCES,
            required_metadata=_required_metadata(),
        )

    assert verdict == "defer", detail
    assert "alembic_version" in detail or "recorded" in detail


@requires_postgres
@pytest.mark.asyncio
async def test_scratch_db_is_isolated_from_prod(monkeypatch):
    """ISOLATION SAFETY: the gate must never apply DDL to production. After a full
    run, the prod-sim DB still has ONLY its seeded tables — no candidate table
    (computejob / audit_entry) leaked into it."""
    async with ephemeral_postgres_database(_base_url()) as (prod_engine, _name):
        await _seed_prod_sim(
            prod_engine, {"widget": True, "computejob": False}, revision="a01"
        )
        await run_migration_apply_gate(
            prod_engine=prod_engine,
            candidate_sources=_CANDIDATE_SOURCES,
            required_metadata=_required_metadata(),
        )

        def _tables(sync_conn):
            return set(sa.inspect(sync_conn).get_table_names())

        async with prod_engine.connect() as conn:
            tables = await conn.run_sync(_tables)

    # The candidate chain ran ONLY on the scratch DB; prod-sim is untouched.
    assert "audit_entry" not in tables
    assert "computejob" not in tables
    assert "widget" in tables


@requires_postgres
@pytest.mark.asyncio
async def test_fetch_failure_defers(monkeypatch):
    """A GitHub fetch failure for the candidate's migration list must HOLD
    (defer), never wave through — a fetch error is not 'no migrations'."""
    import app.migration_apply_gate as gate_mod

    async def _boom(*_a, **_k):
        raise RuntimeError("GitHub unreachable")

    monkeypatch.setattr(gate_mod.github, "list_versions_at_ref", _boom)
    async with ephemeral_postgres_database(_base_url()) as (prod_engine, _name):
        await _seed_prod_sim(prod_engine, {"widget": True}, revision="a01")
        verdict, detail = await run_migration_apply_gate(
            prod_engine=prod_engine,
            gh_token="t",
            owner="o",
            repo="r",
            candidate_ref="deadbeef",
            required_metadata=_required_metadata(),
        )

    assert verdict == "defer", detail
    assert "deadbeef" in detail


# --- Fail-safe cases that need no scratch DB (run everywhere) ---------------


@pytest.mark.asyncio
async def test_non_postgres_bind_defers():
    """A non-Postgres production bind cannot reproduce the deploy; the gate HOLDS
    (defer) rather than waving the release through. Runs on any harness."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            candidate_sources=_CANDIDATE_SOURCES,
            required_metadata=_required_metadata(),
        )
    finally:
        await engine.dispose()

    assert verdict == "defer"
    assert "PostgreSQL" in detail


@pytest.mark.asyncio
async def test_missing_github_coords_defers():
    """No injected sources AND incomplete GitHub coordinates => defer (never run
    a half-configured gate to a wave-through)."""
    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:5432/nope")
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            gh_token="t",
            owner="o",
            # repo/candidate_ref missing
            required_metadata=_required_metadata(),
        )
    finally:
        await engine.dispose()

    # Prod-state read fails to connect (no such DB) OR coords are incomplete —
    # either way the verdict must be a HOLD, never 'ok'/'block'.
    assert verdict == "defer", detail

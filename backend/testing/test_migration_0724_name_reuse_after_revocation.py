"""Regression tests for migration 0724: name reuse after revocation.

The migration replaces the full UNIQUE constraint on ServiceCredential.name
(added by 0723) with a partial unique index WHERE revoked_at IS NULL. This
allows a revoked credential name to be re-minted while still blocking two
active credentials from sharing a name.

These tests exercise the real upgrade path (drop_constraint + partial
create_index) against a PostgreSQL database to validate that:
  (a) Two live credentials cannot share a name.
  (b) Revoking one and re-minting the same name succeeds (not a 409).

The standalone tests (no Postgres required) verify the migration's structure
and revision chain.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from app.migration_apply_gate import ephemeral_postgres_database

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_LOCATION = _BACKEND_ROOT / "alembic"
_USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")
requires_postgres = pytest.mark.skipif(
    not _USE_POSTGRES,
    reason="requires the real PostgreSQL migration harness",
)

_INSERT_CRED_SQL = sa.text(
    "INSERT INTO servicecredential "
    "(id, name, token_hash, token_prefix, principal_user_id, scopes, created_by) "
    "VALUES (CAST(:id AS uuid), :name, :token_hash, 'bpsc_ab', 'svc:test', '[]'::jsonb, 'tester')"
)


def _load_migration():
    path = (
        _BACKEND_ROOT
        / "alembic"
        / "versions"
        / "0724_service_credential_name_reuse_after_revocation.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0724_name_reuse", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_to(sync_connection: sa.Connection, destination: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(_SCRIPT_LOCATION))
    script = ScriptDirectory.from_config(config)

    def _revs(current_revision, _context):
        return script._upgrade_revs(destination, current_revision)

    with EnvironmentContext(
        config,
        script,
        fn=_revs,
        as_sql=False,
        destination_rev=destination,
    ) as environment:
        environment.configure(
            connection=sync_connection,
            transaction_per_migration=True,
        )
        with environment.begin_transaction():
            environment.run_migrations()


async def _upgrade(engine, destination: str) -> None:
    async with engine.connect() as conn:
        await conn.run_sync(lambda sync_conn: _upgrade_to(sync_conn, destination))


# ── Standalone structure tests (no Postgres) ────────────────────────────────


def test_0724_revision_chain():
    migration = _load_migration()
    assert migration.revision == "0724"
    assert migration.down_revision == "0723"


def test_0724_upgrade_drops_full_constraint_then_creates_partial_index():
    """upgrade() must replace the full UNIQUE constraint with a partial index."""
    migration = _load_migration()
    ops = []

    class _FakeOp:
        def drop_constraint(self, name, table, **kwargs):
            ops.append(("drop_constraint", name, table))

        def create_index(self, name, table, columns, **kwargs):
            ops.append(("create_index", name, table, tuple(columns)))

    migration.op = _FakeOp()
    migration.upgrade()

    assert ops == [
        ("drop_constraint", "uq_servicecredential_name", "servicecredential"),
        ("create_index", "uq_servicecredential_name_unrevoked", "servicecredential", ("name",)),
    ], f"unexpected op sequence: {ops}"


def test_0724_downgrade_restores_full_unique_constraint():
    """downgrade() must restore the original full UNIQUE constraint."""
    migration = _load_migration()
    ops = []

    class _FakeOp:
        def drop_index(self, name, **kwargs):
            ops.append(("drop_index", name))

        def create_unique_constraint(self, name, table, columns):
            ops.append(("create_unique_constraint", name, table, tuple(columns)))

    migration.op = _FakeOp()
    migration.downgrade()

    assert ops == [
        ("drop_index", "uq_servicecredential_name_unrevoked"),
        ("create_unique_constraint", "uq_servicecredential_name", "servicecredential", ("name",)),
    ], f"unexpected op sequence: {ops}"


# ── PostgreSQL integration tests ─────────────────────────────────────────────


@requires_postgres
@pytest.mark.asyncio
async def test_0724_active_duplicate_name_rejected_after_migration():
    """The partial unique index still blocks two active credentials sharing a name."""
    from sqlalchemy.exc import IntegrityError

    cred_id_a = str(uuid.uuid4())
    cred_id_b = str(uuid.uuid4())

    async with ephemeral_postgres_database(make_url(os.environ["DATABASE_URL"])) as (
        engine,
        _db_name,
    ):
        await _upgrade(engine, "0724")

        # Insert the first active credential.
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_CRED_SQL,
                {"id": cred_id_a, "name": "conflict-name", "token_hash": f"hash-{cred_id_a}"},
            )

        # A second active credential with the same name must violate the partial index.
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    _INSERT_CRED_SQL,
                    {"id": cred_id_b, "name": "conflict-name", "token_hash": f"hash-{cred_id_b}"},
                )

        # Exactly one row must survive.
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM servicecredential WHERE name = 'conflict-name'"
                    )
                )
            ).scalar_one()
        assert count == 1, f"expected 1 active credential, got {count}"

        await engine.dispose()


@requires_postgres
@pytest.mark.asyncio
async def test_0724_revoked_name_can_be_reminted():
    """Revoking a credential lets the same name be re-minted (the rotation path)."""
    cred_id_a = str(uuid.uuid4())
    cred_id_b = str(uuid.uuid4())

    async with ephemeral_postgres_database(make_url(os.environ["DATABASE_URL"])) as (
        engine,
        _db_name,
    ):
        await _upgrade(engine, "0724")

        # Insert a credential and immediately revoke it.
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_CRED_SQL,
                {"id": cred_id_a, "name": "rotation-cred", "token_hash": f"hash-{cred_id_a}"},
            )
            await conn.execute(
                sa.text(
                    "UPDATE servicecredential SET revoked_at = NOW() "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": cred_id_a},
            )

        # Re-minting the same name after revocation must succeed — no IntegrityError.
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_CRED_SQL,
                {"id": cred_id_b, "name": "rotation-cred", "token_hash": f"hash-{cred_id_b}"},
            )

        # Both rows must exist: the revoked original and the new active credential.
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.text(
                        "SELECT revoked_at FROM servicecredential "
                        "WHERE name = 'rotation-cred' ORDER BY created_at"
                    )
                )
            ).fetchall()

        assert len(rows) == 2, f"expected 2 rows (revoked + reminted), got {len(rows)}"
        assert rows[0].revoked_at is not None, "first credential must be revoked"
        assert rows[1].revoked_at is None, "reminted credential must be active"

        await engine.dispose()


@requires_postgres
@pytest.mark.asyncio
async def test_0724_multiple_revoked_same_name_all_allowed():
    """Multiple revoked credentials may share a name (only active ones are constrained)."""
    ids = [str(uuid.uuid4()) for _ in range(3)]

    async with ephemeral_postgres_database(make_url(os.environ["DATABASE_URL"])) as (
        engine,
        _db_name,
    ):
        await _upgrade(engine, "0724")

        # Insert and revoke three credentials with the same name in sequence.
        for cred_id in ids:
            async with engine.begin() as conn:
                await conn.execute(
                    _INSERT_CRED_SQL,
                    {"id": cred_id, "name": "multi-revoke", "token_hash": f"hash-{cred_id}"},
                )
                await conn.execute(
                    sa.text(
                        "UPDATE servicecredential SET revoked_at = NOW() "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": cred_id},
                )

        # All three revoked rows plus an active one must coexist.
        active_id = str(uuid.uuid4())
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_CRED_SQL,
                {"id": active_id, "name": "multi-revoke", "token_hash": f"hash-{active_id}"},
            )

        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM servicecredential WHERE name = 'multi-revoke'"
                    )
                )
            ).scalar_one()
        assert count == 4, f"expected 4 rows (3 revoked + 1 active), got {count}"

        await engine.dispose()

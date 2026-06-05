"""Pre-promotion scratch-DB migration-apply gate.

The deterministic Alembic migration-identity check (``rollup_migration_validation.
check_qualification_migration_identity``) compares the candidate branch's
migration FILENAMES against the promotion target's filenames to catch a
revision-id collision. That branch-files-vs-branch-files comparison structurally
cannot see the failure class that took prod down on 2026-06-04: the LIVE database
was stamped at a revision whose body was silently skipped by a back-merge
renumber, so a required table (``computejob``) was never created even though the
chain head advanced and the branch files looked internally consistent. Only the
boot-time schema-drift assertion catches that today — AFTER the candidate is
already promoted and deployed, surfacing as a crash.

This module is the reusable engine that catches it BEFORE promotion. It
reproduces what Alembic would ACTUALLY do on the live database:

  1. Read PRODUCTION'S REAL recorded state — the live ``alembic_version`` stamp
     AND the schema actually present (reflected). The bug is precisely that these
     two disagree (stamped ahead of the real schema), so we need both; rebuilding
     an idealized fresh schema from the candidate's own migrations is exactly what
     CI already does and structurally cannot see this class.
  2. Seed an ISOLATED scratch Postgres database with that state (materialize the
     reflected structure, stamp ``alembic_version`` to prod's recorded revision).
  3. Apply the candidate's migration chain the way the deployed upgrade runs it —
     the :mod:`app.migration_reconciliation` pre-step followed by ``alembic
     upgrade head`` (mirroring ``backend/run_migrations.sh``), so the gate
     composes with the existing post-deploy reconciliation band-aid rather than
     diverging from it.
  4. Assert no drift via :func:`app.deploy_smoketest.reflect_schema_drift` against
     the candidate's required metadata. Any missing table/column => ``block``.

Determinism & fail-safe (matching the migration-identity check's convention):
if production's recorded state cannot be determined, the candidate's migration
files cannot be fetched, or the scratch DB cannot be provisioned/seeded/applied,
the verdict is ``defer`` (HOLD the release) — NEVER ``ok``. A ``defer`` is "I
could not prove this is safe", which must hold the release, never wave it through.

ISOLATION SAFETY: the engine runs inside the production backend and must NEVER
apply migrations or DDL against the real production database. Production is read
ONLY (reflection + ``SELECT version_num``); every DDL / migration application
targets the freshly-provisioned, separately-named scratch database. The orchestrator
asserts the scratch database name differs from production's before any DDL runs.

The engine is self-contained and importable so the release qualifier can wire it
in as a new deterministic dimension (the same way it wired in
``check_qualification_migration_identity``).
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from contextlib import asynccontextmanager
from typing import NamedTuple

import sqlalchemy as sa
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

import app.github as github
from app.deploy_smoketest import reflect_schema_drift
from app.migration_reconciliation import reconcile_pre_renumber_divergence

logger = logging.getLogger(__name__)

# ('ok'|'block'|'defer', detail). Mirrors check_qualification_migration_identity.
Verdict = tuple[str, str | None]

# Repo-relative directory holding the Alembic migration files, used to resolve a
# bare basename (as returned by ``github.list_versions_at_ref``) to a full path
# for the per-file text fetch — same constant the rollup validator uses.
_ALEMBIC_VERSIONS_DIR = "backend/alembic/versions"


class _ProdState(NamedTuple):
    """Production's real recorded state: the ``alembic_version`` stamp plus the
    structure (tables/columns) ACTUALLY present, reflected from the live DB.

    ``metadata`` is sanitized for faithful structural re-creation on the scratch
    DB (see :func:`_sanitize_reflected_metadata`) — only the table/column shape
    matters to the drift check, never indexes/constraints/data."""

    revision: str
    metadata: sa.MetaData


class _MigrationApplyError(Exception):
    """A scratch-side seed/apply step failed. Always collapses to ``defer`` — an
    apply failure means we could not prove the candidate is safe, so the release
    must HOLD (never wave through, never hard-block on a possibly-fidelity issue)."""


def _sanitize_reflected_metadata(metadata: sa.MetaData) -> None:
    """Strip everything but table/column SHAPE from a reflected ``MetaData``.

    The scratch DB only needs prod's table/column STRUCTURE so the candidate
    migrations apply on top and :func:`reflect_schema_drift` can see missing
    tables/columns — the one class this gate exists to catch. Reflected indexes
    (e.g. trigram GIN / partial-unique), foreign-key constraints, and native
    enum types do NOT round-trip cleanly through ``create_all`` and would make
    seeding raise (a safe ``defer``, but a needless one). We drop them so seeding
    reliably materializes the structure:

      * indexes — irrelevant to drift (it checks tables/columns only);
      * foreign-key constraints — irrelevant to drift and a source of
        create-ordering / type-mismatch failures;
      * native ``Enum`` column types — coerced to plain ``String`` so no
        ``CREATE TYPE`` is emitted (the column NAME is all drift cares about).

    Mutates *metadata* in place.
    """
    for table in list(metadata.tables.values()):
        table.indexes.clear()
        for fk in [
            c for c in list(table.constraints) if isinstance(c, sa.ForeignKeyConstraint)
        ]:
            table.constraints.discard(fk)
        for column in table.columns:
            column.foreign_keys.clear()
            if isinstance(column.type, sa.Enum):
                column.type = sa.String(length=255)


async def _read_prod_state(prod_engine: AsyncEngine) -> _ProdState | None:
    """Read production's recorded revision and reflect its actual schema.

    READ-ONLY against production: reflection plus a single ``SELECT version_num``.
    Returns ``None`` (=> ``defer``) when the recorded revision cannot be
    determined (no ``alembic_version`` table, or an empty stamp) — an
    indeterminate prod state must HOLD, never wave through.
    """

    def _read(sync_conn: sa.Connection) -> _ProdState | None:
        inspector = sa.inspect(sync_conn)
        if not inspector.has_table("alembic_version"):
            return None
        row = sync_conn.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).fetchone()
        revision = row[0] if row else None
        if not revision:
            return None
        metadata = sa.MetaData()
        metadata.reflect(bind=sync_conn)
        _sanitize_reflected_metadata(metadata)
        return _ProdState(revision=str(revision), metadata=metadata)

    async with prod_engine.connect() as conn:
        return await conn.run_sync(_read)


async def _create_database(base_url: URL, name: str) -> None:
    """``CREATE DATABASE name`` on *base_url*'s server (AUTOCOMMIT — DDL for a
    new database cannot run inside a transaction). Creating a SEPARATE database
    never touches production's schema."""
    engine = create_async_engine(
        base_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


async def _drop_database(base_url: URL, name: str) -> None:
    """Best-effort teardown of an ephemeral scratch database. Terminates any
    lingering backends first (a still-open connection blocks ``DROP DATABASE``)."""
    engine = create_async_engine(
        base_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
    except Exception as exc:  # noqa: BLE001 — teardown is best-effort
        logger.warning(
            "[MIGRATION_APPLY_GATE] could not drop scratch database %r: %s", name, exc
        )
    finally:
        await engine.dispose()


@asynccontextmanager
async def ephemeral_postgres_database(base_url: URL, *, db_name: str | None = None):
    """Provision a fresh, fully isolated Postgres database for the duration of the
    ``async with`` block, yielding ``(engine, name)`` and dropping it on exit.

    A brand-new database (not a schema, not a transaction) is the cleanest full
    isolation: it gives a fresh ``public`` schema so the candidate's migrations —
    including ``CREATE EXTENSION`` / ``CREATE TYPE`` — run exactly as they would
    on a real deploy target, with zero chance of touching production. Reused by
    both the gate (for its scratch DB) and the tests (to stand up a prod-sim DB).
    """
    name = db_name or f"mig_apply_gate_{uuid.uuid4().hex[:16]}"
    await _create_database(base_url, name)
    engine = create_async_engine(base_url.set(database=name), poolclass=NullPool)
    try:
        yield engine, name
    finally:
        await engine.dispose()
        await _drop_database(base_url, name)


async def _seed_scratch(scratch_engine: AsyncEngine, prod_state: _ProdState) -> None:
    """Materialize prod's reflected structure into the scratch DB and stamp its
    ``alembic_version`` to prod's recorded revision — reproducing the live DB the
    candidate's upgrade will actually run against."""

    def _seed(sync_conn: sa.Connection) -> None:
        prod_state.metadata.create_all(sync_conn)
        if not sa.inspect(sync_conn).has_table("alembic_version"):
            sync_conn.execute(
                sa.text(
                    "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
                )
            )
        sync_conn.execute(sa.text("DELETE FROM alembic_version"))
        sync_conn.execute(
            sa.text("INSERT INTO alembic_version (version_num) VALUES (:r)"),
            {"r": prod_state.revision},
        )

    try:
        async with scratch_engine.begin() as conn:
            await conn.run_sync(_seed)
    except Exception as exc:
        raise _MigrationApplyError(
            f"could not seed scratch DB with production's reflected schema "
            f"(rev {prod_state.revision!r}): {type(exc).__name__}: {exc}"
        ) from exc


def _alembic_upgrade_head(sync_conn: sa.Connection, script_location: str) -> None:
    """Drive ``alembic upgrade head`` against an EXISTING connection.

    Reuses the scratch connection (no second engine, no nested event loop) and
    mirrors ``alembic/env.py``'s ``do_run_migrations`` — ``transaction_per_migration
    =True`` so a migration that issues its own ``COMMIT`` (e.g. ``CREATE INDEX
    CONCURRENTLY``) does not leak its transaction boundary into the next one.
    """
    # Imported lazily so module import never pulls Alembic's runtime internals.
    from alembic.config import Config
    from alembic.runtime.environment import EnvironmentContext
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", script_location)
    script = ScriptDirectory.from_config(cfg)

    def _do_upgrade(rev, _context):
        return script._upgrade_revs("head", rev)

    with EnvironmentContext(
        cfg, script, fn=_do_upgrade, as_sql=False, destination_rev="head"
    ) as env:
        env.configure(connection=sync_conn, transaction_per_migration=True)
        with env.begin_transaction():
            env.run_migrations()


def _reconcile_then_upgrade(sync_conn: sa.Connection, script_location: str) -> None:
    """Reproduce ``backend/run_migrations.sh`` against the scratch connection: the
    :mod:`app.migration_reconciliation` pre-step, then ``alembic upgrade head``.

    Honoring the pre-step makes the gate compose with the deployed reconciliation
    band-aid — a candidate whose deploy path self-heals (the authorized
    ``0143`` renumber) is NOT false-blocked, while an unhandled renumber-skip the
    band-aid does not cover still surfaces as drift after the upgrade.
    """
    with sync_conn.begin():
        reconcile_pre_renumber_divergence(sync_conn)
    _alembic_upgrade_head(sync_conn, script_location)


async def _apply_candidate_migrations(
    scratch_engine: AsyncEngine, candidate_sources: dict[str, str]
) -> None:
    """Materialize the candidate's migration files into a temp ``versions/`` dir
    and apply them (reconcile pre-step + upgrade head) against the scratch DB."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            versions_dir = os.path.join(tmp, "versions")
            os.makedirs(versions_dir)
            for filename, text in candidate_sources.items():
                with open(os.path.join(versions_dir, filename), "w") as handle:
                    handle.write(text)
            async with scratch_engine.connect() as conn:
                await conn.run_sync(
                    lambda sync_conn: _reconcile_then_upgrade(sync_conn, tmp)
                )
    except Exception as exc:
        raise _MigrationApplyError(
            f"candidate migration chain failed to apply against production's "
            f"recorded state: {type(exc).__name__}: {exc}"
        ) from exc


async def _reflect_scratch_drift(
    scratch_engine: AsyncEngine, required_metadata: sa.MetaData
) -> list[str]:
    """Tables/columns *required_metadata* expects that the post-upgrade scratch DB
    lacks — the deterministic drift signal."""
    async with scratch_engine.connect() as conn:
        return await conn.run_sync(
            lambda sync_conn: reflect_schema_drift(
                sync_conn, metadata=required_metadata
            )
        )


def _is_migration_source(filename: str) -> bool:
    return (
        filename.endswith(".py")
        and filename != "__init__.py"
        and not filename.startswith("_")
    )


async def _fetch_candidate_sources(
    gh_token: str,
    owner: str,
    repo: str,
    candidate_ref: str,
    project_id: str | None,
) -> dict[str, str] | None:
    """Fetch the candidate's Alembic migration sources at *candidate_ref*.

    Returns ``None`` (=> ``defer``) on ANY fetch failure or an empty/undecodable
    file — a fetch error is never "no migrations". ``list_versions_at_ref`` raises
    on a non-404 listing failure (propagated to the caller's ``defer``);
    ``get_file_text_at_ref`` returns ``None`` on failure, which we treat as defer.
    """
    filenames = await github.list_versions_at_ref(
        gh_token, owner, repo, candidate_ref, project_id=project_id
    )
    sources: dict[str, str] = {}
    for filename in filenames:
        if not _is_migration_source(filename):
            continue
        text = await github.get_file_text_at_ref(
            f"{_ALEMBIC_VERSIONS_DIR}/{filename}",
            token=gh_token,
            owner=owner,
            repo=repo,
            ref=candidate_ref,
            project_id=project_id,
        )
        if not text:
            return None
        sources[filename] = text
    return sources or None


async def run_migration_apply_gate(
    *,
    prod_engine: AsyncEngine,
    gh_token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    candidate_ref: str | None = None,
    project_id: str | None = None,
    required_metadata: sa.MetaData | None = None,
    candidate_sources: dict[str, str] | None = None,
) -> Verdict:
    """Pre-promotion scratch-DB migration-apply gate.

    Returns a deterministic ``('ok'|'block'|'defer', detail)`` verdict:

      * ``('ok', None)``       — the candidate's migrations, applied the deploy
        way against production's REAL recorded state, leave a schema satisfying
        every required table/column. Safe to promote (on this dimension).
      * ``('block', reason)``  — the resulting schema is MISSING a required object
        (*reason* names it). The deploy-fatal renumber-skip class — a HARD gate.
      * ``('defer', reason)``  — production's recorded state could not be read,
        the candidate's migration files could not be fetched, or the scratch DB
        could not be provisioned / seeded / upgraded. Safety is UNKNOWN, so the
        release HOLDS (never waved through); the next qualification cycle retries.

    *prod_engine* is the live production bind (read-only here). *candidate_sources*
    may be injected (filename -> source) to bypass the GitHub fetch in tests; in
    production it is fetched from *gh_token/owner/repo/candidate_ref*.
    *required_metadata* defaults to the application's full ``SQLModel.metadata``.

    ISOLATION SAFETY: production is only ever read; the scratch database is a
    freshly-provisioned, separately-named Postgres database, and the orchestrator
    asserts its name differs from production's before any DDL.
    """
    required = required_metadata if required_metadata is not None else SQLModel.metadata
    prod_url = prod_engine.url
    if not prod_url.get_backend_name().startswith("postgresql"):
        # The renumber-skip class is a Postgres-deploy phenomenon; the gate needs
        # a real migration-applying Postgres scratch target. With a non-Postgres
        # prod bind we cannot reproduce the deploy, so we HOLD (never wave through).
        return (
            "defer",
            f"scratch-DB migration-apply gate requires a PostgreSQL production "
            f"bind; got {prod_url.get_backend_name()!r}",
        )

    # 1. Production's REAL recorded state (read-only).
    try:
        prod_state = await _read_prod_state(prod_engine)
    except Exception as exc:  # noqa: BLE001 — indeterminate prod state HOLDS
        return (
            "defer",
            f"could not read production's recorded schema state: "
            f"{type(exc).__name__}: {exc}",
        )
    if prod_state is None:
        return (
            "defer",
            "production's recorded Alembic revision is unset/unreadable "
            "(no alembic_version stamp) — cannot reproduce the deploy",
        )

    # 2. Candidate migration sources (fetched unless injected).
    if candidate_sources is None:
        if not (gh_token and owner and repo and candidate_ref):
            return (
                "defer",
                "candidate migration sources unavailable: no injected sources and "
                "incomplete GitHub coordinates (token/owner/repo/ref)",
            )
        try:
            candidate_sources = await _fetch_candidate_sources(
                gh_token, owner, repo, candidate_ref, project_id
            )
        except Exception as exc:  # noqa: BLE001 — fetch failure HOLDS
            return (
                "defer",
                f"could not fetch candidate migrations at {candidate_ref!r}: "
                f"{type(exc).__name__}: {exc}",
            )
        if not candidate_sources:
            return (
                "defer",
                f"candidate migration fetch at {candidate_ref!r} returned no "
                "readable migration sources",
            )

    # 3. Provision an isolated scratch DB, 4. seed it, 5. apply, 6. assert no drift.
    try:
        async with ephemeral_postgres_database(prod_url) as (scratch_engine, name):
            # ISOLATION GUARD: never run DDL against the production database.
            if name == prod_url.database:
                return (
                    "defer",
                    "refusing to run the migration-apply gate: scratch database "
                    "name collided with production's",
                )
            await _seed_scratch(scratch_engine, prod_state)
            await _apply_candidate_migrations(scratch_engine, candidate_sources)
            drift = await _reflect_scratch_drift(scratch_engine, required)
    except _MigrationApplyError as exc:
        return ("defer", str(exc))
    except Exception as exc:  # noqa: BLE001 — any provisioning failure HOLDS
        return (
            "defer",
            f"could not provision/run the isolated scratch database: "
            f"{type(exc).__name__}: {exc}",
        )

    if drift:
        return (
            "block",
            f"candidate migrations applied against production's recorded revision "
            f"{prod_state.revision!r} leave the schema missing required object(s) "
            f"(deploy-fatal drift): " + "; ".join(drift),
        )
    return ("ok", None)

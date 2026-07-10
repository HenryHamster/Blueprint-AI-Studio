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

import asyncio
import io
import json
import logging
import os
import sys
import tarfile
import tempfile
import textwrap
import uuid
from contextlib import asynccontextmanager
from typing import NamedTuple
from urllib.parse import quote

import sqlalchemy as sa
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

import app.github as github
from app.deploy_smoketest import reflect_schema_drift
from app.migration_chain import (
    build_migration_chain,
    find_filename_revision_mismatches,
    find_heads,
)
from app.migration_reconciliation import reconcile_pre_renumber_divergence

logger = logging.getLogger(__name__)


def _safe_tar_extractall(tar: tarfile.TarFile, dest: str) -> None:
    """Extract a tar archive into dest, guarding against path traversal (Zip Slip).

    Uses the built-in 'data' filter on Python 3.11.4+ which strips dangerous
    attributes (setuid bits, device files, absolute/traversal paths). Falls back
    to explicit member validation on older runtimes.
    """
    if sys.version_info >= (3, 11, 4):
        tar.extractall(dest, filter="data")
    else:
        abs_dest = os.path.abspath(dest) + os.sep
        safe_members = []
        for member in tar.getmembers():
            member_path = os.path.abspath(os.path.join(dest, member.name))
            if not member_path.startswith(abs_dest):
                raise ValueError(
                    f"tarfile member {member.name!r} would extract outside destination"
                )
            safe_members.append(member)
        tar.extractall(dest, members=safe_members)


# ('ok'|'block'|'defer', detail). Mirrors check_qualification_migration_identity.
Verdict = tuple[str, str | None]

# Repo-relative directory holding the Alembic migration files, used to resolve a
# bare basename (as returned by ``github.list_versions_at_ref``) to a full path
# for the per-file text fetch — same constant the rollup validator uses.
_ALEMBIC_VERSIONS_DIR = "backend/alembic/versions"

_CANDIDATE_METADATA_SCRIPT = textwrap.dedent(
    """
    import importlib
    import json
    import os
    import pathlib
    import sys

    from sqlmodel import SQLModel

    repo_root = pathlib.Path(os.environ["CANDIDATE_REPO_ROOT"])
    backend_root = repo_root / "backend"
    app_root = backend_root / "app"
    sys.path.insert(0, str(backend_root))

    def _module_name(path: pathlib.Path) -> str:
        rel = path.relative_to(backend_root).with_suffix("")
        return ".".join(rel.parts)

    def _looks_like_table_module(path: pathlib.Path) -> bool:
        if path.name == "models.py":
            return True
        try:
            text = path.read_text()
        except Exception:
            return False
        markers = ("table=True", "__tablename__", "sa.Table(", "sqlalchemy.Table(")
        return any(marker in text for marker in markers)

    importlib.import_module("app.models")
    for path in sorted(app_root.rglob("*.py")):
        if path.name == "__init__.py" or not _looks_like_table_module(path):
            continue
        importlib.import_module(_module_name(path))

    payload = {
        table.name: [column.name for column in table.columns]
        for table in SQLModel.metadata.sorted_tables
    }
    print(json.dumps(payload, sort_keys=True))
    """
)


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


def _metadata_from_shape(shape: dict[str, list[str]]) -> sa.MetaData:
    """Build minimal metadata from table/column names only.

    The gate's drift checks are directional on names only: table existence and
    column existence. Types, indexes, and constraints are irrelevant here."""

    metadata = sa.MetaData()
    for table_name in sorted(shape):
        columns = shape[table_name] or []
        sa.Table(
            table_name,
            metadata,
            *(sa.Column(column_name, sa.String()) for column_name in columns),
        )
    return metadata


def _metadata_shape_drift(
    present_metadata: sa.MetaData, required_metadata: sa.MetaData
) -> list[str]:
    """Objects *required_metadata* expects that *present_metadata* lacks."""

    drift: list[str] = []
    present_tables = present_metadata.tables
    for table_name, required_table in required_metadata.tables.items():
        present_table = present_tables.get(table_name)
        if present_table is None:
            drift.append(
                f"table '{table_name}' is missing from the candidate migration chain"
            )
            continue
        present_columns = {column.name for column in present_table.columns}
        drift.extend(
            f"column '{table_name}.{column.name}' is missing from the candidate migration chain"
            for column in required_table.columns
            if column.name not in present_columns
        )
    return sorted(drift)


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


# Serializes the in-process Alembic apply. Alembic's runtime is PROCESS-GLOBAL:
# the ``alembic.op`` / ``alembic.context`` module-level proxies are rebound on
# every ``EnvironmentContext`` enter, so two migration applies running
# concurrently in THIS process clobber each other's bindings — surfacing as a
# flaky ``KeyError``/aborted-transaction rather than a deterministic failure.
# The release qualifier runs this gate PRE-LOCK (before the ReleaseVersion row
# ``FOR UPDATE``), so two overlapping finalize attempts — e.g. the reaper sweep
# starting a second finalize while the first is still inside its slow
# GitHub+scratch-DB gate — execute concurrent applies. On 2026-06-16 that race
# false-failed a healthy release qualification to ``infra_failure`` (the gate
# logged "ok (applies cleanly)" on one attempt and ``KeyError: 'config'`` on the
# overlapping one, minutes apart, for the identical candidate). The lock is
# recreated whenever the running loop changes (``asyncio.Lock`` binds to the loop
# it is first awaited on and raises if reused from another loop — every
# pytest-asyncio test gets a fresh loop); in production the loop is stable, so the
# same lock serializes every apply. Mirrors ``migration_reservations._project_lock``.
_APPLY_LOCK_BY_LOOP: tuple[object, asyncio.Lock] | None = None


def _apply_lock() -> asyncio.Lock:
    global _APPLY_LOCK_BY_LOOP
    loop = asyncio.get_running_loop()
    entry = _APPLY_LOCK_BY_LOOP
    if entry is None or entry[0] is not loop:
        lock = asyncio.Lock()
        _APPLY_LOCK_BY_LOOP = (loop, lock)
        return lock
    return entry[1]


def _write_candidate_files(tmp: str, candidate_sources: dict[str, str]) -> str:
    """Write candidate migration files to a temporary directory (sync helper)."""
    versions_dir = os.path.join(tmp, "versions")
    os.makedirs(versions_dir)
    for filename, text in candidate_sources.items():
        with open(os.path.join(versions_dir, filename), "w") as handle:
            handle.write(text)
    return tmp


async def _apply_candidate_migrations(
    scratch_engine: AsyncEngine, candidate_sources: dict[str, str]
) -> None:
    """Materialize the candidate's migration files into a temp ``versions/`` dir
    and apply them (reconcile pre-step + upgrade head) against the scratch DB.

    The apply itself is serialized process-wide via :func:`_apply_lock` because
    Alembic's runtime is global and not safe for concurrent in-process runs (see
    that lock's note). Writing the throwaway temp files is per-call and stays
    outside the lock; only the EnvironmentContext-driven apply is serialized."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            await asyncio.to_thread(_write_candidate_files, tmp, candidate_sources)
            async with _apply_lock(), scratch_engine.connect() as conn:
                await conn.run_sync(
                    lambda sync_conn: _reconcile_then_upgrade(sync_conn, tmp)
                )
    except Exception as exc:
        raise _MigrationApplyError(
            f"candidate migration chain failed to apply against production's "
            f"recorded state: {type(exc).__name__}: {exc}"
        ) from exc


async def _build_candidate_reference_schema(
    base_url: URL, candidate_sources: dict[str, str]
) -> sa.MetaData:
    """The candidate's required schema, derived by applying its FULL migration
    chain to a FRESH empty database and reflecting the result.

    This is the canonical deploy-target schema the candidate's migrations define:
    EVERY persisted table the chain creates, regardless of which ORM module
    declares it. The earlier approach — exec'ing a candidate ``backend/app/
    models.py`` snapshot inside the serving process to harvest its ORM metadata —
    was both incomplete and unsafe:

      * Incomplete: ``models.py`` is NOT the full ORM metadata. Other modules
        register persisted tables onto the same ``SQLModel.metadata`` at import
        time (e.g. ``DirectionDriftSnapshot`` in ``app.drift_detection``, created
        by migration 0057). A models.py-only reconstruction would miss them, so a
        candidate that adds/changes such a table could pass the gate even when the
        scratch DB is missing it.
      * Unsafe: ``models.py`` has import-time SQLAlchemy event registrations
        (a global ``before_flush`` hook, attribute ``set`` listeners). Exec'ing it
        in-process registers those hooks against the LIVE process and never removes
        them, leaking listeners/mappers into production on every qualification.

    Building the reference from a clean migration apply sidesteps both: migrations
    create the FULL schema, and the candidate's Python is never executed in the
    serving process — it runs only as Alembic DDL against an isolated scratch DB,
    with ZERO in-process side effects. The reference DB is a separate ephemeral
    database; production is never touched.
    """

    def _reflect(sync_conn: sa.Connection) -> sa.MetaData:
        metadata = sa.MetaData()
        metadata.reflect(bind=sync_conn)
        return metadata

    async with ephemeral_postgres_database(base_url) as (ref_engine, _name):
        # Apply the candidate chain from an EMPTY DB (no prod seed): the
        # reconcile pre-step is a strict no-op on a fresh database, so this is a
        # plain ``alembic upgrade head`` that materializes the full schema.
        await _apply_candidate_migrations(ref_engine, candidate_sources)
        async with ref_engine.connect() as conn:
            return await conn.run_sync(_reflect)


async def _download_candidate_repo_archive(
    gh_token: str,
    owner: str,
    repo: str,
    candidate_ref: str,
    project_id: str | None,
) -> bytes | None:
    """Fetch the candidate repo archive as a tarball."""

    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    encoded_ref = quote(candidate_ref, safe="")
    url = f"https://api.github.com/repos/{owner}/{repo}/tarball/{encoded_ref}"
    try:
        response = await github._github_request(
            "GET",
            url,
            headers,
            max_retries=2,
            project_id=project_id,
            follow_redirects=True,
        )
    except Exception:  # noqa: BLE001 - caller degrades to defer
        return None
    if response.status_code != 200:
        return None
    return response.content


def _extract_snapshot_repo_root(
    tmp_dir: str, archive_bytes: bytes
) -> str:
    """Extract a candidate repo archive and return the repo root path (blocking I/O)."""
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tar:
        _safe_tar_extractall(tar, tmp_dir)
    extracted_roots = [
        os.path.join(tmp_dir, name)
        for name in os.listdir(tmp_dir)
        if os.path.isdir(os.path.join(tmp_dir, name))
    ]
    if not extracted_roots:
        raise _MigrationApplyError(
            "candidate code snapshot unpacked to no repo root"
        )
    return extracted_roots[0]


async def _build_candidate_required_metadata_from_snapshot(
    gh_token: str,
    owner: str,
    repo: str,
    candidate_ref: str,
    project_id: str | None,
) -> sa.MetaData:
    """Build candidate code-required metadata out of process from a repo snapshot.

    The snapshot is pinned to ``candidate_ref`` and imported in a subprocess so
    any SQLAlchemy listeners/mappers the candidate registers cannot leak into the
    serving process."""

    archive = await _download_candidate_repo_archive(
        gh_token, owner, repo, candidate_ref, project_id
    )
    if not archive:
        raise _MigrationApplyError(
            f"could not fetch the candidate code snapshot at {candidate_ref!r}"
        )

    with tempfile.TemporaryDirectory() as tmp:
        repo_root = await asyncio.to_thread(
            _extract_snapshot_repo_root, tmp, archive
        )

        env = os.environ.copy()
        env["CANDIDATE_REPO_ROOT"] = repo_root
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _CANDIDATE_METADATA_SCRIPT,
            cwd=repo_root,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = (
                stderr.decode(errors="replace").strip()
                or stdout.decode(errors="replace").strip()
            )
            raise _MigrationApplyError(
                f"candidate code snapshot at {candidate_ref!r} could not be imported "
                f"to derive required metadata: {detail or f'exit {proc.returncode}'}"
            )
        try:
            shape = await asyncio.to_thread(json.loads, stdout.decode())
        except Exception as exc:
            raise _MigrationApplyError(
                f"candidate code snapshot at {candidate_ref!r} returned malformed "
                f"required metadata: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(shape, dict) or not all(
            isinstance(table_name, str)
            and isinstance(columns, list)
            and all(isinstance(column_name, str) for column_name in columns)
            for table_name, columns in shape.items()
        ):
            raise _MigrationApplyError(
                f"candidate code snapshot at {candidate_ref!r} returned an invalid "
                "required-metadata shape"
            )
        return _metadata_from_shape(shape)


async def _build_candidate_required_metadata(
    *,
    gh_token: str | None,
    owner: str | None,
    repo: str | None,
    candidate_ref: str | None,
    project_id: str | None,
) -> sa.MetaData:
    """Build the code-required schema for the candidate.

    Production should resolve this from the candidate's own snapshot. Tests that
    inject ``candidate_sources`` without GitHub coordinates fall back to the live
    process metadata so the synthetic chains can exercise the default path."""

    if gh_token and owner and repo and candidate_ref:
        return await _build_candidate_required_metadata_from_snapshot(
            gh_token, owner, repo, candidate_ref, project_id
        )
    return SQLModel.metadata


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


def _check_structural_defects(candidate_sources: dict[str, str]) -> Verdict | None:
    """Detect deterministically-defective migration chains.

    Dup revision ids and multi-head chains are structurally broken regardless of
    production state or scratch-DB availability — block immediately instead of
    auto-retrying as an infra failure.

    Returns a ``('block', reason)`` verdict when a structural defect is found,
    or ``None`` when the chain is structurally sound.
    """
    _mismatches = find_filename_revision_mismatches(candidate_sources)
    if _mismatches:
        return (
            "block",
            "candidate migration chain has filename/revision mismatches: "
            + "; ".join(_mismatches),
        )
    _chain, _duplicates, _ = build_migration_chain(candidate_sources)
    if _duplicates:
        return (
            "block",
            "candidate migration chain has duplicate revision id(s): "
            + ", ".join(sorted(_duplicates)),
        )
    _heads = find_heads(_chain)
    if len(_heads) > 1:
        return (
            "block",
            "candidate migration chain has multiple heads (not a single linear chain): "
            + ", ".join(sorted(_heads)),
        )
    # A cycle produces zero structural heads (every revision is pointed to by
    # another — no revision has down_revision = None or an external parent).
    if len(_heads) == 0 and _chain:
        return (
            "block",
            "candidate migration chain has a cycle (zero structural heads) — "
            "no revision chains back to down_revision = None",
        )
    # Broken down_revision pointer: a revision whose parent is absent from the
    # chain. This case can produce a single apparent head (masking the defect
    # from the multi-head check above) when only the root has a dangling pointer.
    _dangling = sorted(
        (rev, down)
        for rev, down in _chain.items()
        if down is not None and down not in _chain
    )
    if _dangling:
        rev, down = _dangling[0]
        return (
            "block",
            f"candidate migration chain has a broken down_revision pointer: "
            f"revision '{rev}' references '{down}' which is not in the chain",
        )
    return None


def _extract_and_read_sources(archive: bytes) -> dict[str, str] | None:
    """Extract tar archive and read migration sources (sync helper)."""
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
            _safe_tar_extractall(tar, tmp)
        # GitHub archives always unpack to a single top-level repo dir.
        extracted_roots = [
            os.path.join(tmp, name)
            for name in os.listdir(tmp)
            if os.path.isdir(os.path.join(tmp, name))
        ]
        if not extracted_roots:
            return None
        versions_dir = os.path.join(extracted_roots[0], _ALEMBIC_VERSIONS_DIR)
        if not os.path.isdir(versions_dir):
            return None
        sources: dict[str, str] = {}
        for filename in sorted(os.listdir(versions_dir)):
            if not _is_migration_source(filename):
                continue
            full_path = os.path.join(versions_dir, filename)
            if not os.path.isfile(full_path):
                continue
            with open(full_path, encoding="utf-8") as fh:
                text = fh.read()
            if not text:
                return None
            sources[filename] = text
        return sources or None


async def _fetch_candidate_sources(
    gh_token: str,
    owner: str,
    repo: str,
    candidate_ref: str,
    project_id: str | None,
) -> dict[str, str] | None:
    """Read the candidate's Alembic migration sources at *candidate_ref*.

    Sources come from ONE repo tarball (the same archive endpoint the
    reference-schema builder uses, :func:`_download_candidate_repo_archive`)
    instead of a contents-API call per migration file. The old per-file loop
    issued ``1 + N`` (~215) GitHub contents requests on every finalize attempt —
    the twin of the identity-gate fetch storm — which drained the install
    token's rate-limit budget and wedged qualification into a
    ``migration_apply_check_unavailable`` defer loop.

    Returns ``None`` (=> ``defer``) on ANY download/unpack/read failure or an
    empty result — a fetch error is never "no migrations".
    """
    archive = await _download_candidate_repo_archive(
        gh_token, owner, repo, candidate_ref, project_id
    )
    if not archive:
        return None
    try:
        return await asyncio.to_thread(_extract_and_read_sources, archive)
    except Exception:  # noqa: BLE001 — unpack/read failure HOLDS (=> defer)
        return None


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
    *required_metadata* remains an escape hatch for tests / explicit callers. When
    omitted, the gate derives the candidate's migration-defined reference schema
    from a fresh apply of its own migration chain AND compares that to the
    candidate code's required schema so a forgotten migration still blocks.

    ISOLATION SAFETY: production is only ever read; the scratch database is a
    freshly-provisioned, separately-named Postgres database, and the orchestrator
    asserts its name differs from production's before any DDL.
    """
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

    # 1. Candidate migration sources (fetched unless injected) — resolved BEFORE
    # reading production's state so a structurally-defective candidate (duplicate
    # revision id, multi-head chain) blocks immediately without any DB round-trip.
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

    # 2. Structural check: dup revision ids and multi-head chains are deterministically
    # defective regardless of production state or scratch-DB availability — block
    # immediately instead of auto-retrying the run as an infra failure.
    structural_verdict = _check_structural_defects(candidate_sources)
    if structural_verdict is not None:
        return structural_verdict

    # 3. Production's REAL recorded state (read-only).
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

    if required_metadata is None:
        # Derive the candidate's required schema from a clean apply of its OWN
        # migration chain (full schema, candidate-pinned, zero in-process side
        # effects). candidate_sources is guaranteed non-None at this point —
        # either injected or already fetched above (else we returned 'defer').
        try:
            candidate_reference_metadata = await _build_candidate_reference_schema(
                prod_url, candidate_sources
            )
            candidate_required_metadata = await _build_candidate_required_metadata(
                gh_token=gh_token,
                owner=owner,
                repo=repo,
                candidate_ref=candidate_ref,
                project_id=project_id,
            )
        except _MigrationApplyError as exc:
            return ("defer", str(exc))
        except Exception as exc:  # noqa: BLE001 — reference-build failure HOLDS
            return (
                "defer",
                f"could not build the candidate reference schema at "
                f"{candidate_ref!r}: {type(exc).__name__}: {exc}",
            )
        missing_from_candidate_migrations = _metadata_shape_drift(
            candidate_reference_metadata, candidate_required_metadata
        )
        if missing_from_candidate_migrations:
            return (
                "block",
                f"candidate code at {candidate_ref!r} requires object(s) absent "
                f"from its own migration chain (missing migration): "
                + "; ".join(missing_from_candidate_migrations),
            )
        required_metadata = candidate_reference_metadata

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
            drift = await _reflect_scratch_drift(scratch_engine, required_metadata)
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

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
import re
import signal
import sys
import tarfile
import tempfile
import textwrap
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Awaitable, Callable, cast, NamedTuple, TypeVar
from urllib.parse import quote

import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

import app.github as github
from app.deploy_smoketest import reflect_schema_drift
from app.migration_chain import (
    build_migration_chain,
    find_filename_revision_mismatches,
    find_heads,
    parse_revision_identifiers,
)
from app.migration_reconciliation import reconcile_pre_renumber_divergence
from app.time_utils import exponential_backoff

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
        abs_dest = os.path.abspath(dest)
        safe_members = []
        for member in tar.getmembers():
            member_path = os.path.abspath(os.path.join(dest, member.name))
            if os.path.commonpath((abs_dest, member_path)) != abs_dest:
                raise ValueError(
                    f"tarfile member {member.name!r} would extract outside destination"
                )
            if member.issym() or member.islnk():
                raise ValueError(
                    f"tarfile link member {member.name!r} is not allowed"
                )
            if not (member.isfile() or member.isdir()):
                raise ValueError(
                    f"tarfile special member {member.name!r} is not allowed"
                )
            safe_members.append(member)
        tar.extractall(dest, members=safe_members)


# ('ok'|'block'|'defer', detail). Mirrors check_qualification_migration_identity.
Verdict = tuple[str, str | None]

MIGRATION_APPLY_SCRATCH_ENV = "MIGRATION_APPLY_SCRATCH_DATABASE_URL"


@dataclass(frozen=True)
class MigrationApplyScratchPreflight:
    """Typed, value-safe result for the optional scratch prerequisite."""

    status: str
    code: str
    target: URL | None
    detail: str | None


def preflight_migration_apply_scratch(
    raw_url: str | URL | None,
) -> MigrationApplyScratchPreflight:
    """Validate scratch configuration without connecting or attempting DDL.

    The raw value is deliberately absent from every diagnostic.  Availability
    and CREATE/DROP permission are proven later by the existing isolated gate.
    """
    if raw_url is None or (isinstance(raw_url, str) and not raw_url.strip()):
        return MigrationApplyScratchPreflight(
            status="absent",
            code="scratch_database_url_missing",
            target=None,
            detail=(
                "scratch_database_url_missing: "
                f"{MIGRATION_APPLY_SCRATCH_ENV} is not configured; bind a dedicated "
                "PostgreSQL scratch target whose user can CREATE and DROP databases"
            ),
        )
    try:
        target = raw_url if isinstance(raw_url, URL) else make_url(raw_url.strip())
    except Exception:  # noqa: BLE001 — sanitized typed configuration failure
        return MigrationApplyScratchPreflight(
            status="defer",
            code="scratch_database_url_invalid",
            target=None,
            detail=(
                "scratch_database_url_invalid: "
                f"{MIGRATION_APPLY_SCRATCH_ENV} must be a valid PostgreSQL URL "
                "for a dedicated scratch target"
            ),
        )

    if not target.get_backend_name().startswith("postgresql") or not target.database:
        return MigrationApplyScratchPreflight(
            status="defer",
            code="scratch_database_url_invalid",
            target=None,
            detail=(
                "scratch_database_url_invalid: "
                f"{MIGRATION_APPLY_SCRATCH_ENV} must name a PostgreSQL database "
                "on a dedicated scratch target"
            ),
        )
    if target.drivername == "postgresql":
        target = target.set(drivername="postgresql+asyncpg")
    return MigrationApplyScratchPreflight(
        status="ready",
        code="scratch_database_ready",
        target=target,
        detail=None,
    )


# Repo-relative directory holding the Alembic migration files, used to resolve a
# bare basename (as returned by ``github.list_versions_at_ref``) to a full path
# for the per-file text fetch — same constant the rollup validator uses.
_ALEMBIC_VERSIONS_DIR = "backend/alembic/versions"

_CANDIDATE_METADATA_SCRIPT = textwrap.dedent(
    """
    import importlib
    import io
    import json
    import os
    import pathlib
    import sys
    import tarfile

    from sqlmodel import SQLModel

    work_dir = pathlib.Path(os.environ["CANDIDATE_WORK_DIR"])
    archive = sys.stdin.buffer.read()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
        if sys.version_info >= (3, 11, 4):
            tar.extractall(work_dir, filter="data")
        else:
            abs_dest = str(work_dir.resolve())
            safe_members = []
            for member in tar.getmembers():
                member_path = str((work_dir / member.name).resolve())
                if os.path.commonpath((abs_dest, member_path)) != abs_dest:
                    raise ValueError(
                        f"tarfile member {member.name!r} would extract outside destination"
                    )
                if member.issym() or member.islnk():
                    raise ValueError(
                        f"tarfile link member {member.name!r} is not allowed"
                    )
                if not (member.isfile() or member.isdir()):
                    raise ValueError(
                        f"tarfile special member {member.name!r} is not allowed"
                    )
                safe_members.append(member)
            tar.extractall(work_dir, members=safe_members)

    roots = sorted(path for path in work_dir.iterdir() if path.is_dir())
    if not roots:
        raise RuntimeError("candidate code snapshot unpacked to no repo root")
    repo_root = roots[0]
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

_MIGRATION_APPLY_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import os

    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.migration_apply_gate import (
        _reconcile_then_upgrade,
        _scratch_connect_args,
    )

    async def _main():
        database_url = os.environ["MIGRATION_APPLY_DATABASE_URL"]
        script_location = os.environ["MIGRATION_APPLY_SCRIPT_LOCATION"]
        url = make_url(database_url)
        engine = create_async_engine(
            url,
            poolclass=NullPool,
            connect_args=_scratch_connect_args(url),
        )
        try:
            async with engine.connect() as conn:
                await conn.run_sync(
                    lambda sync_conn: _reconcile_then_upgrade(
                        sync_conn, script_location
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(_main())
    """
)


# Retry configuration for transient operations — overridable in tests (via
# monkeypatch) for speed without altering production behavior.
_TRANSIENT_RETRY_MAX_ATTEMPTS = 3
_TRANSIENT_RETRY_BASE_DELAY = 1.0
_TRANSIENT_RETRY_MAX_DELAY = 16.0

# Every external or database-heavy leg is independently bounded. The values are
# generous relative to CI's observed timings while still bounding one attempt.
_CANDIDATE_METADATA_TIMEOUT_SECONDS = 60.0
_MIGRATION_APPLY_SUBPROCESS_TIMEOUT_SECONDS = 180.0
_MIGRATION_APPLY_LOCK_TIMEOUT_SECONDS = 240.0
_SCRATCH_CONNECT_TIMEOUT_SECONDS = 10.0
_SCRATCH_COMMAND_TIMEOUT_SECONDS = 150.0
_SCRATCH_STATEMENT_TIMEOUT_MS = 120_000
_SCRATCH_LOCK_TIMEOUT_MS = 15_000
_SCRATCH_CREATE_TIMEOUT_SECONDS = 30.0
_SCRATCH_SEED_TIMEOUT_SECONDS = 90.0
_SCRATCH_REFLECT_TIMEOUT_SECONDS = 30.0
_SCRATCH_CLEANUP_TIMEOUT_SECONDS = 30.0
_ENGINE_DISPOSE_TIMEOUT_SECONDS = 10.0
_SUBPROCESS_REAP_TIMEOUT_SECONDS = 5.0
_SUBPROCESS_DIAGNOSTIC_MAX_BYTES = 16_384
_SUBPROCESS_ENV_ALLOWLIST = (
    "LANG",
    "LC_ALL",
    "LD_LIBRARY_PATH",
    "PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "VIRTUAL_ENV",
    "WINDIR",
)


_T = TypeVar("_T")


async def _with_transient_retry(
    coro_factory: Callable[[], Awaitable[_T]],
    *,
    label: str,
) -> _T:
    """Invoke *coro_factory* with bounded exponential backoff on exceptions.

    *coro_factory* is a zero-argument awaitable factory — a lambda or closure
    — because a consumed coroutine cannot be re-awaited.  On each attempt a
    fresh coroutine is created so the retry observes a clean call stack.

    Reads retry parameters from module-level ``_TRANSIENT_RETRY_*`` constants
    so tests can monkeypatch them for speed without altering production.

    After exhausting *max_attempts* consecutive failures, re-raises the *last*
    exception so existing ``try``/``except`` structures around the caller
    convert it to ``("defer", ...)``.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _TRANSIENT_RETRY_MAX_ATTEMPTS + 1):
        if attempt > 1:
            delay = exponential_backoff(
                attempt - 1,
                base=_TRANSIENT_RETRY_BASE_DELAY,
                cap=_TRANSIENT_RETRY_MAX_DELAY,
                jitter=0.25,
            )
            logger.warning(
                "[MIGRATION_APPLY_GATE] %s attempt %d/%d transient failure; "
                "retrying in %.1fs: %s",
                label,
                attempt - 1,
                _TRANSIENT_RETRY_MAX_ATTEMPTS,
                delay,
                last_exc,
            )
            await asyncio.sleep(delay)
        try:
            return await coro_factory()
        except _MigrationApplyTimeout:
            # A stage watchdog already proved this attempt exceeded its budget.
            # Re-running it internally only multiplies a deterministic stall.
            raise
        except Exception as exc:  # noqa: BLE001 — retry on transient exceptions
            last_exc = exc
    # All attempts exhausted — re-raise the last exception.
    raise cast(Exception, last_exc)


# Retry configuration for transient operations — overridable in tests (via
# monkeypatch) for speed without altering production behavior.
_TRANSIENT_RETRY_MAX_ATTEMPTS = 3
_TRANSIENT_RETRY_BASE_DELAY = 1.0
_TRANSIENT_RETRY_MAX_DELAY = 16.0


_T = TypeVar("_T")


async def _with_transient_retry(
    coro_factory: Callable[[], Awaitable[_T]],
    *,
    label: str,
    no_retry_types: tuple[type[Exception], ...] = (),
) -> _T:
    """Invoke *coro_factory* with bounded exponential backoff on exceptions.

    *coro_factory* is a zero-argument awaitable factory — a lambda or closure
    — because a consumed coroutine cannot be re-awaited.  On each attempt a
    fresh coroutine is created so the retry observes a clean call stack.

    Reads retry parameters from module-level ``_TRANSIENT_RETRY_*`` constants
    so tests can monkeypatch them for speed without altering production.

    After exhausting *max_attempts* consecutive failures, re-raises the *last*
    exception so existing ``try``/``except`` structures around the caller
    convert it to ``("defer", ...)``.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _TRANSIENT_RETRY_MAX_ATTEMPTS + 1):
        if attempt > 1:
            delay = exponential_backoff(
                attempt - 1,
                base=_TRANSIENT_RETRY_BASE_DELAY,
                cap=_TRANSIENT_RETRY_MAX_DELAY,
                jitter=0.25,
            )
            logger.warning(
                "[MIGRATION_APPLY_GATE] %s attempt %d/%d transient failure; "
                "retrying in %.1fs (%s)",
                label,
                attempt - 1,
                _TRANSIENT_RETRY_MAX_ATTEMPTS,
                delay,
                type(last_exc).__name__,
            )
            await asyncio.sleep(delay)
        try:
            return await coro_factory()
        except _MigrationApplyTimeout:
            raise  # a timed-out stage cannot be salvaged by retrying
        except Exception as exc:
            if isinstance(exc, no_retry_types):
                raise  # deterministic — retry cannot help
            last_exc = exc
    # All attempts exhausted — re-raise the last exception.
    raise cast(Exception, last_exc)


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


class _DeterministicMigrationDefect(Exception):
    """A migration defect guaranteed to reproduce on every retry: Python
    ``SyntaxError`` / ``ImportError`` during Alembic's migration import, or a
    class-42 SQL error (syntax_error / invalid_schema_name / undefined_table /
    etc.).  These must hard-block the candidate rather than loop as transient
    infra failures — retrying cannot resolve them."""


class _ServingOnlyImportError(Exception):
    """An ``ImportError`` that references an ``app.*`` symbol — a self-reference
    artifact of replaying candidate migrations against the serving process.

    On the actual deploy target the candidate's own app code is present, so this
    is not a deploy-fatal defect.  Degrades the gate to ``'ok'`` rather than
    hard-blocking the candidate."""


def _looks_like_serving_only_import(exc: ImportError) -> bool:
    """Return True when an ImportError references an app.* module.

    Serving-engine replays run candidate migration files against the OLD app
    code.  A migration that imports a candidate-only symbol (e.g.
    ``from app.field_encryption import require_encryption_active``) will raise
    ImportError in this context, but the same migration succeeds on the real
    deploy target where the candidate's own app code is present.
    """
    if exc.name and (exc.name == "app" or exc.name.startswith("app.")):
        return True
    return bool(re.search(r"'app\.", str(exc)))


class _ScratchDatabaseUnavailable(Exception):
    """The dedicated scratch server could not be reached or provision a DB."""





class _MigrationApplyTimeout(_MigrationApplyError):
    """A stable, stage-specific watchdog expiry."""

    def __init__(self, code: str, timeout_seconds: float):
        self.code = code
        self.timeout_seconds = timeout_seconds
        super().__init__(f"{code}: exceeded {timeout_seconds:g}s wall-clock budget")


def _scratch_connect_args(url: URL) -> dict[str, object]:
    """Driver-level backstops for every connection to the scratch server."""

    if url.drivername != "postgresql+asyncpg":
        return {}
    return {
        "timeout": _SCRATCH_CONNECT_TIMEOUT_SECONDS,
        "command_timeout": _SCRATCH_COMMAND_TIMEOUT_SECONDS,
        "server_settings": {
            "statement_timeout": str(_SCRATCH_STATEMENT_TIMEOUT_MS),
            "lock_timeout": str(_SCRATCH_LOCK_TIMEOUT_MS),
        },
    }


async def _run_timed_stage(
    coro_factory: Callable[[], Awaitable[_T]],
    *,
    timeout_seconds: float,
    timeout_code: str,
) -> _T:
    try:
        return await asyncio.wait_for(coro_factory(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise _MigrationApplyTimeout(timeout_code, timeout_seconds) from exc


def _subprocess_base_env() -> dict[str, str]:
    """Return only non-secret runtime variables required to launch Python."""

    return {
        key: value
        for key in _SUBPROCESS_ENV_ALLOWLIST
        if (value := os.environ.get(key)) is not None
    }


def _bounded_subprocess_detail(
    stderr: bytes,
    stdout: bytes,
    returncode: int | None,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> str:
    """Return a bounded diagnostic with credentials and secret assignments redacted."""

    raw = stderr or stdout
    for value in sorted(filter(None, sensitive_values), key=len, reverse=True):
        raw = raw.replace(value.encode(), b"<redacted>")
    truncated = len(raw) > _SUBPROCESS_DIAGNOSTIC_MAX_BYTES
    text = raw[-_SUBPROCESS_DIAGNOSTIC_MAX_BYTES :].decode(
        errors="replace"
    ).strip()
    text = re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+(?::[^/@\s]*)?@",
        r"\1<redacted>@",
        text,
    )
    text = re.sub(
        r"(?i)\b(password|token|secret|api[_-]?key)\s*([=:])\s*[^\s,;]+",
        r"\1\2<redacted>",
        text,
    )
    text = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
        "<redacted-token>",
        text,
    )
    if truncated:
        text = f"[truncated to last {_SUBPROCESS_DIAGNOSTIC_MAX_BYTES} bytes] {text}"
    return text or f"exit {returncode}"


async def _kill_and_reap_process(proc: asyncio.subprocess.Process) -> None:
    """Kill the supervised child and its POSIX process group, then reap it."""

    if proc.returncode is None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:  # pragma: no cover - production is POSIX
                proc.kill()
        except (ProcessLookupError, OSError):
            if proc.returncode is None:
                with suppress(ProcessLookupError, OSError):
                    proc.kill()
    try:
        await asyncio.wait_for(
            proc.communicate(), timeout=_SUBPROCESS_REAP_TIMEOUT_SECONDS
        )
    except (asyncio.TimeoutError, ProcessLookupError, OSError):
        try:
            await asyncio.wait_for(proc.wait(), timeout=_SUBPROCESS_REAP_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, ProcessLookupError, OSError):
            logger.warning(
                "[MIGRATION_APPLY_GATE] child pid=%s could not be reaped",
                getattr(proc, "pid", None),
            )


async def _communicate_bounded(
    proc: asyncio.subprocess.Process,
    *,
    timeout_seconds: float,
    timeout_code: str,
    input_bytes: bytes | None = None,
) -> tuple[bytes, bytes]:
    try:
        return await asyncio.wait_for(
            proc.communicate(input=input_bytes), timeout=timeout_seconds
        )
    except asyncio.TimeoutError as exc:
        await asyncio.shield(_kill_and_reap_process(proc))
        raise _MigrationApplyTimeout(timeout_code, timeout_seconds) from exc
    except asyncio.CancelledError:
        # Shutdown must not orphan candidate Python or delete its temp directory
        # while the child is still using it.
        await asyncio.shield(_kill_and_reap_process(proc))
        raise


async def _dispose_engine_bounded(engine: AsyncEngine, *, label: str) -> None:
    try:
        await asyncio.wait_for(
            engine.dispose(), timeout=_ENGINE_DISPOSE_TIMEOUT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - teardown is best-effort
        logger.warning(
            "[MIGRATION_APPLY_GATE] %s engine dispose did not complete: %s",
            label,
            exc,
        )

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

    async def _run_read() -> _ProdState | None:
        async with prod_engine.connect() as conn:
            return await conn.run_sync(_read)

    return await _run_timed_stage(
        _run_read,
        timeout_seconds=_SCRATCH_REFLECT_TIMEOUT_SECONDS,
        timeout_code="migration_apply_prod_state_timeout",
    )


def _validate_db_name(name: str) -> None:
    """Validate that a database name contains only alphanumeric characters and
    underscores before embedding it in DDL SQL. Postgres cannot bind params for
    identifiers, so regex validation is the correct defense against SQL injection.
    Raises ``ValueError`` with the invalid name and accepted character set."""
    if not re.match(r"^[a-zA-Z0-9_]+$", name):
        raise ValueError(
            f"Invalid database name {name!r}: must contain only alphanumeric "
            f"characters and underscores"
        )


async def _create_database(base_url: URL, name: str) -> None:
    """Create one isolated database under a bounded connect/DDL budget."""

    _validate_db_name(name)
    engine = create_async_engine(
        base_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
        connect_args=_scratch_connect_args(base_url),
    )

    async def _create() -> None:
        async with engine.connect() as conn:
            await conn.execute(sa.text(f'CREATE DATABASE "{name}"'))


    try:
        await _run_timed_stage(
            _create,
            timeout_seconds=_SCRATCH_CREATE_TIMEOUT_SECONDS,
            timeout_code="migration_apply_scratch_provision_timeout",
        )
    finally:
        await _dispose_engine_bounded(engine, label="scratch provision")


async def _drop_database(base_url: URL, name: str) -> None:
    """Best-effort, bounded teardown of an ephemeral scratch database."""

    _validate_db_name(name)
    engine = create_async_engine(
        base_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
        connect_args=_scratch_connect_args(base_url),
    )

    async def _drop() -> None:
        async with engine.connect() as conn:
            await conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))

    try:
        await _run_timed_stage(
            _drop,
            timeout_seconds=_SCRATCH_CLEANUP_TIMEOUT_SECONDS,
            timeout_code="migration_apply_scratch_cleanup_timeout",
        )
    except Exception as exc:  # noqa: BLE001 - teardown is best-effort
        logger.warning(

            "[MIGRATION_APPLY_GATE] could not drop scratch database %r: %s",
            name,
            exc,
        )
    finally:
        await _dispose_engine_bounded(engine, label="scratch cleanup")


async def _cleanup_ephemeral_database(
    engine: AsyncEngine | None, base_url: URL, name: str
) -> None:
    if engine is not None:
        await _dispose_engine_bounded(engine, label="ephemeral scratch")
    await _drop_database(base_url, name)


@asynccontextmanager
async def ephemeral_postgres_database(base_url: URL, *, db_name: str | None = None):
    """Yield a fresh database and always make one bounded cleanup attempt."""

    name = db_name or f"mig_apply_gate_{uuid.uuid4().hex[:16]}"
    _validate_db_name(name)
    engine: AsyncEngine | None = None
    try:
        # Even a timed-out CREATE is ambiguous: the server may have committed it
        # before the response was lost. The finally block therefore always issues
        # DROP IF EXISTS for this unique name.
        await _create_database(base_url, name)
        scratch_url = base_url.set(database=name)
        engine = create_async_engine(
            scratch_url,
            poolclass=NullPool,
            connect_args=_scratch_connect_args(scratch_url),
        )
        yield engine, name
    finally:
        cleanup_task = asyncio.create_task(
            _cleanup_ephemeral_database(engine, base_url, name)
        )
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            # Cleanup is independently bounded. Finish it before propagating the
            # caller's cancellation so no database or connection is orphaned.
            await asyncio.shield(cleanup_task)
            raise


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

    async def _run_seed() -> None:
        async with scratch_engine.begin() as conn:
            await conn.run_sync(_seed)

    try:
        await _run_timed_stage(
            _run_seed,
            timeout_seconds=_SCRATCH_SEED_TIMEOUT_SECONDS,
            timeout_code="migration_apply_scratch_seed_timeout",
        )
    except _MigrationApplyTimeout:
        raise
    except Exception as exc:
        raise _ScratchDatabaseUnavailable() from exc


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


# Retain one process-wide capacity lock even though Alembic now runs in an
# isolated child process. It prevents overlapping qualifier finalizers from
# multiplying full-chain scratch applies and exhausting the dedicated Postgres
# target. Acquisition and child execution have independent watchdogs, so the
# lock cannot remain held by a wedged candidate.
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


async def _run_migration_apply_subprocess(
    scratch_engine: AsyncEngine,
    script_location: str,
    *,
    timeout_code: str,
) -> None:
    """Run candidate migration Python in a supervised, killable process."""

    scratch_url = scratch_engine.url.render_as_string(hide_password=False)
    env = _subprocess_base_env()
    # Candidate migration modules that consult DATABASE_URL must see only the
    # isolated target. Never expose the production URL to candidate Python.
    env["HOME"] = script_location
    env["DATABASE_URL"] = scratch_url
    env["MIGRATION_APPLY_DATABASE_URL"] = scratch_url
    env["MIGRATION_APPLY_SCRIPT_LOCATION"] = script_location
    backend_root = os.path.dirname(os.path.dirname(__file__))
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _MIGRATION_APPLY_SCRIPT,
        cwd=backend_root,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    stdout, stderr = await _communicate_bounded(
        proc,
        timeout_seconds=_MIGRATION_APPLY_SUBPROCESS_TIMEOUT_SECONDS,
        timeout_code=timeout_code,
    )
    if proc.returncode != 0:
        detail = _bounded_subprocess_detail(
            stderr,
            stdout,
            proc.returncode,
            sensitive_values=(scratch_engine.url.password or "",),
        )

        raise _MigrationApplyError(
            "candidate migration subprocess failed to apply against the "
            f"recorded state: {detail}"
        )


async def _apply_candidate_migrations(
    scratch_engine: AsyncEngine,
    candidate_sources: dict[str, str],
    *,
    timeout_code: str = "migration_apply_candidate_chain_timeout",
) -> None:
    """Write the candidate chain and apply it in a supervised subprocess."""

    try:
        with tempfile.TemporaryDirectory() as tmp:
            await asyncio.to_thread(_write_candidate_files, tmp, candidate_sources)
            lock = _apply_lock()
            acquired = False
            try:
                await _run_timed_stage(
                    lock.acquire,
                    timeout_seconds=_MIGRATION_APPLY_LOCK_TIMEOUT_SECONDS,
                    timeout_code="migration_apply_capacity_timeout",
                )
                acquired = True
                await _run_migration_apply_subprocess(
                    scratch_engine, tmp, timeout_code=timeout_code
                )
            finally:
                if acquired:
                    lock.release()
    except _DeterministicMigrationDefect:
        raise  # already classified; propagate without re-wrapping
    except _ServingOnlyImportError:
        raise  # already classified; propagate without re-wrapping
    except (SyntaxError, ImportError) as exc:
        if isinstance(exc, ImportError) and _looks_like_serving_only_import(exc):
            raise _ServingOnlyImportError(
                f"candidate migration imports a candidate-only app symbol "
                f"(serving-engine artifact, not deploy-fatal): {exc}"
            ) from exc
        raise _DeterministicMigrationDefect(
            f"candidate migration has a deterministic Python defect "
            f"({type(exc).__name__}): {exc}"
        ) from exc
    except sa.exc.ProgrammingError as exc:
        pgcode = getattr(exc.orig, "pgcode", None) or getattr(
            exc.orig, "sqlstate", None
        )
        if pgcode and str(pgcode).startswith("42"):
            raise _DeterministicMigrationDefect(
                f"candidate migration has a deterministic SQL defect "
                f"(SQLSTATE {pgcode}): {exc}"
            ) from exc
        raise _MigrationApplyError(
            f"candidate migration chain failed to apply against production's "
            f"recorded state: {type(exc).__name__}: {exc}"
        ) from exc
    except _MigrationApplyTimeout:
        raise
    except Exception as exc:
        raise _MigrationApplyError(
            "candidate migration chain failed to apply against production's "
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
        await _apply_candidate_migrations(
            ref_engine,
            candidate_sources,
            timeout_code="migration_apply_reference_apply_timeout",
        )

        async def _run_reflect() -> sa.MetaData:
            async with ref_engine.connect() as conn:
                return await conn.run_sync(_reflect)

        return await _run_timed_stage(
            _run_reflect,
            timeout_seconds=_SCRATCH_REFLECT_TIMEOUT_SECONDS,
            timeout_code="migration_apply_reference_reflect_timeout",
        )


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
    """Build candidate code-required metadata in one bounded subprocess."""

    archive = await _download_candidate_repo_archive(
        gh_token, owner, repo, candidate_ref, project_id
    )
    if not archive:
        raise _MigrationApplyError(
            f"could not fetch the candidate code snapshot at {candidate_ref!r}"
        )

    with tempfile.TemporaryDirectory() as tmp:
        env = _subprocess_base_env()
        env["CANDIDATE_WORK_DIR"] = tmp
        env["HOME"] = tmp
        # Candidate imports do not need production data. Scrubbing this value
        # prevents import-time code from accidentally reaching the live database.
        env["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
        backend_root = os.path.dirname(os.path.dirname(__file__))
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _CANDIDATE_METADATA_SCRIPT,
            cwd=backend_root,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        stdout, stderr = await _communicate_bounded(
            proc,
            timeout_seconds=_CANDIDATE_METADATA_TIMEOUT_SECONDS,
            timeout_code="migration_apply_candidate_metadata_timeout",
            input_bytes=archive,
        )
        if proc.returncode != 0:
            detail = _bounded_subprocess_detail(stderr, stdout, proc.returncode)

            raise _MigrationApplyError(
                f"candidate code snapshot at {candidate_ref!r} could not be imported "
                f"to derive required metadata: {detail or f'exit {proc.returncode}'}"
            )
        try:
            shape = json.loads(stdout.decode())
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
    async def _run_reflect() -> list[str]:
        async with scratch_engine.connect() as conn:
            return await conn.run_sync(
                lambda sync_conn: reflect_schema_drift(
                    sync_conn, metadata=required_metadata
                )
            )

    return await _run_timed_stage(
        _run_reflect,
        timeout_seconds=_SCRATCH_REFLECT_TIMEOUT_SECONDS,
        timeout_code="migration_apply_scratch_reflect_timeout",
    )


async def _provision_seed_and_detect_drift(
    scratch_base_url: URL,
    prod_state: _ProdState,
    candidate_sources: dict[str, str],
    required_metadata: sa.MetaData,
) -> list[str]:
    """Provision an isolated scratch DB, seed with production's recorded state,
    apply candidate migrations, and return any schema drift.

    Raises ``_MigrationApplyError`` on seed/apply/collision failure (deferrable).
    Returns an empty list when the post-upgrade schema satisfies all requirements.
    """
    async with ephemeral_postgres_database(scratch_base_url) as (scratch_engine, name):
        # ISOLATION GUARD: never run DDL against the production database.
        if name == scratch_base_url.database:
            raise _MigrationApplyError(
                "scratch database name collided with the scratch server's default database"
            )
        await _seed_scratch(scratch_engine, prod_state)
        await _apply_candidate_migrations(
            scratch_engine,
            candidate_sources,
            timeout_code="migration_apply_replay_apply_timeout",
        )
        return await _reflect_scratch_drift(scratch_engine, required_metadata)


async def _provision_seed_and_detect_drift(
    scratch_base_url: URL,
    prod_state: _ProdState,
    candidate_sources: dict[str, str],
    required_metadata: sa.MetaData,
) -> list[str]:
    """Provision an isolated scratch DB, seed with production's recorded state,
    apply candidate migrations, and return any schema drift.

    Raises ``_MigrationApplyError`` on seed/apply/collision failure (deferrable).
    Returns an empty list when the post-upgrade schema satisfies all requirements.
    """
    async with ephemeral_postgres_database(scratch_base_url) as (scratch_engine, name):
        # ISOLATION GUARD: never run DDL against the production database.
        if name == scratch_base_url.database:
            raise _MigrationApplyError(
                "scratch database name collided with the scratch server's default database"
            )
        await _seed_scratch(scratch_engine, prod_state)
        await _apply_candidate_migrations(scratch_engine, candidate_sources)
        return await _reflect_scratch_drift(scratch_engine, required_metadata)


def _is_migration_source(filename: str) -> bool:
    return (
        filename.endswith(".py")
        and filename != "__init__.py"
        and not filename.startswith("_")
    )


# ---- Data-migration proof gate -----------------------------------------------
#
# A migration is a "data migration" when it performs DML operations (UPDATE,
# INSERT INTO, DELETE FROM) or bulk-inserts directly against table rows.
# Running such migrations against the EMPTY scratch database always succeeds —
# there are no rows to touch — so the scratch-apply verdict cannot prove
# production safety.  Authors must declare a proof via:
#
#   # BLUEPRINT_DATA_MIGRATION_PROOF: <invariants or fixture description>
#
# in the migration file.  Missing proof causes the gate to defer (HOLD) with an
# actionable message rather than returning an uninformative 'ok'.
#
# Detection is heuristic and intentionally conservative (false negatives for
# unusual patterns are preferred over false positives that block structural
# migrations).  Covered patterns:
#   - op.bulk_insert(...)
#   - .execute(sa.update/insert/delete(...))   — ORM DML via execute
#   - .execute("UPDATE ..."/.execute(sa.text("UPDATE ...")/sa.text(f"""UPDATE...)
#
# DML inside DDL bodies (trigger functions, stored procedures) is NOT detected
# because the surrounding context (CREATE FUNCTION / CREATE TRIGGER) is DDL.

_DATA_OP_RE = re.compile(
    r"\bop\.bulk_insert\b"  # op.bulk_insert(table, rows)
    # ORM DML via any .execute() — covers op.execute, conn.execute, bind.execute
    r"|\.execute\s*\(\s*sa\.(?:update|insert|delete)\s*\("
    # Literal DML string in .execute() or .execute(sa.text(...))
    r"|\.execute\s*\(\s*(?:sa\.text\s*\()?\s*[bBfFrRuU]*[\"']{1,3}\s*"
    r"(?:UPDATE\b|INSERT\s+INTO\b|DELETE\s+FROM\b)",
    re.IGNORECASE | re.DOTALL,
)

_DATA_MIGRATION_PROOF_RE = re.compile(
    r"#\s*BLUEPRINT_DATA_MIGRATION_PROOF\s*:",
    re.IGNORECASE,
)


def _migration_has_data_ops(source: str) -> bool:
    """Return True when the migration source contains detectable DML operations."""
    return bool(_DATA_OP_RE.search(source))


def _migration_has_proof_declaration(source: str) -> bool:
    """Return True when the migration carries a BLUEPRINT_DATA_MIGRATION_PROOF marker."""
    return bool(_DATA_MIGRATION_PROOF_RE.search(source))


def _new_migration_sources(
    candidate_sources: dict[str, str],
    prod_revision: str | None,
) -> dict[str, str]:
    """Return candidate migration sources that are not yet applied in production.

    Walks the chain backwards from heads to *prod_revision*, collecting revisions
    that postdate the live stamp.  When *prod_revision* is ``None`` or absent from
    the chain, all migration sources are returned (conservative: check everything).
    """
    all_mig = {k: v for k, v in candidate_sources.items() if _is_migration_source(k)}
    if not prod_revision:
        return all_mig

    chain, _, _ = build_migration_chain(all_mig)
    if prod_revision not in chain:
        return all_mig  # prod revision not in candidate chain — check all conservatively

    new_revs: set[str] = set()
    queue: list[str] = list(find_heads(chain))
    visited: set[str] = set()
    while queue:
        rev = queue.pop()
        if rev in visited:
            continue
        visited.add(rev)
        if rev == prod_revision:
            continue
        new_revs.add(rev)
        parent = chain.get(rev)
        if parent and parent not in visited and parent != prod_revision:
            queue.append(parent)

    result: dict[str, str] = {}
    for filename, source in all_mig.items():
        rev_id, _ = parse_revision_identifiers(source)
        if rev_id in new_revs:
            result[filename] = source
    return result


def _check_data_migration_proof_gate(new_sources: dict[str, str]) -> Verdict | None:
    """Defer when any new migration has DML operations but no proof declaration.

    Data migrations produce correct verdicts on the empty scratch database (no
    rows to touch) but may behave differently on real production data.  Requiring
    an explicit ``# BLUEPRINT_DATA_MIGRATION_PROOF: <description>`` comment forces
    authors to document the invariants or fixtures that validate production safety,
    making the gate's 'ok' verdict meaningful for data-touching migrations.

    Returns ``('defer', reason)`` if proof is missing, ``None`` otherwise.
    """
    offenders = [
        filename
        for filename, source in sorted(new_sources.items())
        if _migration_has_data_ops(source) and not _migration_has_proof_declaration(source)
    ]
    if not offenders:
        return None
    return (
        "defer",
        f"data_migration_proof_missing: {len(offenders)} new migration(s) contain "
        "data operations (DML / bulk-insert) but no "
        "# BLUEPRINT_DATA_MIGRATION_PROOF: declaration — add proof evidence to: "
        + ", ".join(offenders),
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


async def _fetch_candidate_sources_with_retry(
    gh_token: str,
    owner: str,
    repo: str,
    candidate_ref: str,
    project_id: str | None,
) -> dict[str, str] | None:
    """Fetch candidate migration sources with bounded transient retry.

    Unlike :func:`_with_transient_retry` (which retries only on exceptions),
    this wrapper also retries on a ``None`` return — because
    :func:`_fetch_candidate_sources` catches transient exceptions internally
    and converts them to ``None``.  Both exception and ``None`` are treated
    as transient signals and retried up to ``_TRANSIENT_RETRY_MAX_ATTEMPTS``
    times with exponential backoff.

    Returns the sources dict on success, or ``None`` after all retries are
    exhausted — letting the caller's existing ``if not candidate_sources``
    check produce the same ``("defer", ...)`` verdict as before.
    """
    for attempt in range(1, _TRANSIENT_RETRY_MAX_ATTEMPTS + 1):
        try:
            sources = await _fetch_candidate_sources(
                gh_token, owner, repo, candidate_ref, project_id
            )
            if sources is not None:
                return sources
        except Exception:  # noqa: BLE001 — transient fetch errors are retried
            logger.warning(
                "[MIGRATION_APPLY_GATE] fetch candidate sources at %s attempt %d/%d "
                "raised unexpected exception (retrying)",
                candidate_ref,
                attempt,
                _TRANSIENT_RETRY_MAX_ATTEMPTS,
                exc_info=True,
            )
        if attempt < _TRANSIENT_RETRY_MAX_ATTEMPTS:
            delay = exponential_backoff(
                attempt,
                base=_TRANSIENT_RETRY_BASE_DELAY,
                cap=_TRANSIENT_RETRY_MAX_DELAY,
                jitter=0.25,
            )
            logger.warning(
                "[MIGRATION_APPLY_GATE] fetch candidate sources at %s attempt %d/%d "
                "transient failure; retrying in %.1fs",
                candidate_ref,
                attempt,
                _TRANSIENT_RETRY_MAX_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)
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
    scratch_db_url: str | URL | None = None,

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


    *scratch_db_url* is an optional dedicated Postgres target for ephemeral
    database provisioning (``CREATE DATABASE``). When provided, the gate creates
    scratch/reference databases on this server instead of the production
    Postgres connection. In the deployed environment the production connection
    is typically a limited-role pool user that cannot ``CREATE DATABASE``, so
    every candidate would defer with ``migration_apply_check_unavailable``.
    Supplying a scratch-capable target unblocks the gate pre-promotion. When
    ``None`` (default), the gate falls back to *prod_engine*'s URL — the
    correct path for dev/test environments where the Postgres user can create
    databases.

    ISOLATION SAFETY: production is only ever read; the scratch database is a
    freshly-provisioned, separately-named Postgres database, and the orchestrator
    asserts its name differs from production's before any DDL.
    """
    prod_url = prod_engine.url

    scratch_base_url = make_url(scratch_db_url) if scratch_db_url else prod_url

    if not prod_url.get_backend_name().startswith("postgresql"):
        # The renumber-skip class is a Postgres-deploy phenomenon; the gate needs
        # a real migration-applying Postgres scratch target. With a non-Postgres
        # prod bind we cannot reproduce the deploy, so we HOLD (never wave through).
        return (
            "defer",
            f"scratch-DB migration-apply gate requires a PostgreSQL production "
            f"bind; got {prod_url.get_backend_name()!r}",
        )

    prod_identity = (
        prod_url.host,
        prod_url.port,
        prod_url.username,
        prod_url.database,
    )
    scratch_identity = (
        scratch_base_url.host,
        scratch_base_url.port,
        scratch_base_url.username,
        scratch_base_url.database,
    )
    if scratch_identity == prod_identity:
        return (
            "defer",
            "scratch_database_url_unsafe: MIGRATION_APPLY_SCRATCH_DATABASE_URL "
            "must not resolve to the production pool target",
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
        candidate_sources = await _fetch_candidate_sources_with_retry(
            gh_token, owner, repo, candidate_ref, project_id
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
    # Transient DB connection errors are retried; a definitive ``None``
    # (no ``alembic_version`` table or empty stamp) is NOT retried.
    try:
        prod_state = await _with_transient_retry(
            lambda: _read_prod_state(prod_engine),
            label="read production's recorded state",
        )

    except _MigrationApplyTimeout as exc:
        return ("defer", str(exc))
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

    # 3b. Data-migration proof gate: new migrations with DML must declare proof.
    # Runs after reading prod_state (needed to identify which migrations are new)
    # but before the expensive scratch-DB provisioning path.
    _new_sources = _new_migration_sources(candidate_sources, prod_state.revision)
    _data_proof_verdict = _check_data_migration_proof_gate(_new_sources)
    if _data_proof_verdict is not None:
        return _data_proof_verdict

    if required_metadata is None:
        # Derive the candidate's required schema from a clean apply of its OWN
        # migration chain (full schema, candidate-pinned, zero in-process side
        # effects). candidate_sources is guaranteed non-None at this point —
        # either injected or already fetched above (else we returned 'defer').
        try:
            candidate_reference_metadata = await _build_candidate_reference_schema(
                scratch_base_url, candidate_sources
            )
            candidate_required_metadata = await _build_candidate_required_metadata(
                gh_token=gh_token,
                owner=owner,
                repo=repo,
                candidate_ref=candidate_ref,
                project_id=project_id,
            )
        except _ServingOnlyImportError:
            return ("ok", None)
        except _DeterministicMigrationDefect as exc:
            return ("block", str(exc))
        except _ScratchDatabaseUnavailable:
            return (
                "defer",
                "scratch_database_unavailable: MIGRATION_APPLY_SCRATCH_DATABASE_URL "
                "must reach dedicated PostgreSQL infrastructure whose user can CREATE "
                "and DROP databases; restore that prerequisite and retry qualification",
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
    # The provision/seed/apply/drift block catches transient Postgres errors
    # (CONCURRENT DB CREATE races, deadlock on write) with bounded retry.
    try:
        drift = await _with_transient_retry(
            lambda: _provision_seed_and_detect_drift(
                scratch_base_url, prod_state, candidate_sources, required_metadata
            ),
            label="provision-seed-apply-drift",
            no_retry_types=(_DeterministicMigrationDefect, _ServingOnlyImportError),
        )
    except _ServingOnlyImportError:
        return ("ok", None)
    except _DeterministicMigrationDefect as exc:
        return ("block", str(exc))
    except _ScratchDatabaseUnavailable:

        return (
            "defer",
            "scratch_database_unavailable: MIGRATION_APPLY_SCRATCH_DATABASE_URL "
            "must reach dedicated PostgreSQL infrastructure whose user can CREATE "
            "and DROP databases; restore that prerequisite and retry qualification",
        )
    except _MigrationApplyError as exc:
        return ("defer", str(exc))
    except Exception:  # Any provisioning failure holds with a value-safe detail.
        return (
            "defer",
            "scratch_database_unavailable: MIGRATION_APPLY_SCRATCH_DATABASE_URL "
            "could not complete the isolated migration apply; verify dedicated "
            "PostgreSQL availability and CREATE/DROP DATABASE permission",
        )

    if drift:
        return (
            "block",
            f"candidate migrations applied against production's recorded revision "
            f"{prod_state.revision!r} leave the schema missing required object(s) "
            f"(deploy-fatal drift): " + "; ".join(drift),
        )
    return ("ok", None)

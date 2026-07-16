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

import asyncio
import contextlib
import io
import os
import tarfile
import tempfile
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.migration_apply_gate import (
    MigrationApplyScratchPreflight,
    _create_database,
    _drop_database,
    _safe_tar_extractall,
    _validate_db_name,
    ephemeral_postgres_database,
    preflight_migration_apply_scratch,
    run_migration_apply_gate,
)
from app.required_runtime_env import _verify_required_env

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


def test_scratch_prereq_is_typed_optional_and_boot_safe(monkeypatch):
    monkeypatch.setenv("K_REVISION", "app-00042-abc")
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", "test-presence-only")
    monkeypatch.delenv("MIGRATION_APPLY_SCRATCH_DATABASE_URL", raising=False)

    assert _verify_required_env() is None
    result = preflight_migration_apply_scratch(None)
    assert isinstance(result, MigrationApplyScratchPreflight)
    assert result.status == "defer"
    assert result.code == "scratch_database_url_missing"
    assert "MIGRATION_APPLY_SCRATCH_DATABASE_URL" in (result.detail or "")


@pytest.mark.parametrize(
    "raw",
    ["not a URL with secret-value", "sqlite:///secret-value", "postgresql://host"],
)
def test_scratch_preflight_rejects_invalid_values_without_leaking(raw):
    result = preflight_migration_apply_scratch(raw)
    assert result.status == "defer"
    assert result.code == "scratch_database_url_invalid"
    assert "secret-value" not in (result.detail or "")


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
            scratch_db_url=_base_url(),
        )

    assert verdict == "block", detail
    assert "computejob" in detail
    assert "a02" in detail


@requires_postgres
@pytest.mark.asyncio
async def test_derived_reference_schema_blocks_renumber_skip_without_injected_metadata():
    """Same renumber-skip as above, but with NO injected ``required_metadata``: the
    gate must DERIVE the required schema from the candidate's own migration chain
    (applied to a fresh DB) and still ``block`` on the missing ``computejob``.

    This exercises :func:`_build_candidate_reference_schema` end-to-end — proving
    the full deploy-target schema is captured from migrations alone, with no
    in-process exec of candidate model modules."""
    async with ephemeral_postgres_database(_base_url()) as (prod_engine, _name):
        await _seed_prod_sim(
            prod_engine, {"widget": True, "computejob": False}, revision="a02"
        )
        verdict, detail = await run_migration_apply_gate(
            prod_engine=prod_engine,
            candidate_sources=_CANDIDATE_SOURCES,
            scratch_db_url=_base_url(),
            # required_metadata omitted on purpose — derived from candidate chain.
        )

    assert verdict == "block", detail
    assert "computejob" in detail


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
            scratch_db_url=_base_url(),
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
            scratch_db_url=_base_url(),
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
            scratch_db_url=_base_url(),
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
    """A GitHub fetch failure for the candidate's migration sources must HOLD
    (defer), never wave through — a fetch error is not 'no migrations'. Sources
    now come from the repo tarball, so a download failure is the fetch-failure
    path."""
    import app.migration_apply_gate as gate_mod

    async def _no_archive(*_a, **_k):
        return None

    monkeypatch.setattr(gate_mod, "_download_candidate_repo_archive", _no_archive)
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_BASE_DELAY", 0.001)
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_MAX_DELAY", 0.01)
    async with ephemeral_postgres_database(_base_url()) as (prod_engine, _name):
        await _seed_prod_sim(prod_engine, {"widget": True}, revision="a01")
        verdict, detail = await run_migration_apply_gate(
            prod_engine=prod_engine,
            gh_token="t",
            owner="o",
            repo="r",
            candidate_ref="deadbeef",
            required_metadata=_required_metadata(),
            scratch_db_url=_base_url(),
        )

    assert verdict == "defer", detail
    assert "deadbeef" in detail


@pytest.mark.asyncio
async def test_transient_fetch_recovers_on_retry(monkeypatch):
    """A transient GitHub fetch failure (None twice, then success) self-clears on
    internal retry — the gate returns ``ok`` instead of deferring to a fresh
    qualification cycle."""
    import io
    import tarfile

    import app.migration_apply_gate as gate_mod

    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_BASE_DELAY", 0.001)
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_MAX_DELAY", 0.01)

    call_count = 0

    async def _fails_then_succeeds(*_a, **_k):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return None
        # Third call: return a valid minimal archive.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = b"revision = '0001'\ndown_revision = None\n"
            info = tarfile.TarInfo("repo-abc/backend/alembic/versions/0001_init.py")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    monkeypatch.setattr(
        gate_mod, "_download_candidate_repo_archive", _fails_then_succeeds
    )

    # Fast-path the DB operations with mocks.
    prod_meta = sa.MetaData()
    sa.Table("widget", prod_meta, sa.Column("id", sa.Integer, primary_key=True))
    monkeypatch.setattr(
        gate_mod,
        "_read_prod_state",
        AsyncMock(
            return_value=gate_mod._ProdState(revision="0001", metadata=prod_meta)
        ),
    )

    @contextlib.asynccontextmanager
    async def _fake_ephemeral(_base_url, db_name=None):
        yield object(), "scratch-gate"

    monkeypatch.setattr(gate_mod, "ephemeral_postgres_database", _fake_ephemeral)
    monkeypatch.setattr(gate_mod, "_seed_scratch", AsyncMock())
    monkeypatch.setattr(gate_mod, "_apply_candidate_migrations", AsyncMock())
    monkeypatch.setattr(gate_mod, "_reflect_scratch_drift", AsyncMock(return_value=[]))

    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:5432/nope")
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            gh_token="t",
            owner="o",
            repo="r",
            candidate_ref="deadbeef",
            required_metadata=sa.MetaData(),
            scratch_db_url="postgresql+asyncpg://scratch:p@localhost:5432/control",
        )
    finally:
        await engine.dispose()

    assert verdict == "ok", detail
    # Verify the retry mechanism actually fired.
    assert call_count == 3, (
        f"_download_candidate_repo_archive called {call_count} times, expected 3"
    )


@pytest.mark.asyncio
async def test_transient_seed_recovers_on_retry(monkeypatch):
    """A transient seed failure (``_MigrationApplyError`` on first call,
    success on second) self-clears on the internal backoff retry — the gate
    returns ``ok`` without deferring."""
    import app.migration_apply_gate as gate_mod

    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_BASE_DELAY", 0.001)
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_MAX_DELAY", 0.01)

    # Mock the prod state read so we skip to the provision/seed/apply/drift leg.
    prod_meta = sa.MetaData()
    sa.Table("widget", prod_meta, sa.Column("id", sa.Integer, primary_key=True))
    monkeypatch.setattr(
        gate_mod,
        "_read_prod_state",
        AsyncMock(
            return_value=gate_mod._ProdState(revision="a01", metadata=prod_meta)
        ),
    )

    call_count = 0

    async def _flaky_seed(scratch_engine, prod_state):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise gate_mod._MigrationApplyError("transient seed failure")

    @contextlib.asynccontextmanager
    async def _fake_ephemeral(_base_url, db_name=None):
        yield object(), "scratch-gate"

    monkeypatch.setattr(gate_mod, "ephemeral_postgres_database", _fake_ephemeral)
    monkeypatch.setattr(gate_mod, "_seed_scratch", _flaky_seed)
    monkeypatch.setattr(gate_mod, "_apply_candidate_migrations", AsyncMock())
    monkeypatch.setattr(gate_mod, "_reflect_scratch_drift", AsyncMock(return_value=[]))

    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:5432/nope")
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            candidate_sources={"0001_init.py": "revision='0001'\ndown_revision=None\n"},
            required_metadata=sa.MetaData(),
            scratch_db_url="postgresql+asyncpg://scratch:p@localhost:5432/control",
        )
    finally:
        await engine.dispose()

    assert verdict == "ok", detail
    # Seed was called twice — once (failing) and once (succeeding).
    assert call_count == 2


@pytest.mark.asyncio
async def test_candidate_sources_read_from_tarball_no_contents_calls(monkeypatch):
    """Regression guard for the apply-gate fetch storm: migration sources must be
    read from the ONE repo tarball, NOT a contents-API call per file (~215/attempt,
    the budget-draining twin of the identity-gate storm). Asserts zero
    list_versions_at_ref / get_file_text_at_ref calls, plus correct filtering."""
    import io
    import tarfile

    import app.migration_apply_gate as gate_mod

    # In-memory tarball: one top-level repo root, a real migration plus an
    # __init__.py that _is_migration_source must filter out.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, body in [
            (
                "myrepo-abc123/backend/alembic/versions/0001_init.py",
                "revision = '0001'\ndown_revision = None\n",
            ),
            ("myrepo-abc123/backend/alembic/versions/__init__.py", ""),
        ]:
            data = body.encode()
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    monkeypatch.setattr(
        gate_mod,
        "_download_candidate_repo_archive",
        AsyncMock(return_value=buf.getvalue()),
    )
    list_spy = AsyncMock(return_value=[])
    get_spy = AsyncMock(return_value=None)
    monkeypatch.setattr(gate_mod.github, "list_versions_at_ref", list_spy)
    monkeypatch.setattr(gate_mod.github, "get_file_text_at_ref", get_spy)

    sources = await gate_mod._fetch_candidate_sources("t", "o", "r", "ref", None)

    assert sources == {"0001_init.py": "revision = '0001'\ndown_revision = None\n"}
    # The storm calls must never happen — the whole point of the fix.
    assert list_spy.await_count == 0
    assert get_spy.await_count == 0


# --- Concurrency safety (runs everywhere) ----------------------------------


@pytest.mark.asyncio
async def test_concurrent_applies_are_serialized():
    """Alembic's runtime is PROCESS-GLOBAL — the ``op``/``context`` proxies are
    module-level singletons rebound on every ``EnvironmentContext`` enter — so two
    in-process migration applies running concurrently corrupt each other. That is
    the flaky ``KeyError: 'config'`` that false-failed a release qualification to
    ``infra_failure`` on 2026-06-16: the qualifier runs this gate PRE-LOCK, so two
    overlapping finalize attempts genuinely invoke it at the same time.

    ``_apply_candidate_migrations`` must therefore serialize the apply so no two
    are ever live at once. The fake connection models the real apply, whose
    greenlet-bridged DDL yields to the event loop at each statement — the exact
    window a concurrent apply would interleave into and clobber the global proxy.
    Without the lock, ``active`` would exceed 1."""
    import app.migration_apply_gate as gate_mod

    state = {"active": 0, "max": 0}

    class _FakeConn:
        async def run_sync(self, _fn):
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            await asyncio.sleep(0.02)  # yield: a concurrent apply could interleave
            state["active"] -= 1

    @contextlib.asynccontextmanager
    async def _fake_connect():
        yield _FakeConn()

    class _FakeEngine:
        def connect(self):
            return _fake_connect()

    sources = {"0001_init.py": "revision = '0001'\ndown_revision = None\n"}
    await asyncio.gather(
        *(
            gate_mod._apply_candidate_migrations(_FakeEngine(), sources)
            for _ in range(3)
        )
    )

    assert state["max"] == 1, (
        f"concurrent in-process Alembic applies observed (max={state['max']}); the "
        "apply must be serialized to protect Alembic's process-global op/context "
        "proxies"
    )


# --- Fail-safe cases that need no scratch DB (run everywhere) ---------------


@pytest.mark.asyncio
async def test_multi_head_chain_blocks_instead_of_infra_retry():
    """Multiple migration heads (two migration files both with down_revision=None)
    indicate a structurally broken chain — deterministically defective regardless
    of production state or scratch-DB availability. The gate must block immediately
    instead of deferring as an infra_failure and auto-retrying indefinitely.

    This pins the multi-head structural defect symmetrically with the duplicate
    revision case, ensuring both defect classes are independently verified."""
    # Two migration files both with down_revision=None — two separate chain heads.
    multi_head_sources = {
        "a01_widget.py": """
from alembic import op
import sqlalchemy as sa

revision = "a01"
down_revision = None


def upgrade():
    op.create_table("widget", sa.Column("id", sa.Integer, primary_key=True))


def downgrade():
    op.drop_table("widget")
""",
        "a02_gadget.py": """
from alembic import op
import sqlalchemy as sa

revision = "a02"
down_revision = None


def upgrade():
    op.create_table("gadget", sa.Column("id", sa.Integer, primary_key=True))


def downgrade():
    op.drop_table("gadget")
""",
    }

    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:5432/nope")
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            candidate_sources=multi_head_sources,
            required_metadata=_required_metadata(),
            scratch_db_url="postgresql+asyncpg://scratch:p@localhost:5432/control",
        )
    finally:
        await engine.dispose()

    assert verdict == "block", detail
    assert "multiple heads" in detail
    # Both head revisions should be named in the error detail
    assert "a01" in detail
    assert "a02" in detail


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
            scratch_db_url="postgresql+asyncpg://scratch:p@localhost:5432/control",
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
            scratch_db_url="postgresql+asyncpg://scratch:p@localhost:5432/control",
        )
    finally:
        await engine.dispose()

    # Prod-state read fails to connect (no such DB) OR coords are incomplete —
    # either way the verdict must be a HOLD, never 'ok'/'block'.
    assert verdict == "defer", detail


@pytest.mark.asyncio
async def test_required_metadata_derived_from_candidate_reference_not_live_metadata(
    monkeypatch,
):
    """The required schema must be the candidate-pinned reference (its own migration
    chain applied to a fresh DB), NOT the live process's ``SQLModel.metadata``.

    A stale backend/head mismatch (automain advancing while a run is in flight)
    must not grade the candidate against newer process-local metadata. The gate
    derives ``required_metadata`` via :func:`_build_candidate_reference_schema`,
    which reflects a clean apply of the candidate's migrations — no candidate
    Python is exec'd in-process, so no event listeners/mappers leak."""
    import app.migration_apply_gate as gate_mod

    # The schema the candidate's OWN migration chain would produce on a fresh DB.
    candidate_reference_meta = sa.MetaData()
    sa.Table(
        "widget",
        candidate_reference_meta,
        sa.Column("id", sa.Integer, primary_key=True),
    )
    sa.Table(
        "computejob",
        candidate_reference_meta,
        sa.Column("id", sa.Integer, primary_key=True),
    )

    prod_meta = sa.MetaData()
    sa.Table("widget", prod_meta, sa.Column("id", sa.Integer, primary_key=True))

    monkeypatch.setattr(
        gate_mod,
        "_read_prod_state",
        AsyncMock(return_value=gate_mod._ProdState(revision="a01", metadata=prod_meta)),
    )

    @contextlib.asynccontextmanager
    async def _fake_ephemeral(_base_url, db_name=None):
        yield object(), "scratch-gate"

    monkeypatch.setattr(gate_mod, "ephemeral_postgres_database", _fake_ephemeral)
    monkeypatch.setattr(gate_mod, "_seed_scratch", AsyncMock())
    monkeypatch.setattr(gate_mod, "_apply_candidate_migrations", AsyncMock())

    observed = {}

    async def _fake_build_candidate_reference_schema(_base_url, _candidate_sources):
        return candidate_reference_meta

    async def _fake_build_candidate_required_metadata(**_kwargs):
        return candidate_reference_meta

    async def _fake_reflect_scratch_drift(_scratch_engine, required_metadata):
        observed["required_metadata"] = required_metadata
        return []

    monkeypatch.setattr(
        gate_mod,
        "_build_candidate_reference_schema",
        _fake_build_candidate_reference_schema,
    )
    monkeypatch.setattr(
        gate_mod,
        "_build_candidate_required_metadata",
        _fake_build_candidate_required_metadata,
    )
    monkeypatch.setattr(gate_mod, "_reflect_scratch_drift", _fake_reflect_scratch_drift)

    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:5432/nope")
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            gh_token="t",
            owner="o",
            repo="r",
            candidate_ref="candidate-sha",
            project_id="p",
            candidate_sources={"0001_init.py": "revision = '0001'"},
            scratch_db_url="postgresql+asyncpg://scratch:p@localhost:5432/control",
        )
    finally:
        await engine.dispose()

    assert verdict == "ok", detail
    assert observed["required_metadata"] is candidate_reference_meta


@pytest.mark.asyncio
async def test_default_path_blocks_when_candidate_code_needs_missing_migration(
    monkeypatch,
):
    """When ``required_metadata`` is omitted, the gate must still catch a code
    change whose migration was forgotten.

    The candidate-pinned migration reference remains the scratch-drift target,
    but the default path must ALSO compare that reference schema against the
    candidate code's required metadata and hard-block any object the code expects
    that the migration chain never creates."""
    import app.migration_apply_gate as gate_mod

    candidate_reference_meta = sa.MetaData()
    sa.Table(
        "widget",
        candidate_reference_meta,
        sa.Column("id", sa.Integer, primary_key=True),
    )

    candidate_required_meta = sa.MetaData()
    sa.Table(
        "widget", candidate_required_meta, sa.Column("id", sa.Integer, primary_key=True)
    )
    sa.Table(
        "computejob",
        candidate_required_meta,
        sa.Column("id", sa.Integer, primary_key=True),
    )

    prod_meta = sa.MetaData()
    sa.Table("widget", prod_meta, sa.Column("id", sa.Integer, primary_key=True))

    monkeypatch.setattr(
        gate_mod,
        "_read_prod_state",
        AsyncMock(return_value=gate_mod._ProdState(revision="a01", metadata=prod_meta)),
    )
    monkeypatch.setattr(
        gate_mod,
        "_build_candidate_reference_schema",
        AsyncMock(return_value=candidate_reference_meta),
    )
    monkeypatch.setattr(
        gate_mod,
        "_build_candidate_required_metadata",
        AsyncMock(return_value=candidate_required_meta),
    )
    reflect_scratch_drift = AsyncMock(return_value=[])
    monkeypatch.setattr(gate_mod, "_reflect_scratch_drift", reflect_scratch_drift)

    @contextlib.asynccontextmanager
    async def _fake_ephemeral(_base_url, db_name=None):
        yield object(), "scratch-gate"

    monkeypatch.setattr(gate_mod, "ephemeral_postgres_database", _fake_ephemeral)
    monkeypatch.setattr(gate_mod, "_seed_scratch", AsyncMock())
    monkeypatch.setattr(gate_mod, "_apply_candidate_migrations", AsyncMock())

    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:5432/nope")
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            gh_token="t",
            owner="o",
            repo="r",
            candidate_ref="candidate-sha",
            project_id="p",
            candidate_sources={"0001_init.py": "revision = '0001'"},
            scratch_db_url="postgresql+asyncpg://scratch:p@localhost:5432/control",
        )
    finally:
        await engine.dispose()

    assert verdict == "block", detail
    assert "missing migration" in detail
    assert "computejob" in detail
    reflect_scratch_drift.assert_not_called()


# --- _safe_tar_extractall path-traversal guard tests -----------------------


def _make_tar_bytes(members: list[tuple[str, bytes]]) -> bytes:
    """Build an in-memory tar archive with the given (name, content) members."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_safe_tar_extractall_normal_member():
    """Legitimate archive members are extracted correctly."""
    data = _make_tar_bytes([("subdir/file.txt", b"hello")])
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            _safe_tar_extractall(tar, tmp)
        extracted = os.path.join(tmp, "subdir", "file.txt")
        assert os.path.isfile(extracted)
        with open(extracted, "rb") as fh:
            assert fh.read() == b"hello"


def test_safe_tar_extractall_blocks_path_traversal():
    """Members with path-traversal names (../../etc) must be rejected."""
    # Build archive with a traversal member manually — tarfile.open(mode='w')
    # accepts any name, allowing us to craft a malicious archive.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="../../evil.txt")
        payload = b"pwned"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    data = buf.getvalue()

    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            # Both code paths (3.11.4+ filter and legacy manual check) must
            # reject the traversal — either by raising or by not writing the
            # file outside the destination.
            try:
                _safe_tar_extractall(tar, tmp)
            except Exception:
                # Acceptable: filter raised (e.g. tarfile.OutsideDestinationError)
                # or manual check raised ValueError.
                pass
            # Either way, the payload must not have escaped the destination.
            evil_path = os.path.abspath(os.path.join(tmp, "../../evil.txt"))
            assert not os.path.exists(evil_path)


# --- _validate_db_name SQL injection guard tests ---------------------------


def test_validate_db_name_accepts_valid_names():
    """Alphanumeric names with underscores — the only valid characters for a
    Postgres unquoted identifier — must pass validation without error."""
    for name in ("mydb", "my_db", "db123", "a" * 63, "mig_apply_gate_a1b2c3d4e5f6"):
        _validate_db_name(name)  # should not raise


def test_validate_db_name_rejects_sql_metacharacters():
    """Names containing quotes, semicolons, spaces, dashes, or other SQL
    metacharacters must be rejected — these are the SQL injection vectors."""
    for name in (
        'my"db',
        "my'db",
        "my;db",
        "my db",
        "my-db",
        "my.db",
        "my/db",
        "my\ndb",
        "my\tdb",
        "my(db)",
        "my{db}",
        "my[db]",
        "my%db",
        "my&db",
        "my|db",
    ):
        with pytest.raises(ValueError, match="Invalid database name"):
            _validate_db_name(name)


def test_validate_db_name_rejects_empty_string():
    """An empty string does not match the allowed character pattern and must
    be rejected — a blank identifier cannot be a valid database name."""
    with pytest.raises(ValueError, match="Invalid database name"):
        _validate_db_name("")


@pytest.mark.asyncio
async def test_create_database_rejects_invalid_name_without_connecting():
    """``_create_database`` must validate the name BEFORE creating any engine
    or connection — a malicious name raises ``ValueError`` immediately,
    without any database interaction."""
    from sqlalchemy.engine import make_url

    url = make_url("postgresql+asyncpg://u:p@localhost:5432/nope")
    with pytest.raises(ValueError, match="Invalid database name"):
        await _create_database(url, "my;db")

@pytest.mark.asyncio
async def test_drop_database_rejects_invalid_name_without_connecting():
    """``_drop_database`` must validate the name BEFORE creating any engine
    or connection — a malicious name raises ``ValueError`` immediately,
    without any database interaction."""
    from sqlalchemy.engine import make_url

    url = make_url("postgresql+asyncpg://u:p@localhost:5432/nope")
    with pytest.raises(ValueError, match="Invalid database name"):
        await _drop_database(url, "my\"db")


# --- Deterministic defect blocking (runs everywhere) -----------------------


@pytest.mark.asyncio
async def test_repeatable_candidate_migration_failure_blocks(monkeypatch):
    """A migration with a deterministic Python defect (SyntaxError / ImportError)
    must return ``('block', ...)`` instead of ``('defer', ...)`` as
    ``migration_apply_check_unavailable``.

    Repeatable defects cannot be resolved by retrying, so the gate must block
    the candidate on the FIRST attempt without issuing any retry — call count
    must be 1.  This is the primary acceptance test for the deterministic-defect
    gate: it exercises ``run_migration_apply_gate``, ``_DeterministicMigrationDefect``,
    the ``no_retry_types`` fast-exit in ``_with_transient_retry``, and the
    ``except _DeterministicMigrationDefect`` handler that returns ``("block", ...)``.
    """
    import app.migration_apply_gate as gate_mod

    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_BASE_DELAY", 0.001)
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_MAX_DELAY", 0.01)

    prod_meta = sa.MetaData()
    sa.Table("widget", prod_meta, sa.Column("id", sa.Integer, primary_key=True))
    monkeypatch.setattr(
        gate_mod,
        "_read_prod_state",
        AsyncMock(return_value=gate_mod._ProdState(revision="a01", metadata=prod_meta)),
    )

    @contextlib.asynccontextmanager
    async def _fake_ephemeral(_base_url, db_name=None):
        yield object(), "scratch-gate"

    monkeypatch.setattr(gate_mod, "ephemeral_postgres_database", _fake_ephemeral)
    monkeypatch.setattr(gate_mod, "_seed_scratch", AsyncMock())

    apply_call_count = 0

    async def _deterministic_defect_apply(scratch_engine, candidate_sources):
        nonlocal apply_call_count
        apply_call_count += 1
        raise gate_mod._DeterministicMigrationDefect(
            "candidate migration has a deterministic Python defect "
            "(SyntaxError): invalid syntax (<migration_0001>, line 3)"
        )

    monkeypatch.setattr(gate_mod, "_apply_candidate_migrations", _deterministic_defect_apply)

    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:5432/nope")
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            candidate_sources={"0001_bad.py": "revision='0001'\ndown_revision=None\n"},
            required_metadata=sa.MetaData(),
            scratch_db_url=(
                "postgresql+asyncpg://scratch_user:scratch-secret@"
                "scratch-db:5432/control"
            ),
        )
    finally:
        await engine.dispose()

    assert verdict == "block", (
        f"expected 'block' for deterministic defect, got {verdict!r}: {detail}"
    )
    assert detail is not None and "SyntaxError" in detail
    assert apply_call_count == 1, (
        f"deterministic defect retried {apply_call_count} time(s); must block on first attempt"
    )


@pytest.mark.asyncio
async def test_apply_candidate_migrations_classifies_syntax_error(monkeypatch):
    """``SyntaxError`` raised during Alembic migration import must be classified
    as ``_DeterministicMigrationDefect``, not wrapped as ``_MigrationApplyError``."""
    import app.migration_apply_gate as gate_mod

    class _FakeConn:
        async def run_sync(self, fn):
            fn(self)  # propagates SyntaxError raised by the patched upgrader

    @contextlib.asynccontextmanager
    async def _fake_connect():
        yield _FakeConn()

    class _FakeEngine:
        def connect(self):
            return _fake_connect()

    def _syntax_error_upgrade(sync_conn, script_location):
        raise SyntaxError("invalid syntax", ("<migration_0001>", 3, 1, "def upgrade(\n"))

    monkeypatch.setattr(gate_mod, "_reconcile_then_upgrade", _syntax_error_upgrade)

    with pytest.raises(gate_mod._DeterministicMigrationDefect) as exc_info:
        await gate_mod._apply_candidate_migrations(
            _FakeEngine(),
            {"0001_bad.py": "revision='0001'\ndown_revision=None\n"},
        )

    assert "SyntaxError" in str(exc_info.value)


@pytest.mark.asyncio
async def test_apply_candidate_migrations_classifies_import_error(monkeypatch):
    """``ImportError`` raised during Alembic migration import must be classified
    as ``_DeterministicMigrationDefect``."""
    import app.migration_apply_gate as gate_mod

    class _FakeConn:
        async def run_sync(self, fn):
            fn(self)

    @contextlib.asynccontextmanager
    async def _fake_connect():
        yield _FakeConn()

    class _FakeEngine:
        def connect(self):
            return _fake_connect()

    def _import_error_upgrade(sync_conn, script_location):
        raise ImportError("No module named 'nonexistent_dep_xyz'")

    monkeypatch.setattr(gate_mod, "_reconcile_then_upgrade", _import_error_upgrade)

    with pytest.raises(gate_mod._DeterministicMigrationDefect) as exc_info:
        await gate_mod._apply_candidate_migrations(
            _FakeEngine(),
            {"0001_bad.py": "revision='0001'\ndown_revision=None\n"},
        )

    assert "ImportError" in str(exc_info.value)


@pytest.mark.asyncio
async def test_apply_candidate_migrations_classifies_class42_sql_error(monkeypatch):
    """A class-42 ``ProgrammingError`` (SQL syntax / undefined table / invalid
    schema) must be classified as ``_DeterministicMigrationDefect``."""
    import app.migration_apply_gate as gate_mod

    class _FakeConn:
        async def run_sync(self, fn):
            fn(self)

    @contextlib.asynccontextmanager
    async def _fake_connect():
        yield _FakeConn()

    class _FakeEngine:
        def connect(self):
            return _fake_connect()

    class _FakeOrig(Exception):
        pgcode = "42601"  # syntax_error

    def _sql_error_upgrade(sync_conn, script_location):
        orig = _FakeOrig("syntax error at or near 'BORK'")
        raise sa.exc.ProgrammingError(
            "syntax error at or near 'BORK'", {}, orig
        )

    monkeypatch.setattr(gate_mod, "_reconcile_then_upgrade", _sql_error_upgrade)

    with pytest.raises(gate_mod._DeterministicMigrationDefect) as exc_info:
        await gate_mod._apply_candidate_migrations(
            _FakeEngine(),
            {"0001_bad.py": "revision='0001'\ndown_revision=None\n"},
        )

    assert "42601" in str(exc_info.value)


@pytest.mark.asyncio
async def test_apply_candidate_migrations_non_class42_sql_error_defers(monkeypatch):
    """A non-class-42 ``ProgrammingError`` (e.g. lock timeout, deadlock) must
    remain a ``_MigrationApplyError`` so it defers (transient, may recover)."""
    import app.migration_apply_gate as gate_mod

    class _FakeConn:
        async def run_sync(self, fn):
            fn(self)

    @contextlib.asynccontextmanager
    async def _fake_connect():
        yield _FakeConn()

    class _FakeEngine:
        def connect(self):
            return _fake_connect()

    class _FakeOrig(Exception):
        pgcode = "40P01"  # deadlock_detected (class 40 — not deterministic)

    def _deadlock_upgrade(sync_conn, script_location):
        orig = _FakeOrig("deadlock detected")
        raise sa.exc.ProgrammingError("deadlock detected", {}, orig)

    monkeypatch.setattr(gate_mod, "_reconcile_then_upgrade", _deadlock_upgrade)

    with pytest.raises(gate_mod._MigrationApplyError):
        await gate_mod._apply_candidate_migrations(
            _FakeEngine(),
            {"0001_ok.py": "revision='0001'\ndown_revision=None\n"},
        )


# --- scratch_db_url routing -------------------------------------------------


@pytest.mark.asyncio
async def test_scratch_db_url_routes_scratch_provisioning_to_dedicated_target(
    monkeypatch,
):
    """When ``scratch_db_url`` is provided, the gate must provision the scratch
    and reference databases on the dedicated target, NOT the production URL.

    The deployed production Postgres user typically cannot ``CREATE DATABASE``,
    so every candidate would defer with ``migration_apply_check_unavailable``.
    Providing a scratch-capable target unblocks the gate pre-promotion.
    """
    import app.migration_apply_gate as gate_mod
    from sqlalchemy.engine import make_url

    scratch_target = "postgresql+asyncpg://scratch_user:p@scratch-host:5432/postgres"
    prod_url = make_url("postgresql+asyncpg://prod_user:p@prod-db:5432/proddb")

    prod_meta = sa.MetaData()
    sa.Table("widget", prod_meta, sa.Column("id", sa.Integer, primary_key=True))

    monkeypatch.setattr(
        gate_mod,
        "_read_prod_state",
        AsyncMock(return_value=gate_mod._ProdState(revision="a01", metadata=prod_meta)),
    )

    # Capture the base_url passed to _build_candidate_reference_schema and
    # _provision_seed_and_detect_drift to verify the scratch target is used.
    observed: dict[str, object] = {}

    async def _capture_build_candidate_reference_schema(
        base_url, candidate_sources
    ):
        observed["build_reference_base_url"] = base_url
        return prod_meta

    async def _capture_provision_seed_and_detect_drift(
        scratch_base_url, prod_state, candidate_sources, required_metadata
    ):
        observed["provision_base_url"] = scratch_base_url
        return []

    monkeypatch.setattr(
        gate_mod,
        "_build_candidate_reference_schema",
        _capture_build_candidate_reference_schema,
    )
    monkeypatch.setattr(
        gate_mod,
        "_build_candidate_required_metadata",
        AsyncMock(return_value=prod_meta),
    )
    monkeypatch.setattr(
        gate_mod,
        "_provision_seed_and_detect_drift",
        _capture_provision_seed_and_detect_drift,
    )

    # Create an engine whose URL differs from the scratch target to prove the
    # gate does NOT fall back to prod_url when scratch_db_url is given.
    engine = create_async_engine(str(prod_url))
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            candidate_sources={"0001_init.py": "revision='0001'\ndown_revision=None\n"},
            scratch_db_url=scratch_target,
        )
    finally:
        await engine.dispose()

    assert verdict == "ok", detail

    # Both internal functions must receive the scratch target URL, not prod.
    # Compare URL components individually: str(URL) redacts passwords to "***",
    # so we check host/port/database which are stable across password redaction.
    expected = make_url(scratch_target)
    build_ref = observed.get("build_reference_base_url")
    assert build_ref is not None, (
        "_build_candidate_reference_schema was not called"
    )
    assert build_ref.host == expected.host, (
        f"_build_candidate_reference_schema host {build_ref.host!r} != {expected.host!r}"
    )
    assert build_ref.database == expected.database, (
        f"_build_candidate_reference_schema database {build_ref.database!r} != {expected.database!r}"
    )

    provision_ref = observed.get("provision_base_url")
    assert provision_ref is not None, (
        "_provision_seed_and_detect_drift was not called"
    )
    assert provision_ref.host == expected.host, (
        f"_provision_seed_and_detect_drift host {provision_ref.host!r} != {expected.host!r}"
    )
    assert provision_ref.database == expected.database, (
        f"_provision_seed_and_detect_drift database {provision_ref.database!r} != {expected.database!r}"
    )


@pytest.mark.asyncio
async def test_missing_scratch_url_defers_without_production_io(monkeypatch):
    """Missing scratch config defers before production reads or any DDL."""
    import app.migration_apply_gate as gate_mod

    read_prod = AsyncMock()
    fetch_sources = AsyncMock()
    provision = AsyncMock()
    monkeypatch.setattr(gate_mod, "_read_prod_state", read_prod)
    monkeypatch.setattr(gate_mod, "_fetch_candidate_sources_with_retry", fetch_sources)
    monkeypatch.setattr(gate_mod, "_provision_seed_and_detect_drift", provision)

    engine = create_async_engine(
        "postgresql+asyncpg://prod_user:secret@prod-db:5432/proddb"
    )
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            candidate_sources={"0001_init.py": "revision='0001'\ndown_revision=None\n"},
        )
    finally:
        await engine.dispose()

    assert verdict == "defer"
    assert "scratch_database_url_missing" in (detail or "")
    assert "secret" not in (detail or "")
    read_prod.assert_not_awaited()
    fetch_sources.assert_not_awaited()
    provision.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_scratch_target_returns_sanitized_typed_defer(monkeypatch):
    import app.migration_apply_gate as gate_mod

    metadata = sa.MetaData()
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_BASE_DELAY", 0)
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_MAX_DELAY", 0)
    monkeypatch.setattr(
        gate_mod,
        "_read_prod_state",
        AsyncMock(return_value=gate_mod._ProdState(revision="a01", metadata=metadata)),
    )
    monkeypatch.setattr(
        gate_mod, "_provision_seed_and_detect_drift", AsyncMock(side_effect=OSError())
    )

    engine = create_async_engine(
        "postgresql+asyncpg://prod_user:prod-secret@prod-db:5432/proddb"
    )
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            candidate_sources={"0001_init.py": "revision='0001'\ndown_revision=None\n"},
            required_metadata=metadata,
            scratch_db_url=(
                "postgresql+asyncpg://scratch_user:scratch-secret@"
                "scratch-db:5432/control"
            ),
        )
    finally:
        await engine.dispose()

    assert verdict == "defer"
    assert "scratch_database_unavailable" in (detail or "")
    assert "MIGRATION_APPLY_SCRATCH_DATABASE_URL" in (detail or "")
    assert "prod-secret" not in (detail or "")
    assert "scratch-secret" not in (detail or "")


@pytest.mark.asyncio
async def test_reference_build_scratch_unavailable_returns_typed_defer(monkeypatch):
    """_ScratchDatabaseUnavailable from the reference-build path must produce the
    same typed, sanitized defer as the replay path — not the generic candidate-
    reference message which does not name the prerequisite."""
    import app.migration_apply_gate as gate_mod

    metadata = sa.MetaData()
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_BASE_DELAY", 0)
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_MAX_DELAY", 0)
    monkeypatch.setattr(
        gate_mod,
        "_read_prod_state",
        AsyncMock(return_value=gate_mod._ProdState(revision="a01", metadata=metadata)),
    )
    monkeypatch.setattr(
        gate_mod,
        "_build_candidate_reference_schema",
        AsyncMock(side_effect=gate_mod._ScratchDatabaseUnavailable()),
    )

    engine = create_async_engine(
        "postgresql+asyncpg://prod_user:prod-secret@prod-db:5432/proddb"
    )
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            candidate_sources={"0001_init.py": "revision='0001'\ndown_revision=None\n"},
            # required_metadata omitted → gate enters reference-build path
            scratch_db_url=(
                "postgresql+asyncpg://scratch_user:scratch-secret@"
                "scratch-db:5432/control"
            ),
        )
    finally:
        await engine.dispose()

    assert verdict == "defer"
    assert "scratch_database_unavailable" in (detail or "")
    assert "MIGRATION_APPLY_SCRATCH_DATABASE_URL" in (detail or "")
    assert "prod-secret" not in (detail or "")
    assert "scratch-secret" not in (detail or "")


@pytest.mark.asyncio
async def test_replay_seed_scratch_unavailable_returns_typed_defer(monkeypatch):
    """_ScratchDatabaseUnavailable from _seed_scratch (the real wrapper, not a
    direct mock of _provision_seed_and_detect_drift) must produce the typed defer.
    Seeding failures are infra failures; raw details must not be returned."""
    import contextlib

    import app.migration_apply_gate as gate_mod

    metadata = sa.MetaData()
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_BASE_DELAY", 0)
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_MAX_DELAY", 0)
    monkeypatch.setattr(
        gate_mod,
        "_read_prod_state",
        AsyncMock(return_value=gate_mod._ProdState(revision="a01", metadata=metadata)),
    )

    @contextlib.asynccontextmanager
    async def _fake_ephemeral(_base_url, db_name=None):
        yield object(), "scratch-replay"

    monkeypatch.setattr(gate_mod, "ephemeral_postgres_database", _fake_ephemeral)
    monkeypatch.setattr(
        gate_mod,
        "_seed_scratch",
        AsyncMock(side_effect=gate_mod._ScratchDatabaseUnavailable()),
    )

    engine = create_async_engine(
        "postgresql+asyncpg://prod_user:prod-secret@prod-db:5432/proddb"
    )
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            candidate_sources={"0001_init.py": "revision='0001'\ndown_revision=None\n"},
            required_metadata=metadata,
            scratch_db_url=(
                "postgresql+asyncpg://scratch_user:scratch-secret@"
                "scratch-db:5432/control"
            ),
        )
    finally:
        await engine.dispose()

    assert verdict == "defer"
    assert "scratch_database_unavailable" in (detail or "")
    assert "MIGRATION_APPLY_SCRATCH_DATABASE_URL" in (detail or "")
    assert "prod-secret" not in (detail or "")
    assert "scratch-secret" not in (detail or "")

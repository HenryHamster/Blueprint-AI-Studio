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
    _check_data_migration_proof_gate,
    _create_database,
    _drop_database,
    _looks_like_serving_only_import,
    _migration_has_data_ops,
    _migration_has_proof_declaration,
    _new_migration_sources,
    _safe_tar_extractall,
    _validate_db_name,
    ephemeral_postgres_database,
    preflight_migration_apply_scratch,
    run_migration_apply_gate,
)
from app.required_runtime_env import (
    QUALIFICATION_ENV,
    _verify_required_env,
    missing_qualification_env,
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


def test_scratch_prereq_is_typed_optional_and_boot_safe(monkeypatch):
    monkeypatch.setenv("K_REVISION", "app-00042-abc")
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", "test-presence-only")
    monkeypatch.delenv("MIGRATION_APPLY_SCRATCH_DATABASE_URL", raising=False)

    # Boot must succeed even without the scratch URL — the gate degrades to
    # 'defer', it does NOT prevent the app from starting.
    assert _verify_required_env() is None

    # The qualification env manifest declares it as a cloud_run-scoped
    # qualification prerequisite and surfaces it as missing when absent.
    assert any(
        req.name == "MIGRATION_APPLY_SCRATCH_DATABASE_URL" for req in QUALIFICATION_ENV
    )
    missing = missing_qualification_env()
    assert any(
        req.name == "MIGRATION_APPLY_SCRATCH_DATABASE_URL" for req in missing
    ), "MIGRATION_APPLY_SCRATCH_DATABASE_URL must appear in missing_qualification_env() when absent in Cloud Run"

    result = preflight_migration_apply_scratch(None)
    assert isinstance(result, MigrationApplyScratchPreflight)
    assert result.status == "absent"
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
async def test_concurrent_applies_are_serialized(monkeypatch):
    """The retained capacity lock admits only one migration subprocess at a time."""

    import app.migration_apply_gate as gate_mod

    state = {"active": 0, "max": 0}
    first_entered = asyncio.Event()
    release = asyncio.Event()

    async def _fake_runner(_engine, _script_location, *, timeout_code):
        del timeout_code
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        first_entered.set()
        await release.wait()
        state["active"] -= 1

    monkeypatch.setattr(
        gate_mod, "_run_migration_apply_subprocess", _fake_runner
    )

    class _FakeEngine:
        pass

    sources = {
        "0001_init.py": """revision = '0001'
down_revision = None
"""
    }
    tasks = [
        asyncio.create_task(
            gate_mod._apply_candidate_migrations(_FakeEngine(), sources)
        )
        for _ in range(3)
    ]
    await first_entered.wait()
    await asyncio.sleep(0)
    assert state["max"] == 1
    release.set()
    await asyncio.gather(*tasks)
    assert state["max"] == 1


class _HangingProcess:
    def __init__(self):
        self.pid = 424242
        self.returncode = None
        self.entered = asyncio.Event()
        self.communicate_calls = 0
        self.killed = False

    async def communicate(self, input=None):
        del input
        self.communicate_calls += 1
        if self.communicate_calls == 1:
            self.entered.set()
            await asyncio.Event().wait()
        return b"", b""

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_candidate_metadata_timeout_kills_and_reaps(monkeypatch):
    import app.migration_apply_gate as gate_mod

    proc = _HangingProcess()
    monkeypatch.setattr(gate_mod, "_CANDIDATE_METADATA_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        gate_mod,
        "_download_candidate_repo_archive",
        AsyncMock(return_value=b"archive"),
    )
    monkeypatch.setattr(
        gate_mod.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proc),
    )
    monkeypatch.setattr(
        gate_mod.os,
        "killpg",
        lambda _pid, _sig: proc.kill(),
        raising=False,
    )

    with pytest.raises(
        gate_mod._MigrationApplyTimeout,
        match="migration_apply_candidate_metadata_timeout",
    ):
        await gate_mod._build_candidate_required_metadata_from_snapshot(
            "token", "owner", "repo", "sha", None
        )

    assert proc.killed is True
    assert proc.communicate_calls == 2
    assert proc.returncode == -9


@pytest.mark.asyncio
async def test_candidate_metadata_subprocess_happy_path(monkeypatch):
    import app.migration_apply_gate as gate_mod

    archive = _make_tar_bytes(
        [
            ("repo/backend/app/__init__.py", b""),
            (
                "repo/backend/app/models.py",
                b"""from sqlmodel import Field, SQLModel
class Widget(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
""",
            ),
        ]
    )
    monkeypatch.setattr(
        gate_mod,
        "_download_candidate_repo_archive",
        AsyncMock(return_value=archive),
    )

    metadata = await gate_mod._build_candidate_required_metadata_from_snapshot(
        "token", "owner", "repo", "sha", None
    )

    assert "widget" in metadata.tables
    assert list(metadata.tables["widget"].columns.keys()) == ["id"]

@pytest.mark.asyncio
async def test_subprocess_cancellation_kills_and_reaps(monkeypatch):
    import app.migration_apply_gate as gate_mod

    proc = _HangingProcess()
    monkeypatch.setattr(
        gate_mod.os,
        "killpg",
        lambda _pid, _sig: proc.kill(),
        raising=False,
    )
    task = asyncio.create_task(
        gate_mod._communicate_bounded(
            proc,
            timeout_seconds=60,
            timeout_code="migration_apply_test_timeout",
        )
    )
    await proc.entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert proc.killed is True
    assert proc.communicate_calls == 2


@pytest.mark.asyncio
async def test_create_timeout_still_attempts_drop(monkeypatch):
    import app.migration_apply_gate as gate_mod

    timeout = gate_mod._MigrationApplyTimeout(
        "migration_apply_scratch_provision_timeout", 0.01
    )
    monkeypatch.setattr(
        gate_mod, "_create_database", AsyncMock(side_effect=timeout)
    )
    drop = AsyncMock()
    monkeypatch.setattr(gate_mod, "_drop_database", drop)

    with pytest.raises(gate_mod._MigrationApplyTimeout):
        async with gate_mod.ephemeral_postgres_database(
            make_url("postgresql+asyncpg://u:p@localhost/postgres")
        ):
            raise AssertionError("unreachable")

    drop.assert_awaited_once()


@pytest.mark.asyncio
async def test_ephemeral_cleanup_finishes_before_cancellation_propagates(
    monkeypatch,
):
    import app.migration_apply_gate as gate_mod

    body_entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    monkeypatch.setattr(gate_mod, "_create_database", AsyncMock())
    monkeypatch.setattr(
        gate_mod, "create_async_engine", lambda *_args, **_kwargs: object()
    )

    async def _cleanup(_engine, _base_url, _name):
        cleanup_started.set()
        await allow_cleanup.wait()
        cleanup_finished.set()

    monkeypatch.setattr(gate_mod, "_cleanup_ephemeral_database", _cleanup)

    async def _use_database():
        async with gate_mod.ephemeral_postgres_database(
            make_url("postgresql+asyncpg://u:p@localhost/postgres")
        ):
            body_entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(_use_database())
    await body_entered.wait()
    task.cancel()
    await cleanup_started.wait()
    assert not task.done()
    allow_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup_finished.is_set()

@pytest.mark.asyncio
async def test_timeout_is_not_retried_internally():
    import app.migration_apply_gate as gate_mod

    calls = 0

    async def _timeout():
        nonlocal calls
        calls += 1
        raise gate_mod._MigrationApplyTimeout(
            "migration_apply_replay_apply_timeout", 0.01
        )

    with pytest.raises(gate_mod._MigrationApplyTimeout):
        await gate_mod._with_transient_retry(_timeout, label="test timeout")

    assert calls == 1

@pytest.mark.asyncio
async def test_apply_timeout_releases_capacity_lock(monkeypatch):
    import app.migration_apply_gate as gate_mod

    calls = 0

    async def _runner(_engine, _script_location, *, timeout_code):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise gate_mod._MigrationApplyTimeout(timeout_code, 0.01)

    monkeypatch.setattr(
        gate_mod, "_run_migration_apply_subprocess", _runner
    )

    class _FakeEngine:
        pass

    sources = {"0001_init.py": "revision = '0001'"}
    with pytest.raises(gate_mod._MigrationApplyTimeout):
        await gate_mod._apply_candidate_migrations(_FakeEngine(), sources)

    # A timeout must release the capacity lock before temp-file cleanup. If it
    # did not, this second call would wait for the 240-second lock watchdog.
    await gate_mod._apply_candidate_migrations(_FakeEngine(), sources)
    assert calls == 2


@pytest.mark.asyncio
async def test_apply_subprocess_receives_only_scratch_database_url(monkeypatch):
    import app.migration_apply_gate as gate_mod

    monkeypatch.setenv("BLUEPRINT_TEST_SECRET", "must-not-leak")
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", "must-not-leak-either")
    observed = {}

    class _CompletedProcess:
        returncode = 0
        pid = 123

        async def communicate(self, input=None):
            del input
            return b"", b""

    async def _spawn(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return _CompletedProcess()

    monkeypatch.setattr(gate_mod.asyncio, "create_subprocess_exec", _spawn)

    class _FakeEngine:
        url = make_url(
            "postgresql+asyncpg://scratch_user:scratch_secret@"
            "scratch-host:5432/mig_apply_gate_test"
        )

    await gate_mod._run_migration_apply_subprocess(
        _FakeEngine(),
        "candidate-versions",
        timeout_code="migration_apply_replay_apply_timeout",
    )

    env = observed["kwargs"]["env"]
    expected = _FakeEngine.url.render_as_string(hide_password=False)
    assert env["DATABASE_URL"] == expected
    assert env["MIGRATION_APPLY_DATABASE_URL"] == expected
    assert "prod" not in env["DATABASE_URL"]
    assert "BLUEPRINT_TEST_SECRET" not in env
    # An ephemeral synthetic Fernet key is injected so fail-closed migrations
    # (e.g. 0609) can execute in the scratch subprocess.  It must be present
    # but must NOT equal the production value set above.
    assert "GITHUB_PAT_ENCRYPTION_KEY" in env
    assert env["GITHUB_PAT_ENCRYPTION_KEY"] != "must-not-leak-either"
    assert observed["kwargs"]["start_new_session"] == (os.name == "posix")


def test_subprocess_diagnostic_is_bounded_and_redacted():
    import app.migration_apply_gate as gate_mod

    password = "super-secret-password"
    token = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    stderr = (
        b"x" * (gate_mod._SUBPROCESS_DIAGNOSTIC_MAX_BYTES + 100)
        + f" postgresql+asyncpg://user:{password}@host/db bearer {token}".encode()
    )

    detail = gate_mod._bounded_subprocess_detail(
        stderr, b"", 1, sensitive_values=(password,)
    )

    assert password not in detail
    assert token not in detail
    assert "<redacted>" in detail
    assert "<redacted-token>" in detail
    assert detail.startswith("[truncated to last ")
    assert len(detail.encode()) < gate_mod._SUBPROCESS_DIAGNOSTIC_MAX_BYTES + 100


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_legacy_tar_fallback_rejects_link_escape(
    monkeypatch, member_type
):
    import app.migration_apply_gate as gate_mod

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        link = tarfile.TarInfo("repo/link")
        link.type = member_type
        link.linkname = "../../outside"
        tar.addfile(link)

    monkeypatch.setattr(gate_mod.sys, "version_info", (3, 11, 3))
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(buf.getvalue()), mode="r:") as tar:
            with pytest.raises(ValueError, match="link member"):
                gate_mod._safe_tar_extractall(tar, tmp)

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
    """``SyntaxError`` escaping the apply subprocess must be classified
    as ``_DeterministicMigrationDefect``, not wrapped as ``_MigrationApplyError``."""
    import app.migration_apply_gate as gate_mod

    async def _raise_syntax_error(scratch_engine, script_location, *, timeout_code):
        raise SyntaxError("invalid syntax", ("<migration_0001>", 3, 1, "def upgrade(\n"))

    monkeypatch.setattr(gate_mod, "_run_migration_apply_subprocess", _raise_syntax_error)

    engine = create_async_engine(
        "postgresql+asyncpg://scratch_user:s@scratch-db:5432/scratch"
    )
    try:
        with pytest.raises(gate_mod._DeterministicMigrationDefect) as exc_info:
            await gate_mod._apply_candidate_migrations(
                engine,
                {"0001_bad.py": "revision='0001'\ndown_revision=None\n"},
            )
    finally:
        await engine.dispose()

    assert "SyntaxError" in str(exc_info.value)


@pytest.mark.asyncio
async def test_apply_candidate_migrations_classifies_import_error(monkeypatch):
    """``ImportError`` (non-app.*) escaping the apply subprocess must be classified
    as ``_DeterministicMigrationDefect``."""
    import app.migration_apply_gate as gate_mod

    async def _raise_import_error(scratch_engine, script_location, *, timeout_code):
        raise ImportError("No module named 'nonexistent_dep_xyz'")

    monkeypatch.setattr(gate_mod, "_run_migration_apply_subprocess", _raise_import_error)

    engine = create_async_engine(
        "postgresql+asyncpg://scratch_user:s@scratch-db:5432/scratch"
    )
    try:
        with pytest.raises(gate_mod._DeterministicMigrationDefect) as exc_info:
            await gate_mod._apply_candidate_migrations(
                engine,
                {"0001_bad.py": "revision='0001'\ndown_revision=None\n"},
            )
    finally:
        await engine.dispose()

    assert "ImportError" in str(exc_info.value)


# --- _looks_like_serving_only_import -----------------------------------------


def test_looks_like_serving_only_import_matched_by_name_attribute():
    """``_looks_like_serving_only_import`` returns True when the ``ImportError``
    has a ``.name`` attribute starting with ``"app."`` — the ``ModuleNotFoundError``
    pattern when a candidate-side app module is absent from the serving engine."""
    exc = ImportError("No module named 'app.field_encryption'")
    exc.name = "app.field_encryption"  # type: ignore[attr-defined]
    assert _looks_like_serving_only_import(exc) is True


def test_looks_like_serving_only_import_matched_by_from_in_message():
    """``_looks_like_serving_only_import`` returns True when the error message
    contains ``"from 'app."`` — the ``ImportError`` pattern when a symbol from
    an existing ``app.*`` module cannot be found in the serving engine."""
    exc = ImportError(
        "cannot import name 'require_encryption_active' "
        "from 'app.field_encryption'"
    )
    assert _looks_like_serving_only_import(exc) is True


def test_looks_like_serving_only_import_matched_by_no_module_in_message():
    """``_looks_like_serving_only_import`` returns True when the error message
    contains ``"No module named 'app."`` — the ``ModuleNotFoundError`` pattern
    when a candidate-side app submodule is entirely absent from the serving engine
    and ``.name`` attribute is absent."""
    exc = ImportError("No module named 'app.new_compute_module'")
    assert _looks_like_serving_only_import(exc) is True


def test_looks_like_serving_only_import_rejects_third_party_import():
    """A non-app third-party ``ImportError`` (e.g. ``nonexistent_package``) must
    return False — these are genuine dependency defects, not serving-only artifacts."""
    exc = ImportError("No module named 'nonexistent_package'")
    assert _looks_like_serving_only_import(exc) is False


def test_looks_like_serving_only_import_rejects_stdlib_module():
    """An ``ImportError`` for a stdlib module that happens to exist only on newer
    Python must return False — it is NOT an ``app.*`` serving-only artifact."""
    exc = ImportError("No module named 'tomllib'")
    assert _looks_like_serving_only_import(exc) is False


# --- Serving-only ImportError degrade (unit: _apply_candidate_migrations) -----


@pytest.mark.asyncio
async def test_apply_candidate_migrations_classifies_app_import_as_serving_only(
    monkeypatch,
):
    """An ``ImportError`` for a candidate-side ``app.*`` module during migration
    import must be classified as ``_ServingOnlyImportError`` (degrade), not
    ``_DeterministicMigrationDefect`` (block)."""
    import app.migration_apply_gate as gate_mod

    async def _app_import_error_apply(scratch_engine, script_location, *, timeout_code):
        raise ImportError(
            "cannot import name 'require_encryption_active' "
            "from 'app.field_encryption'"
        )

    monkeypatch.setattr(
        gate_mod, "_run_migration_apply_subprocess", _app_import_error_apply
    )

    with pytest.raises(gate_mod._ServingOnlyImportError) as exc_info:
        await gate_mod._apply_candidate_migrations(
            object(),
            {"0001_app_dep.py": "revision='0001'\ndown_revision=None\n"},
        )

    assert "app.field_encryption" in str(exc_info.value)


# --- Deterministic defect blocking (runs everywhere) ---------------------------


@pytest.mark.asyncio
async def test_apply_candidate_migrations_classifies_class42_sql_error(monkeypatch):
    """A class-42 ``ProgrammingError`` (SQL syntax / undefined table / invalid
    schema) escaping the apply subprocess must be classified as
    ``_DeterministicMigrationDefect``."""
    import app.migration_apply_gate as gate_mod

    class _FakeOrig(Exception):
        pgcode = "42601"  # syntax_error

    async def _raise_sql_error(scratch_engine, script_location, *, timeout_code):
        orig = _FakeOrig("syntax error at or near 'BORK'")
        raise sa.exc.ProgrammingError("syntax error at or near 'BORK'", {}, orig)

    monkeypatch.setattr(gate_mod, "_run_migration_apply_subprocess", _raise_sql_error)

    engine = create_async_engine(
        "postgresql+asyncpg://scratch_user:s@scratch-db:5432/scratch"
    )
    try:
        with pytest.raises(gate_mod._DeterministicMigrationDefect) as exc_info:
            await gate_mod._apply_candidate_migrations(
                engine,
                {"0001_bad.py": "revision='0001'\ndown_revision=None\n"},
            )
    finally:
        await engine.dispose()

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


async def test_scratch_db_url_none_is_blocked_as_unsafe(monkeypatch):
    """When ``scratch_db_url`` is ``None`` (default), the gate resolves it to the
    production URL — which is then caught as unsafe and deferred before any I/O."""
    import app.migration_apply_gate as gate_mod

    read_prod = AsyncMock()
    monkeypatch.setattr(gate_mod, "_read_prod_state", read_prod)

    engine = create_async_engine(
        "postgresql+asyncpg://prod_user:p@prod-db:5432/proddb"
    )
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            candidate_sources={"0001_init.py": "revision='0001'\ndown_revision=None\n"},
            # scratch_db_url omitted — resolves to prod URL → safety block
        )
    finally:
        await engine.dispose()

    assert verdict == "defer", detail
    assert "scratch_database_url_unsafe" in (detail or "")
    read_prod.assert_not_awaited()



async def test_absent_scratch_config_degrades_not_defers(monkeypatch):
    """When MIGRATION_APPLY_SCRATCH_DATABASE_URL is absent, the finalize resolver
    must return ('ok', None) — non-blocking degrade — rather than deferring every
    candidate indefinitely.  This is the documented intent in required_runtime_env.py:
    absent qualification env degrades specific gate checks, not holds the release."""
    from types import SimpleNamespace

    import app.migration_apply_gate as gate_mod
    from app import release_qualifier as qualifier

    monkeypatch.delenv("MIGRATION_APPLY_SCRATCH_DATABASE_URL", raising=False)

    apply_gate_mock = AsyncMock()
    monkeypatch.setattr(gate_mod, "run_migration_apply_gate", apply_gate_mock)
    monkeypatch.setattr(
        qualifier,
        "_resolve_github_with_session",
        AsyncMock(return_value=("token", "owner", "repo")),
    )
    monkeypatch.setattr(qualifier, "resolve_async_engine", lambda _: None)

    class _FakeSession:
        async def get(self, _model, _id):
            return SimpleNamespace(id="project-id")

    verdict, detail = await qualifier._resolve_migration_apply_at_finalize(
        _FakeSession(),
        SimpleNamespace(project_id="project-id", version_number=1),
        "candidate-sha-abc123",
    )

    assert verdict == "skipped", f"expected skipped for absent scratch config, got {verdict!r}: {detail}"
    apply_gate_mock.assert_not_awaited()

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
    assert "scratch_database_url_unsafe" in (detail or "")
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


# ---- Data-migration proof gate tests -----------------------------------------

_MIG_DATA_NO_PROOF = """
from alembic import op
import sqlalchemy as sa

revision = "d01"
down_revision = "a03"


def upgrade():
    # Backfill existing rows with a default value.
    op.execute(sa.text("UPDATE widget SET colour = 'blue' WHERE colour IS NULL"))


def downgrade():
    pass
"""

_MIG_DATA_WITH_PROOF = """
from alembic import op
import sqlalchemy as sa

revision = "d01"
down_revision = "a03"

# BLUEPRINT_DATA_MIGRATION_PROOF: all rows in widget.colour are either NULL
# (backfilled to 'blue') or already non-NULL; count invariant: total unchanged.


def upgrade():
    op.execute(sa.text("UPDATE widget SET colour = 'blue' WHERE colour IS NULL"))


def downgrade():
    pass
"""

_MIG_DATA_ORM_NO_PROOF = """
from alembic import op
import sqlalchemy as sa

revision = "d02"
down_revision = "a03"


def upgrade():
    tbl = sa.table("widget", sa.column("id"), sa.column("colour"))
    with op.batch_alter_table("widget") as batch_op:
        pass
    conn = op.get_bind()
    conn.execute(sa.update(tbl).where(tbl.c.colour == None).values(colour="blue"))


def downgrade():
    pass
"""

_MIG_BULK_INSERT_NO_PROOF = """
from alembic import op
import sqlalchemy as sa

revision = "d03"
down_revision = "a03"


def upgrade():
    tbl = sa.table("feature_flags", sa.column("name"), sa.column("enabled"))
    op.bulk_insert(tbl, [{"name": "new_flag", "enabled": True}])


def downgrade():
    pass
"""

_MIG_STRUCTURAL_ONLY = """
from alembic import op
import sqlalchemy as sa

revision = "s01"
down_revision = "a03"


def upgrade():
    op.add_column("widget", sa.Column("colour", sa.String(), nullable=True))
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_widget_colour", "widget", ["colour"], postgresql_concurrently=True
        )


def downgrade():
    op.drop_index("ix_widget_colour", "widget")
    op.drop_column("widget", "colour")
"""

_MIG_DDL_WITH_DML_IN_TRIGGER = """
from alembic import op
import sqlalchemy as sa

revision = "t01"
down_revision = "a03"


def upgrade():
    # DDL: trigger function body contains DML keywords but this is NOT a data migration.
    op.execute(sa.text(
        '''
        CREATE OR REPLACE FUNCTION trg_widget_audit()
        RETURNS TRIGGER AS $$
        BEGIN
            INSERT INTO widget_audit(widget_id, changed_at) VALUES (NEW.id, NOW());
            UPDATE widget_audit SET last = TRUE WHERE widget_id = NEW.id;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        '''
    ))
    op.execute(sa.text(
        "CREATE TRIGGER trg_widget AFTER UPDATE ON widget "
        "FOR EACH ROW EXECUTE FUNCTION trg_widget_audit()"
    ))


def downgrade():
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_widget ON widget"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS trg_widget_audit"))
"""


def test_migration_has_data_ops_detects_dml_in_execute():
    assert _migration_has_data_ops(_MIG_DATA_NO_PROOF)
    assert _migration_has_data_ops(_MIG_DATA_ORM_NO_PROOF)
    assert _migration_has_data_ops(_MIG_BULK_INSERT_NO_PROOF)


def test_migration_has_data_ops_skips_structural_only():
    assert not _migration_has_data_ops(_MIG_STRUCTURAL_ONLY)


def test_migration_has_data_ops_skips_ddl_with_embedded_dml():
    # Trigger body starts with CREATE OR REPLACE FUNCTION, not DML — should NOT be flagged.
    assert not _migration_has_data_ops(_MIG_DDL_WITH_DML_IN_TRIGGER)


def test_migration_has_proof_declaration():
    assert _migration_has_proof_declaration(_MIG_DATA_WITH_PROOF)
    assert not _migration_has_proof_declaration(_MIG_DATA_NO_PROOF)
    assert not _migration_has_proof_declaration(_MIG_STRUCTURAL_ONLY)


def test_check_data_migration_proof_gate_defers_on_missing_proof():
    sources = {"d01_backfill.py": _MIG_DATA_NO_PROOF}
    verdict, detail = _check_data_migration_proof_gate(sources)
    assert verdict == "defer"
    assert "data_migration_proof_missing" in (detail or "")
    assert "d01_backfill.py" in (detail or "")


def test_check_data_migration_proof_gate_passes_with_proof():
    sources = {"d01_backfill.py": _MIG_DATA_WITH_PROOF}
    result = _check_data_migration_proof_gate(sources)
    assert result is None


def test_check_data_migration_proof_gate_passes_structural_only():
    sources = {"s01_add_col.py": _MIG_STRUCTURAL_ONLY}
    result = _check_data_migration_proof_gate(sources)
    assert result is None


def test_check_data_migration_proof_gate_bulk_insert_requires_proof():
    sources = {"d03_seed.py": _MIG_BULK_INSERT_NO_PROOF}
    verdict, detail = _check_data_migration_proof_gate(sources)
    assert verdict == "defer"
    assert "data_migration_proof_missing" in (detail or "")


def test_new_migration_sources_filters_to_post_prod_revisions():
    # a03 is prod; d01 and s01 are new.
    sources = {
        "a01_init.py": _MIG_A01,
        "a02_computejob.py": _MIG_A02_COMPUTEJOB,
        "a03_later.py": _MIG_A03,
        "d01_backfill.py": _MIG_DATA_NO_PROOF,
        "s01_add_col.py": _MIG_STRUCTURAL_ONLY,
    }
    # Patch _MIG_A03 down_revision to chain properly (it already has a03)
    new = _new_migration_sources(sources, "a03")
    assert "d01_backfill.py" in new
    assert "s01_add_col.py" in new
    assert "a01_init.py" not in new
    assert "a02_computejob.py" not in new
    assert "a03_later.py" not in new


def test_new_migration_sources_returns_all_when_prod_revision_absent():
    # If prod_revision isn't in the chain, all sources are conservatively returned.
    sources = {
        "a01_init.py": _MIG_A01,
        "d01_backfill.py": _MIG_DATA_NO_PROOF,
    }
    new = _new_migration_sources(sources, "unknown_revision")
    assert "a01_init.py" in new
    assert "d01_backfill.py" in new


@pytest.mark.asyncio
async def test_run_migration_apply_gate_defers_on_data_migration_without_proof(
    monkeypatch,
):
    """run_migration_apply_gate defers when a new migration has DML but no proof."""
    import app.migration_apply_gate as gate_mod

    metadata = sa.MetaData()
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_BASE_DELAY", 0)
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_MAX_DELAY", 0)
    monkeypatch.setattr(
        gate_mod,
        "_read_prod_state",
        AsyncMock(return_value=gate_mod._ProdState(revision="a03", metadata=metadata)),
    )

    sources = {
        "a01_init.py": _MIG_A01,
        "a02_computejob.py": _MIG_A02_COMPUTEJOB,
        "a03_later.py": _MIG_A03,
        "d01_backfill.py": _MIG_DATA_NO_PROOF,
    }
    engine = create_async_engine(
        "postgresql+asyncpg://prod_user:secret@prod-db:5432/proddb"
    )
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            candidate_sources=sources,
            required_metadata=metadata,
            scratch_db_url="postgresql+asyncpg://s:s@scratch-db:5432/ctrl",
        )
    finally:
        await engine.dispose()

    assert verdict == "defer"
    assert "data_migration_proof_missing" in (detail or "")
    assert "d01_backfill.py" in (detail or "")


@pytest.mark.asyncio
async def test_run_migration_apply_gate_does_not_flag_existing_data_migrations(
    monkeypatch,
):
    """The proof gate only checks NEW migrations, not those already applied to prod."""
    import app.migration_apply_gate as gate_mod

    metadata = sa.MetaData()
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_BASE_DELAY", 0)
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_MAX_DELAY", 0)
    # Prod is already at d01 (the data migration is already applied).
    monkeypatch.setattr(
        gate_mod,
        "_read_prod_state",
        AsyncMock(return_value=gate_mod._ProdState(revision="d01", metadata=metadata)),
    )
    # Stub out the scratch provisioning step — required_metadata is provided so
    # the reference-build path is skipped; only _provision_seed_and_detect_drift
    # is called (via _with_transient_retry).
    monkeypatch.setattr(
        gate_mod,
        "_provision_seed_and_detect_drift",
        AsyncMock(return_value=[]),  # no drift
    )

    sources = {
        "a01_init.py": _MIG_A01,
        "a02_computejob.py": _MIG_A02_COMPUTEJOB,
        "a03_later.py": _MIG_A03,
        "d01_backfill.py": _MIG_DATA_NO_PROOF,  # already applied in prod
    }
    engine = create_async_engine(
        "postgresql+asyncpg://prod_user:secret@prod-db:5432/proddb"
    )
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            candidate_sources=sources,
            required_metadata=metadata,
            scratch_db_url="postgresql+asyncpg://s:s@scratch-db:5432/ctrl",
        )
    finally:
        await engine.dispose()

    assert verdict == "ok", detail
    assert detail is None


# --- Serving-only import degrade (runs everywhere) --------------------------


@pytest.mark.asyncio
async def test_candidate_only_app_symbol_import_degrades_not_blocks(monkeypatch):
    """An ImportError for an app.* symbol during migration replay is a
    serving-engine artifact: the candidate's own app code is not present in the
    serving process, but WILL be present on the real deploy target.

    The gate must degrade to ``('ok', None)`` — not hard-block — so qualification
    is not permanently wedged by a self-reference contract violation.  This
    exercises ``_ServingOnlyImportError``, its no-retry classification, and the
    ``except _ServingOnlyImportError`` handler that returns ``("ok", None)`` in
    ``run_migration_apply_gate``.
    """
    import app.migration_apply_gate as gate_mod

    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_BASE_DELAY", 0.001)
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_MAX_DELAY", 0.01)

    prod_meta = sa.MetaData()
    sa.Table("widget", prod_meta, sa.Column("id", sa.Integer, primary_key=True))
    monkeypatch.setattr(
        gate_mod,
        "_read_prod_state",
        AsyncMock(
            return_value=gate_mod._ProdState(revision="a01", metadata=prod_meta)
        ),
    )

    @contextlib.asynccontextmanager
    async def _fake_ephemeral(_base_url, db_name=None):
        yield object(), "scratch-gate"

    monkeypatch.setattr(gate_mod, "ephemeral_postgres_database", _fake_ephemeral)
    monkeypatch.setattr(gate_mod, "_seed_scratch", AsyncMock())

    apply_call_count = 0

    async def _serving_only_import_apply(scratch_engine, candidate_sources, **kwargs):
        nonlocal apply_call_count
        apply_call_count += 1
        # Mirrors the error raised when migration 0621 imports require_encryption_active
        # from app.field_encryption, which doesn't exist in the serving-engine codebase.
        exc = ImportError(
            "cannot import name 'require_encryption_active' from 'app.field_encryption'"
        )
        exc.name = "app.field_encryption"
        raise gate_mod._ServingOnlyImportError(
            f"candidate migration imports a candidate-only app symbol "
            f"(serving-engine artifact, not deploy-fatal): {exc}"
        ) from exc

    monkeypatch.setattr(gate_mod, "_apply_candidate_migrations", _serving_only_import_apply)

    engine = create_async_engine(
        "postgresql+asyncpg://prod_user:secret@prod-db:5432/proddb"
    )
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            candidate_sources={
                "a01_init.py": _MIG_A01,
                "0621_encrypt.py": (
                    "revision='0621'\ndown_revision='a01'\n"
                    "from app.field_encryption import require_encryption_active\n"
                    "def upgrade(): require_encryption_active()\n"
                    "def downgrade(): pass\n"
                ),
            },
            required_metadata=prod_meta,
            scratch_db_url=(
                "postgresql+asyncpg://scratch_user:scratch-secret@scratch-db:5432/control"
            ),
        )
    finally:
        await engine.dispose()

    assert verdict == "ok", (
        f"expected 'ok' for candidate-only app symbol ImportError, got {verdict!r}: {detail}"
    )
    assert apply_call_count == 1, (
        f"serving-only import retried {apply_call_count} time(s); must degrade on first attempt"
    )


# ---------------------------------------------------------------------------
# Ephemeral Fernet key injection — regression tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ephemeral_fernet_key_is_injected_into_scratch_subprocess(monkeypatch):
    """A synthetic Fernet key must be present in the scratch subprocess env so
    fail-closed migrations (e.g. 0609) can execute.  It must be a valid base64
    Fernet key (44 chars ending in '='), not the production value."""
    import base64

    import app.migration_apply_gate as gate_mod

    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", "production-key-must-not-leak")
    observed = {}

    class _CompletedProcess:
        returncode = 0
        pid = 456

        async def communicate(self, input=None):
            del input
            return b"", b""

    async def _spawn(*args, **kwargs):
        observed["env"] = kwargs["env"]
        return _CompletedProcess()

    monkeypatch.setattr(gate_mod.asyncio, "create_subprocess_exec", _spawn)

    class _FakeEngine:
        url = make_url(
            "postgresql+asyncpg://u:p@scratch-host:5432/ephemeral_fernet_test"
        )

    await gate_mod._run_migration_apply_subprocess(
        _FakeEngine(),
        "/tmp/script_loc",
        timeout_code="migration_apply_replay_apply_timeout",
    )

    env = observed["env"]

    # Key must be present (fail-closed migrations need it)
    assert "GITHUB_PAT_ENCRYPTION_KEY" in env, "ephemeral key must be injected"

    # Must NOT be the production value
    assert env["GITHUB_PAT_ENCRYPTION_KEY"] != "production-key-must-not-leak", (
        "production key must not leak into scratch subprocess"
    )

    # Must be a structurally valid Fernet key (32 raw bytes → 44 base64 chars)
    raw = base64.urlsafe_b64decode(env["GITHUB_PAT_ENCRYPTION_KEY"] + "==")
    assert len(raw) == 32, f"Fernet key must be 32 bytes, got {len(raw)}"


@pytest.mark.asyncio
async def test_ephemeral_fernet_key_differs_across_calls(monkeypatch):
    """Each gate run must generate a FRESH ephemeral key — never reuse."""
    import app.migration_apply_gate as gate_mod

    monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
    keys_seen = []

    class _CompletedProcess:
        returncode = 0
        pid = 789

        async def communicate(self, input=None):
            del input
            return b"", b""

    async def _spawn(*args, **kwargs):
        keys_seen.append(kwargs["env"].get("GITHUB_PAT_ENCRYPTION_KEY"))
        return _CompletedProcess()

    monkeypatch.setattr(gate_mod.asyncio, "create_subprocess_exec", _spawn)

    class _FakeEngine:
        url = make_url(
            "postgresql+asyncpg://u:p@scratch-host:5432/ephemeral_fernet_fresh"
        )

    for _ in range(3):
        await gate_mod._run_migration_apply_subprocess(
            _FakeEngine(),
            "/tmp/script_loc",
            timeout_code="migration_apply_replay_apply_timeout",
        )

    assert len(keys_seen) == 3
    assert len(set(keys_seen)) == 3, (
        "each gate run must generate a distinct ephemeral Fernet key"
    )

# --- Keyless-encryption artifact degrade (runs everywhere) --------------------
#
# Migration 0609 fail-closes with RuntimeError when GITHUB_PAT_ENCRYPTION_KEY is
# absent.  The scratch subprocess intentionally withholds this key, so 0609
# ALWAYS fails in the replay.  The artifact must degrade to ('ok', None) —
# mirroring _ServingOnlyImportError — rather than deferring forever.


def test_looks_like_keyless_encryption_artifact():
    """The predicate helper must identify the deliberate keyless fail-closed
    artifact and reject unrelated migration failures."""
    import app.migration_apply_gate as gate_mod

    # True positive: the error message from migration 0609 when the encryption
    # key is deliberately withheld in the scratch subprocess.
    fail_closed_detail = (
        "Traceback (most recent call last):\n"
        "  ...\n"
        "RuntimeError: Migration 0609 requires GITHUB_PAT_ENCRYPTION_KEY "
        "to be set to the live Fernet key before encrypted-column "
        "reconciliation; ALLOW_PLAINTEXT_CREDENTIALS does not permit "
        "this migration to stamp."
    )
    assert gate_mod._looks_like_keyless_encryption_artifact(fail_closed_detail), (
        "must identify the deliberate keyless fail-closed artifact"
    )

    # False positive: an unrelated RuntimeError that does NOT reference the
    # withheld env var — a genuinely broken migration must NOT be degraded.
    unrelated_runtime_error = (
        "RuntimeError: could not connect to the database server: "
        "connection refused"
    )
    assert not gate_mod._looks_like_keyless_encryption_artifact(unrelated_runtime_error), (
        "must NOT swallow an unrelated RuntimeError"
    )

    # False positive: a mention of the env var WITHOUT a RuntimeError (e.g. a
    # log line that happens to name the var, or a different exception type).
    env_var_mention = (
        "psycopg.errors.NotNullViolation: GITHUB_PAT_ENCRYPTION_KEY "
        "column violates not-null constraint"
    )
    assert not gate_mod._looks_like_keyless_encryption_artifact(env_var_mention), (
        "must NOT match the env var name alone without a RuntimeError"
    )

    # False positive: a bare non-RuntimeError migration failure with no env-var
    # reference — the most common case for a genuinely broken migration.
    sql_defect_detail = (
        "psycopg.errors.UndefinedTable: relation \"widget\" does not exist\n"
        "LINE 1: INSERT INTO widget (id) VALUES (1)"
    )
    assert not gate_mod._looks_like_keyless_encryption_artifact(sql_defect_detail), (
        "must NOT swallow a genuine SQL defect with no env-var reference"
    )


def test_looks_like_keyless_encryption_artifact_rejects_accumulated_chain_stderr():
    """Accumulated chain stderr from earlier no-op migrations emits the env-var
    name to stderr, so a real RuntimeError from an *unrelated* broken migration
    that follows a 0531-style warning must NOT be classified as the 0609
    keyless fail-closed artifact.

    This test catches the regression where the predicate matched on the
    env-var name + RuntimeError alone (too broad) instead of requiring the
    0609-specific error message marker.
    """
    import app.migration_apply_gate as gate_mod

    # Simulates accumulated chain stderr: the 0531 no-op migration emits the
    # env-var name as a warning, and then an *unrelated* migration fails with
    # a RuntimeError that has nothing to do with the keyless fail-closed.
    accumulated_chain_stderr = (
        "INFO:root:GITHUB_PAT_ENCRYPTION_KEY not set — "
        "migration 0531 is a no-op and will stamp without encrypting.\n"
        "Traceback (most recent call last):\n"
        "  File \"/app/alembic/versions/0612_add_widget_table.py\", line 42, in upgrade\n"
        "    op.add_column('widget', sa.Column('size', sa.Integer, nullable=False))\n"
        "RuntimeError: Column 'size' violates a domain constraint unrelated to encryption"
    )
    assert not gate_mod._looks_like_keyless_encryption_artifact(accumulated_chain_stderr), (
        "must NOT classify accumulated chain stderr (0531 warning + unrelated RuntimeError) "
        "as the 0609 keyless fail-closed artifact — env-var name appears only in a "
        "prior no-op warning, not in the RuntimeError that follows"
    )


@pytest.mark.asyncio
async def test_keyless_encryption_migration_replay_degrades_not_defers(monkeypatch):
    """The keyless-encryption fail-closed artifact in the scratch subprocess
    must degrade to ``('ok', None)`` — mirroring ``_ServingOnlyImportError`` —
    so qualification is not permanently wedged when the gate deliberately
    withholds GITHUB_PAT_ENCRYPTION_KEY from the secret-free replay.

    This exercises ``_KeylessEncryptionArtifact``, its no-retry classification,
    and the ``except _KeylessEncryptionArtifact`` handler in
    ``run_migration_apply_gate`` that returns ``("ok", None)``.
    """
    import app.migration_apply_gate as gate_mod

    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_BASE_DELAY", 0.001)
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_MAX_DELAY", 0.01)

    prod_meta = sa.MetaData()
    sa.Table("widget", prod_meta, sa.Column("id", sa.Integer, primary_key=True))
    monkeypatch.setattr(
        gate_mod,
        "_read_prod_state",
        AsyncMock(
            return_value=gate_mod._ProdState(revision="a01", metadata=prod_meta)
        ),
    )

    @contextlib.asynccontextmanager
    async def _fake_ephemeral(_base_url, db_name=None):
        yield object(), "scratch-gate"

    monkeypatch.setattr(gate_mod, "ephemeral_postgres_database", _fake_ephemeral)
    monkeypatch.setattr(gate_mod, "_seed_scratch", AsyncMock())

    apply_call_count = 0

    async def _keyless_encryption_artifact_apply(
        scratch_engine, candidate_sources, **kwargs
    ):
        nonlocal apply_call_count
        apply_call_count += 1
        # Mirrors the error raised when migration 0609 fail-closes because the
        # gate deliberately withholds GITHUB_PAT_ENCRYPTION_KEY from the scratch
        # subprocess environment.
        raise gate_mod._KeylessEncryptionArtifact(
            "candidate encryption migration fail-closed because "
            "GITHUB_PAT_ENCRYPTION_KEY is deliberately withheld from "
            "the secret-free scratch replay subprocess (not deploy-fatal)"
        )

    monkeypatch.setattr(
        gate_mod, "_apply_candidate_migrations", _keyless_encryption_artifact_apply
    )

    engine = create_async_engine(
        "postgresql+asyncpg://prod_user:secret@prod-db:5432/proddb"
    )
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            candidate_sources={
                "a01_init.py": _MIG_A01,
                "0609_encrypt.py": (
                    "revision='0609'\ndown_revision='a01'\n"
                    "def upgrade():\n"
                    "    import os\n"
                    "    key = os.environ.get('GITHUB_PAT_ENCRYPTION_KEY')\n"
                    "    if not key:\n"
                    "        raise RuntimeError('requires GITHUB_PAT_ENCRYPTION_KEY')\n"
                    "def downgrade(): pass\n"
                ),
            },
            required_metadata=prod_meta,
            scratch_db_url=(
                "postgresql+asyncpg://scratch_user:scratch-secret@scratch-db:5432/control"
            ),
        )
    finally:
        await engine.dispose()

    assert verdict == "ok", (
        f"expected 'ok' for keyless-encryption artifact, got {verdict!r}: {detail}"
    )
    assert apply_call_count == 1, (
        f"keyless-encryption artifact retried {apply_call_count} time(s); "
        "must degrade on first attempt"
    )

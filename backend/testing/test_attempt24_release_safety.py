"""Combined attempt-24 release-safety acceptance contract."""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet

from app.hotfix_policy import evaluate_hotfix_scope
from app.main import EXPECTED_SCHEMA_REVISION
from app.migration_apply_gate import (
    MigrationApplyScratchPreflight,
    preflight_migration_apply_scratch,
)
from app.migration_chain import validate_standalone_chain
from app.required_runtime_env import REQUIRED_RUNTIME_ENV, _verify_required_env

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = next(
    candidate
    for candidate in (BACKEND_ROOT.parent, Path("/repo"))
    if (candidate / "scripts" / "test-backend.sh").exists()
)
MIGRATION_PATH = (
    BACKEND_ROOT
    / "alembic"
    / "versions"
    / "0609_reconcile_encrypted_columns_fail_closed.py"
)
TEST_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0609", MIGRATION_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_BASE_MIGRATION = """
import sqlalchemy as sa
from alembic import op

revision = "0566"
down_revision = None

def upgrade():
    op.create_table("maxplanaccount", sa.Column("id", sa.Integer, primary_key=True), sa.Column("oauth_token", sa.Text), sa.Column("claude_config_json", sa.Text), sa.Column("secret_blob_ref", sa.Text))
    op.create_table("notification_dead_letter", sa.Column("id", sa.Integer, primary_key=True), sa.Column("webhook_url", sa.String(2048), nullable=False))
    op.create_table("userintegration", sa.Column("id", sa.Integer, primary_key=True), sa.Column("github_pat", sa.Text), sa.Column("github_oauth_token", sa.Text))
    op.create_table("deploymentstate", sa.Column("id", sa.Integer, primary_key=True), sa.Column("runtime_config_json", sa.JSON))

def downgrade():
    pass
"""


_PREDECESSOR_MIGRATION = """
revision = "0608"
down_revision = "0566"

def upgrade():
    pass

def downgrade():
    pass
"""


_ALEMBIC_ENV = """
from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

def run_migrations_online():
    connectable = engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()

run_migrations_online()
"""


def _temporary_alembic(
    tmp_path: Path, name: str, base_migration: str = _BASE_MIGRATION
) -> tuple[Config, sa.Engine]:
    root = tmp_path / name
    versions = root / "versions"
    versions.mkdir(parents=True)
    (root / "env.py").write_text(_ALEMBIC_ENV)
    (versions / "0566_base.py").write_text(base_migration)
    (versions / "0608_predecessor.py").write_text(_PREDECESSOR_MIGRATION)
    (versions / MIGRATION_PATH.name).write_text(
        MIGRATION_PATH.read_text(encoding="utf-8"), encoding="utf-8"
    )
    db_path = tmp_path / f"{name}.sqlite"
    config = Config()
    config.set_main_option("script_location", str(root))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config, sa.create_engine(f"sqlite:///{db_path}")


def _seed_encryption_rows(engine: sa.Engine, fernet: Fernet) -> str:
    preserved = fernet.encrypt(b"already-encrypted").decode()
    longest_legacy_webhook = "https://hooks.example/" + ("a" * 2026)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO maxplanaccount "
                "(id, oauth_token, claude_config_json, secret_blob_ref) VALUES "
                "(1, 'plain-oauth', 'plain-config', 'plain-ref'), "
                "(2, :cipher, :cipher, :cipher)"
            ),
            {"cipher": preserved},
        )
        conn.execute(
            sa.text(
                "INSERT INTO notification_dead_letter (id, webhook_url) "
                "VALUES (1, :webhook_url)"
            ),
            {"webhook_url": longest_legacy_webhook},
        )
        conn.execute(
            sa.text(
                "INSERT INTO userintegration (id, github_pat, github_oauth_token) "
                "VALUES (1, 'plain-pat', 'plain-oauth-token')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO deploymentstate (id, runtime_config_json) "
                "VALUES (1, :config)"
            ),
            {
                "config": json.dumps(
                    {
                        "unrelated": {"preserved": True},
                        "notification_config": {
                            "webhook_url": "https://notify.example/plain",
                            "throttle_seconds": 30,
                        },
                    }
                )
            },
        )
    return preserved


@pytest.mark.asyncio
async def test_attempt24_release_safety_contract(tmp_path, monkeypatch, mocker):
    """Pin encryption, optional-scratch, deploy, and narrow-recovery safety."""
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module
        and (node.module == "app" or node.module.startswith("app."))
        for node in ast.walk(tree)
    )
    migration = _load_migration()
    assert migration.revision == "0609"
    assert migration.down_revision == "0608"

    # Alembic must leave the prior stamp intact for absent, malformed, and
    # invalid-padding keys, even when the normal test/dev plaintext opt-in is present.
    for name, key in (
        ("missing", None),
        ("invalid", "not-a-fernet-key"),
        # binascii.Error: incorrect padding — must also be caught deterministically
        ("invalid-pad", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA!"),
    ):
        config, engine = _temporary_alembic(tmp_path, name)
        command.upgrade(config, "0566")
        monkeypatch.setenv("ALLOW_PLAINTEXT_CREDENTIALS", "true")
        if key is None:
            monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
        else:
            monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)
        with pytest.raises(RuntimeError, match="GITHUB_PAT_ENCRYPTION_KEY"):
            command.upgrade(config, "head")
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                == "0608"
            )
        engine.dispose()

    # An absent github_oauth_token column (schema from before 0566) must also
    # prevent 0609 from stamping — the column is required, not optional.
    _base_no_oauth = _BASE_MIGRATION.replace(
        ', sa.Column("github_oauth_token", sa.Text)', ""
    )
    config_no_oauth, engine_no_oauth = _temporary_alembic(
        tmp_path, "no_oauth_col", base_migration=_base_no_oauth
    )
    command.upgrade(config_no_oauth, "0566")
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", TEST_KEY)
    with pytest.raises(RuntimeError, match="github_oauth_token"):
        command.upgrade(config_no_oauth, "head")
    with engine_no_oauth.connect() as conn:
        assert (
            conn.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0608"
        )
    engine_no_oauth.dispose()

    # A real configured Alembic upgrade reconciles every current storage
    # location and a repeated invocation preserves all ciphertext byte-for-byte.
    config, engine = _temporary_alembic(tmp_path, "valid")
    command.upgrade(config, "0566")
    fernet = Fernet(TEST_KEY.encode())
    preserved = _seed_encryption_rows(engine, fernet)
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", TEST_KEY)
    command.upgrade(config, "head")
    with engine.begin() as conn:
        assert (
            conn.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0609"
        )
        scalars = conn.execute(
            sa.text(
                "SELECT oauth_token, claude_config_json, secret_blob_ref "
                "FROM maxplanaccount WHERE id = 1"
            )
        ).one()
        assert [fernet.decrypt(value.encode()).decode() for value in scalars] == [
            "plain-oauth",
            "plain-config",
            "plain-ref",
        ]
        already = conn.execute(
            sa.text(
                "SELECT oauth_token, claude_config_json, secret_blob_ref "
                "FROM maxplanaccount WHERE id = 2"
            )
        ).one()
        assert list(already) == [preserved, preserved, preserved]
        integration = conn.execute(
            sa.text(
                "SELECT github_pat, github_oauth_token FROM userintegration WHERE id = 1"
            )
        ).one()
        assert [fernet.decrypt(value.encode()).decode() for value in integration] == [
            "plain-pat",
            "plain-oauth-token",
        ]
        dead_letter = conn.execute(
            sa.text("SELECT webhook_url FROM notification_dead_letter WHERE id = 1")
        ).scalar_one()
        assert len(dead_letter) > 2048
        assert fernet.decrypt(dead_letter.encode()).decode() == (
            "https://hooks.example/" + ("a" * 2026)
        )
        runtime = json.loads(
            conn.execute(
                sa.text("SELECT runtime_config_json FROM deploymentstate WHERE id = 1")
            ).scalar_one()
        )
        assert runtime["unrelated"] == {"preserved": True}
        nested_cipher = runtime["notification_config"]["webhook_url"]
        assert (
            fernet.decrypt(nested_cipher.encode()).decode()
            == "https://notify.example/plain"
        )

        before = {
            "max": tuple(already),
            "integration": tuple(integration),
            "dead_letter": dead_letter,
            "runtime": runtime,
        }
        migration.backfill_encrypted_columns(conn, fernet)
        assert (
            tuple(
                conn.execute(
                    sa.text(
                        "SELECT oauth_token, claude_config_json, secret_blob_ref "
                        "FROM maxplanaccount WHERE id = 2"
                    )
                ).one()
            )
            == before["max"]
        )
        assert (
            conn.execute(
                sa.text("SELECT webhook_url FROM notification_dead_letter WHERE id = 1")
            ).scalar_one()
            == before["dead_letter"]
        )
        assert tuple(
            conn.execute(
                sa.text(
                    "SELECT github_pat, github_oauth_token "
                    "FROM userintegration WHERE id = 1"
                )
            ).one()
        ) == before["integration"]
        assert json.loads(
            conn.execute(
                sa.text(
                    "SELECT runtime_config_json FROM deploymentstate WHERE id = 1"
                )
            ).scalar_one()
        ) == before["runtime"]
    with pytest.raises(NotImplementedError, match="plaintext"):
        migration.downgrade()
    engine.dispose()

    versions = {
        path.name: path.read_text(encoding="utf-8")
        for path in (BACKEND_ROOT / "alembic" / "versions").glob("[0-9]*.py")
    }
    assert not validate_standalone_chain(
        versions, expected_schema_revision=EXPECTED_SCHEMA_REVISION
    )
    harness = (REPO_ROOT / "scripts" / "test-backend.sh").read_text(encoding="utf-8")
    assert "TEST_ENCRYPTION_KEY=" in harness
    assert "GITHUB_PAT_ENCRYPTION_KEY=$TEST_ENCRYPTION_KEY" in harness

    # The GitHub Actions full-chain migration runs the cached `deps` target
    # without passing a key. That target carries a non-production test key,
    # while the production `app` target starts independently from `base`.
    dockerfile = (REPO_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    deps_stage = dockerfile.split("FROM base AS deps", 1)[1].split(
        "FROM base AS app", 1
    )[0]
    app_stage = dockerfile.split("FROM base AS app", 1)[1]
    assert f"ENV GITHUB_PAT_ENCRYPTION_KEY={TEST_KEY}" in deps_stage
    assert "GITHUB_PAT_ENCRYPTION_KEY" not in app_stage

    # Optional qualification infrastructure cannot crash Cloud Run boot, and
    # the qualifier consumes a typed defer before resolving production I/O.
    monkeypatch.setenv("K_REVISION", "attempt24-serving")
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.delenv("MIGRATION_APPLY_SCRATCH_DATABASE_URL", raising=False)
    assert "MIGRATION_APPLY_SCRATCH_DATABASE_URL" not in {
        requirement.name for requirement in REQUIRED_RUNTIME_ENV
    }
    assert _verify_required_env() is None
    preflight = preflight_migration_apply_scratch(None)
    assert isinstance(preflight, MigrationApplyScratchPreflight)
    assert preflight.code == "scratch_database_url_missing"

    from app import release_qualifier as qualifier

    fake_project = SimpleNamespace(id="project-id")

    class FakeSession:
        async def get(self, _model, _identifier):
            return fake_project

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    resolve_engine = mocker.patch("app.release_qualifier.resolve_async_engine")
    apply_gate = mocker.patch(
        "app.release_qualifier.run_migration_apply_gate", new=AsyncMock()
    )
    verdict, detail = await qualifier._resolve_migration_apply_at_finalize(
        FakeSession(), SimpleNamespace(project_id="project-id"), "candidate-sha"
    )
    assert verdict == "defer"
    assert "scratch_database_url_missing" in (detail or "")
    resolve_engine.assert_not_called()
    apply_gate.assert_not_awaited()

    cloudbuild = (BACKEND_ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")
    setup = (REPO_ROOT / "setup.sh").read_text(encoding="utf-8")
    replace_all_lines = [
        line for line in (cloudbuild + setup).splitlines() if "--set-secrets" in line
    ]
    service_replace_all = [
        line for line in replace_all_lines if "GITHUB_PAT_ENCRYPTION_KEY" in line
    ]
    assert service_replace_all
    assert all(
        "MIGRATION_APPLY_SCRATCH_DATABASE_URL" not in line
        for line in service_replace_all
    )
    assert "gcloud secrets describe MIGRATION_APPLY_SCRATCH_DATABASE_URL" in cloudbuild
    assert "gcloud secrets describe MIGRATION_APPLY_SCRATCH_DATABASE_URL" in setup
    assert "--update-secrets=MIGRATION_APPLY_SCRATCH_DATABASE_URL=" in cloudbuild
    assert 'prompt_secret_or_skip "MIGRATION_APPLY_SCRATCH_DATABASE_URL"' not in setup

    eligible = evaluate_hotfix_scope(["backend/app/migration_apply_gate.py"])
    assert eligible.eligible is True
    assert (
        evaluate_hotfix_scope(
            ["backend/alembic/versions/0609_reconcile_encrypted_columns_fail_closed.py"]
        ).eligible
        is False
    )
    assert evaluate_hotfix_scope([".github/workflows/deploy.yml"]).eligible is False
    assert (
        evaluate_hotfix_scope(
            ["backend/app/main.py"],
            main_py_patch='-EXPECTED_SCHEMA_REVISION = "0608"\n+EXPECTED_SCHEMA_REVISION = "0609"',
        ).eligible
        is False
    )
    deploy_doc = (REPO_ROOT / "Deploy.md").read_text(encoding="utf-8")
    assert "/promote/hotfix/preview" in deploy_doc
    assert "do not substitute the whole-tree force-promotion" in deploy_doc

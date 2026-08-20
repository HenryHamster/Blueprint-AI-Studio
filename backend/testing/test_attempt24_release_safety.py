"""Combined attempt-24 release-safety acceptance contract."""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
    assert EXPECTED_SCHEMA_REVISION >= "0609"

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
    # A missing scratch DB config is a deliberate "skipped" verdict (not "ok" —
    # a config-absent gate must not read as a passed gate).
    assert verdict == "skipped"
    assert detail == "scratch_database_not_configured"
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
    # The narrow lane is the documented answer to "integration accumulated
    # unrelated work since the last promotion", so it must be in the runbook.
    assert "repair_commit_shas" in deploy_doc


@pytest.mark.asyncio
async def test_hotfix_lane_ships_a_surgical_machinery_repair_from_a_wide_candidate(
    monkeypatch,
):
    """The lane must ship a two-file machinery repair out of a 402-file candidate.

    This is the exact situation the hotfix lane exists for and the exact one it
    could not serve: the latched gate's repair is already sitting in the
    integration branch, but the lane scope-checked the WHOLE
    promotion-target..integration compare, so it always returned
    ``scope_rejected`` and the operator had to hand-cut a branch.

    End-to-end over the real production path (only GitHub I/O is faked):
    ``materialize_narrow_hotfix_candidate`` -> ``narrow_repair_scope`` ->
    ``evaluate_hotfix_scope`` -> ``create_dangling_commit_from_blob_refs``.
    """
    from app import github, github_refs, hotfix_narrow_candidate, hotfix_policy

    prior_deployed = "d" * 40
    repair_sha = "a" * 40
    integration_head = "c" * 40
    repair_files = [
        "backend/app/release_stall_alerts.py",
        "backend/testing/test_release_stall_alerts.py",
    ]
    # 400 unrelated product files landed on integration since the last promotion.
    accumulated = [
        {"filename": f"backend/app/product_{i}.py", "patch": None} for i in range(400)
    ]
    repair_entries = [
        {"filename": path, "patch": "@@ -1 +1 @@\n-old\n+new\n"}
        for path in repair_files
    ]

    async def _fake_compare(token, owner, repo, base_ref, head_ref, project_id=None):
        if head_ref == integration_head:
            # The WHOLE accumulated candidate: repair + everything else.
            return list(repair_entries) + list(accumulated)
        assert (base_ref, head_ref) == (f"{repair_sha}~1", repair_sha)
        return list(repair_entries)

    monkeypatch.setattr(
        github_refs, "get_changed_files_with_patches_between_refs", _fake_compare
    )

    # 1. The pre-existing whole-candidate input is (and stays) ineligible — that
    #    is the latch this task repairs, not something the fix should paper over.
    wide = await github_refs.get_changed_files_with_patches_between_refs(
        "t", "o", "r", prior_deployed, integration_head
    )
    wide_verdict = hotfix_policy.evaluate_hotfix_scope([f["filename"] for f in wide])
    assert wide_verdict.eligible is False
    assert len(wide_verdict.offending_files) == 400

    # 2. GitHub blob/tree/commit plumbing for the narrow materialization.
    blob_shas = {path: f"blob-{i}" for i, path in enumerate(repair_files)}

    async def _fake_blob_sha(path, *, token, owner, repo, ref, project_id=None):
        assert ref == integration_head, "content must come from the repair's tree"
        return (True, blob_shas[path])

    monkeypatch.setattr(github, "get_file_blob_sha_at_ref", _fake_blob_sha)

    calls: list[tuple[str, str, dict]] = []

    async def _fake_request(method, url, headers, json=None, **kwargs):
        calls.append((method, url, json or {}))
        if url.endswith(f"/git/commits/{prior_deployed}"):
            return SimpleNamespace(
                status_code=200, json=lambda: {"tree": {"sha": "base-tree"}}
            )
        if url.endswith("/git/trees"):
            return SimpleNamespace(status_code=201, json=lambda: {"sha": "new-tree"})
        if url.endswith("/git/commits"):
            return SimpleNamespace(status_code=201, json=lambda: {"sha": "e" * 40})
        raise AssertionError(f"unexpected GitHub call: {method} {url}")

    monkeypatch.setattr(github, "_github_request", _fake_request)

    result = await hotfix_narrow_candidate.materialize_narrow_hotfix_candidate(
        "t",
        "o",
        "r",
        prior_deployed_sha=prior_deployed,
        repair_commit_shas=[repair_sha],
        content_ref=integration_head,
    )

    # 3. The surgical repair IS shippable, as a commit parented on the promotion
    #    target that carries ONLY the two machinery files.
    assert result["status"] == "materialized", result
    assert result["candidate_sha"] == "e" * 40
    assert result["changed_files"] == repair_files
    assert result["content_ref"] == integration_head

    tree_call = next(c for c in calls if c[1].endswith("/git/trees"))
    assert tree_call[2]["base_tree"] == "base-tree"
    assert tree_call[2]["tree"] == [
        {"path": repair_files[0], "mode": "100644", "type": "blob", "sha": "blob-0"},
        {"path": repair_files[1], "mode": "100644", "type": "blob", "sha": "blob-1"},
    ], "the narrow commit must carry the repair's EXISTING blobs and nothing else"
    commit_call = next(
        c for c in calls if c[0] == "POST" and c[1].endswith("/git/commits")
    )
    assert commit_call[2]["parents"] == [prior_deployed], (
        "the narrow candidate must be parented on the promotion target, so its "
        "diff is exactly the repair — not the accumulated integration history"
    )
    assert commit_call[2]["tree"] == "new-tree"
    # No ref was created or moved: promote()'s own create_branch is what first
    # points a ref at this object.
    assert not [c for c in calls if "/git/refs" in c[1] or "/git/ref/" in c[1]]

    # 4. Fail-closed is preserved: naming a commit that also carries product code
    #    is still rejected, and nothing is committed for it.
    calls.clear()
    product_sha = "b" * 40

    async def _compare_with_product(
        token, owner, repo, base_ref, head_ref, project_id=None
    ):
        return [
            {"filename": "backend/app/release_stall_alerts.py", "patch": None},
            {"filename": "backend/app/reaper.py", "patch": None},
        ]

    monkeypatch.setattr(
        github_refs,
        "get_changed_files_with_patches_between_refs",
        _compare_with_product,
    )
    rejected = await hotfix_narrow_candidate.materialize_narrow_hotfix_candidate(
        "t",
        "o",
        "r",
        prior_deployed_sha=prior_deployed,
        repair_commit_shas=[product_sha],
        content_ref=product_sha,
    )
    assert rejected["status"] == "scope_rejected"
    assert rejected["offending_files"] == ["backend/app/reaper.py"]
    assert calls == [], "a rejected scope must create no blob, tree, or commit"


class TestVerifyEncryptionConfigBootGate:
    """Behavioral tests for _verify_encryption_config boot-gate wiring.

    These tests pin the exact seam that twice caused promotion boot-crash
    incidents: first-boot mint+persist, second-boot same-key pass, wrong-key
    fails closed, Cloud-Run-no-key raises, and dev-no-key warning-only.
    """

    def _mock_session_local(self, mocker, state):
        """Patch app.database.AsyncSessionLocal to yield a mock session returning `state`."""
        session = MagicMock()
        session.get = AsyncMock(return_value=state)
        session.commit = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_local = MagicMock(return_value=cm)
        mocker.patch("app.database.AsyncSessionLocal", mock_local)
        return session

    @pytest.mark.asyncio
    async def test_first_boot_mints_and_persists_continuity_reference(
        self, monkeypatch, mocker
    ):
        """No stored ref (first boot) → continuity ref minted and committed to DB."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)
        monkeypatch.delenv("K_REVISION", raising=False)
        monkeypatch.delenv("ALLOW_PLAINTEXT_CREDENTIALS", raising=False)

        session = self._mock_session_local(mocker, None)

        from app.main import _verify_encryption_config

        await _verify_encryption_config()

        session.commit.assert_awaited_once()
        session.add.assert_called_once()
        added = session.add.call_args[0][0]
        ref = added.runtime_config_json["encryption_continuity_reference"]
        # Must be a valid Fernet ciphertext for this key (not just truthy).
        plaintext = Fernet(key.encode()).decrypt(ref.encode()).decode()
        assert plaintext == "blueprint-field-encryption-continuity-v1"

    @pytest.mark.asyncio
    async def test_second_boot_correct_key_passes(self, monkeypatch, mocker):
        """Stored ref present + correct key → probe passes → no DB commit."""
        from app.models import DeploymentState

        key = Fernet.generate_key().decode()
        stored_ref = (
            Fernet(key.encode())
            .encrypt(b"blueprint-field-encryption-continuity-v1")
            .decode()
        )
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)
        monkeypatch.delenv("K_REVISION", raising=False)

        state = DeploymentState(
            id=1, runtime_config_json={"encryption_continuity_reference": stored_ref}
        )
        session = self._mock_session_local(mocker, state)

        from app.main import _verify_encryption_config

        await _verify_encryption_config()

        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_second_boot_wrong_key_fails_closed(self, monkeypatch, mocker):
        """Stored ref from old key + new key configured → RuntimeError (fails closed)."""
        from app.models import DeploymentState

        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key().decode()
        stored_ref = (
            Fernet(old_key)
            .encrypt(b"blueprint-field-encryption-continuity-v1")
            .decode()
        )
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", new_key)
        monkeypatch.delenv("K_REVISION", raising=False)

        state = DeploymentState(
            id=1, runtime_config_json={"encryption_continuity_reference": stored_ref}
        )
        self._mock_session_local(mocker, state)

        from app.main import _verify_encryption_config

        with pytest.raises(RuntimeError, match="continuity reference"):
            await _verify_encryption_config()

    @pytest.mark.asyncio
    async def test_cloud_run_no_key_raises(self, monkeypatch):
        """K_REVISION set + GITHUB_PAT_ENCRYPTION_KEY absent → RuntimeError at boot."""
        monkeypatch.setenv("K_REVISION", "v26-attempt24")
        monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)

        from app.main import _verify_encryption_config

        with pytest.raises(RuntimeError, match="GITHUB_PAT_ENCRYPTION_KEY"):
            await _verify_encryption_config()

    @pytest.mark.asyncio
    async def test_local_production_no_key_or_plaintext_opt_in_raises(self, monkeypatch):
        """A local control-plane deployment has the same encryption boundary."""
        monkeypatch.delenv("K_REVISION", raising=False)
        monkeypatch.setenv("BLUEPRINT_DEPLOYMENT_MODE", "local-production")
        monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
        monkeypatch.setenv("ALLOW_PLAINTEXT_CREDENTIALS", "true")

        from app.main import _verify_encryption_config

        with pytest.raises(RuntimeError, match="GITHUB_PAT_ENCRYPTION_KEY"):
            await _verify_encryption_config()

    @pytest.mark.asyncio
    async def test_local_production_rejects_plaintext_opt_in_even_with_key(
        self, monkeypatch
    ):
        monkeypatch.delenv("K_REVISION", raising=False)
        monkeypatch.setenv("BLUEPRINT_DEPLOYMENT_MODE", "local-production")
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", Fernet.generate_key().decode())
        monkeypatch.setenv("ALLOW_PLAINTEXT_CREDENTIALS", "true")

        from app.main import _verify_encryption_config

        with pytest.raises(RuntimeError, match="ALLOW_PLAINTEXT_CREDENTIALS"):
            await _verify_encryption_config()

    @pytest.mark.asyncio
    async def test_dev_no_key_with_plaintext_opt_in_no_raise(self, monkeypatch):
        """K_REVISION absent + no key + ALLOW_PLAINTEXT_CREDENTIALS → no raise (dev mode)."""
        monkeypatch.delenv("K_REVISION", raising=False)
        monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
        monkeypatch.setenv("ALLOW_PLAINTEXT_CREDENTIALS", "true")

        from app.main import _verify_encryption_config

        await _verify_encryption_config()  # must not raise

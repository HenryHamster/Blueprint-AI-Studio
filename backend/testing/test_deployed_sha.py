"""Tests for the best-effort runtime deployed-SHA resolver.

All network calls are mocked — these tests never reach the real Cloud Run API.
"""

import ast
import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from app import deployed_sha


def _resolve_scripts_dir() -> Path:
    """Locate the repo ``scripts/`` dir across local + Docker test environments.

    Candidates, in order: an explicit env override, the ``/repo`` bind-mount,
    then the repo root two levels above ``backend/testing/``. The CI backend
    shards and ``scripts/test-backend.sh`` build the image from ``backend/``
    alone (``COPY . .`` → ``/app``) and mount the repo root read-only at
    ``/repo`` — so inside that image ``parents[2]`` is the filesystem root and
    ``/scripts`` does not exist; the repo ``scripts/`` is reachable only via
    ``/repo/scripts``. Local/plain checkouts resolve via ``parents[2]``.
    Mirrors ``test_local_runner_terminal_log._resolve_local_runner_path``.
    """
    candidates = []
    env_override = os.getenv("BLUEPRINT_REPO_SCRIPTS_DIR")
    if env_override:
        candidates.append(Path(env_override))
    candidates.append(Path("/repo/scripts"))
    candidates.append(Path(__file__).resolve().parents[2] / "scripts")
    for path in candidates:
        if (path / "blueprint_api.py").is_file():
            return path
    # Fall back to the plain-checkout path so a genuine miss surfaces a clear
    # FileNotFoundError naming the expected location rather than a bare None.
    return candidates[-1]


_SCRIPTS_DIR = _resolve_scripts_dir()
_REPO_ROOT = _SCRIPTS_DIR.parent


def _load_blueprint_script():
    """Load the script and its sibling dependency without mutating sys.path."""
    api_spec = importlib.util.spec_from_file_location(
        "blueprint_api_for_deployed_sha_test", _SCRIPTS_DIR / "blueprint_api.py"
    )
    assert api_spec and api_spec.loader
    api_module = importlib.util.module_from_spec(api_spec)
    previous_api = sys.modules.get("blueprint_api")
    sys.modules["blueprint_api"] = api_module
    try:
        api_spec.loader.exec_module(api_module)
        blueprint_spec = importlib.util.spec_from_file_location(
            "blueprint_for_deployed_sha_test", _SCRIPTS_DIR / "blueprint.py"
        )
        assert blueprint_spec and blueprint_spec.loader
        blueprint_module = importlib.util.module_from_spec(blueprint_spec)
        blueprint_spec.loader.exec_module(blueprint_module)
        return blueprint_module
    finally:
        if previous_api is None:
            sys.modules.pop("blueprint_api", None)
        else:
            sys.modules["blueprint_api"] = previous_api


deploy_blueprint = _load_blueprint_script()


def test_operator_docs_do_not_revoke_bootstrap_removed_sudoers_grant():
    """Keep operator guidance aligned with the bootstrap's privilege end state."""
    bootstrap_path = _SCRIPTS_DIR / "bootstrap_local_app_release.py"
    bootstrap_tree = ast.parse(
        bootstrap_path.read_text(encoding="utf-8"), filename=str(bootstrap_path)
    )

    legacy_paths = [
        node.value.args[0].value
        for node in ast.walk(bootstrap_tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_LEGACY_RELEASE_SUDOERS"
            for target in node.targets
        )
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "Path"
        and len(node.value.args) == 1
        and isinstance(node.value.args[0], ast.Constant)
        and isinstance(node.value.args[0].value, str)
    ]
    assert legacy_paths == ["/etc/sudoers.d/blueprint-release"]
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "unlink"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "_LEGACY_RELEASE_SUDOERS"
        for node in ast.walk(bootstrap_tree)
    ), "bootstrap must remove the legacy sudoers grant"

    legacy_path = legacy_paths[0]
    operator_files = [
        *(_REPO_ROOT / "docs").rglob("*.md"),
        *(_REPO_ROOT / "admin-ui").rglob("*.ts"),
        *(_REPO_ROOT / "admin-ui").rglob("*.tsx"),
    ]
    stale_instruction = re.compile(
        rf"\brevoke\b[^.\n]*{re.escape(legacy_path)}", re.IGNORECASE
    )
    stale_lines = [
        f"{path}:{line_number}: {line}"
        for path in operator_files
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        )
        if stale_instruction.search(line)
    ]
    assert stale_lines == []

    deploy_doc = (_REPO_ROOT / "docs/operations/one-command-deploy.md").read_text(
        encoding="utf-8"
    )
    assert (
        f"bootstrap removes the legacy\n  `{legacy_path}` grant during installation"
        in deploy_doc
    )

# A real 64-hex container image digest (sha256). It is all-hex, so a naive
# git-SHA check would WRONGLY accept it — the resolver must reject it.
_DIGEST = "96e4767e89287084484acf420381a0b53a1bd976828342787b07c28b167e082c"
_GIT_SHA = "046653f6615ac6e8716b13d82bdbaa7313bf823f"  # 40-hex git commit


class TestParseShaFromImage:
    def test_parses_sha_tag(self):
        image = (
            "us-central1-docker.pkg.dev/proj/blueprint-repo/"
            "blueprint-orchestrator:abc1234"
        )
        assert deployed_sha._parse_sha_from_image(image) == "abc1234"

    def test_full_length_sha(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        image = f"registry/blueprint-orchestrator:{sha}"
        assert deployed_sha._parse_sha_from_image(image) == sha

    def test_non_sha_tag_returns_none(self):
        image = "registry/blueprint-orchestrator:latest"
        assert deployed_sha._parse_sha_from_image(image) is None

    def test_no_tag_returns_none(self):
        assert deployed_sha._parse_sha_from_image("registry/blueprint-orchestrator") is None

    def test_none_image_returns_none(self):
        assert deployed_sha._parse_sha_from_image(None) is None

    def test_short_hex_tag_rejected(self):
        # Too short to be a git SHA (< 7 chars).
        assert deployed_sha._parse_sha_from_image("registry/svc:abc12") is None

    def test_digest_pinned_ref_returns_none(self):
        # Cloud Run pins the serving revision's image to ``@sha256:<digest>``
        # with NO git tag. The 64-hex digest must NOT be parsed as a git SHA.
        image = (
            "us-central1-docker.pkg.dev/proj/blueprint-repo/"
            f"blueprint-orchestrator@sha256:{_DIGEST}"
        )
        assert deployed_sha._parse_sha_from_image(image) is None

    def test_tag_plus_digest_parses_the_tag(self):
        # A ``:<git-sha>@sha256:<digest>`` ref must yield the git tag, not the digest.
        image = (
            "us-central1-docker.pkg.dev/proj/blueprint-repo/"
            f"blueprint-orchestrator:{_GIT_SHA}@sha256:{_DIGEST}"
        )
        assert deployed_sha._parse_sha_from_image(image) == _GIT_SHA

    def test_bare_64_hex_tag_rejected_as_digest_length(self):
        # Defense-in-depth: a 64-hex value reaching the tag slot is digest-length
        # and must be rejected even without an ``@`` marker.
        image = f"registry/blueprint-orchestrator:{_DIGEST}"
        assert deployed_sha._parse_sha_from_image(image) is None

    def test_registry_port_not_mistaken_for_tag(self):
        # A registry host ``:port`` must not be parsed as the image tag.
        assert deployed_sha._parse_sha_from_image("host:5000/blueprint-orchestrator") is None


class TestLooksLikeGitSha:
    def test_accepts_git_sha(self):
        assert deployed_sha._looks_like_git_sha(_GIT_SHA) is True
        assert deployed_sha._looks_like_git_sha("abc1234") is True

    def test_rejects_digest(self):
        assert deployed_sha._looks_like_git_sha(_DIGEST) is False

    def test_rejects_non_hex_and_empty(self):
        assert deployed_sha._looks_like_git_sha("latest") is False
        assert deployed_sha._looks_like_git_sha("") is False
        assert deployed_sha._looks_like_git_sha(None) is False


@pytest.mark.asyncio
class TestResolveDeployedSha:
    async def test_prefers_deploy_sha_env(self, monkeypatch, mocker):
        # The authoritative source: DEPLOY_SHA stamped on the revision. When set,
        # no Cloud Run API call is made at all.
        monkeypatch.setenv("DEPLOY_SHA", _GIT_SHA)
        rev = mocker.patch.object(deployed_sha, "_fetch_revision_image")
        svc = mocker.patch.object(deployed_sha, "_fetch_service_image")
        result = await deployed_sha.resolve_deployed_sha()
        assert result == _GIT_SHA
        rev.assert_not_called()
        svc.assert_not_called()

    async def test_ignores_non_sha_deploy_sha_env(self, monkeypatch, mocker):
        # A junk DEPLOY_SHA (e.g. a digest) is ignored, falling through to the
        # image-ref resolution.
        monkeypatch.setenv("DEPLOY_SHA", _DIGEST)
        monkeypatch.setenv("K_REVISION", "blueprint-orchestrator-00042-abc")
        monkeypatch.setenv("GCP_PROJECT_ID", "real-project")
        mocker.patch.object(
            deployed_sha,
            "_fetch_revision_image",
            return_value=f"registry/blueprint-orchestrator:{_GIT_SHA}",
        )
        result = await deployed_sha.resolve_deployed_sha()
        assert result == _GIT_SHA


    async def test_returns_none_when_k_revision_absent(self, monkeypatch, mocker):
        monkeypatch.delenv("DEPLOY_SHA", raising=False)
        monkeypatch.delenv("K_REVISION", raising=False)
        # Ensure the network helpers are never invoked off Cloud Run.
        rev = mocker.patch.object(deployed_sha, "_fetch_revision_image")
        svc = mocker.patch.object(deployed_sha, "_fetch_service_image")
        result = await deployed_sha.resolve_deployed_sha()
        assert result is None
        rev.assert_not_called()
        svc.assert_not_called()

    async def test_local_production_without_deploy_sha_logs_error_and_returns_none(
        self, monkeypatch, mocker
    ):
        # A local-production role that boots without a resolvable deploy
        # identity must be loud about it (the launcher normally fails closed,
        # so this is the misconfiguration backstop). Still never raises or
        # blocks startup.
        monkeypatch.delenv("DEPLOY_SHA", raising=False)
        monkeypatch.delenv("K_REVISION", raising=False)
        monkeypatch.setenv("BLUEPRINT_DEPLOYMENT_MODE", "local-production")
        error_log = mocker.patch.object(deployed_sha.logger, "error")
        rev = mocker.patch.object(deployed_sha, "_fetch_revision_image")
        result = await deployed_sha.resolve_deployed_sha()
        assert result is None
        rev.assert_not_called()
        error_log.assert_called_once()
        assert "local-production" in error_log.call_args.args[0]

    async def test_returns_none_when_project_missing(self, monkeypatch, mocker):
        monkeypatch.delenv("DEPLOY_SHA", raising=False)
        monkeypatch.setenv("K_REVISION", "blueprint-orchestrator-00042-abc")
        monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
        rev = mocker.patch.object(deployed_sha, "_fetch_revision_image")
        result = await deployed_sha.resolve_deployed_sha()
        assert result is None
        rev.assert_not_called()

    async def test_returns_none_when_project_placeholder(self, monkeypatch, mocker):
        monkeypatch.delenv("DEPLOY_SHA", raising=False)
        monkeypatch.setenv("K_REVISION", "blueprint-orchestrator-00042-abc")
        monkeypatch.setenv("GCP_PROJECT_ID", "your-gcp-project-id")
        rev = mocker.patch.object(deployed_sha, "_fetch_revision_image")
        result = await deployed_sha.resolve_deployed_sha()
        assert result is None
        rev.assert_not_called()

    async def test_resolves_sha_from_revision_image(self, monkeypatch, mocker):
        monkeypatch.delenv("DEPLOY_SHA", raising=False)
        monkeypatch.setenv("K_REVISION", "blueprint-orchestrator-00042-abc")
        monkeypatch.setenv("GCP_PROJECT_ID", "real-project")
        mocker.patch.object(
            deployed_sha,
            "_fetch_revision_image",
            return_value=(
                "us-central1-docker.pkg.dev/real-project/blueprint-repo/"
                "blueprint-orchestrator:deadbeef123"
            ),
        )
        svc = mocker.patch.object(deployed_sha, "_fetch_service_image")
        result = await deployed_sha.resolve_deployed_sha()
        assert result == "deadbeef123"
        # Revision tag resolved — the service-template fallback is not needed.
        svc.assert_not_called()

    async def test_falls_back_to_service_template_for_digest_pinned_revision(
        self, monkeypatch, mocker
    ):
        # The real production shape: the serving revision is digest-pinned (no git
        # tag), so resolution must fall back to the service template image, which
        # retains the deployed ``:<git-sha>`` tag.
        monkeypatch.delenv("DEPLOY_SHA", raising=False)
        monkeypatch.setenv("K_REVISION", "blueprint-orchestrator-00409-p84")
        monkeypatch.setenv("GCP_PROJECT_ID", "real-project")
        mocker.patch.object(
            deployed_sha,
            "_fetch_revision_image",
            return_value=(
                "us-central1-docker.pkg.dev/real-project/blueprint-repo/"
                f"blueprint-orchestrator@sha256:{_DIGEST}"
            ),
        )
        mocker.patch.object(
            deployed_sha,
            "_fetch_service_image",
            return_value=(
                "us-central1-docker.pkg.dev/real-project/blueprint-repo/"
                f"blueprint-orchestrator:{_GIT_SHA}"
            ),
        )
        result = await deployed_sha.resolve_deployed_sha()
        assert result == _GIT_SHA


class TestApprovedActivationExecutor:
    """The host executor must close approved activations with evidence."""

    _PROMOTED: ClassVar[dict] = {
        "version_number": 30,
        "status": "promoted",
        "promoted_sha": _GIT_SHA,
        "serving_status": "unconfirmed",
        "activation_requested_at": "2026-08-09T00:00:00Z",
    }

    def test_records_receipt_with_deployed_sha(self, monkeypatch):
        calls = []
        monkeypatch.setattr(deploy_blueprint, "_require_write_config", lambda _role: None)
        monkeypatch.setattr(
            deploy_blueprint,
            "_read_live_approved_activation",
            lambda _args: self._PROMOTED,
        )
        monkeypatch.setattr(
            deploy_blueprint,
            "_fetch_deploy_manifest",
            lambda _sha: {
                "run": {"databaseId": 1234},
                "digest": "sha256:" + "0" * 64,
                "revision": _GIT_SHA,
            },
        )
        monkeypatch.setattr(
            deploy_blueprint,
            "_invoke_release_shim",
            lambda digest, revision, *, host: calls.append(
                ("shim", digest, revision, host)
            ),
        )
        monkeypatch.setattr(deploy_blueprint, "_local_served_sha", lambda: _GIT_SHA)
        monkeypatch.setattr(
            deploy_blueprint,
            "get_project_id",
            lambda _project_id=None: "project-1",
        )

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body))
            return {
                "version_number": 30,
                "serving_status": "serving_confirmed",
                "served_sha": _GIT_SHA,
            }

        monkeypatch.setattr(deploy_blueprint, "api", fake_api)

        assert deploy_blueprint._deploy_agent_once(
            type("Args", (), {"project_id": None})()
        ) is True
        assert calls == [
            ("shim", "sha256:" + "0" * 64, _GIT_SHA, None),
            (
                "POST",
                "/api/admin/projects/project-1/activation-receipt",
                {
                    "version_number": 30,
                    "revision": _GIT_SHA,
                    "host": "control-plane",
                },
            ),
        ]

    def test_failed_activation_is_reported_not_timeout(self, monkeypatch):
        APIError = deploy_blueprint.APIError

        monkeypatch.setattr(deploy_blueprint, "_require_write_config", lambda _role: None)
        monkeypatch.setattr(
            deploy_blueprint,
            "_read_live_approved_activation",
            lambda _args: self._PROMOTED,
        )
        monkeypatch.setattr(
            deploy_blueprint,
            "_fetch_deploy_manifest",
            lambda _sha: {
                "run": {"databaseId": 1234},
                "digest": "sha256:" + "0" * 64,
                "revision": _GIT_SHA,
            },
        )
        handoffs = []

        def fail_release(digest, revision):
            handoffs.append((digest, revision))
            raise APIError("release failed with exit code 17")

        monkeypatch.setattr(deploy_blueprint, "_request_release_socket", fail_release)
        receipt_calls = []
        monkeypatch.setattr(
            deploy_blueprint,
            "api",
            lambda method, path, body=None, timeout=30: receipt_calls.append(
                (method, path, body)
            ),
        )

        with pytest.raises(APIError, match="release failed with exit code 17"):
            deploy_blueprint._deploy_agent_once(
                type("Args", (), {"project_id": None})()
            )
        assert handoffs == [("sha256:" + "0" * 64, _GIT_SHA)]
        assert not receipt_calls


@pytest.mark.asyncio
class TestResolveDeployedShaRemainder:
    async def test_returns_none_when_no_source_yields_sha(self, monkeypatch, mocker):
        monkeypatch.delenv("DEPLOY_SHA", raising=False)
        monkeypatch.setenv("K_REVISION", "blueprint-orchestrator-00042-abc")
        monkeypatch.setenv("GCP_PROJECT_ID", "real-project")
        mocker.patch.object(
            deployed_sha,
            "_fetch_revision_image",
            return_value="registry/blueprint-orchestrator:latest",
        )
        mocker.patch.object(
            deployed_sha,
            "_fetch_service_image",
            return_value=f"registry/blueprint-orchestrator@sha256:{_DIGEST}",
        )
        result = await deployed_sha.resolve_deployed_sha()
        assert result is None

    async def test_returns_none_when_client_raises(self, monkeypatch, mocker):
        monkeypatch.delenv("DEPLOY_SHA", raising=False)
        monkeypatch.setenv("K_REVISION", "blueprint-orchestrator-00042-abc")
        monkeypatch.setenv("GCP_PROJECT_ID", "real-project")
        mocker.patch.object(
            deployed_sha,
            "_fetch_revision_image",
            side_effect=RuntimeError("Cloud Run API unavailable"),
        )
        mocker.patch.object(
            deployed_sha,
            "_fetch_service_image",
            side_effect=RuntimeError("Cloud Run API unavailable"),
        )
        # Must swallow the exceptions and degrade to None, never propagate.
        result = await deployed_sha.resolve_deployed_sha()
        assert result is None

    async def test_service_template_recovers_when_revision_fetch_raises(
        self, monkeypatch, mocker
    ):
        monkeypatch.delenv("DEPLOY_SHA", raising=False)
        monkeypatch.setenv("K_REVISION", "blueprint-orchestrator-00042-abc")
        monkeypatch.setenv("GCP_PROJECT_ID", "real-project")
        mocker.patch.object(
            deployed_sha,
            "_fetch_revision_image",
            side_effect=RuntimeError("transient"),
        )
        mocker.patch.object(
            deployed_sha,
            "_fetch_service_image",
            return_value=f"registry/blueprint-orchestrator:{_GIT_SHA}",
        )
        result = await deployed_sha.resolve_deployed_sha()
        assert result == _GIT_SHA


class TestAtMostOnceReleaseFence:
    """The deploy agent must invoke the release shim exactly once even when the
    root executor outlives the client socket timeout.

    Drives _request_release_socket over a real Unix socket with a genuine
    timeout — the sleep outlives the patched 1-second timeout so the fence
    fires exactly as it does in production.  Cost-only side effects (real
    deploys, network egress) are stubbed; the elapsed-time relationship is not.
    """

    _PROMOTED: ClassVar[dict] = {
        "version_number": 30,
        "status": "promoted",
        "promoted_sha": _GIT_SHA,
        "serving_status": "unconfirmed",
        "activation_requested_at": "2026-08-09T00:00:00Z",
    }

    def test_slow_release_is_accepted_exactly_once(self, monkeypatch, tmp_path):
        import socket as _sock
        import threading
        import time as _time

        socket_path = str(tmp_path / "release.sock")
        state_path = str(tmp_path / "deploy-agent.state")
        digest = "sha256:" + "0" * 64

        # Real Unix socket server: accepts the client request, then sleeps past
        # the patched 1-second client timeout before responding.
        server_ready = threading.Event()
        requests_received: list[str] = []

        def _slow_server() -> None:
            srv = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
            try:
                srv.bind(socket_path)
                srv.listen(1)
                server_ready.set()
                conn, _ = srv.accept()
                try:
                    data = b""
                    while b"\n" not in data:
                        chunk = conn.recv(256)
                        if not chunk:
                            break
                        data += chunk
                    requests_received.append(data.decode("ascii").strip())
                    _time.sleep(3.0)  # outlives the patched 1-second client timeout
                    conn.sendall(b"OK\n")
                finally:
                    conn.close()
            finally:
                srv.close()

        server_thread = threading.Thread(target=_slow_server, daemon=True)
        server_thread.start()
        assert server_ready.wait(timeout=3), "socket server failed to start"

        # Patch elapsed-time parameters so the test completes in ~1 second.
        monkeypatch.setattr(deploy_blueprint, "_RELEASE_SOCKET_PATH", socket_path)
        monkeypatch.setattr(deploy_blueprint, "_RELEASE_SOCKET_TIMEOUT_SECONDS", 1)

        # Stub expensive side effects — never the elapsed-time relationship.
        monkeypatch.setattr(deploy_blueprint, "_require_write_config", lambda _a: None)
        monkeypatch.setattr(
            deploy_blueprint,
            "_read_live_approved_activation",
            lambda _a: dict(self._PROMOTED),
        )
        monkeypatch.setattr(
            deploy_blueprint,
            "_fetch_deploy_manifest",
            lambda _sha: {
                "run": {"databaseId": 1234},
                "digest": digest,
                "revision": _GIT_SHA,
            },
        )
        monkeypatch.setattr(
            deploy_blueprint,
            "_validate_deploy_binding",
            lambda _rev, _approved: None,
        )
        monkeypatch.setattr(
            deploy_blueprint, "get_project_id", lambda _project_id=None: "project-1"
        )
        # Second-invocation confirmation: pretend the promoted revision is serving.
        monkeypatch.setattr(deploy_blueprint, "_local_served_sha", lambda: _GIT_SHA)

        receipt_calls: list[tuple] = []

        def _fake_api(method, path, body=None, timeout=30):
            receipt_calls.append((method, path, body))
            return {
                "version_number": 30,
                "serving_status": "serving_confirmed",
                "served_sha": _GIT_SHA,
            }

        monkeypatch.setattr(deploy_blueprint, "api", _fake_api)

        args = type("Args", (), {
            "project_id": None,
            "host": "control-plane",
            "state_path": state_path,
            "poll_state_path": None,
        })()

        # --- First invocation: shim fires, timeout fires mid-recv ---
        result1 = deploy_blueprint._deploy_agent_once(args)

        # 1. Client returns False — must not raise.
        assert result1 is False

        # 2. State persisted at phase='accepted', not 'failed' or stuck at 'started'.
        persisted = deploy_blueprint._read_deploy_agent_state(state_path)
        assert persisted is not None
        assert persisted.get("phase") == "accepted"

        # --- Second invocation: must not re-invoke the shim ---
        deploy_blueprint._deploy_agent_once(args)

        # 3. The socket received exactly one request across both invocations.
        assert len(requests_received) == 1

        # 4. Exactly one receipt was posted.
        receipt_posts = [
            c for c in receipt_calls
            if c[0] == "POST" and "activation-receipt" in (c[1] or "")
        ]
        assert len(receipt_posts) == 1

        server_thread.join(timeout=5)

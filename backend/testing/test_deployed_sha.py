"""Tests for the best-effort runtime deployed-SHA resolver.

All network calls are mocked — these tests never reach the real Cloud Run API.
"""

import pytest

from app import deployed_sha

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

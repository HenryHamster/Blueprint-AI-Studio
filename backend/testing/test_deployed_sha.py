"""Tests for the best-effort runtime deployed-SHA resolver.

All network calls are mocked — these tests never reach the real Cloud Run API.
"""

import pytest

from app import deployed_sha


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


@pytest.mark.asyncio
class TestResolveDeployedSha:
    async def test_returns_none_when_k_revision_absent(self, monkeypatch, mocker):
        monkeypatch.delenv("K_REVISION", raising=False)
        # Ensure the network helper is never invoked off Cloud Run.
        fetch = mocker.patch.object(deployed_sha, "_fetch_revision_image")
        result = await deployed_sha.resolve_deployed_sha()
        assert result is None
        fetch.assert_not_called()

    async def test_returns_none_when_project_missing(self, monkeypatch, mocker):
        monkeypatch.setenv("K_REVISION", "blueprint-orchestrator-00042-abc")
        monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
        fetch = mocker.patch.object(deployed_sha, "_fetch_revision_image")
        result = await deployed_sha.resolve_deployed_sha()
        assert result is None
        fetch.assert_not_called()

    async def test_returns_none_when_project_placeholder(self, monkeypatch, mocker):
        monkeypatch.setenv("K_REVISION", "blueprint-orchestrator-00042-abc")
        monkeypatch.setenv("GCP_PROJECT_ID", "your-gcp-project-id")
        fetch = mocker.patch.object(deployed_sha, "_fetch_revision_image")
        result = await deployed_sha.resolve_deployed_sha()
        assert result is None
        fetch.assert_not_called()

    async def test_resolves_sha_from_revision_image(self, monkeypatch, mocker):
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
        result = await deployed_sha.resolve_deployed_sha()
        assert result == "deadbeef123"

    async def test_returns_none_when_image_has_no_sha_tag(self, monkeypatch, mocker):
        monkeypatch.setenv("K_REVISION", "blueprint-orchestrator-00042-abc")
        monkeypatch.setenv("GCP_PROJECT_ID", "real-project")
        mocker.patch.object(
            deployed_sha,
            "_fetch_revision_image",
            return_value="registry/blueprint-orchestrator:latest",
        )
        result = await deployed_sha.resolve_deployed_sha()
        assert result is None

    async def test_returns_none_when_client_raises(self, monkeypatch, mocker):
        monkeypatch.setenv("K_REVISION", "blueprint-orchestrator-00042-abc")
        monkeypatch.setenv("GCP_PROJECT_ID", "real-project")
        mocker.patch.object(
            deployed_sha,
            "_fetch_revision_image",
            side_effect=RuntimeError("Cloud Run API unavailable"),
        )
        # Must swallow the exception and degrade to None, never propagate.
        result = await deployed_sha.resolve_deployed_sha()
        assert result is None

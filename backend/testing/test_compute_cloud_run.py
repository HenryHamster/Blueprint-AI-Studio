"""Tests for compute_cloud_run helpers."""

import asyncio
import sys
import types
import pytest


def test_build_duration_raises_on_missing_protobuf(monkeypatch):
    """_build_duration must raise ImportError when google.protobuf is absent."""
    # Remove google.protobuf from sys.modules so the lazy import fails
    saved = {}
    for key in list(sys.modules):
        if key == "google.protobuf" or key.startswith("google.protobuf."):
            saved[key] = sys.modules.pop(key)

    # Also stub google so the import path resolves up to the point of the sub-import
    google_stub = types.ModuleType("google")
    monkeypatch.setitem(sys.modules, "google", google_stub)
    monkeypatch.setitem(sys.modules, "google.protobuf", None)  # type: ignore[arg-type]

    try:
        from app.compute_cloud_run import _build_duration  # noqa: PLC0415

        with pytest.raises(ImportError, match="google.protobuf"):
            _build_duration(300)
    finally:
        # Restore saved modules
        for key, mod in saved.items():
            sys.modules[key] = mod


def test_no_plaintext_credentials_in_env_overrides(monkeypatch):
    """launch_agent_cloud_run_job must never emit AGENT_SECRET_TOKEN or LLM
    credential values as plaintext RunJobRequest override values."""
    import app.compute_cloud_run as ccr

    # Capture the overrides passed to the (stubbed) Cloud Run client.
    captured_overrides: list[dict] = []

    class _FakeEnvVar:
        def __init__(self, name: str, value: str) -> None:
            self.name = name
            self.value = value

    class _FakeContainerOverride:
        def __init__(self, env: list) -> None:
            self.env = env

    class _FakeOverrides:
        def __init__(self, *, task_count, timeout, container_overrides):
            for co in container_overrides:
                for ev in co.env:
                    captured_overrides.append({"name": ev.name, "value": ev.value})

    class _FakeRequest:
        def __init__(self, name: str, overrides) -> None:
            pass

    class _FakeOperation:
        class operation:
            name = "op/test"

    class _FakeJobsClient:
        def run_job(self, request):
            return _FakeOperation()

    class _FakeRunJobRequest:
        Overrides = _FakeOverrides
        Overrides.ContainerOverride = _FakeContainerOverride
        def __init__(self, name: str, overrides):
            pass

    fake_run_v2 = types.ModuleType("google.cloud.run_v2")
    fake_run_v2.JobsClient = _FakeJobsClient  # type: ignore[attr-defined]
    fake_run_v2.RunJobRequest = _FakeRunJobRequest  # type: ignore[attr-defined]
    fake_run_v2.EnvVar = _FakeEnvVar  # type: ignore[attr-defined]

    # Stub durations
    fake_duration_pb2 = types.ModuleType("google.protobuf.duration_pb2")

    class _FakeDuration:
        def __init__(self, seconds: int) -> None:
            self.seconds = seconds

    fake_duration_pb2.Duration = _FakeDuration  # type: ignore[attr-defined]

    # Stub compute_manifest helpers
    class _FakeMaterials:
        provider_name = "anthropic"
        auth_mode_value = "oauth"
        secret_env = {"CLAUDE_CODE_OAUTH_TOKEN": "live-oauth-token"}
        cli_translation = None

    def _fake_resolve_provider_materials(**kwargs):
        return _FakeMaterials()

    def _fake_apply_llm_env(
        env, *, model, execution_mode, provider_settings, materials
    ):
        env["LLM_MODEL"] = model or "claude-opus-4-8"
        # Simulate secret_env merge that the real function does last
        env.update(materials.secret_env)

    fake_manifest = types.ModuleType("app.compute_manifest")
    fake_manifest._resolve_provider_materials = _fake_resolve_provider_materials  # type: ignore[attr-defined]
    fake_manifest._apply_llm_env = _fake_apply_llm_env  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "google.cloud.run_v2", fake_run_v2)
    monkeypatch.setitem(sys.modules, "google.protobuf.duration_pb2", fake_duration_pb2)
    monkeypatch.setitem(sys.modules, "app.compute_manifest", fake_manifest)
    monkeypatch.setattr(ccr, "CLOUD_RUN_JOB_NAME", "projects/p/locations/us/jobs/agent")

    # Stub LOCAL_MODE and ORCHESTRATOR_URL
    fake_compute = types.ModuleType("app.compute")
    fake_compute.LOCAL_MODE = False  # type: ignore[attr-defined]
    fake_compute.ORCHESTRATOR_URL = "https://orch.example.com"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.compute", fake_compute)

    result = asyncio.get_event_loop().run_until_complete(
        ccr.launch_agent_cloud_run_job(
            vm_id="cr-vm-123",
            task_id="task-abc",
            payload_uri="gs://bucket/payload.json",
            secret_version_name="projects/p/secrets/agent-creds-xyz/versions/3",
            task_type="research",
            oauth_token="live-oauth-token",
            provider="anthropic",
            auth_mode="oauth",
            model="claude-opus-4-8",
        )
    )

    assert result is True

    override_names = {e["name"] for e in captured_overrides}
    override_values = {e["value"] for e in captured_overrides}

    # AGENT_SECRET_TOKEN must never appear as an override key.
    assert "AGENT_SECRET_TOKEN" not in override_names, (
        "AGENT_SECRET_TOKEN must not be injected as a plaintext override"
    )
    # AGENT_SECRET_VERSION must be present so the agent can resolve the JWT.
    assert "AGENT_SECRET_VERSION" in override_names, (
        "AGENT_SECRET_VERSION must be passed so the agent can fetch credentials"
    )
    # LLM credential env var names must not appear as plaintext override keys.
    credential_keys = {
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "GEMINI_API_KEY",
    }
    assert not (credential_keys & override_names), (
        f"LLM credential keys must not appear as plaintext overrides: {credential_keys & override_names}"
    )
    # The actual credential value must not appear as any override value.
    assert "live-oauth-token" not in override_values, (
        "Raw credential value must not appear in any RunJobRequest override value"
    )
    # Non-secret LLM identifiers should still be present.
    assert "PROVIDER" in override_names
    assert "AUTH_MODE" in override_names


def test_file_path_cred_skipped_in_overrides_but_delivered_via_manifest(monkeypatch):
    """File-path credentials must be skipped from env overrides (they cannot be
    carried as override values) but the AGENT_SECRET_VERSION must always be set
    so the agent can resolve the manifest containing file-path credentials."""
    import app.compute_cloud_run as ccr

    captured_overrides: list[dict] = []

    class _FakeEnvVar:
        def __init__(self, name: str, value: str) -> None:
            self.name = name
            self.value = value

    class _FakeContainerOverride:
        def __init__(self, env: list) -> None:
            self.env = env

    class _FakeOverrides:
        def __init__(self, *, task_count, timeout, container_overrides):
            for co in container_overrides:
                for ev in co.env:
                    captured_overrides.append({"name": ev.name, "value": ev.value})

    class _FakeOperation:
        class operation:
            name = "op/test"

    class _FakeJobsClient:
        def run_job(self, request):
            return _FakeOperation()

    class _FakeRunJobRequest:
        Overrides = _FakeOverrides
        Overrides.ContainerOverride = _FakeContainerOverride
        def __init__(self, name: str, overrides):
            pass

    fake_run_v2 = types.ModuleType("google.cloud.run_v2")
    fake_run_v2.JobsClient = _FakeJobsClient  # type: ignore[attr-defined]
    fake_run_v2.RunJobRequest = _FakeRunJobRequest  # type: ignore[attr-defined]
    fake_run_v2.EnvVar = _FakeEnvVar  # type: ignore[attr-defined]

    # Stub durations
    fake_duration_pb2 = types.ModuleType("google.protobuf.duration_pb2")

    class _FakeDuration:
        def __init__(self, seconds: int) -> None:
            self.seconds = seconds

    fake_duration_pb2.Duration = _FakeDuration  # type: ignore[attr-defined]

    # Stub compute_manifest helpers with a file-path credential
    class _FakeMaterials:
        provider_name = "google"
        auth_mode_value = "service_account"
        # Simulate file-path credential that cannot be carried as env override
        secret_env = {"GOOGLE_APPLICATION_CREDENTIALS": "/opt/gcp/creds.json"}
        cli_translation = None

    def _fake_resolve_provider_materials(**kwargs):
        return _FakeMaterials()

    def _fake_apply_llm_env(
        env, *, model, execution_mode, provider_settings, materials
    ):
        env["LLM_MODEL"] = model or "claude-opus-4-8"
        # Simulate secret_env merge including file-path credentials
        env.update(materials.secret_env)

    fake_manifest = types.ModuleType("app.compute_manifest")
    fake_manifest._resolve_provider_materials = _fake_resolve_provider_materials  # type: ignore[attr-defined]
    fake_manifest._apply_llm_env = _fake_apply_llm_env  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "google.cloud.run_v2", fake_run_v2)
    monkeypatch.setitem(sys.modules, "google.protobuf.duration_pb2", fake_duration_pb2)
    monkeypatch.setitem(sys.modules, "app.compute_manifest", fake_manifest)
    monkeypatch.setattr(ccr, "CLOUD_RUN_JOB_NAME", "projects/p/locations/us/jobs/agent")

    fake_compute = types.ModuleType("app.compute")
    fake_compute.LOCAL_MODE = False  # type: ignore[attr-defined]
    fake_compute.ORCHESTRATOR_URL = "https://orch.example.com"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.compute", fake_compute)

    result = asyncio.get_event_loop().run_until_complete(
        ccr.launch_agent_cloud_run_job(
            vm_id="cr-vm-456",
            task_id="task-file-cred",
            payload_uri="gs://bucket/payload.json",
            secret_version_name="projects/p/secrets/agent-creds-gcp/versions/1",
            task_type="research",
            provider="google",
            auth_mode="service_account",
            model="gemini-2.0-pro",
        )
    )

    assert result is True

    override_dict = {e["name"]: e["value"] for e in captured_overrides}

    # File-path credential MUST NOT appear in env overrides
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in override_dict, (
        "File-path credentials must not be passed as env overrides; "
        "value starts with '/' and cannot be carried in Cloud Run RunJobRequest"
    )
    # AGENT_SECRET_VERSION MUST be present so agent can fetch manifest
    assert "AGENT_SECRET_VERSION" in override_dict, (
        "AGENT_SECRET_VERSION must be passed so the agent can resolve "
        "file-path credentials from the Secret Manager manifest"
    )
    # Verify the secret version contains the file-path credential reference
    assert override_dict["AGENT_SECRET_VERSION"] == (
        "projects/p/secrets/agent-creds-gcp/versions/1"
    )
    # Non-secret LLM identifiers should still be present
    assert override_dict.get("PROVIDER") == "google"
    assert override_dict.get("AUTH_MODE") == "service_account"

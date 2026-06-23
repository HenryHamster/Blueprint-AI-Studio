"""Tests for compute_cloud_run helpers."""

import asyncio
import sys
import types
import pytest


def _make_fake_gcp_harness():
    """Return (fake_run_v2, fake_duration_pb2, captured_overrides_list)."""
    captured: list[dict] = []

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
                    captured.append({"name": ev.name, "value": ev.value})

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

    fake_duration_pb2 = types.ModuleType("google.protobuf.duration_pb2")

    class _FakeDuration:
        def __init__(self, seconds: int) -> None:
            self.seconds = seconds

    fake_duration_pb2.Duration = _FakeDuration  # type: ignore[attr-defined]

    return fake_run_v2, fake_duration_pb2, captured


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
    # Also bind the fake as an attribute on the google.cloud package. The lazy
    # `from google.cloud import run_v2` in compute_cloud_run resolves via the
    # package attribute, which is bypassed by a sys.modules-only patch once the
    # real submodule has been imported by an earlier test (cross-test pollution).
    import google.cloud as _google_cloud  # noqa: PLC0415

    monkeypatch.setattr(_google_cloud, "run_v2", fake_run_v2, raising=False)
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
    # Also bind the fake as an attribute on the google.cloud package. The lazy
    # `from google.cloud import run_v2` in compute_cloud_run resolves via the
    # package attribute, which is bypassed by a sys.modules-only patch once the
    # real submodule has been imported by an earlier test (cross-test pollution).
    import google.cloud as _google_cloud  # noqa: PLC0415

    monkeypatch.setattr(_google_cloud, "run_v2", fake_run_v2, raising=False)
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


def test_real_provider_secret_values_never_appear_as_plaintext_overrides(monkeypatch):
    """Integration guard: real provider materials (no compute_manifest stub) through
    launch_agent_cloud_run_job for each provider x auth_mode with a sentinel secret.
    Asserts the sentinel never appears as a plaintext override value.
    Also includes a hostile synthetic case with an unknown credential key not in
    _SECRET_ENV_KEYS — this case must FAIL against the old fail-open code and PASS
    after the fail-closed value guard is added.
    """
    import app.compute_cloud_run as ccr

    fake_run_v2, fake_duration_pb2, captured = _make_fake_gcp_harness()

    import google.cloud as _google_cloud  # noqa: PLC0415

    monkeypatch.setattr(_google_cloud, "run_v2", fake_run_v2, raising=False)
    monkeypatch.setitem(sys.modules, "google.cloud.run_v2", fake_run_v2)
    monkeypatch.setitem(sys.modules, "google.protobuf.duration_pb2", fake_duration_pb2)
    monkeypatch.setattr(ccr, "CLOUD_RUN_JOB_NAME", "projects/p/locations/us/jobs/agent")

    fake_compute = types.ModuleType("app.compute")
    fake_compute.LOCAL_MODE = False  # type: ignore[attr-defined]
    fake_compute.ORCHESTRATOR_URL = "https://orch.example.com"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.compute", fake_compute)

    # Import the REAL compute_manifest module so _resolve_provider_materials and
    # _apply_llm_env run for real (no stub). This exercises the actual provider path.
    import app.compute_manifest as cm_module  # noqa: PLC0415

    def run_case(**kwargs) -> list[dict]:
        captured.clear()
        result = asyncio.get_event_loop().run_until_complete(
            ccr.launch_agent_cloud_run_job(
                vm_id="cr-vm-integ",
                task_id="task-integ-test",
                payload_uri="gs://bucket/payload.json",
                secret_version_name="projects/p/secrets/agent-creds-xyz/versions/1",
                task_type="research",
                **kwargs,
            )
        )
        assert result is True, f"launch_agent_cloud_run_job returned False for {kwargs}"
        return list(captured)

    # Case 1: anthropic / oauth — sentinel is the oauth token
    s1 = "SENTINEL-OAUTH-ANTHROPIC-TOKEN-abc123def456xyz"
    ov1 = run_case(oauth_token=s1, provider="anthropic", auth_mode="oauth")
    values1 = {e["value"] for e in ov1}
    names1 = {e["name"] for e in ov1}
    assert s1 not in values1, "anthropic/oauth sentinel leaked as plaintext override"
    assert "AGENT_SECRET_VERSION" in names1
    assert "PROVIDER" in names1
    assert "AUTH_MODE" in names1

    # Case 2: anthropic / api_key — sentinel injected via module attribute
    s2 = "SENTINEL-APIKEY-ANTHROPIC-sk-ant-def456abc789xyz"
    monkeypatch.setattr(cm_module, "ANTHROPIC_API_KEY", s2)
    ov2 = run_case(provider="anthropic", auth_mode="api_key")
    values2 = {e["value"] for e in ov2}
    assert s2 not in values2, "anthropic/api_key sentinel leaked as plaintext override"

    # Case 3: openai / api_key — sentinel is the API key
    s3 = "sk-SENTINEL-OPENAI-APIKEY-xyz789ghijklmnop"
    ov3 = run_case(oauth_token=s3, provider="openai", auth_mode="api_key")
    values3 = {e["value"] for e in ov3}
    assert s3 not in values3, "openai/api_key sentinel leaked as plaintext override"

    # Case 4: openai / oauth — sentinel is buried in auth.json; secret goes to file
    s4 = "SENTINEL-CODEX-OAUTH-ACCESS-TOKEN-abc123xyz789"
    auth_json4 = f'{{"tokens": {{"access_token": "{s4}"}}}}'
    ov4 = run_case(oauth_token=auth_json4, provider="openai", auth_mode="oauth")
    values4 = {e["value"] for e in ov4}
    assert s4 not in values4, "openai/oauth sentinel leaked as plaintext override"

    # Case 5: gemini / api_key — sentinel is the API key
    s5 = "AIzaSENTINEL-GEMINI-APIKEY-xyz123abc456def789"
    ov5 = run_case(oauth_token=s5, provider="gemini", auth_mode="api_key")
    values5 = {e["value"] for e in ov5}
    assert s5 not in values5, "gemini/api_key sentinel leaked as plaintext override"

    # Case 6: gemini / service_account — secret goes to file; no plaintext in env
    s6 = "SENTINEL-SA-PRIVATE-KEY-xyz789abc123def456"
    sa_json6 = (
        '{"type":"service_account",'
        '"client_email":"test@proj.iam.gserviceaccount.com",'
        f'"private_key":"{s6}"}}'
    )
    ov6 = run_case(
        oauth_token=sa_json6,
        provider="gemini",
        auth_mode="service_account",
        provider_metadata={"project": "test-project", "location": "us-central1"},
    )
    values6 = {e["value"] for e in ov6}
    assert s6 not in values6, (
        "gemini/service_account sentinel leaked as plaintext override"
    )

    # --- Hostile / synthetic case ---
    # Monkeypatch _resolve_provider_materials so the resolved materials.secret_env
    # contains a brand-new key NOT in _SECRET_ENV_KEYS. The value-based fail-closed
    # guard must suppress it even though the key has never been seen before.
    s_hostile = "SENTINEL-FUTURE-CRED-KEY-xyz789abc123def456ghi"
    from app.compute_manifest import (  # noqa: PLC0415
        _ProviderMaterials,
        _resolve_provider_materials as _real_resolve,
    )

    def _hostile_resolve(**kwargs):
        real = _real_resolve(**kwargs)
        augmented = dict(real.secret_env)
        # FUTURE_PROVIDER_CREDENTIAL_KEY is intentionally absent from _SECRET_ENV_KEYS
        augmented["FUTURE_PROVIDER_CREDENTIAL_KEY"] = s_hostile
        return _ProviderMaterials(
            provider_name=real.provider_name,
            auth_mode_value=real.auth_mode_value,
            files=real.files,
            cli_translation=real.cli_translation,
            secret_env=augmented,
        )

    monkeypatch.setattr(cm_module, "_resolve_provider_materials", _hostile_resolve)
    ov_h = run_case(
        oauth_token="SENTINEL-OAUTH-FOR-HOSTILE-CASE-anthropic-abc123",
        provider="anthropic",
        auth_mode="oauth",
    )
    values_h = {e["value"] for e in ov_h}
    assert s_hostile not in values_h, (
        f"Fail-closed value guard failed: FUTURE_PROVIDER_CREDENTIAL_KEY={s_hostile!r} "
        "appeared as a plaintext override — a credential key unknown to _SECRET_ENV_KEYS "
        "was not suppressed by the value-based guard"
    )

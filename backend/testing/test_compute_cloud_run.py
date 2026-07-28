"""Tests for compute_cloud_run helpers."""

import asyncio
import sys
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.managed_compute import LocalOnlyExecutionPlacement, ManagedComputeDisabled


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
            captured.extend(
                {"name": ev.name, "value": ev.value}
                for co in container_overrides
                for ev in co.env
            )

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


@pytest.mark.asyncio
async def test_managed_compute_disabled_blocks_cloud_run_job_before_client(monkeypatch):
    """The kill switch affects Jobs, not the Cloud Run web service."""
    import app.compute_cloud_run as ccr
    from app import compute
    from app.runtime_config_state import runtime_config

    monkeypatch.setitem(runtime_config, "managed_compute_enabled", False)
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    monkeypatch.setattr(ccr, "CLOUD_RUN_JOB_NAME", "projects/p/locations/us/jobs/a")

    with pytest.raises(ManagedComputeDisabled, match="managed compute is disabled"):
        await ccr.launch_agent_cloud_run_job(
            vm_id="cloud-run-managed-gate",
            task_id="task-abc",
            payload_uri=None,
            secret_version_name=None,
            task_type="research",
        )


@pytest.mark.asyncio
async def test_final_local_only_fence_is_rethrown_without_cloud_run_submission(
    monkeypatch,
):
    """A placement flip after staging must never become a GCE fallback."""
    import app.compute_cloud_run as ccr
    import app.managed_compute as managed_compute
    from app import compute

    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    monkeypatch.setattr(ccr, "CLOUD_RUN_JOB_NAME", "projects/p/locations/us/jobs/a")
    denied = AsyncMock(
        side_effect=LocalOnlyExecutionPlacement("project is local_only")
    )
    monkeypatch.setattr(
        managed_compute,
        "assert_task_managed_execution_allowed_authoritative",
        denied,
    )

    class _NeverSubmit:
        def run_job(self, request):
            raise AssertionError("Cloud Run must not be submitted after local_only")

    import google.cloud.run_v2 as run_v2

    monkeypatch.setattr(run_v2, "JobsClient", _NeverSubmit)

    with pytest.raises(LocalOnlyExecutionPlacement, match="local_only"):
        await ccr.launch_agent_cloud_run_job(
            vm_id="cloud-run-placement-fence",
            task_id=uuid.uuid4(),
            payload_uri=None,
            secret_version_name=None,
            task_type="research",
        )

    denied.assert_awaited_once()


def test_build_duration_raises_on_missing_protobuf(monkeypatch):
    """_build_duration must raise ImportError when google.protobuf is absent."""
    # Hide google.protobuf so the lazy import inside _build_duration fails.
    #
    # monkeypatch must own EVERY sys.modules mutation here. A manual
    # pop/restore mixed with monkeypatch.setitem leaves the interpreter in a
    # broken state: the manual `finally` restore runs first, then monkeypatch
    # undo deletes "google.protobuf" because the key was already absent when
    # setitem recorded it. That strands live submodules (google.protobuf.message,
    # .json_format, ...) in sys.modules while their parent package is gone, so
    # the next `import google.protobuf` builds a *fresh* package object whose
    # submodule attributes are never re-bound -- and every later google.cloud
    # import in the session dies on
    # `AttributeError: module 'google.protobuf' has no attribute 'message'`.
    # monkeypatch undoes in LIFO order, so delitem-then-setitem on the same key
    # unwinds correctly.
    for key in list(sys.modules):
        if key == "google.protobuf" or key.startswith("google.protobuf."):
            monkeypatch.delitem(sys.modules, key)

    # Also stub google so the import path resolves up to the point of the sub-import
    google_stub = types.ModuleType("google")
    monkeypatch.setitem(sys.modules, "google", google_stub)
    monkeypatch.setitem(sys.modules, "google.protobuf", None)  # type: ignore[arg-type]

    from app.compute_cloud_run import _build_duration

    with pytest.raises(ImportError, match=r"google\.protobuf"):
        _build_duration(300)


@pytest.fixture
def allow_cloud_run_payload_launch(monkeypatch):
    """Isolate Cloud Run request-payload tests from durable placement lookup."""
    import app.managed_compute as managed_compute

    async def allow_managed_execution(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        managed_compute,
        "assert_task_managed_execution_allowed_authoritative",
        allow_managed_execution,
    )


def test_no_plaintext_credentials_in_env_overrides(
    monkeypatch, allow_cloud_run_payload_launch
):
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
            captured_overrides.extend(
                {"name": ev.name, "value": ev.value}
                for co in container_overrides
                for ev in co.env
            )

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
        provider_name: str = "anthropic"
        auth_mode_value: str = "oauth"
        secret_env: dict = {"CLAUDE_CODE_OAUTH_TOKEN": "live-oauth-token"}  # noqa: RUF012
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
    import google.cloud as _google_cloud

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


def test_file_path_cred_skipped_in_overrides_but_delivered_via_manifest(
    monkeypatch, allow_cloud_run_payload_launch
):
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
            captured_overrides.extend(
                {"name": ev.name, "value": ev.value}
                for co in container_overrides
                for ev in co.env
            )

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
        provider_name: str = "google"
        auth_mode_value: str = "service_account"
        # Simulate file-path credential that cannot be carried as env override
        secret_env: dict = {"GOOGLE_APPLICATION_CREDENTIALS": "/opt/gcp/creds.json"}  # noqa: RUF012
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
    import google.cloud as _google_cloud

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


def test_real_provider_secret_values_never_appear_as_plaintext_overrides(
    monkeypatch, allow_cloud_run_payload_launch
):
    """Integration guard: real provider materials (no compute_manifest stub) through
    launch_agent_cloud_run_job for each provider x auth_mode with a sentinel secret.
    Asserts the sentinel never appears as a plaintext override value.
    Also includes a hostile synthetic case with an unknown credential key not in
    _SECRET_ENV_KEYS — this case must FAIL against the old fail-open code and PASS
    after the fail-closed value guard is added.
    """
    import app.compute_cloud_run as ccr

    fake_run_v2, fake_duration_pb2, captured = _make_fake_gcp_harness()

    import google.cloud as _google_cloud

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
    import app.compute_manifest as cm_module

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
    from app.compute_manifest import (
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


class TestLaunchAgentCloudRunJob:
    """Tests for compute_cloud_run.launch_agent_cloud_run_job."""

    @pytest.fixture(autouse=True)
    def _allow_cloud_run_payload_launch(self, allow_cloud_run_payload_launch):
        """Keep request-payload tests independent of durable task lookup."""

    @pytest.mark.asyncio
    async def test_launch_injects_agent_secret_version(self, monkeypatch):
        """When secret_version_name is provided, AGENT_SECRET_VERSION must be
        passed as an env var so the agent can fetch secrets at startup.
        Plaintext credentials are never injected as env overrides.
        """
        from app import compute as _compute_module
        from app import compute_cloud_run

        # Disable LOCAL_MODE so the function proceeds past the early-return guard.
        monkeypatch.setattr(_compute_module, "LOCAL_MODE", False)

        # Capture env vars passed to run_job.
        captured_env: list[tuple[str, str]] = []

        class FakeEnvVar:
            def __init__(self, name, value):
                self.name = name
                self.value = value

        class FakeContainerOverride:
            def __init__(self, env=None):
                self.env = env or []

        class FakeOverrides:
            ContainerOverride = FakeContainerOverride

            def __init__(self, task_count=1, timeout=None, container_overrides=None):
                self.task_count = task_count
                self.timeout = timeout
                self.container_overrides = container_overrides or []

        class FakeRunJobRequest:
            Overrides = FakeOverrides

            def __init__(self, name, overrides):
                self.name = name
                self.overrides = overrides

        def fake_run_job(request):
            captured_env.extend(
                (env_var.name, env_var.value)
                for co in request.overrides.container_overrides
                for env_var in co.env
            )
            op = MagicMock()
            op.operation.name = "operations/fake"
            return op

        mock_run_client = MagicMock()
        mock_run_client.run_job.side_effect = fake_run_job

        mock_run_module = MagicMock()
        mock_run_module.JobsClient.return_value = mock_run_client
        mock_run_module.RunJobRequest = FakeRunJobRequest
        mock_run_module.EnvVar = FakeEnvVar

        import google.cloud

        monkeypatch.setattr(google.cloud, "run_v2", mock_run_module, raising=False)
        monkeypatch.setattr(
            compute_cloud_run,
            "CLOUD_RUN_JOB_NAME",
            "projects/p/locations/us/jobs/agent",
        )

        result = await compute_cloud_run.launch_agent_cloud_run_job(
            vm_id="vm-001",
            task_id="task-001",
            payload_uri="gs://bucket/payload.json",
            secret_version_name="projects/p/secrets/s/versions/1",
            task_type="research",
        )

        assert result is True
        env_dict = dict(captured_env)
        assert "AGENT_SECRET_VERSION" in env_dict, (
            f"AGENT_SECRET_VERSION must be injected; got env keys: {list(env_dict)}"
        )
        assert env_dict["AGENT_SECRET_VERSION"] == "projects/p/secrets/s/versions/1"
        # Plaintext secrets must never be injected
        assert "AGENT_SECRET_TOKEN" not in env_dict
        assert "ANTHROPIC_API_KEY" not in env_dict

    @pytest.mark.asyncio
    async def test_no_secret_version_when_not_provided(self, monkeypatch):
        """When secret_version_name is None, AGENT_SECRET_VERSION is not injected."""
        import google.cloud
        from app import compute as _compute_module
        from app import compute_cloud_run

        monkeypatch.setattr(_compute_module, "LOCAL_MODE", False)

        captured_env: list = []

        class FakeClient:
            def run_job(self, *, request):
                for co in request.overrides.container_overrides:
                    captured_env.extend(co.env)
                op = MagicMock()
                op.operation.name = "fake-op"
                return op

        mock_run_module = MagicMock()
        mock_run_module.JobsClient.return_value = FakeClient()

        monkeypatch.setattr(google.cloud, "run_v2", mock_run_module, raising=False)
        monkeypatch.setattr(
            compute_cloud_run,
            "CLOUD_RUN_JOB_NAME",
            "projects/p/locations/us/jobs/agent",
        )

        result = await compute_cloud_run.launch_agent_cloud_run_job(
            vm_id="vm-002",
            task_id="task-002",
            payload_uri=None,
            secret_version_name=None,
            task_type="research",
        )

        assert result is True, "must return True when no secret is needed"
        env_dict = {e.name: e.value for e in captured_env}
        assert "AGENT_SECRET_VERSION" not in env_dict, (
            "AGENT_SECRET_VERSION must not be injected when secret_version_name is None"
        )

    @pytest.mark.asyncio
    async def test_injects_llm_settings_not_plaintext_creds(self, monkeypatch):
        """LLM identifier and settings env vars are injected, but plaintext
        credentials like ANTHROPIC_API_KEY are never included as env overrides."""
        import google.cloud.run_v2 as run_v2_mod
        from app.compute_cloud_run import launch_agent_cloud_run_job
        import app.compute_cloud_run as ccr_mod

        monkeypatch.setattr(
            ccr_mod, "CLOUD_RUN_JOB_NAME", "projects/p/locations/us/jobs/agent"
        )
        monkeypatch.setattr("app.compute_manifest.ANTHROPIC_API_KEY", "sk-ant-test123")
        # Ensure not LOCAL_MODE
        monkeypatch.setattr("app.compute.LOCAL_MODE", False)

        submitted_env: list = []

        class FakeClient:
            def run_job(self, *, request):
                for co in request.overrides.container_overrides:
                    submitted_env.extend(co.env)
                op = MagicMock()
                op.operation.name = "fake-op"
                return op

        with patch.object(run_v2_mod, "JobsClient", return_value=FakeClient()):
            result = await launch_agent_cloud_run_job(
                vm_id="cr-vm-1",
                task_id="task-abc",
                payload_uri=None,
                secret_version_name="projects/p/secrets/s/versions/1",
                task_type="research",
                provider="anthropic",
                auth_mode="api_key",
                provider_settings={},
            )

        assert result is True
        env_dict = {e.name: e.value for e in submitted_env}
        assert env_dict.get("PROVIDER") == "anthropic", (
            f"PROVIDER missing from env overrides; got {env_dict}"
        )
        assert env_dict.get("AUTH_MODE") == "api_key", (
            f"AUTH_MODE missing; got {env_dict}"
        )
        # ANTHROPIC_API_KEY must NOT be injected as plaintext
        assert "ANTHROPIC_API_KEY" not in env_dict, (
            f"ANTHROPIC_API_KEY must not be plaintext in env overrides; got {env_dict}"
        )
        assert "LLM_SETTINGS" in env_dict, (
            f"LLM_SETTINGS missing from env overrides; got {env_dict}"
        )
        assert "LLM_CLI_TRANSLATION" in env_dict, (
            f"LLM_CLI_TRANSLATION missing from env overrides; got {env_dict}"
        )
        # AGENT_SECRET_VERSION must be present so agent can fetch the actual API key
        assert env_dict.get("AGENT_SECRET_VERSION") == "projects/p/secrets/s/versions/1"

    @pytest.mark.asyncio
    async def test_oauth_token_not_plaintext_delivered_via_manifest(self, monkeypatch):
        """OAuth auth must NOT inject CLAUDE_CODE_OAUTH_TOKEN as a plaintext env
        override. The token is delivered via the Secret Manager manifest the agent
        resolves from AGENT_SECRET_VERSION at startup (secure-design contract)."""
        import google.cloud.run_v2 as run_v2_mod
        from app.compute_cloud_run import launch_agent_cloud_run_job
        import app.compute_cloud_run as ccr_mod

        monkeypatch.setattr(
            ccr_mod, "CLOUD_RUN_JOB_NAME", "projects/p/locations/us/jobs/agent"
        )
        monkeypatch.setattr("app.compute.LOCAL_MODE", False)

        submitted_env: list = []

        class FakeClient:
            def run_job(self, *, request):
                for co in request.overrides.container_overrides:
                    submitted_env.extend(co.env)
                op = MagicMock()
                op.operation.name = "fake-op"
                return op

        with patch.object(run_v2_mod, "JobsClient", return_value=FakeClient()):
            result = await launch_agent_cloud_run_job(
                vm_id="cr-vm-2",
                task_id="task-def",
                payload_uri=None,
                secret_version_name="projects/p/secrets/s/versions/1",
                task_type="research",
                oauth_token="sk-ant-oauth-tok",
                provider="anthropic",
                auth_mode="oauth",
                provider_settings={},
            )

        assert result is True
        env_dict = {e.name: e.value for e in submitted_env}
        assert env_dict.get("PROVIDER") == "anthropic"
        assert env_dict.get("AUTH_MODE") == "oauth"
        # The OAuth token (and its raw value) must NEVER be a plaintext override —
        # it is delivered via the Secret Manager manifest, not the RunJobRequest.
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env_dict, (
            f"CLAUDE_CODE_OAUTH_TOKEN must not be a plaintext override; got {env_dict}"
        )
        assert "sk-ant-oauth-tok" not in set(env_dict.values()), (
            f"Raw OAuth token value must not appear in any env override; got {env_dict}"
        )
        # AGENT_SECRET_VERSION must be present so the agent can resolve the token.
        assert env_dict.get("AGENT_SECRET_VERSION") == "projects/p/secrets/s/versions/1"

    @pytest.mark.asyncio
    async def test_skips_file_path_credentials(self, monkeypatch):
        """File-path credential env vars (value starts with '/') are not injected."""
        import google.cloud.run_v2 as run_v2_mod
        from app.compute_cloud_run import launch_agent_cloud_run_job
        from app.compute_manifest import _ProviderMaterials
        import app.compute_cloud_run as ccr_mod

        monkeypatch.setattr(
            ccr_mod, "CLOUD_RUN_JOB_NAME", "projects/p/locations/us/jobs/agent"
        )
        monkeypatch.setattr("app.compute.LOCAL_MODE", False)

        submitted_env: list = []

        class FakeClient:
            def run_job(self, *, request):
                for co in request.overrides.container_overrides:
                    submitted_env.extend(co.env)
                op = MagicMock()
                op.operation.name = "fake-op"
                return op

        # Patch _resolve_provider_materials to return a Gemini-style file-path credential
        fake_materials = _ProviderMaterials(
            provider_name="gemini",
            auth_mode_value="service_account",
            files=[],
            cli_translation=None,
            secret_env={"GOOGLE_APPLICATION_CREDENTIALS": "/opt/agent/gemini/sa.json"},
        )
        with (
            patch.object(run_v2_mod, "JobsClient", return_value=FakeClient()),
            patch(
                "app.compute_manifest._resolve_provider_materials",
                return_value=fake_materials,
            ),
        ):
            result = await launch_agent_cloud_run_job(
                vm_id="cr-vm-3",
                task_id="task-ghi",
                payload_uri=None,
                secret_version_name=None,
                task_type="research",
                provider="gemini",
                auth_mode="service_account",
                provider_settings={},
            )

        assert result is True
        env_dict = {e.name: e.value for e in submitted_env}
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in env_dict, (
            "File-path credential must not appear in Cloud Run env overrides; "
            f"got {env_dict}"
        )
        assert env_dict.get("PROVIDER") == "gemini"

    @pytest.mark.asyncio
    async def test_no_llm_params_only_baseline_env(self, monkeypatch):
        """Without LLM params, only baseline env vars are injected (no plaintext creds)."""
        import google.cloud.run_v2 as run_v2_mod
        from app.compute_cloud_run import launch_agent_cloud_run_job
        import app.compute_cloud_run as ccr_mod

        monkeypatch.setattr(
            ccr_mod, "CLOUD_RUN_JOB_NAME", "projects/p/locations/us/jobs/agent"
        )
        monkeypatch.setattr("app.compute.LOCAL_MODE", False)

        submitted_env: list = []

        class FakeClient:
            def run_job(self, *, request):
                for co in request.overrides.container_overrides:
                    submitted_env.extend(co.env)
                op = MagicMock()
                op.operation.name = "fake-op"
                return op

        with patch.object(run_v2_mod, "JobsClient", return_value=FakeClient()):
            result = await launch_agent_cloud_run_job(
                vm_id="cr-vm-4",
                task_id="task-jkl",
                payload_uri="gs://bucket/payload.json",
                secret_version_name="projects/p/secrets/s/versions/1",
                task_type="research",
                # No LLM params
            )

        assert result is True
        env_dict = {e.name: e.value for e in submitted_env}
        baseline_keys = {
            "VM_INSTANCE_ID",
            "TASK_ID",
            "ORCHESTRATOR_URL",
            "PAYLOAD_URI",
            "AGENT_SECRET_VERSION",
        }
        assert set(env_dict.keys()) == baseline_keys, (
            f"Expected only baseline env keys without LLM params; got {set(env_dict.keys())}"
        )
        # CLOUD_RUN_JOB is a Cloud Run *reserved* env name (the platform auto-sets
        # it to the job name). Providing it in container_overrides makes run_job
        # fail 400; the agent detects job mode from the platform-set value's
        # presence, so the backend must NOT inject it.
        assert "CLOUD_RUN_JOB" not in env_dict
        # Ensure no plaintext secrets are injected
        assert "AGENT_SECRET_TOKEN" not in env_dict
        assert "ANTHROPIC_API_KEY" not in env_dict

    @pytest.mark.asyncio
    async def test_local_mode_returns_true_without_api_call(self, monkeypatch):
        """LOCAL_MODE skips the Cloud Run API entirely and returns True."""
        import google.cloud.run_v2 as run_v2_mod
        from app.compute_cloud_run import launch_agent_cloud_run_job
        import app.compute_cloud_run as ccr_mod

        monkeypatch.setattr(
            ccr_mod, "CLOUD_RUN_JOB_NAME", "projects/p/locations/us/jobs/agent"
        )
        monkeypatch.setattr("app.compute.LOCAL_MODE", True)

        api_called = []

        class FakeClient:
            def run_job(self, *, request):
                api_called.append(True)
                return MagicMock()

        with patch.object(run_v2_mod, "JobsClient", return_value=FakeClient()):
            result = await launch_agent_cloud_run_job(
                vm_id="cr-local",
                task_id="task-local",
                payload_uri=None,
                secret_version_name=None,
                task_type="research",
            )

        assert result is True
        assert not api_called, "Cloud Run API must not be called in LOCAL_MODE"

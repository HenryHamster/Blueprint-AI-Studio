"""
Tests for compute.build_agent_secret_manifest.

Covers provider-specific credential materialisation, file injection, and the
.claude.json onboarding-bypass behaviour for Anthropic.
"""

import uuid


from app import compute


class TestAgentSecretManifest:
    def test_build_agent_secret_manifest_includes_runtime_identity(self):
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-123",
            task_id=task_id,
            oauth_token="sk-ant-test",
            task_type="create",
            provider="anthropic",
            auth_mode="oauth",
            model="claude-sonnet-4-6",
            execution_mode="advisor",
            project_id="project-456",
        )

        assert manifest["env"]["PROVIDER"] == "anthropic"
        assert manifest["env"]["AUTH_MODE"] == "oauth"
        assert manifest["env"]["LLM_MODEL"] == "claude-sonnet-4-6"
        assert manifest["env"]["LLM_EXECUTION_MODE"] == "advisor"
        assert manifest["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-test"
        assert manifest["env"]["TASK_ID"] == str(task_id)
        assert manifest["env"]["PROJECT_ID"] == "project-456"
        assert manifest["metadata"]["task_type"] == "create"
        assert manifest["metadata"]["project_id"] == "project-456"
        # Anthropic always materializes ~/.claude.json (even without claude_config_json)
        # so the CLI doesn't stall on the onboarding dialog.
        assert len(manifest["files"]) == 1
        assert manifest["files"][0]["path"] == "/home/agent/.claude.json"

    def test_build_agent_secret_manifest_materializes_file_based_provider_auth(self):
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-openai",
            task_id=task_id,
            oauth_token='{"tokens":{"access_token":"abc"}}',
            task_type="create",
            provider="openai",
            auth_mode="oauth",
            model="gpt-5.4",
        )

        assert manifest["env"]["PROVIDER"] == "openai"
        assert manifest["env"]["AUTH_MODE"] == "oauth"
        assert manifest["env"]["CODEX_HOME"] == "/opt/agent/openai"
        assert manifest["files"] == [
            {
                "path": "/opt/agent/openai/auth.json",
                "content": '{"tokens":{"access_token":"abc"}}',
                "mode": "0600",
            }
        ]

    def test_build_agent_secret_manifest_maps_zai_to_claude_compat_env(self):
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-zai",
            task_id=task_id,
            oauth_token="zai-test-key",
            task_type="create",
            provider="zai",
            auth_mode="api_key",
            model="glm-5.2",
            provider_metadata={"anthropic_base_url": "https://example.test/anthropic/"},
            provider_settings={"model": "glm-5.2", "effort": "high"},
        )

        assert manifest["env"]["PROVIDER"] == "zai"
        assert manifest["env"]["AUTH_MODE"] == "api_key"
        assert manifest["env"]["LLM_MODEL"] == "glm-5.2"
        assert manifest["env"]["ZAI_API_KEY"] == "zai-test-key"
        assert (
            manifest["env"]["ZAI_ANTHROPIC_BASE_URL"]
            == "https://example.test/anthropic"
        )
        assert manifest["env"]["ANTHROPIC_API_KEY"] == "zai-test-key"
        assert manifest["env"]["ANTHROPIC_AUTH_TOKEN"] == "zai-test-key"
        assert manifest["env"]["ANTHROPIC_BASE_URL"] == "https://example.test/anthropic"
        assert manifest["files"] == []

    def test_build_agent_secret_manifest_maps_deepseek_to_claude_compat_env(self):
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-deepseek",
            task_id=task_id,
            oauth_token="deepseek-test-key",
            task_type="ops",
            provider="deepseek",
            auth_mode="api_key",
            model="deepseek-v4-flash",
            provider_metadata={"anthropic_base_url": "https://example.test/deepseek/"},
            provider_settings={"model": "deepseek-v4-flash", "effort": "low"},
        )

        assert manifest["env"]["PROVIDER"] == "deepseek"
        assert manifest["env"]["AUTH_MODE"] == "api_key"
        assert manifest["env"]["LLM_MODEL"] == "deepseek-v4-flash"
        assert manifest["env"]["DEEPSEEK_API_KEY"] == "deepseek-test-key"
        assert (
            manifest["env"]["DEEPSEEK_ANTHROPIC_BASE_URL"]
            == "https://example.test/deepseek"
        )
        assert manifest["env"]["ANTHROPIC_API_KEY"] == "deepseek-test-key"
        assert manifest["env"]["ANTHROPIC_AUTH_TOKEN"] == "deepseek-test-key"
        assert manifest["env"]["ANTHROPIC_BASE_URL"] == "https://example.test/deepseek"
        assert manifest["files"] == []

    def test_build_agent_secret_manifest_includes_provider_metadata_backed_files(self):
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-gemini",
            task_id=task_id,
            oauth_token='{"type":"service_account","client_email":"bot@example.com","private_key":"abc"}',
            task_type="research",
            provider="gemini",
            auth_mode="service_account",
            model="gemini-2.5-pro",
            provider_metadata={"project": "demo-proj", "location": "us-central1"},
        )

        assert (
            manifest["env"]["GOOGLE_APPLICATION_CREDENTIALS"]
            == "/opt/agent/gemini/sa.json"
        )
        assert manifest["env"]["GOOGLE_CLOUD_PROJECT"] == "demo-proj"
        assert manifest["env"]["GOOGLE_CLOUD_LOCATION"] == "us-central1"
        assert manifest["files"][0]["path"] == "/opt/agent/gemini/sa.json"
        assert '"client_email":"bot@example.com"' in manifest["files"][0]["content"]

    def test_build_agent_secret_manifest_carries_provider_settings_bag(self):
        """Re-target hands the agent the resolved account's provider settings
        bag: a provided (even empty) bag must surface as LLM_SETTINGS + a CLI
        translation, and the runtime identity must reflect the resolved account
        — this is the material warm re-targeting reuses to re-credential a VM."""
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-retarget",
            task_id=task_id,
            oauth_token="sk-ant-retarget",
            task_type="create",
            provider="anthropic",
            auth_mode="oauth",
            model="claude-opus-4-8",
            provider_settings={},
        )

        # Resolved-account identity carried for re-credentialing the warm VM.
        assert manifest["env"]["PROVIDER"] == "anthropic"
        assert manifest["env"]["AUTH_MODE"] == "oauth"
        assert manifest["env"]["LLM_MODEL"] == "claude-opus-4-8"
        assert manifest["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-retarget"
        # An explicit (even empty) settings bag opts into the settings-bag
        # protocol: LLM_SETTINGS is emitted and a CLI translation is computed.
        assert manifest["env"]["LLM_SETTINGS"] == "{}"
        assert "LLM_CLI_TRANSLATION" in manifest["env"]
        assert manifest["metadata"]["model"] == "claude-opus-4-8"

    def test_anthropic_omitted_claude_config_json_materializes_minimal_file(self):
        """Anthropic provider without claude_config_json still produces a
        ~/.claude.json file entry so the CLI doesn't stall on onboarding."""
        import json as _json
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-no-config",
            task_id=task_id,
            oauth_token="sk-ant-oauth01-xyz",
            task_type="create",
            provider="anthropic",
            auth_mode="oauth",
        )
        files = manifest["files"]
        assert len(files) == 1
        assert files[0]["path"] == "/home/agent/.claude.json"
        # Content must not carry secrets — only the structural config shell
        content = _json.loads(files[0]["content"])
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in files[0]["content"]
        assert "sk-ant" not in files[0]["content"]
        assert isinstance(content, dict)

    def test_anthropic_supplied_claude_config_json_preserved(self):
        """When claude_config_json is supplied, it is forwarded verbatim
        (the VM-side normalizer merges hasTrustDialogAccepted on top)."""
        import json as _json
        operator_config = _json.dumps(
            {"hasCompletedOnboarding": True, "email": "op@example.com"}
        )
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-with-config",
            task_id=task_id,
            oauth_token="sk-ant-oauth01-abc",
            task_type="fix",
            provider="anthropic",
            auth_mode="oauth",
            claude_config_json=operator_config,
        )
        files = manifest["files"]
        assert len(files) == 1
        assert files[0]["path"] == "/home/agent/.claude.json"
        content = _json.loads(files[0]["content"])
        assert content["hasCompletedOnboarding"] is True
        assert content["email"] == "op@example.com"
        # Operator fields preserved; trust injection happens in materialize_manifest.py
        assert "sk-ant" not in files[0]["content"]

    def test_anthropic_claude_json_path_is_home_anchored(self):
        """The manifest path must use /home/agent so the agent runner can
        re-anchor it to the live $HOME (e.g. /home inside a bwrap sandbox)."""
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-home-anchor",
            task_id=task_id,
            oauth_token="sk-ant-oauth01-def",
            task_type="create",
            provider="anthropic",
            auth_mode="oauth",
        )
        claude_file = next(
            (f for f in manifest["files"] if f["path"].endswith(".claude.json")), None
        )
        assert claude_file is not None, "expected .claude.json in manifest files"
        # Canonical home-agent path so re-anchoring logic matches '.claude.json' tail
        assert claude_file["path"] == "/home/agent/.claude.json"

    def test_non_anthropic_omitted_claude_config_json_no_file(self):
        """Non-Anthropic providers without claude_config_json must NOT add a
        .claude.json file — OpenAI/ZAI/DeepSeek/Gemini don't need the onboarding bypass."""
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-openai-no-config",
            task_id=task_id,
            oauth_token='{"tokens":{"access_token":"abc"}}',
            task_type="create",
            provider="openai",
            auth_mode="oauth",
        )
        claude_file = next(
            (f for f in manifest["files"] if f["path"].endswith(".claude.json")), None
        )
        assert claude_file is None, "non-Anthropic provider must not add .claude.json"

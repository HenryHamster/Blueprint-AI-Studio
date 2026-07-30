"""Security and database-mode tests for the local API service."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import local_api_service as api

_TEMP_PARENT = Path(__file__).parent if os.name == "nt" else None


class LocalApiServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_TEMP_PARENT)
        self.root = Path(self.temp.name)
        self.secrets = self.root / "api-secrets.json"
        self.google_credentials = self.root / "api-gcp.json"
        self.payload = {
            "DATABASE_URL": (
                "postgresql+asyncpg://user:p%40ss@10.0.0.2:5432/"
                "blueprint?ssl=require"
            ),
            "AGENT_JWT_SECRET": "a" * 32,
            "WS_TICKET_SECRET": "w" * 32,
            "GITHUB_APP_ID": "12345",
            "GITHUB_CLIENT_ID": "client-id",
            "GITHUB_CLIENT_SECRET": "client-secret",
            "GITHUB_PRIVATE_KEY": (
                "-----BEGIN PRIVATE KEY-----\nkey-material\n"
                "-----END PRIVATE KEY-----"
            ),
            "GITHUB_WEBHOOK_SECRET": "webhook-secret",
            "GITHUB_PAT_ENCRYPTION_KEY": "fernet-key",
            "ADMIN_API_KEY": "admin-api-key",
            "METRICS_TOKEN": "metrics-token",
            "GCP_PROJECT_ID": "project-123",
            "ARTIFACT_BUCKET_NAME": "project-123-agent-artifacts",
            "ORCHESTRATOR_URL": "https://orchestrator.example",
            "ALLOWED_ORIGINS": "https://orchestrator.example",
        }
        self.secrets.write_text(json.dumps(self.payload), encoding="utf-8")
        self.google_credentials.write_text(
            json.dumps(
                {
                    "type": "service_account",
                    "project_id": "project-123",
                    "client_email": (
                        "blueprint-local-api-runtime@project-123."
                        "iam.gserviceaccount.com"
                    ),
                }
            ),
            encoding="utf-8",
        )
        if os.name != "nt":
            self.secrets.chmod(0o600)
            self.google_credentials.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def _environment(self, *, database_mode: str) -> dict[str, str]:
        return {
            "BLUEPRINT_API_IMAGE_ID": f"sha256:{'a' * 64}",
            "BLUEPRINT_API_SECRETS_FILE": str(self.secrets),
            "BLUEPRINT_API_GOOGLE_CREDENTIALS_FILE": str(
                self.google_credentials
            ),
            "BLUEPRINT_CONTROL_PLANE_NETWORK": "blueprint-control-plane",
            "BLUEPRINT_API_PORT": "8080",
            "BLUEPRINT_DATABASE_MODE": database_mode,
        }

    @patch.object(api, "_assert_container_slot")
    @patch.object(api.worker, "_network_gateway", return_value="172.20.0.1")
    @patch.object(api.worker, "_assert_proxy_reachable_from_network")
    @patch.object(api.worker, "_wait_for_proxy")
    @patch.object(api.worker, "_assert_no_host_ci_runner")
    def test_local_postgres_api_is_loopback_only_bounded_and_not_leader_eligible(
        self, _ci_guard, wait, network_probe, _gateway, container_slot
    ):
        with patch.dict(
            os.environ,
            self._environment(database_mode="local-postgres"),
            clear=True,
        ):
            argv, child_env = api.command()

        wait.assert_not_called()
        network_probe.assert_not_called()
        container_slot.assert_called_once_with(
            f"sha256:{'a' * 64}", "blueprint-control-plane"
        )
        self.assertEqual(
            argv[argv.index("--publish") + 1], "127.0.0.1:8080:8080"
        )
        self.assertIn("blueprint-control-plane", argv)
        self.assertNotIn("--add-host", argv)
        self.assertEqual(child_env["BLUEPRINT_PROCESS_ROLE"], "api")
        self.assertEqual(
            child_env["BLUEPRINT_DEPLOYMENT_MODE"], "local-production"
        )
        self.assertIn(
            "@blueprint-postgres:5432/", child_env["DATABASE_URL"]
        )
        self.assertIn("ssl=disable", child_env["DATABASE_URL"])
        self.assertIn("--memory", argv)
        self.assertIn("--memory-swap", argv)
        self.assertIn("--cpus", argv)
        self.assertIn("--pids-limit", argv)
        self.assertIn("json-file", argv)
        self.assertIn("max-size=10m", argv)
        self.assertIn("max-file=3", argv)
        command_text = " ".join(argv)
        self.assertNotIn(self.payload["DATABASE_URL"], command_text)
        self.assertNotIn(self.payload["GITHUB_PRIVATE_KEY"], command_text)
        forwarded = {
            argv[index + 1]
            for index, item in enumerate(argv[:-1])
            if item == "--env"
        }
        self.assertFalse(
            {
                "GCP_ZONE",
                "VPC_NETWORK",
                "VPC_SUBNET",
                "AGENT_SERVICE_ACCOUNT_EMAIL",
                "MAX_CONCURRENT_VMS",
                "MAX_PROVISION_SLOTS",
                "MACHINE_TYPE",
                "VM_PROVISIONING_MODEL",
                "CLOUD_RUN_JOB_NAME",
            }
            & forwarded
        )

    @patch.object(api, "_assert_container_slot")
    @patch.object(api.worker, "_network_gateway", return_value="172.20.0.1")
    @patch.object(api.worker, "_assert_proxy_reachable_from_network")
    @patch.object(api.worker, "_wait_for_proxy")
    @patch.object(api.worker, "_assert_no_host_ci_runner")
    def test_cloudsql_mode_preserves_proxy_preflight_and_private_gateway(
        self, _ci_guard, wait, network_probe, _gateway, _container_slot
    ):
        with patch.dict(
            os.environ,
            self._environment(database_mode="cloudsql-proxy"),
            clear=True,
        ):
            argv, child_env = api.command()

        wait.assert_called_once_with("172.20.0.1")
        network_probe.assert_called_once_with(
            f"sha256:{'a' * 64}",
            "blueprint-control-plane",
            "172.20.0.1",
        )
        self.assertIn("host.docker.internal:172.20.0.1", argv)
        self.assertIn(
            "@host.docker.internal:5433/", child_env["DATABASE_URL"]
        )
        self.assertIn("ssl=disable", child_env["DATABASE_URL"])

    def test_mutable_api_image_and_privileged_port_are_rejected(self):
        mutable = self._environment(database_mode="local-postgres")
        mutable["BLUEPRINT_API_IMAGE_ID"] = "blueprint-api:latest"
        with (
            patch.dict(os.environ, mutable, clear=True),
            patch.object(api.worker, "_assert_no_host_ci_runner"),
            self.assertRaisesRegex(ValueError, "immutable"),
        ):
            api.command()

        low_port = self._environment(database_mode="local-postgres")
        low_port["BLUEPRINT_API_PORT"] = "80"
        with (
            patch.dict(os.environ, low_port, clear=True),
            patch.object(api.worker, "_assert_no_host_ci_runner"),
            self.assertRaisesRegex(ValueError, "between 1024 and 65535"),
        ):
            api.command()

    @unittest.skipIf(os.name == "nt", "POSIX ownership and mode contract")
    def test_api_secret_bundle_rejects_group_readability(self):
        self.secrets.chmod(0o640)
        with self.assertRaisesRegex(ValueError, "0600"):
            api._load_api_secrets(str(self.secrets))

    def test_api_secret_bundle_rejects_worker_only_compute_configuration(self):
        self.payload["MAX_CONCURRENT_VMS"] = "10"
        self.secrets.write_text(json.dumps(self.payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsupported keys.*MAX_CONCURRENT_VMS"):
            api._load_api_secrets(str(self.secrets))

    def test_api_secret_bundle_requires_public_http_credentials(self):
        del self.payload["GITHUB_WEBHOOK_SECRET"]
        del self.payload["METRICS_TOKEN"]
        self.secrets.write_text(json.dumps(self.payload), encoding="utf-8")
        with self.assertRaisesRegex(
            ValueError, "GITHUB_WEBHOOK_SECRET.*METRICS_TOKEN"
        ):
            api._load_api_secrets(str(self.secrets))

    def test_api_rejects_dedicated_worker_credentials(self):
        self.google_credentials.write_text(
            json.dumps(
                {
                    "type": "service_account",
                    "project_id": "project-123",
                    "client_email": (
                        "blueprint-local-worker-runtime@project-123."
                        "iam.gserviceaccount.com"
                    ),
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "blueprint-local-api-runtime"):
            api.worker._secure_credentials_file(
                str(self.google_credentials),
                project_id="project-123",
                expected_account_id="blueprint-local-api-runtime",
                credential_label="API",
            )

    @patch.object(api.subprocess, "run")
    def test_foreign_container_at_fixed_name_is_rejected(self, run):
        image = f"sha256:{'a' * 64}"
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "Image": f"sha256:{'b' * 64}",
                        "Config": {
                            "Image": f"sha256:{'b' * 64}",
                            "Labels": {"com.blueprint.role": "api"},
                        },
                        "NetworkSettings": {
                            "Networks": {"blueprint-control-plane": {}}
                        },
                    }
                ]
            ),
            stderr="",
        )
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            api._assert_container_slot(image, "blueprint-control-plane")

    @patch.object(api.subprocess, "run")
    def test_absent_container_slot_is_accepted(self, run):
        run.return_value = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Error: No such object: blueprint-local-api",
        )
        api._assert_container_slot(
            f"sha256:{'a' * 64}", "blueprint-control-plane"
        )


if __name__ == "__main__":
    unittest.main()

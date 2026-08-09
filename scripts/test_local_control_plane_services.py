"""Security and command-boundary tests for the local control-plane services."""

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
import cloud_sql_proxy_service as proxy
import local_worker_service as worker

_TEMP_PARENT = Path(__file__).parent if os.name == "nt" else None


class WorkerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_TEMP_PARENT)
        self.root = Path(self.temp.name)
        self.secrets = self.root / "worker-secrets.json"
        self.google_credentials = self.root / "worker-gcp.json"
        self.payload = {
            "DATABASE_URL": "postgresql+asyncpg://user:p%40ss@10.0.0.2:5432/blueprint",
            "AGENT_JWT_SECRET": "a" * 32,
            "WS_TICKET_SECRET": "w" * 32,
            "GITHUB_APP_ID": "12345",
            "GITHUB_PRIVATE_KEY": (
                "-----BEGIN PRIVATE KEY-----\nkey-material\n"
                "-----END PRIVATE KEY-----"
            ),
            "GITHUB_PAT_ENCRYPTION_KEY": "fernet-key",
            "GCP_PROJECT_ID": "project-123",
            "GCP_ZONE": "us-central1-a",
            "VPC_NETWORK": "agent-sandbox-vpc",
            "VPC_SUBNET": "agent-sandbox-subnet",
            "AGENT_SERVICE_ACCOUNT_EMAIL": "agent-naked-sa@project-123.iam.gserviceaccount.com",
            "ARTIFACT_BUCKET_NAME": "project-123-agent-artifacts",
            "ORCHESTRATOR_URL": "https://orchestrator.example",
            "LOCAL_MODE": "false",
            "MAX_CONCURRENT_VMS": "10",
            "MAX_PROVISION_SLOTS": "2",
            "MACHINE_TYPE": "n2-standard-2",
            "VM_PROVISIONING_MODEL": "STANDARD",
            "DISABLED_REGIONS": "us-east4;us-west1",
            "CLOUD_RUN_JOB_NAME": "projects/project-123/locations/us-central1/jobs/blueprint-agent-job",
        }
        self.secrets.write_text(json.dumps(self.payload), encoding="utf-8")
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
        if os.name != "nt":
            self.secrets.chmod(0o600)
            self.google_credentials.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def test_database_url_is_rewritten_to_unpublished_host_gateway(self):
        rewritten = worker._database_url_for_container(
            self.payload["DATABASE_URL"], mode="cloudsql-proxy"
        )
        self.assertEqual(
            rewritten,
            (
                "postgresql+asyncpg://user:p%40ss@"
                "host.docker.internal:5433/blueprint?ssl=disable"
            ),
        )
        self.assertNotIn("10.0.0.2", rewritten)

    def test_database_url_disables_client_tls_for_cloud_sql_proxy(self):
        rewritten = worker._database_url_for_container(
            "postgresql+asyncpg://user:pass@10.0.0.2:5432/blueprint"
            "?application_name=worker&ssl=require&sslmode=verify-full",
            mode="cloudsql-proxy",
        )
        self.assertEqual(
            rewritten,
            (
                "postgresql+asyncpg://user:pass@host.docker.internal:5433/"
                "blueprint?application_name=worker&ssl=disable"
            ),
        )
        self.assertNotIn("require", rewritten)
        self.assertNotIn("verify-full", rewritten)

    def test_local_postgres_uses_fixed_private_bridge_alias(self):
        rewritten = worker._database_url_for_container(
            "postgresql+asyncpg://user:pass@untrusted-host:9999/blueprint"
            "?application_name=worker&sslmode=require",
            mode="local-postgres",
        )
        self.assertEqual(
            rewritten,
            (
                "postgresql+asyncpg://user:pass@blueprint-postgres:5432/"
                "blueprint?application_name=worker&ssl=disable"
            ),
        )
        self.assertNotIn("untrusted-host", rewritten)

    def test_database_mode_must_be_explicit_and_known(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "BLUEPRINT_DATABASE_MODE"):
                worker._database_mode()
        with patch.dict(
            os.environ, {"BLUEPRINT_DATABASE_MODE": "inferred-from-host"}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "must be one of"):
                worker._database_mode()

    def test_secret_bundle_rejects_unknown_key(self):
        self.payload["ADMIN_API_KEY"] = "must-not-enter-worker"
        self.secrets.write_text(json.dumps(self.payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsupported keys"):
            worker._load_secrets(str(self.secrets))

    def test_secret_bundle_rejects_api_only_github_oauth_credentials(self):
        self.payload["GITHUB_CLIENT_SECRET"] = "must-not-enter-worker"
        self.secrets.write_text(json.dumps(self.payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsupported keys"):
            worker._load_secrets(str(self.secrets))

    def test_secret_bundle_rejects_missing_production_runtime_config(self):
        del self.payload["GCP_PROJECT_ID"]
        self.secrets.write_text(json.dumps(self.payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "GCP_PROJECT_ID"):
            worker._load_secrets(str(self.secrets))

    def test_worker_rejects_credentials_for_another_service_account(self):
        self.google_credentials.write_text(
            json.dumps(
                {
                    "type": "service_account",
                    "project_id": "project-123",
                    "client_email": "orchestrator-sa@project-123.iam.gserviceaccount.com",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "blueprint-local-worker-runtime"):
            worker._secure_credentials_file(
                str(self.google_credentials),
                project_id="project-123",
                expected_account_id="blueprint-local-worker-runtime",
                credential_label="worker",
            )

    def test_worker_rejects_dedicated_api_credentials(self):
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
        with self.assertRaisesRegex(ValueError, "blueprint-local-worker-runtime"):
            worker._secure_credentials_file(
                str(self.google_credentials),
                project_id="project-123",
                expected_account_id="blueprint-local-worker-runtime",
                credential_label="worker",
            )

    @unittest.skipIf(os.name == "nt", "POSIX ownership and mode contract")
    def test_secret_bundle_rejects_group_readability(self):
        self.secrets.chmod(0o640)
        with self.assertRaisesRegex(ValueError, "0600"):
            worker._load_secrets(str(self.secrets))

    @patch.object(worker, "_network_gateway", return_value="172.20.0.1")
    @patch.object(worker, "_assert_proxy_reachable_from_network")
    @patch.object(worker, "_wait_for_proxy")
    @patch.object(worker, "_assert_no_active_user_worker_service")
    @patch.object(worker, "_assert_no_host_ci_runner")
    def test_container_has_no_published_ports_and_is_hardened(
        self, _ci_guard, _user_worker_guard, _wait, _network_probe, _gateway
    ):
        env = {
            "BLUEPRINT_WORKER_IMAGE_ID": f"sha256:{'a' * 64}",
            "BLUEPRINT_CONTROL_PLANE_NETWORK": "blueprint-control-plane",
            "BLUEPRINT_WORKER_SECRETS_FILE": str(self.secrets),
            "BLUEPRINT_WORKER_GOOGLE_CREDENTIALS_FILE": str(self.google_credentials),
            "BLUEPRINT_DATABASE_MODE": "cloudsql-proxy",
            "BLUEPRINT_WORKER_DEPLOY_SHA": "b" * 40,
        }
        with patch.dict(os.environ, env, clear=True):
            argv, child_env = worker.command()
        self.assertNotIn("--publish", argv)
        self.assertNotIn("-p", argv)
        self.assertIn("host.docker.internal:172.20.0.1", argv)
        self.assertNotIn("host-gateway", argv)
        self.assertIn("--read-only", argv)
        self.assertIn("no-new-privileges", argv)
        self.assertEqual(argv[argv.index("--memory") + 1], "4g")
        self.assertEqual(argv[argv.index("--memory-swap") + 1], "4g")
        self.assertEqual(argv[argv.index("--log-driver") + 1], "json-file")
        log_options = [
            argv[index + 1]
            for index, value in enumerate(argv)
            if value == "--log-opt"
        ]
        self.assertEqual(log_options, ["max-size=10m", "max-file=3"])
        self.assertIn(f"{os.getuid()}:{os.getgid()}", argv)
        self.assertTrue(
            any(
                item.endswith(",dst=/var/run/blueprint/gcp.json,readonly")
                for item in argv
            )
        )
        self.assertEqual(
            child_env["GOOGLE_APPLICATION_CREDENTIALS"],
            "/var/run/blueprint/gcp.json",
        )
        self.assertEqual(child_env["BLUEPRINT_PROCESS_ROLE"], "worker")
        self.assertEqual(child_env["BLUEPRINT_DEPLOYMENT_MODE"], "local-production")
        self.assertEqual(child_env["GCP_PROJECT_ID"], "project-123")
        self.assertEqual(child_env["MAX_CONCURRENT_VMS"], "10")
        self.assertNotIn("ALLOW_PLAINTEXT_CREDENTIALS", child_env)
        self.assertNotIn(self.payload["GITHUB_PRIVATE_KEY"], " ".join(argv))
        self.assertNotIn(self.payload["DATABASE_URL"], " ".join(argv))

    @patch.object(worker, "_oci_revision_label")
    @patch.object(worker, "_network_gateway", return_value="172.20.0.1")
    @patch.object(worker, "_assert_proxy_reachable_from_network")
    @patch.object(worker, "_wait_for_proxy")
    @patch.object(worker, "_assert_no_active_user_worker_service")
    @patch.object(worker, "_assert_no_host_ci_runner")
    def test_worker_forwards_deploy_sha_to_container(
        self, _ci_guard, _user_worker_guard, _wait, _network_probe, _gateway, _oci_label
    ):
        env = {
            "BLUEPRINT_WORKER_IMAGE_ID": f"sha256:{'a' * 64}",
            "BLUEPRINT_CONTROL_PLANE_NETWORK": "blueprint-control-plane",
            "BLUEPRINT_WORKER_SECRETS_FILE": str(self.secrets),
            "BLUEPRINT_WORKER_GOOGLE_CREDENTIALS_FILE": str(self.google_credentials),
            "BLUEPRINT_DATABASE_MODE": "local-postgres",
        }

        # Present and valid: exported to the container so the leader's
        # deploy-state health tick can reconcile DeploymentState.current_sha.
        with patch.dict(
            os.environ,
            {**env, "BLUEPRINT_WORKER_DEPLOY_SHA": "b" * 40},
            clear=True,
        ):
            argv, child_env = worker.command()
        self.assertEqual(child_env["DEPLOY_SHA"], "b" * 40)
        self.assertIn("DEPLOY_SHA", argv)
        _oci_label.assert_not_called()

        # Absent env var: the pinned image's OCI revision label is the
        # fallback identity source for first-boot/rollback role environments.
        _oci_label.return_value = "c" * 40
        with patch.dict(os.environ, env, clear=True):
            argv, child_env = worker.command()
        self.assertEqual(child_env["DEPLOY_SHA"], "c" * 40)
        self.assertIn("DEPLOY_SHA", argv)
        _oci_label.assert_called_once_with(f"sha256:{'a' * 64}")

        # Malformed values fail closed rather than stamping a bogus SHA.
        with patch.dict(
            os.environ,
            {**env, "BLUEPRINT_WORKER_DEPLOY_SHA": "not-a-sha"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "full git SHA"):
                worker.command()

    @patch.object(worker, "_oci_revision_label", return_value=None)
    @patch.object(worker, "_network_gateway", return_value="172.20.0.1")
    @patch.object(worker, "_assert_proxy_reachable_from_network")
    @patch.object(worker, "_wait_for_proxy")
    @patch.object(worker, "_assert_no_active_user_worker_service")
    @patch.object(worker, "_assert_no_host_ci_runner")
    def test_missing_deploy_sha_does_not_silently_start(
        self, _ci_guard, _user_worker_guard, _wait, _network_probe, _gateway, _oci_label
    ):
        """A role must never start with an unknown deploy identity.

        With no explicit BLUEPRINT_WORKER_DEPLOY_SHA and no OCI revision label
        on the pinned image, ``command()`` must fail closed instead of booting
        a container that reports serving SHA 'unknown' forever — which would
        time out every promoted version's serving confirmation against a stale
        singleton.
        """
        env = {
            "BLUEPRINT_WORKER_IMAGE_ID": f"sha256:{'a' * 64}",
            "BLUEPRINT_CONTROL_PLANE_NETWORK": "blueprint-control-plane",
            "BLUEPRINT_WORKER_SECRETS_FILE": str(self.secrets),
            "BLUEPRINT_WORKER_GOOGLE_CREDENTIALS_FILE": str(self.google_credentials),
            "BLUEPRINT_DATABASE_MODE": "local-postgres",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "unknown deploy identity"):
                worker.command()
        _oci_label.assert_called_once_with(f"sha256:{'a' * 64}")

    @patch.object(worker, "_oci_revision_label")
    def test_resolve_deploy_sha_prefers_env_then_image_label_then_fails_closed(
        self, _label
    ):
        image = f"sha256:{'a' * 64}"
        # The role-specific env var is authoritative when present and valid.
        with patch.dict(
            os.environ, {"BLUEPRINT_WORKER_DEPLOY_SHA": "d" * 40}, clear=True
        ):
            self.assertEqual(
                worker._resolve_deploy_sha("BLUEPRINT_WORKER_DEPLOY_SHA", image),
                "d" * 40,
            )
        _label.assert_not_called()

        # Absent env var falls back to the pinned image's OCI revision label.
        _label.return_value = "e" * 40
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                worker._resolve_deploy_sha("BLUEPRINT_WORKER_DEPLOY_SHA", image),
                "e" * 40,
            )
        _label.assert_called_once_with(image)

        # A label that is not a full 40-hex SHA is not a valid identity.
        _label.return_value = "not-a-sha"
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "unknown deploy identity"):
                worker._resolve_deploy_sha("BLUEPRINT_WORKER_DEPLOY_SHA", image)

        # A malformed env value fails closed before any image lookup.
        with patch.dict(
            os.environ, {"BLUEPRINT_WORKER_DEPLOY_SHA": "short"}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "full git SHA"):
                worker._resolve_deploy_sha("BLUEPRINT_WORKER_DEPLOY_SHA", image)

    @patch.object(worker.subprocess, "run")
    def test_oci_revision_label_parses_image_inspect(self, run):
        image = f"sha256:{'a' * 64}"
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [{"Config": {"Labels": {"org.opencontainers.image.revision": "c" * 40}}}]
            ),
            stderr="",
        )
        self.assertEqual(worker._oci_revision_label(image), "c" * 40)
        self.assertEqual(run.call_args.args[0], ["docker", "image", "inspect", image])

        # Missing label degrades to None; the caller decides fail-closed.
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps([{"Config": {"Labels": {}}}]),
            stderr="",
        )
        self.assertIsNone(worker._oci_revision_label(image))

        # Docker failure also degrades to None.
        run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="error")
        self.assertIsNone(worker._oci_revision_label(image))

    @patch.object(worker, "_network_gateway", return_value="172.20.0.1")
    @patch.object(worker, "_assert_proxy_reachable_from_network")
    @patch.object(worker, "_wait_for_proxy")
    @patch.object(worker, "_assert_no_active_user_worker_service")
    @patch.object(worker, "_assert_no_host_ci_runner")
    def test_local_postgres_mode_never_uses_proxy_or_host_gateway(
        self, _ci_guard, _user_worker_guard, wait, network_probe, _gateway
    ):
        env = {
            "BLUEPRINT_WORKER_IMAGE_ID": f"sha256:{'a' * 64}",
            "BLUEPRINT_CONTROL_PLANE_NETWORK": "blueprint-control-plane",
            "BLUEPRINT_WORKER_SECRETS_FILE": str(self.secrets),
            "BLUEPRINT_WORKER_GOOGLE_CREDENTIALS_FILE": str(self.google_credentials),
            "BLUEPRINT_DATABASE_MODE": "local-postgres",
            "BLUEPRINT_WORKER_DEPLOY_SHA": "b" * 40,
        }
        with patch.dict(os.environ, env, clear=True):
            argv, child_env = worker.command()
        wait.assert_not_called()
        network_probe.assert_not_called()
        self.assertNotIn("--add-host", argv)
        self.assertIn("@blueprint-postgres:5432/", child_env["DATABASE_URL"])
        self.assertIn("ssl=disable", child_env["DATABASE_URL"])

    @patch.object(worker.subprocess, "run")
    def test_network_probe_fails_closed_when_host_firewall_blocks_bridge(self, run):
        run.return_value.returncode = 1
        with self.assertRaisesRegex(RuntimeError, "host firewall"):
            worker._assert_proxy_reachable_from_network(
                f"sha256:{'a' * 64}",
                "blueprint-control-plane",
                "172.20.0.1",
            )
        argv = run.call_args.args[0]
        self.assertIn("blueprint-control-plane", argv)
        self.assertIn("host.docker.internal:172.20.0.1", argv)
        self.assertNotIn(str(self.secrets), argv)
        self.assertNotIn(str(self.google_credentials), argv)

    def test_mutable_image_tag_is_rejected(self):
        env = {
            "BLUEPRINT_WORKER_IMAGE_ID": "blueprint-worker:latest",
            "BLUEPRINT_CONTROL_PLANE_NETWORK": "blueprint-control-plane",
            "BLUEPRINT_WORKER_SECRETS_FILE": str(self.secrets),
            "BLUEPRINT_WORKER_GOOGLE_CREDENTIALS_FILE": str(self.google_credentials),
            "BLUEPRINT_DATABASE_MODE": "cloudsql-proxy",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(worker, "_assert_no_host_ci_runner"),
            patch.object(worker, "_assert_no_active_user_worker_service"),
        ):
            with self.assertRaisesRegex(ValueError, "immutable"):
                worker.command()

    @patch.object(worker, "_running_as_system_worker_service", return_value=True)
    @patch.object(worker, "_user_scoped_worker_process_present", return_value=False)
    @patch.object(worker.subprocess, "run")
    def test_system_worker_rejects_active_user_worker(
        self, run, _proc_guard, _system_scope
    ):
        run.return_value.returncode = 0
        with self.assertRaisesRegex(RuntimeError, "active user-scoped"):
            worker._assert_no_active_user_worker_service()
        self.assertEqual(
            run.call_args.args[0],
            [
                "systemctl",
                "--user",
                "is-active",
                "--quiet",
                "blueprint-worker.service",
            ],
        )

    @patch.object(worker, "_running_as_system_worker_service", return_value=True)
    @patch.object(worker, "_user_scoped_worker_process_present", return_value=True)
    @patch.object(worker.subprocess, "run")
    def test_system_worker_detects_user_worker_when_user_bus_is_unavailable(
        self, run, _proc_guard, _system_scope
    ):
        run.side_effect = OSError("user bus unavailable")
        with self.assertRaisesRegex(RuntimeError, "active user-scoped"):
            worker._assert_no_active_user_worker_service()

    @patch.object(worker, "_running_as_system_worker_service", return_value=False)
    @patch.object(worker.subprocess, "run")
    def test_user_worker_does_not_reject_itself(self, run, _system_scope):
        worker._assert_no_active_user_worker_service()
        run.assert_not_called()

    def test_cgroup_scope_detection_distinguishes_system_and_user_units(self):
        self.assertTrue(
            worker._cgroup_is_system_worker(
                b"0::/system.slice/blueprint-worker.service\n"
            )
        )
        self.assertFalse(
            worker._cgroup_is_system_worker(
                b"0::/user.slice/user-1000.slice/user@1000.service/"
                b"app.slice/blueprint-worker.service\n"
            )
        )

    def test_proc_fallback_finds_only_user_scoped_worker_process(self):
        proc_root = self.root / "proc"
        system_process = proc_root / "101"
        user_process = proc_root / "102"
        system_process.mkdir(parents=True)
        user_process.mkdir()
        (system_process / "cgroup").write_bytes(
            b"0::/system.slice/blueprint-worker.service\n"
        )
        (user_process / "cgroup").write_bytes(
            b"0::/user.slice/user-1000.slice/user@1000.service/"
            b"app.slice/blueprint-worker.service\n"
        )
        self.assertTrue(worker._user_scoped_worker_process_present(proc_root))
        (user_process / "cgroup").write_bytes(
            b"0::/user.slice/user-1000.slice/user@1000.service/app.slice/shell.scope\n"
        )
        self.assertFalse(worker._user_scoped_worker_process_present(proc_root))

    def test_host_ci_runner_is_a_hard_security_gate(self):
        with tempfile.TemporaryDirectory(dir=self.root) as proc:
            process = Path(proc) / "123"
            process.mkdir()
            (process / "cmdline").write_bytes(
                b"/home/runner/actions-runner/bin/Runner.Listener\x00run"
            )
            with (
                patch.object(worker, "Path", wraps=Path) as path_type,
                self.assertRaisesRegex(RuntimeError, "move CI into a VM"),
            ):
                path_type.return_value = Path(proc)
                worker._assert_no_host_ci_runner()


class ProxyServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_TEMP_PARENT)
        self.root = Path(self.temp.name)
        self.binary = self.root / "cloud-sql-proxy"
        self.credentials = self.root / "cloudsql.json"
        self.binary.write_text("binary", encoding="utf-8")
        self.credentials.write_text("{}", encoding="utf-8")
        self.credentials.write_text(
            json.dumps(
                {
                    "type": "service_account",
                    "project_id": "project-123",
                    "client_email": (
                        "blueprint-local-worker@project-123.iam.gserviceaccount.com"
                    ),
                }
            ),
            encoding="utf-8",
        )
        if os.name != "nt":
            self.binary.chmod(0o700)
            self.credentials.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    @patch.object(proxy, "_docker_network_gateway", return_value="172.20.0.1")
    def test_proxy_binds_only_private_bridge_gateway(self, _gateway):
        env = {
            "BLUEPRINT_CLOUDSQL_PROXY_BINARY": str(self.binary),
            "BLUEPRINT_CLOUDSQL_PROXY_SHA256": __import__("hashlib").sha256(
                self.binary.read_bytes()
            ).hexdigest(),
            "BLUEPRINT_CLOUDSQL_CREDENTIALS_FILE": str(self.credentials),
            "BLUEPRINT_CLOUDSQL_INSTANCE": "project-123:us-central1:database-1",
            "BLUEPRINT_CONTROL_PLANE_NETWORK": "blueprint-control-plane",
        }
        with patch.dict(os.environ, env, clear=True):
            argv = proxy.command()
        self.assertEqual(argv[argv.index("--address") + 1], "172.20.0.1")
        self.assertNotIn("0.0.0.0", argv)
        self.assertNotIn("127.0.0.1", argv)
        self.assertEqual(argv[argv.index("--port") + 1], "5433")

    @unittest.skipIf(os.name == "nt", "POSIX credential mode contract")
    def test_proxy_rejects_world_readable_credentials(self):
        self.credentials.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "group or other"):
            proxy._secure_file(str(self.credentials), executable=False)

    def test_proxy_rejects_credentials_for_a_broader_identity(self):
        self.credentials.write_text(
            json.dumps(
                {
                    "type": "service_account",
                    "project_id": "project-123",
                    "client_email": "orchestrator-sa@project-123.iam.gserviceaccount.com",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "proxy-only"):
            proxy._validate_credentials(self.credentials, project_id="project-123")

    def test_proxy_rejects_unpinned_binary_content(self):
        with self.assertRaisesRegex(ValueError, "pinned digest"):
            proxy._validate_binary_digest(self.binary, "0" * 64)

    @unittest.skipIf(os.name == "nt", "POSIX executable mode contract")
    def test_proxy_rejects_group_writable_binary(self):
        self.binary.chmod(0o720)
        with self.assertRaisesRegex(ValueError, "writable by group"):
            proxy._secure_file(str(self.binary), executable=True)

    @patch.object(proxy.subprocess, "run")
    def test_proxy_rejects_preexisting_unowned_network(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps(
            [
                {
                    "Labels": {},
                    "IPAM": {"Config": [{"Gateway": "172.20.0.1"}]},
                }
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "not owned"):
            proxy._docker_network_gateway("blueprint-control-plane")


class TerraformIdentityContractTests(unittest.TestCase):
    def test_local_identities_remain_separate_and_least_privilege(self):
        terraform = (
            Path(__file__).parents[1] / "infra" / "terraform" / "iam.tf"
        ).read_text(encoding="utf-8")
        proxy_start = terraform.index(
            'resource "google_service_account" "local_cloudsql_proxy_sa"'
        )
        worker_start = terraform.index(
            'resource "google_service_account" "local_worker_sa"'
        )
        api_start = terraform.index(
            'resource "google_service_account" "local_api_sa"'
        )
        agent_start = terraform.index(
            '# Agent "Naked" Service Account'
        )
        proxy_section = terraform[proxy_start:worker_start]
        worker_section = terraform[worker_start:api_start]
        api_section = terraform[api_start:agent_start]

        self.assertIn('"roles/cloudsql.client"', proxy_section)
        self.assertNotIn("secretmanager", proxy_section)
        self.assertNotIn("compute.instanceAdmin", proxy_section)

        self.assertIn('permissions = ["secretmanager.secrets.create"]', terraform)
        self.assertIn('permissions = ["secretmanager.secrets.list"]', terraform)
        self.assertIn('permissions = ["secretmanager.secrets.delete"]', terraform)
        self.assertIn('"roles/secretmanager.secretAccessor"', worker_section)
        self.assertIn('"roles/secretmanager.secretVersionAdder"', worker_section)
        self.assertIn(
            '"google_project_iam_member" "local_worker_secret_inventory"',
            worker_section,
        )
        self.assertIn(
            '"google_project_iam_member" "local_worker_agent_secret_deleter"',
            worker_section,
        )
        self.assertIn(
            'resource.name.startsWith("projects/${var.project_id}/secrets/agent-creds-")',
            worker_section,
        )
        self.assertIn(
            'resource.name.startsWith("projects/${var.project_id}/secrets/user-")',
            worker_section,
        )
        self.assertIn(
            (
                'resource.name.startsWith("projects/${data.google_project.'
                'local_worker_project.number}/secrets/agent-creds-")'
            ),
            worker_section,
        )
        self.assertIn(
            (
                'resource.name.startsWith("projects/${data.google_project.'
                'local_worker_project.number}/secrets/user-")'
            ),
            worker_section,
        )
        agent_delete_condition_start = worker_section.index(
            "local_worker_agent_secret_condition"
        )
        agent_delete_condition_end = worker_section.index(
            '"google_project_iam_member" "local_worker_agent_secret_deleter"'
        )
        agent_delete_condition = worker_section[
            agent_delete_condition_start:agent_delete_condition_end
        ]
        self.assertIn("/secrets/agent-creds-", agent_delete_condition)
        self.assertNotIn("/secrets/user-", agent_delete_condition)
        agent_delete_binding_start = agent_delete_condition_end
        agent_delete_binding_end = worker_section.index(
            '"google_project_iam_member" "local_worker_managed_secret_accessor"'
        )
        agent_delete_binding = worker_section[
            agent_delete_binding_start:agent_delete_binding_end
        ]
        self.assertIn(
            "expression  = local.local_worker_agent_secret_condition",
            agent_delete_binding,
        )
        self.assertNotIn("roles/secretmanager.admin", worker_section)
        self.assertNotIn("roles/iam.serviceAccountTokenCreator", worker_section)
        self.assertNotIn("roles/run.admin", worker_section)
        self.assertIn("roles/iam.serviceAccountTokenCreator", api_section)
        self.assertNotIn("roles/compute.instanceAdmin.v1", api_section)
        self.assertNotIn("roles/cloudbuild.builds.editor", api_section)
        self.assertNotIn("roles/run.jobsExecutorWithOverrides", api_section)
        self.assertNotIn("local_worker_sa.email", api_section)

    def test_terraform_plan_gate_propagates_pipeline_failure(self):
        workflow = (
            Path(__file__).parents[1] / ".github" / "workflows" / "terraform.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("set -o pipefail", workflow)
        self.assertIn("plan_exit_code=$?", workflow)
        self.assertIn('exit "$plan_exit_code"', workflow)
        self.assertIn("if: always() && github.event_name == 'pull_request'", workflow)
        self.assertIn(
            "TF_VAR_alert_email: ${{ secrets.GCP_ALERT_EMAIL }}", workflow
        )
        self.assertIn(
            "TF_VAR_billing_account_id: ${{ secrets.GCP_BILLING_ACCOUNT_ID }}",
            workflow,
        )
        self.assertIn('TF_VAR_monthly_budget_usd: "200"', workflow)
        self.assertIn("Validate production plan inputs", workflow)
        self.assertIn('if [[ -z "${!input_name:-}" ]]', workflow)
        self.assertNotIn("- apply", workflow)

    def test_terraform_plan_identity_has_read_only_refresh_permissions(self):
        workload_identity = (
            Path(__file__).parents[1]
            / "infra"
            / "terraform"
            / "workload_identity.tf"
        ).read_text(encoding="utf-8")
        plan_start = workload_identity.index(
            'resource "google_project_iam_member" "github_actions_plan_viewer"'
        )
        impersonation_start = workload_identity.index(
            'resource "google_service_account_iam_member" '
            '"github_actions_impersonation"'
        )
        plan_section = workload_identity[plan_start:impersonation_start]

        self.assertIn('"roles/viewer"', plan_section)
        self.assertIn('"roles/iam.securityReviewer"', plan_section)
        self.assertIn('"roles/serviceusage.serviceUsageConsumer"', plan_section)
        self.assertNotIn(".admin", plan_section)
        self.assertNotIn("secretAccessor", plan_section)

    def test_artifact_registry_uses_provider_cleanup_policy_schema(self):
        registry = (
            Path(__file__).parents[1] / "infra" / "terraform" / "registry.tf"
        ).read_text(encoding="utf-8")
        self.assertIn("cleanup_policies {", registry)
        self.assertNotIn("cleanup_policy {", registry)

    def test_local_worker_cutover_preserves_production_database_capacity(self):
        cloud_sql = (
            Path(__file__).parents[1] / "infra" / "terraform" / "cloud_sql.tf"
        ).read_text(encoding="utf-8")
        self.assertIn('tier                        = "db-custom-4-15360"', cloud_sql)
        self.assertIn("retained_backups = 7", cloud_sql)
        self.assertIn("ipv4_enabled                                  = true", cloud_sql)
        self.assertIn('value = "400"', cloud_sql)
        self.assertNotIn('tier                        = "db-g1-small"', cloud_sql)

    def test_local_worker_cutover_allows_cloud_run_api_to_scale_to_zero(self):
        cloud_run = (
            Path(__file__).parents[1] / "infra" / "terraform" / "cloud_run.tf"
        ).read_text(encoding="utf-8")
        self.assertIn("min_instance_count = 0", cloud_run)
        self.assertIn("max_instance_count = 1", cloud_run)
        self.assertIn("max_instance_request_concurrency = 120", cloud_run)

    def test_cloud_run_deploy_authorities_keep_api_role(self):
        root = Path(__file__).parents[1]
        cloud_run = (root / "infra" / "terraform" / "cloud_run.tf").read_text(
            encoding="utf-8"
        )
        cloudbuild = (root / "backend" / "cloudbuild.yaml").read_text(
            encoding="utf-8"
        )
        deploy_workflow = (root / ".github" / "workflows" / "deploy.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('name  = "BLUEPRINT_PROCESS_ROLE"', cloud_run)
        self.assertIn('value = "api"', cloud_run)
        self.assertIn("BLUEPRINT_PROCESS_ROLE=api", cloudbuild)
        self.assertIn(
            "--update-env-vars "
            "DEPLOY_SHA=${{ github.sha }},BLUEPRINT_PROCESS_ROLE=api",
            deploy_workflow,
        )

    def test_monthly_gcp_budget_defaults_to_cost_target(self):
        variables = (
            Path(__file__).parents[1] / "infra" / "terraform" / "variables.tf"
        ).read_text(encoding="utf-8")
        budget_start = variables.index('variable "monthly_budget_usd"')
        budget_block = variables[budget_start : budget_start + 400]
        self.assertIn("default     = 200", budget_block)
        self.assertNotIn("default     = 5000", budget_block)


if __name__ == "__main__":
    unittest.main()

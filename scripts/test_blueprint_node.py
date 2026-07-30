"""Unit tests for the additive blueprint-node lifecycle facade.

These tests inject every systemd interaction.  They verify rendered units and
state files only; no test starts a real service or touches a real credential.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import blueprint_node as node


def _run_ok(record):
    def run(argv, **kwargs):
        record.append(list(argv))
        if argv[-1] == node._LEGACY_RUNNER_UNIT and "is-active" in argv:
            return MagicMock(returncode=3, stdout="inactive\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    return run


class ProfileManifestTests(unittest.TestCase):
    def test_profiles_cover_expected_roles(self):
        self.assertEqual(
            node.PROFILE_ROLES["primary"],
            (
                "node-agent",
                "executor",
                "api",
                "database-proxy",
                "worker",
                "edge",
            ),
        )
        self.assertEqual(node.PROFILE_ROLES["worker"], ("node-agent", "executor", "github-actions"))
        self.assertEqual(node.PROFILE_ROLES["gpu-worker"], ("node-agent", "executor"))
        self.assertEqual(
            node.PROFILE_ROLES["control-plane"], ("database-proxy", "worker")
        )
        for roles in node.PROFILE_ROLES.values():
            if "worker" in roles:
                self.assertNotIn("github-actions", roles)
        self.assertTrue(node.ROLE_SPECS["executor"].executable)
        self.assertFalse(node.ROLE_SPECS["api"].executable)
        self.assertTrue(node.ROLE_SPECS["worker"].executable)

    def test_role_paths_are_separate(self):
        with patch.dict(os.environ, {"BLUEPRINT_NODE_DIR": "/tmp/bp-node-test"}, clear=True):
            self.assertNotEqual(node.role_env_path("executor"), node.role_env_path("api"))
            self.assertEqual(
                node.role_env_path("executor"),
                Path("/tmp/bp-node-test/roles/executor/role.env"),
            )

    def test_node_dir_rejects_systemd_control_characters(self):
        with patch.dict(os.environ, {"BLUEPRINT_NODE_DIR": "/tmp/node\nExecStart=/bad"}, clear=True):
            with self.assertRaisesRegex(ValueError, "control characters"):
                node.node_root()

    def test_gpu_profile_has_conservative_gpu_defaults(self):
        defaults = node.PROFILE_DEFAULTS["gpu-worker"]
        self.assertEqual(defaults["BLUEPRINT_RUNNER_MAX_CONCURRENT_COMPUTE_JOBS"], "1")
        self.assertEqual(defaults["BLUEPRINT_RUNNER_GPUS_PER_JOB"], "1")


class RenderTests(unittest.TestCase):
    def test_executor_unit_has_real_paths_and_role_specific_env(self):
        rendered = node._render_unit(
            "executor", system=False, env_file=Path("/tmp/roles/executor/role.env")
        )
        self.assertIn("Description=Blueprint node role: executor", rendered)
        self.assertIn(f"ExecStart={sys.executable} {node._LOCAL_RUNNER}", rendered)
        self.assertIn(
            f"EnvironmentFile=-{Path('/tmp/roles/executor/role.env')}", rendered
        )
        self.assertNotRegex(rendered, r"@[A-Z_]+@")

    def test_planned_role_cannot_render_a_fake_service(self):
        with self.assertRaisesRegex(ValueError, "planned"):
            node._render_unit("api", system=False, env_file=Path("/tmp/api.env"))

    def test_worker_requires_proxy_and_mandatory_env_file(self):
        rendered = node._render_unit(
            "worker", system=False, env_file=Path("/tmp/roles/worker/role.env")
        )
        self.assertIn("Requires=blueprint-database-proxy.service", rendered)
        self.assertIn("After=blueprint-database-proxy.service", rendered)
        self.assertIn(
            "EnvironmentFile=/tmp/roles/worker/role.env",
            rendered,
        )
        self.assertIn(
            "ExecStartPre=/usr/bin/python3 "
            f"{node._LOCAL_WORKER_SERVICE} --check",
            rendered,
        )
        self.assertIn("UMask=0077", rendered)
        self.assertIn("NoNewPrivileges=true", rendered)
        self.assertIn("RestrictSUIDSGID=true", rendered)
        self.assertIn("LockPersonality=true", rendered)
        self.assertNotIn("EnvironmentFile=-/tmp/roles/worker/role.env", rendered)

    def test_system_unit_adds_host_process_sandboxing(self):
        with patch.object(node, "_system_install_user", return_value="blueprint-control"):
            rendered = node._render_unit(
                "worker",
                system=True,
                env_file=Path("/etc/blueprint/nodes/blueprint-control/worker.env"),
            )
        self.assertIn("User=blueprint-control", rendered)
        self.assertIn("ProtectSystem=strict", rendered)
        self.assertIn("ProtectHome=read-only", rendered)
        self.assertIn("ProtectKernelModules=true", rendered)
        self.assertIn("PrivateDevices=true", rendered)

    def test_system_unit_rejects_an_untrusted_user_name(self):
        with patch.dict(os.environ, {"SUDO_USER": "runner\nUser=root"}, clear=True):
            with self.assertRaisesRegex(ValueError, "POSIX account"):
                node._render_unit("executor", system=True, env_file=Path("/etc/blueprint/role.env"))

    def test_system_install_rejects_root_service_account(self):
        with patch.object(node, "_install_user", return_value="root"), \
             patch.object(node, "_system_user_record", return_value=MagicMock(pw_uid=0)):
            with self.assertRaisesRegex(ValueError, "non-root"):
                node._system_install_user()

    def test_system_root_is_not_controlled_by_node_dir_environment(self):
        with patch.dict(os.environ, {"BLUEPRINT_NODE_DIR": "/tmp/ignored"}, clear=True):
            self.assertEqual(
                node.node_root(system=True, user="runner"),
                Path("/etc/blueprint/nodes/runner"),
            )


class InstallTests(unittest.TestCase):
    def setUp(self):
        # Keep test writes inside the repository. Some Windows sandbox hosts do
        # not grant Python permission to create under the user's global temp dir.
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name) / "node"
        self.units = Path(self.temp.name) / "units"

    def tearDown(self):
        self.temp.cleanup()

    def _patches(self):
        return [
            patch.object(node, "node_root", return_value=self.root),
            patch.object(node, "unit_path", side_effect=lambda role, system: self.units / node.ROLE_SPECS[role].unit_name),
            patch.object(node, "_systemd_available", return_value=True),
        ]

    @staticmethod
    def _control_plane_config():
        return {
            "database-proxy": {
                "BLUEPRINT_CLOUDSQL_PROXY_BINARY": "/home/runner/.local/bin/cloud-sql-proxy",
                "BLUEPRINT_CLOUDSQL_PROXY_SHA256": "a" * 64,
                "BLUEPRINT_CLOUDSQL_CREDENTIALS_FILE": "/home/runner/.blueprint/cloudsql.json",
                "BLUEPRINT_CLOUDSQL_INSTANCE": "project-123:us-central1:database-1",
                "BLUEPRINT_CONTROL_PLANE_NETWORK": "blueprint-control-plane",
            },
            "worker": {
                "BLUEPRINT_WORKER_IMAGE_ID": f"sha256:{'a' * 64}",
                "BLUEPRINT_WORKER_SECRETS_FILE": "/home/runner/.blueprint/worker-secrets.json",
                "BLUEPRINT_WORKER_GOOGLE_CREDENTIALS_FILE": "/home/runner/.blueprint/worker-gcp.json",
                "BLUEPRINT_CONTROL_PLANE_NETWORK": "blueprint-control-plane",
            },
        }

    def test_primary_installs_executor_and_control_plane_but_not_planned_roles(self):
        calls = []
        patches = self._patches()
        for item in patches:
            item.start()
        try:
            rc = node.do_install(
                profile="primary", system=False, token="secret", url="https://orch.example",
                dry_run=False,
                role_configs=self._control_plane_config(),
                _run=_run_ok(calls),
            )
        finally:
            for item in reversed(patches):
                item.stop()

        self.assertEqual(rc, 0)
        executor_unit = self.units / "blueprint-executor.service"
        self.assertTrue(executor_unit.is_file())
        self.assertTrue((self.units / "blueprint-database-proxy.service").is_file())
        self.assertTrue((self.units / "blueprint-worker.service").is_file())
        self.assertFalse((self.units / "blueprint-api.service").exists())
        unit_text = executor_unit.read_text()
        self.assertNotIn("secret", unit_text)
        env_text = (self.root / "roles/executor/role.env").read_text()
        self.assertIn("BLUEPRINT_RUNNER_TOKEN=secret", env_text)
        if os.name != "nt":
            self.assertEqual(
                (self.root / "roles/executor/role.env").stat().st_mode & 0o777,
                0o600,
            )
        manifest = json.loads((self.root / "manifest.json").read_text())
        roles = {item["name"]: item for item in manifest["roles"]}
        self.assertEqual(roles["executor"]["state"], "installable")
        self.assertEqual(roles["api"]["state"], "planned")
        self.assertEqual(roles["worker"]["state"], "installable")
        commands = [" ".join(call) for call in calls]
        self.assertTrue(any("enable --now blueprint-executor.service" in command for command in commands))
        self.assertFalse(any("blueprint-api.service" in command for command in commands))

    def test_control_plane_writes_only_non_secret_role_configuration(self):
        calls = []
        patches = self._patches()
        for item in patches:
            item.start()
        try:
            rc = node.do_install(
                profile="control-plane",
                system=False,
                token=None,
                url="https://orch.example",
                dry_run=False,
                role_configs=self._control_plane_config(),
                _run=_run_ok(calls),
            )
        finally:
            for item in reversed(patches):
                item.stop()
        self.assertEqual(rc, 0)
        worker_env = (self.root / "roles/worker/role.env").read_text()
        self.assertIn("BLUEPRINT_WORKER_SECRETS_FILE=", worker_env)
        self.assertNotIn("DATABASE_URL", worker_env)
        self.assertNotIn("GITHUB_PRIVATE_KEY", worker_env)
        self.assertFalse((self.units / "blueprint-executor.service").exists())

    def test_control_plane_can_coexist_with_legacy_executor(self):
        calls = []

        def active_legacy(argv, **kwargs):
            calls.append(list(argv))
            if argv[-1] == node._LEGACY_RUNNER_UNIT:
                return MagicMock(returncode=0, stdout="active\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        patches = self._patches()
        for item in patches:
            item.start()
        try:
            rc = node.do_install(
                profile="control-plane",
                system=False,
                token=None,
                url="https://orch.example",
                dry_run=False,
                role_configs=self._control_plane_config(),
                _run=active_legacy,
            )
        finally:
            for item in reversed(patches):
                item.stop()
        self.assertEqual(rc, 0)
        self.assertFalse(
            any(call[-1] == node._LEGACY_RUNNER_UNIT for call in calls)
        )
        self.assertFalse((self.units / "blueprint-executor.service").exists())

    def test_control_plane_fails_before_writes_when_config_is_incomplete(self):
        calls = []
        patches = self._patches()
        for item in patches:
            item.start()
        try:
            rc = node.do_install(
                profile="control-plane",
                system=False,
                token=None,
                url="https://orch.example",
                dry_run=False,
                role_configs={},
                _run=_run_ok(calls),
            )
        finally:
            for item in reversed(patches):
                item.stop()
        self.assertEqual(rc, 1)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.units.exists())

    def test_gpu_worker_writes_gpu_defaults_without_overriding_explicit_limit(self):
        calls = []
        patches = self._patches()
        for item in patches:
            item.start()
        try:
            rc = node.do_install(
                profile="gpu-worker", system=False, token=None, url="https://orch.example",
                dry_run=False,
                resource_limits={"BLUEPRINT_RUNNER_MAX_CONCURRENT_COMPUTE_JOBS": "3"},
                _run=_run_ok(calls),
            )
        finally:
            for item in reversed(patches):
                item.stop()
        self.assertEqual(rc, 0)
        env_text = (self.root / "roles/executor/role.env").read_text()
        self.assertIn("BLUEPRINT_RUNNER_MAX_CONCURRENT_COMPUTE_JOBS=3", env_text)
        self.assertIn("BLUEPRINT_RUNNER_GPUS_PER_JOB=1", env_text)

    def test_dry_run_writes_nothing(self):
        calls = []
        patches = self._patches()
        for item in patches:
            item.start()
        try:
            rc = node.do_install(
                profile="worker", system=False, token="x", url="https://orch.example",
                dry_run=True, _run=_run_ok(calls),
            )
        finally:
            for item in reversed(patches):
                item.stop()
        self.assertEqual(rc, 0)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.units.exists())
        self.assertEqual(calls, [])

    def test_install_refuses_to_duplicate_an_active_legacy_runner(self):
        calls = []

        def active_legacy(argv, **kwargs):
            calls.append(list(argv))
            return MagicMock(returncode=0, stdout="active\n", stderr="")

        patches = self._patches()
        for item in patches:
            item.start()
        try:
            rc = node.do_install(
                profile="worker", system=False, token="secret", url="https://orch.example",
                dry_run=False, _run=active_legacy,
            )
        finally:
            for item in reversed(patches):
                item.stop()

        self.assertEqual(rc, 1)
        self.assertEqual(
            calls,
            [
                ["systemctl", "--user", "is-active", node._LEGACY_RUNNER_UNIT],
                ["systemctl", "is-active", node._LEGACY_RUNNER_UNIT],
            ],
        )
        self.assertFalse(self.root.exists())

    def test_user_install_checks_an_active_system_legacy_runner_too(self):
        calls = []

        def system_legacy_active(argv, **kwargs):
            calls.append(list(argv))
            return MagicMock(
                returncode=0 if "--user" not in argv else 3,
                stdout="active\n" if "--user" not in argv else "inactive\n",
                stderr="",
            )

        patches = self._patches()
        for item in patches:
            item.start()
        try:
            rc = node.do_install(
                profile="worker", system=False, token="secret", url="https://orch.example",
                dry_run=False, _run=system_legacy_active,
            )
        finally:
            for item in reversed(patches):
                item.stop()

        self.assertEqual(rc, 1)
        self.assertEqual(
            calls,
            [
                ["systemctl", "--user", "is-active", node._LEGACY_RUNNER_UNIT],
                ["systemctl", "is-active", node._LEGACY_RUNNER_UNIT],
            ],
        )
        self.assertFalse(self.root.exists())


class DrainAndStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name) / "node"

    def tearDown(self):
        self.temp.cleanup()

    def test_stop_stops_executor_before_writing_marker(self):
        calls = []
        with patch.object(node, "node_root", return_value=self.root):
            rc = node.do_stop(role="executor", system=False, dry_run=False, _run=_run_ok(calls))
            self.assertEqual(rc, 0)
            self.assertEqual(calls[0], ["systemctl", "--user", "stop", "blueprint-executor.service"])
            self.assertEqual(calls[1], ["systemctl", "--user", "disable", "blueprint-executor.service"])
            marker = json.loads((self.root / "roles/executor/stopped.json").read_text())
            self.assertEqual(marker["role"], "executor")

    def test_stop_does_not_mark_when_stop_fails(self):
        def fail(argv, **kwargs):
            return MagicMock(returncode=1, stdout="", stderr="permission denied")
        with patch.object(node, "node_root", return_value=self.root):
            rc = node.do_stop(role="executor", system=False, dry_run=False, _run=fail)
            self.assertEqual(rc, 1)
            self.assertFalse((self.root / "roles/executor/stopped.json").exists())

    def test_start_starts_before_removing_marker(self):
        calls = []
        with patch.object(node, "node_root", return_value=self.root):
            node._atomic_write(node.stopped_path("executor"), "{}\n")
            rc = node.do_start(role="executor", system=False, _run=_run_ok(calls))
            self.assertEqual(rc, 0)
            self.assertEqual(calls[0], ["systemctl", "--user", "enable", "--now", "blueprint-executor.service"])
            self.assertFalse(node.stopped_path("executor").exists())

    def test_status_follows_system_scope_recorded_by_install(self):
        calls = []
        manifest = {"profile": "gpu-worker", "scope": "system"}
        with patch.object(node, "_read_manifest", return_value=manifest), \
             patch.object(node, "stopped_path", return_value=Path("/does/not/exist")):
            rc = node.do_status(profile=None, system=False, _run=_run_ok(calls))
        self.assertEqual(rc, 0)
        self.assertTrue(all(call[0] == "systemctl" and "--user" not in call for call in calls))

    def test_status_discovers_system_manifest_without_a_flag(self):
        calls = []
        manifest = {"profile": "gpu-worker", "scope": "system"}
        with patch.object(node, "_read_manifest", side_effect=[None, manifest]), \
             patch.object(node, "stopped_path", return_value=Path("/does/not/exist")):
            rc = node.do_status(profile=None, system=False, _run=_run_ok(calls))
        self.assertEqual(rc, 0)
        self.assertTrue(all("--user" not in call for call in calls))

    def test_env_file_rejects_multiline_secret(self):
        with self.assertRaisesRegex(ValueError, "control characters"):
            node._role_env_text(
                profile="worker", url="https://orch.example", token="bad\nsecret", limits={}
            )

    def test_env_file_uses_explicit_service_user_credential_dir(self):
        env_text = node._role_env_text(
            profile="worker",
            url="https://orch.example",
            token=None,
            limits={},
            credential_dir="/home/runner/.blueprint",
        )
        self.assertIn("BLUEPRINT_CREDENTIAL_DIR=/home/runner/.blueprint", env_text)

    def test_install_parser_does_not_accept_token_on_argv(self):
        with self.assertRaises(SystemExit):
            node.build_parser().parse_args(["install", "--profile", "worker", "--token", "secret"])

    def test_status_reports_planned_roles_without_systemctl_calls(self):
        manifest = {"profile": "worker"}
        calls = []
        with patch.object(node, "_read_manifest", return_value=manifest), \
             patch.object(node, "stopped_path", return_value=Path("/does/not/exist")):
            rc = node.do_status(profile=None, system=False, _run=_run_ok(calls))
        self.assertEqual(rc, 0)
        joined = [" ".join(call) for call in calls]
        self.assertTrue(any("blueprint-executor.service" in call for call in joined))
        self.assertFalse(any("blueprint-actions.service" in call for call in joined))


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the additive blueprint-node lifecycle facade.

These tests inject every systemd interaction.  They verify rendered units and
state files only; no test starts a real service or touches a real credential.
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import blueprint_node as node

_TEMP_PARENT = Path(__file__).parent if os.name == "nt" else None


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
        self.assertEqual(
            node.PROFILE_ROLES["local-control-plane"],
            ("database", "database-backup", "api", "worker", "edge"),
        )
        for roles in node.PROFILE_ROLES.values():
            if "worker" in roles:
                self.assertNotIn("github-actions", roles)
        self.assertTrue(node.ROLE_SPECS["executor"].executable)
        self.assertTrue(node.ROLE_SPECS["api"].executable)
        self.assertTrue(node.ROLE_SPECS["database"].executable)
        self.assertTrue(node.ROLE_SPECS["database-backup"].executable)
        self.assertTrue(node.ROLE_SPECS["edge"].executable)
        self.assertTrue(node.ROLE_SPECS["worker"].executable)
        self.assertFalse(node._profile_role_executable("primary", "api"))

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


class LocalPostgresOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_TEMP_PARENT)
        self.allowed_root = Path(self.temp.name) / "srv-blueprint"
        self.allowed_root.mkdir()
        self.data_dir = self.allowed_root / "postgres"

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_data_dir_is_mode_0700_and_chowned_to_image_identity(self):
        with patch.object(node, "_LOCAL_DATABASE_ROOTS", (self.allowed_root,)), \
             patch.object(node, "_require_trusted_directory_fd"), \
             patch.object(node.os, "chown", create=True) as chown, \
             patch.object(node.os, "fchown", create=True) as fchown:
            prepared = node._prepare_local_postgres_data_dir(str(self.data_dir))
        self.assertEqual(prepared, self.data_dir)
        if os.name != "nt":
            self.assertEqual(self.data_dir.stat().st_mode & 0o777, 0o700)
        if os.name == "nt":
            chown.assert_called_once_with(
                self.data_dir,
                node._POSTGRES_CONTAINER_UID,
                node._POSTGRES_CONTAINER_GID,
            )
        else:
            fchown.assert_called_once_with(
                ANY,
                node._POSTGRES_CONTAINER_UID,
                node._POSTGRES_CONTAINER_GID,
            )

    def test_nonempty_uninitialized_data_dir_is_never_chowned(self):
        self.data_dir.mkdir()
        (self.data_dir / "unexpected").write_text("do not overwrite", encoding="utf-8")
        with patch.object(node, "_LOCAL_DATABASE_ROOTS", (self.allowed_root,)), \
             patch.object(node, "_require_trusted_directory_fd"), \
             patch.object(node.os, "chown", create=True) as chown:
            with self.assertRaisesRegex(ValueError, "ambiguous non-empty"):
                node._prepare_local_postgres_data_dir(str(self.data_dir))
        chown.assert_not_called()

    def test_initialized_data_with_foreign_owner_is_refused_not_rechowned(self):
        self.data_dir.mkdir(mode=0o700)
        (self.data_dir / "PG_VERSION").write_text("15\n", encoding="ascii")
        with patch.object(node, "_LOCAL_DATABASE_ROOTS", (self.allowed_root,)), \
             patch.object(node, "_require_trusted_directory_fd"), \
             patch.object(node, "_POSTGRES_CONTAINER_UID", 424242), \
             patch.object(node, "_POSTGRES_CONTAINER_GID", 424242), \
             patch.object(node.os, "chown", create=True) as chown:
            with self.assertRaisesRegex(ValueError, "already be owned"):
                node._prepare_local_postgres_data_dir(str(self.data_dir))
        chown.assert_not_called()

    def test_path_outside_narrow_database_roots_is_refused(self):
        with self.assertRaisesRegex(ValueError, "below /var/lib/blueprint"):
            node._prepare_local_postgres_data_dir(str(Path(self.temp.name) / "db"))

    def test_symlinked_data_path_component_is_refused(self):
        target = self.allowed_root / "target"
        target.mkdir()
        try:
            self.data_dir.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("host does not permit test symlink creation")
        with patch.object(node, "_LOCAL_DATABASE_ROOTS", (self.allowed_root,)), \
             patch.object(node, "_require_trusted_directory_fd"):
            with self.assertRaisesRegex(ValueError, "symlink"):
                node._prepare_local_postgres_data_dir(str(self.data_dir / "pgdata"))

    def test_trusted_ancestor_rejects_group_writable_directory(self):
        info = MagicMock(st_mode=stat.S_IFDIR | 0o775, st_uid=node._TRUSTED_PATH_UID)
        with patch.object(node.os, "fstat", return_value=info):
            with self.assertRaisesRegex(ValueError, "not group/world-writable"):
                node._require_trusted_directory_fd(123)


class RenderTests(unittest.TestCase):
    def test_executor_unit_has_real_paths_and_role_specific_env(self):
        rendered = node._render_unit(
            "executor",
            profile="worker",
            system=False,
            env_file=Path("/tmp/roles/executor/role.env"),
        )
        self.assertIn("Description=Blueprint node role: executor", rendered)
        self.assertIn(f"ExecStart={sys.executable} {node._LOCAL_RUNNER}", rendered)
        self.assertIn(
            f"EnvironmentFile=-{Path('/tmp/roles/executor/role.env')}", rendered
        )
        self.assertNotRegex(rendered, r"@[A-Z_]+@")

    def test_planned_role_cannot_render_a_fake_service(self):
        with self.assertRaisesRegex(ValueError, "planned"):
            node._render_unit(
                "api",
                profile="primary",
                system=False,
                env_file=Path("/tmp/api.env"),
            )

    def test_worker_requires_proxy_and_mandatory_env_file(self):
        rendered = node._render_unit(
            "worker",
            profile="control-plane",
            system=False,
            env_file=Path("/tmp/roles/worker/role.env"),
        )
        self.assertIn("Requires=blueprint-database-proxy.service", rendered)
        self.assertIn("After=blueprint-database-proxy.service", rendered)
        self.assertIn(
            f"EnvironmentFile={Path('/tmp/roles/worker/role.env')}",
            rendered,
        )
        self.assertIn(
            f"ExecStartPre={sys.executable} "
            f"{node._LOCAL_WORKER_SERVICE} --check",
            rendered,
        )
        self.assertIn("UMask=0077", rendered)
        self.assertIn("NoNewPrivileges=true", rendered)
        self.assertIn("RestrictSUIDSGID=true", rendered)
        self.assertIn("LockPersonality=true", rendered)
        self.assertNotIn(
            f"EnvironmentFile=-{Path('/tmp/roles/worker/role.env')}", rendered
        )

    def test_local_worker_requires_database_not_cloud_sql_proxy(self):
        rendered = node._render_unit(
            "worker",
            profile="local-control-plane",
            system=False,
            env_file=Path("/tmp/roles/worker/role.env"),
        )
        self.assertIn("Requires=blueprint-database.service", rendered)
        self.assertIn("After=blueprint-database.service", rendered)
        self.assertNotIn("blueprint-database-proxy.service", rendered)

    def test_edge_uses_dedicated_oneshot_unit_bound_to_api(self):
        with patch.object(node, "_system_install_user", return_value="blueprint-control"):
            rendered = node._render_unit(
                "edge",
                profile="local-control-plane",
                system=True,
                env_file=Path("/tmp/roles/edge/role.env"),
            )
        self.assertIn("Type=oneshot", rendered)
        self.assertIn("RemainAfterExit=yes", rendered)
        self.assertIn("BindsTo=blueprint-api.service", rendered)
        self.assertIn("Requires=tailscaled.service", rendered)
        self.assertIn(
            "WantedBy=multi-user.target blueprint-api.service tailscaled.service",
            rendered,
        )
        self.assertIn(
            f"ExecStart={sys.executable} {node._TAILSCALE_FUNNEL_SERVICE} start",
            rendered,
        )
        self.assertNotRegex(rendered, r"@[A-Z_]+@")

    def test_database_uses_explicit_oneshot_controller_lifecycle(self):
        with patch.object(node, "_system_install_user", return_value="blueprint-control"):
            rendered = node._render_unit(
                "database",
                profile="local-control-plane",
                system=True,
                env_file=Path("/etc/blueprint/database.env"),
            )
        self.assertIn("Type=oneshot", rendered)
        self.assertIn("RemainAfterExit=yes", rendered)
        self.assertIn(
            f"ExecStart={sys.executable} {node._LOCAL_POSTGRES_SERVICE} start",
            rendered,
        )
        self.assertIn(
            f"ExecStop={sys.executable} {node._LOCAL_POSTGRES_SERVICE} stop",
            rendered,
        )
        self.assertNotIn("--check", rendered)

    def test_database_backup_has_twice_daily_randomized_system_timer(self):
        with patch.object(node, "_system_install_user", return_value="blueprint-control"):
            timer = node._render_unit(
                "database-backup",
                profile="local-control-plane",
                system=True,
                env_file=Path("/etc/blueprint/database-backup.env"),
            )
            service = node._render_database_backup_service(
                profile="local-control-plane",
                system=True,
                env_file=Path("/etc/blueprint/database-backup.env"),
            )
        self.assertIn("OnCalendar=*-*-* 00,12:00:00", timer)
        self.assertIn("RandomizedDelaySec=30m", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Requires=blueprint-database.service", timer)
        self.assertIn("Requires=blueprint-database.service", service)
        self.assertIn(
            f"ExecStart={sys.executable} {node._LOCAL_POSTGRES_BACKUP_SERVICE}",
            service,
        )

    def test_system_unit_adds_host_process_sandboxing(self):
        with patch.object(node, "_system_install_user", return_value="blueprint-control"):
            rendered = node._render_unit(
                "worker",
                profile="control-plane",
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
                node._render_unit(
                    "executor",
                    profile="worker",
                    system=True,
                    env_file=Path("/etc/blueprint/role.env"),
                )

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
        self.temp = tempfile.TemporaryDirectory(dir=_TEMP_PARENT)
        self.root = Path(self.temp.name) / "node"
        self.units = Path(self.temp.name) / "units"

    def tearDown(self):
        self.temp.cleanup()

    def _patches(self):
        return [
            patch.object(node, "node_root", return_value=self.root),
            patch.object(node, "unit_path", side_effect=lambda role, system: self.units / node.ROLE_SPECS[role].unit_name),
            patch.object(
                node,
                "_database_backup_service_unit_path",
                return_value=self.units / "blueprint-database-backup.service",
            ),
            patch.object(node, "_systemd_available", return_value=True),
            patch.object(node, "_system_install_user", return_value="blueprint-control"),
            patch.object(
                node,
                "_system_credential_dir",
                return_value=(self.root / "credentials", 1000, 1000),
            ),
            patch.object(node, "_prepare_system_node_root"),
            patch.object(node, "_prepare_system_credential_dir"),
            patch.object(node, "_prepare_local_postgres_data_dir"),
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

    @staticmethod
    def _local_control_plane_config(*, api_image=None, worker_image=None):
        shared_image = f"sha256:{'b' * 64}"
        return {
            "database": {
                "BLUEPRINT_POSTGRES_IMAGE_ID": f"sha256:{'c' * 64}",
                "BLUEPRINT_POSTGRES_DATA_DIR": "/srv/blueprint/postgres",
                "BLUEPRINT_POSTGRES_SECRETS_FILE": "/etc/blueprint/postgres.env",
                "BLUEPRINT_CONTROL_PLANE_NETWORK": "blueprint-control-plane",
            },
            "database-backup": {
                "BLUEPRINT_POSTGRES_IMAGE_ID": f"sha256:{'c' * 64}",
                "BLUEPRINT_POSTGRES_SECRETS_FILE": "/etc/blueprint/postgres.env",
                "BLUEPRINT_POSTGRES_BACKUP_PROJECT_ID": "recovery-project-123",
                "BLUEPRINT_POSTGRES_BACKUP_BUCKET": "gs://blueprint-recovery",
                "BLUEPRINT_POSTGRES_BACKUP_CREDENTIALS_FILE": "/etc/blueprint/backup-gcp.json",
                "BLUEPRINT_POSTGRES_BACKUP_IMAGE_ID": f"sha256:{'f' * 64}",
                "BLUEPRINT_CONTROL_PLANE_NETWORK": "blueprint-control-plane",
            },
            "api": {
                "BLUEPRINT_API_IMAGE_ID": api_image or shared_image,
                "BLUEPRINT_API_SECRETS_FILE": "/etc/blueprint/api-secrets.json",
                "BLUEPRINT_API_GOOGLE_CREDENTIALS_FILE": "/etc/blueprint/api-gcp.json",
                "BLUEPRINT_CONTROL_PLANE_NETWORK": "blueprint-control-plane",
                "BLUEPRINT_API_PORT": "8080",
                "BLUEPRINT_DATABASE_MODE": "local-postgres",
            },
            "worker": {
                "BLUEPRINT_WORKER_IMAGE_ID": worker_image or shared_image,
                "BLUEPRINT_WORKER_SECRETS_FILE": "/etc/blueprint/worker-secrets.json",
                "BLUEPRINT_WORKER_GOOGLE_CREDENTIALS_FILE": "/etc/blueprint/worker-gcp.json",
                "BLUEPRINT_CONTROL_PLANE_NETWORK": "blueprint-control-plane",
                "BLUEPRINT_DATABASE_MODE": "local-postgres",
            },
            "edge": {
                "BLUEPRINT_TAILSCALE_BINARY": "/usr/bin/tailscale",
                "BLUEPRINT_EDGE_API_PORT": "8080",
            },
        }

    def test_local_control_plane_installs_in_dependency_order_without_proxy(self):
        calls = []
        patches = self._patches()
        started = []
        for item in patches:
            started.append(item.start())
        try:
            rc = node.do_install(
                profile="local-control-plane",
                system=True,
                token=None,
                url="https://orch.example",
                dry_run=False,
                role_configs=self._local_control_plane_config(),
                _run=_run_ok(calls),
            )
        finally:
            for item in reversed(patches):
                item.stop()
        self.assertEqual(rc, 0)
        started[-1].assert_called_once_with("/srv/blueprint/postgres")
        enables = [
            call[-1]
            for call in calls
            if "enable" in call and "--now" in call
        ]
        self.assertEqual(
            enables,
            [
                "blueprint-database.service",
                "blueprint-database-backup.timer",
                "blueprint-api.service",
                "blueprint-worker.service",
                "blueprint-edge.service",
            ],
        )
        restarts = [
            call[-1]
            for call in calls
            if "restart" in call
        ]
        self.assertEqual(restarts, enables)
        self.assertFalse((self.units / "blueprint-database-proxy.service").exists())
        worker_env = (self.root / "roles/worker/role.env").read_text()
        self.assertIn("BLUEPRINT_DATABASE_MODE=local-postgres", worker_env)
        edge_unit = (self.units / "blueprint-edge.service").read_text()
        database_unit = (self.units / "blueprint-database.service").read_text()
        backup_timer = (self.units / "blueprint-database-backup.timer").read_text()
        backup_service = (self.units / "blueprint-database-backup.service").read_text()
        self.assertIn("User=root", database_unit)
        self.assertIn("ExecStart=", backup_service)
        self.assertIn(
            "RuntimeDirectory=blueprint-local-postgres-backup",
            backup_service,
        )
        self.assertIn("RuntimeDirectoryMode=0700", backup_service)
        self.assertIn("RandomizedDelaySec=30m", backup_timer)
        self.assertIn("Type=oneshot", edge_unit)
        self.assertIn("RemainAfterExit=yes", edge_unit)
        self.assertIn("BindsTo=blueprint-api.service", edge_unit)
        self.assertIn(
            f"ExecStop={sys.executable} {node._TAILSCALE_FUNNEL_SERVICE} stop",
            edge_unit,
        )

    def test_local_control_plane_refuses_user_scope_before_writes(self):
        calls = []
        patches = self._patches()
        for item in patches:
            item.start()
        try:
            rc = node.do_install(
                profile="local-control-plane",
                system=False,
                token=None,
                url="https://orch.example",
                dry_run=False,
                role_configs=self._local_control_plane_config(),
                _run=_run_ok(calls),
            )
        finally:
            for item in reversed(patches):
                item.stop()
        self.assertEqual(rc, 2)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.units.exists())
        self.assertEqual(calls, [])

    def test_local_control_plane_rejects_different_api_worker_images_before_writes(self):
        calls = []
        patches = self._patches()
        for item in patches:
            item.start()
        try:
            rc = node.do_install(
                profile="local-control-plane",
                system=True,
                token=None,
                url="https://orch.example",
                dry_run=False,
                role_configs=self._local_control_plane_config(
                    api_image=f"sha256:{'d' * 64}",
                    worker_image=f"sha256:{'e' * 64}",
                ),
                _run=_run_ok(calls),
            )
        finally:
            for item in reversed(patches):
                item.stop()
        self.assertEqual(rc, 1)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.units.exists())

    def test_failed_role_start_restores_files_and_prior_unit_states(self):
        calls = []
        started_by_install = set()
        old_manifest = '{"profile":"previous"}\n'
        old_database_unit = "old database unit\n"
        (self.root).mkdir(parents=True)
        (self.units).mkdir(parents=True)
        (self.root / "manifest.json").write_text(old_manifest, encoding="utf-8")
        (self.units / "blueprint-database.service").write_text(
            old_database_unit, encoding="utf-8"
        )

        def fail_worker_start(argv, **kwargs):
            calls.append(list(argv))
            unit = argv[-1]
            if unit == node._LEGACY_RUNNER_UNIT and "is-active" in argv:
                return MagicMock(returncode=3, stdout="inactive\n", stderr="")
            if "is-enabled" in argv:
                return MagicMock(
                    returncode=0
                    if unit in {
                        "blueprint-database.service",
                        "blueprint-api.service",
                    }
                    else 1,
                    stdout="",
                    stderr="",
                )
            if "is-active" in argv:
                return MagicMock(
                    returncode=0
                    if unit in started_by_install
                    or unit in {
                        "blueprint-database.service",
                        "blueprint-api.service",
                    }
                    else 3,
                    stdout="",
                    stderr="",
                )
            if "restart" in argv and unit == "blueprint-api.service":
                return MagicMock(returncode=1, stdout="", stderr="restart failed")
            if "enable" in argv and "--now" in argv:
                started_by_install.add(unit)
            return MagicMock(returncode=0, stdout="", stderr="")

        patches = self._patches()
        for item in patches:
            item.start()
        try:
            rc = node.do_install(
                profile="local-control-plane",
                system=True,
                token=None,
                url="https://orch.example",
                dry_run=False,
                role_configs=self._local_control_plane_config(),
                _run=fail_worker_start,
            )
        finally:
            for item in reversed(patches):
                item.stop()

        self.assertEqual(rc, 1)
        self.assertEqual(
            (self.root / "manifest.json").read_text(encoding="utf-8"),
            old_manifest,
        )
        self.assertEqual(
            (self.units / "blueprint-database.service").read_text(
                encoding="utf-8"
            ),
            old_database_unit,
        )
        self.assertFalse((self.units / "blueprint-database-backup.timer").exists())
        self.assertFalse((self.units / "blueprint-database-backup.service").exists())
        commands = [" ".join(call) for call in calls]
        self.assertTrue(
            any(
                "disable --now blueprint-edge.service" in command
                for command in commands
            )
        )
        self.assertTrue(
            any(
                "enable blueprint-database.service" in command
                for command in commands
            )
        )
        self.assertTrue(
            any(
                "start blueprint-api.service" in command for command in commands
            )
        )

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
        self.assertIn("BLUEPRINT_DATABASE_MODE=cloudsql-proxy", worker_env)
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
        self.temp = tempfile.TemporaryDirectory(dir=_TEMP_PARENT)
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

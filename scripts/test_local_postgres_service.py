#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("local_postgres_service.py")
SPEC = importlib.util.spec_from_file_location("local_postgres_service", MODULE_PATH)
assert SPEC and SPEC.loader
postgres = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(postgres)

IMAGE_ID = f"sha256:{'a' * 64}"
IMAGE_DIGEST = f"postgres:15@sha256:{'b' * 64}"
NETWORK = "blueprint-control-plane"


def completed(argv, returncode=0, stdout="", stderr=""):
    return postgres.subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class LocalPostgresServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "postgres-data"
        self.data.mkdir()
        self.password = self.root / "postgres-password"
        self.password.write_text("not-a-real-password\n", encoding="utf-8")
        if os.name != "nt":
            self.data.chmod(0o700)
            self.password.chmod(0o600)
        self.env = {
            "BLUEPRINT_POSTGRES_IMAGE_ID": IMAGE_ID,
            "BLUEPRINT_CONTROL_PLANE_NETWORK": NETWORK,
            "BLUEPRINT_POSTGRES_DATA_DIR": str(self.data),
            "BLUEPRINT_POSTGRES_SECRETS_FILE": str(self.password),
        }
        self.data_info = patch.object(
            postgres,
            "_data_dir_info",
            return_value=SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700,
                st_uid=999,
                st_gid=999,
            ),
        )
        self.data_info_mock = self.data_info.start()

    def tearDown(self):
        self.data_info.stop()
        if os.name != "nt":
            self.data.chmod(0o700)
            self.password.chmod(0o600)
        self.temp.cleanup()

    def image_payload(self, *, image_id=IMAGE_ID, major="15"):
        return [{"Id": image_id, "Config": {"Env": [f"PG_MAJOR={major}"]}}]

    def network_payload(self, *, label=postgres._NETWORK_LABEL_VALUE, driver="bridge"):
        return [
            {
                "Labels": {postgres._NETWORK_LABEL: label},
                "Driver": driver,
                "IPAM": {"Config": [{"Gateway": "172.20.0.1"}]},
            }
        ]

    def container_payload(
        self,
        *,
        running=True,
        image_id=IMAGE_ID,
        network=NETWORK,
        alias=postgres._NETWORK_ALIAS,
        data_source=None,
        password_source=None,
        ports=None,
    ):
        return {
            "Image": image_id,
            "Config": {
                "Labels": {
                    postgres._SERVICE_LABEL: postgres._SERVICE_LABEL_VALUE
                },
                "Env": [
                    f"POSTGRES_PASSWORD_FILE={postgres._PASSWORD_TARGET}",
                    f"POSTGRES_USER={postgres._POSTGRES_USER}",
                    f"POSTGRES_DB={postgres._POSTGRES_DB}",
                    "PG_MAJOR=15",
                ],
            },
            "HostConfig": {
                "NetworkMode": network,
                "PortBindings": ports,
                "Memory": 2 * 1024**3,
                "NanoCpus": 2_000_000_000,
                "PidsLimit": 256,
                "ShmSize": 256 * 1024**2,
                "SecurityOpt": ["no-new-privileges"],
                "LogConfig": {
                    "Type": "json-file",
                    "Config": {"max-size": "10m", "max-file": "3"},
                },
                "RestartPolicy": {"Name": "unless-stopped"},
            },
            "NetworkSettings": {
                "Networks": {
                    network: {"Aliases": [postgres._CONTAINER_NAME, alias]}
                }
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(data_source or self.data),
                    "Destination": postgres._DATA_TARGET,
                    "RW": True,
                },
                {
                    "Type": "bind",
                    "Source": str(password_source or self.password),
                    "Destination": postgres._PASSWORD_TARGET,
                    "RW": False,
                },
            ],
            "State": {
                "Running": running,
                "Dead": False,
                "Health": {"Status": "healthy"},
            },
        }

    def docker_side_effect(self, *, container=None, run_result=None):
        def invoke(argv):
            if argv[:3] == ["docker", "image", "inspect"]:
                return completed(argv, stdout=json.dumps(self.image_payload()))
            if argv[:3] == ["docker", "network", "inspect"]:
                return completed(argv, stdout=json.dumps(self.network_payload()))
            if argv[:3] == ["docker", "container", "inspect"]:
                if container is None:
                    return completed(argv, returncode=1, stderr="No such object")
                return completed(argv, stdout=json.dumps([container]))
            if argv[:2] == ["docker", "run"]:
                if "--user" in argv and argv[argv.index("--user") + 1] == "999:999":
                    state = "initialized" if (self.data / "PG_VERSION").exists() else "empty"
                    return completed(argv, stdout=f"{state}\n")
                return run_result or completed(argv, stdout="container-id")
            if argv[:3] in (
                ["docker", "container", "start"],
                ["docker", "container", "stop"],
            ):
                return run_result or completed(argv, stdout=postgres._CONTAINER_NAME)
            raise AssertionError(f"unexpected command: {argv}")

        return invoke

    @patch.object(postgres, "_run")
    def test_new_container_is_private_pinned_and_bounded(self, run):
        run.side_effect = self.docker_side_effect()
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(postgres.start(), 0)
        argv = next(
            call.args[0]
            for call in run.call_args_list
            if call.args[0][:2] == ["docker", "run"] and "--name" in call.args[0]
        )
        self.assertNotIn("--publish", argv)
        self.assertNotIn("-p", argv)
        self.assertEqual(argv[argv.index("--pull") + 1], "never")
        self.assertEqual(
            argv[argv.index("--network-alias") + 1], "blueprint-postgres"
        )
        self.assertEqual(argv[argv.index("--network") + 1], NETWORK)
        self.assertEqual(argv[argv.index("--memory") + 1], "2g")
        self.assertEqual(argv[argv.index("--cpus") + 1], "2")
        self.assertEqual(argv[argv.index("--pids-limit") + 1], "256")
        self.assertIn("max-size=10m", argv)
        self.assertIn("max-file=3", argv)
        self.assertEqual(argv[-1], IMAGE_ID)
        joined = " ".join(argv)
        self.assertIn(f"POSTGRES_PASSWORD_FILE={postgres._PASSWORD_TARGET}", joined)
        self.assertNotIn("not-a-real-password", joined)

    @patch.object(postgres, "_run")
    def test_running_matching_container_is_idempotent(self, run):
        run.side_effect = self.docker_side_effect(
            container=self.container_payload(running=True)
        )
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(postgres.start(), 0)
        self.assertFalse(
            any(
                call.args[0][:2] == ["docker", "run"]
                and "--name" in call.args[0]
                for call in run.call_args_list
            )
        )
        self.assertFalse(any(call.args[0][:3] == ["docker", "container", "start"] for call in run.call_args_list))

    @patch.object(postgres, "_run")
    def test_stopped_matching_container_restarts_with_initialized_data(self, run):
        (self.data / "PG_VERSION").write_text("15\n", encoding="ascii")
        run.side_effect = self.docker_side_effect(
            container=self.container_payload(running=False)
        )
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(postgres.start(), 0)
        self.assertTrue(any(call.args[0][:3] == ["docker", "container", "start"] for call in run.call_args_list))

    def test_refuses_ambiguous_existing_data(self):
        with (
            patch.object(
                postgres,
                "_inspect_data_dir_as_postgres",
                side_effect=RuntimeError("ambiguous"),
            ),
            self.assertRaisesRegex(RuntimeError, "ambiguous"),
        ):
            postgres._prepare_data_dir(
                str(self.data), image=IMAGE_ID, inspect_contents=True
            )

    def test_refuses_other_postgres_major_data(self):
        with (
            patch.object(
                postgres,
                "_inspect_data_dir_as_postgres",
                side_effect=RuntimeError("ambiguous or unreadable"),
            ),
            self.assertRaisesRegex(RuntimeError, "ambiguous"),
        ):
            postgres._prepare_data_dir(
                str(self.data), image=IMAGE_ID, inspect_contents=True
            )

    def test_refuses_symlink_data_directory(self):
        link = self.root / "linked-data"
        try:
            link.symlink_to(self.data, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaisesRegex(ValueError, "symlink"):
            postgres._assert_path_has_no_symlink_component(link)

    def test_root_owned_empty_directory_is_refused_until_installer_bootstraps(self):
        self.data_info_mock.return_value = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700,
            st_uid=0,
            st_gid=0,
        )
        with self.assertRaisesRegex(RuntimeError, "system installer"):
            postgres._prepare_data_dir(
                str(self.data), image=IMAGE_ID, inspect_contents=True
            )

    @patch.object(postgres, "_run")
    def test_restart_inspection_runs_read_only_as_postgres_uid(self, run):
        run.return_value = completed(["docker"], stdout="initialized\n")
        path, state = postgres._prepare_data_dir(
            str(self.data), image=IMAGE_ID, inspect_contents=True
        )
        self.assertEqual(path, self.data)
        self.assertEqual(state, "initialized")
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--user") + 1], "999:999")
        self.assertIn("readonly", " ".join(argv))
        self.assertIn("--cap-drop", argv)

    @unittest.skipIf(os.name == "nt", "POSIX file mode contract")
    def test_password_must_be_exactly_0600(self):
        self.password.chmod(0o640)
        with self.assertRaisesRegex(ValueError, "0600"):
            postgres._secure_password_file(str(self.password))

    def test_mutable_image_tag_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "pinned"):
            postgres._validate_image_input("postgres:15")

    @patch.object(postgres, "_run")
    def test_image_id_must_report_postgres_15(self, run):
        run.return_value = completed(
            ["docker"], stdout=json.dumps(self.image_payload(major="16"))
        )
        with self.assertRaisesRegex(RuntimeError, "major version 15"):
            postgres._inspect_image(IMAGE_ID)

    @patch.object(postgres, "_run")
    def test_digest_must_match_inspected_repo_digest(self, run):
        payload = self.image_payload()
        payload[0]["RepoDigests"] = ["postgres@sha256:" + "c" * 64]
        run.return_value = completed(["docker"], stdout=json.dumps(payload))
        with self.assertRaisesRegex(RuntimeError, "requested postgres digest"):
            postgres._inspect_image(IMAGE_DIGEST)

    @patch.object(postgres, "_run")
    def test_refuses_unowned_existing_network(self, run):
        run.return_value = completed(
            ["docker"],
            stdout=json.dumps(self.network_payload(label="someone-else")),
        )
        with self.assertRaisesRegex(RuntimeError, "not owned"):
            postgres._network_payload(NETWORK)

    @patch.object(postgres, "_run")
    def test_refuses_non_bridge_network(self, run):
        run.return_value = completed(
            ["docker"],
            stdout=json.dumps(self.network_payload(driver="overlay")),
        )
        with self.assertRaisesRegex(RuntimeError, "not a bridge"):
            postgres._network_payload(NETWORK)

    def test_refuses_mismatched_existing_image(self):
        with self.assertRaisesRegex(RuntimeError, "different image"):
            postgres._assert_matching_container(
                self.container_payload(image_id=f"sha256:{'d' * 64}"),
                image_id=IMAGE_ID,
                network=NETWORK,
                data_dir=self.data,
                password_file=self.password,
            )

    def test_refuses_mismatched_existing_mount(self):
        with self.assertRaisesRegex(RuntimeError, "data mount"):
            postgres._assert_matching_container(
                self.container_payload(data_source=self.root / "other"),
                image_id=IMAGE_ID,
                network=NETWORK,
                data_dir=self.data,
                password_file=self.password,
            )

    def test_refuses_mismatched_existing_network_alias(self):
        with self.assertRaisesRegex(RuntimeError, "network alias"):
            postgres._assert_matching_container(
                self.container_payload(alias="database"),
                image_id=IMAGE_ID,
                network=NETWORK,
                data_dir=self.data,
                password_file=self.password,
            )

    def test_refuses_existing_published_port(self):
        with self.assertRaisesRegex(RuntimeError, "publishes host ports"):
            postgres._assert_matching_container(
                self.container_payload(ports={"5432/tcp": [{"HostPort": "5432"}]}),
                image_id=IMAGE_ID,
                network=NETWORK,
                data_dir=self.data,
                password_file=self.password,
            )

    @patch.object(postgres, "_run")
    def test_stop_is_idempotent_for_absent_container(self, run):
        run.side_effect = self.docker_side_effect(container=None)
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(postgres.stop(), 0)
        self.assertEqual(len(run.call_args_list), 1)
        self.assertFalse(any(call.args[0][:3] == ["docker", "container", "stop"] for call in run.call_args_list))

    @patch.object(postgres, "_run")
    def test_status_absent_does_not_create_resources(self, run):
        run.return_value = completed(
            ["docker", "container", "inspect"],
            returncode=1,
            stderr="Error: No such object: blueprint-local-postgres",
        )
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(postgres.status(), 3)
        run.assert_called_once_with(
            ["docker", "container", "inspect", postgres._CONTAINER_NAME]
        )

    @patch.object(postgres, "_run")
    def test_container_inspect_daemon_error_is_not_reported_as_absent(self, run):
        run.return_value = completed(
            ["docker"], returncode=1, stderr="Cannot connect to the Docker daemon"
        )
        with self.assertRaisesRegex(RuntimeError, "could not inspect container"):
            postgres._inspect_container()

    @patch.object(postgres, "_run")
    def test_status_path_refuses_missing_network_without_creating_it(self, run):
        run.return_value = completed(
            ["docker"], returncode=1, stderr="network blueprint-control-plane not found"
        )
        with self.assertRaisesRegex(RuntimeError, "network .* missing"):
            postgres._network_payload(NETWORK, create=False)
        run.assert_called_once()

    @patch.object(postgres, "_run")
    def test_stop_refuses_mismatched_container(self, run):
        run.side_effect = self.docker_side_effect(
            container=self.container_payload(alias="wrong")
        )
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "network alias"):
                postgres.stop()
        self.assertFalse(any(call.args[0][:3] == ["docker", "container", "stop"] for call in run.call_args_list))

    def test_refuses_mismatched_existing_resource_limit(self):
        payload = self.container_payload()
        payload["HostConfig"]["ShmSize"] = 64 * 1024**2
        with self.assertRaisesRegex(RuntimeError, "resource limits"):
            postgres._assert_matching_container(
                payload,
                image_id=IMAGE_ID,
                network=NETWORK,
                data_dir=self.data,
                password_file=self.password,
            )

    def test_refuses_existing_password_bypass_environment(self):
        payload = self.container_payload()
        payload["Config"]["Env"].append("POSTGRES_HOST_AUTH_METHOD=trust")
        with self.assertRaisesRegex(RuntimeError, "mismatched configuration"):
            postgres._assert_matching_container(
                payload,
                image_id=IMAGE_ID,
                network=NETWORK,
                data_dir=self.data,
                password_file=self.password,
            )

    @patch.object(postgres, "_run")
    def test_status_reports_matching_health(self, run):
        run.side_effect = self.docker_side_effect(
            container=self.container_payload(running=True)
        )
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(postgres.status(), 0)


if __name__ == "__main__":
    unittest.main()

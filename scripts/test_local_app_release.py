from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).parent))

import local_app_release as release

IMAGE_REF = release._IMAGE_PREFIX + "@sha256:" + "d" * 64
REVISION = "a" * 40
OLD_IMAGE = "sha256:" + "b" * 64
NEW_IMAGE = "sha256:" + "c" * 64


def completed(argv: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv or [], 0, "", "")


class LocalAppReleaseTests(unittest.TestCase):
    @staticmethod
    def _managed_container_record(
        name: str,
        image: str,
        container_id: str,
    ) -> dict[str, object]:
        role = "api" if name.endswith("api") else "worker"
        return {
            "Id": container_id,
            "Name": f"/{name}",
            "Image": image,
            "State": {"Running": True},
            "Config": {
                "Image": image,
                "Labels": (
                    {"com.blueprint.role": role} if role == "api" else {}
                ),
                "Env": [f"BLUEPRINT_PROCESS_ROLE={role}"],
            },
            "HostConfig": {
                "AutoRemove": True,
                "ReadonlyRootfs": True,
                "NetworkMode": "blueprint-control-plane",
            },
            "NetworkSettings": {
                "Networks": {"blueprint-control-plane": {}},
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Destination": "/var/run/blueprint/gcp.json",
                    "RW": False,
                }
            ],
        }

    @staticmethod
    def _tar_payload(entries: list[tuple[str, bytes, bytes]]) -> io.BytesIO:
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            for name, kind, content in entries:
                info = tarfile.TarInfo(name)
                info.type = kind
                info.size = len(content) if kind == tarfile.REGTYPE else 0
                archive.addfile(info, io.BytesIO(content) if content else None)
        payload.seek(0)
        return payload

    def test_frontend_tar_extraction_is_bounded_and_rejects_links(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            release._extract_frontend_tar(
                self._tar_payload(
                    [
                        (".", tarfile.DIRTYPE, b""),
                        ("assets", tarfile.DIRTYPE, b""),
                        ("assets/app-deadbeef.js", tarfile.REGTYPE, b"safe"),
                    ]
                ),
                target,
            )
            self.assertEqual(
                (target / "assets" / "app-deadbeef.js").read_bytes(), b"safe"
            )

        unsafe_archives = (
            [("../escape", tarfile.REGTYPE, b"bad")],
            [("asset-link", tarfile.SYMTYPE, b"")],
        )
        for entries in unsafe_archives:
            with self.subTest(entries=entries):
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(RuntimeError):
                        release._extract_frontend_tar(
                            self._tar_payload(entries), Path(directory)
                        )

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(release.frontend_manifest, "MAX_BYTES", 4):
                with self.assertRaisesRegex(RuntimeError, "size limits"):
                    release._extract_frontend_tar(
                        self._tar_payload(
                            [("oversized.js", tarfile.REGTYPE, b"12345")]
                        ),
                        Path(directory),
                    )

    def test_systemctl_properties_are_named_not_positional(self):
        self.assertEqual(
            release._parse_systemctl_properties(
                "NRestarts=0\nMainPID=123\nActiveState=active\n",
                {"ActiveState", "MainPID", "NRestarts"},
            ),
            {"ActiveState": "active", "MainPID": "123", "NRestarts": "0"},
        )

    def test_worker_readiness_executes_psql_in_real_database_container(self):
        container_id = "a" * 64

        def run(argv, **_kwargs):
            if argv[1:3] == ["exec", container_id]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps(
                        {
                            "status": "degraded",
                            "is_leader": True,
                            "orchestrator_alive": True,
                        }
                    ),
                    "",
                )
            return subprocess.CompletedProcess(argv, 0, "1\n", "")

        with (
            patch.object(
                release,
                "_container_record",
                return_value={"Id": container_id, "State": {"Running": True}},
            ),
            patch.object(release, "_run", side_effect=run) as run_mock,
        ):
            release._verify_worker_ready()
        self.assertEqual(
            run_mock.call_args_list[0].args[0],
            [
                "/usr/bin/docker",
                "exec",
                container_id,
                "/usr/local/bin/python",
                "-c",
                run_mock.call_args_list[0].args[0][-1],
            ],
        )
        self.assertIn(
            "http://127.0.0.1:8080/health",
            run_mock.call_args_list[0].args[0][-1],
        )
        database_call = run_mock.call_args_list[1].args[0]
        self.assertEqual(
            database_call[:7],
            [
                "/usr/bin/docker",
                "exec",
                "--user=postgres",
                "blueprint-local-postgres",
                "psql",
                "--username=blueprint",
                "--dbname=blueprint",
            ],
        )
        leadership_sql = database_call[-1]
        self.assertIn("classid='2984976049'::oid", leadership_sql)
        self.assertIn("objid='3944657387'::oid", leadership_sql)
        self.assertIn("objsubid=1", leadership_sql)
        self.assertIn("application_name='blueprint-worker'", leadership_sql)

    def test_worker_readiness_rejects_untrusted_container_id(self):
        with (
            patch.object(
                release,
                "_container_record",
                return_value={"Id": "--follow", "State": {"Running": True}},
            ),
            patch.object(release, "_run") as run_mock,
            self.assertRaisesRegex(RuntimeError, "trusted immutable ID"),
        ):
            release._verify_worker_ready()
        run_mock.assert_not_called()

    def test_worker_readiness_retries_until_internal_health_is_leader_ready(self):
        container_id = "a" * 64
        with (
            patch.object(
                release,
                "_container_record",
                return_value={"Id": container_id, "State": {"Running": True}},
            ),
            patch.object(
                release,
                "_run",
                return_value=subprocess.CompletedProcess([], 1, "", "starting"),
            ) as run_mock,
            self.assertRaisesRegex(release._TransientNotReady, "not ready"),
        ):
            release._verify_worker_ready()
        self.assertEqual(run_mock.call_count, 1)

    def test_successful_stop_waits_for_docker_auto_remove(self):
        name = "blueprint-local-worker"
        container_id = "1" * 64
        record = self._managed_container_record(name, OLD_IMAGE, container_id)
        calls: list[list[str]] = []

        def run(argv, **_kwargs):
            calls.append(argv)
            return completed(argv)

        with (
            patch.object(
                release,
                "_container_record",
                side_effect=[record, record],
            ),
            patch.object(release, "_run", side_effect=run),
            patch.object(release, "_wait_unit_stopped"),
        ):
            release._stop_unit_container(
                "blueprint-worker.service",
                name,
                frozenset({OLD_IMAGE, NEW_IMAGE}),
            )

        stop = next(call for call in calls if call[2:4] == ["stop", "--timeout"])
        self.assertEqual(stop[-1], container_id)
        self.assertNotEqual(stop[-1], name)
        self.assertEqual(
            [call for call in calls if call[2:4] == ["rm", "--force"]], []
        )

    def test_failed_stop_force_removes_only_captured_managed_container(self):
        name = "blueprint-local-worker"
        container_id = "5" * 64
        record = self._managed_container_record(name, OLD_IMAGE, container_id)
        calls: list[list[str]] = []

        def run(argv, **_kwargs):
            calls.append(argv)
            if argv[1:4] == ["container", "stop", "--timeout"]:
                return subprocess.CompletedProcess(argv, 1, "", "stop failed")
            return completed(argv)

        with (
            patch.object(release, "_container_record", side_effect=[record, record]),
            patch.object(release, "_run", side_effect=run),
            patch.object(release, "_wait_unit_stopped"),
        ):
            release._stop_unit_container(
                "blueprint-worker.service",
                name,
                frozenset({OLD_IMAGE, NEW_IMAGE}),
            )

        remove = next(call for call in calls if call[2:4] == ["rm", "--force"])
        self.assertEqual(remove[-1], container_id)
        self.assertNotEqual(remove[-1], name)

    def test_removal_already_in_progress_waits_without_duplicate_remove(self):
        name = "blueprint-local-worker"
        container_id = "6" * 64
        record = self._managed_container_record(name, OLD_IMAGE, container_id)
        calls: list[list[str]] = []

        def run(argv, **_kwargs):
            calls.append(argv)
            if argv[1:4] == ["container", "stop", "--timeout"]:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    "",
                    f"removal of container {container_id} is already in progress",
                )
            return completed(argv)

        with (
            patch.object(release, "_container_record", side_effect=[record, record]),
            patch.object(release, "_run", side_effect=run),
            patch.object(release, "_wait_unit_stopped") as wait_mock,
        ):
            release._stop_unit_container(
                "blueprint-worker.service",
                name,
                frozenset({OLD_IMAGE, NEW_IMAGE}),
            )

        self.assertEqual(
            [call for call in calls if call[2:4] == ["rm", "--force"]], []
        )
        wait_mock.assert_called_once_with(
            "blueprint-worker.service", name, container_id
        )

    def test_failed_stop_of_stopped_container_waits_without_force_remove(self):
        name = "blueprint-local-worker"
        container_id = "7" * 64
        record = self._managed_container_record(name, OLD_IMAGE, container_id)
        record["State"] = {"Running": False}
        calls: list[list[str]] = []

        def run(argv, **_kwargs):
            calls.append(argv)
            if argv[1:4] == ["container", "stop", "--timeout"]:
                return subprocess.CompletedProcess(argv, 1, "", "is not running")
            return completed(argv)

        with (
            patch.object(release, "_container_record", side_effect=[record, record]),
            patch.object(release, "_run", side_effect=run),
            patch.object(release, "_wait_unit_stopped") as wait_mock,
        ):
            release._stop_unit_container(
                "blueprint-worker.service",
                name,
                frozenset({OLD_IMAGE, NEW_IMAGE}),
            )

        self.assertEqual(
            [call for call in calls if call[2:4] == ["rm", "--force"]], []
        )
        wait_mock.assert_called_once()

    def test_force_remove_racing_with_auto_remove_uses_terminal_gate(self):
        name = "blueprint-local-worker"
        container_id = "8" * 64
        record = self._managed_container_record(name, OLD_IMAGE, container_id)
        calls: list[list[str]] = []

        def run(argv, **_kwargs):
            calls.append(argv)
            if argv[1:4] == ["container", "stop", "--timeout"]:
                return subprocess.CompletedProcess(argv, 1, "", "stop failed")
            if argv[1:4] == ["container", "rm", "--force"]:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    "",
                    f"removal of container {container_id} is already in progress",
                )
            return completed(argv)

        with (
            patch.object(release, "_container_record", side_effect=[record, record]),
            patch.object(release, "_run", side_effect=run),
            patch.object(release, "_wait_unit_stopped") as wait_mock,
        ):
            release._stop_unit_container(
                "blueprint-worker.service",
                name,
                frozenset({OLD_IMAGE, NEW_IMAGE}),
            )

        wait_mock.assert_called_once_with(
            "blueprint-worker.service", name, container_id
        )

    def test_failed_stop_after_auto_remove_uses_terminal_gate(self):
        name = "blueprint-local-worker"
        container_id = "9" * 64
        record = self._managed_container_record(name, OLD_IMAGE, container_id)
        calls: list[list[str]] = []

        def run(argv, **_kwargs):
            calls.append(argv)
            if argv[1:4] == ["container", "stop", "--timeout"]:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    "",
                    f"removal of container {container_id} is already in progress",
                )
            return completed(argv)

        with (
            patch.object(release, "_container_record", side_effect=[record, None]),
            patch.object(release, "_run", side_effect=run),
            patch.object(release, "_wait_unit_stopped") as wait_mock,
        ):
            release._stop_unit_container(
                "blueprint-worker.service",
                name,
                frozenset({OLD_IMAGE, NEW_IMAGE}),
            )

        self.assertEqual(
            [call for call in calls if call[2:4] == ["rm", "--force"]], []
        )
        wait_mock.assert_called_once_with(
            "blueprint-worker.service", name, container_id
        )

    def test_foreign_container_is_rejected_before_any_mutation(self):
        name = "blueprint-local-api"
        foreign = self._managed_container_record(name, "sha256:" + "9" * 64, "2" * 64)
        with (
            patch.object(release, "_container_record", return_value=foreign),
            patch.object(release, "_run") as run_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "unrecognized"):
                release._stop_unit_container(
                    "blueprint-api.service",
                    name,
                    frozenset({OLD_IMAGE, NEW_IMAGE}),
                )
        run_mock.assert_not_called()

    def test_container_name_reuse_does_not_remove_replacement(self):
        name = "blueprint-local-worker"
        original = self._managed_container_record(name, OLD_IMAGE, "3" * 64)
        replacement = self._managed_container_record(name, OLD_IMAGE, "4" * 64)
        calls: list[list[str]] = []

        def run(argv, **_kwargs):
            calls.append(argv)
            return completed(argv)

        with (
            patch.object(
                release,
                "_container_record",
                side_effect=[original, None, replacement],
            ),
            patch.object(release, "_run", side_effect=run),
        ):
            with self.assertRaisesRegex(RuntimeError, "reused"):
                release._stop_unit_container(
                    "blueprint-worker.service",
                    name,
                    frozenset({OLD_IMAGE, NEW_IMAGE}),
                )
        remove_calls = [call for call in calls if call[2:4] == ["rm", "--force"]]
        self.assertEqual(remove_calls, [])

    def test_wait_unit_stopped_requires_terminal_unit_and_absent_container(self):
        with (
            patch.object(
                release,
                "_unit_stop_state",
                side_effect=[("deactivating", "123"), ("inactive", "0")],
            ) as state_mock,
            patch.object(
                release,
                "_container_record",
                side_effect=[None, None, None, None],
            ) as record_mock,
            patch.object(release.time, "sleep"),
        ):
            release._wait_unit_stopped(
                "blueprint-worker.service",
                "blueprint-local-worker",
                "5" * 64,
                timeout=5,
            )
        self.assertEqual(state_mock.call_count, 2)
        self.assertEqual(record_mock.call_count, 4)

    def test_restart_waits_for_stop_before_start_and_readiness(self):
        events: list[str] = []
        with (
            patch.object(
                release,
                "_stop_unit_container",
                side_effect=lambda *_args, **_kwargs: events.append("stopped"),
            ),
            patch.object(
                release,
                "_run",
                side_effect=lambda argv, **_kwargs: (
                    events.append("started") or completed(argv)
                    if argv[:3] == ["/usr/bin/systemctl", "start", "blueprint-worker.service"]
                    else completed(argv)
                ),
            ),
            patch.object(
                release,
                "_wait_service_container",
                side_effect=lambda *_args, **_kwargs: events.append("ready"),
            ),
        ):
            release._restart_service(
                "blueprint-worker.service",
                NEW_IMAGE,
                allowed_images=frozenset({OLD_IMAGE, NEW_IMAGE}),
            )
        self.assertEqual(events, ["stopped", "started", "ready"])

    def test_transient_readiness_is_retried_until_success(self):
        attempts = 0

        def operation() -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise release._TransientNotReady("container not present yet")

        with patch.object(release.time, "sleep"):
            release._wait_verified(operation, "transient readiness", timeout=5, interval=0)
        self.assertEqual(attempts, 3)

    def test_connection_reset_during_startup_is_retried_until_success(self):
        operation = Mock(side_effect=[ConnectionResetError("peer reset"), None])
        with patch.object(release.time, "sleep"):
            release._wait_verified(
                operation, "API startup", timeout=5, interval=0
            )
        self.assertEqual(operation.call_count, 2)

    def test_terminal_readiness_failure_is_not_retried(self):
        operation = Mock(side_effect=RuntimeError("wrong image"))
        with (
            patch.object(release.time, "sleep") as sleep_mock,
            self.assertRaisesRegex(RuntimeError, "wrong image"),
        ):
            release._wait_verified(operation, "terminal failure", timeout=5, interval=0)
        operation.assert_called_once_with()
        sleep_mock.assert_not_called()

    def test_registry_credential_is_opened_safely_and_identity_pinned(self):
        credential = {
            "type": "service_account",
            "project_id": "blueprint-orchestrator",
            "client_email": release._WORKER_REGISTRY_READER,
            "private_key": "secret-private-key",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "worker.json"
            path.write_text(json.dumps(credential), encoding="utf-8")
            path.chmod(0o600)
            expected_uid = path.stat().st_uid
            self.assertEqual(
                json.loads(
                    release._secure_registry_credential(str(path), expected_uid)
                )["client_email"],
                release._WORKER_REGISTRY_READER,
            )

            with self.assertRaisesRegex(ValueError, "unsafe"):
                release._secure_registry_credential(str(path), expected_uid + 1)

            path.chmod(0o640)
            with self.assertRaisesRegex(ValueError, "unsafe"):
                release._secure_registry_credential(str(path), expected_uid)
            path.chmod(0o600)

            link = root / "worker-link.json"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                release._secure_registry_credential(str(link), expected_uid)

            hardlink = root / "worker-hardlink.json"
            os.link(path, hardlink)
            with self.assertRaisesRegex(ValueError, "unsafe"):
                release._secure_registry_credential(str(path), expected_uid)

            fifo = root / "worker.fifo"
            os.mkfifo(fifo, 0o600)
            with self.assertRaisesRegex(ValueError, "unsafe"):
                release._secure_registry_credential(str(fifo), expected_uid)

        with tempfile.TemporaryDirectory() as directory:
            wrong = Path(directory) / "wrong.json"
            credential["client_email"] = "foreign@example.invalid"
            wrong.write_text(json.dumps(credential), encoding="utf-8")
            wrong.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "wrong identity"):
                release._secure_registry_credential(
                    str(wrong), wrong.stat().st_uid
                )

    def test_registry_auth_uses_stdin_and_exact_disposable_config(self):
        secret = '{"private_key":"must-not-leak"}'
        with tempfile.TemporaryDirectory() as parent:
            auth_directory = Path(parent) / "blueprint-registry-auth-abcdef12"
            auth_directory.mkdir(mode=0o700)
            calls: list[tuple[list[str], dict[str, object]]] = []

            def run(argv, **kwargs):
                calls.append((argv, kwargs))
                if "login" in argv:
                    auth_directory.mkdir(exist_ok=True)
                    auth_directory.chmod(0o700)
                    config = auth_directory / "config.json"
                    config.write_text("{}", encoding="utf-8")
                    config.chmod(0o600)
                return completed(argv)

            with (
                patch.object(release, "_cleanup_stale_registry_auth"),
                patch.object(
                    release.tempfile,
                    "mkdtemp",
                    return_value=str(auth_directory),
                ),
                patch.object(release, "_run", side_effect=run),
            ):
                with release._registry_auth(secret) as config:
                    self.assertEqual(config, auth_directory)
                    self.assertTrue(auth_directory.is_dir())

            self.assertFalse(auth_directory.exists())
            login_argv, login_kwargs = calls[0]
            logout_argv, logout_kwargs = calls[-1]
            self.assertEqual(login_argv[1:3], ["--config", str(auth_directory)])
            self.assertEqual(logout_argv[1:3], ["--config", str(auth_directory)])
            self.assertEqual(login_kwargs["input_text"], secret)
            self.assertNotIn(secret, " ".join(login_argv + logout_argv))
            self.assertNotIn("input_text", logout_kwargs)

    def test_registry_auth_cleanup_runs_when_caller_fails(self):
        with tempfile.TemporaryDirectory() as parent:
            auth_directory = Path(parent) / "blueprint-registry-auth-abcdef12"
            auth_directory.mkdir(mode=0o700)

            def run(argv, **_kwargs):
                if "login" in argv:
                    auth_directory.mkdir(exist_ok=True)
                    auth_directory.chmod(0o700)
                    config = auth_directory / "config.json"
                    config.write_text("{}", encoding="utf-8")
                    config.chmod(0o600)
                return completed(argv)

            with (
                patch.object(release, "_cleanup_stale_registry_auth"),
                patch.object(
                    release.tempfile,
                    "mkdtemp",
                    return_value=str(auth_directory),
                ),
                patch.object(release, "_run", side_effect=run),
                self.assertRaisesRegex(RuntimeError, "candidate verification failed"),
            ):
                with release._registry_auth("secret"):
                    raise RuntimeError("candidate verification failed")
            self.assertFalse(auth_directory.exists())

    def test_registry_auth_removes_config_when_logout_raises(self):
        with tempfile.TemporaryDirectory() as parent:
            auth_directory = Path(parent) / "blueprint-registry-auth-abcdef12"
            auth_directory.mkdir(mode=0o700)

            def run(argv, **_kwargs):
                if "login" in argv:
                    config = auth_directory / "config.json"
                    config.write_text("{}", encoding="utf-8")
                    config.chmod(0o600)
                    return completed(argv)
                raise OSError("logout transport failed")

            with (
                patch.object(release, "_cleanup_stale_registry_auth"),
                patch.object(
                    release.tempfile,
                    "mkdtemp",
                    return_value=str(auth_directory),
                ),
                patch.object(release, "_run", side_effect=run),
                self.assertRaisesRegex(RuntimeError, "docker logout failed"),
            ):
                with release._registry_auth("secret"):
                    pass
            self.assertFalse(auth_directory.exists())

    def test_registry_login_failure_is_sanitized_and_cleans_config(self):
        secret = "private-key-fragment"
        with tempfile.TemporaryDirectory() as parent:
            auth_directory = Path(parent) / "blueprint-registry-auth-abcdef12"
            auth_directory.mkdir(mode=0o700)

            def run(argv, **_kwargs):
                if "login" in argv:
                    config = auth_directory / "config.json"
                    config.write_text("{}", encoding="utf-8")
                    config.chmod(0o600)
                    raise RuntimeError(f"provider echoed {secret}")
                return completed(argv)

            with (
                patch.object(release, "_cleanup_stale_registry_auth"),
                patch.object(
                    release.tempfile,
                    "mkdtemp",
                    return_value=str(auth_directory),
                ),
                patch.object(release, "_run", side_effect=run),
                self.assertRaisesRegex(RuntimeError, "registry login failed") as caught,
            ):
                with release._registry_auth(secret):
                    pass
            self.assertNotIn(secret, str(caught.exception))
            self.assertFalse(auth_directory.exists())

    def test_stale_registry_auth_cleanup_is_bounded_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            valid = root / "blueprint-registry-auth-abcdef12"
            valid.mkdir(mode=0o700)
            config = valid / "config.json"
            config.write_text("{}", encoding="utf-8")
            config.chmod(0o600)
            with patch.object(release.Path, "glob", return_value=[valid]):
                release._cleanup_stale_registry_auth(valid.stat().st_uid)
            self.assertFalse(valid.exists())

            unsafe = root / "blueprint-registry-auth-ghijkl34"
            unsafe.mkdir(mode=0o700)
            unexpected = unsafe / "unexpected"
            unexpected.write_text("do not delete", encoding="utf-8")
            with (
                patch.object(release.Path, "glob", return_value=[unsafe]),
                self.assertRaisesRegex(RuntimeError, "unexpected files"),
            ):
                release._cleanup_stale_registry_auth(unsafe.stat().st_uid)
            self.assertEqual(unexpected.read_text(encoding="utf-8"), "do not delete")

    def test_role_update_is_unique_and_preserves_other_bytes(self):
        original = b"# comment\nBLUEPRINT_API_IMAGE_ID=sha256:old\nKEEP=value\n"
        updated = release._replace_role_values(
            original,
            {
                "BLUEPRINT_API_IMAGE_ID": NEW_IMAGE,
                "BLUEPRINT_API_DEPLOY_SHA": REVISION,
            },
        )
        self.assertEqual(
            updated,
            (
                f"# comment\nBLUEPRINT_API_IMAGE_ID={NEW_IMAGE}\n"
                f"KEEP=value\nBLUEPRINT_API_DEPLOY_SHA={REVISION}\n"
            ).encode(),
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            release._replace_role_values(b"KEY=one\nKEY=two\n", {"KEY": "replacement"})

    @contextlib.contextmanager
    def _release_patches(
        self,
        *,
        health_error: Exception | None = None,
        evidence_side_effect: Exception | list[object] | None = None,
    ):
        api = (
            b"BLUEPRINT_API_IMAGE_ID="
            + OLD_IMAGE.encode()
            + b"\nBLUEPRINT_API_SECRETS_FILE=/safe/api.json\n"
        )
        worker = (
            b"BLUEPRINT_WORKER_IMAGE_ID="
            + OLD_IMAGE.encode()
            + b"\nBLUEPRINT_WORKER_GOOGLE_CREDENTIALS_FILE=/safe/worker.json\n"
        )

        def role_file(path: Path) -> bytes:
            return api if "/api/" in path.as_posix() else worker

        def marker(unit: str) -> tuple[str, str]:
            return "active", f"marker-{unit}"

        with contextlib.ExitStack() as stack:
            mocks = {
                "atomic": stack.enter_context(patch.object(release, "_atomic_write")),
                "run": stack.enter_context(
                    patch.object(
                        release,
                        "_run",
                        return_value=subprocess.CompletedProcess(
                            [], 0, "reachable\n", ""
                        ),
                    )
                ),
                "container": stack.enter_context(
                    patch.object(release, "_verify_container")
                ),
                "restart": stack.enter_context(
                    patch.object(release, "_restart_service")
                ),
            }
            stack.enter_context(patch.object(release, "_is_root", return_value=True))
            stack.enter_context(patch.object(release, "verify_privilege_boundary"))
            stack.enter_context(
                patch.dict(os.environ, {"SUDO_USER": "henry_hamster"}, clear=True)
            )
            stack.enter_context(patch.object(release, "_validate_manifest"))
            stack.enter_context(patch.object(release, "_trusted_root_directory"))
            stack.enter_context(
                patch.object(release, "_uid_for_user", return_value=1000)
            )
            stack.enter_context(
                patch.object(release, "_secure_role_file", side_effect=role_file)
            )
            stack.enter_context(
                patch.object(
                    release,
                    "_public_origin",
                    return_value="https://hamsterwheel.tail552e05.ts.net",
                )
            )
            stack.enter_context(
                patch.object(release, "_service_marker", side_effect=marker)
            )
            stack.enter_context(
                patch.object(
                    release, "_hash_protected_tree", return_value={"safe": "hash"}
                )
            )
            stack.enter_context(patch.object(release, "_assert_edge_is_decoupled"))
            stack.enter_context(
                patch.object(
                    release, "_local_version_payload", return_value=b'{"sha":"old"}'
                )
            )
            stack.enter_context(
                patch.object(
                    release,
                    "_public_snapshot",
                    return_value=(b'{"sha":"old"}', "root-hash"),
                )
            )
            stack.enter_context(patch.object(release, "_prepare_evidence_root"))
            stack.enter_context(
                patch.object(
                    release,
                    "_secure_registry_credential",
                    return_value='{"type":"service_account"}',
                )
            )
            mocks["registry"] = stack.enter_context(
                patch.object(
                    release,
                    "_registry_auth",
                    return_value=contextlib.nullcontext(Path("/tmp/registry")),
                )
            )
            stack.enter_context(
                patch.object(release, "_verify_frontend", return_value="e" * 64)
            )
            stack.enter_context(
                patch.object(release, "_inspect_candidate", return_value=NEW_IMAGE)
            )
            stack.enter_context(
                patch.object(
                    release, "_retain_prior_image", return_value="retained:old"
                )
            )
            mocks["image_schema"] = stack.enter_context(
                patch.object(release, "_image_schema_revision", return_value="0742")
            )
            mocks["database_schema"] = stack.enter_context(
                patch.object(release, "_database_schema_revision", return_value="0742")
            )
            mocks["preflight"] = stack.enter_context(
                patch.object(release, "_preflight_local_migration_inputs")
            )
            stack.enter_context(patch.object(release, "_verify_services_stable"))
            stack.enter_context(patch.object(release, "_verify_worker_ready"))
            stack.enter_context(
                patch.object(release, "_verify_health", side_effect=health_error)
                if health_error
                else patch.object(release, "_verify_health")
            )
            evidence_mock = stack.enter_context(
                patch.object(
                    release,
                    "_write_evidence",
                    return_value=Path(
                        "/var/lib/blueprint/local-app-releases/evidence.json"
                    ),
                    side_effect=evidence_side_effect,
                )
            )
            mocks["evidence"] = evidence_mock
            yield mocks

    def test_registry_cleanup_failure_never_mutates_roles_or_services(self):
        @contextlib.contextmanager
        def failing_registry_cleanup():
            yield Path("/tmp/registry")
            raise RuntimeError("registry credential cleanup failed")

        with self._release_patches() as mocks:
            mocks["registry"].return_value = failing_registry_cleanup()
            with self.assertRaisesRegex(
                RuntimeError, "registry credential cleanup failed"
            ):
                release.release(IMAGE_REF, REVISION)
            mocks["atomic"].assert_not_called()
            mocks["restart"].assert_not_called()

    def test_registry_cleanup_finishes_before_first_role_write(self):
        events: list[str] = []

        @contextlib.contextmanager
        def tracked_registry():
            yield Path("/tmp/registry")
            events.append("registry-cleaned")

        with self._release_patches() as mocks:
            mocks["registry"].side_effect = lambda _credential: tracked_registry()
            mocks["atomic"].side_effect = lambda *_args: events.append("role-write")
            release.release(IMAGE_REF, REVISION)
        self.assertLess(events.index("registry-cleaned"), events.index("role-write"))

    def test_candidate_pull_failure_never_mutates_roles_or_services(self):
        def run(argv, **_kwargs):
            if "pull" in argv:
                raise RuntimeError("candidate pull failed")
            return completed(argv)

        with self._release_patches() as mocks:
            mocks["run"].side_effect = run
            with self.assertRaisesRegex(RuntimeError, "candidate pull failed"):
                release.release(IMAGE_REF, REVISION)
            mocks["atomic"].assert_not_called()
            mocks["restart"].assert_not_called()

    def test_success_updates_only_api_and_worker_and_restarts_worker_first(self):
        with self._release_patches() as mocks:
            evidence = release.release(IMAGE_REF, REVISION)
            self.assertEqual(evidence.name, "evidence.json")
            self.assertEqual(mocks["atomic"].call_count, 2)
            written = {
                item.args[0].as_posix() for item in mocks["atomic"].call_args_list
            }
            self.assertEqual(
                written,
                {
                    "/etc/blueprint/nodes/henry_hamster/roles/api/role.env",
                    "/etc/blueprint/nodes/henry_hamster/roles/worker/role.env",
                },
            )
            payloads = {
                item.args[0].as_posix(): item.args[1].decode()
                for item in mocks["atomic"].call_args_list
            }
            worker_env = payloads[
                "/etc/blueprint/nodes/henry_hamster/roles/worker/role.env"
            ]
            self.assertIn(f"BLUEPRINT_WORKER_IMAGE_ID={NEW_IMAGE}\n", worker_env)
            self.assertIn(f"BLUEPRINT_WORKER_DEPLOY_SHA={REVISION}\n", worker_env)
            api_env = payloads[
                "/etc/blueprint/nodes/henry_hamster/roles/api/role.env"
            ]
            self.assertIn(f"BLUEPRINT_API_DEPLOY_SHA={REVISION}\n", api_env)
            self.assertEqual(
                mocks["restart"].call_args_list,
                [
                    call(
                        "blueprint-worker.service",
                        NEW_IMAGE,
                        allowed_images=frozenset({OLD_IMAGE, NEW_IMAGE}),
                    ),
                    call(
                        "blueprint-api.service",
                        NEW_IMAGE,
                        allowed_images=frozenset({OLD_IMAGE, NEW_IMAGE}),
                    ),
                ],
            )

    def test_nonmigration_deploy_does_not_stop_units(self):
        """The established hot-swap path remains untouched when schemas match."""
        with (
            self._release_patches() as mocks,
            patch.object(release, "_stop_unit_container") as stop,
            patch.object(release, "_run_local_migration") as migrate,
        ):
            release.release(IMAGE_REF, REVISION)
        stop.assert_not_called()
        migrate.assert_not_called()
        self.assertEqual(mocks["restart"].call_count, 2)

    def test_migration_deploy_holds_and_does_not_rollback_on_failure(self):
        """Once migration downtime starts, failures require a fix-forward release."""
        with self._release_patches() as mocks:
            mocks["image_schema"].return_value = "0743"
            mocks["database_schema"].return_value = "0742"
            with (
                patch.object(release, "_stop_unit_container") as stop,
                patch.object(
                    release,
                    "_run_local_migration",
                    side_effect=RuntimeError("upgrade failed"),
                ) as migrate,
                self.assertRaisesRegex(
                    RuntimeError,
                    "app units are intentionally left stopped.*do not roll back code.*fix forward",
                ),
            ):
                release.release(IMAGE_REF, REVISION)
        migrate.assert_called_once_with(IMAGE_REF)
        self.assertEqual(stop.call_count, 2)
        mocks["atomic"].assert_not_called()
        mocks["restart"].assert_not_called()
        self.assertEqual(mocks["evidence"].call_args.args[1]["outcome"], "migration_failed")

    def test_non_forward_schema_target_is_refused_before_stopping_units(self):
        """A rollback target must be rejected while both app units remain up."""
        with self._release_patches() as mocks:
            mocks["image_schema"].return_value = "0741"
            mocks["database_schema"].return_value = "0742"
            mocks["run"].return_value = subprocess.CompletedProcess(
                [], 0, "unreachable\n", ""
            )
            with (
                patch.object(release, "_stop_unit_container") as stop,
                patch.object(release, "_run_local_migration") as migrate,
                self.assertRaises(RuntimeError) as caught,
            ):
                release.release(IMAGE_REF, REVISION)

            stop.assert_not_called()
            migrate.assert_not_called()
            self.assertRegex(str(caught.exception), "not forward-reachable")
            self.assertEqual(
                mocks["evidence"].call_args.args[1]["outcome"],
                "rollback_refused",
            )

    def test_bad_migration_inputs_abort_before_any_downtime(self):
        """A deterministic input defect (e.g. a malformed worker secrets file)
        must fail the release BEFORE any Restart=always unit is stopped, so the
        control plane is never taken down for an error a preflight can catch.
        This is the v31 outage class: the worker-secret validation ran only
        inside the post-stop migration, so a rejected value stopped the control
        plane and then aborted with nothing to restart across the migration."""
        with self._release_patches() as mocks:
            mocks["image_schema"].return_value = "0743"
            mocks["database_schema"].return_value = "0742"
            mocks["preflight"].side_effect = ValueError(
                "worker secrets must be non-empty string keys mapping to string "
                "values without embedded NUL bytes"
            )
            with (
                patch.object(release, "_stop_unit_container") as stop,
                patch.object(release, "_run_local_migration") as migrate,
                self.assertRaisesRegex(Exception, "worker secrets"),
            ):
                release.release(IMAGE_REF, REVISION)
        mocks["preflight"].assert_called_once_with(IMAGE_REF)
        # Zero downtime, zero mutation: the preflight fired before the choreography.
        stop.assert_not_called()
        migrate.assert_not_called()
        mocks["atomic"].assert_not_called()
        mocks["restart"].assert_not_called()

    def test_pre_stop_rehearsal_reports_checked_and_deferred_preconditions(self):
        with (
            patch.object(release, "verify_privilege_boundary") as boundary,
            patch.object(release, "_preflight_local_migration_inputs") as inputs,
            patch.object(release, "_target_schema_refusal", return_value=None),
        ):
            report = release._run_pre_stop_rehearsal(
                NEW_IMAGE,
                IMAGE_REF,
                REVISION,
                "henry_hamster",
                "0743",
                "0742",
            )

        boundary.assert_called_once_with(
            "henry_hamster",
            agent_unit="blueprint-deploy-agent.service",
            release_unit="blueprint-release.service",
            run=release._run,
        )
        inputs.assert_called_once_with(IMAGE_REF)
        self.assertFalse(report.units_stopped)
        self.assertIn("worker secret", report.checked[1])
        self.assertIn("migration apply", report.deferred[0])
        self.assertEqual(report.as_dict()["units_stopped"], False)

    def test_rehearse_pulls_candidate_with_registry_auth_before_checks(self):
        with (
            patch.object(release, "_is_root", return_value=True),
            patch.dict(os.environ, {"SUDO_USER": "henry_hamster"}, clear=True),
            patch.object(release, "_pull_candidate_for_rehearsal") as pull,
            patch.object(release, "_verify_frontend", return_value="e" * 64),
            patch.object(release, "_inspect_candidate", return_value=NEW_IMAGE),
            patch.object(release, "_image_schema_revision", return_value="0742"),
            patch.object(release, "_database_schema_revision", return_value="0742"),
            patch.object(
                release,
                "_run_pre_stop_rehearsal",
                return_value=release.RehearsalReport(
                    IMAGE_REF,
                    REVISION,
                    ("candidate",),
                    ("downtime",),
                ),
            ) as rehearsal,
        ):
            report = release.rehearse(IMAGE_REF, REVISION)

        self.assertEqual(report.candidate, IMAGE_REF)
        pull.assert_called_once_with(IMAGE_REF, "henry_hamster")
        rehearsal.assert_called_once_with(
            NEW_IMAGE, IMAGE_REF, REVISION, "henry_hamster", "0742", "0742"
        )

    def test_worker_activation_failure_rolls_back_only_touched_worker(self):
        def restart(unit: str, image: str, **_kwargs) -> None:
            if unit == "blueprint-worker.service" and image == NEW_IMAGE:
                raise RuntimeError("worker activation failed")

        with self._release_patches() as mocks:
            mocks["restart"].side_effect = restart
            with self.assertRaisesRegex(RuntimeError, "rolled back"):
                release.release(IMAGE_REF, REVISION)
            self.assertEqual(
                mocks["restart"].call_args_list,
                [
                    call(
                        "blueprint-worker.service",
                        NEW_IMAGE,
                        allowed_images=frozenset({OLD_IMAGE, NEW_IMAGE}),
                    ),
                    call(
                        "blueprint-worker.service",
                        OLD_IMAGE,
                        allowed_images=frozenset({OLD_IMAGE, NEW_IMAGE}),
                    ),
                ],
            )

    def test_api_activation_failure_rolls_back_both_touched_units(self):
        def restart(unit: str, image: str, **_kwargs) -> None:
            if unit == "blueprint-api.service" and image == NEW_IMAGE:
                raise RuntimeError("API activation failed")

        with self._release_patches() as mocks:
            mocks["restart"].side_effect = restart
            with self.assertRaisesRegex(RuntimeError, "rolled back"):
                release.release(IMAGE_REF, REVISION)
            self.assertEqual(
                mocks["restart"].call_args_list,
                [
                    call(
                        "blueprint-worker.service",
                        NEW_IMAGE,
                        allowed_images=frozenset({OLD_IMAGE, NEW_IMAGE}),
                    ),
                    call(
                        "blueprint-api.service",
                        NEW_IMAGE,
                        allowed_images=frozenset({OLD_IMAGE, NEW_IMAGE}),
                    ),
                    call(
                        "blueprint-worker.service",
                        OLD_IMAGE,
                        allowed_images=frozenset({OLD_IMAGE, NEW_IMAGE}),
                    ),
                    call(
                        "blueprint-api.service",
                        OLD_IMAGE,
                        allowed_images=frozenset({OLD_IMAGE, NEW_IMAGE}),
                    ),
                ],
            )

    def test_rollback_refused_when_prior_image_schema_behind_db(self):
        with self._release_patches(
            health_error=RuntimeError("candidate health failed")
        ) as mocks:
            mocks["image_schema"].side_effect = ["0743", "0742"]
            mocks["database_schema"].return_value = "0743"
            with self.assertRaisesRegex(
                RuntimeError,
                "code-rollback refused: prior image .* expects schema 0742 "
                "but DB is at 0743.*fix forward",
            ):
                release.release(IMAGE_REF, REVISION)

            self.assertEqual(mocks["atomic"].call_count, 2)
            self.assertEqual(
                mocks["restart"].call_args_list,
                [
                    call(
                        "blueprint-worker.service",
                        NEW_IMAGE,
                        allowed_images=frozenset({OLD_IMAGE, NEW_IMAGE}),
                    ),
                    call(
                        "blueprint-api.service",
                        NEW_IMAGE,
                        allowed_images=frozenset({OLD_IMAGE, NEW_IMAGE}),
                    ),
                ],
            )
            mocks["container"].assert_not_called()
            self.assertEqual(
                mocks["evidence"].call_args.args[1]["outcome"],
                "rollback_refused",
            )

    def test_image_schema_revision_parses_one_expected_revision_and_fails_closed(self):
        with patch.object(
            release.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["docker", "run"],
                0,
                'EXPECTED_SCHEMA_REVISION = "0740"\n',
                "",
            ),
        ) as run_mock:
            self.assertEqual(release._image_schema_revision(OLD_IMAGE), "0740")
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 60)

        for stdout in ("", 'EXPECTED_SCHEMA_REVISION = "0740"\n' * 2):
            with self.subTest(stdout=stdout), patch.object(
                release.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout, ""),
            ):
                with self.assertRaisesRegex(RuntimeError, "one schema revision"):
                    release._image_schema_revision(OLD_IMAGE)

    def test_database_schema_revision_parses_one_head_and_fails_closed(self):
        with patch.object(
            release.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "0740\n", ""),
        ) as run_mock:
            self.assertEqual(release._database_schema_revision(), "0740")
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 30)

        for stdout in ("", "0740\n0741\n"):
            with self.subTest(stdout=stdout), patch.object(
                release.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout, ""),
            ):
                with self.assertRaisesRegex(RuntimeError, "one schema head"):
                    release._database_schema_revision()

    def test_target_schema_reachability_probe_fails_closed(self):
        with patch.object(
            release,
            "_run",
            side_effect=RuntimeError("probe unavailable"),
        ):
            refusal = release._target_schema_refusal(NEW_IMAGE, "0743", "0742")
        self.assertRegex(refusal or "", "could not determine migration reachability")

    def test_target_schema_reachability_accepts_explicit_descendant(self):
        with patch.object(
            release,
            "_run",
            return_value=subprocess.CompletedProcess([], 0, "reachable\n", ""),
        ) as probe:
            self.assertIsNone(
                release._target_schema_refusal(NEW_IMAGE, "0743", "0742")
            )
        command = probe.call_args.args[0]
        self.assertEqual(command[-2:], ["0742", "0743"])

    def test_rollback_proceeds_when_prior_image_schema_matches_db(self):
        with self._release_patches(
            health_error=RuntimeError("candidate health failed")
        ) as mocks:
            mocks["image_schema"].return_value = "0743"
            mocks["database_schema"].return_value = "0743"
            with self.assertRaisesRegex(RuntimeError, "rolled back"):
                release.release(IMAGE_REF, REVISION)

            self.assertEqual(
                mocks["restart"].call_args_list[-2:],
                [
                    call(
                        "blueprint-worker.service",
                        OLD_IMAGE,
                        allowed_images=frozenset({OLD_IMAGE, NEW_IMAGE}),
                    ),
                    call(
                        "blueprint-api.service",
                        OLD_IMAGE,
                        allowed_images=frozenset({OLD_IMAGE, NEW_IMAGE}),
                    ),
                ],
            )

    def test_preactivation_write_failure_never_mutates_services(self):
        with self._release_patches() as mocks:
            mocks["atomic"].side_effect = [
                None,
                OSError("worker role write failed"),
                None,
                None,
            ]
            with self.assertRaisesRegex(RuntimeError, "rolled back"):
                release.release(IMAGE_REF, REVISION)
            mocks["restart"].assert_not_called()

    def test_health_failure_restores_both_files_and_prior_containers(self):
        with self._release_patches(health_error=RuntimeError("bad health")) as mocks:
            with self.assertRaisesRegex(RuntimeError, "rolled back"):
                release.release(IMAGE_REF, REVISION)
            self.assertEqual(mocks["atomic"].call_count, 4)
            mocks["container"].assert_any_call("blueprint-local-api", OLD_IMAGE)
            mocks["container"].assert_any_call("blueprint-local-worker", OLD_IMAGE)

    def test_success_receipt_failure_rolls_back_and_records_failure(self):
        evidence_path = Path(
            "/var/lib/blueprint/local-app-releases/rollback-evidence.json"
        )
        with self._release_patches(
            evidence_side_effect=[OSError("disk write failed"), evidence_path]
        ) as mocks:
            with self.assertRaisesRegex(RuntimeError, "rolled back"):
                release.release(IMAGE_REF, REVISION)
            self.assertEqual(mocks["atomic"].call_count, 4)
            self.assertEqual(mocks["evidence"].call_count, 2)
            rollback_receipt = mocks["evidence"].call_args_list[1].args[1]
            self.assertEqual(rollback_receipt["outcome"], "rolled_back")

    def test_rollback_verification_failure_is_recorded_as_incomplete(self):
        with self._release_patches(health_error=RuntimeError("bad health")) as mocks:
            mocks["container"].side_effect = RuntimeError("old container missing")
            with self.assertRaisesRegex(RuntimeError, "ROLLBACK_INCOMPLETE"):
                release.release(IMAGE_REF, REVISION)
            rollback_receipt = mocks["evidence"].call_args.args[1]
            self.assertEqual(rollback_receipt["outcome"], "rollback_incomplete")

    def test_untrusted_inputs_fail_before_mutation(self):
        with patch.object(release, "_is_root", return_value=True):
            with self.assertRaisesRegex(ValueError, "image-ref"):
                release.release("example/image:latest", REVISION)
            with self.assertRaisesRegex(ValueError, "expected-revision"):
                release.release(IMAGE_REF, "short")


if __name__ == "__main__":
    unittest.main()

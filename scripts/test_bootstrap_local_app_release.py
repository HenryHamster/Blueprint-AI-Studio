from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).parent))

import blueprint
import blueprint_release as release
import bootstrap_local_app_release as bootstrap


class BootstrapLocalAppReleaseTests(unittest.TestCase):
    @staticmethod
    def _test_atomic_write(path: Path, payload: bytes, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(mode)

    def test_edge_unit_replaces_hard_dependency_with_soft_ordering(self):
        template = (
            Path(bootstrap.__file__).parent / "blueprint-edge-system.service"
        ).read_bytes()
        payload = bootstrap._edge_unit(
            template,
            "henry_hamster",
            "/etc/blueprint/nodes/henry_hamster/roles/edge/role.env",
            "/usr/local/lib/blueprint-node/edge-deadbeef/"
            "tailscale_funnel_service.py",
        ).decode()
        self.assertNotIn("BindsTo=", payload)
        self.assertIn("Wants=blueprint-api.service", payload)
        self.assertIn("Requires=tailscaled.service", payload)
        self.assertIn("User=henry_hamster", payload)
        self.assertIn(
            "ExecStart=/usr/bin/python3.12 "
            "/usr/local/lib/blueprint-node/edge-deadbeef/"
            "tailscale_funnel_service.py start",
            payload,
        )

    def test_effective_edge_dependencies_fail_closed_on_bind(self):
        healthy = (
            "After=blueprint-api.service tailscaled.service network-online.target\n"
            "BindsTo=\n"
            "Requires=tailscaled.service\n"
            "Wants=blueprint-api.service network-online.target\n"
        )
        with unittest.mock.patch.object(
            bootstrap,
            "_run",
            return_value=bootstrap.subprocess.CompletedProcess([], 0, healthy, ""),
        ):
            bootstrap._verify_edge_dependencies()
        bound = healthy.replace("BindsTo=\n", "BindsTo=blueprint-api.service\n")
        with unittest.mock.patch.object(
            bootstrap,
            "_run",
            return_value=bootstrap.subprocess.CompletedProcess([], 0, bound, ""),
        ):
            with self.assertRaisesRegex(RuntimeError, "hard-bound"):
                bootstrap._verify_edge_dependencies()

    def test_edge_runtime_accepts_live_systemd_property_shape(self):
        entrypoint = (
            "/usr/local/lib/blueprint-node/edge-9dcdbd88fde6846b0046f873316938300"
            "46380068b089817e8cb752a56f4adc3/tailscale_funnel_service.py"
        )

        def command(action: str) -> str:
            return (
                "{ path=/usr/bin/python3.12 ; argv[]=/usr/bin/python3.12 "
                f"{entrypoint} {action} ; ignore_errors=no ; start_time=[n/a] ; "
                "stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
            )

        properties = "\n".join(
            (
                "EnvironmentFiles=/etc/blueprint/nodes/henry_hamster/"
                "roles/edge/role.env (ignore_errors=no)",
                f"ExecStart={command('start')}",
                f"ExecStartPre={command('--check')}",
                f"ExecStop={command('stop')}",
                "User=henry_hamster",
            )
        )
        with (
            unittest.mock.patch.object(
                bootstrap,
                "_run",
                return_value=bootstrap.subprocess.CompletedProcess(
                    [], 0, properties + "\n", ""
                ),
            ),
            unittest.mock.patch.object(bootstrap, "_trusted_file"),
        ):
            self.assertEqual(
                bootstrap._edge_runtime("henry_hamster"),
                (
                    "/etc/blueprint/nodes/henry_hamster/roles/edge/role.env",
                    entrypoint,
                ),
            )

    def test_retirement_is_root_only_and_idempotent(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            legacy = directory / "10-noninteractive-funnel.conf"
            retired = directory / "retired-10-noninteractive-funnel.conf"
            legacy.write_bytes(b"legacy\n")
            legacy.chmod(0o644)
            first = {
                legacy: (b"legacy\n", 0o644),
                retired: None,
            }
            with (
                unittest.mock.patch.object(
                    bootstrap, "_LEGACY_EDGE_DROP_IN", legacy
                ),
                unittest.mock.patch.object(
                    bootstrap, "_RETIRED_EDGE_DROP_IN", retired
                ),
                unittest.mock.patch.object(
                    bootstrap, "_atomic_write", self._test_atomic_write
                ),
            ):
                bootstrap._retire_legacy_edge_drop_in(first)
                self.assertFalse(legacy.exists())
                self.assertEqual(retired.read_bytes(), b"legacy\n")
                self.assertEqual(stat.S_IMODE(retired.stat().st_mode), 0o600)

                second = {
                    legacy: None,
                    retired: (retired.read_bytes(), 0o600),
                }
                bootstrap._retire_legacy_edge_drop_in(second)
                self.assertEqual(retired.read_bytes(), b"legacy\n")
                self.assertEqual(stat.S_IMODE(retired.stat().st_mode), 0o600)

    def test_post_reload_failure_restores_exact_configuration(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            api = directory / "api.conf"
            edge = directory / "edge.service"
            legacy = directory / "10-noninteractive-funnel.conf"
            retired = directory / "retired-10-noninteractive-funnel.conf"
            original = {
                api: (b"old api\n", 0o640),
                edge: (b"old edge\n", 0o600),
                legacy: (b"legacy\n", 0o644),
                retired: None,
            }
            for path, snapshot in original.items():
                if snapshot is not None:
                    self._test_atomic_write(path, *snapshot)

            commands: list[list[str]] = []

            def run(argv: list[str]):
                commands.append(argv)
                return bootstrap.subprocess.CompletedProcess(
                    argv,
                    0,
                    "/trusted/bundle/local_api_service.py\n",
                    "",
                )

            with (
                unittest.mock.patch.object(bootstrap, "_API_DROP_IN", api),
                unittest.mock.patch.object(bootstrap, "_EDGE_UNIT", edge),
                unittest.mock.patch.object(
                    bootstrap, "_LEGACY_EDGE_DROP_IN", legacy
                ),
                unittest.mock.patch.object(
                    bootstrap, "_RETIRED_EDGE_DROP_IN", retired
                ),
                unittest.mock.patch.object(
                    bootstrap, "_atomic_write", self._test_atomic_write
                ),
                unittest.mock.patch.object(bootstrap, "_run", side_effect=run),
                unittest.mock.patch.object(
                    bootstrap,
                    "_verify_edge_dependencies",
                    side_effect=RuntimeError("verification failed"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "verification failed"):
                    bootstrap._install_service_configuration(
                        bundle=PurePosixPath("/trusted/bundle"),
                        edge_payload=b"new edge\n",
                        install_user="henry_hamster",
                        edge_runtime_before=("env", "entrypoint"),
                        markers_before={},
                        protected_before={},
                        previous=original,
                    )

            for path, snapshot in original.items():
                if snapshot is None:
                    self.assertFalse(path.exists())
                else:
                    payload, mode = snapshot
                    self.assertEqual(path.read_bytes(), payload)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
            self.assertEqual(
                commands.count(["/usr/bin/systemctl", "daemon-reload"]),
                2,
            )

    def test_only_intended_systemd_changes_are_excluded(self):
        hashes = {
            str(bootstrap._API_DROP_IN): "api",
            str(bootstrap._EDGE_UNIT): "edge",
            str(bootstrap._LEGACY_EDGE_DROP_IN): "legacy",
            "/etc/systemd/system/blueprint-edge.service.d/80-other.conf": "keep",
            "/etc/blueprint/nodes/henry_hamster/manifest.json": "manifest",
        }
        self.assertEqual(
            bootstrap._without_intentional_changes(hashes),
            {
                "/etc/systemd/system/blueprint-edge.service.d/80-other.conf": "keep",
                "/etc/blueprint/nodes/henry_hamster/manifest.json": "manifest",
            },
        )

    def test_bundle_digest_is_order_independent(self):
        first = bootstrap._bundle_digest({"a.py": b"a", "b.py": b"b"})
        second = bootstrap._bundle_digest({"b.py": b"b", "a.py": b"a"})
        self.assertEqual(first, second)

    def test_api_drop_in_resets_commands_to_reviewed_wrapper(self):
        payload = bootstrap._api_drop_in(PurePosixPath("/trusted/bundle")).decode()
        self.assertIn("ExecStartPre=\n", payload)
        self.assertIn(
            "ExecStartPre=/usr/bin/python3.12 /trusted/bundle/local_api_service.py --check",
            payload,
        )
        self.assertIn("ExecStart=\n", payload)
        self.assertTrue(payload.endswith("/trusted/bundle/local_api_service.py\n"))

    def test_systemctl_properties_allow_reordering(self):
        payload = "MainPID=\nActiveState=active\nActiveEnterTimestampMonotonic=688094\n"
        self.assertEqual(
            bootstrap._parse_systemctl_properties(
                payload,
                {"ActiveState", "MainPID", "ActiveEnterTimestampMonotonic"},
            ),
            {
                "ActiveState": "active",
                "MainPID": "",
                "ActiveEnterTimestampMonotonic": "688094",
            },
        )
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            bootstrap._parse_systemctl_properties(
                "ActiveState=active\n", {"ActiveState", "MainPID"}
            )

    def test_timer_marker_does_not_request_or_require_main_pid(self):
        result = bootstrap.subprocess.CompletedProcess(
            [],
            0,
            "ActiveState=active\nActiveEnterTimestampMonotonic=406788235451\n",
            "",
        )
        with unittest.mock.patch.object(
            bootstrap, "_run", return_value=result
        ) as run_mock:
            marker = bootstrap._marker("blueprint-database-backup.timer")
        self.assertEqual(marker, ("active", "", "406788235451"))
        self.assertNotIn("--property=MainPID", run_mock.call_args.args[0])

    def test_deploy_agent_unit_uses_systemd_owned_runtime_lock_directory(self):
        payload = bootstrap._deploy_agent_unit(
            PurePosixPath("/trusted/bundle"), "henry_hamster"
        ).decode()
        self.assertIn(
            "ExecStartPre=/usr/bin/python3.12 /trusted/bundle/blueprint.py "
            "deploy-agent --preflight",
            payload,
        )
        self.assertIn("StartLimitIntervalSec=60", payload)
        self.assertIn("StartLimitBurst=3", payload)
        self.assertLess(
            payload.index("StartLimitIntervalSec=60"), payload.index("[Service]")
        )
        self.assertLess(
            payload.index("StartLimitBurst=3"), payload.index("[Service]")
        )
        self.assertIn("User=henry_hamster", payload)
        # NoNewPrivileges must be set EXPLICITLY: systemctl's property getter
        # reports the configured field, not the value implied by the sandbox
        # options, so _verify_release_execution_boundary only reads "yes" when
        # the unit states it outright. Guard against a regression to implicit.
        self.assertIn("NoNewPrivileges=yes", payload)
        self.assertIn("RuntimeDirectory=blueprint-deploy-agent", payload)
        self.assertIn("RuntimeDirectoryMode=0750", payload)
        self.assertIn("StateDirectory=blueprint-deploy-agent", payload)
        self.assertIn(
            "--state-path /var/lib/blueprint-deploy-agent/deploy-agent.state", payload
        )
        self.assertIn(
            "--lock-path /run/blueprint-deploy-agent/deploy-agent.lock", payload
        )
        self.assertIn("--poll-interval 30", payload)

    def test_deploy_agent_unit_has_one_repository_writer(self):
        repo_root = Path(__file__).resolve().parents[1]
        canonical_script = repo_root / "scripts" / "bootstrap_local_app_release.py"
        writer_locations = []
        for path in sorted((repo_root / "scripts").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if node.func.id != "_atomic_write" or not node.args:
                    continue
                target = node.args[0]
                if isinstance(target, ast.Name) and target.id == "_DEPLOY_AGENT_UNIT":
                    writer_locations.append(path)

        self.assertEqual(writer_locations, [canonical_script])
        self.assertFalse(
            (repo_root / "scripts" / "blueprint-deploy-agent.service").exists(),
            "the removed template must not become a second unit source",
        )

        direct_documented_writers = []
        target = "/etc/systemd/system/blueprint-deploy-agent.service"
        write_markers = re.compile(r"\b(?:tee|install|cp|mv)\b|write_(?:text|bytes)")
        for path in sorted((repo_root / "docs").rglob("*.md")):
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if target in line and write_markers.search(line):
                    direct_documented_writers.append(f"{path}:{line_number}")
        self.assertEqual(direct_documented_writers, [])

    def test_single_deploy_executor_ships(self):
        repo_root = Path(__file__).resolve().parents[1]
        scripts_root = repo_root / "scripts"
        self.assertFalse(
            (scripts_root / "blueprint_deploy_agent.py").exists(),
            "the orphaned second deploy executor must not ship",
        )

        canonical_script = scripts_root / "bootstrap_local_app_release.py"
        writer_locations = []
        for path in sorted(scripts_root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if node.func.id != "_atomic_write" or not node.args:
                    continue
                target = node.args[0]
                if isinstance(target, ast.Name) and target.id == "_DEPLOY_AGENT_UNIT":
                    writer_locations.append(path)
        self.assertEqual(writer_locations, [canonical_script])

        orphan_lockfile_defaults = []
        for path in sorted(scripts_root.glob("*.py")):
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if "~/.blueprint/" + "deploy-agent.lock" in line:
                    orphan_lockfile_defaults.append(f"{path}:{line_number}")
        self.assertEqual(orphan_lockfile_defaults, [])

    def test_release_execution_is_root_owned_and_socket_activated(self):
        service = bootstrap._release_service_unit().decode()
        socket = bootstrap._release_socket_unit("henry_hamster").decode()
        self.assertIn("User=root", service)
        self.assertIn("ExecStart=/usr/local/sbin/blueprint-release --serve", service)
        self.assertIn("ListenStream=/run/blueprint-release.sock", socket)
        self.assertIn("SocketGroup=henry_hamster", socket)
        self.assertIn("SocketMode=0660", socket)
        self.assertNotIn("sudo", service + socket)

    def test_systemd_verify_parses_the_rendered_execution_units(self):
        if not Path("/usr/bin/systemd-analyze").is_file():
            self.skipTest(
                "systemd-analyze is unavailable at /usr/bin/systemd-analyze; "
                "runtime unit verification cannot run"
            )
        true_path = shutil.which("true")
        if true_path is None:
            self.skipTest("an executable true command is required for unit verification")
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            deploy_agent_unit = bootstrap._deploy_agent_unit(
                PurePosixPath("/usr/local/lib/bundle"), "henry_hamster"
            )
            deploy_agent_text = deploy_agent_unit.decode()
            exec_start_lines = [
                line
                for line in deploy_agent_text.splitlines()
                if line.startswith("ExecStart=")
            ]
            (root / "blueprint-deploy-agent.service").write_bytes(
                deploy_agent_unit.replace(
                    b"/usr/bin/python3.12", true_path.encode()
                )
            )
            (root / "blueprint-release.service").write_bytes(
                bootstrap._release_service_unit().replace(
                    b"/usr/local/sbin/blueprint-release", true_path.encode()
                )
            )
            (root / "blueprint-release.socket").write_bytes(
                bootstrap._release_socket_unit("henry_hamster")
            )
            result = subprocess.run(
                [
                    "/usr/bin/systemd-analyze",
                    "verify",
                    str(root / "blueprint-deploy-agent.service"),
                    str(root / "blueprint-release.service"),
                    str(root / "blueprint-release.socket"),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            diagnostics = "\n".join(
                output for output in (result.stdout, result.stderr) if output
            )
            parse_defect_markers = (
                "missing '='",
                "ignoring line",
                "unknown lvalue",
                "unknown section",
                "unknown key name",
                "unknown directive",
                "ignoring",
            )
            parse_defects = [
                line
                for line in diagnostics.splitlines()
                if any(marker in line.lower() for marker in parse_defect_markers)
            ]
            self.assertFalse(
                parse_defects,
                "systemd-analyze reported unit parse defects:\n"
                + (diagnostics or "<no diagnostics>"),
            )
            self.assertEqual(
                len(exec_start_lines),
                1,
                deploy_agent_text,
            )
            self.assertIn(
                "--state-path /var/lib/blueprint-deploy-agent/deploy-agent.state",
                exec_start_lines[0],
            )

    def test_effective_properties_keep_agent_hardened_and_release_root(self):
        outputs = iter(
            (
                "NoNewPrivileges=yes\nUser=henry_hamster\n",
                "NoNewPrivileges=no\nUser=root\n",
            )
        )
        with unittest.mock.patch.object(
            bootstrap,
            "_run",
            side_effect=lambda _argv: bootstrap.subprocess.CompletedProcess(
                [], 0, next(outputs), ""
            ),
        ):
            bootstrap._verify_release_execution_boundary("henry_hamster")

    def test_rehearse_delegates_to_candidate_release_tool(self):
        sentinel = object()
        with unittest.mock.patch(
            "local_app_release.rehearse", return_value=sentinel
        ) as rehearse:
            self.assertIs(
                bootstrap.rehearse("image@digest", "a" * 40), sentinel
            )
        rehearse.assert_called_once_with("image@digest", "a" * 40)

    def test_approved_activation_rehearses_root_handoff_and_receipt(self):
        deploy_blueprint = blueprint
        revision = "a" * 40
        promoted = {
            "version_number": 7,
            "status": "promoted",
            "promoted_sha": revision,
            "serving_status": "unconfirmed",
            "activation_requested_at": "2026-08-09T00:00:00Z",
        }
        calls = []

        class FakeReleaseSocket:
            def __init__(self):
                self.sent = b""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, timeout):
                self.timeout = timeout

            def setsockopt(self, *_args):
                pass

            def connect(self, path):
                self.path = path

            def sendall(self, payload):
                self.sent += payload

            def shutdown(self, _how):
                pass

            def close(self):
                pass

            def recv(self, _limit):
                return b"OK\n"

        release_socket = FakeReleaseSocket()

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body))
            return {
                "version_number": 7,
                "serving_status": "serving_confirmed",
                "served_sha": revision,
            }

        with (
            unittest.mock.patch.object(deploy_blueprint, "_require_write_config"),
            unittest.mock.patch.object(
                deploy_blueprint, "_read_live_approved_activation", return_value=promoted
            ),
            unittest.mock.patch.object(
                deploy_blueprint,
                "_fetch_deploy_manifest",
                return_value={
                    "run": {"databaseId": 1},
                    "digest": "sha256:" + "0" * 64,
                    "revision": revision,
                },
            ),
            unittest.mock.patch.object(
                deploy_blueprint.socket,
                "socket",
                return_value=release_socket,
            ),
            unittest.mock.patch.object(
                deploy_blueprint, "get_project_id", return_value="project-1"
            ),
            unittest.mock.patch.object(
                deploy_blueprint, "_local_served_sha", return_value=revision
            ),
            unittest.mock.patch.object(deploy_blueprint, "api", side_effect=fake_api),
        ):
            self.assertTrue(
                deploy_blueprint._deploy_agent_once(
                    type("Args", (), {"project_id": None})()
                )
            )

        self.assertEqual(
            release_socket.sent,
            ("sha256:" + "0" * 64 + " " + revision + "\n").encode("ascii"),
        )
        self.assertEqual(
            next(body for method, path, body in calls if method == "POST"),
            {"version_number": 7, "revision": revision, "host": "control-plane"},
        )

    def test_slow_release_timeout_is_one_shot_and_receipt_retries(self):
        """A release outliving the socket wait is not replayed on the next poll."""
        digest = "sha256:" + "0" * 64
        revision = "c" * 40
        promoted = {
            "version_number": 8,
            "status": "promoted",
            "promoted_sha": revision,
            "serving_status": "unconfirmed",
            "activation_requested_at": "2026-08-09T00:00:00Z",
        }
        calls = []
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            socket_path = directory / "blueprint-release.sock"
            release_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            release_socket.bind(str(socket_path))
            release_socket.listen(1)
            count_path = directory / "release-count"
            script = (
                "from pathlib import Path; import time; "
                f"p=Path({str(count_path)!r}); "
                "p.open('a', encoding='utf-8').write('release\\n'); "
                "time.sleep(0.15)"
            )
            original_fd = os.dup(release._SOCKET_FD)
            os.dup2(release_socket.fileno(), release._SOCKET_FD)

            def fake_api(method, path, body=None, timeout=30):
                calls.append((method, path, body))
                return {"serving_status": "serving_confirmed", "served_sha": revision}

            args = type(
                "Args",
                (),
                {
                    "project_id": None,
                    "state_path": str(directory / "missing" / "agent.state"),
                    "poll_state_path": str(directory / "missing" / "agent.poll"),
                },
            )()
            server = threading.Thread(target=release._serve_one_request)
            try:
                with (
                    unittest.mock.patch.object(blueprint, "_RELEASE_SOCKET_PATH", str(socket_path)),
                    unittest.mock.patch.object(blueprint, "_RELEASE_SOCKET_TIMEOUT_SECONDS", 0.03),
                    unittest.mock.patch.object(blueprint, "_require_write_config"),
                    unittest.mock.patch.object(
                        blueprint, "_read_live_approved_activation", return_value=promoted
                    ),
                    unittest.mock.patch.object(
                        blueprint,
                        "_fetch_deploy_manifest",
                        return_value={"run": {}, "digest": digest, "revision": revision},
                    ) as fetch_manifest,
                    unittest.mock.patch.object(blueprint, "get_project_id", return_value="project-1"),
                    unittest.mock.patch.object(
                        blueprint, "_local_served_sha", return_value=revision
                    ),
                    unittest.mock.patch.object(blueprint, "api", side_effect=fake_api),
                    unittest.mock.patch.object(
                        release,
                        "_release_command",
                        return_value=[sys.executable, "-c", script],
                    ),
                    unittest.mock.patch.object(
                        release.os, "geteuid", return_value=0
                    ),
                ):
                    server.start()
                    self.assertFalse(blueprint._deploy_agent_once(args))
                    server.join(timeout=2)
                    self.assertFalse(server.is_alive())
                    self.assertEqual(count_path.read_text(encoding="utf-8"), "release\n")
                    self.assertEqual(
                        [item for item in calls if item[0] == "POST"], [],
                    )

                    self.assertTrue(blueprint._deploy_agent_once(args))
                    self.assertEqual(count_path.read_text(encoding="utf-8"), "release\n")
                    receipts = [item for item in calls if item[0] == "POST"]
                    self.assertEqual(len(receipts), 1)
                    self.assertEqual(
                        receipts[0][2],
                        {"version_number": 8, "revision": revision, "host": "control-plane"},
                    )
                    fetch_manifest.assert_called_once()
            finally:
                os.dup2(original_fd, release._SOCKET_FD)
                os.close(original_fd)
                release_socket.close()

    def test_resume_after_started_phase_does_not_re_release(self):
        """A restarted agent reconciles its release fence into one receipt."""
        revision = "f" * 40
        promoted = {
            "version_number": 12,
            "status": "promoted",
            "promoted_sha": revision,
            "serving_status": "unconfirmed",
            "activation_requested_at": "2026-08-09T00:00:00Z",
        }
        calls = []

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body))
            return {"serving_status": "serving_confirmed", "served_sha": revision}

        with tempfile.TemporaryDirectory() as raw_directory:
            state_path = Path(raw_directory) / "deploy-agent.state"
            state_path.write_text(
                json.dumps({
                    "version_number": promoted["version_number"],
                    "promoted_sha": revision,
                    "revision": revision,
                    "phase": "started",
                }),
                encoding="utf-8",
            )
            args = type(
                "Args",
                (),
                {"project_id": None, "state_path": str(state_path)},
            )()
            with (
                unittest.mock.patch.object(blueprint, "_require_write_config"),
                unittest.mock.patch.object(
                    blueprint, "_read_live_approved_activation", return_value=promoted
                ),
                unittest.mock.patch.object(
                    blueprint, "_local_served_sha", return_value=revision
                ),
                unittest.mock.patch.object(
                    blueprint, "_invoke_release_shim"
                ) as invoke_release,
                unittest.mock.patch.object(
                    blueprint, "get_project_id", return_value="project-1"
                ),
                unittest.mock.patch.object(blueprint, "api", side_effect=fake_api),
            ):
                self.assertTrue(blueprint._deploy_agent_once(args))

            invoke_release.assert_not_called()
            receipts = [item for item in calls if item[0] == "POST"]
            self.assertEqual(len(receipts), 1)
            self.assertEqual(
                receipts[0][2],
                {
                    "version_number": promoted["version_number"],
                    "revision": revision,
                    "host": "control-plane",
                },
            )
            self.assertFalse(state_path.exists())

    def test_deploy_agent_poll_survives_non_api_error(self):
        """Transient poll parsing errors do not terminate the long-running unit."""
        args = type(
            "Args",
            (),
            {"poll_interval": 1, "once": False, "lock_path": "/tmp/unused-lock"},
        )()
        lock = unittest.mock.Mock()
        attempts = []
        sleep_calls = []

        def poll_once(_args):
            attempts.append(True)
            if len(attempts) == 1:
                raise json.JSONDecodeError("invalid approval", "{", 1)
            return False

        def sleep(interval):
            sleep_calls.append(interval)
            args.once = True

        stderr = io.StringIO()
        with (
            unittest.mock.patch.object(blueprint, "_require_write_config"),
            unittest.mock.patch.object(
                blueprint, "_acquire_deploy_agent_lock", return_value=lock
            ),
            unittest.mock.patch.object(blueprint, "_deploy_agent_once", side_effect=poll_once),
            unittest.mock.patch.object(blueprint.time, "sleep", side_effect=sleep),
            unittest.mock.patch.object(blueprint.sys, "stderr", stderr),
        ):
            self.assertIsNone(blueprint.cmd_deploy_agent(args))

        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleep_calls, [1])
        self.assertIn("invalid approval", stderr.getvalue())
        lock.close.assert_called_once_with()

    def test_send_timeout_after_release_handoff_retries_receipt_without_second_release(self):
        """An in-flight release parks the receipt without replaying the release."""
        revision = "d" * 40
        promoted = {
            "version_number": 9,
            "status": "promoted",
            "promoted_sha": revision,
            "serving_status": "unconfirmed",
            "activation_requested_at": "2026-08-09T00:00:00Z",
        }
        calls = []
        release_calls = []

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body))
            return {"serving_status": "serving_confirmed", "served_sha": revision}

        with tempfile.TemporaryDirectory() as raw_directory:
            state_path = Path(raw_directory) / "nested" / "agent.state"
            args = type("Args", (), {"project_id": None, "state_path": str(state_path)})()
            with (
                unittest.mock.patch.object(blueprint, "_require_write_config"),
                unittest.mock.patch.object(
                    blueprint, "_read_live_approved_activation", return_value=promoted
                ),
                unittest.mock.patch.object(
                    blueprint,
                    "_fetch_deploy_manifest",
                    return_value={
                        "run": {},
                        "digest": "sha256:" + "1" * 64,
                        "revision": revision,
                    },
                ),
                unittest.mock.patch.object(blueprint, "get_project_id", return_value="project-1"),
                unittest.mock.patch.object(
                    blueprint, "_local_served_sha", return_value=revision
                ),
                unittest.mock.patch.object(blueprint, "api", side_effect=fake_api),
                unittest.mock.patch.object(
                    blueprint,
                    "_invoke_release_shim",
                    side_effect=lambda *args, **kwargs: (
                        release_calls.append(args),
                        False,
                    )[1],
                ),
            ):
                self.assertFalse(blueprint._deploy_agent_once(args))
                self.assertTrue(state_path.exists())
                self.assertEqual(json.loads(state_path.read_text())["phase"], "accepted")

                self.assertTrue(blueprint._deploy_agent_once(args))
                self.assertEqual(len(release_calls), 1)
                self.assertEqual(
                    len([item for item in calls if item[0] == "POST"]), 1
                )
                self.assertFalse(state_path.exists())

    def test_confirmation_pending_does_not_reinvoke_release_or_post_receipt(self):
        """A not-yet-serving SHA remains pending without replaying activation."""
        revision = "e" * 40
        promoted = {
            "version_number": 10,
            "status": "promoted",
            "promoted_sha": revision,
            "serving_status": "unconfirmed",
            "activation_requested_at": "2026-08-09T00:00:00Z",
        }
        calls = []
        release_calls = []

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body))
            return {"serving_status": "serving_confirmed", "served_sha": revision}

        now = blueprint.datetime(2026, 8, 9, 0, 1, tzinfo=blueprint.timezone.utc)
        with tempfile.TemporaryDirectory() as raw_directory:
            state_path = Path(raw_directory) / "agent.state"
            args = type("Args", (), {"project_id": None, "state_path": str(state_path)})()
            with (
                unittest.mock.patch.object(blueprint, "_require_write_config"),
                unittest.mock.patch.object(
                    blueprint, "_read_live_approved_activation", return_value=promoted
                ),
                unittest.mock.patch.object(
                    blueprint,
                    "_fetch_deploy_manifest",
                    return_value={
                        "run": {},
                        "digest": "sha256:" + "2" * 64,
                        "revision": revision,
                    },
                ),
                unittest.mock.patch.object(blueprint, "get_project_id", return_value="project-1"),
                unittest.mock.patch.object(
                    blueprint,
                    "_invoke_release_shim",
                    side_effect=lambda *args, **kwargs: (
                        release_calls.append(args),
                        True,
                    )[1],
                ),
                unittest.mock.patch.object(
                    blueprint, "_local_served_sha", return_value="f" * 40
                ),
                unittest.mock.patch.object(
                    blueprint, "_activation_confirmation_now", return_value=now
                ),
                unittest.mock.patch.object(blueprint, "api", side_effect=fake_api),
            ):
                self.assertFalse(blueprint._deploy_agent_once(args))
                self.assertFalse(blueprint._deploy_agent_once(args))

            self.assertEqual(len(release_calls), 1)
            self.assertEqual([item for item in calls if item[0] == "POST"], [])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "completed")
            self.assertIn("confirmation_deadline_at", state)

    def test_confirmation_deadline_latches_failed_without_receipt(self):
        """A missed serving deadline fails durably and never posts a receipt."""
        revision = "1" * 40
        promoted = {
            "version_number": 11,
            "status": "promoted",
            "promoted_sha": revision,
            "serving_status": "unconfirmed",
            "activation_requested_at": "2026-08-09T00:00:00Z",
        }
        calls = []
        deadline = blueprint.datetime(2026, 8, 9, tzinfo=blueprint.timezone.utc)

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body))
            return {"serving_status": "serving_confirmed", "served_sha": revision}

        with tempfile.TemporaryDirectory() as raw_directory:
            state_path = Path(raw_directory) / "agent.state"
            state_path.write_text(
                json.dumps(
                    {
                        "version_number": 11,
                        "promoted_sha": revision,
                        "revision": revision,
                        "phase": "completed",
                        "confirmation_deadline_at": deadline.isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            args = type("Args", (), {"project_id": None, "state_path": str(state_path)})()
            with (
                unittest.mock.patch.object(blueprint, "_require_write_config"),
                unittest.mock.patch.object(
                    blueprint, "_read_live_approved_activation", return_value=promoted
                ),
                unittest.mock.patch.object(
                    blueprint, "_local_served_sha", return_value="2" * 40
                ),
                unittest.mock.patch.object(
                    blueprint,
                    "_activation_confirmation_now",
                    return_value=blueprint.datetime(
                        2026, 8, 9, 0, 15, tzinfo=blueprint.timezone.utc
                    ),
                ),
                unittest.mock.patch.object(blueprint, "api", side_effect=fake_api),
                unittest.mock.patch.object(
                    blueprint, "_invoke_release_shim"
                ) as invoke_release,
            ):
                self.assertFalse(blueprint._deploy_agent_once(args))
                self.assertFalse(blueprint._deploy_agent_once(args))

            self.assertEqual(invoke_release.call_count, 0)
            self.assertEqual([item for item in calls if item[0] == "POST"], [])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "failed")
            self.assertEqual(
                state["failure_reason"],
                "served_sha_not_confirmed_before_deadline",
            )

    def test_local_serving_probe_accepts_full_sha_and_normalizes_case(self):
        revision = "A" * 40

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(
                    {"sha": "A" * 40, "revision": "backend-revision"}
                ).encode()

        with unittest.mock.patch.object(blueprint, "urlopen", return_value=Response()):
            self.assertEqual(blueprint._local_served_sha(), revision.lower())

    def test_version_endpoint_exposes_full_sha_to_serving_probe(self):
        """Pin the backend endpoint to the full-SHA activation contract."""
        main_path = Path(__file__).parents[1] / "backend" / "app" / "main.py"
        tree = ast.parse(main_path.read_text(encoding="utf-8"), filename=str(main_path))
        version_route = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "get_version"
        )
        self.assertIn(
            "/api/version",
            [
                decorator.args[0].value
                for decorator in version_route.decorator_list
                if isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "get"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
            ],
        )
        return_node = next(
            node for node in version_route.body if isinstance(node, ast.Return)
        )
        self.assertIsInstance(return_node.value, ast.Dict)
        fields = {
            key.value: value
            for key, value in zip(return_node.value.keys, return_node.value.values)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        self.assertIsInstance(fields["sha"], ast.Name)
        self.assertEqual(fields["sha"].id, "_current_sha")
        self.assertIsInstance(fields["revision"], ast.Name)
        self.assertEqual(fields["revision"].id, "_current_revision")

    def test_manual_lock_path_creates_missing_parent(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            lock_path = Path(raw_directory) / "missing" / "deploy-agent.lock"
            handle = blueprint._acquire_deploy_agent_lock(str(lock_path))
            try:
                self.assertIsNotNone(handle)
                self.assertTrue(lock_path.parent.is_dir())
            finally:
                if handle is not None:
                    handle.close()

    def test_release_serve_accepts_the_exact_agent_wire_payload(self):
        digest = "sha256:" + "0" * 64
        revision = "a" * 40
        payload = f"{digest} {revision}\n".encode("ascii")
        release_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

        with tempfile.TemporaryDirectory() as raw_directory:
            socket_path = str(Path(raw_directory) / "blueprint-release.sock")
            release_socket.bind(socket_path)
            release_socket.listen(1)
            try:
                original_fd = os.dup(release._SOCKET_FD)
            except OSError:
                original_fd = None
            os.dup2(release_socket.fileno(), release._SOCKET_FD)
            try:
                with unittest.mock.patch.object(
                    release.os, "geteuid", return_value=0
                ), unittest.mock.patch.object(
                    release, "_run_release", return_value=0
                ) as run_release:
                    server = threading.Thread(target=release._serve_one_request)
                    server.start()
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.connect(socket_path)
                        client.sendall(payload)
                        self.assertEqual(client.recv(256), b"OK\n")
                    server.join(timeout=2)
                    self.assertFalse(server.is_alive())

                run_release.assert_called_once_with(digest, revision)
            finally:
                if original_fd is None:
                    os.close(release._SOCKET_FD)
                else:
                    os.dup2(original_fd, release._SOCKET_FD)
                    os.close(original_fd)
                release_socket.close()

    def test_release_serve_reports_nonzero_release(self):
        digest = "sha256:" + "1" * 64
        revision = "b" * 40
        payload = f"{digest} {revision}\n".encode("ascii")
        release_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

        with tempfile.TemporaryDirectory() as raw_directory:
            socket_path = str(Path(raw_directory) / "blueprint-release.sock")
            release_socket.bind(socket_path)
            release_socket.listen(1)
            try:
                original_fd = os.dup(release._SOCKET_FD)
            except OSError:
                original_fd = None
            os.dup2(release_socket.fileno(), release._SOCKET_FD)
            try:
                with unittest.mock.patch.object(
                    release.os, "geteuid", return_value=0
                ), unittest.mock.patch.object(
                    release, "_run_release", return_value=17
                ) as run_release:
                    server = threading.Thread(target=release._serve_one_request)
                    server.start()
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.connect(socket_path)
                        client.sendall(payload)
                        self.assertEqual(
                            client.recv(256), b"ERROR release exited 17\n"
                        )
                    server.join(timeout=2)
                    self.assertFalse(server.is_alive())

                run_release.assert_called_once_with(digest, revision)
            finally:
                if original_fd is None:
                    os.close(release._SOCKET_FD)
                else:
                    os.dup2(original_fd, release._SOCKET_FD)
                    os.close(original_fd)
                release_socket.close()

    def test_release_validator_rejects_malformed_payloads_before_release(self):
        digest = "sha256:" + "0" * 64
        revision = "a" * 40
        malformed = (
            (),
            (digest,),
            (digest, revision, "extra"),
            (digest + "x", revision),
            (digest, revision + "a"),
            ("é", revision),
            ("x" * 257,),
        )

        for args in malformed:
            with self.subTest(args=args):
                with (
                    unittest.mock.patch.object(
                        release, "_fail", side_effect=SystemExit(2)
                    ) as fail,
                    unittest.mock.patch.object(release, "_run_release") as run_release,
                ):
                    with self.assertRaises(SystemExit):
                        release._validated_args(list(args))
                fail.assert_called_once()
                run_release.assert_not_called()

    def test_deploy_agent_install_restarts_existing_service_after_update(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            unit = directory / "blueprint-deploy-agent.service"
            shim = directory / "blueprint-release"
            release_service = directory / "blueprint-release.service"
            release_socket = directory / "blueprint-release.socket"
            legacy_sudoers = directory / "blueprint-release sudoers"
            legacy_sudoers.write_bytes(b"henry_hamster ALL=(root) NOPASSWD: old\n")
            commands = []

            def run(argv):
                commands.append(argv)
                return bootstrap.subprocess.CompletedProcess(argv, 0, "", "")

            with (
                unittest.mock.patch.object(bootstrap, "_DEPLOY_AGENT_UNIT", unit),
                unittest.mock.patch.object(bootstrap, "_RELEASE_SERVICE", release_service),
                unittest.mock.patch.object(bootstrap, "_RELEASE_SOCKET_UNIT", release_socket),
                unittest.mock.patch.object(bootstrap, "_RELEASE_SHIM", shim),
                unittest.mock.patch.object(bootstrap, "_LEGACY_RELEASE_SUDOERS", legacy_sudoers),
                unittest.mock.patch.object(
                    bootstrap, "_atomic_write", self._test_atomic_write
                ),
                unittest.mock.patch.object(
                    bootstrap,
                    "_snapshot_optional",
                    side_effect=lambda path: (
                        (legacy_sudoers.read_bytes(), 0o440)
                        if path == legacy_sudoers
                        else None
                    ),
                ),
                unittest.mock.patch.object(bootstrap, "_run", side_effect=run),
                unittest.mock.patch.object(
                    bootstrap,
                    "_verify_release_execution_boundary",
                ),
            ):
                bootstrap._install_deploy_agent(
                    bundle=PurePosixPath("/trusted/new-bundle"),
                    install_user="henry_hamster",
                    release_shim=b"new shim\n",
                )

            self.assertEqual(commands[:2], [
                ["/usr/bin/systemctl", "daemon-reload"],
                ["/usr/bin/systemd-analyze", "verify", str(unit), str(release_service), str(release_socket)],
            ])
            self.assertEqual(
                commands[-1],
                ["/usr/bin/systemctl", "restart", "blueprint-deploy-agent.service"],
            )
            self.assertIn(
                ["/usr/bin/systemctl", "enable", "--now", "blueprint-release.socket"],
                commands,
            )
            self.assertFalse(legacy_sudoers.exists())


if __name__ == "__main__":
    unittest.main()

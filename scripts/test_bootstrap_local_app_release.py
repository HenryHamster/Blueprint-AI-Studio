from __future__ import annotations

import stat
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).parent))

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
        self.assertIn("User=henry_hamster", payload)
        self.assertIn("RuntimeDirectory=blueprint-deploy-agent", payload)
        self.assertIn("RuntimeDirectoryMode=0750", payload)
        self.assertIn(
            "--lock-path /run/blueprint-deploy-agent/deploy-agent.lock", payload
        )
        self.assertIn("--poll-interval 30", payload)
        self.assertNotIn("NoNewPrivileges=true", payload)

    def test_deploy_agent_install_restarts_existing_service_after_update(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            unit = directory / "blueprint-deploy-agent.service"
            shim = directory / "blueprint-release"
            sudoers = directory / "blueprint-release sudoers"
            commands = []

            def run(argv):
                commands.append(argv)
                return bootstrap.subprocess.CompletedProcess(argv, 0, "", "")

            with (
                unittest.mock.patch.object(bootstrap, "_DEPLOY_AGENT_UNIT", unit),
                unittest.mock.patch.object(bootstrap, "_RELEASE_SHIM", shim),
                unittest.mock.patch.object(bootstrap, "_RELEASE_SUDOERS", sudoers),
                unittest.mock.patch.object(
                    bootstrap, "_atomic_write", self._test_atomic_write
                ),
                unittest.mock.patch.object(
                    bootstrap, "_snapshot_optional",
                    return_value=None,
                ),
                unittest.mock.patch.object(bootstrap, "_run", side_effect=run),
            ):
                bootstrap._install_deploy_agent(
                    bundle=PurePosixPath("/trusted/new-bundle"),
                    install_user="henry_hamster",
                    release_shim=b"new shim\n",
                )

            self.assertEqual(commands[:2], [
                ["/usr/bin/systemctl", "daemon-reload"],
                ["/usr/bin/systemctl", "enable", "--now", "blueprint-deploy-agent.service"],
            ])
            self.assertEqual(
                commands[-1],
                ["/usr/bin/systemctl", "restart", "blueprint-deploy-agent.service"],
            )


if __name__ == "__main__":
    unittest.main()

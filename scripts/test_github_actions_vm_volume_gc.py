"""Runtime tests for Actions VM volume garbage collection."""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ASSET_DIR = Path(__file__).parent / "github-actions-vm"


class VolumeGcRuntimeTests(unittest.TestCase):
    def _volume_gc_harness(
        self,
        temporary: str,
        *,
        freezer_state: str,
        runner_state: str = "active",
        inactive_cgroup_pid: int | None = None,
    ) -> tuple[dict[str, str], Path, Path]:
        root = Path(temporary)
        state_root = root / "state"
        socket_root = root / "sockets"
        ownership_root = root / "ownership"
        cgroup_root = root / "cgroup"
        mock_bin = root / "bin"
        log_path = root / "systemctl.log"
        for path in (state_root, socket_root / "1", cgroup_root, mock_bin):
            path.mkdir(parents=True, exist_ok=True)

        cgroup = (
            cgroup_root
            / "system.slice"
            / "system-blueprint\\x2dgithub\\x2dactions.slice"
            / "blueprint-github-actions@1.service"
        )
        if runner_state == "active" or inactive_cgroup_pid is not None:
            cgroup.mkdir(parents=True)
            cgroup_pid = os.getpid() if runner_state == "active" else inactive_cgroup_pid
            (cgroup / "cgroup.procs").write_text(f"{cgroup_pid}\n", encoding="utf-8")

        commands = {
            "df": """
                #!/usr/bin/env bash
                printf 'Size Avail Use%%\\n100000000000 1000000000 99%%\\n'
            """,
            "install": """
                #!/usr/bin/env bash
                mkdir -p -- "${@: -1}"
            """,
            "stat": """
                #!/usr/bin/env bash
                if [[ "$*" == *"%u:%a"* ]]; then
                  printf '0:600\\n'
                elif [[ "$*" == *"%u %g %a %F"* && "${@: -1}" == *.sock ]]; then
                  printf '0 4242 660 socket\\n'
                else
                  printf '0 %s 710 directory\\n' "${BLUEPRINT_TEST_DATA_ROOT_GID:-0}"
                fi
            """,
            "getent": """
                #!/usr/bin/env bash
                [[ "$1" == "group" ]] || exit 2
                printf '%s:x:4242:\\n' "$2"
            """,
            "docker": """
                #!/usr/bin/env bash
                printf '%s\\n' "$*" >> "$BLUEPRINT_TEST_DOCKER_LOG"
                case "$*" in
                  *"info --format"*) printf '%s/docker-1\\n' "$BLUEPRINT_ACTIONS_STATE_ROOT" ;;
                  *"image ls"*) printf 'sha256:test-image\\n' ;;
                  *"volume ls"*) printf 'test-volume\\n' ;;
                  *" ps --quiet --no-trunc"*)
                    if [[ -n "${BLUEPRINT_TEST_CONTAINER_ID:-}" && ! -e "$BLUEPRINT_TEST_CONTAINER_REMOVED" ]]; then
                      printf '%s\\n' "$BLUEPRINT_TEST_CONTAINER_ID"
                    fi
                    ;;
                  *" ps --quiet"*) : ;;
                  *" inspect --format"*)
                    printf '%s|/stale-test-container|%s|%s|true|%s|false|false|postgres:15|4242\\n' \
                      "$BLUEPRINT_TEST_CONTAINER_ID" "$BLUEPRINT_TEST_CONTAINER_CREATED" \
                      "$BLUEPRINT_TEST_CONTAINER_STATUS" "$BLUEPRINT_TEST_CONTAINER_PAUSED"
                    ;;
                  *" rm --force --"*) touch "$BLUEPRINT_TEST_CONTAINER_REMOVED" ;;
                  *"volume prune --force"*) printf 'Total reclaimed space: 1GB\\n' ;;
                  *) exit 2 ;;
                esac
            """,
            "systemctl": """
                #!/usr/bin/env bash
                printf '%s\\n' "$*" >> "$BLUEPRINT_TEST_SYSTEMCTL_LOG"
                case "$1" in
                  is-active) printf '%s\\n' "$BLUEPRINT_TEST_RUNNER_STATE" ;;
                  show)
                    if [[ "$2" == blueprint-dockerd@* ]]; then
                      case "$*" in
                        *"LoadState"*) printf 'loaded\\n' ;;
                        *"ActiveState"*) printf 'active\\n' ;;
                        *"SubState"*) printf 'running\\n' ;;
                        *"MainPID"*) printf '4242\\n' ;;
                        *"FragmentPath"*) printf '/etc/systemd/system/blueprint-dockerd@.service\\n' ;;
                        *) exit 2 ;;
                      esac
                    elif [[ "$*" == *"FreezerState"* ]]; then
                      printf '%s\\n' "$BLUEPRINT_TEST_FREEZER_STATE"
                    elif [[ "$*" == *"ControlGroup"* ]]; then
                      if [[ "$BLUEPRINT_TEST_RUNNER_STATE" == "active" || -d "$BLUEPRINT_ACTIONS_CGROUP_ROOT/system.slice/system-blueprint\\x2dgithub\\x2dactions.slice/blueprint-github-actions@1.service" ]]; then
                        printf '/system.slice/system-blueprint\\x2dgithub\\x2dactions.slice/blueprint-github-actions@1.service\\n'
                      fi
                    elif [[ "$*" == *"ActiveState"* ]]; then
                      printf '%s\\n' "$BLUEPRINT_TEST_RUNNER_STATE"
                    elif [[ "$*" == *"SubState"* ]]; then
                      case "$BLUEPRINT_TEST_RUNNER_STATE" in
                        active) printf 'running\\n' ;;
                        inactive) printf 'dead\\n' ;;
                        failed) printf 'failed\\n' ;;
                      esac
                    elif [[ "$*" == *"MainPID"* ]]; then
                      if [[ "$BLUEPRINT_TEST_RUNNER_STATE" == "active" ]]; then printf '%s\\n' "$PPID"; else printf '0\\n'; fi
                    elif [[ "$*" == *"LoadState"* ]]; then
                      printf 'loaded\\n'
                    elif [[ "$*" == *"FragmentPath"* ]]; then
                      printf '/etc/systemd/system/blueprint-github-actions@.service\\n'
                    else
                      printf '/system.slice/system-blueprint\\x2dgithub\\x2dactions.slice/blueprint-github-actions@1.service\\n'
                    fi
                    ;;
                  freeze)
                    if [[ "${BLUEPRINT_TEST_KILL_ON_FREEZE:-0}" == "1" ]]; then
                      kill -KILL "$PPID"
                    fi
                    ;;
                  thaw) : ;;
                  *) exit 2 ;;
                esac
            """,
        }
        for name, body in commands.items():
            path = mock_bin / name
            path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
            path.chmod(0o755)

        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{mock_bin}:{environment['PATH']}",
                "BLUEPRINT_ACTIONS_STATE_ROOT": str(state_root),
                "BLUEPRINT_ACTIONS_SOCKET_ROOT": str(socket_root),
                "BLUEPRINT_ACTIONS_CGROUP_ROOT": str(cgroup_root),
                "BLUEPRINT_ACTIONS_GC_LOCK_FILE": str(root / "gc.lock"),
                "BLUEPRINT_ACTIONS_GC_OWNERSHIP_ROOT": str(ownership_root),
                "BLUEPRINT_TEST_SYSTEMCTL_LOG": str(log_path),
                "BLUEPRINT_TEST_FREEZER_STATE": freezer_state,
                "BLUEPRINT_TEST_RUNNER_STATE": runner_state,
                "BLUEPRINT_TEST_DOCKER_LOG": str(root / "docker.log"),
                "BLUEPRINT_TEST_CONTAINER_REMOVED": str(root / "container-removed"),
                "BLUEPRINT_TEST_CONTAINER_STATUS": "running",
                "BLUEPRINT_TEST_CONTAINER_PAUSED": "false",
            }
        )
        return environment, socket_root / "1" / "docker.sock", log_path

    @unittest.skipIf(os.name == "nt", "requires a Unix socket")
    def test_volume_gc_rejects_group_owned_steady_state_data_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment, socket_path, _log_path = self._volume_gc_harness(
                temporary, freezer_state="running"
            )
            environment["BLUEPRINT_TEST_DATA_ROOT_GID"] = "4242"
            listener = socket.socket(socket.AF_UNIX)
            try:
                listener.bind(str(socket_path))
                result = subprocess.run(
                    ["bash", str(ASSET_DIR / "prune-runner-volumes.sh")],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
            finally:
                listener.close()

            self.assertEqual(result.returncode, 1)
            self.assertIn("reason=ambiguous_docker_boundary", result.stdout)
            docker_calls = Path(environment["BLUEPRINT_TEST_DOCKER_LOG"])
            self.assertFalse(docker_calls.exists())

    @unittest.skipIf(os.name == "nt", "requires POSIX signals and Unix sockets")
    def test_volume_gc_hard_kill_after_freeze_is_recovered_by_stop_post(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment, socket_path, log_path = self._volume_gc_harness(
                temporary, freezer_state="running"
            )
            listener = socket.socket(socket.AF_UNIX)
            try:
                listener.bind(str(socket_path))
                environment["BLUEPRINT_TEST_KILL_ON_FREEZE"] = "1"
                result = subprocess.run(
                    ["bash", str(ASSET_DIR / "prune-runner-volumes.sh")],
                    env=environment,
                    check=False,
                    timeout=10,
                )
            finally:
                listener.close()

            self.assertEqual(result.returncode, -9)
            marker = Path(environment["BLUEPRINT_ACTIONS_GC_OWNERSHIP_ROOT"]) / "frozen-1"
            self.assertEqual(
                marker.read_text(encoding="utf-8").strip(),
                "blueprint-github-actions@1.service",
            )
            environment.pop("BLUEPRINT_TEST_KILL_ON_FREEZE")
            subprocess.run(
                ["bash", str(ASSET_DIR / "thaw-owned-runners.sh")],
                env=environment,
                check=True,
                timeout=10,
            )
            self.assertFalse(marker.exists())
            calls = log_path.read_text(encoding="utf-8")
            self.assertIn("freeze blueprint-github-actions@1.service", calls)
            self.assertIn("thaw blueprint-github-actions@1.service", calls)

    @unittest.skipIf(os.name == "nt", "requires a Unix socket")
    def test_volume_gc_leaves_operator_prefrozen_runner_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment, socket_path, log_path = self._volume_gc_harness(
                temporary, freezer_state="frozen"
            )
            listener = socket.socket(socket.AF_UNIX)
            try:
                listener.bind(str(socket_path))
                result = subprocess.run(
                    ["bash", str(ASSET_DIR / "prune-runner-volumes.sh")],
                    env=environment,
                    check=False,
                    timeout=10,
                )
            finally:
                listener.close()
            self.assertEqual(result.returncode, 1)
            calls = log_path.read_text(encoding="utf-8")
            self.assertNotIn("freeze blueprint-github-actions@1.service", calls)
            self.assertNotIn("thaw blueprint-github-actions@1.service", calls)
            self.assertFalse(
                (Path(environment["BLUEPRINT_ACTIONS_GC_OWNERSHIP_ROOT"]) / "frozen-1").exists()
            )

    @unittest.skipIf(os.name == "nt", "requires a Unix socket")
    def test_volume_gc_recovers_old_container_from_failed_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment, socket_path, log_path = self._volume_gc_harness(
                temporary,
                freezer_state="dead",
                runner_state="failed",
            )
            container_id = "a" * 64
            environment.update(
                {
                    "BLUEPRINT_ACTIONS_GC_NOW_EPOCH": "2000000000",
                    "BLUEPRINT_TEST_CONTAINER_ID": container_id,
                    "BLUEPRINT_TEST_CONTAINER_CREATED": "2033-05-18T00:46:40Z",
                }
            )
            listener = socket.socket(socket.AF_UNIX)
            try:
                listener.bind(str(socket_path))
                result = subprocess.run(
                    ["bash", str(ASSET_DIR / "prune-runner-volumes.sh")],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=15,
                )
            finally:
                listener.close()

            self.assertEqual(result.returncode, 1)
            self.assertIn(
                f"event=orphan_reaped slot=1 container={container_id}",
                result.stdout,
            )
            docker_calls = Path(environment["BLUEPRINT_TEST_DOCKER_LOG"]).read_text(
                encoding="utf-8"
            )
            self.assertIn(f"rm --force -- {container_id}", docker_calls)
            self.assertIn("volume prune --force", docker_calls)
            systemctl_calls = log_path.read_text(encoding="utf-8")
            self.assertNotIn("freeze blueprint-github-actions@1.service", systemctl_calls)

    @unittest.skipIf(os.name == "nt", "requires a Unix socket")
    def test_volume_gc_refuses_recent_container_from_failed_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment, socket_path, _log_path = self._volume_gc_harness(
                temporary,
                freezer_state="dead",
                runner_state="failed",
            )
            container_id = "b" * 64
            environment.update(
                {
                    "BLUEPRINT_ACTIONS_GC_NOW_EPOCH": "2000000000",
                    "BLUEPRINT_TEST_CONTAINER_ID": container_id,
                    "BLUEPRINT_TEST_CONTAINER_CREATED": "2033-05-18T03:16:40Z",
                }
            )
            listener = socket.socket(socket.AF_UNIX)
            try:
                listener.bind(str(socket_path))
                result = subprocess.run(
                    ["bash", str(ASSET_DIR / "prune-runner-volumes.sh")],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=15,
                )
            finally:
                listener.close()

            self.assertEqual(result.returncode, 1)
            self.assertIn("reason=unclassified_or_changing_containers", result.stdout)
            docker_calls = Path(environment["BLUEPRINT_TEST_DOCKER_LOG"]).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("rm --force", docker_calls)
            self.assertNotIn("volume prune --force", docker_calls)

    @unittest.skipIf(os.name == "nt", "requires a Unix socket")
    def test_volume_gc_refuses_old_paused_container_from_failed_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment, socket_path, _log_path = self._volume_gc_harness(
                temporary,
                freezer_state="dead",
                runner_state="failed",
            )
            environment.update(
                {
                    "BLUEPRINT_ACTIONS_GC_NOW_EPOCH": "2000000000",
                    "BLUEPRINT_TEST_CONTAINER_ID": "d" * 64,
                    "BLUEPRINT_TEST_CONTAINER_CREATED": "2033-05-18T00:46:40Z",
                    "BLUEPRINT_TEST_CONTAINER_STATUS": "paused",
                    "BLUEPRINT_TEST_CONTAINER_PAUSED": "true",
                }
            )
            listener = socket.socket(socket.AF_UNIX)
            try:
                listener.bind(str(socket_path))
                result = subprocess.run(
                    ["bash", str(ASSET_DIR / "prune-runner-volumes.sh")],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=15,
                )
            finally:
                listener.close()

            self.assertEqual(result.returncode, 1)
            self.assertIn("reason=unclassified_or_changing_containers", result.stdout)
            docker_calls = Path(environment["BLUEPRINT_TEST_DOCKER_LOG"]).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("rm --force", docker_calls)
            self.assertNotIn("volume prune --force", docker_calls)

    @unittest.skipIf(os.name == "nt", "requires a Unix socket")
    def test_volume_gc_refuses_failed_runner_with_residual_cgroup_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment, socket_path, _log_path = self._volume_gc_harness(
                temporary,
                freezer_state="dead",
                runner_state="failed",
                inactive_cgroup_pid=os.getpid(),
            )
            environment.update(
                {
                    "BLUEPRINT_ACTIONS_GC_NOW_EPOCH": "2000000000",
                    "BLUEPRINT_TEST_CONTAINER_ID": "c" * 64,
                    "BLUEPRINT_TEST_CONTAINER_CREATED": "2033-05-18T00:46:40Z",
                }
            )
            listener = socket.socket(socket.AF_UNIX)
            try:
                listener.bind(str(socket_path))
                result = subprocess.run(
                    ["bash", str(ASSET_DIR / "prune-runner-volumes.sh")],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=15,
                )
            finally:
                listener.close()

            self.assertEqual(result.returncode, 1)
            self.assertIn("reason=ambiguous_inactive_runner", result.stdout)
            docker_calls = Path(environment["BLUEPRINT_TEST_DOCKER_LOG"]).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("rm --force", docker_calls)
            self.assertNotIn("volume prune --force", docker_calls)


if __name__ == "__main__":
    unittest.main()

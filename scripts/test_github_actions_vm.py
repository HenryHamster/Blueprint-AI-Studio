"""Focused security and rendering tests for the Actions KVM boundary."""

from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import github_actions_vm as gav


ASSET_DIR = Path(__file__).parent / "github-actions-vm"


class PlanValidationTests(unittest.TestCase):
    def test_plan_has_pinned_sources_and_expected_capacity(self):
        plan = gav.build_plan(state_root="/home/runner/.blueprint/actions-vm")
        self.assertEqual(plan.runner_count, 4)
        self.assertEqual(plan.cpus, 12)
        self.assertEqual(plan.memory_mib, 32768)
        self.assertRegex(plan.cloud_image_sha256, r"^[0-9a-f]{64}$")
        self.assertIn("release-20260725", plan.cloud_image_url)
        self.assertIn("@sha256:", gav.HOST_CONTAINER_BASE)

    def test_plan_rejects_unsafe_paths_and_resource_values(self):
        for path in ("relative", "/tmp/../root", "/tmp/has space"):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(ValueError, "state_root"),
            ):
                gav.build_plan(state_root=path)
        with self.assertRaisesRegex(ValueError, "cpus"):
            gav.build_plan(state_root="/safe", cpus=1)
        with self.assertRaisesRegex(ValueError, "runner_count"):
            gav.build_plan(state_root="/safe", runner_count=17)
        with self.assertRaisesRegex(ValueError, "port"):
            gav.build_plan(state_root="/safe", ssh_host_port=22)

    def test_ssh_key_validation_rejects_yaml_and_shell_injection(self):
        valid = "ssh-ed25519 " + ("A" * 64) + " operator@example"
        self.assertEqual(gav.validate_ssh_public_key(valid), valid)
        for key in (
            valid + "\nssh-ed25519 AAAA attacker",
            "ssh-rsa AAAA old",
            "ssh-ed25519 AAAA $(touch /tmp/pwn)",
            "ssh-ed25519 AAAA comment:bad",
        ):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(ValueError, "SSH public key"),
            ):
                gav.validate_ssh_public_key(key)


class RenderTests(unittest.TestCase):
    def test_cloud_init_embeds_real_assets_without_interpreting_them(self):
        plan = gav.build_plan(state_root="/home/runner/.blueprint/actions-vm")
        key = "ssh-ed25519 " + ("A" * 64) + " operator@example"
        rendered = gav.render_cloud_init(plan, key)
        self.assertTrue(rendered.startswith("#cloud-config\n"))
        self.assertIn('"ssh-ed25519 ', rendered)
        self.assertIn("encoding: b64", rendered)
        self.assertNotIn("registration_token", rendered.lower())
        self.assertNotIn("GITHUB_TOKEN", rendered)
        self.assertNotIn("$(touch", rendered)
        for name in (
            "bootstrap-guest.sh",
            "install-runners.sh",
            "prepare-runner-network.sh",
            "blueprint-runner-network@.service",
            "blueprint-dockerd@.service",
            "blueprint-github-actions@.service",
            "prune-runner-volumes.sh",
            "thaw-owned-runners.sh",
            "blueprint-actions-volume-gc.service",
            "blueprint-actions-volume-gc.timer",
            "install-volume-maintenance.sh",
        ):
            self.assertIn(name, rendered)

    @unittest.skipIf(os.name == "nt", "POSIX file modes are not enforced on Windows")
    def test_atomic_render_is_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "user-data"
            gav.atomic_write_private(path, "secret\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class AssetSecurityTests(unittest.TestCase):
    def test_actions_vm_deployment_assets_use_lf(self):
        deployment_assets = [
            Path(__file__).parent / "install-github-actions-vm.sh",
            Path(__file__).parent / "blueprint-actions-vm.service",
            *ASSET_DIR.glob("*.sh"),
            *ASSET_DIR.glob("*.service"),
            *ASSET_DIR.glob("*.timer"),
        ]
        self.assertTrue(deployment_assets)
        for asset in deployment_assets:
            self.assertNotIn(b"\r", asset.read_bytes(), asset)

    def test_host_control_is_persisted_but_not_started_by_install(self):
        scripts = Path(__file__).parent
        installer = (scripts / "install-github-actions-vm.sh").read_text(
            encoding="utf-8"
        )
        unit = (scripts / "blueprint-actions-vm.service").read_text(encoding="utf-8")

        install_control = installer.split("install_control() {", 1)[1].split(
            "guest_status() {", 1
        )[0]
        self.assertIn("verify_prestart", install_control)
        self.assertIn("validate_ssh_key_files", install_control)
        self.assertIn("chmod 0600", install_control)
        self.assertIn("systemctl --user daemon-reload", install_control)
        self.assertIn(
            "systemctl --user enable blueprint-actions-vm.service", install_control
        )
        self.assertNotRegex(
            install_control,
            r"(?m)^\s*systemctl --user (?:start|restart|enable --now)",
        )

        self.assertIn(
            "EnvironmentFile=%h/.blueprint/github-actions-vm-control.env", unit
        )
        self.assertIn("${BLUEPRINT_ACTIONS_VM_LAUNCHER} recover", unit)
        self.assertIn("${BLUEPRINT_ACTIONS_VM_LAUNCHER} stop", unit)
        self.assertIn("RemainAfterExit=yes", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("WantedBy=default.target", unit)

    @staticmethod
    def _host_control_harness(
        temporary: str, *, firewall_status: str, qemu_status: str
    ) -> tuple[dict[str, str], Path, Path, Path]:
        root = Path(temporary)
        state_root = root / "canonical-state"
        mock_bin = root / "bin"
        docker_state = root / "docker-state"
        docker_log = root / "docker.log"
        systemctl_log = root / "systemctl.log"
        for path in (state_root, mock_bin, docker_state, root / ".ssh"):
            path.mkdir(parents=True, exist_ok=True)
        state_root.chmod(0o700)

        for name in (
            "overlay.qcow2",
            "seed.img",
            "ubuntu-24.04-amd64.img",
            "known_hosts",
        ):
            path = state_root / name
            path.write_bytes(b"test\n")
            path.chmod(0o600)
        (state_root / "host-image-id").write_text("sha256:test\n", encoding="utf-8")
        (state_root / "host-image-id").chmod(0o600)

        public_key = root / ".ssh" / "blueprint-actions-vm.pub"
        private_key = root / ".ssh" / "blueprint-actions-vm"
        public_key.write_text("ssh-ed25519 AAAA test\n", encoding="utf-8")
        private_key.write_text("test-private-key\n", encoding="utf-8")
        public_key.chmod(0o644)
        private_key.chmod(0o600)

        (docker_state / "blueprint-actions-vm-net").write_text(
            firewall_status + "\n", encoding="utf-8"
        )
        (docker_state / "blueprint-actions-vm").write_text(
            qemu_status + "\n", encoding="utf-8"
        )
        docker = mock_bin / "docker"
        docker.write_text(
            textwrap.dedent(
                """
                #!/usr/bin/env bash
                set -euo pipefail
                printf '%s\n' "$*" >> "$BLUEPRINT_TEST_DOCKER_LOG"
                if [[ "${BLUEPRINT_TEST_DOCKER_UNAVAILABLE:-0}" == 1 ]] \
                    && { [[ "${1:-}" == inspect ]] \
                      || [[ "${1:-} ${2:-}" == "container ls" ]]; }; then
                  exit 125
                fi
                state_of() {
                  local path="$BLUEPRINT_TEST_DOCKER_STATE/$1"
                  if [[ -f "$path" ]]; then cat "$path"; else printf 'absent\n'; fi
                }
                set_state() {
                  printf '%s\n' "$2" > "$BLUEPRINT_TEST_DOCKER_STATE/$1"
                }
                case "${1:-}" in
                  image)
                    [[ "${2:-}" == inspect ]] || exit 2
                    printf 'sha256:test\n'
                    ;;
                  container)
                    case "${2:-}" in
                      inspect)
                        name="${@: -1}"
                        [[ "$(state_of "$name")" != absent ]]
                        ;;
                      ls)
                        for path in "$BLUEPRINT_TEST_DOCKER_STATE"/*; do
                          [[ -f "$path" ]] || continue
                          name="$(basename -- "$path")"
                          [[ "$(state_of "$name")" == absent ]] || printf '%s\n' "$name"
                        done
                        ;;
                      *) exit 2 ;;
                    esac
                    ;;
                  inspect)
                    name="${@: -1}"
                    state="$(state_of "$name")"
                    [[ "$state" != absent ]] || exit 1
                    if [[ "$*" == *'.State.Running'* ]]; then
                      [[ "$state" == running ]] && printf 'true\n' || printf 'false\n'
                    else
                      printf '%s\n' "$state"
                    fi
                    ;;
                  run)
                    name=''
                    previous=''
                    for argument in "$@"; do
                      if [[ "$previous" == --name ]]; then name="$argument"; break; fi
                      previous="$argument"
                    done
                    [[ -n "$name" ]] || exit 2
                    set_state "$name" running
                    printf '%s-id\n' "$name"
                    ;;
                  exec)
                    exit 0
                    ;;
                  stop)
                    name="${@: -1}"
                    set_state "$name" exited
                    printf '%s\n' "$name"
                    ;;
                  rm)
                    name="${@: -1}"
                    set_state "$name" absent
                    printf '%s\n' "$name"
                    ;;
                  *) exit 2 ;;
                esac
                """
            ).lstrip(),
            encoding="utf-8",
        )
        docker.chmod(0o755)

        systemctl = mock_bin / "systemctl"
        systemctl.write_text(
            '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$BLUEPRINT_TEST_SYSTEMCTL_LOG"\n',
            encoding="utf-8",
        )
        systemctl.chmod(0o755)
        loginctl = mock_bin / "loginctl"
        loginctl.write_text(
            textwrap.dedent(
                """
                #!/usr/bin/env bash
                [[ "${1:-}" == show-user ]] || exit 2
                [[ "${BLUEPRINT_TEST_LINGER_STATE:-yes}" != unavailable ]] || exit 1
                printf '%s\n' "${BLUEPRINT_TEST_LINGER_STATE:-yes}"
                """
            ).lstrip(),
            encoding="utf-8",
        )
        loginctl.chmod(0o755)

        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(root),
                "PATH": f"{mock_bin}:{environment['PATH']}",
                "BLUEPRINT_ACTIONS_VM_STATE": str(state_root),
                "BLUEPRINT_ACTIONS_VM_SSH_KEY": str(public_key),
                "BLUEPRINT_ACTIONS_VM_SSH_PRIVATE_KEY": str(private_key),
                "BLUEPRINT_ACTIONS_VM_KVM_DEVICE": "/dev/null",
                "BLUEPRINT_TEST_DOCKER_STATE": str(docker_state),
                "BLUEPRINT_TEST_DOCKER_LOG": str(docker_log),
                "BLUEPRINT_TEST_SYSTEMCTL_LOG": str(systemctl_log),
            }
        )
        return environment, docker_log, systemctl_log, state_root

    @unittest.skipIf(os.name == "nt", "requires POSIX file modes and Bash")
    def test_recover_reuses_running_pair_and_recreates_only_stopped_guest(self):
        installer = Path(__file__).parent / "install-github-actions-vm.sh"
        with tempfile.TemporaryDirectory() as temporary:
            environment, docker_log, _systemctl_log, state_root = (
                self._host_control_harness(
                    temporary, firewall_status="running", qemu_status="running"
                )
            )
            result = subprocess.run(
                ["bash", str(installer), "recover"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already running", result.stdout)
            mutations = [
                line
                for line in docker_log.read_text(encoding="utf-8").splitlines()
                if line.startswith(("run ", "stop ", "rm "))
            ]
            self.assertEqual(mutations, [])

        with tempfile.TemporaryDirectory() as temporary:
            environment, docker_log, _systemctl_log, state_root = (
                self._host_control_harness(
                    temporary, firewall_status="running", qemu_status="exited"
                )
            )
            result = subprocess.run(
                ["bash", str(installer), "recover"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = docker_log.read_text(encoding="utf-8")
            self.assertIn("rm blueprint-actions-vm", calls)
            self.assertIn("stop --timeout 10 blueprint-actions-vm-net", calls)
            self.assertIn("rm blueprint-actions-vm-net", calls)
            self.assertIn(f"src={state_root / 'overlay.qcow2'}", calls)
            self.assertIn(f"src={state_root / 'seed.img'}", calls)

    @unittest.skipIf(os.name == "nt", "requires POSIX file modes and Bash")
    def test_recover_and_stop_fail_closed_on_ambiguous_container_state(self):
        installer = Path(__file__).parent / "install-github-actions-vm.sh"
        for command, firewall_status, qemu_status in (
            ("recover", "exited", "running"),
            ("recover", "running", "paused"),
            ("stop", "running", "restarting"),
            ("stop", "paused", "running"),
        ):
            with tempfile.TemporaryDirectory() as temporary:
                environment, docker_log, _systemctl_log, _state_root = (
                    self._host_control_harness(
                        temporary,
                        firewall_status=firewall_status,
                        qemu_status=qemu_status,
                    )
                )
                result = subprocess.run(
                    ["bash", str(installer), command],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("refusing", result.stderr)
                calls = docker_log.read_text(encoding="utf-8")
                self.assertNotRegex(calls, r"(?m)^(run|stop|rm) ")

    @unittest.skipIf(os.name == "nt", "requires POSIX file modes and Bash")
    def test_recover_and_stop_fail_closed_when_docker_state_probe_is_unavailable(self):
        installer = Path(__file__).parent / "install-github-actions-vm.sh"
        for command in ("recover", "stop"):
            with tempfile.TemporaryDirectory() as temporary:
                environment, docker_log, _systemctl_log, _state_root = (
                    self._host_control_harness(
                        temporary, firewall_status="running", qemu_status="running"
                    )
                )
                environment["BLUEPRINT_TEST_DOCKER_UNAVAILABLE"] = "1"
                result = subprocess.run(
                    ["bash", str(installer), command],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unable to list containers", result.stderr)
                calls = docker_log.read_text(encoding="utf-8")
                self.assertNotRegex(calls, r"(?m)^(run|stop|rm) ")

    @unittest.skipIf(os.name == "nt", "requires POSIX file modes and Bash")
    def test_install_control_persists_exact_profile_without_starting(self):
        installer = Path(__file__).parent / "install-github-actions-vm.sh"
        with tempfile.TemporaryDirectory() as temporary:
            environment, docker_log, systemctl_log, state_root = (
                self._host_control_harness(
                    temporary, firewall_status="absent", qemu_status="absent"
                )
            )
            environment["BLUEPRINT_TEST_LINGER_STATE"] = "yes"
            result = subprocess.run(
                ["bash", str(installer), "install-control"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            control_env = (
                Path(temporary) / ".blueprint" / "github-actions-vm-control.env"
            )
            control_unit = (
                Path(temporary)
                / ".config"
                / "systemd"
                / "user"
                / "blueprint-actions-vm.service"
            )
            self.assertEqual(stat.S_IMODE(control_env.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(control_unit.stat().st_mode), 0o644)
            profile = control_env.read_text(encoding="utf-8")
            self.assertIn(f"BLUEPRINT_ACTIONS_VM_STATE={state_root}\n", profile)
            self.assertIn(
                f"BLUEPRINT_ACTIONS_VM_SSH_PRIVATE_KEY={temporary}/.ssh/blueprint-actions-vm\n",
                profile,
            )
            systemctl_calls = systemctl_log.read_text(encoding="utf-8")
            self.assertIn("--user daemon-reload", systemctl_calls)
            self.assertIn("--user enable blueprint-actions-vm.service", systemctl_calls)
            self.assertNotIn("--now", systemctl_calls)
            self.assertNotIn(" start ", f" {systemctl_calls} ")
            docker_calls = docker_log.read_text(encoding="utf-8")
            self.assertNotRegex(docker_calls, r"(?m)^(run|stop|rm) ")

    @unittest.skipIf(os.name == "nt", "requires POSIX file modes and Bash")
    def test_install_control_accepts_numeric_uid_without_passwd_entry(self):
        installer = Path(__file__).parent / "install-github-actions-vm.sh"
        with tempfile.TemporaryDirectory() as temporary:
            environment, docker_log, _systemctl_log, _state_root = (
                self._host_control_harness(
                    temporary, firewall_status="absent", qemu_status="absent"
                )
            )
            numeric_id = Path(temporary) / "bin" / "id"
            numeric_id.write_text(
                textwrap.dedent(
                    """
                    #!/usr/bin/env bash
                    case "${1:-}" in
                      -u) printf '995\n' ;;
                      -un) exit 1 ;;
                      *) exec /usr/bin/id "$@" ;;
                    esac
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            numeric_id.chmod(0o755)
            environment["BLUEPRINT_TEST_LINGER_STATE"] = "yes"

            result = subprocess.run(
                ["bash", str(installer), "install-control"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            docker_calls = docker_log.read_text(encoding="utf-8")
            self.assertNotRegex(docker_calls, r"(?m)^(run|stop|rm) ")

    @unittest.skipIf(os.name == "nt", "requires POSIX file modes and Bash")
    def test_install_control_requires_verified_user_lingering(self):
        installer = Path(__file__).parent / "install-github-actions-vm.sh"
        for linger_state, expected_error in (
            ("no", "systemd user lingering is no"),
            ("unavailable", "unable to verify systemd user lingering"),
        ):
            with tempfile.TemporaryDirectory() as temporary:
                environment, docker_log, systemctl_log, _state_root = (
                    self._host_control_harness(
                        temporary, firewall_status="absent", qemu_status="absent"
                    )
                )
                environment["BLUEPRINT_TEST_LINGER_STATE"] = linger_state
                result = subprocess.run(
                    ["bash", str(installer), "install-control"],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)
                self.assertIn("sudo loginctl enable-linger", result.stderr)
                self.assertFalse(systemctl_log.exists())
                docker_calls = docker_log.read_text(encoding="utf-8")
                self.assertNotRegex(docker_calls, r"(?m)^(run|stop|rm) ")

    @unittest.skipIf(os.name == "nt", "requires POSIX symlink semantics and Bash")
    def test_install_control_rejects_symlinked_profile_destination(self):
        installer = Path(__file__).parent / "install-github-actions-vm.sh"
        with tempfile.TemporaryDirectory() as temporary:
            environment, docker_log, systemctl_log, _state_root = (
                self._host_control_harness(
                    temporary, firewall_status="absent", qemu_status="absent"
                )
            )
            profile_dir = Path(temporary) / ".blueprint"
            profile_dir.mkdir(exist_ok=True)
            external = Path(temporary) / "external.env"
            external.write_text("unchanged\n", encoding="utf-8")
            (profile_dir / "github-actions-vm-control.env").symlink_to(external)

            result = subprocess.run(
                ["bash", str(installer), "install-control"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing symlinked control environment file", result.stderr)
            self.assertEqual(external.read_text(encoding="utf-8"), "unchanged\n")
            self.assertFalse(systemctl_log.exists())
            docker_calls = docker_log.read_text(encoding="utf-8")
            self.assertNotRegex(docker_calls, r"(?m)^(run|stop|rm) ")

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
                  printf '0 4242 710 directory\\n'
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

    def test_host_image_and_cloud_image_are_immutable_inputs(self):
        dockerfile = (ASSET_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(f"FROM {gav.HOST_CONTAINER_BASE}", dockerfile)
        self.assertNotRegex(dockerfile, r"(?m)^FROM [^@\n]+$")
        self.assertIn("s|http://|https://|g", dockerfile)

    def test_cloud_seed_generation_is_read_only_with_bounded_scratch_space(self):
        installer = (Path(__file__).parent / "install-github-actions-vm.sh").read_text(
            encoding="utf-8"
        )
        render_seed = installer.split("render_seed() {", 1)[1].split(
            "create_overlay() {", 1
        )[0]
        self.assertIn("--read-only", render_seed)
        self.assertIn("--tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m", render_seed)
        self.assertIn("--entrypoint /usr/bin/cloud-localds", render_seed)

    def test_firewall_startup_uses_bounded_readiness_poll(self):
        installer = (Path(__file__).parent / "install-github-actions-vm.sh").read_text(
            encoding="utf-8"
        )
        start = installer.split("start() {", 1)[1].split("status() {", 1)[0]
        self.assertIn("attempt <= 50", start)
        self.assertIn("sleep 0.1", start)
        self.assertIn("firewall_ready=true", start)
        self.assertIn('[[ "$firewall_ready" != "true" ]]', start)

    def test_qemu_safely_shares_the_firewall_network_namespace(self):
        installer = (Path(__file__).parent / "install-github-actions-vm.sh").read_text(
            encoding="utf-8"
        )
        start = installer.split("start() {", 1)[1].split("status() {", 1)[0]
        self.assertIn('--network "container:$FIREWALL_CONTAINER"', start)
        self.assertNotIn("--hostname blueprint-actions-vm", start)
        self.assertNotIn("--publish", start)

    def test_guest_ssh_uses_a_bounded_ephemeral_namespace_client(self):
        installer = (Path(__file__).parent / "install-github-actions-vm.sh").read_text(
            encoding="utf-8"
        )
        guest_ssh = installer.split("guest_ssh() {", 1)[1].split(
            "ensure_state_root() {", 1
        )[0]
        for hardening in (
            '--network "container:$FIREWALL_CONTAINER"',
            "--cap-drop ALL",
            "--security-opt no-new-privileges",
            "--read-only",
            "--pids-limit 64",
            "--memory 128m",
            "--cpus 0.25",
        ):
            self.assertIn(hardening, guest_ssh)
        self.assertIn("dst=/run/ssh_key,readonly", guest_ssh)
        self.assertIn("dst=/run/known_hosts", guest_ssh)
        self.assertNotIn("BLUEPRINT_ACTIONS_VM_STATE:/", guest_ssh)
        self.assertIn("--entrypoint /usr/bin/ssh", guest_ssh)
        self.assertIn("StrictHostKeyChecking=accept-new", guest_ssh)

    def test_qemu_has_no_host_mounts_or_management_socket(self):
        qemu = (ASSET_DIR / "qemu-entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn("-machine q35,accel=kvm", qemu)
        self.assertIn("-monitor none", qemu)
        self.assertNotIn("/var/run/docker.sock", qemu)
        self.assertNotIn("-qmp", qemu)
        self.assertIn("hostfwd=tcp:127.0.0.1:", qemu)
        self.assertIn("virtio-blk-pci,drive=osdisk,bootindex=1", qemu)
        self.assertIn("virtio-blk-pci,drive=seed", qemu)

    def test_firewall_denies_host_lan_metadata_and_ipv6(self):
        firewall = (ASSET_DIR / "firewall-entrypoint.sh").read_text(encoding="utf-8")
        for network in (
            "10.0.0.0/8",
            "100.64.0.0/10",
            "169.254.0.0/16",
            "172.16.0.0/12",
            "192.168.0.0/16",
        ):
            self.assertIn(network, firewall)
        self.assertIn("ip6tables -P OUTPUT DROP", firewall)
        self.assertIn("iptables -P INPUT DROP", firewall)
        self.assertNotIn("--dport", firewall)

    def test_host_image_contains_only_the_required_ssh_client(self):
        dockerfile = (ASSET_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("openssh-client", dockerfile)
        self.assertNotIn("openssh-server", dockerfile)

    def test_guest_ssh_disables_every_forwarding_mode(self):
        bootstrap = (ASSET_DIR / "bootstrap-guest.sh").read_text(encoding="utf-8")
        self.assertIn("DisableForwarding yes", bootstrap)
        self.assertIn("PermitTunnel no", bootstrap)

    def test_shared_container_runtime_is_masked_and_stopped(self):
        bootstrap = (ASSET_DIR / "bootstrap-guest.sh").read_text(encoding="utf-8")
        self.assertIn(
            "systemctl mask --now docker.service docker.socket containerd.service",
            bootstrap,
        )

    def test_each_runner_gets_a_dedicated_guest_docker_daemon(self):
        dockerd = (ASSET_DIR / "blueprint-dockerd@.service").read_text(encoding="utf-8")
        runner = (ASSET_DIR / "blueprint-github-actions@.service").read_text(
            encoding="utf-8"
        )
        network = (ASSET_DIR / "prepare-runner-network.sh").read_text(encoding="utf-8")
        self.assertIn("/run/blueprint-actions/%i/docker.sock", dockerd)
        self.assertIn("--data-root=/var/lib/blueprint-actions/docker-%i", dockerd)
        self.assertIn("--bip=172.31.%i.1/24", dockerd)
        self.assertIn("NetworkNamespacePath=/run/netns/blueprint-actions-%i", dockerd)
        self.assertIn("NetworkNamespacePath=/run/netns/blueprint-actions-%i", runner)
        self.assertIn("172.29.${index}.2/30", network)
        self.assertIn("iptables -P FORWARD DROP", network)
        self.assertIn('-C INPUT -i "$host_veth" -j REJECT', network)
        self.assertIn("net.ipv6.conf.all.disable_ipv6=1", network)
        self.assertIn("blueprint-dockerd@%i.service", runner)
        self.assertIn("blueprint-runner-network@%i.service", runner)
        self.assertIn(
            "DOCKER_HOST=unix:///run/blueprint-actions/%i/docker.sock", runner
        )

    def test_runner_registration_preflights_all_docker_daemons(self):
        installer = (ASSET_DIR / "install-runners.sh").read_text(encoding="utf-8")
        daemon_preflight = installer.index('systemctl enable --now "blueprint-dockerd@')
        runner_registration = installer.index('"$runner_root/config.sh"')
        self.assertLess(daemon_preflight, runner_registration)
        self.assertIn("info >/dev/null 2>&1", installer)
        self.assertIn('[[ "$daemon_ready" == "true" ]]', installer)
        self.assertIn('if [[ -f "$runner_root/.runner"', installer)
        self.assertIn('["agentName"]', installer)

    def test_volume_gc_is_pressure_quiescence_and_scope_guarded(self):
        cleanup = (ASSET_DIR / "prune-runner-volumes.sh").read_text(encoding="utf-8")
        self.assertIn('PRESSURE_PERCENT=70', cleanup)
        self.assertIn('MIN_FREE_BYTES=$((30 * 1024 * 1024 * 1024))', cleanup)
        self.assertIn('for slot in 1 2 3 4', cleanup)
        self.assertIn('systemctl freeze "$unit"', cleanup)
        self.assertIn('systemctl thaw "$frozen_unit"', cleanup)
        self.assertIn("trap 'terminate 143' TERM", cleanup)
        self.assertIn('exit "$status"', cleanup)
        self.assertIn('--property=FreezerState', cleanup)
        self.assertIn('reason=runner_pre_frozen', cleanup)
        self.assertIn('system-blueprint\\\\x2dgithub\\\\x2dactions.slice', cleanup)
        self.assertIn('"$comm" != "Runner.Worker"', cleanup)
        self.assertIn('sleep 2', cleanup)
        self.assertIn('docker --host "unix://$SOCKET_ROOT/$slot/docker.sock"', cleanup)
        self.assertIn('actual_root="$(docker_for_slot', cleanup)
        self.assertIn('ORPHAN_MIN_AGE_SECONDS=', cleanup)
        self.assertIn('inactive_runner_is_quiescent', cleanup)
        self.assertIn('docker_boundary_is_exact', cleanup)
        self.assertIn('ps --quiet --no-trunc', cleanup)
        self.assertIn('rm --force -- "$id"', cleanup)
        self.assertIn('volume prune --force', cleanup)
        self.assertIn('images_before=', cleanup)
        self.assertIn('images_after=', cleanup)
        self.assertIn('timeout --signal=TERM --kill-after=5s 30s', cleanup)
        for excluded in (
            'volume prune --all',
            'volume rm',
            'image prune',
            'system prune',
            'container prune',
            'network prune',
            '/run/docker.sock',
        ):
            self.assertNotIn(excluded, cleanup)

    def test_volume_gc_timer_is_hardened_and_persistent(self):
        service = (ASSET_DIR / "blueprint-actions-volume-gc.service").read_text(
            encoding="utf-8"
        )
        timer = (ASSET_DIR / "blueprint-actions-volume-gc.timer").read_text(
            encoding="utf-8"
        )
        self.assertIn('NoNewPrivileges=yes', service)
        self.assertIn('ProtectSystem=strict', service)
        self.assertIn('CapabilityBoundingSet=', service)
        self.assertIn('RuntimeDirectory=blueprint-actions-volume-gc', service)
        self.assertIn('RuntimeDirectoryMode=0700', service)
        self.assertIn('RuntimeDirectoryPreserve=yes', service)
        self.assertNotIn('RuntimeMaxSec=', service)
        self.assertIn(
            'ExecStopPost=/usr/local/lib/blueprint/github-actions-vm/thaw-owned-runners.sh',
            service,
        )
        self.assertIn('OnUnitActiveSec=15min', timer)
        self.assertIn('RandomizedDelaySec=2min', timer)
        self.assertNotIn('Persistent=true', timer)

    def test_existing_vm_has_reviewable_maintenance_install_path(self):
        host_installer = (
            Path(__file__).parent / "install-github-actions-vm.sh"
        ).read_text(encoding="utf-8")
        install = host_installer.split("install_maintenance() {", 1)[1].split(
            "stop() {", 1
        )[0]
        self.assertIn('guest_status', install)
        self.assertIn('require_command tar', install)
        self.assertIn('install-volume-maintenance.sh', install)
        self.assertIn('/run/blueprint-actions-volume-gc-install', install)
        self.assertIn('--no-same-permissions', install)
        self.assertNotIn('/blueprint-actions-volume-gc-install/*', install)
        chown_command = install.split('sudo chown -h root:root ', 1)[1].split(
            ' && sudo bash', 1
        )[0]
        for asset in (
            'prune-runner-volumes.sh',
            'thaw-owned-runners.sh',
            'blueprint-actions-volume-gc.service',
            'blueprint-actions-volume-gc.timer',
            'install-volume-maintenance.sh',
        ):
            self.assertIn(
                f'/run/blueprint-actions-volume-gc-install/{asset}', chown_command
            )
        self.assertNotIn(
            '--directory /usr/local/lib/blueprint/github-actions-vm', install
        )
        self.assertNotIn('systemctl stop', install)
        self.assertIn('maintenance-status) maintenance_status', host_installer)

        guest_installer = (ASSET_DIR / "install-volume-maintenance.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('flock --timeout 300', guest_installer)
        self.assertIn('mv --force --no-target-directory', guest_installer)
        self.assertIn('cmp --silent', guest_installer)

    def test_volume_gc_fallback_thaws_only_owned_markers(self):
        thaw = (ASSET_DIR / "thaw-owned-runners.sh").read_text(encoding="utf-8")
        self.assertIn('for slot in 1 2 3 4', thaw)
        self.assertIn('[[ -f "$marker" && ! -L "$marker" ]] || continue', thaw)
        self.assertIn('"0:600"', thaw)
        self.assertIn('expected="blueprint-github-actions@$slot.service"', thaw)
        self.assertNotIn('for unit in', thaw)

    def test_registration_secret_is_fixed_path_private_and_shredded(self):
        installer = (ASSET_DIR / "install-runners.sh").read_text(encoding="utf-8")
        self.assertIn(
            'BLUEPRINT_REGISTRATION_TOKEN_FILE="/run/blueprint-actions-registration-token"',
            installer,
        )
        self.assertIn('== "600"', installer)
        self.assertIn('== "root"', installer)
        self.assertIn('shred -u "$BLUEPRINT_REGISTRATION_TOKEN_FILE"', installer)
        self.assertIn(
            'RUNNER_SHA256="04cf0be1aff4c3ec3554466c39124ca250e3effd8873bb7e8d68535aa9505d5d"',
            installer,
        )
        self.assertIn("sha256sum --check --status", installer)
        self.assertNotIn("--pat", installer)

    def test_viewport_container_tracks_locked_playwright_and_stays_hardened(self):
        workflow = (
            Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        viewport = workflow.split("- name: Viewport tests (browser mode)", 1)[1].split(
            "- name: Upload viewport failure screenshots", 1
        )[0]
        self.assertIn(
            "node -p \"require('playwright-core/package.json').version\"",
            viewport,
        )
        self.assertIn(
            'playwright_image="mcr.microsoft.com/playwright:v${playwright_version}-noble"',
            viewport,
        )
        self.assertNotRegex(viewport, r"playwright:v\d+\.\d+\.\d+-noble")
        for hardening in (
            '--user "$(id -u):$(id -g)"',
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--network=none",
            "--pids-limit=512",
            "--memory=8g",
            "--cpus=6",
            "--tmpfs /tmp:rw,nosuid,nodev,size=1g",
            "--shm-size=2g",
        ):
            self.assertIn(hardening, viewport)
        for lifecycle_guard in (
            "--filter label=com.blueprint.ci=viewport",
            "trap cleanup_viewport EXIT INT TERM",
            "timeout --signal=TERM --kill-after=30s 20m",
            "--label com.blueprint.ci=viewport",
            'docker rm -f "$container_name"',
            'node_root="$(dirname "$(dirname "$(command -v node)")")"',
            '--mount type=bind,src="$node_root",dst=/opt/ci-node,readonly',
            "--env PATH=/opt/ci-node/bin:",
        ):
            self.assertIn(lifecycle_guard, viewport)
        self.assertIn("max-parallel: 2", workflow)
        self.assertIn("shard: [1, 2, 3, 4]", workflow)
        self.assertIn("--shard=${{ matrix.shard }}/4", viewport)
        self.assertIn(
            'bash scripts/run_viewport_with_retry.sh 3m "$container_name"', viewport
        )
        self.assertIn('exit "$viewport_status"', viewport)
        self.assertIn(
            "viewport-failure-screenshots-shard-${{ matrix.shard }}",
            workflow,
        )

    # Its worst case is the `hard_timeout` sample: a child that traps TERM and
    # sleeps 5s, so the attempt costs the full sleep rather than the 2s budget,
    # times up to three attempts -- ~15s. That was already marginal against the
    # then-30s lane budget, and the inner subprocess budget below could never
    # fire underneath it. 120s gives real headroom and lets the inner timeout
    # report a genuinely hung retry loop as itself rather than as an anonymous
    # LANE_HANG_DETECTOR_SECONDS kill.
    @pytest.mark.timeout(120)
    def test_viewport_retry_distinguishes_infrastructure_from_child_exit(self):
        bash = None
        if os.name == "nt":
            git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
            if git_bash.exists():
                bash = str(git_bash)
        else:
            bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is required for the viewport retry lifecycle test")

        helper = Path(__file__).parent / "run_viewport_with_retry.sh"

        def bash_path(path: Path) -> str:
            rendered = path.resolve().as_posix()
            if os.name == "nt" and len(rendered) >= 3 and rendered[1:3] == ":/":
                return f"/{rendered[0].lower()}{rendered[2:]}"
            return rendered

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            docker_log = temp / "docker.log"
            attempt_log = temp / "attempt.log"
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "'
                + bash_path(docker_log)
                + '"\n',
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            fake_tee = fake_bin / "tee"
            fake_tee.write_text(
                "#!/usr/bin/env bash\n"
                'if [[ "${FAIL_TEE:-}" == "1" ]]; then\n'
                "  cat >/dev/null\n"
                "  exit 1\n"
                "fi\n"
                'exec /usr/bin/tee "$@"\n',
                encoding="utf-8",
            )
            fake_tee.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "BLUEPRINT_VIEWPORT_KILL_AFTER": "0.1s",
                    "ATTEMPT_LOG": bash_path(attempt_log),
                    "PATH": f"{bash_path(fake_bin)}:{env.get('PATH', '')}",
                    "RUNNER_TEMP": bash_path(temp),
                }
            )

            # The per-attempt budget has to clear process startup for the
            # two nested bash spawns below, or every case that is supposed
            # to RUN TO COMPLETION gets wall-clock killed instead and
            # silently exercises the timeout path. The previous 0.1s was
            # under startup cost on a slow or Windows host.
            def run(
                command: str, attempt_timeout: str = "2s"
            ) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        bash,
                        bash_path(helper),
                        attempt_timeout,
                        "blueprint-viewport-test",
                        "/usr/bin/bash",
                        "-c",
                        command,
                    ],
                    check=False,
                    capture_output=True,
                    env=env,
                    text=True,
                    # Under the enclosing marker, so a hung retry loop reports
                    # here as itself instead of being killed by pytest-timeout.
                    timeout=60,
                )

            def sample(name: str, text: str) -> str:
                """Materialize a verbatim runner transcript for a fake attempt."""
                path = temp / name
                # write_bytes, not write_text: the ANSI-coded fixtures below
                # must reach the classifier byte for byte, with no newline
                # translation on Windows.
                path.write_bytes(text.encode("utf-8"))
                return bash_path(path)

            def reset() -> None:
                for path in (docker_log, attempt_log):
                    if path.exists():
                        path.unlink()

            def docker_removals() -> int:
                if not docker_log.exists():
                    return 0
                return docker_log.read_text(encoding="utf-8").count("rm -f")

            def attempts() -> int:
                if not attempt_log.exists():
                    return 0
                return attempt_log.read_text(encoding="utf-8").count("attempt")

            # ---------------------------------------------------------------
            # Fixtures. Every one is a verbatim transcript from a real vitest
            # 4.1.10 browser-mode run -- either CI run 32805982004 or a locally
            # planted defect -- not a hand-written approximation. The SGR
            # escapes are real: vitest interleaves them inside the summary line
            # under CI, so "Tests" and "1 failed" are not adjacent in the raw
            # bytes and the classifier only works if it normalizes them first.
            # ---------------------------------------------------------------

            # CI run 32805982004, shard 3/4, attempt 1. The dev server never
            # served the module and never said why: not one "[vite]" line
            # appears anywhere in that job, and every test that did run passed.
            # This is the retryable class.
            provider_disconnect = (
                " \u276f  chromium  src/components/DependencyHealthCard"
                ".viewport.test.tsx (0 test)\n"
                "\u23af\u23af\u23af Failed Suites 1 \u23af\u23af\u23af\n"
                " FAIL   chromium  src/components/DependencyHealthCard"
                ".viewport.test.tsx\n"
                "Error: Failed to import test file /work/admin-ui/src/components"
                "/DependencyHealthCard.viewport.test.tsx\n"
                "Caused by: TypeError: Failed to fetch dynamically imported module: "
                "http://localhost:63315/work/admin-ui/src/components"
                "/DependencyHealthCard.viewport.test.tsx?import&browserv=1787630480373\n"
                "Vitest caught 1 unhandled error during the test run.\n"
                "Error: Failed to run the test /work/admin-ui/src/components"
                "/AcceptanceGradeBadge.viewport.test.tsx.\n"
                "Caused by: Error: Cannot connect to the iframe. Did you change the "
                "location or submitted a form?\n"
                "\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[31m1 failed\x1b[39m\x1b[22m"
                "\x1b[2m | \x1b[22m\x1b[1m\x1b[32m36 passed\x1b[39m\x1b[22m"
                "\x1b[90m (41)\x1b[39m\n"
                "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[32m264 passed\x1b[39m"
                "\x1b[22m\x1b[90m (264)\x1b[39m\n"
            )

            # Same job, the retry, which passed every file it reached.
            clean_pass = (
                " \u2713 |chromium| src/components/DependencyHealthCard"
                ".viewport.test.tsx (3 tests) 111ms\n"
                "\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[32m41 passed\x1b[39m"
                "\x1b[22m\x1b[90m (41)\x1b[39m\n"
                "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[32m273 passed\x1b[39m"
                "\x1b[22m\x1b[90m (273)\x1b[39m\n"
            )

            # A real unresolvable import, planted locally. It prints the SAME
            # client-side "Failed to fetch dynamically imported module:" string
            # as the transient above, because Vite answers the module request
            # with a 500. What separates them is that the dev server states its
            # reason.
            unresolved_import = (
                "9:43:46 PM [vite] Internal server error: Failed to resolve import "
                '"./ZzThisModuleDoesNotExist" from '
                '"src/components/ZzTmpBadImport.viewport.test.tsx". '
                "Does the file exist?\n"
                "  Plugin: vite:import-analysis\n"
                "\u23af\u23af\u23af Failed Suites 1 \u23af\u23af\u23af\n"
                " FAIL  |chromium| src/components/ZzTmpBadImport.viewport.test.tsx\n"
                "Error: Failed to import test file "
                "src/components/ZzTmpBadImport.viewport.test.tsx\n"
                "Caused by: TypeError: Failed to fetch dynamically imported module: "
                "http://localhost:63315/@fs/src/components"
                "/ZzTmpBadImport.viewport.test.tsx?import&browserv=1787633026754\n"
                " Test Files  1 failed (1)\n"
                "      Tests  no tests\n"
            )

            # A real syntax error, planted locally. Same client-side string
            # again; only the server-side reason differs.
            syntax_error = (
                "9:43:55 PM [vite] Internal server error: "
                "Transform failed with 1 error:\n"
                "src/components/ZzTmpSyntax.viewport.test.tsx:5:2: "
                'ERROR: Expected ")" but found "}"\n'
                "  Plugin: vite:esbuild\n"
                "Error: Failed to import test file "
                "src/components/ZzTmpSyntax.viewport.test.tsx\n"
                "Caused by: TypeError: Failed to fetch dynamically imported module: "
                "http://localhost:63315/@fs/src/components"
                "/ZzTmpSyntax.viewport.test.tsx?import&browserv=1787633034993\n"
                " Test Files  1 failed (1)\n"
                "      Tests  no tests\n"
            )

            # A test that ran and asserted false, planted locally.
            assertion_failure = (
                " \u276f |chromium| src/components/ZzTmpAssert.viewport.test.tsx "
                "(1 test | 1 failed) 189ms\n"
                "     \u00d7 renders without horizontal overflow at 375px 188ms\n"
                "\u23af\u23af\u23af Failed Tests 1 \u23af\u23af\u23af\n"
                " FAIL  |chromium| src/components/ZzTmpAssert.viewport.test.tsx > "
                "renders without horizontal overflow at 375px\n"
                "AssertionError: expected 2 to be 3 // Object.is equality\n"
                "\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[31m1 failed\x1b[39m"
                "\x1b[22m\x1b[90m (1)\x1b[39m\n"
                "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[31m1 failed\x1b[39m"
                "\x1b[22m\x1b[90m (1)\x1b[39m\n"
            )

            # A real exception thrown inside a running test, planted locally.
            thrown_exception = (
                " \u276f |chromium| src/components/ZzTmpThrow.viewport.test.tsx "
                "(1 test | 1 failed) 34ms\n"
                "\u23af\u23af\u23af Failed Tests 1 \u23af\u23af\u23af\n"
                " FAIL  |chromium| src/components/ZzTmpThrow.viewport.test.tsx > "
                "renders without horizontal overflow at 375px\n"
                "TypeError: Cannot read properties of null (reading 'width')\n"
                "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[31m1 failed\x1b[39m"
                "\x1b[22m\x1b[90m (1)\x1b[39m\n"
            )

            # A real failure carrying transport noise on top. A transport
            # signature must never be able to outvote a reported test failure.
            failure_under_transport_noise = (
                "Caused by: TypeError: Failed to fetch dynamically imported module: "
                "http://localhost:63315/x.tsx\n"
                "Cannot connect to the iframe.\n" + assertion_failure
            )

            # A test failure reported inline, with the run then truncated by a
            # wall-clock kill before any summary block is written.
            truncated_after_failure = (
                " \u276f |chromium| src/components/ZzTmpAssert.viewport.test.tsx "
                "(1 test | 1 failed) 189ms\n"
            )

            # Each veto fixture must actually contain the string the retry keys
            # on -- otherwise the "does not retry" assertions below would pass
            # vacuously against a condition that never had to decide anything.
            for fixture in (
                unresolved_import,
                syntax_error,
                failure_under_transport_noise,
            ):
                self.assertIn("Failed to fetch dynamically imported module:", fixture)

            disconnect_path = sample("provider_disconnect.txt", provider_disconnect)
            clean_pass_path = sample("clean_pass.txt", clean_pass)
            veto_paths = {
                "assertion failure": sample("assert.txt", assertion_failure),
                "thrown exception": sample("throw.txt", thrown_exception),
                "unresolved import": sample("unresolved.txt", unresolved_import),
                "syntax error": sample("syntax.txt", syntax_error),
                "real failure under transport noise": sample(
                    "laundering.txt", failure_under_transport_noise
                ),
            }
            truncated_path = sample("truncated.txt", truncated_after_failure)

            # --- Child exit codes stay the child's, never the harness's. -----
            genuine_124 = run("exit 124")
            self.assertEqual(genuine_124.returncode, 124, genuine_124.stderr)
            self.assertEqual(docker_removals(), 0)

            genuine_137 = run("exit 137")
            self.assertEqual(genuine_137.returncode, 137, genuine_137.stderr)
            self.assertEqual(docker_removals(), 0)

            normal_failure = run("exit 42")
            self.assertEqual(normal_failure.returncode, 42, normal_failure.stderr)
            self.assertEqual(docker_removals(), 0)

            env["FAIL_TEE"] = "1"
            capture_failure = run("exit 0")
            env.pop("FAIL_TEE")
            self.assertEqual(capture_failure.returncode, 125, capture_failure.stderr)
            self.assertIn(
                "Could not capture viewport provider output", capture_failure.stderr
            )
            self.assertEqual(docker_removals(), 0)

            # --- Never retry a run in which a test produced a failure. -------
            # Assertion failures, exceptions thrown inside tests, and real
            # compile errors all stay red on the first attempt. Retrying any of
            # them is how a flaky product bug gets laundered into a green
            # promotion.
            for label, path in veto_paths.items():
                reset()
                result = run(
                    f'echo attempt >> "$ATTEMPT_LOG"; cat "{path}"; exit 1',
                )
                self.assertEqual(result.returncode, 1, f"{label}: {result.stderr}")
                self.assertEqual(attempts(), 1, f"{label} was retried")
                self.assertEqual(docker_removals(), 0, f"{label} was retried")

            # A test failure reported inline, then a wall-clock hang: the
            # summary block never gets written, so the veto has to key on the
            # per-file progress line instead.
            reset()
            truncated = run(
                f'echo attempt >> "$ATTEMPT_LOG"; cat "{truncated_path}"; sleep 30',
            )
            self.assertIn(truncated.returncode, (124, 137))
            self.assertEqual(attempts(), 1, truncated.stderr)
            self.assertEqual(docker_removals(), 0, truncated.stderr)

            # --- Retry the transport class, on ANY single signature. ---------
            # The previous condition required three strings to co-occur, so a
            # disconnect that presented even slightly differently got no retry
            # at all. Each signature now stands on its own.
            transport_signatures = (
                "Failed to fetch dynamically imported module: http://localhost:1/x",
                "Cannot connect to the iframe.",
                "[vitest] Browser connection was closed while running tests.",
                "Target page, context or browser has been closed",
                "Page crashed",
                "browserType.launch: Target closed",
                "page.goto: net::ERR_CONNECTION_REFUSED",
            )
            for index, signature in enumerate(transport_signatures):
                signature_path = sample(f"signature_{index}.txt", signature + "\n")
                reset()
                result = run(
                    f'echo attempt >> "$ATTEMPT_LOG"; cat "{signature_path}"; exit 1',
                )
                self.assertEqual(
                    attempts(), 3, f"{signature!r} did not exhaust the retry budget"
                )
                self.assertEqual(
                    docker_removals(), 2, f"{signature!r}: {result.stderr}"
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("attempt budget exhausted", result.stderr)

            # --- The real CI transcript, end to end. -------------------------
            reset()
            disconnect_then_success = run(
                'echo attempt >> "$ATTEMPT_LOG"; '
                'if [[ "$(grep -c attempt "$ATTEMPT_LOG")" == "1" ]]; then '
                f'cat "{disconnect_path}"; exit 1; fi; '
                f'cat "{clean_pass_path}"',
            )
            self.assertEqual(
                disconnect_then_success.returncode, 0, disconnect_then_success.stderr
            )
            self.assertIn("lost its browser transport", disconnect_then_success.stderr)
            self.assertEqual(attempts(), 2)
            self.assertEqual(docker_removals(), 1)

            # Exactly what happened in run 32805982004: attempt 1 lost its
            # transport, the retry fired correctly, and the retry itself then
            # wall-clock hung after passing 40 of 41 files. One spare attempt
            # could not absorb that, and a shard with zero failing tests went
            # red and took the promotion with it.
            reset()
            disconnect_then_hang_then_success = run(
                'echo attempt >> "$ATTEMPT_LOG"; '
                'n="$(grep -c attempt "$ATTEMPT_LOG")"; '
                f'if [[ "$n" == "1" ]]; then cat "{disconnect_path}"; exit 1; fi; '
                'if [[ "$n" == "2" ]]; then sleep 30; fi; '
                f'cat "{clean_pass_path}"',
            )
            self.assertEqual(
                disconnect_then_hang_then_success.returncode,
                0,
                disconnect_then_hang_then_success.stderr,
            )
            self.assertEqual(attempts(), 3)
            self.assertEqual(docker_removals(), 2)

            # --- Wall-clock hangs with no reported test failure. -------------
            reset()
            responsive_timeout = run('echo attempt >> "$ATTEMPT_LOG"; sleep 30')
            self.assertIn(responsive_timeout.returncode, (124, 137))
            self.assertIn("wall clock", responsive_timeout.stderr)
            self.assertEqual(attempts(), 3)
            self.assertEqual(docker_removals(), 2)

            reset()
            timeout_then_success = run(
                'echo attempt >> "$ATTEMPT_LOG"; '
                'if [[ "$(grep -c attempt "$ATTEMPT_LOG")" == "1" ]]; then '
                "sleep 30; fi"
            )
            self.assertEqual(
                timeout_then_success.returncode, 0, timeout_then_success.stderr
            )
            self.assertIn("wall clock", timeout_then_success.stderr)
            self.assertEqual(attempts(), 2)
            self.assertEqual(docker_removals(), 1)

            reset()
            # A child that ignores TERM keeps its own grandchild alive holding
            # the pipe open until that grandchild exits, so keep this sleep
            # short: the attempt costs the full sleep, not the 2s budget.
            hard_timeout = run(
                "echo attempt >> \"$ATTEMPT_LOG\"; trap '' TERM; sleep 5"
            )
            self.assertIn(hard_timeout.returncode, (124, 137))
            self.assertIn("wall clock", hard_timeout.stderr)
            self.assertEqual(attempts(), 3)
            self.assertEqual(docker_removals(), 2)

            # --- The attempt budget is configurable, bounded and validated. --
            reset()
            env["BLUEPRINT_VIEWPORT_MAX_ATTEMPTS"] = "1"
            single_attempt = run(
                f'echo attempt >> "$ATTEMPT_LOG"; cat "{disconnect_path}"; exit 1',
            )
            self.assertEqual(single_attempt.returncode, 1, single_attempt.stderr)
            self.assertEqual(attempts(), 1)
            self.assertEqual(docker_removals(), 0)

            env["BLUEPRINT_VIEWPORT_MAX_ATTEMPTS"] = "99"
            rejected_budget = run("exit 0")
            self.assertEqual(rejected_budget.returncode, 2, rejected_budget.stderr)
            self.assertIn("invalid viewport max attempts", rejected_budget.stderr)
            env.pop("BLUEPRINT_VIEWPORT_MAX_ATTEMPTS")

    def test_backend_scrubs_cross_run_postgres_leftovers(self):
        workflow = (
            Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        cleanup = workflow.split("- name: Clean stale PostgreSQL resources", 1)[
            1
        ].split("- name: Start PostgreSQL", 1)[0]
        self.assertIn(
            'docker ps -aq --filter "name=blueprint-ci-postgres-"',
            cleanup,
        )
        self.assertIn("docker rm -f", cleanup)
        self.assertIn("docker network ls", cleanup)
        self.assertIn("sed -n '/^blueprint-ci-/p'", cleanup)
        self.assertIn(
            'docker ps -aq --filter "network=$network"',
            cleanup,
        )
        self.assertLess(
            cleanup.index('docker ps -aq --filter "network=$network"'),
            cleanup.index('docker network rm "$network"'),
        )
        self.assertNotIn('docker rm -f "$POSTGRES_CONTAINER"', cleanup)
        self.assertNotIn('docker network rm "$CI_NETWORK"', cleanup)

    def test_deploy_preserves_scale_to_zero_cloud_run_profile(self):
        workflow = (
            Path(__file__).parents[1] / ".github" / "workflows" / "deploy.yml"
        ).read_text(encoding="utf-8")
        deploy = workflow.split("- name: Deploy to Cloud Run", 1)[1].split(
            "run-migrations:", 1
        )[0]
        self.assertIn("--min-instances 0", deploy)
        self.assertIn("--max-instances 1", deploy)
        self.assertIn("--concurrency 120", deploy)
        self.assertNotIn("--min-instances 1", deploy)
        self.assertNotIn("--max-instances 12", deploy)

    def test_deploy_keeps_production_host_and_credentials_out_of_build_steps(self):
        workflow = (
            Path(__file__).parents[1] / ".github" / "workflows" / "deploy.yml"
        ).read_text(encoding="utf-8")
        detect = workflow.split("  detect:", 1)[1].split(
            "  build-frontend-artifact:", 1
        )[0]
        self.assertIn("runs-on: ubuntu-latest", detect)
        self.assertNotIn("self-hosted", detect)
        self.assertIn(
            "dorny/paths-filter@de90cc6fb38fc0963ad72b210f1f284cd68cea36",
            detect,
        )

        same_origin_build = workflow.split("  build-frontend-artifact:", 1)[1].split(
            "  build-backend:", 1
        )[0]
        self.assertNotIn("id-token: write", same_origin_build)
        self.assertNotIn("google-github-actions/auth", same_origin_build)

        firebase_build = workflow.split("  build-firebase-frontend:", 1)[1].split(
            "  deploy-frontend:", 1
        )[0]
        self.assertNotIn("id-token: write", firebase_build)
        self.assertNotIn("google-github-actions/auth", firebase_build)

        firebase_deploy = workflow.split("  deploy-frontend:", 1)[1]
        self.assertIn("id-token: write", firebase_deploy)
        self.assertIn("actions/download-artifact@v4", firebase_deploy)
        self.assertIn("--verify admin-ui/dist", firebase_deploy)


if __name__ == "__main__":
    unittest.main()

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
            *ASSET_DIR.glob("*.sh"),
            *ASSET_DIR.glob("*.service"),
            *ASSET_DIR.glob("*.timer"),
        ]
        self.assertTrue(deployment_assets)
        for asset in deployment_assets:
            self.assertNotIn(b"\r", asset.read_bytes(), asset)

    def _volume_gc_harness(self, temporary: str, *, freezer_state: str) -> tuple[dict[str, str], Path, Path]:
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
        cgroup.mkdir(parents=True)
        (cgroup / "cgroup.procs").write_text(f"{os.getpid()}\n", encoding="utf-8")

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
                if [[ "$*" == *"%u:%a"* ]]; then printf '0:600\\n'; else printf '0 660\\n'; fi
            """,
            "docker": """
                #!/usr/bin/env bash
                case "$*" in
                  *"info --format"*) printf '%s/docker-1\\n' "$BLUEPRINT_ACTIONS_STATE_ROOT" ;;
                  *"image ls"*) printf 'sha256:test-image\\n' ;;
                  *"volume ls"*) printf 'test-volume\\n' ;;
                  *" ps --quiet"*) : ;;
                  *"volume prune --force"*) printf 'Total reclaimed space: 1GB\\n' ;;
                  *) exit 2 ;;
                esac
            """,
            "systemctl": """
                #!/usr/bin/env bash
                printf '%s\\n' "$*" >> "$BLUEPRINT_TEST_SYSTEMCTL_LOG"
                case "$1" in
                  is-active) printf 'active\\n' ;;
                  show)
                    if [[ "$*" == *"FreezerState"* ]]; then
                      printf '%s\\n' "$BLUEPRINT_TEST_FREEZER_STATE"
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
                    timeout=180,
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

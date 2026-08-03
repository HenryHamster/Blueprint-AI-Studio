"""Focused security and rendering tests for the Actions KVM boundary."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
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
        ):
            self.assertIn(name, rendered)

    @unittest.skipIf(os.name == "nt", "POSIX file modes are not enforced on Windows")
    def test_atomic_render_is_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "user-data"
            gav.atomic_write_private(path, "secret\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class AssetSecurityTests(unittest.TestCase):
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

            def run(command: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        bash,
                        bash_path(helper),
                        "0.1s",
                        "blueprint-viewport-test",
                        "/usr/bin/bash",
                        "-c",
                        command,
                    ],
                    check=False,
                    capture_output=True,
                    env=env,
                    text=True,
                    timeout=10,
                )

            genuine_124 = run("exit 124")
            self.assertEqual(genuine_124.returncode, 124, genuine_124.stderr)
            self.assertFalse(docker_log.exists())

            genuine_137 = run("exit 137")
            self.assertEqual(genuine_137.returncode, 137, genuine_137.stderr)
            self.assertFalse(docker_log.exists())

            normal_failure = run("exit 42")
            self.assertEqual(normal_failure.returncode, 42, normal_failure.stderr)
            self.assertFalse(docker_log.exists())

            env["FAIL_TEE"] = "1"
            capture_failure = run("exit 0")
            env.pop("FAIL_TEE")
            self.assertEqual(capture_failure.returncode, 125, capture_failure.stderr)
            self.assertIn(
                "Could not capture viewport provider output", capture_failure.stderr
            )
            self.assertFalse(docker_log.exists())

            partial_provider_error = run(
                'printf "%s\\n" "Cannot connect to the iframe."; exit 43'
            )
            self.assertEqual(
                partial_provider_error.returncode, 43, partial_provider_error.stderr
            )
            self.assertFalse(docker_log.exists())

            provider_disconnect_then_success = run(
                'if [[ ! -e "$ATTEMPT_LOG" ]]; then '
                'printf "attempt\\n" > "$ATTEMPT_LOG"; '
                'printf "%s\\n" "Failed to fetch dynamically imported module: test" '
                '"Cannot connect to the iframe." '
                '"Vitest caught 1 unhandled error during the test run."; '
                "exit 1; "
                "fi; "
                'printf "attempt\\n" >> "$ATTEMPT_LOG"'
            )
            self.assertEqual(
                provider_disconnect_then_success.returncode,
                0,
                provider_disconnect_then_success.stderr,
            )
            self.assertIn(
                "disconnected from its browser iframe; retrying once",
                provider_disconnect_then_success.stderr,
            )
            self.assertEqual(
                attempt_log.read_text(encoding="utf-8").count("attempt"), 2
            )
            self.assertEqual(docker_log.read_text(encoding="utf-8").count("rm -f"), 1)

            docker_log.unlink()
            attempt_log.unlink()
            persistent_provider_disconnect = run(
                'printf "%s\\n" "Failed to fetch dynamically imported module: test" '
                '"Cannot connect to the iframe." '
                '"Vitest caught 1 unhandled error during the test run."; '
                "exit 44"
            )
            self.assertEqual(
                persistent_provider_disconnect.returncode,
                44,
                persistent_provider_disconnect.stderr,
            )
            self.assertIn(
                "disconnected from its browser iframe; retrying once",
                persistent_provider_disconnect.stderr,
            )
            self.assertEqual(docker_log.read_text(encoding="utf-8").count("rm -f"), 1)

            docker_log.unlink()
            responsive_timeout = run('echo attempt >> "$ATTEMPT_LOG"; sleep 2')
            self.assertEqual(responsive_timeout.returncode, 124)
            self.assertIn("timed out; retrying once", responsive_timeout.stderr)
            self.assertEqual(docker_log.read_text(encoding="utf-8").count("rm -f"), 1)
            self.assertEqual(
                attempt_log.read_text(encoding="utf-8").count("attempt"), 2
            )

            docker_log.unlink()
            attempt_log.unlink()
            timeout_then_success = run(
                'if [[ ! -e "$ATTEMPT_LOG" ]]; then touch "$ATTEMPT_LOG"; sleep 2; fi'
            )
            self.assertEqual(
                timeout_then_success.returncode, 0, timeout_then_success.stderr
            )
            self.assertIn("timed out; retrying once", timeout_then_success.stderr)
            self.assertEqual(docker_log.read_text(encoding="utf-8").count("rm -f"), 1)

            docker_log.unlink()
            attempt_log.unlink()
            hard_timeout = run(
                "echo attempt >> \"$ATTEMPT_LOG\"; trap '' TERM; sleep 2"
            )
            self.assertIn(hard_timeout.returncode, (124, 137))
            self.assertIn("timed out; retrying once", hard_timeout.stderr)
            self.assertEqual(docker_log.read_text(encoding="utf-8").count("rm -f"), 1)
            self.assertEqual(
                attempt_log.read_text(encoding="utf-8").count("attempt"), 2
            )

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

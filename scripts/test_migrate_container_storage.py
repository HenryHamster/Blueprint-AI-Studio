from pathlib import Path
import re
import unittest


SCRIPT = Path(__file__).with_name("migrate-container-storage.sh")
TEXT = SCRIPT.read_text(encoding="utf-8")


class ContainerStorageMigrationContractTests(unittest.TestCase):
    def test_script_is_lf_only_and_strict(self):
        raw = SCRIPT.read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertTrue(raw.startswith(b"#!/usr/bin/env bash\nset -euo pipefail\n"))
        self.assertIn("PATH=/usr/sbin:/usr/bin:/sbin:/bin", TEXT)
        self.assertIn("migration script must be root-owned mode 0700", TEXT)

    def test_moves_both_docker_and_containerd_stores(self):
        self.assertIn("readonly DOCKER_SOURCE=/var/lib/docker", TEXT)
        self.assertIn("readonly CONTAINERD_SOURCE=/var/lib/containerd", TEXT)
        self.assertIn('DOCKER_DEST="$DEST_ROOT/docker"', TEXT)
        self.assertIn('CONTAINERD_DEST="$DEST_ROOT/containerd"', TEXT)
        self.assertNotIn("BLUEPRINT_CONTAINER_DATA_MOUNT", TEXT)

    def test_copy_preserves_runtime_metadata_and_has_offline_verification(self):
        flat = TEXT.replace("\\\n    ", "")
        self.assertIn(
            "rsync --archive --hard-links --acls --xattrs --numeric-ids --one-file-system",
            flat,
        )
        self.assertIn("--delete --dry-run --itemize-changes", flat)
        self.assertLess(TEXT.index("stop_writers"), TEXT.index("performing final offline sync"))
        self.assertIn("two source copies plus 20 GiB", TEXT)

    def test_mounts_fail_closed_before_runtime(self):
        self.assertGreaterEqual(TEXT.count("ConditionPathIsMountPoint=$DATA_MOUNT"), 4)
        self.assertIn("Requires=var-lib-docker.mount var-lib-containerd.mount", TEXT)
        self.assertIn("Before=docker.service", TEXT)
        self.assertIn("Before=containerd.service", TEXT)
        self.assertIn("findmnt --verify --verbose", TEXT)
        self.assertIn("systemd-analyze verify", TEXT)
        self.assertIn("--output FSROOT", TEXT)
        self.assertIn("assert_bind_source \"$DOCKER_SOURCE\" \"$DOCKER_DEST\"", TEXT)

    def test_cutover_retains_exact_rollback_directories(self):
        self.assertIn('mv -- "$DOCKER_SOURCE" "$DOCKER_BACKUP"', TEXT)
        self.assertIn('mv -- "$CONTAINERD_SOURCE" "$CONTAINERD_BACKUP"', TEXT)
        self.assertIn("trap 'on_cutover_error' ERR", TEXT)
        self.assertIn("rollback backups are unavailable", TEXT)
        self.assertIn("rollback could not unmount", TEXT)
        self.assertIn("Docker containers remained before rollback", TEXT)
        self.assertIn("containerd shims remained before rollback", TEXT)

    def test_cleanup_requires_post_reboot_verification_and_avoids_recursive_delete(self):
        self.assertIn('[[ -f "$REBOOT_MARKER" ]] || die "verify after reboot before cleanup"', TEXT)
        self.assertIn("current-boot verification passed; reboot verification still required", TEXT)
        self.assertIn("/proc/sys/kernel/random/boot_id", TEXT)
        self.assertNotRegex(TEXT, re.compile(r"\brm\s+(?:-[^\n ]*r|--recursive)"))
        self.assertIn('mv -- "$DOCKER_BACKUP" "$rollback_root/docker"', TEXT)
        self.assertIn('mv -- "$CONTAINERD_BACKUP" "$rollback_root/containerd"', TEXT)

    def test_local_runner_must_be_stopped(self):
        self.assertIn("local_runner.py|blueprint-runner", TEXT)
        self.assertIn("stop its user service before cutover", TEXT)

    def test_inventory_and_application_health_are_verified(self):
        self.assertIn("assert_runtime_inventory_preserved", TEXT)
        self.assertIn("images.contract", TEXT)
        self.assertIn("volumes.contract", TEXT)
        self.assertIn("http://127.0.0.1:8080/health", TEXT)
        self.assertIn("start_actions_vm_if_required", TEXT)
        self.assertLess(
            TEXT.index("assert_application_healthy\n  capture_inventory", TEXT.index("verify()")),
            TEXT.index("start_actions_vm_if_required", TEXT.index("verify()")),
        )

    def test_destination_requires_root_owned_migration_manifest(self):
        self.assertIn("prepare_destination_root", TEXT)
        self.assertIn(".blueprint-container-storage-migration-v1", TEXT)
        self.assertIn("destination ownership manifest metadata is unsafe", TEXT)

    def test_stop_checks_real_containerd_shims_and_namespaces(self):
        self.assertIn("containerd-shim(-[^[:space:]]*)?", TEXT)
        self.assertIn("(^|/)qemu-system-x86_64([[:space:]]|$)", TEXT)
        self.assertNotIn("pgrep --exact qemu-system-x86_64", TEXT)
        self.assertIn("assert_containerd_namespaces_supported", TEXT)
        self.assertIn("unreviewed non-Docker containerd namespace", TEXT)
        self.assertIn("Docker container scopes remained after stop", TEXT)
        self.assertIn("Docker containers remained after explicit service shutdown", TEXT)
        self.assertIn("docker stop --time 120 blueprint-actions-vm", TEXT)
        self.assertIn("assert_no_nested_mounts", TEXT)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

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
        def run(argv, **_kwargs):
            if argv[1] == "logs":
                return subprocess.CompletedProcess(
                    argv, 0, "Application startup complete.\n", ""
                )
            return subprocess.CompletedProcess(argv, 0, "1\n", "")

        with patch.object(release, "_run", side_effect=run) as run_mock:
            release._verify_worker_ready()
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
        worker = b"BLUEPRINT_WORKER_IMAGE_ID=" + OLD_IMAGE.encode() + b"\n"

        def role_file(path: Path) -> bytes:
            return api if "/api/" in path.as_posix() else worker

        def marker(unit: str) -> tuple[str, str]:
            return "active", f"marker-{unit}"

        with contextlib.ExitStack() as stack:
            mocks = {
                "atomic": stack.enter_context(patch.object(release, "_atomic_write")),
                "run": stack.enter_context(
                    patch.object(release, "_run", return_value=completed())
                ),
                "container": stack.enter_context(
                    patch.object(release, "_verify_container")
                ),
            }
            stack.enter_context(patch.object(release, "_is_root", return_value=True))
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
            stack.enter_context(patch.object(release, "_wait_active"))
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
            restarts = [
                item
                for item in mocks["run"].call_args_list
                if item.args[0][:2] == ["/usr/bin/systemctl", "restart"]
            ]
            self.assertEqual(
                restarts,
                [
                    call(
                        [
                            "/usr/bin/systemctl",
                            "restart",
                            "blueprint-worker.service",
                        ],
                        timeout=120,
                    ),
                    call(
                        [
                            "/usr/bin/systemctl",
                            "restart",
                            "blueprint-api.service",
                        ],
                        timeout=120,
                    ),
                ],
            )

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

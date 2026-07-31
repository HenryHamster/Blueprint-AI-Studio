import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("gcs_create_only_upload.py")
SPEC = importlib.util.spec_from_file_location("gcs_create_only_upload", MODULE_PATH)
assert SPEC and SPEC.loader
upload = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upload)


class CreateOnlyUploadTests(unittest.TestCase):
    def test_url_encodes_object_and_requires_generation_zero(self):
        url = upload._upload_url("gs://recovery-bucket/path/a file+1.json")
        self.assertTrue(
            url.startswith(
                "https://storage.googleapis.com/upload/storage/v1/b/"
                "recovery-bucket/o?"
            )
        )
        self.assertIn("uploadType=media", url)
        self.assertIn("name=path%2Fa+file%2B1.json", url)
        self.assertIn("ifGenerationMatch=0", url)

    def test_destination_rejects_bucket_only_control_chars_and_oversize(self):
        for value in (
            "gs://recovery-bucket",
            "https://storage.googleapis.com/recovery-bucket/object",
            "gs://Bad_Bucket/object",
            "gs://recovery-bucket/bad\nobject",
            "gs://recovery-bucket/" + "a" * 1025,
        ):
            with self.subTest(value=value[:80]), self.assertRaises(ValueError):
                upload._destination(value)

    def test_curl_streams_stdin_without_token_or_followup_read(self):
        argv = upload._curl_argv(
            source="-",
            destination="gs://recovery-bucket/archive.sql.gz",
            header_file=Path("/tmp/private.headers"),
        )
        self.assertEqual(argv[0], "curl")
        self.assertIn("--upload-file", argv)
        self.assertEqual(argv[argv.index("--upload-file") + 1], "-")
        self.assertIn(f"@{Path('/tmp/private.headers')}", argv)
        self.assertIn("--fail-with-body", argv)
        self.assertIn("--retry", argv)
        self.assertEqual(argv[argv.index("--retry") + 1], "0")
        self.assertNotIn("Authorization:", " ".join(argv))
        self.assertNotIn("gcloud", argv)
        self.assertNotIn("storage.objects.get", " ".join(argv))

    def test_token_command_is_noninteractive_and_rejects_whitespace(self):
        success = subprocess.CompletedProcess([], 0, stdout="opaque-token\n", stderr="")
        with patch.object(upload.subprocess, "run", return_value=success) as run:
            self.assertEqual(upload._access_token(), "opaque-token")
        self.assertEqual(
            run.call_args.args[0],
            ["gcloud", "auth", "print-access-token", "--quiet"],
        )

        bad = subprocess.CompletedProcess([], 0, stdout="bad token\n", stderr="")
        with (
            patch.object(upload.subprocess, "run", return_value=bad),
            self.assertRaises(RuntimeError),
        ):
            upload._access_token()


if __name__ == "__main__":
    unittest.main()

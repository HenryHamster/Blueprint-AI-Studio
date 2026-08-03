from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import frontend_release_manifest as manifest


class FrontendReleaseManifestTests(unittest.TestCase):
    def test_manifest_is_deterministic_and_self_excluding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            (root / "index.html").write_text("<html>/assets/app-12345678.js</html>")
            (root / "assets/app-12345678.js").write_text("revision")

            destination = manifest.write_manifest(root, "a" * 40)
            payload = json.loads(destination.read_text())
            first = manifest.describe_tree(root)
            second = manifest.describe_tree(root)

            self.assertEqual(first, second)
            self.assertEqual(payload["revision"], "a" * 40)
            self.assertEqual(payload["tree_sha256"], first["tree_sha256"])
            self.assertEqual(payload["file_count"], 2)
            self.assertEqual(
                manifest.verify_manifest(root, "a" * 40)["tree_sha256"],
                first["tree_sha256"],
            )

            (root / "assets/app-12345678.js").write_text("tampered")
            with self.assertRaisesRegex(ValueError, "does not match"):
                manifest.verify_manifest(root, "a" * 40)

    def test_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "index.html").write_text("index")
            outside = root.parent / f"{root.name}-outside"
            outside.write_text("secret")
            try:
                (root / "escape").symlink_to(outside)
            except (NotImplementedError, OSError):
                self.skipTest("symlinks unavailable")
            try:
                with self.assertRaisesRegex(ValueError, "symlink"):
                    manifest.describe_tree(root)
            finally:
                outside.unlink(missing_ok=True)

    def test_invalid_revision_and_missing_index_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "revision"):
                manifest.write_manifest(root, "short")
            with self.assertRaisesRegex(ValueError, "index.html"):
                manifest.describe_tree(root)


if __name__ == "__main__":
    unittest.main()

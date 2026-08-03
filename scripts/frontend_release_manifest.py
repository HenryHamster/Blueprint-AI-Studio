#!/usr/bin/env python3
"""Create and verify the deterministic manifest for a bundled Blueprint UI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

MANIFEST_NAME = ".blueprint-release.json"
MAX_FILES = 4096
MAX_BYTES = 64 * 1024 * 1024


def describe_tree(root: Path) -> dict[str, object]:
    root = root.resolve(strict=True)
    records: list[tuple[str, int, str]] = []
    total = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"frontend bundle contains symlink: {path}")
        if path.is_dir():
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"frontend bundle contains non-regular file: {path}")
        if info.st_nlink != 1:
            raise ValueError(f"frontend bundle contains hard-linked file: {path}")
        relative = path.relative_to(root).as_posix()
        if relative == MANIFEST_NAME:
            continue
        if any(ord(char) < 32 or ord(char) == 127 for char in relative):
            raise ValueError("frontend bundle path contains control characters")
        total += info.st_size
        if len(records) >= MAX_FILES or total > MAX_BYTES:
            raise ValueError("frontend bundle exceeds reviewed size limits")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        records.append((relative, info.st_size, digest))
    index = next((record for record in records if record[0] == "index.html"), None)
    if index is None:
        raise ValueError("frontend bundle is missing index.html")
    tree = hashlib.sha256()
    for relative, size, digest in records:
        tree.update(relative.encode("utf-8"))
        tree.update(b"\0")
        tree.update(str(size).encode("ascii"))
        tree.update(b"\0")
        tree.update(bytes.fromhex(digest))
        tree.update(b"\n")
    return {
        "tree_sha256": tree.hexdigest(),
        "index_sha256": index[2],
        "file_count": len(records),
        "total_bytes": total,
    }


def write_manifest(root: Path, revision: str) -> Path:
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise ValueError("revision must be a full lowercase git SHA")
    descriptor = describe_tree(root)
    payload = {
        "kind": "blueprint.frontend_release.v1",
        "revision": revision,
        **descriptor,
    }
    destination = root / MANIFEST_NAME
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{MANIFEST_NAME}.", dir=root)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return destination


def verify_manifest(root: Path, revision: str) -> dict[str, object]:
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise ValueError("revision must be a full lowercase git SHA")
    manifest_path = root / MANIFEST_NAME
    info = manifest_path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("frontend release manifest must be a regular file")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {
        "kind": "blueprint.frontend_release.v1",
        "revision": revision,
        **describe_tree(root),
    }
    if payload != expected:
        raise ValueError("frontend release manifest does not match the bundle")
    return expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("root", type=Path)
    parser.add_argument("revision")
    args = parser.parse_args()
    try:
        result: object = (
            verify_manifest(args.root, args.revision)["tree_sha256"]
            if args.verify
            else write_manifest(args.root, args.revision)
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Export a committed source snapshot into a new repository with one commit.

This never resets, rewrites, or pushes the source repository. Back up the original
history first; this tool preserves source provenance in the exported checkout.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def export_snapshot(source: Path, destination: Path, ref: str = "HEAD") -> dict:
    source = Path(git(source, "rev-parse", "--show-toplevel"))
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Destination already exists: {destination}")
    if git(source, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("Commit the source changes before exporting a snapshot")
    # Resolve first, so ref cannot inject options into subsequent Git commands.
    commit = git(source, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    tree = git(source, "rev-parse", f"{commit}^{{tree}}")
    # Git can resolve identity from environment or host defaults even when
    # user.name/email are absent from config. Use its actual author identity.
    identity = re.fullmatch(r"(.*?) <([^>]+)> \d+ [+-]\d{4}", git(source, "var", "GIT_AUTHOR_IDENT"))
    if identity is None:
        raise ValueError("Git could not resolve an author identity")
    name, email = identity.group(1), identity.group(2)
    archive = subprocess.check_output(["git", "-C", str(source), "archive", commit])
    destination.mkdir(parents=True)
    # Data filtering rejects paths or symlinks that escape the destination.
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(destination, filter="data")
    record = {
        "kind": "curated-portfolio-snapshot",
        "source_commit": commit,
        "source_tree": tree,
        "history": "A new initial commit; the source repository and original history are preserved separately.",
    }
    manifest = destination / "docs" / "portfolio" / "SNAPSHOT.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    git(destination, "init", "--quiet", "--initial-branch=main")
    git(destination, "config", "user.name", name)
    git(destination, "config", "user.email", email)
    # The source already tracks some paths also listed in .gitignore (e.g. the
    # static check helper). Preserve that exact committed set in the new index.
    git(destination, "add", "--force", "--all")
    git(destination, "commit", "--quiet", "-m", "Blueprint: curated portfolio snapshot")
    record["snapshot_commit"] = git(destination, "rev-parse", "HEAD")
    record["destination"] = str(destination)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="New, empty output path")
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--ref", default="HEAD", help="Committed source revision")
    args = parser.parse_args()
    try:
        result = export_snapshot(args.source, args.destination, args.ref)
    except (ValueError, subprocess.CalledProcessError, tarfile.FilterError) as error:
        parser.exit(1, f"Snapshot export failed: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

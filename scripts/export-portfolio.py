#!/usr/bin/env python3
"""Export a clean checkout while preserving the selected commit's full history.

The output has one main branch, reachable release tags, and no remotes.
This never resets, rewrites, or pushes the source repository.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def export_history(source: Path, destination: Path, ref: str = "HEAD") -> dict:
    source = Path(git(source, "rev-parse", "--show-toplevel"))
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Destination already exists: {destination}")
    if git(source, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("Commit the source changes before exporting")
    # Resolve first, so ref cannot inject options into subsequent Git commands.
    commit = git(
        source, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"
    )
    tree = git(source, "rev-parse", f"{commit}^{{tree}}")
    tags = git(
        source, "for-each-ref", f"--merged={commit}", "--format=%(refname)", "refs/tags"
    ).splitlines()

    destination.mkdir(parents=True)
    git(destination, "init", "--quiet", "--initial-branch=main")
    # Fetch only the selected ancestry. Unlike a clone of every local ref,
    # this does not import stale task branches or local operational config.
    git(destination, "fetch", "--quiet", "--no-tags", str(source), commit)
    git(destination, "checkout", "--quiet", "-B", "main", "FETCH_HEAD")
    if tags:
        # Copy the original tag objects, including annotations and signatures.
        git(
            destination,
            "fetch",
            "--quiet",
            "--no-tags",
            str(source),
            *(f"{tag}:{tag}" for tag in tags),
        )

    return {
        "kind": "history-preserving-portfolio-checkout",
        "source_commit": commit,
        "source_tree": tree,
        "commit_count": int(git(destination, "rev-list", "--count", "HEAD")),
        "branch": "main",
        "release_tag_count": len(tags),
        "destination": str(destination),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="New, empty output path")
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument(
        "--ref", default="HEAD", help="Committed source revision and ancestry"
    )
    args = parser.parse_args()
    try:
        result = export_history(args.source, args.destination, args.ref)
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"History export failed: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

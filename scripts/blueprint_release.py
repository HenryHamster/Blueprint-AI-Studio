#!/usr/bin/env python3
"""Zero-trust argument shim for unattended hamsterwheel app releases.

Installed root-owned at ``/usr/local/sbin/blueprint-release`` and allowed via a
single sudoers rule so an operator (or automation acting as the operator) can
activate a CI-built release without an interactive password:

    henry_hamster ALL=(root) NOPASSWD: /usr/local/sbin/blueprint-release

The security model deliberately does NOT trust the caller:

- argv must be exactly ``<sha256-digest> <40-hex-revision>``; both are matched
  against strict anchored regexes and are inert strings (never shell-expanded).
- the image reference is constructed HERE from a hardcoded registry, project,
  and repository. A caller cannot point the release at another registry or
  image name; only artifacts CI has pushed into the pinned repo are reachable,
  and only workload-identity CI can write there.
- the actual release runs through the newest root-installed immutable
  ``local_app_release.py`` bundle, which independently re-verifies the image
  digest, revision label, static-tree manifest, service ownership, and health,
  and rolls back a failed activation. This shim adds reachability, not trust.
- residual exposure of the sudoers rule: a compromised operator account can
  activate any image CI has ever built for the pinned repo (including an older
  one). It cannot deploy non-CI code. If rollback-abuse matters, revoke the
  rule; the shim then still works via interactive sudo.

No environment values are consumed except SUDO_USER (identity bookkeeping for
the release tool), which is passed through verbatim when present and defaults
to the host operator account.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_REGISTRY = "us-central1-docker.pkg.dev"
_PROJECT = "blueprint-orchestrator"
_REPOSITORY = "blueprint-repo"
_IMAGE = "blueprint-orchestrator"

_BUNDLE_ROOT = Path("/usr/local/lib/blueprint-node")
_BUNDLE_PREFIX = "app-release-"
_RELEASE_TOOL = "local_app_release.py"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_SUDO_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_DEFAULT_SUDO_USER = "henry_hamster"


def _fail(message: str) -> "NoReturn":  # noqa: F821 - py3.9 host compatibility
    print(f"blueprint-release: {message}", file=sys.stderr)
    raise SystemExit(2)


def _newest_release_tool() -> Path:
    if not _BUNDLE_ROOT.is_dir():
        _fail(f"{_BUNDLE_ROOT} does not exist; install a release bundle first")
    candidates = []
    for entry in _BUNDLE_ROOT.iterdir():
        if not entry.name.startswith(_BUNDLE_PREFIX) or not entry.is_dir():
            continue
        if entry.owner() != "root":
            continue
        tool = entry / _RELEASE_TOOL
        if tool.is_file():
            candidates.append(tool)
    if not candidates:
        _fail(f"no root-owned {_BUNDLE_PREFIX}*/{_RELEASE_TOOL} under {_BUNDLE_ROOT}")
    return max(candidates, key=lambda p: p.parent.stat().st_mtime)


def main() -> None:
    if os.geteuid() != 0:
        _fail("must run as root (via the sudoers rule)")
    args = sys.argv[1:]
    if len(args) != 2:
        _fail("usage: blueprint-release <sha256:digest> <40-hex-revision>")
    digest, revision = args
    if not _DIGEST_RE.fullmatch(digest):
        _fail("first argument must match ^sha256:[0-9a-f]{64}$")
    if not _REVISION_RE.fullmatch(revision):
        _fail("second argument must match ^[0-9a-f]{40}$")

    sudo_user = os.environ.get("SUDO_USER", "") or _DEFAULT_SUDO_USER
    if not _SUDO_USER_RE.fullmatch(sudo_user):
        sudo_user = _DEFAULT_SUDO_USER

    image = f"{_REGISTRY}/{_PROJECT}/{_REPOSITORY}/{_IMAGE}@{digest}"
    tool = _newest_release_tool()
    unit = f"blueprint-local-app-release-{revision[:12]}"

    command = [
        "/usr/bin/systemd-run",
        f"--unit={unit}",
        "--collect",
        "--wait",
        "/usr/bin/env",
        "-i",
        f"SUDO_USER={sudo_user}",
        "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
        str(tool),
        "--image",
        image,
        "--expected-revision",
        revision,
    ]
    os.execv(command[0], command)


if __name__ == "__main__":
    main()

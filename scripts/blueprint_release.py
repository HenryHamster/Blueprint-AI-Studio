#!/usr/bin/env python3
"""Zero-trust argument shim for unattended hamsterwheel app releases.

Installed root-owned at ``/usr/local/sbin/blueprint-release`` and invoked by a
socket-activated root-owned systemd service.  The long-lived deploy agent only
has access to that service's Unix socket; it never needs privilege escalation.

The security model deliberately does NOT trust the caller:

- direct argv must be exactly ``<sha256-digest> <40-hex-revision>``; socket
  requests carry the same two values and are matched against strict anchored
  regexes. They are inert strings (never shell-expanded).
- the image reference is constructed HERE from a hardcoded registry, project,
  and repository. A caller cannot point the release at another registry or
  image name; only artifacts CI has pushed into the pinned repo are reachable,
  and only workload-identity CI can write there.
- the actual release runs through the newest root-installed immutable
  ``local_app_release.py`` bundle, which independently re-verifies the image
  digest, revision label, static-tree manifest, service ownership, and health,
  and rolls back a failed activation. This shim adds reachability, not trust.
- the socket is root-owned and group-readable only by the installed service
  account. A compromised service account can request any image CI has ever
  built for the pinned repo (including an older one), but cannot deploy
  non-CI code or replace the root-owned shim.

No environment values are consumed except SUDO_USER (identity bookkeeping for
the release tool), which is passed through verbatim when present and defaults
to the host operator account.
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

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
_SOCKET_FD = 3
_MAX_REQUEST_BYTES = 256


def _fail(message: str) -> NoReturn:
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


def _validated_args(args: list[str]) -> tuple[str, str]:
    if len(args) != 2:
        _fail("usage: blueprint-release <sha256:digest> <40-hex-revision>")
    digest, revision = args
    if not _DIGEST_RE.fullmatch(digest):
        _fail("first argument must match ^sha256:[0-9a-f]{64}$")
    if not _REVISION_RE.fullmatch(revision):
        _fail("second argument must match ^[0-9a-f]{40}$")
    return digest, revision


def _release_command(
    digest: str, revision: str, *, rehearsal: bool = False
) -> list[str]:

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
    if rehearsal:
        command.append("--rehearse")
    return command


def _run_release(digest: str, revision: str) -> int:
    return subprocess.run(_release_command(digest, revision), check=False).returncode


def _run_rehearsal(digest: str, revision: str) -> dict:
    result = subprocess.run(
        _release_command(digest, revision, rehearsal=True),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        _fail(f"rehearsal exited {result.returncode}")
    marker = "LOCAL_APP_RELEASE_REHEARSAL "
    line = next(
        (line for line in result.stdout.splitlines() if line.startswith(marker)),
        None,
    )
    if line is None:
        _fail("rehearsal returned no machine-readable report")
    try:
        report = json.loads(line.removeprefix(marker))
    except json.JSONDecodeError as exc:
        _fail(f"rehearsal report is malformed: {exc}")
    if not isinstance(report, dict):
        _fail("rehearsal report is not an object")
    return report


def _request_args(payload: bytes) -> list[str]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError:
        _fail("request must contain ASCII arguments")
    return text.strip().split()


def _serve_one_request() -> None:
    if os.geteuid() != 0:
        _fail("release socket service must run as root")
    listener = socket.fromfd(_SOCKET_FD, socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection, _ = listener.accept()
        with connection:
            payload = connection.recv(_MAX_REQUEST_BYTES)
            request = _request_args(payload)
            if request and request[0] == "rehearse":
                digest, revision = _validated_args(request[1:])
                report = _run_rehearsal(digest, revision)
                encoded = base64.b64encode(
                    json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
                ).decode("ascii")
                connection.sendall(f"REHEARSAL {encoded}\n".encode("ascii"))
                return
            digest, revision = _validated_args(request)
            return_code = _run_release(digest, revision)
            try:
                if return_code == 0:
                    connection.sendall(b"OK\n")
                else:
                    connection.sendall(f"ERROR release exited {return_code}\n".encode("ascii"))
            except OSError:
                # The agent may time out while the root release continues. A
                # disconnected client must not turn a completed release into
                # a service error or trigger a second release on restart.
                pass
    finally:
        listener.close()


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "--serve":
        _serve_one_request()
        return
    if os.geteuid() != 0:
        _fail("must run as root (via the release socket service)")
    rehearsal = len(sys.argv) == 4 and sys.argv[1] == "--rehearse"
    args = sys.argv[2:] if rehearsal else sys.argv[1:]
    digest, revision = _validated_args(args)
    command = _release_command(digest, revision, rehearsal=rehearsal)
    os.execv(command[0], command)


if __name__ == "__main__":
    main()

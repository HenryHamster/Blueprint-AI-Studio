#!/usr/bin/env python3
"""Zero-trust argument shim for unattended hamsterwheel app releases.

Installed root-owned at ``/usr/local/sbin/blueprint-release`` and invoked by a
socket-activated root-owned systemd service.  The long-lived deploy agent only
has access to that service's Unix socket; it never needs privilege escalation.

The security model deliberately does NOT trust the caller:

- direct argv must be exactly ``<sha256-digest> <40-hex-revision>`` with an
  optional 64-hex operation id; socket requests use the same strict anchored
  grammar. They are inert strings (never shell-expanded).
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

import os
import json
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
_OPERATION_ID_RE = re.compile(r"^[0-9a-f]{64}$")
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


def _validated_args(args: list[str]) -> tuple[str, str, str | None]:
    if len(args) not in {2, 3}:
        _fail(
            "usage: blueprint-release <sha256:digest> <40-hex-revision> "
            "[64-hex-operation-id]"
        )
    digest, revision = args[:2]
    operation_id = args[2] if len(args) == 3 else None
    if not _DIGEST_RE.fullmatch(digest):
        _fail("first argument must match ^sha256:[0-9a-f]{64}$")
    if not _REVISION_RE.fullmatch(revision):
        _fail("second argument must match ^[0-9a-f]{40}$")
    if operation_id is not None and not _OPERATION_ID_RE.fullmatch(operation_id):
        _fail("third argument must match ^[0-9a-f]{64}$")
    return digest, revision, operation_id


def _release_command(
    digest: str,
    revision: str,
    operation_id: str | None = None,
    *,
    receipt_only: bool = False,
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
    if operation_id is not None:
        command.insert(4, "--pipe")
        command.extend(["--operation-id", operation_id])
    if receipt_only:
        command.append("--receipt-only")
    return command


def _run_release(
    digest: str,
    revision: str,
    operation_id: str | None = None,
    *,
    receipt_only: bool = False,
) -> int | dict[str, object]:
    result = subprocess.run(
        _release_command(
            digest,
            revision,
            operation_id,
            receipt_only=receipt_only,
        ),
        check=False,
        capture_output=operation_id is not None,
        text=operation_id is not None,
    )
    if operation_id is None:
        return result.returncode
    parsed: dict[str, object] = {"returncode": result.returncode}
    for line in ((result.stdout or "") + "\n" + (result.stderr or "")).splitlines():
        for prefix, key in (
            ("LOCAL_APP_RELEASE_RECEIPT ", "receipt"),
            ("LOCAL_APP_RELEASE_STATUS ", "status"),
        ):
            if not line.startswith(prefix):
                continue
            try:
                payload = json.loads(line.removeprefix(prefix))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                parsed[key] = payload
    return parsed


def _operation_response(
    result: int | dict[str, object],
    digest: str,
    revision: str,
    operation_id: str | None,
) -> bytes:
    if isinstance(result, int):
        return b"OK\n" if result == 0 else f"ERROR release exited {result}\n".encode("ascii")
    return_code = result.get("returncode")
    receipt = result.get("receipt")
    if isinstance(receipt, dict) and (
        receipt.get("operation_id") == operation_id
        and receipt.get("image_ref")
        == f"{_REGISTRY}/{_PROJECT}/{_REPOSITORY}/{_IMAGE}@{digest}"
        and receipt.get("revision") == revision
        and isinstance(receipt.get("outcome"), str)
    ):
        outcome = receipt["outcome"]
        verdict = "OK" if return_code == 0 and outcome == "success" else "TERMINAL"
        return f"{verdict} {operation_id} {digest} {revision} {outcome}\n".encode("ascii")
    status = result.get("status")
    if isinstance(status, dict) and (
        status.get("operation_id") == operation_id
        and status.get("image_ref")
        == f"{_REGISTRY}/{_PROJECT}/{_REPOSITORY}/{_IMAGE}@{digest}"
        and status.get("revision") == revision
        and status.get("status") in {"pending", "not_accepted"}
    ):
        return (
            f"{str(status['status']).upper()} {operation_id} {digest} {revision}\n"
        ).encode("ascii")
    return f"ERROR release exited {return_code}\n".encode("ascii")


def _request_args(payload: bytes) -> list[str]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError:
        _fail("request must contain ASCII arguments")
    return text.strip().split()


def _receive_request(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []
    remaining = _MAX_REQUEST_BYTES
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
        if b"\n" in chunk:
            break
    payload = b"".join(chunks)
    if not payload or b"\n" not in payload or payload.index(b"\n") != len(payload) - 1:
        _fail("request must be one newline-terminated frame within 256 bytes")
    return payload


def _serve_one_request() -> None:
    if os.geteuid() != 0:
        _fail("release socket service must run as root")
    listener = socket.fromfd(_SOCKET_FD, socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection, _ = listener.accept()
        with connection:
            payload = _receive_request(connection)
            request = _request_args(payload)
            if request and request[0] == "rehearse":
                # Candidate execution is never permitted on the production
                # host before qualification. Trusted CI owns build/smoke.
                connection.sendall(b"ERROR host rehearsal disabled\n")
                return
            receipt_only = bool(request and request[0] == "receipt")
            if receipt_only:
                request = request[1:]
            digest, revision, operation_id = _validated_args(request)
            if receipt_only and operation_id is None:
                _fail("receipt query requires an operation id")
            if operation_id is None and not receipt_only:
                return_code = _run_release(digest, revision)
            else:
                return_code = _run_release(
                    digest,
                    revision,
                    operation_id,
                    receipt_only=receipt_only,
                )
            try:
                connection.sendall(
                    _operation_response(
                        return_code,
                        digest,
                        revision,
                        operation_id,
                    )
                )
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
    if rehearsal:
        _fail("host rehearsal disabled; use trusted CI build/smoke evidence")
    args = sys.argv[2:] if rehearsal else sys.argv[1:]
    receipt_only = bool(args and args[0] == "--receipt-only")
    if receipt_only:
        args = args[1:]
    digest, revision, operation_id = _validated_args(args)
    if receipt_only and operation_id is None:
        _fail("receipt query requires an operation id")
    result = _run_release(
        digest,
        revision,
        operation_id,
        receipt_only=receipt_only,
    )
    response = _operation_response(result, digest, revision, operation_id)
    sys.stdout.buffer.write(response)
    return_code = result if isinstance(result, int) else result.get("returncode", 1)
    raise SystemExit(return_code if isinstance(return_code, int) else 1)


if __name__ == "__main__":
    main()

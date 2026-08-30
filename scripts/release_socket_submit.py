#!/usr/bin/env python3
"""Submit an activation request to the root release socket and await the verdict.

Runs ON the control-plane host as a member of the release socket's group.
scripts/blueprint.py pipes this file over SSH (``ssh <host> python3 - <digest>
<revision> [operation-id] < release_socket_submit.py``) so a workstation deploy needs neither
a host-side install nor a sudoers grant — the ACL-limited socket described in
docs/operations/one-command-deploy.md is the execution boundary.

The root service replies only after the release tool finishes (it runs the
activation under ``systemd-run --wait``), so this client holds the connection
open for up to VERDICT_TIMEOUT_SECONDS to return a deterministic verdict.

Exit codes:
  0 — release completed and the executor replied OK
  2 — validation failure, malformed verdict, or terminal executor error
  3 — handoff accepted but the verdict wait timed out (the root release may
      still be running; inspect the host receipt before retrying)
  4 — the socket was not reached, so no request was sent
  5 — an exact receipt query proved the operation was not accepted
"""

import re
import socket
import sys

SOCKET_PATH = "/run/blueprint-release.sock"
CONNECT_TIMEOUT_SECONDS = 60
VERDICT_TIMEOUT_SECONDS = 1800

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_OPERATION_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def main() -> int:
    args = sys.argv[1:]
    receipt_only = bool(args and args[0] == "--receipt-only")
    if receipt_only:
        args = args[1:]
    if len(args) not in {2, 3}:
        print(
            "usage: release_socket_submit.py [--receipt-only] <sha256-digest> "
            "<40-hex-revision> [64-hex-operation-id]",
            file=sys.stderr,
        )
        return 2
    digest, revision = args[:2]
    operation_id = args[2] if len(args) == 3 else None
    if not _DIGEST_RE.fullmatch(digest):
        print("ERROR release digest failed validation", file=sys.stderr)
        return 2
    if not _REVISION_RE.fullmatch(revision):
        print("ERROR release revision failed validation", file=sys.stderr)
        return 2
    if operation_id is not None and not _OPERATION_ID_RE.fullmatch(operation_id):
        print("ERROR release operation id failed validation", file=sys.stderr)
        return 2
    if receipt_only and operation_id is None:
        print("ERROR receipt query requires an operation id", file=sys.stderr)
        return 2

    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    handoff_started = False
    try:
        connection.settimeout(CONNECT_TIMEOUT_SECONDS)
        try:
            connection.connect(SOCKET_PATH)
            handoff_started = True
            request = " ".join(
                item
                for item in (
                    "receipt" if receipt_only else None,
                    digest,
                    revision,
                    operation_id,
                )
                if item is not None
            )
            connection.sendall(f"{request}\n".encode("ascii"))
            connection.shutdown(socket.SHUT_WR)
        except OSError as exc:
            if handoff_started:
                print(
                    f"Release executor handoff is ambiguous: {exc}",
                    file=sys.stderr,
                )
                return 3
            print(f"ERROR release executor socket unavailable: {exc}",
                  file=sys.stderr)
            return 4

        connection.settimeout(VERDICT_TIMEOUT_SECONDS)
        chunks = []
        remaining = 256
        try:
            while remaining:
                chunk = connection.recv(remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
                if b"\n" in chunk:
                    break
        except OSError:
            print("Release executor is still running; verdict wait expired "
                  "before it replied.", file=sys.stderr)
            return 3
    finally:
        connection.close()

    payload = b"".join(chunks)
    if payload and (b"\n" not in payload or payload.index(b"\n") != len(payload) - 1):
        print("ERROR release executor returned an unterminated or oversized verdict",
              file=sys.stderr)
        return 2
    try:
        response = payload.decode("ascii").strip()
    except UnicodeDecodeError:
        print("ERROR release executor returned a non-ASCII verdict", file=sys.stderr)
        return 2
    if operation_id is None and response == "OK":
        print("OK")
        return 0
    if not response and handoff_started:
        print("Release executor closed the connection without a verdict.",
              file=sys.stderr)
        return 3
    if operation_id is not None:
        parts = response.split()
        if len(parts) in {4, 5} and parts[1:4] == [
            operation_id,
            digest,
            revision,
        ]:
            if parts[0] == "OK" and len(parts) == 5 and parts[4] == "success":
                print(response)
                return 0
            if parts[0] == "PENDING" and len(parts) == 4:
                print(response, file=sys.stderr)
                return 3
            if parts[0] == "NOT_ACCEPTED" and len(parts) == 4:
                print(response, file=sys.stderr)
                return 5
            if parts[0] == "TERMINAL" and len(parts) == 5:
                print(response, file=sys.stderr)
                return 2
    print(response or "ERROR empty executor response", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

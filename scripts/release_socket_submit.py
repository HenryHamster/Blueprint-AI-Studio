#!/usr/bin/env python3
"""Submit an activation request to the root release socket and await the verdict.

Runs ON the control-plane host as a member of the release socket's group.
scripts/blueprint.py pipes this file over SSH (``ssh <host> python3 - <digest>
<revision> < release_socket_submit.py``) so a workstation deploy needs neither
a host-side install nor a sudoers grant — the ACL-limited socket described in
docs/operations/one-command-deploy.md is the execution boundary.

The root service replies only after the release tool finishes (it runs the
activation under ``systemd-run --wait``), so this client holds the connection
open for up to VERDICT_TIMEOUT_SECONDS to return a deterministic verdict.

Exit codes:
  0 — release completed and the executor replied OK
  2 — validation failure, socket unavailable, or the executor reported an error
  3 — handoff accepted but the verdict wait timed out (the root release may
      still be running; inspect the host receipt before retrying)
"""

import re
import socket
import sys

SOCKET_PATH = "/run/blueprint-release.sock"
CONNECT_TIMEOUT_SECONDS = 60
VERDICT_TIMEOUT_SECONDS = 1800

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: release_socket_submit.py <sha256-digest> <40-hex-revision>",
              file=sys.stderr)
        return 2
    digest, revision = sys.argv[1], sys.argv[2]
    if not _DIGEST_RE.fullmatch(digest):
        print("ERROR release digest failed validation", file=sys.stderr)
        return 2
    if not _REVISION_RE.fullmatch(revision):
        print("ERROR release revision failed validation", file=sys.stderr)
        return 2

    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    request_sent = False
    try:
        connection.settimeout(CONNECT_TIMEOUT_SECONDS)
        try:
            connection.connect(SOCKET_PATH)
            connection.sendall(f"{digest} {revision}\n".encode("ascii"))
            request_sent = True
            connection.shutdown(socket.SHUT_WR)
        except OSError as exc:
            print(f"ERROR release executor socket unavailable: {exc}",
                  file=sys.stderr)
            return 2

        connection.settimeout(VERDICT_TIMEOUT_SECONDS)
        chunks = []
        try:
            while True:
                chunk = connection.recv(256)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
        except OSError:
            print("Release executor is still running; verdict wait expired "
                  "before it replied.", file=sys.stderr)
            return 3
    finally:
        connection.close()

    response = b"".join(chunks).decode("ascii", errors="replace").strip()
    if response == "OK":
        print("OK")
        return 0
    if not response and request_sent:
        print("Release executor closed the connection without a verdict.",
              file=sys.stderr)
        return 3
    print(response or "ERROR empty executor response", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

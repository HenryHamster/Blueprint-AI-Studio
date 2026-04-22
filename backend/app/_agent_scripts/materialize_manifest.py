"""Materialize the agent credential manifest on a fresh VM.

Invoked by the VM startup script (see compute.py) as:

    python3 /tmp/materialize_manifest.py <manifest.json> <env.out>

Reads the manifest JSON produced by build_agent_secret_manifest,
writes the env-dict as a systemd-compatible env file, and drops any
``files`` entries onto disk with the right owner + mode.

Lives as a real .py file rather than an f-string heredoc so escape
semantics are unambiguous — the 0bcfd031 / c94d6d9b post-mortem traced
a multi-hour outage to Python f-string interpolation mangling the
inline heredoc's backslash escapes, causing SyntaxError on every VM.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys


def _lookup_uid_gid(user: str) -> tuple[int, int]:
    """Look up ``user``'s primary uid/gid by reading /etc/passwd and
    /etc/group directly.

    Avoids pwd.getpwnam / grp.getgrnam, which go through NSS — a broken
    NSS source (LDAP, sss, systemd-resolved) can block for many minutes
    with no exception. Flat-file reads can't hang.
    """
    uid: int | None = None
    primary_gid: int | None = None
    with open("/etc/passwd", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith(user + ":"):
                parts = line.rstrip("\n").split(":")
                uid = int(parts[2])
                primary_gid = int(parts[3])
                break
    if uid is None or primary_gid is None:
        raise RuntimeError(f"user {user!r} missing from /etc/passwd")

    gid = primary_gid
    with open("/etc/group", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith(user + ":"):
                parts = line.rstrip("\n").split(":")
                gid = int(parts[2])
                break
    return uid, gid


def _env_file_escape(value: object) -> str:
    """Escape a value for a systemd EnvironmentFile entry.

    systemd's env file format accepts ``KEY="value"`` with backslash
    escapes for ``"``, ``\\``, and newlines.
    """
    s = str(value)
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    s = s.replace("\n", "\\n")
    return s


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <manifest.json> <env.out>", file=sys.stderr)
        return 2
    manifest_path, env_path = argv[1], argv[2]

    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    # 1. Write env file
    env = manifest.get("env") or {}
    with open(env_path, "w", encoding="utf-8") as env_file:
        for key, value in env.items():
            if value is None:
                continue
            env_file.write(f'{key}="{_env_file_escape(value)}"\n')
    os.chmod(env_path, 0o600)

    # 2. Write any auth/config files with the right owner/mode
    files = manifest.get("files") or []
    if files:
        uid, gid = _lookup_uid_gid("agent")
        for item in files:
            path = pathlib.Path(item["path"])
            # Parent dirs created by mkdir here are owned by root (this
            # script runs as root in the VM startup). If the CLI later
            # runs as `agent` and writes siblings into the same dir —
            # e.g. gemini CLI drops ~/.gemini/projects.json next to our
            # ~/.gemini/settings.json — it hits EACCES on the root-owned
            # 0755 dir. Chown every directory we create so the CLI owns
            # its config dir.
            missing_parents: list[pathlib.Path] = []
            ancestor = path.parent
            while not ancestor.exists():
                missing_parents.append(ancestor)
                ancestor = ancestor.parent
            path.parent.mkdir(parents=True, exist_ok=True)
            for created in missing_parents:
                os.chown(created, uid, gid)
            path.write_text(item.get("content", "") or "", encoding="utf-8")
            os.chmod(path, int(item.get("mode", "0600"), 8))
            os.chown(path, uid, gid)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

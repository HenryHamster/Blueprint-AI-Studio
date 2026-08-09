#!/usr/bin/env python3
"""Enable the typed-Fast Resident attestation on a blueprint-node host (run as root).

Interim enablement for control planes whose installed app-release bundle predates
the CALL_FOREGROUND_QUALIFICATION allowlist entry in ``local_api_service.py``
(this repo's copy already has it). Mirrors that allowlist entry onto every
installed launcher bundle and sets the attestation in each API role's secrets
bundle, leaving ``.bak-fastlane`` backups of every file it touches. Idempotent.

The backend fail-closes unless the value exactly matches its pinned
qualification string, so this cannot activate anything other than the intended
typed-Fast lane. Hand-edits made here are superseded by the next app-release
install, which ships an allowlist that already contains the key.

After a successful run, restart the API: ``systemctl restart blueprint-api``.
"""

from __future__ import annotations

import glob
import json
import shutil
import sys

_KEY = "CALL_FOREGROUND_QUALIFICATION"
_VALUE = "claude-warm-stream-json:phase0-v1"
_ANCHOR = '"ADMIN_API_KEY_ALLOWED_IPS",'
_LAUNCHER_GLOB = "/usr/local/lib/blueprint-node/app-release-*/local_api_service.py"
_ROLE_ENV_GLOB = "/etc/blueprint/nodes/*/roles/api/role.env"


def _patch_launcher(path: str) -> None:
    src = open(path, encoding="utf-8").read()
    if _KEY in src:
        print(f"launcher {path}: {_KEY} already allowlisted")
        return
    if _ANCHOR not in src:
        raise SystemExit(f"launcher {path}: anchor {_ANCHOR!r} not found — aborting")
    shutil.copy2(path, path + ".bak-fastlane")
    src = src.replace(_ANCHOR, _ANCHOR + f'\n        "{_KEY}",', 1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src)
    print(f"launcher {path}: added {_KEY} (backup: .bak-fastlane)")


def _secrets_path(role_env: str) -> str:
    for line in open(role_env, encoding="utf-8"):
        line = line.strip()
        if line.startswith("BLUEPRINT_API_SECRETS_FILE") and "=" in line:
            return line.partition("=")[2].strip().strip('"').strip("'")
    raise SystemExit(f"{role_env}: BLUEPRINT_API_SECRETS_FILE not found")


def _patch_secrets(path: str) -> None:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get(_KEY) == _VALUE:
        print(f"secrets {path}: {_KEY} already set")
        return
    shutil.copy2(path, path + ".bak-fastlane")
    data[_KEY] = _VALUE
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    print(f"secrets {path}: set {_KEY} (backup: .bak-fastlane)")


def main() -> int:
    launchers = sorted(glob.glob(_LAUNCHER_GLOB))
    role_envs = sorted(glob.glob(_ROLE_ENV_GLOB))
    if not launchers or not role_envs:
        print("no installed API launcher or API role found", file=sys.stderr)
        return 1
    for launcher in launchers:
        _patch_launcher(launcher)
    for role_env in role_envs:
        _patch_secrets(_secrets_path(role_env))
    print("OK — restart the API: systemctl restart blueprint-api")
    return 0


if __name__ == "__main__":
    sys.exit(main())

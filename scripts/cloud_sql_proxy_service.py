#!/usr/bin/env python3
"""Run Cloud SQL Auth Proxy on a private Docker bridge for a local worker.

The proxy is deliberately not bound to loopback or every host interface.  The
worker runs in a container, so this service binds only to the gateway address of
an un-published Docker bridge.  Nothing is exposed on the host's LAN interfaces.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

_INSTANCE_RE = re.compile(r"^[a-z][a-z0-9-]{4,61}[a-z0-9]:[a-z0-9-]+:[a-z][a-z0-9-]{0,61}[a-z0-9]$")
_NETWORK_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{name} contains control characters")
    return value


def _secure_file(value: str, *, executable: bool) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{path} must be an absolute path")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{path} must be a regular, non-symlink file")
    if info.st_uid != os.getuid():
        raise ValueError(f"{path} must be owned by uid {os.getuid()}")
    if not executable and info.st_mode & 0o077:
        raise ValueError(f"{path} must not be accessible by group or other users")
    if executable and info.st_mode & 0o022:
        raise ValueError(f"{path} must not be writable by group or other users")
    if executable and not os.access(path, os.X_OK):
        raise ValueError(f"{path} is not executable")
    return path


def _validate_credentials(path: Path, *, project_id: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Cloud SQL credentials are not valid JSON") from exc
    expected_email = f"blueprint-local-worker@{project_id}.iam.gserviceaccount.com"
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "service_account"
        or payload.get("project_id") != project_id
        or payload.get("client_email") != expected_email
    ):
        raise ValueError(
            "Cloud SQL credentials are not the dedicated proxy-only service account"
        )


def _validate_binary_digest(path: Path, expected: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("BLUEPRINT_CLOUDSQL_PROXY_SHA256 must be a lowercase SHA-256")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise ValueError("Cloud SQL proxy binary SHA-256 does not match the pinned digest")


def _docker_network_gateway(network: str) -> str:
    if not _NETWORK_RE.fullmatch(network):
        raise ValueError("BLUEPRINT_CONTROL_PLANE_NETWORK is not a safe Docker network name")
    inspect = subprocess.run(
        ["docker", "network", "inspect", network],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode != 0:
        create = subprocess.run(
            [
                "docker",
                "network",
                "create",
                "--label",
                "dev.blueprint.purpose=local-control-plane",
                network,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if create.returncode != 0:
            raise RuntimeError(
                f"could not create Docker network {network}: {(create.stderr or '').strip()}"
            )
        inspect = subprocess.run(
            ["docker", "network", "inspect", network],
            capture_output=True,
            text=True,
            check=False,
        )
    if inspect.returncode != 0:
        raise RuntimeError(
            f"could not inspect Docker network {network}: {(inspect.stderr or '').strip()}"
        )
    try:
        payload = json.loads(inspect.stdout)
        network_payload = payload[0]
        labels = network_payload.get("Labels") or {}
        if labels.get("dev.blueprint.purpose") != "local-control-plane":
            raise RuntimeError(
                f"Docker network {network} is not owned by the Blueprint control plane"
            )
        gateway = network_payload["IPAM"]["Config"][0]["Gateway"]
        parsed = ipaddress.ip_address(gateway)
    except RuntimeError:
        raise
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Docker network {network} has no valid gateway") from exc
    if not parsed.is_private:
        raise RuntimeError(f"refusing non-private Docker gateway {parsed}")
    return str(parsed)


def command() -> list[str]:
    binary = _secure_file(_required_env("BLUEPRINT_CLOUDSQL_PROXY_BINARY"), executable=True)
    _validate_binary_digest(
        binary, _required_env("BLUEPRINT_CLOUDSQL_PROXY_SHA256")
    )
    credentials = _secure_file(
        _required_env("BLUEPRINT_CLOUDSQL_CREDENTIALS_FILE"), executable=False
    )
    instance = _required_env("BLUEPRINT_CLOUDSQL_INSTANCE")
    if not _INSTANCE_RE.fullmatch(instance):
        raise ValueError("BLUEPRINT_CLOUDSQL_INSTANCE is not a valid instance connection name")
    project_id = instance.split(":", 1)[0]
    _validate_credentials(credentials, project_id=project_id)
    network = _required_env("BLUEPRINT_CONTROL_PLANE_NETWORK")
    gateway = _docker_network_gateway(network)
    return [
        str(binary),
        "--address",
        gateway,
        "--port",
        "5433",
        "--credentials-file",
        str(credentials),
        "--structured-logs",
        instance,
    ]


def main(argv: list[str] | None = None) -> int:
    check_only = (argv if argv is not None else sys.argv[1:]) == ["--check"]
    try:
        resolved = command()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: Cloud SQL proxy preflight failed: {exc}", file=sys.stderr)
        return 1
    if check_only:
        print("Cloud SQL proxy preflight passed.")
        return 0
    os.execv(resolved[0], resolved)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

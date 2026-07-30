#!/usr/bin/env python3
"""Run the API-only Blueprint control plane in a hardened local container."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import local_worker_service as worker

_CONTAINER_NAME = "blueprint-local-api"
_ROLE_LABEL = "com.blueprint.role"
_ROLE = "api"
_REQUIRED_API_SECRETS = frozenset(
    {
        "DATABASE_URL",
        "AGENT_JWT_SECRET",
        "WS_TICKET_SECRET",
        "GITHUB_APP_ID",
        "GITHUB_CLIENT_ID",
        "GITHUB_CLIENT_SECRET",
        "GITHUB_PRIVATE_KEY",
        "GITHUB_WEBHOOK_SECRET",
        "GITHUB_PAT_ENCRYPTION_KEY",
        "ADMIN_API_KEY",
        "METRICS_TOKEN",
    }
)
_REQUIRED_API_CONFIG = frozenset(
    {
        "GCP_PROJECT_ID",
        "ARTIFACT_BUCKET_NAME",
        "ORCHESTRATOR_URL",
        "ALLOWED_ORIGINS",
    }
)
_OPTIONAL_API_CONFIG = frozenset(
    {
        "INITIAL_ADMIN_EMAIL",
        "ADMIN_JWT_SECRET",
        "ADMIN_JWT_EXPIRE_MINUTES",
        "ADMIN_API_KEY_ALLOWED_IPS",
    }
)
_ALLOWED_API_ENV = (
    _REQUIRED_API_SECRETS | _REQUIRED_API_CONFIG | _OPTIONAL_API_CONFIG
)


def _load_api_secrets(path_text: str) -> dict[str, str]:
    """Load only the secrets and configuration used by the public API."""

    path = Path(path_text)
    if not path.is_absolute():
        raise ValueError("BLUEPRINT_API_SECRETS_FILE must be absolute")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("API secrets must be a regular, non-symlink file")
    if info.st_uid != os.getuid():
        raise ValueError(f"API secrets must be owned by uid {os.getuid()}")
    if info.st_mode & 0o077:
        raise ValueError("API secrets must have mode 0600 or stricter")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("API secrets are not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("API secrets must be a JSON object")
    unknown = set(payload) - _ALLOWED_API_ENV
    if unknown:
        raise ValueError(
            "API secrets contain unsupported keys: "
            + ", ".join(sorted(unknown))
        )
    missing = (_REQUIRED_API_SECRETS | _REQUIRED_API_CONFIG) - set(payload)
    if missing:
        raise ValueError(
            "API secrets are missing: " + ", ".join(sorted(missing))
        )
    if any(not isinstance(value, str) or not value for value in payload.values()):
        raise ValueError("every API secret and configuration value must be a non-empty string")
    for name, value in payload.items():
        allowed_controls = {"\n"} if name == "GITHUB_PRIVATE_KEY" else set()
        if any(
            (ord(char) < 32 or ord(char) == 127) and char not in allowed_controls
            for char in value
        ):
            raise ValueError(f"{name} contains unsupported control characters")

    private_key = payload["GITHUB_PRIVATE_KEY"].strip()
    if not (
        private_key.startswith("-----BEGIN ")
        and private_key.endswith("PRIVATE KEY-----")
    ):
        raise ValueError("GITHUB_PRIVATE_KEY must be a PEM private key")
    for name in ("AGENT_JWT_SECRET", "WS_TICKET_SECRET"):
        if len(payload[name].encode()) < 32:
            raise ValueError(f"{name} must be at least 32 bytes")
    project_id = payload["GCP_PROJECT_ID"]
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project_id):
        raise ValueError("GCP_PROJECT_ID is invalid")
    if payload["ARTIFACT_BUCKET_NAME"] != f"{project_id}-agent-artifacts":
        raise ValueError("ARTIFACT_BUCKET_NAME does not match GCP_PROJECT_ID")
    if not payload["ORCHESTRATOR_URL"].startswith("https://"):
        raise ValueError("ORCHESTRATOR_URL must use HTTPS")
    return payload


def _api_port() -> int:
    raw = worker._required_env("BLUEPRINT_API_PORT")
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError("BLUEPRINT_API_PORT must be an integer") from exc
    if port < 1024 or port > 65535:
        raise ValueError("BLUEPRINT_API_PORT must be between 1024 and 65535")
    return port


def _assert_container_slot(image: str, network: str) -> None:
    """Refuse stale or foreign state at the API container's fixed name."""

    result = subprocess.run(
        ["docker", "container", "inspect", _CONTAINER_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        diagnostic = f"{result.stdout}\n{result.stderr}".lower()
        if "no such object" in diagnostic or "no such container" in diagnostic:
            return
        raise RuntimeError("could not verify the local API container slot")
    try:
        records = json.loads(result.stdout)
        record = records[0]
        configured_image = record["Config"]["Image"]
        actual_image = record["Image"]
        labels = record["Config"].get("Labels") or {}
        networks = record["NetworkSettings"].get("Networks") or {}
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("existing local API container metadata is invalid") from exc
    if (
        configured_image != image
        or actual_image != image
        or labels.get(_ROLE_LABEL) != _ROLE
        or network not in networks
    ):
        raise RuntimeError(
            "existing blueprint-local-api container does not match the reviewed "
            "image, API role, and private network"
        )
    raise RuntimeError("matching blueprint-local-api container already exists")


def command() -> tuple[list[str], dict[str, str]]:
    worker._assert_no_host_ci_runner()
    image = worker._required_env("BLUEPRINT_API_IMAGE_ID")
    if not worker._IMAGE_ID_RE.fullmatch(image):
        raise ValueError("BLUEPRINT_API_IMAGE_ID must be an immutable sha256 image ID")
    database_mode = worker._database_mode()
    network = worker._required_env("BLUEPRINT_CONTROL_PLANE_NETWORK")
    host_port = _api_port()
    secrets = _load_api_secrets(worker._required_env("BLUEPRINT_API_SECRETS_FILE"))
    google_credentials = worker._secure_credentials_file(
        worker._required_env("BLUEPRINT_API_GOOGLE_CREDENTIALS_FILE"),
        project_id=secrets["GCP_PROJECT_ID"],
        expected_account_id="blueprint-local-api-runtime",
        credential_label="API",
    )
    secrets["DATABASE_URL"] = worker._database_url_for_container(
        secrets["DATABASE_URL"], mode=database_mode
    )

    gateway = worker._network_gateway(network)
    if database_mode == "cloudsql-proxy":
        worker._wait_for_proxy(gateway)
        worker._assert_proxy_reachable_from_network(image, network, gateway)
    _assert_container_slot(image, network)

    environment = os.environ.copy()
    environment.update(secrets)
    environment.pop("K_REVISION", None)
    environment.pop("ALLOW_PLAINTEXT_CREDENTIALS", None)
    environment.update(
        {
            # Backend lifecycle treats api as categorically ineligible for the
            # advisory-lock leader path and all singleton background services.
            "BLUEPRINT_PROCESS_ROLE": _ROLE,
            "BLUEPRINT_DEPLOYMENT_MODE": "local-production",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": "/tmp",
            "PORT": "8080",
            "GOOGLE_APPLICATION_CREDENTIALS": "/var/run/blueprint/gcp.json",
        }
    )
    forwarded = sorted(
        _ALLOWED_API_ENV
        | {
            "BLUEPRINT_PROCESS_ROLE",
            "BLUEPRINT_DEPLOYMENT_MODE",
            "PYTHONDONTWRITEBYTECODE",
            "HOME",
            "PORT",
            "GOOGLE_APPLICATION_CREDENTIALS",
        }
    )
    argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        _CONTAINER_NAME,
        "--label",
        f"{_ROLE_LABEL}={_ROLE}",
        "--network",
        network,
        "--publish",
        f"127.0.0.1:{host_port}:8080",
        "--pull",
        "never",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=512m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--mount",
        f"type=bind,src={google_credentials},dst=/var/run/blueprint/gcp.json,readonly",
        "--pids-limit",
        "512",
        "--memory",
        "4g",
        "--memory-swap",
        "4g",
        "--cpus",
        "4",
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=10m",
        "--log-opt",
        "max-file=3",
    ]
    if database_mode == "cloudsql-proxy":
        argv.extend(["--add-host", f"host.docker.internal:{gateway}"])
    for name in forwarded:
        if name in environment:
            argv.extend(["--env", name])
    argv.append(image)
    return argv, environment


def main(argv: list[str] | None = None) -> int:
    check_only = (argv if argv is not None else sys.argv[1:]) == ["--check"]
    try:
        resolved, environment = command()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: local API preflight failed: {exc}", file=sys.stderr)
        return 1
    if check_only:
        print("Local API preflight passed.")
        return 0
    os.execvpe(resolved[0], resolved, environment)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run the background-only Blueprint control plane in a hardened container."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit

_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_NETWORK_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
_DATABASE_MODES = frozenset({"cloudsql-proxy", "local-postgres"})
_LOCAL_POSTGRES_ALIAS = "blueprint-postgres"
_REQUIRED_SECRETS = frozenset(
    {
        "DATABASE_URL",
        "AGENT_JWT_SECRET",
        "WS_TICKET_SECRET",
        "GITHUB_APP_ID",
        "GITHUB_PRIVATE_KEY",
        "GITHUB_PAT_ENCRYPTION_KEY",
    }
)
_OPTIONAL_SECRETS = frozenset()
_REQUIRED_RUNTIME_CONFIG = frozenset(
    {
        "GCP_PROJECT_ID",
        "GCP_ZONE",
        "VPC_NETWORK",
        "VPC_SUBNET",
        "AGENT_SERVICE_ACCOUNT_EMAIL",
        "ARTIFACT_BUCKET_NAME",
        "ORCHESTRATOR_URL",
        "LOCAL_MODE",
        "MAX_CONCURRENT_VMS",
        "MAX_PROVISION_SLOTS",
        "MACHINE_TYPE",
        "VM_PROVISIONING_MODEL",
        "DISABLED_REGIONS",
        "CLOUD_RUN_JOB_NAME",
    }
)
_OPTIONAL_RUNTIME_CONFIG = frozenset(
    {
        "COMPUTE_IMAGE_BUILDER_SA",
        "COMPUTE_IMAGE_BUILD_LOCATION",
        "COMPUTE_IMAGE_BUILD_SOURCE_BUCKET",
    }
)
_ALLOWED_SECRETS = (
    _REQUIRED_SECRETS
    | _OPTIONAL_SECRETS
    | _REQUIRED_RUNTIME_CONFIG
    | _OPTIONAL_RUNTIME_CONFIG
)
_CONTAINER_NAME = "blueprint-local-worker"
_CI_RUNNER_MARKERS = (
    b"Runner.Listener",
    b"Runner.Worker",
    b"actions-runner",
    b"github-actions-blueprint",
)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{name} contains control characters")
    return value


def _assert_no_host_ci_runner() -> None:
    """Refuse to colocate production credentials with host-level CI code.

    A GitHub Actions runner that can reach the host Docker daemon can become
    root-equivalent and inspect any container or credential on that machine.
    A VM boundary is required; a second Unix account is not sufficient.
    """

    proc_root = Path("/proc")
    if not proc_root.is_dir():  # pragma: no cover - production service is Linux-only.
        raise RuntimeError("cannot verify host CI isolation without /proc")
    own_pid = os.getpid()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == own_pid:
            continue
        try:
            command = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if any(marker in command for marker in _CI_RUNNER_MARKERS):
            raise RuntimeError(
                "host-level GitHub Actions runner detected; move CI into a VM "
                "before starting the production worker"
            )


def _load_secrets(path_text: str) -> dict[str, str]:
    path = Path(path_text)
    if not path.is_absolute():
        raise ValueError("BLUEPRINT_WORKER_SECRETS_FILE must be absolute")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("worker secrets must be a regular, non-symlink file")
    if info.st_uid != os.getuid():
        raise ValueError(f"worker secrets must be owned by uid {os.getuid()}")
    if info.st_mode & 0o077:
        raise ValueError("worker secrets must have mode 0600 or stricter")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("worker secrets are not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("worker secrets must be a JSON object")
    unknown = set(payload) - _ALLOWED_SECRETS
    if unknown:
        raise ValueError(f"worker secrets contain unsupported keys: {', '.join(sorted(unknown))}")
    missing = (_REQUIRED_SECRETS | _REQUIRED_RUNTIME_CONFIG) - set(payload)
    if missing:
        raise ValueError(f"worker secrets are missing: {', '.join(sorted(missing))}")
    if any(not isinstance(value, str) or not value for value in payload.values()):
        raise ValueError("every worker secret must be a non-empty string")
    for name, value in payload.items():
        allowed_controls = {"\n"} if name == "GITHUB_PRIVATE_KEY" else set()
        if any(
            (ord(char) < 32 or ord(char) == 127) and char not in allowed_controls
            for char in value
        ):
            raise ValueError(
                f"{name} contains unsupported control characters"
            )
    private_key = payload["GITHUB_PRIVATE_KEY"].strip()
    if not (
        private_key.startswith("-----BEGIN ")
        and private_key.endswith("PRIVATE KEY-----")
    ):
        raise ValueError("GITHUB_PRIVATE_KEY must be a PEM private key")
    if len(payload["AGENT_JWT_SECRET"].encode()) < 32:
        raise ValueError("AGENT_JWT_SECRET must be at least 32 bytes")
    if len(payload["WS_TICKET_SECRET"].encode()) < 32:
        raise ValueError("WS_TICKET_SECRET must be at least 32 bytes")
    project_id = payload["GCP_PROJECT_ID"]
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project_id):
        raise ValueError("GCP_PROJECT_ID is invalid")
    if payload["LOCAL_MODE"].lower() != "false":
        raise ValueError("LOCAL_MODE must be false for the production worker")
    if payload["VM_PROVISIONING_MODEL"] not in {"STANDARD", "SPOT"}:
        raise ValueError("VM_PROVISIONING_MODEL must be STANDARD or SPOT")
    for name in ("MAX_CONCURRENT_VMS", "MAX_PROVISION_SLOTS"):
        try:
            value = int(payload[name])
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if value < 0 or value > 100:
            raise ValueError(f"{name} must be between 0 and 100")
    expected_agent_sa = f"agent-naked-sa@{project_id}.iam.gserviceaccount.com"
    if payload["AGENT_SERVICE_ACCOUNT_EMAIL"] != expected_agent_sa:
        raise ValueError("AGENT_SERVICE_ACCOUNT_EMAIL does not match GCP_PROJECT_ID")
    expected_job = (
        f"projects/{project_id}/locations/us-central1/jobs/blueprint-agent-job"
    )
    if payload["CLOUD_RUN_JOB_NAME"] != expected_job:
        raise ValueError("CLOUD_RUN_JOB_NAME is not the expected agent job")
    if not payload["ORCHESTRATOR_URL"].startswith("https://"):
        raise ValueError("ORCHESTRATOR_URL must use HTTPS")
    return payload


def _secure_credentials_file(
    path_text: str,
    *,
    project_id: str,
    expected_account_id: str,
    credential_label: str,
) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        raise ValueError(f"{credential_label} Google credentials path must be absolute")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(
            f"{credential_label} Google credentials must be a regular, non-symlink file"
        )
    if info.st_uid != os.getuid():
        raise ValueError(
            f"{credential_label} Google credentials must be owned by uid {os.getuid()}"
        )
    if info.st_mode & 0o077:
        raise ValueError(
            f"{credential_label} Google credentials must have mode 0600 or stricter"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"{credential_label} Google credentials are not valid JSON"
        ) from exc
    expected_email = f"{expected_account_id}@{project_id}.iam.gserviceaccount.com"
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "service_account"
        or payload.get("project_id") != project_id
        or payload.get("client_email") != expected_email
    ):
        raise ValueError(
            f"{credential_label} Google credentials are not the dedicated "
            f"{expected_account_id} service account"
        )
    return path


def _database_mode() -> str:
    mode = _required_env("BLUEPRINT_DATABASE_MODE")
    if mode not in _DATABASE_MODES:
        allowed = ", ".join(sorted(_DATABASE_MODES))
        raise ValueError(f"BLUEPRINT_DATABASE_MODE must be one of: {allowed}")
    return mode


def _database_url_for_container(value: str, *, mode: str) -> str:
    if mode not in _DATABASE_MODES:
        raise ValueError("database mode must be explicit")
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgresql", "postgresql+asyncpg"}:
        raise ValueError("DATABASE_URL must use PostgreSQL")
    if not parsed.username or parsed.password is None or not parsed.path.strip("/"):
        raise ValueError("DATABASE_URL must include user, password, and database")
    userinfo = parsed.netloc.rsplit("@", 1)[0]
    host, port = (
        ("host.docker.internal", 5433)
        if mode == "cloudsql-proxy"
        else (_LOCAL_POSTGRES_ALIAS, 5432)
    )
    netloc = f"{userinfo}@{host}:{port}"
    # Both database transports are confined to the private Docker bridge. The
    # Cloud SQL Auth Proxy encrypts its remote leg, while local Postgres never
    # leaves the host. Preserve unrelated query parameters, remove every
    # existing SSL spelling, and disable TLS only for this private hop.
    kept_query = [
        pair
        for pair in parsed.query.split("&")
        if pair and pair.partition("=")[0].lower() not in {"ssl", "sslmode"}
    ]
    kept_query.append("ssl=disable")
    return urlunsplit(
        SplitResult(
            parsed.scheme,
            netloc,
            parsed.path,
            "&".join(kept_query),
            parsed.fragment,
        )
    )


def _network_gateway(network: str) -> str:
    if not _NETWORK_RE.fullmatch(network):
        raise ValueError("BLUEPRINT_CONTROL_PLANE_NETWORK is not a safe Docker network name")
    result = subprocess.run(
        [
            "docker",
            "network",
            "inspect",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
            network,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Docker network {network} is unavailable")
    gateway = result.stdout.strip()
    try:
        parsed = ipaddress.ip_address(gateway)
    except ValueError as exc:
        raise RuntimeError(f"Docker network {network} has no valid gateway") from exc
    if not parsed.is_private:
        raise RuntimeError(f"refusing non-private Docker gateway {parsed}")
    return str(parsed)


def _wait_for_proxy(gateway: str, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((gateway, 5433), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("Cloud SQL proxy did not accept connections within 30 seconds")


def _assert_proxy_reachable_from_network(
    image: str, network: str, gateway: str
) -> None:
    """Prove the worker's Docker network can cross the host firewall.

    A host-side socket probe is insufficient on UFW-managed machines: the host
    can connect to its own bridge address while packets arriving from a
    container are denied.  Run a credential-free, bounded TCP probe in the
    pinned worker image and the exact private network before loading any
    production secrets into a container.
    """

    probe = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            "--add-host",
            f"host.docker.internal:{gateway}",
            "--pull",
            "never",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "32",
            "--memory",
            "64m",
            "--cpus",
            "0.25",
            "--entrypoint",
            "python",
            image,
            "-c",
            (
                "import socket;"
                "socket.create_connection(('host.docker.internal',5433),5).close()"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if probe.returncode != 0:
        raise RuntimeError(
            "Cloud SQL proxy is unreachable from the private Docker network; "
            f"allow TCP from its subnet to {gateway}:5433 in the host firewall"
        )


def command() -> tuple[list[str], dict[str, str]]:
    _assert_no_host_ci_runner()
    image = _required_env("BLUEPRINT_WORKER_IMAGE_ID")
    if not _IMAGE_ID_RE.fullmatch(image):
        raise ValueError("BLUEPRINT_WORKER_IMAGE_ID must be an immutable sha256 image ID")
    database_mode = _database_mode()
    network = _required_env("BLUEPRINT_CONTROL_PLANE_NETWORK")
    secrets = _load_secrets(_required_env("BLUEPRINT_WORKER_SECRETS_FILE"))
    google_credentials = _secure_credentials_file(
        _required_env("BLUEPRINT_WORKER_GOOGLE_CREDENTIALS_FILE"),
        project_id=secrets["GCP_PROJECT_ID"],
        expected_account_id="blueprint-local-worker-runtime",
        credential_label="worker",
    )
    secrets["DATABASE_URL"] = _database_url_for_container(
        secrets["DATABASE_URL"], mode=database_mode
    )
    gateway = _network_gateway(network)
    if database_mode == "cloudsql-proxy":
        _wait_for_proxy(gateway)
        _assert_proxy_reachable_from_network(image, network, gateway)

    environment = os.environ.copy()
    environment.update(secrets)
    environment.pop("K_REVISION", None)
    environment.pop("ALLOW_PLAINTEXT_CREDENTIALS", None)
    environment.update(
        {
            "BLUEPRINT_PROCESS_ROLE": "worker",
            "BLUEPRINT_DEPLOYMENT_MODE": "local-production",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": "/tmp",
            "PORT": "8080",
            "GOOGLE_APPLICATION_CREDENTIALS": "/var/run/blueprint/gcp.json",
        }
    )
    forwarded = sorted(
        _ALLOWED_SECRETS
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
        "--network",
        network,
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
        "--cpus",
        "4",
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
        print(f"ERROR: local worker preflight failed: {exc}", file=sys.stderr)
        return 1
    if check_only:
        print("Local worker preflight passed.")
        return 0
    os.execvpe(resolved[0], resolved, environment)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

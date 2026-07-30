#!/usr/bin/env python3
"""Manage the private PostgreSQL container used by the local control plane.

The service intentionally exposes no host port.  Other Blueprint containers
reach it only as ``blueprint-postgres`` on the owned control-plane bridge.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

_CONTAINER_NAME = "blueprint-local-postgres"
_NETWORK_ALIAS = "blueprint-postgres"
_NETWORK_LABEL = "dev.blueprint.purpose"
_NETWORK_LABEL_VALUE = "local-control-plane"
_SERVICE_LABEL = "dev.blueprint.service"
_SERVICE_LABEL_VALUE = "local-postgres"
_DATA_TARGET = "/var/lib/postgresql/data"
_PASSWORD_TARGET = "/run/secrets/postgres_password"
_POSTGRES_USER = "blueprint"
_POSTGRES_DB = "blueprint"
_POSTGRES_UID = 999
_POSTGRES_GID = 999
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_DIGEST_RE = re.compile(
    r"^(?:(?:docker\.io/)?library/)?postgres:15@sha256:[0-9a-f]{64}$"
)
_NETWORK_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{name} contains control characters")
    return value


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
    )


def _error(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "").strip()


def _load_single_inspect(result: subprocess.CompletedProcess[str], subject: str) -> dict[str, Any]:
    if result.returncode != 0:
        raise RuntimeError(f"could not inspect {subject}: {_error(result)}")
    try:
        payload = json.loads(result.stdout)
        item = payload[0]
    except (IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Docker returned invalid inspect data for {subject}") from exc
    if not isinstance(item, dict):
        raise RuntimeError(f"Docker returned invalid inspect data for {subject}")
    return item


def _validate_image_input(value: str) -> None:
    if not (_IMAGE_ID_RE.fullmatch(value) or _IMAGE_DIGEST_RE.fullmatch(value)):
        raise ValueError(
            "BLUEPRINT_POSTGRES_IMAGE_ID must be a pinned sha256 image ID or "
            "postgres:15@sha256 digest"
        )


def _inspect_image(image: str) -> str:
    _validate_image_input(image)
    payload = _load_single_inspect(
        _run(["docker", "image", "inspect", image]),
        "the pinned PostgreSQL image",
    )
    image_id = payload.get("Id")
    if not isinstance(image_id, str) or not _IMAGE_ID_RE.fullmatch(image_id):
        raise RuntimeError("pinned PostgreSQL image has no immutable image ID")
    if _IMAGE_ID_RE.fullmatch(image) and image_id != image:
        raise RuntimeError("Docker resolved a different PostgreSQL image ID")
    env = (payload.get("Config") or {}).get("Env") or []
    if "PG_MAJOR=15" not in env:
        raise RuntimeError("pinned image is not PostgreSQL major version 15")
    if _IMAGE_DIGEST_RE.fullmatch(image):
        requested_digest = image.rsplit("@", 1)[1]
        repo_digests = payload.get("RepoDigests") or []
        if not any(
            isinstance(item, str)
            and item.rsplit("@", 1)[-1] == requested_digest
            for item in repo_digests
        ):
            raise RuntimeError("Docker image does not carry the requested postgres digest")
    identity = _run(
        [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "16",
            "--memory",
            "64m",
            "--cpus",
            "0.1",
            "--log-driver",
            "none",
            image,
            "/bin/sh",
            "-ceu",
            'test "$(id -u postgres)" = 999; test "$(id -g postgres)" = 999',
        ]
    )
    if identity.returncode != 0:
        raise RuntimeError("pinned PostgreSQL image does not map postgres to uid/gid 999:999")
    return image_id


def _absolute_path(value: str, env_name: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{env_name} must be an absolute path")
    return path


def _assert_path_has_no_symlink_component(path: Path) -> None:
    current = path
    existing: list[Path] = []
    while True:
        existing.append(current)
        if current == current.parent:
            break
        current = current.parent
    for component in reversed(existing):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"{path} must not contain symlink path components")


def _secure_password_file(value: str) -> Path:
    path = _absolute_path(value, "BLUEPRINT_POSTGRES_SECRETS_FILE")
    _assert_path_has_no_symlink_component(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("PostgreSQL password must be a regular, non-symlink file")
    if info.st_uid != os.getuid():
        raise ValueError(f"PostgreSQL password file must be owned by uid {os.getuid()}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("PostgreSQL password file must have mode 0600")
    if not path.read_bytes():
        raise ValueError("PostgreSQL password file must not be empty")
    return path


def _inspect_data_dir_as_postgres(path: Path, image: str) -> str:
    result = _run(
        [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--user",
            "999:999",
            "--read-only",
            "--mount",
            f"type=bind,src={path},dst={_DATA_TARGET},readonly",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "16",
            "--memory",
            "64m",
            "--cpus",
            "0.1",
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=1m",
            "--log-opt",
            "max-file=1",
            image,
            "/bin/sh",
            "-ceu",
            (
                f'if test -z "$(find {_DATA_TARGET} -mindepth 1 -maxdepth 1 '
                '-print -quit)"; then echo empty; '
                f"elif test ! -L {_DATA_TARGET}/PG_VERSION "
                f'&& test "$(cat {_DATA_TARGET}/PG_VERSION)" = 15; '
                "then echo initialized; else exit 42; fi"
            ),
        ]
    )
    state = result.stdout.strip()
    if result.returncode != 0 or state not in {"empty", "initialized"}:
        raise RuntimeError("refusing ambiguous or unreadable PostgreSQL data directory")
    return state


def _data_dir_info(path: Path) -> os.stat_result:
    return path.lstat()


def _prepare_data_dir(
    value: str,
    *,
    image: str,
    inspect_contents: bool,
    create: bool = True,
) -> tuple[Path, str]:
    path = _absolute_path(value, "BLUEPRINT_POSTGRES_DATA_DIR")
    _assert_path_has_no_symlink_component(path)
    if create:
        try:
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
        except FileExistsError as exc:
            raise ValueError("PostgreSQL data path is not a directory") from exc
    elif not path.exists():
        raise RuntimeError("PostgreSQL data directory is missing")
    _assert_path_has_no_symlink_component(path)
    info = _data_dir_info(path)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("PostgreSQL data path must be a directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("PostgreSQL data directory must have mode 0700")
    if info.st_uid != _POSTGRES_UID or info.st_gid != _POSTGRES_GID:
        raise RuntimeError(
            "PostgreSQL data directory must already be owned by uid/gid 999:999; "
            "only the system installer may bootstrap an empty directory"
        )
    if not inspect_contents:
        return path, "unchecked"
    return path, _inspect_data_dir_as_postgres(path, image)


def _is_not_found(result: subprocess.CompletedProcess[str]) -> bool:
    detail = _error(result).lower()
    return "no such object" in detail or "no such container" in detail or "not found" in detail


def _network_payload(name: str, *, create: bool = True) -> dict[str, Any]:
    if not _NETWORK_RE.fullmatch(name):
        raise ValueError("BLUEPRINT_CONTROL_PLANE_NETWORK is not a safe Docker network name")
    inspect = _run(["docker", "network", "inspect", name])
    if inspect.returncode != 0:
        if not _is_not_found(inspect):
            raise RuntimeError(f"could not inspect Docker network {name}: {_error(inspect)}")
        if not create:
            raise RuntimeError(f"Docker network {name} is missing")
        create = _run(
            [
                "docker",
                "network",
                "create",
                "--driver",
                "bridge",
                "--label",
                f"{_NETWORK_LABEL}={_NETWORK_LABEL_VALUE}",
                name,
            ]
        )
        if create.returncode != 0:
            raise RuntimeError(f"could not create Docker network {name}: {_error(create)}")
        inspect = _run(["docker", "network", "inspect", name])
    payload = _load_single_inspect(inspect, f"Docker network {name}")
    labels = payload.get("Labels") or {}
    if labels.get(_NETWORK_LABEL) != _NETWORK_LABEL_VALUE:
        raise RuntimeError(f"Docker network {name} is not owned by Blueprint")
    if payload.get("Driver") != "bridge":
        raise RuntimeError(f"Docker network {name} is not a bridge")
    try:
        gateway = payload["IPAM"]["Config"][0]["Gateway"]
        parsed = ipaddress.ip_address(gateway)
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Docker network {name} has no valid gateway") from exc
    if not parsed.is_private:
        raise RuntimeError(f"Docker network {name} does not use private addressing")
    return payload


def _inspect_container() -> dict[str, Any] | None:
    result = _run(["docker", "container", "inspect", _CONTAINER_NAME])
    if result.returncode != 0:
        if _is_not_found(result):
            return None
        raise RuntimeError(f"could not inspect container {_CONTAINER_NAME}: {_error(result)}")
    return _load_single_inspect(result, f"container {_CONTAINER_NAME}")


def _mount_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mounts = payload.get("Mounts") or []
    result: dict[str, dict[str, Any]] = {}
    for mount in mounts:
        if isinstance(mount, dict) and isinstance(mount.get("Destination"), str):
            result[mount["Destination"]] = mount
    return result


def _assert_matching_container(
    payload: dict[str, Any],
    *,
    image_id: str,
    network: str,
    data_dir: Path,
    password_file: Path,
) -> None:
    config = payload.get("Config") or {}
    host = payload.get("HostConfig") or {}
    state = payload.get("State") or {}
    labels = config.get("Labels") or {}
    if labels.get(_SERVICE_LABEL) != _SERVICE_LABEL_VALUE:
        raise RuntimeError("existing PostgreSQL container is not owned by Blueprint")
    if payload.get("Image") != image_id:
        raise RuntimeError("existing PostgreSQL container uses a different image")
    if host.get("NetworkMode") != network:
        raise RuntimeError("existing PostgreSQL container uses a different network")
    networks = (payload.get("NetworkSettings") or {}).get("Networks") or {}
    selected = networks.get(network) or {}
    if set(networks) != {network} or _NETWORK_ALIAS not in (selected.get("Aliases") or []):
        raise RuntimeError("existing PostgreSQL container has a mismatched network alias")
    if host.get("PortBindings"):
        raise RuntimeError("existing PostgreSQL container publishes host ports")

    mounts = _mount_map(payload)
    data_mount = mounts.get(_DATA_TARGET) or {}
    password_mount = mounts.get(_PASSWORD_TARGET) or {}
    if (
        data_mount.get("Type") != "bind"
        or Path(data_mount.get("Source", "")) != data_dir
        or data_mount.get("RW") is not True
    ):
        raise RuntimeError("existing PostgreSQL container has a mismatched data mount")
    if (
        password_mount.get("Type") != "bind"
        or Path(password_mount.get("Source", "")) != password_file
        or password_mount.get("RW") is not False
    ):
        raise RuntimeError("existing PostgreSQL container has a mismatched password mount")
    if set(mounts) != {_DATA_TARGET, _PASSWORD_TARGET}:
        raise RuntimeError("existing PostgreSQL container has unexpected mounts")

    container_env = {}
    for item in config.get("Env") or []:
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            container_env[key] = value
    expected_env = {
        "POSTGRES_PASSWORD_FILE": _PASSWORD_TARGET,
        "POSTGRES_USER": _POSTGRES_USER,
        "POSTGRES_DB": _POSTGRES_DB,
    }
    forbidden_env = {
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST_AUTH_METHOD",
        "POSTGRES_INITDB_ARGS",
        "POSTGRES_INITDB_WALDIR",
    }
    if (
        any(container_env.get(key) != value for key, value in expected_env.items())
        or forbidden_env.intersection(container_env)
    ):
        raise RuntimeError("existing PostgreSQL container has mismatched configuration")
    log_config = host.get("LogConfig") or {}
    if (
        host.get("Memory") != 2 * 1024**3
        or host.get("NanoCpus") != 2_000_000_000
        or host.get("PidsLimit") != 256
        or host.get("ShmSize") != 256 * 1024**2
        or log_config.get("Type") != "json-file"
        or (log_config.get("Config") or {}).get("max-size") != "10m"
        or (log_config.get("Config") or {}).get("max-file") != "3"
        or (host.get("RestartPolicy") or {}).get("Name") != "unless-stopped"
    ):
        raise RuntimeError("existing PostgreSQL container has mismatched resource limits")
    if "no-new-privileges" not in (host.get("SecurityOpt") or []):
        raise RuntimeError("existing PostgreSQL container lacks no-new-privileges")
    if state.get("Dead") is True:
        raise RuntimeError("existing PostgreSQL container is dead and requires operator review")


def _run_command(
    *,
    image: str,
    network: str,
    data_dir: Path,
    password_file: Path,
) -> list[str]:
    return [
        "docker",
        "run",
        "--detach",
        "--pull",
        "never",
        "--name",
        _CONTAINER_NAME,
        "--label",
        f"{_SERVICE_LABEL}={_SERVICE_LABEL_VALUE}",
        "--network",
        network,
        "--network-alias",
        _NETWORK_ALIAS,
        "--mount",
        f"type=bind,src={data_dir},dst={_DATA_TARGET}",
        "--mount",
        f"type=bind,src={password_file},dst={_PASSWORD_TARGET},readonly",
        "--env",
        f"POSTGRES_PASSWORD_FILE={_PASSWORD_TARGET}",
        "--env",
        f"POSTGRES_USER={_POSTGRES_USER}",
        "--env",
        f"POSTGRES_DB={_POSTGRES_DB}",
        "--memory",
        "2g",
        "--cpus",
        "2",
        "--pids-limit",
        "256",
        "--shm-size",
        "256m",
        "--restart",
        "unless-stopped",
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=10m",
        "--log-opt",
        "max-file=3",
        "--security-opt",
        "no-new-privileges",
        "--health-cmd",
        f"pg_isready -U {_POSTGRES_USER} -d {_POSTGRES_DB}",
        "--health-interval",
        "10s",
        "--health-timeout",
        "5s",
        "--health-retries",
        "6",
        image,
    ]


def _configuration(
    *, inspect_contents: bool, create_resources: bool
) -> tuple[str, str, Path, Path, str]:
    image = _required_env("BLUEPRINT_POSTGRES_IMAGE_ID")
    image_id = _inspect_image(image)
    network = _required_env("BLUEPRINT_CONTROL_PLANE_NETWORK")
    _network_payload(network, create=create_resources)
    password_file = _secure_password_file(
        _required_env("BLUEPRINT_POSTGRES_SECRETS_FILE")
    )
    data_dir, state = _prepare_data_dir(
        _required_env("BLUEPRINT_POSTGRES_DATA_DIR"),
        image=image,
        inspect_contents=inspect_contents,
        # The privileged installer is solely responsible for creating and
        # bootstrapping this directory to 999:999/0700. Service start must not
        # create a root-owned substitute after an operator path mistake.
        create=False,
    )
    return image, image_id, data_dir, password_file, state


def start() -> int:
    image, image_id, data_dir, password_file, _ = _configuration(
        inspect_contents=False,
        create_resources=True,
    )
    network = _required_env("BLUEPRINT_CONTROL_PLANE_NETWORK")
    existing = _inspect_container()
    if existing is not None:
        _assert_matching_container(
            existing,
            image_id=image_id,
            network=network,
            data_dir=data_dir,
            password_file=password_file,
        )
        if (existing.get("State") or {}).get("Running") is True:
            print("PostgreSQL container is already running.")
            return 0
        _prepare_data_dir(
            str(data_dir),
            image=image,
            inspect_contents=True,
            create=False,
        )
        result = _run(["docker", "container", "start", _CONTAINER_NAME])
    else:
        _prepare_data_dir(
            str(data_dir),
            image=image,
            inspect_contents=True,
            create=False,
        )
        result = _run(
            _run_command(
                image=image,
                network=network,
                data_dir=data_dir,
                password_file=password_file,
            )
        )
    if result.returncode != 0:
        raise RuntimeError(f"could not start PostgreSQL container: {_error(result)}")
    print("PostgreSQL container started.")
    return 0


def stop() -> int:
    existing = _inspect_container()
    if existing is None:
        print("PostgreSQL container is already absent.")
        return 0
    _, image_id, data_dir, password_file, _ = _configuration(
        inspect_contents=False,
        create_resources=False,
    )
    network = _required_env("BLUEPRINT_CONTROL_PLANE_NETWORK")
    _assert_matching_container(
        existing,
        image_id=image_id,
        network=network,
        data_dir=data_dir,
        password_file=password_file,
    )
    if (existing.get("State") or {}).get("Running") is not True:
        print("PostgreSQL container is already stopped.")
        return 0
    result = _run(["docker", "container", "stop", "--time", "60", _CONTAINER_NAME])
    if result.returncode != 0:
        raise RuntimeError(f"could not stop PostgreSQL container: {_error(result)}")
    print("PostgreSQL container stopped.")
    return 0


def status() -> int:
    existing = _inspect_container()
    if existing is None:
        print("absent")
        return 3
    _, image_id, data_dir, password_file, _ = _configuration(
        inspect_contents=False,
        create_resources=False,
    )
    network = _required_env("BLUEPRINT_CONTROL_PLANE_NETWORK")
    _assert_matching_container(
        existing,
        image_id=image_id,
        network=network,
        data_dir=data_dir,
        password_file=password_file,
    )
    state = existing.get("State") or {}
    if state.get("Running") is True:
        health = (state.get("Health") or {}).get("Status", "unknown")
        print(f"running ({health})")
        return 0
    print("stopped")
    return 3


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1 or args[0] not in {"start", "stop", "status"}:
        print("usage: local_postgres_service.py {start|stop|status}", file=sys.stderr)
        return 2
    try:
        return {"start": start, "stop": stop, "status": status}[args[0]]()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: PostgreSQL service preflight failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Create a production-shaped recovery archive from the live Cloud SQL source.

This is deliberately separate from ``local_postgres_backup_service.py``.  It
creates an ownership-labelled, mount-free PostgreSQL 15 client from an immutable
image ID to take a plain SQL dump through the authenticated source proxy, gzip
it, and publish the existing v1 completion-manifest contract. The client is
removed before upload. The archive is uploaded first; the manifest is the
last-step completion marker.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

ARCHIVE_PREFIX = "local-postgres-exports"
MANIFEST_SCHEMA_VERSION = 1
_RECOVERY_DATABASE = "blueprint"
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,255}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")
_BUCKET_RE = re.compile(r"^gs://[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_POSTGRES_DIGEST_RE = re.compile(
    r"^postgres(?::15(?:\.[0-9]+)?(?:-[A-Za-z0-9_.-]+)?)?"
    r"@sha256:[0-9a-f]{64}$"
)
_CLOUDSDK_DIGEST_RE = re.compile(
    r"^(?:gcr\.io/google\.com/cloudsdktool/cloud-sdk|"
    r"google/cloud-sdk)(?::[^@\s]+)?@sha256:[0-9a-f]{64}$"
)
_NETWORK_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
_NETWORK_LABEL = "dev.blueprint.purpose"
_NETWORK_LABEL_VALUE = "local-control-plane"
_CLIENT_OWNER_LABEL = "com.blueprint.owner"
_CLIENT_OWNER_VALUE = "live-source-rehearsal-export"
_CLIENT_SESSION_LABEL = "com.blueprint.rehearsal-id"
_CLIENT_PREFIX = "blueprint-rehearsal-export-"
_CREDENTIALS_TARGET = "/run/secrets/gcp.json"
_SOURCE_TARGET = "/run/input/source"
_SECRET_PREFIX = ".rehearsal-writer-"
_EVIDENCE_KIND = "live-source-rehearsal-source-evidence"


class RehearsalExportError(RuntimeError):
    """Fail-closed error carrying a non-sensitive stage and code."""

    def __init__(self, stage: str, code: str):
        super().__init__(f"{stage}:{code}")
        self.stage = stage
        self.code = code


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _safe_name(value: str, label: str) -> str:
    if not _SAFE_NAME_RE.fullmatch(value):
        raise RehearsalExportError("configuration", f"invalid_{label}")
    return value


def _reject_symlink_components(path: Path) -> None:
    current = path
    components: list[Path] = []
    while True:
        components.append(current)
        if current == current.parent:
            break
        current = current.parent
    for component in reversed(components):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise RehearsalExportError("filesystem", "symlink_path")


def _secure_secret(path_text: str, label: str) -> bytes:
    path = Path(path_text)
    if not path.is_absolute():
        raise RehearsalExportError("filesystem", f"{label}_not_absolute")
    _reject_symlink_components(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RehearsalExportError("filesystem", f"{label}_open_failed") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RehearsalExportError("filesystem", f"{label}_not_regular")
        if os.name != "nt" and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise RehearsalExportError("filesystem", f"{label}_permissions")
        value = bytearray()
        while chunk := os.read(descriptor, 64 * 1024):
            value.extend(chunk)
            if len(value) > 1024 * 1024:
                raise RehearsalExportError("configuration", f"{label}_too_large")
        if not value or b"\x00" in value:
            raise RehearsalExportError("configuration", f"invalid_{label}")
        return bytes(value)
    finally:
        os.close(descriptor)


def _validate_writer_credentials(raw: bytes, project_id: str) -> None:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RehearsalExportError("credentials", "invalid_writer_json") from exc
    expected = f"blueprint-local-db-backup@{project_id}.iam.gserviceaccount.com"
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "service_account"
        or payload.get("project_id") != project_id
        or payload.get("client_email") != expected
    ):
        raise RehearsalExportError("credentials", "wrong_writer_identity")


def _trusted_binary(path_text: str, label: str) -> str:
    path = Path(path_text)
    if not path.is_absolute():
        raise RehearsalExportError("configuration", f"{label}_not_absolute")
    _reject_symlink_components(path.parent)
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise RehearsalExportError("configuration", f"{label}_missing") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        raise RehearsalExportError("configuration", f"{label}_not_executable")
    if os.name != "nt" and (
        info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RehearsalExportError("configuration", f"{label}_untrusted")
    return str(resolved)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_create_json(path_text: str, payload: Mapping[str, Any]) -> Path:
    path = Path(path_text)
    if not path.is_absolute() or not path.name.endswith(".json"):
        raise RehearsalExportError("filesystem", "invalid_evidence_output")
    _reject_symlink_components(path.parent)
    try:
        parent = path.parent.lstat()
    except OSError as exc:
        raise RehearsalExportError("filesystem", "evidence_parent_missing") from exc
    if not stat.S_ISDIR(parent.st_mode) or (
        os.name != "nt"
        and (parent.st_uid != os.geteuid() or parent.st_mode & 0o077)
    ):
        raise RehearsalExportError("filesystem", "evidence_parent_permissions")
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RehearsalExportError("filesystem", "evidence_create_failed") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        return path
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise


def _mount_fstype(path: Path, mountinfo: str) -> str | None:
    resolved = str(path.resolve(strict=True))
    selected: tuple[int, str] | None = None
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        after_fields = after.split()
        if len(fields) < 5 or not after_fields:
            continue
        mountpoint = fields[4].replace("\\040", " ")
        try:
            common = os.path.commonpath([resolved, mountpoint])
        except ValueError:
            continue
        if common == mountpoint and (selected is None or len(mountpoint) > selected[0]):
            selected = (len(mountpoint), after_fields[0])
    return selected[1] if selected is not None else None


def _secret_stage_root(path_text: str) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        raise RehearsalExportError("filesystem", "secret_stage_root_not_absolute")
    _reject_symlink_components(path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise RehearsalExportError("filesystem", "secret_stage_root_missing") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or (os.name != "nt" and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700))
    ):
        raise RehearsalExportError("filesystem", "secret_stage_root_permissions")
    if os.name != "nt":
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except OSError as exc:
            raise RehearsalExportError("filesystem", "mountinfo_unavailable") from exc
        if _mount_fstype(path, mountinfo) != "tmpfs":
            raise RehearsalExportError("filesystem", "secret_stage_root_not_tmpfs")
    for candidate in path.glob(f"{_SECRET_PREFIX}*.json"):
        candidate_info = candidate.lstat()
        if (
            not stat.S_ISREG(candidate_info.st_mode)
            or (os.name != "nt" and candidate_info.st_uid != 0)
        ):
            raise RehearsalExportError("filesystem", "ambiguous_stale_secret")
        candidate.unlink()
    return path


def _docker_inspect(
    docker_binary: str,
    argv: list[str],
    subject: str,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    result = run(
        [docker_binary, *argv],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise RehearsalExportError("docker_validation", f"{subject}_inspect_failed")
    try:
        payload = json.loads(result.stdout)
        item = payload[0]
    except (IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RehearsalExportError(
            "docker_validation", f"{subject}_invalid_inspect"
        ) from exc
    if not isinstance(item, dict):
        raise RehearsalExportError(
            "docker_validation", f"{subject}_invalid_inspect"
        )
    return item


def _validate_uploader_image(
    docker_binary: str,
    image: str,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> None:
    if not _CLOUDSDK_DIGEST_RE.fullmatch(image):
        raise RehearsalExportError("configuration", "uploader_image_not_immutable")
    payload = _docker_inspect(
        docker_binary,
        ["image", "inspect", image],
        "uploader_image",
        run=run,
    )
    resolved = payload.get("Id")
    if not isinstance(resolved, str) or not _IMAGE_ID_RE.fullmatch(resolved):
        raise RehearsalExportError("docker_validation", "uploader_image_missing_id")
    requested_digest = image.rsplit("@", 1)[1]
    if not any(
        isinstance(item, str)
        and item.startswith(
            (
                "gcr.io/google.com/cloudsdktool/cloud-sdk@",
                "google/cloud-sdk@",
            )
        )
        and item.rsplit("@", 1)[-1] == requested_digest
        for item in (payload.get("RepoDigests") or [])
    ):
        raise RehearsalExportError(
            "docker_validation", "uploader_image_digest_mismatch"
        )
    result = run(
        [
            docker_binary,
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
            "32",
            "--memory",
            "128m",
            "--cpus",
            "0.25",
            "--log-driver",
            "none",
            image,
            "gcloud",
            "--version",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise RehearsalExportError("docker_validation", "uploader_gcloud_unavailable")


def _validate_postgres_image(
    docker_binary: str,
    image: str,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> str:
    if not _POSTGRES_DIGEST_RE.fullmatch(image):
        raise RehearsalExportError(
            "configuration", "postgres_image_not_official_digest"
        )
    payload = _docker_inspect(
        docker_binary,
        ["image", "inspect", image],
        "postgres_image",
        run=run,
    )
    resolved = payload.get("Id")
    requested_digest = image.rsplit("@", 1)[1]
    if (
        not isinstance(resolved, str)
        or not _IMAGE_ID_RE.fullmatch(resolved)
        or not any(
            isinstance(item, str)
            and item.startswith("postgres@")
            and item.rsplit("@", 1)[-1] == requested_digest
            for item in (payload.get("RepoDigests") or [])
        )
        or "PG_MAJOR=15" not in ((payload.get("Config") or {}).get("Env") or [])
    ):
        raise RehearsalExportError(
            "docker_validation", "postgres_image_digest_mismatch"
        )
    result = run(
        [
            docker_binary,
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
            "32",
            "--memory",
            "128m",
            "--cpus",
            "0.25",
            "--log-driver",
            "none",
            image,
            "pg_dump",
            "--version",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0 or not re.search(
        r"\bpg_dump \(PostgreSQL\) 15(?:\.|\b)", result.stdout.strip()
    ):
        raise RehearsalExportError("postgres_client", "pg_dump_not_postgresql_15")
    return resolved


def _validate_control_plane_network(
    docker_binary: str,
    network: str,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> str:
    if not _NETWORK_RE.fullmatch(network):
        raise RehearsalExportError("configuration", "invalid_control_plane_network")
    payload = _docker_inspect(
        docker_binary,
        ["network", "inspect", network],
        "control_plane_network",
        run=run,
    )
    if (payload.get("Labels") or {}).get(_NETWORK_LABEL) != _NETWORK_LABEL_VALUE:
        raise RehearsalExportError(
            "docker_validation", "control_plane_network_not_blueprint_owned"
        )
    if payload.get("Driver") != "bridge":
        raise RehearsalExportError(
            "docker_validation", "control_plane_network_not_bridge"
        )
    try:
        gateway = payload["IPAM"]["Config"][0]["Gateway"]
        parsed = ipaddress.ip_address(gateway)
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise RehearsalExportError(
            "docker_validation", "control_plane_network_invalid_gateway"
        ) from exc
    if not parsed.is_private:
        raise RehearsalExportError(
            "docker_validation", "control_plane_network_public_gateway"
        )
    return str(parsed)


def _cloud_sql_instance_evidence(
    path_text: str, project_id: str, instance_name: str
) -> dict[str, Any]:
    raw = _secure_secret(path_text, "cloud_sql_instance_evidence")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RehearsalExportError("source_identity", "invalid_instance_evidence") from exc
    if not isinstance(payload, dict):
        raise RehearsalExportError("source_identity", "invalid_instance_evidence")
    connection_name = payload.get("connectionName")
    expected_prefix = f"{project_id}:"
    if (
        payload.get("name") != instance_name
        or payload.get("databaseVersion") != "POSTGRES_15"
        or not isinstance(connection_name, str)
        or not connection_name.startswith(expected_prefix)
        or not connection_name.endswith(f":{instance_name}")
        or connection_name.count(":") != 2
    ):
        raise RehearsalExportError("source_identity", "instance_identity_mismatch")
    return {
        "path": str(Path(path_text).resolve(strict=True)),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "project_id": project_id,
        "instance": instance_name,
        "connection_name": connection_name,
        "region": payload.get("region"),
        "database_version": "POSTGRES_15",
    }


def _proxy_option(argv: list[str], name: str, default: str) -> str:
    prefix = f"--{name}="
    values: list[str] = []
    for index, value in enumerate(argv):
        if value.startswith(prefix):
            values.append(value[len(prefix) :])
        elif value == f"--{name}":
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                raise RehearsalExportError(
                    "source_identity", f"invalid_proxy_{name}_option"
                )
            values.append(argv[index + 1])
    if len(values) > 1:
        raise RehearsalExportError(
            "source_identity", f"duplicate_proxy_{name}_option"
        )
    return values[0] if values else default


def _validated_proxy_listener(
    argv: list[str],
    *,
    connection_name: str,
    expected_address: str,
    expected_port: int,
) -> tuple[str, int]:
    connection_pattern = re.compile(
        r"^[a-z][a-z0-9-]{4,28}[a-z0-9]:"
        r"[a-z0-9-]+:[A-Za-z0-9_-]+(?:\?[^ ]*)?$"
    )
    instance_specs = [value for value in argv if connection_pattern.fullmatch(value)]
    if instance_specs != [connection_name]:
        raise RehearsalExportError("source_identity", "proxy_connection_mismatch")
    address = _proxy_option(argv, "address", "127.0.0.1")
    port_text = _proxy_option(argv, "port", "5432")
    try:
        port = int(port_text)
        parsed_address = ipaddress.ip_address(address)
    except ValueError as exc:
        raise RehearsalExportError("source_identity", "invalid_proxy_listener") from exc
    if (
        port != expected_port
        or address != expected_address
        or not parsed_address.is_private
    ):
        raise RehearsalExportError("source_identity", "proxy_listener_mismatch")
    return address, port


def _proxy_evidence(
    pid: int,
    *,
    connection_name: str,
    expected_address: str,
    expected_port: int,
) -> dict[str, Any]:
    if os.name == "nt" or pid <= 1:
        raise RehearsalExportError("source_identity", "invalid_proxy_pid")
    proc = Path("/proc") / str(pid)
    try:
        executable = (proc / "exe").resolve(strict=True)
        executable_info = executable.stat()
        raw_argv = (proc / "cmdline").read_bytes()
        stat_text = (proc / "stat").read_text(encoding="utf-8")
        status_text = (proc / "status").read_text(encoding="utf-8")
    except OSError as exc:
        raise RehearsalExportError("source_identity", "proxy_process_unavailable") from exc
    uid_line = next(
        (line for line in status_text.splitlines() if line.startswith("Uid:")),
        "",
    )
    try:
        effective_uid = int(uid_line.split()[2])
    except (IndexError, ValueError) as exc:
        raise RehearsalExportError(
            "source_identity", "invalid_proxy_process_owner"
        ) from exc
    if (
        not stat.S_ISREG(executable_info.st_mode)
        or executable_info.st_uid not in {0, effective_uid}
        or executable_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RehearsalExportError("source_identity", "untrusted_proxy_executable")
    closing = stat_text.rfind(")")
    stat_tail = stat_text[closing + 1 :].split() if closing >= 0 else []
    # The tail starts at proc field 3; starttime is field 22.
    if len(stat_tail) <= 19:
        raise RehearsalExportError("source_identity", "invalid_proxy_process_state")
    try:
        argv = [part.decode("utf-8") for part in raw_argv.split(b"\x00") if part]
    except UnicodeDecodeError as exc:
        raise RehearsalExportError("source_identity", "invalid_proxy_argv") from exc
    address, port = _validated_proxy_listener(
        argv,
        connection_name=connection_name,
        expected_address=expected_address,
        expected_port=expected_port,
    )
    encoded_argv = json.dumps(argv, separators=(",", ":"), ensure_ascii=True).encode()
    return {
        "pid": pid,
        "process_start_ticks": stat_tail[19],
        "effective_uid": effective_uid,
        "executable": str(executable),
        "executable_sha256": _sha256_path(executable),
        "argv": argv,
        "argv_sha256": hashlib.sha256(encoded_argv).hexdigest(),
        "connection_name": connection_name,
        "address": address,
        "port": port,
    }


def _container_inspect(
    docker_binary: str,
    name: str,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any] | None:
    result = run(
        [docker_binary, "container", "inspect", name],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        detail = f"{result.stdout}\n{result.stderr}".lower()
        if "no such object" in detail or "no such container" in detail:
            return None
        raise RehearsalExportError("docker_validation", "client_inspect_failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RehearsalExportError("docker_validation", "client_invalid_inspect") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RehearsalExportError("docker_validation", "client_invalid_inspect")
    return payload[0]


def _assert_owned_client(
    payload: Mapping[str, Any],
    *,
    name: str,
    session_id: str,
    image_id: str,
    network: str,
) -> None:
    labels = ((payload.get("Config") or {}).get("Labels") or {})
    networks = ((payload.get("NetworkSettings") or {}).get("Networks") or {})
    host = payload.get("HostConfig") or {}
    tmpfs = host.get("Tmpfs") or {}
    tmp_options = set(str(tmpfs.get("/tmp") or "").split(","))
    log_config = host.get("LogConfig") or {}
    if (
        str(payload.get("Name") or "").lstrip("/") != name
        or not name.startswith(_CLIENT_PREFIX)
        or labels.get(_CLIENT_OWNER_LABEL) != _CLIENT_OWNER_VALUE
        or labels.get(_CLIENT_SESSION_LABEL) != session_id
        or payload.get("Image") != image_id
        or set(networks) != {network}
        or bool(payload.get("Mounts"))
        or bool(host.get("PortBindings"))
        or str((payload.get("Config") or {}).get("User")) != "999:999"
        or host.get("ReadonlyRootfs") is not True
        or host.get("CapDrop") != ["ALL"]
        or host.get("SecurityOpt") != ["no-new-privileges"]
        or host.get("NetworkMode") != network
        or host.get("PidsLimit") != 64
        or host.get("Memory") != 256 * 1024 * 1024
        or host.get("NanoCpus") != 500_000_000
        or log_config.get("Type") != "none"
        or tmp_options != {"rw", "noexec", "nosuid", "nodev", "size=64m"}
        or (payload.get("State") or {}).get("Running") is not True
    ):
        raise RehearsalExportError("docker_validation", "client_isolation_mismatch")


def _assert_cleanup_ownership(
    payload: Mapping[str, Any],
    *,
    name: str,
    session_id: str,
    image_id: str,
) -> None:
    labels = ((payload.get("Config") or {}).get("Labels") or {})
    if (
        str(payload.get("Name") or "").lstrip("/") != name
        or not name.startswith(_CLIENT_PREFIX)
        or labels.get(_CLIENT_OWNER_LABEL) != _CLIENT_OWNER_VALUE
        or labels.get(_CLIENT_SESSION_LABEL) != session_id
        or payload.get("Image") != image_id
    ):
        raise RehearsalExportError("cleanup", "refusing_unowned_client")


def _start_client(
    args: argparse.Namespace,
    name: str,
    session_id: str,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> None:
    if _container_inspect(args.docker_binary, name, run=run) is not None:
        raise RehearsalExportError("docker_validation", "client_name_collision")
    result = run(
        [
            args.docker_binary,
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            f"{_CLIENT_OWNER_LABEL}={_CLIENT_OWNER_VALUE}",
            "--label",
            f"{_CLIENT_SESSION_LABEL}={session_id}",
            "--pull",
            "never",
            "--network",
            args.control_plane_network,
            "--user",
            "999:999",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--memory",
            "256m",
            "--cpus",
            "0.5",
            "--log-driver",
            "none",
            args.postgres_image_id,
            "sleep",
            "infinity",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise RehearsalExportError("postgres_client", "client_start_failed")
    payload = _container_inspect(args.docker_binary, name, run=run)
    if payload is None:
        raise RehearsalExportError("postgres_client", "client_disappeared")
    _assert_owned_client(
        payload,
        name=name,
        session_id=session_id,
        image_id=args.postgres_image_id,
        network=args.control_plane_network,
    )


def _remove_client(
    args: argparse.Namespace,
    name: str,
    session_id: str,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> None:
    if not name.startswith(_CLIENT_PREFIX):
        raise RehearsalExportError("cleanup", "unsafe_client_name")
    payload = _container_inspect(args.docker_binary, name, run=run)
    if payload is None:
        return
    _assert_cleanup_ownership(
        payload,
        name=name,
        session_id=session_id,
        image_id=args.postgres_image_id,
    )
    result = run(
        [args.docker_binary, "container", "rm", "--force", name],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise RehearsalExportError("cleanup", "client_remove_failed")
    if _container_inspect(args.docker_binary, name, run=run) is not None:
        raise RehearsalExportError("cleanup", "remote_pg_dump_still_present")


def _pg_environment(password: bytes) -> dict[str, str]:
    try:
        decoded = password.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RehearsalExportError("configuration", "invalid_password_encoding") from exc
    environment = os.environ.copy()
    environment["PGPASSWORD"] = decoded.removesuffix("\n")
    environment["PGAPPNAME"] = "blueprint-restore-rehearsal-export"
    return environment


def _pg_dump_argv(args: argparse.Namespace) -> list[str]:
    return [
        args.docker_binary,
        "exec",
        "--interactive",
        "--env",
        "PGPASSWORD",
        "--env",
        "PGAPPNAME",
        args.container,
        "pg_dump",
        "--format=plain",
        "--no-owner",
        "--no-privileges",
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--username",
        args.user,
        "--dbname",
        args.source_database,
    ]


def _assert_pg15(args: argparse.Namespace, run: Callable[..., Any] = subprocess.run) -> None:
    result = run(
        [args.docker_binary, "exec", args.container, "pg_dump", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0 or not re.search(
        r"\bpg_dump \(PostgreSQL\) 15(?:\.|\b)", result.stdout.strip()
    ):
        raise RehearsalExportError("postgres_client", "pg_dump_not_postgresql_15")


def _create_output(path_text: str) -> tuple[int, Path]:
    path = Path(path_text)
    if not path.is_absolute() or not path.name.endswith(".sql.gz"):
        raise RehearsalExportError("filesystem", "invalid_archive_output")
    _reject_symlink_components(path.parent)
    try:
        parent = path.parent.lstat()
    except OSError as exc:
        raise RehearsalExportError("filesystem", "archive_parent_missing") from exc
    if not stat.S_ISDIR(parent.st_mode):
        raise RehearsalExportError("filesystem", "archive_parent_not_directory")
    if os.name != "nt" and (
        parent.st_uid != os.geteuid() or parent.st_mode & 0o077
    ):
        raise RehearsalExportError("filesystem", "archive_parent_permissions")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        return os.open(path, flags, 0o600), path
    except OSError as exc:
        raise RehearsalExportError("filesystem", "archive_create_failed") from exc


def create_archive(
    args: argparse.Namespace,
    password: bytes,
    *,
    popen: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    """Create a new gzip-wrapped plain SQL archive without exposing the password."""

    if not 60 <= args.timeout_seconds <= 21600:
        raise RehearsalExportError("configuration", "invalid_dump_timeout")
    descriptor, path = _create_output(args.archive_output)
    process = None
    relay_errors: list[BaseException] = []
    try:
        process = popen(
            _pg_dump_argv(args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_pg_environment(password),
            start_new_session=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None

        def drain() -> None:
            while process.stderr.read(64 * 1024):
                pass

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()

        def relay() -> None:
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as raw:
                    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                        while chunk := process.stdout.read(1024 * 1024):
                            compressed.write(chunk)
                    raw.flush()
                    os.fsync(descriptor)
            except BaseException as exc:
                relay_errors.append(exc)

        relay_thread = threading.Thread(target=relay, daemon=True)
        relay_thread.start()
        deadline = time.monotonic() + args.timeout_seconds
        while process.poll() is None or relay_thread.is_alive():
            if relay_errors:
                raise RehearsalExportError("dump", "archive_stream_failed")
            if time.monotonic() >= deadline:
                raise RehearsalExportError("dump", "timeout")
            time.sleep(0.1)
        relay_thread.join(timeout=10)
        if relay_thread.is_alive() or relay_errors:
            raise RehearsalExportError("dump", "archive_stream_failed")
        returncode = int(process.returncode)
        reader.join(timeout=10)
        if reader.is_alive():
            raise RehearsalExportError("dump", "stderr_drain_timeout")
        if returncode != 0:
            raise RehearsalExportError("dump", "pg_dump_failed")
    except Exception:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    if size <= 20:
        path.unlink(missing_ok=True)
        raise RehearsalExportError("dump", "empty_archive")
    return {"path": path, "sha256": digest.hexdigest(), "size_bytes": size}


def completion_manifest(
    *,
    bucket: str,
    archive_name: str,
    archive_id: str,
    sha256: str,
    size_bytes: int,
    created_at: dt.datetime,
) -> dict[str, Any]:
    """Build the exact v1 contract consumed by the independent verifier."""

    if not _BUCKET_RE.fullmatch(bucket) or not _HEX32_RE.fullmatch(archive_id):
        raise RehearsalExportError("manifest", "invalid_identity")
    if archive_name != f"{ARCHIVE_PREFIX}/{archive_name.rsplit('/', 1)[-1]}":
        raise RehearsalExportError("manifest", "invalid_archive_name")
    if not archive_name.endswith(f"-{archive_id}.sql.gz"):
        raise RehearsalExportError("manifest", "archive_id_mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256) or size_bytes <= 0:
        raise RehearsalExportError("manifest", "invalid_integrity")
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise RehearsalExportError("manifest", "naive_created_at")
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "archive_id": archive_id,
        "archive_uri": f"{bucket}/{archive_name}",
        "archive_name": archive_name,
        "created_at": created_at.astimezone(dt.timezone.utc).isoformat(),
        "database": _RECOVERY_DATABASE,
        "format": "plain-sql-gzip",
        "postgres_major": 15,
        "sha256": sha256,
        "size_bytes": size_bytes,
    }


def _upload(
    *,
    docker_binary: str,
    uploader_image_id: str,
    control_plane_network: str,
    source: Path,
    destination: str,
    credential_file: Path,
    recovery_project_id: str,
    run: Callable[..., Any] = subprocess.run,
) -> None:
    source = source.resolve(strict=True)
    credential_file = credential_file.resolve(strict=True)
    for path in (source, credential_file):
        if "," in str(path) or any(ord(char) < 32 for char in str(path)):
            raise RehearsalExportError("filesystem", "docker_mount_unsafe")
    result = run(
        [
            docker_binary,
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            control_plane_network,
            "--mount",
            f"type=bind,src={credential_file},dst={_CREDENTIALS_TARGET},readonly",
            "--mount",
            f"type=bind,src={source},dst={_SOURCE_TARGET},readonly",
            "--env",
            f"GOOGLE_APPLICATION_CREDENTIALS={_CREDENTIALS_TARGET}",
            "--env",
            f"CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE={_CREDENTIALS_TARGET}",
            "--env",
            f"CLOUDSDK_CORE_PROJECT={recovery_project_id}",
            "--memory",
            "256m",
            "--cpus",
            "0.5",
            "--pids-limit",
            "64",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=128m",
            "--tmpfs",
            "/root/.config:rw,noexec,nosuid,nodev,size=16m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=10m",
            "--log-opt",
            "max-file=1",
            uploader_image_id,
            "gcloud",
            "storage",
            "cp",
            _SOURCE_TARGET,
            destination,
            "--if-generation-match=0",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=3600,
    )
    if result.returncode != 0:
        raise RehearsalExportError("upload", "object_create_failed")


def publish_archive(
    args: argparse.Namespace,
    archive: Mapping[str, Any],
    writer_credentials: bytes,
    *,
    now: Callable[[], dt.datetime] = _utcnow,
    archive_id: str | None = None,
    upload: Callable[..., None] = _upload,
) -> dict[str, Any]:
    """Upload archive then marker. A failed archive upload can never publish a marker."""

    selected_id = archive_id or uuid.uuid4().hex
    if not _HEX32_RE.fullmatch(selected_id):
        raise RehearsalExportError("manifest", "invalid_archive_id")
    stamp = now().astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_name = f"{ARCHIVE_PREFIX}/{stamp}-{selected_id}.sql.gz"
    archive_uri = f"{args.bucket}/{archive_name}"
    credential_copy: Path | None = None
    marker: Path | None = None
    try:
        credential_fd, credential_name = tempfile.mkstemp(
            prefix=_SECRET_PREFIX,
            suffix=".json",
            dir=args.secret_stage_root,
        )
        credential_copy = Path(credential_name)
        os.fchmod(credential_fd, 0o600)
        with os.fdopen(credential_fd, "wb") as stream:
            stream.write(writer_credentials)
            stream.flush()
            os.fsync(stream.fileno())
        # The archive must exist before its marker can make it eligible for restore.
        upload(
            docker_binary=args.docker_binary,
            uploader_image_id=args.uploader_image_id,
            control_plane_network=args.control_plane_network,
            source=Path(archive["path"]),
            destination=archive_uri,
            credential_file=credential_copy,
            recovery_project_id=args.recovery_project_id,
        )
        manifest = completion_manifest(
            bucket=args.bucket,
            archive_name=archive_name,
            archive_id=selected_id,
            sha256=str(archive["sha256"]),
            size_bytes=int(archive["size_bytes"]),
            created_at=now(),
        )
        encoded = json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(encoded) > 65536:
            raise RehearsalExportError("manifest", "manifest_too_large")
        descriptor, name = tempfile.mkstemp(
            prefix=".rehearsal-manifest-",
            suffix=".json",
            dir=Path(archive["path"]).parent,
        )
        marker = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        upload(
            docker_binary=args.docker_binary,
            uploader_image_id=args.uploader_image_id,
            control_plane_network=args.control_plane_network,
            source=marker,
            destination=f"{archive_uri}.manifest.json",
            credential_file=credential_copy,
            recovery_project_id=args.recovery_project_id,
        )
    finally:
        if marker is not None:
            marker.unlink(missing_ok=True)
        if credential_copy is not None:
            credential_copy.unlink(missing_ok=True)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-image-id", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--source-database", default="postgres")
    parser.add_argument("--source-project-id", required=True)
    parser.add_argument("--source-instance", required=True)
    parser.add_argument("--cloud-sql-instance-evidence", required=True)
    parser.add_argument("--proxy-pid", required=True, type=int)
    parser.add_argument("--source-evidence-output", required=True)
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--archive-output", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--recovery-project-id", required=True)
    parser.add_argument("--writer-credentials-file", required=True)
    parser.add_argument("--docker-binary", default="/usr/bin/docker")
    parser.add_argument("--uploader-image-id", required=True)
    parser.add_argument("--control-plane-network", required=True)
    parser.add_argument(
        "--secret-stage-root", default="/run/blueprint-rehearsal-export"
    )
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    args.user = _safe_name(args.user, "user")
    args.source_database = _safe_name(args.source_database, "source_database")
    args.source_instance = _safe_name(args.source_instance, "source_instance")
    if not _PROJECT_RE.fullmatch(args.source_project_id):
        raise RehearsalExportError("configuration", "invalid_source_project")
    if not _HOST_RE.fullmatch(args.host):
        raise RehearsalExportError("configuration", "invalid_host")
    if not 1 <= args.port <= 65535:
        raise RehearsalExportError("configuration", "invalid_port")
    if not _BUCKET_RE.fullmatch(args.bucket):
        raise RehearsalExportError("configuration", "invalid_bucket")
    if not _PROJECT_RE.fullmatch(args.recovery_project_id):
        raise RehearsalExportError("configuration", "invalid_recovery_project")
    if args.source_project_id == args.recovery_project_id:
        raise RehearsalExportError("configuration", "source_recovery_project_overlap")
    args.docker_binary = _trusted_binary(args.docker_binary, "docker_binary")


def _source_evidence(
    args: argparse.Namespace,
    *,
    instance: Mapping[str, Any],
    proxy: Mapping[str, Any],
    archive: Mapping[str, Any],
    planned_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    manifest_bytes = json.dumps(
        planned_manifest, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return {
        "schema_version": 1,
        "kind": _EVIDENCE_KIND,
        "captured_at": _utcnow().isoformat(),
        "source": {
            "project_id": args.source_project_id,
            "instance": args.source_instance,
            "database": args.source_database,
            "host": args.host,
            "port": args.port,
        },
        "cloud_sql_instance": dict(instance),
        "proxy": dict(proxy),
        "archive": {
            "local_path": str(Path(archive["path"]).resolve(strict=True)),
            "sha256": archive["sha256"],
            "size_bytes": archive["size_bytes"],
            "planned_uri": planned_manifest["archive_uri"],
            "planned_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        },
        "producer": {
            "postgres_image_ref": getattr(
                args, "postgres_image_ref", args.postgres_image_id
            ),
            "postgres_image_id": args.postgres_image_id,
            "uploader_image_ref": args.uploader_image_id,
            "client_owned_and_removed": True,
            "client_mounts": [],
            "production_write_fence_required": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client_name: str | None = None
    session_id: str | None = None
    failure: BaseException | None = None
    manifest: dict[str, Any] | None = None
    try:
        _validate_args(args)
        args.secret_stage_root = _secret_stage_root(args.secret_stage_root)
        password = _secure_secret(args.password_file, "password_file")
        credentials = _secure_secret(
            args.writer_credentials_file, "writer_credentials_file"
        )
        _validate_writer_credentials(credentials, args.recovery_project_id)
        _validate_uploader_image(args.docker_binary, args.uploader_image_id)
        args.postgres_image_ref = args.postgres_image_id
        args.postgres_image_id = _validate_postgres_image(
            args.docker_binary, args.postgres_image_ref
        )
        gateway = _validate_control_plane_network(
            args.docker_binary, args.control_plane_network
        )
        if args.host != gateway:
            raise RehearsalExportError(
                "source_identity", "source_host_not_network_gateway"
            )
        instance = _cloud_sql_instance_evidence(
            args.cloud_sql_instance_evidence,
            args.source_project_id,
            args.source_instance,
        )
        proxy_before = _proxy_evidence(
            args.proxy_pid,
            connection_name=str(instance["connection_name"]),
            expected_address=gateway,
            expected_port=args.port,
        )
        session_id = uuid.uuid4().hex
        client_name = f"{_CLIENT_PREFIX}{session_id}"
        args.container = client_name
        _start_client(args, client_name, session_id)
        _assert_pg15(args)
        archive = create_archive(args, password)
        proxy_after = _proxy_evidence(
            args.proxy_pid,
            connection_name=str(instance["connection_name"]),
            expected_address=gateway,
            expected_port=args.port,
        )
        if proxy_after != proxy_before:
            raise RehearsalExportError(
                "source_identity", "proxy_changed_during_dump"
            )
        _remove_client(args, client_name, session_id)
        client_name = None

        archive_id = uuid.uuid4().hex
        publication_time = _utcnow()
        archive_name = (
            f"{ARCHIVE_PREFIX}/"
            f"{publication_time.strftime('%Y%m%dT%H%M%SZ')}-{archive_id}.sql.gz"
        )
        planned_manifest = completion_manifest(
            bucket=args.bucket,
            archive_name=archive_name,
            archive_id=archive_id,
            sha256=str(archive["sha256"]),
            size_bytes=int(archive["size_bytes"]),
            created_at=publication_time,
        )
        evidence_path = _atomic_create_json(
            args.source_evidence_output,
            _source_evidence(
                args,
                instance=instance,
                proxy=proxy_after,
                archive=archive,
                planned_manifest=planned_manifest,
            ),
        )
        manifest = publish_archive(
            args,
            archive,
            credentials,
            now=lambda: publication_time,
            archive_id=archive_id,
        )
        if manifest != planned_manifest:
            raise RehearsalExportError("manifest", "publication_plan_mismatch")
        evidence_sha256 = _sha256_path(evidence_path)
    except (OSError, RehearsalExportError) as exc:
        failure = exc
    finally:
        if client_name is not None and session_id is not None:
            try:
                _remove_client(args, client_name, session_id)
            except (OSError, RehearsalExportError) as exc:
                failure = exc
    if failure is not None:
        stage = (
            failure.stage
            if isinstance(failure, RehearsalExportError)
            else "operating_system"
        )
        code = (
            failure.code
            if isinstance(failure, RehearsalExportError)
            else "io_failure"
        )
        print(
            f"ERROR: live-source rehearsal export refused ({stage}:{code})",
            file=sys.stderr,
        )
        return 1
    if manifest is None:
        print(
            "ERROR: live-source rehearsal export refused (publication:no_manifest)",
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "event": "live_source_rehearsal_export",
                "outcome": "success",
                "archive_uri": manifest["archive_uri"],
                "source_evidence_sha256": evidence_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

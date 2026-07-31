#!/usr/bin/env python3
"""Disposable, pre-cutover Cloud SQL -> PostgreSQL 15 restore rehearsal.

This tool is deliberately separate from ``local_postgres_cutover.py``. It
creates a serializable-deferrable custom-format dump while the source remains
live, restores it into a uniquely named isolated container, verifies the
restored schema and critical aggregates, and removes the container and its
temporary data directory before reporting success.

The artifact is rehearsal-only. It is not accepted by the final fenced
cutover, and this tool refuses to use the permanent local PostgreSQL container
or any work directory that overlaps the permanent PostgreSQL data directory.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import queue
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import local_postgres_cutover as cutover

SCHEMA_VERSION = 1
MANIFEST_KIND = "postgres-precutover-rehearsal-manifest"
RESULT_KIND = "postgres-precutover-rehearsal-result"
_OWNER_LABEL = "com.blueprint.owner"
_OWNER_VALUE = "precutover-restore-rehearsal"
_SESSION_LABEL = "com.blueprint.rehearsal-id"
_PERMANENT_CONTAINER = "blueprint-local-postgres"
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_POSTGRES_DIGEST_RE = re.compile(
    r"^postgres(?::15(?:\.[0-9]+)?(?:-[A-Za-z0-9_.-]+)?)?"
    r"@sha256:[0-9a-f]{64}$"
)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_NETWORK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SESSION_PREFIX = "blueprint-precutover-rehearsal-"
_POSTGRES_UID = 999
_POSTGRES_GID = 999
_NETWORK_LABEL = "dev.blueprint.purpose"
_NETWORK_LABEL_VALUE = "local-control-plane"
_SNAPSHOT_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9A-Fa-f]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RehearsalError(RuntimeError):
    """Expected fail-closed outcome with no secret-bearing detail."""

    def __init__(self, stage: str, code: str):
        self.stage = stage
        self.code = code
        super().__init__(f"{stage}:{code}")


def _run(
    argv: Sequence[str],
    *,
    timeout: int = 120,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _safe_name(value: str, label: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise RehearsalError("configuration", f"invalid_{label}")
    return value


def _absolute_path(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise RehearsalError("filesystem", f"{label}_not_absolute")
    cutover._reject_symlink_components(path)
    return path


def _trusted_binary(value: str, label: str) -> str:
    path = _absolute_path(value, label)
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise RehearsalError("configuration", f"{label}_missing") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        raise RehearsalError("configuration", f"{label}_not_executable")
    if os.name != "nt" and (
        info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RehearsalError("configuration", f"{label}_untrusted")
    return str(resolved)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError as exc:
        raise RehearsalError("filesystem", "path_resolution_failed") from exc


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_isolated_roots(work_root_value: str, permanent_value: str) -> tuple[Path, Path]:
    work_root = _absolute_path(work_root_value, "work_root")
    permanent = _absolute_path(permanent_value, "permanent_data_dir")
    work_resolved = _resolved(work_root)
    permanent_resolved = _resolved(permanent)
    if _contains(work_resolved, permanent_resolved) or _contains(
        permanent_resolved, work_resolved
    ):
        raise RehearsalError("filesystem", "work_root_overlaps_permanent_data")
    try:
        info = work_root.lstat()
    except FileNotFoundError as exc:
        raise RehearsalError("filesystem", "work_root_missing") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or (os.name != "nt" and info.st_uid != 0)
    ):
        raise RehearsalError("filesystem", "unsafe_work_root")
    return work_root, permanent


def _validate_evidence_outputs(args: argparse.Namespace, permanent: Path) -> None:
    permanent_resolved = _resolved(permanent)
    seen: set[Path] = set()
    for field in (
        "artifact_output",
        "manifest_output",
        "source_snapshot_output",
        "snapshot_output",
        "result_output",
    ):
        path = _absolute_path(getattr(args, field), field)
        resolved = _resolved(path)
        if resolved in seen:
            raise RehearsalError("filesystem", "duplicate_evidence_output")
        seen.add(resolved)
        if _contains(permanent_resolved, resolved):
            raise RehearsalError("filesystem", "evidence_overlaps_permanent_data")
        if path.exists() or path.is_symlink():
            raise RehearsalError("filesystem", "evidence_output_exists")
        try:
            parent_info = path.parent.lstat()
        except FileNotFoundError as exc:
            raise RehearsalError("filesystem", "evidence_parent_missing") from exc
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or stat.S_IMODE(parent_info.st_mode) != 0o700
            or (os.name != "nt" and parent_info.st_uid != 0)
        ):
            raise RehearsalError("filesystem", "unsafe_evidence_parent")
    if not Path(args.artifact_output).name.endswith(".dump"):
        raise RehearsalError("filesystem", "invalid_artifact_suffix")


def _validate_source_password(path_value: str) -> None:
    path = cutover._regular_input(
        path_value, "source_password_file", max_bytes=64 * 1024
    )
    info = path.lstat()
    if os.name != "nt" and (
        stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != 0
    ):
        raise RehearsalError("filesystem", "unsafe_source_password_file")


def _inspect_image(
    docker_binary: str,
    image_ref: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    if not _POSTGRES_DIGEST_RE.fullmatch(image_ref):
        raise RehearsalError("configuration", "postgres_image_not_official_digest")
    result = _run(
        [docker_binary, "image", "inspect", image_ref],
        run=run,
    )
    try:
        payload = json.loads(result.stdout)[0]
    except (IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RehearsalError("postgres_image", "invalid_image_inspect") from exc
    if (
        result.returncode != 0
        or not isinstance(payload, dict)
        or not _IMAGE_ID_RE.fullmatch(str(payload.get("Id") or ""))
        or not any(
            isinstance(item, str)
            and item.startswith("postgres@")
            and item.rsplit("@", 1)[-1] == image_ref.rsplit("@", 1)[1]
            for item in (payload.get("RepoDigests") or [])
        )
        or "PG_MAJOR=15" not in ((payload.get("Config") or {}).get("Env") or [])
    ):
        raise RehearsalError("postgres_image", "image_identity_mismatch")
    return str(payload["Id"])


def _validate_network(
    docker_binary: str,
    name: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    if not _NETWORK_RE.fullmatch(name) or name in {"host", "none"}:
        raise RehearsalError("configuration", "invalid_source_network")
    result = _run([docker_binary, "network", "inspect", name], run=run)
    try:
        payload = json.loads(result.stdout)[0]
        gateway = payload["IPAM"]["Config"][0]["Gateway"]
        parsed = ipaddress.ip_address(gateway)
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RehearsalError("docker", "invalid_source_network_inspect") from exc
    if (
        result.returncode != 0
        or payload.get("Driver") != "bridge"
        or (payload.get("Labels") or {}).get(_NETWORK_LABEL) != _NETWORK_LABEL_VALUE
        or not parsed.is_private
    ):
        raise RehearsalError("docker", "unsafe_source_network")
    return str(parsed)


def _create_json(path_value: str, payload: Mapping[str, Any]) -> Path:
    path = Path(path_value)
    encoded = (json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        if created:
            path.unlink(missing_ok=True)
        raise RehearsalError("filesystem", "evidence_create_failed") from exc
    return path


def _validate_cloud_sql_and_proxy(args: argparse.Namespace) -> dict[str, Any]:
    if args.proxy_pid <= 1 or not 1 <= args.source_port <= 65535:
        raise RehearsalError("configuration", "invalid_proxy_endpoint")
    try:
        ipaddress.ip_address(args.expected_proxy_address)
    except ValueError as exc:
        raise RehearsalError("configuration", "invalid_proxy_address") from exc
    evidence_path = cutover._regular_input(
        args.cloud_sql_instance_evidence_file,
        "cloud_sql_instance_evidence",
        max_bytes=1024 * 1024,
    )
    info = evidence_path.lstat()
    if os.name != "nt" and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600):
        raise RehearsalError("filesystem", "unsafe_cloud_sql_instance_evidence")
    try:
        instance = json.loads(evidence_path.read_bytes())
    except json.JSONDecodeError as exc:
        raise RehearsalError("source_identity", "invalid_cloud_sql_evidence") from exc
    expected_connection = (
        f"{args.source_project_id}:{instance.get('region')}:{args.source_instance}"
        if isinstance(instance, dict)
        else ""
    )
    if (
        not isinstance(instance, dict)
        or instance.get("name") != args.source_instance
        or instance.get("databaseVersion") != "POSTGRES_15"
        or instance.get("connectionName") != expected_connection
    ):
        raise RehearsalError("source_identity", "cloud_sql_identity_mismatch")

    if not _SHA256_RE.fullmatch(args.expected_proxy_executable_sha256):
        raise RehearsalError("configuration", "invalid_proxy_executable_sha256")

    def process_start_ticks(value: str) -> str:
        closing = value.rfind(")")
        tail = value[closing + 1 :].split() if closing >= 0 else []
        # The tail starts at proc field 3; starttime is field 22.
        if len(tail) <= 19:
            raise RehearsalError("source_identity", "invalid_proxy_process_state")
        return tail[19]

    proc = Path("/proc") / str(args.proxy_pid)
    try:
        before = (proc / "stat").read_text(encoding="utf-8")
        executable = (proc / "exe").resolve(strict=True)
        executable_info = executable.stat()
        executable_hash = cutover._file_sha256(executable)
        raw_argv = (proc / "cmdline").read_bytes()
        after = (proc / "stat").read_text(encoding="utf-8")
        status_text = (proc / "status").read_text(encoding="utf-8")
    except OSError as exc:
        raise RehearsalError("source_identity", "proxy_process_unavailable") from exc
    uid_line = next(
        (line for line in status_text.splitlines() if line.startswith("Uid:")),
        "",
    )
    try:
        effective_uid = int(uid_line.split()[2])
    except (IndexError, ValueError) as exc:
        raise RehearsalError("source_identity", "invalid_proxy_process_owner") from exc
    if (
        not stat.S_ISREG(executable_info.st_mode)
        or (
            os.name != "nt"
            and (
                executable_info.st_uid not in {0, effective_uid}
                or executable_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            )
        )
    ):
        raise RehearsalError("source_identity", "untrusted_proxy_executable")
    before_start = process_start_ticks(before)
    after_start = process_start_ticks(after)
    if (
        before_start != after_start
        or executable_hash != args.expected_proxy_executable_sha256
    ):
        raise RehearsalError("source_identity", "proxy_executable_mismatch")
    try:
        argv = [item.decode("utf-8") for item in raw_argv.rstrip(b"\0").split(b"\0")]
    except UnicodeDecodeError as exc:
        raise RehearsalError("source_identity", "invalid_proxy_argv") from exc
    connection = str(instance["connectionName"])
    address_ok = f"--address={args.expected_proxy_address}" in argv or any(
        argv[index : index + 2] == ["--address", args.expected_proxy_address]
        for index in range(len(argv) - 1)
    )
    port_ok = f"--port={args.source_port}" in argv or any(
        argv[index : index + 2] == ["--port", str(args.source_port)]
        for index in range(len(argv) - 1)
    )
    if (
        not argv
        or connection not in argv
        or not address_ok
        or not port_ok
    ):
        raise RehearsalError("source_identity", "proxy_argv_mismatch")
    return {
        "cloud_sql_instance_sha256": cutover._file_sha256(evidence_path),
        "connection_name": connection,
        "proxy_pid": args.proxy_pid,
        "proxy_process_start_ticks": before_start,
        "proxy_effective_uid": effective_uid,
        "proxy_executable": str(executable),
        "proxy_executable_sha256": executable_hash,
        "proxy_address": args.expected_proxy_address,
        "proxy_port": args.source_port,
        "proxy_argv_sha256": hashlib.sha256(raw_argv).hexdigest(),
    }


class _SnapshotClient:
    def __init__(self, client: cutover.DockerPsql, snapshot_id: str):
        if not _SNAPSHOT_RE.fullmatch(snapshot_id):
            raise RehearsalError("source_snapshot", "invalid_snapshot_id")
        self._client = client
        self.snapshot_id = snapshot_id
        self.database = client.database

    def query_json(self, sql: str) -> Any:
        escaped = self.snapshot_id.replace("'", "''")
        wrapped = (
            "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;"
            f"SET TRANSACTION SNAPSHOT '{escaped}';{sql}COMMIT;"
        )
        return self._client.query_json(wrapped)


class _ExportedSnapshot:
    def __init__(self, client: cutover.DockerPsql, *, popen=subprocess.Popen):
        self.client = client
        argv = client.argv(app_name="blueprint-cutover-rehearsal-snapshot")
        # This psql session must remain open to keep the exported snapshot
        # valid. Force machine-readable output even though commands arrive on
        # stdin; otherwise readline() receives psql's column header.
        argv.extend(["--tuples-only", "--no-align"])
        self.process = popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=client.environment(app_name="blueprint-cutover-rehearsal-snapshot"),
            text=True,
        )
        self.stderr: list[str] = []
        self._closed = False
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self._stderr_thread = threading.Thread(
            target=lambda: self.stderr.append(self.process.stderr.read()), daemon=True
        )
        self._stderr_thread.start()
        self.process.stdin.write(
            "BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE READ ONLY DEFERRABLE;\n"
            "SELECT pg_export_snapshot();\n"
        )
        self.process.stdin.flush()
        output: queue.Queue[str] = queue.Queue(maxsize=1)
        threading.Thread(
            target=lambda: output.put(self.process.stdout.readline()), daemon=True
        ).start()
        try:
            self.snapshot_id = output.get(timeout=60).strip()
        except queue.Empty as exc:
            self.close()
            raise RehearsalError("source_snapshot", "export_timeout") from exc
        if not _SNAPSHOT_RE.fullmatch(self.snapshot_id):
            self.close()
            raise RehearsalError("source_snapshot", "export_failed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.process.poll() is None and self.process.stdin is not None:
            try:
                self.process.stdin.write("ROLLBACK;\n\\q\n")
                self.process.stdin.flush()
                self.process.stdin.close()
                self.process.wait(timeout=30)
            except (BrokenPipeError, OSError, ValueError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait()
        self._stderr_thread.join(timeout=10)
        if self._stderr_thread.is_alive():
            raise RehearsalError("cleanup", "snapshot_stderr_drain_timeout")


def _container_payload(
    docker_binary: str,
    name: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any] | None:
    result = _run([docker_binary, "container", "inspect", name], run=run)
    if result.returncode != 0:
        detail = f"{result.stdout}\n{result.stderr}".lower()
        if "no such object" in detail or "no such container" in detail:
            return None
        raise RehearsalError("docker", "container_inspect_failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RehearsalError("docker", "invalid_container_inspect") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RehearsalError("docker", "invalid_container_inspect")
    return payload[0]


def _assert_owned_container(payload: Mapping[str, Any], session_id: str) -> None:
    labels = ((payload.get("Config") or {}).get("Labels") or {})
    name = str(payload.get("Name") or "").lstrip("/")
    if (
        labels.get(_OWNER_LABEL) != _OWNER_VALUE
        or labels.get(_SESSION_LABEL) != session_id
        or not name.startswith(_SESSION_PREFIX)
        or name == _PERMANENT_CONTAINER
    ):
        raise RehearsalError("cleanup", "refusing_unowned_container")


def _remove_owned_container(
    docker_binary: str,
    name: str,
    session_id: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    if name == _PERMANENT_CONTAINER or not name.startswith(_SESSION_PREFIX):
        raise RehearsalError("cleanup", "refusing_permanent_container")
    payload = _container_payload(docker_binary, name, run=run)
    if payload is None:
        return
    _assert_owned_container(payload, session_id)
    result = _run([docker_binary, "container", "rm", "--force", name], timeout=180, run=run)
    if result.returncode != 0:
        raise RehearsalError("cleanup", "container_remove_failed")
    if _container_payload(docker_binary, name, run=run) is not None:
        raise RehearsalError("cleanup", "container_still_present")


def _remove_session_dir(session_dir: Path, work_root: Path, session_id: str) -> None:
    expected = _resolved(work_root) / f"{_SESSION_PREFIX}{session_id}"
    if (
        _resolved(session_dir) != expected
        or session_dir.parent != work_root
        or not session_dir.name.startswith(_SESSION_PREFIX)
    ):
        raise RehearsalError("cleanup", "refusing_unsafe_session_directory")
    if session_dir.is_symlink():
        raise RehearsalError("cleanup", "refusing_symlink_session_directory")
    if session_dir.exists():
        shutil.rmtree(session_dir)
    if session_dir.exists():
        raise RehearsalError("cleanup", "session_directory_still_present")


def _create_session(work_root: Path, session_id: str) -> tuple[Path, Path, Path]:
    session_dir = work_root / f"{_SESSION_PREFIX}{session_id}"
    try:
        session_dir.mkdir(mode=0o700)
        data_dir = session_dir / "postgres"
        data_dir.mkdir(mode=0o700)
        password_file = session_dir / "postgres-password"
        descriptor = os.open(
            password_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.write(descriptor, secrets.token_urlsafe(48).encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if os.name != "nt":
            os.chown(data_dir, _POSTGRES_UID, _POSTGRES_GID)
        return session_dir, data_dir, password_file
    except Exception:
        if session_dir.exists() and not session_dir.is_symlink():
            shutil.rmtree(session_dir)
        raise


def _labels(session_id: str) -> list[str]:
    return [
        "--label",
        f"{_OWNER_LABEL}={_OWNER_VALUE}",
        "--label",
        f"{_SESSION_LABEL}={session_id}",
    ]


def _start_source_client(
    docker_binary: str,
    name: str,
    session_id: str,
    image_id: str,
    network: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    if _container_payload(docker_binary, name, run=run) is not None:
        raise RehearsalError("docker", "generated_container_name_collision")
    result = _run(
        [
            docker_binary,
            "run",
            "--detach",
            "--name",
            name,
            *_labels(session_id),
            "--network",
            network,
            "--user",
            f"{_POSTGRES_UID}:{_POSTGRES_GID}",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
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
            image_id,
            "sleep",
            "infinity",
        ],
        run=run,
    )
    if result.returncode != 0:
        raise RehearsalError("source_client", "container_start_failed")


def _assert_source_isolation(
    docker_binary: str,
    name: str,
    session_id: str,
    image_id: str,
    network: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    payload = _container_payload(docker_binary, name, run=run)
    if payload is None:
        raise RehearsalError("source_client", "container_disappeared")
    _assert_owned_container(payload, session_id)
    host = payload.get("HostConfig") or {}
    networks = ((payload.get("NetworkSettings") or {}).get("Networks") or {})
    if (
        payload.get("Image") != image_id
        or set(networks) != {network}
        or bool(payload.get("Mounts"))
        or bool(host.get("PortBindings"))
        or host.get("ReadonlyRootfs") is not True
        or host.get("CapDrop") != ["ALL"]
        or host.get("SecurityOpt") != ["no-new-privileges"]
    ):
        raise RehearsalError("source_client", "isolation_mismatch")


def _start_target(
    docker_binary: str,
    name: str,
    session_id: str,
    image_id: str,
    data_dir: Path,
    password_file: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    if _container_payload(docker_binary, name, run=run) is not None:
        raise RehearsalError("docker", "generated_container_name_collision")
    result = _run(
        [
            docker_binary,
            "run",
            "--detach",
            "--name",
            name,
            *_labels(session_id),
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/run/postgresql:rw,noexec,nosuid,size=16m,uid=999,gid=999,mode=0755",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--mount",
            f"type=bind,src={data_dir},dst=/var/lib/postgresql/data",
            "--mount",
            f"type=bind,src={password_file},dst=/run/secrets/postgres_password,readonly",
            "--env",
            "POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password",
            "--env",
            "POSTGRES_USER=blueprint",
            "--env",
            "POSTGRES_DB=blueprint",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "4g",
            "--cpus",
            "2",
            "--health-cmd",
            "pg_isready --username=blueprint --dbname=blueprint",
            "--health-interval",
            "2s",
            "--health-timeout",
            "5s",
            "--health-retries",
            "60",
            image_id,
        ],
        timeout=180,
        run=run,
    )
    if result.returncode != 0:
        raise RehearsalError("target", "container_start_failed")


def _wait_healthy(
    docker_binary: str,
    name: str,
    session_id: str,
    *,
    timeout_seconds: int,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = _container_payload(docker_binary, name, run=run)
        if payload is None:
            raise RehearsalError("target", "container_disappeared")
        _assert_owned_container(payload, session_id)
        state = payload.get("State") or {}
        if state.get("Running") is not True:
            raise RehearsalError("target", "container_not_running")
        health = (state.get("Health") or {}).get("Status")
        if health == "healthy":
            return
        if health == "unhealthy":
            raise RehearsalError("target", "container_unhealthy")
        time.sleep(1)
    raise RehearsalError("target", "health_timeout")


def _assert_target_isolation(
    docker_binary: str,
    name: str,
    session_id: str,
    image_id: str,
    data_dir: Path,
    password_file: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    payload = _container_payload(docker_binary, name, run=run)
    if payload is None:
        raise RehearsalError("target", "container_disappeared")
    _assert_owned_container(payload, session_id)
    host = payload.get("HostConfig") or {}
    mounts = payload.get("Mounts") or []
    destinations = {
        str(item.get("Destination")): item
        for item in mounts
        if isinstance(item, dict)
    }
    data_mount = destinations.get("/var/lib/postgresql/data") or {}
    password_mount = destinations.get("/run/secrets/postgres_password") or {}
    if (
        payload.get("Image") != image_id
        or host.get("NetworkMode") != "none"
        or bool(host.get("PortBindings"))
        or set(destinations)
        != {"/var/lib/postgresql/data", "/run/secrets/postgres_password"}
        or _resolved(Path(str(data_mount.get("Source", "")))) != _resolved(data_dir)
        or _resolved(Path(str(password_mount.get("Source", ""))))
        != _resolved(password_file)
        or password_mount.get("RW") is not False
    ):
        raise RehearsalError("target", "isolation_mismatch")


def _validate_expected_revision(snapshot: Mapping[str, Any], expected: str) -> None:
    revisions = snapshot.get("alembic_revisions")
    if revisions != [expected]:
        raise RehearsalError("verification", "alembic_revision_mismatch")


def _artifact_manifest(
    args: argparse.Namespace,
    archive: Mapping[str, Any],
    source_snapshot_path: Path,
    source_identity: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "created_at": cutover._utcnow(),
        "purpose": "precutover_restore_rehearsal_only",
        "final_cutover_eligible": False,
        "source": {
            "project_id": args.source_project_id,
            "instance": args.source_instance,
            "database": args.source_database,
            "identity": dict(source_identity),
        },
        "consistency": {
            "method": "single_exported_serializable_deferrable_snapshot",
            "production_write_fence_required": False,
        },
        "artifact_path": str(archive["path"]),
        "artifact": {
            "format": "postgres-custom",
            "sha256": archive["sha256"],
            "size_bytes": archive["size_bytes"],
        },
        "source_snapshot_sha256": cutover._file_sha256(source_snapshot_path),
        "pg_dump": {
            "major_version": 15,
            "client_version": archive["client_version"],
            "serializable_deferrable_snapshot_export": True,
            "no_owner": True,
            "no_acl": True,
            "stderr_sha256": archive["stderr_sha256"],
            "stderr_bytes": archive["stderr_bytes"],
        },
    }


def run_rehearsal(
    args: argparse.Namespace,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    if os.name != "nt" and os.geteuid() != 0:
        raise RehearsalError("configuration", "root_required")
    args.docker_binary = _trusted_binary(args.docker_binary, "docker_binary")
    image_id = _inspect_image(args.docker_binary, args.postgres_image_id, run=run)
    source_gateway = _validate_network(
        args.docker_binary, args.source_network, run=run
    )
    if (
        args.source_host != source_gateway
        or args.expected_proxy_address != source_gateway
    ):
        raise RehearsalError("source_identity", "source_endpoint_not_network_gateway")
    work_root, permanent = validate_isolated_roots(
        args.work_root, args.permanent_data_dir
    )
    _validate_evidence_outputs(args, permanent)
    _validate_source_password(args.source_password_file)
    source_project = _safe_name(args.source_project_id, "source_project_id")
    source_instance = _safe_name(args.source_instance, "source_instance")
    expected_revision = _safe_name(args.expected_alembic_revision, "expected_revision")
    args.source_project_id = source_project
    args.source_instance = source_instance
    source_identity = _validate_cloud_sql_and_proxy(args)

    session_id = uuid.uuid4().hex
    source_name = f"{_SESSION_PREFIX}{session_id}-source"
    target_name = f"{_SESSION_PREFIX}{session_id}-target"
    session_dir, data_dir, password_file = _create_session(work_root, session_id)
    cleanup_errors: list[Exception] = []
    archive: dict[str, Any] | None = None
    manifest_path: Path | None = None
    restored: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None
    snapshot_path: Path | None = None
    source_snapshot_path: Path | None = None
    source_snapshot: dict[str, Any] | None = None
    keeper: _ExportedSnapshot | None = None
    try:
        _start_source_client(
            args.docker_binary,
            source_name,
            session_id,
            image_id,
            args.source_network,
            run=run,
        )
        _assert_source_isolation(
            args.docker_binary,
            source_name,
            session_id,
            image_id,
            args.source_network,
            run=run,
        )
        source = cutover.DockerPsql(
            container=source_name,
            database=args.source_database,
            user=args.source_user,
            password_file=args.source_password_file,
            host=args.source_host,
            port=args.source_port,
            docker_binary=args.docker_binary,
            run=run,
        )
        keeper = _ExportedSnapshot(source)
        source_snapshot = cutover.snapshot_database(
            _SnapshotClient(source, keeper.snapshot_id),
            label="precutover-live-source",
            instance=args.source_instance,
        )
        _validate_expected_revision(source_snapshot, expected_revision)
        archive = cutover.create_custom_dump(
            source,
            args.artifact_output,
            timeout_seconds=args.dump_timeout_seconds,
            snapshot_id=keeper.snapshot_id,
        )
        if _validate_cloud_sql_and_proxy(args) != source_identity:
            raise RehearsalError("source_identity", "proxy_identity_changed")
        keeper.close()
        keeper = None
        source_snapshot_path = _create_json(
            args.source_snapshot_output, source_snapshot
        )
        manifest_path = _create_json(
            args.manifest_output,
            _artifact_manifest(
                args, archive, source_snapshot_path, source_identity
            ),
        )
        _remove_owned_container(
            args.docker_binary, source_name, session_id, run=run
        )

        _start_target(
            args.docker_binary,
            target_name,
            session_id,
            image_id,
            data_dir,
            password_file,
            run=run,
        )
        _wait_healthy(
            args.docker_binary,
            target_name,
            session_id,
            timeout_seconds=args.target_start_timeout_seconds,
            run=run,
        )
        _assert_target_isolation(
            args.docker_binary,
            target_name,
            session_id,
            image_id,
            data_dir,
            password_file,
            run=run,
        )
        target = cutover.DockerPsql(
            container=target_name,
            database="blueprint",
            user="blueprint",
            docker_binary=args.docker_binary,
            run=run,
        )
        cutover.target_public_schema_empty(target)
        artifact_fd, _path, _info = cutover._open_held_regular(
            str(archive["path"]), "rehearsal_artifact"
        )
        try:
            restored = cutover.stream_restore(
                target,
                artifact_fd,
                app_name="blueprint-cutover-rehearsal-restore",
                timeout_seconds=args.restore_timeout_seconds,
            )
        finally:
            os.close(artifact_fd)
        snapshot = cutover.snapshot_database(
            target,
            label="precutover-disposable-target",
            instance="disposable-rehearsal",
        )
        _validate_expected_revision(snapshot, expected_revision)
        cutover.compare_snapshots(
            source_snapshot,
            snapshot,
            source_owner=args.source_user,
            target_owner="blueprint",
            # PostgreSQL sequence state is not MVCC, so it cannot be bound to
            # the exported snapshot while production writes continue. Both
            # snapshots still prove every sequence is ahead of its own owned
            # column maximum. Exact equality remains mandatory for cutover.
            compare_sequences=False,
        )
        snapshot_path = _create_json(args.snapshot_output, snapshot)
    finally:
        if keeper is not None:
            try:
                keeper.close()
            except Exception as exc:
                cleanup_errors.append(exc)
        for name in (source_name, target_name):
            try:
                _remove_owned_container(
                    args.docker_binary, name, session_id, run=run
                )
            except Exception as exc:
                cleanup_errors.append(exc)
        if not cleanup_errors:
            try:
                _remove_session_dir(session_dir, work_root, session_id)
            except Exception as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            raise RehearsalError("cleanup", "incomplete_cleanup") from cleanup_errors[0]

    if (
        archive is None
        or manifest_path is None
        or restored is None
        or snapshot is None
        or snapshot_path is None
        or source_snapshot is None
        or source_snapshot_path is None
    ):
        raise RehearsalError("rehearsal", "incomplete_result")
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "outcome": "success",
        "completed_at": cutover._utcnow(),
        "purpose": "precutover_restore_rehearsal_only",
        "final_cutover_eligible": False,
        "artifact_sha256": archive["sha256"],
        "manifest_sha256": cutover._file_sha256(manifest_path),
        "target_snapshot_sha256": cutover._file_sha256(snapshot_path),
        "source_snapshot_sha256": cutover._file_sha256(source_snapshot_path),
        "expected_alembic_revision": expected_revision,
        "restore": restored,
        "verification": {
            "alembic_revisions": snapshot["alembic_revisions"],
            "critical_integrity": snapshot["critical_integrity"],
            "table_counts": snapshot["table_counts"],
        },
        "isolation": {
            "permanent_container_used": False,
            "permanent_data_directory_used": False,
            "published_ports": False,
            "target_network": "none",
            "disposable_container_removed": True,
            "disposable_data_directory_removed": True,
        },
    }
    _create_json(args.result_output, result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-image-id", required=True)
    parser.add_argument("--docker-binary", default="/usr/bin/docker")
    parser.add_argument("--source-network", required=True)
    parser.add_argument("--source-host", required=True)
    parser.add_argument("--source-port", required=True, type=int)
    parser.add_argument("--source-database", required=True)
    parser.add_argument("--source-user", required=True)
    parser.add_argument("--source-password-file", required=True)
    parser.add_argument("--source-project-id", required=True)
    parser.add_argument("--source-instance", required=True)
    parser.add_argument("--cloud-sql-instance-evidence-file", required=True)
    parser.add_argument("--proxy-pid", required=True, type=int)
    parser.add_argument("--expected-proxy-executable-sha256", required=True)
    parser.add_argument("--expected-proxy-address", required=True)
    parser.add_argument("--expected-alembic-revision", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--permanent-data-dir", required=True)
    parser.add_argument("--artifact-output", required=True)
    parser.add_argument("--manifest-output", required=True)
    parser.add_argument("--snapshot-output", required=True)
    parser.add_argument("--source-snapshot-output", required=True)
    parser.add_argument("--result-output", required=True)
    parser.add_argument("--dump-timeout-seconds", type=int, default=7200)
    parser.add_argument("--restore-timeout-seconds", type=int, default=7200)
    parser.add_argument("--target-start-timeout-seconds", type=int, default=180)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        run_rehearsal(args)
        print(f"pre-cutover restore rehearsal succeeded: {args.result_output}")
        return 0
    except (OSError, RehearsalError, cutover.CutoverError, ValueError) as exc:
        if isinstance(exc, (RehearsalError, cutover.CutoverError)):
            detail = str(exc)
        else:
            detail = type(exc).__name__
        print(f"ERROR: pre-cutover restore rehearsal failed: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Stream a local PostgreSQL backup into the independent recovery bucket."""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, NamedTuple

_DATABASE_CONTAINER = "blueprint-local-postgres"
_DATABASE_ALIAS = "blueprint-postgres"
_NETWORK_LABEL = "dev.blueprint.purpose"
_NETWORK_LABEL_VALUE = "local-control-plane"
_PASSWORD_TARGET = "/run/secrets/postgres_password"
_CREDENTIALS_TARGET = "/run/secrets/gcp.json"
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_POSTGRES_DIGEST_RE = re.compile(
    r"^(?:(?:docker\.io/)?library/)?postgres:15@sha256:[0-9a-f]{64}$"
)
_CLOUDSDK_DIGEST_RE = re.compile(
    r"^(?:gcr\.io/google\.com/cloudsdktool/cloud-sdk|"
    r"google/cloud-sdk)(?::[^@\s]+)?@sha256:[0-9a-f]{64}$"
)
_NETWORK_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_BUCKET_RE = re.compile(r"^gs://[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
_LOCK_NAME = "\0blueprint-local-postgres-offhost-backup"
_TIMEOUT_SECONDS = 3600
_ARCHIVE_PREFIX = "local-postgres-exports"
_MANIFEST_SCHEMA_VERSION = 1
_SECRET_STAGE_ROOT = Path("/run/blueprint-local-postgres-backup")


class StreamResult(NamedTuple):
    dump_returncode: int
    gzip_returncode: int
    upload_returncode: int
    size_bytes: int
    sha256: str


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{name} contains control characters")
    return value


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _detail(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "").strip()


def _inspect(argv: list[str], subject: str) -> dict[str, Any]:
    result = _run(argv)
    if result.returncode != 0:
        raise RuntimeError(f"could not inspect {subject}: {_detail(result)}")
    try:
        payload = json.loads(result.stdout)
        item = payload[0]
    except (IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Docker returned invalid inspect data for {subject}") from exc
    if not isinstance(item, dict):
        raise RuntimeError(f"Docker returned invalid inspect data for {subject}")
    return item


def _image_id(image: str, *, postgres: bool) -> str:
    allowed = _IMAGE_ID_RE.fullmatch(image)
    allowed = allowed or (
        _POSTGRES_DIGEST_RE.fullmatch(image)
        if postgres
        else _CLOUDSDK_DIGEST_RE.fullmatch(image)
    )
    if not allowed:
        kind = "PostgreSQL 15" if postgres else "Cloud SDK"
        raise ValueError(f"{kind} image must use an immutable sha256 ID or digest")
    payload = _inspect(["docker", "image", "inspect", image], f"image {image}")
    resolved = payload.get("Id")
    if not isinstance(resolved, str) or not _IMAGE_ID_RE.fullmatch(resolved):
        raise RuntimeError("Docker image has no immutable image ID")
    if _IMAGE_ID_RE.fullmatch(image) and resolved != image:
        raise RuntimeError("Docker resolved a different image ID")
    if postgres and "PG_MAJOR=15" not in ((payload.get("Config") or {}).get("Env") or []):
        raise RuntimeError("database image is not PostgreSQL major version 15")
    if "@sha256:" in image:
        digest = image.rsplit("@", 1)[1]
        if not any(
            isinstance(item, str) and item.rsplit("@", 1)[-1] == digest
            for item in (payload.get("RepoDigests") or [])
        ):
            raise RuntimeError("Docker image does not carry the requested digest")
    return resolved


def _assert_cloud_sdk(image: str) -> None:
    result = _run(
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
        ]
    )
    if result.returncode != 0:
        raise RuntimeError("pinned uploader image does not provide a working gcloud")


def _secure_file_contents(value: str, label: str) -> bytes:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    if any(component in {".", ".."} for component in path.parts):
        raise ValueError(f"{label} contains unsafe path components")

    directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    file_fd: int | None = None
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{label} must be a regular, non-symlink file")
        if info.st_uid != os.getuid():
            raise ValueError(f"{label} must be owned by uid {os.getuid()}")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError(f"{label} must have mode 0600")
        with os.fdopen(file_fd, "rb", closefd=False) as stream:
            contents = stream.read()
        if not contents:
            raise ValueError(f"{label} must not be empty")
        return contents
    except OSError as exc:
        raise ValueError(f"{label} must not contain symlink path components") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def _secure_file(value: str, label: str) -> Path:
    path = Path(value)
    _secure_file_contents(value, label)
    return path


@contextlib.contextmanager
def _staged_secret(contents: bytes, label: str) -> Iterator[Path]:
    _SECRET_STAGE_ROOT.mkdir(mode=0o700, parents=False, exist_ok=True)
    root_info = _SECRET_STAGE_ROOT.lstat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.getuid()
        or stat.S_IMODE(root_info.st_mode) != 0o700
    ):
        raise ValueError("backup secret staging directory is unsafe")
    descriptor, name = tempfile.mkstemp(
        prefix="secret-", dir=str(_SECRET_STAGE_ROOT)
    )
    staged = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(descriptor)
        yield staged
    finally:
        os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            staged.unlink()


def _credential_contents(value: str, project_id: str) -> bytes:
    contents = _secure_file_contents(value, "backup credentials file")
    try:
        payload = json.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("backup credentials are not valid JSON") from exc
    expected = f"blueprint-local-db-backup@{project_id}.iam.gserviceaccount.com"
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "service_account"
        or payload.get("project_id") != project_id
        or payload.get("client_email") != expected
    ):
        raise ValueError("backup credentials are not the dedicated writer identity")
    return contents


def _credentials(value: str, project_id: str) -> Path:
    path = Path(value)
    _credential_contents(value, project_id)
    return path


def _network(name: str) -> None:
    if not _NETWORK_RE.fullmatch(name):
        raise ValueError("BLUEPRINT_CONTROL_PLANE_NETWORK is unsafe")
    payload = _inspect(["docker", "network", "inspect", name], f"network {name}")
    if (payload.get("Labels") or {}).get(_NETWORK_LABEL) != _NETWORK_LABEL_VALUE:
        raise RuntimeError("control-plane network is not owned by Blueprint")
    if payload.get("Driver") != "bridge":
        raise RuntimeError("control-plane network is not a bridge")
    try:
        gateway = payload["IPAM"]["Config"][0]["Gateway"]
        parsed = ipaddress.ip_address(gateway)
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("control-plane network has no valid gateway") from exc
    if not parsed.is_private:
        raise RuntimeError("control-plane network does not use private addressing")


def _database_container(image_id: str, network: str) -> None:
    payload = _inspect(
        ["docker", "container", "inspect", _DATABASE_CONTAINER],
        f"container {_DATABASE_CONTAINER}",
    )
    if payload.get("Image") != image_id:
        raise RuntimeError("running database container uses a different image")
    if (payload.get("Config") or {}).get("Labels", {}).get(
        "dev.blueprint.service"
    ) != "local-postgres":
        raise RuntimeError("running database container is not owned by Blueprint")
    state = payload.get("State") or {}
    if state.get("Running") is not True or state.get("Dead") is True:
        raise RuntimeError("local PostgreSQL container is not running")
    host = payload.get("HostConfig") or {}
    if host.get("NetworkMode") != network or host.get("PortBindings"):
        raise RuntimeError("local PostgreSQL container has unsafe networking")
    networks = (payload.get("NetworkSettings") or {}).get("Networks") or {}
    selected = networks.get(network) or {}
    if set(networks) != {network} or _DATABASE_ALIAS not in (selected.get("Aliases") or []):
        raise RuntimeError("local PostgreSQL container has a mismatched private alias")


@contextlib.contextmanager
def _single_flight() -> Iterator[None]:
    lock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            lock.bind(_LOCK_NAME)
        except OSError as exc:
            raise RuntimeError("another local PostgreSQL backup is already running") from exc
        yield
    finally:
        lock.close()


def _dump_command(image: str, network: str, password_file: Path) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        network,
        "--mount",
        f"type=bind,src={password_file},dst={_PASSWORD_TARGET},readonly",
        "--env",
        f"POSTGRES_PASSWORD_FILE={_PASSWORD_TARGET}",
        "--memory",
        "1g",
        "--cpus",
        "1",
        "--pids-limit",
        "128",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--log-driver",
        "none",
        image,
        "/bin/sh",
        "-ceu",
        (
            'export PGPASSWORD="$(cat "$POSTGRES_PASSWORD_FILE")"; '
            "exec pg_dump --format=plain --no-owner --no-privileges "
            f"--host={_DATABASE_ALIAS} --port=5432 "
            "--username=blueprint --dbname=blueprint"
        ),
    ]


def _gzip_command(image: str) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--pull",
        "never",
        "--network",
        "none",
        "--memory",
        "256m",
        "--cpus",
        "0.5",
        "--pids-limit",
        "32",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--log-driver",
        "none",
        image,
        "gzip",
        "-6",
        "-c",
    ]


def _upload_command(
    image: str, network: str, credentials: Path, project_id: str, object_uri: str
) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--pull",
        "never",
        "--network",
        network,
        "--mount",
        f"type=bind,src={credentials},dst={_CREDENTIALS_TARGET},readonly",
        "--env",
        f"GOOGLE_APPLICATION_CREDENTIALS={_CREDENTIALS_TARGET}",
        "--env",
        f"CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE={_CREDENTIALS_TARGET}",
        "--env",
        f"CLOUDSDK_CORE_PROJECT={project_id}",
        "--memory",
        "512m",
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
        image,
        "gcloud",
        "storage",
        "cp",
        "-",
        object_uri,
        "--quiet",
    ]


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _drain(stream: Any, sink: list[bytes]) -> None:
    sink.append(stream.read() if stream is not None else b"")


def _stream(
    dump_argv: list[str], gzip_argv: list[str], upload_argv: list[str]
) -> StreamResult:
    dump: subprocess.Popen[bytes] | None = None
    gzip: subprocess.Popen[bytes] | None = None
    upload: subprocess.Popen[bytes] | None = None
    relay_error: list[BaseException] = []
    digest = hashlib.sha256()
    size = [0]
    try:
        dump = subprocess.Popen(
            dump_argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert dump.stdout is not None
        gzip = subprocess.Popen(
            gzip_argv,
            stdin=dump.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        dump.stdout.close()
        assert gzip.stdout is not None
        upload = subprocess.Popen(
            upload_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert upload.stdin is not None

        diagnostics: list[list[bytes]] = [[], [], [], []]
        readers = [
            threading.Thread(target=_drain, args=(dump.stderr, diagnostics[0]), daemon=True),
            threading.Thread(target=_drain, args=(gzip.stderr, diagnostics[1]), daemon=True),
            threading.Thread(target=_drain, args=(upload.stdout, diagnostics[2]), daemon=True),
            threading.Thread(target=_drain, args=(upload.stderr, diagnostics[3]), daemon=True),
        ]
        for reader in readers:
            reader.start()

        def relay() -> None:
            try:
                while True:
                    chunk = gzip.stdout.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size[0] += len(chunk)
                    upload.stdin.write(chunk)
                upload.stdin.close()
            except BaseException as exc:  # propagated by the supervising thread
                relay_error.append(exc)
                with contextlib.suppress(OSError):
                    upload.stdin.close()

        relay_thread = threading.Thread(target=relay, daemon=True)
        relay_thread.start()
        deadline = time.monotonic() + _TIMEOUT_SECONDS
        while relay_thread.is_alive() or any(
            process.poll() is None for process in (dump, gzip, upload)
        ):
            if relay_error:
                raise RuntimeError("backup upload stream failed") from relay_error[0]
            if upload.poll() not in (None, 0):
                raise RuntimeError("backup upload container failed")
            if time.monotonic() >= deadline:
                raise RuntimeError("local PostgreSQL backup timed out")
            time.sleep(0.1)
        relay_thread.join(timeout=5)
        for reader in readers:
            reader.join(timeout=5)
        if relay_error:
            raise RuntimeError("backup upload stream failed") from relay_error[0]
        return StreamResult(
            dump_returncode=int(dump.returncode),
            gzip_returncode=int(gzip.returncode),
            upload_returncode=int(upload.returncode),
            size_bytes=size[0],
            sha256=digest.hexdigest(),
        )
    finally:
        _terminate(upload)
        _terminate(gzip)
        _terminate(dump)


def _upload_manifest(upload_argv: list[str], payload: dict[str, Any]) -> None:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    try:
        result = subprocess.run(
            upload_argv,
            input=encoded,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("backup success-manifest upload timed out") from exc
    if result.returncode != 0:
        raise RuntimeError("backup success-manifest upload failed")


def _event(outcome: str, message: str, object_uri: str = "") -> None:
    print(
        json.dumps(
            {
                "event": "local_postgres_offhost_backup",
                "outcome": outcome,
                "message": message,
                "object_uri": object_uri,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def backup() -> int:
    with _single_flight():
        project_id = _required_env("BLUEPRINT_POSTGRES_BACKUP_PROJECT_ID")
        if not _PROJECT_RE.fullmatch(project_id):
            raise ValueError("BLUEPRINT_POSTGRES_BACKUP_PROJECT_ID is invalid")
        bucket = _required_env("BLUEPRINT_POSTGRES_BACKUP_BUCKET")
        if not _BUCKET_RE.fullmatch(bucket):
            raise ValueError("BLUEPRINT_POSTGRES_BACKUP_BUCKET must be a bucket-only gs URI")
        password_contents = _secure_file_contents(
            _required_env("BLUEPRINT_POSTGRES_SECRETS_FILE"),
            "PostgreSQL password file",
        )
        credential_contents = _credential_contents(
            _required_env("BLUEPRINT_POSTGRES_BACKUP_CREDENTIALS_FILE"),
            project_id,
        )
        postgres_image = _required_env("BLUEPRINT_POSTGRES_IMAGE_ID")
        postgres_id = _image_id(postgres_image, postgres=True)
        uploader_image = _required_env("BLUEPRINT_POSTGRES_BACKUP_IMAGE_ID")
        _image_id(uploader_image, postgres=False)
        _assert_cloud_sdk(uploader_image)
        network = _required_env("BLUEPRINT_CONTROL_PLANE_NETWORK")
        _network(network)
        _database_container(postgres_id, network)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        archive_uuid = uuid.uuid4().hex
        object_uri = f"{bucket}/{_ARCHIVE_PREFIX}/{stamp}-{archive_uuid}.sql.gz"
        manifest_uri = f"{object_uri}.manifest.json"
        with (
            _staged_secret(password_contents, "PostgreSQL password file") as password_file,
            _staged_secret(credential_contents, "backup credentials file") as credentials,
        ):
            result = _stream(
                _dump_command(postgres_image, network, password_file),
                _gzip_command(postgres_image),
                _upload_command(
                    uploader_image, network, credentials, project_id, object_uri
                ),
            )
        if result.dump_returncode != 0:
            raise RuntimeError("pg_dump container failed")
        if result.gzip_returncode != 0:
            raise RuntimeError("gzip container failed")
        if result.upload_returncode != 0:
            raise RuntimeError("backup upload container failed")
        if result.size_bytes <= 0:
            raise RuntimeError("backup archive is empty")
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        manifest = {
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "archive_id": archive_uuid,
            "archive_uri": object_uri,
            "archive_name": object_uri.removeprefix(f"{bucket}/"),
            "created_at": created_at,
            "database": "blueprint",
            "format": "plain-sql-gzip",
            "postgres_major": 15,
            "sha256": result.sha256,
            "size_bytes": result.size_bytes,
        }
        with _staged_secret(
            credential_contents, "backup credentials file"
        ) as credentials:
            _upload_manifest(
                _upload_command(
                    uploader_image, network, credentials, project_id, manifest_uri
                ),
                manifest,
            )
        _event("success", "backup_uploaded", object_uri)
        return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args:
        print("usage: local_postgres_backup_service.py", file=sys.stderr)
        return 2
    try:
        return backup()
    except (OSError, RuntimeError, ValueError) as exc:
        _event("failure", str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

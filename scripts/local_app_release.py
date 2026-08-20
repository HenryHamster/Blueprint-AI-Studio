#!/usr/bin/env python3
"""Activate a reviewed Blueprint app image without touching local PostgreSQL.

The command is intentionally root-only and hamsterwheel-specific. It pulls an
Artifact Registry image by digest, verifies its OCI revision and bundled UI,
then atomically updates only the API and worker role environment files. A
schema-changing candidate uses a bounded stop -> migrate -> start transaction;
once that downtime begins, failure is held for fix-forward rather than rolling
an older image onto a potentially advanced schema. Other activation or
health-check failures restore both prior files and services.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import frontend_release_manifest as frontend_manifest
from release_preconditions import (
    PRE_STOP_PRECONDITIONS,
    RehearsalReport,
    verify_privilege_boundary,
)

_IMAGE_PREFIX = (
    "us-central1-docker.pkg.dev/blueprint-orchestrator/blueprint-repo/"
    "blueprint-orchestrator"
)
_IMAGE_REF_RE = re.compile(rf"^{re.escape(_IMAGE_PREFIX)}@sha256:[0-9a-f]{{64}}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_SCHEMA_REVISION_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_EXPECTED_SCHEMA_REVISION_LINE_RE = re.compile(
    r"^EXPECTED_SCHEMA_REVISION\s*=\s*(['\"])(?P<revision>[A-Za-z0-9_.-]+)\1$"
)
_SCHEMA_REACHABILITY_PROBE = """
import ast
import sys
from pathlib import Path

revisions = {}
for path in Path('/app/alembic/versions').glob('*.py'):
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        values = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {'revision', 'down_revision'}:
                    values[target.id] = ast.literal_eval(node.value)
        revision = values.get('revision')
        down_revision = values.get('down_revision')
        if not isinstance(revision, str):
            raise ValueError('missing revision')
        if down_revision is None:
            parents = ()
        elif isinstance(down_revision, str):
            parents = (down_revision,)
        elif isinstance(down_revision, (tuple, list)) and all(isinstance(item, str) for item in down_revision):
            parents = tuple(down_revision)
        else:
            raise ValueError('invalid down_revision')
        if revision in revisions:
            raise ValueError('duplicate revision')
        revisions[revision] = parents
    except Exception:
        print('unreachable')
        raise SystemExit(0)

current, target = sys.argv[1:3]
pending = [target]
visited = set()
reachable = False
while pending:
    revision = pending.pop()
    if revision == current:
        reachable = True
        break
    if revision in visited or revision not in revisions:
        continue
    visited.add(revision)
    pending.extend(revisions[revision])
print('reachable' if reachable else 'unreachable')
""".strip()
_SYSTEM_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_SERVICES = ("blueprint-api.service", "blueprint-worker.service")
_ACTIVATION_ORDER = ("blueprint-worker.service", "blueprint-api.service")
# App units may legitimately be crash-looping (activating/failed) or absent
# (inactive) when a release runs — e.g. a migration-carrying deploy left the
# prior image unable to satisfy the new schema-version gate.  Replacing that
# broken image is exactly what a release does, so these states must not block
# it; the image validation in _validate_managed_container stays the hard safety
# gate against acting on an unexpected image.  Any other ActiveState is
# unexpected and still refuses the release.
_RECOVERABLE_APP_STATES = frozenset(
    {"active", "activating", "deactivating", "inactive", "failed"}
)
_UNTOUCHED_UNITS = (
    "blueprint-database.service",
    "blueprint-database-backup.timer",
    "blueprint-edge.service",
)
_CONTAINERS = {
    "blueprint-api.service": "blueprint-local-api",
    "blueprint-worker.service": "blueprint-local-worker",
}
_DATABASE_CONTAINER = "blueprint-local-postgres"
# Must remain identical to backend/app/lifecycle.py's single-bigint background
# leadership key split into the unsigned pg_locks OID halves.
_BACKGROUND_LOCK_CLASSID = 2_984_976_049
_BACKGROUND_LOCK_OBJID = 3_944_657_387
_EVIDENCE_ROOT = Path("/var/lib/blueprint/local-app-releases")
_LOCK_PATH = Path("/run/lock/blueprint-local-app-release.lock")
_PROTECTED_UNIT_FILES = (
    Path("/etc/systemd/system/blueprint-database.service"),
    Path("/etc/systemd/system/blueprint-database-backup.service"),
    Path("/etc/systemd/system/blueprint-database-backup.timer"),
    Path("/etc/systemd/system/blueprint-edge.service"),
)
_PROTECTED_UNIT_DIRS = (Path("/etc/systemd/system/blueprint-edge.service.d"),)
_PUBLIC_ORIGIN_RE = re.compile(r"^https://[^/]+$")
_REGISTRY_HOST = "us-central1-docker.pkg.dev"
_WORKER_REGISTRY_READER = (
    "blueprint-local-worker-runtime@blueprint-orchestrator.iam.gserviceaccount.com"
)
RUNBOOK_ENTRYPOINT = "release"


class _TransientNotReady(RuntimeError):
    """A bounded readiness poll may retry this state."""


def _is_root() -> bool:
    return os.geteuid() == 0


def _run(
    argv: list[str],
    *,
    timeout: int = 180,
    check: bool = True,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            input=input_text,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timed out ({argv[0]})") from exc
    if check and result.returncode != 0:
        diagnostic = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"command failed ({argv[0]}): {diagnostic}")
    return result


def _parse_systemctl_properties(payload: str, expected: set[str]) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in payload.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in expected or key in properties:
            raise RuntimeError("invalid systemctl property output")
        properties[key] = value
    if set(properties) != expected:
        raise RuntimeError("incomplete systemctl property output")
    return properties


def _service_marker(unit: str) -> tuple[str, str]:
    properties = _parse_systemctl_properties(
        _run(
            [
                "/usr/bin/systemctl",
                "show",
                unit,
                "--property=ActiveState",
                "--property=ActiveEnterTimestampMonotonic",
            ]
        ).stdout,
        {"ActiveState", "ActiveEnterTimestampMonotonic"},
    )
    return properties["ActiveState"], properties["ActiveEnterTimestampMonotonic"]


def _secure_role_file(path: Path) -> bytes:
    info = path.lstat()
    parent = path.parent.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError(f"unsafe role environment file: {path}")
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != 0
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise ValueError(f"unsafe role environment directory: {path.parent}")
    return path.read_bytes()


def _trusted_root_directory(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise ValueError(f"unsafe trusted directory: {path}")


def _canonical_https_origin(value: str, *, field: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field} must be a canonical HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{field} must be a canonical HTTPS origin")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    if port is not None:
        host = f"{host}:{port}"
    return f"https://{host}"


def _public_origin(api_role: bytes, service_uid: int) -> str:
    secrets_path = Path(_role_value(api_role, "BLUEPRINT_API_SECRETS_FILE"))
    if not secrets_path.is_absolute():
        raise ValueError("BLUEPRINT_API_SECRETS_FILE must be absolute")
    info = secrets_path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != service_uid
        or info.st_nlink != 1
        or info.st_mode & 0o077
    ):
        raise ValueError("unsafe API secrets file")
    payload = json.loads(secrets_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("API secrets must be a JSON object")
    value = payload.get("ORCHESTRATOR_URL")
    if not isinstance(value, str):
        raise ValueError("API secrets are missing ORCHESTRATOR_URL")
    return _canonical_https_origin(value, field="ORCHESTRATOR_URL")


def _uid_for_user(username: str) -> int:
    import pwd

    return pwd.getpwnam(username).pw_uid


def _validate_manifest(path: Path, install_user: str, node_root: Path) -> None:
    payload = json.loads(_secure_role_file(path).decode("utf-8"))
    if (
        payload.get("profile") != "local-control-plane"
        or payload.get("scope") != "system"
        or payload.get("node_root") != str(node_root)
    ):
        raise ValueError(
            "installed node manifest is not the system local-control-plane"
        )
    roles = payload.get("roles")
    if not isinstance(roles, list):
        raise ValueError("installed node manifest has invalid roles")
    expected_envs = {
        str(node_root / "roles" / role / "role.env")
        for role in ("database", "database-backup", "api", "worker", "edge")
    }
    actual_envs = {role.get("env_file") for role in roles if isinstance(role, dict)}
    if actual_envs != expected_envs:
        raise ValueError(
            f"installed node manifest has unexpected role paths for {install_user}"
        )


@contextlib.contextmanager
def _exclusive_lock():
    import fcntl

    fd = os.open(
        _LOCK_PATH,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("unsafe local app release lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another local app release is already running") from exc
        yield
    finally:
        os.close(fd)


def _replace_role_values(payload: bytes, replacements: dict[str, str]) -> bytes:
    text = payload.decode("utf-8")
    lines = text.splitlines(keepends=True)
    found = {key: 0 for key in replacements}
    output: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in replacements:
            found[key] += 1
            output.append(f"{key}={replacements[key]}\n")
        else:
            output.append(line)
    for key, count in found.items():
        if count > 1:
            raise ValueError(f"duplicate {key} in role environment")
        if count == 0:
            output.append(f"{key}={replacements[key]}\n")
    return "".join(output).encode("utf-8")


def _role_value(payload: bytes, key: str) -> str:
    values = [
        line.split("=", 1)[1]
        for line in payload.decode("utf-8").splitlines()
        if line.startswith(f"{key}=")
    ]
    if len(values) != 1:
        raise ValueError(f"role environment must contain exactly one {key}")
    return values[0]


def _secure_registry_credential(path_text: str, expected_uid: int) -> str:
    path = Path(path_text)
    if not path.is_absolute():
        raise ValueError("worker Google credentials path must be absolute")
    fd = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
    )
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != expected_uid
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValueError("unsafe worker Google credentials file")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            text = handle.read()
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("worker Google credentials are not valid JSON") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "service_account"
        or payload.get("project_id") != "blueprint-orchestrator"
        or payload.get("client_email") != _WORKER_REGISTRY_READER
        or not isinstance(payload.get("private_key"), str)
    ):
        raise ValueError("worker Google credentials have the wrong identity")
    return text


def _remove_registry_auth_directory(directory: Path, expected_uid: int) -> None:
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != expected_uid
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RuntimeError("unsafe registry credential directory")
    entries = list(directory.iterdir())
    if any(entry.name != "config.json" for entry in entries):
        raise RuntimeError("registry credential directory contains unexpected files")
    for entry in entries:
        entry_info = entry.lstat()
        if (
            not stat.S_ISREG(entry_info.st_mode)
            or stat.S_ISLNK(entry_info.st_mode)
            or entry_info.st_uid != expected_uid
            or entry_info.st_nlink != 1
            or stat.S_IMODE(entry_info.st_mode) & 0o077
        ):
            raise RuntimeError("unsafe registry credential config")
        entry.unlink()
    directory.rmdir()


def _cleanup_stale_registry_auth(expected_uid: int) -> None:
    for directory in sorted(Path("/run").glob("blueprint-registry-auth-*")):
        if re.fullmatch(r"blueprint-registry-auth-[A-Za-z0-9_-]{6,16}", directory.name) is None:
            raise RuntimeError("unexpected registry credential directory name")
        _remove_registry_auth_directory(directory, expected_uid)


def _pull_candidate_for_rehearsal(image_ref: str, install_user: str) -> None:
    """Pull the exact candidate using the same ephemeral auth as activation."""
    node_root = Path("/etc/blueprint/nodes") / install_user
    worker_path = node_root / "roles" / "worker" / "role.env"
    for directory in (
        Path("/etc/blueprint"),
        Path("/etc/blueprint/nodes"),
        node_root,
        node_root / "roles",
        node_root / "roles" / "worker",
    ):
        _trusted_root_directory(directory)
    worker_role = _secure_role_file(worker_path)
    install_uid = _uid_for_user(install_user)
    credential = _secure_registry_credential(
        _role_value(worker_role, "BLUEPRINT_WORKER_GOOGLE_CREDENTIALS_FILE"),
        install_uid,
    )
    with _registry_auth(credential) as registry_config:
        _run(
            [
                "/usr/bin/docker",
                "--config",
                str(registry_config),
                "pull",
                image_ref,
            ],
            timeout=600,
        )


@contextlib.contextmanager
def _registry_auth(credential_text: str) -> Iterator[Path]:
    expected_uid = os.geteuid()
    _cleanup_stale_registry_auth(expected_uid)
    directory = Path(tempfile.mkdtemp(prefix="blueprint-registry-auth-", dir="/run"))
    os.chmod(directory, 0o700)
    cleanup_errors: list[str] = []
    try:
        try:
            _run(
                [
                    "/usr/bin/docker",
                    "--config",
                    str(directory),
                    "login",
                    "--username",
                    "_json_key",
                    "--password-stdin",
                    f"https://{_REGISTRY_HOST}",
                ],
                input_text=credential_text,
            )
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("registry login failed") from exc
        credential_text = ""
        yield directory
    finally:
        try:
            logout = _run(
                [
                    "/usr/bin/docker",
                    "--config",
                    str(directory),
                    "logout",
                    _REGISTRY_HOST,
                ],
                check=False,
            )
            if logout.returncode != 0:
                cleanup_errors.append("docker logout failed")
        except (OSError, RuntimeError):
            cleanup_errors.append("docker logout failed")
        finally:
            try:
                _remove_registry_auth_directory(directory, expected_uid)
            except (OSError, RuntimeError) as exc:
                cleanup_errors.append(f"credential removal failed: {exc}")
        if cleanup_errors:
            raise RuntimeError(
                "registry credential cleanup failed: " + "; ".join(cleanup_errors)
            )


def _atomic_write(path: Path, payload: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary, 0, 0)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _inspect_candidate(image_ref: str, revision: str, bundle_sha256: str) -> str:
    record = json.loads(
        _run(["/usr/bin/docker", "image", "inspect", image_ref]).stdout
    )[0]
    image_id = record.get("Id", "")
    if not _IMAGE_ID_RE.fullmatch(image_id):
        raise RuntimeError("candidate did not resolve to an immutable image ID")
    if image_ref not in (record.get("RepoDigests") or []):
        raise RuntimeError("candidate RepoDigests do not contain the requested digest")
    labels = (record.get("Config") or {}).get("Labels") or {}
    if labels.get("org.opencontainers.image.revision") != revision:
        raise RuntimeError("candidate OCI revision does not match expected revision")
    if labels.get("com.blueprint.frontend.bundle-sha256") != bundle_sha256:
        raise RuntimeError("candidate OCI frontend digest does not match its manifest")
    if record.get("Architecture") != "amd64":
        raise RuntimeError("candidate architecture is not amd64")
    return image_id


def _extract_frontend_tar(stream, target: Path) -> None:
    """Extract a Docker tar stream while enforcing the reviewed bundle bounds."""

    seen: set[str] = set()
    file_count = 0
    member_count = 0
    total_bytes = 0
    max_members = frontend_manifest.MAX_FILES * 2 + 32
    max_bytes = frontend_manifest.MAX_BYTES + 1024 * 1024
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for member in archive:
            member_count += 1
            if member_count > max_members or len(member.name) > 1024:
                raise RuntimeError("candidate frontend archive exceeds member limits")
            path = PurePosixPath(member.name)
            if path.is_absolute() or any(
                part == ".." or any(ord(char) < 32 or ord(char) == 127 for char in part)
                for part in path.parts
            ):
                raise RuntimeError("candidate frontend archive has an unsafe path")
            parts = tuple(part for part in path.parts if part not in ("", "."))
            if not parts:
                if member.isdir():
                    continue
                raise RuntimeError("candidate frontend archive has an empty file path")
            relative = PurePosixPath(*parts).as_posix()
            if relative in seen:
                raise RuntimeError("candidate frontend archive has duplicate paths")
            seen.add(relative)
            destination = target.joinpath(*parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                if not destination.is_dir():
                    raise RuntimeError("candidate frontend archive path type changed")
                destination.chmod(0o700)
                continue
            if not member.isreg():
                raise RuntimeError("candidate frontend archive has a non-regular file")
            file_count += 1
            total_bytes += member.size
            if (
                file_count > frontend_manifest.MAX_FILES + 1
                or member.size > frontend_manifest.MAX_BYTES
                or total_bytes > max_bytes
            ):
                raise RuntimeError("candidate frontend archive exceeds size limits")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("candidate frontend archive member is unreadable")
            remaining = member.size
            with destination.open("xb") as output:
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RuntimeError("candidate frontend archive is truncated")
                    output.write(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            destination.chmod(0o600)


def _copy_frontend_bounded(container: str, target: Path) -> None:
    with tempfile.TemporaryFile() as diagnostics:
        process = subprocess.Popen(
            [
                "/usr/bin/docker",
                "cp",
                f"{container}:/app/static/.",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=diagnostics,
        )
        try:
            if process.stdout is None:
                raise RuntimeError("docker cp did not expose its archive stream")
            _extract_frontend_tar(process.stdout, target)
            process.stdout.close()
            return_code = process.wait(timeout=30)
            if return_code != 0:
                diagnostics.seek(0)
                message = diagnostics.read(4096).decode("utf-8", errors="replace")
                raise RuntimeError(f"docker cp failed: {message.strip()}")
        except Exception:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            raise


def _verify_frontend(image_ref: str, revision: str) -> str:
    container = f"blueprint-release-verify-{os.getpid()}"
    with tempfile.TemporaryDirectory(prefix="blueprint-release-") as temporary:
        target = Path(temporary)
        _run(
            [
                "/usr/bin/docker",
                "create",
                "--name",
                container,
                "--entrypoint",
                "/bin/true",
                "--network",
                "none",
                image_ref,
            ]
        )
        try:
            _copy_frontend_bounded(container, target)
        finally:
            _run(["/usr/bin/docker", "rm", "--force", container], check=False)
        index = target / "index.html"
        if not index.is_file() or "/assets/" not in index.read_text(encoding="utf-8"):
            raise RuntimeError("candidate does not contain a complete frontend index")
        revision_found = False
        for child in target.rglob("*"):
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeError("candidate frontend contains a symlink")
            if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
                raise RuntimeError("candidate frontend has unsafe ownership or mode")
            if not child.is_file():
                continue
            if info.st_nlink != 1:
                raise RuntimeError("candidate frontend contains a hard-linked file")
            if child.suffix == ".js" and revision.encode("ascii") in child.read_bytes():
                revision_found = True
        if not revision_found:
            raise RuntimeError(
                "candidate frontend does not embed the expected revision"
            )
        try:
            manifest = frontend_manifest.verify_manifest(target, revision)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "candidate frontend release manifest is invalid"
            ) from exc
        return str(manifest["tree_sha256"])


def _wait_verified(
    operation: Callable[[], None],
    description: str,
    *,
    timeout: float = 120,
    interval: float = 2,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            operation()
            return
        except urllib.error.HTTPError as exc:
            if exc.code not in {502, 503, 504}:
                raise
            last_error = exc
        except (
            ConnectionError,
            TimeoutError,
            urllib.error.URLError,
            _TransientNotReady,
        ) as exc:
            last_error = exc
        time.sleep(interval)
    raise RuntimeError(f"{description} did not become ready: {last_error}")


def _container_record(name: str) -> dict[str, object] | None:
    result = _run(
        ["/usr/bin/docker", "container", "inspect", name], check=False
    )
    if result.returncode != 0:
        diagnostic = (result.stderr or result.stdout).strip()
        if "No such container" in diagnostic:
            return None
        raise RuntimeError(f"cannot inspect {name}: {diagnostic}")
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid container inspection for {name}") from exc
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise RuntimeError(f"ambiguous container inspection for {name}")
    return records[0]


def _validate_managed_container(
    name: str, record: dict[str, object], allowed_images: frozenset[str]
) -> str:
    container_id = record.get("Id")
    image_id = record.get("Image")
    config = record.get("Config")
    host_config = record.get("HostConfig")
    mounts = record.get("Mounts")
    expected_role = "api" if name == "blueprint-local-api" else "worker"
    if (
        not isinstance(container_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", container_id) is None
        or record.get("Name") != f"/{name}"
        or image_id not in allowed_images
        or not isinstance(config, dict)
        or config.get("Image") != image_id
        or not isinstance(host_config, dict)
        or host_config.get("AutoRemove") is not True
        or host_config.get("ReadonlyRootfs") is not True
        or host_config.get("NetworkMode") != "blueprint-control-plane"
        or not isinstance(config.get("Env"), list)
        or f"BLUEPRINT_PROCESS_ROLE={expected_role}" not in config["Env"]
        or not isinstance(mounts, list)
        or not any(
            isinstance(mount, dict)
            and mount.get("Type") == "bind"
            and mount.get("Destination") == "/var/run/blueprint/gcp.json"
            and mount.get("RW") is False
            for mount in mounts
        )
    ):
        raise RuntimeError(f"refusing to stop unrecognized container {name}")
    labels = config.get("Labels")
    if name == "blueprint-local-api" and (
        not isinstance(labels, dict) or labels.get("com.blueprint.role") != "api"
    ):
        raise RuntimeError("refusing to stop API container without role label")
    return container_id


def _unit_stop_state(unit: str) -> tuple[str, str]:
    properties = _parse_systemctl_properties(
        _run(
            [
                "/usr/bin/systemctl",
                "show",
                unit,
                "--property=ActiveState",
                "--property=MainPID",
            ]
        ).stdout,
        {"ActiveState", "MainPID"},
    )
    return properties["ActiveState"], properties["MainPID"]


def _wait_unit_stopped(
    unit: str,
    container: str,
    captured_id: str | None,
    *,
    timeout: float = 90,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        named = _container_record(container)
        if named is not None and named.get("Id") != captured_id:
            raise RuntimeError(f"container name {container} was reused during stop")
        captured = _container_record(captured_id) if captured_id is not None else None
        active_state, main_pid = _unit_stop_state(unit)
        if (
            active_state in {"inactive", "failed"}
            and main_pid == "0"
            and named is None
            and captured is None
        ):
            return
        time.sleep(1)
    raise RuntimeError(f"{unit} and {container} did not stop cleanly")


def _stop_unit_container(
    unit: str, container: str, allowed_images: frozenset[str]
) -> None:
    existing = _container_record(container)
    container_id = None
    if existing is not None:
        container_id = _validate_managed_container(
            container, existing, allowed_images
        )
    # Block until systemd has fully stopped the unit before touching its
    # container.  A non-blocking stop returns while Restart=always is still
    # armed, so a crash-looping old image (common after a migration deploy)
    # gets auto-restarted mid-swap and races the tool's own docker stop/remove
    # ("removal of container ... already in progress").  A blocking stop
    # cancels the restart cycle so the container is static when we proceed.
    _run(["/usr/bin/systemctl", "stop", unit], timeout=120)
    if container_id is not None:
        stop = _run(
            [
                "/usr/bin/docker",
                "container",
                "stop",
                "--timeout",
                "60",
                container_id,
            ],
            timeout=75,
            check=False,
        )
        remaining = _container_record(container_id)
        if remaining is not None:
            _validate_managed_container(container, remaining, allowed_images)
            # A successful stop of an AutoRemove container can return just
            # before dockerd finishes deleting it.  During that interval the
            # container is still inspectable, but a second removal fails with
            # "removal ... is already in progress".  Let the terminal-state
            # gate below observe Docker's original removal instead.  Force
            # removal is only a fallback when stop itself failed and the exact
            # captured managed container is still present.
            stop_diagnostic = (stop.stderr or stop.stdout).lower()
            removal_in_progress = (
                "removal of container" in stop_diagnostic
                and "already in progress" in stop_diagnostic
            )
            state = remaining.get("State")
            if not isinstance(state, dict) or not isinstance(
                state.get("Running"), bool
            ):
                raise RuntimeError(f"ambiguous state for managed container {container}")
            if (
                stop.returncode != 0
                and state["Running"]
                and not removal_in_progress
            ):
                remove = _run(
                    ["/usr/bin/docker", "container", "rm", "--force", container_id],
                    timeout=30,
                    check=False,
                )
                if remove.returncode != 0:
                    remove_diagnostic = (remove.stderr or remove.stdout).lower()
                    benign_remove_race = "no such container" in remove_diagnostic or (
                        "removal of container" in remove_diagnostic
                        and "already in progress" in remove_diagnostic
                    )
                    if not benign_remove_race:
                        raise RuntimeError(
                            f"failed to remove managed container {container_id}"
                        )
    _wait_unit_stopped(unit, container, container_id)


def _wait_service_container(unit: str, container: str, image_id: str) -> None:
    def verify() -> None:
        _runtime_state(unit)
        _verify_container(container, image_id)

    _wait_verified(verify, f"{unit} container", timeout=120, interval=1)


def _restart_service(
    unit: str,
    image_id: str,
    *,
    allowed_images: frozenset[str],
) -> None:
    container = _CONTAINERS[unit]
    _stop_unit_container(unit, container, allowed_images)
    _start_service(unit, image_id, allowed_images=allowed_images)


def _start_service(
    unit: str,
    image_id: str,
    *,
    allowed_images: frozenset[str],
) -> None:
    """Start a previously stopped managed unit on the selected image."""
    container = _CONTAINERS[unit]
    _run(["/usr/bin/systemctl", "reset-failed", unit], check=False)
    _run(["/usr/bin/systemctl", "start", unit], timeout=120)
    _wait_service_container(unit, container, image_id)


def _runtime_state(unit: str) -> tuple[str, str, str]:
    properties = _parse_systemctl_properties(
        _run(
            [
                "/usr/bin/systemctl",
                "show",
                unit,
                "--property=ActiveState",
                "--property=MainPID",
                "--property=NRestarts",
            ]
        ).stdout,
        {"ActiveState", "MainPID", "NRestarts"},
    )
    if properties["ActiveState"] != "active" or properties["MainPID"] == "0":
        raise _TransientNotReady(f"{unit} is not stably active")
    return (
        properties["ActiveState"],
        properties["MainPID"],
        properties["NRestarts"],
    )


def _verify_services_stable(*, observation_seconds: float = 40) -> None:
    before = {unit: _runtime_state(unit) for unit in _SERVICES}
    time.sleep(observation_seconds)
    after = {unit: _runtime_state(unit) for unit in _SERVICES}
    if after != before:
        raise RuntimeError("API or worker restarted during the observation window")


def _verify_container(name: str, image_id: str) -> None:
    record = _container_record(name)
    if record is None:
        raise _TransientNotReady(f"{name} does not exist yet")
    if record.get("Image") != image_id:
        raise RuntimeError(f"{name} is running the wrong image")
    state = record.get("State")
    if not isinstance(state, dict):
        raise RuntimeError(f"{name} has malformed state metadata")
    if state.get("Running") is not True:
        raise _TransientNotReady(f"{name} is not running yet")


def _local_version_payload() -> bytes:
    status, _headers, payload = _fetch("http://127.0.0.1:8080/api/version")
    if status != 200:
        raise RuntimeError("local API version endpoint failed")
    return payload


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        del request, file_pointer, code, message, headers, new_url
        return None


def _fetch(
    url: str, *, expected_origin: str | None = None
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "blueprint-release/1"})
    opener = urllib.request.build_opener(_RejectRedirects())
    with opener.open(request, timeout=20) as response:
        if expected_origin is not None:
            final = urlsplit(response.geturl())
            final_origin = f"{final.scheme}://{final.netloc}"
            if final_origin != expected_origin:
                raise RuntimeError("public health request changed origin")
        return response.status, dict(response.headers.items()), response.read(2_000_000)


def _verify_worker_ready() -> None:
    record = _container_record("blueprint-local-worker")
    if record is None:
        raise _TransientNotReady("worker container does not exist yet")
    container_id = record.get("Id")
    if not isinstance(container_id, str) or re.fullmatch(
        r"[0-9a-f]{64}", container_id
    ) is None:
        raise RuntimeError("worker container has no trusted immutable ID")
    state = record.get("State")
    if not isinstance(state, dict) or state.get("Running") is not True:
        raise _TransientNotReady("worker container is not running yet")
    health_result = _run(
        [
            "/usr/bin/docker",
            "exec",
            container_id,
            "/usr/local/bin/python",
            "-c",
            (
                "import sys,urllib.request;"
                "r=urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=5);"
                "sys.stdout.buffer.write(r.read(200000))"
            ),
        ],
        timeout=10,
        check=False,
    )
    if health_result.returncode != 0:
        raise _TransientNotReady("worker health endpoint is not ready")
    try:
        worker_health = json.loads(health_result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("worker health endpoint returned invalid JSON") from exc
    if (
        not isinstance(worker_health, dict)
        or worker_health.get("status") not in {"healthy", "degraded"}
        or worker_health.get("is_leader") is not True
        or worker_health.get("orchestrator_alive") is not True
    ):
        raise _TransientNotReady("worker health endpoint is not leader-ready")
    query = (
        "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
        "WHERE l.locktype='advisory' AND l.mode='ExclusiveLock' AND l.granted "
        f"AND l.classid='{_BACKGROUND_LOCK_CLASSID}'::oid "
        f"AND l.objid='{_BACKGROUND_LOCK_OBJID}'::oid AND l.objsubid=1 "
        "AND a.usename='blueprint' AND a.datname='blueprint' "
        "AND a.application_name='blueprint-worker'"
    )
    result = _run(
        [
            "/usr/bin/docker",
            "exec",
            "--user=postgres",
            _DATABASE_CONTAINER,
            "psql",
            "--username=blueprint",
            "--dbname=blueprint",
            "--tuples-only",
            "--no-align",
            "--command",
            query,
        ]
    )
    try:
        leader_locks = int(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("worker leadership query returned invalid output") from exc
    if leader_locks == 0:
        raise _TransientNotReady(
            "worker does not yet hold the background leadership lock"
        )
    if leader_locks != 1:
        raise RuntimeError("multiple worker background leadership locks detected")


def _public_snapshot(public_url: str) -> tuple[bytes, str]:
    _status, _headers, version = _fetch(
        public_url + "/api/version", expected_origin=public_url
    )
    status, _headers, root = _fetch(public_url + "/", expected_origin=public_url)
    if status != 200:
        raise RuntimeError("public root health check failed")
    return version, hashlib.sha256(root).hexdigest()


def _verify_health_once(public_url: str, revision: str, image_id: str) -> None:
    status, _headers, payload = _fetch("http://127.0.0.1:8080/api/version")
    version = json.loads(payload)
    if status != 200 or version.get("sha") != revision:
        raise RuntimeError("local API version does not match the candidate revision")
    status, _headers, _payload = _fetch("http://127.0.0.1:8080/health")
    if status != 200:
        raise RuntimeError("local API health endpoint failed")
    status, _headers, public_version_payload = _fetch(
        public_url + "/api/version", expected_origin=public_url
    )
    public_version = json.loads(public_version_payload)
    if status != 200 or public_version.get("sha") != revision:
        raise RuntimeError("public API version does not match the candidate revision")
    status, headers, payload = _fetch(public_url + "/", expected_origin=public_url)
    lowered_headers = {key.lower(): value for key, value in headers.items()}
    if (
        status != 200
        or b'<div id="root">' not in payload
        or "text/html" not in lowered_headers.get("content-type", "")
        or "content-security-policy-report-only" not in lowered_headers
        or lowered_headers.get("x-content-type-options") != "nosniff"
        or "no-cache" not in lowered_headers.get("cache-control", "")
    ):
        raise RuntimeError("public same-origin frontend health check failed")
    asset_match = re.search(rb'(?:src|href)="(/assets/[^"?]+)"', payload)
    if asset_match is None:
        raise RuntimeError("public frontend does not reference a bundled asset")
    asset_url = public_url + asset_match.group(1).decode("ascii")
    status, asset_headers, _payload = _fetch(asset_url, expected_origin=public_url)
    asset_headers = {key.lower(): value for key, value in asset_headers.items()}
    if status != 200 or "immutable" not in asset_headers.get("cache-control", ""):
        raise RuntimeError("public frontend immutable asset health check failed")
    for unit, container in _CONTAINERS.items():
        _runtime_state(unit)
        _verify_container(container, image_id)
    _verify_worker_ready()


def _verify_health(public_url: str, revision: str, image_id: str) -> None:
    _wait_verified(
        lambda: _verify_health_once(public_url, revision, image_id),
        "candidate API, UI, and worker health",
        timeout=180,
    )
    _verify_services_stable()
    _wait_verified(_verify_worker_ready, "worker leadership", timeout=60)


def _hash_protected_tree(node_root: Path) -> dict[str, str]:
    paths = [node_root / "manifest.json", *_PROTECTED_UNIT_FILES]
    for directory in _PROTECTED_UNIT_DIRS:
        if directory.exists():
            paths.extend(sorted(directory.rglob("*")))
    for role in ("database", "database-backup", "edge"):
        paths.extend(sorted((node_root / "roles" / role).rglob("*")))
    result: dict[str, str] = {}
    for path in paths:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"protected path is a symlink: {path}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"protected path is not regular: {path}")
        result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _assert_edge_is_decoupled() -> None:
    result = _run(
        [
            "/usr/bin/systemctl",
            "show",
            "blueprint-edge.service",
            "--property=BindsTo",
            "--value",
        ]
    )
    if "blueprint-api.service" in result.stdout.split():
        raise RuntimeError("edge still has BindsTo=blueprint-api.service")


def _retain_prior_image(image_id: str) -> str:
    tag = f"blueprint-local-retained:{image_id.removeprefix('sha256:')[:16]}"
    _run(["/usr/bin/docker", "tag", image_id, tag])
    return tag


def _image_schema_revision(image_id: str) -> str:
    result = _run(
        [
            "/usr/bin/docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "/bin/sh",
            image_id,
            "-c",
            "grep -E '^EXPECTED_SCHEMA_REVISION[[:space:]]*=' "
            "/app/app/main.py",
        ],
        timeout=60,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError(
            f"prior image {image_id} does not expose one schema revision"
        )
    match = _EXPECTED_SCHEMA_REVISION_LINE_RE.fullmatch(lines[0])
    if match is None:
        raise RuntimeError(
            f"prior image {image_id} exposes an invalid schema revision"
        )
    return match.group("revision")


def _database_schema_revision() -> str:
    result = _run(
        [
            "/usr/bin/docker",
            "exec",
            "--user=postgres",
            _DATABASE_CONTAINER,
            "psql",
            "--username=blueprint",
            "--dbname=blueprint",
            "--tuples-only",
            "--no-align",
            "--command",
            "SELECT version_num FROM alembic_version ORDER BY version_num;",
        ],
        timeout=30,
    )
    revisions = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(revisions) != 1 or not _SCHEMA_REVISION_RE.fullmatch(revisions[0]):
        raise RuntimeError("live database does not expose one schema head")
    return revisions[0]


def _run_local_migration(image_ref: str) -> None:
    """Run the canonical local migration primitive for this immutable image."""
    # The root-installed release bundle includes blueprint.py and blueprint_api.py.
    # Calling cmd_migrate keeps this path on the same hardened container contract
    # as `blueprint migrate --local`, rather than creating a second migration path.
    import blueprint

    result = blueprint.cmd_migrate(
        argparse.Namespace(
            local=True,
            image=image_ref,
            sha=None,
            worker_env_file=None,
            worker_secrets_file=None,
            registry_credential_file=None,
            network=None,
        )
    )
    if result != 0:
        raise RuntimeError(f"local migration exited {result}")


def _preflight_local_migration_inputs(image_ref: str) -> None:
    """Validate migration inputs that depend only on static host state.

    The worker-secret validation (`_migration_worker_values`) is a pure function
    of the on-disk worker role.env and secrets JSON, so a malformed secrets file
    fails deterministically whether checked now or inside the post-stop
    migration.  Running it here — BEFORE any Restart=always unit is stopped —
    means such a defect aborts the release with ZERO downtime, instead of after
    the control plane is already down.  (A too-strict version of that validation,
    reached only inside the post-stop migration, is exactly what turned the v31
    schema deploy into a control-plane outage.)  This mirrors the Namespace
    `_run_local_migration` hands `cmd_migrate`; the call is side-effect free, so
    the real migration re-runs it harmlessly.
    """
    import blueprint

    blueprint._migration_worker_values(
        argparse.Namespace(
            local=True,
            image=image_ref,
            sha=None,
            worker_env_file=None,
            worker_secrets_file=None,
            registry_credential_file=None,
            network=None,
        )
    )


def _run_pre_stop_rehearsal(
    image_id: str,
    image_ref: str,
    revision: str,
    install_user: str,
    target_schema_revision: str,
    database_schema_before: str,
) -> RehearsalReport:
    """Prove all deterministic release preconditions before stopping units."""
    verify_privilege_boundary(
        install_user,
        agent_unit="blueprint-deploy-agent.service",
        release_unit="blueprint-release.service",
        run=_run,
    )
    _preflight_local_migration_inputs(image_ref)
    target_refusal = _target_schema_refusal(
        image_id, target_schema_revision, database_schema_before
    )
    if target_refusal is not None:
        raise RuntimeError(target_refusal)
    return RehearsalReport(
        candidate=image_ref,
        revision=revision,
        checked=PRE_STOP_PRECONDITIONS,
        deferred=(
            "migration apply and post-migration schema verification (mutates live DB)",
            "unit stop/start and application health verification (requires downtime)",
        ),
    )


def rehearse(image_ref: str, revision: str) -> RehearsalReport:
    """Refuse standalone candidate execution on the production host."""
    del image_ref, revision
    raise RuntimeError(
        "host rehearsal disabled; use trusted CI build/smoke evidence"
    )


def _schema_mismatch_refusal(
    image_id: str,
    image_label: str,
    image_revision: str,
    database_revision: str,
    reason: str,
) -> str:
    return (
        f"code-rollback refused: {image_label} {image_id} expects schema "
        f"{image_revision} but DB is at {database_revision}; {reason}"
    )


def _code_rollback_refusal(prior_image_id: str) -> str | None:
    try:
        prior_revision = _image_schema_revision(prior_image_id)
        database_revision = _database_schema_revision()
    except Exception as exc:
        return (
            "code-rollback refused: could not verify schema compatibility for "
            f"prior image {prior_image_id}: {exc}; fix forward"
        )
    if prior_revision == database_revision:
        return None
    return _schema_mismatch_refusal(
        prior_image_id,
        "prior image",
        prior_revision,
        database_revision,
        "a forward migration cannot be undone by restoring the old image -- "
        "fix forward",
    )


def _target_schema_refusal(
    image_id: str, target_revision: str, database_revision: str
) -> str | None:
    """Refuse a target that is not an Alembic descendant of the live head."""
    if target_revision == database_revision:
        return None
    try:
        result = _run(
            [
                "/usr/bin/docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--workdir",
                "/app",
                "--entrypoint",
                "/usr/local/bin/python",
                image_id,
                "-c",
                _SCHEMA_REACHABILITY_PROBE,
                database_revision,
                target_revision,
            ],
            timeout=60,
            check=False,
        )
    except Exception as exc:
        detail = f"could not determine migration reachability: {exc}"
    else:
        detail = (
            "target is not forward-reachable from the live database head"
            if result.returncode != 0 or result.stdout.strip() != "reachable"
            else ""
        )
    if not detail:
        return None
    compatibility_refusal = _schema_mismatch_refusal(
        image_id,
        "candidate image",
        target_revision,
        database_revision,
        f"{detail}; fix forward",
    )
    return (
        f"release refused before stopping app units: {compatibility_refusal}; "
        "app units remain running"
    )


def _prepare_evidence_root(directory: Path) -> None:
    _trusted_root_directory(Path("/var/lib"))
    _trusted_root_directory(Path("/var/lib/blueprint"))
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("unsafe local app release evidence directory")


def _write_evidence(directory: Path, payload: dict[str, object]) -> Path:
    destination = directory / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-")
        + str(payload["revision"])[:12]
        + ".json"
    )
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        parent_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    return destination


def release(image_ref: str, revision: str) -> Path:
    if not _is_root():
        raise PermissionError("local app release must run as root")
    if not _IMAGE_REF_RE.fullmatch(image_ref):
        raise ValueError("image-ref must be the approved Artifact Registry digest")
    if not _REVISION_RE.fullmatch(revision):
        raise ValueError("expected-revision must be a full lowercase git SHA")
    install_user = os.environ.get("SUDO_USER", "")
    if not _SYSTEM_USER_RE.fullmatch(install_user) or install_user == "root":
        raise ValueError("SUDO_USER must identify the non-root service account")

    node_root = Path("/etc/blueprint/nodes") / install_user
    for directory in (
        Path("/etc/blueprint"),
        Path("/etc/blueprint/nodes"),
        node_root,
        node_root / "roles",
        *(node_root / "roles" / role for role in ("api", "worker")),
    ):
        _trusted_root_directory(directory)
    _validate_manifest(node_root / "manifest.json", install_user, node_root)
    roles_root = node_root / "roles"
    api_path = roles_root / "api" / "role.env"
    worker_path = roles_root / "worker" / "role.env"
    snapshots = {
        api_path: _secure_role_file(api_path),
        worker_path: _secure_role_file(worker_path),
    }
    install_uid = _uid_for_user(install_user)
    public_url = _public_origin(snapshots[api_path], install_uid)
    prior_api = _role_value(snapshots[api_path], "BLUEPRINT_API_IMAGE_ID")
    prior_worker = _role_value(snapshots[worker_path], "BLUEPRINT_WORKER_IMAGE_ID")
    if prior_api != prior_worker or not _IMAGE_ID_RE.fullmatch(prior_api):
        raise RuntimeError("current API and worker image IDs are not a safe pair")
    for unit in _SERVICES:
        state = _service_marker(unit)[0]
        if state not in _RECOVERABLE_APP_STATES:
            raise RuntimeError(
                f"{unit} is in unexpected state {state!r}; refusing to release"
            )
    untouched_before = {unit: _service_marker(unit) for unit in _UNTOUCHED_UNITS}
    for unit, marker in untouched_before.items():
        if marker[0] != "active":
            raise RuntimeError(f"{unit} must be active before release")
    protected_before = _hash_protected_tree(node_root)
    _assert_edge_is_decoupled()
    prior_version_payload = _local_version_payload()
    prior_public_snapshot = _public_snapshot(public_url)
    _prepare_evidence_root(_EVIDENCE_ROOT)

    registry_credential = _secure_registry_credential(
        _role_value(
            snapshots[worker_path], "BLUEPRINT_WORKER_GOOGLE_CREDENTIALS_FILE"
        ),
        install_uid,
    )
    with _registry_auth(registry_credential) as registry_config:
        _run(
            [
                "/usr/bin/docker",
                "--config",
                str(registry_config),
                "pull",
                image_ref,
            ],
            timeout=600,
        )
    registry_credential = ""
    bundle_sha256 = _verify_frontend(image_ref, revision)
    image_id = _inspect_candidate(image_ref, revision, bundle_sha256)
    retained_tag = _retain_prior_image(prior_api)
    allowed_images = frozenset({prior_api, image_id})
    target_schema_revision = _image_schema_revision(image_id)
    database_schema_before = _database_schema_revision()
    migration_required = target_schema_revision != database_schema_before

    def receipt(outcome: str, error: str, markers_match: bool) -> dict[str, object]:
        return {
            "kind": "blueprint.local_app_release.v1",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "revision": revision,
            "image_ref": image_ref,
            "image_id": image_id,
            "prior_image_id": prior_api,
            "retained_prior_tag": retained_tag,
            "target_schema_revision": target_schema_revision,
            "database_schema_before": database_schema_before,
            "migration_required": migration_required,
            "outcome": outcome,
            "error_sha256": (
                hashlib.sha256(error.encode()).hexdigest() if error else None
            ),
            "untouched_units": list(_UNTOUCHED_UNITS),
            "untouched_unit_markers_match": markers_match,
        }

    try:
        _run_pre_stop_rehearsal(
            image_id,
            image_ref,
            revision,
            install_user,
            target_schema_revision,
            database_schema_before,
        )
    except Exception as precondition_error:
        target_refusal = str(precondition_error)
        outcome = (
            "rollback_refused"
            if "release refused before stopping" in target_refusal
            else "precondition_failed"
        )
        try:
            evidence = _write_evidence(
                _EVIDENCE_ROOT,
                receipt(outcome, target_refusal, False),
            )
        except Exception as evidence_exc:
            raise RuntimeError(
                f"{target_refusal}; EVIDENCE_WRITE_FAILED: {evidence_exc}"
            ) from evidence_exc
        raise RuntimeError(f"{target_refusal}; evidence={evidence}")

    touched_units: list[str] = []
    migration_choreography_started = False
    try:
        if migration_required:
            # A schema-changing deploy cannot hot-swap: the old image rejects
            # the advanced DB and the candidate rejects the old DB.  Stop both
            # Restart=always units first, wait for each managed container to be
            # gone, then use the existing hardened migration transaction.
            migration_choreography_started = True
            for unit in _ACTIVATION_ORDER:
                _stop_unit_container(unit, _CONTAINERS[unit], allowed_images)
            _run_local_migration(image_ref)
            database_schema_after = _database_schema_revision()
            if database_schema_after != target_schema_revision:
                raise RuntimeError(
                    "local migration completed at schema "
                    f"{database_schema_after}, but target image expects "
                    f"{target_schema_revision}"
                )
        _atomic_write(
            api_path,
            _replace_role_values(
                snapshots[api_path],
                {
                    "BLUEPRINT_API_IMAGE_ID": image_id,
                    "BLUEPRINT_API_DEPLOY_SHA": revision,
                },
            ),
        )
        _atomic_write(
            worker_path,
            _replace_role_values(
                snapshots[worker_path],
                {
                    "BLUEPRINT_WORKER_IMAGE_ID": image_id,
                    "BLUEPRINT_WORKER_DEPLOY_SHA": revision,
                },
            ),
        )
        for unit in _ACTIVATION_ORDER:
            touched_units.append(unit)
            if migration_required:
                _start_service(unit, image_id, allowed_images=allowed_images)
            else:
                _restart_service(unit, image_id, allowed_images=allowed_images)
            if unit == "blueprint-worker.service":
                _wait_verified(
                    _verify_worker_ready,
                    "candidate worker leadership",
                    timeout=120,
                )
        _verify_health(public_url, revision, image_id)
        if _hash_protected_tree(node_root) != protected_before:
            raise RuntimeError("release changed protected database/backup/edge files")
        if {
            unit: _service_marker(unit) for unit in _UNTOUCHED_UNITS
        } != untouched_before:
            raise RuntimeError("release changed an untouched database/backup/edge unit")
        return _write_evidence(
            _EVIDENCE_ROOT,
            receipt("success", "", True),
        )
    except Exception as exc:
        error = str(exc)
        if migration_choreography_started:
            hold_message = (
                "migration deploy failed after bounded downtime began "
                f"({error}); app units are intentionally left stopped; "
                "do not roll back code across the migration — fix forward"
            )
            try:
                evidence = _write_evidence(
                    _EVIDENCE_ROOT,
                    receipt("migration_failed", hold_message, False),
                )
            except (OSError, PermissionError, RuntimeError, ValueError) as evidence_exc:
                raise RuntimeError(
                    f"{hold_message}; EVIDENCE_WRITE_FAILED: {evidence_exc}"
                ) from exc
            raise RuntimeError(f"{hold_message}; evidence={evidence}") from exc
        rollback_refusal = _code_rollback_refusal(prior_api)
        if rollback_refusal is not None:
            try:
                evidence = _write_evidence(
                    _EVIDENCE_ROOT,
                    receipt("rollback_refused", rollback_refusal, False),
                )
            except Exception as evidence_exc:
                raise RuntimeError(
                    f"release failed ({error}); {rollback_refusal}; "
                    f"EVIDENCE_WRITE_FAILED: {evidence_exc}"
                ) from exc
            raise RuntimeError(
                f"release failed ({error}); {rollback_refusal}; evidence={evidence}"
            ) from exc
        rollback_errors: list[str] = []
        for path, payload in snapshots.items():
            try:
                _atomic_write(path, payload)
            except Exception as rollback_exc:
                rollback_errors.append(f"restore {path}: {rollback_exc}")
        for unit in (unit for unit in _ACTIVATION_ORDER if unit in touched_units):
            try:
                _restart_service(unit, prior_api, allowed_images=allowed_images)
                if unit == "blueprint-worker.service":
                    _wait_verified(
                        _verify_worker_ready,
                        "rollback worker leadership",
                        timeout=120,
                    )
            except Exception as rollback_exc:  # pragma: no cover - emergency path
                rollback_errors.append(f"{unit}: {rollback_exc}")
        for container in _CONTAINERS.values():
            try:
                _verify_container(container, prior_api)
            except Exception as rollback_exc:
                rollback_errors.append(f"{container}: {rollback_exc}")

        def verify_local_rollback() -> None:
            if _local_version_payload() != prior_version_payload:
                raise _TransientNotReady(
                    "local API version has not returned to its prior value"
                )

        try:
            _wait_verified(
                verify_local_rollback,
                "rollback local API",
                timeout=180,
            )
        except Exception as rollback_exc:
            rollback_errors.append(f"local API version: {rollback_exc}")

        def verify_public_rollback() -> None:
            if _public_snapshot(public_url) != prior_public_snapshot:
                raise _TransientNotReady(
                    "public API/UI has not returned to its prior snapshot"
                )

        try:
            _wait_verified(
                verify_public_rollback,
                "rollback public API/UI",
                timeout=180,
            )
        except Exception as rollback_exc:
            rollback_errors.append(f"public rollback health: {rollback_exc}")
        try:
            _verify_services_stable()
            _wait_verified(
                _verify_worker_ready,
                "rollback worker stability",
                timeout=60,
            )
        except Exception as rollback_exc:
            rollback_errors.append(f"service stability: {rollback_exc}")
        try:
            if _hash_protected_tree(node_root) != protected_before:
                rollback_errors.append("protected database/backup/edge files drifted")
        except Exception as rollback_exc:
            rollback_errors.append(f"protected file verification: {rollback_exc}")
        try:
            untouched_after = {unit: _service_marker(unit) for unit in _UNTOUCHED_UNITS}
            markers_match = untouched_after == untouched_before
            if not markers_match:
                rollback_errors.append("database/backup/edge unit markers changed")
        except Exception as rollback_exc:
            markers_match = False
            rollback_errors.append(f"untouched unit verification: {rollback_exc}")
        outcome = "rollback_incomplete" if rollback_errors else "rolled_back"
        rollback_message = (
            f"release failed ({error}); ROLLBACK_INCOMPLETE: {rollback_errors}"
            if rollback_errors
            else f"release failed and was rolled back: {error}"
        )
        try:
            evidence = _write_evidence(
                _EVIDENCE_ROOT,
                receipt(outcome, error, markers_match),
            )
        except Exception as evidence_exc:
            raise RuntimeError(
                f"{rollback_message}; EVIDENCE_WRITE_FAILED: {evidence_exc}"
            ) from exc
        raise RuntimeError(f"{rollback_message}; evidence={evidence}") from exc


def _locked_release(image_ref: str, revision: str) -> Path:
    with _exclusive_lock():
        return release(image_ref, revision)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument(
        "--rehearse",
        action="store_true",
        help="disabled: refuses standalone candidate execution on the production host",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.rehearse:
            report = rehearse(args.image, args.expected_revision)
            print(
                "LOCAL_APP_RELEASE_REHEARSAL "
                + json.dumps(report.as_dict(), sort_keys=True)
            )
            return 0
        evidence = _locked_release(args.image, args.expected_revision)
    except (OSError, ValueError, RuntimeError, PermissionError) as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 1
    print(f"LOCAL_APP_RELEASE_COMPLETE evidence={evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

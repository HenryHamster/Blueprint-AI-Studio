#!/usr/bin/env python3
"""Activate a reviewed Blueprint app image without touching local PostgreSQL.

The command is intentionally root-only and hamsterwheel-specific. It pulls an
Artifact Registry image by digest, verifies its OCI revision and bundled UI,
then atomically updates only the API and worker role environment files. Any
activation or health-check failure restores both prior files and services.
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
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import frontend_release_manifest as frontend_manifest

_IMAGE_PREFIX = (
    "us-central1-docker.pkg.dev/blueprint-orchestrator/blueprint-repo/"
    "blueprint-orchestrator"
)
_IMAGE_REF_RE = re.compile(rf"^{re.escape(_IMAGE_PREFIX)}@sha256:[0-9a-f]{{64}}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_SYSTEM_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_SERVICES = ("blueprint-api.service", "blueprint-worker.service")
_ACTIVATION_ORDER = ("blueprint-worker.service", "blueprint-api.service")
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


def _is_root() -> bool:
    return os.geteuid() == 0


def _run(
    argv: list[str], *, timeout: int = 180, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
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


def _wait_active(unit: str, *, timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (
            _run(
                ["/usr/bin/systemctl", "is-active", "--quiet", unit], check=False
            ).returncode
            == 0
        ):
            return
        time.sleep(1)
    raise RuntimeError(f"{unit} did not become active")


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
        raise RuntimeError(f"{unit} is not stably active")
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
    records = json.loads(_run(["/usr/bin/docker", "container", "inspect", name]).stdout)
    if records[0].get("Image") != image_id:
        raise RuntimeError(f"{name} is not running the candidate image")


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
    log_result = _run(
        ["/usr/bin/docker", "logs", "--tail", "500", "blueprint-local-worker"]
    )
    logs = log_result.stdout + log_result.stderr
    if (
        "Application startup complete." not in logs
        or "Traceback (most recent call last)" in logs
    ):
        raise RuntimeError("worker startup log is not healthy")
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
    if leader_locks != 1:
        raise RuntimeError("worker does not hold the exact background leadership lock")


def _public_snapshot(public_url: str) -> tuple[bytes, str]:
    _status, _headers, version = _fetch(
        public_url + "/api/version", expected_origin=public_url
    )
    status, _headers, root = _fetch(public_url + "/", expected_origin=public_url)
    if status != 200:
        raise RuntimeError("public root health check failed")
    return version, hashlib.sha256(root).hexdigest()


def _verify_health(public_url: str, revision: str, image_id: str) -> None:
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
        _wait_active(unit)
        _verify_container(container, image_id)
    _verify_worker_ready()
    _verify_services_stable()
    _verify_worker_ready()


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
    public_url = _public_origin(snapshots[api_path], _uid_for_user(install_user))
    prior_api = _role_value(snapshots[api_path], "BLUEPRINT_API_IMAGE_ID")
    prior_worker = _role_value(snapshots[worker_path], "BLUEPRINT_WORKER_IMAGE_ID")
    if prior_api != prior_worker or not _IMAGE_ID_RE.fullmatch(prior_api):
        raise RuntimeError("current API and worker image IDs are not a safe pair")
    for unit in _SERVICES:
        if _service_marker(unit)[0] != "active":
            raise RuntimeError(f"{unit} must be active before release")
    untouched_before = {unit: _service_marker(unit) for unit in _UNTOUCHED_UNITS}
    for unit, marker in untouched_before.items():
        if marker[0] != "active":
            raise RuntimeError(f"{unit} must be active before release")
    protected_before = _hash_protected_tree(node_root)
    _assert_edge_is_decoupled()
    prior_version_payload = _local_version_payload()
    prior_public_snapshot = _public_snapshot(public_url)
    _prepare_evidence_root(_EVIDENCE_ROOT)

    _run(["/usr/bin/docker", "pull", image_ref], timeout=600)
    bundle_sha256 = _verify_frontend(image_ref, revision)
    image_id = _inspect_candidate(image_ref, revision, bundle_sha256)
    retained_tag = _retain_prior_image(prior_api)

    def receipt(outcome: str, error: str, markers_match: bool) -> dict[str, object]:
        return {
            "kind": "blueprint.local_app_release.v1",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "revision": revision,
            "image_ref": image_ref,
            "image_id": image_id,
            "prior_image_id": prior_api,
            "retained_prior_tag": retained_tag,
            "outcome": outcome,
            "error_sha256": (
                hashlib.sha256(error.encode()).hexdigest() if error else None
            ),
            "untouched_units": list(_UNTOUCHED_UNITS),
            "untouched_unit_markers_match": markers_match,
        }

    try:
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
                snapshots[worker_path], {"BLUEPRINT_WORKER_IMAGE_ID": image_id}
            ),
        )
        for unit in _ACTIVATION_ORDER:
            _run(["/usr/bin/systemctl", "restart", unit], timeout=120)
            _wait_active(unit)
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
        rollback_errors: list[str] = []
        for path, payload in snapshots.items():
            try:
                _atomic_write(path, payload)
            except Exception as rollback_exc:
                rollback_errors.append(f"restore {path}: {rollback_exc}")
        for unit in _ACTIVATION_ORDER:
            try:
                _run(["/usr/bin/systemctl", "restart", unit], timeout=120)
                _wait_active(unit)
            except Exception as rollback_exc:  # pragma: no cover - emergency path
                rollback_errors.append(f"{unit}: {rollback_exc}")
        for container in _CONTAINERS.values():
            try:
                _verify_container(container, prior_api)
            except Exception as rollback_exc:
                rollback_errors.append(f"{container}: {rollback_exc}")
        try:
            _verify_services_stable()
            _verify_worker_ready()
        except Exception as rollback_exc:
            rollback_errors.append(f"service stability: {rollback_exc}")
        try:
            if _local_version_payload() != prior_version_payload:
                rollback_errors.append(
                    "local API version did not return to prior value"
                )
        except Exception as rollback_exc:
            rollback_errors.append(f"local API version: {rollback_exc}")
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
        try:
            if _public_snapshot(public_url) != prior_public_snapshot:
                rollback_errors.append("public API/UI did not return to prior state")
        except Exception as rollback_exc:
            rollback_errors.append(f"public rollback health: {rollback_exc}")
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--expected-revision", required=True)
    args = parser.parse_args()
    try:
        evidence = _locked_release(args.image, args.expected_revision)
    except (OSError, ValueError, RuntimeError, PermissionError) as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 1
    print(f"LOCAL_APP_RELEASE_COMPLETE evidence={evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

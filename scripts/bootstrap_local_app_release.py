#!/usr/bin/env python3
"""Narrow, no-restart bootstrap for hamsterwheel application releases."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

_SOURCE_NAMES = (
    "frontend_release_manifest.py",
    "local_app_release.py",
    "local_api_service.py",
    "local_worker_service.py",
)
_EDGE_DROP_IN_NAME = "blueprint-edge-api-decoupling.conf"
_BUNDLE_ROOT = Path("/usr/local/lib/blueprint-node")
_API_DROP_IN = Path(
    "/etc/systemd/system/blueprint-api.service.d/20-local-app-release.conf"
)
_EDGE_DROP_IN = Path(
    "/etc/systemd/system/blueprint-edge.service.d/90-api-lifecycle.conf"
)
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_UNITS = (
    "blueprint-database.service",
    "blueprint-database-backup.timer",
    "blueprint-api.service",
    "blueprint-worker.service",
    "blueprint-edge.service",
)


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({argv[0]}): {(result.stderr or result.stdout).strip()}"
        )
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


def _trusted_file(path: Path) -> bytes:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise ValueError(f"unsafe bootstrap source: {path}")
    return path.read_bytes()


def _trusted_directory(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise ValueError(f"unsafe bootstrap source directory: {path}")


def _bundle_digest(payloads: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(payloads):
        digest.update(name.encode("ascii"))
        digest.update(b"\0")
        digest.update(payloads[name])
        digest.update(b"\n")
    return digest.hexdigest()


def _snapshot_optional(path: Path) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise ValueError(f"unsafe existing drop-in: {path}")
    return path.read_bytes()


def _atomic_write(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != 0
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise ValueError(f"unsafe bootstrap destination directory: {path.parent}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary, 0, 0)
        os.chmod(temporary, mode)
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


def _marker(unit: str) -> tuple[str, str, str]:
    property_names = {"ActiveState", "ActiveEnterTimestampMonotonic"}
    if not unit.endswith(".timer"):
        property_names.add("MainPID")
    arguments = ["/usr/bin/systemctl", "show", unit]
    arguments.extend(f"--property={name}" for name in sorted(property_names))
    output = _parse_systemctl_properties(
        _run(arguments).stdout,
        property_names,
    )
    if output["ActiveState"] != "active":
        raise RuntimeError(f"{unit} is not active")
    return (
        output["ActiveState"],
        output.get("MainPID", ""),
        output["ActiveEnterTimestampMonotonic"],
    )


def _protected_hashes(install_user: str) -> dict[str, str]:
    node_root = Path("/etc/blueprint/nodes") / install_user
    paths = [node_root / "manifest.json"]
    expected_role_envs = {
        node_root / "roles" / role / "role.env"
        for role in ("database", "database-backup", "api", "worker", "edge")
    }
    actual_role_envs = set(node_root.glob("roles/*/role.env"))
    if actual_role_envs != expected_role_envs:
        raise ValueError("installed node has an unexpected role environment set")
    paths.extend(expected_role_envs)
    paths.extend(
        Path("/etc/systemd/system") / name
        for name in (
            "blueprint-database.service",
            "blueprint-database-backup.service",
            "blueprint-database-backup.timer",
            "blueprint-api.service",
            "blueprint-worker.service",
            "blueprint-edge.service",
        )
    )
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }


def _api_drop_in(bundle: Path) -> bytes:
    service = bundle / "local_api_service.py"
    return (
        "[Service]\n"
        "ExecStartPre=\n"
        f"ExecStartPre=/usr/bin/python3.12 {service} --check\n"
        "ExecStart=\n"
        f"ExecStart=/usr/bin/python3.12 {service}\n"
    ).encode()


def bootstrap(revision: str) -> Path:
    if os.geteuid() != 0:
        raise PermissionError("bootstrap must run as root")
    if not _REVISION_RE.fullmatch(revision):
        raise ValueError("revision must be a full lowercase git SHA")
    install_user = os.environ.get("SUDO_USER", "")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", install_user):
        raise ValueError("SUDO_USER must identify the service account")

    source = Path(__file__).resolve(strict=True).parent
    _trusted_directory(source)
    payloads = {name: _trusted_file(source / name) for name in _SOURCE_NAMES}
    edge_payload = _trusted_file(source / _EDGE_DROP_IN_NAME)
    bundle = _BUNDLE_ROOT / f"app-release-{_bundle_digest(payloads)}"
    markers_before = {unit: _marker(unit) for unit in _UNITS}
    protected_before = _protected_hashes(install_user)
    previous = {
        path: _snapshot_optional(path) for path in (_API_DROP_IN, _EDGE_DROP_IN)
    }

    try:
        bundle_root_info = _BUNDLE_ROOT.lstat()
        if (
            not stat.S_ISDIR(bundle_root_info.st_mode)
            or stat.S_ISLNK(bundle_root_info.st_mode)
            or bundle_root_info.st_uid != 0
            or stat.S_IMODE(bundle_root_info.st_mode) & 0o022
        ):
            raise ValueError("unsafe immutable bundle root")
        bundle.mkdir(parents=True, mode=0o755, exist_ok=True)
        bundle_info = bundle.lstat()
        if (
            not stat.S_ISDIR(bundle_info.st_mode)
            or stat.S_ISLNK(bundle_info.st_mode)
            or bundle_info.st_uid != 0
            or stat.S_IMODE(bundle_info.st_mode) & 0o022
        ):
            raise ValueError("unsafe immutable bundle directory")
        for name, payload in payloads.items():
            destination = bundle / name
            if destination.exists():
                if _trusted_file(destination) != payload:
                    raise ValueError("existing immutable bundle payload mismatch")
            else:
                _atomic_write(destination, payload, 0o755)
        _atomic_write(_API_DROP_IN, _api_drop_in(bundle), 0o644)
        _atomic_write(_EDGE_DROP_IN, edge_payload, 0o644)
        _run(["/usr/bin/systemctl", "daemon-reload"])
        api_exec = _run(
            [
                "/usr/bin/systemctl",
                "show",
                "blueprint-api.service",
                "--property=ExecStart",
                "--value",
            ]
        ).stdout
        edge_binds = _run(
            [
                "/usr/bin/systemctl",
                "show",
                "blueprint-edge.service",
                "--property=BindsTo",
                "--value",
            ]
        ).stdout
        if str(bundle / "local_api_service.py") not in api_exec:
            raise RuntimeError("API drop-in was not loaded")
        if "blueprint-api.service" in edge_binds.split():
            raise RuntimeError("edge lifecycle remains hard-bound to API")
        if _protected_hashes(install_user) != protected_before:
            raise RuntimeError("bootstrap changed protected role or base unit files")
        if {unit: _marker(unit) for unit in _UNITS} != markers_before:
            raise RuntimeError("bootstrap restarted or changed a live service")
    except Exception:
        for path, payload in previous.items():
            if payload is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                _atomic_write(path, payload, 0o644)
        _run(["/usr/bin/systemctl", "daemon-reload"])
        raise
    return bundle


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    try:
        bundle = bootstrap(args.revision)
    except (OSError, PermissionError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 1
    print(f"LOCAL_APP_RELEASE_BOOTSTRAP_COMPLETE bundle={bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

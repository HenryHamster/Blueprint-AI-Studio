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
_EDGE_TEMPLATE_NAME = "blueprint-edge-system.service"
_BUNDLE_ROOT = Path("/usr/local/lib/blueprint-node")
_API_DROP_IN = Path(
    "/etc/systemd/system/blueprint-api.service.d/20-local-app-release.conf"
)
_EDGE_UNIT = Path("/etc/systemd/system/blueprint-edge.service")
_LEGACY_EDGE_DROP_IN = Path(
    "/etc/systemd/system/blueprint-edge.service.d/10-noninteractive-funnel.conf"
)
_RETIRED_EDGE_DROP_IN = Path(
    "/var/lib/blueprint/bootstrap/local-app-release/"
    "retired-10-noninteractive-funnel.conf"
)
_NODE_ROOT = Path("/etc/blueprint/nodes")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_EDGE_ENTRYPOINT_RE = re.compile(
    r"^/usr/local/lib/blueprint-node/[a-z0-9-]+/tailscale_funnel_service\.py$"
)
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


def _snapshot_optional(path: Path) -> tuple[bytes, int] | None:
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
        raise ValueError(f"unsafe existing managed file: {path}")
    return path.read_bytes(), stat.S_IMODE(info.st_mode)


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
    for unit in ("blueprint-api.service", "blueprint-edge.service"):
        drop_in_directory = Path("/etc/systemd/system") / f"{unit}.d"
        if drop_in_directory.exists():
            _trusted_directory(drop_in_directory)
            paths.extend(drop_in_directory.iterdir())
    return {
        str(path): hashlib.sha256(_trusted_file(path)).hexdigest()
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


def _edge_runtime(install_user: str) -> tuple[str, str]:
    expected = {
        "User",
        "EnvironmentFiles",
        "ExecStartPre",
        "ExecStart",
        "ExecStop",
    }
    output = _parse_systemctl_properties(
        _run(
            [
                "/usr/bin/systemctl",
                "show",
                "blueprint-edge.service",
                *(f"--property={name}" for name in sorted(expected)),
            ]
        ).stdout,
        expected,
    )
    if output["User"] != install_user:
        raise RuntimeError("edge unit user does not match install user")
    env_file = _NODE_ROOT / install_user / "roles" / "edge" / "role.env"
    if output["EnvironmentFiles"] != f"{env_file} (ignore_errors=no)":
        raise RuntimeError("edge unit environment file is unexpected")

    prefix = "argv[]=/usr/bin/python3.12 "
    preflight = output["ExecStartPre"]
    start = preflight.find(prefix)
    end = preflight.find(" --check ;", start + len(prefix))
    if start < 0 or end < 0:
        raise RuntimeError("edge preflight command is unexpected")
    entrypoint = preflight[start + len(prefix) : end]
    if not _EDGE_ENTRYPOINT_RE.fullmatch(entrypoint):
        raise RuntimeError("edge entrypoint path is unexpected")
    entrypoint_path = Path(entrypoint)
    _trusted_file(entrypoint_path)
    expected_commands = {
        "ExecStartPre": f"argv[]=/usr/bin/python3.12 {entrypoint} --check ;",
        "ExecStart": f"argv[]=/usr/bin/python3.12 {entrypoint} start ;",
        "ExecStop": f"argv[]=/usr/bin/python3.12 {entrypoint} stop ;",
    }
    for property_name, command in expected_commands.items():
        value = output[property_name]
        if f"path=/usr/bin/python3.12 ; {command}" not in value:
            raise RuntimeError(f"edge {property_name} command is unexpected")
        if "ignore_errors=no" not in value or "\n" in value:
            raise RuntimeError(f"edge {property_name} metadata is unexpected")
    return str(env_file), entrypoint


def _edge_unit(
    template: bytes,
    install_user: str,
    env_file: str,
    entrypoint: str,
) -> bytes:
    text = template.decode("utf-8")
    replacements = {
        "@USER@": install_user,
        "@ENV_FILE@": env_file,
        "@PYTHON@": "/usr/bin/python3.12",
        "@EXECUTABLE@": entrypoint,
    }
    expected_counts = {
        "@USER@": 1,
        "@ENV_FILE@": 1,
        "@PYTHON@": 3,
        "@EXECUTABLE@": 3,
    }
    for placeholder, value in replacements.items():
        if text.count(placeholder) != expected_counts[placeholder]:
            raise ValueError(f"edge unit template has invalid {placeholder} count")
        text = text.replace(placeholder, value)
    if "@" in text or "BindsTo=" in text:
        raise ValueError("edge unit template retains an unsafe directive")
    return text.encode("utf-8")


def _verify_edge_dependencies() -> None:
    expected = {"BindsTo", "Wants", "Requires", "After"}
    values = _parse_systemctl_properties(
        _run(
            [
                "/usr/bin/systemctl",
                "show",
                "blueprint-edge.service",
                *(f"--property={name}" for name in sorted(expected)),
            ]
        ).stdout,
        expected,
    )
    if values["BindsTo"].split():
        raise RuntimeError("edge lifecycle remains hard-bound")
    if "blueprint-api.service" not in values["Wants"].split():
        raise RuntimeError("edge lost soft API startup ordering")
    if "tailscaled.service" not in values["Requires"].split():
        raise RuntimeError("edge lost its Tailscale requirement")
    after = set(values["After"].split())
    if not {
        "blueprint-api.service",
        "tailscaled.service",
        "network-online.target",
    }.issubset(after):
        raise RuntimeError("edge ordering dependencies are incomplete")


def _verify_edge_service_hardening() -> None:
    expected = {
        "Type": "oneshot",
        "RemainAfterExit": "yes",
        "UMask": "0077",
        "NoNewPrivileges": "yes",
        "PrivateTmp": "yes",
        "ProtectSystem": "strict",
        "ProtectHome": "read-only",
        "ProtectKernelTunables": "yes",
        "ProtectKernelModules": "yes",
        "ProtectKernelLogs": "yes",
        "ProtectControlGroups": "yes",
        "PrivateDevices": "yes",
        "RestrictSUIDSGID": "yes",
        "LockPersonality": "yes",
    }
    values = _parse_systemctl_properties(
        _run(
            [
                "/usr/bin/systemctl",
                "show",
                "blueprint-edge.service",
                *(f"--property={name}" for name in sorted(expected)),
            ]
        ).stdout,
        set(expected),
    )
    if values != expected:
        raise RuntimeError("edge service hardening contract is incomplete")


def _retire_legacy_edge_drop_in(
    previous: dict[Path, tuple[bytes, int] | None],
) -> None:
    legacy = previous[_LEGACY_EDGE_DROP_IN]
    retired = previous[_RETIRED_EDGE_DROP_IN]
    if legacy is None:
        if retired is None:
            raise ValueError("legacy edge drop-in and retirement evidence are both absent")
        return
    if retired is not None and retired[0] != legacy[0]:
        raise ValueError("retired edge drop-in evidence does not match live legacy file")
    payload, _ = legacy
    if retired is None:
        _atomic_write(_RETIRED_EDGE_DROP_IN, payload, 0o600)
    _LEGACY_EDGE_DROP_IN.unlink()


def _restore_snapshots(
    previous: dict[Path, tuple[bytes, int] | None],
) -> None:
    for path, snapshot in previous.items():
        if snapshot is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            payload, mode = snapshot
            _atomic_write(path, payload, mode)


def _without_intentional_changes(hashes: dict[str, str]) -> dict[str, str]:
    result = dict(hashes)
    for path in (_API_DROP_IN, _EDGE_UNIT, _LEGACY_EDGE_DROP_IN):
        result.pop(str(path), None)
    return result


def _install_service_configuration(
    *,
    bundle: Path,
    edge_payload: bytes,
    install_user: str,
    edge_runtime_before: tuple[str, str],
    markers_before: dict[str, tuple[str, str, str]],
    protected_before: dict[str, str],
    previous: dict[Path, tuple[bytes, int] | None],
) -> None:
    edge_snapshot = previous[_EDGE_UNIT]
    if edge_snapshot is None:
        raise ValueError("edge base unit is missing")
    try:
        _atomic_write(_API_DROP_IN, _api_drop_in(bundle), 0o644)
        _atomic_write(_EDGE_UNIT, edge_payload, edge_snapshot[1])
        _retire_legacy_edge_drop_in(previous)
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
        if str(bundle / "local_api_service.py") not in api_exec:
            raise RuntimeError("API drop-in was not loaded")
        _verify_edge_dependencies()
        _verify_edge_service_hardening()
        if _edge_runtime(install_user) != edge_runtime_before:
            raise RuntimeError("edge execution contract changed")
        protected_after = _protected_hashes(install_user)
        if _without_intentional_changes(
            protected_after
        ) != _without_intentional_changes(protected_before):
            raise RuntimeError("bootstrap changed protected role or base unit files")
        if {unit: _marker(unit) for unit in _UNITS} != markers_before:
            raise RuntimeError("bootstrap restarted or changed a live service")
    except Exception:
        _restore_snapshots(previous)
        _run(["/usr/bin/systemctl", "daemon-reload"])
        raise


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
    edge_template = _trusted_file(source / _EDGE_TEMPLATE_NAME)
    edge_runtime_before = _edge_runtime(install_user)
    edge_payload = _edge_unit(
        edge_template,
        install_user,
        *edge_runtime_before,
    )
    bundle = _BUNDLE_ROOT / f"app-release-{_bundle_digest(payloads)}"
    markers_before = {unit: _marker(unit) for unit in _UNITS}
    protected_before = _protected_hashes(install_user)
    previous = {
        path: _snapshot_optional(path)
        for path in (
            _API_DROP_IN,
            _EDGE_UNIT,
            _LEGACY_EDGE_DROP_IN,
            _RETIRED_EDGE_DROP_IN,
        )
    }
    retired = previous[_RETIRED_EDGE_DROP_IN]
    if retired is not None and retired[1] != 0o600:
        raise ValueError("retired edge drop-in evidence must be mode 0600")

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
    _install_service_configuration(
        bundle=bundle,
        edge_payload=edge_payload,
        install_user=install_user,
        edge_runtime_before=edge_runtime_before,
        markers_before=markers_before,
        protected_before=protected_before,
        previous=previous,
    )
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

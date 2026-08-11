#!/usr/bin/env python3
"""Root-owned bootstrap for hamsterwheel application releases.

The long-lived deploy agent remains a hardened non-root service.  It hands an
already approval- and manifest-validated request to the socket-activated
root-owned release service; the privileged release step never runs in the
agent's sandbox.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

_SOURCE_NAMES = (
    "frontend_release_manifest.py",
    "local_app_release.py",
    "release_preconditions.py",
    "local_api_service.py",
    "local_worker_service.py",
    "blueprint.py",
    "blueprint_api.py",
    "release_activation.py",
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
_DEPLOY_AGENT_UNIT = Path("/etc/systemd/system/blueprint-deploy-agent.service")
_GH_CONFIG_DIR = Path("/var/lib/blueprint-deploy-agent/gh")
_RELEASE_SERVICE = Path("/etc/systemd/system/blueprint-release.service")
_RELEASE_SOCKET_UNIT = Path("/etc/systemd/system/blueprint-release.socket")
_RELEASE_SHIM = Path("/usr/local/sbin/blueprint-release")
_LEGACY_RELEASE_SUDOERS = Path("/etc/sudoers.d/blueprint-release")
# Keep the socket client and root executor alive for the same complete,
# synchronous activation envelope.  See blueprint.py for the budget's
# component waits and margin.
_RELEASE_ACTIVATION_TIMEOUT_SECONDS = 1200
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
RUNBOOK_ENTRYPOINT = "bootstrap"


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({argv[0]}): {(result.stderr or result.stdout).strip()}"
        )
    return result


def _parse_systemctl_properties(payload: str, expected: set[str]) -> dict[str, str]:
    from release_preconditions import parse_systemctl_properties

    return parse_systemctl_properties(payload, expected)


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


def _source_path(source: Path, name: str) -> Path:
    """Resolve allowlisted bundle inputs, including the shared retry module."""
    staged = source / name
    if staged.is_file():
        return staged
    if name == "release_activation.py":
        return source.parent / "backend" / "app" / name
    return staged


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


def _deploy_agent_unit(bundle: Path, install_user: str) -> bytes:
    """Render the non-root service that polls approved activation requests.

    RuntimeDirectory is created and owned by systemd for ``User`` before the
    process starts. This is necessary because /run itself is not writable by
    the service account, while the agent's advisory flock must be local and
    unavailable to unprivileged peers.
    """
    executable = bundle / "blueprint.py"
    return (
        "[Unit]\n"
        "Description=Blueprint approved-activation deploy agent\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "StartLimitIntervalSec=60\n"
        "StartLimitBurst=3\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={install_user}\n"
        "UMask=0077\n"
        "PrivateTmp=true\n"
        "ProtectSystem=strict\n"
        "ProtectHome=read-only\n"
        "ProtectKernelTunables=true\n"
        "ProtectKernelModules=true\n"
        "ProtectKernelLogs=true\n"
        "ProtectControlGroups=true\n"
        "PrivateDevices=true\n"
        "RestrictSUIDSGID=true\n"
        "LockPersonality=true\n"
        # The sandbox options above imply no-new-privileges at runtime, but the
        # NoNewPrivileges D-Bus property getter reports only the explicitly
        # configured field (it does not expose the implied value). Set it
        # explicitly so `systemctl show --property=NoNewPrivileges` reads "yes"
        # and _verify_release_execution_boundary can confirm the hardening.
        "NoNewPrivileges=yes\n"
        "RuntimeDirectory=blueprint-deploy-agent\n"
        "RuntimeDirectoryMode=0750\n"
        "StateDirectory=blueprint-deploy-agent\n"
        "StateDirectoryMode=0700\n"
        f"Environment=GH_CONFIG_DIR={_GH_CONFIG_DIR}\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        f"ExecStartPre=/usr/bin/python3.12 {executable} deploy-agent --preflight\n"
        f"ExecStart=/usr/bin/python3.12 {executable} deploy-agent "
        "--poll-interval 30 "
        "--lock-path /run/blueprint-deploy-agent/deploy-agent.lock "
        "--state-path /var/lib/blueprint-deploy-agent/deploy-agent.state\n"
        "Restart=always\n"
        "RestartSec=30\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    ).encode()


def _source_gh_config_dir(install_user: str) -> Path:
    try:
        return Path(pwd.getpwnam(install_user).pw_dir) / ".config" / "gh"
    except KeyError as exc:
        raise ValueError(f"service account does not exist: {install_user}") from exc


def _provision_github_cli_config(install_user: str) -> None:
    """Copy the service account's gh host token into protected state storage."""
    try:
        account = pwd.getpwnam(install_user)
    except KeyError as exc:
        raise ValueError(f"service account does not exist: {install_user}") from exc
    source_dir = _source_gh_config_dir(install_user)
    source = source_dir / "hosts.yml"
    try:
        directory_info = source_dir.lstat()
        info = source.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"missing GitHub CLI credential {source}; authenticate gh as {install_user}"
        ) from exc
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or stat.S_ISLNK(directory_info.st_mode)
        or directory_info.st_uid != account.pw_uid
        or stat.S_IMODE(directory_info.st_mode) & 0o077
        or not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != account.pw_uid
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise RuntimeError(
            f"GitHub CLI credential {source} must be private and owned by {install_user}"
        )

    _GH_CONFIG_DIR.mkdir(parents=True, mode=0o755, exist_ok=True)
    target_info = _GH_CONFIG_DIR.lstat()
    if not stat.S_ISDIR(target_info.st_mode) or stat.S_ISLNK(target_info.st_mode):
        raise RuntimeError(f"unsafe GitHub CLI state directory: {_GH_CONFIG_DIR}")
    os.chown(_GH_CONFIG_DIR, account.pw_uid, account.pw_gid)
    os.chmod(_GH_CONFIG_DIR, 0o700)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".hosts.", dir=_GH_CONFIG_DIR)
        with os.fdopen(fd, "wb") as handle:
            handle.write(source.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary, account.pw_uid, account.pw_gid)
        os.chmod(temporary, 0o600)
        os.replace(temporary, _GH_CONFIG_DIR / "hosts.yml")
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _verify_github_cli_auth(install_user: str) -> None:
    """Require the service account's owner-only gh credential before enable.

    The deploy agent downloads private Actions artifacts, so an API key alone
    is insufficient. Keep GitHub auth in the service account's own narrow
    config directory and run both the auth probe and the exact workflow-list
    operation as that account during bootstrap; a fresh host fails closed
    before the unit is enabled.
    """
    try:
        service_uid = pwd.getpwnam(install_user).pw_uid
    except KeyError as exc:
        raise ValueError(f"service account does not exist: {install_user}") from exc
    config_dir = _GH_CONFIG_DIR
    hosts_file = config_dir / "hosts.yml"
    try:
        directory_info = config_dir.lstat()
        info = hosts_file.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"missing GitHub CLI credential {hosts_file}; authenticate gh as {install_user}"
        ) from exc
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or stat.S_ISLNK(directory_info.st_mode)
        or directory_info.st_uid != service_uid
        or stat.S_IMODE(directory_info.st_mode) & 0o077
        or
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != service_uid
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise RuntimeError(
            f"GitHub CLI credential {hosts_file} must be a private regular file "
            f"owned by {install_user}"
        )

    def run_gh(*arguments: str) -> None:
        result = subprocess.run(
            [
                "/usr/sbin/runuser",
                "--user", install_user,
                "--",
                "/usr/bin/env",
                f"GH_CONFIG_DIR={config_dir}",
                "gh",
                *arguments,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "GitHub CLI authentication/preflight failed; run gh auth login "
                f"as {install_user} and grant read access to private Actions artifacts"
            )

    run_gh("auth", "status", "--hostname", "github.com")
    run_gh(
        "run", "list", "--repo", "HenryHamster/BlueprintAIStudio",
        "--workflow", "Deploy Blueprint", "--branch", "main", "--limit", "1",
        "--json", "databaseId,headSha,status,conclusion",
    )


def _release_service_unit() -> bytes:
    """Render the unsandboxed root executor started by the release socket."""
    return (
        b"[Unit]\n"
        b"Description=Blueprint approved-activation root release executor\n"
        b"After=blueprint-release.socket\n"
        b"\n"
        b"[Service]\n"
        b"Type=oneshot\n"
        b"User=root\n"
        b"NoNewPrivileges=no\n"
        + f"TimeoutStartSec={_RELEASE_ACTIVATION_TIMEOUT_SECONDS}s\n".encode()
        + b"ExecStart=/usr/local/sbin/blueprint-release --serve\n"
    )


def _release_socket_unit(install_user: str) -> bytes:
    """Render the ACL-limited socket that crosses the privilege boundary."""
    return (
        "[Unit]\n"
        "Description=Blueprint approved-activation release socket\n"
        "\n"
        "[Socket]\n"
        "ListenStream=/run/blueprint-release.sock\n"
        "SocketUser=root\n"
        f"SocketGroup={install_user}\n"
        "SocketMode=0660\n"
        "Service=blueprint-release.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=sockets.target\n"
    ).encode()


def _verify_release_execution_boundary(install_user: str) -> None:
    """Confirm systemd's reported User/NoNewPrivileges on both privilege sides.

    ``systemctl show --property=NoNewPrivileges`` reports the explicitly
    configured field, not the value implied by the sandbox options, so the
    deploy-agent unit sets ``NoNewPrivileges=yes`` explicitly for this check to
    observe. The root release executor keeps ``NoNewPrivileges=no`` so it can
    cross the privilege boundary.
    """
    from release_preconditions import verify_privilege_boundary

    verify_privilege_boundary(
        install_user,
        agent_unit=_DEPLOY_AGENT_UNIT.name,
        release_unit=_RELEASE_SERVICE.name,
        run=_run,
    )


def _install_deploy_agent(
    *,
    bundle: Path,
    install_user: str,
    release_shim: bytes,
) -> None:
    """Install the agent and root socket executor as one recoverable set."""
    managed = (
        _DEPLOY_AGENT_UNIT,
        _RELEASE_SERVICE,
        _RELEASE_SOCKET_UNIT,
        _RELEASE_SHIM,
        _LEGACY_RELEASE_SUDOERS,
    )
    previous = {path: _snapshot_optional(path) for path in managed}
    try:
        # This renderer is the sole owner of blueprint-deploy-agent.service.
        deploy_agent_unit = _deploy_agent_unit(bundle, install_user)
        _atomic_write(_DEPLOY_AGENT_UNIT, deploy_agent_unit, 0o644)
        _atomic_write(_RELEASE_SERVICE, _release_service_unit(), 0o644)
        _atomic_write(_RELEASE_SOCKET_UNIT, _release_socket_unit(install_user), 0o644)
        _atomic_write(_RELEASE_SHIM, release_shim, 0o755)
        if previous[_LEGACY_RELEASE_SUDOERS] is not None:
            _LEGACY_RELEASE_SUDOERS.unlink()
        _run(["/usr/bin/systemctl", "daemon-reload"])
        _run([
            "/usr/bin/systemd-analyze",
            "verify",
            str(_DEPLOY_AGENT_UNIT),
            str(_RELEASE_SERVICE),
            str(_RELEASE_SOCKET_UNIT),
        ])
        _verify_release_execution_boundary(install_user)
        _verify_github_cli_auth(install_user)
        _run(["/usr/bin/systemctl", "enable", "--now", _DEPLOY_AGENT_UNIT.name])
        _run(["/usr/bin/systemctl", "enable", "--now", _RELEASE_SOCKET_UNIT.name])
        # enable --now does not reload an already-running process after its
        # immutable bundle or unit has changed. Explicitly roll it over so the
        # installed agent is the one that is actually polling.
        _run(["/usr/bin/systemctl", "restart", _DEPLOY_AGENT_UNIT.name])
    except Exception:
        _restore_snapshots(previous)
        _run(["/usr/bin/systemctl", "daemon-reload"])
        try:
            _run(["/usr/bin/systemctl", "restart", _DEPLOY_AGENT_UNIT.name])
        except Exception as rollback_error:  # noqa: BLE001 - preserve original failure
            print(
                f"WARNING: could not restart restored deploy agent: {rollback_error}",
                file=sys.stderr,
            )
        raise


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
    payloads = {
        name: _trusted_file(_source_path(source, name))
        for name in _SOURCE_NAMES
    }
    _provision_github_cli_config(install_user)
    release_shim = _trusted_file(source / "blueprint_release.py")
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
    deploy_agent_previous = {
        path: _snapshot_optional(path)
        for path in (
            _DEPLOY_AGENT_UNIT,
            _RELEASE_SERVICE,
            _RELEASE_SOCKET_UNIT,
            _RELEASE_SHIM,
            _LEGACY_RELEASE_SUDOERS,
        )
    }
    all_previous = {**previous, **deploy_agent_previous}
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
    try:
        _install_service_configuration(
            bundle=bundle,
            edge_payload=edge_payload,
            install_user=install_user,
            edge_runtime_before=edge_runtime_before,
            markers_before=markers_before,
            protected_before=protected_before,
            previous=previous,
        )
        _install_deploy_agent(
            bundle=bundle,
            install_user=install_user,
            release_shim=release_shim,
        )
    except Exception:
        _restore_snapshots(all_previous)
        _run(["/usr/bin/systemctl", "daemon-reload"])
        if deploy_agent_previous[_DEPLOY_AGENT_UNIT] is not None:
            try:
                _run(["/usr/bin/systemctl", "restart", _DEPLOY_AGENT_UNIT.name])
            except Exception as rollback_error:  # noqa: BLE001
                print(
                    f"WARNING: could not restart restored deploy agent: {rollback_error}",
                    file=sys.stderr,
                )
        else:
            try:
                _run(["/usr/bin/systemctl", "stop", _DEPLOY_AGENT_UNIT.name])
            except Exception as rollback_error:  # noqa: BLE001
                print(
                    f"WARNING: could not stop newly installed deploy agent: {rollback_error}",
                    file=sys.stderr,
                )
        raise
    return bundle


def rehearse(image_ref: str, revision: str):
    """Refuse prequalification candidate execution on the production host."""
    del image_ref, revision
    raise RuntimeError(
        "host rehearsal disabled; use trusted CI build/smoke evidence"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--image")
    parser.add_argument(
        "--rehearse",
        action="store_true",
        help="check candidate preconditions without changing units or files",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.rehearse:
            if not args.image:
                parser.error("--rehearse requires --image")
            report = rehearse(args.image, args.revision)
            print(
                "LOCAL_APP_RELEASE_REHEARSAL "
                + json.dumps(report.as_dict(), sort_keys=True)
            )
            return 0
        bundle = bootstrap(args.revision)
    except (OSError, PermissionError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"LOCAL_APP_RELEASE_BOOTSTRAP_COMPLETE bundle={bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Additive lifecycle facade for a multi-role Blueprint node.

The facade installs only the roles executable for the selected profile.  In
particular, ``local-control-plane`` owns a private PostgreSQL container, API,
worker, and exact Tailscale Funnel route, while existing Cloud SQL profiles
retain their original proxy/worker lifecycle.

``blueprint-runner`` remains unchanged and supported.  ``blueprint-node`` is an
opt-in facade for new hosts and never modifies the legacy unit or env file.

The executor has no coordinated drain protocol yet.  Its lifecycle command is
therefore deliberately named ``stop`` and warns that active work can be
interrupted; it must not be used as a maintenance drain.
"""

from __future__ import annotations

import argparse
import errno
import getpass
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

try:  # ``pwd`` is intentionally optional so plan/unit tests remain portable.
    import pwd
except ImportError:  # pragma: no cover - Windows has no systemd either.
    pwd = None  # type: ignore[assignment]

import blueprint_runner as runner

_SCRIPT_DIR = Path(__file__).resolve().parent
_LOCAL_RUNNER = _SCRIPT_DIR / "local_runner.py"
_CLOUD_SQL_PROXY_SERVICE = _SCRIPT_DIR / "cloud_sql_proxy_service.py"
_LOCAL_POSTGRES_SERVICE = _SCRIPT_DIR / "local_postgres_service.py"
_LOCAL_POSTGRES_BACKUP_SERVICE = _SCRIPT_DIR / "local_postgres_backup_service.py"
_LOCAL_API_SERVICE = _SCRIPT_DIR / "local_api_service.py"
_LOCAL_WORKER_SERVICE = _SCRIPT_DIR / "local_worker_service.py"
_TAILSCALE_FUNNEL_SERVICE = _SCRIPT_DIR / "tailscale_funnel_service.py"
_USER_TEMPLATE = _SCRIPT_DIR / "blueprint-node.service"
_SYSTEM_TEMPLATE = _SCRIPT_DIR / "blueprint-node-system.service"
_EDGE_USER_TEMPLATE = _SCRIPT_DIR / "blueprint-edge.service"
_EDGE_SYSTEM_TEMPLATE = _SCRIPT_DIR / "blueprint-edge-system.service"
_DATABASE_SYSTEM_TEMPLATE = _SCRIPT_DIR / "blueprint-database-system.service"
_DATABASE_BACKUP_SYSTEM_TEMPLATE = (
    _SCRIPT_DIR / "blueprint-database-backup-system.service"
)
_DATABASE_BACKUP_TIMER_TEMPLATE = (
    _SCRIPT_DIR / "blueprint-database-backup-system.timer"
)
_MANIFEST_VERSION = 1
_SYSTEM_NODE_ROOT = Path("/etc/blueprint/nodes")
_SYSTEM_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_POSTGRES_CONTAINER_UID = 999
_POSTGRES_CONTAINER_GID = 999
_LOCAL_DATABASE_ROOTS = (Path("/var/lib/blueprint"), Path("/srv/blueprint"))
_TRUSTED_PATH_UID = 0


@dataclass(frozen=True)
class RoleSpec:
    name: str
    unit_name: str
    executable: bool
    description: str
    entrypoint: Path | None = None
    env_required: bool = False


ROLE_SPECS: dict[str, RoleSpec] = {
    "node-agent": RoleSpec(
        "node-agent", "blueprint-node-agent.service", False,
        "node inventory, capacity ledger, coordinated updates, and draining",
    ),
    "executor": RoleSpec(
        "executor", "blueprint-executor.service", True,
        "Blueprint agent and compute lanes (current local_runner implementation)",
        entrypoint=_LOCAL_RUNNER,
    ),
    "github-actions": RoleSpec(
        "github-actions", "blueprint-actions.service", False,
        "official self-hosted GitHub Actions runner lifecycle",
    ),
    "api": RoleSpec(
        "api",
        "blueprint-api.service",
        True,
        "local Blueprint HTTP API bound only to loopback",
        entrypoint=_LOCAL_API_SERVICE,
        env_required=True,
    ),
    "database": RoleSpec(
        "database",
        "blueprint-database.service",
        True,
        "private local PostgreSQL data plane",
        entrypoint=_LOCAL_POSTGRES_SERVICE,
        env_required=True,
    ),
    "database-proxy": RoleSpec(
        "database-proxy",
        "blueprint-database-proxy.service",
        True,
        "Cloud SQL Auth Proxy bound only to the local control-plane Docker bridge",
        entrypoint=_CLOUD_SQL_PROXY_SERVICE,
        env_required=True,
    ),
    "worker": RoleSpec(
        "worker", "blueprint-worker.service", True,
        "scheduler, dispatch, reapers, and background loops",
        entrypoint=_LOCAL_WORKER_SERVICE,
        env_required=True,
    ),
    "database-backup": RoleSpec(
        "database-backup",
        "blueprint-database-backup.timer",
        True,
        "twice-daily off-host local PostgreSQL backup",
        entrypoint=_LOCAL_POSTGRES_BACKUP_SERVICE,
        env_required=True,
    ),
    "edge": RoleSpec(
        "edge",
        "blueprint-edge.service",
        True,
        "exact public Tailscale Funnel route to the loopback API",
        entrypoint=_TAILSCALE_FUNNEL_SERVICE,
        env_required=True,
    ),
}

PROFILE_ROLES: dict[str, tuple[str, ...]] = {
    "primary": (
        "node-agent",
        "executor",
        "api",
        "database-proxy",
        "worker",
        "edge",
    ),
    "worker": ("node-agent", "executor", "github-actions"),
    "gpu-worker": ("node-agent", "executor"),
    "control-plane": ("database-proxy", "worker"),
    "local-control-plane": ("database", "database-backup", "api", "worker", "edge"),
}

PROFILE_DEFAULTS: dict[str, dict[str, str]] = {
    "primary": {},
    "worker": {},
    "control-plane": {},
    "local-control-plane": {},
    "gpu-worker": {
        "BLUEPRINT_RUNNER_MAX_CONCURRENT_COMPUTE_JOBS": "1",
        "BLUEPRINT_RUNNER_GPUS_PER_JOB": "1",
    },
}

# Implemented roles remain profile-scoped. ``primary`` retains its historical
# manifest-only API/edge placeholders, and the Cloud SQL profiles continue to
# install only the proxy-backed worker.
PROFILE_EXECUTABLE_ROLES: dict[str, tuple[str, ...]] = {
    "primary": ("executor", "database-proxy", "worker"),
    "worker": ("executor",),
    "gpu-worker": ("executor",),
    "control-plane": ("database-proxy", "worker"),
    "local-control-plane": ("database", "database-backup", "api", "worker", "edge"),
}

PROFILE_ROLE_REQUIRES: dict[tuple[str, str], tuple[str, ...]] = {
    ("primary", "worker"): ("blueprint-database-proxy.service",),
    ("control-plane", "worker"): ("blueprint-database-proxy.service",),
    ("local-control-plane", "api"): ("blueprint-database.service",),
    ("local-control-plane", "worker"): ("blueprint-database.service",),
    ("local-control-plane", "database-backup"): ("blueprint-database.service",),
}


# The node facade reuses the existing runner credential store.  Running this
# unit alongside the legacy service would therefore create two consumers with
# the same identity and bypass the shared resource ledger.  Adoption is an
# explicit operator cutover, never an additive second daemon.
_LEGACY_RUNNER_UNIT = "blueprint-runner.service"


def _require_safe_path(value: str, *, field: str) -> str:
    """Reject values that could change the meaning of a rendered unit line."""
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} must not contain control characters")
    if any(char.isspace() for char in value):
        raise ValueError(f"{field} must not contain whitespace")
    return value


def _system_user_record(user: str):
    """Return the real POSIX account used by a system service, fail closed."""
    if not _SYSTEM_USER_RE.fullmatch(user):
        raise ValueError("system runner user must be a local POSIX account name")
    if pwd is None:
        raise ValueError("system installs require a POSIX host with a passwd database")
    try:
        return pwd.getpwnam(user)
    except KeyError as exc:
        raise ValueError(f"system runner user {user!r} does not exist") from exc


def _system_install_user() -> str:
    user = _install_user()
    record = _system_user_record(user)
    if record.pw_uid == 0:
        raise ValueError(
            "system runner user must be non-root; invoke sudo from the intended "
            "service account or use a dedicated local account"
        )
    return user


def node_root(*, system: bool = False, user: str | None = None) -> Path:
    """Return root-owned node configuration, separate from runner credentials."""
    if system:
        return _SYSTEM_NODE_ROOT / (user or _system_install_user())
    base = os.environ.get("BLUEPRINT_NODE_DIR", "").strip()
    if base:
        return Path(_require_safe_path(base, field="BLUEPRINT_NODE_DIR")).expanduser()
    return Path("~/.blueprint/node").expanduser()


def role_env_path(role: str, *, system: bool = False, user: str | None = None) -> Path:
    _require_role(role)
    return node_root(system=system, user=user) / "roles" / role / "role.env"


def manifest_path(*, system: bool = False, user: str | None = None) -> Path:
    return node_root(system=system, user=user) / "manifest.json"


def stopped_path(role: str, *, system: bool = False, user: str | None = None) -> Path:
    _require_role(role)
    return node_root(system=system, user=user) / "roles" / role / "stopped.json"


def _require_role(role: str) -> RoleSpec:
    try:
        return ROLE_SPECS[role]
    except KeyError as exc:
        raise ValueError(f"unknown node role {role!r}") from exc


def _require_profile(profile: str) -> tuple[str, ...]:
    try:
        return PROFILE_ROLES[profile]
    except KeyError as exc:
        raise ValueError(f"unknown node profile {profile!r}") from exc


def _profile_role_executable(profile: str, role: str) -> bool:
    _require_profile(profile)
    _require_role(role)
    return role in PROFILE_EXECUTABLE_ROLES[profile]


def _profile_role_requires(profile: str, role: str) -> tuple[str, ...]:
    _require_profile(profile)
    _require_role(role)
    return PROFILE_ROLE_REQUIRES.get((profile, role), ())


def unit_path(role: str, *, system: bool) -> Path:
    spec = _require_role(role)
    if system:
        return Path("/etc/systemd/system") / spec.unit_name
    return Path("~/.config/systemd/user").expanduser() / spec.unit_name


def _database_backup_service_unit_path(*, system: bool) -> Path:
    if not system:
        raise ValueError("database backup is supported only as a system service")
    return Path("/etc/systemd/system/blueprint-database-backup.service")


def _install_user() -> str:
    return (
        os.environ.get("SUDO_USER", "").strip()
        or os.environ.get("USER", "").strip()
        or getpass.getuser()
    )


def _system_credential_dir(user: str) -> tuple[Path, int, int]:
    """Return the service account's writable credential directory and ownership."""
    record = _system_user_record(user)
    home = _require_safe_path(record.pw_dir, field="system runner home")
    if not Path(home).is_absolute():
        raise ValueError("system runner home must be an absolute path")
    home_path = Path(home)
    try:
        home_stat = home_path.stat()
    except OSError as exc:
        raise ValueError(f"system runner home {home!r} is not accessible") from exc
    if not stat.S_ISDIR(home_stat.st_mode) or home_stat.st_uid != record.pw_uid:
        raise ValueError("system runner home must be an existing directory owned by the runner user")
    return home_path / ".blueprint", record.pw_uid, record.pw_gid


def _legacy_user_unit_exists(user: str) -> bool:
    """Whether the service account still has a legacy user-unit definition.

    A system install runs as ``user`` and deliberately shares its credential
    directory. From a sudo invocation we cannot reliably query that user's
    session bus, so a leftover user unit is an adoption blocker even if its
    active state is unavailable to the caller. The operator must explicitly
    uninstall the old unit before changing service-manager scopes.
    """

    record = _system_user_record(user)
    return (
        Path(record.pw_dir) / ".config" / "systemd" / "user" / _LEGACY_RUNNER_UNIT
    ).is_file()


def _prepare_system_credential_dir(path: Path, *, uid: int, gid: int) -> None:
    """Create a runner-writable credential directory without broadening access."""
    if path.is_symlink():
        raise ValueError(f"refusing symlinked credential directory {path}")
    path.mkdir(parents=True, exist_ok=True)
    path_stat = path.lstat()
    if not stat.S_ISDIR(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode):
        raise ValueError(f"credential directory {path} is not a real directory")
    os.chown(path, uid, gid)
    os.chmod(path, 0o700)


def _prepare_system_node_root(path: Path) -> None:
    """Keep generated root-only service configuration out of user-controlled homes."""
    if path.is_symlink():
        raise ValueError(f"refusing symlinked system node directory {path}")
    path.mkdir(parents=True, exist_ok=True)
    path_stat = path.lstat()
    if not stat.S_ISDIR(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode):
        raise ValueError(f"system node directory {path} is not a real directory")
    os.chmod(path, 0o700)


def _portable_prepare_local_postgres_data_dir(path: Path) -> Path:
    """Windows-only test fallback; production uses descriptor-relative traversal."""
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
            raise ValueError("PostgreSQL data path must not contain symlinks")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    entries = list(path.iterdir())
    if entries:
        version = path / "PG_VERSION"
        try:
            version_info = version.lstat()
        except FileNotFoundError as exc:
            raise ValueError(
                "refusing ambiguous non-empty PostgreSQL data directory"
            ) from exc
        if stat.S_ISLNK(version_info.st_mode) or not stat.S_ISREG(
            version_info.st_mode
        ):
            raise ValueError("refusing unsafe PG_VERSION in PostgreSQL data directory")
        if version.read_text(encoding="ascii").strip() != "15":
            raise ValueError("PostgreSQL data directory must be major version 15")
        for root, directories, files in os.walk(path, followlinks=False):
            for name in [*directories, *files]:
                child_info = (Path(root) / name).lstat()
                if stat.S_ISLNK(child_info.st_mode):
                    raise ValueError(
                        "PostgreSQL data directory must not contain symlinks"
                    )
                if (
                    child_info.st_uid != _POSTGRES_CONTAINER_UID
                    or child_info.st_gid != _POSTGRES_CONTAINER_GID
                ):
                    raise ValueError(
                        "existing PostgreSQL data must already be owned by uid/gid 999"
                    )
        if (
            info.st_uid != _POSTGRES_CONTAINER_UID
            or info.st_gid != _POSTGRES_CONTAINER_GID
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError(
                "existing PostgreSQL data directory must be mode 0700 and owned by uid/gid 999"
            )
        return path
    os.chmod(path, 0o700)
    os.chown(path, _POSTGRES_CONTAINER_UID, _POSTGRES_CONTAINER_GID)
    return path


def _require_trusted_directory_fd(fd: int) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != _TRUSTED_PATH_UID
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise ValueError(
            "PostgreSQL data path ancestors must be trusted directories "
            "owned by root and not group/world-writable"
        )


def _validate_postgres_tree_fd(fd: int) -> bytes | None:
    """Validate an existing PGDATA tree without following pathname replacements."""
    version: bytes | None = None
    for name in os.listdir(fd):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        try:
            child_fd = os.open(name, flags, dir_fd=fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ValueError(
                    "PostgreSQL data directory must not contain symlinks"
                ) from exc
            raise
        try:
            child_info = os.fstat(child_fd)
            if (
                child_info.st_uid != _POSTGRES_CONTAINER_UID
                or child_info.st_gid != _POSTGRES_CONTAINER_GID
            ):
                raise ValueError(
                    "existing PostgreSQL data must already be owned by uid/gid 999"
                )
            if stat.S_ISDIR(child_info.st_mode):
                _validate_postgres_tree_fd(child_fd)
            elif stat.S_ISREG(child_info.st_mode):
                if name == "PG_VERSION":
                    version = os.read(child_fd, 32)
            else:
                raise ValueError(
                    "PostgreSQL data directory may contain only files and directories"
                )
        finally:
            os.close(child_fd)
    return version


def _prepare_local_postgres_data_dir(value: str) -> Path:
    """Prepare PGDATA through pinned directory descriptors; never follow symlinks."""
    path = Path(_require_safe_path(value, field="BLUEPRINT_POSTGRES_DATA_DIR"))
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("BLUEPRINT_POSTGRES_DATA_DIR must be an absolute normalized path")
    selected_root = next(
        (
            root
            for root in _LOCAL_DATABASE_ROOTS
            if path != root and path.is_relative_to(root)
        ),
        None,
    )
    if selected_root is None:
        raise ValueError(
            "BLUEPRINT_POSTGRES_DATA_DIR must be below /var/lib/blueprint "
            "or /srv/blueprint"
        )
    if os.name == "nt":  # pragma: no cover - production system installs are POSIX.
        return _portable_prepare_local_postgres_data_dir(path)

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    target_parts = path.relative_to("/").parts
    create_from = len(selected_root.relative_to("/").parts) - 1
    try:
        _require_trusted_directory_fd(fd)
        for index, component in enumerate(target_parts):
            try:
                child_fd = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                if index < create_from:
                    raise ValueError(
                        "trusted PostgreSQL data path parent does not exist"
                    ) from None
                os.mkdir(component, mode=0o700, dir_fd=fd)
                child_fd = os.open(component, flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError(
                        "PostgreSQL data path must not contain symlinks"
                    ) from exc
                raise
            os.close(fd)
            fd = child_fd
            if index < len(target_parts) - 1:
                _require_trusted_directory_fd(fd)

        info = os.fstat(fd)
        entries = os.listdir(fd)
        if entries:
            if "PG_VERSION" not in entries:
                raise ValueError(
                    "refusing ambiguous non-empty PostgreSQL data directory"
                )
            version = _validate_postgres_tree_fd(fd)
            if version is None:
                raise ValueError(
                    "refusing ambiguous non-empty PostgreSQL data directory"
                )
            try:
                major = version.decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise ValueError(
                    "PostgreSQL data directory must be major version 15"
                ) from exc
            if major != "15":
                raise ValueError(
                    "PostgreSQL data directory must be major version 15"
                )
            if (
                info.st_uid != _POSTGRES_CONTAINER_UID
                or info.st_gid != _POSTGRES_CONTAINER_GID
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise ValueError(
                    "existing PostgreSQL data directory must be mode 0700 and owned by uid/gid 999"
                )
            return path
        os.fchmod(fd, 0o700)
        os.fchown(fd, _POSTGRES_CONTAINER_UID, _POSTGRES_CONTAINER_GID)
        return path
    finally:
        os.close(fd)


def resolve_install_token(*, system: bool) -> str | None:
    """Resolve enrollment without ever accepting a token in process arguments."""
    if not system:
        return runner.resolve_token(None)
    credential_dir, _uid, _gid = _system_credential_dir(_system_install_user())
    try:
        token = (credential_dir / "runner_token").read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if token:
        return token
    # An operator can supply a short-lived token via the inherited environment,
    # but the CLI itself never puts a secret into argv or generated commands.
    return os.environ.get("BLUEPRINT_RUNNER_TOKEN", "").strip() or None


def _atomic_write(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Write a sensitive config atomically, never exposing a partial role file."""
    _atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def _atomic_write_bytes(path: Path, payload: bytes, *, mode: int) -> None:
    """Atomically replace a file while retaining an explicit reviewed mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        if os.name != "nt":
            try:
                os.chmod(temporary, mode)
            except OSError:
                pass
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class _FileSnapshot:
    existed: bool
    payload: bytes = b""
    mode: int = 0


def _snapshot_file(path: Path) -> _FileSnapshot:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _FileSnapshot(False)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"refusing to replace non-regular install target {path}")
    return _FileSnapshot(True, path.read_bytes(), stat.S_IMODE(info.st_mode))


def _restore_files(snapshots: dict[Path, _FileSnapshot]) -> list[str]:
    failures: list[str] = []
    for path, snapshot in reversed(tuple(snapshots.items())):
        try:
            if snapshot.existed:
                _atomic_write_bytes(path, snapshot.payload, mode=snapshot.mode)
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            failures.append(f"{path}: {exc}")
    return failures


def _unit_state(
    unit: str, *, system: bool, _run=subprocess.run
) -> tuple[bool, bool]:
    enabled = (
        _systemctl(["is-enabled", unit], system=system, _run=_run).returncode == 0
    )
    active = (
        _systemctl(["is-active", unit], system=system, _run=_run).returncode == 0
    )
    return enabled, active


def _rollback_install(
    *,
    units: list[str],
    prior_states: dict[str, tuple[bool, bool]],
    snapshots: dict[Path, _FileSnapshot],
    system: bool,
    services_mutated: bool,
    _run=subprocess.run,
) -> None:
    """Restore both on-disk configuration and pre-install unit state."""
    failures: list[str] = []
    if services_mutated:
        for unit in reversed(units):
            result = _systemctl(
                ["disable", "--now", unit], system=system, _run=_run
            )
            if result.returncode != 0:
                failures.append(f"stop/disable {unit}")
    failures.extend(_restore_files(snapshots))
    if _systemctl(["daemon-reload"], system=system, _run=_run).returncode != 0:
        failures.append("daemon-reload")
    if services_mutated:
        for unit in units:
            enabled, active = prior_states[unit]
            if enabled:
                if (
                    _systemctl(["enable", unit], system=system, _run=_run).returncode
                    != 0
                ):
                    failures.append(f"re-enable {unit}")
            if active:
                if (
                    _systemctl(["start", unit], system=system, _run=_run).returncode
                    != 0
                ):
                    failures.append(f"restart {unit}")
    if failures:
        print(
            "ERROR: install rollback was incomplete: " + ", ".join(failures),
            file=sys.stderr,
        )


def _systemctl(args: list[str], *, system: bool, _run=subprocess.run):
    argv = ["systemctl"] if system else ["systemctl", "--user"]
    try:
        return _run([*argv, *args], capture_output=True, text=True)
    except OSError as exc:
        # A missing systemctl (or a broken user bus) should be reported as an
        # ordinary lifecycle failure, not an unhandled traceback after files
        # have already been written.
        return subprocess.CompletedProcess([*argv, *args], 127, "", str(exc))


def _systemd_available(*, system: bool, _run=subprocess.run) -> bool:
    return runner._systemd_available(system=system, _run=_run)


def _active_legacy_runner_scopes(*, _run=subprocess.run) -> tuple[bool, ...]:
    """Return every manager scope where the legacy runner is active.

    Query both managers before adopting a host. A user-scope node install can
    otherwise silently coexist with a system-scope legacy runner, and vice
    versa. Non-zero status (including an inaccessible user bus) is conservative
    only for the active-state probe; system installs add a service-account unit
    file check below because the target user's bus is not necessarily ours.
    """

    return tuple(
        system
        for system in (False, True)
        if _systemctl(["is-active", _LEGACY_RUNNER_UNIT], system=system, _run=_run).returncode
        == 0
    )


def _render_unit(role: str, *, profile: str, system: bool, env_file: Path) -> str:
    spec = _require_role(role)
    if not _profile_role_executable(profile, role):
        raise ValueError(f"role {role!r} is planned but not installable yet")
    if role == "database-backup" and not system:
        raise ValueError("database backup is supported only as a system service")
    template = (
        _EDGE_SYSTEM_TEMPLATE
        if role == "edge" and system
        else _EDGE_USER_TEMPLATE
        if role == "edge"
        else _DATABASE_SYSTEM_TEMPLATE
        if role == "database" and system
        else _DATABASE_BACKUP_TIMER_TEMPLATE
        if role == "database-backup"
        else _SYSTEM_TEMPLATE
        if system
        else _USER_TEMPLATE
    )
    env_file_text = _require_safe_path(str(env_file), field="EnvironmentFile path")
    if spec.entrypoint is None:
        raise ValueError(f"role {role!r} has no executable entrypoint")
    requires = _profile_role_requires(profile, role)
    substitutions = {
        "@ROLE@": role,
        "@PYTHON@": sys.executable,
        "@EXECUTABLE@": str(spec.entrypoint),
        "@EXEC_START_PRE@": (
            f"ExecStartPre={sys.executable} {spec.entrypoint} --check"
            if role != "executor"
            else ""
        ),
        "@ENV_FILE@": env_file_text,
        "@ENV_FILE_PREFIX@": "" if spec.env_required else "-",
        "@UNIT_REQUIRES@": (
            f"Requires={' '.join(requires)}" if requires else ""
        ),
        "@UNIT_AFTER@": f"After={' '.join(requires)}" if requires else "",
    }
    if system:
        substitutions.update({"@USER@": _system_install_user(), "@WANTED_BY@": "multi-user.target"})
    return runner.render_unit(str(template), substitutions)


def _render_database_backup_service(*, profile: str, system: bool, env_file: Path) -> str:
    if profile != "local-control-plane" or not system:
        raise ValueError(
            "database backup service belongs only to the system local-control-plane"
        )
    return runner.render_unit(
        str(_DATABASE_BACKUP_SYSTEM_TEMPLATE),
        {
            "@PYTHON@": sys.executable,
            "@EXECUTABLE@": str(_LOCAL_POSTGRES_BACKUP_SERVICE),
            "@ENV_FILE@": _require_safe_path(
                str(env_file), field="EnvironmentFile path"
            ),
        },
    )


def _role_env_text(
    *,
    profile: str,
    url: str,
    token: str | None,
    limits: dict[str, str],
    credential_dir: str | None = None,
) -> str:
    values = {
        "BLUEPRINT_NODE_PROFILE": profile,
        "BLUEPRINT_ORCHESTRATOR_URL": url,
        "BLUEPRINT_CREDENTIAL_DIR": credential_dir or runner._credential_dir(),
        **({"BLUEPRINT_RUNNER_TOKEN": token} if token else {}),
        **limits,
    }
    for key, value in values.items():
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(f"{key} must not contain control characters for a systemd EnvironmentFile")
        if any(char.isspace() for char in value):
            raise ValueError(f"{key} must not contain whitespace for a systemd EnvironmentFile")
    lines = [
        "# Written by blueprint-node install. Mode 0600; do not commit.\n",
        f"BLUEPRINT_NODE_PROFILE={profile}\n",
        f"BLUEPRINT_ORCHESTRATOR_URL={url}\n",
        f"BLUEPRINT_CREDENTIAL_DIR={values['BLUEPRINT_CREDENTIAL_DIR']}\n",
    ]
    if token:
        lines.append(f"BLUEPRINT_RUNNER_TOKEN={token}\n")
    for key in sorted(limits):
        lines.append(f"{key}={limits[key]}\n")
    return "".join(lines)


def _service_role_env_text(
    role: str, values: dict[str, str], *, profile: str
) -> str:
    """Render the exact non-secret configuration contract for a service role."""
    values = dict(values)
    required = {
        "database": {
            "BLUEPRINT_POSTGRES_DATA_DIR",
            "BLUEPRINT_POSTGRES_IMAGE_ID",
            "BLUEPRINT_POSTGRES_SECRETS_FILE",
            "BLUEPRINT_CONTROL_PLANE_NETWORK",
        },
        "database-backup": {
            "BLUEPRINT_POSTGRES_IMAGE_ID",
            "BLUEPRINT_POSTGRES_SECRETS_FILE",
            "BLUEPRINT_POSTGRES_BACKUP_PROJECT_ID",
            "BLUEPRINT_POSTGRES_BACKUP_BUCKET",
            "BLUEPRINT_POSTGRES_BACKUP_CREDENTIALS_FILE",
            "BLUEPRINT_POSTGRES_BACKUP_IMAGE_ID",
            "BLUEPRINT_CONTROL_PLANE_NETWORK",
        },
        "database-proxy": {
            "BLUEPRINT_CLOUDSQL_PROXY_BINARY",
            "BLUEPRINT_CLOUDSQL_PROXY_SHA256",
            "BLUEPRINT_CLOUDSQL_CREDENTIALS_FILE",
            "BLUEPRINT_CLOUDSQL_INSTANCE",
            "BLUEPRINT_CONTROL_PLANE_NETWORK",
        },
        "worker": {
            "BLUEPRINT_WORKER_IMAGE_ID",
            "BLUEPRINT_WORKER_SECRETS_FILE",
            "BLUEPRINT_WORKER_GOOGLE_CREDENTIALS_FILE",
            "BLUEPRINT_CONTROL_PLANE_NETWORK",
        },
        "api": {
            "BLUEPRINT_API_IMAGE_ID",
            "BLUEPRINT_API_SECRETS_FILE",
            "BLUEPRINT_API_GOOGLE_CREDENTIALS_FILE",
            "BLUEPRINT_CONTROL_PLANE_NETWORK",
            "BLUEPRINT_API_PORT",
            "BLUEPRINT_DATABASE_MODE",
        },
        "edge": {
            "BLUEPRINT_TAILSCALE_BINARY",
            "BLUEPRINT_EDGE_API_PORT",
        },
    }.get(role)
    if required is None:
        raise ValueError(f"role {role!r} has no service configuration contract")
    if role in {"worker", "api"}:
        expected_database_mode = (
            "local-postgres"
            if profile == "local-control-plane"
            else "cloudsql-proxy"
        )
        supplied_mode = values.get("BLUEPRINT_DATABASE_MODE")
        if supplied_mode is not None and supplied_mode != expected_database_mode:
            raise ValueError(
                f"{profile} {role} requires "
                f"BLUEPRINT_DATABASE_MODE={expected_database_mode}"
            )
        values["BLUEPRINT_DATABASE_MODE"] = expected_database_mode
        required = {*required, "BLUEPRINT_DATABASE_MODE"}
    missing = required - set(values)
    if missing:
        raise ValueError(f"{role} configuration missing: {', '.join(sorted(missing))}")
    unexpected = set(values) - required
    if unexpected:
        raise ValueError(f"{role} configuration has unsupported keys")
    for key, value in values.items():
        _require_safe_path(value, field=key)
    if role in {"api", "edge"}:
        port_key = (
            "BLUEPRINT_API_PORT" if role == "api" else "BLUEPRINT_EDGE_API_PORT"
        )
        if (
            not values[port_key].isdigit()
            or values[port_key].startswith("0")
            or not (1024 if role == "api" else 1)
            <= int(values[port_key])
            <= 65535
        ):
            minimum = 1024 if role == "api" else 1
            raise ValueError(
                f"{port_key} must be a decimal port from {minimum} to 65535"
            )
    return "".join(
        [
            "# Written by blueprint-node install. Mode 0600; contains paths and identifiers only.\n",
            *(f"{key}={values[key]}\n" for key in sorted(values)),
        ]
    )


def _manifest(profile: str, *, system: bool, user: str | None = None) -> dict:
    roles = _require_profile(profile)
    def role_payload(role: str) -> dict:
        payload = asdict(ROLE_SPECS[role])
        if payload["entrypoint"] is not None:
            payload["entrypoint"] = str(payload["entrypoint"])
        payload["requires"] = list(_profile_role_requires(profile, role))
        return payload

    return {
        "schema_version": _MANIFEST_VERSION,
        "profile": profile,
        "scope": "system" if system else "user",
        "node_root": str(node_root(system=system, user=user)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "roles": [
            {
                **role_payload(role),
                "env_file": str(role_env_path(role, system=system, user=user)),
                "unit_path": str(unit_path(role, system=system)),
                "state": (
                    "installable"
                    if _profile_role_executable(profile, role)
                    else "planned"
                ),
            }
            for role in roles
        ],
    }


def _read_manifest(*, system: bool = False) -> dict | None:
    try:
        value = json.loads(manifest_path(system=system).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _installed_manifest_and_scope(*, requested_system: bool) -> tuple[dict | None, bool]:
    """Find an installed manifest without making system installs invisible.

    A user-scope manifest wins when both scopes exist: it is the least
    surprising default for an unqualified command.  An explicit ``--system``
    always selects the system manifest.  This keeps normal operation concise
    while still letting a shared-host install be discovered by plain status,
    stop, and start commands.
    """

    if requested_system:
        return _read_manifest(system=True), True

    user_manifest = _read_manifest(system=False)
    if user_manifest is not None:
        return user_manifest, bool(user_manifest.get("scope") == "system")

    system_manifest = _read_manifest(system=True)
    if system_manifest is not None:
        return system_manifest, True
    return None, False


def do_install(
    *,
    profile: str,
    system: bool,
    token: str | None,
    url: str,
    dry_run: bool,
    resource_limits: dict[str, str] | None = None,
    role_configs: dict[str, dict[str, str]] | None = None,
    _run=subprocess.run,
) -> int:
    """Install currently executable roles and persist the full node manifest.

    Planned roles are deliberately recorded but not rendered or started.  A
    successful command therefore never claims that the API, worker, edge, or
    Actions service exists before its implementation and credential model land.
    """
    try:
        roles = _require_profile(profile)
        if profile == "local-control-plane" and not system:
            raise ValueError(
                "local-control-plane requires --system; refusing credential-bearing "
                "database/API services in user scope"
            )
        system_user = _system_install_user() if system else None
        credential_dir: str | None = None
        if system:
            credential_path, credential_uid, credential_gid = _system_credential_dir(system_user)
            credential_dir = str(credential_path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not _systemd_available(system=system, _run=_run):
        runner._print_no_systemd_guidance(system=system)
        return 1
    # A node executor and the legacy runner share a credential identity.  Do
    # not start a second claimant on a host that is still running the old unit.
    # Dry-run remains side-effect-free and useful for checking profiles before
    # the operator performs the explicit migration.
    adopts_executor = "executor" in roles
    legacy_scopes = (
        _active_legacy_runner_scopes(_run=_run)
        if adopts_executor and not dry_run
        else ()
    )
    legacy_user_unit = bool(
        adopts_executor
        and system
        and system_user
        and _legacy_user_unit_exists(system_user)
    )
    if legacy_scopes or legacy_user_unit:
        active_scopes = ", ".join(
            "system" if scope else "user" for scope in legacy_scopes
        ) or "target user-unit"
        print(
            "ERROR: legacy blueprint-runner.service is still present in "
            f"{active_scopes} scope. Stop and disable active units in both scopes "
            f"before adoption; for a user-to-system migration, run "
            f"`blueprint-runner uninstall` for {_LEGACY_RUNNER_UNIT}'s user unit. "
            "Refusing to run two runners with the same credentials.",
            file=sys.stderr,
        )
        return 1
    limits = {**PROFILE_DEFAULTS[profile], **(resource_limits or {})}
    role_configs = role_configs or {}
    executable_roles = [
        role for role in roles if _profile_role_executable(profile, role)
    ]
    planned_roles = [
        role for role in roles if not _profile_role_executable(profile, role)
    ]
    if profile == "local-control-plane":
        api_image = role_configs.get("api", {}).get("BLUEPRINT_API_IMAGE_ID")
        worker_image = role_configs.get("worker", {}).get("BLUEPRINT_WORKER_IMAGE_ID")
        if api_image and worker_image and api_image != worker_image:
            print(
                "ERROR: local API and worker must use the same immutable image ID",
                file=sys.stderr,
            )
            return 1
    rendered: dict[str, str] = {}
    rendered_auxiliary: dict[str, str] = {}
    rendered_envs: dict[str, str] = {}
    try:
        for role in executable_roles:
            rendered_envs[role] = (
                _role_env_text(
                    profile=profile,
                    url=url,
                    token=token,
                    limits=limits,
                    credential_dir=credential_dir,
                )
                if role == "executor"
                else _service_role_env_text(
                    role, role_configs.get(role, {}), profile=profile
                )
            )
            env_path = role_env_path(
                role, system=system, user=system_user
            )
            rendered[role] = _render_unit(
                role,
                profile=profile,
                system=system,
                env_file=env_path,
            )
            if role == "database-backup":
                rendered_auxiliary[role] = _render_database_backup_service(
                    profile=profile, system=system, env_file=env_path
                )
    except (OSError, ValueError) as exc:
        print(f"ERROR: could not render node service: {exc}", file=sys.stderr)
        return 1

    if dry_run:
        print(f"[dry-run] profile: {profile} ({'system' if system else 'user'} scope)")
        for role in executable_roles:
            print(f"[dry-run] would write: {unit_path(role, system=system)}")
            print(f"[dry-run] would seed:  {role_env_path(role, system=system, user=system_user)}")
            print(f"[dry-run] would enable: {ROLE_SPECS[role].unit_name}")
            print(rendered[role])
            if role == "database-backup":
                print(
                    "[dry-run] would write: "
                    f"{_database_backup_service_unit_path(system=system)}"
                )
                print(rendered_auxiliary[role])
        for role in planned_roles:
            print(f"[dry-run] planned, not installed: {role} ({ROLE_SPECS[role].description})")
        return 0

    files_to_write: dict[Path, tuple[str, int]] = {}
    for role in executable_roles:
        files_to_write[
            role_env_path(role, system=system, user=system_user)
        ] = (rendered_envs[role], 0o600)
        files_to_write[unit_path(role, system=system)] = (rendered[role], 0o644)
        if role == "database-backup":
            files_to_write[
                _database_backup_service_unit_path(system=system)
            ] = (rendered_auxiliary[role], 0o644)
    files_to_write[manifest_path(system=system, user=system_user)] = (
        json.dumps(
            _manifest(profile, system=system, user=system_user), indent=2
        )
        + "\n",
        0o600,
    )
    units = [ROLE_SPECS[role].unit_name for role in executable_roles]
    snapshots: dict[Path, _FileSnapshot] = {}
    prior_states = {
        unit: _unit_state(unit, system=system, _run=_run) for unit in units
    }
    try:
        if profile == "local-control-plane":
            _prepare_local_postgres_data_dir(
                role_configs["database"]["BLUEPRINT_POSTGRES_DATA_DIR"]
            )
        if system:
            _prepare_system_node_root(node_root(system=True, user=system_user))
            _prepare_system_credential_dir(
                credential_path, uid=credential_uid, gid=credential_gid
            )
        snapshots = {
            path: _snapshot_file(path) for path in files_to_write
        }
        for path, (text, mode) in files_to_write.items():
            _atomic_write(path, text, mode=mode)
    except (OSError, ValueError) as exc:
        if snapshots:
            _rollback_install(
                units=units,
                prior_states=prior_states,
                snapshots=snapshots,
                system=system,
                services_mutated=False,
                _run=_run,
            )
        print(f"ERROR: could not write node configuration: {exc}", file=sys.stderr)
        return 1

    if _systemctl(["daemon-reload"], system=system, _run=_run).returncode != 0:
        _rollback_install(
            units=units,
            prior_states=prior_states,
            snapshots=snapshots,
            system=system,
            services_mutated=False,
            _run=_run,
        )
        print("ERROR: systemctl daemon-reload failed", file=sys.stderr)
        return 1
    for role in executable_roles:
        unit = ROLE_SPECS[role].unit_name
        result = _systemctl(["enable", "--now", unit], system=system, _run=_run)
        if result.returncode != 0:
            _rollback_install(
                units=units,
                prior_states=prior_states,
                snapshots=snapshots,
                system=system,
                services_mutated=True,
                _run=_run,
            )
            print(f"ERROR: could not enable {unit}: {(result.stderr or '').strip()}", file=sys.stderr)
            return 1
        if prior_states[unit][1]:
            result = _systemctl(["restart", unit], system=system, _run=_run)
            if result.returncode != 0:
                _rollback_install(
                    units=units,
                    prior_states=prior_states,
                    snapshots=snapshots,
                    system=system,
                    services_mutated=True,
                    _run=_run,
                )
                print(
                    f"ERROR: could not restart {unit} with updated configuration: "
                    f"{(result.stderr or '').strip()}",
                    file=sys.stderr,
                )
                return 1
        if _systemctl(["is-active", unit], system=system, _run=_run).returncode != 0:
            _rollback_install(
                units=units,
                prior_states=prior_states,
                snapshots=snapshots,
                system=system,
                services_mutated=True,
                _run=_run,
            )
            print(f"ERROR: {unit} did not become active", file=sys.stderr)
            return 1
    if not system:
        _run(["loginctl", "enable-linger", _install_user()], capture_output=True, text=True)

    print(f"[blueprint-node] installed profile {profile}.")
    for role in executable_roles:
        print(f"  active role:  {role} ({ROLE_SPECS[role].unit_name})")
    for role in planned_roles:
        print(f"  planned role: {role} — not installed until its implementation lands")
    return 0


def do_status(*, profile: str | None, system: bool, _run=subprocess.run) -> int:
    manifest, system = _installed_manifest_and_scope(requested_system=system)
    selected_profile = profile or (manifest or {}).get("profile")
    if not isinstance(selected_profile, str):
        print("No blueprint-node manifest found. Run `blueprint-node install --profile ...`.")
        return 1
    try:
        roles = _require_profile(selected_profile)
    except ValueError as exc:
        print(f"ERROR: manifest has {exc}", file=sys.stderr)
        return 1
    print(f"Blueprint node profile: {selected_profile}")
    for role in roles:
        spec = ROLE_SPECS[role]
        if not _profile_role_executable(selected_profile, role):
            print(f"  {role:<15} planned   {spec.description}")
            continue
        stopped = stopped_path(role, system=system).is_file()
        active = _systemctl(["is-active", spec.unit_name], system=system, _run=_run).returncode == 0
        enabled = _systemctl(["is-enabled", spec.unit_name], system=system, _run=_run).returncode == 0
        state = "stopped" if stopped else ("active" if active else "inactive")
        print(f"  {role:<15} {state:<9} enabled={'yes' if enabled else 'no'} unit={spec.unit_name}")
    return 0


def do_stop(*, role: str, system: bool, dry_run: bool, _run=subprocess.run) -> int:
    """Immediately stop a role.

    This is intentionally *not* named drain: the current local runner cannot
    decline new work while keeping an arbitrarily long active job alive.  A
    future node-agent protocol can add a true coordinated drain command.
    """
    spec = _require_role(role)
    if not spec.executable:
        print(f"ERROR: {role} is planned and has no service to stop yet.", file=sys.stderr)
        return 2
    _manifest, system = _installed_manifest_and_scope(requested_system=system)
    if dry_run:
        print(f"[dry-run] would stop {spec.unit_name} and write {stopped_path(role, system=system)}")
        return 0
    result = _systemctl(["stop", spec.unit_name], system=system, _run=_run)
    if result.returncode != 0:
        print(f"ERROR: could not stop {spec.unit_name}: {(result.stderr or '').strip()}", file=sys.stderr)
        return 1
    result = _systemctl(["disable", spec.unit_name], system=system, _run=_run)
    if result.returncode != 0:
        print(
            f"ERROR: stopped {spec.unit_name}, but could not disable it for reboot: "
            f"{(result.stderr or '').strip()}",
            file=sys.stderr,
        )
        return 1
    _atomic_write(
        stopped_path(role, system=system),
        json.dumps({"role": role, "stopped_at": datetime.now(timezone.utc).isoformat()}) + "\n",
    )
    print(f"[blueprint-node] {role} stopped immediately; active work may have been interrupted.")
    return 0


def do_start(*, role: str, system: bool, _run=subprocess.run) -> int:
    spec = _require_role(role)
    if not spec.executable:
        print(f"ERROR: {role} is planned and has no service to start yet.", file=sys.stderr)
        return 2
    _manifest, system = _installed_manifest_and_scope(requested_system=system)
    result = _systemctl(["enable", "--now", spec.unit_name], system=system, _run=_run)
    if result.returncode != 0:
        print(f"ERROR: could not enable/start {spec.unit_name}: {(result.stderr or '').strip()}", file=sys.stderr)
        return 1
    try:
        stopped_path(role, system=system).unlink()
    except FileNotFoundError:
        pass
    print(f"[blueprint-node] {role} started.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blueprint-node", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    install = sub.add_parser("install", help="Install an additive node profile.")
    install.add_argument("--profile", choices=sorted(PROFILE_ROLES), required=True)
    install.add_argument("--system", action="store_true")
    install.add_argument("--url")
    install.add_argument("--dry-run", action="store_true")
    install.add_argument("--cloudsql-instance")
    install.add_argument("--cloudsql-proxy-binary")
    install.add_argument("--cloudsql-proxy-sha256")
    install.add_argument("--cloudsql-credentials-file")
    install.add_argument("--worker-image-id")
    install.add_argument("--worker-secrets-file")
    install.add_argument("--worker-google-credentials-file")
    install.add_argument("--postgres-image-id")
    install.add_argument("--postgres-data-dir")
    install.add_argument("--postgres-secrets-file")
    install.add_argument("--postgres-backup-project-id")
    install.add_argument("--postgres-backup-bucket")
    install.add_argument("--postgres-backup-credentials-file")
    install.add_argument("--postgres-backup-image-id")
    install.add_argument("--api-image-id")
    install.add_argument("--api-secrets-file")
    install.add_argument("--api-google-credentials-file")
    install.add_argument("--api-port", default="8080")
    install.add_argument("--tailscale-binary", default="/usr/bin/tailscale")
    install.add_argument(
        "--control-plane-network", default="blueprint-control-plane"
    )
    for flag, _env, dest, help_text in runner._RESOURCE_LIMIT_VARS:
        install.add_argument(flag, dest=dest, default=None, help=help_text)
    status = sub.add_parser("status", help="Show all roles in a node profile.")
    status.add_argument("--profile", choices=sorted(PROFILE_ROLES))
    status.add_argument("--system", action="store_true")
    for command, help_text in (
        ("stop", "Immediately stop an executable role; this can interrupt active work."),
        ("start", "Start a previously stopped executable role."),
    ):
        role_parser = sub.add_parser(command, help=help_text)
        role_parser.add_argument("--role", choices=sorted(ROLE_SPECS), default="executor")
        role_parser.add_argument("--system", action="store_true")
        if command == "stop":
            role_parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "install":
        limits = {
            env: str(getattr(args, dest))
            for _flag, env, dest, _help in runner._RESOURCE_LIMIT_VARS
            if getattr(args, dest) is not None
        }
        role_configs: dict[str, dict[str, str]] = {}
        if args.profile in {"primary", "control-plane"}:
            role_configs = {
                "database-proxy": {
                    key: value
                    for key, value in {
                        "BLUEPRINT_CLOUDSQL_PROXY_BINARY": args.cloudsql_proxy_binary,
                        "BLUEPRINT_CLOUDSQL_PROXY_SHA256": args.cloudsql_proxy_sha256,
                        "BLUEPRINT_CLOUDSQL_CREDENTIALS_FILE": args.cloudsql_credentials_file,
                        "BLUEPRINT_CLOUDSQL_INSTANCE": args.cloudsql_instance,
                        "BLUEPRINT_CONTROL_PLANE_NETWORK": args.control_plane_network,
                    }.items()
                    if value
                },
                "worker": {
                    key: value
                    for key, value in {
                        "BLUEPRINT_WORKER_IMAGE_ID": args.worker_image_id,
                        "BLUEPRINT_WORKER_SECRETS_FILE": args.worker_secrets_file,
                        "BLUEPRINT_WORKER_GOOGLE_CREDENTIALS_FILE": args.worker_google_credentials_file,
                        "BLUEPRINT_CONTROL_PLANE_NETWORK": args.control_plane_network,
                    }.items()
                    if value
                },
            }
        elif args.profile == "local-control-plane":
            role_configs = {
                "database": {
                    key: value
                    for key, value in {
                        "BLUEPRINT_POSTGRES_IMAGE_ID": args.postgres_image_id,
                        "BLUEPRINT_POSTGRES_DATA_DIR": args.postgres_data_dir,
                        "BLUEPRINT_POSTGRES_SECRETS_FILE": args.postgres_secrets_file,
                        "BLUEPRINT_CONTROL_PLANE_NETWORK": args.control_plane_network,
                    }.items()
                    if value
                },
                "database-backup": {
                    key: value
                    for key, value in {
                        "BLUEPRINT_POSTGRES_IMAGE_ID": args.postgres_image_id,
                        "BLUEPRINT_POSTGRES_SECRETS_FILE": args.postgres_secrets_file,
                        "BLUEPRINT_POSTGRES_BACKUP_PROJECT_ID": (
                            args.postgres_backup_project_id
                        ),
                        "BLUEPRINT_POSTGRES_BACKUP_BUCKET": args.postgres_backup_bucket,
                        "BLUEPRINT_POSTGRES_BACKUP_CREDENTIALS_FILE": (
                            args.postgres_backup_credentials_file
                        ),
                        "BLUEPRINT_POSTGRES_BACKUP_IMAGE_ID": (
                            args.postgres_backup_image_id
                        ),
                        "BLUEPRINT_CONTROL_PLANE_NETWORK": args.control_plane_network,
                    }.items()
                    if value
                },
                "api": {
                    key: value
                    for key, value in {
                        "BLUEPRINT_API_IMAGE_ID": args.api_image_id,
                        "BLUEPRINT_API_SECRETS_FILE": args.api_secrets_file,
                        "BLUEPRINT_API_GOOGLE_CREDENTIALS_FILE": (
                            args.api_google_credentials_file
                        ),
                        "BLUEPRINT_CONTROL_PLANE_NETWORK": args.control_plane_network,
                        "BLUEPRINT_API_PORT": args.api_port,
                        "BLUEPRINT_DATABASE_MODE": "local-postgres",
                    }.items()
                    if value
                },
                "worker": {
                    key: value
                    for key, value in {
                        "BLUEPRINT_WORKER_IMAGE_ID": args.worker_image_id,
                        "BLUEPRINT_WORKER_SECRETS_FILE": args.worker_secrets_file,
                        "BLUEPRINT_WORKER_GOOGLE_CREDENTIALS_FILE": (
                            args.worker_google_credentials_file
                        ),
                        "BLUEPRINT_CONTROL_PLANE_NETWORK": args.control_plane_network,
                        "BLUEPRINT_DATABASE_MODE": "local-postgres",
                    }.items()
                    if value
                },
                "edge": {
                    "BLUEPRINT_TAILSCALE_BINARY": args.tailscale_binary,
                    "BLUEPRINT_EDGE_API_PORT": args.api_port,
                },
            }
        return do_install(
            profile=args.profile,
            system=args.system,
            token=resolve_install_token(system=args.system),
            url=runner.resolve_url(args.url),
            dry_run=args.dry_run,
            resource_limits=limits,
            role_configs=role_configs,
        )
    if args.command == "status":
        return do_status(profile=args.profile, system=args.system)
    if args.command == "stop":
        return do_stop(role=args.role, system=args.system, dry_run=args.dry_run)
    return do_start(role=args.role, system=args.system)


if __name__ == "__main__":
    raise SystemExit(main())

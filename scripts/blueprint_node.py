#!/usr/bin/env python3
"""Additive lifecycle facade for a multi-role Blueprint node.

This intentionally starts small: it installs the existing local runner as the
``executor`` sibling service and records the complete future role manifest.  It
does *not* start an API, scheduler, edge proxy, or GitHub Actions runner yet;
those require separate lifecycle, credential, and fencing work.  Naming those
roles now gives every host one durable manifest and per-role secret paths
without pretending an unimplemented service is healthy.

``blueprint-runner`` remains unchanged and supported.  ``blueprint-node`` is an
opt-in facade for new hosts and never modifies the legacy unit or env file.

The executor has no coordinated drain protocol yet.  Its lifecycle command is
therefore deliberately named ``stop`` and warns that active work can be
interrupted; it must not be used as a maintenance drain.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
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
_USER_TEMPLATE = _SCRIPT_DIR / "blueprint-node.service"
_SYSTEM_TEMPLATE = _SCRIPT_DIR / "blueprint-node-system.service"
_MANIFEST_VERSION = 1
_SYSTEM_NODE_ROOT = Path("/etc/blueprint/nodes")
_SYSTEM_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class RoleSpec:
    name: str
    unit_name: str
    executable: bool
    description: str


ROLE_SPECS: dict[str, RoleSpec] = {
    "node-agent": RoleSpec(
        "node-agent", "blueprint-node-agent.service", False,
        "node inventory, capacity ledger, coordinated updates, and draining",
    ),
    "executor": RoleSpec(
        "executor", "blueprint-executor.service", True,
        "Blueprint agent and compute lanes (current local_runner implementation)",
    ),
    "github-actions": RoleSpec(
        "github-actions", "blueprint-actions.service", False,
        "official self-hosted GitHub Actions runner lifecycle",
    ),
    "api": RoleSpec(
        "api", "blueprint-api.service", False, "local Blueprint HTTP API",
    ),
    "worker": RoleSpec(
        "worker", "blueprint-worker.service", False,
        "scheduler, dispatch, reapers, and background loops",
    ),
    "edge": RoleSpec(
        "edge", "blueprint-edge.service", False,
        "authenticated public tunnel or reverse proxy",
    ),
}

PROFILE_ROLES: dict[str, tuple[str, ...]] = {
    "primary": ("node-agent", "executor", "github-actions", "api", "worker", "edge"),
    "worker": ("node-agent", "executor", "github-actions"),
    "gpu-worker": ("node-agent", "executor"),
}

PROFILE_DEFAULTS: dict[str, dict[str, str]] = {
    "primary": {},
    "worker": {},
    "gpu-worker": {
        "BLUEPRINT_RUNNER_MAX_CONCURRENT_COMPUTE_JOBS": "1",
        "BLUEPRINT_RUNNER_GPUS_PER_JOB": "1",
    },
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


def unit_path(role: str, *, system: bool) -> Path:
    spec = _require_role(role)
    if system:
        return Path("/etc/systemd/system") / spec.unit_name
    return Path("~/.config/systemd/user").expanduser() / spec.unit_name


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
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
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


def _render_unit(role: str, *, system: bool, env_file: Path) -> str:
    spec = _require_role(role)
    if not spec.executable:
        raise ValueError(f"role {role!r} is planned but not installable yet")
    template = _SYSTEM_TEMPLATE if system else _USER_TEMPLATE
    env_file_text = _require_safe_path(str(env_file), field="EnvironmentFile path")
    substitutions = {
        "@ROLE@": role,
        "@PYTHON@": sys.executable,
        "@EXECUTABLE@": str(_LOCAL_RUNNER),
        "@ENV_FILE@": env_file_text,
    }
    if system:
        substitutions.update({"@USER@": _system_install_user(), "@WANTED_BY@": "multi-user.target"})
    return runner.render_unit(str(template), substitutions)


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


def _manifest(profile: str, *, system: bool, user: str | None = None) -> dict:
    roles = _require_profile(profile)
    return {
        "schema_version": _MANIFEST_VERSION,
        "profile": profile,
        "scope": "system" if system else "user",
        "node_root": str(node_root(system=system, user=user)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "roles": [
            {
                **asdict(ROLE_SPECS[role]),
                "env_file": str(role_env_path(role, system=system, user=user)),
                "unit_path": str(unit_path(role, system=system)),
                "state": "installable" if ROLE_SPECS[role].executable else "planned",
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
    _run=subprocess.run,
) -> int:
    """Install currently executable roles and persist the full node manifest.

    Planned roles are deliberately recorded but not rendered or started.  A
    successful command therefore never claims that the API, worker, edge, or
    Actions service exists before its implementation and credential model land.
    """
    try:
        roles = _require_profile(profile)
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
    legacy_scopes = _active_legacy_runner_scopes(_run=_run) if not dry_run else ()
    legacy_user_unit = bool(
        system and system_user and _legacy_user_unit_exists(system_user)
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
    executable_roles = [role for role in roles if ROLE_SPECS[role].executable]
    planned_roles = [role for role in roles if not ROLE_SPECS[role].executable]
    rendered: dict[str, str] = {}
    try:
        for role in executable_roles:
            rendered[role] = _render_unit(
                role,
                system=system,
                env_file=role_env_path(role, system=system, user=system_user),
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
        for role in planned_roles:
            print(f"[dry-run] planned, not installed: {role} ({ROLE_SPECS[role].description})")
        return 0

    try:
        if system:
            _prepare_system_node_root(node_root(system=True, user=system_user))
            _prepare_system_credential_dir(
                credential_path, uid=credential_uid, gid=credential_gid
            )
        for role in executable_roles:
            _atomic_write(
                role_env_path(role, system=system, user=system_user),
                _role_env_text(
                    profile=profile,
                    url=url,
                    token=token,
                    limits=limits,
                    credential_dir=credential_dir,
                ),
            )
            _atomic_write(unit_path(role, system=system), rendered[role], mode=0o644)
        _atomic_write(
            manifest_path(system=system, user=system_user),
            json.dumps(_manifest(profile, system=system, user=system_user), indent=2) + "\n",
        )
    except (OSError, ValueError) as exc:
        print(f"ERROR: could not write node configuration: {exc}", file=sys.stderr)
        return 1

    if _systemctl(["daemon-reload"], system=system, _run=_run).returncode != 0:
        print("ERROR: systemctl daemon-reload failed", file=sys.stderr)
        return 1
    for role in executable_roles:
        unit = ROLE_SPECS[role].unit_name
        result = _systemctl(["enable", "--now", unit], system=system, _run=_run)
        if result.returncode != 0:
            print(f"ERROR: could not enable {unit}: {(result.stderr or '').strip()}", file=sys.stderr)
            return 1
        if _systemctl(["is-active", unit], system=system, _run=_run).returncode != 0:
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
        if not spec.executable:
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
        return do_install(
            profile=args.profile,
            system=args.system,
            token=resolve_install_token(system=args.system),
            url=runner.resolve_url(args.url),
            dry_run=args.dry_run,
            resource_limits=limits,
        )
    if args.command == "status":
        return do_status(profile=args.profile, system=args.system)
    if args.command == "stop":
        return do_stop(role=args.role, system=args.system, dry_run=args.dry_run)
    return do_start(role=args.role, system=args.system)


if __name__ == "__main__":
    raise SystemExit(main())

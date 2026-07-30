#!/usr/bin/env python3
"""Plan a hardened GitHub Actions runner role inside a Blueprint KVM guest.

This module deliberately does *not* download, register, or start a GitHub
runner.  ``blueprint-node`` can call it later to obtain deterministic paths,
systemd units, and lifecycle commands without ever putting a registration token
on an argv or in a unit file.

Two safety profiles are supported:

* ``trusted``: persistent or ephemeral internal-workflow runner inside the
  dedicated Actions VM. Docker is disabled by default; a caller may opt into a
  dedicated *guest* socket, never the physical host's Docker socket.
* ``untrusted``: ephemeral-only fork/public-workflow runner. It receives a
  dedicated Unix user, no production credentials, and an explicit systemd deny
  for both conventional Docker socket paths. It is still an initial local
  isolation profile inside the mandatory KVM boundary.

Run ``python3 scripts/github_actions_runner.py plan ...`` to print JSON.  The
installer owns all privileged writes and token exchange.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping
from urllib.parse import urlparse


_SCRIPT_DIR = Path(__file__).resolve().parent
_TEMPLATE_DIR = _SCRIPT_DIR / "templates"
_ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,63}$")
_FORBIDDEN_UNTRUSTED_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AZURE_CLIENT_SECRET",
        "BLUEPRINT_RUNNER_TOKEN",
        "CODEX_API_KEY",
        "DATABASE_URL",
        "GCP_SERVICE_ACCOUNT_KEY",
        "GITHUB_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "OPENAI_API_KEY",
        "SSH_AUTH_SOCK",
    }
)
_SHARED_DOCKER_SOCKETS = frozenset({"/var/run/docker.sock", "/run/docker.sock"})


class SafetyMode(StrEnum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


@dataclass(frozen=True)
class GitHubActionsRunnerRole:
    """Resolved, filesystem-safe configuration for one service role."""

    name: str
    safety_mode: str
    ephemeral: bool
    registration_url: str
    labels: tuple[str, ...]
    runner_user: str
    runner_group: str
    runner_root: str
    state_root: str
    env_file: str
    registration_token_file: str
    unit_name: str
    unit_path: str
    docker_socket: str | None

    @property
    def effective_role(self) -> str:
        return f"{self.safety_mode}-{self.name}"


def _validate_absolute_path(value: str, field: str) -> str:
    # These values are rendered into Linux systemd units even when the planner
    # runs on a developer's Windows workstation, so use POSIX path semantics
    # rather than host ``pathlib.Path`` semantics.
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or any(ch.isspace() for ch in value):
        raise ValueError(f"{field} must be an absolute, whitespace-free path without '..'")
    return str(path)


def _validate_environment_value(value: str, field: str) -> str:
    """Keep generated EnvironmentFile entries unambiguous for systemd."""
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} must not contain control characters")
    if any(char.isspace() for char in value) or any(char in value for char in "\\\"'`#"):
        raise ValueError(f"{field} must be safe for an unquoted systemd EnvironmentFile value")
    return value


def _validate_registration_url(value: str) -> str:
    _validate_environment_value(value, "registration_url")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("registration_url must be a clean https GitHub or GitHub Enterprise URL")
    return value.rstrip("/")


def _normalize_labels(labels: Iterable[str]) -> tuple[str, ...]:
    normalized = sorted({label.strip() for label in labels if label and label.strip()})
    invalid = [label for label in normalized if not _LABEL_RE.fullmatch(label)]
    if invalid:
        raise ValueError(f"invalid GitHub Actions runner label(s): {invalid}")
    return tuple(normalized)


def build_role(
    *,
    name: str,
    registration_url: str,
    safety_mode: SafetyMode | str,
    ephemeral: bool = False,
    labels: Iterable[str] = (),
    runner_root: str | None = None,
    state_parent: str = "/var/lib/blueprint/github-actions",
    env_parent: str = "/etc/blueprint/github-actions",
    docker_socket: str | None = None,
) -> GitHubActionsRunnerRole:
    """Validate and resolve a role without touching the host or GitHub.

    The resolved paths are intentionally role-scoped.  A trusted runner and an
    untrusted runner can therefore never accidentally share a Unix account,
    work directory, environment file, or systemd unit.
    """

    if not _ROLE_RE.fullmatch(name):
        raise ValueError("role name must match [a-z][a-z0-9-]{0,31}")
    mode = SafetyMode(safety_mode)
    if mode is SafetyMode.UNTRUSTED and not ephemeral:
        raise ValueError("untrusted GitHub Actions runners must be ephemeral")
    if docker_socket is not None:
        docker_socket = _validate_absolute_path(docker_socket, "docker_socket")
        if docker_socket in _SHARED_DOCKER_SOCKETS:
            raise ValueError("the host shared Docker socket is never permitted")
        if mode is SafetyMode.UNTRUSTED:
            raise ValueError("untrusted GitHub Actions runners may not receive a Docker socket")

    state_parent = _validate_absolute_path(state_parent, "state_parent")
    env_parent = _validate_absolute_path(env_parent, "env_parent")
    effective_role = f"{mode.value}-{name}"
    # Linux user/group names are commonly limited to 32 bytes.  Keep the
    # operator-facing role readable while using a bounded, collision-resistant
    # account suffix even when the full role label is long.
    account_hash = hashlib.sha256(effective_role.encode("utf-8")).hexdigest()[:10]
    account_name = f"bpgha-{mode.value[0]}-{name[:10]}-{account_hash}"
    state_root = f"{state_parent}/{effective_role}"
    if runner_root is None:
        runner_root = f"{state_root}/runner"
    runner_root = _validate_absolute_path(runner_root, "runner_root")
    labels = _normalize_labels(("self-hosted", "blueprint", mode.value, *labels))
    unit_name = f"blueprint-github-actions-{effective_role}.service"
    return GitHubActionsRunnerRole(
        name=name,
        safety_mode=mode.value,
        ephemeral=ephemeral,
        registration_url=_validate_registration_url(registration_url),
        labels=labels,
        runner_user=account_name,
        runner_group=account_name,
        runner_root=runner_root,
        state_root=state_root,
        env_file=f"{env_parent}/{effective_role}.env",
        registration_token_file=f"{env_parent}/{effective_role}.registration-token",
        unit_name=unit_name,
        unit_path=f"/etc/systemd/system/{unit_name}",
        docker_socket=docker_socket,
    )


def validate_environment(role: GitHubActionsRunnerRole, environment: Mapping[str, str]) -> None:
    """Reject production-like credentials from an untrusted role's env file."""

    if role.safety_mode != SafetyMode.UNTRUSTED.value:
        return
    forbidden = sorted(
        key
        for key, value in environment.items()
        if value and (key in _FORBIDDEN_UNTRUSTED_ENV or key.endswith(("_TOKEN", "_SECRET", "_API_KEY")))
    )
    if forbidden:
        raise ValueError(
            "untrusted runner environment may not contain production credentials: "
            + ", ".join(forbidden)
        )


def render_environment(role: GitHubActionsRunnerRole, environment: Mapping[str, str] | None = None) -> str:
    """Render a role env file without registration tokens or shell interpolation."""

    environment = dict(environment or {})
    validate_environment(role, environment)
    lines = [
        "# Generated by github_actions_runner.py; mode 0600, owned by root.",
        f"BLUEPRINT_GITHUB_ACTIONS_ROLE={role.effective_role}",
        f"RUNNER_NAME={role.effective_role}",
    ]
    for key, value in sorted(environment.items()):
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            raise ValueError(f"invalid environment key: {key!r}")
        lines.append(f"{key}={_validate_environment_value(value, f'environment value for {key}')}")
    return "\n".join(lines) + "\n"


def _render_template(template_name: str, substitutions: Mapping[str, str]) -> str:
    template = (_TEMPLATE_DIR / template_name).read_text(encoding="utf-8")
    rendered = template
    for key, value in substitutions.items():
        rendered = rendered.replace(f"@{key}@", value)
    unresolved = re.findall(r"@[A-Z_]+@", rendered)
    if unresolved:
        raise RuntimeError(f"unresolved systemd template tokens: {sorted(set(unresolved))}")
    return rendered


def render_systemd_unit(role: GitHubActionsRunnerRole) -> str:
    """Render the appropriate unit without embedding any secret value."""

    is_untrusted = role.safety_mode == SafetyMode.UNTRUSTED.value
    template_name = (
        "github-actions-runner-untrusted.service.template"
        if is_untrusted
        else "github-actions-runner-trusted.service.template"
    )
    unit = _render_template(
        template_name,
        {
            "ROLE": role.effective_role,
            "USER": role.runner_user,
            "GROUP": role.runner_group,
            "RUNNER_ROOT": role.runner_root,
            "STATE_ROOT": role.state_root,
            "ENV_FILE": role.env_file,
            "RESTART": "no" if role.ephemeral else "always",
            "STOP_TIMEOUT": "7200" if role.safety_mode == SafetyMode.TRUSTED.value else "900",
        },
    )
    if role.docker_socket:
        # A caller may opt a trusted role into a *dedicated* socket. Never
        # render a shared daemon socket, which build/test jobs could use to
        # take over the host.
        unit = unit.replace(
            "PrivateTmp=yes\n",
            f"PrivateTmp=yes\nBindReadOnlyPaths={role.docker_socket}\n",
        )
    return unit


def lifecycle_plan(role: GitHubActionsRunnerRole) -> dict[str, list[str]]:
    """Return non-executing install/status/drain/uninstall instructions."""

    labels = ",".join(role.labels)
    registration = [
        "test -f /var/lib/blueprint-actions/BOOTSTRAP_COMPLETE",
        f"getent group {shlex.quote(role.runner_group)} >/dev/null || groupadd --system {shlex.quote(role.runner_group)}",
        f"id -u {shlex.quote(role.runner_user)} >/dev/null 2>&1 || useradd --system --gid {shlex.quote(role.runner_group)} --home-dir {shlex.quote(role.state_root)} --create-home --shell /usr/sbin/nologin {shlex.quote(role.runner_user)}",
        f"test \"$(id -gn {shlex.quote(role.runner_user)})\" = {shlex.quote(role.runner_group)}",
        f"install -d -m 0750 -o {role.runner_user} -g {role.runner_group} {shlex.quote(role.state_root)}",
        f"install -d -m 0750 -o {role.runner_user} -g {role.runner_group} {shlex.quote(role.runner_root)}",
        f"install -d -m 0700 -o root -g root {shlex.quote(str(PurePosixPath(role.env_file).parent))}",
        f"install -m 0600 -o root -g root <role-env> {shlex.quote(role.env_file)}",
        "# Installer-owned registration step: exchange a short-lived token out of band and redact its child argv/logs.",
        "# config.sh inputs: "
        f"url={role.registration_url}, name={role.effective_role}, labels={labels}, "
        f"work={PurePosixPath(role.state_root) / '_work'}, disableupdate=true, "
        f"ephemeral={str(role.ephemeral).lower()}, token_file={role.registration_token_file}",
        f"install -m 0644 <rendered-unit> {shlex.quote(role.unit_path)} && systemctl daemon-reload && systemctl enable --now {shlex.quote(role.unit_name)}",
    ]
    return {
        "install": registration,
        "status": [
            f"systemctl status {shlex.quote(role.unit_name)} --no-pager",
            f"journalctl -u {shlex.quote(role.unit_name)} -n 100 --no-pager",
            "Confirm the GitHub runner is online and has only the intended labels.",
        ],
        "drain": [
            "In GitHub, disable the runner or remove its routing label so it receives no new jobs.",
            "Wait for the active job to finish; do not use systemctl stop as a job-cancellation mechanism.",
            f"systemctl stop {shlex.quote(role.unit_name)}",
        ],
        "uninstall": [
            f"systemctl disable --now {shlex.quote(role.unit_name)} && rm -f {shlex.quote(role.unit_path)} && systemctl daemon-reload",
            "Remove the runner in GitHub using a fresh removal token, then securely remove the role-scoped state and environment directories.",
        ],
    }


def describe_role(role: GitHubActionsRunnerRole) -> dict:
    """JSON-safe plan consumed by a future ``blueprint-node`` installer."""

    return {
        "role": asdict(role),
        "systemd_unit": render_systemd_unit(role),
        "environment_file": render_environment(role),
        "lifecycle": lifecycle_plan(role),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="print a non-executing role plan as JSON")
    plan.add_argument("--name", required=True, help="short role suffix, e.g. ci")
    plan.add_argument("--url", required=True, dest="registration_url")
    plan.add_argument("--safety", choices=[mode.value for mode in SafetyMode], default="trusted")
    plan.add_argument("--ephemeral", action="store_true")
    plan.add_argument("--label", action="append", default=[])
    plan.add_argument("--runner-root")
    plan.add_argument("--docker-socket", help="trusted role only; must be a dedicated socket")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "plan":
        try:
            role = build_role(
                name=args.name,
                registration_url=args.registration_url,
                safety_mode=args.safety,
                ephemeral=args.ephemeral,
                labels=args.label,
                runner_root=args.runner_root,
                docker_socket=args.docker_socket,
            )
        except ValueError as exc:
            _parser().error(str(exc))
        print(json.dumps(describe_role(role), indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

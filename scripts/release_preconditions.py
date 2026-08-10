"""Read-only checks shared by local release bootstrap and activation."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Labels are the stable, operator-facing names of every read-only check that
# must pass before local_app_release may stop an application unit.  The
# runbook drift guard imports this declaration instead of scraping prose.
PRE_STOP_PRECONDITIONS = (
    "live systemd privilege boundary",
    "worker secret and static migration inputs",
    "candidate migration reachability from live schema",
)


def parse_systemctl_properties(
    payload: str, expected: set[str]
) -> dict[str, str]:
    """Parse the exact key/value interface returned by ``systemctl show``."""
    properties: dict[str, str] = {}
    for line in payload.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in expected or key in properties:
            raise RuntimeError("invalid systemctl property output")
        properties[key] = value
    if set(properties) != expected:
        raise RuntimeError("incomplete systemctl property output")
    return properties


def verify_privilege_boundary(
    install_user: str,
    *,
    agent_unit: str,
    release_unit: str,
    run: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> None:
    """Verify the live systemd-reported privilege boundary.

    Unit-file text is deliberately not inspected here.  ``systemctl show`` is
    the same reported-property interface used by the production check, and it
    exposes the explicit ``NoNewPrivileges`` values systemd has loaded.
    """
    expected = {"User", "NoNewPrivileges"}

    def properties(unit: str) -> dict[str, str]:
        return parse_systemctl_properties(
            run(
                [
                    "/usr/bin/systemctl",
                    "show",
                    unit,
                    "--property=NoNewPrivileges",
                    "--property=User",
                ]
            ).stdout,
            expected,
        )

    agent = properties(agent_unit)
    release = properties(release_unit)
    if agent != {"User": install_user, "NoNewPrivileges": "yes"}:
        raise RuntimeError("deploy agent effective hardening is incomplete")
    if release != {"User": "root", "NoNewPrivileges": "no"}:
        raise RuntimeError("release executor is still subject to no-new-privileges")


@dataclass(frozen=True)
class RehearsalReport:
    """Machine-readable result of checks that precede bounded downtime."""

    candidate: str
    revision: str
    checked: tuple[str, ...]
    deferred: tuple[str, ...]
    units_stopped: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "blueprint.local_app_release_rehearsal.v1",
            "candidate": self.candidate,
            "revision": self.revision,
            "checked": list(self.checked),
            "deferred": list(self.deferred),
            "units_stopped": self.units_stopped,
        }

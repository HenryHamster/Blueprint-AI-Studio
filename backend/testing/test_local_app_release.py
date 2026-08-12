"""Merge-test coverage for the read-only local release rehearsal."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import release_preconditions  # noqa: E402


def test_privilege_boundary_uses_systemd_reported_properties_not_unit_text():
    calls: list[list[str]] = []
    outputs = iter(
        (
            "NoNewPrivileges=yes\nUser=blueprint\n",
            "NoNewPrivileges=no\nUser=root\n",
        )
    )

    def fake_systemctl(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, next(outputs), "")

    release_preconditions.verify_privilege_boundary(
        "blueprint",
        agent_unit="blueprint-deploy-agent.service",
        release_unit="blueprint-release.service",
        run=fake_systemctl,
    )

    assert calls == [
        [
            "/usr/bin/systemctl",
            "show",
            "blueprint-deploy-agent.service",
            "--property=NoNewPrivileges",
            "--property=User",
        ],
        [
            "/usr/bin/systemctl",
            "show",
            "blueprint-release.service",
            "--property=NoNewPrivileges",
            "--property=User",
        ],
    ]


def test_privilege_boundary_rejects_reported_implicit_or_wrong_values():
    def fake_systemctl(argv: list[str]) -> subprocess.CompletedProcess[str]:
        del argv
        return subprocess.CompletedProcess(
            [], 0, "NoNewPrivileges=\nUser=blueprint\n", ""
        )

    with pytest.raises(RuntimeError, match="effective hardening is incomplete"):
        release_preconditions.verify_privilege_boundary(
            "blueprint",
            agent_unit="blueprint-deploy-agent.service",
            release_unit="blueprint-release.service",
            run=fake_systemctl,
        )

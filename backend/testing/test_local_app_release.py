"""Merge-test coverage for the read-only local release rehearsal."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import bootstrap_local_app_release as bootstrap  # noqa: E402
import release_preconditions  # noqa: E402


def test_bootstrap_aborts_before_unit_teardown_on_revision_mismatch(monkeypatch):
    """A staged bundle naming the wrong candidate must never reach systemd.

    Bootstrap is the step that stops and restarts the live API/worker/edge
    units, so a revision it never checked against the staged tree is the
    highest-consequence failure mode in the release path. This exercises the
    real ``bootstrap()`` entrypoint end-to-end (with only filesystem/systemd
    boundary crossings stubbed) and proves the mismatch is caught before any
    of the unit-installing/teardown helpers run.
    """
    requested_revision = "a" * 40
    staged_revision = "b" * 40
    called: list[str] = []

    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "henry_hamster")
    monkeypatch.setattr(bootstrap, "_trusted_directory", lambda path: None)

    def fake_trusted_file(path):
        if path.name == bootstrap._REVISION_MARKER_NAME:
            return staged_revision.encode("ascii")
        return f"payload-for-{path.name}".encode("ascii")

    monkeypatch.setattr(bootstrap, "_trusted_file", fake_trusted_file)
    monkeypatch.setattr(bootstrap, "_source_path", lambda source, name: source / name)

    def record(name):
        def _fail(*_args, **_kwargs):
            called.append(name)
            raise AssertionError(f"{name} must not run before revision verification")

        return _fail

    for attr in (
        "_provision_github_cli_config",
        "_edge_runtime",
        "_install_service_configuration",
        "_install_deploy_agent",
        "_run",
    ):
        monkeypatch.setattr(bootstrap, attr, record(attr))

    with pytest.raises(ValueError, match="RELEASE_REVISION"):
        bootstrap.bootstrap(requested_revision)

    assert called == []


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

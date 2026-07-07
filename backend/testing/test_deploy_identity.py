"""Unit tests for the deploy-level identity resolver and predicate.

The deploy identity (``owner/repo`` of the repo THIS deployment serves) replaces
the per-project ``is_self_repo`` boolean for deploy-level serving observability.
These tests pin the env→runtime-config resolution order, the slug parser's
rejection of malformed values, and the case-insensitive project match.
"""

from __future__ import annotations

import pytest

from app.deploy_identity import (
    _parse_owner_repo,
    deploy_repo_identity,
    project_is_deploy_repo,
    project_matches_deploy_identity,
)


class _Proj:
    """Minimal stand-in for a Project row (no DB needed)."""

    def __init__(self, github_owner=None, github_repo=None, is_self_repo=False):
        self.github_owner = github_owner
        self.github_repo = github_repo
        self.is_self_repo = is_self_repo


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("acme/widget", ("acme", "widget")),
        ("  Acme/Widget  ", ("acme", "widget")),  # trimmed + lowercased
        ("ACME/WIDGET", ("acme", "widget")),
        ("acme / widget", ("acme", "widget")),  # inner whitespace trimmed
        (None, None),
        ("", None),
        ("no-slash", None),
        ("/widget", None),  # empty owner
        ("acme/", None),  # empty repo
        (123, None),  # wrong type
        (["acme", "widget"], None),
    ],
)
def test_parse_owner_repo(raw, expected):
    assert _parse_owner_repo(raw) == expected


def test_deploy_repo_identity_prefers_env(monkeypatch):
    monkeypatch.setenv("DEPLOY_REPO_IDENTITY", "envowner/envrepo")
    # Even if runtime_config has a value, the env wins.
    from app.runtime_config_state import runtime_config

    monkeypatch.setitem(runtime_config, "deploy_repo_identity", "cfgowner/cfgrepo")
    assert deploy_repo_identity() == ("envowner", "envrepo")


def test_deploy_repo_identity_falls_back_to_runtime_config(monkeypatch):
    monkeypatch.delenv("DEPLOY_REPO_IDENTITY", raising=False)
    from app.runtime_config_state import runtime_config

    monkeypatch.setitem(runtime_config, "deploy_repo_identity", "cfgowner/cfgrepo")
    assert deploy_repo_identity() == ("cfgowner", "cfgrepo")


def test_deploy_repo_identity_none_when_unset(monkeypatch):
    monkeypatch.delenv("DEPLOY_REPO_IDENTITY", raising=False)
    from app.runtime_config_state import runtime_config

    monkeypatch.setitem(runtime_config, "deploy_repo_identity", None)
    assert deploy_repo_identity() is None


def test_project_matches_identity_case_insensitive():
    project = _Proj(github_owner="Acme", github_repo="Widget")
    assert project_matches_deploy_identity(project, ("acme", "widget")) is True


def test_project_does_not_match_different_repo():
    project = _Proj(github_owner="acme", github_repo="other")
    assert project_matches_deploy_identity(project, ("acme", "widget")) is False


def test_project_match_false_without_identity():
    project = _Proj(github_owner="acme", github_repo="widget")
    assert project_matches_deploy_identity(project, None) is False


def test_project_match_false_without_github_repo():
    project = _Proj(github_owner=None, github_repo=None, is_self_repo=True)
    # No github repo → no deploy-level match, even for is_self_repo=True.
    assert project_matches_deploy_identity(project, ("acme", "widget")) is False


def test_project_match_resolves_identity_from_env(monkeypatch):
    monkeypatch.setenv("DEPLOY_REPO_IDENTITY", "acme/widget")
    project = _Proj(github_owner="acme", github_repo="widget")
    # identity omitted → resolved internally from env.
    assert project_matches_deploy_identity(project) is True


def test_project_match_does_not_read_is_self_repo(monkeypatch):
    # is_self_repo True but the repo is NOT the deploy identity → no match.
    monkeypatch.setenv("DEPLOY_REPO_IDENTITY", "acme/widget")
    project = _Proj(github_owner="other", github_repo="proj", is_self_repo=True)
    assert project_matches_deploy_identity(project) is False


# ── project_is_deploy_repo classifier ───────────────────────────────────────
#
# The reliable "is this the repo this deployment serves?" predicate the
# non-bootstrap identity/observability read-sites route through: deploy identity
# is authoritative when stamped, else it falls back to is_self_repo so behavior
# is preserved on deployments that have not stamped their identity.


def _clear_deploy_identity(monkeypatch):
    monkeypatch.delenv("DEPLOY_REPO_IDENTITY", raising=False)
    from app.runtime_config_state import runtime_config

    monkeypatch.setitem(runtime_config, "deploy_repo_identity", None)


def test_deploy_repo_falls_back_to_is_self_repo_true(monkeypatch):
    # No deploy identity configured → fall back to the is_self_repo flag.
    _clear_deploy_identity(monkeypatch)
    project = _Proj(github_owner="acme", github_repo="widget", is_self_repo=True)
    assert project_is_deploy_repo(project) is True


def test_deploy_repo_falls_back_to_is_self_repo_false(monkeypatch):
    _clear_deploy_identity(monkeypatch)
    project = _Proj(github_owner="acme", github_repo="widget", is_self_repo=False)
    assert project_is_deploy_repo(project) is False


def test_deploy_repo_fallback_handles_project_without_flag(monkeypatch):
    # Missing attribute degrades to False, never raises.
    _clear_deploy_identity(monkeypatch)
    assert project_is_deploy_repo(object()) is False


def test_deploy_repo_uses_identity_when_configured(monkeypatch):
    monkeypatch.setenv("DEPLOY_REPO_IDENTITY", "acme/widget")
    project = _Proj(github_owner="acme", github_repo="widget", is_self_repo=False)
    # Identity matches even though is_self_repo is False → classified as deploy repo.
    assert project_is_deploy_repo(project) is True


def test_deploy_repo_identity_overrides_misset_is_self_repo(monkeypatch):
    monkeypatch.setenv("DEPLOY_REPO_IDENTITY", "acme/widget")
    # is_self_repo misset True but the repo is NOT the deploy identity → False.
    project = _Proj(github_owner="other", github_repo="proj", is_self_repo=True)
    assert project_is_deploy_repo(project) is False


def test_deploy_repo_accepts_preresolved_identity():
    project = _Proj(github_owner="acme", github_repo="widget", is_self_repo=False)
    assert project_is_deploy_repo(project, ("acme", "widget")) is True
    assert project_is_deploy_repo(project, ("acme", "other")) is False


# ── Startup lifecycle observability tests ────────────────────────────────────


def test_log_startup_deploy_identity_warns_when_unresolved(monkeypatch, caplog):
    """When deploy_repo_identity() is None the startup helper emits a WARNING."""
    monkeypatch.delenv("DEPLOY_REPO_IDENTITY", raising=False)
    from app.runtime_config_state import runtime_config

    monkeypatch.setitem(runtime_config, "deploy_repo_identity", None)

    import logging

    from app.lifecycle import _log_startup_deploy_identity

    with caplog.at_level(logging.WARNING, logger="blueprint"):
        _log_startup_deploy_identity()

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "Expected a WARNING log but none was emitted"
    assert "[STARTUP]" in warning_records[0].message
    assert "UNRESOLVED" in warning_records[0].message
    assert "deploy_repo_identity" in warning_records[0].message


def test_log_startup_deploy_identity_info_when_resolved(monkeypatch, caplog):
    """When deploy_repo_identity() returns a slug the startup helper emits an INFO."""
    monkeypatch.delenv("DEPLOY_REPO_IDENTITY", raising=False)
    from app.runtime_config_state import runtime_config

    monkeypatch.setitem(runtime_config, "deploy_repo_identity", "acme/widget")

    import logging

    from app.lifecycle import _log_startup_deploy_identity

    with caplog.at_level(logging.INFO, logger="blueprint"):
        _log_startup_deploy_identity()

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert info_records, "Expected an INFO log but none was emitted"
    assert "[STARTUP]" in info_records[0].message
    assert "acme" in info_records[0].message
    assert "widget" in info_records[0].message

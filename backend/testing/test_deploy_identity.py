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

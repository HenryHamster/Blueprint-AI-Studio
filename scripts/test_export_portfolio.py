"""Verify snapshot provenance and preservation of the source repository."""

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "export_portfolio", Path(__file__).with_name("export-portfolio.py")
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    MODULE.git(repo, "init", "--quiet", "--initial-branch=main")
    MODULE.git(repo, "config", "user.name", "Snapshot Test")
    MODULE.git(repo, "config", "user.email", "snapshot@example.invalid")
    (repo / ".gitignore").write_text("check\n.env\n")
    (repo / "README.md").write_text("# Example\n")
    (repo / "check").write_text("tracked despite ignore rule\n")
    MODULE.git(repo, "add", "--force", ".gitignore", "README.md", "check")
    MODULE.git(repo, "commit", "--quiet", "-m", "Original")
    (repo / "README.md").write_text("# Curated example\n")
    MODULE.git(repo, "commit", "--quiet", "-am", "Presentation")
    (repo / ".env").write_text("ignored local configuration\n")
    return repo


def test_export_has_one_commit_with_provenance_and_preserves_tracked_content(source, tmp_path):
    original = MODULE.git(source, "rev-parse", "HEAD")
    destination = tmp_path / "snapshot"
    result = MODULE.export_snapshot(source, destination)
    assert MODULE.git(destination, "rev-list", "--count", "HEAD") == "1"
    assert result["source_commit"] == original
    assert MODULE.git(source, "rev-parse", "HEAD") == original
    assert MODULE.git(source, "rev-list", "--count", "HEAD") == "2"
    assert MODULE.git(source, "status", "--porcelain") == ""
    assert MODULE.git(destination, "status", "--porcelain") == ""
    assert (destination / "check").read_bytes() == (source / "check").read_bytes()
    assert (destination / "README.md").read_bytes() == (source / "README.md").read_bytes()
    assert not (destination / ".env").exists()
    manifest = json.loads((destination / "docs/portfolio/SNAPSHOT.json").read_text())
    assert manifest["source_tree"] == MODULE.git(source, "rev-parse", "HEAD^{tree}")
    assert MODULE.git(destination, "remote") == ""


def test_export_ref_is_resolved_and_does_not_import_later_changes(source, tmp_path):
    destination = tmp_path / "snapshot"
    result = MODULE.export_snapshot(source, destination, "HEAD~1")
    assert result["source_commit"] == MODULE.git(source, "rev-parse", "HEAD~1")
    assert (destination / "README.md").read_text() == "# Example\n"


def test_export_refuses_dirty_source_without_creating_output(source, tmp_path):
    (source / "README.md").write_text("uncommitted work\n")
    destination = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="Commit the source"):
        MODULE.export_snapshot(source, destination)
    assert not destination.exists()


def test_export_refuses_existing_output_without_overwriting_it(source, tmp_path):
    destination = tmp_path / "snapshot"
    destination.mkdir()
    sentinel = destination / "keep"
    sentinel.write_text("other work\n")
    with pytest.raises(ValueError, match="already exists"):
        MODULE.export_snapshot(source, destination)
    assert sentinel.read_text() == "other work\n"


def test_export_accepts_git_identity_resolved_without_local_config(source, tmp_path, monkeypatch):
    MODULE.git(source, "config", "--unset", "user.name")
    MODULE.git(source, "config", "--unset", "user.email")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Resolved Author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "resolved@example.invalid")
    destination = tmp_path / "snapshot"
    MODULE.export_snapshot(source, destination)
    assert MODULE.git(destination, "log", "-1", "--format=%an") == "Resolved Author"
    assert MODULE.git(destination, "log", "-1", "--format=%ae") == "resolved@example.invalid"

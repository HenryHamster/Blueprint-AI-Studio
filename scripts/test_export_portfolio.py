"""Verify that portfolio export preserves original history and other work."""

import importlib.util
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
    MODULE.git(repo, "config", "user.name", "History Test")
    MODULE.git(repo, "config", "user.email", "history@example.invalid")
    (repo / ".gitignore").write_text("check\n.env\n")
    (repo / "README.md").write_text("# Example\n")
    (repo / "check").write_text("tracked despite ignore rule\n")
    (repo / "check").chmod(0o755)
    MODULE.git(repo, "add", "--force", ".gitignore", "README.md", "check")
    MODULE.git(repo, "commit", "--quiet", "-m", "Original")
    MODULE.git(repo, "tag", "--annotate", "v1", "-m", "First release")
    (repo / "README.md").write_text("# Curated example\n")
    MODULE.git(
        repo,
        "commit",
        "--quiet",
        "-am",
        "Presentation",
        "--author",
        "Original Contributor <contributor@example.invalid>",
    )
    (repo / ".env").write_text("ignored local configuration\n")
    return repo


def test_export_preserves_commit_ids_ancestry_authors_dates_and_tree(source, tmp_path):
    original = MODULE.git(source, "rev-parse", "HEAD")
    destination = tmp_path / "portfolio"
    result = MODULE.export_history(source, destination)
    assert result["source_commit"] == original
    assert result["source_tree"] == MODULE.git(source, "rev-parse", "HEAD^{tree}")
    assert result["commit_count"] == 2
    assert MODULE.git(destination, "rev-parse", "HEAD") == original
    assert MODULE.git(destination, "log", "--format=raw") == MODULE.git(
        source, "log", "--format=raw"
    )
    assert MODULE.git(destination, "ls-tree", "-r", "HEAD") == MODULE.git(
        source, "ls-tree", "-r", "HEAD"
    )
    assert MODULE.git(source, "rev-parse", "HEAD") == original
    assert MODULE.git(source, "status", "--porcelain") == ""
    assert MODULE.git(destination, "status", "--porcelain") == ""
    assert (destination / "check").stat().st_mode & 0o111
    assert not (destination / ".env").exists()
    assert not (destination / "docs/portfolio/SNAPSHOT.json").exists()
    assert MODULE.git(destination, "remote") == ""
    assert MODULE.git(destination, "branch", "--format=%(refname:short)") == "main"


def test_export_preserves_annotated_release_tags_but_excludes_abandoned_refs(
    source, tmp_path
):
    MODULE.git(source, "checkout", "--quiet", "-b", "abandoned-task")
    (source / "abandoned.txt").write_text("unfinished task\n")
    MODULE.git(source, "add", "abandoned.txt")
    MODULE.git(source, "commit", "--quiet", "-m", "Abandoned task")
    MODULE.git(source, "tag", "unreleased")
    MODULE.git(source, "checkout", "--quiet", "main")
    destination = tmp_path / "portfolio"
    result = MODULE.export_history(source, destination)
    assert result["release_tag_count"] == 1
    assert MODULE.git(destination, "tag", "--list") == "v1"
    assert MODULE.git(destination, "rev-parse", "v1") == MODULE.git(
        source, "rev-parse", "v1"
    )
    assert MODULE.git(destination, "branch", "--format=%(refname:short)") == "main"
    assert not (destination / "abandoned.txt").exists()
    assert "abandoned-task" in MODULE.git(source, "branch", "--list")


def test_export_preserves_merge_graph(source, tmp_path):
    MODULE.git(source, "checkout", "--quiet", "-b", "feature", "HEAD~1")
    (source / "feature.txt").write_text("implemented feature\n")
    MODULE.git(source, "add", "feature.txt")
    MODULE.git(source, "commit", "--quiet", "-m", "Feature")
    MODULE.git(source, "checkout", "--quiet", "main")
    MODULE.git(source, "merge", "--quiet", "--no-ff", "feature", "-m", "Merge feature")
    destination = tmp_path / "portfolio"
    result = MODULE.export_history(source, destination)
    assert result["commit_count"] == 4
    assert MODULE.git(destination, "rev-list", "--parents", "HEAD") == MODULE.git(
        source, "rev-list", "--parents", "HEAD"
    )
    assert (destination / "feature.txt").read_text() == "implemented feature\n"


def test_export_ref_excludes_later_changes(source, tmp_path):
    destination = tmp_path / "portfolio"
    result = MODULE.export_history(source, destination, "HEAD~1")
    assert result["source_commit"] == MODULE.git(source, "rev-parse", "HEAD~1")
    assert result["commit_count"] == 1
    assert (destination / "README.md").read_text() == "# Example\n"


def test_export_refuses_dirty_source_without_creating_output(source, tmp_path):
    (source / "README.md").write_text("uncommitted work\n")
    destination = tmp_path / "portfolio"
    with pytest.raises(ValueError, match="Commit the source"):
        MODULE.export_history(source, destination)
    assert not destination.exists()


def test_export_refuses_untracked_source_work(source, tmp_path):
    (source / "new.txt").write_text("uncommitted work\n")
    destination = tmp_path / "portfolio"
    with pytest.raises(ValueError, match="Commit the source"):
        MODULE.export_history(source, destination)
    assert not destination.exists()


def test_export_refuses_existing_output_without_overwriting_it(source, tmp_path):
    destination = tmp_path / "portfolio"
    destination.mkdir()
    sentinel = destination / "keep"
    sentinel.write_text("other work\n")
    with pytest.raises(ValueError, match="already exists"):
        MODULE.export_history(source, destination)
    assert sentinel.read_text() == "other work\n"


def test_export_does_not_require_a_new_git_author_identity(source, tmp_path):
    MODULE.git(source, "config", "--unset", "user.name")
    MODULE.git(source, "config", "--unset", "user.email")
    destination = tmp_path / "portfolio"
    MODULE.export_history(source, destination)
    assert MODULE.git(destination, "rev-parse", "HEAD") == MODULE.git(
        source, "rev-parse", "HEAD"
    )

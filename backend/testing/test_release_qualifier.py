"""Regression tests for release qualification measurements."""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.release_commit_authorship import (
    BOT_COMMIT_AUTHOR,
    count_non_bot_authored_commits,
)


def _run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(root: Path, *, author: str) -> str:
    history = root / "history.txt"
    history.write_text(
        history.read_text(encoding="utf-8") + author + "\n"
        if history.exists()
        else author + "\n",
        encoding="utf-8",
    )
    _run_git(root, "add", "history.txt")
    _run_git(
        root,
        "-c",
        f"user.name={author}",
        "-c",
        "user.email=release-test@example.invalid",
        "commit",
        "-m",
        "release history fixture",
        "--author",
        f"{author} <release-test@example.invalid>",
    )
    return _run_git(root, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _run_git(root, "init", "--quiet")
    return root


def test_only_bot_commits_report_zero(tmp_path: Path):
    root = _repo(tmp_path)
    base = _commit(root, author=BOT_COMMIT_AUTHOR)
    head = _commit(root, author=BOT_COMMIT_AUTHOR)

    report = count_non_bot_authored_commits(base, head, repo_root=root)

    assert report["state"] == "resolved"
    assert report["count"] == 0
    assert report["offenders"] == []


def test_non_bot_commit_is_reported_with_short_sha_and_author(tmp_path: Path):
    root = _repo(tmp_path)
    base = _commit(root, author=BOT_COMMIT_AUTHOR)
    human = _commit(root, author="Henry Example")

    report = count_non_bot_authored_commits(base, human, repo_root=root)

    assert report["state"] == "resolved"
    assert report["count"] == 1
    assert report["offenders"] == [{"sha": human[:12], "author": "Henry Example"}]


def test_unresolvable_range_is_not_a_false_zero(tmp_path: Path):
    root = _repo(tmp_path)
    base = _commit(root, author=BOT_COMMIT_AUTHOR)

    report = count_non_bot_authored_commits(base, "f" * 40, repo_root=root)

    assert report["state"] == "unresolved"
    assert report["count"] is None
    assert report["reason"] == "commit_not_resolved"


def test_shallow_repository_is_unresolved(tmp_path: Path):
    source = _repo(tmp_path)
    base = _commit(source, author=BOT_COMMIT_AUTHOR)
    _commit(source, author="Henry Example")
    shallow = tmp_path / "shallow"
    _run_git(
        tmp_path,
        "clone",
        "--quiet",
        "--depth=1",
        source.as_uri(),
        str(shallow),
    )
    head = _run_git(shallow, "rev-parse", "HEAD")

    report = count_non_bot_authored_commits(base, head, repo_root=shallow)

    assert report["state"] == "unresolved"
    assert report["count"] is None
    assert report["reason"] == "shallow_repository"

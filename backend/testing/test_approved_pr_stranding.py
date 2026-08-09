"""Tests for the recurring approved-PR merge-state detector."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.approved_pr_stranding import inspect_approved_pr, sweep_approved_pr_stranding
from app.models import Alert, Project, ReleaseVersion, Task, TaskStatus


def _run(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        list(args), cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _git_fixture(tmp_path: Path, *, conflict: bool = False) -> tuple[Path, str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run("git", "init", "-b", "main", cwd=repo)
    _run("git", "config", "user.email", "test@example.com", cwd=repo)
    _run("git", "config", "user.name", "Test", cwd=repo)
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    _run("git", "add", ".", cwd=repo)
    _run("git", "commit", "-m", "base", cwd=repo)
    base_sha = _run("git", "rev-parse", "HEAD", cwd=repo)

    _run("git", "checkout", "-b", "approved-work", cwd=repo)
    (repo / "shared.txt").write_text("work\n" if conflict else "base\nwork\n", encoding="utf-8")
    _run("git", "add", ".", cwd=repo)
    _run("git", "commit", "-m", "approved work", cwd=repo)
    head_sha = _run("git", "rev-parse", "HEAD", cwd=repo)

    _run("git", "checkout", "main", cwd=repo)
    (repo / "shared.txt").write_text("main\n" if conflict else "base\n", encoding="utf-8")
    (repo / "main.txt").write_text("integration\n", encoding="utf-8")
    _run("git", "add", ".", cwd=repo)
    _run("git", "commit", "-m", "integration", cwd=repo)
    integration_sha = _run("git", "rev-parse", "HEAD", cwd=repo)
    return repo, base_sha, head_sha, integration_sha


def test_inspect_flags_clean_strand_and_ignores_ancestor(tmp_path: Path):
    repo, base_sha, head_sha, integration_sha = _git_fixture(tmp_path)

    stranded = inspect_approved_pr(
        base_sha=base_sha,
        head_sha=head_sha,
        integration_branch="main",
        repo_root=str(repo),
    )
    assert stranded.state == "stranded"
    assert stranded.mergeability == "clean"
    assert stranded.integration_head_sha == integration_sha

    landed = inspect_approved_pr(
        base_sha=base_sha,
        head_sha=integration_sha,
        integration_branch="main",
        repo_root=str(repo),
    )
    assert landed.state == "landed"
    assert landed.mergeability == "not_applicable"


def test_inspect_reports_current_integration_conflict(tmp_path: Path):
    repo, base_sha, head_sha, _ = _git_fixture(tmp_path, conflict=True)
    evidence = inspect_approved_pr(
        base_sha=base_sha,
        head_sha=head_sha,
        integration_branch="main",
        repo_root=str(repo),
    )
    assert evidence.state == "stranded"
    assert evidence.mergeability == "conflicted"


def test_inspect_unknown_never_becomes_clean(tmp_path: Path):
    evidence = inspect_approved_pr(
        base_sha="base",
        head_sha="head",
        integration_branch="main",
        repo_root=str(tmp_path / "missing"),
    )
    assert evidence.state == "unknown"
    assert evidence.mergeability == "unknown"


@pytest.mark.asyncio
async def test_sweep_alerts_after_two_qualification_cycles(db_session, tmp_path: Path):
    repo, base_sha, head_sha, _ = _git_fixture(tmp_path)
    project = Project(name="Detector project", user_id="test-user", integration_branch="main")
    db_session.add(project)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        title="approved work",
        status=TaskStatus.COMPLETED,
        created_by="TEST",
        context={
            "review_loop": {
                "review_approved": True,
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 17048,
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                },
            }
        },
    )
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        active_qualification_attempt_id=uuid.uuid4(),
    )
    db_session.add(task)
    db_session.add(version)
    await db_session.commit()

    first = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))
    assert first["stranded"] == 1
    assert first["alerts_created"] == 0
    assert (task.context["approved_pr_stranding"]["qualification_cycles_seen"]) == 1

    version.active_qualification_attempt_id = uuid.uuid4()
    db_session.add(version)
    await db_session.commit()
    second = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))
    assert second["alerts_created"] == 1
    alert = (
        await db_session.execute(
            select(Alert).where(Alert.event_type == "release.approved_pr_stranded")
        )
    ).scalar_one()
    assert "PR #17048" in alert.message
    assert "merge-tree classification: clean" in alert.message

"""Tests for the recurring approved-PR merge-state detector."""

from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.approved_pr_stranding as stranding
from app.approved_pr_stranding import inspect_approved_pr, sweep_approved_pr_stranding
from app.landed_signal import has_landed_signal_via_rollup, mark_landed_on_integration
from app.models import Alert, Project, ReleaseVersion, Task, TaskStatus


def _run(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        list(args), cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _git_fixture(
    tmp_path: Path,
    *,
    conflict: bool = False,
    conflict_marker_content: bool = False,
) -> tuple[Path, str, str, str]:
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
    if conflict_marker_content:
        marker = "<" * 7
        separator = "=" * 7
        (repo / "conflict-notes.txt").write_text(
            f"{marker} HEAD\nThis documents a conflict marker.\n"
            f"{separator}\n{marker} branch\n",
            encoding="utf-8",
        )
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


def _non_integration_merge_fixture(tmp_path: Path) -> tuple[Path, str, str, str]:
    """Create an approved commit merged to research but absent from main."""
    repo, base_sha, head_sha, _ = _git_fixture(tmp_path)
    _run("git", "checkout", "-b", "research/staging", cwd=repo)
    (repo / "staging.txt").write_text("research staging\n", encoding="utf-8")
    _run("git", "add", ".", cwd=repo)
    _run("git", "commit", "-m", "research staging", cwd=repo)
    _run("git", "merge", "--no-ff", "approved-work", "-m", "merge approved work", cwd=repo)
    merge_commit_sha = _run("git", "rev-parse", "HEAD", cwd=repo)
    return repo, base_sha, head_sha, merge_commit_sha


def _squash_merge_fixture(tmp_path: Path) -> tuple[Path, str, str, str]:
    """Create a squash merge where only the new merge commit reaches main."""
    repo, base_sha, head_sha, _ = _git_fixture(tmp_path)
    _run("git", "checkout", "main", cwd=repo)
    _run("git", "merge", "--squash", "approved-work", cwd=repo)
    _run("git", "commit", "-m", "squash approved work", cwd=repo)
    merge_commit_sha = _run("git", "rev-parse", "HEAD", cwd=repo)
    return repo, base_sha, head_sha, merge_commit_sha


def _checkpoint_fixture(
    tmp_path: Path, task_id: uuid.UUID, *, valid: bool = True
) -> tuple[Path, str, str]:
    repo, base_sha, _head_sha, _integration_sha = _git_fixture(tmp_path)
    branch = f"blueprint/task-{str(task_id).replace('-', '')[:8]}"
    _run("git", "checkout", "-b", branch, cwd=repo)
    (repo / "checkpoint.txt").write_text("preserved work\n", encoding="utf-8")
    _run("git", "add", ".", cwd=repo)
    subject = (
        f"blueprint-checkpoint: preserve unshipped task {task_id}"
        if valid
        else "ordinary commit on a task branch"
    )
    body = (
        "Blueprint-Checkpoint-Version: 1\n"
        f"Blueprint-Task-Id: {task_id}\n"
        f"Blueprint-Base-Commit: {base_sha}"
        if valid
        else "not a checkpoint"
    )
    _run("git", "commit", "-m", subject, "-m", body, cwd=repo)
    checkpoint_sha = _run("git", "rev-parse", "HEAD", cwd=repo)
    _run("git", "checkout", "main", cwd=repo)
    return repo, branch, checkpoint_sha


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
    assert stranded.reachability_signal == "neither"

    landed = inspect_approved_pr(
        base_sha=base_sha,
        head_sha=integration_sha,
        integration_branch="main",
        repo_root=str(repo),
    )
    assert landed.state == "landed"
    assert landed.mergeability == "not_applicable"


def test_inspect_prefers_squash_merge_commit_over_pr_head(tmp_path: Path):
    repo, base_sha, head_sha, merge_commit_sha = _squash_merge_fixture(tmp_path)

    evidence = inspect_approved_pr(
        base_sha=base_sha,
        head_sha=head_sha,
        merge_commit_sha=merge_commit_sha,
        integration_branch="main",
        repo_root=str(repo),
    )

    assert evidence.state == "landed"
    assert evidence.reachability_signal == "merge_sha"
    assert evidence.checked_commit_sha == merge_commit_sha
    assert head_sha != merge_commit_sha


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


def test_inspect_ignores_conflict_marker_content_in_clean_diff(tmp_path: Path):
    repo, base_sha, head_sha, _ = _git_fixture(tmp_path, conflict_marker_content=True)
    evidence = inspect_approved_pr(
        base_sha=base_sha,
        head_sha=head_sha,
        integration_branch="main",
        repo_root=str(repo),
    )
    assert evidence.state == "stranded"
    assert evidence.mergeability == "clean"


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
async def test_sweep_assigns_exact_checkpoint_branch_to_failed_owner(
    db_session, tmp_path: Path
):
    task_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    repo, branch, checkpoint_sha = _checkpoint_fixture(tmp_path, task_id)
    project = Project(
        name="Checkpoint owner project",
        user_id="test-user",
        integration_branch="main",
    )
    task = Task(
        id=task_id,
        project_id=project.id,
        title="unshipped work",
        status=TaskStatus.FAILED,
        created_by="TEST",
        payloads=[{"type": "create", "prompt": "preserve"}],
        context={"branch": branch},
    )
    db_session.add_all([project, task])
    await db_session.commit()

    result = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))

    await db_session.refresh(task)
    owner = task.context["unshipped_checkpoint"]
    assert result["checkpoint_scanned"] == 1
    assert result["checkpoint_stranded"] == 1
    assert result["checkpoint_handoffs"] == 1
    assert result.get("checkpoint_alerts_created", 0) == 0
    assert task.status == TaskStatus.QUEUED
    assert owner["task_id"] == str(task_id)
    assert owner["branch"] == branch
    assert owner["checkpoint_sha"] == checkpoint_sha
    assert owner["state"] == "stranded"


@pytest.mark.asyncio
async def test_sweep_ignores_unproven_checkpoint_lookalike(
    db_session, tmp_path: Path
):
    task_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    repo, branch, _checkpoint_sha = _checkpoint_fixture(tmp_path, task_id, valid=False)
    project = Project(
        name="Malformed checkpoint project",
        user_id="test-user",
        integration_branch="main",
    )
    task = Task(
        id=task_id,
        project_id=project.id,
        title="unrelated branch",
        status=TaskStatus.FAILED,
        created_by="TEST",
        payloads=[{"type": "create", "prompt": "ignore"}],
        context={"branch": branch},
    )
    db_session.add_all([project, task])
    await db_session.commit()

    result = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))

    await db_session.refresh(task)
    assert result.get("checkpoint_scanned", 0) == 0
    assert result.get("checkpoint_handoffs", 0) == 0
    assert task.status == TaskStatus.FAILED
    assert "unshipped_checkpoint" not in task.context
    # A branch we read that carries no checkpoint contract is a durable verdict,
    # so the row must leave the oldest-first candidate window.
    assert (
        task.context["unshipped_checkpoint_examined"]["reason"]
        == "not_a_checkpoint_branch"
    )


def _checkpoint_project_and_task(
    task_id: uuid.UUID, *, branch: str, name: str
) -> tuple[Project, Task]:
    project = Project(
        name=name,
        user_id="test-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="main",
    )
    task = Task(
        id=task_id,
        project_id=project.id,
        title="unshipped work",
        status=TaskStatus.FAILED,
        created_by="TEST",
        payloads=[{"type": "create", "prompt": "preserve"}],
        context={"branch": branch, "failure_class": "AGENT_TIMEOUT"},
    )
    return project, task


def _patch_github_checkpoint(
    monkeypatch, *, branch: str, task_id: uuid.UUID, ancestor: bool | None
) -> None:
    """Wire the deployed (repo_root=None) checkpoint path onto fake GitHub."""
    monkeypatch.setattr(
        "app.release_promotion._resolve_github_with_session",
        AsyncMock(return_value=("token", "acme", "widget")),
    )
    monkeypatch.setattr(
        "app.github.list_branches",
        AsyncMock(
            return_value=[
                {"name": branch, "sha": "checkpoint-sha"},
                {"name": "blueprint/task-unrelated", "sha": "other-sha"},
            ]
        ),
    )
    monkeypatch.setattr(
        "app.github.list_recent_commit_messages",
        AsyncMock(
            return_value=[
                f"blueprint-checkpoint: preserve unshipped task {task_id}\n"
                "Blueprint-Checkpoint-Version: 1\n"
                f"Blueprint-Task-Id: {task_id}\n"
                "Blueprint-Base-Commit: abc1234"
            ]
        ),
    )
    monkeypatch.setattr(
        "app.github.get_branch_sha", AsyncMock(return_value="integration-head")
    )
    monkeypatch.setattr(
        "app.github.is_ancestor_of", AsyncMock(return_value=ancestor)
    )


@pytest.mark.asyncio
async def test_github_checkpoint_strand_hands_off_owner_without_checkout(
    db_session, monkeypatch
):
    """The deployed sweep runs with repo_root=None and no git binary."""

    def no_git_available(*args, **kwargs):
        raise AssertionError("deployed checkpoint sweep must not invoke local git")

    monkeypatch.setattr(stranding.subprocess, "run", no_git_available)
    task_id = uuid.UUID("33333333-3333-4333-8333-333333333333")
    branch = "blueprint/task-33333333"
    project, task = _checkpoint_project_and_task(
        task_id, branch=branch, name="Deployed checkpoint project"
    )
    task.vm_instance_id = "agent-vm-dead"
    task.last_heartbeat = datetime(2026, 8, 9, tzinfo=timezone.utc)
    db_session.add_all([project, task])
    await db_session.commit()
    _patch_github_checkpoint(
        monkeypatch, branch=branch, task_id=task_id, ancestor=False
    )

    counts = await stranding._sweep_checkpoint_owners(
        db_session, repo_root=None, now=datetime(2026, 8, 10)
    )

    await db_session.refresh(task)
    assert counts["checkpoint_scanned"] == 1
    assert counts["checkpoint_stranded"] == 1
    assert counts["checkpoint_handoffs"] == 1
    assert counts["checkpoint_alerts_created"] == 0
    assert task.status == TaskStatus.QUEUED
    assert task.context["unshipped_checkpoint"]["checkpoint_sha"] == "checkpoint-sha"
    assert task.context["unshipped_checkpoint"]["state"] == "stranded"
    # The requeue must be a genuine fresh attempt, not a bare status flip.
    assert task.vm_instance_id is None
    assert task.last_heartbeat is None
    assert task.started_at is None
    assert "failure_class" not in task.context
    assert "_fresh_attempt_started_at" in task.context


@pytest.mark.asyncio
async def test_github_checkpoint_unknown_never_hands_off_or_alerts(
    db_session, monkeypatch
):
    """An unavailable ancestry answer is unknown — never a strand, never clean."""
    task_id = uuid.UUID("44444444-4444-4444-8444-444444444444")
    branch = "blueprint/task-44444444"
    project, task = _checkpoint_project_and_task(
        task_id, branch=branch, name="Unknown checkpoint project"
    )
    db_session.add_all([project, task])
    await db_session.commit()
    _patch_github_checkpoint(
        monkeypatch, branch=branch, task_id=task_id, ancestor=None
    )

    counts = await stranding._sweep_checkpoint_owners(
        db_session, repo_root=None, now=datetime(2026, 8, 10)
    )

    await db_session.refresh(task)
    assert counts["checkpoint_scanned"] == 1
    assert counts["checkpoint_unknown"] == 1
    assert counts["checkpoint_stranded"] == 0
    assert counts["checkpoint_handoffs"] == 0
    assert counts["checkpoint_alerts_created"] == 0
    assert task.status == TaskStatus.FAILED
    assert task.context["unshipped_checkpoint"]["state"] == "unknown"
    # Unknown evidence must NOT close the row out of the candidate window.
    assert "unshipped_checkpoint_examined" not in task.context


@pytest.mark.asyncio
async def test_checkpoint_handoff_skips_active_idempotency_key_collision(
    db_session, monkeypatch
):
    """A live clone already holds the key — requeueing would abort the commit."""
    task_id = uuid.UUID("55555555-5555-4555-8555-555555555555")
    branch = "blueprint/task-55555555"
    project, task = _checkpoint_project_and_task(
        task_id, branch=branch, name="Colliding checkpoint project"
    )
    task.idempotency_key = "shared-key"
    replacement = Task(
        project_id=project.id,
        title="live replacement",
        status=TaskStatus.QUEUED,
        created_by="TEST",
        payloads=[{"type": "create", "prompt": "preserve"}],
        idempotency_key="shared-key",
    )
    db_session.add_all([project, task, replacement])
    await db_session.commit()
    _patch_github_checkpoint(
        monkeypatch, branch=branch, task_id=task_id, ancestor=False
    )

    counts = await stranding._sweep_checkpoint_owners(
        db_session, repo_root=None, now=datetime(2026, 8, 10)
    )

    await db_session.refresh(task)
    assert counts["checkpoint_stranded"] == 1
    assert counts["checkpoint_handoffs"] == 0
    assert counts["checkpoint_alerts_created"] == 1
    assert task.status == TaskStatus.FAILED
    assert task.context["unshipped_checkpoint_examined"]["reason"] == (
        "operator_attention"
    )


@pytest.mark.asyncio
async def test_checkpoint_sweep_skips_parents_and_abandoned_work(
    db_session, monkeypatch
):
    """Parents re-evaluate through their own path; closed PRs are abandoned."""
    parent_id = uuid.UUID("66666666-6666-4666-8666-666666666666")
    abandoned_id = uuid.UUID("77777777-7777-4777-8777-777777777777")
    project, parent = _checkpoint_project_and_task(
        parent_id, branch="blueprint/task-66666666", name="Parent checkpoint project"
    )
    parent.payloads = [{"type": "research", "prompt": "plan"}]
    child = Task(
        project_id=project.id,
        parent_task_id=parent_id,
        title="child",
        status=TaskStatus.COMPLETED,
        created_by="TEST",
        payloads=[{"type": "create", "prompt": "work"}],
    )
    abandoned = Task(
        id=abandoned_id,
        project_id=project.id,
        title="abandoned",
        status=TaskStatus.FAILED,
        created_by="TEST",
        payloads=[{"type": "create", "prompt": "work"}],
        context={"branch": "blueprint/task-77777777", "pr_closed_unmerged": True},
    )
    db_session.add_all([project, parent, child, abandoned])
    await db_session.commit()
    list_branches = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "app.release_promotion._resolve_github_with_session",
        AsyncMock(return_value=("token", "acme", "widget")),
    )
    monkeypatch.setattr("app.github.list_branches", list_branches)

    counts = await stranding._sweep_checkpoint_owners(
        db_session, repo_root=None, now=datetime(2026, 8, 10)
    )

    await db_session.refresh(parent)
    await db_session.refresh(abandoned)
    assert counts["checkpoint_scanned"] == 0
    assert counts["checkpoint_handoffs"] == 0
    # The abandoned row is excluded by the candidate predicate itself, so the
    # sweep never even resolves a project for it.
    assert list_branches.await_count == 0
    assert "unshipped_checkpoint_examined" not in abandoned.context
    assert parent.context["unshipped_checkpoint_examined"]["reason"] == (
        "parent_with_children"
    )


@pytest.mark.asyncio
async def test_examined_rows_leave_the_candidate_window(db_session, monkeypatch):
    """A scanned row without a checkpoint must not pin the bounded scan."""
    task_id = uuid.UUID("88888888-8888-4888-8888-888888888888")
    branch = "blueprint/task-88888888"
    project, task = _checkpoint_project_and_task(
        task_id, branch=branch, name="Starvation checkpoint project"
    )
    db_session.add_all([project, task])
    await db_session.commit()
    monkeypatch.setattr(
        "app.release_promotion._resolve_github_with_session",
        AsyncMock(return_value=("token", "acme", "widget")),
    )
    monkeypatch.setattr(
        "app.github.list_branches",
        AsyncMock(return_value=[{"name": "blueprint/task-other", "sha": "sha"}]),
    )

    first = await stranding._sweep_checkpoint_owners(
        db_session, repo_root=None, now=datetime(2026, 8, 10)
    )
    await db_session.refresh(task)
    assert first["checkpoint_scanned"] == 0
    assert task.context["unshipped_checkpoint_examined"]["reason"] == (
        "no_checkpoint_branch"
    )

    second_listing = AsyncMock(
        return_value=[{"name": branch, "sha": "checkpoint-sha"}]
    )
    monkeypatch.setattr("app.github.list_branches", second_listing)
    second = await stranding._sweep_checkpoint_owners(
        db_session, repo_root=None, now=datetime(2026, 8, 10)
    )
    assert second["checkpoint_scanned"] == 0
    assert second_listing.await_count == 0


@pytest.mark.asyncio
async def test_incomplete_branch_listing_never_marks_a_row_examined(
    db_session, monkeypatch
):
    """An empty listing is an API failure, not proof that no branch exists."""
    task_id = uuid.UUID("99999999-9999-4999-8999-999999999999")
    project, task = _checkpoint_project_and_task(
        task_id,
        branch="blueprint/task-99999999",
        name="Unavailable listing project",
    )
    db_session.add_all([project, task])
    await db_session.commit()
    monkeypatch.setattr(
        "app.release_promotion._resolve_github_with_session",
        AsyncMock(return_value=("token", "acme", "widget")),
    )
    monkeypatch.setattr("app.github.list_branches", AsyncMock(return_value=[]))

    counts = await stranding._sweep_checkpoint_owners(
        db_session, repo_root=None, now=datetime(2026, 8, 10)
    )

    await db_session.refresh(task)
    assert counts["checkpoint_scanned"] == 0
    assert "unshipped_checkpoint_examined" not in task.context


@pytest.mark.asyncio
async def test_sweep_recognizes_child_subsumed_by_landed_rollup(
    db_session, tmp_path: Path, monkeypatch
):
    """A landed rollup carries a child's content even when its PR tip is absent."""
    repo, base_sha, head_sha, _ = _git_fixture(tmp_path)
    project = Project(
        name="Rollup-subsumed child", user_id="test-user", integration_branch="main"
    )
    db_session.add(project)
    await db_session.flush()

    parent = Task(
        project_id=project.id,
        title="landed rollup",
        status=TaskStatus.COMPLETED,
        created_by="TEST",
        context={"_is_rollup_review": True},
    )
    mark_landed_on_integration(parent)
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="subsumed child",
        status=TaskStatus.COMPLETED,
        created_by="TEST",
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
            "review_loop": {
                "review_approved": True,
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 18001,
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                },
            },
        },
    )
    db_session.add_all([parent, child])
    await db_session.commit()

    def fail_if_pr_ancestry_is_used(*args, **kwargs):
        raise AssertionError("rollup-subsumed child should use the landed signal")

    monkeypatch.setattr(stranding, "inspect_approved_pr", fail_if_pr_ancestry_is_used)

    assert await has_landed_signal_via_rollup(child, db_session) is True
    result = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))

    assert result["stranded"] == 0
    assert result["rollup_subsumed"] == 1
    assert child.context["approved_pr_stranding"]["state"] == "landed"
    assert child.context["approved_pr_stranding"]["rollup_subsumed"] is True


@pytest.mark.asyncio
async def test_closed_child_without_rollup_subsumption_remains_stranded(
    db_session, tmp_path: Path
):
    """A closed child with no merge-equivalent signal is still actionable."""
    repo, base_sha, head_sha, _ = _git_fixture(tmp_path)
    project = Project(
        name="Closed unsubsumed child", user_id="test-user", integration_branch="main"
    )
    db_session.add(project)
    await db_session.flush()

    parent = Task(
        project_id=project.id,
        title="landed rollup",
        status=TaskStatus.COMPLETED,
        created_by="TEST",
        context={"_is_rollup_review": True},
    )
    mark_landed_on_integration(parent)
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="closed child",
        status=TaskStatus.CANCELLED,
        created_by="TEST",
        context={
            "review_loop": {
                "review_approved": True,
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 18002,
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                },
            }
        },
    )
    db_session.add_all([parent, child])
    await db_session.commit()

    assert await has_landed_signal_via_rollup(child, db_session) is False
    result = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))

    assert result["stranded"] == 1
    assert child.context["approved_pr_stranding"]["state"] == "stranded"


@pytest.mark.asyncio
async def test_sweep_alerts_after_one_qualification_cycle(db_session, tmp_path: Path):
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
    assert first["alerts_created"] == 1
    assert (task.context["approved_pr_stranding"]["qualification_cycles_seen"]) == 1

    version.active_qualification_attempt_id = uuid.uuid4()
    db_session.add(version)
    await db_session.commit()
    second = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))
    assert second["alerts_created"] == 0
    alert = (
        await db_session.execute(
            select(Alert).where(Alert.event_type == "release.approved_pr_stranded")
        )
    ).scalar_one()
    assert "PR #17048" in alert.message
    assert "Mergeability classification: clean" in alert.message


@pytest.mark.asyncio
async def test_clean_strand_triggers_bounded_reland_attempt(db_session, tmp_path: Path):
    """A clean terminal approval is restored once to the canonical CI queue."""
    repo, base_sha, head_sha, _ = _git_fixture(tmp_path)
    project = Project(
        name="Re-land detector project", user_id="test-user", integration_branch="main"
    )
    db_session.add(project)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        title="clean stranded approval",
        status=TaskStatus.COMPLETED,
        created_by="TEST",
        context={
            "review_loop": {
                "review_approved": True,
                "ci_passed": True,
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 17161,
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                },
            }
        },
    )
    db_session.add(task)
    await db_session.commit()

    first = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))

    assert first["relands_started"] == 1
    assert task.status == TaskStatus.EVALUATING
    review_loop = task.context["review_loop"]
    assert review_loop["ci_waiting"] is True
    assert review_loop["ci_passed"] is False
    receipt = task.context["approved_pr_reland"]
    assert receipt["state"] == "in_flight"
    assert receipt["attempt_count"] == 1
    assert receipt["target"] == {
        "project_id": str(project.id),
        "pr_number": 17161,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }

    second = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))

    assert second["relands_started"] == 0
    assert task.status == TaskStatus.EVALUATING
    assert task.context["approved_pr_reland"]["attempt_count"] == 1


@pytest.mark.asyncio
async def test_reland_defers_when_review_target_changes_after_evidence(
    db_session, tmp_path: Path, monkeypatch
):
    """Clean evidence for one immutable target cannot revive its replacement."""
    repo, base_sha, head_sha, _ = _git_fixture(tmp_path)
    project = Project(name="Re-land target race", user_id="test-user", integration_branch="main")
    db_session.add(project)
    await db_session.flush()
    target = {
        "project_id": str(project.id),
        "pr_number": 17164,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    task = Task(
        project_id=project.id,
        title="target changed after evidence",
        status=TaskStatus.COMPLETED,
        created_by="TEST",
        context={"review_loop": {"review_approved": True, "review_target": target}},
    )
    db_session.add(task)
    await db_session.commit()

    original_observation_for = stranding._observation_for

    def change_target_after_observation(candidate, evidence, *, cycle_id, now):
        result = original_observation_for(candidate, evidence, cycle_id=cycle_id, now=now)
        changed_context = dict(candidate.task.context)
        changed_loop = dict(changed_context["review_loop"])
        changed_loop["review_target"] = {**target, "head_sha": "replacement-head"}
        changed_context["review_loop"] = changed_loop
        candidate.task.context = changed_context
        db_session.add(candidate.task)
        return result

    monkeypatch.setattr(stranding, "_observation_for", change_target_after_observation)

    result = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))

    assert result["relands_started"] == 0
    assert task.status == TaskStatus.COMPLETED
    assert task.context["review_loop"]["review_target"]["head_sha"] == "replacement-head"
    assert "approved_pr_reland" not in task.context


@pytest.mark.asyncio
async def test_approval_gated_replaced_target_is_durably_superseded_not_relanded(
    db_session, tmp_path: Path
):
    """A handoff never follows a changed immutable review target."""
    repo, base_sha, head_sha, _ = _git_fixture(tmp_path)
    project = Project(name="Superseded re-land", user_id="test-user", integration_branch="main")
    db_session.add(project)
    await db_session.flush()
    original_target = {
        "project_id": str(project.id),
        "pr_number": 17162,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    task = Task(
        project_id=project.id,
        title="replaced approved target",
        status=TaskStatus.EVALUATING,
        created_by="TEST",
        requires_approval=True,
        context={
            "review_loop": {"review_approved": True, "review_target": original_target},
            "approved_pr_reland": {
                "identity": "original-target",
                "target": original_target,
                "state": "in_flight",
                "attempt_count": 1,
            },
        },
    )
    db_session.add(task)
    await db_session.commit()

    changed_context = dict(task.context)
    changed_loop = dict(changed_context["review_loop"])
    changed_loop["review_target"] = {**original_target, "head_sha": "replacement-head"}
    changed_context["review_loop"] = changed_loop
    task.context = changed_context
    db_session.add(task)
    await db_session.commit()

    result = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))

    assert result["superseded"] == 1
    assert result["relands_started"] == 0
    assert task.status == TaskStatus.EVALUATING
    assert task.context["approved_pr_reland"]["state"] == "superseded"
    assert "replaced" in task.context["approved_pr_reland"]["reason"]

    repeated = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))

    assert repeated["superseded"] == 0
    assert task.context["approved_pr_reland"]["state"] == "superseded"


@pytest.mark.asyncio
async def test_malformed_replacement_never_classifies_a_reland_receipt_superseded(
    db_session, tmp_path: Path
):
    """A missing immutable replacement remains actionable rather than resolved."""
    repo, base_sha, head_sha, _ = _git_fixture(tmp_path)
    project = Project(name="Malformed replacement", user_id="test-user", integration_branch="main")
    db_session.add(project)
    await db_session.flush()
    target = {
        "project_id": str(project.id),
        "pr_number": 17165,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    task = Task(
        project_id=project.id,
        title="malformed replacement",
        status=TaskStatus.EVALUATING,
        created_by="TEST",
        context={
            "review_loop": {"review_approved": True, "review_target": {"pr_number": 17165}},
            "approved_pr_reland": {
                "identity": "original-target",
                "target": target,
                "state": "in_flight",
                "attempt_count": 1,
            },
        },
    )
    db_session.add(task)
    await db_session.commit()

    result = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))

    assert result["superseded"] == 0
    assert task.context["approved_pr_reland"]["state"] == "in_flight"


@pytest.mark.asyncio
async def test_reland_clears_stale_fix_latches_but_defers_live_child_work(
    db_session, tmp_path: Path
):
    """The monitor receives a reachable handoff, never one it must skip."""
    repo, base_sha, head_sha, _ = _git_fixture(tmp_path)
    project = Project(name="Re-land retry latches", user_id="test-user", integration_branch="main")
    db_session.add(project)
    await db_session.flush()
    target = {
        "project_id": str(project.id),
        "pr_number": 17166,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    stale_latches = {
        "conflict_fix_spawned": str(uuid.uuid4()),
        "ci_fix_spawned": str(uuid.uuid4()),
        "merge_rule_blocked": True,
    }
    task = Task(
        project_id=project.id,
        title="stale retry latches",
        status=TaskStatus.FAILED,
        created_by="TEST",
        context={"review_loop": {"review_approved": True, "review_target": target, **stale_latches}},
    )
    db_session.add(task)
    await db_session.commit()

    first = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))

    assert first["relands_started"] == 1
    review_loop = task.context["review_loop"]
    assert not set(stale_latches).intersection(review_loop)

    active_target = {**target, "pr_number": 17167}
    active_parent = Task(
        project_id=project.id,
        title="active conflict fix",
        status=TaskStatus.COMPLETED,
        created_by="TEST",
        context={"review_loop": {"review_approved": True, "review_target": active_target}},
    )
    db_session.add(active_parent)
    await db_session.flush()
    active_child = Task(
        project_id=project.id,
        parent_task_id=active_parent.id,
        title="live conflict fix",
        status=TaskStatus.IN_PROGRESS,
        created_by="TEST",
    )
    active_parent.context = {
        "review_loop": {
            "review_approved": True,
            "review_target": active_target,
            "conflict_fix_spawned": str(active_child.id),
        }
    }
    db_session.add_all([active_parent, active_child])
    await db_session.commit()

    second = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))

    assert second["relands_started"] == 0
    assert active_parent.status == TaskStatus.COMPLETED
    assert "approved_pr_reland" not in active_parent.context
    assert active_parent.context["review_loop"]["conflict_fix_spawned"] == str(active_child.id)


@pytest.mark.asyncio
async def test_reland_defers_terminal_repository_rule_approval_gate(
    db_session, tmp_path: Path
):
    """A repository-rule approval gate remains operator-owned, not re-driven."""
    repo, base_sha, head_sha, _ = _git_fixture(tmp_path)
    project = Project(name="Re-land rule approval", user_id="test-user", integration_branch="main")
    db_session.add(project)
    await db_session.flush()
    target = {
        "project_id": str(project.id),
        "pr_number": 17168,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    task = Task(
        project_id=project.id,
        title="repository-rule approval gate",
        status=TaskStatus.FAILED,
        created_by="TEST",
        requires_approval=True,
        context={
            "review_loop": {
                "review_approved": True,
                "review_target": target,
                "merge_rule_blocked": True,
            }
        },
    )
    db_session.add(task)
    await db_session.commit()

    result = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))

    assert result["relands_started"] == 0
    assert task.status == TaskStatus.FAILED
    assert task.requires_approval is True
    assert task.context["review_loop"]["merge_rule_blocked"] is True
    assert "approved_pr_reland" not in task.context


@pytest.mark.asyncio
async def test_unavailable_evidence_never_handoffs_a_terminal_approval(db_session, tmp_path: Path):
    """Missing ancestry evidence cannot revive a task or imply a resolution."""
    project = Project(name="Unknown re-land", user_id="test-user", integration_branch="main")
    db_session.add(project)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        title="unknown approval",
        status=TaskStatus.COMPLETED,
        created_by="TEST",
        context={
            "review_loop": {
                "review_approved": True,
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 17163,
                    "base_sha": "base-sha",
                    "head_sha": "head-sha",
                },
            }
        },
    )
    db_session.add(task)
    await db_session.commit()

    result = await sweep_approved_pr_stranding(
        db_session, repo_root=str(tmp_path / "no-checkout")
    )

    assert result["unknown"] == 1
    assert result["relands_started"] == 0
    assert result["superseded"] == 0
    assert task.status == TaskStatus.COMPLETED
    assert "approved_pr_reland" not in task.context


@pytest.mark.asyncio
async def test_stranded_verdict_without_local_checkout(db_session, monkeypatch):
    """The deployed image uses GitHub ancestry without a repo or git binary."""
    monkeypatch.delenv("BLUEPRINT_REPO_ROOT", raising=False)

    def no_git_available(*args, **kwargs):
        raise AssertionError("deployed detector must not invoke local git")

    monkeypatch.setattr(stranding.subprocess, "run", no_git_available)
    monkeypatch.setattr(
        "app.release_promotion._resolve_github_with_session",
        AsyncMock(return_value=("token", "acme", "widget")),
    )
    monkeypatch.setattr(
        "app.github.get_pull_request",
        AsyncMock(return_value={"mergeable": True}),
    )
    monkeypatch.setattr(
        "app.github.get_branch_sha",
        AsyncMock(return_value="integration-head"),
    )
    monkeypatch.setattr(
        "app.github.is_ancestor_of",
        AsyncMock(return_value=False),
    )

    project = Project(
        name="Deployed detector project",
        user_id="test-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="main",
    )
    db_session.add(project)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        title="approved work without checkout",
        status=TaskStatus.COMPLETED,
        created_by="TEST",
        context={
            "review_loop": {
                "review_approved": True,
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 17160,
                    "base_sha": "base-sha",
                    "head_sha": "reviewed-head",
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
    db_session.add_all([task, version])
    await db_session.commit()

    first = await sweep_approved_pr_stranding(
        db_session, now=datetime(2026, 8, 9, tzinfo=timezone.utc)
    )
    assert first["stranded"] == 1
    assert first["alerts_created"] == 1
    assert task.context["approved_pr_stranding"]["state"] == "stranded"

    version.active_qualification_attempt_id = uuid.uuid4()
    db_session.add(version)
    await db_session.commit()
    second = await sweep_approved_pr_stranding(
        db_session, now=datetime(2026, 8, 9, 0, 15, tzinfo=timezone.utc)
    )
    assert second["stranded"] == 1
    assert second["alerts_created"] == 0
    alert = (
        await db_session.execute(
            select(Alert).where(
                Alert.event_type == "release.approved_pr_stranded",
                Alert.project_id == project.id,
            )
        )
    ).scalar_one()
    assert alert.task_id == task.id
    assert "PR #17160" in alert.message
    assert "integration-head" in alert.message


@pytest.mark.asyncio
async def test_sweep_normalizes_aware_cadence_timestamp(db_session):
    stranding._last_stranding_scan = datetime(2026, 8, 9, tzinfo=timezone.utc)

    result = await sweep_approved_pr_stranding(
        db_session, now=datetime(2026, 8, 10, tzinfo=timezone.utc)
    )

    assert result == {
        "scanned": 0,
        "stranded": 0,
        "unknown": 0,
        "rollup_subsumed": 0,
        "alerts_created": 0,
        "relands_started": 0,
        "superseded": 0,
    }


@pytest.mark.asyncio
async def test_sweep_detects_strand_on_non_integration_branch(
    db_session, tmp_path: Path
):
    repo, base_sha, head_sha, merge_commit_sha = _non_integration_merge_fixture(tmp_path)
    project = Project(
        name="Non-integration strand project",
        user_id="test-user",
        integration_branch="main",
    )
    db_session.add(project)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        title="research branch merge",
        status=TaskStatus.COMPLETED,
        archived=True,
        created_by="TEST",
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "merge_commit_sha": merge_commit_sha,
            "review_loop": {
                "review_approved": False,
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 17141,
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                },
            },
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
    assert first["alerts_created"] == 1
    assert task.context["approved_pr_stranding"]["checked_commit_sha"] == merge_commit_sha
    assert task.context["approved_pr_stranding"]["reachability_signal"] == "neither"

    version.active_qualification_attempt_id = uuid.uuid4()
    db_session.add(version)
    await db_session.commit()
    second = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))
    assert second["alerts_created"] == 0
    alert = (
        await db_session.execute(
            select(Alert).where(
                Alert.event_type == "release.approved_pr_stranded",
                Alert.project_id == project.id,
            )
        )
    ).scalar_one()
    assert "PR #17141" in alert.message
    assert merge_commit_sha in alert.message
    assert "Reachability signal: neither" in alert.message


@pytest.mark.asyncio
async def test_sweep_alerts_post_promotion_without_open_version(
    db_session, tmp_path: Path
):
    repo, base_sha, head_sha, _ = _git_fixture(tmp_path)
    project = Project(
        name="Post-promotion detector project",
        user_id="test-user",
        integration_branch="main",
    )
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
                    "pr_number": 17049,
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                },
            }
        },
    )
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="promoted",
        last_qualification={"qualification_attempt_id": str(uuid.uuid4())},
    )
    db_session.add(task)
    db_session.add(version)
    await db_session.commit()

    first = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))
    assert first["stranded"] == 1
    assert first["alerts_created"] == 1
    assert task.context["approved_pr_stranding"]["qualification_cycles_seen"] == 1

    version.last_qualification = {"qualification_attempt_id": str(uuid.uuid4())}
    db_session.add(version)
    await db_session.commit()
    second = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))
    assert second["alerts_created"] == 0
    alert = (
        await db_session.execute(
            select(Alert).where(
                Alert.event_type == "release.approved_pr_stranded",
                Alert.project_id == project.id,
            )
        )
    ).scalar_one()
    assert "PR #17049" in alert.message

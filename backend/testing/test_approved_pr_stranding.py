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
    assert first["alerts_created"] == 0
    assert task.context["approved_pr_stranding"]["state"] == "stranded"

    version.active_qualification_attempt_id = uuid.uuid4()
    db_session.add(version)
    await db_session.commit()
    second = await sweep_approved_pr_stranding(
        db_session, now=datetime(2026, 8, 9, 0, 15, tzinfo=timezone.utc)
    )
    assert second["stranded"] == 1
    assert second["alerts_created"] == 1
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
    assert first["alerts_created"] == 0
    assert task.context["approved_pr_stranding"]["checked_commit_sha"] == merge_commit_sha
    assert task.context["approved_pr_stranding"]["reachability_signal"] == "neither"

    version.active_qualification_attempt_id = uuid.uuid4()
    db_session.add(version)
    await db_session.commit()
    second = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))
    assert second["alerts_created"] == 1
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
    assert first["alerts_created"] == 0
    assert task.context["approved_pr_stranding"]["qualification_cycles_seen"] == 1

    version.last_qualification = {"qualification_attempt_id": str(uuid.uuid4())}
    db_session.add(version)
    await db_session.commit()
    second = await sweep_approved_pr_stranding(db_session, repo_root=str(repo))
    assert second["alerts_created"] == 1
    alert = (
        await db_session.execute(
            select(Alert).where(
                Alert.event_type == "release.approved_pr_stranded",
                Alert.project_id == project.id,
            )
        )
    ).scalar_one()
    assert "PR #17049" in alert.message

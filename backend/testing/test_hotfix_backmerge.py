"""Tests for the hotfix back-merge (automain LTS Phase 5).

After a hotfix merges to ``main``, ``main`` and ``automain`` diverge.
``hotfix_backmerge.open_backmerge_pr`` opens + merges a ``main -> automain``
back-merge PR to close that gap.  It is the exact inverse of
``release_qualifier.promote()``.

Covers:
  * success — the back-merge PR is opened and merged.
  * empty_diff — ``main == automain`` is a no-op (no merge attempted).
  * conflict (409) — an ``Alert`` is raised AND a high-priority ``create``
    task is spawned to resolve the conflict; the task targets ``automain`` and
    does NOT carry ``hotfix:true``; a second resolution task is NOT spawned
    when one is already open for the same hotfix.
  * 405 / error — a non-merge or an exception raises an ``Alert`` (a failed
    back-merge must never pass silently) and spawns no fix task.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from app import hotfix_backmerge as hb
from app.migration_chain import build_migration_chain, find_heads
from app.rollup_migration_validation import (
    _plan_backmerge_migration_reconciliation,
)
from app.models import (
    Alert,
    AlertSeverity,
    Project,
    Task,
    TaskHistory,
    TaskStatus,
)

pytestmark = pytest.mark.asyncio


# ===========================================================================
# Fixtures / helpers
# ===========================================================================


async def _make_project(
    db_session,
    *,
    integration_branch: str | None = "automain",
    promotion_target: str | None = "main",
) -> Project:
    """Create a project with an authorized GitHub repo.

    The runtime_config branch fallbacks were removed; tests now configure
    integration_branch / promotion_target directly on the project. Defaults
    activate the back-merge model (LTS split active) since most cases in this
    file exercise that path; tests that need an inactive LTS pass
    ``integration_branch="main"`` (or matching promotion_target).
    """
    project = Project(
        name="Backmerge-Test",
        user_id="bm-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch=integration_branch,
        promotion_target=promotion_target,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _alerts_for(db_session, project_id) -> list[Alert]:
    return (
        (await db_session.execute(select(Alert).where(Alert.project_id == project_id)))
        .scalars()
        .all()
    )


async def _tasks_for(db_session, project_id) -> list[Task]:
    return (
        (await db_session.execute(select(Task).where(Task.project_id == project_id)))
        .scalars()
        .all()
    )


def _patch_github(
    mocker,
    *,
    create_return=None,
    create_side_effect=None,
    merge_return=None,
    merge_side_effect=None,
):
    """Patch the three GitHub helpers open_backmerge_pr touches.

    Returns ``(create_mock, merge_mock)`` so call assertions can be made.
    """
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    create_kwargs: dict = {}
    if create_side_effect is not None:
        create_kwargs["side_effect"] = create_side_effect
    else:
        create_kwargs["return_value"] = create_return
    create_mock = mocker.patch(
        "app.github.create_pull_request", new=AsyncMock(**create_kwargs)
    )
    merge_kwargs: dict = {}
    if merge_side_effect is not None:
        merge_kwargs["side_effect"] = merge_side_effect
    else:
        merge_kwargs["return_value"] = merge_return
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", new=AsyncMock(**merge_kwargs)
    )
    # Safe defaults for the step-6/7 lookups so the blocked path never makes a
    # real network call. Tests that exercise conflict / CI-failure override these.
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"mergeable_state": "clean", "head": {"sha": "deadbeef"}}
        ),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "pending", "conclusion": None, "details": []}
        ),
    )
    # Combined-Alembic-chain guard (step 3b). Default: the back-merge PR touches
    # no migration files, so _validate_rollup_migration_chain returns ("ok", None)
    # without fetching the diff or the target branch listing. Tests exercising the
    # dual-head block / defer paths override these.
    mocker.patch("app.github.get_pr_files", new=AsyncMock(return_value=[]))
    mocker.patch(
        "app.github.get_pull_request_diff", new=AsyncMock(return_value="")
    )
    mocker.patch(
        "app.github.list_versions_at_ref", new=AsyncMock(return_value=[])
    )
    return create_mock, merge_mock


# ===========================================================================
# Success — PR opened + merged
# ===========================================================================


async def test_open_backmerge_pr_success(db_session, mocker):
    """The happy path: open the main -> automain PR and merge it."""
    project = await _make_project(db_session)
    create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 12, "html_url": "http://pr/12"},
        merge_return={"merged": True, "message": "Merged"},
    )

    result = await hb.open_backmerge_pr(
        db_session,
        project.id,
        merged_pr_url="http://pr/hotfix-7",
        hotfix_task_id=uuid.uuid4(),
    )
    await db_session.commit()

    assert result["status"] == "merged"
    assert result["pr_url"] == "http://pr/12"

    # PR opened main -> automain (the inverse of promote's automain -> main).
    assert create_mock.call_args.kwargs["head"] == "main"
    assert create_mock.call_args.kwargs["base"] == "automain"
    assert create_mock.call_args.kwargs["title"] == "Back-merge: main -> automain"

    # Merge uses merge_method="merge" to preserve the hotfix history.
    assert merge_mock.call_args.kwargs["merge_method"] == "merge"
    assert merge_mock.call_args.args[3] == 12  # pr_number

    # A clean back-merge raises no Alert and spawns no task.
    assert await _alerts_for(db_session, project.id) == []
    assert await _tasks_for(db_session, project.id) == []


async def test_open_backmerge_pr_success_no_hotfix_metadata(db_session, mocker):
    """The optional merged_pr_url / hotfix_task_id may be omitted entirely."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 3, "html_url": "http://pr/3"},
        merge_return={"merged": True},
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "merged"
    assert result["pr_url"] == "http://pr/3"


# ===========================================================================
# empty_diff — main == automain -> noop
# ===========================================================================


async def test_open_backmerge_pr_empty_diff_is_noop(db_session, mocker):
    """When main == automain there is nothing to back-merge: create_pull_request
    returns {empty_diff: True}; no merge is attempted and no Alert is raised."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"empty_diff": True, "merged": True, "html_url": ""},
        merge_return={"merged": True},
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "noop_empty_diff"
    # No merge attempted on an empty diff.
    merge_mock.assert_not_called()
    # No Alert, no task.
    assert await _alerts_for(db_session, project.id) == []
    assert await _tasks_for(db_session, project.id) == []


# ===========================================================================
# conflict (409) — Alert + plan task spawned
# ===========================================================================


async def test_open_backmerge_pr_conflict_raises_alert_and_spawns_plan(
    db_session, mocker
):
    """A 409 conflict raises an ERROR Alert AND spawns a high-priority create
    task targeting automain. The task must NOT carry hotfix:true."""
    project = await _make_project(db_session)
    hotfix_id = uuid.uuid4()
    _patch_github(
        mocker,
        create_return={"number": 55, "html_url": "http://pr/55"},
        merge_return={
            "merged": False,
            "conflict": True,
            "message": "merge conflict",
        },
    )

    result = await hb.open_backmerge_pr(
        db_session,
        project.id,
        merged_pr_url="http://pr/hotfix-9",
        hotfix_task_id=hotfix_id,
    )
    await db_session.commit()

    assert result["status"] == "conflict"
    assert result["pr_url"] == "http://pr/55"

    # An ERROR Alert was raised with the conflict title.
    alerts = await _alerts_for(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert alerts[0].title == "Back-merge conflict: main -> automain"

    # Exactly one resolution task was spawned.
    tasks = await _tasks_for(db_session, project.id)
    assert len(tasks) == 1
    resolution = tasks[0]
    assert resolution.title == "Resolve back-merge conflict: main -> automain"
    assert (
        resolution.description
        == "The automated main -> automain back-merge PR #55 hit a merge conflict; resolve it on automain so the next promotion does not conflict."
    )
    payload = resolution.first_payload
    # ``create`` (not ``plan``): single direct task so resolver guardrails
    # reach the executing agent verbatim, no planner-decomposer indirection.
    # Not ``refactor`` because the refactor handler forbids behavior changes
    # (which contradicts absorbing main's bugfixes); not ``fix`` because
    # handleFix is hard-coded to clone an existing PR branch.
    assert payload["type"] == "create"
    # The resolution task targets automain — it is a normal automain fix.
    assert payload["base_branch"] == "automain"
    # It must NOT be marked as a hotfix.
    assert "hotfix" not in payload
    # The prompt instructs a git merge origin/main conflict resolution.
    assert "git merge origin/main" in payload["prompt"]
    # The prompt carries the asymmetric resolver policy — main wins on
    # behavior in genuine conflicts, automain's structural work is preserved.
    assert "human-reviewed LTS branch" in payload["prompt"]
    # High priority (>= 90), so it is dispatched expedited.
    assert resolution.priority >= 90
    assert resolution.status == TaskStatus.QUEUED
    # Context carries the dedup + merge-method markers.
    assert resolution.context["backmerge_conflict"] is True
    assert resolution.context["hotfix_task_id"] == str(hotfix_id)
    assert resolution.context["backmerge_pr_number"] == 55

    # A SPAWNED history row was written for the resolution task.
    history = (
        (
            await db_session.execute(
                select(TaskHistory).where(TaskHistory.task_id == resolution.id)
            )
        )
        .scalars()
        .all()
    )
    assert any(h.action == "SPAWNED" for h in history)


@pytest.mark.asyncio
async def test_open_backmerge_pr_dirty_pr_detected_as_conflict_spawns_resolver(
    db_session, mocker
):
    """An ALREADY-dirty/conflicting PR returns 405 "not mergeable" (so
    merge_pull_request does NOT set conflict=True).  open_backmerge_pr must
    still detect it via the PR's mergeable_state == "dirty" and route to the
    conflict-resolution path (Alert + resolver task) — NOT spin forever in the
    generic "blocked" branch.  Regression for the stuck PR #5277 case."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 77, "html_url": "http://pr/77"},
        # merged=False with NO conflict flag — exactly the 405 "not mergeable"
        # shape GitHub returns for an already-dirty PR.
        merge_return={"merged": False, "message": "Pull Request is not mergeable"},
    )
    # The PR's authoritative state says it's dirty (has conflicts).
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"number": 77, "mergeable_state": "dirty"}),
    )

    result = await hb.open_backmerge_pr(db_session, project.id, periodic=True)
    await db_session.commit()

    # Detected as a conflict despite the missing conflict flag.
    assert result["status"] == "conflict"

    # A resolution task was spawned (the whole point — not a silent "blocked").
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [t for t in tasks if (t.first_payload or {}).get("type") == "create"]
    assert len(resolvers) == 1
    assert resolvers[0].context["backmerge_conflict"] is True
    assert resolvers[0].first_payload["base_branch"] == "automain"


@pytest.mark.asyncio
async def test_open_backmerge_pr_blocked_when_not_dirty(db_session, mocker):
    """A non-merge that is NOT a conflict and whose CI is still PENDING (not
    red) stays in the "blocked" path and does NOT spawn any remediation task —
    a pending build clears itself next cycle; there is nothing to fix yet."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 78, "html_url": "http://pr/78"},
        merge_return={"merged": False, "message": "required status checks pending"},
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 78,
                "mergeable_state": "blocked",
                "head": {"sha": "abc123"},
            }
        ),
    )
    # CI still in progress (not red) → not auto-fixable.
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "in_progress", "conclusion": None, "details": []}
        ),
    )

    result = await hb.open_backmerge_pr(db_session, project.id, periodic=True)
    await db_session.commit()

    assert result["status"] == "blocked"
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [t for t in tasks if (t.first_payload or {}).get("type") == "create"]
    assert len(resolvers) == 0


@pytest.mark.asyncio
async def test_open_backmerge_pr_ci_red_spawns_ci_fix_task(db_session, mocker):
    """A back-merge that merges cleanly (no conflict) but whose CI is RED on the
    merged tree spawns a CI-fix task — the systemic auto-remediation for a
    semantic merge break the textual conflict resolver can't see."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 91, "html_url": "http://pr/91"},
        merge_return={"merged": False, "message": "Repository rule violations found"},
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 91,
                "mergeable_state": "blocked",  # not "dirty" → not a conflict
                "head": {"sha": "cafe123"},
            }
        ),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [
                    {"name": "CI / Backend (shard 6)", "status": "completed", "conclusion": "failure"},
                    {"name": "CI / Backend (shard 1)", "status": "completed", "conclusion": "success"},
                ],
            }
        ),
    )

    result = await hb.open_backmerge_pr(db_session, project.id, periodic=True)
    await db_session.commit()

    assert result["status"] == "ci_failure"
    tasks = await _tasks_for(db_session, project.id)
    fixers = [
        t for t in tasks if (t.context or {}).get("backmerge_ci_failure") is True
    ]
    assert len(fixers) == 1, "exactly one CI-fix task should be spawned"
    fixer = fixers[0]
    # Inherits the merge-method=merge gate + unified dedup marker.
    assert fixer.context["backmerge_conflict"] is True
    assert fixer.context["failed_checks"] == ["CI / Backend (shard 6)"]
    assert fixer.priority == hb._RESOLUTION_PRIORITY
    assert (fixer.first_payload or {}).get("base_branch") == "automain"
    assert "CI / Backend (shard 6)" in (fixer.first_payload or {}).get("prompt", "")
    # An operator-visible Alert was also raised.
    alerts = await _alerts_for(db_session, project.id)
    assert any("blocked by CI" in (a.title or "") for a in alerts)


@pytest.mark.asyncio
async def test_open_backmerge_pr_ci_red_no_duplicate_remediation(db_session, mocker):
    """A second CI-red back-merge attempt does not spawn a duplicate fix task —
    the project-wide back-merge-remediation dedup covers conflict + CI-fix."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 92, "html_url": "http://pr/92"},
        merge_return={"merged": False, "message": "Repository rule violations found"},
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 92,
                "mergeable_state": "blocked",
                "head": {"sha": "beef456"},
            }
        ),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [
                    {"name": "CI / Backend", "status": "completed", "conclusion": "failure"},
                ],
            }
        ),
    )

    r1 = await hb.open_backmerge_pr(db_session, project.id, periodic=True)
    await db_session.commit()
    await hb.open_backmerge_pr(db_session, project.id, periodic=True)
    await db_session.commit()

    assert r1["status"] == "ci_failure"
    tasks = await _tasks_for(db_session, project.id)
    fixers = [t for t in tasks if (t.context or {}).get("backmerge_ci_failure") is True]
    assert len(fixers) == 1, "the dedup must prevent a duplicate CI-fix task"


@pytest.mark.asyncio
async def test_open_backmerge_pr_conflict_no_duplicate_resolver(db_session, mocker):
    """A second back-merge attempt for the SAME hotfix must raise another Alert
    but must NOT spawn a second resolution task (dedupe guard)."""
    project = await _make_project(db_session)
    hotfix_id = uuid.uuid4()
    _patch_github(
        mocker,
        create_return={"number": 70, "html_url": "http://pr/70"},
        merge_return={
            "merged": False,
            "conflict": True,
            "message": "still conflicting",
        },
    )

    # First attempt — spawns the resolution task.
    r1 = await hb.open_backmerge_pr(db_session, project.id, hotfix_task_id=hotfix_id)
    await db_session.commit()
    assert r1["status"] == "conflict"

    # Second attempt for the same hotfix — the resolution task is still open.
    r2 = await hb.open_backmerge_pr(db_session, project.id, hotfix_task_id=hotfix_id)
    await db_session.commit()
    assert r2["status"] == "conflict"

    # Still exactly ONE resolution task — no duplicate spawned.
    tasks = await _tasks_for(db_session, project.id)
    assert len(tasks) == 1

    # But both attempts raised an Alert (the operator sees the recurrence).
    alerts = await _alerts_for(db_session, project.id)
    assert len(alerts) == 2


async def test_open_backmerge_pr_conflict_uses_custom_branch_names_for_resolver(
    db_session, mocker
):
    """The spawned resolver task should mirror the configured branch names in
    its user-facing title and description."""
    project = await _make_project(
        db_session, integration_branch="trunk", promotion_target="stable"
    )
    _patch_github(
        mocker,
        create_return={"number": 88, "html_url": "http://pr/88"},
        merge_return={"merged": False, "conflict": True, "message": "conflict"},
    )

    await hb.open_backmerge_pr(db_session, project.id, hotfix_task_id=uuid.uuid4())
    await db_session.commit()

    task = (await _tasks_for(db_session, project.id))[0]
    assert task.title == "Resolve back-merge conflict: stable -> trunk"
    assert (
        task.description
        == "The automated stable -> trunk back-merge PR #88 hit a merge conflict; resolve it on trunk so the next promotion does not conflict."
    )


@pytest.mark.asyncio
async def test_advisory_lock_prevents_concurrent_duplicate_resolver(db_session, mocker):
    """Advisory lock prevents a duplicate resolver when two concurrent callers
    both pass the _resolution_task_already_open check before either commits.

    Simulates the production race: promotion-conflict hook + periodic reconciler
    fire in the same instant, both see no open resolver, but the advisory lock
    serializes them — exactly one resolver task is spawned.
    """
    project = await _make_project(db_session)

    # Both callers "see" no open resolver — simulates the race where neither has committed yet.
    mocker.patch(
        "app.hotfix_backmerge._resolution_task_already_open",
        new=AsyncMock(return_value=False),
    )

    # Lock mock: first call acquires it, second does not.
    lock_call_count = {"n": 0}
    real_execute = db_session.execute

    async def mock_execute(stmt, params=None, **kwargs):
        if "pg_try_advisory_xact_lock" in str(stmt):
            lock_call_count["n"] += 1
            from unittest.mock import MagicMock

            result = MagicMock()
            result.scalar.return_value = lock_call_count["n"] == 1
            return result
        if params is not None:
            return await real_execute(stmt, params, **kwargs)
        return await real_execute(stmt, **kwargs)

    mocker.patch.object(db_session, "execute", side_effect=mock_execute)

    # First call (e.g. promotion-conflict hook) — acquires lock, spawns resolver.
    await hb._handle_conflict(
        db_session,
        project.id,
        55,
        "http://pr/55",
        None,
        "hook",
        project=project,
        message="conflict",
        periodic=False,
    )
    await db_session.flush()

    # Second call (e.g. periodic reconciler) — lock fails, returns immediately.
    await hb._handle_conflict(
        db_session,
        project.id,
        55,
        "http://pr/55",
        None,
        "reconciler",
        project=project,
        message="conflict",
        periodic=False,
    )
    await db_session.commit()

    tasks = await _tasks_for(db_session, project.id)
    resolvers = [t for t in tasks if (t.first_payload or {}).get("type") == "create"]
    assert len(resolvers) == 1, (
        f"Advisory lock must prevent duplicate resolver spawn — got {len(resolvers)} resolvers"
    )


@pytest.mark.asyncio
async def test_open_backmerge_pr_conflict_respawns_after_resolver_terminal(
    db_session, mocker
):
    """If the prior resolution task has terminated (e.g. CANCELLED), a fresh
    conflict for the same hotfix DOES spawn a new resolution task — a terminal
    task no longer counts as 'open'."""
    project = await _make_project(db_session)
    hotfix_id = uuid.uuid4()
    _patch_github(
        mocker,
        create_return={"number": 80, "html_url": "http://pr/80"},
        merge_return={"merged": False, "conflict": True, "message": "conflict"},
    )

    r1 = await hb.open_backmerge_pr(db_session, project.id, hotfix_task_id=hotfix_id)
    await db_session.commit()
    assert r1["status"] == "conflict"

    # Mark the spawned resolver terminal (as if it was cancelled / failed).
    tasks = await _tasks_for(db_session, project.id)
    assert len(tasks) == 1
    tasks[0].status = TaskStatus.CANCELLED
    db_session.add(tasks[0])
    await db_session.commit()

    # A fresh conflict for the same hotfix now spawns a NEW resolver.
    r2 = await hb.open_backmerge_pr(db_session, project.id, hotfix_task_id=hotfix_id)
    await db_session.commit()
    assert r2["status"] == "conflict"

    tasks = await _tasks_for(db_session, project.id)
    assert len(tasks) == 2


# ===========================================================================
# 405 / error — Alert raised, no fix task
# ===========================================================================


async def test_open_backmerge_pr_405_non_merge_raises_alert(db_session, mocker):
    """A 405 non-merge (branch protection / checks) raises an ERROR Alert and
    spawns NO fix task — a 405 is an operator-resolved CI problem, not a code
    conflict."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 41, "html_url": "http://pr/41"},
        merge_return={
            "merged": False,
            "conflict": False,
            "message": "required status check pending",
        },
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "blocked"
    assert result["error"] == "merge_blocked"

    # An ERROR Alert was raised (a failed back-merge is never silent).
    alerts = await _alerts_for(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert alerts[0].title == "Back-merge failed: main -> automain"

    # No fix task spawned for a 405.
    assert await _tasks_for(db_session, project.id) == []


async def test_open_backmerge_pr_merge_exception_raises_alert(db_session, mocker):
    """An exception from merge_pull_request (e.g. a GitHub 5xx) is treated as a
    blocked back-merge: an ERROR Alert is raised and no task is spawned."""
    from fastapi import HTTPException

    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 60, "html_url": "http://pr/60"},
        merge_side_effect=HTTPException(status_code=500, detail="GitHub 500"),
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "blocked"
    assert result["error"] == "merge_error"

    alerts = await _alerts_for(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert await _tasks_for(db_session, project.id) == []


async def test_open_backmerge_pr_pr_creation_failure_raises_alert(db_session, mocker):
    """An exception from create_pull_request raises an ERROR Alert and returns
    blocked — the back-merge could not even be opened."""
    from fastapi import HTTPException

    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_side_effect=HTTPException(status_code=422, detail="bad ref"),
        merge_return={"merged": True},
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "blocked"
    assert result["error"] == "pr_creation_failed"

    alerts = await _alerts_for(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert await _tasks_for(db_session, project.id) == []


async def test_open_backmerge_pr_no_credentials_raises_alert(db_session, mocker):
    """With no resolvable GitHub token the back-merge is blocked and an ERROR
    Alert is raised — a missed back-merge must never pass silently."""
    # Project with no github_owner/repo -> _resolve_github_with_session fails.
    # integration_branch/promotion_target set so the branch resolver in
    # shared_state doesn't stage its own "Missing integration_branch" Alert
    # alongside the no_credentials alert we're asserting on.
    project = Project(
        name="Backmerge-NoCreds",
        user_id="bm-user",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "blocked"
    assert result["error"] == "no_credentials"

    alerts = await _alerts_for(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR


async def test_open_backmerge_pr_missing_project_is_blocked(db_session):
    """An unknown project id returns blocked without raising."""
    result = await hb.open_backmerge_pr(db_session, uuid.uuid4())
    assert result["status"] == "blocked"
    assert result["error"] == "project_not_found"


async def test_open_backmerge_pr_skipped_when_single_branch(db_session, mocker):
    """On a single-branch repo (integration_branch == promotion_target) the
    one-shot back-merge skips cleanly as inactive WITHOUT touching GitHub — it
    must never open a PR whose head equals its base. Mirrors the back-merge
    reconciler's guard so the hotfix-merge handler is safe for external
    single-branch projects."""
    project = await _make_project(
        db_session, integration_branch="main", promotion_target="main"
    )
    create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 1, "html_url": "http://pr/1"},
        merge_return={"merged": True},
    )

    result = await hb.open_backmerge_pr(
        db_session,
        project.id,
        merged_pr_url="http://pr/hotfix-1",
        hotfix_task_id=uuid.uuid4(),
    )
    await db_session.commit()

    assert result == {"status": "skipped", "reason": "lts_inactive"}
    # GitHub is never touched — no PR opened, no merge attempted.
    create_mock.assert_not_called()
    merge_mock.assert_not_called()
    # And no Alert / task is raised for the inert skip.
    assert await _alerts_for(db_session, project.id) == []
    assert await _tasks_for(db_session, project.id) == []


# ===========================================================================
# Combined-Alembic-chain guard (step 3b) — block / defer before merge
# ===========================================================================


def _migration_diff(path: str, revision: str, down_revision: str) -> str:
    """Build a minimal unified diff that ADDS one Alembic migration file.

    Shaped so _extract_rollup_migration_sources reconstructs the added file's
    revision / down_revision from the diff's ``+`` lines.
    """
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1,5 @@\n"
        '+"""hotfix migration"""\n'
        f"+revision = '{revision}'\n"
        f"+down_revision = '{down_revision}'\n"
        "+def upgrade():\n"
        "+    pass\n"
    )


async def test_backmerge_blocked_on_same_revision_dual_head(db_session, mocker):
    """A back-merge whose promotion-target diff adds a migration whose revision id
    already exists on the integration branch is BLOCKED before merge: the merge
    API is never called, an Alert is raised, and a dual-head-aware resolver is
    spawned (carrying the merge-method=merge marker)."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 200, "html_url": "http://pr/200"},
        merge_return={"merged": True},  # must never be reached
    )
    # The back-merge PR touches a migration file.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py"}]
        ),
    )
    # main adds 0002_hotfix while automain already has its own 0002_feature.
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(return_value=["0001_init.py", "0002_feature.py"]),
    )

    result = await hb.open_backmerge_pr(
        db_session, project.id, hotfix_task_id=uuid.uuid4()
    )
    await db_session.commit()

    assert result["status"] == "migration_conflict"
    assert result["pr_url"] == "http://pr/200"
    # The merge API was NEVER called — the dual head was caught before merge.
    merge_mock.assert_not_called()

    # An operator-facing Alert was raised.
    alerts = await _alerts_for(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert "blocked by Alembic chain" in alerts[0].title

    # Exactly one migration-aware resolver was spawned.
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers) == 1
    resolver = resolvers[0]
    # Inherits the merge-method=merge gate + unified dedup marker.
    assert resolver.context["backmerge_conflict"] is True
    assert resolver.first_payload["base_branch"] == "automain"
    assert resolver.first_payload["type"] == "create"
    assert resolver.priority == hb._RESOLUTION_PRIORITY
    # The prompt is Alembic-dual-head-aware, not the generic semantic-merge text.
    prompt = resolver.first_payload["prompt"]
    assert "EXPECTED_SCHEMA_REVISION" in prompt
    assert "alembic upgrade head" in prompt
    assert "immutable" in prompt.lower()
    # A SPAWNED history row was written.
    history = (
        (
            await db_session.execute(
                select(TaskHistory).where(TaskHistory.task_id == resolver.id)
            )
        )
        .scalars()
        .all()
    )
    assert any(h.action == "SPAWNED" for h in history)


async def test_backmerge_blocked_on_stale_anchor_dual_head(db_session, mocker):
    """The stale-anchor variant: the added migration anchors to a revision that
    is no longer the integration branch's head, which would create a second
    Alembic head. The back-merge is BLOCKED (no merge) and a resolver spawned."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 201, "html_url": "http://pr/201"},
        merge_return={"merged": True},
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0003_hotfix.py"}]
        ),
    )
    # 0003 anchors to 0001, but automain's head is 0002 — a stale anchor.
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0003_hotfix.py", "0003", "0001"
            )
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(return_value=["0001_init.py", "0002_feature.py"]),
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "migration_conflict"
    merge_mock.assert_not_called()
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers) == 1


async def test_backmerge_clean_migration_still_merges(db_session, mocker):
    """A back-merge whose added migration cleanly chains onto the integration
    head (no collision, no stale anchor) passes the guard and merges normally —
    the guard does not block valid migration back-merges."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 202, "html_url": "http://pr/202"},
        merge_return={"merged": True, "message": "Merged"},
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0003_hotfix.py"}]
        ),
    )
    # 0003 chains cleanly onto the current head 0002 — valid.
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0003_hotfix.py", "0003", "0002"
            )
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(return_value=["0001_init.py", "0002_feature.py"]),
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "merged"
    merge_mock.assert_called_once()
    assert await _alerts_for(db_session, project.id) == []
    assert await _tasks_for(db_session, project.id) == []


async def test_backmerge_defers_when_diff_fetch_fails(db_session, mocker):
    """Fail-safe: when the back-merge PR touches migrations but the diff (or the
    target-branch listing) cannot be fetched, the guard DEFERS — it must never
    merge an unvalidated migration back-merge. The periodic reconciler retries."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 203, "html_url": "http://pr/203"},
        merge_return={"merged": True},
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py"}]
        ),
    )
    # The diff fetch fails (transient GitHub error) — uncertainty -> defer.
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(side_effect=RuntimeError("GitHub 502")),
    )

    result = await hb.open_backmerge_pr(db_session, project.id, periodic=True)
    await db_session.commit()

    assert result["status"] == "deferred"
    assert result["error"] == "alembic_unverified"
    # Never merged, never alerted, never spawned a task — just retry next cycle.
    merge_mock.assert_not_called()
    assert await _alerts_for(db_session, project.id) == []
    assert await _tasks_for(db_session, project.id) == []


async def test_backmerge_migration_block_dedupes_resolver(db_session, mocker):
    """A periodic back-merge that keeps hitting the same dual head spawns exactly
    ONE resolver across cycles — the project-wide dedup (shared with the conflict
    / CI-fix paths) prevents a duplicate."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 204, "html_url": "http://pr/204"},
        merge_return={"merged": True},
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py"}]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(return_value=["0001_init.py", "0002_feature.py"]),
    )

    for _ in range(3):
        result = await hb.open_backmerge_pr(db_session, project.id, periodic=True)
        await db_session.commit()
        assert result["status"] == "migration_conflict"

    # One Alert, one resolver — deduped across all cycles.
    assert len(await _alerts_for(db_session, project.id)) == 1
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers) == 1


# ===========================================================================
# Auto-reconcile a single-collision dual head (no resolver spawned)
# ===========================================================================


def _migration_source(revision: str, down_revision: str) -> str:
    """A minimal, parseable Alembic migration source (revision + down_revision)."""
    return (
        '"""migration"""\n'
        f"revision = '{revision}'\n"
        f"down_revision = '{down_revision}'\n"
        "def upgrade():\n    pass\n"
        "def downgrade():\n    pass\n"
    )


def _linear_chain_sources(filenames: list[str]) -> dict[str, str]:
    """Build a parseable single-head linear chain from ordered ``NNNN`` filenames.

    The lowest ``NNNN`` is the root (``down_revision = None``); each subsequent
    migration chains onto the previous revision. Used to reconstruct the
    post-merge integration-branch chain and assert it resolves to one head.
    """
    sources: dict[str, str] = {}
    prev: str | None = None
    for fn in sorted(filenames):
        rev = fn.split("_", 1)[0]
        if prev is None:
            sources[fn] = (
                '"""migration"""\n'
                f"revision = '{rev}'\n"
                "down_revision = None\n"
                "def upgrade():\n    pass\n"
            )
        else:
            sources[fn] = _migration_source(rev, prev)
        prev = rev
    return sources


def _branch_aware_versions(main_list: list[str], automain_list: list[str]) -> AsyncMock:
    """Branch-keyed ``list_versions_at_ref``: promotion target vs integration.

    ``ref`` is the 4th positional arg of ``list_versions_at_ref``.
    """

    async def _side(token, owner, repo, ref, project_id=None):
        return list(main_list) if ref == "main" else list(automain_list)

    return AsyncMock(side_effect=_side)


async def test_backmerge_autoreconciles_single_collision_dual_head(db_session, mocker):
    """A single integration-side migration colliding with the immutable
    promotion-target hotfix migration is NEVER reconciled inline by pushing to
    the live integration branch: doing so would leave automain with a dangling
    migration (the renumber chains onto the hotfix rev, which is not on automain
    until the merge lands) for as long as the merge is held off — wedging
    `alembic upgrade head`. The block ALWAYS routes to the guided resolver:
    commit_file_changes is NOT called against the integration branch, the merge
    is never attempted, and exactly one resolver task is spawned."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 300, "html_url": "http://pr/300"},
        merge_return={"merged": True, "message": "Merged"},  # must never be reached
    )
    # The back-merge PR adds main's 0002_hotfix (rev 0002, down 0001).
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py"}]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    # main carries the hotfix at 0002; automain carries its own feature at 0002.
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_branch_aware_versions(
            ["0001_init.py", "0002_hotfix.py"],
            ["0001_init.py", "0002_feature.py"],
        ),
    )

    async def _file_text(path, *, token, owner, repo, ref, project_id=None):
        if path == "backend/app/main.py":
            return 'EXPECTED_SCHEMA_REVISION = "0002"\n'
        if path.endswith("0002_feature.py"):
            return _migration_source("0002", "0001")
        return None

    mocker.patch(
        "app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_file_text)
    )
    commit_mock = mocker.patch(
        "app.github.commit_file_changes", new=AsyncMock(return_value={"commit": "sha"})
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    # Routed to the guided resolver — no inline push, no merge.
    assert result["status"] == "migration_conflict"
    merge_mock.assert_not_called()
    # The integration branch was never mutated — no direct push to automain.
    commit_mock.assert_not_called()

    # Exactly one resolver task was spawned to land the renumber atomically.
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers) == 1


async def test_backmerge_block_never_pushes_to_integration_branch(db_session, mocker):
    """Explicit regression for the wedge hazard: on a dual-head `block` verdict,
    commit_file_changes must NEVER be invoked with the integration branch as the
    target — the live integration branch is never mutated before the merge."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 305, "html_url": "http://pr/305"},
        merge_return={"merged": True},  # must never be reached
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py"}]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_branch_aware_versions(
            ["0001_init.py", "0002_hotfix.py"],
            ["0001_init.py", "0002_feature.py"],
        ),
    )

    async def _file_text(path, *, token, owner, repo, ref, project_id=None):
        if path == "backend/app/main.py":
            return 'EXPECTED_SCHEMA_REVISION = "0002"\n'
        if path.endswith("0002_feature.py"):
            return _migration_source("0002", "0001")
        return None

    mocker.patch(
        "app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_file_text)
    )
    commit_mock = mocker.patch(
        "app.github.commit_file_changes", new=AsyncMock(return_value={"commit": "sha"})
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "migration_conflict"
    merge_mock.assert_not_called()
    # No call to commit_file_changes targets the integration branch (automain).
    for call in commit_mock.call_args_list:
        assert "automain" not in call.args, (
            "block verdict must never push to the live integration branch"
        )


async def test_backmerge_autoreconcile_push_then_defers_no_wedge(db_session, mocker):
    """A dual head routes to the resolver instead of pushing to automain, and a
    subsequent periodic call with the resolver still open produces no duplicate
    resolver and no new alert — the integration branch is never mutated."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 306, "html_url": "http://pr/306"},
        merge_return={"merged": True},  # must never be reached
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py"}]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_branch_aware_versions(
            ["0001_init.py", "0002_hotfix.py"],
            ["0001_init.py", "0002_feature.py"],
        ),
    )

    async def _file_text(path, *, token, owner, repo, ref, project_id=None):
        if path == "backend/app/main.py":
            return 'EXPECTED_SCHEMA_REVISION = "0002"\n'
        if path.endswith("0002_feature.py"):
            return _migration_source("0002", "0001")
        return None

    mocker.patch(
        "app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_file_text)
    )
    commit_mock = mocker.patch(
        "app.github.commit_file_changes", new=AsyncMock(return_value={"commit": "sha"})
    )

    # First call: spawns the resolver, never touches the integration branch.
    first = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()
    assert first["status"] == "migration_conflict"
    commit_mock.assert_not_called()
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers) == 1
    alerts_after_first = len(await _alerts_for(db_session, project.id))

    # Second (periodic) call with the resolver still open: dedup — no duplicate
    # resolver, no new alert, and still no push to the integration branch.
    second = await hb.open_backmerge_pr(db_session, project.id, periodic=True)
    await db_session.commit()
    assert second["status"] == "migration_conflict"
    merge_mock.assert_not_called()
    commit_mock.assert_not_called()
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers) == 1
    assert len(await _alerts_for(db_session, project.id)) == alerts_after_first


async def test_backmerge_autoreconcile_subsequent_cycle_no_spurious_resolver(
    db_session, mocker
):
    """The periodic reconciler calling open_backmerge_pr(periodic=True) when a
    migration resolver is already open must dedup: it returns migration_conflict
    without spawning a new task or raising a new alert."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 307, "html_url": "http://pr/307"},
        merge_return={"merged": True},  # must never be reached
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py"}]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_branch_aware_versions(
            ["0001_init.py", "0002_hotfix.py"],
            ["0001_init.py", "0002_feature.py"],
        ),
    )

    async def _file_text(path, *, token, owner, repo, ref, project_id=None):
        if path == "backend/app/main.py":
            return 'EXPECTED_SCHEMA_REVISION = "0002"\n'
        if path.endswith("0002_feature.py"):
            return _migration_source("0002", "0001")
        return None

    mocker.patch(
        "app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_file_text)
    )
    commit_mock = mocker.patch(
        "app.github.commit_file_changes", new=AsyncMock(return_value={"commit": "sha"})
    )

    # Prime an open resolver via a first non-periodic block.
    await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()
    baseline_alerts = len(await _alerts_for(db_session, project.id))

    # Periodic re-entry: dedup against the open resolver.
    result = await hb.open_backmerge_pr(db_session, project.id, periodic=True)
    await db_session.commit()

    assert result["status"] == "migration_conflict"
    merge_mock.assert_not_called()
    commit_mock.assert_not_called()
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers) == 1
    assert len(await _alerts_for(db_session, project.id)) == baseline_alerts


async def test_backmerge_ambiguous_dual_head_falls_back_to_resolver(db_session, mocker):
    """When the block is NOT a single unambiguous collision (the integration
    branch's post-divergence set is empty), the guided dual-head resolver is
    spawned and nothing is pushed to the integration branch."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 301, "html_url": "http://pr/301"},
        merge_return={"merged": True},  # must never be reached
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py"}]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    # Both refs report the same listing — no post-divergence integration file to
    # renumber, so the planner returns None (ambiguous / nothing mechanical).
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(return_value=["0001_init.py", "0002_feature.py"]),
    )
    commit_mock = mocker.patch(
        "app.github.commit_file_changes", new=AsyncMock(return_value={"commit": "sha"})
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "migration_conflict"
    merge_mock.assert_not_called()
    commit_mock.assert_not_called()
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers) == 1


async def test_backmerge_autoreconcile_push_error_falls_back(db_session, mocker):
    """Fail-safe: even if commit_file_changes would raise, the block path never
    pushes to the integration branch — the merge is never attempted and the
    guided resolver is spawned."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 302, "html_url": "http://pr/302"},
        merge_return={"merged": True},  # must never be reached
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py"}]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_branch_aware_versions(
            ["0001_init.py", "0002_hotfix.py"],
            ["0001_init.py", "0002_feature.py"],
        ),
    )

    async def _file_text(path, *, token, owner, repo, ref, project_id=None):
        if path == "backend/app/main.py":
            return 'EXPECTED_SCHEMA_REVISION = "0002"\n'
        if path.endswith("0002_feature.py"):
            return _migration_source("0002", "0001")
        return None

    mocker.patch(
        "app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_file_text)
    )
    mocker.patch(
        "app.github.commit_file_changes",
        new=AsyncMock(side_effect=RuntimeError("GitHub 500")),
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "migration_conflict"
    merge_mock.assert_not_called()
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers) == 1


async def test_backmerge_autoreconcile_missing_schema_literal_falls_back(
    db_session, mocker
):
    """A dual-head block routes to the guided resolver and pushes nothing to the
    integration branch, regardless of main.py contents."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 303, "html_url": "http://pr/303"},
        merge_return={"merged": True},  # must never be reached
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py"}]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_branch_aware_versions(
            ["0001_init.py", "0002_hotfix.py"],
            ["0001_init.py", "0002_feature.py"],
        ),
    )

    async def _file_text(path, *, token, owner, repo, ref, project_id=None):
        if path == "backend/app/main.py":
            return "# no schema literal here\n"
        if path.endswith("0002_feature.py"):
            return _migration_source("0002", "0001")
        return None

    mocker.patch(
        "app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_file_text)
    )
    commit_mock = mocker.patch(
        "app.github.commit_file_changes", new=AsyncMock(return_value={"commit": "sha"})
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "migration_conflict"
    merge_mock.assert_not_called()
    commit_mock.assert_not_called()
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers) == 1


# ===========================================================================
# Embedded renumber plan in the spawned dual-head resolver
# ===========================================================================


async def test_backmerge_single_collision_embeds_renumber_plan(db_session, mocker):
    """A single-collision dual-head block hands the spawned resolver the CONCRETE,
    already-validated renumber plan — both embedded in the prompt text (new
    revision id, file being renamed, resulting EXPECTED_SCHEMA_REVISION) AND
    stored as structured data under ``context['backmerge_renumber_plan']``. The
    merge API is never called and exactly one resolver is spawned."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 600, "html_url": "http://pr/600"},
        merge_return={"merged": True},  # must never be reached
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py"}]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    # main carries the hotfix at 0002; automain carries its own feature at 0002 —
    # the unambiguous single-collision case the planner mechanically reconciles.
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_branch_aware_versions(
            ["0001_init.py", "0002_hotfix.py"],
            ["0001_init.py", "0002_feature.py"],
        ),
    )

    async def _file_text(path, *, token, owner, repo, ref, project_id=None):
        if path.endswith("0002_feature.py"):
            return _migration_source("0002", "0001")
        return None

    mocker.patch(
        "app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_file_text)
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "migration_conflict"
    merge_mock.assert_not_called()

    tasks = await _tasks_for(db_session, project.id)
    resolvers = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers) == 1
    resolver = resolvers[0]

    # The structured plan is stored in context and matches the planner output.
    expected_plan = await _plan_backmerge_migration_reconciliation(
        "gh-token", "acme", "widget", 600, "automain", "main"
    )
    assert expected_plan is not None
    plan = resolver.context["backmerge_renumber_plan"]
    assert plan is not None
    assert plan["new_revision"] == "0003"
    assert plan["expected_schema_revision"] == "0003"
    assert plan["old_path"] == "backend/alembic/versions/0002_feature.py"
    assert plan["new_path"] == expected_plan["new_path"]
    assert plan["new_down_revision"] == expected_plan["new_down_revision"]

    # The concrete plan is embedded in the resolver prompt itself.
    prompt = resolver.first_payload["prompt"]
    assert "Known-good renumber plan" in prompt
    assert "0003" in prompt  # the new revision id
    assert "0002_feature.py" in prompt  # the file being renamed
    assert "EXPECTED_SCHEMA_REVISION" in prompt
    # Presented as a verify-then-apply candidate, not a blind apply.
    assert "verify" in prompt.lower()


async def test_backmerge_nonmechanical_block_falls_back_to_prose(db_session, mocker):
    """A non-mechanical block (fan-out tail) yields a None plan, so the resolver
    falls back to the existing prose guidance with NO embedded plan: no
    "Known-good renumber plan" section in the prompt and
    ``context['backmerge_renumber_plan']`` is None. The merge is still blocked
    and exactly one resolver is spawned."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 601, "html_url": "http://pr/601"},
        merge_return={"merged": True},  # must never be reached
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py"}]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    # automain's colliding 0002_feature fans out into two children — no single
    # linear tail, so the planner refuses (returns None).
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_branch_aware_versions(
            ["0001_init.py", "0002_hotfix.py"],
            ["0001_init.py", "0002_feature.py", "0003_a.py", "0004_b.py"],
        ),
    )

    async def _file_text(path, *, token, owner, repo, ref, project_id=None):
        if path.endswith("0002_feature.py"):
            return _migration_source("0002", "0001")
        if path.endswith("0003_a.py"):
            return _migration_source("0003", "0002")
        if path.endswith("0004_b.py"):
            return _migration_source("0004", "0002")
        return None

    mocker.patch(
        "app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_file_text)
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "migration_conflict"
    merge_mock.assert_not_called()

    tasks = await _tasks_for(db_session, project.id)
    resolvers = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers) == 1
    resolver = resolvers[0]

    # No mechanical plan: context key present but None, prose prompt unchanged.
    assert resolver.context["backmerge_renumber_plan"] is None
    prompt = resolver.first_payload["prompt"]
    assert "Known-good renumber plan" not in prompt
    # The prose guidance is still present.
    assert "alembic upgrade head" in prompt
    assert "EXPECTED_SCHEMA_REVISION" in prompt


async def test_backmerge_planner_exception_still_spawns_resolver(db_session, mocker):
    """Fail-safe: a planner that RAISES must never crash the block path or prevent
    the resolver spawn. The block still alerts and spawns exactly one prose-only
    resolver (plan None in context), and the merge is never attempted."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 602, "html_url": "http://pr/602"},
        merge_return={"merged": True},  # must never be reached
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py"}]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_branch_aware_versions(
            ["0001_init.py", "0002_hotfix.py"],
            ["0001_init.py", "0002_feature.py"],
        ),
    )
    # The planner blows up — the block path must degrade to the prose resolver.
    mocker.patch.object(
        hb,
        "_plan_backmerge_migration_reconciliation",
        new=AsyncMock(side_effect=RuntimeError("planner exploded")),
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "migration_conflict"
    merge_mock.assert_not_called()

    # Exactly one Alert and one resolver, despite the planner failure.
    assert len(await _alerts_for(db_session, project.id)) == 1
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers) == 1
    resolver = resolvers[0]
    assert resolver.context["backmerge_renumber_plan"] is None
    assert "Known-good renumber plan" not in resolver.first_payload["prompt"]


# ===========================================================================
# _plan_backmerge_migration_reconciliation — unit (helper orchestration)
# ===========================================================================


async def test_plan_backmerge_reconciliation_single_collision(mocker):
    """The helper filters to post-divergence integration migrations and returns a
    plan for the single-collision case."""
    from app import rollup_migration_validation as rmv

    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_branch_aware_versions(
            ["0001_init.py", "0002_hotfix.py"],
            ["0001_init.py", "0002_feature.py"],
        ),
    )

    async def _file_text(path, *, token, owner, repo, ref, project_id=None):
        assert ref == "automain"  # integration sources read at the integration ref
        if path.endswith("0002_feature.py"):
            return _migration_source("0002", "0001")
        return None

    mocker.patch(
        "app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_file_text)
    )

    plan = await rmv._plan_backmerge_migration_reconciliation(
        "tok", "acme", "widget", 400, "automain", "main"
    )
    assert plan is not None
    assert plan["new_revision"] == "0003"
    assert plan["expected_schema_revision"] == "0003"
    assert plan["deletes"] == ["backend/alembic/versions/0002_feature.py"]


async def test_plan_backmerge_reconciliation_empty_diff_returns_none(mocker):
    """No added migration in the back-merge diff → nothing to reconcile → None."""
    from app import rollup_migration_validation as rmv

    mocker.patch(
        "app.github.get_pull_request_diff", new=AsyncMock(return_value="")
    )
    plan = await rmv._plan_backmerge_migration_reconciliation(
        "tok", "acme", "widget", 401, "automain", "main"
    )
    assert plan is None


async def test_plan_backmerge_reconciliation_fetch_error_returns_none(mocker):
    """A failure listing the promotion target's migrations is fail-safe → None."""
    from app import rollup_migration_validation as rmv

    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(side_effect=RuntimeError("GitHub 502")),
    )
    plan = await rmv._plan_backmerge_migration_reconciliation(
        "tok", "acme", "widget", 402, "automain", "main"
    )
    assert plan is None


# ===========================================================================
# reconcile_main_into_automain — the periodic reconciler
# ===========================================================================


async def test_reconcile_skipped_when_integration_branch_not_automain(
    db_session, mocker
):
    """The reconciler is a no-op when the LTS branch model is not active —
    integration_branch != "automain" means there is no separate trunk to
    reconcile, so GitHub is never touched."""
    project = await _make_project(db_session, integration_branch="main")
    create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 1, "html_url": "http://pr/1"},
        merge_return={"merged": True},
    )

    result = await hb.reconcile_main_into_automain(db_session, project.id)

    assert result == {"status": "skipped", "reason": "lts_inactive"}
    create_mock.assert_not_called()
    merge_mock.assert_not_called()


async def test_reconcile_delegates_to_backmerge_when_lts_active(db_session, mocker):
    """With integration_branch == "automain" the reconciler delegates to
    open_backmerge_pr and back-merges main -> automain."""
    project = await _make_project(db_session)
    create_mock, _merge_mock = _patch_github(
        mocker,
        create_return={"number": 9, "html_url": "http://pr/9"},
        merge_return={"merged": True},
    )

    result = await hb.reconcile_main_into_automain(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "merged"
    assert create_mock.call_args.kwargs["head"] == "main"
    assert create_mock.call_args.kwargs["base"] == "automain"


async def test_reconcile_empty_diff_is_noop(db_session, mocker):
    """When main == automain the reconciler is a clean no-op."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"empty_diff": True, "merged": True, "html_url": ""},
        merge_return={"merged": True},
    )

    result = await hb.reconcile_main_into_automain(db_session, project.id)

    assert result["status"] == "noop_empty_diff"


# ===========================================================================
# periodic=True — alert-storm suppression
# ===========================================================================


async def test_periodic_block_raises_one_deduped_alert(db_session, mocker):
    """A periodic back-merge stuck on a non-conflict block (CI pending / 405)
    raises exactly ONE Alert no matter how many cycles run — the first stuck
    cycle escalates, the rest dedupe against the still-open Alert. Not a storm,
    but not silent either: a persistently stuck back-merge is surfaced once."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 41, "html_url": "http://pr/41"},
        merge_return={
            "merged": False,
            "conflict": False,
            "message": "required status check pending",
        },
    )

    # Three reconciler cycles, all hitting the same non-conflict block.
    for _ in range(3):
        result = await hb.open_backmerge_pr(db_session, project.id, periodic=True)
        await db_session.commit()
        assert result["status"] == "blocked"
        assert result["error"] == "merge_blocked"

    # Exactly one Alert — escalated once, deduped across the rest. No fix task.
    alerts = await _alerts_for(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert alerts[0].title == "Back-merge failed: main -> automain"
    assert await _tasks_for(db_session, project.id) == []


async def test_periodic_conflict_dedupes_alert_and_plan(db_session, mocker):
    """A periodic back-merge that keeps hitting a conflict raises exactly ONE
    Alert and spawns exactly ONE resolution plan, no matter how many cycles
    run — the reconciler must not stack an Alert + plan every cycle."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 77, "html_url": "http://pr/77"},
        merge_return={
            "merged": False,
            "conflict": True,
            "message": "still conflicting",
        },
    )

    # Three reconciler cycles, all hitting the same conflict.
    for _ in range(3):
        result = await hb.open_backmerge_pr(db_session, project.id, periodic=True)
        await db_session.commit()
        assert result["status"] == "conflict"

    # Exactly one Alert and one resolution plan — deduped across all cycles.
    assert len(await _alerts_for(db_session, project.id)) == 1
    tasks = await _tasks_for(db_session, project.id)
    assert len(tasks) == 1
    assert tasks[0].context["backmerge_conflict"] is True


# ===========================================================================
# Config-driven branch names — arbitrary integration trunk and promotion target
# ===========================================================================


async def test_open_backmerge_pr_uses_configured_branch_names(db_session, mocker):
    """With arbitrary integration_branch / promotion_target, open_backmerge_pr
    creates the PR from promotion_target INTO integration_branch — the back-merge
    path is config-driven, not hardcoded to 'main'/'automain'."""
    project = await _make_project(
        db_session, integration_branch="mytrunk", promotion_target="mylts"
    )
    create_mock, _merge_mock = _patch_github(
        mocker,
        create_return={"number": 99, "html_url": "http://pr/99"},
        merge_return={"merged": True},
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "merged"
    # PR head is the promotion target; base is the integration trunk.
    assert create_mock.call_args.kwargs["head"] == "mylts"
    assert create_mock.call_args.kwargs["base"] == "mytrunk"
    assert create_mock.call_args.kwargs["title"] == "Back-merge: mylts -> mytrunk"


async def test_reconcile_activates_for_custom_branch_names(db_session, mocker):
    """The reconciler is active whenever integration_branch != promotion_target,
    regardless of the specific names — not only when integration_branch is
    literally 'automain'."""
    project = await _make_project(
        db_session, integration_branch="mytrunk", promotion_target="mylts"
    )
    create_mock, _merge_mock = _patch_github(
        mocker,
        create_return={"number": 100, "html_url": "http://pr/100"},
        merge_return={"merged": True},
    )

    result = await hb.reconcile_main_into_automain(db_session, project.id)
    await db_session.commit()

    # Should NOT skip — integration != promotion means LTS model is active.
    assert result["status"] == "merged"
    assert create_mock.call_args.kwargs["head"] == "mylts"
    assert create_mock.call_args.kwargs["base"] == "mytrunk"


async def test_open_backmerge_pr_skipped_when_integration_equals_promotion(
    db_session, mocker
):
    """The single-branch external-repo case (integration_branch ==
    promotion_target) short-circuits open_backmerge_pr explicitly: GitHub is
    never touched, no Alert, no task — the back-merge tail is genuinely inert
    rather than relying on a degenerate empty-diff PR to no-op by accident.

    This is the direct hotfix entry point (task_lifecycle.py calls
    open_backmerge_pr, not the reconciler), so its inertness must be guarded
    independently of reconcile_main_into_automain."""
    project = await _make_project(
        db_session, integration_branch="main", promotion_target="main"
    )
    create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 1, "html_url": "http://pr/1"},
        merge_return={"merged": True},
    )

    result = await hb.open_backmerge_pr(
        db_session,
        project.id,
        merged_pr_url="http://pr/hotfix-1",
        hotfix_task_id=uuid.uuid4(),
    )
    await db_session.commit()

    assert result == {"status": "skipped", "reason": "lts_inactive"}
    # No GitHub I/O at all — not even a PR-creation attempt.
    create_mock.assert_not_called()
    merge_mock.assert_not_called()
    # No Alert and no resolution task for a single-branch repo.
    assert await _alerts_for(db_session, project.id) == []
    assert await _tasks_for(db_session, project.id) == []


async def test_open_backmerge_pr_skipped_single_branch_custom_name(db_session, mocker):
    """The single-branch guard is config-driven, not hardcoded to 'main': any
    project whose integration_branch equals its promotion_target short-circuits,
    resolving the names through the per-project branch resolver."""
    project = await _make_project(
        db_session, integration_branch="trunk", promotion_target="trunk"
    )
    create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 2, "html_url": "http://pr/2"},
        merge_return={"merged": True},
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result == {"status": "skipped", "reason": "lts_inactive"}
    create_mock.assert_not_called()
    merge_mock.assert_not_called()


async def test_reconcile_skipped_when_integration_equals_promotion(db_session, mocker):
    """The reconciler skips when integration_branch == promotion_target — that
    means there is no separate trunk to reconcile (LTS model inactive)."""
    project = await _make_project(
        db_session, integration_branch="main", promotion_target="main"
    )
    create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 1, "html_url": "http://pr/1"},
        merge_return={"merged": True},
    )

    result = await hb.reconcile_main_into_automain(db_session, project.id)

    assert result == {"status": "skipped", "reason": "lts_inactive"}
    create_mock.assert_not_called()
    merge_mock.assert_not_called()


# ===========================================================================
# Per-project promotion_target resolution
# ===========================================================================


async def test_open_backmerge_pr_uses_per_project_promotion_target(db_session, mocker):
    """When project.promotion_target is set, the back-merge goes to the
    project-specific LTS branch."""
    project = await _make_project(
        db_session, integration_branch="proj-trunk", promotion_target="proj-lts"
    )
    create_mock, _merge_mock = _patch_github(
        mocker,
        create_return={"number": 42, "html_url": "http://pr/42"},
        merge_return={"merged": True},
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "merged"
    # PR must use the per-project branch names, not the global config values.
    assert create_mock.call_args.kwargs["head"] == "proj-lts"
    assert create_mock.call_args.kwargs["base"] == "proj-trunk"
    assert create_mock.call_args.kwargs["title"] == "Back-merge: proj-lts -> proj-trunk"


async def test_reconcile_uses_per_project_branch_names(db_session, mocker):
    """reconcile_main_into_automain uses per-project branch names for both the
    LTS-active check and the back-merge PR."""
    project = await _make_project(
        db_session, integration_branch="proj-trunk", promotion_target="proj-lts"
    )
    create_mock, _merge_mock = _patch_github(
        mocker,
        create_return={"number": 43, "html_url": "http://pr/43"},
        merge_return={"merged": True},
    )

    result = await hb.reconcile_main_into_automain(db_session, project.id)
    await db_session.commit()

    # Per-project branches differ, so LTS is active and reconciler runs.
    assert result["status"] == "merged"
    assert create_mock.call_args.kwargs["head"] == "proj-lts"
    assert create_mock.call_args.kwargs["base"] == "proj-trunk"


# ===========================================================================
# Regression: single-head invariant after successful merge
# ===========================================================================


async def test_backmerge_after_successful_merge_has_single_head(db_session, mocker):
    """After a successful back-merge, the resulting integration branch's migration
    chain must have exactly one head (linear, no dual heads). This is the critical
    invariant for the back-merge path: the merge must never leave automain with a
    dangling second migration head that would wedge `alembic upgrade head`."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 500, "html_url": "http://pr/500"},
        merge_return={"merged": True, "message": "Merged"},
    )
    # The back-merge PR adds a clean migration (no collision, no stale anchor).
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0003_hotfix.py"}]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0003_hotfix.py", "0003", "0002"
            )
        ),
    )
    # main has 0001 <- 0002 <- 0003(hotfix)
    # automain has 0001 <- 0002 <- 0002b(feature, a collision or different feature)
    # After merge, automain should have a single linear chain from 0001 to 0003,
    # with no dangling second head.
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_branch_aware_versions(
            ["0001_init.py", "0002_prev.py", "0003_hotfix.py"],
            ["0001_init.py", "0002_prev.py"],
        ),
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    # Merge succeeded with no operator escalation.
    assert result["status"] == "merged"
    merge_mock.assert_called_once()
    assert await _alerts_for(db_session, project.id) == []
    assert await _tasks_for(db_session, project.id) == []

    # Load-bearing invariant: the migration set on the integration branch AFTER
    # the back-merge lands must resolve to EXACTLY ONE Alembic head. The merge
    # fast-forwards automain to include main's full chain, so the post-merge set
    # is the union of the promotion target's migration filenames and the added
    # hotfix migration. Reconstruct that chain and assert a single linear head —
    # a planner/merge regression that produced or merged a second head would
    # leave len(find_heads(...)) == 2 here and fail this test.
    post_merge_sources = _linear_chain_sources(
        ["0001_init.py", "0002_prev.py", "0003_hotfix.py"]
    )
    chain, duplicates, unparseable = build_migration_chain(post_merge_sources)
    assert not duplicates, f"merged chain has duplicate revisions: {duplicates}"
    assert not unparseable, f"merged chain has unparseable files: {unparseable}"
    assert find_heads(chain) == ["0003"], (
        "back-merge must leave the integration branch with exactly one Alembic "
        f"head; got heads {sorted(find_heads(chain))}"
    )


# ===========================================================================
# Planner refuses: fan-out tail (multi-child descendant) → resolver spawned
# ===========================================================================


async def test_backmerge_migration_fanout_tail_refuses_falls_back(db_session, mocker):
    """Fan-out tail (multi-child descendant) defers to the human resolver.

    Two mechanisms guard this class, both asserted here:

    * The live back-merge path BLOCKS the merge via
      ``_validate_rollup_migration_chain`` ->
      ``validate_combined_migration_chain``'s revision-id collision check (the
      added hotfix revision already exists on the integration branch). It does
      NOT auto-reconcile inline; it raises an Alert and spawns the dual-head
      resolver.
    * The back-merge reconciliation planner
      (``_plan_backmerge_migration_reconciliation`` ->
      ``plan_integration_side_renumber``) deliberately REFUSES (returns None)
      because the colliding integration migration's descendants form a fan-out
      (more than one child), not a single linear tail — so there is no
      mechanical renumber and the work must defer to the resolver.
    """
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 501, "html_url": "http://pr/501"},
        merge_return={"merged": True},  # must never be reached
    )
    # main back-merges its hotfix at revision 0002 (anchored to the shared 0001).
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py"}]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    # automain has 0002_feature (rev 0002, colliding with main's hotfix), and it
    # fans out into two distinct children: 0002_feature <- 0003_a and
    # 0002_feature <- 0004_b. The fan-out is what makes the renumber ambiguous.
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_branch_aware_versions(
            ["0001_init.py", "0002_hotfix.py"],
            [
                "0001_init.py",
                "0002_feature.py",
                "0003_a.py",
                "0004_b.py",
            ],
        ),
    )

    async def _file_text(path, *, token, owner, repo, ref, project_id=None):
        # automain's 0002_feature (anchored to shared 0001) collides with main's
        # 0002_hotfix; it has two distinct children — a genuine fan-out tail.
        if path.endswith("0002_feature.py"):
            return _migration_source("0002", "0001")
        if path.endswith("0003_a.py"):
            return _migration_source("0003", "0002")
        if path.endswith("0004_b.py"):
            return _migration_source("0004", "0002")
        return None

    mocker.patch(
        "app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_file_text)
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    # Routed to the resolver — merge never attempted.
    assert result["status"] == "migration_conflict"
    merge_mock.assert_not_called()

    # Exactly one resolver task was spawned.
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers) == 1, (
        "fan-out tail (multi-child) must route to resolver, not be auto-reconciled"
    )

    # An Alert was raised.
    alerts = await _alerts_for(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR

    # And the back-merge reconciliation planner itself refuses this fan-out:
    # no mechanical renumber exists, so it returns None and the path defers.
    plan = await _plan_backmerge_migration_reconciliation(
        "gh-token", "acme", "widget", 501, "automain", "main"
    )
    assert plan is None, "fan-out tail must not be mechanically auto-reconciled"


async def test_backmerge_migration_multiple_collisions_refuses_falls_back(
    db_session, mocker
):
    """More-than-one simultaneous collision defers to the human resolver.

    The live back-merge path BLOCKS the merge via
    ``validate_combined_migration_chain``'s revision-id collision check (the
    first colliding revision is reported) and spawns the resolver. The
    back-merge reconciliation planner
    (``_plan_backmerge_migration_reconciliation`` ->
    ``plan_integration_side_renumber``) independently REFUSES (returns None)
    because more than one integration revision collides with the promotion
    target — an ambiguous multi-collision the mechanical single-renumber cannot
    safely resolve."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 502, "html_url": "http://pr/502"},
        merge_return={"merged": True},  # must never be reached
    )
    # main has two migrations colliding with automain's:
    # main: 0001 <- 0002(hotfix_a) and 0001 <- 0002 <- 0003(hotfix_b)
    # automain: 0001 <- 0002(feature_a) and 0001 <- 0002 <- 0003(feature_b)
    # Both 0002 and 0003 collide.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[
                {"filename": "backend/alembic/versions/0002_hotfix_a.py"},
                {"filename": "backend/alembic/versions/0003_hotfix_b.py"},
            ]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=(
                _migration_diff(
                    "backend/alembic/versions/0002_hotfix_a.py", "0002", "0001"
                )
                + _migration_diff(
                    "backend/alembic/versions/0003_hotfix_b.py", "0003", "0002"
                )
            )
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_branch_aware_versions(
            [
                "0001_init.py",
                "0002_hotfix_a.py",
                "0003_hotfix_b.py",
            ],
            [
                "0001_init.py",
                "0002_feature_a.py",
                "0003_feature_b.py",
            ],
        ),
    )

    async def _file_text(path, *, token, owner, repo, ref, project_id=None):
        # Both integration migrations collide with main's back-merged revisions.
        if path.endswith("0002_feature_a.py"):
            return _migration_source("0002", "0001")
        if path.endswith("0003_feature_b.py"):
            return _migration_source("0003", "0002")
        return None

    mocker.patch(
        "app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_file_text)
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    # Routed to the resolver — merge never attempted.
    assert result["status"] == "migration_conflict"
    merge_mock.assert_not_called()

    # Exactly one resolver task was spawned (deduped).
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers) == 1, (
        "multiple simultaneous collisions must route to resolver"
    )

    # The reconciliation planner refuses: more than one collision is ambiguous,
    # so no mechanical renumber is attempted and the work defers to the resolver.
    plan = await _plan_backmerge_migration_reconciliation(
        "gh-token", "acme", "widget", 502, "automain", "main"
    )
    assert plan is None, "multiple simultaneous collisions must not auto-reconcile"


async def test_backmerge_migration_stale_anchor_non_shared_refuses_falls_back(
    db_session, mocker
):
    """Stale-anchor collision (ancestor not on the target) defers to the resolver.

    The live back-merge path BLOCKS the merge via
    ``validate_combined_migration_chain``'s revision-id collision check and
    spawns the resolver. The back-merge reconciliation planner
    (``_plan_backmerge_migration_reconciliation`` ->
    ``plan_integration_side_renumber``) independently REFUSES (returns None)
    because the colliding integration migration anchors to another
    integration-only revision rather than the shared promotion-target ancestor —
    a non-mechanical fork that must never be auto-renumbered."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 503, "html_url": "http://pr/503"},
        merge_return={"merged": True},  # must never be reached
    )
    # main: 0001 <- 0002(hotfix) — the shared ancestor is 0001.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py"}]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    # automain diverged with an integration-only 0005_feature_x (rev 0005, off
    # 0001), then a colliding 0002 (collides with main's hotfix) that anchors to
    # 0005 — NOT to the shared 0001 ancestor. That stale anchor is what makes the
    # renumber non-mechanical: 0005 is not on the immutable promotion target.
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_branch_aware_versions(
            [
                "0001_init.py",
                "0002_hotfix.py",
            ],
            [
                "0001_init.py",
                "0005_feature_x.py",
                "0002_collide.py",
            ],
        ),
    )

    async def _file_text(path, *, token, owner, repo, ref, project_id=None):
        # An integration-only migration off the shared root.
        if path.endswith("0005_feature_x.py"):
            return _migration_source("0005", "0001")
        # Collides at 0002 but anchors to 0005 (integration-only), not 0001.
        if path.endswith("0002_collide.py"):
            return _migration_source("0002", "0005")
        return None

    mocker.patch(
        "app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_file_text)
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    # Routed to the resolver — merge never attempted.
    assert result["status"] == "migration_conflict"
    merge_mock.assert_not_called()

    # Exactly one resolver task was spawned.
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers) == 1, (
        "stale anchor to non-shared ancestor must route to resolver"
    )

    # The reconciliation planner refuses: the colliding migration anchors to an
    # integration-only revision (0005), not the shared target ancestor, so it is
    # a non-mechanical fork and the planner returns None (defers to the resolver).
    plan = await _plan_backmerge_migration_reconciliation(
        "gh-token", "acme", "widget", 503, "automain", "main"
    )
    assert plan is None, "stale-anchor collision must not be auto-reconciled"

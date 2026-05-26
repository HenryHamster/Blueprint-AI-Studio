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


async def _make_project(db_session) -> Project:
    """Create a project with an authorized GitHub repo."""
    project = Project(
        name="Backmerge-Test",
        user_id="bm-user",
        github_owner="acme",
        github_repo="widget",
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
    return create_mock, merge_mock


# ===========================================================================
# Success — PR opened + merged
# ===========================================================================


async def test_open_backmerge_pr_success(db_session, mocker):
    """The happy path: open the main -> automain PR and merge it."""
    project = await _make_project(db_session)
    mocker.patch.dict(
        "app.shared_state.runtime_config", {"integration_branch": "automain"}
    )
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
    mocker.patch.dict(
        "app.shared_state.runtime_config", {"integration_branch": "automain"}
    )
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
    assert resolution.title == "Resolve back-merge conflict: main -> automain"
    assert (
        resolution.description
        == "The automated main -> automain back-merge PR #55 hit a merge conflict; resolve it on automain so the next promotion does not conflict."
    )

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
    mocker.patch.dict(
        "app.shared_state.runtime_config", {"integration_branch": "automain"}
    )
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
    """A non-merge that is NOT a conflict (e.g. pending required checks —
    mergeable_state "blocked") stays in the "blocked" path and does NOT spawn
    a resolution task (a conflict resolver can't fix a checks block)."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 78, "html_url": "http://pr/78"},
        merge_return={"merged": False, "message": "required status checks pending"},
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"number": 78, "mergeable_state": "blocked"}),
    )

    result = await hb.open_backmerge_pr(db_session, project.id, periodic=True)
    await db_session.commit()

    assert result["status"] == "blocked"
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [t for t in tasks if (t.first_payload or {}).get("type") == "create"]
    assert len(resolvers) == 0


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
    project = await _make_project(db_session)
    project.integration_branch = "trunk"
    project.promotion_target = "stable"
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
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
    mocker.patch.dict(
        "app.shared_state.runtime_config", {"integration_branch": "automain"}
    )

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
    mocker.patch.dict(
        "app.shared_state.runtime_config", {"integration_branch": "automain"}
    )
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
    project = Project(name="Backmerge-NoCreds", user_id="bm-user")
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


# ===========================================================================
# reconcile_main_into_automain — the periodic reconciler
# ===========================================================================


async def test_reconcile_skipped_when_integration_branch_not_automain(
    db_session, mocker
):
    """The reconciler is a no-op when the LTS branch model is not active —
    integration_branch != "automain" means there is no separate trunk to
    reconcile, so GitHub is never touched."""
    project = await _make_project(db_session)
    mocker.patch.dict("app.shared_state.runtime_config", {"integration_branch": "main"})
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
    mocker.patch.dict(
        "app.shared_state.runtime_config", {"integration_branch": "automain"}
    )
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
    mocker.patch.dict(
        "app.shared_state.runtime_config", {"integration_branch": "automain"}
    )
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
    mocker.patch.dict(
        "app.shared_state.runtime_config", {"integration_branch": "automain"}
    )
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
    project = await _make_project(db_session)
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"integration_branch": "mytrunk", "promotion_target": "mylts"},
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
    project = await _make_project(db_session)
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"integration_branch": "mytrunk", "promotion_target": "mylts"},
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


async def test_reconcile_skipped_when_integration_equals_promotion(db_session, mocker):
    """The reconciler skips when integration_branch == promotion_target — that
    means there is no separate trunk to reconcile (LTS model inactive)."""
    project = await _make_project(db_session)
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"integration_branch": "main", "promotion_target": "main"},
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
    """When project.promotion_target is set, it takes precedence over
    runtime_config so the back-merge goes to the project-specific LTS branch."""
    project = await _make_project(db_session)
    project.integration_branch = "proj-trunk"
    project.promotion_target = "proj-lts"
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # runtime_config points to different branches — project column wins.
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"integration_branch": "automain", "promotion_target": "main"},
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
    project = await _make_project(db_session)
    project.integration_branch = "proj-trunk"
    project.promotion_target = "proj-lts"
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # runtime_config has matching branches (LTS inactive globally) — project column wins.
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"integration_branch": "main", "promotion_target": "main"},
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

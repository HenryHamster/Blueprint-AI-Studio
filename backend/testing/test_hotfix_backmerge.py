"""Tests for the hotfix back-merge (automain LTS Phase 5).

After a hotfix merges to ``main``, ``main`` and ``automain`` diverge.
``hotfix_backmerge.open_backmerge_pr`` opens + merges a ``main -> automain``
back-merge PR to close that gap.  It is the exact inverse of
``release_qualifier.promote()``.

Covers:
  * success — the back-merge PR is opened and merged.
  * empty_diff — ``main == automain`` is a no-op (no merge attempted).
  * conflict (409) — an ``Alert`` is raised AND a high-priority ``plan`` task
    is spawned to resolve the conflict; the plan targets ``automain`` and does
    NOT carry ``hotfix:true``; a second resolution plan is NOT spawned when one
    is already open for the same hotfix.
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
    assert (
        create_mock.call_args.kwargs["title"]
        == "Back-merge: main -> automain"
    )

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
    """A 409 conflict raises an ERROR Alert AND spawns a high-priority plan
    task targeting automain. The plan must NOT carry hotfix:true."""
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

    # Exactly one plan task was spawned.
    tasks = await _tasks_for(db_session, project.id)
    assert len(tasks) == 1
    plan = tasks[0]
    payload = plan.first_payload
    assert payload["type"] == "plan"
    # The resolution plan targets automain — it is a normal automain fix.
    assert payload["base_branch"] == "automain"
    # It must NOT be marked as a hotfix.
    assert "hotfix" not in payload
    # The prompt instructs a git merge origin/main conflict resolution.
    assert "git merge origin/main" in payload["prompt"]
    # High priority (>= 90), so it is dispatched expedited.
    assert plan.priority >= 90
    assert plan.status == TaskStatus.QUEUED
    # Context carries the dedup markers.
    assert plan.context["backmerge_conflict"] is True
    assert plan.context["hotfix_task_id"] == str(hotfix_id)
    assert plan.context["backmerge_pr_number"] == 55

    # A SPAWNED history row was written for the plan task.
    history = (
        (
            await db_session.execute(
                select(TaskHistory).where(TaskHistory.task_id == plan.id)
            )
        )
        .scalars()
        .all()
    )
    assert any(h.action == "SPAWNED" for h in history)


@pytest.mark.asyncio
async def test_open_backmerge_pr_dirty_pr_detected_as_conflict_spawns_plan(
    db_session, mocker
):
    """An ALREADY-dirty/conflicting PR returns 405 "not mergeable" (so
    merge_pull_request does NOT set conflict=True).  open_backmerge_pr must
    still detect it via the PR's mergeable_state == "dirty" and route to the
    conflict-resolution path (Alert + plan) — NOT spin forever in the generic
    "blocked" branch.  Regression for the stuck PR #5277 case."""
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

    # A resolution plan was spawned (the whole point — not a silent "blocked").
    tasks = await _tasks_for(db_session, project.id)
    plans = [t for t in tasks if (t.first_payload or {}).get("type") == "plan"]
    assert len(plans) == 1
    assert plans[0].context["backmerge_conflict"] is True
    assert plans[0].first_payload["base_branch"] == "automain"


@pytest.mark.asyncio
async def test_open_backmerge_pr_blocked_when_not_dirty(db_session, mocker):
    """A non-merge that is NOT a conflict (e.g. pending required checks —
    mergeable_state "blocked") stays in the "blocked" path and does NOT spawn a
    resolution plan (a conflict resolver can't fix a checks block)."""
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
    plans = [t for t in tasks if (t.first_payload or {}).get("type") == "plan"]
    assert len(plans) == 0


@pytest.mark.asyncio
async def test_open_backmerge_pr_conflict_no_duplicate_plan(db_session, mocker):
    """A second back-merge attempt for the SAME hotfix must raise another Alert
    but must NOT spawn a second resolution plan (dedupe guard)."""
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

    # First attempt — spawns the resolution plan.
    r1 = await hb.open_backmerge_pr(
        db_session, project.id, hotfix_task_id=hotfix_id
    )
    await db_session.commit()
    assert r1["status"] == "conflict"

    # Second attempt for the same hotfix — the resolution plan is still open.
    r2 = await hb.open_backmerge_pr(
        db_session, project.id, hotfix_task_id=hotfix_id
    )
    await db_session.commit()
    assert r2["status"] == "conflict"

    # Still exactly ONE plan task — no duplicate spawned.
    tasks = await _tasks_for(db_session, project.id)
    assert len(tasks) == 1

    # But both attempts raised an Alert (the operator sees the recurrence).
    alerts = await _alerts_for(db_session, project.id)
    assert len(alerts) == 2


async def test_open_backmerge_pr_conflict_respawns_after_plan_terminal(
    db_session, mocker
):
    """If the prior resolution plan has terminated (e.g. CANCELLED), a fresh
    conflict for the same hotfix DOES spawn a new resolution plan — a terminal
    task no longer counts as 'open'."""
    project = await _make_project(db_session)
    hotfix_id = uuid.uuid4()
    _patch_github(
        mocker,
        create_return={"number": 80, "html_url": "http://pr/80"},
        merge_return={"merged": False, "conflict": True, "message": "conflict"},
    )

    r1 = await hb.open_backmerge_pr(
        db_session, project.id, hotfix_task_id=hotfix_id
    )
    await db_session.commit()
    assert r1["status"] == "conflict"

    # Mark the spawned plan terminal (as if it was cancelled / failed).
    tasks = await _tasks_for(db_session, project.id)
    assert len(tasks) == 1
    tasks[0].status = TaskStatus.CANCELLED
    db_session.add(tasks[0])
    await db_session.commit()

    # A fresh conflict for the same hotfix now spawns a NEW plan.
    r2 = await hb.open_backmerge_pr(
        db_session, project.id, hotfix_task_id=hotfix_id
    )
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


async def test_open_backmerge_pr_pr_creation_failure_raises_alert(
    db_session, mocker
):
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
    mocker.patch.dict(
        "app.shared_state.runtime_config", {"integration_branch": "main"}
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


async def test_reconcile_delegates_to_backmerge_when_lts_active(
    db_session, mocker
):
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
        result = await hb.open_backmerge_pr(
            db_session, project.id, periodic=True
        )
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
        result = await hb.open_backmerge_pr(
            db_session, project.id, periodic=True
        )
        await db_session.commit()
        assert result["status"] == "conflict"

    # Exactly one Alert and one resolution plan — deduped across all cycles.
    assert len(await _alerts_for(db_session, project.id)) == 1
    tasks = await _tasks_for(db_session, project.id)
    assert len(tasks) == 1
    assert tasks[0].context["backmerge_conflict"] is True

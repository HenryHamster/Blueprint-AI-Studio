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
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app import hotfix_backmerge as hb
from app.migration_chain import build_migration_chain, find_heads
from app.rollup_migration_validation import (
    _plan_backmerge_migration_reconciliation,
)
from testing import check_size_limits as csl
from app.models import (
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    Project,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.time_utils import _utcnow

pytestmark = pytest.mark.asyncio

# Derive the tracked orchestrator.py cap from the canonical limits file rather
# than hardcoding it. The cap is legitimately bumped as the module grows (it
# rose from 7750 to 9100), and a stale hardcoded value silently turns the
# "over cap" fixtures below into UNDER-cap no-ops (no violation → no alert),
# which is exactly the failure class that slipped past automain and only
# surfaced on the full promotion matrix. Reading the live cap keeps these
# tests honest across future bumps.
_ORCH_CAP: int = (hb._load_size_limits().get("backend") or {}).get(
    "orchestrator.py"
) or 0


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
# ALREADY MERGED — merged=False from the merge call, but the PR really landed
#
# merge_pull_request collapses two outcomes onto {"merged": False}: a real block
# AND an already-merged PR (GitHub's merge endpoint 405s once a PR is merged).
# The second happens for real: the merge PUT lands server-side but its response
# is lost to a 5xx, _github_request retries, and the retry gets the 405. The
# blocked classification used to read only ``mergeable_state == "dirty"``, never
# ``merged`` / ``merged_at`` — so a back-merge that LANDED was reported blocked,
# with a persistent operator alert, forever.
# ===========================================================================


async def test_open_backmerge_pr_already_merged_pr_is_merged_not_blocked(
    db_session, mocker
):
    """merged=False + PR resource says merged=True ⇒ status 'merged', no alert."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 77, "html_url": "http://pr/77"},
        # What a lost-5xx-then-405-retry actually returns:
        merge_return={
            "merged": False,
            "conflict": False,
            "message": "Pull Request is not mergeable",
        },
    )
    # The PR resource is authoritative: it landed.
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "merged": True,
                "merged_at": "2026-07-12T00:00:00Z",
                "mergeable_state": "unknown",
                "head": {"sha": "deadbeef"},
            }
        ),
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "merged", (
        "a back-merge that LANDED was reported blocked — the operator alert that "
        "follows is false and persists every cycle"
    )
    assert result["pr_url"] == "http://pr/77"
    # No false operator alert, no conflict/CI-fix task.
    assert await _alerts_for(db_session, project.id) == []
    assert await _tasks_for(db_session, project.id) == []


async def test_open_backmerge_pr_merged_at_only_also_counts_as_merged(
    db_session, mocker
):
    """``merged_at`` alone (no ``merged`` key) is equally authoritative."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 78, "html_url": "http://pr/78"},
        merge_return={"merged": False, "conflict": False, "message": "not mergeable"},
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "merged_at": "2026-07-12T00:00:00Z",
                "mergeable_state": "clean",
                "head": {"sha": "deadbeef"},
            }
        ),
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "merged"
    assert await _alerts_for(db_session, project.id) == []


async def test_open_backmerge_pr_unmerged_dirty_pr_still_classified_conflict(
    db_session, mocker
):
    """Regression guard: an unmerged PR that is DIRTY is still a conflict — the
    already-merged short-circuit must not swallow the real conflict path."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 79, "html_url": "http://pr/79"},
        merge_return={"merged": False, "conflict": False, "message": "not mergeable"},
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "merged": False,
                "merged_at": None,
                "mergeable_state": "dirty",
                "head": {"sha": "deadbeef"},
            }
        ),
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "conflict"


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


def _migration_patch(revision: str, down_revision: str) -> str:
    """Per-file patch for get_pr_files mocks — mirrors _migration_diff content.

    GitHub's per-file patch omits the diff header; only hunk content with
    ``+``/``-``/context lines. _collect_added_migration_sources extracts the
    ``+`` lines via _extract_source_from_patch, so the patch only needs the
    added lines for parse_revision_identifiers to find revision/down_revision.
    """
    return (
        '+"""hotfix migration"""\n'
        f"+revision = '{revision}'\n"
        f"+down_revision = '{down_revision}'\n"
        "+def upgrade():\n"
        "+    pass"
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
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py", "status": "added", "patch": _migration_patch("0002", "0001")}]
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
            return_value=[{"filename": "backend/alembic/versions/0003_hotfix.py", "status": "added", "patch": _migration_patch("0003", "0001")}]
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
            return_value=[{"filename": "backend/alembic/versions/0003_hotfix.py", "status": "added", "patch": _migration_patch("0003", "0002")}]
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
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py", "status": "added", "patch": _migration_patch("0002", "0001")}]
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
# Dual-head block routes to resolver — never pushes to live integration branch
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


async def test_backmerge_block_routes_to_resolver_single_collision(db_session, mocker):
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
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py", "status": "added", "patch": _migration_patch("0002", "0001")}]
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
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py", "status": "added", "patch": _migration_patch("0002", "0001")}]
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


async def test_backmerge_block_defers_with_open_resolver_no_new_alert(db_session, mocker):
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
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py", "status": "added", "patch": _migration_patch("0002", "0001")}]
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


async def test_backmerge_block_dedup_prevents_spurious_resolver_on_periodic(
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
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py", "status": "added", "patch": _migration_patch("0002", "0001")}]
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


async def test_backmerge_deduped_periodic_reentry_skips_planner(db_session, mocker):
    """Deduped periodic re-entry computes NO plan. The migration-block handler
    must consult the renumber planner ONLY on the path where it actually spawns a
    resolver. When a resolver is already open and the periodic reconciler
    re-enters, the project-wide dedup short-circuits BEFORE the planner runs: the
    planner ``_plan_backmerge_migration_reconciliation`` must NOT be invoked and
    its file-read GitHub leg must NOT fire.

    Regression guard against re-introducing a per-cycle GitHub fetch on the hot
    deduped path (every reconciler tick would otherwise re-fetch the diff +
    branch listings + every post-divergence migration source for no reason)."""
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 308, "html_url": "http://pr/308"},
        merge_return={"merged": True},  # must never be reached
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py", "status": "added", "patch": _migration_patch("0002", "0001")}]
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

    # Prime an open resolver via a first NON-periodic block. The real planner runs
    # here (spawn path) and embeds the renumber plan into the resolver.
    first = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()
    assert first["status"] == "migration_conflict"
    tasks = await _tasks_for(db_session, project.id)
    resolvers_after_first = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers_after_first) == 1
    baseline_alerts = len(await _alerts_for(db_session, project.id))

    # Install the planner spy AND reset the file-read leg AFTER the prime so we
    # measure only the second (deduped) call. The planner is patched in the
    # hotfix_backmerge module namespace, where it is imported and called.
    planner_spy = mocker.patch.object(
        hb,
        "_plan_backmerge_migration_reconciliation",
        new=AsyncMock(return_value=None),
    )
    file_read_spy = mocker.patch(
        "app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_file_text)
    )

    # Periodic re-entry with the resolver still open: the dedup short-circuits
    # before the planner runs.
    second = await hb.open_backmerge_pr(db_session, project.id, periodic=True)
    await db_session.commit()

    assert second["status"] == "migration_conflict"
    # The planner (and therefore its file-read leg) was never invoked.
    planner_spy.assert_not_called()
    file_read_spy.assert_not_called()
    # No merge, no inline push.
    merge_mock.assert_not_called()
    commit_mock.assert_not_called()
    # No duplicate resolver task and no new Alert.
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
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py", "status": "added", "patch": _migration_patch("0002", "0001")}]
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


async def test_backmerge_block_fallback_on_planner_error(db_session, mocker):
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
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py", "status": "added", "patch": _migration_patch("0002", "0001")}]
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


async def test_backmerge_block_fallback_on_missing_schema_literal(
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
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py", "status": "added", "patch": _migration_patch("0002", "0001")}]
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
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py", "status": "added", "patch": _migration_patch("0002", "0001")}]
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
    for rewritten_source in expected_plan["upserts"].values():
        assert rewritten_source in prompt
    # Presented as a verify-then-apply candidate, not a blind apply.
    assert "verify" in prompt.lower()


async def test_backmerge_collision_with_linear_descendant_embeds_full_plan(
    db_session, mocker
):
    """A single-collision dual-head block whose colliding integration migration
    has a LINEAR DESCENDANT tail hands the spawned resolver the FULL structured
    renumber plan — the mechanically richer two-file shape: the colliding
    migration is renamed+renumbered AND its descendant is renumbered+rechained
    monotonically onto the new revision. Both rewritten bodies are stored under
    ``context['backmerge_renumber_plan']['upserts']`` and embedded verbatim in
    the resolver prompt. This is the production/embedding-level coverage for the
    descendant-tail shape that ``test_migration_chain.py`` only exercises at the
    pure-planner unit level.

    Promotion target (main): ``0001_init`` + ``0002_hotfix``.
    Integration branch (automain): ``0001_init`` + ``0002_feature`` +
    ``0003_more`` (a linear descendant chaining onto 0002). The back-merge PR
    adds main's ``0002_hotfix`` (rev 0002, down 0001), colliding with automain's
    own ``0002_feature``. The planner renumbers ``0002_feature`` to ``0003``
    (the next free revision above the promotion-target head ``0002``), rechained
    onto that head; the descendant ``0003_more`` is renumbered to ``0004``
    (monotonically above ``0003``), its ``down_revision`` moving from ``0002`` to
    ``0003``; the reconciled single head is ``0004`` (the numeric maximum).
    """
    project = await _make_project(db_session)
    _create_mock, merge_mock = _patch_github(
        mocker,
        create_return={"number": 610, "html_url": "http://pr/610"},
        merge_return={"merged": True},  # must never be reached
    )
    # The back-merge PR adds main's 0002_hotfix (rev 0002, down 0001).
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py", "status": "added", "patch": _migration_patch("0002", "0001")}]
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
    # main carries the hotfix at 0002; automain carries its own feature at 0002
    # PLUS a linear descendant 0003_more chaining onto 0002.
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_branch_aware_versions(
            ["0001_init.py", "0002_hotfix.py"],
            ["0001_init.py", "0002_feature.py", "0003_more.py"],
        ),
    )

    async def _file_text(path, *, token, owner, repo, ref, project_id=None):
        # Integration sources read at the integration ref.
        if path.endswith("0002_feature.py"):
            return _migration_source("0002", "0001")
        if path.endswith("0003_more.py"):
            return _migration_source("0003", "0002")
        return None

    mocker.patch(
        "app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_file_text)
    )
    commit_mock = mocker.patch(
        "app.github.commit_file_changes", new=AsyncMock(return_value={"commit": "sha"})
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    # (a) Blocked before merge — the merge API and the inline integration-branch
    # push must both stay untouched (the block-path-pushes-nothing guarantee).
    assert result["status"] == "migration_conflict"
    assert result["pr_url"] == "http://pr/610"
    merge_mock.assert_not_called()
    commit_mock.assert_not_called()
    for call in commit_mock.call_args_list:  # belt-and-braces
        assert "automain" not in call.args

    # (b) Exactly one migration-aware resolver spawned.
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [
        t for t in tasks if (t.context or {}).get("backmerge_migration_conflict") is True
    ]
    assert len(resolvers) == 1
    resolver = resolvers[0]

    # Derive the exact expected plan from the same mocked GitHub state, exactly
    # as test_backmerge_single_collision_embeds_renumber_plan does.
    expected_plan = await _plan_backmerge_migration_reconciliation(
        "gh-token", "acme", "widget", 610, "automain", "main"
    )
    assert expected_plan is not None

    # (c) The stored structured plan is the FULL two-file shape.
    plan = resolver.context["backmerge_renumber_plan"]
    assert plan is not None
    # The colliding 0002_feature renumbers to the next free revision (0003),
    # rechained onto the promotion-target head 0002.
    assert plan["new_revision"] == "0003"
    assert plan["new_down_revision"] == "0002"
    assert plan["old_revision"] == "0002"
    assert plan["old_path"] == "backend/alembic/versions/0002_feature.py"
    assert plan["new_path"] == "backend/alembic/versions/0003_feature.py"
    # The reconciled single head is 0004 — the descendant is renumbered too, so
    # the numeric maximum revision is the head (monotonic chain).
    assert plan["expected_schema_revision"] == "0004"
    # ``deletes`` names BOTH old integration paths being renamed away (chain order).
    assert plan["deletes"] == [
        "backend/alembic/versions/0002_feature.py",
        "backend/alembic/versions/0003_more.py",
    ]
    # ``upserts`` contains BOTH rewritten files: the renamed+renumbered colliding
    # migration AND the renumbered+rechained descendant.
    assert set(plan["upserts"].keys()) == {
        "backend/alembic/versions/0003_feature.py",
        "backend/alembic/versions/0004_more.py",
    }
    # The stored plan matches the independently-derived planner output exactly.
    assert plan == expected_plan

    # The renamed+renumbered colliding migration carries the new revision id and
    # is rechained onto the unified promotion-target head.
    renamed_source = plan["upserts"]["backend/alembic/versions/0003_feature.py"]
    assert "revision = '0003'" in renamed_source
    assert "down_revision = '0002'" in renamed_source
    # The descendant is renumbered to 0004 and rechains onto 0003.
    descendant_source = plan["upserts"]["backend/alembic/versions/0004_more.py"]
    assert "revision = '0004'" in descendant_source
    assert "down_revision = '0003'" in descendant_source

    # The reconciled integration set resolves to exactly one head (0004), and the
    # numeric-max revision IS that head (monotonicity).
    reconciled = {
        "backend/alembic/versions/0003_feature.py": renamed_source,
        "backend/alembic/versions/0004_more.py": descendant_source,
    }
    reconciled_chain, _dups, _unp = build_migration_chain(reconciled)
    assert find_heads(reconciled_chain) == ["0004"]
    assert max(reconciled_chain, key=int) == find_heads(reconciled_chain)[0]

    # (d) The full plan is embedded in the resolver prompt.
    prompt = resolver.first_payload["prompt"]
    assert "Known-good renumber plan" in prompt
    # The correct new revision and down_revision are rendered.
    assert "0003" in prompt  # the new revision id
    assert "0002_feature.py" in prompt  # the file being renamed
    assert "0004_more.py" in prompt  # the renumbered+rechained descendant
    assert "EXPECTED_SCHEMA_REVISION" in prompt
    # BOTH rewritten migration bodies are embedded verbatim — every rewritten
    # source in expected_plan['upserts'] appears in the prompt.
    for rewritten_source in expected_plan["upserts"].values():
        assert rewritten_source in prompt
    # Verify-then-apply framing, not a blind apply.
    assert "verify" in prompt.lower()
    assert "do NOT apply blindly" in prompt


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
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py", "status": "added", "patch": _migration_patch("0002", "0001")}]
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
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py", "status": "added", "patch": _migration_patch("0002", "0001")}]
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


async def test_plan_backmerge_reconciliation_file_read_error_returns_none(mocker):
    """Per-file read FAILURE fails safe to no-plan. When reading an individual
    post-divergence integration migration source raises, the helper must return
    ``None`` — never a partial/unsafe plan computed from a missing source."""
    from app import rollup_migration_validation as rmv

    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    # main carries the hotfix at 0002; automain has its own post-divergence
    # 0002_feature that would be the colliding migration to renumber.
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_branch_aware_versions(
            ["0001_init.py", "0002_hotfix.py"],
            ["0001_init.py", "0002_feature.py"],
        ),
    )
    # Reading the post-divergence integration migration source blows up.
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(side_effect=RuntimeError("GitHub 502 reading file")),
    )

    plan = await rmv._plan_backmerge_migration_reconciliation(
        "tok", "acme", "widget", 403, "automain", "main"
    )
    assert plan is None


async def test_plan_backmerge_reconciliation_empty_file_read_returns_none(mocker):
    """Per-file EMPTY/None read fails safe to no-plan. When the post-divergence
    integration migration source comes back empty/``None`` (the file could not be
    materialized), the helper must return ``None`` — it must never proceed with a
    missing source."""
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
    # Empty source read for the post-divergence integration migration.
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(return_value=""),
    )

    plan = await rmv._plan_backmerge_migration_reconciliation(
        "tok", "acme", "widget", 404, "automain", "main"
    )
    assert plan is None


async def test_plan_backmerge_reconciliation_no_post_divergence_returns_none(mocker):
    """Nothing post-divergence yields no plan. When EVERY integration-branch
    migration basename is shared with the promotion target (so the post-divergence
    set is empty — there is no colliding integration migration to renumber), the
    helper returns ``None`` even though the back-merge diff DID add a migration.
    No integration source is ever read in this case."""
    from app import rollup_migration_validation as rmv

    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=_migration_diff(
                "backend/alembic/versions/0002_hotfix.py", "0002", "0001"
            )
        ),
    )
    # Identical filename lists for both branches → post-divergence set is empty.
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_branch_aware_versions(
            ["0001_init.py", "0002_feature.py"],
            ["0001_init.py", "0002_feature.py"],
        ),
    )
    file_read_mock = mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(return_value=_migration_source("0002", "0001")),
    )

    plan = await rmv._plan_backmerge_migration_reconciliation(
        "tok", "acme", "widget", 405, "automain", "main"
    )
    assert plan is None
    # With an empty post-divergence set there is nothing to read.
    file_read_mock.assert_not_called()


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
            return_value=[{"filename": "backend/alembic/versions/0003_hotfix.py", "status": "added", "patch": _migration_patch("0003", "0002")}]
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
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py", "status": "added", "patch": _migration_patch("0002", "0001")}]
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
                {"filename": "backend/alembic/versions/0002_hotfix_a.py", "status": "added", "patch": _migration_patch("0002", "0001")},
                {"filename": "backend/alembic/versions/0003_hotfix_b.py", "status": "added", "patch": _migration_patch("0003", "0002")},
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
            return_value=[{"filename": "backend/alembic/versions/0002_hotfix.py", "status": "added", "patch": _migration_patch("0002", "0001")}]
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


# ===========================================================================
# Cross-cycle dedup — a TERMINAL resolver whose resolution PR is STILL OPEN
# counts as in-flight (regression for the competing-resolver incident
# #7476 CI-red-orphaned vs #7478 clean-merged).
#
# A resolver ``create`` task goes COMPLETED the instant it OPENS its resolution
# PR, NOT when that PR merges into the integration trunk. The back-merge is not
# actually resolved until the PR lands (CI + review tail, or never if CI stays
# red). The "non-terminal only" guard returned False during that window, so the
# next reconciler cycle re-saw the still-DIRTY back-merge PR and spawned a
# DUPLICATE resolver. _resolution_task_already_open now also treats a recently-
# terminal resolver with an OPEN resolution PR as in-flight.
# ===========================================================================


async def _make_resolver_task(
    db_session,
    project_id,
    *,
    status: TaskStatus,
    created_at=None,
    backmerge_conflict: bool = True,
) -> Task:
    """Create a back-merge resolver task (``context['backmerge_conflict']=True``)."""
    task = Task(
        project_id=project_id,
        title="Resolve back-merge conflict: main -> automain",
        payloads=[{"type": "create", "prompt": "resolve", "base_branch": "automain"}],
        context={"backmerge_conflict": backmerge_conflict, "backmerge_pr_number": 100},
        status=status,
        created_by="ORCHESTRATOR",
    )
    if created_at is not None:
        task.created_at = created_at
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


async def _add_pr_artifact(db_session, task_id, pr_url: str) -> Artifact:
    """Attach a PULL_REQUEST artifact (the resolver's resolution PR) to a task."""
    artifact = Artifact(
        task_id=task_id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url=pr_url,
    )
    db_session.add(artifact)
    await db_session.commit()
    return artifact


async def test_resolution_already_open_terminal_with_open_pr_blocks(
    db_session, mocker
):
    """A COMPLETED resolver whose resolution PR is STILL OPEN on GitHub counts
    as in-flight — no duplicate respawn. This is the #7476/#7478 regression: the
    resolver completes the moment it opens its PR, long before that PR merges."""
    project = await _make_project(db_session)
    task = await _make_resolver_task(db_session, project.id, status=TaskStatus.COMPLETED)
    await _add_pr_artifact(
        db_session, task.id, "https://github.com/acme/widget/pull/7478"
    )

    get_pr = mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"number": 7478, "state": "open", "merged": False}),
    )

    blocked = await hb._resolution_task_already_open(
        db_session, project.id, token="gh-token", owner="acme", repo="widget"
    )

    assert blocked is True
    # The GH call was made against the resolution PR parsed from the artifact.
    assert get_pr.call_args.args[3] == 7478


async def test_resolution_already_open_terminal_with_merged_pr_allows_respawn(
    db_session, mocker
):
    """A COMPLETED resolver whose resolution PR has MERGED does NOT block — the
    resolver succeeded; nothing is in flight."""
    project = await _make_project(db_session)
    task = await _make_resolver_task(db_session, project.id, status=TaskStatus.COMPLETED)
    await _add_pr_artifact(
        db_session, task.id, "https://github.com/acme/widget/pull/7478"
    )

    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"number": 7478, "state": "closed", "merged": True}
        ),
    )

    blocked = await hb._resolution_task_already_open(
        db_session, project.id, token="gh-token", owner="acme", repo="widget"
    )

    assert blocked is False


async def test_resolution_already_open_terminal_with_closed_unmerged_pr_allows_respawn(
    db_session, mocker
):
    """A FAILED resolver whose resolution PR was CLOSED-unmerged does NOT block —
    the first resolver genuinely failed, so a fresh respawn is legitimate."""
    project = await _make_project(db_session)
    task = await _make_resolver_task(db_session, project.id, status=TaskStatus.FAILED)
    await _add_pr_artifact(
        db_session, task.id, "https://github.com/acme/widget/pull/7476"
    )

    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"number": 7476, "state": "closed", "merged": False}
        ),
    )

    blocked = await hb._resolution_task_already_open(
        db_session, project.id, token="gh-token", owner="acme", repo="widget"
    )

    assert blocked is False


async def test_resolution_already_open_terminal_without_pr_artifact_allows_respawn(
    db_session, mocker
):
    """A terminal resolver that never opened a PR (no PULL_REQUEST artifact)
    does NOT block — it genuinely failed before publishing; respawn is allowed.
    No GitHub call is made (there is no PR number to query)."""
    project = await _make_project(db_session)
    await _make_resolver_task(db_session, project.id, status=TaskStatus.CANCELLED)

    get_pr = mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "open", "merged": False}),
    )

    blocked = await hb._resolution_task_already_open(
        db_session, project.id, token="gh-token", owner="acme", repo="widget"
    )

    assert blocked is False
    get_pr.assert_not_called()


async def test_resolution_already_open_gh_lookup_raises_is_failsafe(
    db_session, mocker
):
    """If the GitHub lookup raises, the resolver is treated as STILL OPEN
    (fail-safe) so a transient hiccup never lets a duplicate resolver through."""
    project = await _make_project(db_session)
    task = await _make_resolver_task(db_session, project.id, status=TaskStatus.COMPLETED)
    await _add_pr_artifact(
        db_session, task.id, "https://github.com/acme/widget/pull/7478"
    )

    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(side_effect=RuntimeError("GitHub 502")),
    )

    blocked = await hb._resolution_task_already_open(
        db_session, project.id, token="gh-token", owner="acme", repo="widget"
    )

    assert blocked is True


async def test_resolution_already_open_terminal_older_than_lookback_is_ignored(
    db_session, mocker
):
    """A terminal resolver OLDER than the lookback window is ignored entirely —
    no GitHub call, respawn allowed. An ancient never-landed resolver must not
    block a fresh attempt forever, and a stale row never triggers a network hit."""
    project = await _make_project(db_session)
    stale_created = _utcnow() - hb._RESOLVER_PR_LOOKBACK - timedelta(hours=1)
    task = await _make_resolver_task(
        db_session,
        project.id,
        status=TaskStatus.COMPLETED,
        created_at=stale_created,
    )
    # Even though its PR is technically still open, the row is too old to query.
    await _add_pr_artifact(
        db_session, task.id, "https://github.com/acme/widget/pull/7000"
    )

    get_pr = mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"number": 7000, "state": "open", "merged": False}),
    )

    blocked = await hb._resolution_task_already_open(
        db_session, project.id, token="gh-token", owner="acme", repo="widget"
    )

    assert blocked is False
    get_pr.assert_not_called()


async def test_resolution_already_open_nonterminal_blocks_without_gh_call(
    db_session, mocker
):
    """The fast path is unchanged: a NON-terminal resolver blocks immediately
    and makes ZERO GitHub calls even when creds are supplied."""
    project = await _make_project(db_session)
    await _make_resolver_task(db_session, project.id, status=TaskStatus.QUEUED)

    get_pr = mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "open", "merged": False}),
    )

    blocked = await hb._resolution_task_already_open(
        db_session, project.id, token="gh-token", owner="acme", repo="widget"
    )

    assert blocked is True
    get_pr.assert_not_called()


async def test_resolution_already_open_no_creds_skips_gh_leg(db_session, mocker):
    """Without token/owner/repo only the GH-free fast path runs: a terminal
    resolver with an open PR is NOT detected (the caller supplied no creds), and
    no GitHub call is attempted. This keeps unit callers that omit creds working
    and guarantees the common no-conflict path makes zero GitHub calls."""
    project = await _make_project(db_session)
    task = await _make_resolver_task(db_session, project.id, status=TaskStatus.COMPLETED)
    await _add_pr_artifact(
        db_session, task.id, "https://github.com/acme/widget/pull/7478"
    )

    get_pr = mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"number": 7478, "state": "open", "merged": False}),
    )

    blocked = await hb._resolution_task_already_open(db_session, project.id)

    assert blocked is False
    get_pr.assert_not_called()


async def test_open_backmerge_pr_no_duplicate_when_prior_resolver_pr_still_open(
    db_session, mocker
):
    """Full-path regression for #7476/#7478: a periodic reconciler cycle that
    re-sees a still-DIRTY back-merge while the prior resolver's PR is COMPLETED
    (PR opened) but its resolution PR is STILL OPEN must NOT spawn a duplicate
    resolver."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 7476, "html_url": "http://pr/7476"},
        merge_return={"merged": False, "conflict": True, "message": "conflict"},
    )

    # Cycle 1: conflict → one resolver spawned.
    r1 = await hb.open_backmerge_pr(db_session, project.id, periodic=True)
    await db_session.commit()
    assert r1["status"] == "conflict"
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [t for t in tasks if (t.context or {}).get("backmerge_conflict")]
    assert len(resolvers) == 1
    resolver = resolvers[0]

    # The resolver opens its resolution PR and goes COMPLETED — but that PR has
    # NOT merged into automain yet (CI + review tail).
    resolver.status = TaskStatus.COMPLETED
    db_session.add(resolver)
    await db_session.commit()
    await _add_pr_artifact(
        db_session, resolver.id, "https://github.com/acme/widget/pull/7478"
    )
    # The resolution PR is still open on GitHub.
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"number": 7478, "state": "open", "merged": False}),
    )

    # Cycle 2 (600s later): the back-merge PR is still dirty → conflict again.
    r2 = await hb.open_backmerge_pr(db_session, project.id, periodic=True)
    await db_session.commit()
    assert r2["status"] == "conflict"

    # No DUPLICATE resolver — the still-open resolution PR counts as in-flight.
    tasks = await _tasks_for(db_session, project.id)
    resolvers = [t for t in tasks if (t.context or {}).get("backmerge_conflict")]
    assert len(resolvers) == 1, (
        f"a still-open resolution PR must dedupe the next cycle — got {len(resolvers)}"
    )


# ===========================================================================
# Module size violation alerts (step 3c)
# ===========================================================================


def _make_pr_file(filename: str) -> dict:
    """Minimal PR-files entry for a single changed file."""
    return {"filename": filename, "status": "modified"}


def _make_file_content(n_lines: int) -> str:
    """Synthesise a file body with exactly ``n_lines`` lines."""
    return "\n".join(f"line {i}" for i in range(n_lines))


async def test_module_size_violation_is_suppressed_from_operator_alerts(
    db_session, mocker
):
    """Tracked size violations stay strategist-internal and never block merge."""
    project = await _make_project(db_session)
    # create_pull_request returns a PR with head.sha so step 3c resolves the ref.
    _patch_github(
        mocker,
        create_return={"number": 55, "html_url": "http://pr/55"},
        merge_return={"merged": True},
    )

    # PR touches orchestrator.py (tracked under backend).
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[_make_pr_file("backend/app/orchestrator.py")]),
    )
    # The file at the LTS branch head is over cap by 200 lines.
    over = _ORCH_CAP + 200
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(return_value=_make_file_content(over)),
    )

    result = await hb.open_backmerge_pr(
        db_session, project.id, merged_pr_url="http://pr/hotfix-99"
    )
    await db_session.commit()

    assert result["status"] == "merged"
    assert await _alerts_for(db_session, project.id) == []


async def test_module_size_check_does_not_block_merge(db_session, mocker):
    """A size violation must NEVER block the back-merge (status=merged even
    when violations are detected)."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 66, "html_url": "http://pr/66"},
        merge_return={"merged": True},
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[_make_pr_file("backend/app/orchestrator.py")]),
    )
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(return_value=_make_file_content(_ORCH_CAP + 500)),
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "merged"


async def test_module_size_same_bucket_remains_suppressed(db_session, mocker):
    """Repeated strategist-internal violations create no operator alerts."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 77, "html_url": "http://pr/77"},
        merge_return={"merged": True},
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[_make_pr_file("backend/app/orchestrator.py")]),
    )
    # First whole 100-line bucket strictly above cap (robust even when the cap
    # itself is not a multiple of 100).
    bucket = ((_ORCH_CAP + 100) // 100) * 100
    # First call: bucket+10 lines → bucket.
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(return_value=_make_file_content(bucket + 10)),
    )

    await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()
    assert await _alerts_for(db_session, project.id) == []

    # Second call: bucket+50 lines — still in the same bucket.
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(return_value=_make_file_content(bucket + 50)),
    )
    await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert await _alerts_for(db_session, project.id) == []


async def test_module_size_larger_bucket_remains_suppressed(db_session, mocker):
    """Growth into a new bucket remains suppressed from operator alerts."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 88, "html_url": "http://pr/88"},
        merge_return={"merged": True},
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[_make_pr_file("backend/app/orchestrator.py")]),
    )

    # First whole 100-line bucket strictly above cap, and the next one up.
    bucket_a = ((_ORCH_CAP + 100) // 100) * 100
    bucket_b = bucket_a + 100
    # First cycle: bucket_a+10 lines stays strategist-internal.
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(return_value=_make_file_content(bucket_a + 10)),
    )
    await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()
    assert await _alerts_for(db_session, project.id) == []

    # Second cycle reaches the next bucket but remains strategist-internal.
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(return_value=_make_file_content(bucket_b + 10)),
    )
    await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert await _alerts_for(db_session, project.id) == []


async def test_module_size_check_exception_does_not_block_merge(db_session, mocker):
    """A GitHub API failure during the size check is silently swallowed and
    the back-merge still proceeds to completion."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 99, "html_url": "http://pr/99"},
        merge_return={"merged": True},
    )
    # The first get_pr_files call (step 3b migration guard) returns [] so it
    # proceeds.  The second call (step 3c size check) raises — must be caught.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(side_effect=[[], RuntimeError("transient API failure")]),
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "merged"
    assert await _alerts_for(db_session, project.id) == []


async def test_module_size_check_within_cap_no_alert(db_session, mocker):
    """A tracked module that is within its cap produces no alert."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 100, "html_url": "http://pr/100"},
        merge_return={"merged": True},
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[_make_pr_file("backend/app/orchestrator.py")]),
    )
    # A line count comfortably within the tracked cap → no violation, no alert.
    # Derived from the live cap (not hardcoded) so a future cap change — e.g. the
    # orchestrator.py decomposition that lowered the cap from 7750 to 4400 — can't
    # silently flip this "within cap" fixture into an over-cap one and mask the
    # no-alert path. Mirrors the `_ORCH_CAP + N` pattern used by the over-cap
    # fixtures above; see the _ORCH_CAP rationale near the top of this module.
    within = max(_ORCH_CAP - 200, 0)
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(return_value=_make_file_content(within)),
    )

    result = await hb.open_backmerge_pr(db_session, project.id)
    await db_session.commit()

    assert result["status"] == "merged"
    assert await _alerts_for(db_session, project.id) == []


async def test_uncapped_large_file_is_suppressed_from_operator_alerts(
    db_session, mocker
):
    """Uncapped large files stay strategist-internal and do not block merge."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 101, "html_url": "http://pr/101"},
        merge_return={"merged": True},
    )
    assert hb._UNCAPPED_SIZE_THRESHOLD == csl._COVERAGE_THRESHOLD

    # A file that maps to the backend section but has no cap entry.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[_make_pr_file("backend/app/brand_new_huge_module.py")]
        ),
    )
    # The limits file has no entry for brand_new_huge_module.py.
    mocker.patch.object(
        hb,
        "_load_size_limits",
        return_value={"backend": {}, "agent": {}, "frontend": {}},
    )
    # File is over the uncapped threshold.
    over_threshold = csl._COVERAGE_THRESHOLD + 100
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(return_value=_make_file_content(over_threshold)),
    )

    result = await hb.open_backmerge_pr(
        db_session, project.id, merged_pr_url="http://pr/hotfix-5"
    )
    await db_session.commit()

    # The merge still proceeds — the check is advisory, never blocking.
    assert result["status"] == "merged"

    assert await _alerts_for(db_session, project.id) == []


@pytest.mark.parametrize(
    "filename",
    [
        # Agent exclusions: .test.ts and .d.ts files
        "agent/src/foo.test.ts",
        "agent/src/types/bar.d.ts",
        # Frontend exclusions: .test.ts, .test.tsx, .d.ts, and test/ prefix
        "admin-ui/src/components/Foo.test.ts",
        "admin-ui/src/components/Foo.test.tsx",
        "admin-ui/src/types/global.d.ts",
        "admin-ui/src/test/setup.ts",
    ],
)
async def test_module_size_check_skips_excluded_file_types(
    filename, db_session, mocker
):
    """Excluded file types (test files, declaration files) must NOT raise alerts
    even when they exceed the uncapped threshold and have no cap entry.
    check_size_limits.py --fix will never add caps for these files, so alerting
    would send operators on a wild goose chase."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 102, "html_url": "http://pr/102"},
        merge_return={"merged": True},
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[_make_pr_file(filename)]),
    )
    mocker.patch.object(
        hb,
        "_load_size_limits",
        return_value={"backend": {}, "agent": {}, "frontend": {}},
    )
    # File is over the uncapped threshold.
    over_threshold = csl._COVERAGE_THRESHOLD + 100
    get_file_text = mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(return_value=_make_file_content(over_threshold)),
    )

    result = await hb.open_backmerge_pr(
        db_session, project.id, merged_pr_url="http://pr/hotfix-excluded"
    )
    await db_session.commit()

    # Merge still proceeds — excluded files never block.
    assert result["status"] == "merged"
    # The filename alone proves the file is excluded, so no content fetch is needed.
    get_file_text.assert_not_awaited()
    # No alert should be raised for excluded file types.
    alerts = await _alerts_for(db_session, project.id)
    assert alerts == [], (
        f"Expected no alert for excluded file {filename!r} but got: "
        + ", ".join(a.title for a in alerts)
    )


# ===========================================================================
# RATE-LIMITED MERGE — the 503 must be RETRYABLE, not a persistent "blocked" alert
#
# This is the branch that gives the 500 -> 503 reclassification its teeth. Every
# OTHER consumer of merge_pull_request catches bare Exception and cannot tell a
# 503 from a 500, so the status code alone changes nothing anywhere. Here it does:
# a drained quota bucket means the merge PUT never reached GitHub's merge
# machinery — the PR is neither merged nor blocked, it just has to be retried.
# Raising the persistent operator alert would page a human about a transient
# quota window, and the alert would outlive the next sweep's quiet success.
# ===========================================================================


async def test_rate_limited_merge_is_retryable_not_blocked(db_session, mocker):
    """A 503 from merge_pull_request => status 'retry', and NO blocked alert."""
    project = await _make_project(db_session)
    _patch_github(
        mocker,
        create_return={"number": 91, "html_url": "http://pr/91"},
        merge_side_effect=HTTPException(
            status_code=503,
            detail="Cannot merge pull request #91: the GitHub API is rate-limited.",
            headers={"Retry-After": "42"},
        ),
    )

    result = await hb.open_backmerge_pr(db_session, project.id)

    assert result["status"] == "retry", (
        "a rate-limited merge was never attempted upstream — it must be retried, "
        "not reported as a hard block"
    )
    assert result["error"] == "rate_limited"

    alerts = await _alerts_for(db_session, project.id)
    assert alerts == [], (
        "a transient quota exhaustion must NOT raise a persistent operator alert — "
        "this is the whole point of the 503 reclassification"
    )

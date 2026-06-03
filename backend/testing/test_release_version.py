"""Tests for release_version.py — the ReleaseVersion table helpers
(automain LTS Phase 2).

Focus areas:
  * count_merged_since — the landed-on-integration filter MUST be applied on
    both dialects: a COMPLETED task without a landed signal is never counted;
    a merge-equivalent non-landing is excluded; a non-COMPLETED task with the
    flag is never counted; the ``since`` lower bound is exclusive-of-older rows.
  * open_version / current_open_version — insert + single-open-row read,
    including the multiple-open-rows anomaly path.
  * set_version_phase — valid transitions, idempotent no-op, invalid phase
    raises, reasoning truncation.
  * to_phase_block — compact payload shape.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import release_version as rv
from app.models import Project, ReleaseVersion, Task, TaskStatus
from app.time_utils import _utcnow


# ===========================================================================
# Shared fixture helpers
# ===========================================================================


async def _make_project(db_session, name: str = "RV-Test") -> Project:
    project = Project(name=name, user_id="rv-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _make_task(
    db_session,
    project: Project,
    *,
    status: TaskStatus,
    context: dict,
    updated_at,
) -> Task:
    """Create a Task with an explicit ``updated_at`` and ``context``.

    ``updated_at`` is set at construction time; ``onupdate`` only fires on a
    subsequent UPDATE so the insert value is preserved — this lets a test
    place a task before/after the ``since`` boundary deterministically.
    """
    task = Task(
        project_id=project.id,
        title="merge-test-task",
        payloads=[{"type": "create", "prompt": "x"}],
        status=status,
        context=context,
        updated_at=updated_at,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


# ===========================================================================
# count_merged_since — the landed signal filter must be applied
# ===========================================================================


@pytest.mark.asyncio
async def test_count_merged_since_counts_merged_completed_task(db_session):
    """A COMPLETED task with context['pr_merged']=True updated after `since`
    is counted."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=1)

    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        updated_at=_utcnow(),
    )

    count = await rv.count_merged_since(db_session, project.id, since)
    assert count == 1


@pytest.mark.asyncio
async def test_count_merged_since_excludes_completed_without_pr_merged(db_session):
    """REGRESSION GUARD: a COMPLETED task that lacks the pr_merged flag must
    NOT be counted.  The old SQLite/except-fallback path dropped this filter
    and over-counted every COMPLETED task."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=1)

    # COMPLETED but never merged — no pr_merged key at all.
    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context={"some_other_key": "value"},
        updated_at=_utcnow(),
    )
    # COMPLETED with pr_merged explicitly False.
    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": False},
        updated_at=_utcnow(),
    )

    count = await rv.count_merged_since(db_session, project.id, since)
    assert count == 0, "pr_merged filter must exclude non-merged COMPLETED tasks"


@pytest.mark.asyncio
async def test_count_merged_since_mixed_only_counts_merged(db_session):
    """With a mix of merged and non-merged COMPLETED tasks, only the ones
    flagged pr_merged=True are counted."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=1)

    for _ in range(3):
        await _make_task(
            db_session,
            project,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            updated_at=_utcnow(),
        )
    for _ in range(2):
        await _make_task(
            db_session,
            project,
            status=TaskStatus.COMPLETED,
            context={},
            updated_at=_utcnow(),
        )

    count = await rv.count_merged_since(db_session, project.id, since)
    assert count == 3


@pytest.mark.asyncio
async def test_count_merged_since_excludes_non_merge_unblocks(db_session):
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=1)

    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": True},
        updated_at=_utcnow(),
    )
    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
        updated_at=_utcnow(),
    )

    count = await rv.count_merged_since(db_session, project.id, since)
    assert count == 1


@pytest.mark.asyncio
async def test_count_merged_since_excludes_non_completed_status(db_session):
    """A task with context['pr_merged']=True but a non-COMPLETED status is
    not counted — the status filter still applies alongside the flag."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=1)

    await _make_task(
        db_session,
        project,
        status=TaskStatus.IN_PROGRESS,
        context={"pr_merged": True},
        updated_at=_utcnow(),
    )
    await _make_task(
        db_session,
        project,
        status=TaskStatus.FAILED,
        context={"pr_merged": True},
        updated_at=_utcnow(),
    )

    count = await rv.count_merged_since(db_session, project.id, since)
    assert count == 0


@pytest.mark.asyncio
async def test_count_merged_since_respects_since_lower_bound(db_session):
    """A merged task updated BEFORE `since` is excluded; the same task type
    updated after is included."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=1)

    # Updated 2h ago — before `since`.
    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        updated_at=_utcnow() - timedelta(hours=2),
    )
    # Updated now — after `since`.
    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        updated_at=_utcnow(),
    )

    count = await rv.count_merged_since(db_session, project.id, since)
    assert count == 1


@pytest.mark.asyncio
async def test_count_merged_since_scoped_to_project(db_session):
    """A merged task belonging to a different project is not counted."""
    project_a = await _make_project(db_session, name="RV-A")
    project_b = await _make_project(db_session, name="RV-B")
    since = _utcnow() - timedelta(hours=1)

    await _make_task(
        db_session,
        project_a,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        updated_at=_utcnow(),
    )
    await _make_task(
        db_session,
        project_b,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        updated_at=_utcnow(),
    )

    assert await rv.count_merged_since(db_session, project_a.id, since) == 1
    assert await rv.count_merged_since(db_session, project_b.id, since) == 1


@pytest.mark.asyncio
async def test_count_merged_since_zero_when_no_tasks(db_session):
    """An empty project returns 0, not None."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=1)
    count = await rv.count_merged_since(db_session, project.id, since)
    assert count == 0
    assert isinstance(count, int)


# ===========================================================================
# open_version / current_open_version
# ===========================================================================


@pytest.mark.asyncio
async def test_open_version_inserts_open_build_row(db_session):
    """open_version inserts a row with status='open', phase='build'."""
    project = await _make_project(db_session)
    version = await rv.open_version(
        db_session, project.id, version_number=1, base_sha="abc123"
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert version.version_number == 1
    assert version.status == "open"
    assert version.phase == "build"
    assert version.base_sha == "abc123"
    assert version.opened_at is not None


@pytest.mark.asyncio
async def test_open_version_stores_goal(db_session):
    """open_version persists the goal snapshot when provided."""
    project = await _make_project(db_session)
    version = await rv.open_version(
        db_session,
        project.id,
        version_number=1,
        goal="Ship the mobile-responsive admin UI",
    )
    await db_session.commit()
    await db_session.refresh(version)
    assert version.goal == "Ship the mobile-responsive admin UI"


@pytest.mark.asyncio
async def test_open_version_goal_defaults_to_none(db_session):
    """open_version leaves goal as None when not provided."""
    project = await _make_project(db_session)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)
    assert version.goal is None


@pytest.mark.asyncio
async def test_open_version_truncates_long_goal(db_session):
    """A goal longer than _GOAL_MAX is truncated to the cap."""
    project = await _make_project(db_session)
    version = await rv.open_version(
        db_session, project.id, version_number=1, goal="g" * 5000
    )
    await db_session.commit()
    await db_session.refresh(version)
    assert version.goal is not None
    assert len(version.goal) == rv._GOAL_MAX


@pytest.mark.asyncio
async def test_current_open_version_returns_none_when_absent(db_session):
    """No open version → None."""
    project = await _make_project(db_session)
    assert await rv.current_open_version(db_session, project.id) is None


@pytest.mark.asyncio
async def test_current_open_version_returns_the_open_row(db_session):
    """current_open_version returns the single status='open' row."""
    project = await _make_project(db_session)
    await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    open_ver = await rv.current_open_version(db_session, project.id)
    assert open_ver is not None
    assert open_ver.version_number == 1


@pytest.mark.asyncio
async def test_current_open_version_ignores_non_open_rows(db_session):
    """A promoted row is not returned; only the open one is."""
    project = await _make_project(db_session)

    promoted = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="build",
        status="promoted",
        opened_at=_utcnow(),
    )
    db_session.add(promoted)
    await rv.open_version(db_session, project.id, version_number=2)
    await db_session.commit()

    open_ver = await rv.current_open_version(db_session, project.id)
    assert open_ver is not None
    assert open_ver.version_number == 2


@pytest.mark.asyncio
async def test_current_open_version_picks_most_recent_on_anomaly(db_session):
    """Two open rows (data anomaly) → the most recently opened is returned."""
    project = await _make_project(db_session)

    older = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow() - timedelta(hours=2),
    )
    newer = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    db_session.add(older)
    db_session.add(newer)
    await db_session.commit()

    open_ver = await rv.current_open_version(db_session, project.id)
    assert open_ver is not None
    assert open_ver.version_number == 2


# ===========================================================================
# set_version_phase
# ===========================================================================


@pytest.mark.asyncio
async def test_set_version_phase_transition(db_session):
    """A build→stabilize transition stamps phase, phase_changed_at, reasoning."""
    project = await _make_project(db_session)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    assert version.phase == "build"
    rv.set_version_phase(version, "stabilize", reasoning="ready to stabilize")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    assert version.phase == "stabilize"
    assert version.phase_changed_at is not None
    assert version.phase_reasoning == "ready to stabilize"


@pytest.mark.asyncio
async def test_set_version_phase_idempotent_noop(db_session):
    """Setting the phase to its current value is a no-op (no timestamp bump)."""
    project = await _make_project(db_session)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    # Version starts in 'build'; set to 'build' again.
    rv.set_version_phase(version, "build", reasoning="should not apply")
    # No-op: reasoning is not stamped, phase_changed_at stays None.
    assert version.phase == "build"
    assert version.phase_changed_at is None
    assert version.phase_reasoning is None


def test_set_version_phase_rejects_invalid_phase():
    """An out-of-set phase value raises ValueError."""
    version = ReleaseVersion(
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    with pytest.raises(ValueError, match="Invalid phase"):
        rv.set_version_phase(version, "not-a-phase")


def test_set_version_phase_truncates_long_reasoning():
    """Reasoning longer than the cap is truncated."""
    version = ReleaseVersion(
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    long_reason = "x" * 5000
    rv.set_version_phase(version, "stabilize", reasoning=long_reason)
    assert version.phase_reasoning is not None
    assert len(version.phase_reasoning) == rv._PHASE_REASONING_MAX


# ===========================================================================
# to_phase_block
# ===========================================================================


def test_to_phase_block_shape():
    """to_phase_block returns the compact dict for the strategic payload."""
    version = ReleaseVersion(
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=_utcnow(),
        phase_changed_at=_utcnow(),
        phase_reasoning="ready to qualify",
        goal="Ship the mobile-responsive admin UI",
        last_qualification={"status": "pending"},
    )
    block = rv.to_phase_block(version)
    assert block["version_number"] == 3
    assert block["phase"] == "stabilize"
    assert block["goal"] == "Ship the mobile-responsive admin UI"
    assert block["phase_changed_at"] is not None
    assert block["phase_reasoning"] == "ready to qualify"
    assert block["last_qualification"] == {"status": "pending"}


def test_to_phase_block_phase_reasoning_none_when_absent():
    """A version with no phase_reasoning / goal yields None for those keys."""
    version = ReleaseVersion(
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    block = rv.to_phase_block(version)
    assert block["phase_reasoning"] is None
    assert block["goal"] is None


def test_to_phase_block_handles_none_phase_changed_at():
    """A version with no phase_changed_at yields None for that key."""
    version = ReleaseVersion(
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    block = rv.to_phase_block(version)
    assert block["phase_changed_at"] is None


def test_to_phase_block_carries_qualification_state():
    """to_phase_block exposes the disambiguated qualification_state alongside the
    raw last_qualification (incident 2026-06-02: raw status='pending' was
    ambiguous)."""
    version = ReleaseVersion(
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=_utcnow(),
        last_qualification={"status": "pending"},
    )
    block = rv.to_phase_block(version)
    # raw status stays untouched; the derived sub-state is added.
    assert block["last_qualification"] == {"status": "pending"}
    assert block["qualification_state"]["state"] == "qualifying"


def test_to_phase_block_qualification_state_none_when_unqualified():
    """An unqualified version (no verdict) yields qualification_state=None."""
    version = ReleaseVersion(
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow(),
        last_qualification=None,
    )
    assert rv.to_phase_block(version)["qualification_state"] is None


# ===========================================================================
# summarize_qualification_state — disambiguates the overloaded status="pending"
# ===========================================================================


def test_qual_state_none_when_no_verdict():
    assert rv.summarize_qualification_state(None) is None
    assert rv.summarize_qualification_state({}) is None
    assert rv.summarize_qualification_state({"confidence": 0.9}) is None


def test_qual_state_pending_audit_is_qualifying():
    """Bare status='pending' (no promotion_pending) = audit still scoring."""
    state = rv.summarize_qualification_state({"status": "pending"})
    assert state["state"] == "qualifying"
    assert "audit" in state["summary"].lower()


def test_qual_state_pending_with_promotion_is_promotion_in_flight():
    """status='pending' WITH a promotion_pending sub-key = audit passed, the
    promotion PR is open — the case the agent misread as 'still auditing'."""
    state = rv.summarize_qualification_state(
        {
            "status": "pending",
            "promotion_pending": {
                "pr_number": 7357,
                "pr_url": "https://example/pr/7357",
                "auto_merge_enabled": True,
            },
        }
    )
    assert state["state"] == "promotion_in_flight"
    assert state["promotion_pr_number"] == 7357
    assert state["promotion_pr_url"] == "https://example/pr/7357"
    # The summary must NOT tell the agent the audit is still running.
    assert "passed" in state["summary"].lower()
    assert "armed" in state["summary"].lower()


def test_qual_state_promotion_failed_surfaces_reason():
    state = rv.summarize_qualification_state(
        {"status": "promotion_failed", "error": "pr_ci_red", "detail": "shard 8"}
    )
    assert state["state"] == "promotion_failed"
    assert state["error"] == "pr_ci_red"
    assert "pr_ci_red" in state["summary"]
    assert "fresh qualify" in state["summary"].lower()


def test_qual_state_not_promoted_includes_scores():
    state = rv.summarize_qualification_state(
        {
            "status": "not_promoted",
            "confidence": 0.62,
            "threshold": 0.9,
            "blocker": "audit",
        }
    )
    assert state["state"] == "not_promoted"
    assert state["blocker"] == "audit"
    assert "0.62" in state["summary"]


def test_qual_state_dry_run_is_promotion_ready():
    state = rv.summarize_qualification_state({"status": "dry_run"})
    assert state["state"] == "dry_run"
    assert "promotion-ready" in state["summary"].lower()


def test_qual_state_promoted():
    state = rv.summarize_qualification_state({"status": "promoted"})
    assert state["state"] == "promoted"


def test_qual_state_deferred_unsynced():
    """The qualify-on-synced gate's deferral renders as an explicit, no-action
    sub-state (not the bare forward-compat fallback)."""
    state = rv.summarize_qualification_state(
        {"status": "deferred_unsynced", "candidate_sha": "abc"}
    )
    assert state["state"] == "deferred_unsynced"
    assert "deferred" in state["summary"].lower()
    assert "auto-retries" in state["summary"].lower()


def test_qual_state_unknown_status_passes_through():
    """Forward-compat: an unrecognized status is echoed, never crashes."""
    state = rv.summarize_qualification_state({"status": "some_new_status"})
    assert state["state"] == "some_new_status"


# ===========================================================================
# recent_closed_versions
# ===========================================================================


@pytest.mark.asyncio
async def test_recent_closed_versions_returns_empty_for_no_history(db_session):
    """A project with only an open version (no closed history) returns []."""
    project = await _make_project(db_session)
    await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    rows = await rv.recent_closed_versions(db_session, project.id)
    assert rows == []


@pytest.mark.asyncio
async def test_recent_closed_versions_returns_closed_rows_newest_first(db_session):
    """Closed versions are returned newest-first, open version is excluded."""
    project = await _make_project(db_session)

    older = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="build",
        status="promoted",
        opened_at=_utcnow() - timedelta(hours=4),
    )
    newer = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        status="promoted",
        opened_at=_utcnow() - timedelta(hours=2),
    )
    open_v = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    for v in (older, newer, open_v):
        db_session.add(v)
    await db_session.commit()

    rows = await rv.recent_closed_versions(db_session, project.id)
    assert len(rows) == 2
    assert rows[0].version_number == 2
    assert rows[1].version_number == 1


@pytest.mark.asyncio
async def test_recent_closed_versions_respects_limit(db_session):
    """Passing limit=2 returns at most 2 rows even with more history."""
    project = await _make_project(db_session)

    for i in range(1, 5):
        db_session.add(
            ReleaseVersion(
                project_id=project.id,
                version_number=i,
                phase="build",
                status="promoted",
                opened_at=_utcnow() - timedelta(hours=10 - i),
            )
        )
    await db_session.commit()

    rows = await rv.recent_closed_versions(db_session, project.id, limit=2)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_recent_closed_versions_scoped_to_project(db_session):
    """Closed versions belonging to another project are not returned."""
    project_a = await _make_project(db_session, name="RV-A")
    project_b = await _make_project(db_session, name="RV-B")

    db_session.add(
        ReleaseVersion(
            project_id=project_a.id,
            version_number=1,
            phase="build",
            status="promoted",
            opened_at=_utcnow(),
        )
    )
    await db_session.commit()

    assert await rv.recent_closed_versions(db_session, project_a.id) != []
    assert await rv.recent_closed_versions(db_session, project_b.id) == []


# ===========================================================================
# ensure_initial_release_version
# ===========================================================================


@pytest.mark.asyncio
async def test_ensure_initial_release_version_creates_stabilize_version(db_session):
    """When no version rows exist and branch != 'main', version 1 is opened in
    the 'stabilize' phase (ready to qualify immediately, not waiting on build)."""
    project = await _make_project(db_session)
    version = await rv.ensure_initial_release_version(
        db_session,
        project.id,
        integration_branch="automain",
        base_sha="deadbeef",
        promotion_target="main",
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert version is not None
    assert version.version_number == 1
    assert version.phase == "stabilize"
    assert version.status == "open"
    assert version.base_sha == "deadbeef"


@pytest.mark.asyncio
async def test_ensure_initial_release_version_snapshots_goal(db_session):
    """The goal passed to the bootstrap is persisted on version 1."""
    project = await _make_project(db_session)
    version = await rv.ensure_initial_release_version(
        db_session,
        project.id,
        integration_branch="automain",
        base_sha="deadbeef",
        goal="Bootstrap the LTS release line",
        promotion_target="main",
    )
    await db_session.commit()
    await db_session.refresh(version)
    assert version is not None
    assert version.goal == "Bootstrap the LTS release line"


@pytest.mark.asyncio
async def test_ensure_initial_release_version_noop_when_main_branch(db_session):
    """integration_branch='main' is degenerate (main…HEAD diff is empty) — no row
    is created and None is returned."""
    project = await _make_project(db_session)
    result = await rv.ensure_initial_release_version(
        db_session,
        project.id,
        integration_branch="main",
        base_sha="abc123",
        promotion_target="main",
    )
    await db_session.commit()

    assert result is None
    assert await rv.current_open_version(db_session, project.id) is None


@pytest.mark.asyncio
async def test_ensure_initial_release_version_idempotent_open_exists(db_session):
    """If an open version already exists the function is a no-op and returns None."""
    project = await _make_project(db_session)
    await rv.open_version(db_session, project.id, version_number=1, base_sha="sha1")
    await db_session.commit()

    result = await rv.ensure_initial_release_version(
        db_session,
        project.id,
        integration_branch="automain",
        base_sha="sha2",
        promotion_target="main",
    )
    assert result is None

    open_ver = await rv.current_open_version(db_session, project.id)
    assert open_ver is not None
    assert open_ver.version_number == 1
    assert open_ver.base_sha == "sha1"


@pytest.mark.asyncio
async def test_ensure_initial_release_version_noop_when_history_exists(db_session):
    """If any version row exists (even promoted, not open), return None — the
    lifecycle has already started and bootstrapping would skip gaps."""
    project = await _make_project(db_session)

    promoted = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="build",
        status="promoted",
        opened_at=_utcnow() - timedelta(hours=1),
    )
    db_session.add(promoted)
    await db_session.commit()

    result = await rv.ensure_initial_release_version(
        db_session,
        project.id,
        integration_branch="automain",
        base_sha="newsha",
        promotion_target="main",
    )
    assert result is None
    assert await rv.current_open_version(db_session, project.id) is None


@pytest.mark.asyncio
async def test_ensure_initial_release_version_requires_promotion_target(db_session):
    """Omitting promotion_target raises TypeError — it is a required keyword arg."""
    project = await _make_project(db_session)
    with pytest.raises(TypeError):
        await rv.ensure_initial_release_version(
            db_session,
            project.id,
            integration_branch="automain",
            base_sha="abc123",
        )


@pytest.mark.asyncio
async def test_ensure_initial_release_version_noop_when_branch_equals_promotion_target(
    db_session,
):
    """When integration_branch equals a non-'main' promotion_target the check is
    still degenerate — no row is created and None is returned."""
    project = await _make_project(db_session)
    result = await rv.ensure_initial_release_version(
        db_session,
        project.id,
        integration_branch="deploy",
        base_sha="abc123",
        promotion_target="deploy",
    )
    await db_session.commit()

    assert result is None
    assert await rv.current_open_version(db_session, project.id) is None


@pytest.mark.asyncio
async def test_ensure_initial_release_version_creates_when_promotion_target_differs(
    db_session,
):
    """When promotion_target is not 'main' but integration_branch differs from it,
    version 1 is bootstrapped normally."""
    project = await _make_project(db_session)
    version = await rv.ensure_initial_release_version(
        db_session,
        project.id,
        integration_branch="automain",
        base_sha="deadbeef",
        promotion_target="deploy",
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert version is not None
    assert version.version_number == 1
    assert version.phase == "stabilize"
    assert version.status == "open"

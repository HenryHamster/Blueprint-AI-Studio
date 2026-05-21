"""Tests for release_version.py — the ReleaseVersion table helpers
(automain LTS Phase 2).

Focus areas:
  * count_merged_since — the ``pr_merged`` filter MUST be applied on both
    dialects: a COMPLETED task without ``context["pr_merged"] = True`` is
    never counted; a non-COMPLETED task with the flag is never counted;
    the ``since`` lower bound is exclusive-of-older rows.
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
# count_merged_since — the pr_merged filter must be applied
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
        last_qualification={"status": "pending"},
    )
    block = rv.to_phase_block(version)
    assert block["version_number"] == 3
    assert block["phase"] == "stabilize"
    assert block["phase_changed_at"] is not None
    assert block["phase_reasoning"] == "ready to qualify"
    assert block["last_qualification"] == {"status": "pending"}


def test_to_phase_block_phase_reasoning_none_when_absent():
    """A version with no phase_reasoning yields None for that key."""
    version = ReleaseVersion(
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    block = rv.to_phase_block(version)
    assert block["phase_reasoning"] is None


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

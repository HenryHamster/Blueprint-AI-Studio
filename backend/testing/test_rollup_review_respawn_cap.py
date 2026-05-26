"""Tests for the review-respawn circuit-breaker in rollup_management.

Regression — the 2026-05-09 SCOPE_EMPTY incident: the stale-PR rollup recovery
in ``_check_and_trigger_rollup`` respawned a review for the same unmerged PR
795 times in 9.5h because nothing capped the respawns. ``MAX_REVIEW_RESPAWNS``
and ``_review_respawn_count`` bound the loop so it escalates to manual approval
instead of looping unbounded.

These tests exercise the actual circuit-breaker in ``_check_and_trigger_rollup``
end-to-end against a real DB session — not just the trivial counter accessor:

  - at-cap → parent escalated to ``requires_approval``, NO review respawned
  - below-cap → counter increments by exactly 1 after a real spawn
  - below-cap + dedup hit → counter does NOT increment
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.models import Artifact, ArtifactType, Project, Task, TaskStatus
from app.rollup_management import MAX_REVIEW_RESPAWNS, _review_respawn_count
from testing.factories import make_task


# ── Counter-accessor unit tests ───────────────────────────────────────────────
# These pin genuine edge cases of ``_review_respawn_count`` — a missing/None
# context or a corrupt non-numeric value must not crash the rollup recovery
# loop. They are NOT tautologies: each would fail if the helper's try/except
# or the ``or 0`` fallback were removed.


def _child(**ctx):
    """A create-task child carrying the given context keys."""
    return make_task(context=dict(ctx), payloads=[{"type": "create"}])


class TestReviewRespawnCount:
    def test_zero_when_unset(self):
        assert _review_respawn_count(_child()) == 0

    def test_zero_when_context_is_none(self):
        t = _child()
        t.context = None
        assert _review_respawn_count(t) == 0

    def test_reads_stored_count(self):
        assert _review_respawn_count(_child(_review_respawn_count=3)) == 3

    def test_tolerates_non_numeric_value(self):
        # A corrupt context value must not crash the rollup recovery loop.
        assert _review_respawn_count(_child(_review_respawn_count="oops")) == 0

    def test_tolerates_none_value(self):
        assert _review_respawn_count(_child(_review_respawn_count=None)) == 0

    def test_ignores_legacy_unprefixed_key(self):
        # The key was renamed to underscore-prefixed so it does not leak
        # through _spawn_pipeline_task's non-underscore context propagation.
        # A stale unprefixed value from before the rename must read as 0.
        assert _review_respawn_count(_child(review_respawn_count=4)) == 0


def test_max_review_respawns_is_a_sane_positive_int():
    assert isinstance(MAX_REVIEW_RESPAWNS, int)
    assert 1 <= MAX_REVIEW_RESPAWNS <= 20


# ── Circuit-breaker integration tests ─────────────────────────────────────────


def _naive_utc_ago(**kwargs) -> datetime:
    """Naive UTC datetime ``kwargs`` in the past (matches _utcnow() semantics)."""
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).replace(tzinfo=None)


@pytest_asyncio.fixture()
async def rollup_db():
    """Isolated in-memory SQLite session for circuit-breaker tests."""
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
            type_, **kw
        )

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    async with Session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


async def _make_respawn_scenario(
    session, *, child_respawn_count, payload_type="create"
):
    """Build an EVALUATING parent with one stale, unmerged COMPLETED child.

    The child has a PR artifact and an ``updated_at`` 30 min in the past so it
    lands in the ``unmerged`` stale-respawn list of ``_check_and_trigger_rollup``.
    ``child._review_respawn_count`` context key is seeded so the cap check can
    be exercised on either side of ``MAX_REVIEW_RESPAWNS``.

    Returns ``(parent, child)``.
    """
    project = Project(name="Respawn Cap Test", user_id="user1")
    session.add(project)
    await session.commit()
    await session.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "parent"}],
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/respawn", "parent_base": "main"},
        requires_approval=False,
    )
    session.add(parent)
    await session.commit()
    await session.refresh(parent)

    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": payload_type, "prompt": "child"}],
        status=TaskStatus.COMPLETED,
        context={"_review_respawn_count": child_respawn_count},
        requires_approval=False,
        # Stale: older than the 10-min threshold so the respawn loop engages.
        updated_at=_naive_utc_ago(minutes=30),
    )
    session.add(child)
    await session.commit()
    await session.refresh(child)

    session.add(
        Artifact(
            task_id=child.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/321",
        )
    )
    await session.commit()
    return parent, child


@pytest.mark.asyncio
async def test_at_cap_escalates_parent_and_does_not_respawn(rollup_db, mocker):
    """A child whose review has been respawned MAX_REVIEW_RESPAWNS times must
    NOT spawn another review — the parent is escalated to requires_approval."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mock_alert = mocker.patch(
        "app.main._maybe_create_failure_alert", new_callable=AsyncMock
    )
    # PR is genuinely not merged so the child stays in the unmerged list.
    mocker.patch(
        "app.rollup_management._verify_pr_merged",
        new=AsyncMock(return_value=False),
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    parent, child = await _make_respawn_scenario(
        rollup_db, child_respawn_count=MAX_REVIEW_RESPAWNS
    )

    await _check_and_trigger_rollup(child, rollup_db)

    # Circuit-breaker fired: no further review spawned for the wedged PR.
    mock_spawn.assert_not_called()
    # Parent escalated to manual approval instead of looping forever.
    await rollup_db.refresh(parent)
    assert parent.requires_approval is True
    assert parent.status == TaskStatus.EVALUATING
    # Operator alert raised so the escalation is visible.
    mock_alert.assert_awaited()


@pytest.mark.asyncio
async def test_below_cap_increments_counter_by_one_on_real_spawn(rollup_db, mocker):
    """Below the cap, a real respawn (was_spawned=True) increments the child's
    _review_respawn_count by exactly 1 and leaves the parent un-escalated."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_management._verify_pr_merged",
        new=AsyncMock(return_value=False),
    )

    start = 2
    assert start < MAX_REVIEW_RESPAWNS, "test fixture must start below the cap"

    # A real, freshly-created review task: distinct id, was_spawned=True.
    spawned_review = Task(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        payloads=[{"type": "review", "prompt": "review"}],
        status=TaskStatus.QUEUED,
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(spawned_review, True),
    )

    from app.rollup_management import _check_and_trigger_rollup

    parent, child = await _make_respawn_scenario(rollup_db, child_respawn_count=start)

    await _check_and_trigger_rollup(child, rollup_db)

    mock_spawn.assert_awaited_once()
    await rollup_db.refresh(child)
    # Exactly one increment — not zero, not two.
    assert (child.context or {}).get("_review_respawn_count") == start + 1
    # Below cap → parent stays un-escalated.
    await rollup_db.refresh(parent)
    assert parent.requires_approval is False


@pytest.mark.asyncio
async def test_respawn_uses_task_type_branch_prefix(rollup_db, mocker):
    """Regression — 2026-05-25 incident: PRs #5779/#5812/#5815/#5819 stalled
    at the review cap because rollup_management hardcoded `blueprint/task-*`
    for the clone target, while the agent had pushed `blueprint/refactor-*`
    for refactor-type children. The respawn must derive the branch from the
    child's task type via shared_state.derive_task_branch, not synthesize."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_management._verify_pr_merged",
        new=AsyncMock(return_value=False),
    )
    spawned_review = Task(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        payloads=[{"type": "review", "prompt": "review"}],
        status=TaskStatus.QUEUED,
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(spawned_review, True),
    )

    from app.rollup_management import _check_and_trigger_rollup
    from app.shared_state import derive_task_branch

    # Refactor-type child — agents push these to blueprint/refactor-{8hex},
    # NOT blueprint/task-{8hex}.
    _parent, child = await _make_respawn_scenario(
        rollup_db, child_respawn_count=0, payload_type="refactor"
    )

    await _check_and_trigger_rollup(child, rollup_db)

    mock_spawn.assert_awaited_once()
    spawn_kwargs = mock_spawn.call_args.kwargs
    spawn_args = mock_spawn.call_args.args
    # _spawn_pipeline_task signature: (source_task, task_type, session,
    # pr_number, branch, base_branch, ...). branch is positional index 4.
    branch_passed = spawn_kwargs.get("branch") or spawn_args[4]
    expected = derive_task_branch(child.id, "refactor")
    assert branch_passed == expected, (
        f"respawn cloned wrong branch: got {branch_passed!r}, expected {expected!r} "
        f"(must use child's task type, not hardcoded 'task-' prefix)"
    )
    assert "refactor-" in branch_passed


@pytest.mark.asyncio
async def test_respawn_prefers_review_loop_branch_over_synthesized(
    rollup_db, mocker
):
    """The respawn now goes through ``resolve_task_branch``, which prefers
    ``context.review_loop.branch`` over ``context.branch`` and the synthesized
    fallback. Regression for the 2026-05-26 fix-task respawn bug where the
    fallback ``derive_task_branch(c.id, "fix")`` produced a branch no agent
    ever pushed -- when an agent re-uses a parent's branch (review_loop
    carries it), the synthesized name diverges and the spawned review fails
    at ``git clone --branch``.

    Verified here against a create-type child (the only type the rollup
    respawn loop actually iterates) seeded with a review_loop.branch that
    differs from what context.branch / derivation would produce, so the
    precedence is observable.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_management._verify_pr_merged",
        new=AsyncMock(return_value=False),
    )
    spawned_review = Task(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        payloads=[{"type": "review", "prompt": "review"}],
        status=TaskStatus.QUEUED,
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(spawned_review, True),
    )

    from app.rollup_management import _check_and_trigger_rollup

    _parent, child = await _make_respawn_scenario(
        rollup_db, child_respawn_count=0, payload_type="create"
    )
    # Seed the child's context with a review_loop.branch that does NOT
    # equal what synthesizing from id+type would produce. The helper
    # must prefer review_loop.branch.
    canonical_branch = "blueprint/task-fromreviewloop"
    child.context = {
        **(child.context or {}),
        # No context.branch on purpose -- the bug was the fallback chain
        # ignoring review_loop and going straight to derivation.
        "review_loop": {"branch": canonical_branch, "iteration": 2},
    }
    # Re-stamp updated_at as stale; the commit below would otherwise refresh
    # it to now via onupdate=_utcnow and the >10min staleness gate would skip
    # the respawn.
    child.updated_at = _naive_utc_ago(minutes=30)
    rollup_db.add(child)
    await rollup_db.commit()

    await _check_and_trigger_rollup(child, rollup_db)

    mock_spawn.assert_awaited_once()
    spawn_kwargs = mock_spawn.call_args.kwargs
    spawn_args = mock_spawn.call_args.args
    branch_passed = spawn_kwargs.get("branch") or spawn_args[4]
    # Must read from review_loop.branch, not synthesize from id+type.
    assert branch_passed == canonical_branch, (
        f"respawn used wrong branch: got {branch_passed!r}, "
        f"expected {canonical_branch!r} (must read review_loop.branch)"
    )
    # Sanity: this is NOT what plain derivation would have produced.
    from app.shared_state import derive_task_branch as _derive

    synthesized = _derive(child.id, "create")
    assert branch_passed != synthesized


@pytest.mark.asyncio
async def test_dedup_hit_does_not_increment_counter(rollup_db, mocker):
    """When _spawn_pipeline_task returns was_spawned=False (an idempotency
    dedup hit — an existing review task was reused, not created), the
    respawn counter must NOT advance. Otherwise a dedup-stable PR would
    still burn through the cap and false-escalate."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_management._verify_pr_merged",
        new=AsyncMock(return_value=False),
    )

    start = 2
    assert start < MAX_REVIEW_RESPAWNS, "test fixture must start below the cap"

    # An EXISTING review task returned by the dedup path: distinct id,
    # was_spawned=False (no new row created).
    existing_review = Task(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        payloads=[{"type": "review", "prompt": "review"}],
        status=TaskStatus.IN_PROGRESS,
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(existing_review, False),
    )

    from app.rollup_management import _check_and_trigger_rollup

    parent, child = await _make_respawn_scenario(rollup_db, child_respawn_count=start)

    await _check_and_trigger_rollup(child, rollup_db)

    mock_spawn.assert_awaited_once()
    await rollup_db.refresh(child)
    # Dedup hit → counter unchanged.
    assert (child.context or {}).get("_review_respawn_count") == start
    await rollup_db.refresh(parent)
    assert parent.requires_approval is False

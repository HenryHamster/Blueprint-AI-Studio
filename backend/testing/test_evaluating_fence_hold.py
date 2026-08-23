"""A MACHINE-set fence hold is never killed by the human-approval clock.

Incident 2026-08-23, research task ``70c5ec8f``. The platform FAILED it with
``Approval-gate timeout after 2.0h — no manual approval received, escalating
to FAILED``. But its hold was never a question posed to a human about that
task; ``rollup_lineage_fence.mark_ancestor_rollups_awaiting_operator`` had
established it autonomously::

    _approval_hold_provenance = {"owner": "rollup_lineage_fence",
      "reason": "descendant 5fb05561 needs operator intervention",
      "set_at": "2026-08-23T01:42:32", ...}

Fence hold set 01:42Z → the 2h human-approval timeout FAILED the ancestor at
~03:42Z → the blocking descendant's PR #17693 merged at 12:11Z. The blocker
resolved itself 8.5h later, but the rollup was already destroyed.

These tests pin the two halves of the fix at the recovery-handler seam:

  * a hold the fence PROVABLY owns is adjudicated (wait / release / decide),
    never terminalized — and the wait states its own reason; and
  * ownership is STRUCTURAL, so any hold the fence cannot prove it owns keeps
    its pre-existing disposition byte-for-byte, including a row that merely
    LOOKS fence-owned.

The adjudicator's own state machine (release / decide / window / unknown-state
conservatism) is covered in ``test_rollup_lineage_fence.py``; the first-preserve
and spend/PR-less dispositions live in ``test_evaluating.py``. This file exists
as a sibling only to keep that module under its size cap, exactly like
``test_evaluating_handmerge_latch.py``.
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

from app.models import Project, Task, TaskStatus
from app.services import _check_evaluating_timeouts  # noqa: F401

# Imported AFTER ``app.services`` on purpose: ``recovery_evaluating_approval``
# participates in a package-level import cycle through it, so pulling it in
# first at module scope fails collection.
from app.recovery_evaluating_approval import _handle_approval_gate_timeout
from app.rollup_lineage_fence import (
    descendant_operator_block_hold_is_owned,
    mark_ancestor_rollups_awaiting_operator,
)

_BLOCKS_KEY = "_descendant_operator_blocks"
_HOLD_RECEIPT_KEY = "_descendant_operator_blocks_approval_hold"


def _naive_utc_ago(**kwargs) -> datetime:
    """Naive-UTC timestamp ``kwargs`` in the past (``_utcnow`` strips tzinfo)."""
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).replace(tzinfo=None)


@pytest_asyncio.fixture
async def fence_db(mocker):
    """Isolated in-memory SQLite DB, mirroring ``test_evaluating.eval_db``."""
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
    mocker.patch("app.services.AsyncSessionLocal", Session)

    async with Session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


async def _fenced_ancestor(session, *, pr_number: int = 17693):
    """An EVALUATING rollup parent carrying a REAL fence descendant block."""
    project = Project(name=f"Fence Hold {uuid.uuid4().hex[:6]}", user_id="user1")
    session.add(project)
    await session.commit()
    await session.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        status=TaskStatus.EVALUATING,
        context={"rollup_pr_number": 997, "_is_rollup_review": True},
    )
    session.add(parent)
    await session.commit()
    await session.refresh(parent)

    child = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "implement"}],
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
    )
    session.add(child)
    await session.commit()
    await session.refresh(child)

    await mark_ancestor_rollups_awaiting_operator(
        child, session, reason="review spawn failed", pr_number=pr_number
    )
    await session.commit()
    await session.refresh(parent)
    assert descendant_operator_block_hold_is_owned(parent) is True
    return parent, child


@pytest.mark.asyncio
async def test_approval_gate_never_fails_a_fence_owned_descendant_block_hold(
    fence_db, mocker
):
    """INCIDENT REGRESSION (70c5ec8f): blocker PR still OPEN at the timeout.

    The ancestor must NOT be FAILED. The hold is retained, the row stays
    EVALUATING, nothing is recorded as a failure, and the status message
    surfaces WHY it is waiting: which descendant, which PR, the recorded
    reason, and how long it has been blocked.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.services.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": False, "state": "open", "mergeable_state": "dirty"},
    )
    rec = mocker.patch("app.routers.agent_status.record_task_result")
    fail_alert = AsyncMock()

    parent, child = await _fenced_ancestor(fence_db)
    # Well past the 2h gate AND past its 6h chain-busy defer window, so the
    # ONLY thing standing between this row and FAILED is the fence branch.
    now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=9)

    handled = await _handle_approval_gate_timeout(
        parent,
        parent.context or {},
        fence_db,
        now,
        spend_approval_fail_seconds=24 * 3600,
        spend_hours=24,
        maybe_create_failure_alert=fail_alert,
    )
    await fence_db.refresh(parent)

    assert handled is True
    assert parent.status == TaskStatus.EVALUATING, (
        f"a MACHINE-set fence hold must never be terminalized by the "
        f"human-approval clock, got {parent.status}: "
        f"{parent.latest_status_message!r}"
    )
    assert parent.requires_approval is True
    assert str(child.id) in ((parent.context or {}).get(_BLOCKS_KEY) or {})
    message = parent.latest_status_message or ""
    assert str(child.id)[:8] in message
    assert "PR #17693" in message
    assert "review spawn failed" in message
    assert "blocked 9h" in message
    # Not a failure in any ledger, and no hand-merge park either.
    rec.assert_not_called()
    fail_alert.assert_not_awaited()
    assert (parent.context or {}).get("failure_detail") is None
    assert (parent.context or {}).get(
        "approval_timeout_preserved_for_handmerge"
    ) is None


@pytest.mark.asyncio
async def test_approval_gate_releases_a_fence_hold_whose_blocker_pr_merged(
    fence_db, mocker
):
    """The same ancestor, once #17693 lands: released, restored, not failed.

    The new adjudication branch must not shadow the pre-existing out-of-band
    release: a resolved blocker still lifts the hold and leaves the ancestor
    EVALUATING rather than falling through to the timeout dispositions.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.services.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )

    async def _pr(*args, **kwargs):
        # Only the BLOCKER landed; the ancestor's own rollup PR #997 is still
        # open, so nothing else can dispose of this row.
        number = kwargs.get("pr_number", args[3] if len(args) > 3 else None)
        if number == 17693:
            return {"merged": True, "state": "closed"}
        return {"merged": False, "state": "open", "mergeable_state": "dirty"}

    mocker.patch("app.github.get_pull_request", side_effect=_pr)
    rec = mocker.patch("app.routers.agent_status.record_task_result")
    fail_alert = AsyncMock()

    parent, _child = await _fenced_ancestor(fence_db)
    now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=9)

    handled = await _handle_approval_gate_timeout(
        parent,
        parent.context or {},
        fence_db,
        now,
        spend_approval_fail_seconds=24 * 3600,
        spend_hours=24,
        maybe_create_failure_alert=fail_alert,
    )
    await fence_db.refresh(parent)

    assert handled is True
    assert parent.status == TaskStatus.EVALUATING
    assert parent.requires_approval is False
    assert _BLOCKS_KEY not in (parent.context or {})
    rec.assert_not_called()
    fail_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_gate_still_fails_holds_the_fence_cannot_prove_it_owns(
    fence_db, mocker
):
    """FAIL TOWARD THE OLD BEHAVIOUR — ownership is structural, not textual.

    Two rows past the 2h gate that the fence must NOT claim, both keeping
    their pre-existing terminalization:

      a) an ordinary review-escalation hold with a PR behind it; and
      b) a row that LOOKS fence-owned — keyed ``_descendant_operator_blocks``,
         a ``descendant ... needs operator intervention`` reason, and even
         ``_approval_hold_provenance.owner == "rollup_lineage_fence"`` — but
         carrying no hold receipt, so ownership is unproven. Matching the
         provenance owner or the reason string alone would wrongly rescue it.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.services.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    rec = mocker.patch("app.routers.agent_status.record_task_result")
    fail_alert = AsyncMock()

    project = Project(name="Unproven Fence Holds", user_id="user1")
    fence_db.add(project)
    await fence_db.commit()
    await fence_db.refresh(project)

    plain_hold = Task(
        project_id=project.id,
        payloads=[{"type": "review", "prompt": "approval-gate parent"}],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={
            "_approval_set_at": _naive_utc_ago(hours=9).isoformat(),
            "review_loop": {"pr_number": 8124, "base_branch": "automain"},
        },
    )
    blocker_id = str(uuid.uuid4())
    reason = f"descendant {blocker_id} needs operator intervention"
    unproven_fence_hold = Task(
        project_id=project.id,
        payloads=[{"type": "review", "prompt": "approval-gate parent"}],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={
            "_approval_set_at": _naive_utc_ago(hours=9).isoformat(),
            "review_loop": {"pr_number": 8125, "base_branch": "automain"},
            "approval_hold_reason": reason,
            "_approval_hold_provenance": {
                "episode": "episode-without-a-receipt",
                "owner": "rollup_lineage_fence",
                "reason": reason,
                "set_at": _naive_utc_ago(hours=9).isoformat(),
            },
            _BLOCKS_KEY: {
                blocker_id: {
                    "task_id": blocker_id,
                    "reason": "review spawn failed",
                    "pr_number": None,
                    "blocked_at": _naive_utc_ago(hours=9).isoformat(),
                }
            },
        },
    )
    assert _HOLD_RECEIPT_KEY not in (unproven_fence_hold.context or {})
    assert descendant_operator_block_hold_is_owned(unproven_fence_hold) is False

    fence_db.add(plain_hold)
    fence_db.add(unproven_fence_hold)
    await fence_db.commit()
    await fence_db.refresh(plain_hold)
    await fence_db.refresh(unproven_fence_hold)

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    async def _call(task):
        return await _handle_approval_gate_timeout(
            task,
            task.context or {},
            fence_db,
            now,
            spend_approval_fail_seconds=24 * 3600,
            spend_hours=24,
            maybe_create_failure_alert=fail_alert,
        )

    assert await _call(plain_hold) is True
    await fence_db.refresh(plain_hold)
    assert plain_hold.status == TaskStatus.FAILED
    assert "Approval-gate timeout" in (plain_hold.latest_status_message or "")

    assert await _call(unproven_fence_hold) is True
    await fence_db.refresh(unproven_fence_hold)
    assert unproven_fence_hold.status == TaskStatus.FAILED, (
        "an UNPROVEN fence hold must keep the pre-existing disposition; "
        "ownership is proven structurally, never by matching a reason string "
        "or a provenance owner on its own"
    )
    assert "Approval-gate timeout" in (
        unproven_fence_hold.latest_status_message or ""
    )

    assert rec.call_count == 2
    assert fail_alert.await_count == 2

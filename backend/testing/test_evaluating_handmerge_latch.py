"""The approval-timeout hand-merge preserve latch is a BOUNDED park.

``context.approval_timeout_preserved_for_handmerge`` used to be a ONE-WAY
DOOR: once the approval-gate timeout preserved a mergeable PR, the handler's
idempotent re-entry check returned early on every later sweep and the row sat
``EVALUATING`` with ``requires_approval=True`` forever — waiting for a human to
press Merge on a PR the engine had ALREADY PROVEN landable (task ``129fb995`` /
PR #17720, ``MERGEABLE`` + ``mergeStateStatus: CLEAN``, every required check
green, 2026-08-23).

These tests pin the replacement: after a second bounded window the engine
re-reads the PR and DECIDES — reconcile an already-merged PR, MERGE one that is
still provably green-and-clean, or re-arm a bounded number of times and finally
terminalize. They also pin the two fail-safes that make that safe to run
autonomously: a GitHub outage must re-arm WITHOUT spending re-arm budget (no
bulk cull), and a legacy latch with no timestamp must be stamped rather than
decided on absent evidence.

The existing first-preserve tests live in ``test_evaluating.py``; this file
covers only the re-decision that follows it.
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

from app.recovery_evaluating_handmerge_latch import (
    HANDMERGE_LATCH_DECISION_KEY,
    HANDMERGE_LATCH_KEY,
    HANDMERGE_LATCH_REARM_KEY,
    HANDMERGE_LATCH_STAMPED_AT_KEY,
    HANDMERGE_REDECIDE_MAX_REARMS,
    HANDMERGE_REDECIDE_WINDOW_HOURS,
    redecide_preserved_handmerge_latch,
    resolve_handmerge_redecide_seconds,
)
from app.models import Project, Task, TaskStatus
from app.services import _check_evaluating_timeouts


def _naive_utc_ago(**kwargs) -> datetime:
    """Naive-UTC timestamp ``kwargs`` in the past (``_utcnow`` strips tzinfo)."""
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).replace(tzinfo=None)


@pytest_asyncio.fixture
async def latch_db(mocker):
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


def _quiet_broadcasts(mocker):
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.services.broadcast_to_project", new_callable=AsyncMock)


async def _latched_parent(
    session,
    *,
    pr_number: int,
    stamped_ago_hours: float | None,
    rearms: int = 0,
    name: str = "Handmerge Latch",
) -> tuple[Project, Task]:
    """An EVALUATING parent already sitting on the preserve latch."""
    project = Project(name=f"{name} {uuid.uuid4().hex[:6]}", user_id="user1")
    session.add(project)
    await session.commit()
    await session.refresh(project)

    ctx = {
        # Well past the 2h approval-gate hard timeout.
        "_approval_set_at": _naive_utc_ago(hours=30).isoformat(),
        "rollup_pr_number": pr_number,
        "_is_rollup_review": True,
        HANDMERGE_LATCH_KEY: True,
        "failure_detail": HANDMERGE_LATCH_KEY,
    }
    if stamped_ago_hours is not None:
        ctx[HANDMERGE_LATCH_STAMPED_AT_KEY] = _naive_utc_ago(
            hours=stamped_ago_hours
        ).isoformat()
    if rearms:
        ctx[HANDMERGE_LATCH_REARM_KEY] = rearms

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context=ctx,
    )
    session.add(parent)
    await session.commit()
    await session.refresh(parent)
    return project, parent


# ── The window itself: ON in code, retunable, never gate-able off ────────────


def test_redecide_window_defaults_in_code_not_in_config():
    """The second window ships ON with a CODE default.

    Literal pins on purpose: the whole point of the fix is that no host env
    var, feature flag, or absent runtime-config key can restore the one-way
    door. An empty/absent/corrupt config must yield the active 6h window, and a
    nonsense value must be rejected rather than silently disabling the
    re-decision by making the window astronomically large.
    """
    assert HANDMERGE_REDECIDE_WINDOW_HOURS == 6
    assert HANDMERGE_REDECIDE_MAX_REARMS == 3

    assert resolve_handmerge_redecide_seconds({}) == 6 * 3600
    assert resolve_handmerge_redecide_seconds(None) == 6 * 3600
    assert resolve_handmerge_redecide_seconds({"unrelated": 1}) == 6 * 3600
    # Operator retune inside the sane band is honoured...
    assert resolve_handmerge_redecide_seconds({"handmerge_redecide_window_hours": 2}) == (
        2 * 3600
    )
    # ...and anything outside it, or unparseable, falls back to the CODE value.
    assert resolve_handmerge_redecide_seconds(
        {"handmerge_redecide_window_hours": 0}
    ) == (6 * 3600)
    assert resolve_handmerge_redecide_seconds(
        {"handmerge_redecide_window_hours": 100000}
    ) == (6 * 3600)
    assert resolve_handmerge_redecide_seconds(
        {"handmerge_redecide_window_hours": "never"}
    ) == (6 * 3600)


# ── Case 2: a still-clean PR gets MERGED, not handed to a human ──────────────


@pytest.mark.asyncio
async def test_handmerge_latch_expiry_merges_a_still_clean_pr(latch_db, mocker):
    """THE DEFECT, INVERTED. A latched PR that is still ``mergeable`` and
    ``clean`` after the second window is MERGED by the engine and the task
    reconciles to its normal merged terminal.

    This is the exact live shape of task 129fb995 / PR #17720: the engine had
    already proven the work landable and then asked a human to click Merge.
    """
    _quiet_broadcasts(mocker)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "open",
            "merged": False,
            "number": 17720,
            "mergeable": True,
            "mergeable_state": "clean",
            "changed_files": 4,
            "base": {"ref": "automain"},
            "head": {"sha": "cafef00d"},
        },
    )
    mocker.patch(
        "app.rollup_migration_validation.enforce_premerge_migration_guard",
        new_callable=AsyncMock,
        return_value=(True, None),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "sha": "1234abcd"},
    )
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec = mocker.patch("app.routers.agent_status.record_task_result")

    _project, parent = await _latched_parent(
        latch_db, pr_number=17720, stamped_ago_hours=12
    )

    await _check_evaluating_timeouts()

    merge_mock.assert_awaited_once()
    assert merge_mock.await_args.args[3] == 17720
    # Fail-closed on a moved head: the merge is pinned to the sha we judged.
    assert merge_mock.await_args.kwargs.get("expected_head_sha") == "cafef00d"
    # An autonomous lane must NEVER override the review veto or merge with the
    # operator's own PAT.
    assert merge_mock.await_args.kwargs.get("allow_changes_requested") is None
    assert merge_mock.await_args.kwargs.get("allow_operator_pat") is None

    await latch_db.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED, (
        f"a PR the engine proved landable must be merged and reconciled, not "
        f"parked for a human — got {parent.status}: "
        f"{parent.latest_status_message!r}"
    )
    assert parent.requires_approval is False, (
        "the hold must be released once the work landed"
    )
    # SUCCESS path: never counted as a task failure.
    rec.assert_not_called()
    alert_mock.assert_not_awaited()


# ── Case 1: an already-merged PR reconciles, with no second merge attempt ────


@pytest.mark.asyncio
async def test_handmerge_latch_expiry_reconciles_an_already_merged_pr(
    latch_db, mocker
):
    """A latched PR that merged out-of-band reconciles to the normal merged
    terminal, and the latch never tries to merge it a second time.

    Called directly rather than through the sweep on purpose: the sweep's own
    credit-blindness guard reconciles a merged PR BEFORE the latch branch is
    reached, so an end-to-end run would prove that guard, not this one. This
    branch is what catches a PR that merged between the two reads (and the
    reconcile of our own merge above routes through the same helper).
    """
    _quiet_broadcasts(mocker)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "closed",
            "merged": True,
            "number": 474,
            "base": {"ref": "automain"},
            "merge_commit_sha": "beefbeef",
        },
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", new_callable=AsyncMock
    )

    _project, parent = await _latched_parent(
        latch_db, pr_number=474, stamped_ago_hours=12
    )

    handled = await redecide_preserved_handmerge_latch(
        parent,
        dict(parent.context or {}),
        latch_db,
        datetime.now(timezone.utc).replace(tzinfo=None),
        window_seconds=HANDMERGE_REDECIDE_WINDOW_HOURS * 3600,
    )

    assert handled is True, "a reconciled row is handled, never terminalized"
    merge_mock.assert_not_awaited()
    await latch_db.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED
    assert parent.requires_approval is False
    assert "474" in (parent.latest_status_message or "")


# ── Case 3a: a red PR is NOT merged ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_handmerge_latch_expiry_never_merges_an_unstable_pr(latch_db, mocker):
    """``unstable`` means mergeable but a required check is RED.

    Strictness is the safety property here: the engine merges only what it has
    proven green-and-clean. A red PR must re-arm (spending one budget slot,
    because this IS a real answer about the PR) and must never be merged.
    """
    _quiet_broadcasts(mocker)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "open",
            "merged": False,
            "number": 601,
            # Mergeable, but a status check is failing.
            "mergeable": True,
            "mergeable_state": "unstable",
            "changed_files": 4,
            "base": {"ref": "automain"},
            "head": {"sha": "cafef00d"},
        },
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", new_callable=AsyncMock
    )
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec = mocker.patch("app.routers.agent_status.record_task_result")

    _project, parent = await _latched_parent(
        latch_db, pr_number=601, stamped_ago_hours=12
    )

    await _check_evaluating_timeouts()

    merge_mock.assert_not_awaited()
    await latch_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING, (
        "a red PR is re-armed, not terminalized on the first expiry"
    )
    rec.assert_not_called()
    ctx = parent.context or {}
    assert ctx.get(HANDMERGE_LATCH_REARM_KEY) == 1
    decision = ctx.get(HANDMERGE_LATCH_DECISION_KEY) or {}
    assert decision.get("outcome") == "not_landable"
    assert "unstable" in (decision.get("why") or "")


# ── Case 3b: a GitHub outage must never cull ─────────────────────────────────


@pytest.mark.asyncio
async def test_handmerge_latch_github_outage_rearms_without_spending_budget(
    latch_db, mocker
):
    """A LOOKUP FAILURE IS NOT AN ANSWER.

    If an unreadable PR consumed re-arm budget, a single install-token
    exhaustion window would march every latched row to its cap and cull a fleet
    of perfectly landable PRs in bulk. Only a SUCCESSFUL read of a
    genuinely-not-landable PR may spend a slot.
    """
    from fastapi import HTTPException

    _quiet_broadcasts(mocker)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=HTTPException(
            status_code=503,
            detail="GitHub rate limit exhausted fetching PR #602 (reset at 999)",
        ),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", new_callable=AsyncMock
    )
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec = mocker.patch("app.routers.agent_status.record_task_result")

    _project, parent = await _latched_parent(
        latch_db,
        pr_number=602,
        # Already at the cap: if an unreadable PR consumed budget this row
        # would terminalize on this very sweep.
        stamped_ago_hours=12,
        rearms=HANDMERGE_REDECIDE_MAX_REARMS,
    )
    original_stamp = (parent.context or {})[HANDMERGE_LATCH_STAMPED_AT_KEY]

    await _check_evaluating_timeouts()

    merge_mock.assert_not_awaited()
    await latch_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING, (
        f"a GitHub outage must never cull a latched PR — got {parent.status}: "
        f"{parent.latest_status_message!r}"
    )
    rec.assert_not_called()
    ctx = parent.context or {}
    assert ctx.get(HANDMERGE_LATCH_REARM_KEY) == HANDMERGE_REDECIDE_MAX_REARMS, (
        "an unreadable PR must NOT consume a re-arm budget slot"
    )
    decision = ctx.get(HANDMERGE_LATCH_DECISION_KEY) or {}
    assert decision.get("outcome") == "pr_read_failed"
    assert ctx.get(HANDMERGE_LATCH_STAMPED_AT_KEY) != original_stamp, (
        "the clock must be re-armed so the next window is measured from now"
    )


# ── Case 3c: the dud gets culled once the budget is spent ────────────────────


@pytest.mark.asyncio
async def test_handmerge_latch_rearm_budget_exhaustion_terminalizes(
    latch_db, mocker
):
    """Bounded, not infinite: once the re-arm budget is spent on a PR that
    reads cleanly and is still not landable, the row falls through to the
    existing terminalization path instead of sitting EVALUATING forever."""
    _quiet_broadcasts(mocker)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "open",
            "merged": False,
            "number": 603,
            "mergeable": False,
            "mergeable_state": "dirty",
            "changed_files": 4,
            "base": {"ref": "automain"},
            "head": {"sha": "cafef00d"},
        },
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", new_callable=AsyncMock
    )
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec = mocker.patch("app.routers.agent_status.record_task_result")

    _project, parent = await _latched_parent(
        latch_db,
        pr_number=603,
        stamped_ago_hours=12,
        rearms=HANDMERGE_REDECIDE_MAX_REARMS,
    )

    await _check_evaluating_timeouts()

    merge_mock.assert_not_awaited()
    await latch_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED, (
        f"a conflicted PR that outlived its whole re-decision budget must reach "
        f"a terminal, not be nursed — got {parent.status}"
    )
    rec.assert_called_once()
    assert rec.call_args.kwargs.get("success") is False
    alert_mock.assert_awaited()


# ── Legacy rows: no stamp must not mean "never expires" ──────────────────────


@pytest.mark.asyncio
async def test_legacy_latched_row_without_a_stamp_is_stamped_not_decided(
    latch_db, mocker
):
    """A latch written before this feature existed carries no timestamp.

    A missing stamp must NOT read as "never expires" (that is the wedge again),
    but it also must not be treated as expired long ago — we have no evidence
    of when the latch was set. Backfill the stamp and start the clock: one
    window of extra delay, never a decision on absent evidence.
    """
    _quiet_broadcasts(mocker)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "open",
            "merged": False,
            "number": 604,
            "mergeable": True,
            "mergeable_state": "clean",
            "changed_files": 4,
            "base": {"ref": "automain"},
            "head": {"sha": "cafef00d"},
        },
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", new_callable=AsyncMock
    )
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec = mocker.patch("app.routers.agent_status.record_task_result")

    _project, parent = await _latched_parent(
        latch_db, pr_number=604, stamped_ago_hours=None
    )
    assert HANDMERGE_LATCH_STAMPED_AT_KEY not in (parent.context or {})

    await _check_evaluating_timeouts()

    merge_mock.assert_not_awaited()
    await latch_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    rec.assert_not_called()
    ctx = parent.context or {}
    assert ctx.get(HANDMERGE_LATCH_STAMPED_AT_KEY), (
        "a legacy latch must be stamped on first sight so its clock can run"
    )
    assert ctx.get(HANDMERGE_LATCH_REARM_KEY) is None, (
        "backfilling a stamp must not spend re-arm budget"
    )
    decision = ctx.get(HANDMERGE_LATCH_DECISION_KEY) or {}
    assert decision.get("outcome") == "stamp_backfilled"


# ── Authority boundary: CLEAN is not permission to promote ───────────────────


@pytest.mark.asyncio
async def test_handmerge_latch_never_auto_promotes_to_the_promotion_branch(
    latch_db, mocker
):
    """A PR based on the promotion target is NOT this path's to merge.

    Everything the merge branch checks — ``mergeable`` plus
    ``mergeStateStatus: CLEAN`` — is a statement about GitHub branch
    protection: no conflict, required checks green, no blocking review. For an
    ordinary task PR into the integration trunk that is the whole question. For
    a PR into the promotion branch it is not even most of it: promotion
    authority lives in the release qualifier (composite score, audit,
    test_erosion), which GitHub cannot see and a green check cannot stand in
    for. Merging here would land unqualified work on the branch that deploys,
    and would report a passing check as the thing that authorized it.

    So the refusal is structural rather than conditional, and it consumes
    re-arm budget on purpose: this is a permanent fact about the PR, so the row
    must walk to a terminal rather than re-deciding it every window forever.
    The PR is left OPEN throughout — the release lane still owns it.
    """
    _quiet_broadcasts(mocker)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "open",
            "merged": False,
            "number": 17999,
            # Deliberately the SAME shape the merge test uses. The only thing
            # that differs is the base branch, so nothing but the authority
            # boundary can be what stops the merge.
            "mergeable": True,
            "mergeable_state": "clean",
            "changed_files": 4,
            "base": {"ref": "main"},
            "head": {"sha": "cafef00d"},
        },
    )
    guard = mocker.patch(
        "app.rollup_migration_validation.enforce_premerge_migration_guard",
        new_callable=AsyncMock,
        return_value=(True, None),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "sha": "1234abcd"},
    )
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.routers.agent_status.record_task_result")

    project, parent = await _latched_parent(
        latch_db, pr_number=17999, stamped_ago_hours=12
    )
    # Explicit rather than relying on the "main" fallback, so the test still
    # means what it says if a project ever promotes somewhere else.
    project.promotion_target = "main"
    latch_db.add(project)
    await latch_db.commit()

    await _check_evaluating_timeouts()

    merge_mock.assert_not_awaited(), "an approval-timeout sweep must not promote"
    guard.assert_not_awaited(), (
        "the refusal must come BEFORE the Alembic guard: a guard that happens "
        "to pass must never be able to let a promotion through"
    )
    await latch_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING, (
        "declining to promote is not a failure of this task"
    )
    ctx = parent.context or {}
    assert ctx.get(HANDMERGE_LATCH_REARM_KEY) == 1, (
        "a permanent refusal must spend budget so the row reaches a terminal "
        "instead of re-deciding the same thing every window forever"
    )
    decision = ctx.get(HANDMERGE_LATCH_DECISION_KEY) or {}
    assert "promotion" in (decision.get("why") or "").lower(), (
        f"the breadcrumb must say WHY it refused, not just that it did: "
        f"{decision!r}"
    )

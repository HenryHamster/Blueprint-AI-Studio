"""Tests for the review-loop operator-park gates.

Incident (lineage d3c12745 / PR #16759, 2026-08-06 03:00-06:15Z): after the
stuck-cycle detector parked a rollup chain for the operator at 04:36
(``requires_approval`` + a ``stuck`` review-loop terminal-outcome breadcrumb),
the rollup sweep kept respawning reviews — 13 of them — because each prior
review had COMPLETED (freeing its active-idempotency key) and nothing checked
the park. Every respawn re-hit the stuck gate on its fix spawn, COUNTED toward
the global pr_iterations recount, and at 16 iterations the cap handler
auto-closed the PR the park had deliberately preserved, destroying a real
landed fix.

Three gates under test:
  A. ``review_loop_parked_for_operator`` + the sweep/stale-discard respawn
     decisions built on it — a parked chain must not get fresh reviews.
  B. ``_handle_cap_exhaustion`` must not override an operator-park (no PR
     close, no FAILED transition on the parked orig).
  C. Refusal status messages must surface the REAL spawn-refusal reason from
     SPAWN_REASON_KEY, not a blanket "iteration cap reached".
"""

import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.models import Task, TaskStatus
from app.review_loop_convergence import (
    _set_review_loop_terminal_outcome,
    clear_review_escalation_approval_gate,
    review_loop_parked_for_operator,
)
from testing.factories import make_project, make_task


@pytest_asyncio.fixture()
async def park_db():
    """Isolated in-memory SQLite session."""
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


async def _make_db_project(session):
    project = make_project()
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


def _park(task, *, kind="stuck", pr_number=16759, iterations=5):
    """Record the park exactly as the production stuck/standoff path does."""
    task.requires_approval = True
    _set_review_loop_terminal_outcome(
        task,
        kind=kind,
        reason=kind,
        pr_number=pr_number,
        iterations=iterations,
        detail="3/3 issues repeated",
    )


# ── A. The park-detection helper ─────────────────────────────────────────────


class TestReviewLoopParkedForOperator:
    def test_stuck_outcome_parks(self):
        task = make_task(status=TaskStatus.EVALUATING)
        _park(task, kind="stuck")
        assert review_loop_parked_for_operator(task) == "stuck"

    def test_standoff_outcome_parks(self):
        task = make_task(status=TaskStatus.EVALUATING)
        _park(task, kind="standoff")
        assert review_loop_parked_for_operator(task) == "standoff"

    def test_fresh_chain_root_is_not_parked(self):
        # FIRST spawn for a fresh rollup PR: no terminal outcome recorded yet —
        # the gate must let the normal spawn proceed.
        task = make_task(status=TaskStatus.EVALUATING)
        assert review_loop_parked_for_operator(task) is None

    def test_cap_exhausted_outcome_is_not_a_park(self):
        # cap_exhausted terminalizes the chain through its own path; the park
        # gate is deliberately narrow to stuck/standoff.
        task = make_task(status=TaskStatus.FAILED)
        _set_review_loop_terminal_outcome(
            task,
            kind="cap_exhausted",
            reason="review_cap_exhausted",
            pr_number=1,
            iterations=16,
        )
        assert review_loop_parked_for_operator(task) is None

    def test_resolved_park_is_released(self):
        # clear_review_escalation_approval_gate stamps resolved=True when the
        # loop demonstrably converges — the gate must stop parking then.
        task = make_task(status=TaskStatus.EVALUATING)
        _park(task, kind="stuck")
        assert clear_review_escalation_approval_gate(
            task, resolved_by="test_convergence"
        )
        assert review_loop_parked_for_operator(task) is None

    def test_none_task_is_not_parked(self):
        assert review_loop_parked_for_operator(None) is None

    def test_requires_approval_alone_is_not_a_park(self):
        # An unrelated hold (spend budget, branch safety) sets the flag without
        # the breadcrumb — it must not suppress review respawns.
        task = make_task(status=TaskStatus.EVALUATING, requires_approval=True)
        assert review_loop_parked_for_operator(task) is None


# ── A. Rollup sweep review-respawn decision ──────────────────────────────────


class TestRollupSweepRespawnGate:
    @pytest.mark.asyncio
    async def test_parked_parent_blocks_respawn_with_truthful_message(self, park_db):
        from app.rollup_management import _rollup_review_respawn_park_message

        project = await _make_db_project(park_db)
        parent = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            payloads=[{"type": "create", "prompt": "research"}],
            context={
                "_is_rollup_review": True,
                "rollup_pr_number": 16759,
                "review_loop": {"pr_number": 16759},
            },
        )
        _park(parent, kind="stuck", pr_number=16759)
        park_db.add(parent)
        await park_db.commit()
        await park_db.refresh(parent)

        msg = _rollup_review_respawn_park_message(parent, 16759)
        assert msg is not None, "Parked parent must not get a review respawn"
        assert "parked for operator (stuck)" in msg
        assert "not respawning review for rollup PR #16759" in msg

    @pytest.mark.asyncio
    async def test_fresh_rollup_pr_first_spawn_proceeds(self, park_db):
        # Regression guard: a parent with no terminal outcome (the normal first
        # spawn for a fresh rollup PR) must not be gated.
        from app.rollup_management import _rollup_review_respawn_park_message

        project = await _make_db_project(park_db)
        parent = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            payloads=[{"type": "create", "prompt": "research"}],
            context={"_is_rollup_review": True, "rollup_pr_number": 42},
        )
        park_db.add(parent)
        await park_db.commit()

        assert _rollup_review_respawn_park_message(parent, 42) is None


# ── A. Stale-discard respawn decision ────────────────────────────────────────


class TestStaleDiscardRespawnGate:
    """The stale-target discard path (task_lifecycle ~:5586) loads the chain
    root by ``original_task_id`` and consults ``review_loop_parked_for_operator``
    before respawning an exact-target review. These tests exercise that exact
    decision (session.get on the persisted root → gate) — driving the full
    agent-status completion handler requires a live GitHub target proof and is
    covered by the existing stale-head tests for the non-parked branch."""

    @pytest.mark.asyncio
    async def test_parked_root_blocks_stale_discard_respawn(self, park_db):
        project = await _make_db_project(park_db)
        orig = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            context={"review_loop": {"pr_number": 16759}},
        )
        _park(orig, kind="stuck", pr_number=16759)
        park_db.add(orig)
        await park_db.commit()

        original_task_id = str(orig.id)
        loaded = await park_db.get(Task, uuid.UUID(original_task_id))
        assert review_loop_parked_for_operator(loaded) == "stuck"

    @pytest.mark.asyncio
    async def test_healthy_root_still_respawns_after_stale_discard(self, park_db):
        # Regression guard: a healthy loop (no breadcrumb) keeps today's
        # discard-and-respawn behavior — the gate returns None.
        project = await _make_db_project(park_db)
        orig = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            context={"review_loop": {"pr_number": 77}},
        )
        park_db.add(orig)
        await park_db.commit()

        loaded = await park_db.get(Task, uuid.UUID(str(orig.id)))
        assert review_loop_parked_for_operator(loaded) is None


# ── B. Cap exhaustion must not override the operator-park ────────────────────


def _mock_cap_side_effects(mocker, *, changed_files=4, mergeable_state="clean"):
    """Patch every external side effect of ``_handle_cap_exhaustion``.

    Patched at the source modules because the handler imports them lazily
    inside the function body. Returns (close_mock, get_pr_mock).
    """
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    close_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    get_pr_mock = mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "open",
            "changed_files": changed_files,
            "mergeable_state": mergeable_state,
        },
    )
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.routers.agent_status.record_task_result")
    mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )
    return close_mock, get_pr_mock


class TestCapExhaustionHonorsPark:
    @pytest.mark.asyncio
    async def test_parked_orig_pr_not_closed_park_preserved(self, park_db, mocker):
        """DEFECT B: cap exhaustion on an operator-parked orig must be a no-op —
        no PR close, no FAILED/CANCELLED transition, park breadcrumb intact."""
        close_mock, get_pr_mock = _mock_cap_side_effects(mocker)

        from app.review_loop_convergence import _handle_cap_exhaustion

        project = await _make_db_project(park_db)
        orig = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
            context={
                "review_loop": {
                    "pr_number": 16759,
                    "review_history": [
                        {"verdict": "CHANGES_REQUESTED", "issues": ["x"]},
                    ],
                },
            },
        )
        _park(orig, kind="stuck", pr_number=16759)
        park_db.add(orig)
        source_task = make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            payloads=[
                {"type": "review", "pr_number": 16759, "original_task_id": str(orig.id)}
            ],
            context={"review_loop": {"pr_number": 16759}},
        )
        park_db.add(source_task)
        await park_db.commit()
        await park_db.refresh(orig)
        await park_db.refresh(source_task)

        await _handle_cap_exhaustion(
            source_task, str(orig.id), 16759, 16, park_db,
            scope="global", verified_disposition="reject"
        )

        await park_db.refresh(orig)
        close_mock.assert_not_called()  # the parked PR stays OPEN
        get_pr_mock.assert_not_called()  # early guard: no preservability GET
        assert orig.status == TaskStatus.EVALUATING, (
            "Cap exhaustion must not terminalize an operator-parked orig"
        )
        assert orig.requires_approval is True, "The operator-park must survive"
        outcome = (orig.context or {}).get("review_loop_terminal_outcome") or {}
        assert outcome.get("kind") == "stuck", (
            "Cap exhaustion must not overwrite the stuck park breadcrumb"
        )
        assert (orig.context or {}).get("failure_detail") is None
        assert (orig.context or {}).get("pr_rejected") is not True

    @pytest.mark.asyncio
    async def test_nonparked_cap_still_closes(self, park_db, mocker):
        """Regression guard: a genuinely churning (non-parked) loop keeps the
        destructive cap path — FAILED + review_cap_exhausted + PR closed."""
        close_mock, _ = _mock_cap_side_effects(mocker)

        from app.review_loop_convergence import _handle_cap_exhaustion

        project = await _make_db_project(park_db)
        orig = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
            context={
                "scope": "medium",
                # No rollup markers, no backmerge_conflict, no park breadcrumb.
                "review_loop": {
                    "pr_number": 720,
                    "review_history": [
                        {"verdict": "CHANGES_REQUESTED", "issues": ["x"]},
                    ],
                },
            },
        )
        park_db.add(orig)
        source_task = make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            payloads=[
                {"type": "fix", "pr_number": 720, "original_task_id": str(orig.id)}
            ],
            context={"review_loop": {"pr_number": 720}},
        )
        park_db.add(source_task)
        await park_db.commit()
        await park_db.refresh(orig)
        await park_db.refresh(source_task)

        await _handle_cap_exhaustion(
            source_task, str(orig.id), 720, 16, park_db,
            scope="per-chain", verified_disposition="reject"
        )

        await park_db.refresh(orig)
        assert orig.status == TaskStatus.FAILED
        assert (orig.context or {}).get("failure_detail") == "review_cap_exhausted"
        assert (orig.context or {}).get("pr_rejected") is True
        close_mock.assert_awaited()  # destructive path still closes the PR


# ── C. Truthful refusal messages ─────────────────────────────────────────────


class TestTruthfulRefusalMessages:
    def test_fix_refusal_surfaces_stuck_cycle_not_cap(self):
        """DEFECT C: when SPAWN_REASON_KEY says stuck_cycle, the refusal message
        must say stuck_cycle — not 'iteration cap reached'."""
        from app.review_pipeline import SPAWN_REASON_KEY, SPAWN_REASON_STUCK
        from app.task_lifecycle import _pipeline_spawn_refusal_label

        task = make_task(context={SPAWN_REASON_KEY: SPAWN_REASON_STUCK})
        label = _pipeline_spawn_refusal_label(task)
        message = f"Fix not spawned for PR #16759 — {label}"
        assert "stuck_cycle" in message
        assert "operator review required" in message
        assert "iteration cap reached" not in message

    def test_fix_refusal_cap_exhausted_still_says_cap(self):
        from app.review_pipeline import SPAWN_REASON_CAP, SPAWN_REASON_KEY
        from app.task_lifecycle import _pipeline_spawn_refusal_label

        task = make_task(context={SPAWN_REASON_KEY: SPAWN_REASON_CAP})
        label = _pipeline_spawn_refusal_label(task)
        assert "cap_exhausted" in label
        assert "iteration cap reached" in label

    def test_unmapped_reason_passes_through_verbatim(self):
        from app.review_pipeline import SPAWN_REASON_KEY

        from app.task_lifecycle import _pipeline_spawn_refusal_label

        task = make_task(context={SPAWN_REASON_KEY: "supersession_churn_halt"})
        assert _pipeline_spawn_refusal_label(task) == "supersession_churn_halt"

    def test_missing_reason_reads_unknown(self):
        from app.task_lifecycle import _pipeline_spawn_refusal_label

        task = make_task(context={})
        assert _pipeline_spawn_refusal_label(task) == "unknown"

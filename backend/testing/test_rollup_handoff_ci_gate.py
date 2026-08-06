"""Tests for ``_resolve_ci_for_unpolled_rollup_parent`` (task_lifecycle).

Regression — the 2026-08-06 PR #16735 treadmill: the rollup CI-gate handoff
merges only when ``review_loop.ci_passed`` is already set, but the orchestrator
CI monitor restricts its poll set to EVALUATING rows. A rollup child that is
already COMPLETED therefore parks every exact-target approval at the gate
forever: ``ci_passed`` never flips, the rollup recovery respawns another
review, and the respawn cap escalates the parent to manual approval. Four
approvals accumulated on PR #16735 (staging base ``research/*`` with zero
check-runs — CI path-filtered out by design) with no merge.

The helper resolves the *definitive* CI states inline with the monitor's own
rules; genuinely pending / unresolved CI keeps parking so a merge never rides
on the absence of a signal.
"""

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.models import Project, TaskStatus
from app.task_lifecycle import _resolve_ci_for_unpolled_rollup_parent
from testing.factories import make_task


@pytest_asyncio.fixture()
async def gate_db():
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


async def _make_parked_parent(
    session, *, base_branch, build_config=None
):
    """A COMPLETED rollup child parked at the CI gate, plus its project."""
    project = Project(name="Gate Test", user_id="user1")
    if build_config is not None:
        project.build_config = build_config
    session.add(project)
    await session.commit()
    await session.refresh(project)

    child = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={
            "review_loop": {
                "pr_number": 16735,
                "branch": "blueprint/task-x",
                "base_branch": base_branch,
                "head_sha": "de8d4694" * 5,
                "ci_waiting": True,
                "ci_passed": False,
                "review_approved": True,
            }
        },
    )
    session.add(child)
    await session.commit()
    await session.refresh(child)
    return child, (child.context or {})["review_loop"]


def _resolved(state, ci_passed=False):
    return {
        "ci_passed": ci_passed,
        "conclusion": "success" if ci_passed else None,
        "head_sha_resolved": "de8d4694" * 5,
        "source": "branch_head" if state != "no_checks" else "unresolved",
        "state": state,
        "details": [],
    }


class TestResolveCiForUnpolledRollupParent:
    @pytest.mark.asyncio
    async def test_green_build_passes(self, gate_db):
        child, loop = await _make_parked_parent(
            gate_db, base_branch="research/44f81121"
        )
        with patch(
            "app.ci_resolution.resolve_pr_ci_status",
            new=AsyncMock(return_value=_resolved("green", ci_passed=True)),
        ):
            ok, state = await _resolve_ci_for_unpolled_rollup_parent(
                gate_db, child, loop, 16735, "tok", "owner", "repo"
            )
        assert ok is True
        assert state == "green"

    @pytest.mark.asyncio
    async def test_no_checks_on_staging_base_passes(self, gate_db):
        # The PR #16735 shape: internal staging base, CI path-filtered out by
        # design — a definitive no_checks must pass so the approval can merge.
        child, loop = await _make_parked_parent(
            gate_db, base_branch="research/44f81121"
        )
        with patch(
            "app.ci_resolution.resolve_pr_ci_status",
            new=AsyncMock(return_value=_resolved("no_checks")),
        ):
            ok, state = await _resolve_ci_for_unpolled_rollup_parent(
                gate_db, child, loop, 16735, "tok", "owner", "repo"
            )
        assert ok is True
        assert state == "no_checks"

    @pytest.mark.asyncio
    async def test_no_checks_on_ci_required_base_keeps_parked(self, gate_db):
        # A CI-capable repo's required branch must never merge on the absence
        # of a signal (the empty-checks window between PR-open and check-run
        # registration).
        child, loop = await _make_parked_parent(
            gate_db,
            base_branch="main",
            build_config={"has_ci": True},
        )
        with patch(
            "app.ci_resolution.resolve_pr_ci_status",
            new=AsyncMock(return_value=_resolved("no_checks")),
        ):
            ok, state = await _resolve_ci_for_unpolled_rollup_parent(
                gate_db, child, loop, 16735, "tok", "owner", "repo"
            )
        assert ok is False
        assert state == "no_checks"

    @pytest.mark.asyncio
    async def test_pending_keeps_parked(self, gate_db):
        child, loop = await _make_parked_parent(
            gate_db, base_branch="research/44f81121"
        )
        with patch(
            "app.ci_resolution.resolve_pr_ci_status",
            new=AsyncMock(return_value=_resolved("pending")),
        ):
            ok, state = await _resolve_ci_for_unpolled_rollup_parent(
                gate_db, child, loop, 16735, "tok", "owner", "repo"
            )
        assert ok is False
        assert state == "pending"

    @pytest.mark.asyncio
    async def test_red_build_keeps_parked(self, gate_db):
        child, loop = await _make_parked_parent(
            gate_db, base_branch="research/44f81121"
        )
        with patch(
            "app.ci_resolution.resolve_pr_ci_status",
            new=AsyncMock(return_value=_resolved("red")),
        ):
            ok, state = await _resolve_ci_for_unpolled_rollup_parent(
                gate_db, child, loop, 16735, "tok", "owner", "repo"
            )
        assert ok is False
        assert state == "red"

    @pytest.mark.asyncio
    async def test_resolver_exception_keeps_parked_never_raises(self, gate_db):
        child, loop = await _make_parked_parent(
            gate_db, base_branch="research/44f81121"
        )
        with patch(
            "app.ci_resolution.resolve_pr_ci_status",
            new=AsyncMock(side_effect=RuntimeError("github down")),
        ):
            ok, state = await _resolve_ci_for_unpolled_rollup_parent(
                gate_db, child, loop, 16735, "tok", "owner", "repo"
            )
        assert ok is False
        assert state == "error"

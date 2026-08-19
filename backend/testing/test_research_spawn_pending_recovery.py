"""Same-lineage recovery regressions for stranded research finalization."""

from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.findings_index import FINALIZED_RESEARCH_FINDINGS_KEY
from app.models import Project, Task, TaskStatus
from app.recovery_research_spawn_pending import (
    RESEARCH_SPAWN_RECOVERY_KEY,
    _claim_markerless_research_spawn,
    _sweep_markerless_research_spawn_pending,
)
from app.schemas import AgentStatusPayload
from app.task_blocked import reset_task_for_fresh_attempt
from app.task_evaluators import QUALITY_GRADER_VERDICT_KEY
from app.time_utils import _utcnow


USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


def _session_factory(db_session):
    return sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def _make_stranded_research(db_session, *, paused: bool = False):
    from test_autonomous_research import _active_direction, _make_finding

    direction = _active_direction(f"d-{uuid.uuid4().hex[:8]}")
    project = Project(
        name=f"markerless-research-{uuid.uuid4().hex[:8]}",
        user_id="markerless-research-test",
        autonomous_enabled=True,
        paused=paused,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.flush()
    finding = _make_finding(
        "Recover the exact persisted finding",
        direction_id=direction["id"],
    )
    task = Task(
        project_id=project.id,
        title="Stranded after sidecar resume",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={
            "direction_id": direction["id"],
            "research_depth": 0,
            "_research_spawn_pending": True,
            QUALITY_GRADER_VERDICT_KEY: {
                "passed": True,
                "reason": "adversarial sidecar passed",
            },
        },
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        plan_output=[finding],
        updated_at=_utcnow() - timedelta(minutes=20),
    )
    db_session.add(task)
    await db_session.commit()
    return project, task, finding


def _patch_recovery_runtime(mocker, db_session):
    Session = _session_factory(db_session)
    mocker.patch("app.services.AsyncSessionLocal", Session)
    mocker.patch("app.orchestrator._mark_orchestrator_progress", return_value=None)
    return Session


@pytest.mark.asyncio
async def test_markerless_sweeps_run_with_zero_pending_markers(db_session, mocker):
    """An empty marker-based page must not short-circuit marker-less repair."""

    from app.services import _sweep_post_commit_pending

    _patch_recovery_runtime(mocker, db_session)
    childless = mocker.patch(
        "app.recovery_post_commit_pending._recover_childless_evaluating_without_marker",
        new=AsyncMock(),
    )
    research = mocker.patch(
        "app.recovery_research_spawn_pending._sweep_markerless_research_spawn_pending",
        new=AsyncMock(),
    )

    await _sweep_post_commit_pending()

    childless.assert_awaited_once()
    research.assert_awaited_once()


@pytest.mark.skipif(not USE_POSTGRES, reason="pause race requires PostgreSQL locks")
@pytest.mark.asyncio
async def test_paused_then_unpaused_replays_exact_same_lineage(db_session, mocker):
    """Pause is a mutation-free hold; unpause replays only stored output."""

    project, task, finding = await _make_stranded_research(db_session, paused=True)
    Session = _patch_recovery_runtime(mocker, db_session)

    async def _finalize_same_attempt(task_id, payload, **kwargs):
        assert task_id == task.id
        assert json.loads(payload.logs) == [finding]
        token = kwargs["_expected_research_spawn_recovery_token"]
        async with Session() as session:
            locked = (
                await session.execute(
                    select(Task).where(Task.id == task_id).with_for_update()
                )
            ).scalar_one()
            assert (locked.context or {})[RESEARCH_SPAWN_RECOVERY_KEY][
                "claim_token"
            ] == token
            context = dict(locked.context or {})
            context.pop("_research_spawn_pending", None)
            context[FINALIZED_RESEARCH_FINDINGS_KEY] = True
            locked.context = context
            flag_modified(locked, "context")
            session.add(locked)
            await session.commit()
        return {"status": "accepted"}

    process = mocker.patch(
        "app.routers.agent_status._process_post_commit",
        new=AsyncMock(side_effect=_finalize_same_attempt),
    )

    await _sweep_markerless_research_spawn_pending()
    process.assert_not_awaited()
    await db_session.refresh(task)
    assert RESEARCH_SPAWN_RECOVERY_KEY not in (task.context or {})

    project.paused = False
    db_session.add(project)
    await db_session.commit()
    await _sweep_markerless_research_spawn_pending()

    process.assert_awaited_once()
    await db_session.refresh(task)
    assert task.id == process.await_args.args[0]
    assert task.plan_output == [finding]
    assert task.context[FINALIZED_RESEARCH_FINDINGS_KEY] is True
    assert "_research_spawn_pending" not in task.context
    assert task.context[RESEARCH_SPAWN_RECOVERY_KEY]["state"] == "recovered"
    research_roots = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.payloads[0]["type"].as_string() == "research",
                )
            )
        )
        .scalars()
        .all()
    )
    assert [row.id for row in research_roots] == [task.id]


@pytest.mark.skipif(not USE_POSTGRES, reason="claim race requires PostgreSQL locks")
@pytest.mark.asyncio
async def test_concurrent_claims_publish_one_exact_attempt_lease(db_session, mocker):
    """Canonical Project -> Task authority admits only one concurrent claim."""

    _project, task, _finding = await _make_stranded_research(db_session)
    _patch_recovery_runtime(mocker, db_session)

    import asyncio

    claims = await asyncio.gather(
        _claim_markerless_research_spawn(task.id),
        _claim_markerless_research_spawn(task.id),
    )

    admitted = [claim for claim in claims if claim is not None]
    assert len(admitted) == 1
    await db_session.refresh(task)
    meta = task.context[RESEARCH_SPAWN_RECOVERY_KEY]
    assert meta["claim_count"] == 1
    assert meta["claim_token"] == admitted[0]["claim_token"]
    assert meta["attempt_identity"] == admitted[0]["attempt_identity"]


@pytest.mark.asyncio
async def test_lifecycle_revalidation_defers_claim_when_pause_wins(db_session, mocker):
    """A pause racing after claim wins before any completion handler runs."""

    from app.task_lifecycle import _process_post_commit

    project, task, finding = await _make_stranded_research(db_session)
    Session = _patch_recovery_runtime(mocker, db_session)
    claim = await _claim_markerless_research_spawn(task.id)
    assert claim is not None

    project.paused = True
    db_session.add(project)
    await db_session.commit()
    handler = mocker.patch(
        "app.research_completion._handle_research_completion",
        new=AsyncMock(),
    )

    result = await _process_post_commit(
        task.id,
        AgentStatusPayload(
            task_id=task.id,
            status=TaskStatus.COMPLETED,
            logs=json.dumps([finding]),
        ),
        _session_factory=Session,
        _expected_research_spawn_recovery_token=claim["claim_token"],
    )

    assert result == {
        "status": "accepted",
        "recovery_skipped": True,
        "recovery_reason": "project_paused",
    }
    handler.assert_not_awaited()
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.plan_output == [finding]


@pytest.mark.asyncio
async def test_lifecycle_revalidation_rejects_fresh_attempt_aba(db_session, mocker):
    """An old token cannot act after the task entered a fresh attempt."""

    from app.task_lifecycle import _process_post_commit

    _project, task, finding = await _make_stranded_research(db_session)
    Session = _patch_recovery_runtime(mocker, db_session)
    claim = await _claim_markerless_research_spawn(task.id)
    assert claim is not None
    old_vm = task.vm_instance_id

    await db_session.refresh(task)
    reset_task_for_fresh_attempt(task)
    task.status = TaskStatus.QUEUED
    db_session.add(task)
    await db_session.commit()
    handler = mocker.patch(
        "app.research_completion._handle_research_completion",
        new=AsyncMock(),
    )

    result = await _process_post_commit(
        task.id,
        AgentStatusPayload(
            task_id=task.id,
            status=TaskStatus.COMPLETED,
            logs=json.dumps([finding]),
        ),
        _session_factory=Session,
        _expected_research_spawn_recovery_token=claim["claim_token"],
    )

    assert result == {
        "status": "accepted",
        "recovery_skipped": True,
        "recovery_reason": "stale_claim",
    }
    handler.assert_not_awaited()
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.vm_instance_id is None
    assert old_vm is not None
    assert RESEARCH_SPAWN_RECOVERY_KEY not in task.context


@pytest.mark.asyncio
async def test_retry_after_post_spawn_crash_dedupes_child_and_finalizes(
    db_session,
    mocker,
):
    """A crash after child commit retries the same parent without duplication."""

    import app.research_completion as research_completion
    from app.research_completion import _handle_research_completion
    from test_autonomous_research import _patch_research_externals

    project, task, finding = await _make_stranded_research(db_session)
    project_id = project.id
    task_id = task.id
    Session = _patch_recovery_runtime(mocker, db_session)
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )

    original_apply = research_completion.apply_post_spawn_state
    failed_once = False

    async def _fail_once_after_child_commit(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("simulated crash after same-lineage child commit")
        return await original_apply(*args, **kwargs)

    mocker.patch(
        "app.research_completion.apply_post_spawn_state",
        new=_fail_once_after_child_commit,
    )

    async def _real_research_handler(task_id, payload, **kwargs):
        token = kwargs["_expected_research_spawn_recovery_token"]
        async with Session() as session:
            source = await session.get(Task, task_id)
            assert source.context[RESEARCH_SPAWN_RECOVERY_KEY]["claim_token"] == token
            await _handle_research_completion(source, payload.logs or "", session)
        return {"status": "accepted"}

    process = mocker.patch(
        "app.routers.agent_status._process_post_commit",
        new=AsyncMock(side_effect=_real_research_handler),
    )

    await _sweep_markerless_research_spawn_pending()
    await db_session.rollback()
    task = (
        await db_session.execute(
            select(Task)
            .where(Task.id == task_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    first_children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task_id)))
        .scalars()
        .all()
    )
    assert failed_once is True
    assert len(first_children) == 1
    assert task.context["_research_spawn_pending"] is True
    assert task.context[RESEARCH_SPAWN_RECOVERY_KEY]["failure_attempts"] == 1

    await _sweep_markerless_research_spawn_pending()
    await db_session.rollback()
    task = (
        await db_session.execute(
            select(Task)
            .where(Task.id == task_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    final_children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task_id)))
        .scalars()
        .all()
    )
    research_roots = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.payloads[0]["type"].as_string() == "research",
                )
            )
        )
        .scalars()
        .all()
    )

    assert process.await_count == 2
    assert len(final_children) == 1
    assert final_children[0].id == first_children[0].id
    assert [row.id for row in research_roots] == [task.id]
    assert task.context[FINALIZED_RESEARCH_FINDINGS_KEY] is True
    assert "_research_spawn_pending" not in task.context
    assert task.context[RESEARCH_SPAWN_RECOVERY_KEY]["state"] == "recovered"


def _patch_research_replay(mocker, finding, Session):
    """Route the recovery sweep into the real research completion handler."""

    from app.research_completion import _handle_research_completion
    from test_autonomous_research import _patch_research_externals

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )

    async def _real_research_handler(task_id, payload, **kwargs):
        async with Session() as session:
            source = await session.get(Task, task_id)
            await _handle_research_completion(source, payload.logs or "", session)
        return {"status": "accepted"}

    return mocker.patch(
        "app.routers.agent_status._process_post_commit",
        new=AsyncMock(side_effect=_real_research_handler),
    )


@pytest.mark.asyncio
async def test_replay_with_completed_sibling_skips_duplicate_and_receipts(
    db_session,
    mocker,
):
    """A COMPLETED sibling already delivering the finding suppresses re-spawn.

    The prior child completed WITHOUT pr_merged, so the bounded hash-dedup
    corpus deliberately leaves the finding eligible for a fresh research cycle
    — exactly the gap that let a replay spawn a second child for the same
    finding. The spawn-boundary sibling filter must drop the duplicate and
    publish a skip receipt on the parent instead.
    """

    from app.finding_deduplication import canonical_finding_body, finding_hash

    project, task, finding = await _make_stranded_research(db_session)
    fhash = finding_hash(finding["title"], canonical_finding_body(finding))
    prior_child = Task(
        project_id=project.id,
        parent_task_id=task.id,
        title=finding["title"],
        payloads=[{"type": "fix"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        origination="RESEARCH",
        context={
            "research_task_id": str(task.id),
            "parent_research_task_id": str(task.id),
            "finding_hash": fhash,
        },
    )
    db_session.add(prior_child)
    await db_session.commit()
    task_id, prior_child_id = task.id, prior_child.id

    Session = _patch_recovery_runtime(mocker, db_session)
    process = _patch_research_replay(mocker, finding, Session)

    await _sweep_markerless_research_spawn_pending()
    await db_session.rollback()

    task = (
        await db_session.execute(
            select(Task)
            .where(Task.id == task_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task_id)))
        .scalars()
        .all()
    )

    assert process.await_count == 1
    assert [row.id for row in children] == [prior_child_id]
    skips = task.context["research_duplicate_finding_skips"]
    assert len(skips) == 1
    assert skips[0]["finding_hash"] == fhash
    assert skips[0]["surviving_task_id"] == str(prior_child_id)
    assert skips[0]["stage"] == "spawn_boundary_replay"
    assert "Skipped 1 duplicate finding child" in (task.latest_status_message or "")
    assert task.context[FINALIZED_RESEARCH_FINDINGS_KEY] is True
    assert "_research_spawn_pending" not in task.context


@pytest.mark.asyncio
async def test_spawned_research_finding_child_pins_idempotency_key(
    db_session,
    mocker,
):
    """Research-finding children carry the parent-scoped finding claim key that
    the ux_task_active_idempotency_key index enforces against concurrent
    duplicates."""

    project, task, finding = await _make_stranded_research(db_session)
    task_id, project_id = task.id, project.id
    Session = _patch_recovery_runtime(mocker, db_session)
    _patch_research_replay(mocker, finding, Session)

    await _sweep_markerless_research_spawn_pending()
    await db_session.rollback()

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task_id)))
        .scalars()
        .all()
    )

    assert len(children) == 1
    child = children[0]
    fhash = child.context["finding_hash"]
    assert fhash
    # Literal prefix pin: the active-key namespace is security-relevant — a
    # renamed prefix would silently stop colliding with rows written before it.
    assert child.idempotency_key.startswith("research-finding:")
    assert child.idempotency_key == f"research-finding:{project_id}:{task_id}:{fhash}"

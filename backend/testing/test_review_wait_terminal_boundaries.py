"""PostgreSQL regressions for review-wait terminal transaction boundaries."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import select

from app.models import (
    Alert,
    MessageType,
    Project,
    ProjectMessage,
    Task,
    TaskHistory,
    TaskStatus,
)


_REVIEW_WAIT_HEAD_SHA = "review-wait-head"
_REVIEW_WAIT_BASE_SHA = "review-wait-base"


@pytest_asyncio.fixture(autouse=True)
async def _patch_dedicated_sessions_to_test_engine(mocker, db_session):
    """Keep any dedicated production session on the shared PostgreSQL engine."""
    dedicated_session_local = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    mocker.patch("app.database.AsyncSessionLocal", dedicated_session_local)


def _make_review_wait_task(db_session, project, *, started_at):
    task = Task(
        project_id=project.id,
        title="CI review-wait terminal task",
        payloads=[
            {
                "type": "review",
                "pr_number": 42,
                "branch": "blueprint/test-branch",
                "base_branch": "main",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 42,
                "branch": "blueprint/test-branch",
                "base_branch": "main",
                "ci_waiting": True,
                "iteration": 1,
                "review_wait_started": started_at,
            }
        },
        vm_instance_id=f"agent-vm-rw-{uuid.uuid4().hex[:8]}",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    return task


@pytest.mark.parametrize("alert_outcome", ["dedup", "error"])
@pytest.mark.asyncio
async def test_repeat_fingerprint_terminal_effects_survive_alert_boundary(
    db_session, mocker, alert_outcome
):
    """The real backstop wrapper cannot roll back terminal repeat evidence."""
    from app.ci_completion import handle_ci_completion
    from app.database import AsyncSessionLocal

    mocker.patch("app.review_pipeline.broadcast_to_project", new=AsyncMock())
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    if alert_outcome == "error":
        mocker.patch(
            "app.alert_dispatch._maybe_create_escalation_alert",
            new=AsyncMock(side_effect=RuntimeError("synthetic alert outage")),
        )

    project = Project(name=f"Review wait repeat {alert_outcome}", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    task = _make_review_wait_task(
        db_session, project, started_at=(now - timedelta(minutes=70)).isoformat()
    )
    await db_session.commit()
    await db_session.refresh(task)
    project_id = project.id
    task_id = task.id
    common_failure = (
        "[wrapper] ./check failed: error while loading shared libraries: "
        "libatk-1.0.so.0"
    )

    def _failed_review(*, created_at, iteration):
        return Task(
            project_id=project_id,
            title=f"Failed review {iteration}",
            payloads=[
                {
                    "type": "review",
                    "pr_number": 42,
                    "branch": "blueprint/test-branch",
                    "base_branch": "main",
                    "original_task_id": str(task_id),
                }
            ],
            context={
                "review_loop": {
                    "iteration": iteration,
                    "original_task_id": str(task_id),
                    "pr_number": 42,
                }
            },
            status=TaskStatus.FAILED,
            created_at=created_at,
            latest_status_message=(
                f"[2026-08-17T01:0{iteration}:00] iter={iteration} "
                f"vm=agent-vm-repeat-{iteration} {common_failure}"
            ),
        )

    earlier = _failed_review(created_at=now - timedelta(minutes=20), iteration=2)
    recent = _failed_review(created_at=now - timedelta(minutes=10), iteration=3)
    db_session.add_all([earlier, recent])
    if alert_outcome == "dedup":
        db_session.add(
            Alert(
                project_id=project_id,
                task_id=task_id,
                title="Review-fix escalation: repeat-fingerprint halt on PR #42",
                message="pre-existing exact alert",
                event_type="review.escalated",
            )
        )
    await db_session.commit()
    earlier_id = earlier.id
    recent_id = recent.id

    result = await handle_ci_completion(
        task,
        {"status": "completed", "conclusion": "success"},
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    await db_session.rollback()
    async with AsyncSessionLocal() as verifying_session:
        persisted_recent = await verifying_session.get(Task, recent_id)
        persisted_original = await verifying_session.get(Task, task_id)
        assert persisted_recent is not None
        assert persisted_original is not None
        recent_ctx = persisted_recent.context or {}
        assert recent_ctx.get("failure_category") == "BLOCKED_PRECONDITION"
        assert recent_ctx.get("blocked_repeat_fingerprint")
        assert recent_ctx.get("blocked_repeat_prior_task_id") == str(earlier_id)
        assert (persisted_recent.latest_status_message or "").startswith("BLOCKER:")

        assert persisted_original.requires_approval is True
        terminal = (persisted_original.context or {}).get(
            "review_loop_terminal_outcome"
        )
        assert terminal is not None
        assert terminal.get("kind") == "repeat_fingerprint_halt"
        assert terminal.get("reason") == "repeat-fingerprint"
        assert (
            (persisted_original.context or {})
            .get("review_loop", {})
            .get("review_wait_spawn_claim")
            is None
        )

        blockers = (
            (
                await verifying_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project_id,
                        ProjectMessage.message_type == MessageType.BLOCKER,
                    )
                )
            )
            .scalars()
            .all()
        )
        alerts = (
            (
                await verifying_session.execute(
                    select(Alert).where(
                        Alert.project_id == project_id,
                        Alert.task_id == task_id,
                        Alert.title
                        == "Review-fix escalation: repeat-fingerprint halt on PR #42",
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(blockers) == 1
    assert blockers[0].context.get("source_task_id") == str(recent_id)
    assert len(alerts) == (1 if alert_outcome == "dedup" else 0)


@pytest.mark.asyncio
async def test_review_wait_real_spawn_atomically_publishes_history_and_is_adopted(
    db_session, mocker
):
    """A committed exact review and its SPAWNED history are adopted together."""
    from app.ci_completion import handle_ci_completion
    from app.database import AsyncSessionLocal

    mocker.patch("app.review_pipeline.broadcast_to_project", new=AsyncMock())
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())
    failure_alert = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

    project = Project(name="Review wait atomic publication", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    started_at = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)
    ).isoformat()
    source = _make_review_wait_task(db_session, project, started_at=started_at)
    await db_session.commit()
    await db_session.refresh(source)
    project_id = project.id
    source_id = source.id
    target = {
        "project_id": str(project_id),
        "pr_number": 42,
        "base_sha": _REVIEW_WAIT_BASE_SHA,
        "head_sha": _REVIEW_WAIT_HEAD_SHA,
    }

    result = await handle_ci_completion(
        source,
        {"status": "completed", "conclusion": "success"},
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    await db_session.rollback()
    async with AsyncSessionLocal() as verifying_session:
        persisted_source = await verifying_session.get(Task, source_id)
        assert persisted_source is not None
        children = (
            (
                await verifying_session.execute(
                    select(Task).where(
                        Task.project_id == project_id,
                        Task.id != source_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        exact_reviews = [
            child
            for child in children
            if child.first_payload.get("type") == "review"
            and ((child.context or {}).get("review_loop") or {}).get(
                "review_target"
            )
            == target
        ]
        assert len(exact_reviews) == 1
        child = exact_reviews[0]
        assert child.status == TaskStatus.QUEUED

        histories = (
            (
                await verifying_session.execute(
                    select(TaskHistory).where(
                        TaskHistory.task_id == child.id,
                        TaskHistory.action == "SPAWNED",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(histories) == 1
        assert histories[0].details == {
            "spawned_by": "REVIEW_PIPELINE",
            "type": "review",
            "pr_number": 42,
        }

        source_loop = (persisted_source.context or {}).get("review_loop") or {}
        assert source_loop.get("review_wait_spawn_claim") is None
        assert source_loop.get("review_wait_respawned") is True
        assert source_loop.get("review_wait_respawn_count") == 1
        assert source_loop.get("review_wait_respawn_task_id") == str(child.id)
        assert source_loop.get("authoritative_review_required") is True
        assert source_loop.get("authoritative_review_task_id") == str(child.id)
        assert source_loop.get("authoritative_review_target") == target
        assert source_loop.get("review_wait_started") != started_at
        assert source_loop.get("review_wait_escalated") is not True
        assert persisted_source.requires_approval is not True

    failure_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_wait_history_construction_failure_cannot_orphan_or_adopt_child(
    db_session, mocker
):
    """A publication failure rolls back child and history before wrapper retry."""
    from app.ci_completion import handle_ci_completion
    from app.database import AsyncSessionLocal

    mocker.patch("app.review_pipeline.broadcast_to_project", new=AsyncMock())
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())
    failure_alert = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

    project = Project(name="Review wait atomic history failure", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    started_at = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)
    ).isoformat()
    source = _make_review_wait_task(db_session, project, started_at=started_at)
    await db_session.commit()
    await db_session.refresh(source)
    project_id = project.id
    source_id = source.id
    mocker.patch(
        "app.review_pipeline.TaskHistory",
        side_effect=RuntimeError("synthetic history construction failure"),
    )

    result = await handle_ci_completion(
        source,
        {"status": "completed", "conclusion": "success"},
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    await db_session.rollback()
    async with AsyncSessionLocal() as verifying_session:
        persisted_source = await verifying_session.get(Task, source_id)
        assert persisted_source is not None
        children = (
            (
                await verifying_session.execute(
                    select(Task).where(
                        Task.project_id == project_id,
                        Task.id != source_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        histories = (
            (
                await verifying_session.execute(
                    select(TaskHistory)
                    .join(Task, TaskHistory.task_id == Task.id)
                    .where(Task.project_id == project_id)
                )
            )
            .scalars()
            .all()
        )
        assert children == []
        assert histories == []

        source_loop = (persisted_source.context or {}).get("review_loop") or {}
        assert source_loop.get("review_wait_spawn_claim") is None
        assert source_loop.get("review_wait_respawned") is not True
        assert source_loop.get("review_wait_respawn_task_id") is None
        assert source_loop.get("authoritative_review_required") is not True
        assert source_loop.get("authoritative_review_task_id") is None
        assert source_loop.get("review_wait_started") == started_at
        assert source_loop.get("ci_poll_interval") == 30
        assert source_loop.get("review_wait_escalated") is not True
        assert persisted_source.requires_approval is not True
        assert "retrying shortly" in (persisted_source.latest_status_message or "")

    failure_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_merged_terminal_effects_are_preserved(db_session, mocker):
    """A real uncommitted already-merged exit completes instead of manual-gating."""
    from app.ci_completion import handle_ci_completion

    mocker.patch("app.review_pipeline.broadcast_to_project", new=AsyncMock())
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

    project = Project(name="Review wait already merged", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    task = _make_review_wait_task(
        db_session, project, started_at=(now - timedelta(minutes=70)).isoformat()
    )
    task.context = {**(task.context or {}), "pr_merged": True}
    db_session.add(task)
    await db_session.commit()

    result = await handle_ci_completion(
        task,
        {"status": "completed", "conclusion": "success"},
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.requires_approval is not True
    assert (task.context or {}).get("review_loop", {}).get(
        "review_wait_escalated"
    ) is not True
    alert_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_rollup_already_merged_backstop_never_installs_manual_gate(
    db_session, mocker
):
    """An already-merged rollup stays with rollup convergence, not a false gate."""
    from app.ci_completion import handle_ci_completion

    mocker.patch("app.review_pipeline.broadcast_to_project", new=AsyncMock())
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

    project = Project(name="Review wait rollup already merged", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    task = _make_review_wait_task(
        db_session, project, started_at=(now - timedelta(minutes=70)).isoformat()
    )
    task.context = {
        **(task.context or {}),
        "_is_rollup_review": True,
        "rollup_pr_number": 42,
        "pr_merged": True,
    }
    db_session.add(task)
    await db_session.commit()

    result = await handle_ci_completion(
        task,
        {"status": "completed", "conclusion": "success"},
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    await db_session.refresh(task)
    review_loop = (task.context or {}).get("review_loop", {})
    assert task.status == TaskStatus.EVALUATING
    assert task.requires_approval is not True
    assert review_loop.get("ci_waiting") is False
    assert review_loop.get("review_wait_started") is None
    assert review_loop.get("review_wait_escalated") is not True
    assert "already merged" in (task.latest_status_message or "").lower()
    alert_mock.assert_not_awaited()


@pytest.mark.parametrize("is_rollup", [False, True], ids=["ordinary", "rollup"])
@pytest.mark.asyncio
async def test_already_merged_releases_exact_backstop_owned_gate(
    db_session, mocker, is_rollup
):
    """Already-merged convergence consumes only its exact automatic hold."""
    from app.ci_completion import handle_ci_completion
    from app.closed_pr_reconciliation import (
        REVIEW_WAIT_APPROVAL_HOLD_KEY,
        REVIEW_WAIT_APPROVAL_HOLD_OWNER,
    )

    mocker.patch("app.review_pipeline.broadcast_to_project", new=AsyncMock())
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

    project = Project(name="Review wait merged owned gate", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    started_at = (now - timedelta(minutes=70)).isoformat()
    approval_set_at = (now - timedelta(minutes=5)).isoformat()
    task = _make_review_wait_task(db_session, project, started_at=started_at)
    review_loop = dict((task.context or {}).get("review_loop") or {})
    review_loop[REVIEW_WAIT_APPROVAL_HOLD_KEY] = {
        "owner": REVIEW_WAIT_APPROVAL_HOLD_OWNER,
        "project_id": str(project.id),
        "pr_number": 42,
        "base_sha": _REVIEW_WAIT_BASE_SHA,
        "head_sha": _REVIEW_WAIT_HEAD_SHA,
        "set_at": approval_set_at,
    }
    task.context = {
        **(task.context or {}),
        "_approval_set_at": approval_set_at,
        "pr_merged": True,
        "review_loop": review_loop,
    }
    if is_rollup:
        task.context = {
            **task.context,
            "_is_rollup_review": True,
            "rollup_pr_number": 42,
        }
    task.requires_approval = True
    db_session.add(task)
    await db_session.commit()

    result = await handle_ci_completion(
        task,
        {"status": "completed", "conclusion": "success"},
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    await db_session.refresh(task)
    persisted_loop = (task.context or {}).get("review_loop", {})
    assert task.status == (
        TaskStatus.EVALUATING if is_rollup else TaskStatus.COMPLETED
    )
    assert task.requires_approval is not True
    assert (task.context or {}).get("_approval_set_at") is None
    assert persisted_loop.get(REVIEW_WAIT_APPROVAL_HOLD_KEY) is None
    if is_rollup:
        assert persisted_loop.get("ci_waiting") is False
        assert persisted_loop.get("review_wait_started") is None
        assert persisted_loop.get("review_wait_escalated") is not True
    alert_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_wait_backstop_preserves_independent_hold_then_retries_on_release(
    db_session, mocker
):
    """A cap/disabled backstop cannot overwrite or latch over another hold."""
    from app.admin_approval_authority import (
        APPROVAL_HOLD_EPISODE_KEY,
        APPROVAL_HOLD_PROVENANCE_KEY,
        approval_hold_provenance,
        establish_independent_approval_hold,
    )
    from app.ci_completion import handle_ci_completion
    from app.shared_state import runtime_config

    mocker.patch("app.review_pipeline.broadcast_to_project", new=AsyncMock())
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task", new_callable=AsyncMock
    )
    mocker.patch.dict(runtime_config, {"review_wait_auto_respawn": False})

    project = Project(name="Review wait independent hold", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    task = _make_review_wait_task(
        db_session, project, started_at=(now - timedelta(minutes=70)).isoformat()
    )
    establish_independent_approval_hold(
        task,
        reason="security-policy-manual-hold",
        owner="test-independent-policy",
    )
    task.latest_status_message = "Security review requires a human decision"
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    owned_hold = approval_hold_provenance(task)
    original_message = task.latest_status_message

    result = await handle_ci_completion(
        task,
        {"status": "completed", "conclusion": "success"},
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    await db_session.refresh(task)
    review_loop = (task.context or {}).get("review_loop", {})
    assert task.requires_approval is True
    assert approval_hold_provenance(task) == owned_hold
    assert task.latest_status_message == original_message
    assert review_loop.get("review_wait_escalated") is not True
    spawn_mock.assert_not_awaited()
    alert_mock.assert_not_awaited()

    # Once that unrelated episode is explicitly released, the same aged
    # review-wait remains eligible for its ordinary disabled/capped escalation.
    released_context = dict(task.context or {})
    released_context.pop("_approval_set_at", None)
    released_context.pop("approval_hold_reason", None)
    released_context.pop(APPROVAL_HOLD_EPISODE_KEY, None)
    released_context.pop(APPROVAL_HOLD_PROVENANCE_KEY, None)
    task.context = released_context
    task.requires_approval = False
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    result = await handle_ci_completion(
        task,
        {"status": "completed", "conclusion": "success"},
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    await db_session.refresh(task)
    assert task.requires_approval is True
    assert (task.context or {}).get("review_loop", {}).get(
        "review_wait_escalated"
    ) is True
    alert_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_fix_admission_refreshes_and_fences_ambiguous_human_merge(
    db_session, mocker
):
    """A stale source cannot publish code after merge submission may have begun."""
    from app.admin_approval_authority import APPROVAL_MERGE_LEDGER_KEY
    from app.database import AsyncSessionLocal
    from app.review_pipeline import SPAWN_REASON_KEY, _spawn_pipeline_task

    mocker.patch("app.review_pipeline.broadcast_to_project", new=AsyncMock())
    project = Project(name="Ambiguous merge fix fence", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    source = Task(
        project_id=project.id,
        title="Stale code-writing source",
        payloads=[
            {
                "type": "create",
                "branch": "blueprint/ambiguous-merge",
                "base_branch": "automain",
                "prompt": "Do not publish after a maybe-sent merge",
            }
        ],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={"review_loop": {"pr_number": 42, "iteration": 1}},
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    project_id = project.id
    source_id = source.id

    ledger = {
        "intent_id": "maybe-sent-intent",
        "state": "ambiguous",
        "project_id": str(project_id),
        "pr_number": 42,
        "head_sha": "head-42",
        "base_sha": "base-42",
    }
    async with AsyncSessionLocal() as peer:
        peer_source = await peer.get(Task, source_id)
        assert peer_source is not None
        peer_source.context = {
            **(peer_source.context or {}),
            APPROVAL_MERGE_LEDGER_KEY: ledger,
        }
        peer.add(peer_source)
        await peer.commit()

    spawned, was_created = await _spawn_pipeline_task(
        source,
        "fix",
        db_session,
        42,
        "blueprint/ambiguous-merge",
        "automain",
        fix_prompt="This code write must be suppressed",
        original_task_id=str(source_id),
    )

    assert spawned is None
    assert was_created is False
    await db_session.rollback()
    async with AsyncSessionLocal() as verifying_session:
        persisted_source = await verifying_session.get(Task, source_id)
        children = (
            (
                await verifying_session.execute(
                    select(Task).where(
                        Task.project_id == project_id,
                        Task.id != source_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        histories = (
            (
                await verifying_session.execute(
                    select(TaskHistory).where(TaskHistory.task_id == source_id)
                )
            )
            .scalars()
            .all()
        )

    assert persisted_source is not None
    assert (persisted_source.context or {})[APPROVAL_MERGE_LEDGER_KEY] == ledger
    assert SPAWN_REASON_KEY not in (persisted_source.context or {})
    assert persisted_source.latest_status_message is None
    assert children == []
    assert histories == []


@pytest.mark.asyncio
async def test_fix_admission_fences_chain_original_merge_from_review_child(
    db_session, mocker
):
    """A review child cannot publish code while its original may be merged."""

    from app.admin_approval_authority import APPROVAL_MERGE_LEDGER_KEY
    from app.database import AsyncSessionLocal
    from app.review_pipeline import SPAWN_REASON_KEY, _spawn_pipeline_task

    mocker.patch("app.review_pipeline.broadcast_to_project", new=AsyncMock())
    project = Project(name="Original merge fix fence", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    original = Task(
        project_id=project.id,
        title="Original with possible merge",
        payloads=[
            {
                "type": "create",
                "branch": "blueprint/original-merge",
                "base_branch": "automain",
            }
        ],
        status=TaskStatus.EVALUATING,
        context={"review_loop": {"pr_number": 43, "iteration": 1}},
    )
    db_session.add(original)
    await db_session.commit()
    await db_session.refresh(original)
    review_child = Task(
        project_id=project.id,
        title="Review child requesting a fix",
        payloads=[
            {
                "type": "review",
                "pr_number": 43,
                "branch": "blueprint/original-merge",
                "base_branch": "automain",
                "original_task_id": str(original.id),
            }
        ],
        status=TaskStatus.EVALUATING,
        context={"review_loop": {"pr_number": 43, "iteration": 1}},
    )
    db_session.add(review_child)
    await db_session.commit()
    await db_session.refresh(review_child)
    project_id = project.id
    original_id = original.id
    review_child_id = review_child.id

    ledger = {
        "intent_id": "original-maybe-sent",
        "state": "ambiguous",
        "project_id": str(project_id),
        "task_id": str(original_id),
        "pr_number": 43,
        "head_sha": "head-43",
        "base_sha": "base-43",
    }
    async with AsyncSessionLocal() as peer:
        peer_original = await peer.get(Task, original_id)
        assert peer_original is not None
        peer_original.context = {
            **(peer_original.context or {}),
            APPROVAL_MERGE_LEDGER_KEY: ledger,
        }
        peer.add(peer_original)
        await peer.commit()

    spawned, created = await _spawn_pipeline_task(
        review_child,
        "fix",
        db_session,
        43,
        "blueprint/original-merge",
        "automain",
        original_task_id=str(original_id),
        fix_prompt="Do not write after possible merge",
    )
    assert spawned is None
    assert created is False
    await db_session.rollback()
    async with AsyncSessionLocal() as verify:
        persisted_original = await verify.get(Task, original_id)
        persisted_review = await verify.get(Task, review_child_id)
        children = (
            (
                await verify.execute(
                    select(Task).where(
                        Task.project_id == project_id,
                        Task.id.notin_([original_id, review_child_id]),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert persisted_original is not None
    assert persisted_review is not None
    assert (persisted_original.context or {})[APPROVAL_MERGE_LEDGER_KEY] == ledger
    assert SPAWN_REASON_KEY not in (persisted_review.context or {})
    assert children == []

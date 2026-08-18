"""PostgreSQL races for exactly-once approval-merge comments."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.admin_approval_authority import (
    APPROVAL_MERGE_LEDGER_KEY,
    ApprovalDecision,
    decide_operator_approval,
)
from app.admin_task_approvals import execute_approval_followup
from app.admin_approval_comment_delivery import (
    acquire_comment_lease,
    complete_comment_lease,
    deliver_approval_merge_comment,
)
from app.admin_approval_merge_protocol import (
    _ledger_history,
    approval_merge_ledger,
)
from app.database import AsyncSessionLocal, resolve_dialect_name
from app.models import Project, Task, TaskStatus


def _require_postgres(session) -> None:
    if resolve_dialect_name(session) != "postgresql":
        pytest.skip("independent-session lock race requires PostgreSQL")


def _response(status_code: int, payload, *, link: str = ""):
    return SimpleNamespace(
        status_code=status_code,
        headers={"Link": link} if link else {},
        text=str(payload),
        json=lambda: payload,
    )


async def _seed_comment_effect(session, *, suffix: str = ""):
    project = Project(
        name=f"Approval comment recovery {suffix}",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    task = Task(
        project_id=project.id,
        title="Merged approval source",
        description="Publish one merge comment",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={},
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    decision = ApprovalDecision(
        task_id=task.id,
        project_id=project.id,
        desired=False,
        was_blocked=True,
        status=TaskStatus.EVALUATING,
        modified_by="USER",
        should_follow_up=True,
        followup_kind="critical_merge",
        claim={"token": f"decision-{suffix}", "status": "evaluating"},
        pr_number=42,
        rollup=False,
        artifact_id=None,
        artifact_pr_url=None,
    )
    ledger = {
        "version": 1,
        "intent_id": f"intent-{suffix}",
        "approval_decision_token": f"decision-{suffix}",
        "task_id": str(task.id),
        "project_id": str(project.id),
        "pr_number": 42,
        "head_sha": "head-42",
        "base_sha": "base-42",
        "head_ref": "blueprint/comment-recovery",
        "base_ref": "automain",
        "followup_kind": "critical_merge",
        "source_status": "evaluating",
        "owner": "owner",
        "repo": "repo",
        "state": "merged",
        "semantic_state": "finalized",
        "transition_seq": 10,
        "post_effects": {
            "comment": "pending",
            "branch_cleanup": "skipped",
            "direction_refresh": "skipped",
            "branch_alert": "skipped",
            "smoketest": "skipped",
        },
    }
    task.context = {APPROVAL_MERGE_LEDGER_KEY: ledger}
    session.add(task)
    session.add(_ledger_history(task, "APPROVAL_MERGE_FINALIZED", ledger))
    await session.commit()
    return project.id, task.id, decision


async def _read_ledger(task_id, decision):
    async with AsyncSessionLocal() as session:
        task = await session.get(Task, task_id, populate_existing=True)
        assert task is not None
        return await approval_merge_ledger(task, decision, session)


async def _expire_comment_lease(task_id) -> None:
    async with AsyncSessionLocal() as session:
        task = (
            (
                await session.execute(
                    select(Task).where(Task.id == task_id).with_for_update()
                )
            )
            .scalars()
            .one()
        )
        context = dict(task.context or {})
        ledger = dict(context[APPROVAL_MERGE_LEDGER_KEY])
        leases = dict(ledger.get("post_effect_leases") or {})
        lease = dict(leases["comment"])
        lease["expires_at_epoch"] = 0
        leases["comment"] = lease
        ledger["post_effect_leases"] = leases
        ledger["transition_seq"] = int(ledger.get("transition_seq") or 0) + 1
        context[APPROVAL_MERGE_LEDGER_KEY] = ledger
        task.context = context
        session.add(task)
        session.add(_ledger_history(task, "APPROVAL_MERGE_POST_EFFECT", ledger))
        await session.commit()


@pytest.mark.asyncio
async def test_page_two_marker_completes_without_post(db_session, mocker):
    _require_postgres(db_session)
    _, task_id, decision = await _seed_comment_effect(db_session, suffix="page2")
    marker = "<!-- blueprint-approval-merge:intent-page2 -->"
    github_request = mocker.patch(
        "app.github._github_request",
        side_effect=[
            _response(
                200,
                [{"body": "some earlier comment"}],
                link=(
                    '<https://api.github.com/repos/owner/repo/issues/42/'
                    'comments?per_page=100&page=2>; rel="next"'
                ),
            ),
            _response(200, [{"body": marker}]),
        ],
    )

    assert await deliver_approval_merge_comment(
        decision, db_session, ("token", "owner", "repo")
    )

    ledger = await _read_ledger(task_id, decision)
    assert ledger["post_effects"]["comment"] == "done"
    assert [call.args[0] for call in github_request.await_args_list] == ["GET", "GET"]


@pytest.mark.asyncio
async def test_lookup_failure_is_retryable_and_never_posts(db_session, mocker):
    _require_postgres(db_session)
    _, task_id, decision = await _seed_comment_effect(db_session, suffix="lookup")
    github_request = mocker.patch(
        "app.github._github_request",
        return_value=_response(503, {"message": "unavailable"}),
    )

    with pytest.raises(RuntimeError, match="lookup failed"):
        await deliver_approval_merge_comment(
            decision, db_session, ("token", "owner", "repo")
        )

    ledger = await _read_ledger(task_id, decision)
    assert ledger["post_effects"]["comment"] == "pending"
    assert ledger["post_effect_leases"]["comment"]["state"] == "pending"
    assert ledger["post_effect_leases"]["comment"]["release_reason"] == (
        "marker_lookup_failed_before_post"
    )
    assert [call.args[0] for call in github_request.await_args_list] == ["GET"]


@pytest.mark.asyncio
async def test_concurrent_retryers_publish_exactly_one_comment(db_session, mocker):
    _require_postgres(db_session)
    _, task_id, decision = await _seed_comment_effect(db_session, suffix="race")
    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()
    methods: list[str] = []

    async def github_request(method, *_args, **_kwargs):
        methods.append(method)
        if method == "GET":
            lookup_started.set()
            await release_lookup.wait()
            return _response(200, [])
        return _response(201, {"id": 7})

    mocker.patch("app.github._github_request", side_effect=github_request)
    async with AsyncSessionLocal() as first, AsyncSessionLocal() as second:
        first_delivery = asyncio.create_task(
            deliver_approval_merge_comment(
                decision, first, ("token", "owner", "repo")
            )
        )
        await lookup_started.wait()
        second_delivery = asyncio.create_task(
            deliver_approval_merge_comment(
                decision, second, ("token", "owner", "repo")
            )
        )
        await asyncio.wait_for(second_delivery, timeout=2)
        release_lookup.set()
        assert await first_delivery is True

    ledger = await _read_ledger(task_id, decision)
    assert ledger["post_effects"]["comment"] == "done"
    assert methods == ["GET", "POST"]


@pytest.mark.asyncio
async def test_crash_after_post_recovers_by_lookup_without_repost(db_session, mocker):
    _require_postgres(db_session)
    _, task_id, decision = await _seed_comment_effect(db_session, suffix="crash")
    marker = "<!-- blueprint-approval-merge:intent-crash -->"
    github_request = mocker.patch(
        "app.github._github_request",
        side_effect=[
            _response(200, []),
            _response(201, {"id": 8}),
            _response(200, [{"body": marker}]),
        ],
    )
    real_complete = __import__(
        "app.admin_approval_comment_delivery", fromlist=["complete_comment_lease"]
    ).complete_comment_lease
    complete = mocker.patch(
        "app.admin_approval_comment_delivery.complete_comment_lease",
        side_effect=RuntimeError("process lost after POST"),
    )
    mocker.patch("app.admin_approval_comment_delivery.COMMENT_LEASE_SECONDS", 0)

    with pytest.raises(RuntimeError, match="process lost"):
        await deliver_approval_merge_comment(
            decision, db_session, ("token", "owner", "repo")
        )
    ledger = await _read_ledger(task_id, decision)
    assert ledger["post_effects"]["comment"] == "submitting"

    complete.side_effect = None
    complete.side_effect = real_complete
    async with AsyncSessionLocal() as retry:
        assert await deliver_approval_merge_comment(
            decision, retry, ("token", "owner", "repo")
        )

    ledger = await _read_ledger(task_id, decision)
    assert ledger["post_effects"]["comment"] == "done"
    assert [call.args[0] for call in github_request.await_args_list] == [
        "GET",
        "POST",
        "GET",
    ]


@pytest.mark.asyncio
async def test_ambiguous_post_failure_keeps_submitting(db_session, mocker):
    _require_postgres(db_session)
    _, task_id, decision = await _seed_comment_effect(db_session, suffix="post-loss")
    methods: list[str] = []

    async def github_request(method, *_args, **_kwargs):
        methods.append(method)
        if method == "GET":
            return _response(200, [])
        raise RuntimeError("comment POST response lost")

    request = mocker.patch("app.github._github_request", side_effect=github_request)
    with pytest.raises(RuntimeError, match="POST response lost"):
        await deliver_approval_merge_comment(
            decision, db_session, ("token", "owner", "repo")
        )

    ledger = await _read_ledger(task_id, decision)
    assert ledger["post_effects"]["comment"] == "submitting"
    assert ledger["post_effect_leases"]["comment"]["state"] == "submitting"
    assert methods == ["GET", "POST"]
    assert request.await_args_list[1].kwargs["max_retries"] == 1


@pytest.mark.asyncio
async def test_credential_mismatch_releases_pending_without_github(db_session, mocker):
    _require_postgres(db_session)
    _, task_id, decision = await _seed_comment_effect(db_session, suffix="credentials")
    github_request = mocker.patch("app.github._github_request")

    with pytest.raises(RuntimeError, match="credentials do not match"):
        await deliver_approval_merge_comment(
            decision, db_session, ("token", "different-owner", "repo")
        )

    ledger = await _read_ledger(task_id, decision)
    assert ledger["post_effects"]["comment"] == "pending"
    assert ledger["post_effect_leases"]["comment"]["state"] == "pending"
    assert ledger["post_effect_leases"]["comment"]["release_reason"] == (
        "credential_target_mismatch_before_post"
    )
    github_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_github_io_runs_without_task_or_project_lock(db_session, mocker):
    _require_postgres(db_session)
    project_id, task_id, decision = await _seed_comment_effect(
        db_session, suffix="unlocked"
    )

    async def github_request(method, *_args, **_kwargs):
        if method == "GET":
            async with AsyncSessionLocal() as peer:
                await peer.execute(
                    select(Project)
                    .where(Project.id == project_id)
                    .with_for_update(nowait=True)
                )
                await peer.execute(
                    select(Task).where(Task.id == task_id).with_for_update(nowait=True)
                )
                await peer.rollback()
            return _response(200, [])
        return _response(201, {"id": 9})

    mocker.patch("app.github._github_request", side_effect=github_request)
    assert await deliver_approval_merge_comment(
        decision, db_session, ("token", "owner", "repo")
    )


@pytest.mark.asyncio
async def test_recovery_token_fences_stale_lease_completion(db_session, mocker):
    _require_postgres(db_session)
    _, task_id, decision = await _seed_comment_effect(db_session, suffix="lease-cas")
    mocker.patch("app.admin_approval_comment_delivery.COMMENT_LEASE_SECONDS", 0)

    first = await acquire_comment_lease(decision, db_session)
    assert first is not None
    recovered = await acquire_comment_lease(decision, db_session)
    assert recovered is not None
    assert recovered.token != first.token

    assert await complete_comment_lease(decision, db_session, first) is False
    ledger = await _read_ledger(task_id, decision)
    assert ledger["post_effects"]["comment"] == "submitting"
    assert ledger["post_effect_leases"]["comment"]["token"] == recovered.token

    assert await complete_comment_lease(decision, db_session, recovered) is True
    ledger = await _read_ledger(task_id, decision)
    assert ledger["post_effects"]["comment"] == "done"


@pytest.mark.asyncio
async def test_public_duplicate_approval_recovers_sole_submitting_comment(
    db_session, mocker
):
    _require_postgres(db_session)
    project_id, task_id, original = await _seed_comment_effect(
        db_session, suffix="public-submitting"
    )
    lease = await acquire_comment_lease(original, db_session)
    assert lease is not None
    await _expire_comment_lease(task_id)
    marker = "<!-- blueprint-approval-merge:intent-public-submitting -->"
    github_request = mocker.patch(
        "app.github._github_request",
        return_value=_response(200, [{"body": marker}]),
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        return_value=("token", "owner", "repo"),
    )

    _, recovery, changes = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )

    assert changes == {}
    assert recovery.project_id == project_id
    assert recovery.merge_recovery is True
    assert recovery.post_effects_only is True
    await execute_approval_followup(recovery, db_session)

    ledger = await _read_ledger(task_id, recovery)
    assert ledger["post_effects"]["comment"] == "done"
    assert [call.args[0] for call in github_request.await_args_list] == ["GET"]


@pytest.mark.asyncio
async def test_expired_stale_lookup_holder_cannot_post_after_takeover(
    db_session, mocker
):
    _require_postgres(db_session)
    _, task_id, decision = await _seed_comment_effect(
        db_session, suffix="stale-before-post"
    )
    first_lookup_started = asyncio.Event()
    release_first_lookup = asyncio.Event()
    methods: list[str] = []
    get_count = 0

    async def github_request(method, *_args, **_kwargs):
        nonlocal get_count
        methods.append(method)
        if method == "GET":
            get_count += 1
            if get_count == 1:
                first_lookup_started.set()
                await release_first_lookup.wait()
            return _response(200, [])
        return _response(201, {"id": 10})

    mocker.patch("app.github._github_request", side_effect=github_request)
    async with AsyncSessionLocal() as first, AsyncSessionLocal() as second:
        stale_delivery = asyncio.create_task(
            deliver_approval_merge_comment(
                decision, first, ("token", "owner", "repo")
            )
        )
        await first_lookup_started.wait()
        await _expire_comment_lease(task_id)
        takeover_delivery = asyncio.create_task(
            deliver_approval_merge_comment(
                decision, second, ("token", "owner", "repo")
            )
        )
        assert await asyncio.wait_for(takeover_delivery, timeout=2) is True
        release_first_lookup.set()
        await stale_delivery

    ledger = await _read_ledger(task_id, decision)
    assert ledger["post_effects"]["comment"] == "done"
    assert methods.count("GET") == 2
    assert methods.count("POST") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([None], id="null-comment-item"),
        pytest.param([{"body": {"unexpected": "object"}}], id="non-string-body"),
    ],
)
async def test_malformed_comment_page_fails_closed_without_post(
    db_session, mocker, payload
):
    _require_postgres(db_session)
    _, task_id, decision = await _seed_comment_effect(
        db_session, suffix=f"malformed-{len(str(payload))}"
    )
    github_request = mocker.patch(
        "app.github._github_request",
        return_value=_response(200, payload),
    )

    with pytest.raises(RuntimeError, match="malformed comment"):
        await deliver_approval_merge_comment(
            decision, db_session, ("token", "owner", "repo")
        )

    ledger = await _read_ledger(task_id, decision)
    assert ledger["post_effects"]["comment"] == "pending"
    assert ledger["post_effect_leases"]["comment"]["state"] == "pending"
    assert [call.args[0] for call in github_request.await_args_list] == ["GET"]


@pytest.mark.asyncio
async def test_comment_get_and_post_carry_project_identity(db_session, mocker):
    _require_postgres(db_session)
    project_id, _, decision = await _seed_comment_effect(
        db_session, suffix="project-identity"
    )
    github_request = mocker.patch(
        "app.github._github_request",
        side_effect=[_response(200, []), _response(201, {"id": 11})],
    )

    assert await deliver_approval_merge_comment(
        decision, db_session, ("token", "owner", "repo")
    )

    assert [call.args[0] for call in github_request.await_args_list] == [
        "GET",
        "POST",
    ]
    assert [call.kwargs.get("project_id") for call in github_request.await_args_list] == [
        str(project_id),
        str(project_id),
    ]

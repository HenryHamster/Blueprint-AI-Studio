"""Durable approval-merge submission and ambiguous-recovery authority tests."""

import asyncio

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from sqlalchemy import select, text, update

from app.admin_task_approvals import (
    APPROVAL_FOLLOWUP_CLAIM_KEY,
    decide_operator_approval,
)
from app.admin_approval_authority import (
    APPROVAL_HOLD_PROVENANCE_KEY,
    approval_merge_in_flight,
    approval_merge_intent_writer,
    approval_merge_is_in_flight,
    approval_merge_ledger,
    approval_merge_receipt_writer,
    approval_merge_submission_starter,
    establish_independent_approval_hold,
    reconcile_approval_merge_ledger,
    recover_approval_merge_decision,
)
from app.admin_approval_merge_protocol import (
    APPROVAL_MERGE_LEDGER_KEY,
    _ledger_history,
    _project_merge_config,
    _promotion_merge_policy_snapshot,
)
from app.database import AsyncSessionLocal, resolve_dialect_name
from app.models import Artifact, ArtifactType, Project, Task, TaskHistory, TaskStatus
from app.review_merge_authority import (
    MergeAuthorityDecision,
    MergePreSubmissionRefused,
    MergeSubmissionIndeterminate,
    attempt_merge_with_authority,
)
from app.review_policy_merge import attempt_explicit_human_merge


def _require_postgres(session) -> None:
    if resolve_dialect_name(session) != "postgresql":
        pytest.skip("independent-session lock race requires PostgreSQL")

async def _seed_regular_approval_attempt(
    db_session,
    *,
    name: str,
    pr_number: int = 42,
    touched_critical: bool = False,
):
    project = Project(
        name=name,
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="Operator approval source",
        description="Review the exact approved attempt",
        status=TaskStatus.EVALUATING,
        payloads=[
            {
                "type": "create",
                "branch": "blueprint/operator-approval",
                "base_branch": "automain",
                "prompt": "Review the exact approved attempt",
            }
        ],
        requires_approval=True,
        context={
            **({"touched_critical": True} if touched_critical else {}),
            "review_loop": {
                "iteration": 1,
                "original_task_id": "",
                "pr_number": pr_number,
                "branch": "blueprint/operator-approval",
                "base_branch": "automain",
                "head_sha": f"head-{pr_number}",
                "base_sha": f"base-{pr_number}",
            },
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task.context = {
        **(task.context or {}),
        "review_loop": {
            **((task.context or {}).get("review_loop") or {}),
            "original_task_id": str(task.id),
        },
    }
    db_session.add(task)
    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
        )
    )
    await db_session.commit()
    await db_session.refresh(task)
    return project, task

def _approval_merge_target(project_id, *, pr_number: int = 42) -> dict:
    return {
        "project_id": str(project_id),
        "pr_number": pr_number,
        "head_sha": f"head-{pr_number}",
        "base_sha": f"base-{pr_number}",
        "head_ref": "blueprint/operator-approval",
        "base_ref": "automain",
        "state": "open",
        "merged": False,
        "draft": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "merge_commit_sha": "",
    }

async def _persist_ambiguous_merge_intent(
    session,
    project,
    decision,
    *,
    pr_number: int = 42,
):
    target = _approval_merge_target(project.id, pr_number=pr_number)
    intent_writer = approval_merge_intent_writer(
        decision,
        owner="owner",
        repo="repo",
        branch="blueprint/operator-approval",
        base_branch="automain",
    )
    ledger = await intent_writer(session, target)
    assert ledger is not None
    assert ledger["state"] == "not_sent"
    starter = approval_merge_submission_starter(decision)
    started = await starter(
        session,
        MergeAuthorityDecision(
            True,
            f"head-{pr_number}",
            f"base-{pr_number}",
            {},
        ),
    )
    assert started is not None
    return target, started

@pytest.mark.asyncio
async def test_merge_receipt_commit_failure_keeps_ambiguous_history(db_session, mocker):
    """A receipt commit failure rolls back to durable ambiguity, never pending."""

    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge receipt constructor failure",
        touched_critical=True,
    )
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    _, started = await _persist_ambiguous_merge_intent(
        db_session, project, decision
    )
    authority_token = await started.acquire_send_lease(db_session)
    assert authority_token
    writer = approval_merge_receipt_writer(
        decision,
        branch="blueprint/operator-approval",
        base_branch="automain",
    )
    mocker.patch.object(
        db_session,
        "commit",
        new=AsyncMock(side_effect=RuntimeError("receipt commit failed")),
    )

    with pytest.raises(RuntimeError, match="receipt commit failed"):
        await writer(
            db_session,
            SimpleNamespace(checked_head_sha="head-42", checked_base_sha="base-42"),
            {
                "merged": True,
                "sha": "merge-old",
                "message": "merged",
                "checked_base_parent_verified": True,
            },
            authority_token,
        )
    await db_session.rollback()

    async with AsyncSessionLocal() as verifying_session:
        source = await verifying_session.get(Task, task_id)
        histories = (
            (
                await verifying_session.execute(
                    select(TaskHistory).where(
                        TaskHistory.task_id == task_id,
                        TaskHistory.action.in_(
                            {
                                "APPROVAL_MERGE_SUBMISSION_STARTED",
                                "APPROVAL_MERGE_SUBMITTED",
                            }
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert source is not None
    claim = (source.context or {}).get(APPROVAL_FOLLOWUP_CLAIM_KEY) or {}
    assert claim.get("state") == "merge_intent"
    assert source.requires_approval is True
    assert [row.action for row in histories] == ["APPROVAL_MERGE_SUBMISSION_STARTED"]
    assert histories[0].details["merge_ledger"]["state"] == "ambiguous"


@pytest.mark.asyncio
async def test_merge_receipt_cannot_bypass_durable_submission_start(db_session):
    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge receipt requires submission start",
        touched_critical=True,
    )
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    intent_writer = approval_merge_intent_writer(
        decision,
        owner="owner",
        repo="repo",
        branch="blueprint/operator-approval",
        base_branch="automain",
    )
    assert await intent_writer(db_session, _approval_merge_target(project.id))
    receipt_writer = approval_merge_receipt_writer(
        decision,
        branch="blueprint/operator-approval",
        base_branch="automain",
    )
    with pytest.raises(RuntimeError, match="ledger changed before receipt"):
        await receipt_writer(
            db_session,
            SimpleNamespace(checked_head_sha="head-42", checked_base_sha="base-42"),
            {
                "merged": True,
                "sha": "merge-without-start",
                "checked_base_parent_verified": True,
            },
            "not-started",
        )
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, db_session)
    assert ledger["state"] == "not_sent"


@pytest.mark.asyncio
async def test_ambiguous_response_loss_recovers_by_get_without_replaying_put(
    db_session, mocker
):
    """Process loss after PUT acceptance is resumed by exact GET only."""

    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge response loss",
        touched_critical=True,
    )
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    target, _started = await _persist_ambiguous_merge_intent(
        db_session, project, decision
    )

    merge_put = AsyncMock()
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.ci_merge_helpers._fetch_live_review_target",
        new=AsyncMock(
            return_value={
                **target,
                "state": "closed",
                "merged": True,
                "merge_commit_sha": "merge-recovered",
            }
        ),
    )
    mocker.patch(
        "app.ci_merge_helpers._merge_commit_parent_matches_checked_base",
        new=AsyncMock(return_value=True),
    )

    async with AsyncSessionLocal() as retry:
        source = await retry.get(Task, task_id, populate_existing=True)
        recovery = await recover_approval_merge_decision(source, retry)
        assert recovery is not None
        assert recovery.merge_recovery is True
        assert await reconcile_approval_merge_ledger(recovery, retry) is True
        await retry.rollback()
        source = await retry.get(Task, task_id, populate_existing=True)
        ledger = await approval_merge_ledger(source, recovery, retry)

    assert merge_put.await_count == 0
    assert ledger is not None
    assert ledger["state"] == "merged"
    assert ledger["merge_fact"]["merge_sha"] == "merge-recovered"
    assert ledger["merge_fact"]["checked_base_parent_verified"] is True


@pytest.mark.asyncio
async def test_ambiguous_retry_probe_outage_never_becomes_refusal_or_put(
    db_session, mocker
):
    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge recovery outage",
        touched_critical=True,
    )
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    await _persist_ambiguous_merge_intent(db_session, project, decision)
    token_lookup = mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(side_effect=RuntimeError("token service unavailable")),
    )

    async with AsyncSessionLocal() as retry:
        source = await retry.get(Task, task_id, populate_existing=True)
        recovery = await recover_approval_merge_decision(source, retry)
        assert recovery is not None
        with pytest.raises(MergeSubmissionIndeterminate):
            await reconcile_approval_merge_ledger(recovery, retry)
        await retry.rollback()
        source = await retry.get(Task, task_id, populate_existing=True)
        claimed = await approval_merge_ledger(source, recovery, retry)
        first_recovery_token = claimed["recovery_token"]

    # A live reconciliation lease prevents a concurrent GET from rejecting the
    # sender window. Expiry then permits a fresh token to take over after crash.
    async with AsyncSessionLocal() as immediate:
        source = await immediate.get(Task, task_id, populate_existing=True)
        recovery = await recover_approval_merge_decision(source, immediate)
        with pytest.raises(MergeSubmissionIndeterminate):
            await reconcile_approval_merge_ledger(recovery, immediate)
    assert token_lookup.await_count == 1

    async with AsyncSessionLocal() as expire:
        source = await expire.get(Task, task_id, populate_existing=True)
        recovery = await recover_approval_merge_decision(source, expire)
        ledger = await approval_merge_ledger(source, recovery, expire)
        ledger["recovery_expires_at"] = "2000-01-01T00:00:00"
        ledger["transition_seq"] += 1
        source.context = {
            **(source.context or {}),
            APPROVAL_MERGE_LEDGER_KEY: ledger,
        }
        expire.add(source)
        expire.add(
            _ledger_history(
                source, "APPROVAL_MERGE_RECONCILIATION_STARTED", ledger
            )
        )
        await expire.commit()

    async with AsyncSessionLocal() as retry:
        source = await retry.get(Task, task_id, populate_existing=True)
        recovery = await recover_approval_merge_decision(source, retry)
        with pytest.raises(MergeSubmissionIndeterminate):
            await reconcile_approval_merge_ledger(recovery, retry)

    async with AsyncSessionLocal() as verify:
        source = await verify.get(Task, task_id, populate_existing=True)
        recovery = await recover_approval_merge_decision(source, verify)
        ledger = await approval_merge_ledger(source, recovery, verify)
    assert ledger["state"] == "reconciling"
    assert ledger["recovery_token"] != first_recovery_token
    assert token_lookup.await_count == 2
    assert source.requires_approval is True


@pytest.mark.asyncio
async def test_ambiguous_exact_unmerged_recovery_requires_time_separated_proofs(
    db_session, mocker
):
    """One negative GET keeps ambiguity; a later exact negative closes it."""

    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge exact-unmerged recovery",
        touched_critical=True,
    )
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    target, _started = await _persist_ambiguous_merge_intent(
        db_session, project, decision
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.ci_merge_helpers._fetch_live_review_target",
        new=AsyncMock(return_value={**target, "base_sha": "advanced-base-42"}),
    )
    first_observed_at = datetime.fromisoformat("2026-08-17T12:00:00")
    recovery_clock = mocker.patch(
        "app.admin_approval_merge_recovery._utcnow",
        return_value=first_observed_at,
    )

    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    recovery = await recover_approval_merge_decision(source, db_session)
    assert recovery is not None
    with pytest.raises(MergeSubmissionIndeterminate):
        await reconcile_approval_merge_ledger(recovery, db_session)

    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, db_session)
    first_proof = ledger.get("negative_merge_proof") or {}
    assert ledger["state"] == "ambiguous"
    assert first_proof["first_observed_at"] == first_observed_at.isoformat()
    assert first_proof["fingerprint"]
    assert first_proof["observation_count"] == 1
    assert await approval_merge_in_flight(source, db_session) is True

    recovery_clock.return_value = first_observed_at + timedelta(minutes=5)
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    recovery = await recover_approval_merge_decision(source, db_session)
    assert recovery is not None
    assert await reconcile_approval_merge_ledger(recovery, db_session) is False

    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, db_session)
    assert ledger["state"] == "submission_rejected"
    assert await approval_merge_in_flight(source, db_session) is False
    assert (source.context or {}).get(APPROVAL_HOLD_PROVENANCE_KEY, {}).get(
        "owner"
    ) == "approval_merge_submission_terminal"


@pytest.mark.asyncio
async def test_negative_recovery_uses_frozen_repo_across_project_config_drift(
    db_session, mocker
):
    """Ambient config drift cannot strand an exact historical merge intent."""

    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge frozen-repo negative recovery",
        touched_critical=True,
    )
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    target, _started = await _persist_ambiguous_merge_intent(
        db_session, project, decision
    )
    async with AsyncSessionLocal() as peer:
        current_project = await peer.get(Project, project.id)
        current_project.github_owner = "replacement-owner"
        current_project.github_repo = "replacement-repo"
        current_project.promotion_target = "release"
        peer.add(current_project)
        await peer.commit()

    current_repo_token = mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(side_effect=AssertionError("ambient repo token is unsafe")),
    )

    async def exact_frozen_repo_token(owner, repo):
        assert (owner, repo) == ("owner", "repo")
        return "frozen-repo-token"

    frozen_token = mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(side_effect=exact_frozen_repo_token),
    )
    frozen_repo_get = mocker.patch(
        "app.ci_merge_helpers._fetch_live_review_target",
        new=AsyncMock(return_value=target),
    )
    first_observed_at = datetime.fromisoformat("2026-08-17T12:00:00")
    recovery_clock = mocker.patch(
        "app.admin_approval_merge_recovery._utcnow",
        return_value=first_observed_at,
    )

    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    recovery = await recover_approval_merge_decision(source, db_session)
    assert recovery is not None
    with pytest.raises(MergeSubmissionIndeterminate):
        await reconcile_approval_merge_ledger(recovery, db_session)

    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, db_session)
    assert ledger["state"] == "ambiguous"
    assert ledger["negative_merge_proof"]["observation_count"] == 1

    recovery_clock.return_value = first_observed_at + timedelta(minutes=5)
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    recovery = await recover_approval_merge_decision(source, db_session)
    assert recovery is not None
    assert await reconcile_approval_merge_ledger(recovery, db_session) is False
    assert frozen_token.await_count == 2
    current_repo_token.assert_not_awaited()
    assert frozen_repo_get.await_args_list == [
        call("frozen-repo-token", "owner", "repo", 42, project_id=str(project.id)),
        call("frozen-repo-token", "owner", "repo", 42, project_id=str(project.id)),
    ]


@pytest.mark.asyncio
async def test_not_sent_head_change_durably_aborts_and_unfences(db_session):
    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge obsolete intent",
        touched_critical=True,
    )
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    writer = approval_merge_intent_writer(
        decision,
        owner="owner",
        repo="repo",
        branch="blueprint/operator-approval",
        base_branch="automain",
    )
    target = _approval_merge_target(project.id)
    assert (await writer(db_session, target))["state"] == "not_sent"
    assert await writer(db_session, {**target, "head_sha": "replacement-head"}) is None

    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, db_session)
    assert ledger["state"] == "unsent_aborted"
    assert await approval_merge_in_flight(source, db_session) is False
    assert (source.context or {}).get(APPROVAL_HOLD_PROVENANCE_KEY, {}).get(
        "owner"
    ) == "approval_merge_unsent_abort"


@pytest.mark.asyncio
async def test_stale_not_sent_history_does_not_fence_independent_hold(db_session):
    """A known-unsent intent fences only while its exact claim still owns the hold."""

    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge stale known-unsent fence",
        touched_critical=True,
    )
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    writer = approval_merge_intent_writer(
        decision,
        owner="owner",
        repo="repo",
        branch="blueprint/operator-approval",
        base_branch="automain",
    )
    assert (await writer(db_session, _approval_merge_target(project.id)))[
        "state"
    ] == "not_sent"

    async with AsyncSessionLocal() as peer:
        source = await peer.get(Task, task_id, populate_existing=True)
        assert source is not None
        source.context = {"replacement_sentinel": "new-attempt"}
        establish_independent_approval_hold(
            source,
            reason="replacement attempt requires approval",
            owner="test-replacement",
        )
        peer.add(source)
        await peer.commit()

    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    assert source is not None
    assert approval_merge_is_in_flight(source) is False
    assert await approval_merge_in_flight(source, db_session) is False
    assert await recover_approval_merge_decision(source, db_session) is None


@pytest.mark.asyncio
async def test_project_config_change_after_start_blocks_second_gate_and_put(
    db_session,
):
    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge second gate config race",
        touched_critical=True,
    )
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    _target, started = await _persist_ambiguous_merge_intent(
        db_session, project, decision
    )
    async with AsyncSessionLocal() as peer:
        peer_project = await peer.get(Project, project.id)
        peer_project.github_repo = "replacement-repo"
        peer.add(peer_project)
        await peer.commit()

    await db_session.rollback()
    assert await started.project_authority(db_session) is False
    assert await started.abort_before_send(db_session) is True
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, db_session)
    assert ledger["state"] == "unsent_aborted"
    assert await approval_merge_in_flight(source, db_session) is False


@pytest.mark.asyncio
async def test_recovery_claim_before_second_gate_invalidates_sender_without_put(
    db_session, mocker
):
    """Recovery owns an exact token before GET; the pre-send process loses CAS."""

    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval recovery wins before second gate",
        touched_critical=True,
    )
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    target, started = await _persist_ambiguous_merge_intent(
        db_session, project, decision
    )
    merge_put = AsyncMock()
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.ci_merge_helpers._fetch_live_review_target",
        new=AsyncMock(return_value=target),
    )

    async with AsyncSessionLocal() as recovery_session:
        source = await recovery_session.get(Task, task_id, populate_existing=True)
        recovery = await recover_approval_merge_decision(source, recovery_session)
        assert recovery is not None
        with pytest.raises(MergeSubmissionIndeterminate):
            await reconcile_approval_merge_ledger(recovery, recovery_session)

    assert await started.acquire_send_lease(db_session) is None
    assert await started.abort_before_send(db_session) is False
    merge_put.assert_not_awaited()
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, db_session)
    assert ledger["state"] == "ambiguous"
    assert ledger["negative_merge_proof"]["fingerprint"]
    assert await approval_merge_in_flight(source, db_session) is True


@pytest.mark.asyncio
async def test_post_send_read_error_is_one_put_then_get_only_recovery(
    db_session, mocker
):
    """A response-lost PUT relinquishes its lease; recovery never replays it."""

    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge post-send ReadError",
        touched_critical=True,
    )
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    writer = approval_merge_intent_writer(
        decision,
        owner="owner",
        repo="repo",
        branch="blueprint/operator-approval",
        base_branch="automain",
    )
    target = _approval_merge_target(project.id)
    assert await writer(db_session, target)
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    assert source is not None

    mocker.patch(
        "app.review_merge_authority.enforce_merge_authority",
        new=AsyncMock(
            return_value=MergeAuthorityDecision(True, "head-42", "base-42", {})
        ),
    )
    live_get = mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        new=AsyncMock(return_value=target),
    )
    merge_put = AsyncMock(side_effect=OSError("response body was lost after send"))
    with pytest.raises(MergeSubmissionIndeterminate):
        await attempt_merge_with_authority(
            source,
            db_session,
            merge_call=merge_put,
            gh_token="token",
            owner="owner",
            repo="repo",
            ci_loop=dict((source.context or {}).get("review_loop") or {}),
            pr_number=42,
            branch="blueprint/operator-approval",
            base_branch="automain",
            original_task_id=str(task_id),
            checked_head_sha="head-42",
            checked_base_sha="base-42",
            explicit_review_override=True,
            submission_start=approval_merge_submission_starter(decision),
            submission_receipt=approval_merge_receipt_writer(
                decision,
                branch="blueprint/operator-approval",
                base_branch="automain",
            ),
        )
    merge_put.assert_awaited_once_with("head-42")
    live_get.assert_awaited_once()

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    recovery_get = mocker.patch(
        "app.ci_merge_helpers._fetch_live_review_target",
        new=AsyncMock(
            side_effect=[
                target,
                {
                    **target,
                    "state": "closed",
                    "merged": True,
                    "merge_commit_sha": "merge-after-read-error",
                },
            ]
        ),
    )
    mocker.patch(
        "app.ci_merge_helpers._merge_commit_parent_matches_checked_base",
        new=AsyncMock(return_value=True),
    )
    async with AsyncSessionLocal() as recovery_session:
        recovered_task = await recovery_session.get(Task, task_id)
        recovery = await recover_approval_merge_decision(
            recovered_task, recovery_session
        )
        assert recovery is not None
        with pytest.raises(MergeSubmissionIndeterminate):
            await reconcile_approval_merge_ledger(recovery, recovery_session)
        await recovery_session.rollback()
        recovered_task = await recovery_session.get(
            Task, task_id, populate_existing=True
        )
        ledger = await approval_merge_ledger(
            recovered_task, recovery, recovery_session
        )
        assert ledger["state"] == "ambiguous"
        assert ledger["negative_merge_proof"]["fingerprint"]
        assert await approval_merge_in_flight(
            recovered_task, recovery_session
        ) is True

    async with AsyncSessionLocal() as recovery_session:
        recovered_task = await recovery_session.get(Task, task_id)
        recovery = await recover_approval_merge_decision(
            recovered_task, recovery_session
        )
        assert recovery is not None
        assert await reconcile_approval_merge_ledger(recovery, recovery_session) is True
    merge_put.assert_awaited_once()
    assert recovery_get.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["target", "config", "fix"])
async def test_every_second_gate_denial_terminalizes_exact_intent_and_unfences(
    db_session, mocker, denial
):
    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name=f"Approval merge exact pre-PUT denial {denial}",
        touched_critical=True,
    )
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    writer = approval_merge_intent_writer(
        decision,
        owner="owner",
        repo="repo",
        branch="blueprint/operator-approval",
        base_branch="automain",
    )
    target = _approval_merge_target(project.id)
    assert await writer(db_session, target)
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    assert source is not None

    gate_calls = 0

    async def controlled_gate(_task, gate_session, **kwargs):
        nonlocal gate_calls
        gate_calls += 1
        if gate_calls == 1:
            return MergeAuthorityDecision(True, "head-42", "base-42", {})
        if denial == "target":
            return MergeAuthorityDecision(True, "head-42", "base-42", {})
        if denial == "config":
            async with AsyncSessionLocal() as peer:
                peer_project = await peer.get(Project, project.id)
                peer_project.github_repo = "replacement-repo"
                peer.add(peer_project)
                await peer.commit()
            assert await kwargs["pre_lock_authority"](gate_session) is False
        return MergeAuthorityDecision(False, "head-42", "base-42", {})

    mocker.patch(
        "app.review_merge_authority.enforce_merge_authority",
        side_effect=controlled_gate,
    )
    live_target = (
        {**target, "head_sha": "replacement-head"}
        if denial == "target"
        else target
    )
    mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        new=AsyncMock(return_value=live_target),
    )
    merge_put = AsyncMock()
    with pytest.raises(MergePreSubmissionRefused):
        await attempt_merge_with_authority(
            source,
            db_session,
            merge_call=merge_put,
            gh_token="token",
            owner="owner",
            repo="repo",
            ci_loop=dict((source.context or {}).get("review_loop") or {}),
            pr_number=42,
            branch="blueprint/operator-approval",
            base_branch="automain",
            original_task_id=str(task_id),
            checked_head_sha="head-42",
            checked_base_sha="base-42",
            explicit_review_override=True,
            submission_start=approval_merge_submission_starter(decision),
        )
    merge_put.assert_not_awaited()
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, db_session)
    assert ledger["state"] == "unsent_aborted"
    assert await approval_merge_in_flight(source, db_session) is False
    assert source.requires_approval is True
    assert (source.context or {}).get(APPROVAL_HOLD_PROVENANCE_KEY, {}).get(
        "owner"
    ) == "approval_merge_submission_terminal"


@pytest.mark.asyncio
async def test_prechecked_promotion_policy_drift_refuses_before_intent_or_put(
    db_session, mocker
):
    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge prechecked policy drift",
        touched_critical=True,
    )
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    await db_session.refresh(project)
    prechecked_config = _project_merge_config(project)
    prechecked_policy = {
        "pr_number": 42,
        "base_branch": "automain",
        "promotion_target": prechecked_config["promotion_target"],
        "migration_guard": "ok",
        "promotion_policy": _promotion_merge_policy_snapshot(
            task,
            prechecked_config,
            base_branch="automain",
            followup_kind=decision.followup_kind,
        ),
    }
    writer = approval_merge_intent_writer(
        decision,
        owner="owner",
        repo="repo",
        branch="blueprint/operator-approval",
        base_branch="automain",
        expected_project_config=prechecked_config,
        prechecked_policy=prechecked_policy,
    )
    async with AsyncSessionLocal() as peer:
        peer_project = await peer.get(Project, project.id)
        peer_project.promotion_target = "release"
        peer.add(peer_project)
        await peer.commit()

    mocker.patch(
        "app.review_policy_merge._fetch_live_review_target",
        new=AsyncMock(return_value=_approval_merge_target(project.id)),
    )
    merge_put = AsyncMock()
    with pytest.raises(MergePreSubmissionRefused):
        await attempt_explicit_human_merge(
            task,
            db_session,
            merge_call=merge_put,
            gh_token="token",
            owner="owner",
            repo="repo",
            pr_number=42,
            branch="blueprint/operator-approval",
            base_branch="automain",
            submission_intent=writer,
        )
    merge_put.assert_not_awaited()
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    assert source.requires_approval is True
    assert (source.context or {}).get(APPROVAL_HOLD_PROVENANCE_KEY, {}).get(
        "owner"
    ) == "approval_merge_precheck_refused"


@pytest.mark.asyncio
async def test_hotfix_promotion_authority_drift_refuses_before_intent_or_put(
    db_session, mocker
):
    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge hotfix policy drift",
        touched_critical=True,
    )
    project.promotion_target = "main"
    task.payloads = [
        {
            **task.first_payload,
            "base_branch": "main",
        }
    ]
    task.context = {
        **(task.context or {}),
        "hotfix": True,
        "review_loop": {
            **((task.context or {}).get("review_loop") or {}),
            "base_branch": "main",
        },
    }
    db_session.add(project)
    db_session.add(task)
    await db_session.commit()
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    await db_session.refresh(project)
    prechecked_config = _project_merge_config(project)
    prechecked_policy = {
        "pr_number": 42,
        "base_branch": "main",
        "promotion_target": "main",
        "migration_guard": "ok",
        "promotion_policy": {
            "followup_kind": "critical_merge",
            "base_branch": "main",
            "integration_branch": "automain",
            "promotion_target": "main",
            "promotion_landing": True,
            "hotfix_lane": True,
            "allowed": True,
        },
    }
    writer = approval_merge_intent_writer(
        decision,
        owner="owner",
        repo="repo",
        branch="blueprint/operator-approval",
        base_branch="main",
        expected_project_config=prechecked_config,
        prechecked_policy=prechecked_policy,
    )
    async with AsyncSessionLocal() as peer:
        peer_task = await peer.get(Task, task_id)
        peer_task.context = {
            **(peer_task.context or {}),
            "hotfix": False,
        }
        peer.add(peer_task)
        await peer.commit()

    target = {
        **_approval_merge_target(project.id),
        "base_ref": "main",
    }
    mocker.patch(
        "app.review_policy_merge._fetch_live_review_target",
        new=AsyncMock(return_value=target),
    )
    merge_put = AsyncMock()
    with pytest.raises(MergePreSubmissionRefused):
        await attempt_explicit_human_merge(
            task,
            db_session,
            merge_call=merge_put,
            gh_token="token",
            owner="owner",
            repo="repo",
            pr_number=42,
            branch="blueprint/operator-approval",
            base_branch="main",
            submission_intent=writer,
        )
    merge_put.assert_not_awaited()
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    assert source.requires_approval is True
    assert (source.context or {}).get(APPROVAL_HOLD_PROVENANCE_KEY, {}).get(
        "owner"
    ) == "approval_merge_precheck_refused"
    assert await approval_merge_in_flight(source, db_session) is False


@pytest.mark.asyncio
async def test_send_lease_releases_rows_for_peer_writes_and_fences_competitors(
    db_session, mocker
):
    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval durable send lease lock release",
        touched_critical=True,
    )
    project_id = project.id
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    writer = approval_merge_intent_writer(
        decision,
        owner="owner",
        repo="repo",
        branch="blueprint/operator-approval",
        base_branch="automain",
    )
    target = _approval_merge_target(project_id)
    assert await writer(db_session, target)
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)

    mocker.patch(
        "app.review_merge_authority.enforce_merge_authority",
        new=AsyncMock(
            return_value=MergeAuthorityDecision(True, "head-42", "base-42", {})
        ),
    )
    get_started = asyncio.Event()
    release_get = asyncio.Event()

    async def blocked_final_get(*_args, **_kwargs):
        get_started.set()
        await release_get.wait()
        return target

    mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        side_effect=blocked_final_get,
    )
    merge_put = AsyncMock(
        return_value={"merged": False, "conflict": True, "message": "conflict"}
    )
    attempt = asyncio.create_task(
        attempt_merge_with_authority(
            source,
            db_session,
            merge_call=merge_put,
            gh_token="token",
            owner="owner",
            repo="repo",
            ci_loop=dict((source.context or {}).get("review_loop") or {}),
            pr_number=42,
            branch="blueprint/operator-approval",
            base_branch="automain",
            original_task_id=str(task_id),
            checked_head_sha="head-42",
            checked_base_sha="base-42",
            explicit_review_override=True,
            submission_start=approval_merge_submission_starter(decision),
        )
    )
    await asyncio.wait_for(get_started.wait(), timeout=5)
    try:
        async with AsyncSessionLocal() as peer:
            await peer.execute(text("SET LOCAL lock_timeout = '500ms'"))
            await peer.execute(
                update(Project).where(Project.id == project_id).values(name="peer-write")
            )
            await peer.execute(
                update(Task).where(Task.id == task_id).values(title="peer-task-write")
            )
            await peer.commit()
        async with AsyncSessionLocal() as competitor:
            competing_source = await competitor.get(Task, task_id)
            assert await approval_merge_in_flight(competing_source, competitor) is True
            competing_started = approval_merge_submission_starter(decision)
            assert (
                await competing_started(
                    competitor,
                    MergeAuthorityDecision(True, "head-42", "base-42", {}),
                )
                is None
            )
    finally:
        release_get.set()
    gate, result = await attempt
    assert gate.allowed is True
    assert result["conflict"] is True
    merge_put.assert_awaited_once_with("head-42")


@pytest.mark.asyncio
async def test_project_config_drift_during_final_get_revokes_send_lease_before_put(
    db_session, mocker
):
    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval send lease config drift",
        touched_critical=True,
    )
    project_id = project.id
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    writer = approval_merge_intent_writer(
        decision,
        owner="owner",
        repo="repo",
        branch="blueprint/operator-approval",
        base_branch="automain",
    )
    target = _approval_merge_target(project_id)
    assert await writer(db_session, target)
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)

    mocker.patch(
        "app.review_merge_authority.enforce_merge_authority",
        new=AsyncMock(
            return_value=MergeAuthorityDecision(True, "head-42", "base-42", {})
        ),
    )
    get_started = asyncio.Event()
    release_get = asyncio.Event()

    async def blocked_final_get(*_args, **_kwargs):
        get_started.set()
        await release_get.wait()
        return target

    mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        side_effect=blocked_final_get,
    )
    merge_put = AsyncMock(
        return_value={"merged": False, "conflict": True, "message": "conflict"}
    )
    attempt = asyncio.create_task(
        attempt_merge_with_authority(
            source,
            db_session,
            merge_call=merge_put,
            gh_token="token",
            owner="owner",
            repo="repo",
            ci_loop=dict((source.context or {}).get("review_loop") or {}),
            pr_number=42,
            branch="blueprint/operator-approval",
            base_branch="automain",
            original_task_id=str(task_id),
            checked_head_sha="head-42",
            checked_base_sha="base-42",
            explicit_review_override=True,
            submission_start=approval_merge_submission_starter(decision),
        )
    )
    await asyncio.wait_for(get_started.wait(), timeout=5)
    try:
        async with AsyncSessionLocal() as peer:
            peer_project = await peer.get(Project, project_id)
            peer_project.promotion_target = "release"
            peer.add(peer_project)
            await peer.commit()
    finally:
        release_get.set()
    with pytest.raises(MergePreSubmissionRefused):
        await attempt
    merge_put.assert_not_awaited()
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, db_session)
    assert ledger["state"] == "unsent_aborted"
    assert await approval_merge_in_flight(source, db_session) is False
    assert source.requires_approval is True
    assert (source.context or {}).get(APPROVAL_HOLD_PROVENANCE_KEY, {}).get(
        "owner"
    ) == "approval_merge_submission_terminal"

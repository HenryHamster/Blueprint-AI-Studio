"""Fresh-session races for operator approval authority.

These tests intentionally use independent sessions.  The bugs they protect
against are identity-map/transaction races and cannot be proven by mutating two
ORM objects in one session.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select, text, update

from app.admin_task_approvals import (
    APPROVAL_FOLLOWUP_CLAIM_KEY,
    ApprovalFollowupRefused,
    _finalize_approval_merge,
    _maybe_spawn_rollup_smoketest,
    decide_operator_approval,
    execute_approval_followup,
)
from app.admin_approval_authority import (
    APPROVAL_HOLD_PROVENANCE_KEY,
    APPROVAL_MERGE_FACT_KEY,
    APPROVAL_MERGE_LEDGER_KEY,
    approval_merge_in_flight,
    approval_merge_intent_writer,
    approval_merge_ledger,
    approval_merge_receipt_writer,
    approval_merge_submission_starter,
    establish_independent_approval_hold,
)
from app.admin_approval_merge_effects import run_approval_merge_post_effects
from app.admin_approval_merge_protocol import _ledger_history
from app.admin_task_mutations import modify_task_apply
from app.closed_pr_reconciliation import (
    REVIEW_WAIT_APPROVAL_HOLD_KEY,
    REVIEW_WAIT_APPROVAL_HOLD_OWNER,
    reconcile_closed_pr_replacement,
)
from app.database import AsyncSessionLocal, resolve_dialect_name
from app.models import (
    Artifact,
    ArtifactType,
    Alert,
    Project,
    ProjectMember,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.review_merge_authority import (
    MergeAuthorityDecision,
    MergeSubmissionIndeterminate,
    attempt_merge_with_authority,
)


def _require_postgres(session) -> None:
    if resolve_dialect_name(session) != "postgresql":
        pytest.skip("independent-session lock race requires PostgreSQL")


def _owned_hold(project_id, pr_number: int, timer: str, head: str, base: str):
    return {
        "owner": REVIEW_WAIT_APPROVAL_HOLD_OWNER,
        "project_id": str(project_id),
        "pr_number": pr_number,
        "head_sha": head,
        "base_sha": base,
        "set_at": timer,
    }


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


async def _persist_finalized_effect_ledger(
    session,
    project,
    task,
    decision,
    *,
    effects: dict,
):
    _, started = await _persist_ambiguous_merge_intent(session, project, decision)
    authority_token = await started.acquire_send_lease(session)
    assert authority_token
    writer = approval_merge_receipt_writer(
        decision,
        branch="blueprint/operator-approval",
        base_branch="automain",
    )
    await writer(
        session,
        SimpleNamespace(checked_head_sha="head-42", checked_base_sha="base-42"),
        {
            "merged": True,
            "sha": "merge-effects",
            "message": "merged",
            "checked_base_parent_verified": True,
        },
        authority_token,
    )
    await session.rollback()
    source = await session.get(Task, task.id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, session)
    ledger["semantic_state"] = "finalized"
    ledger["transition_seq"] += 1
    ledger["landed_branch"] = "automain"
    ledger["post_effects"] = dict(effects)
    context = dict(source.context or {})
    context.pop(APPROVAL_FOLLOWUP_CLAIM_KEY, None)
    context[APPROVAL_MERGE_LEDGER_KEY] = ledger
    source.context = context
    source.requires_approval = False
    source.status = TaskStatus.COMPLETED
    session.add(source)
    session.add(_ledger_history(source, "APPROVAL_MERGE_FINALIZED", ledger))
    await session.commit()
    return ledger


@pytest.mark.asyncio
async def test_false_decision_refreshes_replaced_task_and_artifact(db_session):
    """FALSE locks and refreshes both authority row and regular-PR identity."""

    _require_postgres(db_session)
    project = Project(name="Approval replacement race", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "branch": "feat/old"}],
        requires_approval=True,
        context={
            "_approval_set_at": "old-timer",
            "review_loop": {
                "pr_number": 77,
                "head_sha": "old-head",
                "base_sha": "old-base",
                REVIEW_WAIT_APPROVAL_HOLD_KEY: _owned_hold(
                    project.id, 77, "old-timer", "old-head", "old-base"
                ),
            },
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/owner/repo/pull/77",
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    # Route authorization has already populated both identities. Replacement
    # wins in a genuinely independent transaction before the mutation helper.
    async with AsyncSessionLocal() as peer:
        peer_task = await peer.get(Task, task.id)
        peer_artifact = await peer.get(Artifact, artifact.id)
        peer_task.context = {
            "_approval_set_at": "new-timer",
            "replacement_sentinel": "keep-fresh-row",
            "review_loop": {
                "pr_number": 91,
                "head_sha": "new-head",
                "base_sha": "new-base",
                REVIEW_WAIT_APPROVAL_HOLD_KEY: _owned_hold(
                    project.id, 91, "new-timer", "new-head", "new-base"
                ),
            },
        }
        peer_artifact.pr_url = "https://github.com/owner/repo/pull/91"
        peer.add(peer_task)
        peer.add(peer_artifact)
        await peer.commit()

    fresh, decision, _ = await decide_operator_approval(
        task.id, False, db_session, modified_by="USER"
    )

    # Follow-up-required FALSE remains gated until the exact child or merge
    # publication consumes the durable, resumable claim.
    assert fresh.requires_approval is True
    assert (decision.claim or {}).get("state") == "pending"
    assert (fresh.context or {}).get("replacement_sentinel") == "keep-fresh-row"
    claim_episode = (decision.claim or {}).get("approval_episode") or {}
    approval_set_at = (fresh.context or {}).get("_approval_set_at")
    provenance = (fresh.context or {}).get(APPROVAL_HOLD_PROVENANCE_KEY) or {}
    assert approval_set_at != "new-timer"
    assert approval_set_at == claim_episode.get("set_at")
    assert provenance.get("episode") == claim_episode.get("episode")
    assert provenance.get("set_at") == approval_set_at
    assert REVIEW_WAIT_APPROVAL_HOLD_KEY not in (
        (fresh.context or {}).get("review_loop") or {}
    )
    assert decision.pr_number == 91
    assert decision.artifact_pr_url.endswith("/91")
    assert (decision.claim or {}).get("head_sha") == "new-head"


@pytest.mark.asyncio
async def test_interrupted_false_retry_resumes_claim_and_atomically_spawns(
    db_session, mocker
):
    """Process loss after the decision neither clears the gate nor duplicates work."""

    _require_postgres(db_session)
    mocker.patch("app.review_pipeline.broadcast_to_project", new=AsyncMock())
    project, task = await _seed_regular_approval_attempt(
        db_session, name="Interrupted operator approval"
    )
    project_id = project.id
    task_id = task.id

    first_task, first_decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    token = (first_decision.claim or {}).get("token")
    assert token
    assert first_task.requires_approval is True

    # A new request/session after process loss must resume the durable receipt,
    # not clear the gate, mint a second token, or append a second decision row.
    async with AsyncSessionLocal() as retry_session:
        retry_task, retry_decision, retry_changes = await decide_operator_approval(
            task_id, False, retry_session, modified_by="USER"
        )
        assert retry_task.requires_approval is True
        assert (retry_decision.claim or {}).get("token") == token
        assert retry_changes == {}
        completed_task, followup_changes = await execute_approval_followup(
            retry_decision, retry_session
        )
        assert completed_task.requires_approval is False
        assert followup_changes.get("spawned_review")

    await db_session.rollback()
    async with AsyncSessionLocal() as verifying_session:
        source = await verifying_session.get(Task, task_id)
        children = (
            (
                await verifying_session.execute(
                    select(Task).where(
                        Task.project_id == project_id,
                        Task.id != task_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        histories = (
            (
                await verifying_session.execute(
                    select(TaskHistory).where(
                        TaskHistory.task_id.in_(
                            [task_id] + [row.id for row in children]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )

    assert source is not None
    assert source.requires_approval is False
    assert (source.context or {}).get(APPROVAL_FOLLOWUP_CLAIM_KEY) is None
    assert len(children) == 1
    child = children[0]
    assert child.first_payload.get("type") == "review"
    assert ((child.context or {}).get("review_loop") or {}).get("review_target") == {
        "project_id": str(project_id),
        "pr_number": 42,
        "base_sha": "base-42",
        "head_sha": "head-42",
    }
    pending_rows = [
        row
        for row in histories
        if ((row.details or {}).get("changes") or {}).get("approval_followup_pending")
        is True
    ]
    consumed_rows = [
        row
        for row in histories
        if ((row.details or {}).get("changes") or {}).get("requires_approval") is False
    ]
    assert len(pending_rows) == 1
    assert len(consumed_rows) == 1
    assert len([row for row in histories if row.action == "SPAWNED"]) == 1


@pytest.mark.asyncio
async def test_refusal_rearms_current_claim_and_audits(db_session, mocker):
    _require_postgres(db_session)
    _project, task = await _seed_regular_approval_attempt(
        db_session, name="Approval refusal"
    )
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    mocker.patch(
        "app.admin_task_approvals._handle_task_pr_approval",
        new=AsyncMock(
            side_effect=ApprovalFollowupRefused(
                "Approved merge refused by exact guard",
                issue_code="approval_test_guard",
            )
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await execute_approval_followup(decision, db_session)
    assert exc_info.value.status_code == 409

    await db_session.rollback()
    async with AsyncSessionLocal() as verifying_session:
        source = await verifying_session.get(Task, task_id)
        histories = (
            (
                await verifying_session.execute(
                    select(TaskHistory).where(TaskHistory.task_id == task_id)
                )
            )
            .scalars()
            .all()
        )
    assert source is not None
    assert source.requires_approval is True
    assert (source.context or {}).get(APPROVAL_FOLLOWUP_CLAIM_KEY) is None
    assert source.latest_status_message == "Approved merge refused by exact guard"
    assert any(
        ((row.details or {}).get("changes") or {}).get("approval_refused")
        == "approval_test_guard"
        for row in histories
    )


@pytest.mark.asyncio
async def test_refusal_after_replacement_is_source_neutral(db_session, mocker):
    """A guard verdict derived from an old claim cannot re-gate a peer attempt."""

    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session, name="Approval refusal replacement race"
    )
    task_id = task.id
    project_id = project.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )

    async def _replace_then_refuse(*_args, **_kwargs):
        async with AsyncSessionLocal() as peer:
            peer_task = await peer.get(Task, task_id)
            peer_project = await peer.get(Project, project_id)
            assert peer_task is not None
            assert peer_project is not None
            peer_task.requires_approval = True
            peer_task.context = {
                "approval_hold_reason": "independent-manual-hold",
                "replacement_sentinel": "preserve",
                "review_loop": {
                    "pr_number": 91,
                    "head_sha": "head-91",
                    "base_sha": "base-91",
                },
            }
            peer_project.problem_model = {"refusal_race_sentinel": "preserve"}
            peer.add(peer_task)
            peer.add(peer_project)
            await peer.commit()
        raise ApprovalFollowupRefused(
            "Stale guard verdict", issue_code="stale_guard_must_not_persist"
        )

    mocker.patch(
        "app.admin_task_approvals._handle_task_pr_approval",
        new=AsyncMock(side_effect=_replace_then_refuse),
    )
    with pytest.raises(HTTPException) as exc_info:
        await execute_approval_followup(decision, db_session)
    assert exc_info.value.status_code == 409

    await db_session.rollback()
    async with AsyncSessionLocal() as verifying_session:
        source = await verifying_session.get(Task, task_id)
        fresh_project = await verifying_session.get(Project, project_id)
    assert source is not None
    assert fresh_project is not None
    assert source.requires_approval is True
    assert (source.context or {}).get("replacement_sentinel") == "preserve"
    assert (source.context or {}).get("approval_hold_reason") == (
        "independent-manual-hold"
    )
    assert ((source.context or {}).get("review_loop") or {}).get("pr_number") == 91
    assert fresh_project.problem_model == {"refusal_race_sentinel": "preserve"}


@pytest.mark.asyncio
async def test_merge_receipt_survives_replacement_without_finalizing_peer(db_session):
    """An irreversible old merge is audited but cannot complete a new attempt."""

    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge receipt replacement",
        touched_critical=True,
    )
    project_id = project.id
    task_id = task.id
    artifact = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task_id)))
        .scalars()
        .one()
    )
    artifact_id = artifact.id
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

    async with AsyncSessionLocal() as peer:
        peer_task = await peer.get(Task, task_id)
        peer_artifact = await peer.get(Artifact, artifact_id)
        assert peer_task is not None
        assert peer_artifact is not None
        peer_task.status = TaskStatus.EVALUATING
        peer_task.context = {
            "replacement_sentinel": "keep-new-attempt",
            "review_loop": {
                "pr_number": 91,
                "head_sha": "head-91",
                "base_sha": "base-91",
            },
        }
        establish_independent_approval_hold(
            peer_task,
            reason="new-independent-hold",
            owner="test-new-attempt",
        )
        peer_artifact.pr_url = "https://github.com/owner/repo/pull/91"
        peer.add(peer_task)
        peer.add(peer_artifact)
        await peer.commit()

    changes = {}
    assert await _finalize_approval_merge(decision, db_session, changes) is not None
    assert changes == {"approval_merge_fact_recorded": 42}

    await db_session.rollback()
    async with AsyncSessionLocal() as verifying_session:
        source = await verifying_session.get(Task, task_id)
        receipt_histories = (
            (
                await verifying_session.execute(
                    select(TaskHistory).where(
                        TaskHistory.task_id == task_id,
                        TaskHistory.action == "APPROVAL_MERGE_SUBMITTED",
                    )
                )
            )
            .scalars()
            .all()
        )
        finalized_histories = (
            (
                await verifying_session.execute(
                    select(TaskHistory).where(
                        TaskHistory.task_id == task_id,
                        TaskHistory.action == "APPROVAL_MERGE_FINALIZED",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert source is not None
    assert source.project_id == project_id
    assert source.requires_approval is True
    assert source.status == TaskStatus.EVALUATING
    assert (source.context or {}).get("replacement_sentinel") == "keep-new-attempt"
    assert (source.context or {}).get("approval_hold_reason") == (
        "new-independent-hold"
    )
    assert (source.context or {}).get(APPROVAL_FOLLOWUP_CLAIM_KEY) is None
    assert (source.context or {}).get(APPROVAL_HOLD_PROVENANCE_KEY, {}).get(
        "owner"
    ) == "test-new-attempt"
    fact = (source.context or {}).get(APPROVAL_MERGE_FACT_KEY) or {}
    assert fact.get("current_attempt_mutated") is False
    assert fact.get("merge_fact", {}).get("merge_sha") == "merge-old"
    assert (source.context or {}).get("pr_merged") is not True
    assert len(receipt_histories) == 1
    assert receipt_histories[0].details["merge_receipt"]["merge_sha"] == "merge-old"
    assert len(finalized_histories) == 1
    assert finalized_histories[0].details["merge_ledger"]["semantic_state"] == (
        "fact_only_finalized"
    )




@pytest.mark.asyncio
async def test_project_and_task_updates_remain_available_during_merge_transport(
    db_session, mocker
):
    """The durable send lease replaces database locks during GitHub I/O."""

    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge Project lock through PUT",
        touched_critical=True,
    )
    project_id = project.id
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
    assert await intent_writer(db_session, _approval_merge_target(project_id))
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    assert source is not None

    gate_calls = 0
    gate_decision = MergeAuthorityDecision(True, "head-42", "base-42", {})

    async def exact_gate(_task, gate_session, **kwargs):
        nonlocal gate_calls
        gate_calls += 1
        if gate_calls == 2:
            assert await kwargs["pre_lock_authority"](gate_session) is True
            assert await kwargs["source_authority"](gate_session) is not None
        return gate_decision

    mocker.patch(
        "app.review_merge_authority.enforce_merge_authority",
        side_effect=exact_gate,
    )
    mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        new=AsyncMock(return_value=_approval_merge_target(project_id)),
    )
    merge_call = AsyncMock()

    async def available_peer_updates(_head_sha):
        async with AsyncSessionLocal() as peer:
            await peer.execute(text("SET LOCAL lock_timeout = '250ms'"))
            await peer.execute(
                update(Project)
                .where(Project.id == project_id)
                .values(name="peer-project-write")
            )
            await peer.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(title="peer-task-write")
            )
            await peer.commit()
        return {"merged": False, "conflict": True, "message": "conflict"}

    merge_call.side_effect = available_peer_updates
    gate, result = await attempt_merge_with_authority(
        source,
        db_session,
        merge_call=merge_call,
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

    assert gate.allowed is True
    assert result == {"merged": False, "conflict": True, "message": "conflict"}
    assert gate_calls == 2
    merge_call.assert_awaited_once_with("head-42")
    await db_session.rollback()
    assert (await db_session.get(Project, project_id, populate_existing=True)).name == (
        "peer-project-write"
    )
    assert (await db_session.get(Task, task_id, populate_existing=True)).title == (
        "peer-task-write"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_kind", "expected_state"),
    [
        ("rate_limit", "known_not_sent"),
        ("pat_fallback_blocked", "known_not_sent"),
        ("http_409", "submission_rejected"),
        ("http_405", "submission_rejected"),
        ("http_405_missing_merged", "ambiguous"),
        ("http_405_malformed_merged", "ambiguous"),
    ],
)
async def test_definitive_merge_transport_outcomes_terminalize_and_unfence(
    db_session, mocker, response_kind, expected_state
):
    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name=f"Approval merge terminal transport {response_kind}",
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
    target = _approval_merge_target(project.id)
    assert await intent_writer(db_session, target)
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    assert source is not None

    mocker.patch(
        "app.review_merge_authority.enforce_merge_authority",
        new=AsyncMock(
            return_value=MergeAuthorityDecision(True, "head-42", "base-42", {})
        ),
    )
    observed_target = {**target, "base_sha": "advanced-base-42"}
    if response_kind == "http_405_missing_merged":
        observed_target.pop("merged")
    elif response_kind == "http_405_malformed_merged":
        observed_target["merged"] = "false"
    live_target_fetch = mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        new=AsyncMock(
            side_effect=(
                [target, observed_target]
                if response_kind.startswith("http_405")
                else None
            ),
            return_value=target,
        ),
    )
    merge_call = AsyncMock()
    if response_kind == "rate_limit":
        rate_limit = HTTPException(status_code=429, detail="rate limited before PUT")
        rate_limit.merge_not_attempted = True
        merge_call.side_effect = rate_limit
    elif response_kind == "pat_fallback_blocked":
        # Refused locally before any PUT: the only credential resolvable for
        # this lane was the operator's own PAT. Nothing was sent, so the ledger
        # must read known_not_sent — never submission_rejected (a receipt
        # claiming GitHub rejected a request that never existed) and never
        # ambiguous (which would fence the task for reconciliation).
        merge_call.return_value = {
            "merged": False,
            "conflict": False,
            "pat_fallback_blocked": True,
            "message": "refusing to merge with an operator PAT credential",
        }
    elif response_kind == "http_409":
        merge_call.return_value = {
            "merged": False,
            "conflict": True,
            "message": "head changed",
        }
    else:
        merge_call.return_value = {
            "merged": False,
            "conflict": False,
            "message": "not mergeable",
        }

    attempt = attempt_merge_with_authority(
        source,
        db_session,
        merge_call=merge_call,
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
    if response_kind == "rate_limit":
        with pytest.raises(HTTPException) as exc_info:
            await attempt
        assert exc_info.value.status_code == 429
    elif expected_state == "ambiguous":
        with pytest.raises(MergeSubmissionIndeterminate):
            await attempt
    else:
        _gate, result = await attempt
        assert result is not None
        assert result["merged"] is False

    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, db_session)
    assert ledger["state"] == expected_state
    assert await approval_merge_in_flight(source, db_session) is (
        expected_state == "ambiguous"
    )
    assert source.requires_approval is True
    if expected_state != "ambiguous":
        assert (source.context or {}).get(APPROVAL_HOLD_PROVENANCE_KEY, {}).get(
            "owner"
        ) == "approval_merge_submission_terminal"
    merge_call.assert_awaited_once_with("head-42")
    assert live_target_fetch.await_count == (
        2 if response_kind.startswith("http_405") else 1
    )


@pytest.mark.asyncio
async def test_checked_base_mismatch_is_fact_only_and_alerts_once(db_session):
    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge checked base mismatch",
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
    await writer(
        db_session,
        SimpleNamespace(checked_head_sha="head-42", checked_base_sha="base-42"),
        {
            "merged": True,
            "sha": "merge-wrong-parent",
            "message": "merged after external base advance",
            "checked_base_parent_verified": False,
        },
        authority_token,
    )

    changes = {}
    assert await _finalize_approval_merge(decision, db_session, changes) is not None
    assert await _finalize_approval_merge(decision, db_session, {}) is not None
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.task_id == task_id,
                    Alert.event_type == "approval.merge_checked_base_mismatch",
                )
            )
        )
        .scalars()
        .all()
    )
    assert source.requires_approval is True
    assert source.status == TaskStatus.EVALUATING
    assert (source.context or {}).get("pr_merged") is not True
    assert (source.context or {}).get(APPROVAL_MERGE_FACT_KEY, {}).get(
        "semantic_block_reason"
    ) == "checked_base_parent_mismatch"
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_comment_failure_stays_retryable_and_cleanup_is_never_deleted(
    db_session, mocker
):
    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge post effect retry",
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
    await writer(
        db_session,
        SimpleNamespace(checked_head_sha="head-42", checked_base_sha="base-42"),
        {
            "merged": True,
            "sha": "merge-effects",
            "message": "merged",
            "checked_base_parent_verified": True,
        },
        authority_token,
    )
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, db_session)
    ledger["semantic_state"] = "finalized"
    ledger["transition_seq"] += 1
    ledger["post_effects"] = {
        "comment": "pending",
        "branch_cleanup": "skipped",
        "direction_refresh": "skipped",
        "branch_alert": "skipped",
        "smoketest": "skipped",
    }
    source.context = {**(source.context or {}), APPROVAL_MERGE_LEDGER_KEY: ledger}
    db_session.add(source)
    db_session.add(_ledger_history(source, "APPROVAL_MERGE_FINALIZED", ledger))
    await db_session.commit()

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    marker_lookup = mocker.patch(
        "app.approval_comment_github.approval_comment_marker_exists_strict",
        new=AsyncMock(
            side_effect=[RuntimeError("marker lookup failed before POST"), False]
        ),
    )
    comment = mocker.patch(
        "app.approval_comment_github.create_approval_comment_once",
        new=AsyncMock(return_value={"id": 1}),
    )
    delete_branch = mocker.patch("app.github.delete_branch", new=AsyncMock())
    await run_approval_merge_post_effects(decision, db_session)
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, db_session)
    assert ledger["post_effects"]["comment"] == "pending"
    assert comment.await_count == 0

    await run_approval_merge_post_effects(decision, db_session)
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, db_session)
    assert ledger["post_effects"]["comment"] == "done"
    assert ledger["post_effects"]["branch_cleanup"] == "skipped"
    assert marker_lookup.await_count == 2
    assert comment.await_count == 1
    assert delete_branch.await_count == 0


@pytest.mark.asyncio
async def test_direction_effect_requires_verified_success_before_done(
    db_session, mocker
):
    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge direction effect retry",
        touched_critical=True,
    )
    task_id = task.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    await _persist_finalized_effect_ledger(
        db_session,
        project,
        task,
        decision,
        effects={
            "comment": "skipped",
            "branch_cleanup": "skipped",
            "direction_refresh": "pending",
            "branch_alert": "skipped",
            "smoketest": "skipped",
        },
    )
    refresh = mocker.patch(
        "app.admin_task_mutations._refresh_direction_outcomes_for_task",
        new=AsyncMock(side_effect=[False, True]),
    )

    await run_approval_merge_post_effects(decision, db_session)
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, db_session)
    assert ledger["post_effects"]["direction_refresh"] == "pending"

    await run_approval_merge_post_effects(decision, db_session)
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, db_session)
    assert ledger["post_effects"]["direction_refresh"] == "done"
    assert refresh.await_count == 2


@pytest.mark.asyncio
async def test_smoketest_effect_retries_by_intent_without_duplicate_child(
    db_session, mocker
):
    """A crash after child commit dedups the child before completing the effect."""

    _require_postgres(db_session)
    project, task = await _seed_regular_approval_attempt(
        db_session,
        name="Approval merge smoketest intent dedup",
        touched_critical=True,
    )
    task_id = task.id
    project_id = project.id
    _, decision, _ = await decide_operator_approval(
        task_id, False, db_session, modified_by="USER"
    )
    seeded = await _persist_finalized_effect_ledger(
        db_session,
        project,
        task,
        decision,
        effects={
            "comment": "skipped",
            "branch_cleanup": "skipped",
            "direction_refresh": "skipped",
            "branch_alert": "skipped",
            "smoketest": "pending",
        },
    )
    intent_id = str(seeded["intent_id"])
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.admin_task_mutations.broadcast_to_project",
        new=AsyncMock(),
    )
    from app.admin_approval_merge_finalization import (
        record_approval_merge_post_effect as real_record_effect,
    )
    from app.shared_state import runtime_config

    record_calls = 0

    async def fail_once_then_record(*args, **kwargs):
        nonlocal record_calls
        record_calls += 1
        if record_calls == 1:
            raise RuntimeError("process lost after smoketest child commit")
        return await real_record_effect(*args, **kwargs)

    mocker.patch(
        "app.admin_approval_merge_finalization.record_approval_merge_post_effect",
        side_effect=fail_once_then_record,
    )
    mocker.patch.dict(runtime_config, {"smoketest_enabled": True})

    await run_approval_merge_post_effects(decision, db_session)
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, db_session)
    assert ledger["post_effects"]["smoketest"] == "pending"

    await run_approval_merge_post_effects(decision, db_session)
    await db_session.rollback()
    source = await db_session.get(Task, task_id, populate_existing=True)
    ledger = await approval_merge_ledger(source, decision, db_session)
    children = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.parent_task_id == task_id,
                )
            )
        )
        .scalars()
        .all()
    )
    spawned = (
        (
            await db_session.execute(
                select(TaskHistory).where(
                    TaskHistory.task_id.in_([child.id for child in children]),
                    TaskHistory.action == "SPAWNED",
                )
            )
        )
        .scalars()
        .all()
    )
    assert ledger["post_effects"]["smoketest"] == "done"
    assert len(children) == 1
    assert len(spawned) == 1
    assert spawned[0].details["spawned_by"] == (f"ADMIN_APPROVAL_MERGE:{intent_id}")
    assert record_calls == 2


@pytest.mark.asyncio
async def test_false_then_independent_hold_then_replacement_revokes_followup(
    db_session, mocker
):
    """A hold installed after FALSE survives replacement and blocks old action."""

    _require_postgres(db_session)
    project = Project(name="Approval replacement revocation", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "branch": "feat/same"}],
        requires_approval=True,
        context={
            "_approval_set_at": "owned-timer",
            "review_loop": {
                "pr_number": 77,
                "branch": "feat/same",
                "base_branch": "automain",
                "head_sha": "old-head",
                "base_sha": "old-base",
                REVIEW_WAIT_APPROVAL_HOLD_KEY: _owned_hold(
                    project.id, 77, "owned-timer", "old-head", "old-base"
                ),
            },
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/77",
        )
    )
    await db_session.commit()

    _, decision, _ = await decide_operator_approval(
        task.id, False, db_session, modified_by="USER"
    )
    assert decision.should_follow_up is True

    async with AsyncSessionLocal() as peer:
        peer_task = await peer.get(Task, task.id)
        peer_task.requires_approval = True
        peer_context = dict(peer_task.context or {})
        peer_context["_approval_set_at"] = "independent-timer"
        peer_context["approval_hold_reason"] = "manual"
        peer_task.context = peer_context
        peer.add(peer_task)
        await peer.commit()

    async with AsyncSessionLocal() as replacement_session:
        replacement_task = await replacement_session.get(Task, task.id)
        replacement_loop = dict(
            (replacement_task.context or {}).get("review_loop") or {}
        )
        assert await reconcile_closed_pr_replacement(
            task=replacement_task,
            session=replacement_session,
            review_loop=replacement_loop,
            pr_number=77,
            pr_data={
                "head": {"ref": "feat/same", "sha": "old-head"},
                "base": {"ref": "automain", "sha": "old-base"},
            },
            replacement_lookup={
                "status": "ok",
                "replacement_pr": {
                    "number": 91,
                    "state": "open",
                    "head": {"ref": "feat/same", "sha": "new-head"},
                    "base": {"ref": "automain", "sha": "new-base"},
                },
            },
        )

    handler = mocker.patch(
        "app.admin_task_approvals._handle_task_pr_approval",
        new_callable=AsyncMock,
    )
    fresh, changes = await execute_approval_followup(decision, db_session)

    assert changes == {}
    assert fresh.requires_approval is True
    assert (fresh.context or {}).get("_approval_set_at") == "independent-timer"
    assert (fresh.context or {}).get("approval_hold_reason") == "manual"
    assert ((fresh.context or {}).get("review_loop") or {}).get("pr_number") == 91
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_bulk_approve_discards_prefetch_before_decision(
    client, db_session, mocker
):
    """Bulk membership prefetch is never mutation authority."""

    _require_postgres(db_session)
    project = Project(name="Bulk approval fresh row", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id="test-user-123",
            role="owner",
        )
    )
    task = Task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "compute_experiment", "command": "true"}],
        requires_approval=True,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    from app import admin as admin_module

    real_decide = admin_module.decide_operator_approval
    raced = False

    async def _race_then_decide(*args, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            async with AsyncSessionLocal() as peer:
                peer_task = await peer.get(Task, task.id)
                peer_task.context = {"bulk_race_sentinel": "fresh"}
                peer.add(peer_task)
                await peer.commit()
        return await real_decide(*args, **kwargs)

    mocker.patch(
        "app.admin.decide_operator_approval",
        new=AsyncMock(side_effect=_race_then_decide),
    )
    response = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "approve", "task_ids": [str(task.id)]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["success_count"] == 1
    await db_session.refresh(task)
    assert task.requires_approval is False
    assert (task.context or {}).get("bulk_race_sentinel") == "fresh"
    histories = (
        (
            await db_session.execute(
                select(TaskHistory).where(TaskHistory.task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert any(
        (row.details or {}).get("modified_by") == "BULK_ACTION"
        and ((row.details or {}).get("changes") or {}).get("requires_approval") is False
        for row in histories
    )


@pytest.mark.asyncio
async def test_bulk_approve_three_no_followup_tasks_keeps_session_usable(
    client, db_session
):
    """Each committed decision leaves later prefetched items safe to process."""

    _require_postgres(db_session)
    project = Project(name="Bulk approval three tasks", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id="test-user-123",
            role="owner",
        )
    )
    tasks = [
        Task(
            project_id=project.id,
            status=TaskStatus.QUEUED,
            payloads=[{"type": "compute_experiment", "command": f"echo {index}"}],
            requires_approval=True,
        )
        for index in range(3)
    ]
    db_session.add_all(tasks)
    await db_session.commit()
    for task in tasks:
        await db_session.refresh(task)
    task_ids = [task.id for task in tasks]

    response = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "approve", "task_ids": [str(task_id) for task_id in task_ids]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success_count"] == 3
    assert body["error_count"] == 0
    assert [row["status"] for row in body["results"]] == ["ok", "ok", "ok"]
    await db_session.rollback()
    async with AsyncSessionLocal() as verifying_session:
        fresh_tasks = (
            (await verifying_session.execute(select(Task).where(Task.id.in_(task_ids))))
            .scalars()
            .all()
        )
        histories = (
            (
                await verifying_session.execute(
                    select(TaskHistory).where(TaskHistory.task_id.in_(task_ids))
                )
            )
            .scalars()
            .all()
        )
    assert len(fresh_tasks) == 3
    assert all(task.requires_approval is False for task in fresh_tasks)
    approved_ids = {
        row.task_id
        for row in histories
        if (row.details or {}).get("modified_by") == "BULK_ACTION"
        and ((row.details or {}).get("changes") or {}).get("requires_approval") is False
    }
    assert approved_ids == set(task_ids)


@pytest.mark.asyncio
async def test_rollup_smoketest_history_failure_publishes_no_child(db_session, mocker):
    """A dispatchable smoketest and its SPAWNED audit are one transaction."""

    _require_postgres(db_session)
    project = Project(name="Rollup smoketest atomic history", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "plan", "base_branch": "automain"}],
        context={"rollup_pr_number": 42},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    project_id = project.id
    task_id = task.id
    mocker.patch(
        "app.admin_approval_followup_handlers.TaskHistory",
        side_effect=RuntimeError("smoketest history construction failed"),
    )
    broadcast = mocker.patch(
        "app.admin_task_mutations.broadcast_to_project", new=AsyncMock()
    )

    await _maybe_spawn_rollup_smoketest(task, "owner", "repo", "automain", db_session)
    await db_session.rollback()

    async with AsyncSessionLocal() as verifying_session:
        children = (
            (
                await verifying_session.execute(
                    select(Task).where(
                        Task.project_id == project_id,
                        Task.id != task_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        spawned_histories = (
            (
                await verifying_session.execute(
                    select(TaskHistory).where(TaskHistory.action == "SPAWNED")
                )
            )
            .scalars()
            .all()
        )
    assert children == []
    assert spawned_histories == []
    broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_status_and_approval_are_status_first_audited_transactions(
    db_session,
):
    project = Project(name="Mixed approval mutation", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create"}],
        requires_approval=False,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    result = await modify_task_apply(
        task,
        {"status": TaskStatus.CANCELLED.value, "requires_approval": True},
        BackgroundTasks(),
        db_session,
    )

    assert result["status"] == TaskStatus.CANCELLED
    assert result["requires_approval"] is True
    histories = (
        (
            await db_session.execute(
                select(TaskHistory).where(TaskHistory.task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    change_sets = [(row.details or {}).get("changes") or {} for row in histories]
    assert {"status": TaskStatus.CANCELLED.value} in change_sets
    assert {"requires_approval": True} in change_sets
    assert len(histories) == 2

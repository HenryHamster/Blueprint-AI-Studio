"""Exact-status authority for operator-approval review deduplication."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.admin_approval_authority import (
    APPROVAL_FOLLOWUP_CLAIM_KEY,
    APPROVAL_HOLD_EPISODE_KEY,
    APPROVAL_HOLD_PROVENANCE_KEY,
    consume_approval_followup_after_dedup,
    decide_operator_approval,
)
from app.closed_pr_reconciliation import (
    REVIEW_WAIT_APPROVAL_HOLD_KEY,
    REVIEW_WAIT_APPROVAL_HOLD_OWNER,
    owns_review_wait_approval_hold,
)
from app.database import AsyncSessionLocal, resolve_dialect_name
from app.models import Artifact, ArtifactType, Project, Task, TaskStatus


ACTIVE_REVIEW_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
    TaskStatus.BLOCKED,
    TaskStatus.PARKED,
)
TERMINAL_REVIEW_STATUSES = (
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
)


def _require_postgres(session) -> None:
    if resolve_dialect_name(session) != "postgresql":
        pytest.skip("independent approval-authority proof requires PostgreSQL")


async def _seed_pending_review_followup(
    db_session,
    *,
    review_status: TaskStatus,
    claim_owned_hold_metadata: bool = False,
):
    project = Project(
        name=f"Approval dedup {review_status.value}",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    source = Task(
        project_id=project.id,
        title="Approval source",
        description="Resume only through a live exact review",
        status=TaskStatus.EVALUATING,
        payloads=[
            {
                "type": "create",
                "branch": "blueprint/operator-approval",
                "base_branch": "automain",
                "prompt": "Review the exact attempt",
            }
        ],
        requires_approval=True,
        context={
            **(
                {
                    "approval_hold_reason": "stalled review escalation",
                    "review_loop_terminal_outcome": {
                        "kind": "review_stuck",
                        "resolved": False,
                    },
                }
                if claim_owned_hold_metadata
                else {}
            ),
            "review_loop": {
                "iteration": 1,
                "pr_number": 42,
                "branch": "blueprint/operator-approval",
                "base_branch": "automain",
                "head_sha": "head-42",
                "base_sha": "base-42",
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 42,
                    "base_sha": "base-42",
                    "head_sha": "head-42",
                },
            }
        },
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    source.context = {
        **(source.context or {}),
        "review_loop": {
            **((source.context or {}).get("review_loop") or {}),
            "original_task_id": str(source.id),
        },
    }
    db_session.add(source)
    db_session.add(
        Artifact(
            task_id=source.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/42",
        )
    )
    await db_session.commit()

    _, decision, _ = await decide_operator_approval(
        source.id,
        False,
        db_session,
        modified_by="USER",
    )
    expected_target = {
        "project_id": str(project.id),
        "pr_number": 42,
        "base_sha": "base-42",
        "head_sha": "head-42",
    }
    review = Task(
        project_id=project.id,
        parent_task_id=source.id,
        title="Exact approval review",
        description="Existing exact-target review",
        status=review_status,
        payloads=[
            {
                "type": "review",
                "pr_number": 42,
                "branch": "blueprint/operator-approval",
                "base_branch": "automain",
                "original_task_id": str(source.id),
            }
        ],
        context={"review_loop": {"review_target": expected_target}},
    )
    db_session.add(review)
    await db_session.commit()
    await db_session.refresh(review)
    return source.id, review.id, decision


@pytest.mark.asyncio
async def test_exact_review_dedup_consumes_claim_owned_hold_metadata(db_session):
    """A consumed episode cannot poison the next review-wait backstop hold."""

    _require_postgres(db_session)
    source_id, review_id, decision = await _seed_pending_review_followup(
        db_session,
        review_status=TaskStatus.QUEUED,
        claim_owned_hold_metadata=True,
    )
    claim = dict(decision.claim or {})
    assert claim["approval_hold_reason"] == "stalled review escalation"
    assert claim["review_loop_terminal_outcome"] == {
        "kind": "review_stuck",
        "resolved": False,
    }

    assert await consume_approval_followup_after_dedup(
        decision,
        review_id,
        db_session,
    )
    await db_session.rollback()

    async with AsyncSessionLocal() as verifying_session:
        source = await verifying_session.get(Task, source_id)
        assert source is not None
        context = dict(source.context or {})
        assert "approval_hold_reason" not in context
        assert "review_loop_terminal_outcome" not in context
        assert "_approval_set_at" not in context
        assert APPROVAL_HOLD_EPISODE_KEY not in context
        assert APPROVAL_HOLD_PROVENANCE_KEY not in context

        timer = "2026-08-17T12:00:00+00:00"
        loop = dict(context.get("review_loop") or {})
        loop[REVIEW_WAIT_APPROVAL_HOLD_KEY] = {
            "owner": REVIEW_WAIT_APPROVAL_HOLD_OWNER,
            "project_id": str(source.project_id),
            "pr_number": 42,
            "head_sha": "head-42",
            "base_sha": "base-42",
            "set_at": timer,
        }
        source.requires_approval = True
        source.context = {**context, "_approval_set_at": timer, "review_loop": loop}
        assert owns_review_wait_approval_hold(
            source,
            loop,
            pr_number=42,
            head_sha="head-42",
            base_sha="base-42",
        )


@pytest.mark.asyncio
async def test_immediate_false_consumes_only_current_hold_episode(db_session):
    """A no-follow-up release clears its hold metadata but keeps evidence."""

    _require_postgres(db_session)
    project = Project(name="Immediate approval release", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    episode = "immediate-release-episode"
    timer = "2026-08-17T12:15:00+00:00"
    task = Task(
        project_id=project.id,
        title="Terminal approval hold",
        description="No child or merge follow-up remains",
        status=TaskStatus.FAILED,
        payloads=[{"type": "create", "prompt": "No follow-up"}],
        requires_approval=True,
        context={
            "_approval_set_at": timer,
            APPROVAL_HOLD_EPISODE_KEY: episode,
            APPROVAL_HOLD_PROVENANCE_KEY: {
                "episode": episode,
                "owner": "review_escalation",
                "reason": "terminal review escalation",
                "set_at": timer,
            },
            "approval_hold_reason": "terminal review escalation",
            "review_loop_terminal_outcome": {
                "kind": "review_stuck",
                "resolved": False,
            },
            "touched_critical": ["preserve-evidence.py"],
        },
    )
    db_session.add(task)
    await db_session.commit()
    task_id = task.id

    fresh, decision, _changes = await decide_operator_approval(
        task_id,
        False,
        db_session,
        modified_by="USER",
    )
    assert decision.should_follow_up is False
    assert fresh.requires_approval is False
    context = dict(fresh.context or {})
    assert "_approval_set_at" not in context
    assert APPROVAL_HOLD_EPISODE_KEY not in context
    assert APPROVAL_HOLD_PROVENANCE_KEY not in context
    assert "approval_hold_reason" not in context
    assert "review_loop_terminal_outcome" not in context
    assert context["touched_critical"] == ["preserve-evidence.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("review_status", "should_consume"),
    [
        *((status, True) for status in ACTIVE_REVIEW_STATUSES),
        *((status, False) for status in TERMINAL_REVIEW_STATUSES),
    ],
    ids=lambda value: value.value if isinstance(value, TaskStatus) else str(value),
)
async def test_exact_review_dedup_consumes_only_live_statuses(
    db_session,
    review_status: TaskStatus,
    should_consume: bool,
):
    """A terminal same-key race never inherits operator approval authority."""

    _require_postgres(db_session)
    source_id, review_id, decision = await _seed_pending_review_followup(
        db_session,
        review_status=review_status if should_consume else TaskStatus.QUEUED,
    )
    claim = dict(decision.claim or {})
    assert claim.get("token")

    if not should_consume:
        # Model the IntegrityError fallback race precisely: the row held the
        # active idempotency slot when insertion collided, then a peer moved it
        # terminal before the canonical caller validated the returned DEDUP.
        async with AsyncSessionLocal() as peer_session:
            raced_review = await peer_session.get(Task, review_id)
            assert raced_review is not None
            raced_review.status = review_status
            peer_session.add(raced_review)
            await peer_session.commit()

    consumed = await consume_approval_followup_after_dedup(
        decision,
        review_id,
        db_session,
    )
    assert consumed is should_consume
    await db_session.rollback()

    async with AsyncSessionLocal() as verifying_session:
        source = await verifying_session.get(Task, source_id)
        assert source is not None
        context = dict(source.context or {})
        if should_consume:
            assert source.requires_approval is False
            assert APPROVAL_FOLLOWUP_CLAIM_KEY not in context
            assert APPROVAL_HOLD_EPISODE_KEY not in context
            assert APPROVAL_HOLD_PROVENANCE_KEY not in context
        else:
            assert source.requires_approval is True
            assert context[APPROVAL_FOLLOWUP_CLAIM_KEY] == claim
            assert context[APPROVAL_HOLD_EPISODE_KEY] == claim["token"]
            provenance = context[APPROVAL_HOLD_PROVENANCE_KEY]
            assert provenance["episode"] == claim["token"]
            assert provenance["set_at"] == context["_approval_set_at"]


@pytest.mark.asyncio
async def test_unbound_legacy_review_dedup_cannot_consume_approval(db_session):
    """Even a malformed child carrying literal empty SHAs is never authority."""

    _require_postgres(db_session)
    source_id, review_id, decision = await _seed_pending_review_followup(
        db_session,
        review_status=TaskStatus.QUEUED,
    )
    empty_target = {
        "project_id": str(decision.project_id),
        "pr_number": 42,
        "base_sha": "",
        "head_sha": "",
    }
    empty_claim = {**(decision.claim or {}), "base_sha": "", "head_sha": ""}

    async with AsyncSessionLocal() as peer_session:
        source = await peer_session.get(Task, source_id)
        review = await peer_session.get(Task, review_id)
        assert source is not None
        assert review is not None
        context = dict(source.context or {})
        loop = dict(context.get("review_loop") or {})
        loop.pop("base_sha", None)
        loop.pop("head_sha", None)
        loop.pop("review_target", None)
        context["review_loop"] = loop
        context[APPROVAL_FOLLOWUP_CLAIM_KEY] = empty_claim
        source.context = context
        review.context = {"review_loop": {"review_target": empty_target}}
        peer_session.add(source)
        peer_session.add(review)
        await peer_session.commit()

    legacy_decision = replace(decision, claim=empty_claim)
    assert not await consume_approval_followup_after_dedup(
        legacy_decision,
        review_id,
        db_session,
    )
    await db_session.rollback()

    async with AsyncSessionLocal() as verifying_session:
        source = await verifying_session.get(Task, source_id)
        assert source is not None
        context = dict(source.context or {})
        assert source.requires_approval is True
        assert context[APPROVAL_FOLLOWUP_CLAIM_KEY] == empty_claim
        assert context[APPROVAL_HOLD_EPISODE_KEY] == empty_claim["token"]
        provenance = context[APPROVAL_HOLD_PROVENANCE_KEY]
        assert provenance["episode"] == empty_claim["token"]

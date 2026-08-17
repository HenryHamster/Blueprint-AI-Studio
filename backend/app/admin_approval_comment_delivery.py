"""Leased exactly-once delivery for approval-merge comments."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.admin_approval_authority import (
    APPROVAL_MERGE_LEDGER_KEY,
    ApprovalDecision,
)
from app.models import Task, _utcnow

COMMENT_LEASE_SECONDS = 300


@dataclass(frozen=True)
class ApprovalCommentLease:
    token: str
    intent_id: str
    project_id: str
    owner: str
    repo: str
    pr_number: int


def _lease_is_live(lease: object, *, now: float, intent_id: str) -> bool:
    if not isinstance(lease, dict) or lease.get("intent_id") != intent_id:
        return False
    try:
        return float(lease.get("expires_at_epoch") or 0) > now
    except (TypeError, ValueError):
        return False


async def acquire_comment_lease(
    decision: ApprovalDecision,
    session: AsyncSession,
) -> ApprovalCommentLease | None:
    """CAS pending/expired-submitting to a tokenized lease under a Task lock."""

    from app.admin_approval_merge_protocol import (
        _ledger_history,
        approval_merge_ledger,
    )

    task_id = decision.task_id
    decision_project_id = str(decision.project_id)
    await session.rollback()
    task = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if task is None:
        await session.rollback()
        return None
    ledger = await approval_merge_ledger(task, decision, session)
    if (
        ledger is None
        or ledger.get("state") != "merged"
        or ledger.get("semantic_state") != "finalized"
    ):
        await session.rollback()
        return None
    effects = dict(ledger.get("post_effects") or {})
    state = effects.get("comment")
    if state not in {"pending", "submitting"}:
        await session.commit()
        return None
    intent_id = str(ledger.get("intent_id") or "")
    project_id = str(ledger.get("project_id") or "")
    owner = str(ledger.get("owner") or "").strip().lower()
    repo = str(ledger.get("repo") or "").strip().lower()
    pr_number = int(ledger.get("pr_number") or 0)
    if (
        not intent_id
        or not project_id
        or project_id != decision_project_id
        or not owner
        or not repo
        or pr_number <= 0
    ):
        await session.rollback()
        return None
    leases = dict(ledger.get("post_effect_leases") or {})
    previous = leases.get("comment")
    now = time.time()
    if state == "submitting" and _lease_is_live(
        previous, now=now, intent_id=intent_id
    ):
        await session.commit()
        return None
    token = str(uuid.uuid4())
    previous_attempt = (
        int(previous.get("attempt") or 0) if isinstance(previous, dict) else 0
    )
    lease = {
        "state": "submitting",
        "token": token,
        "intent_id": intent_id,
        "attempt": previous_attempt + 1,
        "acquired_at": _utcnow().isoformat(),
        "expires_at_epoch": now + COMMENT_LEASE_SECONDS,
        **(
            {"recovered_from_token": previous.get("token")}
            if state == "submitting" and isinstance(previous, dict)
            else {}
        ),
    }
    effects["comment"] = "submitting"
    leases["comment"] = lease
    ledger["post_effects"] = effects
    ledger["post_effect_leases"] = leases
    ledger["transition_seq"] = int(ledger.get("transition_seq") or 0) + 1
    context = dict(task.context or {})
    context[APPROVAL_MERGE_LEDGER_KEY] = ledger
    task.context = context
    session.add(task)
    session.add(_ledger_history(task, "APPROVAL_MERGE_POST_EFFECT", ledger))
    await session.commit()
    return ApprovalCommentLease(
        token, intent_id, project_id, owner, repo, pr_number
    )


async def renew_comment_lease_for_post(
    decision: ApprovalDecision,
    session: AsyncSession,
    lease: ApprovalCommentLease,
) -> bool:
    """Fresh-CAS the lease and extend it immediately before the POST."""

    from app.admin_approval_merge_protocol import (
        _ledger_history,
        approval_merge_ledger,
    )

    task_id = decision.task_id
    await session.rollback()
    task = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if task is None:
        await session.rollback()
        return False
    ledger = await approval_merge_ledger(task, decision, session)
    effects = dict((ledger or {}).get("post_effects") or {})
    leases = dict((ledger or {}).get("post_effect_leases") or {})
    current = leases.get("comment")
    if (
        ledger is None
        or ledger.get("state") != "merged"
        or ledger.get("semantic_state") != "finalized"
        or str(ledger.get("project_id") or "") != lease.project_id
        or effects.get("comment") != "submitting"
        or not isinstance(current, dict)
        or current.get("token") != lease.token
        or current.get("intent_id") != lease.intent_id
    ):
        await session.rollback()
        return False
    renewed = dict(current)
    renewed["state"] = "submitting"
    renewed["renewed_at"] = _utcnow().isoformat()
    renewed["expires_at_epoch"] = time.time() + COMMENT_LEASE_SECONDS
    leases["comment"] = renewed
    ledger["post_effect_leases"] = leases
    ledger["transition_seq"] = int(ledger.get("transition_seq") or 0) + 1
    context = dict(task.context or {})
    context[APPROVAL_MERGE_LEDGER_KEY] = ledger
    task.context = context
    session.add(task)
    session.add(_ledger_history(task, "APPROVAL_MERGE_POST_EFFECT", ledger))
    await session.commit()
    return True


async def complete_comment_lease(
    decision: ApprovalDecision,
    session: AsyncSession,
    lease: ApprovalCommentLease,
) -> bool:
    """Finalize only the still-current lease token under a fresh Task lock."""

    from app.admin_approval_merge_protocol import (
        _ledger_history,
        approval_merge_ledger,
    )

    task_id = decision.task_id
    await session.rollback()
    task = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if task is None:
        await session.rollback()
        return False
    ledger = await approval_merge_ledger(task, decision, session)
    if (
        ledger is None
        or ledger.get("state") != "merged"
        or ledger.get("semantic_state") != "finalized"
    ):
        await session.rollback()
        return False
    effects = dict(ledger.get("post_effects") or {})
    if effects.get("comment") == "done":
        await session.commit()
        return True
    leases = dict(ledger.get("post_effect_leases") or {})
    current = leases.get("comment")
    if (
        effects.get("comment") != "submitting"
        or not isinstance(current, dict)
        or current.get("token") != lease.token
        or current.get("intent_id") != lease.intent_id
    ):
        await session.rollback()
        return False
    effects["comment"] = "done"
    completed = dict(current)
    completed["state"] = "done"
    completed["completed_at"] = _utcnow().isoformat()
    leases["comment"] = completed
    ledger["post_effects"] = effects
    ledger["post_effect_leases"] = leases
    ledger["transition_seq"] = int(ledger.get("transition_seq") or 0) + 1
    context = dict(task.context or {})
    context[APPROVAL_MERGE_LEDGER_KEY] = ledger
    task.context = context
    session.add(task)
    session.add(_ledger_history(task, "APPROVAL_MERGE_POST_EFFECT", ledger))
    await session.commit()
    return True


async def release_comment_lease(
    decision: ApprovalDecision,
    session: AsyncSession,
    lease: ApprovalCommentLease,
    *,
    reason: str = "marker_lookup_failed_before_post",
) -> bool:
    """CAS a known pre-POST failure back to immediately retryable pending."""

    from app.admin_approval_merge_protocol import (
        _ledger_history,
        approval_merge_ledger,
    )

    task_id = decision.task_id
    await session.rollback()
    task = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if task is None:
        await session.rollback()
        return False
    ledger = await approval_merge_ledger(task, decision, session)
    effects = dict((ledger or {}).get("post_effects") or {})
    leases = dict((ledger or {}).get("post_effect_leases") or {})
    current = leases.get("comment")
    if (
        ledger is None
        or ledger.get("state") != "merged"
        or ledger.get("semantic_state") != "finalized"
        or effects.get("comment") != "submitting"
        or not isinstance(current, dict)
        or current.get("token") != lease.token
        or current.get("intent_id") != lease.intent_id
    ):
        await session.rollback()
        return False
    effects["comment"] = "pending"
    released = dict(current)
    released["state"] = "pending"
    released["released_at"] = _utcnow().isoformat()
    released["release_reason"] = reason
    leases["comment"] = released
    ledger["post_effects"] = effects
    ledger["post_effect_leases"] = leases
    ledger["transition_seq"] = int(ledger.get("transition_seq") or 0) + 1
    context = dict(task.context or {})
    context[APPROVAL_MERGE_LEDGER_KEY] = ledger
    task.context = context
    session.add(task)
    session.add(_ledger_history(task, "APPROVAL_MERGE_POST_EFFECT", ledger))
    await session.commit()
    return True


async def deliver_approval_merge_comment(
    decision: ApprovalDecision,
    session: AsyncSession,
    credentials: tuple[str, str, str],
) -> bool:
    """Acquire, release the DB lock, reconcile the marker, and submit once."""

    lease = await acquire_comment_lease(decision, session)
    if lease is None:
        return False
    token, credential_owner, credential_repo = credentials
    if (
        credential_owner.strip().lower() != lease.owner
        or credential_repo.strip().lower() != lease.repo
    ):
        await release_comment_lease(
            decision,
            session,
            lease,
            reason="credential_target_mismatch_before_post",
        )
        raise RuntimeError("approval comment credentials do not match merge intent")
    marker = f"<!-- blueprint-approval-merge:{lease.intent_id} -->"
    from app.approval_comment_github import (
        approval_comment_marker_exists_strict,
        create_approval_comment_once,
    )

    try:
        exists = await approval_comment_marker_exists_strict(
            token,
            lease.owner,
            lease.repo,
            lease.pr_number,
            marker,
            project_id=lease.project_id,
        )
    except Exception:
        await release_comment_lease(decision, session, lease)
        raise
    if not exists:
        if not await renew_comment_lease_for_post(decision, session, lease):
            return False
        label = (
            "Human-approved rollup merge."
            if decision.followup_kind == "rollup_merge"
            else "Human-approved merge (security-critical files)."
        )
        await create_approval_comment_once(
            token,
            lease.owner,
            lease.repo,
            lease.pr_number,
            f"**Blueprint Review Pipeline**: {label}\n\n{marker}",
            project_id=lease.project_id,
        )
    return await complete_comment_lease(decision, session, lease)

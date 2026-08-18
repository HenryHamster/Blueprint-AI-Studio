"""One-process authority for a durably-started approval merge submission."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.admin_approval_merge_protocol import (
    APPROVAL_MERGE_LEDGER_KEY,
    _authority_token_matches,
    _lease_deadline,
    _ledger_history,
    _project_merge_config,
    _promotion_merge_policy_snapshot,
    approval_merge_ledger,
)
from app.models import Project, Task, _utcnow
from app.task_locking import lock_project_then_task

if TYPE_CHECKING:
    from app.admin_approval_authority import ApprovalDecision


@dataclass(frozen=True)
class ApprovalMergeSubmissionAuthority:
    """One-process authority returned by the durable submission-start CAS."""

    project_authority: Callable[[AsyncSession], Awaitable[bool]]
    source_authority: Callable[[AsyncSession], Awaitable[Task | None]]
    acquire_send_lease: Callable[[AsyncSession], Awaitable[str | None]]
    validate_send_lease: Callable[[AsyncSession, str], Awaitable[bool]]
    release_for_reconciliation: Callable[[AsyncSession, str], Awaitable[bool]]
    abort_before_send: Callable[..., Awaitable[bool]]
    close_without_merge: Callable[..., Awaitable[bool]]


def started_merge_authority(
    decision: ApprovalDecision,
    *,
    expected_project_config: Dict[str, Any],
    intent_id: str,
    submission_nonce: str,
) -> ApprovalMergeSubmissionAuthority:
    """Build the exact final authority for this process's one permitted PUT."""

    async def _acquire_project(authority_session: AsyncSession) -> bool:
        project = (
            (
                await authority_session.execute(
                    select(Project)
                    .where(Project.id == decision.project_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        return bool(
            project is not None
            and _project_merge_config(project) == expected_project_config
        )

    async def _acquire(authority_session: AsyncSession) -> Task | None:
        from app.admin_approval_authority import _claim_matches_authoritative_task

        task = (
            (
                await authority_session.execute(
                    select(Task)
                    .where(Task.id == decision.task_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        if task is None or not await _claim_matches_authoritative_task(
            task,
            decision,
            authority_session,
            allowed_states=frozenset({"merge_intent"}),
        ):
            return None
        ledger = await approval_merge_ledger(task, decision, authority_session)
        if (
            ledger is None
            or ledger.get("intent_id") != intent_id
            or ledger.get("state") != "ambiguous"
            or ledger.get("submission_nonce") != submission_nonce
            # Acquiring a send lease is the point after which a process may
            # have crossed the network boundary.  Recovery can release that
            # lease for GET-only reconciliation, but must never make the PUT
            # authority reusable.
            or ledger.get("send_lease_started_at")
            or ledger.get("submission_uncertain_at")
            or isinstance(ledger.get("negative_merge_proof"), dict)
        ):
            return None
        return task

    async def _close_without_merge(
        authority_session: AsyncSession,
        terminal_state: str,
        reason: str,
        authority_token: str | None = None,
        *,
        require_never_sent: bool = False,
    ) -> bool:
        """Close this nonce after proof that no merge was accepted."""

        from app.admin_approval_authority import (
            _claim_matches_authoritative_task,
            establish_independent_approval_hold,
        )

        project_id = decision.project_id
        task_id = decision.task_id
        await authority_session.rollback()
        await authority_session.execute(
            select(Project)
            .where(Project.id == project_id)
            .execution_options(populate_existing=True)
        )
        async with lock_project_then_task(
            authority_session,
            project_id,
            task_id,
            skip_locked=False,
        ) as (project, task):
            if project is None or task is None:
                await authority_session.rollback()
                return False
            ledger = await approval_merge_ledger(task, decision, authority_session)
            if (
                ledger is None
                or ledger.get("intent_id") != intent_id
                or ledger.get("submission_nonce") != submission_nonce
                or not _authority_token_matches(
                    ledger, str(authority_token or submission_nonce)
                )
                or (
                    require_never_sent
                    and (
                        ledger.get("send_lease_started_at")
                        or ledger.get("submission_uncertain_at")
                        or isinstance(ledger.get("negative_merge_proof"), dict)
                    )
                )
            ):
                await authority_session.rollback()
                return False
            claim_is_current = await _claim_matches_authoritative_task(
                task,
                decision,
                authority_session,
                allowed_states=frozenset({"merge_intent"}),
            )
            ledger["state"] = terminal_state
            ledger["semantic_state"] = "not_applicable"
            ledger["transition_seq"] = int(ledger.get("transition_seq") or 0) + 1
            ledger["submission_terminal_at"] = _utcnow().isoformat()
            ledger["submission_terminal_reason"] = str(reason)
            ledger["terminal_authority_token"] = str(
                authority_token or submission_nonce
            )
            ledger.pop("send_lease_token", None)
            ledger.pop("send_lease_expires_at", None)
            ledger.pop("recovery_expires_at", None)
            if claim_is_current:
                establish_independent_approval_hold(
                    task,
                    reason=str(reason),
                    owner="approval_merge_submission_terminal",
                )
                task.latest_status_message = (
                    f"Approved merge was not submitted: {reason}; "
                    "fresh operator approval is required"
                )[:500]
            context = dict(task.context or {})
            context[APPROVAL_MERGE_LEDGER_KEY] = ledger
            task.context = context
            authority_session.add(task)
            authority_session.add(
                _ledger_history(task, "APPROVAL_MERGE_SUBMISSION_TERMINAL", ledger)
            )
            await authority_session.commit()
            return True

    async def _abort_before_send(
        authority_session: AsyncSession,
        reason: str = "final authority changed before the merge request",
    ) -> bool:
        return await _close_without_merge(
            authority_session,
            "unsent_aborted",
            reason,
            require_never_sent=True,
        )

    async def _acquire_send_lease(authority_session: AsyncSession) -> str | None:
        """CAS the pre-send nonce to one durable lease and release all locks."""

        if not await _acquire_project(authority_session):
            await authority_session.rollback()
            return None
        task = await _acquire(authority_session)
        if task is None:
            await authority_session.rollback()
            return None
        ledger = await approval_merge_ledger(task, decision, authority_session)
        if (
            ledger is None
            or ledger.get("intent_id") != intent_id
            or ledger.get("state") != "ambiguous"
            or ledger.get("submission_nonce") != submission_nonce
            or ledger.get("send_lease_token")
        ):
            await authority_session.rollback()
            return None
        send_token = str(uuid.uuid4())
        ledger["send_lease_token"] = send_token
        ledger["send_lease_expires_at"] = _lease_deadline()
        ledger["send_lease_started_at"] = _utcnow().isoformat()
        ledger["transition_seq"] = int(ledger.get("transition_seq") or 0) + 1
        context = dict(task.context or {})
        context[APPROVAL_MERGE_LEDGER_KEY] = ledger
        task.context = context
        authority_session.add(task)
        authority_session.add(
            _ledger_history(task, "APPROVAL_MERGE_SEND_LEASED", ledger)
        )
        await authority_session.commit()
        return send_token

    async def _release_for_reconciliation(
        authority_session: AsyncSession,
        send_token: str,
    ) -> bool:
        """Make a completed uncertain transport immediately GET-recoverable."""

        project_id = decision.project_id
        task_id = decision.task_id
        await authority_session.rollback()
        await authority_session.execute(
            select(Project)
            .where(Project.id == project_id)
            .execution_options(populate_existing=True)
        )
        async with lock_project_then_task(
            authority_session,
            project_id,
            task_id,
            skip_locked=False,
        ) as (project, task):
            if project is None or task is None:
                await authority_session.rollback()
                return False
            ledger = await approval_merge_ledger(task, decision, authority_session)
            if (
                ledger is None
                or ledger.get("intent_id") != intent_id
                or ledger.get("state") != "ambiguous"
                or str(ledger.get("send_lease_token") or "") != str(send_token)
            ):
                await authority_session.rollback()
                return False
            ledger.pop("send_lease_token", None)
            ledger.pop("send_lease_expires_at", None)
            ledger["submission_uncertain_at"] = _utcnow().isoformat()
            ledger["transition_seq"] = int(ledger.get("transition_seq") or 0) + 1
            context = dict(task.context or {})
            context[APPROVAL_MERGE_LEDGER_KEY] = ledger
            task.context = context
            authority_session.add(task)
            authority_session.add(
                _ledger_history(
                    task,
                    "APPROVAL_MERGE_SUBMISSION_UNCERTAIN",
                    ledger,
                )
            )
            await authority_session.commit()
            return True

    async def _validate_send_lease(
        authority_session: AsyncSession,
        send_token: str,
    ) -> bool:
        """Revalidate and renew the exact lease after the lock-free final GET."""

        from app.admin_approval_authority import _claim_matches_authoritative_task

        project_id = decision.project_id
        task_id = decision.task_id
        await authority_session.rollback()
        await authority_session.execute(
            select(Project)
            .where(Project.id == project_id)
            .execution_options(populate_existing=True)
        )
        async with lock_project_then_task(
            authority_session,
            project_id,
            task_id,
            skip_locked=False,
        ) as (project, task):
            if project is None or task is None:
                await authority_session.rollback()
                return False
            ledger = await approval_merge_ledger(task, decision, authority_session)
            project_config = _project_merge_config(project)
            bound_policy = (
                ledger.get("prechecked_policy")
                if isinstance(ledger, dict)
                else None
            )
            promotion_policy_exact = True
            if isinstance(bound_policy, dict):
                bound_promotion_policy = bound_policy.get("promotion_policy")
                promotion_policy_exact = bool(
                    isinstance(bound_promotion_policy, dict)
                    and bound_promotion_policy.get("allowed") is True
                    and bound_promotion_policy
                    == _promotion_merge_policy_snapshot(
                        task,
                        project_config,
                        base_branch=str(ledger.get("base_branch") or ""),
                        followup_kind=decision.followup_kind,
                    )
                )
            claim_is_current = await _claim_matches_authoritative_task(
                task,
                decision,
                authority_session,
                allowed_states=frozenset({"merge_intent"}),
            )
            if (
                ledger is None
                or ledger.get("intent_id") != intent_id
                or ledger.get("submission_nonce") != submission_nonce
                or ledger.get("state") != "ambiguous"
                or not _authority_token_matches(ledger, send_token)
                or not claim_is_current
                or project_config != expected_project_config
                or not promotion_policy_exact
            ):
                await authority_session.rollback()
                return False
            # Renew from this exact CAS so recovery cannot steal authority during
            # the bounded one-attempt PUT/receipt window after a slow final GET.
            ledger["send_lease_expires_at"] = _lease_deadline()
            ledger["send_lease_validated_at"] = _utcnow().isoformat()
            ledger["transition_seq"] = int(ledger.get("transition_seq") or 0) + 1
            context = dict(task.context or {})
            context[APPROVAL_MERGE_LEDGER_KEY] = ledger
            task.context = context
            authority_session.add(task)
            authority_session.add(
                _ledger_history(
                    task,
                    "APPROVAL_MERGE_SEND_LEASE_RENEWED",
                    ledger,
                )
            )
            await authority_session.commit()
            return True

    return ApprovalMergeSubmissionAuthority(
        project_authority=_acquire_project,
        source_authority=_acquire,
        acquire_send_lease=_acquire_send_lease,
        validate_send_lease=_validate_send_lease,
        release_for_reconciliation=_release_for_reconciliation,
        abort_before_send=_abort_before_send,
        close_without_merge=_close_without_merge,
    )

"""Durable non-schema protocol for human-approved merge submission.

The revocable approval claim authorizes only the next local transition.  This
module owns the independent append-only merge ledger that survives claim or
Task.context replacement and reconciles transport ambiguity without replaying
a possibly-accepted GitHub PUT.
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Project, Task, TaskHistory, _utcnow
from app.task_locking import lock_project_then_task

if TYPE_CHECKING:
    from app.admin_approval_authority import ApprovalDecision
    from app.admin_approval_merge_submission import ApprovalMergeSubmissionAuthority

APPROVAL_FOLLOWUP_CLAIM_KEY = "_operator_approval_followup_claim"
APPROVAL_MERGE_LEDGER_KEY = "_operator_approval_merge_ledger"
APPROVAL_MERGE_FACT_KEY = "_operator_approval_merge_fact"
APPROVAL_MERGE_LEASE_SECONDS = 120
APPROVAL_MERGE_NEGATIVE_CONFIRMATION_SECONDS = 30
APPROVAL_MERGE_HISTORY_ACTIONS = frozenset(
    {
        "APPROVAL_MERGE_INTENT_CREATED",
        "APPROVAL_MERGE_UNSENT_ABORTED",
        "APPROVAL_MERGE_SUBMISSION_TERMINAL",
        "APPROVAL_MERGE_SUBMISSION_STARTED",
        "APPROVAL_MERGE_SEND_LEASED",
        "APPROVAL_MERGE_SEND_LEASE_RENEWED",
        "APPROVAL_MERGE_SUBMISSION_UNCERTAIN",
        "APPROVAL_MERGE_RECONCILIATION_STARTED",
        "APPROVAL_MERGE_NEGATIVE_OBSERVED",
        "APPROVAL_MERGE_OBSERVED_MERGED",
        "APPROVAL_MERGE_SUBMITTED",
        "APPROVAL_MERGE_FINALIZED",
        "APPROVAL_MERGE_POST_EFFECT",
    }
)


def _project_merge_config(project: Project) -> Dict[str, Any]:
    from app.shared_state import (
        get_project_integration_branch,
        get_project_promotion_target,
    )

    return {
        "github_owner": str(project.github_owner or "").strip().lower(),
        "github_repo": str(project.github_repo or "").strip().lower(),
        "configured_integration_branch": project.integration_branch,
        "configured_promotion_target": getattr(project, "promotion_target", None),
        "integration_branch": get_project_integration_branch(project),
        "promotion_target": get_project_promotion_target(project),
        "is_self_repo": project.is_self_repo is True,
    }


def _promotion_merge_policy_snapshot(
    task: Task,
    project_config: Dict[str, Any],
    *,
    base_branch: str,
    followup_kind: str | None,
) -> Dict[str, Any]:
    """Pure promotion admission result bound to a fresh Project/Task pair."""

    from app.admin_task_mutations import _promotion_target_merge_allowed

    target = str(base_branch or "")
    integration = str(project_config.get("integration_branch") or "")
    promotion = str(project_config.get("promotion_target") or "")
    promotion_landing = bool(
        target and target == promotion and target != integration
    )
    hotfix_lane = bool(
        followup_kind != "rollup_merge"
        and promotion_landing
        and _promotion_target_merge_allowed(task, target)
    )
    # A rollup approval is itself the explicit human gate for a promotion
    # landing. Critical task PRs retain the narrower hotfix-only exception.
    allowed = bool(
        followup_kind == "rollup_merge"
        or not promotion_landing
        or hotfix_lane
    )
    return {
        "followup_kind": str(followup_kind or ""),
        "base_branch": target,
        "integration_branch": integration,
        "promotion_target": promotion,
        "promotion_landing": promotion_landing,
        "hotfix_lane": hotfix_lane,
        "allowed": allowed,
    }


def _ledger_matches_decision(
    ledger: Dict[str, Any], decision: ApprovalDecision
) -> bool:
    claim = decision.claim or {}
    return bool(
        ledger.get("approval_decision_token") == claim.get("token")
        and str(ledger.get("task_id") or "") == str(decision.task_id)
        and str(ledger.get("project_id") or "") == str(decision.project_id)
        and int(ledger.get("pr_number") or 0) == int(decision.pr_number or 0)
        and ledger.get("followup_kind") == decision.followup_kind
        and str(ledger.get("source_status") or "") == str(claim.get("status") or "")
    )


def _ledger_transition_key(ledger: Dict[str, Any]) -> tuple[int, int, int]:
    """Order full snapshots without trusting wall clocks or random UUIDs."""

    try:
        sequence = int(ledger.get("transition_seq") or 0)
    except (TypeError, ValueError):
        sequence = 0
    state_rank = {
        "not_sent": 1,
        "ambiguous": 2,
        "reconciling": 2,
        "unsent_aborted": 3,
        "known_not_sent": 3,
        "submission_rejected": 3,
        "merged": 3,
    }.get(str(ledger.get("state") or ""), 0)
    if ledger.get("semantic_state") in {"finalized", "fact_only_finalized"}:
        state_rank = 4
    completed_effects = sum(
        value in {"done", "skipped"}
        for value in (ledger.get("post_effects") or {}).values()
    )
    return sequence, state_rank, completed_effects


def _lease_deadline() -> str:
    return (_utcnow() + timedelta(seconds=APPROVAL_MERGE_LEASE_SECONDS)).isoformat()


def _lease_is_active(ledger: Dict[str, Any], *, prefix: str) -> bool:
    """Return whether a complete durable lease is still authoritative."""

    token = str(ledger.get(f"{prefix}_token") or "")
    expires_raw = str(ledger.get(f"{prefix}_expires_at") or "")
    if not token or not expires_raw:
        return False
    try:
        expires_at = datetime.fromisoformat(expires_raw)
    except (TypeError, ValueError):
        return False
    return expires_at > _utcnow()


def _authority_token_matches(ledger: Dict[str, Any], authority_token: str) -> bool:
    """Bind a receipt/terminal transition to the exact current lease owner."""

    token = str(authority_token or "")
    if not token:
        return False
    if ledger.get("state") == "reconciling":
        return bool(
            token == str(ledger.get("recovery_token") or "")
            and _lease_is_active(ledger, prefix="recovery")
        )
    if ledger.get("state") != "ambiguous":
        return False
    send_token = str(ledger.get("send_lease_token") or "")
    if send_token:
        return bool(
            token == send_token and _lease_is_active(ledger, prefix="send_lease")
        )
    return token == str(ledger.get("submission_nonce") or "")


def _latest_ledgers_by_intent(
    history_ledgers: list[Dict[str, Any]],
    *,
    cached: Dict[str, Any] | None = None,
) -> dict[str, Dict[str, Any]]:
    """Select history snapshots by per-intent sequence; cache is fallback only."""

    latest: dict[str, Dict[str, Any]] = {}
    for ledger in history_ledgers:
        intent_id = str(ledger.get("intent_id") or "")
        if not intent_id:
            continue
        current = latest.get(intent_id)
        if current is None or _ledger_transition_key(ledger) > _ledger_transition_key(
            current
        ):
            latest[intent_id] = copy.deepcopy(ledger)
    if isinstance(cached, dict):
        intent_id = str(cached.get("intent_id") or "")
        if intent_id and intent_id not in latest:
            latest[intent_id] = copy.deepcopy(cached)
    return latest


async def approval_merge_ledger(
    task: Task,
    decision: ApprovalDecision,
    session: AsyncSession,
) -> Dict[str, Any] | None:
    """Load the objective ledger, using append-only history as authority.

    Replacement paths historically replace ``Task.context`` wholesale.  The
    context value is therefore only a cache; every transition stores a complete
    ledger snapshot in TaskHistory so an irreversible merge fact survives it.
    """

    histories = (
        (
            await session.execute(
                select(TaskHistory)
                .where(
                    TaskHistory.task_id == task.id,
                    TaskHistory.action.in_(APPROVAL_MERGE_HISTORY_ACTIONS),
                )
                .order_by(TaskHistory.timestamp.desc(), TaskHistory.id.desc())
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    history_ledgers = [
        ledger
        for history in histories
        for ledger in [(history.details or {}).get("merge_ledger")]
        if isinstance(ledger, dict) and _ledger_matches_decision(ledger, decision)
    ]
    latest = _latest_ledgers_by_intent(history_ledgers)
    if latest:
        return max(latest.values(), key=_ledger_transition_key)
    cached = (task.context or {}).get(APPROVAL_MERGE_LEDGER_KEY)
    if isinstance(cached, dict) and _ledger_matches_decision(cached, decision):
        return copy.deepcopy(cached)
    return None


def _ledger_history(task: Task, action: str, ledger: Dict[str, Any]) -> TaskHistory:
    return TaskHistory(
        task_id=task.id,
        action=action,
        details={
            "approval_decision_token": ledger.get("approval_decision_token"),
            "intent_id": ledger.get("intent_id"),
            "followup_kind": ledger.get("followup_kind"),
            "merge_ledger": copy.deepcopy(ledger),
            **(
                {"merge_receipt": copy.deepcopy(ledger.get("merge_fact"))}
                if isinstance(ledger.get("merge_fact"), dict)
                else {}
            ),
        },
    )


def _target_matches_intent(
    target: Dict[str, Any], ledger: Dict[str, Any], *, require_checked_base: bool
) -> bool:
    if (
        str(target.get("project_id") or "") != str(ledger.get("project_id") or "")
        or int(target.get("pr_number") or 0) != int(ledger.get("pr_number") or 0)
        or str(target.get("head_sha") or "") != str(ledger.get("head_sha") or "")
        or str(target.get("head_ref") or "") != str(ledger.get("head_ref") or "")
        or str(target.get("base_ref") or "") != str(ledger.get("base_ref") or "")
    ):
        return False
    return not require_checked_base or str(target.get("base_sha") or "") == str(
        ledger.get("base_sha") or ""
    )


async def _terminalize_pending_precheck_refusal(
    task: Task,
    decision: ApprovalDecision,
    session: AsyncSession,
    *,
    reason: str,
) -> bool:
    """Consume only this pending claim and establish a separately-owned hold."""

    from app.admin_approval_authority import (
        _claim_matches_authoritative_task,
        establish_independent_approval_hold,
    )

    if not await _claim_matches_authoritative_task(
        task,
        decision,
        session,
        allowed_states=frozenset({"pending"}),
    ):
        return False
    establish_independent_approval_hold(
        task,
        reason=reason,
        owner="approval_merge_precheck_refused",
    )
    task.latest_status_message = (
        f"Approved merge refused before submission: {reason}; "
        "fresh operator approval is required"
    )[:500]
    session.add(task)
    session.add(
        TaskHistory(
            task_id=task.id,
            action="MODIFIED",
            details={
                "changes": {
                    "approval_followup_pending": False,
                    "approval_refused": "merge_precheck_changed",
                },
                "modified_by": decision.modified_by,
                "approval_decision_token": (decision.claim or {}).get("token"),
            },
        )
    )
    return True


def approval_merge_intent_writer(
    decision: ApprovalDecision,
    *,
    owner: str,
    repo: str,
    branch: str,
    base_branch: str,
    expected_project_config: Dict[str, Any] | None = None,
    prechecked_policy: Dict[str, Any] | None = None,
) -> Callable[[AsyncSession, Dict[str, Any]], Awaitable[Dict[str, Any] | None]]:
    """Persist an exact, fresh-config merge intent before any possible PUT."""

    bound_project_config = copy.deepcopy(expected_project_config)
    bound_policy = copy.deepcopy(prechecked_policy)

    async def _record_intent(
        authority_session: AsyncSession, live_target: Dict[str, Any]
    ) -> Dict[str, Any] | None:
        from app.admin_approval_authority import _claim_matches_authoritative_task

        project_id = decision.project_id
        task_id = decision.task_id
        await authority_session.rollback()
        # ``rollback`` expires the caller's identity map.  Force a real fresh
        # Project read before entering the canonical Project -> Task lock helper;
        # the helper then upgrades this same identity to the authoritative lock.
        # Keeping this refetch explicit also prevents an asyncpg lazy load if the
        # caller supplied an already-loaded Project instance.
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
                return None
            existing = await approval_merge_ledger(task, decision, authority_session)
            if existing is not None:
                # Once submission may have started, config/hold revocation cannot
                # erase the objective ledger.  A retry may only observe the same
                # repo/PR/head/base-ref; it never sends the old intent again.
                if existing.get("state") in {"ambiguous", "merged"}:
                    await authority_session.commit()
                    return existing
                claim_is_current = bool(
                    existing.get("state") == "not_sent"
                    and await _claim_matches_authoritative_task(
                        task,
                        decision,
                        authority_session,
                        allowed_states=frozenset({"merge_intent"}),
                    )
                )
                if (
                    claim_is_current
                    and _project_merge_config(project) == existing.get("project_config")
                    and _target_matches_intent(
                        live_target, existing, require_checked_base=True
                    )
                ):
                    await authority_session.commit()
                    return existing
                if existing.get("state") == "not_sent":
                    # No submission-start marker exists, so this process can
                    # prove the old intent never reached PUT.  Close it durably
                    # instead of leaving an eternal in-flight fence.  Only the
                    # still-exact claim is converted to an independent hold;
                    # a newer hold/replacement remains completely untouched.
                    existing["state"] = "unsent_aborted"
                    existing["semantic_state"] = "not_applicable"
                    existing["transition_seq"] = (
                        int(existing.get("transition_seq") or 0) + 1
                    )
                    existing["unsent_aborted_at"] = _utcnow().isoformat()
                    existing["unsent_abort_reason"] = (
                        "approval claim, project config, or live target changed "
                        "before submission started"
                    )
                    if claim_is_current:
                        from app.admin_approval_authority import (
                            establish_independent_approval_hold,
                        )

                        establish_independent_approval_hold(
                            task,
                            reason="approved merge target changed before submission",
                            owner="approval_merge_unsent_abort",
                        )
                        task.latest_status_message = (
                            "Approved merge target changed before submission; "
                            "fresh operator approval is required"
                        )[:500]
                    context = dict(task.context or {})
                    context[APPROVAL_MERGE_LEDGER_KEY] = existing
                    task.context = context
                    authority_session.add(task)
                    authority_session.add(
                        _ledger_history(task, "APPROVAL_MERGE_UNSENT_ABORTED", existing)
                    )
                    await authority_session.commit()
                    return None
                await authority_session.rollback()
                return None

            if not await _claim_matches_authoritative_task(
                task,
                decision,
                authority_session,
                allowed_states=frozenset({"pending"}),
            ):
                await authority_session.rollback()
                return None
            project_config = _project_merge_config(project)
            target_head = str(live_target.get("head_sha") or "")
            target_base = str(live_target.get("base_sha") or "")
            target_head_ref = str(live_target.get("head_ref") or "")
            target_base_ref = str(live_target.get("base_ref") or "")
            claim = dict((task.context or {}).get(APPROVAL_FOLLOWUP_CLAIM_KEY) or {})
            expected_base_branch = str(
                task.first_payload.get("base_branch")
                or _project_merge_config(project).get("integration_branch")
                or ""
            )
            if (
                not target_head
                or not target_base
                or not target_head_ref
                or not target_base_ref
                or int(live_target.get("pr_number") or 0)
                != int(decision.pr_number or 0)
                or str(live_target.get("project_id") or "") != str(decision.project_id)
                or target_head != str(claim.get("head_sha") or "")
                or target_base != str(claim.get("base_sha") or "")
                or target_base_ref != str(base_branch)
                or str(base_branch) != expected_base_branch
                or (str(branch) and target_head_ref != str(branch))
            ):
                await _terminalize_pending_precheck_refusal(
                    task,
                    decision,
                    authority_session,
                    reason="the exact approved pull-request target changed",
                )
                await authority_session.commit()
                return None
            configured_owner = str(project_config.get("github_owner") or "")
            configured_repo = str(project_config.get("github_repo") or "")
            if (configured_owner and configured_owner != owner.strip().lower()) or (
                configured_repo and configured_repo != repo.strip().lower()
            ):
                await _terminalize_pending_precheck_refusal(
                    task,
                    decision,
                    authority_session,
                    reason="the approved repository configuration changed",
                )
                await authority_session.commit()
                return None
            policy_is_exact = bool(
                bound_policy is None
                or (
                    bound_policy.get("migration_guard") == "ok"
                    and int(bound_policy.get("pr_number") or 0)
                    == int(decision.pr_number or 0)
                    and str(bound_policy.get("base_branch") or "")
                    == str(base_branch)
                    and str(bound_policy.get("promotion_target") or "")
                    == str(project_config.get("promotion_target") or "")
                    and bound_policy.get("promotion_policy")
                    == _promotion_merge_policy_snapshot(
                        task,
                        project_config,
                        base_branch=base_branch,
                        followup_kind=decision.followup_kind,
                    )
                    and bool(
                        (bound_policy.get("promotion_policy") or {}).get(
                            "allowed"
                        )
                    )
                )
            )
            if (
                (bound_project_config is not None and project_config != bound_project_config)
                or not policy_is_exact
            ):
                await _terminalize_pending_precheck_refusal(
                    task,
                    decision,
                    authority_session,
                    reason=(
                        "project promotion or migration policy changed after its "
                        "pre-merge guard"
                    ),
                )
                await authority_session.commit()
                return None
            intent_id = str(uuid.uuid4())
            ledger = {
                "intent_id": intent_id,
                "state": "not_sent",
                "semantic_state": "pending",
                "transition_seq": 1,
                "approval_decision_token": claim.get("token"),
                "approval_episode": copy.deepcopy(claim.get("approval_episode")),
                "approval_claim": copy.deepcopy(claim),
                "task_id": str(task.id),
                "project_id": str(project.id),
                "source_status": str(claim.get("status") or ""),
                "followup_kind": decision.followup_kind,
                "owner": owner.strip().lower(),
                "repo": repo.strip().lower(),
                "pr_number": int(decision.pr_number or 0),
                "branch": str(branch),
                "base_branch": str(base_branch),
                "head_ref": target_head_ref,
                "head_sha": target_head,
                "base_ref": target_base_ref,
                "base_sha": target_base,
                "merge_method": "squash",
                "project_config": project_config,
                "prechecked_policy": copy.deepcopy(bound_policy),
                "created_at": _utcnow().isoformat(),
            }
            claim["state"] = "merge_intent"
            claim["merge_intent_id"] = intent_id
            history = _ledger_history(task, "APPROVAL_MERGE_INTENT_CREATED", ledger)
            context = dict(task.context or {})
            context[APPROVAL_FOLLOWUP_CLAIM_KEY] = claim
            context[APPROVAL_MERGE_LEDGER_KEY] = ledger
            task.context = context
            authority_session.add(task)
            authority_session.add(history)
            await authority_session.commit()
            return copy.deepcopy(ledger)

    return _record_intent


def approval_merge_submission_starter(
    decision: ApprovalDecision,
) -> Callable[
    [AsyncSession, Any],
    Awaitable[ApprovalMergeSubmissionAuthority | None],
]:
    """Linearize possible submission before the socket write.

    Only the process that performs the ``not_sent -> ambiguous`` CAS receives
    the in-memory authority callback used for one final live-target/fix recheck.
    A restarted caller sees ``ambiguous`` and is reconciliation-only.
    """

    from app.admin_approval_merge_submission import started_merge_authority

    async def _start(
        authority_session: AsyncSession, merge_decision: Any
    ) -> ApprovalMergeSubmissionAuthority | None:
        from app.admin_approval_authority import (
            _claim_matches_authoritative_task,
            establish_independent_approval_hold,
        )
        from app.review_merge_authority import MergePreSubmissionRefused

        # Persist any proof staged by the first admission pass, then reacquire
        # Project -> Task in canonical order for the durable transition.
        await authority_session.commit()
        async with lock_project_then_task(
            authority_session,
            decision.project_id,
            decision.task_id,
            skip_locked=False,
        ) as (project, task):
            if project is None or task is None:
                await authority_session.rollback()
                return None
            ledger = await approval_merge_ledger(task, decision, authority_session)
            claim_is_current = await _claim_matches_authoritative_task(
                task,
                decision,
                authority_session,
                allowed_states=frozenset({"merge_intent"}),
            )
            if (
                ledger is None
                or ledger.get("state") != "not_sent"
            ):
                await authority_session.rollback()
                return None
            denial_reason = ""
            if not claim_is_current:
                denial_reason = "the approved merge claim changed"
            elif _project_merge_config(project) != ledger.get("project_config"):
                denial_reason = "project merge policy changed after its guard"
            elif str(getattr(merge_decision, "checked_head_sha", "") or "") != str(
                ledger.get("head_sha") or ""
            ) or str(getattr(merge_decision, "checked_base_sha", "") or "") != str(
                ledger.get("base_sha") or ""
            ):
                denial_reason = "the exact checked pull-request target changed"
            elif getattr(merge_decision, "allowed", False) is not True:
                denial_reason = "the final target, review, or active-fix gate refused"
            if denial_reason:
                ledger["state"] = "unsent_aborted"
                ledger["semantic_state"] = "not_applicable"
                ledger["transition_seq"] = int(ledger.get("transition_seq") or 0) + 1
                ledger["unsent_aborted_at"] = _utcnow().isoformat()
                ledger["unsent_abort_reason"] = denial_reason
                if claim_is_current:
                    establish_independent_approval_hold(
                        task,
                        reason=denial_reason,
                        owner="approval_merge_submission_terminal",
                    )
                    task.latest_status_message = (
                        f"Approved merge refused before submission: {denial_reason}; "
                        "fresh operator approval is required"
                    )[:500]
                context = dict(task.context or {})
                context[APPROVAL_MERGE_LEDGER_KEY] = ledger
                task.context = context
                authority_session.add(task)
                authority_session.add(
                    _ledger_history(
                        task,
                        "APPROVAL_MERGE_UNSENT_ABORTED",
                        ledger,
                    )
                )
                await authority_session.commit()
                raise MergePreSubmissionRefused(denial_reason)
            nonce = str(uuid.uuid4())
            ledger["state"] = "ambiguous"
            ledger["transition_seq"] = int(ledger.get("transition_seq") or 0) + 1
            ledger["submission_nonce"] = nonce
            ledger["submission_started_at"] = _utcnow().isoformat()
            history = _ledger_history(task, "APPROVAL_MERGE_SUBMISSION_STARTED", ledger)
            context = dict(task.context or {})
            context[APPROVAL_MERGE_LEDGER_KEY] = ledger
            task.context = context
            authority_session.add(task)
            authority_session.add(history)
            await authority_session.commit()
            return started_merge_authority(
                decision,
                expected_project_config=copy.deepcopy(
                    ledger.get("project_config") or {}
                ),
                intent_id=str(ledger.get("intent_id") or ""),
                submission_nonce=nonce,
            )

    return _start


def approval_merge_receipt_writer(
    decision: ApprovalDecision,
    *,
    branch: str,
    base_branch: str,
) -> Callable[[AsyncSession, Any, dict, str], Awaitable[None]]:
    """Persist objective merge proof independently of a revocable hold claim."""

    async def _record(
        authority_session: AsyncSession,
        merge_decision: Any,
        result: dict,
        authority_token: str,
    ) -> None:
        from app.admin_approval_authority import (
            _claim_matches_authoritative_task,
            _decision_from_pending_claim,
        )

        merge_sha = str(result.get("sha") or result.get("merge_commit_sha") or "")
        if result.get("merged") is not True or not merge_sha:
            raise RuntimeError("approval merge receipt lacks objective merged proof")
        project_id = decision.project_id
        task_id = decision.task_id
        await authority_session.rollback()
        # The receipt may be entered after a failed/uncertain network call.  Do
        # not let an expired Project from that transaction leak into the exact
        # Project -> Task re-lock below.
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
                raise RuntimeError("approval merge source disappeared before receipt")
            ledger = await approval_merge_ledger(task, decision, authority_session)
            if (
                ledger is None
                or ledger.get("state") not in {"ambiguous", "reconciling", "merged"}
                or str(getattr(merge_decision, "checked_head_sha", "") or "")
                != str(ledger.get("head_sha") or "")
                or str(getattr(merge_decision, "checked_base_sha", "") or "")
                != str(ledger.get("base_sha") or "")
                or str(branch) != str(ledger.get("branch") or "")
                or str(base_branch) != str(ledger.get("base_branch") or "")
            ):
                raise RuntimeError("approval merge ledger changed before receipt")
            if ledger.get("state") == "merged":
                if str(ledger.get("receipt_authority_token") or "") != str(
                    authority_token or ""
                ):
                    raise RuntimeError("approval merge receipt authority changed")
                await authority_session.commit()
                return
            if not _authority_token_matches(ledger, authority_token):
                raise RuntimeError("approval merge receipt authority changed")
            proof_kind = (
                "observed_merged"
                if result.get("observed_merged") is True
                else "put_acknowledged"
            )
            receipt = {
                "intent_id": ledger.get("intent_id"),
                "pr_number": decision.pr_number,
                "branch": branch,
                "base_branch": base_branch,
                "head_ref": ledger.get("head_ref"),
                "head_sha": ledger.get("head_sha"),
                "base_ref": ledger.get("base_ref"),
                "base_sha": ledger.get("base_sha"),
                "merge_sha": merge_sha,
                "merged": True,
                "proof_kind": proof_kind,
                "checked_base_parent_verified": result.get(
                    "checked_base_parent_verified"
                )
                is True,
                "message": str(result.get("message") or "")[:500],
                "recorded_at": _utcnow().isoformat(),
            }
            ledger["state"] = "merged"
            ledger["merge_fact"] = receipt
            ledger["receipt_authority_token"] = str(authority_token)
            ledger["semantic_state"] = "pending"
            ledger["transition_seq"] = int(ledger.get("transition_seq") or 0) + 1
            ledger.pop("send_lease_token", None)
            ledger.pop("send_lease_expires_at", None)
            ledger.pop("recovery_expires_at", None)
            context = dict(task.context or {})
            claim = context.get(APPROVAL_FOLLOWUP_CLAIM_KEY)
            if isinstance(claim, dict):
                current_decision = _decision_from_pending_claim(
                    task, dict(claim), modified_by=decision.modified_by
                )
                if (
                    current_decision is not None
                    and (current_decision.claim or {}).get("token")
                    == (decision.claim or {}).get("token")
                    and await _claim_matches_authoritative_task(
                        task,
                        decision,
                        authority_session,
                        allowed_states=frozenset({"merge_intent"}),
                    )
                ):
                    claim = dict(claim)
                    claim["state"] = "merge_submitted"
                    context[APPROVAL_FOLLOWUP_CLAIM_KEY] = claim
            context[APPROVAL_MERGE_LEDGER_KEY] = ledger
            history = _ledger_history(
                task,
                (
                    "APPROVAL_MERGE_OBSERVED_MERGED"
                    if proof_kind == "observed_merged"
                    else "APPROVAL_MERGE_SUBMITTED"
                ),
                ledger,
            )
            task.context = context
            authority_session.add(task)
            authority_session.add(history)
            await authority_session.commit()

    return _record


def approval_merge_receipt(
    task: Task, decision: ApprovalDecision
) -> Dict[str, Any] | None:
    ledger = (task.context or {}).get(APPROVAL_MERGE_LEDGER_KEY)
    if not isinstance(ledger, dict) or not _ledger_matches_decision(ledger, decision):
        return None
    receipt = ledger.get("merge_fact")
    return dict(receipt) if isinstance(receipt, dict) else None

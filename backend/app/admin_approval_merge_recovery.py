"""History-authoritative recovery and fencing for approval merges."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Project, Task, TaskHistory, _utcnow
from app.admin_approval_merge_protocol import (
    APPROVAL_MERGE_HISTORY_ACTIONS,
    APPROVAL_MERGE_LEDGER_KEY,
    APPROVAL_MERGE_NEGATIVE_CONFIRMATION_SECONDS,
    _authority_token_matches,
    _lease_deadline,
    _lease_is_active,
    _ledger_history,
    _latest_ledgers_by_intent,
    _target_matches_intent,
    approval_merge_ledger,
    approval_merge_receipt_writer,
)
from app.admin_approval_merge_submission import started_merge_authority
from app.task_locking import lock_project_then_task

if TYPE_CHECKING:
    from app.admin_approval_authority import ApprovalDecision


def _negative_merge_proof_fingerprint(ledger: Dict[str, Any]) -> str:
    """Hash the immutable intent/config bound to an exact unmerged proof."""

    identity = {
        "version": 1,
        "owner": str(ledger.get("owner") or "").lower(),
        "repo": str(ledger.get("repo") or "").lower(),
        "project_id": str(ledger.get("project_id") or ""),
        "task_id": str(ledger.get("task_id") or ""),
        "intent_id": str(ledger.get("intent_id") or ""),
        "submission_nonce": str(ledger.get("submission_nonce") or ""),
        "pr_number": int(ledger.get("pr_number") or 0),
        "head_sha": str(ledger.get("head_sha") or ""),
        "head_ref": str(ledger.get("head_ref") or ""),
        "base_ref": str(ledger.get("base_ref") or ""),
        "proof_kind": "exact_unmerged",
        "project_config": copy.deepcopy(ledger.get("project_config") or {}),
    }
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _negative_proof_first_observed_at(proof: Dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(proof.get("first_observed_at") or ""))
    except (TypeError, ValueError):
        return None


async def recover_approval_merge_decision(
    task: Task,
    session: AsyncSession,
) -> ApprovalDecision | None:
    """Rebuild an immutable GET-only recovery receipt without a live claim."""

    from app.admin_approval_authority import ApprovalDecision

    history_ledgers: list[Dict[str, Any]] = []
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
    history_ledgers.extend(
        copy.deepcopy(ledger)
        for history in histories
        if isinstance((ledger := (history.details or {}).get("merge_ledger")), dict)
    )
    cached = (task.context or {}).get(APPROVAL_MERGE_LEDGER_KEY)
    latest_by_intent = _latest_ledgers_by_intent(
        history_ledgers,
        cached=cached if isinstance(cached, dict) else None,
    )
    for ledger in latest_by_intent.values():
        intent_id = str(ledger.get("intent_id") or "")
        if not intent_id:
            continue
        if (
            ledger.get("state") not in {"ambiguous", "reconciling", "merged"}
            or str(ledger.get("task_id") or "") != str(task.id)
            or str(ledger.get("project_id") or "") != str(task.project_id)
            or not ledger.get("approval_decision_token")
            or not ledger.get("pr_number")
            or not ledger.get("head_sha")
            or not ledger.get("head_ref")
            or not ledger.get("base_ref")
        ):
            continue
        claim = copy.deepcopy(ledger.get("approval_claim") or {})
        claim.setdefault("token", ledger.get("approval_decision_token"))
        claim.setdefault("task_id", ledger.get("task_id"))
        claim.setdefault("project_id", ledger.get("project_id"))
        claim.setdefault("followup_kind", ledger.get("followup_kind"))
        claim.setdefault("status", ledger.get("source_status"))
        claim.setdefault("pr_number", ledger.get("pr_number"))
        claim.setdefault("head_sha", ledger.get("head_sha"))
        claim.setdefault("base_sha", ledger.get("base_sha"))
        claim.setdefault("approval_episode", ledger.get("approval_episode"))
        claim["state"] = (
            "merge_submitted" if ledger.get("state") == "merged" else "merge_intent"
        )
        try:
            artifact_id = (
                uuid.UUID(str(claim.get("artifact_id")))
                if claim.get("artifact_id")
                else None
            )
        except (TypeError, ValueError, AttributeError):
            artifact_id = None
        effects = dict(ledger.get("post_effects") or {})
        effects_recoverable = any(
            value == "pending" for value in effects.values()
        ) or effects.get("comment") == "submitting"
        if (
            ledger.get("state") == "merged"
            and ledger.get("semantic_state") in {"finalized", "fact_only_finalized"}
            and not effects_recoverable
        ):
            continue
        return ApprovalDecision(
            task_id=task.id,
            project_id=task.project_id,
            desired=False,
            was_blocked=True,
            status=str(ledger.get("source_status") or ""),
            modified_by=str(claim.get("modified_by") or "USER"),
            should_follow_up=True,
            followup_kind=str(ledger.get("followup_kind") or "") or None,
            claim=claim,
            pr_number=int(ledger.get("pr_number") or 0),
            rollup=ledger.get("followup_kind") == "rollup_merge",
            artifact_id=artifact_id,
            artifact_pr_url=claim.get("artifact_pr_url"),
            merge_intent_id=intent_id,
            post_effects_only=bool(
                ledger.get("semantic_state") == "finalized" and effects_recoverable
            ),
            merge_recovery=True,
            merge_ledger=copy.deepcopy(ledger),
        )
    return None


async def approval_merge_in_flight(task: Task, session: AsyncSession) -> bool:
    """History-authoritative fence for code writes and request-changes."""

    from app.admin_approval_authority import approval_merge_not_sent_is_current

    history_ledgers: list[Dict[str, Any]] = []
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
    history_ledgers.extend(
        copy.deepcopy(ledger)
        for history in histories
        if isinstance((ledger := (history.details or {}).get("merge_ledger")), dict)
    )
    cached = (task.context or {}).get(APPROVAL_MERGE_LEDGER_KEY)
    latest_by_intent = _latest_ledgers_by_intent(
        history_ledgers,
        cached=cached if isinstance(cached, dict) else None,
    )
    return any(
        approval_merge_not_sent_is_current(task, ledger)
        or ledger.get("state") in {"ambiguous", "reconciling"}
        or (
            ledger.get("state") == "merged"
            and ledger.get("semantic_state") not in {"finalized", "fact_only_finalized"}
        )
        for ledger in latest_by_intent.values()
    )


async def _record_exact_unmerged_observation(
    decision: ApprovalDecision,
    session: AsyncSession,
    *,
    target: Dict[str, Any],
    claimed_ledger: Dict[str, Any],
    recovery_token: str,
) -> str:
    """Persist one token-fenced negative GET without doing network I/O.

    The first matching observation releases the reconciliation lease back to
    durable ambiguity.  A matching observation can become terminal proof only
    after the quiescence interval; the exact recovery token remains live until
    the terminal CAS consumes it.
    """

    project_id = decision.project_id
    task_id = decision.task_id
    await session.rollback()
    await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .execution_options(populate_existing=True)
    )
    async with lock_project_then_task(
        session,
        project_id,
        task_id,
        skip_locked=False,
    ) as (project, task):
        if project is None or task is None:
            await session.rollback()
            return "stale"
        ledger = await approval_merge_ledger(task, decision, session)
        expected_config = copy.deepcopy(claimed_ledger.get("project_config") or {})
        if (
            ledger is None
            or ledger.get("state") != "reconciling"
            or str(ledger.get("intent_id") or "")
            != str(claimed_ledger.get("intent_id") or "")
            or str(ledger.get("submission_nonce") or "")
            != str(claimed_ledger.get("submission_nonce") or "")
            or copy.deepcopy(ledger.get("project_config") or {}) != expected_config
            or not _authority_token_matches(ledger, recovery_token)
            or target.get("merged") is not False
            or str(target.get("state") or "").lower() not in {"open", "closed"}
            or not _target_matches_intent(
                target,
                ledger,
                require_checked_base=False,
            )
        ):
            await session.rollback()
            return "stale"

        observed_at = _utcnow()
        fingerprint = _negative_merge_proof_fingerprint(ledger)
        prior = (
            copy.deepcopy(ledger.get("negative_merge_proof"))
            if isinstance(ledger.get("negative_merge_proof"), dict)
            else {}
        )
        first_observed_at = _negative_proof_first_observed_at(prior)
        same_proof = bool(
            first_observed_at is not None
            and first_observed_at <= observed_at
            and str(prior.get("fingerprint") or "") == fingerprint
        )
        if not same_proof:
            first_observed_at = observed_at
            observation_count = 1
        else:
            observation_count = int(prior.get("observation_count") or 0) + 1
        confirmation_not_before = first_observed_at + timedelta(
            seconds=APPROVAL_MERGE_NEGATIVE_CONFIRMATION_SECONDS
        )
        confirmed = bool(
            same_proof
            and observation_count >= 2
            and observed_at >= confirmation_not_before
        )
        proof = {
            "version": 1,
            "fingerprint": fingerprint,
            "first_observed_at": first_observed_at.isoformat(),
            "last_observed_at": observed_at.isoformat(),
            "confirmation_not_before": confirmation_not_before.isoformat(),
            "observation_count": observation_count,
            "first_state": (
                str(prior.get("first_state") or "")
                if same_proof
                else str(target.get("state") or "").lower()
            ),
            "last_state": str(target.get("state") or "").lower(),
            "last_recovery_token": recovery_token,
            **({"confirmed_at": observed_at.isoformat()} if confirmed else {}),
        }
        ledger["negative_merge_proof"] = proof
        ledger["transition_seq"] = int(ledger.get("transition_seq") or 0) + 1
        if not confirmed:
            ledger["state"] = "ambiguous"
            ledger.pop("recovery_token", None)
            ledger.pop("recovery_expires_at", None)
        context = dict(task.context or {})
        context[APPROVAL_MERGE_LEDGER_KEY] = ledger
        task.context = context
        session.add(task)
        session.add(
            _ledger_history(task, "APPROVAL_MERGE_NEGATIVE_OBSERVED", ledger)
        )
        await session.commit()
        return "confirmed" if confirmed else "pending"


async def _claim_approval_merge_reconciliation(
    decision: ApprovalDecision,
    session: AsyncSession,
) -> tuple[Dict[str, Any], str | None] | None:
    """CAS ambiguity to an expiring GET-only authority before any network read."""

    from app.review_merge_authority import MergeSubmissionIndeterminate

    project_id = decision.project_id
    task_id = decision.task_id
    await session.rollback()
    await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .execution_options(populate_existing=True)
    )
    async with lock_project_then_task(
        session,
        project_id,
        task_id,
        skip_locked=False,
    ) as (project, task):
        if project is None or task is None:
            await session.rollback()
            return None
        ledger = await approval_merge_ledger(task, decision, session)
        if ledger is None:
            await session.rollback()
            return None
        if ledger.get("state") == "merged":
            await session.commit()
            return copy.deepcopy(ledger), None
        state = str(ledger.get("state") or "")
        if state == "ambiguous" and _lease_is_active(ledger, prefix="send_lease"):
            await session.rollback()
            raise MergeSubmissionIndeterminate(
                "approval merge sender still owns its durable send lease"
            )
        if state == "reconciling" and _lease_is_active(ledger, prefix="recovery"):
            await session.rollback()
            raise MergeSubmissionIndeterminate(
                "approval merge reconciliation is already leased"
            )
        if state not in {"ambiguous", "reconciling"}:
            await session.rollback()
            return None
        recovery_token = str(uuid.uuid4())
        previous_send_token = str(ledger.get("send_lease_token") or "")
        ledger["state"] = "reconciling"
        ledger["recovery_token"] = recovery_token
        ledger["recovery_expires_at"] = _lease_deadline()
        ledger["reconciliation_started_at"] = _utcnow().isoformat()
        ledger["transition_seq"] = int(ledger.get("transition_seq") or 0) + 1
        if previous_send_token:
            ledger["invalidated_send_lease_token"] = previous_send_token
        ledger.pop("send_lease_token", None)
        ledger.pop("send_lease_expires_at", None)
        context = dict(task.context or {})
        context[APPROVAL_MERGE_LEDGER_KEY] = ledger
        task.context = context
        session.add(task)
        session.add(
            _ledger_history(task, "APPROVAL_MERGE_RECONCILIATION_STARTED", ledger)
        )
        await session.commit()
        return copy.deepcopy(ledger), recovery_token


async def reconcile_approval_merge_ledger(
    decision: ApprovalDecision,
    session: AsyncSession,
) -> bool:
    """Resolve an ambiguous historical intent by GET only; never replay PUT."""

    from app.ci_merge_helpers import (
        _fetch_live_review_target,
        _merge_commit_parent_matches_checked_base,
    )
    from app.github import get_repo_installation_token
    from app.review_merge_authority import (
        MergeAuthorityDecision,
        MergeSubmissionIndeterminate,
    )
    from app.routers.agent import get_github_token_for_task

    claimed = await _claim_approval_merge_reconciliation(decision, session)
    if claimed is None:
        return False
    ledger, recovery_token = claimed
    if ledger.get("state") == "merged":
        return True
    if ledger.get("state") != "reconciling" or not recovery_token:
        return False
    task_id = decision.task_id
    project_id = str(decision.project_id)
    await session.rollback()
    # The Project may have been reconfigured after the one-shot PUT. Query only
    # the repository frozen in the durable intent; ambient owner/repo values
    # cannot retarget historical reconciliation or strand the old intent.
    owner = str(ledger.get("owner") or "").strip().lower()
    repo = str(ledger.get("repo") or "").strip().lower()
    if not owner or not repo:
        raise MergeSubmissionIndeterminate(
            "approval merge frozen repository identity is unavailable"
        )
    try:
        gh_token = await get_repo_installation_token(owner, repo)
    except Exception:  # noqa: BLE001 - exact-repo task credential fallback below
        gh_token = None
    if not gh_token:
        try:
            gh_token, current_owner, current_repo = await get_github_token_for_task(
                task_id,
                session,
                release_dedicated_session_before_io=True,
            )
        except Exception as exc:
            raise MergeSubmissionIndeterminate(
                "approval merge credentials unavailable during GET reconciliation"
            ) from exc
        if (
            str(current_owner or "").strip().lower() != owner
            or str(current_repo or "").strip().lower() != repo
        ):
            raise MergeSubmissionIndeterminate(
                "approval merge credential is not bound to the frozen repository"
            )
    try:
        target = await _fetch_live_review_target(
            gh_token,
            owner,
            repo,
            int(ledger.get("pr_number") or 0),
            project_id=project_id,
        )
    except Exception as exc:
        raise MergeSubmissionIndeterminate(
            "approval merge GitHub GET failed; ambiguous ledger preserved"
        ) from exc
    exact_merged = bool(
        target.get("merged") is True
        and str(target.get("head_sha") or "") == str(ledger.get("head_sha") or "")
        and str(target.get("head_ref") or "") == str(ledger.get("head_ref") or "")
        and str(target.get("base_ref") or "") == str(ledger.get("base_ref") or "")
        and str(target.get("merge_commit_sha") or "")
    )
    if exact_merged:
        try:
            base_parent_verified = await _merge_commit_parent_matches_checked_base(
                gh_token,
                owner,
                repo,
                str(target.get("merge_commit_sha") or ""),
                str(ledger.get("base_sha") or ""),
                project_id=project_id,
            )
        except Exception as exc:
            raise MergeSubmissionIndeterminate(
                "approval merge commit-parent proof unavailable; ledger preserved"
            ) from exc
    exact_unmerged = bool(
        target.get("merged") is False
        and str(target.get("state") or "").lower() in {"open", "closed"}
        # A negative observation is bound by immutable PR/head/ref identity.
        # Ambient base movement cannot turn that exact unmerged head into a
        # historical merge; checked-base parent proof remains mandatory only
        # for the positive merged case above.
        and _target_matches_intent(target, ledger, require_checked_base=False)
    )
    if exact_unmerged:
        negative_proof = await _record_exact_unmerged_observation(
            decision,
            session,
            target=target,
            claimed_ledger=ledger,
            recovery_token=recovery_token,
        )
        if negative_proof == "confirmed":
            started = started_merge_authority(
                decision,
                expected_project_config=copy.deepcopy(
                    ledger.get("project_config") or {}
                ),
                intent_id=str(ledger.get("intent_id") or ""),
                submission_nonce=str(ledger.get("submission_nonce") or ""),
            )
            closed = await started.close_without_merge(
                session,
                "submission_rejected",
                "time-separated exact PR observations remained unmerged after "
                "ambiguous submission",
                recovery_token,
            )
            if closed:
                return False
    if not exact_merged:
        raise MergeSubmissionIndeterminate(
            "approval merge submission remains ambiguous; no PUT replayed"
        )
    merge_decision = MergeAuthorityDecision(
        True,
        str(ledger.get("head_sha") or ""),
        str(ledger.get("base_sha") or ""),
        {},
    )
    writer = approval_merge_receipt_writer(
        decision,
        branch=str(ledger.get("branch") or ""),
        base_branch=str(ledger.get("base_branch") or ""),
    )
    await writer(
        session,
        merge_decision,
        {
            "merged": True,
            "sha": str(target.get("merge_commit_sha") or ""),
            "message": "exact merged PR observed during recovery",
            "observed_merged": True,
            "checked_base_parent_verified": base_parent_verified,
        },
        recovery_token,
    )
    return True

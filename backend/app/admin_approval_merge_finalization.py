"""Lock-safe semantic finalization for an objective approval merge fact."""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.admin_approval_authority import (
    APPROVAL_FOLLOWUP_CLAIM_KEY,
    APPROVAL_HOLD_EPISODE_KEY,
    APPROVAL_HOLD_PROVENANCE_KEY,
    APPROVAL_MERGE_FACT_KEY,
    APPROVAL_MERGE_LEDGER_KEY,
    ApprovalDecision,
    _claim_matches_authoritative_task,
    _claim_matches_task,
    establish_independent_approval_hold,
    revoke_operator_approval_followup,
)
from app.admin_approval_merge_protocol import (
    _ledger_history,
    _project_merge_config,
    approval_merge_ledger,
)
from app.models import (
    Alert,
    AlertSeverity,
    Project,
    Task,
    TaskHistory,
    TaskStatus,
    _utcnow,
)
from app.task_locking import lock_project_then_task

logger = logging.getLogger("app.admin_task_mutations")

_PROOF_PROTECTED_KEYS = frozenset(
    {
        APPROVAL_FOLLOWUP_CLAIM_KEY,
        APPROVAL_HOLD_EPISODE_KEY,
        APPROVAL_HOLD_PROVENANCE_KEY,
        APPROVAL_MERGE_LEDGER_KEY,
        "_approval_set_at",
        "approval_hold_reason",
        "review_loop",
        "review_loop_terminal_outcome",
        "rollup_pr_number",
        "touched_critical",
    }
)


def _classify_merge(
    task: Task,
    project: Project,
    decision: ApprovalDecision,
    *,
    base_branch: str,
) -> tuple[bool, bool, str, bool]:
    """Pure branch classification; alerts are a post-commit effect."""

    from app.branch_resolution import (
        is_custom_staged_rollup_root,
        resolve_pr_landing_branch,
    )
    from app.shared_state import (
        get_project_integration_branch,
        get_project_promotion_target,
    )

    landed_branch = resolve_pr_landing_branch(
        task, project, merged_base_branch=base_branch
    )
    integration = get_project_integration_branch(project)
    promotion = get_project_promotion_target(project)
    promotion_non_landing = bool(
        landed_branch == promotion and promotion != integration
    )
    if decision.followup_kind == "rollup_merge" and is_custom_staged_rollup_root(
        task, project, landed_branch
    ):
        landed_on_integration = landed_branch == integration
        terminal = landed_on_integration or landed_branch == promotion
        return landed_on_integration, terminal, landed_branch, promotion_non_landing
    return not promotion_non_landing, True, landed_branch, promotion_non_landing


async def _prepare_landing_proof(
    decision: ApprovalDecision,
    session: AsyncSession,
) -> tuple[Dict[str, Any] | None, Dict[str, Any]]:
    """Perform all GitHub proof I/O without Project or Task row locks."""

    task = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == decision.task_id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    project = (
        (
            await session.execute(
                select(Project)
                .where(Project.id == decision.project_id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if task is None or project is None:
        await session.rollback()
        return None, {}
    ledger = await approval_merge_ledger(task, decision, session)
    if (
        ledger is None
        or ledger.get("state") != "merged"
        or not isinstance(ledger.get("merge_fact"), dict)
        or not _claim_matches_task(task, decision)
        or _project_merge_config(project) != ledger.get("project_config")
    ):
        await session.rollback()
        return ledger, {}
    receipt = dict(ledger["merge_fact"])
    if (
        receipt.get("merged") is not True
        or receipt.get("checked_base_parent_verified") is not True
        or int(receipt.get("pr_number") or 0) != int(decision.pr_number or 0)
        or str(receipt.get("head_sha") or "") != str(ledger.get("head_sha") or "")
        or str(receipt.get("base_ref") or "") != str(ledger.get("base_ref") or "")
    ):
        await session.rollback()
        return None, {}
    record_pr_merged, _, landed_branch, _ = _classify_merge(
        task,
        project,
        decision,
        base_branch=str(receipt.get("base_branch") or ""),
    )
    baseline = copy.deepcopy(dict(task.context or {}))
    proof_task = Task.model_validate(task.model_dump())
    proof_task.context = copy.deepcopy(baseline)
    if record_pr_merged:
        from app.landed_signal import (
            _stamp_changed_files_before_landing,
            _verify_finding_clearance_before_landing,
        )
        from app.acceptance_gate import corroborate_acceptance_for_landing

        await _stamp_changed_files_before_landing(
            proof_task,
            session,
            project,
            base_branch=landed_branch,
            pr_number=decision.pr_number,
        )
        await corroborate_acceptance_for_landing(
            proof_task,
            session,
            project=project,
            branch=landed_branch,
            commit_sha=str(receipt.get("merge_sha") or "") or None,
        )
        await _verify_finding_clearance_before_landing(
            proof_task,
            session,
            merge_commit_sha=str(receipt.get("merge_sha") or "") or None,
        )
    prepared = dict(proof_task.context or {})
    proof_patch = {
        key: copy.deepcopy(value)
        for key, value in prepared.items()
        if key not in _PROOF_PROTECTED_KEYS and baseline.get(key) != value
    }
    await session.rollback()
    return ledger, proof_patch


async def finalize_approval_merge(
    decision: ApprovalDecision,
    session: AsyncSession,
    changes: Dict[str, Any],
) -> Task | None:
    """Apply semantic landing only to the still-exact approval episode."""

    prepared_ledger, proof_patch = await _prepare_landing_proof(decision, session)
    async with lock_project_then_task(
        session,
        decision.project_id,
        decision.task_id,
        skip_locked=False,
    ) as (project, task):
        if project is None or task is None:
            await session.rollback()
            return None
        ledger = await approval_merge_ledger(task, decision, session)
        if (
            ledger is None
            or ledger.get("state") != "merged"
            or not isinstance(ledger.get("merge_fact"), dict)
        ):
            await session.rollback()
            return None
        # The objective fact is intentionally independent.  A newer hold,
        # replacement, status, artifact, or branch config survives untouched.
        claim_still_current = await _claim_matches_authoritative_task(
            task,
            decision,
            session,
            allowed_states=frozenset({"merge_submitted"}),
        )
        if (
            prepared_ledger is None
            or prepared_ledger.get("intent_id") != ledger.get("intent_id")
            or _project_merge_config(project) != ledger.get("project_config")
            or not claim_still_current
        ):
            if ledger.get("semantic_state") in {
                "finalized",
                "fact_only_finalized",
            }:
                await session.commit()
                return task
            ledger["semantic_state"] = "fact_only_finalized"
            ledger["transition_seq"] = int(ledger.get("transition_seq") or 0) + 1
            base_parent_mismatch = (ledger.get("merge_fact") or {}).get(
                "checked_base_parent_verified"
            ) is not True
            ledger["semantic_block_reason"] = (
                "checked_base_parent_mismatch"
                if base_parent_mismatch
                else "approval_authority_or_project_config_changed"
            )
            ledger["objective_recorded_at"] = _utcnow().isoformat()
            ledger["post_effects"] = {
                "comment": "skipped",
                "branch_cleanup": "skipped",
                "direction_refresh": "skipped",
                "branch_alert": "skipped",
                "smoketest": "skipped",
            }
            context = dict(task.context or {})
            if claim_still_current:
                establish_independent_approval_hold(
                    task,
                    reason=(
                        "merged PR could not be attributed to the checked base"
                        if base_parent_mismatch
                        else "merged PR requires operator reconciliation"
                    ),
                    owner="approval_merge_fact_only",
                )
                task.latest_status_message = (
                    "Merged PR recorded as an objective fact, but normal landing "
                    "was blocked because exact checked-base authority was lost"
                )[:500]
                context = dict(task.context or {})
            context[APPROVAL_MERGE_LEDGER_KEY] = ledger
            context[APPROVAL_MERGE_FACT_KEY] = {
                "intent_id": ledger.get("intent_id"),
                "task_id": ledger.get("task_id"),
                "project_id": ledger.get("project_id"),
                "pr_number": ledger.get("pr_number"),
                "head_sha": ledger.get("head_sha"),
                "base_ref": ledger.get("base_ref"),
                "merge_fact": copy.deepcopy(ledger.get("merge_fact")),
                "semantic_block_reason": ledger.get("semantic_block_reason"),
                "current_attempt_mutated": False,
            }
            task.context = context
            history = _ledger_history(task, "APPROVAL_MERGE_FINALIZED", ledger)
            session.add(task)
            session.add(history)
            if base_parent_mismatch:
                dedupe_key = (
                    f"approval_merge_checked_base_mismatch:{ledger.get('intent_id')}"
                )
                existing_alert = (
                    (
                        await session.execute(
                            select(Alert).where(
                                Alert.project_id == task.project_id,
                                Alert.dedupe_key == dedupe_key,
                                Alert.dismissed_at.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if existing_alert is None:
                    session.add(
                        Alert(
                            project_id=task.project_id,
                            task_id=task.id,
                            severity=AlertSeverity.ERROR,
                            title="Approval merge base attribution mismatch",
                            message=(
                                f"PR #{ledger.get('pr_number')} merged at "
                                f"{(ledger.get('merge_fact') or {}).get('merge_sha')}, "
                                "but its immutable first parent did not match the "
                                "operator-approved checked base. The objective merge "
                                "fact was retained without clearing the approval hold."
                            ),
                            domain="pipeline",
                            event_type="approval.merge_checked_base_mismatch",
                            source="admin_approval_merge_finalization",
                            dedupe_key=dedupe_key,
                        )
                    )
            await session.commit()
            changes["approval_merge_fact_recorded"] = decision.pr_number
            return task
        receipt = dict(ledger["merge_fact"])
        record_pr_merged, terminal_merge, landed_branch, needs_branch_alert = (
            _classify_merge(
                task,
                project,
                decision,
                base_branch=str(receipt.get("base_branch") or ""),
            )
        )
        context = dict(task.context or {})
        context.update(proof_patch)
        task.context = context
        if record_pr_merged:
            # Deliberately use the sync setter here.  _prepare_landing_proof
            # already completed the corroborating GitHub reads on a detached
            # clone before these canonical Project -> Task locks were acquired.
            # The async twin would repeat external I/O while both rows are
            # locked, defeating the two-phase finalization protocol.
            from app.landed_signal import apply_merged_landed_signal

            apply_merged_landed_signal(
                task,
                project,
                base_branch=landed_branch,
                hotfix_lane=bool(
                    task.first_payload.get("hotfix") or context.get("hotfix")
                ),
                non_merge_reason="merged_pr_non_integration_branch",
                merge_commit_sha=str(receipt.get("merge_sha") or "") or None,
                onboarding_proof_authorized=True,
            )
        elif bool(task.first_payload.get("hotfix") or context.get("hotfix")):
            from app.landed_signal import mark_unblocked_non_merge

            mark_unblocked_non_merge(task, reason="merged_pr_non_integration_branch")
        task.status = TaskStatus.COMPLETED if terminal_merge else TaskStatus.EVALUATING
        revoke_operator_approval_followup(task)
        task.requires_approval = False
        ledger["semantic_state"] = "finalized"
        ledger["transition_seq"] = int(ledger.get("transition_seq") or 0) + 1
        ledger["semantic_finalized_at"] = _utcnow().isoformat()
        ledger["record_pr_merged"] = record_pr_merged
        ledger["terminal_merge"] = terminal_merge
        ledger["landed_branch"] = landed_branch
        from app.shared_state import get_project_promotion_target

        ledger["post_effects"] = {
            "comment": "pending",
            # GitHub's ref-delete API has no expected-SHA CAS. A ref can be
            # reused between GET and DELETE, so deferred approval cleanup cannot
            # safely delete it; leave cleanup to the normal lifecycle sweeper.
            "branch_cleanup": "skipped",
            "direction_refresh": "pending" if record_pr_merged else "skipped",
            "branch_alert": "skipped",
            "smoketest": (
                "pending"
                if decision.followup_kind == "rollup_merge"
                and landed_branch == get_project_promotion_target(project)
                else "skipped"
            ),
        }
        current_context = dict(task.context or {})
        current_context[APPROVAL_MERGE_LEDGER_KEY] = ledger
        task.context = current_context
        if decision.followup_kind == "rollup_merge" and not terminal_merge:
            task.latest_status_message = (
                f"Human-approved merge of rollup PR #{decision.pr_number} "
                f"into {landed_branch}; awaiting downstream landing"
            )[:500]
        else:
            label = "rollup PR" if decision.followup_kind == "rollup_merge" else "PR"
            task.latest_status_message = (
                f"Human-approved merge of {label} #{decision.pr_number}"
            )[:500]
        persisted_changes: Dict[str, Any] = {
            "requires_approval": False,
            "approval_followup_pending": False,
            "merged_pr": decision.pr_number,
            "status": task.status.value,
        }
        ledger_history = _ledger_history(task, "APPROVAL_MERGE_FINALIZED", ledger)
        modified_history = TaskHistory(
            task_id=task.id,
            action="MODIFIED",
            details={
                "changes": persisted_changes,
                "modified_by": decision.modified_by,
                "approval_decision_token": (decision.claim or {}).get("token"),
                "merge_intent_id": ledger.get("intent_id"),
            },
        )
        session.add(project)
        session.add(task)
        session.add(ledger_history)
        session.add(modified_history)
        if needs_branch_alert:
            alert_dedupe_key = (
                f"approval_merge_promotion_target:{ledger.get('intent_id')}"
            )
            existing_alert = (
                (
                    await session.execute(
                        select(Alert).where(
                            Alert.project_id == task.project_id,
                            Alert.dedupe_key == alert_dedupe_key,
                            Alert.dismissed_at.is_(None),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing_alert is None:
                session.add(
                    Alert(
                        project_id=task.project_id,
                        task_id=task.id,
                        severity=AlertSeverity.ERROR,
                        title="Merged PR landed on promotion target before integration",
                        message=(
                            f"Human-approved PR #{decision.pr_number} landed on "
                            f"{landed_branch}, the promotion target, without an "
                            "integration-branch landing. The objective merge is "
                            "recorded but remains operationally unqualified."
                        ),
                        domain="pipeline",
                        event_type="branch.promotion_target_landing",
                        source="admin_approval_merge_finalization",
                        dedupe_key=alert_dedupe_key,
                    )
                )
        await session.commit()
        changes.update(persisted_changes)
        return task


async def record_approval_merge_post_effect(
    decision: ApprovalDecision,
    session: AsyncSession,
    *,
    effect: str,
    outcome: str = "done",
) -> bool:
    """CAS one idempotent post-commit effect marker on the objective ledger."""

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
        or ledger.get("semantic_state") not in {"finalized", "fact_only_finalized"}
    ):
        await session.rollback()
        return False
    effects = dict(ledger.get("post_effects") or {})
    if effects.get(effect) != "pending":
        await session.commit()
        return effects.get(effect) == outcome
    effects[effect] = outcome
    ledger["post_effects"] = effects
    ledger["transition_seq"] = int(ledger.get("transition_seq") or 0) + 1
    context = dict(task.context or {})
    context[APPROVAL_MERGE_LEDGER_KEY] = ledger
    task.context = context
    history = _ledger_history(task, "APPROVAL_MERGE_POST_EFFECT", ledger)
    session.add(task)
    session.add(history)
    await session.commit()
    return True

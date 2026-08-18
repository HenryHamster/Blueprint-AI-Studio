"""Resumable idempotent effects after approval merge finalization."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.admin_approval_authority import ApprovalDecision
from app.models import Task

logger = logging.getLogger("app.admin_task_mutations")


def _mutation_facade():
    import app.admin_task_mutations as facade

    return facade


async def run_approval_merge_post_effects(
    decision: ApprovalDecision,
    session: AsyncSession,
) -> None:
    """Best-effort, intent-idempotent effects after objective finalization."""

    from app.admin_approval_merge_finalization import (
        record_approval_merge_post_effect,
    )
    from app.admin_approval_merge_protocol import approval_merge_ledger

    async def _fresh_task() -> Task | None:
        if session.in_transaction():
            await session.rollback()
        return (
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

    task = await _fresh_task()
    if task is None:
        return
    ledger = await approval_merge_ledger(task, decision, session)
    if (
        ledger is None
        or ledger.get("state") != "merged"
        or ledger.get("semantic_state") != "finalized"
    ):
        await session.rollback()
        return
    effects = dict(ledger.get("post_effects") or {})
    intent_id = str(ledger.get("intent_id") or "")
    if not intent_id:
        await session.rollback()
        return

    credentials: tuple[str, str, str] | None = None
    if (
        effects.get("comment") in {"pending", "submitting"}
        or effects.get("smoketest") == "pending"
    ):
        try:
            from app.routers.agent import get_github_token_for_task

            credentials = await get_github_token_for_task(task.id, session)
            if credentials[1].strip().lower() != str(
                ledger.get("owner") or ""
            ) or credentials[2].strip().lower() != str(ledger.get("repo") or ""):
                credentials = None
        except Exception:
            logger.exception(
                "[ADMIN] Approval merge post-effects could not resolve credentials"
            )

    if (
        effects.get("comment") in {"pending", "submitting"}
        and credentials is not None
    ):
        try:
            from app.admin_approval_comment_delivery import (
                deliver_approval_merge_comment,
            )

            await deliver_approval_merge_comment(decision, session, credentials)
        except Exception:
            logger.exception(
                "[ADMIN] Approval merge comment failed for intent %s; retryable",
                intent_id,
            )

    if effects.get("direction_refresh") == "pending":
        try:
            task = await _fresh_task()
            if task is None:
                return
            refreshed = await _mutation_facade()._refresh_direction_outcomes_for_task(
                task
            )
            if refreshed is not True:
                raise RuntimeError("direction refresh was not acknowledged")
            await record_approval_merge_post_effect(
                decision, session, effect="direction_refresh"
            )
        except Exception:
            logger.exception(
                "[ADMIN] Approval merge direction refresh failed for intent %s",
                intent_id,
            )

    if effects.get("smoketest") == "pending" and credentials is not None:
        try:
            from app.shared_state import runtime_config

            if runtime_config.get("smoketest_enabled"):
                task = await _fresh_task()
                if task is None:
                    return
                spawned = await _mutation_facade()._maybe_spawn_rollup_smoketest(
                    task,
                    credentials[1],
                    credentials[2],
                    str(ledger.get("landed_branch") or ""),
                    session,
                    spawned_by=f"ADMIN_APPROVAL_MERGE:{intent_id}",
                )
                if spawned is not True:
                    raise RuntimeError("approval smoketest was not durably published")
            await record_approval_merge_post_effect(
                decision, session, effect="smoketest"
            )
        except Exception:
            logger.exception(
                "[ADMIN] Approval merge smoketest failed for intent %s",
                intent_id,
            )

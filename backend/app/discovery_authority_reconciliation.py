"""Bounded reconciliation for discovery cycles stalled on authority checks.

The discovery decision boundary freezes direction authority when a cycle is
commissioned.  Mutable research evidence is deliberately outside that fence:
an old false mismatch can be retried, while a genuine direction edit
supersedes the stale cycle so a fresh generation can freeze current authority.

All helpers mutate the caller's transaction and never commit.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from sqlmodel import select

from app.discovery_admission import (
    POLICY_BLOCKING_DISCOVERY_VERDICTS,
    direction_authority_matches,
)
from app.models import DiscoveryCycle, Project, Task
from app.notification_dispatch import post_event_message


_AUTHORITY_DRIFT_ERROR_PREFIX = "direction authority changed before "
_AUTHORITY_RECONCILIATION_MAX_ATTEMPTS = 3
_DISCOVERY_REVIEW_TASK_TYPE = "discovery_review"


async def _current_direction_for_cycle(
    session,
    cycle: DiscoveryCycle,
) -> tuple[bool, Mapping[str, Any] | None]:
    """Read current direction authority while holding its canonical row lock."""

    from app.task_locking import project_lock

    current: Mapping[str, Any] | None = None
    async with project_lock(
        session,
        cycle.project_id,
        load_columns=[Project.id, Project.directions],
    ) as project:
        if project is None:
            return False, None
        current = next(
            (
                direction
                for direction in list(project.directions or [])
                if str(direction.get("id") or "") == cycle.direction_key
            ),
            None,
        )
    return True, current


async def reconcile_stalled_direction_authority(
    session,
    *,
    cycle: DiscoveryCycle,
    now: datetime,
) -> str | None:
    """Reconcile one stale decision state without weakening authority fences.

    The caller's sweep is bounded and row-locks each cycle. Evidence-only
    drift resumes exactly within the three-decision budget; real authority
    drift supersedes the old generation so the scheduler can freeze a fresh
    cycle. Other decision failures remain fail-closed for an explicit
    disposition.
    """

    if cycle.state not in {"review_pending", "review_blocked"} and not (
        cycle.state == "reviewed"
        and cycle.verdict in POLICY_BLOCKING_DISCOVERY_VERDICTS
    ):
        return None
    frozen = (cycle.bet_snapshot or {}).get("direction")
    authority_loaded, current = await _current_direction_for_cycle(session, cycle)
    if not authority_loaded:
        return "authority_reconciliation_deferred"
    if isinstance(frozen, Mapping) and current is None:
        # Direction removal is a real authority change, not missing proof.
        authority_match = False
    elif isinstance(current, Mapping) and str(
        current.get("status") or "active"
    ).strip().lower() != "active":
        # Lifecycle authority no longer permits an active-direction decision.
        authority_match = False
    else:
        authority_match = direction_authority_matches(frozen, current)
    prior = dict(cycle.review_result or {})

    if authority_match is False:
        previous_state = cycle.state
        previous_verdict = cycle.verdict
        cycle.state = "superseded"
        cycle.review_result = {
            **prior,
            "status": "AUTHORITY_SUPERSEDED",
            "reason_code": "direction_authority_changed",
            "reason": "Current direction authority differs from the frozen commission.",
            "next_action": "Commission a fresh cycle from current direction authority.",
            "superseded_state": previous_state,
            "superseded_verdict": previous_verdict,
            "superseded_at": now.isoformat(),
        }
        cycle.revision = int(cycle.revision or 0) + 1
        cycle.updated_at = now
        session.add(cycle)
        await post_event_message(
            session,
            cycle.project_id,
            event_type="discovery.authority_superseded",
            summary=(
                f"Superseded stale discovery cycle for {cycle.direction_key}; "
                "current direction authority will be recommissioned."
            ),
            context={
                "cycle_id": str(cycle.id),
                "cycle_revision": cycle.revision,
                "direction_key": cycle.direction_key,
                "previous_state": previous_state,
                "previous_verdict": previous_verdict,
                "reason_code": "direction_authority_changed",
                "next_action": "commission_current_direction",
            },
            task_id=cycle.review_task_id or cycle.source_research_task_id,
            idempotency_key=f"discovery-authority-superseded:{cycle.id}:{cycle.revision}",
        )
        return "authority_superseded"

    if authority_match is None:
        if cycle.state == "reviewed":
            return None
        if prior.get("status") == "AUTHORITY_PROOF_MISSING":
            return "authority_proof_missing_unchanged"
        cycle.state = "review_blocked"
        cycle.review_result = {
            **prior,
            "status": "AUTHORITY_PROOF_MISSING",
            "reason_code": "direction_authority_proof_missing",
            "reason": "Frozen or current direction authority is unavailable.",
            "next_action": "Resolve, park, or close the cycle explicitly.",
        }
        cycle.revision = int(cycle.revision or 0) + 1
        cycle.updated_at = now
        session.add(cycle)
        return "authority_proof_missing"

    error = str(prior.get("error") or "").strip()
    if cycle.state != "review_blocked" or not error.startswith(
        _AUTHORITY_DRIFT_ERROR_PREFIX
    ):
        return None

    review_tasks = (
        (
            await session.execute(
                select(Task).where(Task.discovery_cycle_id == cycle.id)
            )
        )
        .scalars()
        .all()
    )
    attempts = sum(
        1
        for task in review_tasks
        if task.first_payload.get("type") == _DISCOVERY_REVIEW_TASK_TYPE
    )
    if attempts >= _AUTHORITY_RECONCILIATION_MAX_ATTEMPTS:
        if prior.get("reason_code") != "authority_reconciliation_budget_exhausted":
            cycle.review_result = {
                **prior,
                "reason_code": "authority_reconciliation_budget_exhausted",
                "next_action": "Resolve, park, or close the cycle explicitly.",
                "reconciliation_attempts": attempts,
            }
            cycle.revision = int(cycle.revision or 0) + 1
            cycle.updated_at = now
            session.add(cycle)
        return "authority_reconciliation_budget_exhausted"

    stale_review_task_id = cycle.review_task_id
    cycle.state = "review_pending"
    cycle.review_task_id = None
    cycle.review_result = {
        "status": "AUTHORITY_RECONCILED",
        "reason_code": "evidence_only_authority_drift",
        "reason": "True direction authority is unchanged after canonical normalization.",
        "next_action": "Retry the bounded discovery decision.",
        "previous_error": error,
        "stale_review_task_id": str(stale_review_task_id)
        if stale_review_task_id is not None
        else None,
        "reconciliation_attempts": attempts,
    }
    cycle.revision = int(cycle.revision or 0) + 1
    cycle.updated_at = now
    session.add(cycle)
    await post_event_message(
        session,
        cycle.project_id,
        event_type="discovery.authority_reconciled",
        summary=(
            f"Resumed discovery review for {cycle.direction_key}; only mutable "
            "research evidence changed."
        ),
        context={
            "cycle_id": str(cycle.id),
            "cycle_revision": cycle.revision,
            "direction_key": cycle.direction_key,
            "reason_code": "evidence_only_authority_drift",
            "next_action": "retry_discovery_review",
            "reconciliation_attempts": attempts,
        },
        task_id=stale_review_task_id or cycle.source_research_task_id,
        idempotency_key=f"discovery-authority-reconciled:{cycle.id}:{cycle.revision}",
    )
    return "authority_reconciled"

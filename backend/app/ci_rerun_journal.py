"""Durable state machine for crash-safe GitHub Actions rerun mutations."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm.attributes import flag_modified

from app.admin_approval_authority import (
    APPROVAL_HOLD_PROVENANCE_KEY,
    approval_hold_provenance,
    consume_approval_hold_metadata,
)
from app.models import Task, TaskStatus, _utcnow


_CI_RERUN_RECHECK_SECONDS = 15
_CI_RERUN_SEND_LEASE_SECONDS = 15
_CI_RERUN_ACCEPTANCE_FENCE_SECONDS = 90
_CI_RERUN_JOURNAL_KEY = "ci_rerun_journal"
_CI_RERUN_HOLD_STATES = {"reserved", "accepted", "ambiguous"}


def _failed_run_attempts(ci_result: dict) -> dict[str, int]:
    raw_attempts = ci_result.get("failed_run_attempts") or {}
    if not isinstance(raw_attempts, dict):
        return {}
    return {
        str(run_id): attempt
        for run_id, attempt in raw_attempts.items()
        if isinstance(attempt, int) and not isinstance(attempt, bool)
    }


def _clear_ci_rerun_journal(task: Task) -> None:
    context = dict(task.context or {})
    if context.pop(_CI_RERUN_JOURNAL_KEY, None) is None:
        return
    task.context = context
    flag_modified(task, "context")


def _ci_rerun_identity(
    task: Task,
    *,
    ci_pr_number: int | None,
    checked_head_sha: str | None,
) -> tuple[int | None, str]:
    context = dict(task.context or {})
    review_loop = dict(context.get("review_loop") or {})
    exact_head_sha = str(
        checked_head_sha or review_loop.get("head_sha") or ""
    ).strip()
    return ci_pr_number, exact_head_sha


def _failed_run_ids(ci_result: dict) -> list[int]:
    return sorted(
        {
            int(run_id)
            for run_id in (ci_result.get("failed_run_ids") or [])
            if isinstance(run_id, int) and not isinstance(run_id, bool)
        }
    )


def _ci_rerun_deadline(value: object) -> datetime | None:
    try:
        deadline = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    now = _utcnow()
    if now.tzinfo is None and deadline.tzinfo is not None:
        return deadline.astimezone(timezone.utc).replace(tzinfo=None)
    if now.tzinfo is not None and deadline.tzinfo is None:
        return deadline.replace(tzinfo=timezone.utc)
    return deadline


def _ci_rerun_sender_lease_active(journal: dict) -> bool:
    lease_until = _ci_rerun_deadline(journal.get("sender_lease_until"))
    return lease_until is not None and _utcnow() < lease_until


def _reserve_ci_rerun(
    task: Task,
    ci_result: dict,
    *,
    ci_pr_number: int | None,
    checked_head_sha: str | None,
    kind: str,
) -> str | None:
    """Durably-authorizable journal state, populated before any GitHub POST."""
    pr_number, exact_head_sha = _ci_rerun_identity(
        task,
        ci_pr_number=ci_pr_number,
        checked_head_sha=checked_head_sha,
    )
    failure_run_ids = _failed_run_ids(ci_result)
    if not exact_head_sha or pr_number is None or not failure_run_ids:
        return None

    now = _utcnow()
    journal_id = str(uuid.uuid4())
    context = dict(task.context or {})
    review_loop = dict(context.get("review_loop") or {})
    prior_poll = {
        "ci_last_polled_present": "ci_last_polled" in review_loop,
        "ci_last_polled": review_loop.get("ci_last_polled"),
        "ci_poll_interval_present": "ci_poll_interval" in review_loop,
        "ci_poll_interval": review_loop.get("ci_poll_interval"),
    }
    journal = {
        "id": journal_id,
        "state": "reserved",
        "kind": kind,
        "pr_number": int(pr_number),
        "head_sha": exact_head_sha,
        "failure_run_ids": failure_run_ids,
        "run_attempts": _failed_run_attempts(ci_result),
        "run_outcomes": {str(run_id): "reserved" for run_id in failure_run_ids},
        "reserved_at": now.isoformat(),
        "sender_lease_until": (
            now + timedelta(seconds=_CI_RERUN_SEND_LEASE_SECONDS)
        ).isoformat(),
        "deadline_at": (
            now + timedelta(seconds=_CI_RERUN_ACCEPTANCE_FENCE_SECONDS)
        ).isoformat(),
        "prior_poll": prior_poll,
    }
    if kind == "infra":
        journal["prior_budget_value"] = context.get("ci_infra_rerun_done")
        journal["prior_budget_present"] = "ci_infra_rerun_done" in context
        context["ci_infra_rerun_done"] = True
    else:
        consumed = context.get("ci_rerun_consumed", 0)
        journal["prior_budget_value"] = consumed
        journal["prior_budget_present"] = "ci_rerun_consumed" in context
        context["ci_rerun_consumed"] = consumed + 1
    context[_CI_RERUN_JOURNAL_KEY] = journal
    task.context = context
    flag_modified(task, "context")
    return journal_id


def _ci_rerun_journal_disposition(
    task: Task,
    ci_result: dict,
    *,
    ci_pr_number: int | None,
    checked_head_sha: str | None,
) -> str:
    """Classify a prior journal as none, hold, distinct, rejected, or expired."""
    context = dict(task.context or {})
    journal = context.get(_CI_RERUN_JOURNAL_KEY)
    if not isinstance(journal, dict):
        return "none"
    pr_number, exact_head_sha = _ci_rerun_identity(
        task,
        ci_pr_number=ci_pr_number,
        checked_head_sha=checked_head_sha,
    )
    if (
        not exact_head_sha
        or pr_number is None
        or str(journal.get("head_sha") or "") != exact_head_sha
        or str(journal.get("pr_number")) != str(pr_number)
    ):
        _clear_ci_rerun_journal(task)
        return "distinct"

    observed_run_ids = set(_failed_run_ids(ci_result))
    fenced_run_ids = {
        int(run_id)
        for run_id in (journal.get("failure_run_ids") or [])
        if isinstance(run_id, int) and not isinstance(run_id, bool)
    }
    if observed_run_ids != fenced_run_ids:
        # A newly observed workflow-run ID is positive evidence of a distinct
        # attempt even when the fan-out was partial. Disappearance of a fenced
        # ID is trustworthy only when github_checks proved that every workflow
        # run and jobs page was projected successfully.
        if observed_run_ids - fenced_run_ids or ci_result.get(
            "failed_run_ids_complete"
        ) is True:
            _clear_ci_rerun_journal(task)
            return "distinct"

    observed_attempts = _failed_run_attempts(ci_result)
    fenced_attempts = journal.get("run_attempts") or {}
    if isinstance(fenced_attempts, dict):
        for run_id, old_attempt in fenced_attempts.items():
            new_attempt = observed_attempts.get(str(run_id))
            if (
                isinstance(old_attempt, int)
                and not isinstance(old_attempt, bool)
                and isinstance(new_attempt, int)
                and not isinstance(new_attempt, bool)
                and new_attempt > old_attempt
            ):
                _clear_ci_rerun_journal(task)
                return "distinct"

    state = str(journal.get("state") or "")
    if state == "rejected":
        return "rejected"
    if state == "timed_out":
        return "expired"
    if state not in _CI_RERUN_HOLD_STATES:
        return "expired"

    deadline = _ci_rerun_deadline(journal.get("deadline_at"))
    if deadline is None:
        return "expired"
    now = _utcnow()
    if now >= deadline:
        return "expired"

    if state == "reserved":
        # The reserving handler commits before reacquiring the row for its
        # final pre-send validation. A concurrent poll/webhook must not steal
        # that live reservation during this bounded lease; after the lease a
        # crash is conservatively recovered as an ambiguous possible send.
        if _ci_rerun_sender_lease_active(journal):
            return "hold"
        journal = dict(journal)
        journal["state"] = "ambiguous"
        journal["recovered_at"] = _utcnow().isoformat()
        context[_CI_RERUN_JOURNAL_KEY] = journal
        task.context = context
        flag_modified(task, "context")
    return "hold"


def _record_ci_rerun_outcomes(
    task: Task, journal_id: str, outcomes: dict[int, str]
) -> str | None:
    context = dict(task.context or {})
    journal = context.get(_CI_RERUN_JOURNAL_KEY)
    if not isinstance(journal, dict) or journal.get("id") != journal_id:
        return None
    journal = dict(journal)
    journal["run_outcomes"] = {
        str(run_id): outcome for run_id, outcome in sorted(outcomes.items())
    }
    if any(
        outcome not in {"accepted", "rejected"} for outcome in outcomes.values()
    ):
        state = "ambiguous"
    elif any(outcome == "accepted" for outcome in outcomes.values()):
        state = "accepted"
    else:
        state = "rejected"
    journal["state"] = state
    journal["updated_at"] = _utcnow().isoformat()
    context[_CI_RERUN_JOURNAL_KEY] = journal
    task.context = context
    flag_modified(task, "context")
    return state


def _rollback_rejected_ci_rerun(task: Task) -> None:
    context = dict(task.context or {})
    journal = context.get(_CI_RERUN_JOURNAL_KEY)
    if not isinstance(journal, dict) or journal.get("state") != "rejected":
        return
    kind = journal.get("kind")
    budget_key = (
        "ci_infra_rerun_done" if kind == "infra" else "ci_rerun_consumed"
    )
    if journal.get("prior_budget_present"):
        context[budget_key] = journal.get("prior_budget_value")
    else:
        context.pop(budget_key, None)

    review_loop = dict(context.get("review_loop") or {})
    prior_poll = journal.get("prior_poll") or {}
    for key in ("ci_last_polled", "ci_poll_interval"):
        if prior_poll.get(f"{key}_present"):
            review_loop[key] = prior_poll.get(key)
        else:
            review_loop.pop(key, None)
    context["review_loop"] = review_loop
    task.context = context
    flag_modified(task, "context")


def _mark_ci_rerun_timed_out(task: Task) -> None:
    context = dict(task.context or {})
    journal = context.get(_CI_RERUN_JOURNAL_KEY)
    if isinstance(journal, dict):
        journal = dict(journal)
        journal["state"] = "timed_out"
        journal["timed_out_at"] = _utcnow().isoformat()
        context[_CI_RERUN_JOURNAL_KEY] = journal
    task.context = context
    flag_modified(task, "context")


def resolve_timed_out_ci_rerun(
    task: Task,
    *,
    journal_id: str,
    disposition: str,
    resolved_by: str,
    note: str,
) -> dict:
    """Apply an authenticated operator's exact timed-out journal verdict."""
    context = dict(task.context or {})
    journal = context.get(_CI_RERUN_JOURNAL_KEY)
    provenance = context.get(APPROVAL_HOLD_PROVENANCE_KEY)
    hold_episode = approval_hold_provenance(task)
    if not (
        task.status == TaskStatus.EVALUATING
        and task.requires_approval is True
        and isinstance(journal, dict)
        and journal.get("id") == journal_id
        and journal.get("state") == "timed_out"
        and isinstance(provenance, dict)
        and provenance.get("owner") == "ci_completion"
        and provenance.get("reason") == "ci_rerun_outcome_unconfirmed"
        and provenance.get("episode")
        and provenance.get("episode") == hold_episode.get("episode")
        and provenance.get("set_at") == hold_episode.get("set_at")
        and hold_episode.get("reason") == "ci_rerun_outcome_unconfirmed"
        and hold_episode.get("review_wait_hold") is None
    ):
        raise ValueError("CI rerun journal is not the current operator hold")
    if disposition not in {"accepted", "rejected"}:
        raise ValueError("Unsupported CI rerun disposition")

    now = _utcnow()
    journal = dict(journal)
    journal["state"] = disposition
    journal["updated_at"] = now.isoformat()
    journal["operator_resolution"] = {
        "disposition": disposition,
        "resolved_at": now.isoformat(),
        "resolved_by": str(resolved_by),
        "note": str(note),
    }
    if disposition == "accepted":
        journal["deadline_at"] = (
            now + timedelta(seconds=_CI_RERUN_ACCEPTANCE_FENCE_SECONDS)
        ).isoformat()
    context[_CI_RERUN_JOURNAL_KEY] = journal
    task.context = context
    flag_modified(task, "context")

    if disposition == "rejected":
        _rollback_rejected_ci_rerun(task)
    context = dict(task.context or {})
    review_loop = dict(context.get("review_loop") or {})
    review_loop["ci_last_polled"] = None
    review_loop["ci_poll_interval"] = _CI_RERUN_RECHECK_SECONDS
    context["review_loop"] = review_loop
    task.context = context
    consumed = consume_approval_hold_metadata(
        task,
        expected_episode=hold_episode,
    )
    if not consumed:
        raise ValueError("CI rerun operator hold changed during resolution")
    task.requires_approval = False
    task.latest_status_message = (
        f"Operator verified CI rerun {disposition} on PR "
        f"#{journal.get('pr_number')} - resuming CI reconciliation"
    )
    flag_modified(task, "context")
    return dict((task.context or {})[_CI_RERUN_JOURNAL_KEY])

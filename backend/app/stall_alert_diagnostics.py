"""Stall-alert diagnostic helpers extracted from orchestrator.py.

Pure functions that classify and format pipeline stall conditions:
  - _compute_all_accounts_blocked_action: debounce logic for the stall alert
  - _compute_retry_after_escalation_action: escalation decision for retry_after loops
  - _missing_runtime_credentials_stall_reason: human-readable no-credentials reason
  - _format_stall_alert: builds the (title, body) pair for a stall notification
"""

from datetime import datetime


_STALL_TITLE_PREFIX = "Pipeline stalled —"


def _compute_all_accounts_blocked_action(
    blocked_count: int, dispatched_count: int, already_alerted: bool
) -> str:
    """Decide how to handle the all-accounts-blocked stall signal after a cycle.

    Returns 'fire' (emit a new stall alert), 'reset' (re-arm the debounce),
    or 'noop' (leave state untouched).
    """
    if blocked_count > 0 and dispatched_count == 0:
        return "noop" if already_alerted else "fire"
    if dispatched_count > 0:
        return "reset"
    return "noop"


def _compute_retry_after_escalation_action(
    resets: int, alert_threshold: int, fail_threshold: int
) -> str | None:
    """Decide how to escalate a task that keeps cycling through retry_after holds.

    Pool exhaustion is a wait condition, not a terminal one: when every account
    is rate-limited the right behaviour is to wait for recovery, not to drop the
    task on the floor. We fire a WARNING once `resets` crosses `alert_threshold`
    so the situation is visible, and never escalate to 'fail'. `fail_threshold`
    is accepted for call-site compatibility but ignored.
    """
    del fail_threshold
    if alert_threshold > 0 and resets >= alert_threshold:
        return "alert"
    return None


def _missing_runtime_credentials_stall_reason(
    *,
    user_pool_count: int,
    provider_account_count: int,
    ready_provider_account_count: int,
    runtime_provider: str | None,
) -> str:
    """Describe why dispatch cannot find any usable runtime credentials."""
    if user_pool_count == 0:
        return "no accounts configured"
    if runtime_provider and provider_account_count == 0:
        return f"no accounts configured for provider {runtime_provider}"
    if ready_provider_account_count == 0:
        provider_suffix = (
            f" for provider {runtime_provider}" if runtime_provider else ""
        )
        return f"no ready accounts{provider_suffix}"
    return "no eligible accounts or slot contention"


def _format_stall_alert(
    blockage_reasons: set[str] | None, earliest_recovery: datetime | None
) -> tuple[str, str]:
    """Build a truthful (title, body) for the all-accounts-blocked stall alert
    from the per-cycle blockage reasons emitted by describe_pool_blockage().

    A stall can come from rate-limit cooldown, slot/daily-cap contention,
    missing accounts, or a mix. Each shape gets a distinct title so the
    operator alert is not lying about why work isn't dispatching.

    The title says "eligible" rather than "all" because describe_pool_blockage
    summarizes per-task-model constraints — when only the model-eligible
    candidates are exhausted, other accounts in the pool can still serve
    different models. Calling that "all" was the 2026-04-24 misleading-alert
    bug where the user saw "all rate-limited" while 50 other tasks completed.

    With no reasons supplied (legacy callers / tests), fall back to the
    rate-limit wording so dedup keys and copy stay stable.
    """
    recovery_iso = earliest_recovery.isoformat() if earliest_recovery else None
    reasons = sorted(blockage_reasons) if blockage_reasons else []
    if not reasons:
        title = f"{_STALL_TITLE_PREFIX} eligible Max plan accounts rate-limited"
        body = (
            "Every dispatchable task was skipped because the model-eligible "
            "Max plan accounts are in rate-limit cooldown. "
            + (
                f"Earliest recovery: {recovery_iso}."
                if recovery_iso
                else "Waiting for cooldown."
            )
        )
        return title, body
    rate_limited = any(
        "rate-limit cooldown" in r or "rate-limited" in r for r in reasons
    )
    capacity = any("slot contention" in r or "at capacity" in r for r in reasons)
    no_pool_accounts = any(r == "no accounts configured" for r in reasons)
    no_provider_accounts = any(
        "no accounts configured for provider" in r for r in reasons
    )
    no_ready_accounts = any("no ready accounts" in r for r in reasons)
    if (
        no_pool_accounts
        and not no_provider_accounts
        and not no_ready_accounts
        and not rate_limited
        and not capacity
    ):
        title = f"{_STALL_TITLE_PREFIX} no Max plan accounts configured"
    elif (
        (no_pool_accounts or no_provider_accounts or no_ready_accounts)
        and not rate_limited
        and not capacity
    ):
        title = f"{_STALL_TITLE_PREFIX} no Max plan accounts ready"
    elif rate_limited and capacity:
        title = f"{_STALL_TITLE_PREFIX} eligible Max plan accounts unavailable (rate limits + capacity)"
    elif rate_limited:
        title = f"{_STALL_TITLE_PREFIX} eligible Max plan accounts rate-limited"
    elif capacity:
        title = f"{_STALL_TITLE_PREFIX} eligible Max plan accounts at capacity"
    else:
        title = f"{_STALL_TITLE_PREFIX} eligible Max plan accounts unavailable"
    body = "Every dispatchable task was skipped because: " + "; ".join(reasons) + "."
    if rate_limited:
        body += (
            f" Earliest recovery: {recovery_iso}."
            if recovery_iso
            else " Waiting for cooldown."
        )
    return title, body

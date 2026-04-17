"""Rollup transient-state helpers.

Extracted from app.rollup_management to keep that module under its
line-count guardrail. The transient-keys set and the scrubber that wipes
them on terminal transitions are pure dict helpers with no other deps.
"""


# Ephemeral counters/flags written to parent.context during the rollup's merge
# phase. They exist to ratchet retry budgets and de-duplicate alerts *within a
# single EVALUATING → terminal cycle*, not across cycles. Any terminal
# transition (COMPLETED after merge, COMPLETED empty-diff, FAILED, etc.) must
# scrub them so a future re-entry (re-used parent, admin requeue) starts from
# zero instead of inheriting stale budget/latch state.
_ROLLUP_TRANSIENT_CTX_KEYS = frozenset({
    "_merge_deferred_count",
    "_merge_deferred_state",
    "_merge_total_deferrals",
    "_merge_first_deferred_at",
    "_merge_total_alerted",
    "_merge_time_alerted",
    "_merge_alerted_states",
    "_refetch_failure_count",
    "_behind_update_retries",
    "_rollup_retries",
})


def _clear_rollup_transient_state(ctx: dict | None) -> dict:
    """Return a new context dict with transient deferral/retry state removed.

    Call at every terminal success/failure path of the rollup state machine so
    the counters don't leak into a future cycle. See _ROLLUP_TRANSIENT_CTX_KEYS
    for the full list.
    """
    if not ctx:
        return {}
    return {k: v for k, v in ctx.items() if k not in _ROLLUP_TRANSIENT_CTX_KEYS}

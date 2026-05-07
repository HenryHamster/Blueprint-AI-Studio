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
_ROLLUP_TRANSIENT_CTX_KEYS = frozenset(
    {
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
    }
)


def _clear_rollup_transient_state(ctx: dict | None) -> dict:
    """Return a new context dict with transient deferral/retry state removed.

    Call at every terminal success/failure path of the rollup state machine so
    the counters don't leak into a future cycle. See _ROLLUP_TRANSIENT_CTX_KEYS
    for the full list.
    """
    if not ctx:
        return {}
    return {k: v for k, v in ctx.items() if k not in _ROLLUP_TRANSIENT_CTX_KEYS}


def _is_rollup_parent_awaiting_merge(ctx: dict | None) -> bool:
    """True if this task is a rollup parent whose rollup PR has not yet merged.

    Rollup parents park in EVALUATING with ``_is_rollup_review=True`` and
    ``rollup_pr_number`` set while the review/CI/fix pipeline lands the PR.
    Intermediate pipeline events (conflict-fix spawn, ci-fix spawn, existing
    fix reuse) must NOT mark the parent COMPLETED — that terminates the
    parent before the PR is actually merged, leaving a "ghost success" and
    preventing ``ci_completion`` from promoting it via ``pr_merged=True``.

    Child review/fix tasks spawned for the rollup PR carry ``rollup_pr_number``
    AND ``review_loop.pr_number`` pointing at the same PR — those are NOT
    rollup parents and must not be parked. Misidentifying them keeps the child
    in EVALUATING after CI-fix takeover (incident 2026-05-07, task 12b4eb3b).
    """
    if not ctx:
        return False
    if ctx.get("_is_rollup_review") is True:
        return True
    review_loop_pr = (ctx.get("review_loop") or {}).get("pr_number")
    if review_loop_pr and review_loop_pr == ctx.get("rollup_pr_number"):
        return False
    return bool(ctx.get("rollup_pr_number")) and not ctx.get("pr_merged")

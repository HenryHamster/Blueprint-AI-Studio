"""Rollup transient-state helpers.

Extracted from app.rollup_management to keep that module under its
line-count guardrail. The transient-keys set and the scrubber that wipes
them on terminal transitions are pure dict helpers with no other deps.
"""

from datetime import datetime

# Rollup merge queue (merge train): when a sibling rollup merges into the
# shared base, the post-merge handler proactively update-branches every other
# queued rollup PR and stamps this ISO-timestamp marker on each updated
# parent's context. Conflict-fix spawners consult the marker (via
# ``_merge_queue_update_in_flight``) and defer a dirty-state fix spawn while
# the update is fresh — GitHub's mergeable computation lags the update-branch
# merge commit, so a "dirty" read inside this window is likely stale and a fix
# VM would double-fire against the queue's own in-flight update.
_MERGE_QUEUE_UPDATE_AT_KEY = "_merge_queue_update_at"

# Freshness window (seconds) for the merge-queue update marker. Within this
# window a mergeable=False+dirty observation defers the conflict-fix spawn to
# the next sweep instead of spawning; past it, the normal agent conflict-fix
# path proceeds unchanged so a genuine conflict is never wedged.
_MERGE_QUEUE_UPDATE_FRESH_SECONDS = 300

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
        # Merge-queue update marker (see _MERGE_QUEUE_UPDATE_AT_KEY above) —
        # only meaningful while the rollup PR is in flight; scrub on terminal.
        _MERGE_QUEUE_UPDATE_AT_KEY,
        # Duplicate-create guard: a per-cycle stake (epoch seconds) that a peer
        # invocation is mid-create of the rollup PR. It must never outlive the
        # cycle — a stale claim would block the next rollup attempt until its
        # TTL expires. Scrub it on every terminal transition. See
        # rollup_management._ROLLUP_PR_CREATE_CLAIM_TTL_S.
        "rollup_pr_creating_at",
        # Escalation latch: set when the empty/unknown escalation merge fires so
        # it only triggers once per alerting cycle. Must be scrubbed on terminal
        # so a future deferral episode can re-escalate if needed.
        "_merge_escalation_attempted",
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


def _merge_queue_update_in_flight(ctx: dict | None, now: datetime | None = None) -> bool:
    """True if a merge-queue update-branch on this parent's rollup PR is fresh.

    Reads the ``_merge_queue_update_at`` ISO timestamp stamped by the rollup
    merge train (rollup_management._nudge_sibling_rollup_prs) and returns True
    while it is younger than ``_MERGE_QUEUE_UPDATE_FRESH_SECONDS``. Conflict-fix
    spawners call this before spawning on mergeable=False+dirty so the queue's
    proactive update and the agent fix path never double-fire on the same PR.

    A malformed or missing marker reads as "not in flight" — the normal
    conflict-fix path proceeds, never wedging on bad data.
    """
    raw = (ctx or {}).get(_MERGE_QUEUE_UPDATE_AT_KEY)
    if not raw or not isinstance(raw, str):
        return False
    try:
        stamped = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return False
    if stamped.tzinfo is not None:
        # Stored values are naive UTC (app convention); tolerate an aware
        # writer by normalizing instead of miscomparing.
        from datetime import timezone

        stamped = stamped.astimezone(timezone.utc).replace(tzinfo=None)
    if now is None:
        from app.time_utils import _utcnow

        now = _utcnow()
    age_seconds = (now - stamped).total_seconds()
    return 0 <= age_seconds < _MERGE_QUEUE_UPDATE_FRESH_SECONDS


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

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

# Alembic auto-reconcile in-flight stake (MAJOR 4). Written under the rollup
# parent's FOR UPDATE lock, in the same commit as the retry-counter stake, right
# before that lock is released for the multi-request GitHub push. A peer sweep
# that sees a FRESH one for the SAME PR defers instead of issuing a concurrent
# second push to the staging branch. TTL-bounded so a sweep that dies mid-push
# cannot wedge the reconcile forever.
_ALEMBIC_AUTORECONCILE_INFLIGHT_AT_KEY = "_alembic_autoreconcile_inflight_at"
_ALEMBIC_AUTORECONCILE_PR_KEY = "_alembic_autoreconcile_pr"
# Must comfortably exceed one reconcile (PR-file plan + file read + multi-request
# push; measured at ~285s under rate-limit backoff).
_ALEMBIC_AUTORECONCILE_INFLIGHT_TTL_S = 900  # 15 min

# The rollup PR number whose security-scan report comment has already been
# posted (NIT 8). K7 hoists ``create_pr_comment`` out of the parent's lock
# window, which means the post can land and the subsequent ``_relock_parent``
# can still be LOST — and a per-invocation local flag then lets the NEXT sweep
# re-scan and post the same report AGAIN. Persisting the fact of the post,
# keyed by PR, makes it exactly-once ACROSS sweeps, not just within one.
_SCAN_REPORT_POSTED_PR_KEY = "_scan_report_posted_pr"

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
        # Alembic auto-reconcile in-flight stake — same shape as
        # ``rollup_pr_creating_at`` above: a per-cycle claim that a peer sweep is
        # mid-push. It must never outlive the cycle, or a stale claim would block
        # the next reconcile until its TTL expires.
        _ALEMBIC_AUTORECONCILE_INFLIGHT_AT_KEY,
        # Scan-report exactly-once marker. Scoped to one rollup PR; a re-entry on
        # a NEW PR must post its own report. (The marker is PR-keyed, so this is
        # belt-and-braces — but leaving it would strand a dead PR number in the
        # context of every re-used parent.)
        _SCAN_REPORT_POSTED_PR_KEY,
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


def _alembic_autoreconcile_in_flight(
    ctx: dict | None, pr_number: int, now: datetime | None = None
) -> bool:
    """True if a peer sweep is mid-push on this PR's Alembic auto-reconcile.

    MAJOR 4 (review round 2). K6 releases the rollup parent's FOR UPDATE lock
    across the auto-reconcile push so the connection is not left
    idle-in-transaction across multi-request GitHub I/O. The cost of that release
    is that ``skip_locked`` no longer defers a second sweep — and
    ``_check_and_trigger_rollup`` is driven from at least six call sites (github
    webhook, agent_status, task_lifecycle, reaper_evaluating_sweeps,
    reaper_post_orphan_sweep, recovery_evaluating) — so two sweeps could both
    enter the alembic block and both push to ``staging_branch``.

    The retry stake, written under the parent lock before the release, doubles as
    a durable in-flight marker: a peer that sees a FRESH one for the SAME PR
    defers to the next cycle instead of double-pushing. TTL-bounded so a sweep
    that dies mid-push cannot wedge the reconcile forever — and the retry cap
    still bounds total attempts either way.

    Deliberately NOT a Postgres advisory lock: ``_release_parent_lock`` returns
    the connection to the pool on commit (rollup_locking.py, H6), so a
    session-scoped advisory lock would ride a pooled connection into an unrelated
    caller and leak on crash. A malformed/missing marker, or one stamped for a
    DIFFERENT PR, reads as "not in flight" — never wedging on bad data.
    """
    ctx = ctx or {}
    if ctx.get(_ALEMBIC_AUTORECONCILE_PR_KEY) != pr_number:
        return False
    raw = ctx.get(_ALEMBIC_AUTORECONCILE_INFLIGHT_AT_KEY)
    if not raw or not isinstance(raw, str):
        return False
    try:
        stamped = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return False
    if stamped.tzinfo is not None:
        from datetime import timezone

        stamped = stamped.astimezone(timezone.utc).replace(tzinfo=None)
    if now is None:
        from app.time_utils import _utcnow

        now = _utcnow()
    age_seconds = (now - stamped).total_seconds()
    return 0 <= age_seconds < _ALEMBIC_AUTORECONCILE_INFLIGHT_TTL_S


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

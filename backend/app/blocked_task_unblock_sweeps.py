"""Blocked-task auto-unblock and escalation sweep helpers.

Extracted from reaper_sweeps.py (Step 12 and the related recovery sweep).
All public sweep entry points and markers are re-exported from
app.reaper_sweeps so existing import sites require no changes.
"""

from datetime import timedelta

# ── Step 12: BLOCKED-task auto-unblock / escalation sweep ──────────────────
#
# A task enters the first-class BLOCKED state when a durable precondition is
# missing — an orchestrator-discovered conflict that persisted past the defer
# threshold (orchestrator_filters._record_defer_and_maybe_escalate) or an
# agent/lifecycle "BLOCKER:" with an upstream dependency. BLOCKED tasks are
# undispatchable on the normal queue path (the candidate query is QUEUED-only)
# and are excluded from stale-task reaping. This sweep is the recovery path:
# it cheaply rechecks each BLOCKED task at/after its recheck_after and either
# moves it back to QUEUED (precondition cleared) or re-arms a later recheck.
# A blocker that is permanently unsatisfiable (its dependency cascade-cancelled)
# or that outlives the escalation deadline gets ONE deduplicated operator alert
# and stops being rechecked.

# Base recheck interval, doubled per consecutive still-blocked recheck up to the
# cap. Keeps the sweep cheap (no per-cycle re-judging) while staying responsive.
_BLOCKED_RECHECK_BASE = timedelta(minutes=5)
_BLOCKED_RECHECK_MAX = timedelta(hours=1)
# A blocker that never clears within this window is escalated to the operator
# as permanently blocked (one ERROR alert) and parked.
_BLOCKED_ESCALATE_AFTER = timedelta(hours=6)
# Parking horizon for terminally-escalated blockers. On escalation we push
# ``recheck_after`` far into the future (rather than leaving it NULL) so the row
# drops out of the due query (``recheck_after IS NULL OR <= now``) and is never
# re-SELECTed / FOR UPDATE-locked on the reaper hot path again. Leaving it NULL
# kept escalated rows permanently "due" (sorted first by nullsfirst), where they
# were re-locked every cycle and could crowd genuinely-due rechecks out of the
# batch-size window. Recovery has given up on these; an operator re-queues them.
_BLOCKED_ESCALATED_PARK = timedelta(days=3650)
# A parked (escalated) blocker that STILL carries a machine-resolvable need is
# re-examined at this LOW, bounded cadence instead of the multi-year park
# horizon, so that when the foundation it waited on finally lands — a sibling
# task, a merged PR, or shipped code now reachable on integration — the task
# resumes automatically rather than waiting for a manual operator requeue. The
# re-evaluation is cheap (one conservative exact-match pass) and never
# re-escalates or re-alerts, so it does not reintroduce per-cycle churn. Parked
# blockers with NO need keep the multi-year horizon and stay for the operator.
_BLOCKED_PARKED_REEVAL_INTERVAL = timedelta(hours=6)
# Bounded number of parked re-evaluations a still-unsatisfiable need is given
# before the dependent is terminated. A need that never becomes satisfiable —
# e.g. a capability no task ever advertises — would otherwise re-arm the parked
# re-evaluation at the low cadence forever. After this many parked re-evals with
# no resolution and no provably-dead foundation, the dependent reaches a bounded
# TERMINAL (CANCELLED) outcome instead of parking forever. The legitimate
# "foundation might still appear" case keeps its resume behavior within the
# bound — only an exhausted, never-satisfiable need terminates.
_BLOCKED_PARKED_REEVAL_CAP = 4
# A no-dependency agent/lifecycle blocker is often a TRANSIENT infra failure
# (orchestrator ./pr open 5xx, git push stale-lease) that clears on its own, but
# can also be a durable precondition (missing base branch/file). We can't tell
# them apart, so we optimistically requeue such a blocker up to this many times —
# a transient blip recovers on retry; a durable blocker re-blocks and, once the
# budget is spent, parks for the normal backoff + 6h operator escalation. The cap
# is deliberately small: re-dispatch costs a full agent VM run, so this trades a
# few wasted runs for auto-recovery instead of a multi-hour operator wedge.
_BLOCKED_OPTIMISTIC_RETRY_CAP = 3

# Durable context marker stamped on a blocked dependent that the resume system
# terminally CANCELS because it can provably never resume. The terminal
# transition clears all structured blocker metadata, so without this marker the
# give-up is invisible in aggregate (only a free-text status message + one
# deduplicated alert survive). The marker is queryable JSONB — no migration — and
# feeds the fleet-wide unresumable-cancellation health signal
# (``digest_metrics.compute_unresumable_cancellation_summary``). Observability
# only: stamping it changes nothing about WHEN or WHETHER a dependent is
# cancelled.
UNRESUMABLE_CANCEL_CONTEXT_KEY = "_unresumable_cancellation"
# Durable marker for the GOOD outcome, symmetric to the give-up marker above: a
# blocked dependent that successfully RESUMED (status → QUEUED) when its foundation
# landed. The transient latest_status_message and the in-memory ``requeued`` counter
# do not survive, so a regression where resumes stall looks identical to 'all
# healthy'. This queryable JSONB marker (no migration) feeds the dependency-health
# digest's positive resume signal (``digest_metrics.compute_blocked_resume_summary``).
# Observability only: stamping it changes nothing about WHEN or WHETHER a dependent
# resumes.
RESUME_MARKER_CONTEXT_KEY = "_blocked_resume"
# Give-up classes — the two reasons a blocked dependent can provably never resume.
GIVE_UP_FOUNDATION_DIED = (
    "foundation_died"  # needed foundation reached terminal FAILED/CANCELLED
)
GIVE_UP_NEVER_SATISFIABLE = (
    "never_satisfiable"  # need outlived its bounded parked re-evals
)

# Need kinds that name a FOUNDATION (a sibling task / finding-hash, or an explicit
# capability) rather than an external ref like a PR number. When one of these is
# cancelled as never_satisfiable, no task ever provided it — the dependent was
# stranded because the foundation was never created, which is first-class
# missing-foundation demand (see
# ``capability_demand.collect_missing_capability_demand``).
_FOUNDATION_NEED_KINDS = ("capability", "task")
# Additive sub-key stamped onto the UNRESUMABLE_CANCEL_CONTEXT_KEY marker when a
# never-satisfiable give-up cancelled a foundation-kind need: it flags an EXPLICIT
# missing-foundation retirement (stranded because nothing was ever built to satisfy
# the prerequisite), distinct from a give-up whose foundation existed and died.
# Purely additive — it never removes or renames an existing marker key, and the
# demand collector reads it to fold post-cancel demand back into the aggregate.
MISSING_FOUNDATION_MARKER_KEY = "missing_foundation"

# A recheck_after more than this far in the future is considered "hard-parked"
# by the old +10y sentinel path; any legitimate bounded recheck will be shorter.
_TRANSIENT_BLOCKER_PARK_SENTINEL = timedelta(days=365)

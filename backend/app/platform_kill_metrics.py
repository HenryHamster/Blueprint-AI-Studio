"""Platform-kill measurement: how many tasks per day the PLATFORM killed.

The operator's stated reliability metric is "platform-killed tasks per day,
driven to zero". A *platform kill* is a task that reached a terminal failure for
a reason attributable to the PLATFORM (infrastructure, harness/budget timeouts,
routing or provider failures, orchestrator wedges, gate deadlocks) rather than to
the work itself (the agent wrote bad code, tests genuinely failed, the scope was
already covered).

Until this module existed the number was unmeasurable: defect classes were found
anecdotally, as someone tripped over them, with no denominator and no trend.

--------------------------------------------------------------------------------
WHY THIS IS NOT JUST ``INFRA_ANALYTICS_CLASSES``
--------------------------------------------------------------------------------
``failure_classification.INFRA_ANALYTICS_CLASSES`` already buckets failures into
infra-vs-code, and reusing it would have been a one-liner. It is the wrong set
for this question in two specific ways:

1. It contains ``ADMIN_CANCEL`` and ``SCOPE_EMPTY``, which the codebase's own
   :func:`failure_classification.is_non_death_cancellation` policy declares are
   *not deaths at all* — an operator deliberately stopped the task, or the agent
   exited cleanly because the scope was already covered. Counting those as
   platform kills would inflate the metric with ~46% of terminal rows that nobody
   would call a defect. (Measured on the live estate: 698 ADMIN_CANCEL + 104
   SCOPE_EMPTY out of 1716 terminal rows.)
2. It says nothing about the several hundred terminal rows that carry NO
   persisted class at all. Those are the honest unknown, and they must be
   *visible* rather than silently absorbed into "not a kill".

So this module partitions terminal tasks four ways, not two.

--------------------------------------------------------------------------------
THE FOUR VERDICTS
--------------------------------------------------------------------------------
``platform_kill``  The platform killed runnable work. This is the number.
``work_outcome``   The work itself decided the outcome (bad code, failing tests,
                   empty scope, overflowing scope). Not a platform defect.
``not_a_death``    Terminal, but the work continues elsewhere or was deliberately
                   stopped: a requeue replacement, a review-pipeline supersede, a
                   co-deferred shelving, an operator cancel. Excluded from both.
``unclassified``   We do not know, and we say so. A terminal task with no
                   persisted class and no recognized terminal marker.

``unclassified`` is load-bearing. A classifier that quietly defaulted everything
it did not recognize to "not a platform kill" would report a beautiful zero while
measuring nothing — worse than no metric at all. Every aggregate this module
emits therefore carries ``unclassified_count`` and ``classified_fraction``
alongside the headline rate, so a reader can tell a real zero from an unmeasured
one.

--------------------------------------------------------------------------------
GROUND-TRUTH RULE
--------------------------------------------------------------------------------
This module reads ONLY persisted per-task ground truth, never re-derives from
logs:

  * ``ctx['failure_category']`` — stamped at terminal time by
    ``failure_classification.persist_failure_category``.
  * ``ctx['failure_class']``    — stamped by ``persist_authoritative_verdict``.
  * ``ctx['failure_detail']``   — the finer-grained reason, when a call site
    recorded one.
  * a NARROW allowlist of literal terminal status-message markers, for the
    orchestration paths that terminalize a task without stamping any class at
    all (see ``_TERMINAL_MESSAGE_MARKERS``).

It never calls ``classify_failure`` / ``classify_timeout_subtype``, which DERIVE
from log text. Re-deriving would make the metric drift whenever a status message
is rewritten, and would let a log-format change silently move the number. This
mirrors the ground-truth rule already documented on
``digest_metrics.compute_timeout_rate``.

--------------------------------------------------------------------------------
TOTALITY (the anti-vacuity property)
--------------------------------------------------------------------------------
:data:`PLATFORM_KILL_CLASSES`, :data:`WORK_OUTCOME_CLASSES` and
:data:`NOT_A_DEATH_CLASSES` are required to *cover*
``failure_classification.ALL_FAILURE_CLASSES`` — every canonical class in exactly
one map, none in two. ``test_platform_kill_metrics.py`` enforces this against the
live tuple, so adding a class to ``failure_classification`` without triaging it
here fails the suite instead of silently landing in the "not a kill" bucket. That
test asserts on a non-empty corpus (``ALL_FAILURE_CLASSES`` has 23 members)
precisely so it cannot pass vacuously.

Coverage is a superset, not an equality: ``PR_BRANCH_DELETED``,
``COMPUTE_PREFLIGHT_SMOKE_FAILED`` and ``COMPUTE_UNMEASURED_CRITERION`` are real
persisted values exported by ``failure_classification`` that are not members of
its ``ALL_FAILURE_CLASSES`` roster. They are triaged here anyway, because the
classifier reads whatever the terminalizing path actually wrote — not whatever
the roster says it should have written. Requiring exact equality would force
their removal and send live rows to ``unclassified`` for no reason.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List

from app.time_utils import _utcnow
from app.failure_classification import (
    ADMIN_CANCEL,
    AGENT_STALLED,
    AGENT_TIMEOUT,
    ALL_CHILDREN_SHELVED_DETAIL,
    APPROVED_CAP_PR_CLOSED,
    BLOCKED_PRECONDITION,
    BRANCH_GONE,
    CODE_ERROR,
    CO_DEFERRED_SHELVED,
    COMPUTE_PREFLIGHT_SMOKE_FAILED,
    COMPUTE_UNMEASURED_CRITERION,
    DEAD_AUTH,
    DEPENDENCY_TASK_FAILED_DETAIL,
    EVALUATION_GATE_FAILED_DETAIL,
    EXTERNAL_API,
    MALFORMED_TASK_PAYLOAD,
    MIGRATION_ERROR,
    NO_PROGRESS_TIMEOUT,
    PARENT_CI_BACKSTOP_CASCADE_DETAIL,
    PARENT_DEPENDENCY_FAILED_DETAIL,
    PARENT_EVALUATION_TIMEOUT_CASCADE_DETAIL,
    PARK_WAKE_STRANDED,
    PARK_WAKE_TIMEOUT,
    PR_BRANCH_DELETED,
    PR_EXECUTION_FENCE_DENIED_DETAIL,
    PREREQUISITE_NO_PROVIDER_DETAIL,
    PREREQUISITE_STRUCTURALLY_UNRESOLVABLE_DETAIL,
    PROVISION_FAILURE,
    RATE_LIMIT,
    REASSIGNED,
    REPO_UNINITIALIZED,
    ROLLUP_DELIVERABLE_DROPPED,
    SCOPE_EMPTY,
    SCOPE_OVERFLOW,
    UNPLACEABLE_EXECUTION_SITE,
    VIEWPORT_REGRESSION,
)

logger = logging.getLogger(__name__)


# ── verdicts ────────────────────────────────────────────────────────────────

PLATFORM_KILL = "platform_kill"
WORK_OUTCOME = "work_outcome"
NOT_A_DEATH = "not_a_death"
UNCLASSIFIED = "unclassified"

VERDICTS: tuple[str, ...] = (PLATFORM_KILL, WORK_OUTCOME, NOT_A_DEATH, UNCLASSIFIED)


# ── class → verdict maps, each entry carrying its WHY ───────────────────────
#
# "Surface reasons, not just states" is a standing requirement in this codebase:
# a bucket name alone does not let an operator act. Every entry therefore maps to
# a sentence explaining what the platform (or the work) actually did, and that
# sentence travels with each counted kill all the way to the CLI.

PLATFORM_KILL_CLASSES: Dict[str, str] = {
    PROVISION_FAILURE: (
        "environment provisioning failed before the agent could start work"
    ),
    AGENT_TIMEOUT: (
        "the attempt was ended by a clock, not by an assertion — a harness or "
        "budget timeout killed work that was not proven bad"
    ),
    NO_PROGRESS_TIMEOUT: (
        "no forward progress inside the budget window; ended by a clock, not by "
        "an assertion"
    ),
    AGENT_STALLED: (
        "the agent stopped heartbeating and the reaper terminated it"
    ),
    EXTERNAL_API: (
        "an upstream provider or API call failed the attempt"
    ),
    RATE_LIMIT: (
        "provider rate-limit or quota exhaustion ended the attempt"
    ),
    DEAD_AUTH: (
        "the provider account was disabled; no attempt could have succeeded"
    ),
    UNPLACEABLE_EXECUTION_SITE: (
        "the declared execution site had no dispatch path — the task was "
        "unroutable and never reached a runner"
    ),
    MALFORMED_TASK_PAYLOAD: (
        "the spawner wrote a payload the handler cannot run; the task was "
        "unrunnable by construction"
    ),
    BRANCH_GONE: (
        "the branch vanished before the clone — a repository-state race"
    ),
    REPO_UNINITIALIZED: (
        "the repository had no commits, so nothing could be cloned; the "
        "onboarding seed did not happen"
    ),
    PARK_WAKE_TIMEOUT: (
        "the parked task's wake source never fired within its deadline"
    ),
    PARK_WAKE_STRANDED: (
        "the task was parked with no live wake registration — unreachable by "
        "any wake, a permanent orchestrator wedge"
    ),
    APPROVED_CAP_PR_CLOSED: (
        "the fix-loop iteration cap was hit AFTER the change was approved — a "
        "gate deadlock, not a code defect"
    ),
    ROLLUP_DELIVERABLE_DROPPED: (
        "conflict resolution erased a landed child deliverable"
    ),
}

WORK_OUTCOME_CLASSES: Dict[str, str] = {
    CODE_ERROR: (
        "the candidate's code or tests failed on their merits"
    ),
    MIGRATION_ERROR: (
        "a migration the candidate authored failed to apply"
    ),
    VIEWPORT_REGRESSION: (
        "the candidate's UI change broke a viewport assertion"
    ),
    SCOPE_OVERFLOW: (
        "the task's scope exceeded what one attempt can carry — a planning "
        "outcome, not an infrastructure failure"
    ),
    SCOPE_EMPTY: (
        "deliberate clean no-op exit: the scope was already covered"
    ),
    PR_BRANCH_DELETED: (
        "the PR branch was already merged and deleted, so there was nothing "
        "left to review or fix"
    ),
    BLOCKED_PRECONDITION: (
        "a declared precondition was never satisfied — a dependency-ordering "
        "outcome. NOTE: the landing-gate defect class (a dep that provably "
        "merged but was never credited) also lands here; it is separated by "
        "failure_detail, not by class"
    ),
    COMPUTE_UNMEASURED_CRITERION: (
        "the compute experiment never measured its own declared criterion"
    ),
    COMPUTE_PREFLIGHT_SMOKE_FAILED: (
        "the compute experiment's own preflight smoke check failed"
    ),
}

NOT_A_DEATH_CLASSES: Dict[str, str] = {
    ADMIN_CANCEL: (
        "an operator or an orchestration path deliberately stopped the task; "
        "the codebase's own is_non_death_cancellation policy treats this as a "
        "stop, not a death"
    ),
    REASSIGNED: (
        "the task was reassigned to another agent — the work continues"
    ),
}


# ── failure_detail overrides ────────────────────────────────────────────────
#
# ``ctx['failure_detail']`` is strictly more specific than the class, so it is
# consulted FIRST. This is what separates a genuine operator cancel from the
# gate deadlocks that are *recorded* as ADMIN_CANCEL: on the live estate 375 of
# 698 ADMIN_CANCEL rows carry `superseded_by_review_pipeline` (work continues),
# while a handful carry approval/cap timeouts that are real platform kills.

_DETAIL_VERDICTS: Dict[str, tuple[str, str]] = {
    # ── gate deadlocks recorded as cancels ──
    "approval_timeout_preserved_for_handmerge": (
        PLATFORM_KILL,
        "the approval gate timed out while the change waited; the task was "
        "preserved for a hand-merge because the gate never resolved",
    ),
    "review_cap_preserved_for_handmerge": (
        PLATFORM_KILL,
        "the review loop hit its iteration cap without converging and was "
        "preserved for a hand-merge — a review-loop deadlock",
    ),
    "cap_exhausted_after_approve": (
        PLATFORM_KILL,
        "the fix-loop cap was exhausted after the change was already approved "
        "— a gate deadlock",
    ),
    "review_park_abandoned": (
        PLATFORM_KILL,
        "a parked review was abandoned without its wake ever firing",
    ),
    # ── work outcomes ──
    "review_cap_exhausted": (
        WORK_OUTCOME,
        "the review loop hit its iteration cap without the change converging",
    ),
    "fix_loop_diverged": (
        WORK_OUTCOME,
        "successive fix attempts diverged instead of converging",
    ),
    "empty_effective_diff_at_merge": (
        WORK_OUTCOME,
        "the change carried no effective diff by the time it reached merge",
    ),
    # ── deliberate stops ──
    "pr_closed_non_rejection": (
        NOT_A_DEATH,
        "the PR was closed for a reason other than rejection",
    ),
    "cancelled_by_churn_halt": (
        NOT_A_DEATH,
        "stopped by the churn guardrail, not by a failure of the work",
    ),
    # A PR-less approval hold that nobody answered. The row already RECORDED
    # this reason (recovery_evaluating_prless_hold stamps it as failure_detail);
    # it was simply never registered here, so a fully-explained terminal row was
    # reported as "we do not know" — a false unknown, which is worse than an
    # honest one because it hides in the same bucket.
    #
    # NOT_A_DEATH rather than PLATFORM_KILL is taken from the codebase's own
    # existing ruling on this exact key: ``direction_outcomes`` excludes it from
    # direction health because it is *operator latency*, not a failure of the
    # work or of the platform. Note this deliberately differs from the sibling
    # ``approval_timeout_preserved_for_handmerge`` above, which is a kill because
    # there the GATE itself never resolved rather than a human never answering.
    "approval_timeout_hold_expired": (
        NOT_A_DEATH,
        "a PR-less approval hold expired without an operator answering it; the "
        "change was never rejected and the work was never run",
    ),
    # ── terminal paths that used to record no reason at all ──
    #
    # Every key below is imported from ``failure_classification`` rather than
    # retyped, so a writer and this registry cannot drift apart, and
    # ``ALL_TERMINAL_REASON_DETAILS`` gives the suite something total to assert
    # over. See the module note on ``_TERMINAL_MESSAGE_MARKERS``: three of these
    # replace a literal-substring match on a log sentence with a durable field,
    # and their verdicts reproduce the marker's verdict EXACTLY so the recorded
    # evidence improves without the measured number moving.
    EVALUATION_GATE_FAILED_DETAIL: (
        WORK_OUTCOME,
        "the deliverable did not clear its own forward evaluation gate; which "
        "gate and why is recorded under context['evaluation_gate']",
    ),
    PR_EXECUTION_FENCE_DENIED_DETAIL: (
        NOT_A_DEATH,
        "the PR execution fence refused this row's claim — another task "
        "canonically owns the PR, or the assigned target no longer exists; the "
        "winning claimant is named in context['pr_execution_fence']['reason']",
    ),
    DEPENDENCY_TASK_FAILED_DETAIL: (
        WORK_OUTCOME,
        "the prerequisite this task declared failed, so the dependent could "
        "never run — a dependency-ordering outcome. The prerequisite's own row "
        "carries whatever killed IT; attributing it here too would multiply one "
        "defect into a whole cascade",
    ),
    PARENT_DEPENDENCY_FAILED_DETAIL: (
        WORK_OUTCOME,
        "swept up in a dependency cascade: this row's parent was cancelled "
        "because ITS prerequisite failed",
    ),
    PARENT_EVALUATION_TIMEOUT_CASCADE_DETAIL: (
        PLATFORM_KILL,
        "cancelled because an EVALUATING parent was reaped after waiting out "
        "its evaluation window — runnable work ended by the parent's clock",
    ),
    PARENT_CI_BACKSTOP_CASCADE_DETAIL: (
        PLATFORM_KILL,
        "cancelled because the parent's CI-wait backstop fired — CI never "
        "reported, so a gate deadlock took the children down with it",
    ),
    ALL_CHILDREN_SHELVED_DETAIL: (
        NOT_A_DEATH,
        "every subtask was deliberately shelved, superseded or administratively "
        "cancelled, so the parent had nothing left to roll up",
    ),
    PREREQUISITE_STRUCTURALLY_UNRESOLVABLE_DETAIL: (
        WORK_OUTCOME,
        "the declared prerequisite was structurally unresolvable, so the work "
        "was escalated to the parent instead of blocked",
    ),
    PREREQUISITE_NO_PROVIDER_DETAIL: (
        WORK_OUTCOME,
        "the declared prerequisite had no spawned, deferred or in-flight "
        "provider this cycle, so the work was escalated to the parent",
    ),
}


# ── terminal status-message markers ─────────────────────────────────────────
#
# Several orchestration paths terminalize a task WITHOUT stamping any class:
# on the live estate 489 of 1716 terminal rows carry no `failure_category`. Some
# of those are recognizable from a literal, stable marker the orchestrator writes
# into `latest_status_message`.
#
# This allowlist is deliberately NARROW. Each entry is a literal substring, not a
# regex or a keyword heuristic, and covers an orchestration path that is known to
# terminalize without classifying. Anything that does not match lands in
# ``unclassified`` — which is the correct answer, not a failure of the classifier.
# Widening this list to "reduce unclassified" would be exactly the vacuous-guard
# mistake this module exists to avoid.
#
# THIS LIST IS NOW A BACK-COMPAT TAIL, NOT THE PRIMARY EVIDENCE. The three
# orchestration paths behind the first, second and fourth entries now stamp a
# durable ``failure_detail`` (see the terminal-reason detail keys in
# ``failure_classification``), which is read at precedence 1 and produces the
# SAME verdict each marker produces here. The markers are retained because rows
# terminalized before that change carry only the prose, and deleting the marker
# would silently reclassify live history. Do not add new entries: a new
# terminalizing path must stamp a field, not hope its log sentence survives.

_TERMINAL_MESSAGE_MARKERS: tuple[tuple[str, str, str], ...] = (
    (
        "parent evaluation timeout after",
        PLATFORM_KILL,
        "an EVALUATING parent was reaped after waiting out its evaluation "
        "window — the children never reported and the parent was stranded",
    ),
    (
        "CI wait backstop timeout after",
        PLATFORM_KILL,
        "the change was approved but CI never reported, so the CI-wait backstop "
        "reaped the task — a gate deadlock, not a code defect",
    ),
    (
        "Co-deferred with same-cycle-deferred prerequisite",
        NOT_A_DEATH,
        "co-deferred alongside its same-cycle prerequisite; a recoverable "
        "shelving, the pair re-surfaces together",
    ),
    (
        "deliberately shelved or superseded",
        NOT_A_DEATH,
        "every subtask was deliberately shelved or superseded — not a failure",
    ),
    (
        "Parent requeued",
        NOT_A_DEATH,
        "the parent was requeued and this row was replaced by the retry",
    ),
    (
        "Auto-resurrected after infrastructure recovery",
        NOT_A_DEATH,
        "the task was auto-resurrected by the infra-recovery sweep; the work "
        "continued under the resurrection",
    ),
)


# Context keys that, on their own, mean the work continues elsewhere.
_REPLACEMENT_CONTEXT_KEYS: tuple[str, ...] = (
    "_replaced_by_task_id",
    CO_DEFERRED_SHELVED,
)

# ``failure_detail`` values that describe CONTINUATION rather than attribution:
# the row stopped because the work was re-cut somewhere else. These behave
# exactly like ``_replaced_by_task_id`` — they set ``work_continued`` and decide
# the verdict ONLY when no failure class is present to decide it.
#
# Keeping these out of ``_DETAIL_VERDICTS`` is not a stylistic choice. On the
# live estate 87 rows carry BOTH ``failure_category == AGENT_TIMEOUT`` and
# ``failure_detail == superseded_by_review_pipeline``: a review agent lost its
# heartbeat and the pipeline spawned a replacement. Treating the supersede as the
# verdict would have erased 87 of 133 AGENT_TIMEOUT kills — the platform's own
# remediation hiding the platform's own defect, which is the exact failure mode
# the ``_replaced_by_task_id`` ordering above already guards against.
_CONTINUATION_DETAILS: Dict[str, str] = {
    "superseded_by_review_pipeline": (
        "superseded by a newer review for the same PR; the work continues "
        "under the replacement"
    ),
    "superseded_by_moved_pr_target": (
        "the PR target moved and the task was re-cut against the new target"
    ),
}


@dataclass(frozen=True)
class KillVerdict:
    """One terminal task's classification, carrying WHY.

    ``reason`` is the operator-facing sentence. ``evidence_field`` /
    ``evidence_value`` name the exact persisted datum the verdict was read from,
    so a disputed classification can be traced to a row and a key rather than to
    a guess.
    """

    verdict: str
    kill_class: str | None
    reason: str
    evidence_field: str
    evidence_value: str | None = None
    work_continued: bool = False

    @property
    def is_platform_kill(self) -> bool:
        return self.verdict == PLATFORM_KILL

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "kill_class": self.kill_class,
            "reason": self.reason,
            "evidence_field": self.evidence_field,
            "evidence_value": self.evidence_value,
            "work_continued": self.work_continued,
        }


def _context(task: Any) -> Dict[str, Any]:
    ctx = getattr(task, "context", None) or {}
    return ctx if isinstance(ctx, dict) else {}


def _verdict_for_class(cls: str) -> tuple[str, str | None]:
    """Look ``cls`` up in the three class maps. None when it is in no map."""
    if cls in PLATFORM_KILL_CLASSES:
        return PLATFORM_KILL, PLATFORM_KILL_CLASSES[cls]
    if cls in WORK_OUTCOME_CLASSES:
        return WORK_OUTCOME, WORK_OUTCOME_CLASSES[cls]
    if cls in NOT_A_DEATH_CLASSES:
        return NOT_A_DEATH, NOT_A_DEATH_CLASSES[cls]
    return None


def classify_terminal_task(task: Any) -> KillVerdict:
    """Classify one terminal task as platform kill / work outcome / not-a-death.

    Precedence, most specific evidence first:

      1. ``ctx['failure_detail']`` — strictly finer-grained than the class, and
         the only thing that separates a gate deadlock recorded as ADMIN_CANCEL
         from a genuine operator cancel.
      2. ``ctx['failure_category']``, then ``ctx['failure_class']`` — the
         persisted canonical class.
      3. ``_replaced_by_task_id`` / ``_co_deferred_shelved`` context markers, for
         rows that carry NO class — a bookkeeping replacement.
      4. A narrow allowlist of literal terminal status-message markers, for
         orchestration paths that terminalize without stamping a class.
      5. ``unclassified`` — with a reason naming exactly what was missing.

    WHY THE REPLACEMENT MARKERS RANK BELOW THE CLASS, NOT ABOVE IT
    --------------------------------------------------------------
    An earlier revision of this function checked ``_replaced_by_task_id`` first,
    on the reasoning that a replaced row is "not a death". Replaying it over
    1716 real terminal rows showed what that costs: AGENT_TIMEOUT kills fell from
    134 to 34, because most timed-out attempts ARE requeued. That is precisely
    backwards. A retry is *recovery*, not absolution — the platform still killed
    a runnable attempt, and AGENT_TIMEOUT stranding is one of the named defect
    classes the operator wants driven to zero. Letting the requeue erase it would
    hide the metric's single largest true class behind its own remediation.

    The replacement marker is therefore recorded as ``work_continued`` on the
    verdict instead of overriding it, so an operator can see kills-that-were-
    retried as a sub-count without them vanishing from the numerator.

    Never re-derives from log text (see the module ground-truth rule).
    """
    ctx = _context(task)
    detail = ctx.get("failure_detail")
    work_continued = any(bool(ctx.get(key)) for key in _REPLACEMENT_CONTEXT_KEYS) or (
        isinstance(detail, str) and detail in _CONTINUATION_DETAILS
    )

    # 1. failure_detail — the most specific persisted reason.
    if isinstance(detail, str) and detail in _DETAIL_VERDICTS:
        verdict, reason = _DETAIL_VERDICTS[detail]
        return KillVerdict(
            verdict=verdict,
            kill_class=detail if verdict == PLATFORM_KILL else None,
            reason=reason,
            evidence_field="context.failure_detail",
            evidence_value=detail,
            work_continued=work_continued,
        )

    # 2. Persisted canonical class.
    for key in ("failure_category", "failure_class"):
        raw = ctx.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        resolved = _verdict_for_class(raw)
        if resolved is None:
            # A persisted class this module has never been taught. Saying so is
            # the point — silently treating it as "not a kill" is the failure
            # mode this bucket exists to prevent.
            return KillVerdict(
                verdict=UNCLASSIFIED,
                kill_class=None,
                reason=(
                    f"persisted failure class {raw!r} is not triaged in "
                    "platform_kill_metrics; it belongs in exactly one of "
                    "PLATFORM_KILL_CLASSES / WORK_OUTCOME_CLASSES / "
                    "NOT_A_DEATH_CLASSES"
                ),
                evidence_field=f"context.{key}",
                evidence_value=raw,
                work_continued=work_continued,
            )
        verdict, reason = resolved
        return KillVerdict(
            verdict=verdict,
            kill_class=raw if verdict == PLATFORM_KILL else None,
            reason=reason,
            evidence_field=f"context.{key}",
            evidence_value=raw,
            work_continued=work_continued,
        )

    # 3. Classless rows that were provably replaced, superseded or shelved.
    if isinstance(detail, str) and detail in _CONTINUATION_DETAILS:
        return KillVerdict(
            verdict=NOT_A_DEATH,
            kill_class=None,
            reason=_CONTINUATION_DETAILS[detail],
            evidence_field="context.failure_detail",
            evidence_value=detail,
            work_continued=True,
        )
    for key in _REPLACEMENT_CONTEXT_KEYS:
        if ctx.get(key):
            return KillVerdict(
                verdict=NOT_A_DEATH,
                kill_class=None,
                reason=(
                    "the task row carries no failure class and was replaced or "
                    "deliberately shelved; the work continues elsewhere"
                ),
                evidence_field=f"context.{key}",
                evidence_value=str(ctx.get(key))[:120],
                work_continued=True,
            )

    # 4. Narrow literal terminal-message markers.
    message = getattr(task, "latest_status_message", None) or ""
    if isinstance(message, str) and message:
        for marker, verdict, reason in _TERMINAL_MESSAGE_MARKERS:
            if marker in message:
                return KillVerdict(
                    verdict=verdict,
                    kill_class=marker if verdict == PLATFORM_KILL else None,
                    reason=reason,
                    evidence_field="latest_status_message",
                    evidence_value=marker,
                    work_continued=work_continued,
                )

    # 5. Honest unknown.
    return KillVerdict(
        verdict=UNCLASSIFIED,
        kill_class=None,
        reason=(
            "terminal task carries no persisted failure_category or "
            "failure_class, and its status message matched no known terminal "
            "marker — the terminalizing path did not record why"
        ),
        evidence_field="none",
        evidence_value=None,
        work_continued=work_continued,
    )


# ── aggregation ─────────────────────────────────────────────────────────────


def _completed_date(task: Any) -> date | None:
    """UTC calendar date a task went terminal, from the durable column."""
    ts = getattr(task, "completed_at", None)
    if ts is None:
        ts = getattr(task, "updated_at", None)
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)
    return ts.date()


@dataclass
class _ClassRollup:
    count: int = 0
    recovered: int = 0
    reason: str = ""
    sample_task_ids: List[str] = field(default_factory=list)


def compute_platform_kill_metrics(
    tasks: Iterable[Any],
    *,
    window_days: int,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Platform kills per day over ``window_days``, broken down by class.

    ``tasks`` must already be the terminal (FAILED / CANCELLED) rows for the
    project and window; this function does no filtering by status, only by date,
    so the caller owns the query and this stays a pure, directly-testable
    function.

    The returned payload deliberately leads with the honest denominator:

      ``terminal_task_count``     every terminal row considered
      ``platform_kill_count``     the numerator
      ``platform_kills_per_day``  the headline metric
      ``unclassified_count``      rows we could not classify
      ``classified_fraction``     1.0 - unclassified/terminal
      ``daily``                   zero-filled totals plus each day's class mix

    A reader who sees ``platform_kills_per_day: 0.0`` next to
    ``classified_fraction: 0.31`` knows the zero is meaningless. That pairing is
    the whole point — a rate without its unclassified share is not a result.
    """
    if window_days <= 0:
        raise ValueError("window_days must be positive")

    now = now or _utcnow()
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    window_start_date = (now - timedelta(days=window_days)).date()

    verdict_counts: Counter[str] = Counter()
    by_class: Dict[str, _ClassRollup] = {}
    per_day_kills: Dict[str, int] = defaultdict(int)
    per_day_kills_by_class: Dict[str, Counter[str]] = defaultdict(Counter)
    per_day_terminal: Dict[str, int] = defaultdict(int)
    unclassified_reasons: Counter[str] = Counter()
    unclassified_samples: List[Dict[str, Any]] = []
    terminal_count = 0
    kills_recovered = 0

    for task in tasks:
        day = _completed_date(task)
        if day is None or day < window_start_date:
            continue
        terminal_count += 1
        day_key = day.isoformat()
        per_day_terminal[day_key] += 1

        verdict = classify_terminal_task(task)
        verdict_counts[verdict.verdict] += 1

        if verdict.verdict == PLATFORM_KILL:
            per_day_kills[day_key] += 1
            if verdict.work_continued:
                kills_recovered += 1
            cls = verdict.kill_class or "unknown"
            per_day_kills_by_class[day_key][cls] += 1
            rollup = by_class.setdefault(cls, _ClassRollup(reason=verdict.reason))
            rollup.count += 1
            if verdict.work_continued:
                rollup.recovered += 1
            if len(rollup.sample_task_ids) < 5:
                rollup.sample_task_ids.append(str(getattr(task, "id", "")))
        elif verdict.verdict == UNCLASSIFIED:
            unclassified_reasons[verdict.reason] += 1
            if len(unclassified_samples) < 10:
                unclassified_samples.append(
                    {
                        "task_id": str(getattr(task, "id", "")),
                        "reason": verdict.reason,
                        "evidence_field": verdict.evidence_field,
                        "evidence_value": verdict.evidence_value,
                    }
                )

    kill_count = verdict_counts[PLATFORM_KILL]
    unclassified_count = verdict_counts[UNCLASSIFIED]

    # Emit a zero-filled row for every day in the window so a downward trend is
    # visible as a trend and not as missing data.
    daily: List[Dict[str, Any]] = []
    for offset in range(window_days - 1, -1, -1):
        day_key = (now - timedelta(days=offset)).date().isoformat()
        daily.append(
            {
                "date": day_key,
                "platform_kills": per_day_kills.get(day_key, 0),
                "platform_kills_by_class": dict(
                    sorted(per_day_kills_by_class.get(day_key, {}).items())
                ),
                "terminal_tasks": per_day_terminal.get(day_key, 0),
            }
        )

    breakdown = [
        {
            "kill_class": cls,
            "count": rollup.count,
            "recovered_count": rollup.recovered,
            "reason": rollup.reason,
            "sample_task_ids": rollup.sample_task_ids,
        }
        for cls, rollup in sorted(
            by_class.items(), key=lambda kv: (-kv[1].count, kv[0])
        )
    ]

    return {
        "window_days": window_days,
        "window_start": window_start_date.isoformat(),
        "terminal_task_count": terminal_count,
        "platform_kill_count": kill_count,
        "platform_kills_per_day": round(kill_count / window_days, 3),
        "platform_kills_recovered": kills_recovered,
        "work_outcome_count": verdict_counts[WORK_OUTCOME],
        "not_a_death_count": verdict_counts[NOT_A_DEATH],
        "unclassified_count": unclassified_count,
        "classified_fraction": (
            round(1.0 - (unclassified_count / terminal_count), 4)
            if terminal_count
            else None
        ),
        "by_class": breakdown,
        "daily": daily,
        "unclassified_reasons": [
            {"reason": reason, "count": count}
            for reason, count in unclassified_reasons.most_common(10)
        ],
        "unclassified_samples": unclassified_samples,
    }

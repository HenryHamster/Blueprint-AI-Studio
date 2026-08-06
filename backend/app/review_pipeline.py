"""Review pipeline: spawn and manage automated review/fix task cycles.

Extracted from task_lifecycle.py to reduce its size. Contains the per-PR
review-fix loop, iteration cap, and stuck-cycle detection logic.

All imports are module-level — no circular dependency risk with app.main
or app.task_lifecycle.
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from sqlalchemy import func
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.context_bounds import bound_review_history, warn_if_context_oversized
from app.ci_merge_helpers import revoke_review_authority_for_active_fix
from app.database import resolve_dialect_name
from app.github import post_pipeline_merge_summary_comment as post_merge_summary_comment  # noqa: F401
from app.models import Task, TaskHistory, TaskStatus
from app.payload_schemas import validate_internal_payload
from app.review_fix_lane_pilot import PILOT_PROPAGATE_KEYS
from app.shared_state import (
    runtime_config as _runtime_config,
    is_warm_continuity_suppressed,
)
from app.broadcast import broadcast_to_project
from app.time_utils import _utcnow
from app import vm_lifecycle
from app.spawn_calibration import (
    _compute_scope_budget_utilization,
    _compute_scope_overflow_warning,
)

# Authoritative issue ledger. Side-effect-free; re-exported here so callers
# importing from app.review_pipeline keep working.
from app.issue_ledger import (  # noqa: F401
    build_issue_ledger,
    ensure_issue_ids,
)

# Pure, side-effect-free review-loop heuristics live in review_heuristics; they
# are re-exported here so existing callers/tests that import them from
# app.review_pipeline keep working unchanged.
from app.review_heuristics import (  # noqa: F401
    EARLY_NONCONVERGENCE_ESCALATE_AT,
    ISSUE_CHURN_WINDOW,
    LANE_CAP_CI_DEFAULT,
    LANE_CAP_CONFLICT_DEFAULT,
    LANE_CAP_NON_REVIEW_DEFAULT,
    LANE_CI,
    LANE_CONFLICT,
    LANE_REVIEW_CONVERGENCE,
    PERSISTENT_CONFLICT_ESCALATION_THRESHOLD,
    STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_DEFAULT,
    STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_MEDIUM,
    STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_REFACTOR,
    STUCK_CYCLE_TEXT_THRESHOLD_DEFAULT,
    STUCK_CYCLE_TEXT_THRESHOLD_MEDIUM,
    STUCK_CYCLE_TEXT_THRESHOLD_REFACTOR,
    STUCK_CYCLE_THRESHOLD_DEFAULT,
    STUCK_CYCLE_THRESHOLD_MEDIUM,
    STUCK_CYCLE_THRESHOLD_REFACTOR,
    _critical_major_count,
    _detect_issue_churn,
    _diff_lines_changed,
    _feedback_jaccard,
    _feedback_token_set,
    _FEEDBACK_TOKEN_RE,
    _format_repeating_issues_section,
    _is_test_file,
    _reconcile_narrow_focus_with_fix_all,
    _repeating_issue_descriptions,
    _stuck_thresholds,
    classify_iteration_lane,
    compute_drift_signal,
    detect_early_nonconvergence,
    detect_fix_all_issue_class,
    detect_hedges_in_diff,
    detect_unjustified_hedge,
    fix_all_occurrences_directive,
)

# Review-skip decisions live in review_policy (the single documented home for
# all review-skip policy). Re-exported here so existing callers/tests that
# import them from app.review_pipeline keep working unchanged.
from app.review_policy import (  # noqa: F401
    AUTO_APPROVE_MIN_ACCEPTANCE_RATE,
    ReviewSkipDecision,
    evaluate_auto_approve_small_main,
    should_auto_approve_diff,
    should_auto_approve_small_diff,
    should_skip_initial_review,
)

# Review<->fix outer-loop convergence machinery (scope-pressure / narrow-focus
# thresholds + prefixes, pipeline-task staleness check, terminal cap-exhaustion
# handler) lives in review_loop_convergence; re-exported here so existing
# callers/tests that import these from app.review_pipeline keep working, and so
# the staying spawner code can reference them by bare name.
from app.review_loop_convergence import (
    MID_BUDGET_NARROW_FOCUS_THRESHOLD,
    PIPELINE_TASK_STALE_MINUTES,
    SCOPE_PRESSURE_HARD_CAP_PERCENTAGE,
    SCOPE_PRESSURE_HARD_CAP_REMAINING,
    SCOPE_PRESSURE_HARD_CAP_REMAINING_LARGE,
    SCOPE_PRESSURE_WARN_THRESHOLD,
    _CHURN_NARROW_FOCUS_PREFIX,
    _EARLY_REPEATING_ISSUES_PREFIX,
    _handle_cap_exhaustion,
    _handle_persistent_conflict_escalation,
    _MID_BUDGET_NARROW_FOCUS_PREFIX,
    _SCOPE_PRESSURE_HARD_CAP_PREFIX,
    _is_pipeline_task_stale,
    _task_is_actively_ci_waiting,
    _set_review_loop_terminal_outcome,
    review_loop_parked_for_operator,  # noqa: F401 — re-exported for lifecycle/rollup callers
)

logger = logging.getLogger(__name__)


_REVIEW_SUPERSEDE_CHURN_COUNT_KEY = "_review_supersede_churn_count"
_REVIEW_SUPERSEDE_CHURN_LAST_KEY = "_review_supersede_churn_last"
_REVIEW_SUPERSEDE_CHURN_LIMIT = max(
    1, int(os.getenv("REVIEW_SUPERSEDE_CHURN_LIMIT", "3"))
)


# Per-entry feedback in review_loop.review_history is freeform reviewer text. On
# the verdict-parse fallback path it is the agent's ENTIRE log (task_lifecycle
# sets ``feedback = payload.logs`` when no structured verdict is parsed), so a
# SINGLE history entry can be multiple MB. task.context is GIN-indexed: every
# write of a fat row pays a TOAST rewrite plus a GIN reindex, producing the
# multi-second single-row PK updates that saturated Postgres and tripped Cloud
# Run liveness probes. A count cap on the number of entries is necessary but not
# sufficient (N entries each holding a full log is still a multi-MB row), so we
# also bound the SIZE of each entry's stored feedback. This is safe for every
# consumer: the freeform feedback is read only by the inter-iteration
# string-similarity comparison (``_feedback_jaccard``); the structured
# issues / changed_files / verdict / iteration fields every other consumer reads
# are untouched, and the feedback is independently truncated again where it is
# surfaced to GitHub (routers/agent.py). 4000 chars is generous relative to real
# reviewer feedback yet keeps even an uncapped run's history to a few KB.
# Precedent: task_evaluators.py caps a sibling feedback field at ``[:5000]``.
MAX_REVIEW_HISTORY_FEEDBACK_CHARS = max(
    500, int(os.getenv("MAX_REVIEW_HISTORY_FEEDBACK_CHARS", "4000"))
)


def _cap_review_history_feedback(
    history: list | None,
    max_chars: int = MAX_REVIEW_HISTORY_FEEDBACK_CHARS,
) -> list:
    """Return a copy of ``history`` with every entry's ``feedback`` truncated to
    ``max_chars`` characters.

    Pure / side-effect-free: the input list and its entries are never mutated.
    Entries already within bounds (or lacking a string ``feedback``) are passed
    through by reference; oversized ones are shallow-copied with a truncated
    ``feedback`` so all other keys (iteration / verdict / changed_files /
    issues) are preserved verbatim. Applied to BOTH the freshly built entry and
    any carried-forward prior entries so the persisted row can never be fat,
    even from history written before this cap was deployed.
    """
    if not history:
        return list(history or [])
    capped: list = []
    for entry in history:
        feedback = entry.get("feedback") if isinstance(entry, dict) else None
        if isinstance(feedback, str) and len(feedback) > max_chars:
            capped.append({**entry, "feedback": feedback[:max_chars]})
        else:
            capped.append(entry)
    return capped


# Goal framing for review-fix tasks: the cited issues are necessary
# changes, but the goal is shipping the PR cleanly. Without this framing,
# fix agents narrowly edit the cited line and silently drop adjacent
# guarantees the function used to enforce, which the next reviewer
# surfaces as a new MAJOR — a divergent review-fix loop.
REVIEW_FIX_GOAL_PREAMBLE = (
    "Your goal: make this PR mergeable. That means the cited issues below "
    "are addressed AND every function you touch still enforces every "
    "invariant it currently enforces. Before editing a function, read what "
    "it does end-to-end (callers, error paths, related branches) so you know "
    "what it guarantees; after editing, confirm each guarantee still holds. "
    "A regression in adjacent behavior costs more iterations than the "
    "original finding — if a narrow patch can't satisfy both the cited "
    "issue and the existing guarantees, a wider, well-justified change is "
    "the right call.\n\n"
)


# ── Verification-only REQUEST_CHANGES downgrade ───────────────────────────────
#
# A reviewer can emit REQUEST_CHANGES purely because it could not RUN/VERIFY in
# its sandbox (a verify tool was missing, a CI probe 401'd, the workspace was
# stale, an install failed) — NOT because it found a concrete code defect. The
# pipeline's merge gate is `ci_passed AND review_approved` where `ci_passed`
# comes from the orchestrator's own authoritative check-run polling. So a
# "I couldn't verify" REQUEST_CHANGES on a CI-green PR with no concrete defect
# is spurious and must not block forever.
#
# These two patterns classify a blocking issue. The downgrade is gated on
# BOTH: every blocking issue is verification-class (matches _VERIFICATION_*),
# AND no blocking issue cites a concrete code defect (matches _CONCRETE_*).
# Anything matching _CONCRETE_DEFECT_RE is ALWAYS respected, even if it also
# mentions a build/verify phrase — the conservative bias is to keep blocking.
_VERIFICATION_CLASS_RE = re.compile(
    r"could ?n'?t run|could not run|unable to run|cannot run|can'?t run|"
    r"failed to run|command not found|no such file|not found in (?:path|sandbox)|"
    r"\./check|check-runs?\b|"
    r"unverifiable|could ?n'?t verify|could not verify|cannot verify|"
    r"can'?t verify|unable to verify|not verifiable|no way to verify|"
    r"\b401\b|\b403\b|unauthorized|forbidden|"
    r"stale workspace|stale checkout|cannot resolve [\w.@/-]+|"
    r"could ?n'?t install|could not install|failed to install|install(?:ation)? failed|"
    r"npm ci|tsc\b|pytest\b|\bci\b|"
    r"build fail(?:ed|ure)?|build broke|build (?:is )?red|"
    r"did ?n'?t pass|did not pass|pass(?:ed|ing)? status|"
    r"verification gate|verify gate|sandbox",
    re.IGNORECASE,
)

# Concrete code-defect cues. If ANY blocking issue matches this, the verdict is
# respected (never downgraded) — the reviewer found a real problem, not just a
# tooling gap. Kept broad and conservative: when in doubt, this wins.
_CONCRETE_DEFECT_RE = re.compile(
    r"\b(?:bug|defect|incorrect|wrong|logic error|off[- ]by[- ]one|"
    r"race condition|deadlock|data ?race|"
    r"null (?:pointer|deref|reference|check)|none ?type|nullpointer|npe\b|"
    r"undefined behavior|uninitialized|use[- ]after[- ]free|memory leak|"
    r"sql injection|injection|xss|csrf|ssrf|path traversal|"
    r"vulnerab(?:le|ility)|insecure|security (?:hole|flaw|issue|risk)|"
    r"hard[- ]?coded (?:secret|credential|password|token|key)|"
    r"leak(?:s|ed|ing)? (?:secret|credential|token|key|pii)|"
    r"missing (?:test|validation|null check|bounds check|error handling)|"
    r"no test(?:s)? (?:for|covering)|untested|"
    r"breaks? (?:the )?invariant|invariant violation|violates? (?:the )?invariant|"
    r"breaking change|backward[- ]?incompat|api contract|"
    r"crash(?:es|ed|ing)?|throws? (?:an )?(?:unhandled|uncaught)|unhandled exception|"
    r"divide by zero|index out of (?:range|bounds)|overflow|underflow|"
    r"regress(?:es|ion)|correctness|"
    r"missing return|wrong return|returns? (?:the )?wrong|"
    r"dead ?lock|infinite loop|unbounded (?:loop|recursion)|"
    r"resource leak|file (?:handle|descriptor) leak|connection leak|"
    r"missing await|unawaited|not awaited)\b",
    re.IGNORECASE,
)


def _is_verification_only_rejection(
    issues: "list | None",
    feedback: "str | None",
) -> bool:
    """Classify whether a REQUEST_CHANGES verdict is PURELY verification-class.

    Returns ``True`` only when EVERY blocking issue is verification-class
    (the reviewer's inability to run/verify — missing tool, 401/403 probe,
    stale workspace, failed install, "build failed"/CI pass-status concern,
    ``./check`` not found, "could not verify", etc.) AND no blocking issue
    cites a concrete code defect (logic / security / correctness / missing
    test / broken invariant / null-handling / race / injection / crash …).

    This is the conservative pre-condition for downgrading a spurious
    "I couldn't verify" REQUEST_CHANGES against authoritative green CI. The
    bias is to return ``False`` (keep blocking): an empty/whitespace blocking
    set, ANY issue that is not verification-class, or ANY concrete-defect cue
    anywhere all force ``False``. Pure / side-effect-free — unit-testable.
    """
    # Build the set of per-issue blocking texts. Fall back to the feedback
    # string only when there are no structured issues, so a single prose
    # "could not verify" verdict can still be classified.
    blocking_texts: list[str] = []
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        desc = (issue.get("description") or "").strip()
        if desc:
            blocking_texts.append(desc)
    if not blocking_texts:
        fb = (feedback or "").strip()
        if fb:
            blocking_texts.append(fb)

    # No blocking content at all → cannot positively classify as
    # verification-only; keep blocking (fail-safe).
    if not blocking_texts:
        return False

    # ANY concrete-defect cue anywhere → respect the verdict. Check the union
    # first so a defect buried in one issue can't be masked by verify phrases
    # in another.
    combined = "\n".join(blocking_texts)
    if _CONCRETE_DEFECT_RE.search(combined):
        return False

    # Every blocking issue must INDEPENDENTLY be verification-class. A single
    # issue that matches nothing (an opaque "please fix this") is not provably
    # verification-class, so it keeps the verdict blocking.
    return all(_VERIFICATION_CLASS_RE.search(text) for text in blocking_texts)


# Severities the review contract defines as NON-blocking. Per
# agent/src/prompts/review-prompt.ts, MINOR findings (cosmetic nits, naming,
# docs, optional polish) "are NON-BLOCKING … Only CRITICAL and MAJOR issues
# should drive a REQUEST_CHANGES verdict. A … PR with only MINOR findings should
# APPROVE." A blank/unknown severity is deliberately NOT in this set: an
# unclassified issue cannot be PROVEN non-blocking, so it keeps blocking.
_NON_BLOCKING_SEVERITIES = frozenset(
    {
        "MINOR",
        "NIT",
        "NITPICK",
        "LOW",
        "INFO",
        "INFORMATIONAL",
        "TRIVIAL",
        "STYLE",
        "SUGGESTION",
        "NONE",
    }
)


def _genuine_blocking_issue_exists(issues: "list | None") -> bool:
    """Does this REQUEST_CHANGES carry a GENUINE, actionable blocker?

    The recorded review decision must be a function of the reviewer's own
    STRUCTURED evidence, not of the free-text ``verdict`` token taken on faith.
    The review contract (agent/src/prompts/review-prompt.ts) already defines when
    REQUEST_CHANGES is justified, and this encodes exactly that contract. A
    genuine blocker is a structured issue that is BOTH:

      * blocking-severity — CRITICAL/MAJOR. MINOR (and synonyms) are non-blocking
        by the agent's OWN classification; a blank/unknown severity is treated
        conservatively AS blocking (cannot prove it non-blocking), AND
      * not purely verification-class — the reviewer's inability to run or verify
        (missing tool, 401/403 probe, failed install, "build failed"/CI
        pass-status worry) is, per the contract, NEVER grounds for REQUEST_CHANGES
        regardless of the severity the agent stamped on it (the prompt tells
        reviewers to flag unverifiable steps as MAJOR, so severity alone cannot
        distinguish a real defect from a tooling gap). An issue that is
        verification-class BUT also cites a concrete code defect
        (``_CONCRETE_DEFECT_RE``) IS a genuine blocker — the conservative bias.

    So a REQUEST_CHANGES whose ``issues`` array is empty, or holds only MINOR /
    purely-verification entries, is unsupported by its own structured evidence
    and (on authoritative green CI — checked by the caller) reconciles to APPROVE.

    Deliberately does NOT scan the free-text ``feedback`` to MANUFACTURE a blocker
    the structured ``issues`` omit: that free-text→decision coupling is the exact
    defect this reconciliation removes, and prose scanning was negation-blind (a
    CLEARING phrase such as "no remaining blocking correctness issue" read as a
    concrete defect via the bare ``correctness`` cue, latching the fix loop to the
    iteration cap). The structured issues are authoritative; CI-green is the
    independent safety gate.
    Pure / side-effect-free — unit-testable.
    """
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        desc = (issue.get("description") or "").strip()
        if not desc:
            # No description → no actionable blocker the fix agent could address.
            continue
        sev = (issue.get("severity") or "").strip().upper()
        if sev in _NON_BLOCKING_SEVERITIES:
            continue
        # A concrete code defect is ALWAYS a genuine blocker, even if the same
        # issue also mentions a build/verify phrase.
        if _CONCRETE_DEFECT_RE.search(desc):
            return True
        # Purely verification-class (could-not-run/verify) → not a code defect;
        # never grounds for REQUEST_CHANGES per contract, regardless of severity.
        if _VERIFICATION_CLASS_RE.search(desc):
            continue
        # A CRITICAL/MAJOR (or blank/unknown-severity) issue that is neither a
        # recognized verification phrase nor a recognized concrete-defect cue is
        # an opaque blocker ("please fix this") — keep it blocking (fail-safe).
        return True
    return False


def reconciled_approve_markers(
    issues: "list | None", feedback: "str | None"
) -> "tuple[dict[str, bool], str]":
    """Context markers + operator note for a REQUEST_CHANGES that reconciles to
    APPROVE because it carries no genuine blocker on authoritative-green CI
    (see ``_genuine_blocking_issue_exists``).

    Returns ``({context_key: True, ...}, note_markdown)``.
    ``review_verdict_reconciled_to_approve`` is always set; the legacy
    ``verification_only_downgrade`` marker is added only for the
    verification-only sub-case (reviewer could not run/verify), which also
    selects a more specific note. The note is prepended to the APPROVE review's
    feedback so the reviewer's concerns stay visible without a second comment.
    """
    markers: dict[str, bool] = {"review_verdict_reconciled_to_approve": True}
    if _is_verification_only_rejection(issues, feedback):
        markers["verification_only_downgrade"] = True
        note = (
            "_Note: the reviewer raised only verification/tooling concerns "
            "(could not run or verify in its sandbox), with no concrete code "
            "defect, while CI is authoritatively green. Treated as APPROVE._"
        )
    else:
        note = (
            "_Note: the reviewer returned REQUEST_CHANGES but listed no blocking "
            "(CRITICAL/MAJOR) issue — only non-blocking observations — while CI "
            "is authoritatively green. Per the review contract that does not "
            "block merge; treated as APPROVE._"
        )
    return markers, note


def build_ci_failure_fix_prompt(
    *,
    pr_number: int,
    base_branch: str,
    ci_errors: str,
    owner: str | None = None,
    repo: str | None = None,
) -> str:
    """Canonical prompt for a fix task spawned because CI failed on an
    already-approved PR.

    **Conflict-aware by construction.** A rollup PR that reaches the CI gate can
    *also* be out-of-date / conflicting against base. A fix prompt that only
    mentions "build/lint/test errors" — and worse, says "don't change logic" —
    sends the agent to chase CI failures while the merge conflict sits
    untouched, because the agent never even rebases onto base to discover it.
    Each cycle burns a review-fix iteration until the global cap auto-closes
    the PR with the work stranded (incident 2026-05-23, PR #5587: a single
    trivial import conflict outlived 6 iterations of CI-only fixes).

    Every CI-failure fix path MUST therefore rebase onto base first and resolve
    any conflict as part of the same fix. This is the single source of truth so
    the paths in ci_completion.py and task_lifecycle.py cannot drift again.
    """
    failing_tests = sorted(
        set(re.findall(r"^FAILED\s+(\S+::\S+)", ci_errors or "", flags=re.MULTILINE))
    )[:25]
    failing_tests_block = (
        "\nFailing tests (extracted from CI summary — fix these):\n"
        + "\n".join(f"  - {t}" for t in failing_tests)
        + "\n"
        if failing_tests
        else ""
    )
    check_runs_hint = ""
    if owner and repo:
        check_runs_hint = (
            "  2. Inspect the failing checks via the orchestrator's read-only proxy "
            '(`curl -sS -H "X-Agent-Token: $AGENT_API_TOKEN" '
            '"$ORCHESTRATOR_URL/api/agent/github/check-runs?commit_sha=$(git rev-parse HEAD)" '
            "| jq '.data.details[]'`) "
            "to confirm which checks are still failing on the latest head.\n"
        )
    # Viewport escape-hatch budget is a self-imposed CI gate that refactors trip
    # structurally (splitting/moving a component redistributes its viewport
    # opt-outs past the per-component cap). The agent can't be expected to know
    # this cross-file invariant, so spell out the remediation when the failure
    # signature is present — otherwise the fix flails (the recurring frontend
    # non-convergence trap).
    viewport_budget_hint = ""
    _ci_lower = (ci_errors or "").lower()
    if "escape-hatch" in _ci_lower and "budget" in _ci_lower:
        viewport_budget_hint = (
            "\nVIEWPORT ESCAPE-HATCH BUDGET — this specific failure is NOT a code bug. "
            "'Viewport escape-hatch debt exceeds the budget' means a component has more viewport "
            "opt-outs (data-viewport-allow-overflow / -truncation / -small-target, or an "
            "`eslint-disable ... viewport` comment) than its cap in "
            "admin-ui/src/test/viewport-escape-hatch-budget.json — almost always because this "
            "refactor split or moved a component, redistributing existing opt-outs. The fix is to "
            "BUMP that component's per-component limit (and the top-level `total`) in that JSON by "
            "the 'over by N' amount shown in the failure, IN THIS PR. Do NOT delete the opt-outs to "
            "force the count down unless they are genuinely removable without reintroducing "
            "overflow/truncation. The budget is a visible, reviewable signal, not a ratchet "
            "(a single new opt-out is already tolerated automatically).\n"
        )
    return (
        f"PR #{pr_number} is blocked by failing CI checks (code review already approved).\n\n"
        f"IMPORTANT — verify current state before acting. Between when this task was spawned and "
        f"when you run, the PR's mergeable state or check-run results may have changed. The PR may "
        f"also be conflicting or behind `{base_branch}`, which blocks the merge independently of CI. "
        f"After cloning:\n"
        f"  1. Run `git fetch origin {base_branch} && git rebase origin/{base_branch}` FIRST — if "
        f"conflicts surface, resolve every marker (preserve the intent of both branches) and continue "
        f"the rebase before touching anything else. A conflicting PR cannot merge no matter how green CI is.\n"
        f"{check_runs_hint}"
        f"{failing_tests_block}"
        f"{viewport_budget_hint}\n"
        f"CI errors captured at spawn time:\n{ci_errors[:2000]}\n\n"
        f"Diagnose before fixing. A 'new' test failure can be: (a) your diff regressed, "
        f"(b) a sibling PR broke the test — mock-signature drift or fixture change, or (c) flake. "
        f"Read the failing test first; confirm its mocks match the live signatures they patch. A "
        f'TypeError like "got multiple values for argument" or "unexpected keyword" is the mock, '
        f"not your code. Run the test on `{base_branch}` HEAD too — if it fails there, fix the test.\n\n"
        f"Focus: fix the build/lint/test errors — the code review is already approved, "
        f"don't re-architect unrelated code. If a failing test was already broken on `{base_branch}` "
        f"(unrelated to this PR's diff), repairing it is still in scope — fix it as part of "
        f"this PR rather than skipping or `./blocked`-ing.\n\n"
        f"Before declaring done: ensure (a) `git rebase origin/{base_branch}` succeeds cleanly with no "
        f"remaining conflict markers, (b) local lint/type/test suites pass on the changed files, "
        f"and (c) every fix is committed."
    )


MAX_REVIEW_FIX_ITERATIONS = 16

# Underscore-prefixed context keys are excluded from spawn-time propagation by
# default because they are per-task internal state (self-review outcomes,
# scope-pressure latches, etc.) that the child task should compute fresh. The
# keys below are exceptions: rollup-classifier flags that must survive across
# every fix/review iteration so the cap policy at `_spawn_pipeline_task`
# (global_cap / per_chain_cap) keeps resolving to MAX_REVIEW_FIX_ITERATIONS
# instead of collapsing to the medium-scope base cap (incident: PR #6061
# auto-closed at iter 12 because the rollup tag was stripped at iter 1).
#
# ``_is_rollup_review`` is DELIBERATELY NOT propagated. It is the authoritative
# PARENT-ONLY marker that ``rollup_state._is_rollup_parent_awaiting_merge``
# short-circuits on (it returns True the instant the flag is set). Propagating
# it onto child review/fix tasks made every child *look* like a rollup parent
# awaiting merge, so the EVALUATING terminalization paths (orchestrator /
# ci_completion) skipped them and a child whose PR actually merged stranded in
# EVALUATING forever (the conflict_fix_spawned latch was never cleared). The
# cap policy below no longer relies on the child carrying the flag: it detects a
# rollup chain via ``_rollup_retries`` AND the propagated ``rollup_pr_number`` +
# ``review_loop.pr_number`` lineage (see ``_is_rollup_chain_source``), both of
# which children DO carry, so the elevated cap still holds across iterations.
# Review-fix lane pilot: the intent-to-treat arm assignment (_review_fix_lane_arm)
# and the chain-private lane id (_review_fix_lane_id) must survive every
# fix/review iteration so the chain stays bound to its arm/lane. Both are inert
# to dispatch (unlike ``improvement_lane_id``, which is re-derived per fix
# phase). Reference the canonical PILOT_PROPAGATE_KEYS frozenset that owns these
# keys (imported at the top of this module) rather than re-typing the literals,
# so the propagation set and the pilot module can never silently drift apart (a
# drifted key would de-populate the trial's arm/receipt subpopulations — see
# app.review_fix_lane_pilot).
_PROPAGATE_UNDERSCORE_KEYS = frozenset({"_rollup_retries"}) | PILOT_PROPAGATE_KEYS


def _is_rollup_chain_source(source_task: "Task") -> bool:
    """True when ``source_task`` belongs to a rollup PR chain — used for cap
    policy and same-PR dedup scoping.

    Recognises a rollup chain WITHOUT requiring the child to carry the
    parent-only ``_is_rollup_review`` flag (which is no longer propagated, see
    ``_PROPAGATE_UNDERSCORE_KEYS``):
      • the parent's own ``_is_rollup_review`` flag (parent spawning the first
        review/fix), or
      • ``_rollup_retries`` (set on conflict-retry chains), or
      • the propagated rollup lineage: ``rollup_pr_number`` set AND
        ``review_loop.pr_number`` pointing at that same rollup PR — exactly what
        every spawned child of a rollup review/fix carries.
    """
    ctx = source_task.context or {}
    if ctx.get("_is_rollup_review") is True:
        return True
    if ctx.get("_rollup_retries") is not None:
        return True
    rollup_pr = ctx.get("rollup_pr_number")
    if rollup_pr:
        review_loop_pr = (ctx.get("review_loop") or {}).get("pr_number")
        if review_loop_pr and review_loop_pr == rollup_pr:
            return True
    return False


# Phase 2c (warm-VM continuity): how long after a source task's last heartbeat
# (or its completion) the warm-VM pin is honored on the spawned task. After
# this window the orchestrator's provisioning loop clears the pin and
# cold-provisions normally. Tuned to cover the typical role-handoff gap
# (agent finishing prior role + a few VM-heartbeat ticks during the next-role
# poll) without keeping a stale pin alive long enough to mask a dead VM.
WARM_HANDOFF_WINDOW_SECS = 240


def _max_review_iterations(task: Task, direction_data: dict | None = None) -> int:
    """Scope-aware review-fix iteration limit, optionally reduced for low-quality directions.

    If direction_data is provided and the direction has a poor operator acceptance rate
    (acceptance_rate < 0.3) or a high rejection ratio (prs_rejected / prs_merged > 2.0
    in recent history), the scope-based cap is reduced by ~30% to avoid wasting compute
    on directions the operator consistently rejects.
    """
    ctx = task.context or {}
    scope = ctx.get("scope", "medium")
    fix_reason = ctx.get("review_loop", {}).get("fix_reason", "code_review")

    # CI-only fixes should converge fast (lint/type/test errors)
    if fix_reason == "ci_failure":
        limit = 8
    # Small scope tasks: less code, fewer things to fix
    elif scope == "small":
        limit = 10
    # Medium scope: moderate iteration budget
    elif scope == "medium":
        limit = 12
    # Large scope or unknown: full budget
    else:
        limit = 16

    # Direction-aware reduction: cut cap by ~30% for consistently-rejected directions.
    # Only applies when direction_data is available and we're not already at the ci_failure cap.
    if direction_data is not None and fix_reason != "ci_failure":
        from app.research_completion import _compute_acceptance_rate

        acceptance_rate = _compute_acceptance_rate(direction_data)
        # Check recent rejection ratio from last few history entries
        recent_history = (direction_data.get("research_history") or [])[-3:]
        recent_merged = sum(e.get("prs_merged", 0) for e in recent_history)
        recent_rejected = sum(e.get("prs_rejected", 0) for e in recent_history)
        high_rejection_ratio = (
            recent_merged > 0 and (recent_rejected / recent_merged) > 2.0
        ) or (recent_merged == 0 and recent_rejected >= 3)
        if (
            acceptance_rate is not None and acceptance_rate < 0.3
        ) or high_rejection_ratio:
            limit = max(1, round(limit * 0.7))

    # Scope-budget hard cap: once a medium-scope PR crosses 90% of its
    # file budget, a prior spawn persists an absolute iteration ceiling via
    # _scope_budget_hard_cap. Honor it regardless of scope/direction-aware
    # logic so the reduction survives across subsequent review/fix spawns.
    hard_cap = ctx.get("_scope_budget_hard_cap")
    if isinstance(hard_cap, int) and hard_cap > 0:
        limit = min(limit, hard_cap)

    # Issue-churn cap reduction: when churn is detected in a prior spawn,
    # the reduced cap persists via _churn_cap_reduction so subsequent spawns
    # respect the earlier reduction and converge sooner instead of always
    # running to the full base cap.
    churn_cap = ctx.get("_churn_cap_reduction")
    if isinstance(churn_cap, int) and churn_cap > 0:
        limit = min(limit, churn_cap)

    # Retry carry-forward: if a prior attempt of this task (before parent requeue
    # cloned it) already burned review iterations, subtract half of that usage
    # from the fresh cap so retries don't each get a full budget. Floor at 3 so
    # a heavily-iterated prior attempt doesn't leave the retry dead on arrival.
    prior_consumed = ctx.get("_prior_review_iterations_consumed")
    if isinstance(prior_consumed, int) and prior_consumed > 0:
        limit = max(limit - prior_consumed // 2, 3)

    return min(limit, MAX_REVIEW_FIX_ITERATIONS)


SPAWN_REASON_KEY = "_last_pipeline_spawn_reason"
# Reason codes written to ``source_task.context[SPAWN_REASON_KEY]`` by
# ``_spawn_pipeline_task``. Rollup callers read this to distinguish
# transient deferrals (retry next sweep) from terminal failures.
SPAWN_REASON_SPAWNED = "spawned"
SPAWN_REASON_DEDUP = "dedup_same_type"
SPAWN_REASON_ALREADY_MERGED = "already_merged"
# PR was closed without merging — distinct from already_merged so callers can
# log accurately (a closed-unmerged PR is NOT the same as a mid-flight merge).
SPAWN_REASON_PR_CLOSED_UNMERGED = "pr_closed_unmerged"
SPAWN_REASON_DEFERRED = "deferred_cross_type"
SPAWN_REASON_CAP = "cap_exhausted"
SPAWN_REASON_STUCK = "stuck_cycle"
# Early non-convergence escalation: the review-fix loop is whittling/drifting
# (shrinking-but-never-zero count, verification-only drift, or one-at-a-time
# disclosure) and was escalated to the operator at iteration K rather than
# grinding to the full cap. Distinct from stuck_cycle (same issue re-raised).
SPAWN_REASON_NONCONVERGENCE = "early_nonconvergence"
# Consecutive supersession cancellations on one PR chain crossed the churn
# bound, so the pipeline stopped respawning and escalated the driver task.
SPAWN_REASON_SUPERSEDE_CHURN = "supersession_churn_halt"
# Two consecutive same-type pipeline tasks failed with an identical failure
# fingerprint — the agent's code edits aren't moving the failure shape, which
# is the signature of a precondition gap (missing system lib, baseline lint
# debt, toolchain layer) rather than a code defect. Halt the chain and let
# the strategic agent (or operator) decide whether to spawn an infra fix.
SPAWN_REASON_BLOCKED_REPEAT = "blocked_repeat_fingerprint"
# A lane-arm review boundary found the lane checkpoint missing/unverifiable and,
# while still under the requeue budget, suppressed the cold review and requeued
# the failed phase as a lane-aware recovery task (see _handle_lane_checkpoint_recovery).
SPAWN_REASON_LANE_RECOVERY = "lane_checkpoint_recovery"
# The conflict-fix lane accumulated enough consecutive completions without
# resolving the merge conflict that further attempts are unlikely to succeed.
# Escalated to the operator before the lane sub-cap fires.
SPAWN_REASON_PERSISTENT_CONFLICT = "persistent_conflict"

# Grace window for the mid-retry defer: when a same-PR fix is FAILED/CANCELLED
# with an infra failure_category and the failure is younger than this many minutes,
# a review spawn is deferred so the fix can be requeued (rather than producing a
# stale verdict on unchanged code). Outside the window or with budget exhausted,
# review spawns proceed normally.
_STALE_HEAD_MID_RETRY_GRACE_MINUTES = 20

_PIPELINE_ACTIVE_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
    # A BLOCKED pipeline task still holds its active-idempotency slot at the DB
    # level (alembic 0138 / ux_task_active_idempotency_key counts BLOCKED), so
    # the liveness/dedup logic here must treat it as live too — otherwise a
    # BLOCKED fix is invisible to dedup while the index reserves its key,
    # producing duplicate re-spawns against an unchanged head (PR #8742) and
    # missed cross-type defers (PR #8746).
    TaskStatus.BLOCKED,
    # PARKED holds its active-idempotency slot for the same reason: alembic 0199
    # widened ux_task_active_idempotency_key to count PARKED. Treat it as live in
    # dedup or a parked task is invisible while the index reserves its key,
    # reproducing the exact duplicate-re-spawn bug BLOCKED was added to close.
    TaskStatus.PARKED,
)

SCOPE_OVERFLOW_PR_SIGNAL_EMITTED_KEY = "scope_overflow_pr_signal_emitted"


def _pipeline_task_idempotency_key(
    *,
    project_id,
    task_type: str,
    pr_number: int,
    original_task_id: str,
    iteration: int,
    is_rollup: bool,
    review_target: dict[str, str | int] | None = None,
) -> str:
    chain_id = f"rollup-pr-{pr_number}" if is_rollup else original_task_id
    target_suffix = ""
    if task_type == "review" and review_target is not None:
        target_suffix = (
            f":{review_target['base_sha']}:{review_target['head_sha']}"
        )
    return (
        f"review_pipeline:{project_id}:{chain_id}:{pr_number}:{task_type}:{iteration}"
        f"{target_suffix}"
    )


async def _get_active_pipeline_task_by_key(
    session: AsyncSession,
    idempotency_key: str,
) -> Task | None:
    return (
        (
            await session.execute(
                select(Task).where(
                    Task.idempotency_key == idempotency_key,
                    Task.status.in_(_PIPELINE_ACTIVE_STATUSES),
                )
            )
        )
        .scalars()
        .first()
    )


def _set_spawn_reason(task: Task, reason: str) -> None:
    ctx = dict(task.context or {})
    ctx[SPAWN_REASON_KEY] = reason
    task.context = ctx


def _normalized_review_target(
    target: dict[str, str | int] | None,
    *,
    project_id: object,
    pr_number: int,
) -> dict[str, str | int] | None:
    """Return a comparable immutable review target, or ``None`` when incomplete."""
    if not isinstance(target, dict):
        return None
    base_sha = target.get("base_sha")
    head_sha = target.get("head_sha")
    if (
        not isinstance(base_sha, str)
        or not base_sha
        or not isinstance(head_sha, str)
        or not head_sha
    ):
        return None
    return {
        "project_id": str(project_id),
        "pr_number": int(pr_number),
        "base_sha": base_sha,
        "head_sha": head_sha,
    }


def _review_task_matches_target(task: Task, target: dict[str, str | int]) -> bool:
    """Whether a spawned review is for exactly ``target`` (never a loose PR match)."""
    task_target = ((task.context or {}).get("review_loop") or {}).get("review_target")
    return task_target == target


async def _load_vm_contact_lookup(
    session: AsyncSession, tasks: Iterable[Task]
) -> dict[str, datetime]:
    """{vm_instance_id: last_contact_at} for the VMs behind ``tasks``.

    R3: the uncontended liveness signal the supersede path needs. See
    ``_is_pipeline_task_stale`` and ``VmContact`` (models.py) — a lock-starved
    heartbeat leaves ``Task.last_heartbeat`` stale on a LIVE agent, but the
    agent's ``VmContact`` row is written off the contended Task row and cannot
    be starved. Batch-fetched once per spawn (mirrors reaper.py:477-496).
    """
    from app.models import VmContact

    vm_ids = {t.vm_instance_id for t in tasks if t.vm_instance_id}
    if not vm_ids:
        return {}
    rows = (
        await session.execute(
            select(VmContact.vm_instance_id, VmContact.last_contact_at).where(
                VmContact.vm_instance_id.in_(vm_ids)
            )
        )
    ).all()
    return {
        vm_id: last_contact for vm_id, last_contact in rows if last_contact is not None
    }


async def _load_pipeline_liveness_lookups(
    session: AsyncSession, tasks: Iterable[Task]
) -> tuple[dict[str, datetime], dict]:
    """Both liveness lookups ``_is_pipeline_task_stale`` needs, in one pass.

    Returns ``(vm_contact_lookup, runner_report_lookup)``:

      • ``vm_contact_lookup`` — the uncontended ``VmContact.last_contact_at``
        override that stops a lock-starved-but-LIVE agent being superseded.
      • ``runner_report_lookup`` — the DEFEAT for that override on fleet/local
        runners, where ``vm_instance_id='runner:<id>'`` is SHARED by every task
        on the runner, so a live sibling's contact would otherwise mask a dead
        reviewer forever (MAJOR 5; the shared-VmContact zombie gap #14413 closed
        in the reaper).

    Callers MUST pass both into ``_is_pipeline_task_stale`` — passing only the
    first re-opens the zombie mask. Mirrors reaper.py:521-534, which builds the
    same pair from the same sweep's candidate VM ids.
    """
    from app.reaper_orphan_classify import _build_runner_report_lookup

    vm_contact_lookup = await _load_vm_contact_lookup(session, tasks)
    vm_ids = {t.vm_instance_id for t in tasks if t.vm_instance_id}
    if not vm_ids:
        return vm_contact_lookup, {}
    runner_report_lookup = await _build_runner_report_lookup(session, vm_ids)
    return vm_contact_lookup, runner_report_lookup


async def _relock_pipeline_task_for_cancel(
    session: AsyncSession, task_id
) -> Task | None:
    """Re-SELECT a supersede/cancel candidate FOR UPDATE; None if it must be spared.

    R3 (2026-07-12 concurrency audit). The supersede decision is made from a
    plain, UNLOCKED ``SELECT ... WHERE status IN (active)`` and the cancel that
    follows is an ORM UPDATE-by-PK with no ``WHERE status`` guard — and
    ``version_id_col`` was deliberately dropped globally (models.py:1370, :1677),
    so there is no optimistic-concurrency backstop either. A reviewer that
    commits COMPLETED + APPROVE inside that window is therefore BLIND-OVERWRITTEN
    to CANCELLED and its VM queued for deletion; worse, the write also replaces
    the whole ``context`` blob from the stale in-memory snapshot, so the
    just-committed APPROVE verdict is ERASED rather than merely superseded.

    This closes the window: re-select the row FOR UPDATE (which serializes
    against the agent's own terminal write, the only thing the per-PR advisory
    lock does NOT serialize against) and re-assert the status under that lock.
    ``populate_existing=True`` refreshes the identity-mapped instance from THIS
    select, so the caller's subsequent ``dict(t.context or {})`` reads the FRESH
    context, not the pre-race snapshot.

    Returns None — meaning DO NOT CANCEL — when the row went terminal during the
    window, or when it is held by a peer (``skip_locked``): a peer holding this
    row is typically the agent's completion handler mid-commit, and the fail-safe
    is to leave a possibly-live task alone. The lock is released by the caller's
    eventual commit; everything between here and that commit is DB-only work.
    """
    locked = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == task_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if locked is None:
        return None
    if locked.status not in _PIPELINE_ACTIVE_STATUSES:
        return None
    return locked


def _is_lock_timeout_error(exc: BaseException) -> bool:
    """True when a DBAPI error is Postgres' bounded lock_timeout firing.

    On Postgres a ``lock_timeout`` surfaces as asyncpg
    ``LockNotAvailableError`` (sqlstate 55P03, message "canceling statement
    due to lock timeout"), wrapped by SQLAlchemy into a ``DBAPIError``
    subclass. There is no stable wrapped type across driver versions, so
    match the canonical message/sqlstate markers — same string-matching
    convention as ``_POST_COMMIT_TRANSIENT_SUBSTRINGS`` in recovery_sweeps.
    """
    msg = f"{type(exc).__name__}: {exc}".lower()
    return (
        "lock timeout" in msg
        or "lock_timeout" in msg
        or "locknotavailable" in msg
        or "lock not available" in msg
        or "55p03" in msg
    )


_FINGERPRINT_NOISE_RE = (
    # Canonical UUID with dashes (8-4-4-4-12) — matched before the generic
    # hex regex so the whole UUID collapses into one token instead of leaving
    # the 4-char middle groups behind.
    re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    ),
    # SHAs / hex tokens of 8+ hex chars (no dashes)
    re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE),
    # Timestamps "2026-05-06T09:31" / "2026-05-06 09:31"
    re.compile(
        r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(z|[+-]\d{2}:?\d{2})?",
        re.IGNORECASE,
    ),
    # /tmp/<noise> paths
    re.compile(r"/tmp/\S+"),
    # iter / iteration markers ("iter=3", "iteration 3", "iter:3")
    re.compile(r"\biter(ation)?[\s=:]+\d+", re.IGNORECASE),
    # attempt N / attempt: N
    re.compile(r"\battempt[\s=:]+\d+", re.IGNORECASE),
    # task-XXXXXX / agent-vm-XXXXX style identifiers
    re.compile(r"\b(?:task|agent-vm|vm)-[a-z0-9-]+", re.IGNORECASE),
    # Standalone integers ≥ 4 digits (line numbers, counts, etc. that vary by run)
    re.compile(r"\b\d{4,}\b"),
)


def _failure_fingerprint_msg(msg: str | None) -> str:
    """Stable 12-char hash of a failure status message for cross-iteration comparison.

    Mirrors the agent-side ``errorFingerprint`` (in ``agent/src/index.ts``) but
    additionally strips iteration-specific noise — UUIDs, timestamps, /tmp paths,
    iter/attempt counters, agent-vm identifiers — so that two iterations hitting
    the same underlying blocker produce identical fingerprints despite cosmetic
    differences in their status messages.

    Returns ``""`` for empty input. Returned hashes are 12 hex chars (SHA-256
    truncated) to match the ``errorFingerprint`` format used elsewhere.
    """
    from hashlib import sha256

    if not msg:
        return ""
    s = msg.strip().lower()
    if not s:
        return ""
    for pat in _FINGERPRINT_NOISE_RE:
        s = pat.sub("X", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    return sha256(s.encode("utf-8")).hexdigest()[:12]


async def _post_repeat_fingerprint_blocker_message(
    session: AsyncSession,
    *,
    source_task: Task,
    pr_number: int,
    fingerprint: str,
    prior_task_id,
) -> None:
    """Surface a repeat-fingerprint halt to the strategic agent via ProjectMessage.

    The strategic agent reads ``open`` ProjectMessages on its cycle (see
    ``directions.py`` open_messages loading), so this becomes the trigger for
    "decide whether to spawn a precondition-fix direction or defer." Posted
    with ``from_type="system"`` and ``message_type=BLOCKER`` so it sorts
    correctly in the strategic agent's context block.
    """
    from app.message_audience import stamp_message_audience
    from app.models import MessageType
    from app.project_message_creation import build_project_message

    body = (
        f"Pipeline halt — repeat-fingerprint detected on PR #{pr_number}.\n\n"
        f"The fix-agent's code edits aren't moving the failure shape: this "
        f"task ({str(source_task.id)[:8]}) failed with the same fingerprint "
        f"({fingerprint}) as the prior attempt ({str(prior_task_id)[:8]}). "
        f"Identical failures across iterations is the signature of a "
        f"*precondition gap* (missing system library, baseline lint/test "
        f"debt, toolchain mismatch) rather than a code defect — another "
        f"review-fix iteration won't help.\n\n"
        f"Latest failure text:\n"
        f"  {(source_task.latest_status_message or '')[:600]}\n\n"
        f"Decision points (yours):\n"
        f"  - Spawn a direction to fix the precondition (update the packer "
        f"image, scope the gate to changed files, add a missing dep, etc.) "
        f"if the unblock is worth the investment for this PR + future ones.\n"
        f"  - Defer and route around it — leave this PR halted, work on "
        f"unrelated directions.\n"
        f"  - Cancel the chain (operator) if the work is no longer worth "
        f"pursuing.\n\n"
        f"This task is now classified as BLOCKED_PRECONDITION (INFRA class — "
        f"does not count toward the per-PR review-fix iteration cap, will "
        f"not be auto-resurrected by the reaper)."
    )
    pm = await build_project_message(
        session,
        project_id=source_task.project_id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        from_task_id=source_task.id,
        content=body,
        context=stamp_message_audience(
            {
                "pr_number": pr_number,
                "fingerprint": fingerprint,
                "source_task_id": str(source_task.id),
                "prior_task_id": str(prior_task_id),
                "trigger": "repeat_fingerprint_halt",
            },
            "repeat_fingerprint_halt",
        ),
    )
    session.add(pm)


async def _emit_scope_overflow_pr_signal(session, signal: dict) -> None:
    """Emit the deferred scope-overflow PR signal (label + comment) POST-commit.

    C2 lock-window fix: token mint + add_issue_label + create_pr_comment are
    GitHub network I/O that must NOT run while the per-PR advisory lock is held
    (those round-trips can block 5-30s under rate-limit). The caller persists
    the ``scope_overflow_pr_signal_emitted`` idempotency flag transactionally
    BEFORE committing, so this is fire-and-forget / best-effort: it never
    double-posts and a network failure here only drops one advisory
    label/comment (logged). Token resolution itself is GitHub/secret I/O, so it
    too stays out of the lock.
    """
    try:
        from app.github import add_issue_label, create_pr_comment
        from app.routers.agent import get_github_token_for_task

        gh_token, owner, repo = await get_github_token_for_task(
            signal["token_task_id"], session
        )
        _pr = signal["pr_number"]
        await add_issue_label(gh_token, owner, repo, _pr, "scope-overflow")
        await create_pr_comment(gh_token, owner, repo, _pr, signal["comment_body"])
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Failed to emit scope-overflow PR signal for PR "
            f"#{signal['pr_number']}: {e}",
            exc_info=True,
        )


async def _spawn_verify_tests_task(
    source_task: Task,
    session: AsyncSession,
    *,
    pr_number: int,
    branch: str,
    base_branch: str,
    original_task_id: str,
    attempt: int = 0,
) -> tuple[Task | None, bool]:
    """Spawn (or reuse) a one-shot ``verify_tests`` merge-gate task on a PR head.

    ``attempt`` is the verify-gate retry counter (0 on first spawn). It feeds the
    idempotency-key ``iteration`` so a re-spawn after a crashed prior attempt gets
    a distinct key — a not-yet-terminalized corpse cannot block the retry, and
    TaskHistory lineage stays clean.

    Returns ``(task, was_created)`` like ``_spawn_pipeline_task``:
      - ``(existing, False)`` when an active verify_tests task for this PR is
        already running (dedup) — the caller can await its verdict.
      - ``(new_task, True)`` when a fresh task was created.

    Called inline from ``_spawn_pipeline_task`` while it holds the per-PR
    advisory lock, so the dedup SELECT + insert are serialized against
    concurrent spawns on the same PR.  build_config (which the agent's
    ``resolveBuildConfig`` reads to detect the suite) is attached to the task
    at claim time from the Project, so it is not propagated here.
    """
    # Dedup: an active verify_tests task on the same PR is reused rather than
    # duplicated. The no-CI gate records the spawned id and awaits it, so this
    # is belt-and-suspenders against a double poll.
    active = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == source_task.project_id,
                    Task.status.in_(_PIPELINE_ACTIVE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    for t in active:
        if t.id == source_task.id:
            continue
        tp = t.first_payload
        if tp.get("type") == "verify_tests" and tp.get("pr_number") == pr_number:
            logger.info(
                f"[PIPELINE] Reusing active verify_tests task {t.id} for PR #{pr_number}"
            )
            return (t, False)

    idempotency_key = _pipeline_task_idempotency_key(
        project_id=source_task.project_id,
        task_type="verify_tests",
        pr_number=pr_number,
        original_task_id=original_task_id,
        iteration=attempt,
        is_rollup=False,
    )
    existing_by_key = await _get_active_pipeline_task_by_key(session, idempotency_key)
    if existing_by_key is not None:
        logger.warning(
            f"[PIPELINE] Reusing idempotent verify_tests spawn for PR #{pr_number}: "
            f"{existing_by_key.id}"
        )
        return (existing_by_key, False)

    # Parent: mirror review/fix (sibling under the create task's parent).
    parent_id = None
    inherited_priority = source_task.priority
    try:
        orig = await session.get(Task, uuid.UUID(original_task_id))
        if orig and orig.parent_task_id:
            parent_id = orig.parent_task_id
        elif orig:
            parent_id = orig.id
        if orig:
            inherited_priority = orig.priority
    except Exception:  # noqa: BLE001
        logger.debug(
            f"[PIPELINE] verify_tests: could not inherit from original task "
            f"{original_task_id[:8] if original_task_id else '?'}"
        )

    type_model = _runtime_config.get("task_type_models", {}).get("verify_tests") or None
    type_effort = (
        _runtime_config.get("task_type_efforts", {}).get("verify_tests") or None
    )
    payload_data = {
        "type": "verify_tests",
        "pr_number": pr_number,
        "branch": branch,
        "base_branch": base_branch,
        "original_task_id": original_task_id,
        **({"model": type_model} if type_model else {}),
        **({"effort": type_effort} if type_effort else {}),
    }
    payload_data = validate_internal_payload(
        payload_data, f"review_pipeline:verify_tests:pr#{pr_number}"
    )

    # Carry strategic context (direction_id, scope, …) but drop the parking
    # task's review_loop and transient underscore-internal flags.
    source_ctx = source_task.context or {}
    propagated_ctx = {
        k: v
        for k, v in source_ctx.items()
        if k != "review_loop"
        and (not k.startswith("_") or k in _PROPAGATE_UNDERSCORE_KEYS)
    }
    propagated_ctx["review_loop"] = {
        "pr_number": pr_number,
        "branch": branch,
        "base_branch": base_branch,
        "original_task_id": original_task_id,
    }

    new_task = Task(
        project_id=source_task.project_id,
        title=f"Verify tests PR #{pr_number}",
        description=(
            f"Run the repo's detected test suite on PR #{pr_number} head to gate "
            f"the no-CI merge"
        ),
        parent_task_id=parent_id,
        priority=inherited_priority,
        payloads=[payload_data],
        context=propagated_ctx,
        idempotency_key=idempotency_key,
        # verify_tests is a read-only one-shot — cold-provision a fresh VM (no
        # warm-handoff pin); the review chain has ended at this point.
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await _get_active_pipeline_task_by_key(session, idempotency_key)
        if existing is None:
            raise
        logger.warning(
            f"[PIPELINE] Concurrent verify_tests spawn for PR #{pr_number} "
            f"hit idempotency key; using existing task {existing.id}"
        )
        return (existing, False)
    await session.refresh(new_task)

    session.add(
        TaskHistory(
            task_id=new_task.id,
            action="SPAWNED",
            details={
                "spawned_by": "REVIEW_PIPELINE",
                "type": "verify_tests",
                "pr_number": pr_number,
            },
        )
    )
    await session.commit()

    await broadcast_to_project(
        str(source_task.project_id),
        "task_updated",
        {"task_id": str(new_task.id), "status": TaskStatus.QUEUED},
    )
    return (new_task, True)


def build_merge_conflict_fix_prompt(
    pr_number: int | str,
    base_branch: str,
    *,
    merge_error: str | None = None,
) -> str:
    """Prompt for a merge-conflict resolution fix task (review already approved).

    Hardened after rollup PR #8920 (research task 72e4b062). Two failure modes
    shipped a textually-clean resolution green through the agent's standalone
    gate and only failed in CI — after the 16-iteration review-fix budget was
    already spent: (a) the resolution silently DROPPED a hunk one side added
    (`db_session.add(project)` → FK violation), and (b) it kept one side's code
    against the OTHER side's test for the same feature (a code/test vintage
    skew). The guidance below tells the agent to COMBINE both intents on
    overlapping hunks rather than pick a side, and to preserve every side's
    non-overlapping content. Enforcement is agent-side: a conflict-resolution
    fix now runs the affected backend test files under Postgres in ``./check``,
    so ``./pr open`` cannot push until they pass (see the agent's
    ``buildMergeVerifyBlock``).

    ``merge_error`` (when provided) appends the original GitHub merge message,
    truncated, for context.
    """
    prompt = (
        f"PR #{pr_number} cannot merge into `{base_branch}` due to a merge conflict "
        f"(GitHub mergeable_state='dirty'). The code review is already approved — "
        f"focus only on resolving the conflict.\n\n"
        f"The base (`{base_branch}`) has already been fetched and integrated into "
        f"your working tree by the harness — do NOT re-run git fetch/rebase.\n\n"
        f"  1. Resolve the conflict markers the integration left.\n"
        f"  2. When BOTH sides changed the same function or logic, COMBINE both "
        f"behaviors — do NOT pick one side or delete the "
        f"other side's change. If `{base_branch}` restructured code this branch "
        f"also modified, re-apply this branch's intent on top of the new "
        f"structure so neither side's change is lost; and do NOT drop a hunk "
        f"either side added in a region the other did not touch.\n"
        f"  3. Re-run the affected tests. Your `./check` gate runs the affected "
        f"backend test files under Postgres and `./pr open` will NOT push until "
        f"they pass — iterate edit → `./check` until green.\n"
        f"  4. Commit the resolution and force-push with `--force-with-lease`."
    )
    if merge_error:
        prompt += f"\n\nOriginal merge error: {merge_error[:500]}"
    return prompt


def build_migration_guard_fix_prompt(
    *,
    pr_number: int | str,
    base_branch: str,
    guard_reason: str,
) -> str:
    """Prompt for a deterministic Alembic pre-merge guard recovery task.

    A revision collision does not necessarily mean the PR still has new schema
    work to land. Another PR may already have shipped the same schema change
    under a different revision. The repair prompt must make that semantic
    distinction before suggesting a mechanical renumber; otherwise the fix lane
    can manufacture a second migration for a change that is already on base.
    """
    return (
        f"PR #{pr_number} cannot merge into `{base_branch}` because Blueprint's "
        "pre-merge Alembic combined-chain guard found a deterministic migration "
        "chain conflict.\n\n"
        f"Guard reason: {guard_reason[:1000]}\n\n"
        "Your task is only to repair the PR's Alembic migration chain so it is "
        f"valid against the current `{base_branch}` branch.\n\n"
        "  1. Inspect the PR's added/modified files under "
        "`backend/alembic/versions` and compare each migration's schema intent "
        "with the migrations and resulting schema already present on the base "
        "branch; do not decide from revision numbers or filenames alone.\n"
        "  2. If another PR already landed the same schema change on the base "
        "branch, drop this PR's semantically duplicate migration and rebase the "
        "remaining code/tests onto the base implementation. Do not renumber a "
        "semantically duplicate migration into a second migration.\n"
        "  3. Only when the PR contains genuinely new schema work whose revision "
        "id collides with the base, renumber that PR migration to the next unused "
        "revision id and update its filename and in-file `revision` consistently.\n"
        "  4. Rechain each remaining PR migration's `down_revision` onto the "
        "current base head. If the PR contains a sequence of migrations, "
        "preserve their relative order and rechain each descendant onto its "
        "updated predecessor.\n"
        "  5. Do not edit, delete, or rename migrations that already exist on "
        "the base branch; only rewrite this PR's migration files.\n"
        "  6. Run the relevant Alembic/backend checks, then commit and "
        "force-push with `--force-with-lease`."
    )


async def _check_fix_mid_retry_defer(
    source_task: Task,
    session: AsyncSession,
    pr_number: int,
    resolved_original_id: str,
    is_rollup_source: bool,
) -> bool:
    """True if a same-PR fix is FAILED/CANCELLED with an infra failure_category
    within the grace window AND still has retry budget, so a review spawn should
    be deferred rather than producing a verdict on unchanged code.

    Outside the grace window or with budget exhausted, returns False so the
    review spawn proceeds and the chain cannot wedge forever.
    Best-effort: a lookup error always returns False (fail-open for review spawns).
    """
    from app.failure_classification import INFRA_CLASSES
    from app.reaper_sweeps import _BLOCKED_OPTIMISTIC_RETRY_CAP

    cutoff = _utcnow() - timedelta(minutes=_STALE_HEAD_MID_RETRY_GRACE_MINUTES)
    try:
        # Push updated_at and pr_number filters into SQL to avoid a full project
        # table scan on long-lived projects. pr_number is matched Python-side via
        # first_payload because the JSONB expression index (ix_task_payloads_pr_number)
        # is on the payloads column; updated_at has a plain B-tree index.
        dialect_name = resolve_dialect_name(session)
        if dialect_name == "sqlite":
            pr_number_match = (
                func.json_extract(Task.payloads, "$[0].pr_number") == pr_number
            )
            type_match = func.json_extract(Task.payloads, "$[0].type") == "fix"
        else:
            pr_number_match = Task.payloads[0]["pr_number"].as_string() == str(
                pr_number
            )
            type_match = Task.payloads[0]["type"].as_string() == "fix"

        candidates = (
            (
                await session.execute(
                    select(Task)
                    .options(
                        load_only(
                            Task.id,
                            Task.status,
                            Task.context,
                            Task.payloads,
                            Task.updated_at,
                            raiseload=True,
                        )
                    )
                    .where(
                        Task.project_id == source_task.project_id,
                        Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
                        Task.updated_at >= cutoff,
                        pr_number_match,
                        type_match,
                    )
                )
            )
            .scalars()
            .all()
        )
        for t in candidates:
            fp = t.first_payload
            if (
                not is_rollup_source
                and (fp.get("original_task_id") or "") != resolved_original_id
            ):
                continue
            ctx = t.context or {}
            fc = ctx.get("failure_category")
            if fc not in INFRA_CLASSES:
                continue
            # Budget check differs by terminal state:
            # - FAILED: retried by the resurrection sweep (cap=1 via _resurrection_count
            #   in reaper_infra_recovery.py). Budget exhausted when _resurrection_count >= 1.
            # - CANCELLED: retried by the blocked-optimistic requeue path (cap via
            #   _BLOCKED_OPTIMISTIC_RETRY_CAP / _blocked_optimistic_retries).
            if t.status == TaskStatus.FAILED:
                if int(ctx.get("_resurrection_count", 0)) >= 1:
                    continue
            else:
                if (
                    int(ctx.get("_blocked_optimistic_retries", 0))
                    >= _BLOCKED_OPTIMISTIC_RETRY_CAP
                ):
                    continue
            return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "[PIPELINE] _check_fix_mid_retry_defer lookup failed for "
            f"PR #{pr_number} — allowing review spawn",
            exc_info=True,
        )
    return False


def task_counts_toward_review_cap(task: Task) -> bool:
    """Whether ``task`` counts toward the per-PR / per-lane review-fix iteration cap.

    Single source of truth for cap-counting eligibility, shared by the pipeline
    spawn gate (``_spawn_pipeline_task``) and read-only diagnostics
    (admin lane-diag) so the diagnostic's iteration totals match operator
    control logic exactly. A task is EXCLUDED from the cap when any of the
    following apply:

      * it is archived — the pipeline cap query always filters ``archived=False``,
        mirroring the manual iteration-cap reset path;
      * it is FAILED/CANCELLED with an infra-class ``failure_category`` — it
        never actually ran a review (provisioning / rate limit / agent timeout
        / external API / admin-cancel supersede), so counting it would trip the
        cap with zero real reviews on the PR; or
      * it is a stale-head discard (``_review_stale_head``) — a review that ran
        against an older head than the live PR head, discarded and respawned;
        counting it would penalise the chain for a verdict it couldn't act on.

    Note: the live pipeline cap query selects status in (COMPLETED, FAILED) only
    — it never counts CANCELLED tasks, even non-infra ones.  This function
    applies the same infra-class exclusion to CANCELLED tasks for consistency,
    so non-infra CANCELLED tasks *are* counted here.  This is an intentional
    diagnostic-only deviation: CANCELLED tasks in live chains are rare outside
    infra/supersede paths and the slight over-count is acceptable for a pilot
    diagnostic.

    Excluded tasks remain VISIBLE as waste/drilldown rows in diagnostics; they
    are only omitted from the cap-counted iteration total.

    Uses the INFRA analytics superset (INFRA_ANALYTICS_CLASSES) so an
    UNPLACEABLE_EXECUTION_SITE task — which likewise never ran a review — is
    excluded too.
    """
    from app.failure_classification import INFRA_ANALYTICS_CLASSES

    if getattr(task, "archived", False):
        return False
    if task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
        fc = (task.context or {}).get("failure_category")
        if fc in INFRA_ANALYTICS_CLASSES:
            return False
    return not (task.context or {}).get("_review_stale_head")


def _recent_consecutive_lane_count(
    tasks: Iterable[Task],
    target_lane: str,
    counts_toward_cap: Callable[[Task], bool] = task_counts_toward_review_cap,
) -> int:
    """Count the newest uninterrupted segment of cap-counting tasks in one lane."""

    count = 0
    sorted_tasks = sorted(
        tasks,
        key=lambda task: (
            task.created_at.timestamp() if task.created_at is not None else 0.0
        ),
        reverse=True,
    )
    for task in sorted_tasks:
        if not counts_toward_cap(task):
            continue
        if classify_iteration_lane(task) != target_lane:
            break
        count += 1
    return count


def _review_supersede_churn_count(task: Task | None) -> int:
    """Consecutive same-PR supersession cancellations carried on the chain root."""
    try:
        return int((task.context or {}).get(_REVIEW_SUPERSEDE_CHURN_COUNT_KEY, 0) or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _review_supersede_churn_snapshot(
    task: Task,
    *,
    task_type: str,
    pr_number: int,
    reason: str,
) -> dict[str, object]:
    """Capture the last superseded task's diagnostics for escalation context."""
    snapshot: dict[str, object] = {
        "task_id": str(task.id),
        "task_type": task_type,
        "reason": reason,
        "status": task.status,
    }
    if task.latest_status_message:
        snapshot["latest_status_message"] = task.latest_status_message
    if task.started_at is not None:
        snapshot["started_at"] = task.started_at.isoformat()
    if task.last_heartbeat is not None:
        snapshot["last_heartbeat"] = task.last_heartbeat.isoformat()
    snapshot["pr_number"] = pr_number
    return snapshot


async def _handle_lane_checkpoint_recovery(
    session: AsyncSession,
    *,
    source_task: Task,
    lane: Any,
    pr_number: int,
    branch: str,
    base_branch: str,
    resolved_original_id: str,
    propagated_ctx: dict,
    parent_id: uuid.UUID | None,
    inherited_priority: Any,
) -> tuple[Task | None, bool]:
    """Bounded recovery for a lane checkpoint that went missing at a review boundary.

    Replaces the old silent "spawn the cold review anyway" behavior on a
    checkpoint miss that is still under the requeue budget: suppress the review
    and spawn a lane-aware retry of the failed phase carrying ONLY durable chain
    metadata (lane chain id, arm assignment, PR/branch/review_loop state, and the
    recovery reason). It deliberately carries NO stale warm-handoff pin, lane
    checkpoint SHA, or lane-reserved VM directive — the warm workspace could not
    be trusted, so the recovery runs cold from a fresh clone. The active lane is
    rebound to the recovery task so chain continuity and the operator view follow
    the retry. Returns ``(recovery_task, True)``; the caller treats a truthy task
    as "the chain advanced" (not an iteration-cap escalation).

    The exhausted-budget case is handled upstream by ``checkpoint_or_recover_phase``
    (lane CAS to KILLED + reservation release) and is NOT routed here — an
    exhausted lane falls back to a classic cold review.
    """
    from app.review_fix_lane_pilot import (
        LANE_CHAIN_ID_KEY,
        LANE_PILOT_ASSIGNMENT_KEY,
        LANE_RECOVERY_ATTEMPT_KEY,
        LANE_RECOVERY_REASON_KEY,
        rebind_lane_to_recovery,
        strip_transient_lane_context,
    )

    attempt = (lane.phase_requeues or 0) if lane is not None else 0

    # Durable-only recovery context: carry the chain's review_loop (PR/branch/
    # iteration/original_task_id), the intent-to-treat arm, and the chain lane id,
    # then defensively strip any transient lane dispatch directive so no stale
    # checkpoint SHA / lane-reserved VM directive can ride the recovery task.
    recovery_ctx: dict[str, Any] = {}
    for key in ("review_loop", LANE_PILOT_ASSIGNMENT_KEY, LANE_CHAIN_ID_KEY):
        if propagated_ctx.get(key) is not None:
            recovery_ctx[key] = propagated_ctx[key]
    strip_transient_lane_context(recovery_ctx)
    recovery_ctx[LANE_RECOVERY_REASON_KEY] = "checkpoint_missing"
    recovery_ctx[LANE_RECOVERY_ATTEMPT_KEY] = attempt

    recovery_prompt = (
        f"Lane checkpoint recovery for PR #{pr_number}. The prior phase completed "
        f"but its lane checkpoint could not be verified on branch {branch}, so the "
        f"warm lane workspace cannot be trusted. Starting from a FRESH clone of "
        f"{branch} (assume no local workspace state), re-verify the work on this "
        f"PR, address any outstanding review feedback carried in context, and push "
        f"your changes so the review re-runs against a verifiable head."
    )
    recovery_payload = validate_internal_payload(
        {
            "type": "fix",
            "pr_number": pr_number,
            "branch": branch,
            "base_branch": base_branch,
            "prompt": recovery_prompt,
            "original_task_id": resolved_original_id,
        },
        "lane_checkpoint_recovery",
    )
    # Dedup per requeue attempt: a second review-boundary entry for the same
    # missing checkpoint maps to the same key, so the active-idempotency index
    # prevents a duplicate recovery for the same attempt.
    idempotency_key = _pipeline_task_idempotency_key(
        project_id=source_task.project_id,
        task_type="lane_recovery",
        pr_number=pr_number,
        original_task_id=resolved_original_id,
        iteration=attempt,
        is_rollup=False,
    )
    recovery_task = Task(
        project_id=source_task.project_id,
        title=f"Lane checkpoint recovery PR #{pr_number}",
        description=recovery_prompt,
        parent_task_id=parent_id,
        priority=inherited_priority,
        payloads=[recovery_payload],
        context=recovery_ctx,
        idempotency_key=idempotency_key,
        vm_instance_id=None,
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(recovery_task)
    await session.flush()  # assign recovery_task.id for the lane rebind
    await rebind_lane_to_recovery(
        session, lane, task_id=recovery_task.id, reason="checkpoint_missing"
    )
    source_task.latest_status_message = (
        f"Lane checkpoint missing for PR #{pr_number} — review suppressed; "
        f"requeued failed phase as recovery task {str(recovery_task.id)[:8]} "
        f"(attempt {attempt})"
    )
    _set_spawn_reason(source_task, SPAWN_REASON_LANE_RECOVERY)
    session.add(source_task)
    session.add(
        TaskHistory(
            task_id=recovery_task.id,
            action="SPAWNED",
            details={
                "spawned_by": "REVIEW_PIPELINE",
                "type": "lane_checkpoint_recovery",
                "pr_number": pr_number,
                "attempt": attempt,
            },
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # A concurrent recovery for the same attempt already exists (idempotency
        # collision). Roll back and treat as a benign dedup — the sibling
        # recovery drives the chain.
        await session.rollback()
        logger.warning(
            "[PIPELINE] lane recovery for PR #%s hit idempotency key; "
            "sibling recovery already active",
            pr_number,
        )
        return (None, False)
    await session.refresh(recovery_task)
    await broadcast_to_project(
        str(source_task.project_id),
        "task_updated",
        {"task_id": str(recovery_task.id), "status": TaskStatus.QUEUED},
    )
    logger.info(
        "[PIPELINE] lane checkpoint recovery: requeued failed phase for PR #%s "
        "as task %s (attempt %d); cold review suppressed",
        pr_number,
        recovery_task.id,
        attempt,
    )
    return (recovery_task, True)


async def _pinned_unclaimed_sibling_exists(
    session: AsyncSession,
    *,
    project_id,
    warm_target_vm_id: str,
    pr_number: int | None,
) -> bool:
    """True iff a QUEUED (still-unclaimed) task is already pinned to
    ``warm_target_vm_id`` for the same PR.

    WI-5 anti-re-pin guard. A source VM serves exactly one task before it is
    torn down, so once one QUEUED sibling is pinned to it, minting a second
    pin is dead-on-arrival: the VM's /next-role poll claims the first sibling
    and the VM is deleted, yet the second pin's fresh 240s deadline keeps the
    orchestrator skip-provision loop orbiting an already-doomed VM. Each new
    fix/review row for the same PR minting its own 240s pin is exactly why the
    handoff deadline appears to "never expire" (new-row churn defeats the
    per-row deadline bound). Reusing the standing pin lets the existing
    deadline govern the whole set.

    Mirrors the /next-role claim query's filter shape (agent_warm_continuity):
    QUEUED + ``context->>'warm_target_vm_id'`` match, narrowed by
    ``warm_target_pr`` when a PR number is known.
    """
    use_jsonb_has_key = resolve_dialect_name(session) == "postgresql"
    filters = [
        Task.project_id == project_id,
        Task.status == TaskStatus.QUEUED,
    ]
    if use_jsonb_has_key:
        filters.append(Task.context.has_key("warm_target_vm_id"))
    filters.append(
        Task.context["warm_target_vm_id"].as_string() == str(warm_target_vm_id)
    )
    if pr_number is not None:
        if use_jsonb_has_key:
            filters.append(Task.context.has_key("warm_target_pr"))
        filters.append(Task.context["warm_target_pr"].as_string() == str(pr_number))
    stmt = select(Task.id).where(*filters).limit(1)
    return (await session.execute(stmt)).first() is not None


async def _spawn_pipeline_task(
    source_task: Task,
    task_type: str,
    session: AsyncSession,
    pr_number: int,
    branch: str,
    base_branch: str,
    original_prompt: str = "",
    fix_prompt: str = "",
    original_task_id: str = "",
    current_feedback: str = "",
    fix_reason: str = "",
    current_issues: list | None = None,
    current_resolutions: list | None = None,
    ci_failure_logs: str = "",
    investigated_files: list[str] | None = None,
    reviewed_head_sha: str = "",
    reviewed_base_sha: str = "",
    target_verified_by_backend: bool = False,
    delta_base_sha: str = "",
    review_target: dict[str, str | int] | None = None,
    fix_target: dict[str, str | int] | None = None,
    suppress_warm_handoff: bool = False,
    attempt: int = 0,
) -> tuple[Task | None, bool]:
    """Spawn a review or fix task as part of the automated review pipeline.

    Returns (task, was_created):
    - (None, False)          → spawn suppressed. Reasons:
                                 * PR already merged
                                 * iteration cap reached (per-chain or global)
                                 * stuck-cycle detected
                                 * a DIFFERENT-type task is in flight on the
                                   same chain and must finish first (e.g. a fix
                                   is running and the caller asked for a review)
                                Caller must decide follow-up (retry later, mark
                                for manual approval, etc.).
                                The specific reason is written to
                                ``source_task.context[SPAWN_REASON_KEY]`` so
                                callers can distinguish transient defers from
                                terminal caps without string-matching messages.
    - (existing_task, False) → same-type dedup hit: a review/fix of the SAME
                                task_type is already running on the same chain
                                and is still alive (has heartbeated recently).
                                Caller can trust it to finish the work.
    - (new_task, True)       → a fresh review/fix task was created. This path
                                also fires when we superseded a stale same-
                                type task, or cancelled an in-flight review
                                whose snapshot is now stale because the caller
                                requested a fix.
    """
    source_task_id = source_task.id
    # Serialize concurrent spawn attempts for the same PR. Without this, two
    # callers can both pass the SELECT in the dedup guard below before either
    # commits — each spawn has a distinct idempotency_key so the active-key
    # partial UNIQUE doesn't catch the race, and we end up with a double
    # review/fix on the same PR/chain. Lock is held until commit/rollback.
    from app.task_locking import (
        acquire_pr_spawn_lock,
    )  # local import to keep top of file lean

    # ``pg_advisory_xact_lock`` BLOCKS. Under contention it waits out Postgres'
    # bounded ``lock_timeout`` (30s — database.py:405-418) and then raises
    # LockNotAvailableError (55P03) right here. That had NO handler: the module's
    # ``_is_lock_timeout_error`` was only wired in around the spawn INSERT far
    # below, so an acquire-side timeout propagated out of the pipeline and into
    # the agent-status post-commit chain as a 500 (and left the session in
    # pending-rollback, cascading PendingRollbackError through the rest of
    # post-commit). Treat it exactly like the in-flight-peer deferral the INSERT
    # path already uses: a peer IS holding this PR's spawn lock, which means a
    # peer is already spawning for this PR — deferring is not just safe, it is
    # the correct outcome.
    try:
        await acquire_pr_spawn_lock(session, source_task.project_id, pr_number)
    except DBAPIError as _spawn_lock_exc:
        if not _is_lock_timeout_error(_spawn_lock_exc):
            raise
        await session.rollback()
        logger.warning(
            f"[PIPELINE] {task_type} spawn for PR #{pr_number} timed out acquiring "
            f"the per-PR spawn lock (a peer spawner holds it) — deferring to the "
            f"next sweep: {str(_spawn_lock_exc)[:200]}"
        )
        # The rollback expired every loaded instance (even under
        # expire_on_commit=False), so re-fetch the source by id before touching
        # it — mirrors the INSERT-side lock-timeout handler.
        #
        # NIT 9 — SCOPE OF THIS RE-FETCH. The rollback expired EVERY instance in
        # the session, not just ``source_task``; re-fetching the source alone does
        # not un-expire the rest. That is sufficient HERE, and only here, because
        # this branch is a hard early return: the sole instance touched between the
        # rollback and ``return (None, False)`` is the re-fetched source, and the
        # caller's post-commit chain re-reads what it needs from a fresh session.
        # This is a property of the branch, NOT a general licence — any future code
        # added below the rollback that touches another pre-loaded ORM instance
        # (parent, orig, a child task) MUST re-fetch it too, or it will lazy-load
        # outside greenlet context and raise MissingGreenlet. Same contract as
        # review_loop_convergence._handle_cap_exhaustion; see the rollback-expiry
        # note at the top of that function.
        _reloaded_source = await session.get(Task, source_task_id)
        if _reloaded_source is not None:
            _reloaded_source.latest_status_message = (
                f"{task_type.title()} spawn deferred for PR #{pr_number} — "
                f"another spawner holds the PR lock; retrying next sweep"
            )
            _set_spawn_reason(_reloaded_source, SPAWN_REASON_DEFERRED)
            session.add(_reloaded_source)
            await session.commit()
        return (None, False)

    from app.review_repository_scope import repository_project_ids

    _repository_project_ids = await repository_project_ids(
        session, source_task.project_id
    )

    # Collect VMs to delete when tasks are superseded or churn-halted.
    # VMs are deleted after the session commits to avoid holding the
    # transaction open during long GCP operations.
    vms_to_delete: list[tuple[str, str]] = []  # (vm_id, reason)

    # C2 lock-window fix: the scope-overflow PR signal is GitHub network I/O
    # and is deferred out of the advisory-lock-held section to a post-commit
    # fire-and-forget block (see the spawn commit near the end of this
    # function). None until a scope-overflow is detected and the signal has
    # not yet been emitted for this chain.
    _deferred_scope_overflow_signal: dict | None = None

    # Resolve the chain identifier once, so the "already merged" guard, the
    # dedup guard, and the spawn path all agree on which chain this is.
    # A chain is identified by the *create* task that opened the PR; review/fix
    # tasks in the chain carry that id in payload.original_task_id. For a source
    # task that has no explicit chain id, fall back to its own id (the source IS
    # the create task in that case).
    resolved_original_id = (
        original_task_id
        or (source_task.context or {})
        .get("review_loop", {})
        .get("original_task_id", "")
        or (source_task.first_payload).get("original_task_id", "")
        or str(source_task.id)
    )
    requested_review_target = (
        _normalized_review_target(
            review_target,
            project_id=source_task.project_id,
            pr_number=pr_number,
        )
        if task_type == "review"
        else None
    )
    requested_fix_target = (
        _normalized_review_target(
            fix_target,
            project_id=source_task.project_id,
            pr_number=pr_number,
        )
        if task_type == "fix"
        else None
    )

    # Guard: don't spawn if PR is already merged (check both original and source).
    orig_task: Task | None = None
    try:
        orig_task = await session.get(Task, uuid.UUID(resolved_original_id))
        if orig_task and (orig_task.context or {}).get("pr_merged"):
            logger.info(
                f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — PR already merged (original task {resolved_original_id[:8]})"
            )
            source_task.latest_status_message = (
                f"PR #{pr_number} already merged — skipping {task_type}"
            )
            _set_spawn_reason(source_task, SPAWN_REASON_ALREADY_MERGED)
            session.add(source_task)
            # Finalize a stranded originating task. A leaf write-task reaped
            # mid-run with an open PR is parked EVALUATING and handed a review to
            # drive it to completion (reaper.py open-PR handoff). If the PR then
            # merges out-of-band BEFORE that review runs, the review is skipped
            # here — but the originating task would otherwise sit EVALUATING
            # forever (its merge never reconciled back), wedging its rollup
            # parent. Its PR has merged (pr_merged is set), so complete it now.
            # Gated to non-rollup-chain sources: a rollup parent's
            # intermediate-vs-terminal landing is decided by
            # classify_rollup_merge_outcome elsewhere, not here.
            if (
                orig_task.status == TaskStatus.EVALUATING
                and not _is_rollup_chain_source(orig_task)
            ):
                orig_task.status = TaskStatus.COMPLETED
                orig_task.latest_status_message = (
                    f"PR #{pr_number} merged out-of-band; completed on "
                    f"already-merged review skip"
                )
                session.add(orig_task)
            # Retire the chain's lane row now that the PR has merged.
            try:
                from app.review_fix_lane_pilot import retire_chain_lane as _retire_lane

                await _retire_lane(session, source_task.context or {})
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[PIPELINE] retire_chain_lane failed (non-critical)", exc_info=True
                )
            return (None, False)
    except Exception:  # noqa: BLE001
        logger.debug(
            f"[PIPELINE] Could not verify pr_merged for original task {resolved_original_id[:8]}: skipping check"
        )
    if (source_task.context or {}).get("pr_merged"):
        logger.info(
            f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — PR already merged (source task)"
        )
        _set_spawn_reason(source_task, SPAWN_REASON_ALREADY_MERGED)
        session.add(source_task)
        # Retire the chain's lane row now that the PR has merged.
        try:
            from app.review_fix_lane_pilot import retire_chain_lane as _retire_lane

            await _retire_lane(session, source_task.context or {})
        except Exception:  # noqa: BLE001
            logger.debug(
                "[PIPELINE] retire_chain_lane failed (non-critical)", exc_info=True
            )
        return (None, False)

    # Guard: don't spawn if PR was closed without merge.
    _pr_closed = (
        orig_task is not None and (orig_task.context or {}).get("pr_closed_unmerged")
    ) or ((source_task.context or {}).get("pr_closed_unmerged"))
    if _pr_closed:
        logger.info(
            f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — PR closed without merge"
        )
        source_task.latest_status_message = (
            f"PR #{pr_number} closed without merge — skipping {task_type}"
        )
        _set_spawn_reason(source_task, SPAWN_REASON_PR_CLOSED_UNMERGED)
        session.add(source_task)
        # Retire the chain's lane row so it reaches a terminal state promptly.
        try:
            from app.review_fix_lane_pilot import retire_chain_lane as _retire_lane

            await _retire_lane(session, source_task.context or {})
        except Exception:  # noqa: BLE001
            logger.debug(
                "[PIPELINE] retire_chain_lane (pr_closed_unmerged) failed (non-critical)",
                exc_info=True,
            )
        return (None, False)

    # ── verify_tests fast-path ──────────────────────────────────────────────
    # verify_tests is a one-shot, read-only merge-gate verification: it clones
    # the PR head, runs the repo's detected test suite, and posts a
    # MERGE_TEST_RESULT verdict the no-CI merge gate consults. It is NOT a
    # review/fix iteration, so it deliberately bypasses the review-fix iteration
    # caps, review_history threading, churn / stuck-cycle detection, and warm-
    # handoff pinning the review/fix body below applies. It still runs under the
    # per-PR advisory lock acquired above and behind the already-merged guard.
    if task_type == "verify_tests":
        return await _spawn_verify_tests_task(
            source_task,
            session,
            pr_number=pr_number,
            branch=branch,
            base_branch=base_branch,
            original_task_id=resolved_original_id,
            attempt=attempt,
        )

    supersede_churn_count = _review_supersede_churn_count(orig_task)
    # Tracks whether the immediately-preceding task was a no-op fix (deliverable
    # already present, zero new commits).  Used below to extend the churn-halt
    # condition so that consecutive no-op fixes trip the halt just as stale
    # supersessions do.
    _source_noop_fix = False
    _source_is_fix = source_task.first_payload.get("type") == "fix"
    if source_task.status in (  # noqa: SIM102
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ) and task_counts_toward_review_cap(source_task):
        # Only fix-type source tasks affect the churn counter.  Review tasks
        # completing with REQUEST_CHANGES are neutral — they do not represent
        # effective progress and must NOT reset a counter that a no-op fix just
        # incremented.  Without this gate, every review→fix cycle resets the
        # counter and the halt never accumulates.
        if _source_is_fix:
            # A no-op fix (./done --already-done; deliverable already present, no new
            # commits) is NOT effective progress.  Do not reset the churn counter —
            # count it toward the halt instead, so a streak of no-op fixes terminates
            # the loop rather than keeping it alive indefinitely.
            _source_noop_fix = bool((source_task.context or {}).get("no_op_reason"))
            if _source_noop_fix:
                if orig_task is not None:
                    from sqlalchemy.orm.attributes import flag_modified

                    orig_ctx = dict(orig_task.context or {})
                    supersede_churn_count = (
                        int(orig_ctx.get(_REVIEW_SUPERSEDE_CHURN_COUNT_KEY, 0) or 0) + 1
                    )
                    orig_ctx[_REVIEW_SUPERSEDE_CHURN_COUNT_KEY] = supersede_churn_count
                    orig_task.context = orig_ctx
                    flag_modified(orig_task, "context")
                    session.add(orig_task)
                    logger.info(
                        f"[PIPELINE] No-op fix detected for PR #{pr_number} "
                        f"(no_op_reason set) — incrementing churn count to "
                        f"{supersede_churn_count} (not resetting)"
                    )
            else:
                # Fix with real commits: genuine progress, reset churn counter.
                supersede_churn_count = 0
                if orig_task is not None:
                    from sqlalchemy.orm.attributes import flag_modified

                    orig_ctx = dict(orig_task.context or {})
                    orig_ctx.pop(_REVIEW_SUPERSEDE_CHURN_LAST_KEY, None)
                    orig_ctx[_REVIEW_SUPERSEDE_CHURN_COUNT_KEY] = 0
                    orig_task.context = orig_ctx
                    flag_modified(orig_task, "context")
                    session.add(orig_task)
        # else: source is a review task — leave churn counter unchanged.

    # Dedup guard for active pipeline tasks on this PR/chain. The match axes are
    # (pr_number, chain, task_type, liveness):
    #
    # - Different chain (original_task_id) ⇒ unrelated; ignore.
    # - Same chain, same type, alive ⇒ real dedup hit; return the existing task.
    # - Same chain, any type, stale (no heartbeat for PIPELINE_TASK_STALE_MINUTES)
    #   ⇒ supersede: cancel the corpse and continue to a fresh spawn. This closes
    #   the "dying VM never posts status, caller blocked forever" path.
    # - Same chain, different type, alive:
    #     * fix requested while review in flight ⇒ cancel the review; its verdict
    #       would be against a pre-conflict / pre-CI-failure snapshot. A fresh
    #       review auto-spawns once the fix pushes.
    #     * any other cross-type combo ⇒ return (None, False) and let the caller
    #       retry on the next tick; no silent substitution.
    #
    # Rollup exception: rollup PRs aggregate subtasks whose parents each run
    # their own rollup logic. When two rollup parents target the same PR, they
    # each pass ``original_task_id=str(parent.id)`` — different chains, but the
    # SAME rollup PR. Gate dedup on PR-number only in that case so we don't
    # double-spawn review/fix for the shared PR.
    is_rollup_source = _is_rollup_chain_source(source_task)
    superseded_stale_task = False
    supersede_churn_last_snapshot: dict[str, object] | None = None
    active_pipeline = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id.in_(_repository_project_ids),
                    # Shared with _get_active_pipeline_task_by_key / verify_tests
                    # dedup so cross-type liveness can never diverge from key-level
                    # dedup. Includes BLOCKED — a same-PR fix parked on a
                    # precondition must defer a review spawn, not be invisible.
                    Task.status.in_(_PIPELINE_ACTIVE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    # R3: uncontended liveness for every candidate, fetched once. The per-PR
    # advisory lock serializes SPAWNERS only — it does not serialize against a
    # reviewer's own terminal status write, and Task.last_heartbeat goes stale on
    # a LIVE agent whose row write was 409-skipped under contention. Corroborate
    # against VmContact before believing any task is dead (mirrors reaper.py:481)
    # — AND against the runner's own active-work self-report, which defeats the
    # shared-VmContact mask on fleet runners (MAJOR 5; mirrors reaper.py:528).
    vm_contact_lookup, runner_report_lookup = await _load_pipeline_liveness_lookups(
        session, active_pipeline
    )
    for t in active_pipeline:
        if t.id == source_task.id:
            continue
        t_payload = t.first_payload
        t_type = t_payload.get("type")
        t_orig = t_payload.get("original_task_id") or ""
        if t_payload.get("pr_number") != pr_number or t_type not in ("review", "fix"):
            continue
        same_chain = (
            is_rollup_source
            or t_orig == resolved_original_id
            or t.project_id != source_task.project_id
        )
        same_target_review = (
            task_type == "review"
            and t_type == "review"
            and requested_review_target is not None
            and _review_task_matches_target(t, requested_review_target)
        )
        # Exact immutable review targets are safe to share across independent
        # chains for the same project/PR.  Other task types retain the existing
        # chain boundary so a foreign fix can never block or be cancelled by this
        # pipeline invocation.
        if not same_chain and not same_target_review:
            continue

        # A BLOCKED task is a deliberate precondition park, not a dying VM, so
        # its stale (no-heartbeat) signal is expected and must NOT supersede it —
        # otherwise a legitimately parked same-PR fix gets cancelled and the
        # cross-type defer below is skipped. The blocked-escalation machinery in
        # reaper_sweeps bounds its lifetime instead. A CI-waiting task
        # (review_loop.ci_waiting) is expected-silent for the same reason — the
        # orchestrator polls CI, not the VM — so stale-superseding it both cancels
        # live progress and feeds the supersession-churn halt (research b2231dd0 /
        # PR #10389). Exempt both; an incoming spawn DEFERS instead. See
        # _task_is_actively_ci_waiting for the full rationale + the backstop.
        if (
            t.status != TaskStatus.BLOCKED
            and not _task_is_actively_ci_waiting(t)
            and _is_pipeline_task_stale(
                t,
                vm_last_contact=vm_contact_lookup.get(t.vm_instance_id or ""),
                runner_report_lookup=runner_report_lookup,
            )
        ):
            from app.failure_classification import (
                ADMIN_CANCEL,
                persist_failure_category,
            )
            from sqlalchemy.orm.attributes import flag_modified

            # R3: re-select FOR UPDATE and re-assert the status before the
            # destructive write. Without this the CANCELLED write is an
            # UPDATE-by-PK against a stale snapshot and blind-overwrites a
            # reviewer that reached COMPLETED+APPROVE inside this window.
            _locked_t = await _relock_pipeline_task_for_cancel(session, t.id)
            if _locked_t is None:
                logger.info(
                    f"[PIPELINE] NOT superseding {t_type} task {t.id} for PR "
                    f"#{pr_number} — it went terminal (or its row is held) while we "
                    f"were deciding; it is not a corpse. Leaving it and its verdict "
                    f"intact."
                )
                continue
            t = _locked_t

            t_ctx = dict(t.context or {})
            t_ctx["failure_detail"] = "superseded_by_review_pipeline"
            t_ctx[_REVIEW_SUPERSEDE_CHURN_LAST_KEY] = {
                **_review_supersede_churn_snapshot(
                    t, task_type=t_type, pr_number=pr_number, reason="stale"
                ),
                "supersession_kind": "stale",
            }
            t.context = t_ctx
            t.status = TaskStatus.CANCELLED
            t.latest_status_message = (
                f"Superseded by new {task_type} for PR #{pr_number}: "
                f"no heartbeat for >{PIPELINE_TASK_STALE_MINUTES}min"
            )
            persist_failure_category(t, ADMIN_CANCEL)
            flag_modified(t, "context")
            session.add(t)
            superseded_stale_task = True
            # Collect VM for deletion after commit
            if t.vm_instance_id:
                vms_to_delete.append((t.vm_instance_id, "superseded_stale_task"))
            supersede_churn_last_snapshot = t_ctx[_REVIEW_SUPERSEDE_CHURN_LAST_KEY]
            if orig_task is not None:
                orig_ctx = dict(orig_task.context or {})
                supersede_churn_count = (
                    int(orig_ctx.get(_REVIEW_SUPERSEDE_CHURN_COUNT_KEY, 0) or 0) + 1
                )
                orig_ctx[_REVIEW_SUPERSEDE_CHURN_COUNT_KEY] = supersede_churn_count
                orig_ctx[_REVIEW_SUPERSEDE_CHURN_LAST_KEY] = (
                    supersede_churn_last_snapshot
                )
                orig_task.context = orig_ctx
                flag_modified(orig_task, "context")
                session.add(orig_task)
            else:
                # Fail-safe for unloadable original task: cannot track churn counter,
                # so halt the spawn to prevent unbounded supersession. The counter is
                # the only bound; we must stop when we can't record it.
                logger.warning(
                    f"[PIPELINE] Halting {task_type} spawn for PR #{pr_number} — "
                    f"cannot track supersession churn (original task unloadable). "
                    f"Superseding stale task {t.id} but cannot persist count."
                )
                await session.flush()
                source_task.latest_status_message = (
                    f"Review/fix spawn halted for PR #{pr_number}: original task "
                    f"unloadable and supersession counter cannot be persisted. "
                    f"Manual review required."
                )
                _set_spawn_reason(source_task, SPAWN_REASON_SUPERSEDE_CHURN)
                session.add(source_task)
                await session.commit()
                # Delete VMs for cancelled task
                for vm_id, reason in vms_to_delete:
                    await vm_lifecycle._ensure_vm_deleted(
                        vm_id, reason=f"review_pipeline_{reason}"
                    )
                return (None, False)
            logger.warning(
                f"[PIPELINE] Superseding stale {t_type} task {t.id} "
                f"(last_heartbeat={t.last_heartbeat}, started_at={t.started_at}) "
                f"for PR #{pr_number}"
            )
            continue

        if t_type == task_type:
            # A review is only deduplicated by its immutable Git target.  The
            # older same-chain guard remains a safe fallback for legacy callers
            # that cannot supply both SHAs, but a known different target must
            # never suppress a review of newer code.
            if (
                task_type == "review"
                and requested_review_target is not None
                and not _review_task_matches_target(t, requested_review_target)
            ):
                logger.info(
                    "[PIPELINE] Active review %s for PR #%s targets a different "
                    "base/head; not reusing it",
                    t.id,
                    pr_number,
                )
                continue
            logger.warning(
                f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — "
                f"same-chain {t_type} already active: {t.id}"
            )
            source_task.latest_status_message = (
                f"{task_type.title()} skipped for PR #{pr_number} — "
                f"{t_type} active → {str(t.id)[:8]}"
            )
            _set_spawn_reason(source_task, SPAWN_REASON_DEDUP)
            session.add(source_task)
            if task_type == "fix":
                revoke_review_authority_for_active_fix(source_task, t.id)
                session.add(source_task)
                if orig_task is not None and orig_task.id != source_task.id:
                    revoke_review_authority_for_active_fix(orig_task, t.id)
                    session.add(orig_task)
                # The active-fix row and revoked merge authority must become
                # visible together.  Returning an existing fix without this
                # commit would leave caller-specific legacy bypasses live.
                await session.commit()
            return (t, False)

        if task_type == "fix" and t_type == "review":
            from app.failure_classification import (
                ADMIN_CANCEL,
                persist_failure_category,
            )
            from sqlalchemy.orm.attributes import flag_modified

            # R3: same guarded re-lock as the stale path. A review that has ALREADY
            # committed COMPLETED+APPROVE is not "scoped to a pre-conflict
            # snapshot" — it is a landed verdict, and cancelling it here would
            # erase it (the context blob is rewritten from the stale snapshot).
            _locked_t = await _relock_pipeline_task_for_cancel(session, t.id)
            if _locked_t is None:
                logger.info(
                    f"[PIPELINE] NOT superseding review task {t.id} with a fix for PR "
                    f"#{pr_number} — it went terminal (or its row is held) while we "
                    f"were deciding; keeping its verdict."
                )
                continue
            t = _locked_t

            t_ctx = dict(t.context or {})
            t_ctx["failure_detail"] = "superseded_by_review_pipeline"
            t_ctx[_REVIEW_SUPERSEDE_CHURN_LAST_KEY] = {
                **_review_supersede_churn_snapshot(
                    t, task_type=t_type, pr_number=pr_number, reason="fix"
                ),
                "supersession_kind": "fix_supersedes_review",
            }
            t.context = t_ctx
            t.status = TaskStatus.CANCELLED
            t.latest_status_message = (
                f"Superseded by fix on PR #{pr_number}: review was scoped to "
                f"a pre-conflict / pre-CI-failure snapshot"
            )
            persist_failure_category(t, ADMIN_CANCEL)
            flag_modified(t, "context")
            session.add(t)
            superseded_stale_task = True
            # Collect VM for deletion after commit
            if t.vm_instance_id:
                vms_to_delete.append(
                    (t.vm_instance_id, "superseded_fix_replaces_review")
                )
            supersede_churn_last_snapshot = t_ctx[_REVIEW_SUPERSEDE_CHURN_LAST_KEY]
            if orig_task is not None:
                orig_ctx = dict(orig_task.context or {})
                supersede_churn_count = (
                    int(orig_ctx.get(_REVIEW_SUPERSEDE_CHURN_COUNT_KEY, 0) or 0) + 1
                )
                orig_ctx[_REVIEW_SUPERSEDE_CHURN_COUNT_KEY] = supersede_churn_count
                orig_ctx[_REVIEW_SUPERSEDE_CHURN_LAST_KEY] = (
                    supersede_churn_last_snapshot
                )
                orig_task.context = orig_ctx
                flag_modified(orig_task, "context")
                session.add(orig_task)
            else:
                # Fail-safe for unloadable original task: cannot track churn counter,
                # so halt the spawn to prevent unbounded supersession. The counter is
                # the only bound; we must stop when we can't record it.
                logger.warning(
                    f"[PIPELINE] Halting {task_type} spawn for PR #{pr_number} — "
                    f"cannot track supersession churn (original task unloadable). "
                    f"Superseding review {t.id} but cannot persist count."
                )
                await session.flush()
                source_task.latest_status_message = (
                    f"Review/fix spawn halted for PR #{pr_number}: original task "
                    f"unloadable and supersession counter cannot be persisted. "
                    f"Manual review required."
                )
                _set_spawn_reason(source_task, SPAWN_REASON_SUPERSEDE_CHURN)
                session.add(source_task)
                await session.commit()
                # Delete VMs for cancelled task
                for vm_id, reason in vms_to_delete:
                    await vm_lifecycle._ensure_vm_deleted(
                        vm_id, reason=f"review_pipeline_{reason}"
                    )
                return (None, False)
            logger.warning(
                f"[PIPELINE] Cancelling in-flight review {t.id} on PR #{pr_number} "
                f"so fix can proceed (review snapshot is stale)"
            )
            continue

        logger.info(
            f"[PIPELINE] Deferring {task_type} spawn for PR #{pr_number} — "
            f"in-flight {t_type} task {t.id} must finish first"
        )
        source_task.latest_status_message = (
            f"{task_type.title()} deferred for PR #{pr_number} — "
            f"waiting on active {t_type} task {str(t.id)[:8]}"
        )
        _set_spawn_reason(source_task, SPAWN_REASON_DEFERRED)
        session.add(source_task)
        return (None, False)

    if superseded_stale_task:
        # Release the active-idempotency partial index before inserting the
        # replacement task in the same transaction.
        await session.flush()

    churn_limit = int(
        _runtime_config.get(
            "review_supersede_churn_limit", _REVIEW_SUPERSEDE_CHURN_LIMIT
        )
        or _REVIEW_SUPERSEDE_CHURN_LIMIT
    )
    if (
        (superseded_stale_task or _source_noop_fix)
        and orig_task is not None
        and supersede_churn_count >= churn_limit
    ):
        from app.alert_dispatch import _maybe_create_escalation_alert
        from sqlalchemy.orm.attributes import flag_modified

        churn_last = supersede_churn_last_snapshot or (orig_task.context or {}).get(
            _REVIEW_SUPERSEDE_CHURN_LAST_KEY
        )
        if _source_noop_fix:
            detail = (
                "No-op fix churn reached the bound after "
                f"{supersede_churn_count} consecutive no-op fix(es) with no "
                "effective commit progress (deliverable already present)."
            )
        else:
            detail = (
                "Supersession churn reached the bound after "
                f"{supersede_churn_count} consecutive superseded task(s) with no "
                "counted run in between."
            )
            if churn_last:
                detail += f" Last superseded task: {churn_last}."
        if _runtime_config.get("require_approval_gates", True):
            orig_task.requires_approval = True
        orig_task.latest_status_message = (
            f"Review/fix supersession churn on PR #{pr_number}: "
            f"{supersede_churn_count} consecutive superseded task(s); "
            "manual review required."
        )
        _set_review_loop_terminal_outcome(
            orig_task,
            kind="supersession_churn_halt",
            reason="noop_fix_already_satisfied"
            if _source_noop_fix
            else "supersession-churn",
            pr_number=pr_number,
            iterations=supersede_churn_count,
            detail=detail,
        )
        orig_ctx = dict(orig_task.context or {})
        orig_ctx[_REVIEW_SUPERSEDE_CHURN_COUNT_KEY] = supersede_churn_count
        if churn_last is not None:
            orig_ctx[_REVIEW_SUPERSEDE_CHURN_LAST_KEY] = churn_last
        orig_task.context = orig_ctx
        flag_modified(orig_task, "context")
        session.add(orig_task)
        # Cancel any remaining in-flight review/fix tasks on this PR chain so
        # they don't fail later with "PR closed without being merged" / BRANCH_GONE.
        from app.failure_classification import (
            ADMIN_CANCEL,
            persist_failure_category,
        )

        for _inflight in active_pipeline:
            if _inflight.id == source_task.id:
                continue
            if _inflight.status not in _PIPELINE_ACTIVE_STATUSES:
                continue
            _inflight_payload = _inflight.first_payload
            _inflight_type = _inflight_payload.get("type")
            _inflight_orig = _inflight_payload.get("original_task_id") or ""
            if _inflight_payload.get("pr_number") != pr_number:
                continue
            if _inflight_type not in ("review", "fix"):
                continue
            if not is_rollup_source and _inflight_orig != resolved_original_id:
                continue
            # R3 (third site, same defect class): the `status not in
            # _PIPELINE_ACTIVE_STATUSES` check above reads the STALE in-memory
            # snapshot from the same unlocked SELECT, so it cannot see a task that
            # went COMPLETED after that SELECT — and the cancel below is the same
            # unguarded UPDATE-by-PK + whole-context rewrite. Churn-halt is a
            # deliberate kill of the loop, but killing a reviewer that has ALREADY
            # landed its verdict is still wrong (and erases it). Re-lock and
            # re-assert; a task that is no longer active needs no cancelling.
            _locked_inflight = await _relock_pipeline_task_for_cancel(
                session, _inflight.id
            )
            if _locked_inflight is None:
                logger.info(
                    f"[PIPELINE] Churn halt for PR #{pr_number}: NOT cancelling "
                    f"{_inflight_type} task {_inflight.id} — already terminal (or its "
                    f"row is held); leaving its result intact"
                )
                continue
            _inflight = _locked_inflight
            _inflight_ctx = dict(_inflight.context or {})
            _inflight_ctx["failure_detail"] = "cancelled_by_churn_halt"
            _inflight.context = _inflight_ctx
            _inflight.status = TaskStatus.CANCELLED
            _inflight.latest_status_message = (
                f"Cancelled: review/fix loop terminated (churn halt) "
                f"for PR #{pr_number}"
            )
            persist_failure_category(_inflight, ADMIN_CANCEL)
            flag_modified(_inflight, "context")
            session.add(_inflight)
            # Collect VM for deletion after commit
            if _inflight.vm_instance_id:
                vms_to_delete.append(
                    (_inflight.vm_instance_id, "cancelled_by_churn_halt")
                )
            logger.info(
                f"[PIPELINE] Cancelled in-flight {_inflight_type} task {_inflight.id} "
                f"for PR #{pr_number} (churn halt)"
            )
        try:
            await _maybe_create_escalation_alert(
                orig_task,
                session,
                pr_number=pr_number,
                reason="supersession-churn",
                iteration=supersede_churn_count,
            )
        except Exception as _ae:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Failed to create supersession churn alert for "
                f"PR #{pr_number}: {_ae}",
                exc_info=True,
            )
        try:
            import asyncio as _asyncio
            from app.notification_dispatch import dispatch_external_notification

            _asyncio.ensure_future(  # noqa: RUF006
                dispatch_external_notification(
                    orig_task.project_id,
                    "APPROVAL_NEEDED",
                    {
                        "task_id": str(orig_task.id),
                        "task_title": orig_task.title or str(orig_task.id)[:8],
                        "pr_number": pr_number,
                        "iteration": supersede_churn_count,
                        "reason": "Review/fix supersession churn",
                        "details": detail,
                        "last_superseded_task": churn_last,
                    },
                )
            )
        except Exception as _ne:  # noqa: BLE001
            logger.warning(f"[NOTIFY] APPROVAL_NEEDED dispatch setup failed: {_ne}")
        _set_spawn_reason(source_task, SPAWN_REASON_SUPERSEDE_CHURN)
        session.add(source_task)
        await session.commit()
        # Delete VMs for all cancelled tasks after commit
        for vm_id, reason in vms_to_delete:
            await vm_lifecycle._ensure_vm_deleted(
                vm_id, reason=f"review_pipeline_{reason}"
            )
        return (None, False)

    # ── No-op fix short-circuit ─────────────────────────────────────────
    # The immediately-preceding fix completed with zero net diff
    # (``./done --already-done`` / ``no_op_reason`` set). The deliverable is
    # already present on the target branch via another path, so spawning a
    # fresh review would only loop: the review would find the branch behind
    # again (same change already in base), spawn another futile fix that
    # also produces zero diff, and the cycle repeats until churn halt.
    #
    # Close the chain as converged immediately instead.  On the Nth
    # consecutive no-op the churn halt above catches it; for N < limit we
    # short-circuit here.  The churn counter is still incremented by the
    # block above for observability, but we never reach the spawn logic.
    if _source_noop_fix:
        logger.info(
            f"[PIPELINE] No-op fix for PR #{pr_number}: zero net diff after "
            f"rebase — closing review/fix chain as converged (deliverable "
            f"already satisfied)"
        )
        source_task.latest_status_message = (
            f"No-op fix — deliverable already present for PR #{pr_number}; "
            f"closing review/fix chain."
        )
        if orig_task is not None:
            _set_review_loop_terminal_outcome(
                orig_task,
                kind="converged",
                reason="noop_fix_already_satisfied",
                pr_number=pr_number,
                iterations=supersede_churn_count,
                detail=(
                    "Fix task completed with zero net diff — the deliverable "
                    "is already present on the target branch via another path. "
                    "No review needed."
                ),
            )
        _set_spawn_reason(source_task, SPAWN_REASON_SUPERSEDE_CHURN)
        session.add(source_task)
        await session.commit()
        return (None, False)

    # Mid-retry defer: when spawning a review, treat the chain as busy if a
    # same-PR fix is FAILED/CANCELLED with an infra failure_category within the
    # grace window AND still has retry budget remaining — the fix will be requeued
    # by the reaper, and a review against the unchanged head would only produce a
    # stale verdict that burns a cap slot. Outside the window or with budget
    # exhausted, proceed; the chain cannot wedge forever.
    if task_type == "review" and await _check_fix_mid_retry_defer(
        source_task, session, pr_number, resolved_original_id, is_rollup_source
    ):
        logger.info(
            f"[PIPELINE] Deferring review spawn for PR #{pr_number} — "
            f"same-PR fix is in mid-retry infra-failure grace window"
        )
        source_task.latest_status_message = (
            f"Review deferred for PR #{pr_number} — "
            f"fix in mid-retry (infra failure, within grace window)"
        )
        _set_spawn_reason(source_task, SPAWN_REASON_DEFERRED)
        session.add(source_task)
        return (None, False)

    # Per-PR iteration cap: count completed review/fix tasks for this PR across all chains.
    # Excludes archived tasks (manual iteration-cap reset path) and infra-class
    # failures: a task that died in VM provisioning / rate limit / agent timeout
    # / external API / stale-info push never actually ran a review, so counting
    # it toward the per-PR iteration cap would trip the cap with zero real
    # reviews on the PR.
    #
    # Push pr_number + type filters into SQL via the partial expression index
    # ``ix_task_payloads_pr_number`` (alembic 0057). The infra-class failure
    # filter remains Python-side (in ``task_counts_toward_review_cap``) because
    # INFRA_CLASSES is composed dynamically. ``load_only`` keeps the row narrow —
    # we only need status/context/payloads for the post-filter; status_log and
    # other JSONB columns stay deferred.
    #
    # Dialect-conditional WHERE for the per-PR cap query (audit 2026-04-29
    # round 2 / finding C2). Postgres requires byte-exact expression match
    # for ``ix_task_payloads_pr_number`` (built on
    # ``(payloads -> 0 ->> 'pr_number')`` per alembic 0057). The
    # ``.as_string() == str(pr_number)`` form compiles to
    # ``CAST(((payloads -> 0) ->> 'pr_number') AS VARCHAR) = '55'``; the
    # planner is smart enough to drop the redundant CAST AS VARCHAR (text
    # already) and matches the index — verified via EXPLAIN.
    #
    # SQLite test path: ``.as_string()`` compiles to
    # ``JSON_EXTRACT(JSON_QUOTE(JSON_EXTRACT(payloads, '$[0]')), ...)``
    # which double-quotes the int and never matches. ``func.json_extract``
    # reads the value as a primitive (typeof = 'integer' for an int
    # payload) — that's the form that works on SQLite. SQLite has no
    # expression index here (the migration creates a regular B-tree on
    # ``payloads`` for the SQLite branch), so seq-scan is acceptable for
    # tests. Postgres production path is the source of truth and gets the
    # index acceleration.
    dialect_name = resolve_dialect_name(session)
    if dialect_name == "sqlite":
        pr_number_match = (
            func.json_extract(Task.payloads, "$[0].pr_number") == pr_number
        )
        type_match = func.json_extract(Task.payloads, "$[0].type").in_(
            ("review", "fix")
        )
    else:
        pr_number_match = Task.payloads[0]["pr_number"].as_string() == str(pr_number)
        type_match = (
            Task.payloads[0]["type"].as_string().in_(("review", "fix"))
        )
    pr_pipeline_tasks = (
        (
            await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.status,
                        Task.context,
                        Task.payloads,
                        Task.archived,
                        Task.project_id,
                        Task.created_at,
                        Task.latest_status_message,
                        # status_log must be loaded eagerly: the event listener on
                        # latest_status_message setter (models.py:_on_status_message_set)
                        # reads target.status_log to append. Under async SQLAlchemy, a
                        # lazy-load of a deferred attribute during a setter event raises
                        # MissingGreenlet — see memory note "load_only Task event-listener
                        # gotcha". The repeat-fingerprint halt below mutates
                        # latest_status_message on the most-recent failed task; without
                        # this column the mutation crashes mid-cycle.
                        Task.status_log,
                        raiseload=True,
                    )
                )
                .where(
                    Task.project_id == source_task.project_id,
                    Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                    Task.archived.is_(False),
                    pr_number_match,
                    type_match,
                )
            )
        )
        .scalars()
        .all()
    )

    # ── Repeat-fingerprint halt ─────────────────────────────────────────
    # If the last two same-type FAILED tasks in this chain share an identical
    # failure fingerprint, the agent's code edits aren't changing the failure
    # shape — signature of a precondition gap (missing system lib, baseline
    # debt, toolchain mismatch). Halt: reclassify the most recent FAILED task
    # to BLOCKED_PRECONDITION (INFRA-class — won't burn cap or get auto-
    # resurrected), set requires_approval=True on the chain's original task
    # (mirrors stuck-cycle), and post a ProjectMessage so the strategic agent
    # (or operator) can decide whether to spawn a precondition-fix direction.
    #
    # This runs regardless of source_task.status (FAILED, EVALUATING-after-
    # FAILED-with-open-PR, etc.) because the FAILED → EVALUATING transition
    # in agent_status.py happens before this function is called for the
    # handoff path. The signal lives in chain history (pr_pipeline_tasks),
    # not in source_task's current status. Skip on COMPLETED source — a
    # successful task means the chain IS making progress, even if prior
    # iterations failed identically.
    if source_task.status != TaskStatus.COMPLETED:
        src_payload_type = source_task.first_payload.get("type", "")
        if src_payload_type:
            same_type_failures: list[Task] = []
            for _t in pr_pipeline_tasks:
                if _t.status != TaskStatus.FAILED:
                    continue
                if _t.first_payload.get("type", "") != src_payload_type:
                    continue
                _t_chain = (
                    (_t.context or {}).get("review_loop", {}).get("original_task_id")
                    or _t.first_payload.get("original_task_id")
                    or ""
                )
                if _t_chain != resolved_original_id:
                    continue
                same_type_failures.append(_t)
            if len(same_type_failures) >= 2:
                same_type_failures.sort(key=lambda x: x.created_at)
                a, b = same_type_failures[-2], same_type_failures[-1]
                fp_a = _failure_fingerprint_msg(a.latest_status_message)
                fp_b = _failure_fingerprint_msg(b.latest_status_message)
                if fp_a and fp_a == fp_b:
                    # Idempotency: if this halt already fired for `b` (we
                    # stamped blocked_repeat_fingerprint in a prior cycle),
                    # don't reclassify or re-post the BLOCKER message — just
                    # halt the spawn and exit. Without this guard, repeated
                    # _spawn_pipeline_task calls for the same chain (e.g.
                    # rollup retries) would create duplicate ProjectMessages.
                    already_halted = bool(
                        (b.context or {}).get("blocked_repeat_fingerprint")
                    )
                    logger.warning(
                        f"[PIPELINE] Repeat-fingerprint halt on PR #{pr_number} "
                        f"chain={resolved_original_id[:8]} type={src_payload_type}: "
                        f"prior={str(a.id)[:8]} and most-recent={str(b.id)[:8]} "
                        f"both failed with fp={fp_b} — halting spawn"
                        f"{' (already classified, skipping reclassify)' if already_halted else ', reclassifying most-recent as BLOCKED_PRECONDITION'}."
                    )
                    if not already_halted:
                        # Reclassify b (the most recent FAILED) so its row stops
                        # contributing to CODE_ERROR cap pressure and so any
                        # downstream consumer (digest, alerts, direction-health)
                        # sees the same reason as the BLOCKER message.
                        _b_ctx = dict(b.context or {})
                        _b_ctx["failure_category"] = "BLOCKED_PRECONDITION"
                        _b_ctx["blocked_repeat_fingerprint"] = fp_b
                        _b_ctx["blocked_repeat_prior_task_id"] = str(a.id)
                        b.context = _b_ctx
                        _b_msg = b.latest_status_message or ""
                        if not _b_msg.lstrip().lower().startswith("blocker:"):
                            b.latest_status_message = (
                                f"BLOCKER: identical failure to prior iteration "
                                f"({str(a.id)[:8]}, fp={fp_b}) — code-shaped fix has "
                                f"no effect, precondition gap suspected. Original: "
                                f"{_b_msg[:200]}"
                            )
                        session.add(b)
                        # Surface to operator/strategic via requires_approval
                        # on the chain's original task — mirrors stuck-cycle.
                        _orig = None
                        try:
                            _orig = await session.get(
                                Task, uuid.UUID(resolved_original_id)
                            )
                            if _orig:
                                # Discretionary gate: only approval-gate the chain
                                # original when require_approval_gates is on. With
                                # gates off, leaving the child unflagged lets the
                                # rollup respawn-cap terminalization (rollup_management
                                # ~:1019) skip it so the parent converges, instead of
                                # the 2h approval-gate timeout FAILing an EVALUATING
                                # parent. The BLOCKER status message is informational
                                # and set regardless.
                                if _runtime_config.get("require_approval_gates", True):
                                    _orig.requires_approval = True
                                _orig.latest_status_message = (
                                    f"Repeat-fingerprint halt on PR #{pr_number}: last "
                                    f"two {src_payload_type} attempts failed identically "
                                    f"(fp={fp_b}). Likely a precondition gap — manual "
                                    f"review or strategic-agent decision required."
                                )
                                _set_review_loop_terminal_outcome(
                                    _orig,
                                    kind="repeat_fingerprint_halt",
                                    reason="repeat-fingerprint",
                                    pr_number=pr_number,
                                    iterations=len(same_type_failures),
                                    detail=fp_b,
                                )
                                session.add(_orig)
                        except Exception as _orig_err:  # noqa: BLE001
                            logger.warning(
                                f"[PIPELINE] Failed to mark requires_approval on chain "
                                f"original {resolved_original_id[:8]}: {_orig_err}",
                                exc_info=True,
                            )
                        try:
                            await _post_repeat_fingerprint_blocker_message(
                                session,
                                source_task=b,
                                pr_number=pr_number,
                                fingerprint=fp_b,
                                prior_task_id=a.id,
                            )
                        except Exception as _msg_err:  # noqa: BLE001
                            logger.warning(
                                f"[PIPELINE] Failed to post repeat-fingerprint BLOCKER "
                                f"message for PR #{pr_number}: {_msg_err}",
                                exc_info=True,
                            )
                        # Durable operator Alert — fires regardless of approval
                        # gates (mirrors the stuck-cycle path). Deduplicated by
                        # (task_id, title) so re-entry does not spam the banner.
                        if _orig is not None:
                            try:
                                from app.alert_dispatch import (
                                    _maybe_create_escalation_alert,
                                )

                                await _maybe_create_escalation_alert(
                                    _orig,
                                    session,
                                    pr_number=pr_number,
                                    reason="repeat-fingerprint",
                                    iteration=len(same_type_failures),
                                )
                            except Exception as _ae:  # noqa: BLE001
                                logger.warning(
                                    f"[PIPELINE] Failed to create escalation alert "
                                    f"for repeat-fingerprint halt on PR #{pr_number}: "
                                    f"{_ae}",
                                    exc_info=True,
                                )
                    _set_spawn_reason(source_task, SPAWN_REASON_BLOCKED_REPEAT)
                    session.add(source_task)
                    return (None, False)

    # Cap-counting eligibility lives in the module-level
    # ``task_counts_toward_review_cap`` so read-only diagnostics share the exact
    # same exclusion of infra-class FAILED/CANCELLED tasks and stale-head
    # discards. See that function for the rationale (incident 2026-05-07 task
    # 78de0fdb: supersede-cancels burning cap slots at PR #4091).
    _counts_toward_cap = task_counts_toward_review_cap

    pr_iterations = sum(1 for t in pr_pipeline_tasks if _counts_toward_cap(t))

    # ── Per-lane iteration counts ───────────────────────────────────────────
    # Count existing iterations by lane (applying the same infra-class cap
    # exemption) so sub-cap checks can block mechanical churn before it crowds
    # out review-convergence work.
    lane_counts: dict[str, int] = {
        LANE_CONFLICT: 0,
        LANE_CI: 0,
        LANE_REVIEW_CONVERGENCE: 0,
    }
    for _lt in pr_pipeline_tasks:
        if _counts_toward_cap(_lt):
            _lane = classify_iteration_lane(_lt)
            lane_counts[_lane] = lane_counts.get(_lane, 0) + 1
    recent_consecutive_conflict_count = _recent_consecutive_lane_count(
        pr_pipeline_tasks,
        LANE_CONFLICT,
        _counts_toward_cap,
    )
    non_review_total = lane_counts[LANE_CONFLICT] + lane_counts[LANE_CI]

    # Runtime-config-tunable sub-caps.
    _lane_cap_conflict = _runtime_config.get(
        "review_lane_cap_conflict", LANE_CAP_CONFLICT_DEFAULT
    )
    _lane_cap_ci = _runtime_config.get("review_lane_cap_ci", LANE_CAP_CI_DEFAULT)
    _lane_cap_non_review = _runtime_config.get(
        "review_lane_cap_non_review", LANE_CAP_NON_REVIEW_DEFAULT
    )

    def _lane_breakdown_detail() -> str:
        return (
            f"conflict={lane_counts[LANE_CONFLICT]}/{_lane_cap_conflict}, "
            f"ci={lane_counts[LANE_CI]}/{_lane_cap_ci}, "
            f"non_review={non_review_total}/{_lane_cap_non_review}, "
            f"review_convergence={lane_counts[LANE_REVIEW_CONVERGENCE]}"
        )

    # Classify the upcoming spawn's lane so we can gate on the right sub-cap.
    _spawn_lane = classify_iteration_lane(source_task)
    # For the upcoming spawn, fix_reason and task_type determine the lane more
    # precisely than source_task's completed context (which may be from a prior
    # review or a different fix iteration). Override with spawn parameters.
    if fix_reason in ("merge_conflict", "update_branch_failed", "migration_guard"):
        _spawn_lane = LANE_CONFLICT
    elif fix_reason == "ci_failure":
        _spawn_lane = LANE_CI
    elif task_type in ("review", "fix") and fix_reason in ("", "code_review"):
        _spawn_lane = LANE_REVIEW_CONVERGENCE

    # Load direction data for direction-aware cap adjustment.
    direction_data: dict | None = None
    direction_id = (source_task.context or {}).get("direction_id")
    if direction_id:
        from app.models import Project

        project = await session.get(Project, source_task.project_id)
        if project:
            for d in project.directions or []:
                if d.get("id") == direction_id:
                    direction_data = d
                    break

    base_cap = _max_review_iterations(source_task, direction_data)
    # Rollup reviews accumulate iterations across retry attempts — always use the max cap
    # so transient review failures don't permanently block rollup merges. Detection uses
    # context flags set authoritatively by rollup_management when spawning review/fix tasks.
    is_rollup = _is_rollup_chain_source(source_task)
    global_cap = MAX_REVIEW_FIX_ITERATIONS if is_rollup else base_cap
    if pr_iterations >= global_cap:
        logger.warning(
            f"[PIPELINE] PR #{pr_number} hit global review-fix cap ({pr_iterations} iterations across all chains)"
        )
        await _handle_cap_exhaustion(
            source_task,
            resolved_original_id,
            pr_number,
            pr_iterations,
            session,
            scope="global",
            lane_counts=lane_counts,
        )
        _set_spawn_reason(source_task, SPAWN_REASON_CAP)
        session.add(source_task)
        return (None, False)

    # ── Persistent-conflict early escalation ───────────────────────────────
    # When the conflict-fix lane has already accumulated >= threshold completions
    # without resolving the conflict and this spawn would add another conflict
    # attempt, stop early and close with a conflict-named terminal outcome. Fires
    # BEFORE the lane sub-cap (LANE_CAP_CONFLICT_DEFAULT=4) so the PR closes at
    # attempt 3 instead of grinding to cap or global exhaustion.
    _persistent_conflict_threshold = _runtime_config.get(
        "review_persistent_conflict_threshold",
        PERSISTENT_CONFLICT_ESCALATION_THRESHOLD,
    )
    if (
        _spawn_lane == LANE_CONFLICT
        and recent_consecutive_conflict_count >= _persistent_conflict_threshold
    ):
        logger.warning(
            f"[PIPELINE] PR #{pr_number} persistent merge conflict: "
            f"{recent_consecutive_conflict_count} consecutive conflict-fix "
            f"iterations completed without resolution "
            f"(threshold={_persistent_conflict_threshold})"
        )
        await _handle_persistent_conflict_escalation(
            source_task,
            resolved_original_id,
            pr_number,
            pr_iterations,
            recent_consecutive_conflict_count,
            session,
        )
        _set_spawn_reason(source_task, SPAWN_REASON_PERSISTENT_CONFLICT)
        session.add(source_task)
        return (None, False)

    # ── Per-lane sub-cap checks ─────────────────────────────────────────────
    # Block a spawn when its lane's sub-cap is exhausted or non-review lanes
    # collectively hit the collective ceiling. These fire before the global cap
    # so mechanical churn never starves review-convergence work.
    # Conflict/CI exhaustion routes to _handle_cap_exhaustion, which uses
    # _pr_is_preservable_on_cap to preserve rollup work.
    _lane_exhausted: str | None = None
    if (
        _spawn_lane == LANE_CONFLICT
        and lane_counts[LANE_CONFLICT] >= _lane_cap_conflict
    ):
        _lane_exhausted = LANE_CONFLICT
    elif _spawn_lane == LANE_CI and lane_counts[LANE_CI] >= _lane_cap_ci:
        _lane_exhausted = LANE_CI
    elif (
        _spawn_lane != LANE_REVIEW_CONVERGENCE
        and non_review_total >= _lane_cap_non_review
    ):
        _lane_exhausted = "non_review_collective"

    if _lane_exhausted is not None:
        _detail = _lane_breakdown_detail()
        # Conflict or CI lane exhausted (or non-review collective cap) →
        # route to _handle_cap_exhaustion. For rollup PRs,
        # _pr_is_preservable_on_cap returns True → preserve work.
        _scope_label = (
            f"{_lane_exhausted}-lane"
            if _lane_exhausted in (LANE_CONFLICT, LANE_CI)
            else "non-review-collective"
        )
        logger.warning(
            f"[PIPELINE] PR #{pr_number} {_scope_label} lane cap exhausted. "
            f"Lane breakdown: {_detail}"
        )
        await _handle_cap_exhaustion(
            source_task,
            resolved_original_id,
            pr_number,
            pr_iterations,
            session,
            scope=_scope_label,
            lane_counts=lane_counts,
        )
        _set_spawn_reason(source_task, SPAWN_REASON_CAP)
        session.add(source_task)
        return (None, False)

    # Track review-fix iteration count to prevent infinite loops (per-chain).
    # Rollup PRs use the same elevated cap as the global check above — without
    # this, a 12-iteration medium-scope cap fires on rollup chains even though
    # the global path was bumped to MAX_REVIEW_FIX_ITERATIONS. Incident
    # 2026-05-07 task 78de0fdb hit per-chain cap on rollup PR #4091 with simple
    # fixable issues (duplicate vi.mock keys) because the per-chain cap had
    # not been bumped in step with the global cap.
    source_loop = (source_task.context or {}).get("review_loop", {})
    if task_type == "fix" and not reviewed_head_sha:
        # CI, conflict, and migration-guard fixes are commonly spawned after
        # approval, from a parked source task rather than directly from the
        # review verdict. Preserve that source's pre-fix head centrally so the
        # mandatory follow-up can remain a cheap exact-SHA delta review instead
        # of silently expanding to a full-PR review. Explicit verdict provenance
        # wins; older source rows fall back through their live head and pinned
        # immutable review target.
        source_review_target = source_loop.get("review_target")
        reviewed_head_sha = str(
            source_loop.get("reviewed_head_sha")
            or source_loop.get("head_sha")
            or (
                source_review_target.get("head_sha")
                if isinstance(source_review_target, dict)
                else ""
            )
            or ""
        )
    cached_verdict_json = ""
    cached_review_task_id = ""
    if task_type == "review" and requested_review_target is not None:
        # Completed-result reuse deliberately creates a normal review task with
        # a cached structured verdict instead of returning the old terminal row.
        # The normal completion path must still run to apply CI/fix lifecycle
        # effects to this source task. The review agent validates this payload
        # before short-circuiting its model call.
        # Review targets also suffix review idempotency keys.  A LIKE suffix
        # predicate is portable across SQLite/PostgreSQL and avoids an
        # unbounded project-wide JSON-context scan, while final Python checks
        # below retain the full structured-result validation.
        target_key_suffix = (
            f":{requested_review_target['base_sha']}:"
            f"{requested_review_target['head_sha']}"
        )
        completed_candidates = await session.stream(
            select(Task)
            .where(
                Task.project_id == source_task.project_id,
                Task.status == TaskStatus.COMPLETED,
                Task.idempotency_key.like(f"%{target_key_suffix}"),
            )
            .order_by(Task.created_at.desc())
        )
        async for candidate in completed_candidates.scalars():
            if (candidate.first_payload or {}).get("type") != "review":
                continue
            if not _review_task_matches_target(candidate, requested_review_target):
                continue
            result = (candidate.context or {}).get("review_result")
            if not isinstance(result, dict) or result.get("structured") is not True:
                continue
            if result.get("verdict") not in ("APPROVE", "REQUEST_CHANGES"):
                continue
            raw_verdict = result.get("verdict_json")
            if not isinstance(raw_verdict, str) or not raw_verdict:
                continue
            try:
                cached_verdict = json.loads(raw_verdict)
            except (TypeError, ValueError):
                continue
            if not isinstance(cached_verdict, dict):
                continue
            if (
                cached_verdict.get("reviewed_base_sha")
                != requested_review_target["base_sha"]
                or cached_verdict.get("reviewed_head_sha")
                != requested_review_target["head_sha"]
            ):
                continue
            cached_verdict_json = raw_verdict
            cached_review_task_id = str(candidate.id)
            logger.info(
                "[PIPELINE] Replaying structured review %s for identical target "
                "on PR #%s through a fresh lifecycle task",
                candidate.id,
                pr_number,
            )
            break
    iteration = source_loop.get("iteration", 0) + 1
    per_chain_cap = MAX_REVIEW_FIX_ITERATIONS if is_rollup else base_cap

    if iteration > per_chain_cap:
        logger.warning(
            f"[PIPELINE] Review-fix cycle limit reached for PR #{pr_number} after {iteration - 1} iterations"
        )
        await _handle_cap_exhaustion(
            source_task,
            resolved_original_id,
            pr_number,
            per_chain_cap,
            session,
            scope="per-chain",
            lane_counts=lane_counts,
        )
        _set_spawn_reason(source_task, SPAWN_REASON_CAP)
        session.add(source_task)
        return (None, False)

    idempotency_key = _pipeline_task_idempotency_key(
        project_id=source_task.project_id,
        task_type=task_type,
        pr_number=pr_number,
        original_task_id=resolved_original_id,
        iteration=iteration,
        is_rollup=is_rollup,
        review_target=requested_review_target,
    )
    existing_by_key = await _get_active_pipeline_task_by_key(session, idempotency_key)
    if existing_by_key is not None:
        logger.warning(
            f"[PIPELINE] Reusing idempotent {task_type} spawn for PR #{pr_number}: "
            f"{existing_by_key.id}"
        )
        source_task.latest_status_message = (
            f"{task_type.title()} skipped for PR #{pr_number} — "
            f"idempotent spawn already active → {str(existing_by_key.id)[:8]}"
        )
        _set_spawn_reason(source_task, SPAWN_REASON_DEDUP)
        session.add(source_task)
        return (existing_by_key, False)

    vm_name = f"agent-vm-{uuid.uuid4().hex}"

    # Determine parent: use the original task's parent (so review/fix are siblings of the
    # create task under the plan/research parent), falling back to original task itself.
    # resolved_original_id was computed at the top of this function.
    parent_id = None
    inherited_priority = source_task.priority
    orig: Task | None = None
    try:
        orig = await session.get(Task, uuid.UUID(resolved_original_id))
        if orig and orig.parent_task_id:
            parent_id = orig.parent_task_id
        elif orig:
            parent_id = orig.id
        if orig:
            inherited_priority = orig.priority
    except Exception:  # noqa: BLE001
        logger.debug(
            f"[PIPELINE] Could not inherit properties from original task {resolved_original_id[:8] if resolved_original_id else '?'}"
        )

    # Propagate hotfix marker from the original task's payload.
    _orig_payload = (
        orig.first_payload if orig is not None else source_task.first_payload
    ) or {}
    orig_hotfix = bool(_orig_payload.get("hotfix"))

    # Apply per-task-type model/effort defaults from settings
    type_model = _runtime_config.get("task_type_models", {}).get(task_type) or None
    type_effort = _runtime_config.get("task_type_efforts", {}).get(task_type) or None

    # Reviewer-model pin: once the first review in a chain uses a specific model,
    # all subsequent reviews on the same chain reuse it. Prevents model-rotation
    # from introducing reviewer personality drift mid-chain. Gated on the
    # "pin_reviewer_model" runtime flag (default on). Carry-forward always happens
    # (fix spawns thread it through review_loop so the next review sees it).
    _pin_reviewer = bool(_runtime_config.get("pin_reviewer_model", True))
    reviewer_model_for_chain = (
        source_loop.get("reviewer_model") if _pin_reviewer else None
    )
    if task_type == "review" and _pin_reviewer:
        if reviewer_model_for_chain:
            # Use the pinned model from a prior review in this chain.
            type_model = reviewer_model_for_chain
        elif type_model:
            # First review in the chain — stamp the resolved type_model as the pin.
            reviewer_model_for_chain = type_model

    # Forward viewport screenshots captured by the implementer so both
    # reviewers and fix agents can visually inspect before/after/diff PNGs.
    _impl_shots: list = (
        (orig.context or {}).get("viewport_screenshots", []) if orig is not None else []
    )

    if task_type == "review":
        title = f"Review PR #{pr_number}"
        payload_data = {
            "type": "review",
            "pr_number": pr_number,
            "branch": branch,
            "base_branch": base_branch,
            "original_prompt": original_prompt,
            "original_task_id": resolved_original_id,
            "original_task_type": source_task.first_payload.get("type", ""),
            **(
                {
                    "delta_base_sha": delta_base_sha,
                }
                if delta_base_sha
                else {}
            ),
            **(
                {
                    "cached_verdict_json": cached_verdict_json,
                    "cached_review_task_id": cached_review_task_id,
                }
                if cached_verdict_json and cached_review_task_id
                else {}
            ),
            **({"hotfix": True} if orig_hotfix else {}),
            **({"model": type_model} if type_model else {}),
            **({"effort": type_effort} if type_effort else {}),
            **(
                {
                    "viewport_shot_entries": _impl_shots,
                    "viewport_source_task_id": resolved_original_id,
                }
                if _impl_shots
                else {}
            ),
        }
    else:
        title = f"Fix PR #{pr_number}"
        # Prior review feedback is carried via review_loop.review_history in the
        # task context, which the fix agent formats with severity tags and file/line
        # locations. Avoid duplicating it here as unstructured text.
        payload_data = {
            "type": "fix",
            "pr_number": pr_number,
            "branch": branch,
            "base_branch": base_branch,
            "prompt": fix_prompt,
            "original_prompt": original_prompt,
            "original_task_id": resolved_original_id,
            **({"hotfix": True} if orig_hotfix else {}),
            **({"model": type_model} if type_model else {}),
            **({"effort": type_effort} if type_effort else {}),
            **(
                {
                    "viewport_shot_entries": _impl_shots,
                    "viewport_source_task_id": resolved_original_id,
                }
                if _impl_shots
                else {}
            ),
        }

    # Build review_history: carry forward from source task, appending current feedback if spawning a fix
    prev_review_history = source_loop.get("review_history", [])
    # The PR's actual changed files at this iteration boundary, used by drift
    # detection. The review task itself doesn't push commits and so won't have
    # _pr_changed_files in its own context — fall back to the original task
    # (which is the create/fix that pushed the commits being reviewed).
    _source_changed_files = (
        (source_task.context or {}).get("_pr_changed_files")
        or ((orig.context or {}).get("_pr_changed_files") if orig else None)
        or []
    )
    _normalized_current_issues = ensure_issue_ids(
        current_issues,
        reviewed_head_sha=reviewed_head_sha,
        iteration=iteration - 1,
    )
    new_review_entry: dict[str, object] = {
        "iteration": iteration - 1,
        "feedback": current_feedback,
        # Gate: this branch fires when spawning a fix from REQUEST_CHANGES.
        # APPROVE iterations are written separately at the CI-wait transition
        # in task_lifecycle.py so the trajectory shows them too.
        "verdict": "REQUEST_CHANGES",
        "changed_files": list(_source_changed_files)[:30],
        **(
            {"reviewed_head_sha": reviewed_head_sha}
            if reviewed_head_sha
            else {}
        ),
        **(
            {"reviewed_base_sha": reviewed_base_sha}
            if reviewed_base_sha
            else {}
        ),
        **(
            {
                "target_verified_by_backend": True,
                "review_task_id": str(source_task.id),
            }
            if target_verified_by_backend
            and reviewed_head_sha
            and reviewed_base_sha
            else {}
        ),
        **(
            {"delta_base_sha": source_loop.get("delta_base_sha")}
            if source_loop.get("delta_base_sha")
            else {}
        ),
        **(
            {"resolutions": list(current_resolutions or [])[:30]}
            if current_resolutions
            else {}
        ),
    }
    if _normalized_current_issues:
        new_review_entry["issues"] = _normalized_current_issues
    # Bound each entry's freeform feedback so the persisted row can never be fat,
    # no matter how large the underlying agent log is. Applied to the whole list —
    # the freshly built entry AND any carried-forward prior entries — BEFORE the
    # stuck-cycle similarity check below, so the Jaccard compares consistently
    # bounded prefixes. Uses bound_review_history (REVIEW_FEEDBACK_MAX_CHARS + marker)
    # rather than the legacy _cap_review_history_feedback so the truncation marker is
    # present on truncated entries; the bound_review_history call at the persistence
    # chokepoint below becomes a no-op because the list is already bounded here.
    has_current_review_result = task_type == "fix" and bool(
        current_feedback or _normalized_current_issues or current_resolutions
    )
    new_review_history = bound_review_history(
        [*prev_review_history, new_review_entry]
        if has_current_review_result
        else prev_review_history
    )

    churn_detections = int(source_loop.get("churn_detections", 0) or 0)
    # Carry forward any prior churn cap reduction so it survives across iterations.
    churn_cap_reduction: int | None = (source_task.context or {}).get(
        "_churn_cap_reduction"
    )
    # Stuck-cycle detection: if the same CRITICAL/MAJOR issues repeat across consecutive
    # iterations, escalate for manual review instead of consuming the full iteration budget.
    if task_type == "fix" and iteration >= 2 and len(new_review_history) >= 2:
        prev_entry = new_review_history[-2]
        curr_entry = new_review_history[-1]
        prev_issues_list = prev_entry.get("issues") or []
        curr_issues_list = curr_entry.get("issues") or []
        prev_critical_major = {
            i.get("description", "").lower().strip()
            for i in prev_issues_list
            if i.get("severity", "").upper() in ("CRITICAL", "MAJOR")
            and i.get("description", "").strip()
        }
        curr_critical_major = [
            i.get("description", "").lower().strip()
            for i in curr_issues_list
            if i.get("severity", "").upper() in ("CRITICAL", "MAJOR")
            and i.get("description", "").strip()
        ]
        # Refactor tasks get a higher threshold — structural review feedback often repeats
        # because the fix agent cannot fully resolve architectural concerns in scope
        source_type = (source_task.first_payload or {}).get("type", "")
        is_refactor = source_type == "refactor"
        source_scope = (source_task.context or {}).get("scope", "medium")
        stuck_threshold, issue_threshold, text_threshold = _stuck_thresholds(
            source_scope, is_refactor
        )

        is_stuck = False
        stuck_detail = ""

        if curr_critical_major and prev_critical_major:
            matched_count = sum(
                1 for desc in curr_critical_major if desc in prev_critical_major
            )
            total_count = len(curr_critical_major)
            if matched_count / total_count >= stuck_threshold:
                is_stuck = True
                stuck_detail = f"{matched_count}/{total_count} issues repeated"
            else:
                # Primary uses normalized exact equality, so it misses pure
                # rephrasings ("Missing null check in handler" ↔ "Handler lacks
                # null check"). Fall back to a Jaccard on the *concatenated*
                # CRITICAL/MAJOR descriptions: token overlap aggregated across
                # all issues distinguishes genuine rephrasings (high overall
                # overlap) from issues that only share boilerplate but target
                # different symbols (processUser vs processOrder — distinct
                # target tokens dilute aggregate overlap below the 0.65 floor).
                similarity = _feedback_jaccard(
                    " ".join(prev_critical_major), " ".join(curr_critical_major)
                )
                if similarity >= issue_threshold:
                    is_stuck = True
                    stuck_detail = (
                        f"rephrased-issue similarity {similarity:.0%} "
                        f"≥ {issue_threshold:.0%} "
                        f"(exact match {matched_count}/{total_count})"
                    )
        elif not prev_issues_list or not curr_issues_list:
            # Fallback when at least one side lacks structured issues (verdict parsing
            # fell back to log/keyword scraping). Compare normalized feedback text.
            prev_feedback = (prev_entry.get("feedback") or "").strip()
            curr_feedback = (curr_entry.get("feedback") or "").strip()
            if prev_feedback and curr_feedback:
                similarity = _feedback_jaccard(prev_feedback, curr_feedback)
                if similarity >= text_threshold:
                    is_stuck = True
                    stuck_detail = (
                        f"feedback similarity {similarity:.0%} ≥ {text_threshold:.0%}"
                    )

        # Standoff detection: distinct from generic stuck. When the prior fix
        # iteration submitted structured rationale for an issue AND the current
        # reviewer re-raised the same issue anyway, that's a genuine
        # disagreement — operator should adjudicate rather than burn another
        # cycle on a topic two agents already disagree about.  Counts only
        # responses whose verify steps are mechanical (verify_steps_ok at the
        # agent side; backend trusts what landed but applies the same check).
        is_standoff = False
        standoff_detail = ""
        if curr_critical_major and prev_critical_major:
            prior_responses = list(source_loop.get("fix_agent_responses") or [])
            # Collect copied stable BP issue ids when present; retain the
            # issue-summary fallback so older structured responses still count.
            standoff_issue_ids: set[str] = set()
            for r in prior_responses:
                if not isinstance(r, dict):
                    continue
                rid = r.get("issue_id")
                if isinstance(rid, str) and rid:
                    standoff_issue_ids.add(rid)
            # Re-raised description is in curr_critical_major (already lowered+stripped).
            # If any prior response targets one of those issues, the agent disputed
            # but the reviewer rejected — standoff.
            re_raised_with_response = 0
            for desc in curr_critical_major:
                # Standoff currently requires at least one structured response
                # carrying an id plus the same normalized description recurring.
                if standoff_issue_ids and desc in prev_critical_major:
                    re_raised_with_response += 1
            if re_raised_with_response > 0 and standoff_issue_ids:
                is_standoff = True
                standoff_detail = (
                    f"{re_raised_with_response} issue(s) re-raised after fix-agent rationale; "
                    f"reviewer rejected the rationale and re-flagged"
                )

        if is_standoff:
            logger.warning(
                f"[PIPELINE] Standoff detected for PR #{pr_number}: {standoff_detail}"
            )
            # Treat the same as is_stuck — escalate to operator. Differs from
            # stuck only in WHY (genuine disagreement vs. agent not trying).
            is_stuck = True
            stuck_detail = standoff_detail or stuck_detail

        if is_stuck:
            logger.warning(
                f"[PIPELINE] Stuck cycle detected for PR #{pr_number}: {stuck_detail}"
            )
            # resolved_original_id is always non-empty (falls back to str(source_task.id))
            try:
                stuck_orig = await session.get(Task, uuid.UUID(resolved_original_id))
                if stuck_orig:
                    # Discretionary gate (see repeat-fingerprint path above): only
                    # approval-gate when require_approval_gates is on; with gates off
                    # the rollup respawn-cap terminalization converges the parent.
                    if _runtime_config.get("require_approval_gates", True):
                        stuck_orig.requires_approval = True
                    stuck_orig.latest_status_message = (
                        f"Review-fix cycle appears stuck — same issues repeated across "
                        f"{iteration} iterations. Manual review required."
                    )
                    _set_review_loop_terminal_outcome(
                        stuck_orig,
                        kind="standoff" if is_standoff else "stuck",
                        reason="standoff" if is_standoff else "stuck",
                        pr_number=pr_number,
                        iterations=iteration,
                        detail=stuck_detail or None,
                    )
                    session.add(stuck_orig)
                    await session.commit()
                    # Durable operator Alert — fires regardless of approval-gate
                    # state (the requires_approval flag above is discretionary,
                    # but the escalation must always surface in the banner).
                    try:
                        from app.alert_dispatch import (
                            _maybe_create_escalation_alert,
                        )

                        await _maybe_create_escalation_alert(
                            stuck_orig,
                            session,
                            pr_number=pr_number,
                            reason="standoff" if is_standoff else "stuck",
                            iteration=iteration,
                        )
                    except Exception as _ae:  # noqa: BLE001
                        logger.warning(
                            f"[PIPELINE] Failed to create escalation alert for "
                            f"stuck cycle on PR #{pr_number}: {_ae}",
                            exc_info=True,
                        )
                    try:
                        import asyncio as _asyncio
                        from app.notification_dispatch import (
                            dispatch_external_notification,
                        )

                        _asyncio.ensure_future(  # noqa: RUF006
                            dispatch_external_notification(
                                stuck_orig.project_id,
                                "APPROVAL_NEEDED",
                                {
                                    "task_id": str(stuck_orig.id),
                                    "task_title": stuck_orig.title
                                    or str(stuck_orig.id)[:8],
                                    "pr_number": pr_number,
                                    "iteration": iteration,
                                    "reason": "Review-fix cycle stuck — manual review required",
                                },
                            )
                        )
                    except Exception as _ne:  # noqa: BLE001
                        logger.warning(
                            f"[NOTIFY] APPROVAL_NEEDED dispatch setup failed: {_ne}"
                        )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[PIPELINE] Failed to mark stuck cycle on {resolved_original_id}: {e}",
                    exc_info=True,
                )
            _set_spawn_reason(source_task, SPAWN_REASON_STUCK)
            session.add(source_task)
            return (None, False)

        # Early non-convergence escalation: catches the whack-a-mole / drift
        # shape the stuck-cycle, standoff, and churn detectors all MISS — a
        # shrinking-but-never-zero issue count, verification-only drift, or
        # one-at-a-time disclosure. The existing detectors require the SAME
        # issue re-raised (stuck/standoff) or NON-decreasing counts (churn), so
        # a sequence like [3, 2, 2, 1, 1] grinds all the way to the cap. We
        # escalate to the operator at iteration K instead of burning the full
        # budget. Only fires from iteration K onward so a normally-converging
        # loop (which lands APPROVE well before K) is never escalated early.
        if task_type == "fix" and iteration >= EARLY_NONCONVERGENCE_ESCALATE_AT:
            should_escalate, nonconvergence_reason = detect_early_nonconvergence(
                new_review_history
            )
            if should_escalate:
                logger.warning(
                    f"[PIPELINE] Early non-convergence for PR #{pr_number} at "
                    f"iteration {iteration} (cap {per_chain_cap}): "
                    f"{nonconvergence_reason}. Escalating to operator instead of "
                    f"grinding to cap."
                )
                try:
                    nc_orig = await session.get(Task, uuid.UUID(resolved_original_id))
                    if nc_orig:
                        # Discretionary gate, same as stuck-cycle: only
                        # approval-gate when require_approval_gates is on.
                        if _runtime_config.get("require_approval_gates", True):
                            nc_orig.requires_approval = True
                        nc_orig.latest_status_message = (
                            f"Review-fix loop not converging "
                            f"({nonconvergence_reason}) after {iteration} "
                            f"iterations — escalated for manual review at "
                            f"iteration {iteration} rather than grinding to cap."
                        )
                        session.add(nc_orig)
                        await session.commit()
                        try:
                            import asyncio as _asyncio
                            from app.notification_dispatch import (
                                dispatch_external_notification,
                            )

                            _asyncio.ensure_future(  # noqa: RUF006
                                dispatch_external_notification(
                                    nc_orig.project_id,
                                    "APPROVAL_NEEDED",
                                    {
                                        "task_id": str(nc_orig.id),
                                        "task_title": nc_orig.title
                                        or str(nc_orig.id)[:8],
                                        "pr_number": pr_number,
                                        "iteration": iteration,
                                        "reason": (
                                            "Review-fix loop not converging — "
                                            f"{nonconvergence_reason}"
                                        ),
                                    },
                                )
                            )
                        except Exception as _ne:  # noqa: BLE001
                            logger.warning(
                                f"[NOTIFY] APPROVAL_NEEDED dispatch setup failed: {_ne}"
                            )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[PIPELINE] Failed to escalate non-convergence on "
                        f"{resolved_original_id}: {e}",
                        exc_info=True,
                    )
                _set_spawn_reason(source_task, SPAWN_REASON_NONCONVERGENCE)
                session.add(source_task)
                return (None, False)

        # Issue-churn detection: complements stuck-cycle above. If total
        # CRITICAL+MAJOR count fails to decrease across the last
        # ISSUE_CHURN_WINDOW iterations (even though the specific issues are
        # rotating and so evading Jaccard stuck detection), reduce the
        # remaining iteration cap by ~1/3 and instruct the fix agent to
        # narrow focus. The stuck-cycle detector handles harsher intervention.
        is_churn, churn_counts = _detect_issue_churn(new_review_history)
        if is_churn and task_type == "fix":
            churn_detections += 1
            remaining = max(per_chain_cap - iteration, 0)
            reduced_cap = iteration + (remaining // 2)
            if reduced_cap < per_chain_cap:
                logger.warning(
                    f"[PIPELINE] Issue churn detected for PR #{pr_number} "
                    f"(detection #{churn_detections}): "
                    f"CRITICAL+MAJOR counts {churn_counts} non-decreasing over "
                    f"{ISSUE_CHURN_WINDOW} iterations. Reducing cap "
                    f"{per_chain_cap} → {reduced_cap}; injecting narrow-focus directive."
                )
                per_chain_cap = reduced_cap
                churn_cap_reduction = reduced_cap
            else:
                logger.warning(
                    f"[PIPELINE] Issue churn detected for PR #{pr_number} "
                    f"(detection #{churn_detections}): "
                    f"counts {churn_counts}; injecting narrow-focus directive."
                )
            narrow_prefix = _CHURN_NARROW_FOCUS_PREFIX.format(
                window=ISSUE_CHURN_WINDOW, counts=churn_counts
            )
            narrow_prefix += _format_repeating_issues_section(new_review_history)
            narrow_prefix = _reconcile_narrow_focus_with_fix_all(
                narrow_prefix, fix_prompt
            )
            payload_data["prompt"] = narrow_prefix + (payload_data.get("prompt") or "")
            fix_prompt = narrow_prefix + (fix_prompt or "")
        elif (
            task_type == "fix"
            and per_chain_cap > 0
            and (iteration >= MID_BUDGET_NARROW_FOCUS_THRESHOLD * per_chain_cap)
        ):
            # Proactive mid-budget directive: softer wording, no cap reduction.
            mid_prefix = _MID_BUDGET_NARROW_FOCUS_PREFIX.format(
                iteration=iteration, cap=per_chain_cap
            )
            mid_prefix += _format_repeating_issues_section(new_review_history)
            mid_prefix = _reconcile_narrow_focus_with_fix_all(mid_prefix, fix_prompt)
            logger.info(
                f"[PIPELINE] Mid-budget narrow-focus directive for PR #{pr_number} "
                f"(iteration {iteration}/{per_chain_cap})."
            )
            payload_data["prompt"] = mid_prefix + (payload_data.get("prompt") or "")
            fix_prompt = mid_prefix + (fix_prompt or "")
        elif task_type == "fix" and len(new_review_history) >= 2:
            # Early visibility: from iteration 2 onward, surface any repeating
            # CRITICAL/MAJOR issues without the churn/mid-budget urgency signals.
            repeating_section = _format_repeating_issues_section(new_review_history)
            if repeating_section:
                early_prefix = _EARLY_REPEATING_ISSUES_PREFIX + repeating_section
                payload_data["prompt"] = early_prefix + (
                    payload_data.get("prompt") or ""
                )
                fix_prompt = early_prefix + (fix_prompt or "")

    # Advance the durable complete ledger from only the newly observed review
    # row. Replaying the bounded 50-row history would lose blockers whose demand
    # row was compacted before a later omission; the prior ledger is the compact
    # carry-forward state for every unresolved demand.
    _issue_ledger: dict = {}
    if source_loop.get("issue_ledger") is not None:
        _issue_ledger = build_issue_ledger(
            new_review_history,
            seed_ledger=source_loop.get("issue_ledger"),
            stamp_complete=True,
        )
    elif new_review_history:
        _issue_ledger = build_issue_ledger(
            new_review_history,
            stamp_complete=True,
        )

    # Graduated scope-budget pressure cap: a medium- or large-scope fix cycle's
    # remaining iterations are scaled down by how close the PR is to its
    # production file budget. Activation is at SCOPE_PRESSURE_WARN_THRESHOLD
    # (70%); the no-new-files prompt still only fires at
    # SCOPE_PRESSURE_HARD_CAP_PERCENTAGE (90%). The derived cap persists via
    # the spawned task's context so subsequent spawns respect the ceiling.
    scope_budget_hard_cap: int | None = None
    if task_type == "fix":
        _scope_for_pressure = (source_task.context or {}).get(
            "scope", source_loop.get("scope", "medium")
        )
        _changed_for_pressure = (
            (source_task.context or {}).get("_pr_changed_files")
            or ((orig.context or {}).get("_pr_changed_files") if orig else None)
            or []
        )
        _util_for_pressure = _compute_scope_budget_utilization(
            _changed_for_pressure, _scope_for_pressure
        )
        if (
            _util_for_pressure
            and _scope_for_pressure in ("medium", "large")
            and _util_for_pressure.get("percentage", 0)
            >= SCOPE_PRESSURE_WARN_THRESHOLD * 100
        ):
            util_pct = _util_for_pressure["percentage"]
            base_remaining = per_chain_cap - iteration
            if base_remaining > 0:
                # Use a slightly higher hard cap floor for large scope to avoid
                # slamming to 2 remaining iterations on a near-finished large refactor.
                hard_cap_floor = (
                    SCOPE_PRESSURE_HARD_CAP_REMAINING_LARGE
                    if _scope_for_pressure == "large"
                    else SCOPE_PRESSURE_HARD_CAP_REMAINING
                )
                graduated_remaining = max(
                    hard_cap_floor,
                    round(base_remaining * (1.0 - util_pct / 100)),
                )
                hard_cap_target = iteration + graduated_remaining
                if hard_cap_target < per_chain_cap:
                    logger.warning(
                        f"[PIPELINE] Scope-budget pressure for PR #{pr_number}: "
                        f"utilization {util_pct}% ≥ "
                        f"{round(SCOPE_PRESSURE_WARN_THRESHOLD * 100)}% (non-test "
                        f"{_util_for_pressure['non_test_file_count']}/"
                        f"{_util_for_pressure['budget']}). Reducing cap "
                        f"{per_chain_cap} → {hard_cap_target} "
                        f"(graduated remaining={graduated_remaining})."
                    )
                    per_chain_cap = hard_cap_target
                scope_budget_hard_cap = hard_cap_target
            if util_pct >= SCOPE_PRESSURE_HARD_CAP_PERCENTAGE:
                hard_prefix = _SCOPE_PRESSURE_HARD_CAP_PREFIX.format(
                    count=_util_for_pressure["non_test_file_count"],
                    budget=_util_for_pressure["budget"],
                    pct=util_pct,
                )
                hard_prefix = _reconcile_narrow_focus_with_fix_all(
                    hard_prefix, fix_prompt
                )
                payload_data["prompt"] = hard_prefix + (
                    payload_data.get("prompt") or ""
                )
                fix_prompt = hard_prefix + (fix_prompt or "")

    payload_data = validate_internal_payload(
        payload_data, f"review_pipeline:{task_type}:pr#{pr_number}"
    )
    source_ctx = source_task.context or {}
    propagated_scope = source_ctx.get("scope", source_loop.get("scope", "medium"))
    # Carry strategic context (direction_id, related_files, parent_context, scope, …)
    # from source task so fix/review agents see the same direction/intent the create
    # task had. Skip underscore-prefixed internal flags (per-task transient state)
    # and override review_loop below; exceptions in _PROPAGATE_UNDERSCORE_KEYS
    # carry cross-iteration capability classifiers (e.g. rollup tags).
    propagated_ctx = {
        k: v
        for k, v in source_ctx.items()
        if k != "review_loop"
        and (not k.startswith("_") or k in _PROPAGATE_UNDERSCORE_KEYS)
    }
    propagated_ctx["scope"] = propagated_scope
    # Propagate direction title (text snippet) so agent PR-body footers can
    # show a human-readable label alongside the direction ID.
    if direction_data and not propagated_ctx.get("direction_title"):
        _dir_text = (direction_data.get("text") or "").strip()
        if _dir_text:
            propagated_ctx["direction_title"] = _dir_text[:80]
    # Carry forward the create task's self-review outcome so the review agent
    # can see which files were verified (truncated_files_read) and whether the
    # self-review was marked incomplete, and focus attention on blind spots
    # rather than re-reviewing files the self-review already checked.
    prev_self_review_outcome = source_ctx.get("_self_review_outcome")
    if prev_self_review_outcome:
        propagated_ctx["_self_review_outcome"] = prev_self_review_outcome
    # Scope-pressure awareness: expose file-budget utilization to both fix and
    # review agents. Fix agents use it to prefer minimal edits; reviewers use it
    # to weigh whether requesting changes would push the PR over its budget.
    changed_files = (
        source_ctx.get("_pr_changed_files")
        or ((orig.context or {}).get("_pr_changed_files") if orig else None)
        or []
    )
    utilization = _compute_scope_budget_utilization(changed_files, propagated_scope)
    if utilization:
        propagated_ctx["scope_budget_utilization"] = utilization
    overflow_warning = _compute_scope_overflow_warning(changed_files, propagated_scope)
    if overflow_warning:
        propagated_ctx["scope_overflow_warned"] = True
        propagated_ctx["scope_overflow_warning"] = overflow_warning
        source_ctx_updated = dict(source_ctx)
        source_ctx_updated["scope_overflow_warned"] = True
        source_ctx_updated["scope_overflow_warning"] = overflow_warning
        source_task.context = source_ctx_updated
        source_ctx = source_ctx_updated
        signal_already_emitted = bool(
            source_ctx.get(SCOPE_OVERFLOW_PR_SIGNAL_EMITTED_KEY)
            or (
                (orig.context or {}).get(SCOPE_OVERFLOW_PR_SIGNAL_EMITTED_KEY)
                if orig
                else False
            )
        )
        if not signal_already_emitted:
            # C2 lock-window fix: the scope-overflow PR signal (token mint +
            # add_issue_label + create_pr_comment) is GitHub network I/O and must
            # NOT run while the per-PR advisory lock (acquired at the top of this
            # function via acquire_pr_spawn_lock) is held — those round-trips can
            # block 5-30s under rate-limit, starving concurrent spawn attempts on
            # the same PR. The advisory lock only needs to protect the DB
            # dedup/insert. So: mark the idempotency flag durable INSIDE this
            # transaction (which dedups concurrent spawns the instant we commit),
            # capture the signal payload, and emit the actual GitHub calls
            # post-commit (after the lock releases). See the deferred-signal
            # block after the spawn commit below.
            changed_paths = overflow_warning.get("changed_files") or []
            file_lines = "\n".join(f"- `{path}`" for path in changed_paths)
            scope_label = overflow_warning.get("scope") or propagated_scope
            _deferred_scope_overflow_signal = {
                "token_task_id": orig.id if orig is not None else source_task.id,
                "pr_number": pr_number,
                "comment_body": (
                    "⚠️ Scope overflow detected for this PR.\n\n"
                    f"- Declared scope: `{scope_label}`\n"
                    f"- Non-test files changed: `{overflow_warning['non_test_file_count']}`\n"
                    f"- Scope budget: `{overflow_warning['threshold']}`\n\n"
                    "Affected files (capped):\n"
                    f"{file_lines}"
                ),
            }
            # Persist the idempotency flag transactionally so a concurrent or
            # later spawn on the same PR sees signal_already_emitted=True and
            # does not double-post. The flag is durable on commit regardless of
            # whether the post-commit GitHub call later succeeds — losing one
            # advisory label/comment on a rare network failure is strictly
            # better than holding a DB lock across that I/O.
            source_ctx_updated[SCOPE_OVERFLOW_PR_SIGNAL_EMITTED_KEY] = True
            source_task.context = source_ctx_updated
            source_ctx = source_ctx_updated
            if orig is not None:
                orig_ctx = dict(orig.context or {})
                orig_ctx[SCOPE_OVERFLOW_PR_SIGNAL_EMITTED_KEY] = True
                orig.context = orig_ctx
                session.add(orig)
        if task_type == "review":
            overflow_instruction = (
                "Scope-overflow context: this PR exceeded the declared scope file "
                f"budget ({overflow_warning['non_test_file_count']} non-test files "
                f"vs threshold {overflow_warning['threshold']}). During review, "
                "explicitly assess whether the additional files form a coherent, "
                "necessary unit or indicate scope sprawl that should be deferred."
            )
            payload_data["original_prompt"] = (
                (payload_data.get("original_prompt") or "").strip()
                + "\n\n"
                + overflow_instruction
            ).strip()
    # Persist the scope-budget hard cap (either newly triggered this spawn or
    # carried forward from a prior spawn) so _max_review_iterations enforces
    # it on subsequent review/fix iterations for this chain.
    prior_hard_cap = source_ctx.get("_scope_budget_hard_cap")
    effective_hard_cap = scope_budget_hard_cap
    # Only propagate a prior hard cap while the PR is still under scope pressure.
    # If utilization has dropped below the warn threshold (or is unknown), the
    # fix chain has successfully narrowed its footprint and the iteration
    # restriction is no longer warranted — let the cap relax back to per_chain_cap.
    _prior_cap_still_relevant = (
        utilization is not None
        and utilization.get("percentage", 0) >= SCOPE_PRESSURE_WARN_THRESHOLD * 100
    )
    if (
        isinstance(prior_hard_cap, int)
        and prior_hard_cap > 0
        and _prior_cap_still_relevant
    ):
        effective_hard_cap = (
            min(effective_hard_cap, prior_hard_cap)
            if effective_hard_cap is not None
            else prior_hard_cap
        )
    if effective_hard_cap is not None:
        propagated_ctx["_scope_budget_hard_cap"] = effective_hard_cap
    # Persist issue-churn cap reduction across iterations. Once churn is detected,
    # the reduced cap is durable so subsequent spawns honor the earlier reduction.
    if isinstance(churn_cap_reduction, int) and churn_cap_reduction > 0:
        propagated_ctx["_churn_cap_reduction"] = churn_cap_reduction
    # Propagate diagnosis context from the original create task when the PR was
    # spawned by autonomous remediation. The reviewer uses these to verify the
    # fix targets the diagnosed root cause rather than doing a generic review.
    orig_ctx_src = (orig.context or {}) if orig else {}
    if orig_ctx_src.get("diagnosis_task_id"):
        for _dk in (
            "diagnosis_task_id",
            "diagnosis_root_cause",
            "diagnosis_recommended_fix",
            "severity",
            "diagnosis_confidence",
            "diagnosis_fingerprint",
            "error_fingerprint",
            "trigger_failure_class",
            "trigger_failure_taxonomy",
        ):
            if orig_ctx_src.get(_dk) is not None:
                propagated_ctx[_dk] = orig_ctx_src[_dk]

    if task_type == "fix" and fix_reason == "ci_failure" and ci_failure_logs:
        # Truncate from the start, not the end — pytest's FAILED summary lines
        # (the ones the agent extracts selectors from) sit at the tail of each
        # job's log section.
        propagated_ctx["ci_failure_logs"] = str(ci_failure_logs)[-5000:]

    if task_type == "fix" and investigated_files:
        # Reviewer's working set forwarded so the fix agent can start at the
        # paths the reviewer already opened, instead of rediscovering them.
        # Capped to keep per-task context bounded; the cap mirrors the agent
        # side's 25-path slice when it builds verdict_json.
        propagated_ctx["investigated_files"] = [
            str(p) for p in investigated_files if isinstance(p, str) and p
        ][:25]

    # Propagate the original create task's planned file list so the reviewer can
    # objectively detect scope creep (files changed but not in the original plan).
    planned_files = (orig.context or {}).get("related_files") if orig else None
    if not planned_files:
        planned_files = source_ctx.get("related_files")
    if planned_files:
        propagated_ctx["_planned_files"] = list(planned_files)
    # Drift signal: surface files that drifted in (no plan/approval link) or
    # drifted out (lost from approved iteration). Computed at spawn time so the
    # fix-agent prompt builder can render the section without re-deriving from
    # scratch. Only computed when spawning a fix — reviewers don't need it.
    drift_signal: dict | None = None
    if task_type == "fix":
        _planned_for_drift = list(planned_files) if planned_files else []
        # As above: review tasks don't push commits, fall back to the original
        # task's _pr_changed_files (the create/fix that produced the PR state
        # the review verdict was made against).
        _current_files = list(
            (source_task.context or {}).get("_pr_changed_files")
            or ((orig.context or {}).get("_pr_changed_files") if orig else None)
            or []
        )
        drift_signal = compute_drift_signal(
            new_review_history or [],
            _planned_for_drift,
            _current_files,
        )

    # Fix-agent responses carry-forward: if the prior fix iteration wrote
    # structured rationale for disputed issues, accumulate it onto the next
    # reviewer's context so the verify-mode prompt block can render. Each
    # entry persists across iterations until the issue is resolved.
    prior_responses = list(source_loop.get("fix_agent_responses") or [])
    new_responses = list((source_task.context or {}).get("_fix_agent_responses") or [])
    # Cap aggregate at ~30 (recent-first) so context doesn't bloat over long
    # cycles. Per-iteration cap of 5 is enforced at write time elsewhere.
    accumulated_responses = (prior_responses + new_responses)[-30:]

    # Bound the carried-forward review history before persisting it into the
    # child's GIN-indexed context. review_history accumulates one entry per
    # iteration; on the log-scraping fallback path each entry's `feedback` is the
    # reviewer agent's ENTIRE run log (observed multi-MB). Unbounded, that blob is
    # re-serialized into every downstream task.context on every spawn, and because
    # context is GIN-indexed each write triggers a TOAST rewrite + GIN reindex —
    # the multi-second single-row PK updates that saturated Postgres (the outage
    # this fix closes). bound_review_history caps the entry count and truncates
    # each `feedback` field; it re-bounds the WHOLE list (not just the new entry)
    # so a fat blob carried over from a pre-fix in-flight row self-heals here on
    # the next spawn. All structured consumers (churn/drift/stuck-cycle/issue-
    # ledger) ran above on the unbounded list and read `issues`/`changed_files`,
    # not `feedback`; the only `feedback` readers are the truncated retry-prompt
    # preview and the stuck-cycle Jaccard fallback, both unaffected within the cap
    # for any real-world feedback (truncation is a no-op below it).
    bounded_review_history = bound_review_history(new_review_history)

    propagated_ctx["review_loop"] = {
        "pr_number": pr_number,
        "branch": branch,
        "base_branch": base_branch,
        "original_task_id": resolved_original_id,
        "iteration": iteration,
        "max_iterations": per_chain_cap,
        **(
            {"review_history": bounded_review_history} if bounded_review_history else {}
        ),
        **({"fix_reason": fix_reason} if fix_reason else {}),
        **(
            {"reviewed_head_sha": reviewed_head_sha}
            if task_type == "fix" and reviewed_head_sha
            else {}
        ),
        **(
            {"delta_base_sha": delta_base_sha}
            if task_type == "review" and delta_base_sha
            else {}
        ),
        **(
            {"review_target": requested_review_target}
            if task_type == "review" and requested_review_target is not None
            else {}
        ),
        **(
            {"fix_target": requested_fix_target}
            if task_type == "fix" and requested_fix_target is not None
            else {}
        ),
        **({"churn_detections": churn_detections} if churn_detections else {}),
        **({"scope_overflow_warned": True} if overflow_warning else {}),
        **({"drift_signal": drift_signal} if drift_signal else {}),
        **(
            {"fix_agent_responses": accumulated_responses}
            if accumulated_responses
            else {}
        ),
        # Binding blocker state. Open is complete and never capped; resolved is
        # bounded historical explanation.
        **(
            {"issue_ledger": _issue_ledger}
            if _issue_ledger.get("open") or _issue_ledger.get("resolved")
            else {}
        ),
        # Reviewer-model pin: first review stamps the resolved model; subsequent
        # reviews and fix tasks carry it forward so all reviews on the chain use
        # the same model (prevents reviewer personality drift mid-chain).
        **(
            {"reviewer_model": reviewer_model_for_chain}
            if reviewer_model_for_chain
            else {}
        ),
    }

    # ── Review-fix lane pilot wiring (flag-gated, self-repo only) ─────────────
    # Lane-ize ONLY the fix side of the review -> fix -> re-review loop. The
    # reviewer/auditor ALWAYS runs cold: stripping transient lane directives is
    # unconditional, so a stale ``improvement_lane_id`` can never ride a review
    # onto a lane-reserved member (gate integrity, kill-class). On a classic
    # chain (flag off or not self-repo) every line below is a no-op and the
    # spawn path is byte-for-byte unchanged.
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    from app.models import Project as _Project
    from app.review_fix_lane_pilot import (
        CHECKPOINT_PHASE_RECOVER,
        LANE_CHAIN_ID_KEY,
        LANE_PILOT_ASSIGNMENT_KEY,
        activate_lane_for_fix,
        checkpoint_or_recover_phase,
        ensure_chain_lane,
        get_chain_lane,
        is_review_fix_lane_pilot_active_for_project,
        is_review_fix_lane_shadow_active_for_project,
        lane_arm_of,
        record_lane_arm_assignment,
        strip_transient_lane_context,
        wire_fix_lane_context,
    )
    from app.models import ImprovementLaneStatus

    strip_transient_lane_context(propagated_ctx)
    lane_review_force_cold = False
    lane_fix_task_id: uuid.UUID | None = None
    lane_fix_to_activate = None
    lane_fix_lease_workspace = True
    # Fetch the project for the project-aware pilot gate. The gate consults both
    # the runtime flag AND the durable persisted KILL/ADOPT verdict so a KILL
    # that survived a runtime_config reload is still honored.
    _pilot_project = await session.get(_Project, source_task.project_id)
    _lane_pilot_active = is_review_fix_lane_pilot_active_for_project(_pilot_project)
    _lane_shadow_active = is_review_fix_lane_shadow_active_for_project(_pilot_project)
    if _lane_pilot_active or _lane_shadow_active:
        if _lane_shadow_active:
            # Shadow chains must not write or forward the ITT assignment key:
            # compute_lane_pilot_aggregates treats it as a trial sample.
            propagated_ctx.pop(LANE_PILOT_ASSIGNMENT_KEY, None)
            if LANE_PILOT_ASSIGNMENT_KEY in source_ctx:
                source_ctx = dict(source_ctx)
                source_ctx.pop(LANE_PILOT_ASSIGNMENT_KEY, None)
                source_task.context = source_ctx
                _flag_modified(source_task, "context")
        # Record the intent-to-treat arm ONCE for the chain (BOTH arms), keyed on
        # the immutable original create-task id, so arm membership is decided
        # before any fix-side treatment. Persisted on the source task (durable,
        # read by the measurement on terminal rows) AND propagated forward.
        _arm = None
        if _lane_pilot_active:
            _seed = resolved_original_id or str(source_task.id)
            record_lane_arm_assignment(source_ctx, _seed, _pilot_project)
            _arm = lane_arm_of(source_ctx)
            if _arm is not None:
                propagated_ctx[LANE_PILOT_ASSIGNMENT_KEY] = _arm
                if (source_task.context or {}).get(LANE_PILOT_ASSIGNMENT_KEY) != _arm:
                    source_task.context = source_ctx
                    _flag_modified(source_task, "context")
        if _lane_shadow_active or _arm == "lane":
            if task_type == "review":
                # Implement/fix phase just completed -> checkpoint the lane and
                # in full-pilot mode move its workspace to WARM_RESERVED, then
                # spawn the review COLD. Shadow writes the same checkpoint/CAS
                # evidence but leaves classic review spawning untouched.
                lane_review_force_cold = _lane_pilot_active
                _lane = await ensure_chain_lane(
                    session,
                    project_id=source_task.project_id,
                    ctx=source_ctx,
                    title=(propagated_ctx.get("direction_title") or title)[:120],
                    branch_name=branch,
                    base_branch=base_branch,
                    direction_id=propagated_ctx.get("direction_id"),
                    current_task_id=source_task.id,
                    pilot_arm="lane" if _lane_pilot_active else None,
                )
                _chain_lane_id = source_ctx.get(LANE_CHAIN_ID_KEY)
                if _chain_lane_id:
                    propagated_ctx[LANE_CHAIN_ID_KEY] = _chain_lane_id
                    source_task.context = source_ctx
                    _flag_modified(source_task, "context")
                _phase_sha = source_ctx.get("head_sha") or source_ctx.get(
                    "_pr_head_sha"
                )
                if not _phase_sha and orig is not None:
                    _phase_sha = (orig.context or {}).get("head_sha")
                # Act on the checkpoint outcome instead of ignoring it. A
                # missing/unverifiable checkpoint under budget deterministically
                # suppresses this cold review and requeues the failed phase as a
                # lane-aware recovery task; an exhausted budget closes the lane
                # inline (KILLED + reservation released) and falls back to a
                # classic cold review. A written checkpoint (or a benign no-op)
                # proceeds to the cold review unchanged.
                _ckpt_outcome = await checkpoint_or_recover_phase(
                    session,
                    source_task,
                    _lane,
                    sha=_phase_sha,
                    summary=f"lane phase complete PR #{pr_number}",
                    reserve_workspace=_lane_pilot_active,
                    enable_recovery=_lane_pilot_active,
                    record_miss_marker=_lane_shadow_active,
                )
                if _ckpt_outcome == CHECKPOINT_PHASE_RECOVER:
                    return await _handle_lane_checkpoint_recovery(
                        session,
                        source_task=source_task,
                        lane=_lane,
                        pr_number=pr_number,
                        branch=branch,
                        base_branch=base_branch,
                        resolved_original_id=resolved_original_id,
                        propagated_ctx=propagated_ctx,
                        parent_id=parent_id,
                        inherited_priority=inherited_priority,
                    )
            elif task_type == "fix":
                # Fix side resumes warm on the lane-reserved member: bind the
                # lane ACTIVE for this phase and attach warm-resume directives +
                # use receipts. The reviewer never reaches this branch.
                _lane = await get_chain_lane(session, source_ctx)
                _expected_pilot_arm = "lane" if _lane_pilot_active else None
                if (
                    _lane is not None
                    and _lane.status == ImprovementLaneStatus.PARKED.value
                    and _lane.pilot_arm == _expected_pilot_arm
                ):
                    if _lane_pilot_active:
                        lane_fix_task_id = uuid.uuid4()
                    lane_fix_to_activate = _lane
                    lane_fix_lease_workspace = _lane_pilot_active
                    if _lane_pilot_active:
                        wire_fix_lane_context(propagated_ctx, _lane)
                elif _lane is not None:
                    logger.info(
                        "[LANE-PILOT] chain lane %s has pilot_arm=%s but fix mode "
                        "expects %s; skipping lane reuse for this spawn",
                        _lane.id,
                        _lane.pilot_arm,
                        _expected_pilot_arm,
                    )

    # Phase 2c: warm-VM continuity pin. When the runtime flag is on AND the
    # source task's VM is still alive (IN_PROGRESS or completed within the
    # warm-handoff window), pin the spawned task to that VM and leave
    # vm_instance_id=NULL so the orchestrator's provisioning loop knows to
    # check the heartbeat map before provisioning. The agent on the live
    # VM picks the task up via /api/agent/next-role. If the warm window
    # expires or the VM dies before pickup, the orchestrator clears the
    # warm fields and provisions normally.
    spawn_vm_instance_id: str | None = vm_name
    warm_continuity_enabled = bool(_runtime_config.get("warm_continuity_enabled", True))
    # Funnel observability: every spawn that reaches this decision block counts
    # as an attempt; exactly one outcome (pin_set or one skip reason) is also
    # recorded. Guards are ordered as a flat sequence so each branch is
    # distinguishable in the funnel snapshot.
    from app.warm_backlog_reuse import (
        record_spawn_attempt,
        record_spawn_pin_set,
        record_spawn_pin_skipped,
    )

    # Defense-in-depth pause guard: never pin a warm-handoff to a paused
    # project's VM. Pause blocks ALL new dispatch on every path; the
    # chain-continuity claim site (agent_warm_continuity.py) already filters
    # Project.paused, but skipping the pin here stops a paused project's
    # create->review->fix->... chain from even creating the pin during drain.
    from app.models import Project

    source_project = await session.get(Project, source_task.project_id)
    source_project_paused = bool(source_project and source_project.paused)

    record_spawn_attempt()
    if suppress_warm_handoff:
        record_spawn_pin_skipped("caller_suppressed")
    elif lane_review_force_cold:
        # Lane-arm review: the reviewer must be independent and cold. Never pin
        # it to the implementer's (now lane-reserved) VM — gate integrity.
        record_spawn_pin_skipped("lane_review_cold")
    elif not warm_continuity_enabled:
        record_spawn_pin_skipped("disabled")
    elif is_warm_continuity_suppressed():
        record_spawn_pin_skipped("breaker_tripped")
    elif source_project_paused:
        record_spawn_pin_skipped("project_paused")
    elif not source_task.vm_instance_id:
        record_spawn_pin_skipped("source_cold")
    else:
        # Source task is eligible if it's currently running OR completed
        # recently enough to keep the warm chain warm. Use the most recent
        # heartbeat as the "completion" reference. Note: routers/agent_status.py
        # does NOT set last_heartbeat=_utcnow() on the terminal write, so this
        # measures time since the LAST 30s task-heartbeat tick — giving up to
        # 30s less margin than the WARM_HANDOFF_WINDOW_SECS budget suggests.
        # The 240s window absorbs that gap with room to spare.
        is_in_progress = source_task.status == TaskStatus.IN_PROGRESS
        is_recent_terminal = False
        if not is_in_progress and source_task.last_heartbeat is not None:
            elapsed = (_utcnow() - source_task.last_heartbeat).total_seconds()
            is_recent_terminal = elapsed <= WARM_HANDOFF_WINDOW_SECS
        if not (is_in_progress or is_recent_terminal):
            record_spawn_pin_skipped("stale_source")
        elif await _pinned_unclaimed_sibling_exists(
            session,
            project_id=source_task.project_id,
            warm_target_vm_id=source_task.vm_instance_id,
            pr_number=pr_number,
        ):
            # A prior spawn for this same PR already pinned an unclaimed task
            # to this same source VM. Minting a second 240s pin is redundant
            # churn — the VM serves one task then tears down, so the standing
            # pin (and its existing deadline) is the one that governs. Skipping
            # here stops the new-row pin churn that keeps the orchestrator's
            # skip-provision loop orbiting a soon-to-be-doomed VM.
            record_spawn_pin_skipped("sibling_already_pinned")
        else:
            propagated_ctx["warm_target_vm_id"] = source_task.vm_instance_id
            # Stored as int — query side uses .as_string() which on Postgres reads
            # ->> (text), so int → "<n>" conversion is automatic. SQLite test
            # backend behavior is not load-bearing here, but keeping the type
            # consistent makes the test fixtures reflect production.
            propagated_ctx["warm_target_pr"] = (
                int(pr_number) if pr_number is not None else None
            )
            propagated_ctx["warm_handoff_deadline"] = (
                _utcnow() + timedelta(seconds=WARM_HANDOFF_WINDOW_SECS)
            ).isoformat()
            spawn_vm_instance_id = None
            record_spawn_pin_set()
            logger.info(
                f"[PIPELINE] Warm-VM pin set for {task_type} PR #{pr_number}: "
                f"target_vm={source_task.vm_instance_id} "
                f"deadline={propagated_ctx['warm_handoff_deadline']}"
            )

    # Cheap fat-row telemetry on the final persisted context. With review_history
    # now bounded this should never fire; if it does, a new unbounded context
    # sub-field has regressed and is visible in logs rather than only post-outage.
    # Non-raising — never blocks the spawn.
    warn_if_context_oversized(propagated_ctx, label=f"spawn {task_type} PR#{pr_number}")

    new_task = Task(
        project_id=source_task.project_id,
        title=title,
        description=fix_prompt or f"Automated {task_type} for PR #{pr_number}",
        parent_task_id=parent_id,
        priority=inherited_priority,
        payloads=[payload_data],
        context=propagated_ctx,
        idempotency_key=idempotency_key,
        **({"id": lane_fix_task_id} if lane_fix_task_id is not None else {}),
        vm_instance_id=spawn_vm_instance_id,
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    if task_type == "fix":
        # Commit the active child and authority revocation atomically.  Every
        # fix origin (review rejection, CI, conflict, migration, recovery) uses
        # this helper, so no caller can accidentally preserve an old APPROVE or
        # no-review exemption while code is changing.
        revoke_review_authority_for_active_fix(source_task, new_task.id)
        session.add(source_task)
        if orig_task is not None and orig_task.id != source_task.id:
            revoke_review_authority_for_active_fix(orig_task, new_task.id)
            session.add(orig_task)
    session.add(new_task)
    if lane_fix_to_activate is not None:
        await activate_lane_for_fix(
            session,
            lane_fix_to_activate,
            task_id=new_task.id,
            lease_workspace=lane_fix_lease_workspace,
        )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await _get_active_pipeline_task_by_key(session, idempotency_key)
        if existing is None:
            # The colliding task can race OUT of the active-status window between
            # our failed commit and this re-query — e.g. a sibling fix task that
            # was QUEUED at insert time gets BLOCKED on a file-conflict lock (or
            # goes terminal) before we look. The active-only lookup then misses
            # it and the old code re-raised, surfacing the IntegrityError to
            # operators as a bogus "CI check error" that wedged the whole review
            # chain. A row with this idempotency_key DID exist in an active
            # status at commit time (that is exactly what the
            # ux_task_active_idempotency_key partial index enforces), so re-query
            # WITHOUT the status filter instead of crashing.
            existing = (
                (
                    await session.execute(
                        select(Task)
                        .where(Task.idempotency_key == idempotency_key)
                        .order_by(Task.created_at.desc())
                    )
                )
                .scalars()
                .first()
            )
        if existing is None:
            # Truly nothing with this key post-rollback (would require a hard
            # delete racing us — not observed in practice). Treat as a benign
            # idempotency race and suppress this spawn rather than crashing the
            # caller; the next rollup pass re-evaluates with a fresh iteration key.
            logger.warning(
                f"[PIPELINE] {task_type} spawn for PR #{pr_number} hit idempotency "
                f"key but found no row post-rollback; suppressing spawn "
                f"(benign idempotency race)."
            )
            return (None, False)
        logger.warning(
            f"[PIPELINE] Concurrent {task_type} spawn for PR #{pr_number} "
            f"hit idempotency key; using existing task {existing.id}"
        )
        reloaded_source = await session.get(Task, source_task_id)
        if reloaded_source is not None:
            reloaded_source.latest_status_message = (
                f"{task_type.title()} skipped for PR #{pr_number} — "
                f"idempotent spawn already active → {str(existing.id)[:8]}"
            )
            _set_spawn_reason(reloaded_source, SPAWN_REASON_DEDUP)
            if task_type == "fix":
                revoke_review_authority_for_active_fix(
                    reloaded_source, existing.id
                )
            session.add(reloaded_source)
            if task_type == "fix":
                try:
                    _reloaded_orig_id = uuid.UUID(resolved_original_id)
                except (TypeError, ValueError):
                    _reloaded_orig_id = None
                if (
                    _reloaded_orig_id is not None
                    and _reloaded_orig_id != reloaded_source.id
                ):
                    _reloaded_orig = await session.get(Task, _reloaded_orig_id)
                    if _reloaded_orig is not None:
                        revoke_review_authority_for_active_fix(
                            _reloaded_orig, existing.id
                        )
                        session.add(_reloaded_orig)
            await session.commit()
        return (existing, False)
    except DBAPIError as _spawn_db_exc:
        if not _is_lock_timeout_error(_spawn_db_exc):
            raise
        # Transient row/index contention (prod burst 2026-06-07 01:05): the
        # INSERT waited on a peer's uncommitted same-key insert (or a held
        # row) until Postgres' bounded lock_timeout cancelled the statement.
        # Letting this propagate left the caller's session in pending-rollback
        # and cascaded PendingRollbackError through the rest of post-commit
        # (rollup retry-counter read, final commit, agent_status 500s). Treat
        # it exactly like the in-flight-peer deferral above: roll back, mark
        # the source DEFERRED, and let the next rollup sweep retry. The
        # rollback expires every loaded instance (even with
        # expire_on_commit=False), so re-fetch the source by id before
        # touching it — session.get refreshes the caller's identity-mapped
        # instance in place. The deferral marker rides the caller's eventual
        # commit, mirroring the in-flight-defer path.
        await session.rollback()
        logger.warning(
            f"[PIPELINE] {task_type} spawn for PR #{pr_number} hit a lock "
            f"timeout on commit — deferring to the next sweep: "
            f"{str(_spawn_db_exc)[:200]}"
        )
        reloaded_source = await session.get(Task, source_task_id)
        if reloaded_source is not None:
            reloaded_source.latest_status_message = (
                f"{task_type.title()} deferred for PR #{pr_number} — "
                f"spawn commit hit a lock timeout; next sweep retries"
            )
            _set_spawn_reason(reloaded_source, SPAWN_REASON_DEFERRED)
            session.add(reloaded_source)
        return (None, False)
    await session.refresh(new_task)

    history = TaskHistory(
        task_id=new_task.id,
        action="SPAWNED",
        details={
            "spawned_by": "REVIEW_PIPELINE",
            "type": task_type,
            "pr_number": pr_number,
        },
    )
    session.add(history)
    await session.commit()

    await broadcast_to_project(
        str(source_task.project_id),
        "task_updated",
        {"task_id": str(new_task.id), "status": TaskStatus.QUEUED},
    )
    _set_spawn_reason(source_task, SPAWN_REASON_SPAWNED)
    session.add(source_task)

    # C2 lock-window fix: emit the deferred scope-overflow PR signal AFTER the
    # spawn commit above released the per-PR advisory lock (helper does the
    # token + label + comment best-effort, lock-free).
    if _deferred_scope_overflow_signal is not None:
        await _emit_scope_overflow_pr_signal(session, _deferred_scope_overflow_signal)

    # Delete VMs for all superseded/churn-halted tasks after successful spawn
    for vm_id, reason in vms_to_delete:
        await vm_lifecycle._ensure_vm_deleted(vm_id, reason=f"review_pipeline_{reason}")

    return (new_task, True)

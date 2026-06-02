"""Review pipeline: spawn and manage automated review/fix task cycles.

Extracted from task_lifecycle.py to reduce its size. Contains the per-PR
review-fix loop, iteration cap, and stuck-cycle detection logic.

All imports are module-level — no circular dependency risk with app.main
or app.task_lifecycle.
"""

import logging
import re
import uuid
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.database import resolve_dialect_name
from app.github import post_pipeline_merge_summary_comment as post_merge_summary_comment  # noqa: F401
from app.models import Task, TaskHistory, TaskStatus
from app.payload_schemas import validate_internal_payload
from app.shared_state import (
    runtime_config as _runtime_config,
    is_warm_continuity_suppressed,
)
from app.broadcast import broadcast_to_project
from app.time_utils import _utcnow
from app.spawn_calibration import (
    _compute_scope_budget_utilization,
    _compute_scope_overflow_warning,
)

# Pure, side-effect-free review-loop heuristics live in review_heuristics; they
# are re-exported here so existing callers/tests that import them from
# app.review_pipeline keep working unchanged.
from app.review_heuristics import (  # noqa: F401
    AUTO_APPROVE_MIN_ACCEPTANCE_RATE,
    EARLY_NONCONVERGENCE_ESCALATE_AT,
    ISSUE_CHURN_WINDOW,
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
    compute_drift_signal,
    detect_early_nonconvergence,
    detect_fix_all_issue_class,
    fix_all_occurrences_directive,
    should_auto_approve_diff,
    should_auto_approve_small_diff,
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
    _MID_BUDGET_NARROW_FOCUS_PREFIX,
    _SCOPE_PRESSURE_HARD_CAP_PREFIX,
    _is_pipeline_task_stale,
    _set_review_loop_terminal_outcome,
)

logger = logging.getLogger(__name__)


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
_PROPAGATE_UNDERSCORE_KEYS = frozenset(
    {
        "_rollup_retries",
    }
)


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
SPAWN_REASON_DEFERRED = "deferred_cross_type"
SPAWN_REASON_CAP = "cap_exhausted"
SPAWN_REASON_STUCK = "stuck_cycle"
# Early non-convergence escalation: the review-fix loop is whittling/drifting
# (shrinking-but-never-zero count, verification-only drift, or one-at-a-time
# disclosure) and was escalated to the operator at iteration K rather than
# grinding to the full cap. Distinct from stuck_cycle (same issue re-raised).
SPAWN_REASON_NONCONVERGENCE = "early_nonconvergence"
# Two consecutive same-type pipeline tasks failed with an identical failure
# fingerprint — the agent's code edits aren't moving the failure shape, which
# is the signature of a precondition gap (missing system lib, baseline lint
# debt, toolchain layer) rather than a code defect. Halt the chain and let
# the strategic agent (or operator) decide whether to spawn an infra fix.
SPAWN_REASON_BLOCKED_REPEAT = "blocked_repeat_fingerprint"

_PIPELINE_ACTIVE_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
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
) -> str:
    chain_id = f"rollup-pr-{pr_number}" if is_rollup else original_task_id
    return (
        f"review_pipeline:{project_id}:{chain_id}:{pr_number}:{task_type}:{iteration}"
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
    from app.models import MessageType, ProjectMessage

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
    pm = ProjectMessage(
        project_id=source_task.project_id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        from_task_id=source_task.id,
        content=body,
        context={
            "pr_number": pr_number,
            "fingerprint": fingerprint,
            "source_task_id": str(source_task.id),
            "prior_task_id": str(prior_task_id),
            "trigger": "repeat_fingerprint_halt",
        },
    )
    session.add(pm)


# ---------------------------------------------------------------------------
# Cross-cutting safeguard gate
# ---------------------------------------------------------------------------

TOP_LEVEL_MODULES = frozenset({"backend", "agent", "admin-ui"})


def _is_cross_cutting_pr(source_task: Task) -> tuple[bool, str]:
    """Return (True, reason) when the PR qualifies for a cross-cutting safeguard gate.

    Three conditions, any one of which triggers the gate:
    (a) scope=large — large PRs always get a safeguard review.
    (b) scope=medium AND changed files span >= cross_cutting_gate_medium_module_threshold
        distinct top-level modules (backend / agent / admin-ui).
    (c) scope_overflow_warned=True AND diff lines exceed
        cross_cutting_gate_overflow_line_threshold.
    """
    if not _runtime_config.get("cross_cutting_gate_enabled", True):
        return (False, "gate disabled")
    ctx = source_task.context or {}
    scope = ctx.get("scope", "medium")
    if scope == "large":
        return (True, "scope=large")
    if scope == "medium":
        changed = ctx.get("_pr_changed_files") or []
        mods = len(
            {f.split("/")[0] for f in changed if f.split("/")[0] in TOP_LEVEL_MODULES}
        )
        threshold = int(
            _runtime_config.get("cross_cutting_gate_medium_module_threshold", 3)
        )
        if mods >= threshold:
            return (True, f"scope=medium,{mods}_modules")
    if (ctx.get("review_loop") or {}).get("scope_overflow_warned"):
        lines = _diff_lines_changed(ctx.get("_pr_diff_summary"))
        overflow_threshold = int(
            _runtime_config.get("cross_cutting_gate_overflow_line_threshold", 100)
        )
        if lines and lines > overflow_threshold:
            return (True, f"overflow+{lines}_lines")
    return (False, "not cross-cutting")


async def maybe_spawn_cross_cutting_safeguard(
    orig: Task, pr_number: int, branch: str, base_branch: str, session: AsyncSession
) -> bool:
    """Spawn a security_audit safeguard task if the PR is cross-cutting.

    Sets orig.requires_approval=True and stores the safeguard task ID in
    orig.context so the orchestrator and completion handlers can coordinate.
    Returns True if a safeguard was spawned, False otherwise.
    Idempotent: no-ops if orig.context already has a safeguard_task_id.
    """
    from sqlalchemy.orm.attributes import flag_modified

    ctx = orig.context or {}
    if ctx.get("safeguard_task_id"):
        return False
    # C5 invariant: the safeguard gate is a *merge-approval* gate — it sets
    # orig.requires_approval=True and relies on the safeguard's completion to
    # release it. With require_approval_gates OFF the operator has opted out of
    # manual merge gating, so spawning the safeguard would (a) burn a VM that
    # gates nothing actionable and (b) set requires_approval, which services.py's
    # 2h approval-gate timeout would then use to FAIL the otherwise-healthy
    # parent (discarding merged siblings) if the safeguard ever FAILED/reaped.
    # Early-return so no VM is wasted and no flag is set. (Gates the spawn
    # *and* the requires_approval set in one place — the invariant the whole
    # cluster turns on.)
    if not _runtime_config.get("require_approval_gates", True):
        logger.info(
            f"[PIPELINE] Cross-cutting safeguard skipped for orig {orig.id} "
            f"(require_approval_gates off — no merge gate to enforce)"
        )
        return False
    is_cross, reason = _is_cross_cutting_pr(orig)
    if not is_cross:
        return False

    safeguard_id = uuid.uuid4()
    changed = ctx.get("_pr_changed_files") or []
    files_str = ", ".join(changed[:15]) or "all changed files"
    payload_data = validate_internal_payload(
        {
            "type": "security_audit",
            "prompt": (
                f"Cross-cutting safeguard review for PR #{pr_number} ({reason}). "
                "Assess: (1) security vulnerabilities introduced by cross-module changes, "
                "(2) interface breakage across module boundaries, "
                "(3) test coverage for cross-module interactions. "
                f"Changed files: {files_str}. Return findings as a JSON array."
            ),
            "base_branch": base_branch,
        },
        "review_pipeline:cross_cutting_safeguard",
    )
    safeguard = Task(
        id=safeguard_id,
        project_id=orig.project_id,
        title=f"Cross-cutting safeguard: PR #{pr_number}",
        description=f"Security/QA safeguard ({reason})",
        parent_task_id=orig.id,
        payloads=[payload_data],
        context={
            "safeguard_for_task_id": str(orig.id),
            "safeguard_pr_number": pr_number,
            "safeguard_base_branch": base_branch,
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(safeguard)
    orig.requires_approval = True
    orig.context = {
        **ctx,
        "safeguard_task_id": str(safeguard_id),
        "safeguard_reason": reason,
        "safeguard_pr_number": pr_number,
    }
    flag_modified(orig, "context")
    orig.latest_status_message = (
        f"Merge gated — awaiting cross-cutting safeguard for PR #{pr_number} ({reason})"
    )
    session.add(orig)
    session.add(
        TaskHistory(
            task_id=safeguard_id,
            action="SPAWNED",
            details={
                "spawned_by": "CROSS_CUTTING_GATE",
                "pr_number": pr_number,
                "reason": reason,
            },
        )
    )
    await session.commit()
    logger.info(
        f"[PIPELINE] Cross-cutting safeguard {safeguard_id} spawned "
        f"for PR #{pr_number} ({reason}) — merge gated on orig {orig.id}"
    )
    await broadcast_to_project(
        str(orig.project_id),
        "task_updated",
        {"task_id": str(safeguard_id), "status": TaskStatus.QUEUED},
    )
    return True


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
    ci_failure_logs: str = "",
    investigated_files: list[str] | None = None,
    skip_rereview_minor_only: bool = False,
    suppress_warm_handoff: bool = False,
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

    await acquire_pr_spawn_lock(session, pr_number)

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

    # Guard: don't spawn if PR is already merged (check both original and source).
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
        return (None, False)

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
    active_pipeline = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == source_task.project_id,
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.PROVISIONING,
                            TaskStatus.IN_PROGRESS,
                            TaskStatus.EVALUATING,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for t in active_pipeline:
        if t.id == source_task.id:
            continue
        t_payload = t.first_payload
        t_type = t_payload.get("type")
        t_orig = t_payload.get("original_task_id") or ""
        if t_payload.get("pr_number") != pr_number or t_type not in ("review", "fix"):
            continue
        if not is_rollup_source and t_orig != resolved_original_id:
            continue

        if _is_pipeline_task_stale(t):
            from app.failure_classification import (
                AGENT_TIMEOUT,
                persist_failure_category,
            )

            t.status = TaskStatus.CANCELLED
            t.latest_status_message = (
                f"Superseded by new {task_type} for PR #{pr_number}: "
                f"no heartbeat for >{PIPELINE_TASK_STALE_MINUTES}min"
            )
            persist_failure_category(t, AGENT_TIMEOUT)
            session.add(t)
            superseded_stale_task = True
            logger.warning(
                f"[PIPELINE] Superseding stale {t_type} task {t.id} "
                f"(last_heartbeat={t.last_heartbeat}, started_at={t.started_at}) "
                f"for PR #{pr_number}"
            )
            continue

        if t_type == task_type:
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
            return (t, False)

        if task_type == "fix" and t_type == "review":
            from app.failure_classification import (
                AGENT_TIMEOUT,
                persist_failure_category,
            )

            t.status = TaskStatus.CANCELLED
            t.latest_status_message = (
                f"Superseded by fix on PR #{pr_number}: review was scoped to "
                f"a pre-conflict / pre-CI-failure snapshot"
            )
            persist_failure_category(t, AGENT_TIMEOUT)
            session.add(t)
            superseded_stale_task = True
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

    # Per-PR iteration cap: count completed review/fix tasks for this PR across all chains.
    # Excludes archived tasks (manual iteration-cap reset path) and infra-class
    # failures: a task that died in VM provisioning / rate limit / agent timeout
    # / external API / stale-info push never actually ran a review, so counting
    # it toward the per-PR iteration cap would trip the cap with zero real
    # reviews on the PR.
    #
    # Push pr_number + type filters into SQL via the partial expression index
    # ``ix_task_payloads_pr_number`` (alembic 0057). The infra-class failure
    # filter remains Python-side because INFRA_CLASSES is composed dynamically.
    # ``load_only`` keeps the row narrow — we only need status/context/payloads
    # for the post-filter; status_log and other JSONB columns stay deferred.
    from app.failure_classification import INFRA_CLASSES

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
        type_match = Task.payloads[0]["type"].as_string().in_(("review", "fix"))
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

    def _counts_toward_cap(t: Task) -> bool:
        # Exempt both FAILED and CANCELLED tasks with infra failure categories.
        # Without CANCELLED here, supersede-cancels (review_pipeline.py:1170,
        # 1204) — which stamp failure_category=AGENT_TIMEOUT — burn cap slots
        # without doing useful work. Incident 2026-05-07 task 78de0fdb hit
        # the global cap at PR #4091 because ~13 of 25 tasks on the chain
        # were supersede-cancels (review-snapshot-stale, no-heartbeat-for-15min)
        # that each consumed an iteration slot.
        if t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            fc = (t.context or {}).get("failure_category")
            if fc in INFRA_CLASSES:
                return False
        return True

    pr_iterations = sum(1 for t in pr_pipeline_tasks if _counts_toward_cap(t))
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

    # Cross-cutting safeguard gate: on the first review spawn for a PR that spans
    # multiple top-level modules, is large-scope, or has an overflow warning on a
    # large diff, spawn a security_audit safeguard task and block auto-merge until
    # it completes. Skip for rollup reviews — those are already aggregated.
    if task_type == "review" and iteration == 1 and orig is not None and not is_rollup:
        try:
            await maybe_spawn_cross_cutting_safeguard(
                orig, pr_number, branch, base_branch, session
            )
        except Exception as _sg_err:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cross-cutting safeguard spawn failed for PR #{pr_number}: {_sg_err}",
                exc_info=True,
            )

    # Apply per-task-type model/effort defaults from settings
    type_model = _runtime_config.get("task_type_models", {}).get(task_type) or None
    type_effort = _runtime_config.get("task_type_efforts", {}).get(task_type) or None

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
            **({"hotfix": True} if orig_hotfix else {}),
            **({"model": type_model} if type_model else {}),
            **({"effort": type_effort} if type_effort else {}),
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
    new_review_entry: dict[str, object] = {
        "iteration": iteration - 1,
        "feedback": current_feedback,
        # Gate: this branch fires when spawning a fix from REQUEST_CHANGES.
        # APPROVE iterations are written separately at the CI-wait transition
        # in task_lifecycle.py so the trajectory shows them too.
        "verdict": "REQUEST_CHANGES",
        "changed_files": list(_source_changed_files)[:30],
    }
    if current_issues:
        new_review_entry["issues"] = current_issues
    new_review_history = (
        [*prev_review_history, new_review_entry]
        if task_type == "fix" and current_feedback
        else prev_review_history
    )

    churn_detections = int(source_loop.get("churn_detections", 0) or 0)
    # Carry forward any prior churn cap reduction so it survives across iterations.
    churn_cap_reduction: int | None = (source_task.context or {}).get("_churn_cap_reduction")
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
            # Match by issue_id (sha1 of normalized description) when present;
            # fall back to substring match on issue_summary so rationale
            # written before issue_id became standard still counts.
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
                # issue_id sha1 matching deferred until backend hashes
                # consistently; for now any rationale present + same issue
                # description re-appearing is treated as a standoff signal.
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
        if (
            task_type == "fix"
            and iteration >= EARLY_NONCONVERGENCE_ESCALATE_AT
        ):
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
                    nc_orig = await session.get(
                        Task, uuid.UUID(resolved_original_id)
                    )
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
                                f"[NOTIFY] APPROVAL_NEEDED dispatch setup "
                                f"failed: {_ne}"
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

    propagated_ctx["review_loop"] = {
        "pr_number": pr_number,
        "branch": branch,
        "base_branch": base_branch,
        "original_task_id": resolved_original_id,
        "iteration": iteration,
        "max_iterations": per_chain_cap,
        **({"review_history": new_review_history} if new_review_history else {}),
        **({"fix_reason": fix_reason} if fix_reason else {}),
        **({"churn_detections": churn_detections} if churn_detections else {}),
        **({"scope_overflow_warned": True} if overflow_warning else {}),
        **({"drift_signal": drift_signal} if drift_signal else {}),
        **(
            {"fix_agent_responses": accumulated_responses}
            if accumulated_responses
            else {}
        ),
        # First-pass minor-only marker: when set, the fix-completion handler
        # skips the follow-up re-review (CI gates the merge instead), since the
        # fix only addressed minor nits. See task_lifecycle.py fix-completion.
        **(
            {"skip_rereview_minor_only": True}
            if (task_type == "fix" and skip_rereview_minor_only)
            else {}
        ),
    }

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

    record_spawn_attempt()
    if suppress_warm_handoff:
        record_spawn_pin_skipped("caller_suppressed")
    elif not warm_continuity_enabled:
        record_spawn_pin_skipped("disabled")
    elif is_warm_continuity_suppressed():
        record_spawn_pin_skipped("breaker_tripped")
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

    new_task = Task(
        project_id=source_task.project_id,
        title=title,
        description=fix_prompt or f"Automated {task_type} for PR #{pr_number}",
        parent_task_id=parent_id,
        priority=inherited_priority,
        payloads=[payload_data],
        context=propagated_ctx,
        idempotency_key=idempotency_key,
        vm_instance_id=spawn_vm_instance_id,
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
            session.add(reloaded_source)
            await session.commit()
        return (existing, False)
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

    return (new_task, True)

"""Single decision point for all review-skip policy.

This module is the ONE documented home for every review-skip decision the
pipeline makes. Previously these rules were scattered across
``review_heuristics.py`` (the pure predicates) and inline blocks in
``task_lifecycle.py`` (the main-branch auto-approve gate and the security-scan
override). They now live here as documented, independently-testable functions.

Two kinds of thing live here:

1. **Primitive predicates** — ``should_auto_approve_diff``,
   ``should_skip_initial_review``, ``should_skip_rereview`` — pure
   ``(skip, reason)``-returning functions moved verbatim from
   ``review_heuristics.py``. Their behavior is unchanged; only their home
   moved. ``review_pipeline`` re-exports them so existing callers/tests that
   import from ``app.review_pipeline`` keep working unchanged.

2. **Composite decision functions** — ``evaluate_auto_approve_small_main`` and
   ``should_skip_re_review`` — that aggregate the primitives plus the two rules
   that used to be inline in ``task_lifecycle.py`` (the direction-acceptance
   gate on main-branch auto-approve, and the security-scan-blocked override on
   re-review). These return a :class:`ReviewSkipDecision` so the caller can
   distinguish *why* a decision was reached (which named rule fired) instead of
   re-deriving it from a bare boolean.

Everything here is side-effect-free and DB/async-free: callers resolve the
direction row (DB I/O) and run the security scan (network I/O) themselves, then
pass the resolved data in. Keeping this module pure is what makes the
decision-parity test suite (``test_review_policy_parity.py``) able to pin every
rule vector without a database.
"""

from dataclasses import dataclass

from app.models import Task
from app.review_heuristics import _diff_lines_changed, _is_test_file
from app.shared_state import runtime_config as _runtime_config
from app.spawn_calibration import _compute_scope_overflow_warning

# Minimum direction acceptance rate required to auto-approve a small diff.
# Directions with poor merge history still get a full review regardless of
# diff size — the acceptance gate is independent of the line/file thresholds.
AUTO_APPROVE_MIN_ACCEPTANCE_RATE = 0.5

# Intermediate (staging) branches only require a full initial review at large
# scope; small/medium feed a rollup PR that is reviewed as a whole.
_REVIEW_REQUIRED_SCOPES = frozenset({"large"})


@dataclass
class ReviewSkipDecision:
    """Outcome of a review-skip decision.

    ``skip`` is the behavioral answer (skip the review task?). ``reason`` is a
    short human-readable string suitable for status messages and operator logs.
    ``rule`` names the specific rule that produced the outcome so callers can
    branch on it (e.g. the re-review flow does nothing further when the security
    scan blocked, which is distinct from both skip and spawn-review). ``detail``
    carries optional structured metadata (acceptance rate, line/file counts).
    """

    skip: bool
    reason: str
    rule: str = "none"
    detail: dict | None = None


def should_auto_approve_diff(
    source_task: Task, direction_data: dict | None
) -> tuple[bool, str]:
    """Decide whether to skip the review task for a trivially small/medium diff.

    Returns (skip, reason). Reason is always populated — a short human-
    readable string suitable for task status messages and operator logs.

    Gates (all must pass):
    - source task type is create, refactor, or fix
    - scope is 'small' or 'medium' (large always gets full review). Medium
      uses tighter line/file thresholds than small and can be disabled
      independently via auto_approve_medium_max_lines=0.
    - diff size known and <= per-scope max_lines
    - non-test file count <= per-scope max_files
    - direction acceptance_rate > AUTO_APPROVE_MIN_ACCEPTANCE_RATE when
      direction history is available (new directions pass by default)
    """
    source_type = (source_task.first_payload or {}).get("type", "")
    if source_type not in ("create", "refactor", "fix", "migrate"):
        return (False, f"source type {source_type!r} not eligible")

    ctx = source_task.context or {}
    scope = ctx.get("scope", "medium")
    if scope == "small":
        max_lines = _runtime_config.get("auto_approve_max_lines", 50)
        max_files = _runtime_config.get("auto_approve_max_files", 4)
        if not max_lines or max_lines <= 0:
            return (False, "auto-approve disabled (auto_approve_max_lines=0)")
    elif scope == "medium":
        max_lines = _runtime_config.get("auto_approve_medium_max_lines", 30)
        max_files = _runtime_config.get("auto_approve_medium_max_files", 2)
        if not max_lines or max_lines <= 0:
            return (
                False,
                "medium auto-approve disabled (auto_approve_medium_max_lines=0)",
            )
    else:
        return (False, f"scope {scope!r} not eligible (small/medium only)")

    changed_files = ctx.get("_pr_changed_files") or []
    if not changed_files:
        return (False, "changed files unknown")
    overflow_warning = _compute_scope_overflow_warning(changed_files, scope)
    if overflow_warning:
        return (
            False,
            "scope overflow warning present: "
            f"{overflow_warning['non_test_file_count']} non-test files > "
            f"{overflow_warning['threshold']} threshold",
        )
    non_test_count = sum(1 for f in changed_files if not _is_test_file(f))
    if non_test_count > max_files:
        return (False, f"{non_test_count} non-test files > {max_files}")

    lines_changed = _diff_lines_changed(
        ctx.get("_pr_diff_summary"), exclude_test_files=True
    )
    if lines_changed is None:
        return (False, "diff size unknown")
    if lines_changed > max_lines:
        return (False, f"{lines_changed} non-test lines > {max_lines}")

    if direction_data is not None:
        from app.research_completion import _compute_acceptance_rate

        acceptance_rate = _compute_acceptance_rate(direction_data)
        if (
            acceptance_rate is not None
            and acceptance_rate <= AUTO_APPROVE_MIN_ACCEPTANCE_RATE
        ):
            return (
                False,
                f"direction acceptance_rate {acceptance_rate:.0%} "
                f"≤ {AUTO_APPROVE_MIN_ACCEPTANCE_RATE:.0%}",
            )

    return (
        True,
        f"auto-approve: {lines_changed} non-test lines across {non_test_count} non-test "
        f"file(s) (≤ {max_lines}/{max_files})",
    )


should_auto_approve_small_diff = should_auto_approve_diff


def should_skip_initial_review(is_intermediate: bool, scope: str) -> tuple[bool, str]:
    """Whether to skip the automated code review for a newly opened PR.

    Non-intermediate (single-task-to-automain) PRs always skip — the promotion
    gate and CI are the only required backstop, matching v24 cost plan P2.
    Intermediate (staging/rollup sub-PR) tasks skip for small/medium scope;
    the rollup PR to main gets the full review instead.
    Large-scope intermediate PRs always get a full review.

    Returns (skip, reason).
    """
    if not is_intermediate:
        return (True, "non-intermediate branch: CI and promotion gate backstop review")
    if scope not in _REVIEW_REQUIRED_SCOPES:
        return (True, f"{scope} scope on intermediate branch: rollup PR gets full review")
    return (False, f"{scope} scope on intermediate branch: full review required")


def should_skip_rereview(
    fix_reason: str, rereview_waived: bool
) -> tuple[bool, str]:
    """Whether to skip re-review after a fix task completes.

    Skips when the fix addresses mechanical issues that don't change the
    review decision (CI failures, merge conflicts, migration repairs), or
    when the reviewer explicitly waived follow-up review for an ordinary
    code-review fix.

    Does NOT consider the security-scan override — a blocked scan halts the
    whole flow, which is a separate outcome from skip/re-review. The composite
    :func:`should_skip_re_review` layers that override on top of this predicate.

    Returns (skip, reason).
    """
    if fix_reason == "code_review" and rereview_waived:
        return (True, "reviewer waived follow-up review: CI gates the merge")
    if fix_reason in ("ci_failure", "merge_conflict", "migration_guard"):
        return (True, f"{fix_reason}: original review already approved; CI gates merge")
    return (False, f"{fix_reason}: re-review required")


# --- Rule: AUTO_APPROVE_SMALL_MAIN (with DIRECTION_ACCEPTANCE_GATE sub-rule) ---
# Why this rule exists:
#   Trivially small main-branch (non-staging) diffs from directions with a
#   positive merge history are safe to auto-merge without a model review; CI
#   still gates the merge. Skipping review on these saves an entire review VM
#   cycle on the highest-volume, lowest-risk PRs.
#
# When it fires:
#   - The PR base branch is NOT a staging branch (is_intermediate=False)
#   - scope == "small"
#   - runtime config auto_approve_small_diff_enabled is truthy (default True)
#   - DIRECTION_ACCEPTANCE_GATE: the resolved direction has a KNOWN acceptance
#     rate strictly above AUTO_APPROVE_MIN_ACCEPTANCE_RATE. This is STRICTER
#     than should_auto_approve_diff's own internal check: an unknown/None
#     acceptance (new direction, no merge history) is REJECTED here because
#     main-branch merges skip review entirely, so there is no downstream gate to
#     catch a bad direction. should_auto_approve_diff, by contrast, passes
#     direction_data=None by default.
#   - should_auto_approve_diff's size/file/type gates all pass.
#
# What it does:
#   Skips model review; the task proceeds to the security scan + CI-gated
#   auto-merge path (the same one the intermediate small/medium skip uses).
#
# Config keys: auto_approve_small_diff_enabled, auto_approve_max_lines,
#   auto_approve_max_files.
#
# Counterpart tests: test_task_lifecycle.py auto-approve suite; parity vectors
#   in test_review_policy_parity.py::TestAutoApproveSmallMainParity.
def evaluate_auto_approve_small_main(
    task: Task,
    is_intermediate: bool,
    scope: str,
    direction_data: dict | None,
) -> ReviewSkipDecision:
    """Decide whether a small main-branch diff auto-approves (skips review).

    Mirrors the inline gate that previously lived in ``task_lifecycle.py``. The
    caller is expected to resolve ``direction_data`` (DB I/O) and only invoke
    this on the eligible path; the eligibility conditions are re-checked here so
    the full rule is documented and testable in one place.
    """
    if is_intermediate:
        return ReviewSkipDecision(
            False,
            "intermediate branch: not a main-branch auto-approve candidate",
            "none",
        )
    if scope != "small":
        return ReviewSkipDecision(
            False,
            f"scope {scope!r} not eligible for main-branch auto-approve (small only)",
            "none",
        )
    if not _runtime_config.get("auto_approve_small_diff_enabled", True):
        return ReviewSkipDecision(
            False,
            "main-branch auto-approve disabled (auto_approve_small_diff_enabled=False)",
            "none",
        )

    # DIRECTION_ACCEPTANCE_GATE: require a *positive* acceptance signal. An
    # unknown direction or one with no merge/reject history (acceptance is None)
    # is NOT auto-approved here, intentionally stricter than the helper default.
    from app.direction_metrics import _compute_acceptance_rate

    acceptance = (
        _compute_acceptance_rate(direction_data) if direction_data is not None else None
    )
    if acceptance is None or acceptance <= AUTO_APPROVE_MIN_ACCEPTANCE_RATE:
        return ReviewSkipDecision(
            False,
            f"direction acceptance={acceptance} "
            f"(require >{AUTO_APPROVE_MIN_ACCEPTANCE_RATE:.0%} with history)",
            "direction_acceptance_gate",
            {"acceptance": acceptance},
        )

    skip, reason = should_auto_approve_diff(task, direction_data)
    if skip:
        return ReviewSkipDecision(
            True, reason, "auto_approve_small", {"acceptance": acceptance}
        )
    return ReviewSkipDecision(False, reason, "none", {"acceptance": acceptance})


# --- Rule: SECURITY_BLOCKED (re-review override) ---
# Why this rule exists:
#   When the post-fix security scan blocks the PR, the task is flagged for
#   manual approval. Neither skipping to CI-wait nor spawning a fresh re-review
#   is appropriate — the flow must halt and wait for a human. This override
#   takes precedence over every skip reason below it.
#
# When it fires: scan_blocked is True (the caller ran the scan and it blocked).
#
# What it does: returns skip=False with rule="security_blocked" so the caller
#   can distinguish "do nothing / await approval" from "spawn re-review".
def should_skip_re_review(
    fix_reason: str,
    rereview_waived: bool,
    scan_blocked: bool,
) -> ReviewSkipDecision:
    """Composite re-review decision including the security-scan override.

    Returns a :class:`ReviewSkipDecision`:
      - ``rule == "security_blocked"`` (skip=False) → the scan blocked the PR;
        the caller must NOT spawn a re-review nor park for CI. The task is
        already flagged for manual approval.
      - ``skip == True`` → mechanical or reviewer-waived fix; park for CI instead of
        re-reviewing (rule names which mechanical reason fired).
      - ``skip == False, rule == "none"`` → spawn a fresh re-review.
    """
    if scan_blocked:
        return ReviewSkipDecision(
            False,
            "security scan blocked fix output; manual approval required",
            "security_blocked",
        )
    skip, reason = should_skip_rereview(fix_reason, rereview_waived)
    if not skip:
        return ReviewSkipDecision(False, reason, "none")
    rule = (
        "reviewer_waived"
        if rereview_waived and fix_reason == "code_review"
        else fix_reason
    )
    return ReviewSkipDecision(True, reason, rule)

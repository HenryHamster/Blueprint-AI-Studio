"""Pure, synchronous review-loop heuristics.

Extracted from review_pipeline.py to keep that module below its size
guardrail. Everything here is side-effect-free and DB/async-free: it takes
data (review history, diff summaries, task context) and returns a verdict or
score. review_pipeline re-exports every public symbol below so existing
callers and tests that import from ``app.review_pipeline`` keep working.

Contains: Jaccard stuck-cycle thresholds, issue-churn detection, the
repeating-issues formatting helpers, drift-signal computation, feedback
tokenization/similarity, and the auto-approval heuristic.
"""

import re

from app.models import Task
from app.shared_state import runtime_config as _runtime_config
from app.spawn_calibration import _compute_scope_overflow_warning

STUCK_CYCLE_THRESHOLD_DEFAULT = 0.55
STUCK_CYCLE_THRESHOLD_REFACTOR = 0.65
# Medium-scope tasks share the default threshold: cross-module PRs legitimately
# need a few iterations to resolve cross-file consistency issues, and bailing
# out earlier prevented them from converging.
STUCK_CYCLE_THRESHOLD_MEDIUM = 0.55

# Token-level Jaccard thresholds used by the feedback-text fallback when one or
# both review_history entries lack a structured `issues` list (e.g. verdict
# parsing fell back to log/keyword scraping). Set higher than the structured
# thresholds because text comparison is noisier.
STUCK_CYCLE_TEXT_THRESHOLD_DEFAULT = 0.7
STUCK_CYCLE_TEXT_THRESHOLD_REFACTOR = 0.75
STUCK_CYCLE_TEXT_THRESHOLD_MEDIUM = 0.7

# Secondary Jaccard thresholds for the rephrased-issue fallback: both sides
# have structured CRITICAL/MAJOR issues but the exact-match proportion falls
# below the primary structured threshold. Stays above 0.6 so that two issues
# sharing boilerplate tokens but targeting different symbols (e.g. "Missing
# error handling in processUser" vs "…processOrder" → Jaccard 0.6) do NOT
# trigger a false positive.
STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_DEFAULT = 0.68
STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_REFACTOR = 0.7
STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_MEDIUM = 0.68


def _stuck_thresholds(scope: str, is_refactor: bool) -> tuple[float, float, float]:
    """Return (exact_match, issue_similarity, text_similarity) thresholds.

    Refactor takes priority over scope because refactor feedback structurally
    repeats. Medium scope currently matches the default; the constants are kept
    distinct so future tuning can diverge again without churning call sites.
    """
    if is_refactor:
        return (
            STUCK_CYCLE_THRESHOLD_REFACTOR,
            STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_REFACTOR,
            STUCK_CYCLE_TEXT_THRESHOLD_REFACTOR,
        )
    if scope == "medium":
        return (
            STUCK_CYCLE_THRESHOLD_MEDIUM,
            STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_MEDIUM,
            STUCK_CYCLE_TEXT_THRESHOLD_MEDIUM,
        )
    return (
        STUCK_CYCLE_THRESHOLD_DEFAULT,
        STUCK_CYCLE_ISSUE_SIMILARITY_THRESHOLD_DEFAULT,
        STUCK_CYCLE_TEXT_THRESHOLD_DEFAULT,
    )


_FEEDBACK_TOKEN_RE = re.compile(r"[a-z0-9_]+")

# Issue-churn: fix agent keeps rotating CRITICAL+MAJOR issues without the total
# count decreasing — evades Jaccard stuck-cycle detection but still unproductive.
ISSUE_CHURN_WINDOW = 2


def _critical_major_count(entry: dict) -> int:
    return sum(
        1
        for i in (entry.get("issues") or [])
        if i.get("severity", "").upper() in ("CRITICAL", "MAJOR")
        and (i.get("description") or "").strip()
    )


def _detect_issue_churn(history: list) -> tuple[bool, list[int]]:
    if len(history) < ISSUE_CHURN_WINDOW:
        return (False, [])
    counts = [_critical_major_count(e) for e in history[-ISSUE_CHURN_WINDOW:]]
    if any(c == 0 for c in counts):
        return (False, counts)
    non_decreasing = all(counts[i + 1] >= counts[i] for i in range(len(counts) - 1))
    # Oscillating patterns (e.g. [5, 3, 5], [4, 2, 4]) also represent zero net
    # progress: the fix agent removes some issues but reintroduces others.
    no_net_progress = counts[-1] >= counts[0]
    return (non_decreasing or no_net_progress, counts)


def _repeating_issue_descriptions(
    history: list, window: int = ISSUE_CHURN_WINDOW, min_occurrences: int = 2
) -> list[str]:
    """Descriptions appearing in >= min_occurrences of the last `window` entries."""
    if len(history) < min_occurrences:
        return []
    recent = history[-window:]
    desc_by_norm: dict[str, str] = {}
    per_entry: list[set[str]] = []
    for entry in recent:
        seen: set[str] = set()
        for issue in entry.get("issues") or []:
            if issue.get("severity", "").upper() not in ("CRITICAL", "MAJOR"):
                continue
            desc = (issue.get("description") or "").strip()
            if not desc:
                continue
            norm = " ".join(_FEEDBACK_TOKEN_RE.findall(desc.lower()))
            if not norm:
                continue
            seen.add(norm)
            desc_by_norm.setdefault(norm, desc)
        per_entry.append(seen)
    counts: dict[str, int] = {}
    for s in per_entry:
        for n in s:
            counts[n] = counts.get(n, 0) + 1
    repeating = [desc_by_norm[n] for n, c in counts.items() if c >= min_occurrences]
    return repeating[:5]


def _format_repeating_issues_section(history: list) -> str:
    repeating = _repeating_issue_descriptions(history)
    if not repeating:
        return ""
    bullets = "\n".join(f"- {d}" for d in repeating)
    return (
        "Repeating issues from prior iterations (prioritize breaking the cycle "
        "on these exact issues):\n" + bullets + "\n\n"
    )


def compute_drift_signal(
    review_history: list,
    planned_files: list,
    current_files: list,
) -> dict:
    """Compute drift_in / drift_out for the trajectory section in fix prompts.

    drift_in = files in the current diff that have NOT appeared in the planned
    list and have NOT been part of any APPROVE iteration's diff. These are the
    candidates for "leftover from a rebase or earlier fix that nobody reverted"
    — the failure mode behind PR #3435 / PR #3408.

    drift_out = files that WERE planned or part of an approved iteration's diff
    but are missing from the current diff. These are the candidates for "lost
    during a rebase fix" (e.g. PR #3435's iter-1-approved test changes that
    iter-2's rebase clobbered).

    Each drift_in entry is annotated with first_appeared_iteration (when the
    file first showed up in any review_history entry's changed_files) and
    flag_count (how many REQUEST_CHANGES iterations cited the file in issues).
    """
    approved_files: set[str] = set()
    file_first_seen: dict[str, int] = {}
    file_flag_count: dict[str, int] = {}
    for entry in review_history or []:
        if not isinstance(entry, dict):
            continue
        cf = entry.get("changed_files") or []
        verdict = entry.get("verdict")
        iteration_num = entry.get("iteration")
        for f in cf:
            if not isinstance(f, str) or not f:
                continue
            if f not in file_first_seen and isinstance(iteration_num, int):
                file_first_seen[f] = iteration_num
            if verdict == "APPROVE":
                approved_files.add(f)
        # Count REQUEST_CHANGES iterations that flagged a specific file via
        # an issue's `file` field — distinguishes "merely changed" from
        # "actively rejected by a reviewer."
        if verdict == "REQUEST_CHANGES":
            for issue in entry.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                fname = issue.get("file")
                if isinstance(fname, str) and fname:
                    file_flag_count[fname] = file_flag_count.get(fname, 0) + 1

    planned_set = {f for f in (planned_files or []) if isinstance(f, str) and f}
    current_set = {f for f in (current_files or []) if isinstance(f, str) and f}

    drift_in_files = current_set - (planned_set | approved_files)
    drift_in = [
        {
            "file": f,
            "first_appeared_iteration": file_first_seen.get(f),
            "flag_count": file_flag_count.get(f, 0),
        }
        for f in sorted(drift_in_files)
    ]
    drift_out = sorted((planned_set | approved_files) - current_set)
    return {"drift_in": drift_in, "drift_out": drift_out}


def _is_test_file(path: str) -> bool:
    p = path.lower()
    if (
        "/testing/" in p
        or p.startswith("testing/")
        or "/tests/" in p
        or p.startswith("tests/")
    ):
        return True
    name = p.rsplit("/", 1)[-1]
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return ".test." in name or ".spec." in name


# Minimum direction acceptance rate required to auto-approve a small diff.
# Directions with poor merge history still get a full review regardless of
# diff size — the acceptance gate is independent of the line/file thresholds.
AUTO_APPROVE_MIN_ACCEPTANCE_RATE = 0.5


def _diff_lines_changed(
    diff_summary: list | None, exclude_test_files: bool = False
) -> int | None:
    """Sum added+removed across a diff summary list.

    Returns None when the summary is missing or malformed, so callers can
    fall through to the full review path when diff size is unknown.
    If exclude_test_files is True, skips entries where the file is a test file.
    """
    if not diff_summary or not isinstance(diff_summary, list):
        return None
    total = 0
    for entry in diff_summary:
        if not isinstance(entry, dict):
            return None
        if exclude_test_files:
            file_path = entry.get("file", "")
            if isinstance(file_path, str) and _is_test_file(file_path):
                continue
        total += int(entry.get("added") or 0) + int(entry.get("removed") or 0)
    return total


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


def _feedback_token_set(text: str) -> set[str]:
    return {t for t in _FEEDBACK_TOKEN_RE.findall(text.lower()) if len(t) >= 3}


def _feedback_jaccard(a: str, b: str) -> float:
    tokens_a = _feedback_token_set(a)
    tokens_b = _feedback_token_set(b)
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)

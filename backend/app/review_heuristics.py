"""Pure, synchronous review-loop heuristics.

Extracted from review_pipeline.py to keep that module below its size
guardrail. Everything here is side-effect-free and DB/async-free: it takes
data (review history, diff summaries, task context) and returns a verdict or
score. review_pipeline re-exports every public symbol below so existing
callers and tests that import from ``app.review_pipeline`` keep working.

Contains: Jaccard stuck-cycle thresholds, issue-churn detection, the
repeating-issues formatting helpers, drift-signal computation, feedback
tokenization/similarity, and the diff-size/test-file utilities shared with
``review_policy`` (which owns the review-skip decisions).
"""

import re

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


# ── Early non-convergence escalation (whack-a-mole / drift detection) ─────────
# The stuck-cycle / standoff / churn detectors all MISS the whack-a-mole shape:
#   • stuck/standoff need the SAME issue re-raised; whack-a-mole flags different
#     sibling sites each round, so they never fire.
#   • churn only fires on NON-DECREASING counts, so a shrinking-but-never-zero
#     sequence like [3, 2, 2, 1, 1] slips through and grinds all 16 rounds.
# This detector catches three shapes the others miss and escalates to the
# operator at iteration K (≈8) instead of grinding to MAX_REVIEW_FIX_ITERATIONS:
#   (1) PLATEAU: over the last N rounds the actionable (CRITICAL+MAJOR) count is
#       low-but-nonzero and never reaches 0 (slow whittling that never lands).
#   (2) VERIFICATION-ONLY DRIFT: M consecutive REQUEST_CHANGES rounds produce no
#       genuinely-NEW issue description (the reviewer is re-checking, not finding
#       new defects — convergence has effectively stalled).
#   (3) ONE-AT-A-TIME DISCLOSURE: the total DISTINCT issue-descriptions seen
#       across the whole loop greatly exceeds any single round's count (the
#       reviewer is disclosing the issue-class one site per round).
EARLY_NONCONVERGENCE_ESCALATE_AT = 8  # K: iteration to escalate on a hit.
EARLY_NONCONVERGENCE_PLATEAU_WINDOW = 4  # N: rounds inspected for a plateau.
# A plateau round count above 0 and at/below this is "low-but-nonzero".
EARLY_NONCONVERGENCE_PLATEAU_MAX_COUNT = 3
EARLY_NONCONVERGENCE_VERIFY_DRIFT_ROUNDS = 3  # M: consecutive no-new-issue rounds.
# One-at-a-time multiplier: distinct-descriptions-total >= this * max-single-round
# (with a small absolute floor) signals serial disclosure of one issue-class.
EARLY_NONCONVERGENCE_DISCLOSURE_RATIO = 3
EARLY_NONCONVERGENCE_DISCLOSURE_MIN_DISTINCT = 5


def _rejection_round_counts(history: list) -> list[int]:
    """CRITICAL+MAJOR issue count for each REQUEST_CHANGES round, in order."""
    counts: list[int] = []
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        verdict = (entry.get("verdict") or "").upper()
        # Skip APPROVE rows; count only rejection rounds (REQUEST_CHANGES,
        # REJECT, or rows that simply carry issues without an explicit verdict).
        if verdict == "APPROVE":
            continue
        counts.append(_critical_major_count(entry))
    return counts


def _distinct_rejection_descriptions(history: list) -> set[str]:
    """Set of normalized CRITICAL+MAJOR descriptions across all rejection rounds."""
    distinct: set[str] = set()
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        if (entry.get("verdict") or "").upper() == "APPROVE":
            continue
        for issue in entry.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            if (issue.get("severity") or "").upper() not in ("CRITICAL", "MAJOR"):
                continue
            desc = (issue.get("description") or "").strip()
            if not desc:
                continue
            norm = " ".join(_FEEDBACK_TOKEN_RE.findall(desc.lower()))
            if norm:
                distinct.add(norm)
    return distinct


def detect_early_nonconvergence(history: list) -> tuple[bool, str]:
    """Detect the whack-a-mole / drift non-convergence shape the stuck-cycle,
    standoff, and churn detectors all miss.

    Returns ``(should_escalate, reason)``. The caller decides WHETHER to act on
    it based on the current iteration (escalate at K). This function only
    classifies the trajectory shape; it does not read iteration state.
    """
    rejection_counts = _rejection_round_counts(history)
    # Need a few rejection rounds before any trajectory shape is meaningful.
    if len(rejection_counts) < EARLY_NONCONVERGENCE_VERIFY_DRIFT_ROUNDS:
        return (False, "")

    # (1) PLATEAU: last N rounds all low-but-nonzero (never reaches 0). The
    # churn detector misses this because the counts are (weakly) decreasing.
    window = rejection_counts[-EARLY_NONCONVERGENCE_PLATEAU_WINDOW:]
    if len(window) >= EARLY_NONCONVERGENCE_PLATEAU_WINDOW and all(
        0 < c <= EARLY_NONCONVERGENCE_PLATEAU_MAX_COUNT for c in window
    ):
        return (
            True,
            f"plateau: last {len(window)} rounds low-but-nonzero {window} "
            f"(never reached 0)",
        )

    # (2) VERIFICATION-ONLY DRIFT: the last M rejection rounds introduce no
    # genuinely-new issue description (each round's normalized descriptions are
    # a subset of what earlier rounds already raised). Convergence has stalled —
    # the reviewer is re-verifying, not finding new defects.
    rejection_entries = [
        e
        for e in (history or [])
        if isinstance(e, dict) and (e.get("verdict") or "").upper() != "APPROVE"
    ]
    if len(rejection_entries) > EARLY_NONCONVERGENCE_VERIFY_DRIFT_ROUNDS:

        def _norm_descs(entry: dict) -> set[str]:
            out: set[str] = set()
            for issue in entry.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                if (issue.get("severity") or "").upper() not in ("CRITICAL", "MAJOR"):
                    continue
                desc = (issue.get("description") or "").strip()
                if desc:
                    norm = " ".join(_FEEDBACK_TOKEN_RE.findall(desc.lower()))
                    if norm:
                        out.add(norm)
            return out

        recent = rejection_entries[-EARLY_NONCONVERGENCE_VERIFY_DRIFT_ROUNDS:]
        earlier = rejection_entries[:-EARLY_NONCONVERGENCE_VERIFY_DRIFT_ROUNDS]
        seen_before: set[str] = set()
        for e in earlier:
            seen_before |= _norm_descs(e)
        no_new_each_round = True
        for e in recent:
            descs = _norm_descs(e)
            # A round with structured issues none of which are new = no progress.
            if descs and not (descs - seen_before):
                seen_before |= descs
                continue
            if descs:  # at least one genuinely new issue this round
                no_new_each_round = False
                break
            # No structured issues this round — can't confirm drift, bail.
            no_new_each_round = False
            break
        if no_new_each_round:
            return (
                True,
                f"verification-only drift: last "
                f"{EARLY_NONCONVERGENCE_VERIFY_DRIFT_ROUNDS} rounds raised no "
                f"genuinely-new issue",
            )

    # (3) ONE-AT-A-TIME DISCLOSURE: total distinct descriptions across the loop
    # greatly exceeds any single round's count — the reviewer is disclosing the
    # issue-class one site per round.
    distinct_total = len(_distinct_rejection_descriptions(history))
    max_single_round = max(rejection_counts) if rejection_counts else 0
    if (
        distinct_total >= EARLY_NONCONVERGENCE_DISCLOSURE_MIN_DISTINCT
        and max_single_round > 0
        and distinct_total >= EARLY_NONCONVERGENCE_DISCLOSURE_RATIO * max_single_round
    ):
        return (
            True,
            f"one-at-a-time disclosure: {distinct_total} distinct issues across "
            f"the loop vs max {max_single_round}/round",
        )

    return (False, "")


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


# ── FIX-ALL-OCCURRENCES directive (whack-a-mole prevention) ───────────────────
# The dominant waste source in the review→fix loop is "whack-a-mole": the
# reviewer flags ONE instance of a file-spanning pattern (e.g. a deprecated
# call, a naive timestamp, an unguarded access), the fix agent migrates exactly
# that line — pushed there by the narrow-focus guardrails — and the next review
# finds the next sibling site. Each round costs a full review+fix VM cycle.
#
# When the flagged issues look like instances of a SINGLE issue-class, we inject
# a directive telling the fix agent to grep for and fix EVERY occurrence in this
# pass. This is "completing the fix", not incidental refactoring, so it is
# explicitly reconciled with the narrow-focus rules below.

# Minimum count of CRITICAL/MAJOR issues sharing one normalized description stem
# to treat them as a single file-spanning class.
FIX_ALL_MIN_SHARED_STEM = 2
# Description stem length (leading normalized tokens) used to group issues into
# a class. Short enough to catch "Use X instead of Y in foo" / "...in bar" as
# one class, long enough not to collapse genuinely-distinct issues.
FIX_ALL_STEM_TOKENS = 5

# Pattern cues in issue text that explicitly signal a file-spanning class even
# when only one instance is enumerated (reviewers often say "this is one of
# several" rather than listing them all).
_FIX_ALL_PATTERN_CUES = (
    "one instance of a pattern",
    "every other call site",
    "all instances",
    "other call sites",
    "elsewhere",
    "same pattern",
    "throughout the",
    "across the",
    "in several places",
    "multiple places",
    "rest of the",
    "remaining call sites",
)

_FIX_ALL_OCCURRENCES_DIRECTIVE = (
    "🔁 FIX ALL OCCURRENCES: the findings above are instances of a SINGLE "
    "issue-class that spans the repo, not isolated one-off bugs. Before "
    "./done, grep the repo for EVERY occurrence of this pattern (not just the "
    "exact file:line cited) and fix them ALL in this pass — a partial "
    "migration will be re-flagged on the next review and waste an entire "
    "iteration. Migrating the sibling call sites of THIS flagged class is "
    "IN SCOPE: it completes the fix, it is not the incidental refactoring the "
    "narrow-focus guidance warns against. Where this directive and a "
    "narrow-focus rule conflict, this directive wins FOR SITES OF THE FLAGGED "
    "CLASS specifically — fully migrate the class, while still leaving "
    "unrelated cleanups/refactors for follow-up tasks.\n\n"
)

# Marker substring used to detect, downstream in _spawn_pipeline_task, that the
# fix-prompt builder already injected the fix-all directive — so the
# narrow-focus prefixes can append a reconciliation clause instead of silently
# contradicting it.
_FIX_ALL_DIRECTIVE_MARKER = "FIX ALL OCCURRENCES"

# Reconciliation clause appended to a narrow-focus prefix when the fix-all
# directive is also active. Makes the precedence explicit so the fix agent
# doesn't read "avoid modifying files not referenced by the issues" as a veto
# on migrating the flagged class's sibling sites.
_FIX_ALL_NARROW_FOCUS_RECONCILE = (
    "(Exception: a FIX-ALL-OCCURRENCES directive is also active below. "
    "Migrating EVERY site of the flagged issue-class is in scope and takes "
    "precedence over the 'avoid modifying files not referenced by the issues' "
    "rule for those sites — that is completing the fix, not the incidental "
    "refactoring this guidance restricts.)\n\n"
)


def _reconcile_narrow_focus_with_fix_all(prefix: str, fix_prompt: str | None) -> str:
    """Append the fix-all reconciliation clause to ``prefix`` when the fix
    prompt already carries the fix-all directive. No-op otherwise."""
    if fix_prompt and _FIX_ALL_DIRECTIVE_MARKER in fix_prompt:
        return prefix + _FIX_ALL_NARROW_FOCUS_RECONCILE
    return prefix


def _normalized_issue_stem(description: str, stem_tokens: int) -> str:
    """Leading ``stem_tokens`` normalized tokens of an issue description.

    Tokenizes the same way as the stuck-cycle / repeating-issue helpers
    (``_FEEDBACK_TOKEN_RE``) so a class like "Use timezone-aware datetime in
    foo" / "...in bar" collapses to a shared stem regardless of trailing
    file/symbol differences.
    """
    if not description:
        return ""
    tokens = _FEEDBACK_TOKEN_RE.findall(description.lower())
    return " ".join(tokens[:stem_tokens])


def detect_fix_all_issue_class(issues: list | None) -> bool:
    """True when the reviewer's flagged issues look like instances of ONE
    file-spanning issue-class — the signal to inject the fix-all-occurrences
    directive into the fix prompt.

    Triggers when EITHER:
      • ≥ ``FIX_ALL_MIN_SHARED_STEM`` CRITICAL/MAJOR issues share a normalized
        description stem (the same fix applied at several sites), OR
      • any issue's description contains a pattern cue ("all instances",
        "other call sites", "elsewhere", …) explicitly stating the finding is
        one of several.

    Minor-only sets and a single unrelated issue do NOT trigger — the directive
    must not turn an isolated nit into a repo-wide grep mandate.
    """
    if not issues:
        return False
    stem_counts: dict[str, int] = {}
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if (issue.get("severity") or "").lower() not in ("critical", "major"):
            continue
        desc = (issue.get("description") or "").strip()
        if not desc:
            continue
        low = desc.lower()
        if any(cue in low for cue in _FIX_ALL_PATTERN_CUES):
            return True
        stem = _normalized_issue_stem(desc, FIX_ALL_STEM_TOKENS)
        if stem:
            stem_counts[stem] = stem_counts.get(stem, 0) + 1
    return any(c >= FIX_ALL_MIN_SHARED_STEM for c in stem_counts.values())


def fix_all_occurrences_directive() -> str:
    """The directive text injected into the fix prompt when
    ``detect_fix_all_issue_class`` fires. Exposed as a function so callers don't
    import the private constant directly."""
    return _FIX_ALL_OCCURRENCES_DIRECTIVE


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


def _feedback_token_set(text: str) -> set[str]:
    return {t for t in _FEEDBACK_TOKEN_RE.findall(text.lower()) if len(t) >= 3}


def _feedback_jaccard(a: str, b: str) -> float:
    tokens_a = _feedback_token_set(a)
    tokens_b = _feedback_token_set(b)
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


# ── Per-lane iteration cap accounting ─────────────────────────────────────────
# Review-fix iteration budget lanes — mechanical churn (conflict resolution,
# CI fixes, second-opinion arbitration) must not crowd out review-convergence
# work. Each lane has a sub-cap; non-review lanes share a collective ceiling so
# review_convergence always retains at least (global_cap - collective_ceil)
# iterations regardless of churn volume.
#
# Lane values:
#   conflict         — merge_conflict or update_branch_failed fix iterations
#   ci               — ci_failure fix iterations
#   arbitration      — panel_review (second-opinion/dispute) iterations
#   review_convergence — everything else (code-review fix + review tasks)
LANE_CONFLICT = "conflict"
LANE_CI = "ci"
LANE_ARBITRATION = "arbitration"
LANE_REVIEW_CONVERGENCE = "review_convergence"

# Default sub-caps (runtime-config-tunable; see review_pipeline._LANE_CAPS).
LANE_CAP_CONFLICT_DEFAULT = 4
LANE_CAP_CI_DEFAULT = 6
LANE_CAP_ARBITRATION_DEFAULT = 2
LANE_CAP_NON_REVIEW_DEFAULT = 8  # conflict + ci + arbitration collectively

# Number of consecutive conflict-lane fix completions that trigger persistent-
# conflict escalation BEFORE the lane sub-cap fires. The loop closes the PR and
# records failure_detail="persistent_merge_conflict" on the orig task so the
# operator can see the conflict, not a generic cap-exhaustion message.
# Fires at lane_count >= threshold (default 2) so the first conflict attempt
# is preserved (keeps hardened prompt behavior for attempt 1), and a second
# failure without resolution confirms the conflict is not auto-resolvable.
PERSISTENT_CONFLICT_ESCALATION_THRESHOLD = 2


def classify_iteration_lane(task: "Task") -> str:  # type: ignore[name-defined]  # noqa: F821
    """Classify a completed/failed pipeline task into its review-fix lane.

    Used to compute per-lane iteration counts so sub-caps can be applied
    alongside the global cap. The classifier is a pure function of the task's
    payload type and ``review_loop.fix_reason``.
    """
    payload = (task.first_payload or {}) if hasattr(task, "first_payload") else {}
    payload_type = payload.get("type", "")
    if payload_type == "panel_review":
        return LANE_ARBITRATION
    fix_reason = (
        (task.context or {}).get("review_loop", {}).get("fix_reason", "code_review")
    )
    if fix_reason in ("merge_conflict", "update_branch_failed"):
        return LANE_CONFLICT
    if fix_reason == "ci_failure":
        return LANE_CI
    return LANE_REVIEW_CONVERGENCE

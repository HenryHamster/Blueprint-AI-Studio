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

import hashlib
import json
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

# Cross-PR repeat-concern replay deliberately uses a lower threshold than the
# intra-PR stuck-cycle detector: it compares one finding across independently
# merged PRs, where wording changes are expected. The file path remains the
# hard identity boundary; a symbol, when both reviews provide one, is also
# required to match.
CROSS_PR_CONCERN_JACCARD_THRESHOLD = 0.5
CROSS_PR_MIN_DISTINCT_PRS = 2
_CROSS_PR_ACTIONABLE_SEVERITIES = frozenset({"CRITICAL", "MAJOR"})


def _critical_major_count(entry: dict) -> int:
    return sum(
        1
        for i in (entry.get("issues") or [])
        if i.get("severity", "").upper() in ("CRITICAL", "MAJOR")
        and (i.get("description") or "").strip()
    )


def _entry_target_pair(entry: dict) -> tuple[str, str] | None:
    base_sha = str(entry.get("reviewed_base_sha") or "").strip()
    head_sha = str(entry.get("reviewed_head_sha") or "").strip()
    if not base_sha or not head_sha:
        return None
    return (base_sha, head_sha)


def _verified_blocking_issue_ids(entry: dict) -> set[str] | None:
    """Return canonical blocker IDs when the round proves an exact target.

    ``None`` means the older count-only fallback must remain in force. An empty
    set is valid exact evidence (an approval/no-blocker round).
    """

    if (
        entry.get("target_verified_by_backend") is not True
        or not str(entry.get("review_task_id") or "").strip()
        or not _entry_target_pair(entry)
    ):
        return None
    issue_ids: set[str] = set()
    for issue in entry.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        if issue.get("severity", "").upper() not in ("CRITICAL", "MAJOR"):
            continue
        if not (issue.get("description") or "").strip():
            continue
        issue_id = str(issue.get("issue_id") or "").strip()
        if not issue_id:
            return None
        issue_ids.add(issue_id)
    return issue_ids


def _detect_issue_churn(history: list) -> tuple[bool, list[int]]:
    if len(history) < ISSUE_CHURN_WINDOW:
        return (False, [])
    window = history[-ISSUE_CHURN_WINDOW:]
    counts = [_critical_major_count(e) for e in window]
    if any(c == 0 for c in counts):
        return (False, counts)
    non_decreasing = all(counts[i + 1] >= counts[i] for i in range(len(counts) - 1))
    # Oscillating patterns (e.g. [5, 3, 5], [4, 2, 4]) also represent zero net
    # progress: the fix agent removes some issues but reintroduces others.
    no_net_progress = counts[-1] >= counts[0]
    count_shape_is_churn = non_decreasing or no_net_progress
    if not count_shape_is_churn:
        return (False, counts)

    canonical_ids = [_verified_blocking_issue_ids(entry) for entry in window]
    if all(issue_ids is not None for issue_ids in canonical_ids):
        canonical_id_sets = [
            issue_ids for issue_ids in canonical_ids if issue_ids is not None
        ]
        target_pairs = [
            pair
            for entry in window
            if (pair := _entry_target_pair(entry)) is not None
        ]
        heads_changed = all(
            target_pairs[index][1] != target_pairs[index + 1][1]
            for index in range(len(target_pairs) - 1)
        ) and len(target_pairs) == len(window)
        current_ids = canonical_id_sets[-1]
        prior_canonical_ids: set[str] = set()
        for prior_entry in history[:-1]:
            prior_ids = _verified_blocking_issue_ids(prior_entry)
            if prior_ids is not None:
                prior_canonical_ids.update(prior_ids)
        re_raised = bool(current_ids & prior_canonical_ids)
        if heads_changed and not re_raised:
            # Every retained canonical blocker disappeared on an actioned head
            # and every current blocker is genuinely new. Count growth here is
            # reviewer discovery, not failed repair progress; shortening the
            # patch budget would punish convergence. Comparing the current set
            # with all bounded retained history also catches A -> B -> A loops.
            return (False, counts)
    return (True, counts)


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


def _cross_pr_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _cross_pr_record_pr_number(record: dict) -> int | None:
    """Read a PR number from normalized or historical task-shaped records."""
    context = record.get("context") or {}
    context = context if isinstance(context, dict) else {}
    review_loop = context.get("review_loop") or {}
    review_loop = review_loop if isinstance(review_loop, dict) else {}
    payloads = record.get("payloads") or []
    payload = payloads[0] if payloads and isinstance(payloads[0], dict) else {}
    for value in (
        record.get("pr_number"),
        review_loop.get("pr_number"),
        payload.get("pr_number"),
        context.get("rollup_pr_number"),
    ):
        number = _cross_pr_int(value)
        if number is not None:
            return number
    return None


def _cross_pr_review_history(record: dict) -> list:
    direct = record.get("review_history")
    if isinstance(direct, list):
        return direct
    context = record.get("context") or {}
    context = context if isinstance(context, dict) else {}
    review_loop = context.get("review_loop") or {}
    review_loop = review_loop if isinstance(review_loop, dict) else {}
    history = review_loop.get("review_history")
    return history if isinstance(history, list) else []


def _cross_pr_file(issue: dict) -> str:
    path = issue.get("file") or issue.get("file_path") or ""
    if not isinstance(path, str):
        return ""
    return path.strip().replace("\\", "/").removeprefix("./")


def _cross_pr_symbol(issue: dict) -> str:
    symbol = issue.get("symbol") or issue.get("region") or ""
    return str(symbol).strip() if symbol else ""


def _cross_pr_description(issue: dict) -> str:
    description = issue.get("description") or issue.get("summary") or ""
    return str(description).strip() if description else ""


def _cross_pr_concern_similarity(left: dict, right: dict) -> float:
    if left["file"] != right["file"]:
        return 0.0
    if left["symbol"] and right["symbol"] and left["symbol"] != right["symbol"]:
        return 0.0
    return _feedback_jaccard(left["description"], right["description"])


def detect_cross_pr_repeat_concern_chains(
    records: list[dict],
    *,
    min_distinct_prs: int = CROSS_PR_MIN_DISTINCT_PRS,
    similarity_threshold: float = CROSS_PR_CONCERN_JACCARD_THRESHOLD,
) -> dict:
    """Replay repeat-concern identity across distinct PRs without side effects.

    ``records`` are task-shaped dictionaries. Each record may expose
    ``review_history`` directly or under ``context.review_loop`` and must carry
    a PR number directly, in that review loop, or in its first payload. A group
    is a repeat-concern chain only when the same file and compatible region
    recur on at least ``min_distinct_prs`` PRs. Repeated observations on one PR
    are retained as iterative-hardening evidence, never as cross-PR burden.
    """
    groups: list[dict] = []
    malformed_records = 0
    concern_observations = 0

    for record_index, record in enumerate(records or []):
        if not isinstance(record, dict):
            malformed_records += 1
            continue
        pr_number = _cross_pr_record_pr_number(record)
        history = _cross_pr_review_history(record)
        if pr_number is None or not history:
            malformed_records += 1
            continue
        for entry in history:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("verdict") or "").upper() == "APPROVE":
                continue
            iteration = _cross_pr_int(entry.get("iteration"))
            iteration = iteration if iteration is not None else 0
            for issue in entry.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                severity = str(issue.get("severity") or "").upper()
                description = _cross_pr_description(issue)
                file_path = _cross_pr_file(issue)
                if severity not in _CROSS_PR_ACTIONABLE_SEVERITIES or not description or not file_path:
                    continue
                concern = {
                    "pr_number": pr_number,
                    "iteration": iteration,
                    "severity": severity,
                    "description": description,
                    "file": file_path,
                    "symbol": _cross_pr_symbol(issue),
                    "record_index": record_index,
                }
                concern_observations += 1
                group = next(
                    (
                        candidate
                        for candidate in groups
                        if any(
                            _cross_pr_concern_similarity(observation, concern)
                            >= similarity_threshold
                            for observation in candidate["observations"]
                        )
                    ),
                    None,
                )
                if group is None:
                    groups.append(
                        {
                            "representative": concern,
                            "pr_numbers": [pr_number],
                            "iterations_by_pr": {str(pr_number): [iteration]},
                            "severities": [severity],
                            "descriptions": [description],
                            "observations": [concern],
                        }
                    )
                    continue
                if pr_number not in group["pr_numbers"]:
                    group["pr_numbers"].append(pr_number)
                group["iterations_by_pr"].setdefault(str(pr_number), []).append(iteration)
                if severity not in group["severities"]:
                    group["severities"].append(severity)
                if description not in group["descriptions"] and len(group["descriptions"]) < 5:
                    group["descriptions"].append(description)
                group["observations"].append(concern)

    chain_groups = [
        group
        for group in groups
        if len(group["pr_numbers"]) >= max(2, int(min_distinct_prs))
    ]
    hardening_groups = [
        group
        for group in groups
        if len(group["pr_numbers"]) == 1
        and len(set(group["iterations_by_pr"][str(group["pr_numbers"][0])])) > 1
    ]

    chains: list[dict] = []
    for group in chain_groups:
        representative = group["representative"]
        normalized_tokens = " ".join(
            sorted(_feedback_token_set(representative["description"]))
        )
        # The grouping identity is (file, compatible symbol, description
        # tokens) at a given similarity threshold — see
        # _cross_pr_concern_similarity, which refuses to merge two concerns
        # that carry different symbols. The key must hash every one of those
        # components, or two independent chains on the same file (different
        # symbols) collapse onto one key and any consumer keying escalation or
        # dedup on chain_key silently merges them.
        identity = (
            f"{representative['file']}"
            f"|{representative['symbol']}"
            f"|{normalized_tokens}"
            f"|{float(similarity_threshold):.4f}"
        )
        chain = {
            "chain_key": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
            "file": representative["file"],
            "symbol": representative["symbol"] or None,
            "representative_description": representative["description"],
            "pr_numbers": list(group["pr_numbers"]),
            "distinct_prs": len(group["pr_numbers"]),
            "occurrences": len(group["pr_numbers"]),
            "iterations_by_pr": {
                pr: sorted(set(iterations))
                for pr, iterations in group["iterations_by_pr"].items()
            },
            "severities": sorted(group["severities"]),
            "descriptions": list(group["descriptions"]),
            "resolved_before_next_pr": max(0, len(group["pr_numbers"]) - 1),
            "same_pr_hardening_prs": sorted(
                int(pr)
                for pr, iterations in group["iterations_by_pr"].items()
                if len(set(iterations)) > 1
            ),
        }
        chains.append(chain)

    return {
        "records_seen": len(records or []),
        "concern_observations": concern_observations,
        "malformed_records": malformed_records,
        "concern_groups": len(groups),
        "repeat_concern_chain_count": len(chains),
        "repeat_concern_chains": chains,
        "same_pr_hardening_count": len(hardening_groups),
        "resolved_before_next_pr": sum(
            chain["resolved_before_next_pr"] for chain in chains
        ),
        "similarity_threshold": similarity_threshold,
    }


# ── Mechanical hedge detection (anti-hedging enforcement) ─────────────────────
# Anti-hedging principle: new features, gates, and flags must default ON/committed,
# not observe/off/disabled/fail-open, without documented justification.
# This regex matches assignments where the variable name (or a keyword like
# "default", "mode", "flag", "gate", "feature", "pilot", "lane") is followed
# by one of the hedge string values.
_HEDGE_DEFAULT_VALUE_RE = re.compile(
    r"(?:default|mode|flag|gate|feature|pilot|lane)\s*[=:]\s*"
    r"[\"'](observe|off|disabled|fail-?open)[\"']",
    re.IGNORECASE,
)


def detect_hedges_in_diff(diff_files: list[dict]) -> list[dict]:
    """Scan PR diff files for unjustified default-off/observe/fail-open hedges.

    Inspects each file's ``patch`` field (unified diff text available when
    ``get_pr_files`` is called with ``include_patch=True``) for added lines
    that introduce a new flag, gate, or mode defaulting to ``observe``,
    ``off``, ``disabled``, or ``fail-open``. These are the default-off/hedge
    patterns the anti-hedging principle (ANTI_HEDGE_FOCUS / REVIEW_ANTI_HEDGE_CRITERION)
    defines as 'guilty until proven innocent'.

    Returns a list of issue dicts with ``severity`` set to ``"MAJOR"`` (so
    they are treated as genuine blocking issues by the review pipeline's
    reconciliation logic). Returns an empty list when no hedge patterns are
    found.

    The caller is responsible for providing ``patch`` fields (via
    ``include_patch=True``) and for merging returned findings into the review
    issues list.
    """
    findings: list[dict] = []
    for file_entry in diff_files or []:
        if not isinstance(file_entry, dict):
            continue
        filename = file_entry.get("filename", "")
        if not isinstance(filename, str):
            continue
        patch = file_entry.get("patch")
        if not isinstance(patch, str) or not patch:
            continue
        for raw_line in patch.splitlines():
            if not raw_line.startswith("+"):
                continue
            # Strip the leading '+' to get the added line content.
            added = raw_line[1:]
            stripped = added.strip()
            if not stripped:
                continue
            # Skip comments, docstrings, and import lines.
            if stripped.startswith(("#", '"""', "'''", "//", "*", "/*")):
                continue
            if stripped.startswith("import ") or stripped.startswith("from "):
                continue
            match = _HEDGE_DEFAULT_VALUE_RE.search(stripped)
            if match:
                hedge_value = match.group(1)
                # Store with a descriptive issue format matching ANTI_HEDGE_FOCUS.
                findings.append({
                    "severity": "MAJOR",
                    "file": filename,
                    "line": 0,
                    "description": (
                        f"Unjustified hedge: new flag/gate defaults to "
                        f"'{hedge_value}' — anti-hedging principle requires "
                        f"new features to default ON/committed, not observe/"
                        f"off/disabled/fail-open, without documented justification."
                    ),
                })
    return findings


# ── Per-lane iteration cap accounting ─────────────────────────────────────────
# Review-fix iteration budget lanes — mechanical churn (conflict resolution
# and CI fixes) must not crowd out review-convergence
# work. Each lane has a sub-cap; non-review lanes share a collective ceiling so
# review_convergence always retains at least (global_cap - collective_ceil)
# iterations regardless of churn volume.
#
# Lane values:
#   conflict         — merge_conflict or update_branch_failed fix iterations
#   ci               — ci_failure fix iterations
#   review_convergence — everything else (code-review fix + review tasks)
LANE_CONFLICT = "conflict"
LANE_CI = "ci"
LANE_REVIEW_CONVERGENCE = "review_convergence"

# Default sub-caps (runtime-config-tunable; see review_pipeline._LANE_CAPS).
LANE_CAP_CONFLICT_DEFAULT = 4
LANE_CAP_CI_DEFAULT = 6
LANE_CAP_NON_REVIEW_DEFAULT = 8  # conflict + ci collectively

# Number of consecutive conflict-lane fix completions that trigger persistent-
# conflict escalation BEFORE the lane sub-cap fires. The loop parks the open PR
# in the operator-disposition hold with a persistent-conflict marker and exact
# target evidence; conflict alone is not authority to reject or close the PR.
# Fires at lane_count >= threshold (default 2) so the first conflict attempt
# is preserved (keeps hardened prompt behavior for attempt 1), and a second
# failure without resolution confirms the conflict is not auto-resolvable.
PERSISTENT_CONFLICT_ESCALATION_THRESHOLD = 2


# ── Anti-hedging: detect unjustified default-off / observe / fail-open ─────────
# A "hedge" is a newly-introduced default-off flag, observe-mode lane, or
# fail-open behavior with no explicit written justification. The review checklist
# instructs reviewers to flag these as MAJOR. This detector confirms the finding
# programmatically by scanning CRITICAL/MAJOR issue descriptions for hedge-cue
# patterns. A matching issue that also contains a justification keyword is allowed
# through — the reviewer saw the hedge and confirmed it has a rationale.
_HEDGE_CUE_RES = [
    re.compile(r"default(?:s)?\s+(?:to\s+)?(?:off|false|disabled)", re.IGNORECASE),
    re.compile(r"observe[_-]?mode", re.IGNORECASE),
    re.compile(r"fail[_-]?open", re.IGNORECASE),
    re.compile(r"feature\s+flag\s+.*(?:off|disabled|false)", re.IGNORECASE),
    re.compile(r"disabled\s+by\s+default", re.IGNORECASE),
    re.compile(r"\bunjustified\s+(?:hedge|default|flag|observe|mode)\b", re.IGNORECASE),
    re.compile(r"no\s+(?:written\s+)?justification", re.IGNORECASE),
    re.compile(r"without\s+(?:an?\s+)?(?:written\s+)?justification", re.IGNORECASE),
]

_HEDGE_JUSTIFIED_RE = re.compile(
    r"\b(?:justified\s+by|justification\s+in|intentional(?:ly)?|documented\s+(?:in|reason)"
    r"|by\s+design|staged\s+rollout|rationale\s*:)\b",
    re.IGNORECASE,
)


def detect_unjustified_hedge(issues: list) -> tuple[bool, str]:
    """Scan reviewer issues for unjustified-hedge findings.

    Returns ``(True, reason)`` when a CRITICAL or MAJOR issue description
    matches a hedge-cue pattern (default-off, observe mode, fail-open, …) and
    does NOT contain a justification keyword indicating the hedge was accepted
    as intentional. Returns ``(False, "")`` otherwise.

    Intended use: the review pipeline calls this after the model reviewer
    produces its issue list to confirm the anti-hedge checklist rule fired and
    to prevent auto-approval of PRs that introduce unjustified hedges.
    """
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        severity = (issue.get("severity") or "").lower()
        if severity not in ("critical", "major"):
            continue
        desc = (issue.get("description") or "").strip()
        if not desc:
            continue
        if _HEDGE_JUSTIFIED_RE.search(desc):
            continue
        for cue_re in _HEDGE_CUE_RES:
            if cue_re.search(desc):
                return (True, f"anti-hedge rule: {desc[:120]}")
    return (False, "")


def classify_iteration_lane(task: "Task") -> str:  # type: ignore[name-defined]  # noqa: F821
    """Classify a completed/failed pipeline task into its review-fix lane.

    Used to compute per-lane iteration counts so sub-caps can be applied
    alongside the global cap. The classifier is a pure function of the task's
    ``review_loop.fix_reason``.
    """
    fix_reason = (
        (task.context or {}).get("review_loop", {}).get("fix_reason", "code_review")
    )
    if fix_reason in ("merge_conflict", "update_branch_failed"):
        return LANE_CONFLICT
    if fix_reason == "ci_failure":
        return LANE_CI
    return LANE_REVIEW_CONVERGENCE


def _exact_review_target_identity(target: object) -> tuple[str, int, str, str] | None:
    """Return the complete immutable review-target identity, if present.

    Review authority is a property of the exact ``(project, PR, base, head)``
    tuple.  A head-only identity is deliberately not accepted: two reviews of
    the same head against different integration bases did not inspect the same
    effective change.
    """

    if not isinstance(target, dict):
        return None
    project_id = str(target.get("project_id") or "").strip()
    base_sha = str(target.get("base_sha") or "").strip()
    head_sha = str(target.get("head_sha") or "").strip()
    try:
        pr_number = int(target.get("pr_number") or 0)
    except (TypeError, ValueError):
        return None
    if not project_id or pr_number <= 0 or not base_sha or not head_sha:
        return None
    return (project_id, pr_number, base_sha, head_sha)


def _structured_review_verdict(task: "Task") -> tuple[str, str, str] | None:  # type: ignore[name-defined]  # noqa: F821
    """Return ``(verdict, reviewed_base, reviewed_head)`` from signed result data.

    ``blueprint_review_verdict`` is a convenient UI mirror, not sufficient
    provenance for cap accounting.  The replay-safe ``review_result`` retains
    the agent-stamped exact pair and is therefore the authoritative source.
    """

    result = (task.context or {}).get("review_result")
    if not isinstance(result, dict) or result.get("structured") is not True:
        return None
    raw = result.get("verdict_json")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    verdict = str(payload.get("verdict") or result.get("verdict") or "").upper()
    reviewed_base = str(payload.get("reviewed_base_sha") or "").strip()
    reviewed_head = str(payload.get("reviewed_head_sha") or "").strip()
    if verdict not in ("APPROVE", "REQUEST_CHANGES"):
        return None
    return (verdict, reviewed_base, reviewed_head)


def review_cap_generation_identity(
    task: "Task",  # type: ignore[name-defined]  # noqa: F821
    *,
    count_stale_discards: bool = False,
) -> tuple[object, ...] | None:
    """Return the actioned generation charged to the review/fix cap.

    A cap unit is an exact patch/open-set generation, not a worker row:

    * a structured REQUEST_CHANGES review and the fix it launches share the
      same exact target key and therefore consume one unit together;
    * parallel reviewers of that same target collapse to the same unit;
    * APPROVE-only and target-mismatched reviews consume none;
    * mechanical conflict/CI fixes remain bounded independently; and
    * old unstructured rows retain a task-id fallback so legacy chains cannot
      become unbounded merely because they predate exact-target receipts.

    ``count_stale_discards`` (default ``False``) selects which of two
    disjoint populations this returns identities for. By default a
    stale-target discard (``_review_stale_head`` / ``_review_stale_target`` —
    a review/fix whose recorded target no longer matches the PR's live head
    because a supersede/retarget landed while it was in flight or awaiting
    action) returns ``None``, exactly as before: the ORDINARY per-chain/lane
    cap must not penalise a chain for a verdict it could not act on. When
    ``True`` — used only by the PR-lifetime cross-episode census in
    :func:`app.review_loop_convergence.build_cross_episode_abandon_candidate`
    — the function returns identities *only* for those stale-target rows,
    using the exact same target/verdict extraction below, so a supersede-
    discarded generation collapses on the (base, head) pair the agent
    actually reviewed instead of over- or under-counting duplicate discards
    of one superseded generation. The two populations never overlap, so a
    caller may safely combine both counts without double-counting a row.
    """

    payload = task.first_payload or {}
    task_type = str(payload.get("type") or "")
    if task_type not in ("review", "fix"):
        return None
    context = task.context or {}
    is_stale_discard = bool(
        context.get("_review_stale_head") or context.get("_review_stale_target")
    )
    if is_stale_discard != count_stale_discards:
        return None
    loop = context.get("review_loop")
    loop = loop if isinstance(loop, dict) else {}
    lane = classify_iteration_lane(task)

    if task_type == "fix":
        target = _exact_review_target_identity(loop.get("fix_target"))
        if target is not None:
            # A code-review fix and the REQUEST_CHANGES review that demanded it
            # are one actioned patch generation. Mechanical lanes keep their
            # own namespace so their sub-caps remain independent.
            return (lane, *target)
        reviewed_base = str(loop.get("reviewed_base_sha") or "").strip()
        reviewed_head = str(loop.get("reviewed_head_sha") or "").strip()
        if reviewed_base and reviewed_head:
            try:
                pr_number = int(payload.get("pr_number") or 0)
            except (TypeError, ValueError):
                return (lane, "legacy-fix", str(task.id))
            return (
                lane,
                str(task.project_id),
                pr_number,
                reviewed_base,
                reviewed_head,
            )
        return (lane, "legacy-fix", str(task.id))

    target = _exact_review_target_identity(loop.get("review_target"))
    signed = _structured_review_verdict(task)
    if signed is not None:
        verdict, reviewed_base, reviewed_head = signed
        if target is None:
            return None
        # Both halves are required. A result that was stamped for a different
        # base or head is stale/non-actionable even if a legacy completion path
        # happened to leave the row COMPLETED.
        if (reviewed_base, reviewed_head) != (target[2], target[3]):
            return None
        if verdict != "REQUEST_CHANGES":
            return None
        return (LANE_REVIEW_CONVERGENCE, *target)

    # Durable authoritative history is accepted for rows that predate the
    # replay-safe review_result mirror. Require this task's exact id and pair so
    # a copied/parallel history row cannot make an unrelated reviewer count.
    for entry in reversed(loop.get("review_history") or []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("review_task_id") or "") != str(task.id):
            continue
        if entry.get("target_verified_by_backend") is not True or target is None:
            return None
        entry_base = str(entry.get("reviewed_base_sha") or "").strip()
        entry_head = str(entry.get("reviewed_head_sha") or "").strip()
        if (entry_base, entry_head) != (target[2], target[3]):
            return None
        if str(entry.get("verdict") or "").upper() != "REQUEST_CHANGES":
            return None
        return (LANE_REVIEW_CONVERGENCE, *target)

    # Explicitly parallel rows without exact, actioned verdict evidence are
    # observation fan-out, not patch generations.
    if context.get("review_was_parallelized") is True:
        return None
    # Legacy rows without target/result receipts remain one bounded unit each.
    if target is None:
        return (LANE_REVIEW_CONVERGENCE, "legacy-review", str(task.id))
    return None


def actioned_review_cap_tasks(
    tasks: "list[Task]",  # type: ignore[name-defined]  # noqa: F821
    *,
    count_stale_discards: bool = False,
) -> list["Task"]:  # type: ignore[name-defined]  # noqa: F821
    """Collapse eligible task rows into actioned review/fix generations.

    The returned representative rows are used for both global and lane counts,
    keeping those two views on the same unit. Prefer a fix row when a review and
    its fix share a target because the fix is the concrete patch action.

    ``count_stale_discards`` is forwarded to
    :func:`review_cap_generation_identity` unchanged — see its docstring. Pass
    ``True`` only to collapse a population already filtered down to
    stale-target discards; the default collapses ordinary actioned rows.
    """

    by_generation: dict[tuple[object, ...], "Task"] = {}  # type: ignore[name-defined]  # noqa: F821
    for task in tasks:
        identity = review_cap_generation_identity(
            task, count_stale_discards=count_stale_discards
        )
        if identity is None:
            continue
        prior = by_generation.get(identity)
        if prior is None or (
            (prior.first_payload or {}).get("type") != "fix"
            and (task.first_payload or {}).get("type") == "fix"
        ):
            by_generation[identity] = task
    return list(by_generation.values())

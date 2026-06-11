"""Pure, synchronous issue-ledger derivation for the review→fix loop.

Side-effect-free: no DB, no async, no logging side-effects. Takes
review_history (the list of dicts with keys verdict / issues / iteration /
feedback / changed_files assembled by _spawn_pipeline_task) and returns a
structured ledger classifying per-region state as DEMANDED, APPLIED,
RE_DEMANDED, or CONTESTED.

The CONTESTED state is the reversal shape: a region was demanded, the fix
complied (region absent from the next review), then a later review demands
something *different* on the same region. This is the oscillation mode the
existing stuck-cycle / standoff / churn detectors miss because they count
textual repetition, not contradiction.

Incident motivating this module (PR #9244 / task 7f5e1f0a): across 9 stateless
reviews, one region (a frozenset in task_evaluators.py) was demanded changed one
way (iter 4: "replace old constants"), the fix complied exactly, then a later
review (iter 9) demanded the exact reversal ("add them back"). Each verdict was
locally defensible; the existing detectors count textual repetition, not
contradiction, so oscillation read as progress until the global cap killed the
chain.

Re-exported from app.review_pipeline so existing callers keep working.
"""

import re

# Regex for tokenizing descriptions — same surface as review_heuristics._FEEDBACK_TOKEN_RE.
_FEEDBACK_TOKEN_RE = re.compile(r"[a-z0-9_]+")

# Jaccard similarity boundary between RE_DEMANDED (high — same concern resurfaced)
# and CONTESTED (low — different demand on the same region, the reversal shape).
# A previously-applied region reappearing with ≥ this similarity to the original
# demand is RE_DEMANDED (feeds existing stuck machinery).  Below this → CONTESTED.
CONTESTED_JACCARD_THRESHOLD = 0.5

# Maximum contested regions serialized into review_loop["issue_ledger"].
ISSUE_LEDGER_MAX_CONTESTED = 10

# Line-window block size. Two issues on lines X and Y in the same file are treated
# as the same region when floor(X / LINE_WINDOW_BLOCK) == floor(Y / LINE_WINDOW_BLOCK),
# giving ±LINE_WINDOW_BLOCK/2 tolerance centred on the block boundary.
LINE_WINDOW_BLOCK = 40


# ── Token-level Jaccard ────────────────────────────────────────────────────────


def _token_set(text: str) -> set[str]:
    return {t for t in _FEEDBACK_TOKEN_RE.findall(text.lower()) if len(t) >= 3}


def _jaccard(a: str, b: str) -> float:
    ta = _token_set(a)
    tb = _token_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ── Region fingerprint ─────────────────────────────────────────────────────────


def _region_anchor(issue: dict) -> str:
    """Derive the anchor component of a region fingerprint.

    Preference: symbol > line-window > empty string.  Symbol takes priority
    because it is stable across edits (a line number shifts when code is
    inserted/removed above it, but the function name does not).
    """
    symbol = (issue.get("symbol") or "").strip()
    if symbol:
        return f"sym:{symbol}"
    line = issue.get("line")
    if line is not None:
        try:
            block = int(line) // LINE_WINDOW_BLOCK
            return f"win:{block}"
        except (TypeError, ValueError):
            pass
    return ""


def _region_key(issue: dict) -> tuple[str, str] | None:
    """Return (file, anchor) or None when the issue has no file."""
    file_path = (issue.get("file") or "").strip()
    if not file_path:
        return None
    return (file_path, _region_anchor(issue))


# ── Ledger builder ─────────────────────────────────────────────────────────────


def build_issue_ledger(review_history: list) -> dict:
    """Derive per-region state from review_history and return a ledger dict.

    Returns:
      {
        "contested": [
          {
            "file": str,
            "anchor": str,            # "sym:X", "win:N", or ""
            "demanded_iteration": int,
            "demanded_description": str,   # description from the APPLIED demand
            "reappeared_iteration": int,
            "reappeared_description": str, # description of the contradicting demand
          },
          ...
        ],   # at most ISSUE_LEDGER_MAX_CONTESTED, in discovery order
        "total_regions": int,          # unique region keys seen across history
      }

    State machine per region key = (file, anchor):
      NOT_SEEN
        → DEMANDED  when issue appears in a REQUEST_CHANGES entry
      DEMANDED
        → APPLIED   when region is absent from the next REQUEST_CHANGES entry
                    (or when an APPROVE entry is seen — the whole batch passed)
      APPLIED
        → CONTESTED if the region reappears with Jaccard(applied_desc, new_desc)
                    < CONTESTED_JACCARD_THRESHOLD   [the reversal shape]
        → RE_DEMANDED otherwise (same concern resurfaced; feeds stuck machinery)

    Only CRITICAL and MAJOR issues are tracked, consistent with stuck-cycle
    and churn detection elsewhere.

    Description text is a tiebreaker only: when multiple issues in the same
    entry map to the same (file, anchor), the one whose description has the
    highest Jaccard similarity to the prior demand is kept.
    """
    # {key: {"state": "DEMANDED"|"APPLIED", "demanded_iteration": int,
    #        "demanded_description": str}}
    state: dict[tuple, dict] = {}
    contested: list[dict] = []
    all_region_keys: set[tuple] = set()

    for entry in review_history or []:
        if not isinstance(entry, dict):
            continue
        verdict = (entry.get("verdict") or "").upper()
        iteration = int(entry.get("iteration") or 0)
        issues = entry.get("issues") or []

        if verdict == "APPROVE":
            # All currently DEMANDED regions were addressed — mark APPLIED.
            for key in list(state):
                if state[key]["state"] == "DEMANDED":
                    state[key] = dict(state[key])
                    state[key]["state"] = "APPLIED"
            continue

        if verdict != "REQUEST_CHANGES":
            # Fix tasks, verify_tests, or entries without a verdict — skip.
            continue

        # ── Collect this entry's CRITICAL/MAJOR regions ───────────────────
        current_regions: dict[tuple, dict] = {}
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if (issue.get("severity") or "").upper() not in ("CRITICAL", "MAJOR"):
                continue
            key = _region_key(issue)
            if key is None:
                continue
            desc = (issue.get("description") or "").strip()
            if not desc:
                continue
            all_region_keys.add(key)
            if key not in current_regions:
                current_regions[key] = {"iteration": iteration, "description": desc}
            else:
                # Tiebreaker: keep the description with higher Jaccard to prior demand.
                if key in state:
                    prior_desc = state[key].get("demanded_description", "")
                    existing = current_regions[key]["description"]
                    if _jaccard(desc, prior_desc) > _jaccard(existing, prior_desc):
                        current_regions[key] = {"iteration": iteration, "description": desc}

        # ── State transitions ─────────────────────────────────────────────
        # Snapshot current state before mutating (avoid dict-change-during-iter).
        prev_state_snapshot = {k: dict(v) for k, v in state.items()}

        # DEMANDED → APPLIED: region was demanded but absent from this entry.
        for key, info in prev_state_snapshot.items():
            if info["state"] == "DEMANDED" and key not in current_regions:
                state[key]["state"] = "APPLIED"

        # APPLIED → RE_DEMANDED or CONTESTED: previously applied, now reappears.
        for key, info in prev_state_snapshot.items():
            if info["state"] == "APPLIED" and key in current_regions:
                applied_desc = info.get("demanded_description", "")
                new_desc = current_regions[key]["description"]
                similarity = _jaccard(applied_desc, new_desc)
                if (
                    similarity < CONTESTED_JACCARD_THRESHOLD
                    and len(contested) < ISSUE_LEDGER_MAX_CONTESTED
                ):
                    contested.append(
                        {
                            "file": key[0],
                            "anchor": key[1],
                            "demanded_iteration": info.get("demanded_iteration", 0),
                            "demanded_description": applied_desc,
                            "reappeared_iteration": iteration,
                            "reappeared_description": new_desc,
                        }
                    )
                # Transition to DEMANDED regardless (both RE_DEMANDED and CONTESTED
                # are re-demands; distinction is only for prompt context).
                state[key] = {
                    "state": "DEMANDED",
                    "demanded_iteration": iteration,
                    "demanded_description": new_desc,
                }

        # Update/add DEMANDED regions from current entry.
        for key, info in current_regions.items():
            all_region_keys.add(key)
            if key in state and state[key]["state"] == "DEMANDED":
                state[key]["demanded_description"] = info["description"]
                state[key]["demanded_iteration"] = iteration
            elif key not in state:
                state[key] = {
                    "state": "DEMANDED",
                    "demanded_iteration": iteration,
                    "demanded_description": info["description"],
                }
            # APPLIED regions reappearing are handled in the transition loop above.

    return {
        "contested": contested[:ISSUE_LEDGER_MAX_CONTESTED],
        "total_regions": len(all_region_keys),
    }


# ── Prompt injection helpers ───────────────────────────────────────────────────

_CONTESTED_FIX_PREFIX_HEADER = (
    "⚠️ CONTESTED REGIONS DETECTED: The following code regions were demanded "
    "by a prior review, addressed by the fix (verified absent from a later "
    "review), and then demanded in a DIFFERENT direction by a subsequent review. "
    "Both demands are valid constraints — you must implement the UNION of both, "
    "satisfying each simultaneously. Use the structured-response channel "
    "(verify_steps / rationale) to explain how each prior demand is still honored "
    "in your fix.\n\n"
)

_CONTESTED_FIX_BASE_HINT = (
    "\nFor each contested file, run:\n"
    "  git diff $(git merge-base HEAD origin/{base_branch})"
    "..origin/{base_branch} -- {{file}}\n"
    "If the base evolved that region since branching, the base version is "
    "presumptively authoritative — converge to base or implement an explicit union.\n\n"
)

_CONTESTED_REVIEW_APPENDIX_HEADER = (
    "CONTESTED REGIONS: The code regions listed below were previously demanded, "
    "addressed (verified absent from a subsequent review), and are now being "
    "demanded again — possibly in a different direction. If you raise a demand "
    "on any of these regions, you MUST explicitly reconcile with the prior "
    "applied demand: state how the new demand coexists with, supersedes, or "
    "forms a union with the prior fix. Silent reversal of an applied demand is "
    "invalid feedback.\n\n"
)

_CONTESTED_REVIEW_BASE_HINT = (
    "For each contested file, run:\n"
    "  git diff $(git merge-base HEAD origin/{base_branch})"
    "..origin/{base_branch} -- {{file}}\n"
    "If the base evolved the region since branching, the base version is "
    "authoritative — demand convergence-to-base or an explicit union.\n\n"
)


def _format_contested_region_line(region: dict) -> str:
    file_path = region.get("file", "")
    anchor = region.get("anchor", "")
    d_iter = region.get("demanded_iteration", 0)
    d_desc = (region.get("demanded_description") or "").strip()
    r_iter = region.get("reappeared_iteration", 0)
    r_desc = (region.get("reappeared_description") or "").strip()
    anchor_part = f" ({anchor})" if anchor else ""
    return (
        f"- `{file_path}`{anchor_part}: "
        f'iter {d_iter} demanded "{d_desc}" → applied; '
        f'iter {r_iter} now demands "{r_desc}"'
    )


def format_contested_regions_for_fix(contested: list, base_branch: str) -> str:
    """Build the fix-prompt prefix for contested regions.

    Prepended to the fix task prompt when contested regions exist in the ledger.
    """
    if not contested:
        return ""
    lines = "\n".join(_format_contested_region_line(r) for r in contested)
    base_hint = _CONTESTED_FIX_BASE_HINT.format(base_branch=base_branch)
    return _CONTESTED_FIX_PREFIX_HEADER + lines + "\n" + base_hint


def format_contested_regions_for_review(contested: list, base_branch: str) -> str:
    """Build the review-prompt appendix for contested regions.

    Appended to the review task's original_prompt when contested regions exist.
    """
    if not contested:
        return ""
    lines = "\n".join(_format_contested_region_line(r) for r in contested)
    base_hint = _CONTESTED_REVIEW_BASE_HINT.format(base_branch=base_branch)
    return _CONTESTED_REVIEW_APPENDIX_HEADER + base_hint + lines + "\n"

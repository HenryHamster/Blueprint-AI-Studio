"""The one command that resolves derived-artifact conflicts, as prompt text.

WHY THIS IS A MODULE AND NOT THREE STRINGS
------------------------------------------
Three orchestrator prompts tell an agent how to resolve a merge conflict:
``rollup_management`` spawns one for a dirty child PR and one for a dirty
rollup PR, and ``hotfix_backmerge`` spawns one for a failed ``main -> automain``
back-merge. All three already describe, in prose, the resolution this repo
implements in code -- the back-merge prompt literally says "size-cap files:
take the higher cap" and "generated files / snapshots: take main and regenerate
if a generator exists", which is exactly what ``backend/testing/
merge_size_limits.py`` and ``scripts/generate_test_estate_ranked_queue.py`` do.

Prose that restates a deterministic procedure is the expensive way to run it:
the agent re-derives the policy from the description, and on
``backend/testing/test-estate-ranked-queue.md`` it re-derives it wrongly often enough that the
same conflict came back five times in one afternoon (2026-08-26). This module
holds the command instead, in one place, so the three prompts cannot drift into
three slightly different descriptions of the same procedure.

WHAT IT DOES NOT CLAIM
----------------------
This is instruction text, not a guarantee. The deterministic pass that needs no
agent at all already runs before the agent turn for conflict-only fix tasks
(``agent/src/handlers/review-fix-derived-conflicts.ts``). This block covers the
case that pass deliberately declines: a merge carrying BOTH a real source
conflict and a derived-artifact one. That pass is all-or-nothing on purpose --
a half-resolved tree hides which conflicts were real -- so the derived files
are still conflicted when the agent takes over, and without this block the
agent hand-resolves them too.
"""

from __future__ import annotations

RESOLVE_SCRIPT = "scripts/resolve-derived-conflicts.sh"
"""Repo-relative wrapper: registers the merge drivers, then resolves."""

DERIVED_CONFLICT_PROMPT_BLOCK = (
    "\n\n## Derived artifacts: run the resolver, do not hand-merge them\n"
    f"Some conflicts in this repository have exactly one correct resolution and "
    f"a script that produces it. Once you have resolved the conflicts that need "
    f"real judgement, run:\n\n"
    f"    bash {RESOLVE_SCRIPT}\n\n"
    "It resolves and stages `backend/testing/test-estate-ranked-queue.md` (regenerated from the "
    "merged tree), `backend/.test_durations` (insert-only union) and "
    "`backend/testing/module_size_limits.json` (max-cap union, the same driver "
    "git itself uses), and it refuses to touch anything else -- if it exits "
    "non-zero it has changed NOTHING and the paths it names are yours to "
    "resolve. Exit 2 just means there was nothing left for it to do.\n"
    "Do not hand-resolve those three: `backend/testing/test-estate-ranked-queue.md` in "
    "particular is a whole-tree report, so picking either side leaves it stale "
    "and the `Derived artifacts` check goes red on the merge result even though "
    "both sides were individually correct."
)
"""Appended once to a conflict-resolution prompt, after its numbered steps."""


__all__ = ["DERIVED_CONFLICT_PROMPT_BLOCK", "RESOLVE_SCRIPT"]

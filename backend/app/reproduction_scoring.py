"""Reproduce-on-HEAD verdict carried from a defect source onto its fix task.

A fix (or any code task) is spawned to resolve a defect named by a bug-report,
a research finding, or a review. Blueprint already computes a conservative
reproduce-on-HEAD verdict for that defect at ingestion
(:func:`app.source_ref_validation.classify_reproduction`). This leaf module is
the single place that:

  * names the additive context key the verdict travels under
    (:data:`TARGET_DEFECT_REPRODUCTION_KEY`), and
  * stamps a recognized verdict onto a spawn-context dict
    (:func:`record_target_defect_reproduction`).

The contract is deliberately additive and backward-compatible: a task with no
recorded key (feature creates, legacy rows, anything spawned before this lands)
is never blocked — absence of the key means "no reproduction state was
recorded" and preserves the prior bare-merge scoring exactly.
"""

from __future__ import annotations

from app.source_ref_validation import (
    CITATION_STATE_ABSENT,
    CITATION_STATE_INDETERMINATE,
    CITATION_STATE_PRESENT,
)

# Context key under which a fix task carries the reproduce-on-HEAD verdict of
# the defect it was spawned to resolve. A non-underscore key so it propagates
# through the standard context-copy paths (e.g. review-fix spawn) by default.
TARGET_DEFECT_REPRODUCTION_KEY = "target_defect_reproduction"

# The verdicts we recognize — exactly the tri-state of ``classify_reproduction``.
_RECOGNIZED_VERDICTS = frozenset(
    {CITATION_STATE_PRESENT, CITATION_STATE_ABSENT, CITATION_STATE_INDETERMINATE}
)


def record_target_defect_reproduction(context: dict, verdict: str | None) -> None:
    """Stamp a recognized reproduce-on-HEAD verdict onto a spawn-context dict.

    No-op for ``None`` or any unrecognized value, so a missing/garbled verdict
    never writes a key that would later block scoring. Only the canonical
    ``CITATION_STATE_*`` strings are ever recorded.
    """
    if verdict in _RECOGNIZED_VERDICTS:
        context[TARGET_DEFECT_REPRODUCTION_KEY] = verdict



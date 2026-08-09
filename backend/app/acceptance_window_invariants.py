"""Pure invariants for release-qualifier acceptance-window sub-scores.

The release qualifier writes these fields into a version's durable
``last_qualification["sub_scores"]`` payload.  Keeping their validation
separate from the task acceptance gate makes the population contract reusable
by metrics-integrity checks without growing the chronic gate module.
"""

from __future__ import annotations

#: The five keys emitted by the acceptance-window aggregate and merged into
#: ``last_qualification["sub_scores"]``.  The helper is fail-safe: it returns
#: ``[]`` when none of these keys are present (legacy verdicts).
_ACCEPTANCE_WINDOW_KEYS: frozenset[str] = frozenset(
    {
        "acceptance_window_total",
        "acceptance_window_verified",
        "acceptance_window_fraction",
        "acceptance_window_write_total",
        "acceptance_window_total_all_merges",
    }
)

# The two keys introduced by the write-task-population fix.  Their absence
# means the verdict was written by pre-fix code (3-key schema) and should be
# skipped by invariant checks that only apply to the new schema.
_ACCEPTANCE_WINDOW_POPULATION_KEYS: frozenset[str] = frozenset(
    {"acceptance_window_write_total", "acceptance_window_total_all_merges"}
)


def acceptance_window_sub_scores_violations(sub_scores: dict) -> list[str]:
    """Return invariants violated by ``acceptance_window_*`` sub-score fields.

    Empty list means every guarantee holds, or the fields are absent because
    the verdict predates the acceptance-window population schema.  The helper
    is pure and read-only, and deliberately remains fail-safe for legacy
    verdicts.

    Invariants checked:

    * ``verified >= 0``
    * ``write_total >= 0``
    * ``verified <= write_total``
    * ``write_total <= total_all_merges``
    * ``fraction is None`` iff ``write_total == 0``; otherwise it equals
      ``verified / write_total``
    """
    # Skip both the zero-key case and the pre-fix 3-key schema.  Checking for
    # the population keys avoids false violations on healthy legacy verdicts
    # where write_total would otherwise default to zero.
    if not any(k in sub_scores for k in _ACCEPTANCE_WINDOW_POPULATION_KEYS):
        return []

    violations: list[str] = []

    verified: int = int(sub_scores.get("acceptance_window_verified") or 0)
    write_total: int = int(sub_scores.get("acceptance_window_write_total") or 0)
    total_all: int = int(sub_scores.get("acceptance_window_total_all_merges") or 0)
    fraction = sub_scores.get("acceptance_window_fraction")

    if verified < 0:
        violations.append(f"acceptance_window_verified ({verified}) is negative")
    if write_total < 0:
        violations.append(f"acceptance_window_write_total ({write_total}) is negative")
    if verified > write_total:
        violations.append(
            f"acceptance_window_verified ({verified}) exceeds "
            f"acceptance_window_write_total ({write_total})"
        )
    if write_total > total_all:
        violations.append(
            f"acceptance_window_write_total ({write_total}) exceeds "
            f"acceptance_window_total_all_merges ({total_all})"
        )
    if write_total == 0:
        if fraction is not None:
            violations.append(
                f"acceptance_window_fraction ({fraction}) is not None "
                "with zero write_total (expected None)"
            )
    else:
        expected = verified / write_total
        if fraction is None:
            violations.append(
                f"acceptance_window_fraction is None but write_total ({write_total}) > 0"
            )
        elif fraction != expected:
            violations.append(
                f"acceptance_window_fraction ({fraction}) != "
                f"verified/write_total ({verified}/{write_total} = {expected})"
            )

    return violations

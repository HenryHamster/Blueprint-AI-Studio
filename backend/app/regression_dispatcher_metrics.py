"""Pure metric / formatter helpers extracted from pipeline_regression_dispatcher.

These functions are synchronous, take no AsyncSession, and do no I/O. They are
the compute-and-format family from the dispatcher (layer set math, MTTI
percentile, etc.) that admin_digest already consumed via deferred imports.

The dispatcher re-imports these names at module load so existing
``from app.pipeline_regression_dispatcher import <name>`` callers and
internal references continue to work unchanged — this module is a sibling,
not a replacement.
"""

from __future__ import annotations

from typing import Any

from app.regression_layer_classifier import (
    LAYER_DISPLAY_ORDER,
    aggregate_targeted_layers,
)


def compute_mtti_p50_sec(cases: list) -> float | None:
    """Return median seconds from escalation to operator acknowledgement.

    Filters to cases where both ``needs_manual_intervention_at`` and
    ``operator_acknowledged_at`` are set.  Returns ``None`` when no acked
    cases are present in the input.  Uses ``statistics.median`` — no numpy.
    """
    import statistics

    deltas = [
        (c.operator_acknowledged_at - c.needs_manual_intervention_at).total_seconds()
        for c in cases
        if c.needs_manual_intervention_at is not None
        and c.operator_acknowledged_at is not None
    ]
    return statistics.median(deltas) if deltas else None


def _layers_targeted_union(entries: list[dict[str, Any]] | None) -> set[str]:
    """Union of canonical ``layers_targeted`` across fix-log / cycle entries.

    Backwards-compatible name for callers (tests, admin_digest); the actual
    aggregation is owned by ``regression_layer_classifier.aggregate_targeted_layers``.
    Entries from ``_extract_recurrence_cycles`` carry an empty ``layers_targeted``
    list when ``layers_targeted_present`` is False, so the classifier's
    canonical-layer filter yields the same result as the prior dispatcher
    implementation.
    """
    return aggregate_targeted_layers(entries or [])


def compute_untargeted_layers(
    fingerprint_to_fix_log: list[dict[str, Any]] | None,
) -> list[str]:
    """Canonical layers never targeted by any prior fix in this case's history.

    Returns layers from ``LAYER_DISPLAY_ORDER`` (in canonical order)
    that do not appear in any entry's ``layers_targeted`` list. Unrecognized
    layer names in the fix-log are silently dropped so a typo or future-added
    layer never crashes the read path. Empty / missing / non-list fix-log
    yields the full canonical tuple — everything is untargeted by default.
    """
    if not isinstance(fingerprint_to_fix_log, list):
        return list(LAYER_DISPLAY_ORDER)
    targeted = _layers_targeted_union(fingerprint_to_fix_log)
    canonical = set(LAYER_DISPLAY_ORDER)
    targeted &= canonical
    return [layer for layer in LAYER_DISPLAY_ORDER if layer not in targeted]


def compute_class_layer_defense_counts(
    fix_logs: list[dict[str, Any]] | None,
) -> dict[str, int]:
    """Per-canonical-layer fix counts across a list of fix-log entries.

    Caller should pass the aggregated fix-log entries for all cases that share
    the same regression_class.  Returns one entry per canonical layer in
    canonical order (including layers with zero fixes) so the UI renders a
    stable six-row table.
    """
    counts: dict[str, int] = dict.fromkeys(LAYER_DISPLAY_ORDER, 0)
    for entry in fix_logs or []:
        if not isinstance(entry, dict):
            continue
        for layer in entry.get("layers_targeted") or []:
            if layer in counts:
                counts[layer] += 1
    return counts

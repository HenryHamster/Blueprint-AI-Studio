"""Scope complexity scoring helpers.

Extracted from app.research_completion to keep that module under its
line-count guardrail. Contains composite-merge-probability scoring that
combines file count, historical expansion ratio, cochange cluster density,
and cross-module span into a single P(merge) estimate in [0, 1].

Also home to SCOPE_FILE_BUDGETS — the per-scope file-count budgets surfaced
to research agents and consulted by decomposition heuristics.
"""
from app.cochange_decomposition import (
    cluster_files_by_cochange as _cluster_files_by_cochange,
)

# Hard file-count budgets enforced by implementing agents per scope.
# Values mirror SCOPE_OVERFLOW_THRESHOLDS and MEDIUM_ANALYSIS_PLAN_FILE_LIMIT
# in agent/src/constants.ts — keep in sync.
SCOPE_FILE_BUDGETS: dict[str, dict[str, int]] = {
    "small": {"total_files_max": 6},
    "medium": {"production_files_max": 7, "total_files_max": 10},
    "large": {"total_files_max": 20},
}


def _composite_complexity_score(
    related_files: list[str], direction: dict | None, cochange_index: dict | None,
) -> tuple[float, dict]:
    """Estimate P(merge) in [0,1] from file count, historical expansion ratio,
    cochange cluster density, and cross-module span. Lower = more complex."""
    n = len(related_files or [])
    score = 1.0 - min(0.08 * max(0, n - 2), 0.32)
    ratio = 0.0
    if direction and n > 0:
        rs = [(e.get("scope_breakdown") or {}).get("medium", {}).get("avg_expansion_ratio")
              for e in (direction.get("research_history") or [])[-3:]]
        rs = [float(r) for r in rs if isinstance(r, (int, float))]
        if rs:
            ratio = sum(rs) / len(rs)
            score -= min(0.12 * max(0.0, ratio - 1.0), 0.3)
    if n > 1 and cochange_index:
        clusters = _cluster_files_by_cochange(list(related_files), cochange_index)
        if clusters is None:
            score -= 0.1
        elif len(clusters) > 1:
            score -= min(0.08 * (len(clusters) - 1), 0.24)
    modules = {"/".join(f.split("/", 2)[:2]) if "/" in f else f for f in (related_files or [])}
    if len(modules) > 1:
        score -= min(0.12 * (len(modules) - 1), 0.3)
    return max(0.0, min(1.0, score)), {"files": n, "expansion_ratio": round(ratio, 2), "modules": len(modules)}

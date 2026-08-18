"""Scope complexity scoring helpers.

Extracted from app.research_completion to keep that module under its
line-count guardrail. Contains composite-merge-probability scoring that
combines file count, historical expansion ratio, cochange cluster density,
and cross-module span into a single P(merge) estimate in [0, 1].

Uses multiplicative factors so moderate values across multiple dimensions
do not cascade into premature escalation — each dimension contributes a
factor in [0.5, 1.0], and the product gives the composite probability.
Extreme values in any single dimension can still drive the composite below
the escalation threshold.
"""

from app.cochange_decomposition import (
    cluster_files_by_cochange as _cluster_files_by_cochange,
)
from app.migration_path import is_migration_file


def _is_auto_generated(path: str) -> bool:
    """Auto-generated files (e.g. alembic migrations) are mechanically
    derived from models.py changes, so they should not inflate complexity
    scoring dimensions."""
    return is_migration_file(path)


def _composite_complexity_score(
    related_files: list[str],
    direction: dict | None,
    cochange_index: dict | None,
) -> tuple[float, dict]:
    """Estimate P(merge) in [0,1] as a product of per-dimension factors.

    Each dimension returns a factor in [0.5, 1.0]; the composite is the
    product. Lower = more complex / less likely to merge.
    """
    all_files = list(related_files or [])
    auto_generated = [f for f in all_files if _is_auto_generated(f)]
    related_files = [f for f in all_files if not _is_auto_generated(f)]
    n = len(related_files)
    # File count: no penalty up to 6 files, linear down to 0.5 at n=14+.
    # The knee sits well inside the medium band (16 files) on purpose: modern
    # implementing agents handle multi-file changes reliably, and penalizing
    # ordinary contract-complete change sets just biases spawning toward
    # fragments that individually merge while collectively breaking contracts.
    f_files = max(0.5, 1.0 - 0.0625 * max(0, n - 6))
    ratio = 0.0
    f_ratio = 1.0
    if direction and n > 0:
        rs = [
            (e.get("scope_breakdown") or {})
            .get("medium", {})
            .get("avg_expansion_ratio")
            for e in (direction.get("research_history") or [])[-3:]
        ]
        rs = [float(r) for r in rs if isinstance(r, (int, float))]
        if rs:
            ratio = sum(rs) / len(rs)
            # ratio=1.0 → 1.0; ratio=5.0+ → floor 0.5.
            f_ratio = max(0.5, 1.0 - 0.125 * max(0.0, ratio - 1.0))
    f_clusters = 1.0
    if n > 1 and cochange_index:
        clusters = _cluster_files_by_cochange(list(related_files), cochange_index)
        if clusters is None:
            f_clusters = 0.9
        elif len(clusters) > 1:
            f_clusters = max(0.5, 1.0 - 0.08 * (len(clusters) - 1))
    modules = {
        "/".join(f.split("/", 2)[:2]) if "/" in f else f for f in (related_files or [])
    }
    # modules=1 → 1.0; modules=5+ → floor 0.5.
    f_modules = max(0.5, 1.0 - 0.125 * max(0, len(modules) - 1))
    neighbor_ratio = 0.0
    f_neighbors = 1.0
    if n > 0 and cochange_index:
        listed = set(related_files or [])
        unlisted = 0
        for f in related_files or []:
            for partner, score in (cochange_index.get(f) or {}).items():
                if (
                    partner not in listed
                    and isinstance(score, (int, float))
                    and score >= 1.0
                ):
                    unlisted += 1
        neighbor_ratio = unlisted / n
        # ratio<=0.5 → 1.0; ratio>=2.0 → 0.5; linear between.
        f_neighbors = max(0.5, min(1.0, 1.0 - (neighbor_ratio - 0.5) / 3.0))
    score = f_files * f_ratio * f_clusters * f_modules * f_neighbors
    return max(0.0, min(1.0, score)), {
        "files": n,
        "auto_generated_files": len(auto_generated),
        "expansion_ratio": round(ratio, 2),
        "modules": len(modules),
        "neighbor_ratio": round(neighbor_ratio, 2),
        "factors": {
            "files": round(f_files, 3),
            "ratio": round(f_ratio, 3),
            "clusters": round(f_clusters, 3),
            "modules": round(f_modules, 3),
            "neighbors": round(f_neighbors, 3),
        },
    }

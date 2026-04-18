"""Co-change clustering + auto-decomposition of medium-scope research findings.

Extracted from ``app.research_completion`` to keep that module focused on the
research completion handler itself (file-size guardrail).
"""
import logging
from typing import Callable

from app.shared_state import DEFAULT_TASK_SCOPE

logger = logging.getLogger(__name__)


def cluster_files_by_cochange(
    files: list[str],
    cochange_index: dict,
    threshold: float = 1.0,
) -> list[list[str]] | None:
    """Group files into independent clusters by co-change affinity.

    Two files join the same cluster when the cochange_index records an
    affinity >= threshold between them (in either direction). Returns
    clusters preserving input order, or None when none of the files have
    any recorded affinity — that sparse-history case must skip decomposition,
    otherwise every file becomes its own singleton cluster.
    """
    if len(files) < 2 or not cochange_index:
        return None
    if not any(f in cochange_index and cochange_index[f] for f in files):
        return None

    parent = {f: f for f in files}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    files_set = set(files)
    for fa in files:
        for fb, score in (cochange_index.get(fa) or {}).items():
            if (
                fb in files_set
                and isinstance(score, (int, float))
                and score >= threshold
            ):
                union(fa, fb)

    groups: dict[str, list[str]] = {}
    for f in files:
        groups.setdefault(find(f), []).append(f)
    return list(groups.values())


def cluster_files_by_toplevel_dir(files: list[str]) -> list[list[str]] | None:
    """Group files by their top-level directory (e.g. backend/, agent/, admin-ui/).

    Fallback clustering for sparse-cochange findings: returns one cluster per
    top-level dir when 3+ directory-prefixed files span 2+ dirs, else None.
    Root-level files (no ``/``) don't establish a directory span and are
    ignored — if after filtering fewer than 3 files remain, returns None.
    """
    if len(files) < 3:
        return None
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    kept = 0
    for f in files:
        if "/" not in f:
            continue
        top = f.split("/", 1)[0]
        if top not in groups:
            order.append(top)
        groups.setdefault(top, []).append(f)
        kept += 1
    if kept < 3 or len(groups) < 2:
        return None
    return [groups[t] for t in order]


def auto_decompose_findings_by_cochange(
    deduplicated: list[tuple[int, dict, str]],
    cochange_index: dict | None,
    task_context: dict | None,
    finding_hash_fn: Callable[[str, str], str],
) -> list[tuple[int, dict, str]]:
    """Split medium-scope findings whose files form independent co-change clusters.

    When a medium-scope finding's related_files form multiple independent
    clusters in the cochange_index (files that don't historically co-change
    together), split it into one small sub-finding per cluster so each piece
    spawns as a small task at the healthier small-scope merge rate.

    Falls back to clustering by top-level directory when the cochange index
    has no affinity data for the finding's files — sparse history would
    otherwise bypass decomposition entirely.

    Clusters that exceed the small-scope file budget keep medium scope rather
    than being downgraded — a cluster too large for small wouldn't fit the
    agent file budget at small scope. If every cluster exceeds the small
    budget, skip decomposition entirely so the original medium finding stays
    intact.
    """
    from app.research_completion import SCOPE_FILE_BUDGETS  # noqa: PLC0415
    small_budget = (
        SCOPE_FILE_BUDGETS.get("small", {}).get("total_files_max") or 6
    )
    expanded: list[tuple[int, dict, str]] = []
    for orig_i, finding, fhash in deduplicated:
        f_scope = finding.get("scope", DEFAULT_TASK_SCOPE)
        f_files = finding.get("related_files") or []
        f_already_calibrated = bool(
            finding.get("scope_calibrated_from")
            or (task_context or {}).get("scope_calibrated_from")
        )
        if (
            f_scope == "medium"
            and len(f_files) >= 3
            and not f_already_calibrated
        ):
            f_clusters = (
                cluster_files_by_cochange(f_files, cochange_index)
                if cochange_index
                else None
            )
            cluster_source = "co-change"
            if f_clusters is None:
                f_clusters = cluster_files_by_toplevel_dir(f_files)
                cluster_source = "top-level-dir"
            if f_clusters and len(f_clusters) >= 2:
                f_title = finding.get("title", f"Research finding #{orig_i+1}")
                if all(len(c) > small_budget for c in f_clusters):
                    logger.info(
                        "[RESEARCH] Skipping co-change decomposition for '%s': "
                        "all %d clusters exceed small-scope budget (%d files)",
                        f_title, len(f_clusters), small_budget,
                    )
                    expanded.append((orig_i, finding, fhash))
                    continue
                logger.info(
                    "[RESEARCH] Auto-decomposing medium finding '%s' into %d "
                    "sub-findings via %s clustering: %s",
                    f_title, len(f_clusters), cluster_source, f_clusters,
                )
                for ci, cluster in enumerate(f_clusters):
                    sub = dict(finding)
                    if len(cluster) > small_budget:
                        sub["scope"] = "medium"
                    else:
                        sub["scope"] = "small"
                        sub["scope_calibrated_from"] = "medium"
                    sub["related_files"] = cluster
                    sub["title"] = (
                        f"{f_title} (cluster {ci + 1}/{len(f_clusters)})"
                    )
                    sub["auto_decomposed"] = True
                    sub_hash = finding_hash_fn(
                        sub["title"], sub.get("description", "")
                    )
                    expanded.append((orig_i, sub, sub_hash))
                continue
        expanded.append((orig_i, finding, fhash))
    return expanded

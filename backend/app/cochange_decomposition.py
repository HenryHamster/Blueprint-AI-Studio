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
    clusters = list(groups.values())
    # Reject an all-singletons result: if union-find joined no pair, every file
    # is its own cluster and there is no real co-change structure to split on.
    # The affinity guard above can still pass when a single file carries
    # cochange data only with files OUTSIDE this set, so the loop joins nothing
    # and we'd otherwise fan a coherent finding out into one task per file
    # (incident 2026-06-02: research a473833c -> 1 finding -> 5 redundant
    # "(cluster N/5)" tasks, 1 stalled + 4 cancelled). Treat it as "no
    # co-change structure" — matching this function's documented contract — and
    # skip decomposition so the finding stays a single task.
    if not any(len(c) >= 2 for c in clusters):
        return None
    return clusters


def auto_decompose_findings_by_cochange(
    deduplicated: list[tuple[int, dict, str]],
    cochange_index: dict | None,
    task_context: dict | None,
    finding_hash_fn: Callable[[str, str], str],
) -> list[tuple[int, dict, str]]:
    """Split medium-scope findings whose files form independent co-change clusters.

    Splits ONLY on positive historical evidence: when the cochange_index
    partitions the finding's related_files into multiple clusters, each with
    genuine internal co-change affinity. Absence of affinity data is NOT
    evidence of independence — files can be coupled through a shared contract
    (types, API shapes, message schemas) that co-change history never
    recorded, and fragmenting such a finding produces sibling PRs that merge
    individually while breaking each other's contracts. For that reason there
    is deliberately no directory-based fallback split, and a partition that
    leaves any singleton cluster (a file with no proven affinity to either
    side) skips decomposition entirely.

    Clusters that exceed the small-scope file budget keep medium scope rather
    than being downgraded. If every cluster exceeds the small budget, skip
    decomposition entirely so the original medium finding stays intact.
    """
    from app.research_completion import SCOPE_FILE_BUDGETS  # noqa: PLC0415

    small_budget = SCOPE_FILE_BUDGETS.get("small", {}).get("total_files_max") or 8
    expanded: list[tuple[int, dict, str]] = []
    for orig_i, finding, fhash in deduplicated:
        f_scope = finding.get("scope", DEFAULT_TASK_SCOPE)
        f_files = finding.get("related_files") or []
        f_already_calibrated = bool(
            finding.get("scope_calibrated_from")
            or (task_context or {}).get("scope_calibrated_from")
        )
        if f_scope == "medium" and len(f_files) >= 2 and not f_already_calibrated:
            f_clusters: list[list[str]] | None = None
            cluster_source = "co-change"
            if len(f_files) >= 3 and cochange_index:
                f_clusters = cluster_files_by_cochange(f_files, cochange_index)
            # Singleton clusters mean a file had no proven affinity to either
            # side — absence of evidence, not independence. Keep the finding
            # whole rather than strand that file in its own fragment.
            if f_clusters is not None and any(len(c) < 2 for c in f_clusters):
                f_clusters = None
            if f_clusters and len(f_clusters) >= 2:
                f_title = finding.get("title", f"Research finding #{orig_i + 1}")
                if all(len(c) > small_budget for c in f_clusters):
                    logger.info(
                        "[RESEARCH] Skipping co-change decomposition for "
                        "'%s': all %d clusters exceed small-scope budget "
                        "(%d files)",
                        f_title,
                        len(f_clusters),
                        small_budget,
                    )
                    expanded.append((orig_i, finding, fhash))
                    continue
                logger.info(
                    "[RESEARCH] Auto-decomposing medium finding '%s' into %d "
                    "sub-findings via %s clustering: %s",
                    f_title,
                    len(f_clusters),
                    cluster_source,
                    f_clusters,
                )
                for ci, cluster in enumerate(f_clusters):
                    sub = dict(finding)
                    if len(cluster) > small_budget:
                        sub["scope"] = "medium"
                    else:
                        sub["scope"] = "small"
                        sub["scope_calibrated_from"] = "medium"
                    sub["related_files"] = cluster
                    sub["title"] = f"{f_title} (cluster {ci + 1}/{len(f_clusters)})"
                    sub["auto_decomposed"] = True
                    file_lines = "\n".join(f"- {p}" for p in cluster)
                    other_count = len(f_clusters) - 1
                    noun = "cluster" if other_count == 1 else "clusters"
                    preamble = (
                        f"## Scope boundary (cluster {ci + 1}/{len(f_clusters)})\n"
                        "This task is one part of a larger finding that was "
                        "auto-decomposed by co-change clustering. Focus your "
                        "implementation exclusively on this cluster's "
                        f"{len(cluster)} file(s):\n"
                        f"{file_lines}\n"
                        f"{other_count} other {noun} cover the remaining "
                        "files from the original finding and will be handled "
                        "by separate tasks. "
                        "Do not modify files outside this cluster — edits to "
                        "other files belong to the sibling tasks. "
                        "If your declared files were already merged by a sibling cluster, "
                        "exit cleanly via SCOPE_EMPTY rather than retrying.\n\n"
                    )
                    if sub.get("prompt"):
                        sub["prompt"] = preamble + sub["prompt"]
                    elif sub.get("description"):
                        sub["description"] = preamble + sub["description"]
                    else:
                        sub["prompt"] = preamble
                    sub_hash = finding_hash_fn(sub["title"], sub.get("description", ""))
                    expanded.append((orig_i, sub, sub_hash))
                continue
        expanded.append((orig_i, finding, fhash))
    return expanded

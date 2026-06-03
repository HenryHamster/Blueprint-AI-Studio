"""Analyze the co-change ("frequency") index + auto-decomposition payoff for a project.

Read-only. Reports whether the cochange_index is being maintained, how dense it
is, and the empirical merge rate of cochange/dir auto-decomposition sub-findings
— the data needed to decide if the co-change machinery earns its keep.

Usage:
    python scripts/analyze_cochange.py [project_name_or_id_substring]
    (defaults to the first project whose repo/name matches "blueprint")
"""
import argparse
import sys

from blueprint_api import api

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _projects():
    resp = api("GET", "/api/admin/projects")
    return resp.get("items", resp) if isinstance(resp, dict) else resp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default="blueprint")
    args = ap.parse_args()
    q = args.query.lower()

    if q == "all":
        print(f"{'status':10} {'#dirs':>5} {'idx':>4} {'pairs':>6} {'merge':>6}  name / id")
        for p in _projects():
            idx = (p.get("design_doc") or {}).get("cochange_index") or {}
            pairs = sum(len(v or {}) for v in idx.values())
            dirs = p.get("directions") or []
            sub = merged = 0
            for d in dirs:
                for e in d.get("research_history") or []:
                    cd = e.get("cochange_decomposition") or {}
                    sub += cd.get("sub_findings_total", 0) or 0
                    merged += cd.get("sub_findings_merged", 0) or 0
            mr = f"{round(100*merged/sub)}%" if sub else "-"
            print(f"{str(p.get('status'))[:10]:10} {len(dirs):>5} {len(idx):>4} "
                  f"{pairs:>6} {mr:>6}  {p.get('name')} / {p.get('id')}")
        return

    proj = None
    for p in _projects():
        hay = f"{p.get('id','')} {p.get('name','')} {p.get('github_repo','')}".lower()
        if q in hay:
            proj = p
            break
    if not proj:
        print(f"No project matching {args.query!r}")
        return

    print(f"PROJECT {proj.get('name')!r} ({proj.get('id')})  repo={proj.get('github_repo')}")

    # ── cochange_index density ────────────────────────────────────────────
    idx = (proj.get("design_doc") or {}).get("cochange_index") or {}
    pairs = sum(len(v or {}) for v in idx.values())
    all_scores = [s for v in idx.values() for s in (v or {}).values()]
    strong = [s for s in all_scores if s >= 1.0]
    print("\n=== cochange_index ('the frequency') ===")
    print(f"  files tracked:      {len(idx)} (cap 200)")
    print(f"  directed pairs:     {pairs}")
    if all_scores:
        print(f"  score range:        {min(all_scores)} .. {max(all_scores)}")
        print(f"  pairs >= 1.0 (used by clustering threshold): {len(strong)} "
              f"({round(100*len(strong)/len(all_scores))}% of pairs)")
    else:
        print("  (empty — clustering always falls back to dir-based)")

    # ── auto-decomposition payoff across directions ───────────────────────
    # The projects-list endpoint strips the heavy directions array; fetch the
    # full set (with research_history) from the dedicated endpoint.
    dirs = proj.get("directions") or []
    try:
        dresp = api("GET", f"/api/admin/projects/{proj.get('id')}/directions")
        fetched = dresp.get("directions", dresp) if isinstance(dresp, dict) else dresp
        if fetched:
            dirs = fetched
    except Exception as e:
        print(f"  (directions endpoint failed: {e})")
    agg_split = agg_sub = agg_merged = 0
    per_dir = []
    suppressed = 0
    for d in dirs:
        hist = d.get("research_history") or []
        d_total = d_merged = d_split = 0
        for e in hist:
            cd = e.get("cochange_decomposition") or {}
            d_split += cd.get("medium_findings_split", 0) or 0
            d_total += cd.get("sub_findings_total", 0) or 0
            d_merged += cd.get("sub_findings_merged", 0) or 0
        if d_total:
            per_dir.append((d.get("id"), d_split, d_merged, d_total))
            agg_split += d_split
            agg_sub += d_total
            agg_merged += d_merged
        # mirror _cochange_decomposition_suppressed (min 3 sub, <0.4 merge rate)
        if d_total >= 3 and (d_merged / d_total) < 0.4:
            suppressed += 1

    print("\n=== auto-decomposition payoff (all directions) ===")
    print(f"  directions with decomposition history: {len(per_dir)} / {len(dirs)}")
    print(f"  medium findings split:  {agg_split}")
    print(f"  sub-findings produced:  {agg_sub}")
    print(f"  sub-findings merged:    {agg_merged}")
    if agg_sub:
        print(f"  overall merge rate:     {round(100*agg_merged/agg_sub)}%  "
              f"(suppression kicks in below 40%)")
    print(f"  directions now SUPPRESSED (poor merge rate): {suppressed}")
    if per_dir:
        print("\n  per-direction (id, split, merged/total):")
        for did, sp, mg, tot in sorted(per_dir, key=lambda x: -x[3]):
            print(f"    {did}: split={sp}  merged={mg}/{tot} "
                  f"({round(100*mg/tot)}%)")


if __name__ == "__main__":
    main()

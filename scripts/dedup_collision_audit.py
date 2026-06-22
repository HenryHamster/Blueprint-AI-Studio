"""Reconstruct WHY research findings are being word-overlap-filtered.

For every recent COMPLETED research task that spawned 0 children but stored
findings, replay the DEPLOYED dedup gate logic (base 4/6-word + technical-token
threshold-1 + bigram-Jaccard) against the current active-task corpus and report,
per finding, which gate fired and what title it collided with.

Also reports corpus size + which gate dominates, so we can tell apart:
  * genuine saturation (findings truly duplicate in-flight work), vs
  * over-aggressive gate (single shared file/identifier token trips threshold-1).
"""
from __future__ import annotations
import sys
sys.path.insert(0, "scripts")
from blueprint_api import list_tasks  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_STOP = {"the","a","an","to","for","and","or","in","of","is","it","this","that",
         "with","by","we","should","be","are","on","as","feat","fix","refactor",
         "add","update","improve"}

def tech_tokens(title: str) -> set[str]:
    out = set()
    for w in title.split():
        s = w.strip("'\"()[]{}.,;:!?")
        if not s or s.lower() in _STOP:
            continue
        if ("_" in s or "/" in s or "::" in s or "." in s or
                (s != s.lower() and s != s.upper() and len(s) > 3)):
            out.add(s.lower())
    return out

def bigrams(title: str) -> set:
    toks = [w.lower() for w in title.split() if w.lower() not in _STOP]
    if len(toks) < 2:
        return set()
    return {(toks[i], toks[i+1]) for i in range(len(toks)-1)}

def jac(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    u = a | b
    return len(a & b) / len(u) if u else 0.0

def match(ftitle, fdir, corpus):
    """Return (matched_title, gate) or (None, None). corpus: list of (dir,title)."""
    words = set(ftitle.lower().split()) - _STOP
    ftech = tech_tokens(ftitle)
    fbg = bigrams(ftitle)
    for cdir, ctitle in corpus:
        same = (not fdir) or (not cdir) or (fdir == cdir)
        overlap = words & (set(ctitle.lower().split()) - _STOP)
        if len(overlap) >= (4 if same else 6):
            return ctitle, f"word({len(overlap)})"
        if ftech:
            ct = tech_tokens(ctitle)
            if len(ftech & ct) >= (1 if same else 2):
                return ctitle, f"tech{sorted(ftech & ct)}"
        if fbg:
            cb = bigrams(ctitle)
            if cb and jac(fbg, cb) >= (0.65 if same else 0.80):
                return ctitle, f"bigram({jac(fbg,cb):.2f})"
    return None, None

ACTIVE = ("QUEUED","IN_PROGRESS","EVALUATING","PROVISIONING","BLOCKED","PARKED")

def main():
    allt = {}
    kids = {}
    for st in ACTIVE + ("COMPLETED", "FAILED", "CANCELLED"):
        for t in list_tasks(status=st, limit=400):
            allt[str(t["id"])] = t
            p = t.get("parent_task_id")
            if p:
                kids.setdefault(p, []).append(t)

    def proj(t):
        return str(t.get("project_id"))

    # Per-project corpora: (a) active-task titles, (b) recent-terminal research
    # finding titles (the cross-cycle dedup seed).
    active_corpus = {}
    terminal_corpus = {}
    for t in allt.values():
        pid = proj(t)
        if t.get("status") in ACTIVE and t.get("title"):
            active_corpus.setdefault(pid, []).append(
                ((t.get("context") or {}).get("direction_id"), t["title"].lower()))
        if t.get("task_type") == "research" and t.get("status") in (
                "COMPLETED", "FAILED", "CANCELLED"):
            for f in (t.get("plan_output") or []):
                if isinstance(f, dict) and f.get("title"):
                    terminal_corpus.setdefault(pid, []).append(
                        (f.get("direction_id"), f["title"].lower(), str(t["id"])))
    print(f"# projects seen: {len(set(proj(t) for t in allt.values()))}")
    for pid in active_corpus:
        print(f"#   {pid[:8]}: active={len(active_corpus.get(pid,[]))} "
              f"terminal_research_findings={len(terminal_corpus.get(pid,[]))}")
    print()

    # Gate-firing tally over all findings of 0-child COMPLETED research tasks.
    gate_tally = {}
    tech_token_hits = {}
    targets = []
    for t in allt.values():
        if t.get("task_type") != "research" or t.get("status") != "COMPLETED":
            continue
        if kids.get(str(t["id"])):
            continue
        findings = t.get("plan_output") or t.get("research_findings") or []
        if isinstance(findings, list) and findings:
            targets.append(t)

    print(f"# {len(targets)} COMPLETED research tasks with findings + 0 children\n")
    for t in targets:
        findings = t.get("plan_output") or t.get("research_findings") or []
        fdir = (t.get("context") or {}).get("direction_id")
        pid = proj(t)
        # corpus = active tasks + recent-terminal findings from OTHER tasks (cross-cycle)
        corpus = list(active_corpus.get(pid, []))
        corpus += [(d, ti) for (d, ti, src) in terminal_corpus.get(pid, [])
                   if src != str(t["id"])]
        print(f"## {str(t['id'])[:8]}  ({len(findings)} findings)  dir={str(fdir)[:8]}  proj={pid[:8]}")
        for f in findings:
            if not isinstance(f, dict):
                continue
            ftitle = f.get("title", "")
            ftype = f.get("type", "improvement")
            mt, gate = match(ftitle, f.get("direction_id") or fdir, corpus)
            if ftype in ("design_doc_observation", "direction_update"):
                tag = f"[type-filtered:{ftype}]"
            elif gate:
                tag = f"[{gate}] -> {mt[:55]}"
                gate_key = gate.split("(")[0].split("[")[0]
                gate_tally[gate_key] = gate_tally.get(gate_key, 0) + 1
                if gate.startswith("tech"):
                    for tok in gate[4:].strip("[]").replace("'","").split(", "):
                        tech_token_hits[tok] = tech_token_hits.get(tok, 0) + 1
            else:
                tag = "[would SPAWN — no collision]"
            print(f"   {ftitle[:70]:<72} {tag}")
        print()

    print("# gate-firing tally across all filtered findings:")
    for g, n in sorted(gate_tally.items(), key=lambda x: -x[1]):
        print(f"   {g:<10} {n}")
    print("\n# most common technical tokens that tripped threshold-1 tech gate:")
    for tok, n in sorted(tech_token_hits.items(), key=lambda x: -x[1])[:15]:
        print(f"   {tok:<40} {n}")

if __name__ == "__main__":
    main()

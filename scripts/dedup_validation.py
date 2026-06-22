"""Validation: is title-overlap dedup redundant vs hash + region/claim identity?

For every spawn-candidate finding on a recent zero-child research task, replay
the DEPLOYED title-overlap gate against the cross-cycle terminal-finding corpus,
then ask: of the findings title-overlap FILTERS, how many would ALSO be caught by
  (1) exact hash, or
  (2) region+claim identity (related_files overlap + shared claim/source),
and how many are FALSE POSITIVES (no structural overlap with the match = distinct
work wrongly dropped on prose).

Decision rule:
  * If title-only-dups (caught by title, NOT by hash/region-claim, yet plausibly
    same work) are ~0 → title-overlap earns nothing hash+region/claim doesn't →
    safe to remove.
  * False positives recovered = pure win.
"""
from __future__ import annotations
import sys, hashlib
sys.path.insert(0, "scripts")
from blueprint_api import list_tasks  # noqa: E402
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_STOP = {"the","a","an","to","for","and","or","in","of","is","it","this","that",
         "with","by","we","should","be","are","on","as","feat","fix","refactor",
         "add","update","improve"}
TYPE_FILTERED = {"design_doc_observation", "direction_update"}
ACTIVE = ("QUEUED","IN_PROGRESS","EVALUATING","PROVISIONING","BLOCKED","PARKED")

def tech_tokens(t):
    out=set()
    for w in t.split():
        s=w.strip("'\"()[]{}.,;:!?")
        if not s or s.lower() in _STOP: continue
        if ("_" in s or "/" in s or "::" in s or "." in s or
                (s!=s.lower() and s!=s.upper() and len(s)>3)):
            out.add(s.lower())
    return out

def bigrams(t):
    toks=[w.lower() for w in t.split() if w.lower() not in _STOP]
    return {(toks[i],toks[i+1]) for i in range(len(toks)-1)} if len(toks)>=2 else set()

def jac(a,b):
    u=a|b
    return len(a&b)/len(u) if u else 0.0

def title_match(ftitle, fdir, cand):
    """Deployed title-overlap logic. cand: list of (dir,title,fulldict)."""
    words=set(ftitle.lower().split())-_STOP
    ftech=tech_tokens(ftitle); fbg=bigrams(ftitle)
    for cdir,ctitle,cd in cand:
        same=(not fdir) or (not cdir) or (fdir==cdir)
        if len(words & (set(ctitle.lower().split())-_STOP)) >= (4 if same else 6):
            return cd, f"word"
        if ftech and len(ftech & tech_tokens(ctitle)) >= (1 if same else 2):
            return cd, f"tech:{sorted(ftech & tech_tokens(ctitle))}"
        cb=bigrams(ctitle)
        if fbg and cb and jac(fbg,cb) >= (0.65 if same else 0.80):
            return cd, "bigram"
    return None, None

def fhash(f):
    title=f.get("title",""); desc=str(f.get("description") or f.get("prompt") or "").strip()
    return hashlib.sha256((title+desc).encode()).hexdigest()[:16]

def files_of(f):
    rf=f.get("related_files") or []
    return {str(x).split(":")[0].strip().lower() for x in rf if x} if isinstance(rf,list) else set()

def claims_of(f):
    cl=f.get("claims") or []
    return {" ".join(str(c).lower().split()) for c in cl if c} if isinstance(cl,list) else set()

def srcrefs_of(f):
    s=f.get("sources") or []
    out=set()
    if isinstance(s,list):
        for x in s:
            if isinstance(x,dict) and x.get("ref"):
                out.add(str(x["ref"]).split("#")[0].split(":")[0].strip().lower())
    return out

def region_claim_match(a, b):
    """Proposed identity: file overlap AND (shared claim OR source-ref overlap).
    Returns (matched, detail)."""
    fa, fb = files_of(a), files_of(b)
    file_int = fa & fb
    file_jac = jac(fa, fb)
    region = bool(fa and fb) and (len(file_int) >= 2 or file_jac >= 0.5)
    ca, cb = claims_of(a), claims_of(b)
    sa, sb = srcrefs_of(a), srcrefs_of(b)
    claim_ov = bool(ca & cb) or (bool(sa and sb) and jac(sa, sb) >= 0.5)
    if region and claim_ov:
        return True, f"files∩={len(file_int)} jac={file_jac:.2f} claim/src=yes"
    return False, f"files∩={len(file_int)} jac={file_jac:.2f} region={region} claim/src={claim_ov}"

def main():
    allt={}; kids={}
    for st in ACTIVE+("COMPLETED","FAILED","CANCELLED"):
        for t in list_tasks(status=st, limit=400):
            allt[str(t["id"])]=t
            p=t.get("parent_task_id")
            if p: kids.setdefault(p,[]).append(t)

    # per-project recent-terminal research findings (full dicts) + their hashes
    term={}; term_hashes={}
    for t in allt.values():
        pid=str(t.get("project_id"))
        if t.get("task_type")=="research" and t.get("status") in ("COMPLETED","FAILED","CANCELLED"):
            for f in (t.get("plan_output") or []):
                if isinstance(f,dict) and f.get("title"):
                    term.setdefault(pid,[]).append((f.get("direction_id"), f["title"].lower(), f, str(t["id"])))
        # task-level finding hashes (from context) for hash-dedup approximation
        fh=(t.get("context") or {}).get("finding_hash")
        if fh: term_hashes.setdefault(pid,set()).add(str(fh))

    targets=[t for t in allt.values()
             if t.get("task_type")=="research" and t.get("status")=="COMPLETED"
             and not kids.get(str(t["id"])) and isinstance(t.get("plan_output"),list) and t.get("plan_output")]

    pop_total=0; pop_files=0; pop_claims=0
    stats={"hash":0,"title_and_regionclaim":0,"title_only_FALSEPOS":0,
           "title_only_PLAUSIBLE_DUP":0,"not_filtered":0,"type_filtered":0}
    falsepos=[]; plausible=[]
    for t in targets:
        pid=str(t.get("project_id")); fdir=(t.get("context") or {}).get("direction_id")
        cand=[(d,ti,fd) for (d,ti,fd,src) in term.get(pid,[]) if src!=str(t["id"])]
        for f in t.get("plan_output"):
            if not isinstance(f,dict): continue
            ftype=f.get("type","improvement")
            if ftype in TYPE_FILTERED:
                stats["type_filtered"]+=1; continue
            pop_total+=1
            if files_of(f): pop_files+=1
            if claims_of(f): pop_claims+=1
            h=fhash(f)
            hash_hit = h in term_hashes.get(pid,set())
            cd, tgate = title_match(f.get("title",""), f.get("direction_id") or fdir, cand)
            if hash_hit:
                stats["hash"]+=1; continue
            if cd is None:
                stats["not_filtered"]+=1; continue
            rc_match, rc_detail = region_claim_match(f, cd)
            if rc_match:
                stats["title_and_regionclaim"]+=1
            else:
                # title filters, structure does NOT. genuine dup or false positive?
                fa, fb = files_of(f), files_of(cd)
                file_int = fa & fb
                if file_int:  # some file overlap but below identity threshold → plausible dup
                    stats["title_only_PLAUSIBLE_DUP"]+=1
                    plausible.append((f.get("title","")[:55], cd.get("title","")[:45], tgate, rc_detail))
                else:  # zero file overlap → distinct work dropped on prose
                    stats["title_only_FALSEPOS"]+=1
                    falsepos.append((f.get("title","")[:55], cd.get("title","")[:45], tgate, rc_detail))

    print(f"# spawn-candidate findings analyzed: {pop_total}")
    print(f"#   with related_files populated: {pop_files} ({100*pop_files//max(pop_total,1)}%)")
    print(f"#   with claims populated:        {pop_claims} ({100*pop_claims//max(pop_total,1)}%)")
    print(f"# type-filtered (not candidates): {stats['type_filtered']}\n")
    print("# how title-overlap-filtered findings would fare under hash + region/claim:")
    print(f"   caught by exact HASH (title-overlap redundant):        {stats['hash']}")
    print(f"   title + region/claim AGREE (title redundant):          {stats['title_and_regionclaim']}")
    print(f"   TITLE-ONLY, plausible dup (some file overlap):         {stats['title_only_PLAUSIBLE_DUP']}")
    print(f"   TITLE-ONLY, FALSE POSITIVE (zero structural overlap):  {stats['title_only_FALSEPOS']}")
    print(f"   not filtered at all (would spawn):                     {stats['not_filtered']}")
    print("\n# FALSE POSITIVES recovered by dropping title-overlap (sample):")
    for a,b,g,d in falsepos[:20]:
        print(f"   '{a}'\n      x-collided[{g}] '{b}'  ({d})")
    print("\n# TITLE-ONLY plausible dups (the risk set — only title catches these):")
    for a,b,g,d in plausible[:20]:
        print(f"   '{a}'\n      vs '{b}'  [{g}]  ({d})")

if __name__=="__main__":
    main()

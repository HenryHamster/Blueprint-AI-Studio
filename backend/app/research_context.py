"""Research context document management.

Pure-text functions for parsing, formatting, and building research_context
markdown documents. Extracted from app.research_completion to keep that
module focused on pipeline orchestration. These functions have no coupling
to DB, LLM, or broadcast layers — they operate only on strings and dicts.
"""

import datetime
import re
from typing import Optional


def _parse_research_context(text: str) -> dict:
    """Parse a research_context string into its 3 sections and optional eviction summary."""
    result: dict = {
        "established": [],
        "failed": [],
        "open_questions": [],
        "eviction_summary": None,
    }
    if not text:
        return result
    section_map = {
        "## Established Findings": "established",
        "## Failed Approaches": "failed",
        "## Open Questions": "open_questions",
    }
    current_key: Optional[str] = None
    in_eviction = False
    eviction_lines: list = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "## Eviction Summary":
            in_eviction = True
            current_key = None
        elif stripped in section_map:
            in_eviction = False
            current_key = section_map[stripped]
        elif in_eviction and stripped:
            eviction_lines.append(stripped)
        elif current_key and stripped.startswith("- "):
            result[current_key].append(stripped[2:])
    if eviction_lines:
        result["eviction_summary"] = "\n".join(eviction_lines)
    return result


def _parse_eviction_counts(summary_text: Optional[str]) -> dict:
    """Parse counts and failed titles from an eviction summary body."""
    result: dict = {
        "established": 0,
        "established_confirmed": 0,
        "failed": 0,
        "open_questions": 0,
        "failed_titles": [],
    }
    if not summary_text:
        return result
    for line in summary_text.splitlines():
        line = line.strip()
        # Check "Failed:" prefix before "evicted" keyword — titles may themselves contain "evicted"
        if line.startswith("Failed:"):
            titles_str = line[len("Failed:") :].strip()
            result["failed_titles"] = [
                t.strip() for t in titles_str.split(";") if t.strip()
            ]
        elif "evicted" in line:
            m = re.search(r"(\d+) established", line)
            if m:
                result["established"] = int(m.group(1))
            # Confirmed sub-count: established evictions that were neither
            # (proposed) nor (failed:) at eviction time. Legacy summaries
            # written before this sub-count existed parse to 0 (a safe
            # undercount, never an over-count).
            m = re.search(r"(\d+) confirmed", line)
            if m:
                result["established_confirmed"] = int(m.group(1))
            m = re.search(r"(\d+) failed", line)
            if m:
                result["failed"] = int(m.group(1))
            m = re.search(r"(\d+) open question", line)
            if m:
                result["open_questions"] = int(m.group(1))
    return result


def _format_evicted_failed(entry: str, max_len: int = 60) -> str:
    """Format an evicted failed-approach entry preserving the category.

    Failed entries follow '{title} ({category}) [at=... stage=...] — {reason}'.
    Long titles push the category past a naive prefix truncation, so extract
    the category and ensure it is preserved within max_len.
    """
    m = re.match(r"^(.*?)\s*\(([^)]+)\)(?:\s*\[|\s*—|\s*$)", entry)
    if not m:
        return entry[:max_len]
    title = m.group(1).strip()
    category = m.group(2).strip()
    suffix = f" ({category})"
    budget = max_len - len(suffix)
    if budget <= 0:
        return entry[:max_len]
    if len(title) <= budget:
        return f"{title}{suffix}"
    ellipsis = "..."
    truncated = title[: max(0, budget - len(ellipsis))] + ellipsis
    return f"{truncated}{suffix}"


def _format_eviction_body(
    established: int,
    failed: int,
    open_questions: int,
    failed_titles: list,
    established_confirmed: int = 0,
) -> str:
    """Format the eviction summary body content (without the ## header line).

    ``established_confirmed`` is the subset of evicted established entries that
    were confirmed (neither ``(proposed)`` nor ``(failed:)``) at eviction time.
    It is emitted as a parenthetical on the established count so the
    outcome-progress proxy can credit only durable evictions, mirroring the
    visible-count exclusion. Omitted when zero so legacy/empty summaries stay
    compact.
    """
    count_parts = []
    if established > 0:
        if established_confirmed > 0:
            count_parts.append(
                f"{established} established ({established_confirmed} confirmed)"
            )
        else:
            count_parts.append(f"{established} established")
    if failed > 0:
        count_parts.append(f"{failed} failed")
    if open_questions > 0:
        plural = "s" if open_questions != 1 else ""
        count_parts.append(f"{open_questions} open question{plural}")
    lines = []
    if count_parts:
        lines.append(", ".join(count_parts) + " evicted")
    if failed_titles:
        lines.append("Failed: " + "; ".join(failed_titles))
    return "\n".join(lines)


def _parse_established_entry(entry_text: str) -> dict:
    """Parse a formatted established-findings entry into a structured eviction record.

    Returns {title, claims, source_refs, status} — safe to store in history_aggregate.
    """
    title: str = entry_text
    claims: list = []
    source_refs: list = []
    status: str = "established"

    # Extract status tag: (proposed), (merged), (unproven), (stale), (failed: category)
    status_m = re.search(
        r"\((proposed|merged|unproven|stale|failed:[^)]*)\)", entry_text
    )
    if status_m:
        status = status_m.group(1).strip()

    # Title is everything before the first [scope] tag
    scope_m = re.match(r"^(.*?)\s*\[", entry_text)
    if scope_m:
        title = scope_m.group(1).strip()
    else:
        paren_m = re.search(
            r"\s*\((?:proposed|merged|unproven|stale|failed)", entry_text
        )
        if paren_m:
            title = entry_text[: paren_m.start()].strip()

    if ". Claims: " in entry_text:
        claims_raw = entry_text.split(". Claims: ", 1)[1]
        if ". Sources: " in claims_raw:
            claims_raw = claims_raw.split(". Sources: ", 1)[0]
        claims = [c.strip() for c in claims_raw.split(";") if c.strip()]

    if ". Sources: " in entry_text:
        sources_raw = entry_text.split(". Sources: ", 1)[1]
        source_refs = [s.strip() for s in sources_raw.split(",") if s.strip()]

    return {
        "title": title[:200],
        "claims": [c[:200] for c in claims[:3]],
        "source_refs": source_refs[:3],
        "status": status[:50],
    }


def _apply_prior_outcomes(established: list, prior_history: list) -> list:
    """Retag '(proposed)' entries from prior outcomes.

    Matches the title prefix of each established entry against prior cycles'
    finding_titles and failed_task_details / no_op_task_details /
    unproven_merge_details so agents can see which findings actually shipped
    ('(merged)'), shipped but with unsatisfied declared evidence ('(unproven)'),
    failed ('(failed: CATEGORY)'), or no-op'd as already-resolved /
    non-reproducing on HEAD ('(stale)'). Precedence is
    failed > unproven > merged > stale.
    """
    if not prior_history:
        return established
    outcomes: dict = {}
    for entry in prior_history:
        failed = entry.get("failed_task_details") or []
        failed_map = {
            (f.get("title") or "").strip(): (f.get("category") or "")
            for f in failed
            if f.get("title")
        }
        # Affirmative-no-op findings: the only child task no-op'd as
        # already-resolved / non-reproducing on HEAD. Tagged '(stale)' so the
        # research agent stops re-chasing a defect that was already gone.
        # failed/merged take precedence (handled by the 'tn in outcomes' /
        # ordering below), so a finding that also failed or shipped keeps that tag.
        no_op = entry.get("no_op_task_details") or []
        no_op_map = {
            (n.get("title") or "").strip()
            for n in no_op
            if n.get("title")
        }
        # Delivered-but-unproven merges: the finding's child task merged, but its
        # DECLARED acceptance evidence was UNSATISFIED, so the per-task scorer
        # downgraded it to neutral. Tagged '(unproven)' so the research agent
        # re-targets a finding that was shipped-but-not-proven instead of treating
        # it as resolved. A missing key (legacy history) reads as an empty list.
        # Precedence is failed > unproven > merged > stale: a finding that ALSO
        # genuinely shipped a verified merge keeps its '(merged)' tag (handled
        # below), but a per-finding unproven merge is never masked by a sibling's
        # verified merge in the same cycle.
        unproven = entry.get("unproven_merge_details") or []
        unproven_map = {
            (u.get("title") or "").strip()
            for u in unproven
            if u.get("title")
        }
        prs_merged = entry.get("prs_merged", 0) or 0
        # 'settled' tracks whether every created task reached a terminal state
        # (merged-or-failed) and must keep counting RAW merges — a merged-but-
        # unverified task is still terminal and accounted-for. The 'merged' TAG,
        # however, signals shipped work and so requires a VERIFIED merge; a PR
        # that merged without its claim reproducing on HEAD is not shipped work.
        # Function-scope import: direction_outcomes imports research_context
        # lazily, so a module-level import here would be circular.
        from app.direction_outcomes import verified_merged_count

        verified_merged = verified_merged_count(entry)
        tasks_created = entry.get("tasks_created", 0) or 0
        settled = tasks_created > 0 and (prs_merged + len(failed)) >= tasks_created
        for t in entry.get("finding_titles") or []:
            tn = (t or "").strip()
            if not tn or tn in outcomes:
                continue
            if tn in failed_map:
                cat = failed_map[tn] or "unknown"
                outcomes[tn] = f"failed: {cat}"
            elif tn in unproven_map:
                # Delivered-but-unproven merge: shipped, but declared evidence did
                # not hold. Ranks above the cycle-level 'merged' tag so a sibling
                # finding's verified merge cannot mask this finding's unproven one.
                outcomes[tn] = "unproven"
            elif settled and verified_merged > 0:
                outcomes[tn] = "merged"
            elif tn in no_op_map:
                outcomes[tn] = "stale"
    if not outcomes:
        return established
    updated = []
    for entry_text in established:
        if "(proposed)" not in entry_text:
            updated.append(entry_text)
            continue
        head = entry_text.split(" (proposed)", 1)[0]
        title_key = head.split(" [", 1)[0].strip()
        new_tag = outcomes.get(title_key)
        if new_tag:
            updated.append(entry_text.replace("(proposed)", f"({new_tag})", 1))
        else:
            updated.append(entry_text)
    return updated


def _build_research_context(
    prior_context: Optional[str],
    finding_titles: list,
    key_finding_descriptions: list,
    cycle_insights: list,
    history_entry: dict,
    finding_scopes: Optional[list] = None,
    prior_history: Optional[list] = None,
    finding_claims: Optional[list] = None,
    finding_sources: Optional[list] = None,
    evicted_out: Optional[list] = None,
) -> str:
    """Build a compounding research_context document deterministically (no LLM)."""
    parsed = _parse_research_context(prior_context or "")
    if prior_history:
        parsed["established"] = _apply_prior_outcomes(
            parsed["established"], prior_history
        )

    # Parse prior eviction summary for accumulation
    prior_eviction = _parse_eviction_counts(parsed["eviction_summary"])

    # Append new findings to Established Findings as (proposed)
    for i, title in enumerate(finding_titles):
        desc = key_finding_descriptions[i] if i < len(key_finding_descriptions) else ""
        scope = (
            finding_scopes[i] if finding_scopes and i < len(finding_scopes) else None
        ) or "small"
        scope_tag = f" [{scope}]"
        if desc:
            first_sentence = desc.split(".")[0].strip()
            entry_text = (
                f"{title}{scope_tag} (proposed). {first_sentence}"
                if first_sentence
                else f"{title}{scope_tag} (proposed)"
            )
        else:
            entry_text = f"{title}{scope_tag} (proposed)"
        _claims = (
            finding_claims[i] if finding_claims and i < len(finding_claims) else None
        ) or []
        _sources = (
            finding_sources[i] if finding_sources and i < len(finding_sources) else None
        ) or []
        if _claims:
            entry_text += ". Claims: " + "; ".join(str(c)[:80] for c in _claims[:6])
        if _sources:
            _ref_parts = []
            for s in _sources:
                if not isinstance(s, dict):
                    continue
                ref = s.get("ref", "")
                if not ref:
                    continue
                excerpt = s.get("excerpt", "")
                _ref_parts.append(f"{ref} ({excerpt[:60]})" if excerpt else ref)
            if _ref_parts:
                entry_text += ". Sources: " + ", ".join(_ref_parts)
        parsed["established"].append(entry_text)

    # Add cycle_insights to Open Questions (deduplicate)
    existing_oq = set(parsed["open_questions"])
    for insight in cycle_insights:
        if insight and insight not in existing_oq:
            parsed["open_questions"].append(insight)
            existing_oq.add(insight)

    def _format_doc(sections: dict, eviction_body: Optional[str] = None) -> str:
        parts: list = []
        if eviction_body:
            parts.append("## Eviction Summary")
            parts.append(eviction_body)
            parts.append("")
        for header, key in [
            ("## Established Findings", "established"),
            ("## Failed Approaches", "failed"),
            ("## Open Questions", "open_questions"),
        ]:
            parts.append(header)
            for item in sections[key]:
                parts.append(f"- {item}")
            parts.append("")
        return "\n".join(parts).strip() + "\n"

    MAX_MAIN_CHARS = 7500
    EVICTION_MAX = 500
    TOTAL_MAX = MAX_MAIN_CHARS + EVICTION_MAX  # 8000

    doc = _format_doc(parsed)
    if len(doc) <= MAX_MAIN_CHARS:
        # No trimming needed; preserve any existing eviction summary
        if (
            prior_eviction["established"]
            or prior_eviction["failed"]
            or prior_eviction["open_questions"]
        ):
            eviction_body = _format_eviction_body(
                prior_eviction["established"],
                prior_eviction["failed"],
                prior_eviction["open_questions"],
                prior_eviction["failed_titles"],
                prior_eviction["established_confirmed"],
            )
            return _format_doc(parsed, eviction_body[:EVICTION_MAX])[:TOTAL_MAX]
        return doc

    # Snapshot sections before trimming
    before = {
        k: list(v)
        for k, v in parsed.items()
        if k in ("established", "failed", "open_questions")
    }

    # Trim oldest entries (from top of each section) to stay under main limit
    while len(doc) > MAX_MAIN_CHARS:
        trimmed = False
        for key in sorted(
            ["established", "failed", "open_questions"],
            key=lambda k: -len(parsed[k]),
        ):
            if len(parsed[key]) > 1:
                parsed[key] = parsed[key][1:]
                trimmed = True
                break
        if not trimmed:
            break
        doc = _format_doc(parsed)

    # Compute newly evicted entries
    new_established = len(before["established"]) - len(parsed["established"])
    new_failed = len(before["failed"]) - len(parsed["failed"])
    new_oq = len(before["open_questions"]) - len(parsed["open_questions"])

    # Confirmed sub-count: of the evicted established entries, how many were
    # durable (neither proposed nor failed). The outcome-progress proxy credits
    # only these, matching the visible-count exclusion so a saturated direction
    # that evicts only churn (proposed/failed) entries does not appear to advance.
    new_established_confirmed = sum(
        1
        for e in before["established"][:new_established]
        if "(proposed)" not in e and "(failed:" not in e
    )

    evicted_failed_titles = (
        [_format_evicted_failed(e) for e in before["failed"][:new_failed]]
        if new_failed > 0
        else []
    )

    # Populate structured eviction records for callers that want audit history
    if evicted_out is not None:
        _evicted_at = datetime.datetime.utcnow().isoformat()
        for e in before["established"][:new_established]:
            rec = _parse_established_entry(e)
            rec["evicted_at"] = _evicted_at
            evicted_out.append(rec)
        for e in before["failed"][:new_failed]:
            evicted_out.append(
                {
                    "title": e[:200],
                    "claims": [],
                    "source_refs": [],
                    "status": "failed",
                    "evicted_at": _evicted_at,
                }
            )
        for e in before["open_questions"][:new_oq]:
            evicted_out.append(
                {
                    "title": e[:200],
                    "claims": [],
                    "source_refs": [],
                    "status": "open_question",
                    "evicted_at": _evicted_at,
                }
            )

    # Merge with prior eviction summary
    total_established = prior_eviction["established"] + new_established
    total_established_confirmed = (
        prior_eviction["established_confirmed"] + new_established_confirmed
    )
    total_failed = prior_eviction["failed"] + new_failed
    total_oq = prior_eviction["open_questions"] + new_oq
    all_failed_titles = prior_eviction["failed_titles"] + evicted_failed_titles

    eviction_body = _format_eviction_body(
        total_established,
        total_failed,
        total_oq,
        all_failed_titles,
        total_established_confirmed,
    )
    return _format_doc(parsed, eviction_body[:EVICTION_MAX])[:TOTAL_MAX]


_EVIDENCE_TITLE_RE = re.compile(r"^(.*?)\s*(?:\[.*?\])?\s*\(([^)]+)\)")


def filter_direction_evidence(
    research_context_str: Optional[str],
    raw_evidence: list,
    max_findings: int = 5,
    max_claims: int = 3,
    max_source_refs: int = 3,
) -> list:
    """Filter recent_findings_evidence to merged/proposed findings only.

    Cross-references titles against research_context status tags. Findings with
    status starting with 'failed' or equal to 'stale' (already-resolved /
    non-reproducing on HEAD) are excluded. Returns capped evidence entries.
    """
    if not raw_evidence:
        return []
    status_map: dict[str, str] = {}
    if research_context_str:
        parsed = _parse_research_context(research_context_str)
        for entry in parsed.get("established", []):
            m = _EVIDENCE_TITLE_RE.match(entry)
            if m:
                status_map[m.group(1).strip()] = m.group(2).strip().lower()
    result = []
    for ev in raw_evidence:
        title = (ev.get("title") or "").strip()
        status = status_map.get(title, "proposed")
        # Exclude proven-failed AND proven-stale (already-resolved / non-
        # reproducing on HEAD) findings so they are not re-surfaced as fresh
        # evidence the strategist would chase again.
        if status.startswith("failed") or status == "stale":
            continue
        entry = {
            "title": title,
            "claims": (ev.get("claims") or [])[:max_claims],
            "source_refs": (ev.get("source_refs") or [])[:max_source_refs],
        }
        if entry["claims"] or entry["source_refs"]:
            result.append(entry)
            if len(result) >= max_findings:
                break
    return result


def extract_diagnosis_from_context(research_context_str: Optional[str]) -> str:
    """Extract diagnosis summary from research context eviction section.

    Returns the eviction summary text (counts + failed titles) which represents
    the diagnosis of why a direction's research was curtailed.
    """
    if not research_context_str:
        return ""
    parsed = _parse_research_context(research_context_str)
    return parsed.get("eviction_summary") or ""


def build_recent_findings_evidence(
    finding_titles: list,
    finding_claims: Optional[list] = None,
    finding_sources: Optional[list] = None,
    max_findings: int = 5,
    max_chars: int = 2000,
) -> list:
    """Return compact structured evidence for the most recent N findings.

    Each entry is {title, claims, source_refs} where source_refs is a list of
    {ref, excerpt?} dicts. Used by autonomous_research to populate
    recent_findings_evidence in direction summaries for the strategic agent.
    Returns an empty list when there are no findings with claims or sources.
    """
    n = len(finding_titles)
    if not n:
        return []
    results = []
    total_chars = 0
    for i in range(max(0, n - max_findings), n):
        title = finding_titles[i]
        claims = list(
            (finding_claims[i] if finding_claims and i < len(finding_claims) else None)
            or []
        )
        raw_sources = (
            finding_sources[i] if finding_sources and i < len(finding_sources) else None
        ) or []
        source_refs = [
            {
                "ref": s["ref"],
                **({"excerpt": s["excerpt"][:120]} if s.get("excerpt") else {}),
            }
            for s in raw_sources
            if isinstance(s, dict) and s.get("ref")
        ]
        if not claims and not source_refs:
            continue
        entry: dict = {"title": title, "claims": claims[:6], "source_refs": source_refs}
        entry_size = len(str(entry))
        if total_chars + entry_size > max_chars and results:
            break
        results.append(entry)
        total_chars += entry_size
    return results

"""Research context document management.

Pure-text functions for parsing, formatting, and building research_context
markdown documents. Extracted from app.research_completion to keep that
module focused on pipeline orchestration. These functions have no coupling
to DB, LLM, or broadcast layers — they operate only on strings and dicts.
"""
import re
from typing import Optional


def _parse_research_context(text: str) -> dict:
    """Parse a research_context string into its 3 sections and optional eviction summary."""
    result: dict = {"established": [], "failed": [], "open_questions": [], "eviction_summary": None}
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
    result: dict = {"established": 0, "failed": 0, "open_questions": 0, "failed_titles": []}
    if not summary_text:
        return result
    for line in summary_text.splitlines():
        line = line.strip()
        # Check "Failed:" prefix before "evicted" keyword — titles may themselves contain "evicted"
        if line.startswith("Failed:"):
            titles_str = line[len("Failed:"):].strip()
            result["failed_titles"] = [t.strip() for t in titles_str.split(";") if t.strip()]
        elif "evicted" in line:
            m = re.search(r'(\d+) established', line)
            if m:
                result["established"] = int(m.group(1))
            m = re.search(r'(\d+) failed', line)
            if m:
                result["failed"] = int(m.group(1))
            m = re.search(r'(\d+) open question', line)
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


def _format_eviction_body(established: int, failed: int, open_questions: int, failed_titles: list) -> str:
    """Format the eviction summary body content (without the ## header line)."""
    count_parts = []
    if established > 0:
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


def _apply_prior_outcomes(established: list, prior_history: list) -> list:
    """Retag '(proposed)' entries as '(merged)' or '(failed: CATEGORY)' from prior outcomes.

    Matches the title prefix of each established entry against prior cycles'
    finding_titles and failed_task_details so agents can see which findings
    actually shipped vs failed.
    """
    if not prior_history:
        return established
    outcomes: dict = {}
    for entry in prior_history:
        failed = entry.get("failed_task_details") or []
        failed_map = {(f.get("title") or "").strip(): (f.get("category") or "") for f in failed if f.get("title")}
        prs_merged = entry.get("prs_merged", 0) or 0
        tasks_created = entry.get("tasks_created", 0) or 0
        settled = tasks_created > 0 and (prs_merged + len(failed)) >= tasks_created
        for t in entry.get("finding_titles") or []:
            tn = (t or "").strip()
            if not tn or tn in outcomes:
                continue
            if tn in failed_map:
                cat = failed_map[tn] or "unknown"
                outcomes[tn] = f"failed: {cat}"
            elif settled and prs_merged > 0:
                outcomes[tn] = "merged"
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
) -> str:
    """Build a compounding research_context document deterministically (no LLM)."""
    parsed = _parse_research_context(prior_context or "")
    if prior_history:
        parsed["established"] = _apply_prior_outcomes(parsed["established"], prior_history)

    # Parse prior eviction summary for accumulation
    prior_eviction = _parse_eviction_counts(parsed["eviction_summary"])

    # Append new findings to Established Findings as (proposed)
    for i, title in enumerate(finding_titles):
        desc = key_finding_descriptions[i] if i < len(key_finding_descriptions) else ""
        scope = (finding_scopes[i] if finding_scopes and i < len(finding_scopes) else None) or "small"
        scope_tag = f" [{scope}]"
        if desc:
            first_sentence = desc.split(".")[0].strip()
            entry_text = f"{title}{scope_tag} (proposed). {first_sentence}" if first_sentence else f"{title}{scope_tag} (proposed)"
        else:
            entry_text = f"{title}{scope_tag} (proposed)"
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
        if prior_eviction["established"] or prior_eviction["failed"] or prior_eviction["open_questions"]:
            eviction_body = _format_eviction_body(
                prior_eviction["established"],
                prior_eviction["failed"],
                prior_eviction["open_questions"],
                prior_eviction["failed_titles"],
            )
            return _format_doc(parsed, eviction_body[:EVICTION_MAX])[:TOTAL_MAX]
        return doc

    # Snapshot sections before trimming
    before = {k: list(v) for k, v in parsed.items() if k in ("established", "failed", "open_questions")}

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

    evicted_failed_titles = (
        [_format_evicted_failed(e) for e in before["failed"][:new_failed]] if new_failed > 0 else []
    )

    # Merge with prior eviction summary
    total_established = prior_eviction["established"] + new_established
    total_failed = prior_eviction["failed"] + new_failed
    total_oq = prior_eviction["open_questions"] + new_oq
    all_failed_titles = prior_eviction["failed_titles"] + evicted_failed_titles

    eviction_body = _format_eviction_body(total_established, total_failed, total_oq, all_failed_titles)
    return _format_doc(parsed, eviction_body[:EVICTION_MAX])[:TOTAL_MAX]

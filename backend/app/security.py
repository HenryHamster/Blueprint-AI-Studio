import re
import base64
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)
# GitHub imports are deferred to scan_pr_for_injection() so pure functions
# (scan_diff_text, evaluate_findings, etc.) can be used without app.github.

# ── Pattern Definitions ─────────────────────────────────────────────────────
# severity: high = blocks review (requires human), medium = flags but proceeds
#
# Patterns are grouped by attack category. Each pattern is compiled once at
# module load for performance.

_RAW_PATTERNS: List[Dict[str, Any]] = [
    # ── 1. Direct instruction overrides ──────────────────────────────────
    {"pattern": r"ignore\s+(all\s+)?previous\s+(instructions|prompts|rules|context)", "severity": "high", "cat": "instruction_override"},
    {"pattern": r"disregard\s+(all\s+)?(above|previous|prior|earlier|preceding)", "severity": "high", "cat": "instruction_override"},
    {"pattern": r"forget\s+(everything|all|your)\s+(previous\s+)?(instructions|rules|context|training)", "severity": "high", "cat": "instruction_override"},
    {"pattern": r"override\s+(your|the|all|any)\s+(instructions|rules|guidelines|constraints|safeguards)", "severity": "high", "cat": "instruction_override"},
    {"pattern": r"do\s+not\s+follow\s+(your|the|any|previous)\s+(instructions|rules|guidelines)", "severity": "high", "cat": "instruction_override"},
    {"pattern": r"stop\s+being\s+(a|an)\s+(ai|assistant|language\s+model|helpful)", "severity": "high", "cat": "instruction_override"},
    {"pattern": r"(from|starting)\s+now\s+(on\s+)?(you|your)\s+(are|will|must|should)", "severity": "high", "cat": "instruction_override"},
    {"pattern": r"new\s+(system\s+)?(prompt|instructions|rules|persona|role|directive)", "severity": "medium", "cat": "instruction_override"},
    {"pattern": r"(here|these)\s+are\s+your\s+new\s+(instructions|rules|directives)", "severity": "high", "cat": "instruction_override"},
    {"pattern": r"act\s+as\s+(if|though)\s+you\s+(have\s+)?(no|don.t\s+have)\s+(restrictions|rules|guidelines)", "severity": "high", "cat": "instruction_override"},

    # ── 2. Role reassignment / persona injection ─────────────────────────
    {"pattern": r"you\s+are\s+now\s+(a|an|the|my)\s+", "severity": "high", "cat": "role_injection"},
    {"pattern": r"pretend\s+(you\s+are|to\s+be|you.re)\s+", "severity": "high", "cat": "role_injection"},
    {"pattern": r"roleplay\s+as\s+", "severity": "high", "cat": "role_injection"},
    {"pattern": r"switch\s+(to|into)\s+(a\s+)?(.+?)\s+(mode|persona|role|character)", "severity": "high", "cat": "role_injection"},
    {"pattern": r"you.re\s+(actually|really|secretly)\s+(a|an|not)\s+", "severity": "high", "cat": "role_injection"},
    {"pattern": r"enter\s+(developer|admin|god|debug|unrestricted|jailbreak)\s+mode", "severity": "high", "cat": "role_injection"},
    {"pattern": r"enable\s+(developer|admin|god|debug|unrestricted|DAN)\s+mode", "severity": "high", "cat": "role_injection"},

    # ── 3. Prompt/system boundary markers ────────────────────────────────
    {"pattern": r"<\|endoftext\|>", "severity": "high", "cat": "boundary_marker"},
    {"pattern": r"<\|im_start\|>", "severity": "high", "cat": "boundary_marker"},
    {"pattern": r"<\|im_end\|>", "severity": "high", "cat": "boundary_marker"},
    {"pattern": r"<\|system\|>", "severity": "high", "cat": "boundary_marker"},
    {"pattern": r"<\|user\|>", "severity": "high", "cat": "boundary_marker"},
    {"pattern": r"<\|assistant\|>", "severity": "high", "cat": "boundary_marker"},
    {"pattern": r"\[INST\]", "severity": "high", "cat": "boundary_marker"},
    {"pattern": r"\[/INST\]", "severity": "high", "cat": "boundary_marker"},
    {"pattern": r"<<SYS>>", "severity": "high", "cat": "boundary_marker"},
    {"pattern": r"<</SYS>>", "severity": "high", "cat": "boundary_marker"},
    {"pattern": r"<\|eot_id\|>", "severity": "high", "cat": "boundary_marker"},
    {"pattern": r"<\|start_header_id\|>", "severity": "high", "cat": "boundary_marker"},

    # Anthropic-specific XML injection
    {"pattern": r"<system>", "severity": "high", "cat": "boundary_marker"},
    {"pattern": r"</system>", "severity": "high", "cat": "boundary_marker"},
    {"pattern": r"<anthropic>", "severity": "high", "cat": "boundary_marker"},
    {"pattern": r"</?human>", "severity": "high", "cat": "boundary_marker"},
    {"pattern": r"</?assistant>", "severity": "high", "cat": "boundary_marker"},
    {"pattern": r"<antThinking>", "severity": "high", "cat": "boundary_marker"},
    {"pattern": r"</?artifacts?>", "severity": "medium", "cat": "boundary_marker"},

    # Conversation turn markers (in code files — not natural)
    {"pattern": r"^Human:", "severity": "medium", "cat": "turn_marker"},
    {"pattern": r"^Assistant:", "severity": "medium", "cat": "turn_marker"},
    {"pattern": r"^###\s*(System|Instruction|Human|User|Assistant)\s*(Message|Prompt)?:", "severity": "medium", "cat": "turn_marker"},
    {"pattern": r"^(SYSTEM|USER|ASSISTANT)\s*:", "severity": "medium", "cat": "turn_marker"},

    # ── 4. Prompt leaking / exfiltration ─────────────────────────────────
    {"pattern": r"(output|print|show|display|reveal|repeat|echo|give\s+me)\s+.{0,30}(system\s+)?(prompt|instructions|rules|context|directives)", "severity": "medium", "cat": "prompt_leak"},
    {"pattern": r"what\s+(are|is|were)\s+your\s+.{0,20}(system\s+)?(prompt|instructions|rules|directives)", "severity": "medium", "cat": "prompt_leak"},
    {"pattern": r"(send|post|upload|transmit|exfiltrate)\s+.{0,40}(to|at|via)\s+(https?://|ftp://|wss?://)", "severity": "high", "cat": "exfiltration"},
    {"pattern": r"curl\s+.{0,60}(https?://|ftp://)\S+\s+.{0,40}(-d|--data|--upload)", "severity": "high", "cat": "exfiltration"},

    # ── 5. Tool/function manipulation ────────────────────────────────────
    {"pattern": r"call\s+the\s+(tool|function|api)\s+with\s+(these|the\s+following)\s+parameters", "severity": "high", "cat": "tool_manipulation"},
    {"pattern": r"(execute|run|invoke)\s+(the\s+)?(bash|shell|terminal|exec)\s+(tool|command)\s+with", "severity": "high", "cat": "tool_manipulation"},
    {"pattern": r"use\s+tool_use\s+to\s+", "severity": "high", "cat": "tool_manipulation"},

    # ── 6. Unicode / encoding attacks ────────────────────────────────────
    # Bidi overrides can visually hide malicious code
    {"pattern": r"[\u202a-\u202e\u2066-\u2069]", "severity": "high", "cat": "unicode_attack"},
    # Zero-width chars used to smuggle invisible text
    {"pattern": r"[\u200b-\u200f\u2028-\u2029\u2060-\u206f\ufeff]", "severity": "medium", "cat": "unicode_attack"},
    # Homoglyph attacks (Cyrillic/Greek lookalikes in otherwise ASCII code)
    {"pattern": r"[\u0400-\u04ff\u0370-\u03ff]", "severity": "medium", "cat": "homoglyph"},

    # ── 7. Encoded payloads ──────────────────────────────────────────────
    # Long base64 blobs in comments (potential hidden instructions)
    {"pattern": r"(?:#|//|/\*|\*|--)\s*[A-Za-z0-9+/=]{100,}", "severity": "medium", "cat": "encoded_payload"},
    # Hex-encoded strings in comments
    {"pattern": r"(?:#|//|/\*|\*|--)\s*(?:\\x[0-9a-fA-F]{2}){20,}", "severity": "medium", "cat": "encoded_payload"},

    # ── 8. HTML/Markdown injection in code files ─────────────────────────
    {"pattern": r"<!--[\s\S]*?(?:ignore|disregard|override|instruction|system|prompt|inject)[\s\S]*?-->", "severity": "medium", "cat": "html_injection"},
    # Hidden text via HTML (zero-size, white text, etc.)
    {"pattern": r"<(?:span|div|p)\s+style\s*=\s*[\"'][^\"']*(?:display\s*:\s*none|font-size\s*:\s*0|color\s*:\s*(?:white|#fff))", "severity": "high", "cat": "html_injection"},

    # ── 9. Multi-language injection ──────────────────────────────────────
    {"pattern": r"ignor(?:ar|ez|ieren)\s+(?:todas?\s+las?\s+)?(?:instrucciones|instructions|Anweisungen)", "severity": "high", "cat": "multilang_injection"},
    {"pattern": r"(?:oubliez|vergessen\s+Sie)\s+(?:toutes?\s+les?\s+|alle\s+)?(?:instructions|r[eè]gles|Anweisungen)", "severity": "high", "cat": "multilang_injection"},

]

# Compile patterns once at module load
INJECTION_PATTERNS: List[Dict[str, Any]] = []
for p in _RAW_PATTERNS:
    try:
        INJECTION_PATTERNS.append({
            "compiled": re.compile(p["pattern"], re.IGNORECASE),
            "severity": p["severity"],
            "category": p["cat"],
            "description": p["pattern"][:80],
        })
    except re.error:
        pass  # Skip malformed patterns

# File extensions where natural language is expected (higher false-positive threshold)
DOCS_EXTENSIONS = {".md", ".txt", ".rst", ".adoc", ".html", ".htm"}
# File extensions to never scan (binary)
BINARY_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2",
                     ".ttf", ".eot", ".pdf", ".zip", ".tar", ".gz", ".bin", ".exe",
                     ".dll", ".so", ".dylib", ".pyc", ".class"}
# Auto-generated lock files — huge, full of hashes, never contain injections
SKIP_FILENAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                  "Cargo.lock", "poetry.lock", "composer.lock"}
# Test files that contain injection patterns by definition (they test the scanner)
SKIP_PATHS_CONTAINING = {"test_security", "testing/test_security"}

# ── Fuzzy matching for evasion detection ─────────────────────────────────────
# Key phrases that attackers try to obfuscate via leet-speak, spacing, misspellings
FUZZY_PHRASES = [
    ("ignore previous instructions", "high", "instruction_override"),
    ("ignore all instructions", "high", "instruction_override"),
    ("disregard previous instructions", "high", "instruction_override"),
    ("override system prompt", "high", "instruction_override"),
    ("forget your instructions", "high", "instruction_override"),
    ("you are now a", "high", "role_injection"),
    ("enter developer mode", "high", "role_injection"),
    ("system prompt", "medium", "prompt_leak"),
]

# Leet-speak normalization map
_LEET_MAP = str.maketrans("01345$@!", "oieassal")


def _normalize_for_fuzzy(text: str) -> str:
    """Normalize text for fuzzy matching: lowercase, leet→ascii, strip separators."""
    t = text.lower()
    t = t.translate(_LEET_MAP)
    # Collapse separators between characters: "i.g.n.o.r.e" → "ignore"
    t = re.sub(r'(?<=\w)[._\-\s]+(?=\w)', '', t)
    # Remove null bytes, backslashes used for evasion
    t = t.replace('\\x00', '').replace('\\0', '').replace('\x00', '')
    return t


def _levenshtein(s1: str, s2: str) -> int:
    """Levenshtein distance — O(n*m) but strings are short (<50 chars)."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[-1]


def _fuzzy_scan(text: str, source: str) -> List[Dict[str, str]]:
    """
    Scan text for fuzzy matches against known injection phrases.
    Uses normalization + Levenshtein distance to catch leet-speak,
    spacing tricks, and misspellings.
    """
    findings = []
    normalized = _normalize_for_fuzzy(text)

    for phrase, severity, category in FUZZY_PHRASES:
        norm_phrase = _normalize_for_fuzzy(phrase)
        phrase_len = len(norm_phrase)
        # Max allowed distance scales with phrase length (roughly 20% tolerance)
        max_dist = max(2, phrase_len // 5)

        # Sliding window over normalized text
        for i in range(len(normalized) - phrase_len + max_dist + 1):
            window = normalized[i:i + phrase_len + max_dist]
            if not window:
                continue
            dist = _levenshtein(window[:phrase_len], norm_phrase)
            if dist <= max_dist:
                findings.append({
                    "source": source,
                    "source_type": "code",
                    "category": f"evasion:{category}",
                    "severity": severity,
                    "matched_text": text[i:i + phrase_len + max_dist].strip()[:200],
                    "context": text.strip()[:300],
                })
                break  # One match per phrase per line is enough

    return findings


def _get_extension(path: str) -> str:
    """Extract lowercase file extension."""
    if "." in path:
        return "." + path.rsplit(".", 1)[-1].lower()
    return ""


def _scan_text(text: str, source: str, source_type: str = "code") -> List[Dict[str, str]]:
    """
    Scan a text string against all injection patterns.
    source: identifier for where the text came from (file path, "commit_message", etc.)
    source_type: "code" (strict), "docs" (only high-severity), "metadata" (commit msg, PR body)
    """
    findings: List[Dict[str, str]] = []
    ext = _get_extension(source)

    if ext in BINARY_EXTENSIONS:
        return findings

    for pattern_def in INJECTION_PATTERNS:
        match = pattern_def["compiled"].search(text)
        if match:
            # In docs files, only flag high-severity patterns
            if source_type == "docs" and pattern_def["severity"] != "high":
                continue

            findings.append({
                "source": source,
                "source_type": source_type,
                "category": pattern_def["category"],
                "severity": pattern_def["severity"],
                "matched_text": match.group()[:200],
                "context": text.strip()[:300],
            })
    return findings


def _check_base64_hidden_text(text: str, source: str) -> List[Dict[str, str]]:
    """
    Check if any base64-encoded strings in comments decode to suspicious text.
    Goes beyond pattern matching — actually decodes and scans the content.
    """
    findings = []
    # Find base64 blobs in comments (at least 40 chars to avoid false positives)
    b64_pattern = re.compile(r'(?:#|//|/\*|\*|--)\s*([A-Za-z0-9+/]{40,}={0,2})')
    for match in b64_pattern.finditer(text):
        try:
            decoded = base64.b64decode(match.group(1)).decode("utf-8", errors="ignore")
            # Scan the decoded content for injection patterns
            decoded_findings = _scan_text(decoded, f"{source} (decoded base64)", "code")
            for f in decoded_findings:
                f["severity"] = "high"  # Encoded injection is always high
                f["category"] = "encoded_injection"
                findings.append(f)
        except Exception:  # noqa: S110 — malformed base64 is expected when scanning arbitrary diff hunks
            pass
    return findings


def scan_diff_text(diff_text: str) -> List[Dict[str, str]]:
    """
    Scan a unified diff for prompt injection patterns.
    Scans: added lines, file paths.
    """
    findings: List[Dict[str, str]] = []
    current_file = ""

    for line in diff_text.split("\n"):
        # Track which file we're in
        if line.startswith("diff --git"):
            parts = line.split(" b/")
            current_file = parts[-1] if len(parts) > 1 else ""
            # Scan the file PATH itself for injection
            if current_file:
                path_findings = _scan_text(current_file, f"filename:{current_file}", "metadata")
                findings.extend(path_findings)

        elif line.startswith("+++"):
            current_file = line[6:] if line.startswith("+++ b/") else current_file

        elif (line.startswith("+") and not line.startswith("+++")) or \
             (line.startswith("-") and not line.startswith("---")):
            # Scan both added AND deleted lines — deletions of security patterns are suspicious
            is_deletion = line.startswith("-")
            # Skip auto-generated lock files and security test files
            basename = current_file.rsplit("/", 1)[-1] if "/" in current_file else current_file
            if basename in SKIP_FILENAMES:
                continue
            if any(skip in current_file for skip in SKIP_PATHS_CONTAINING):
                continue
            # Scan line content
            line_content = line[1:]
            ext = _get_extension(current_file)
            source_type = "docs" if ext in DOCS_EXTENSIONS else "code"
            line_findings = _scan_text(line_content, current_file, source_type)
            if is_deletion:
                # Tag deletion findings so they can be distinguished
                for f in line_findings:
                    f["source"] = f"deleted:{f['source']}"
            findings.extend(line_findings)

            # Fuzzy matching for evasion (leet-speak, misspellings, separator tricks)
            if ext not in BINARY_EXTENSIONS:
                fuzzy_findings = _fuzzy_scan(line_content, current_file)
                if is_deletion:
                    for f in fuzzy_findings:
                        f["source"] = f"deleted:{f['source']}"
                findings.extend(fuzzy_findings)

            # Also check for hidden base64 payloads
            b64_findings = _check_base64_hidden_text(line_content, current_file)
            if is_deletion:
                for f in b64_findings:
                    f["source"] = f"deleted:{f['source']}"
            findings.extend(b64_findings)

    return findings


def scan_commit_messages(commits: list) -> List[Dict[str, str]]:
    """Scan commit messages for injection patterns."""
    findings: List[Dict[str, str]] = []
    for commit in commits:
        msg = commit.get("commit", {}).get("message", "")
        sha = commit.get("sha", "unknown")[:8]
        if msg:
            msg_findings = _scan_text(msg, f"commit:{sha}", "metadata")
            findings.extend(msg_findings)
    return findings


def scan_pr_metadata(pr_data: dict) -> List[Dict[str, str]]:
    """Scan PR title and body for injection patterns."""
    findings: List[Dict[str, str]] = []

    title = pr_data.get("title", "")
    if title:
        findings.extend(_scan_text(title, "pr:title", "metadata"))

    body = pr_data.get("body", "") or ""
    if body:
        findings.extend(_scan_text(body, "pr:body", "metadata"))

    return findings


def _deduplicate_findings(findings: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Remove duplicate findings (same source + category)."""
    seen = set()
    deduped = []
    for f in findings:
        key = (f.get("source", ""), f.get("category", ""), f.get("severity", ""))
        if key not in seen:
            seen.add(key)
            deduped.append(f)
    return deduped


def evaluate_findings(findings: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Evaluate findings and decide whether to block the pipeline.

    Block rule:
      - 5+ high-severity findings (isolated matches are common in normal code)
      - 20+ medium-severity findings (sustained signal suggests coordinated evasion)
      - Any encoded_injection (base64-hidden payload is never accidental)
    """
    high = [f for f in findings if f["severity"] == "high"]
    medium = [f for f in findings if f["severity"] == "medium"]
    high_categories = sorted({f["category"] for f in high})

    has_encoded_injection = any(f["category"] == "encoded_injection" for f in findings)
    medium_escalation = len(medium) >= 20  # sustained medium signals suggest coordinated evasion
    blocked = has_encoded_injection or len(high) >= 5 or medium_escalation

    if blocked:
        if has_encoded_injection:
            reason = "encoded injection detected (base64-hidden payload)"
        elif medium_escalation:
            reason = f"{len(medium)} medium-severity findings — possible evasion of high-severity patterns"
        else:
            reason = f"{len(high)} high-severity findings across {', '.join(high_categories)}"
    elif high:
        reason = f"{len(high)} high finding(s) — below blocking threshold"
    elif findings:
        reason = f"{len(medium)} medium finding(s) — informational only"
    else:
        reason = "clean"

    verdict = "BLOCKED" if blocked else "PASSED"

    if findings:
        summary = f"{len(findings)} finding(s) ({len(high)} high, {len(medium)} medium) — {verdict}"
    else:
        summary = "clean — no findings"

    return {
        "blocked": blocked,
        "high_count": len(high),
        "medium_count": len(medium),
        "categories": high_categories,
        "reason": reason,
        "summary": summary,
        "verdict": verdict,
    }


def format_findings_report(findings: List[Dict[str, str]], evaluation: Dict[str, Any], context: str = "") -> str:
    """
    Format findings into a markdown report for PR comments and logs.
    Always shows every finding with its location, even when not blocking.
    """
    ctx = f" ({context})" if context else ""

    if not findings:
        return f"**Blueprint Security Scan**{ctx} — No issues found."

    lines = []
    if evaluation["blocked"]:
        lines.append(f"**Blueprint Security Scan**{ctx} — **BLOCKED**: {evaluation['reason']}")
        lines.append("")
        lines.append("> This PR requires manual approval before the pipeline can proceed.")
    else:
        lines.append(f"**Blueprint Security Scan**{ctx} — Passed with {len(findings)} finding(s)")
        lines.append("")
        lines.append("> Below blocking threshold. Isolated matches are common in normal code.")

    lines.append("")
    lines.append("| # | Severity | Category | Source | Matched |")
    lines.append("|---|----------|----------|--------|---------|")

    for i, f in enumerate(findings, 1):
        sev = f.get("severity", "?").upper()
        cat = f.get("category", "?")
        src = f.get("source", "?")
        matched = f.get("matched_text", "")[:80].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {i} | {sev} | {cat} | `{src}` | `{matched}` |")

    lines.append("")
    lines.append("*Blocking threshold: 5+ high-severity, 20+ medium-severity, or any encoded injection.*")

    return "\n".join(lines)


async def scan_pr_for_injection(gh_token: str, owner: str, repo: str, pr_number: int) -> Dict[str, Any]:
    """
    Comprehensive PR security scan. Fetches and scans:
      1. PR diff (added lines + file names)
      2. Commit messages
      3. PR title and body

    Returns {"clean": bool, "blocked": bool, "findings": [...], "report": str, "summary": str, "scanned": [...]}
    """
    from app.github import get_pull_request_diff, get_pull_request, get_pull_request_commits

    all_findings: List[Dict[str, str]] = []
    scanned: List[str] = []

    # 1. Scan diff (added lines + file paths)
    try:
        diff_text = await get_pull_request_diff(gh_token, owner, repo, pr_number)
        if diff_text:
            all_findings.extend(scan_diff_text(diff_text))
            scanned.append("diff")
    except Exception as e:
        logger.warning(f"[SECURITY] Failed to fetch diff for PR #{pr_number}: {e}")

    # 2. Scan commit messages
    try:
        commits = await get_pull_request_commits(gh_token, owner, repo, pr_number)
        if commits:
            all_findings.extend(scan_commit_messages(commits))
            scanned.append("commits")
    except Exception as e:
        logger.warning(f"[SECURITY] Failed to fetch commits for PR #{pr_number}: {e}")

    # 3. Scan PR title and body
    try:
        pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
        if pr_data:
            all_findings.extend(scan_pr_metadata(pr_data))
            scanned.append("pr_metadata")
    except Exception as e:
        logger.warning(f"[SECURITY] Failed to fetch PR data for PR #{pr_number}: {e}")

    # If we couldn't scan anything, don't block but flag it
    if not scanned:
        return {
            "clean": True,
            "blocked": False,
            "findings": [],
            "report": "**Blueprint Security Scan** — Could not fetch any PR data for scanning.",
            "summary": f"PR #{pr_number} — scan skipped (no data fetched)",
            "scanned": [],
            "error": "Could not fetch any PR data for scanning",
        }

    all_findings = _deduplicate_findings(all_findings)
    evaluation = evaluate_findings(all_findings)
    context = f"PR #{pr_number}"
    report = format_findings_report(all_findings, evaluation, context)

    return {
        "clean": len(all_findings) == 0,
        "blocked": evaluation["blocked"],
        "findings": all_findings,
        "report": report,
        "summary": f"PR #{pr_number} — {evaluation['summary']}",
        "scanned": scanned,
    }

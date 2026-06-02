"""Validation of research finding source file references at ingestion time.

Validates that file refs in source objects actually exist and that line
numbers are within bounds. Refs that fail validation are marked with
valid: false or dropped, with warnings logged. The system degrades gracefully
on validation failures — does not crash.
"""

import asyncio
import logging
import os
import re
from urllib.parse import quote
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_REPO_ROOT = "/tmp/workspace"
_DESIGN_DOC_PATH_RE = re.compile(
    r"\b((?:backend|agent|admin-ui)/(?:[\w.-]+/)*[\w.-]+\.\w+)\b"
)
_PRESENT_SYSTEM_STATUSES = {"mature", "functional", "partial"}
_PRESENT_CAPABILITY_STATUSES = {"working", "partial"}


def _parse_ref(ref: str) -> Tuple[str, Optional[int], Optional[int]]:
    """Parse a file reference string into (path, start_line, end_line).

    Formats:
    - path/to/file.py → (path, None, None)
    - path/to/file.py:42 → (path, 42, None)
    - path/to/file.py#L10-L20 → (path, 10, 20)
    - path/to/file.py:42-50 → (path, 42, 50)

    Returns (path, start_line, end_line) where lines are 1-indexed.
    Malformed refs return (ref, None, None) so we catch them during validation.
    """
    if not isinstance(ref, str) or not ref:
        return ref, None, None

    # Try #L{start}-L{end} format (GitHub style)
    match = re.match(r"^(.+?)#L(\d+)(?:-L(\d+))?$", ref)
    if match:
        path = match.group(1)
        start = int(match.group(2))
        end = int(match.group(3)) if match.group(3) else None
        return path, start, end

    # Try :{start}-{end} or :{start} format
    match = re.match(r"^(.+?):(\d+)(?:-(\d+))?$", ref)
    if match:
        path = match.group(1)
        start = int(match.group(2))
        end = int(match.group(3)) if match.group(3) else None
        return path, start, end

    # Plain path
    return ref, None, None


def _looks_like_file_path(path: str) -> bool:
    """Cheap rejector for obvious non-file refs before expensive checks."""
    return bool(
        path
        and isinstance(path, str)
        and (
            "/" in path
            or path.endswith((".py", ".ts", ".tsx", ".js", ".go", ".rs"))
        )
    )


async def _file_exists_at_head(path: str, *, cwd: str = "/tmp/workspace") -> bool:
    """Check if a file exists in the HEAD commit using 'git ls-files'.

    Returns False if git command fails, file doesn't exist, or path is invalid.
    Does not raise exceptions — logs and returns False on errors.
    """
    if not _looks_like_file_path(path):
        # Reject obvious non-file paths to avoid wasting git calls
        return False
    exists = await _path_exists_at_head(path, repo_root=cwd)
    return exists is True


async def _path_exists_at_head(path: str, repo_root: str = _REPO_ROOT) -> Optional[bool]:
    """Return whether *path* exists in HEAD, or None when indeterminate.

    False means the path is definitely absent in HEAD. None means git/root
    validation failed, so callers should degrade conservatively and leave the
    original value untouched.
    """
    if not _looks_like_file_path(path):
        # Not a recognizable file path — indeterminate, leave callers to degrade.
        return None
    if not repo_root or not os.path.isdir(repo_root):
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "ls-files",
            "--",
            path,
            cwd=repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning(
                f"[SOURCE_REF_VALIDATION] git ls-files timeout for path {path}"
            )
            return None
        if proc.returncode != 0:
            return None
        # git ls-files returns 0 and outputs the path if it exists, empty otherwise
        return path in stdout.decode(errors="replace").splitlines()
    except Exception as e:
        logger.warning(
            f"[SOURCE_REF_VALIDATION] Failed to check if file exists: {path}: {e}"
        )
        return None


async def _file_exists_in_repo_branch(
    path: str,
    *,
    token: str,
    owner: str,
    repo: str,
    branch: str,
    project_id: str | None = None,
) -> bool:
    """Check whether *path* exists on ``owner/repo@branch`` via GitHub API."""
    if not _looks_like_file_path(path):
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    encoded_path = quote(path, safe="/")
    encoded_branch = quote(branch, safe="")
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/contents/"
        f"{encoded_path}?ref={encoded_branch}"
    )
    try:
        from app.github import _github_request

        response = await _github_request(
            "GET",
            url,
            headers,
            max_retries=2,
            project_id=project_id,
        )
        if response.status_code == 404:
            return False
        if response.status_code != 200:
            logger.warning(
                "[SOURCE_REF_VALIDATION] GitHub contents lookup failed for %s on "
                "%s/%s@%s: HTTP %s",
                path,
                owner,
                repo,
                branch,
                response.status_code,
            )
            return False
        payload = response.json()
        return isinstance(payload, dict) and payload.get("type") == "file"
    except Exception as e:
        logger.warning(
            "[SOURCE_REF_VALIDATION] Failed GitHub contents lookup for %s on "
            "%s/%s@%s: %s",
            path,
            owner,
            repo,
            branch,
            e,
        )
        return False


async def _file_has_line_count(path: str, min_line: int) -> bool:
    """Check if a file has at least min_line lines.

    Uses 'git show HEAD:path' and counts newlines to validate without checkout.
    Returns False if the file doesn't exist, has fewer lines, or git fails.
    Does not raise exceptions — logs and returns False on errors.
    """
    if not path or min_line < 1:
        return False

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "show",
            f"HEAD:{path}",
            cwd="/tmp/workspace",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning(
                f"[SOURCE_REF_VALIDATION] Failed to validate line count for {path}"
            )
            return False

        if proc.returncode != 0:
            logger.warning(
                f"[SOURCE_REF_VALIDATION] Failed to count lines in {path}: {stderr.decode().strip()}"
            )
            return False

        line_count = stdout.count(b"\n")
        return line_count >= min_line
    except Exception as e:
        logger.warning(
            f"[SOURCE_REF_VALIDATION] Error validating line count for {path}: {e}"
        )
        return False


def partition_missing(
    paths: list, exists_fn: Callable[[str], bool]
) -> Tuple[List[str], List[str]]:
    """Split *paths* into (present, missing) using the synchronous *exists_fn*.

    Pure partitioner shared by the file-existence checkers. *exists_fn* is a
    ``path -> bool`` predicate. Non-string entries are treated as missing.
    Order is preserved within each bucket. Used by the playbook stale-ref
    path (``os.path.isfile``-backed) so it shares partition logic with the
    async git-backed classifier below.
    """
    present: List[str] = []
    missing: List[str] = []
    for p in paths:
        if isinstance(p, str) and exists_fn(p):
            present.append(p)
        else:
            missing.append(p)
    return present, missing


async def classify_existing_paths(
    paths: list,
    *,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    project_id: str | None = None,
    cwd: str = "/tmp/workspace",
) -> Tuple[List[str], List[str]]:
    """Classify *paths* into (present, missing) against the integration-branch
    file tree.

    When ``token/owner/repo/branch`` are provided, checks the project's real
    GitHub repository tree at that branch. Otherwise falls back to the local
    checkout's git HEAD (the same mechanism :func:`validate_source_refs`
    relies on). Read-only and advisory: callers use the result to annotate,
    never to mutate stored state. Non-string entries are treated as missing.
    Order is preserved within each bucket.
    """
    present: List[str] = []
    missing: List[str] = []
    use_repo_branch = all([token, owner, repo, branch])
    for p in paths:
        exists = False
        if isinstance(p, str):
            if use_repo_branch:
                exists = await _file_exists_in_repo_branch(
                    p,
                    token=str(token),
                    owner=str(owner),
                    repo=str(repo),
                    branch=str(branch),
                    project_id=project_id,
                )
            else:
                exists = (
                    await _file_exists_at_head(p)
                    if cwd == "/tmp/workspace"
                    else await _file_exists_at_head(p, cwd=cwd)
                )
        if exists:
            present.append(p)
        else:
            missing.append(p)
    return present, missing


# Recognizes the `path/to/file.ext:symbol` convention used in finding claims
# (e.g. "backend/app/x.py:my_func stores Y"). The symbol must start with a
# letter or underscore, which excludes line-number refs like file.py:42.
_PATH_SYMBOL_RE = re.compile(
    r"(?<![\w/])([\w./-]+\.(?:py|ts|tsx|js|jsx|mjs|cjs|go|rs)):([A-Za-z_]\w*)"
)

# Definition-introducing keywords across Python, TS/JS, Go, and Rust. A symbol
# is considered grounded when any of these precedes it, OR when it appears on
# the left of an assignment/object-key/definition. Conservative by design: we
# only flag a symbol as absent when NO plausible definition is found.
_DEF_KEYWORDS = (
    "def",
    "class",
    "function",
    "func",
    "fn",
    "const",
    "let",
    "var",
    "type",
    "interface",
    "enum",
    "struct",
    "trait",
    "mod",
    "static",
)


def _parse_path_symbol_refs(text: Any) -> list:
    """Extract deduplicated (path, symbol) pairs from `path:symbol` refs in text.

    Returns [] for non-strings or text with no recognizable references.
    """
    if not isinstance(text, str) or not text:
        return []
    out: list = []
    seen: set = set()
    for m in _PATH_SYMBOL_RE.finditer(text):
        key = (m.group(1), m.group(2))
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _symbol_definition_regex(symbol: str) -> "re.Pattern":
    """Build a regex matching a plausible definition of ``symbol`` in source.

    Matches a definition keyword followed by the symbol, or the symbol on the
    left of an assignment / object-key / function-or-method definition. The
    leading ``[^\\w.]`` guard avoids treating ``obj.symbol`` as a definition.
    """
    s = re.escape(symbol)
    kw = r"(?:^|[^\w.])(?:" + "|".join(_DEF_KEYWORDS) + r")\s+" + s + r"\b"
    lhs = r"(?:^|[^\w.])" + s + r"\s*(?:[:=]|\()"
    return re.compile(kw + "|" + lhs, re.MULTILINE)


async def _symbol_defined_at_head(path: str, symbol: str) -> Optional[bool]:
    """Return whether ``symbol`` is defined in ``path`` at HEAD.

    True  → file exists at HEAD and a plausible definition of ``symbol`` found.
    False → unambiguously absent (file exists but no definition, or the file
            itself is absent at HEAD).
    None  → indeterminate (git unavailable / timeout / non-repo / error). Callers
            treat None as grounded so a git outage never flags refs.
    """
    if not path or not symbol or not isinstance(path, str) or not isinstance(symbol, str):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "show",
            f"HEAD:{path}",
            cwd="/tmp/workspace",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning(f"[SOURCE_REF_VALIDATION] git show timeout for {path}")
            return None
    except Exception as e:
        logger.warning(f"[SOURCE_REF_VALIDATION] git show failed for {path}: {e}")
        return None
    if proc.returncode == 0:
        return bool(_symbol_definition_regex(symbol).search(stdout.decode(errors="replace")))
    err = stderr.decode(errors="replace").lower()
    if "does not exist" in err or "exists on disk, but not in" in err:
        # git ran fine; the path is genuinely absent at HEAD → unambiguously absent.
        return False
    # not-a-repo or any other git error → indeterminate, degrade gracefully.
    return None


def _extract_design_doc_paths(entry: Any) -> list[str]:
    """Collect unique repo-relative file paths referenced by a design-doc entry."""
    if not isinstance(entry, dict):
        return []
    seen: set[str] = set()
    paths: list[str] = []

    modules = entry.get("modules")
    if isinstance(modules, list):
        for module in modules:
            if isinstance(module, str):
                path = module.strip()
                if path and path not in seen:
                    seen.add(path)
                    paths.append(path)

    for field in ("notes", "description"):
        text = entry.get(field)
        if not isinstance(text, str) or not text:
            continue
        for match in _DESIGN_DOC_PATH_RE.finditer(text):
            path = match.group(1)
            if path not in seen:
                seen.add(path)
                paths.append(path)

    return paths


async def reconcile_design_doc_presence(
    value: Any,
    key: str,
    repo_root: str = _REPO_ROOT,
) -> Any:
    """Downgrade present-status design-doc entries whose backing files are gone.

    Conservative no-op when the entry is not a system/capability record, the
    repo root is invalid, any referenced path is indeterminate, or at least one
    referenced path still exists. Only entries whose every referenced path is
    confirmed absent are downgraded to ``missing`` and annotated.
    """
    if not isinstance(value, dict):
        return value
    if not isinstance(key, str):
        return value
    if not repo_root or not os.path.isdir(repo_root):
        return value

    if key.startswith("systems/"):
        present_statuses = _PRESENT_SYSTEM_STATUSES
        text_field = "description"
    elif key.startswith("capabilities/"):
        present_statuses = _PRESENT_CAPABILITY_STATUSES
        text_field = "notes"
    else:
        return value

    status = str(value.get("status") or "").strip().lower()
    if status not in present_statuses:
        return value

    paths = _extract_design_doc_paths(value)
    if not paths:
        return value

    seen_present = False
    for path in paths:
        exists = await _path_exists_at_head(path, repo_root=repo_root)
        if exists is None:
            return value
        if exists:
            seen_present = True
    if seen_present:
        return value

    warning = (
        f"claimed {status} but all referenced module file(s) are absent on the "
        f"integration branch — treat as missing"
    )
    downgraded: dict[str, Any] = {**value, "status": "missing", "presence_warning": warning}
    raw_text = value.get(text_field)
    existing = raw_text.strip() if isinstance(raw_text, str) else ""
    downgraded[text_field] = (
        f"{existing} " if existing else ""
    ) + f"[reconciled: {warning}]"
    return downgraded


async def detect_absent_symbols(
    module_pointers: Optional[list] = None,
    text_fields: Optional[list] = None,
) -> List[str]:
    """Return ``path:symbol`` refs whose symbol is absent from a present file.

    Symbol-level companion to :func:`reconcile_design_doc_presence`'s
    file-level check. A design-doc system/capability entry can cite a specific
    function/class via a ``path:symbol`` pointer in its ``modules`` list or in
    its notes/description free text. The file can still exist while the symbol
    that implemented the behavior was deleted or moved — so the file-existence
    check passes yet the cited behavior is not actually built. This collects the
    ``path:symbol`` references out of *module_pointers* and *text_fields* and
    checks each against HEAD via :func:`_symbol_defined_at_head`.

    A ref is returned ONLY when that check is unambiguously ``False`` — the file
    exists at HEAD but no plausible definition of the symbol is found (or the
    file itself is absent). An indeterminate result (git unavailable / non-repo)
    and a present symbol are never returned, preserving the conservative
    semantics shared with the file-level path: a symbol is flagged only on
    unambiguous absence, never on a partial or indeterminate hit. Deduplicated,
    order-preserving.
    """
    pairs: List[Tuple[str, str]] = []
    seen: set = set()
    for text in list(module_pointers or []) + list(text_fields or []):
        for pair in _parse_path_symbol_refs(text):
            if pair not in seen:
                seen.add(pair)
                pairs.append(pair)
    absent: List[str] = []
    for path, symbol in pairs:
        if await _symbol_defined_at_head(path, symbol) is False:
            absent.append(f"{path}:{symbol}")
    return absent


async def summarize_ungrounded_claims(
    claims: Optional[list],
    sources: Optional[list] = None,
) -> Optional[Dict[str, Any]]:
    """Verify ``path:symbol`` references in a finding's claims resolve at HEAD.

    Parses ``path:symbol`` references out of each claim (and, when provided,
    file source excerpts using the same convention) and checks each one names a
    symbol actually defined in an existing file at HEAD. A claim is *ungrounded*
    when one of its references is unambiguously absent. Returns a structured
    grounding-warning summary mirroring :func:`summarize_broken_refs`::

        {"ungrounded_claim_count": N, "ungrounded_refs": ["path:symbol", ...]}

    ``ungrounded_claim_count`` is the number of distinct claims carrying an
    absent reference — callers use it to discount ungrounded claims from the
    actionability grade, mirroring how an invalid file citation already fails to
    count as a valid source. Returns None when no claim carries a parseable
    reference or all references are grounded. Degrades gracefully: an
    indeterminate ref (git unavailable) is never flagged.
    """
    if not isinstance(claims, list):
        claims = []
    ungrounded_refs: list = []
    seen_refs: set = set()
    ungrounded_claim_count = 0

    async def _check(text: Any, *, count_claim: bool) -> None:
        nonlocal ungrounded_claim_count
        flagged = False
        for path, symbol in _parse_path_symbol_refs(text):
            if await _symbol_defined_at_head(path, symbol) is False:
                flagged = True
                ref = f"{path}:{symbol}"
                if ref not in seen_refs:
                    seen_refs.add(ref)
                    ungrounded_refs.append(ref)
        if flagged and count_claim:
            ungrounded_claim_count += 1

    for claim in claims:
        await _check(claim, count_claim=True)
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        if str(source.get("type") or "").lower() != "file":
            continue
        await _check(source.get("quote") or source.get("excerpt"), count_claim=False)

    if not ungrounded_refs:
        return None
    return {
        "ungrounded_claim_count": ungrounded_claim_count,
        "ungrounded_refs": ungrounded_refs,
    }


def extract_source_file_paths(sources: Optional[list]) -> list:
    """Extract normalized file paths from all file-type sources.

    Returns a deduplicated list of file paths (line numbers stripped) from
    file-type sources. Used by the reverse-index machinery to determine which
    source files a finding cites — e.g. to detect overlap with a PR's changed
    files during revalidation.
    """
    if not isinstance(sources, list):
        return []
    seen: set = set()
    paths = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get("type", "").lower() != "file":
            continue
        ref = source.get("ref")
        if not ref:
            continue
        path, _, _ = _parse_ref(ref)
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def summarize_broken_refs(
    validated_sources: Optional[list],
) -> Optional[Dict[str, Any]]:
    """Summarize file-type sources marked invalid by validate_source_refs.

    Walks the output of validate_source_refs and returns a structured
    provenance summary for findings whose citations include broken file refs:

        {"broken_source_ref_count": N, "broken_refs": [path, ...]}

    Paths are deduplicated using _parse_ref so different line numbers on the
    same file collapse to one entry. Returns None when there are no broken
    file refs (count would be 0). Non-file invalid sources are ignored.
    """
    if not isinstance(validated_sources, list):
        return None
    seen: set = set()
    broken_paths: list = []
    for source in validated_sources:
        if not isinstance(source, dict):
            continue
        if source.get("valid") is not False:
            continue
        if source.get("type", "").lower() != "file":
            continue
        ref = source.get("ref")
        if not ref:
            continue
        path, _, _ = _parse_ref(ref)
        if not path or path in seen:
            continue
        seen.add(path)
        broken_paths.append(path)
    if not broken_paths:
        return None
    return {
        "broken_source_ref_count": len(broken_paths),
        "broken_refs": broken_paths,
    }


def collect_stale_refs(validated_sources: list) -> list:
    """Return refs marked invalid by validate_source_refs.

    Given the output of validate_source_refs, returns the ref strings for
    sources marked valid=False. Callers use this to surface affected findings
    for re-evaluation when a file no longer exists at HEAD.
    """
    return [
        s["ref"]
        for s in validated_sources
        if isinstance(s, dict) and s.get("valid") is False and s.get("ref")
    ]


async def validate_source_refs(sources: Optional[list]) -> list:
    """Validate all file-type source refs in a sources list.

    For each source with type='file':
    - Validates that the file exists at HEAD
    - Validates that line numbers (if present) are within bounds
    - Marks invalid sources with valid: false or drops them

    For sources with type='pr' or type='message': no validation needed.

    Args:
        sources: List of source dicts with 'type' and 'ref' fields.
                Can be None.

    Returns:
        List of validated sources, with invalid file refs marked valid: false
        or dropped (depending on validation error severity).
    """
    if not isinstance(sources, list):
        return []

    validated: list[Dict[str, Any]] = []

    for source in sources:
        if not isinstance(source, dict):
            logger.warning("[SOURCE_REF_VALIDATION] Dropping non-dict source entry")
            continue

        source_type = source.get("type", "").lower()

        # PR and message sources don't need file validation
        if source_type in ("pr", "message"):
            validated.append(source)
            continue

        # File sources require validation
        if source_type == "file":
            ref = source.get("ref")
            if not ref:
                logger.warning(
                    "[SOURCE_REF_VALIDATION] File source missing 'ref' field"
                )
                continue

            path, start_line, end_line = _parse_ref(ref)

            # Validate path exists
            if not await _file_exists_at_head(path):
                logger.warning(
                    f"[SOURCE_REF_VALIDATION] File not found in HEAD: {path} "
                    f"(from ref {ref})"
                )
                # Mark as invalid rather than dropping, so downstream can decide
                # whether to use it or log it as stale
                marked = dict(source)
                marked["valid"] = False
                validated.append(marked)
                continue

            # Validate line numbers if present
            if start_line is not None:
                if not await _file_has_line_count(path, start_line):
                    logger.warning(
                        f"[SOURCE_REF_VALIDATION] Line number out of range: {path}:{start_line} "
                        f"(from ref {ref})"
                    )
                    marked = dict(source)
                    marked["valid"] = False
                    validated.append(marked)
                    continue

                # Also validate end_line if present
                if end_line is not None and end_line < start_line:
                    logger.warning(
                        f"[SOURCE_REF_VALIDATION] Invalid line range: {path}:{start_line}-{end_line} "
                        f"(end < start in ref {ref})"
                    )
                    marked = dict(source)
                    marked["valid"] = False
                    validated.append(marked)
                    continue

                if end_line is not None and not await _file_has_line_count(
                    path, end_line
                ):
                    logger.warning(
                        f"[SOURCE_REF_VALIDATION] End line out of range: {path}:{start_line}-{end_line} "
                        f"(from ref {ref})"
                    )
                    marked = dict(source)
                    marked["valid"] = False
                    validated.append(marked)
                    continue

            # Validation passed
            validated.append(source)
        else:
            # Unknown source type — pass through
            validated.append(source)

    return validated

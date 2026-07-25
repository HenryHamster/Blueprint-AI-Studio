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
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

_REPO_ROOT = "/tmp/workspace"
_DESIGN_DOC_PATH_RE = re.compile(
    r"\b((?:backend|agent|admin-ui)/(?:[\w.-]+/)*[\w.-]+\.\w+)\b"
)
# Matches a non-numeric symbol suffix on a module pointer, e.g. "models.py:Alert".
# Line-number refs like "file.py:42" are excluded because digits don't match [A-Za-z_].
_MODULE_PTR_SYMBOL_RE = re.compile(r":[A-Za-z_]\w*$")


def _normalize_module_ptr(ref: str) -> str:
    """Strip a path:Symbol suffix to recover the bare file path.

    Converts 'backend/app/models.py:Alert' → 'backend/app/models.py'.
    Line-number refs like 'file.py:42' are unchanged (digit after colon).
    """
    return _MODULE_PTR_SYMBOL_RE.sub("", ref)


_PRESENT_SYSTEM_STATUSES = {"mature", "functional", "partial"}
_PRESENT_CAPABILITY_STATUSES = {"working", "partial"}


def _parse_ref(ref: str) -> Tuple[str, int | None, int | None]:
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
    if not path or not isinstance(path, str):
        return False
    # Glob patterns ("routers/*.py") and whitespace-bearing tokens are never
    # literal repo paths — checking them costs a guaranteed-miss API call each.
    if any(ch in path for ch in "*?[]{}") or path != path.strip() or " " in path:
        return False
    return "/" in path or path.endswith((".py", ".ts", ".tsx", ".js", ".go", ".rs"))


async def _file_exists_at_head(
    path: str, *, cwd: str = "/tmp/workspace"
) -> bool | None:
    """Check whether a file exists in HEAD using 'git ls-files'. Tri-state.

    True  → present at HEAD.
    False → unambiguously absent at HEAD.
    None  → indeterminate (no local checkout, git error/timeout, or an
            unrecognizable path).

    An indeterminate result must NEVER be collapsed into "absent": a backend
    without a local checkout has no HEAD to consult, so callers that flag
    missing refs treat None as "leave untouched" rather than marking every
    citation invalid. Path-existence partitioners that have no notion of
    indeterminacy (e.g. :func:`classify_existing_paths`) treat the falsy None
    as missing, which preserves their prior behavior.
    """
    return await _path_exists_at_head(path, repo_root=cwd)


async def _path_exists_at_head(path: str, repo_root: str = _REPO_ROOT) -> bool | None:
    """Return whether *path* exists in HEAD, or None when indeterminate.

    False means the path is definitely absent in HEAD. None means git/root
    validation failed, so callers should degrade conservatively and leave the
    original value untouched.

    Normalizes path:Symbol suffixes before the git check so module pointers like
    'backend/app/models.py:Alert' resolve to the underlying file. For directory
    pointers (trailing '/'), any non-empty output from git ls-files confirms
    presence — the directory name itself never appears in the listing.
    """
    path = _normalize_module_ptr(path)
    is_dir = path.endswith("/")
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
        output = stdout.decode(errors="replace")
        if is_dir:
            # Directory pointer: presence is confirmed by any listed file beneath it.
            return bool(output.strip())
        return path in output.splitlines()
    except Exception as e:  # noqa: BLE001 — grounding must degrade to indeterminate, never raise
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
) -> bool | None:
    """Check whether *path* exists on ``owner/repo@branch``. Tri-state.

    Backed by the cached branch-tree snapshot
    (:func:`app.github_repo_snapshot.path_exists_on_branch` and
    :func:`app.github_repo_snapshot.glob_exists_on_branch`): set membership
    against one cached ``git/trees`` fetch instead of one ``/contents`` call
    per path, so a grounding pass costs O(1) API calls regardless of how many
    files it cites.

    True/False are definite answers from the snapshot. None is indeterminate
    (snapshot unavailable — rate limit, quota reserve, truncated tree, or any
    API error) and must NEVER be treated as absence: previously an API failure
    here read as ``False``, which let a quota outage downgrade design-doc
    entries to "missing".
    """
    if not isinstance(path, str):
        return None
    path = _normalize_module_ptr(path)
    if not path or path != path.strip() or " " in path:
        return None
    from app.github_repo_snapshot import glob_exists_on_branch, path_exists_on_branch

    if any(ch in path for ch in "*?[]{}"):
        return await glob_exists_on_branch(
            path,
            token=token,
            owner=owner,
            repo=repo,
            branch=branch,
            project_id=project_id,
        )

    if path.endswith("/"):
        # Directory pointer: use a glob to check whether any file exists beneath it.
        return await glob_exists_on_branch(
            path + "*",
            token=token,
            owner=owner,
            repo=repo,
            branch=branch,
            project_id=project_id,
        )

    return await path_exists_on_branch(
        path,
        token=token,
        owner=owner,
        repo=repo,
        branch=branch,
        project_id=project_id,
    )


async def _file_content_in_repo_branch(
    path: str,
    *,
    token: str,
    owner: str,
    repo: str,
    branch: str,
    project_id: str | None = None,
    return_content: bool = False,
) -> Tuple[bool | None, Any | None]:
    """Fetch *path* on ``owner/repo@branch`` and return ``(exists, line_count)``.

    The GitHub-API content foundation that line-number validation needs (the
    existing :func:`_file_exists_in_repo_branch` only answers existence). Also
    reusable by the symbol- and playbook-grounding paths that fetch content.

    ``exists`` is tri-state:
      True  → the path is a file on the branch (``line_count`` is its newline
              count, matching :func:`_file_has_line_count`'s ``count(b"\\n")``).
      False → the branch unambiguously has no such file (HTTP 404).
      None  → indeterminate (non-200/non-404, not-a-file payload, undecodable
              content, or any error). Callers must NOT treat None as absent.

    ``line_count`` is None whenever it cannot be determined, so an indeterminate
    count never invalidates a line-number citation.

    Backed by the cached snapshot layer
    (:func:`app.github_repo_snapshot.file_text_on_branch`): existence comes
    from the branch-tree snapshot and content from the blob cache (keyed by
    immutable blob SHA), so repeated reads of the same file across findings,
    design-doc entries, and retried passes cost one API call per content
    change instead of one per read.
    """
    path = _normalize_module_ptr(path)
    if not _looks_like_file_path(path):
        return None, None
    from app.github_repo_snapshot import file_text_on_branch

    exists, text = await file_text_on_branch(
        path,
        token=token,
        owner=owner,
        repo=repo,
        branch=branch,
        project_id=project_id,
    )
    if exists is not True:
        return exists, None
    if not isinstance(text, str):
        return True, None
    return (True, text) if return_content else (True, text.count("\n"))


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
    except Exception as e:  # noqa: BLE001 — grounding/advisory path must degrade, never raise
        logger.warning(
            f"[SOURCE_REF_VALIDATION] Error validating line count for {path}: {e}"
        )
        return False


async def _classify_each_path(
    paths: list,
    *,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    project_id: str | None = None,
    cwd: str = "/tmp/workspace",
) -> List[bool | None]:
    """Per-item existence verdicts for *paths*, aligned by index.

    True/False are definite answers from the integration-branch tree (or local
    git HEAD); None is indeterminate. Non-string entries are unambiguously not
    files → False. Shared core of the two public classifiers below.
    """
    use_repo_branch = all([token, owner, repo, branch])
    verdicts: List[bool | None] = []
    for p in paths:
        exists: bool | None
        if isinstance(p, str):
            # Both callees normalize path:Symbol refs and directory pointers
            # internally — no caller-level normalization needed here.
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
        else:
            exists = False
        verdicts.append(exists)
    return verdicts


async def classify_paths_tristate(
    paths: list,
    *,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    project_id: str | None = None,
    cwd: str = "/tmp/workspace",
) -> Tuple[List[str], List[str], List[str]]:
    """Classify *paths* into (present, missing, indeterminate) buckets.

    The honest three-bucket sibling of :func:`classify_existing_paths`:
    ``missing`` contains ONLY unambiguous absences from the integration-branch
    tree (or local git HEAD), while paths whose existence could not be
    determined (rate limit, quota reserve, truncated tree, git error) land in
    ``indeterminate``. Verdict-producing callers (claimed_absent / stale-ref /
    effective-status computations) must use THIS form so an API outage is
    never laundered into "the code does not exist". Order is preserved within
    each bucket.
    """
    verdicts = await _classify_each_path(
        paths,
        token=token,
        owner=owner,
        repo=repo,
        branch=branch,
        project_id=project_id,
        cwd=cwd,
    )
    present: List[str] = []
    missing: List[str] = []
    indeterminate: List[str] = []
    for p, exists in zip(paths, verdicts, strict=True):
        if exists is True:
            present.append(p)
        elif exists is False:
            missing.append(p)
        else:
            indeterminate.append(p)
    return present, missing, indeterminate


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

    Two-bucket compatibility form: indeterminate paths are folded into
    ``missing`` (the documented legacy falsy-None behavior). Callers that
    derive verdicts from absence must use :func:`classify_paths_tristate`
    instead.
    """
    verdicts = await _classify_each_path(
        paths,
        token=token,
        owner=owner,
        repo=repo,
        branch=branch,
        project_id=project_id,
        cwd=cwd,
    )
    present: List[str] = []
    missing: List[str] = []
    for p, exists in zip(paths, verdicts, strict=True):
        if exists is True:
            present.append(p)
        else:
            missing.append(p)
    return present, missing


async def _git_output(
    args: List[str], repo_root: str = _REPO_ROOT, timeout: int = 5
) -> str | None:
    """Run ``git <args>`` in *repo_root* and return stdout, or None on any failure.

    Conservative by construction: a missing checkout, non-zero exit, timeout, or
    any exception all collapse to None so callers degrade rather than guess.
    """
    if not repo_root or not os.path.isdir(repo_root):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return None
        if proc.returncode != 0:
            return None
        return stdout.decode(errors="replace")
    except Exception:  # noqa: BLE001 — grounding/advisory path must degrade, never raise
        logger.debug("[SOURCE_REF_VALIDATION] git %s failed", args, exc_info=True)
        return None


async def resolve_head_commit(
    *,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    project_id: str | None = None,
    cwd: str = "/tmp/workspace",
) -> str | None:
    """Resolve the current HEAD commit SHA of the integration/verification branch.

    Uses the GitHub API (the branch tip) when ``token/owner/repo/branch`` are all
    provided — the backend has no local checkout in prod — and falls back to the
    local checkout's ``git rev-parse HEAD`` otherwise (the agent VM has one).
    Returns None when indeterminate (no creds + no checkout, or any API/git
    error) so a finding is never stamped with a bogus provenance SHA. Mirrors the
    conservative degradation of :func:`validate_source_refs`.
    """
    if all([token, owner, repo, branch]):
        try:
            from app.github_refs import get_branch_sha

            return await get_branch_sha(str(token), str(owner), str(repo), str(branch))
        except Exception:  # noqa: BLE001 — grounding/advisory path must degrade, never raise
            logger.debug(
                "[SOURCE_REF_VALIDATION] head-commit GitHub resolution failed",
                exc_info=True,
            )
    out = await _git_output(["rev-parse", "HEAD"], cwd)
    return out.strip() if out and out.strip() else None


def finding_cited_paths(finding: Dict[str, Any]) -> List[str]:
    """Extract the file paths a finding cites via its file-type ``sources``.

    PR and message sources are skipped; line/range suffixes are stripped. Only
    refs that look like file paths are returned, ready to test against a diff.
    """
    paths: List[str] = []
    if not isinstance(finding, dict):
        return paths
    for src in finding.get("sources") or []:
        if not isinstance(src, dict):
            continue
        if (src.get("type") or "").lower() in ("pr", "message"):
            continue
        ref = src.get("ref")
        if not isinstance(ref, str) or not ref:
            continue
        path, _, _ = _parse_ref(ref)
        if path and _looks_like_file_path(path):
            paths.append(path)
    return paths


async def cited_paths_changed_since(
    sha: str | None,
    paths: list,
    *,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    project_id: str | None = None,
    cwd: str = "/tmp/workspace",
) -> bool | None:
    """Return whether any of *paths* changed between *sha* and current HEAD.

    Tri-state, mirroring :func:`validate_source_refs`:
      * True  — the diff was computed and at least one cited path appears in it.
      * False — the diff was computed and none of the cited paths changed.
      * None  — indeterminate (unknown SHA, no cited paths, no creds + no
                checkout, or an API/git error). Callers MUST NOT treat None as
                "changed": only an affirmative True flags a finding stale.
    """
    if not sha:
        return None
    norm = {_parse_ref(p)[0] for p in paths if isinstance(p, str) and p}
    norm.discard("")
    if not norm:
        return None
    if all([token, owner, repo, branch]):
        try:
            from app.github_refs import get_files_changed_between_refs

            changed = await get_files_changed_between_refs(
                str(token),
                str(owner),
                str(repo),
                sha,
                str(branch),
                project_id=project_id,
            )
        except Exception:  # noqa: BLE001 — grounding/advisory path must degrade, never raise
            logger.debug(
                "[SOURCE_REF_VALIDATION] changed-files GitHub compare failed",
                exc_info=True,
            )
            changed = None
    else:
        out = await _git_output(["diff", "--name-only", f"{sha}..HEAD"], cwd)
        changed = out.splitlines() if out is not None else None
    if changed is None:
        return None
    changed_set = {c for c in changed if c}
    return any(c in changed_set for c in norm)


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


async def _symbol_defined_at_head(
    path: str,
    symbol: str,
    *,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    project_id: str | None = None,
) -> bool | None:
    """Return whether ``symbol`` is defined in ``path`` at HEAD.

    True  → file exists at HEAD and a plausible definition of ``symbol`` found.
    False → unambiguously absent (file exists but no definition, or the file
            itself is absent at HEAD).
    None  → indeterminate (git unavailable / timeout / non-repo / error). Callers
            treat None as grounded so a git outage never flags refs.
    """
    if (
        not path
        or not symbol
        or not isinstance(path, str)
        or not isinstance(symbol, str)
    ):
        return None
    use_repo_branch = all([token, owner, repo, branch])
    if use_repo_branch:
        exists, text = await _file_content_in_repo_branch(
            path,
            token=str(token),
            owner=str(owner),
            repo=str(repo),
            branch=str(branch),
            project_id=project_id,
            return_content=True,
        )
        if exists is False:
            return False
        if not isinstance(text, str):
            return None
        return bool(_symbol_definition_regex(symbol).search(text))

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
    except Exception as e:  # noqa: BLE001 — grounding/advisory path must degrade, never raise
        logger.warning(f"[SOURCE_REF_VALIDATION] git show failed for {path}: {e}")
        return None
    if proc.returncode == 0:
        return bool(
            _symbol_definition_regex(symbol).search(stdout.decode(errors="replace"))
        )
    err = stderr.decode(errors="replace").lower()
    if "does not exist" in err or "exists on disk, but not in" in err:
        # git ran fine; the path is genuinely absent at HEAD → unambiguously absent.
        return False
    # not-a-repo or any other git error → indeterminate, degrade gracefully.
    return None


async def _symbol_defined_in_repo_branch(
    path: str,
    symbol: str,
    *,
    token: str,
    owner: str,
    repo: str,
    branch: str,
    project_id: str | None = None,
) -> bool | None:
    """Return whether ``symbol`` is defined in ``path`` on ``owner/repo@branch``.

    Branch-aware sibling of :func:`_symbol_defined_at_head` (which only reads the
    local checkout's HEAD). Fetches the file content from the project's real
    integration branch via the GitHub contents API and matches a plausible
    definition with :func:`_symbol_definition_regex` — the same content-fetch
    foundation :func:`_file_content_in_repo_branch` uses, so symbol grounding on
    the backend reflects the integration branch rather than a checkout that does
    not exist there.

    Tri-state, mirroring the conservative local-HEAD semantics:
      True  → the file exists on the branch and a plausible definition of
              ``symbol`` is found.
      False → unambiguously absent: the file exists on the branch but no
              plausible definition is found.
      None  → indeterminate (file 404/absent, not-a-file payload, undecodable
              content, or any API error). Callers treat None as grounded so a
              transient outage never flags a ref.

    Note the deliberate asymmetry with :func:`_symbol_defined_at_head`, which
    returns ``False`` for a file that is genuinely absent at HEAD: here a 404
    yields ``None`` because the symbol-level verdict is only meaningful when the
    file EXISTS. File-level absence is the file-level grounding's concern, so the
    symbol path must never flag (or double-flag) a missing file.
    """
    if (
        not path
        or not symbol
        or not isinstance(path, str)
        or not isinstance(symbol, str)
        or not _looks_like_file_path(path)
    ):
        return None

    # Shares the cached snapshot/blob reads with every other grounding path —
    # symbol checks on hot files no longer re-download the file per reference.
    # An absent file (exists False) stays indeterminate here per the docstring:
    # file-level absence is the file-level grounding pass's concern.
    exists, text = await _file_content_in_repo_branch(
        path,
        token=token,
        owner=owner,
        repo=repo,
        branch=branch,
        project_id=project_id,
        return_content=True,
    )
    if exists is not True or not isinstance(text, str):
        return None
    return bool(_symbol_definition_regex(symbol).search(text))


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
    *,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    project_id: str | None = None,
) -> Any:
    """Downgrade present-status design-doc entries whose backing files are gone.

    Conservative no-op when the entry is not a system/capability record, any
    referenced path is indeterminate, or at least one referenced path still
    exists. Only entries whose every referenced path is confirmed absent are
    downgraded to ``missing`` and annotated.

    When ``token``/``owner``/``repo``/``branch`` are all provided, backing-path
    presence is checked against the project's real GitHub integration-branch
    tree (tri-state — same mechanism :func:`classify_existing_paths` uses).
    Otherwise the local checkout's git HEAD is consulted via
    :func:`_path_exists_at_head`, in which case a valid ``repo_root`` is
    required (the ``os.path.isdir`` guard gates only this local branch). On
    EITHER path an indeterminate result leaves the entry untouched — a GitHub
    rate limit or git outage is never evidence the backing files are gone.
    """
    if not isinstance(value, dict):
        return value
    if not isinstance(key, str):
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

    use_repo_branch = all([token, owner, repo, branch])
    if not use_repo_branch and (not repo_root or not os.path.isdir(repo_root)):
        # Local-git fallback needs a valid checkout root; degrade to a no-op.
        return value

    seen_present = False
    for path in paths:
        if use_repo_branch:
            exists: bool | None = await _file_exists_in_repo_branch(
                path,
                token=str(token),
                owner=str(owner),
                repo=str(repo),
                branch=str(branch),
                project_id=project_id,
            )
            # Indeterminate (rate limit / quota reserve / API error) must leave
            # the entry untouched, exactly like the local-git branch below —
            # a GitHub outage is not evidence the backing files are gone.
            if exists is None:
                return value
        else:
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
    downgraded: dict[str, Any] = {
        **value,
        "status": "missing",
        "presence_warning": warning,
    }
    raw_text = value.get(text_field)
    existing = raw_text.strip() if isinstance(raw_text, str) else ""
    downgraded[text_field] = (
        f"{existing} " if existing else ""
    ) + f"[reconciled: {warning}]"
    return downgraded


async def ground_design_doc_value(
    value: Any,
    key: str,
    repo_root: str = _REPO_ROOT,
    *,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    project_id: str | None = None,
) -> Any:
    """File- AND symbol-level grounding for a single design-doc entry.

    First applies the file-level reconciliation
    (:func:`reconcile_design_doc_presence`): a present-status system/capability
    entry whose every referenced backing file is absent on the integration
    branch is downgraded to ``missing``. If the entry survives as present-status,
    it then runs the symbol-level check (:func:`detect_absent_symbols` over the
    entry's ``modules`` list plus its notes/description text) and, when a cited
    ``path:symbol`` pointer names a symbol that is unambiguously absent from a
    file that still exists (the post-refactor facade case the file-level check
    cannot see), applies the same "treat as missing" downgrade — annotating the
    text field and naming the absent symbol(s) in a ``presence_warning``.

    Conservative, mirroring the file-level semantics: a symbol is flagged only on
    an unambiguous absence (file present at HEAD, no plausible definition). An
    indeterminate result (git unavailable / non-repo) or a present symbol never
    downgrades the entry.

    Shared, public, self-contained helper: the symbol-aware sibling of the
    per-entry file-only :func:`reconcile_design_doc_presence`, reusable by the
    admin read path and the strategic-payload path. ``token``/``owner``/``repo``/
    ``branch``/``project_id`` ground BOTH passes against the project's real
    integration branch via the GitHub API; when credentials are absent both
    checks fall back to local git HEAD.
    """
    value = await reconcile_design_doc_presence(
        value,
        key,
        repo_root,
        token=token,
        owner=owner,
        repo=repo,
        branch=branch,
        project_id=project_id,
    )
    if not isinstance(value, dict) or not isinstance(key, str):
        return value
    if key.startswith("systems/"):
        present_statuses = _PRESENT_SYSTEM_STATUSES
        text_field = "description"
    elif key.startswith("capabilities/"):
        present_statuses = _PRESENT_CAPABILITY_STATUSES
        text_field = "notes"
    else:
        return value
    # A non-present status means the file-level pass already downgraded it (or it
    # never claimed to be built) — nothing for the symbol check to add.
    status = str(value.get("status") or "").strip().lower()
    if status not in present_statuses:
        return value

    modules = value.get("modules")
    module_pointers = modules if isinstance(modules, list) else []
    absent = await detect_absent_symbols(
        module_pointers,
        [value.get("notes"), value.get("description")],
        token=token,
        owner=owner,
        repo=repo,
        branch=branch,
        project_id=project_id,
    )
    if not absent:
        return value

    warning = (
        f"claimed {status} but cited symbol(s) {', '.join(absent)} are absent "
        f"even though the file exists — treat as missing"
    )
    downgraded: dict[str, Any] = {
        **value,
        "status": "missing",
        "presence_warning": warning,
    }
    raw_text = value.get(text_field)
    existing = raw_text.strip() if isinstance(raw_text, str) else ""
    downgraded[text_field] = (
        f"{existing} " if existing else ""
    ) + f"[reconciled: {warning}]"
    return downgraded


async def ground_design_doc(
    doc: Any,
    *,
    repo_root: str = _REPO_ROOT,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    project_id: str | None = None,
) -> Any:
    """Symbol-aware reconcile of every ``systems/*``/``capabilities/*`` entry.

    The symbol-aware replacement for the file-only walker: applies the combined file- AND symbol-level
    :func:`ground_design_doc_value` to each system/capability key, forwarding the
    GitHub verification context so present-but-absent entries — whether their
    backing file is gone OR their cited symbol was deleted from a still-present
    file — are downgraded against the project's real integration branch (and the
    local-git fallback otherwise). Non system/capability keys are passed through
    untouched. Operates on a shallow copy: the input mapping is never mutated.
    """
    if not isinstance(doc, dict):
        return doc
    out: dict[str, Any] = dict(doc)
    for entry_key, entry_value in doc.items():
        if not isinstance(entry_key, str):
            continue
        if entry_key.startswith("systems/") or entry_key.startswith("capabilities/"):
            out[entry_key] = await ground_design_doc_value(
                entry_value,
                entry_key,
                repo_root,
                token=token,
                owner=owner,
                repo=repo,
                branch=branch,
                project_id=project_id,
            )
    return out


async def resolve_branch_verification_context(
    session: Any, project: Any
) -> Dict[str, str]:
    """Resolve the GitHub-backed verification context for design-doc grounding.

    Returns the keyword arguments shared by :func:`classify_existing_paths`,
    :func:`ground_design_doc`, and the capability backing-path verifiers:
    always ``branch`` + ``project_id``, plus ``token``/``owner``/``repo`` when
    the project's GitHub credentials resolve. Degrades gracefully — on any
    credential-resolution failure (or a project without an authorized repo) the
    branch-only dict is returned so callers fall back to local git HEAD.
    """
    from app.branch_resolution import get_project_integration_branch
    from sqlalchemy import inspect as sa_inspect

    # This resolver is called from request and worker sessions whose identity
    # maps may already contain a deliberately narrow Project projection. A
    # later session.get() returns that same partial instance without querying,
    # so refresh the one branch column this function owns before synchronous
    # branch resolution touches a raiseload descriptor.
    state = sa_inspect(project, raiseerr=False)
    if state is not None and "integration_branch" in state.unloaded:
        await session.refresh(project, attribute_names=["integration_branch"])

    ctx: Dict[str, str] = {
        "branch": get_project_integration_branch(project),
        "project_id": str(project.id),
    }
    try:
        from app.release_promotion import _resolve_github_with_session

        resolved = await _resolve_github_with_session(session, project)
        if resolved:
            token, owner, repo = resolved
            ctx.update({"token": token, "owner": owner, "repo": repo})
    except Exception:  # noqa: BLE001 — grounding/advisory path must degrade, never raise
        logger.debug(
            "[SOURCE_REF_VALIDATION] GitHub credential resolution skipped for "
            "design-doc grounding",
            exc_info=True,
        )
    return ctx


async def detect_absent_symbols(
    module_pointers: list | None = None,
    text_fields: list | None = None,
    *,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    project_id: str | None = None,
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
        if (
            await _symbol_defined_at_head(
                path,
                symbol,
                token=token,
                owner=owner,
                repo=repo,
                branch=branch,
                project_id=project_id,
            )
            is False
        ):
            absent.append(f"{path}:{symbol}")
    return absent


async def summarize_ungrounded_claims(
    claims: list | None,
    sources: list | None = None,
) -> Dict[str, Any] | None:
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


def summarize_broken_refs(
    validated_sources: list | None,
) -> Dict[str, Any] | None:
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


async def validate_source_refs(
    sources: list | None,
    *,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    project_id: str | None = None,
    cwd: str = "/tmp/workspace",
) -> list:
    """Validate all file-type source refs in a sources list.

    For each source with type='file':
    - Validates that the file exists on the project's integration branch
    - Validates that line numbers (if present) are within bounds
    - Marks unambiguously-invalid sources with valid: false

    When ``token``/``owner``/``repo``/``branch`` are all provided, existence and
    line counts are checked against the project's REAL integration branch via
    the GitHub contents API (:func:`_file_content_in_repo_branch`) — the same
    repo/branch verification context the design-doc reconcile path resolves. The
    finding-ingestion caller already holds the project + session, so it resolves
    that context once (see :func:`resolve_branch_verification_context`) and
    passes it through. Otherwise validation falls back to the local checkout's
    git HEAD (the agent VM legitimately has one).

    Degradation is conservative: an INDETERMINATE result (no GitHub credentials,
    no local checkout, or a git/API error) must NEVER mark a citation invalid —
    only an unambiguous absence on the integration branch (or a genuinely
    out-of-range line) flags a ref. This keeps prod findings (where the backend
    has no checkout) from having every valid citation marked broken.

    For sources with type='pr' or type='message': no validation needed.

    Returns the list of validated sources, with invalid file refs marked
    valid: false (refs missing a 'ref' field are dropped).
    """
    if not isinstance(sources, list):
        return []

    use_repo_branch = all([token, owner, repo, branch])
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

        # Unknown (non-file) source types pass through untouched
        if source_type != "file":
            validated.append(source)
            continue

        ref = source.get("ref")
        if not ref:
            logger.warning("[SOURCE_REF_VALIDATION] File source missing 'ref' field")
            continue

        path, start_line, end_line = _parse_ref(ref)

        # Resolve existence (tri-state) against the integration branch or local
        # HEAD. ``line_count`` is only populated by the GitHub path; the local
        # path validates line bounds lazily via _file_has_line_count below.
        if use_repo_branch:
            exists, line_count = await _file_content_in_repo_branch(
                path,
                token=str(token),
                owner=str(owner),
                repo=str(repo),
                branch=str(branch),
                project_id=project_id,
            )
        else:
            # Preserve the legacy single-arg call signature when cwd is the
            # default so existing callers/mocks (path-only) keep working.
            exists = (
                await _file_exists_at_head(path)
                if cwd == "/tmp/workspace"
                else await _file_exists_at_head(path, cwd=cwd)
            )
            line_count = None

        # Indeterminate existence (None) must never flag a citation — leave it
        # untouched. Only an unambiguous absence (False) marks the ref invalid.
        if exists is None:
            validated.append(source)
            continue
        if exists is False:
            logger.warning(
                f"[SOURCE_REF_VALIDATION] File not found on integration branch: "
                f"{path} (from ref {ref})"
            )
            marked = dict(source)
            marked["valid"] = False
            validated.append(marked)
            continue

        # exists is True — validate line numbers when present.
        if start_line is not None:
            # end < start is intrinsically malformed regardless of the tree.
            if end_line is not None and end_line < start_line:
                logger.warning(
                    f"[SOURCE_REF_VALIDATION] Invalid line range: "
                    f"{path}:{start_line}-{end_line} (end < start in ref {ref})"
                )
                marked = dict(source)
                marked["valid"] = False
                validated.append(marked)
                continue

            if use_repo_branch:
                # An indeterminate line_count (None) never invalidates; only a
                # definite out-of-range count does.
                if line_count is not None and (
                    start_line > line_count
                    or (end_line is not None and end_line > line_count)
                ):
                    logger.warning(
                        f"[SOURCE_REF_VALIDATION] Line number out of range: "
                        f"{path}:{start_line} (file has {line_count} lines, "
                        f"from ref {ref})"
                    )
                    marked = dict(source)
                    marked["valid"] = False
                    validated.append(marked)
                    continue
            else:
                if not await _file_has_line_count(path, start_line):
                    logger.warning(
                        f"[SOURCE_REF_VALIDATION] Line number out of range: "
                        f"{path}:{start_line} (from ref {ref})"
                    )
                    marked = dict(source)
                    marked["valid"] = False
                    validated.append(marked)
                    continue
                if end_line is not None and not await _file_has_line_count(
                    path, end_line
                ):
                    logger.warning(
                        f"[SOURCE_REF_VALIDATION] End line out of range: "
                        f"{path}:{start_line}-{end_line} (from ref {ref})"
                    )
                    marked = dict(source)
                    marked["valid"] = False
                    validated.append(marked)
                    continue

        validated.append(source)

    return validated


# ── Free-text bug-report citation grounding ───────────────────────────────────
# Agent/operator-submitted BUG_REPORT (and BLOCKER) findings arrive as free
# prose, not the structured ``sources``/``claims`` a research finding carries.
# The helpers below reuse the SAME validation machinery research findings flow
# through (file existence via :func:`validate_source_refs`/:func:`summarize_broken_refs`,
# symbol grounding via :func:`detect_absent_symbols`) so a bug report whose cited
# code was already refactored away or fixed is flagged — and discounted — exactly
# like a stale research citation, instead of being trusted blindly.

CITATION_STATE_ABSENT = "absent"
CITATION_STATE_PRESENT = "present"
CITATION_STATE_INDETERMINATE = "indeterminate"


def classify_reproduction(
    *,
    present_file_count: int,
    broken_file_count: int,
    total_symbol_refs: int,
    ungrounded_symbol_count: int,
    has_backend: bool,
) -> str:
    """Single conservative reproduce-on-HEAD verdict shared by every gate.

    The one place the "does this finding/bug-report still reproduce on the
    integration HEAD?" question is decided, so finding ingestion, deferred-finding
    recovery, and the bug-report citation summary all agree by construction.

    Inputs are pre-computed counts from the EXISTING grounding helpers:
      * ``present_file_count``  — file citations that resolve at HEAD (a file ref
        not marked ``valid: False`` by :func:`validate_source_refs`; an
        indeterminate file is counted here so an unknowable file never drives a
        rejection).
      * ``broken_file_count``   — file citations unambiguously absent at HEAD
        (``broken_source_ref_count`` from :func:`summarize_broken_refs`).
      * ``total_symbol_refs``   — distinct ``path:symbol`` references parsed from
        the claims/excerpts (:func:`_parse_path_symbol_refs`).
      * ``ungrounded_symbol_count`` — of those, the count unambiguously absent
        (:func:`detect_absent_symbols`).
      * ``has_backend``         — whether any existence/symbol check could run at
        all (:func:`_has_validation_backend`).

    Returns:
      * :data:`CITATION_STATE_ABSENT` ONLY when at least one citation was actually
        checkable, ZERO file citations survive, and EVERY parseable ``path:symbol``
        reference is ungrounded — i.e. nothing the input cites still exists.
      * :data:`CITATION_STATE_INDETERMINATE` when no backend was available or
        nothing was checkable (no citations, or every check degraded). A
        checkout-less prod backend therefore NEVER rejects.
      * :data:`CITATION_STATE_PRESENT` otherwise — any surviving file or grounded
        symbol (including a partially-grounded input) passes through.

    Mirrors the conservative tri-state semantics of :func:`validate_source_refs`:
    an indeterminate or partially-grounded input is never reported ``absent``.
    """
    if not has_backend:
        return CITATION_STATE_INDETERMINATE
    # A surviving (present-or-unknowable) file citation means cited code remains.
    if present_file_count > 0:
        return CITATION_STATE_PRESENT
    # A grounded symbol (any parseable ref not proven absent) likewise survives.
    # Indeterminate symbols count as grounded here, so an inconclusive symbol
    # check never tips a verdict to ``absent``.
    if (total_symbol_refs - ungrounded_symbol_count) > 0:
        return CITATION_STATE_PRESENT
    # Nothing survives. Reject ONLY when at least one citation came back
    # unambiguously absent; if nothing could actually be checked, stay
    # indeterminate so a git/API outage never spuriously rejects.
    if broken_file_count > 0 or ungrounded_symbol_count > 0:
        return CITATION_STATE_ABSENT
    return CITATION_STATE_INDETERMINATE


def _extract_bug_report_file_refs(
    content: Any, structured_context: Dict[str, Any] | None = None
) -> List[str]:
    """Extract repo-relative file paths cited in a bug report's free text.

    References are pulled two ways, mirroring the design-doc grounding path:
    - From the prose via :data:`_DESIGN_DOC_PATH_RE`, which matches repo-relative
      ``backend|agent|admin-ui/.../<name>.<ext>`` tokens embedded anywhere in the
      text (the same regex :func:`_extract_design_doc_paths` uses on notes).
    - From an optional ``structured_context`` mapping's ``file_path`` field (e.g.
      an agent annotation's ``file_path``), so a structured pointer is grounded
      even when the prose does not repeat it.

    Line/range suffixes (``:42``, ``#L10-L20``) are stripped via
    :func:`_parse_ref`, non-file-looking tokens are dropped, and the result is
    deduplicated and order-preserving.
    """
    paths: List[str] = []
    seen: set = set()

    def _add(raw: Any) -> None:
        if not isinstance(raw, str) or not raw:
            return
        path, _, _ = _parse_ref(raw.strip())
        if path and _looks_like_file_path(path) and path not in seen:
            seen.add(path)
            paths.append(path)

    if isinstance(structured_context, dict):
        _add(structured_context.get("file_path"))
    if isinstance(content, str) and content:
        for match in _DESIGN_DOC_PATH_RE.finditer(content):
            _add(match.group(1))
    return paths


def _has_validation_backend(
    token: str | None,
    owner: str | None,
    repo: str | None,
    branch: str | None,
    cwd: str,
) -> bool:
    """Whether existence/symbol checks have any usable backend to consult.

    True when full GitHub credentials are present, or a local git checkout
    exists at *cwd*. When neither is available every check degrades to
    indeterminate, so the caller must NOT report a clean result as ``present``.
    """
    if all([token, owner, repo, branch]):
        return True
    return bool(cwd and os.path.isdir(os.path.join(cwd, ".git")))


async def summarize_bug_report_citations(
    content: Any,
    *,
    structured_context: Dict[str, Any] | None = None,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    project_id: str | None = None,
    cwd: str = "/tmp/workspace",
) -> Dict[str, Any]:
    """Ground a free-text bug report's citations against the integration branch.

    Extracts file references and ``path:symbol`` references from *content* (and an
    optional *structured_context* mapping, e.g. an annotation's ``file_path``) —
    see :func:`_extract_bug_report_file_refs` and :func:`_parse_path_symbol_refs`
    for exactly how references are pulled from the prose — then validates each
    against the project's real integration branch reusing the existing machinery:
    file existence via :func:`validate_source_refs` + :func:`summarize_broken_refs`,
    symbol presence via :func:`detect_absent_symbols`.

    Returns a structured signal mirroring :func:`summarize_broken_refs`::

        {"citation_state": "absent"|"present"|"indeterminate",
         "broken_refs": [path, ...],
         "ungrounded_refs": ["path:symbol", ...]}

    Degradation is conservative and consistent with :func:`validate_source_refs`:
    a reference is only ``absent`` when it is UNAMBIGUOUSLY missing on the branch.
    When NO references can be extracted, or no validation backend is available
    (no credentials and no local checkout), the state is ``indeterminate`` and is
    NEVER reported as ``absent``/broken. Best-effort: any internal error degrades
    to an empty (indeterminate) result rather than raising.
    """
    file_paths = _extract_bug_report_file_refs(content, structured_context)
    symbol_pairs = _parse_path_symbol_refs(content)

    broken_refs: List[str] = []
    if file_paths:
        sources = [{"type": "file", "ref": p} for p in file_paths]
        try:
            validated = await validate_source_refs(
                sources,
                token=token,
                owner=owner,
                repo=repo,
                branch=branch,
                project_id=project_id,
                cwd=cwd,
            )
            broken = summarize_broken_refs(validated)
            if broken:
                broken_refs = list(broken.get("broken_refs") or [])
        except Exception:  # noqa: BLE001 — grounding must never raise to the caller
            logger.debug(
                "[SOURCE_REF_VALIDATION] bug-report file-citation check failed",
                exc_info=True,
            )
            broken_refs = []

    ungrounded_refs: List[str] = []
    if symbol_pairs:
        try:
            ungrounded_refs = await detect_absent_symbols(
                None,
                [content],
                token=token,
                owner=owner,
                repo=repo,
                branch=branch,
                project_id=project_id,
            )
        except Exception:  # noqa: BLE001 — grounding must never raise to the caller
            logger.debug(
                "[SOURCE_REF_VALIDATION] bug-report symbol-grounding check failed",
                exc_info=True,
            )
            ungrounded_refs = []

    # Delegate the absent/present/indeterminate verdict to the single shared
    # reproduce-on-HEAD classifier so bug-report grounding agrees with finding
    # ingestion by construction: ``absent`` only when NOTHING the report cites
    # still exists; any surviving independent file (or grounded symbol) keeps the
    # verdict non-absent (a partially-grounded report counts as ``present``).
    #
    # CRITICAL: ``_DESIGN_DOC_PATH_RE`` also surfaces the host path of a
    # ``path:symbol`` token as a standalone file ref, so a file that appears
    # ONLY as the host of an explicitly-cited (now-absent) symbol must NOT be
    # counted as an independent surviving file — otherwise the symbol-locus-absent
    # case (present host file + absent symbol) would wrongly flip to ``present``.
    symbol_host_paths = {path for path, _symbol in symbol_pairs}
    present_file_count = sum(
        1
        for path in file_paths
        if path not in broken_refs and path not in symbol_host_paths
    )
    # A definitively-broken file ref or ungrounded symbol can only have been
    # produced by a backend that actually ran, so treat their presence as proof
    # a backend was consulted even when the credentials/local-checkout heuristic
    # cannot see one (e.g. a checkout-less prod path whose existence check still
    # resolved to a hard 404). ``classify_reproduction`` trusts ``has_backend``
    # absolutely — without this, a confirmed-absent citation would be suppressed
    # to ``indeterminate`` and never flagged. Empty broken/ungrounded sets leave
    # ``has_backend`` driven solely by the heuristic, so an unknowable (None)
    # check still degrades to ``indeterminate``, never ``absent``.
    has_backend = (
        _has_validation_backend(token, owner, repo, branch, cwd)
        or bool(broken_refs)
        or bool(ungrounded_refs)
    )
    state = classify_reproduction(
        present_file_count=present_file_count,
        broken_file_count=len(broken_refs),
        total_symbol_refs=len(symbol_pairs),
        ungrounded_symbol_count=len(ungrounded_refs),
        has_backend=has_backend,
    )

    return {
        "citation_state": state,
        "broken_refs": broken_refs,
        "ungrounded_refs": ungrounded_refs,
    }


def should_persist_message_citation_warning(signal: Any) -> bool:
    """Whether a citation signal should be stored on message/task context.

    We only persist signals that carry operator/strategist-facing provenance:
    a fully stale ``absent`` verdict, or a mixed non-absent verdict that still
    has broken/ungrounded refs worth surfacing. Clean ``present`` and empty
    ``indeterminate`` results are omitted to avoid storing noise.
    """
    if not isinstance(signal, dict):
        return False
    if signal.get("citation_state") == CITATION_STATE_ABSENT:
        return True
    broken_refs = signal.get("broken_refs")
    if isinstance(broken_refs, list) and broken_refs:
        return True
    ungrounded_refs = signal.get("ungrounded_refs")
    return isinstance(ungrounded_refs, list) and bool(ungrounded_refs)


async def compute_message_citation_warning(
    session: Any,
    project: Any,
    content: Any,
    *,
    structured_context: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    """Best-effort citation warning for a BUG_REPORT/BLOCKER message, or None.

    Resolves the project's integration-branch verification context (via
    :func:`resolve_branch_verification_context`) and runs
    :func:`summarize_bug_report_citations`. Returns the structured signal when
    it carries actionable provenance for downstream consumers: a fully stale
    ``absent`` verdict, or a mixed non-absent verdict with broken/ungrounded
    refs that should remain visible. Clean ``present``/``indeterminate`` inputs
    still return None so message creation does not accumulate noise. Never
    raises: any failure degrades to None, so message creation is never blocked
    by grounding.
    """
    try:
        ctx = await resolve_branch_verification_context(session, project)
        signal = await summarize_bug_report_citations(
            content, structured_context=structured_context, **ctx
        )
    except Exception:  # noqa: BLE001 — best-effort, must never fail message creation
        logger.debug(
            "[SOURCE_REF_VALIDATION] message citation warning computation failed",
            exc_info=True,
        )
        return None
    if should_persist_message_citation_warning(signal):
        return signal
    return None

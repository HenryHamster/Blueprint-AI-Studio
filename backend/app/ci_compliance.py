"""Per-merge CI-compliance evaluator + per-SHA store (Direction E).

Blueprint's module-size guardrails (``backend/testing/module_size_limits.json``)
are normally enforced only when a qualification PR runs CI. By then a violation
has already merged onto ``automain`` and the engine learns about it late. This
module is the foundational PRODUCER for discovering those violations
CONTINUOUSLY — once per automain-HEAD advance — so the strategist can react
before the next qualify_request.

CRITICAL CONSTRAINT — no local checkout: the production backend has NO working
tree of the evaluated repo. EVERY file read therefore goes through the GitHub
file-read-at-SHA path (:func:`app.github.get_file_text_at_ref`); we never touch
``os.path`` / ``open()`` for the evaluated repo contents. The precedent this
generalizes is :func:`app.hotfix_backmerge._check_module_size_violations`, which
already reads capped files at a ref and compares line counts to the caps.

The breach/cap-bump POLICY is NOT duplicated here: classification is delegated
to :func:`testing.check_size_limits.classify_size_record`, the single
content-injectable source of truth shared with the disk-mode CLI.

Three public entry points:

  * :func:`evaluate_ci_compliance_at_ref` — the async evaluator. Reads the
    limits file, the branch-literal rules file, and the repo tree at the ref,
    then checks (a) capped modules for line-count breaches, (b) uncapped source
    files over the coverage threshold, and (c) backend/agent source files for
    hardcoded branch-literal patterns. Sections mirror CI: ``backend``,
    ``agent``, ``frontend``, and ``testing`` (``backend/testing/*.py`` per-file
    caps and the 1500-line uncapped coverage guardrail). The branch-literal
    scan runs whenever the repo tree is available AND the rules file is
    readable — even when the limits file is unreadable — because CI's
    branch-literal invariants run regardless of the limits file's readability.
    The branch-literal ALLOWLIST is read AT THE CANDIDATE REF (the same
    checked-in file the local invariant test scans against), so a rule that
    ships in the same commit as the file it describes is observable by this
    gate. Returns a JSON-serializable result. Fail-safe: a source file that
    404s / cannot be read at the ref is SKIPPED (treated as not-a-violation),
    never crashing the evaluation. The size-limits FILE and the branch-literal
    RULES file are the deliberate exceptions: when either cannot be read or
    parsed, the verdict FAILS CLOSED (``compliant=False`` with a diagnostic) —
    an unreadable config is exactly the circumstance in which the gate is least
    entitled to pass, so the evaluator refuses instead of skipping those
    guardrails and reporting success.
  * :func:`record_ci_compliance` — persist a result onto ``Project.ci_compliance``
    keyed by its commit SHA.
  * :func:`get_ci_compliance` — read the per-SHA status back.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from app import github
from app.branch_literal_compliance import (
    AGENT_PATTERNS,
    AGENT_SOURCE_FILES,  # noqa: F401 — re-exported for module-level identity checks
    BACKEND_PATTERNS,
    BRANCH_LITERAL_ALLOWLIST_PATH,
    CI_AGENT_FORBIDDEN_RE,  # noqa: F401 — re-exported for module-level identity checks
    CI_AGENT_PAREN_FALLBACK_RE,
    strip_typescript_with_linenos,
)
from app.json_extract import async_json_loads

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import Project

logger = logging.getLogger(__name__)

# Repo-relative path of the tracked caps file. Read AT the ref via GitHub — the
# on-disk copy (used by hotfix_backmerge._load_size_limits) describes THIS
# checkout, not the automain HEAD being evaluated.
_MODULE_SIZE_LIMITS_PATH = "backend/testing/module_size_limits.json"

# Maps a module_size_limits.json section to the repo-relative path prefix its
# ``rel`` keys are rooted at. Mirrors hotfix_backmerge._FILE_PREFIX_MAP (inverted
# direction) and check_size_limits.resolve_section_root's section roots. The
# ``testing`` section is enforced by CI's TestTestFileSizeGuardrails /
# test_testing_no_uncapped_large_file and must be scanned exactly like CI does.
_SECTION_PREFIXES: dict[str, str] = {
    "backend": "backend/app/",
    "agent": "agent/src/",
    "frontend": "admin-ui/src/",
    "testing": "backend/testing/",
}

# Bound the per-SHA history so the JSONB column cannot grow without limit across
# thousands of merges. Most-recently-recorded SHAs are retained (dict insertion
# order); older entries are evicted.
_MAX_RECORDED_SHAS = 50

# Process-level registry of in-flight CI-compliance evaluations keyed by
# ``"{project_id}:{sha}"``. When a cache miss triggers a new evaluation, the
# coroutine is wrapped in a task and registered here. Concurrent requests for
# the SAME (project, SHA) pair await the SAME task rather than spawning duplicate
# full-evaluation round-trips to GitHub (which are expensive: 200+ file reads).
# Entries are removed once the task completes (success or exception).
_in_flight_evals: dict[str, asyncio.Task[dict[str, Any]]] = {}

# Maximum concurrent GitHub API requests for file reads. The three scan helpers
# (_scan_capped_files, _scan_uncapped_files, _scan_branch_literals) parallelize
# per-file fetches with asyncio.gather across potentially 200+ files. Without a
# cap, all requests fire concurrently, overwhelming the HTTP connection pool and
# triggering GitHub secondary rate limiting — observed as spiky latency and
# transient 429/503 responses on cold-compute paths. Using 10 concurrent slots
# is generous enough to saturate a typical 4-core VM's HTTP stack while keeping
# the outgoing connection burst well under GitHub's per-second rate limits.
_DEFAULT_CONCURRENCY = 10

_semaphore = asyncio.Semaphore(_DEFAULT_CONCURRENCY)

# Line-count threshold above which an uncapped source file triggers a violation.
# Mirrors check_size_limits._COVERAGE_THRESHOLD and test_invariants._COVERAGE_THRESHOLD.
_UNCAPPED_THRESHOLD = 1500

# Per-section source file extensions for the uncapped-coverage guardrail.
# Mirrors check_size_limits._SECTION_SCAN (backend/app .py, agent .ts,
# frontend .ts/.tsx, testing .py).
_SECTION_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "backend": (".py",),
    "agent": (".ts",),
    "frontend": (".ts", ".tsx"),
    "testing": (".py",),
}

# Files excluded from the uncapped-coverage guardrail per section.
# Mirrors check_size_limits._is_frontend_excluded / _is_agent_excluded /
# _no_exclusion (testing scans every .py like the backend section).
def _is_frontend_excluded(rel: str) -> bool:
    return rel.endswith((".test.ts", ".test.tsx", ".d.ts")) or rel.startswith("test/")


def _is_agent_excluded(rel: str) -> bool:
    return rel.endswith((".test.ts", ".d.ts"))


_SECTION_EXCLUDED: dict[str, Any] = {
    "backend": lambda rel: False,
    "agent": _is_agent_excluded,
    "frontend": _is_frontend_excluded,
    "testing": lambda rel: False,
}

# Supplemental agent patterns: extend canonical AGENT_PATTERNS with loose
# equality (==|!=, not ===|!==) for main/automain and all equality ops for
# master. The lookbehind/lookahead anchors prevent double-counting with the
# canonical equality-main pattern (===|!==).
_AGENT_SUPPLEMENTAL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("equality-loose-main", re.compile(r"""(?<![=!])(?:==|!=)(?!=)\s*['"](main|automain)['"]""")),
    ("equality-master", re.compile(r"""(?:===|!==|==|!=)\s*['"]master['"]""")),
]


# Status values returned by _load_size_limits_at_ref. ``ok`` means the limits
# file was read and parsed as a JSON object — possibly ``{}``, a legitimate
# empty config that simply declares no caps. The other two are genuine
# read/parse failures and FAIL CLOSED: an unreadable size-limits config is
# exactly the circumstance in which the compliance gate is least entitled to
# pass, because the size guardrails cannot be verified at all.
_LIMITS_STATUS_OK = "ok"
_LIMITS_STATUS_UNREADABLE = "unreadable"
_LIMITS_STATUS_MALFORMED = "malformed"


async def _load_size_limits_at_ref(
    *, token: str, owner: str, repo: str, ref: str, project_id: str | None
) -> tuple[dict, str, str | None]:
    """Read ``module_size_limits.json`` AT ``ref`` and parse it.

    Returns ``(limits, status, error)``:

      * ``status == "ok"`` — the file was read and parsed as a JSON object.
        ``limits`` is that object (``{}`` when the config legitimately declares
        no caps); ``error`` is None.
      * ``status == "unreadable"`` — the file could not be read at the ref
        (404 / non-file / undecodable transport outcome). ``limits`` is ``{}``
        and ``error`` names the path and the failure.
      * ``status == "malformed"`` — the file was read but its body is not a
        JSON object (unparseable JSON or a non-dict top-level value).
        ``limits`` is ``{}`` and ``error`` names the path and the failure.

    The distinction between a legitimate empty config and a genuine failure is
    deliberate: only the former may pass, so the caller can fail closed on the
    latter instead of silently skipping the size guardrails.
    """
    text = await github.get_file_text_at_ref(
        _MODULE_SIZE_LIMITS_PATH,
        token=token,
        owner=owner,
        repo=repo,
        ref=ref,
        project_id=project_id,
    )
    _where = f"{_MODULE_SIZE_LIMITS_PATH} at {owner}/{repo}@{ref[:12]}"
    if text is None:
        return {}, _LIMITS_STATUS_UNREADABLE, (
            f"could not read {_where} (404 / non-file / undecodable)"
        )
    if not text:
        return {}, _LIMITS_STATUS_MALFORMED, (
            f"{_where} is empty; expected a JSON object of size caps"
        )
    try:
        data = await async_json_loads(text)
    except (ValueError, TypeError) as exc:
        logger.warning("[CI-COMPLIANCE] could not parse %s: %s", _where, exc)
        return {}, _LIMITS_STATUS_MALFORMED, (
            f"{_where} is not valid JSON: {exc}"
        )
    if not isinstance(data, dict):
        return {}, _LIMITS_STATUS_MALFORMED, (
            f"{_where} is a JSON {type(data).__name__}, not an object"
        )
    return data, _LIMITS_STATUS_OK, None


# Status values returned by _load_branch_literal_rules_at_ref. Same contract as
# the size-limits statuses above: ``ok`` means the file was read and parsed as
# a JSON object (possibly ``{}``); ``unreadable`` and ``malformed`` are genuine
# failures that FAIL CLOSED. The branch-literal allowlist is read AT THE
# CANDIDATE REF precisely so a rule that ships in the same commit as the file
# it describes is observable by the deployed gate — a process-local allowlist
# cannot see it and deadlocks the gate (the provenance defect this reader
# fixes).
_RULES_STATUS_OK = "ok"
_RULES_STATUS_UNREADABLE = "unreadable"
_RULES_STATUS_MALFORMED = "malformed"


async def _load_branch_literal_rules_at_ref(
    *, token: str, owner: str, repo: str, ref: str, project_id: str | None
) -> tuple[dict, str, str | None]:
    """Read ``branch_literal_allowlist.json`` AT ``ref`` and parse it.

    Mirrors :func:`_load_size_limits_at_ref` for the branch-literal allowlist:
    returns ``(rules, status, error)`` where ``status`` is ``ok`` /
    ``unreadable`` / ``malformed``. ``rules`` is the parsed JSON object on
    ``ok`` (``{}`` for a legitimately-empty config) and ``{}`` otherwise.
    ``error`` names the path and the failure when ``status != ok``.

    The distinction between a legitimate empty config and a genuine failure is
    deliberate: only the former may pass, so the caller can FAIL CLOSED on
    unreadable/malformed rules instead of scanning with an in-process allowlist
    the deployed backend may not know about.
    """
    text = await github.get_file_text_at_ref(
        BRANCH_LITERAL_ALLOWLIST_PATH,
        token=token,
        owner=owner,
        repo=repo,
        ref=ref,
        project_id=project_id,
    )
    _where = f"{BRANCH_LITERAL_ALLOWLIST_PATH} at {owner}/{repo}@{ref[:12]}"
    if text is None:
        return {}, _RULES_STATUS_UNREADABLE, (
            f"could not read {_where} (404 / non-file / undecodable)"
        )
    if not text:
        return {}, _RULES_STATUS_MALFORMED, (
            f"{_where} is empty; expected a JSON object of allowlists"
        )
    try:
        data = await async_json_loads(text)
    except (ValueError, TypeError) as exc:
        logger.warning("[CI-COMPLIANCE] could not parse %s: %s", _where, exc)
        return {}, _RULES_STATUS_MALFORMED, (
            f"{_where} is not valid JSON: {exc}"
        )
    if not isinstance(data, dict):
        return {}, _RULES_STATUS_MALFORMED, (
            f"{_where} is a JSON {type(data).__name__}, not an object"
        )
    return data, _RULES_STATUS_OK, None


def _strip_agent_paren_fallbacks(line: str) -> str:
    """Remove parenthesized fallback expressions like ``(promotionTarget ?? 'main')``."""
    prev = None
    out = line
    while prev != out:
        prev = out
        out = CI_AGENT_PAREN_FALLBACK_RE.sub("", out)
    return out


async def _scan_capped_files(
    *,
    limits: dict,
    token: str,
    owner: str,
    repo: str,
    ref: str,
    project_id: str | None,
) -> tuple[list[dict], int]:
    """Check capped modules for line-count breaches.

    Returns (violations, checked) where violations is a list of
    ``{section, rel, count, cap}`` dicts and checked is the number of files
    actually read at the ref.
    """
    from testing.check_size_limits import classify_size_record

    # Collect all (section, rel, cap, path) entries to fetch in parallel.
    entries: list[tuple[str, str, int, str]] = []
    for section, prefix in _SECTION_PREFIXES.items():
        caps = limits.get(section)
        if not isinstance(caps, dict):
            continue
        for rel, cap in caps.items():
            if not isinstance(cap, int):
                continue
            entries.append((section, rel, cap, f"{prefix}{rel}"))

    async def _fetch(path: str) -> str | None:
        async with _semaphore:
            try:
                return await github.get_file_text_at_ref(
                    path,
                    token=token,
                    owner=owner,
                    repo=repo,
                    ref=ref,
                    project_id=project_id,
                )
            except Exception as exc:  # noqa: BLE001 — never crash the producer
                logger.debug(
                    "[CI-COMPLIANCE] read of %s at %s/%s@%s raised: %s — skipping",
                    path,
                    owner,
                    repo,
                    ref,
                    exc,
                )
                return None

    texts = await asyncio.gather(*(_fetch(path) for _, _, _, path in entries))
    violations: list[dict] = []
    checked = 0
    for (section, rel, cap, _path), text in zip(entries, texts):
        if text is None:
            continue
        checked += 1
        count = len(text.splitlines())
        rec = classify_size_record(rel, count, cap, section, with_verdict=False)
        if rec["breach"]:
            violations.append(
                {"section": section, "rel": rel, "count": count, "cap": cap}
            )
    return violations, checked


async def _scan_uncapped_files(
    *,
    limits: dict,
    tree_paths: list[dict],
    token: str,
    owner: str,
    repo: str,
    ref: str,
    project_id: str | None,
) -> list[dict]:
    """Check for source files over the coverage threshold that lack a cap entry.

    Mirrors the uncapped-coverage guardrail in
    ``test_invariants._test_no_uncapped_large_file`` and
    ``check_size_limits.scan_uncapped_section``: a source file over
    ``_UNCAPPED_THRESHOLD`` lines with no cap entry in the limits JSON is a
    violation because it is invisible to the per-file caps and can grow without
    limit while the guardrail reports clean.

    Returns a list of ``{section, rel, count, cap: None}`` violation dicts.
    """
    # Collect candidates that pass the cheap pre-filters, then fetch in parallel.
    candidates: list[tuple[str, str, str]] = []  # (section, rel, path)

    for section, prefix in _SECTION_PREFIXES.items():
        caps = limits.get(section) or {}
        exts = _SECTION_EXTENSIONS.get(section, ())
        is_excluded = _SECTION_EXCLUDED.get(section, lambda _: False)

        for entry in tree_paths:
            path = entry["path"]
            if not path.startswith(prefix):
                continue
            if not path.endswith(exts):
                continue
            rel = path[len(prefix):]
            if is_excluded(rel):
                continue
            if rel in caps:
                continue
            # Only fetch if size heuristic suggests it might be over threshold.
            # Each line has at least one newline byte, so a file with
            # _UNCAPPED_THRESHOLD lines has at least _UNCAPPED_THRESHOLD bytes.
            # Skip files whose reported size is strictly below that minimum —
            # they cannot possibly exceed the line threshold.
            file_size = entry.get("size", 0)
            if isinstance(file_size, int) and file_size < _UNCAPPED_THRESHOLD:
                continue
            candidates.append((section, rel, path))

    if not candidates:
        return []

    async def _fetch(path: str) -> str | None:
        async with _semaphore:
            try:
                return await github.get_file_text_at_ref(
                    path,
                    token=token,
                    owner=owner,
                    repo=repo,
                    ref=ref,
                    project_id=project_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[CI-COMPLIANCE] uncapped read of %s at %s/%s@%s raised: %s — skipping",
                    path,
                    owner,
                    repo,
                    ref,
                    exc,
                )
                return None

    texts = await asyncio.gather(*(_fetch(path) for _, _, path in candidates))

    violations: list[dict] = []
    for (section, rel, _path), text in zip(candidates, texts):
        if text is None:
            continue
        count = len(text.splitlines())
        if count > _UNCAPPED_THRESHOLD:
            violations.append(
                {"section": section, "rel": rel, "count": count, "cap": None}
            )
    return violations


async def _scan_branch_literals(
    *,
    rules: dict,
    tree_paths: list[dict],
    token: str,
    owner: str,
    repo: str,
    ref: str,
    project_id: str | None,
) -> list[dict]:
    """Scan backend and agent source files for hardcoded branch-literal patterns.

    ``rules`` is the branch-literal allowlist read AT THE CANDIDATE REF (see
    :func:`_load_branch_literal_rules_at_ref`): ``{"backend":
    {"allowlisted_filenames": [...]}, "agent": {"allowlist": [{path, line,
    pattern, rationale}, ...]}}``. Suppressions come from the candidate ref,
    never from in-process config, so a file whose allowlist entry ships in the
    same commit can clear the gate.

    Mirrors:
      * ``test_invariants.TestHardcodedBranchNames.test_no_hardcoded_main_branch_fallbacks``
        for ``backend/app/*.py`` files.
      * ``test_agent_branch_invariants.TestAgentBranchInvariants.
        test_no_hardcoded_branch_comparison_in_agent_sources`` for specific
        ``agent/src/`` TS files.

    Returns a list of ``{section, rel, line, text}`` violation dicts where
    ``line`` is the 1-based line number and ``text`` is the offending line.
    """
    backend_cfg = rules.get("backend")
    if not isinstance(backend_cfg, dict):
        backend_cfg = {}
    raw_allow = backend_cfg.get("allowlisted_filenames")
    _backend_allow = (
        frozenset(f for f in raw_allow if isinstance(f, str))
        if isinstance(raw_allow, list)
        else frozenset()
    )
    _backend_patterns = [p.regex for p in BACKEND_PATTERNS]

    # --- Collect backend and agent paths to scan ---
    backend_prefix = _SECTION_PREFIXES["backend"]
    backend_entries: list[tuple[str, str]] = []  # (path, rel)
    for entry in tree_paths:
        path = entry["path"]
        if not path.startswith(backend_prefix) or not path.endswith(".py"):
            continue
        rel = path[len(backend_prefix):]
        fname = rel.rsplit("/", 1)[-1] if "/" in rel else rel
        if fname not in _backend_allow:
            backend_entries.append((path, rel))

    agent_cfg = rules.get("agent")
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
    agent_prefix = _SECTION_PREFIXES["agent"]
    _agent_allowset: set[str] = set()
    raw_agent_allow = agent_cfg.get("allowlist")
    if isinstance(raw_agent_allow, list):
        for item in raw_agent_allow:
            if not isinstance(item, dict):
                continue
            a_path = item.get("path")
            a_line = item.get("line")
            a_pattern = item.get("pattern")
            if (
                isinstance(a_path, str)
                and isinstance(a_line, int)
                and isinstance(a_pattern, str)
            ):
                _agent_allowset.add(f"{a_path}::{a_line}::{a_pattern}")
    # Scan exclusion suffixes mirror SURFACE_CONFIGS['agent'].exclude_suffixes
    # (agent/src test/d.ts files are not production sources).
    agent_exclude_suffixes = (".test.ts", ".d.ts")
    agent_entries: list[tuple[str, str]] = []  # (path, rel)
    for entry in tree_paths:
        path = entry["path"]
        if not path.startswith(agent_prefix) or not path.endswith(".ts"):
            continue
        if any(path.endswith(suffix) for suffix in agent_exclude_suffixes):
            continue
        agent_entries.append((path, path[len(agent_prefix):]))

    all_entries = [("backend", path, rel) for path, rel in backend_entries] + [
        ("agent", path, rel) for path, rel in agent_entries
    ]

    async def _fetch(path: str) -> str | None:
        async with _semaphore:
            try:
                return await github.get_file_text_at_ref(
                    path,
                    token=token,
                    owner=owner,
                    repo=repo,
                    ref=ref,
                    project_id=project_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[CI-COMPLIANCE] branch-literal read of %s raised: %s — skipping",
                    path,
                    exc,
                )
                return None

    texts = await asyncio.gather(*(_fetch(path) for _, path, _ in all_entries))

    violations: list[dict] = []
    for (section, _path, rel), text in zip(all_entries, texts):
        if text is None:
            continue
        if section == "backend":
            for lineno, line in enumerate(text.splitlines(), start=1):
                lstripped = line.lstrip()
                if lstripped.startswith("#") or "# noqa" in line:
                    continue
                if any(pat.search(line) for pat in _backend_patterns):
                    violations.append(
                        {"section": "backend", "rel": rel, "line": lineno, "text": line.strip()}
                    )
        else:
            for lineno, line in strip_typescript_with_linenos(text):
                cleaned = _strip_agent_paren_fallbacks(line)
                for pat in AGENT_PATTERNS:
                    if pat.regex.search(cleaned):
                        allow_key = f"{rel}::{lineno}::{pat.name}"
                        if allow_key not in _agent_allowset:
                            violations.append(
                                {
                                    "section": "agent",
                                    "rel": rel,
                                    "line": lineno,
                                    "text": line.strip(),
                                }
                            )
                for pat_name, pat_re in _AGENT_SUPPLEMENTAL_PATTERNS:
                    if pat_re.search(cleaned):
                        allow_key = f"{rel}::{lineno}::{pat_name}"
                        if allow_key not in _agent_allowset:
                            violations.append(
                                {
                                    "section": "agent",
                                    "rel": rel,
                                    "line": lineno,
                                    "text": line.strip(),
                                }
                            )

    return violations


async def _empty_list() -> list:
    return []


async def evaluate_ci_compliance_at_ref(
    *,
    token: str,
    owner: str,
    repo: str,
    ref: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate CI-compliance guardrails for ``owner/repo`` at ``ref``.

    Three guardrail classes are evaluated:

    1. **Capped-file size breach** — for every capped key in
       ``module_size_limits.json``, reads that file at the ref, line-counts it,
       and classifies the breach via
       :func:`testing.check_size_limits.classify_size_record`. Covers the
       ``backend``, ``agent``, ``frontend``, and ``testing`` sections exactly as
       CI's per-file size guardrails do.

    2. **Uncapped large file** — uses the GitHub recursive tree API to enumerate
       ALL source files in backend/app/, agent/src/, admin-ui/src/, and
       backend/testing/. Any file over ``_UNCAPPED_THRESHOLD`` lines that has NO
       cap entry in the limits JSON is reported as a violation (it would fail
       the ``test_*_no_uncapped_large_file`` coverage meta-tests in CI).

    3. **Branch-literal regression** — scans backend/app/*.py for the hardcoded
       branch-fallback patterns from
       ``test_invariants.TestHardcodedBranchNames.test_no_hardcoded_main_branch_fallbacks``
       and specific agent/src/ TS handlers for equality/inequality comparisons
       against bare branch literals (``'main'``, ``'automain'``, ``'master'``).

    Returns a JSON-serializable dict::

        {
            "ref": <sha>,
            "compliant": <bool>,                        # True iff no violations of any kind
            "violations": [{section, rel, count, cap}], # size violations (cap=None for uncapped)
            "branch_literal_violations": [{section, rel, line, text}],
            "checked": <int>,                           # capped files actually read
            "limits_available": <bool>,                 # limits file readable?
            "limits_status": <"ok"|"unreadable"|"malformed">,
            "limits_error": <str|None>,                 # diagnostic when status != ok
            "rules_available": <bool>,                  # branch-literal rules file readable?
            "rules_status": <"ok"|"unreadable"|"malformed">,
            "rules_error": <str|None>,                  # diagnostic when status != ok
            "tree_available": <bool>,                   # repo tree listing available?
        }

    Fail-safe throughout: a source file that 404s / cannot be read at the ref is
    SKIPPED (counted as compliant), never raising. The size-limits FILE and the
    branch-literal RULES file are the deliberate exceptions — they FAIL CLOSED:
    when either config cannot be read or parsed, ``compliant`` is False with a
    diagnostic (``limits_status`` / ``limits_error`` or ``rules_status`` /
    ``rules_error``), because an unreadable config is exactly when the gate is
    least entitled to pass. A legitimately-empty config (a valid ``{}`` that
    declares no rules) still passes. The branch-literal scan runs whenever the
    repo tree is available AND the rules file is readable — even when the
    limits file is unreadable — because CI's branch-literal invariants do not
    depend on it. An unavailable repo tree yields ``tree_available=False`` and
    skips guardrails 2 and 3 (branch-literal, uncapped).
    """
    # Fetch limits, branch-literal rules, and repo tree in parallel — they are
    # independent.
    (limits, limits_status, limits_error), (
        rules,
        rules_status,
        rules_error,
    ), tree_paths_raw = await asyncio.gather(
        _load_size_limits_at_ref(
            token=token, owner=owner, repo=repo, ref=ref, project_id=project_id
        ),
        _load_branch_literal_rules_at_ref(
            token=token, owner=owner, repo=repo, ref=ref, project_id=project_id
        ),
        github.list_repo_tree_paths_at_ref(
            token, owner, repo, ref, project_id=project_id
        ),
    )

    tree_available = tree_paths_raw is not None
    tree_paths: list[dict] = tree_paths_raw if tree_paths_raw is not None else []

    # FAIL CLOSED on an unreadable/malformed limits config OR branch-literal
    # rules. The fail-open predecessor returned compliant=True here — the
    # guardrails silently skipped and the gate reported success, exactly the
    # circumstance in which it is least entitled to pass. When the limits
    # source cannot be read, the size guardrails cannot be verified; when the
    # rules source cannot be read, the branch-literal guardrail cannot know its
    # allowlist — both refuse the verdict. The branch-literal scan is
    # independent of the limits file (CI's branch-literal invariants run
    # regardless of its readability), so its findings are surfaced too when the
    # tree is available AND the rules are readable; the verdict stays
    # non-compliant either way.
    if limits_status != _LIMITS_STATUS_OK or rules_status != _RULES_STATUS_OK:
        branch_literal_violations = (
            await _scan_branch_literals(
                rules=rules,
                tree_paths=tree_paths,
                token=token,
                owner=owner,
                repo=repo,
                ref=ref,
                project_id=project_id,
            )
            if tree_available and rules_status == _RULES_STATUS_OK
            else []
        )
        return {
            "ref": ref,
            "compliant": False,
            "violations": [],
            "branch_literal_violations": branch_literal_violations,
            "checked": 0,
            "limits_available": limits_status == _LIMITS_STATUS_OK,
            "limits_status": limits_status,
            "limits_error": limits_error,
            "rules_available": rules_status == _RULES_STATUS_OK,
            "rules_status": rules_status,
            "rules_error": rules_error,
            "tree_available": tree_available,
        }

    # Run all three guardrails in parallel: capped-file reads (guardrail 1),
    # uncapped large-file reads (guardrail 2), and branch-literal scans
    # (guardrail 3). Guardrails 2 and 3 are no-ops when the tree is unavailable.
    capped_task = _scan_capped_files(
        limits=limits,
        token=token,
        owner=owner,
        repo=repo,
        ref=ref,
        project_id=project_id,
    )
    uncapped_task = (
        _scan_uncapped_files(
            limits=limits,
            tree_paths=tree_paths,
            token=token,
            owner=owner,
            repo=repo,
            ref=ref,
            project_id=project_id,
        )
        if tree_available
        else _empty_list()
    )
    branch_task = (
        _scan_branch_literals(
            rules=rules,
            tree_paths=tree_paths,
            token=token,
            owner=owner,
            repo=repo,
            ref=ref,
            project_id=project_id,
        )
        if tree_available
        else _empty_list()
    )

    (capped_violations, checked), uncapped_violations, branch_literal_violations = (
        await asyncio.gather(capped_task, uncapped_task, branch_task)
    )

    all_size_violations = capped_violations + uncapped_violations
    compliant = not all_size_violations and not branch_literal_violations

    return {
        "ref": ref,
        "compliant": compliant,
        "violations": all_size_violations,
        "branch_literal_violations": branch_literal_violations,
        "checked": checked,
        "limits_available": True,
        "limits_status": _LIMITS_STATUS_OK,
        "limits_error": None,
        "rules_available": True,
        "rules_status": _RULES_STATUS_OK,
        "rules_error": None,
        "tree_available": tree_available,
    }


def record_ci_compliance(project: "Project", result: dict) -> None:
    """Persist ``result`` onto ``project.ci_compliance`` keyed by its commit SHA.

    The SHA key is ``result["ref"]``. A reassignment (not in-place mutation) is
    used so SQLAlchemy flags the JSONB column dirty. The per-SHA map is bounded
    to :data:`_MAX_RECORDED_SHAS`, evicting the oldest entries. A result without
    a ``ref`` is a no-op (nothing to key on). The caller commits the session.
    """
    sha = (result or {}).get("ref")
    if not sha:
        return
    existing = dict(project.ci_compliance or {})
    # Re-recording a SHA should refresh its recency: drop then re-insert so it
    # moves to the end of the (insertion-ordered) eviction queue.
    existing.pop(sha, None)
    existing[sha] = result
    if len(existing) > _MAX_RECORDED_SHAS:
        for stale in list(existing)[: len(existing) - _MAX_RECORDED_SHAS]:
            existing.pop(stale, None)
    project.ci_compliance = existing


def get_ci_compliance(project: "Project", sha: str | None = None) -> Any:
    """Read per-SHA CI-compliance status off ``project``.

    With ``sha`` supplied, returns that SHA's recorded result (or ``None`` if
    absent). With ``sha=None``, returns the full ``{sha: result}`` map (``{}``
    when nothing has been recorded).
    """
    data = project.ci_compliance or {}
    if sha is None:
        return data
    return data.get(sha)


def compact_ci_compliance(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the small strategist/fetch shape for one compliance result."""
    if not isinstance(result, dict):
        return None
    violations = result.get("violations")
    if not isinstance(violations, list):
        violations = []
    branch_literal_violations = result.get("branch_literal_violations")
    if not isinstance(branch_literal_violations, list):
        branch_literal_violations = []
    return {
        "head_sha": result.get("ref"),
        "compliant": bool(result.get("compliant")),
        "violation_count": len(violations),
        "violations": violations,
        "branch_literal_violation_count": len(branch_literal_violations),
        "branch_literal_violations": branch_literal_violations,
        "checked": result.get("checked"),
        "limits_available": result.get("limits_available"),
        "limits_status": result.get("limits_status"),
        "limits_error": result.get("limits_error"),
        "rules_available": result.get("rules_available"),
        "rules_status": result.get("rules_status"),
        "rules_error": result.get("rules_error"),
        "tree_available": result.get("tree_available"),
    }


async def ensure_current_ci_compliance(
    session: "AsyncSession",
    project: "Project",
) -> dict[str, Any]:
    """Resolve the integration-branch HEAD and ensure its compliance is stored.

    The evaluator still reads all repo contents through GitHub at the resolved
    SHA; this helper only coordinates credential resolution, branch-tip lookup,
    per-SHA cache reuse, and JSONB persistence.

    Returns ``{"status": "success", "head_sha": ..., "data": result, ...}`` on
    success. When GitHub credentials or the branch SHA cannot be resolved it
    returns ``status="unavailable"`` instead of raising, so strategic spawning
    can fail open while the fetch endpoint can surface a clear HTTP error.
    """
    from app.release_promotion import _resolve_github_with_session
    from app.shared_state import get_project_integration_branch

    integration_branch = get_project_integration_branch(project)
    resolved = await _resolve_github_with_session(session, project)
    if resolved is None:
        return {
            "status": "unavailable",
            "reason": "github_credentials_unavailable",
            "integration_branch": integration_branch,
            "head_sha": None,
            "data": None,
        }

    token, owner, repo = resolved
    try:
        head_sha = await github.get_branch_sha(
            token,
            owner,
            repo,
            integration_branch,
            project_id=str(project.id),
        )
    except Exception as exc:  # noqa: BLE001 — caller decides how to surface
        logger.warning(
            "[CI-COMPLIANCE] could not resolve %s/%s %s HEAD for project %s: %s",
            owner,
            repo,
            integration_branch,
            project.id,
            exc,
        )
        return {
            "status": "unavailable",
            "reason": "head_sha_unavailable",
            "integration_branch": integration_branch,
            "head_sha": None,
            "data": None,
        }

    stored = get_ci_compliance(project, head_sha)
    computed = False
    if not isinstance(stored, dict):
        flight_key = f"{project.id}:{head_sha}"
        if flight_key in _in_flight_evals:
            # Another coroutine is already computing this SHA — await its result
            # instead of spawning a duplicate evaluation (coalescing).
            try:
                stored = await _in_flight_evals[flight_key]
            except Exception:
                stored = get_ci_compliance(project, head_sha) or {}
        else:
            task: asyncio.Task[dict[str, Any]] = asyncio.create_task(
                evaluate_ci_compliance_at_ref(
                    token=token,
                    owner=owner,
                    repo=repo,
                    ref=head_sha,
                    project_id=str(project.id),
                )
            )
            _in_flight_evals[flight_key] = task
            try:
                stored = await task
            finally:
                _in_flight_evals.pop(flight_key, None)
        record_ci_compliance(project, stored)
        session.add(project)
        await session.flush()
        computed = True

    return {
        "status": "success",
        "integration_branch": integration_branch,
        "head_sha": head_sha,
        "data": stored,
        "computed": computed,
    }


async def get_or_compute_ci_compliance(
    project: "Project",
    session: "AsyncSession",
) -> dict[str, Any] | None:
    """Backward-compatible wrapper for legacy callers."""
    return await ensure_current_ci_compliance(session, project)

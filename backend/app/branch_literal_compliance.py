"""Shared branch-literal compliance scanner.

The scanner is intentionally small and file-system based so test guards and
CI-compliance reporting can share one set of branch-literal patterns without
importing frontend or agent toolchains.

The ALLOWLIST (the enumerated exceptions to the patterns) lives in a
checked-in JSON data file (``backend/testing/branch_literal_allowlist.json``),
NOT in this module. That is a deliberate provenance decision: the CI-compliance
evaluator reads that same file AT THE CANDIDATE REF via GitHub, so a rule
change made in the same commit as the file/entry it describes is observable by
both the local invariant test and the remote gate at once. If the allowlist
were hardcoded in the running process, the deployed backend could not know
about an entry that ships with the file it describes, deadlocking the gate.
This module only LOADS the file (for the local filesystem scanner); the
at-ref reader lives in :mod:`app.ci_compliance`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Literal

Surface = Literal["backend", "agent", "admin-ui"]

# Repo-relative path of the checked-in branch-literal allowlist. The local
# filesystem scanner reads this file from the checkout (see
# ``_allowlist_file_path``) and the CI-compliance evaluator reads it AT THE
# CANDIDATE REF via ``github.get_file_text_at_ref`` — the SAME file is the
# single source of truth for both, so a rule change is one edit observed by
# the local invariant tests and the remote gate together.
BRANCH_LITERAL_ALLOWLIST_PATH = "backend/testing/branch_literal_allowlist.json"


def normalize_source_anchor(source: str) -> str:
    """Normalize a source line for a line-number-independent allowlist key."""
    return " ".join(source.strip().split())


def _allowlist_file_path() -> Path:
    """Resolve the checked-in allowlist file for this checkout.

    The file sits at ``backend/testing/branch_literal_allowlist.json`` relative
    to the repo root. Resolving from the module location (not the caller's
    cwd) works both in a source checkout (``backend/app/..`` → repo root) and
    in the backend Docker image, where the build context is ``backend/`` and
    the file lands at ``/app/testing/...`` (module at ``/app/app/...``).
    ``BLUEPRINT_BRANCH_LITERAL_ALLOWLIST_PATH`` overrides it for forks/tests.
    """
    override = os.getenv("BLUEPRINT_BRANCH_LITERAL_ALLOWLIST_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "testing" / "branch_literal_allowlist.json"


def _load_allowlist_json() -> dict:
    """Read + parse the local allowlist data file, failing loudly on absence.

    The file is checked in, so a missing or malformed file in a context where
    this module is importable is a repo-integrity failure, not a case to scan
    without the allowlist (that would be fail-open). Raising at import makes
    the failure impossible to miss.
    """
    path = _allowlist_file_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"branch-literal allowlist {path} could not be read: {exc}"
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"branch-literal allowlist {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"branch-literal allowlist {path} is a JSON {type(data).__name__}, "
            "not an object"
        )
    return data


def _surface_allowlist_from_json(
    surface: Surface, data: dict
) -> dict[str, object]:
    """Extract one surface's allowlist fields from the allowlist JSON.

    Returns ``{"allowlist": tuple[...], "allowlisted_filenames": frozenset}``.
    Unknown/invalid entries are skipped rather than raising, so a harmless
    extra field does not make the whole file unusable; a structurally broken
    section (non-object) simply yields an empty allowlist for that surface.
    """
    section = data.get(surface)
    if not isinstance(section, dict):
        return {"allowlist": (), "allowlisted_filenames": frozenset()}

    allowlisted_filenames: frozenset[str] = frozenset()
    raw_filenames = section.get("allowlisted_filenames")
    if isinstance(raw_filenames, list):
        allowlisted_filenames = frozenset(
            f for f in raw_filenames if isinstance(f, str)
        )

    allowlist: tuple[BranchLiteralAllowEntry, ...] = ()
    raw_allowlist = section.get("allowlist")
    if isinstance(raw_allowlist, list):
        entries: list[BranchLiteralAllowEntry] = []
        for item in raw_allowlist:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            pattern = item.get("pattern")
            source = item.get("source")
            count = item.get("count")
            if (
                not isinstance(path, str)
                or not isinstance(pattern, str)
                or not isinstance(source, str)
                or not isinstance(count, int)
                or count < 1
            ):
                continue
            entries.append(
                BranchLiteralAllowEntry(
                    path=path,
                    pattern=pattern,
                    source=normalize_source_anchor(source),
                    count=count,
                    rationale=str(item.get("rationale", "")),
                )
            )
        allowlist = tuple(entries)

    return {
        "allowlist": allowlist,
        "allowlisted_filenames": allowlisted_filenames,
    }


@dataclass(frozen=True)
class BranchLiteralPattern:
    name: str
    regex: re.Pattern[str]
    group: str = "branch-decision"


@dataclass(frozen=True)
class BranchLiteralAllowEntry:
    path: str
    pattern: str
    source: str
    count: int
    rationale: str


@dataclass(frozen=True)
class BranchLiteralOffender:
    surface: Surface
    path: str
    line: int
    pattern: str
    snippet: str


@dataclass(frozen=True)
class StaleBranchLiteralAllowEntry:
    surface: Surface
    path: str
    line: int
    pattern: str
    found: str


@dataclass(frozen=True)
class BranchLiteralScanResult:
    surface: Surface
    root: str
    files_scanned: int
    offenders: list[BranchLiteralOffender]
    stale_allowlist: list[StaleBranchLiteralAllowEntry]


@dataclass(frozen=True)
class BranchLiteralSurfaceConfig:
    surface: Surface
    default_root: tuple[str, ...]
    extensions: tuple[str, ...]
    exclude_suffixes: tuple[str, ...]
    patterns: tuple[BranchLiteralPattern, ...]
    allowlist: tuple[BranchLiteralAllowEntry, ...] = ()
    allowlisted_filenames: frozenset[str] = frozenset()
    comment_style: Literal["python", "typescript"] = "typescript"


BACKEND_PATTERNS = (
    BranchLiteralPattern(
        "python-else-main",
        re.compile(r'else\s+["\']main["\']'),
        "fallback",
    ),
    BranchLiteralPattern(
        "python-else-master",
        re.compile(r'else\s+["\']master["\']'),
        "fallback",
    ),
    BranchLiteralPattern(
        "runtime-config-main",
        re.compile(
            r'runtime_config\.get\(["\'](?:integration_branch|promotion_target)["\'],\s*["\']main["\']\)'
        ),
        "fallback",
    ),
    BranchLiteralPattern(
        "assign-branch-main",
        re.compile(r'\bbranch\s*=\s*["\']main["\']'),
        "fallback",
    ),
    BranchLiteralPattern(
        "display-main-range",
        re.compile(r"""(?:title|description|message)\s*=\s*f?['""][^'"]*\bmain\.\.\."""),
        "display",
    ),
    BranchLiteralPattern(
        "display-main-automain",
        re.compile(
            r"""(?:title|description|message)\s*=\s*f?['""][^'"]*\bmain\s*->\s*automain"""
        ),
        "display",
    ),
)

AGENT_PATTERNS = (
    BranchLiteralPattern("equality-main", re.compile(r"(?:===|!==)\s*['\"](?:main|automain)['\"]")),
    BranchLiteralPattern(
        "assign-branch",
        re.compile(r"\b(?:branch|baseBranch|baseRef|base_branch)\s*=(?!=)\s*['\"](?:main|automain)['\"]"),
    ),
    BranchLiteralPattern(
        "object-baseBranch",
        re.compile(r"\b(?:branch|baseBranch|base_branch|baseRef)\s*:\s*['\"](?:main|automain)['\"]"),
    ),
    BranchLiteralPattern("fallback-main", re.compile(r"(?:\|\||\?\?)\s*['\"](?:main|automain)['\"]")),
    BranchLiteralPattern("fallback-blueprint", re.compile(r"(?:\|\||\?\?)\s*['\"]blueprint['\"]")),
)

ADMIN_UI_PATTERNS = (
    BranchLiteralPattern("equality-main", re.compile(r"(?:===|!==)\s*['\"](?:main|automain)['\"]")),
    BranchLiteralPattern(
        "assign-branch",
        re.compile(
            r"\b(?:branch|baseBranch|baseRef|base_branch|integrationBranch|integration_branch|promotionTarget|promotion_target)\s*=(?!=)\s*['\"](?:main|automain)['\"]"
        ),
    ),
    BranchLiteralPattern(
        "object-baseBranch",
        re.compile(
            r"\b(?:branch|baseBranch|base_branch|baseRef|integrationBranch|integration_branch|promotionTarget|promotion_target)\s*:\s*['\"](?:main|automain)['\"]"
        ),
    ),
    BranchLiteralPattern("fallback-main", re.compile(r"(?:\|\||\?\?)\s*['\"](?:main|automain)['\"]")),
    BranchLiteralPattern("fallback-blueprint", re.compile(r"(?:\|\||\?\?)\s*['\"]blueprint['\"]")),
)

# The allowlist fields (``allowlisted_filenames`` / ``allowlist``) come from
# the checked-in data file at module load — the single source of truth shared
# with the CI-compliance evaluator's at-ref reader. A missing/malformed file
# raises here (see ``_load_allowlist_json``) rather than scanning without the
# allowlist.
_allow_data = _load_allowlist_json()

SURFACE_CONFIGS: dict[Surface, BranchLiteralSurfaceConfig] = {
    "backend": BranchLiteralSurfaceConfig(
        surface="backend",
        default_root=("backend", "app"),
        extensions=(".py",),
        exclude_suffixes=(),
        patterns=BACKEND_PATTERNS,
        allowlisted_filenames=_surface_allowlist_from_json("backend", _allow_data)[
            "allowlisted_filenames"
        ],
        comment_style="python",
    ),
    "agent": BranchLiteralSurfaceConfig(
        surface="agent",
        default_root=("agent", "src"),
        extensions=(".ts",),
        exclude_suffixes=(".test.ts", ".d.ts"),
        patterns=AGENT_PATTERNS,
        allowlist=_surface_allowlist_from_json("agent", _allow_data)["allowlist"],
    ),
    "admin-ui": BranchLiteralSurfaceConfig(
        surface="admin-ui",
        default_root=("admin-ui", "src"),
        extensions=(".ts", ".tsx"),
        exclude_suffixes=(".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx", ".d.ts"),
        patterns=ADMIN_UI_PATTERNS,
        allowlist=_surface_allowlist_from_json("admin-ui", _allow_data)["allowlist"],
    ),
}


# ---------------------------------------------------------------------------
# CI-compliance agent scan constants — single source of truth for the
# ci_compliance evaluator's agent branch-literal check. These differ from the
# shared AGENT_PATTERNS intentionally: the CI path enforces a broader rule
# (== and != in addition to === and !==, plus 'master' in addition to 'main'
# and 'automain') and strips parenthesized fallbacks rather than flagging them.
# ---------------------------------------------------------------------------

# Agent TS files scanned by the CI-compliance evaluator. Paths relative to
# agent/src/.  Mirrors _TARGET_RELATIVE in test_agent_branch_invariants.py.
AGENT_SOURCE_FILES: tuple[str, ...] = (
    "handlers/review-fix.ts",
    "handlers/specialized.ts",
    "handlers/standard.ts",
    "handlers/code-change.ts",
    "prompt.ts",
)

# Equality/inequality comparison against a bare branch literal — broader than
# the shared equality-main pattern (includes == / != and 'master').
CI_AGENT_FORBIDDEN_RE: re.Pattern[str] = re.compile(
    r"""(===|!==|==|!=)\s*['"](main|automain|master)['"]"""
)

# Parenthesized fallback expressions stripped BEFORE the forbidden regex runs
# so ``(promotionTarget ?? 'main')`` is allowed rather than flagged.
CI_AGENT_PAREN_FALLBACK_RE: re.Pattern[str] = re.compile(
    r"""\(\s*[^()]*?\s*(\?\?|\|\|)\s*['"](?:main|automain|master)['"]\s*\)"""
)


def strip_typescript_with_linenos(src: str) -> list[tuple[int, str]]:
    """Return ``(1-based lineno, text)`` pairs with TS/JS comments removed.

    Handles ``//`` line comments and ``/* ... */`` block comments.  Used by
    the CI-compliance evaluator to scan GitHub-fetched TS file text.
    Mirrors ``_strip_comments`` in ``test_agent_branch_invariants.py``.
    """
    result: list[tuple[int, str]] = []
    in_block = False
    for i, line in enumerate(src.splitlines(), start=1):
        stripped = line
        if in_block:
            end = stripped.find("*/")
            if end == -1:
                continue
            stripped = stripped[end + 2 :]
            in_block = False
        while True:
            start_idx = stripped.find("/*")
            if start_idx == -1:
                break
            end_idx = stripped.find("*/", start_idx + 2)
            if end_idx == -1:
                stripped = stripped[:start_idx]
                in_block = True
                break
            stripped = stripped[:start_idx] + stripped[end_idx + 2 :]
        slash_idx = stripped.find("//")
        if slash_idx != -1:
            stripped = stripped[:slash_idx]
        lstripped = stripped.lstrip()
        if not lstripped:
            continue
        if lstripped.startswith("*"):
            continue
        result.append((i, stripped))
    return result


def _iter_source_files(root: Path, config: BranchLiteralSurfaceConfig) -> Iterable[Path]:
    for current_root, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {"node_modules", "__pycache__"}]
        for filename in files:
            if config.allowlisted_filenames and filename in config.allowlisted_filenames:
                continue
            if not filename.endswith(config.extensions):
                continue
            if any(filename.endswith(suffix) for suffix in config.exclude_suffixes):
                continue
            yield Path(current_root) / filename


def _strip_python_line(line: str) -> str:
    if line.lstrip().startswith("#") or "# noqa" in line:
        return ""
    return line


def _strip_typescript_lines(lines: list[str]) -> list[str]:
    stripped: list[str] = []
    in_block_comment = False
    for raw_line in lines:
        line = raw_line
        if in_block_comment:
            end_idx = line.find("*/")
            if end_idx < 0:
                stripped.append("")
                continue
            line = line[end_idx + 2 :]
            in_block_comment = False

        line = re.sub(r"/\*.*?\*/", "", line)
        open_idx = line.find("/*")
        if open_idx >= 0:
            line = line[:open_idx]
            in_block_comment = True

        line_comment_idx = line.find("//")
        if line_comment_idx >= 0:
            line = line[:line_comment_idx]
        stripped.append(line)
    return stripped


def _allow_key(path: str, pattern: str, source: str) -> tuple[str, str, str]:
    return path, pattern, normalize_source_anchor(source)


def _scanned_lines(
    file_path: Path, config: BranchLiteralSurfaceConfig
) -> list[tuple[int, str]]:
    raw_lines = file_path.read_text(encoding="utf-8").split("\n")
    lines = (
        [_strip_python_line(line) for line in raw_lines]
        if config.comment_style == "python"
        else _strip_typescript_lines(raw_lines)
    )
    return [
        (index, line)
        for index, line in enumerate(lines, start=1)
        if line.strip()
    ]


def scan_branch_literal_surface(
    surface: Surface,
    root: str | Path,
) -> BranchLiteralScanResult:
    config = SURFACE_CONFIGS[surface]
    root_path = Path(root)
    allow_counts = {
        _allow_key(entry.path, entry.pattern, entry.source): entry.count
        for entry in config.allowlist
    }
    observed_counts: Counter[tuple[str, str, str]] = Counter()
    offenders: list[BranchLiteralOffender] = []
    files_scanned = 0

    for file_path in _iter_source_files(root_path, config):
        files_scanned += 1
        rel = file_path.relative_to(root_path).as_posix()
        for index, line in _scanned_lines(file_path, config):
            for pattern in config.patterns:
                if pattern.regex.search(line):
                    allow_key = _allow_key(rel, pattern.name, line)
                    observed_counts[allow_key] += 1
                    offender = BranchLiteralOffender(
                        surface=surface,
                        path=rel,
                        line=index,
                        pattern=pattern.name,
                        snippet=line.strip()[:160],
                    )
                    if observed_counts[allow_key] > allow_counts.get(allow_key, 0):
                        offenders.append(offender)

    stale_allowlist = _find_stale_allowlist_entries(surface, root_path, config)
    return BranchLiteralScanResult(
        surface=surface,
        root=str(root_path),
        files_scanned=files_scanned,
        offenders=offenders,
        stale_allowlist=stale_allowlist,
    )


def _find_stale_allowlist_entries(
    surface: Surface,
    root: Path,
    config: BranchLiteralSurfaceConfig,
) -> list[StaleBranchLiteralAllowEntry]:
    stale: list[StaleBranchLiteralAllowEntry] = []
    patterns_by_name = {pattern.name: pattern for pattern in config.patterns}
    for entry in config.allowlist:
        file_path = root / entry.path
        try:
            scanned_lines = _scanned_lines(file_path, config)
        except FileNotFoundError:
            scanned_lines = []
        pattern = patterns_by_name.get(entry.pattern)
        matches = [
            (line_number, line)
            for line_number, line in scanned_lines
            if pattern is not None
            and pattern.regex.search(line)
            and normalize_source_anchor(line) == entry.source
        ]
        if len(matches) != entry.count:
            first_line = matches[0][0] if matches else 0
            found = (
                f"{len(matches)} occurrence(s); "
                f"{normalize_source_anchor(matches[0][1]) if matches else ''}"
            )
            stale.append(
                StaleBranchLiteralAllowEntry(
                    surface=surface,
                    path=entry.path,
                    line=first_line,
                    pattern=entry.pattern,
                    found=found[:120],
                )
            )
    return stale


def scan_branch_literal_compliance(
    repo_root: str | Path,
    *,
    surfaces: Iterable[Surface] = ("backend", "agent", "admin-ui"),
) -> list[BranchLiteralScanResult]:
    root = Path(repo_root)
    results: list[BranchLiteralScanResult] = []
    for surface in surfaces:
        config = SURFACE_CONFIGS[surface]
        results.append(scan_branch_literal_surface(surface, root.joinpath(*config.default_root)))
    return results


def branch_literal_results_to_dicts(
    results: Iterable[BranchLiteralScanResult],
) -> list[dict[str, object]]:
    return [
        {
            **asdict(result),
            "offenders": [asdict(offender) for offender in result.offenders],
            "stale_allowlist": [asdict(entry) for entry in result.stale_allowlist],
        }
        for result in results
    ]


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", choices=[*SURFACE_CONFIGS.keys(), "all"], default="all")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    root = Path(args.root)
    if args.surface == "all":
        results = scan_branch_literal_compliance(root)
    else:
        results = [scan_branch_literal_surface(args.surface, root)]

    if args.json:
        print(json.dumps(branch_literal_results_to_dicts(results), indent=2, sort_keys=True))
        return 0

    offenders = [offender for result in results for offender in result.offenders]
    stale = [entry for result in results for entry in result.stale_allowlist]
    for offender in offenders:
        print(
            f"{offender.surface}:{offender.path}:{offender.line} "
            f"[{offender.pattern}]: {offender.snippet}"
        )
    for entry in stale:
        print(
            f"stale allowlist {entry.surface}:{entry.path}:{entry.line} "
            f"[{entry.pattern}]: {entry.found}"
        )
    return 1 if offenders or stale else 0


if __name__ == "__main__":
    raise SystemExit(_main())

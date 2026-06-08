#!/usr/bin/env python3
"""Fast, dependency-free module size-guardrail checker.

Mirrors the line-count guardrail enforced by ``backend/testing/test_invariants.py``
(``module_size_limits.json``) so an agent can surface and remediate size breaches
*before* opening a PR — without spinning up Docker, a DB, or the full test suite.

Two modes:

* check mode (default): line-count every file listed in the scoped section(s) and
  print each breach (path, current lines, cap). Exits non-zero if any file exceeds
  its cap, zero when clean.
* ``--fix`` mode: for every breached file, rewrite *only* that file's cap in the
  tracked JSON to the canonical ``bump_size_limit`` policy value; AND for every
  uncapped source file over ``_COVERAGE_THRESHOLD`` lines (the complementary
  coverage guardrail the ``test_*_no_uncapped_large_file`` meta-tests enforce),
  ADD a new cap entry sized at the same policy headroom. Both paths preserve key
  order and the leading ``_doc`` field so the diff stays minimal. After ``--fix``
  re-running check mode passes. The bump stays policy-consistent so the guardrail
  still catches genuine runaway growth.

Usage (from the repo root, no install step)::

    python3 backend/testing/check_size_limits.py                 # check all sections
    python3 backend/testing/check_size_limits.py --sections backend
    python3 backend/testing/check_size_limits.py --fix           # bump breached caps

Stdlib only. The three sections resolve their source roots exactly the way
``test_invariants.py`` does: backend -> ``backend/app``, agent -> ``agent/src``,
frontend -> ``admin-ui/src`` (honoring the ``ADMIN_UI_SRC`` override and the
Docker ``/admin-ui/src`` mount). A section whose source tree is absent in the
current environment is reported as skipped rather than treated as an error.
"""

import argparse
import json
import os
import sys

# Make ``app.size_limit_policy`` importable when run as
# ``python3 backend/testing/check_size_limits.py`` from the repo root: add the
# backend dir (this file's parent's parent) to sys.path. The policy itself is
# imported lazily (in ``_bump_size_limit``) so this path setup runs first.
_BACKEND_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


def _bump_size_limit(current_size: int) -> int:
    """Canonical bump policy, shared with the server endpoint.

    Imported lazily so the ``sys.path`` setup above runs before the import and
    the module stays runnable as a bare script from the repo root."""
    from app.size_limit_policy import bump_size_limit

    return bump_size_limit(current_size)


# Repo root is two levels up from this file (backend/testing -> repo root).
_DEFAULT_REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
_DEFAULT_JSON_PATH = os.path.join(os.path.dirname(__file__), "module_size_limits.json")

# Section order is significant: matches the JSON's section order so output and
# --sections=all iteration are deterministic.
SECTIONS = ("backend", "agent", "frontend")

# Coverage threshold for the complementary guardrail: a source file larger than
# this with NO cap entry at all is invisible to the per-file caps and can grow
# without limit. Mirrors ``test_invariants._COVERAGE_THRESHOLD`` (and the agent /
# frontend coverage meta-tests). Kept in sync by a guard test in
# test_size_limits_checker.py. NEVER mirror this by importing test_invariants —
# that module pulls in pytest/app machinery and would break the stdlib-only,
# dependency-free contract this checker guarantees.
_COVERAGE_THRESHOLD = 1500


def _is_frontend_excluded(rel_path: str) -> bool:
    """Mirror test_invariants._is_frontend_excluded: skip frontend test/type files."""
    return rel_path.endswith((".test.ts", ".test.tsx", ".d.ts")) or rel_path.startswith(
        "test/"
    )


def _is_agent_excluded(rel_path: str) -> bool:
    """Mirror test_invariants._is_agent_excluded: skip agent test/type files."""
    return rel_path.endswith((".test.ts", ".d.ts"))


def _no_exclusion(_rel_path: str) -> bool:
    return False


# Per-section scan rules for the uncapped-file coverage guardrail. Mirrors the
# suffix tuples and exclusion predicates the coverage meta-tests in
# test_invariants.py use, so --fix adds the exact caps those tests demand.
_SECTION_SCAN = {
    "backend": ((".py",), _no_exclusion),
    "agent": ((".ts",), _is_agent_excluded),
    "frontend": ((".ts", ".tsx"), _is_frontend_excluded),
}


def resolve_section_root(
    section: str, repo_root: str, *, use_env_overrides: bool = True
) -> str | None:
    """Resolve a section's source tree root, mirroring test_invariants.py.

    Returns the directory path if it exists, else None (caller skips the
    section). frontend honors the ``ADMIN_UI_SRC`` override and the Docker
    ``/admin-ui/src`` mount before falling back to ``<repo>/admin-ui/src``.

    ``use_env_overrides`` gates the environment-specific frontend escape
    hatches (the ``ADMIN_UI_SRC`` env var and the global ``/admin-ui/src``
    Docker mount). They make sense only for the *real* run rooted at the
    default repo root; when an explicit ``--repo-root`` is supplied (e.g. a
    hermetic test fixture) resolution must stay strictly under that root so a
    stray global mount does not leak the host environment into the result.
    """
    if section == "backend":
        root = os.path.join(repo_root, "backend", "app")
        return root if os.path.isdir(root) else None
    if section == "agent":
        root = os.path.join(repo_root, "agent", "src")
        return root if os.path.isdir(root) else None
    if section == "frontend":
        candidates = []
        if use_env_overrides:
            env_override = os.getenv("ADMIN_UI_SRC")
            if env_override:
                candidates.append(env_override)
            candidates.append("/admin-ui/src")
        candidates.append(os.path.join(repo_root, "admin-ui", "src"))
        for candidate in candidates:
            if os.path.isdir(candidate):
                return candidate
        return None
    raise ValueError(f"Unknown section: {section}")


def count_lines(path: str) -> int:
    """Line-count a file the same way test_invariants.py does.

    The line count is encoding-invariant (only the newline byte matters), so
    ``errors="replace"`` yields the identical count to test_invariants.py's
    default ``open()`` for our utf-8 sources. ``errors="replace"`` (not a bare
    utf-8 pin) is deliberate: this checker runs on EVERY ./check now, so a stray
    non-utf-8 file must NOT raise UnicodeDecodeError and hard-fail every agent's
    gate fleet-wide — it degrades (still counts lines correctly) and any real
    encoding problem still surfaces via the actual test suite.
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        return len(f.readlines())


def scan_section(caps: dict, root: str) -> list[dict]:
    """Line-count every file in ``caps`` under ``root``.

    Returns one record per file: ``{rel, path, count, cap, breach}``.
    A file missing from disk is recorded with ``count=None`` and
    ``breach=False`` (a missing file can't have grown past its cap).
    """
    records = []
    for rel in caps:
        cap = caps[rel]
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            records.append(
                {"rel": rel, "path": path, "count": None, "cap": cap, "breach": False}
            )
            continue
        count = count_lines(path)
        records.append(
            {
                "rel": rel,
                "path": path,
                "count": count,
                "cap": cap,
                "breach": count > cap,
            }
        )
    return records


def scan_uncapped_section(
    root: str, caps: dict, suffixes: tuple, is_excluded
) -> list[dict]:
    """Walk ``root`` for source files over ``_COVERAGE_THRESHOLD`` lines that have
    NO cap entry in ``caps``.

    Mirrors ``test_invariants._scan_uncapped``: collects every file whose name
    ends in one of ``suffixes``, is not skipped by ``is_excluded``, exceeds the
    coverage threshold, and is absent from ``caps``. Returns one record per file
    (``{rel, path, count}``) sorted largest-first. Tree-relative paths use forward
    slashes to match the JSON key convention.
    """
    offenders = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(suffixes):
                continue
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            if is_excluded(rel) or rel in caps:
                continue
            count = count_lines(abs_path)
            if count > _COVERAGE_THRESHOLD:
                offenders.append({"rel": rel, "path": abs_path, "count": count})
    offenders.sort(key=lambda rec: rec["count"], reverse=True)
    return offenders


def load_limits(json_path: str) -> dict:
    """Load the size-limits JSON, preserving key order (dicts are ordered)."""
    with open(json_path) as f:
        return json.load(f)


def write_limits(json_path: str, data: dict) -> None:
    """Write the size-limits JSON back, preserving structure and key order.

    Uses the same formatting as the tracked file (2-space indent, ASCII-escaped,
    trailing newline) so a --fix diff shows only the changed cap numbers.
    """
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=True)
        f.write("\n")


def evaluate(
    data: dict, sections: list[str], repo_root: str, *, use_env_overrides: bool = True
) -> tuple[dict[str, list[dict]], list[str]]:
    """Evaluate the scoped sections.

    Returns ``(checked, skipped)`` where ``checked`` maps section -> list of
    file records (from scan_section) and ``skipped`` is the list of section
    names whose source tree was absent. ``use_env_overrides`` is forwarded to
    ``resolve_section_root`` (see its docstring).
    """
    checked: dict[str, list[dict]] = {}
    skipped: list[str] = []
    for section in sections:
        caps = data.get(section) or {}
        root = resolve_section_root(
            section, repo_root, use_env_overrides=use_env_overrides
        )
        if root is None:
            skipped.append(section)
            continue
        checked[section] = scan_section(caps, root)
    return checked, skipped


def _parse_sections(raw: str) -> list[str]:
    if raw.strip().lower() == "all":
        return list(SECTIONS)
    requested = [s.strip() for s in raw.split(",") if s.strip()]
    invalid = [s for s in requested if s not in SECTIONS]
    if invalid:
        raise argparse.ArgumentTypeError(
            f"unknown section(s): {', '.join(invalid)}; choose from {', '.join(SECTIONS)}"
        )
    # Preserve canonical order regardless of how the user ordered them.
    return [s for s in SECTIONS if s in requested]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check (and optionally --fix) module size-limit guardrails."
    )
    parser.add_argument(
        "--sections",
        type=_parse_sections,
        default=list(SECTIONS),
        help="comma-separated subset of {backend,agent,frontend} (default: all)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="bump the cap of each breached file to the canonical policy value",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default=_DEFAULT_JSON_PATH,
        help="path to module_size_limits.json (default: alongside this script)",
    )
    parser.add_argument(
        "--repo-root",
        dest="repo_root",
        default=_DEFAULT_REPO_ROOT,
        help="repo root used to resolve section source trees (default: inferred)",
    )
    args = parser.parse_args(argv)

    data = load_limits(args.json_path)
    # The environment-specific frontend escape hatches (ADMIN_UI_SRC /
    # /admin-ui/src) apply only to the real run rooted at the default repo
    # root. An explicit --repo-root means hermetic resolution under that root.
    use_env_overrides = os.path.normpath(args.repo_root) == _DEFAULT_REPO_ROOT
    checked, skipped = evaluate(
        data, args.sections, args.repo_root, use_env_overrides=use_env_overrides
    )

    for section in skipped:
        print(f"[skip] {section}: source tree not present in this environment")

    breaches = [
        (section, rec)
        for section in args.sections
        for rec in checked.get(section, [])
        if rec["breach"]
    ]

    # Complementary guardrail: uncapped source files over the coverage threshold.
    # A capped-but-breached file is handled by ``breaches`` above; a file with no
    # cap at all is invisible to that scan, so walk each scoped section's tree.
    uncapped: list[tuple[str, dict]] = []
    for section in args.sections:
        if section in skipped:
            continue
        root = resolve_section_root(section, args.repo_root)
        if root is None:
            continue
        suffixes, is_excluded = _SECTION_SCAN[section]
        caps = data.get(section) or {}
        for rec in scan_uncapped_section(root, caps, suffixes, is_excluded):
            uncapped.append((section, rec))

    if args.fix:
        if not breaches and not uncapped:
            print("No breaches to fix — all scoped files within cap.")
            return 0
        for section, rec in breaches:
            new_cap = _bump_size_limit(rec["count"])
            data[section][rec["rel"]] = new_cap
            print(
                f"[fix]  {section}/{rec['rel']}: {rec['count']} lines — "
                f"cap {rec['cap']} -> {new_cap}"
            )
        for section, rec in uncapped:
            new_cap = _bump_size_limit(rec["count"])
            data.setdefault(section, {})[rec["rel"]] = new_cap
            print(
                f"[add]  {section}/{rec['rel']}: {rec['count']} lines — "
                f"added cap {new_cap}"
            )
        write_limits(args.json_path, data)
        actions = []
        if breaches:
            actions.append(f"Bumped {len(breaches)} cap(s)")
        if uncapped:
            actions.append(f"Added {len(uncapped)} cap(s)")
        print(f"{'; '.join(actions)} in {args.json_path}.")
        return 0

    exit_code = 0
    if breaches:
        for section, rec in breaches:
            print(
                f"[breach] {section}/{rec['rel']}: {rec['count']} lines "
                f"(cap: {rec['cap']})"
            )
        print(
            f"\n{len(breaches)} size breach(es). Run with --fix to bump the "
            f"breached caps to the canonical policy value."
        )
        exit_code = 1

    if uncapped:
        for section, rec in uncapped:
            print(
                f"[uncapped] {section}/{rec['rel']}: {rec['count']} lines "
                f"(threshold: {_COVERAGE_THRESHOLD}) — NO cap entry"
            )
        print(
            f"\n{len(uncapped)} uncapped file(s) over {_COVERAGE_THRESHOLD} lines "
            f"violate the size guardrail (no cap entry). Run with --fix to add a "
            f"cap entry (current size + policy headroom)."
        )
        exit_code = 1

    if exit_code == 0:
        print("All scoped files within their size caps.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

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
  tracked JSON to the canonical ``bump_size_limit`` policy value, preserving key
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


def resolve_section_root(section: str, repo_root: str) -> str | None:
    """Resolve a section's source tree root, mirroring test_invariants.py.

    Returns the directory path if it exists, else None (caller skips the
    section). frontend honors the ``ADMIN_UI_SRC`` override and the Docker
    ``/admin-ui/src`` mount before falling back to ``<repo>/admin-ui/src``.
    """
    if section == "backend":
        root = os.path.join(repo_root, "backend", "app")
        return root if os.path.isdir(root) else None
    if section == "agent":
        root = os.path.join(repo_root, "agent", "src")
        return root if os.path.isdir(root) else None
    if section == "frontend":
        candidates = []
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
    """Line-count a file the same way test_invariants.py does."""
    with open(path) as f:
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
    data: dict, sections: list[str], repo_root: str
) -> tuple[dict[str, list[dict]], list[str]]:
    """Evaluate the scoped sections.

    Returns ``(checked, skipped)`` where ``checked`` maps section -> list of
    file records (from scan_section) and ``skipped`` is the list of section
    names whose source tree was absent.
    """
    checked: dict[str, list[dict]] = {}
    skipped: list[str] = []
    for section in sections:
        caps = data.get(section) or {}
        root = resolve_section_root(section, repo_root)
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
    checked, skipped = evaluate(data, args.sections, args.repo_root)

    for section in skipped:
        print(f"[skip] {section}: source tree not present in this environment")

    breaches = [
        (section, rec)
        for section in args.sections
        for rec in checked.get(section, [])
        if rec["breach"]
    ]

    if args.fix:
        if not breaches:
            print("No breaches to fix — all scoped files within cap.")
            return 0
        for section, rec in breaches:
            new_cap = _bump_size_limit(rec["count"])
            data[section][rec["rel"]] = new_cap
            print(
                f"[fix]  {section}/{rec['rel']}: {rec['count']} lines — "
                f"cap {rec['cap']} -> {new_cap}"
            )
        write_limits(args.json_path, data)
        print(f"Bumped {len(breaches)} cap(s) in {args.json_path}.")
        return 0

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
        return 1

    print("All scoped files within their size caps.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Reverse-dependency resolver: changed repo paths -> impacted backend test files.

Fast, dependency-free (stdlib only), no DB / Docker / network — sub-second. It is
invoked BY REFERENCE from the merge-time gate the agent generates in
``agent/src/prompts/check-helper.ts`` (see CLAUDE.md's "Generating Code for One
Language From Another"): the generated ``./check`` pipes the changed-file list
into this module and runs the emitted backend test files under the sanctioned
Postgres harness. It is a plain module (no ``test_`` prefix) so pytest never
collects it.

Why it exists: the merge-time backend gate otherwise runs only the ~10-file
``--standalone --no-postgres`` smoke set. Test files that exercise the CHANGED
modules — by importing ``app.<module>`` or by mock-patching ``app.<module>...`` —
and even test files the PR itself ADDS, never run mechanically before merge, so a
contract break merges green on the task PR and only surfaces at the promotion-PR
full matrix, burning a long diagnosis+re-provision round-trip.

Contract
--------
* Changed repo-relative paths are read from STDIN, one per line (blank lines
  ignored). Positional CLI args, when given, are treated as additional changed
  paths — handy for direct unit tests. ``--repo-root`` overrides the inferred
  root (the repo root is two levels up from this file).
* The impacted set is:
    - every directly changed/added ``backend/testing/test_*.py`` that exists on
      disk (this is what makes a PR's OWN new tests run), PLUS
    - for each changed ``backend/app/<...>.py``, every ``backend/testing/
      test_*.py`` that references that module. Both import forms
      (``import app.foo``, ``from app.foo import x``, ``from app import foo``,
      ``from app.routers import agent``) and string mock-patch targets
      (``patch("app.foo.bar")``, ``mocker.patch('app.foo...')``,
      ``patch.object(app.foo, ...)``) are detected. Matching respects module
      boundaries: ``app.foo`` must NOT match a longer sibling ``app.foobar``.
* Emitted deduped + stable-sorted to STDOUT, one repo-relative path per line.
  Empty set -> no output, exit 0. Optional ``--limit N`` truncates to the first N
  (sorted); the overflow is reported to STDERR as ``deferred-to-CI:`` lines
  (never silently dropped).
* Robust on partial trees: any listed path absent on disk is skipped (deletions),
  and the reverse-dep scan is skipped gracefully when ``backend/testing`` is
  absent.
"""

import argparse
import os
import re
import sys

# Repo root is two levels up from this file (backend/testing -> repo root).
_DEFAULT_REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)

# A changed backend app module: capture the dotted-path remainder after
# ``backend/app/`` so ``backend/app/routers/agent.py`` -> ``routers/agent``.
_APP_PY_RE = re.compile(r"^backend/app/(.+)\.py$")
# A backend test file, by the pytest ``test_`` collection convention.
_TEST_PY_RE = re.compile(r"^backend/testing/test_[A-Za-z0-9_]+\.py$")


def app_module_for_path(repo_rel: str) -> "str | None":
    """Return the dotted module path for a ``backend/app/<...>.py`` path, else None.

    ``backend/app/routers/agent.py`` -> ``app.routers.agent``;
    ``backend/app/foo.py`` -> ``app.foo``. Non-app / non-``.py`` paths -> None.
    """
    m = _APP_PY_RE.match(repo_rel.strip())
    if not m:
        return None
    return "app." + m.group(1).replace("/", ".")


def module_reference_patterns(dotted: str) -> "list[re.Pattern]":
    """Compiled regexes that match a reference to ``dotted`` in test source.

    Two forms are covered:

    * The full dotted path with an identifier boundary on each end — catches
      ``import app.x.y``, ``from app.x.y import z``, ``patch("app.x.y.z")`` and
      ``patch.object(app.x.y, ...)``. The trailing boundary excludes a longer
      sibling (``app.foo`` must not match ``app.foobar``); the leading boundary
      (which also forbids a preceding ``.``) avoids matching a same-named
      attribute of an unrelated package (``otherpkg.app.foo``). A trailing ``.``
      is *allowed* so ``app.foo`` still matches ``app.foo.bar`` in a patch target.
    * ``from <parent> import <name>`` — this form never spells the dotted path
      contiguously (e.g. module ``app.routers.agent`` referenced as
      ``from app.routers import agent``), so it needs its own pattern with a
      boundary around the imported ``<name>``.
    """
    parts = dotted.split(".")
    esc = re.escape(dotted)
    pats = [
        re.compile(r"(?<![A-Za-z0-9_.])" + esc + r"(?![A-Za-z0-9_])"),
    ]
    if len(parts) >= 2:
        parent = re.escape(".".join(parts[:-1]))
        name = re.escape(parts[-1])
        pats.append(
            re.compile(
                r"from\s+"
                + parent
                + r"\s+import\s+[^\n]*?(?<![A-Za-z0-9_])"
                + name
                + r"(?![A-Za-z0-9_])"
            )
        )
    return pats


def resolve_impacted(changed_paths, repo_root: str) -> "list[str]":
    """Compute the impacted ``backend/testing/test_*.py`` set (sorted, deduped).

    ``changed_paths`` is any iterable of repo-relative paths. ``repo_root`` roots
    the on-disk existence checks and the reverse-dep scan.
    """
    testing_dir = os.path.join(repo_root, "backend", "testing")
    impacted: "set[str]" = set()
    changed_modules: "list[str]" = []

    for raw in changed_paths:
        path = raw.strip()
        if not path:
            continue
        if _TEST_PY_RE.match(path):
            # A directly changed/added test file — include it (if it exists on
            # disk, so a deletion doesn't select a vanished file).
            if os.path.isfile(os.path.join(repo_root, path)):
                impacted.add(path)
            continue
        dotted = app_module_for_path(path)
        if dotted is not None:
            changed_modules.append(dotted)

    if changed_modules and os.path.isdir(testing_dir):
        pattern_sets = [module_reference_patterns(mod) for mod in changed_modules]
        for name in os.listdir(testing_dir):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            abs_path = os.path.join(testing_dir, name)
            if not os.path.isfile(abs_path):
                continue
            try:
                with open(abs_path, encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError:
                # Unreadable file: skip rather than crash the whole gate.
                continue
            for pats in pattern_sets:
                if any(pat.search(content) for pat in pats):
                    impacted.add("backend/testing/" + name)
                    break

    return sorted(impacted)


def _read_changed(paths_argv, stdin) -> "list[str]":
    """Positional args if any, else the newline-delimited STDIN payload."""
    if paths_argv:
        return list(paths_argv)
    # Only read stdin when it isn't an interactive terminal, so an accidental
    # bare invocation doesn't hang waiting for input.
    if getattr(stdin, "isatty", lambda: False)():
        return []
    return stdin.read().splitlines()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve the backend test files impacted by a set of changed repo "
            "paths (reverse-dependency scan). Changed paths on STDIN (one per "
            "line) or as positional args; impacted test files to STDOUT."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="changed repo-relative paths (default: read newline-delimited STDIN)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap the emitted count; overflow is logged to STDERR (never dropped)",
    )
    parser.add_argument(
        "--repo-root",
        dest="repo_root",
        default=None,
        help="repo root used to resolve on-disk paths (default: inferred)",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root if args.repo_root is not None else _DEFAULT_REPO_ROOT
    changed = _read_changed(args.paths, sys.stdin)
    impacted = resolve_impacted(changed, repo_root)

    if args.limit is not None and args.limit >= 0 and len(impacted) > args.limit:
        overflow = impacted[args.limit :]
        impacted = impacted[: args.limit]
        for rel in overflow:
            print(f"deferred-to-CI: {rel}", file=sys.stderr)

    for rel in impacted:
        print(rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())

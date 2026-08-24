"""Shared test-file inventory rules for the estate queue and freshness guards.

This module owns path discovery only.  Test identities remain canonical in
``identity_extraction`` so inventory consumers cannot quietly grow different
collector rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


def _suite_root(workspace_root: Path, suite: str) -> Path:
    """Return the filesystem root for a logical Vitest suite."""
    if suite == "admin-ui-viewport":
        return workspace_root / "admin-ui"
    if suite in {"agent", "admin-ui"}:
        return workspace_root / suite
    raise ValueError(f"Unknown Vitest suite: {suite}")


def iter_backend_test_files(workspace_root: Path) -> tuple[Path, ...]:
    """Return the backend test modules included by the pytest inventory."""
    return tuple(sorted((workspace_root / "backend" / "testing").glob("test_*.py")))


def suite_scan_prefixes(suite: str) -> tuple[str, ...]:
    """Return the only top-level subtrees ``is_vitest_test_path`` can accept.

    Kept adjacent to the predicate on purpose: this is a restatement of the
    predicate's ``relative.startswith(...)`` clauses, and the two drifting apart
    would silently drop test files from every inventory. That is pinned by
    ``test_scan_prefixes_cover_every_path_the_predicate_accepts``.
    """
    if suite == "agent":
        return ("src",)
    if suite == "admin-ui":
        return ("src", "eslint-rules")
    if suite == "admin-ui-viewport":
        return ("src",)
    raise ValueError(f"Unknown Vitest suite: {suite}")


def is_vitest_test_path(suite: str, relative: str) -> bool:
    """Return whether *relative* is collected by a configured Vitest suite."""
    if suite == "agent":
        return relative.startswith("src/") and relative.endswith(".test.ts")
    if suite == "admin-ui":
        included = (
            (
                relative.startswith("src/")
                and relative.endswith((".test.ts", ".test.tsx"))
            )
            or (
                relative.startswith("eslint-rules/")
                and relative.endswith((".test.js", ".test.ts"))
            )
        )
        return included and not (
            relative.startswith("src/") and ".viewport.test." in relative
        )
    if suite == "admin-ui-viewport":
        return relative.startswith("src/") and relative.endswith(
            (".viewport.test.ts", ".viewport.test.tsx")
        )
    raise ValueError(f"Unknown Vitest suite: {suite}")


def iter_vitest_test_files(workspace_root: Path, suite: str) -> tuple[Path, ...]:
    """Return absolute paths in the files collected by *suite*'s config.

    Two things here are load-bearing for cost, not for correctness, and the
    result is identical without them.

    This used to ``rglob("*")`` the whole suite root and call ``is_file()`` --
    a stat syscall -- on every entry before asking whether the path could even
    be a test. ``admin-ui`` contains ``node_modules``, so that stat-ed on the
    order of 200k files to find ~500, and every one of them was then rejected
    by the predicate for not starting with ``src/``. Cheap locally; over the
    Docker-on-Windows bind mount the standalone lane runs on it blew the 30s
    pytest timeout, taking down both this inventory and the ranked-queue guard
    that consumes it -- reported as an inventory failure, which is not what it
    was.

    So: descend only into the subtrees the predicate can accept, and apply the
    predicate (pure string work) before spending the stat.
    """
    suite_root = _suite_root(workspace_root, suite)
    paths: list[Path] = []
    for prefix in suite_scan_prefixes(suite):
        prefix_root = suite_root / prefix
        for path in prefix_root.rglob("*"):
            relative = path.relative_to(suite_root).as_posix()
            if is_vitest_test_path(suite, relative) and path.is_file():
                paths.append(path)
    return tuple(sorted(paths))


def vitest_test_file_inventory(
    workspace_root: Path, suite: str, paths: Iterable[Path] | None = None
) -> set[str]:
    """Return suite-relative POSIX paths in the Vitest inventory."""
    suite_root = _suite_root(workspace_root, suite)
    candidates = paths if paths is not None else iter_vitest_test_files(workspace_root, suite)
    return {
        path.relative_to(suite_root).as_posix()
        for path in candidates
        if is_vitest_test_path(suite, path.relative_to(suite_root).as_posix())
    }


def vitest_test_file_inventory_from_paths(suite: str, paths: Iterable[str]) -> set[str]:
    """Filter suite-relative paths, including paths read from ``git ls-tree``."""
    return {relative for relative in paths if is_vitest_test_path(suite, relative)}

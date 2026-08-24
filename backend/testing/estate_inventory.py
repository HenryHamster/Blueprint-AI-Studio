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
    """Return absolute paths in the files collected by *suite*'s config."""
    suite_root = _suite_root(workspace_root, suite)
    paths = (
        path
        for path in suite_root.rglob("*")
        if path.is_file()
        and is_vitest_test_path(suite, path.relative_to(suite_root).as_posix())
    )
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

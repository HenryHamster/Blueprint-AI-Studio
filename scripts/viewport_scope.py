#!/usr/bin/env python3
"""Select viewport tests directly importing changed admin-ui components.

The task-level viewport gate deliberately follows only one import edge: a
viewport test's direct relative import of a changed component module. Following
component-to-component imports would turn ordinary component edits into a
full-suite run and defeat the gate's scoped-feedback purpose.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


_IMPORT_RE = re.compile(
    r"""(?mx)
    ^\s*import\b(?!\s*\()
    (?:
        \s*['\"](?P<side_effect>\.[^'\"]+)['\"]
        |
        (?:(?!^\s*import\b)[\s\S])*?\bfrom\s*['\"](?P<from>\.[^'\"]+)['\"]
    )
    """
)
_MODULE_SUFFIXES = (".tsx", ".ts", ".jsx", ".js")


def _relative_path(path: Path, repo_root: Path) -> str:
    return path.relative_to(repo_root).as_posix()


def _resolve_relative_module(
    importer: Path, specifier: str, repo_root: Path
) -> Path | None:
    """Resolve a relative TypeScript module to its checked-in source file."""
    candidate = (importer.parent / specifier).resolve()
    try:
        candidate.relative_to(repo_root)
    except ValueError:
        return None
    candidates = [candidate]
    if candidate.suffix == "":
        candidates.extend(candidate.with_suffix(suffix) for suffix in _MODULE_SUFFIXES)
        candidates.extend(
            candidate / f"index{suffix}" for suffix in _MODULE_SUFFIXES
        )
    for resolved in candidates:
        if resolved.is_file():
            return resolved
    return None


def _direct_relative_imports(test_path: Path, repo_root: Path) -> set[Path]:
    imported_modules: set[Path] = set()
    source = test_path.read_text(encoding="utf-8")
    for match in _IMPORT_RE.finditer(source):
        specifier = match.group("side_effect") or match.group("from")
        resolved = _resolve_relative_module(
            test_path, specifier, repo_root
        )
        if resolved is not None:
            imported_modules.add(resolved)
    return imported_modules


def select_viewport_tests(repo_root: Path, changed_files: set[str]) -> list[str]:
    """Return sorted repo-relative viewport tests selected by changed modules."""
    repo_root = repo_root.resolve()
    changed_paths = {
        (repo_root / path).resolve()
        for path in changed_files
        if path.startswith("admin-ui/")
    }
    changed_components = {
        path
        for path in changed_paths
        if path.parent == repo_root / "admin-ui/src/components"
        and path.suffix == ".tsx"
        and not path.name.endswith(".viewport.test.tsx")
    }
    viewport_tests = sorted(
        (repo_root / "admin-ui/src").rglob("*.viewport.test.tsx")
    )
    selected = {
        path
        for path in changed_paths
        if path.name.endswith(".viewport.test.tsx") and path in viewport_tests
    }
    for component in changed_components:
        namesake = component.with_name(f"{component.stem}.viewport.test.tsx")
        if namesake.is_file():
            selected.add(namesake)
        for viewport_test in viewport_tests:
            if component in _direct_relative_imports(viewport_test, repo_root):
                selected.add(viewport_test)
    return sorted(_relative_path(path, repo_root) for path in selected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--changed-files-file", type=Path, required=True)
    args = parser.parse_args()
    changed_files = {
        line.strip()
        for line in args.changed_files_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    selected = select_viewport_tests(args.repo_root, changed_files)
    if selected:
        sys.stdout.write("\n".join(selected) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

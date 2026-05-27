"""Helpers for surfacing operator-visible warnings on `build_config.directories[*].lint_cmd`.

The agent (`agent/src/prompt.ts::detectLinterKind`) recognizes a small set of
lint command prefixes for scoped, changed-file-only execution. Anything else
falls through to a verbatim run against the full tree — quietly re-introducing
baseline-debt blockage. The verbatim fallback is intentional; this module only
adds visibility, it does not reject or rewrite commands.

Python `detect_linter_kind` MUST stay in lockstep with the canonical TypeScript
logic at agent/src/prompt.ts:969-977.
"""

from __future__ import annotations

import re

_WRAPPER_RE = re.compile(r"^(python3?\s+-m\s+|npx\s+|pnpm\s+exec\s+)")
_LINTER_RE = re.compile(r"^(ruff|eslint|mypy|flake8|pyright)\b")


def detect_linter_kind(lint_cmd: str) -> str | None:
    """Return the recognized linter kind for `lint_cmd`, or None.

    Mirrors `agent/src/prompt.ts::detectLinterKind`: strips an optional
    leading wrapper (`python3 -m `, `python -m `, `npx `, `pnpm exec `),
    then checks the remaining command for one of the recognized leading
    tokens delimited by a word boundary.
    """
    if not isinstance(lint_cmd, str) or not lint_cmd:
        return None
    base = _WRAPPER_RE.sub("", lint_cmd)
    match = _LINTER_RE.match(base)
    return match.group(1) if match else None


def build_config_lint_warnings(build_config: dict | None) -> list[str]:
    """Return one warning string per directory whose `lint_cmd` is unrecognized.

    Empty / missing `lint_cmd` is treated as no-op (no warning). The returned
    list preserves directory order so warnings line up with the config UI.
    """
    if not build_config:
        return []
    directories = build_config.get("directories") or []
    warnings: list[str] = []
    for entry in directories:
        if not isinstance(entry, dict):
            continue
        lint_cmd = entry.get("lint_cmd")
        if not isinstance(lint_cmd, str) or not lint_cmd.strip():
            continue
        if detect_linter_kind(lint_cmd) is not None:
            continue
        path = entry.get("path") or "<unknown>"
        warnings.append(
            f"Directory {path!r}: lint_cmd {lint_cmd!r} is not a recognized linter "
            "(ruff/eslint/mypy/flake8/pyright). It will run verbatim against the "
            "full tree on every PR rather than only changed files."
        )
    return warnings


__all__ = ["build_config_lint_warnings", "detect_linter_kind"]

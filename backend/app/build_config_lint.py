"""Helpers for surfacing operator-visible warnings on `build_config`.

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


class _BuildConfigCarrier:
    """Minimal attribute carrier for the admission decoder."""

    __slots__ = ("build_config",)

    def __init__(self, build_config: dict) -> None:
        self.build_config = build_config


def strategic_lane_warnings(
    build_config: dict | None, autonomous_enabled: bool | None
) -> list[str]:
    """Warn when the Strategic lane is off on a project that is autonomous.

    ``autonomous_enabled=True`` with ``automatic_strategic_cycles_enabled``
    disabled is ALWAYS an operator-intent mismatch: the tactical loop keeps
    spawning research (so every liveness surface looks healthy) while the whole
    automatic Strategic authoring lane is off. Nothing in the codebase ever
    writes that flag back to ``True``, so a "bounded tactical canary" stored as
    a bare ``false`` is permanent by construction. Passing ``None`` for
    ``autonomous_enabled`` means "unknown" and suppresses the check.
    """
    if not build_config or autonomous_enabled is not True:
        return []

    # Local import: this module is a dependency-light leaf that the project
    # routers import at module scope, while the admission module pulls in the
    # ORM and the locking helpers.
    from app.strategic_cycle_admission import (
        AUTOMATIC_STRATEGIC_CYCLES_ENABLED_BUILD_CONFIG_KEY as _KEY,
        describe_automatic_strategic_cycles,
    )

    admission = describe_automatic_strategic_cycles(
        _BuildConfigCarrier(build_config)
    )
    warnings: list[str] = []
    if not admission.enabled:
        bound = (
            f"expires {admission.until.isoformat()}Z"
            if admission.until is not None
            else "NO expiry — it will never re-enable itself"
        )
        detail = (
            f"reason {admission.reason!r}"
            if admission.reason
            else "no reason recorded"
        )
        actor = (
            f"disabled by {admission.disabled_by!r}"
            if admission.disabled_by
            else "no actor recorded"
        )
        warnings.append(
            f"{_KEY} is disabled while autonomous_enabled is true: tactical "
            "research keeps spawning but NO automatic Strategic cycle will ever "
            f"be authored ({bound}; {detail}; {actor}). If this is a bounded "
            'canary, store the bounded form {"enabled": false, "until": '
            '"<iso8601>", "reason": "...", "disabled_by": "..."} so it expires '
            "on its own; otherwise turn autonomous_enabled off too."
        )
    if admission.malformed_until:
        warnings.append(
            f"{_KEY}.until is not a parseable ISO-8601 timestamp "
            f"({admission.until_raw!r}); the disable is being treated as "
            "UNBOUNDED and will never expire on its own."
        )
    return warnings


def build_config_lint_warnings(
    build_config: dict | None,
    *,
    autonomous_enabled: bool | None = None,
) -> list[str]:
    """Return operator-visible advisory warnings for a stored ``build_config``.

    One warning per directory whose `lint_cmd` is unrecognized (empty / missing
    `lint_cmd` is a no-op), preserving directory order so warnings line up with
    the config UI, followed by the Strategic-lane intent-mismatch warnings when
    the caller supplies ``autonomous_enabled``.
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
    warnings.extend(strategic_lane_warnings(build_config, autonomous_enabled))
    return warnings


__all__ = [
    "build_config_lint_warnings",
    "detect_linter_kind",
    "strategic_lane_warnings",
]

"""Pure theme-delivery certification layer.

No AsyncSession, no DB writes — maps a version goal + PR diffstat to a
deterministic cert object the release qualifier uses to detect when a
candidate's diffstat directionally contradicts its stated theme.
"""

from __future__ import annotations

import os
from typing import Any

# Theme vocab — checked in priority order so leanness wins over reliability
# when a goal mentions both "lean" and "stabilize".
_LEANNESS_KEYWORDS: frozenset[str] = frozenset(
    {"lean", "shrink", "reduce", "decompose", "remove", "trim", "cut"}
)
_RELIABILITY_KEYWORDS: frozenset[str] = frozenset(
    {"stabilize", "harden", "reliability", "recovery", "fix"}
)
_PERFORMANCE_KEYWORDS: frozenset[str] = frozenset(
    {"performance", "latency", "cost", "speed"}
)
_FEATURE_KEYWORDS: frozenset[str] = frozenset(
    {"add", "build", "implement", "ship", "enable"}
)

_DEFAULT_WARN_THRESHOLD: float = 0.70
_DEFAULT_BLOCK_THRESHOLD: float = 0.0


def classify_theme(goal: str | None) -> dict[str, Any]:
    """Map a ReleaseVersion goal string to a theme_class and falsifiable flag.

    Returns ``{theme_class, falsifiable}`` where ``falsifiable=True`` only for
    ``"leanness"`` — the sole class whose claim can be contradicted in sign by
    net-LOC evidence.
    """
    if not goal:
        return {"theme_class": "unknown", "falsifiable": False}

    goal_lower = goal.lower()

    for kw in _LEANNESS_KEYWORDS:
        if kw in goal_lower:
            return {"theme_class": "leanness", "falsifiable": True}

    for kw in _RELIABILITY_KEYWORDS:
        if kw in goal_lower:
            return {"theme_class": "reliability", "falsifiable": False}

    for kw in _PERFORMANCE_KEYWORDS:
        if kw in goal_lower:
            return {"theme_class": "performance", "falsifiable": False}

    for kw in _FEATURE_KEYWORDS:
        if kw in goal_lower:
            return {"theme_class": "feature", "falsifiable": False}

    return {"theme_class": "unknown", "falsifiable": False}


def _extract_module_name(filename: str) -> str:
    """Return the basename of a file path."""
    return os.path.basename(filename) if filename else ""


def _extract_named_targets(
    goal: str | None, per_module_deltas: dict[str, int]
) -> list[str]:
    """Return module names whose stem appears verbatim in the goal text."""
    if not goal:
        return []
    goal_lower = goal.lower()
    result = []
    for module_name in per_module_deltas:
        stem = module_name.lower()
        if "." in stem:
            stem = stem.rsplit(".", 1)[0]
        if stem and stem in goal_lower:
            result.append(module_name)
    return result


def compute_theme_evidence(
    version_goal: str | None,
    pr_files_by_number: dict[int, list[dict[str, Any]]],
    module_size_limits_path: str | None = None,
) -> dict[str, Any]:
    """Compute LOC evidence from PR file diffs.

    ``pr_files_by_number`` maps PR number → list of file dicts, each with
    ``additions: int``, ``deletions: int``, and ``filename: str``.
    ``module_size_limits_path`` is reserved for future use.
    """
    theme = classify_theme(version_goal)

    net_loc = 0
    per_module_deltas: dict[str, int] = {}

    for files in pr_files_by_number.values():
        for f in files:
            if not isinstance(f, dict):
                continue
            additions = int(f.get("additions", 0) or 0)
            deletions = int(f.get("deletions", 0) or 0)
            delta = additions - deletions
            net_loc += delta

            filename = str(f.get("filename", "") or "")
            module_name = _extract_module_name(filename)
            if module_name:
                per_module_deltas[module_name] = (
                    per_module_deltas.get(module_name, 0) + delta
                )

    named_targets = _extract_named_targets(version_goal, per_module_deltas)

    return {
        "net_loc": net_loc,
        "per_module_deltas": per_module_deltas,
        "named_targets": named_targets,
        "theme_class": theme["theme_class"],
        "falsifiable": theme["falsifiable"],
    }


def build_theme_delivery_cert(
    version_goal: str | None,
    evidence: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Build a theme-delivery certification object from evidence and policy.

    ``policy`` is the promotion-policy dict (from ``_load_policy``); the
    ``theme_warn_threshold`` and ``theme_block_threshold`` keys are read from
    it, falling back to 0.70 / 0.0 when absent.

    Hard-block fires ONLY when BOTH ``directional_contradiction=True`` AND
    ``deterministic_grade < theme_block_threshold`` AND
    ``theme_block_threshold > 0.0``.  Ships dark by default (0.0 means nothing
    is below 0.0, so the block arm never fires until an operator raises it).
    """
    theme_warn_threshold = float(
        policy.get("theme_warn_threshold", _DEFAULT_WARN_THRESHOLD)
    )
    theme_block_threshold = float(
        policy.get("theme_block_threshold", _DEFAULT_BLOCK_THRESHOLD)
    )

    theme_class: str = str(evidence.get("theme_class", "unknown"))
    falsifiable: bool = bool(evidence.get("falsifiable", False))
    net_loc: int = int(evidence.get("net_loc", 0))

    directional_contradiction = falsifiable and net_loc > 0

    if theme_class == "leanness":
        deterministic_grade = 1.0 if net_loc <= 0 else 0.0
    else:
        deterministic_grade = 1.0

    assessed_grade = deterministic_grade

    if theme_class == "unknown":
        state = "not_applicable"
    elif directional_contradiction and deterministic_grade < theme_block_threshold:
        state = "blocked"
    elif assessed_grade < theme_warn_threshold:
        state = "shortfall"
    else:
        state = "certified"

    shortfalls: list[dict[str, Any]] = []
    criteria: list[dict[str, Any]] = []

    if theme_class == "leanness":
        met = net_loc <= 0
        criteria.append(
            {
                "name": "net_loc_negative",
                "description": "Version claiming leanness must have net-negative LOC delta",
                "required": True,
                "met": met,
                "actual": net_loc,
            }
        )
        if not met:
            shortfalls.append(
                {
                    "criterion": "net_loc_negative",
                    "why": (
                        f"leanness theme requires net-negative LOC but candidate "
                        f"added {net_loc} lines"
                    ),
                    "evidence": {"net_loc": net_loc, "theme_class": theme_class},
                }
            )

    return {
        "theme_class": theme_class,
        "falsifiable": falsifiable,
        "net_loc": net_loc,
        "directional_contradiction": directional_contradiction,
        "deterministic_grade": deterministic_grade,
        "assessed_grade": assessed_grade,
        "state": state,
        "shortfalls": shortfalls,
        "criteria": criteria,
        "threshold_warn": theme_warn_threshold,
        "threshold_block": theme_block_threshold,
    }

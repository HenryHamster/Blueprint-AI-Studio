#!/usr/bin/env python3
"""One-shot migration: rewrite retired/old model names in the live
runtime_config to their current equivalents so PATCH /api/admin/settings
stops 400ing on stale DB values.

Fetches the current config, applies a conservative rewrite map (only for
values that would now fail schema validation), PATCHes back only the
changed fields. Defaults to dry-run — pass --apply to actually save.

Usage:
    python scripts/migrate_runtime_config.py             # preview only
    python scripts/migrate_runtime_config.py --apply     # save
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from repo root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from blueprint_api import api  # noqa: E402


# Map of retired/old model names → current replacement. Only include
# values that would fail schema validation under the new provider enums.
# Intentionally avoid "upgrades" that change intent (e.g. leave
# reasoning_effort=high alone even though xhigh is now available).
REWRITES: dict[str, str] = {
    # OpenAI — the old pre-dotted-name catalog is gone from the enum.
    # Map to the nearest capability-class peer.
    "gpt-5":        "gpt-5.4",
    "gpt-5-codex":  "gpt-5.3-codex",
    "gpt-5-mini":   "gpt-5.4-mini",
    "gpt-5-nano":   "gpt-5.4-mini",   # no 5.4-nano published yet
    "gpt-4.1":      "gpt-5.4",
    "gpt-4o":       "gpt-5.4-mini",
    "gpt-4o-mini":  "gpt-5.4-mini",
    "o3":           "gpt-5.4",
    "o3-pro":       "gpt-5.4",
    "o3-mini":      "gpt-5.4-mini",
    "o4-mini":      "gpt-5.4-mini",
    "o4-mini-high": "gpt-5.4-mini",

    # Gemini — retired / doc-drift preview IDs. Legacy alias handling
    # in gemini.py::settings_to_cli rewrites these at dispatch, but the
    # admin PATCH validator rejects them so DB values have to be clean.
    "gemini-3-pro-preview":     "gemini-3.1-pro-preview",
    "gemini-3.1-flash-preview": "gemini-3-flash-preview",
}


# Fields in runtime_config that can hold model strings. Anything not in
# this list we leave alone.
_MODEL_BEARING_FIELDS = (
    "provider_tier_model_map",
    "provider_settings_by_effort",
    "provider_settings_defaults",
    "task_type_provider_overrides",
    "task_type_model_preferences",
)


def rewrite(value):
    if isinstance(value, str):
        return REWRITES.get(value, value)
    if isinstance(value, dict):
        return {k: rewrite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [rewrite(v) for v in value]
    return value


def walk_diff(old, new, path: str = ""):
    """Yield (path, old_leaf, new_leaf) for each changed leaf."""
    if isinstance(old, dict) and isinstance(new, dict):
        for key in old:
            sub_path = f"{path}.{key}" if path else key
            yield from walk_diff(old.get(key), new.get(key), sub_path)
    elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for i, (o, n) in enumerate(zip(old, new)):
            yield from walk_diff(o, n, f"{path}[{i}]")
    elif old != new:
        yield (path, old, new)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually PATCH the orchestrator. Without this flag, runs dry.",
    )
    args = parser.parse_args()

    print("Fetching current runtime_config...")
    cfg = api("GET", "/api/admin/settings")

    patch_body: dict = {}
    total_changes = 0
    for field in _MODEL_BEARING_FIELDS:
        original = cfg.get(field)
        if original is None:
            continue
        rewritten = rewrite(original)
        changes = list(walk_diff(original, rewritten))
        if not changes:
            continue
        print(f"\n[{field}]")
        for path_, old_v, new_v in changes:
            print(f"  {path_}: {old_v!r} -> {new_v!r}")
            total_changes += 1
        patch_body[field] = rewritten

    if total_changes == 0:
        print("\nNo stale values found. Nothing to do.")
        return 0

    print(f"\nTotal changes: {total_changes}")

    if not args.apply:
        print("\nDry run only — rerun with --apply to save.")
        return 0

    print("\nApplying PATCH /api/admin/settings...")
    api("PATCH", "/api/admin/settings", patch_body)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

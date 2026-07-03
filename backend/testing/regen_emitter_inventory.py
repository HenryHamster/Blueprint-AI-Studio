#!/usr/bin/env python3
"""Regenerate (or check) the backend websocket emitter inventory snapshot.

The guard test ``test_websocket_emitter_inventory.py`` asserts that a checked-in
snapshot fixture (``__fixtures__/websocket_event_inventory_v1.json``) still
matches an AST-built inventory of every broadcast/send_json/emit_* call site in
``backend/app``. When a new emitter lands on the integration branch the snapshot
goes stale and the test first fails *at the promotion gate*. This script is the
missing regen tool: it rebuilds the snapshot deterministically so the staleness
is a one-command, reviewable fix instead of hand-editing a 4000-line fixture.

Mirrors ``backend/testing/check_size_limits.py`` — stdlib only, no DB, no Docker,
no ``app`` imports — so it runs in a bare checkout.

Two modes:

* check mode (default): rebuild the inventory from ``backend/app`` and compare it
  to the fixture using the SAME stable-signature semantics the gate test uses
  (line numbers ignored). Prints whether the fixture is up to date and lists the
  added/removed emitters. Exits non-zero when stale, so it doubles as a fast,
  DB-free proxy for the promotion gate.
* ``--fix`` / ``--write`` mode: rewrite the fixture from current source. Existing
  line numbers are preserved for emitters whose stable signature is unchanged
  (so unrelated edits that merely shift lines never churn the fixture); genuinely
  new/changed emitters take their current line number. ``description`` and
  ``schema_version`` are carried over from the existing fixture. Running ``--fix``
  twice yields no diff on the second run (idempotent), and after ``--fix`` the
  gate test passes.

Usage (from the repo root, no install step)::

    python backend/testing/regen_emitter_inventory.py            # check, exit 1 if stale
    python backend/testing/regen_emitter_inventory.py --fix      # regenerate the fixture
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# Make ``websocket_event_inventory`` importable when this file is run as
# ``python backend/testing/regen_emitter_inventory.py`` from the repo root: add
# this file's directory (backend/testing) to sys.path. The inventory builder is
# stdlib-only (ast + pathlib), so this pulls in no server/DB machinery.
_TESTING_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTING_DIR not in sys.path:
    sys.path.insert(0, _TESTING_DIR)

from websocket_event_inventory import (  # noqa: E402
    build_websocket_event_inventory,
    inventory_signature,
)

# Repo root is two levels up (backend/testing -> repo root).
_REPO_ROOT = os.path.normpath(os.path.join(_TESTING_DIR, "..", ".."))


def _resolve_default_app_dir() -> str:
    """Locate ``backend/app`` robustly across CWD / mount layouts.

    ``app`` is always a sibling of ``testing`` under ``backend``, but different
    runners invoke us with different CWDs (repo root, ``backend/``, or a
    bind-mount like ``/app``), so a single ``__file__``-relative navigation is
    brittle (it once resolved to ``/backend/app`` under the Docker test mount).
    Try the most reliable candidates in order and pick the first that exists;
    fall back to the sibling-of-testing path so ``--app-dir`` overrides still work.
    """
    candidates = [
        os.path.join(os.path.dirname(_TESTING_DIR), "app"),  # <backend>/app (sibling)
        os.path.join(_REPO_ROOT, "backend", "app"),  # <root>/backend/app
        os.path.join(os.getcwd(), "backend", "app"),  # CWD == repo root
        os.path.join(os.getcwd(), "app"),  # CWD == backend/
    ]
    for cand in candidates:
        if os.path.isdir(cand):
            return os.path.normpath(cand)
    return os.path.normpath(candidates[0])


_DEFAULT_APP_DIR = _resolve_default_app_dir()
_DEFAULT_FIXTURE = os.path.join(
    _TESTING_DIR, "__fixtures__", "websocket_event_inventory_v1.json"
)

# Canonical metadata used when no existing fixture is present to carry it over.
_DEFAULT_DESCRIPTION = (
    "Reviewed backend websocket emitter inventory. Line numbers are "
    "informational; tests compare stable fields only."
)
_DEFAULT_SCHEMA_VERSION = 1


def _signature_key(record: dict[str, Any]) -> str:
    """Hashable, order-stable key for an emitter's stable (lineno-free) signature.

    Uses the same ``inventory_signature`` the gate test compares on, so two
    records collapse to one key iff the gate would treat them as the same site.
    """
    return json.dumps(inventory_signature(record), sort_keys=True)


def load_fixture(path: str) -> dict[str, Any] | None:
    """Load the snapshot fixture, or ``None`` when it does not exist yet."""
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def build_fixture(
    app_dir: str, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build the full snapshot fixture dict from current ``backend/app`` source.

    ``existing`` (the prior fixture, if any) is used only to preserve line numbers
    for emitters whose stable signature is unchanged and to carry over the
    ``description``/``schema_version`` metadata — never to alter which emitters or
    fields are recorded, which always come from live source.
    """
    records = build_websocket_event_inventory(Path(app_dir))

    preserved_lineno: dict[str, int] = {}
    if existing:
        for entry in existing.get("emitters", []):
            lineno = entry.get("lineno")
            if lineno is not None:
                preserved_lineno[_signature_key(entry)] = lineno

    emitters: list[dict[str, Any]] = []
    for record in records:
        entry = dict(record)
        key = _signature_key(record)
        if key in preserved_lineno:
            entry["lineno"] = preserved_lineno[key]
        emitters.append(entry)

    summary = dict(sorted(Counter(r["classification"] for r in records).items()))
    meta = existing or {}
    return {
        "description": meta.get("description", _DEFAULT_DESCRIPTION),
        "emitters": emitters,
        "schema_version": meta.get("schema_version", _DEFAULT_SCHEMA_VERSION),
        "summary": summary,
    }


def serialize_fixture(fixture: dict[str, Any]) -> str:
    """Serialize the fixture the canonical way (matches the checked-in file).

    ``indent=2``, ``sort_keys=True``, ASCII-escaped, trailing newline — so a
    ``--fix`` diff shows only genuinely changed inventory content, and two runs
    produce byte-identical output.
    """
    return json.dumps(fixture, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def diff_signatures(
    app_dir: str, fixture: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compare current source vs fixture using the gate test's stable signatures.

    Returns ``(missing, added)``: ``missing`` are signatures in the fixture but no
    longer in source (removed emitters), ``added`` are signatures in source but
    absent from the fixture (new/changed emitters). A missing fixture yields every
    current emitter as ``added``.
    """
    current = [
        inventory_signature(record)
        for record in build_websocket_event_inventory(Path(app_dir))
    ]
    expected = [
        inventory_signature(entry) for entry in (fixture or {}).get("emitters", [])
    ]
    missing = [record for record in expected if record not in current]
    added = [record for record in current if record not in expected]
    return missing, added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check (default) or regenerate (--fix) the backend websocket emitter "
            "inventory snapshot fixture."
        )
    )
    parser.add_argument(
        "--fix",
        "--write",
        action="store_true",
        dest="fix",
        help="rewrite the fixture from current backend/app source",
    )
    parser.add_argument(
        "--fixture",
        dest="fixture_path",
        default=_DEFAULT_FIXTURE,
        help="path to the snapshot fixture (default: alongside this script)",
    )
    parser.add_argument(
        "--app-dir",
        dest="app_dir",
        default=_DEFAULT_APP_DIR,
        help="backend/app source root to inventory (default: inferred from repo root)",
    )
    args = parser.parse_args(argv)

    if not os.path.isdir(args.app_dir):
        print(f"[error] app dir not found: {args.app_dir}", file=sys.stderr)
        return 2

    existing = load_fixture(args.fixture_path)

    if args.fix:
        fixture = build_fixture(args.app_dir, existing)
        new_text = serialize_fixture(fixture)
        old_text = (
            open(args.fixture_path, encoding="utf-8").read()
            if os.path.isfile(args.fixture_path)
            else None
        )
        if old_text == new_text:
            print(
                f"Fixture already up to date ({len(fixture['emitters'])} emitters): "
                f"{args.fixture_path}"
            )
            return 0
        os.makedirs(os.path.dirname(args.fixture_path) or ".", exist_ok=True)
        with open(args.fixture_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(new_text)
        missing, added = diff_signatures(args.app_dir, existing)
        print(
            f"Wrote {len(fixture['emitters'])} emitters to {args.fixture_path} "
            f"(+{len(added)} added, -{len(missing)} removed since prior fixture)."
        )
        return 0

    # check mode
    if existing is None:
        print(
            f"[stale] snapshot fixture missing: {args.fixture_path}\n"
            "Run with --fix to generate it."
        )
        return 1

    missing, added = diff_signatures(args.app_dir, existing)
    if not missing and not added:
        print(
            f"Snapshot fixture up to date "
            f"({len(existing.get('emitters', []))} emitters)."
        )
        return 0

    for record in added[:20]:
        print(
            f"[added]   {record['source_path']}:{record['scope']} "
            f"{record['callee']} event={record['event']}"
        )
    for record in missing[:20]:
        print(
            f"[removed] {record['source_path']}:{record['scope']} "
            f"{record['callee']} event={record['event']}"
        )
    print(
        f"\nSnapshot fixture stale: {len(added)} added, {len(missing)} removed "
        "emitter signature(s). Review the call sites above, then run "
        "'python backend/testing/regen_emitter_inventory.py --fix' to regenerate "
        f"{os.path.relpath(args.fixture_path, _REPO_ROOT)}."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

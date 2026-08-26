#!/usr/bin/env python3
"""Delete duration rows for tests the tree no longer has. Never add one.

WHY A PRUNER, WHEN THE GUARDS ALREADY SAY "REGENERATE"
------------------------------------------------------
The three durations artifacts are MEASUREMENTS, so the registered repair for
all of them is a timed run -- ``scripts/store-backend-durations.sh`` (Docker +
Postgres, 15-20 min) or ``node scripts/vitest-test-telemetry.mjs``. That is the
honest answer to a MISSING row: nothing in a checkout can invent a timing, and
``derived_artifact_repair.MEASUREMENT_TAX_GUIDANCE`` spells out why the two
cheap shortcuts are worse than the red.

It is NOT the honest answer to a STALE row, and that asymmetry is the whole
point of this script. Removing the entry for a test that no longer exists is a
total function of the checked-out tree: it needs no measurement, it discards no
measurement anything could ever use again, and it is exactly what a full
regeneration would do to that row anyway. Charging a 20-minute timed run for it
is what makes rot sit on the integration branch until the promotion PR
fail-closes on it -- and in this repository a red promotion run CLOSES the PR
and deletes its release branch.

So: rot is repaired here, for free and offline. Additions still cost what they
cost, and this script will not pretend otherwise.

WHAT IT WILL NOT DO
-------------------
* It never writes a row. There is no ``--fill``, no averaged placeholder, no
  ``measured: false``. A fabricated row permanently marks a file "covered" so
  no later timed run measures it, and pytest-split already estimates an unknown
  test from the mean, so the fabrication buys the shard matrix nothing.
* It never rewrites a file it cannot round-trip. Before pruning anything it
  re-serialises the file's own parsed contents and compares bytes; a mismatch
  means the on-disk formatting is not the one this script would emit, and
  rewriting would produce a whole-file diff that conflicts with every
  concurrent branch. That is a refusal, not a warning -- these artifacts are
  already the repository's worst merge ratchets.
* It never decides what is stale on its own. The rot sets come from
  ``derived_artifact_classify``, which is also what the guard-data lane reports
  from, so the pruner and the report cannot disagree about what is stale.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from derived_artifact_classify import (  # noqa: E402
    backend_stale_entries,
    vitest_live,
    vitest_recorded,
)

__all__ = ["PruneResult", "prune_backend_durations", "prune_vitest_durations", "main"]

PREFIX = "[prune-durations]"

BACKEND_ARTIFACT = "backend/.test_durations"
VITEST_SUITES = ("admin-ui", "agent")

# Serialisation shapes the artifacts are committed in. A file must re-emit
# BYTE-IDENTICALLY under its own shape before this script will touch it.
_BACKEND_SHAPE = {"indent": 2, "sort_keys": True}
_VITEST_SHAPE = {"indent": 2, "sort_keys": False}


@dataclass(frozen=True)
class PruneResult:
    """What one artifact's pass found and did."""

    artifact: str
    removed: tuple[str, ...] = ()
    written: bool = False
    refused: str = ""
    """Why the artifact was left alone despite carrying rot (empty if none)."""

    @property
    def blocked(self) -> bool:
        return bool(self.refused)


def _serialise(data, shape: dict) -> str:
    return json.dumps(data, indent=shape["indent"], sort_keys=shape["sort_keys"]) + "\n"


def _load_round_trippable(path: Path, shape: dict):
    """Parse *path* and prove this script can re-emit it byte for byte.

    Returns ``(data, None)`` when the round trip holds, or ``(None, reason)``.
    Fail-closed by construction: a shape this script cannot reproduce is a shape
    it must not rewrite.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"{path} is not valid JSON: {exc}"
    if _serialise(data, shape) != raw:
        return None, (
            f"{path} does not round-trip through this script's serialiser "
            f"(indent={shape['indent']}, sort_keys={shape['sort_keys']}); "
            "rewriting it would reformat every line and conflict with every "
            "concurrent branch. Refusing to touch it -- repair it by hand or "
            "regenerate it, and fix the shape here if the format changed on "
            "purpose."
        )
    return data, None


def prune_backend_durations(repo: Path, write: bool) -> PruneResult:
    """Drop entries whose test FILE is gone, or whose node id no longer exists."""
    path = repo / BACKEND_ARTIFACT
    if not path.is_file():
        return PruneResult(artifact=BACKEND_ARTIFACT)

    data, refusal = _load_round_trippable(path, _BACKEND_SHAPE)
    if data is None:
        # Compute the rot anyway so the caller can still REPORT it; only the
        # write is refused.
        return PruneResult(artifact=BACKEND_ARTIFACT, refused=refusal or "")

    stale = backend_stale_entries(repo)

    if not stale:
        return PruneResult(artifact=BACKEND_ARTIFACT)

    if not write:
        return PruneResult(artifact=BACKEND_ARTIFACT, removed=tuple(sorted(stale)))

    pruned = {key: value for key, value in data.items() if key not in stale}
    path.write_text(_serialise(pruned, _BACKEND_SHAPE), encoding="utf-8", newline="")
    return PruneResult(
        artifact=BACKEND_ARTIFACT, removed=tuple(sorted(stale)), written=True
    )


def prune_vitest_durations(repo: Path, suite: str, write: bool) -> PruneResult:
    """Drop ``files[]`` entries naming a vitest file the suite no longer collects."""
    artifact = f"{suite}/.test_durations.json"
    path = repo / artifact
    if not path.is_file():
        return PruneResult(artifact=artifact)

    data, refusal = _load_round_trippable(path, _VITEST_SHAPE)
    if data is None:
        return PruneResult(artifact=artifact, refused=refusal or "")

    stale = vitest_recorded(repo, suite) - vitest_live(repo, suite)
    if not stale:
        return PruneResult(artifact=artifact)

    if not write:
        return PruneResult(artifact=artifact, removed=tuple(sorted(stale)))

    kept = [
        entry
        for entry in data.get("files", [])
        if not (isinstance(entry, dict) and entry.get("file") in stale)
    ]
    data["files"] = kept
    path.write_text(_serialise(data, _VITEST_SHAPE), encoding="utf-8", newline="")
    return PruneResult(artifact=artifact, removed=tuple(sorted(stale)), written=True)


def prune_all(repo: Path, write: bool) -> list[PruneResult]:
    results = [prune_backend_durations(repo, write)]
    for suite in VITEST_SUITES:
        results.append(prune_vitest_durations(repo, suite, write))
    return results


def _report(results: list[PruneResult], write: bool, stream) -> None:
    for result in results:
        if result.refused:
            print(f"{PREFIX} REFUSED: {result.refused}", file=stream)
            continue
        if not result.removed:
            print(f"{PREFIX} {result.artifact}: no stale entries", file=stream)
            continue
        verb = "removed" if result.written else "would remove"
        shown = ", ".join(sorted(result.removed)[:10])
        more = len(result.removed) - min(len(result.removed), 10)
        suffix = f" (+{more} more)" if more else ""
        print(
            f"{PREFIX} {result.artifact}: {verb} {len(result.removed)} stale "
            f"entry(ies): {shown}{suffix}",
            file=stream,
        )
        if not write:
            print(
                f"{PREFIX}   run `python scripts/prune_stale_durations.py --write` "
                "to repair. This is ROT, not a missing measurement: no timed run "
                "is needed.",
                file=stream,
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Remove duration entries for tests that no longer exist."
    )
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument(
        "--write",
        action="store_true",
        help="Apply the prune. Without it the script only reports.",
    )
    args = parser.parse_args(argv)

    repo = Path(args.repo_root).resolve()
    results = prune_all(repo, args.write)
    _report(results, args.write, sys.stdout)

    if any(result.blocked for result in results):
        return 2
    if not args.write and any(result.removed for result in results):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())

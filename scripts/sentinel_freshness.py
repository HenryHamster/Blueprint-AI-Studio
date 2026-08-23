#!/usr/bin/env python3
"""Always-on derived-artifact freshness lane for the ``./check`` publication gate.

WHY THIS LANE EXISTS
--------------------
This repository stores several DERIVED artifacts as hand-maintained checked-in
files -- the websocket emitter inventory fixture, the hand-kept
``WEBSOCKET_EMITTER_INVENTORY`` dict, the conversational-Workday manifest's
per-file test counts, the ranked-queue and durations artifacts, the module-size
cap ledger.  Each has a drift sentinel that regenerates the artifact's expected
value from source and compares.

Before this lane, the publication gate's ALWAYS-ON portion covered exactly ONE
of them: the module-size cap ledger, and only because its guard happens to be a
standalone checker (``backend/testing/check_size_limits.py``) rather than a
pytest node.  Every other derived artifact's guard is a pytest drift sentinel
that runs only under ``./check test``.  That asymmetry is arbitrary -- the size
ledger is not more load-bearing than the emitter inventory -- and it is the
whole reason promotion PR #17764 (and #17738 before it) fail-closed on backend
shards whose only content was stale checked-in guard data.

Stale guard data has a second property that makes late detection expensive: it
is self-concealing.  The static release guard attributes failures differentially
(a failure present in BOTH the task tree and the base tree is INHERITED and
warn-passes), so the drift is visible on exactly one pull request -- the one
that introduces it.  Every later PR inherits it and passes.  The next tree with
no base to attribute against is the automain->main promotion matrix, which is
where the cost lands.

WHAT IT DOES
------------
Runs exactly the drift sentinels that
``backend/testing/drift_sentinel_surfaces.json`` registers for the surfaces THIS
diff touches -- never the whole standalone corpus -- through the harness's own
selector channel, and attributes them against the extracted base tree:

* a sentinel failing in the task tree but not the base tree BLOCKS, naming the
  sentinel and the exact regeneration command;
* a sentinel failing in BOTH trees does not block (inherited drift must never
  stop the fleet) but is reported loudly as stale guard data ON THE BASE BRANCH,
  again with the exact regeneration command, because that is the state that
  fail-closes the next promotion PR;
* an unusable current or baseline result fails CLOSED -- a lane that cannot tell
  must never report clean.

Deliberately NOT a self-healing guard: nothing here writes, regenerates or
refreshes any artifact.  It only reports which one drifted and how to repair it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from static_release_guard import (  # noqa: E402
    SENTINEL_REPAIR_COMMANDS,
    JUnitReport,
    _base_ref,
    _changed_files,
    _extract_base,
    _harness_selector,
    _pytest_result_is_authoritative,
    _run_static_corpus,
    classify_verification,
    parse_junit_report,
    sentinel_file,
    surface_sentinels,
)

PREFIX = "[sentinel freshness]"


def select_sentinels(changed: list[str], repo_root: str) -> list[str]:
    """Registered drift sentinels whose surface globs this diff matches.

    Selection is delegated to the SAME accessor the merge-time impacted-test
    gate and ``static_release_guard._is_static_scope`` use, so this lane cannot
    drift out of sync with the registry it enforces.
    """
    return sorted(surface_sentinels(changed, repo_root))


def repair_hint(sentinel: str) -> str:
    """The registered regeneration command for ``sentinel``, or a fallback."""
    command = SENTINEL_REPAIR_COMMANDS.get(sentinel_file(sentinel))
    if command:
        return command
    return (
        "no regeneration command is registered in "
        "static_release_guard.SENTINEL_REPAIR_COMMANDS for this sentinel; add "
        "one so the next failure names its own repair"
    )


def _report_sentinels(label: str, sentinels: list[str], stream) -> None:
    for sentinel in sentinels:
        print(f"{PREFIX} {label}: {sentinel}", file=stream)
        print(f"{PREFIX}   repair: {repair_hint(sentinel)}", file=stream)


def _failing_sentinels(failures: dict[str, str], sentinels: list[str]) -> list[str]:
    """Sentinels named by a JUnit failure map, matched on the file's basename.

    JUnit route keys carry the harness-relative path (``testing/test_x.py``)
    while the registry names them repo-relative, so the comparison is on the
    file's basename, which is unique across the standalone roster.
    """
    blob = " ".join([*failures.keys(), *(str(value) for value in failures.values())])
    return [
        sentinel
        for sentinel in sentinels
        if sentinel_file(sentinel).rsplit("/", 1)[-1] in blob
    ]


def _base_selectors(selectors: list[str], base_root: Path) -> list[str]:
    """Selectors whose underlying file exists in the extracted base tree.

    A selector naming a file the base does not have is a pytest USAGE error
    ("not found"), which aborts the whole base run before any test executes and
    would leave the lane with no baseline at all.
    """
    kept: list[str] = []
    for selector in selectors:
        relative = selector.split("::", 1)[0]
        if any(
            (root / relative).is_file()
            for root in (base_root, base_root / "backend")
        ):
            kept.append(selector)
    return kept


def _run_lane(repo: Path, base: str) -> int:
    changed = _changed_files(repo, base)
    sentinels = select_sentinels(changed, str(repo))
    if not sentinels:
        print(f"{PREFIX} no registered drift-sentinel surface changed; skipping")
        return 0

    print(f"{PREFIX} drift sentinels registered for this diff:")
    for sentinel in sentinels:
        print(f"  - {sentinel}")
    selectors = [_harness_selector(sentinel) for sentinel in sentinels]

    base_ref = _base_ref(repo, base)
    if base_ref is None:
        print(
            f"{PREFIX} ERROR: cannot resolve base ref origin/{base} or {base}; "
            "refusing to report clean without a baseline",
            file=sys.stderr,
        )
        return 2

    with tempfile.TemporaryDirectory(prefix="blueprint-sentinel-freshness-") as tmp:
        current_junit = Path(tmp) / "current.xml"
        current_result = _run_static_corpus(repo, current_junit, selectors=selectors)
        try:
            current_report = parse_junit_report(
                current_junit, tree_root=repo, label="current"
            )
        except RuntimeError as exc:
            print(f"{PREFIX} ERROR: cannot read the sentinel run: {exc}", file=sys.stderr)
            print(current_result.stdout, end="")
            print(current_result.stderr, end="", file=sys.stderr)
            return 1
        if not _pytest_result_is_authoritative(
            current_result, current_report, authority="sentinel_run_unusable"
        ):
            print(
                f"{PREFIX} ERROR: the sentinel run is not attributable; refusing "
                "to report clean",
                file=sys.stderr,
            )
            return 1
        if not current_report.failures:
            print(f"{PREFIX} PASS - every registered sentinel for this diff is fresh")
            return 0

        base_root = Path(tmp) / "base"
        base_root.mkdir()
        try:
            _extract_base(repo, base_ref, base_root)
        except RuntimeError as exc:
            print(f"{PREFIX} ERROR: cannot extract the base tree: {exc}", file=sys.stderr)
            return 1
        base_selectors = _base_selectors(selectors, base_root)
        if not base_selectors:
            baseline_report = JUnitReport()
        else:
            base_junit = Path(tmp) / "base.xml"
            base_result = _run_static_corpus(
                base_root, base_junit, selectors=base_selectors
            )
            try:
                baseline_report = parse_junit_report(
                    base_junit, tree_root=base_root, label="base"
                )
            except RuntimeError as exc:
                print(
                    f"{PREFIX} ERROR: cannot read the baseline sentinel run: {exc}",
                    file=sys.stderr,
                )
                return 1
            if not _pytest_result_is_authoritative(
                base_result, baseline_report, authority="sentinel_baseline_unusable"
            ):
                print(
                    f"{PREFIX} ERROR: the baseline sentinel run is not attributable; "
                    "refusing to charge or excuse this failure without it",
                    file=sys.stderr,
                )
                return 1

        outcome = classify_verification(
            runtime_available=True,
            current_exit_code=current_result.returncode,
            current_report=current_report,
            baseline_report=baseline_report,
        )
        new_failures = dict(outcome["new_failures"])
        inherited = {
            key: value
            for key, value in current_report.failures.items()
            if key not in new_failures
        }

        if inherited:
            stale = _failing_sentinels(inherited, sentinels)
            print(
                f"{PREFIX} BASE GUARD DATA STALE on {base_ref} - "
                f"{json.dumps(sorted(inherited))}. This does NOT block your "
                "change (the drift is inherited), but it WILL fail-close the "
                "next promotion PR until it is refreshed on the base branch.",
                file=sys.stderr,
            )
            _report_sentinels("stale on base", stale, sys.stderr)

        if new_failures:
            drifted = _failing_sentinels(new_failures, sentinels)
            print(
                f"{PREFIX} FAIL - this change leaves derived guard data stale: "
                f"{json.dumps(sorted(new_failures))}",
                file=sys.stderr,
            )
            _report_sentinels("drifted", drifted, sys.stderr)
            print(current_result.stdout, end="")
            print(current_result.stderr, end="", file=sys.stderr)
            return 1

    print(f"{PREFIX} PASS - no NEW derived-artifact drift attributable to this change")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="automain")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        return _run_lane(args.repo_root.resolve(), args.base)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"{PREFIX} ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

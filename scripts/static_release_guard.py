#!/usr/bin/env python3
"""Run the database-free release guard corpus against the task diff.

The size checker is baseline-aware.  The remaining static corpus is executed
against both the task tree and an extracted base tree; only newly appearing
pytest failures are attributed to this task.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

try:  # absent from synthetic fixtures that copy scripts/ piecemeal
    from posix_bash import bash_executable, posix_path  # noqa: E402
except ImportError:  # pragma: no cover - fixture trees are POSIX already
    bash_executable, posix_path = (lambda: "bash"), os.fspath  # noqa: E731

try:
    from guard_suppression_receipt import record_classification  # noqa: E402
except ImportError:  # pragma: no cover - emitting is fail-open by contract
    # A tree without the emitter (an older base checkout, a synthetic harness
    # fixture) must still run this lane unchanged: recording suppressions can
    # never be the reason a verdict, exit code or blocking decision moves.
    def record_classification(*_args: object, **_kwargs: object) -> None:
        return None
from testing.fixgate_attribution import (  # noqa: E402
    JUnitReport,
    classify_verification,
    extract_base,
    is_attributable_pytest_result,
    parse_junit_report,
    test_route_coverage_gaps,
)
from verification_receipt import (  # noqa: E402
    LANE_STATIC_CORPUS,
    record_lane as record_verification_lane,
)
from testing.impacted_tests import (  # noqa: E402
    load_surface_manifest,
    sentinel_file,
    surface_sentinels,
)
from app.size_limit_policy import SECTION_PATH_PREFIXES

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "backend"
    / "testing"
    / "conversational_workday_manifest.json"
)


def _manifest_protected_prefixes(manifest_path: Path = MANIFEST_PATH) -> tuple[str, ...]:
    """Derive extra static-guard prefixes from the Workday protection manifest.

    36 of the manifest's 55 protected paths live outside backend/ (admin-ui/,
    agent/, scripts/) -- keying guard scope only on the guard's own historical
    footprint leaves those changes unscoped. Falls back to no extra prefixes
    (the static floor below still applies) if the manifest is missing or
    malformed, so a broken manifest cannot crash guard selection.
    """
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    prefixes = set()
    for entry in manifest.get("protect", []):
        path = entry.get("path", "")
        if "/" in path:
            prefixes.add(f"{path.split('/', 1)[0]}/")
    return tuple(sorted(prefixes))


STATIC_PATH_PREFIXES = (
    "backend/",
    "check",
    "scripts/test-backend.sh",
    "scripts/static_release_guard.py",
) + _manifest_protected_prefixes()

_REPO_ROOT = str(Path(__file__).resolve().parents[1])


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def _base_ref(repo: Path, base: str) -> str | None:
    for candidate in (f"origin/{base}", base):
        if _git(repo, "rev-parse", "--verify", candidate, check=False).returncode == 0:
            return candidate
    return None


def _changed_files(repo: Path, base: str) -> list[str]:
    ref = _base_ref(repo, base)
    if ref is None:
        raise RuntimeError(f"cannot resolve base ref origin/{base} or {base}")
    committed = _git(repo, "diff", "--name-only", f"{ref}...HEAD").stdout.splitlines()
    uncommitted = _git(repo, "diff", "--name-only", "HEAD").stdout.splitlines()
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
    return sorted({path for path in (*committed, *uncommitted, *untracked) if path})


def _is_static_scope(path: str, repo_root: str = _REPO_ROOT) -> bool:
    """Whether ``path`` falls in scope for the static release guard corpus.

    In scope either by legacy prefix match, or by matching a surface glob
    registered in the drift-sentinel manifest (``drift_sentinel_surfaces.json``)
    — the same manifest that drives the merge-time impacted-test selector, so
    the two selection layers agree on which non-backend surfaces (e.g.
    admin-ui/, agent/) have guards with inputs the static corpus must also run
    for, rather than maintaining a second, independently drifting path list.
    """
    normalized = path.replace("\\", "/").strip("/")
    if _STANDALONE_SCRIPT_TEST_RE.fullmatch(normalized):
        return True
    if any(
        normalized == prefix.rstrip("/")
        or normalized.startswith(f"{prefix.rstrip('/')}/")
        for prefix in STATIC_PATH_PREFIXES
    ):
        return True
    for regex, _sentinels in load_surface_manifest(repo_root):
        if regex.match(normalized):
            return True
    return False


# Repair command for each drift sentinel whose failure means a DERIVED
# artifact is stale rather than that the code under test is wrong. A stale
# artifact is the single most common way a drift sentinel goes red, and the
# failure text alone names the mismatch, not the regenerator — so the operator
# is left to grep for it. Surfacing the exact command here is the difference
# between a two-minute fix and the hand-repair PR that #17738 needed.
SENTINEL_REPAIR_COMMANDS: dict[str, str] = {
    "backend/testing/test_websocket_emitter_inventory.py": (
        "python backend/testing/regen_emitter_inventory.py --fix"
    ),
    "backend/testing/test_operator_workbench_contracts.py": (
        "python backend/testing/regen_emitter_inventory.py --fix, then mirror the "
        "same paths into WEBSOCKET_EMITTER_INVENTORY in "
        "backend/app/workbench_contracts.py (the two inventories are kept by hand "
        "and drift independently)"
    ),
    "backend/testing/test_test_estate_ranked_queue.py": (
        "python3 scripts/generate_test_estate_ranked_queue.py --write"
    ),
    "backend/testing/test_test_durations_freshness.py": (
        "regenerate backend/.test_durations from a full timed run "
        "(scripts/durations_quality.py describes the accepted shape)"
    ),
    "backend/testing/test_conversational_workday_protection.py": (
        "update the per-file test_count entries in "
        "backend/testing/conversational_workday_manifest.json to the counts the "
        "failure names"
    ),
    "backend/testing/test_vitest_durations_freshness.py": (
        "regenerate admin-ui/.test_durations.json / agent/.test_durations.json "
        "from a full timed vitest run"
    ),
    "backend/testing/test_event_type_invariants.py": (
        "update backend/testing/event_type_snapshot.json to the event_type "
        "literals the failure names"
    ),
}


def _harness_selector(sentinel: str) -> str:
    """Translate a manifest sentinel into the harness's checkout-relative form.

    scripts/test-backend.sh runs pytest with cwd=backend, so its roster names
    backend tests as ``testing/test_x.py`` while the surface manifest names
    them repo-relative as ``backend/testing/test_x.py``. Both forms may carry a
    ``::node`` suffix, which is preserved.
    """
    normalized = sentinel.replace("\\", "/").strip("/")
    if normalized.startswith("backend/"):
        return normalized[len("backend/"):]
    return normalized


def _uncovered_sentinels(sentinels: list[str], inventory: set[str]) -> list[str]:
    """Matched sentinels the harness-owned standalone corpus does not run.

    ``inventory`` is the harness's own corpus identity as emitted by
    ``scripts/test-backend.sh --standalone --standalone-inventory``, which
    canonicalizes every entry to REPO-relative form (``testing/x.py`` ->
    ``backend/testing/x.py``) so the Docker and direct execution modes compare
    equal. That is the same namespace the surface manifest uses, so the two are
    compared directly and ``_harness_selector`` is only used to phrase the
    repair hint in the form STANDALONE_ARGS actually wants.

    A sentinel is covered when the corpus runs it as a whole file OR runs the
    exact node id registered. The reverse does NOT hold: a corpus entry for one
    node does not cover a whole-file registration, because the file's other
    tests never execute.
    """
    uncovered: list[str] = []
    for sentinel in sentinels:
        if sentinel in inventory:
            continue
        if sentinel_file(sentinel) in inventory:
            continue
        uncovered.append(sentinel)
    return uncovered


def _sentinel_repair_hints(new_failures: dict[str, str]) -> list[str]:
    """Repair lines for every registered sentinel named by a new failure."""
    blob = " ".join([*new_failures.keys(), *(str(v) for v in new_failures.values())])
    hints: list[str] = []
    for sentinel, command in sorted(SENTINEL_REPAIR_COMMANDS.items()):
        basename = sentinel.rsplit("/", 1)[-1]
        if basename in blob:
            hints.append(f"{sentinel} is a DERIVED-ARTIFACT guard — repair: {command}")
    return hints


def _is_size_scope(path: str) -> bool:
    """Whether ``path`` belongs to one of the five governed size sections.

    Size enforcement must not inherit the static-corpus roster's narrower
    surface selection.  The canonical section prefixes come from the same
    policy module used by the checker, so a newly governed section cannot be
    silently omitted from this publication gate.
    """
    normalized = path.replace("\\", "/").strip("/")
    return any(
        normalized == prefix.rstrip("/")
        or normalized.startswith(prefix)
        for prefix in SECTION_PATH_PREFIXES.values()
    )


_extract_base = extract_base


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    if command and command[0] == "bash":
        command = [bash_executable(), *command[1:]]
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def _pytest_result_is_authoritative(
    result: subprocess.CompletedProcess[str],
    report: JUnitReport,
    *,
    authority: str,
) -> bool:
    """Reject an exit/JUnit pair that cannot authorize differential attribution."""

    if is_attributable_pytest_result(result.returncode, report):
        return True
    print(
        f"[static guard] ERROR: {authority} \u2014 pytest exit code/JUnit pair "
        f"is not attributable (exit {result.returncode})",
        file=sys.stderr,
    )
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    return False


def _junit_failures(
    path: Path, *, label: str, tree_root: Path | None = None
) -> dict[str, str]:
    """Compatibility wrapper for callers that only need failed nodes."""
    return parse_junit_report(path, tree_root=tree_root, label=label).failures


def _static_roster_changed(base_harness: Path, current_harness: Path) -> bool:
    """Return whether the harness changed how the static corpus is selected."""
    start_marker = "CI_TEST_MODULE_ROOTS=("
    end_marker = "STATIC_GUARD_SELECTORS_ARGS=()"
    fingerprints = []
    for harness in (base_harness, current_harness):
        source = harness.read_text(encoding="utf-8")
        start = source.find(start_marker)
        end = source.find(end_marker, start + len(start_marker))
        if start < 0 or end < 0:
            return True
        fingerprints.append(source[start:end])
    return fingerprints[0] != fingerprints[1]


def _static_coverage_failures(
    current: JUnitReport,
    baseline: JUnitReport,
    *,
    harness_changed: bool,
    roster_changed: bool,
) -> dict[str, str]:
    """Return independent static-corpus coverage and harness protections."""

    coverage = {
        f"{route}::coverage": detail
        for route, detail in test_route_coverage_gaps(current, baseline).items()
    }
    if harness_changed and baseline.failures:
        subject = "changed static test roster" if roster_changed else "changed static test harness"
        coverage["<harness>"] = f"{subject} runs against a failing base corpus"
    return coverage


def _standalone_corpus_inventory(repo: Path) -> set[str]:
    """Return the harness-owned, checkout-relative standalone corpus identity."""

    result = _run(
        [
            "bash",
            "scripts/test-backend.sh",
            "--standalone",
            "--standalone-inventory",
            "--no-postgres",
        ],
        cwd=repo,
        env={
            **os.environ,
            "BLUEPRINT_STANDALONE_INVENTORY": "1",
            "TESTMON_NO_FETCH": "1",
        },
    )
    if result.returncode != 0:
        raise RuntimeError(
            "standalone corpus inventory failed "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )
    lines = result.stdout.splitlines()
    try:
        start = lines.index("BLUEPRINT_STANDALONE_CORPUS_V1")
        end = lines.index("BLUEPRINT_STANDALONE_CORPUS_END", start + 1)
    except ValueError as exc:
        raise RuntimeError("standalone corpus inventory markers are missing") from exc
    entries = []
    prefix = "BLUEPRINT_STANDALONE_CORPUS_ENTRY="
    for line in lines[start + 1 : end]:
        if not line.startswith(prefix) or not line.removeprefix(prefix):
            raise RuntimeError("standalone corpus inventory contains an invalid entry")
        entries.append(line.removeprefix(prefix))
    if len(entries) != len(set(entries)):
        raise RuntimeError("standalone corpus inventory contains duplicate entries")
    return set(entries)


def _run_static_corpus(
    repo: Path, junit: Path, *, selectors: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "BLUEPRINT_STATIC_GUARD_JUNIT": posix_path(junit),
        "TESTMON_NO_FETCH": "1",
    }
    if selectors is not None:
        encoded = base64.b64encode(("\n".join(selectors) + "\n").encode()).decode()
        env["BLUEPRINT_STATIC_GUARD_SELECTORS"] = encoded
    return _run(
        ["bash", "scripts/test-backend.sh", "--standalone", "--no-postgres"],
        cwd=repo,
        env=env,
    )


def _run_size_guard(repo: Path, base_root: Path) -> subprocess.CompletedProcess[str]:
    # --repo-root is passed EXPLICITLY even though it equals the cwd. It is not
    # redundant: check_size_limits.py keys its frontend escape hatches off
    # whether the caller supplied a root (`use_env_overrides = not
    # repo_root_explicit`). Without the flag the current tree resolves
    # `frontend` through $ADMIN_UI_SRC and the global /admin-ui/src mount BEFORE
    # <repo>/admin-ui/src. Those exist inside the backend test container, so a
    # hermetic fixture repo silently measured the REAL admin-ui tree against the
    # fixture's own (empty) cap ledger and reported every real component as an
    # uncapped offender. The base side already resolved hermetically; only the
    # current side leaked. Real runs are unaffected -- this guard executes on the
    # host, where the mount is absent and <repo>/admin-ui/src is what the
    # fallback selected anyway.
    return _run(
        [
            sys.executable,
            "backend/testing/check_size_limits.py",
            "--repo-root",
            str(repo),
            "--base-root",
            str(base_root),
        ],
        cwd=repo,
        env=os.environ.copy(),
    )


def _task_id() -> str:
    """Return the runner-provided task identity used for guard attribution."""
    return os.environ.get("TASK_ID", "").strip()


_STANDALONE_SCRIPT_TEST_RE = re.compile(r"^scripts/test_[^/]+\.py$")


def _base_file_selectors(selectors: dict[str, str], *, base_root: Path) -> list[str]:
    """Return FILE-level pytest selectors for failed nodes resolvable in base.

    Deliberately file-level rather than node-level.  A failed node that is NEW
    or RENAMED in the current tree has no counterpart in the base tree, and
    handing its node id to pytest is a *usage* error (``ERROR: not found:
    <nodeid>``) that aborts before any test runs: no JUnit report is written, so
    the guard would block with an unattributable corpus failure naming no test.
    Selecting the containing file always resolves, and the new node simply never
    appears in the base report — so it is still correctly charged as new.
    """
    files: set[str] = set()
    for selector in selectors.values():
        relative = selector.split("::", 1)[0]
        if any(
            (source_root / relative).is_file()
            for source_root in (base_root, base_root / "backend")
        ):
            files.add(relative)
    return sorted(files)


def _failed_selectors(
    path: Path, *, tree_root: Path
) -> tuple[dict[str, str], list[str]]:
    """Return selectors for failed JUnit nodes, plus the keys that resolved to none.

    The unresolved keys matter as much as the resolved ones: a failure whose
    selector cannot be derived is invisible to the SCOPED baseline, so its
    counterpart would never appear in ``base_failures`` and the failure would be
    charged as new even when it exists identically in base.  The caller uses a
    non-empty unresolved list as the signal to run the FULL base corpus, which
    is what the pre-scoping guard always did.
    """
    report = parse_junit_report(path, tree_root=tree_root, label="current")
    selectors = {
        key: selector for key, selector in report.selectors.items()
    }
    unresolved = sorted(set(report.failures) - set(selectors))
    return selectors, unresolved


def _attributed_findings(
    findings: dict[str, str], *, task_id: str, source: str
) -> list[dict[str, str]]:
    """Attach the current task to findings, refusing an unknown owner."""
    if findings and not task_id:
        names = json.dumps(sorted(findings))
        raise RuntimeError(
            f"{source} finding(s) cannot be attributed: TASK_ID is missing ({names})"
        )
    return [
        {"task_id": task_id, "test": key, "detail": detail}
        for key, detail in sorted(findings.items())
    ]


LANE = "static"


def _record_suppressions(outcome: dict[str, object]) -> None:
    """Record this lane's suppressions in the cross-lane aggregate (fail-open)."""

    record_classification(LANE, outcome)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="automain")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--size-only",
        action="store_true",
        help="run the always-on module-size check without the standalone corpus",
    )
    mode_group.add_argument(
        "--corpus-only",
        action="store_true",
        help="run the attributed standalone corpus without the module-size check",
    )
    args = parser.parse_args(argv)
    repo = args.repo_root.resolve()
    failures: list[str] = []
    receipt_maps: dict[str, dict[str, object]] = {
        name: {}
        for name in (
            "new_failures",
            "suppressed_failures",
            "disappeared_failures",
            "skipped_failures",
        )
    }
    receipt_extra: dict[str, object] = {}
    receipt_state: str | None = None
    blocking_state: str | None = None
    blocking_error: str | None = None

    def record_static_receipt(state: str, blocking: bool, **values: object) -> None:
        """Merge this run's diagnostic facts into the advisory lane receipt."""

        nonlocal receipt_state
        receipt_state = state
        for name in receipt_maps:
            findings = values.pop(name, None)
            if isinstance(findings, Mapping):
                receipt_maps[name].update(findings)
        metadata = values.pop("extra", None)
        if isinstance(metadata, Mapping):
            receipt_extra.update(metadata)
        receipt_extra.update(
            {name: value for name, value in values.items() if value is not None}
        )
        record_verification_lane(
            LANE_STATIC_CORPUS,
            state=state,
            blocking=blocking,
            new_failures=receipt_maps["new_failures"],
            suppressed_failures=receipt_maps["suppressed_failures"],
            disappeared_failures=receipt_maps["disappeared_failures"],
            skipped_failures=receipt_maps["skipped_failures"],
            extra=receipt_extra,
        )

    def mark_blocking(state: str, error: str, **values: object) -> None:
        """Keep the root block diagnosis through later corpus receipt writes."""

        nonlocal blocking_state, blocking_error
        if blocking_state is None:
            blocking_state = state
            blocking_error = error
        record_static_receipt(state, True, error=error, **values)

    def blocking_exit(state: str, error: str, exit_code: int, **values: object) -> int:
        """Write a fail-open diagnosis immediately before a fail-closed exit."""

        mark_blocking(state, error, guard_exit_code=exit_code, **values)
        return exit_code

    def finish() -> int:
        return blocking_exit(
            blocking_state or receipt_state or "guard_failure",
            blocking_error or f"static guard blocked: {', '.join(failures)}",
            1,
        )

    def runner_metadata(label: str, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        return {
            "guard_exit_code": result.returncode,
            "extra": {f"{label}_exit_code": result.returncode},
        }

    try:
        changed = _changed_files(repo, args.base)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[static guard] ERROR: {exc}", file=sys.stderr)
        return blocking_exit("setup_failure", str(exc), 2)

    size_scoped = [path for path in changed if _is_size_scope(path)]
    corpus_scoped = [path for path in changed if _is_static_scope(path)]
    if args.size_only:
        scoped = size_scoped
    elif args.corpus_only:
        scoped = corpus_scoped
    else:
        scoped = sorted(set(size_scoped) | set(corpus_scoped))
    if not scoped:
        print("[static guard] no static release-guard paths changed; skipping")
        return 0

    print("[static guard] changed scope:")
    for path in scoped:
        print(f"  - {path}")

    task_id = _task_id()
    base_ref = _base_ref(repo, args.base)
    assert base_ref is not None
    with tempfile.TemporaryDirectory(prefix="blueprint-static-guard-") as temp_dir:
        base_root = Path(temp_dir) / "base"
        base_root.mkdir()
        try:
            _extract_base(repo, base_ref, base_root)
        except RuntimeError as exc:
            print(f"[static guard] ERROR: {exc}", file=sys.stderr)
            return blocking_exit("base_extraction_failure", str(exc), 2)

        if args.corpus_only:
            print("[static guard] module size guard SKIPPED (--corpus-only)")
        elif not size_scoped:
            print("[static guard] module size guard SKIPPED (no governed size paths changed)")
        else:
            size_result = _run_size_guard(repo, base_root)
            if size_result.returncode != 0:
                print("[static guard] module size guard FAILED")
                size_failure = {
                    "<module-size-guard>": size_result.stdout + size_result.stderr
                }
                mark_blocking(
                    "size_failure",
                    "module size guard failed",
                    guard_exit_code=size_result.returncode,
                    new_failures=size_failure,
                    extra={"size_guard_exit_code": size_result.returncode},
                )
                try:
                    size_findings = _attributed_findings(
                        size_failure,
                        task_id=task_id,
                        source="module size guard",
                    )
                except RuntimeError as exc:
                    print(f"[static guard] ERROR: {exc}", file=sys.stderr)
                    failures.append("module size guard attribution")
                    mark_blocking("attribution_unavailable", str(exc))
                else:
                    print(f"[static guard] findings: {json.dumps(size_findings)}")
                print(size_result.stdout, end="")
                print(size_result.stderr, end="", file=sys.stderr)
                failures.append("module size guard")
            else:
                print("[static guard] module size guard OK")

        if args.size_only:
            if failures:
                print(
                    f"[static guard] FAIL — {', '.join(failures)}",
                    file=sys.stderr,
                )
                return finish()
            print("[static guard] PASS (size-only)")
            return 0

        if not corpus_scoped:
            print("[static guard] standalone static corpus SKIPPED (no corpus paths changed)")
            if failures:
                print(
                    f"[static guard] FAIL — {', '.join(failures)}",
                    file=sys.stderr,
                )
                return 1
            print("[static guard] PASS (size-only)")
            return 0

        # Each tree must run its own harness and static roster. Replacing the
        # extracted base harness with the task's copy would make a weakened
        # candidate harness weaken the baseline too, hiding exactly the class
        # of change this differential guard is meant to catch.
        base_harness = base_root / "scripts" / "test-backend.sh"
        current_harness = repo / "scripts" / "test-backend.sh"
        if not current_harness.is_file() or not base_harness.is_file():
            print("[static guard] ERROR: scripts/test-backend.sh is unavailable", file=sys.stderr)
            return blocking_exit(
                "harness_unavailable",
                "scripts/test-backend.sh is unavailable",
                2,
            )

        harness_changed = "scripts/test-backend.sh" in corpus_scoped
        roster_changed = _static_roster_changed(base_harness, current_harness)
        # The route helper is an execution dependency mounted alongside the
        # harness; retain the existing fixture/bootstrap behavior for it. The
        # harness and its roster above remain tree-specific.
        current_route = repo / "scripts" / "backend-test-route.sh"
        base_route = base_root / "scripts" / "backend-test-route.sh"
        if current_route.is_file():
            shutil.copy2(current_route, base_route)
            base_route.chmod(base_route.stat().st_mode | 0o111)

        try:
            base_inventory = _standalone_corpus_inventory(base_root)
            current_inventory = _standalone_corpus_inventory(repo)
        except RuntimeError as exc:
            print(f"[static guard] ERROR: cannot inspect standalone corpus: {exc}", file=sys.stderr)
            failures.append("standalone corpus inventory")
            mark_blocking("corpus_inventory_failure", str(exc))
            base_inventory = set()
            current_inventory = set()
        corpus_membership_changed = base_inventory != current_inventory

        # Drift-sentinel reachability. The standalone corpus is a FIXED roster;
        # the surface manifest is a separate, independently edited list. When a
        # sentinel is registered for a surface but is not in the roster, the
        # registration is dead for every change that cannot also reach it
        # through the backend-authoring lane, and the drift it guards merges
        # green until the promotion matrix forces every guard on at once
        # (PR #17738). Check the two lists against each other on every corpus
        # run and fail CLOSED on a mismatch rather than silently skipping.
        matched_sentinels = sorted(surface_sentinels(corpus_scoped, str(repo)))
        if matched_sentinels:
            print("[static guard] drift sentinels matched by this diff:")
            for sentinel in matched_sentinels:
                print(f"  - {sentinel}")
        # A changed backend Python file makes the required backend-authoring
        # lane run, and THAT lane resolves every matched sentinel through
        # impacted_tests.py --mandatory-output. The standalone roster only has
        # to carry the sentinels for a diff that lane never sees, so the check
        # below applies exactly to the diffs the hoist exists for. Deletions are
        # counted as present here: erring toward "reachable" keeps this
        # secondary check from ever inventing a failure, while
        # test_impacted_tests.py's manifest/roster invariant stays
        # unconditional.
        authoring_lane_runs = any(
            path.startswith("backend/") and path.endswith(".py") for path in changed
        )
        if current_inventory and not authoring_lane_runs:
            unreachable = _uncovered_sentinels(matched_sentinels, current_inventory)
            if unreachable:
                print(
                    "[static guard] ERROR: drift sentinel(s) registered in "
                    "backend/testing/drift_sentinel_surfaces.json for this diff are "
                    "NOT in the scripts/test-backend.sh standalone roster, so nothing "
                    "in this lane runs them: "
                    f"{json.dumps(unreachable)}. Add each to STANDALONE_ARGS (harness "
                    "form: "
                    f"{json.dumps([_harness_selector(x) for x in unreachable])}) or "
                    "remove the registration.",
                    file=sys.stderr,
                )
                failures.append("drift sentinel unreachable")
        # File inventory alone cannot see a deleted, renamed, or skipped test
        # node inside a still-selected corpus member.  In that case a scoped
        # baseline of only surviving current failures would omit the baseline
        # node entirely.  Run the complete base corpus whenever either tree's
        # selected source was changed; structured JUnit attribution below then
        # compares the actual failure identities.
        corpus_source_changed = bool(set(changed) & (base_inventory | current_inventory))

        base_junit = Path(temp_dir) / "base.xml"
        current_junit = Path(temp_dir) / "current.xml"
        base_result: subprocess.CompletedProcess[str] | None = None
        current_result = _run_static_corpus(repo, current_junit)
        # Keep finalization safe when the current JUnit report cannot be
        # parsed. The attribution exception records the blocking receipt, and
        # these conservative defaults ensure no later branch can treat that
        # failed parse as authoritative or as a newly attributed failure.
        new_failures: dict[str, str] = {}
        corpus_authoritative = False
        try:
            current_report = parse_junit_report(
                current_junit, tree_root=repo, label="current"
            )
        except RuntimeError as exc:
            print(f"[static guard] ERROR: cannot attribute standalone corpus: {exc}", file=sys.stderr)
            print(current_result.stdout, end="")
            print(current_result.stderr, end="", file=sys.stderr)
            failures.append("standalone static corpus attribution")
            mark_blocking(
                "attribution_unavailable",
                str(exc),
                **runner_metadata("current", current_result),
            )
        else:
            current_failures = current_report.failures
            corpus_authoritative = _pytest_result_is_authoritative(
                current_result,
                current_report,
                authority="execution_failure",
            )
            if not corpus_authoritative:
                failures.append("standalone static corpus execution_failure")
                mark_blocking(
                    "execution_failure",
                    "current standalone corpus result is not attributable",
                    **runner_metadata("current", current_result),
                )
            elif not current_failures:
                # A changed harness is checked against the base corpus, but a
                # normal clean candidate does not need differential subtraction.
                # A changed corpus inventory or a skipped current route is also
                # evidence that a clean report may have erased a baseline
                # failure, so those paths require the full base corpus.
                corpus_execution_changed = (
                    harness_changed
                    or corpus_membership_changed
                    or corpus_source_changed
                    or bool(current_report.skipped_routes)
                )
                if corpus_execution_changed:
                    # The partial roster fingerprint only sharpens the finding's
                    # diagnostic. It can never authorize a clean current report
                    # when the unchanged base harness still exposes failures.
                    base_result = _run_static_corpus(base_root, base_junit)
                    try:
                        baseline_report = parse_junit_report(
                            base_junit, tree_root=base_root, label="base"
                        )
                    except RuntimeError as exc:
                        print(
                            f"[static guard] ERROR: cannot attribute standalone corpus: {exc}",
                            file=sys.stderr,
                        )
                        print(base_result.stdout, end="")
                        print(base_result.stderr, end="", file=sys.stderr)
                        failures.append("standalone static corpus attribution")
                        corpus_authoritative = False
                        mark_blocking(
                            "attribution_unavailable",
                            str(exc),
                            **runner_metadata("baseline", base_result),
                        )
                    else:
                        if not _pytest_result_is_authoritative(
                            base_result,
                            baseline_report,
                            authority="baseline_unavailable",
                        ):
                            failures.append("standalone static corpus baseline_unavailable")
                            corpus_authoritative = False
                            mark_blocking(
                                "baseline_unavailable",
                                "baseline standalone corpus result is not attributable",
                                **runner_metadata("baseline", base_result),
                            )
                        else:
                            verification = classify_verification(
                                runtime_available=True,
                                current_exit_code=current_result.returncode,
                                baseline_report=baseline_report,
                                current_report=current_report,
                            )
                            disappeared = verification["disappeared_failures"]
                            skipped = verification["skipped_failures"]
                            new_failures.update(verification["new_failures"])
                            coverage_failures = (
                                _static_coverage_failures(
                                    current_report,
                                    baseline_report,
                                    harness_changed=harness_changed,
                                    roster_changed=roster_changed,
                                )
                                if corpus_execution_changed
                                else {}
                            )
                            new_failures.update(coverage_failures)
                            if coverage_failures:
                                print(
                                    "[static guard] static corpus coverage regressed: "
                                    f"{json.dumps(coverage_failures, sort_keys=True)}"
                                )
                            corpus_state = (
                                "coverage_regression"
                                if coverage_failures
                                else str(verification["state"])
                            )
                            record_static_receipt(
                                state=corpus_state,
                                blocking=bool(new_failures),
                                new_failures=new_failures,
                                suppressed_failures=verification.get("suppressed_failures"),
                                disappeared_failures=disappeared,
                                skipped_failures=skipped,
                            )
                            _record_suppressions(verification)
            else:
                selectors, unresolved = _failed_selectors(current_junit, tree_root=repo)
                corpus_execution_changed = (
                    harness_changed
                    or corpus_membership_changed
                    or corpus_source_changed
                    or bool(current_report.skipped_routes)
                )
                if corpus_execution_changed:
                    # A changed harness can hide a baseline failure outside the
                    # files that still fail in the current tree.  A baseline
                    # scoped to those surviving failures cannot distinguish
                    # current A from baseline A+B, so only the full base corpus
                    # is authoritative for harness-change attribution.
                    base_selectors = None
                elif unresolved:
                    # At least one current failure has no derivable selector
                    # (e.g. a collection error, which pytest reports with an
                    # empty classname). A scoped baseline cannot cover it, so
                    # it would be charged as new even when base fails the same
                    # way. Fall back to the FULL base corpus — ``None`` selects
                    # everything — which is exactly what the pre-scoping guard
                    # always did.
                    print(
                        "[static guard] WARNING: unresolvable failure id(s) "
                        f"{json.dumps(unresolved)}; running the full base corpus",
                        file=sys.stderr,
                    )
                    base_selectors = None
                else:
                    base_selectors = _base_file_selectors(
                        selectors, base_root=base_root
                    )
                if base_selectors is not None and not base_selectors:
                    # Every failed file is absent from the base tree, so there is
                    # no baseline evidence to gather: all of it is new.
                    baseline_report = JUnitReport()
                else:
                    base_result = _run_static_corpus(
                        base_root, base_junit, selectors=base_selectors
                    )
                    try:
                        baseline_report = parse_junit_report(
                            base_junit, tree_root=base_root, label="base"
                        )
                    except RuntimeError as scoped_exc:
                        # The scoped baseline produced no usable report (e.g. the
                        # diff changed the harness's own roster/routing, so the
                        # base run collapsed). Fall back to the FULL base corpus
                        # rather than blocking with an attribution failure that
                        # names no test — unscoped is slower but still correct.
                        # When the run was ALREADY unscoped there is no broader
                        # baseline left, so fail attribution below instead.
                        if base_selectors is not None:
                            print(
                                "[static guard] WARNING: scoped baseline unusable "
                                f"({scoped_exc}); retrying the full base corpus",
                                file=sys.stderr,
                            )
                            base_junit.unlink(missing_ok=True)
                            base_result = _run_static_corpus(base_root, base_junit)
                        try:
                            baseline_report = parse_junit_report(
                                base_junit, tree_root=base_root, label="base"
                            )
                        except RuntimeError as exc:
                            print(
                                f"[static guard] ERROR: cannot attribute standalone corpus: {exc}",
                                file=sys.stderr,
                            )
                            print(base_result.stdout, end="")
                            print(base_result.stderr, end="", file=sys.stderr)
                            print(current_result.stdout, end="")
                            print(current_result.stderr, end="", file=sys.stderr)
                            failures.append("standalone static corpus attribution")
                            baseline_report = None
                            corpus_authoritative = False
                            mark_blocking(
                                "attribution_unavailable",
                                str(exc),
                                **runner_metadata("baseline", base_result),
                            )
                    if baseline_report is not None:
                        if base_result is not None and not _pytest_result_is_authoritative(
                            base_result,
                            baseline_report,
                            authority="baseline_unavailable",
                        ):
                            failures.append("standalone static corpus baseline_unavailable")
                            corpus_authoritative = False
                            mark_blocking(
                                "baseline_unavailable",
                                "baseline standalone corpus result is not attributable",
                                **runner_metadata("baseline", base_result),
                            )
                        else:
                            outcome = classify_verification(
                                runtime_available=True,
                                current_exit_code=current_result.returncode,
                                current_report=current_report,
                                baseline_report=baseline_report,
                            )
                            new_failures = dict(outcome["new_failures"])
                            disappeared = outcome.get("disappeared_failures") or {}
                            skipped = outcome.get("skipped_failures") or {}
                            coverage_failures = (
                                _static_coverage_failures(
                                    current_report,
                                    baseline_report,
                                    harness_changed=harness_changed,
                                    roster_changed=roster_changed,
                                )
                                if corpus_execution_changed
                                else {}
                            )
                            new_failures.update(coverage_failures)
                            if coverage_failures:
                                print(
                                    "[static guard] static corpus coverage regressed: "
                                    f"{json.dumps(coverage_failures, sort_keys=True)}"
                                )
                            suppressed_failures = outcome.get("suppressed_failures") or {}
                            corpus_state = (
                                "coverage_regression"
                                if coverage_failures
                                else str(outcome["state"])
                            )
                            # Advisory: the receipt records what this lane
                            # relied on; it never decides the lane's verdict.
                            # `new_failures` is used rather than the raw
                            # outcome bucket because the synthetic "<harness>"
                            # finding above is what actually blocks here.
                            record_static_receipt(
                                state=corpus_state,
                                blocking=bool(new_failures),
                                new_failures=new_failures,
                                suppressed_failures=suppressed_failures,
                                disappeared_failures=disappeared,
                                skipped_failures=skipped,
                            )
                            if suppressed_failures:
                                print(
                                    "[static guard] suppressed baseline failures: "
                                    f"{json.dumps(suppressed_failures, sort_keys=True)}"
                                )
                            # Additive and fail-open: the stdout line above is
                            # unchanged, and the receipt never touches the
                            # verdict computed from `outcome`.
                            _record_suppressions(outcome)

            if corpus_authoritative and new_failures:
                try:
                    attributed = _attributed_findings(
                        new_failures, task_id=task_id, source="standalone static corpus"
                    )
                except RuntimeError as exc:
                    print(f"[static guard] ERROR: {exc}", file=sys.stderr)
                    failures.append("standalone static corpus attribution")
                    mark_blocking("attribution_unavailable", str(exc))
                else:
                    print("[static guard] standalone static corpus FAILED for this diff")
                    print(f"[static guard] new failures: {json.dumps(attributed)}")
                    for hint in _sentinel_repair_hints(new_failures):
                        print(f"[static guard] {hint}")
                print(current_result.stdout, end="")
                print(current_result.stderr, end="", file=sys.stderr)
                failures.append("standalone static corpus")
            elif corpus_authoritative:
                print("[static guard] standalone static corpus OK (no new failures vs base)")

    if failures:
        print(f"[static guard] FAIL — {', '.join(failures)}", file=sys.stderr)
        return finish()
    print("[static guard] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

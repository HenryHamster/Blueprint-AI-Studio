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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from testing.fixgate_attribution import (  # noqa: E402
    JUnitReport,
    attribute_reports,
    extract_base,
    parse_junit_report,
)

STATIC_PATH_PREFIXES = (
    "backend/",
    "check",
    "scripts/test-backend.sh",
    "scripts/static_release_guard.py",
)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", *args],
        cwd=repo,
        capture_output=True,
        text=True,
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


def _is_static_scope(path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    return any(
        normalized == prefix.rstrip("/")
        or normalized.startswith(f"{prefix.rstrip('/')}/")
        for prefix in STATIC_PATH_PREFIXES
    )


_extract_base = extract_base


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=False)


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


def _run_static_corpus(
    repo: Path, junit: Path, *, selectors: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "BLUEPRINT_STATIC_GUARD_JUNIT": str(junit),
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
    return _run(
        [
            sys.executable,
            "backend/testing/check_size_limits.py",
            "--base-root",
            str(base_root),
        ],
        cwd=repo,
        env=os.environ.copy(),
    )


def _task_id() -> str:
    """Return the runner-provided task identity used for guard attribution."""
    return os.environ.get("TASK_ID", "").strip()


def _pytest_selector(
    classname: str, name: str, *, tree_root: Path
) -> tuple[str, Path] | None:
    """Translate pytest's JUnit classname/name pair into a repository node id.

    pytest derives ``classname`` from the failing node id relative to its
    rootdir, so a file selected from OUTSIDE rootdir keeps the traversal
    segments — and ``.``-splitting drops the empty ones but keeps any real
    directory name they walked through.  The harness runs pytest from the
    backend directory and selects the ``scripts/`` corpus by path, giving two
    distinct shapes for the same file:

    * non-Docker: ``../scripts/test_x.py`` -> ``...scripts.test_x``
    * Docker (rootdir ``/app``, repo bind-mounted at ``/repo``):
      ``/repo/scripts/test_x.py`` -> ``...repo.scripts.test_x``

    The Docker shape retains the ``repo`` mount segment, which exists in the
    container but not in the tree being resolved.  So resolution walks the
    leading segments off as well as the trailing ones: the first prefix that
    names a real file wins, which handles the ``/repo`` mount (and any future
    mount point) without hard-coding the container layout.

    A COLLECTION error has no test identity: pytest's ``mangle_test_address``
    yields a single element, so ``classname`` is empty and the dotted MODULE
    path lands in ``name`` instead.  That shape is resolved by reading the
    module path out of ``name`` and returning a file-level selector with no
    ``::`` node — otherwise an import error present in both trees would be
    unresolvable here and charged as new.
    """
    parts = [part for part in classname.split(".") if part]
    node = name
    if not parts:
        parts = [part for part in name.split(".") if part]
        node = ""
    for start in range(len(parts)):
        for module_length in range(len(parts), start, -1):
            relative = Path(*parts[start:module_length]).with_suffix(".py")
            for source_root in (tree_root, tree_root / "backend"):
                source = source_root / relative
                if source.is_file():
                    suffix = [part for part in (*parts[module_length:], node) if part]
                    return (
                        f"{relative}{''.join(f'::{part}' for part in suffix)}",
                        source,
                    )
    return None


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="automain")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--size-only",
        action="store_true",
        help="run the always-on module-size check without the standalone corpus",
    )
    args = parser.parse_args(argv)
    repo = args.repo_root.resolve()

    try:
        changed = _changed_files(repo, args.base)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[static guard] ERROR: {exc}", file=sys.stderr)
        return 2

    scoped = [path for path in changed if _is_static_scope(path)]
    if not scoped:
        print("[static guard] no static release-guard paths changed; skipping")
        return 0

    print("[static guard] changed scope:")
    for path in scoped:
        print(f"  - {path}")

    failures: list[str] = []
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
            return 2

        size_result = _run_size_guard(repo, base_root)
        if size_result.returncode != 0:
            print("[static guard] module size guard FAILED")
            try:
                size_findings = _attributed_findings(
                    {"<module-size-guard>": size_result.stdout + size_result.stderr},
                    task_id=task_id,
                    source="module size guard",
                )
            except RuntimeError as exc:
                print(f"[static guard] ERROR: {exc}", file=sys.stderr)
                failures.append("module size guard attribution")
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
            return 2
        harness_changed = "scripts/test-backend.sh" in scoped
        roster_changed = _static_roster_changed(base_harness, current_harness)
        # The route helper is an execution dependency mounted alongside the
        # harness; retain the existing fixture/bootstrap behavior for it. The
        # harness and its roster above remain tree-specific.
        current_route = repo / "scripts" / "backend-test-route.sh"
        base_route = base_root / "scripts" / "backend-test-route.sh"
        if current_route.is_file():
            shutil.copy2(current_route, base_route)
            base_route.chmod(base_route.stat().st_mode | 0o111)

        base_junit = Path(temp_dir) / "base.xml"
        current_junit = Path(temp_dir) / "current.xml"
        base_result: subprocess.CompletedProcess[str] | None = None
        current_result = _run_static_corpus(repo, current_junit)
        try:
            current_report = parse_junit_report(
                current_junit, tree_root=repo, label="current"
            )
        except RuntimeError as exc:
            print(f"[static guard] ERROR: cannot attribute standalone corpus: {exc}", file=sys.stderr)
            print(current_result.stdout, end="")
            print(current_result.stderr, end="", file=sys.stderr)
            failures.append("standalone static corpus attribution")
        else:
            current_failures = current_report.failures
            new_failures: dict[str, str] = {}
            accounted_failures: dict[str, str] = {}
            if not current_failures and current_result.returncode != 0:
                # A harness/import failure has no testcase identity. It is still a
                # real failure, and must not be converted into a green result.
                new_failures = {"<harness>": current_result.stderr or current_result.stdout}
            else:
                # A clean current report still needs a full baseline: a route
                # that vanished or became skipped must be explicitly accounted
                # for rather than silently turning into a green publication.
                selectors, unresolved = _failed_selectors(current_junit, tree_root=repo) if current_failures else ({}, [])
                base_selectors: list[str] | None
                if not current_failures:
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
                    if baseline_report is not None:
                        attributed = attribute_reports(current_report, baseline_report)
                        new_failures = attributed.new_failures
                        accounted_failures = {
                            **{
                                key: f"baseline route identity: route disappeared from current report; {detail}"
                                for key, detail in attributed.disappeared_failures.items()
                            },
                            **{
                                key: f"baseline route identity: route is skipped in current report; {detail}"
                                for key, detail in attributed.skipped_failures.items()
                            },
                        }
                        if harness_changed and roster_changed and baseline_report.failures:
                            accounted_failures["<harness>"] = (
                                "changed static test roster made base corpus "
                                "failure(s) disappear"
                            )
                        if attributed.suppressed_failures:
                            suppressed = {
                                key: {
                                    "detail": detail,
                                    "reason": "same route/test/failure kind failed on baseline",
                                }
                                for key, detail in attributed.suppressed_failures.items()
                            }
                            print(
                                "[static guard] suppressed baseline failures: "
                                f"{json.dumps(suppressed, sort_keys=True)}"
                            )

            if new_failures or accounted_failures:
                try:
                    attributed = _attributed_findings(
                        {**new_failures, **accounted_failures},
                        task_id=task_id,
                        source="standalone static corpus",
                    )
                except RuntimeError as exc:
                    print(f"[static guard] ERROR: {exc}", file=sys.stderr)
                    failures.append("standalone static corpus attribution")
                else:
                    print("[static guard] standalone static corpus FAILED for this diff")
                    print(f"[static guard] new failures: {json.dumps(attributed)}")
                print(current_result.stdout, end="")
                print(current_result.stderr, end="", file=sys.stderr)
                failures.append("standalone static corpus")
            else:
                print("[static guard] standalone static corpus OK (no new failures vs base)")

    if failures:
        print(f"[static guard] FAIL — {', '.join(failures)}", file=sys.stderr)
        return 1
    print("[static guard] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

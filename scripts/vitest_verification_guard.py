#!/usr/bin/env python3
"""Run a Vitest suite and attribute its JUnit failures to an isolated base."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

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
    parse_junit_report,
    _test_key,
    test_route_coverage_gaps,
)
from verification_receipt import record_lane, vitest_lane  # noqa: E402


@dataclass(frozen=True)
class Suite:
    directory: str
    install_directory: str
    # Vitest config, relative to `directory`. None means Vitest's default
    # (`vitest.config.ts`), which is what the two happy-dom unit lanes use.
    config: str | None = None
    # Environment the suite's Vite define block reads at transform time.
    env: tuple[tuple[str, str], ...] = ()
    # Suite-relative path for Vitest's `json` reporter. The guard passes
    # explicit `--reporter` flags, which replace the config's `reporters`
    # array wholesale, so a config-declared JSON manifest has to be restated
    # here or downstream consumers silently lose it.
    json_report: str | None = None
    # Browser-mode suites need a Playwright browser in every tree they run in,
    # including the isolated base checkout.
    requires_browser: bool = False

    @property
    def config_path(self) -> str:
        """Repo-relative path to the config that DEFINES this suite's roster.

        Derived rather than declared so the roster this guard resolves and the
        config Vitest actually runs under can never drift apart. The unit and
        viewport lanes read the same `src/**` tree; their split is exactly the
        include/exclude globs in their own configs, so reading them is what
        keeps a changed viewport test from being attributed against the unit
        config that excludes it (and never actually run).
        """

        return f"{self.directory}/{self.config or 'vitest.config.ts'}"


SUITES = {
    "agent": Suite(
        directory="agent",
        install_directory="agent",
    ),
    "admin-ui": Suite(
        directory="admin-ui",
        install_directory=".",
    ),
    "admin-ui-viewport": Suite(
        directory="admin-ui",
        install_directory=".",
        config="vitest.viewport.config.ts",
        env=(
            ("VITE_API_URL", "http://localhost:8080"),
            ("VITE_FIREBASE_API_KEY", "ci"),
            ("VITE_FIREBASE_AUTH_DOMAIN", "ci.firebaseapp.com"),
            ("VITE_FIREBASE_PROJECT_ID", "ci"),
        ),
        json_report="./test-results/viewport-failures.json",
        requires_browser=True,
    ),
}


@dataclass(frozen=True)
class _Roster:
    """A suite's Vitest test-file roster, resolved from its own config."""

    include: tuple[re.Pattern[str], ...]
    exclude: tuple[re.Pattern[str], ...]


_BRACE_RE = re.compile(r"\{([^{}]*)\}")
_STRING_LITERAL_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def _expand_braces(pattern: str) -> list[str]:
    """Expand one level of `{a,b}` alternation, recursively."""

    match = _BRACE_RE.search(pattern)
    if not match:
        return [pattern]
    prefix, suffix = pattern[: match.start()], pattern[match.end() :]
    expanded: list[str] = []
    for option in match.group(1).split(","):
        expanded.extend(_expand_braces(f"{prefix}{option}{suffix}"))
    return expanded


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a Vitest/minimatch-style glob into an anchored regex."""

    parts: list[str] = []
    i = 0
    length = len(pattern)
    while i < length:
        remainder = pattern[i:]
        if remainder.startswith("**/"):
            parts.append("(?:.*/)?")
            i += 3
        elif remainder.startswith("**"):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def _compile_globs(patterns: list[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(
        _glob_to_regex(expanded)
        for pattern in patterns
        for expanded in _expand_braces(pattern)
    )


def _extract_string_array(source: str, key: str) -> list[str]:
    """Extract a flat array-of-string-literals config value by key."""

    match = re.search(rf"\b{re.escape(key)}\s*:\s*\[(.*?)\]", source, re.DOTALL)
    if not match:
        return []
    return [single or double for single, double in _STRING_LITERAL_RE.findall(match.group(1))]


def _load_roster(repo: Path, suite: Suite) -> _Roster:
    """Resolve a suite's authoritative test-file roster from its own Vitest config.

    This must stay a single source of truth: the guard previously carried a
    hand-maintained copy of these globs, which could drift from the config
    (or omit its exclude half) and let a config-only change suppress a
    baseline failure without the guard ever noticing.
    """

    config_file = repo / suite.config_path
    if not config_file.is_file():
        raise RuntimeError(f"cannot resolve Vitest roster: missing {suite.config_path}")
    source = config_file.read_text(encoding="utf-8")
    include = _extract_string_array(source, "include")
    if not include:
        raise RuntimeError(
            f"cannot resolve Vitest roster: no include globs found in {suite.config_path}"
        )
    exclude = _extract_string_array(source, "exclude")
    return _Roster(include=_compile_globs(include), exclude=_compile_globs(exclude))

_EXECUTION_REPORT_ROUTE = "blueprint.vitest.execution"
_EXECUTION_REPORT_RUNNER = f"{_EXECUTION_REPORT_ROUTE}::runner"
_EXECUTION_REPORT_FAILURE = f"{_EXECUTION_REPORT_ROUTE}::unhandled_error::error"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def _base_ref(repo: Path, base: str) -> str:
    for candidate in (f"origin/{base}", base):
        if _git(repo, "rev-parse", "--verify", candidate).returncode == 0:
            return candidate
    raise RuntimeError(f"cannot resolve base ref origin/{base} or {base}")


def _changed_files(repo: Path, base_ref: str) -> list[str]:
    """Return committed, working-tree, and untracked paths in the task diff."""

    commands = (
        ("diff", "--name-only", f"{base_ref}...HEAD"),
        ("diff", "--name-only", "HEAD"),
        ("ls-files", "--others", "--exclude-standard"),
    )
    changed: set[str] = set()
    for command in commands:
        result = _git(repo, *command)
        if result.returncode != 0:
            raise RuntimeError(
                f"cannot compute changed files with git {' '.join(command)}"
            )
        changed.update(path for path in result.stdout.splitlines() if path)
    return sorted(changed)


def _is_suite_test_file(path: str, suite: Suite, roster: _Roster) -> bool:
    """Return whether a repo-relative path belongs to this suite's Vitest roster."""

    normalized = path.replace("\\", "/").strip("/")
    prefix = f"{suite.directory}/"
    if not normalized.startswith(prefix):
        return False
    relative = normalized[len(prefix) :]
    if not any(pattern.match(relative) for pattern in roster.include):
        return False
    return not any(pattern.match(relative) for pattern in roster.exclude)


def _changed_test_files(repo: Path, base_ref: str, suite: Suite, roster: _Roster) -> list[str]:
    """Return changed repo-relative test files selected by this suite's roster."""

    return [
        path for path in _changed_files(repo, base_ref) if _is_suite_test_file(path, suite, roster)
    ]


def _runnable_changed_test_files(
    repo: Path, base_ref: str, suite: Suite, roster: _Roster
) -> list[str]:
    """Return changed suite test files the current side can still execute.

    `_changed_test_files` reports the raw diff, which names deleted and
    pre-rename paths too. Those cannot appear in a caller's scope -- the tree
    no longer holds them, so `_current_file_selectors` rejects them -- and a
    test the diff removes has no current-side behavior left to prove. Keeping
    them would make a delete or rename an unclearable scope block. Their
    baseline failures are still handled downstream: the unscoped
    `changed_test_files` roster runs them against the extracted base tree.
    """

    return [
        path
        for path in _changed_test_files(repo, base_ref, suite, roster)
        if (repo / path).is_file()
    ]


def _base_file_selectors(
    paths: list[str], suite: Suite, roster: _Roster, base_root: Path
) -> list[str]:
    """Return suite-relative selectors for changed or failed files present in base."""

    selectors: set[str] = set()
    prefix = f"{suite.directory}/"
    for path in paths:
        normalized = path.replace("\\", "/").strip("/")
        if not normalized.startswith(prefix):
            normalized = f"{prefix}{normalized}"
        if not _is_suite_test_file(normalized, suite, roster):
            continue
        relative = normalized[len(prefix) :]
        if (base_root / normalized).is_file():
            selectors.add(relative)
    return sorted(selectors)


def _current_file_selectors(
    paths: list[str], suite: Suite, roster: _Roster, repo: Path
) -> list[str]:
    """Return validated suite-relative selectors for an explicitly scoped run.

    A caller-supplied scope narrows what the current side executes, so an entry
    the suite would not run at all -- wrong roster, or missing from the tree --
    has to raise rather than quietly shrink the executed set.
    """

    if not paths:
        raise RuntimeError("--current-files was passed with no test file")
    prefix = f"{suite.directory}/"
    selectors: set[str] = set()
    for path in paths:
        normalized = path.replace("\\", "/").strip("/")
        if not normalized.startswith(prefix):
            normalized = f"{prefix}{normalized}"
        if not _is_suite_test_file(normalized, suite, roster):
            raise RuntimeError(f"--current-files entry is not a suite test file: {path}")
        if not (repo / normalized).is_file():
            raise RuntimeError(f"--current-files entry does not exist: {path}")
        selectors.add(normalized[len(prefix) :])
    return sorted(selectors)


def _route_source(test_route: str) -> str:
    """Return the suite-relative source file from a Vitest test route."""

    return test_route.split("::", 1)[0]


def _coverage_failures(
    current_report: JUnitReport,
    baseline_report: JUnitReport,
    current_selectors: list[str] | None,
) -> dict[str, str]:
    """Check baseline-failing routes without confusing repair with deletion.

    An unscoped run covers the whole suite. For an explicitly scoped run, the
    baseline must be made from the same file scope; if it is not, fail closed
    rather than silently dropping an out-of-scope baseline failure.
    """

    if current_selectors is not None:
        current_scope = set(current_selectors)
        baseline_scope = {
            _route_source(_test_key(key)) for key in baseline_report.failures
        }
        outside_scope = sorted(baseline_scope - current_scope)
        if outside_scope:
            raise RuntimeError(
                "baseline report contains failing routes outside current scope: "
                + ",".join(outside_scope)
            )
    return cast(dict[str, str], test_route_coverage_gaps(current_report, baseline_report))


def _tail(output: str, *, lines: int = 80) -> str:
    return "\n".join(output.splitlines()[-lines:])


def _run_vitest(
    root: Path,
    suite: Suite,
    report: Path,
    execution_report: Path,
    selectors: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = ["npx", "vitest", "run"]
    if suite.config:
        command.append(f"--config={suite.config}")
    if selectors:
        command.extend(selectors)
    command.extend(
        [
            "--reporter=default",
            "--reporter=junit",
            f"--reporter={REPO_ROOT / 'scripts' / 'vitest_execution_reporter.mjs'}",
            f"--outputFile.junit={report}",
        ]
    )
    if suite.json_report:
        # Restated because the CLI reporter flags above replace the config's
        # own `reporters` array; the screenshot-upload block downstream reads
        # this manifest.
        command.extend(
            ["--reporter=json", f"--outputFile.json={suite.json_report}"]
        )
    return subprocess.run(
        command,
        cwd=root / suite.directory,
        env={
            **os.environ,
            **dict(suite.env),
            "BLUEPRINT_VITEST_EXECUTION_JUNIT": str(execution_report),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _install_base(root: Path, suite: Suite) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["npm", "ci"],
        cwd=root / suite.install_directory,
        capture_output=True,
        text=True,
        check=False,
    )


def _install_base_browser(root: Path, suite: Suite) -> subprocess.CompletedProcess[str]:
    """Ensure the extracted base tree can launch the suite's Playwright browser.

    Mirrors the gate's cache-check-then-install pattern. A failure here makes
    the baseline unobservable, which is a blocking condition -- never a reason
    to waive the current side.
    """

    cache = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright"
    )
    if cache.is_dir() and any(cache.glob("chromium-*")):
        return subprocess.CompletedProcess([], 0, stdout="chromium already cached", stderr="")
    return subprocess.run(
        ["npx", "playwright", "install", "chromium"],
        cwd=root / suite.directory,
        capture_output=True,
        text=True,
        check=False,
    )


def _print_result(label: str, result: subprocess.CompletedProcess[str]) -> None:
    output = _tail(f"{result.stdout}\n{result.stderr}")
    if output:
        print(f"[vitest-verification] {label} output:\n{output}")


def _blocking(state: str, **extra: object) -> int:
    detail = " ".join(f"{key}={value}" for key, value in extra.items())
    print(f"[vitest-verification] FAILED state={state}{(' ' + detail) if detail else ''}")
    return 1


def _record(
    suite_name: str,
    outcome: dict[str, object],
    *,
    coverage_failures: dict[str, str] | None = None,
) -> None:
    """Persist this lane's attribution to the shared multi-lane sidecar.

    Advisory by construction: `record_lane` never raises and its return value
    is deliberately not consulted, so a receipt failure cannot turn this lane
    red (or green).
    """

    coverage_failures = coverage_failures or {}
    new_failures = dict(cast(dict[str, str], outcome.get("new_failures") or {}))
    new_failures.update(
        {f"{route}::coverage": detail for route, detail in coverage_failures.items()}
    )
    record_lane(
        vitest_lane(suite_name),
        state="coverage_regression" if coverage_failures else str(outcome["state"]),
        blocking=bool(outcome.get("blocking", False) or coverage_failures),
        new_failures=new_failures,
        suppressed_failures=outcome.get("suppressed_failures"),
        disappeared_failures=outcome.get("disappeared_failures"),
        skipped_failures=outcome.get("skipped_failures"),
    )


def _execution_errors(report) -> bool:
    """Validate and return the runner-level errors emitted by our reporter."""

    allowed_routes = {
        _EXECUTION_REPORT_RUNNER,
        _EXECUTION_REPORT_FAILURE.rsplit("::", 1)[0],
    }
    if report.test_routes - allowed_routes or report.skipped_routes:
        raise RuntimeError("Vitest execution report contains unexpected test identities")
    if set(report.failures) - {_EXECUTION_REPORT_FAILURE}:
        raise RuntimeError("Vitest execution report contains unexpected failure identities")
    if _EXECUTION_REPORT_RUNNER not in report.test_routes:
        raise RuntimeError("Vitest execution report is missing its runner identity")
    return _EXECUTION_REPORT_FAILURE in report.failures


def _valid_exit_report_pair(exit_code: int, failures: dict[str, str]) -> bool:
    """Return whether Vitest's process exit agrees with its named failures."""

    return (exit_code == 0 and not failures) or (exit_code == 1 and bool(failures))


def lane_name(suite: str) -> str:
    """Return this lane's stable identity in the cross-lane aggregate."""

    return f"vitest:{suite}"


def _record_suppressions(suite: str, outcome: dict[str, object]) -> None:
    """Record this lane's suppressions in the cross-lane aggregate (fail-open)."""

    record_classification(lane_name(suite), outcome)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=sorted(SUITES), required=True)
    parser.add_argument("--base", default="automain")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--current-files",
        nargs="*",
        default=None,
        metavar="TEST_FILE",
        help=(
            "Restrict the CURRENT-side run to these suite test files. Serialized "
            "browser-mode suites cannot run their whole roster in the task gate. "
            "Base-side scoping is unchanged; identity comparison stays per test."
        ),
    )
    args = parser.parse_args(argv)
    repo = args.repo_root.resolve()
    suite = SUITES[args.suite]

    current_selectors: list[str] | None = None
    if args.current_files is not None:
        try:
            # The roster comes from the suite's own Vitest config, so a scope
            # entry is validated against the same include/exclude globs the
            # runner would apply -- not a second, drift-prone copy of them.
            scope_roster = _load_roster(repo, suite)
            current_selectors = _current_file_selectors(
                args.current_files, suite, scope_roster, repo
            )
            scoped_out = sorted(
                set(
                    _runnable_changed_test_files(
                        repo, _base_ref(repo, args.base), suite, scope_roster
                    )
                )
                - {f"{suite.directory}/{relative}" for relative in current_selectors}
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return _blocking("current_scope_unavailable", error=str(exc))
        if scoped_out:
            # The caller's scope omits a suite test file this diff changed, so
            # a clean scoped run would prove nothing about it.
            return _blocking(
                "current_scope_incomplete", unrun_changed_test_files=",".join(scoped_out)
            )

    with tempfile.TemporaryDirectory(prefix="blueprint-vitest-verification-") as temp_dir:
        temp = Path(temp_dir)
        current_junit = temp / "current.xml"
        current_execution_junit = temp / "current-execution.xml"
        # Keep the unscoped call shape byte-identical for the two unit lanes;
        # only an explicitly scoped caller gets the selector argument.
        if current_selectors is None:
            current = _run_vitest(repo, suite, current_junit, current_execution_junit)
        else:
            current = _run_vitest(
                repo,
                suite,
                current_junit,
                current_execution_junit,
                selectors=current_selectors,
            )
        _print_result(f"{args.suite} current", current)
        try:
            current_report = parse_junit_report(
                current_junit, tree_root=repo, label="current"
            )
            current_execution_error = _execution_errors(
                parse_junit_report(
                    current_execution_junit, tree_root=repo, label="current execution"
                )
            )
        except RuntimeError as exc:
            return _blocking("attribution_unavailable", error=str(exc))

        if not _valid_exit_report_pair(current.returncode, current_report.failures):
            return _blocking(
                "execution_failure",
                current_exit_code=current.returncode,
                current_failure_count=len(current_report.failures),
            )

        current_is_clean = (
            current.returncode == 0
            and not current_report.failures
            and not current_execution_error
        )
        try:
            base_ref = _base_ref(repo, args.base)
            roster = _load_roster(repo, suite)
            if current_is_clean:
                changed_test_files = _changed_test_files(repo, base_ref, suite, roster)
                roster_config_changed = suite.config_path in _changed_files(repo, base_ref)
            else:
                changed_test_files = []
                roster_config_changed = False
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return _blocking("baseline_unavailable", error=str(exc))

        # A clean current run with no changed test file, and no change to the
        # config that DEFINES the roster, cannot have made a baseline failure
        # disappear or become skipped. Keep this cheap path, but only after
        # proving the suite's test-file diff (roster included) is empty.
        if (
            current_is_clean
            and not changed_test_files
            and not roster_config_changed
            and not current_report.skipped_routes
        ):
            outcome = classify_verification(
                runtime_available=True,
                current_exit_code=current.returncode,
                current_report=current_report,
            )
            state = str(outcome["state"])
            _record(args.suite, outcome)
            if bool(outcome["blocking"]):
                return _blocking(state)
            print(f"[vitest-verification] PASS state={state}")
            return 0

        try:
            base_root = temp / "base"
            base_root.mkdir()
            extract_base(repo, base_ref, base_root)
            if current_is_clean:
                if current_report.skipped_routes:
                    # A clean exit with skipped tests cannot prove that the
                    # candidate preserved the base suite's executable scope.
                    print(
                        "[vitest-verification] baseline scoped to the full suite: "
                        "current report contains skipped test routes"
                    )
                    base_selectors = None
                elif roster_config_changed:
                    # The roster itself moved (an include/exclude glob
                    # changed). We cannot safely narrow to individual files -
                    # a newly excluded file wouldn't even be in this list -
                    # so run the full suite against base under base's own
                    # config and let route-keyed attribution catch anything
                    # the new roster silently dropped.
                    print(
                        f"[vitest-verification] baseline scoped to the full suite: "
                        f"{suite.config_path} changed"
                    )
                    base_selectors = None
                else:
                    base_selectors = _base_file_selectors(
                        changed_test_files, suite, roster, base_root
                    )
            else:
                failed_file_paths = [
                    selector.split("::", 1)[0]
                    for selector in current_report.selectors.values()
                ]
                base_selectors = (
                    _base_file_selectors(failed_file_paths, suite, roster, base_root)
                    if current_report.selectors
                    else None
                )
            baseline_junit = temp / "baseline.xml"
            baseline_execution_junit = temp / "baseline-execution.xml"
            if base_selectors is not None and not base_selectors:
                # Every changed test file is new in the candidate tree. There
                # is no base-side test to execute, so an empty report is the
                # complete baseline evidence and npm ci is unnecessary.
                print(
                    "[vitest-verification] baseline skipped: no changed suite test files exist in base"
                )
                baseline_report = JUnitReport()
                baseline_execution_error = False
            else:
                install = _install_base(base_root, suite)
                _print_result(f"{args.suite} baseline install", install)
                if install.returncode != 0:
                    return _blocking(
                        "baseline_unavailable", base_install_exit_code=install.returncode
                    )
                if suite.requires_browser:
                    browser = _install_base_browser(base_root, suite)
                    _print_result(f"{args.suite} baseline browser", browser)
                    if browser.returncode != 0:
                        return _blocking(
                            "baseline_unavailable",
                            base_browser_install_exit_code=browser.returncode,
                        )
                if base_selectors is None:
                    baseline = _run_vitest(
                        base_root, suite, baseline_junit, baseline_execution_junit
                    )
                else:
                    baseline = _run_vitest(
                        base_root,
                        suite,
                        baseline_junit,
                        baseline_execution_junit,
                        selectors=base_selectors,
                    )
                _print_result(f"{args.suite} baseline", baseline)
                baseline_report = parse_junit_report(
                    baseline_junit, tree_root=base_root, label="baseline"
                )
                baseline_execution_error = _execution_errors(
                    parse_junit_report(
                        baseline_execution_junit,
                        tree_root=base_root,
                        label="baseline execution",
                    )
                )
                if baseline_execution_error:
                    return _blocking("baseline_execution_failure", base_exit_code=baseline.returncode)
                if not _valid_exit_report_pair(
                    baseline.returncode, baseline_report.failures
                ):
                    return _blocking(
                        "baseline_unavailable",
                        base_exit_code=baseline.returncode,
                        base_failure_count=len(baseline_report.failures),
                    )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return _blocking("baseline_unavailable", error=str(exc))

        outcome = classify_verification(
            runtime_available=True,
            current_exit_code=current.returncode,
            current_report=current_report,
            baseline_report=baseline_report,
        )
        try:
            coverage_failures = _coverage_failures(
                current_report, baseline_report, current_selectors
            )
        except RuntimeError as exc:
            return _blocking("coverage_scope_unavailable", error=str(exc))
        _record(args.suite, outcome, coverage_failures=coverage_failures)
        for key in sorted(outcome.get("suppressed_failures", {})):
            print(f"[vitest-verification] WARN suppressed baseline failure: {key}")
        # Additive and fail-open: the WARN lines above are unchanged and the
        # receipt cannot affect the state/exit decision made below.
        _record_suppressions(args.suite, outcome)
        if current_execution_error:
            return _blocking("execution_failure")
        if coverage_failures:
            return _blocking(
                "coverage_regression",
                test_routes=",".join(sorted(coverage_failures)),
            )
        state = str(outcome["state"])
        if bool(outcome["blocking"]):
            return _blocking(state)
        print(f"[vitest-verification] PASS state={state}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

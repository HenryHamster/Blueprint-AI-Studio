#!/usr/bin/env python3
"""Run a Vitest suite and attribute its JUnit failures to an isolated base."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from testing.fixgate_attribution import (  # noqa: E402
    classify_verification,
    extract_base,
    parse_junit_report,
)


@dataclass(frozen=True)
class Suite:
    directory: str
    install_directory: str


SUITES = {
    "agent": Suite(directory="agent", install_directory="agent"),
    "admin-ui": Suite(directory="admin-ui", install_directory="."),
}

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


def _tail(output: str, *, lines: int = 80) -> str:
    return "\n".join(output.splitlines()[-lines:])


def _run_vitest(
    root: Path, suite: Suite, report: Path, execution_report: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "npx",
            "vitest",
            "run",
            "--reporter=default",
            "--reporter=junit",
            f"--reporter={REPO_ROOT / 'scripts' / 'vitest_execution_reporter.mjs'}",
            f"--outputFile.junit={report}",
        ],
        cwd=root / suite.directory,
        env={
            **os.environ,
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


def _print_result(label: str, result: subprocess.CompletedProcess[str]) -> None:
    output = _tail(f"{result.stdout}\n{result.stderr}")
    if output:
        print(f"[vitest-verification] {label} output:\n{output}")


def _blocking(state: str, **extra: object) -> int:
    detail = " ".join(f"{key}={value}" for key, value in extra.items())
    print(f"[vitest-verification] FAILED state={state}{(' ' + detail) if detail else ''}")
    return 1


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=sorted(SUITES), required=True)
    parser.add_argument("--base", default="automain")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    repo = args.repo_root.resolve()
    suite = SUITES[args.suite]

    with tempfile.TemporaryDirectory(prefix="blueprint-vitest-verification-") as temp_dir:
        temp = Path(temp_dir)
        current_junit = temp / "current.xml"
        current_execution_junit = temp / "current-execution.xml"
        current = _run_vitest(repo, suite, current_junit, current_execution_junit)
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

        # A clean current run needs no differential attribution. Avoid the
        # expensive base archive, dependency install, and second Vitest run.
        if (
            current.returncode == 0
            and not current_report.failures
            and not current_execution_error
        ):
            outcome = classify_verification(
                runtime_available=True,
                current_exit_code=current.returncode,
                current_failures=current_report.failures,
            )
            state = str(outcome["state"])
            if bool(outcome["blocking"]):
                return _blocking(state)
            print(f"[vitest-verification] PASS state={state}")
            return 0

        try:
            base_root = temp / "base"
            base_root.mkdir()
            base_ref = _base_ref(repo, args.base)
            extract_base(repo, base_ref, base_root)
            install = _install_base(base_root, suite)
            _print_result(f"{args.suite} baseline install", install)
            if install.returncode != 0:
                return _blocking("baseline_unavailable", base_install_exit_code=install.returncode)
            baseline_junit = temp / "baseline.xml"
            baseline_execution_junit = temp / "baseline-execution.xml"
            baseline = _run_vitest(
                base_root, suite, baseline_junit, baseline_execution_junit
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
            current_failures=current_report.failures,
            current_report=current_report,
            baseline_report=baseline_report,
        )
        for key in sorted(outcome.get("suppressed_failures", {})):
            print(f"[vitest-verification] WARN suppressed baseline failure: {key}")
        if current_execution_error:
            return _blocking("execution_failure")
        state = str(outcome["state"])
        if bool(outcome["blocking"]):
            return _blocking(state)
        print(f"[vitest-verification] PASS state={state}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

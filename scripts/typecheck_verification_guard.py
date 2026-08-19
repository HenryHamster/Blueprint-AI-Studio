#!/usr/bin/env python3
"""Run a suite's `tsc -b --noEmit` build and attribute failures to a base ref.

Mirrors scripts/vitest_verification_guard.py's fail-closed, route-keyed
attribution contract, but for TypeScript project-build diagnostics instead of
JUnit test reports: a diagnostic inherited unchanged from the base ref warn
passes; a genuinely new diagnostic still blocks.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "backend"))

try:
    from guard_suppression_receipt import record_classification  # noqa: E402
except ImportError:  # pragma: no cover - emitting is fail-open by contract
    # A tree without the emitter (an older base checkout, a synthetic harness
    # fixture) must still run this lane unchanged: recording suppressions can
    # never be the reason a verdict, exit code or blocking decision moves.
    def record_classification(*_args: object, **_kwargs: object) -> None:
        return None
from testing.fixgate_attribution import (  # noqa: E402
    classify_verification,
    extract_base,
)


@dataclass(frozen=True)
class Suite:
    directory: str
    install_directory: str
    command: tuple[str, ...]


SUITES = {
    "admin-ui": Suite(
        directory="admin-ui",
        install_directory="admin-ui",
        command=("npx", "tsc", "-b", "--noEmit"),
    ),
}

# tsc's default diagnostic line format:
#   src/components/Foo.tsx(12,5): error TS2322: Type 'X' is not assignable to type 'Y'.
_TSC_DIAGNOSTIC_RE = re.compile(
    r"^(?P<file>[^\s(].*?)\((?P<line>\d+),\d+\): error (?P<code>TS\d+): (?P<message>.*)$"
)

# Every diagnostic tsc emits carries an ``error TS<code>`` token, including the
# file-less ones (``error TS18003: No inputs were found in config file ...``,
# ``error TS5083: Cannot read file ...``) that have no ``file(line,col)``
# anchor and therefore cannot be attributed to a route.  Counting these is how
# the lane detects that its parser saw less than tsc reported.
_TSC_ANY_ERROR_RE = re.compile(r"\berror TS\d+\b")


@dataclass(frozen=True)
class TscDiagnostics:
    """Attributable diagnostics plus the accounting needed to fail closed."""

    failures: dict[str, str]
    locations: dict[str, tuple[str, ...]]
    parsed_lines: int
    total_lines: int
    unparsed: tuple[str, ...]


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


def _run_tsc(root: Path, suite: Suite) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(suite.command),
        cwd=root / suite.directory,
        capture_output=True,
        text=True,
        check=False,
    )


def _install(root: Path, suite: Suite) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["npm", "ci"],
        cwd=root / suite.install_directory,
        capture_output=True,
        text=True,
        check=False,
    )


def _normalize_message(message: str) -> str:
    """Collapse whitespace so a re-wrapped diagnostic keeps one identity."""

    return " ".join(message.split())


def parse_tsc_failures(output: str) -> TscDiagnostics:
    """Parse `tsc -b --noEmit` output into a route-keyed failure map.

    The route key is ``file:TScode:message`` and deliberately excludes the
    line/column: an inherited diagnostic must keep its identity when unrelated
    edits above it shift its line, otherwise whichever task next touches the
    file gets blamed for pre-existing breakage.  Lines that announce an error
    but carry no ``file(line,col)`` anchor are unattributable and are returned
    in ``unparsed`` so the caller can fail closed instead of dropping them.
    """

    failures: dict[str, str] = {}
    locations: dict[str, tuple[str, ...]] = {}
    parsed_lines = 0
    total_lines = 0
    unparsed: list[str] = []
    for line in output.splitlines():
        if not _TSC_ANY_ERROR_RE.search(line):
            continue
        total_lines += 1
        match = _TSC_DIAGNOSTIC_RE.match(line)
        if not match:
            unparsed.append(line.strip())
            continue
        parsed_lines += 1
        message = _normalize_message(match.group("message"))
        key = f"{match.group('file')}:{match.group('code')}:{message}"
        failures[key] = message
        location = f"{match.group('file')}({match.group('line')})"
        if location not in locations.get(key, ()):
            locations[key] = (*locations.get(key, ()), location)
    return TscDiagnostics(
        failures=failures,
        locations=locations,
        parsed_lines=parsed_lines,
        total_lines=total_lines,
        unparsed=tuple(unparsed),
    )


def _tail(output: str, *, lines: int = 80) -> str:
    return "\n".join(output.splitlines()[-lines:])


def _print_result(label: str, result: subprocess.CompletedProcess[str]) -> None:
    output = _tail(f"{result.stdout}\n{result.stderr}")
    if output:
        print(f"[typecheck-verification] {label} output:\n{output}")


def _locations(diagnostics: TscDiagnostics, key: str) -> str:
    """Render the source locations a route key was observed at, if any."""

    found = diagnostics.locations.get(key, ())
    return f" at {', '.join(found)}" if found else ""


def _blocking(state: str, **extra: object) -> int:
    detail = " ".join(f"{key}={value}" for key, value in extra.items())
    print(f"[typecheck-verification] FAILED state={state}{(' ' + detail) if detail else ''}")
    return 1


def lane_name(suite: str) -> str:
    """Return this lane's stable identity in the cross-lane aggregate."""

    return f"typecheck:{suite}"


def _record_suppressions(suite: str, outcome: dict[str, object]) -> None:
    """Record this lane's suppressions in the cross-lane aggregate (fail-open)."""

    record_classification(lane_name(suite), outcome)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=sorted(SUITES), default="admin-ui")
    parser.add_argument("--base", default="automain")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    repo = args.repo_root.resolve()
    suite = SUITES[args.suite]

    current = _run_tsc(repo, suite)
    _print_result(f"{args.suite} current", current)
    current_diagnostics = parse_tsc_failures(current.stdout + current.stderr)
    current_failures = current_diagnostics.failures

    if current_diagnostics.unparsed:
        # tsc reported more errors than the lane could attribute to a route.
        # Suppressing the remainder would let a genuinely new type error ride
        # alongside an inherited one and publish green, so fail closed.
        for line in current_diagnostics.unparsed:
            print(f"[typecheck-verification] unattributable diagnostic: {line}")
        return _blocking(
            "unattributable_diagnostic",
            parsed_diagnostics=current_diagnostics.parsed_lines,
            total_diagnostics=current_diagnostics.total_lines,
            current_exit_code=current.returncode,
        )

    if current.returncode == 0 and not current_failures:
        print("[typecheck-verification] PASS state=passed")
        return 0

    if current.returncode != 0 and not current_failures:
        # tsc failed but produced no parseable diagnostics: an infra/execution
        # problem, not an attributable type error. Fail closed.
        return _blocking("execution_failure", current_exit_code=current.returncode)

    try:
        base_ref = _base_ref(repo, args.base)
    except RuntimeError as exc:
        return _blocking("baseline_unavailable", error=str(exc))

    with tempfile.TemporaryDirectory(prefix="blueprint-typecheck-verification-") as temp_dir:
        base_root = Path(temp_dir) / "base"
        base_root.mkdir()
        try:
            extract_base(repo, base_ref, base_root)
        except RuntimeError as exc:
            return _blocking("baseline_unavailable", error=str(exc))

        install = _install(base_root, suite)
        _print_result(f"{args.suite} baseline install", install)
        if install.returncode != 0:
            return _blocking(
                "baseline_unavailable", base_install_exit_code=install.returncode
            )

        baseline = _run_tsc(base_root, suite)
        _print_result(f"{args.suite} baseline", baseline)
        baseline_diagnostics = parse_tsc_failures(baseline.stdout + baseline.stderr)
        baseline_failures = baseline_diagnostics.failures
        if baseline_diagnostics.unparsed:
            # An unattributable baseline diagnostic means the baseline failure
            # set is incomplete, so inherited breakage can't be established.
            for line in baseline_diagnostics.unparsed:
                print(
                    "[typecheck-verification] unattributable baseline diagnostic: "
                    f"{line}"
                )
            return _blocking(
                "baseline_unavailable",
                parsed_baseline_diagnostics=baseline_diagnostics.parsed_lines,
                total_baseline_diagnostics=baseline_diagnostics.total_lines,
            )
        if baseline.returncode != 0 and not baseline_failures:
            return _blocking(
                "baseline_unavailable", base_exit_code=baseline.returncode
            )

    outcome = classify_verification(
        runtime_available=True,
        current_exit_code=current.returncode,
        current_failures=current_failures,
        baseline_failures=baseline_failures,
    )
    for key in sorted(outcome.get("suppressed_failures", {})):
        print(
            "[typecheck-verification] WARN suppressed baseline failure: "
            f"{key}{_locations(current_diagnostics, key)}"
        )
    # Additive and fail-open: stdout above is unchanged and the receipt is
    # written before, and independent of, the blocking decision below.
    _record_suppressions(args.suite, outcome)
    disappeared = outcome.get("disappeared_failures", {})
    if not isinstance(disappeared, dict):  # pragma: no cover - contract guard
        disappeared = {}
    for key in sorted(disappeared):
        print(
            "[typecheck-verification] NOTE baseline diagnostic no longer reported: "
            f"{key}{_locations(baseline_diagnostics, key)}"
        )
    new_failures = outcome.get("new_failures") or {}
    state = str(outcome["state"])
    blocking = bool(outcome["blocking"])
    if blocking and state == "baseline_failure_disappeared" and not new_failures:
        # attribute_reports blocks a vanished failure as pytest anti-gaming (a
        # failing test can be deleted).  A vanished tsc diagnostic means the
        # type error was fixed or the code legitimately removed, which cannot
        # be gamed: tsc still reports every error in whatever code remains.
        blocking = False
    if blocking:
        for key in sorted(new_failures):
            print(
                "[typecheck-verification] new diagnostic: "
                f"{key}{_locations(current_diagnostics, key)}"
            )
        return _blocking(state)
    print(f"[typecheck-verification] PASS state={state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run a TypeScript compile and attribute its diagnostics to an isolated base.

The publication gate's TypeScript lanes are the last verification lanes that
compare a candidate tree against nothing at all.  This runner compiles the same
compiler configuration in the current tree and in an isolated base checkout and
routes the delta through the single shared comparator in
``backend/testing/fixgate_attribution.py`` -- no second attributor exists here.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
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
    FailureIdentity,
    JUnitReport,
    attribute_reports,
    extract_base,
)
from verification_receipt import record_attribution, record_lane, typecheck_lane  # noqa: E402


class ExecutionDisagreement(RuntimeError):
    """tsc's exit code and its parsed diagnostics contradict each other."""


@dataclass(frozen=True)
class Suite:
    """One publication typecheck lane."""

    directory: str
    install_directory: str
    # ``tsc`` argument vectors reproducing the lane's real compile.
    check_commands: tuple[tuple[str, ...], ...]
    # Leaf projects whose checked program defines this lane's coverage.
    source_programs: tuple[str, ...]
    extra_programs: tuple[str, ...] = ()
    # Solution config whose ``references`` must equal ``source_programs``.
    solution: str | None = None

    @property
    def programs(self) -> tuple[str, ...]:
        return (*self.source_programs, *self.extra_programs)


SUITES = {
    "agent": Suite(
        directory="agent",
        install_directory="agent",
        check_commands=(("-p", "tsconfig.json", "--noEmit"),),
        source_programs=("tsconfig.json",),
    ),
    "admin-ui": Suite(
        directory="admin-ui",
        install_directory=".",
        check_commands=(
            ("-b", "tsconfig.json", "--noEmit"),
            ("-p", "tsconfig.test.json", "--noEmit"),
        ),
        source_programs=("tsconfig.app.json", "tsconfig.node.json"),
        # Compile the test project against the base ref as part of this same
        # attributed lane, so inherited test-file errors cannot block a task.
        extra_programs=("tsconfig.test.json",),
        solution="tsconfig.json",
    ),
}

_GLOBAL_ROUTE = "<global>"
_BUILD_FLAGS = frozenset({"-b", "--build"})
# Global diagnostics that mean "the argv itself is wrong", not "this tree has a
# type error".  They reproduce identically in the base checkout, so attribution
# would score them as inherited and warn-pass a lane that compiled nothing.
_COMMAND_LINE_OPTION_CODES = frozenset(
    {
        "TS5023",  # Unknown compiler option '{0}'.
        "TS5024",  # Compiler option '{0}' requires a value of type {1}.
        "TS5025",  # Unknown compiler option '{0}'. Did you mean '{1}'?
        "TS5042",  # Option 'project' cannot be mixed with source files.
        "TS5093",  # Compiler option '--{0}' may only be used with '--build'.
        "TS5094",  # Compiler option '--{0}' may not be used with '--build'.
        "TS5108",  # Option '{0}={1}' has been removed.
        "TS6369",  # Option '--build' must be the first command line argument.
    }
)
_FILE_DIAGNOSTIC_RE = re.compile(
    r"^(?P<file>\S[^(]*)\((?P<line>\d+),(?P<column>\d+)\): "
    r"(?P<severity>error|warning) (?P<code>TS\d+): (?P<message>.*)$"
)
_GLOBAL_DIAGNOSTIC_RE = re.compile(
    r"^(?P<severity>error|warning) (?P<code>TS\d+): (?P<message>.*)$"
)
_SUMMARY_RE = re.compile(r"^Found \d+ errors?[ .]")
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


@dataclass
class CompileResult:
    """Structured diagnostics for one tree's compile of a whole suite."""

    report: JUnitReport = field(default_factory=JUnitReport)
    locations: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class DiagnosticIdentity:
    """One occurrence of a structured TypeScript diagnostic.

    ``FailureIdentity`` intentionally omits the source position so line motion
    does not turn inherited diagnostics into new failures.  A compiler can
    nevertheless emit the same structured diagnostic more than once, and each
    occurrence must remain independently attributable.  The ordinal is local
    to that position-free identity, so it preserves multiplicity without
    making line or column part of the comparison contract.
    """

    failure: FailureIdentity
    occurrence: int

    @property
    def key(self) -> str:
        # Keep every existing single-diagnostic receipt key stable.  Only a
        # repeated occurrence needs a discriminator for the shared attributor.
        if self.occurrence == 1:
            return self.failure.key
        return f"{self.failure.key}::occurrence={self.occurrence}"


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


def _changed_program_configs(repo: Path, base_ref: str, suite: Suite) -> list[str]:
    """Return changed tsconfig files that could narrow this suite's program."""

    prefix = f"{suite.directory}/"
    return [
        path
        for path in _changed_files(repo, base_ref)
        if path.replace("\\", "/").startswith(prefix)
        and Path(path).name.startswith("tsconfig")
        and path.endswith(".json")
    ]


def _strip_jsonc(text: str) -> str:
    """Drop tsconfig comments, preserving string literals."""

    without_strings: list[str] = []
    index = 0
    for match in re.finditer(r'"(?:\\.|[^"\\])*"', text):
        without_strings.append(
            _BLOCK_COMMENT_RE.sub(
                " ", _LINE_COMMENT_RE.sub("", text[index : match.start()])
            )
        )
        without_strings.append(match.group(0))
        index = match.end()
    without_strings.append(
        _BLOCK_COMMENT_RE.sub(" ", _LINE_COMMENT_RE.sub("", text[index:]))
    )
    return "".join(without_strings)


def _declared_references(root: Path, suite: Suite) -> list[str]:
    """Return the solution config's referenced project paths."""

    assert suite.solution is not None
    config = root / suite.directory / suite.solution
    try:
        parsed = json.loads(_strip_jsonc(config.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read {suite.solution}: {exc}") from exc
    references = parsed.get("references", [])
    malformed = not isinstance(references, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
        for entry in references
    )
    if malformed:
        raise RuntimeError(f"{suite.solution} has a malformed references list")
    return sorted(entry["path"].removeprefix("./") for entry in references)


def _verify_declared_programs(root: Path, suite: Suite) -> None:
    """Fail closed when the lane's declared coverage drifts from its config."""

    if suite.solution is None:
        return
    declared = sorted(suite.source_programs)
    referenced = _declared_references(root, suite)
    if declared != referenced:
        raise RuntimeError(
            f"{suite.solution} references {referenced}, but this lane declares {declared}"
        )


def _tail(output: str, *, lines: int = 80) -> str:
    return "\n".join(output.splitlines()[-lines:])


def _tsc_argv(args: tuple[str, ...]) -> list[str]:
    """Return the full ``npx tsc`` argv for one compile.

    ``--pretty false`` is injected here rather than spelled out per lane so no
    ``Suite`` can forget it -- the whole parser depends on unstyled output.
    But it must never land first: TypeScript's ``isBuildCommand`` inspects only
    ``commandLineArgs[0]``, so ``tsc --pretty false -b ...`` silently leaves
    build mode and reports ``-b`` as an unknown compiler option instead of
    compiling the solution.  Build invocations therefore keep ``-b`` first and
    take ``--pretty false`` as a trailing build option (``pretty`` is part of
    ``commonOptionsWithBuild``, so it is valid there).
    """

    if args and args[0] in _BUILD_FLAGS:
        return ["npx", "tsc", *args, "--pretty", "false"]
    return ["npx", "tsc", "--pretty", "false", *args]


def _run_tsc(
    root: Path, suite: Suite, args: tuple[str, ...]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _tsc_argv(args),
        cwd=root / suite.directory,
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


def _normalize_message(message: str) -> str:
    """Collapse whitespace so a re-wrapped diagnostic keeps one identity."""

    return " ".join(message.split())


def lane_name(suite: str) -> str:
    """Return this lane's stable identity in the cross-lane aggregate."""

    return typecheck_lane(suite)


def _record_suppressions(suite: str, outcome: dict[str, object]) -> None:
    """Record this lane's suppressions in the cross-lane aggregate (fail-open)."""

    record_classification(lane_name(suite), outcome)


def _relative_route(raw: str, project_root: Path, tree_root: Path) -> str:
    """Return a tree-relative, checkout-independent route for a diagnostic file."""

    path = Path(raw)
    resolved = path if path.is_absolute() else project_root / path
    try:
        return resolved.resolve().relative_to(tree_root.resolve()).as_posix()
    except (OSError, ValueError):
        return resolved.as_posix()


def _parse_diagnostics(
    output: str, *, project_root: Path, tree_root: Path, label: str
) -> CompileResult:
    """Convert ``tsc --pretty false`` output into route-keyed failure identities.

    The base identity is (repo-relative file, severity + TS code + message).
    Line and column are deliberately excluded so unrelated line motion cannot
    re-key an inherited diagnostic; they are kept only as reporting context.
    Repeated base identities receive an occurrence ordinal, preserving the
    diagnostic count for the shared report attributor.
    """

    result = CompileResult()
    occurrences: dict[str, int] = {}
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if line[0].isspace():
            # Indented related-information lines belong to the preceding
            # diagnostic and carry unstable positions; they are not identity.
            continue
        if _SUMMARY_RE.match(line):
            continue
        match = _FILE_DIAGNOSTIC_RE.match(line)
        if match is not None:
            route = _relative_route(match.group("file"), project_root, tree_root)
            location = f"{match.group('line')}:{match.group('column')}"
        else:
            match = _GLOBAL_DIAGNOSTIC_RE.match(line)
            if match is None:
                raise RuntimeError(f"{label} tsc output line is not parseable: {line!r}")
            if match.group("code") in _COMMAND_LINE_OPTION_CODES:
                raise ExecutionDisagreement(
                    f"{label} tsc rejected its own command line: {line}"
                )
            route = _GLOBAL_ROUTE
            location = ""
        message = _normalize_message(match.group("message"))
        testcase = f"{match.group('severity')} {match.group('code')}: {message}"
        identity = FailureIdentity(route=route, testcase=testcase, kind="error")
        occurrence = occurrences.get(identity.key, 0) + 1
        occurrences[identity.key] = occurrence
        diagnostic = DiagnosticIdentity(identity, occurrence)
        # ``test_routes`` is the route/testcase roster consumed by the shared
        # attributor's coverage check.  ``diagnostic.key`` also includes the
        # failure kind and (for repeats) an occurrence discriminator, which
        # belongs only to the failure map.
        result.report.test_routes.add(identity.test_key)
        result.report.failures[diagnostic.key] = f"{route}: {testcase}"
        if location:
            result.locations.setdefault(diagnostic.key, []).append(location)
    return result


def _valid_exit_diagnostic_pair(exit_code: int, diagnostic_count: int) -> bool:
    """Return whether tsc's process exit agrees with its parsed diagnostics."""

    return (exit_code == 0 and diagnostic_count == 0) or (
        exit_code != 0 and diagnostic_count > 0
    )


def _compile_suite(root: Path, suite: Suite, *, label: str) -> CompileResult:
    """Run every configured compile for the suite and merge its diagnostics."""

    merged = CompileResult()
    for args in suite.check_commands:
        completed = _run_tsc(root, suite, args)
        output = _tail(f"{completed.stdout}\n{completed.stderr}")
        if output.strip():
            print(f"[typecheck-verification] {label} {' '.join(args)} output:\n{output}")
        parsed = _parse_diagnostics(
            completed.stdout,
            project_root=root / suite.directory,
            tree_root=root,
            label=f"{label} {' '.join(args)}",
        )
        if not _valid_exit_diagnostic_pair(
            completed.returncode, len(parsed.report.failures)
        ):
            raise ExecutionDisagreement(
                f"{label} tsc {' '.join(args)} exited {completed.returncode} with "
                f"{len(parsed.report.failures)} parsed diagnostics"
            )
        merged.report.failures.update(parsed.report.failures)
        merged.report.test_routes.update(parsed.report.test_routes)
        for key, locations in parsed.locations.items():
            merged.locations.setdefault(key, []).extend(locations)
    return merged


def _program_files(root: Path, suite: Suite, *, label: str) -> set[str]:
    """Return the tree-relative source files this suite's compiles actually check."""

    project_root = root / suite.directory
    files: set[str] = set()
    for program in suite.programs:
        completed = _run_tsc(root, suite, ("-p", program, "--listFilesOnly"))
        if completed.returncode != 0:
            raise RuntimeError(
                f"{label} tsc -p {program} --listFilesOnly exited "
                f"{completed.returncode}: {_tail(completed.stderr or completed.stdout)}"
            )
        for raw_line in completed.stdout.splitlines():
            candidate = raw_line.strip()
            if not candidate:
                continue
            route = _relative_route(candidate, project_root, root)
            if "node_modules/" in route or not route.startswith(f"{suite.directory}/"):
                continue
            files.add(route)
    return files


def _blocking(
    suite_name: str,
    state: str,
    *,
    outcome: dict[str, object] | None = None,
    **extra: object,
) -> int:
    recorded: dict[str, object] = {
        "state": state,
        "blocking": True,
        "current_exit_code": 1,
        "new_failures": {},
        "suppressed_failures": {},
        "disappeared_failures": {},
        "skipped_failures": {},
        **(outcome or {}),
        **extra,
    }
    recorded["state"] = state
    recorded["blocking"] = True
    record_classification(lane_name(suite_name), recorded)
    record_lane(
        typecheck_lane(suite_name),
        state=state,
        blocking=True,
        new_failures=recorded.get("new_failures"),
        suppressed_failures=recorded.get("suppressed_failures"),
        disappeared_failures=recorded.get("disappeared_failures"),
        skipped_failures=recorded.get("skipped_failures"),
        extra={
            key: recorded[key]
            for key in ("current_exit_code", "error")
            if key in recorded
        },
    )
    detail_items = [
        (key, value) for key, value in extra.items() if key != "current_exit_code"
    ]
    detail_items.append(("current_exit_code", recorded["current_exit_code"]))
    detail = " ".join(f"{key}={value}" for key, value in detail_items)
    print(
        f"[typecheck-verification] FAILED state={state}{(' ' + detail) if detail else ''}"
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=sorted(SUITES), required=True)
    parser.add_argument("--base", default="automain")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    repo = args.repo_root.resolve()
    suite = SUITES[args.suite]

    try:
        _verify_declared_programs(repo, suite)
    except RuntimeError as exc:
        return _blocking(args.suite, "project_configuration_mismatch", error=str(exc))

    try:
        current = _compile_suite(repo, suite, label="current")
    except ExecutionDisagreement as exc:
        return _blocking(args.suite, "execution_failure", error=str(exc))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return _blocking(args.suite, "attribution_unavailable", error=str(exc))

    try:
        base_ref = _base_ref(repo, args.base)
        changed_configs = _changed_program_configs(repo, base_ref, suite)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return _blocking(
            args.suite,
            "baseline_unavailable",
            current_exit_code=1 if current.report.failures else 0,
            error=str(exc),
        )

    # A clean current compile can only be hiding an inherited diagnostic if the
    # checked program itself narrowed, and the program can only narrow through
    # one of this suite's tsconfig files. Keep that cheap path, but only after
    # proving the suite's tsconfig diff is empty.
    if not current.report.failures and not changed_configs:
        # Advisory: neither emitter raises, so the lane's verdict below is
        # independent of whether either receipt could be written.
        record_lane(typecheck_lane(args.suite), state="passed")
        _record_suppressions(args.suite, {"state": "passed"})
        print("[typecheck-verification] PASS state=passed")
        return 0

    with tempfile.TemporaryDirectory(prefix="blueprint-typecheck-verification-") as temp_dir:
        base_root = Path(temp_dir) / "base"
        try:
            base_root.mkdir(parents=True)
            extract_base(repo, base_ref, base_root)
            install = _install_base(base_root, suite)
            if install.returncode != 0:
                return _blocking(
                    args.suite,
                    "baseline_unavailable",
                    current_exit_code=1 if current.report.failures else 0,
                    base_install_exit_code=install.returncode,
                )
            baseline = _compile_suite(base_root, suite, label="baseline")
            base_programs = _program_files(base_root, suite, label="baseline")
            current_programs = _program_files(repo, suite, label="current")
        except ExecutionDisagreement as exc:
            return _blocking(
                args.suite,
                "baseline_unavailable",
                current_exit_code=1 if current.report.failures else 0,
                error=str(exc),
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return _blocking(
                args.suite,
                "baseline_unavailable",
                current_exit_code=1 if current.report.failures else 0,
                error=str(exc),
            )

    # ``classify_verification``'s disappeared/skipped states model a test
    # roster: a baseline failure whose route is absent from the current report
    # is treated as evidence the run lost coverage. A compiler emits its total
    # diagnostic set instead, so an inherited error that this diff FIXED would
    # be scored as disappeared and would block. Attribute directly and cover
    # lost coverage with the explicit program-shrink check below.
    attributed = attribute_reports(current.report, baseline.report)
    for key in sorted(attributed.suppressed_failures):
        locations = ", ".join(sorted(set(current.locations.get(key, [])))) or "n/a"
        print(
            f"[typecheck-verification] WARN suppressed baseline diagnostic: "
            f"{key} (current locations: {locations})"
        )

    shrunk = sorted(
        route
        for route in base_programs - current_programs
        if (repo / route).exists()
    )

    def record(state: str, *, blocking: bool) -> None:
        """Persist this lane's attribution; never consulted for the verdict.

        Two sidecars, both fail-open and both additive: the multi-lane
        publication receipt this gate projects into the durable PR receipt, and
        the cross-lane guard-suppression aggregate every ``./check`` lane feeds
        (``scripts/guard_suppression_receipt.py``).
        """

        record_attribution(
            typecheck_lane(args.suite),
            attributed,
            state=state,
            blocking=blocking,
            extra={"current_exit_code": 1 if current.report.failures else 0},
        )
        _record_suppressions(
            args.suite,
            {
                "state": state,
                "suppressed_failures": attributed.suppressed_failures,
                "disappeared_failures": attributed.disappeared_failures,
                "skipped_failures": attributed.skipped_failures,
            },
        )

    if shrunk:
        record("coverage_shrink", blocking=True)
        return _blocking(
            args.suite,
            "coverage_shrink",
            outcome={
                "new_failures": attributed.new_failures,
                "suppressed_failures": attributed.suppressed_failures,
                "disappeared_failures": attributed.disappeared_failures,
                "skipped_failures": attributed.skipped_failures,
                "current_exit_code": 1 if current.report.failures else 0,
            },
            files=",".join(shrunk),
        )
    if attributed.new_failures:
        record("type_failure", blocking=True)
        for key in sorted(attributed.new_failures):
            print(f"[typecheck-verification] new diagnostic: {key}")
        return _blocking(
            args.suite,
            "type_failure",
            outcome={
                "new_failures": attributed.new_failures,
                "suppressed_failures": attributed.suppressed_failures,
                "disappeared_failures": attributed.disappeared_failures,
                "skipped_failures": attributed.skipped_failures,
                "current_exit_code": 1,
            },
            new_diagnostic_count=len(attributed.new_failures),
        )

    state = "preexisting_type_failure" if current.report.failures else "passed"
    record(state, blocking=False)
    print(f"[typecheck-verification] PASS state={state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Focused contract tests for the shared Vitest baseline-attribution runner."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import vitest_verification_guard as guard  # noqa: E402
from testing.fixgate_attribution import JUnitReport, parse_junit_report  # noqa: E402

_NAMED_FAILURE = "src/example.test.ts::fails::failure"
_INVALID_EXIT_REPORT_PAIRS = [
    (0, True),
    (1, False),
    (2, False),
    (2, True),
    (3, False),
    (3, True),
    (4, False),
    (4, True),
    (5, False),
    (5, True),
    (137, False),
    (137, True),
]


def _named_failure_report(detail: str = "failure") -> JUnitReport:
    return JUnitReport(
        failures={_NAMED_FAILURE: detail},
        test_routes={"src/example.test.ts::fails"},
    )


def _clean_execution_report() -> JUnitReport:
    return JUnitReport(test_routes={"blueprint.vitest.execution::runner"})


def test_developer_gate_uses_the_shared_lane_only_in_test_mode() -> None:
    """The tracked fallback keeps unit tests opt-in while sharing attribution."""

    script = (REPO_ROOT / "scripts" / "check-default.sh").read_text(encoding="utf-8")
    for suite in ("admin-ui", "agent"):
        command = (
            f'run "{suite} vitest (attributed)" '
            f"python3 scripts/vitest_verification_guard.py --suite {suite} "
            '--base "$BASE" --repo-root "$WORKSPACE"'
        )
        assert command in script
        assert re.search(
            rf'if \[ "\$WITH_TESTS" = "1" \]; then\n\s*{re.escape(command)}',
            script,
        )


@pytest.mark.parametrize("suite", ["agent", "admin-ui"])
def test_guard_warn_passes_only_named_identical_baseline_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], suite: str
) -> None:
    """Both stacks run current + base and expose the suppressed identity."""

    runs: list[tuple[Path, str]] = []

    def fake_run(
        root: Path, selected: guard.Suite, report: Path, execution_report: Path
    ) -> subprocess.CompletedProcess[str]:
        runs.append((root, selected.directory))
        return subprocess.CompletedProcess([], 1, stdout="vitest output", stderr="")

    def fake_report(path: Path, **_: object) -> JUnitReport:
        if path.name.endswith("execution.xml"):
            return JUnitReport(test_routes={"blueprint.vitest.execution::runner"})
        return JUnitReport(
            failures={"src/example.test.ts::keeps failing::failure": "expected inherited failure"},
            test_routes={"src/example.test.ts::keeps failing"},
        )

    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_install_base", lambda *_: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "extract_base", lambda _, __, destination: (destination / "agent").mkdir())
    monkeypatch.setattr(guard, "parse_junit_report", fake_report)

    assert guard.main(["--suite", suite, "--base", "automain", "--repo-root", str(tmp_path)]) == 0
    assert [directory for _, directory in runs] == [guard.SUITES[suite].directory] * 2
    assert runs[0][0] == tmp_path
    assert runs[1][0] != tmp_path
    assert "WARN suppressed baseline failure: src/example.test.ts::keeps failing::failure" in capsys.readouterr().out


def test_guard_blocks_execution_only_current_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A runner error without a JUnit failed node cannot inherit a base pass."""

    def fake_run(_: Path, __: guard.Suite, ___: Path, ____: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, stdout="worker crashed", stderr="")

    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_install_base", lambda *_: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "extract_base", lambda _, __, destination: (destination / "agent").mkdir())
    monkeypatch.setattr(
        guard,
        "parse_junit_report",
        lambda path, **_kwargs: JUnitReport(test_routes={"blueprint.vitest.execution::runner"})
        if path.name.endswith("execution.xml")
        else JUnitReport(),
    )

    assert guard.main(["--suite", "agent", "--repo-root", str(tmp_path)]) == 1
    assert "FAILED state=execution_failure" in capsys.readouterr().out


@pytest.mark.parametrize("suite", ["agent", "admin-ui"])
def test_guard_clean_current_skips_every_baseline_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    suite: str,
) -> None:
    """A clean run with no changed suite test file keeps the cheap skip."""

    runs: list[Path] = []

    def fake_run(
        root: Path, _: guard.Suite, __: Path, ___: Path
    ) -> subprocess.CompletedProcess[str]:
        runs.append(root)
        assert root == tmp_path
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def unexpected_baseline(*_: object, **__: object) -> None:
        pytest.fail("clean current run must not prepare or execute a baseline")

    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "_changed_test_files", lambda *_: [])
    monkeypatch.setattr(guard, "extract_base", unexpected_baseline)
    monkeypatch.setattr(guard, "_install_base", unexpected_baseline)
    monkeypatch.setattr(
        guard,
        "parse_junit_report",
        lambda path, **_kwargs: JUnitReport(
            test_routes={"blueprint.vitest.execution::runner"}
        )
        if path.name.endswith("execution.xml")
        else JUnitReport(test_routes={"src/example.test.ts::passes"}),
    )

    assert guard.main(["--suite", suite, "--repo-root", str(tmp_path)]) == 0
    assert runs == [tmp_path]
    assert "PASS state=passed" in capsys.readouterr().out


def _set_up_clean_changed_test_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    suite_name: str,
    baseline_report: JUnitReport,
    *,
    current_report: JUnitReport | None = None,
) -> list[tuple[Path, list[str] | None]]:
    """Stub the two Vitest runs while preserving selector and attribution logic."""

    suite = guard.SUITES[suite_name]
    calls: list[tuple[Path, list[str] | None]] = []
    current_report = current_report or JUnitReport(
        test_routes={"src/example.test.ts::passes"}
    )

    def fake_run(
        root: Path,
        _: guard.Suite,
        report: Path,
        __: Path,
        selectors: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((root, selectors))
        exit_code = 0 if report.name == "current.xml" else 1
        return subprocess.CompletedProcess([], exit_code, stdout="", stderr="")

    def fake_extract(_: Path, __: str, destination: Path) -> None:
        test_file = destination / suite.directory / "src" / "example.test.ts"
        test_file.parent.mkdir(parents=True)
        test_file.touch()

    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(
        guard,
        "_changed_test_files",
        lambda *_: [f"{suite.directory}/src/example.test.ts"],
    )
    monkeypatch.setattr(guard, "extract_base", fake_extract)
    monkeypatch.setattr(guard, "_install_base", lambda *_: subprocess.CompletedProcess([], 0))

    def fake_report(path: Path, **_: object) -> JUnitReport:
        if path.name.endswith("execution.xml"):
            return _clean_execution_report()
        return current_report if path.name == "current.xml" else baseline_report

    monkeypatch.setattr(guard, "parse_junit_report", fake_report)
    return calls


@pytest.mark.parametrize("suite", ["agent", "admin-ui"])
def test_guard_blocks_a_vanished_baseline_failure_when_a_suite_test_file_changed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    suite: str,
) -> None:
    """A clean current run cannot erase a failing changed base test."""

    baseline = _named_failure_report("inherited failure")
    calls = _set_up_clean_changed_test_run(monkeypatch, tmp_path, suite, baseline)

    assert guard.main(["--suite", suite, "--repo-root", str(tmp_path)]) == 1
    assert calls[0] == (tmp_path, None)
    assert calls[1][1] == ["src/example.test.ts"]
    assert "FAILED state=baseline_failure_disappeared" in capsys.readouterr().out


@pytest.mark.parametrize("suite", ["agent", "admin-ui"])
def test_guard_blocks_a_skipped_baseline_failure_when_a_suite_test_file_changed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    suite: str,
) -> None:
    """A clean current run cannot skip a failing changed base test."""

    route = "src/example.test.ts::fails"
    baseline = _named_failure_report("inherited failure")
    current = JUnitReport(test_routes={route}, skipped_routes={route})
    calls = _set_up_clean_changed_test_run(
        monkeypatch, tmp_path, suite, baseline, current_report=current
    )

    assert guard.main(["--suite", suite, "--repo-root", str(tmp_path)]) == 1
    assert calls[1][1] == ["src/example.test.ts"]
    assert "FAILED state=baseline_failure_skipped" in capsys.readouterr().out


@pytest.mark.parametrize("suite", ["agent", "admin-ui"])
def test_guard_clean_current_with_only_non_test_changes_skips_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    suite: str,
) -> None:
    """A source-only suite diff cannot make a baseline test disappear."""

    runs: list[Path] = []

    def fake_run(
        root: Path, _: guard.Suite, __: Path, ___: Path
    ) -> subprocess.CompletedProcess[str]:
        runs.append(root)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(
        guard,
        "_changed_files",
        lambda *_: [f"{suite}/src/example.ts", f"{suite}/README.md"],
    )
    monkeypatch.setattr(
        guard,
        "parse_junit_report",
        lambda path, **_: _clean_execution_report()
        if path.name.endswith("execution.xml")
        else JUnitReport(test_routes={"src/example.test.ts::passes"}),
    )

    assert guard.main(["--suite", suite, "--repo-root", str(tmp_path)]) == 0
    assert runs == [tmp_path]
    assert "PASS state=passed" in capsys.readouterr().out


def test_guard_clean_new_suite_test_without_base_counterpart_uses_empty_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A newly added test has no base selector and needs no base install."""

    runs: list[Path] = []

    def fake_run(
        root: Path, _: guard.Suite, __: Path, ___: Path
    ) -> subprocess.CompletedProcess[str]:
        runs.append(root)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def unexpected_install(*_: object, **__: object) -> None:
        pytest.fail("a new-only test file must not install base dependencies")

    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(
        guard,
        "_changed_test_files",
        lambda *_: ["agent/src/new.test.ts"],
    )
    monkeypatch.setattr(guard, "extract_base", lambda *_: None)
    monkeypatch.setattr(guard, "_install_base", unexpected_install)
    monkeypatch.setattr(
        guard,
        "parse_junit_report",
        lambda path, **_: _clean_execution_report()
        if path.name.endswith("execution.xml")
        else JUnitReport(test_routes={"src/new.test.ts::passes"}),
    )

    assert guard.main(["--suite", "agent", "--repo-root", str(tmp_path)]) == 0
    assert runs == [tmp_path]
    assert "PASS state=passed" in capsys.readouterr().out


def test_changed_suite_test_files_union_all_diff_sources_and_filter_rosters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Committed, working-tree, and untracked paths all feed suite filtering."""

    def fake_git(_: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("diff", "--name-only") and args[2].endswith("...HEAD"):
            output = "agent/src/committed.test.ts\nadmin-ui/src/admin.test.tsx\n"
        elif args[:2] == ("diff", "--name-only"):
            output = "agent/src/working.test.ts\nagent/src/working.ts\n"
        else:
            output = "agent/src/untracked.test.ts\nagent/src/ignored.test.js\n"
        return subprocess.CompletedProcess([], 0, stdout=output, stderr="")

    monkeypatch.setattr(guard, "_git", fake_git)

    assert guard._changed_test_files(
        tmp_path, "origin/automain", guard.SUITES["agent"]
    ) == [
        "agent/src/committed.test.ts",
        "agent/src/untracked.test.ts",
        "agent/src/working.test.ts",
    ]
    assert guard._changed_test_files(
        tmp_path, "origin/automain", guard.SUITES["admin-ui"]
    ) == ["admin-ui/src/admin.test.tsx"]


def test_run_vitest_passes_base_file_selectors_to_vitest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The base arm scopes Vitest by file without changing its reporters."""

    captured: dict[str, object] = {}

    def fake_subprocess_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_subprocess_run)
    report = tmp_path / "baseline.xml"
    execution_report = tmp_path / "baseline-execution.xml"

    guard._run_vitest(
        tmp_path,
        guard.SUITES["agent"],
        report,
        execution_report,
        selectors=["src/example.test.ts"],
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert command[:4] == ["npx", "vitest", "run", "src/example.test.ts"]
    assert f"--outputFile.junit={report}" in command
    assert captured["cwd"] == tmp_path / "agent"


@pytest.mark.parametrize(
    ("exit_code", "has_failures"),
    _INVALID_EXIT_REPORT_PAIRS,
    ids=[
        f"exit-{exit_code}-{'failures' if has_failures else 'clean'}"
        for exit_code, has_failures in _INVALID_EXIT_REPORT_PAIRS
    ],
)
def test_guard_blocks_every_invalid_current_exit_report_pair_before_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    exit_code: int,
    has_failures: bool,
) -> None:
    """Invalid current process/JUnit pairs are execution failures, never attributable."""

    calls = 0

    def fake_run(
        _: Path, __: guard.Suite, ___: Path, ____: Path
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls != 1:
            pytest.fail("invalid current result must not execute a baseline")
        return subprocess.CompletedProcess([], exit_code, stdout="", stderr="")

    def unexpected_baseline(*_: object, **__: object) -> None:
        pytest.fail("invalid current result must not prepare a baseline")

    current_report = _named_failure_report() if has_failures else JUnitReport()
    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_base_ref", unexpected_baseline)
    monkeypatch.setattr(guard, "extract_base", unexpected_baseline)
    monkeypatch.setattr(guard, "_install_base", unexpected_baseline)
    monkeypatch.setattr(
        guard,
        "parse_junit_report",
        lambda path, **_: _clean_execution_report()
        if path.name.endswith("execution.xml")
        else current_report,
    )

    assert guard.main(["--suite", "agent", "--repo-root", str(tmp_path)]) == 1
    assert calls == 1
    output = capsys.readouterr().out
    assert "FAILED state=execution_failure" in output
    assert f"current_exit_code={exit_code}" in output


@pytest.mark.parametrize(
    ("exit_code", "has_failures"),
    _INVALID_EXIT_REPORT_PAIRS,
    ids=[
        f"exit-{exit_code}-{'failures' if has_failures else 'clean'}"
        for exit_code, has_failures in _INVALID_EXIT_REPORT_PAIRS
    ],
)
def test_guard_blocks_every_invalid_baseline_exit_report_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    exit_code: int,
    has_failures: bool,
) -> None:
    """Invalid base process/JUnit pairs make differential attribution unavailable."""

    calls = 0

    def fake_run(
        _: Path, __: guard.Suite, ___: Path, ____: Path
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            [], 1 if calls == 1 else exit_code, stdout="", stderr=""
        )

    baseline_report = _named_failure_report("baseline") if has_failures else JUnitReport()
    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_install_base", lambda *_: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "extract_base", lambda *_: None)
    monkeypatch.setattr(
        guard,
        "parse_junit_report",
        lambda path, **_: _clean_execution_report()
        if path.name.endswith("execution.xml")
        else (
            _named_failure_report("current")
            if path.name == "current.xml"
            else baseline_report
        ),
    )

    assert guard.main(["--suite", "agent", "--repo-root", str(tmp_path)]) == 1
    assert calls == 2
    output = capsys.readouterr().out
    assert "FAILED state=baseline_unavailable" in output
    assert f"base_exit_code={exit_code}" in output


def test_guard_accepts_valid_failing_current_and_clean_baseline_pairs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The strict contract still attributes a valid named failure against a clean base."""

    calls = 0

    def fake_run(
        _: Path, __: guard.Suite, ___: Path, ____: Path
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            [], 1 if calls == 1 else 0, stdout="", stderr=""
        )

    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_install_base", lambda *_: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "extract_base", lambda *_: None)
    monkeypatch.setattr(
        guard,
        "parse_junit_report",
        lambda path, **_: _clean_execution_report()
        if path.name.endswith("execution.xml")
        else (
            _named_failure_report("current")
            if path.name == "current.xml"
            else JUnitReport()
        ),
    )

    assert guard.main(["--suite", "agent", "--repo-root", str(tmp_path)]) == 1
    assert calls == 2
    output = capsys.readouterr().out
    assert "FAILED state=test_failure" in output
    assert "baseline_unavailable" not in output


def test_guard_blocks_mixed_inherited_failure_and_current_execution_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A v2 runner error cannot inherit an otherwise identical JUnit failure."""

    def fake_run(_: Path, __: guard.Suite, ___: Path, ____: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, stdout="", stderr="")

    inherited = JUnitReport(
        failures={"src/example.test.ts::keeps failing::failure": "inherited"},
        test_routes={"src/example.test.ts::keeps failing"},
    )
    execution_error = JUnitReport(
        failures={
            "blueprint.vitest.execution::unhandled_error::error": "unhandled"
        },
        test_routes={
            "blueprint.vitest.execution::runner",
            "blueprint.vitest.execution::unhandled_error",
        },
    )

    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_install_base", lambda *_: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "extract_base", lambda _, __, destination: (destination / "agent").mkdir())
    monkeypatch.setattr(
        guard,
        "parse_junit_report",
        lambda path, **_: execution_error if path.name == "current-execution.xml" else (
            JUnitReport(test_routes={"blueprint.vitest.execution::runner"})
            if path.name.endswith("execution.xml")
            else inherited
        ),
    )

    assert guard.main(["--suite", "agent", "--repo-root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "WARN suppressed baseline failure: src/example.test.ts::keeps failing::failure" in output
    assert "FAILED state=execution_failure" in output


def test_guard_blocks_baseline_execution_report_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A valid base process/JUnit pair cannot hide its structured runner error."""

    calls = 0

    def fake_run(_: Path, __: guard.Suite, ___: Path, ____: Path) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            [], 1 if calls == 1 else 0, stdout="", stderr=""
        )

    baseline_execution_error = JUnitReport(
        failures={
            "blueprint.vitest.execution::unhandled_error::error": "unhandled"
        },
        test_routes={
            "blueprint.vitest.execution::runner",
            "blueprint.vitest.execution::unhandled_error",
        },
    )

    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_install_base", lambda *_: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "extract_base", lambda _, __, destination: (destination / "agent").mkdir())
    monkeypatch.setattr(
        guard,
        "parse_junit_report",
        lambda path, **_kwargs: baseline_execution_error
        if path.name == "baseline-execution.xml"
        else (
            _clean_execution_report()
            if path.name.endswith("execution.xml")
            else (
                _named_failure_report("current")
                if path.name == "current.xml"
                else JUnitReport()
            )
        ),
    )

    assert guard.main(["--suite", "agent", "--repo-root", str(tmp_path)]) == 1
    assert calls == 2
    assert "FAILED state=baseline_execution_failure" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Coverage for the Node artifact producer the fail-closed contract depends on.
# The cases above deliberately stub `parse_junit_report`, so without these the
# reporter itself is never loaded and a Vitest hook rename is invisible.
# --------------------------------------------------------------------------

_REPORTER = REPO_ROOT / "scripts" / "vitest_execution_reporter.mjs"

# Reporter hooks, newest Vitest interface first.  Vitest 2 dispatches
# `onFinished`; Vitest 3+ removed it from the reporter interface and dispatches
# `onTestRunEnd`.  Every installed Vitest major must dispatch at least one.
_REPORTER_HOOKS = ("onTestRunEnd", "onFinished")

_VITEST_INSTALLS = {
    "root (admin-ui)": REPO_ROOT / "node_modules" / "vitest",
    "agent": REPO_ROOT / "agent" / "node_modules" / "vitest",
}


def _reporter_source() -> str:
    return _REPORTER.read_text(encoding="utf-8")


def _dispatched_reporter_hooks(install: Path) -> set[str]:
    """Return the reporter hook names a Vitest install actually dispatches."""

    hooks: set[str] = set()
    for bundle in (install / "dist").rglob("*.js"):
        hooks.update(re.findall(r'report\(\s*"(on[A-Za-z]+)"', bundle.read_text(encoding="utf-8", errors="ignore")))
    return hooks


def test_execution_reporter_declares_every_supported_vitest_hook() -> None:
    """The reporter must implement both majors' run-completion hooks."""

    source = _reporter_source()
    for hook in _REPORTER_HOOKS:
        assert re.search(rf"^\s*async {hook}\(", source, re.MULTILINE), (
            f"scripts/vitest_execution_reporter.mjs must implement {hook}(); "
            "Vitest 2 dispatches onFinished and Vitest 3+ dispatches onTestRunEnd."
        )


@pytest.mark.parametrize("label", sorted(_VITEST_INSTALLS))
def test_execution_reporter_hooks_cover_each_installed_vitest_major(label: str) -> None:
    """Pin the reporter against what each installed Vitest really dispatches.

    This is the regression that a hook rename must trip: if a Vitest bump stops
    dispatching every hook the reporter implements, that suite would silently
    stop writing the execution artifact and the guard would block forever.
    """

    install = _VITEST_INSTALLS[label]
    if not (install / "dist").is_dir():
        pytest.skip(f"{label} vitest is not installed in this environment")
    dispatched = _dispatched_reporter_hooks(install)
    assert dispatched, f"could not read {label} vitest dispatch table"
    assert dispatched.intersection(_REPORTER_HOOKS), (
        f"{label} vitest dispatches {sorted(dispatched)}, none of which the "
        f"execution reporter implements ({list(_REPORTER_HOOKS)}); the suite "
        "would never write BLUEPRINT_VITEST_EXECUTION_JUNIT."
    )


def _drive_reporter(tmp_path: Path, hook: str, *, errors: str, reason: str = "passed") -> Path:
    """Execute a reporter hook in real Node and return the written artifact."""

    if shutil.which("node") is None:
        pytest.skip("node is unavailable in this environment")
    report = tmp_path / "nested" / "execution.xml"
    call = (
        "await reporter.onFinished([], errors);"
        if hook == "onFinished"
        else f"await reporter.onTestRunEnd([], errors, {json.dumps(reason)});"
    )
    driver = tmp_path / "driver.mjs"
    driver.write_text(
        f"import Reporter from {json.dumps(_REPORTER.as_uri())};\n"
        "const reporter = new Reporter();\n"
        f"const errors = {errors};\n"
        f"{call}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(driver)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "BLUEPRINT_VITEST_EXECUTION_JUNIT": str(report)},
    )
    assert result.returncode == 0, result.stderr
    return report


@pytest.mark.parametrize("hook", _REPORTER_HOOKS)
def test_execution_reporter_emits_the_reserved_clean_identity_from_each_hook(
    tmp_path: Path, hook: str
) -> None:
    """Either hook alone must satisfy the guard's clean-run contract."""

    report = _drive_reporter(tmp_path, hook, errors="[]")
    parsed = parse_junit_report(report, tree_root=tmp_path, label="current execution")
    assert parsed.test_routes == {guard._EXECUTION_REPORT_RUNNER}
    assert not parsed.failures
    assert guard._execution_errors(parsed) is False


@pytest.mark.parametrize("hook", _REPORTER_HOOKS)
def test_execution_reporter_reports_runner_errors_from_each_hook(
    tmp_path: Path, hook: str
) -> None:
    """Either hook alone must surface a runner-level error to the guard."""

    report = _drive_reporter(tmp_path, hook, errors='[{ type: "boom & <bad>" }]')
    parsed = parse_junit_report(report, tree_root=tmp_path, label="current execution")
    assert guard._EXECUTION_REPORT_FAILURE in parsed.failures
    assert guard._execution_errors(parsed) is True


def test_execution_reporter_fails_closed_on_an_interrupted_run(tmp_path: Path) -> None:
    """An interrupted Vitest 3+ run has no trustworthy verdicts."""

    report = _drive_reporter(tmp_path, "onTestRunEnd", errors="[]", reason="interrupted")
    parsed = parse_junit_report(report, tree_root=tmp_path, label="current execution")
    assert guard._execution_errors(parsed) is True


def test_execution_reporter_requires_its_output_path(tmp_path: Path) -> None:
    """A missing artifact path must raise rather than silently skip the write."""

    if shutil.which("node") is None:
        pytest.skip("node is unavailable in this environment")
    driver = tmp_path / "driver.mjs"
    driver.write_text(
        f"import Reporter from {json.dumps(_REPORTER.as_uri())};\n"
        "await new Reporter().onTestRunEnd([], [], 'passed');\n",
        encoding="utf-8",
    )
    env = {key: value for key, value in os.environ.items() if key != "BLUEPRINT_VITEST_EXECUTION_JUNIT"}
    result = subprocess.run(["node", str(driver)], capture_output=True, text=True, check=False, env=env)
    assert result.returncode != 0
    assert "BLUEPRINT_VITEST_EXECUTION_JUNIT is required" in result.stderr

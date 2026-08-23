"""Focused contract tests for the shared Vitest baseline-attribution runner."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

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


@pytest.fixture(autouse=True)
def _isolate_verification_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """Keep every in-process guard run out of the real durable sidecar.

    These tests drive ``guard.main()`` in-process, and the guard records its
    lane into the receipt resolved from ``BLUEPRINT_VERIFICATION_RECEIPT`` (or
    the ``TASK_ID`` fallback).  Without this, a corpus run of these tests --
    the static-corpus lane runs the whole ``scripts/test_*.py`` roster inside
    ./check, before the real Vitest lanes -- would write fixture lane blocks,
    with fabricated suppressed baseline failures, into the very receipt this
    gate treats as the single durable record.
    """

    isolated = tmp_path_factory.mktemp("verification-receipt") / "receipt.json"
    monkeypatch.setenv("BLUEPRINT_VERIFICATION_RECEIPT", str(isolated))
    monkeypatch.delenv("TASK_ID", raising=False)
    return isolated


def _named_failure_report(detail: str = "failure") -> JUnitReport:
    return JUnitReport(
        failures={_NAMED_FAILURE: detail},
        test_routes={"src/example.test.ts::fails"},
    )


def _clean_execution_report() -> JUnitReport:
    return JUnitReport(test_routes={"blueprint.vitest.execution::runner"})


def test_blocking_helper_records_the_printed_verdict_and_all_buckets(
    monkeypatch: pytest.MonkeyPatch,
    _isolate_verification_receipt: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A blocking early-return helper leaves both durable sidecars populated."""

    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(
        guard,
        "record_classification",
        lambda _lane, outcome: recorded.append(dict(outcome)),
    )

    assert (
        guard._blocking(
            "agent",
            "attribution_unavailable",
            current_exit_code=1,
            error="current JUnit missing",
        )
        == 1
    )

    assert recorded[0]["state"] == "attribution_unavailable"
    assert recorded[0]["blocking"] is True
    assert recorded[0]["error"] == "current JUnit missing"
    assert set(recorded[0]) >= {
        "new_failures",
        "suppressed_failures",
        "disappeared_failures",
        "skipped_failures",
    }
    lane = json.loads(_isolate_verification_receipt.read_text(encoding="utf-8"))["lanes"]["vitest:agent"]
    assert lane["state"] == "attribution_unavailable"
    assert lane["blocking"] is True
    assert lane["current_exit_code"] == 1
    assert "FAILED state=attribution_unavailable" in capsys.readouterr().out


def test_blocking_exit_preserves_recorded_coverage_identities(
    _isolate_verification_receipt: Path,
) -> None:
    outcome = {
        "state": "coverage_regression",
        "blocking": True,
        "current_exit_code": 1,
        "new_failures": {},
        "suppressed_failures": {},
        "disappeared_failures": {},
        "skipped_failures": {},
    }
    guard._record("admin-ui", outcome, coverage_failures={"src/foo.test.ts": "missing"})

    assert guard._blocking("admin-ui", "coverage_regression", outcome=outcome) == 1

    lane = json.loads(_isolate_verification_receipt.read_text(encoding="utf-8"))["lanes"]["vitest:admin-ui"]
    assert lane["new_failures"] == {"src/foo.test.ts::coverage": {"detail": "missing"}}


@pytest.fixture(autouse=True)
def _seed_suite_configs(tmp_path: Path) -> None:
    """Give every synthetic repo the REAL Vitest config of each suite.

    The guard resolves a suite's roster from its own config and fails closed
    (`baseline_unavailable`) when it is missing — correctly, because a tree
    whose roster cannot be read is a tree whose coverage cannot be established.
    Seeding the shipped configs keeps these fixtures exercising the real
    include/exclude globs instead of a hand-kept copy, and keeps the
    fail-closed branch itself testable by deleting the file in the one test
    that wants it.
    """

    for name, suite in guard.SUITES.items():
        destination = tmp_path / suite.config_path
        source = REPO_ROOT / suite.config_path
        if destination.exists() or not source.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _permissive_roster() -> guard._Roster:
    """A roster that matches any `*.test.{ts,tsx,js}` file, for tests that
    don't care about a suite's real include/exclude globs."""

    return guard._Roster(
        include=guard._compile_globs(["**/*.test.ts", "**/*.test.tsx", "**/*.test.js"]),
        exclude=(),
    )


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
    monkeypatch.setattr(guard, "_load_roster", lambda *_: _permissive_roster())
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
    monkeypatch.setattr(guard, "_load_roster", lambda *_: _permissive_roster())
    monkeypatch.setattr(guard, "_changed_test_files", lambda *_: [])
    # The roster-config-change probe reads the raw diff too; stub it beside
    # the roster-filtered list so a scoped fixture never shells out to git.
    monkeypatch.setattr(guard, "_changed_files", lambda *_: [])
    monkeypatch.setattr(guard, "_changed_files", lambda *_: [])
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
        test_routes={"src/example.test.ts::fails"}
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
    monkeypatch.setattr(guard, "_load_roster", lambda *_: _permissive_roster())
    monkeypatch.setattr(
        guard,
        "_changed_test_files",
        lambda *_: [f"{suite.directory}/src/example.test.ts"],
    )
    monkeypatch.setattr(
        guard,
        "_changed_files",
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
def test_guard_allows_a_vanished_baseline_failure_when_the_test_route_still_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    suite: str,
) -> None:
    """A repaired test route keeps coverage without charging inherited debt."""

    baseline = _named_failure_report("inherited failure")
    calls = _set_up_clean_changed_test_run(monkeypatch, tmp_path, suite, baseline)

    assert guard.main(["--suite", suite, "--repo-root", str(tmp_path)]) == 0
    assert calls[0] == (tmp_path, None)
    assert calls[1][1] == ["src/example.test.ts"]
    assert "PASS state=baseline_failure_disappeared" in capsys.readouterr().out


def test_guard_blocks_a_deleted_baseline_route_during_a_non_clean_current_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A repaired route may disappear from failures, but a deleted route may not.

    The current run also retains an inherited failure, so this specifically
    covers the non-clean path where the old guard skipped route coverage.
    """

    repaired = "src/repaired.test.ts::now passes::failure"
    inherited = "src/inherited.test.ts::still fails::failure"
    deleted = "src/deleted.test.ts::gone::failure"
    current = JUnitReport(
        failures={inherited: "inherited"},
        test_routes={
            "src/repaired.test.ts::now passes",
            "src/inherited.test.ts::still fails",
        },
    )
    baseline = JUnitReport(
        failures={
            repaired: "repaired on current",
            inherited: "inherited",
            deleted: "deleted from current",
        },
        test_routes={
            "src/repaired.test.ts::now passes",
            "src/inherited.test.ts::still fails",
            "src/deleted.test.ts::gone",
        },
    )
    recorded: list[tuple[dict[str, object], dict[str, str]]] = []

    def fake_run(
        _: Path,
        __: guard.Suite,
        report: Path,
        ___: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, stdout="", stderr="")

    def fake_report(path: Path, **_: object) -> JUnitReport:
        if path.name.endswith("execution.xml"):
            return _clean_execution_report()
        return current if path.name == "current.xml" else baseline

    def capture_record(
        _: str, outcome: dict[str, object], *, coverage_failures: dict[str, str] | None = None
    ) -> None:
        recorded.append((outcome, coverage_failures or {}))

    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "_load_roster", lambda *_: _permissive_roster())
    monkeypatch.setattr(guard, "extract_base", lambda *_: None)
    monkeypatch.setattr(guard, "_install_base", lambda *_: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(guard, "parse_junit_report", fake_report)
    monkeypatch.setattr(guard, "_record", capture_record)

    assert guard.main(["--suite", "agent", "--repo-root", str(tmp_path)]) == 1

    outcome, coverage_failures = recorded[0]
    assert outcome["blocking"] is False
    assert coverage_failures == {
        "src/deleted.test.ts::gone": "baseline test route is absent from the current report"
    }
    output = capsys.readouterr().out
    assert "WARN suppressed baseline failure: src/inherited.test.ts::still fails::failure" in output
    assert "FAILED state=coverage_regression" in output


def test_scoped_coverage_fails_closed_when_baseline_scope_is_wider() -> None:
    """Coverage is checked against the selectors the baseline actually ran."""

    baseline = JUnitReport(
        failures={
            "src/current.test.ts::inherited::failure": "inherited",
            "src/omitted.test.ts::deleted::failure": "deleted",
        },
        test_routes={
            "src/current.test.ts::inherited",
            "src/omitted.test.ts::deleted",
        },
    )

    gaps = guard._coverage_failures(
        JUnitReport(test_routes={"src/current.test.ts::inherited"}),
        baseline,
        ["src/current.test.ts", "src/omitted.test.ts"],
    )
    assert gaps == {
        "src/omitted.test.ts::deleted": "baseline test route is absent from the current report"
    }


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
    assert calls[1][1] is None
    assert "FAILED state=coverage_regression" in capsys.readouterr().out


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
    monkeypatch.setattr(guard, "_load_roster", lambda *_: _permissive_roster())
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
    monkeypatch.setattr(guard, "_load_roster", lambda *_: _permissive_roster())
    monkeypatch.setattr(
        guard,
        "_changed_test_files",
        lambda *_: ["agent/src/new.test.ts"],
    )
    monkeypatch.setattr(
        guard,
        "_changed_files",
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
    agent_suite = guard.SUITES["agent"]
    admin_ui_suite = guard.SUITES["admin-ui"]
    agent_roster = guard._load_roster(REPO_ROOT, agent_suite)
    admin_ui_roster = guard._load_roster(REPO_ROOT, admin_ui_suite)

    # agent's real roster only includes `.test.ts`, so `.js` is filtered out -
    # unlike a permissive stub, this pins the suite's actual exclude/include
    # boundary, not just the diff-source union.
    assert guard._changed_test_files(
        tmp_path, "origin/automain", agent_suite, agent_roster
    ) == [
        "agent/src/committed.test.ts",
        "agent/src/untracked.test.ts",
        "agent/src/working.test.ts",
    ]
    assert guard._changed_test_files(
        tmp_path, "origin/automain", admin_ui_suite, admin_ui_roster
    ) == ["admin-ui/src/admin.test.tsx"]


def test_load_roster_resolves_admin_ui_config_include_and_exclude() -> None:
    """The roster is derived from admin-ui's own config, not a hand-kept copy.

    admin-ui/vitest.config.ts excludes `src/**/*.viewport.test.{ts,tsx}`; a
    hardcoded suffix-only copy of the include half cannot know that.
    """

    suite = guard.SUITES["admin-ui"]
    roster = guard._load_roster(REPO_ROOT, suite)
    assert guard._is_suite_test_file("admin-ui/src/components/Example.test.tsx", suite, roster)
    assert guard._is_suite_test_file("admin-ui/eslint-rules/example.test.js", suite, roster)
    assert not guard._is_suite_test_file(
        "admin-ui/src/components/Example.viewport.test.tsx", suite, roster
    )


def test_load_roster_resolves_agent_config_include() -> None:
    suite = guard.SUITES["agent"]
    roster = guard._load_roster(REPO_ROOT, suite)
    assert guard._is_suite_test_file("agent/src/prompts/check-helper.test.ts", suite, roster)
    assert not guard._is_suite_test_file("agent/src/prompts/check-helper.ts", suite, roster)


def test_guard_blocks_a_baseline_failure_removed_by_a_vitest_config_exclude(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A diff that only widens vitest.config.ts's exclude glob cannot make a
    failing baseline test vanish undetected.

    This reproduces the reported hole directly: the candidate tree's config
    now excludes `src/flaky.test.ts`, so a clean current run has zero paths
    matching the OLD hardcoded prefix/suffix roster (the config itself has
    neither a `src/` prefix nor a `.test.ts` suffix), and the guard used to
    take the cheap-skip branch without ever installing or running a baseline.
    """

    suite = guard.SUITES["admin-ui"]
    config_file = tmp_path / suite.config_path
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        "export default {\n"
        "  test: {\n"
        "    include: ['src/**/*.test.{ts,tsx}'],\n"
        "    exclude: ['node_modules/**', 'src/flaky.test.ts'],\n"
        "  },\n"
        "};\n",
        encoding="utf-8",
    )

    def fake_run(
        root: Path, _: guard.Suite, report: Path, __: Path, selectors: list[str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        exit_code = 0 if report.name == "current.xml" else 1
        return subprocess.CompletedProcess([], exit_code, stdout="", stderr="")

    def fake_report(path: Path, **_: object) -> JUnitReport:
        if path.name.endswith("execution.xml"):
            return _clean_execution_report()
        if path.name == "current.xml":
            return JUnitReport(test_routes={"src/other.test.ts::passes"})
        return JUnitReport(
            failures={"src/flaky.test.ts::breaks::failure": "inherited failure"},
            test_routes={"src/flaky.test.ts::breaks"},
        )

    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "_changed_files", lambda *_: [suite.config_path])
    monkeypatch.setattr(guard, "extract_base", lambda *_: None)
    monkeypatch.setattr(guard, "_install_base", lambda *_: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(guard, "parse_junit_report", fake_report)

    assert guard.main(["--suite", "admin-ui", "--repo-root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "baseline scoped to the full suite" in output
    assert "FAILED state=coverage_regression" in output


def test_runnable_changed_test_files_drops_deleted_and_pre_rename_paths(
    tmp_path: Path,
) -> None:
    """A real git delete and rename must not survive into the current-side scope.

    The raw diff still names both paths -- the base arm needs them to rerun the
    baseline -- but neither is executable on the current side.
    """

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "automain")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    components = tmp_path / "admin-ui" / "src" / "components"
    components.mkdir(parents=True)
    for name in ("Gone", "OldName", "Kept"):
        # Distinct bodies keep git's rename detection deterministic: identical
        # files let it pair the delete with the rename target instead.
        (components / f"{name}.viewport.test.tsx").write_text(
            f"// {name}\n", encoding="utf-8"
        )
    git("add", "-A")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "work")
    (components / "Gone.viewport.test.tsx").unlink()
    (components / "OldName.viewport.test.tsx").rename(
        components / "NewName.viewport.test.tsx"
    )
    (components / "Kept.viewport.test.tsx").write_text(
        "// Kept, edited\n", encoding="utf-8"
    )
    git("add", "-A")
    git("commit", "-qm", "delete, rename, and edit viewport tests")

    suite = guard.SUITES["admin-ui-viewport"]
    prefix = "admin-ui/src/components/"
    # Rename detection collapses OldName -> NewName to the destination alone;
    # the delete stays in the raw diff, which is what the base arm needs.
    roster = guard._load_roster(REPO_ROOT, suite)
    assert guard._changed_test_files(tmp_path, "automain", suite, roster) == [
        f"{prefix}Gone.viewport.test.tsx",
        f"{prefix}Kept.viewport.test.tsx",
        f"{prefix}NewName.viewport.test.tsx",
    ]
    assert guard._runnable_changed_test_files(tmp_path, "automain", suite, roster) == [
        f"{prefix}Kept.viewport.test.tsx",
        f"{prefix}NewName.viewport.test.tsx",
    ]


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
    monkeypatch.setattr(guard, "_load_roster", lambda *_: _permissive_roster())
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
    monkeypatch.setattr(guard, "_load_roster", lambda *_: _permissive_roster())
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
    monkeypatch.setattr(guard, "_load_roster", lambda *_: _permissive_roster())
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
    monkeypatch.setattr(guard, "_load_roster", lambda *_: _permissive_roster())
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


# --------------------------------------------------------------------------
# admin-ui browser-mode viewport lane. The bespoke bash comparison this
# replaces waived every current-side viewport failure whenever the base-side
# chain (npm ci, `playwright install`, or the run itself) exited non-zero.
# --------------------------------------------------------------------------

_VIEWPORT_TEST = "admin-ui/src/components/Widget.viewport.test.tsx"
_VIEWPORT_FAILURE = (
    "src/components/Widget.viewport.test.tsx::overflows at 375px::failure"
)


def _viewport_failure_report(detail: str) -> JUnitReport:
    route = "src/components/Widget.viewport.test.tsx::overflows at 375px"
    return JUnitReport(
        failures={_VIEWPORT_FAILURE: detail},
        test_routes={route},
        selectors={_VIEWPORT_FAILURE: route},
    )


def _write_suite_config(root: Path, suite_name: str) -> None:
    """Copy a suite's REAL Vitest config into a synthetic tree.

    The guard resolves a suite's roster from that config, so a fixture tree
    without it is not just under-specified — it is a tree the guard correctly
    refuses to attribute. Copying the shipped file (rather than stubbing
    `_load_roster`) keeps these fixtures honest about the globs production uses.
    """

    suite = guard.SUITES[suite_name]
    destination = root / suite.config_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / suite.config_path, destination)


def _write_viewport_test(root: Path) -> None:
    path = root / _VIEWPORT_TEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    _write_suite_config(root, "admin-ui-viewport")


_VIEWPORT_DELETED_TEST = "admin-ui/src/components/Deleted.viewport.test.tsx"
_VIEWPORT_INHERITED = "src/components/Widget.viewport.test.tsx::inherited::failure"
_VIEWPORT_REPAIRED = "src/components/Widget.viewport.test.tsx::repaired::failure"
_VIEWPORT_DELETED = "src/components/Deleted.viewport.test.tsx::deleted::failure"
_VIEWPORT_OUTSIDE = "src/components/Other.viewport.test.tsx::outside::failure"
_VIEWPORT_NEW = "src/components/Widget.viewport.test.tsx::new::failure"


def _viewport_report(failures: dict[str, str], *extra_routes: str) -> JUnitReport:
    return JUnitReport(
        failures=failures,
        test_routes={key.rsplit("::", 1)[0] for key in failures} | set(extra_routes),
    )


@pytest.mark.parametrize(
    ("current", "baseline", "changed", "expected_exit", "expected_state"),
    [
        pytest.param(
            _viewport_report({_VIEWPORT_INHERITED: "inherited"}),
            _viewport_report({_VIEWPORT_INHERITED: "inherited", _VIEWPORT_OUTSIDE: "unrelated base failure"}),
            [_VIEWPORT_TEST],
            0,
            "preexisting_test_failure",
        ),
        pytest.param(
            _viewport_report({_VIEWPORT_INHERITED: "inherited"}),
            _viewport_report({_VIEWPORT_INHERITED: "inherited", _VIEWPORT_DELETED: "deleted base failure"}),
            [_VIEWPORT_TEST, _VIEWPORT_DELETED_TEST],
            1,
            "coverage_regression",
        ),
        pytest.param(
            _viewport_report({_VIEWPORT_INHERITED: "inherited"}, "src/components/Widget.viewport.test.tsx::repaired"),
            _viewport_report({_VIEWPORT_INHERITED: "inherited", _VIEWPORT_REPAIRED: "repaired base failure"}),
            [_VIEWPORT_TEST],
            0,
            "baseline_failure_disappeared",
        ),
        pytest.param(
            _viewport_report({_VIEWPORT_NEW: "introduced failure"}),
            _viewport_report({_VIEWPORT_OUTSIDE: "unrelated base failure"}),
            [_VIEWPORT_TEST],
            1,
            "test_failure",
        ),
    ],
)
def test_scoped_viewport_reaches_an_attributed_verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    current: JUnitReport,
    baseline: JUnitReport,
    changed: list[str],
    expected_exit: int,
    expected_state: str,
) -> None:
    """A scoped browser lane attributes its executed base scope, not full-base debt."""

    _write_viewport_test(tmp_path)
    runs: list[tuple[Path, list[str] | None]] = []
    recorded: list[dict[str, object]] = []
    reports: dict[str, JUnitReport] = {}
    def fake_run(
        root: Path,
        _: guard.Suite,
        report: Path,
        __: Path,
        selectors: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        runs.append((root, selectors))
        if report.name == "current.xml":
            result = current
        elif selectors is None:
            result = baseline
        else:
            selected = set(selectors)
            result = JUnitReport(
                failures={key: detail for key, detail in baseline.failures.items() if key.split("::", 1)[0] in selected},
                test_routes={route for route in baseline.test_routes if route.split("::", 1)[0] in selected},
            )
        reports[report.name] = result
        return subprocess.CompletedProcess(
            [], 1 if result.failures else 0, stdout="", stderr=""
        )

    def fake_extract(_: Path, __: str, destination: Path) -> None:
        _write_viewport_test(destination)
        deleted = destination / _VIEWPORT_DELETED_TEST
        deleted.parent.mkdir(parents=True, exist_ok=True)
        deleted.touch()

    def fake_report(path: Path, **_: object) -> JUnitReport:
        if path.name.endswith("execution.xml"):
            return _clean_execution_report()
        if path.name == "current.xml":
            return current
        return reports["baseline.xml"]
    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "_changed_test_files", lambda *_: changed)
    monkeypatch.setattr(guard, "_changed_files", lambda *_: [])
    monkeypatch.setattr(guard, "_install_base", lambda *_: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(guard, "_install_base_browser", lambda *_: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(guard, "extract_base", fake_extract)
    monkeypatch.setattr(guard, "parse_junit_report", fake_report)
    monkeypatch.setattr(
        guard,
        "_record",
        lambda _, outcome, **__: recorded.append(outcome),
    )

    result = guard.main(
        [
            "--suite",
            "admin-ui-viewport",
            "--repo-root",
            str(tmp_path),
            "--current-files",
            _VIEWPORT_TEST,
        ]
    )

    assert result == expected_exit
    output = capsys.readouterr().out
    assert f"state={expected_state}" in output and "coverage_scope_unavailable" not in output
    assert runs[0][1] == ["src/components/Widget.viewport.test.tsx"]
    assert runs[1][1] == (
        [
            "src/components/Deleted.viewport.test.tsx",
            "src/components/Widget.viewport.test.tsx",
        ]
        if len(changed) > 1
        else ["src/components/Widget.viewport.test.tsx"]
    )
    if len(changed) > 1:
        assert "src/components/Deleted.viewport.test.tsx::deleted" in output
    if expected_state == "baseline_failure_disappeared":
        disappeared = cast(dict[str, dict[str, str]], recorded[0]["disappeared_failures"])
        assert disappeared[_VIEWPORT_REPAIRED]["reason"] == (
            "baseline failure no longer reproduces in current report"
        )
    if expected_state == "test_failure":
        new_failures = cast(dict[str, str], recorded[0]["new_failures"])
        assert _VIEWPORT_NEW in new_failures


def test_viewport_and_unit_suites_own_disjoint_admin_ui_rosters() -> None:
    """The two admin-ui configs must never claim the same test file."""

    unit = guard.SUITES["admin-ui"]
    viewport = guard.SUITES["admin-ui-viewport"]
    # Each roster comes from that suite's OWN vitest config, so this asserts the
    # real shipped split rather than a hand-kept copy of it.
    unit_roster = guard._load_roster(REPO_ROOT, unit)
    viewport_roster = guard._load_roster(REPO_ROOT, viewport)

    assert guard._is_suite_test_file(_VIEWPORT_TEST, viewport, viewport_roster) is True
    assert guard._is_suite_test_file(_VIEWPORT_TEST, unit, unit_roster) is False
    assert guard._is_suite_test_file(
        "admin-ui/src/components/Widget.viewport.test.ts", unit, unit_roster
    ) is False
    assert guard._is_suite_test_file(
        "admin-ui/src/components/Widget.test.tsx", unit, unit_roster
    ) is True
    assert guard._is_suite_test_file(
        "admin-ui/src/components/Widget.test.tsx", viewport, viewport_roster
    ) is False
    assert guard._is_suite_test_file(
        "admin-ui/eslint-rules/rule.test.js", viewport, viewport_roster
    ) is False


def test_viewport_run_uses_the_browser_config_env_and_failure_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The viewport lane must reproduce what the bespoke gate command supplied."""

    captured: dict[str, object] = {}

    def fake_subprocess_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_subprocess_run)
    report = tmp_path / "current.xml"

    guard._run_vitest(
        tmp_path,
        guard.SUITES["admin-ui-viewport"],
        report,
        tmp_path / "current-execution.xml",
        selectors=["src/components/Widget.viewport.test.tsx"],
    )

    command = captured["command"]
    assert isinstance(command, list)
    # `npm run test:viewport` chains the 247-file unit suite ahead of the
    # browser run; the guard must invoke vitest directly instead.
    assert command[:3] == ["npx", "vitest", "run"]
    assert "npm" not in command
    assert command[3] == "--config=vitest.viewport.config.ts"
    assert command[4] == "src/components/Widget.viewport.test.tsx"
    assert f"--outputFile.junit={report}" in command
    assert "--reporter=json" in command
    assert "--outputFile.json=./test-results/viewport-failures.json" in command
    assert captured["cwd"] == tmp_path / "admin-ui"
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["VITE_API_URL"] == "http://localhost:8080"
    assert env["VITE_FIREBASE_API_KEY"] == "ci"
    assert env["VITE_FIREBASE_AUTH_DOMAIN"] == "ci.firebaseapp.com"
    assert env["VITE_FIREBASE_PROJECT_ID"] == "ci"


def test_unit_suites_keep_their_default_config_and_reporters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Adding the viewport lane must not perturb the happy-dom lanes' command."""

    captured: dict[str, object] = {}

    def fake_subprocess_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_subprocess_run)
    guard._run_vitest(
        tmp_path,
        guard.SUITES["admin-ui"],
        tmp_path / "current.xml",
        tmp_path / "current-execution.xml",
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert not [flag for flag in command if flag.startswith("--config")]
    assert "--reporter=json" not in command
    assert not [flag for flag in command if flag.startswith("--outputFile.json")]
    for key in ("VITE_API_URL", "VITE_FIREBASE_API_KEY"):
        assert key not in set(captured["env"]) - set(os.environ)


def test_viewport_regression_blocks_when_the_base_run_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A base-side Chromium install failure must not waive a current failure.

    This is the exact defect of the retired whole-run bash comparison: any
    non-zero base-side chain -- npm ci, `playwright install`, or the run --
    was read as "also broken on base" and disarmed the entire viewport gate.
    """

    runs: list[tuple[Path, list[str] | None]] = []

    def fake_run(
        root: Path,
        _: guard.Suite,
        __: Path,
        ___: Path,
        selectors: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        runs.append((root, selectors))
        return subprocess.CompletedProcess([], 1, stdout="375px overflow", stderr="")

    def fake_extract(_: Path, __: str, destination: Path) -> None:
        _write_viewport_test(destination)

    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "_install_base", lambda *_: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(
        guard,
        "_install_base_browser",
        lambda *_: subprocess.CompletedProcess([], 1, stdout="", stderr="chromium download failed"),
    )
    monkeypatch.setattr(guard, "extract_base", fake_extract)
    monkeypatch.setattr(
        guard,
        "parse_junit_report",
        lambda path, **_: _clean_execution_report()
        if path.name.endswith("execution.xml")
        else _viewport_failure_report("current regression"),
    )

    assert (
        guard.main(
            ["--suite", "admin-ui-viewport", "--repo-root", str(tmp_path)]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "FAILED state=baseline_unavailable" in output
    assert "base_browser_install_exit_code=1" in output
    assert "WARN suppressed baseline failure" not in output
    # Only the current side ever ran; the base run was never reached.
    assert len(runs) == 1


def test_viewport_blocks_when_the_base_npm_ci_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The install arm of the base chain is fail-closed for the browser lane too."""

    def fake_run(
        _: Path, __: guard.Suite, ___: Path, ____: Path, selectors: list[str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, stdout="", stderr="")

    def unexpected_browser(*_: object, **__: object) -> None:
        pytest.fail("a failed base install must not proceed to the browser bootstrap")

    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "_install_base", lambda *_: subprocess.CompletedProcess([], 1))
    monkeypatch.setattr(guard, "_install_base_browser", unexpected_browser)
    monkeypatch.setattr(guard, "extract_base", lambda _, __, destination: _write_viewport_test(destination))
    monkeypatch.setattr(
        guard,
        "parse_junit_report",
        lambda path, **_: _clean_execution_report()
        if path.name.endswith("execution.xml")
        else _viewport_failure_report("current regression"),
    )

    assert guard.main(["--suite", "admin-ui-viewport", "--repo-root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "FAILED state=baseline_unavailable" in output
    assert "base_install_exit_code=1" in output


def test_viewport_warn_passes_an_inherited_failure_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An identical base-side viewport identity is inherited, not introduced."""

    runs: list[tuple[Path, list[str] | None]] = []

    def fake_run(
        root: Path,
        _: guard.Suite,
        __: Path,
        ___: Path,
        selectors: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        runs.append((root, selectors))
        return subprocess.CompletedProcess([], 1, stdout="", stderr="")

    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "_install_base", lambda *_: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(
        guard, "_install_base_browser", lambda *_: subprocess.CompletedProcess([], 0)
    )
    monkeypatch.setattr(guard, "extract_base", lambda _, __, destination: _write_viewport_test(destination))
    monkeypatch.setattr(
        guard,
        "parse_junit_report",
        lambda path, **_: _clean_execution_report()
        if path.name.endswith("execution.xml")
        else _viewport_failure_report("inherited overflow"),
    )

    assert guard.main(["--suite", "admin-ui-viewport", "--repo-root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert f"WARN suppressed baseline failure: {_VIEWPORT_FAILURE}" in output
    assert "PASS state=" in output
    # The base arm reruns only the failing file, which exists in the base tree.
    assert runs[1][1] == ["src/components/Widget.viewport.test.tsx"]


def test_current_files_scopes_only_the_current_side_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Serialized browser runs need an explicit current scope; base scoping is unchanged."""

    _write_viewport_test(tmp_path)
    runs: list[tuple[Path, list[str] | None]] = []

    def fake_run(
        root: Path,
        _: guard.Suite,
        report: Path,
        __: Path,
        selectors: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        runs.append((root, selectors))
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "_changed_test_files", lambda *_: [_VIEWPORT_TEST])
    # The roster-config-change probe reads the raw diff too; stub it beside
    # the roster-filtered list so a scoped fixture never shells out to git.
    monkeypatch.setattr(guard, "_changed_files", lambda *_: [])
    monkeypatch.setattr(guard, "_install_base", lambda *_: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(
        guard, "_install_base_browser", lambda *_: subprocess.CompletedProcess([], 0)
    )
    monkeypatch.setattr(guard, "extract_base", lambda _, __, destination: _write_viewport_test(destination))
    monkeypatch.setattr(
        guard,
        "parse_junit_report",
        lambda path, **_: _clean_execution_report()
        if path.name.endswith("execution.xml")
        else JUnitReport(
            test_routes={"src/components/Widget.viewport.test.tsx::fits at 375px"}
        ),
    )

    assert (
        guard.main(
            [
                "--suite",
                "admin-ui-viewport",
                "--repo-root",
                str(tmp_path),
                "--current-files",
                _VIEWPORT_TEST,
            ]
        )
        == 0
    )
    assert runs[0][1] == ["src/components/Widget.viewport.test.tsx"]
    assert runs[1][1] == ["src/components/Widget.viewport.test.tsx"]
    assert "PASS state=passed" in capsys.readouterr().out


def test_current_files_blocks_when_it_omits_a_changed_suite_test_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A scope that skips a changed viewport test proves nothing about it."""

    _write_viewport_test(tmp_path)
    other = "admin-ui/src/components/Other.viewport.test.tsx"
    (tmp_path / other).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / other).write_text("// still in the tree\n", encoding="utf-8")

    def unexpected_run(*_: object, **__: object) -> None:
        pytest.fail("an incomplete current scope must not execute any suite")

    monkeypatch.setattr(guard, "_run_vitest", unexpected_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "_changed_test_files", lambda *_: [_VIEWPORT_TEST, other])
    # The roster-config-change probe reads the raw diff too; stub it beside
    # the roster-filtered list so a scoped fixture never shells out to git.
    monkeypatch.setattr(guard, "_changed_files", lambda *_: [])

    assert (
        guard.main(
            [
                "--suite",
                "admin-ui-viewport",
                "--repo-root",
                str(tmp_path),
                "--current-files",
                _VIEWPORT_TEST,
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "FAILED state=current_scope_incomplete" in output
    assert f"unrun_changed_test_files={other}" in output


@pytest.mark.parametrize(
    ("current_files", "expected_error"),
    [
        pytest.param(
            ["admin-ui/src/components/Widget.test.tsx"],
            "is not a suite test file",
            id="wrong-roster",
        ),
        pytest.param(
            ["admin-ui/src/components/Missing.viewport.test.tsx"],
            "does not exist",
            id="absent-from-tree",
        ),
        pytest.param([], "no test file", id="empty-scope"),
    ],
)
def test_current_files_rejects_a_scope_the_suite_would_not_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    current_files: list[str],
    expected_error: str,
) -> None:
    """An unrunnable scope entry is blocking, never a silently narrowed run."""

    def unexpected_run(*_: object, **__: object) -> None:
        pytest.fail("an invalid current scope must not execute any suite")

    monkeypatch.setattr(guard, "_run_vitest", unexpected_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "_changed_test_files", lambda *_: [])
    # The roster-config-change probe reads the raw diff too; stub it beside
    # the roster-filtered list so a scoped fixture never shells out to git.
    monkeypatch.setattr(guard, "_changed_files", lambda *_: [])

    assert (
        guard.main(
            [
                "--suite",
                "admin-ui-viewport",
                "--repo-root",
                str(tmp_path),
                "--current-files",
                *current_files,
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "FAILED state=current_scope_unavailable" in output
    assert expected_error in output


def test_viewport_config_still_declares_the_manifest_the_guard_restates() -> None:
    """Pin the manifest path the guard's explicit reporter flags must preserve."""

    config = (REPO_ROOT / "admin-ui" / "vitest.viewport.config.ts").read_text(encoding="utf-8")
    assert "'./test-results/viewport-failures.json'" in config
    assert guard.SUITES["admin-ui-viewport"].json_report == "./test-results/viewport-failures.json"
    assert "src/**/*.viewport.test.{ts,tsx}" in config


def test_base_browser_bootstrap_reuses_the_cache_before_downloading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cached Chromium must satisfy the base bootstrap without a download."""

    cache = tmp_path / "ms-playwright"
    (cache / "chromium-1234").mkdir(parents=True)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))

    def unexpected_run(*_: object, **__: object) -> None:
        pytest.fail("a cached browser must not trigger `playwright install`")

    monkeypatch.setattr(guard.subprocess, "run", unexpected_run)
    result = guard._install_base_browser(tmp_path, guard.SUITES["admin-ui-viewport"])
    assert result.returncode == 0


def test_base_browser_bootstrap_installs_chromium_when_uncached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty cache falls through to the same install the gate ran."""

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "empty"))
    captured: dict[str, object] = {}

    def fake_subprocess_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_subprocess_run)
    result = guard._install_base_browser(tmp_path, guard.SUITES["admin-ui-viewport"])

    assert result.returncode == 0
    assert captured["command"] == ["npx", "playwright", "install", "chromium"]
    assert captured["cwd"] == tmp_path / "admin-ui"


def _scoped_run_recorder(
    monkeypatch: pytest.MonkeyPatch, changed: list[str]
) -> list[list[str] | None]:
    """Stub the suite runs for a scoped viewport gate over `changed` test files."""

    recorded: list[list[str] | None] = []

    def fake_run(
        _: Path,
        __: guard.Suite,
        ___: Path,
        ____: Path,
        selectors: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        recorded.append(selectors)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(guard, "_changed_test_files", lambda *_: changed)
    # The roster-config-change probe reads the raw diff too; stub it beside
    # the roster-filtered list so a scoped fixture never shells out to git.
    monkeypatch.setattr(guard, "_changed_files", lambda *_: [])
    monkeypatch.setattr(guard, "_install_base", lambda *_: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(
        guard, "_install_base_browser", lambda *_: subprocess.CompletedProcess([], 0)
    )
    monkeypatch.setattr(
        guard, "extract_base", lambda _, __, destination: _write_viewport_test(destination)
    )
    monkeypatch.setattr(
        guard,
        "parse_junit_report",
        lambda path, **_: _clean_execution_report()
        if path.name.endswith("execution.xml")
        else JUnitReport(
            test_routes={"src/components/Widget.viewport.test.tsx::fits at 375px"}
        ),
    )
    return recorded


def test_current_files_ignores_a_deleted_changed_suite_test_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A test the diff DELETES has nothing left to prove on the current side.

    The caller can never include it: it is absent from the working tree, so
    `--current-files` would reject it outright. Treating it as unrun scope
    would hard-block the lane with no remedy.
    """

    _write_viewport_test(tmp_path)
    deleted = "admin-ui/src/components/Gone.viewport.test.tsx"
    selectors = _scoped_run_recorder(monkeypatch, [_VIEWPORT_TEST, deleted])

    assert (
        guard.main(
            [
                "--suite",
                "admin-ui-viewport",
                "--repo-root",
                str(tmp_path),
                "--current-files",
                _VIEWPORT_TEST,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "current_scope_incomplete" not in output
    assert "PASS state=passed" in output
    assert selectors[0] == ["src/components/Widget.viewport.test.tsx"]


def test_current_files_ignores_the_old_path_of_a_renamed_suite_test_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rename shows both paths as changed; only the surviving one is runnable."""

    _write_viewport_test(tmp_path)
    old_path = "admin-ui/src/components/OldName.viewport.test.tsx"
    selectors = _scoped_run_recorder(monkeypatch, [old_path, _VIEWPORT_TEST])

    assert (
        guard.main(
            [
                "--suite",
                "admin-ui-viewport",
                "--repo-root",
                str(tmp_path),
                "--current-files",
                _VIEWPORT_TEST,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "current_scope_incomplete" not in output
    assert "PASS state=passed" in output
    assert selectors[0] == ["src/components/Widget.viewport.test.tsx"]


def _stub_inherited_vitest_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, suite: str
) -> None:
    """Stub both Vitest runs so the same named failure appears in each tree."""

    def fake_run(
        _: Path, __: guard.Suite, ___: Path, ____: Path
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, stdout="vitest output", stderr="")

    def fake_report(path: Path, **_: object) -> JUnitReport:
        if path.name.endswith("execution.xml"):
            return _clean_execution_report()
        return JUnitReport(
            failures={_NAMED_FAILURE: "expected inherited failure"},
            test_routes={"src/example.test.ts::fails"},
        )

    monkeypatch.setattr(guard, "_run_vitest", fake_run)
    monkeypatch.setattr(guard, "_install_base", lambda *_: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(guard, "_base_ref", lambda *_: "origin/automain")
    monkeypatch.setattr(
        guard,
        "extract_base",
        lambda _, __, destination: (destination / guard.SUITES[suite].directory).mkdir(),
    )
    monkeypatch.setattr(guard, "parse_junit_report", fake_report)


@pytest.mark.parametrize("suite", ["agent", "admin-ui"])
def test_suppressed_baseline_failures_are_written_to_the_shared_verification_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, suite: str
) -> None:
    """The Vitest lane's suppressions become durable, structured receipt data.

    Before this contract the identity below existed only on stdout, so a
    publication that relied on an inherited baseline failure presented as
    fully clean to every downstream reviewer.
    """

    receipt_path = tmp_path / "receipt.json"
    monkeypatch.setenv("BLUEPRINT_VERIFICATION_RECEIPT", str(receipt_path))
    _stub_inherited_vitest_failure(monkeypatch, tmp_path, suite)

    assert guard.main(["--suite", suite, "--repo-root", str(tmp_path)]) == 0

    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    block = payload["lanes"][f"vitest:{suite}"]
    assert block["lane"] == f"vitest:{suite}"
    assert block["state"] == "preexisting_test_failure"
    assert block["blocking"] is False
    assert block["new_failures"] == {}
    assert list(block["suppressed_failures"]) == [_NAMED_FAILURE]
    assert block["suppressed_failures"][_NAMED_FAILURE] == {
        "detail": "expected inherited failure",
        "reason": "same route/test/failure kind failed on baseline",
    }


def test_each_vitest_lane_keeps_its_own_receipt_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two suites in one gate run accumulate; the second must not clobber."""

    receipt_path = tmp_path / "receipt.json"
    monkeypatch.setenv("BLUEPRINT_VERIFICATION_RECEIPT", str(receipt_path))
    for suite in ("agent", "admin-ui"):
        _stub_inherited_vitest_failure(monkeypatch, tmp_path, suite)
        assert guard.main(["--suite", suite, "--repo-root", str(tmp_path)]) == 0

    lanes = json.loads(receipt_path.read_text(encoding="utf-8"))["lanes"]
    assert set(lanes) == {"vitest:agent", "vitest:admin-ui"}
    for lane in lanes.values():
        assert list(lane["suppressed_failures"]) == [_NAMED_FAILURE]


def test_an_unwritable_receipt_does_not_change_the_vitest_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A receipt that cannot be written warns but never re-decides the lane."""

    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")
    monkeypatch.setenv("BLUEPRINT_VERIFICATION_RECEIPT", str(blocked / "receipt.json"))
    _stub_inherited_vitest_failure(monkeypatch, tmp_path, "agent")

    assert guard.main(["--suite", "agent", "--repo-root", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "PASS state=preexisting_test_failure" in captured.out
    assert "could not record lane vitest:agent" in captured.err


def test_a_lane_without_a_receipt_path_still_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No task identity means no receipt — not a new fail-closed gate."""

    monkeypatch.delenv("BLUEPRINT_VERIFICATION_RECEIPT", raising=False)
    monkeypatch.delenv("TASK_ID", raising=False)
    _stub_inherited_vitest_failure(monkeypatch, tmp_path, "agent")

    assert guard.main(["--suite", "agent", "--repo-root", str(tmp_path)]) == 0
    assert "PASS state=preexisting_test_failure" in capsys.readouterr().out


def test_the_gate_exports_the_receipt_path_every_lane_writes_to() -> None:
    """Both gate shells hand the guards one already-resolved sidecar path."""

    for source in (
        REPO_ROOT / "scripts" / "check-default.sh",
        REPO_ROOT / "agent" / "src" / "prompts" / "check-helper.ts",
    ):
        text = source.read_text(encoding="utf-8")
        assert 'BLUEPRINT_VERIFICATION_RECEIPT="$_VERIFY_RECEIPT"' in text
        assert "export BLUEPRINT_VERIFICATION_RECEIPT" in text


def test_a_guard_test_run_leaves_the_gate_resolved_sidecar_untouched(
    tmp_path: Path,
) -> None:
    """Running this module must not write into the real durable receipt.

    The static-corpus lane runs the whole ``scripts/test_*.py`` roster inside
    ./check, before the real Vitest lanes and with the gate's TASK_ID in the
    environment.  If these tests resolved the ambient sidecar, they would
    publish fixture lane blocks -- including fabricated suppressed baseline
    failures -- as durable evidence for lanes that never ran.
    """

    task_id = f"receipt-isolation-{tmp_path.name}"
    sidecar = Path("/tmp") / f"project_verification_receipt_{task_id}.json"
    sentinel = '{"version": 1, "lanes": {}}\n'
    sidecar.write_text(sentinel, encoding="utf-8")
    environment = dict(os.environ)
    environment.pop("BLUEPRINT_VERIFICATION_RECEIPT", None)
    environment["TASK_ID"] = task_id
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(Path(__file__).resolve()),
                "-q",
                "-p",
                "no:cacheprovider",
                "-k",
                "not leaves_the_gate_resolved_sidecar_untouched",
            ],
            cwd=str(REPO_ROOT),
            env=environment,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert sidecar.read_text(encoding="utf-8") == sentinel
    finally:
        sidecar.unlink(missing_ok=True)

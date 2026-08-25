"""The repo-root ``./check`` gate must fail closed when the base diff is uncomputable.

Regression target: the changed-file computation in ``./check`` used to end in
``git diff ... 2>/dev/null || true``. When the base ref could not be resolved —
the NORMAL case on a truncated/shallow clone in CI and on task VMs — the error
was swallowed, the changed-set came back empty, every downstream gate found
nothing to check, and the run printed ``PASS`` with zero checks actually
executed. The empty set is indistinguishable in that code path from a genuine
no-op diff, so the gate reported green exactly when it was least entitled to.

The route tests below also pin the backend PostgreSQL capability selection used
by the gate and harness.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from testing.fixgate_attribution import (
    JUnitReport,
    _normalize_failure_detail,
    attribute_reports,
    classify_verification,
    compare_structured,
    format_offender_marker,
    parse_junit_report,
    test_route_coverage_gaps as route_coverage_gaps,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if not (_REPO_ROOT / "check").exists() and Path("/repo/check").exists():
    _REPO_ROOT = Path("/repo")
_CHECK = _REPO_ROOT / "check"
_DEFAULT_CHECK = _REPO_ROOT / "scripts" / "check-default.sh"
_ROUTE_HELPER = _REPO_ROOT / "scripts" / "backend-test-route.sh"

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
static_release_guard = importlib.import_module("static_release_guard")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command inside the throwaway repo."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )


def _install_developer_check(repo: Path) -> None:
    """Install the tracked developer gate where the dispatcher resolves it."""
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    destination = scripts_dir / "check-default.sh"
    shutil.copy2(_DEFAULT_CHECK, destination)
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR)


def _install_static_release_guard(repo: Path) -> None:
    """Install the size-guard helper the gate now requires on every path.

    ``scripts/check-default.sh`` runs ``static_release_guard.py --size-only``
    on the lint-only path and the full attributed corpus under ``check test``,
    and fails closed when the helper is absent. The real helper imports the
    backend package, so these gate fixtures install a zero-exit stand-in: the
    subject of this module is the changed-file computation, not the size
    ledger, and the missing-helper branch is exercised separately.
    """
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "static_release_guard.py").write_text(
        "raise SystemExit(0)\n", encoding="utf-8"
    )


def _install_sentinel_freshness(repo: Path) -> None:
    """Install the derived-artifact freshness helper the gate also requires.

    Same contract and same reason as :func:`_install_static_release_guard`:
    ``scripts/check-default.sh`` runs ``sentinel_freshness.py`` on the
    lint-only path and fails closed when the helper is absent, and the real
    helper drives the pytest harness. The subject of this module is the
    changed-file computation, so a zero-exit stand-in is installed here and
    the missing-helper branch is exercised separately.
    """
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "sentinel_freshness.py").write_text(
        "raise SystemExit(0)\n", encoding="utf-8"
    )


def _init_repo(repo: Path) -> None:
    """Bootstrap a minimal repo with the tracked developer gate committed."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "check-gate-test@example.com")
    _git(repo, "config", "user.name", "check gate test")
    # A host-level gpg signing config would otherwise make the throwaway commit
    # try to sign (and fail) for reasons unrelated to the gate under test.
    _git(repo, "config", "commit.gpgsign", "false")
    # ``check`` is now a dispatcher: task work reaches ``.check-task`` while a
    # true developer checkout reaches this tracked fallback. Commit the fallback
    # into the fixture before creating the base ref so it does not manufacture
    # an unrelated untracked diff in tests whose subject is an empty diff.
    _install_developer_check(repo)
    # Commit the size-guard stand-in for the same reason: an untracked helper
    # would itself become a changed file in the empty-diff tests.
    _install_static_release_guard(repo)
    _install_sentinel_freshness(repo)
    (repo / "file.txt").write_text("hello\n", encoding="utf-8")
    _git(
        repo,
        "add",
        "file.txt",
        "scripts/check-default.sh",
        "scripts/static_release_guard.py",
        "scripts/sentinel_freshness.py",
    )
    _git(repo, "commit", "-q", "-m", "initial")


def _run_check(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the tracked ``./check`` script against the throwaway repo."""
    env = os.environ.copy()
    env["WORKSPACE"] = str(repo)
    # This fixture exercises the tracked developer route. A host/CI TASK_ID
    # must not accidentally select the generated task-gate route instead.
    env.pop("TASK_ID", None)
    return subprocess.run(
        ["bash", str(_CHECK), *args],
        capture_output=True,
        text=True,
        env=env,
        # 20s, not 60: the CI lanes run pytest with --timeout=30, so a 60s
        # inner budget could never fire -- the harness would kill this call
        # first and report a healthy `check` run as an unreproducible flake.
        # Measured worst case across this file's callers is 1.57s (~13x head).
        timeout=20,
    )


def test_uncomputable_base_diff_fails_closed(tmp_path: Path) -> None:
    """An unresolvable base ref is a hard failure, never a green run.

    The repo has a commit, but no ``origin/automain``, no local ``automain``,
    and no ``origin`` remote to fetch one from — so ``git diff ...automain...HEAD``
    cannot be computed. The gate must exit non-zero and name the base ref it
    tried to resolve, with an actionable fetch instruction.
    """
    bash = shutil.which("bash")
    git = shutil.which("git")
    if not bash:
        pytest.skip("bash not available on this host")
    if not git:
        pytest.skip("git not available on this host")

    repo = tmp_path / "repo"
    _init_repo(repo)
    # No origin remote and no base ref: the committed diff is uncomputable.
    result = _run_check(repo)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, (
        "check must FAIL CLOSED when the base diff cannot be computed; "
        f"got exit 0 with output:\n{combined}"
    )
    assert "automain" in combined, (
        "the diagnostic must name the base ref it tried to resolve; "
        f"output:\n{combined}"
    )
    assert "fetch" in combined.lower(), (
        "the diagnostic must tell the caller what to fetch; "
        f"output:\n{combined}"
    )


def test_genuine_noop_diff_still_exits_zero(tmp_path: Path) -> None:
    """A computed-but-empty committed diff must keep exiting zero.

    The repo has a local ``automain`` branch at HEAD and a clean working tree,
    so ``git diff automain...HEAD`` succeeds and genuinely reports no changed
    files. This is the legitimate pass the fail-closed change must preserve.
    """
    bash = shutil.which("bash")
    git = shutil.which("git")
    if not bash:
        pytest.skip("bash not available on this host")
    if not git:
        pytest.skip("git not available on this host")

    repo = tmp_path / "repo"
    _init_repo(repo)
    # Local base ref at HEAD -> the committed diff computes and is empty.
    _git(repo, "branch", "automain")
    result = _run_check(repo)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, (
        "a genuine no-op diff must still pass; "
        f"exit {result.returncode} with output:\n{combined}"
    )
    assert "PASS" in combined, f"expected the green PASS marker, got:\n{combined}"


@pytest.mark.parametrize(
    "case, expected_state",
    [
        ("changed_files", "setup_failure"),
        ("base_extract", "base_extraction_failure"),
        ("size", "size_failure"),
        ("size_no_corpus", "size_failure"),
        ("harness", "harness_unavailable"),
        ("inventory", "corpus_inventory_failure"),
        ("execution", "execution_failure"),
        ("attribution", "attribution_unavailable"),
        ("baseline", "baseline_unavailable"),
        ("introduced_failure", "test_failure"),
    ],
)
def test_static_release_guard_records_every_blocking_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, expected_state: str
) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    if case != "harness":
        (scripts / "test-backend.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    receipt = tmp_path / "static-receipt.json"
    monkeypatch.setenv("BLUEPRINT_VERIFICATION_RECEIPT", str(receipt))
    monkeypatch.setenv("TASK_ID", "static-receipt-test")
    monkeypatch.setattr(static_release_guard, "_changed_files", lambda *_args: ["backend/app/changed.py"])
    monkeypatch.setattr(static_release_guard, "_base_ref", lambda *_args: "automain")
    monkeypatch.setattr(static_release_guard, "_static_roster_changed", lambda *_args: False)
    monkeypatch.setattr(static_release_guard, "_standalone_corpus_inventory", lambda *_args: set())

    def extract_base(_repo: Path, _ref: str, destination: Path) -> None:
        (destination / "scripts").mkdir(parents=True, exist_ok=True)
        (destination / "scripts" / "test-backend.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (destination / "backend" / "testing").mkdir(parents=True, exist_ok=True)
        (destination / "backend" / "testing" / "test_x.py").write_text("pass\n", encoding="utf-8")

    if case == "changed_files":
        monkeypatch.setattr(static_release_guard, "_changed_files", lambda *_args: (_ for _ in ()).throw(RuntimeError("base diff unavailable")))
    elif case == "base_extract":
        monkeypatch.setattr(static_release_guard, "_extract_base", lambda *_args: (_ for _ in ()).throw(RuntimeError("archive unavailable")))
    else:
        monkeypatch.setattr(static_release_guard, "_extract_base", extract_base)

    failure_key = "backend/testing/test_x.py::test_x::failure"
    failed = case in {"baseline", "introduced_failure"}
    current_report = JUnitReport(
        failures={failure_key: "introduced failure"} if failed else {},
        test_routes={"backend/testing/test_x.py::test_x"} if failed else set(),
        selectors={failure_key: "backend/testing/test_x.py"} if failed else {},
    )

    def parse_junit(_path: Path, *, label: str, **_kwargs: object) -> JUnitReport:
        if case == "attribution" and label == "current":
            raise RuntimeError("current JUnit missing")
        if label == "current":
            return current_report
        return JUnitReport()

    monkeypatch.setattr(static_release_guard, "parse_junit_report", parse_junit)
    monkeypatch.setattr(
        static_release_guard,
        "_run_size_guard",
        lambda *_args: subprocess.CompletedProcess(
            [], 7 if case in {"size", "size_no_corpus"} else 0, "size output", "size error"
        ),
    )
    corpus_calls = 0

    def run_corpus(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal corpus_calls
        corpus_calls += 1
        if case == "execution":
            return subprocess.CompletedProcess([], 2, "runner output", "runner error")
        if case == "baseline" and corpus_calls == 2:
            return subprocess.CompletedProcess([], 2, "base output", "base error")
        return subprocess.CompletedProcess(
            [], 1 if case in {"baseline", "introduced_failure"} and corpus_calls == 1 else 0, "", ""
        )

    monkeypatch.setattr(static_release_guard, "_run_static_corpus", run_corpus)
    if case == "inventory":
        monkeypatch.setattr(
            static_release_guard,
            "_standalone_corpus_inventory",
            lambda *_args: (_ for _ in ()).throw(RuntimeError("inventory unavailable")),
        )

    arguments = ["--repo-root", str(repo)]
    if case == "size":
        arguments.append("--size-only")
    result = static_release_guard.main(arguments)

    assert result != 0
    lane = json.loads(receipt.read_text(encoding="utf-8"))["lanes"]["static-corpus"]
    assert lane["blocking"] is True
    assert lane["state"] == expected_state
    assert {"new_failures", "suppressed_failures", "disappeared_failures", "skipped_failures", "error", "guard_exit_code"} <= lane.keys()
    assert lane["error"]
    assert lane["guard_exit_code"] != 0
    if case == "introduced_failure":
        assert lane["new_failures"] == {failure_key: {"detail": "introduced failure"}}
    if case == "size_no_corpus":
        assert lane["guard_exit_code"] == 1


def test_missing_static_release_guard_fails_the_lint_only_path(
    tmp_path: Path,
) -> None:
    """An absent size-guard helper must fail the gate, not silently skip it.

    The size guard binds on EVERY path into the gate, including the lint-only
    invocation. A checkout that has lost ``scripts/static_release_guard.py``
    cannot prove its module-size ledger, so the gate must refuse rather than
    report a green PASS with the guard quietly unexecuted.
    """
    bash = shutil.which("bash")
    git = shutil.which("git")
    if not bash:
        pytest.skip("bash not available on this host")
    if not git:
        pytest.skip("git not available on this host")

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "branch", "automain")
    # Everything else about this repo is the green no-op case above; the only
    # difference is the removed helper.
    _git(repo, "rm", "-q", "scripts/static_release_guard.py")
    _git(repo, "commit", "-q", "-m", "drop the size guard helper")
    result = _run_check(repo)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, (
        "a checkout missing the size-guard helper must fail closed; "
        f"got exit 0 with output:\n{combined}"
    )
    assert "static release guard helper is missing" in combined, (
        "the diagnostic must name the missing helper; "
        f"output:\n{combined}"
    )
    assert "PASS" not in combined, (
        f"the gate must not report PASS with the size guard unexecuted:\n{combined}"
    )


def test_missing_sentinel_freshness_helper_fails_the_lint_only_path(
    tmp_path: Path,
) -> None:
    """An absent freshness helper must fail the gate, not silently skip it.

    Same contract as the size-guard helper above, for the same reason: the
    derived-artifact freshness lane is the only always-on check over checked-in
    guard data that is NOT the module-size ledger, and stale guard data is
    self-concealing (attributable on one PR, inherited-and-warn-passed on every
    PR after), so a silently skipped lane surfaces first at the automain->main
    promotion matrix.
    """
    bash = shutil.which("bash")
    git = shutil.which("git")
    if not bash:
        pytest.skip("bash not available on this host")
    if not git:
        pytest.skip("git not available on this host")

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "branch", "automain")
    _git(repo, "rm", "-q", "scripts/sentinel_freshness.py")
    _git(repo, "commit", "-q", "-m", "drop the freshness helper")
    result = _run_check(repo)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, (
        "a checkout missing the freshness helper must fail closed; "
        f"got exit 0 with output:\n{combined}"
    )
    assert "derived-artifact freshness helper is missing" in combined, (
        "the diagnostic must name the missing helper; "
        f"output:\n{combined}"
    )


def test_required_backend_verification_unavailable_is_a_hard_failure(
    tmp_path: Path,
) -> None:
    """Even ``check test`` cannot turn a missing PostgreSQL route into a skip."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "branch", "automain")
    (repo / "backend" / "app").mkdir(parents=True)
    (repo / "backend" / "testing").mkdir(parents=True)
    shutil.copy2(_ROUTE_HELPER, repo / "scripts" / "backend-test-route.sh")
    shutil.copy2(
        _REPO_ROOT / "backend" / "testing" / "impacted_tests.py",
        repo / "backend" / "testing" / "impacted_tests.py",
    )
    # impacted_tests.py delegates JUnit attribution to this sibling module.
    # Install the complete resolver boundary so this test reaches the intended
    # PostgreSQL-capability decision instead of manufacturing an import error.
    shutil.copy2(
        _REPO_ROOT / "backend" / "testing" / "fixgate_attribution.py",
        repo / "backend" / "testing" / "fixgate_attribution.py",
    )
    # The gate checks that the sanctioned harness exists before probing the
    # route. It must still fail on the unavailable route, without invoking it.
    harness = repo / "scripts" / "test-backend.sh"
    harness.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    harness.chmod(harness.stat().st_mode | stat.S_IXUSR)
    # Keep the broad test-gate prelude green so the only final failure is the
    # required targeted verification capability decision under test.
    (repo / "scripts" / "static_release_guard.py").write_text(
        "raise SystemExit(0)\n", encoding="utf-8"
    )

    (repo / "backend" / "app" / "changed.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    (repo / "backend" / "testing" / "test_changed.py").write_text(
        "def test_changed():\n    assert True\n", encoding="utf-8"
    )
    # The VM used for this test does not provide Ruff. This narrow shim lets
    # the test reach the PostgreSQL capability decision while delegating every
    # other python invocation to the real interpreter.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-m\" ] && [ \"${2:-}\" = \"ruff\" ]; then exit 0; fi\n"
        f"exec {sys.executable} \"$@\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
    fake_pytest = fake_bin / "pytest"
    fake_pytest.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_pytest.chmod(fake_pytest.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["WORKSPACE"] = str(repo)
    env.pop("TASK_ID", None)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["BLUEPRINT_SANDBOX_PYTHON"] = str(tmp_path / "missing-python")
    result = subprocess.run(
        ["bash", str(_CHECK), "test"],
        capture_output=True,
        text=True,
        env=env,
        # 20s, not 60: the CI lanes run pytest with --timeout=30, so a 60s
        # inner budget could never fire -- the harness would kill this call
        # first and report a healthy `check` run as an unreproducible flake.
        # Measured worst case across this file's callers is 1.57s (~13x head).
        timeout=20,
    )

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, (
        "required backend verification must fail when PostgreSQL is unavailable; "
        f"got exit {result.returncode} with output:\n{combined}"
    )
    assert "required backend verification state=runtime_unavailable" in combined
    assert "backend verification: runtime_unavailable" in combined
    assert "execution capability receipt:" in combined
    assert "unbound variable" not in combined
    assert "GATE-ENVIRONMENT: required backend verification skipped" not in combined
    assert "[check] PASS" not in combined


def test_merge_affected_backend_tests_run_through_the_attributed_lane(
    tmp_path: Path,
) -> None:
    """A conflict-style same-module test is attributed, not raw-exit blocked."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    scripts = repo / "scripts"
    backend = repo / "backend"
    (backend / "app").mkdir(parents=True)
    (backend / "testing").mkdir(parents=True)
    shutil.copy2(
        _REPO_ROOT / "backend" / "testing" / "impacted_tests.py",
        backend / "testing" / "impacted_tests.py",
    )
    shutil.copy2(
        _REPO_ROOT / "backend" / "testing" / "fixgate_attribution.py",
        backend / "testing" / "fixgate_attribution.py",
    )
    shutil.copy2(
        _REPO_ROOT / "scripts" / "backend_verification_guard.py",
        scripts / "backend_verification_guard.py",
    )
    (scripts / "backend-test-route.sh").write_text(
        "select_backend_test_route() { printf '%s\n' --postgres; }\n",
        encoding="utf-8",
    )
    (scripts / "emit_failure_junit.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "Path(sys.argv[1]).write_text('<testsuite><testcase classname=\"testing.test_merge_affected\" name=\"test_preexisting_failure\"><failure>inherited</failure></testcase></testsuite>', encoding='utf-8')",
                "raise SystemExit(1)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    harness = scripts / "test-backend.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "exec python3 scripts/emit_failure_junit.py \"$BLUEPRINT_VERIFICATION_JUNIT\"\n",
        encoding="utf-8",
    )
    harness.chmod(harness.stat().st_mode | stat.S_IXUSR)
    (backend / "app" / "changed.py").write_text("VALUE = 1\n", encoding="utf-8")
    # No import of app.changed: this must be selected by the retained
    # same-module convention, not by reverse-dependency inference.
    (backend / "testing" / "test_changed.py").write_text(
        "def test_preexisting_failure():\n    assert True\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "branch", "automain")
    (backend / "app" / "changed.py").write_text("VALUE = 2\n", encoding="utf-8")

    fragment = (
        _REPO_ROOT / "agent" / "src" / "prompts" / "check_backend_verification.sh"
    ).read_text(encoding="utf-8")
    for token, value in {
        "@DIR_RE@": "backend",
        "@PYTHON_PATH_RE@": r"^backend/.*\.py$",
        "@TEST_PATH_RE@": r"^backend/(testing|tests)/test_.*\.py$",
        "@SELECTOR_PREFIX_RE@": "backend/",
        "@DIR@": "backend",
        "@HARNESS@": "scripts/test-backend.sh",
    }.items():
        fragment = fragment.replace(token, value)
    gate = repo / "attributed-merge-gate.sh"
    gate.write_text(
        "#!/usr/bin/env bash\n"
        "set -uo pipefail\n"
        "FAILED=()\n"
        "BASE=automain\n"
        "_VERIFY_TASK_ID=merge-attribution\n"
        "_VERIFY_RECEIPT=\"$WORKSPACE/receipt.json\"\n"
        "_VERIFY_HARNESS_RECEIPT=\"$WORKSPACE/harness-receipt.json\"\n"
        "_VERIFY_CAPABILITY_RECEIPT=\"$WORKSPACE/capability-receipt.txt\"\n"
        "CHANGED='backend/app/changed.py'\n"
        + fragment
        + "\nif ! declare -p FAILED | grep -q '=()$'; then exit 1; fi\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"ruff\" ]; then exit 0; fi\n"
        f"exec {sys.executable} \"$@\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
    env = os.environ | {"WORKSPACE": str(repo), "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    result = subprocess.run(
        ["bash", str(gate)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "state=preexisting_test_failure" in result.stdout
    receipt = json.loads((repo / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["pytest"]["selectors"] == ["testing/test_changed.py"]
    assert receipt["pytest"]["suppressed_failures"]


def test_inherited_failure_that_stops_reproducing_does_not_block_a_green_diff(
    tmp_path: Path,
) -> None:
    """The real merge-test fragment publishes a repaired baseline failure.

    The production module changes from the failing base behavior to the green
    candidate behavior.  The exact generated backend verification fragment
    runs the candidate harness and the extracted-base harness, so the receipt
    proves both that the inherited identity survived differential attribution
    and that it did not prevent the green production diff from publishing.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    scripts = repo / "scripts"
    backend = repo / "backend"
    (backend / "app").mkdir(parents=True)
    (backend / "testing").mkdir(parents=True)
    for source, destination in (
        (_REPO_ROOT / "backend" / "testing" / "impacted_tests.py", backend / "testing" / "impacted_tests.py"),
        (_REPO_ROOT / "backend" / "testing" / "fixgate_attribution.py", backend / "testing" / "fixgate_attribution.py"),
        (_REPO_ROOT / "scripts" / "backend_verification_guard.py", scripts / "backend_verification_guard.py"),
    ):
        shutil.copy2(source, destination)
    (scripts / "backend-test-route.sh").write_text(
        "select_backend_test_route() { printf '%s\n' --postgres; }\n",
        encoding="utf-8",
    )
    (scripts / "emit_junit_from_production.py").write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "is_base_failure = 'VALUE = 1' in Path('backend/app/changed.py').read_text(encoding='utf-8')\n"
        "body = ('<failure>inherited</failure>' if is_base_failure else '')\n"
        "Path(sys.argv[1]).write_text(f'<testsuite><testcase classname=\"testing.test_changed\" name=\"test_repaired\">{body}</testcase></testsuite>', encoding='utf-8')\n"
        "raise SystemExit(1 if is_base_failure else 0)\n",
        encoding="utf-8",
    )
    harness = scripts / "test-backend.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "exec python3 scripts/emit_junit_from_production.py \"$BLUEPRINT_VERIFICATION_JUNIT\"\n",
        encoding="utf-8",
    )
    harness.chmod(harness.stat().st_mode | stat.S_IXUSR)
    (backend / "app" / "changed.py").write_text("VALUE = 1\n", encoding="utf-8")
    (backend / "testing" / "test_changed.py").write_text(
        "def test_repaired():\n    assert True\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base failure")
    _git(repo, "branch", "automain")
    # This is the non-test production diff the harness exercises above.
    (backend / "app" / "changed.py").write_text("VALUE = 2\n", encoding="utf-8")

    fragment = (
        _REPO_ROOT / "agent" / "src" / "prompts" / "check_backend_verification.sh"
    ).read_text(encoding="utf-8")
    for token, value in {
        "@DIR_RE@": "backend",
        "@PYTHON_PATH_RE@": r"^backend/.*\.py$",
        "@TEST_PATH_RE@": r"^backend/(testing|tests)/test_.*\.py$",
        "@SELECTOR_PREFIX_RE@": "backend/",
        "@DIR@": "backend",
        "@HARNESS@": "scripts/test-backend.sh",
    }.items():
        fragment = fragment.replace(token, value)
    gate = repo / "attributed-merge-gate.sh"
    gate.write_text(
        "#!/usr/bin/env bash\n"
        "set -uo pipefail\n"
        "FAILED=()\n"
        "BASE=automain\n"
        "_VERIFY_TASK_ID=resolved-baseline-attribution\n"
        "_VERIFY_RECEIPT=\"$WORKSPACE/receipt.json\"\n"
        "_VERIFY_HARNESS_RECEIPT=\"$WORKSPACE/harness-receipt.json\"\n"
        "_VERIFY_CAPABILITY_RECEIPT=\"$WORKSPACE/capability-receipt.txt\"\n"
        "CHANGED='backend/app/changed.py'\n"
        + fragment
        + "\nif ! declare -p FAILED | grep -q '=()$'; then exit 1; fi\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"ruff\" ]; then exit 0; fi\n"
        f"exec {sys.executable} \"$@\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
    result = subprocess.run(
        ["bash", str(gate)],
        cwd=repo,
        env=os.environ | {"WORKSPACE": str(repo), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((repo / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["pytest"]["state"] == "baseline_failure_disappeared"
    assert receipt["pytest"]["new_failures"] == {}
    assert receipt["pytest"]["disappeared_failures"] == {
        "backend/testing/test_changed.py::test_repaired::failure": {
            "detail": "inherited",
            "reason": "baseline failure no longer reproduces in current report",
        }
    }


def test_off_image_pgserver_route_wins_without_baked_paths(tmp_path: Path) -> None:
    """An importable pgserver package is a real route, not a Docker fallback."""
    pg_bin = tmp_path / "pg-bin"
    pg_bin.mkdir()
    for name in ("initdb", "pg_ctl"):
        binary = pg_bin / name
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    pgserver = tmp_path / "pgserver"
    pgserver.mkdir()
    (pgserver / "__init__.py").write_text("", encoding="utf-8")
    (pgserver / "_commands.py").write_text(
        f"POSTGRES_BIN_PATH = {str(pg_bin)!r}\n", encoding="utf-8"
    )
    (tmp_path / "pytest.py").write_text("", encoding="utf-8")

    env = os.environ.copy()
    # Use the running interpreter as the sandbox Python: it is guaranteed to
    # exist on every host (the CI backend-test-deps image installs it at
    # /usr/local/bin/python, not /usr/bin/python3), and the fake ``pgserver``
    # package on PYTHONPATH shadows any real wheel, so the route still resolves
    # the fabricated bindir. A hardcoded /usr/bin/python3 silently fails closed
    # on any image without a system python3 there.
    env["BLUEPRINT_SANDBOX_PYTHON"] = sys.executable
    env["PYTHONPATH"] = str(tmp_path)
    env["PATH"] = f"{tmp_path}:/usr/bin:/bin"
    result = subprocess.run(
        ["bash", "-c", f"source {_ROUTE_HELPER}; select_backend_test_route"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "--sandbox-postgres"


def test_backend_route_stays_fail_closed_without_a_runtime(tmp_path: Path) -> None:
    """A missing sandbox and Docker daemon cannot silently select SQLite."""
    fake_pg_config = tmp_path / "pg_config"
    fake_pg_config.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_pg_config.chmod(fake_pg_config.stat().st_mode | stat.S_IXUSR)
    env = os.environ.copy()
    env["BLUEPRINT_SANDBOX_PYTHON"] = "/usr/bin/python3"
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = str(tmp_path / "empty-python-path")
    env["PATH"] = str(tmp_path)
    result = subprocess.run(
        ["/bin/bash", "-c", f"source {_ROUTE_HELPER}; select_backend_test_route"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""


def _failure_report(key: str, detail: str = "boom", *, skipped: bool = False) -> JUnitReport:
    test_key = key.rsplit("::", 1)[0]
    return JUnitReport(
        failures={} if skipped else {key: detail},
        test_routes={test_key},
        skipped_routes={test_key} if skipped else set(),
    )


def test_normalize_strips_pytest_asyncio_task_counter() -> None:
    """A pytest-asyncio internal Task-<n> counter is process-wide and
    monotonically increasing, so the identical failure names a different
    counter value on every run (baseline vs current or rerun vs rerun) even
    though the test's own behavior never changed. It must not defeat
    attribution."""
    detail = "future = <Task finished name='Task-4898' coro=<foo() done>>"
    other = "future = <Task finished name='Task-7290' coro=<foo() done>>"
    assert _normalize_failure_detail(detail, tree_root=None) == _normalize_failure_detail(
        other, tree_root=None
    )


def test_normalize_strips_truncated_coroutine_repr_checkout_path() -> None:
    """A coroutine/Task repr truncates its embedded source path with a
    leading '...' once the qualname exceeds Python's repr budget, so the
    full-path checkout-root replace never matches it. The truncated prefix
    still differs between the current checkout and a freshly extracted
    baseline temp dir purely by directory name, not by test behavior."""
    current = "coro=<f() done...space/agent/backend/testing/test_x.py:16451>"
    baseline = "coro=<f() done...main-check2/backend/testing/test_x.py:16451>"
    assert _normalize_failure_detail(current, tree_root=None) == _normalize_failure_detail(
        baseline, tree_root=None
    )


def test_normalize_preserves_a_genuinely_different_line_number() -> None:
    """The truncated-path normalization must only erase the checkout-name
    noise before '/backend/', not the file:line suffix that would signal a
    real code change."""
    current = "coro=<f() done...space/agent/backend/testing/test_x.py:100>"
    baseline = "coro=<f() done...main-check2/backend/testing/test_x.py:200>"
    assert _normalize_failure_detail(current, tree_root=None) != _normalize_failure_detail(
        baseline, tree_root=None
    )


def test_attribute_reports_suppress_task_counter_only_difference() -> None:
    """End-to-end: a failure identical except for the pytest-asyncio Task
    counter and the truncated checkout path must be classified as
    pre-existing (suppressed), not a new failure introduced by this diff."""
    key = "backend/testing/test_strategic_spawning.py::test_x::failure"
    current = {
        key: "future = <Task finished name='Task-4898' "
        "coro=<x() done...space/agent/backend/testing/test_x.py:16451>>"
    }
    baseline = {
        key: "future = <Task finished name='Task-7290' "
        "coro=<x() done...main-check2/backend/testing/test_x.py:16451>>"
    }
    attribution = attribute_reports(
        _failure_report(
            key, _normalize_failure_detail(current[key], tree_root=None)
        ),
        _failure_report(
            key, _normalize_failure_detail(baseline[key], tree_root=None)
        ),
    )

    assert attribution.new_failures == {}
    assert key in attribution.suppressed_failures


def test_duplicate_junit_failures_remain_independently_attributable(
    tmp_path: Path,
) -> None:
    """Repeated nodes keep their occurrence and compare normalized details."""

    baseline_junit = tmp_path / "baseline.xml"
    baseline_junit.write_text(
        "<testsuite>"
        '<testcase classname="testing.test_dupe" name="test_same">'
        "<failure>Task-17 uuid=3fa85f64-5717-4562-b3fc-2c963f66afa6: inherited</failure>"
        "</testcase>"
        '<testcase classname="testing.test_dupe" name="test_same">'
        "<failure>AssertionError: old detail</failure>"
        "</testcase>"
        "</testsuite>",
        encoding="utf-8",
    )
    current_junit = tmp_path / "current.xml"
    current_junit.write_text(
        "<testsuite>"
        '<testcase classname="testing.test_dupe" name="test_same">'
        "<failure>Task-902 uuid=9c858901-8a57-4791-81fe-4c455b099bc9: inherited</failure>"
        "</testcase>"
        '<testcase classname="testing.test_dupe" name="test_same">'
        "<failure>AssertionError: changed detail</failure>"
        "</testcase>"
        "</testsuite>",
        encoding="utf-8",
    )

    baseline = parse_junit_report(baseline_junit)
    current = parse_junit_report(current_junit)
    first = "testing.test_dupe::test_same::failure"
    second = f"{first}::occurrence=2"

    assert set(baseline.failures) == {first, second}
    assert set(current.failures) == {first, second}
    attribution = classify_verification(
        runtime_available=True,
        current_exit_code=1,
        current_report=current,
        baseline_report=baseline,
    )
    assert attribution["blocking"] is True
    assert list(attribution["suppressed_failures"]) == [first]
    assert attribution["new_failures"] == {second: "AssertionError: changed detail"}


def test_structured_comparator_normalizes_noise_and_preserves_offender_semantics() -> None:
    result = compare_structured(
        {
            "baseline": [
                {
                    "route": "vitest/example.test.ts",
                    "testcase": "fails",
                    "kind": "failure",
                    "detail": "Task-4 uuid=3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "offenders": ["src/example.test.ts"],
                }
            ],
            "current": [
                {
                    "route": "vitest/example.test.ts",
                    "testcase": "fails",
                    "kind": "failure",
                    "detail": "Task-902 uuid=9c858901-8a57-4791-81fe-4c455b099bc9",
                    "offenders": ["src/example.test.ts"],
                }
            ],
        }
    )

    key = "vitest/example.test.ts::fails::failure"
    assert result["blocking"] is False
    assert result["new_failures"] == {}
    assert result["suppressed_failures"][key]["detail"] == "Task-<n> uuid=<uuid>"


def test_structured_comparator_rejects_unbounded_occurrence_arrays() -> None:
    with pytest.raises(ValueError, match="4096-record limit"):
        compare_structured(
            {
                "current": [
                    {
                        "route": "route",
                        "testcase": "test",
                        "kind": "failure",
                        "detail": "detail",
                    }
                ]
                * 4097,
                "baseline": [],
            }
        )


def test_structured_comparator_cli_returns_machine_readable_verdict() -> None:
    payload = {
        "baseline": [
            {"route": "r", "testcase": "t", "kind": "failure", "detail": "Task-1"}
        ],
        "current": [
            {"route": "r", "testcase": "t", "kind": "failure", "detail": "Task-2"}
        ],
    }
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "backend/testing/fixgate_attribution.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    verdict = json.loads(result.stdout)
    assert verdict["blocking"] is False
    assert verdict["new_failures"] == {}
    assert verdict["suppressed_failures"]["r::t::failure"]["detail"] == "Task-<n>"


def test_repaired_baseline_failure_is_distinguishable_from_a_deleted_route() -> None:
    """Only lossless reports can distinguish repair, deletion, and skip states."""
    key = "backend/testing/test_old.py::test_existing::failure"
    route = "backend/testing/test_old.py::test_existing"
    baseline = _failure_report(key, "inherited")
    repaired = JUnitReport(test_routes={route})
    deleted = JUnitReport(test_routes={"backend/testing/test_other.py::test_ok"})
    skipped = JUnitReport(test_routes={route}, skipped_routes={route})

    repaired_outcome = classify_verification(
        runtime_available=True,
        current_exit_code=0,
        current_report=repaired,
        baseline_report=baseline,
    )
    deleted_outcome = classify_verification(
        runtime_available=True,
        current_exit_code=0,
        current_report=deleted,
        baseline_report=baseline,
    )
    skipped_outcome = classify_verification(
        runtime_available=True,
        current_exit_code=0,
        current_report=skipped,
        baseline_report=baseline,
    )

    assert repaired_outcome["state"] == "baseline_failure_disappeared"
    assert repaired_outcome["disappeared_failures"][key]["reason"] == (
        "baseline failure no longer reproduces in current report"
    )
    assert route_coverage_gaps(repaired, baseline) == {}
    assert deleted_outcome["state"] == "baseline_failure_disappeared"
    assert deleted_outcome["disappeared_failures"][key]["reason"] == (
        "baseline failing route absent from current report"
    )
    assert route_coverage_gaps(deleted, baseline) == {
        route: "baseline test route is absent from the current report"
    }
    assert skipped_outcome["state"] == "baseline_failure_skipped"
    assert route_coverage_gaps(skipped, baseline) == {
        route: "baseline test route is skipped in the current report"
    }


def test_attributed_gate_blocks_a_failure_introduced_by_the_diff() -> None:
    key = "backend/testing/test_changed.py::test_new::failure"
    outcome = classify_verification(
        runtime_available=True,
        current_exit_code=1,
        current_report=_failure_report(key, "introduced"),
        baseline_report=JUnitReport(test_routes={"backend/testing/test_changed.py::test_new"}),
    )

    assert outcome["state"] == "test_failure"
    assert outcome["blocking"] is True
    assert outcome["new_failures"] == {key: "introduced"}


def test_attributed_gate_blocks_when_a_baseline_failure_disappears() -> None:
    """Keep the route stable: the targeted gate enforces its coverage separately."""
    key = "backend/testing/test_old.py::test_existing::failure"
    outcome = classify_verification(
        runtime_available=True,
        current_exit_code=0,
        current_report=JUnitReport(test_routes={"backend/testing/test_other.py::test_ok"}),
        baseline_report=_failure_report(key),
    )

    assert outcome["state"] == "baseline_failure_disappeared"
    assert outcome["blocking"] is False
    assert key in outcome["disappeared_failures"]


def test_attributed_gate_blocks_when_a_baseline_failure_becomes_skipped() -> None:
    """Keep the route stable: the targeted gate enforces its coverage separately."""
    key = "backend/testing/test_old.py::test_existing::failure"
    current = _failure_report(key, skipped=True)
    outcome = classify_verification(
        runtime_available=True,
        current_exit_code=0,
        current_report=current,
        baseline_report=_failure_report(key),
    )

    assert outcome["state"] == "baseline_failure_skipped"
    assert outcome["blocking"] is False
    assert key in outcome["skipped_failures"]


def test_attributed_gate_names_unavailable_baseline_and_blocks(tmp_path: Path) -> None:
    """The retained shell seam receives a named fail-closed attribution state."""
    junit = tmp_path / "current.xml"
    junit.write_text(
        '<testsuite><testcase classname="testing.test_x" name="test_x" /></testsuite>',
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "scripts" / "backend_verification_guard.py"),
            "--base",
            "missing-base",
            "--repo-root",
            str(tmp_path),
            "--current-junit",
            str(junit),
            "--current-exit-code",
            "0",
            "--route=--sandbox-postgres",
            "--harness",
            "scripts/test-backend.sh",
            "testing/test_x.py",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0  # the shell consumes the JSON verdict
    verdict = json.loads(result.stdout)
    assert verdict["state"] == "baseline_unavailable"
    assert verdict["blocking"] is True
    assert "cannot resolve base ref" in verdict["error"]


def test_rendered_gate_carries_all_matched_sentinels_past_ordinary_cap(tmp_path: Path) -> None:
    """The rendered gate sends eight ordinary tests plus every sentinel to harness."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    scripts = repo / "scripts"
    backend = repo / "backend"
    (backend / "app").mkdir(parents=True)
    (backend / "testing").mkdir(parents=True)
    shutil.copy2(
        _REPO_ROOT / "backend" / "testing" / "impacted_tests.py",
        backend / "testing" / "impacted_tests.py",
    )
    shutil.copy2(
        _REPO_ROOT / "backend" / "testing" / "fixgate_attribution.py",
        backend / "testing" / "fixgate_attribution.py",
    )
    shutil.copy2(
        _REPO_ROOT / "backend" / "testing" / "drift_sentinel_surfaces.json",
        backend / "testing" / "drift_sentinel_surfaces.json",
    )
    shutil.copy2(
        _REPO_ROOT / "scripts" / "backend_verification_guard.py",
        scripts / "backend_verification_guard.py",
    )
    (scripts / "backend-test-route.sh").write_text(
        "select_backend_test_route() { printf '%s\n' --postgres; }\n",
        encoding="utf-8",
    )
    (scripts / "record_selectors.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "selectors = sys.argv[2:]\n"
        "Path(os.environ['SELECTOR_LOG']).write_text(json.dumps(selectors), encoding='utf-8')\n"
        "cases = ''.join(f'<testcase classname=\"testing.synthetic\" name=\"{s}\" />' for s in selectors)\n"
        "Path(os.environ['BLUEPRINT_VERIFICATION_JUNIT']).write_text(f'<testsuite tests=\"{len(selectors)}\">{cases}</testsuite>', encoding='utf-8')\n",
        encoding="utf-8",
    )
    harness = scripts / "test-backend.sh"
    harness.write_text(
        "#!/usr/bin/env bash\nexec python3 scripts/record_selectors.py \"$@\"\n",
        encoding="utf-8",
    )
    harness.chmod(harness.stat().st_mode | stat.S_IXUSR)
    sentinel_paths = [
        "backend/testing/test_event_sentinel.py",
        "backend/testing/test_tree_sentinel.py",
    ]
    for path in sentinel_paths:
        (repo / path).write_text("def test_inventory(): pass\n", encoding="utf-8")
    for index in range(12):
        (backend / "testing" / f"test_fanout_{index:02d}.py").write_text(
            "from app.changed import VALUE\n", encoding="utf-8"
        )
    (backend / "app" / "changed.py").write_text("VALUE = 1\n", encoding="utf-8")
    (backend / "testing" / "drift_sentinel_surfaces.json").write_text(
        json.dumps(
            {
                "surfaces": [
                    {
                        "glob": "backend/app/**/*.py",
                        "sentinels": sentinel_paths,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "branch", "automain")
    (backend / "app" / "changed.py").write_text("VALUE = 2\n", encoding="utf-8")

    fragment = (
        _REPO_ROOT / "agent" / "src" / "prompts" / "check_backend_verification.sh"
    ).read_text(encoding="utf-8")
    for token, value in {
        "@DIR_RE@": "backend",
        "@PYTHON_PATH_RE@": r"^backend/.*\.py$",
        "@TEST_PATH_RE@": r"^backend/(testing|tests)/test_.*\.py$",
        "@SELECTOR_PREFIX_RE@": "backend/",
        "@DIR@": "backend",
        "@HARNESS@": "scripts/test-backend.sh",
    }.items():
        fragment = fragment.replace(token, value)
    gate = repo / "attributed-sentinel-gate.sh"
    gate.write_text(
        "#!/usr/bin/env bash\n"
        "set -uo pipefail\n"
        "FAILED=()\n"
        "BASE=automain\n"
        "_VERIFY_TASK_ID=sentinel-retention\n"
        "_VERIFY_RECEIPT=\"$WORKSPACE/receipt.json\"\n"
        "_VERIFY_HARNESS_RECEIPT=\"$WORKSPACE/harness-receipt.json\"\n"
        "_VERIFY_CAPABILITY_RECEIPT=\"$WORKSPACE/capability-receipt.txt\"\n"
        "CHANGED='backend/app/changed.py'\n"
        + fragment
        + "\nif ! declare -p FAILED | grep -q '=()$'; then exit 1; fi\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"ruff\" ]; then exit 0; fi\n"
        f"exec {sys.executable} \"$@\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
    selector_log = tmp_path / "selectors.json"
    env = os.environ | {
        "WORKSPACE": str(repo),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SELECTOR_LOG": str(selector_log),
    }
    result = subprocess.run(
        ["bash", str(gate)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    selectors = json.loads(selector_log.read_text(encoding="utf-8"))
    assert [selector for selector in selectors if "fanout" in selector] == [
        f"testing/test_fanout_{i:02d}.py" for i in range(8)
    ]
    assert [selector for selector in selectors if "sentinel" in selector] == [
        "testing/test_event_sentinel.py",
        "testing/test_tree_sentinel.py",
    ]
    assert len(selectors) == len(set(selectors)) == 10


def test_uuid_bearing_baseline_failure_is_attributed_preexisting() -> None:
    """A run-generated UUID in a traceback must not defeat baseline suppression.

    backend/app/task_completion.py emits a diagnostic that embeds the current
    task's UUID (e.g. ``f"... for task {task_id_str}: {chain_exc}"``). The UUID
    is generated fresh on every run, so an identical pre-existing failure can
    never byte-match across a baseline/current comparison unless the UUID
    token itself is normalized away, exactly like the existing Task-<n> and
    truncated-checkout-path normalizations above.
    """
    key = (
        "backend/testing/test_strategic_spawning.py"
        "::test_architecture_cycle_operator_wake_reaches_phase2_after_marker_cleared"
        "::error"
    )
    current_detail = _normalize_failure_detail(
        "[DESIGN DOC] Failed to chain Phase 2 for task "
        "3fa85f64-5717-4562-b3fc-2c963f66afa6: PendingRollbackError",
        tree_root=None,
    )
    baseline_detail = _normalize_failure_detail(
        "[DESIGN DOC] Failed to chain Phase 2 for task "
        "9c858901-8a57-4791-81fe-4c455b099bc9: PendingRollbackError",
        tree_root=None,
    )
    outcome = classify_verification(
        runtime_available=True,
        current_exit_code=1,
        current_report=_failure_report(key, current_detail),
        baseline_report=_failure_report(key, baseline_detail),
    )

    assert outcome["state"] == "preexisting_test_failure"
    assert outcome["blocking"] is False
    assert outcome["new_failures"] == {}


def test_normalize_uuid_does_not_erase_a_genuinely_different_failure_reason() -> None:
    """UUID normalization must be scoped to the UUID token only.

    A failure that differs in its non-UUID content (a different exception
    type) is a genuinely different failure and must still be reported as new,
    even though both details also happen to embed a run-unique UUID.
    """
    current = _normalize_failure_detail(
        "[DESIGN DOC] Failed to chain Phase 2 for task "
        "3fa85f64-5717-4562-b3fc-2c963f66afa6: PendingRollbackError",
        tree_root=None,
    )
    baseline = _normalize_failure_detail(
        "[DESIGN DOC] Failed to chain Phase 2 for task "
        "9c858901-8a57-4791-81fe-4c455b099bc9: KeyError",
        tree_root=None,
    )
    assert current != baseline


# --- Current-side attributability -------------------------------------------
#
# ``is_attributable_pytest_result`` gates ``classify_verification`` before any
# baseline comparison happens. If pytest's exit code and its JUnit report
# disagree, the report is not trustworthy evidence about the current tree, so
# no identity in it may be attributed away against a baseline — not even one
# that matches the baseline exactly. These pin the CURRENT side of that
# property; ``test_guard_gate_left_shift.py`` covers the baseline side.


def test_zero_exit_with_reported_failures_is_execution_failure() -> None:
    """pytest reporting failures while exiting 0 is a broken run, not a pass."""
    key = "backend/testing/test_x.py::test_fails::failure"
    outcome = classify_verification(
        runtime_available=True,
        current_exit_code=0,
        current_report=_failure_report(key),
        baseline_report=JUnitReport(),
    )

    assert outcome == {
        "state": "execution_failure",
        "blocking": True,
        "new_failures": {},
    }


@pytest.mark.parametrize("exit_code", [2, 3, 4, 5, 137, -15])
def test_abnormal_current_exit_cannot_suppress_a_matching_failure(
    exit_code: int,
) -> None:
    """An abnormal exit is never attributed away, even against an identical
    baseline failure. Collection errors, OOM kills (137) and SIGTERM (-15) all
    truncate the run, so the report cannot prove the identity is inherited."""
    key = "backend/testing/test_x.py::test_fails::failure"
    report = _failure_report(key)

    outcome = classify_verification(
        runtime_available=True,
        current_exit_code=exit_code,
        current_report=report,
        baseline_report=report,
    )

    assert outcome == {
        "state": "execution_failure",
        "blocking": True,
        "new_failures": {},
    }


@pytest.mark.parametrize("exit_code", [1, 2, 3, 4, 5, 137, -15])
def test_nonzero_exit_with_a_clean_report_is_execution_failure(
    exit_code: int,
) -> None:
    """A clean JUnit report cannot explain a non-zero exit; fail closed."""
    report = JUnitReport(test_routes={"backend/testing/test_x.py::test_passes"})

    outcome = classify_verification(
        runtime_available=True,
        current_exit_code=exit_code,
        current_report=report,
        baseline_report=report,
    )

    assert outcome == {
        "state": "execution_failure",
        "blocking": True,
        "new_failures": {},
    }


# --- Offender-set identity ---------------------------------------------------
#
# Aggregate guard failures ("N offenders: ...") render prose whose counts and
# ordering change while the underlying inherited violation does not. The
# BLUEPRINT_OFFENDERS marker gives those failures a stable set identity, and
# ``attribute_reports`` suppresses one only when the current offender set is a
# SUBSET of the baseline's. That subset rule is what decides whether a failing
# identity may publish, so it is pinned in both directions below.


def _offender_report(key: str, prose: str, offenders: list[str]) -> JUnitReport:
    detail = f"{prose}\n{format_offender_marker(offenders)}"
    test_key = key.rsplit("::", 1)[0]
    return JUnitReport(failures={key: detail}, test_routes={test_key})


def test_offender_set_subset_is_attributed_to_baseline() -> None:
    """Changing diagnostics or removing an old offender stays pre-existing."""
    key = "backend/testing/test_invariants.py::test_backend::failure"
    current = _offender_report(key, "current counts", ["app/a.py"])
    baseline = _offender_report(key, "baseline counts", ["app/a.py", "app/old.py"])

    outcome = classify_verification(
        runtime_available=True,
        current_exit_code=1,
        current_report=current,
        baseline_report=baseline,
    )

    assert outcome["state"] == "preexisting_test_failure"
    assert outcome["blocking"] is False
    assert outcome["new_failures"] == {}
    assert outcome["suppressed_failures"][key]["detail"] == current.failures[key]


def test_offender_set_new_path_remains_blocking() -> None:
    """One added offender blocks even though the rest of the set is inherited."""
    key = "backend/testing/test_invariants.py::test_backend::failure"
    current = _offender_report(key, "current", ["app/a.py", "app/new.py"])
    baseline = _offender_report(key, "baseline", ["app/a.py"])

    outcome = classify_verification(
        runtime_available=True,
        current_exit_code=1,
        current_report=current,
        baseline_report=baseline,
    )

    assert outcome["state"] == "test_failure"
    assert outcome["blocking"] is True
    assert outcome["new_failures"] == {key: current.failures[key]}


def test_offender_marker_includes_snapshot_entry_identity() -> None:
    """A second bad entry with the same field must not be suppressed as inherited."""
    key = (
        "backend/testing/test_event_type_invariants.py"
        "::test_snapshot_has_required_fields::failure"
    )
    current = _offender_report(
        key,
        "Event type 'new_event' missing fields: {'audience'}",
        ["new_event:audience"],
    )
    baseline = _offender_report(
        key,
        "Event type 'old_event' missing fields: {'audience'}",
        ["old_event:audience"],
    )

    outcome = classify_verification(
        runtime_available=True,
        current_exit_code=1,
        current_report=current,
        baseline_report=baseline,
    )

    assert outcome["state"] == "test_failure"
    assert outcome["blocking"] is True
    assert outcome["new_failures"] == {key: current.failures[key]}


def _write_junit(tmp_path: Path, failure_body: str) -> Path:
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuite><testcase classname="testing.test_x" name="test_old">'
        f"<failure>{failure_body}</failure>"
        "</testcase></testsuite>",
        encoding="utf-8",
    )
    return report


def test_junit_parser_extracts_offender_set_marker(tmp_path: Path) -> None:
    """The marker must survive the XML round trip into ``offender_sets``."""
    report = _write_junit(
        tmp_path, 'counts\nBLUEPRINT_OFFENDERS: ["app/a.py", "app/b.py"]'
    )

    parsed = parse_junit_report(report)

    assert parsed.offender_sets == {
        "testing.test_x::test_old::failure": frozenset({"app/a.py", "app/b.py"})
    }


def test_offender_marker_formatter_is_deterministic_and_parseable(
    tmp_path: Path,
) -> None:
    """Producers must emit a sorted, de-duplicated marker the parser reads back.

    The literal pin matters: the marker is the wire format between guard
    producers and the differential gate, so a change in separators, ordering,
    or dedup silently re-partitions every inherited failure identity.
    """
    marker = format_offender_marker(["app/z.py", "app/a.py", "app/z.py"])

    assert marker == 'BLUEPRINT_OFFENDERS: ["app/a.py","app/z.py"]'

    parsed = parse_junit_report(_write_junit(tmp_path, f"human diagnostic\n{marker}"))

    assert parsed.offender_sets == {
        "testing.test_x::test_old::failure": frozenset({"app/a.py", "app/z.py"})
    }


def test_offender_marker_formatter_rejects_ambiguous_identities() -> None:
    """A newline inside an identity would forge extra marker lines."""
    with pytest.raises(ValueError, match="single-line"):
        format_offender_marker(["app/a.py\napp/b.py"])

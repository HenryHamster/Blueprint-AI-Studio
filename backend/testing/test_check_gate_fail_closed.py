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
    attribute_failure_maps,
    classify_verification,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if not (_REPO_ROOT / "check").exists() and Path("/repo/check").exists():
    _REPO_ROOT = Path("/repo")
_CHECK = _REPO_ROOT / "check"
_DEFAULT_CHECK = _REPO_ROOT / "scripts" / "check-default.sh"
_ROUTE_HELPER = _REPO_ROOT / "scripts" / "backend-test-route.sh"


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
    (repo / "file.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "file.txt", "scripts/check-default.sh")
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
        timeout=60,
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
        timeout=60,
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


def test_attribute_failure_maps_suppresses_task_counter_only_difference() -> None:
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
    attribution = attribute_failure_maps(current, baseline)

    assert attribution.new_failures == {}
    assert key in attribution.suppressed_failures


def test_attributed_gate_blocks_a_failure_introduced_by_the_diff() -> None:
    key = "backend/testing/test_changed.py::test_new::failure"
    outcome = classify_verification(
        runtime_available=True,
        current_exit_code=1,
        current_failures={key: "introduced"},
        current_report=_failure_report(key, "introduced"),
        baseline_report=JUnitReport(test_routes={"backend/testing/test_changed.py::test_new"}),
    )

    assert outcome["state"] == "test_failure"
    assert outcome["blocking"] is True
    assert outcome["new_failures"] == {key: "introduced"}


def test_attributed_gate_blocks_when_a_baseline_failure_disappears() -> None:
    key = "backend/testing/test_old.py::test_existing::failure"
    outcome = classify_verification(
        runtime_available=True,
        current_exit_code=0,
        current_failures={},
        current_report=JUnitReport(test_routes={"backend/testing/test_other.py::test_ok"}),
        baseline_report=_failure_report(key),
    )

    assert outcome["state"] == "baseline_failure_disappeared"
    assert outcome["blocking"] is True
    assert key in outcome["disappeared_failures"]


def test_attributed_gate_blocks_when_a_baseline_failure_becomes_skipped() -> None:
    key = "backend/testing/test_old.py::test_existing::failure"
    current = _failure_report(key, skipped=True)
    outcome = classify_verification(
        runtime_available=True,
        current_exit_code=0,
        current_failures={},
        current_report=current,
        baseline_report=_failure_report(key),
    )

    assert outcome["state"] == "baseline_failure_skipped"
    assert outcome["blocking"] is True
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
        current_failures={key: current_detail},
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

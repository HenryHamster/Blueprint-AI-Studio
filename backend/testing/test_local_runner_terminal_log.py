"""Terminal-evidence invariant for the local runner's compute-job loop.

The runner's deadline/cancel/shutdown kill paths previously reported
``exec_status="failed"`` with NO ``command_log`` — silently discarding the
captured output of exactly the most expensive failures (a 24h run killed at
its runtime deadline). This AST check pins the invariant on
``scripts/local_runner.py`` without importing it (the script requires its
sibling runner modules): every terminal ``_report_compute_job`` call inside
``_run_compute_job`` must carry a ``command_log`` argument.

Standalone — no database. The script is resolved across local checkouts and
the Docker test harness (repo mounted at ``/repo``), mirroring
``_resolve_prompt_ts_path`` in test_invariants.py.
"""

import ast
import os

import pytest


def _resolve_local_runner_path() -> "str | None":
    """Locate ``scripts/local_runner.py`` across local + Docker test environments.

    Candidates, in order: explicit env override, the ``/repo`` bind-mount
    (``scripts/test-backend.sh`` mounts the repo root read-only at ``/repo``
    in its Docker harness), then the repo root two levels above this file
    (plain checkouts and non-Docker runs).
    """
    candidates = []
    env_override = os.getenv("BLUEPRINT_LOCAL_RUNNER_PY")
    if env_override:
        candidates.append(env_override)
    candidates.append("/repo/scripts/local_runner.py")
    candidates.append(
        os.path.normpath(
            os.path.join(
                os.path.dirname(__file__), "..", "..", "scripts", "local_runner.py"
            )
        )
    )
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _require_local_runner_path() -> str:
    """Resolve the script or skip — except in CI, where a miss FAILS loudly.

    A silent skip in the promotion CI means the terminal-evidence invariant
    never enforces; only ad-hoc environments without the checkout may skip.
    """
    path = _resolve_local_runner_path()
    if path is not None:
        return path
    msg = (
        "scripts/local_runner.py not available in this test environment "
        "(checked BLUEPRINT_LOCAL_RUNNER_PY, the /repo bind-mount that "
        "scripts/test-backend.sh provides, and ../../scripts relative to "
        "this file)"
    )
    if os.getenv("CI"):
        pytest.fail(
            msg + " — refusing to skip in CI: mount the repo root at /repo "
            "or set BLUEPRINT_LOCAL_RUNNER_PY so this invariant enforces"
        )
    pytest.skip(msg)


def _find_function(tree: ast.Module, name: str) -> "ast.FunctionDef | None":
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _keyword(call: ast.Call, name: str) -> "ast.keyword | None":
    for kw in call.keywords:
        if kw.arg == name:
            return kw
    return None


def test_all_terminal_compute_reports_carry_command_log():
    """Every ``_report_compute_job(..., exec_status="failed"/"succeeded", ...)``
    call in ``_run_compute_job`` must pass ``command_log`` — no terminal path may
    drop the captured evidence."""
    path = _require_local_runner_path()
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())

    run_job = _find_function(tree, "_run_compute_job")
    assert run_job is not None, "_run_compute_job not found in local_runner.py"

    report_terminal = _find_function(run_job, "_report_terminal")
    assert report_terminal is not None, "_report_terminal helper not found"

    helper_reports = [
        node
        for node in ast.walk(report_terminal)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_report_compute_job"
    ]
    assert len(helper_reports) == 1, (
        "_report_terminal must have one authoritative _report_compute_job call"
    )
    assert _keyword(helper_reports[0], "command_log") is not None, (
        "_report_terminal does not forward command_log to _report_compute_job"
    )

    terminal_calls = []
    violations = []
    for node in ast.walk(run_job):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_report_terminal"
        ):
            continue
        terminal_calls.append(node)
        if _keyword(node, "command_log") is None:
            violations.append(
                f"line {node.lineno}: _report_terminal has no command_log"
            )

    # Sanity floor so a refactor that renames the reporter cannot make this
    # test vacuously pass: decline, cancel-at-claim, clone-fail, login-fail,
    # probe-abort, start-failure, deadline, shutdown, cancel, the shared final
    # report, and the crash handler.
    assert len(terminal_calls) >= 8, (
        f"expected >=8 _report_terminal calls in _run_compute_job, "
        f"found {len(terminal_calls)} — reporter renamed/moved? update this test"
    )
    assert not violations, (
        "terminal compute-job reports dropping the captured command log "
        "(evidence of exactly the most expensive failures):\n"
        + "\n".join(violations)
    )

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

from testing.repo_scripts_path import resolve_scripts_dir_from_env

_BACKEND_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_LOCAL_RUNNER_ENV = "BLUEPRINT_LOCAL_RUNNER_PY"


def _resolve_local_runner_path() -> "tuple[str | None, str]":
    """Locate ``scripts/local_runner.py``, or return the NAMED reason it is absent.

    The file-level ``BLUEPRINT_LOCAL_RUNNER_PY`` override is honoured first and
    still points straight at a script. Everything below it is delegated to the
    shared ``resolve_scripts_dir_from_env`` (backend/testing/repo_scripts_path.py)
    so the candidate order, the ``/repo`` bind-mount constant and the sentinel
    check live in ONE place instead of being re-typed per module. The old copy
    here probed the same three locations by hand, which is correct only for as
    long as nobody edits one copy and not the others.

    Returns ``(path, "")`` on a hit and ``(None, reason)`` on a miss; the reason
    is the resolver's own, naming every candidate it probed.
    """
    env_override = os.getenv(_LOCAL_RUNNER_ENV)
    if env_override:
        if os.path.isfile(env_override):
            return os.path.normpath(env_override), ""
        return None, (
            f"{_LOCAL_RUNNER_ENV}={env_override!r} does not name a file"
        )
    scripts_dir, unavailable = resolve_scripts_dir_from_env(_BACKEND_DIR)
    if scripts_dir is None:
        return None, unavailable
    candidate = os.path.join(scripts_dir, "local_runner.py")
    if os.path.isfile(candidate):
        return candidate, ""
    # The scripts tree resolved (it carries the shared sentinel) and the script
    # this guard reads is not in it. That is a BROKEN checkout, not an absent
    # one, and it must not read as "nothing to check" (#17872).
    return None, (
        f"{scripts_dir} is the repo scripts/ tree (it carries the resolver "
        f"sentinel) but local_runner.py is not in it — the scripts tree is "
        f"present and broken, not missing"
    )


def _require_local_runner_path() -> str:
    """Resolve the script or skip — except in CI, where a miss FAILS loudly.

    A silent skip in the promotion CI means the terminal-evidence invariant
    never enforces; only ad-hoc environments without the checkout may skip.
    """
    path, unavailable = _resolve_local_runner_path()
    if path is not None:
        return path
    msg = f"scripts/local_runner.py not available in this test environment: {unavailable}"
    if os.getenv("CI"):
        pytest.fail(
            msg + " — refusing to skip in CI: mount the repo root at /repo "
            f"or set {_LOCAL_RUNNER_ENV} so this invariant enforces"
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

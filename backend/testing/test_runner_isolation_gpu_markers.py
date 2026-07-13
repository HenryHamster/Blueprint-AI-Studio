"""Parity guard: the no-usable-GPU marker list is DUPLICATED, so pin it.

Two independent guards key on the same container banner:

* ``scripts/runner_isolation.py::NO_USABLE_GPU_LOG_MARKERS`` — the PROACTIVE
  gate. Fails the in-sandbox GPU probe before the real run, so an image whose
  CUDA runtime cannot use the host GPU's arch is caught in seconds.
* ``backend/app/compute_jobs.py::_NO_USABLE_GPU_LOG_MARKERS`` — the REACTIVE
  guard (#14452). Reclassifies an already-SUCCEEDED job as a FAILURE when the
  banner shows up in its logs.

``scripts/`` is deployed standalone on runner hosts with no ``backend`` package
importable, so the tuple cannot be shared by import — it is duplicated. A
duplicated constant silently drifts: someone adds a marker to the reactive
guard, the proactive probe keeps passing the very jobs the new marker describes,
and the regression looks exactly like the original bug. This test is the thing
that stops that.

The scripts-side tuple is read with ``ast`` rather than imported, because
``runner_isolation`` requires its sibling runner modules to import.
"""

import ast
import os

import pytest

from app.compute_jobs import _NO_USABLE_GPU_LOG_MARKERS


def _resolve_runner_isolation_path() -> "str | None":
    """Locate ``scripts/runner_isolation.py`` across local + Docker test envs.

    Mirrors ``test_local_runner_terminal_log.py``: explicit env override, then
    the ``/repo`` bind-mount that ``scripts/test-backend.sh`` provides, then the
    repo root two levels above this file.
    """
    candidates = []
    env_override = os.getenv("BLUEPRINT_RUNNER_ISOLATION_PY")
    if env_override:
        candidates.append(env_override)
    candidates.append("/repo/scripts/runner_isolation.py")
    candidates.append(
        os.path.normpath(
            os.path.join(
                os.path.dirname(__file__), "..", "..", "scripts", "runner_isolation.py"
            )
        )
    )
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _require_runner_isolation_path() -> str:
    """Resolve the script, or skip — except in CI, where a miss FAILS loudly.

    A silent skip would defeat the whole point: the guard exists to catch drift,
    and a drift guard that quietly does not run is worse than none, because it
    reads as coverage.
    """
    path = _resolve_runner_isolation_path()
    if path is not None:
        return path
    msg = (
        "scripts/runner_isolation.py not available in this test environment "
        "(checked BLUEPRINT_RUNNER_ISOLATION_PY, the /repo bind-mount that "
        "scripts/test-backend.sh provides, and ../../scripts relative to this file)"
    )
    if os.getenv("CI"):
        pytest.fail(
            msg + " — refusing to skip in CI: mount the repo root at /repo or set "
            "BLUEPRINT_RUNNER_ISOLATION_PY so this parity invariant enforces"
        )
    pytest.skip(msg)


def _runner_side_markers() -> tuple[str, ...]:
    """``NO_USABLE_GPU_LOG_MARKERS`` from scripts/, read without importing it."""
    path = _require_runner_isolation_path()
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)

    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "NO_USABLE_GPU_LOG_MARKERS":
                assert node.value is not None
                return tuple(ast.literal_eval(node.value))

    pytest.fail(
        f"NO_USABLE_GPU_LOG_MARKERS not found in {path} — it was renamed or removed "
        "without updating the reactive guard in app/compute_jobs.py"
    )


def test_gpu_marker_lists_are_identical_across_runner_and_backend():
    """The proactive probe and the reactive guard must key on the SAME banners.

    If they drift, a marker added to only one side creates a gap that looks like
    coverage: the reactive guard catches the job after it burns an image pull and
    a full run, while the probe that should have caught it in seconds waves it
    through.
    """
    runner_markers = _runner_side_markers()

    assert runner_markers == tuple(_NO_USABLE_GPU_LOG_MARKERS), (
        "no-usable-GPU marker lists have drifted:\n"
        f"  scripts/runner_isolation.py : {runner_markers}\n"
        f"  backend/app/compute_jobs.py : {tuple(_NO_USABLE_GPU_LOG_MARKERS)}\n"
        "Both must be updated together — the runner copy cannot import the "
        "backend one (scripts/ ships standalone to runner hosts)."
    )


def test_gpu_markers_are_lowercase_so_case_insensitive_matching_works():
    """Both sides lowercase the haystack, so a non-lowercase marker never matches.

    An uppercase entry here would be a marker that silently matches nothing —
    the exact failure this parity file exists to prevent.
    """
    for marker in _NO_USABLE_GPU_LOG_MARKERS:
        assert marker == marker.lower(), (
            f"marker {marker!r} is not lowercase; both the probe "
            "(runner_isolation.no_usable_gpu_marker) and the reactive guard "
            "(compute_jobs) match against a lowercased haystack, so this marker "
            "could never fire"
        )

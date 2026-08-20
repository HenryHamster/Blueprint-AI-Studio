"""Task-side capability contracts: what does THIS task demand of a runner?

Extracted from :mod:`app.orchestrator_runner_dispatch`, which is a chronic
size-limit offender (decomposition required; cap bumps are forbidden by
``app.size_limit_policy``).

:mod:`app.runner_eligibility` answers "what does this runner ADVERTISE?".
This module answers the mirror question — "what does this task REQUIRE?" —
and joins the two into the ``_runner_lacks_required_*`` refusal predicates
shared by the dispatch selector and the runner's own claim-time guard in
``app.routers.runner``. Keeping both sides of that join in one place is the
point: when the dispatcher and the claim guard drift, a task is pinned to a
runner that then refuses it.

It also owns the two dispatch-fit inputs that are properties of the WORK
rather than of the runner: the agent-authored ``vram_gb`` /
``min_compute_capability`` coercions, and the container-image free-disk
floors a runner must clear before a multi-GB image job is sent to it.

Every function here is pure (or reads only ``runtime_config``) — no session,
no I/O. Re-exported from ``orchestrator_runner_dispatch`` so existing import
paths keep working unchanged.
"""

import math

from app.models import LocalRunner, Task
from app.runner_eligibility import (
    _runner_chromium_usable,
    _runner_has_project_verification,
)
from app.shared_state import runtime_config as _runtime_config


def _coerce_vram_gb(payload: dict) -> tuple[float | None, str | None]:
    """Resolve the agent-authored ``vram_gb`` hint to a float, or an honest error.

    ``vram_gb`` is AGENT-AUTHORED free-form payload data read in the dispatch hot
    path, so it must be treated as untrusted. A bare ``float(raw)`` raises on the
    values agents actually write — ``"24GB"`` (unit suffix), ``[24]``, ``{}`` —
    and that exception propagates out of the dispatch cycle, taking down dispatch
    for EVERY task in the cycle (blast radius: the whole fleet) while the offending
    task itself only dies later, reasonless, via poison quarantine.

    So: coerce defensively and return ``(value, None)`` on success or
    ``(None, error)`` on a value that cannot be a VRAM figure — the caller
    fails THAT task with a message naming the bad field and value, and the cycle
    survives. ``None`` (unset) is not an error: it simply declares no VRAM floor.

    Non-finite values are rejected too, not just non-numeric ones: ``inf`` parses
    fine but blows up downstream at ``int(vram_gb * 1024)`` (OverflowError) in
    :func:`_select_eligible_runner`, which is the same hot-path crash by a longer
    route. ``bool`` is rejected explicitly — it is an ``int`` subclass, so
    ``float(True) == 1.0`` would otherwise silently mean "1 GB".
    """
    raw = payload.get("vram_gb") or (payload.get("compute_experiment") or {}).get(
        "vram_gb"
    )
    if raw is None:
        return None, None
    if isinstance(raw, bool):
        return None, (
            f"vram_gb must be a number of gigabytes (e.g. 24 or 24.5); "
            f"got boolean {raw!r}"
        )
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except (ValueError, TypeError):
            return None, (
                f"vram_gb must be a number of gigabytes (e.g. 24 or 24.5); got "
                f"{raw!r} — drop the unit suffix and requeue"
            )
    else:
        return None, (
            f"vram_gb must be a number of gigabytes (e.g. 24 or 24.5); got "
            f"{type(raw).__name__} {raw!r}"
        )
    if not math.isfinite(value):
        return None, (
            f"vram_gb must be a finite number of gigabytes (e.g. 24 or 24.5); "
            f"got {raw!r}"
        )
    return value, None


def _coerce_min_compute_capability(payload: dict) -> tuple[float | None, str | None]:
    """Resolve an image's ``min_compute_capability`` floor to a float, or an error.

    ``min_compute_capability`` is the CUDA compute-capability an image's bundled
    GPU wheels require (e.g. ``8.0`` for an ``sm_80``-built image). It is the same
    class of AGENT/OPERATOR-AUTHORED free-form payload data as ``vram_gb`` and is
    read in the dispatch hot path, so it gets the identical defensive contract: a
    bare ``float(raw)`` would raise on the values authors actually write (``"8.0"``
    with a stray unit, ``[8.0]``, ``{}``) and that exception would propagate out of
    the dispatch cycle, taking down dispatch for EVERY task in the cycle while the
    offending task dies later, reasonless, in poison quarantine.

    Returns ``(value, None)`` on success or ``(None, error)`` on a value that
    cannot be a compute-capability figure — the caller fails THAT task with a
    message naming the bad field and the cycle survives. ``None`` (unset) and a
    non-positive figure both mean "no floor" and are not errors. ``bool`` is
    rejected explicitly (an ``int`` subclass, so ``float(True) == 1.0`` would
    silently mean a 1.0 floor).
    """
    raw = payload.get("min_compute_capability") or (
        payload.get("compute_experiment") or {}
    ).get("min_compute_capability")
    if raw is None:
        return None, None
    if isinstance(raw, bool):
        return None, (
            f"min_compute_capability must be a CUDA compute-capability number "
            f"(e.g. 7.0 or 8.9); got boolean {raw!r}"
        )
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except (ValueError, TypeError):
            return None, (
                f"min_compute_capability must be a CUDA compute-capability number "
                f"(e.g. 7.0 or 8.9); got {raw!r} — drop the unit suffix and requeue"
            )
    else:
        return None, (
            f"min_compute_capability must be a CUDA compute-capability number "
            f"(e.g. 7.0 or 8.9); got {type(raw).__name__} {raw!r}"
        )
    if not math.isfinite(value):
        return None, (
            f"min_compute_capability must be a finite CUDA compute-capability "
            f"number (e.g. 7.0 or 8.9); got {raw!r}"
        )
    if value <= 0:
        return None, None  # non-positive floor == no floor
    return value, None

# Free-disk floor a runner must clear to be sent a container-image compute job.
# A baked compute image is multi-GB and must be PULLED before the job starts;
# a runner below this floor would die mid-pull ("no space left on device"),
# wasting the claim. This is a DISPATCH-FIT floor distinct from the (smaller)
# ``_RUNNER_LOW_DISK_THRESHOLD_BYTES_DEFAULT`` alert threshold — the alert warns
# an operator, this gate keeps a doomed job from ever being claimed. Overridable
# via runtime_config ``container_image_min_free_disk_bytes``.
# Missing workspace/image-store telemetry is treated as eligible (backward
# compatible). When reported, the image store must clear the pull floor while
# the workspace clears its smaller checkout/artifact reserve.
_CONTAINER_IMAGE_MIN_FREE_DISK_BYTES_DEFAULT = 10_737_418_240  # 10 GiB
_CONTAINER_WORKSPACE_MIN_FREE_DISK_BYTES_DEFAULT = 2_147_483_648  # 2 GiB


def _container_image_min_free_disk_bytes() -> int:
    """The dispatch-fit free-disk floor for container-image jobs (config-overridable)."""
    try:
        return int(
            _runtime_config.get(
                "container_image_min_free_disk_bytes",
                _CONTAINER_IMAGE_MIN_FREE_DISK_BYTES_DEFAULT,
            )
        )
    except (TypeError, ValueError):
        return _CONTAINER_IMAGE_MIN_FREE_DISK_BYTES_DEFAULT


def _container_workspace_min_free_disk_bytes() -> int:
    """Workspace reserve, preserving the legacy single-floor override."""
    try:
        if "container_workspace_min_free_disk_bytes" not in _runtime_config:
            # Before split-volume telemetry, the image floor governed the only
            # known filesystem. Preserve an explicitly configured legacy value
            # (including zero) until an operator sets the new workspace key.
            if "container_image_min_free_disk_bytes" in _runtime_config:
                return max(
                    0,
                    int(_runtime_config["container_image_min_free_disk_bytes"]),
                )
            return _CONTAINER_WORKSPACE_MIN_FREE_DISK_BYTES_DEFAULT
        return max(
            0,
            int(_runtime_config["container_workspace_min_free_disk_bytes"]),
        )
    except (TypeError, ValueError):
        return _CONTAINER_WORKSPACE_MIN_FREE_DISK_BYTES_DEFAULT

_BROWSER_EVIDENCE_KINDS = frozenset(
    {"browser", "playwright", "screenshot", "viewport", "visual"}
)
_BROWSER_RUNTIME_PATHS = frozenset(
    {
        "admin-ui/package.json",
        "admin-ui/package-lock.json",
        "admin-ui/vite.config.ts",
        "admin-ui/vitest.config.ts",
        "admin-ui/vitest.viewport.config.ts",
        "admin-ui/eslint.config.js",
    }
)
_BROWSER_RUNTIME_PREFIXES = ("admin-ui/src/", "admin-ui/eslint-rules/")


def _paths_require_browser(paths: object) -> bool:
    """Return whether a declared/observed path set enters the viewport gate."""
    if not isinstance(paths, (list, tuple, set)):
        return False
    return any(
        isinstance(path, str)
        and (
            path.startswith(_BROWSER_RUNTIME_PREFIXES)
            or path in _BROWSER_RUNTIME_PATHS
        )
        for path in paths
    )


def _payload_requires_browser(payload: dict, context: dict) -> bool:
    """Read the browser contract from payload and task path provenance.

    ``related_files`` is available before an agent starts (the dispatch-time
    authoring scope), while ``_pr_changed_files`` is the authoritative observed
    diff once GitHub has reported it. Either path set must trigger the same
    ``check:107`` admin-ui gate; missing provenance never grants capability.
    """
    if payload.get("requires_browser") is True:
        return True
    if _paths_require_browser(payload.get("related_files")):
        return True
    if _paths_require_browser((context or {}).get("related_files")):
        return True
    if _paths_require_browser((context or {}).get("_pr_changed_files")):
        return True
    evidence = payload.get("acceptance_evidence")
    if not isinstance(evidence, dict):
        return False
    return str(evidence.get("kind") or "").strip().lower() in _BROWSER_EVIDENCE_KINDS


def _task_requires_browser(task: Task) -> bool:
    """Evaluate the browser contract using the task's complete provenance."""
    return _payload_requires_browser(task.first_payload or {}, task.context or {})


def _runner_lacks_required_browser(runner: LocalRunner, task: Task) -> bool:
    """Return whether ``runner`` must refuse this task's browser contract."""
    return not _runner_chromium_usable(runner) and _task_requires_browser(task)


def _task_requires_project_verification_pinned(task: Task) -> bool:
    """Read the project-verification contract stamped onto ``task`` at pin time.

    Unlike the browser contract above, this cannot be recomputed as a pure
    function of the task alone: it depends on the task's project
    (``is_deploy_repo`` / ``build_config``), which costs a DB round-trip.
    ``_try_runner_dispatch`` already computes ``requires_project_verification``
    once, at pin time; it stamps the result into ``task.context`` so this claim
    -time check can read it back cheaply instead of re-querying the project for
    every polled candidate in ``next-task``'s lock-free pre-pass.
    """
    return bool((task.context or {}).get("requires_project_verification"))


def _runner_lacks_required_project_verification(runner: LocalRunner, task: Task) -> bool:
    """Return whether ``runner`` must refuse this task's project-verification contract."""
    return not _runner_has_project_verification(
        runner
    ) and _task_requires_project_verification_pinned(task)


def _fleet_missing_capability_reason(
    candidates: list,
    *,
    requires_project_verification: bool,
    requires_browser: bool,
) -> str | None:
    """Name the capability, if any, that ZERO active runners advertise.

    Used only when ``_select_eligible_runner`` failed to find a runner for a
    capability-gated task, so a task held for local-only placement gets a
    reason that names the missing capability instead of a generic "waiting
    for capacity" message. Capability advertisement is independent of current
    load/capacity, so this checks the raw candidate pool, not eligibility.
    Order matches ``_select_eligible_runner``'s filter order (project
    verification, then browser) so the reported reason is deterministic when
    both are missing.
    """
    if requires_project_verification and not any(
        _runner_has_project_verification(r) for r in candidates
    ):
        return (
            "requires project verification (ruff + Docker/Postgres); "
            "no active runner advertises it"
        )
    if requires_browser and not any(_runner_chromium_usable(r) for r in candidates):
        return "requires playwright_chromium_usable; no active runner advertises it"
    return None

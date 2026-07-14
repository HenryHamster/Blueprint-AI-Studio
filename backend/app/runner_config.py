"""Server-authoritative LIVE-SAFE runner config: extraction + generation hash.

The runner heartbeat is a two-way config channel: the response carries the
server-authoritative subset of a runner's config that the daemon is allowed to
apply to a LIVE process mid-flight (concurrency, task-type allowlist, GPU-index
allowlist), and the request carries the daemon's applied-config report so the
orchestrator and Fleet UI can tell desired-vs-applied apart.

Everything here is pure (no DB, no I/O) so it can be shared by the runner-facing
heartbeat handler (``app.routers.runner``) and the admin fleet view
(``app.routers.admin_runners``) without a circular import, and unit-tested in
isolation.

All state rides in the existing ``LocalRunner.capabilities`` JSONB — no columns,
no migration. The live-safe subset is a projection of that dict.
"""

import hashlib
import json
from typing import Any, Dict, List, Union

# The keys the daemon is allowed to reconcile onto a LIVE process without a
# restart. Everything else in capabilities is either runner-reported telemetry
# (has_gpu, gpu_count, disk_free_bytes, ...) or requires a bootstrap. Kept in a
# stable order so the generation hash below is deterministic.
LIVE_SAFE_CONFIG_KEYS = (
    "max_concurrent",
    "max_concurrent_compute",
    "task_types",
    "gpu_indices_allowed",
    "gpus_per_job",
)

# Sentinel meaning "no restriction" for the GPU-index allowlist (parallels the
# "all" sentinel used for task_types).
GPU_INDICES_ALL = "all"

# Bounds for the per-job GPU count (how many GPUs a single compute job reserves).
# 1 is the universal default; the upper bound is a sanity ceiling — the largest
# operator box in the fleet (dgx-1) has 8 GPUs, and the daemon self-limits anyway
# (a job simply waits when fewer than ``gpus_per_job`` GPUs are free).
GPUS_PER_JOB_MIN = 1
GPUS_PER_JOB_MAX = 8


def _coerce_positive_int(raw: Any) -> Union[int, None]:
    """Best-effort read of a positive int from a JSONB value (else None)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() and raw > 0 else None
    if isinstance(raw, str):
        try:
            parsed = int(raw.strip())
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _projected_task_types(raw: Any) -> Union[str, List[str]]:
    """Project a stored task_types value into the config block.

    Tolerant of whatever is on the JSONB (the write paths normalize it, but the
    generation hash must never raise on a legacy/oddly-shaped value).
    """
    if raw == "all" or raw is None:
        return "all"
    if isinstance(raw, list):
        return [str(item) for item in raw if isinstance(item, str) and item]
    return "all"


def _projected_gpu_indices(raw: Any) -> Union[str, List[int]]:
    """Project a stored gpu_indices_allowed value into the config block.

    ``None``/absent means unset → treated as the ``"all"`` sentinel (no
    restriction, backward compatible with runners enrolled before this field
    existed). A list is deduped and sorted so the generation hash is stable
    regardless of insertion order.
    """
    if raw is None or raw == GPU_INDICES_ALL:
        return GPU_INDICES_ALL
    if isinstance(raw, list):
        out: List[int] = []
        seen: set[int] = set()
        for item in raw:
            if isinstance(item, bool):
                continue
            value: Union[int, None] = None
            if isinstance(item, int):
                value = item
            elif isinstance(item, float) and item.is_integer():
                value = int(item)
            elif isinstance(item, str) and item.strip().lstrip("+").isdecimal():
                value = int(item.strip())
            if value is None or value < 0 or value in seen:
                continue
            out.append(value)
            seen.add(value)
        return sorted(out)
    return GPU_INDICES_ALL


def _projected_gpus_per_job(raw: Any) -> int:
    """Project a stored gpus_per_job value into the config block.

    ``None``/absent/invalid means unset → the universal default of
    ``GPUS_PER_JOB_MIN`` (1 GPU per job, backward compatible with runners
    enrolled before this field existed). A valid value is clamped into
    ``[GPUS_PER_JOB_MIN, GPUS_PER_JOB_MAX]`` so the generation hash is stable and
    a bad legacy value can never push an out-of-range count to the daemon.
    """
    value = _coerce_positive_int(raw)
    if value is None:
        return GPUS_PER_JOB_MIN
    return max(GPUS_PER_JOB_MIN, min(GPUS_PER_JOB_MAX, value))


def live_safe_config(capabilities: Union[Dict[str, Any], None]) -> Dict[str, Any]:
    """Project the LIVE-SAFE server-authoritative config subset from capabilities.

    ``max_concurrent_compute`` defaults to ``max_concurrent`` when unset, mirroring
    the dispatch-side default in ``admin_runners._runner_max_concurrent_compute``,
    so the daemon and the orchestrator agree on the effective compute ceiling.
    """
    caps = capabilities or {}
    max_concurrent = _coerce_positive_int(caps.get("max_concurrent"))
    raw_compute = caps.get("max_concurrent_compute")
    max_concurrent_compute = (
        _coerce_positive_int(raw_compute) if raw_compute is not None else max_concurrent
    )
    return {
        "max_concurrent": max_concurrent,
        "max_concurrent_compute": max_concurrent_compute,
        "task_types": _projected_task_types(caps.get("task_types", "all")),
        "gpu_indices_allowed": _projected_gpu_indices(caps.get("gpu_indices_allowed")),
        "gpus_per_job": _projected_gpus_per_job(caps.get("gpus_per_job")),
    }


def config_generation(config: Dict[str, Any]) -> str:
    """Deterministic content hash of a live-safe config subset.

    Changes iff the effective live-safe config changes, so the daemon (and the
    UI) can compare a desired generation against the one it last applied without
    diffing every field. Stable across processes/restarts (sorted-key JSON →
    SHA256), truncated to 16 hex chars for a compact marker.
    """
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def live_safe_config_generation(capabilities: Union[Dict[str, Any], None]) -> str:
    """Convenience: project the live-safe subset and hash it in one call."""
    return config_generation(live_safe_config(capabilities))

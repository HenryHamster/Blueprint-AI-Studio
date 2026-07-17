"""Experiment-authoring context: fleet devices, prior-outcome ledger, shared budget.

Three additive authoring-context surfaces for ``compute_experiment`` work, all
bounded by ONE shared character budget so they can never crowd out existing
strategist context:

* **Devices block** — per-runner hardware detail (GPU count/models, per-GPU
  VRAM when advertised, container capability, current load) derived from live
  ``LocalRunner`` capabilities. Built by :func:`build_runner_devices` and
  attached to ``local_runner_status['devices']`` by
  ``strategic_local_runner._load_local_runner_status`` — bounded per-runner
  (model/VRAM list caps) AND in total (:data:`DEVICES_MAX_RUNNERS`).
* **Experiment ledger** — a bounded QUERY over existing rows (recent terminal
  ``compute_experiment`` tasks + their ComputeJobs), NOT a new table. Rendered
  as compact one-per-experiment lines so authoring agents see what was already
  measured (title/hypothesis, normalized command fingerprint, key result
  metric, criterion verdict, cost) before commissioning a near-duplicate.
* **Near-duplicate advisories** — one-line notes emitted when a pending
  (non-terminal) experiment's command/hypothesis closely matches a prior
  ledger entry. Pure normalized-string fingerprint/containment matching — no
  LLM calls, no new task types.

Budget contract (:func:`apply_experiment_authoring_budget`): every addition
shares :data:`EXPERIMENT_AUTHORING_BUDGET_CHARS`, allocated in priority order
**devices > ledger > guidance extras (advisories)** — module constants, not
runtime-config keys. Under pressure advisories drop first, then the oldest
ledger lines, then trailing device entries; the highest-priority content is
trimmed last and existing context is never displaced.
"""

import hashlib
import json
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import ComputeJob, LocalRunner, Task, TaskStatus
from app.runner_eligibility import _container_gpu_passthrough_verified

logger = logging.getLogger(__name__)


# ── ONE shared char budget for ALL experiment-authoring context additions ──
# Priority order under the budget: devices > ledger lines > advisories.
# Module constants by design (review-imposed): no new runtime-config keys.
EXPERIMENT_AUTHORING_BUDGET_CHARS = 4500

# Devices block bounds — per-runner AND total, so the block stays small as the
# fleet grows (the packer ships local_runner_status exempt/unsummarized, so it
# MUST be bounded at build time).
DEVICES_MAX_RUNNERS = 8
DEVICES_MAX_GPU_MODELS = 4
DEVICES_MAX_VRAM_ENTRIES = 8
_DEVICE_NAME_MAX_CHARS = 64

# Experiment-ledger bounds.
EXPERIMENT_LEDGER_MAX_ROWS = 15
_LEDGER_LINE_MAX_CHARS = 240
_LEDGER_TITLE_MAX_CHARS = 60
_LEDGER_HYPOTHESIS_MAX_CHARS = 90
_LEDGER_REASON_MAX_CHARS = 60
_COMMAND_FINGERPRINT_HEX_CHARS = 10

# Near-duplicate advisory bounds.
NEAR_DUPLICATE_MAX_ADVISORIES = 3
_PENDING_EXPERIMENT_SCAN_LIMIT = 10
# Containment matches on very short normalized strings are noise; require some
# substance before calling two commands/hypotheses near-duplicates.
_MIN_CONTAINMENT_CHARS = 24

_TERMINAL_TASK_STATUSES = (
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
)
_PENDING_TASK_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
    TaskStatus.BLOCKED,
)


# ---------------------------------------------------------------------------
# Command normalization / fingerprinting
# ---------------------------------------------------------------------------


def normalize_command(command: Any) -> str:
    """Whitespace-collapsed, stripped command text for fingerprint/containment.

    Deliberately conservative: no token stripping or number scrubbing — the
    advisory is informational and the agent decides, so false negatives are
    cheaper than confusing false positives.
    """
    if not isinstance(command, str):
        return ""
    return " ".join(command.split())


def command_fingerprint(command: Any) -> str | None:
    """Short stable digest of the normalized command (``None`` when empty)."""
    normalized = normalize_command(command)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[
        :_COMMAND_FINGERPRINT_HEX_CHARS
    ]


def _clamp(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1].rstrip() + "…"
    return collapsed


# ---------------------------------------------------------------------------
# Devices block (consumed by strategic_local_runner._load_local_runner_status)
# ---------------------------------------------------------------------------


def _container_flags(capabilities: dict[str, Any]) -> tuple[bool, bool]:
    """(container_ok, gpu_in_container_ok) from a runner's isolation inventory."""
    backends = capabilities.get("isolation_backends")
    if not isinstance(backends, dict):
        return False, False
    container_ok = any(
        bool(backends.get(key))
        for key in ("docker_usable", "podman_usable", "apptainer_ok")
    )
    # Use the same selected-runtime + exact-mode contract as dispatch. Proof for
    # a lower-priority runtime cannot authorize the runtime jobs will execute.
    gpu_in_container_ok = _container_gpu_passthrough_verified(capabilities)
    return container_ok, gpu_in_container_ok


def _dedup_bounded(values: Any, limit: int) -> list[str]:
    """Order-preserving dedup of a string list, bounded to ``limit`` entries."""
    if not isinstance(values, (list, tuple)):
        return []
    seen: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        name = _clamp(value, _DEVICE_NAME_MAX_CHARS)
        if name not in seen:
            seen.append(name)
        if len(seen) >= limit:
            break
    return seen


def build_runner_device(runner: LocalRunner) -> dict[str, Any]:
    """One bounded device descriptor for a live runner's persisted capabilities.

    ``gpu_vram_mib`` is included ONLY when the runner advertises
    ``gpu_memory_total_mib`` (newer heartbeats) — older runners simply omit the
    key rather than reporting a misleading 0. Load counts come from the
    heartbeat-reported ``active_task_ids`` / ``active_compute_job_ids``.
    """
    caps = runner.capabilities or {}
    raw_name = runner.hostname or f"runner-{str(runner.id)[:8]}"
    gpu_count = caps.get("gpu_count")
    if not isinstance(gpu_count, int) or gpu_count < 0:
        gpu_count = 0
    active_tasks = caps.get("active_task_ids")
    active_jobs = caps.get("active_compute_job_ids")
    container_ok, gpu_in_container_ok = _container_flags(caps)
    device: dict[str, Any] = {
        "runner_name": _clamp(raw_name, _DEVICE_NAME_MAX_CHARS),
        "gpu_count": gpu_count,
        "gpu_models": _dedup_bounded(caps.get("gpu_names"), DEVICES_MAX_GPU_MODELS),
        "container_ok": container_ok,
        "gpu_in_container_ok": gpu_in_container_ok,
        "active_tasks": len(active_tasks) if isinstance(active_tasks, list) else 0,
        "active_compute_jobs": len(active_jobs) if isinstance(active_jobs, list) else 0,
    }
    vram = caps.get("gpu_memory_total_mib")
    if isinstance(vram, (list, tuple)):
        bounded_vram = [
            int(v)
            for v in list(vram)[:DEVICES_MAX_VRAM_ENTRIES]
            if isinstance(v, (int, float)) and v > 0
        ]
        if bounded_vram:
            device["gpu_vram_mib"] = bounded_vram
    return device


def build_runner_devices(live_runners: list[LocalRunner]) -> list[dict[str, Any]]:
    """Bounded devices list for the strategist's ``local_runner_status`` block.

    GPU-richest runners first (they are the ones VRAM-fit reasoning needs),
    then most-recently-seen; capped at :data:`DEVICES_MAX_RUNNERS` total so the
    block stays small as the fleet grows.
    """
    ordered = sorted(
        live_runners,
        key=lambda r: (
            -int((r.capabilities or {}).get("gpu_count") or 0),
            -(r.last_seen_at.timestamp() if r.last_seen_at else 0.0),
        ),
    )
    return [build_runner_device(r) for r in ordered[:DEVICES_MAX_RUNNERS]]


# ---------------------------------------------------------------------------
# Experiment ledger — bounded query over existing rows (NOT a new table)
# ---------------------------------------------------------------------------


def _compact_criterion(criterion: Any) -> str | None:
    """``accuracy>=0.9 MET (0.93)`` from a stored criterion verdict dict."""
    if not isinstance(criterion, dict):
        return None
    metric = criterion.get("metric")
    operator = criterion.get("operator")
    threshold = criterion.get("threshold")
    if not metric or operator is None or threshold is None:
        return None
    verdict = "MET" if criterion.get("met") else "unmet"
    value = criterion.get("value")
    value_note = f" ({value})" if value is not None else " (unmeasured)"
    return f"{metric}{operator}{threshold} {verdict}{value_note}"


async def build_experiment_ledger(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    limit: int = EXPERIMENT_LEDGER_MAX_ROWS,
) -> list[dict[str, Any]]:
    """Last ``limit`` terminal compute_experiment outcomes for the project.

    A bounded query over existing ``Task`` rows (first payload type
    ``compute_experiment``, terminal status) plus their ``ComputeJob`` rows for
    cost/outcome fallback — deliberately NOT a new table. Newest first. Each
    entry carries: task id, title, hypothesis, normalized command fingerprint,
    outcome, compact criterion verdict, cost, and conclusion timestamp.
    """
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.payloads[0]["type"].as_string() == "compute_experiment",
            Task.status.in_(_TERMINAL_TASK_STATUSES),
        )
        .order_by(Task.created_at.desc())
        .limit(limit)
    )
    tasks = (await session.execute(stmt)).scalars().all()
    if not tasks:
        return []

    # Latest ComputeJob per task — fallback for cost/outcome/terminal_reason on
    # tasks whose plan_output carries no compute_result (crash-before-metric).
    job_by_task: dict[uuid.UUID, ComputeJob] = {}
    job_rows = (
        await session.execute(
            select(ComputeJob)
            .where(ComputeJob.task_id.in_([t.id for t in tasks]))
            .order_by(ComputeJob.created_at.asc())
        )
    ).scalars().all()
    for job in job_rows:
        if job.task_id is not None:
            job_by_task[job.task_id] = job  # ascending order → latest wins

    entries: list[dict[str, Any]] = []
    for task in tasks:
        payload = task.first_payload or {}
        plan_output = task.plan_output if isinstance(task.plan_output, dict) else {}
        compute_result = plan_output.get("compute_result")
        compute_result = compute_result if isinstance(compute_result, dict) else {}
        job = job_by_task.get(task.id)

        outcome = compute_result.get("outcome")
        if not outcome and job is not None and job.outcome is not None:
            outcome = getattr(job.outcome, "value", job.outcome)
        if not outcome:
            outcome = getattr(task.status, "value", task.status)

        cost = compute_result.get("final_cost_usd")
        if cost is None and job is not None:
            cost = job.final_cost_usd

        terminal_reason = None
        if job is not None and job.terminal_reason and str(outcome) not in (
            "SUCCESS",
            "COMPLETED",
        ):
            terminal_reason = _clamp(job.terminal_reason, _LEDGER_REASON_MAX_CHARS)

        concluded = task.completed_at or task.created_at
        entries.append(
            {
                "task_id": str(task.id),
                "title": _clamp(task.title or "", _LEDGER_TITLE_MAX_CHARS),
                "hypothesis": _clamp(
                    str(payload.get("hypothesis") or ""), _LEDGER_HYPOTHESIS_MAX_CHARS
                ),
                "command_fingerprint": command_fingerprint(payload.get("command")),
                "normalized_command": normalize_command(payload.get("command")),
                "outcome": str(outcome),
                "criterion": _compact_criterion(compute_result.get("criterion")),
                "cost_usd": cost,
                "terminal_reason": terminal_reason,
                "concluded_at": concluded.isoformat() if concluded else None,
            }
        )
    return entries


def render_experiment_ledger_lines(entries: list[dict[str, Any]]) -> list[str]:
    """One compact line per ledger entry (newest first), each length-clamped."""
    lines: list[str] = []
    for entry in entries:
        date = (entry.get("concluded_at") or "")[:10]
        parts: list[str] = [
            f"- [{date}] {entry.get('task_id', '')[:8]} \"{entry.get('title') or 'untitled'}\""
        ]
        if entry.get("hypothesis"):
            parts.append(f"hyp: {entry['hypothesis']}")
        if entry.get("command_fingerprint"):
            parts.append(f"cmd#{entry['command_fingerprint']}")
        parts.append(f"outcome={entry.get('outcome')}")
        if entry.get("criterion"):
            parts.append(f"crit: {entry['criterion']}")
        if entry.get("cost_usd") is not None:
            parts.append(f"cost=${entry['cost_usd']}")
        if entry.get("terminal_reason"):
            parts.append(f"reason: {entry['terminal_reason']}")
        lines.append(_clamp(" | ".join(parts), _LEDGER_LINE_MAX_CHARS))
    return lines


# ---------------------------------------------------------------------------
# Near-duplicate advisory (guidance extras — lowest budget priority)
# ---------------------------------------------------------------------------


async def fetch_pending_experiment_specs(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    limit: int = _PENDING_EXPERIMENT_SCAN_LIMIT,
) -> list[dict[str, Any]]:
    """Compact specs of pending (non-terminal) compute_experiment tasks."""
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.payloads[0]["type"].as_string() == "compute_experiment",
            Task.status.in_(_PENDING_TASK_STATUSES),
        )
        .order_by(Task.created_at.desc())
        .limit(limit)
    )
    tasks = (await session.execute(stmt)).scalars().all()
    specs: list[dict[str, Any]] = []
    for task in tasks:
        payload = task.first_payload or {}
        specs.append(
            {
                "task_id": str(task.id),
                "title": _clamp(task.title or "", _LEDGER_TITLE_MAX_CHARS),
                "command": normalize_command(payload.get("command")),
                "hypothesis": normalize_command(payload.get("hypothesis")),
            }
        )
    return specs


def _contains_either_way(a: str, b: str) -> bool:
    if len(a) < _MIN_CONTAINMENT_CHARS or len(b) < _MIN_CONTAINMENT_CHARS:
        return False
    a_low, b_low = a.casefold(), b.casefold()
    return a_low in b_low or b_low in a_low


def _matches_prior(spec: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Simple normalized-string fingerprint/containment match — NO LLM calls."""
    spec_cmd = spec.get("command") or ""
    entry_cmd = entry.get("normalized_command") or ""
    if (
        spec_cmd
        and entry.get("command_fingerprint")
        and command_fingerprint(spec_cmd) == entry["command_fingerprint"]
    ):
        return True
    if spec_cmd and entry_cmd and _contains_either_way(spec_cmd, entry_cmd):
        return True
    spec_hyp = spec.get("hypothesis") or ""
    entry_hyp = entry.get("hypothesis") or ""
    return bool(spec_hyp and entry_hyp and _contains_either_way(spec_hyp, entry_hyp))


def build_near_duplicate_advisories(
    pending_specs: list[dict[str, Any]],
    ledger_entries: list[dict[str, Any]],
    *,
    limit: int = NEAR_DUPLICATE_MAX_ADVISORIES,
) -> list[str]:
    """One-line advisories for pending experiments matching a prior ledger entry.

    Advisory-only: the agent decides whether a re-run is justified (seed
    sweeps and post-fix retries are legitimate) — nothing is blocked.
    """
    advisories: list[str] = []
    for spec in pending_specs:
        match = next((e for e in ledger_entries if _matches_prior(spec, e)), None)
        if match is None:
            continue
        result_bits = [f"outcome={match.get('outcome')}"]
        if match.get("criterion"):
            result_bits.append(match["criterion"])
        if match.get("cost_usd") is not None:
            result_bits.append(f"cost=${match['cost_usd']}")
        advisories.append(
            _clamp(
                f"- NEAR-DUPLICATE: pending experiment {spec['task_id'][:8]} "
                f"\"{spec.get('title') or 'untitled'}\" closely matches prior "
                f"{match.get('task_id', '')[:8]} ({', '.join(result_bits)}). "
                "Re-run only if something material changed — name what.",
                _LEDGER_LINE_MAX_CHARS,
            )
        )
        if len(advisories) >= limit:
            break
    return advisories


# ---------------------------------------------------------------------------
# Shared budget — devices > ledger > guidance extras, one total cap
# ---------------------------------------------------------------------------


def _devices_chars(devices: list[dict[str, Any]]) -> int:
    try:
        return len(json.dumps(devices, separators=(",", ":"), default=str))
    except (TypeError, ValueError):
        return sum(len(str(d)) for d in devices)


def apply_experiment_authoring_budget(
    devices: list[dict[str, Any]] | None,
    ledger_lines: list[str],
    advisory_lines: list[str],
    *,
    budget_chars: int = EXPERIMENT_AUTHORING_BUDGET_CHARS,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Trim the three additions to fit ONE shared character budget.

    Priority order (kept longest under pressure): devices > ledger lines >
    advisories. Devices are trimmed from the tail (lowest-GPU runners drop
    first per :func:`build_runner_devices` ordering); ledger lines drop from
    the oldest end (list is newest-first); advisories drop wholesale first.
    """
    kept_devices = list(devices or [])
    while kept_devices and _devices_chars(kept_devices) > budget_chars:
        kept_devices.pop()

    remaining = budget_chars - _devices_chars(kept_devices) if kept_devices else budget_chars

    kept_ledger = list(ledger_lines)
    while kept_ledger and sum(len(line) + 1 for line in kept_ledger) > remaining:
        kept_ledger.pop()  # newest-first list → drop the oldest entry
    remaining -= sum(len(line) + 1 for line in kept_ledger)

    kept_advisories = list(advisory_lines)
    while kept_advisories and sum(len(line) + 1 for line in kept_advisories) > remaining:
        kept_advisories.pop()

    return kept_devices, kept_ledger, kept_advisories


def render_experiment_ledger_prompt_block(
    ledger_lines: list[str],
    advisory_lines: list[str],
) -> str:
    """Markdown block appended to the strategist's cycle ``prompt_suffix``.

    Returns ``''`` when there is nothing to show so the common no-experiments
    path adds zero bytes.
    """
    if not ledger_lines and not advisory_lines:
        return ""
    sections: list[str] = []
    if ledger_lines:
        sections.append(
            "## Experiment ledger (recent compute_experiment outcomes)\n"
            "Already-measured results for this project — check BEFORE "
            "commissioning a new experiment; do not re-run a settled hypothesis "
            "without naming what changed.\n" + "\n".join(ledger_lines)
        )
    if advisory_lines:
        sections.append(
            "### Near-duplicate advisories\n" + "\n".join(advisory_lines)
        )
    return "\n\n".join(sections)

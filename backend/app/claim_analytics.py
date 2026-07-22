"""Claim-path telemetry helpers.

This module is the canonical home for analytics fields stamped while an
agent claims work. The cold /claim endpoint records dispatch-to-claim latency;
warm /next-role claims record their reuse origin without creating cold-start
samples.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Callable

from app.models import _utcnow


class ClaimResponseTiming:
    """Collect coarse, in-process timings for one ``/claim`` response.

    This deliberately stays request-local: it adds no persistence or lifecycle
    state, and callers emit a single structured log only after the response has
    been assembled. Phase values are durations between consecutive ``mark``
    calls, which makes the slow section visible without logging task contents.
    """

    def __init__(self, *, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._started_at = clock()
        self._last_mark_at = self._started_at
        self._phase_ms: dict[str, int] = {}

    def mark(self, phase: str) -> None:
        now = self._clock()
        self._phase_ms[phase] = max(0, round((now - self._last_mark_at) * 1000))
        self._last_mark_at = now

    def finish(self, response: object) -> dict[str, Any]:
        """Return log-safe timing and approximate serialized-size metrics."""

        measurement_started_at = self._clock()
        response_bytes: int | None
        try:
            response_bytes = len(
                json.dumps(
                    response,
                    default=str,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError, OverflowError):
            response_bytes = None
        finished_at = self._clock()
        return {
            "total_ms": max(0, round((finished_at - self._started_at) * 1000)),
            "phase_ms": dict(self._phase_ms),
            "response_bytes_approx": response_bytes,
            "size_measurement_ms": max(
                0, round((finished_at - measurement_started_at) * 1000)
            ),
        }


def stamp_cold_claim_timing(task: Any, *, claimed_at: datetime | None = None) -> bool:
    """Stamp cold dispatch-to-claim latency fields on ``task``.

    Returns True when a dispatch/provision anchor existed and timing was stamped.
    The dedicated ``vm_cold_start_ms`` column is write-once so later retries or
    provision-confirm restamps cannot overwrite the first real claim sample.
    """

    effective_claimed_at = claimed_at or task.last_heartbeat or _utcnow()
    cold_start_anchor = task.vm_dispatched_at or task.vm_provisioned_at
    if cold_start_anchor is None:
        return False

    claim_latency = (effective_claimed_at - cold_start_anchor).total_seconds()
    context = dict(task.context or {})
    stages = dict(context.get("_cold_start_stages") or {})
    stages["claim_at"] = effective_claimed_at.isoformat()

    # Backfill provision_slot_acquired_at from compute's in-memory store if not
    # already present; efficiency snapshots use it for dispatch_to_slot_ms.
    if "provision_slot_acquired_at" not in stages and task.vm_instance_id:
        from app.compute import get_provision_slot_acquired_at

        slot_ts = get_provision_slot_acquired_at(task.vm_instance_id)
        if slot_ts:
            stages["provision_slot_acquired_at"] = slot_ts

    context["claim_latency_secs"] = round(claim_latency, 1)
    context["_cold_start_stages"] = stages
    task.context = context

    if task.vm_cold_start_ms is None:
        task.vm_cold_start_ms = max(0, int(claim_latency * 1000))
    return True


def stamp_warm_claim_timing(
    context: dict | None,
    *,
    origin: str,
    vm_instance_id: str,
) -> dict:
    """Return ``context`` with warm-claim timing attribution stamped.

    ``warm_start`` is set with ``setdefault`` to preserve the existing
    idempotent contract: agent status remains the authoritative source if a
    prior value is already present.
    """

    updated = dict(context or {})
    timing = dict(updated.get("_timing") or {})
    timing.setdefault("warm_start", True)
    timing["warm_claim_origin"] = origin
    timing["prior_role_vm_id"] = vm_instance_id
    updated["_timing"] = timing
    return updated


__all__ = [
    "ClaimResponseTiming",
    "stamp_cold_claim_timing",
    "stamp_warm_claim_timing",
]

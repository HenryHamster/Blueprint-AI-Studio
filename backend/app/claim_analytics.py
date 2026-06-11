"""Claim-path telemetry helpers.

This module is the canonical home for analytics fields stamped while an
agent claims work. The cold /claim endpoint records dispatch-to-claim latency;
warm /next-role claims record their reuse origin without creating cold-start
samples.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.models import _utcnow


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


__all__ = ["stamp_cold_claim_timing", "stamp_warm_claim_timing"]

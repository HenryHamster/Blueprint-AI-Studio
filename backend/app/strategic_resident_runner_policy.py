"""Immutable execution-route policy for bounded Strategic Resident turns."""

from __future__ import annotations

from typing import Any


RESIDENT_TURN_TASK_TYPE = "strategic_resident_turn"
RESIDENT_RUNNER_POLICY_PAYLOAD_KEY = "resident_runner_policy"


def runner_policy_requires_local(policy: object) -> bool:
    return bool(
        isinstance(policy, dict)
        and (
            policy.get("require_local_runner") is True
            or policy.get("mode") in {"local", "local_required"}
        )
    )


def resident_payload(payloads: object) -> dict[str, Any] | None:
    if not isinstance(payloads, (list, tuple)) or not payloads:
        return None
    first = payloads[0]
    if not isinstance(first, dict) or first.get("type") != RESIDENT_TURN_TASK_TYPE:
        return None
    return first


def is_resident_turn(task: object) -> bool:
    return resident_payload(getattr(task, "payloads", None)) is not None


def resident_payloads_require_local_runner(payloads: object) -> bool:
    """Read local-only authority from the frozen first payload, never context."""

    payload = resident_payload(payloads)
    return bool(
        payload is not None
        and runner_policy_requires_local(
            payload.get(RESIDENT_RUNNER_POLICY_PAYLOAD_KEY)
        )
    )


def resident_turn_requires_local_runner(task: object) -> bool:
    return resident_payloads_require_local_runner(getattr(task, "payloads", None))


def resident_local_claim_route_matches(task: object, vm_instance_id: object) -> bool:
    """Require a local-only turn to arrive through its selected runner pin."""

    if not resident_turn_requires_local_runner(task):
        return True
    vm_id = str(vm_instance_id or "")
    if not vm_id.startswith("runner:"):
        return False
    runner_id = vm_id.removeprefix("runner:").strip()
    context = getattr(task, "context", None)
    target = (context or {}).get("runner_target_id") if isinstance(context, dict) else None
    return bool(runner_id and str(target or "") == runner_id)


__all__ = [
    "RESIDENT_RUNNER_POLICY_PAYLOAD_KEY",
    "RESIDENT_TURN_TASK_TYPE",
    "is_resident_turn",
    "resident_local_claim_route_matches",
    "resident_payload",
    "resident_payloads_require_local_runner",
    "resident_turn_requires_local_runner",
    "runner_policy_requires_local",
]

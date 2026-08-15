"""Failure-frame classification and server-owned execution requirements.

The text parser in this module is compatibility-only classification evidence;
its result must never grant routing or retry-budget authority. Durable records
are minted by ``agent_status`` only from its schema-validated structured
terminal field, then read here for later retry/unblock dispatch.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Mapping


REQUIRED_EXECUTION_CAPABILITIES_CONTEXT_KEY = "required_execution_capabilities"
REQUIRED_EXECUTION_CAPABILITIES_AUTHORITY = "server_failure_observation_v1"
EXECUTION_CAPABILITY_WAIT_CONTEXT_KEY = "execution_capability_wait"

CAPABILITY_BACKEND_VERIFICATION = "backend_verification"
KNOWN_EXECUTION_CAPABILITIES = frozenset({CAPABILITY_BACKEND_VERIFICATION})

POSTGRES_RUNTIME_UNAVAILABLE_SENTINEL = (
    "[check] required backend verification state=runtime_unavailable "
    "— PostgreSQL runtime unavailable"
)


def classify_execution_capability_failure_frames(*texts: object) -> frozenset[str]:
    """Classify exact failure frames without establishing routing authority."""

    capabilities: set[str] = set()
    for text in texts:
        if not isinstance(text, str):
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line == POSTGRES_RUNTIME_UNAVAILABLE_SENTINEL:
                capabilities.add(CAPABILITY_BACKEND_VERIFICATION)
    return frozenset(capabilities)


def learned_execution_capabilities(context: Mapping | None) -> frozenset[str]:
    """Read only server-authoritative, bounded capability records."""

    raw = (context or {}).get(REQUIRED_EXECUTION_CAPABILITIES_CONTEXT_KEY)
    if not isinstance(raw, dict):
        return frozenset()
    return frozenset(
        capability
        for capability, record in raw.items()
        if capability in KNOWN_EXECUTION_CAPABILITIES
        and isinstance(record, dict)
        and record.get("authority") == REQUIRED_EXECUTION_CAPABILITIES_AUTHORITY
    )


def persist_learned_execution_capabilities(
    context: Mapping | None,
    capabilities: Iterable[str],
    *,
    observed_at: datetime,
) -> tuple[dict, bool]:
    """Merge trusted requirements into a bounded copy of task context."""

    requested = KNOWN_EXECUTION_CAPABILITIES.intersection(capabilities)
    if not requested:
        return dict(context or {}), False

    new_context = dict(context or {})
    existing = new_context.get(REQUIRED_EXECUTION_CAPABILITIES_CONTEXT_KEY)
    records = dict(existing) if isinstance(existing, dict) else {}
    changed = False
    for capability in sorted(requested):
        record = records.get(capability)
        if (
            isinstance(record, dict)
            and record.get("authority") == REQUIRED_EXECUTION_CAPABILITIES_AUTHORITY
        ):
            continue
        records[capability] = {
            "authority": REQUIRED_EXECUTION_CAPABILITIES_AUTHORITY,
            "observed_at": observed_at.isoformat(),
        }
        changed = True
    if changed:
        # At most the one known key survives.  Never retain agent-authored or
        # unknown capability names from a pre-existing context value.
        new_context[REQUIRED_EXECUTION_CAPABILITIES_CONTEXT_KEY] = {
            capability: records[capability]
            for capability in sorted(KNOWN_EXECUTION_CAPABILITIES)
            if capability in records
            and isinstance(records[capability], dict)
            and records[capability].get("authority")
            == REQUIRED_EXECUTION_CAPABILITIES_AUTHORITY
        }
    return new_context, changed


def has_learned_execution_capability(context: Mapping | None, capability: str) -> bool:
    return capability in learned_execution_capabilities(context)

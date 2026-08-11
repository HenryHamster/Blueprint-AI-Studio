"""Shared release markers for the research findings producer contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# A task receives this server-owned, attempt-scoped marker only on an
# authoritative claim. Completion uses it to enforce the schema emitted by the
# corresponding agent release while stale runners and pre-release,
# already-running attempts remain on the legacy compatibility path.
RESEARCH_OUTPUT_CONTRACT_VERSION_KEY = "research_output_contract_version"
RESEARCH_OUTPUT_CONTRACT_VERSION = 2
RESEARCH_OUTPUT_CONTRACT_VERSION_HEADER = "x-research-output-contract-version"

# Deferred findings can outlive the agent release that produced them.  Keep the
# origin contract version on each finding so a newer consumer can validate the
# artifact under the producer's contract instead of retroactively applying its
# own.  The field is authored/normalized by the backend, never by agent output.
RESEARCH_FINDING_ORIGIN_CONTRACT_VERSION_KEY = (
    "origin_research_output_contract_version"
)
RESEARCH_OUTPUT_CONTRACT_LEGACY_VERSION = 1


def normalize_research_output_contract_version(
    raw_version: Any,
    *,
    fallback: int = RESEARCH_OUTPUT_CONTRACT_LEGACY_VERSION,
) -> int:
    """Return a positive contract version, falling back for absent/bad input."""

    try:
        version = int(raw_version)
    except (TypeError, ValueError):
        return fallback
    return version if version >= RESEARCH_OUTPUT_CONTRACT_LEGACY_VERSION else fallback


def research_output_contract_version(
    context: Mapping[str, Any] | None,
) -> int:
    """Resolve the server-owned attempt contract, defaulting to legacy v1."""

    raw_version = context.get(RESEARCH_OUTPUT_CONTRACT_VERSION_KEY) if context else None
    return normalize_research_output_contract_version(raw_version)


def research_finding_origin_contract_version(
    finding: Mapping[str, Any],
) -> int:
    """Resolve the contract version stamped on a deferred finding's origin."""

    return normalize_research_output_contract_version(
        finding.get(RESEARCH_FINDING_ORIGIN_CONTRACT_VERSION_KEY)
    )

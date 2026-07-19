"""Strict identity policy for decisive Resident evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


EvidenceKey = tuple[str, str, str, str]
MAX_TRUSTED_EVIDENCE = 256
SOURCE_ID_FIELDS = (
    "source_id",
    "artifact_id",
    "task_id",
    "message_id",
    "tool_id",
    "attachment_id",
    "id",
)
EVIDENCE_KINDS = frozenset(
    {
        "task",
        "task_log",
        "runner",
        "runner_capacity",
        "pr",
        "pr_diff",
        "pr_check",
        "ci_run",
        "deployment",
        "artifact",
        "operator_message",
        "other",
    }
)
# Maps each inspection capability to its receipt operation kind. Trusted evidence
# requires that exact pairing; stale keys fail closed. The capability-kind
# consistency test guards this table against drift.
INSPECTION_CAPABILITY_KINDS = {
    "task.inspect": "inspect_task",
    "task.logs.inspect": "inspect_task_log",
    "task.failure.inspect": "inspect_failure",
    "task.provider.inspect": "inspect_provider",
    "task.lineage.inspect": "inspect_task_lineage",
    "runner.inspect": "inspect_runner",
    "runner.list": "inspect_runner_capacity",
    "pipeline.inspect": "inspect_pipeline",
    "qualification.inspect": "inspect_qualification",
    "pr.inspect": "inspect_pr",
    "pr.diff.inspect": "inspect_pr_diff",
    "pr.checks.inspect": "inspect_pr_checks",
    "ci.run.inspect": "inspect_ci",
    "deployment.inspect": "inspect_deployment",
}
SUCCESSFUL_INSPECTION_STATUSES = frozenset({"applied", "reconciled"})

# Maps a ``ResidentTaskEvidence.kind`` (the inspection taxonomy) to the trust
# ``EVIDENCE_KINDS`` taxonomy used by ``evidence_key``/``build_trusted_evidence_index``.
# All task-derived inspections collapse to ``task`` except logs -> ``task_log``;
# source_id + revision + checksum still distinguish the specific evidence (e.g. a
# failure vs a provider inspection of the same task differ by checksum), so the
# collapse never conflates distinct evidence.
INSPECTION_EVIDENCE_KIND_MAP = {
    "task": "task",
    "lineage": "task",
    "logs": "task_log",
    "failure": "task",
    "provider": "task",
    "runner": "runner",
    "runner_capacity": "runner_capacity",
    "pr": "pr",
    "pr_diff": "pr_diff",
    "pr_check": "pr_check",
}


def _bounded_identity(value: Any, *, max_bytes: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value if len(value.encode("utf-8")) <= max_bytes else None


def evidence_key(
    item: Mapping[str, Any],
    *,
    allow_source_aliases: bool,
) -> EvidenceKey | None:
    """Return an exact bounded key, rejecting truncation and alias ambiguity."""

    kind = _bounded_identity(item.get("kind"), max_bytes=64)
    revision = _bounded_identity(item.get("revision"), max_bytes=512)
    checksum = _bounded_identity(item.get("checksum"), max_bytes=256)
    if kind not in EVIDENCE_KINDS or revision is None or checksum is None:
        return None
    identity_fields = SOURCE_ID_FIELDS if allow_source_aliases else ("source_id",)
    if not allow_source_aliases and any(
        item.get(field) is not None
        for field in SOURCE_ID_FIELDS
        if field != "source_id"
    ):
        return None
    identities = {
        value
        for field in identity_fields
        if (value := _bounded_identity(item.get(field), max_bytes=512)) is not None
    }
    if len(identities) != 1:
        return None
    return kind, identities.pop(), revision, checksum


__all__ = [
    "INSPECTION_CAPABILITY_KINDS",
    "MAX_TRUSTED_EVIDENCE",
    "SUCCESSFUL_INSPECTION_STATUSES",
    "EvidenceKey",
    "evidence_key",
]

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
INSPECTION_CAPABILITY_KINDS = {
    "project.inspect": "inspect_project",
    "task.inspect": "inspect_task",
    "task_log.inspect": "inspect_task_log",
    "failure.inspect": "inspect_failure",
    "provider.inspect": "inspect_provider",
    "runner.inspect": "inspect_runner",
    "pipeline.inspect": "inspect_pipeline",
    "qualification.inspect": "inspect_qualification",
    "pr.inspect": "inspect_pr",
    "ci.inspect": "inspect_ci",
    "deployment.inspect": "inspect_deployment",
    "artifact.inspect": "inspect_artifact",
    "message.inspect": "inspect_message",
}
SUCCESSFUL_INSPECTION_STATUSES = frozenset({"applied", "reconciled"})


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

"""Server-owned task-context namespace for grader/challenge sidecars.

These keys can change whether an artifact is graded, which sidecar may resolve a
parked source, or how a returned verdict is applied. They are execution
authority, not agent/user-authored task context. Keep the namespace policy in a
small leaf module so every untrusted context-ingress path uses the same rule.
"""

from __future__ import annotations

import uuid
from typing import Any


# Exact keys without a dedicated prefix, plus the three active verdict records.
# The grader/challenge marker fields live on the sidecar itself and control how
# its terminal output is interpreted, so they are authority too (not harmless
# descriptive context).
SIDECAR_SERVER_OWNED_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        "quality_grader",
        "adversarial_gate",
        "finding_challenge",
        "finding_challenge_records",
        "awaiting_quality_grader",
        "awaiting_adversarial_grader",
        "awaiting_finding_challenge",
        "sidecar_verdict_application_pending",
        "graded_task_type",
        "adversarial_grader",
        "challenged_boundary",
        "challenged_first_verdict",
        "challenged_finding_hash",
        "grader_diagnostic_trail",
    }
)

# Prefixes cover current diagnostics, source links, bounded histories, retry
# receipts, and future fields added inside the same authority namespace.
SIDECAR_SERVER_OWNED_CONTEXT_PREFIXES: tuple[str, ...] = (
    "quality_grader_",
    "adversarial_gate_",
    "finding_challenge_",
    "research_spawn_gate_",
    "_adversarial_",
)

# Context that must survive a fresh execution attempt of the sidecar itself.
# These fields are fixed when the backend creates the sidecar and are needed to
# route/interpret its eventual terminal result. They are *not* preserved on an
# ordinary/source task merely because the same key is present there.
QUALITY_GRADER_SOURCE_CONTEXT_KEY = "quality_grader_for_task_id"
FINDING_CHALLENGE_SOURCE_CONTEXT_KEY = "finding_challenge_for_task_id"
SIDECAR_QUALITY_IMMUTABLE_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        QUALITY_GRADER_SOURCE_CONTEXT_KEY,
        "graded_task_type",
        "adversarial_grader",
        "_adversarial_challenged_hashes",
    }
)
SIDECAR_CHALLENGE_IMMUTABLE_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        FINDING_CHALLENGE_SOURCE_CONTEXT_KEY,
        "challenged_boundary",
        "challenged_first_verdict",
        "challenged_finding_hash",
    }
)


def is_server_owned_sidecar_context_key(key: object) -> bool:
    """Return whether ``key`` belongs to the backend-owned sidecar namespace."""

    return isinstance(key, str) and (
        key in SIDECAR_SERVER_OWNED_CONTEXT_KEYS
        or key.startswith(SIDECAR_SERVER_OWNED_CONTEXT_PREFIXES)
    )


def _authenticated_backend_sidecar_lane(task: Any) -> str | None:
    if (
        str(getattr(task, "created_by", "") or "").upper() != "ORCHESTRATOR"
        or getattr(task, "parent_task_id", None) is not None
    ):
        return None
    try:
        payload = task.first_payload
    except Exception:  # noqa: BLE001 -- unloaded ORM attributes fail closed
        return None
    if not isinstance(payload, dict) or payload.get("type") != "investigate":
        return None
    context = getattr(task, "context", None)
    if not isinstance(context, dict):
        return None

    valid_source_keys: list[str] = []
    for source_key in (
        QUALITY_GRADER_SOURCE_CONTEXT_KEY,
        FINDING_CHALLENGE_SOURCE_CONTEXT_KEY,
    ):
        try:
            uuid.UUID(str(context.get(source_key)))
        except (TypeError, ValueError, AttributeError):
            continue
        valid_source_keys.append(source_key)
    if len(valid_source_keys) != 1:
        return None
    return (
        "quality_grader"
        if valid_source_keys[0] == QUALITY_GRADER_SOURCE_CONTEXT_KEY
        else "finding_challenge"
    )


def is_authenticated_backend_sidecar_shape(task: Any) -> bool:
    """Return whether ``task`` has the immutable backend sidecar shape."""

    return _authenticated_backend_sidecar_lane(task) is not None


def authenticated_sidecar_fresh_attempt_preserve_keys(task: Any) -> frozenset[str]:
    """Return immutable sidecar keys safe to retain when ``task`` is requeued.

    This is deliberately task-aware. ``_adversarial_challenged_hashes`` is
    stale gate state on a research source but immutable interpretation state on
    its adversarial grader. Preserve it only for the backend-authored,
    parentless ``investigate`` sidecar shape with exactly one valid source link.
    The source row's exact awaited-id/project authorization remains enforced at
    verdict delivery; this helper only decides reset semantics.
    """

    lane = _authenticated_backend_sidecar_lane(task)
    if lane is None:
        return frozenset()
    immutable_keys = (
        SIDECAR_QUALITY_IMMUTABLE_CONTEXT_KEYS
        if lane == "quality_grader"
        else SIDECAR_CHALLENGE_IMMUTABLE_CONTEXT_KEYS
    )
    context = task.context or {}
    return frozenset(key for key in immutable_keys if key in context)


# State that belongs to one source-task execution attempt. Source-link keys are
# deliberately absent: a failed grader/challenge task may itself be requeued and
# must retain the immutable link that routes its eventual terminal result.
SIDECAR_FRESH_ATTEMPT_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        "quality_grader",
        "quality_grader_diagnostic",
        "adversarial_gate",
        "adversarial_gate_diagnostic",
        "finding_challenge",
        "finding_challenge_records",
        "awaiting_quality_grader",
        "awaiting_adversarial_grader",
        "awaiting_finding_challenge",
        "sidecar_verdict_application_pending",
        "_adversarial_challenged_hashes",
        "_adversarial_original_challenged_hashes",
        "_adversarial_wholesale_retry_count",
        "_adversarial_targeted_retry_count",
        "research_spawn_gate_wholesale_retry_exhausted",
        "research_spawn_gate_refusal_skip_retry",
        "research_spawn_gate_last_wholesale_parse_outcome",
    }
)

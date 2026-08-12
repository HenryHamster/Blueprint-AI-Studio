"""Bounded, hash-stable views of release policy decision contracts."""

from __future__ import annotations


import contextlib
import hashlib
import json
import math
from typing import Any
_MODULE_SIZE_GOVERNANCE_CAP: int = 115

_POLICY_PROJECTION_WEIGHT_LIMIT = 8
_POLICY_PROJECTION_ERROR_CHARS = 512


def promotion_policy_projection(
    policy: dict[str, Any],
    *,
    source: str,
    fallback_error: str | None = None,
) -> dict[str, Any]:
    """Return the bounded, hash-stable policy truth exposed to Strategic.

    The qualifier's full policy may carry non-scoring feature flags. Strategic
    needs only the confidence threshold, active scoring weights, and provenance
    needed to distinguish live runtime truth from an older attempt snapshot.
    ``policy_hash`` deliberately excludes ``fallback_error``: exception wording
    is diagnostic provenance, not part of the effective decision contract.
    """

    raw_weights = policy.get("weights")
    weights: dict[str, float] = {}
    if isinstance(raw_weights, dict):
        ordered_weights = sorted(raw_weights.items(), key=lambda item: str(item[0]))
        for raw_key, value in ordered_weights[:_POLICY_PROJECTION_WEIGHT_LIMIT]:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                with contextlib.suppress(OverflowError):
                    finite_value = float(value)
                    if math.isfinite(finite_value):
                        key = str(raw_key)[:64]
                        weights[key] = finite_value

    threshold_raw = policy.get("threshold")
    threshold: float | None = None
    if isinstance(threshold_raw, (int, float)) and not isinstance(
        threshold_raw, bool
    ):
        with contextlib.suppress(OverflowError):
            finite_threshold = float(threshold_raw)
            if math.isfinite(finite_threshold):
                threshold = finite_threshold
    bounded_source = str(source or "unknown")[:64]
    hash_input = {
        "threshold": threshold,
        "weights": weights,
        "source": bounded_source,
    }
    policy_hash = hashlib.sha256(
        json.dumps(hash_input, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        **hash_input,
        "fallback_error": (
            str(fallback_error)[:_POLICY_PROJECTION_ERROR_CHARS]
            if fallback_error
            else None
        ),
        "policy_hash": policy_hash,
    }


def qualification_attempt_policy_projection(
    qualification: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Project a launch-time policy snapshot when the record actually has one.

    Older finalized records commonly retain only ``threshold``. Returning no
    attempt projection is more honest than fabricating weights or provenance for
    those rows. Pending/new finalized records carry the full frozen snapshot.
    """

    if not isinstance(qualification, dict):
        return None
    snapshot = qualification.get("promotion_policy_snapshot")
    if not isinstance(snapshot, dict) or not snapshot:
        return None
    source = qualification.get("promotion_policy_source")
    return promotion_policy_projection(
        snapshot,
        source=(str(source) if source else "legacy_snapshot"),
        fallback_error=(
            str(qualification["promotion_policy_fallback_error"])
            if qualification.get("promotion_policy_fallback_error")
            else None
        ),
    )


def qualification_attempt_policy_record_fields(
    qualification: dict[str, Any] | None,
) -> dict[str, Any]:
    """Fields that must survive every pending-to-terminal record replacement."""

    projection = qualification_attempt_policy_projection(qualification)
    if projection is None or not isinstance(qualification, dict):
        return {}
    return {
        "promotion_policy_snapshot": qualification["promotion_policy_snapshot"],
        "promotion_policy_source": projection["source"],
        "promotion_policy_fallback_error": projection["fallback_error"],
        "promotion_policy_hash": (
            qualification.get("promotion_policy_hash") or projection["policy_hash"]
        ),
    }

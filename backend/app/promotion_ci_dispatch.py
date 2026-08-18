"""Structured promotion-CI dispatch outcomes and terminal receipts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from app.time_utils import _utcnow


_TERMINAL_DISPATCH_OUTCOMES = frozenset({"rejected", "rejected_permanent_404"})
_TERMINAL_DISPATCH_RECEIPT_TTL_SECONDS = 30 * 60


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def promotion_ci_dispatch_gate_verdict(
    outcome: str,
    *,
    owner: str,
    repo: str,
    workflow_file: str,
    ref: str,
    head_sha: str,
) -> dict[str, Any]:
    """Project a raw GitHub dispatch outcome without losing retry authority."""
    common = {
        "head_sha": head_sha,
        "run_id": None,
        "html_url": None,
        "dispatch_outcome": outcome,
        "dispatch_ref": ref,
        "dispatch_workflow_file": workflow_file,
    }
    if outcome == "accepted":
        return {
            **common,
            "gate_state": "dispatched",
            "reason": (
                f"no {workflow_file} run for integration HEAD — dispatched a "
                f"fresh full-matrix run on {ref}"
            ),
            "dispatch_retryable": False,
            "dispatch_suppression_key": None,
        }
    if outcome in _TERMINAL_DISPATCH_OUTCOMES:
        suppression_key = f"{owner}/{repo}:{workflow_file}:{ref}:{head_sha}"
        detail = (
            "workflow is unavailable to this GitHub authority"
            if outcome == "rejected_permanent_404"
            else "GitHub denied this dispatch authority"
        )
        return {
            **common,
            "gate_state": "dispatch_rejected",
            "reason": (
                f"{workflow_file} dispatch was terminally rejected: {detail}; "
                "suppress the same repository/ref/HEAD tuple until retry-after"
            ),
            "dispatch_retryable": False,
            "dispatch_suppression_key": suppression_key,
        }
    return {
        **common,
        "gate_state": "unavailable",
        "reason": (
            f"{workflow_file} dispatch outcome was ambiguous; reconcile "
            "authoritative workflow runs before retrying"
        ),
        "dispatch_retryable": True,
        "dispatch_suppression_key": None,
    }


def terminal_promotion_ci_dispatch_receipt(
    verdict: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return a time-bounded receipt suppressing one authority tuple."""
    if (
        verdict.get("gate_state") != "dispatch_rejected"
        or verdict.get("dispatch_outcome") not in _TERMINAL_DISPATCH_OUTCOMES
        or verdict.get("dispatch_retryable") is not False
    ):
        return None
    required = {
        "workflow_file": verdict.get("dispatch_workflow_file"),
        "head_sha": verdict.get("head_sha"),
        "ref": verdict.get("dispatch_ref"),
        "outcome": verdict.get("dispatch_outcome"),
        "suppression_key": verdict.get("dispatch_suppression_key"),
    }
    if not all(required.values()):
        return None
    recorded_at = _utc_naive(now or _utcnow())
    retry_after = recorded_at + timedelta(
        seconds=_TERMINAL_DISPATCH_RECEIPT_TTL_SECONDS
    )
    return {
        "schema_version": 2,
        "kind": "promotion_ci_dispatch",
        **required,
        "retryable": False,
        "recorded_at": recorded_at.isoformat(),
        "retry_after": retry_after.isoformat(),
        "retry_authority": "bounded_terminal_receipt_ttl",
    }


def terminal_promotion_ci_dispatch_receipt_active(
    receipt: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Whether a v2 terminal receipt still owns same-tuple suppression."""
    if (
        receipt.get("schema_version") != 2
        or receipt.get("kind") != "promotion_ci_dispatch"
        or receipt.get("retry_authority") != "bounded_terminal_receipt_ttl"
        or receipt.get("retryable") is not False
    ):
        return False
    try:
        retry_after = _utc_naive(datetime.fromisoformat(str(receipt["retry_after"])))
    except (KeyError, TypeError, ValueError):
        return False
    return _utc_naive(now or _utcnow()) < retry_after

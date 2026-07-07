"""Write-time referential-integrity and grounding helpers for Directions.

Mirrors classify_gap_link / check_gap_id_write in problem_model.py but
enforces link validity from Direction rows back to VersionAcceptanceCriterion
rows. Direction-grounding helpers are also pure; async helpers provide the
DB-backed key sets or memo rows they need.
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

CriterionLinkClassification = Literal["valid", "dangling", "inactive"]
RetiredDirectionOutcome = Literal["landed", "success_evidence_met", "abandoned"]

_SUCCESS_REASON_MARKERS = (
    "success",
    "success_evidence",
    "success evidence",
    "evidence met",
    "already met",
    "completed",
    "delivered",
    "landed",
    "merged",
)
_SUCCESS_REASON_TOKENS = frozenset(
    {
        "complete",
        "completed",
        "delivered",
        "landed",
        "merged",
        "met",
    }
)


def classify_retired_direction_outcome(
    reason: str | None,
    *,
    prs_merged: int | None = None,
) -> RetiredDirectionOutcome:
    """Classify why a retired direction is relevant to a future create.

    Merged work is the strongest signal that the prior direction landed. When no
    merge count is available, success/evidence language in the retirement reason
    records the softer "success_evidence_met" outcome. Everything else is treated
    as abandoned/stopped work so callers do not mistake a generic retirement for
    delivered work.
    """
    if isinstance(prs_merged, int) and prs_merged > 0:
        return "landed"
    reason_l = (reason or "").strip().lower()
    if any(marker in reason_l for marker in _SUCCESS_REASON_MARKERS):
        return "success_evidence_met"
    reason_tokens = frozenset(re.findall(r"[a-z0-9]+", reason_l))
    if reason_tokens & _SUCCESS_REASON_TOKENS:
        return "success_evidence_met"
    return "abandoned"


def build_retired_direction_grounding(
    memory_entry: dict[str, Any] | None,
    retirement_memo: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return compact grounding metadata for a matched retired-memory entry.

    Legacy memory entries may lack the new ``outcome`` object; this helper
    derives a best-effort classification from the older reason/prs_merged fields
    and never raises on malformed input.
    """
    if not isinstance(memory_entry, dict):
        return None

    raw_outcome = memory_entry.get("retired_outcome")
    if not isinstance(raw_outcome, dict):
        legacy_outcome = memory_entry.get("outcome")
        raw_outcome = legacy_outcome if isinstance(legacy_outcome, dict) else None
    raw_metrics = memory_entry.get("metrics")
    if not isinstance(raw_metrics, dict):
        raw_metrics = {}
    if isinstance(raw_outcome, dict):
        classification = str(raw_outcome.get("classification") or "").strip()
        raw_outcome_metrics = raw_outcome.get("metrics")
        if isinstance(raw_outcome_metrics, dict):
            raw_metrics = {**raw_outcome_metrics, **raw_metrics}
    else:
        classification = ""

    prs_merged = _coerce_int(
        raw_metrics.get("prs_merged", memory_entry.get("prs_merged"))
    )
    cycles = _coerce_int(raw_metrics.get("cycles", memory_entry.get("cycles")))
    if classification not in {"landed", "success_evidence_met", "abandoned"}:
        classification = classify_retired_direction_outcome(
            str(memory_entry.get("reason") or ""),
            prs_merged=prs_merged,
        )

    annotation: dict[str, Any] = {
        "direction_id": str(memory_entry.get("id") or ""),
        "classification": classification,
        "metrics": {
            "prs_merged": prs_merged or 0,
            "cycles": cycles or 0,
        },
        "reason": str(memory_entry.get("reason") or ""),
    }
    gap_id = memory_entry.get("gap_id")
    if gap_id:
        annotation["gap_id"] = str(gap_id)
    if isinstance(retirement_memo, dict):
        memo_id = retirement_memo.get("id")
        if memo_id:
            annotation["retirement_memo"] = {
                "knowledge_doc_id": str(memo_id),
                "summary": str(retirement_memo.get("summary") or "")[:240],
            }
    return annotation


def _coerce_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def classify_criterion_key(
    key: str,
    active_keys: frozenset[str],
    inactive_keys: frozenset[str],
) -> CriterionLinkClassification:
    """Classify a criterion key against the live key sets for a release version.

    Returns:
        ``"valid"``    — key exists and is active.
        ``"inactive"`` — key exists but is retracted or superseded.
        ``"dangling"`` — key is absent from the version entirely.

    Pure and side-effect-free.
    """
    if not key:
        return "dangling"
    if key in active_keys:
        return "valid"
    if key in inactive_keys:
        return "inactive"
    return "dangling"


async def load_version_criterion_key_sets(
    session: AsyncSession,
    project_id: uuid.UUID,
    release_version_id: uuid.UUID,
) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(active_keys, inactive_keys)`` for a release version in one query.

    Bulk-fetches all VersionAcceptanceCriterion rows for the version and
    partitions them by status.  Callers receive immutable frozensets they can
    pass into the pure classifiers without further DB round-trips.
    """
    from app.models import VersionAcceptanceCriterion

    result = await session.execute(
        select(
            VersionAcceptanceCriterion.criterion_key,
            VersionAcceptanceCriterion.status,
        ).where(
            VersionAcceptanceCriterion.project_id == project_id,
            VersionAcceptanceCriterion.release_version_id == release_version_id,
        )
    )
    rows = result.all()
    active = frozenset(k for k, s in rows if s == "active")
    inactive = frozenset(k for k, s in rows if s != "active")
    return active, inactive


def check_criterion_key_write(
    key: str,
    active_keys: frozenset[str],
    inactive_keys: frozenset[str],
) -> tuple[str | None, list[str]]:
    """Return ``(classification_or_None, sorted_active_keys)`` for a proposed write.

    Mirrors ``check_gap_id_write``'s sentinel-tuple contract:
    - ``(None, [])`` when the key is valid or empty (callers treat empty as no-op).
    - ``(classification, active_keys_list)`` when dangling or inactive, where
      ``active_keys_list`` lists currently active keys for error surfacing.
    """
    if not key:
        return None, []
    cls = classify_criterion_key(key, active_keys, inactive_keys)
    if cls == "valid":
        return None, []
    return cls, sorted(active_keys)

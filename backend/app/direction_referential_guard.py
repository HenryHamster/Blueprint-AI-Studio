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
RetiredDirectionOutcome = Literal[
    "landed",
    "success_evidence_met",
    "stopped",
    "failed",
    "exhausted",
    "obsolete",
    "pivoted",
    "abandoned",
]

# Positive success-signal words. Matched as WHOLE tokens (word boundaries), not
# raw substrings, so a literally-negating word such as "unmerged" (token !=
# "merged") never reads as success. Multi-word markers like "evidence met" /
# "already met" reduce to the "met" token, which is included here.
_SUCCESS_REASON_TOKENS = frozenset(
    {
        "success",
        "complete",
        "completed",
        "delivered",
        "landed",
        "merged",
        "met",
    }
)
# Words that negate a nearby success token, so phrases like "success evidence
# NOT met" and "PRs UNmerged" / "un-merged" are not classified as success. "un"
# catches hyphen-split forms ("un-merged" -> ["un", "merged"]); a fused
# "unmerged" is already safe because whole-token matching never equals "merged".
_NEGATION_TOKENS = frozenset(
    {
        "not",
        "no",
        "never",
        "without",
        "failed",
        "fail",
        "unmet",
        "un",
    }
)
# Tokens on either side of a success token that are scanned for a negation.
_NEGATION_WINDOW = 3


def classify_retired_direction_outcome(
    reason: str | None,
    *,
    prs_merged: int | None = None,
    retirement_outcome: str | None = None,
) -> RetiredDirectionOutcome:
    """Classify why a retired direction is relevant to a future create.

    An explicit stored outcome is authoritative.  Merged work may establish only
    that some work landed; it never establishes that the direction's declared
    success evidence was met. Legacy entries without an explicit outcome retain
    the conservative reason/merge fallback for grounding old data.

    The success-language scan is NEGATION-AWARE: a success token only counts when
    no negating word ("not"/"no"/"un-"/"failed"/"unmet"/"without" ...) sits within
    ``_NEGATION_WINDOW`` tokens of it. This keeps a FAILED prior direction —
    "success evidence not met", "PRs unmerged" — from being reported to the
    strategist as delivered work. Ambiguous negations bias toward "abandoned",
    the safe default (never over-report success).
    """
    explicit = str(retirement_outcome or "").strip().lower()
    if explicit in {
        "success_evidence_met",
        "stopped",
        "failed",
        "exhausted",
        "obsolete",
        "pivoted",
    }:
        return explicit  # type: ignore[return-value]
    tokens = re.findall(r"[a-z0-9]+", (reason or "").lower())
    for i, tok in enumerate(tokens):
        if tok not in _SUCCESS_REASON_TOKENS:
            continue
        window = tokens[max(0, i - _NEGATION_WINDOW) : i + _NEGATION_WINDOW + 1]
        if any(w in _NEGATION_TOKENS for w in window):
            continue  # negated ("not met", "un merged") — not a success signal
        return "success_evidence_met"
    if isinstance(prs_merged, int) and prs_merged > 0:
        return "landed"
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
    if classification not in {
        "landed",
        "success_evidence_met",
        "stopped",
        "failed",
        "exhausted",
        "obsolete",
        "pivoted",
        "abandoned",
    }:
        classification = classify_retired_direction_outcome(
            str(memory_entry.get("reason") or ""),
            prs_merged=prs_merged,
            retirement_outcome=memory_entry.get("retirement_outcome"),
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

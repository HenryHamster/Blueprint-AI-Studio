"""Write-time referential-integrity guard for Direction.criterion_keys.

Mirrors classify_gap_link / check_gap_id_write in problem_model.py but
enforces link validity from Direction rows back to VersionAcceptanceCriterion
rows.  All classifiers are pure (no I/O); async helpers provide the DB-backed
key sets they need.
"""
from __future__ import annotations

import uuid
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

CriterionLinkClassification = Literal["valid", "dangling", "inactive"]


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

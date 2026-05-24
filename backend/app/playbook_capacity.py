"""Playbook capacity-management helpers.

Handles the full 'playbook size enforcement' concern: size accounting,
archive-least-useful-entry, mark-stale-review-entries, and the
maybe-archive-for-capacity entrypoint.

Extracted from playbook_learning.py; re-exported from there so existing
callers that import through playbook_learning continue to work unchanged.
"""

import json
import logging
from datetime import datetime

from app.models import _utcnow

# Total playbook size (JSON-serialised) above which the least-useful entry is
# archived before a new one is added.
_PLAYBOOK_CAPACITY_BYTES = 30 * 1024

# Cap on playbook_archive length. The archive exists for operator review of
# retired playbook entries; there is no functional reason to keep more than
# the most-recent N. Without this cap the archive grows monotonically
# (observed: 517 entries / 351 KB on a single project) and can bloat the
# design_doc past the agent payload budget. Configurable via runtime_config.
_PLAYBOOK_ARCHIVE_MAX_DEFAULT = 50

# Minimum followed_count before success rate replaces raw confirmed_count in
# archival ranking and retirement decisions.
_MIN_FOLLOWED_FOR_RATE = 3

# Review-extracted playbook entries are flagged potentially_stale once they
# exceed _REVIEW_PLAYBOOK_STALE_DAYS without being observed again.
_REVIEW_PLAYBOOK_STALE_DAYS = 60

logger = logging.getLogger(__name__)


def _playbook_size_bytes(doc: dict) -> int:
    """Return the JSON-serialised byte size of all playbook entries in *doc*."""
    playbook_subset = {k: v for k, v in doc.items() if k.startswith("playbook/")}
    return len(json.dumps(playbook_subset).encode())


def _archive_least_useful_entry(doc: dict) -> dict:
    """Move the least-useful playbook entry to ``playbook_archive`` in *doc*.

    "Least useful" is determined by a sort over:
    1. ``stale_file_refs`` flag (stale first)
    2. Effectiveness: inverse success rate (confirmed/followed) when
       ``followed_count ≥ _MIN_FOLLOWED_FOR_RATE``, otherwise inverse
       ``confirmed_count`` (least confirmed first)
    3. Entry age (oldest first, using ``updated_at``)
    4. Key name (tie-break)

    The archived entry is removed from the ``playbook/`` namespace and appended
    to the ``playbook_archive`` list (preserving it for operator review).

    Returns the mutated doc.  If there are no archiveable entries, returns doc
    unchanged.
    """
    now = _utcnow()
    candidates: list[tuple] = []
    for k, v in doc.items():
        if not k.startswith("playbook/") or not isinstance(v, dict):
            continue
        confirmed = v.get("confirmed_count", 0)
        followed = v.get("followed_count", 0)
        stale = 1 if v.get("stale_file_refs") else 0
        updated_at_str = v.get("updated_at", "")
        age_days = 0
        if updated_at_str:
            try:
                updated = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                # _utcnow() returns naive UTC; strip tzinfo so comparison is always naive
                if updated.tzinfo is not None:
                    updated = updated.replace(tzinfo=None)
                age_days = (now - updated).days
            except (ValueError, TypeError):
                pass
        # Use inverse success rate when enough observations exist so low-performing
        # entries surface before unobserved ones; fall back to raw confirmed_count.
        if followed >= _MIN_FOLLOWED_FOR_RATE:
            effectiveness = -(confirmed / followed)
        else:
            effectiveness = -confirmed
        # Sort: stale first, then least-effective, then oldest, then key
        candidates.append((stale, effectiveness, age_days, k))

    if not candidates:
        return doc

    candidates.sort(
        reverse=True
    )  # highest (stale, -confirmed, age) first — oldest first within same tier
    _, _, _, victim_key = candidates[0]
    victim_entry = dict(doc.pop(victim_key))
    victim_entry["_archived_key"] = victim_key
    victim_entry["_archived_at"] = now.isoformat()

    archive = list(doc.get("playbook_archive") or [])
    archive.append(victim_entry)
    # Trim oldest entries past the cap so the archive can't grow without
    # bound — see the payload budget rationale at _PLAYBOOK_ARCHIVE_MAX_DEFAULT.
    from app.shared_state import runtime_config as _rc  # noqa: PLC0415

    archive_max = _rc.get("playbook_archive_max", _PLAYBOOK_ARCHIVE_MAX_DEFAULT)
    if archive_max > 0 and len(archive) > archive_max:
        dropped = len(archive) - archive_max
        archive = archive[-archive_max:]
        logger.info(
            "[PLAYBOOK] Dropped %d oldest archive entries to stay within cap of %d",
            dropped,
            archive_max,
        )
    doc["playbook_archive"] = archive

    logger.info(
        "[PLAYBOOK] Archived entry '%s' to playbook_archive (capacity cleanup)",
        victim_key,
    )
    return doc


def _mark_stale_review_entries(doc: dict) -> bool:
    """Flag review-extracted entries whose ``updated_at`` exceeds
    ``_REVIEW_PLAYBOOK_STALE_DAYS`` with ``potentially_stale: True``.

    Returns True if any entry was changed.
    """
    now = _utcnow()
    changed = False
    for k, v in doc.items():
        if not k.startswith("playbook/review-") or not isinstance(v, dict):
            continue
        if v.get("source") != "review-extracted":
            continue
        updated_at_str = v.get("updated_at") or ""
        try:
            dt = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
        is_stale = (now - dt).days >= _REVIEW_PLAYBOOK_STALE_DAYS
        if is_stale and not v.get("potentially_stale"):
            entry = dict(v)
            entry["potentially_stale"] = True
            doc[k] = entry
            changed = True
    return changed


def maybe_archive_for_capacity(doc: dict, new_entry_estimate: int = 0) -> dict:
    """Archive least-useful playbook entries until the incoming write fits within capacity.

    Loops until either sufficient space is freed or nothing is evictable, so a
    single large write or an already-over-cap playbook self-heals rather than
    staying permanently unwritable. Call this *before* adding a new entry.
    *new_entry_estimate* is the approximate byte size of the entry to be added.

    Returns the (possibly mutated) doc.
    """
    while _playbook_size_bytes(doc) + new_entry_estimate > _PLAYBOOK_CAPACITY_BYTES:
        size_before = _playbook_size_bytes(doc)
        doc = _archive_least_useful_entry(doc)
        if _playbook_size_bytes(doc) >= size_before:
            break  # nothing evictable; size didn't shrink
    return doc

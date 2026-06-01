"""Playbook capacity-management helpers.

Handles the 'playbook size enforcement' concern: size accounting,
mark-stale-review-entries, and the maybe-archive-for-capacity entrypoint.

The actual eviction *selection* (``_archive_least_useful_entry`` — which entry
is archived) lives in ``app.playbook_storage`` and is re-exported here so the
storage path and the capacity path share a single source of truth and cannot
diverge. ``maybe_archive_for_capacity`` drives that shared selection in a loop.

Extracted from playbook_learning.py; re-exported from there so existing
callers that import through playbook_learning continue to work unchanged.
"""

import json
import logging
from datetime import datetime

from app.models import _utcnow

# The capacity-eviction selection (which entry is archived) is owned by
# ``app.playbook_storage`` so the storage path and capacity path share one
# implementation and can never diverge. We re-export it here unchanged so the
# many callers that ``from app.playbook_capacity import _archive_least_useful_entry``
# keep working, and ``maybe_archive_for_capacity`` below drives that same logic.
from app.playbook_storage import _archive_least_useful_entry

# Total playbook size (JSON-serialised) above which the least-useful entry is
# archived before a new one is added.
_PLAYBOOK_CAPACITY_BYTES = 30 * 1024

# Cap on playbook_archive length. The archive exists for operator review of
# retired playbook entries; there is no functional reason to keep more than
# the most-recent N. Without this cap the archive grows monotonically
# (observed: 517 entries / 351 KB on a single project) and can bloat the
# design_doc past the agent payload budget. Configurable via runtime_config.
_PLAYBOOK_ARCHIVE_MAX_DEFAULT = 50

# Review-extracted playbook entries are flagged potentially_stale once they
# exceed _REVIEW_PLAYBOOK_STALE_DAYS without being observed again.
_REVIEW_PLAYBOOK_STALE_DAYS = 60

logger = logging.getLogger(__name__)


def _playbook_size_bytes(doc: dict) -> int:
    """Return the JSON-serialised byte size of all playbook entries in *doc*."""
    playbook_subset = {k: v for k, v in doc.items() if k.startswith("playbook/")}
    return len(json.dumps(playbook_subset).encode())


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

"""Canonical playbook storage and lifecycle helpers.

This module owns persistent playbook storage policy: stale-reference scans,
pending-entry finalization, capacity eviction, archival/suppression, tombstone
management, and auto-retirement counters. ``app.playbook_learning`` re-exports
the legacy names so existing callers and tests remain compatible.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Project, Task, _utcnow

logger = logging.getLogger(__name__)


# Total playbook size (JSON-serialised) above which the least-useful entry is
# archived before a new one is added.
_PLAYBOOK_CAPACITY_BYTES = 30 * 1024

# Cap on playbook_archive length. The archive exists for operator review of
# retired playbook entries; there is no functional reason to keep more than
# the most-recent N. Without this cap the archive grows monotonically and can
# bloat the design_doc past the agent payload budget. Configurable via
# runtime_config.
_PLAYBOOK_ARCHIVE_MAX_DEFAULT = 50

# Entries older than this (in days) with fewer than _STALE_CONFIRM_THRESHOLD
# confirmed uses are deprioritised during the claim sort.
_STALE_AGE_DAYS = 30
_STALE_CONFIRM_THRESHOLD = 3

# Minimum followed_count before success rate replaces raw confirmed_count in
# archival ranking and retirement decisions.
_MIN_FOLLOWED_FOR_RATE = 3

# Auto-retirement: entries followed >= _RETIRE_MIN_FOLLOWED times with a success
# rate below _RETIRE_MAX_SUCCESS_RATE are archived with a _retirement_reason tag.
_RETIRE_MIN_FOLLOWED = 5
_RETIRE_MAX_SUCCESS_RATE = 0.20

# Time-decay retirement for auto-generated playbook entries.
_IDLE_RETIRE_DAYS = 30

# Cooldown window (days) during which a recently-archived auto-playbook entry
# cannot be re-created.
_AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS = 30

# Review-extracted playbook entries are flagged potentially_stale once they
# exceed this age without being refreshed.
_REVIEW_PLAYBOOK_STALE_DAYS = 60

# Operator-deleted keys are retained outside the archive so suppression survives
# archive rotation.
_PLAYBOOK_DELETED_KEYS_MAX = 200

_FILE_PATH_RE = re.compile(
    r"\b((?:backend|agent|admin-ui)/(?:[\w.-]+/)*[\w.-]+\.\w+)\b"
)


def _playbook_archive_max() -> int:
    from app.shared_state import runtime_config as _rc

    return _rc.get("playbook_archive_max", _PLAYBOOK_ARCHIVE_MAX_DEFAULT)


def _extract_file_paths(content: str) -> list[str]:
    """Return unique source-file paths found in *content*."""
    return list(dict.fromkeys(_FILE_PATH_RE.findall(content)))


def check_and_flag_stale_refs(doc: dict, repo_root: str) -> dict:
    """Mark playbook entries whose referenced file paths no longer exist."""
    if not repo_root or not os.path.isdir(repo_root):
        return doc

    updated = False
    for key, value in doc.items():
        if not key.startswith("playbook/") or not isinstance(value, dict):
            continue
        paths = _extract_file_paths(value.get("content", ""))
        if not paths:
            continue

        any_missing = any(
            not os.path.isfile(os.path.join(repo_root, path)) for path in paths
        )
        if any_missing and not value.get("stale_file_refs"):
            entry = dict(value)
            entry["stale_file_refs"] = True
            doc[key] = entry
            updated = True
            logger.debug(
                "[PLAYBOOK] Flagged '%s' as stale_file_refs (missing paths: %s)",
                key,
                [
                    path
                    for path in paths
                    if not os.path.isfile(os.path.join(repo_root, path))
                ],
            )
        elif not any_missing and value.get("stale_file_refs"):
            entry = dict(value)
            entry["stale_file_refs"] = False
            doc[key] = entry
            updated = True

    if updated:
        meta = dict(doc.get("_meta", {"revision": 0}))
        meta["revision"] = meta.get("revision", 0) + 1
        meta["last_updated"] = _utcnow().isoformat()
        meta["updated_by"] = "stale-ref-check"
        doc["_meta"] = meta

    return doc


def _playbook_size_bytes(doc: dict) -> int:
    """Return the JSON-serialised byte size of all playbook entries in *doc*."""
    playbook_subset = {
        key: value for key, value in doc.items() if key.startswith("playbook/")
    }
    return len(json.dumps(playbook_subset).encode())


def _append_archived_playbook_entry(
    doc: dict,
    full_key: str,
    archived_entry: dict,
    *,
    archived_at: str | None = None,
) -> int:
    """Append *archived_entry* to playbook_archive and trim to the configured cap."""
    archive = list(doc.get("playbook_archive") or [])
    archived_payload = dict(archived_entry)
    archived_payload["_archived_key"] = full_key
    archived_payload["_archived_at"] = archived_at or _utcnow().isoformat()
    archive.append(archived_payload)

    archive_max = _playbook_archive_max()
    dropped = 0
    if archive_max > 0 and len(archive) > archive_max:
        dropped = len(archive) - archive_max
        archive = archive[-archive_max:]
    doc["playbook_archive"] = archive
    return dropped


def archive_playbook_entry(
    doc: dict,
    full_key: str,
    *,
    archived_at: str | None = None,
    retirement_reason: str | None = None,
) -> bool:
    """Move an active playbook entry into playbook_archive.

    Returns ``True`` when the key existed and was archived, ``False`` otherwise.
    """
    entry = doc.get(full_key)
    if not isinstance(entry, dict):
        return False

    victim = dict(doc.pop(full_key))
    if retirement_reason is not None:
        victim["_retirement_reason"] = retirement_reason
    _append_archived_playbook_entry(doc, full_key, victim, archived_at=archived_at)
    return True


def record_playbook_tombstone(
    doc: dict,
    full_key: str,
    *,
    archived_at: str | None = None,
) -> None:
    """Record an operator-deletion tombstone and durable suppression key."""
    tombstone = {"_retirement_reason": "operator_deleted"}
    _append_archived_playbook_entry(doc, full_key, tombstone, archived_at=archived_at)

    deleted_keys = list(doc.get("playbook_deleted_keys") or [])
    if full_key not in deleted_keys:
        deleted_keys.append(full_key)
    if len(deleted_keys) > _PLAYBOOK_DELETED_KEYS_MAX:
        deleted_keys = deleted_keys[-_PLAYBOOK_DELETED_KEYS_MAX:]
    doc["playbook_deleted_keys"] = deleted_keys


async def finalize_pending_playbook_entries(
    task: Task,
    session: AsyncSession,
    *,
    succeeded: bool,
) -> int:
    """Resolve playbook entries left ``pending_source=<task.id>`` by *task*."""
    project_row = await session.execute(
        select(Project)
        .where(Project.id == task.project_id)
        .with_for_update(skip_locked=False, key_share=True)
    )
    project = project_row.scalar_one_or_none()
    if project is None or not project.design_doc:
        return 0

    doc = dict(project.design_doc)
    task_id = str(task.id)
    changed = 0
    to_delete: list[str] = []

    for key, value in list(doc.items()):
        if not key.startswith("playbook/") or not isinstance(value, dict):
            continue
        if value.get("pending_source") != task_id:
            continue
        if succeeded:
            entry = dict(value)
            entry.pop("pending_source", None)
            doc[key] = entry
        else:
            to_delete.append(key)
        changed += 1

    for key in to_delete:
        doc.pop(key, None)

    if changed:
        meta = dict(doc.get("_meta", {"revision": 0}))
        meta["revision"] = meta.get("revision", 0) + 1
        meta["last_updated"] = _utcnow().isoformat()
        meta["updated_by"] = (
            "playbook-finalize-success" if succeeded else "playbook-finalize-failure"
        )
        doc["_meta"] = meta
        project.design_doc = doc
        flag_modified(project, "design_doc")
        session.add(project)
        logger.info(
            "[PLAYBOOK] %s %d pending entr%s for terminal task %s (succeeded=%s)",
            "Finalised" if succeeded else "Removed",
            changed,
            "y" if changed == 1 else "ies",
            task.id,
            succeeded,
        )
    return changed


def _archive_least_useful_entry(doc: dict) -> dict:
    """Move the least-useful playbook entry to ``playbook_archive`` in *doc*.

    This is the single source of truth for capacity-eviction selection; both
    the storage path (``success_patterns``, ``admin_notifications``, and the
    ``playbook_learning`` re-export) and the capacity path
    (``playbook_capacity.maybe_archive_for_capacity`` — used by
    ``playbook_auto``, ``review_learning`` and ``admin_agent_ops``) route
    through here so the eviction order can never diverge again.

    "Least useful" is determined by a reverse-sort over the tuple
    ``(stale, effectiveness, age_days, key)`` — the entry with the highest
    tuple is archived first:

    1. ``stale_file_refs`` flag (stale entries first).
    2. ``effectiveness``: a *proven low-rate* entry must always rank worse
       (be archived before) an *untried* zero-count entry. To guarantee that,
       entries with enough observations (``followed_count >=
       _MIN_FOLLOWED_FOR_RATE``) use ``1.0 - success_rate`` which lives in
       ``[0, 1]`` (1 == always-fails == worst), while untried/under-observed
       entries fall back to ``-confirmed_count`` which lives in ``[-inf, 0]``
       (0 == never confirmed). Because every rate-based score is ``>= 0`` and
       every raw-count score is ``<= 0``, a measured poor performer is always
       evicted before an unobserved entry.
    3. Entry age (oldest ``updated_at`` first).
    4. Key name (deterministic tie-break).

    Returns the mutated *doc*; unchanged if there are no archiveable entries.
    """
    now = _utcnow()
    candidates: list[tuple[int, float, int, str]] = []
    for key, value in doc.items():
        if not key.startswith("playbook/") or not isinstance(value, dict):
            continue
        confirmed = value.get("confirmed_count", 0)
        followed = value.get("followed_count", 0)
        stale = 1 if value.get("stale_file_refs") else 0
        updated_at_str = value.get("updated_at", "")
        age_days = 0
        if updated_at_str:
            try:
                updated = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                if updated.tzinfo is not None:
                    updated = updated.replace(tzinfo=None)
                age_days = (now - updated).days
            except (ValueError, TypeError):
                pass
        # Rate-based entries map to [0, 1] (1 == worst, archived first); raw-count
        # entries map to [-inf, 0] (0 == untried). Reverse-sorted, this guarantees a
        # proven low-rate entry archives BEFORE an untried zero-count entry.
        if followed >= _MIN_FOLLOWED_FOR_RATE:
            effectiveness = 1.0 - (confirmed / followed)
        else:
            effectiveness = -confirmed
        candidates.append((stale, effectiveness, age_days, key))

    if not candidates:
        return doc

    candidates.sort(reverse=True)
    _, _, _, victim_key = candidates[0]
    victim = dict(doc.pop(victim_key))
    dropped = _append_archived_playbook_entry(
        doc,
        victim_key,
        victim,
        archived_at=now.isoformat(),
    )
    if dropped:
        logger.info(
            "[PLAYBOOK] Dropped %d oldest archive entries to stay within cap of %d",
            dropped,
            _playbook_archive_max(),
        )
    logger.info(
        "[PLAYBOOK] Archived entry '%s' to playbook_archive (capacity cleanup)",
        victim_key,
    )
    return doc


def _mark_stale_review_entries(doc: dict) -> bool:
    """Flag review-extracted entries whose ``updated_at`` exceeds the stale threshold."""
    now = _utcnow()
    changed = False
    for key, value in doc.items():
        if not key.startswith("playbook/review-") or not isinstance(value, dict):
            continue
        if value.get("source") != "review-extracted":
            continue
        updated_at_str = value.get("updated_at") or ""
        try:
            updated = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
            if updated.tzinfo is not None:
                updated = updated.replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
        if (now - updated).days < _REVIEW_PLAYBOOK_STALE_DAYS or value.get(
            "potentially_stale"
        ):
            continue
        entry = dict(value)
        entry["potentially_stale"] = True
        doc[key] = entry
        changed = True
    return changed


def maybe_archive_for_capacity(doc: dict, new_entry_estimate: int = 0) -> dict:
    """Archive least-useful playbook entries until the incoming write fits."""
    while _playbook_size_bytes(doc) + new_entry_estimate > _PLAYBOOK_CAPACITY_BYTES:
        size_before = _playbook_size_bytes(doc)
        doc = _archive_least_useful_entry(doc)
        if _playbook_size_bytes(doc) >= size_before:
            break
    return doc


def _get_prior_archive_entry(doc: dict, full_key: str) -> dict | None:
    """Return the most recent ``playbook_archive`` entry for *full_key*, or None."""
    for entry in reversed(list(doc.get("playbook_archive") or [])):
        if entry.get("_archived_key") == full_key:
            return entry
    return None


def _is_auto_playbook_suppressed(doc: dict, full_key: str) -> bool:
    """Return True when *full_key* must not be re-created."""
    import app.shared_state as _ss

    if full_key in (doc.get("playbook_deleted_keys") or []):
        return True

    cooldown_days: int = _ss.runtime_config.get(
        "auto_playbook_resurrection_cooldown_days",
        _AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS,
    )
    if cooldown_days <= 0:
        return False

    cutoff = _utcnow() - timedelta(days=cooldown_days)
    for entry in reversed(list(doc.get("playbook_archive") or [])):
        if entry.get("_archived_key") != full_key:
            continue
        archived_at_str = entry.get("_archived_at", "")
        if not archived_at_str:
            return True
        try:
            archived_at = datetime.fromisoformat(archived_at_str.replace("Z", "+00:00"))
            if archived_at.tzinfo is not None:
                archived_at = archived_at.replace(tzinfo=None)
        except (ValueError, TypeError):
            return True
        if archived_at >= cutoff:
            return True
    return False


def record_playbook_applied(doc: dict, key: str) -> None:
    """Increment followed_count when a playbook entry is applied to a task prompt."""
    full_key = key if key.startswith("playbook/") else f"playbook/{key}"
    entry = doc.get(full_key)
    if isinstance(entry, dict):
        updated = dict(entry)
        updated["followed_count"] = updated.get("followed_count", 0) + 1
        doc[full_key] = updated


def record_playbook_confirmed(doc: dict, key: str) -> None:
    """Increment confirmed_count when a task that used this entry succeeds."""
    full_key = key if key.startswith("playbook/") else f"playbook/{key}"
    entry = doc.get(full_key)
    if isinstance(entry, dict):
        updated = dict(entry)
        updated["confirmed_count"] = updated.get("confirmed_count", 0) + 1
        doc[full_key] = updated


def retire_ineffective_patterns(
    doc: dict,
    *,
    min_followed: int = _RETIRE_MIN_FOLLOWED,
    max_success_rate: float = _RETIRE_MAX_SUCCESS_RATE,
) -> list[str]:
    """Auto-archive low-success auto-generated playbook entries."""
    now_iso = _utcnow().isoformat()
    retired: list[str] = []

    for key in list(doc.keys()):
        entry = doc.get(key)
        if not key.startswith("playbook/") or not isinstance(entry, dict):
            continue
        if entry.get("source") != "auto-generated":
            continue
        followed = entry.get("followed_count", 0)
        if followed < min_followed:
            continue
        confirmed = entry.get("confirmed_count", 0)
        success_rate = confirmed / followed
        if success_rate >= max_success_rate:
            continue

        archive_playbook_entry(
            doc,
            key,
            archived_at=now_iso,
            retirement_reason=f"low_success_rate:{success_rate:.2f}",
        )
        retired.append(key)
        logger.info(
            "[PLAYBOOK] Retired '%s' success_rate=%.2f followed=%d confirmed=%d",
            key,
            success_rate,
            followed,
            confirmed,
        )

    return retired


def retire_idle_auto_entries(
    doc: dict,
    *,
    idle_days: int = _IDLE_RETIRE_DAYS,
) -> list[str]:
    """Auto-archive idle auto-generated playbook entries."""
    now = _utcnow()
    now_iso = now.isoformat()
    threshold = timedelta(days=idle_days)
    retired: list[str] = []

    for key in list(doc.keys()):
        entry = doc.get(key)
        if not key.startswith("playbook/") or not isinstance(entry, dict):
            continue
        if entry.get("source") != "auto-generated":
            continue

        ts_str = entry.get("last_referenced_at") or entry.get("updated_at")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
        if (now - ts) < threshold:
            continue

        archive_playbook_entry(
            doc,
            key,
            archived_at=now_iso,
            retirement_reason=f"idle_auto_entry:{idle_days}d",
        )
        retired.append(key)
        logger.info(
            "[PLAYBOOK] Retired idle auto-entry '%s' (idle >= %dd)",
            key,
            idle_days,
        )

    return retired


def count_idle_auto_retired_within(doc: dict, *, hours: int = 24) -> int:
    """Count playbook_archive entries retired by ``retire_idle_auto_entries``."""
    if not doc:
        return 0
    archive = doc.get("playbook_archive") or []
    if not archive:
        return 0
    cutoff = _utcnow() - timedelta(hours=hours)
    count = 0
    for entry in archive:
        if not isinstance(entry, dict):
            continue
        if not str(entry.get("_retirement_reason") or "").startswith("idle_auto_entry"):
            continue
        ts_str = entry.get("_archived_at")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
        if ts >= cutoff:
            count += 1
    return count


def count_low_success_retired_within(doc: dict, *, hours: int = 24) -> int:
    """Count playbook_archive entries retired by ``retire_ineffective_patterns``."""
    if not doc:
        return 0
    archive = doc.get("playbook_archive") or []
    if not archive:
        return 0
    cutoff = _utcnow() - timedelta(hours=hours)
    count = 0
    for entry in archive:
        if not isinstance(entry, dict):
            continue
        if not str(entry.get("_retirement_reason") or "").startswith(
            "low_success_rate"
        ):
            continue
        ts_str = entry.get("_archived_at")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
        if ts >= cutoff:
            count += 1
    return count

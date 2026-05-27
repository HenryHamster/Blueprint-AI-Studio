"""Playbook entry lifecycle and storage helpers.

Owns the canonical mutation paths for the playbook key namespace:
pending-source finalization, suppression and resurrection cooldown,
retirement (low success rate, idle decay), follow/confirm bookkeeping,
and stale-reference flagging.

Extracted from ``playbook_learning.py`` so the auto-playbook writer in
``playbook_auto.py`` can depend on the lifecycle subsystem directly without
lazy back-imports into the monolith.  ``playbook_learning`` re-exports the
public surface for backward compatibility with callers and tests that patch
via ``app.playbook_learning.<name>``.
"""

import logging
import os
import re
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Project, Task, _utcnow
from app.playbook_capacity import _PLAYBOOK_ARCHIVE_MAX_DEFAULT

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifecycle constants
# ---------------------------------------------------------------------------

# Entries older than this (in days) with fewer than _STALE_CONFIRM_THRESHOLD
# confirmed uses are deprioritised during the claim sort.
_STALE_AGE_DAYS = 30
_STALE_CONFIRM_THRESHOLD = 3

# Auto-retirement: entries followed ≥ _RETIRE_MIN_FOLLOWED times with a success
# rate below _RETIRE_MAX_SUCCESS_RATE are archived with a _retirement_reason tag.
_RETIRE_MIN_FOLLOWED = 5
_RETIRE_MAX_SUCCESS_RATE = 0.20

# Time-decay retirement for auto-generated playbook entries: entries whose
# last_referenced_at (or updated_at fallback) is older than _IDLE_RETIRE_DAYS
# get archived. Operator-curated entries (source != 'auto-generated') are
# EXEMPT — only entries with source == 'auto-generated' decay on idleness.
_IDLE_RETIRE_DAYS = 30

# Cooldown window (days) during which a recently-archived auto-playbook entry
# cannot be re-created.  Prevents "resurrection" of entries pruned for capacity,
# idle decay, or low success rate.  Configurable via
# runtime_config["auto_playbook_resurrection_cooldown_days"].
_AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS = 30

# Regex patterns that match source-file paths embedded in playbook entry content.
# Used to detect references to files that may have been renamed or removed.
_FILE_PATH_RE = re.compile(
    r"\b((?:backend|agent|admin-ui)/(?:[\w.-]+/)*[\w.-]+\.\w+)\b"
)


# ---------------------------------------------------------------------------
# Stale file-reference flagging
# ---------------------------------------------------------------------------


def _extract_file_paths(content: str) -> list[str]:
    """Return unique source-file paths found in *content*.

    Matches paths of the form ``backend/app/...``, ``agent/src/...``,
    ``admin-ui/src/...``, etc.
    """
    return list(dict.fromkeys(_FILE_PATH_RE.findall(content)))


def check_and_flag_stale_refs(doc: dict, repo_root: str) -> dict:
    """Scan each playbook entry in *doc* for file-path references and set
    ``stale_file_refs: True`` on entries that reference files which no longer
    exist under *repo_root*.

    Only marks an entry stale when at least one referenced path is confirmed
    missing.  Entries with no file-path references are left untouched.
    The check is conservative: if *repo_root* is not a valid directory the
    function returns *doc* unchanged.

    Returns the (possibly mutated) doc dict.
    """
    if not repo_root or not os.path.isdir(repo_root):
        return doc

    updated = False
    for k, v in doc.items():
        if not k.startswith("playbook/") or not isinstance(v, dict):
            continue
        content = v.get("content", "")
        paths = _extract_file_paths(content)
        if not paths:
            continue
        any_missing = any(not os.path.isfile(os.path.join(repo_root, p)) for p in paths)
        if any_missing and not v.get("stale_file_refs"):
            entry = dict(v)
            entry["stale_file_refs"] = True
            doc[k] = entry
            updated = True
            logger.debug(
                "[PLAYBOOK] Flagged '%s' as stale_file_refs (missing paths: %s)",
                k,
                [p for p in paths if not os.path.isfile(os.path.join(repo_root, p))],
            )
        elif not any_missing and v.get("stale_file_refs"):
            # All referenced files now exist — clear the flag
            entry = dict(v)
            entry["stale_file_refs"] = False
            doc[k] = entry
            updated = True

    if updated:
        meta = dict(doc.get("_meta", {"revision": 0}))
        meta["revision"] = meta.get("revision", 0) + 1
        meta["last_updated"] = _utcnow().isoformat()
        meta["updated_by"] = "stale-ref-check"
        doc["_meta"] = meta

    return doc


# ---------------------------------------------------------------------------
# Pending-source finalization (anti-poisoning)
# ---------------------------------------------------------------------------


async def finalize_pending_playbook_entries(
    task: Task,
    session: AsyncSession,
    *,
    succeeded: bool,
) -> int:
    """Resolve playbook entries left ``pending_source=<task.id>`` by *task*.

    Anti-poisoning: agents may call ``./playbook set`` while their task is
    still IN_PROGRESS. Those writes carry a ``pending_source`` field stamped
    by ``main.update_playbook`` so peers can't see them yet. This helper
    runs at terminal status and:

    * ``succeeded=True`` (task COMPLETED) — clears ``pending_source`` from
      every entry whose source is *task*, making them visible to peers.
    * ``succeeded=False`` (task FAILED / CANCELLED) — deletes those entries
      entirely, so a task that never landed code can't leave a lie behind
      claiming "X exists in the codebase."

    Returns the number of entries finalised (cleared or removed). No-op when
    the project has no design_doc or no matching pending entries.
    """
    _proj_row = await session.execute(
        select(Project)
        .where(Project.id == task.project_id)
        .with_for_update(skip_locked=False, key_share=True)
    )
    project = _proj_row.scalar_one_or_none()
    if project is None or not project.design_doc:
        return 0

    doc = dict(project.design_doc)
    my_id = str(task.id)
    changed = 0
    to_delete: list[str] = []

    for k, v in list(doc.items()):
        if not k.startswith("playbook/") or not isinstance(v, dict):
            continue
        if v.get("pending_source") != my_id:
            continue
        if succeeded:
            new_entry = dict(v)
            new_entry.pop("pending_source", None)
            doc[k] = new_entry
        else:
            to_delete.append(k)
        changed += 1

    for k in to_delete:
        doc.pop(k, None)

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


# ---------------------------------------------------------------------------
# Suppression and resurrection
# ---------------------------------------------------------------------------


def _get_prior_archive_entry(doc: dict, full_key: str) -> dict | None:
    """Return the most recent ``playbook_archive`` entry for *full_key*, or None."""
    for entry in reversed(list(doc.get("playbook_archive") or [])):
        if entry.get("_archived_key") == full_key:
            return entry
    return None


def _is_auto_playbook_suppressed(doc: dict, full_key: str) -> bool:
    """Return True when *full_key* must not be re-created.

    Checks (in priority order):

    1. ``playbook_deleted_keys`` — keys explicitly deleted by an operator.
       These are suppressed until they age off the 200-entry cap.
    2. ``playbook_archive`` — keys retired for capacity, idle decay, or low
       success rate whose ``_archived_at`` is within the cooldown window
       (``auto_playbook_resurrection_cooldown_days``, default 30 days).
    """
    import app.shared_state as _ss  # noqa: PLC0415

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
            return True  # Archived but no timestamp: suppress conservatively
        try:
            archived_at = datetime.fromisoformat(archived_at_str.replace("Z", "+00:00"))
            if archived_at.tzinfo is not None:
                archived_at = archived_at.replace(tzinfo=None)
        except (ValueError, TypeError):
            return True  # Can't parse: suppress conservatively
        if archived_at >= cutoff:
            return True
    return False


# ---------------------------------------------------------------------------
# Follow / confirm outcome bookkeeping
# ---------------------------------------------------------------------------


def record_playbook_applied(doc: dict, key: str) -> None:
    """Increment followed_count when a playbook entry is applied to a task prompt.

    *key* may be the short form (e.g. ``"success-create-small"``) or the full
    ``playbook/<key>`` form.  No-op if the entry is not present.
    """
    full_key = key if key.startswith("playbook/") else f"playbook/{key}"
    entry = doc.get(full_key)
    if isinstance(entry, dict):
        e = dict(entry)
        e["followed_count"] = e.get("followed_count", 0) + 1
        doc[full_key] = e


def record_playbook_confirmed(doc: dict, key: str) -> None:
    """Increment confirmed_count when a task that used this entry completes successfully.

    *key* may be the short form or the full ``playbook/<key>`` form.
    No-op if the entry is not present.
    """
    full_key = key if key.startswith("playbook/") else f"playbook/{key}"
    entry = doc.get(full_key)
    if isinstance(entry, dict):
        e = dict(entry)
        e["confirmed_count"] = e.get("confirmed_count", 0) + 1
        doc[full_key] = e


# ---------------------------------------------------------------------------
# Retirement (low success rate, idle decay) and retirement counters
# ---------------------------------------------------------------------------


def retire_ineffective_patterns(
    doc: dict,
    *,
    min_followed: int = _RETIRE_MIN_FOLLOWED,
    max_success_rate: float = _RETIRE_MAX_SUCCESS_RATE,
) -> list[str]:
    """Auto-archive playbook entries whose success rate has fallen below the threshold.

    Only considers entries with ``followed_count >= min_followed`` so the rate
    is statistically meaningful.  Archived entries receive a ``_retirement_reason``
    tag with the computed success rate for operator review.

    Returns a list of full playbook keys that were archived.
    """
    from app.shared_state import runtime_config as _rc  # noqa: PLC0415

    archive_max = _rc.get("playbook_archive_max", _PLAYBOOK_ARCHIVE_MAX_DEFAULT)
    now = _utcnow()
    retired: list[str] = []

    for k in list(doc.keys()):
        if not k.startswith("playbook/") or not isinstance(doc.get(k), dict):
            continue
        entry = doc[k]
        if entry.get("source") != "auto-generated":
            continue
        followed = entry.get("followed_count", 0)
        if followed < min_followed:
            continue
        confirmed = entry.get("confirmed_count", 0)
        success_rate = confirmed / followed
        if success_rate >= max_success_rate:
            continue

        victim = dict(doc.pop(k))
        victim["_archived_key"] = k
        victim["_archived_at"] = now.isoformat()
        victim["_retirement_reason"] = f"low_success_rate:{success_rate:.2f}"
        archive = list(doc.get("playbook_archive") or [])
        archive.append(victim)
        if archive_max > 0 and len(archive) > archive_max:
            archive = archive[-archive_max:]
        doc["playbook_archive"] = archive
        retired.append(k)
        logger.info(
            "[PLAYBOOK] Retired '%s' success_rate=%.2f followed=%d confirmed=%d",
            k,
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
    """Auto-archive auto-generated playbook entries that have not been referenced
    for at least ``idle_days``.

    Operator-curated entries (``source != 'auto-generated'``) are EXEMPT; only
    entries with ``source == 'auto-generated'`` decay on idleness.

    Reuses the existing ``last_referenced_at`` field as the freshness signal
    (it is stamped in routers/agent.py whenever a playbook entry is shown to
    a claim). Falls back to ``updated_at`` for entries that were never
    referenced, so historical entries don't all retire on the first run.
    Entries lacking both timestamps are skipped (defensive — brand-new
    entries written without an ``updated_at`` would otherwise look idle).

    Archived entries receive ``_retirement_reason='idle_auto_entry:<N>d'``
    so the digest counter can distinguish idle-decay retirements from
    low-success-rate retirements.

    Returns the list of full playbook keys that were archived.
    """
    from app.shared_state import runtime_config as _rc  # noqa: PLC0415

    archive_max = _rc.get("playbook_archive_max", _PLAYBOOK_ARCHIVE_MAX_DEFAULT)
    now = _utcnow()
    threshold = timedelta(days=idle_days)
    retired: list[str] = []

    for k in list(doc.keys()):
        if not k.startswith("playbook/"):
            continue
        entry = doc.get(k)
        if not isinstance(entry, dict):
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

        victim = dict(doc.pop(k))
        victim["_archived_key"] = k
        victim["_archived_at"] = now.isoformat()
        victim["_retirement_reason"] = f"idle_auto_entry:{idle_days}d"
        archive = list(doc.get("playbook_archive") or [])
        archive.append(victim)
        if archive_max > 0 and len(archive) > archive_max:
            archive = archive[-archive_max:]
        doc["playbook_archive"] = archive
        retired.append(k)
        logger.info(
            "[PLAYBOOK] Retired idle auto-entry '%s' (idle >= %dd)",
            k,
            idle_days,
        )

    return retired


def count_idle_auto_retired_within(doc: dict, *, hours: int = 24) -> int:
    """Count playbook_archive entries retired by ``retire_idle_auto_entries``
    within the last ``hours``. Used by admin_digest to surface accidental
    over-retirement to operators.
    """
    if not doc:
        return 0
    archive = doc.get("playbook_archive") or []
    if not archive:
        return 0
    now = _utcnow()
    cutoff = now - timedelta(hours=hours)
    count = 0
    for entry in archive:
        if not isinstance(entry, dict):
            continue
        reason = entry.get("_retirement_reason") or ""
        if not reason.startswith("idle_auto_entry"):
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
    """Count playbook_archive entries retired by ``retire_ineffective_patterns``
    within the last ``hours``. Used by admin_digest to surface auto-entry pruning.
    """
    if not doc:
        return 0
    archive = doc.get("playbook_archive") or []
    if not archive:
        return 0
    now = _utcnow()
    cutoff = now - timedelta(hours=hours)
    count = 0
    for entry in archive:
        if not isinstance(entry, dict):
            continue
        reason = entry.get("_retirement_reason") or ""
        if not reason.startswith("low_success_rate"):
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

"""Backward-compatibility shim for the playbook entry lifecycle surface.

The lifecycle helpers (pending-source finalization, suppression and
resurrection cooldown, retirement, follow/confirm bookkeeping, and
stale-reference flagging) once lived here as a parallel implementation.
They have been consolidated into their single canonical home,
``app.playbook_storage``, which is a strict superset (retirement routes
through storage-local archive helpers, so the dependency direction forbids
lifecycle owning them without an import cycle).

This module now re-exports those names by identity so existing callers and
tests that import ``app.playbook_lifecycle.<name>`` (e.g. ``playbook_auto``
and ``autonomous_research``) keep resolving to the one canonical
implementation. Do NOT redefine any of these helpers here — that would
re-introduce the dead twins this consolidation removed.
"""

from app.playbook_storage import (  # noqa: F401
    _AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS,
    _FILE_PATH_RE,
    _IDLE_RETIRE_DAYS,
    _RETIRE_MAX_SUCCESS_RATE,
    _RETIRE_MIN_FOLLOWED,
    _STALE_AGE_DAYS,
    _STALE_CONFIRM_THRESHOLD,
    _extract_file_paths,
    _get_prior_archive_entry,
    _is_auto_playbook_suppressed,
    check_and_flag_stale_refs,
    count_idle_auto_retired_within,
    count_low_success_retired_within,
    finalize_pending_playbook_entries,
    record_playbook_applied,
    record_playbook_confirmed,
    retire_idle_auto_entries,
    retire_ineffective_patterns,
)

__all__ = [
    "_AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS",
    "_FILE_PATH_RE",
    "_IDLE_RETIRE_DAYS",
    "_RETIRE_MAX_SUCCESS_RATE",
    "_RETIRE_MIN_FOLLOWED",
    "_STALE_AGE_DAYS",
    "_STALE_CONFIRM_THRESHOLD",
    "_extract_file_paths",
    "_get_prior_archive_entry",
    "_is_auto_playbook_suppressed",
    "check_and_flag_stale_refs",
    "count_idle_auto_retired_within",
    "count_low_success_retired_within",
    "finalize_pending_playbook_entries",
    "record_playbook_applied",
    "record_playbook_confirmed",
    "retire_idle_auto_entries",
    "retire_ineffective_patterns",
]

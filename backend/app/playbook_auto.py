"""Auto-generate playbook entries from repeated failure patterns.

When the same failure signature appears 3+ times in 7 days for a project,
a playbook entry is automatically created to surface the pattern to future
agents.  Entries are marked 'auto-generated' to distinguish them from
agent-written entries.

Threshold and window are configurable via runtime_config:
  auto_playbook_threshold  (default 3)
  auto_playbook_window_days (default 7)

This module owns failure-signature extraction and the
``_maybe_auto_playbook`` writer.  Entry archival/suppression helpers live in
``app.playbook_capacity`` (``maybe_archive_for_capacity``) and
``app.playbook_storage`` (``_get_prior_archive_entry``,
``_is_auto_playbook_suppressed``); both are pure leaves of the import graph,
so they are imported eagerly here.
``app.playbook_learning`` re-exports the public names from this module for
backward compatibility with callers and tests that patch via
``app.playbook_learning._maybe_auto_playbook``.
"""

import json
import logging
import re
from datetime import timedelta
from typing import Optional

from sqlalchemy import cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import resolve_dialect_name
from app.models import Task, TaskStatus, _utcnow
from app.playbook_capacity import maybe_archive_for_capacity
from app.playbook_storage import (
    _get_prior_archive_entry,
    _is_auto_playbook_suppressed,
)
from app.project_telemetry import get_project_telemetry, set_project_telemetry
from app.shared_state import DEFAULT_TASK_SCOPE
from app.task_locking import DesignDocSizeLimitExceeded, with_design_doc_locked

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signature extraction
# ---------------------------------------------------------------------------


def _extract_failure_module(error_lower: str) -> Optional[str]:
    """Return the base module name from the innermost traceback frame, if present.

    For example, given a traceback containing ``File "backend/app/auth.py"``,
    returns ``"auth"``.  Returns ``None`` when no ``File "..."`` pattern is found.
    """
    frame_matches = re.findall(r'file "([^"]+\.py)"', error_lower)
    if not frame_matches:
        return None
    innermost = frame_matches[-1]
    base = re.sub(r".*/", "", innermost)  # strip directory prefix
    return re.sub(r"\.py$", "", base) or None  # strip .py extension


def _extract_failure_signature(
    failure_class: str,
    error_summary: str,
    task_type: str = "",
    scope: str = "",
) -> str:
    """Return a short, normalised string that identifies the failure pattern.

    The goal is reproducibility: the same root cause should produce the same
    signature across different tasks so the counter in ``_maybe_auto_playbook``
    accumulates correctly.

    For Python exceptions with a traceback, the module name from the innermost
    frame is embedded in the signature to distinguish per-module failures:
    e.g. ``"importerror: auth: cannot import name foo"`` rather than
    ``"importerror: cannot import name foo"``.
    """
    error_lower = (error_summary or "").lower()

    # Timeouts are identified by (type, scope) — the error text varies too much
    if failure_class == "AGENT_TIMEOUT":
        return f"timeout-{task_type or 'unknown'}-{scope or 'medium'}"

    if failure_class == "CODE_ERROR":
        # Extract module from innermost traceback frame for module-aware signatures
        module_name = _extract_failure_module(error_lower)

        # Well-known Python exception names give stable signatures
        python_exc = re.search(
            r"(modulenotfounderror|importerror|typeerror|attributeerror|"
            r"valueerror|nameerror|keyerror|syntaxerror|assertionerror)"
            r"[:\s][^\n]{0,60}",
            error_lower,
        )
        if python_exc:
            raw = python_exc.group(0)
            # Drop file-path tokens (contain /) and line-number suffixes
            raw = re.sub(r"\S*/\S+", "", raw)
            raw = re.sub(r":\d+", ":", raw)
            clean = re.sub(r"\s+", " ", raw).strip()
            if module_name:
                # Inject module context: "importerror: auth: cannot import name foo"
                colon_idx = clean.find(":")
                if colon_idx >= 0:
                    exc_type = clean[:colon_idx].strip()
                    rest = clean[colon_idx + 1 :].strip()
                    clean = f"{exc_type}: {module_name}: {rest}"
            return clean[:80]

        # Test-runner failure lines: "FAILED backend/testing/test_foo.py"
        fail_match = re.search(r"fail(?:ed)?[\s:]+[\w/_.-]+\.py", error_lower)
        if fail_match:
            raw = fail_match.group(0)
            raw = re.sub(r"\S+/", "", raw)  # keep filename only
            return raw.strip()[:60]

    # Generic fallback: first meaningful line, stripped of paths/numbers
    for line in error_lower.split("\n"):
        line = line.strip()
        if line:
            line = re.sub(r"\S+/", "", line)  # strip path prefixes
            line = re.sub(r":\d+", ":", line)  # strip line numbers
            return line[:80]

    return f"{failure_class.lower()}-unknown"


def _signature_to_key(signature: str) -> str:
    """Convert a signature string to a valid playbook key (1-50 alphanumeric/hyphen chars)."""
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", signature.lower())
    normalized = normalized.strip("-")
    normalized = re.sub(r"-+", "-", normalized)
    if not normalized:
        normalized = "unknown"
    # Prefix with "auto" so the key always starts with an alphanumeric char
    key = ("auto-" + normalized)[:50].rstrip("-")
    return key


def _hint_for_failure_class(failure_class: str) -> str:
    """Return a short actionable hint for the given failure class."""
    return {
        "CODE_ERROR": (
            "Check imports, syntax, and test assertions. Run lint before committing."
        ),
        "AGENT_TIMEOUT": (
            "Reduce scope. Focus on one critical change. Commit working changes early."
        ),
        "PROVISION_FAILURE": (
            "Infrastructure issue. Check GCP quota and zone availability."
        ),
        "EXTERNAL_API": (
            "External service failure. "
            "May be transient — retry after checking API status."
        ),
        "RATE_LIMIT": (
            "API rate limit hit. Reduce call frequency or add exponential backoff."
        ),
    }.get(failure_class, "Check the error log for details and reduce scope.")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def _maybe_auto_playbook(
    task: Task,
    failure_class: str,
    error_summary: str,
    session: AsyncSession,
) -> Optional[str]:
    """Store failure signature on *task* and create a playbook entry if the
    pattern has been seen ``threshold`` or more times in the last ``window_days``.

    Must be called while *task* still has ``status == FAILED`` (before it is
    cancelled by the requeue logic) so that it appears in the counting query.

    Returns the short playbook key (e.g. ``"auto-modulenotfounderror-..."``
    ) if a new entry was created, ``None`` otherwise.
    """
    import app.shared_state as _ss  # noqa: PLC0415

    threshold: int = _ss.runtime_config.get("auto_playbook_threshold", 3)
    window_days: int = _ss.runtime_config.get("auto_playbook_window_days", 7)

    # ── 1. Compute & persist failure signature ──────────────────────────────
    task_type = task.first_payload.get("type", "")
    task_scope = (task.context or {}).get("scope", DEFAULT_TASK_SCOPE)
    signature = _extract_failure_signature(
        failure_class, error_summary, task_type, task_scope
    )
    # Store module context for cross-referencing with module_feedback_profiles
    module_name = (
        _extract_failure_module((error_summary or "").lower())
        if failure_class == "CODE_ERROR"
        else None
    )

    ctx = dict(task.context or {})
    ctx["_failure_signature"] = signature
    if module_name:
        ctx["_failure_module"] = module_name
    task.context = ctx
    flag_modified(task, "context")
    session.add(task)

    # ── 2. Count recent tasks with same signature (excluding current task) ──
    window_start = _utcnow() - timedelta(days=window_days)
    # Phase F3 (H4): push the ``_failure_signature`` filter into SQL via JSONB
    # containment so Postgres can use ``ix_task_context_gin`` instead of
    # seq-scanning every FAILED/CANCELLED task in the window and filtering in
    # Python. ``context @> {"_failure_signature": signature}`` is index-safe
    # on Postgres; on SQLite the JSONB op doesn't compile cleanly so we omit
    # the SQL-side filter and rely on the Python re-check below (audit
    # R3-W2 / H3). ``load_only`` drops heavy JSONBs we don't read here
    # (payloads, plan_output) but keeps ``status_log`` to avoid
    # MissingGreenlet from the Task event listeners (models.py ~360) under
    # async sessions. Limit caps the per-project scan; a project that hit
    # the cap had >200 same-signature failures in window and the playbook
    # entry already triggers well below that threshold.
    dialect_name = resolve_dialect_name(session)
    where_clauses = [
        Task.project_id == task.project_id,
        Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
        Task.created_at >= window_start,
        Task.id != task.id,
    ]
    if dialect_name != "sqlite":
        # Postgres path: GIN containment via ``ix_task_context_gin``.
        where_clauses.append(
            Task.context.op("@>")(cast({"_failure_signature": signature}, JSONB))
        )
    # SQLite path: no SQL-side filter; Python re-check below filters the
    # candidate set. The 200-row limit caps the seq-scan worst case.
    result = await session.execute(
        select(Task)
        .where(*where_clauses)
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.context,
                Task.created_at,
                Task.status_log,
            raiseload=True,)
        )
        .limit(200)
    )
    recent_tasks = result.scalars().all()

    # Containment guarantees signature match on Postgres; on SQLite this is
    # the actual filter. Either way the Python re-check is the source of
    # truth for ``matching``.
    matching = [
        t
        for t in recent_tasks
        if (t.context or {}).get("_failure_signature") == signature
    ]
    total_count = len(matching) + 1  # +1 for the current task

    if total_count < threshold:
        logger.debug(
            "[AUTO_PLAYBOOK] project=%s signature='%s' seen %d/%d — no entry yet",
            task.project_id,
            signature,
            total_count,
            threshold,
        )
        return None

    key = _signature_to_key(signature)
    playbook_full_key = f"playbook/{key}"

    # ── 4. Create auto-generated playbook entry ─────────────────────────────
    recent_ids = [str(t.id) for t in matching[-2:]] + [str(task.id)]
    hint = _hint_for_failure_class(failure_class)
    module_note = (
        f" Repeated failures in module '{module_name}'." if module_name else ""
    )
    content = (
        f"Repeated failure pattern ({total_count}x in {window_days}d): {signature}. "
        f"Recent tasks: {', '.join(recent_ids)}. "
        f"Common fix: {hint}"
        f"{module_note}"
    )

    # Infer scope: if ALL tasks in the pattern have a direction_id and they all
    # share the same one, mark as direction-scoped.  Tasks without a direction_id
    # must NOT be silently dropped — their presence means the pattern is universal.
    all_pattern_tasks = matching + [task]
    all_have_direction = all(
        (t.context or {}).get("direction_id") for t in all_pattern_tasks
    )
    direction_ids = {
        (t.context or {}).get("direction_id")
        for t in all_pattern_tasks
        if (t.context or {}).get("direction_id")
    }
    if all_have_direction and len(direction_ids) == 1:
        entry_scope: str = "direction"
        learned_from_direction: Optional[str] = next(iter(direction_ids))
    else:
        entry_scope = "universal"
        learned_from_direction = None

    # Pre-fetch archive from project_telemetry so _get_prior_archive_entry and
    # _is_auto_playbook_suppressed see the real data inside the design-doc lock
    # (strip_migrated_doc_keys already removed "playbook_archive" from the
    # persisted design_doc, so the lock-loaded doc would have an empty archive
    # without this pre-fetch).
    pre_fetched_archive = await get_project_telemetry(
        session, task.project_id, "playbook_archive"
    )

    entry_created = False
    suppression_reason: Optional[str] = None
    captured_archive: Optional[list] = None

    def _mutate_design_doc(doc: dict) -> None:
        nonlocal entry_created, suppression_reason, captured_archive

        # Inject the real archive so suppression/prior-entry helpers work.
# Prefer the project_telemetry copy; when no telemetry row exists yet
        # (pre-backfill or post-rollback) fall back to whatever the locked
        # design_doc already holds instead of clobbering it with an empty list.
        if pre_fetched_archive is not None:
            doc["playbook_archive"] = list(pre_fetched_archive)
        else:
            doc.setdefault("playbook_archive", [])

        if playbook_full_key in doc:
            suppression_reason = "exists"
            return

        prior = _get_prior_archive_entry(doc, playbook_full_key)
        if _is_auto_playbook_suppressed(doc, playbook_full_key):
            suppression_reason = "suppressed"
            return

        # Carry forward confirmed_count and retirement reason from prior archive
        # entry (if any) after the cooldown has elapsed.
        prior_confirmed: int = (prior or {}).get("confirmed_count", 0)
        prior_retirement_reason: Optional[str] = (prior or {}).get("_retirement_reason")
        now_iso = _utcnow().isoformat()
        new_entry = {
            "content": content,
            "source_task_id": str(task.id),
            "updated_at": now_iso,
            "confirmed_count": prior_confirmed,
            "source": "auto-generated",
        }
        archived_doc = maybe_archive_for_capacity(
            doc, len(json.dumps(new_entry).encode())
        )
        # Capture archive before doc.clear() wipes archived_doc (same object).
        captured_archive = list(archived_doc.get("playbook_archive") or [])
        doc.clear()
        doc.update(archived_doc)

        recreated_entry: dict = {
            "content": content,
            "source_task_id": str(task.id),
            "updated_at": now_iso,
            "confirmed_count": prior_confirmed,
            "source": "auto-generated",
            "scope": entry_scope,
            "learned_from_direction": learned_from_direction,
        }
        if prior_retirement_reason:
            recreated_entry["prior_retirement_reason"] = prior_retirement_reason
        doc[playbook_full_key] = recreated_entry

        meta = dict(doc.get("_meta", {"revision": 0}))
        meta["revision"] = meta.get("revision", 0) + 1
        meta["last_updated"] = now_iso
        meta["updated_by"] = "auto-playbook"
        doc["_meta"] = meta
        entry_created = True

    try:
        await with_design_doc_locked(session, task.project_id, _mutate_design_doc)
    except DesignDocSizeLimitExceeded as exc:
        logger.warning(
            "[AUTO_PLAYBOOK] design_doc size limit exceeded for project %s — "
            "playbook entry skipped: %s",
            task.project_id,
            exc,
        )
        return None

    if not entry_created:
        if suppression_reason == "exists":
            logger.debug(
                "[AUTO_PLAYBOOK] project=%s key='%s' already exists — skipping",
                task.project_id,
                key,
            )
        elif suppression_reason == "suppressed":
            logger.debug(
                "[AUTO_PLAYBOOK] project=%s key='%s' suppressed (pruned within cooldown) — skipping",
                task.project_id,
                key,
            )
        return None

    # Persist the archive captured inside the lock to project_telemetry so
    # strip_migrated_doc_keys (which runs in with_design_doc_locked) doesn't
    # lose any capacity-eviction entries added by maybe_archive_for_capacity.
    if captured_archive is not None:
        await set_project_telemetry(
            session, task.project_id, "playbook_archive", captured_archive
        )

    logger.info(
        "[AUTO_PLAYBOOK] project=%s created entry '%s' for pattern '%s' (seen %dx)",
        task.project_id,
        key,
        signature,
        total_count,
    )
    return key

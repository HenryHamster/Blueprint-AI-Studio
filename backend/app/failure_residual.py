"""Residual-remediation, fingerprinting, and per-account failure-log seam.

Extracted from ``failure_classification.py`` to keep that module below its
size cap. This module owns the self-contained residual subsystem:

  * the bounded in-memory residual-fingerprint store and its closed/reopen
    bookkeeping,
  * error-signature extraction and message-fingerprint normalization,
  * the ResidualRemediationCase DB upsert plumbing, and
  * the rolling per-account failure-class log with its record/get accessors.

All public and private symbols are re-exported from
``app.failure_classification`` so existing callers and tests that import them
from there keep working with an unchanged public interface.
"""

import logging
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import Task

logger = logging.getLogger(__name__)


# Bounded in-memory store for CODE_ERROR fallthrough fingerprints.
# record_residual_fingerprint() populates it at classify_failure()'s default
# branch; get_top_residual_fingerprints() is the query API for admin_digest.
_residual_store: dict[str, dict] = {}
_residual_seen_task_ids: set[str] = set()
_RESIDUAL_MAX_FINGERPRINTS = 300
_RESIDUAL_SEEN_MAX = 20_000

# Fingerprints whose ResidualRemediationCase has been closed organically.
# Updated by the reaper sweep; consulted by get_top_residual_fingerprints to
# prevent re-spawning remediation tasks for fingerprints that stopped occurring.
_residual_closed_fingerprints: set[str] = set()


def _mark_residual_case_closed(fingerprint: str) -> None:
    """Mark a fingerprint as having a closed ResidualRemediationCase."""
    _residual_closed_fingerprints.add(fingerprint)


def _reactivate_residual_case(fingerprint: str) -> None:
    """Remove fingerprint from the closed set (case reopened on new occurrence)."""
    _residual_closed_fingerprints.discard(fingerprint)


# Per-account failure class log — rolling 24h window, keyed account_id →
# list[(failure_class, confidence, timestamp)].
_account_failure_log: dict[str, list[tuple[str, float | None, float]]] = {}

# Strong references to in-flight fire-and-forget DB tasks. Without this the event
# loop only holds a weak reference and may GC the task mid-flight, silently
# dropping the upsert (RUF006). Tasks self-remove via add_done_callback.
_residual_bg_tasks: set = set()


def _spawn_bg_task(coro) -> None:
    """Fire-and-forget a coroutine on the running loop, keeping a strong ref so
    it isn't GC'd before completion. No-op if no loop is running (e.g. sync
    tests); the caller's in-memory path is then authoritative."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (e.g. sync tests); close the coroutine so it doesn't
        # surface a "coroutine was never awaited" warning. Caller's in-memory
        # path is authoritative.
        coro.close()
        return
    task = loop.create_task(coro)
    _residual_bg_tasks.add(task)
    task.add_done_callback(_residual_bg_tasks.discard)


_RE_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_RE_HEX = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
# Accept decimal memory readings ([10.5mb], [10.5MB cg:512.4MB]) — the old
# integer-only prefix regex let decimal variants slip through as distinct
# fingerprints, creating one "residual CODE_ERROR" per memory reading.
_RE_MEM_PREFIX = re.compile(
    r"^\s*\[\d+(?:\.\d+)?\s*(?:mb|gb|kb|b)"
    r"(?:\s+cg:\d+(?:\.\d+)?\s*(?:mb|gb|kb|b))?\]\s*",
    re.IGNORECASE,
)
# Generic size/duration tokens with unit adjacency — these don't hit word
# boundaries (digit adjacent to letter) so _RE_NUM can't reach them. Strip
# before numbering so "10.5mb" and "10.4mb" collapse together rather than
# leaving stray "5mb"/"4mb" differentiators.
_RE_SIZE_TOKEN = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:gib|mib|kib|gb|mb|kb|bytes?|b|ms|s)\b",
    re.IGNORECASE,
)
_RE_NUM = re.compile(r"\b\d+\b")


# Structural patterns that mark a LINE as error-shaped. Each must match
# at line-start (with optional leading whitespace) — never substring-
# anywhere — so prose like ``avoid except Exception: blocks (BLE001)``
# or JSON prompts mentioning ``Exception`` inside string values do not
# qualify. Token-bounded class-name + ``:`` or ``(`` lookahead requires
# real error-log shape, not just word mention.
#
# Single source of truth for both _extract_error_signature (pulls a line
# out of a log blob) and _looks_like_error (predicate on a single text).
# Both keep benign heartbeats (Cloning/Rebasing/Using tool/PROVISION_PROGRESS)
# out and structured-data lines (JSON objects starting with ``{``) out.
_ERROR_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Bracketed log-level prefix: [ERROR], [FATAL]
    re.compile(r"^\s*\[(error|fatal)\]", re.I),
    # ``Error:`` / ``FAIL:`` / ``FAILED:`` at line start
    re.compile(r"^\s*(error|fail(ed)?)\s*:", re.I),
    # Python tracebacks always start with this
    re.compile(r"^\s*traceback\b", re.I),
    # Filesystem / lookup negatives at line start
    re.compile(r"^\s*(no such|cannot)\s", re.I),
    # Python exception classes at line start (optionally preceded by
    # ``raise``), followed by ``:`` or ``(`` to require structural
    # error-log shape rather than just a word mention.
    re.compile(
        r"^\s*(?:raise\s+)?("
        r"AssertionError|TypeError|ValueError|KeyError|AttributeError|"
        r"NameError|IndexError|RuntimeError|ImportError|ModuleNotFoundError|"
        r"FileNotFoundError|PermissionError|ConnectionError|HTTPError|"
        r"IntegrityError|NotImplementedError|StopIteration|MemoryError|"
        r"RecursionError|SystemError|OSError|IOError|LookupError|"
        r"ArithmeticError|OverflowError|ZeroDivisionError|ReferenceError|"
        r"UnicodeError|UnicodeDecodeError|UnicodeEncodeError|EOFError|"
        r"UnboundLocalError|SyntaxError|IndentationError|EnvironmentError|"
        r"Exception"
        r")\s*[:(]",
    ),
)


def _looks_like_error(text: str) -> bool:
    """True when ``text`` is structurally shaped like an error log line.

    Strict: must match an `_ERROR_LINE_PATTERNS` pattern at line-start.
    Lines that begin with ``{`` (serialized JSON) are rejected outright
    — structured data with error keywords mentioned inside string values
    is not an error log.
    """
    if not text:
        return False
    s = text.strip()
    if not s or s[0] == "{":
        return False
    return any(p.search(s) for p in _ERROR_LINE_PATTERNS)


def _extract_error_signature(logs: str | None) -> str:
    """Pull the most fingerprint-able line out of an error log blob.

    Prefers lines matching typical error shapes (Error/Exception/Traceback/
    assertion/FAIL). Returns the first such line found, trimmed. Empty
    string when logs are missing or have no recognizable error line —
    callers must NOT record a fingerprint in that case (no fallback
    to latest_status_message: the message is usually a benign agent
    heartbeat that pollutes the residual store with success-shape
    inputs).
    """
    if not logs:
        return ""
    for line in logs.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _looks_like_error(stripped):
            return stripped[:500]
    return ""


def _fingerprint_message(msg: str) -> str:
    """Normalize a status/error message into a grouping fingerprint.

    Strips memory/size/duration tokens, UUIDs, hex hashes, and standalone
    numbers so near-duplicate messages (same error, different task IDs,
    different memory readings, different line numbers) cluster into a
    single fingerprint instead of spawning redundant remediations.
    """
    s = (msg or "").lower().strip()
    s = _RE_MEM_PREFIX.sub("", s)
    s = _RE_SIZE_TOKEN.sub("<size>", s)
    s = _RE_UUID.sub("<id>", s)
    s = _RE_HEX.sub("<hash>", s)
    s = _RE_NUM.sub("<n>", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:200]


def normalize_error_fingerprint(msg: str | None) -> str:
    """Public API: normalize an error message into a stable grouping fingerprint.

    Delegates to ``_fingerprint_message`` so all fingerprint consumers —
    residual store, fix-loop telemetry, regression dispatcher — produce
    identical keys from the same input.
    """
    return _fingerprint_message(msg or "")


def record_residual_fingerprint(task: "Task", logs: str | None = None) -> None:
    """Record a CODE_ERROR fallthrough in the residual store.

    Two strict gates keep the store free of false positives that pollute
    auto-remediation spawn decisions:

    1. ``task.status == FAILED``. ``classify_failure`` is called from
       ~20 sites — analytics, anomaly detection, direction outcomes,
       admin API responses — many of which run on COMPLETED/CANCELLED
       tasks for read-only classification. Recording fingerprints on
       non-FAILED tasks pollutes the store with success-shape strings
       (rollup messages, plan output JSON, review approvals).

    2. ``logs`` must contain an error-shaped line (Traceback / Error: /
       Python exception class / [ERROR] / FAIL: / no such / cannot).
       No fallback to ``latest_status_message`` — that's almost always
       a benign heartbeat (``[10.5MB] Cloning…``, ``Using tool: Bash``)
       that just happened to be current when classification ran.

    The _residual_seen_task_ids dedup set prevents double-counting when
    classify_failure_taxonomy() calls classify_failure() internally.
    """
    # Gate 1: only FAILED tasks contribute residual fingerprints.
    from app.models import TaskStatus

    task_status = getattr(task, "status", None)
    if task_status != TaskStatus.FAILED:
        return
    task_id = str(getattr(task, "id", "") or "")
    if not task_id or task_id in _residual_seen_task_ids:
        return
    # Gate 2: must have an error-shaped line in logs. No status_msg fallback.
    err_line = _extract_error_signature(logs)
    if not err_line:
        return
    # Both gates passed — claim the dedup slot now.
    if len(_residual_seen_task_ids) < _RESIDUAL_SEEN_MAX:
        _residual_seen_task_ids.add(task_id)
    status_msg = task.latest_status_message or ""
    fingerprint_source = err_line
    # Always error-shaped at this point (Gate 2 guarantees err_line is
    # non-empty + structurally matched). Field retained for backward
    # compat with autonomous_research's threshold gate; it now no-ops.
    is_error_shaped = True
    fp = _fingerprint_message(fingerprint_source) or "<empty>"
    now = time.time()
    trimmed_sample = fingerprint_source[:500]
    trimmed_status = status_msg[:500]
    # If this fingerprint had a closed case, reactivate it — the error is back.
    _reactivate_residual_case(fp)

    if fp in _residual_store:
        entry = _residual_store[fp]
        entry["count"] += 1
        entry["last_seen"] = now
        # Sticky-true: a cluster counts as error-shaped if any
        # contribution had error-shaped logs/status. Once true, keep
        # true so later heartbeat-only contributions don't downgrade
        # the cluster's threshold.
        if is_error_shaped:
            entry["is_error_shaped"] = True
        if task_id not in entry["task_ids"] and len(entry["task_ids"]) < 5:
            entry["task_ids"].append(task_id)
        # Accumulate distinct samples across tasks that share this
        # fingerprint. The fingerprint is a normalized clustering key —
        # individual task error lines can still vary (line numbers,
        # param values, adjacent context) and each variation is useful
        # signal for the remediation agent. Cap at 5 distinct entries.
        samples = entry.setdefault("sample_messages", [entry.get("sample_message", "")])
        if trimmed_sample and trimmed_sample not in samples and len(samples) < 5:
            samples.append(trimmed_sample)
        statuses = entry.setdefault("sample_statuses", [entry.get("sample_status", "")])
        if trimmed_status and trimmed_status not in statuses and len(statuses) < 5:
            statuses.append(trimmed_status)
    else:
        if len(_residual_store) >= _RESIDUAL_MAX_FINGERPRINTS:
            oldest = min(_residual_store, key=lambda k: _residual_store[k]["last_seen"])
            del _residual_store[oldest]
        _residual_store[fp] = {
            "count": 1,
            "first_seen": now,
            "last_seen": now,
            "is_error_shaped": is_error_shaped,
            # First/primary samples — kept for backward-compat with code
            # that reads `sample_message`/`sample_status`.
            "sample_message": trimmed_sample,
            "sample_status": trimmed_status,
            # Distinct-sample lists (up to 5 each) built as additional
            # tasks hit this fingerprint. Richer context for remediation.
            "sample_messages": [trimmed_sample] if trimmed_sample else [],
            "sample_statuses": [trimmed_status] if trimmed_status else [],
            "task_ids": [task_id],
        }

    # Persist to DB for durability across restarts.
    project_id = getattr(task, "project_id", None)
    if project_id is not None:
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        _schedule_residual_db_upsert(
            project_id, fp, now_dt, trimmed_sample or trimmed_status
        )


async def _upsert_residual_case_async(
    project_id,
    fingerprint: str,
    now_dt: datetime,
    sample_message: str,
) -> None:
    """Upsert a ResidualRemediationCase row for durability across restarts.

    - New fingerprint → INSERT ACTIVE row, hit_count=1.
    - Existing ACTIVE / CLOSED_ORGANICALLY → bump hit_count + last_seen_at;
      reopen CLOSED_ORGANICALLY to ACTIVE.
    - IGNORED / RESOLVED → bump hit_count + last_seen_at; leave status alone.
    IntegrityError (concurrent INSERT race) is handled by retrying as an UPDATE.
    """
    from app.database import AsyncSessionLocal
    from app.models import ResidualRemediationCase, ResidualCaseStatus
    from sqlalchemy import update as sa_update
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import select

    async def _atomic_increment(session, row_id, reopen: bool) -> None:
        """Increment hit_count atomically at the SQL layer (no read-modify-write)."""
        vals: dict = {
            "hit_count": ResidualRemediationCase.hit_count + 1,
            "last_seen_at": now_dt,
            "updated_at": now_dt,
        }
        if reopen:
            vals["status"] = ResidualCaseStatus.ACTIVE
        await session.execute(
            sa_update(ResidualRemediationCase)
            .where(ResidualRemediationCase.id == row_id)
            .values(**vals)
        )

    async def _apply_update(session) -> bool:
        """Return True if the row was processed; False if it was held by another worker.

        Phase A1: skip_locked=True so a contended row doesn't queue behind the
        30s lock_timeout. The next residual hit (or the in-memory cache flush
        on next failure) re-tries the upsert — losing one increment is far
        cheaper than a 500-quarantined orchestrator instance.
        """
        # SELECT FOR UPDATE acquires a row lock on Postgres, preventing
        # concurrent transactions from racing on hit_count.
        stmt = (
            select(ResidualRemediationCase)
            .where(
                ResidualRemediationCase.project_id == project_id,
                ResidualRemediationCase.fingerprint == fingerprint,
            )
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        existing = result.scalars().first()
        # If the SELECT returned nothing, distinguish "row doesn't exist" from
        # "row held under skip_locked" with a non-locking probe.
        if existing is None:
            probe = (
                await session.execute(
                    select(ResidualRemediationCase.id).where(
                        ResidualRemediationCase.project_id == project_id,
                        ResidualRemediationCase.fingerprint == fingerprint,
                    )
                )
            ).first()
            if probe is not None:
                # Row exists but is held — skip this upsert; next hit will retry.
                logger.info(
                    "[FAILURE_CLASS] residual upsert: row %r held by peer; skipping",
                    fingerprint,
                )
                return False
            # Genuinely missing — INSERT.
            session.add(
                ResidualRemediationCase(
                    project_id=project_id,
                    fingerprint=fingerprint,
                    status=ResidualCaseStatus.ACTIVE,
                    first_seen_at=now_dt,
                    last_seen_at=now_dt,
                    hit_count=1,
                    sample_message=sample_message or None,
                )
            )
            return True
        await _atomic_increment(
            session,
            existing.id,
            reopen=existing.status == ResidualCaseStatus.CLOSED_ORGANICALLY,
        )
        return True

    try:
        async with AsyncSessionLocal() as session:
            try:
                async with session.begin():
                    await _apply_update(session)
            except IntegrityError:
                # Concurrent INSERT won the race; retry as a pure atomic UPDATE.
                async with session.begin():
                    stmt = (
                        select(ResidualRemediationCase)
                        .where(
                            ResidualRemediationCase.project_id == project_id,
                            ResidualRemediationCase.fingerprint == fingerprint,
                        )
                        .with_for_update(skip_locked=True)
                    )
                    existing = (await session.execute(stmt)).scalars().first()
                    if existing is not None:
                        await _atomic_increment(
                            session,
                            existing.id,
                            reopen=existing.status
                            == ResidualCaseStatus.CLOSED_ORGANICALLY,
                        )
                    # If still None here, the contending writer is mid-INSERT;
                    # leave it — they'll commit and our next hit will see it.
    except Exception:
        logger.exception(
            "[FAILURE_CLASS] Residual case DB upsert failed (fingerprint=%r)",
            fingerprint,
        )


def _schedule_residual_db_upsert(
    project_id,
    fingerprint: str,
    now_dt: datetime,
    sample_message: str,
) -> None:
    """Schedule a fire-and-forget DB upsert when an event loop is running."""
    _spawn_bg_task(
        _upsert_residual_case_async(project_id, fingerprint, now_dt, sample_message)
    )


def get_top_residual_fingerprints(
    window_seconds: int = 86400, top_n: int = 10
) -> list[dict]:
    """Return the top-N residual CODE_ERROR fingerprints within the time window.

    Each result dict contains: fingerprint, count, last_seen (epoch float),
    sample_message (error-line or status fallback), sample_status (original
    agent heartbeat at failure time — kept as human context even though it
    doesn't drive the fingerprint), task_ids (up to 5 examples).
    """
    cutoff = time.time() - window_seconds
    results = [
        {
            "fingerprint": fp,
            "count": entry["count"],
            "last_seen": entry["last_seen"],
            "is_error_shaped": entry.get("is_error_shaped", True),
            "sample_message": entry["sample_message"],
            "sample_status": entry.get("sample_status", ""),
            "sample_messages": entry.get("sample_messages", [entry["sample_message"]]),
            "sample_statuses": entry.get(
                "sample_statuses", [entry.get("sample_status", "")]
            ),
            "task_ids": entry["task_ids"],
        }
        for fp, entry in _residual_store.items()
        if entry["last_seen"] >= cutoff and fp not in _residual_closed_fingerprints
    ]
    results.sort(key=lambda x: x["count"], reverse=True)
    return results[:top_n]


def record_account_failure_class(
    account_id: str,
    failure_class: str,
    confidence: float | None = None,
) -> None:
    """Record a failure-class event for an account in the rolling 24h window."""
    if not account_id:
        return
    now = time.time()
    cutoff = now - 86400
    records = _account_failure_log.setdefault(account_id, [])
    records.append((failure_class, confidence, now))
    _account_failure_log[account_id] = [rec for rec in records if rec[2] >= cutoff]


def get_account_failure_class_records(
    account_id: str,
    window_seconds: int = 86400,
) -> list[tuple[str, float | None, float]]:
    """Return raw per-failure-class records (class, confidence, timestamp)."""
    cutoff = time.time() - window_seconds
    records = _account_failure_log.get(account_id, [])
    return [rec for rec in records if rec[2] >= cutoff]


def get_account_failure_class_counts(
    account_id: str, window_seconds: int = 86400
) -> dict[str, int]:
    """Return per-failure-class counts for an account within the time window."""
    from app.failure_classification import ALL_FAILURE_CLASSES

    records = get_account_failure_class_records(
        account_id, window_seconds=window_seconds
    )
    return {
        cls: sum(
            1 for failure_class, _confidence, _ts in records if failure_class == cls
        )
        for cls in ALL_FAILURE_CLASSES
    }

"""Pure finding-deduplication helpers used by research completion.

Extracted from ``app.research_completion._handle_research_completion`` so the
dedup logic can be independently unit-tested. These helpers do not touch the
database session — callers supply already-loaded task lists and artifact files.

Three strategies are applied, in order:
  1. Hash dedup (``finding_hash`` collides with a recent task that is either
     already merged, still running, a suppressed cancellation, or a "real"
     failure within cooldown).
  2. Word-overlap title matching against currently active tasks, with a
     direction-aware threshold (same-direction 4 words, cross-direction 6).
  3. File-overlap: skip findings whose ``related_files`` are fully covered by
     open PR artifacts.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from app.failure_classification import INFRA_CLASSES, classify_failure
from app.models import Task, TaskStatus
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

CODE_ERROR_COOLDOWN = timedelta(hours=48)

_STOPWORDS = frozenset({
    'the', 'a', 'an', 'to', 'for', 'and', 'or', 'in', 'of', 'is', 'it',
    'this', 'that', 'with', 'by', 'we', 'should', 'be', 'are', 'on', 'as',
    'feat', 'fix', 'refactor', 'add', 'update', 'improve',
})

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _task_context(task: Task) -> dict:
    ctx = task.context or {}
    return ctx if isinstance(ctx, dict) else {}


def _task_finding_hash(task: Task) -> str | None:
    value = _task_context(task).get("finding_hash")
    return str(value) if value else None


def _coerce_datetime_for_compare(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=timezone.utc)
    if value.tzinfo is not None and reference.tzinfo is None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _within_code_error_cooldown(task: Task, now: datetime) -> bool:
    """Return true while a CODE_ERROR task should still block respawn."""
    if task.created_at is None:
        return True
    created_at = _coerce_datetime_for_compare(task.created_at, now)
    return created_at + CODE_ERROR_COOLDOWN > now


def finding_hash(title: str, description: str) -> str:
    """Stable 16-char hash used to dedup findings across research cycles."""
    return hashlib.sha256((title + description).encode()).hexdigest()[:16]


def is_duplicate(
    finding_title: str,
    finding_dir_id: Optional[str],
    existing: Iterable[tuple],
) -> bool:
    """Word-overlap check with direction-aware threshold.

    Cross-direction: require 6+ word overlap. Independent strategic tracks
    rarely produce true duplicates, so a higher threshold avoids false
    positives. If either direction is unknown (None), treat as same-direction
    (safer default).
    """
    words = set(finding_title.lower().split()) - _STOPWORDS
    for task_dir_id, title in existing:
        overlap = words & (set(title.lower().split()) - _STOPWORDS)
        same_direction = (
            not finding_dir_id
            or not task_dir_id
            or finding_dir_id == task_dir_id
        )
        threshold = 4 if same_direction else 6
        if len(overlap) >= threshold:
            return True
    return False


def build_dedup_state(
    recent_tasks: Iterable[Task],
    failed_parent_ids: set,
    now: Optional[datetime] = None,
) -> tuple[set[str], dict[str, object], dict[str, Task]]:
    """Build hash-dedup state from a list of recent tasks.

    Returns ``(recent_finding_hashes, failed_hashes, prior_failure_by_hash)``:
      * ``recent_finding_hashes``: set of finding_hash values that should block
        a new spawn. Excludes ghost merges, infra failures, and CODE_ERROR
        failures older than the 48h cooldown.
      * ``failed_hashes``: map of finding_hash -> task status (or
        ``"suppressed"`` sentinel) for nicer log messages.
      * ``prior_failure_by_hash``: map of finding_hash -> most-recent
        FAILED/CANCELLED task, used to inject retry context when a re-spawned
        finding passes dedup after the cooldown.
    """
    now = now or _utcnow()
    recent_tasks = list(recent_tasks)

    recent_finding_hashes: set[str] = set()
    failed_hashes: dict[str, object] = {}
    for t in recent_tasks:
        ctx = _task_context(t)
        fhash = _task_finding_hash(t)
        if not fhash:
            continue
        if t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            failed_hashes[fhash] = t.status
        # Exclude COMPLETED without pr_merged (code was never shipped).
        if t.status == TaskStatus.COMPLETED and not ctx.get("pr_merged"):
            continue
        # Exclude ghost merges: pr_merged=True but rollup parent failed (code
        # never reached main).
        if (
            t.status == TaskStatus.COMPLETED
            and ctx.get("pr_merged")
            and t.parent_task_id in failed_parent_ids
        ):
            continue
        if t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            failure_class = classify_failure(t)
            # Exclude transient infra failures (task never ran, should be retried).
            if failure_class in INFRA_CLASSES:
                continue
            # Exclude CODE_ERROR failures after their task-specific cooldown has
            # elapsed. This is equivalent in intent to "created_at+48h", but is
            # explicit and handles aware/naive timestamps consistently.
            if failure_class == "CODE_ERROR" and not _within_code_error_cooldown(t, now):
                continue
        recent_finding_hashes.add(fhash)

    prior_failure_by_hash: dict[str, Task] = {}
    for pt in recent_tasks:
        if not pt.context:
            continue
        pfh = _task_finding_hash(pt)
        if not pfh:
            continue
        if pt.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            continue
        existing = prior_failure_by_hash.get(pfh)
        if existing is None or (pt.created_at or now) > (existing.created_at or now):
            prior_failure_by_hash[pfh] = pt

    # Also suppress tasks that have an active suppress_until field (set on
    # user-cancelled low-priority tasks).
    now_iso = now.isoformat()
    for t in recent_tasks:
        ctx = _task_context(t)
        suppress_until = ctx.get("suppress_until")
        if suppress_until and str(suppress_until) > now_iso:
            fhash = _task_finding_hash(t)
            if fhash:
                recent_finding_hashes.add(fhash)
                failed_hashes.setdefault(fhash, "suppressed")

    return recent_finding_hashes, failed_hashes, prior_failure_by_hash


@dataclass
class DedupOutcome:
    """Result of running the three-stage dedup pipeline over findings."""

    deduplicated: list[tuple[int, dict, str]] = field(default_factory=list)
    hash_dedup_count: int = 0
    word_overlap_count: int = 0
    file_overlap_count: int = 0
    # Per-skip reason records: {title, direction_id, reason} for each filtered
    # finding, so the research agent can be told exactly what was rejected and
    # why on its next cycle. Reasons are short literals (≤80 chars).
    filtered: list[dict] = field(default_factory=list)


def deduplicate_findings(
    findings: list[dict],
    task_context: Optional[dict],
    recent_finding_hashes: set[str],
    failed_hashes: dict[str, object],
    existing_tasks_with_dirs: list[tuple],
    open_pr_files: set[str],
) -> DedupOutcome:
    """Apply hash, word-overlap, and file-overlap dedup to ``findings``.

    Preserves priority-first ordering (high → medium → low) with stable order
    inside each tier. Returns a :class:`DedupOutcome` carrying the surviving
    ``(orig_index, finding, finding_hash)`` triples plus per-stage counts.
    Logs skip reasons at the same levels and with the same wording as the
    original inline implementation.
    """
    indexed_findings = list(enumerate(findings))
    indexed_findings.sort(
        key=lambda x: _PRIORITY_ORDER.get(x[1].get("priority", "medium"), 1)
    )

    outcome = DedupOutcome()
    parent_dir_id = (task_context or {}).get("direction_id")

    for orig_i, finding in indexed_findings:
        title = finding.get("title", f"Research finding #{orig_i + 1}")
        description = finding.get("description", finding.get("prompt", ""))
        fhash = finding_hash(title, description)
        finding_dir_id = finding.get("direction_id") or parent_dir_id

        if fhash in recent_finding_hashes:
            out = failed_hashes.get(fhash, "prior")
            logger.info(
                f"[RESEARCH] Skipping finding '{title}' — hash {fhash} matched {out} task"
            )
            outcome.hash_dedup_count += 1
            outcome.filtered.append({
                "title": title[:120],
                "direction_id": finding_dir_id,
                "reason": "hash match with prior task"[:80],
            })
            continue

        matched_title = _find_overlap_match(title, finding_dir_id, existing_tasks_with_dirs)
        if matched_title is not None:
            logger.warning(
                f"[RESEARCH] Skipping duplicate finding '{title}' — matches existing task"
            )
            outcome.word_overlap_count += 1
            reason = f"word overlap with: {matched_title[:60]}"
            outcome.filtered.append({
                "title": title[:120],
                "direction_id": finding_dir_id,
                "reason": reason[:80],
            })
            continue

        finding_files = set(finding.get('related_files', []))
        if finding_files and finding_files.issubset(open_pr_files):
            logger.info(
                f"[RESEARCH] Skipping finding '{title}' — all related files already have open PRs"
            )
            outcome.file_overlap_count += 1
            outcome.filtered.append({
                "title": title[:120],
                "direction_id": finding_dir_id,
                "reason": "all files covered by open PR"[:80],
            })
            continue

        outcome.deduplicated.append((orig_i, finding, fhash))

    return outcome


def _find_overlap_match(
    finding_title: str,
    finding_dir_id: Optional[str],
    existing: Iterable[tuple],
) -> Optional[str]:
    """Return the first existing title that word-overlaps ``finding_title``.

    Uses the same threshold rules as :func:`is_duplicate` (4 words same or
    unknown direction, 6 words cross-direction). Returns ``None`` when no
    match is found, so callers can build a reason string with the matched
    title.
    """
    words = set(finding_title.lower().split()) - _STOPWORDS
    for task_dir_id, title in existing:
        overlap = words & (set(title.lower().split()) - _STOPWORDS)
        same_direction = (
            not finding_dir_id
            or not task_dir_id
            or finding_dir_id == task_dir_id
        )
        threshold = 4 if same_direction else 6
        if len(overlap) >= threshold:
            return title
    return None

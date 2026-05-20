"""Pure finding-deduplication helpers used by research completion.

Extracted from ``app.research_completion._handle_research_completion`` so the
dedup logic can be independently unit-tested. These helpers do not touch the
database session — callers supply already-loaded task lists and artifact files.

Four strategies are applied, in order:
  1. Hash dedup (``finding_hash`` collides with a recent task that is either
     already merged, still running, a suppressed cancellation, or a "real"
     failure within cooldown).
  2. Word-overlap title matching against currently active tasks, with a
     direction-aware threshold (same-direction 4 words, cross-direction 6).
  3. File-overlap: skip findings whose ``related_files`` are fully covered by
     open PR artifacts.
  4. Evidence-overlap: skip findings whose source refs (≥80% Jaccard overlap)
     and claims (at least one shared) match recent findings.
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

_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "to",
        "for",
        "and",
        "or",
        "in",
        "of",
        "is",
        "it",
        "this",
        "that",
        "with",
        "by",
        "we",
        "should",
        "be",
        "are",
        "on",
        "as",
        "feat",
        "fix",
        "refactor",
        "add",
        "update",
        "improve",
    }
)

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
            not finding_dir_id or not task_dir_id or finding_dir_id == task_dir_id
        )
        threshold = 4 if same_direction else 6
        if len(overlap) >= threshold:
            return True
    return False


def build_dedup_state(
    recent_tasks: Iterable[Task],
    failed_parent_ids: set,
    now: Optional[datetime] = None,
    previously_filtered_hashes: Optional[set[str]] = None,
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
            if failure_class == "CODE_ERROR" and not _within_code_error_cooldown(
                t, now
            ):
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

    # Fold in hashes filtered in a previous cycle so they stay suppressed.
    for fhash in previously_filtered_hashes or set():
        recent_finding_hashes.add(fhash)
        failed_hashes.setdefault(fhash, "filtered-last-cycle")

    return recent_finding_hashes, failed_hashes, prior_failure_by_hash


def extract_retired_failed_approaches(retired_context: list[dict]) -> list[str]:
    """Extract failed approach strings from retired direction context dicts.

    Each dict may have 'failed_approaches' (list[str]) and/or
    'recently_completed_finding_titles' (list[str]). Both are used as
    suppression signals — the system already explored them.
    """
    approaches: list[str] = []
    for ctx in retired_context or []:
        for approach in ctx.get("failed_approaches", []):
            if isinstance(approach, str) and approach.strip():
                approaches.append(approach)
        for title in ctx.get("recently_completed_finding_titles", []):
            if isinstance(title, str) and title.strip():
                approaches.append(title)
    return approaches


def extract_archived_diagnoses(design_doc: dict) -> list[str]:
    """Extract diagnosis strings from retired_diagnoses_archive.

    Each archived diagnosis is a summary of why a direction was retired, capturing
    evicted findings counts and failed approach titles. Used for deduplication to
    avoid re-exploring approaches the system has already determined don't work.
    """
    diagnoses: list[str] = []
    for entry in (design_doc or {}).get("retired_diagnoses_archive", []):
        diagnosis = entry.get("diagnosis", "")
        if isinstance(diagnosis, str) and diagnosis.strip():
            diagnoses.append(diagnosis)
    return diagnoses


def _find_retired_approach_match(
    finding_title: str,
    retired_failed_approaches: list[str],
) -> Optional[str]:
    """Return the first retired approach whose words overlap finding_title.

    Uses a lower threshold (3 words) than active-task matching (4/6) because
    the system has already concluded these approaches don't work.
    """
    words = set(finding_title.lower().split()) - _STOPWORDS
    for approach in retired_failed_approaches:
        overlap = words & (set(approach.lower().split()) - _STOPWORDS)
        if len(overlap) >= 3:
            return approach[:60]
    return None


def _find_archived_diagnosis_match(
    finding_title: str,
    archived_diagnoses: list[str],
) -> Optional[str]:
    """Return the first archived diagnosis whose content overlaps finding_title.

    Uses the same 3-word threshold as retired approaches because diagnoses
    represent previously explored territory.
    """
    words = set(finding_title.lower().split()) - _STOPWORDS
    for diagnosis in archived_diagnoses:
        overlap = words & (set(diagnosis.lower().split()) - _STOPWORDS)
        if len(overlap) >= 3:
            return diagnosis[:80]
    return None


def _normalize_source_ref(ref: str) -> str:
    """Normalize a source ref by stripping line numbers and line ranges.

    Handles formats like:
      - path/to/file.py
      - path/to/file.py:42
      - path/to/file.py#L10-L20
    Returns the base file path only.
    """
    if not ref:
        return ""
    # Strip line numbers (e.g., :42) and line ranges (e.g., #L10-L20)
    ref = ref.split(":")[0].split("#")[0]
    return ref.strip()


def _extract_source_refs(sources: list) -> set[str]:
    """Extract and normalize refs from a sources list.

    Sources should be a list of dicts with a 'ref' field.
    Returns a set of normalized file paths.
    """
    refs = set()
    if not isinstance(sources, list):
        return refs
    for source in sources:
        if isinstance(source, dict) and source.get("ref"):
            normalized = _normalize_source_ref(source["ref"])
            if normalized:
                refs.add(normalized)
    return refs


def _normalize_claim(claim: str) -> str:
    """Normalize a claim by lowercasing and removing extra whitespace."""
    if not isinstance(claim, str):
        return ""
    return " ".join(claim.lower().split())


def _extract_normalized_claims(claims: list) -> set[str]:
    """Extract and normalize claims from a claims list.

    Returns a set of normalized claim strings for comparison.
    """
    normalized = set()
    if not isinstance(claims, list):
        return normalized
    for claim in claims:
        norm = _normalize_claim(claim)
        if norm:
            normalized.add(norm)
    return normalized


def _jaccard_overlap(set_a: set, set_b: set) -> float:
    """Calculate Jaccard similarity between two sets.

    Returns a value between 0 and 1. Returns 0 if both sets are empty.
    """
    if not set_a and not set_b:
        return 0.0
    union = set_a | set_b
    if not union:
        return 0.0
    intersection = set_a & set_b
    return len(intersection) / len(union)


def _check_evidence_overlap(
    candidate_sources: list,
    candidate_claims: list,
    existing_findings: list[tuple],
    threshold: float = 0.8,
) -> tuple[bool, str]:
    """Check if candidate finding has evidence overlap with existing findings.

    Returns (is_duplicate, matched_title) where:
      - is_duplicate: True if source-ref Jaccard overlap >= threshold AND
        at least one claim is shared
      - matched_title: title of the first matching finding (empty string if no match)

    existing_findings should be a list of (title, sources, claims) tuples.
    """
    candidate_refs = _extract_source_refs(candidate_sources)
    candidate_claims_norm = _extract_normalized_claims(candidate_claims)

    # If candidate has no sources or claims, no evidence overlap can occur
    if not candidate_refs or not candidate_claims_norm:
        return False, ""

    for title, sources, claims in existing_findings:
        existing_refs = _extract_source_refs(sources)
        existing_claims = _extract_normalized_claims(claims)

        # Both must have sources and claims for evidence overlap check
        if not existing_refs or not existing_claims:
            continue

        # Check Jaccard overlap on source refs
        overlap = _jaccard_overlap(candidate_refs, existing_refs)
        if overlap < threshold:
            continue

        # Check if at least one claim is shared
        shared_claims = candidate_claims_norm & existing_claims
        if shared_claims:
            return True, title

    return False, ""


@dataclass
class DedupOutcome:
    """Result of running the four-stage dedup pipeline over findings."""

    deduplicated: list[tuple[int, dict, str]] = field(default_factory=list)
    hash_dedup_count: int = 0
    word_overlap_count: int = 0
    file_overlap_count: int = 0
    evidence_overlap_count: int = 0
    retired_approach_count: int = 0
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
    retired_failed_approaches: Optional[list[str]] = None,
    archived_diagnoses: Optional[list[str]] = None,
) -> DedupOutcome:
    """Apply hash, word-overlap, file-overlap, and evidence-overlap dedup to ``findings``.

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
    # Build evidence list from already-deduped findings for evidence-overlap check
    evidence_list: list[tuple[str, list, list]] = []

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
            outcome.filtered.append(
                {
                    "title": title[:120],
                    "direction_id": finding_dir_id,
                    "reason": "hash match with prior task"[:80],
                }
            )
            continue

        matched_title = _find_overlap_match(
            title, finding_dir_id, existing_tasks_with_dirs
        )
        if matched_title is not None:
            logger.warning(
                f"[RESEARCH] Skipping duplicate finding '{title}' — matches existing task"
            )
            outcome.word_overlap_count += 1
            reason = f"word overlap with: {matched_title[:60]}"
            outcome.filtered.append(
                {
                    "title": title[:120],
                    "direction_id": finding_dir_id,
                    "reason": reason[:80],
                }
            )
            continue

        if retired_failed_approaches:
            matched_approach = _find_retired_approach_match(
                title, retired_failed_approaches
            )
            if matched_approach is not None:
                logger.info(
                    f"[RESEARCH] Skipping finding '{title}' — matches retired direction failed approach"
                )
                outcome.retired_approach_count += 1
                reason = f"retired approach: {matched_approach}"
                outcome.filtered.append(
                    {
                        "title": title[:120],
                        "direction_id": finding_dir_id,
                        "reason": reason[:80],
                    }
                )
                continue

        if archived_diagnoses:
            matched_diagnosis = _find_archived_diagnosis_match(
                title, archived_diagnoses
            )
            if matched_diagnosis is not None:
                logger.info(
                    f"[RESEARCH] Skipping finding '{title}' — matches archived diagnosis"
                )
                outcome.retired_approach_count += 1
                reason = f"archived diagnosis: {matched_diagnosis[:60]}"
                outcome.filtered.append(
                    {
                        "title": title[:120],
                        "direction_id": finding_dir_id,
                        "reason": reason[:80],
                    }
                )
                continue

        finding_files = set(finding.get("related_files", []))
        if finding_files and finding_files.issubset(open_pr_files):
            logger.info(
                f"[RESEARCH] Skipping finding '{title}' — all related files already have open PRs"
            )
            outcome.file_overlap_count += 1
            outcome.filtered.append(
                {
                    "title": title[:120],
                    "direction_id": finding_dir_id,
                    "reason": "all files covered by open PR"[:80],
                }
            )
            continue

        # Evidence-overlap check: compare sources and claims against already-deduped findings
        is_evidence_dup, matched_evidence_title = _check_evidence_overlap(
            finding.get("sources", []),
            finding.get("claims", []),
            evidence_list,
        )
        if is_evidence_dup:
            logger.info(
                f"[RESEARCH] Skipping finding '{title}' — evidence overlap with '{matched_evidence_title}'"
            )
            outcome.evidence_overlap_count += 1
            reason = f"evidence overlap with: {matched_evidence_title[:60]}"
            outcome.filtered.append(
                {
                    "title": title[:120],
                    "direction_id": finding_dir_id,
                    "reason": reason[:80],
                }
            )
            continue

        outcome.deduplicated.append((orig_i, finding, fhash))
        # Add to evidence list for future comparisons in this cycle
        evidence_list.append(
            (title, finding.get("sources", []), finding.get("claims", []))
        )

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
            not finding_dir_id or not task_dir_id or finding_dir_id == task_dir_id
        )
        threshold = 4 if same_direction else 6
        if len(overlap) >= threshold:
            return title
    return None

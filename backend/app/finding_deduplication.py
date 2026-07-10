"""Pure finding-deduplication helpers used by research completion.

Extracted from ``app.research_completion._handle_research_completion`` so the
dedup logic can be independently unit-tested. These helpers do not touch the
database session — callers supply already-loaded task lists and artifact files.

Four strategies are applied, in order:
  1. Hash dedup (``finding_hash`` collides with a recent task that is either
     already merged, still running, a suppressed cancellation, or a "real"
     failure within cooldown).
  2. Region-identity matching against currently active tasks and recent terminal
     findings: a candidate duplicates a finding only when their ``related_files``
     overlap (the necessary condition) AND that overlap is corroborated by title
     word-overlap or a shared claim. This replaced an earlier title-string-only
     match whose single-token false positives (e.g. a shared ``a/b``) silently
     dropped genuinely-distinct work; gating title overlap behind concrete
     file-region overlap removes that failure mode. See :func:`_find_overlap_match`.
  3. File-overlap: skip findings whose ``related_files`` are fully covered by
     open PR artifacts.
  4. Evidence-overlap: skip findings whose source refs (≥80% Jaccard overlap)
     and claims (at least one shared) match recent findings. A bounded recent-
     terminal history path is also consulted when callers provide it.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from app.capability_registry import (
    compose_capability_markers,
    normalize_capability_name,
)
from app.failure_classification import INFRA_CLASSES, classify_failure
from app.landed_signal import suppresses_redispatch
from app.models import Task, TaskStatus
from app.task_blocked import canonicalize_exact_task_ref, is_exact_task_ref
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

CODE_ERROR_COOLDOWN = timedelta(hours=48)
RECENT_TERMINAL_TASK_HISTORY_CAP = 50
RECENT_TERMINAL_FINDING_HISTORY_CAP = 200

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
_COMPUTE_EXPERIMENT_TASK_TYPE = "compute_experiment"
_COMPUTE_EXPERIMENT_FINDING_TYPES = frozenset(
    {_COMPUTE_EXPERIMENT_TASK_TYPE, "measurement", "benchmark"}
)


# ── Region-identity dedup thresholds ────────────────────────────────────────
# A finding is a duplicate of a candidate only when they target an overlapping
# code REGION (related_files) AND that overlap is corroborated by title/claim
# evidence. This replaces the prior title-string-similarity match (word /
# technical-token / bigram overlap), which produced unbounded false positives:
# a single shared generic token (e.g. ``a/b``) collided genuinely-distinct work.
# Requiring concrete file-region overlap as a NECESSARY precondition neutralizes
# that failure mode while keeping the agent's title/claims as corroborators.
#
# region (necessary):  |related_files ∩| >= REGION_MIN_SHARED  OR  jaccard >= REGION_MIN_JACCARD
# corroboration:
#   same-direction (any of): title word-overlap >= CORROBORATE_WORDS_SAME,
#                            OR >=1 shared claim, OR file jaccard >= STRONG_REGION_JACCARD
#   cross-direction (any of): >=1 shared claim, OR file jaccard >= STRONG_REGION_JACCARD
#                            — the bare title-word arm is DROPPED for independent
#                            directions, which share hot files without sharing intent.
REGION_MIN_SHARED = 2
REGION_MIN_JACCARD = 0.5
STRONG_REGION_JACCARD = 0.7
# Minimum shared-file count required for the jaccard-only corroborator to fire.
# Identical 1-2-file sets are not strong enough evidence alone (module-scoped
# directions concentrate distinct work onto the same 2 hot files, so jaccard=1.0
# is trivially achieved by any two findings in the same module — confirmed live
# incident: two distinct findings sharing acceptance_criteria.py +
# routers/admin_acceptance_criteria.py were both dropped against "fix pr #12662",
# which shared only those two files without any title or claim agreement).
STRONG_REGION_MIN_SHARED = 3
CORROBORATE_WORDS_SAME = 3
# Retained for reference / legacy direction-aware title-overlap callers
# (e.g. ``is_duplicate``). The region-identity matcher no longer uses a
# cross-direction title-word threshold — cross-direction findings require a
# strong corroborator instead — but the constant documents the prior policy.
CORROBORATE_WORDS_CROSS = 5


@dataclass(frozen=True)
class _OverlapCandidate:
    direction_id: Optional[str]
    title: str
    task_type: Optional[str] = None
    related_files: frozenset = frozenset()
    claims: frozenset = frozenset()


# Whitelisted prerequisite kinds for the machine-resolvable {kind, ref} need
# representation a finding may declare. Conservative by design: only these two
# kinds are ever resolved into a dependency edge — anything else is dropped.
VALID_PREREQUISITE_KINDS = frozenset({"task", "capability"})


def _task_context(task: Task) -> dict:
    ctx = task.context or {}
    return ctx if isinstance(ctx, dict) else {}


def _task_finding_hash(task: Task) -> str | None:
    value = _task_context(task).get("finding_hash")
    return str(value) if value else None


def task_finding_lineage(task: Task) -> set[str]:
    """Return the explicit finding hashes this task descends from.

    The lineage is intentionally hash-only. It includes the task's canonical
    ``finding_hash`` plus any spawn-time ``finding_lineage`` entries recorded on
    the task context, so retitled/split findings can still satisfy a prerequisite
    keyed by their original hash without adding fuzzy title matching.
    """
    ctx = _task_context(task)
    lineage: set[str] = set()
    primary = ctx.get("finding_hash")
    if primary:
        lineage.add(str(primary))
    raw = ctx.get("finding_lineage")
    if isinstance(raw, (list, set, tuple)):
        lineage.update(str(v) for v in raw if v)
    elif isinstance(raw, str) and raw:
        lineage.add(raw)
    return lineage


def need_matches_task_lineage(need: dict, task: Task | None) -> bool:
    """True when a task-kind need ref is in the task's explicit lineage set."""
    if task is None or not isinstance(need, dict):
        return False
    if need.get("kind") != "task":
        return False
    canonical_ref = canonicalize_exact_task_ref(need.get("ref"))
    if canonical_ref is None:
        return False
    return canonical_ref in task_finding_lineage(task)


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


def canonical_finding_body(finding: dict) -> str:
    """Return the canonical body text for a finding dict.

    Prefers ``description``; falls back to ``prompt`` when description is absent
    or empty.  Using this helper everywhere keeps hashing, spawning, and deferral
    consistent regardless of which key the research agent happened to populate.
    """
    return str(finding.get("description") or finding.get("prompt") or "").strip()


def finding_hash(title: str, description: str) -> str:
    """Stable 16-char hash used to dedup findings across research cycles."""
    return hashlib.sha256((title + description).encode()).hexdigest()[:16]


def is_duplicate(
    finding_title: str,
    finding_dir_id: Optional[str],
    existing: Iterable[tuple],
) -> bool:
    """Legacy title-only word-overlap check with direction-aware threshold.

    NOTE: This predicate is NOT on the research spawn path — the live finding
    dedup uses region-identity matching (:func:`_find_overlap_match`). It is
    retained only as a standalone title-similarity helper for callers/tests that
    want pure title comparison; do not route new spawn-gating through it.

    Cross-direction: require 6+ word overlap. Independent strategic tracks
    rarely produce true duplicates, so a higher threshold avoids false
    positives. If either direction is unknown (None), treat as same-direction
    (safer default).

    Secondary gates catch paraphrased duplicates that slip past the word-overlap
    threshold:
    - Technical token overlap: shared identifiers/paths/function names are strong
      signals of same intent even when prose is reworded. Threshold: 1 token
      same-direction, 2 tokens cross-direction.
    - Bigram Jaccard: high similarity in the sequence of non-stopword term pairs
      indicates near-identical phrasing. Threshold: 0.65 same-direction, 0.80
      cross-direction.
    """
    words = set(finding_title.lower().split()) - _STOPWORDS
    finding_tech = _extract_technical_tokens(finding_title)
    finding_bgs = _title_bigrams(finding_title)
    for candidate_raw in existing:
        candidate = _coerce_overlap_candidate(candidate_raw)
        if candidate is None:
            continue
        task_dir_id = candidate.direction_id
        title = candidate.title
        overlap = words & (set(title.lower().split()) - _STOPWORDS)
        same_direction = (
            not finding_dir_id or not task_dir_id or finding_dir_id == task_dir_id
        )
        threshold = 4 if same_direction else 6
        if len(overlap) >= threshold:
            return True
        # Technical token secondary gate
        if finding_tech:
            cand_tech = _extract_technical_tokens(title)
            tech_threshold = 1 if same_direction else 2
            if len(finding_tech & cand_tech) >= tech_threshold:
                return True
        # Bigram Jaccard secondary gate
        if finding_bgs:
            cand_bgs = _title_bigrams(title)
            if cand_bgs:
                jac_threshold = 0.65 if same_direction else 0.80
                if _jaccard_overlap(finding_bgs, cand_bgs) >= jac_threshold:
                    return True
    return False


def normalize_prerequisite(raw: object) -> Optional[dict]:
    """Coerce a finding's declared prerequisite into a validated ``{kind, ref}``.

    The need representation is intentionally tiny and machine-resolvable: a
    ``kind`` drawn from :data:`VALID_PREREQUISITE_KINDS` and a non-empty ``ref``
    string. Returns ``None`` for any malformed input (non-dict, missing/blank
    ref, non-string fields) or an unknown kind, so callers can treat "declared a
    prerequisite" as a hard, explicit signal rather than a fuzzy guess.

    A ``kind=task`` ref additionally MUST be exact — a real task UUID or a
    canonical finding hash (:func:`app.task_blocked.is_exact_task_ref`). A
    title-like task ref (free prose naming a desired foundation) is rejected
    here at ingestion so it never drives an edge nor becomes durable stranded
    "missing task" demand; a named foundation must be declared as
    ``kind=capability`` instead.

    ``kind`` is lowercased; ``ref`` is whitespace-stripped but case-preserved
    (task ids / finding hashes are case-sensitive; capability matching lowercases
    at compare time).
    """
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    ref = raw.get("ref")
    if not isinstance(kind, str) or not isinstance(ref, str):
        return None
    kind = kind.strip().lower()
    ref = ref.strip()
    if kind not in VALID_PREREQUISITE_KINDS or not ref:
        return None
    if kind == "task" and not is_exact_task_ref(ref):
        return None
    if kind == "task":
        ref = canonicalize_exact_task_ref(ref) or ref
    return {"kind": kind, "ref": ref}


def _task_capability_markers(task: Task) -> set[str]:
    """Return the explicit capability markers a task advertises in its context.

    Reads the shared capability-marker composition helper so every task exposes
    its explicit ``provides_capability`` / ``capability`` markers PLUS the
    namespaced derived floor marker. No title/file inference is added here.
    """
    return compose_capability_markers(task)


def prerequisite_matches_task(normalized: dict, task: Optional[Task]) -> bool:
    """True if ``task`` satisfies a normalized ``{kind, ref}`` prereq by explicit signal.

    The single-candidate predicate underneath :func:`resolve_prerequisite_task`,
    exposed so the resume-time matcher (``blocked_need_resolution``) can reuse the
    exact same task-id / finding-hash / capability-marker signals rather than
    re-deriving them — keeping the spawn-time and resume-time resolvers in
    lockstep. EXPLICIT signals only:

      * ``kind == "task"``: ``ref`` equals ``str(task.id)`` or the task's
        ``context["finding_hash"]``.
      * ``kind == "capability"``: ``ref`` (case-insensitive) matches an explicit
        capability marker (see :func:`_task_capability_markers`).

    Any other kind, a ``None`` task, or a malformed ``normalized`` is ``False``.
    """
    if task is None or not isinstance(normalized, dict):
        return False
    kind = normalized.get("kind")
    ref = normalized.get("ref")
    if not isinstance(ref, str):
        return False
    if kind == "task":
        canonical_ref = canonicalize_exact_task_ref(ref)
        if canonical_ref is None:
            return False
        return (
            canonical_ref == str(task.id)
            or canonical_ref == _task_finding_hash(task)
            or need_matches_task_lineage({"kind": "task", "ref": canonical_ref}, task)
        )
    if kind == "capability":
        markers = _task_capability_markers(task)
        # Floor markers (task-provider:<uuid>) contain a colon that normalize_capability_name
        # collapses to a hyphen, so also try a direct lowercase match before normalizing.
        if ref.strip().lower() in markers:
            return True
        normalized_ref = normalize_capability_name(ref)
        return normalized_ref is not None and normalized_ref in markers
    return False


def _canonical_order_key(task: Task) -> tuple:
    """Deterministic canonical ordering key for a candidate task.

    Replicates the exact key used by
    :func:`app.digest_metrics.compute_foundation_convergence_actions`
    (``(getattr(t, "created_at", None) or datetime.min, str(t.id))``) so the
    "which duplicate is canonical" decision agrees by construction across the
    convergence path, the spawn-time resolver, and the resume-time matcher.

    Earliest ``created_at`` wins; a missing/``None`` ``created_at`` sorts as the
    minimum (``datetime.min``), so it does not silently outrank a real
    timestamped foundation unless it genuinely is earliest. Ties on
    ``created_at`` break on ``str(task.id)``.
    """
    return (getattr(task, "created_at", None) or datetime.min, str(task.id))


def select_canonical_provider(tasks: Iterable[Task]) -> Optional[Task]:
    """Return the canonical provider among ``tasks`` (earliest, id-tiebroken).

    Purely additive deterministic tie-break: given any set of candidate tasks
    that all satisfy the same prerequisite/need, pick the SAME canonical one —
    earliest by ``created_at`` (``None`` treated as the minimum), tie-broken by
    ``str(task.id)`` — using :func:`_canonical_order_key`. Returns ``None`` for
    an empty input. Selection only; it does NOT re-check the match predicate, so
    callers must pass an already-filtered match set.
    """
    candidates = list(tasks)
    if not candidates:
        return None
    return min(candidates, key=_canonical_order_key)


def resolve_prerequisite_task(
    prereq: object,
    candidate_tasks: Iterable[Task],
) -> Optional[Task]:
    """Conservatively resolve a declared prerequisite to one canonical candidate.

    EXPLICIT references only — never fuzzy title overlap — so genuinely
    independent work is never falsely serialized:

      * ``kind == "task"``: ``ref`` must equal ``str(task.id)`` or the task's
        ``context["finding_hash"]``.
      * ``kind == "capability"``: ``ref`` (case-insensitive) must match an
        explicit capability marker on the task (see
        :func:`_task_capability_markers`).

    ``prereq`` is normalized internally, so a raw finding field can be passed
    directly. When MORE THAN ONE candidate satisfies the prereq (e.g. several
    active tasks advertise the same explicit capability marker), the CANONICAL
    provider — earliest by ``created_at``, tie-broken by ``str(task.id)`` (see
    :func:`select_canonical_provider`) — is returned, NOT merely the first in
    iteration order, so every dependent of a duplicated foundation
    deterministically attaches to the SAME provider. Returns ``None`` when the
    prereq is malformed or nothing matches. The candidate enumeration (e.g.
    active/queued/in-flight statuses) is entirely the caller's responsibility.
    """
    normalized = normalize_prerequisite(prereq)
    if normalized is None:
        return None
    matches = [
        task for task in candidate_tasks if prerequisite_matches_task(normalized, task)
    ]
    return select_canonical_provider(matches)


def build_dedup_state(
    recent_tasks: Iterable[Task],
    failed_parent_ids: set,
    now: Optional[datetime] = None,
    previously_filtered_hashes: Optional[set[str]] = None,
) -> tuple[set[str], dict[str, object], dict[str, Task]]:
    """Build hash-dedup state from a list of recent tasks.

    ``failed_parent_ids`` is the "stranded parent" set: parent task IDs whose
    rollup code is confirmed NOT on the integration branch.  It covers:
      * parents that FAILED or CANCELLED (rollup never landed),
      * parents that COMPLETED without ``pr_merged`` (rollup PR closed/
        superseded without merging), and
      * non-terminal parents (EVALUATING/QUEUED/IN_PROGRESS/PROVISIONING)
        where a git-ancestry check definitively confirmed the rollup merge
        commit is not reachable from the integration branch.

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
        # COMPLETED without pr_merged: code was never shipped. Normally this
        # leaves the finding eligible for re-dispatch — BUT an affirmative
        # non-reproducing completion is the exception. When an implementation
        # task takes the reproduce-on-HEAD off-ramp it records the canonical
        # verdict ``reproduce_verdict.status == "already_resolved"`` (or, for
        # tasks predating the verdict, a legacy ``no_op_reason`` stamped by
        # ``./done --already-done``): the agent verified the asserted defect does
        # not reproduce / is already resolved. Suppress that finding hash so the
        # same non-reproducing finding is not re-parsed and re-dispatched every
        # research cycle. An ``indeterminate`` verdict or a dropped/abandoned
        # no-PR completion carries neither signal and stays eligible for retry.
        if t.status == TaskStatus.COMPLETED and not ctx.get("pr_merged"):
            if suppresses_redispatch(t):
                recent_finding_hashes.add(fhash)
                failed_hashes.setdefault(fhash, "no-op (not reproducing)")
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
            failure_class = classify_failure(
                t, at_time=t.last_heartbeat or t.started_at
            )
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


def extract_recent_terminal_finding_history(
    recent_tasks: Iterable[Task],
    failed_parent_ids: set,
    now: Optional[datetime] = None,
    *,
    task_cap: int = RECENT_TERMINAL_TASK_HISTORY_CAP,
    finding_cap: int = RECENT_TERMINAL_FINDING_HISTORY_CAP,
) -> list[dict]:
    """Return recent terminal findings suitable for cross-cycle dedup.

    The history is bounded and conservative:
      * only tasks with persisted ``plan_output`` findings are considered;
      * completed tasks whose rollup parent was already marked stranded are
        excluded as ghost merges;
      * FAILED/CANCELLED tasks keep the existing retry semantics, so transient
        infra failures and CODE_ERROR tasks beyond cooldown are not added; and
      * the returned findings are capped so a busy project cannot balloon the
        terminal-history seed.
    """
    now = now or _utcnow()
    terminal_tasks = sorted(
        recent_tasks,
        key=lambda t: (getattr(t, "created_at", None) or datetime.min, str(t.id)),
        reverse=True,
    )
    findings: list[dict] = []
    task_count = 0
    for t in terminal_tasks:
        if task_count >= task_cap or len(findings) >= finding_cap:
            break
        if t.status not in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            continue
        ctx = _task_context(t)
        if (
            t.status == TaskStatus.COMPLETED
            and ctx.get("pr_merged")
            and t.parent_task_id in failed_parent_ids
        ):
            continue
        if t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            failure_class = classify_failure(
                t, at_time=t.last_heartbeat or t.started_at
            )
            if failure_class in INFRA_CLASSES:
                continue
            if failure_class == "CODE_ERROR" and not _within_code_error_cooldown(
                t, now
            ):
                continue
        if not isinstance(t.plan_output, list) or not t.plan_output:
            continue
        task_count += 1
        task_dir_id = _optional_str(ctx.get("direction_id"))
        task_type = _normalize_task_type((t.first_payload or {}).get("type"))
        for finding in t.plan_output:
            if len(findings) >= finding_cap:
                break
            if not isinstance(finding, dict):
                continue
            title = finding.get("title")
            if not title:
                continue
            enriched = dict(finding)
            enriched["title"] = str(title)
            if task_dir_id:
                enriched.setdefault("direction_id", task_dir_id)
            if task_type:
                enriched.setdefault("type", task_type)
            findings.append(enriched)
    return findings


def extract_retired_failed_approaches(retired_context: list[dict]) -> list[dict]:
    """Extract failed approach dicts from retired direction context dicts.

    Each dict may have 'failed_approaches' (list[str]) and/or
    'recently_completed_finding_titles' (list[str]). Both are used as
    suppression signals — the system already explored them.

    Each input dict may carry a 'category' field ('product' | 'structural' | 'mixed').
    Missing/None category defaults to 'mixed' (permissive: matches any finding category).

    Returns a list of dicts with 'text' (str) and 'category' (str) keys.
    """
    approaches: list[dict] = []
    for ctx in retired_context or []:
        category = ctx.get("category") or "mixed"
        for approach in ctx.get("failed_approaches", []):
            if isinstance(approach, str) and approach.strip():
                approaches.append({"text": approach, "category": category})
        for title in ctx.get("recently_completed_finding_titles", []):
            if isinstance(title, str) and title.strip():
                approaches.append({"text": title, "category": category})
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
    retired_failed_approaches: list[dict],
    finding_category: Optional[str] = None,
) -> Optional[str]:
    """Return the first retired approach whose words overlap finding_title.

    Uses a lower threshold (3 words) than active-task matching (4/6) because
    the system has already concluded these approaches don't work.

    Category gating: a match is skipped when both the finding and the retired
    approach have explicit non-mixed categories that differ. 'mixed' on either
    side is permissive and always allows the match to block.
    """
    words = set(finding_title.lower().split()) - _STOPWORDS
    fc = finding_category or "mixed"
    for entry in retired_failed_approaches:
        text = entry.get("text", "") if isinstance(entry, dict) else str(entry)
        ac = (entry.get("category") or "mixed") if isinstance(entry, dict) else "mixed"
        # Cross-category: both sides have explicit non-mixed categories that differ → skip
        if fc != "mixed" and ac != "mixed" and fc != ac:
            continue
        overlap = words & (set(text.lower().split()) - _STOPWORDS)
        if len(overlap) >= 3:
            return text[:60]
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
      - path/to/file.py:42-50
      - path/to/file.py#L10-L20
    Returns the base file path only.

    Typed refs (pr:123, message:abc) and URL schemes (https://...) are
    preserved because they have no path separator or extension before the
    colon, so they are not mistaken for file-path line-number suffixes.
    """
    if not ref:
        return ""
    # Strip fragment-based line ranges (e.g., #L10-L20)
    ref = ref.split("#")[0]
    # Strip numeric line-number suffixes (e.g., :42, :42-50) only when the ref
    # looks like a file path (contains a / or . before the colon). This preserves
    # typed refs like pr:123, message:abc, and URL schemes like https://.
    if re.search(r"[/.].*:", ref):
        ref = re.sub(r":\d+(?:-\d+)?$", "", ref)
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


def _norm_related_files(raw: object) -> frozenset:
    """Normalize a ``related_files`` value into a set of comparable file paths.

    Strips line-number suffixes (reusing :func:`_normalize_source_ref`) and
    lowercases for case-insensitive path comparison. Non-list inputs yield the
    empty set so a finding/task with no declared files simply cannot establish a
    region (it falls back to exact-hash dedup).
    """
    if not isinstance(raw, (list, set, tuple)):
        return frozenset()
    out = set()
    for x in raw:
        if not x:
            continue
        norm = _normalize_source_ref(str(x)).lower()
        if norm:
            out.add(norm)
    return frozenset(out)


def _norm_claim_set(raw: object) -> frozenset:
    """Normalize a ``claims`` list into a set of comparable claim strings."""
    if not isinstance(raw, (list, set, tuple)):
        return frozenset()
    return frozenset(
        _normalize_claim(c) for c in raw if isinstance(c, str) and c.strip()
    )


def _finding_declares_dependency(finding: dict) -> bool:
    """True when a finding declares it builds on a prerequisite foundation.

    A dependent finding — one carrying a non-empty ``depends_on_indices`` or a
    normalized ``_prerequisite`` ({kind, ref}) — legitimately shares a code
    region with the foundation it depends on; that file overlap is the SIGNAL of
    a genuine cross-tree code-dep (it drives base_branch inheritance), not
    evidence of duplication. Such findings must therefore be exempt from the
    region-identity dedup stage, which would otherwise drop the dependent for
    "sharing a code region with" its own prerequisite and strand the cross-tree
    wiring. Exact-hash and evidence-overlap stages still apply, so a dependent
    that is a true duplicate of prior work is still caught.
    """
    deps = finding.get("depends_on_indices")
    if isinstance(deps, (list, tuple)) and any(
        isinstance(d, int) for d in deps
    ):
        return True
    return bool(finding.get("_prerequisite"))


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
    # Count of findings dropped by region-identity dedup (shared related_files +
    # title/claim corroboration). Field name retained for backward compatibility
    # with the persisted ``word_overlap_dedup`` breakdown key and its downstream
    # consumers (digest / direction_outcomes / agent research-plan prompt).
    word_overlap_count: int = 0
    file_overlap_count: int = 0
    evidence_overlap_count: int = 0
    retired_approach_count: int = 0
    archived_diagnoses_count: int = 0
    # Per-skip reason records: {title, direction_id, reason, finding} for each
    # filtered finding. ``finding`` carries the full original payload (description,
    # evidence, file citations, severity, sources, claims, etc.) so downstream
    # salvage consumers can inspect type, severity, and context of skipped findings
    # without a separate lookup. ``reason`` is a short literal (≤80 chars) and
    # ``title`` is truncated to 120 chars for the research-agent feedback loop.
    filtered: list[dict] = field(default_factory=list)


def deduplicate_findings(
    findings: list[dict],
    task_context: Optional[dict],
    recent_finding_hashes: set[str],
    failed_hashes: dict[str, object],
    existing_tasks_with_dirs: list[tuple],
    open_pr_files: set[str],
    retired_failed_approaches: Optional[list[dict]] = None,
    archived_diagnoses: Optional[list[str]] = None,
    direction_categories: Optional[dict[str, str]] = None,
    recent_terminal_findings: Optional[list[dict]] = None,
) -> DedupOutcome:
    """Apply hash, region-identity, file-overlap, and evidence-overlap dedup to ``findings``.

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
    recent_terminal_findings = [
        f for f in (recent_terminal_findings or []) if isinstance(f, dict)
    ]
    # Build evidence list seeded from recent terminal findings (cross-cycle dedup),
    # then extended with surviving findings from the current batch.
    evidence_list: list[tuple[str, list, list]] = [
        (f.get("title", ""), f.get("sources", []), f.get("claims", []))
        for f in recent_terminal_findings
    ]

    for orig_i, finding in indexed_findings:
        title = finding.get("title", f"Research finding #{orig_i + 1}")
        description = canonical_finding_body(finding)
        fhash = finding_hash(title, description)
        finding_dir_id = finding.get("direction_id") or parent_dir_id
        finding_task_type = _dedup_spawn_task_type(finding)
        # A finding that declares a dependency on a foundation legitimately
        # shares a code region with that prerequisite — that overlap is what
        # marks it a genuine cross-tree code-dep (it drives base inheritance),
        # not a duplicate. Exempt it from region-identity dedup so it survives
        # to the cross-tree wiring path; exact-hash / evidence stages still run.
        declares_dependency = _finding_declares_dependency(finding)

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
                    "finding": finding,
                }
            )
            continue

        matched_title = (
            None
            if declares_dependency
            else _find_overlap_match(
                finding, finding_dir_id, existing_tasks_with_dirs, finding_task_type
            )
        )
        if matched_title is not None:
            logger.warning(
                f"[RESEARCH] Skipping duplicate finding '{title}' — shares code region "
                f"with existing task '{matched_title[:60]}'"
            )
            outcome.word_overlap_count += 1
            reason = f"region overlap with: {matched_title[:60]}"
            outcome.filtered.append(
                {
                    "title": title[:120],
                    "direction_id": finding_dir_id,
                    "reason": reason[:80],
                    "finding": finding,
                }
            )
            continue

        if recent_terminal_findings and not declares_dependency:
            matched_title = _find_overlap_match(
                finding, finding_dir_id, recent_terminal_findings, finding_task_type
            )
            if matched_title is not None:
                logger.warning(
                    f"[RESEARCH] Skipping finding '{title}' — shares code region with "
                    f"recent terminal finding '{matched_title[:60]}'"
                )
                outcome.word_overlap_count += 1
                reason = f"recent terminal region overlap with: {matched_title[:60]}"
                outcome.filtered.append(
                    {
                        "title": title[:120],
                        "direction_id": finding_dir_id,
                        "reason": reason[:80],
                        "finding": finding,
                    }
                )
                continue

        if retired_failed_approaches:
            _finding_category = (
                (direction_categories or {}).get(finding_dir_id or "") or "mixed"
                if finding_dir_id
                else "mixed"
            )
            matched_approach = _find_retired_approach_match(
                title, retired_failed_approaches, _finding_category
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
                        "finding": finding,
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
                outcome.archived_diagnoses_count += 1
                reason = f"archived diagnosis: {matched_diagnosis[:60]}"
                outcome.filtered.append(
                    {
                        "title": title[:120],
                        "direction_id": finding_dir_id,
                        "reason": reason[:80],
                        "finding": finding,
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
                    "finding": finding,
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
                    "finding": finding,
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
    finding: dict,
    finding_dir_id: Optional[str],
    existing: Iterable[tuple],
    finding_task_type: Optional[str] = None,
) -> Optional[str]:
    """Return the title of the first candidate that is a REGION-IDENTITY duplicate.

    A candidate duplicates ``finding`` only when both conditions hold:

    1. REGION (necessary): their ``related_files`` overlap by at least
       :data:`REGION_MIN_SHARED` files OR Jaccard >= :data:`REGION_MIN_JACCARD`.
       A finding/candidate with no declared files cannot establish a region and
       is never matched here (it falls back to exact-hash dedup).
    2. CORROBORATION (sufficient, given region), direction-aware:
       * SAME-direction (or either direction unknown): title word-overlap >=
         :data:`CORROBORATE_WORDS_SAME`, OR >=1 shared claim, OR file sets
         near-identical (Jaccard >= :data:`STRONG_REGION_JACCARD`).
       * CROSS-direction (both directions known and different): a STRONG
         corroborator only — >=1 shared claim OR Jaccard >=
         :data:`STRONG_REGION_JACCARD`. The bare title-word arm is dropped,
         because independent strategic tracks routinely touch the same hot file
         (e.g. ``orchestrator.py``) while pursuing distinct work; word overlap
         there was silently dropping genuinely-distinct cross-direction findings
         (the live "1 word-overlap + 1 type-filtered → 0 tasks" signature behind
         ~28% of research tasks spawning zero children).

    This replaces the prior title-string-only match (word / technical-token /
    bigram overlap), whose single-token false positives silently dropped
    distinct work. Title overlap survives only as a SAME-direction corroborator
    *gated behind* concrete file-region overlap, so a shared generic token alone
    can no longer suppress a finding. Returns ``None`` when nothing matches, so
    callers can build a reason string from the matched title.
    """
    finding_files = _norm_related_files(finding.get("related_files"))
    if not finding_files:
        return None
    finding_words = set((finding.get("title") or "").lower().split()) - _STOPWORDS
    finding_claims = _norm_claim_set(finding.get("claims"))
    for candidate_raw in existing:
        candidate = _coerce_overlap_candidate(candidate_raw)
        if candidate is None:
            continue
        if not _candidate_can_overlap(finding_task_type, candidate):
            continue
        cand_files = candidate.related_files
        if not cand_files:
            continue
        shared = finding_files & cand_files
        file_jaccard = _jaccard_overlap(finding_files, cand_files)
        region = len(shared) >= REGION_MIN_SHARED or file_jaccard >= REGION_MIN_JACCARD
        if not region:
            continue
        # Direction identity is "known to differ" only when BOTH sides declare a
        # direction and they disagree. A missing direction on either side is
        # treated as same-direction (the safe, looser default) because we cannot
        # prove independence.
        cross_direction = bool(
            finding_dir_id
            and candidate.direction_id
            and finding_dir_id != candidate.direction_id
        )
        # Strong corroborators (shared claim, near-identical file set) assert
        # semantic identity directly and apply regardless of direction.
        # File-jaccard alone is only trusted when the shared set is large enough
        # (>= STRONG_REGION_MIN_SHARED): identical 1-2-file sets are typical for
        # any two findings in a module-scoped direction, so jaccard=1.0 there is
        # not meaningful signal without title or claim corroboration.
        strong_files = (
            file_jaccard >= STRONG_REGION_JACCARD
            and len(shared) >= STRONG_REGION_MIN_SHARED
        )
        strong_corroborated = bool(finding_claims & candidate.claims) or strong_files
        if cross_direction:
            # Independent strategic tracks routinely touch the same hot file
            # (e.g. orchestrator.py) while pursuing genuinely distinct work, so
            # a region overlap there is expected, not duplicative. Title-word
            # overlap is too weak to distinguish "same fix, two directions found
            # it" from "two different fixes in the same file" — it was eating
            # real cross-direction findings (~28% of research tasks spawned 0
            # children, the recurring "1 word-overlap + 1 type-filtered → 0
            # tasks" signature). For cross-direction candidates we therefore
            # DROP the bare title-word arm entirely and require a strong
            # corroborator (shared claim or near-identical files).
            corroborated = strong_corroborated
        else:
            # Same-direction (or unknown-direction) candidates keep the looser
            # title-word corroborator: within one direction the agent is the
            # same author iterating on the same intent, so word overlap on top
            # of a shared region is a reliable duplicate signal.
            title_words = set(candidate.title.lower().split()) - _STOPWORDS
            corroborated = (
                strong_corroborated
                or len(finding_words & title_words) >= CORROBORATE_WORDS_SAME
            )
        if corroborated:
            return candidate.title
    return None


def _coerce_overlap_candidate(raw: object) -> Optional[_OverlapCandidate]:
    if isinstance(raw, _OverlapCandidate):
        return raw
    if isinstance(raw, dict):
        title = raw.get("title")
        if not title:
            return None
        return _OverlapCandidate(
            direction_id=_optional_str(raw.get("direction_id")),
            title=str(title),
            task_type=_normalize_task_type(raw.get("task_type") or raw.get("type")),
            related_files=_norm_related_files(raw.get("related_files")),
            claims=_norm_claim_set(raw.get("claims")),
        )
    if isinstance(raw, tuple) and len(raw) >= 2:
        # Active-task tuples: (direction_id, title[, task_type[, related_files]]).
        # Legacy 2/3-tuples carry no files and so cannot establish a region —
        # they are effectively inert under region-identity dedup, which is the
        # safe default (no false positives) for callers that have not been
        # updated to supply related_files.
        return _OverlapCandidate(
            direction_id=_optional_str(raw[0]),
            title=str(raw[1]),
            task_type=_normalize_task_type(raw[2]) if len(raw) >= 3 else None,
            related_files=_norm_related_files(raw[3]) if len(raw) >= 4 else frozenset(),
            claims=_norm_claim_set(raw[4]) if len(raw) >= 5 else frozenset(),
        )
    return None


def _dedup_spawn_task_type(finding: dict) -> Optional[str]:
    finding_type = _normalize_task_type(finding.get("type"))
    if finding_type in _COMPUTE_EXPERIMENT_FINDING_TYPES:
        return _COMPUTE_EXPERIMENT_TASK_TYPE
    return finding_type


def _candidate_can_overlap(
    finding_task_type: Optional[str], candidate: _OverlapCandidate
) -> bool:
    if finding_task_type != _COMPUTE_EXPERIMENT_TASK_TYPE:
        return True
    # Legacy tuple callers have no task metadata, so keep the historical
    # conservative behavior for them. Typed active research/design tasks are the
    # false positives this gate is meant to exclude.
    return candidate.task_type in (None, _COMPUTE_EXPERIMENT_TASK_TYPE)


def _normalize_task_type(value: object) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized or None


def _optional_str(value: object) -> Optional[str]:
    return str(value) if value is not None else None


def _extract_technical_tokens(title: str) -> set[str]:
    """Extract tokens that are likely identifiers, file paths, or function names.

    A token is "technical" when it contains '_', '/', '::', or '.' (file-path or
    attribute notation) or is mixed-case (camelCase / PascalCase), after stripping
    boundary punctuation. These tokens reliably anchor semantic identity even when
    surrounding prose is reworded — they are the specific referents the finding
    addresses. Stopwords are excluded.
    """
    technical: set[str] = set()
    for word in title.split():
        stripped = word.strip("'\"()[]{}.,;:!?")
        if not stripped or stripped.lower() in _STOPWORDS:
            continue
        if (
            "_" in stripped
            or "/" in stripped
            or "::" in stripped
            or "." in stripped
            or (
                stripped != stripped.lower()
                and stripped != stripped.upper()
                and len(stripped) > 3
            )
        ):
            technical.add(stripped.lower())
    return technical


def _title_bigrams(title: str) -> set[tuple[str, str]]:
    """Compute bigrams of non-stopword tokens for similarity scoring.

    Used to catch paraphrased titles where prose is rewritten but the
    sequence structure of key terms is preserved.
    """
    tokens = [w.lower() for w in title.split() if w.lower() not in _STOPWORDS]
    if len(tokens) < 2:
        return set()
    return {(tokens[i], tokens[i + 1]) for i in range(len(tokens) - 1)}

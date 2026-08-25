"""Read-only findings-index query surface.

Extracted from ``app.research_completion`` (which still re-exports these symbols
for backward compatibility) so the small, cohesive findings-index query surface
lives in its own module away from the large research-completion handler.

Contains two query functions used as a shared service across the codebase:
  * ``lookup_findings_by_source_ref`` — find findings citing a file/PR ref.
  * ``query_findings_index`` — export a structured findings index for prompts
    and the admin UI.

The state-mutating next-cycle recovery helper lives in
``app.findings_shelving``.
"""

from datetime import timedelta
from typing import Any, Dict, List

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import resolve_dialect_name
from app.findings_shelving import RETIRED_DISPOSITION_KEY
from app.models import (
    Task,
    TaskStatus,
)
from app.research_execution_authority import (
    FINALIZED_RESEARCH_FINDINGS_KEY as _FINALIZED_RESEARCH_FINDINGS_KEY,
)
from app.source_ref_validation import _parse_ref
from app.time_utils import _utcnow

# A research task's ``plan_output`` is written once before its quality,
# adversarial, deduplication, staleness and dispatch gates run so interrupted
# spawns can resume without re-running the agent.  That pre-gate copy is not a
# findings journal: it contains findings that may later be refuted or rejected.
# The completion path rewrites ``plan_output`` to the final surviving set and
# stamps this marker only after spawn wiring is durable.  Index readers fail
# closed for legacy/unfinalized tasks instead of reviving raw agent output.
#
# Canonically defined in ``app.research_execution_authority`` (a leaf that
# ``app.task_blocked`` imports, so the dependency only goes that way) and
# re-exported here, where most readers already import it from.
FINALIZED_RESEARCH_FINDINGS_KEY = _FINALIZED_RESEARCH_FINDINGS_KEY
ACCEPTED_RESEARCH_FINDING_STATUSES = (
    TaskStatus.COMPLETED,
    TaskStatus.EVALUATING,
)

_BOUNDED_CHALLENGE_SURFACE_KEYS: frozenset[str] = frozenset(
    {
        "challenged_boundary",
        "first_verdict",
        "second_opinion_verdict",
        "agreement",
        "reason",
        "sidecar_task_id",
    }
)
_FINDING_CHALLENGE_RECORD_KEY = "finding_challenge"


def finalized_research_findings_from_state(
    plan_output: Any,
    context: Any,
    *,
    status: Any,
    task_type: Any,
) -> List[Dict[str, Any]]:
    """Return authoritative findings from an already-loaded research task state.

    Projection-heavy readers can use this form without loading the full task
    payload. The marker, accepted-analysis status and task type are all mandatory
    so a pre-gate artifact can never become accepted evidence merely because it
    has a list-shaped ``plan_output``. ``EVALUATING`` is authoritative only with
    the marker: research parents remain in delivery rollup after their analysis
    artifact and child topology have already finalized.
    """
    if not isinstance(context, dict):
        return []
    if context.get(FINALIZED_RESEARCH_FINDINGS_KEY) is not True:
        return []
    if status not in ACCEPTED_RESEARCH_FINDING_STATUSES:
        return []
    if str(task_type or "").strip().lower() != "research":
        return []
    if not isinstance(plan_output, list):
        return []
    return [finding for finding in plan_output if isinstance(finding, dict)]


def finalized_research_findings(task: Task) -> List[Dict[str, Any]]:
    """Return a research task's authoritative final findings, or ``[]``.

    Raw ``plan_output`` is intentionally ignored unless the completion handler
    stamped :data:`FINALIZED_RESEARCH_FINDINGS_KEY`.  This single read seam is
    shared by both index queries so rejected findings cannot re-enter through a
    less-visible source-ref lookup.
    """
    return finalized_research_findings_from_state(
        task.plan_output,
        task.context,
        status=task.status,
        task_type=(task.first_payload or {}).get("type"),
    )


def resolve_research_findings_for_surface_from_state(
    plan_output: Any,
    context: Any,
    *,
    status: Any,
    task_type: Any,
) -> List[Dict[str, Any]] | None:
    """Return the bounded authoritative finding list for user-facing surfaces.

    There is deliberately no artifact-log fallback. ``RESEARCH_FINDINGS`` logs
    are written before ingestion finishes and can contain refuted, stale,
    duplicate or otherwise rejected claims. The finalized task snapshot is the
    sole presentation source.
    """

    def _trim_finding(finding: Dict[str, Any]) -> Dict[str, Any]:
        challenge = finding.get(_FINDING_CHALLENGE_RECORD_KEY)
        if not isinstance(challenge, dict):
            return finding
        bounded = {
            key: value
            for key, value in challenge.items()
            if key in _BOUNDED_CHALLENGE_SURFACE_KEYS
        }
        if bounded == challenge:
            return finding
        out = dict(finding)
        out[_FINDING_CHALLENGE_RECORD_KEY] = bounded
        return out

    findings = finalized_research_findings_from_state(
        plan_output,
        context,
        status=status,
        task_type=task_type,
    )
    if not findings:
        return None
    return [_trim_finding(finding) for finding in findings]


def resolve_research_findings_for_surface(
    task: Task,
) -> List[Dict[str, Any]] | None:
    """Task-object wrapper for the authoritative presentation seam."""
    return resolve_research_findings_for_surface_from_state(
        task.plan_output,
        task.context,
        status=task.status,
        task_type=(task.first_payload or {}).get("type"),
    )


async def lookup_findings_by_source_ref(
    session: AsyncSession,
    project_id: Any,
    ref: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Return findings whose source_refs cite the given file path or PR ref.

    Searches finalized findings of completed research tasks for a source whose
    normalized path matches ref (exact) or is a child path of ref (prefix).
    Supports file refs (e.g. "backend/app/foo.py") and PR/message refs
    (matched by exact ref string).

    Returns a list of dicts with task_id, direction_id, finding_title,
    finding_description, matched_sources, and finding_index.
    """
    ref_path, _, _ = _parse_ref(ref)

    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status.in_(ACCEPTED_RESEARCH_FINDING_STATUSES),
            Task.plan_output.isnot(None),
            Task.context[FINALIZED_RESEARCH_FINDINGS_KEY].as_boolean().is_(True),
            Task.payloads[0]["type"].as_string() == "research",
        )
        .order_by(Task.created_at.desc())
        .limit(1000)
    )
    tasks = (await session.execute(stmt)).scalars().all()

    matches: List[Dict[str, Any]] = []
    for task in tasks:
        plan = finalized_research_findings(task)
        for idx, finding in enumerate(plan):
            sources = finding.get("sources") or []
            matched_sources = []
            for src in sources:
                if not isinstance(src, dict):
                    continue
                src_ref = src.get("ref", "")
                src_path, _, _ = _parse_ref(src_ref)
                if src_path == ref_path or src_path.startswith(ref_path + "/"):
                    matched_sources.append(src)
            if matched_sources:
                matches.append(
                    {
                        "task_id": str(task.id),
                        "direction_id": (task.context or {}).get("direction_id"),
                        "finding_index": idx,
                        "finding_title": finding.get("title", ""),
                        "finding_description": (finding.get("description") or "")[:200],
                        "matched_sources": matched_sources,
                    }
                )
                if len(matches) >= limit:
                    return matches
    return matches


async def query_findings_index(
    session: AsyncSession,
    project_id: Any,
    direction_id: str | None = None,
    limit: int = 50,
    days: int = 30,
) -> List[Dict[str, Any]]:
    """Return structured findings rows for the strategic cycle and admin display.

    Scans only the completion handler's finalized surviving findings for the
    project, optionally filtered to a single direction.  Pre-gate/raw
    ``plan_output`` from legacy or interrupted tasks is deliberately ignored.
    Each returned dict has:
      task_id, direction_id, completed_at (ISO), finding_title,
      finding_description, actionability_grade, claims (list), sources (list),
      finding_index — ready for prompt embedding or JSON serialisation.

    Bounded by ``days`` recency and ``limit`` total rows so prompt budgets are
    predictable.  Ordered newest-task-first, then by finding_index within task.

    When a finding carries a ``finding_challenge`` dict (recorded by the
    challenge side-car resolver), the returned row also includes a bounded
    ``challenge_summary`` key with fields: ``challenged_boundary``,
    ``agreement`` (one of ``"agrees"`` | ``"disagrees"`` | ``"indeterminate"``),
    ``second_opinion_verdict``, and ``reason`` (truncated to 200 chars).
    Unchallenged findings have no ``challenge_summary`` key.
    """
    since = _utcnow() - timedelta(days=days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status.in_(ACCEPTED_RESEARCH_FINDING_STATUSES),
            Task.plan_output.isnot(None),
            Task.context[FINALIZED_RESEARCH_FINDINGS_KEY].as_boolean().is_(True),
            Task.payloads[0]["type"].as_string() == "research",
            Task.created_at >= since,
        )
        .order_by(Task.created_at.desc())
        .limit(500)
    )
    tasks = (await session.execute(stmt)).scalars().all()

    rows: List[Dict[str, Any]] = []
    for task in tasks:
        ctx = task.context or {}
        task_dir = ctx.get("direction_id")
        if direction_id is not None and task_dir != direction_id:
            continue
        plan = finalized_research_findings(task)
        for idx, finding in enumerate(plan):
            row: Dict[str, Any] = {
                "task_id": str(task.id),
                "direction_id": task_dir,
                "completed_at": task.created_at.isoformat()
                if task.created_at
                else None,
                "finding_index": idx,
                "finding_title": finding.get("title", ""),
                "finding_description": finding.get("description", ""),
                "actionability_grade": finding.get("actionability_grade", "MEDIUM"),
                "verification_basis": finding.get(
                    "verification_basis", "pattern_inferred"
                ),
                "verification_basis_downgraded_from": finding.get(
                    "verification_basis_downgraded_from"
                ),
                "verification_basis_downgrade_reason": finding.get(
                    "verification_basis_downgrade_reason"
                ),
                "claims": finding.get("claims") or [],
                "sources": finding.get("sources") or [],
            }
            challenge = finding.get("finding_challenge")
            if isinstance(challenge, dict):
                raw_agreement = str(challenge.get("agreement") or "").lower()
                if raw_agreement in ("agrees", "disagrees"):
                    agreement = raw_agreement
                else:
                    agreement = "indeterminate"
                row["challenge_summary"] = {
                    "challenged_boundary": challenge.get("challenged_boundary"),
                    "agreement": agreement,
                    "second_opinion_verdict": challenge.get("second_opinion_verdict")
                    or challenge.get("verdict"),
                    "reason": (str(challenge.get("reason") or challenge.get("reasoning") or ""))[:200],
                }
            rows.append(row)
            if len(rows) >= limit:
                return rows
    return rows


async def query_retired_dispositions(
    session: AsyncSession,
    project_id: Any,
    limit: int = 100,
    days: int = 30,
) -> List[Dict[str, Any]]:
    """Return retired-disposition records for TTL-dropped deferred findings.

    Scans recent COMPLETED research tasks whose ``context[RETIRED_DISPOSITION_KEY]``
    carries records (written by the spawn-side recovery scan and the
    completion-side recovery block when a shelved finding is dropped at the
    ``deferred_finding_ttl_days`` TTL).  Mirrors ``query_findings_index``:
    bounded by ``days`` recency and ``limit`` total rows, dialect-aware JSON
    handling, newest-task-first ordering.

    Each returned row is one disposition record enriched with the ``task_id``
    of the task whose context holds it: finding_hash, title, direction_id,
    deferred_reason, deferred_count, retired_at, retired_by ("ttl"),
    disposition ("retired") and optional source_task_id.
    """
    since = _utcnow() - timedelta(days=days)
    if resolve_dialect_name(session) == "postgresql":
        has_dispositions = Task.context.has_key(RETIRED_DISPOSITION_KEY)
    else:
        has_dispositions = (
            func.json_type(Task.context, "$.finding_retired_dispositions") == "array"
        )
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.payloads[0]["type"].as_string() == "research",
            Task.created_at >= since,
            has_dispositions,
        )
        .order_by(Task.created_at.desc())
        .limit(500)
    )
    tasks = (await session.execute(stmt)).scalars().all()

    rows: List[Dict[str, Any]] = []
    for task in tasks:
        records = (task.context or {}).get(RETIRED_DISPOSITION_KEY)
        if not isinstance(records, list):
            continue
        for rec in records:
            if not isinstance(rec, dict):
                continue
            row: Dict[str, Any] = {"task_id": str(task.id), **rec}
            rows.append(row)
            if len(rows) >= limit:
                return rows
    return rows

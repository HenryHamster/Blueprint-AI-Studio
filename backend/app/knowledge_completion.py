"""Completion handlers for the knowledge-document task types (``web_research``
and ``design``).

A ``web_research`` task runs read-only with web access; a ``design`` task runs
read-only over a clone of the repo. Both emit a structured knowledge document
(title / summary / body / citations / tags / topic) and share ONE write path
into the durable knowledge-document store: the shared
:func:`_handle_knowledge_document_completion` seam gates the agent's output
through the forward-quality evaluator and, on a passing grade, persists exactly
ONE :class:`~app.models.KnowledgeDocument` row. The only per-kind differences are
the log tag and the default citation modality (web URLs for ``web_research``,
file references for ``design``); the structural contract
(title/summary/body + at least one usable citation) is identical, so the
parsing / persistence / citation helpers are NOT duplicated.

It is modeled closely on ``task_completion._handle_memo_completion`` — a
terminal read-only handler that marks the task COMPLETED/FAILED directly
(``web_research`` opens no PR and spawns no children, so no other lifecycle
path would promote a parked task). The difference from memo is the durable
home: a queryable ``KnowledgeDocument`` instead of a task-linked ``Artifact``.

The forward-quality gate has two tiers, exactly as the research lane does:
  * the cheap synchronous :func:`evaluate_terminal_output` structural check
    runs always; an empty / incoherent artifact fails it and the task is
    marked FAILED with the verdict recorded — it NEVER completes clean.
  * when ``research_quality_grader_enabled`` is set the document is escalated
    to an INDEPENDENT grader before it is stored: the task is parked
    EVALUATING and the grader's verdict returns via the re-entrant completion
    path (``task_lifecycle._resolve_quality_grader_completion`` →
    ``_resume_handlers['web_research']``), which re-invokes this handler with
    the verdict in ``context``. The evaluator module itself is not modified.

This is a PARALLEL lane to ``findings_index`` and the findings→spawn-code-task
path; it does not touch them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.broadcast import broadcast_to_project
from app.direct_llm_client import DIRECT_LLM_MODEL
from app.inline_llm import (
    call_inline_llm_json,
    inline_llm_unavailable_reason,
    resolve_inline_llm_settings,
)
from app.json_extract import _extract_json_object
from app.schemas import sanitize_rpc_diagnostic
from app.knowledge_query import (
    KnowledgeSubstance,
    classify_knowledge_acceptance,
    derive_compute_arm_label_from_payload,
    knowledge_substance_has_durable_compute_grounding,
)
from app.models import (
    Artifact,
    ComputeJob,
    ComputeJobStatus,
    KnowledgeDocument,
    SourceKind,
    Task,
    TaskStatus,
)
from app.task_evaluators import (
    QUALITY_GRADER_VERDICT_KEY,
    EvaluationVerdict,
    derive_release_binding_status,
    evaluate_terminal_output,
    record_failed_gate,
)

logger = logging.getLogger(__name__)

# Context key under which the parsed-but-not-yet-persisted document is stashed
# when the task escalates to an independent grader. The re-entrant resume pass
# reads the document back from here (the reconstructed ``logs`` it is handed
# carry the grader's output, not the original document). Shared by both
# knowledge-document kinds — a task is only ever one type, so a single key is
# unambiguous; the historic ``_web_research`` name is kept for back-compat.
_PENDING_DOCUMENT_KEY = "_web_research_document"

# Per-kind presentation/phrasing for the shared knowledge-document producer.
# Keyed by task-type value. ``tag`` is the log prefix; ``citation_hint`` is the
# human phrasing appended to a citation-less gate failure (the default modality
# differs: web URLs for web_research, file references for design).
_KIND_META: dict[str, dict[str, str]] = {
    "web_research": {
        "tag": "WEB_RESEARCH",
        "citation_hint": "at least one web URL or file reference is required",
    },
    "design": {
        "tag": "DESIGN",
        "citation_hint": "at least one file reference (or web URL) is required",
    },
    "research_audit": {
        "tag": "RESEARCH_AUDIT",
        "citation_hint": "at least one file reference or task reference is required",
    },
}


def _kind_tag(kind: str) -> str:
    meta = _KIND_META.get(kind)
    return meta["tag"] if meta else kind.upper()


def _source_kind(kind: str) -> str:
    """Coerce a producer ``kind`` to a stored :class:`SourceKind` value.

    Unrecognized kinds fall back to ``unknown`` so an unexpected producer never
    writes a junk discriminator into the durable store.
    """
    try:
        return SourceKind(kind).value
    except ValueError:
        return SourceKind.UNKNOWN.value


_ORIGIN_STATE_RESEARCH_FINDING = "research_finding"
_ORIGIN_STATE_NOT_RESEARCH_FINDING_DERIVED = "not_research_finding_derived"
_ORIGIN_CHALLENGE_UNCHALLENGED = "unchallenged"
_ORIGIN_CHALLENGE_CONFIRMED = "challenger_confirmed"
_ORIGIN_CHALLENGE_OVERTURNED = "overturned"
_ORIGIN_CHALLENGE_INDETERMINATE = "indeterminate_fail_closed"
_FINDING_CHALLENGE_RECORD_KEY = "finding_challenge"
_LIVE_RELEASE_EVIDENCE_KEY = "live_release_evidence"
_COMPUTE_JOB_TERMINAL_STATUSES = (
    ComputeJobStatus.SUCCEEDED,
    ComputeJobStatus.FAILED,
    ComputeJobStatus.CANCELLED,
    ComputeJobStatus.TIMED_OUT,
)
_CHALLENGE_KEYS = {
    "challenged_boundary",
    "first_verdict",
    "second_opinion_verdict",
    "agreement",
    "reason",
    "sidecar_task_id",
}


def _bounded_context_str(value: Any, *, limit: int = 128) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _bounded_finding_index(value: Any) -> int | None:
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if index >= 0 else None


def _bounded_challenge_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    record: dict[str, Any] = {}
    for key in _CHALLENGE_KEYS:
        item = value.get(key)
        if item is None:
            continue
        if key == "reason":
            record[key] = str(item)[:1000]
        else:
            record[key] = str(item)[:200]
    return record or None


def _challenge_state(record: dict[str, Any] | None) -> str:
    if not record:
        return _ORIGIN_CHALLENGE_UNCHALLENGED
    agreement = str(record.get("agreement") or "").strip().lower()
    second = str(record.get("second_opinion_verdict") or "").strip().lower()
    if agreement == "disagrees":
        return _ORIGIN_CHALLENGE_OVERTURNED
    if agreement == "agrees" and second not in {"", "unknown", "indeterminate"}:
        return _ORIGIN_CHALLENGE_CONFIRMED
    return _ORIGIN_CHALLENGE_INDETERMINATE


def _derive_origin_provenance(
    task: Task,
    *,
    source_kind: str,
) -> dict[str, Any]:
    """Derive durable, bounded research-finding origin from producer context."""
    ctx = task.context if isinstance(task.context, dict) else {}
    research_task_id = _bounded_context_str(
        ctx.get("research_task_id") or ctx.get("parent_research_task_id"), limit=64
    )
    parent_research_task_id = _bounded_context_str(
        ctx.get("parent_research_task_id") or ctx.get("research_task_id"), limit=64
    )
    finding_hash = _bounded_context_str(ctx.get("finding_hash"), limit=128)
    finding_index = _bounded_finding_index(ctx.get("finding_index"))
    has_research_origin = bool(
        research_task_id
        or parent_research_task_id
        or finding_hash
        or finding_index is not None
    )
    if source_kind not in {
        SourceKind.DESIGN.value,
        SourceKind.WEB_RESEARCH.value,
    }:
        origin_state = _ORIGIN_STATE_NOT_RESEARCH_FINDING_DERIVED
    else:
        origin_state = (
            _ORIGIN_STATE_RESEARCH_FINDING
            if has_research_origin
            else _ORIGIN_STATE_NOT_RESEARCH_FINDING_DERIVED
        )
    challenge_record = _bounded_challenge_record(ctx.get(_FINDING_CHALLENGE_RECORD_KEY))
    challenge_state = (
        _challenge_state(challenge_record)
        if origin_state == _ORIGIN_STATE_RESEARCH_FINDING
        else None
    )
    return {
        "origin_state": origin_state,
        "origin_research_task_id": research_task_id,
        "origin_parent_research_task_id": parent_research_task_id,
        "origin_finding_index": finding_index,
        "origin_finding_hash": finding_hash,
        "origin_challenge_state": challenge_state,
        "origin_challenge_record": challenge_record,
    }


def _citation_hint(kind: str) -> str:
    meta = _KIND_META.get(kind)
    return (
        meta["citation_hint"]
        if meta
        else "at least one web URL or file reference is required"
    )


# Required structural keys for a knowledge document. tags / topic are optional
# and normalized to safe defaults. At least one *usable* source citation is
# required — see :func:`_usable_citations` — because a web_research task is, by
# contract, a web-grounded, citation-backed knowledge producer.
_DOCUMENT_REQUIRED_KEYS = ("title", "summary", "body")


def _is_usable_citation(value: Any) -> bool:
    """True when ``value`` is a non-empty citation that grounds the document.

    The agent contract permits both web URLs and file references, so a usable
    citation is either an ``http(s)://`` URL or a file reference (a ``file:``
    URI or a path-like string with a ``/`` separator). Empty / whitespace-only
    strings and non-string junk are rejected — they do not ground anything.
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith(("http://", "https://", "file:")):
        return True
    # Accept a path-like file reference (e.g. ``docs/x.md``) but reject bare
    # junk tokens with no separator (e.g. ``not-a-url``).
    return "/" in text


def _is_durable_citation(value: Any) -> bool:
    """True only for citations that are durably resolvable storage URIs or file refs.

    Rejects bare ``http://``/``https://`` URLs — signed download URLs and bare
    result URLs are transient; they cannot serve as durable acceptance evidence.
    Accepts cloud-storage URIs (``gs://``, ``s3://``, ``az://``), ``file:`` URIs,
    and path-like references (containing ``/``). Metric fragment references of
    the form ``gs://...#metric:name=value`` are accepted because they are
    anchored to a durable storage URI. Empty / non-string inputs are rejected.
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith(("http://", "https://")):
        return False
    if lowered.startswith(("gs://", "s3://", "az://", "file:")):
        return True
    return "/" in text


def _usable_citations(obj: dict[str, Any]) -> list[str]:
    """Return the stripped, usable citation strings from a parsed document."""
    raw = obj.get("citations") or obj.get("source_citations") or []
    if not isinstance(raw, list):
        return []
    return [c.strip() for c in raw if _is_usable_citation(c)]


# ---------------------------------------------------------------------------
# Goal-level acceptance probe: is the document actually responsive to what was
# asked? The structural/citation gates above only check shape — a well-formed
# but off-topic document clears them. This cheap, no-dependency lexical probe
# rejects a CLEAR topical miss (essentially no keyword overlap with the asked
# topic/prompt) while erring toward acceptance whenever the signal is weak,
# mirroring the conservative fall-open philosophy of the code merge gate.
# ---------------------------------------------------------------------------

# Common words carry no topical signal; drop them before measuring overlap.
_PROBE_STOPWORDS = frozenset(
    {
        "about",
        "above",
        "across",
        "after",
        "against",
        "research",
        "current",
        "state",
        "their",
        "these",
        "those",
        "which",
        "while",
        "would",
        "could",
        "should",
        "there",
        "where",
        "what",
        "when",
        "with",
        "from",
        "that",
        "this",
        "into",
        "over",
        "under",
        "between",
        "your",
        "have",
        "been",
        "being",
        "does",
        "using",
        "used",
        "make",
        "made",
        "such",
        "than",
        "then",
        "they",
        "them",
        "will",
        "shall",
        "topic",
        "prompt",
        "report",
        "document",
        "summary",
    }
)

# Probe only judges when the request carries enough signal; below this many
# distinct content keywords there is too little to judge — accept.
_MIN_REQUEST_KEYWORDS = 3
# A clear miss is essentially-no overlap; require only a small fraction of the
# request keywords to appear so borderline-but-genuine docs are not failed.
_MIN_TOPIC_OVERLAP_RATIO = 0.1


def _keywords(text: str) -> set[str]:
    """Lowercase content keywords (alphanumeric, >=4 chars, non-stopword)."""
    return {
        w
        for w in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(w) >= 4 and w not in _PROBE_STOPWORDS
    }


def _design_grounding_miss(document: dict[str, Any]) -> str | None:
    """Return an operator-readable reason when a DESIGN document is ungrounded.

    A ``design`` document's entire value is that it is GROUNDED in the actual
    code: its file-reference citations must name real repository paths. The
    design agent already has the repo cloned, so it resolves each file citation
    against its working tree before emitting and stamps the result under
    ``_citation_grounding`` (``{"file_citations", "grounded", "checked"}``).

    This gate fails ONLY the clear case — the agent checked, the document
    carries file-reference citations, and NONE of them resolve to a real path —
    an ungrounded "design" that is not codebase-internal at all. It preserves
    the conservative errs-toward-acceptance posture everywhere else: a missing
    or unchecked stamp (older agent / web-only doc), zero file citations
    (web-URL-only), or AT LEAST ONE grounded citation all pass — so a design
    that legitimately also references not-yet-created files is NOT failed by a
    single unresolved citation. ``web_research`` docs never carry this stamp and
    are completely unaffected.
    """
    grounding = document.get("_citation_grounding")
    if not isinstance(grounding, dict) or not grounding.get("checked"):
        return None
    file_citations = int(grounding.get("file_citations") or 0)
    grounded = int(grounding.get("grounded") or 0)
    if file_citations > 0 and grounded <= 0:
        return (
            "output is not grounded in the codebase: all "
            f"{file_citations} file citation(s) resolve to paths absent from "
            "the repository"
        )
    return None


def _web_research_grounding_miss(document: dict[str, Any]) -> str | None:
    """Return an operator-readable reason when a WEB_RESEARCH doc is ungrounded.

    A ``web_research`` document's entire value is that it is GROUNDED in real,
    reachable web sources: its cited URLs must actually resolve. The web-research
    agent probes each cited URL for reachability before emitting and stamps the
    result under ``_citation_grounding`` — the SAME shape ``design`` uses, but for
    web_research ``file_citations`` is the number of URLs CHECKED, ``grounded``
    the count confirmed reachable, and ``unresolved`` the dead URL strings.

    This gate fails ONLY the clear case — the agent checked, the document cited
    URLs, and NONE of them were reachable — a "research" document grounded
    entirely in dead or fabricated links. It preserves the conservative
    errs-toward-acceptance posture everywhere else: a missing or unchecked stamp
    (older agent), zero checked URLs, or AT LEAST ONE reachable URL all pass — so
    a document with one dead link among several reachable ones is NOT failed.
    Mirrors :func:`_design_grounding_miss` exactly but with web-URL-reachability
    wording; design's reason string and behavior are untouched.
    """
    grounding = document.get("_citation_grounding")
    if not isinstance(grounding, dict) or not grounding.get("checked"):
        return None
    checked = int(grounding.get("file_citations") or 0)
    grounded = int(grounding.get("grounded") or 0)
    if checked > 0 and grounded <= 0:
        return (
            "cited web sources are unreachable: all "
            f"{checked} cited URL(s) failed reachability checks (dead or "
            "unresolvable links)"
        )
    return None


def _compute_grounding_miss(document: dict[str, Any]) -> str | None:
    """Return an operator-readable reason when a COMPUTE document is ungrounded.

    Mirrors :func:`_design_grounding_miss` for the compute-experiment lane.
    A compute document's value is that its citations resolve to REAL durable
    artifacts. The backend stamps ``_citation_grounding`` when it checks; this
    gate flags ONLY the clear case — checked, cited artifacts, NONE resolve. It
    errs toward acceptance: a missing/unchecked stamp or zero citations passes,
    so older agents and jobs without a result_reference are completely unaffected.
    This never raises and never blocks completion — it is additive observability.
    """
    grounding = document.get("_citation_grounding")
    if not isinstance(grounding, dict) or not grounding.get("checked"):
        return None
    file_citations = int(grounding.get("file_citations") or 0)
    grounded = int(grounding.get("grounded") or 0)
    if file_citations > 0 and grounded <= 0:
        return (
            "compute artifact citations are unresolved: all "
            f"{file_citations} citation(s) point to artifacts not found in the "
            "durable store"
        )
    return None


def _grounding_probe_detail(document: dict[str, Any]) -> dict[str, Any] | None:
    """Structured citation-grounding detail lifted from ``_citation_grounding``.

    Returns the grounding counts plus the specific UNRESOLVED citation strings
    the agent reported as ungrounded (the agent is the only place that can know
    which citations failed): absent file paths for ``design`` documents, dead
    URLs for ``web_research`` documents. Returns ``None`` when the stamp is
    absent or unchecked — older / un-probed documents simply omit the grounding
    key from the persisted verdict rather than carrying an empty one. Kind-
    agnostic by design, so it surfaces grounding symmetrically for both knowledge
    kinds. Purely observational: this NEVER affects the gate decision, which
    stays in :func:`_design_grounding_miss` / :func:`_web_research_grounding_miss`.
    """
    grounding = document.get("_citation_grounding")
    if not isinstance(grounding, dict) or not grounding.get("checked"):
        return None
    unresolved = grounding.get("unresolved")
    unresolved_list = (
        [str(u) for u in unresolved] if isinstance(unresolved, list) else []
    )
    return {
        "file_citations": int(grounding.get("file_citations") or 0),
        "grounded": int(grounding.get("grounded") or 0),
        "unresolved": unresolved_list,
    }


def _acceptance_probe_detail(
    task: Task, document: dict[str, Any], *, kind: str
) -> dict[str, Any]:
    """Structured detail behind the goal-level acceptance probes, for persistence.

    Carries the grounding counts + unresolved citations (present for BOTH design
    and web_research when the agent stamped a grounding result; absent only for
    older / un-probed documents) and the topic-responsiveness overlap/total
    keyword counts with
    a ``passed`` flag. The flag is derived from the SAME gate decider
    (:func:`_topic_responsiveness_miss`) so the persisted detail can never
    disagree with the pass/fail decision. This function is purely additive
    observability — it does NOT influence whether the document passes the gate.
    """
    detail: dict[str, Any] = {}

    grounding = _grounding_probe_detail(document)
    if grounding is not None:
        detail["grounding"] = grounding

    payload = task.first_payload or {}
    requested = " ".join(str(payload.get(k) or "") for k in ("topic", "prompt"))
    request_keywords = _keywords(requested)
    doc_keywords = _keywords(
        " ".join(str(document.get(k) or "") for k in ("title", "summary", "body"))
    )
    total = len(request_keywords)
    overlap = len(request_keywords & doc_keywords)
    detail["topic_responsiveness"] = {
        # Equal-by-construction to the gate: passed iff the gate found no miss.
        "passed": _topic_responsiveness_miss(task, document) is None,
        "overlap": overlap,
        "total": total,
    }
    return detail


def _verdict_with_probe_detail(
    verdict_record: dict[str, Any], task: Task, document: dict[str, Any], *, kind: str
) -> dict[str, Any]:
    """Return a copy of ``verdict_record`` with acceptance-probe detail attached.

    Additive only: the existing ``passed`` / ``reason`` / ``strategy`` /
    ``grader_task_id`` keys are preserved verbatim; the structured detail rides
    along under a new ``probes`` key. Shared by the first-pass and grader-resume
    persist paths so the persisted verdict shape is identical either way.
    """
    record = dict(verdict_record)
    record["probes"] = _acceptance_probe_detail(task, document, kind=kind)
    return record


def _topic_responsiveness_miss(task: Task, document: dict[str, Any]) -> str | None:
    """Return an operator-readable reason on a CLEAR topical miss, else ``None``.

    Cheap deterministic keyword-overlap probe between the task's requested
    topic/prompt and the document's title+summary+body. Returns ``None`` (accept)
    whenever the request carries too little signal to judge — uncertainty errs
    toward acceptance.
    """
    payload = task.first_payload or {}
    requested = " ".join(str(payload.get(k) or "") for k in ("topic", "prompt"))
    request_keywords = _keywords(requested)
    if len(request_keywords) < _MIN_REQUEST_KEYWORDS:
        return None  # too little signal in the request — accept.

    doc_keywords = _keywords(
        " ".join(str(document.get(k) or "") for k in ("title", "summary", "body"))
    )
    overlap = request_keywords & doc_keywords
    if len(overlap) / len(request_keywords) >= _MIN_TOPIC_OVERLAP_RATIO:
        return None
    return (
        "output is not responsive to the requested topic "
        f"(only {len(overlap)}/{len(request_keywords)} topic keyword(s) appear "
        "in the document title/summary/body)"
    )


def _parse_web_research_document(text: str | None) -> dict[str, Any] | None:
    """Best-effort extract the structured knowledge document from agent ``logs``.

    The agent's contract is to emit a JSON object with at least ``title``,
    ``summary`` and ``body`` (markdown). Optional ``citations`` (list),
    ``tags`` (list[str]) and ``topic`` (str) are accepted. We take either a
    bare JSON object as the whole payload, or an object embedded in surrounding
    prose (string-aware balanced-brace scan, mirroring ``_parse_memo_output``).
    """
    if not text:
        return None
    snippet = text[:500_000]

    # Direct decode — the common case where the agent posts a clean JSON body.
    try:
        parsed = json.loads(snippet)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and all(k in parsed for k in _DOCUMENT_REQUIRED_KEYS):
        return parsed

    # Fallback: scan for a balanced JSON object carrying the required keys.
    candidate = _extract_json_object(snippet, *_DOCUMENT_REQUIRED_KEYS)
    if candidate is None:
        return None
    try:
        obj = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(obj, dict) and all(k in obj for k in _DOCUMENT_REQUIRED_KEYS):
        return obj
    return None


def _normalize_document(obj: dict[str, Any], task: Task) -> dict[str, Any]:
    """Coerce a raw parsed document into a stable shape for persistence.

    Falls back to the task payload's ``topic`` / ``tags`` when the agent did
    not supply them, so operator-stamped routing context is not lost.
    """
    payload = task.first_payload or {}

    # Persist only usable citations (web URLs / file references); junk entries
    # are dropped so the stored document reflects the grounded sources only.
    citations = _usable_citations(obj)

    tags = obj.get("tags")
    if not isinstance(tags, list) or not tags:
        tags = payload.get("tags") if isinstance(payload.get("tags"), list) else []
    tags = [str(t) for t in tags[:30]]

    topic = obj.get("topic")
    if topic is None:
        topic = payload.get("topic")

    authoring_branch = obj.get("authoring_branch")
    head_sha = obj.get("head_sha")

    return {
        "title": (str(obj.get("title")) if obj.get("title") is not None else "")[:500],
        "summary": str(obj.get("summary")) if obj.get("summary") is not None else "",
        "body": str(obj.get("body")) if obj.get("body") is not None else "",
        "topic": str(topic)[:200] if topic is not None else None,
        "tags": tags,
        "source_citations": citations[:100],
        "authoring_branch": str(authoring_branch)[:255]
        if authoring_branch is not None
        else None,
        "head_sha": str(head_sha)[:40] if head_sha is not None else None,
    }


async def _fail_gate(
    task: Task,
    verdict: EvaluationVerdict,
    session: AsyncSession,
    kind: str = "web_research",
):
    """Mark ``task`` FAILED on a failing forward-quality verdict and broadcast."""
    record_failed_gate(task, verdict)
    session.add(task)
    await session.commit()
    await broadcast_to_project(
        str(task.project_id),
        "task_updated",
        {"task_id": str(task.id), "status": TaskStatus.FAILED},
    )
    logger.info(
        "[%s] Task %s did not clear the forward-quality gate: %s",
        _kind_tag(kind),
        task.id,
        verdict.reason,
    )


# Context key under which a knowledge prerequisite's durable pointer is stamped
# onto each dependent task that declared the producing task in its ``depends_on``.
# The implementation handler reads this at launch and resolves each pointer to
# full content via the agent by-id endpoint (fail-soft). Shared contract name with
# the agent runtime (``agent/src/handlers/prior-knowledge.ts``).
KNOWLEDGE_PREREQUISITES_KEY = "knowledge_prerequisites"

# Bound the dependent scan exactly as the orchestrator's _mark_unblocked_dependents
# does: ``depends_on`` is a JSON column filtered in Python, so the cap is on the raw
# QUEUED-dependent pool, ordered deterministically (oldest first).
_DEPENDENT_SCAN_CAP = 100


async def _deliver_knowledge_to_dependents(
    task: Task,
    doc: KnowledgeDocument,
    normalized: dict[str, Any],
    session: AsyncSession,
) -> None:
    """Stamp a durable knowledge pointer onto every QUEUED dependent that declared
    this producing task as a prerequisite.

    When a plan commissions a ``web_research`` / ``design`` prerequisite and makes
    an implementation task depend on it, the implementation task must receive the
    produced knowledge when it later launches. We establish that linkage HERE — at
    prerequisite completion — so it SURVIVES the dependent being released later: a
    compact pointer (``doc_id`` / ``source_task_id`` / ``source_kind`` / ``title`` /
    ``summary``) is appended to each dependent's
    ``context["knowledge_prerequisites"]`` (deduped by ``doc_id``). The
    implementation handler resolves the pointer to full content at launch via the
    agent by-id endpoint, fail-soft.

    Mirrors the orchestrator's ``_mark_unblocked_dependents`` Python-filter over the
    ``depends_on`` JSON column. Purely additive: it never changes whether/how the
    document is persisted, a project with no dependents is a no-op, and any error
    is swallowed so delivery can never break the producing task's completion.
    """
    task_id_str = str(task.id)
    if doc.source_kind == SourceKind.COMPUTE_EXPERIMENT.value:
        substance = KnowledgeSubstance(
            finding_hash=(task.context or {}).get("finding_hash"),
            topic=doc.topic,
            source_kind=doc.source_kind,
            source_citations=tuple(doc.source_citations or ()),
            forward_quality_verdict=doc.forward_quality_verdict,
        )
        if not knowledge_substance_has_durable_compute_grounding(substance):
            logger.info(
                "[%s] Skipping dependent delivery for hollow compute document %s",
                _kind_tag(doc.source_kind),
                doc.id,
            )
            return
    try:
        dependents = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.status.in_([TaskStatus.QUEUED, TaskStatus.BLOCKED]),
                        Task.project_id == task.project_id,
                        Task.depends_on.isnot(None),
                    )
                    .order_by(Task.created_at)
                    .limit(_DEPENDENT_SCAN_CAP)
                )
            )
            .scalars()
            .all()
        )
    except Exception:  # noqa: BLE001 — best-effort; delivery must never break completion
        logger.warning(
            "[%s] dependent scan failed for task %s; knowledge not delivered",
            _kind_tag(doc.source_kind),
            task.id,
            exc_info=True,
        )
        return

    pointer = {
        "doc_id": str(doc.id),
        "source_task_id": task_id_str,
        "source_kind": doc.source_kind,
        "title": normalized["title"],
        "summary": normalized["summary"],
    }
    touched = 0
    for dep in dependents:
        if task_id_str not in (dep.depends_on or []):
            continue
        ctx = dict(dep.context or {})
        existing = ctx.get(KNOWLEDGE_PREREQUISITES_KEY)
        prereqs = list(existing) if isinstance(existing, list) else []
        # Dedupe by doc id so a re-entrant completion never stamps twice.
        if any(
            isinstance(p, dict) and p.get("doc_id") == pointer["doc_id"]
            for p in prereqs
        ):
            continue
        prereqs.append(pointer)
        ctx[KNOWLEDGE_PREREQUISITES_KEY] = prereqs
        dep.context = ctx
        flag_modified(dep, "context")
        session.add(dep)
        touched += 1

    if touched:
        await session.commit()
        logger.info(
            "[%s] Delivered knowledge document %s to %d dependent task(s)",
            _kind_tag(doc.source_kind),
            doc.id,
            touched,
        )

    # Fast-path event-driven unblock: the QUEUED-dependent stamp above does NOT
    # cover BLOCKED waiters (it filters on status == QUEUED). A blocked task whose
    # kind='task' / finding-hash prerequisite is now durably satisfied by this
    # freshly-committed document would otherwise sit BLOCKED until the slow periodic
    # sweep re-checks it (often hours out, or only every 6h once escalated). The doc
    # is already committed at this point (both _persist_document and
    # _persist_compute_knowledge_document commit + refresh before delivering), so the
    # substance index sees it. Re-evaluate the project's BLOCKED set through the SAME
    # shared resolution machinery and resume any task whose needs are now satisfied.
    # Best-effort: a failure here must never break the producing task's completion.
    try:
        from app.reaper_sweeps import event_driven_unblock_on_knowledge

        await event_driven_unblock_on_knowledge(task.project_id, session)
    except Exception:  # noqa: BLE001 — best-effort; must never break completion
        logger.warning(
            "[%s] event-driven unblock-on-knowledge failed for doc %s in project %s",
            _kind_tag(doc.source_kind),
            doc.id,
            task.project_id,
            exc_info=True,
        )


async def _persist_document(
    task: Task,
    document: dict[str, Any],
    verdict_record: dict[str, Any],
    session: AsyncSession,
    kind: str = "web_research",
) -> None:
    """Persist exactly one KnowledgeDocument, mirror a compact copy, mark COMPLETED.

    Idempotent on replay: if a document with the same (project_id, source_task_id,
    source_kind) already exists, the call is a no-op; the existing row is not
    updated. This guards against double-insertion on post-commit recovery replay.
    """
    normalized = _normalize_document(document, task)

    # The strategic direction that motivated the producing task is carried on
    # ``task.context["direction_id"]`` (a string direction key, not a FK).
    # Persisting it is purely additive — it never changes whether the document
    # is stored — and lets a direction's accumulated knowledge be discovered.
    direction_id = (task.context or {}).get("direction_id")
    direction_id = str(direction_id) if direction_id else None
    source_kind = _source_kind(kind)
    origin = _derive_origin_provenance(task, source_kind=source_kind)

    # Idempotency guard: check if a document with the same (project_id,
    # source_task_id, source_kind) already exists. If so, skip insertion.
    # This prevents replay from double-inserting the same document.
    existing = (
        (
            await session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.project_id == task.project_id,
                    KnowledgeDocument.source_task_id == task.id,
                    KnowledgeDocument.source_kind == source_kind,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        logger.info(
            "[%s] Knowledge document already exists (%s) for task %s — skipping "
            "insertion on replay",
            _kind_tag(kind),
            existing.id,
            task.id,
        )
        doc = existing
    else:
        # Design and web_research docs participate in the follow-up lifecycle:
        # they are created with lifecycle_status="pending" so the strategist
        # knows they exist and haven't been consumed/dismissed yet.
        # Omit lifecycle_status for non-participating kinds — the column is NOT
        # NULL (server_default='pending'), and the query already filters by
        # source_kind, so no sentinel value is needed.
        doc = KnowledgeDocument(
            project_id=task.project_id,
            source_task_id=task.id,
            direction_id=direction_id,
            source_kind=source_kind,
            title=normalized["title"],
            summary=normalized["summary"],
            body=normalized["body"],
            topic=normalized["topic"],
            tags=normalized["tags"],
            source_citations=normalized["source_citations"],
            forward_quality_verdict=verdict_record,
            authoring_branch=normalized.get("authoring_branch"),
            head_sha=normalized.get("head_sha"),
            **origin,
        )
        if source_kind in {SourceKind.DESIGN.value, SourceKind.WEB_RESEARCH.value}:
            doc.lifecycle_status = "pending"
        session.add(doc)

    # Mirror a compact copy into plan_output for debugging / admin visibility
    # (the body is intentionally omitted to keep plan_output small). The
    # document ``id`` and forward-quality ``verdict`` are carried forward so the
    # task detail view can link straight to the produced document and render its
    # gate verdict without a second lookup.
    plan_output = task.plan_output if isinstance(task.plan_output, dict) else {}
    plan_output = dict(plan_output)
    plan_output["knowledge_document"] = {
        "id": str(doc.id),
        "title": normalized["title"],
        "summary": normalized["summary"],
        "topic": normalized["topic"],
        "tags": normalized["tags"],
        "direction_id": direction_id,
        "citation_count": len(normalized["source_citations"]),
        "forward_quality_verdict": verdict_record,
        "origin_provenance": {
            "origin_state": origin["origin_state"],
            "research_task_id": origin["origin_research_task_id"],
            "parent_research_task_id": origin["origin_parent_research_task_id"],
            "finding_index": origin["origin_finding_index"],
            "finding_hash": origin["origin_finding_hash"],
            "direction_id": direction_id,
            "challenge_state": origin["origin_challenge_state"],
            "finding_challenge": origin["origin_challenge_record"],
        },
    }
    task.plan_output = plan_output
    flag_modified(task, "plan_output")

    # Clear the escalation stash now that the document is durable.
    ctx = dict(task.context or {})
    ctx.pop(_PENDING_DOCUMENT_KEY, None)
    task.context = ctx
    flag_modified(task, "context")

    task.status = TaskStatus.COMPLETED
    task.latest_status_message = (
        f"Knowledge document recorded ({len(normalized['source_citations'])} "
        f"citation(s), {len(normalized['tags'])} tag(s))"
    )[:500]
    session.add(task)
    await session.commit()
    await session.refresh(doc)

    # Close the knowledge-prerequisite loop: link this freshly-persisted document
    # to any QUEUED dependent that commissioned it, so the dependent launches with
    # the produced knowledge. Established at completion so it survives the dependent
    # dispatching later. Best-effort — never blocks the producing task's completion.
    await _deliver_knowledge_to_dependents(task, doc, normalized, session)

    await broadcast_to_project(
        str(task.project_id),
        "task_updated",
        {"task_id": str(task.id), "status": TaskStatus.COMPLETED},
    )
    logger.info(
        "[%s] Persisted knowledge document %s for task %s",
        _kind_tag(kind),
        doc.id,
        task.id,
    )


async def _handle_knowledge_document_completion(
    task: Task, logs: str, session: AsyncSession, *, kind: str
) -> None:
    """Gate a knowledge-document task's output and persist on pass.

    Shared seam for the ``web_research`` and ``design`` task types: both produce
    the same structured knowledge document and write into the same store; only
    the ``kind`` (log tag + default citation modality) differs. Two entry modes:
      * FIRST pass (no grader verdict in context): run the cheap synchronous
        forward-quality gate; on pass, optionally escalate to an independent
        grader (parking EVALUATING), otherwise persist the document.
      * RESUME pass (grader verdict present): persist on a passing grade, or
        fail the task on a failing grade — never complete clean on a fail.
    """
    ctx = dict(task.context or {})
    grader_verdict = ctx.get(QUALITY_GRADER_VERDICT_KEY)

    # ---- RESUME pass: an independent grader has returned a verdict. ----------
    if isinstance(grader_verdict, dict):
        if not grader_verdict.get("passed"):
            await _fail_gate(
                task,
                EvaluationVerdict(
                    False,
                    str(grader_verdict.get("reason") or "independent grader failed"),
                    "forward-quality",
                ),
                session,
                kind,
            )
            return
        document = ctx.get(_PENDING_DOCUMENT_KEY) or _parse_web_research_document(logs)
        if not isinstance(document, dict):
            await _fail_gate(
                task,
                EvaluationVerdict(
                    False,
                    "graded document is no longer recoverable for persistence",
                    "forward-quality",
                ),
                session,
                kind,
            )
            return
        # Defense-in-depth: a graded document must still carry a usable citation.
        if not _usable_citations(document):
            await _fail_gate(
                task,
                EvaluationVerdict(
                    False,
                    f"{kind} output has no usable source citation "
                    f"({_citation_hint(kind)})",
                    "forward-quality",
                ),
                session,
                kind,
            )
            return
        await _persist_document(
            task,
            document,
            _verdict_with_probe_detail(grader_verdict, task, document, kind=kind),
            session,
            kind,
        )
        return

    # ---- FIRST pass: run the cheap synchronous structural gate. --------------
    verdict = evaluate_terminal_output(task, logs)
    if not verdict.passed:
        await _fail_gate(task, verdict, session, kind)
        return

    document = _parse_web_research_document(logs)
    if document is None:
        # The cheap gate passed but the output is not a parseable document —
        # treat as a gate failure rather than completing clean.
        await _fail_gate(
            task,
            EvaluationVerdict(
                False,
                f"{kind} output is not a parseable knowledge document "
                "(missing title/summary/body)",
                "forward-quality",
            ),
            session,
            kind,
        )
        return

    # A knowledge document MUST be grounded: at least one usable source citation
    # (web URL or file reference). Enforce BEFORE grader escalation or
    # persistence so an ungrounded summary never parks for grading nor persists.
    if not _usable_citations(document):
        await _fail_gate(
            task,
            EvaluationVerdict(
                False,
                f"{kind} output has no usable source citation ({_citation_hint(kind)})",
                "forward-quality",
            ),
            session,
            kind,
        )
        return

    # Codebase-grounding probe (design only): a design document whose file
    # citations resolve to ZERO real repository paths is not codebase-internal
    # design and must not ship as an authoritative artifact. Distinct from the
    # "no usable citation" gate above (the citations are well-shaped) and the
    # topical-miss gate below (the document is on-topic). Errs toward acceptance
    # — a single broken citation among several never fails a grounded design.
    if kind == "design":
        grounding_miss = _design_grounding_miss(document)
        if grounding_miss is not None:
            await _fail_gate(
                task,
                EvaluationVerdict(False, f"{kind} {grounding_miss}", "forward-quality"),
                session,
                kind,
            )
            return
    elif kind == "web_research":
        # Web-citation grounding probe: a web_research document whose cited URLs
        # are ALL unreachable is grounded in dead/fabricated links and must not
        # ship as authoritative knowledge. Symmetric to the design branch above,
        # reading the SAME ``_citation_grounding`` stamp; errs toward acceptance
        # — one reachable URL among several dead ones never fails the gate.
        grounding_miss = _web_research_grounding_miss(document)
        if grounding_miss is not None:
            await _fail_gate(
                task,
                EvaluationVerdict(False, f"{kind} {grounding_miss}", "forward-quality"),
                session,
                kind,
            )
            return

    # Goal-level acceptance probe: a cheap, deterministic check that the
    # document is actually responsive to the asked topic/prompt before it is
    # accepted as done. Err toward acceptance — only a CLEAR miss fails here.
    miss_reason = _topic_responsiveness_miss(task, document)
    if miss_reason is not None:
        await _fail_gate(
            task,
            EvaluationVerdict(False, f"{kind} {miss_reason}", "forward-quality"),
            session,
            kind,
        )
        return

    # Optional blast-radius-proportional escalation: stand up an INDEPENDENT
    # grader and park EVALUATING. The verdict returns via the re-entrant
    # completion path, which re-invokes this handler (RESUME pass above).
    from app.shared_state import runtime_config as _runtime_config

    if _runtime_config.get("research_quality_grader_enabled", False):
        from app.task_evaluators import spawn_forward_quality_grader

        # Stash the parsed document so the resume pass can persist it without
        # re-parsing the (grader-derived) resume logs.
        ctx[_PENDING_DOCUMENT_KEY] = document
        task.context = ctx
        flag_modified(task, "context")
        session.add(task)
        await session.commit()

        payload = task.first_payload or {}
        question = " ".join(
            str(payload.get(k) or "") for k in ("topic", "prompt")
        ).strip()
        grader = await spawn_forward_quality_grader(
            task, json.dumps(document), session, question=question or None
        )
        if grader is not None:
            logger.info(
                "[%s] Task %s parked awaiting forward-quality grade "
                "(grader %s) before persisting knowledge document",
                _kind_tag(kind),
                task.id,
                grader.id,
            )
            return
        # Grader not spawned (already graded / in flight) — fall through and
        # persist with the cheap-gate verdict.

    verdict_record = _verdict_with_probe_detail(
        {
            "passed": True,
            "reason": verdict.reason,
            "strategy": verdict.strategy,
            "grader_task_id": None,
        },
        task,
        document,
        kind=kind,
    )
    await _persist_document(task, document, verdict_record, session, kind)


# ---------------------------------------------------------------------------
# Compute-experiment knowledge: completion persists the deterministic
# artifact-grounded document. Cited LLM analysis is an explicit optional upgrade.
# ---------------------------------------------------------------------------

# Opt-out switch in the same spirit as DISABLE_LLM_REVIEW_CATEGORIZATION.
_DISABLE_LLM_COMPUTE_ANALYSIS_ENV = "DISABLE_LLM_COMPUTE_ANALYSIS"
# Bound any free text (log excerpt) folded into the prompt so a runaway command
# log can never blow up the analysis prompt.
_COMPUTE_LOG_TEXT_CAP = 4000
_COMPUTE_MAX_CITATIONS = 20


def _enrich_per_arm_cost(
    per_arm_verdicts: dict[str, dict[str, Any]],
    arms: Any,
    total_cost: float | None,
) -> None:
    """Add apportioned cost_usd to each per-arm verdict entry, in-place.

    Apportions total_cost by elapsed_seconds fraction when available in the arms
    block; falls back to an even split when durations are missing, and leaves
    cost_usd None when total_cost is None.
    """
    if not per_arm_verdicts:
        return
    if total_cost is None:
        for pav in per_arm_verdicts.values():
            pav["cost_usd"] = None
        return

    elapsed: dict[str, float] = {}
    arm_iter: list[tuple[str, Any]] = []
    if isinstance(arms, dict):
        arm_iter = list(arms.items())
    elif isinstance(arms, list):
        arm_iter = [
            (a.get("name") or a.get("label") or a.get("arm") or f"arm_{i}", a)
            for i, a in enumerate(arms)
            if isinstance(a, dict)
        ]
    for arm_name, arm_data in arm_iter:
        if isinstance(arm_data, dict):
            e = arm_data.get("elapsed_seconds")
            if isinstance(e, (int, float)) and e >= 0:
                elapsed[str(arm_name)] = float(e)

    total_elapsed = sum(elapsed.values())
    n_arms = len(per_arm_verdicts)

    for arm_name, pav in per_arm_verdicts.items():
        if total_elapsed > 0:
            # Time-weighted split: only apportion arms that have elapsed data.
            # Arms without timing (e.g. crashed before finishing) get None so
            # the timed arms' costs don't over-count beyond the total.
            arm_cost = (
                total_cost * elapsed[arm_name] / total_elapsed
                if arm_name in elapsed
                else None
            )
        else:
            arm_cost = total_cost / n_arms if n_arms > 0 else None
        pav["cost_usd"] = arm_cost


def _evaluate_criterion_per_arm(
    criterion: dict[str, Any], arms: Any
) -> dict[str, dict[str, Any]] | None:
    """Evaluate criterion against each arm and return per-arm verdicts.

    Returns a dict mapping arm name → {"value": metric_value, "met": bool}, or
    None if no criterion/arms or evaluation is not possible. Mirrors the per-arm
    metric extraction logic in task_evaluators._find_metric_in_arms.
    """
    if not isinstance(criterion, dict) or arms is None:
        return None

    metric = criterion.get("metric")
    op = criterion.get("operator")
    threshold = criterion.get("threshold")
    if not metric or not op or threshold is None:
        return None

    # Import the criterion_met helper from task_evaluators
    from app.task_evaluators import criterion_met as _criterion_met, _coerce_number

    per_arm: dict[str, dict[str, Any]] = {}
    arm_items: list[tuple[str, Any]] = []

    # Handle both dict and list arm shapes
    if isinstance(arms, dict):
        arm_items = list(arms.items())
    elif isinstance(arms, list):
        arm_items = [
            (a.get("name") or a.get("label") or a.get("arm") or f"arm_{i}", a)
            for i, a in enumerate(arms)
            if isinstance(a, dict)
        ]

    for arm_name, arm_data in arm_items:
        if not isinstance(arm_data, dict):
            continue

        # Extract metric value from arm, trying exact match then case-insensitive
        raw_value = arm_data.get(metric)
        if raw_value is None:
            # Try case-insensitive lookup
            for key in arm_data:
                if key.lower() == metric.lower():
                    raw_value = arm_data[key]
                    break

        value = _coerce_number(raw_value)
        # Distinguish unmeasured (crashed/missing) from failed: None means the
        # arm didn't report the metric at all, not that it failed the criterion.
        met = _criterion_met(raw_value, op, threshold) if value is not None else None
        per_arm[str(arm_name)] = {"value": value, "met": met}

    return per_arm if per_arm else None


def _compute_evidence_tier(
    job: Any | None,
    result_metrics: dict[str, Any] | None,
) -> str:
    """Compute the evidence tier for a compute-experiment knowledge document.

    Uses the ComputeJob's backend attribute as the primary signal so that
    both successful and failed runs are classified correctly.
    """
    if job is None:
        return "backfilled" if result_metrics else "uncovered"
    backend = getattr(job, "backend", None) or ""
    if backend.startswith("local-") or backend == "runner-subprocess":
        return "covered"
    if backend:
        return "attested"
    if result_metrics:
        return "backfilled"
    return "partial"


def _build_compute_experiment_document(
    task: Task,
    *,
    outcome: str,
    cost: float | None,
    criterion: dict[str, Any] | None,
    result_reference: str | None,
    terminal_diagnostics: dict[str, Any] | None = None,
    result_metrics: dict[str, Any] | None = None,
    arms: Any | None = None,
    evidence_tier: str | None = None,
) -> dict[str, Any]:
    """Build the DETERMINISTIC compute-experiment knowledge document.

    This is the hardcoded outcome/cost/criterion template, lifted into a helper
    so it can be persisted at completion and handed to the explicit LLM analysis
    pass as the structured facts to ground its prose in. Returns a dict of
    ``title``/``summary``/``body``/``topic``/``tags``/``citations`` plus the
    metric ``verdict_record`` so the persisted shape is identical to before.

    ``terminal_diagnostics`` is an optional dict extracted from the ComputeJob row
    (``terminal_reason``, ``vm_phase``, ``accelerator_type``, ``machine_type``).
    When present it is appended to the human-readable summary and body and stored
    in ``verdict_record`` so a failed run is attributable at a glance.

    ``result_metrics`` (the experiment's reported numbers) and ``arms`` (an A/B /
    multi-arm comparison block, when the experiment reported one) are captured
    FAITHFULLY — verbatim, no interpretation — into the body and the
    ``verdict_record`` so the strategist and the later opt-in judgment agent can
    query the real result rather than a truncated narrative. Supports arms-only
    results (no top-level scalars, only an ``arms`` block): scalar-only results,
    mixed results with both scalars and arms, and arms-only results all render
    correctly with appropriate sections in the body.
    """
    payload = task.first_payload or {}
    experiment = str(payload.get("experiment") or "").strip()
    benchmark = str(payload.get("benchmark") or "").strip()
    identity = experiment or benchmark or "compute experiment"

    crit_met = criterion.get("met") if isinstance(criterion, dict) else None

    if crit_met is True:
        verdict_phrase = "criterion met"
    elif crit_met is False:
        verdict_phrase = "criterion NOT met"
    else:
        verdict_phrase = outcome
    title = f"Compute experiment: {identity} — {verdict_phrase}"[:500]

    summary_parts = [f"Outcome: {outcome}."]
    if isinstance(criterion, dict):
        metric = criterion.get("metric")
        op = criterion.get("operator")
        threshold = criterion.get("threshold")
        value = criterion.get("value")
        if crit_met is True:
            summary_parts.append(
                f"Success criterion MET: {metric}={value} (required {op} {threshold})."
            )
        elif criterion.get("measurable"):
            summary_parts.append(
                f"Success criterion NOT met: {metric}={value} "
                f"(required {op} {threshold})."
            )
        else:
            summary_parts.append(
                f"Success criterion NOT evaluable: {metric} "
                f"(required {op} {threshold}) was not reported."
            )
    if cost is not None:
        summary_parts.append(f"Cost: ${cost}.")
    if terminal_diagnostics:
        td_reason = terminal_diagnostics.get("terminal_reason")
        td_phase = terminal_diagnostics.get("vm_phase")
        td_cause = terminal_diagnostics.get("provisioning_cause")
        td_contract = terminal_diagnostics.get("launch_contract")
        if td_reason:
            phase_clause = f"; VM phase: {td_phase}" if td_phase else ""
            summary_parts.append(f"Terminal diagnostics: {td_reason}{phase_clause}.")
        if td_cause:
            cause_parts = []
            if td_cause.get("error_type"):
                cause_parts.append(f"type={td_cause['error_type']}")
            if td_cause.get("error_message"):
                cause_parts.append(f"message={td_cause['error_message'][:200]}")
            if td_cause.get("reason"):
                cause_parts.append(f"reason={td_cause['reason']}")
            if cause_parts:
                summary_parts.append(f"Provisioning failure: {'; '.join(cause_parts)}.")
        if td_contract:
            contract_parts = []
            if td_contract.get("requested_deadline_seconds") is not None:
                contract_parts.append(
                    f"deadline={td_contract['requested_deadline_seconds']}s"
                )
            if td_contract.get("provisioning_model"):
                contract_parts.append(f"model={td_contract['provisioning_model']}")
            if contract_parts:
                summary_parts.append(f"Launch contract: {', '.join(contract_parts)}.")
        td_backend = terminal_diagnostics.get("executing_backend")
        if td_backend:
            summary_parts.append(f"Backend: {td_backend}.")
        td_runner = terminal_diagnostics.get("runner_target_id")
        if td_runner:
            td_runner_name = terminal_diagnostics.get("runner_hostname")
            label = f"{td_runner_name} ({td_runner})" if td_runner_name else td_runner
            summary_parts.append(f"Runner: {label}.")
    summary = " ".join(summary_parts)

    body_lines = [
        f"# Compute experiment verdict: {identity}",
        "",
        f"- **Outcome:** {outcome}",
    ]
    if benchmark:
        body_lines.append(f"- **Benchmark:** {benchmark}")
    if isinstance(criterion, dict):
        body_lines.append(
            f"- **Success criterion:** `{criterion.get('metric')} "
            f"{criterion.get('operator')} {criterion.get('threshold')}`"
        )
        body_lines.append(f"- **Measured value:** {criterion.get('value')}")
        body_lines.append(f"- **Criterion met:** {crit_met}")
        body_lines.append(f"- **Measurable:** {criterion.get('measurable')}")
    if cost is not None:
        body_lines.append(f"- **Cost (USD):** {cost}")
    if result_reference:
        body_lines.append(f"- **Result reference:** {result_reference}")
    if terminal_diagnostics:
        td_reason = terminal_diagnostics.get("terminal_reason")
        td_phase = terminal_diagnostics.get("vm_phase")
        td_accel = terminal_diagnostics.get("accelerator_type")
        td_machine = terminal_diagnostics.get("machine_type")
        td_backend = terminal_diagnostics.get("executing_backend")
        td_runner = terminal_diagnostics.get("runner_target_id")
        td_runner_hostname = terminal_diagnostics.get("runner_hostname")
        td_cause = terminal_diagnostics.get("provisioning_cause")
        td_contract = terminal_diagnostics.get("launch_contract")
        if td_reason:
            body_lines.append(f"- **Terminal reason:** {td_reason}")
        if td_phase:
            body_lines.append(f"- **VM phase:** {td_phase}")
        if td_accel:
            body_lines.append(f"- **Accelerator type:** {td_accel}")
        if td_machine:
            body_lines.append(f"- **Machine type:** {td_machine}")
        if td_backend:
            body_lines.append(f"- **Executing backend:** {td_backend}")
        if td_runner:
            runner_label = f"{td_runner_hostname} (`{td_runner}`)" if td_runner_hostname else td_runner
            body_lines.append(f"- **Runner:** {runner_label}")
        if td_cause:
            if td_cause.get("error_type"):
                body_lines.append(
                    f"- **Provisioning failure type:** {td_cause['error_type']}"
                )
            if td_cause.get("error_message"):
                body_lines.append(
                    f"- **Provisioning failure message:** {td_cause['error_message'][:300]}"
                )
            if td_cause.get("reason"):
                body_lines.append(
                    f"- **Provisioning failure reason:** {td_cause['reason']}"
                )
        if td_contract:
            if td_contract.get("requested_deadline_seconds") is not None:
                body_lines.append(
                    f"- **Requested deadline (s):** {td_contract['requested_deadline_seconds']}"
                )
            if td_contract.get("provisioning_model"):
                body_lines.append(
                    f"- **Provisioning model:** {td_contract['provisioning_model']}"
                )
    # Faithful capture of the reported numbers and any A/B arms — verbatim, no
    # interpretation. The flattened scalar metrics render as a list; the arms
    # block (mapping / list of arm records) is preserved as a JSON fence so a
    # multi-arm result survives intact for downstream comparison.
    if result_metrics:
        scalar_metrics = {k: v for k, v in result_metrics.items() if k != "arms"}
        if scalar_metrics:
            body_lines.append("")
            body_lines.append("## Reported result metrics")
            for k, v in scalar_metrics.items():
                body_lines.append(f"- **{k}:** {v}")
    if arms is not None:
        body_lines.append("")
        body_lines.append("## Experiment arms")
        body_lines.append("```json")
        body_lines.append(json.dumps(arms, sort_keys=True, default=str))
        body_lines.append("```")
        # Append human-readable failure summary for any arm that failed so a
        # strategist can see which arms crashed and why without parsing JSON.
        failed_lines: list[str] = []
        arm_items: list[tuple[str, Any]] = []
        if isinstance(arms, dict):
            arm_items = list(arms.items())
        elif isinstance(arms, list):
            arm_items = [(a.get("name") or a.get("label") or a.get("arm") or f"arm_{i}", a)
                         for i, a in enumerate(arms) if isinstance(a, dict)]
        for arm_name, arm_data in arm_items:
            if not isinstance(arm_data, dict):
                continue
            arm_exit = arm_data.get("exit_code")
            arm_error = arm_data.get("error") or arm_data.get("reason")
            arm_stderr = arm_data.get("stderr_excerpt")
            if (arm_exit is not None and arm_exit != 0) or arm_error:
                parts: list[str] = [f"**{arm_name}** FAILED"]
                if arm_exit is not None:
                    parts.append(f"exit {arm_exit}")
                if arm_error:
                    parts.append(str(arm_error)[:200])
                line = ": ".join(parts)
                if arm_stderr:
                    line += f" | stderr: {arm_stderr[:200]}"
                failed_lines.append(f"- {line}")
        if failed_lines:
            body_lines.append("")
            body_lines.append("### Failed arms")
            body_lines.extend(failed_lines)

        # Append per-arm criterion verdicts when a criterion is declared
        per_arm_verdicts = _evaluate_criterion_per_arm(criterion, arms)
        if per_arm_verdicts:
            body_lines.append("")
            body_lines.append("### Per-arm criterion evaluation")
            for arm_name, verdict in per_arm_verdicts.items():
                value = verdict.get("value")
                met = verdict.get("met")
                if met is None:
                    status = "— UNMEASURED"
                elif met:
                    status = "✓ MET"
                else:
                    status = "✗ NOT MET"
                if value is not None:
                    body_lines.append(f"- **{arm_name}:** {status} ({criterion.get('metric')}={value})")
                else:
                    body_lines.append(f"- **{arm_name}:** {status} ({criterion.get('metric')}=unmeasured)")
    body = "\n".join(body_lines)

    raw_tags = payload.get("tags")
    base_tags = [str(t) for t in raw_tags[:28]] if isinstance(raw_tags, list) else []
    tags = list(dict.fromkeys([*base_tags, "compute_experiment", outcome]))[:30]

    topic = payload.get("topic") or experiment or benchmark or payload.get("metric")
    topic = str(topic)[:200] if topic else None

    citations = (
        [result_reference]
        if result_reference and _is_durable_citation(result_reference)
        else []
    )

    verdict_record: dict[str, Any] = {
        "strategy": "metric",
        "outcome": outcome,
        "cost_usd": cost,
        "criterion": criterion,
        "criterion_met": crit_met,
    }
    if evidence_tier is not None:
        verdict_record["evidence_tier"] = evidence_tier
    if terminal_diagnostics:
        verdict_record["terminal_diagnostics"] = terminal_diagnostics
    # Faithful, queryable home for the real numbers and any A/B arms so future
    # cycles can act on the measured result without re-running the experiment.
    if result_metrics:
        verdict_record["result_metrics"] = result_metrics
    if arms is not None:
        verdict_record["arms"] = arms
    # Per-arm criterion verdicts for deterministic querying and rendering
    per_arm_verdicts = _evaluate_criterion_per_arm(criterion, arms)
    if per_arm_verdicts:
        _enrich_per_arm_cost(per_arm_verdicts, arms, cost)
        verdict_record["per_arm_verdicts"] = per_arm_verdicts

    return {
        "title": title,
        "summary": summary,
        "body": body,
        "topic": topic,
        "tags": tags,
        "citations": citations,
        "verdict_record": verdict_record,
    }


async def _load_compute_job_from_logs(
    logs: str | None, session: AsyncSession
) -> ComputeJob | None:
    """Load the ComputeJob referenced by the terminal COMPUTE_RESULT ``logs``.

    The agent's terminal artifact carries the originating ``job_id``; we parse it
    and load the row so the analysis pass can reach the preserved metrics and the
    durable artifact linkage. Returns ``None`` for unparseable logs, a missing /
    non-UUID ``job_id``, or a row that no longer exists — every such case simply
    falls the caller back to the deterministic document.
    """
    try:
        payload = await asyncio.to_thread(json.loads, logs) if logs else None
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("job_id")
    if raw is None:
        return None
    try:
        job_id = uuid.UUID(str(raw))
    except (TypeError, ValueError):
        return None
    try:
        return await session.get(ComputeJob, job_id)
    except Exception:  # noqa: BLE001 — analysis must never break completion
        return None


async def _compute_citation_grounding_stamp(
    result_reference: str | None,
    logs: str | None,
    session: AsyncSession,
) -> dict[str, Any]:
    """Build a citation-grounding stamp for the deterministic document.

    The deterministic document's only citation is the ``result_reference``. This
    helper checks whether that reference is backed by a real durable :class:`Artifact`
    row by loading the :class:`ComputeJob` from ``logs`` and verifying its
    ``artifact_id``. Returns a ``_citation_grounding`` dict using the same shape
    as the design / web_research lanes so :func:`_grounding_probe_detail` and
    :func:`_compute_grounding_miss` apply symmetrically. Never raises — any
    storage error is treated as unresolved, which is the conservative choice.
    """
    if not result_reference or not _is_usable_citation(result_reference):
        # No citation to probe — 0 checked means the miss gate won't fire.
        return {"checked": True, "file_citations": 0, "grounded": 0, "unresolved": []}

    grounded = 0
    job = await _load_compute_job_from_logs(logs, session)
    if job is not None and job.artifact_id is not None:
        try:
            artifact = await session.get(Artifact, job.artifact_id)
            if artifact is not None:
                # Require the result_reference to actually match the artifact's
                # durable storage URI — bare artifact-row presence is not enough.
                storage_prefix = (artifact.storage_uri or "").rstrip("/")
                ref = (result_reference or "").rstrip("/")
                if storage_prefix and (
                    ref == storage_prefix or ref.startswith(storage_prefix + "/")
                ):
                    grounded = 1
        except Exception:  # noqa: BLE001 — best-effort, never breaks completion
            pass

    return {
        "checked": True,
        "file_citations": 1,
        "grounded": grounded,
        "unresolved": [] if grounded > 0 else [result_reference],
    }


async def _gather_compute_evidence(
    task: Task,
    *,
    logs: str | None,
    result_reference: str | None,
    criterion: dict[str, Any] | None,
    session: AsyncSession,
) -> dict[str, Any] | None:
    """Assemble the durable analysis inputs that already exist on disk.

    Loads the ComputeJob from the terminal logs, reads its preserved numeric
    metrics (``polling_metadata['result_metrics']``) and durable-artifact linkage
    (the auto-populated COMPUTE_RESULT :class:`Artifact` row whose ``.logs`` JSON
    carries ``artifact_uri``/``file_count``), and builds the candidate citation
    set the LLM is allowed to ground its analysis in: the artifact bundle
    ``gs://`` URI, an optional signed download URL, a per-metric reference, and
    the ``result_reference``.

    Returns ``None`` whenever there are no usable durable artifacts — an empty
    prefix, a failed job with nothing durable, or no parseable job linkage at all.
    ``real_evidence`` is the
    subset of citations that point at the artifact/metric — i.e. everything BUT a
    bare ``result_reference`` — so the caller can require the analysis to cite at
    least one piece of real evidence rather than only re-stating the reference.
    """
    job = await _load_compute_job_from_logs(logs, session)
    if job is None:
        return None

    metrics: dict[str, Any] = {}
    arms: Any | None = None
    raw_metrics = (job.polling_metadata or {}).get("result_metrics")
    if isinstance(raw_metrics, dict):
        metrics = {
            str(k): v
            for k, v in raw_metrics.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        arms = _extract_experiment_arms(raw_metrics)

    artifact_uri: str | None = None
    storage_uri: str | None = None
    file_count = 0
    art_logs: dict | None = None
    if job.artifact_id is not None:
        try:
            artifact = await session.get(Artifact, job.artifact_id)
        except Exception:  # noqa: BLE001
            artifact = None
        if artifact is not None:
            storage_uri = (artifact.storage_uri or "").strip() or None
            try:
                art_logs = (
                    await asyncio.to_thread(json.loads, artifact.logs)
                    if artifact.logs
                    else None
                )
            except (json.JSONDecodeError, ValueError):
                art_logs = None
            if isinstance(art_logs, dict):
                uri = str(art_logs.get("artifact_uri") or "").strip()
                artifact_uri = uri or None
                try:
                    file_count = int(art_logs.get("file_count") or 0)
                except (TypeError, ValueError):
                    file_count = 0

    # The durable artifact location is the canonical evidence pointer when one
    # exists. Metric-only runner/local jobs can still be questioned directly from
    # their preserved result_metrics even if no artifact row was produced.
    job_reference = (job.result_reference or "").strip() or None
    artifact_uri = artifact_uri or job_reference or storage_uri
    has_metric_evidence = bool(metrics)
    if artifact_uri is None and not has_metric_evidence:
        return None
    if artifact_uri is not None and not _is_durable_citation(artifact_uri):
        return None
    if file_count <= 0 and not metrics:
        # No durable files survived AND no preserved metric — nothing the LLM can
        # honestly analyse beyond the bare verdict; fall back.
        return None

    # Candidate citations the analysis is allowed to cite. Membership is enforced
    # after the LLM responds so the document can only cite real, durable evidence.
    # real_evidence: durable storage URIs / metric refs the LLM MUST cite at least
    # one of to pass the gate. supplementary: transient helpers (e.g. signed URLs)
    # included in the prompt for LLM convenience but excluded from the gate check.
    real_evidence: list[str] = []
    supplementary: list[str] = []
    seen: set[str] = set()

    def _add_real(value: str | None) -> None:
        if value and _is_durable_citation(value) and value not in seen:
            seen.add(value)
            real_evidence.append(value)

    def _add_supplementary(value: str | None) -> None:
        if value and _is_usable_citation(value) and value not in seen:
            seen.add(value)
            supplementary.append(value)

    def _add_metric(value: str | None) -> None:
        if value and value not in seen:
            seen.add(value)
            real_evidence.append(value)

    if artifact_uri is not None:
        _add_real(artifact_uri)
        if storage_uri and storage_uri != artifact_uri:
            _add_real(storage_uri)

    # Signed download URLs are transient (they expire); include them in the LLM
    # prompt as supplementary context but NOT in real_evidence so a signed URL
    # alone cannot satisfy the durable-artifact citation gate.
    try:
        from app import storage as _storage

        signed = await _storage.generate_download_signed_url(artifact_uri)
        _add_supplementary(signed)
    except Exception:  # noqa: BLE001, S110 — signing is optional, nothing to log
        pass

    # Per-metric references: each preserved numeric metric, pinned to the durable
    # artifact bundle, is a citation resolving to durable measured evidence.
    metric_root = artifact_uri or job_reference or f"compute-job:{job.id}"
    for name, value in metrics.items():
        _add_metric(f"{metric_root}#metric:{name}={value}")
    if isinstance(criterion, dict):
        c_metric = criterion.get("metric")
        c_value = criterion.get("value")
        if c_metric and c_value is not None:
            _add_metric(f"{metric_root}#metric:{c_metric}={c_value}")

    # Per-arm citations: each arm in a multi-arm sweep is a distinct citable unit
    # so operators can ask "which arm won?" and get a grounded answer.
    if isinstance(arms, dict):
        for arm_name in arms:
            _add_metric(f"{metric_root}#arms:{arm_name}")
    elif isinstance(arms, list):
        for i in range(len(arms)):
            _add_metric(f"{metric_root}#arms:{i}")

    if not real_evidence:
        return None

    citations = list(real_evidence) + supplementary
    if (
        result_reference
        and _is_usable_citation(result_reference)
        and result_reference not in seen
    ):
        citations.append(result_reference)

    log_excerpt = (logs or "")[:_COMPUTE_LOG_TEXT_CAP]

    # Extract the bounded command log text (stdout/stderr from the experiment
    # command) from the COMPUTE_RESULT artifact text so the LLM can find metrics
    # and results that live in command output rather than in $BLUEPRINT_RESULT_FILE.
    # build_compute_result_logs() embeds command_log_text directly in the artifact
    # JSON; fall back to reading from the Artifact row's logs if absent there.
    command_log_text: str | None = None
    try:
        result_obj = await asyncio.to_thread(json.loads, logs) if logs else None
        if isinstance(result_obj, dict):
            cl = str(result_obj.get("command_log_text") or "").strip()
            if cl:
                command_log_text = cl[:_COMPUTE_LOG_TEXT_CAP]
    except (json.JSONDecodeError, ValueError):
        pass
    if not command_log_text and isinstance(art_logs, dict):
        raw_cl = art_logs.get("command_log")
        if isinstance(raw_cl, dict):
            cl = str(raw_cl.get("text") or "").strip()
            if cl:
                command_log_text = cl[:_COMPUTE_LOG_TEXT_CAP]

    return {
        "job_id": str(job.id),
        "artifact_uri": artifact_uri,
        "file_count": file_count,
        "metrics": metrics,
        "arms": arms,
        "citations": citations,
        "real_evidence": real_evidence,
        "log_excerpt": log_excerpt,
        "command_log_text": command_log_text,
    }


def _build_compute_analysis_prompt(
    task: Task,
    *,
    outcome: str,
    cost: float | None,
    criterion: dict[str, Any] | None,
    evidence: dict[str, Any],
    fallback: dict[str, Any],
) -> str:
    """Build a compact, bounded analysis prompt grounded in the durable inputs."""
    payload = task.first_payload or {}
    identity = (
        str(payload.get("experiment") or "").strip()
        or str(payload.get("benchmark") or "").strip()
        or "compute experiment"
    )
    allowed = "\n".join(f"  - {c}" for c in evidence["citations"])
    metrics_lines = (
        "\n".join(f"  - {k} = {v}" for k, v in evidence["metrics"].items())
        or "  (no command-reported numeric metrics)"
    )
    criterion_line = (
        json.dumps(criterion, sort_keys=True)
        if criterion is not None
        else "(none declared)"
    )
    # Prefer the embedded command-output log (where non-scalar results often
    # live); when absent, fall back to the truncated terminal result log so the
    # job's logs are never dropped from the analysis prompt.
    command_log_section = ""
    command_log_text = evidence.get("command_log_text")
    log_excerpt = evidence.get("log_excerpt")
    if command_log_text:
        command_log_section = (
            "Command output log (may contain results not in scalar metrics):\n"
            f"{command_log_text}\n"
        )
    elif log_excerpt:
        command_log_section = f"Terminal result log (truncated):\n{log_excerpt}\n"
    return (
        "You are analysing the durable outputs of a finished compute experiment. "
        "Write a grounded, factual knowledge document about what the experiment "
        "found, citing the durable evidence below. Do NOT invent results — every "
        "claim must rest on the provided outcome, metrics, cost, or artifacts.\n\n"
        f"Experiment: {identity}\n"
        f"Terminal outcome: {outcome}\n"
        f"Cost (USD): {cost}\n"
        f"Durable artifact bundle: {evidence['artifact_uri']} "
        f"({evidence['file_count']} file(s))\n"
        f"Declared success criterion: {criterion_line}\n"
        "Preserved numeric metrics:\n"
        f"{metrics_lines}\n"
        f"{command_log_section}"
        "You MUST choose source_citations ONLY from this allowed evidence list "
        "(copy the strings verbatim); cite at least one artifact or metric "
        "reference, not only the result reference:\n"
        f"{allowed}\n\n"
        "Respond with ONLY valid JSON in this exact shape (no markdown, no "
        "commentary):\n"
        '{"title": "...", "summary": "...", "body": "...(markdown)...", '
        '"source_citations": ["...", "..."]}\n'
    )


async def _call_compute_analysis_llm(prompt: str) -> dict[str, Any] | None:
    """Call the configured inline LLM for the analysis, with a single JSON-parse retry.

    Returns the parsed JSON object on success, or ``None`` on a persistent parse
    error or any API failure. The explicit analysis caller reports that as
    unavailable; it never persists deterministic output as if it were analysis.
    """
    data = await call_inline_llm_json(
        "compute_analysis",
        prompt,
        required_keys=("title", "summary", "body"),
    )
    if data is None:
        return None
    return data


async def _analyze_compute_experiment_with_llm(
    task: Task,
    *,
    outcome: str,
    cost: float | None,
    criterion: dict[str, Any] | None,
    result_reference: str | None,
    logs: str | None,
    session: AsyncSession,
    fallback: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """Produce a cited LLM analysis over durable artifacts, or ``(None, reason)``.

    This helper is only used by the explicit analysis entry point, never by the
    completion path. ``reason`` is a compact machine-readable token for why the
    analysis was unavailable (for example ``"analysis-disabled"``,
    ``"precondition-not-met"``, ``"no-evidence"``, ``"llm-error"``, or
    ``"citations-ungrounded"``).
    """
    if os.environ.get(_DISABLE_LLM_COMPUTE_ANALYSIS_ENV, "").strip() == "1":
        logger.info("[COMPUTE] LLM analysis disabled via env for task %s", task.id)
        return None, "analysis-disabled"
    if resolve_inline_llm_settings("compute_analysis").disabled:
        logger.info(
            "[COMPUTE] LLM analysis disabled via runtime config for task %s",
            task.id,
        )
        return None, "analysis-disabled"
    unavailable = inline_llm_unavailable_reason("compute_analysis")
    if unavailable:
        logger.info(
            "[COMPUTE] LLM analysis skipped for task %s: %s",
            task.id,
            unavailable,
        )
        return None, "precondition-not-met"

    evidence = await _gather_compute_evidence(
        task,
        logs=logs,
        result_reference=result_reference,
        criterion=criterion,
        session=session,
    )
    if evidence is None:
        return None, "no-evidence"

    prompt = _build_compute_analysis_prompt(
        task,
        outcome=outcome,
        cost=cost,
        criterion=criterion,
        evidence=evidence,
        fallback=fallback,
    )
    document = await _call_compute_analysis_llm(prompt)
    if document is None:
        return None, "llm-error"
    return _ground_and_assemble_compute_analysis(
        document, evidence, fallback, execution="backend"
    )


def _ground_and_assemble_compute_analysis(
    document: dict[str, Any],
    evidence: dict[str, Any],
    fallback: dict[str, Any],
    *,
    analysis_model: str = DIRECT_LLM_MODEL,
    execution: str = "backend",
) -> tuple[dict[str, Any] | None, str]:
    """Ground a produced analysis document and assemble the upgrade payload.

    Backend-authoritative grounding for the explicit (operator-invoked) LLM
    analysis path. The producer's citations are kept only if they are members of
    the durable candidate set, and at least one must resolve to real
    artifact/metric evidence.

    Returns ``(document, "")`` on success or ``(None, reason)`` with a compact
    machine-readable cause (``"llm-error"``, ``"citations-ungrounded"``).
    """
    # Citation grounding: keep only citations that are members of the durable
    # candidate set (the producer cannot fabricate evidence), then run them
    # through the SAME shape gate the other knowledge lanes use.
    allowed = set(evidence["citations"])
    real_evidence = set(evidence["real_evidence"])
    raw_citations = document.get("source_citations")
    if not isinstance(raw_citations, list):
        return None, "llm-error"
    grounded = [
        c.strip() for c in raw_citations if isinstance(c, str) and c.strip() in allowed
    ]
    usable = [c for c in grounded if _is_usable_citation(c)]
    # Defence-in-depth: require at least one usable citation AND that at least one
    # resolves to real artifact/metric evidence — not only the result_reference.
    if not usable or not any(c in real_evidence for c in usable):
        logger.info(
            "[COMPUTE] %s analysis lacked grounded artifact citations; "
            "analysis unavailable",
            execution,
        )
        return None, "citations-ungrounded"

    summary = str(document.get("summary") or "")
    body = str(document.get("body") or "")
    if not summary.strip() or not body.strip():
        return None, "llm-error"
    title = (
        str(document.get("title"))[:500] if document.get("title") else fallback["title"]
    )

    verdict_record = dict(fallback["verdict_record"])
    verdict_record["analysis_mode"] = "llm"
    verdict_record["analysis_model"] = analysis_model
    # Auditable provenance: whether the model ran in the backend process or on the
    # agent-VM centralized-accounts substrate (operator policy p-13f153e9).
    verdict_record["analysis_execution"] = execution

    # Stamp citation-grounding counts so _persist_compute_knowledge_document can
    # attach them to the verdict, mirroring the design/web_research grounding stamp.
    # At this point we already know: at least one usable citation is in real_evidence
    # (the gate above enforced it), so grounded >= 1 and citation_grounding_miss
    # will not fire for produced documents.
    grounding_stamp = {
        "checked": True,
        "file_citations": len(usable),
        "grounded": sum(1 for c in usable if c in real_evidence),
        "unresolved": [c for c in usable if c not in real_evidence],
    }

    return {
        "title": title,
        "summary": summary,
        "body": body,
        "topic": fallback["topic"],
        "tags": fallback["tags"],
        # Only persist durable citations (real_evidence); supplementary signed
        # URLs are prompt helpers that must not survive into source_citations.
        "citations": [c for c in usable if c in real_evidence][:_COMPUTE_MAX_CITATIONS],
        "verdict_record": verdict_record,
        "_citation_grounding": grounding_stamp,
    }, ""


_ANALYSIS_STRENGTH: dict[str, int] = {"fallback": 0, "deterministic": 0, "llm": 1}


def _incoming_mode_is_upgrade(
    existing_doc: KnowledgeDocument, incoming_mode: str
) -> bool:
    """True when ``incoming_mode`` is strictly stronger than the existing doc's mode.

    Reads ``analysis_mode`` from the existing doc's ``forward_quality_verdict``
    JSON, defaulting to ``"deterministic"`` if absent. A strictly higher
    ``_ANALYSIS_STRENGTH`` value is an upgrade; equal or lower is not.
    """
    existing_verdict = existing_doc.forward_quality_verdict or {}
    existing_mode = str(existing_verdict.get("analysis_mode") or "deterministic")
    return _ANALYSIS_STRENGTH.get(incoming_mode, 0) > _ANALYSIS_STRENGTH.get(
        existing_mode, 0
    )


async def _latest_compute_live_release_evidence(
    task: Task,
    session: AsyncSession,
    *,
    job_id: uuid.UUID | None = None,
) -> tuple[dict[str, Any], str | None] | None:
    """Return (evidence, echo_sha) from the most-recent compute job, or None."""
    if job_id is not None:
        job = await session.get(ComputeJob, job_id)
        if (
            job is None
            or job.task_id != task.id
            or not isinstance(job.polling_metadata, dict)
        ):
            return None
    else:
        result = await session.execute(
            select(ComputeJob)
            .where(ComputeJob.task_id == task.id)
            .order_by(
                ComputeJob.status.in_(_COMPUTE_JOB_TERMINAL_STATUSES).desc(),
                ComputeJob.created_at.desc(),
            )
        )
        job = result.scalars().first()
        if job is None or not isinstance(job.polling_metadata, dict):
            return None
    evidence = job.polling_metadata.get(_LIVE_RELEASE_EVIDENCE_KEY)
    if not isinstance(evidence, dict):
        return None
    echo_sha = job.polling_metadata.get("release_echo_sha") or None
    return evidence, echo_sha


async def _persist_compute_knowledge_document(
    task: Task,
    document: dict[str, Any],
    mode: str,
    session: AsyncSession,
    *,
    compute_job_id: uuid.UUID | None = None,
) -> KnowledgeDocument:
    """Persist one compute-experiment KnowledgeDocument, mirror a compact copy.

    Shared by the LLM-analysis and deterministic paths so the persisted
    shape is identical either way; ``mode`` (``"llm"``/``"deterministic"``) is stamped
    on the verdict and the mirrored plan_output so consumers can tell which path
    produced the document. NEVER mutates ``task.status``,
    ``latest_status_message``, or ``plan_output['compute_result']``.

    Idempotent on same-strength retries: a second call with the same (or weaker)
    mode returns the existing row unchanged. An incoming ``mode`` that is strictly
    STRONGER (e.g. ``"llm"`` over an existing ``"deterministic"``) upgrades the stored
    document's content and verdict in-place so the best available analysis always
    wins, while a downgrade (``"deterministic"`` over ``"llm"``) is a no-op.
    """
    direction_id = (task.context or {}).get("direction_id")
    direction_id = str(direction_id) if direction_id else None

    payload = task.first_payload or {}
    raw_sweep_id = payload.get("sweep_id") or None
    sweep_id_val = (str(raw_sweep_id).strip()[:128] if raw_sweep_id else None) or None
    arm_label_val = derive_compute_arm_label_from_payload(payload)

    title = document["title"]
    summary = document["summary"]
    topic = document["topic"]
    tags = document["tags"]
    citations = document["citations"][:100]

    verdict_record = dict(document["verdict_record"])
    verdict_record.setdefault("analysis_mode", mode)
    live_data = await _latest_compute_live_release_evidence(
        task,
        session,
        job_id=compute_job_id,
    )
    release_evidence: dict[str, Any] | None = None
    if live_data is not None:
        release_evidence, echo_sha = live_data
        verdict_record["release_evidence"] = release_evidence
        expected_sha = release_evidence.get("promoted_sha") or None
        binding_status = derive_release_binding_status(echo_sha, expected_sha)
        verdict_record["release_binding"] = {
            "status": binding_status,
            "echo_sha": echo_sha,
            "expected_sha": expected_sha,
        }

    # Attach citation-grounding probe detail (additive, best-effort).
    # Reuses the shared _grounding_probe_detail helper so the shape is identical
    # to the design/web_research lanes. Only present when the document carries a
    # checked _citation_grounding stamp — older documents that lack it are unaffected.
    grounding = _grounding_probe_detail(document)
    if grounding is not None:
        probes = dict(verdict_record.get("probes") or {})
        probes["grounding"] = grounding
        verdict_record["probes"] = probes

    # Flag documents whose citations are fully unresolved (all citations point at
    # artifacts not found in the durable store). Additive — never blocks completion.
    if _compute_grounding_miss(document) is not None:
        verdict_record["citation_grounding_miss"] = True
    acceptance = classify_knowledge_acceptance(
        source_kind=SourceKind.COMPUTE_EXPERIMENT.value,
        forward_quality_verdict=verdict_record,
        source_citations=citations,
    )
    verdict_record["knowledge_acceptance_evidence"] = acceptance[
        "knowledge_acceptance_evidence"
    ]
    verdict_record["compute_acceptance_grade"] = acceptance["compute_acceptance_grade"]
    verdict_record["compute_acceptance_reason"] = acceptance[
        "compute_acceptance_reason"
    ]

    existing = (
        (
            await session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.project_id == task.project_id,
                    KnowledgeDocument.source_task_id == task.id,
                    KnowledgeDocument.source_kind
                    == SourceKind.COMPUTE_EXPERIMENT.value,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is None:
        doc = KnowledgeDocument(
            project_id=task.project_id,
            source_task_id=task.id,
            direction_id=direction_id,
            source_kind=SourceKind.COMPUTE_EXPERIMENT.value,
            title=title,
            summary=summary,
            body=document["body"],
            topic=topic,
            tags=tags,
            source_citations=citations,
            forward_quality_verdict=verdict_record,
            sweep_id=sweep_id_val,
            arm_label=arm_label_val,
        )
        session.add(doc)
    else:
        doc = existing
        if _incoming_mode_is_upgrade(existing, mode):
            # Upgrade the stored document to the stronger analysis in-place.
            # Never downgrades (deterministic over llm is a no-op above).
            doc.title = title
            doc.summary = summary
            doc.body = document["body"]
            doc.topic = topic
            doc.tags = tags
            doc.source_citations = citations
            doc.forward_quality_verdict = verdict_record
            # Backfill sweep identity on upgrade if not yet set.
            if doc.sweep_id is None and sweep_id_val:
                doc.sweep_id = sweep_id_val
            if doc.arm_label is None and arm_label_val:
                doc.arm_label = arm_label_val
            flag_modified(doc, "tags")
            flag_modified(doc, "source_citations")
            flag_modified(doc, "forward_quality_verdict")
            session.add(doc)

    # Mirror a compact copy into plan_output for admin visibility WITHOUT
    # touching the compute_result the compute handler already wrote there.
    # Always reflect the stored doc's actual (post-upgrade) values so the
    # mirror is consistent with what was written to the database.
    # When the stored document is at a STRONGER mode than the incoming
    # verdict (e.g. llm doc receiving a deterministic re-run), pull
    # acceptance fields from the stored verdict so the mirror stays
    # consistent with the graded llm document instead of regressing to
    # the deterministic acceptance.
    plan_output = task.plan_output if isinstance(task.plan_output, dict) else {}
    plan_output = dict(plan_output)
    stored_verdict = doc.forward_quality_verdict or verdict_record
    plan_output["knowledge_document"] = {
        "id": str(doc.id),
        "title": doc.title or title,
        "summary": doc.summary or summary,
        "topic": doc.topic if doc.topic is not None else topic,
        "tags": doc.tags if doc.tags is not None else tags,
        "direction_id": direction_id,
        "citation_count": len(doc.source_citations or citations),
        "source_kind": SourceKind.COMPUTE_EXPERIMENT.value,
        "analysis_mode": stored_verdict.get("analysis_mode", mode),
        "knowledge_acceptance_evidence": stored_verdict.get(
            "knowledge_acceptance_evidence",
            acceptance["knowledge_acceptance_evidence"],
        ),
        "compute_acceptance_grade": stored_verdict.get(
            "compute_acceptance_grade",
            acceptance["compute_acceptance_grade"],
        ),
        "compute_acceptance_reason": stored_verdict.get(
            "compute_acceptance_reason",
            acceptance["compute_acceptance_reason"],
        ),
        "release_evidence": stored_verdict.get("release_evidence"),
        "analysis_fallback_reason": stored_verdict.get("fallback_reason"),
        "per_arm_verdicts": stored_verdict.get("per_arm_verdicts"),
    }
    task.plan_output = plan_output
    flag_modified(task, "plan_output")
    session.add(task)
    await session.commit()
    await session.refresh(doc)

    # Close the knowledge-prerequisite loop for any QUEUED dependent that waited
    # on this experiment's result. Best-effort — never blocks completion.
    await _deliver_knowledge_to_dependents(
        task, doc, {"title": title, "summary": summary}, session
    )

    logger.info(
        "[COMPUTE] Persisted compute-experiment knowledge document %s for task %s "
        "(analysis_mode=%s)",
        doc.id,
        task.id,
        verdict_record.get("analysis_mode", mode),
    )
    return doc


def _extract_job_terminal_diagnostics(job: Any) -> dict[str, Any] | None:
    """Extract attributable terminal diagnostics from a ComputeJob row.

    Always populates execution-location fields (``executing_backend``,
    ``runner_target_id``, ``runner_hostname``, ``accelerator_type``,
    ``machine_type``) when present, so clean-SUCCESS runs surface provenance
    in the run-provenance panel.  Adds ``terminal_reason``, ``vm_phase``, and
    failure-specific fields (``provisioning_cause``, ``launch_contract``) only
    when ``terminal_reason`` is non-empty.  Returns ``None`` only when ``job``
    is ``None``.
    """
    if job is None:
        return None
    diag: dict[str, Any] = {}
    accel = getattr(job, "accelerator_type", None)
    if accel:
        diag["accelerator_type"] = accel
    machine = getattr(job, "machine_type", None)
    if machine:
        diag["machine_type"] = machine
    backend = getattr(job, "backend", None)
    if backend:
        diag["executing_backend"] = backend
    try:
        meta = getattr(job, "polling_metadata", None) or {}
        runner_target_id = meta.get("runner_target_id")
        if runner_target_id:
            diag["runner_target_id"] = str(runner_target_id)
        runner_hostname = meta.get("runner_hostname")
        if runner_hostname:
            diag["runner_hostname"] = str(runner_hostname)
    except Exception:
        pass
    reason = getattr(job, "terminal_reason", None)
    if reason:
        started_at = getattr(job, "started_at", None)
        vm_phase = "provisioning" if started_at is None else "runtime"
        diag["terminal_reason"] = reason
        diag["vm_phase"] = vm_phase
        if vm_phase == "provisioning":
            try:
                meta = getattr(job, "polling_metadata", None) or {}
                raw_diag = meta.get("compute_rpc_diagnostic")
                cause = sanitize_rpc_diagnostic(raw_diag)
                if cause:
                    diag["provisioning_cause"] = cause
                deadline = meta.get("requested_deadline_seconds")
                prov_model = meta.get("provisioning_model")
                contract: dict[str, Any] = {}
                if deadline is not None:
                    contract["requested_deadline_seconds"] = deadline
                if prov_model is not None:
                    contract["provisioning_model"] = prov_model
                if contract:
                    diag["launch_contract"] = contract
            except Exception:
                pass
    return diag if diag else None


# Defensive bound on how many reported metric keys are captured verbatim into a
# single faithful document, so an adversarial / runaway result object can never
# bloat the knowledge store. A real experiment reports far fewer.
_COMPUTE_FAITHFUL_METRICS_MAX_KEYS = 200


def _extract_faithful_result_metrics(job: ComputeJob | None) -> dict[str, Any] | None:
    """Return the experiment's reported result metrics, captured FAITHFULLY.

    Reads the raw reported result object preserved on the job row
    (``polling_metadata['result_metrics']``). Unlike the COMPUTE_RESULT logs —
    which keep only the flattened top-level NUMERIC keys — this preserves the
    reported object verbatim, so non-scalar structure (e.g. an A/B ``arms``
    block) survives. Returns ``None`` when nothing was reported.
    """
    if job is None:
        return None
    meta = getattr(job, "polling_metadata", None) or {}
    if not isinstance(meta, dict):
        return None
    raw = meta.get("result_metrics")
    if not isinstance(raw, dict) or not raw:
        return None
    # Bound the captured key set defensively (faithful, not interpretive).
    return dict(list(raw.items())[:_COMPUTE_FAITHFUL_METRICS_MAX_KEYS])


def _extract_experiment_arms(result_metrics: dict[str, Any] | None) -> Any | None:
    """Return the A/B (multi-arm) comparison block when the result reported one.

    Faithful and structure-only: an experiment that compared arms reports them
    under an ``arms`` key in its result object (a mapping of arm name -> metrics,
    or a list of arm records). Captured verbatim so the strategist and the later
    opt-in judgment agent can compare arms — the backend applies no interpretation.
    """
    if not isinstance(result_metrics, dict):
        return None
    arms = result_metrics.get("arms")
    if isinstance(arms, (dict, list)) and arms:
        return arms
    return None


async def _prepare_deterministic_compute_document(
    task: Task,
    *,
    outcome: str,
    cost: float | None,
    criterion: dict[str, Any] | None,
    result_reference: str | None,
    session: AsyncSession,
    logs: str | None = None,
) -> tuple[dict[str, Any], uuid.UUID | None]:
    """Build the primary deterministic artifact-grounded compute document."""
    job = await _load_compute_job_from_logs(logs, session)
    terminal_diagnostics = _extract_job_terminal_diagnostics(job)
    result_metrics = _extract_faithful_result_metrics(job)
    arms = _extract_experiment_arms(result_metrics)
    evidence_tier = _compute_evidence_tier(job, result_metrics)
    document = _build_compute_experiment_document(
        task,
        outcome=outcome,
        cost=cost,
        criterion=criterion,
        result_reference=result_reference,
        terminal_diagnostics=terminal_diagnostics,
        result_metrics=result_metrics,
        arms=arms,
        evidence_tier=evidence_tier,
    )
    try:
        grounding_stamp = await _compute_citation_grounding_stamp(
            result_reference, logs, session
        )
        document = dict(document)
        document["_citation_grounding"] = grounding_stamp
        if grounding_stamp.get("grounded", 0) == 0 and document.get("citations"):
            document["citations"] = []
    except Exception:  # noqa: BLE001 — grounding stamp must never break completion
        logger.warning(
            "[COMPUTE] citation grounding stamp failed for task %s; "
            "persisting deterministic document without grounding detail",
            task.id,
            exc_info=True,
        )
        # Fail-closed: treat unexpected error as ungrounded; strip citations
        if document.get("citations"):
            document["citations"] = []
    return document, job.id if job is not None else None


async def persist_compute_experiment_knowledge(
    task: Task,
    *,
    outcome: str,
    cost: float | None,
    criterion: dict[str, Any] | None,
    result_reference: str | None,
    session: AsyncSession,
    logs: str | None = None,
) -> KnowledgeDocument | None:
    """Persist a ``compute_experiment`` artifact result as durable knowledge.

    A ``compute_experiment`` runs a budgeted compute job and completes with a
    measured result — a terminal ``outcome``, the measured metric value, whether
    a declared success criterion was met, and the ``cost``. ``web_research`` and
    ``design`` results become first-class :class:`KnowledgeDocument` rows that are
    indexed, delivered to dependents, and injected as prior knowledge into future
    research/design/strategist prompts; this seam gives a compute_experiment's
    answer the SAME durable home, so a later cycle asking the same measurable
    question can discover that the experiment already ran and what it found.

    The PRIMARY producer is the deterministic outcome/cost/criterion document.
    It performs no LLM call and does not label itself as a degraded analysis.
    Optional cited LLM analysis is available only through
    :func:`analyze_compute_experiment_knowledge`, which is explicitly invoked and
    non-gating.

    Invoked for BOTH the met-criterion success path AND the unmet-criterion fail
    path, so the system durably learns "we tried X and it did NOT meet the bar"
    rather than silently dropping a negative result.

    This is a strictly ADDITIVE, best-effort post-completion step: it persists one
    row, mirrors a compact ``plan_output["knowledge_document"]``, and reuses
    :func:`_deliver_knowledge_to_dependents`. It NEVER mutates ``task.status``,
    ``latest_status_message``, or ``plan_output["compute_result"]`` — the compute
    handler owns those — and any failure is the caller's to swallow so durable
    knowledge can never break task completion.
    """
    document, compute_job_id = await _prepare_deterministic_compute_document(
        task,
        outcome=outcome,
        cost=cost,
        criterion=criterion,
        result_reference=result_reference,
        session=session,
        logs=logs,
    )
    return await _persist_compute_knowledge_document(
        task,
        document,
        "deterministic",
        session,
        compute_job_id=compute_job_id,
    )


async def analyze_compute_experiment_knowledge(
    task: Task,
    *,
    outcome: str,
    cost: float | None,
    criterion: dict[str, Any] | None,
    result_reference: str | None,
    session: AsyncSession,
    logs: str | None = None,
) -> dict[str, Any]:
    """Explicitly run optional LLM analysis and upgrade the stored document.

    This step is deliberately decoupled from completion. It never mutates task
    status or ``plan_output["compute_result"]``; if credentials/evidence/output
    are unavailable it reports that honestly and leaves the deterministic
    document intact.
    """
    fallback, compute_job_id = await _prepare_deterministic_compute_document(
        task,
        outcome=outcome,
        cost=cost,
        criterion=criterion,
        result_reference=result_reference,
        session=session,
        logs=logs,
    )
    try:
        analysis, reason = await _analyze_compute_experiment_with_llm(
            task,
            outcome=outcome,
            cost=cost,
            criterion=criterion,
            result_reference=result_reference,
            logs=logs,
            session=session,
            fallback=fallback,
        )
    except Exception:  # noqa: BLE001 — optional analysis must never gate callers
        logger.warning(
            "[COMPUTE] explicit LLM analysis raised for task %s",
            task.id,
            exc_info=True,
        )
        analysis = None
        reason = "llm-error"
    if analysis is None:
        return await _stamp_compute_analysis_unavailable(task, reason, session)

    doc = await _persist_compute_knowledge_document(
        task,
        analysis,
        "llm",
        session,
        compute_job_id=compute_job_id,
    )
    return {
        "status": "upgraded",
        "reason": None,
        "analysis_mode": "llm",
        "knowledge_document_id": str(doc.id),
    }


async def _stamp_compute_analysis_unavailable(
    task: Task, reason: str | None, session: AsyncSession
) -> dict[str, Any]:
    """Stamp an unavailable cause on the deterministic doc and return the envelope.

    Shared by the backend-key path and the VM-substrate submission path. Never
    downgrades: an already-``llm`` document keeps its acceptance grade and is NOT
    annotated with an unavailable cause (a successful upgrade is authoritative).
    """
    stamped_doc_id: str | None = None
    try:
        existing = (
            (
                await session.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.project_id == task.project_id,
                        KnowledgeDocument.source_task_id == task.id,
                        KnowledgeDocument.source_kind
                        == SourceKind.COMPUTE_EXPERIMENT.value,
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            stamped_doc_id = str(existing.id)
            verdict = dict(existing.forward_quality_verdict or {})
            # Do not annotate an already-upgraded llm doc as unavailable.
            if str(verdict.get("analysis_mode") or "") != "llm":
                verdict["unavailable_cause"] = reason or "unknown"
                verdict["analysis_attempted_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
                existing.forward_quality_verdict = verdict
                flag_modified(existing, "forward_quality_verdict")
                session.add(existing)
                await session.commit()
    except Exception:  # noqa: BLE001 — stamp must never block the caller
        logger.warning(
            "[COMPUTE] failed to stamp unavailable_cause for task %s",
            task.id,
            exc_info=True,
        )
    envelope: dict[str, Any] = {
        "status": "analysis_unavailable",
        "reason": reason or "unknown",
        "analysis_mode": "unavailable",
    }
    if stamped_doc_id is not None:
        envelope["knowledge_document_id"] = stamped_doc_id
    return envelope


def _qa_unavailable(reason: str) -> dict[str, Any]:
    """Return a ComputeQAResponse-shaped dict for unavailable Q&A."""
    return {
        "answer": None,
        "citations": [],
        "grounding": {"checked": True, "file_citations": 0, "grounded": 0, "unresolved": []},
        "grounding_miss": True,
        "fallback_reason": reason,
    }


_QA_HISTORY_MAX_TURNS = 6
_QA_HISTORY_TURN_MAX_CHARS = 1000


def _build_compute_qa_prompt(
    task: Task,
    *,
    question: str,
    outcome: str,
    cost: float | None,
    criterion: dict[str, Any] | None,
    evidence: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
) -> str:
    """Build a grounded Q&A prompt for a single question about a compute run."""
    payload = task.first_payload or {}
    identity = (
        str(payload.get("experiment") or "").strip()
        or str(payload.get("benchmark") or "").strip()
        or "compute experiment"
    )
    metrics_lines = (
        "\n".join(f"  - {k} = {v}" for k, v in evidence["metrics"].items())
        or "  (no reported numeric metrics)"
    )
    criterion_line = json.dumps(criterion, sort_keys=True) if criterion else "(none)"
    log_text = evidence.get("command_log_text") or evidence.get("log_excerpt") or ""
    log_section = f"Command output (excerpt):\n{log_text}\n" if log_text else ""
    arms = evidence.get("arms")
    arms_section = (
        f"Per-arm results:\n{json.dumps(arms, sort_keys=True, default=str)}\n"
        if arms is not None
        else ""
    )
    allowed = "\n".join(f"  - {c}" for c in evidence["citations"])

    history_section = ""
    if history:
        turns = []
        for turn in history:
            q = str(turn.get("question") or "")[:_QA_HISTORY_TURN_MAX_CHARS]
            a = str(turn.get("answer") or "")[:_QA_HISTORY_TURN_MAX_CHARS]
            if q and a:
                turns.append(f"Q: {q}\nA: {a}")
        if turns:
            history_section = (
                "## Prior conversation (for context only — NOT evidence; do not cite from it)\n"
                + "\n---\n".join(turns)
                + "\n\n"
            )

    return (
        "You are answering a question about a completed compute experiment. "
        "Answer ONLY based on the provided evidence; decline honestly if the "
        "evidence does not cover the question.\n\n"
        f"Experiment: {identity}\n"
        f"Outcome: {outcome}\n"
        f"Cost (USD): {cost}\n"
        f"Success criterion: {criterion_line}\n"
        f"Metrics:\n{metrics_lines}\n"
        f"{arms_section}"
        f"{log_section}"
        f"{history_section}"
        f"Question: {question[:500]}\n\n"
        "Choose citations ONLY from this allowed list (copy verbatim):\n"
        f"{allowed}\n\n"
        'Respond with ONLY valid JSON: {"answer": "...", "citations": ["..."]}\n'
    )


async def answer_compute_experiment_question(
    task: Task,
    *,
    question: str,
    outcome: str,
    cost: float | None,
    criterion: dict[str, Any] | None,
    result_reference: str | None,
    session: AsyncSession,
    logs: str | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Answer a free-form question grounded in a completed experiment's durable evidence.

    Returns a ComputeQAResponse-shaped dict with answer, citations, grounding stamp,
    grounding_miss, and fallback_reason. Declines honestly when evidence is insufficient
    or the LLM is unavailable. Never persists any data — purely ephemeral Q&A.
    """
    if (
        os.environ.get(_DISABLE_LLM_COMPUTE_ANALYSIS_ENV, "").strip() == "1"
        or resolve_inline_llm_settings("compute_qa").disabled
    ):
        return _qa_unavailable("llm-disabled")
    unavailable = inline_llm_unavailable_reason("compute_qa")
    if unavailable:
        return _qa_unavailable(unavailable)
    evidence = await _gather_compute_evidence(
        task,
        logs=logs,
        result_reference=result_reference,
        criterion=criterion,
        session=session,
    )
    if evidence is None:
        return _qa_unavailable("no-evidence")
    bounded_history: list[dict[str, Any]] | None = None
    if history:
        valid_turns = [
            t for t in history
            if isinstance(t, dict)
            and isinstance(t.get("question"), str)
            and isinstance(t.get("answer"), str)
        ]
        bounded_history = valid_turns[-_QA_HISTORY_MAX_TURNS:]
    prompt = _build_compute_qa_prompt(
        task,
        outcome=outcome,
        cost=cost,
        criterion=criterion,
        evidence=evidence,
        question=question,
        history=bounded_history,
    )
    allowed_set = set(evidence.get("citations", []))
    data = await call_inline_llm_json(
        "compute_qa",
        prompt,
        required_keys=("answer",),
    )
    if data is None:
        return _qa_unavailable("llm-error")
    raw_cits = data.get("citations") or []
    grounded_cits = [
        c.strip()
        for c in raw_cits
        if isinstance(c, str) and c.strip() in allowed_set
    ][:_COMPUTE_MAX_CITATIONS]
    unresolved_cits = [
        c.strip()
        for c in raw_cits
        if isinstance(c, str) and c.strip() not in allowed_set
    ]
    real_evidence_set = set(evidence.get("real_evidence", []))
    durable_cits = [c for c in grounded_cits if c in real_evidence_set]
    grounding_stamp = {
        "checked": True,
        "file_citations": len(raw_cits),
        "grounded": len(durable_cits),
        "unresolved": unresolved_cits,
    }
    miss_reason = None if durable_cits else "no-grounded-citations"
    return {
        "answer": str(data["answer"])[:4000],
        "citations": durable_cits,
        "grounding": grounding_stamp,
        "grounding_miss": miss_reason is not None,
        "fallback_reason": miss_reason,
    }


async def _handle_web_research_completion(
    task: Task, logs: str, session: AsyncSession
) -> None:
    """Completion handler for the ``web_research`` task type (web-grounded doc)."""
    await _handle_knowledge_document_completion(
        task, logs, session, kind="web_research"
    )


async def _handle_design_completion(
    task: Task, logs: str, session: AsyncSession
) -> None:
    """Completion handler for the ``design`` task type (codebase-grounded doc)."""
    await _handle_knowledge_document_completion(task, logs, session, kind="design")


# ---------------------------------------------------------------------------
# Research-audit knowledge: when a research task completes in report/audit mode
# its numeric findings are durably persisted as a queryable KnowledgeDocument
# so future cycles can retrieve prior measurements without operator help.
# ---------------------------------------------------------------------------


def _build_research_audit_document(
    task: Task,
    findings: list[dict[str, Any]],
    insights: list[dict[str, Any]],
    direction: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build a research_audit KnowledgeDocument dict from completed research output.

    Returns None when there is no substantive content (empty findings and insights).
    The body synthesizes the findings list and any insights into a structured report
    that is human-readable and keyword-searchable in the knowledge index.
    """
    if not findings and not insights:
        return None

    payload = task.first_payload or {}
    direction_id = str((task.context or {}).get("direction_id") or "")
    topic = payload.get("topic") or (direction or {}).get("title") or "Research audit"
    raw_tags = payload.get("tags")
    base_tags = [str(t) for t in raw_tags[:28]] if isinstance(raw_tags, list) else []
    tags = list(dict.fromkeys([*base_tags, "research_audit"]))[:30]

    _FALLBACK_TOPIC = "Research audit"
    topic_str = str(topic)[:200]
    if topic_str == _FALLBACK_TOPIC:
        title = _FALLBACK_TOPIC
    else:
        title = f"Research audit: {topic_str}"[:500]

    summary_parts: list[str] = []
    if findings:
        n = len(findings)
        leading_titles = [
            str(f.get("title") or f"Finding {i + 1}").strip()[:80]
            for i, f in enumerate(findings[:3])
        ]
        title_text = "; ".join(leading_titles)
        summary_parts.append(f"{n} finding(s): {title_text}.")
    if insights:
        summary_parts.append(f"{len(insights)} insight(s).")
    summary = " ".join(summary_parts) or "Research audit report."

    body_lines = [f"# Research audit: {topic}", ""]
    if findings:
        body_lines.append(f"## Findings ({len(findings)})")
        body_lines.append("")
        for i, f in enumerate(findings[:50], 1):
            f_title = str(f.get("title") or f"Finding {i}").strip()[:200]
            f_desc = str(f.get("description") or f.get("prompt") or "").strip()[:800]
            f_type = str(f.get("type") or "").strip()
            f_scope = str(f.get("scope") or "").strip()
            f_grade = str(f.get("actionability_grade") or "").strip()
            meta_parts = [p for p in [f_type, f_scope, f_grade] if p]
            meta_str = f" ({', '.join(meta_parts)})" if meta_parts else ""
            body_lines.append(f"### {i}. {f_title}{meta_str}")
            if f_desc:
                body_lines.append("")
                body_lines.append(f_desc)
            body_lines.append("")
    if insights:
        body_lines.append(f"## Insights ({len(insights)})")
        body_lines.append("")
        for j, ins in enumerate(insights[:20], 1):
            ins_summary = str(ins.get("summary") or "").strip()[:500]
            if ins_summary:
                body_lines.append(f"- {ins_summary}")
        body_lines.append("")
    body = "\n".join(body_lines)

    citations = [f"task:{task.id}"]

    return {
        "title": title,
        "summary": summary,
        "body": body,
        "topic": str(topic)[:200],
        "tags": tags,
        "citations": citations,
        "direction_id": direction_id or None,
    }


async def persist_research_audit_document(
    task: Task,
    findings: list[dict[str, Any]],
    insights: list[dict[str, Any]],
    direction: dict[str, Any] | None,
    session: AsyncSession,
) -> None:
    """Persist a research_audit KnowledgeDocument for a completed audit-mode research task.

    Idempotent: a second call for the same source task is a no-op (existing row
    returned unchanged). Best-effort: callers must wrap in try/except so a persist
    failure never blocks the research completion spawn loop.
    """
    document = _build_research_audit_document(task, findings, insights, direction)
    if document is None:
        logger.info(
            "[RESEARCH_AUDIT] Skipping knowledge doc for task %s: no substantive content",
            task.id,
        )
        return

    direction_id = document.get("direction_id")
    citations = [c for c in (document.get("citations") or []) if isinstance(c, str)]

    existing = (
        (
            await session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.project_id == task.project_id,
                    KnowledgeDocument.source_task_id == task.id,
                    KnowledgeDocument.source_kind == SourceKind.RESEARCH_AUDIT.value,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        logger.info(
            "[RESEARCH_AUDIT] Knowledge doc already exists (%s) for task %s — skipping",
            existing.id,
            task.id,
        )
        return

    verdict_record: dict[str, Any] = {
        "passed": True,
        "strategy": "research_audit",
        "finding_count": len(findings),
        "insight_count": len(insights),
    }

    doc = KnowledgeDocument(
        project_id=task.project_id,
        source_task_id=task.id,
        direction_id=direction_id,
        source_kind=SourceKind.RESEARCH_AUDIT.value,
        title=document["title"],
        summary=document["summary"],
        body=document["body"],
        topic=document["topic"],
        tags=document["tags"],
        source_citations=citations[:100],
        forward_quality_verdict=verdict_record,
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)

    logger.info(
        "[RESEARCH_AUDIT] Persisted knowledge document %s for task %s "
        "(%d finding(s), %d insight(s))",
        doc.id,
        task.id,
        len(findings),
        len(insights),
    )

    normalized = {"title": document["title"], "summary": document["summary"]}
    await _deliver_knowledge_to_dependents(task, doc, normalized, session)

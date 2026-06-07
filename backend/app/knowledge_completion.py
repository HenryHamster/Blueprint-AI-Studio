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

import json
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.broadcast import broadcast_to_project
from app.models import KnowledgeDocument, SourceKind, Task, TaskStatus
from app.task_evaluators import (
    QUALITY_GRADER_VERDICT_KEY,
    EvaluationVerdict,
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
        "about", "above", "across", "after", "against", "research", "current",
        "state", "their", "these", "those", "which", "while", "would", "could",
        "should", "there", "where", "what", "when", "with", "from", "that",
        "this", "into", "over", "under", "between", "your", "have", "been",
        "being", "does", "using", "used", "make", "made", "such", "than",
        "then", "they", "them", "will", "shall", "topic", "prompt", "report",
        "document", "summary",
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
    if not all(f'"{k}"' in snippet for k in _DOCUMENT_REQUIRED_KEYS):
        return None
    start = snippet.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(snippet)):
            c = snippet[i]
            if escape_next:
                escape_next = False
                continue
            if c == "\\" and in_string:
                escape_next = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = snippet[start : i + 1]
                    if all(f'"{k}"' in candidate for k in _DOCUMENT_REQUIRED_KEYS):
                        try:
                            obj = json.loads(candidate)
                        except (json.JSONDecodeError, ValueError):
                            obj = None
                        if isinstance(obj, dict) and all(
                            k in obj for k in _DOCUMENT_REQUIRED_KEYS
                        ):
                            return obj
                    break
        start = snippet.find("{", start + 1)
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
        "authoring_branch": str(authoring_branch)[:255] if authoring_branch is not None else None,
        "head_sha": str(head_sha)[:40] if head_sha is not None else None,
    }


async def _fail_gate(
    task: Task, verdict: EvaluationVerdict, session: AsyncSession, kind: str = "web_research"
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
    try:
        dependents = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.status == TaskStatus.QUEUED,
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


async def _persist_document(
    task: Task,
    document: dict[str, Any],
    verdict_record: dict[str, Any],
    session: AsyncSession,
    kind: str = "web_research",
) -> None:
    """Persist exactly one KnowledgeDocument, mirror a compact copy, mark COMPLETED."""
    normalized = _normalize_document(document, task)

    # The strategic direction that motivated the producing task is carried on
    # ``task.context["direction_id"]`` (a string direction key, not a FK).
    # Persisting it is purely additive — it never changes whether the document
    # is stored — and lets a direction's accumulated knowledge be discovered.
    direction_id = (task.context or {}).get("direction_id")
    direction_id = str(direction_id) if direction_id else None

    doc = KnowledgeDocument(
        project_id=task.project_id,
        source_task_id=task.id,
        direction_id=direction_id,
        source_kind=_source_kind(kind),
        title=normalized["title"],
        summary=normalized["summary"],
        body=normalized["body"],
        topic=normalized["topic"],
        tags=normalized["tags"],
        source_citations=normalized["source_citations"],
        forward_quality_verdict=verdict_record,
        authoring_branch=normalized.get("authoring_branch"),
        head_sha=normalized.get("head_sha"),
    )
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
                f"{kind} output has no usable source citation "
                f"({_citation_hint(kind)})",
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
                EvaluationVerdict(
                    False, f"{kind} {grounding_miss}", "forward-quality"
                ),
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
                EvaluationVerdict(
                    False, f"{kind} {grounding_miss}", "forward-quality"
                ),
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


async def persist_compute_experiment_knowledge(
    task: Task,
    *,
    outcome: str,
    cost: float | None,
    criterion: dict[str, Any] | None,
    result_reference: str | None,
    session: AsyncSession,
) -> KnowledgeDocument | None:
    """Persist a ``compute_experiment`` verdict as a durable KnowledgeDocument.

    A ``compute_experiment`` runs a budgeted compute job and completes with a
    measured verdict — a terminal ``outcome``, the measured metric value, whether
    a declared success criterion was met, and the ``cost``. ``web_research`` and
    ``design`` results become first-class :class:`KnowledgeDocument` rows that are
    indexed, delivered to dependents, and injected as prior knowledge into future
    research/design/strategist prompts; this seam gives a compute_experiment's
    answer the SAME durable home, so a later cycle asking the same measurable
    question can discover that the experiment already ran and what it found.

    Unlike the web_research/design lane this synthesizes the document from the
    measured verdict rather than from agent-emitted prose, so it bypasses the
    citation/topic forward-quality gates — the metric gate already ran in
    ``task_completion._handle_compute_experiment_completion`` and is what admits
    (or fails) the experiment. It is invoked for BOTH the met-criterion success
    path AND the unmet-criterion fail path, so the system durably learns "we
    tried X and it did NOT meet the bar" rather than silently dropping a negative
    result.

    This is a strictly ADDITIVE, best-effort post-completion step: it persists
    one row, mirrors a compact ``plan_output["knowledge_document"]``, and reuses
    :func:`_deliver_knowledge_to_dependents`. It NEVER mutates ``task.status``,
    ``latest_status_message``, or ``plan_output["compute_result"]`` — the compute
    handler owns those — and any failure is the caller's to swallow so durable
    knowledge can never break task completion.
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
                f"Success criterion MET: {metric}={value} "
                f"(required {op} {threshold})."
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
    body = "\n".join(body_lines)

    raw_tags = payload.get("tags")
    base_tags = [str(t) for t in raw_tags[:28]] if isinstance(raw_tags, list) else []
    tags = list(dict.fromkeys([*base_tags, "compute_experiment", outcome]))[:30]

    topic = (
        payload.get("topic") or experiment or benchmark or payload.get("metric")
    )
    topic = str(topic)[:200] if topic else None

    citations = (
        [result_reference]
        if result_reference and _is_usable_citation(result_reference)
        else []
    )

    direction_id = (task.context or {}).get("direction_id")
    direction_id = str(direction_id) if direction_id else None

    verdict_record = {
        "strategy": "metric",
        "outcome": outcome,
        "cost_usd": cost,
        "criterion": criterion,
        "criterion_met": crit_met,
    }

    doc = KnowledgeDocument(
        project_id=task.project_id,
        source_task_id=task.id,
        direction_id=direction_id,
        source_kind=SourceKind.COMPUTE_EXPERIMENT.value,
        title=title,
        summary=summary,
        body=body,
        topic=topic,
        tags=tags,
        source_citations=citations,
        forward_quality_verdict=verdict_record,
    )
    session.add(doc)

    # Mirror a compact copy into plan_output for admin visibility WITHOUT
    # touching the compute_result the compute handler already wrote there.
    plan_output = task.plan_output if isinstance(task.plan_output, dict) else {}
    plan_output = dict(plan_output)
    plan_output["knowledge_document"] = {
        "id": str(doc.id),
        "title": title,
        "summary": summary,
        "topic": topic,
        "tags": tags,
        "direction_id": direction_id,
        "citation_count": len(citations),
        "source_kind": SourceKind.COMPUTE_EXPERIMENT.value,
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
        "[COMPUTE] Persisted compute-experiment knowledge document %s for task %s",
        doc.id,
        task.id,
    )
    return doc


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

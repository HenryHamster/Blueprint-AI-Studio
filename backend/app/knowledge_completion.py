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
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.broadcast import broadcast_to_project
from app.models import KnowledgeDocument, Task, TaskStatus
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

    return {
        "title": (str(obj.get("title")) if obj.get("title") is not None else "")[:500],
        "summary": str(obj.get("summary")) if obj.get("summary") is not None else "",
        "body": str(obj.get("body")) if obj.get("body") is not None else "",
        "topic": str(topic)[:200] if topic is not None else None,
        "tags": tags,
        "source_citations": citations[:100],
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


async def _persist_document(
    task: Task,
    document: dict[str, Any],
    verdict_record: dict[str, Any],
    session: AsyncSession,
    kind: str = "web_research",
) -> None:
    """Persist exactly one KnowledgeDocument, mirror a compact copy, mark COMPLETED."""
    normalized = _normalize_document(document, task)

    doc = KnowledgeDocument(
        project_id=task.project_id,
        source_task_id=task.id,
        title=normalized["title"],
        summary=normalized["summary"],
        body=normalized["body"],
        topic=normalized["topic"],
        tags=normalized["tags"],
        source_citations=normalized["source_citations"],
        forward_quality_verdict=verdict_record,
    )
    session.add(doc)

    # Mirror a compact copy into plan_output for debugging / admin visibility
    # (the body is intentionally omitted to keep plan_output small).
    plan_output = task.plan_output if isinstance(task.plan_output, dict) else {}
    plan_output = dict(plan_output)
    plan_output["knowledge_document"] = {
        "title": normalized["title"],
        "summary": normalized["summary"],
        "topic": normalized["topic"],
        "tags": normalized["tags"],
        "citation_count": len(normalized["source_citations"]),
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
        await _persist_document(task, document, grader_verdict, session, kind)
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

        grader = await spawn_forward_quality_grader(
            task, json.dumps(document), session
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

    verdict_record = {
        "passed": True,
        "reason": verdict.reason,
        "strategy": verdict.strategy,
        "grader_task_id": None,
    }
    await _persist_document(task, document, verdict_record, session, kind)


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

"""Serialize a project's built-in tool content into agent-readable text.

The per-project built-in tools (``notes``, ``whiteboard``) store their mutable
document in ``ProjectTool.content`` (a JSONB column). That content is rendered
UI-only today — no agent working on a task ever sees it. This module is the
bridge: given a session + project, it loads the project's ENABLED tools and
renders each into a compact, bounded, agent-readable block, mirroring the way
``.blueprint/tools.json`` manifest SKILLS already reach agents via the claim
response.

Design constraints (a large scratchpad must never blow the agent's context
budget):

* Per-tool text is truncated to :data:`_PER_TOOL_CHAR_CAP`.
* The overall returned payload is capped at :data:`_OVERALL_CHAR_CAP`; once the
  running total is exhausted, remaining tools are dropped.
* A ``whiteboard`` never dumps raw stroke JSON — it contributes a single
  presence/summary line (title + stroke count).
* Tools with empty content are skipped entirely.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProjectTool

# Per-tool text cap (characters). A few KB is plenty for a notes scratchpad
# while bounding any single tool's contribution to the prompt.
_PER_TOOL_CHAR_CAP = 4000

# Overall cap (characters) across ALL tools returned for one project. Prevents a
# project with many large tools from dominating the agent's context budget.
_OVERALL_CHAR_CAP = 16000

# Appended when a tool's text is truncated at the per-tool cap.
_TRUNCATION_MARKER = "\n\n[…truncated]"


def _render_notes(content: dict) -> str:
    """Render a notes tool's markdown text, truncated to the per-tool cap.

    Returns an empty string when there is no non-whitespace text so empty
    scratchpads are skipped by the caller.
    """
    text = content.get("text")
    if not isinstance(text, str):
        return ""
    text = text.strip()
    if not text:
        return ""
    if len(text) > _PER_TOOL_CHAR_CAP:
        text = text[:_PER_TOOL_CHAR_CAP] + _TRUNCATION_MARKER
    return text


def _render_whiteboard(title: str, content: dict) -> str:
    """Render a compact summary for a whiteboard, including v2 node content.

    For text nodes the operator-authored text is included verbatim (bounded by
    _PER_TOOL_CHAR_CAP). Image and document nodes contribute only to a presence
    count line — their raw data (dataUrl, etc.) is NEVER dumped. Stroke data is
    also never dumped — only the count. Returns an empty string when the board
    has no strokes and no nodes with content.
    """
    strokes = content.get("strokes")
    stroke_count = len(strokes) if isinstance(strokes, list) else 0

    node_list = content.get("nodes")
    node_list = node_list if isinstance(node_list, list) else []

    text_nodes = [n for n in node_list if isinstance(n, dict) and n.get("type") == "text"]
    image_nodes = [n for n in node_list if isinstance(n, dict) and n.get("type") == "image"]
    doc_nodes = [n for n in node_list if isinstance(n, dict) and n.get("type") == "document"]

    text_parts = []
    for node in text_nodes:
        raw_text = node.get("text")
        if not isinstance(raw_text, str):
            continue
        text = raw_text.strip()
        if text:
            text_parts.append(text)

    if stroke_count == 0 and not text_parts and not image_nodes and not doc_nodes:
        return ""

    summary_parts = []
    if stroke_count:
        noun = "stroke" if stroke_count == 1 else "strokes"
        summary_parts.append(f"{stroke_count} {noun}")
    if image_nodes:
        n = len(image_nodes)
        summary_parts.append(f"{n} {'image' if n == 1 else 'images'}")
    if doc_nodes:
        n = len(doc_nodes)
        summary_parts.append(f"{n} {'document' if n == 1 else 'documents'}")

    header = f"Whiteboard '{title}'"
    if summary_parts:
        header += f" has {', '.join(summary_parts)} (visual canvas — not shown as text)."
    else:
        header += " (visual canvas — not shown as text)."

    parts = [header]
    if text_parts:
        parts.append("Text notes on whiteboard:")
        parts.extend(text_parts)

    result = "\n".join(parts)
    if len(result) > _PER_TOOL_CHAR_CAP:
        result = result[:_PER_TOOL_CHAR_CAP] + _TRUNCATION_MARKER
    return result


async def load_project_tool_context(
    session: AsyncSession, project_id: uuid.UUID
) -> list[dict]:
    """Load and serialize a project's enabled built-in tool content.

    Returns a bounded list of ``{"slug", "title", "kind", "text"}`` dicts, one
    per enabled tool that has non-empty renderable content, ordered by creation
    time. Tools with empty content are skipped. The overall payload is capped at
    :data:`_OVERALL_CHAR_CAP` characters — tools past that budget are dropped.

    This is best-effort context, not authoritative data: callers should treat a
    failure as non-fatal (see the claim handler's try/except guard).
    """
    result = await session.execute(
        select(ProjectTool)
        .where(ProjectTool.project_id == project_id)
        .where(ProjectTool.enabled.is_(True))
        .order_by(ProjectTool.created_at)
    )
    tools = result.scalars().all()

    rendered: list[dict] = []
    budget = _OVERALL_CHAR_CAP
    for tool in tools:
        content = tool.content if isinstance(tool.content, dict) else {}
        if tool.kind == "notes":
            text = _render_notes(content)
        elif tool.kind == "whiteboard":
            text = _render_whiteboard(tool.title, content)
        else:
            # Unknown kind: skip rather than guess at a rendering.
            text = ""
        if not text:
            continue
        # Enforce the overall cap: never exceed the remaining budget, and stop
        # once it is exhausted so a huge scratchpad can't blow the prompt.
        if budget <= 0:
            break
        if len(text) > budget:
            text = text[:budget] + _TRUNCATION_MARKER
        budget -= len(text)
        rendered.append(
            {
                "slug": tool.slug,
                "title": tool.title,
                "kind": tool.kind,
                "text": text,
            }
        )
    return rendered

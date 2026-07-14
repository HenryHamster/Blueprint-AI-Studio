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
    """Render a compact presence/summary line for a whiteboard.

    NEVER dumps raw stroke JSON — only a human-readable summary (title + stroke
    count). Returns an empty string when the board has no strokes.
    """
    strokes = content.get("strokes")
    if not isinstance(strokes, list) or len(strokes) == 0:
        return ""
    count = len(strokes)
    noun = "stroke" if count == 1 else "strokes"
    return f"Whiteboard '{title}' has {count} {noun} (visual canvas — not shown as text)."


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

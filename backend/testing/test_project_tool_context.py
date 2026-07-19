"""Unit tests for the built-in project-tool content serializer.

Covers app.project_tool_context.load_project_tool_context: per-kind rendering
(notes markdown, whiteboard summary), skipping of disabled/empty tools, the
per-tool truncation cap, and the overall size cap.
"""

from __future__ import annotations

import pytest

from app.models import Project, ProjectTool
from app.project_tool_context import (
    _OVERALL_CHAR_CAP,
    _PER_TOOL_CHAR_CAP,
    _TRUNCATION_MARKER,
    load_project_tool_context,
)


async def _make_project(session) -> Project:
    project = Project(name="ToolCtx-Test", user_id="test-user-ctx")
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


@pytest.mark.asyncio
async def test_notes_tool_rendered_as_markdown(db_session):
    project = await _make_project(db_session)
    db_session.add(
        ProjectTool(
            project_id=project.id,
            kind="notes",
            slug="notes",
            title="Notes",
            enabled=True,
            content={"text": "# Idea\n\nTest the retry path."},
        )
    )
    await db_session.commit()

    out = await load_project_tool_context(db_session, project.id)
    assert len(out) == 1
    assert out[0] == {
        "slug": "notes",
        "title": "Notes",
        "kind": "notes",
        "text": "# Idea\n\nTest the retry path.",
    }


@pytest.mark.asyncio
async def test_whiteboard_rendered_as_summary_not_raw_json(db_session):
    project = await _make_project(db_session)
    strokes = [{"points": [1, 2, 3]} for _ in range(5)]
    db_session.add(
        ProjectTool(
            project_id=project.id,
            kind="whiteboard",
            slug="wb",
            title="Sketch",
            enabled=True,
            content={"strokes": strokes},
        )
    )
    await db_session.commit()

    out = await load_project_tool_context(db_session, project.id)
    assert len(out) == 1
    text = out[0]["text"]
    assert "Sketch" in text
    assert "5 strokes" in text
    # Never dump raw stroke JSON.
    assert "points" not in text


@pytest.mark.asyncio
async def test_whiteboard_skips_malformed_non_string_text_nodes(db_session):
    project = await _make_project(db_session)
    db_session.add(
        ProjectTool(
            project_id=project.id,
            kind="whiteboard",
            slug="wb-malformed",
            title="Malformed",
            enabled=True,
            content={
                "nodes": [
                    {"type": "text", "text": 1},
                    {"type": "text", "text": {"nested": "bad"}},
                    {"type": "text", "text": " valid note "},
                ]
            },
        )
    )
    await db_session.commit()

    out = await load_project_tool_context(db_session, project.id)
    assert len(out) == 1
    assert "valid note" in out[0]["text"]
    assert "nested" not in out[0]["text"]


@pytest.mark.asyncio
async def test_single_stroke_uses_singular_noun(db_session):
    project = await _make_project(db_session)
    db_session.add(
        ProjectTool(
            project_id=project.id,
            kind="whiteboard",
            slug="wb",
            title="One",
            enabled=True,
            content={"strokes": [{"points": []}]},
        )
    )
    await db_session.commit()

    out = await load_project_tool_context(db_session, project.id)
    assert "1 stroke " in out[0]["text"]
    assert "strokes" not in out[0]["text"]


@pytest.mark.asyncio
async def test_disabled_and_empty_tools_skipped(db_session):
    project = await _make_project(db_session)
    db_session.add_all(
        [
            ProjectTool(
                project_id=project.id, kind="notes", slug="disabled",
                title="Disabled", enabled=False, content={"text": "hidden"},
            ),
            ProjectTool(
                project_id=project.id, kind="notes", slug="blank",
                title="Blank", enabled=True, content={"text": "   "},
            ),
            ProjectTool(
                project_id=project.id, kind="whiteboard", slug="empty-wb",
                title="EmptyBoard", enabled=True, content={"strokes": []},
            ),
            ProjectTool(
                project_id=project.id, kind="notes", slug="real",
                title="Real", enabled=True, content={"text": "keep me"},
            ),
        ]
    )
    await db_session.commit()

    out = await load_project_tool_context(db_session, project.id)
    assert [t["slug"] for t in out] == ["real"]


@pytest.mark.asyncio
async def test_per_tool_truncation(db_session):
    project = await _make_project(db_session)
    big = "x" * (_PER_TOOL_CHAR_CAP + 500)
    db_session.add(
        ProjectTool(
            project_id=project.id, kind="notes", slug="big",
            title="Big", enabled=True, content={"text": big},
        )
    )
    await db_session.commit()

    out = await load_project_tool_context(db_session, project.id)
    text = out[0]["text"]
    assert text.endswith(_TRUNCATION_MARKER)
    assert len(text) == _PER_TOOL_CHAR_CAP + len(_TRUNCATION_MARKER)


@pytest.mark.asyncio
async def test_overall_cap_drops_excess_tools(db_session):
    project = await _make_project(db_session)
    # Each tool is at the per-tool cap; more than fit under the overall cap.
    n = (_OVERALL_CHAR_CAP // _PER_TOOL_CHAR_CAP) + 3
    for i in range(n):
        # Zero-padded slug/title so created_at-tie ordering is stable-ish; the
        # key assertion is the total does not exceed the overall cap.
        db_session.add(
            ProjectTool(
                project_id=project.id, kind="notes", slug=f"t{i:03d}",
                title=f"T{i:03d}", enabled=True,
                content={"text": "y" * _PER_TOOL_CHAR_CAP},
            )
        )
    await db_session.commit()

    out = await load_project_tool_context(db_session, project.id)
    total = sum(len(t["text"]) for t in out)
    # Total text never exceeds the overall cap (plus at most one truncation
    # marker on the final, budget-trimmed tool).
    assert total <= _OVERALL_CHAR_CAP + len(_TRUNCATION_MARKER)
    assert len(out) < n


@pytest.mark.asyncio
async def test_empty_project_returns_empty_list(db_session):
    project = await _make_project(db_session)
    out = await load_project_tool_context(db_session, project.id)
    assert out == []


# --- Whiteboard v2 node tests ---

@pytest.mark.asyncio
async def test_whiteboard_nodes_only_not_dropped(db_session):
    """A board with only nodes (no strokes) must produce a non-empty entry."""
    project = await _make_project(db_session)
    db_session.add(
        ProjectTool(
            project_id=project.id,
            kind="whiteboard",
            slug="wb-nodes",
            title="NodeBoard",
            enabled=True,
            content={"nodes": [{"type": "text", "text": "hello"}]},
        )
    )
    await db_session.commit()

    out = await load_project_tool_context(db_session, project.id)
    assert len(out) == 1
    assert out[0]["slug"] == "wb-nodes"


@pytest.mark.asyncio
async def test_whiteboard_text_node_content_included(db_session):
    """Text node .text values must appear in the rendered output."""
    project = await _make_project(db_session)
    db_session.add(
        ProjectTool(
            project_id=project.id,
            kind="whiteboard",
            slug="wb-text",
            title="TextBoard",
            enabled=True,
            content={"nodes": [
                {"type": "text", "text": "First idea"},
                {"type": "text", "text": "Second idea"},
            ]},
        )
    )
    await db_session.commit()

    out = await load_project_tool_context(db_session, project.id)
    assert len(out) == 1
    text = out[0]["text"]
    assert "First idea" in text
    assert "Second idea" in text
    # Raw node JSON must not be dumped.
    assert "dataUrl" not in text


@pytest.mark.asyncio
async def test_whiteboard_image_and_doc_nodes_counted(db_session):
    """Image and document nodes must appear as presence counts, not raw data."""
    project = await _make_project(db_session)
    db_session.add(
        ProjectTool(
            project_id=project.id,
            kind="whiteboard",
            slug="wb-media",
            title="MediaBoard",
            enabled=True,
            content={"nodes": [
                {"type": "image", "dataUrl": "data:image/png;base64,AAAA"},
                {"type": "document", "name": "spec.pdf"},
            ]},
        )
    )
    await db_session.commit()

    out = await load_project_tool_context(db_session, project.id)
    assert len(out) == 1
    text = out[0]["text"]
    assert "1 image" in text
    assert "1 document" in text
    # Raw data must never appear.
    assert "dataUrl" not in text
    assert "base64" not in text


@pytest.mark.asyncio
async def test_whiteboard_mixed_strokes_and_nodes(db_session):
    """A board with both strokes and nodes must render a combined summary."""
    project = await _make_project(db_session)
    db_session.add(
        ProjectTool(
            project_id=project.id,
            kind="whiteboard",
            slug="wb-mixed",
            title="MixedBoard",
            enabled=True,
            content={
                "strokes": [{"points": [0, 1]}, {"points": [2, 3]}],
                "nodes": [
                    {"type": "text", "text": "caption"},
                    {"type": "image", "dataUrl": "data:image/png;base64,BBB"},
                ],
            },
        )
    )
    await db_session.commit()

    out = await load_project_tool_context(db_session, project.id)
    assert len(out) == 1
    text = out[0]["text"]
    assert "2 strokes" in text
    assert "1 image" in text
    assert "caption" in text
    assert "points" not in text
    assert "dataUrl" not in text


@pytest.mark.asyncio
async def test_whiteboard_empty_text_nodes_still_empty(db_session):
    """Blank text nodes alone must not produce output — board should be skipped."""
    project = await _make_project(db_session)
    db_session.add(
        ProjectTool(
            project_id=project.id,
            kind="whiteboard",
            slug="wb-blank-nodes",
            title="BlankNodeBoard",
            enabled=True,
            content={"nodes": [{"type": "text", "text": "   "}]},
        )
    )
    await db_session.commit()

    out = await load_project_tool_context(db_session, project.id)
    assert out == []


@pytest.mark.asyncio
async def test_whiteboard_summary_not_raw_json_with_nodes(db_session):
    """Extend raw-JSON guard: node dataUrl must never appear in output."""
    project = await _make_project(db_session)
    db_session.add(
        ProjectTool(
            project_id=project.id,
            kind="whiteboard",
            slug="wb-guard",
            title="GuardBoard",
            enabled=True,
            content={
                "strokes": [{"points": [1, 2, 3]}],
                "nodes": [{"type": "image", "dataUrl": "data:image/png;base64,XYZ"}],
            },
        )
    )
    await db_session.commit()

    out = await load_project_tool_context(db_session, project.id)
    assert len(out) == 1
    text = out[0]["text"]
    assert "GuardBoard" in text
    assert "points" not in text
    assert "dataUrl" not in text

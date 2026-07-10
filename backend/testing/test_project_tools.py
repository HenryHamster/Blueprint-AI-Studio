"""Tests for the per-project custom UI tools endpoints + tool-kind drift guard.

Exercises the ``admin_project_tools`` router:
  GET    /api/admin/projects/{project_id}/tools
  POST   /api/admin/projects/{project_id}/tools
  PATCH  /api/admin/projects/{project_id}/tools/{tool_id}
  DELETE /api/admin/projects/{project_id}/tools/{tool_id}
  GET    /api/admin/projects/{project_id}/tools/{tool_id}/content
  PUT    /api/admin/projects/{project_id}/tools/{tool_id}/content
  POST   /api/admin/projects/{project_id}/tools/seed

Plus the TOOL_KINDS ↔ tool_kinds_manifest.json parity guard, mirroring the
STRATEGIC_OPS_REGISTRY ↔ strategic_ops_manifest.json pattern.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.models import Project, ProjectMember

TEST_USER_ID = "test-user-123"

_BASE = "/api/admin"

_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "tool_kinds_manifest.json"
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _make_project_member(session, role: str = "owner") -> Project:
    project = Project(name="ProjectTool-Test", user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role=role))
    await session.commit()
    return project


# ---------------------------------------------------------------------------
# TOOL_KINDS ↔ manifest parity (pure source/JSON, no DB)
# ---------------------------------------------------------------------------


def test_manifest_is_wellformed():
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(manifest, list) and manifest, "manifest must be a non-empty list"
    seen: set[str] = set()
    for kind in manifest:
        assert isinstance(kind, str) and kind, f"manifest kind must be non-empty str: {kind!r}"
        assert kind not in seen, f"duplicate kind in manifest: {kind}"
        seen.add(kind)


def test_tool_kinds_match_manifest_exactly():
    """The Python TOOL_KINDS constant and the checked-in manifest must list the
    SAME kinds — no kind in code that isn't in the manifest, and vice versa."""
    from app.routers.admin_project_tools import TOOL_KINDS

    manifest_kinds = set(json.loads(_MANIFEST_PATH.read_text(encoding="utf-8")))
    code_kinds = set(TOOL_KINDS)

    missing_in_code = manifest_kinds - code_kinds
    silent_addition = code_kinds - manifest_kinds
    assert not missing_in_code, (
        f"manifest kinds with no TOOL_KINDS entry: {sorted(missing_in_code)}"
    )
    assert not silent_addition, (
        "TOOL_KINDS entries missing from tool_kinds_manifest.json "
        f"(add them to the manifest): {sorted(silent_addition)}"
    )
    assert manifest_kinds == code_kinds


def test_tool_kinds_entries_wellformed():
    from app.routers.admin_project_tools import TOOL_KINDS

    for kind, spec in TOOL_KINDS.items():
        assert set(spec) >= {"config_schema", "default_title", "default_placement"}, kind
        assert isinstance(spec["config_schema"], dict), kind
        assert isinstance(spec["default_title"], str) and spec["default_title"], kind
        assert isinstance(spec["default_placement"], str) and spec["default_placement"], kind


# ---------------------------------------------------------------------------
# GET /tools (list)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_empty(client: AsyncClient, db_session):
    project = await _make_project_member(db_session)
    resp = await client.get(f"{_BASE}/projects/{project.id}/tools")
    assert resp.status_code == 200
    assert resp.json()["tools"] == []


@pytest.mark.asyncio
async def test_list_tools_after_seed(client: AsyncClient, db_session):
    project = await _make_project_member(db_session)
    seed = await client.post(f"{_BASE}/projects/{project.id}/tools/seed")
    assert seed.status_code == 200
    assert seed.json()["added"] == ["whiteboard"]

    resp = await client.get(f"{_BASE}/projects/{project.id}/tools")
    assert resp.status_code == 200
    tools = resp.json()["tools"]
    assert len(tools) == 1
    t = tools[0]
    assert t["kind"] == "whiteboard"
    assert t["slug"] == "whiteboard"
    assert t["title"] == "Whiteboard"
    assert t["placement"] == "project_section"
    assert t["enabled"] is True
    assert t["content_revision"] == 0
    # Lean projection must NOT leak content.
    assert "content" not in t


@pytest.mark.asyncio
async def test_list_tools_requires_membership(client: AsyncClient, db_session):
    """A non-member gets 404 (project appears not found), never another project's tools."""
    other = Project(name="Other", user_id="other-user")
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)

    resp = await client.get(f"{_BASE}/projects/{other.id}/tools")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /tools (register)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_tool_valid(client: AsyncClient, db_session):
    project = await _make_project_member(db_session)
    resp = await client.post(
        f"{_BASE}/projects/{project.id}/tools",
        json={"kind": "whiteboard", "slug": "board-1", "title": "My Board"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "whiteboard"
    assert body["slug"] == "board-1"
    assert body["title"] == "My Board"
    assert body["content_revision"] == 0


@pytest.mark.asyncio
async def test_register_tool_defaults_slug_and_title(client: AsyncClient, db_session):
    project = await _make_project_member(db_session)
    resp = await client.post(
        f"{_BASE}/projects/{project.id}/tools",
        json={"kind": "whiteboard"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slug"] == "whiteboard"
    assert body["title"] == "Whiteboard"
    assert body["placement"] == "project_section"


@pytest.mark.asyncio
async def test_register_tool_invalid_kind(client: AsyncClient, db_session):
    project = await _make_project_member(db_session)
    resp = await client.post(
        f"{_BASE}/projects/{project.id}/tools",
        json={"kind": "not-a-real-kind"},
    )
    assert resp.status_code == 400
    # The app reshapes HTTPException bodies to {"error": {"code", "message"}}.
    assert "Invalid tool kind" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_register_tool_duplicate_slug(client: AsyncClient, db_session):
    project = await _make_project_member(db_session)
    first = await client.post(
        f"{_BASE}/projects/{project.id}/tools",
        json={"kind": "whiteboard", "slug": "dup"},
    )
    assert first.status_code == 200
    second = await client.post(
        f"{_BASE}/projects/{project.id}/tools",
        json={"kind": "whiteboard", "slug": "dup"},
    )
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_register_tool_requires_owner(client: AsyncClient, db_session):
    """A member (below owner) cannot register a tool -> 403."""
    project = await _make_project_member(db_session, role="member")
    resp = await client.post(
        f"{_BASE}/projects/{project.id}/tools",
        json={"kind": "whiteboard"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PATCH /tools/{id} (update metadata)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_tool_rename_and_disable(client: AsyncClient, db_session):
    project = await _make_project_member(db_session)
    created = (
        await client.post(
            f"{_BASE}/projects/{project.id}/tools",
            json={"kind": "whiteboard", "slug": "b"},
        )
    ).json()
    tool_id = created["id"]

    resp = await client.patch(
        f"{_BASE}/projects/{project.id}/tools/{tool_id}",
        json={"title": "Renamed", "enabled": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "Renamed"
    assert body["enabled"] is False


@pytest.mark.asyncio
async def test_update_tool_not_found(client: AsyncClient, db_session):
    import uuid as _uuid

    project = await _make_project_member(db_session)
    resp = await client.patch(
        f"{_BASE}/projects/{project.id}/tools/{_uuid.uuid4()}",
        json={"title": "X"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /tools/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_tool(client: AsyncClient, db_session):
    project = await _make_project_member(db_session)
    created = (
        await client.post(
            f"{_BASE}/projects/{project.id}/tools",
            json={"kind": "whiteboard", "slug": "gone"},
        )
    ).json()
    tool_id = created["id"]

    resp = await client.delete(f"{_BASE}/projects/{project.id}/tools/{tool_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"

    # Now gone.
    listed = (await client.get(f"{_BASE}/projects/{project.id}/tools")).json()["tools"]
    assert listed == []
    again = await client.delete(f"{_BASE}/projects/{project.id}/tools/{tool_id}")
    assert again.status_code == 404


# ---------------------------------------------------------------------------
# GET/PUT /tools/{id}/content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_roundtrip_and_stale_conflict(client: AsyncClient, db_session):
    project = await _make_project_member(db_session)
    created = (
        await client.post(
            f"{_BASE}/projects/{project.id}/tools",
            json={"kind": "whiteboard", "slug": "c"},
        )
    ).json()
    tool_id = created["id"]

    # Initial content is empty at revision 0.
    got = await client.get(f"{_BASE}/projects/{project.id}/tools/{tool_id}/content")
    assert got.status_code == 200
    assert got.json() == {"content": {}, "content_revision": 0}

    # Save at base_revision 0 -> revision becomes 1.
    saved = await client.put(
        f"{_BASE}/projects/{project.id}/tools/{tool_id}/content",
        json={"content": {"strokes": [1, 2, 3]}, "base_revision": 0},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["content_revision"] == 1

    # Re-read reflects the write.
    got2 = await client.get(f"{_BASE}/projects/{project.id}/tools/{tool_id}/content")
    assert got2.json() == {"content": {"strokes": [1, 2, 3]}, "content_revision": 1}

    # Saving again with the now-stale base_revision 0 -> 409.
    stale = await client.put(
        f"{_BASE}/projects/{project.id}/tools/{tool_id}/content",
        json={"content": {"strokes": []}, "base_revision": 0},
    )
    assert stale.status_code == 409

    # Content untouched by the rejected write.
    got3 = await client.get(f"{_BASE}/projects/{project.id}/tools/{tool_id}/content")
    assert got3.json()["content"] == {"strokes": [1, 2, 3]}
    assert got3.json()["content_revision"] == 1


# ---------------------------------------------------------------------------
# POST /tools/seed idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_is_idempotent(client: AsyncClient, db_session):
    project = await _make_project_member(db_session)

    first = await client.post(f"{_BASE}/projects/{project.id}/tools/seed")
    assert first.status_code == 200
    assert first.json()["added"] == ["whiteboard"]
    assert first.json()["skipped"] == []

    second = await client.post(f"{_BASE}/projects/{project.id}/tools/seed")
    assert second.status_code == 200
    assert second.json()["added"] == []
    assert second.json()["skipped"] == ["whiteboard"]

    # Still exactly one tool after repeated seeding.
    tools = (await client.get(f"{_BASE}/projects/{project.id}/tools")).json()["tools"]
    assert len(tools) == 1


@pytest.mark.asyncio
async def test_seed_requires_owner(client: AsyncClient, db_session):
    """A member (below owner) cannot seed default tools -> 403."""
    project = await _make_project_member(db_session, role="member")
    resp = await client.post(f"{_BASE}/projects/{project.id}/tools/seed")
    assert resp.status_code == 403

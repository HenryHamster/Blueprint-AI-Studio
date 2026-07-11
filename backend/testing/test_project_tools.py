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

from app.models import Project, ProjectMember, ProjectTool

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
        assert set(spec) >= {
            "config_schema", "default_title", "default_placement",
            "label", "description",
        }, kind
        assert isinstance(spec["config_schema"], dict), kind
        assert isinstance(spec["default_title"], str) and spec["default_title"], kind
        assert isinstance(spec["default_placement"], str) and spec["default_placement"], kind
        assert isinstance(spec["label"], str) and spec["label"], kind
        assert isinstance(spec["description"], str) and spec["description"], kind


# ---------------------------------------------------------------------------
# GET /tool-kinds (dynamic kind discovery — no DB needed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tool_kinds_endpoint(client: AsyncClient, db_session):
    """The /tool-kinds endpoint returns a bare array of kind configs."""
    resp = await client.get(f"{_BASE}/tool-kinds")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list), "must return a bare array, not a wrapped object"
    assert len(body) >= 1


@pytest.mark.asyncio
async def test_list_tool_kinds_response_matches_ts_interface(
    client: AsyncClient, db_session
):
    """Response shape must match the TypeScript ToolKindConfig interface:
    kind, label, description, icon?, config_schema?."""
    resp = await client.get(f"{_BASE}/tool-kinds")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) >= 1

    for entry in body:
        required = {"kind", "label", "description"}
        missing = required - set(entry.keys())
        assert not missing, f"entry missing fields: {sorted(missing)} in {entry['kind']}"

        assert isinstance(entry["kind"], str) and entry["kind"], entry
        assert isinstance(entry["label"], str) and entry["label"], entry
        assert isinstance(entry["description"], str), entry
        # icon is optional; config_schema is optional but should be dict when present
        if "icon" in entry:
            assert isinstance(entry["icon"], str), entry
        if "config_schema" in entry:
            assert isinstance(entry["config_schema"], dict), entry


@pytest.mark.asyncio
async def test_list_tool_kinds_whiteboard_known(client: AsyncClient, db_session):
    """The whiteboard kind is always present with known metadata."""
    resp = await client.get(f"{_BASE}/tool-kinds")
    assert resp.status_code == 200
    kinds = {e["kind"]: e for e in resp.json()}
    assert "whiteboard" in kinds
    wb = kinds["whiteboard"]
    assert wb["label"] == "Whiteboard"
    assert isinstance(wb["description"], str) and wb["description"]


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


@pytest.mark.asyncio
async def test_get_tool_content_requires_membership(client: AsyncClient, db_session):
    """Non-member gets 404 on tool content GET/PUT; a member can read and write."""
    import uuid as _uuid

    # ── Non-member: project owned by another user with a tool ──
    other_uid = "other-" + str(_uuid.uuid4())[:8]
    other = Project(name="Other-Content", user_id=other_uid)
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)

    other_tool = ProjectTool(
        project_id=other.id,
        kind="whiteboard",
        slug="other-tool",
        title="Other Tool",
        placement="project_section",
        created_by=other_uid,
    )
    db_session.add(other_tool)
    await db_session.commit()
    await db_session.refresh(other_tool)

    # GET content as non-member → 404 (project not found).
    resp = await client.get(
        f"{_BASE}/projects/{other.id}/tools/{other_tool.id}/content"
    )
    assert resp.status_code == 404

    # PUT content as non-member → 404.
    resp = await client.put(
        f"{_BASE}/projects/{other.id}/tools/{other_tool.id}/content",
        json={"content": {"x": 1}, "base_revision": 0},
    )
    assert resp.status_code == 404

    # ── Member (not owner) can read content ──
    member_proj = await _make_project_member(db_session, role="member")
    member_tool = ProjectTool(
        project_id=member_proj.id,
        kind="whiteboard",
        slug="member-tool",
        title="Member Tool",
        placement="project_section",
        created_by=TEST_USER_ID,
    )
    db_session.add(member_tool)
    await db_session.commit()
    await db_session.refresh(member_tool)

    resp = await client.get(
        f"{_BASE}/projects/{member_proj.id}/tools/{member_tool.id}/content"
    )
    assert resp.status_code == 200
    assert resp.json() == {"content": {}, "content_revision": 0}

    # Member can save content.
    resp = await client.put(
        f"{_BASE}/projects/{member_proj.id}/tools/{member_tool.id}/content",
        json={"content": {"strokes": [1]}, "base_revision": 0},
    )
    assert resp.status_code == 200
    assert resp.json()["content_revision"] == 1


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


# ---------------------------------------------------------------------------
# Client contract: list response shape must match the TS ProjectTool interface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_response_shape_matches_client_contract(
    client: AsyncClient, db_session
):
    """The GET /tools list response must match the TypeScript ProjectTool interface.

    The TS client (fetchProjectTools) unwraps {"tools": [...]} and uses:
      id, kind, slug, title, enabled, placement, content_revision.
    "label" must NOT appear (old name that caused the original contract bug).
    "content" must NOT appear (lean projection; fetched separately via /content).
    """
    project = await _make_project_member(db_session)
    seed = await client.post(f"{_BASE}/projects/{project.id}/tools/seed")
    assert seed.status_code == 200

    resp = await client.get(f"{_BASE}/projects/{project.id}/tools")
    assert resp.status_code == 200
    body = resp.json()

    # Outer envelope the TS client unwraps
    assert "tools" in body, "response must be {'tools': [...]}, not a bare array"
    assert isinstance(body["tools"], list)

    tool = body["tools"][0]
    # Fields the TS ProjectTool interface reads
    required_fields = {"id", "kind", "slug", "title", "enabled", "placement", "content_revision"}
    missing = required_fields - set(tool.keys())
    assert not missing, f"list item missing fields expected by TS client: {sorted(missing)}"

    # "label" was the old (wrong) field name — must not be present
    assert "label" not in tool, "field 'label' must not appear; TS interface uses 'title'"
    # content is fetched separately via /content endpoint (lean projection)
    assert "content" not in tool, "lean list projection must not include content blob"

    # Spot-check field types align with TS interface
    assert isinstance(tool["id"], str)
    assert isinstance(tool["title"], str) and tool["title"]
    assert isinstance(tool["slug"], str) and tool["slug"]
    assert isinstance(tool["enabled"], bool)
    assert isinstance(tool["content_revision"], int)


# ---------------------------------------------------------------------------
# Config validation against kind config_schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_tool_with_config(client: AsyncClient, db_session):
    """Whiteboard has an empty config_schema so any dict config is accepted."""
    project = await _make_project_member(db_session)
    resp = await client.post(
        f"{_BASE}/projects/{project.id}/tools",
        json={"kind": "whiteboard", "config": {"theme": "dark", "grid": True}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "whiteboard"


@pytest.mark.asyncio
async def test_register_tool_config_rejected_by_schema(
    client: AsyncClient, db_session
):
    """With a patched schema that requires an api_key, missing it is rejected."""
    from unittest.mock import patch
    from app.routers.admin_project_tools import TOOL_KINDS

    project = await _make_project_member(db_session)
    patched_kinds = {
        **TOOL_KINDS,
        "test-kind": {
            "config_schema": {
                "type": "object",
                "required": ["api_key"],
                "properties": {"api_key": {"type": "string"}},
            },
            "default_title": "Test",
            "default_placement": "project_section",
        },
    }
    with patch("app.routers.admin_project_tools.TOOL_KINDS", patched_kinds):
        resp = await client.post(
            f"{_BASE}/projects/{project.id}/tools",
            json={"kind": "test-kind", "config": {"not_api_key": 123}},
        )
    assert resp.status_code == 400
    assert "Invalid config" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_register_tool_valid_config_passes_schema(
    client: AsyncClient, db_session
):
    """With a patched schema, config that satisfies constraints is accepted."""
    from unittest.mock import patch
    from app.routers.admin_project_tools import TOOL_KINDS

    project = await _make_project_member(db_session)
    patched_kinds = {
        **TOOL_KINDS,
        "test-kind": {
            "config_schema": {
                "type": "object",
                "required": ["api_key"],
                "properties": {"api_key": {"type": "string"}},
            },
            "default_title": "Test",
            "default_placement": "project_section",
        },
    }
    with patch("app.routers.admin_project_tools.TOOL_KINDS", patched_kinds):
        resp = await client.post(
            f"{_BASE}/projects/{project.id}/tools",
            json={"kind": "test-kind", "config": {"api_key": "sk-abc"}},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "test-kind"


@pytest.mark.asyncio
async def test_update_tool_config(client: AsyncClient, db_session):
    """PATCH with a config dict is accepted (whiteboard schema is empty)."""
    project = await _make_project_member(db_session)
    created = (
        await client.post(
            f"{_BASE}/projects/{project.id}/tools",
            json={"kind": "whiteboard", "slug": "cfg-upd"},
        )
    ).json()
    tool_id = created["id"]

    resp = await client.patch(
        f"{_BASE}/projects/{project.id}/tools/{tool_id}",
        json={"config": {"theme": "dark"}},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_update_tool_config_rejected_by_schema(
    client: AsyncClient, db_session
):
    """PATCH with config violating the kind's config_schema is rejected."""
    from unittest.mock import patch
    from app.routers.admin_project_tools import TOOL_KINDS

    project = await _make_project_member(db_session)
    created = (
        await client.post(
            f"{_BASE}/projects/{project.id}/tools",
            json={"kind": "whiteboard", "slug": "cfg-rej"},
        )
    ).json()
    tool_id = created["id"]

    patched_kinds = {
        **TOOL_KINDS,
        "whiteboard": {
            **TOOL_KINDS["whiteboard"],
            "config_schema": {
                "type": "object",
                "required": ["api_key"],
                "properties": {"api_key": {"type": "string"}},
            },
        },
    }
    with patch("app.routers.admin_project_tools.TOOL_KINDS", patched_kinds):
        resp = await client.patch(
            f"{_BASE}/projects/{project.id}/tools/{tool_id}",
            json={"config": {"not_api_key": True}},
        )
    assert resp.status_code == 400
    assert "Invalid config" in resp.json()["error"]["message"]

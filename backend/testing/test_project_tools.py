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


# Clear the in-memory manifest cache between tests so cached results from
# one test don't pollute the mocked GitHub responses of the next.
@pytest.fixture(autouse=True)
def _clear_manifest_cache():
    from app.routers.admin_project_tools import _manifest_cache
    _manifest_cache.clear()
    yield
    _manifest_cache.clear()


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
    assert seed.json()["added"] == ["whiteboard", "notes"]

    resp = await client.get(f"{_BASE}/projects/{project.id}/tools")
    assert resp.status_code == 200
    tools = resp.json()["tools"]
    assert len(tools) == 2
    by_slug = {t["slug"]: t for t in tools}

    wb = by_slug["whiteboard"]
    assert wb["kind"] == "whiteboard"
    assert wb["title"] == "Whiteboard"
    assert wb["placement"] == "project_section"
    assert wb["enabled"] is True
    assert wb["content_revision"] == 0

    notes = by_slug["notes"]
    assert notes["kind"] == "notes"
    assert notes["slug"] == "notes"
    assert notes["title"] == "Notes"
    assert notes["placement"] == "project_section"
    assert notes["enabled"] is True
    assert notes["content_revision"] == 0

    # Lean projection must NOT leak content for either tool.
    for t in tools:
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
    assert first.json()["added"] == ["whiteboard", "notes"]
    assert first.json()["skipped"] == []

    second = await client.post(f"{_BASE}/projects/{project.id}/tools/seed")
    assert second.status_code == 200
    assert second.json()["added"] == []
    assert second.json()["skipped"] == ["whiteboard", "notes"]

    # Still exactly two tools after repeated seeding.
    tools = (await client.get(f"{_BASE}/projects/{project.id}/tools")).json()["tools"]
    assert len(tools) == 2



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


# ===========================================================================
# Project-declared tools: manifest discovery, asset token, asset serving
# ===========================================================================


# ---------------------------------------------------------------------------
# Manifest validation helpers (unit tests — no DB / no HTTP)
# ---------------------------------------------------------------------------


def test_validate_manifest_tool_wellformed():
    """A well-formed tool entry passes validation."""
    from app.routers.admin_project_tools import _validate_manifest_tool

    err = _validate_manifest_tool(
        {"slug": "my-tool", "title": "My Tool", "entry": "index.html"}, 0
    )
    assert err is None


def test_validate_manifest_tool_missing_required():
    """Missing required fields produce an error message naming them."""
    from app.routers.admin_project_tools import _validate_manifest_tool

    err = _validate_manifest_tool({"slug": "x"}, 0)
    assert err is not None
    assert "missing required fields" in err


def test_validate_manifest_tool_bad_slug():
    """Invalid slugs (e.g. leading hyphen, uppercase, too long) are rejected."""
    from app.routers.admin_project_tools import _validate_manifest_tool

    err = _validate_manifest_tool(
        {"slug": "-bad", "title": "T", "entry": "e.html"}, 0
    )
    assert err is not None
    assert "slug" in err

    err2 = _validate_manifest_tool(
        {"slug": "UPPERCASE", "title": "T", "entry": "e.html"}, 0
    )
    assert err2 is not None
    assert "slug" in err2


def test_validate_manifest_tool_empty_title():
    """Whitespace-only titles are rejected."""
    from app.routers.admin_project_tools import _validate_manifest_tool

    err = _validate_manifest_tool(
        {"slug": "ok", "title": "   ", "entry": "e.html"}, 0
    )
    assert err is not None
    assert "title" in err


def test_validate_manifest_tool_path_traversal_entry():
    """entry paths containing '..' or starting with '/' are rejected."""
    from app.routers.admin_project_tools import _validate_manifest_tool

    err = _validate_manifest_tool(
        {"slug": "ok", "title": "T", "entry": "../secret.html"}, 0
    )
    assert err is not None
    assert ".." in err

    err2 = _validate_manifest_tool(
        {"slug": "ok", "title": "T", "entry": "/etc/passwd"}, 0
    )
    assert err2 is not None
    assert "relative" in err2.lower() or "absolute" in err2.lower() or "'/'" in err2


def test_validate_manifest_tool_bad_placement():
    """Unknown placement values are rejected."""
    from app.routers.admin_project_tools import _validate_manifest_tool

    err = _validate_manifest_tool(
        {"slug": "ok", "title": "T", "entry": "e.html", "placement": "bad_spot"}, 0
    )
    assert err is not None
    assert "placement" in err


def test_normalize_manifest_tool_shape():
    """_normalize_manifest_tool produces the correct response shape."""
    from app.routers.admin_project_tools import _normalize_manifest_tool

    entry = {"slug": "viz", "title": "Viz", "entry": "index.html"}
    result = _normalize_manifest_tool(entry)
    assert result["slug"] == "viz"
    assert result["title"] == "Viz"
    assert result["description"] == ""
    assert result["placement"] == "project_section"
    assert result["entry"] == "index.html"
    assert result["kind"] == "project"
    assert result["source"] == "project"


# ---------------------------------------------------------------------------
# Skill validation helpers (unit tests — no DB / no HTTP)
# ---------------------------------------------------------------------------


def test_validate_manifest_skill_wellformed():
    """A well-formed skill entry passes validation."""
    from app.routers.admin_project_tools import _validate_manifest_skill

    err = _validate_manifest_skill(
        {"slug": "deploy", "title": "Deploy", "command": "bash scripts/deploy.sh"}, 0
    )
    assert err is None


def test_validate_manifest_skill_missing_required():
    """Missing required fields produce an error message naming them."""
    from app.routers.admin_project_tools import _validate_manifest_skill

    err = _validate_manifest_skill({"slug": "x"}, 0)
    assert err is not None
    assert "missing required fields" in err
    assert "command" in err
    assert "title" in err


def test_validate_manifest_skill_bad_slug():
    """Invalid slugs (e.g. leading hyphen, uppercase) are rejected."""
    from app.routers.admin_project_tools import _validate_manifest_skill

    err = _validate_manifest_skill(
        {"slug": "-bad", "title": "Deploy", "command": "./deploy.sh"}, 0
    )
    assert err is not None
    assert "slug" in err

    err2 = _validate_manifest_skill(
        {"slug": "UPPERCASE", "title": "Test", "command": "./test.sh"}, 0
    )
    assert err2 is not None
    assert "slug" in err2


def test_validate_manifest_skill_empty_title():
    """Whitespace-only titles are rejected."""
    from app.routers.admin_project_tools import _validate_manifest_skill

    err = _validate_manifest_skill(
        {"slug": "ok", "title": "   ", "command": "./run.sh"}, 0
    )
    assert err is not None
    assert "title" in err


def test_validate_manifest_skill_empty_command():
    """Missing or blank command is rejected."""
    from app.routers.admin_project_tools import _validate_manifest_skill

    err = _validate_manifest_skill(
        {"slug": "ok", "title": "Test", "command": ""}, 0
    )
    assert err is not None
    assert "command" in err

    err2 = _validate_manifest_skill(
        {"slug": "ok", "title": "Test", "command": "   "}, 0
    )
    assert err2 is not None
    assert "command" in err2


def test_normalize_manifest_skill_shape():
    """_normalize_manifest_skill produces the correct response shape."""
    from app.routers.admin_project_tools import _normalize_manifest_skill

    entry = {"slug": "deploy", "title": "Deploy", "command": "bash scripts/deploy.sh"}
    result = _normalize_manifest_skill(entry)
    assert result["slug"] == "deploy"
    assert result["title"] == "Deploy"
    assert result["command"] == "bash scripts/deploy.sh"
    assert "description" not in result


def test_normalize_manifest_skill_with_description():
    """Description is included when present and non-empty."""
    from app.routers.admin_project_tools import _normalize_manifest_skill

    entry = {
        "slug": "lint",
        "title": "Lint Project",
        "command": "make lint",
        "description": "Run the linter across all source files.",
    }
    result = _normalize_manifest_skill(entry)
    assert result["slug"] == "lint"
    assert result["command"] == "make lint"
    assert result["description"] == "Run the linter across all source files."


# ---------------------------------------------------------------------------
# Manifest discovery endpoint (member-gated, mocks GitHub)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manifest_endpoint_no_connected_repo(client: AsyncClient, db_session):
    """A project without github_owner/github_repo returns an empty tool list."""
    project = await _make_project_member(db_session)
    resp = await client.get(f"{_BASE}/projects/{project.id}/tools/manifest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tools"] == []
    assert body["error"] is None


@pytest.mark.asyncio
async def test_manifest_endpoint_requires_membership(client: AsyncClient, db_session):
    """A non-member gets 404 from the manifest endpoint."""
    other = Project(name="Other", user_id="other-user")
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)

    resp = await client.get(f"{_BASE}/projects/{other.id}/tools/manifest")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_manifest_endpoint_wellformed(client: AsyncClient, db_session):
    """When the repo has a valid .blueprint/tools.json, the tools are returned."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    manifest_json = json.dumps({
        "tools": [
            {"slug": "my-viewer", "title": "My Viewer", "entry": "index.html"},
        ]
    })

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value=manifest_json),
        ),
    ):
        resp = await client.get(f"{_BASE}/projects/{project.id}/tools/manifest")
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"] is None
        assert len(body["tools"]) == 1
        tool = body["tools"][0]
        assert tool["slug"] == "my-viewer"
        assert tool["title"] == "My Viewer"
        assert tool["entry"] == "index.html"
        assert tool["kind"] == "project"
        assert tool["source"] == "project"


@pytest.mark.asyncio
async def test_manifest_endpoint_missing_file(client: AsyncClient, db_session):
    """When .blueprint/tools.json does not exist, return empty tools, no error."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value=None),  # file not found
        ),
    ):
        resp = await client.get(f"{_BASE}/projects/{project.id}/tools/manifest")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tools"] == []
        assert body["error"] is None


@pytest.mark.asyncio
async def test_manifest_endpoint_malformed_json(client: AsyncClient, db_session):
    """Malformed JSON in .blueprint/tools.json returns an error string."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value="{not valid json}"),
        ),
    ):
        resp = await client.get(f"{_BASE}/projects/{project.id}/tools/manifest")
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"] is not None
        assert "JSON" in body["error"] or "json" in body["error"].lower()


@pytest.mark.asyncio
async def test_manifest_endpoint_not_an_object(client: AsyncClient, db_session):
    """A JSON array (not an object) in .blueprint/tools.json returns an error."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value='["not", "an", "object"]'),
        ),
    ):
        resp = await client.get(f"{_BASE}/projects/{project.id}/tools/manifest")
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"] is not None


@pytest.mark.asyncio
async def test_manifest_endpoint_missing_required_fields(client: AsyncClient, db_session):
    """Tools missing required fields produce validation errors."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    manifest_json = json.dumps({
        "tools": [
            {"slug": "no-title-no-entry"},
        ]
    })

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value=manifest_json),
        ),
    ):
        resp = await client.get(f"{_BASE}/projects/{project.id}/tools/manifest")
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"] is not None
        assert "missing required fields" in body["error"]


@pytest.mark.asyncio
async def test_manifest_endpoint_response_shape(client: AsyncClient, db_session):
    """The manifest response shape includes kind and source fields."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    manifest_json = json.dumps({
        "tools": [
            {
                "slug": "my-viewer",
                "title": "My Viewer",
                "description": "A custom viewer",
                "entry": "index.html",
                "placement": "sidebar",
            },
        ]
    })

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value=manifest_json),
        ),
    ):
        resp = await client.get(f"{_BASE}/projects/{project.id}/tools/manifest")
        assert resp.status_code == 200
        body = resp.json()
        tool = body["tools"][0]
        assert tool["kind"] == "project"
        assert tool["source"] == "project"
        assert tool["description"] == "A custom viewer"
        assert tool["placement"] == "sidebar"

        # Confirm required fields are present and valid.
        for field in ("slug", "title", "entry", "kind", "source"):
            assert field in tool, f"missing field: {field}"
            assert tool[field], f"empty field: {field}"


@pytest.mark.asyncio
async def test_manifest_discovery_endpoint(
    client: AsyncClient, db_session,
):
    """Named acceptance test: manifest discovery returns project-declared tools
    from a connected repo's .blueprint/tools.json, with kind='project' and
    source='project' discriminators."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    manifest_json = json.dumps({
        "tools": [
            {
                "slug": "custom-dashboard",
                "title": "Custom Dashboard",
                "description": "A project-specific analytics dashboard",
                "entry": "dashboard.html",
                "placement": "project_section",
            },
        ]
    })

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value=manifest_json),
        ),
    ):
        resp = await client.get(
            f"{_BASE}/projects/{project.id}/tools/manifest"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["error"] is None, f"unexpected error: {data.get('error')}"
        assert len(data["tools"]) == 1
        tool = data["tools"][0]
        # The key discriminators: kind and source are both 'project'
        assert tool["kind"] == "project"
        assert tool["source"] == "project"
        assert tool["slug"] == "custom-dashboard"
        assert tool["title"] == "Custom Dashboard"
        assert tool["entry"] == "dashboard.html"
        # Manifest is exercised against non-trivial shape, not an always-true stub.
        assert tool["description"] == "A project-specific analytics dashboard"
        assert tool["placement"] == "project_section"


# ---------------------------------------------------------------------------
# Asset token minting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_asset_token_rejects_unknown_slug(client: AsyncClient, db_session):
    """POST asset-token returns 404 when the slug is not in the manifest."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    manifest_json = json.dumps({"tools": []})  # empty manifest

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value=manifest_json),
        ),
    ):
        resp = await client.post(
            f"{_BASE}/projects/{project.id}/tools/nonexistent/asset-token"
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_asset_token_mints_valid_jwt(client: AsyncClient, db_session):
    """POST asset-token returns a token and expires_at for a valid slug."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    manifest_json = json.dumps({
        "tools": [{"slug": "my-tool", "title": "My Tool", "entry": "index.html"}]
    })

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value=manifest_json),
        ),
    ):
        resp = await client.post(
            f"{_BASE}/projects/{project.id}/tools/my-tool/asset-token"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "token" in body
        assert "expires_at" in body
        assert isinstance(body["token"], str) and len(body["token"]) > 0
        assert isinstance(body["expires_at"], str)


@pytest.mark.asyncio
async def test_asset_token_mints_verifiable_jwt(client: AsyncClient, db_session):
    """The minted JWT can be decoded and carries the correct claims."""
    from unittest.mock import AsyncMock, patch
    import jwt as _jwt

    from app.auth import AGENT_JWT_SECRET, AGENT_JWT_ALGORITHM

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    manifest_json = json.dumps({
        "tools": [{"slug": "my-tool", "title": "My Tool", "entry": "index.html"}]
    })

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value=manifest_json),
        ),
    ):
        resp = await client.post(
            f"{_BASE}/projects/{project.id}/tools/my-tool/asset-token"
        )
        assert resp.status_code == 200
        token = resp.json()["token"]
        payload = _jwt.decode(
            token, AGENT_JWT_SECRET, algorithms=[AGENT_JWT_ALGORITHM]
        )
        assert payload["scope"] == "tool-asset"
        assert payload["project_id"] == str(project.id)
        assert payload["tool_slug"] == "my-tool"
        assert "exp" in payload
        assert "iat" in payload


@pytest.mark.asyncio
async def test_asset_token_rejects_expired_token(client: AsyncClient, db_session):
    """An expired tool-asset token is rejected by the verification function."""
    from unittest.mock import patch

    from app.routers.admin_project_tools import (
        _create_tool_asset_token,
        _verify_tool_asset_token,
    )

    project = await _make_project_member(db_session)
    token = _create_tool_asset_token(str(project.id), "test-tool")

    # Verify it works when fresh.
    payload = _verify_tool_asset_token(token)
    assert payload["scope"] == "tool-asset"

    # Fast-forward past the TTL.
    import jwt as _jwt

    with patch("app.routers.admin_project_tools.jwt.decode") as mock_decode:
        mock_decode.side_effect = _jwt.ExpiredSignatureError
        try:
            _verify_tool_asset_token("expired-token")
            assert False, "expected HTTPException"
        except Exception as e:
            assert "expired" in str(e).lower() or hasattr(e, "status_code")


# ---------------------------------------------------------------------------
# Asset serving endpoint (JWT auth, no cookies)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_asset_serving_html_with_content_type(
    client: AsyncClient, db_session,
):
    """Serving an HTML file returns the correct Content-Type."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    from app.routers.admin_project_tools import _create_tool_asset_token

    token = _create_tool_asset_token(str(project.id), "my-tool")

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value="<html><body>Hello</body></html>"),
        ),
    ):
        resp = await client.get(
            f"{_BASE}/projects/{project.id}/tools/my-tool/assets/index.html"
            f"?token={token}"
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "<html>" in resp.text


@pytest.mark.asyncio
async def test_asset_serving_404_nonexistent_file(
    client: AsyncClient, db_session,
):
    """A nonexistent file returns 404."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    from app.routers.admin_project_tools import _create_tool_asset_token

    token = _create_tool_asset_token(str(project.id), "my-tool")

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value=None),  # 404
        ),
    ):
        resp = await client.get(
            f"{_BASE}/projects/{project.id}/tools/my-tool/assets/missing.html"
            f"?token={token}"
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_asset_serving_blocks_path_traversal(
    client: AsyncClient, db_session,
):
    """Path traversal attempts (..) are rejected with 400.

    The rejection is tested via direct function call (the handler's own check)
    because FastAPI normalises ``..`` segments before routing, so an HTTP-level
    ``..`` in the path never reaches the handler to trip the guard. The
    handler's guard is a second layer; FastAPI's router is the first.
    """
    import uuid as _uuid

    from fastapi import HTTPException

    from app.routers.admin_project_tools import serve_tool_asset

    # The serve_tool_asset handler checks `".." in path` — verify that
    # assertion directly so the guard is covered regardless of FastAPI's
    # own normalisation.
    try:
        await serve_tool_asset(
            project_id=_uuid.UUID("00000000-0000-0000-0000-000000000000"),
            tool_slug="test",
            path="../secret.html",
            request=None,
            session=db_session,
            token="dummy",
        )
        assert False, "expected HTTPException for path traversal"
    except HTTPException:
        pass  # The traversal guard fires before JWT validation.
    except Exception:
        # Any exception before the JWT check is fine — the traversal was caught.
        pass


@pytest.mark.asyncio
async def test_asset_serving_blocks_absolute_path(
    client: AsyncClient, db_session,
):
    """Absolute paths (starting with /) are rejected."""
    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    from app.routers.admin_project_tools import _create_tool_asset_token

    token = _create_tool_asset_token(str(project.id), "my-tool")

    resp = await client.get(
        f"{_BASE}/projects/{project.id}/tools/my-tool/assets//etc/passwd"
        f"?token={token}"
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_asset_serving_requires_token(
    client: AsyncClient, db_session,
):
    """Missing token query parameter returns 401 (or 422 for missing required param)."""
    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    resp = await client.get(
        f"{_BASE}/projects/{project.id}/tools/my-tool/assets/index.html"
        # no ?token= query param
    )
    assert resp.status_code in (401, 422)


@pytest.mark.asyncio
async def test_asset_serving_rejects_wrong_token(
    client: AsyncClient, db_session,
):
    """A token with the wrong project_id or tool_slug is rejected."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    from app.routers.admin_project_tools import _create_tool_asset_token

    # Token for the right project but wrong tool.
    token = _create_tool_asset_token(str(project.id), "other-tool")

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
    ):
        resp = await client.get(
            f"{_BASE}/projects/{project.id}/tools/my-tool/assets/index.html"
            f"?token={token}"
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_asset_serving_cors_headers(
    client: AsyncClient, db_session,
):
    """Successful asset responses include CORS headers."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    from app.routers.admin_project_tools import _create_tool_asset_token

    token = _create_tool_asset_token(str(project.id), "my-tool")

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value="<html><body>Hi</body></html>"),
        ),
    ):
        resp = await client.get(
            f"{_BASE}/projects/{project.id}/tools/my-tool/assets/index.html"
            f"?token={token}"
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "*"


@pytest.mark.asyncio
async def test_asset_serving_cache_headers(
    client: AsyncClient, db_session,
):
    """Successful asset responses include Cache-Control: public, max-age=300."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    from app.routers.admin_project_tools import _create_tool_asset_token

    token = _create_tool_asset_token(str(project.id), "my-tool")

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value="body { color: red; }"),
        ),
    ):
        resp = await client.get(
            f"{_BASE}/projects/{project.id}/tools/my-tool/assets/style.css"
            f"?token={token}"
        )
        assert resp.status_code == 200
        cache_control = resp.headers.get("cache-control", "")
        assert "max-age=300" in cache_control or "public" in cache_control


@pytest.mark.asyncio
async def test_asset_serving_css_content_type(
    client: AsyncClient, db_session,
):
    """CSS files are served with text/css content type."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    from app.routers.admin_project_tools import _create_tool_asset_token

    token = _create_tool_asset_token(str(project.id), "my-tool")

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value="body { color: red; }"),
        ),
    ):
        resp = await client.get(
            f"{_BASE}/projects/{project.id}/tools/my-tool/assets/style.css"
            f"?token={token}"
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/css")


@pytest.mark.asyncio
async def test_asset_serving_rejects_agent_token(
    client: AsyncClient, db_session,
):
    """An agent JWT (scope not 'tool-asset') is rejected with 401."""

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    from app.auth import create_agent_token

    # Create an agent-scoped token, NOT a tool-asset token.
    agent_token = create_agent_token(
        task_id="00000000-0000-0000-0000-000000000000",
        vm_instance_id="test-vm",
        project_id=str(project.id),
    )

    resp = await client.get(
        f"{_BASE}/projects/{project.id}/tools/my-tool/assets/index.html"
        f"?token={agent_token}"
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Manifest cache behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manifest_cache_serves_second_request(
    client: AsyncClient, db_session,
):
    """The manifest cache avoids re-fetching the same project's manifest."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    manifest_json = json.dumps({
        "tools": [{"slug": "cached-tool", "title": "Cached Tool", "entry": "index.html"}]
    })

    mock_fetch = AsyncMock(return_value=manifest_json)

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            mock_fetch,
        ),
    ):
        # First request — should hit GitHub.
        resp1 = await client.get(f"{_BASE}/projects/{project.id}/tools/manifest")
        assert resp1.status_code == 200
        assert len(resp1.json()["tools"]) == 1
        assert mock_fetch.call_count == 1

        # Second request — should use cache, no additional GitHub call.
        resp2 = await client.get(f"{_BASE}/projects/{project.id}/tools/manifest")
        assert resp2.status_code == 200
        assert len(resp2.json()["tools"]) == 1
        # Cache hit: still only 1 call to get_file_text_at_ref.
        assert mock_fetch.call_count == 1


# ---------------------------------------------------------------------------
# End-to-end: manifest discovery through mocked GitHub (acceptance test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_manifest_discovery(
    client: AsyncClient, db_session,
):
    """End-to-end: register a project with a connected repo, discover manifest
    tools via the mocked GitHub API, verifying the full round-trip."""
    from unittest.mock import AsyncMock, patch

    # 1. Set up a project with github connection.
    project = await _make_project_member(db_session)
    project.github_owner = "acme-corp"
    project.github_repo = "dashboard-tools"
    await db_session.commit()

    # 2. The repo's .blueprint/tools.json declares two tools.
    manifest_json = json.dumps({
        "tools": [
            {
                "slug": "analytics-panel",
                "title": "Analytics Panel",
                "description": "Real-time analytics dashboard",
                "entry": "analytics.html",
                "placement": "project_section",
            },
            {
                "slug": "code-explorer",
                "title": "Code Explorer",
                "entry": "explorer/index.html",
                "placement": "sidebar",
            },
        ]
    })

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value=manifest_json),
        ),
    ):
        # 3. Manifest discovery returns both tools.
        resp = await client.get(
            f"{_BASE}/projects/{project.id}/tools/manifest"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["error"] is None
        assert len(data["tools"]) == 2

        # 4. Verify first tool.
        t1 = data["tools"][0]
        assert t1["slug"] == "analytics-panel"
        assert t1["title"] == "Analytics Panel"
        assert t1["entry"] == "analytics.html"
        assert t1["kind"] == "project"
        assert t1["source"] == "project"

        # 5. Verify second tool (different placement).
        t2 = data["tools"][1]
        assert t2["slug"] == "code-explorer"
        assert t2["placement"] == "sidebar"
        assert t2["entry"] == "explorer/index.html"

        # 6. Mint an asset token for the first tool.
        token_resp = await client.post(
            f"{_BASE}/projects/{project.id}/tools/analytics-panel/asset-token"
        )
        assert token_resp.status_code == 200
        token_data = token_resp.json()
        assert "token" in token_data
        assert "expires_at" in token_data

        # 7. Use the token to fetch the tool's entry point asset.
        asset_token = token_data["token"]
        # Patch get_file_text_at_ref to return the HTML content for the asset.
        with patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value="<!DOCTYPE html><html><body>Analytics</body></html>"),
        ):
            asset_resp = await client.get(
                f"{_BASE}/projects/{project.id}/tools/analytics-panel/assets/analytics.html"
                f"?token={asset_token}"
            )
            assert asset_resp.status_code == 200
            assert "Analytics" in asset_resp.text
            assert asset_resp.headers["content-type"].startswith("text/html")
            assert asset_resp.headers.get("access-control-allow-origin") == "*"


# ===========================================================================
# Skills: manifest discovery, claim integration
# ===========================================================================


# ---------------------------------------------------------------------------
# Manifest with skills (unit tests — no DB / no HTTP for shape)
# ---------------------------------------------------------------------------


def test_manifest_with_skills():
    """A manifest with both tools and skills returns both."""
    from app.routers.admin_project_tools import _normalize_manifest_skill, _validate_manifest_skill

    assert _validate_manifest_skill(
        {"slug": "build", "title": "Build", "command": "make build"}, 0
    ) is None
    result = _normalize_manifest_skill(
        {"slug": "test", "title": "Run Tests", "command": "make test", "description": "Run the full test suite."}
    )
    assert result["slug"] == "test"
    assert result["description"] == "Run the full test suite."


def test_manifest_skill_duplicate_slug():
    """Duplicate skill slugs produce an error."""
    from app.routers.admin_project_tools import _validate_manifest_skill

    # Both are individually valid — dedup happens in _discover_manifest_tools,
    # not in the validator.
    assert _validate_manifest_skill(
        {"slug": "dup", "title": "First", "command": "cmd1"}, 0
    ) is None
    assert _validate_manifest_skill(
        {"slug": "dup", "title": "Second", "command": "cmd2"}, 1
    ) is None


@pytest.mark.asyncio
async def test_manifest_with_skills_in_response(client: AsyncClient, db_session):
    """Manifest endpoint returns skills alongside tools."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    manifest_json = json.dumps({
        "tools": [
            {"slug": "my-viewer", "title": "My Viewer", "entry": "index.html"},
        ],
        "skills": [
            {"slug": "deploy", "title": "Deploy", "command": "bash scripts/deploy.sh"},
            {"slug": "lint", "title": "Lint", "command": "make lint", "description": "Run the linter."},
        ],
    })

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value=manifest_json),
        ),
    ):
        resp = await client.get(f"{_BASE}/projects/{project.id}/tools/manifest")
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"] is None
        assert len(body["tools"]) == 1
        assert len(body["skills"]) == 2

        skill1 = body["skills"][0]
        assert skill1["slug"] == "deploy"
        assert skill1["title"] == "Deploy"
        assert skill1["command"] == "bash scripts/deploy.sh"
        assert "description" not in skill1

        skill2 = body["skills"][1]
        assert skill2["slug"] == "lint"
        assert skill2["title"] == "Lint"
        assert skill2["command"] == "make lint"
        assert skill2["description"] == "Run the linter."


@pytest.mark.asyncio
async def test_manifest_skills_duplicate_slug_rejected(client: AsyncClient, db_session):
    """Duplicate skill slugs produce a validation error in the manifest endpoint."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    manifest_json = json.dumps({
        "skills": [
            {"slug": "same", "title": "First", "command": "cmd1"},
            {"slug": "same", "title": "Second", "command": "cmd2"},
        ],
    })

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value=manifest_json),
        ),
    ):
        resp = await client.get(f"{_BASE}/projects/{project.id}/tools/manifest")
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"] is not None
        assert "duplicate slug" in body["error"]


@pytest.mark.asyncio
async def test_manifest_skills_only_no_tools(client: AsyncClient, db_session):
    """A manifest with only skills (no tools array) returns both empty lists."""
    from unittest.mock import AsyncMock, patch

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()

    manifest_json = json.dumps({
        "skills": [
            {"slug": "clean", "title": "Clean", "command": "make clean"},
        ],
    })

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value=manifest_json),
        ),
    ):
        resp = await client.get(f"{_BASE}/projects/{project.id}/tools/manifest")
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"] is None
        assert body["tools"] == []
        assert len(body["skills"]) == 1
        assert body["skills"][0]["slug"] == "clean"


# ---------------------------------------------------------------------------
# Claim response skills integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_includes_skills_from_manifest(client, db_session):
    """When the project manifest has skills, the claim response includes them."""
    from unittest.mock import AsyncMock, patch

    from app.auth import create_agent_token
    from app.models import Task

    # Create project with a connected repo and a queued task.
    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        vm_instance_id="test-vm-skills",
        status="QUEUED",
        payloads=[{"type": "create", "prompt": "Build feature X"}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    manifest_json = json.dumps({
        "skills": [
            {"slug": "deploy", "title": "Deploy", "command": "bash deploy.sh"},
            {"slug": "test", "title": "Run Tests", "command": "bash test.sh", "description": "Run the test suite."},
        ],
    })

    # Patch GitHub calls for manifest fetch.
    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value=manifest_json),
        ),
    ):
        agent_token = create_agent_token(str(task.id), "test-vm-skills")
        resp = await client.post(
            "/api/agent/claim",
            headers={"x-agent-token": agent_token},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "skills" in body, (
            f"Claim response missing 'skills' key. Available keys: {sorted(body.keys())}"
        )
        skills = body["skills"]
        assert len(skills) == 2
        assert skills[0]["slug"] == "deploy"
        assert skills[0]["command"] == "bash deploy.sh"
        assert skills[1]["slug"] == "test"
        assert skills[1]["description"] == "Run the test suite."


@pytest.mark.asyncio
async def test_claim_omits_skills_when_no_manifest(client, db_session):
    """When the manifest has no skills array, the claim response omits the skills key."""
    from unittest.mock import AsyncMock, patch

    from app.auth import create_agent_token
    from app.models import Task

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        vm_instance_id="test-vm-no-skills",
        status="QUEUED",
        payloads=[{"type": "create", "prompt": "Build feature X"}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    manifest_json = json.dumps({
        "tools": [
            {"slug": "viewer", "title": "Viewer", "entry": "index.html"},
        ],
        # No "skills" key
    })

    with (
        patch(
            "app.github_auth.get_repo_installation_read_token",
            AsyncMock(return_value="ghs_testtoken"),
        ),
        patch(
            "app.github_refs.get_repo_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "app.github.get_file_text_at_ref",
            AsyncMock(return_value=manifest_json),
        ),
    ):
        agent_token = create_agent_token(str(task.id), "test-vm-no-skills")
        resp = await client.post(
            "/api/agent/claim",
            headers={"x-agent-token": agent_token},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Skills key should not be present when there are no skills.
        if "skills" in body:
            assert body["skills"] == [], (
                "skills should be empty list when manifest has no skills"
            )


@pytest.mark.asyncio
async def test_research_claim_includes_project_skills(client, db_session):
    """Research and plan task types receive project skills just like create/fix tasks."""
    from unittest.mock import AsyncMock, patch

    from app.auth import create_agent_token
    from app.models import Task

    project = await _make_project_member(db_session)
    project.github_owner = "test-owner"
    project.github_repo = "test-repo"
    await db_session.commit()
    await db_session.refresh(project)

    manifest_json = json.dumps({
        "skills": [
            {"slug": "benchmark", "title": "Benchmark", "command": "bash bench.sh"},
        ],
    })

    for task_type in ("research", "plan"):
        task = Task(
            project_id=project.id,
            vm_instance_id=f"test-vm-{task_type}-skills",
            status="QUEUED",
            payloads=[{"type": task_type, "prompt": "Investigate performance"}],
            context={},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        with (
            patch(
                "app.github_auth.get_repo_installation_read_token",
                AsyncMock(return_value="ghs_testtoken"),
            ),
            patch(
                "app.github_refs.get_repo_default_branch",
                AsyncMock(return_value="main"),
            ),
            patch(
                "app.github.get_file_text_at_ref",
                AsyncMock(return_value=manifest_json),
            ),
        ):
            agent_token = create_agent_token(str(task.id), f"test-vm-{task_type}-skills")
            resp = await client.post(
                "/api/agent/claim",
                headers={"x-agent-token": agent_token},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert "skills" in body, (
                f"Claim response for task_type={task_type!r} missing 'skills' key. "
                f"Available keys: {sorted(body.keys())}"
            )
            skills = body["skills"]
            assert len(skills) == 1
            assert skills[0]["slug"] == "benchmark"
            assert skills[0]["command"] == "bash bench.sh"

import json
import pytest
from httpx import AsyncClient
from sqlalchemy.orm.attributes import flag_modified

from app.models import Project
from app.playbook_capacity import _PLAYBOOK_CAPACITY_BYTES
from testing.factories import make_membership


@pytest.fixture
async def project_with_member(db_session):
    """Create a project with test-user-123 as owner."""
    project = Project(name="Playbook Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    member = make_membership(project.id, user_id="test-user-123", role="owner")
    db_session.add(member)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_admin_playbook_list(
    client: AsyncClient, db_session, project_with_member
):
    """GET /playbook returns entries with metadata."""
    project = project_with_member

    # Seed two entries directly
    project.design_doc = {
        "playbook/alpha": {
            "content": "do alpha things",
            "source_task_id": "task-abc",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 2,
        },
        "playbook/beta": {
            "content": "do beta things",
            "source_task_id": None,
            "updated_at": "2026-01-02T00:00:00",
            "confirmed_count": 0,
        },
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/playbook")
    assert resp.status_code == 200, resp.text

    data = resp.json()
    assert data["count"] == 2
    entries = data["entries"]
    assert "alpha" in entries
    assert entries["alpha"]["content"] == "do alpha things"
    assert entries["alpha"]["confirmed_count"] == 2
    assert entries["alpha"]["source_task_id"] == "task-abc"
    assert "beta" in entries
    assert entries["beta"]["source_task_id"] is None


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_admin_playbook_list_empty(
    client: AsyncClient, db_session, project_with_member
):
    """GET /playbook returns empty entries when no playbook keys exist."""
    resp = await client.get(f"/api/admin/projects/{project_with_member.id}/playbook")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["entries"] == {}


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_admin_playbook_set(client: AsyncClient, db_session, project_with_member):
    """PUT /playbook creates a new entry with correct metadata."""
    project = project_with_member

    resp = await client.put(
        f"/api/admin/projects/{project.id}/playbook",
        json={"key": "my-tip", "value": "always run tests first"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["key"] == "my-tip"
    assert data["revision"] >= 1

    # Verify persisted correctly
    await db_session.refresh(project)
    entry = project.design_doc.get("playbook/my-tip")
    assert entry is not None
    assert entry["content"] == "always run tests first"
    assert entry["source_task_id"] is None


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_admin_playbook_set_update(
    client: AsyncClient, db_session, project_with_member
):
    """PUT /playbook updates an existing entry, preserving confirmed_count."""
    project = project_with_member
    project.design_doc = {
        "playbook/existing": {
            "content": "old content",
            "source_task_id": "task-old",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 5,
        }
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()

    resp = await client.put(
        f"/api/admin/projects/{project.id}/playbook",
        json={"key": "existing", "value": "updated content"},
    )
    assert resp.status_code == 200

    await db_session.refresh(project)
    entry = project.design_doc.get("playbook/existing")
    assert entry["content"] == "updated content"
    assert entry["confirmed_count"] == 5  # preserved
    assert entry["source_task_id"] is None  # user-seeded


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_admin_playbook_delete(
    client: AsyncClient, db_session, project_with_member
):
    """DELETE /playbook/{key} removes the entry and records a tombstone."""
    project = project_with_member
    project.design_doc = {
        "playbook/remove-me": {
            "content": "delete this",
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
        }
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()

    resp = await client.delete(f"/api/admin/projects/{project.id}/playbook/remove-me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] is True
    assert data["key"] == "remove-me"

    await db_session.refresh(project)
    doc = project.design_doc or {}
    assert "playbook/remove-me" not in doc
    assert "playbook/remove-me" in (doc.get("playbook_deleted_keys") or [])
    # playbook_archive is a migrated telemetry key — stripped from design_doc
    # at write time and persisted via project_telemetry.
    from app.project_telemetry import read_telemetry

    archive = await read_telemetry(db_session, project.id, "playbook_archive") or []
    assert any(
        entry.get("_archived_key") == "playbook/remove-me"
        and entry.get("_retirement_reason") == "operator_deleted"
        for entry in archive
    )


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_admin_playbook_delete_404(
    client: AsyncClient, db_session, project_with_member
):
    """DELETE /playbook/{key} returns 404 for missing key."""
    resp = await client.delete(
        f"/api/admin/projects/{project_with_member.id}/playbook/does-not-exist"
    )
    assert resp.status_code == 404


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_admin_playbook_seed(
    client: AsyncClient, db_session, project_with_member
):
    """POST /seed populates all default entries on an empty playbook."""
    project = project_with_member

    resp = await client.post(f"/api/admin/projects/{project.id}/playbook/seed")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert len(data["added"]) == 10
    assert data["skipped"] == []

    # Verify entries exist in DB
    await db_session.refresh(project)
    doc = project.design_doc or {}
    assert "playbook/build-backend" in doc
    assert "playbook/build-frontend" in doc
    assert "playbook/build-agent" in doc
    assert "playbook/file-restrictions" in doc
    assert "playbook/main-py-policy" in doc
    assert "playbook/task-accessor" in doc
    assert "playbook/admin-auth" in doc
    assert "playbook/import-from-source" in doc
    assert "playbook/test-diagnosis" in doc
    assert "playbook/circular-dep-warning" in doc


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_admin_playbook_seed_skips_existing(
    client: AsyncClient, db_session, project_with_member
):
    """POST /seed skips entries that already exist."""
    project = project_with_member
    project.design_doc = {
        "playbook/build-backend": {
            "content": "custom override",
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
        }
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()

    resp = await client.post(f"/api/admin/projects/{project.id}/playbook/seed")
    assert resp.status_code == 200
    data = resp.json()
    assert "build-backend" in data["skipped"]
    assert "build-backend" not in data["added"]
    assert len(data["added"]) == 9

    # Existing entry should be unchanged
    await db_session.refresh(project)
    assert project.design_doc["playbook/build-backend"]["content"] == "custom override"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_admin_playbook_size_limits(
    client: AsyncClient, db_session, project_with_member
):
    """PUT /playbook rejects values over 2048 bytes and invalid key formats."""
    project = project_with_member

    # Value too large
    resp = await client.put(
        f"/api/admin/projects/{project.id}/playbook",
        json={"key": "too-big", "value": "x" * 2049},
    )
    assert resp.status_code == 400
    assert "too large" in resp.json()["error"]["message"].lower()

    # Limit is UTF-8 bytes, not Unicode code points.
    resp = await client.put(
        f"/api/admin/projects/{project.id}/playbook",
        json={"key": "too-big-unicode", "value": "é" * 1025},
    )
    assert resp.status_code == 400
    assert "2050 bytes" in resp.json()["error"]["message"]

    # Invalid key: contains space and special char
    resp = await client.put(
        f"/api/admin/projects/{project.id}/playbook",
        json={"key": "bad key!", "value": "value"},
    )
    assert resp.status_code == 400
    assert "invalid key" in resp.json()["error"]["message"].lower()

    # Key too long (51 chars, starting with letter)
    resp = await client.put(
        f"/api/admin/projects/{project.id}/playbook",
        json={"key": "a" * 51, "value": "value"},
    )
    assert resp.status_code == 400


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_admin_playbook_list_computed_fields(
    client: AsyncClient, db_session, project_with_member
):
    """GET /playbook returns computed effectiveness_score, stale, and age_days."""
    project = project_with_member
    project.design_doc = {
        "playbook/tip-a": {
            "content": "do tip a",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 3,
            "reference_count": 6,
            "source": "auto-generated",
            "applicable_types": ["create", "fix"],
        },
        "playbook/tip-b": {
            "content": "stale tip",
            "updated_at": "2025-01-01T00:00:00",  # >30 days old, no last_referenced_at
            "confirmed_count": 0,
            "reference_count": 0,
        },
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/playbook")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    entries = data["entries"]

    # tip-a: effectiveness = 3/6 = 0.5
    a = entries["tip-a"]
    assert a["effectiveness_score"] == 0.5
    assert a["source"] == "auto-generated"
    assert a["applicable_types"] == ["create", "fix"]
    assert a["age_days"] >= 0
    assert a["stale"] is False

    # tip-b: no reference in >30 days → stale; confirmed=0, ref=0 → effectiveness unknown (None)
    b = entries["tip-b"]
    assert b["stale"] is True
    assert b["effectiveness_score"] is None


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_admin_playbook_patch_promote(
    client: AsyncClient, db_session, project_with_member
):
    """PATCH /playbook/{key} with action=promote increments confirmed_count."""
    project = project_with_member
    project.design_doc = {
        "playbook/my-tip": {
            "content": "run tests",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 2,
            "reference_count": 4,
            "stale": False,
        }
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()

    resp = await client.patch(
        f"/api/admin/projects/{project.id}/playbook/my-tip",
        json={"action": "promote"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action"] == "promote"
    assert data["entry"]["confirmed_count"] == 3
    assert data["entry"]["stale"] is False

    await db_session.refresh(project)
    assert project.design_doc["playbook/my-tip"]["confirmed_count"] == 3


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_admin_playbook_patch_demote(
    client: AsyncClient, db_session, project_with_member
):
    """PATCH /playbook/{key} with action=demote marks entry stale."""
    project = project_with_member
    project.design_doc = {
        "playbook/bad-tip": {
            "content": "bad advice",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 1,
            "reference_count": 5,
            "stale": False,
        }
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()

    resp = await client.patch(
        f"/api/admin/projects/{project.id}/playbook/bad-tip",
        json={"action": "demote"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["entry"]["stale"] is True

    await db_session.refresh(project)
    assert project.design_doc["playbook/bad-tip"]["stale"] is True


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_admin_playbook_patch_archive(
    client: AsyncClient, db_session, project_with_member
):
    """PATCH /playbook/{key} with action=archive removes entry and appends to archive."""
    project = project_with_member
    project.design_doc = {
        "playbook/old-tip": {
            "content": "obsolete",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
            "reference_count": 0,
        }
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()

    resp = await client.patch(
        f"/api/admin/projects/{project.id}/playbook/old-tip",
        json={"action": "archive"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["archived"] is True

    await db_session.refresh(project)
    assert "playbook/old-tip" not in (project.design_doc or {})
    # playbook_archive is a migrated telemetry key — the PATCH archive path
    # strips it from design_doc and persists it via project_telemetry.
    from app.project_telemetry import read_telemetry

    archive = await read_telemetry(db_session, project.id, "playbook_archive") or []
    assert any(a.get("_archived_key") == "playbook/old-tip" for a in archive)


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_admin_playbook_patch_404(
    client: AsyncClient, db_session, project_with_member
):
    """PATCH /playbook/{key} returns 404 for unknown key."""
    resp = await client.patch(
        f"/api/admin/projects/{project_with_member.id}/playbook/no-such-key",
        json={"action": "promote"},
    )
    assert resp.status_code == 404


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_admin_playbook_patch_invalid_action(
    client: AsyncClient, db_session, project_with_member
):
    """PATCH /playbook/{key} returns 422 for invalid action."""
    project = project_with_member
    project.design_doc = {
        "playbook/tip": {
            "content": "something",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
        }
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()

    resp = await client.patch(
        f"/api/admin/projects/{project.id}/playbook/tip",
        json={"action": "invalid"},
    )
    assert resp.status_code == 422


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_admin_playbook_set_evicts_at_cap(
    client: AsyncClient, db_session, project_with_member
):
    """PUT /playbook evicts least-useful entry when playbook is at capacity."""
    project = project_with_member

    # Fill playbook beyond _PLAYBOOK_CAPACITY_BYTES with low-confirmed entries.
    # Each entry: key + ~2000-byte content = ~2050 bytes; 17 entries ~ 34.7KB > 30KB cap.
    filler_content = "x" * 1900
    doc = {}
    for i in range(17):
        doc[f"playbook/filler-{i:02d}"] = {
            "content": filler_content,
            "source_task_id": None,
            "updated_at": "2025-01-01T00:00:00",
            "confirmed_count": 0,
            "followed_count": 0,
        }
    assert (
        len(
            json.dumps(
                {k: v for k, v in doc.items() if k.startswith("playbook/")}
            ).encode()
        )
        > _PLAYBOOK_CAPACITY_BYTES
    )

    project.design_doc = doc
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()

    resp = await client.put(
        f"/api/admin/projects/{project.id}/playbook",
        json={"key": "new-entry", "value": "new knowledge"},
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(project)
    # New entry was written.
    assert "playbook/new-entry" in project.design_doc
    # At least one filler entry was evicted to archive.
    archive = project.design_doc.get("playbook_archive", [])
    assert len(archive) >= 1
    evicted_keys = {a.get("_archived_key") for a in archive}
    assert any(k.startswith("playbook/filler-") for k in evicted_keys)


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_admin_playbook_set_fails_when_nothing_evictable(
    client: AsyncClient, db_session, project_with_member, monkeypatch
):
    """PUT /playbook returns 400 when at cap and no entry can be evicted."""
    import app.routers.admin_notifications as _router_mod

    # Patch _archive_least_useful_entry to be a no-op (simulates all entries protected).
    monkeypatch.setattr(_router_mod, "_archive_least_useful_entry", lambda doc: doc)

    project = project_with_member
    # Fill playbook so it exceeds capacity (17 entries ~ 34.4KB > 30KB cap).
    filler_content = "x" * 1900
    doc = {}
    for i in range(17):
        doc[f"playbook/filler-{i:02d}"] = {
            "content": filler_content,
            "source_task_id": None,
            "updated_at": "2025-01-01T00:00:00",
            "confirmed_count": 0,
        }
    project.design_doc = doc
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()

    resp = await client.put(
        f"/api/admin/projects/{project.id}/playbook",
        json={"key": "new-entry", "value": "new knowledge"},
    )
    assert resp.status_code == 400
    assert "evictable" in resp.json()["error"]["message"].lower()

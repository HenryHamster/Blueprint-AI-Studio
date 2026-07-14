"""Integration tests for the operator migration-exemptions admin router.

These lock the sanctioned WRITE path for the DB-backed migration-divergence
exemption store (``backend/app/routers/admin_migration_exemptions.py``):

* GET returns the effective list — baked entries tagged ``source="baked"`` plus
  runtime entries tagged ``source="runtime"`` with audit metadata.
* POST captures the acting operator + reason server-side and makes the exemption
  appear in the effective GET list.
* POST without a reason is rejected (400).
* POST ignores a client-supplied ``added_by`` / ``added_at`` (server stamps the
  real operator identity from auth).
* Attempting to DELETE a baked-in entry is rejected (the baked list is the
  immutable floor).
* A non-admin caller is forbidden (403).
* END-TO-END SEAM: an exemption added over the HTTP write path is honored by the
  migration guard's ``effective_authorized_divergences()`` /
  ``_divergence_record_is_authorized`` path WITHOUT a redeploy.
"""

import contextlib

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

import app.runtime_config_state as _rcs
from app.auth import get_current_user, require_admin
from app.main import app
from app.migration_chain import find_content_divergence_records
from app.migration_reconciliation import (
    AUTHORIZED_DIVERGENCES,
    RUNTIME_EXEMPTIONS_CONFIG_KEY,
    effective_authorized_divergences,
)
from app.rollup_migration_validation import _divergence_record_is_authorized

BASE = "/api/admin/migration-exemptions"


@pytest.fixture(autouse=True)
def _clean_runtime_store():
    """Snapshot and restore the in-memory runtime exemption store around each test
    so POST/DELETE mutations of the shared global config never leak between tests."""
    prev = _rcs.runtime_config.get(RUNTIME_EXEMPTIONS_CONFIG_KEY)
    _rcs.runtime_config[RUNTIME_EXEMPTIONS_CONFIG_KEY] = []
    try:
        yield
    finally:
        if prev is None:
            _rcs.runtime_config.pop(RUNTIME_EXEMPTIONS_CONFIG_KEY, None)
        else:
            _rcs.runtime_config[RUNTIME_EXEMPTIONS_CONFIG_KEY] = prev


def _valid_body(**overrides) -> dict:
    """A well-formed POST body (all match fields + a reason). Defaults to the
    reused 0138 digest_cache_table -> task_blocked_state_fields divergence used
    across the migration-divergence tests."""
    body = {
        "revision": "0138",
        "parent_revision": "0137",
        "deployed_identity": "digest_cache_table",
        "integration_identity": "task_blocked_state_fields",
        "created_table": "",
        "integration_revision": "",
        "reason": "authorized reconciled 0138 back-merge renumber (test)",
    }
    body.update(overrides)
    return body


@contextlib.contextmanager
def _as_non_admin():
    """Override auth so require_admin actually rejects (simulating a non-admin)."""

    async def _non_admin():
        return {"user_id": "other", "email": "other@example.com", "role": "user"}

    async def _reject_admin():
        raise HTTPException(status_code=403, detail="Admin privileges required")

    app.dependency_overrides[get_current_user] = _non_admin
    app.dependency_overrides[require_admin] = _reject_admin
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(require_admin, None)


# --- GET: effective list, baked + runtime ----------------------------------


@pytest.mark.asyncio
async def test_get_lists_baked_entries_tagged_source_baked(client: AsyncClient):
    resp = await client.get(BASE)
    assert resp.status_code == 200
    rows = resp.json()["exemptions"]
    baked = [r for r in rows if r["source"] == "baked"]
    # Every baked AUTHORIZED_DIVERGENCE is present and carries no audit metadata.
    assert len(baked) == len(AUTHORIZED_DIVERGENCES)
    baked_revs = {r["revision"] for r in baked}
    assert baked_revs == {d.revision for d in AUTHORIZED_DIVERGENCES}
    assert all(r["added_by"] == "" and r["reason"] == "" for r in baked)


# --- POST: add captures operator + reason, appears in GET -------------------


@pytest.mark.asyncio
async def test_post_add_captures_operator_and_reason_and_appears_in_get(
    client: AsyncClient,
):
    resp = await client.post(BASE, json=_valid_body())
    assert resp.status_code == 201
    added = resp.json()["exemption"]
    assert added["source"] == "runtime"
    # Operator identity is the authenticated admin (conftest override), stamped
    # server-side, and added_at is populated by the server.
    assert added["added_by"] == "test@example.com"
    assert added["added_at"]
    assert added["reason"].startswith("authorized reconciled 0138")

    # It now shows up in the effective GET list as a runtime entry with metadata.
    listed = (await client.get(BASE)).json()["exemptions"]
    runtime_rows = [r for r in listed if r["source"] == "runtime"]
    assert len(runtime_rows) == 1
    assert runtime_rows[0]["revision"] == "0138"
    assert runtime_rows[0]["added_by"] == "test@example.com"


@pytest.mark.asyncio
async def test_post_without_reason_is_rejected(client: AsyncClient):
    resp = await client.post(BASE, json=_valid_body(reason=""))
    assert resp.status_code == 400
    assert "reason" in resp.text.lower()
    # Nothing was stored.
    assert _rcs.runtime_config.get(RUNTIME_EXEMPTIONS_CONFIG_KEY) == []


@pytest.mark.asyncio
async def test_post_missing_match_field_is_rejected(client: AsyncClient):
    resp = await client.post(BASE, json=_valid_body(deployed_identity=""))
    assert resp.status_code == 400
    assert _rcs.runtime_config.get(RUNTIME_EXEMPTIONS_CONFIG_KEY) == []


@pytest.mark.asyncio
async def test_post_bad_revision_shape_is_rejected(client: AsyncClient):
    resp = await client.post(BASE, json=_valid_body(revision="bad revision/../"))
    assert resp.status_code == 400
    assert "revision" in resp.text.lower()


@pytest.mark.asyncio
async def test_post_ignores_client_supplied_added_by_and_added_at(
    client: AsyncClient,
):
    resp = await client.post(
        BASE,
        json=_valid_body(
            added_by="attacker@evil.example",
            added_at="1999-01-01T00:00:00Z",
        ),
    )
    assert resp.status_code == 201
    added = resp.json()["exemption"]
    # The server never trusts the client actor/timestamp.
    assert added["added_by"] == "test@example.com"
    assert added["added_by"] != "attacker@evil.example"
    assert not added["added_at"].startswith("1999")


@pytest.mark.asyncio
async def test_post_duplicate_of_baked_entry_is_conflict(client: AsyncClient):
    baked = AUTHORIZED_DIVERGENCES[0]
    resp = await client.post(
        BASE,
        json=_valid_body(
            revision=baked.revision,
            parent_revision=baked.parent_revision,
            deployed_identity=baked.deployed_identity,
            integration_identity=baked.integration_identity,
            created_table=baked.created_table,
            integration_revision=baked.integration_revision,
        ),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_post_duplicate_runtime_entry_is_conflict(client: AsyncClient):
    assert (await client.post(BASE, json=_valid_body())).status_code == 201
    dup = await client.post(BASE, json=_valid_body(reason="second attempt"))
    assert dup.status_code == 409
    # Store still has exactly one entry.
    assert len(_rcs.runtime_config.get(RUNTIME_EXEMPTIONS_CONFIG_KEY)) == 1


# --- DELETE -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_runtime_exemption(client: AsyncClient):
    assert (await client.post(BASE, json=_valid_body())).status_code == 201
    resp = await client.delete(
        BASE,
        params={
            "revision": "0138",
            "deployed_identity": "digest_cache_table",
            "integration_identity": "task_blocked_state_fields",
        },
    )
    assert resp.status_code == 200
    assert _rcs.runtime_config.get(RUNTIME_EXEMPTIONS_CONFIG_KEY) == []


@pytest.mark.asyncio
async def test_delete_baked_entry_is_rejected(client: AsyncClient):
    baked = AUTHORIZED_DIVERGENCES[0]
    resp = await client.delete(
        BASE,
        params={
            "revision": baked.revision,
            "deployed_identity": baked.deployed_identity,
            "integration_identity": baked.integration_identity,
        },
    )
    assert resp.status_code == 400
    assert "baked" in resp.text.lower() or "floor" in resp.text.lower()


@pytest.mark.asyncio
async def test_delete_unknown_runtime_entry_is_404(client: AsyncClient):
    resp = await client.delete(
        BASE,
        params={
            "revision": "9999",
            "deployed_identity": "nope",
            "integration_identity": "nada",
        },
    )
    assert resp.status_code == 404


# --- Auth -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_admin_is_forbidden(override_get_session, db_session):
    with _as_non_admin():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            assert (await ac.get(BASE)).status_code == 403
            assert (await ac.post(BASE, json=_valid_body())).status_code == 403
            assert (
                await ac.delete(
                    BASE,
                    params={
                        "revision": "0138",
                        "deployed_identity": "digest_cache_table",
                        "integration_identity": "task_blocked_state_fields",
                    },
                )
            ).status_code == 403


# --- END-TO-END SEAM: added exemption is honored by the migration guard ------


@pytest.mark.asyncio
async def test_added_exemption_is_honored_by_migration_guard(client: AsyncClient):
    """An exemption added over the HTTP write path flips an otherwise-unauthorized
    divergence from block -> authorized at the gate, WITHOUT a redeploy — the whole
    point of the runtime store."""
    record = find_content_divergence_records(
        ["0138_digest_cache_table.py"], ["0138_task_blocked_state_fields.py"]
    )[0]
    assert record["kind"] == "reused"

    # Before the operator writes the exemption, the divergence is NOT authorized.
    assert _divergence_record_is_authorized(record) is False

    resp = await client.post(BASE, json=_valid_body())
    assert resp.status_code == 201

    # After the write, effective_authorized_divergences() (read at gate time)
    # includes the runtime entry, and the guard authorizes the exact-match record.
    eff = effective_authorized_divergences()
    assert any(d.is_runtime and d.revision == "0138" for d in eff)
    assert _divergence_record_is_authorized(record) is True

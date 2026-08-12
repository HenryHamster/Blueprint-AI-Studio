"""
Integration tests for the directions system.

Tests cover:
- CRUD: create, update (priority/status/text/paths), delete (cancels QUEUED tasks)
- Cooldown filter: directions within/past the cooldown window in _spawn_research_task
- last_researched_at update: after _handle_research_completion runs
- task-counts endpoint: counts research tasks per direction
- Status/priority validation: invalid values return 400
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.time_utils import _utcnow
from app.models import (
    Alert,
    AlertSeverity,
    Direction,
    MessagesNoisePrefs,
    Project,
    ProjectMessage,
    Task,
    TaskStatus,
)
from app.autonomous_research import _spawn_research_task
from app.direction_lifecycle import _process_direction_lifecycle
from app.research_completion import (
    _handle_research_completion as _handle_research_completion_impl,
    _calibrate_scope,
    predict_spawn_success,
    _poor_decomp_history,
)
from app.direction_metrics import (
    _compute_acceptance_rate,
    _compute_direction_quality_score,
    _compute_momentum_factor,
    _compute_productivity_score,
    _compute_rolling_median_review_iterations,
)
from app.direction_refractory import (
    get_direction_refractory_info,
    stamp_direction_refractory,
)
from app.max_plan_pool import _max_plan_pools, _account_by_id


async def _handle_research_completion(task: Task, logs: str, db_session, **kwargs):
    """Invoke research completion after the lifecycle's terminal transition."""

    if task.status == TaskStatus.IN_PROGRESS:
        task.status = TaskStatus.COMPLETED
        db_session.add(task)
        await db_session.commit()
    return await _handle_research_completion_impl(task, logs, db_session, **kwargs)


@pytest.fixture(autouse=True)
def _ensure_test_account_pool():
    """Ensure _spawn_research_task finds an available account for the test user."""
    uid = "test-user-123"
    entry = {
        "id": "test-acct",
        "user_id": uid,
        "name": "test",
        "enabled": True,
        "active_tasks": 0,
        "oauth_token": "sk-ant-test",
        "total_tasks": 0,
        "token_created_at": None,
        "claude_config_json": None,
    }
    _max_plan_pools.setdefault(uid, []).append(entry)
    _account_by_id["test-acct"] = entry
    yield
    _max_plan_pools.get(uid, [])[:] = [
        a for a in _max_plan_pools.get(uid, []) if a["id"] != "test-acct"
    ]
    _account_by_id.pop("test-acct", None)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _deploy_project(
    client: AsyncClient, mocker, name: str = "Test Project"
) -> str:
    """Create a project via the deploy API and return its project_id string."""
    mocker.patch("app.main.provision_agent_vm", return_value=None)
    resp = await client.post(
        "/api/deploy",
        json={"name": name, "payloads": [{"type": "smoketest"}], "storage_uris": []},
    )
    assert resp.status_code == 200
    return resp.json()["project_id"]


async def _add_direction(
    client: AsyncClient,
    project_id: str,
    *,
    text: str = "Improve test coverage",
    priority: str = "medium",
    paths: list | None = None,
) -> dict:
    """POST a direction to a project and return the created direction dict."""
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": text, "priority": priority, "paths": paths or []},
    )
    assert resp.status_code == 200
    return resp.json()["direction"]


async def _load_project_and_direction(db_session, project_id: str, direction_id: str):
    project = (
        await db_session.execute(
            select(Project).where(Project.id == uuid.UUID(project_id))
        )
    ).scalar_one()
    direction_row = (
        await db_session.execute(
            select(Direction).where(
                Direction.project_id == uuid.UUID(project_id),
                Direction.direction_key == direction_id,
            )
        )
    ).scalar_one_or_none()
    return project, direction_row


# ─────────────────────────────────────────────────────────────────────────────
# 1. CRUD
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_directions_empty(client: AsyncClient, mocker):
    """GET on a fresh project returns an empty directions list."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["directions"] == []
    assert "autonomous_enabled" in data


@pytest.mark.asyncio
async def test_get_directions_includes_proof_enriched_problem_model(
    client: AsyncClient, db_session, mocker
):
    """Directions payload carries proof annotations for the operator view."""
    from sqlalchemy.orm.attributes import flag_modified

    project_id = await _deploy_project(client, mocker)
    project = (
        await db_session.execute(
            select(Project).where(Project.id == uuid.UUID(project_id))
        )
    ).scalar_one()
    project.problem_model = {
        "revision": 11,
        "capabilities": [
            {"name": "language-aware repo map", "status": "working"},
            {"name": "claimed only capability", "status": "planned"},
        ],
    }
    flag_modified(project, "problem_model")
    project.capability_proof_records = {
        "language-aware repo map": [
            {
                "task_id": "task-proof-2",
                "proved_at": "2026-06-09T01:02:03Z",
                "proof_kind": "downstream_artifact",
                "proof_ref": "knowledge-document-123",
                "commit_sha": "fedcba987654",
            }
        ]
    }
    flag_modified(project, "capability_proof_records")
    db_session.add(project)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    data = resp.json()
    caps = data["problem_model"]["capabilities"]
    assert caps[0]["proof_status"] == "proven"
    assert caps[0]["proof_records"][0]["proof_kind"] == "downstream_artifact"
    assert caps[0]["proof_records"][0]["proof_ref"] == "knowledge-document-123"
    assert caps[0]["proof_records"][0]["commit_sha"] == "fedcba987654"
    assert caps[0]["proof_records"][0]["proved_at"] == "2026-06-09T01:02:03Z"
    assert caps[1]["proof_status"] == "unproven"
    assert "proof_records" not in caps[1]


@pytest.mark.asyncio
async def test_create_direction(client: AsyncClient, mocker):
    """POST creates a direction with an auto-generated id and default active status."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "Improve test coverage", "priority": "high", "paths": ["src/"]},
    )
    assert resp.status_code == 200
    d = resp.json()["direction"]
    assert d["text"] == "Improve test coverage"
    assert d["priority"] == "high"
    assert d["paths"] == ["src/"]
    assert d["status"] == "active"
    assert d["id"].startswith("d-")
    assert "added_at" in d


@pytest.mark.asyncio
async def test_create_direction_is_idempotent_with_idempotency_key(
    client: AsyncClient, mocker
):
    """POST with idempotency_key returns existing direction on duplicate submit."""
    project_id = await _deploy_project(client, mocker)
    idem_key = "test-key-12345"
    # First submit
    resp1 = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={
            "text": "Improve test coverage",
            "priority": "high",
            "paths": ["src/"],
            "idempotency_key": idem_key,
        },
    )
    assert resp1.status_code == 200
    d1 = resp1.json()["direction"]
    direction_id_1 = d1["id"]
    assert d1["idempotency_key"] == idem_key

    # Duplicate submit with same key should return the existing direction
    resp2 = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={
            "text": "Improve test coverage",
            "priority": "high",
            "paths": ["src/"],
            "idempotency_key": idem_key,
        },
    )
    assert resp2.status_code == 200
    d2 = resp2.json()["direction"]
    # Should be the same direction (same id)
    assert d2["id"] == direction_id_1
    assert d2["idempotency_key"] == idem_key
    # Response should indicate it's a duplicate
    assert resp2.json().get("status") == "duplicate"

    # Verify only one direction was created (not two)
    resp_list = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp_list.status_code == 200
    directions = resp_list.json()["directions"]
    assert len(directions) == 1
    assert directions[0]["id"] == direction_id_1


@pytest.mark.asyncio
async def test_create_staged_direction_duplicate_replay_preserves_staging(
    client: AsyncClient, db_session, mocker
):
    """A duplicate replay of a born-staged create returns the same staged
    direction verbatim (preserving staged_for_version and completion_criteria)
    and does not create a second lane."""
    project_id = await _deploy_project(client, mocker)
    project_uuid = uuid.UUID(project_id)
    from app import release_version as rv

    await rv.open_version(db_session, project_uuid, version_number=7)
    await db_session.commit()

    idem_key = "staged-replay-key"
    body = {
        "text": "Stage this for next version",
        "priority": "medium",
        "staged": True,
        "completion_criteria": "All staged checks pass",
        "idempotency_key": idem_key,
    }
    resp1 = await client.post(f"/api/admin/projects/{project_id}/directions", json=body)
    assert resp1.status_code == 200
    d1 = resp1.json()["direction"]
    assert d1["status"] == "staged"
    assert d1["staged_for_version"] == 7
    assert d1["completion_criteria"] == "All staged checks pass"

    # Replay with the same key returns the stored staged direction verbatim.
    resp2 = await client.post(f"/api/admin/projects/{project_id}/directions", json=body)
    assert resp2.status_code == 200
    assert resp2.json().get("status") == "duplicate"
    d2 = resp2.json()["direction"]
    assert d2["id"] == d1["id"]
    assert d2["status"] == "staged"
    assert d2["staged_for_version"] == 7
    assert d2["completion_criteria"] == "All staged checks pass"

    # Exactly one direction exists after the replay.
    resp_list = await client.get(f"/api/admin/projects/{project_id}/directions")
    directions = resp_list.json()["directions"]
    assert len(directions) == 1


@pytest.mark.asyncio
async def test_create_direction_duplicate_replay_does_not_double_count_cap(
    client: AsyncClient, mocker
):
    """A duplicate replay carrying an existing key does not consume an extra
    active-direction slot — the cap counts the single underlying direction once."""
    project_id = await _deploy_project(client, mocker)
    from app.shared_state import runtime_config

    mocker.patch.dict(runtime_config, {"max_active_directions": 1})

    idem_key = "cap-replay-key"
    body = {"text": "Only lane", "priority": "high", "idempotency_key": idem_key}
    resp1 = await client.post(f"/api/admin/projects/{project_id}/directions", json=body)
    assert resp1.status_code == 200

    # Replaying the same key returns the duplicate, not a 409 cap error.
    resp2 = await client.post(f"/api/admin/projects/{project_id}/directions", json=body)
    assert resp2.status_code == 200
    assert resp2.json().get("status") == "duplicate"

    resp_list = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert len(resp_list.json()["directions"]) == 1


@pytest.mark.asyncio
async def test_get_directions_returns_created(client: AsyncClient, mocker):
    """GET returns directions that were previously POSTed."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(
        client, project_id, text="Direction A", priority="low"
    )

    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    directions = resp.json()["directions"]
    assert len(directions) == 1
    assert directions[0]["id"] == direction["id"]


@pytest.mark.asyncio
async def test_get_directions_includes_knowledge_document_count(
    client: AsyncClient, db_session, mocker
):
    """GET /directions enriches each direction with its accumulated
    knowledge-document count, keyed by the string direction id."""
    from app.models import KnowledgeDocument

    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Has knowledge")

    task = Task(project_id=uuid.UUID(project_id), status=TaskStatus.COMPLETED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    for i in range(2):
        db_session.add(
            KnowledgeDocument(
                project_id=uuid.UUID(project_id),
                source_task_id=task.id,
                direction_id=direction["id"],
                title=f"Doc {i}",
                summary="S",
                body="B",
            )
        )
    # A document with no direction must not be counted against this direction.
    db_session.add(
        KnowledgeDocument(
            project_id=uuid.UUID(project_id),
            source_task_id=task.id,
            direction_id=None,
            title="Unlinked",
            summary="S",
            body="B",
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    d = next(x for x in resp.json()["directions"] if x["id"] == direction["id"])
    assert d["knowledge_document_count"] == 2


@pytest.mark.asyncio
async def test_get_directions_includes_hold_timing_fields(client: AsyncClient, mocker):
    """GET includes held/held_reason plus held_at/held_until for held directions."""
    import app.shared_state as _ss

    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(
        client, project_id, text="Direction A", priority="low"
    )
    did = direction["id"]

    _ss.direction_failure_streaks.clear()
    for _ in range(_ss.DIRECTION_HOLD_THRESHOLD):
        _ss.record_direction_result(did, success=False, failure_class="CODE_ERROR")

    try:
        resp = await client.get(f"/api/admin/projects/{project_id}/directions")
        assert resp.status_code == 200
        payload = resp.json()["directions"][0]
        assert payload["held"] is True
        assert payload["held_reason"]
        assert isinstance(payload["held_at"], str)
        assert isinstance(payload["held_until"], str)
    finally:
        _ss.direction_failure_streaks.clear()


@pytest.mark.asyncio
async def test_get_directions_includes_null_hold_timing_when_not_held(
    client: AsyncClient, mocker
):
    """GET includes held_at/held_until as null when direction is not held."""
    project_id = await _deploy_project(client, mocker)
    await _add_direction(client, project_id, text="Direction A", priority="low")

    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    payload = resp.json()["directions"][0]
    assert payload["held"] is False
    assert payload["held_reason"] is None
    assert payload["held_at"] is None
    assert payload["held_until"] is None
    assert payload["refractory_until"] is None
    assert payload["refractory_reason"] is None
    assert payload["refractory_remaining_minutes"] is None


@pytest.mark.asyncio
async def test_get_directions_includes_refractory_payload(client: AsyncClient, mocker):
    """GET surfaces remaining refractory time and whether it is a standard or saturation hold."""
    project_id = await _deploy_project(client, mocker)
    future_ts = (datetime.now(timezone.utc) + timedelta(minutes=90)).isoformat()
    await client.put(
        f"/api/admin/projects/{project_id}/directions",
        json=[
            {
                "id": "d-refract",
                "text": "Direction A",
                "priority": "low",
                "status": "active",
                "added_at": "2026-01-01T00:00:00",
                "last_researched_at": datetime.now(timezone.utc).isoformat(),
                "refractory_until": future_ts,
                "possibly_saturated": True,
                "research_history": [],
            }
        ],
    )

    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    payload = resp.json()["directions"][0]
    assert payload["refractory_reason"] == "saturation"
    assert payload["refractory_remaining_minutes"] >= 89
    assert payload["refractory_until"] is not None


@pytest.mark.asyncio
async def test_get_directions_running_update_reconciliation_null_by_default(
    client: AsyncClient, mocker
):
    """GET exposes running_update_reconciliation, None when no operator edit has
    superseded an agent's running_update write."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    data = resp.json()
    assert "running_update_reconciliation" in data
    assert data["running_update_reconciliation"] is None


@pytest.mark.asyncio
async def test_get_directions_running_update_reconciliation_summarizes_ledger(
    client: AsyncClient, db_session, mocker
):
    """When the rejected-edits ledger holds running_update_superseded entries,
    GET summarizes them as operator_wins_count + the latest timestamp."""
    from app.running_update_merge import RUNNING_UPDATE_REJECTED_KIND
    from app.vision_merge import REJECTED_EDITS_KEY

    project_id = await _deploy_project(client, mocker)
    project = await db_session.get(Project, uuid.UUID(project_id))
    project.vision = {
        REJECTED_EDITS_KEY: [
            {
                "at": "2026-06-01T10:00:00",
                "field": "running_update",
                "kind": RUNNING_UPDATE_REJECTED_KIND,
                "agent_value": "old superseded doc",
            },
            {
                "at": "2026-06-03T12:30:00",
                "field": "running_update",
                "kind": RUNNING_UPDATE_REJECTED_KIND,
                "agent_value": "newer superseded doc",
            },
            # A non-running-update ledger entry must NOT be counted.
            {"at": "2026-06-04T00:00:00", "field": "purpose", "kind": "vision_field"},
        ]
    }
    db_session.add(project)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    recon = resp.json()["running_update_reconciliation"]
    assert recon == {
        "operator_wins_count": 2,
        "last_operator_win_at": "2026-06-03T12:30:00",
    }


@pytest.mark.asyncio
async def test_unhold_direction_clears_hold(client: AsyncClient, mocker):
    """POST /unhold clears an existing direction hold and reports was_held=True."""
    import app.shared_state as _ss

    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)
    did = direction["id"]

    # Drive the direction into a held state
    _ss.direction_failure_streaks.clear()
    for _ in range(_ss.DIRECTION_HOLD_THRESHOLD):
        _ss.record_direction_result(did, success=False, failure_class="CODE_ERROR")
    assert _ss.is_direction_held(did)[0]

    try:
        resp = await client.post(
            f"/api/admin/projects/{project_id}/directions/{did}/unhold",
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "unheld"
        assert body["direction_id"] == did
        assert body["was_held"] is True
        assert not _ss.is_direction_held(did)[0]
    finally:
        _ss.direction_failure_streaks.clear()


@pytest.mark.asyncio
async def test_unhold_direction_idempotent_when_not_held(client: AsyncClient, mocker):
    """POST /unhold on a healthy direction returns 200 with was_held=False."""
    import app.shared_state as _ss

    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    _ss.direction_failure_streaks.clear()
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}/unhold",
    )
    assert resp.status_code == 200
    assert resp.json()["was_held"] is False


@pytest.mark.asyncio
async def test_unhold_direction_not_found_returns_404(client: AsyncClient, mocker):
    """POST /unhold on an unknown direction id returns 404."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/d-does-not-exist/unhold",
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_direction_with_cost_budget(client: AsyncClient, mocker):
    """POST accepts and stores cost_budget_usd."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={
            "text": "budgeted",
            "priority": "low",
            "paths": ["src/"],
            "cost_budget_usd": 12.5,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["direction"]["cost_budget_usd"] == 12.5


@pytest.mark.asyncio
async def test_create_direction_rejects_negative_cost_budget(
    client: AsyncClient, mocker
):
    """POST rejects negative cost_budget_usd via pydantic ge=0 validator."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "x", "priority": "low", "paths": ["src/"], "cost_budget_usd": -1},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_direction_cost_budget_set_and_clear(client: AsyncClient, mocker):
    """PATCH sets cost_budget_usd and clears it when set to null."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"cost_budget_usd": 5},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["direction"]["cost_budget_usd"] == 5.0

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"cost_budget_usd": None},
    )
    assert resp.status_code == 200
    assert "cost_budget_usd" not in resp.json()["direction"]


@pytest.mark.asyncio
async def test_update_direction_rejects_invalid_cost_budget(
    client: AsyncClient, mocker
):
    """PATCH rejects non-numeric and negative cost_budget_usd."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"cost_budget_usd": "lots"},
    )
    assert resp.status_code == 400

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"cost_budget_usd": -3},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_direction_rejects_invalid_recommended_mode(
    client: AsyncClient, mocker
):
    """PATCH rejects a recommended_mode outside the documented set."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"recommended_mode": "turbo"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_direction_normalizes_recommended_mode(
    client: AsyncClient, mocker
):
    """PATCH normalizes mixed-case/whitespace recommended_mode before storing."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"recommended_mode": "  Ship "},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["direction"]["recommended_mode"] == "ship"


@pytest.mark.asyncio
async def test_update_direction_recommended_mode_set_and_clear(
    client: AsyncClient, mocker
):
    """PATCH sets recommended_mode and clears it (with meta) when set to null."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"recommended_mode": "audit"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["direction"]["recommended_mode"] == "audit"

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"recommended_mode": None},
    )
    assert resp.status_code == 200
    body = resp.json()["direction"]
    assert "recommended_mode" not in body
    assert "recommended_mode_meta" not in body


@pytest.mark.asyncio
async def test_update_direction_rejects_invalid_system_id(client: AsyncClient, mocker):
    """PATCH rejects non-string and over-length system_id."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"system_id": 123},
    )
    assert resp.status_code == 400

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"system_id": "x" * 101},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_direction_system_id_set_and_clear(client: AsyncClient, mocker):
    """PATCH sets system_id and clears it when set to null."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"system_id": "sys-alpha"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["direction"]["system_id"] == "sys-alpha"

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"system_id": None},
    )
    assert resp.status_code == 200
    assert "system_id" not in resp.json()["direction"]


@pytest.mark.asyncio
async def test_update_direction_priority(client: AsyncClient, mocker):
    """PATCH updates direction priority."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, priority="low")

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"priority": "high"},
    )
    assert resp.status_code == 200
    assert resp.json()["direction"]["priority"] == "high"


@pytest.mark.asyncio
async def test_operator_update_direction_reconciles_gap_staffing_on_status_and_gap_changes(
    client: AsyncClient, db_session, mocker
):
    """PATCH re-ground/status changes keep problem-model gap staffing in sync."""
    from sqlalchemy.orm.attributes import flag_modified

    project_id = await _deploy_project(client, mocker)
    direction_id = "d-staffed"
    project = (
        await db_session.execute(
            select(Project).where(Project.id == uuid.UUID(project_id))
        )
    ).scalar_one()
    project.problem_model = {
        "revision": 1,
        "gaps": [
            {
                "id": "g-old",
                "name": "old gap",
                "status": "active",
                "active_moves": [direction_id],
                "past_moves": [],
            },
            {
                "id": "g-new",
                "name": "new gap",
                "status": "active",
                "active_moves": [],
                "past_moves": [],
                "deferral": {
                    "rationale": "parked before staffing",
                    "created_at": "2026-01-01T00:00:00",
                },
            },
        ],
    }
    flag_modified(project, "problem_model")
    project.directions = [
        {
            "id": direction_id,
            "text": "Move from old gap to new gap",
            "status": "active",
            "priority": "medium",
            "paths": [],
            "gap_id": "g-old",
            "added_at": "2026-01-01T00:00:00",
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    pause_resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={"gap_id": "g-new", "status": "paused"},
    )
    assert pause_resp.status_code == 200, pause_resp.text
    assert pause_resp.json()["direction"]["gap_id"] == "g-new"
    assert pause_resp.json()["direction"]["status"] == "paused"

    await db_session.refresh(project)
    gaps = {g["id"]: g for g in project.problem_model["gaps"]}
    assert gaps["g-old"]["active_moves"] == []
    assert gaps["g-old"]["past_moves"] == [direction_id]
    assert gaps["g-old"]["deferral"]["created_at"]
    assert direction_id in gaps["g-old"]["deferral"]["rationale"]
    assert gaps["g-new"]["active_moves"] == []
    assert gaps["g-new"]["deferral"]["rationale"] == "parked before staffing"

    active_resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={"status": "active"},
    )
    assert active_resp.status_code == 200, active_resp.text
    await db_session.refresh(project)
    gaps = {g["id"]: g for g in project.problem_model["gaps"]}
    assert gaps["g-new"]["active_moves"] == [direction_id]
    assert gaps["g-new"]["deferral"] is None


@pytest.mark.asyncio
async def test_update_direction_status_stage_sets_version_marker(
    client: AsyncClient, db_session, mocker
):
    """PATCH status='staged' stamps the current open version onto the direction."""
    project_id = await _deploy_project(client, mocker)
    project_uuid = uuid.UUID(project_id)
    from app import release_version as rv
    from app.task_locking import with_directions_locked

    await rv.open_version(db_session, project_uuid, version_number=7)
    await db_session.commit()

    direction = await _add_direction(client, project_id)
    direction_id = direction["id"]

    def _seed_residue(dirs):
        dirs[0].update(
            {
                "paused_reason": "cooldown residue",
                "paused_at": "2026-06-01T00:00:00",
                "auto_paused": True,
                "expired_at": "2026-06-01T01:00:00",
                "needs_manual_intervention": True,
                "needs_manual_intervention_at": "2026-06-01T02:00:00",
                "escalation_notification_pending": True,
                "escalation_notification_retries": 2,
                "last_researched_at": "2026-06-01T03:00:00",
                "staged_activated_at": "2026-06-01T04:00:00",
                "remediation_attempts": 4,
            }
        )

    await with_directions_locked(db_session, project_uuid, _seed_residue)
    await db_session.commit()

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"status": "staged"},
    )
    assert resp.status_code == 200
    body = resp.json()["direction"]
    assert body["status"] == "staged"
    assert body["staged_for_version"] == 7
    assert "staged_activated_at" not in body
    for field in (
        "paused_reason",
        "needs_manual_intervention",
        "escalation_notification_pending",
        "last_researched_at",
    ):
        assert field not in body
    assert body["remediation_attempts"] == 0

    project, row = await _load_project_and_direction(
        db_session, project_id, direction_id
    )
    blob = next(d for d in project.directions or [] if d.get("id") == direction_id)
    assert blob["status"] == "staged"
    assert blob["staged_for_version"] == 7
    assert "staged_activated_at" not in blob
    assert row is not None
    assert row.status == "staged"
    assert row.data["staged_for_version"] == 7


async def _seed_churn_recommended_direction(
    db_session,
    project_id: str,
    direction_id: str,
    *,
    gap_id: str = "g-churn",
    churn_count: int = 2,
) -> None:
    """Seed a direction held PAUSED pending a keep/retire decision, staffing a gap."""
    from sqlalchemy.orm.attributes import flag_modified

    project = (
        await db_session.execute(
            select(Project).where(Project.id == uuid.UUID(project_id))
        )
    ).scalar_one()
    project.problem_model = {
        "revision": 1,
        "gaps": [
            {
                "id": gap_id,
                "name": "churn gap",
                "status": "active",
                "active_moves": [direction_id],
                "past_moves": [],
            }
        ],
    }
    flag_modified(project, "problem_model")
    project.directions = [
        {
            "id": direction_id,
            "text": "Zombie direction pending keep/retire decision",
            "status": "paused",
            "priority": "medium",
            "paths": [],
            "gap_id": gap_id,
            "added_at": "2026-01-01T00:00:00",
            "auto_paused": True,
            "paused_reason": "Churn guardrail: repeated no-merge cycles",
            "paused_at": "2026-06-01T00:00:00",
            "churn_family_pause_count": churn_count,
            "churn_pause_evidence": {"windowed_non_merge": 9},
            "needs_manual_intervention": True,
            "needs_manual_intervention_at": "2026-06-01T00:00:00",
            "churn_retirement_recommended": True,
            "churn_retirement_recommended_reason": (
                "Retirement recommended: zombie direction paused twice."
            ),
            "churn_retirement_recommended_at": "2026-06-01T00:00:00",
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()


@pytest.mark.asyncio
async def test_churn_retirement_decision_keep_and_retire_clear_flag(
    client: AsyncClient, db_session, mocker
):
    """The keep/retire decision seam clears the churn-retirement recommendation.

    KEEP reactivates the held-paused direction, resets the churn-family ladder
    counter to 0, and clears the recommendation + manual-hold flags. RETIRE
    confirms the explicit stopped/retired terminal outcome, clears the
    recommendation, and reconciles the freed problem-model gap.
    """
    # ── KEEP ────────────────────────────────────────────────────────────────
    keep_project_id = await _deploy_project(client, mocker)
    keep_dir = "d-keep-churn"
    await _seed_churn_recommended_direction(db_session, keep_project_id, keep_dir)

    keep_resp = await client.post(
        f"/api/admin/projects/{keep_project_id}/directions/{keep_dir}/churn-retirement-decision",
        json={"decision": "KEEP", "rationale": "still worth pursuing"},
    )
    assert keep_resp.status_code == 200, keep_resp.text
    keep_body = keep_resp.json()
    assert keep_body["status"] == "kept"
    assert keep_body["decision"] == "KEEP"
    kept = keep_body["direction"]
    assert kept["status"] == "active"
    # Recommendation + manual-hold residue cleared; churn ladder reset to 0.
    assert "churn_retirement_recommended" not in kept
    assert "churn_retirement_recommended_reason" not in kept
    assert "needs_manual_intervention" not in kept
    assert kept["churn_family_pause_count"] == 0
    assert "churn_pause_evidence" not in kept
    # Grace marker stamped so frozen history can't immediately re-pause it.
    assert kept.get("auto_recovered_at")

    # Persisted state matches the response.
    project = (
        await db_session.execute(
            select(Project).where(Project.id == uuid.UUID(keep_project_id))
        )
    ).scalar_one()
    await db_session.refresh(project)
    blob = next(d for d in project.directions if d["id"] == keep_dir)
    assert blob["status"] == "active"
    assert not blob.get("churn_retirement_recommended")
    assert blob["churn_family_pause_count"] == 0

    # Idempotent: a second KEEP on a now-decided direction is a clean no-op.
    noop_resp = await client.post(
        f"/api/admin/projects/{keep_project_id}/directions/{keep_dir}/churn-retirement-decision",
        json={"decision": "KEEP"},
    )
    assert noop_resp.status_code == 200, noop_resp.text
    assert noop_resp.json()["status"] == "noop"

    # ── RETIRE ──────────────────────────────────────────────────────────────
    retire_project_id = await _deploy_project(client, mocker)
    retire_dir = "d-retire-churn"
    await _seed_churn_recommended_direction(
        db_session, retire_project_id, retire_dir, gap_id="g-retire"
    )

    retire_resp = await client.post(
        f"/api/admin/projects/{retire_project_id}/directions/{retire_dir}/churn-retirement-decision",
        json={"decision": "RETIRE", "rationale": "no path to value"},
    )
    assert retire_resp.status_code == 200, retire_resp.text
    retire_body = retire_resp.json()
    assert retire_body["status"] == "retired"
    assert retire_body["decision"] == "RETIRE"
    retired = retire_body["direction"]
    assert retired["status"] == "retired"
    assert retired["retirement_outcome"] == "stopped"
    assert retired["auto_completed"] is True
    assert "decision" in retired["auto_completed_reason"].lower()
    assert retired["completed_at"]
    assert "churn_retirement_recommended" not in retired
    assert "needs_manual_intervention" not in retired

    # Gap reconciled: the retired direction is pruned from active_moves.
    retire_project = (
        await db_session.execute(
            select(Project).where(Project.id == uuid.UUID(retire_project_id))
        )
    ).scalar_one()
    await db_session.refresh(retire_project)
    gap = next(g for g in retire_project.problem_model["gaps"] if g["id"] == "g-retire")
    assert retire_dir not in gap["active_moves"]
    assert retire_dir in gap["past_moves"]
    # Retired-direction memory recorded so a same-gap re-spawn is blocked next cycle.
    retired_mem = (retire_project.vision or {}).get("_recently_retired_directions") or []
    assert any(m.get("id") == retire_dir for m in retired_mem)


@pytest.mark.asyncio
async def test_churn_retirement_decision_rejects_invalid_decision(
    client: AsyncClient, db_session, mocker
):
    """An unknown decision value is a 400, not a silent no-op or 500."""
    project_id = await _deploy_project(client, mocker)
    direction_id = "d-bad-decision"
    await _seed_churn_recommended_direction(db_session, project_id, direction_id)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/churn-retirement-decision",
        json={"decision": "MAYBE"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_direction_stage_for_explicit_future_version(
    client: AsyncClient, db_session, mocker
):
    """PATCH status='staged' with an explicit future staged_for_version stamps
    that version rather than the current open one."""
    project_id = await _deploy_project(client, mocker)
    project_uuid = uuid.UUID(project_id)
    from app import release_version as rv

    await rv.open_version(db_session, project_uuid, version_number=3)
    await db_session.commit()

    direction = await _add_direction(client, project_id)
    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"status": "staged", "staged_for_version": 5},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["direction"]["staged_for_version"] == 5


@pytest.mark.asyncio
async def test_update_direction_stage_rejects_already_promoted_version(
    client: AsyncClient, db_session, mocker
):
    """A staged_for_version below the current open version (already promoted) is
    rejected with 400 rather than auto-activating on the next sweep."""
    project_id = await _deploy_project(client, mocker)
    project_uuid = uuid.UUID(project_id)
    from app import release_version as rv

    await rv.open_version(db_session, project_uuid, version_number=4)
    await db_session.commit()

    direction = await _add_direction(client, project_id)
    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"status": "staged", "staged_for_version": 2},
    )
    assert resp.status_code == 400
    assert "already promoted" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_update_direction_retarget_staged_direction(
    client: AsyncClient, db_session, mocker
):
    """PATCH staged_for_version on an already-staged direction retargets it to a
    new version without changing its status."""
    project_id = await _deploy_project(client, mocker)
    project_uuid = uuid.UUID(project_id)
    from app import release_version as rv

    await rv.open_version(db_session, project_uuid, version_number=6)
    await db_session.commit()

    direction = await _add_direction(client, project_id)
    direction_id = direction["id"]
    await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={"status": "staged"},
    )

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={"staged_for_version": 8},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["direction"]
    assert body["status"] == "staged"
    assert body["staged_for_version"] == 8

    _, row = await _load_project_and_direction(db_session, project_id, direction_id)
    assert row is not None and row.data["staged_for_version"] == 8


@pytest.mark.asyncio
async def test_update_direction_retarget_rejects_non_staged(
    client: AsyncClient, db_session, mocker
):
    """Retargeting (staged_for_version without a status change) is rejected with
    409 when the direction is not currently staged."""
    project_id = await _deploy_project(client, mocker)
    project_uuid = uuid.UUID(project_id)
    from app import release_version as rv

    await rv.open_version(db_session, project_uuid, version_number=6)
    await db_session.commit()

    direction = await _add_direction(client, project_id)
    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"staged_for_version": 7},
    )
    assert resp.status_code == 409
    assert "staged direction" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_update_direction_status_activate_now_clears_stage_marker(
    client: AsyncClient, db_session, mocker
):
    """PATCH status='active' reactivates a staged direction and clears staging residue."""
    project_id = await _deploy_project(client, mocker)
    project_uuid = uuid.UUID(project_id)
    from app import release_version as rv
    from app.task_locking import with_directions_locked

    await rv.open_version(db_session, project_uuid, version_number=8)
    await db_session.commit()

    direction = await _add_direction(client, project_id)
    direction_id = direction["id"]

    await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={"status": "staged"},
    )

    def _seed_staged_residue(dirs):
        dirs[0].update(
            {
                "paused_reason": "stale pause",
                "paused_at": "2026-06-01T05:00:00",
                "auto_paused": True,
                "needs_manual_intervention": True,
                "needs_manual_intervention_at": "2026-06-01T06:00:00",
                "escalation_notification_pending": True,
                "escalation_notification_retries": 1,
                "last_researched_at": "2026-06-01T07:00:00",
                "staged_activated_at": "2026-06-01T08:00:00",
                "remediation_attempts": 5,
            }
        )

    await with_directions_locked(db_session, project_uuid, _seed_staged_residue)
    await db_session.commit()

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={"status": "active"},
    )
    assert resp.status_code == 200
    body = resp.json()["direction"]
    assert body["status"] == "active"
    assert "staged_for_version" not in body
    assert "staged_activated_at" not in body
    for field in (
        "paused_reason",
        "needs_manual_intervention",
        "escalation_notification_pending",
        "last_researched_at",
    ):
        assert field not in body
    assert body["remediation_attempts"] == 0

    project, row = await _load_project_and_direction(
        db_session, project_id, direction_id
    )
    blob = next(d for d in project.directions or [] if d.get("id") == direction_id)
    assert blob["status"] == "active"
    assert "staged_for_version" not in blob
    assert row is not None
    assert row.status == "active"
    assert "staged_for_version" not in row.data


@pytest.mark.asyncio
async def test_update_direction_text(client: AsyncClient, mocker):
    """PATCH updates direction text."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Original text")

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"text": "Updated text"},
    )
    assert resp.status_code == 200
    assert resp.json()["direction"]["text"] == "Updated text"


@pytest.mark.asyncio
async def test_update_direction_paths(client: AsyncClient, mocker):
    """PATCH updates direction paths."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, paths=["old/path"])

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"paths": ["new/path1", "new/path2"]},
    )
    assert resp.status_code == 200
    assert resp.json()["direction"]["paths"] == ["new/path1", "new/path2"]


@pytest.mark.asyncio
async def test_delete_direction_removes_it(client: AsyncClient, db_session, mocker):
    """DELETE tombstones a staged direction to prevent re-staging."""
    project_id = await _deploy_project(client, mocker)
    project_uuid = uuid.UUID(project_id)
    from app import release_version as rv

    await rv.open_version(db_session, project_uuid, version_number=9)
    await db_session.commit()
    direction = await _add_direction(client, project_id)
    direction_id = direction["id"]

    stage_resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={"status": "staged"},
    )
    assert stage_resp.status_code == 200
    assert stage_resp.json()["direction"]["status"] == "staged"

    resp = await client.delete(
        f"/api/admin/projects/{project_id}/directions/{direction_id}"
    )
    assert resp.status_code == 200
    assert resp.json()["message"] == "Direction removed"

    project, row = await _load_project_and_direction(
        db_session, project_id, direction_id
    )
    tombstone = next(d for d in project.directions or [] if d.get("id") == direction_id)
    assert tombstone["status"] == "rejected"
    assert "staged_for_version" not in tombstone
    assert row is not None
    assert row.status == "rejected"
    assert row.data["status"] == "rejected"


@pytest.mark.asyncio
async def test_delete_direction_cancels_queued_tasks(
    client: AsyncClient, db_session, mocker
):
    """DELETE cancels QUEUED tasks whose context.direction_id matches the deleted direction."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Direction to delete")
    direction_id = direction["id"]

    # Create a QUEUED task that references this direction in its context
    task = Task(
        project_id=uuid.UUID(project_id),
        title="Autonomous research task",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={"direction_id": direction_id},
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()

    resp = await client.delete(
        f"/api/admin/projects/{project_id}/directions/{direction_id}"
    )
    assert resp.status_code == 200
    assert resp.json()["cancelled_tasks"] == 1

    # Verify the task status is now CANCELLED
    await db_session.refresh(task)
    assert task.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_manual_research_409_when_research_in_flight(
    client: AsyncClient, db_session, mocker
):
    """The manual research endpoint must refuse to spawn a second research on a
    direction that already has one in flight — two concurrent research cycles
    produce two rollup PRs that conflict (incident 2026-06-03 on d-5d841cea).
    Uses the legacy payload shape (no current_direction_id) for the in-flight
    task to also exercise the directions-list fallback matcher."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Already researching")
    direction_id = direction["id"]

    in_flight = Task(
        project_id=uuid.UUID(project_id),
        title="Research: in flight",
        # Legacy shape: directions list, no current_direction_id.
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(in_flight)
    await db_session.commit()

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
    )
    assert resp.status_code == 409, resp.text
    assert "in progress" in resp.text.lower()

    # bypass_cooldown must NOT override the concurrency guard.
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
        "?bypass_cooldown=true"
    )
    assert resp.status_code == 409, resp.text

    # A different direction is unaffected.
    other = await _add_direction(client, project_id, text="Different direction")
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{other['id']}/research"
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_manual_research_409_when_research_parked(
    client: AsyncClient, db_session, mocker
):
    """The manual research endpoint must refuse to spawn when the direction already
    has a PARKED research task. PARKED is non-terminal (waiting to wake) and must
    hold the direction exactly like IN_PROGRESS or BLOCKED."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Already parked")
    direction_id = direction["id"]

    # Modern row: carries current_direction_id so the fast-path claim-key check fires.
    parked_modern = Task(
        project_id=uuid.UUID(project_id),
        title="Research: parked modern",
        payloads=[
            {
                "type": "research",
                "current_direction_id": direction_id,
                "directions": [direction],
                "base_branch": "main",
            }
        ],
        status=TaskStatus.PARKED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(parked_modern)
    await db_session.commit()

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
    )
    assert resp.status_code == 409, resp.text
    assert "in progress" in resp.text.lower()

    # bypass_cooldown must NOT override the concurrency guard.
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
        "?bypass_cooldown=true"
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_manual_research_409_when_research_parked_legacy(
    client: AsyncClient, db_session, mocker
):
    """Legacy payload shape (no current_direction_id, direction in 'directions' list)
    with PARKED status must also return 409 — the directions-list fallback matcher
    must treat PARKED as active."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Parked legacy shape")
    direction_id = direction["id"]

    parked_legacy = Task(
        project_id=uuid.UUID(project_id),
        title="Research: parked legacy",
        # Legacy shape: directions list, no current_direction_id.
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        status=TaskStatus.PARKED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(parked_legacy)
    await db_session.commit()

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
    )
    assert resp.status_code == 409, resp.text
    assert "in progress" in resp.text.lower()


@pytest.mark.asyncio
async def test_manual_research_stamps_current_direction_id(
    client: AsyncClient, db_session, mocker
):
    """Manual research uses the same complete envelope as every other root."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.discovery_admission import direction_authority_snapshot
    from app.research_completion import effective_research_finding_headroom
    from app.shared_state import runtime_config

    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Stamp the direction id")
    direction_id = direction["id"]
    project = await db_session.get(Project, uuid.UUID(project_id))
    project.problem_model = {
        "capabilities": [{"name": "research-envelope", "status": "working"}]
    }
    flag_modified(project, "problem_model")
    db_session.add(project)
    await db_session.commit()
    mocker.patch.dict(
        runtime_config.setdefault("agent_prompt_suffixes", {}),
        {"research": "Preserve the canonical root envelope."},
    )

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]

    created = await db_session.get(Task, uuid.UUID(task_id))
    assert created is not None
    payload = created.first_payload
    assert payload["current_direction_id"] == direction_id
    assert payload["directions"] == [direction_authority_snapshot(direction)]
    assert created.discovery_cycle_id is not None
    assert payload["discovery_intent_snapshot"]
    assert payload["discovery_bet_snapshot"]
    assert payload["work_headroom"] == effective_research_finding_headroom(0)
    assert payload["available_capabilities"][0]["name"] == "research-envelope"
    assert payload["prompt_suffix"] == "Preserve the canonical root envelope."


@pytest.mark.asyncio
async def test_manual_research_stamps_direction_claim_key(
    client: AsyncClient, db_session, mocker
):
    """The manual research task must carry the SAME per-direction idempotency
    claim key the autonomous loop stamps, so a manual spawn and a concurrent
    autonomous tick can never both create an active research cycle on one
    direction (atomic under ux_task_active_idempotency_key)."""
    from app.autonomous_research import research_direction_claim_key

    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Claim the direction")
    direction_id = direction["id"]

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]

    created = await db_session.get(Task, uuid.UUID(task_id))
    assert created is not None
    assert created.idempotency_key == research_direction_claim_key(
        uuid.UUID(project_id), direction_id
    )


@pytest.mark.asyncio
async def test_manual_research_409_when_concurrent_spawn_holds_claim(
    client: AsyncClient, db_session, mocker
):
    """Atomic backstop for the TOCTOU window the in-flight SELECT guard cannot
    close (same race class as the 2026-06-06 autonomous twins): a concurrent
    spawn committed its research task AFTER this request's in-flight read but
    BEFORE its insert. Simulated by an active claim-holder whose payload is
    invisible to the read guard (no current_direction_id / empty directions):
    the endpoint must hit the unique-index claim and return 409, not create a
    duplicate research task."""
    from app.autonomous_research import research_direction_claim_key

    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Claim already held")
    direction_id = direction["id"]

    claim_key = research_direction_claim_key(uuid.UUID(project_id), direction_id)
    holder = Task(
        project_id=uuid.UUID(project_id),
        title="Research: concurrent spawn that won the claim",
        # Payload shape the read guard can't attribute to the direction —
        # mimics "committed between the read and the insert".
        payloads=[{"type": "research", "directions": [], "base_branch": "main"}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        idempotency_key=claim_key,
    )
    db_session.add(holder)
    await db_session.commit()

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
    )
    assert resp.status_code == 409, resp.text
    assert "in progress" in resp.text.lower()
    assert str(holder.id)[:8] in resp.text

    # Exactly one task holds the claim — no duplicate row was created.
    result = await db_session.execute(
        select(Task).where(Task.idempotency_key == claim_key)
    )
    assert len(result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_manual_research_409_semi_modern_current_direction_id(
    client: AsyncClient, db_session, mocker
):
    """Semi-modern fast path: a task that carries current_direction_id but no
    idempotency key (predates the claim-key stamp) must still be detected as
    an in-flight duplicate and return 409."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Semi-modern in flight")
    direction_id = direction["id"]

    semi_modern = Task(
        project_id=uuid.UUID(project_id),
        title="Research: semi-modern in flight",
        # Has current_direction_id but no idempotency_key.
        payloads=[
            {
                "type": "research",
                "current_direction_id": direction_id,
                "base_branch": "main",
            }
        ],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(semi_modern)
    await db_session.commit()

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
    )
    assert resp.status_code == 409, resp.text
    assert "in progress" in resp.text.lower()

    # A different direction is unaffected.
    other = await _add_direction(client, project_id, text="Other direction")
    resp2 = await client.post(
        f"/api/admin/projects/{project_id}/directions/{other['id']}/research"
    )
    assert resp2.status_code == 200, resp2.text


@pytest.mark.asyncio
async def test_manual_research_ignores_non_research_current_direction_id(
    client: AsyncClient, db_session, mocker
):
    """An active task from another workflow may still carry current_direction_id,
    but it must not block research unless it is itself a research task."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Non-research payload")
    direction_id = direction["id"]

    blocker = Task(
        project_id=uuid.UUID(project_id),
        title="Create task with direction attribution",
        payloads=[
            {
                "type": "create",
                "current_direction_id": direction_id,
                "base_branch": "main",
            }
        ],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(blocker)
    await db_session.commit()

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
    )
    assert resp.status_code == 200, resp.text

    created = await db_session.execute(
        select(Task).where(
            Task.project_id == uuid.UUID(project_id),
            Task.status == TaskStatus.QUEUED,
        )
    )
    assert any(
        task.idempotency_key is not None for task in created.scalars().all()
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Cooldown Filter (_spawn_research_task)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cooldown_filter_ignores_recent_last_researched_at_without_refractory(
    db_session,
):
    """A recent last_researched_at alone no longer blocks research; refractory_until does."""
    project = Project(
        name="Cooldown Test — Recent",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                "id": "d-recent01",
                "text": "Researched recently",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "last_researched_at": (
                    _utcnow() - timedelta(hours=1)
                ).isoformat(),
            }
        ],
        vision={
            "purpose": "test"
        },  # Non-empty vision so force_strategic doesn't bypass
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert len(tasks) >= 1, "recent last_researched_at alone should not block research"


@pytest.mark.asyncio
async def test_cooldown_filter_includes_direction_past_cooldown(db_session):
    """A direction researched 5 hours ago (past 4-hour cooldown) should trigger a task."""
    project = Project(
        name="Cooldown Test — Past",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                "id": "d-past0001",
                "text": "Researched long ago",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "last_researched_at": (
                    _utcnow() - timedelta(hours=5)
                ).isoformat(),
            }
        ],
        vision={
            "purpose": "test"
        },  # Non-empty vision so force_strategic doesn't bypass
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    research_tasks = [
        t for t in tasks if (t.payloads or [{}])[0].get("type") == "research"
    ]
    assert len(research_tasks) == 1, (
        "A research task should be spawned when past cooldown"
    )


@pytest.mark.asyncio
async def test_cooldown_filter_includes_direction_never_researched(db_session):
    """A direction with no last_researched_at is always eligible for research."""
    project = Project(
        name="Cooldown Test — Never",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                "id": "d-never001",
                "text": "Never been researched",
                "status": "active",
                "priority": "high",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                # no last_researched_at key
            }
        ],
        vision={
            "purpose": "test"
        },  # Non-empty vision so force_strategic doesn't bypass
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    research_tasks = [
        t for t in tasks if (t.payloads or [{}])[0].get("type") == "research"
    ]
    assert len(research_tasks) == 1, (
        "A research task should be spawned for a never-researched direction"
    )


@pytest.mark.asyncio
async def test_spawn_skipped_when_autonomous_disabled(db_session):
    """_spawn_research_task does nothing when autonomous_enabled is False."""
    project = Project(
        name="Autonomous Disabled",
        user_id="test-user-123",
        autonomous_enabled=False,
        directions=[
            {
                "id": "d-dis00001",
                "text": "Should not be researched",
                "status": "active",
                "priority": "high",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. last_researched_at Update (_handle_research_completion)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_research_completion_updates_last_researched_at(
    db_session, mocker
):
    """After _handle_research_completion runs, the researched direction gets last_researched_at set."""
    direction_id = "d-resrch01"
    direction = {
        "id": direction_id,
        "text": "Improve logging",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }

    project = Project(
        name="Research Completion Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Research: Improve logging",
        payloads=[
            {
                "type": "research",
                "directions": [direction],
                "base_branch": "main",
            }
        ],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Mock all external calls made inside _handle_research_completion
    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new=AsyncMock(return_value=("fake-token", "owner", "repo")),
    )
    mocker.patch(
        "app.task_completion.get_branch_sha", new=AsyncMock(return_value="abc123sha")
    )
    mocker.patch("app.task_completion.create_branch", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.research_completion._validate_plan_dependencies", return_value=True
    )
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch(
        "app.research_completion._apply_structural_actionability_downgrade",
        return_value=0,
    )
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "title": "Add unit tests for directions",
                "description": "Improve coverage of the directions module",
                "prompt": "Write unit tests for the directions CRUD endpoints",
                "priority": "medium",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction_id,
            }
        ],
    )

    before = _utcnow()
    await _handle_research_completion(task, "", db_session)

    # Reload project from DB to get the updated directions
    await db_session.refresh(project)
    updated_directions = project.directions
    assert updated_directions is not None

    researched = next((d for d in updated_directions if d["id"] == direction_id), None)
    assert researched is not None, (
        "Direction should still exist after research completion"
    )
    assert "last_researched_at" in researched, (
        "last_researched_at should be set after completion"
    )

    last_ts = datetime.fromisoformat(researched["last_researched_at"])
    assert last_ts >= before, (
        "last_researched_at should be set to a time after the test started"
    )


@pytest.mark.asyncio
async def test_handle_research_completion_no_findings_skips_update(db_session, mocker):
    """When there are no findings, last_researched_at is NOT updated and no tasks are created."""
    direction_id = "d-nofind01"
    direction = {
        "id": direction_id,
        "text": "Direction with no findings",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }

    project = Project(
        name="No Findings Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Research: Direction with no findings",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[]
    )  # no findings

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    directions = project.directions
    researched = next(d for d in directions if d["id"] == direction_id)
    assert "last_researched_at" not in researched, (
        "last_researched_at should NOT be set when there are no findings"
    )

    # No child tasks should have been created
    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 0


@pytest.mark.asyncio
async def test_handle_research_completion_direction_update_sets_pending_update(
    db_session, mocker
):
    """A direction_update finding stores pending_update on the target direction and creates no task."""
    direction_id = "d-dupdate1"
    direction = {
        "id": direction_id,
        "text": "Old direction text",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }

    project = Project(
        name="Direction Update Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Research: Old direction text",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "o", "r")),
    )
    mocker.patch(
        "app.task_completion.get_branch_sha", new=AsyncMock(return_value="abc123sha")
    )
    mocker.patch("app.task_completion.create_branch", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.research_completion._validate_plan_dependencies", return_value=True
    )
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "type": "direction_update",
                "direction_id": direction_id,
                "title": "Refine direction scope",
                "description": "The direction is too broad; narrow it to authentication only.",
                "suggested_text": "Focus on authentication module",
                "suggested_priority": "high",
                "suggested_paths": ["backend/app/auth.py"],
                "prompt": "",  # direction_update findings have no task prompt
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    # Reload from DB
    await db_session.refresh(project)
    updated_directions = project.directions
    target = next((d for d in updated_directions if d["id"] == direction_id), None)
    assert target is not None

    # pending_update should be set
    assert "pending_update" in target, (
        "pending_update should be written to the direction"
    )
    pu = target["pending_update"]
    assert pu["suggested_text"] == "Focus on authentication module"
    assert pu["suggested_priority"] == "high"
    assert pu["suggested_paths"] == ["backend/app/auth.py"]
    assert "rationale" in pu
    assert "proposed_at" in pu

    # No child task should have been created
    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 0, (
        "direction_update findings must not create child tasks"
    )


@pytest.mark.asyncio
async def test_handle_research_completion_direction_note_appends_note(
    db_session, mocker
):
    """A direction_note finding appends a note to the target direction and creates no task."""
    direction_id = "d-dnote01"
    direction = {
        "id": direction_id,
        "text": "Improve logging coverage",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }

    project = Project(
        name="Direction Note Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Research: Improve logging coverage",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "o", "r")),
    )
    mocker.patch(
        "app.task_completion.get_branch_sha", new=AsyncMock(return_value="abc123sha")
    )
    mocker.patch("app.task_completion.create_branch", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.research_completion._validate_plan_dependencies", return_value=True
    )
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "type": "direction_note",
                "direction_id": direction_id,
                "title": "Logging observation",
                "description": "Found that request errors are not logged at the middleware level.",
                "prompt": "",  # direction_note findings have no task prompt
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    # Reload from DB
    await db_session.refresh(project)
    updated_directions = project.directions
    target = next((d for d in updated_directions if d["id"] == direction_id), None)
    assert target is not None

    # Note should be appended
    assert "notes" in target, "notes list should be written to the direction"
    assert len(target["notes"]) == 1
    note = target["notes"][0]
    assert (
        note["text"]
        == "Found that request errors are not logged at the middleware level."
    )
    assert "added_at" in note
    assert "added_by_task" in note

    # No child task should have been created
    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 0, "direction_note findings must not create child tasks"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Task-Counts Endpoint
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_counts_empty(client: AsyncClient, mocker):
    """task-counts returns an empty dict when no research tasks exist."""
    project_id = await _deploy_project(client, mocker)

    resp = await client.get(f"/api/admin/projects/{project_id}/directions/task-counts")
    assert resp.status_code == 200
    assert resp.json()["counts"] == {}


@pytest.mark.asyncio
async def test_task_counts_single_direction(client: AsyncClient, mocker):
    """task-counts returns count=1 after one research task is triggered for a direction."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Direction for research")
    direction_id = direction["id"]

    research_resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
    )
    assert research_resp.status_code == 200

    resp = await client.get(f"/api/admin/projects/{project_id}/directions/task-counts")
    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert counts.get(direction_id, 0) == 1


@pytest.mark.asyncio
async def test_task_counts_multiple_directions(client: AsyncClient, mocker):
    """task-counts tracks counts independently per direction."""
    project_id = await _deploy_project(client, mocker)
    dir_a = await _add_direction(client, project_id, text="Direction A")
    dir_b = await _add_direction(client, project_id, text="Direction B")

    await client.post(
        f"/api/admin/projects/{project_id}/directions/{dir_a['id']}/research"
    )
    await client.post(
        f"/api/admin/projects/{project_id}/directions/{dir_b['id']}/research"
    )

    resp = await client.get(f"/api/admin/projects/{project_id}/directions/task-counts")
    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert counts[dir_a["id"]] == 1
    assert counts[dir_b["id"]] == 1


@pytest.mark.asyncio
async def test_task_counts_includes_non_research_tasks_with_direction_id(
    client: AsyncClient, db_session, mocker
):
    """Non-research tasks with a direction_id in context ARE counted, and
    appear under their own type bucket in counts_by_type."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Direction C")
    direction_id = direction["id"]

    db_session.add(
        Task(
            project_id=uuid.UUID(project_id),
            title="feat: something",
            payloads=[{"type": "create", "base_branch": "main"}],
            context={"direction_id": direction_id},
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions/task-counts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts"].get(direction_id) == 1
    assert body["counts_by_type"][direction_id]["create"] == 1


@pytest.mark.asyncio
async def test_task_counts_context_direction_id_preferred_over_payload(
    client: AsyncClient, db_session, mocker
):
    """context.direction_id takes precedence over payload-derived direction ids."""
    project_id = await _deploy_project(client, mocker)
    dir_ctx = await _add_direction(client, project_id, text="Ctx direction")
    dir_payload = await _add_direction(client, project_id, text="Payload direction")

    db_session.add(
        Task(
            project_id=uuid.UUID(project_id),
            title="Research: mixed",
            payloads=[
                {
                    "type": "research",
                    "current_direction_id": dir_payload["id"],
                    "directions": [dir_payload],
                    "base_branch": "main",
                }
            ],
            context={"direction_id": dir_ctx["id"]},
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions/task-counts")
    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert counts.get(dir_ctx["id"]) == 1
    assert counts.get(dir_payload["id"], 0) == 0


@pytest.mark.asyncio
async def test_task_counts_payload_fallback_when_context_missing(
    client: AsyncClient, db_session, mocker
):
    """Older tasks without context.direction_id are still counted via payload."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Fallback direction")
    direction_id = direction["id"]

    db_session.add(
        Task(
            project_id=uuid.UUID(project_id),
            title="Research: legacy",
            payloads=[
                {
                    "type": "research",
                    "current_direction_id": direction_id,
                    "base_branch": "main",
                }
            ],
            context={},
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions/task-counts")
    assert resp.status_code == 200
    assert resp.json()["counts"].get(direction_id) == 1


@pytest.mark.asyncio
async def test_task_counts_by_type_breakdown(client: AsyncClient, db_session, mocker):
    """counts_by_type reports per-type counts for mixed task types on a direction."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Mixed types")
    direction_id = direction["id"]

    for payload_type in ("research", "create", "create", "review"):
        db_session.add(
            Task(
                project_id=uuid.UUID(project_id),
                title=f"{payload_type} task",
                payloads=[{"type": payload_type, "base_branch": "main"}],
                context={"direction_id": direction_id},
                status=TaskStatus.IN_PROGRESS,
                created_by="ORCHESTRATOR",
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            )
        )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions/task-counts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts"][direction_id] == 4
    by_type = body["counts_by_type"][direction_id]
    assert by_type == {"research": 1, "create": 2, "review": 1}


@pytest.mark.asyncio
async def test_task_counts_excludes_cancelled_and_failed(
    client: AsyncClient, db_session, mocker
):
    """task-counts must not count CANCELLED or FAILED research tasks."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(
        client, project_id, text="Direction for cancelled test"
    )
    direction_id = direction["id"]

    # Insert a CANCELLED research task directly into the DB
    cancelled_task = Task(
        project_id=uuid.UUID(project_id),
        title="Research: cancelled",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={"direction_id": direction_id},
        status=TaskStatus.CANCELLED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    # Insert a FAILED research task directly into the DB
    failed_task = Task(
        project_id=uuid.UUID(project_id),
        title="Research: failed",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={"direction_id": direction_id},
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(cancelled_task)
    db_session.add(failed_task)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions/task-counts")
    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert counts.get(direction_id, 0) == 0, (
        "CANCELLED and FAILED tasks must not be included in task-counts"
    )


@pytest.mark.asyncio
async def test_task_counts_project_not_found(client: AsyncClient):
    """task-counts returns 404 for a nonexistent project."""
    fake_id = str(uuid.uuid4())
    resp = await client.get(f"/api/admin/projects/{fake_id}/directions/task-counts")
    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 5. Status and Priority Validation
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_direction_invalid_status_returns_400(client: AsyncClient, mocker):
    """PATCH with an unrecognised status value returns 400."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"status": "invalid_status"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_direction_invalid_priority_returns_400(
    client: AsyncClient, mocker
):
    """PATCH with an unrecognised priority value returns 400."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"priority": "urgent"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_direction_empty_text_returns_400(client: AsyncClient, mocker):
    """PATCH with empty text returns 400."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"text": ""},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_direction_not_found_returns_404(client: AsyncClient, mocker):
    """PATCH on a nonexistent direction_id returns 404."""
    project_id = await _deploy_project(client, mocker)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/d-notexist",
        json={"priority": "low"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_directions_project_not_found(client: AsyncClient):
    """GET directions for a nonexistent project returns 404."""
    resp = await client.get(f"/api/admin/projects/{uuid.uuid4()}/directions")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_nonexistent_direction_returns_404(client: AsyncClient, mocker):
    """DELETE a direction that does not exist returns 404."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.delete(
        f"/api/admin/projects/{project_id}/directions/d-notexist"
    )
    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 7. max_parallel_research_per_project settings
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_settings_returns_max_parallel_default(client: AsyncClient):
    """GET /api/admin/settings returns max_parallel_research_per_project with default 6."""
    resp = await client.get("/api/admin/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "max_parallel_research_per_project" in data
    assert data["max_parallel_research_per_project"] == 6


@pytest.mark.asyncio
async def test_patch_settings_max_parallel_persists(client: AsyncClient):
    """PATCH /api/admin/settings with max_parallel_research_per_project updates the value."""
    resp = await client.patch(
        "/api/admin/settings", json={"max_parallel_research_per_project": 5}
    )
    assert resp.status_code == 200

    resp2 = await client.get("/api/admin/settings")
    assert resp2.status_code == 200
    assert resp2.json()["max_parallel_research_per_project"] == 5


@pytest.mark.asyncio
async def test_patch_settings_max_parallel_invalid_returns_400(client: AsyncClient):
    """PATCH /api/admin/settings with out-of-range max_parallel_research_per_project returns 400."""
    resp = await client.patch(
        "/api/admin/settings", json={"max_parallel_research_per_project": 0}
    )
    assert resp.status_code == 400

    resp2 = await client.patch(
        "/api/admin/settings", json={"max_parallel_research_per_project": 21}
    )
    assert resp2.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# Standalone tests for _compute_productivity_score (no DB required)
# ─────────────────────────────────────────────────────────────────────────────


class TestComputeMergeRate:
    """Unit tests for _compute_productivity_score — pure function, no DB."""

    def test_never_researched_returns_neutral(self):
        """A direction with no history returns 0.5 (neutral/unknown)."""
        d = {"id": "d1", "priority": "medium"}
        assert _compute_productivity_score(d) == 0.5

    def test_empty_history_returns_neutral(self):
        """Explicit empty lists also return 0.5."""
        d = {"id": "d1", "research_history": [], "history_aggregate": {}}
        assert _compute_productivity_score(d) == 0.5

    def test_history_with_no_merges(self):
        """Cycles present but zero merges → rate = 0.0."""
        d = {
            "research_history": [
                {"prs_merged": 0},
                {"prs_merged": 0},
            ]
        }
        assert _compute_productivity_score(d) == 0.0

    def test_history_with_merges(self):
        """3 cycles, 6 total merges → rate = 2.0."""
        d = {
            "research_history": [
                {"prs_merged": 1},
                {"prs_merged": 3},
                {"prs_merged": 2},
            ]
        }
        assert _compute_productivity_score(d) == pytest.approx(6 / 3)

    def test_aggregate_counted(self):
        """history_aggregate cycles and merges are included in the total."""
        d = {
            "research_history": [{"prs_merged": 1}],
            "history_aggregate": {"total_cycles": 4, "total_prs_merged": 8},
        }
        # lifetime: 1 live + 4 agg = 5 cycles, 1+8=9 merged → 1.8
        # recent (last 3 live): [{"prs_merged":1}] → rate=1.0
        # weighted: (1.0*3 + 1.8) / 4 = 1.2
        assert _compute_productivity_score(d) == pytest.approx(1.2)

    def test_higher_merge_rate_sorted_first(self):
        """Within the same priority tier, higher merge_rate direction is selected first."""
        priority_rank = {"high": 0, "medium": 1, "low": 2}
        now = "2026-01-01T00:00:00"

        # Both medium priority, both researched — differ only by merge rate
        low_rate = {
            "id": "low",
            "priority": "medium",
            "possibly_saturated": False,
            "last_researched_at": now,
            "research_history": [{"prs_merged": 0}, {"prs_merged": 0}],
        }
        high_rate = {
            "id": "high",
            "priority": "medium",
            "possibly_saturated": False,
            "last_researched_at": now,
            "research_history": [{"prs_merged": 1}, {"prs_merged": 1}],
        }

        directions = [low_rate, high_rate]
        directions.sort(
            key=lambda d: (
                priority_rank.get(d.get("priority", "medium"), 1),
                1 if d.get("possibly_saturated") else 0,
                -_compute_productivity_score(d),
                d.get("last_researched_at") or "",
            )
        )
        assert directions[0]["id"] == "high"

    def test_never_researched_takes_precedence_over_merge_rate(self):
        """Directions with no last_researched_at are picked first (never_researched path)."""
        import random

        random.seed(42)

        # Simulate the never_researched branch: a direction with high merge rate
        # that has been researched should NOT beat a never-researched direction.
        never = {"id": "never", "priority": "medium", "last_researched_at": None}
        productive = {
            "id": "productive",
            "priority": "medium",
            "last_researched_at": "2026-01-01T00:00:00",
            "research_history": [{"prs_merged": 5}],
        }

        never_researched = [
            d for d in [never, productive] if not d.get("last_researched_at")
        ]
        assert never_researched == [never], (
            "Only the never-researched direction qualifies"
        )

    def test_saturated_deprioritized_regardless_of_merge_rate(self):
        """Saturated directions sort after non-saturated ones even with higher merge rate."""
        priority_rank = {"high": 0, "medium": 1, "low": 2}
        now = "2026-01-01T00:00:00"

        saturated_high = {
            "id": "sat",
            "priority": "medium",
            "possibly_saturated": True,
            "last_researched_at": now,
            "research_history": [{"prs_merged": 10}],
        }
        unsaturated_low = {
            "id": "unsat",
            "priority": "medium",
            "possibly_saturated": False,
            "last_researched_at": now,
            "research_history": [{"prs_merged": 0}],
        }

        directions = [saturated_high, unsaturated_low]
        directions.sort(
            key=lambda d: (
                priority_rank.get(d.get("priority", "medium"), 1),
                1 if d.get("possibly_saturated") else 0,
                -_compute_productivity_score(d),
                d.get("last_researched_at") or "",
            )
        )
        assert directions[0]["id"] == "unsat"


class TestComputeAcceptanceRate:
    """Unit tests for _compute_acceptance_rate — pure function, no DB."""

    def test_no_data_returns_none(self):
        assert _compute_acceptance_rate({}) is None
        assert (
            _compute_acceptance_rate({"research_history": [], "history_aggregate": {}})
            is None
        )

    def test_only_merges_returns_one(self):
        d = {"research_history": [{"prs_merged": 2, "prs_rejected": 0}]}
        assert _compute_acceptance_rate(d) == 1.0

    def test_only_rejections_returns_zero(self):
        d = {"research_history": [{"prs_merged": 0, "prs_rejected": 3}]}
        assert _compute_acceptance_rate(d) == 0.0

    def test_empty_recent_falls_back_to_lifetime(self):
        """No live history but aggregate has data → recent is empty → lifetime rate."""
        d = {
            "research_history": [],
            "history_aggregate": {"total_prs_merged": 3, "total_prs_rejected": 1},
        }
        assert _compute_acceptance_rate(d) == pytest.approx(0.75)

    def test_aggregate_contributes_to_lifetime(self):
        """history_aggregate merged/rejected totals roll into the lifetime computation."""
        d = {
            "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
            "history_aggregate": {"total_prs_merged": 1, "total_prs_rejected": 2},
        }
        # lifetime: merged=2, rejected=2 → 0.5; recent (last 3 live)=1 cycle, recent_rate=1.0
        # weighted: (1.0*3 + 0.5)/4 = 0.875
        assert _compute_acceptance_rate(d) == pytest.approx(0.875)

    def test_recent_dominates_lifetime(self):
        """3:1 weighting: recent (1.0) over lifetime (≈0.23) yields ~0.808."""
        d = {
            "research_history": [
                {"prs_merged": 1, "prs_rejected": 0},
                {"prs_merged": 1, "prs_rejected": 0},
                {"prs_merged": 1, "prs_rejected": 0},
            ],
            "history_aggregate": {"total_prs_merged": 0, "total_prs_rejected": 10},
        }
        lifetime = 3 / 13
        expected = (1.0 * 3 + lifetime) / 4
        assert _compute_acceptance_rate(d) == pytest.approx(expected)

    def test_recent_zero_prs_falls_back_to_lifetime(self):
        """When the recent window has cycles but no PRs, lifetime rate is returned."""
        d = {
            "research_history": [
                {"prs_merged": 0, "prs_rejected": 0},
                {"prs_merged": 0, "prs_rejected": 0},
                {"prs_merged": 0, "prs_rejected": 0},
            ],
            "history_aggregate": {"total_prs_merged": 4, "total_prs_rejected": 1},
        }
        assert _compute_acceptance_rate(d) == pytest.approx(0.8)


class TestComputeDirectionQualityScore:
    """Unit tests for _compute_direction_quality_score — pure function, no DB."""

    def test_no_history_returns_neutral(self):
        assert _compute_direction_quality_score({}) == 0.5
        assert _compute_direction_quality_score({"research_history": []}) == 0.5

    def test_all_actionable_taxonomy_high_score(self):
        """All failures actionable, tasks merging → score near 1.0."""
        d = {
            "research_history": [
                {
                    "tasks_created": 2,
                    "prs_merged": 1,
                    "taxonomy_summary": {
                        "code_defect": 2,
                        "scope_related": 1,
                        "infra_related": 0,
                        "review_quality": 0,
                    },
                }
            ]
        }
        # taxonomy_health=1.0, productivity=1.0 (active and merged), scope=1.0 (33% <= 50%)
        assert _compute_direction_quality_score(d) == pytest.approx(1.0)

    def test_all_structural_failures_low_taxonomy_health(self):
        """All failures structural (infra/review) → taxonomy_health=0, productivity=0, scope=1 → 1/3."""
        d = {
            "research_history": [
                {
                    "tasks_created": 2,
                    "prs_merged": 0,
                    "taxonomy_summary": {
                        "code_defect": 0,
                        "scope_related": 0,
                        "infra_related": 2,
                        "review_quality": 1,
                    },
                }
            ]
        }
        assert _compute_direction_quality_score(d) == pytest.approx(1 / 3)

    def test_scope_dominant_triggers_zero_scope_signal(self):
        """scope_related > 50% of taxonomy → scope_signal = 0."""
        d = {
            "research_history": [
                {
                    "tasks_created": 1,
                    "prs_merged": 1,
                    "taxonomy_summary": {
                        "code_defect": 1,
                        "scope_related": 5,
                        "infra_related": 0,
                        "review_quality": 0,
                    },
                }
            ]
        }
        # taxonomy_health=1.0 (all actionable), productivity=1.0, scope=0 → 2/3
        assert _compute_direction_quality_score(d) == pytest.approx(2 / 3)

    def test_recency_weighting_favors_recent(self):
        """Recent cycle weighted 3x: new healthy cycle dominates stale unhealthy ones."""
        d_recent_good = {
            "research_history": [
                {
                    "tasks_created": 1,
                    "prs_merged": 0,
                    "taxonomy_summary": {
                        "code_defect": 0,
                        "scope_related": 0,
                        "infra_related": 3,
                        "review_quality": 0,
                    },
                },
                {
                    "tasks_created": 1,
                    "prs_merged": 1,
                    "taxonomy_summary": {
                        "code_defect": 1,
                        "scope_related": 0,
                        "infra_related": 0,
                        "review_quality": 0,
                    },
                },
            ]
        }
        d_recent_bad = {
            "research_history": [
                {
                    "tasks_created": 1,
                    "prs_merged": 1,
                    "taxonomy_summary": {
                        "code_defect": 1,
                        "scope_related": 0,
                        "infra_related": 0,
                        "review_quality": 0,
                    },
                },
                {
                    "tasks_created": 1,
                    "prs_merged": 0,
                    "taxonomy_summary": {
                        "code_defect": 0,
                        "scope_related": 0,
                        "infra_related": 3,
                        "review_quality": 0,
                    },
                },
            ]
        }
        assert _compute_direction_quality_score(
            d_recent_good
        ) > _compute_direction_quality_score(d_recent_bad)

    def test_grade_distribution_high_majority_raises_score(self):
        """A cycle with mostly HIGH-grade findings scores higher than one with mostly LOW."""
        d_high = {
            "research_history": [
                {
                    "tasks_created": 1,
                    "prs_merged": 1,
                    "taxonomy_summary": {"code_defect": 1},
                    "grade_distribution": {"HIGH": 4, "MEDIUM": 1, "LOW": 0},
                }
            ]
        }
        d_low = {
            "research_history": [
                {
                    "tasks_created": 1,
                    "prs_merged": 1,
                    "taxonomy_summary": {"code_defect": 1},
                    "grade_distribution": {"HIGH": 0, "MEDIUM": 1, "LOW": 4},
                }
            ]
        }
        assert _compute_direction_quality_score(d_high) > _compute_direction_quality_score(d_low)

    def test_grade_distribution_neutral_when_absent(self):
        """grade_distribution absent from all cycles — score equals the 3-component baseline."""
        d_with = {
            "research_history": [
                {
                    "tasks_created": 1,
                    "prs_merged": 1,
                    "taxonomy_summary": {"code_defect": 1},
                    "grade_distribution": {"HIGH": 1, "MEDIUM": 1, "LOW": 1},
                }
            ]
        }
        d_without = {
            "research_history": [
                {
                    "tasks_created": 1,
                    "prs_merged": 1,
                    "taxonomy_summary": {"code_defect": 1},
                }
            ]
        }
        # Without grade_distribution the component is omitted; with balanced distribution
        # it contributes 1/3 ≈ 0.333 which pulls the score away from the 3-component result.
        assert _compute_direction_quality_score(d_with) != _compute_direction_quality_score(
            d_without
        )

    def test_grade_distribution_all_high_maximises_grade_component(self):
        """All-HIGH grade_distribution yields grade_quality_signal == 1.0."""
        d = {
            "research_history": [
                {
                    "tasks_created": 1,
                    "prs_merged": 1,
                    "taxonomy_summary": {"code_defect": 2},
                    "grade_distribution": {"HIGH": 5, "MEDIUM": 0, "LOW": 0},
                }
            ]
        }
        # Taxonomy=1.0, productivity=1.0, scope=1.0, grade=1.0 → avg=1.0
        assert _compute_direction_quality_score(d) == pytest.approx(1.0)

    def test_grade_distribution_legacy_cycles_ignored(self):
        """Cycles without grade_distribution are skipped in the grade component."""
        d_mixed = {
            "research_history": [
                # Legacy entry — no grade_distribution
                {"tasks_created": 1, "prs_merged": 1, "taxonomy_summary": {"code_defect": 1}},
                # New entry with all-HIGH grades
                {
                    "tasks_created": 1,
                    "prs_merged": 1,
                    "taxonomy_summary": {"code_defect": 1},
                    "grade_distribution": {"HIGH": 3, "MEDIUM": 0, "LOW": 0},
                },
            ]
        }
        d_all_grade = {
            "research_history": [
                {
                    "tasks_created": 1,
                    "prs_merged": 1,
                    "taxonomy_summary": {"code_defect": 1},
                    "grade_distribution": {"HIGH": 3, "MEDIUM": 0, "LOW": 0},
                },
                {
                    "tasks_created": 1,
                    "prs_merged": 1,
                    "taxonomy_summary": {"code_defect": 1},
                    "grade_distribution": {"HIGH": 3, "MEDIUM": 0, "LOW": 0},
                },
            ]
        }
        # Both should have grade_quality_signal dominated by HIGH;
        # mixed case should be close to but not necessarily equal to all-grade case.
        assert _compute_direction_quality_score(d_mixed) > 0.5
        assert _compute_direction_quality_score(d_all_grade) > 0.5

    def test_quality_score_treats_unverified_merge_as_wasted(self):
        """Unverified merge (verified_prs_merged=0) counts as wasted cycle.

        A cycle with tasks_created > 0 but zero verified merges (though raw
        prs_merged > 0) is scored as a wasted cycle. The quality score should
        be equal to a cycle that merged nothing, and strictly lower than the
        same cycle with verified merges.
        """
        # Cycle with unverified merge: tasks_created > 0, prs_merged > 0, verified_prs_merged = 0
        unverified_merge = {
            "research_history": [
                {
                    "tasks_created": 1,
                    "prs_merged": 1,
                    "verified_prs_merged": 0,  # unverified/degraded merge
                    "taxonomy_summary": {
                        "code_defect": 1,
                        "scope_related": 0,
                        "infra_related": 0,
                        "review_quality": 0,
                    },
                }
            ]
        }
        # Same cycle but with zero raw merges (truly wasted)
        no_merge = {
            "research_history": [
                {
                    "tasks_created": 1,
                    "prs_merged": 0,
                    "verified_prs_merged": 0,
                    "taxonomy_summary": {
                        "code_defect": 1,
                        "scope_related": 0,
                        "infra_related": 0,
                        "review_quality": 0,
                    },
                }
            ]
        }
        # Same cycle but with verified merge
        verified_merge = {
            "research_history": [
                {
                    "tasks_created": 1,
                    "prs_merged": 1,
                    "verified_prs_merged": 1,  # verified merge
                    "taxonomy_summary": {
                        "code_defect": 1,
                        "scope_related": 0,
                        "infra_related": 0,
                        "review_quality": 0,
                    },
                }
            ]
        }
        # Unverified merge should be equal to no merge
        assert _compute_direction_quality_score(unverified_merge) == pytest.approx(
            _compute_direction_quality_score(no_merge)
        )
        # Verified merge should score higher than unverified
        assert _compute_direction_quality_score(
            verified_merge
        ) > _compute_direction_quality_score(unverified_merge)

    def test_direction_sort_respects_quality_tiebreak(self):
        """Two directions with equal productivity: higher-quality sorts first."""
        from app.failure_classification import (
            TAXONOMY_CODE_DEFECT,
            TAXONOMY_INFRA_RELATED,
        )

        now = "2026-01-01T00:00:00"
        high_q = {
            "id": "high_q",
            "priority": "medium",
            "last_researched_at": now,
            "research_history": [
                {
                    "prs_merged": 1,
                    "tasks_created": 1,
                    "taxonomy_summary": {TAXONOMY_CODE_DEFECT: 1},
                },
            ],
        }
        low_q = {
            "id": "low_q",
            "priority": "medium",
            "last_researched_at": now,
            "research_history": [
                {
                    "prs_merged": 1,
                    "tasks_created": 2,
                    "taxonomy_summary": {TAXONOMY_INFRA_RELATED: 3},
                },
            ],
        }
        # Equivalent productivity (both have prs_merged=1 over 1 cycle)
        assert _compute_productivity_score(high_q) == pytest.approx(
            _compute_productivity_score(low_q)
        )
        # Quality score should differentiate
        assert _compute_direction_quality_score(
            high_q
        ) > _compute_direction_quality_score(low_q)
        directions = [low_q, high_q]
        directions.sort(
            key=lambda d: (
                -_compute_productivity_score(d),
                -_compute_direction_quality_score(d),
            )
        )
        assert directions[0]["id"] == "high_q"


class TestRollingMedianReviewIterations:
    """Unit tests for _compute_rolling_median_review_iterations — pure function."""

    def test_no_history_returns_none(self):
        assert _compute_rolling_median_review_iterations({}) is None
        assert (
            _compute_rolling_median_review_iterations({"research_history": []}) is None
        )

    def test_no_recorded_values_returns_none(self):
        d = {"research_history": [{"cycle_id": "c1"}, {"cycle_id": "c2"}]}
        assert _compute_rolling_median_review_iterations(d) is None

    def test_single_value(self):
        d = {"research_history": [{"median_review_iterations": 4}]}
        assert _compute_rolling_median_review_iterations(d) == 4.0

    def test_odd_count_returns_middle(self):
        d = {
            "research_history": [
                {"median_review_iterations": 2},
                {"median_review_iterations": 8},
                {"median_review_iterations": 5},
            ]
        }
        assert _compute_rolling_median_review_iterations(d) == 5.0

    def test_even_count_averages_middle(self):
        # Only last 3 considered; with just 2 values present in window, average them.
        d = {
            "research_history": [
                {"cycle_id": "c1"},  # no iteration recorded
                {"median_review_iterations": 3},
                {"median_review_iterations": 7},
            ]
        }
        assert _compute_rolling_median_review_iterations(d) == 5.0

    def test_only_last_three_cycles_considered(self):
        d = {
            "research_history": [
                {"median_review_iterations": 100},  # older, ignored
                {"median_review_iterations": 1},
                {"median_review_iterations": 2},
                {"median_review_iterations": 3},
            ]
        }
        assert _compute_rolling_median_review_iterations(d) == 2.0


# ─────────────────────────────────────────────────────────────────────────────
# 8. Direction Cap, completion_criteria, and merges_per_cycle in list
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_direction_cap_blocks_excess_active(client: AsyncClient, mocker):
    """Creating an 11th active direction returns 409 when cap is 10."""
    project_id = await _deploy_project(client, mocker)
    for i in range(10):
        resp = await client.post(
            f"/api/admin/projects/{project_id}/directions",
            json={"text": f"Direction {i + 1}", "priority": "medium"},
        )
        assert resp.status_code == 200

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "Direction 11 - should be blocked", "priority": "medium"},
    )
    assert resp.status_code == 409
    assert "Direction cap reached" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_direction_cap_allows_non_active(client: AsyncClient, mocker):
    """proposed and paused directions do not count toward the cap."""
    project_id = await _deploy_project(client, mocker)
    # Create 5 active directions to hit the cap
    for i in range(5):
        await client.post(
            f"/api/admin/projects/{project_id}/directions",
            json={"text": f"Active {i + 1}", "priority": "medium"},
        )
    # Pause one to make room
    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    directions = resp.json()["directions"]
    first_id = directions[0]["id"]
    await client.patch(
        f"/api/admin/projects/{project_id}/directions/{first_id}",
        json={"status": "paused"},
    )
    # Now we have 4 active, 1 paused — creating another active should succeed
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "Direction 6 - now allowed", "priority": "medium"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_direction_cap_on_reactivation(client: AsyncClient, mocker):
    """PATCH setting status to 'active' returns 409 when cap is already at 10."""
    project_id = await _deploy_project(client, mocker)
    # Create 9 active, then one more to reach cap of 10
    for i in range(9):
        await client.post(
            f"/api/admin/projects/{project_id}/directions",
            json={"text": f"Active {i + 1}", "priority": "medium"},
        )
    await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "To be paused", "priority": "low"},
    )
    # Now at cap (10 active). Pause one to allow creating an 11th, then re-activate to trigger 409.
    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    directions = resp.json()["directions"]
    # Pause one to allow creating an 11th
    first_id = directions[0]["id"]
    await client.patch(
        f"/api/admin/projects/{project_id}/directions/{first_id}",
        json={"status": "paused"},
    )
    # Now create 10th active direction (9 active + 1 paused = OK)
    tenth_resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "Tenth direction", "priority": "medium"},
    )
    assert tenth_resp.status_code == 200
    # Now we have 10 active + 1 paused. Trying to reactivate the paused one should fail
    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{first_id}",
        json={"status": "active"},
    )
    assert resp.status_code == 409
    assert "Direction cap reached" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_reactivation_clears_manual_intervention_state(
    client: AsyncClient, db_session, mocker
):
    """Operator reactivating a flagged direction clears escalation/intervention state."""
    from sqlalchemy.orm.attributes import flag_modified

    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Flagged direction")
    direction_id = direction["id"]

    # Simulate an escalated / flagged state (as set by remediation monitoring).
    project = (
        await db_session.execute(
            select(Project).where(Project.id == uuid.UUID(project_id))
        )
    ).scalar_one()
    for d in project.directions:
        if d["id"] == direction_id:
            d["status"] = "paused"
            d["auto_paused"] = True
            d["paused_reason"] = "Remediation attempts exhausted"
            d["paused_at"] = _utcnow().isoformat()
            d["needs_manual_intervention"] = True
            d["escalation_notification_pending"] = True
            d["escalation_notification_retries"] = 2
            d["remediation_attempts"] = 3
    flag_modified(project, "directions")
    await db_session.commit()

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={"status": "active"},
    )
    assert resp.status_code == 200
    d = resp.json()["direction"]
    assert d["status"] == "active"
    assert "auto_paused" not in d
    assert "paused_reason" not in d
    assert "paused_at" not in d
    assert "needs_manual_intervention" not in d
    assert "escalation_notification_pending" not in d
    assert "escalation_notification_retries" not in d
    assert d["remediation_attempts"] == 0


@pytest.mark.asyncio
async def test_operator_reactivation_stamps_grace_blocks_immediate_repause(
    client: AsyncClient, db_session, mocker
):
    """Operator reactivating an auto-paused churn direction stamps grace so the
    frozen unproductive research_history cannot immediately re-pause or
    zombie-retire it on the next lifecycle tick until a new cycle is appended."""
    from sqlalchemy.orm.attributes import flag_modified

    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Churn direction")
    direction_id = direction["id"]

    # Auto-paused (churn-family) with 5 stale cycles, 0 merges — Rule 2 would fire.
    paused_at = _utcnow() - timedelta(days=1)
    stale_history = [
        {
            "timestamp": (paused_at - timedelta(days=i)).isoformat(),
            "tasks_created": 5,
            "tasks_completed": 5,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": 0,
        }
        for i in range(5, 0, -1)
    ]
    project = (
        await db_session.execute(
            select(Project).where(Project.id == uuid.UUID(project_id))
        )
    ).scalar_one()
    for d in project.directions:
        if d["id"] == direction_id:
            d["status"] = "paused"
            d["auto_paused"] = True
            d["paused_reason"] = "Unproductive: 0 PRs merged across 5 cycles"
            d["paused_at"] = paused_at.isoformat()
            d["research_history"] = stale_history
            d["churn_family_pause_count"] = 1
    flag_modified(project, "directions")
    await db_session.commit()

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={"status": "active"},
    )
    assert resp.status_code == 200
    d = resp.json()["direction"]
    assert d["status"] == "active"
    # Grace marker stamped, counter preserved.
    assert d.get("auto_recovered_at") is not None
    assert d.get("churn_family_pause_count") == 1

    # A lifecycle tick must NOT immediately re-pause the just-reactivated
    # direction while the stale history predates the grace stamp.
    project = (
        await db_session.execute(
            select(Project).where(Project.id == uuid.UUID(project_id))
        )
    ).scalar_one()
    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )
    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == direction_id)
    assert d["status"] == "active", (
        "grace should block immediate re-pause of an operator-reactivated direction"
    )
    assert d.get("auto_recovered_at") is not None


@pytest.mark.asyncio
async def test_merge_rate_in_list(client: AsyncClient, db_session, mocker):
    """GET directions returns merges_per_cycle for each direction based on research_history."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Direction with history")
    direction_id = direction["id"]

    # Directly set research_history on the direction in the DB
    from app.models import Project as ProjectModel
    from sqlmodel import select as sa_select

    result = await db_session.execute(
        sa_select(ProjectModel).where(ProjectModel.id == uuid.UUID(project_id))
    )
    project = result.scalars().first()
    updated_directions = list(project.directions or [])
    for d in updated_directions:
        if d["id"] == direction_id:
            d["research_history"] = [{"prs_merged": 1}, {"prs_merged": 0}]
    from sqlalchemy.orm.attributes import flag_modified

    project.directions = updated_directions
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    dirs = resp.json()["directions"]
    target = next(d for d in dirs if d["id"] == direction_id)
    assert "merges_per_cycle" in target
    assert abs(target["merges_per_cycle"] - 0.5) < 0.001  # 1 merged / 2 cycles = 0.5


@pytest.mark.asyncio
async def test_merges_per_cycle_null_when_never_researched(client: AsyncClient, mocker):
    """GET directions returns null merges_per_cycle when direction has never been researched."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(
        client, project_id, text="Never researched direction"
    )
    direction_id = direction["id"]

    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    dirs = resp.json()["directions"]
    target = next(d for d in dirs if d["id"] == direction_id)
    assert "merges_per_cycle" in target
    assert target["merges_per_cycle"] is None


@pytest.mark.asyncio
async def test_generalized_metrics_in_list_use_lifetime_and_legacy_fallback(
    client: AsyncClient, db_session, mocker
):
    """GET directions exposes additive generalized metrics across history + aggregate."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Direction with evals")
    direction_id = direction["id"]

    from app.models import Project as ProjectModel
    from sqlmodel import select as sa_select

    result = await db_session.execute(
        sa_select(ProjectModel).where(ProjectModel.id == uuid.UUID(project_id))
    )
    project = result.scalars().first()
    updated_directions = list(project.directions or [])
    for d in updated_directions:
        if d["id"] == direction_id:
            d["research_history"] = [
                {"prs_merged": 1},
                {
                    "generalized_successes": 3,
                    "tasks_completed": 3,
                    "outcome_kind_breakdown": {
                        "research_quality": {
                            "success": 3,
                            "neutral": 0,
                            "failure": 0,
                        }
                    },
                },
            ]
            d["history_aggregate"] = {
                "total_generalized_successes": 2,
                "total_generalized_terminal_outcomes": 4,
                "outcome_kind_breakdown": {
                    "audit_findings": {"success": 1, "neutral": 0, "failure": 1}
                },
            }
    from sqlalchemy.orm.attributes import flag_modified

    project.directions = updated_directions
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    dirs = resp.json()["directions"]
    target = next(d for d in dirs if d["id"] == direction_id)

    assert target["generalized_successes"] == 6
    assert target["generalized_health_rate"] == 0.75
    assert target["outcome_kind_breakdown"] == {
        "code_merge": {"success": 1, "neutral": 0, "failure": 0},
        "research_quality": {"success": 3, "neutral": 0, "failure": 0},
        "audit_findings": {"success": 1, "neutral": 0, "failure": 1},
        "read_only_unclassified": {"success": 1, "neutral": 1, "failure": 0},
    }


@pytest.mark.asyncio
async def test_generalized_metrics_in_list_merge_legacy_aggregate_breakdown(
    client: AsyncClient, db_session, mocker
):
    """Legacy aggregate merge counts still appear in the breakdown beside live typed outcomes."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(
        client, project_id, text="Direction with legacy agg"
    )
    direction_id = direction["id"]

    from app.models import Project as ProjectModel
    from sqlmodel import select as sa_select

    result = await db_session.execute(
        sa_select(ProjectModel).where(ProjectModel.id == uuid.UUID(project_id))
    )
    project = result.scalars().first()
    updated_directions = list(project.directions or [])
    for d in updated_directions:
        if d["id"] == direction_id:
            d["research_history"] = [
                {
                    "generalized_successes": 3,
                    "tasks_completed": 3,
                    "outcome_kind_breakdown": {
                        "research_quality": {
                            "success": 3,
                            "neutral": 0,
                            "failure": 0,
                        }
                    },
                }
            ]
            d["history_aggregate"] = {
                "total_prs_merged": 2,
                "total_tasks_completed": 2,
                "total_tasks_failed": 1,
            }
    from sqlalchemy.orm.attributes import flag_modified

    project.directions = updated_directions
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    dirs = resp.json()["directions"]
    target = next(d for d in dirs if d["id"] == direction_id)

    assert target["generalized_successes"] == 5
    assert target["generalized_health_rate"] == round(5 / 6, 3)
    assert target["outcome_kind_breakdown"] == {
        "code_merge": {"success": 2, "neutral": 0, "failure": 0},
        "research_quality": {"success": 3, "neutral": 0, "failure": 0},
        "read_only_unclassified": {"success": 0, "neutral": 1, "failure": 0},
    }


@pytest.mark.asyncio
async def test_generalized_metrics_in_list_backfills_sparse_aggregate_breakdown(
    client: AsyncClient, db_session, mocker
):
    """Aggregate generalized totals without typed buckets stay visible in the breakdown."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(
        client, project_id, text="Direction with sparse aggregate evals"
    )
    direction_id = direction["id"]

    from app.models import Project as ProjectModel
    from sqlmodel import select as sa_select

    result = await db_session.execute(
        sa_select(ProjectModel).where(ProjectModel.id == uuid.UUID(project_id))
    )
    project = result.scalars().first()
    updated_directions = list(project.directions or [])
    for d in updated_directions:
        if d["id"] == direction_id:
            d["research_history"] = [
                {
                    "generalized_successes": 1,
                    "tasks_completed": 1,
                    "outcome_kind_breakdown": {
                        "memo_artifact": {
                            "success": 1,
                            "neutral": 0,
                            "failure": 0,
                        }
                    },
                }
            ]
            d["history_aggregate"] = {
                "total_generalized_successes": 4,
                "total_generalized_terminal_outcomes": 6,
            }
    from sqlalchemy.orm.attributes import flag_modified

    project.directions = updated_directions
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    dirs = resp.json()["directions"]
    target = next(d for d in dirs if d["id"] == direction_id)

    assert target["generalized_successes"] == 5
    assert target["generalized_health_rate"] == round(5 / 7, 3)
    assert target["outcome_kind_breakdown"] == {
        "memo_artifact": {"success": 1, "neutral": 0, "failure": 0},
        "read_only_unclassified": {"success": 4, "neutral": 2, "failure": 0},
    }


@pytest.mark.asyncio
async def test_completion_criteria_stored(client: AsyncClient, mocker):
    """POST direction with completion_criteria stores and returns it."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={
            "text": "Direction with criteria",
            "priority": "medium",
            "completion_criteria": "All auth tests pass",
        },
    )
    assert resp.status_code == 200
    d = resp.json()["direction"]
    assert d["completion_criteria"] == "All auth tests pass"

    # Verify it's returned in the GET list too
    get_resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    dirs = get_resp.json()["directions"]
    stored = next(x for x in dirs if x["id"] == d["id"])
    assert stored["completion_criteria"] == "All auth tests pass"


@pytest.mark.asyncio
async def test_completion_criteria_optional(client: AsyncClient, mocker):
    """POST direction without completion_criteria succeeds."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "Direction without criteria", "priority": "low"},
    )
    assert resp.status_code == 200
    d = resp.json()["direction"]
    assert "completion_criteria" not in d or d.get("completion_criteria") is None


@pytest.mark.asyncio
async def test_put_directions_with_valid_completion_criteria(
    client: AsyncClient, mocker
):
    """PUT directions with valid completion_criteria succeeds."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.put(
        f"/api/admin/projects/{project_id}/directions",
        json=[
            {
                "text": "Direction A",
                "priority": "medium",
                "completion_criteria": "All tests pass",
            }
        ],
    )
    assert resp.status_code == 200
    dirs = resp.json()["directions"]
    assert dirs[0]["completion_criteria"] == "All tests pass"


@pytest.mark.asyncio
async def test_put_directions_completion_criteria_too_long(client: AsyncClient, mocker):
    """PUT directions with completion_criteria exceeding 500 chars returns 400."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.put(
        f"/api/admin/projects/{project_id}/directions",
        json=[
            {
                "text": "Direction A",
                "priority": "medium",
                "completion_criteria": "x" * 501,
            }
        ],
    )
    assert resp.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# Auto-completion
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_completion_triggers(db_session, mocker):
    """A saturated direction completes only with challenge-approved success proof."""
    direction_id = "d-autocomp"
    direction = {
        "id": direction_id,
        "text": "Autocompletion target",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "context": {"success_evidence": "The target behavior is verified"},
        "evidence_progress": {"status": "met", "challenge_approved": True},
        "research_history": [
            {
                "timestamp": "2026-01-01T01:00:00",
                "task_id": "t1",
                "tasks_created": 2,
                "prs_merged": 1,
            },
            {
                "timestamp": "2026-01-02T01:00:00",
                "task_id": "t2",
                "tasks_created": 1,
                "prs_merged": 1,
            },
            {
                "timestamp": "2026-01-03T01:00:00",
                "task_id": "t3",
                "tasks_created": 1,
                "prs_merged": 1,
            },
        ],
    }

    project = Project(
        name="Auto-completion Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Research: Autocompletion target",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new=AsyncMock(return_value=("fake-token", "owner", "repo")),
    )
    mocker.patch(
        "app.task_completion.get_branch_sha", new=AsyncMock(return_value="abc123sha")
    )
    mocker.patch("app.task_completion.create_branch", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.research_completion._validate_plan_dependencies", return_value=True
    )
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch(
        "app.research_completion._apply_structural_actionability_downgrade",
        return_value=0,
    )
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "title": "Final improvement",
                "description": "Last polish",
                "prompt": "Do the thing",
                "priority": "medium",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction_id,
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == direction_id)
    assert d["status"] == "completed"
    assert d["auto_completed"] is True
    assert "auto_completed_reason" in d
    assert "3" in d["auto_completed_reason"]  # mentions the PR count

    # Verify an INFO alert was created
    result = await db_session.execute(
        select(Alert).where(
            Alert.project_id == project.id,
            Alert.severity == AlertSeverity.INFO,
        )
    )
    alerts = result.scalars().all()
    assert len(alerts) == 1
    assert "auto-completed" in alerts[0].title.lower()


@pytest.mark.asyncio
async def test_auto_completion_skips_unsaturated(db_session, mocker):
    """Direction stays active when not saturated, even with enough PRs."""
    direction_id = "d-nosat01"
    direction = {
        "id": direction_id,
        "text": "Unsaturated direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [
            {
                "timestamp": "2026-01-01T01:00:00",
                "task_id": "t1",
                "tasks_created": 5,
                "prs_merged": 2,
            },
            {
                "timestamp": "2026-01-02T01:00:00",
                "task_id": "t2",
                "tasks_created": 4,
                "prs_merged": 2,
            },
        ],
    }

    project = Project(
        name="Unsaturated Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Research: Unsaturated direction",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new=AsyncMock(return_value=("fake-token", "owner", "repo")),
    )
    mocker.patch(
        "app.task_completion.get_branch_sha", new=AsyncMock(return_value="abc123sha")
    )
    mocker.patch("app.task_completion.create_branch", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.research_completion._validate_plan_dependencies", return_value=True
    )
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "title": "Active finding",
                "description": "Lots to do",
                "prompt": "Keep going",
                "priority": "medium",
                "scope": "medium",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction_id,
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == direction_id)
    assert d["status"] == "active"
    assert d.get("auto_completed") is not True


@pytest.mark.asyncio
async def test_auto_completion_skips_insufficient_prs(db_session, mocker):
    """Direction stays active when saturated but with fewer than 3 total merged PRs."""
    direction_id = "d-fewprs1"
    direction = {
        "id": direction_id,
        "text": "Few PRs direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        # 3 cycles with <=1 tasks each triggers saturation, but only 2 PRs merged
        "research_history": [
            {
                "timestamp": "2026-01-01T01:00:00",
                "task_id": "t1",
                "tasks_created": 1,
                "prs_merged": 1,
            },
            {
                "timestamp": "2026-01-02T01:00:00",
                "task_id": "t2",
                "tasks_created": 1,
                "prs_merged": 1,
            },
            {
                "timestamp": "2026-01-03T01:00:00",
                "task_id": "t3",
                "tasks_created": 0,
                "prs_merged": 0,
            },
        ],
    }

    project = Project(
        name="Few PRs Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Research: Few PRs direction",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new=AsyncMock(return_value=("fake-token", "owner", "repo")),
    )
    mocker.patch(
        "app.task_completion.get_branch_sha", new=AsyncMock(return_value="abc123sha")
    )
    mocker.patch("app.task_completion.create_branch", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.research_completion._validate_plan_dependencies", return_value=True
    )
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "title": "Small finding",
                "description": "Minor tweak",
                "prompt": "Fix the thing",
                "priority": "low",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction_id,
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == direction_id)
    assert d["status"] == "active"
    assert d.get("auto_completed") is not True


# ─────────────────────────────────────────────────────────────────────────────
# Momentum factor
# ─────────────────────────────────────────────────────────────────────────────


class TestComputeMomentumFactor:
    """Unit tests for _compute_momentum_factor — pure function, no DB needed."""

    def test_momentum_factor_hot_streak(self):
        """Last 2 cycles both productive → 2.0 multiplier."""
        d = {
            "research_history": [
                {"tasks_created": 3},
                {"tasks_created": 2},
            ]
        }
        assert _compute_momentum_factor(d) == 2.0

    def test_momentum_factor_hot_streak_longer_history(self):
        """Hot streak detected even with older stalled entries."""
        d = {
            "research_history": [
                {"tasks_created": 0},
                {"tasks_created": 0},
                {"tasks_created": 5},
                {"tasks_created": 1},
            ]
        }
        assert _compute_momentum_factor(d) == 2.0

    def test_momentum_factor_stalled(self):
        """Last 3 cycles all empty → 0.5 multiplier."""
        d = {
            "research_history": [
                {"tasks_created": 0},
                {"tasks_created": 0},
                {"tasks_created": 0},
            ]
        }
        assert _compute_momentum_factor(d) == 0.5

    def test_momentum_factor_neutral_mixed(self):
        """Mixed history → 1.0 multiplier."""
        d = {
            "research_history": [
                {"tasks_created": 3},
                {"tasks_created": 0},
                {"tasks_created": 2},
            ]
        }
        assert _compute_momentum_factor(d) == 1.0

    def test_momentum_factor_neutral_short_history(self):
        """Only 1 entry — too short for either streak → 1.0."""
        d = {
            "research_history": [
                {"tasks_created": 0},
            ]
        }
        assert _compute_momentum_factor(d) == 1.0

    def test_momentum_factor_empty_history(self):
        """No history at all → 1.0."""
        d = {"research_history": []}
        assert _compute_momentum_factor(d) == 1.0

    def test_momentum_factor_no_history_key(self):
        """Missing research_history key → 1.0."""
        assert _compute_momentum_factor({}) == 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Auto-recovery of auto-paused directions
# ─────────────────────────────────────────────────────────────────────────────


def _make_paused_direction(
    did: str, hours_ago: float, healthy_after_cooldown: bool = True
) -> dict:
    """Build a direction that is auto-paused at the given age."""
    paused_at = (_utcnow() - timedelta(hours=hours_ago)).isoformat()
    history = []
    if not healthy_after_cooldown:
        # Add saturated history so _check_direction_health still returns a reason
        history = [{"tasks_created": 0}] * 5
    return {
        "id": did,
        "text": "Paused direction",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "Saturated: last 5 research cycles produced no actionable findings",
        "paused_at": paused_at,
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "possibly_saturated": not healthy_after_cooldown,
        "research_history": history,
    }


@pytest.mark.asyncio
async def test_auto_recovery_after_cooldown(db_session):
    """Auto-paused direction recovers when health improves after the cooldown."""
    direction = _make_paused_direction(
        "d-recov001", hours_ago=50, healthy_after_cooldown=True
    )
    project = Project(
        name="Auto-Recovery Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    recovered = next(d for d in project.directions if d["id"] == "d-recov001")
    assert recovered["status"] == "active", (
        "Direction should be reactivated after cooldown"
    )
    assert "auto_paused" not in recovered, "auto_paused flag should be removed"
    assert "paused_reason" not in recovered, "paused_reason should be cleared"
    assert "paused_at" not in recovered, "paused_at should be cleared"


@pytest.mark.asyncio
async def test_auto_recovery_blocked_by_needs_manual_intervention(db_session):
    """Direction flagged needs_manual_intervention stays paused even after cooldown expires."""
    direction = _make_paused_direction("d-manint01", hours_ago=50)
    direction["needs_manual_intervention"] = True
    direction["needs_manual_intervention_at"] = (
        _utcnow() - timedelta(hours=50)
    ).isoformat()
    # Simulate a lifetime-burn pause (the only pause type that sets needs_manual_intervention)
    direction["paused_reason"] = (
        "Lifetime burn backstop: 150 tasks finished without merging lifetime, "
        "0 merged (lifetime merge rate 0%)"
    )
    project = Project(
        name="Manual Intervention Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-manint01")
    assert d["status"] == "paused", (
        "Direction with needs_manual_intervention must stay paused after cooldown"
    )
    assert d.get("auto_paused") is True, "auto_paused flag must be preserved"
    assert d.get("needs_manual_intervention") is True, (
        "needs_manual_intervention flag must be preserved"
    )


@pytest.mark.asyncio
async def test_auto_recovery_with_frozen_churn_history(db_session):
    """Churn-paused direction with frozen unhealthy history recovers after cooldown.

    A direction paused by the rolling-window churn guardrail accumulates no new
    cycles while paused, so its history is frozen. Previously, the recovery gate
    re-ran the health check against this frozen history, which always fired the
    same rule → direction was stuck paused forever.

    After the fix, the recovery gate is needs_manual_intervention (only set by
    the lifetime-burn backstop). Windowed-rule pauses (churn, saturation, zero-merge)
    recover after cooldown. A grace period then stamps ``auto_recovered_at`` so
    ``_check_direction_health`` skips the history-based rules until a new research
    cycle is appended after recovery — otherwise the same frozen history would
    immediately re-pause the direction on the active-direction pass (pause-thrash).
    The observable result: the direction returns to ``active`` and stays there at
    auto_pause_count=1, with ``auto_recovered_at`` set, rather than re-pausing to 2.
    """
    # Build the frozen churn history that triggered the pause:
    # 5 cycles x 5 completed tasks, 0 merged -> 25 non-merge finished, merge rate 0%
    # The grace period skips the churn rule until a NEW cycle is appended, so this
    # frozen history must not re-pause the direction on the same lifecycle tick.
    churn_history = [
        {"tasks_created": 5, "tasks_completed": 5, "prs_merged": 0, "tasks_failed": 0}
        for _ in range(5)
    ]
    direction = {
        "id": "d-churn001",
        "text": "Churn-paused direction",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": (
            "Churn guardrail: 25 tasks finished without merging, 0 merged "
            "in last 5 cycles (merge rate 0%)"
        ),
        "paused_at": (_utcnow() - timedelta(hours=50)).isoformat(),
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": churn_history,
        "auto_pause_count": 1,
    }
    project = Project(
        name="Frozen Churn History Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-churn001")
    # Grace-period contract: the direction recovers to active and STAYS active at
    # auto_pause_count=1 — the frozen churn history is ignored until a new cycle is
    # appended post-recovery, so it is not immediately re-paused (no pause-thrash).
    assert d["status"] == "active", (
        "Churn-paused direction should auto-recover to active after cooldown"
    )
    assert d.get("auto_pause_count") == 1, (
        "Direction should stay at original pause count (1) during the grace period, "
        "not re-pause to 2 off the frozen churn history"
    )
    assert d.get("auto_recovered_at"), (
        "auto_recovered_at must be stamped so the grace period skips history-based "
        "health rules until a new cycle is appended"
    )


@pytest.mark.asyncio
async def test_auto_recovery_before_cooldown_stays_paused(db_session):
    """Auto-paused direction is not recovered before the cooldown expires."""
    direction = _make_paused_direction(
        "d-early001", hours_ago=10, healthy_after_cooldown=True
    )
    project = Project(
        name="Early Recovery Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-early001")
    assert d["status"] == "paused", (
        "Direction should stay paused before cooldown expires"
    )


@pytest.mark.asyncio
async def test_user_paused_direction_never_auto_recovered(db_session):
    """Directions paused by the user (auto_paused not set) are never auto-recovered."""
    direction = {
        "id": "d-usrpaus1",
        "text": "User-paused direction",
        "status": "paused",
        # auto_paused intentionally NOT set
        "paused_reason": "User manually paused",
        "paused_at": (_utcnow() - timedelta(hours=100)).isoformat(),
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }
    project = Project(
        name="User Pause Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-usrpaus1")
    assert d["status"] == "paused", "User-paused direction must NOT be auto-recovered"


@pytest.mark.asyncio
async def test_auto_recovery_respects_custom_cooldown(db_session, monkeypatch):
    """direction_auto_recovery_hours runtime config is respected."""
    import app.shared_state as _ss

    monkeypatch.setitem(_ss.runtime_config, "direction_auto_recovery_hours", 100)

    # Paused 50 hours ago — within the custom 100h cooldown
    direction = _make_paused_direction(
        "d-cstmcld1", hours_ago=50, healthy_after_cooldown=True
    )
    project = Project(
        name="Custom Cooldown Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-cstmcld1")
    assert d["status"] == "paused", (
        "Direction should stay paused within the custom 100h cooldown"
    )


@pytest.mark.asyncio
async def test_auto_recovery_starts_remediation_monitoring(db_session):
    """Auto-recovery of a direction with a merged remediation starts an effectiveness window."""
    direction = _make_paused_direction(
        "d-remed001", hours_ago=50, healthy_after_cooldown=True
    )
    direction["last_remediation_result"] = "merged"
    direction["remediation_attempts"] = 1
    direction["error_fingerprint"] = "abc123def456"
    project = Project(
        name="Remediation Monitor Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-remed001")
    assert d["status"] == "active"
    assert d.get("remediation_monitor_started_at"), (
        "monitoring window should be set on recovery"
    )
    # Tracking fields from the prior remediation must be preserved for fingerprint matching
    assert d.get("last_remediation_result") == "merged"
    assert d.get("error_fingerprint") == "abc123def456"


@pytest.mark.asyncio
async def test_re_pause_during_monitoring_flags_manual_intervention(db_session):
    """Re-pause while remediation monitoring is active and attempts ≥ 3 triggers escalation flag."""
    direction = {
        "id": "d-escal001",
        "text": "Repeatedly failing direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [{"tasks_created": 0}] * 5,  # unhealthy → will re-pause
        "possibly_saturated": True,
        "remediation_monitor_started_at": (
            _utcnow() - timedelta(hours=1)
        ).isoformat(),
        "remediation_attempts": 3,
        "last_remediation_result": "merged",
    }
    project = Project(
        name="Escalation Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-escal001")
    assert d["status"] == "paused"
    assert d.get("needs_manual_intervention") is True, (
        "should flag for operator when attempts ≥ 3"
    )


@pytest.mark.asyncio
async def test_re_pause_without_monitoring_does_not_flag_intervention(db_session):
    """Re-pause without active remediation monitoring does not set needs_manual_intervention."""
    direction = {
        "id": "d-nomon001",
        "text": "Unhealthy direction with no remediation history",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [{"tasks_created": 0}] * 5,
        "possibly_saturated": True,
    }
    project = Project(
        name="No Escalation Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-nomon001")
    assert d["status"] == "paused"
    assert "needs_manual_intervention" not in d


@pytest.mark.asyncio
async def test_stuck_manual_intervention_cleared_after_ttl(db_session):
    """needs_manual_intervention auto-clears via TTL when no remediation monitor ever started."""
    flagged_at = (_utcnow() - timedelta(hours=80)).isoformat()
    direction = {
        "id": "d-stuck001",
        "text": "Stuck flagged direction",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "Saturated",
        "paused_at": flagged_at,
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
        "needs_manual_intervention": True,
        "needs_manual_intervention_at": flagged_at,
    }
    project = Project(
        name="Stuck Intervention Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-stuck001")
    assert "needs_manual_intervention" not in d
    assert "needs_manual_intervention_at" not in d


@pytest.mark.asyncio
async def test_stuck_manual_intervention_preserved_when_monitor_active(db_session):
    """TTL clear does not fire when a remediation_monitor_started_at is present."""
    flagged_at = (_utcnow() - timedelta(hours=80)).isoformat()
    direction = {
        "id": "d-stuck002",
        "text": "Flagged with monitor",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "Saturated",
        "paused_at": flagged_at,
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
        "needs_manual_intervention": True,
        "needs_manual_intervention_at": flagged_at,
        "remediation_monitor_started_at": (
            _utcnow() - timedelta(hours=1)
        ).isoformat(),
    }
    project = Project(
        name="Stuck Intervention Monitor Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-stuck002")
    assert d.get("needs_manual_intervention") is True


# ─────────────────────────────────────────────────────────────────────────────
# Diagnosis research on auto-pause
# ─────────────────────────────────────────────────────────────────────────────


def _make_unhealthy_active_direction(did: str, auto_pause_count: int = 0) -> dict:
    """Build an ACTIVE direction whose history trips Rule 2 (zero merge rate).

    5 cycles, all finished, all with 0 prs_merged → _check_direction_health
    returns a pause reason, forcing an auto-pause transition.
    """
    history = [
        {"tasks_created": 1, "tasks_completed": 0, "tasks_failed": 1, "prs_merged": 0}
        for _ in range(5)
    ]
    return {
        "id": did,
        "text": "Direction failing repeatedly",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "auto_pause_count": auto_pause_count,
        "research_history": history,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Direction cap enforcement in autonomous creation paths
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_strategic_batch_create_respects_cap(db_session, mocker):
    """_handle_strategic_completion batch creates should not exceed max_active_directions."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion
    from app.shared_state import runtime_config as _runtime_config

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    # Seed runtime config cap to 3 to make the test fast
    original_cap = _runtime_config.get("max_active_directions")
    _runtime_config["max_active_directions"] = 3

    try:
        # Project already has 3 active directions (at cap)
        existing_dirs = [
            {
                "id": f"d-exist{i}",
                "text": f"Existing {i}",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            }
            for i in range(3)
        ]
        project = Project(
            name="P",
            user_id="u1",
            vision={"purpose": "test", "revision": 1},
            directions=existing_dirs,
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            payloads=[
                {
                    "type": "strategic_review",
                    "prompt": "review",
                    "vision": {"revision": 1},
                }
            ],
            context={},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        # Log output tries to create 2 new directions
        import json

        logs = json.dumps(
            {
                "vision": {"purpose": "test", "revision": 2},
                "direction_operations": [
                    {
                        "op": "create",
                        "text": "New direction A",
                        "priority": "medium",
                        "rationale": "needed",
                    },
                    {
                        "op": "create",
                        "text": "New direction B",
                        "priority": "low",
                        "rationale": "needed",
                    },
                ],
            }
        )

        await _handle_strategic_completion(task, logs, db_session)

        await db_session.refresh(project)
        active_count = sum(
            1 for d in (project.directions or []) if d.get("status") == "active"
        )
        assert active_count <= 3, (
            f"Expected at most 3 active directions, got {active_count}"
        )
    finally:
        if original_cap is None:
            _runtime_config.pop("max_active_directions", None)
        else:
            _runtime_config["max_active_directions"] = original_cap


@pytest.mark.asyncio
async def test_strategic_api_create_direction_respects_cap(db_session, mocker):
    """POST /api/agent/strategic with op=create_direction should skip when cap is reached."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token
    from app.shared_state import runtime_config as _runtime_config

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    original_cap = _runtime_config.get("max_active_directions")
    _runtime_config["max_active_directions"] = 2

    try:
        # Project with 2 active directions (at cap)
        existing_dirs = [
            {
                "id": f"d-sapi{i}",
                "text": f"Strategic API Dir {i}",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            }
            for i in range(2)
        ]
        project = Project(
            name="P-strategic-api",
            user_id="u1",
            vision={"purpose": "test", "revision": 1},
            directions=existing_dirs,
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            user_id="u1",
            task_type="strategic_review",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"command": "review", "vision": {"revision": 1}}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        task_id = str(task.id)

        async def _override_session():
            yield db_session

        app.dependency_overrides[get_session] = _override_session
        app.dependency_overrides[verify_agent_token] = lambda: {
            "sub": task_id,
            "task_type": "strategic_review",
        }

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
            ) as ac:
                resp = await ac.post(
                    "/api/agent/strategic",
                    json={
                        "op": "create_direction",
                        "task_id": task_id,
                        "data": {
                            "text": "Overcap direction",
                            "priority": "medium",
                            "success_evidence": "No overcap errors",
                            "retirement_condition": "Cap resolved",
                        },
                    },
                )
            assert resp.status_code == 409, resp.text
            body = resp.json()
            assert body["status"] == "skipped"
            assert "cap" in body.get("reason", "").lower()
        finally:
            app.dependency_overrides.pop(get_session, None)
            app.dependency_overrides.pop(verify_agent_token, None)

        # Confirm no new direction was added
        await db_session.refresh(project)
        active_count = sum(
            1 for d in (project.directions or []) if d.get("status") == "active"
        )
        assert active_count == 2, f"Expected 2 active directions, got {active_count}"
    finally:
        if original_cap is None:
            _runtime_config.pop("max_active_directions", None)
        else:
            _runtime_config["max_active_directions"] = original_cap


@pytest.mark.asyncio
async def test_strategic_api_create_direction_advisory_on_duplicate(db_session, mocker):
    """POST /api/agent/strategic with op=create_direction CREATES a near-duplicate
    (advisory) rather than hard-blocking it — the strategic agent owns its set, so
    overlap is surfaced via an advisory note, not a 409. (The old 409 "skipped"
    behavior was removed when dedup became advisory for the agent path.)"""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    existing_dirs = [
        {
            "id": "d-dup001",
            "text": "Increase automated test coverage across all service modules",
            "status": "active",
            "priority": "medium",
            "notes": [],
            "research_history": [],
        }
    ]
    project = Project(
        name="P-strategic-dup",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=existing_dirs,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        user_id="u1",
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"command": "review", "vision": {"revision": 1}}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id = str(task.id)

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "create_direction",
                    "task_id": task_id,
                    "data": {
                        "text": "Increase automated test coverage across all service modules",
                        "priority": "high",
                        "success_evidence": "Coverage above 80%",
                        "retirement_condition": "Coverage stable for 2 cycles",
                    },
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["op"] == "create_direction"

        # The near-duplicate is created (advisory), carrying a resemblance note;
        # no 409 / rejection record.
        await db_session.refresh(project)
        new_dirs = [d for d in (project.directions or []) if d.get("id") != "d-dup001"]
        assert len(new_dirs) == 1, "Near-duplicate should be created (advisory)"
        created = new_dirs[0]
        assert created.get("status") == "active"
        notes = created.get("notes") or []
        assert any(
            (n.get("text") or "").startswith("Advisory: resembles existing direction")
            for n in notes
        ), "Created direction should carry an advisory-resemblance note"

        await db_session.refresh(task)
        assert task.context is not None
        assert "_strategic_dedup_rejections" not in task.context
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


@pytest.mark.asyncio
async def test_strategic_api_accumulates_direction_ids(db_session, mocker):
    """create_direction and retire_direction should append IDs to task.context lists."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    existing = [
        {
            "id": "d-retire-me",
            "text": "Old direction",
            "status": "active",
            "priority": "medium",
            "notes": [],
            "research_history": [],
        }
    ]
    project = Project(
        name="P-sapi-ids",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=existing,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        user_id="u1",
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"command": "review", "vision": {"revision": 1}}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = str(task.id)

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            r1 = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "create_direction",
                    "task_id": task_id,
                    "data": {
                        "text": "New direction alpha",
                        "priority": "medium",
                        "success_evidence": "Alpha outcome achieved",
                        "retirement_condition": "Alpha gap closed",
                    },
                },
            )
            assert r1.status_code == 200, r1.text
            new_id_1 = r1.json()["direction_id"]

            r2 = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "create_direction",
                    "task_id": task_id,
                    "data": {
                        "text": "New direction beta",
                        "priority": "low",
                        "success_evidence": "Beta outcome achieved",
                        "retirement_condition": "Beta gap closed",
                    },
                },
            )
            assert r2.status_code == 200, r2.text
            new_id_2 = r2.json()["direction_id"]

            r3 = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "retire_direction",
                    "task_id": task_id,
                    "data": {
                        "direction_id": "d-retire-me",
                        "rationale": "obsolete",
                        "terminal_reason": "invalidated",
                    },
                },
            )
            assert r3.status_code == 200, r3.text

        await db_session.refresh(task)
        ctx = task.context or {}
        assert ctx.get("_strategic_created_ids") == [new_id_1, new_id_2]
        assert ctx.get("_strategic_retired_ids") == ["d-retire-me"]
        assert ctx.get("_strategic_dirs_created") == 2
        assert ctx.get("_strategic_dirs_retired") == 1
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


async def _retire_one_direction(db_session, mocker, active_ids):
    """Seed a project with the given active direction ids, retire the first via
    the strategic API, and return the response JSON."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    existing = [
        {
            "id": did,
            "text": f"Direction {did}",
            "status": "active",
            "priority": "medium",
            "notes": [],
            "research_history": [],
        }
        for did in active_ids
    ]
    project = Project(
        name=f"P-retire-floor-{len(active_ids)}",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=existing,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        user_id="u1",
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"command": "review", "vision": {"revision": 1}}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = str(task.id)

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            r = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "retire_direction",
                    "task_id": task_id,
                    "data": {
                        "direction_id": active_ids[0],
                        "rationale": "obsolete",
                        "terminal_reason": "invalidated",
                    },
                },
            )
        assert r.status_code == 200, r.text
        return r.json()
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


@pytest.mark.asyncio
async def test_retire_direction_does_not_treat_direction_count_as_parallelism_floor(
    db_session, mocker
):
    """Direction retirement never manufactures replacement lanes for capacity."""
    body = await _retire_one_direction(db_session, mocker, ["d-a", "d-b", "d-c"])
    assert body["status"] == "ok"
    warnings = body.get("warnings") or []
    assert not any("parallel" in w.lower() for w in warnings), warnings
    assert not any("replacement direction" in w.lower() for w in warnings), warnings


@pytest.mark.asyncio
async def test_retire_direction_count_never_emits_capacity_warning(db_session, mocker):
    """The same contract holds at larger portfolio sizes."""
    body = await _retire_one_direction(db_session, mocker, ["d-a", "d-b", "d-c", "d-d"])
    assert body["status"] == "ok"
    warnings = body.get("warnings") or []
    assert not any("parallel" in w.lower() for w in warnings), warnings


@pytest.mark.asyncio
async def test_retire_direction_spawns_and_persists_memo_task(db_session, mocker):
    """retire_direction via the strategic API creates and persists a retirement memo task.

    Regression guard for the missing session.commit() after _spawn_retirement_memos.
    Without the commit the memo task is rolled back and never persisted.
    """
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    direction_id = "d-memo-test"
    existing = [
        {
            "id": direction_id,
            "text": "A direction to retire with a memo",
            "status": "active",
            "priority": "medium",
            "notes": [],
            "research_history": [],
            "success_evidence": "Evidence the gap is closed",
            "retirement_condition": "Condition met when gap is closed",
        },
        {
            "id": "d-stay-active",
            "text": "Stays active",
            "status": "active",
            "priority": "medium",
            "notes": [],
            "research_history": [],
        },
        {
            "id": "d-stay-active-2",
            "text": "Stays active 2",
            "status": "active",
            "priority": "medium",
            "notes": [],
            "research_history": [],
        },
        {
            "id": "d-stay-active-3",
            "text": "Stays active 3",
            "status": "active",
            "priority": "medium",
            "notes": [],
            "research_history": [],
        },
    ]
    project = Project(
        name="P-memo-persist",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=existing,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        user_id="u1",
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"command": "review", "vision": {"revision": 1}}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = str(task.id)

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            r = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "retire_direction",
                    "task_id": task_id,
                    "data": {
                        "direction_id": direction_id,
                        "rationale": "Gap is now closed, evidence confirms success.",
                        "terminal_reason": "invalidated",
                    },
                },
            )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"

        # Verify the retirement memo task was committed to the DB
        result = await db_session.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.created_by == "ORCHESTRATOR",
            )
        )
        memo_tasks = result.scalars().all()
        assert memo_tasks, "Expected a retirement memo task to be persisted after retire_direction"
        memo = memo_tasks[0]
        ctx = memo.context or {}
        assert ctx.get("memo_reasoning_kind") == "direction_retirement"
        assert ctx.get("memo_source_direction_id") == direction_id
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


@pytest.mark.asyncio
async def test_retire_direction_reconciles_problem_model_gap(db_session, mocker):
    """Live API retire path reconciles the problem-model gap in the same
    committed transaction: prunes the retired id from gap.active_moves into
    past_moves and records a GapDeferral when it was the gap's last move."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    direction_id = "d-gap-staff"
    project = Project(
        name="P-gap-reconcile",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        problem_model={
            "revision": 1,
            "gaps": [
                {
                    "id": "g-staffed",
                    "name": "staffed gap",
                    "status": "active",
                    "active_moves": [direction_id],
                    "past_moves": [],
                }
            ],
        },
        directions=[
            {
                "id": direction_id,
                "text": "Close the staffed gap",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
                "success_evidence": "Gap closed",
                "retirement_condition": "Gap closed",
            },
            {"id": "d-other", "text": "Other", "status": "active", "notes": []},
            {"id": "d-other-2", "text": "Other 2", "status": "active", "notes": []},
            {"id": "d-other-3", "text": "Other 3", "status": "active", "notes": []},
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        user_id="u1",
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"command": "review", "vision": {"revision": 1}}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = str(task.id)

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test-token"},
        ) as ac:
            r = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "retire_direction",
                    "task_id": task_id,
                    "data": {
                        "direction_id": direction_id,
                        "rationale": "Gap is closed.",
                        "terminal_reason": "invalidated",
                    },
                },
            )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"

        await db_session.refresh(project)
        gap = next(g for g in project.problem_model["gaps"] if g["id"] == "g-staffed")
        assert gap["active_moves"] == []
        assert gap["past_moves"] == [direction_id]
        assert gap.get("deferral")
        assert direction_id in gap["deferral"]["rationale"]
        assert gap["deferral"]["created_at"]
        # Gap is never auto-closed.
        assert gap["status"] == "active"
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


@pytest.mark.asyncio
async def test_strategic_api_create_direction_rejects_missing_structured_fields(
    db_session, mocker
):
    """create_direction returns 400 when success_evidence or retirement_condition is absent."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    project = Project(
        name="P-structured-missing",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        user_id="u1",
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"command": "review", "vision": {"revision": 1}}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = str(task.id)

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            # Missing success_evidence
            resp_no_evidence = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "create_direction",
                    "task_id": task_id,
                    "data": {
                        "text": "Direction without evidence",
                        "priority": "medium",
                        "retirement_condition": "Gap closed",
                    },
                },
            )
            assert resp_no_evidence.status_code == 400, resp_no_evidence.text
            detail_no_ev = (
                resp_no_evidence.json().get("error", {}).get("message", {})
            )
            assert detail_no_ev.get("field") == "success_evidence"

            # Missing retirement_condition
            resp_no_retirement = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "create_direction",
                    "task_id": task_id,
                    "data": {
                        "text": "Direction without retirement",
                        "priority": "medium",
                        "success_evidence": "Outcome achieved",
                    },
                },
            )
            assert resp_no_retirement.status_code == 400, resp_no_retirement.text
            detail_no_ret = (
                resp_no_retirement.json().get("error", {}).get("message", {})
            )
            assert detail_no_ret.get("field") == "retirement_condition"

        # Neither direction should have been created
        await db_session.refresh(project)
        assert len(project.directions or []) == 0, (
            "No directions should be created without required fields"
        )
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


# ─────────────────────────────────────────────────────────────────────────────
# Strategic API — new problem_model / running_update / reflect / pending_confirm ops
# ─────────────────────────────────────────────────────────────────────────────


async def _make_strategic_project_and_task(db_session):
    """Shared setup: one project + one strategic_review task, returns (project, task_id)."""
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    project = Project(name="P-strategic-new", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        user_id="u1",
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"command": "review", "vision": {"revision": 1}}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = str(task.id)

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }
    return project, task_id


def _cleanup_strategic_overrides():
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(verify_agent_token, None)


@pytest.mark.asyncio
async def test_strategic_api_set_problem_model_happy_path(db_session):
    """op=set_problem_model validates, bumps revision, persists."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_problem_model",
                    "task_id": task_id,
                    "data": {
                        "intent": {
                            "purpose": "Build an autonomous dev studio.",
                            "current_focus": "Stabilize the review loop.",
                        },
                        "gaps": [
                            {
                                "id": "g-mem",
                                "name": "No cross-cycle memory",
                                "size": "large",
                                "priority": "high",
                                "status": "active",
                            },
                        ],
                    },
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["op"] == "set_problem_model"
        assert body["revision"] == 1

        await db_session.refresh(project)
        pm = project.problem_model
        assert pm["intent"]["purpose"] == "Build an autonomous dev studio."
        assert len(pm["gaps"]) == 1
        assert pm["gaps"][0]["id"] == "g-mem"
        assert pm["revision"] == 1
    finally:
        _cleanup_strategic_overrides()


def _operator_seeded_pm() -> dict:
    """A problem_model as the onboarding seed leaves it: operator purpose with a
    user_message source, operator context/constraints, last_updated_by=user."""
    return {
        "revision": 1,
        "last_updated_by": "user",
        "intent": {
            "purpose": "Ship reliable billing.",
            "current_focus": "Phase 1.",
            "source": {"type": "user_message", "message_id": "m-1"},
        },
        "context": {"project_type": "startup", "users": "SMBs"},
        "constraints": ["No PII in logs"],
    }


async def _attach_cycle_with_problem_model_baseline(
    db_session, project, task_id, *, baseline_pm, current_pm
):
    """Snapshot ``baseline_pm`` as the cycle's problem_model baseline, link the
    fixture task to the cycle, and set ``current_pm`` as the live DB model.
    Mirrors :func:`_attach_cycle_with_running_update_baseline`."""
    from app import strategic_cycle

    cycle = await strategic_cycle.create(
        db_session,
        project_id=project.id,
        baseline_problem_model=baseline_pm,
    )
    await db_session.commit()

    project.problem_model = current_pm
    db_session.add(project)

    task = await db_session.get(Task, uuid.UUID(task_id))
    task.strategic_cycle_id = cycle.id
    task.context = {**(task.context or {}), "strategic_cycle_id": cycle.id}
    db_session.add(task)
    await db_session.commit()
    return cycle.id


@pytest.mark.asyncio
async def test_set_problem_model_double_write_same_cycle_keeps_second_values(
    db_session,
):
    """Case 1: two set_problem_model writes in one cycle with NO operator edit
    between — the SECOND write's distinct context/constraints persist (not
    dropped back to the first write) and NO false operator-conflict is recorded.
    This is the bug: the 2nd write's ancestor must be the agent's own 1st write,
    not the cycle-start snapshot."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        baseline = _operator_seeded_pm()
        await _attach_cycle_with_problem_model_baseline(
            db_session,
            project,
            task_id,
            baseline_pm=baseline,
            current_pm=dict(baseline),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            r1 = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_problem_model",
                    "task_id": task_id,
                    "data": {
                        "intent": {
                            "purpose": "Ship reliable billing.",
                            "current_focus": "Phase 1.",
                        },
                        "context": {"project_type": "scale-up", "users": "SMBs"},
                        "constraints": ["No PII in logs", "SOC2"],
                    },
                },
            )
            assert r1.status_code == 200, r1.text
            r2 = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_problem_model",
                    "task_id": task_id,
                    "data": {
                        "intent": {
                            "purpose": "Ship reliable billing.",
                            "current_focus": "Phase 2.",
                        },
                        "context": {
                            "project_type": "enterprise",
                            "users": "Fortune 500",
                        },
                        "constraints": ["No PII in logs", "SOC2", "HIPAA"],
                    },
                },
            )
            assert r2.status_code == 200, r2.text
        await db_session.refresh(project)
        pm = project.problem_model
        # The SECOND write's values win — not silently reverted to the first.
        assert pm["context"]["project_type"] == "enterprise"
        assert pm["context"]["users"] == "Fortune 500"
        assert pm["constraints"] == ["No PII in logs", "SOC2", "HIPAA"]
        # No fabricated operator-conflict in the ledger.
        assert not pm.get("rejected_agent_edits")
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_set_problem_model_operator_edit_between_writes_still_wins(db_session):
    """Case 3: an operator edits problem_model BETWEEN the agent's two writes.
    The operator's mid-cycle value must still win on the 2nd write (the agent's
    own prior-proposal ancestor does not re-open the operator's field), and the
    agent's overridden value is recorded to the ledger."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        baseline = _operator_seeded_pm()
        await _attach_cycle_with_problem_model_baseline(
            db_session,
            project,
            task_id,
            baseline_pm=baseline,
            current_pm=dict(baseline),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            r1 = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_problem_model",
                    "task_id": task_id,
                    "data": {
                        "intent": {
                            "purpose": "Ship reliable billing.",
                            "current_focus": "Phase 1.",
                        },
                        "context": {"project_type": "scale-up", "users": "SMBs"},
                        "constraints": ["No PII in logs", "SOC2"],
                    },
                },
            )
            assert r1.status_code == 200, r1.text

            # Operator edits problem_model mid-cycle (admin patch stamps user).
            await db_session.refresh(project)
            edited = dict(project.problem_model)
            edited["context"] = {"project_type": "regulated-fintech"}
            edited["constraints"] = ["No PII in logs", "PCI-DSS"]
            edited["last_updated_by"] = "user"
            project.problem_model = edited
            db_session.add(project)
            await db_session.commit()

            r2 = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_problem_model",
                    "task_id": task_id,
                    "data": {
                        "intent": {
                            "purpose": "Ship reliable billing.",
                            "current_focus": "Phase 2.",
                        },
                        "context": {"project_type": "enterprise"},
                        "constraints": ["Move fast"],
                    },
                },
            )
            assert r2.status_code == 200, r2.text
        await db_session.refresh(project)
        pm = project.problem_model
        # Operator's mid-cycle edit survives the agent's 2nd write.
        assert pm["context"]["project_type"] == "regulated-fintech"
        assert pm["constraints"] == ["No PII in logs", "PCI-DSS"]
        fields = {e["field"] for e in (pm.get("rejected_agent_edits") or [])}
        assert fields == {"context", "constraints"}
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_problem_model_rejects_malformed(db_session):
    """Invalid schema fields return 400, don't mutate state."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            # size="enormous" is not a valid GapSize literal
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_problem_model",
                    "task_id": task_id,
                    "data": {
                        "gaps": [{"id": "g-bad", "name": "x", "size": "enormous"}]
                    },
                },
            )
        assert resp.status_code == 400
        body = resp.json()
        msg = body.get("detail") or body.get("error", {}).get("message", "")
        assert "Invalid problem_model" in msg

        await db_session.refresh(project)
        # Model unchanged (still empty dict)
        assert project.problem_model in (None, {}, {"revision": 0})
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_problem_model_accepts_capability_backing_paths(
    db_session,
):
    """The richer capability shape (with backing_paths) validates cleanly and
    persists; transient sibling fields are still stripped from the durable
    model."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_problem_model",
                    "task_id": task_id,
                    "data": {
                        "capabilities": [
                            {
                                "name": "Auth",
                                "status": "working",
                                "backing_paths": [
                                    "backend/app/auth.py",
                                    "backend/app/models.py",
                                ],
                            },
                            # Old-shape capability (no backing_paths) in the
                            # same write must still validate.
                            {"name": "Legacy", "status": "partial"},
                        ],
                        # Transient per-cycle siblings — must be stripped, not
                        # persisted, and must not cause a 400.
                        "version_phase_decision": {"phase": "stabilize"},
                        "qualify_request": {"reason": "ready"},
                    },
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["op"] == "set_problem_model"
        assert body["revision"] == 1

        await db_session.refresh(project)
        pm = project.problem_model
        caps = pm["capabilities"]
        assert caps[0]["backing_paths"] == [
            "backend/app/auth.py",
            "backend/app/models.py",
        ]
        # Old-shape capability defaulted to empty list.
        assert caps[1]["backing_paths"] == []
        # Transient siblings never leak into the durable model.
        assert "version_phase_decision" not in pm
        assert "qualify_request" not in pm
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_problem_model_accepts_old_shape_capability(db_session):
    """A write with only old-shape capabilities (predating backing_paths)
    still validates — full backward compatibility for stored agent payloads."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_problem_model",
                    "task_id": task_id,
                    "data": {
                        "capabilities": [
                            {"name": "CapA", "status": "working"},
                            {"name": "CapB", "status": "planned"},
                        ]
                    },
                },
            )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(project)
        pm = project.problem_model
        assert all(c["backing_paths"] == [] for c in pm["capabilities"])
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_running_update(db_session):
    """op=set_running_update with {markdown: str} replaces project.running_update."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            doc = (
                "## What's going on\nShipping problem_model v1.\n\n## Next\nMerge #103."
            )
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_running_update",
                    "task_id": task_id,
                    "data": {"markdown": doc},
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["op"] == "set_running_update"
        assert body["length"] == len(doc)

        await db_session.refresh(project)
        assert project.running_update == doc
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_running_update_requires_markdown_field(db_session):
    """Missing 'markdown' key (or wrong shape) returns 400."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    _project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_running_update",
                    "task_id": task_id,
                    "data": {"other_field": "nope"},
                },
            )
        assert resp.status_code == 400
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_running_update_replaces(db_session):
    """Second call replaces the first — it's set, not append."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_running_update",
                    "task_id": task_id,
                    "data": {"markdown": "first"},
                },
            )
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_running_update",
                    "task_id": task_id,
                    "data": {"markdown": "second"},
                },
            )
        assert resp.status_code == 200
        await db_session.refresh(project)
        assert project.running_update == "second"
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_running_update_rejects_over_limit(db_session):
    """set_running_update rejects content exceeding 100KB limit."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            # Use size that exceeds 100KB to trigger the payload size check
            oversized = "x" * 101_000
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_running_update",
                    "task_id": task_id,
                    "data": {"markdown": oversized},
                },
            )
        assert resp.status_code == 400
        body = resp.json()
        msg = body.get("error", {}).get("message", "")
        assert "too large" in msg or "exceeds" in msg or "character limit" in msg
        # Verify nothing was set
        await db_session.refresh(project)
        assert project.running_update is None or project.running_update == ""
    finally:
        _cleanup_strategic_overrides()


async def _attach_cycle_with_running_update_baseline(
    db_session, project, task_id, *, baseline, current
):
    """Snapshot ``baseline`` as the cycle's running_update baseline, link the
    fixture's task to the cycle, and set ``current`` as the live DB document
    (simulating a mid-cycle operator/digest edit). Returns the cycle id."""
    from app import strategic_cycle

    cycle = await strategic_cycle.create(
        db_session,
        project_id=project.id,
        baseline_running_update=baseline,
    )
    await db_session.commit()

    project.running_update = current
    db_session.add(project)

    task = await db_session.get(Task, uuid.UUID(task_id))
    task.strategic_cycle_id = cycle.id
    task.context = {**(task.context or {}), "strategic_cycle_id": cycle.id}
    db_session.add(task)
    await db_session.commit()
    return cycle.id


@pytest.mark.asyncio
async def test_strategic_api_set_running_update_agent_wins_when_untouched(db_session):
    """Live op: when nothing changed running_update since the cycle baseline,
    the agent's refreshed document is taken verbatim."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        await _attach_cycle_with_running_update_baseline(
            db_session, project, task_id, baseline="BASELINE", current="BASELINE"
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_running_update",
                    "task_id": task_id,
                    "data": {"markdown": "AGENT REFRESHED DOC"},
                },
            )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(project)
        assert project.running_update == "AGENT REFRESHED DOC"
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_running_update_operator_edit_preserved(db_session):
    """Live op: a concurrent operator body edit survives (operator wins) and the
    agent's superseded document is recorded on the vision rejected-edits ledger."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.vision_merge import REJECTED_EDITS_KEY

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        await _attach_cycle_with_running_update_baseline(
            db_session,
            project,
            task_id,
            baseline="BASELINE",
            current="OPERATOR EDITED BODY",
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_running_update",
                    "task_id": task_id,
                    "data": {"markdown": "AGENT REFRESHED DOC"},
                },
            )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(project)
        assert project.running_update == "OPERATOR EDITED BODY"
        ledger = (project.vision or {}).get(REJECTED_EDITS_KEY) or []
        assert any(
            e.get("field") == "running_update"
            and e.get("agent_value") == "AGENT REFRESHED DOC"
            for e in ledger
        ), f"Expected the agent's superseded doc on the ledger; got {ledger}"
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_running_update_digest_reintegrated(db_session):
    """Live op: a mid-cycle routine-event digest append does NOT discard the
    agent's rewrite — the agent's document lands and the digest line is
    re-integrated onto it (not frozen at the stale baseline)."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    digest_line = "- [2026-06-03 12:00] **pr_merged**: Merged PR #103"
    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        await _attach_cycle_with_running_update_baseline(
            db_session,
            project,
            task_id,
            baseline="BASELINE",
            current=f"BASELINE\n{digest_line}",
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_running_update",
                    "task_id": task_id,
                    "data": {"markdown": "AGENT REFRESHED DOC"},
                },
            )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(project)
        assert project.running_update == f"AGENT REFRESHED DOC\n{digest_line}"
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_running_update_twice_in_one_cycle_both_persist(
    db_session,
):
    """Live op: two set_running_update calls in one cycle BOTH persist. The
    baseline advances after the first write, so the second write is recognized
    as the agent's own refinement and is NOT misclassified as an operator edit /
    recorded as superseded. Regression: pre-fix the second write was dropped."""
    from httpx import AsyncClient, ASGITransport
    from app import strategic_cycle
    from app.main import app
    from app.running_update_merge import RUNNING_UPDATE_REJECTED_KIND
    from app.vision_merge import REJECTED_EDITS_KEY

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        cycle_id = await _attach_cycle_with_running_update_baseline(
            db_session, project, task_id, baseline="BASELINE", current="BASELINE"
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp1 = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_running_update",
                    "task_id": task_id,
                    "data": {"markdown": "AGENT DOC A"},
                },
            )
            assert resp1.status_code == 200, resp1.text
            await db_session.refresh(project)
            assert project.running_update == "AGENT DOC A"
            # Baseline advanced to the first persisted document.
            cycle = await strategic_cycle.get(db_session, cycle_id)
            assert strategic_cycle.baseline_running_update(cycle) == "AGENT DOC A"

            resp2 = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_running_update",
                    "task_id": task_id,
                    "data": {"markdown": "AGENT DOC B"},
                },
            )
            assert resp2.status_code == 200, resp2.text

        await db_session.refresh(project)
        assert project.running_update == "AGENT DOC B"
        ledger = (project.vision or {}).get(REJECTED_EDITS_KEY) or []
        assert not any(e.get("kind") == RUNNING_UPDATE_REJECTED_KIND for e in ledger), (
            f"Second same-cycle write must not be recorded superseded; got {ledger}"
        )
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_running_update_operator_edit_between_writes_wins(
    db_session,
):
    """Live op: a genuine operator body edit made BETWEEN two agent writes still
    wins — it diverges from the advanced baseline, so the second agent write is
    preserved operator-wins and the agent's doc is recorded as superseded."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.running_update_merge import RUNNING_UPDATE_REJECTED_KIND
    from app.vision_merge import REJECTED_EDITS_KEY

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        await _attach_cycle_with_running_update_baseline(
            db_session, project, task_id, baseline="BASELINE", current="BASELINE"
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp1 = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_running_update",
                    "task_id": task_id,
                    "data": {"markdown": "AGENT DOC A"},
                },
            )
            assert resp1.status_code == 200, resp1.text
            await db_session.refresh(project)
            assert project.running_update == "AGENT DOC A"

            # Operator hand-edits the body between the two agent writes.
            project.running_update = "OPERATOR HAND EDIT"
            db_session.add(project)
            await db_session.commit()

            resp2 = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_running_update",
                    "task_id": task_id,
                    "data": {"markdown": "AGENT DOC B"},
                },
            )
            assert resp2.status_code == 200, resp2.text

        await db_session.refresh(project)
        assert project.running_update == "OPERATOR HAND EDIT"
        ledger = (project.vision or {}).get(REJECTED_EDITS_KEY) or []
        assert any(
            e.get("kind") == RUNNING_UPDATE_REJECTED_KIND
            and e.get("agent_value") == "AGENT DOC B"
            for e in ledger
        ), f"Expected agent doc B recorded operator-superseded; got {ledger}"
    finally:
        _cleanup_strategic_overrides()


async def _attach_cycle_with_vision_baseline(
    db_session, project, task_id, *, baseline, current
):
    """Snapshot ``baseline`` as the cycle's vision baseline, link the fixture's
    task to the cycle, and set ``current`` as the live DB vision (simulating a
    mid-cycle operator edit). Returns the cycle id. Mirrors
    :func:`_attach_cycle_with_running_update_baseline`."""
    from app import strategic_cycle

    cycle = await strategic_cycle.create(
        db_session,
        project_id=project.id,
        baseline_vision=baseline,
    )
    await db_session.commit()

    project.vision = current
    db_session.add(project)

    task = await db_session.get(Task, uuid.UUID(task_id))
    task.strategic_cycle_id = cycle.id
    task.context = {**(task.context or {}), "strategic_cycle_id": cycle.id}
    db_session.add(task)
    await db_session.commit()
    return cycle.id


@pytest.mark.asyncio
async def test_strategic_api_set_vision_operator_edit_preserved(db_session):
    """Live op: a concurrent operator edit to a vision field (purpose) survives
    (operator wins) and the agent's superseded value lands on the rejected-edits
    ledger. Regression: pre-fix the live set_vision blind-assigned and silently
    clobbered the operator's edit."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.vision_merge import REJECTED_EDITS_KEY

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        baseline = {"revision": 1, "purpose": "Original purpose."}
        current = {"revision": 1, "purpose": "OPERATOR EDITED PURPOSE."}
        await _attach_cycle_with_vision_baseline(
            db_session, project, task_id, baseline=baseline, current=current
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_vision",
                    "task_id": task_id,
                    "data": {"purpose": "AGENT REWRITTEN PURPOSE."},
                },
            )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(project)
        # Operator's mid-cycle edit wins on the persisted vision.
        assert project.vision["purpose"] == "OPERATOR EDITED PURPOSE."
        # Agent's superseded value is recorded for next cycle.
        ledger = (project.vision or {}).get(REJECTED_EDITS_KEY) or []
        assert any(
            e.get("agent_value") == "AGENT REWRITTEN PURPOSE." for e in ledger
        ), f"Expected the agent's superseded purpose on the ledger; got {ledger}"
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_vision_agent_wins_when_untouched(db_session):
    """Live op: when the operator did not touch vision since the cycle baseline,
    the agent's rewritten field is taken (no false operator-conflict)."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.vision_merge import REJECTED_EDITS_KEY

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        baseline = {"revision": 1, "purpose": "Original purpose."}
        await _attach_cycle_with_vision_baseline(
            db_session, project, task_id, baseline=baseline, current=dict(baseline)
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_vision",
                    "task_id": task_id,
                    "data": {"purpose": "AGENT REWRITTEN PURPOSE."},
                },
            )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(project)
        assert project.vision["purpose"] == "AGENT REWRITTEN PURPOSE."
        assert not (project.vision or {}).get(REJECTED_EDITS_KEY)
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_vision_legacy_no_cycle_blind_assign(db_session):
    """Live op: with NO cycle attached (legacy task) the op degrades to today's
    direct assignment — the agent's vision is taken verbatim, revision bumps, and
    internal tracking fields are preserved."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        project.vision = {
            "revision": 3,
            "purpose": "Old purpose.",
            "_strategic_failures": 2,
            "_keep_me": "internal",
        }
        db_session.add(project)
        await db_session.commit()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_vision",
                    "task_id": task_id,
                    "data": {"purpose": "AGENT NEW PURPOSE."},
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["revision"] == 4
        await db_session.refresh(project)
        assert project.vision["purpose"] == "AGENT NEW PURPOSE."
        # Internal tracking preserved; failure counter reset on success.
        assert project.vision["_keep_me"] == "internal"
        assert "_strategic_failures" not in project.vision
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_vision_second_write_advances_own_proposal(db_session):
    """Live op: two set_vision writes in one cycle with NO operator edit between —
    the SECOND write's value persists (advances the agent's own proposal) and is
    NOT misread as a mid-cycle operator edit / recorded as superseded."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.vision_merge import REJECTED_EDITS_KEY

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        baseline = {"revision": 1, "purpose": "Original purpose."}
        await _attach_cycle_with_vision_baseline(
            db_session, project, task_id, baseline=baseline, current=dict(baseline)
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            r1 = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_vision",
                    "task_id": task_id,
                    "data": {"purpose": "AGENT PURPOSE A."},
                },
            )
            assert r1.status_code == 200, r1.text
            r2 = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_vision",
                    "task_id": task_id,
                    "data": {"purpose": "AGENT PURPOSE B."},
                },
            )
            assert r2.status_code == 200, r2.text
        await db_session.refresh(project)
        assert project.vision["purpose"] == "AGENT PURPOSE B."
        assert not (project.vision or {}).get(REJECTED_EDITS_KEY)
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_create_message_rejects_over_limit(db_session):
    """create_message rejects content exceeding limit."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.strategic_constants import STRATEGIC_MESSAGE_CONTENT_MAX
    from app.models import ProjectMessage
    from sqlmodel import select as sa_select

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            oversized = "x" * (STRATEGIC_MESSAGE_CONTENT_MAX + 1)
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "create_message",
                    "task_id": task_id,
                    "data": {"content": oversized, "type": "NOTE"},
                },
            )
        assert resp.status_code == 400
        body = resp.json()
        msg = body.get("error", {}).get("message", "")
        assert "exceeds" in msg or "character limit" in msg
        # Verify no message was created
        await db_session.refresh(project)
        messages = await db_session.execute(
            sa_select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
        assert len(messages.scalars().all()) == 0
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_reflect(db_session):
    """op=reflect appends to project.self_reflection_log."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "reflect",
                    "task_id": task_id,
                    "data": {
                        "what_was_hard": "Couldn't tell two near-duplicate directions apart.",
                        "missing_context": "Direction outcome doesn't include user rejection reason.",
                        "proposed_self_improvement": "Add rejection_reason to direction outcomes.",
                    },
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["op"] == "reflect"
        assert body["log_size"] == 1

        await db_session.refresh(project)
        assert len(project.self_reflection_log) == 1
        entry = project.self_reflection_log[0]
        assert entry["what_was_hard"].startswith("Couldn't tell")
        assert entry["cycle_task_id"] == task_id
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_memory(db_session):
    """op=set_memory writes a strategic_memory KnowledgeDocument (sole write path)."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.knowledge_query import list_knowledge_documents

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_memory",
                    "task_id": task_id,
                    "data": {"note": "Always verify claims before escalating them."},
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["op"] == "set_memory"
        assert body["source_kind"] == "strategic_memory"
        assert body["entry_count"] == 1
        assert body["doc_id"]

        # The KnowledgeDocument row is the store; the legacy JSONB column is
        # frozen (dual-write leg cut).
        listed = await list_knowledge_documents(
            db_session, project.id, source_kind="strategic_memory"
        )
        assert listed["total"] == 1
        assert listed["documents"][0]["id"] == body["doc_id"]

        await db_session.refresh(project)
        assert (project.strategic_memory_notes or []) == []
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_memory_rejects_empty_note(db_session):
    """set_memory with missing/empty/whitespace note returns 400."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    _project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            # missing "note" key
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_memory",
                    "task_id": task_id,
                    "data": {"other_field": "nope"},
                },
            )
        assert resp.status_code == 400, resp.text

        # empty string note
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac2:
            resp2 = await ac2.post(
                "/api/agent/strategic",
                json={
                    "op": "set_memory",
                    "task_id": task_id,
                    "data": {"note": ""},
                },
            )
        assert resp2.status_code == 400, resp2.text

        # whitespace-only note
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac3:
            resp3 = await ac3.post(
                "/api/agent/strategic",
                json={
                    "op": "set_memory",
                    "task_id": task_id,
                    "data": {"note": "   "},
                },
            )
        assert resp3.status_code == 400, resp3.text

        # non-dict data
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac4:
            resp4 = await ac4.post(
                "/api/agent/strategic",
                json={
                    "op": "set_memory",
                    "task_id": task_id,
                    "data": "just a string",
                },
            )
        assert resp4.status_code == 400, resp4.text
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_memory_leaves_legacy_jsonb_frozen(db_session):
    """The JSONB dual-write leg is cut: a pre-existing legacy list is untouched.

    The former FIFO-cap-at-100 behavior no longer applies — the JSONB column is
    retained for rollback safety but no longer receives writes. New facts land
    only as KnowledgeDocument rows.
    """
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        preexisting = [
            {"note": f"entry {i}", "set_at": "2026-01-01T00:00:00"} for i in range(3)
        ]
        project.strategic_memory_notes = list(preexisting)
        from sqlalchemy.orm.attributes import flag_modified as _fm
        _fm(project, "strategic_memory_notes")
        db_session.add(project)
        await db_session.commit()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_memory",
                    "task_id": task_id,
                    "data": {"note": "newest note"},
                },
            )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(project)
        # The legacy JSONB list is frozen — no append, no eviction.
        assert project.strategic_memory_notes == preexisting
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_session_summary(db_session):
    """op=set_session_summary stores summary in task.context."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        # Load task to verify context update
        task = (
            await db_session.execute(select(Task).where(Task.id == uuid.UUID(task_id)))
        ).scalar_one()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_session_summary",
                    "task_id": task_id,
                    "data": {"summary": "Research revealed three key gap areas to investigate."},
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["op"] == "set_session_summary"
        assert body["status"] == "ok"

        # Verify task context was updated
        await db_session.refresh(task)
        assert task.context is not None
        assert task.context["_session_summary"] == "Research revealed three key gap areas to investigate."
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_session_summary_rejects_invalid_data(db_session):
    """set_session_summary with invalid data returns 400."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    _project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            # non-dict data
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "set_session_summary",
                    "task_id": task_id,
                    "data": "just a string",
                },
            )
        assert resp.status_code == 400, resp.text

        # missing "summary" key
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac2:
            resp2 = await ac2.post(
                "/api/agent/strategic",
                json={
                    "op": "set_session_summary",
                    "task_id": task_id,
                    "data": {"other_field": "nope"},
                },
            )
        assert resp2.status_code == 400, resp2.text

        # empty string summary
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac3:
            resp3 = await ac3.post(
                "/api/agent/strategic",
                json={
                    "op": "set_session_summary",
                    "task_id": task_id,
                    "data": {"summary": ""},
                },
            )
        assert resp3.status_code == 400, resp3.text

        # whitespace-only summary
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac4:
            resp4 = await ac4.post(
                "/api/agent/strategic",
                json={
                    "op": "set_session_summary",
                    "task_id": task_id,
                    "data": {"summary": "   "},
                },
            )
        assert resp4.status_code == 400, resp4.text
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_pending_confirm(db_session):
    """op=pending_confirm adds a pending_user_confirmation item to problem_model."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "pending_confirm",
                    "task_id": task_id,
                    "data": {
                        "type": "credentials_needed",
                        "proposal": "Need OpenAI key with higher rate limit.",
                        "rationale": "Strategic cycles hitting 429s consistently.",
                    },
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["op"] == "pending_confirm"
        assert body["id"].startswith("p-")

        await db_session.refresh(project)
        pm = project.problem_model
        pending = pm.get("pending_user_confirmation", [])
        assert len(pending) == 1
        item = pending[0]
        assert item["type"] == "credentials_needed"
        assert item["proposal"].startswith("Need OpenAI")
        assert item["id"] == body["id"]
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_pending_confirm_rejects_invalid_type(db_session):
    """Invalid pending_confirmation type returns 400."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    _project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "pending_confirm",
                    "task_id": task_id,
                    "data": {"type": "some_made_up_type", "proposal": "x"},
                },
            )
        assert resp.status_code == 400
        body = resp.json()
        msg = body.get("detail") or body.get("error", {}).get("message", "")
        assert "Invalid pending_confirmation" in msg
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_approve_direction_flips_proposed_to_active(
    db_session, mocker
):
    """POST /api/agent/strategic with op=approve_direction promotes a proposal to active."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    existing_dirs = [
        {
            "id": "d-act01",
            "text": "Existing active",
            "status": "active",
            "priority": "medium",
            "notes": [],
            "research_history": [],
        },
        {
            "id": "d-prop01",
            "text": "Proposed from research",
            "status": "proposed",
            "priority": "high",
            "proposed_by": "some-task-id",
            "proposal_rationale": "research found gap",
            "notes": [],
            "research_history": [],
        },
    ]
    project = Project(
        name="P-strategic-approve",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=existing_dirs,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        user_id="u1",
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"command": "review"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id = str(task.id)

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "approve_direction",
                    "task_id": task_id,
                    "data": {
                        "direction_id": "d-prop01",
                        "rationale": "fits current focus",
                    },
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["op"] == "approve_direction"
        assert body["direction_id"] == "d-prop01"

        await db_session.refresh(project)
        approved = next(d for d in project.directions if d["id"] == "d-prop01")
        assert approved["status"] == "active"
        assert "activated_at" in approved
        assert any(
            "Approved by strategic review" in (n.get("text") or "")
            for n in approved.get("notes", [])
        )

        await db_session.refresh(task)
        assert task.context.get("_strategic_proposals_approved") == 1
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


@pytest.mark.asyncio
async def test_strategic_api_approve_direction_respects_cap(db_session, mocker):
    """POST /api/agent/strategic approve_direction should 409 when direction cap reached."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token
    from app.shared_state import runtime_config as _runtime_config

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    original_cap = _runtime_config.get("max_active_directions")
    _runtime_config["max_active_directions"] = 2
    try:
        existing_dirs = [
            {
                "id": f"d-act{i}",
                "text": f"Active {i}",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            }
            for i in range(2)
        ] + [
            {
                "id": "d-pend01",
                "text": "Pending proposal",
                "status": "proposed",
                "priority": "high",
                "proposed_by": "t-x",
                "notes": [],
                "research_history": [],
            },
        ]
        project = Project(
            name="P-strategic-approve-cap",
            user_id="u1",
            vision={"purpose": "test", "revision": 1},
            directions=existing_dirs,
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            user_id="u1",
            task_type="strategic_review",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"command": "review"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        task_id = str(task.id)

        async def _override_session():
            yield db_session

        app.dependency_overrides[get_session] = _override_session
        app.dependency_overrides[verify_agent_token] = lambda: {
            "sub": task_id,
            "task_type": "strategic_review",
        }

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
            ) as ac:
                resp = await ac.post(
                    "/api/agent/strategic",
                    json={
                        "op": "approve_direction",
                        "task_id": task_id,
                        "data": {"direction_id": "d-pend01", "rationale": "x"},
                    },
                )
            assert resp.status_code == 409, resp.text
            body = resp.json()
            assert body["status"] == "skipped"
            assert "cap" in body.get("reason", "").lower()

            await db_session.refresh(project)
            still_proposed = next(
                d for d in project.directions if d["id"] == "d-pend01"
            )
            assert still_proposed["status"] == "proposed", (
                "Proposal should stay proposed when cap is reached"
            )
        finally:
            app.dependency_overrides.pop(get_session, None)
            app.dependency_overrides.pop(verify_agent_token, None)
    finally:
        if original_cap is None:
            _runtime_config.pop("max_active_directions", None)
        else:
            _runtime_config["max_active_directions"] = original_cap


@pytest.mark.asyncio
async def test_strategic_api_approve_direction_rejects_non_proposed(db_session, mocker):
    """approve_direction should 409 when target direction is not in 'proposed' status."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    existing_dirs = [
        {
            "id": "d-act01",
            "text": "Already active",
            "status": "active",
            "priority": "medium",
            "notes": [],
            "research_history": [],
        },
    ]
    project = Project(
        name="P-strategic-approve-wrongstatus",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=existing_dirs,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        user_id="u1",
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"command": "review"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = str(task.id)

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "approve_direction",
                    "task_id": task_id,
                    "data": {"direction_id": "d-act01", "rationale": "x"},
                },
            )
        assert resp.status_code == 409, resp.text
        assert "proposed" in resp.json().get("reason", "").lower()
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


@pytest.mark.asyncio
async def test_strategic_api_reject_direction_creates_tombstone(db_session, mocker):
    """reject_direction should replace proposal with a 'rejected' tombstone and cancel queued tasks for it."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", return_value=True)

    existing_dirs = [
        {
            "id": "d-prop01",
            "text": "Noisy proposal to reject",
            "status": "proposed",
            "priority": "medium",
            "proposed_by": "t-research",
            "notes": [],
            "research_history": [],
        },
    ]
    project = Project(
        name="P-strategic-reject",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=existing_dirs,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Strategic-review task that will issue the reject op
    strategic_task = Task(
        project_id=project.id,
        user_id="u1",
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"command": "review"}],
    )
    # A QUEUED task tied to the proposal — should get cancelled
    queued_task = Task(
        project_id=project.id,
        user_id="u1",
        task_type="research",
        status=TaskStatus.QUEUED,
        payloads=[{"command": "research"}],
        context={"direction_id": "d-prop01"},
        vm_instance_id="vm-prop01",
    )
    db_session.add(strategic_task)
    db_session.add(queued_task)
    await db_session.commit()
    await db_session.refresh(strategic_task)
    await db_session.refresh(queued_task)
    task_id = str(strategic_task.id)

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "reject_direction",
                    "task_id": task_id,
                    "data": {
                        "direction_id": "d-prop01",
                        "rationale": "duplicate of existing work",
                    },
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["op"] == "reject_direction"
        assert body["cancelled_tasks"] == 1

        await db_session.refresh(project)
        tombstone = next(d for d in project.directions if d.get("id") == "d-prop01")
        assert tombstone["status"] == "rejected"
        assert tombstone.get("rejected_by") == "strategic"
        assert tombstone.get("rejection_rationale") == "duplicate of existing work"

        await db_session.refresh(queued_task)
        assert queued_task.status == TaskStatus.CANCELLED
        assert "d-prop01" in (queued_task.latest_status_message or "")

        await db_session.refresh(strategic_task)
        assert strategic_task.context.get("_strategic_proposals_rejected") == 1
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


# ─────────────────────────────────────────────────────────────────────────────
# Standalone tests for _calibrate_scope (no DB required)
# ─────────────────────────────────────────────────────────────────────────────


class TestCalibrateScope:
    """Unit tests for _calibrate_scope — pure function, no DB."""

    def _direction(self, scope_breakdown_per_entry: list[dict]) -> dict:
        """Build a minimal direction dict with research_history entries."""
        return {
            "id": "d-test",
            "title": "Test direction",
            "research_history": [
                {"scope_breakdown": bd} for bd in scope_breakdown_per_entry
            ],
        }

    def test_no_history_returns_unchanged(self, monkeypatch):
        """Direction with no research_history + no baselines → scope unchanged."""
        from app.shared_state import runtime_config as _rc

        monkeypatch.setitem(_rc, "pipeline_scope_baseline_rates", {})
        d = {"id": "d1"}
        assert _calibrate_scope("medium", d) == ("medium", None)

    def test_empty_history_returns_unchanged(self, monkeypatch):
        """Explicit empty research_history + no baselines → scope unchanged."""
        from app.shared_state import runtime_config as _rc

        monkeypatch.setitem(_rc, "pipeline_scope_baseline_rates", {})
        d = {"id": "d1", "research_history": []}
        assert _calibrate_scope("medium", d) == ("medium", None)

    def test_insufficient_data_points_returns_unchanged(self):
        """Less than 3 total tasks across history → no calibration (insufficient signal)."""
        d = self._direction(
            [
                {"small": {"created": 1, "merged": 0, "failed": 1}},
                {"small": {"created": 1, "merged": 0, "failed": 1}},
            ]
        )
        # total_data_points = 2 < 3
        assert _calibrate_scope("small", d) == ("small", None)

    def test_acceptable_rate_returns_unchanged(self):
        """Proposed scope at >= 40% merge rate → no downgrade."""
        d = self._direction(
            [
                {"medium": {"created": 5, "merged": 2, "failed": 3}},  # 40%
            ]
        )
        assert _calibrate_scope("medium", d) == ("medium", None)

    def test_exactly_40_percent_returns_unchanged(self):
        """Exactly 40% merge rate → not downgraded (rate >= 0.4 threshold)."""
        d = self._direction(
            [
                {"medium": {"created": 5, "merged": 2, "failed": 3}},  # 40% exactly
            ]
        )
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_below_threshold_downgrades_to_smaller_scope(self):
        """Proposed scope below 40% → downgraded to next smaller scope with acceptable rate.

        Uses 'large' scope to exercise the downgrade path without triggering the
        medium→large(decompose) upgrade.
        """
        d = self._direction(
            [
                {"large": {"created": 10, "merged": 3, "failed": 7}},  # 30% < 40%
                {"medium": {"created": 5, "merged": 3, "failed": 2}},  # 60% >= 40%
            ]
        )
        result_scope, calibrated_from = _calibrate_scope("large", d)
        assert result_scope == "medium"
        assert calibrated_from == "large"

    def test_smaller_scope_also_bad_returns_unchanged(self):
        """Proposed scope below 40% but no smaller scope has acceptable rate → no downgrade."""
        d = self._direction(
            [
                {"medium": {"created": 10, "merged": 2, "failed": 8}},  # 20%
                {"small": {"created": 5, "merged": 1, "failed": 4}},  # 20%
            ]
        )
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_smaller_scope_with_no_data_is_acceptable(self):
        """Smaller scope with no historical data (rate=None) is treated as acceptable."""
        d = self._direction(
            [
                {"medium": {"created": 10, "merged": 2, "failed": 8}},  # 20% < 40%
                # no "small" data
            ]
        )
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "small"
        assert calibrated_from == "medium"

    def test_boundary_40_percent_smaller_scope_is_accepted(self):
        """Smaller scope at exactly 40% merge rate should be accepted as downgrade target.

        Uses 'large' scope to bypass the medium→large(decompose) upgrade path.
        """
        d = self._direction(
            [
                {
                    "large": {"created": 10, "merged": 3, "failed": 7}
                },  # 30% — triggers downgrade search
                {
                    "medium": {"created": 5, "merged": 2, "failed": 3}
                },  # 40% — should be accepted (>= 0.4)
            ]
        )
        result_scope, calibrated_from = _calibrate_scope("large", d)
        assert result_scope == "medium"
        assert calibrated_from == "large"

    def test_large_scope_downgrades_through_medium_to_small(self):
        """Large scope below threshold downgrades: picks first smaller scope with acceptable rate."""
        d = self._direction(
            [
                {"large": {"created": 10, "merged": 2, "failed": 8}},  # 20%
                {"medium": {"created": 10, "merged": 2, "failed": 8}},  # 20%
                {"small": {"created": 5, "merged": 3, "failed": 2}},  # 60%
            ]
        )
        result_scope, calibrated_from = _calibrate_scope("large", d)
        assert result_scope == "small"
        assert calibrated_from == "large"

    def test_small_scope_cannot_be_downgraded(self):
        """Small is the smallest scope — even with low rate, no downgrade possible."""
        d = self._direction(
            [
                {"small": {"created": 10, "merged": 1, "failed": 9}},  # 10%
            ]
        )
        result_scope, calibrated_from = _calibrate_scope("small", d)
        assert result_scope == "small"
        assert calibrated_from is None

    def test_uses_only_last_3_history_entries(self):
        """Only the last 3 history entries are considered; older entries are ignored."""
        # Older entries show bad rate for small, recent entries show good rate for medium
        older_bad = {"small": {"created": 100, "merged": 0, "failed": 100}}  # very bad
        recent_good = {"medium": {"created": 5, "merged": 4, "failed": 1}}  # 80% good
        d = self._direction(
            [
                older_bad,
                older_bad,
                older_bad,
                older_bad,
                recent_good,
                recent_good,
                recent_good,
            ]
        )
        # Only last 3 (recent_good x3) used → medium rate 80%, no downgrade
        assert _calibrate_scope("medium", d) == ("medium", None)

    def test_high_overflow_rate_escalates_even_with_acceptable_merge_rate(self):
        """High failed_scope_overflow rate escalates to larger scope despite acceptable merge rate."""
        # medium: 10 created, 6 merged (60%), 4 failed incl. 4 overflow → 40% overflow
        # Merge rate alone would not trigger downgrade/upgrade, but overflow rate does.
        d = self._direction(
            [
                {
                    "medium": {
                        "created": 10,
                        "merged": 6,
                        "failed": 4,
                        "failed_infra": 4,
                        "failed_scope_overflow": 4,
                    }
                },
            ]
        )
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "large"
        assert calibrated_from == "medium"

    def test_low_overflow_rate_does_not_escalate(self):
        """Overflow rate below 30% does not trigger escalation."""
        d = self._direction(
            [
                {
                    "medium": {
                        "created": 10,
                        "merged": 7,
                        "failed": 3,
                        "failed_infra": 1,
                        "failed_scope_overflow": 1,
                    }
                },
            ]
        )
        assert _calibrate_scope("medium", d) == ("medium", None)

    def test_overflow_escalation_requires_3_data_points(self):
        """Fewer than 3 created at scope → overflow escalation does not fire."""
        # Only 2 medium tasks; project fallback also insufficient → cold-start path
        d = self._direction(
            [
                {
                    "medium": {
                        "created": 2,
                        "merged": 0,
                        "failed": 2,
                        "failed_infra": 2,
                        "failed_scope_overflow": 2,
                    },
                    "small": {"created": 3, "merged": 2, "failed": 1},
                },
            ]
        )
        # 2 medium created < 3 → overflow escalation skipped; medium effective
        # denominator also < 3 so merge rate is None → returns unchanged.
        assert _calibrate_scope("medium", d) == ("medium", None)

    def test_analysis_overflow_dominance_escalates_medium(self):
        """50%+ analysis_overflow rate at medium → hard-escalate medium→large."""
        # 4 medium created, 2 analysis_overflow failures (50%), merge rate 50%
        # (acceptable), scope_overflow below 30% → only analysis signal triggers.
        d = self._direction(
            [
                {
                    "medium": {
                        "created": 4,
                        "merged": 2,
                        "failed": 2,
                        "failed_infra": 2,
                        "failed_scope_overflow": 0,
                        "failed_analysis_overflow": 2,
                    }
                },
            ]
        )
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "large"
        assert calibrated_from == "medium"

    def test_overflow_escalation_skipped_for_large_scope(self):
        """Large scope has no larger scope to escalate to → no-op."""
        d = self._direction(
            [
                {
                    "large": {
                        "created": 10,
                        "merged": 6,
                        "failed": 4,
                        "failed_infra": 4,
                        "failed_scope_overflow": 4,
                    }
                },
            ]
        )
        # 60% merge rate, 40% overflow, but no scope larger than "large"
        assert _calibrate_scope("large", d) == ("large", None)

    def test_unknown_scope_returns_unchanged(self):
        """Unrecognized scope string → no downgrade (not in SCOPE_ORDER)."""
        d = self._direction(
            [
                {"xlarge": {"created": 10, "merged": 0}},
            ]
        )
        assert _calibrate_scope("xlarge", d) == ("xlarge", None)

    def _project(self, dirs_history: list[list[dict]]):
        """Build a minimal project stub with .directions populated."""

        class _P:
            pass

        p = _P()
        p.directions = [
            {
                "id": f"d{i}",
                "research_history": [{"scope_breakdown": bd} for bd in hist],
            }
            for i, hist in enumerate(dirs_history)
        ]
        return p

    def test_project_fallback_downgrades_when_direction_data_insufficient(self):
        """Direction has < 3 data points; project-wide rate < 30% → downgrade.

        Uses 'large' scope so the project-fallback downgrade path is exercised
        without triggering the medium→large(decompose) upgrade.
        """
        d = self._direction([])  # no direction history
        # Project-wide large scope: 10 created / 1 merged = 10% < 30%
        # Project-wide medium scope: 5 created / 4 merged = 80% — acceptable target
        project = self._project(
            [
                [
                    {"large": {"created": 5, "merged": 0, "failed": 5}},
                    {"medium": {"created": 3, "merged": 3, "failed": 0}},
                ],
                [
                    {"large": {"created": 5, "merged": 1, "failed": 4}},
                    {"medium": {"created": 2, "merged": 1, "failed": 1}},
                ],
            ]
        )
        result_scope, calibrated_from = _calibrate_scope("large", d, project)
        assert result_scope == "medium"
        assert calibrated_from == "large"

    def test_project_fallback_softer_threshold_30_percent(self):
        """Project-wide rate at exactly 30% → not downgraded (threshold >= 0.3)."""
        d = self._direction([])
        # Project-wide medium: 10 created / 3 merged = 30% (not below threshold)
        project = self._project(
            [
                [{"medium": {"created": 10, "merged": 3, "failed": 7}}],
            ]
        )
        result_scope, calibrated_from = _calibrate_scope("medium", d, project)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_project_fallback_skipped_when_direction_has_enough_data(self):
        """Direction has >= 3 data points → use direction-specific 40% threshold,
        ignore project-wide data."""
        # Direction medium: 5 created / 2 merged = 40% — acceptable at direction threshold
        d = self._direction(
            [
                {"medium": {"created": 5, "merged": 2, "failed": 3}},
            ]
        )
        # Project-wide medium would be 0% (very bad), but should be ignored
        project = self._project(
            [
                [{"medium": {"created": 100, "merged": 0, "failed": 100}}],
            ]
        )
        result_scope, calibrated_from = _calibrate_scope("medium", d, project)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_project_fallback_returns_unchanged_when_project_data_also_thin(
        self, monkeypatch
    ):
        """Both direction and project-wide data insufficient + no baselines → no calibration."""
        from app.shared_state import runtime_config as _rc

        monkeypatch.setitem(_rc, "pipeline_scope_baseline_rates", {})
        d = self._direction([])
        project = self._project(
            [
                [{"medium": {"created": 1, "merged": 0, "failed": 1}}],
            ]
        )  # project-wide total = 1 < 3
        result_scope, calibrated_from = _calibrate_scope("medium", d, project)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_project_fallback_omitted_returns_unchanged(self, monkeypatch):
        """No project passed AND direction insufficient + no baselines → unchanged (back-compat)."""
        from app.shared_state import runtime_config as _rc

        monkeypatch.setitem(_rc, "pipeline_scope_baseline_rates", {})
        d = self._direction([])
        assert _calibrate_scope("medium", d) == ("medium", None)

    def test_cold_start_upgrades_medium_when_pipeline_baseline_below_threshold(
        self, monkeypatch
    ):
        """No direction/project history + baseline medium < 40% + small healthy → upgrade medium→large."""
        from app.shared_state import runtime_config as _rc

        monkeypatch.setitem(
            _rc,
            "pipeline_scope_baseline_rates",
            {"small": 0.75, "medium": 0.04, "large": 0.50},
        )
        d = self._direction([])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "large"
        assert calibrated_from == "medium"

    def test_cold_start_upgrades_medium_when_project_data_thin(self, monkeypatch):
        """Direction empty + project < 3 data points + baseline says medium failing → upgrade."""
        from app.shared_state import runtime_config as _rc

        monkeypatch.setitem(
            _rc,
            "pipeline_scope_baseline_rates",
            {"small": 0.75, "medium": 0.04, "large": 0.50},
        )
        d = self._direction([])
        project = self._project(
            [
                [{"medium": {"created": 1, "merged": 0, "failed": 1}}],
            ]
        )
        result_scope, calibrated_from = _calibrate_scope("medium", d, project)
        assert result_scope == "large"
        assert calibrated_from == "medium"

    def test_cold_start_no_upgrade_when_baseline_medium_healthy(self, monkeypatch):
        """Cold-start + baseline medium >= 40% → no upgrade (pipeline-wide medium is fine)."""
        from app.shared_state import runtime_config as _rc

        monkeypatch.setitem(
            _rc,
            "pipeline_scope_baseline_rates",
            {"small": 0.75, "medium": 0.50, "large": 0.50},
        )
        d = self._direction([])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_cold_start_no_upgrade_when_baseline_small_unhealthy(self, monkeypatch):
        """Cold-start + baseline small < 40% → no upgrade (decompose target is unproven)."""
        from app.shared_state import runtime_config as _rc

        monkeypatch.setitem(
            _rc,
            "pipeline_scope_baseline_rates",
            {"small": 0.20, "medium": 0.04, "large": 0.50},
        )
        d = self._direction([])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_cold_start_no_upgrade_for_non_medium_scope(self, monkeypatch):
        """Cold-start upgrade only fires for medium — small/large pass through unchanged."""
        from app.shared_state import runtime_config as _rc

        monkeypatch.setitem(
            _rc,
            "pipeline_scope_baseline_rates",
            {"small": 0.75, "medium": 0.04, "large": 0.50},
        )
        d = self._direction([])
        assert _calibrate_scope("small", d) == ("small", None)
        assert _calibrate_scope("large", d) == ("large", None)

    def test_cold_start_no_upgrade_when_baselines_missing(self, monkeypatch):
        """Cold-start + no baseline configured → returns unchanged (no implicit defaults applied)."""
        from app.shared_state import runtime_config as _rc

        monkeypatch.setitem(_rc, "pipeline_scope_baseline_rates", {"small": 0.75})
        d = self._direction([])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_medium_upgrades_to_large_when_small_is_healthy(self):
        """Medium below threshold + small healthy → upgrade to large for plan-agent decomposition.

        calibrated_from is the bare proposed scope ('medium') — the "(decompose)"
        detail is logged, not persisted, so downstream consumers that treat
        calibrated_from as a scope key (direction_outcomes bookkeeping,
        prompt.ts calibration note, admin analytics) receive a valid scope.
        """
        d = self._direction(
            [
                {"medium": {"created": 10, "merged": 2, "failed": 8}},  # 20% < 40%
                {
                    "small": {"created": 5, "merged": 4, "failed": 1}
                },  # 80% >= 40% (healthy)
            ]
        )
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "large"
        assert calibrated_from == "medium"

    def test_medium_no_upgrade_when_small_also_failing(self):
        """Medium below threshold + small below threshold → systemic issue, no upgrade.

        Falls through to downgrade loop; since small also fails, returns unchanged.
        """
        d = self._direction(
            [
                {"medium": {"created": 10, "merged": 2, "failed": 8}},  # 20%
                {"small": {"created": 5, "merged": 1, "failed": 4}},  # 20% < 40%
            ]
        )
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_medium_no_upgrade_when_small_lacks_data(self):
        """Medium below threshold + small has insufficient data → no upgrade.

        Without proven small-scope success, decomposition is not justified. Falls
        through to existing downgrade behavior (which treats no-data as acceptable).
        """
        d = self._direction(
            [
                {"medium": {"created": 10, "merged": 2, "failed": 8}},  # 20%
                # no "small" data
            ]
        )
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "small"
        assert calibrated_from == "medium"

    def test_medium_downgrades_to_small_when_decomp_history_poor(self):
        """Medium below threshold + small healthy + poor decomposition history →
        downgrade to small rather than upgrading to large or staying at medium.

        Guards the dead-zone bug where _poor_decomp_history suppressed the
        medium→large upgrade and the function returned early at medium, skipping
        the downgrade loop entirely.
        """
        d = self._direction(
            [
                {"medium": {"created": 10, "merged": 2, "failed": 8}},  # 20% < 40%
                {"small": {"created": 5, "merged": 4, "failed": 1}},  # 80% healthy
            ]
        )
        d["research_history"][-1]["decomposition_quality"] = [
            {"subtask_merge_rate": 0.1, "subtask_count": 5},
        ]
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "small"
        assert calibrated_from == "medium"

    def test_large_does_not_upgrade(self):
        """Upgrade path only fires for medium — large below threshold still downgrades normally."""
        d = self._direction(
            [
                {"large": {"created": 10, "merged": 2, "failed": 8}},  # 20%
                {
                    "medium": {"created": 5, "merged": 1, "failed": 4}
                },  # 20% — bad, skipped
                {"small": {"created": 5, "merged": 4, "failed": 1}},  # 80% — healthy
            ]
        )
        result_scope, calibrated_from = _calibrate_scope("large", d)
        # Despite small being healthy (the trigger condition for upgrade), large
        # only ever downgrades — calibrated_from is the bare scope, not a "→...(decompose)" marker.
        assert result_scope == "small"
        assert calibrated_from == "large"

    def test_in_flight_tasks_excluded_from_denominator(self):
        """16 medium created, only 4 terminal (1 merged + 3 failed) → rate reads 25%, not 6%.

        Guards against in-flight bias: the older formula (created - true_infra)
        would read 1/16 = 6% because still-QUEUED/IN_PROGRESS tasks inflate
        `created`. Terminal-outcome denominator (merged + failed - true_infra)
        restores the honest 1/4 = 25% rate.
        """
        d = self._direction(
            [
                # 16 medium spawned, 12 still in-flight (not in merged/failed counters);
                # 5 small with 4 merged (healthy) to trigger the decompose-upgrade path.
                {
                    "medium": {"created": 16, "merged": 1, "failed": 3},
                    "small": {"created": 5, "merged": 4, "failed": 1},
                },
            ]
        )
        # With the old formula, medium rate = 1/16 = 6% < 40% (in-flight bias).
        # With the terminal denominator, rate = 1/4 = 25% < 40%. Medium still
        # downgrades here, but the *reason* is the honest terminal rate, not
        # the artificial in-flight skew. Small at 4/5 = 80% triggers the
        # medium→large(decompose) upgrade path.
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "large"
        assert calibrated_from == "medium"

    def test_in_flight_bias_does_not_force_downgrade(self):
        """Early-cycle medium task (many in-flight, few terminal) is not downgraded
        prematurely by an artificially low rate."""
        d = self._direction(
            [
                # 10 spawned, only 2 terminal: both merged (100% true rate).
                # Old formula: rate = 2/10 = 20% → would downgrade.
                # New formula: rate = 2/(2+0) = 100% but effective=2 < 3 → rate=None → unchanged.
                {"medium": {"created": 10, "merged": 2, "failed": 0}},
            ]
        )
        # direction_data_points uses created (10 >= 3), so we enter calibration.
        # effective denominator = 2 + 0 - 0 = 2 < 3 → rate=None → unchanged.
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "medium"
        assert calibrated_from is None


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline awareness: stalled-cycle detection and research failure recording
# ─────────────────────────────────────────────────────────────────────────────


class TestDirectionHealthRule4:
    """Rule 4 pauses a direction after 3 consecutive failing cycles."""

    def _make_entry(
        self,
        *,
        research_task_failed: bool = False,
        tasks_failed: int = 0,
        prs_merged: int = 0,
        tasks_created: int = 0,
    ) -> dict:
        return {
            "timestamp": "2026-04-15T00:00:00",
            "tasks_created": tasks_created,
            "tasks_failed": tasks_failed,
            "prs_merged": prs_merged,
            "research_task_failed": research_task_failed,
        }

    def test_rule4_three_research_task_failures_trips(self):
        from app.strategic_completion import _check_direction_health

        d = {
            "research_history": [
                self._make_entry(research_task_failed=True) for _ in range(3)
            ]
        }
        reason = _check_direction_health(d)
        assert reason is not None
        assert "Recent stall" in reason

    def test_rule4_three_failed_children_trips(self):
        """3 cycles each with ≥1 failed child and zero merges pauses the direction.

        Keep the failure ratio under 70% so Rule 3 does not fire first — we are
        specifically testing that Rule 4 catches the 'any failures, no merges'
        pattern that Rule 3 would miss.
        """
        from app.strategic_completion import _check_direction_health

        d = {
            "research_history": [
                # 5 created, 1 failed = 20% — below Rule 3's 70% — but 0 merged triggers Rule 4
                self._make_entry(tasks_created=5, tasks_failed=1, prs_merged=0)
                for _ in range(3)
            ]
        }
        reason = _check_direction_health(d)
        assert reason is not None
        assert "Recent stall" in reason

    def test_rule4_mixed_with_merge_does_not_trip(self):
        """If any of the last 3 cycles has a merge, Rule 4 does not trip."""
        from app.strategic_completion import _check_direction_health

        d = {
            "research_history": [
                self._make_entry(tasks_created=5, tasks_failed=1, prs_merged=0),
                self._make_entry(
                    tasks_created=5, tasks_failed=1, prs_merged=1
                ),  # has a merge
                self._make_entry(tasks_created=5, tasks_failed=1, prs_merged=0),
            ]
        }
        reason = _check_direction_health(d)
        # Rules 1-3 should not trip either on this short history
        assert reason is None

    def test_rule4_fewer_than_three_entries_does_not_trip(self):
        from app.strategic_completion import _check_direction_health

        d = {
            "research_history": [
                self._make_entry(research_task_failed=True) for _ in range(2)
            ]
        }
        assert _check_direction_health(d) is None

    def test_rule4_mixed_failure_modes_trip(self):
        """Mixed research-task failures and failed-children count as failing cycles.

        Middle cycle keeps failure ratio under Rule 3's 70% threshold so Rule 4
        is the check that actually catches the pattern.
        """
        from app.strategic_completion import _check_direction_health

        d = {
            "research_history": [
                self._make_entry(research_task_failed=True),
                self._make_entry(tasks_created=5, tasks_failed=1, prs_merged=0),
                self._make_entry(research_task_failed=True),
            ]
        }
        reason = _check_direction_health(d)
        assert reason is not None
        assert "Recent stall" in reason


@pytest.mark.asyncio
async def test_record_research_task_failure_appends_history(db_session):
    """FAILED research task records a research_task_failed entry on its direction."""
    from app.research_completion import _record_research_task_failure

    direction = {
        "id": "d-failrec1",
        "text": "Failure recording test",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
    }
    project = Project(
        name="Failure Recording Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Failed research",
        description="research failed at provisioning",
        payloads=[{"type": "research", "prompt": "find things"}],
        context={"direction_id": "d-failrec1"},
        status=TaskStatus.FAILED,
        latest_status_message="provisioning failed: zone exhausted",
        created_by="ORCHESTRATOR",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _record_research_task_failure(task, db_session)
    await db_session.commit()
    await db_session.refresh(project)

    d = next(x for x in project.directions if x["id"] == "d-failrec1")
    history = d.get("research_history", [])
    assert len(history) == 1
    entry = history[0]
    assert entry.get("research_task_failed") is True
    assert entry.get("tasks_created") == 0
    assert entry.get("task_id") == str(task.id)
    assert "failure_category" in entry
    assert d.get("last_researched_at") is not None


@pytest.mark.asyncio
async def test_record_research_task_failure_no_direction_is_noop(db_session):
    """Task without direction_id does not crash _record_research_task_failure."""
    from app.research_completion import _record_research_task_failure

    project = Project(
        name="No Direction Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Failed research",
        description="no direction attached",
        payloads=[{"type": "research", "prompt": "find things"}],
        context={},
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Should be a silent no-op, no exception
    await _record_research_task_failure(task, db_session)


# ─────────────────────────────────────────────────────────────────────────────
# Standalone tests for predict_spawn_success (no DB required)
# ─────────────────────────────────────────────────────────────────────────────


class TestPredictSpawnSuccessPipelineSignal:
    """Unit tests for the pipeline-wide scope merge rate fallback signal."""

    def test_no_data_no_pipeline_rate_returns_neutral(self):
        """With no signals at all, returns 0.5 — preserves prior fallback behavior."""
        from app.shared_state import runtime_config

        runtime_config["pipeline_scope_merge_rates"] = {}
        try:
            assert predict_spawn_success("medium", {}) == 0.5
        finally:
            runtime_config["pipeline_scope_merge_rates"] = {}

    def test_only_pipeline_rate_dominates_when_others_absent(self):
        """When only signal #4 is available (sparse direction), it sets P(success)
        to the global rate so a 4% medium rate triggers the low_confidence gate
        instead of collapsing to a neutral 0.5 that always passes."""
        from app.shared_state import runtime_config

        runtime_config["pipeline_scope_merge_rates"] = {"medium": 0.04}
        try:
            p = predict_spawn_success("medium", {})
            assert p == pytest.approx(0.04)
        finally:
            runtime_config["pipeline_scope_merge_rates"] = {}

    def test_pipeline_rate_does_not_apply_to_other_scopes(self):
        """A medium pipeline rate must not influence small-scope predictions."""
        from app.shared_state import runtime_config

        runtime_config["pipeline_scope_merge_rates"] = {"medium": 0.04}
        try:
            assert predict_spawn_success("small", {}) == 0.5
        finally:
            runtime_config["pipeline_scope_merge_rates"] = {}

    def test_pipeline_rate_weighted_below_direction_signal(self):
        """Direction-level signal (weight 2.0) must dominate the pipeline-wide
        fallback (weight 0.5) when both are present."""
        from app.shared_state import runtime_config

        runtime_config["pipeline_scope_merge_rates"] = {"medium": 0.0}
        try:
            d = {
                "research_history": [
                    {
                        "scope_breakdown": {
                            "medium": {"created": 5, "merged": 5, "failed": 0}
                        }
                    },
                ],
            }
            # Signal 1: 1.0 (weight 2.0); Signal 4: 0.0 (weight 0.5).
            # Weighted: (1.0*2.0 + 0.0*0.5) / 2.5 = 0.8
            p = predict_spawn_success("medium", d)
            assert p == pytest.approx(0.8)
        finally:
            runtime_config["pipeline_scope_merge_rates"] = {}

    def test_pipeline_rate_clamped_to_unit_interval(self):
        """Out-of-range pipeline rates are clamped to [0.0, 1.0]."""
        from app.shared_state import runtime_config

        runtime_config["pipeline_scope_merge_rates"] = {"medium": 1.5}
        try:
            assert predict_spawn_success("medium", {}) == pytest.approx(1.0)
            runtime_config["pipeline_scope_merge_rates"] = {"medium": -0.2}
            assert predict_spawn_success("medium", {}) == pytest.approx(0.0)
        finally:
            runtime_config["pipeline_scope_merge_rates"] = {}

    def test_pipeline_rate_ignores_non_numeric(self):
        """Non-numeric values (e.g. None, strings) are ignored — falls back to neutral."""
        from app.shared_state import runtime_config

        runtime_config["pipeline_scope_merge_rates"] = {"medium": None}
        try:
            assert predict_spawn_success("medium", {}) == 0.5
            runtime_config["pipeline_scope_merge_rates"] = {"medium": "0.04"}
            assert predict_spawn_success("medium", {}) == 0.5
        finally:
            runtime_config["pipeline_scope_merge_rates"] = {}


class TestPredictSpawnSuccessInfraFailureExclusion:
    """Infra failures (provision/rate-limit/timeout) must not reduce spawn confidence."""

    def test_infra_failures_excluded_from_failure_rate(self):
        """All failures are infra → code-attributable failure rate is 0 → signal contributes 1.0."""
        d = {
            "research_history": [
                {"tasks_created": 3, "tasks_failed": 3, "tasks_failed_infra": 3},
            ],
        }
        # Only Signal 3 present: 1.0 - 0/3 = 1.0
        assert predict_spawn_success("medium", d) == pytest.approx(1.0)

    def test_code_failures_still_reduce_confidence(self):
        """Non-infra failures still count against the direction."""
        d = {
            "research_history": [
                {"tasks_created": 4, "tasks_failed": 3, "tasks_failed_infra": 1},
            ],
        }
        # Signal 3: code_failed=2, failure_rate=0.5 → 0.5
        assert predict_spawn_success("medium", d) == pytest.approx(0.5)


class TestPredictSpawnSuccessDecompositionSignal:
    """Unit tests for the large-scope decomposition_quality signal."""

    def test_decomposition_signal_only_applies_to_large_scope(self):
        d = {
            "research_history": [
                {"decomposition_quality": [{"subtask_merge_rate": 0.8}]},
            ],
        }
        # medium scope: decomposition signal omitted → no signals → neutral 0.5
        assert predict_spawn_success("medium", d) == 0.5

    def test_decomposition_signal_averages_recent_plans(self):
        d = {
            "research_history": [
                {
                    "decomposition_quality": [
                        {"subtask_merge_rate": 0.4},
                        {"subtask_merge_rate": 0.8},
                    ]
                },
            ],
        }
        # Only signal 5: average = 0.6
        assert predict_spawn_success("large", d) == pytest.approx(0.6)

    def test_decomposition_signal_omitted_when_no_data(self):
        """No decomposition data → signal omitted, not defaulted to 0.5."""
        d = {"research_history": [{"decomposition_quality": []}]}
        assert predict_spawn_success("large", d) == 0.5

    def test_decomposition_signal_skips_none_rates(self):
        d = {
            "research_history": [
                {
                    "decomposition_quality": [
                        {"subtask_merge_rate": None},
                        {"subtask_merge_rate": 0.5},
                    ]
                },
            ],
        }
        assert predict_spawn_success("large", d) == pytest.approx(0.5)

    def test_decomposition_signal_combines_with_direction_signal(self):
        d = {
            "research_history": [
                {
                    "scope_breakdown": {
                        "large": {"created": 2, "merged": 2, "failed": 0}
                    },
                    "decomposition_quality": [{"subtask_merge_rate": 0.0}],
                },
            ],
        }
        # Signal 1 (weight 2.0): 1.0; Signal 5 (weight 1.5): 0.0
        # (2.0*1.0 + 1.5*0.0) / 3.5 = 0.5714...
        assert predict_spawn_success("large", d) == pytest.approx(2.0 / 3.5)


class TestPredictSpawnSuccessDiagnosisSignal:
    """Unit tests for the diagnosis-state penalty signal (Signal 6)."""

    def test_no_diagnosis_state_omits_signal(self):
        """A direction with zero diagnosis indicators is unaffected — Signal 6
        contributes nothing, so the result equals the no-data neutral 0.5.
        """
        assert predict_spawn_success("medium", {}) == 0.5

    def test_needs_manual_intervention_applies_low_score(self):
        """needs_manual_intervention is the strongest indicator: contributes 0.1
        regardless of other diagnosis fields.
        """
        d = {"needs_manual_intervention": True}
        # Only Signal 6 present at weight 1.0 → 0.1
        assert predict_spawn_success("medium", d) == pytest.approx(0.1)

    def test_needs_manual_intervention_overrides_other_diagnosis_fields(self):
        """When needs_manual_intervention is True, weaker indicators are ignored."""
        d = {
            "needs_manual_intervention": True,
            "diagnosis_in_progress": True,
            "remediation_attempts": 5,
            "last_remediation_result": "failed",
        }
        assert predict_spawn_success("medium", d) == pytest.approx(0.1)

    def test_diagnosis_in_progress_applies_moderate_penalty(self):
        d = {"diagnosis_in_progress": True}
        # Only Signal 6 present at weight 1.0 → 0.3
        assert predict_spawn_success("medium", d) == pytest.approx(0.3)

    def test_failed_remediations_proportional_penalty(self):
        """rem_attempts=2 with last_result=failed → 0.4; rem_attempts=4 → 0.2."""
        d2 = {"remediation_attempts": 2, "last_remediation_result": "failed"}
        assert predict_spawn_success("medium", d2) == pytest.approx(0.4)
        d4 = {"remediation_attempts": 4, "last_remediation_result": "failed"}
        assert predict_spawn_success("medium", d4) == pytest.approx(0.2)
        d10 = {"remediation_attempts": 10, "last_remediation_result": "failed"}
        # Floored at 0.1.
        assert predict_spawn_success("medium", d10) == pytest.approx(0.1)

    def test_failed_remediations_below_threshold_omits_signal(self):
        """rem_attempts=1 — below the 2-attempt threshold — leaves direction
        unaffected even with last_result=failed.
        """
        d = {"remediation_attempts": 1, "last_remediation_result": "failed"}
        assert predict_spawn_success("medium", d) == 0.5

    def test_merged_remediation_omits_signal(self):
        """A successful (merged) remediation is not a penalty signal even with
        many attempts — the direction has already been fixed.
        """
        d = {"remediation_attempts": 5, "last_remediation_result": "merged"}
        assert predict_spawn_success("medium", d) == 0.5

    def test_diagnosis_signal_combines_with_direction_signal(self):
        """Signal 1 (weight 2.0) at 1.0 combined with Signal 6 (weight 1.0) at 0.3
        → (2.0*1.0 + 1.0*0.3) / 3.0 = 0.7666...
        """
        d = {
            "research_history": [
                {
                    "scope_breakdown": {
                        "medium": {"created": 5, "merged": 5, "failed": 0}
                    }
                },
            ],
            "diagnosis_in_progress": True,
        }
        assert predict_spawn_success("medium", d) == pytest.approx(2.3 / 3.0)


class TestPredictSpawnSuccessAcceptanceSignal:
    """Unit tests for the operator acceptance-rate signal (Signal 7)."""

    def test_no_pr_data_omits_signal(self):
        """A direction with zero PRs merged/rejected contributes nothing."""
        d = {"research_history": [{"decomposition_quality": []}]}
        assert predict_spawn_success("medium", d) == 0.5

    def test_below_threshold_omits_signal(self):
        """Fewer than 3 total PRs — signal omitted to avoid single-PR noise."""
        d = {"research_history": [{"prs_merged": 1, "prs_rejected": 1}]}
        assert predict_spawn_success("medium", d) == 0.5

    def test_low_acceptance_reduces_confidence(self):
        """3+ PRs with majority rejected meaningfully reduces P(success)."""
        d = {
            "research_history": [
                {"prs_merged": 1, "prs_rejected": 4},
            ],
        }
        # Only Signal 7 present at weight 1.5: acceptance = 1/5 = 0.2
        assert predict_spawn_success("medium", d) == pytest.approx(0.2)

    def test_high_acceptance_supports_confidence(self):
        d = {
            "research_history": [
                {"prs_merged": 4, "prs_rejected": 1},
            ],
        }
        assert predict_spawn_success("medium", d) == pytest.approx(0.8)

    def test_aggregate_counts_toward_threshold(self):
        """history_aggregate PRs count toward the 3-PR minimum."""
        d = {
            "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
            "history_aggregate": {"total_prs_merged": 0, "total_prs_rejected": 5},
        }
        # total PRs = 6 >= 3; acceptance via _compute_acceptance_rate combines
        # recent (1/1=1.0) with lifetime (1/6≈0.1667): (1.0*3 + 0.1667)/4
        expected = (1.0 * 3 + 1.0 / 6) / 4
        assert predict_spawn_success("medium", d) == pytest.approx(expected)

    def test_related_files_small_count_no_penalty(self):
        """1-2 related_files emit no file-count signal."""
        assert (
            predict_spawn_success("small", {}, related_files=["backend/app/a.py"])
            == 0.5
        )
        assert (
            predict_spawn_success(
                "small",
                {},
                related_files=["backend/app/a.py", "backend/app/b.py"],
            )
            == 0.5
        )

    def test_related_files_moderate_penalty(self):
        """3-4 files → moderate 0.6 penalty at weight 1.0."""
        files = [f"backend/app/f{i}.py" for i in range(3)]
        assert predict_spawn_success("small", {}, related_files=files) == pytest.approx(
            0.6
        )

    def test_related_files_strong_penalty(self):
        """5+ files → strong 0.3 penalty at weight 1.0."""
        files = [f"backend/app/f{i}.py" for i in range(5)]
        assert predict_spawn_success("small", {}, related_files=files) == pytest.approx(
            0.3
        )

    def test_cross_module_span_penalty(self):
        """Cross-module span is intentionally NOT a separate spawn signal — the
        composite complexity score already folds module span into its factor.
        Only the file-count penalty (0.6 for 3 files at small scope) fires.
        """
        files = ["backend/app/a.py", "agent/src/b.ts", "admin-ui/src/c.ts"]
        assert predict_spawn_success("small", {}, related_files=files) == pytest.approx(
            0.6
        )

    def test_two_module_span_no_cross_module_penalty(self):
        """2-module findings stay at medium scope (per escalation threshold), so
        the cross-module spawn penalty should not fire below 3 top-level dirs."""
        files = ["backend/app/a.py", "agent/src/b.ts"]
        assert predict_spawn_success("small", {}, related_files=files) == 0.5

    def test_single_top_level_no_cross_module_penalty(self):
        """All files under one top-level dir → no cross-module signal."""
        files = ["backend/app/a.py", "backend/app/b.py"]
        assert predict_spawn_success("small", {}, related_files=files) == 0.5

    def test_related_files_scope_aware_thresholds(self):
        """Medium scope uses shifted thresholds (7+/5+) so 5 files gets
        the moderate penalty (0.6) rather than small's strong 0.3.
        Small scope is unchanged — 5 files still gets 0.3."""
        files = [f"backend/app/f{i}.py" for i in range(5)]
        medium_score = predict_spawn_success("medium", {}, related_files=files)
        small_score = predict_spawn_success("small", {}, related_files=files)
        assert small_score == pytest.approx(0.3)
        assert medium_score == pytest.approx(0.6)
        assert medium_score > small_score
        # Medium with 7 files hits the strong 0.3 penalty.
        files7 = [f"backend/app/f{i}.py" for i in range(7)]
        assert predict_spawn_success(
            "medium", {}, related_files=files7
        ) == pytest.approx(0.3)

    def test_analysis_overflow_dominance_penalizes_medium(self):
        """50%+ of medium failures being analysis_overflow triggers 0.2 penalty."""
        # 2 medium failures, both analysis_overflow — scope merge rate = 0/2 = 0.0
        # with weight 2.0, plus analysis_overflow signal 0.2 at weight 1.5.
        d = {
            "research_history": [
                {
                    "scope_breakdown": {
                        "medium": {
                            "created": 2,
                            "merged": 0,
                            "failed": 2,
                            "failed_infra": 2,
                            "failed_scope_overflow": 2,
                            "failed_analysis_overflow": 2,
                        },
                    },
                },
            ],
        }
        # Signal 1: 0/2 = 0.0 weight 2.0
        # Signal 10: 0.2 weight 1.5
        # Signal 3 omitted (tasks_created=0 in history entry)
        expected = (0.0 * 2.0 + 0.2 * 1.5) / (2.0 + 1.5)
        assert predict_spawn_success("medium", d) == pytest.approx(expected)

    def test_analysis_overflow_below_threshold_omitted(self):
        """<50% analysis_overflow share of failures does not trigger signal 10."""
        d = {
            "research_history": [
                {
                    "scope_breakdown": {
                        "medium": {
                            "created": 5,
                            "merged": 1,
                            "failed": 4,
                            "failed_infra": 4,
                            "failed_scope_overflow": 4,
                            "failed_analysis_overflow": 1,
                        },
                    },
                },
            ],
        }
        # Only signal 1: merge rate = 1/5 = 0.2
        assert predict_spawn_success("medium", d) == pytest.approx(0.2)

    def test_analysis_overflow_sample_too_small_omitted(self):
        """Single failure (sample < 2) does not trigger signal 10 even at 100%."""
        d = {
            "research_history": [
                {
                    "scope_breakdown": {
                        "medium": {
                            "created": 1,
                            "merged": 0,
                            "failed": 1,
                            "failed_infra": 1,
                            "failed_scope_overflow": 1,
                            "failed_analysis_overflow": 1,
                        },
                    },
                },
            ],
        }
        # effective = 1 < 2 → signal 1 omitted; signal 10 also requires failed>=2
        assert predict_spawn_success("medium", d) == 0.5

    def test_analysis_overflow_small_scope_not_affected(self):
        """Signal 10 is medium-only."""
        d = {
            "research_history": [
                {
                    "scope_breakdown": {
                        "small": {
                            "created": 2,
                            "merged": 0,
                            "failed": 2,
                            "failed_infra": 2,
                            "failed_scope_overflow": 2,
                            "failed_analysis_overflow": 2,
                        },
                    },
                },
            ],
        }
        # Only signal 1: 0/2 = 0.0
        assert predict_spawn_success("small", d) == pytest.approx(0.0)

    def test_complexity_score_signal_lowers_medium_prediction(self):
        """A medium finding with a low complexity_score should receive a lower
        predicted P(success) than one with a high score (weight 1.5)."""
        low = predict_spawn_success("medium", {}, complexity_score=0.2)
        high = predict_spawn_success("medium", {}, complexity_score=0.9)
        assert low < high
        # With no other signals, the weighted average equals the complexity
        # score itself (single signal, any weight).
        assert low == pytest.approx(0.2)
        assert high == pytest.approx(0.9)
        # Absent/None preserves the neutral fallback.
        assert predict_spawn_success("medium", {}) == 0.5

    def test_review_iteration_signal_graduated_penalty(self):
        """Rolling median review-iteration count applies a graduated penalty:
        1-2 iters → no effect, 3-4 → 0.5 (moderate), 5+ → 0.2 (strong)."""
        # No effect at 1-2 iterations (normal churn).
        d_low = {"research_history": [{"median_review_iterations": 2}]}
        assert predict_spawn_success("small", d_low) == 0.5
        # Moderate penalty at 3-4 iterations: combine with complexity 0.9
        # (weight 1.5) so the review-iter signal (0.5, weight 1.0) is observable.
        d_mid = {"research_history": [{"median_review_iterations": 3}]}
        # (0.9*1.5 + 0.5*1.0) / 2.5 = 1.85/2.5 = 0.74
        assert predict_spawn_success(
            "small", d_mid, complexity_score=0.9
        ) == pytest.approx(0.74)
        d_mid2 = {"research_history": [{"median_review_iterations": 4}]}
        assert predict_spawn_success(
            "small", d_mid2, complexity_score=0.9
        ) == pytest.approx(0.74)
        # Without the review-iter penalty, the same complexity would yield 0.9.
        assert predict_spawn_success(
            "small", d_low, complexity_score=0.9
        ) == pytest.approx(0.9)
        # Strong penalty at 5+ iterations.
        d_high = {"research_history": [{"median_review_iterations": 6}]}
        assert predict_spawn_success("small", d_high) == pytest.approx(0.2)


# ─────────────────────────────────────────────────────────────────────────────
# _poor_decomp_history helper unit tests (suppression signal)
# ─────────────────────────────────────────────────────────────────────────────


class TestPoorDecompHistory:
    """Unit tests for _poor_decomp_history: suppression signal shared by
    _calibrate_scope and the low-confidence auto-escalation gate.
    """

    def test_no_history_returns_false(self):
        assert _poor_decomp_history({}) is False

    def test_insufficient_subtask_count_returns_false(self):
        d = {
            "research_history": [
                {
                    "decomposition_quality": [
                        {"subtask_merge_rate": 0.0, "subtask_count": 2},
                    ]
                }
            ]
        }
        assert _poor_decomp_history(d) is False

    def test_poor_weighted_avg_returns_true(self):
        d = {
            "research_history": [
                {
                    "decomposition_quality": [
                        {"subtask_merge_rate": 0.1, "subtask_count": 4},
                    ]
                }
            ]
        }
        assert _poor_decomp_history(d) is True

    def test_healthy_weighted_avg_returns_false(self):
        d = {
            "research_history": [
                {
                    "decomposition_quality": [
                        {"subtask_merge_rate": 0.5, "subtask_count": 4},
                    ]
                }
            ]
        }
        assert _poor_decomp_history(d) is False

    def test_weighted_averaging_across_entries(self):
        # 2*0.8 + 3*0.1 = 1.9 over 5 subtasks = 0.38 >= 0.3 → healthy
        d = {
            "research_history": [
                {
                    "decomposition_quality": [
                        {"subtask_merge_rate": 0.8, "subtask_count": 2},
                        {"subtask_merge_rate": 0.1, "subtask_count": 3},
                    ]
                }
            ]
        }
        assert _poor_decomp_history(d) is False

    def test_skips_non_numeric_rates(self):
        d = {
            "research_history": [
                {
                    "decomposition_quality": [
                        {"subtask_merge_rate": None, "subtask_count": 10},
                        {"subtask_merge_rate": 0.1, "subtask_count": 3},
                    ]
                }
            ]
        }
        assert _poor_decomp_history(d) is True


# ─────────────────────────────────────────────────────────────────────────────
# Low-confidence medium auto-escalation gate (integration via _handle_research_completion)
# ─────────────────────────────────────────────────────────────────────────────


async def _setup_escalation_project(db_session, direction_id, decomp_quality):
    # Empty research_history keeps _calibrate_scope a no-op (cold-start falls through);
    # low-confidence is simulated via mocker.patch on predict_spawn_success in the test.
    direction = {
        "id": direction_id,
        "text": "Low-confidence medium",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": (
            [{"decomposition_quality": decomp_quality}] if decomp_quality else []
        ),
    }
    project = Project(
        name="Escalation test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="Research: low-conf",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task, direction


def _mock_escalation_externals(mocker, direction_id):
    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new=AsyncMock(return_value=("fake-token", "owner", "repo")),
    )
    mocker.patch(
        "app.task_completion.get_branch_sha", new=AsyncMock(return_value="abc123sha")
    )
    mocker.patch("app.task_completion.create_branch", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.research_completion._validate_plan_dependencies", return_value=True
    )
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    # Disable structural-actionability gate so simplified test findings (without
    # claims/sources/long descriptions) are not deferred as low_actionability.
    mocker.patch(
        "app.research_completion._apply_structural_actionability_downgrade",
        return_value=0,
    )
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "title": "Medium improvement",
                "description": "Some work",
                "prompt": "Do the work",
                "priority": "medium",
                "scope": "medium",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction_id,
            }
        ],
    )


@pytest.mark.asyncio
async def test_cochange_decomposition_gated_by_runtime_flag(db_session, mocker):
    """Co-change auto-decomposition runs only when cochange_decomposition_enabled
    is on. Default off, _handle_research_completion must not call
    auto_decompose_findings_by_cochange at all (live data: 0/14 sub-findings
    merged while ~3x-ing fan-out). Spying on the splitter isolates the call-site
    gate from finding-shape prerequisites."""
    import app.shared_state as _ss

    direction_id = "d-decompgate"
    _mock_escalation_externals(mocker, direction_id)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.9)
    spy = mocker.patch(
        "app.research_completion.auto_decompose_findings_by_cochange",
        side_effect=lambda dedup, *a, **k: dedup,
    )
    # patch.dict snapshots/restores the whole runtime_config around the test.
    mocker.patch.dict(_ss.runtime_config, {"cochange_decomposition_enabled": False})

    # OFF (default): the splitter is never invoked.
    _, task_off, _ = await _setup_escalation_project(db_session, direction_id, None)
    await _handle_research_completion(task_off, "", db_session)
    assert spy.call_count == 0, "decomposition must be skipped when flag is off"

    # ON: the splitter runs.
    _ss.runtime_config["cochange_decomposition_enabled"] = True
    _, task_on, _ = await _setup_escalation_project(db_session, direction_id, None)
    await _handle_research_completion(task_on, "", db_session)
    assert spy.call_count >= 1, "decomposition must run when flag is on"


@pytest.mark.asyncio
async def test_low_confidence_medium_auto_escalates_to_large(db_session, mocker):
    """Medium-scope low-confidence finding is escalated to large+plan; the
    low_confidence_spawn flag is preserved in context for tracking."""
    direction_id = "d-lowconf1"
    _, task, _ = await _setup_escalation_project(db_session, direction_id, None)
    _mock_escalation_externals(mocker, direction_id)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.15)
    # Disable exploration-bypass randomness so the escalation path is deterministic.
    mocker.patch.dict(
        "app.shared_state.runtime_config", {"medium_exploration_fraction": 0}
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )
    children = result.scalars().all()
    assert len(children) == 1
    child = children[0]
    assert child.context["scope"] == "large"
    assert child.context["scope_calibrated_from"] == "medium"
    assert child.context["low_confidence_spawn"] is True
    assert child.first_payload["type"] == "plan"


@pytest.mark.asyncio
async def test_exploration_floor_prevents_zero_collapse(db_session, mocker):
    """Directions with exploration_created>0 but exploration_merged=0 still get
    the exploration floor instead of collapsing to fraction=0 (death spiral).

    Uses exploration_created<3 so the _exploration_proven_unviable branch is
    NOT triggered — the floor should still apply in the early-data regime.
    """
    direction_id = "d-floor-1"
    direction = {
        "id": direction_id,
        "text": "Zero-merge medium exploration",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [
            {
                "scope_breakdown": {
                    "medium": {"exploration_created": 2, "exploration_merged": 0}
                },
            }
        ],
    }
    project = Project(
        name="Floor test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="Research: floor",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    _mock_escalation_externals(mocker, direction_id)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.15)
    # Force the random draw below the floor so the exploration path is taken
    # iff the floor is applied; without the floor, _explore_frac would be 0
    # and random.random() < 0 is always False regardless of the draw.
    mocker.patch("app.research_completion.random.random", return_value=0.01)

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )
    children = result.scalars().all()
    assert len(children) == 1
    assert children[0].context["scope"] == "medium"


@pytest.mark.asyncio
async def test_exploration_floor_skipped_when_proven_unviable(db_session, mocker):
    """Once a direction has 3+ exploration attempts with 0 merges, the
    exploration floor is skipped and the low-confidence scope escalates to
    large — continued exploration is waste once the data is informative."""
    direction_id = "d-unviable-1"
    direction = {
        "id": direction_id,
        "text": "Proven-unviable medium exploration",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [
            {
                "scope_breakdown": {
                    "medium": {"exploration_created": 5, "exploration_merged": 0}
                },
            }
        ],
    }
    project = Project(
        name="Unviable test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="Research: unviable",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    _mock_escalation_externals(mocker, direction_id)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.15)
    # Even with the random draw forced below the floor, the floor must be
    # skipped (proven-unviable branch) so escalation to large occurs.
    mocker.patch("app.research_completion.random.random", return_value=0.01)

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )
    children = result.scalars().all()
    assert len(children) == 1
    child = children[0]
    assert child.context["scope"] == "large"
    assert child.context["scope_calibrated_from"] == "medium"
    assert child.context["low_confidence_spawn"] is True
    assert child.first_payload["type"] == "plan"


@pytest.mark.asyncio
async def test_exploration_floor_skipped_when_pipeline_unviable(db_session, mocker):
    """When pipeline-wide medium merge rate is at/below the floor, the
    exploration floor is skipped even for a new direction with no history —
    pipeline evidence is sufficient proof of unviability."""
    from app.shared_state import runtime_config

    direction_id = "d-pipeline-unviable-1"
    direction = {
        "id": direction_id,
        "text": "New direction, no exploration history",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
    }
    project = Project(
        name="Pipeline unviable test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="Research: pipeline unviable",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    _mock_escalation_externals(mocker, direction_id)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.15)
    mocker.patch("app.research_completion.random.random", return_value=0.01)
    runtime_config["pipeline_scope_merge_rates"] = {"medium": 0.02}
    try:
        await _handle_research_completion(task, "", db_session)
    finally:
        runtime_config["pipeline_scope_merge_rates"] = {}

    result = await db_session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )
    children = result.scalars().all()
    assert len(children) == 1
    child = children[0]
    assert child.context["scope"] == "large"
    assert child.context["scope_calibrated_from"] == "medium:pipeline_floor"


@pytest.mark.asyncio
async def test_complexity_modulates_medium_exploration_fraction(db_session, mocker):
    """A low-complexity medium finding gets a halved exploration fraction
    compared to a high-complexity finding from the same direction. With
    base explore_frac=0.2 and random draw=0.15, the high-complexity finding
    stays medium (0.15 < 0.20) while the low-complexity finding (modulated
    to 0.10) escalates to large (0.15 >= 0.10)."""
    direction_id = "d-complexity-mod-1"
    direction = {
        "id": direction_id,
        "text": "Complexity modulation test",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
    }
    project = Project(
        name="Complexity mod test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="Research: complexity mod",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new=AsyncMock(return_value=("fake-token", "owner", "repo")),
    )
    mocker.patch(
        "app.task_completion.get_branch_sha", new=AsyncMock(return_value="abc123sha")
    )
    mocker.patch("app.task_completion.create_branch", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.research_completion._validate_plan_dependencies", return_value=True
    )
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch(
        "app.research_completion._apply_structural_actionability_downgrade",
        return_value=0,
    )
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "title": "Low-complexity finding",
                "description": "simple",
                "prompt": "do it",
                "priority": "medium",
                "scope": "medium",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction_id,
            },
            {
                "title": "High-complexity finding",
                "description": "hairy",
                "prompt": "do it",
                "priority": "medium",
                "scope": "medium",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction_id,
            },
        ],
    )

    def _fake_calibrate(finding, scope, direction, cochange_index, all_directions=None):
        if "Low-complexity" in finding.get("title", ""):
            finding["_complexity_score"] = 0.20
        else:
            finding["_complexity_score"] = 0.50
        finding["_complexity_signals"] = {}
        return "medium", None

    mocker.patch(
        "app.research_completion._calibrate_finding_scope",
        side_effect=_fake_calibrate,
    )
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.15)
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"medium_exploration_fraction": 0.2},
    )
    # Draw 0.15: passes for high-complexity (unmodulated 0.20), fails for
    # low-complexity (modulated 0.10).
    mocker.patch("app.research_completion.random.random", return_value=0.15)

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )
    children = result.scalars().all()
    by_title = {c.title: c for c in children}
    high = next(c for t, c in by_title.items() if "High-complexity" in t)
    low = next(c for t, c in by_title.items() if "Low-complexity" in t)
    assert high.context["scope"] == "medium"
    assert low.context["scope"] == "large"
    assert low.context["scope_calibrated_from"] == "medium"


@pytest.mark.asyncio
async def test_hard_skip_below_threshold_drops_finding(db_session, mocker):
    """Medium-scope finding with P(success) below medium_hard_skip_threshold is
    dropped entirely (no child task spawned) and recorded in deferred_findings
    with skip_reason='ultra_low_confidence'."""
    from app.shared_state import runtime_config

    direction_id = "d-hardskip-1"
    project, task, _ = await _setup_escalation_project(db_session, direction_id, None)
    _mock_escalation_externals(mocker, direction_id)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.05)
    runtime_config["medium_hard_skip_threshold"] = 0.10
    # Seed medium history so the direction is not data-starved (which would
    # bypass the hard-skip gate).
    project.directions[0]["research_history"] = [
        {"scope_breakdown": {"medium": {"tasks_created": 1, "prs_merged": 0}}}
    ]
    from sqlalchemy.orm.attributes import flag_modified as _fm1

    _fm1(project, "directions")
    await db_session.commit()
    try:
        await _handle_research_completion(task, "", db_session)
    finally:
        runtime_config.pop("medium_hard_skip_threshold", None)

    result = await db_session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )
    children = result.scalars().all()
    assert len(children) == 0
    await db_session.refresh(task)
    deferred = (task.context or {}).get("deferred_findings") or []
    assert len(deferred) == 1
    assert deferred[0]["skip_reason"] == "ultra_low_confidence"
    assert deferred[0]["deferred_reason"] == "ultra_low_confidence"
    skip_counts = (task.context or {}).get("skipped_low_confidence_by_scope") or {}
    assert skip_counts.get("medium") == 1


@pytest.mark.asyncio
async def test_hard_skip_above_threshold_runs_normal_path(db_session, mocker):
    """Finding above hard_skip threshold but below confidence_threshold still
    follows the normal exploration/escalation path."""
    from app.shared_state import runtime_config

    direction_id = "d-hardskip-2"
    project, task, _ = await _setup_escalation_project(db_session, direction_id, None)
    _mock_escalation_externals(mocker, direction_id)
    # 0.15 is above hard_skip (0.10) but below confidence_threshold (default 0.25)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.15)
    mocker.patch.dict(
        "app.shared_state.runtime_config", {"medium_exploration_fraction": 0}
    )
    runtime_config["medium_hard_skip_threshold"] = 0.10
    project.directions[0]["research_history"] = [
        {"scope_breakdown": {"medium": {"tasks_created": 1, "prs_merged": 0}}}
    ]
    from sqlalchemy.orm.attributes import flag_modified as _fm2

    _fm2(project, "directions")
    await db_session.commit()
    try:
        await _handle_research_completion(task, "", db_session)
    finally:
        runtime_config.pop("medium_hard_skip_threshold", None)

    result = await db_session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )
    children = result.scalars().all()
    assert len(children) == 1
    # Escalated to large+plan via the normal low-confidence path.
    assert children[0].context["scope"] == "large"
    assert children[0].context["scope_calibrated_from"] == "medium"
    await db_session.refresh(task)
    assert not (task.context or {}).get("skipped_low_confidence_by_scope")


@pytest.mark.asyncio
async def test_hard_skip_bypassed_for_data_starved_direction(db_session, mocker):
    """A direction with no medium scope_breakdown in its recent research_history
    is data-starved — the hard-skip gate is bypassed so the exploration lottery
    can run, breaking the self-reinforcing loop where medium is never attempted."""
    from app.shared_state import runtime_config

    direction_id = "d-hardskip-starved"
    _, task, _ = await _setup_escalation_project(db_session, direction_id, None)
    _mock_escalation_externals(mocker, direction_id)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.05)
    # Force exploration to fire so a child is spawned (otherwise the finding
    # still falls through to escalation, which also spawns a child).
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"medium_exploration_fraction": 1.0},
    )
    mocker.patch("app.research_completion.random.random", return_value=0.0)
    runtime_config["medium_hard_skip_threshold"] = 0.10
    try:
        await _handle_research_completion(task, "", db_session)
    finally:
        runtime_config.pop("medium_hard_skip_threshold", None)

    result = await db_session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )
    children = result.scalars().all()
    # A child was spawned (hard-skip was bypassed) and no deferral was recorded.
    assert len(children) == 1
    await db_session.refresh(task)
    skip_counts = (task.context or {}).get("skipped_low_confidence_by_scope") or {}
    assert skip_counts.get("medium", 0) == 0


@pytest.mark.asyncio
async def test_hard_skip_counter_in_scope_breakdown(db_session, mocker):
    """skipped_low_confidence counter from the parent research task surfaces in
    the per-direction scope_breakdown after _update_direction_research_outcomes
    runs."""
    from app.shared_state import runtime_config
    from app.direction_outcomes import _update_direction_research_outcomes

    direction_id = "d-hardskip-3"
    project, task, _ = await _setup_escalation_project(db_session, direction_id, None)
    _mock_escalation_externals(mocker, direction_id)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.05)
    runtime_config["medium_hard_skip_threshold"] = 0.10
    # Seed a research_history entry so _update_direction_research_outcomes finds it.
    # Includes medium scope_breakdown so the hard-skip gate is not bypassed for
    # data-starved directions.
    project.directions[0]["research_history"] = [
        {
            "task_id": str(task.id),
            "tasks_created": 0,
            "scope_breakdown": {"medium": {"tasks_created": 1, "prs_merged": 0}},
        }
    ]
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    try:
        await _handle_research_completion(task, "", db_session)
    finally:
        runtime_config.pop("medium_hard_skip_threshold", None)

    # Spawn at least one dummy child so the rebuild logic engages.
    dummy_child = Task(
        project_id=project.id,
        title="dummy",
        parent_task_id=task.id,
        payloads=[{"type": "create"}],
        context={"scope": "small", "direction_id": direction_id},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(dummy_child)
    await db_session.commit()

    await _update_direction_research_outcomes(
        project.id, direction_id, str(task.id), db_session
    )
    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    assert sb["medium"]["skipped_low_confidence"] == 1


@pytest.mark.asyncio
async def test_auto_escalate_suppressed_by_poor_decomp_history(db_session, mocker):
    """Medium-scope low-confidence finding stays at medium when prior plan
    decompositions merged poorly (shares suppression with _calibrate_scope)."""
    direction_id = "d-suppressed"
    _, task, _ = await _setup_escalation_project(
        db_session,
        direction_id,
        [{"subtask_merge_rate": 0.1, "subtask_count": 5}],
    )
    _mock_escalation_externals(mocker, direction_id)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.15)

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )
    children = result.scalars().all()
    assert len(children) == 1
    child = children[0]
    assert child.context["scope"] == "medium"
    assert child.context["low_confidence_spawn"] is True
    assert child.first_payload["type"] == "create"


@pytest.mark.asyncio
async def test_completion_suggestion_toggles_off_when_no_longer_complete(db_session):
    """A previously possibly_complete direction clears both flags when
    _check_direction_completion later returns None, and the cleared state
    is committed."""
    direction = {
        "id": "d-toggle-off-1",
        "text": "Direction that looked complete earlier",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        # Empty history → _check_direction_completion returns None (needs ≥4 entries)
        # and _check_direction_health returns None (no rules trip).
        "research_history": [],
        "possibly_complete": True,
        "completion_suggestion": (
            "Direction appears complete: 3 PRs merged across 4 cycles, "
            "last 2 cycles low-yield"
        ),
    }
    project = Project(
        name="Completion Toggle-Off",
        user_id="u-toggle",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "t"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    active = await _process_direction_lifecycle(
        project,
        db_session,
        busy_direction_ids=set(),
        now=_utcnow(),
    )

    # Direction stays active and eligible.
    assert len(active) == 1
    assert active[0]["id"] == "d-toggle-off-1"
    assert active[0]["status"] == "active"

    # Both flags removed from the direction dict.
    d = project.directions[0]
    assert "possibly_complete" not in d
    assert "completion_suggestion" not in d

    # directions_modified triggered a commit — the cleared state is persisted.
    await db_session.refresh(project)
    persisted = project.directions[0]
    assert "possibly_complete" not in persisted
    assert "completion_suggestion" not in persisted


def test_direction_refractory_helper_uses_adaptive_tiers_and_saturation_multiplier(
    mocker,
):
    """The shared refractory helper stamps the adaptive standard and saturation holds."""
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {
            "autonomous_direction_cooldown_minutes": 240,
            "productive_direction_cooldown_minutes": 45,
            "direction_refractory_minutes": None,
            "direction_saturation_refractory_minutes": None,
        },
        clear=False,
    )
    now = datetime(2026, 1, 1, 0, 0, 0)

    standard = {
        "last_cycle_productive": False,
        "consecutive_unproductive_cycles": 1,
    }
    stamp_direction_refractory(standard, now=now)
    assert datetime.fromisoformat(standard["refractory_until"]) == now + timedelta(
        minutes=90
    )
    standard_info = get_direction_refractory_info(standard, now=now)
    assert standard_info["refractory_reason"] == "standard"
    assert standard_info["refractory_remaining_minutes"] == 90

    productive = {
        "last_cycle_productive": True,
        "consecutive_unproductive_cycles": 0,
    }
    stamp_direction_refractory(productive, now=now)
    assert datetime.fromisoformat(productive["refractory_until"]) == now + timedelta(
        minutes=45
    )

    saturated = {
        "last_cycle_productive": False,
        "consecutive_unproductive_cycles": 1,
        "possibly_saturated": True,
    }
    stamp_direction_refractory(saturated, now=now)
    assert datetime.fromisoformat(saturated["refractory_until"]) == now + timedelta(
        minutes=180
    )
    saturated_info = get_direction_refractory_info(saturated, now=now)
    assert saturated_info["refractory_reason"] == "saturation"

    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {
            "autonomous_direction_cooldown_minutes": 240,
            "productive_direction_cooldown_minutes": 45,
            "direction_refractory_minutes": 360,
            "direction_saturation_refractory_minutes": None,
        },
        clear=False,
    )
    explicit_standard = {
        "last_cycle_productive": False,
        "consecutive_unproductive_cycles": 1,
    }
    stamp_direction_refractory(explicit_standard, now=now)
    assert datetime.fromisoformat(
        explicit_standard["refractory_until"]
    ) == now + timedelta(minutes=135)


# ─────────────────────────────────────────────────────────────────────────────
# integration_branch routing — manual operator endpoints (R4-MEDIUM)
#
# The manual /research and /strategic-cycle endpoints produce the SAME task
# types as the autonomous strategic/research spawns. Their spawn-time
# base_branch default MUST route through the project integration_branch column
# so that after the automain Phase-1 cutover an operator-triggered cycle
# drives the version lifecycle from automain, not the frozen LTS main.
# ─────────────────────────────────────────────────────────────────────────────


async def _make_strategic_project(db_session, name: str):
    """Project with owner membership so trigger_strategic_cycle's role check
    (_get_user_project_with_role, min_role='member') passes."""
    from app.models import ProjectMember

    project = Project(
        name=name,
        user_id="test-user-123",
        vision={"purpose": "test"},
        design_doc={"proposed_changes": []},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    )
    await db_session.commit()
    return project


@pytest.mark.asyncio
async def test_research_endpoint_uses_integration_branch(
    client: AsyncClient, db_session, mocker
):
    """When the project integration_branch is 'automain', /research must spawn with that base branch."""
    project_id = await _deploy_project(client, mocker)
    project = await db_session.get(Project, uuid.UUID(project_id))
    project.integration_branch = "automain"
    await db_session.commit()
    direction = await _add_direction(client, project_id, text="Routed research")
    direction_id = direction["id"]

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
    )
    assert resp.status_code == 200, resp.text

    # _deploy_project also creates a payload-less "Deploy ..." parent task with
    # created_by="USER", so filter down to the research task by payload type.
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == uuid.UUID(project_id),
            Task.created_by == "USER",
        )
    )
    research_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research_tasks) == 1, "expected exactly one research task to be created"
    for t in research_tasks:
        assert t.first_payload.get("base_branch") == "automain", (
            "manual /research endpoint bypassed the integration_branch column"
        )


@pytest.mark.asyncio
async def test_research_endpoint_defaults_to_main(
    client: AsyncClient, db_session, mocker
):
    """With no project override, the manual /research endpoint spawns from 'main'."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Default research")
    direction_id = direction["id"]

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
    )
    assert resp.status_code == 200, resp.text

    # _deploy_project also creates a payload-less "Deploy ..." parent task with
    # created_by="USER", so filter down to the research task by payload type.
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == uuid.UUID(project_id),
            Task.created_by == "USER",
        )
    )
    research_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research_tasks) == 1, "expected exactly one research task to be created"
    for t in research_tasks:
        assert t.first_payload.get("base_branch") == "main"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase,expected_type",
    [
        ("design_doc", "design_doc_update"),
        ("strategic_review", "strategic_review"),
    ],
)
async def test_strategic_cycle_endpoint_uses_integration_branch(
    client: AsyncClient, db_session, phase, expected_type
):
    """When the project integration_branch is 'automain', both phases must use that base branch."""
    project = await _make_strategic_project(db_session, f"automain strategic {phase}")
    project.integration_branch = "automain"
    await db_session.commit()

    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-cycle",
        params={"phase": phase},
    )
    assert resp.status_code == 200, resp.text

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    strategic_tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == expected_type
    ]
    assert len(strategic_tasks) == 1, (
        f"expected exactly one {expected_type} task to be created"
    )
    for t in strategic_tasks:
        assert t.first_payload.get("base_branch") == "automain", (
            f"manual /strategic-cycle ({phase}) bypassed the integration_branch column"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["design_doc", "strategic_review"])
async def test_strategic_cycle_endpoint_defaults_to_main(
    client: AsyncClient, db_session, phase
):
    """With no project override, both phases of the manual /strategic-cycle endpoint spawn from 'main'."""
    project = await _make_strategic_project(db_session, f"main strategic {phase}")

    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-cycle",
        params={"phase": phase},
    )
    assert resp.status_code == 200, resp.text

    expected_type = (
        "design_doc_update" if phase == "design_doc" else "strategic_review"
    )
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    strategic_tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == expected_type
    ]
    assert len(strategic_tasks) == 1, (
        f"expected exactly one {expected_type} task to be created"
    )
    for t in strategic_tasks:
        assert t.first_payload.get("base_branch") == "main"


@pytest.mark.asyncio
async def test_manual_strategic_cycle_ignores_exhausted_initiative_budget(
    client: AsyncClient, db_session
):
    """The operator /strategic-cycle path should not inherit the autonomous initiative cap."""
    project = await _make_strategic_project(db_session, "manual strategic budget")
    project.build_config = {"bootstrap_completed_at": "2026-01-01T00:00:00"}
    await db_session.commit()

    budget_source = Project(name="budget source", user_id="test-user-123")
    db_session.add(budget_source)
    await db_session.commit()
    db_session.add(
        Task(
            project_id=budget_source.id,
            title="Budget source task",
            payloads=[{"type": "research"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
    )
    await db_session.commit()

    from app.shared_state import runtime_config as _runtime_config

    original_mode = _runtime_config["autonomous_spawn_budget_mode"]
    original_initiative = _runtime_config[
        "max_global_autonomous_initiative_tasks_per_hour"
    ]
    original_global = _runtime_config["max_global_autonomous_tasks_per_hour"]
    _runtime_config["autonomous_spawn_budget_mode"] = "enforce"
    _runtime_config["max_global_autonomous_initiative_tasks_per_hour"] = 1
    _runtime_config["max_global_autonomous_tasks_per_hour"] = 500
    try:
        resp = await client.post(
            f"/api/admin/projects/{project.id}/strategic-cycle",
            params={"phase": "design_doc"},
        )
        assert resp.status_code == 200, resp.text
    finally:
        _runtime_config["autonomous_spawn_budget_mode"] = original_mode
        _runtime_config["max_global_autonomous_initiative_tasks_per_hour"] = original_initiative
        _runtime_config["max_global_autonomous_tasks_per_hour"] = original_global


@pytest.mark.asyncio
async def test_manual_strategic_review_ignores_exhausted_global_budget(
    client: AsyncClient, db_session
):
    """The operator /strategic-cycle strategic_review path should ignore the autonomous backstop."""
    project = await _make_strategic_project(db_session, "manual strategic review budget")
    project.build_config = {"bootstrap_completed_at": "2026-01-01T00:00:00"}
    await db_session.commit()

    budget_source = Project(name="budget source global", user_id="test-user-123")
    db_session.add(budget_source)
    await db_session.commit()
    db_session.add(
        Task(
            project_id=budget_source.id,
            title="Budget source task",
            payloads=[{"type": "research"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
    )
    await db_session.commit()

    from app.shared_state import runtime_config as _runtime_config

    original_mode = _runtime_config["autonomous_spawn_budget_mode"]
    original_initiative = _runtime_config[
        "max_global_autonomous_initiative_tasks_per_hour"
    ]
    original_global = _runtime_config["max_global_autonomous_tasks_per_hour"]
    _runtime_config["autonomous_spawn_budget_mode"] = "observe"
    _runtime_config["max_global_autonomous_initiative_tasks_per_hour"] = 500
    _runtime_config["max_global_autonomous_tasks_per_hour"] = 1
    try:
        resp = await client.post(
            f"/api/admin/projects/{project.id}/strategic-cycle",
            params={"phase": "strategic_review"},
        )
        assert resp.status_code == 200, resp.text
    finally:
        _runtime_config["autonomous_spawn_budget_mode"] = original_mode
        _runtime_config["max_global_autonomous_initiative_tasks_per_hour"] = original_initiative
        _runtime_config["max_global_autonomous_tasks_per_hour"] = original_global


def test_no_hardcoded_main_base_branch_in_directions_router():
    """Static guard: spawn payloads in routers/directions.py must never
    hardcode {"base_branch": "main"} — they must route through
    the project integration_branch column so the automain cutover
    takes effect. Inheritance reads `.get("base_branch", "main")` (a "(" or
    "," precedes the key) are a different, correct pattern and are allowed.
    """
    import re
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "app" / "routers" / "directions.py"
    hardcoded = re.compile(r'(?<![(,]\s)"base_branch":\s*"main"')
    offenders = [
        f"routers/directions.py:{lineno}: {line.strip()}"
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if hardcoded.search(line)
    ]
    assert not offenders, (
        "routers/directions.py spawn payloads hardcode base_branch='main' "
        "instead of routing through integration_branch:\n" + "\n".join(offenders)
    )


# ─────────────────────────────────────────────────────────────────────────────
# 10. success_evidence and retirement_condition (context fields)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_direction_set_success_evidence(client: AsyncClient, mocker):
    """PATCH sets success_evidence in direction.context."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Auth direction")

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"success_evidence": "SAML login flow passes end-to-end"},
    )
    assert resp.status_code == 200, resp.text
    d = resp.json()["direction"]
    ctx = d.get("context") or {}
    assert ctx.get("success_evidence") == "SAML login flow passes end-to-end"


@pytest.mark.asyncio
async def test_update_direction_set_retirement_condition(client: AsyncClient, mocker):
    """PATCH sets retirement_condition in direction.context."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Auth direction")

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"retirement_condition": "3 consecutive cycles with zero failures"},
    )
    assert resp.status_code == 200, resp.text
    d = resp.json()["direction"]
    ctx = d.get("context") or {}
    assert ctx.get("retirement_condition") == "3 consecutive cycles with zero failures"


@pytest.mark.asyncio
async def test_update_direction_set_both_context_fields(client: AsyncClient, mocker):
    """PATCH sets both success_evidence and retirement_condition together."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Logging direction")

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={
            "success_evidence": "All request logging captures method, path, status",
            "retirement_condition": "All services logging uniformly for 2 cycles",
        },
    )
    assert resp.status_code == 200, resp.text
    d = resp.json()["direction"]
    ctx = d.get("context") or {}
    assert (
        ctx.get("success_evidence")
        == "All request logging captures method, path, status"
    )
    assert (
        ctx.get("retirement_condition") == "All services logging uniformly for 2 cycles"
    )


@pytest.mark.asyncio
async def test_update_direction_context_fields_persist_across_reads(
    client: AsyncClient, mocker
):
    """Context fields persist when re-reading direction via GET."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Persistence test")
    direction_id = direction["id"]

    # Set context fields
    await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={
            "success_evidence": "Metrics endpoint responds in <100ms",
            "retirement_condition": "P99 latency below threshold for 30 days",
        },
    )

    # Read back and verify
    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    directions = resp.json()["directions"]
    target = next((d for d in directions if d["id"] == direction_id), None)
    assert target is not None
    ctx = target.get("context") or {}
    assert ctx.get("success_evidence") == "Metrics endpoint responds in <100ms"
    assert ctx.get("retirement_condition") == "P99 latency below threshold for 30 days"


@pytest.mark.asyncio
async def test_update_direction_clear_success_evidence(client: AsyncClient, mocker):
    """PATCH clears success_evidence when set to null."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Clearable direction")

    # Set it first
    await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"success_evidence": "Initial evidence"},
    )

    # Clear it
    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"success_evidence": None},
    )
    assert resp.status_code == 200
    d = resp.json()["direction"]
    ctx = d.get("context") or {}
    assert "success_evidence" not in ctx


@pytest.mark.asyncio
async def test_update_direction_clear_retirement_condition(client: AsyncClient, mocker):
    """PATCH clears retirement_condition when set to null."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Clearable direction")

    # Set it first
    await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"retirement_condition": "Initial condition"},
    )

    # Clear it
    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"retirement_condition": None},
    )
    assert resp.status_code == 200
    d = resp.json()["direction"]
    ctx = d.get("context") or {}
    assert "retirement_condition" not in ctx


@pytest.mark.asyncio
async def test_update_direction_clear_both_context_fields(client: AsyncClient, mocker):
    """PATCH clears both context fields when set to null."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Two-field direction")

    # Set both
    await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={
            "success_evidence": "Field A",
            "retirement_condition": "Field B",
        },
    )

    # Clear both
    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={
            "success_evidence": None,
            "retirement_condition": None,
        },
    )
    assert resp.status_code == 200
    d = resp.json()["direction"]
    ctx = d.get("context") or {}
    assert "success_evidence" not in ctx
    assert "retirement_condition" not in ctx


@pytest.mark.asyncio
async def test_update_direction_clear_refractory_until(client: AsyncClient, mocker):
    """PATCH clears refractory_until when set to null."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Hold-clear direction")

    future_ts = (_utcnow() + timedelta(minutes=30)).isoformat()
    await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"refractory_until": future_ts},
    )

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"refractory_until": None},
    )
    assert resp.status_code == 200
    d = resp.json()["direction"]
    assert "refractory_until" not in d


@pytest.mark.asyncio
async def test_update_direction_context_fields_independent(client: AsyncClient, mocker):
    """Updating one context field doesn't affect the other."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Independence test")
    direction_id = direction["id"]

    # Set both
    await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={
            "success_evidence": "Evidence value",
            "retirement_condition": "Condition value",
        },
    )

    # Update only success_evidence
    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={"success_evidence": "Updated evidence"},
    )
    assert resp.status_code == 200
    d = resp.json()["direction"]
    ctx = d.get("context") or {}
    assert ctx.get("success_evidence") == "Updated evidence"
    assert ctx.get("retirement_condition") == "Condition value"

    # Update only retirement_condition
    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={"retirement_condition": "Updated condition"},
    )
    assert resp.status_code == 200
    d = resp.json()["direction"]
    ctx = d.get("context") or {}
    assert ctx.get("success_evidence") == "Updated evidence"
    assert ctx.get("retirement_condition") == "Updated condition"


@pytest.mark.asyncio
async def test_update_direction_success_evidence_validates_type(
    client: AsyncClient, mocker
):
    """PATCH rejects non-string success_evidence."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"success_evidence": 123},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_direction_retirement_condition_validates_type(
    client: AsyncClient, mocker
):
    """PATCH rejects non-string retirement_condition."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"retirement_condition": {"nested": "object"}},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_direction_context_fields_with_other_updates(
    client: AsyncClient, mocker
):
    """Context fields can be updated together with other direction fields."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(
        client, project_id, text="Multi-update", priority="low"
    )

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={
            "text": "Updated text",
            "priority": "high",
            "success_evidence": "New evidence",
            "retirement_condition": "New condition",
        },
    )
    assert resp.status_code == 200, resp.text
    d = resp.json()["direction"]
    assert d["text"] == "Updated text"
    assert d["priority"] == "high"
    ctx = d.get("context") or {}
    assert ctx.get("success_evidence") == "New evidence"
    assert ctx.get("retirement_condition") == "New condition"


@pytest.mark.asyncio
async def test_get_directions_includes_context_fields_when_set(
    client: AsyncClient, mocker
):
    """GET /directions includes context object with success_evidence/retirement_condition."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Context test")

    # Set the context fields
    await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={
            "success_evidence": "All tests passing",
            "retirement_condition": "Zero failures for 4 weeks",
        },
    )

    # Fetch and verify they appear in the list response
    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    directions = resp.json()["directions"]
    target = next((d for d in directions if d["id"] == direction["id"]), None)
    assert target is not None
    assert "context" in target
    ctx = target["context"]
    assert ctx.get("success_evidence") == "All tests passing"
    assert ctx.get("retirement_condition") == "Zero failures for 4 weeks"


@pytest.mark.asyncio
async def test_create_direction_with_acceptance_evidence(client: AsyncClient, mocker):
    """POST can author success_evidence/retirement_condition at create time;
    they round-trip into context exactly as the PATCH path stores them."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={
            "text": "Author acceptance at create",
            "priority": "high",
            "success_evidence": "Operators see truthful status labels",
            "retirement_condition": "One promotion cycle runs without workarounds",
        },
    )
    assert resp.status_code == 200, resp.text
    d = resp.json()["direction"]
    ctx = d.get("context") or {}
    assert ctx.get("success_evidence") == "Operators see truthful status labels"
    assert (
        ctx.get("retirement_condition")
        == "One promotion cycle runs without workarounds"
    )

    # Round-trips through GET inside context, same shape PATCH produces.
    resp_list = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp_list.status_code == 200
    target = next(
        (x for x in resp_list.json()["directions"] if x["id"] == d["id"]), None
    )
    assert target is not None
    assert target["context"]["success_evidence"] == (
        "Operators see truthful status labels"
    )
    assert target["context"]["retirement_condition"] == (
        "One promotion cycle runs without workarounds"
    )


@pytest.mark.asyncio
async def test_create_direction_without_acceptance_evidence_has_no_empty_context(
    client: AsyncClient, mocker
):
    """POST omitting the acceptance fields introduces no empty context entry."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "No acceptance authored", "priority": "medium"},
    )
    assert resp.status_code == 200, resp.text
    d = resp.json()["direction"]
    # No context key at all (or, if present, it carries neither acceptance key).
    ctx = d.get("context")
    assert ctx is None or (
        "success_evidence" not in ctx and "retirement_condition" not in ctx
    )

    resp_list = await client.get(f"/api/admin/projects/{project_id}/directions")
    target = next(
        (x for x in resp_list.json()["directions"] if x["id"] == d["id"]), None
    )
    assert target is not None
    ctx = target.get("context")
    assert ctx is None or (
        "success_evidence" not in ctx and "retirement_condition" not in ctx
    )


@pytest.mark.asyncio
async def test_create_direction_acceptance_evidence_revisable_via_patch(
    client: AsyncClient, mocker
):
    """A direction authored with acceptance fields at create can still be
    revised by a subsequent PATCH."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={
            "text": "Revisable acceptance",
            "success_evidence": "Initial evidence",
            "retirement_condition": "Initial condition",
        },
    )
    assert resp.status_code == 200, resp.text
    direction_id = resp.json()["direction"]["id"]

    patch_resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={"success_evidence": "Revised evidence"},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    ctx = patch_resp.json()["direction"].get("context") or {}
    assert ctx.get("success_evidence") == "Revised evidence"
    # Untouched field from create is preserved.
    assert ctx.get("retirement_condition") == "Initial condition"


@pytest.mark.asyncio
async def test_create_direction_rejects_oversized_acceptance_evidence(
    client: AsyncClient, mocker
):
    """POST enforces the same 500-char cap the PATCH path applies."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={
            "text": "Oversized evidence",
            "success_evidence": "x" * 501,
        },
    )
    assert resp.status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# 11. Directions-lock invariant
# ─────────────────────────────────────────────────────────────────────────────


class TestDirectionsRouterLockRatchet:
    """Positive ratchet: routers/directions.py must keep its direction writes
    routed through with_directions_locked.

    Complements TestDirectionsLockInvariants in test_task_locking.py (negative
    ratchet — catches new direct .directions = assignments in *new* files).
    This positive ratchet catches the mirror failure: a with_directions_locked
    call site is removed from the router without being replaced, so the
    operation silently regresses to an unlocked write.

    _EXPECTED_MIN_CALLS is set to 5 (the five mutation sites active at the
    time this PR was written — the ratchet was originally set to 6 in PR #5811
    but audit of routers/directions.py confirmed only 5 call sites exist; the
    constant is corrected here). It must not decrease. If a call site is
    removed, either add a replacement locked call or lower this constant in the
    same PR with an explanation.
    """

    _EXPECTED_MIN_CALLS = 5

    def test_directions_router_locked_call_count(self):
        """routers/directions.py must have at least 6 with_directions_locked call sites."""
        import ast
        from pathlib import Path

        router_path = (
            Path(__file__).resolve().parents[1] / "app" / "routers" / "directions.py"
        )
        source = router_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(router_path))
        call_count = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "with_directions_locked"
        )
        assert call_count >= self._EXPECTED_MIN_CALLS, (
            f"routers/directions.py has only {call_count} call(s) to "
            f"with_directions_locked (expected >= {self._EXPECTED_MIN_CALLS}). "
            "All direction mutation sites in the router must remain routed through "
            "the canonical locked helper — do not remove calls without replacing them "
            "or lowering _EXPECTED_MIN_CALLS with an explanation in the PR."
        )


# ── Contract-reset integration tests (operator PATCH path) ────────────────────
#
# Each test seeds a direction with all 8 stale outcome-detection fields, then
# mutates ONE contract field through the operator PATCH endpoint, and asserts:
#   (a) the new contract value is persisted, and
#   (b) all outcome-detection state is reset to a clean baseline.
#
# The reset guard lives at routers/directions.py lines 706-715.  These tests
# provide end-to-end coverage so a future writer cannot remove the guard
# without at least one test failing.


_STALE_OUTCOME_DIRECTION = {
    "id": "d-contract-reset",
    "text": "Original direction text",
    "status": "active",
    "priority": "medium",
    "paths": ["src/old/"],
    "added_at": "2026-01-01T00:00:00",
    "context": {
        "success_evidence": "Old success criteria",
        "retirement_condition": "Old retirement condition",
    },
    # All 8 stale outcome-detection keys
    "outcome_progress_history": [{"progress": 5, "success_evidence_fp": "stale-fp"}],
    "outcome_progress_ever_met": True,
    "evidence_progress": {"status": "advancing"},
    "possibly_complete": True,
    "completion_suggestion": "Looks almost done",
    "outcome_scaffolding_flagged": True,
    "outcome_scaffolding_flagged_at": "2026-01-01T00:00:00",
    "outcome_scaffolding_flagged_cycles": 3,
}

_OUTCOME_DETECTION_OPTIONAL_KEYS = (
    "outcome_progress_ever_met",
    "evidence_progress",
    "possibly_complete",
    "completion_suggestion",
    "outcome_scaffolding_flagged",
    "outcome_scaffolding_flagged_at",
    "outcome_scaffolding_flagged_cycles",
)


async def _seed_stale_direction(db_session, project_id: str) -> None:
    """Replace the project's directions list with a single stale-state direction.

    Called after _deploy_project to inject stale outcome-detection state so
    the operator PATCH endpoint has something to reset.
    """
    import copy
    import uuid as _uuid
    from sqlalchemy import select as _sa_select
    from sqlalchemy.orm.attributes import flag_modified
    from app.models import Project as _Project

    result = await db_session.execute(
        _sa_select(_Project).where(_Project.id == _uuid.UUID(project_id))
    )
    project = result.scalar_one()
    project.directions = [copy.deepcopy(_STALE_OUTCOME_DIRECTION)]
    flag_modified(project, "directions")
    await db_session.commit()
    await db_session.refresh(project)


def _assert_outcome_detection_reset(d: dict) -> None:
    """Assert that all outcome-detection state is at a clean baseline."""
    assert d["outcome_progress_history"] == [], (
        "outcome_progress_history must be emptied (set to []) by the reset"
    )
    for key in _OUTCOME_DETECTION_OPTIONAL_KEYS:
        assert key not in d, (
            f"{key} must be removed from direction by the contract-reset guard"
        )


@pytest.mark.asyncio
async def test_update_direction_text_resets_outcome_detection_state(
    client: AsyncClient, db_session, mocker
):
    """PATCH text triggers _reset_outcome_detection_state on the direction.

    Operator updating the direction goal text is a contract change: the
    success criteria may now mean something different, so all accumulated
    outcome-detection state must be cleared.
    """
    project_id = await _deploy_project(client, mocker)
    await _seed_stale_direction(db_session, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/d-contract-reset",
        json={"text": "Rewritten direction text"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["direction"]["text"] == "Rewritten direction text"

    from sqlalchemy import select as _sa_select
    import uuid as _uuid
    from app.models import Project as _Project

    result = await db_session.execute(
        _sa_select(_Project).where(_Project.id == _uuid.UUID(project_id))
    )
    project = result.scalar_one()
    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-contract-reset")
    assert d["text"] == "Rewritten direction text", "contract field must be updated"
    _assert_outcome_detection_reset(d)


@pytest.mark.asyncio
async def test_update_direction_paths_resets_outcome_detection_state(
    client: AsyncClient, db_session, mocker
):
    """PATCH paths triggers _reset_outcome_detection_state on the direction.

    Changing the file-scope paths is a contract change because it redefines
    what evidence is relevant, invalidating prior outcome snapshots.
    """
    project_id = await _deploy_project(client, mocker)
    await _seed_stale_direction(db_session, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/d-contract-reset",
        json={"paths": ["src/new/", "tests/new/"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["direction"]["paths"] == ["src/new/", "tests/new/"]

    from sqlalchemy import select as _sa_select
    import uuid as _uuid
    from app.models import Project as _Project

    result = await db_session.execute(
        _sa_select(_Project).where(_Project.id == _uuid.UUID(project_id))
    )
    project = result.scalar_one()
    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-contract-reset")
    assert d["paths"] == ["src/new/", "tests/new/"], "paths must be updated"
    _assert_outcome_detection_reset(d)


@pytest.mark.asyncio
async def test_update_direction_completion_criteria_resets_outcome_detection_state(
    client: AsyncClient, db_session, mocker
):
    """PATCH completion_criteria triggers _reset_outcome_detection_state.

    Rewriting the completion criteria changes the contract by which the system
    measures whether the direction is done.
    """
    project_id = await _deploy_project(client, mocker)
    await _seed_stale_direction(db_session, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/d-contract-reset",
        json={"completion_criteria": "All integration tests green for 3 cycles"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["direction"]["completion_criteria"] == (
        "All integration tests green for 3 cycles"
    )

    from sqlalchemy import select as _sa_select
    import uuid as _uuid
    from app.models import Project as _Project

    result = await db_session.execute(
        _sa_select(_Project).where(_Project.id == _uuid.UUID(project_id))
    )
    project = result.scalar_one()
    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-contract-reset")
    assert d.get("completion_criteria") == "All integration tests green for 3 cycles"
    _assert_outcome_detection_reset(d)


@pytest.mark.asyncio
async def test_update_direction_success_evidence_resets_outcome_detection_state(
    client: AsyncClient, db_session, mocker
):
    """PATCH success_evidence triggers _reset_outcome_detection_state.

    success_evidence is the primary fingerprint used by the outcome scaffolding
    detector.  Changing it invalidates all accumulated fingerprinted snapshots.
    """
    project_id = await _deploy_project(client, mocker)
    await _seed_stale_direction(db_session, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/d-contract-reset",
        json={"success_evidence": "All SAML login flows pass e2e for 2 cycles"},
    )
    assert resp.status_code == 200, resp.text
    ctx = resp.json()["direction"].get("context") or {}
    assert ctx.get("success_evidence") == "All SAML login flows pass e2e for 2 cycles"

    from sqlalchemy import select as _sa_select
    import uuid as _uuid
    from app.models import Project as _Project

    result = await db_session.execute(
        _sa_select(_Project).where(_Project.id == _uuid.UUID(project_id))
    )
    project = result.scalar_one()
    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-contract-reset")
    ctx = d.get("context") or {}
    assert ctx.get("success_evidence") == "All SAML login flows pass e2e for 2 cycles"
    _assert_outcome_detection_reset(d)


@pytest.mark.asyncio
async def test_update_direction_retirement_condition_resets_outcome_detection_state(
    client: AsyncClient, db_session, mocker
):
    """PATCH retirement_condition triggers _reset_outcome_detection_state.

    Changing when the direction should be retired is a contract change that
    may make previously-met conditions irrelevant.
    """
    project_id = await _deploy_project(client, mocker)
    await _seed_stale_direction(db_session, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/d-contract-reset",
        json={"retirement_condition": "Zero auth failures for 3 consecutive cycles"},
    )
    assert resp.status_code == 200, resp.text
    ctx = resp.json()["direction"].get("context") or {}
    assert (
        ctx.get("retirement_condition") == "Zero auth failures for 3 consecutive cycles"
    )

    from sqlalchemy import select as _sa_select
    import uuid as _uuid
    from app.models import Project as _Project

    result = await db_session.execute(
        _sa_select(_Project).where(_Project.id == _uuid.UUID(project_id))
    )
    project = result.scalar_one()
    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-contract-reset")
    ctx = d.get("context") or {}
    assert (
        ctx.get("retirement_condition") == "Zero auth failures for 3 consecutive cycles"
    )
    _assert_outcome_detection_reset(d)


@pytest.mark.asyncio
async def test_update_direction_clears_scaffolding_state_on_outcome_change(
    client: AsyncClient, db_session, mocker
):
    """PATCH clears stale outcome-progress state when success_evidence changes."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Scaffolding direction")
    direction_id = direction["id"]

    # Inject stale scaffolding state directly into the DB
    result = await db_session.execute(
        select(Project).where(Project.id == uuid.UUID(project_id))
    )
    project = result.scalar_one()
    d = next(x for x in project.directions if x.get("id") == direction_id)
    d["outcome_progress_ever_met"] = True
    d["outcome_progress_history"] = [{"progress": 7, "cycle_index": 10}]
    d["evidence_progress"] = {"status": "advancing", "rationale": "stale"}
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    # PATCH with a new success_evidence — should trigger scaffolding reset
    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={"success_evidence": "New goal: zero P0 incidents"},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()["direction"]
    assert "outcome_progress_ever_met" not in updated
    # Either popped (scaffolding reset) or reset to [] (outcome-detection reset).
    # Both forms mean "cleared" and consumers read via `... or []`.
    assert not updated.get("outcome_progress_history")
    assert "evidence_progress" not in updated


@pytest.mark.asyncio
async def test_update_direction_preserves_scaffolding_state_when_outcome_unchanged(
    client: AsyncClient, db_session, mocker
):
    """PATCH leaves outcome-progress state intact when outcome fields do not change."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Stable direction")
    direction_id = direction["id"]

    # Set success_evidence via API first so it has a known value
    await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={"success_evidence": "Existing goal"},
    )

    # Inject progress state into the DB
    result = await db_session.execute(
        select(Project).where(Project.id == uuid.UUID(project_id))
    )
    project = result.scalar_one()
    d = next(x for x in project.directions if x.get("id") == direction_id)
    d["outcome_progress_ever_met"] = True
    from sqlalchemy.orm.attributes import flag_modified as _fm

    _fm(project, "directions")
    db_session.add(project)
    await db_session.commit()

    # PATCH with the same success_evidence — outcome is unchanged, state must survive
    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={"success_evidence": "Existing goal"},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()["direction"]
    assert updated.get("outcome_progress_ever_met") is True


# ─────────────────────────────────────────────────────────────────────────────
# Operator message wakes the strategist (coalescing strategic-refresh trigger)
# ─────────────────────────────────────────────────────────────────────────────


async def _reload_vision(db_session, project_id: str) -> dict:
    """Reload a project's vision dict from a fresh read of the DB."""
    db_session.expire_all()
    result = await db_session.execute(
        select(Project).where(Project.id == uuid.UUID(project_id))
    )
    project = result.scalar_one()
    return project.vision or {}


@pytest.mark.asyncio
async def test_operator_message_arms_strategic_refresh(
    client: AsyncClient, db_session, mocker
):
    """A steering-class operator message arms the coalescing refresh marker."""
    from app.strategic_trigger import strategic_refresh_pending

    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/messages",
        json={"content": "Please refocus on the auth bug", "message_type": "FEEDBACK"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "created"
    # The CLI endpoint reports the truthful arming outcome identically to the
    # canonical live-chat endpoint, so the two paths cannot diverge.
    assert resp.json()["arming"] == "steering"

    vision = await _reload_vision(db_session, project_id)
    assert strategic_refresh_pending(vision) is True
    marker = vision["_strategic_refresh"]
    assert marker["reason"] == "operator_message"
    assert marker["count"] == 1
    # The producer must stamp operator origin so the breaker-bypass and
    # deferral-notice consumer paths actually activate (the whole point of the
    # live-steering feature). Without this wiring the marker defaulted to
    # qualification origin and the feature was dead in production.
    assert marker["origin"] == "operator"


@pytest.mark.asyncio
async def test_operator_message_idempotency_key_dedupes(
    client: AsyncClient, db_session, mocker
):
    """Mirrors the live-chat idempotency test: a double-send of the same
    steering instruction with one idempotency key collapses to a single
    message row and a single strategist arming, instead of duplicating the
    operator message and re-quoting it as the priority directive."""
    from sqlalchemy import func as sa_func
    from app.models import ProjectMessage

    project_id = await _deploy_project(client, mocker)
    body = {
        "content": "Please refocus on the auth bug",
        "message_type": "FEEDBACK",
        "idempotency_key": "key-double-submit-1",
    }

    first = await client.post(f"/api/admin/projects/{project_id}/messages", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "created"
    assert first.json()["arming"] == "steering"
    first_id = first.json()["id"]

    # Identical retry with the same key — idempotent success, same row, and the
    # duplicate does NOT re-arm the strategist.
    second = await client.post(f"/api/admin/projects/{project_id}/messages", json=body)
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "duplicate"
    assert second.json()["id"] == first_id
    assert second.json()["arming"] == "duplicate"

    # Exactly one row landed for this project.
    count = await db_session.scalar(
        select(sa_func.count())
        .select_from(ProjectMessage)
        .where(ProjectMessage.project_id == uuid.UUID(project_id))
    )
    assert count == 1

    # Exactly one arming: the marker coalesced count stays at 1.
    vision = await _reload_vision(db_session, project_id)
    assert vision["_strategic_refresh"]["count"] == 1

    # A different key on the same content does create a second message.
    third = await client.post(
        f"/api/admin/projects/{project_id}/messages",
        json={**body, "idempotency_key": "key-double-submit-2"},
    )
    assert third.status_code == 200, third.text
    assert third.json()["status"] == "created"
    assert third.json()["id"] != first_id


@pytest.mark.asyncio
async def test_project_scoped_and_canonical_messages_share_workbench_capture(
    client: AsyncClient, db_session, mocker
):
    """The legacy project-scoped endpoint uses the durable operator create path.

    Workbench state persistence and sanitized strategic-session snapshots are
    distinctive shared-helper behavior; keeping both endpoints aligned here
    prevents the legacy route from growing a second create flow again.
    """

    async def _stored_message_context(message_id: str) -> dict:
        msg = await db_session.get(ProjectMessage, uuid.UUID(message_id))
        assert msg is not None
        return msg.context or {}

    async def _stored_workbench(project_id: str) -> dict:
        prefs = (
            await db_session.execute(
                select(MessagesNoisePrefs).where(
                    MessagesNoisePrefs.project_id == uuid.UUID(project_id),
                    MessagesNoisePrefs.user_id == "test-user-123",
                )
            )
        ).scalar_one()
        return prefs.prefs["_strategic_workbench"]

    project_scoped_id = await _deploy_project(client, mocker, "Legacy message project")
    canonical_id = await _deploy_project(client, mocker, "Canonical message project")
    workbench_context = {
        "interaction_mode": "invalid",
        "active_surface": "runtime",
        "session_started_at": "2026-06-29T12:00:00Z",
        "context_package_id": "strategic-context:shared",
        "selected_refs": [
            {"kind": "canvas", "id": "strategic-canvas:shared", "label": "Context map"},
            {"kind": "canvas", "id": "strategic-canvas:shared", "label": "Duplicate"},
            {
                "kind": "artifact",
                "id": "artifact-1",
                "label": "x" * 500,
                "storage_uri": "gs://secret",
            },
        ],
        "storage_uri": "gs://secret",
    }

    legacy_resp = await client.post(
        f"/api/admin/projects/{project_scoped_id}/messages",
        json={
            "content": "Keep this runtime context attached.",
            "message_type": "FEEDBACK",
            "workbench_context": workbench_context,
        },
    )
    canonical_resp = await client.post(
        "/api/admin/messages",
        json={
            "project_id": canonical_id,
            "content": "Keep this runtime context attached.",
            "message_type": "FEEDBACK",
            "workbench_context": workbench_context,
        },
    )

    assert legacy_resp.status_code == 200, legacy_resp.text
    assert canonical_resp.status_code == 200, canonical_resp.text
    assert set(legacy_resp.json()) == {"id", "status", "arming"}
    assert canonical_resp.json()["message"]["id"] == canonical_resp.json()["id"]

    for project_id, message_id in (
        (project_scoped_id, legacy_resp.json()["id"]),
        (canonical_id, canonical_resp.json()["id"]),
    ):
        context = await _stored_message_context(message_id)
        session = context["strategic_session"]
        assert session["interaction_mode"] == "chat"
        assert session["active_surface"] == "runtime"
        assert session["session_started_at"] == "2026-06-29T12:00:00Z"
        assert session["context_package_id"] == "strategic-context:shared"
        assert session["selected_refs"] == [
            {"kind": "canvas", "id": "strategic-canvas:shared", "label": "Context map"},
            {"kind": "artifact", "id": "artifact-1", "label": "x" * 160},
        ]
        assert "storage_uri" not in session

        stored_workbench = await _stored_workbench(project_id)
        assert stored_workbench["interaction_mode"] == "chat"
        assert stored_workbench["active_surface"] == "runtime"
        assert stored_workbench["selected_refs"] == session["selected_refs"]
        assert "context_package_id" not in stored_workbench
        assert "storage_uri" not in stored_workbench


@pytest.mark.asyncio
async def test_passive_note_does_not_arm_strategic_refresh(
    client: AsyncClient, db_session, mocker
):
    """A passive NOTE must NOT wake the strategist (no refresh marker)."""
    from app.strategic_trigger import strategic_refresh_pending

    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/messages",
        json={"content": "FYI, just a note", "message_type": "NOTE"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["arming"] == "passive_note"

    vision = await _reload_vision(db_session, project_id)
    assert strategic_refresh_pending(vision) is False


@pytest.mark.asyncio
async def test_operator_message_arm_failure_reports_not_armed(
    client: AsyncClient, db_session, mocker
):
    """Parity with the canonical endpoint: when a genuine steering message
    cannot persist its wake marker (contended row exhausts the bounded acquire),
    the CLI endpoint surfaces the truthful ``not_armed`` outcome and arms no
    marker — never a false ``steering`` promise."""
    from app.strategic_trigger import strategic_refresh_pending

    project_id = await _deploy_project(client, mocker)
    mocker.patch(
        "app.strategic_trigger.request_strategic_refresh",
        new=AsyncMock(return_value=False),
    )
    resp = await client.post(
        f"/api/admin/projects/{project_id}/messages",
        json={"content": "Please refocus on the auth bug", "message_type": "FEEDBACK"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "created"
    assert resp.json()["arming"] == "not_armed"

    vision = await _reload_vision(db_session, project_id)
    assert strategic_refresh_pending(vision) is False


@pytest.mark.asyncio
async def test_operator_message_burst_coalesces_into_one_slot(
    client: AsyncClient, db_session, mocker
):
    """A burst of operator messages collapses into a single coalesced marker."""
    from app.strategic_trigger import strategic_refresh_pending

    project_id = await _deploy_project(client, mocker)
    for message_type in ("QUESTION", "BLOCKER"):
        resp = await client.post(
            f"/api/admin/projects/{project_id}/messages",
            json={"content": f"steer {message_type}", "message_type": message_type},
        )
        assert resp.status_code == 200, resp.text

    vision = await _reload_vision(db_session, project_id)
    assert strategic_refresh_pending(vision) is True
    marker = vision["_strategic_refresh"]
    # Single slot — two messages bumped the observability count, not stacked.
    assert marker["count"] == 2
    assert marker["reason"] == "operator_message"
    # Coalesced burst stays operator-origin (sticky), so the bypass holds.
    assert marker["origin"] == "operator"


@pytest.mark.asyncio
async def test_operator_message_marker_is_operator_origin_and_bypasses_breaker(
    client: AsyncClient, db_session, mocker
):
    """End-to-end producer wiring: an operator message arms an operator-origin
    marker whose breaker gate is bypassed even when the strategist failure
    breaker is tripped (>= 3 failures). The unit tests construct operator-origin
    markers by hand; this drives the real producer endpoint so a missing
    origin wire-up (the bug this fix closes) can't slip through green."""
    from app.strategic_trigger import (
        ORIGIN_OPERATOR,
        refresh_breaker_applies,
        strategic_refresh_origin,
    )

    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/messages",
        json={
            "content": "The loop is stuck — pivot to the deploy bug",
            "message_type": "BLOCKER",
        },
    )
    assert resp.status_code == 200, resp.text

    vision = await _reload_vision(db_session, project_id)
    # The producer stamped operator origin (not the default qualification).
    assert strategic_refresh_origin(vision) == ORIGIN_OPERATOR
    # Operator intent bypasses the _strategic_failures breaker — the gate is
    # open even with the breaker tripped, which is exactly when an operator
    # needs to steer.
    assert refresh_breaker_applies(vision) is False
    strategic_failures = 5  # breaker tripped (>= 3)
    breaker_gate_ok = not refresh_breaker_applies(vision) or strategic_failures < 3
    assert breaker_gate_ok is True


@pytest.mark.asyncio
async def test_operator_message_deferral_notice_when_cycle_active(
    client: AsyncClient, db_session, mocker
):
    """When a strategic cycle is already active, the operator-origin marker drives
    a one-time deferral notice through the real consumer path — proving the
    notice fires (it was silently swallowed under the buggy qualification
    origin) and stamps ``notified`` so it does not repeat."""
    from app.autonomous_research import _maybe_notify_operator_refresh_deferred
    from app.strategic_trigger import strategic_refresh_notified

    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/messages",
        json={
            "content": "Reprioritize the auth regression",
            "message_type": "FEEDBACK",
        },
    )
    assert resp.status_code == 200, resp.text

    # The dispatch helper is imported inside the consumer from
    # app.notification_dispatch — patch it there.
    dispatch = mocker.patch(
        "app.notification_dispatch.post_strategic_steering_deferred_message",
        new=AsyncMock(),
    )

    db_session.expire_all()
    project = (
        await db_session.execute(
            select(Project).where(Project.id == uuid.UUID(project_id))
        )
    ).scalar_one()

    await _maybe_notify_operator_refresh_deferred(
        project, db_session, "queued behind an active strategic cycle"
    )

    dispatch.assert_awaited_once()
    vision = await _reload_vision(db_session, project_id)
    assert strategic_refresh_notified(vision) is True

    # Idempotent: a second tick with the marker already notified stays quiet.
    dispatch.reset_mock()
    db_session.expire_all()
    project = (
        await db_session.execute(
            select(Project).where(Project.id == uuid.UUID(project_id))
        )
    ).scalar_one()
    await _maybe_notify_operator_refresh_deferred(
        project, db_session, "queued behind an active strategic cycle"
    )
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_direction_emits_strategic_updated(client: AsyncClient, mocker):
    """POST /directions broadcasts a strategic_updated(directions) refetch signal."""
    project_id = await _deploy_project(client, mocker)
    bcast = mocker.patch("app.strategic_events.broadcast_to_project", new=AsyncMock())
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "Live-refresh signal", "priority": "medium"},
    )
    assert resp.status_code == 200, resp.text
    events = [c.args for c in bcast.await_args_list if c.args[1] == "strategic_updated"]
    assert len(events) == 1, "expected one strategic_updated broadcast"
    assert events[0][0] == str(project_id)
    payload = events[0][2]
    assert payload["kind"] == "directions"
    assert payload["resource"] == "strategic_output"
    assert payload["strategic"]["output_kind"] == "directions"
    assert payload["refetch"]["surfaces"] == ["strategic_session", "directions"]


@pytest.mark.asyncio
async def test_remove_direction_emits_strategic_updated(client: AsyncClient, mocker):
    """DELETE /directions/{id} broadcasts a strategic_updated(directions) signal."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)
    bcast = mocker.patch("app.strategic_events.broadcast_to_project", new=AsyncMock())
    resp = await client.delete(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}"
    )
    assert resp.status_code == 200, resp.text
    events = [c.args for c in bcast.await_args_list if c.args[1] == "strategic_updated"]
    assert len(events) == 1
    payload = events[0][2]
    assert payload["kind"] == "directions"
    assert payload["resource"] == "strategic_output"
    assert payload["strategic"]["output_kind"] == "directions"
    assert payload["refetch"]["surfaces"] == ["strategic_session", "directions"]


@pytest.mark.asyncio
async def test_update_direction_round_trips_success_evidence_and_retirement_condition(
    db_session, mocker
):
    """update_direction via /api/agent/strategic persists success_evidence and
    retirement_condition onto direction context and the values round-trip back."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    direction_id = "d-rt-se-rc"
    project = Project(
        name="P-rt-se-rc",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": direction_id,
                "text": "Original direction text",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
                "context": {
                    "success_evidence": "old evidence",
                    "retirement_condition": "old retirement",
                },
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        user_id="u1",
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"command": "review", "vision": {"revision": 1}}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = str(task.id)

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "update_direction",
                    "task_id": task_id,
                    "data": {
                        "direction_id": direction_id,
                        "changes": {
                            "success_evidence": "updated observable outcome",
                            "retirement_condition": "updated measurable retirement condition",
                        },
                        "rationale": "Reframed acceptance criteria",
                    },
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["direction_id"] == direction_id

        await db_session.refresh(project)
        directions = project.directions or []
        direction = next((d for d in directions if d.get("id") == direction_id), None)
        assert direction is not None, "direction not found after update"
        ctx = direction.get("context") or {}
        assert ctx.get("success_evidence") == "updated observable outcome"
        assert (
            ctx.get("retirement_condition") == "updated measurable retirement condition"
        )
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


# ---------------------------------------------------------------------------
# conflicts_with round-trip tests via agent strategic API
# ---------------------------------------------------------------------------


async def _setup_strategic_fixtures(db_session, directions_list, project_name):
    """Create project + task, return (project, task, task_id)."""
    project = Project(
        name=project_name,
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=directions_list,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        user_id="u1",
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"command": "review", "vision": {"revision": 1}}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task, str(task.id)


@pytest.mark.asyncio
async def test_strategic_api_persists_conflicts_with(db_session, mocker):
    """create_direction with conflicts_with persists onto the new direction.

    Round-trip: create d-cw-first, then create d-cw-second with conflicts_with=[d-cw-first].
    After the second create the stored direction should carry conflicts_with.
    """
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    project, task, task_id = await _setup_strategic_fixtures(
        db_session,
        [
            {
                "id": "d-cw-first",
                "text": "First direction",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
                "context": {
                    "success_evidence": "ev1",
                    "retirement_condition": "rc1",
                },
            }
        ],
        "P-cw-create",
    )

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "create_direction",
                    "task_id": task_id,
                    "data": {
                        "text": "Second direction that conflicts with first",
                        "priority": "medium",
                        "success_evidence": "no parallel run with first",
                        "retirement_condition": "first direction retired",
                        "conflicts_with": ["d-cw-first"],
                    },
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        new_id = body["direction_id"]

        await db_session.refresh(project)
        directions = project.directions or []
        new_dir = next((d for d in directions if d.get("id") == new_id), None)
        assert new_dir is not None, "new direction not found"
        assert new_dir.get("conflicts_with") == ["d-cw-first"]
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


@pytest.mark.asyncio
async def test_strategic_api_create_direction_conflicts_with_unknown_id_returns_400(
    db_session, mocker
):
    """create_direction with an unknown ID in conflicts_with returns 400."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    project, task, task_id = await _setup_strategic_fixtures(
        db_session, [], "P-cw-create-bad"
    )

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "create_direction",
                    "task_id": task_id,
                    "data": {
                        "text": "Direction with bad conflicts_with",
                        "priority": "medium",
                        "success_evidence": "ev",
                        "retirement_condition": "rc",
                        "conflicts_with": ["d-nonexistent"],
                    },
                },
            )
        assert resp.status_code == 400, resp.text
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


@pytest.mark.asyncio
async def test_strategic_api_update_direction_sets_conflicts_with(db_session, mocker):
    """update_direction with changes.conflicts_with persists onto the direction."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    project, task, task_id = await _setup_strategic_fixtures(
        db_session,
        [
            {
                "id": "d-upd-a",
                "text": "Direction A",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
                "context": {"success_evidence": "ev", "retirement_condition": "rc"},
            },
            {
                "id": "d-upd-b",
                "text": "Direction B",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
                "context": {"success_evidence": "ev", "retirement_condition": "rc"},
            },
        ],
        "P-cw-update",
    )

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "update_direction",
                    "task_id": task_id,
                    "data": {
                        "direction_id": "d-upd-a",
                        "changes": {"conflicts_with": ["d-upd-b"]},
                    },
                },
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"

        await db_session.refresh(project)
        dir_a = next(
            (d for d in (project.directions or []) if d.get("id") == "d-upd-a"), None
        )
        assert dir_a is not None
        assert dir_a.get("conflicts_with") == ["d-upd-b"]
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


@pytest.mark.asyncio
async def test_strategic_api_update_direction_clears_conflicts_with(db_session, mocker):
    """update_direction with changes.conflicts_with=null removes the key."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    project, task, task_id = await _setup_strategic_fixtures(
        db_session,
        [
            {
                "id": "d-clr-a",
                "text": "Direction A",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
                "conflicts_with": ["d-clr-b"],
                "context": {"success_evidence": "ev", "retirement_condition": "rc"},
            },
            {
                "id": "d-clr-b",
                "text": "Direction B",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
                "context": {"success_evidence": "ev", "retirement_condition": "rc"},
            },
        ],
        "P-cw-clear",
    )

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "update_direction",
                    "task_id": task_id,
                    "data": {
                        "direction_id": "d-clr-a",
                        "changes": {"conflicts_with": None},
                    },
                },
            )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(project)
        dir_a = next(
            (d for d in (project.directions or []) if d.get("id") == "d-clr-a"), None
        )
        assert dir_a is not None
        assert "conflicts_with" not in dir_a
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


@pytest.mark.asyncio
async def test_strategic_api_update_direction_conflicts_with_unknown_id_returns_400(
    db_session, mocker
):
    """update_direction with an unknown ID in changes.conflicts_with returns 400."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    project, task, task_id = await _setup_strategic_fixtures(
        db_session,
        [
            {
                "id": "d-bad-upd",
                "text": "A direction",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
                "context": {"success_evidence": "ev", "retirement_condition": "rc"},
            }
        ],
        "P-cw-update-bad",
    )

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "update_direction",
                    "task_id": task_id,
                    "data": {
                        "direction_id": "d-bad-upd",
                        "changes": {"conflicts_with": ["d-does-not-exist"]},
                    },
                },
            )
        assert resp.status_code == 400, resp.text
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


@pytest.mark.asyncio
async def test_strategic_api_reject_direction_cancels_queued_tasks(
    db_session, mocker
):
    """reject_direction cancels QUEUED tasks tied to the rejected direction."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    dir_id = "d-rej-cancel-01"
    existing_dirs = [
        {
            "id": dir_id,
            "text": "Direction to reject",
            "status": "proposed",
            "priority": "medium",
            "notes": [],
            "research_history": [],
        }
    ]
    project = Project(
        name="P-reject-cancel",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=existing_dirs,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create enough QUEUED tasks to force the 5000-row batch loop to iterate twice.
    queued_tasks = []
    for i in range(5001):
        t = Task(
            project_id=project.id,
            user_id="u1",
            status=TaskStatus.QUEUED,
            payloads=[{"type": "create", "prompt": f"task {i}"}],
            context={"direction_id": dir_id},
        )
        db_session.add(t)
        queued_tasks.append(t)
    # One QUEUED task for a different direction — should NOT be cancelled
    other_task = Task(
        project_id=project.id,
        user_id="u1",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "other"}],
        context={"direction_id": "d-other"},
    )
    db_session.add(other_task)
    await db_session.commit()

    sr_task = Task(
        project_id=project.id,
        user_id="u1",
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"command": "review"}],
    )
    db_session.add(sr_task)
    await db_session.commit()
    await db_session.refresh(sr_task)

    task_id = str(sr_task.id)

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "reject_direction",
                    "task_id": task_id,
                    "data": {
                        "direction_id": dir_id,
                        "rationale": "not aligned with focus",
                    },
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["op"] == "reject_direction"
        assert body["cancelled_tasks"] == len(queued_tasks)

        # All QUEUED tasks for the rejected direction are now CANCELLED
        for t in queued_tasks:
            await db_session.refresh(t)
            assert t.status == TaskStatus.CANCELLED, (
                f"Expected CANCELLED, got {t.status}"
            )

        # Task for a different direction is untouched
        await db_session.refresh(other_task)
        assert other_task.status == TaskStatus.QUEUED

    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


# ─────────────────────────────────────────────────────────────────────────────
# Guarded resurrection endpoint
# ─────────────────────────────────────────────────────────────────────────────


def _churn_history(now: datetime) -> list[dict]:
    """5 cycles that trip the churn guardrail (Rule 5): 1 merge total, ~2% rate."""
    return [
        {
            "timestamp": (now - timedelta(days=5)).isoformat(),
            "tasks_created": 10,
            "tasks_completed": 10,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": 1,  # one merge => total_merged=1, bypasses Rule 2
        },
        *[
            {
                "timestamp": (now - timedelta(days=i)).isoformat(),
                "tasks_created": 10,
                "tasks_completed": 10,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 0,
            }
            for i in range(4, 0, -1)
        ],
    ]


@pytest.mark.asyncio
async def test_resurrect_pruned_direction_resets_ladder_and_restaffs_gap(
    client: AsyncClient, db_session, mocker
):
    """End-to-end: auto-retire+prune a gap-linked direction, then resurrect it.

    Asserts the resurrected direction is back in project.directions with
    status='active', every auto-retire ladder counter reset to 0, terminal
    residue cleared, auto_recovered_at (grace) set, and its id re-staffed into the
    owning gap's active_moves.
    """
    from sqlalchemy.orm.attributes import flag_modified
    from app.directions_repo import sync_directions

    project_id = await _deploy_project(client, mocker)
    pid = uuid.UUID(project_id)
    now = _utcnow()

    # The exact terminal end-state the zombie auto-retire ladder leaves: status
    # 'completed', auto_completed residue stamped, EVERY ladder counter primed at
    # or above its threshold, and a gap link. This is what a raw completed->active
    # flip cannot safely undo.
    direction = {
        "id": "d-resurrect-me",
        "text": "Direction that was wrongly auto-retired",
        "status": "completed",
        "auto_completed": True,
        "auto_completed_reason": (
            "Auto-retired: zombie direction paused 2 times with no merged PRs."
        ),
        "completed_at": now.isoformat(),
        "priority": "medium",
        "paths": ["backend/app/foo.py"],
        "added_at": "2026-01-01T00:00:00",
        "gap_id": "g-resurrect",
        "churn_family_pause_count": 2,
        "lifetime_burn_pause_count": 2,
        "code_failure_pause_count": 1,
        "auto_pause_count": 3,
        "churn_pause_evidence": {"non_merge_finished": 49, "merge_rate": 0.02},
        "lifetime_burn_pause_evidence": {"lifetime_non_merge": 200},
        "research_history": _churn_history(now),
    }
    project = (
        await db_session.execute(select(Project).where(Project.id == pid))
    ).scalar_one()
    project.autonomous_enabled = True
    # Row already pruned out of the blob (as the lifecycle sweep does within one
    # tick), and the gap already unstaffed (the retire reconciliation moved the
    # direction from active_moves into past_moves).
    project.directions = []
    project.problem_model = {
        "revision": 1,
        "gaps": [
            {
                "id": "g-resurrect",
                "name": "the gap this direction staffs",
                "status": "active",
                "active_moves": [],
                "past_moves": ["d-resurrect-me"],
            }
        ],
    }
    flag_modified(project, "directions")
    flag_modified(project, "problem_model")
    db_session.add(project)
    await db_session.commit()

    # (a) Reproduce the append-only DirectionEvent trail the real retire+prune
    # emits: a snapshot-carrying row is written, then pruned. The pruned event's
    # snapshot_data is what makes a vanished direction resurrectable at all.
    await sync_directions(db_session, pid, [direction])
    await db_session.commit()
    await sync_directions(db_session, pid, [])  # prune -> emits pruned snapshot event
    await db_session.commit()

    # Precondition: the direction is gone and the gap is unstaffed.
    db_session.expire_all()
    project = (
        await db_session.execute(select(Project).where(Project.id == pid))
    ).scalar_one()
    assert all(d.get("id") != "d-resurrect-me" for d in (project.directions or []))
    gap = next(g for g in project.problem_model["gaps"] if g["id"] == "g-resurrect")
    assert "d-resurrect-me" not in gap["active_moves"], "gap unstaffed before resurrect"

    # (b) Resurrect via the endpoint (reconstructs from the DirectionEvent snapshot).
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/d-resurrect-me/resurrect",
        json={"rationale": "operator confirmed this retirement was wrong"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resurrected_at"]
    d = body["direction"]

    # (c) Clean, resurrectable end-state: ladder reset, residue stripped, grace set.
    assert d["status"] == "active"
    assert d["churn_family_pause_count"] == 0
    assert d["lifetime_burn_pause_count"] == 0
    assert d["code_failure_pause_count"] == 0
    assert d["auto_pause_count"] == 0
    assert "churn_pause_evidence" not in d
    assert "lifetime_burn_pause_evidence" not in d
    assert "auto_completed" not in d
    assert "auto_completed_reason" not in d
    assert "completed_at" not in d
    assert d.get("auto_recovered_at") is not None
    assert d.get("resurrected_at") is not None
    # Full contract reconstructed from the snapshot even though the row was gone.
    assert d["gap_id"] == "g-resurrect"
    assert d["paths"] == ["backend/app/foo.py"]

    # Persisted back into project.directions + gap re-staffed. Read through the
    # GET endpoint (app session) so the assertion sees the committed write.
    get_resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert get_resp.status_code == 200, get_resp.text
    get_body = get_resp.json()
    ids = [x.get("id") for x in (get_body["directions"] or [])]
    assert "d-resurrect-me" in ids, "resurrected direction back in project.directions"
    revived = next(
        x for x in get_body["directions"] if x.get("id") == "d-resurrect-me"
    )
    assert revived["status"] == "active"
    gap = next(
        g for g in get_body["problem_model"]["gaps"] if g["id"] == "g-resurrect"
    )
    assert "d-resurrect-me" in gap["active_moves"], (
        "resurrected direction id must be re-staffed into its gap's active_moves"
    )


@pytest.mark.asyncio
async def test_resurrect_direction_409_when_at_active_cap(
    client: AsyncClient, db_session, mocker
):
    """Resurrection enforces the active-direction cap exactly like reactivation."""
    from sqlalchemy.orm.attributes import flag_modified
    from app.shared_state import runtime_config

    project_id = await _deploy_project(client, mocker)
    pid = uuid.UUID(project_id)

    project = (
        await db_session.execute(select(Project).where(Project.id == pid))
    ).scalar_one()
    project.directions = [
        {
            "id": "d-active-1",
            "text": "already active",
            "status": "active",
            "priority": "medium",
            "paths": [],
            "added_at": "2026-01-01T00:00:00",
        },
        {
            "id": "d-terminal",
            "text": "terminal direction to resurrect",
            "status": "completed",
            "auto_completed": True,
            "completed_at": "2026-06-01T00:00:00",
            "priority": "medium",
            "paths": [],
            "added_at": "2026-01-01T00:00:00",
        },
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    prior_cap = runtime_config.get("max_active_directions")
    runtime_config["max_active_directions"] = 1
    try:
        resp = await client.post(
            f"/api/admin/projects/{project_id}/directions/d-terminal/resurrect",
            json={},
        )
    finally:
        if prior_cap is None:
            runtime_config.pop("max_active_directions", None)
        else:
            runtime_config["max_active_directions"] = prior_cap
    assert resp.status_code == 409, resp.text
    assert "cap" in resp.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_resurrect_direction_404_when_no_snapshot(
    client: AsyncClient, db_session, mocker
):
    """Resurrecting a pruned direction with no event snapshot returns 404."""
    project_id = await _deploy_project(client, mocker)

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/d-never-existed/resurrect",
        json={},
    )
    assert resp.status_code == 404, resp.text
    assert "snapshot" in resp.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_resurrect_snapshot_with_non_terminal_status_returns_409(
    client: AsyncClient, db_session, mocker
):
    """Resurrection from a non-resurrectable snapshot (e.g. 'rejected') returns 409.

    A snapshot captured while the direction was in a non-terminal, non-resurrectable
    state (status not in {completed, expired, superseded} and auto_completed unset)
    must be rejected by the endpoint with a 409, not silently reinserted as active.
    """
    from sqlalchemy.orm.attributes import flag_modified
    from app.directions_repo import sync_directions

    project_id = await _deploy_project(client, mocker)
    pid = uuid.UUID(project_id)

    # A 'rejected' direction (strategic proposal rejected — not auto-completed, not
    # in a resurrectable terminal status). This is the state that reaches terminal
    # via a non-retirement path, and _is_terminal_direction returns False for it.
    rejected_direction = {
        "id": "d-rejected-snap",
        "text": "A rejected strategic proposal",
        "status": "rejected",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
    }

    project = (
        await db_session.execute(select(Project).where(Project.id == pid))
    ).scalar_one()
    project.directions = []
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    # Write the direction into the event trail then prune it, so a snapshot
    # exists for d-rejected-snap with status='rejected'.
    await sync_directions(db_session, pid, [rejected_direction])
    await db_session.commit()
    await sync_directions(db_session, pid, [])  # prune -> emits pruned snapshot event
    await db_session.commit()

    # Resurrect endpoint must reject: snapshot status='rejected' is not resurrectable.
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/d-rejected-snap/resurrect",
        json={"rationale": "trying to resurrect a rejected direction"},
    )
    assert resp.status_code == 409, resp.text
    assert "terminal" in resp.json()["error"]["message"].lower(), (
        f"Expected 'terminal' in error message, got: {resp.json()['error']['message']!r}"
    )


@pytest.mark.asyncio
async def test_gap_staffing_reconcile_survives_concurrent_problem_model_write(
    client: AsyncClient, db_session, mocker
):
    """Gap-staffing reconciliation on direction PATCH reads problem_model inside
    with_problem_model_locked, not from a stale project snapshot.

    Simulates the concurrent-write race: the project is loaded before the
    directions lock is acquired, then a concurrent session modifies problem_model.
    The endpoint must reconcile against the freshly-locked value, not the stale
    pre-lock snapshot.  Verified by:

    1. Spying on with_problem_model_locked to assert the new code path is active.
    2. Asserting the DB reflects the correct reconciled state (direction moved
       from g-alpha active_moves to g-beta active_moves after the PATCH).
    """
    from sqlalchemy.orm.attributes import flag_modified
    from app.task_locking import with_problem_model_locked as _orig_wpm

    project_id = await _deploy_project(client, mocker)
    pid = uuid.UUID(project_id)

    direction_id = "d-concurrent-pm-test"
    project = (
        await db_session.execute(select(Project).where(Project.id == pid))
    ).scalar_one()
    project.directions = [
        {
            "id": direction_id,
            "text": "test concurrent pm direction",
            "status": "active",
            "priority": "medium",
            "paths": [],
            "added_at": "2026-01-01T00:00:00",
            "gap_id": "g-alpha",
        }
    ]
    project.problem_model = {
        "revision": 1,
        "gaps": [
            {
                "id": "g-alpha",
                "name": "alpha gap",
                "status": "active",
                "active_moves": [direction_id],
                "past_moves": [],
            },
            {
                "id": "g-beta",
                "name": "beta gap",
                "status": "active",
                "active_moves": [],
                "past_moves": [],
            },
        ],
    }
    flag_modified(project, "directions")
    flag_modified(project, "problem_model")
    db_session.add(project)
    await db_session.commit()

    # Spy: record every invocation of with_problem_model_locked so we can
    # assert the new locked code path is exercised (not the old stale read).
    pm_locked_calls: list[bool] = []

    async def _spy_wpm(session, project_id, mutate_fn, **kwargs):
        pm_locked_calls.append(True)
        return await _orig_wpm(session, project_id, mutate_fn, **kwargs)

    mocker.patch("app.routers.directions.with_problem_model_locked", side_effect=_spy_wpm)

    # PATCH: move direction from gap g-alpha to g-beta.  The reconciliation
    # should (a) call with_problem_model_locked, (b) remove direction from
    # g-alpha active_moves / add to past_moves, and (c) add it to g-beta
    # active_moves — all using the freshly-locked PM, not a stale snapshot.
    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={"gap_id": "g-beta"},
    )
    assert resp.status_code == 200, resp.text

    # (1) The new locked path was exercised.
    assert pm_locked_calls, (
        "with_problem_model_locked must be called for gap-staffing reconciliation "
        "on direction PATCH — stale direct read is no longer acceptable"
    )

    # (2) Correct reconciled state in the DB. Use refresh on the existing object
    # (same pattern as test_operator_update_direction_reconciles_gap_staffing*).
    await db_session.refresh(project)
    gaps = {g["id"]: g for g in project.problem_model["gaps"]}
    assert direction_id not in gaps["g-alpha"]["active_moves"], (
        "direction must be removed from g-alpha active_moves after gap change"
    )
    assert direction_id in gaps["g-alpha"]["past_moves"], (
        "direction must appear in g-alpha past_moves after leaving the gap"
    )
    assert direction_id in gaps["g-beta"]["active_moves"], (
        "direction must be added to g-beta active_moves after gap change"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Role floor on the project message board.
#
# POST /api/admin/projects/{project_id}/messages was the LONE mutating outlier in
# this router: every other mutating route here takes
# _get_user_project_with_role(..., "member"), but this one took the
# membership-only _get_user_project. It is a near-duplicate of
# admin_notifications' POST /messages, so gating that one alone left this route
# as a trivial bypass.
#
# It is not a benign write: it calls create_operator_project_message ->
# maybe_arm_operator_refresh, which arms an OPERATOR-origin strategic refresh
# (breaker-bypassing, cooldown-bypassing — see
# test_operator_message_arms_strategic_refresh above) and persists the caller's
# steering text into the project-global Project.vision column. Without the
# member+ floor, a viewer can steer the autonomous strategist.
# ─────────────────────────────────────────────────────────────────────────────


async def _make_project_with_role(db_session, name: str, role: str):
    """Project with test-user-123 holding the given role."""
    from app.models import ProjectMember

    project = Project(
        name=name,
        user_id="test-user-123",
        vision={"purpose": "test"},
        design_doc={"proposed_changes": []},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role=role)
    )
    await db_session.commit()
    return project


@pytest.mark.asyncio
async def test_create_message_viewer_forbidden(client: AsyncClient, db_session):
    """A viewer gets 403; no message row lands and no refresh is armed."""
    from sqlalchemy import func as sa_func
    from app.models import ProjectMessage
    from app.strategic_trigger import strategic_refresh_pending

    project = await _make_project_with_role(db_session, "Viewer Board", "viewer")

    resp = await client.post(
        f"/api/admin/projects/{project.id}/messages",
        json={"content": "Drop everything and rewrite auth", "message_type": "FEEDBACK"},
    )

    assert resp.status_code == 403, resp.text
    assert "member" in resp.json()["error"]["message"].lower()

    # No message persisted...
    count = await db_session.scalar(
        select(sa_func.count())
        .select_from(ProjectMessage)
        .where(ProjectMessage.project_id == project.id)
    )
    assert count == 0

    # ...and the project-global vision carries no viewer-armed operator refresh.
    vision = await _reload_vision(db_session, str(project.id))
    assert strategic_refresh_pending(vision) is False


@pytest.mark.asyncio
async def test_create_message_member_allowed(client: AsyncClient, db_session):
    """The floor must not over-reject: a member (not owner) can still steer."""
    from sqlalchemy import func as sa_func
    from app.models import ProjectMessage
    from app.strategic_trigger import strategic_refresh_pending

    project = await _make_project_with_role(db_session, "Member Board", "member")

    resp = await client.post(
        f"/api/admin/projects/{project.id}/messages",
        json={"content": "Please refocus on the auth bug", "message_type": "FEEDBACK"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "created"

    count = await db_session.scalar(
        select(sa_func.count())
        .select_from(ProjectMessage)
        .where(ProjectMessage.project_id == project.id)
    )
    assert count == 1

    vision = await _reload_vision(db_session, str(project.id))
    assert strategic_refresh_pending(vision) is True
    assert vision["_strategic_refresh"]["origin"] == "operator"

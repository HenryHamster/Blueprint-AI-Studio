"""Tests for the self_audit task end-to-end:

1. ``_handle_self_audit_completion`` ingest path — SELF_AUDIT_REPORT logs
   produce SystemMaturity rows, idempotent re-ingest, malformed entries are
   dropped with a warning, invalid recommended_mode is rejected.
2. ``_normalize_self_audit_finding`` — pure-function validator behavior.
3. ``POST /api/admin/projects/{project_id}/self-audit`` — enqueues a Task with
   the right payload type and forwards focus_systems / max_systems.
"""
import json
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.models import (
    Project, ProjectMember, SystemMaturity, Task, TaskStatus,
)
from app.task_completion import (
    _handle_self_audit_completion,
    _normalize_self_audit_finding,
    _parse_self_audit_findings,
)
from testing.factories import make_project, make_task


TEST_USER_ID = "test-user-123"


async def _seed_project_with_member(session):
    project = make_project(name="Self Audit Test", user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner"))
    await session.commit()
    return project


# ── Pure-function validators ────────────────────────────────────────────────

class TestNormalizeFinding:
    def test_full_valid_finding_round_trips(self):
        raw = {
            "system_id": "sys-auth",
            "score": 72,
            "components": {
                "test_coverage": 80, "friction_density": 65,
                "claim_consistency": 75, "failure_mix": 70,
            },
            "top_issues": ["Missing integration tests", "Flaky CI"],
            "recommended_mode": "fundamentals",
            "evidence": [{"file": "backend/app/auth.py", "summary": "no tests"}],
        }
        norm = _normalize_self_audit_finding(raw)
        assert norm is not None
        assert norm["system_id"] == "sys-auth"
        assert norm["score"] == 72
        assert norm["recommended_mode"] == "fundamentals"
        assert norm["components"]["test_coverage"] == 80
        # string issues get wrapped as {issue: str}
        assert norm["top_issues"][0] == {"issue": "Missing integration tests"}
        assert norm["evidence"][0]["file"] == "backend/app/auth.py"

    def test_invalid_recommended_mode_rejected(self):
        raw = {"system_id": "sys-x", "score": 50, "recommended_mode": "panic"}
        assert _normalize_self_audit_finding(raw) is None

    def test_missing_system_id_rejected(self):
        assert _normalize_self_audit_finding({"score": 50, "recommended_mode": "ship"}) is None
        assert _normalize_self_audit_finding({"system_id": "", "score": 50}) is None
        assert _normalize_self_audit_finding({"system_id": "   ", "score": 50}) is None

    def test_score_clamped_to_0_100(self):
        assert _normalize_self_audit_finding({"system_id": "s", "score": -10})["score"] == 0
        assert _normalize_self_audit_finding({"system_id": "s", "score": 999})["score"] == 100

    def test_non_integer_score_rejected(self):
        assert _normalize_self_audit_finding({"system_id": "s", "score": "n/a"}) is None

    def test_recommended_mode_optional(self):
        norm = _normalize_self_audit_finding({"system_id": "s", "score": 50})
        assert norm is not None
        assert norm["recommended_mode"] is None

    def test_non_dict_returns_none(self):
        assert _normalize_self_audit_finding("not a dict") is None  # type: ignore[arg-type]
        assert _normalize_self_audit_finding(None) is None  # type: ignore[arg-type]


class TestParseFindings:
    def test_parses_object_with_findings_key(self):
        logs = json.dumps({"findings": [{"system_id": "a", "score": 50}]})
        out = _parse_self_audit_findings(logs)
        assert len(out) == 1
        assert out[0]["system_id"] == "a"

    def test_parses_bare_array(self):
        logs = json.dumps([{"system_id": "a", "score": 50}])
        out = _parse_self_audit_findings(logs)
        assert len(out) == 1

    def test_parses_array_with_surrounding_text(self):
        logs = 'preamble\n[{"system_id":"a","score":50}]\ntrailing'
        out = _parse_self_audit_findings(logs)
        assert len(out) == 1
        assert out[0]["system_id"] == "a"

    def test_empty_logs_returns_empty(self):
        assert _parse_self_audit_findings("") == []
        assert _parse_self_audit_findings("no json here") == []


# ── Completion ingest ───────────────────────────────────────────────────────

class TestSelfAuditCompletion:
    @pytest.mark.asyncio
    async def test_ingest_creates_system_maturity_rows(self, db_session, mock_broadcast):
        project = await _seed_project_with_member(db_session)
        task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()

        logs = json.dumps({"findings": [
            {
                "system_id": "sys-auth",
                "score": 72,
                "components": {"test_coverage": 80, "friction_density": 65,
                               "claim_consistency": 75, "failure_mix": 70},
                "top_issues": ["Missing integration tests"],
                "recommended_mode": "fundamentals",
                "evidence": [{"file": "backend/app/auth.py", "summary": "no tests"}],
            },
            {
                "system_id": "sys-billing",
                "score": 30,
                "components": {"test_coverage": 20, "friction_density": 30,
                               "claim_consistency": 30, "failure_mix": 30},
                "top_issues": ["No tests at all"],
                "recommended_mode": "audit",
                "evidence": [],
            },
        ]})

        await _handle_self_audit_completion(task, logs, db_session)

        rows = (await db_session.execute(
            select(SystemMaturity).where(SystemMaturity.project_id == project.id)
        )).scalars().all()
        assert len(rows) == 2
        by_system = {r.system_id: r for r in rows}
        assert by_system["sys-auth"].score == 72
        assert by_system["sys-auth"].recommended_mode == "fundamentals"
        assert by_system["sys-auth"].source_task_id == task.id
        assert by_system["sys-billing"].score == 30
        assert by_system["sys-billing"].recommended_mode == "audit"

    @pytest.mark.asyncio
    async def test_ingest_is_idempotent(self, db_session, mock_broadcast):
        """Re-running an audit overwrites the existing row in place."""
        project = await _seed_project_with_member(db_session)
        first_task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(first_task)
        await db_session.commit()

        first_logs = json.dumps({"findings": [{
            "system_id": "sys-auth", "score": 50, "recommended_mode": "fundamentals",
            "components": {"test_coverage": 50, "friction_density": 50,
                           "claim_consistency": 50, "failure_mix": 50},
            "top_issues": [], "evidence": [],
        }]})
        await _handle_self_audit_completion(first_task, first_logs, db_session)

        # Re-audit with a different score / mode under the same project_id+system_id
        second_task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(second_task)
        await db_session.commit()

        second_logs = json.dumps({"findings": [{
            "system_id": "sys-auth", "score": 90, "recommended_mode": "ship",
            "components": {"test_coverage": 90, "friction_density": 90,
                           "claim_consistency": 90, "failure_mix": 90},
            "top_issues": ["nothing"], "evidence": [{"file": "x.py", "summary": "ok"}],
        }]})
        await _handle_self_audit_completion(second_task, second_logs, db_session)

        rows = (await db_session.execute(
            select(SystemMaturity).where(SystemMaturity.project_id == project.id)
        )).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.score == 90
        assert row.recommended_mode == "ship"
        assert row.source_task_id == second_task.id
        assert row.evidence[0]["file"] == "x.py"

    @pytest.mark.asyncio
    async def test_ingest_drops_invalid_recommended_mode(self, db_session, mock_broadcast):
        project = await _seed_project_with_member(db_session)
        task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()

        logs = json.dumps({"findings": [
            {"system_id": "sys-good", "score": 50, "recommended_mode": "ship"},
            {"system_id": "sys-bad", "score": 50, "recommended_mode": "explode"},
        ]})
        await _handle_self_audit_completion(task, logs, db_session)

        rows = (await db_session.execute(
            select(SystemMaturity).where(SystemMaturity.project_id == project.id)
        )).scalars().all()
        # The valid finding lands; the invalid one is dropped.
        assert {r.system_id for r in rows} == {"sys-good"}

    @pytest.mark.asyncio
    async def test_ingest_no_findings_marks_status_message(self, db_session, mock_broadcast):
        project = await _seed_project_with_member(db_session)
        task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()

        await _handle_self_audit_completion(task, "completely unparseable garbage", db_session)
        await db_session.refresh(task)
        assert task.latest_status_message and "no parseable findings" in task.latest_status_message
        rows = (await db_session.execute(
            select(SystemMaturity).where(SystemMaturity.project_id == project.id)
        )).scalars().all()
        assert rows == []


# ── Admin endpoint ──────────────────────────────────────────────────────────

class TestSelfAuditAdminEndpoint:
    @pytest.mark.asyncio
    async def test_endpoint_creates_self_audit_task(self, client: AsyncClient, db_session):
        project = await _seed_project_with_member(db_session)

        resp = await client.post(f"/api/admin/projects/{project.id}/self-audit", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == TaskStatus.QUEUED
        task_id = uuid.UUID(body["task_id"])

        task = await db_session.get(Task, task_id)
        assert task is not None
        assert task.payloads[0]["type"] == "self_audit"
        assert task.created_by == "USER"

    @pytest.mark.asyncio
    async def test_endpoint_forwards_focus_systems_and_max(self, client: AsyncClient, db_session):
        project = await _seed_project_with_member(db_session)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/self-audit",
            json={"focus_systems": ["sys-auth", "sys-billing"], "max_systems": 5},
        )
        assert resp.status_code == 200, resp.text

        task = await db_session.get(Task, uuid.UUID(resp.json()["task_id"]))
        payload = task.payloads[0]
        assert payload["focus_systems"] == ["sys-auth", "sys-billing"]
        assert payload["max_systems"] == 5

    @pytest.mark.asyncio
    async def test_endpoint_rejects_unauthorized_project(self, client: AsyncClient, db_session):
        # Create a project owned by someone else (test user is NOT a member)
        other_project = make_project(name="Other", user_id="other-user-999")
        db_session.add(other_project)
        await db_session.commit()
        await db_session.refresh(other_project)

        resp = await client.post(f"/api/admin/projects/{other_project.id}/self-audit", json={})
        assert resp.status_code == 404
